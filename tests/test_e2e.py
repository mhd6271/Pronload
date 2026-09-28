"""End-to-End gegen eine lokale Fake-Tube-Seite: python tests/test_e2e.py"""
from __future__ import annotations

import functools
import os
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pronload.cli import main  # noqa: E402
from pronload.extract import analyze  # noqa: E402
from pronload.state import State  # noqa: E402

PORT = 8765
RANGE_REQUESTS: list[tuple[str, str]] = []
REQUESTS: list[str] = []
BASE = f"http://127.0.0.1:{PORT}"

NAV = """<nav><a href="/categories/amateur/">Amateur</a><a href="/categories/outdoor/">Outdoor</a>
<a href="/models/">Models</a><a href="/videos/">Videos</a><a href="/videos/?sort_by=rating">Top</a>
<a href="/de/">Deutsch</a><a href="/es/videos/">Español</a></nav>"""

INDEX = f"""<html><head><title>Startseite | FakeTube</title></head><body>{NAV}
<div class="thumb"><a href="/videos/erstes-video/">1</a><video src="/media/preview/1.mp4"></video></div>
<div class="thumb"><a href="/videos/zweites-video/">2</a><video src="/media/preview/2.mp4"></video></div>
<div class="thumb"><a href="/videos/drittes-video/">3</a><video src="/media/preview/3.mp4"></video></div>
<div class="thumb"><a href="/videos/viertes-video/">4</a><video src="/media/preview/4.mp4"></video></div>
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
    for i in range(1, 5):
        w(f"media/preview/{i}.mp4", os.urandom(1000))


def serve(root: Path) -> ThreadingHTTPServer:
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a) -> None:
            pass

        def do_GET(self) -> None:  # minimale Range-Unterstützung zum Testen von Fortsetzen
            REQUESTS.append(self.path)
            if self.path.startswith("/get_file/"):
                self.path = self.path.rstrip("/")  # KVS-Links enden auf ".mp4/"
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

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(Quiet, directory=str(root)))
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
            check(len(files) == 4, "genau 4 Videos geladen (keine Previews, keine Duplikate)")
            check(all(f.startswith("127.0.0.1/") for f in files), "Ordner pro Seite")
            check(sum("/Anna Muster/" in f for f in files) == 2, "2 Videos im Model-Ordner Anna Muster")
            check(any("/Erstes Video Test & Co [" in f for f in files), "Name aus Seitentitel")
            check(not any("FakeTube" in f for f in files), "Seitenname aus dem Titel entfernt")
            check(all((out / f).stat().st_size == 1_500_000 for f in files if "KVS" not in f),
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
            check(stats.get("videos.done") == 4, f"Status: 4 fertig ({stats})")
            check(stats.get("pages.error", 0) == 0, "404-Seiten gelten als erledigt, nicht als Fehler")

            # Abgebrochenen Download simulieren: halbe .part-Datei, Video wieder 'pending'
            target = next(out.rglob("Zweites Video*.mp4"))
            original = (site / "media/zweites-video_720p.mp4").read_bytes()
            target.unlink()
            Path(str(target) + ".part").write_bytes(original[:700_000])
            (out / ".pronload-archive.txt").write_text("")
            st = State(out / ".pronload.db")
            st._q("UPDATE videos SET status='pending' WHERE page_url LIKE '%zweites%'")
            RANGE_REQUESTS.clear()
            rc = main(["-o", str(out)])
            check(rc == 0, "Fortsetzungs-Lauf endet sauber")
            check(target.exists() and target.read_bytes() == original, "Datei nach Fortsetzen vollständig & korrekt")
            check(any(r == "bytes=700000-" for _, r in RANGE_REQUESTS),
                  f"nur der Rest wurde geladen (Range ab Byte 700000): {RANGE_REQUESTS}")
        finally:
            srv.shutdown()
    print("\nalles grün")


if __name__ == "__main__":
    run()
