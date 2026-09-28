"""Crawler: arbeitet die persistente Warteschlange ab und meldet gefundene Videoseiten."""
from __future__ import annotations

import gzip
import json
import re
import time
from typing import Callable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from .downloader import _SEPARATORS, clean_title
from .extract import USER_AGENT, PageInfo, analyze, lang_prefix, link_label, url_priority
from .net import NetGuard
from .state import State, Video

MAX_HTML = 8 * 1024 * 1024
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)


class _Offline(Exception):
    """Abruf gescheitert, weil das Internet weg ist."""


def bare_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class Crawler:
    def __init__(self, state: State, start_urls: list[str], *, max_depth: int | None = None,
                 max_pages: int | None = None, delay: float = 0.5, subdomains: bool = False,
                 include: str | None = None, exclude: str | None = None,
                 video_pattern: str | None = None, respect_robots: bool = True,
                 all_languages: bool = False, cookies: str | None = None,
                 guard: NetGuard | None = None) -> None:
        self.state = state
        self.guard = guard
        self.hosts = {bare_host(u) for u in start_urls}
        # Sprachversionen (/de/, /es/ …) überspringen - außer der Sprache der Start-URL(s)
        self.all_languages = all_languages
        self.langs = {lang_prefix(u) for u in start_urls}
        self.max_depth, self.max_pages, self.delay = max_depth, max_pages, delay
        self.subdomains = subdomains
        self.include = re.compile(include) if include else None
        self.exclude = re.compile(exclude) if exclude else None
        self.video_pattern = re.compile(video_pattern) if video_pattern else None
        self.respect_robots = respect_robots
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
        if cookies:
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar(cookies)
            jar.load(ignore_discard=True, ignore_expires=True)
            self.session.cookies.update(jar)
        self.nav: set[str] = set(json.loads(state.get_meta("nav_links") or "[]"))
        self.site_names: set[str] = set(json.loads(state.get_meta("site_names") or "[]"))
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0
        self.fetched = 0
        self.errors: list[str] = []
        state.reprioritize(self.priority)

    def priority(self, url: str) -> int:
        return url_priority(url, self.video_pattern)

    # --- Scope ---------------------------------------------------------------
    def in_scope(self, url: str) -> bool:
        host = bare_host(url)
        if host not in self.hosts and not (
                self.subdomains and any(host.endswith("." + h) for h in self.hosts)):
            return False
        if not self.all_languages and lang_prefix(url) not in self.langs:
            return False
        if self.exclude and self.exclude.search(url):
            return False
        if self.include and not self.include.search(url):
            # Videoseiten dürfen auch außerhalb von --include liegen
            return bool(self.video_pattern and self.video_pattern.search(url))
        return True

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if origin not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.session.get(origin + "/robots.txt", timeout=15)
                rp.parse(r.text.splitlines() if r.ok else [])
            except requests.RequestException:
                # nicht merken: war es ein Netzausfall, wird robots.txt beim nächsten Mal richtig gelesen
                return True
            self._robots[origin] = rp
        rp = self._robots[origin]
        return rp.can_fetch(USER_AGENT, url) if rp else True

    def to_video(self, info: PageInfo) -> Video:
        cats = [link_label(c) for c in info.categories if c not in self.nav]
        models = [link_label(m) for m in info.models if m not in self.nav]
        # Canonical-URL als Schlüssel: dieselbe Videoseite unter mehreren URLs nur einmal
        url = info.canonical if info.canonical and bare_host(info.canonical) == bare_host(info.url) else info.url
        title = clean_title(info.title, url, self.site_names | {info.site_name or ""})
        return Video(url, title, info.media,
                     next(filter(None, cats), None), next(filter(None, models), None))

    def learn_site(self, info: PageInfo) -> None:
        """Seitenname (og:site_name bzw. Titel-Suffix der Startseite) + Navigations-Links merken."""
        self.nav.update(info.categories + info.models)
        if info.site_name:
            self.site_names.add(info.site_name)
        if info.title:
            parts = _SEPARATORS.split(info.title)
            if len(parts) > 1:
                self.site_names.add(parts[-1])
        self.state.set_meta("nav_links", json.dumps(sorted(self.nav)))
        self.state.set_meta("site_names", json.dumps(sorted(self.site_names)))

    # --- HTTP ----------------------------------------------------------------
    def _get(self, url: str) -> requests.Response:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            return self.session.get(url, timeout=25, stream=True)
        finally:
            self._last_request = time.monotonic()

    def fetch_html(self, url: str) -> str | None:
        r = self._get(url)
        try:
            r.raise_for_status()
            if "html" not in r.headers.get("Content-Type", "html").lower():
                return None
            body = r.raw.read(MAX_HTML, decode_content=True)
            return body.decode(r.encoding or r.apparent_encoding or "utf-8", errors="replace")
        finally:
            r.close()

    # --- Sitemaps ------------------------------------------------------------
    def seed_sitemaps(self, start_url: str, limit: int = 200_000) -> int:
        """Alle URLs aus sitemap.xml (+ robots.txt-Sitemaps) einreihen - viel schneller als Klicken."""
        origin = "{0.scheme}://{0.netloc}".format(urlparse(start_url))
        todo = [origin + "/sitemap.xml"]
        try:
            robots = self.session.get(origin + "/robots.txt", timeout=15)
            if robots.ok:
                todo += [line.split(":", 1)[1].strip() for line in robots.text.splitlines()
                         if line.lower().startswith("sitemap:")]
        except requests.RequestException:
            pass
        seen, added = set(), 0
        while todo and added < limit:
            sm = todo.pop()
            if sm in seen:
                continue
            seen.add(sm)
            try:
                r = self._get(sm)
                data = r.content
                r.close()
                if not r.ok:
                    continue
                if data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
            except (requests.RequestException, OSError):
                continue
            text = data.decode("utf-8", errors="replace")
            for loc in _LOC_RE.findall(text):
                loc = loc.replace("&amp;", "&")
                if "<sitemapindex" in text[:500].lower() or loc.lower().split("?")[0].endswith((".xml", ".xml.gz")):
                    todo.append(loc)
                elif self.in_scope(loc) and self.state.enqueue(loc, 1, self.priority(loc)):
                    added += 1
        return added

    # --- Hauptschleife -------------------------------------------------------
    def run(self, on_video: Callable[[Video], None], on_tick: Callable[[str], None],
            should_stop: Callable[[], bool], focus: set[str] | None = None) -> None:
        """focus: nur Seiten dieser Hosts abarbeiten (andere bleiben für spätere Läufe liegen)."""
        while not should_stop():
            if self.max_pages is not None and self.fetched >= self.max_pages:
                break
            nxt = self.state.next_page(focus)  # reserviert die Seite gleich (parallele Tabs)
            if not nxt:
                break
            url, depth = nxt
            on_tick(url)
            try:
                self._process(url, depth, on_video)
            except _Offline:
                # Internet weg: Seite zurück, warten bis es wieder geht (zählt nicht als Fehler)
                self.state.unfetch(url)
                if not self.guard.wait(should_stop):
                    break
            except Exception as e:  # noqa: BLE001 - eine kaputte Seite darf den Lauf nicht beenden
                self.errors.append(f"{url}: {type(e).__name__}: {e}")
                self.state.finish_page(url, "error")  # beim nächsten Lauf nochmal
            except BaseException:
                self.state.unfetch(url)  # Strg+C mitten im Abruf: Seite zurück in die Warteschlange
                raise

    def _process(self, url: str, depth: int, on_video: Callable[[Video], None]) -> None:
        if depth > 0 and not self.in_scope(url):
            # stand schon in der Warteschlange, passt aber nicht (mehr) zu den Filtern
            self.state.finish_page(url, "skipped")
            return
        if not self.allowed(url):
            self.state.finish_page(url, "done")
            return
        try:
            text = self.fetch_html(url)
        except requests.HTTPError as e:
            # 4xx = Seite gibt's nicht (mehr) -> erledigt; 5xx beim nächsten Lauf nochmal
            code = e.response.status_code if e.response is not None else 500
            self.state.finish_page(url, "done" if 400 <= code < 500 and code != 429 else "error")
            return
        except requests.RequestException as e:
            if self.guard and self.guard.check(e):
                raise _Offline() from e
            self.state.finish_page(url, "error")
            return
        self.fetched += 1
        if text is None:
            self.state.finish_page(url, "done")
            return

        info = analyze(url, text)
        is_video = bool(self.video_pattern.search(url)) if self.video_pattern else info.is_video_page
        if is_video:
            on_video(self.to_video(info))
        elif depth == 0:
            # Kategorie-/Model-Links der Startseite = Navigation, gilt für jede Seite
            self.learn_site(info)
        if self.max_depth is None or depth < self.max_depth:
            # alle Links einer Seite in einer Transaktion (statt ein Commit pro Link)
            self.state.enqueue_many((link, depth + 1, self.priority(link))
                                    for link in info.links if self.in_scope(link))
        self.state.finish_page(url, "done", is_video)
