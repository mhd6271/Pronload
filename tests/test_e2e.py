"""End-to-End gegen eine lokale Fake-Tube-Seite: python tests/test_e2e.py"""
from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pronload.cli import main  # noqa: E402
from pronload.extract import analyze  # noqa: E402
from pronload.state import State  # noqa: E402

PORT = 8765
RANGE_REQUESTS: list[tuple[str, str]] = []
REQUESTS: list[str] = []
HOSTS: list[str] = []
BASE = f"http://127.0.0.1:{PORT}"

NAV = """<nav><a href="/categories/amateur/">Amateur</a><a href="/categories/outdoor/">Outdoor</a>
<a href="/models/">Models</a><a href="/videos/">Videos</a><a href="/videos/?sort_by=rating">Top</a>
<a href="/de/">Deutsch</a><a href="/es/videos/">Español</a></nav>"""

INDEX = f"""<html><head><title>Startseite | FakeTube</title></head><body>{NAV}
<div class="thumb"><a href="/videos/erstes-video/">1</a><video src="/media/preview/1.mp4"></video></div>
<div class="thumb"><a href="/videos/zweites-video/">2</a><video src="/media/preview/2.mp4"></video></div>
<div class="thumb"><a href="/videos/drittes-video/">3</a><video src="/media/preview/3.mp4"></video></div>
<div class="thumb"><a href="/videos/viertes-video/">4</a><video src="/media/preview/4.mp4"></video></div>
<div class="thumb"><a href="/videos/fuenftes-video/">5</a></div>
<div class="thumb"><a href="/videos/sechstes-video/">6</a></div>
<a href="/page/2/">weiter</a></body></html>"""

PAGE2 = f"""<html><head><title>Seite 2 | FakeTube</title></head><body>{NAV}
<a href="/videos/erstes-video/">1 (doppelt verlinkt)</a><a href="/videos/zweites-video/?ref=p2">2</a>
</body></html>"""


def video_page(slug: str, title: str, model: str, cat: str) -> str:
    return f"""<html><head><title>{title} - FakeTube</title>
<meta property="og:title" content="{title} - FakeTube"></head><body>{NAV}
<a href="/models/{model}/">{model}</a> <a href="/categories/{cat}/">{cat}</a>
<video controls><source src="/media/{slug}_480p.mp4" type="video/mp4">
<source src="/media/{slug}_720p.mp4" type="video/mp4"></video>
<a href="/videos/erstes-video/">Ähnlich</a></body></html>"""


KVS_LICENSE = "$518920714361235"
KVS_HASH = "0123456789abcdef0123456789abcdef"[::-1]


def kvs_scramble(real: str, license_code: str) -> str:
    """Umkehrung von yt-dlps _kvs_get_real_url (so verwürfelt eine KVS-Seite ihre Links)."""
    from yt_dlp.extractor.generic import GenericIE
    token = GenericIE._kvs_get_license_token(license_code)
    idx = list(range(32))
    accum = 0
    for src in reversed(range(32)):
        accum += token[src]
        dest = (src + accum) % 32
        idx[src], idx[dest] = idx[dest], idx[src]
    out = [""] * 32
    for i, j in enumerate(idx):
        out[j] = real[i]
    return "".join(out)


def kvs_page() -> str:
    """Wie echte KVS-Seiten: Variable heißt nicht 'flashvars' -> yt-dlp scheitert, wir nicht."""
    s = kvs_scramble(KVS_HASH, KVS_LICENSE)
    u = lambda q: f"function/0/{BASE}/get_file/1/{s}/1000/1005/1005_{q}.mp4/"  # noqa: E731
    return f"""<html><head><title>Viertes Video (KVS) | FakeTube</title></head><body>{NAV}
<a href="/models/clara-kvs/">Clara</a>
<script src="/player/kt_player.js"></script>
<script>var flashvars_1005 = {{ video_id: '1005', license_code: '{KVS_LICENSE}',
 video_url: '{u("480p")}', video_url_text: '480p',
 video_alt_url: '{u("1080p")}', video_alt_url_text: '1080p' }};
kt_player('kt_player', '/player/kt_player.swf', '100%', '100%', flashvars_1005);</script>
</body></html>"""


def kvs_session_page() -> str:
    """Link funktioniert nur mit dem Cookie, das die Seite setzt (wie KVS mit v-acctoken)."""
    s = kvs_scramble(KVS_HASH, KVS_LICENSE)
    return f"""<html><head><title>Fuenftes Video (Session) | FakeTube</title></head><body>{NAV}
<script>var flashvars_2006 = {{ license_code: '{KVS_LICENSE}',
 video_url: 'function/0/{BASE}/get_file/2/{s}/2000/2006/2006_720p.mp4/?v-acctoken=abc' }};</script>
</body></html>"""


VIDEOS = [
    ("erstes-video", "Erstes Video: Test & Co", "anna-muster", "amateur"),
    ("zweites-video", "Zweites Video", "berta-beispiel", "outdoor"),
    ("drittes-video", "Drittes Video", "anna-muster", "outdoor"),
]


def build_site(root: Path) -> None:
    def w(rel: str, content: str | bytes) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())

    w("index.html", INDEX)
    w("page/2/index.html", PAGE2)
    for slug, title, model, cat in VIDEOS:
        w(f"videos/{slug}/index.html", video_page(slug, title, model, cat))
        w(f"media/{slug}_480p.mp4", os.urandom(200_000))
        w(f"media/{slug}_720p.mp4", os.urandom(1_500_000))
    # viertes Video: KVS-Player mit verwürfelten Links; Datei liegt unter dem ECHTEN Hash
    w("videos/viertes-video/index.html", kvs_page())
    w(f"get_file/1/{KVS_HASH}/1000/1005/1005_480p.mp4", os.urandom(300_000))
    w(f"get_file/1/{KVS_HASH}/1000/1005/1005_1080p.mp4", os.urandom(2_000_000))
    w("videos/fuenftes-video/index.html", kvs_session_page())
    # sechstes Video: echter HLS-Stream in vielen Teilen (braucht ffmpeg zum Erzeugen)
    (root / "hls").mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=12:size=320x240:rate=25", "-c:v", "libx264", "-g", "25",
                    "-f", "hls", "-hls_time", "1", "-hls_list_size", "0", str(root / "hls" / "s.m3u8")],
                   check=True)
    w("videos/sechstes-video/index.html",
      f"<html><head><title>Sechstes Video (HLS) | FakeTube</title></head><body>{NAV}"
      '<video controls><source src="/hls/s.m3u8" type="application/x-mpegURL"></video></body></html>')
    w(f"get_file/2/{KVS_HASH}/2000/2006/2006_720p.mp4", os.urandom(400_000))
    for i in range(1, 5):
        w(f"media/preview/{i}.mp4", os.urandom(1000))


NET = {"down": False, "media_down": False}


def serve(root: Path, port: int = PORT) -> ThreadingHTTPServer:
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a) -> None:
            pass

        def end_headers(self) -> None:
            if self.path.startswith("/videos/fuenftes-video"):
                self.send_header("Set-Cookie", "kt_session=ok; Path=/")
            super().end_headers()

        def do_GET(self) -> None:  # minimale Range-Unterstützung zum Testen von Fortsetzen
            if NET["media_down"] and self.path.startswith("/media/"):
                self.close_connection = True  # Verbindung ohne Antwort kappen = Netz weg
                return
            REQUESTS.append(self.path)
            HOSTS.append(self.headers.get("Host", "").split(":")[0])
            if self.path.startswith("/get_file/2/") and "kt_session=ok" not in self.headers.get("Cookie", ""):
                self.send_error(403)  # ohne Session-Cookie der Videoseite kein Zugriff
                return
            if self.path.startswith("/get_file/"):
                self.path = self.path.split("?")[0].rstrip("/")  # KVS-Links enden auf ".mp4/"
            rng = self.headers.get("Range")
            path = Path(self.translate_path(self.path))
            if not rng or not path.is_file():
                return super().do_GET()
            RANGE_REQUESTS.append((self.path, rng))
            data = path.read_bytes()
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.send_header("Content-Length", str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])

    srv = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=str(root)))
    srv.handle_error = lambda *a: None  # abgebrochene Verbindungen nicht loggen
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def test_detection() -> None:
    idx = analyze(BASE + "/", INDEX)
    check(not idx.is_video_page, "Übersicht mit Hover-Previews ist keine Videoseite")
    vp = analyze(BASE + "/videos/x/", video_page("x", "T", "m", "c"))
    check(vp.is_video_page, "Videoseite erkannt")
    check(vp.title == "T - FakeTube", "Titel gelesen")


def test_parallel_claims() -> None:
    """Zwei Prozesse (= zwei Tabs) auf derselben Datenbank dürfen sich nicht in die Quere kommen."""
    from pronload.state import CLAIM_TTL, Video
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / ".pronload.db"
        a, b = State(db), State(db)
        for i in range(3):
            a.enqueue(f"https://x.test/videos/v{i}/", 1)
        pages = {a.next_page()[0], b.next_page()[0], a.next_page()[0]}
        check(len(pages) == 3 and a.next_page() is None and b.next_page() is None,
              "Seiten werden atomar reserviert – kein Tab holt dieselbe Seite")

        a.add_video(Video("https://x.test/videos/v0/"))
        check(a.claim_video("https://x.test/videos/v0/"), "Tab A reserviert ein Video")
        check(not b.claim_video("https://x.test/videos/v0/"), "Tab B bekommt dasselbe Video nicht")
        check(b.pending_videos(3) == [], "Tab B sieht es auch nicht als offen")
        a._q("UPDATE videos SET claimed_at = claimed_at - ?", (CLAIM_TTL + 1,))
        check(b.claim_video("https://x.test/videos/v0/"), "abgelaufene Reservierung (Absturz) wird übernommen")
        b.release_all()
        check(a.claim_video("https://x.test/videos/v0/"), "nach Freigabe wieder verfügbar")

        a._q("UPDATE pages SET claimed_at = claimed_at - 400 WHERE status='fetching'")
        b.release_stale()
        check(a.queue_sizes()[1] == 3, "hängengebliebene Seiten-Reservierungen werden freigegeben")

        # Prozess wurde hart beendet (Taskmanager/Kill): Reservierung ist frisch, Prozess aber tot
        from pronload.state import HOST
        for dead_owner in (f"{HOST}:4000000:abcdef", "4000001-deadbeef"):  # neues + altes Format
            a._q("UPDATE videos SET claimed_by=?, claimed_at=? WHERE page_url LIKE '%v0/'",
                 (dead_owner, time.time()))
            check(not b.claim_video("https://x.test/videos/v0/"), f"frische Reservierung blockiert ({dead_owner})")
            b.release_stale()
            check(b.claim_video("https://x.test/videos/v0/"), f"… aber toter Prozess wird sofort erkannt ({dead_owner})")
            b.release_all()
        a._q("UPDATE videos SET claimed_by=?, claimed_at=? WHERE page_url LIKE '%v0/'",
             (f"anderer-rechner:{os.getpid()}:abcdef", time.time()))
        b.release_stale()
        check(not b.claim_video("https://x.test/videos/v0/"),
              "Reservierung eines anderen Rechners wird nicht angefasst (nur Zeitablauf)")

        # Fremder Prozess hält eine Schreibsperre länger als SQLites eigenes Warten -> wiederholen
        import sqlite3
        import pronload.state as st_mod
        blocker = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        threading.Timer(1.5, lambda: blocker.execute("COMMIT")).start()
        c = State(db)
        c._db.execute("PRAGMA busy_timeout = 100")  # SQLite gibt nach 0,1 s auf ...
        n = c.enqueue_many((f"https://x.test/viele/{i}/", 2, 1) for i in range(500))
        check(n == 500, "... pronload wiederholt trotzdem, bis die Sperre weg ist (500 Links in einer Transaktion)")
        check(st_mod.LOCK_PATIENCE >= 60, "Geduld bei Sperren reicht für parallele Terminals")


def test_offline(site: Path, tmp: Path) -> None:
    """Internet fällt aus: pausieren, später weitermachen, nichts zählt als Fehlversuch."""
    import pronload.net as net
    net.BACKOFF = [0.3]
    net.probe_internet = lambda hosts=(): not (NET["down"] or NET["media_down"])

    # 1) Beim Start kein Netz (Server nicht erreichbar), nach 1,5 s wieder da -> Crawler wartet
    port = 8766
    out = tmp / "offline1"
    NET["down"] = True
    servers: list[ThreadingHTTPServer] = []

    def back_online() -> None:
        servers.append(serve(site, port))
        NET["down"] = False

    threading.Timer(1.5, back_online).start()
    t0 = time.monotonic()
    rc = main([f"http://127.0.0.1:{port}/", "-o", str(out), "--delay", "0"])
    servers[0].shutdown()
    stats = State(out / ".pronload.db").stats()
    check(rc == 0 and time.monotonic() - t0 >= 1.5, "Crawler pausiert ohne Netz und macht danach weiter")
    check(stats.get("pages.error", 0) == 0 and stats.get("videos.done") == 6 and "videos.failed" not in stats,
          f"Ausfall beim Crawlen zählt nicht als Fehler, alles geladen ({stats})")

    # 2) Netz bricht während der Downloads weg -> Downloads warten, danach fertig
    out = tmp / "offline2"
    NET["media_down"] = True
    threading.Timer(1.5, lambda: NET.update(media_down=False)).start()
    rc = main([BASE + "/", "-o", str(out), "--delay", "0"])
    stats = State(out / ".pronload.db").stats()
    check(rc == 0 and stats.get("videos.done") == 6 and "videos.failed" not in stats,
          f"Ausfall beim Download: gewartet statt fehlgeschlagen ({stats})")

    # 3) Erreichbares Internet, aber die Seite selbst antwortet nicht -> normaler Fehler, keine Pause
    check(not net.NetGuard(()).check(RuntimeError("HTTP Error 404: Not Found")),
          "HTTP-Fehler der Seite ist kein Netzausfall")


def run() -> None:
    test_detection()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        site, out = Path(tmp) / "site", Path(tmp) / "out"
        build_site(site)
        srv = serve(site)
        try:
            rc = main([BASE + "/", "-o", str(out), "--sort", "model", "--delay", "0"]
                      + (["-v"] if os.environ.get("PRONLOAD_DEBUG") else []))
            check(rc == 0, "erster Lauf endet sauber")
            check(not any(r.startswith(("/de/", "/es/")) for r in REQUESTS),
                  "Sprachversionen (/de/, /es/) übersprungen")
            pages = [r for r in REQUESTS if not r.startswith(("/media/", "/get_file/", "/robots"))]
            first_listing = min(pages.index(p) for p in ("/page/2/", "/videos/?sort_by=rating", "/models/"))
            check(pages.index("/videos/erstes-video/") < first_listing,
                  f"Videoseiten vor Übersichten gecrawlt ({pages[:8]})")
            check(pages.index("/page/2/") < pages.index("/videos/?sort_by=rating"),
                  "Sortiervarianten zuletzt")
            files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.mp4"))
            print("     " + "\n     ".join(files))
            check(len(files) == 6, "genau 6 Videos geladen (keine Previews, keine Duplikate)")
            check(any("Fuenftes Video (Session)" in f for f in files),
                  "Link mit Zugangs-Token: Session-Cookie der Seite wird an yt-dlp weitergegeben")
            check(all(f.startswith("127.0.0.1/") for f in files), "Ordner pro Seite")
            check(sum("/Anna Muster/" in f for f in files) == 2, "2 Videos im Model-Ordner Anna Muster")
            check(any("/Erstes Video Test & Co [" in f for f in files), "Name aus Seitentitel")
            check(not any("FakeTube" in f for f in files), "Seitenname aus dem Titel entfernt")
            check(all((out / f).stat().st_size == 1_500_000 for f in files if "(" not in f),
                  "jeweils beste Qualität (720p)")
            kvs = [f for f in files if "Viertes Video (KVS)" in f]
            for url, _, err in State(out / ".pronload.db").failed():
                print(f"     fehlgeschlagen: {url}\n       " + (err or "").replace(" || ", "\n       "))
            check(not State(out / ".pronload.db").failed(), "erster Lauf ohne Fehlschläge")
            check(len(kvs) == 1 and "/Clara Kvs/" in kvs[0] and (out / kvs[0]).stat().st_size == 2_000_000,
                  "KVS: Link entschlüsselt, 1080p geladen")

            # Tiefe begrenzen, dann wieder aufheben: gespeicherte Einstellung muss sich ändern lassen
            main(["-o", str(out), "--depth", "1", "--max-pages", "0"])
            main(["-o", str(out), "--depth", "0", "--max-pages", "0"])
            import json
            depth = json.loads(State(out / ".pronload.db").get_meta("settings"))["depth"]
            check(depth is None, "--depth 0 hebt gespeichertes Tiefenlimit auf")

            mtimes = {f: (out / f).stat().st_mtime_ns for f in files}
            rc = main(["-o", str(out), "--refresh"])  # Fortsetzen ohne URL, Übersichten neu crawlen
            check(rc == 0, "zweiter Lauf (ohne URL) endet sauber")
            files2 = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.mp4"))
            check(files2 == files and all((out / f).stat().st_mtime_ns == mtimes[f] for f in files),
                  "zweiter Lauf lädt nichts doppelt")
            stats = State(out / ".pronload.db").stats()
            check(stats.get("videos.done") == 6, f"Status: 6 fertig ({stats})")
            check(stats.get("pages.error", 0) == 0, "404-Seiten gelten als erledigt, nicht als Fehler")

            # Abgebrochenen Download simulieren: halbe .part-Datei, Video wieder 'pending'
            from pronload.downloader import TMP_DIR, _page_tag
            check(not (out / TMP_DIR).exists() or not any((out / TMP_DIR).iterdir()),
                  "keine Zwischendateien übrig nach erfolgreichen Downloads")
            target = next(out.rglob("Zweites Video*.mp4"))
            original = (site / "media/zweites-video_720p.mp4").read_bytes()
            target.unlink()
            # .part liegt jetzt im Zwischenordner pro (Video, Quelle) - hier: yt-dlp über die Videoseite
            page_url = BASE + "/videos/zweites-video/"
            part = out / TMP_DIR / _page_tag(page_url) / "page" / (str(target.relative_to(out)) + ".part")
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(original[:700_000])
            # Rest einer ALTEN Version neben der Zieldatei, aus einer anderen Quelle: darf nicht verwendet werden
            stale = Path(str(target) + ".part")
            stale.write_bytes(os.urandom(900_000))
            (out / ".pronload-archive.txt").write_text("")
            st = State(out / ".pronload.db")
            st._q("UPDATE videos SET status='pending' WHERE page_url LIKE '%zweites%'")
            RANGE_REQUESTS.clear()
            rc = main(["-o", str(out)])
            check(rc == 0, "Fortsetzungs-Lauf endet sauber")
            check(target.exists() and target.read_bytes() == original, "Datei nach Fortsetzen vollständig & korrekt")
            check(any(r == "bytes=700000-" for _, r in RANGE_REQUESTS),
                  f"nur der Rest wurde geladen (Range ab Byte 700000): {RANGE_REQUESTS}")
            check(not stale.exists(), "fremde alte .part-Datei nicht verwendet und aufgeräumt")

            # HLS-Stream (wie xhamster): Teile laden, ffmpeg packt um, keine Reste
            hls = [p for p in out.rglob("*.mp4") if "Sechstes Video (HLS)" in p.name]
            check(len(hls) == 1 and hls[0].stat().st_size > 50_000, "HLS-Video geladen und von ffmpeg umgepackt")
            check(not list(out.rglob("*.part-Frag*")) and not list(out.rglob("*.temp.*")),
                  "keine .part-Frag- oder .temp-Reste im Archiv")

            # Fokus: zweite Seite (localhost) im selben Ordner, während für 127.0.0.1 noch was offen ist
            st = State(out / ".pronload.db")
            st.enqueue(BASE + "/page/2/?liegen=1", 1)
            HOSTS.clear()
            rc = main(["http://localhost:8765/", "-o", str(out), "--dry-run", "--delay", "0"])
            check(rc == 0, "Lauf mit zweiter Seite endet sauber")
            check(HOSTS and set(HOSTS) == {"localhost"}, f"Fokus: nur die angegebene Seite angefragt ({set(HOSTS)})")
            check(st._q("SELECT status FROM pages WHERE url LIKE '%liegen=1'") == [("queued",)],
                  "offene Seite der anderen Domain bleibt liegen")
            settings = json.loads(st.get_meta("settings"))
            check(len(settings["start_urls"]) == 2, "beide Seiten bleiben im Ordner gespeichert")

            test_offline(site, Path(tmp))
        finally:
            srv.shutdown()
    test_parallel_claims()
    print("\nalles grün")


if __name__ == "__main__":
    run()
