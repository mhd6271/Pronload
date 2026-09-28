# pronload

Kommandozeilen-Tool, das eine Webseite crawlt und alle eingebetteten Videos archiviert. Mit `pronload play` durchsuchst du das Archiv danach im Terminal und spielst es in VLC ab.

- **Crawlt selbstständig:** Videoseiten werden automatisch erkannt und bevorzugt, Sprachversionen und Sortiervarianten übersprungen.
- **Lädt effizient:** parallele Downloads, immer die beste verfügbare Qualität, ein Fortschrittsbalken pro Datei.
- **Unterbrechen und weitermachen:** Jeder Lauf lässt sich abbrechen. Beim nächsten Start geht es beim letzten Stand weiter, halbfertige Dateien werden fortgesetzt.
- **Nichts doppelt:** Duplikate werden über die Canonical-URL, Tracking-Parameter und Video-IDs erkannt.
- **Ordentliche Ablage:** Dateiname aus dem Seitentitel, optional Unterordner pro Model oder Kategorie.
- **Basiert auf [yt-dlp](https://github.com/yt-dlp/yt-dlp):** kennt über 1800 Seiten und viele eingebettete Player, KVS-Seiten eingeschlossen.

```
  Videos     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  12/40   ✓ 11  ✗ 1
  Crawl      ━━━━━━━━━━━╸━━━━━━━━━━━━━━━━━━  96/310  38 Videoseiten · /videos/beispiel-titel/
    Beispiel Titel Eins                     ━━━━━━━━━━━━━━━━━━━━━━  61.2%  312.4/510.1 MB  8.2 MB/s  0:00:24
    Noch Ein Video                          ━━━━━━━━━╸━━━━━━━━━━━━  38.9%  151.0/388.2 MB  6.9 MB/s  0:00:34
```

## Installation

Voraussetzung: Python 3.10+.

```
python -m pip install git+https://github.com/mhd6271/Pronload.git
```

Oder zum Mitentwickeln:

```
git clone https://github.com/mhd6271/Pronload.git
cd Pronload
python -m pip install -e .
```

Aufruf: `pronload …` oder, falls der Python-Scripts-Ordner nicht im PATH liegt, `python -m pronload …`.

Empfohlen:
- **ffmpeg** für HLS/DASH-Streams mit getrennten Video- und Audiospuren (`winget install Gyan.FFmpeg`)
- **VLC** für `pronload play` (`winget install VideoLAN.VLC`). mpv geht auch, ohne beides wird der Windows-Standardplayer genommen.

## Benutzung

```
pronload https://seite.tld/                       # crawlen + laden, Ziel = aktueller Ordner
pronload https://seite.tld/ -o D:\Archiv          # anderer Zielordner
pronload https://seite.tld/ --sort model          # Unterordner pro Model/Creator
pronload https://seite.tld/ --sort category       # Unterordner pro Kategorie
pronload -o D:\Archiv                             # letzten Lauf in diesem Ordner fortsetzen
pronload -o D:\Archiv --refresh                   # Übersichtsseiten neu crawlen -> neue Uploads holen
pronload -o D:\Archiv --status                    # Stand + letzte Fehler anzeigen
pronload -o D:\Archiv --retry-failed              # fehlgeschlagene Videos nochmal versuchen
pronload URL1 URL2 --no-crawl                     # nur diese Videoseiten laden
pronload https://seite.tld/categories/x/ --depth 2 --dry-run   # nur schauen, was gefunden würde
```

Mit **Strg+C** wird sauber abgebrochen. Halbfertige `.part`-Dateien bleiben liegen und werden beim nächsten Start per HTTP-Range fortgesetzt.

### Wichtige Optionen

| Option | Bedeutung |
|---|---|
| `-o, --output` | Zielordner (Standard: aktueller Ordner). Dort liegt auch der Stand. |
| `-w, --workers` | parallele Downloads (Standard 3) |
| `--fragments` | parallele Fragmente pro HLS/DASH-Video (Standard 4) |
| `--rate-limit 5M` | Bandbreite pro Download begrenzen |
| `--depth N` | max. Linktiefe ab Start-URL; `--depth 0` = unbegrenzt (hebt ein gespeichertes Limit auf und crawlt die Seiten an der alten Grenze nochmal) |
| `--all-languages` | auch Sprachversionen (`/de/`, `/es/` …) crawlen. Standard: nur die Sprache der Start-URL |
| `--max-pages N` | max. Seitenabrufe in diesem Lauf (der Rest bleibt für den nächsten Lauf in der Warteschlange) |
| `--delay S` | Pause zwischen Seitenabrufen (Standard 0.5 s) |
| `--include / --exclude REGEX` | nur bestimmte URLs crawlen bzw. bestimmte URLs auslassen, z.B. `--exclude "/(members|albums|login)/"` |
| `--video-pattern REGEX` | Videoseiten per URL-Muster festlegen statt automatisch erkennen, z.B. `"/videos/[^/]+/$"` |
| `--sitemap` | sitemap.xml auslesen, oft der schnellste Weg zu allen Seiten |
| `--cookies-from-browser firefox` | Cookies aus dem Browser nutzen (Login, Altersabfrage) |
| `-v` | Meldungen von yt-dlp anzeigen (zur Fehlersuche) |

Einstellungen wie Tiefe, Filter und Sortierung werden pro Zielordner gespeichert. `pronload -o <Ordner>` ohne URL läuft dann mit denselben Einstellungen weiter.

### Mehrere Seiten, mehrere Terminals

Ein Archivordner kann beliebig viele Seiten enthalten. Jede bekommt ihren eigenen Unterordner (`D:\Archiv\seite-a.com\…`, `D:\Archiv\seite-b.com\…`).

- **Mit URL** kümmert sich ein Lauf nur um diese Seite, beim Crawlen wie beim Laden. Offenes von anderen Seiten im selben Ordner bleibt liegen.
- **Ohne URL** (`pronload -o D:\Archiv`) wird alles abgearbeitet, was im Ordner offen ist.
- **Mehrere Terminals gleichzeitig** auf denselben Ordner sind sicher, zum Beispiel ein Terminal pro Seite. Jeder Prozess reserviert die Seiten und Videos, an denen er arbeitet, und die anderen überspringen sie. So wird keine Datei doppelt oder gleichzeitig geladen. Stürzt ein Prozess ab, laufen seine Reservierungen nach 3 Minuten aus.

```
# Terminal 1                                   # Terminal 2
pronload https://seite-a.com/ -o D:\Archiv     pronload https://seite-b.com/ -o D:\Archiv
```

## Abspielen: `pronload play`

```
pronload play -o D:\Archiv        # oder einfach "pronload play" im Archivordner
```

Das öffnet einen Datei-Browser im Terminal. Links stehen die Ordner (Seite → Model/Kategorie), rechts die Videos mit Größe und Datum. Die Wiedergabe läuft in VLC, eine offene VLC-Instanz wird wiederverwendet.

| Taste | Aktion |
|---|---|
| `Enter` | Video abspielen, danach geht es mit den folgenden Videos der Liste weiter |
| `a` | alle Videos der aktuellen Liste abspielen |
| `z` | alle Videos der aktuellen Liste zufällig abspielen |
| `Leertaste` / `p` | Videos markieren / Markierte abspielen |
| `/` | Suche (Titel, Model, Kategorie – mehrere Wörter = alle müssen passen) |
| `o` | Sortierung: neueste / Titel / Größe |
| `e` | im Explorer zeigen |
| `F5` | neu einlesen (z.B. während im Hintergrund geladen wird) |
| `q` | beenden |

Ein Ordner, der im Baum ausgewählt ist, filtert die Liste auf diesen Ordner inklusive Unterordnern. „Alle“ und „zufällig“ beziehen sich immer auf das, was gerade in der Liste steht, also auch auf ein Suchergebnis.

## Ablage

```
<Ziel>/<domain>/[<Model|Kategorie>/]<Titel der Videoseite> [<8-stelliger Hash>].mp4
<Ziel>/.pronload.db               # Stand: Warteschlange, Videos, Einstellungen
<Ziel>/.pronload-archive.txt      # yt-dlp-Archiv (gleiche Video-ID nie doppelt)
```

- Der **Titel** kommt aus `og:title` bzw. `<title>` der Videoseite. Der Seitenname („… - FakeTube“) wird automatisch entfernt.
- Der **Hash** leitet sich aus der URL der Videoseite ab. Dadurch ist der Dateiname bei jedem Lauf gleich, und abgebrochene Downloads werden fortgesetzt statt neu begonnen.
- **Model/Kategorie** stammen aus Links wie `/models/<name>/`, `/onlyfans-models/<name>/` oder `/categories/<name>/` auf der Videoseite. Links aus der Navigation der Startseite werden ignoriert. Findet sich nichts, werden die Metadaten von yt-dlp genommen, sonst landet das Video in `_unsortiert`.

## So funktioniert es

1. **Crawler** (`crawler.py`): Arbeitet die Warteschlange in SQLite ab. Er bleibt auf der Domain, beachtet robots.txt und macht eine Pause zwischen den Abrufen.
   - **Reihenfolge:** Links, die wie eine Videoseite aussehen (`/videos/<titel>/`, `/watch/…`), kommen zuerst dran. Damit starten die Downloads sofort. Danach folgt Breitensuche über die Übersichten, Sortier- und Filtervarianten (`?sort_by=…`) kommen zuletzt.
   - **Sprachversionen** (`/de/`, `/es/`, …) sind dieselben Seiten nochmal und werden übersprungen.
   - **Fehler:** 4xx-Seiten gelten als erledigt, Netzwerk- und 5xx-Fehler werden beim nächsten Lauf wiederholt.
2. **Erkennung** (`extract.py`): Eine Seite gilt als Videoseite bei Player-Code (flashvars/KVS, JW Player, video.js, Plyr, Fluid Player, hls.js), bei `og:video` oder JSON-LD `VideoObject`, oder bei 1–3 echten `<video>`-Quellen. Hover-Previews (`preview`, `thumb`, `teaser`, …) und Übersichten mit vielen `<video>`-Tags zählen nicht. Tracking-Parameter (`?ref=`, `utm_*`) werden entfernt, die Canonical-URL dient als Schlüssel.
3. **Download** (`downloader.py`): Zuerst bekommt yt-dlp die Videoseite, das kennt über 1800 Seiten und viele eingebettete Player. Klappt das nicht, wird die Videoseite frisch geladen und ihre Medien-URLs werden direkt geladen, höchste Auflösung zuerst, mit der Videoseite als Referer. Nach 3 Fehlversuchen gilt ein Video als fehlgeschlagen (`--retry-failed`).
   - **KVS-Seiten** (Kernel Video Sharing) verwürfeln ihre `get_file`-Links. pronload liest `license_code` und `video_url`/`video_alt_url*` unabhängig vom Variablennamen und entschlüsselt sie mit yt-dlps Routine. yt-dlp selbst findet die Links nur, wenn die Variable exakt `flashvars` heißt.

## Tests

```
python tests/test_e2e.py     # Crawler + Downloads gegen eine lokale Fake-Seite
python tests/test_play.py    # Archiv-Browser headless
```

`test_e2e.py` startet eine lokale Fake-Tube-Seite und prüft Folgendes: Erkennung, Previews, Duplikate, Titel, Sortierung, beste Qualität, KVS-Entschlüsselung, Reihenfolge der Queue, Sprachfilter, Neustart ohne Doppel-Downloads und das Fortsetzen einer abgebrochenen `.part`-Datei. Echte Seiten werden dabei nicht angefragt.

## Lizenz

[MIT](LICENSE)

## Rechtliches

pronload ist ein Werkzeug zum privaten Archivieren. Lade nur Inhalte, die du laden darfst, und beachte die Nutzungsbedingungen der jeweiligen Seite. Das Urheberrecht und die Rechte der Darsteller:innen und Creator:innen bleiben dabei unberührt. robots.txt wird standardmäßig beachtet (`--ignore-robots` schaltet das ab). Die Pause zwischen Abrufen (`--delay`) schont die Server.
