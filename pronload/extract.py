"""HTML-Analyse: Links, direkt eingebettete Medien und Video-Signale einer Seite."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MEDIA_EXT = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv", ".m3u8", ".mpd")
SKIP_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".css", ".js", ".json", ".xml", ".txt", ".pdf", ".zip", ".rar", ".7z",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".wav", ".ogg",
)

# Medien-URLs irgendwo im Quelltext (auch JS-escaped mit \/)
RAW_MEDIA_RE = re.compile(
    r"""https?:(?:\\?/){2}[^\s"'<>()\\]+?\.(?:mp4|m4v|webm|mkv|mov|m3u8|mpd)(?:\?[^\s"'<>()]*)?""",
    re.IGNORECASE,
)
# Hinweise auf gängige Player / eingebettete Videos
PLAYER_HINTS = re.compile(
    r"flashvars|video_url\s*[:=]|jwplayer\(|videojs\(|new\s+Plyr|Clappr\.Player|"
    r"fluidPlayer\(|hls\.loadSource|\"@type\"\s*:\s*\"VideoObject\"",
    re.IGNORECASE,
)

# Vorschau-Clips / Sprites / Trailer sind nicht das eigentliche Video
PREVIEW_RE = re.compile(r"preview|thumb|teaser|sprite|trailer|/screenshots?/", re.IGNORECASE)


# Typische Pfade von Tube-Seiten, optional mit Sprachpräfix (/de/...)
CATEGORY_PATH = re.compile(r"^/(?:[a-z]{2}/)?(?:categories|category|cats?|genres?|niches?)/([^/]+)/?$", re.I)
MODEL_PATH = re.compile(
    r"^/(?:[a-z]{2}/)?(?:[a-z0-9]+-)*(?:models?|pornstars?|stars|creators?|actors?|actresses|"
    r"performers?|channels?|studios?)/([^/]+)/?$", re.I)


# Sprachpräfixe (/de/, /pt-br/ …): dieselben Seiten nochmal übersetzt
LANG_CODES = {
    "ar", "bg", "bn", "cs", "da", "de", "el", "en", "es", "et", "fa", "fi", "fil", "fr", "he", "hi", "hr",
    "hu", "it", "ja", "jp", "ko", "kr", "lt", "lv", "ms", "nl", "no", "pl", "pt", "ro", "ru", "sk",
    "sl", "sr", "sv", "th", "tr", "uk", "ua", "vi", "zh", "cn", "tw",
}
_LANG_RE = re.compile(r"^/([a-z]{2,3})(?:[-_][a-z]{2,4})?(?:/|$)", re.I)
# Detailseite eines Videos: /videos/<slug>/, /watch/<slug>, … (reine Zahlen = Seitennummer)
VIDEO_LINK = re.compile(
    r"/(?:videos?|watch|movies?|clips?|v|embed|scenes?|porn|vid)/(?![0-9]+/?$)[^/?#]+/?$", re.I)
# Sortier-/Filtervarianten zeigen nur dieselben Videos in anderer Reihenfolge
SORT_PARAM = re.compile(r"(?:^|&)(?:sort|sort_by|sortby|order|order_by|orderby|filter|duration|period)=", re.I)


def lang_prefix(url: str) -> str | None:
    m = _LANG_RE.match(urlparse(url).path)
    return m.group(1).lower() if m and m.group(1).lower() in LANG_CODES else None


def url_priority(url: str, video_pattern: re.Pattern | None = None) -> int:
    """0 = vermutlich Videoseite, 1 = normale Seite, 2 = Sortier-/Filtervariante."""
    parts = urlparse(url)
    if video_pattern.search(url) if video_pattern else VIDEO_LINK.search(parts.path):
        return 0
    return 2 if SORT_PARAM.search(parts.query) else 1


def slug_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_+]+", slug) if w)


def link_label(url: str) -> str | None:
    """'/onlyfans-models/august-heat/' -> 'August Heat'"""
    path = urlparse(url).path
    m = CATEGORY_PATH.match(path) or MODEL_PATH.match(path)
    return slug_name(m.group(1)) if m else None


# KVS (Kernel Video Sharing): Links liegen als "function/0/<url>" mit verwürfeltem Hash im
# Quelltext, der license_code der Seite stellt ihn wieder her. Absichtlich unabhängig vom
# Variablennamen (flashvars, flashvars_12345, …), daran scheitert yt-dlps Generic-Extractor.
_KVS_LICENSE = re.compile(r"""license_code['"]?\s*[:=]\s*['"](\$?\d+)['"]""")
_KVS_URL = re.compile(r"""\b(video_(?:url|alt_url\d*))['"]?\s*[:=]\s*['"]([^'"]+)['"]""")


def kvs_media(text: str, base: str) -> list[str]:
    urls = _KVS_URL.findall(text)
    if not urls:
        return []
    lic = _KVS_LICENSE.search(text)
    out = []
    for _key, raw in urls:
        raw = raw.replace("\\/", "/")
        if raw.startswith("function/0/"):
            if not lic:
                continue
            try:
                from yt_dlp.extractor.generic import GenericIE
                raw = GenericIE._kvs_get_real_url(raw, lic.group(1))
            except Exception:  # noqa: BLE001 - kaputter Code/URL: dann eben nicht
                continue
        n = normalize(raw, base)
        if n and path_ext(n) in MEDIA_EXT or (n and "/get_file/" in n):
            out.append(n)
    return list(dict.fromkeys(out))


@dataclass
class PageInfo:
    url: str
    title: str | None = None
    links: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    has_player: bool = False
    categories: list[str] = field(default_factory=list)  # Link-URLs, Filterung der Navigation im Crawler
    models: list[str] = field(default_factory=list)
    canonical: str | None = None
    site_name: str | None = None

    @property
    def is_video_page(self) -> bool:
        return self.has_player


TRACKING_PARAM = re.compile(r"^(utm_\w+|ref|refer|referrer|from|source|src|fbclid|gclid|promo|aff\w*|campaign)$", re.I)


def normalize(url: str, base: str) -> str | None:
    url = html.unescape(url.strip())
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    absolute, _ = urldefrag(urljoin(base, url))
    parts = urlparse(absolute)
    if parts.scheme not in ("http", "https"):
        return None
    if parts.query:  # ?ref=... & Co. erzeugen sonst Duplikate derselben Seite
        query = "&".join(q for q in parts.query.split("&") if not TRACKING_PARAM.match(q.split("=", 1)[0]))
        absolute = urlunparse(parts._replace(query=query))
    return absolute


def path_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    dot = path.rfind(".")
    return path[dot:] if dot > path.rfind("/") else ""


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.media: list[str] = []
        self.meta_video: list[str] = []
        self.ld_json: list[str] = []
        self.title_parts: list[str] = []
        self.og_title: str | None = None
        self.site_name: str | None = None
        self.canonical: str | None = None
        self._in_title = False
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        elif tag in ("video", "source"):
            for key in ("src", "data-src"):
                if a.get(key):
                    self.media.append(a[key])
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream", "contenturl"):
                if a.get("content"):
                    self.meta_video.append(a["content"])
            elif prop == "og:title":
                self.og_title = a.get("content") or None
            elif prop == "og:site_name":
                self.site_name = a.get("content") or None
        elif tag == "link" and "canonical" in a.get("rel", "").lower() and a.get("href"):
            self.canonical = a["href"]
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self.ld_json.append("")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_ld:
            self.ld_json[-1] += data


def _ld_video_urls(blob: str) -> list[str]:
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return []
    found: list[str] = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if node.get("@type") == "VideoObject" and isinstance(node.get("contentUrl"), str):
                found.append(node["contentUrl"])
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return found


def analyze(url: str, text: str) -> PageInfo:
    p = _Parser()
    try:
        p.feed(text)
    except Exception:  # kaputtes HTML soll den Crawl nicht abbrechen
        pass

    info = PageInfo(url=url)
    info.title = p.og_title or " ".join("".join(p.title_parts).split()) or None
    info.site_name = p.site_name
    info.canonical = normalize(p.canonical, url) if p.canonical else None

    seen: set[str] = set()
    media: list[str] = []
    for href in p.hrefs:
        n = normalize(href, url)
        if not n:
            continue
        if path_ext(n) in MEDIA_EXT:
            media.append(n)
        elif n not in seen and path_ext(n) not in SKIP_EXT:
            seen.add(n)
            info.links.append(n)
            path = urlparse(n).path
            if CATEGORY_PATH.match(path):
                info.categories.append(n)
            elif MODEL_PATH.match(path):
                info.models.append(n)

    for raw in p.media + p.meta_video + [u for b in p.ld_json for u in _ld_video_urls(b)]:
        n = normalize(raw, url)
        if n:
            media.append(n)
    kvs = kvs_media(text, url)
    # Verwürfelte KVS-Links ("function/0/…") sind ohne Entschlüsselung wertlos -> nicht roh übernehmen
    scrambled = {m.split("function/0/", 1)[1].replace("\\/", "/").rstrip("/")
                 for m in re.findall(r"function/0/[^'\"\s]+", text)}
    media += [m.replace("\\/", "/") for m in RAW_MEDIA_RE.findall(text)]
    info.media = kvs + [m for m in dict.fromkeys(media)
                        if path_ext(m) in MEDIA_EXT and m.rstrip("/") not in scrambled and m not in kvs]
    info.media = [m for m in info.media if not PREVIEW_RE.search(m)]
    # Starke Signale: echter Player-Code, og:video / VideoObject, oder wenige <video>-Quellen.
    # Viele <video>-Tags = Übersicht mit Hover-Previews, keine Videoseite.
    tag_media = {m for m in p.media if not PREVIEW_RE.search(m)}
    info.has_player = (bool(kvs) or bool(PLAYER_HINTS.search(text)) or bool(p.meta_video)
                       or 1 <= len(tag_media) <= 3)
    return info


_QUALITY_RES = (re.compile(r"(\d{3,4})[pP](?![a-z])"), re.compile(r"_(\d{3,4})\.[a-z0-9]+/?$", re.I))


def rank_media(urls: list[str]) -> list[str]:
    """Beste Kandidaten zuerst: höchste Auflösung im Dateinamen, Manifeste vor Einzeldateien."""
    def score(u: str) -> tuple[int, int]:
        name = urlparse(u).path.rstrip("/").rsplit("/", 1)[-1]
        res = 0
        for rx in _QUALITY_RES:  # "720p" hat Vorrang vor "_720.mp4"; Pfadteile wie /1000/ zählen nicht
            m = rx.search(name)
            if m:
                res = int(m.group(1))
                break
        return (res, 1 if path_ext(u.rstrip("/")) in (".m3u8", ".mpd") else 0)
    return sorted(urls, key=score, reverse=True)
