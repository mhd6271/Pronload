"""Parallele Downloads über yt-dlp mit einem Fortschrittsbalken pro Datei."""
from __future__ import annotations

import hashlib
import re
from glob import escape as glob_escape
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests
import yt_dlp
from rich.console import Group
from rich.progress import (BarColumn, DownloadColumn, MofNCompleteColumn, Progress, SpinnerColumn,
                           TaskID, TextColumn, TimeRemainingColumn, TransferSpeedColumn)
from rich.table import Column

from .extract import USER_AGENT, analyze, path_ext, rank_media
from .net import NetGuard

DIRECT_EXT = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv")
TMP_DIR = ".pronload-tmp"
# Postprocessoren ohne nennenswerte Laufzeit - dafür keine Anzeige umschalten
_SILENT_PP = {"MoveFiles", "MoveFilesAfterDownload", "FFmpegMetadata", "EmbedThumbnail"}


def _page_tag(page_url: str) -> str:
    return hashlib.sha1(page_url.encode()).hexdigest()[:8]
from .state import State, Video

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SEPARATORS = re.compile(r"\s+[-|–—:•»]\s+")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Aborted(Exception):
    """Wird im Progress-Hook geworfen, um bei Strg+C sauber abzubrechen (.part bleibt)."""


def site_folder(url: str) -> str:
    host = urlparse(url).hostname or "unknown"
    return host[4:] if host.startswith("www.") else host


def clean_title(title: str | None, url: str, site_names: Iterable[str] = ()) -> str | None:
    """Seitentitel ohne angehängten Seitennamen ("Titel - FakeTube" -> "Titel")."""
    if not title:
        return None
    names = {site_folder(url).split(".")[0].lower()}
    names |= {n.lower().replace(" ", "") for n in site_names if n}

    def is_site(part: str) -> bool:
        p = part.lower().replace(" ", "")
        return any(n and n in p for n in names)

    parts = _SEPARATORS.split(title)
    while len(parts) > 1 and is_site(parts[-1]):
        parts.pop()
    while len(parts) > 1 and is_site(parts[0]):
        parts.pop(0)
    return " - ".join(parts).strip() or None


def safe_filename(name: str, limit: int = 150) -> str:
    name = _BAD_CHARS.sub(" ", name)
    name = " ".join(name.split())[:limit].rstrip(" .")
    return name or "video"


class Display:
    """Oben: Übersicht (Crawl + Videos x/y). Darunter: ein Balken pro laufender Datei."""

    def __init__(self) -> None:
        self.overview = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}", table_column=Column(width=12)),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TextColumn("{task.fields[info]}", style="dim"),
        )
        self.files = Progress(
            TextColumn("  {task.description}", style="cyan",
                       table_column=Column(width=52, no_wrap=True, overflow="ellipsis")),
            BarColumn(bar_width=30),
            TextColumn("{task.percentage:>5.1f}%"),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        )
        self.console = self.overview.console
        self.renderable = Group(self.overview, self.files)


class _QuietLogger:
    def __init__(self, verbose: bool, console) -> None:
        self.verbose, self.console = verbose, console

    def debug(self, msg: str) -> None:
        if self.verbose and not msg.startswith("[debug]"):
            self.console.print(f"[dim]{msg}[/]", markup=True, highlight=False)

    info = debug

    def warning(self, msg: str) -> None:
        if self.verbose:
            self.console.print(f"[yellow]{msg}[/]")

    def error(self, msg: str) -> None:
        if self.verbose:
            self.console.print(f"[red]{msg}[/]")


class Downloader:
    def __init__(self, state: State, out_dir: Path, display: Display, *, workers: int = 3,
                 fragments: int = 4, rate_limit: str | None = None, cookies: str | None = None,
                 cookies_from_browser: str | None = None, sort: str = "site",
                 verbose: bool = False, guard: NetGuard | None = None) -> None:
        self.state, self.out_dir, self.display, self.sort = state, out_dir, display, sort
        self.guard = guard
        self.verbose = verbose
        self.stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl")
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self.ok = self.failed = self._submitted = 0
        self.overall = display.overview.add_task("Videos", total=0, info="")
        self._closed = threading.Event()
        threading.Thread(target=self._heartbeat, daemon=True, name="claims").start()
        self._cookie_file = cookies
        self._tls = threading.local()  # eine HTTP-Session pro Download-Thread

        self.base_opts: dict = {
            # ohne ffmpeg kein Zusammenführen getrennter Video-/Audiospuren
            "format": "bv*+ba/b" if shutil.which("ffmpeg") else "b/bv*+ba",
            "merge_output_format": "mp4",
            "continuedl": True,                 # abgebrochene .part-Dateien fortsetzen
            "nooverwrites": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": fragments,
            "noplaylist": True,
            "windowsfilenames": True,
            "quiet": True,
            "no_warnings": not verbose,
            "noprogress": True,
            "color": {"stdout": "no_color", "stderr": "no_color"},
            "logger": _QuietLogger(verbose, display.console),
            # gleiche Video-ID (auch unter anderer Seiten-URL) nie doppelt laden
            "download_archive": str(out_dir / ".pronload-archive.txt"),
        }
        if rate_limit:
            self.base_opts["ratelimit"] = yt_dlp.parse_bytes(rate_limit)
        if cookies:
            self.base_opts["cookiefile"] = cookies
        if cookies_from_browser:
            self.base_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    # ------------------------------------------------------------------------
    def submit(self, video: Video) -> bool:
        with self._lock:
            if video.page_url in self._active:
                return False
            # lädt gerade ein anderer pronload-Prozess (anderer Tab) dasselbe Video? -> überspringen
            if not self.state.claim_video(video.page_url):
                return False
            self._active.add(video.page_url)
            self._submitted += 1
            self.display.overview.update(self.overall, total=self._submitted)
        self._pool.submit(self._run, video)
        return True

    def busy(self) -> bool:
        with self._lock:
            return bool(self._active)

    def wait(self, refill: Callable[[], int] | None = None, every: float = 60) -> None:
        """Warten, bis alles geladen ist. refill() holt regelmäßig neu frei gewordene/offene Videos
        (z.B. von einem beendeten anderen Prozess) und liefert die Zahl neu eingereihter."""
        last = time.monotonic()
        while True:
            # Polling statt join(), damit Strg+C unter Windows sofort ankommt
            while self.busy():
                time.sleep(0.2)
                if refill and time.monotonic() - last > every:
                    refill()
                    last = time.monotonic()
            if not refill or not refill():
                break
            last = time.monotonic()
        self._pool.shutdown(wait=True)
        self._close()

    def abort(self) -> None:
        self.stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._close()

    def _close(self) -> None:
        self._closed.set()
        self.state.release_all()  # übrige Reservierungen sofort für andere Tabs freigeben

    def _heartbeat(self) -> None:
        # Reservierungen regelmäßig erneuern; stirbt der Prozess, laufen sie nach CLAIM_TTL ab
        while not self._closed.wait(30):
            try:
                self.state.touch_claims()
            except Exception:  # noqa: BLE001 - DB kurz gesperrt: beim nächsten Mal
                pass

    # ------------------------------------------------------------------------
    def _outtmpl(self, v: Video) -> str:
        # stabiler Name -> derselbe .part-Pfad bei jedem Lauf -> Fortsetzen klappt
        # relativ zu paths.home (= Zielordner) - sonst ignoriert yt-dlp den eigenen Zwischenordner
        tag = _page_tag(v.page_url)
        name = clean_title(v.title, v.page_url)
        stem = safe_filename(name).replace("%", "%%") if name else "%(title).150B"
        folder = Path(site_folder(v.page_url))
        if self.sort in ("model", "category"):
            hint = v.model if self.sort == "model" else v.category
            # nichts im HTML gefunden -> Metadaten von yt-dlp, sonst "_unsortiert"
            sub = (safe_filename(hint, 80).replace("%", "%%") if hint else
                   "%(cast.0,uploader,channel|_unsortiert)s" if self.sort == "model" else
                   "%(categories.0,tags.0|_unsortiert)s")
            folder = folder / sub
        return str(folder / f"{stem} [{tag}].%(ext)s")

    def _run(self, v: Video) -> None:
        if self.stop.is_set():
            return
        page_url, media = v.page_url, v.media
        label = safe_filename(clean_title(v.title, page_url) or urlparse(page_url).path, 80)
        files = self.display.files
        task = files.add_task(label, total=None)
        outtmpl = self._outtmpl(v)
        errors: list[str] = []
        try:
            # 1) yt-dlp auf die Videoseite: kennt viele Seiten + eingebettete Player
            # 2) Fallback: Medien-URLs aus der frisch geladenen Seite (KVS-Links laufen ab),
            #    beste Qualität zuerst; gespeicherte URLs nur, wenn die Seite nicht lädt
            def attempts():
                yield page_url
                fresh = rank_media(self._fresh_media(page_url))  # erst bei Bedarf laden
                yield from fresh
                yield from (m for m in rank_media(media) if m not in fresh)  # beim Crawlen gespeicherte

            while True:
                offline = False
                for url in attempts():
                    if self.guard and not self.guard.wait(self.stop.is_set):
                        return  # abgebrochen während offline -> bleibt 'pending'
                    try:
                        file = self._download(url, page_url, outtmpl, task,
                                              clean_title(v.title, page_url), label)
                    except Aborted:
                        return  # bleibt 'pending', .part wird beim nächsten Lauf fortgesetzt
                    except Exception as e:  # noqa: BLE001 - yt-dlp wirft sehr unterschiedliche Fehler
                        if self.guard and self.guard.check(e):
                            offline = True  # Internet weg: zählt nicht als Fehlversuch
                            break
                        if self.verbose:
                            import traceback
                            self.display.console.print(traceback.format_exc(), markup=False, highlight=False)
                        msg = _ANSI.sub("", str(e)).strip()
                        msg = msg.splitlines()[0] if msg else type(e).__name__
                        errors.append(f"{url}: {msg.removeprefix('ERROR: ')}")
                        continue
                    self.state.video_done(page_url, file)
                    self._cleanup(page_url, file)
                    with self._lock:
                        self.ok += 1
                    self.display.console.print(f"[green]✓[/] {label}", highlight=False)
                    return
                if not offline:
                    break
                # warten, dann dasselbe Video nochmal von vorn (die .part-Datei wird weiterverwendet)
                files.update(task, description=f"⏸ {label}")
                if not self.guard.wait(self.stop.is_set):
                    return
                files.update(task, description=label)
                errors.clear()
            self.state.video_failed(page_url, " || ".join(errors) or "kein Video gefunden")
            with self._lock:
                self.failed += 1
            self.display.console.print(f"[red]✗[/] {label}", highlight=False)
        finally:
            files.remove_task(task)
            if not self.stop.is_set():
                self.display.overview.advance(self.overall)
                self.display.overview.update(self.overall, info=f"✓ {self.ok}  ✗ {self.failed}")
            with self._lock:
                self._active.discard(page_url)

    def _session(self) -> requests.Session:
        s = getattr(self._tls, "session", None)
        if s is None:
            s = self._tls.session = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            if self._cookie_file:
                from http.cookiejar import MozillaCookieJar
                jar = MozillaCookieJar(self._cookie_file)
                jar.load(ignore_discard=True, ignore_expires=True)
                s.cookies.update(jar)
        return s

    def _temp_dir(self, page_url: str, src_url: str) -> Path:
        """Zwischenordner für .part/.ytdl/Fragmente: stabil pro (Videoseite, Quelle), Token egal."""
        src = "page" if src_url == page_url else hashlib.sha1(
            urlparse(src_url)._replace(query="", fragment="").geturl().encode()).hexdigest()[:10]
        return self.out_dir / TMP_DIR / _page_tag(page_url) / src

    def _cleanup(self, page_url: str, final: str | None) -> None:
        """Nach Erfolg: Zwischenordner des Videos und Reste alter Versionen (.part neben der Datei) weg."""
        shutil.rmtree(self.out_dir / TMP_DIR / _page_tag(page_url), ignore_errors=True)
        if final:
            for leftover in Path(final).parent.glob(glob_escape(Path(final).name) + ".*"):
                if re.search(r"\.(part|ytdl)$|\.part-Frag\d+(\.part)?$", leftover.name):
                    leftover.unlink(missing_ok=True)

    def _watch_postprocessing(self, d: dict, task: TaskID, label: str, done: threading.Event) -> None:
        files = self.display.files
        info = d.get("info_dict") or {}
        src = info.get("filepath") or info.get("_filename") or ""
        inputs = [Path(p) for p in info.get("__files_to_merge") or [src] if p]
        total = sum(p.stat().st_size for p in inputs if p.exists()) or None
        name = {"FFmpegMerger": "Zusammenfügen"}.get(d.get("postprocessor"), "Umpacken")
        files.update(task, description=f"⚙ {name}: {label}", total=total, completed=0)
        if not src or not total:
            return
        stem = Path(src).with_suffix("")
        pattern = glob_escape(stem.name) + ".temp.*"

        def watch() -> None:
            while not done.wait(0.5):  # bis der Postprocessor fertig ist
                try:
                    size = sum(p.stat().st_size for p in stem.parent.glob(pattern))
                    files.update(task, completed=min(size, total))
                except (OSError, KeyError):  # Datei gerade umbenannt / Anzeige-Task schon weg
                    pass

        threading.Thread(target=watch, daemon=True, name="pp-watch").start()

    def _fresh_media(self, page_url: str) -> list[str]:
        try:
            r = self._session().get(page_url, timeout=25)
            r.raise_for_status()
            return analyze(page_url, r.text).media
        except Exception:  # noqa: BLE001 - dann gespeicherte URLs verwenden
            return []

    def _download(self, url: str, referer: str, outtmpl: str, task: TaskID,
                  title: str | None = None, label: str = "") -> str | None:
        files = self.display.files

        last_total = [None]
        pp_done = threading.Event()

        def hook(d: dict) -> None:
            if self.stop.is_set():
                raise Aborted()
            if d["status"] == "downloading":
                done = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if not total and d.get("fragment_index") and d.get("fragment_count"):
                    # Stream ohne Größenangabe: aus den bisher geladenen Teilen hochrechnen
                    total = done / d["fragment_index"] * d["fragment_count"]
                last_total[0] = total or last_total[0]
                files.update(task, total=total, completed=done)
            elif d["status"] == "finished":
                done = d.get("total_bytes") or d.get("downloaded_bytes") or last_total[0] or 0
                files.update(task, total=done, completed=done)

        def pp_hook(d: dict) -> None:
            # ffmpeg (Umpacken/Zusammenfügen) meldet keinen Fortschritt -> wachsende .temp-Datei beobachten
            if d["status"] == "started" and d.get("postprocessor") not in _SILENT_PP:
                pp_done.clear()
                self._watch_postprocessing(d, task, label, pp_done)
            elif d["status"] == "finished":
                pp_done.set()
                files.update(task, description=label)

        opts = dict(self.base_opts, outtmpl=outtmpl, progress_hooks=[hook], postprocessor_hooks=[pp_hook],
                    http_headers={"Referer": referer},
                    # Halbfertiges pro Video UND Quelle getrennt: .part einer anderen Qualität/Quelle
                    # wird nie "fortgesetzt" (sonst HTTP 416 oder ein still zusammengestückeltes Video)
                    paths={"home": str(self.out_dir), "temp": str(self._temp_dir(referer, url))})
        ext = path_ext(url.rstrip("/"))
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Cookies vom Seitenabruf mitgeben: Zugangs-Tokens in Links (z.B. v-acctoken)
            # gelten oft nur zusammen mit der Session, in der die Seite geladen wurde
            for cookie in self._session().cookies:
                ydl.cookiejar.set_cookie(cookie)
            if url != referer and (ext in DIRECT_EXT or "/get_file/" in url):
                # Direkte Datei: am Generic-Extractor vorbei (der scheitert z.B. an KVS' ".mp4/"),
                # yt-dlp lädt sie trotzdem mit Fortschritt + Fortsetzen
                vid = hashlib.sha1(referer.encode()).hexdigest()[:12]
                info = ydl.process_ie_result({
                    "id": vid, "title": title or vid, "webpage_url": referer,
                    "extractor": "pronload", "extractor_key": "Pronload",
                    "formats": [{"url": url, "ext": (ext or ".mp4")[1:], "format_id": "direct",
                                 "http_headers": {"Referer": referer, "User-Agent": USER_AGENT}}],
                }, download=True)
            else:
                info = ydl.extract_info(url, download=True)
        if info is None:
            return None  # steht schon im Archiv -> gilt als erledigt
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries") or [] if e]
            if not entries:
                raise RuntimeError("keine Videos in der Seite")
            info = entries[0]
        downloads = info.get("requested_downloads") or []
        return downloads[0].get("filepath") if downloads else info.get("filepath")
