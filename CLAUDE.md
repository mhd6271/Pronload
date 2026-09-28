# pronload – Hinweise für Claude

Python-CLI (3.10+, getestet mit 3.14 unter Windows) zum Crawlen von Webseiten und Archivieren eingebetteter Videos. Benutzerdoku: README.md (deutsch).

## Aufbau
- `pronload/cli.py`: argparse, Live-Anzeige (rich), Ablauf. Einstellungen werden pro Zielordner in `meta.settings` gespeichert. Ein Aufruf ohne URL setzt den Lauf fort.
- `pronload/state.py`: SQLite `<out>/.pronload.db` mit `pages` (Warteschlange), `videos` (pending/done/failed, category, model) und `meta` (settings, nav_links, site_names).
- `pronload/crawler.py`: Breitensuche über die persistente Queue, robots.txt, Sitemaps. `learn_site()` merkt sich auf Tiefe 0 die Navigationslinks und den Seitennamen. `to_video()` filtert die Navigation raus, nimmt die Canonical-URL und säubert den Titel.
- `pronload/extract.py`: HTML-Analyse mit stdlib-HTMLParser, Heuristik für Videoseiten, Preview-Filter, Tracking-Parameter, Muster für Kategorie- und Model-Pfade. Außerdem `kvs_media()` (Entschlüsselung verwürfelter KVS-Links über `GenericIE._kvs_get_real_url`) und `url_priority()`/`lang_prefix()` für die Reihenfolge der Queue und den Sprachfilter.
- Fokus und Parallelbetrieb: Sind URLs angegeben, filtern `next_page`, `pending_videos` und `queue_sizes` auf deren Hosts (`State._host_filter`, per SQL LIKE). `next_page` reserviert atomar (`UPDATE … RETURNING`, Status `fetching`). Videos werden per `claim_video` (claimed_by/claimed_at) reserviert, ein Heartbeat-Thread im Downloader erneuert die Reservierung alle 30 s. `CLAIM_TTL` = 180 s, `release_stale()` läuft beim Start.
- DB-Zugriffe laufen alle über `State._run()`. Das wiederholt bei „database is locked“ bis zu `LOCK_PATIENCE` Sekunden. Massen-Schreibzugriffe (`enqueue_many`, `reprioritize`) laufen als eine `BEGIN IMMEDIATE`-Transaktion, sonst gibt es pro Zeile einen fsync und Sperrkonflikte mit anderen Terminals. `synchronous=NORMAL`.
- Downloader: eine HTTP-Session pro Thread (`_session()`). Deren Cookies gehen in `ydl.cookiejar`, weil KVS-Links mit `v-acctoken` nur mit der Session der Videoseite funktionieren.
- Priorität in der Queue: `pages.prio` (0 Videoseite, 1 normal, 2 Sortiervariante). Die Spalte wird per Migration in alten Datenbanken ergänzt, `reprioritize()` läuft bei jedem Start.
- Direkte Medien-URLs werden per `ydl.process_ie_result()` geladen und nicht über `extract_info`, weil der Generic-Extractor an KVS-URLs mit `.mp4/` scheitert.
- Achtung rich: `Progress.tasks` ist eine Liste, keine Zuordnung von Task-ID zu Task. Nie mit der Task-ID indizieren.
- `pronload/downloader.py`: ThreadPool mit yt-dlp. Die Videoseite zuerst, danach Fallback auf `rank_media(media)`. Der Dateiname ist stabil (sha1 der Seiten-URL), dadurch klappt `.part`-Resume. Das `Aborted`-Signal aus dem Progress-Hook sorgt für einen sauberen Abbruch per Strg+C.

## Konventionen
- Code-Kommentare und UI-Texte auf Deutsch.
- Keine seitenspezifischen Scraper: Seiten-Support kommt von yt-dlp oder bleibt generisch.
- Test: `python tests/test_e2e.py` (lokale Fake-Seite auf Port 8765, inklusive Range-Resume). Nach jeder Änderung laufen lassen.
- Echte Seiten nicht selbst crawlen oder testen, nur die lokale Fake-Seite. Reale Läufe macht der User.
- `pronload/play.py`: Textual-TUI (`pronload play`). Wiedergabe über eine temporäre .m3u an VLC (`--one-instance`), mpv oder `os.startfile`. Textual reserviert Attributnamen wie `visible`, deshalb heißt die Liste `shown`. Test: `python tests/test_play.py` (headless per Pilot, `play()` gemockt).
