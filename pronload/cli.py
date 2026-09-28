"""Kommandozeile. Jeder Aufruf setzt beim gespeicherten Stand im Zielordner fort."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .crawler import Crawler, bare_host
from .net import NetGuard
from .downloader import Display, Downloader
from .state import State, Video

MAX_ATTEMPTS = 3
# Einstellungen, die pro Zielordner gespeichert werden und beim Fortsetzen gelten
CRAWL_KEYS = ("depth", "delay", "subdomains", "include", "exclude",
              "video_pattern", "ignore_robots", "all_languages", "sort")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pronload",
        description="Crawlt Webseiten und archiviert alle eingebetteten Videos. "
                    "Ohne URL: setzt den letzten Lauf im Zielordner fort. "
                    "Abspielen: pronload play [-o ORDNER]",
    )
    p.add_argument("urls", nargs="*", help="Start-URL(s) zum Crawlen")
    p.add_argument("-o", "--output", type=Path, default=Path.cwd(),
                   help="Zielordner (Standard: aktueller Ordner)")

    g = p.add_argument_group("Crawl")
    g.add_argument("--depth", type=int, help="max. Linktiefe ab Start-URL (Standard/0: unbegrenzt)")
    g.add_argument("--max-pages", type=int, help="max. Seitenabrufe in diesem Lauf")
    g.add_argument("--delay", type=float, help="Pause zwischen Seitenabrufen in s (Standard 0.5)")
    g.add_argument("--subdomains", action="store_true", default=None, help="Subdomains mitcrawlen")
    g.add_argument("--include", metavar="REGEX", help="nur URLs folgen, die passen")
    g.add_argument("--exclude", metavar="REGEX", help="URLs ignorieren, die passen")
    g.add_argument("--video-pattern", metavar="REGEX",
                   help="URL-Muster für Videoseiten (z.B. '/videos/[^/]+/$'), ersetzt die Automatik")
    g.add_argument("--sitemap", action="store_true", help="sitemap.xml auslesen (schnellster Weg)")
    g.add_argument("--ignore-robots", action="store_true", default=None, help="robots.txt ignorieren")
    g.add_argument("--all-languages", action="store_true", default=None,
                   help="auch Sprachversionen (/de/, /es/ …) crawlen; Standard: nur die Sprache der Start-URL")
    g.add_argument("--no-crawl", action="store_true", help="URLs direkt als Videoseiten laden, nichts crawlen")
    g.add_argument("--refresh", action="store_true", help="Übersichtsseiten neu crawlen (neue Uploads finden)")
    g.add_argument("--dry-run", action="store_true", help="nur crawlen und Videos erfassen, nicht laden")

    d = p.add_argument_group("Download")
    d.add_argument("-w", "--workers", type=int, default=3, help="parallele Downloads (Standard 3)")
    d.add_argument("--fragments", type=int, default=4, help="parallele HLS/DASH-Fragmente pro Video")
    d.add_argument("--rate-limit", metavar="RATE", help="max. Bandbreite pro Download, z.B. 5M")
    d.add_argument("--sort", choices=("site", "model", "category"),
                   help="Unterordner: nur Seite (Standard), pro Model/Creator oder pro Kategorie")
    d.add_argument("--cookies", metavar="FILE", help="cookies.txt (Netscape-Format)")
    d.add_argument("--cookies-from-browser", metavar="BROWSER", help="z.B. firefox, chrome, edge")
    d.add_argument("--retry-failed", action="store_true", help="fehlgeschlagene Videos erneut versuchen")

    p.add_argument("--status", action="store_true", help="Stand anzeigen und beenden")
    p.add_argument("-v", "--verbose", action="store_true", help="yt-dlp-Meldungen anzeigen")
    return p


DEFAULTS = {"depth": None, "delay": 0.5, "subdomains": False, "include": None,
            "exclude": None, "video_pattern": None, "ignore_robots": False, "all_languages": False,
            "sort": "site"}


def resolve_settings(args: argparse.Namespace, state: State) -> dict:
    """Gespeicherte Einstellungen laden, explizit angegebene Optionen überschreiben sie."""
    saved = json.loads(state.get_meta("settings") or "{}")
    settings = {**DEFAULTS, **saved}
    for key in CRAWL_KEYS:
        value = getattr(args, key)
        if value is not None:
            settings[key] = value
    if settings["depth"] is not None and settings["depth"] <= 0:
        settings["depth"] = None  # --depth 0 hebt ein gespeichertes Limit wieder auf
    old, new = saved.get("depth"), settings["depth"]
    # Limit angehoben -> Seiten an der alten Grenze müssen ihre Links jetzt doch einreihen
    settings["_depth_raised"] = old is not None and (new is None or new > old)
    if args.urls and not args.no_crawl:
        settings["start_urls"] = sorted(set(saved.get("start_urls", [])) | set(args.urls))
    state.set_meta("settings", json.dumps({k: v for k, v in settings.items() if not k.startswith("_")}))
    return settings


def show_status(state: State, console: Console) -> None:
    s = state.stats()
    t = Table(title="pronload Stand", show_header=False)
    t.add_row("Seiten offen", str(s.get("pages.queued", 0)))
    t.add_row("Seiten erledigt", str(s.get("pages.done", 0)))
    t.add_row("Seiten Fehler", str(s.get("pages.error", 0)))
    t.add_row("Videos offen", str(s.get("videos.pending", 0)))
    t.add_row("Videos fertig", f"[green]{s.get('videos.done', 0)}[/]")
    t.add_row("Videos fehlgeschlagen", f"[red]{s.get('videos.failed', 0)}[/]")
    console.print(t)
    for url, attempts, err in state.failed(15):
        console.print(f"[red]✗[/] {url} [dim]({attempts}x) {(err or '')[:160]}[/]", highlight=False)


def main_play(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="pronload play",
                                description="Archiv durchstöbern und in VLC abspielen")
    p.add_argument("folder", nargs="?", type=Path, help="Archivordner (Standard: -o bzw. aktueller Ordner)")
    p.add_argument("-o", "--output", type=Path, default=Path.cwd(), help="Archivordner")
    args = p.parse_args(argv)
    from .play import run
    return run((args.folder or args.output).expanduser().resolve())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["play"]:
        return main_play(argv[1:])
    args = build_parser().parse_args(argv)
    console = Console()
    out: Path = args.output.expanduser().resolve()
    state = State(out / ".pronload.db")

    if args.status:
        show_status(state, console)
        return 0

    settings = resolve_settings(args, state)
    start_urls = settings.get("start_urls", [])
    if not start_urls and not args.urls and not state.pending_videos(MAX_ATTEMPTS):
        console.print("[yellow]Kein gespeicherter Lauf in[/] " + str(out) +
                      "\n→ Start mit: [bold]pronload https://seite.tld/[/]")
        return 1

    display = Display()
    console = display.console
    # Internet-Ausfall: alles pausieren, in wachsenden Abständen neu prüfen (Zeile nur bei Ausfall sichtbar)
    net_task = display.overview.add_task("Netz", total=None, info="", visible=False)
    guard = NetGuard(
        {bare_host(u) for u in (args.urls or start_urls)},
        on_status=lambda msg: display.overview.update(net_task, visible=msg is not None, info=msg or ""),
        on_event=lambda msg: console.print(f"[yellow]{msg}[/]", highlight=False),
    )

    crawler = Crawler(
        state, start_urls or args.urls, max_depth=settings["depth"], max_pages=args.max_pages,
        delay=settings["delay"], subdomains=settings["subdomains"], include=settings["include"],
        exclude=settings["exclude"], video_pattern=settings["video_pattern"],
        respect_robots=not settings["ignore_robots"], all_languages=settings["all_languages"],
        cookies=args.cookies, guard=guard,
    )

    if args.retry_failed:
        console.print(f"{state.reset_failed()} fehlgeschlagene Videos wieder eingereiht")
    if args.refresh or settings["_depth_raised"]:
        console.print(f"{state.refresh_listings()} Übersichtsseiten neu eingereiht")
    state.requeue_page_errors()
    state.release_stale()

    # Mit URL(s): nur diese Seite(n) bearbeiten. Andere Seiten im selben Ordner bleiben liegen
    # (eigener Tab oder später "pronload -o <Ordner>" ohne URL, der macht alles).
    focus = {bare_host(u) for u in args.urls} or None
    if focus:
        others = {bare_host(u) for u in start_urls} - focus
        console.print(f"Fokus: [bold]{', '.join(sorted(focus))}[/]" +
                      (f" [dim](liegen gelassen: {', '.join(sorted(others))})[/]" if others else ""))

    if args.no_crawl:
        for url in args.urls:
            try:
                text = crawler.fetch_html(url)
            except Exception:  # noqa: BLE001 - Titel ist nur Komfort, yt-dlp probiert es trotzdem
                text = None
            if text:
                from .extract import analyze
                video = crawler.to_video(analyze(url, text))
            else:
                video = Video(url)
            state.add_video(video)
    else:
        for url in args.urls:
            state.enqueue(url, 0)
        if args.sitemap:
            for url in start_urls:
                n = crawler.seed_sitemaps(url)
                console.print(f"Sitemap {url}: {n} Seiten eingereiht")

    dl = None
    with Live(display.renderable, console=console, refresh_per_second=4, transient=False):
        if not args.dry_run:
            dl = Downloader(state, out, display, workers=args.workers, fragments=args.fragments,
                            rate_limit=args.rate_limit, cookies=args.cookies,
                            cookies_from_browser=args.cookies_from_browser, sort=settings["sort"],
                            verbose=args.verbose, guard=guard)
        crawl_task = display.overview.add_task("Crawl", total=None, info="")
        found = 0

        def on_video(video: Video) -> None:
            nonlocal found
            found += 1
            is_new = state.add_video(video)
            if dl and is_new:
                dl.submit(video)
            elif args.dry_run and is_new:
                console.print(f"[cyan]▶[/] {video.title or video.page_url}  [dim]{video.page_url}[/]",
                              highlight=False)

        def on_tick(url: str) -> None:
            done, queued = state.queue_sizes(focus)
            display.overview.update(crawl_task, total=done + queued, completed=done,
                                    info=f"{found} Videoseiten · {url[-60:]}")

        try:
            if dl:  # Unfertiges vom letzten Lauf zuerst (inkl. .part-Fortsetzung)
                for video in state.pending_videos(MAX_ATTEMPTS, focus):
                    dl.submit(video)
            if not args.no_crawl:
                crawler.run(on_video, on_tick, should_stop=lambda: False, focus=focus)
            done, queued = state.queue_sizes(focus)
            display.overview.update(crawl_task, total=done + queued, completed=done,
                                    info=f"fertig · {found} Videoseiten" +
                                         (f" · {queued} Seiten offen (--max-pages)" if queued else ""))
            if dl:
                dl.wait()
        except KeyboardInterrupt:
            console.print("\n[yellow]Abbruch – laufende Downloads werden angehalten, "
                          "Stand ist gespeichert. Einfach nochmal starten zum Fortsetzen.[/]")
            if dl:
                dl.abort()
            return 130
        finally:
            guard.close()

    if dl:
        console.print(f"\n[bold]Fertig:[/] [green]{dl.ok} geladen[/], [red]{dl.failed} fehlgeschlagen[/] "
                      f"→ {out}")
        if dl.failed:
            console.print("[dim]Details: pronload --status · nochmal versuchen: pronload --retry-failed[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
