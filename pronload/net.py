"""Internet-Ausfälle erkennen: alles pausieren und in wachsenden Abständen erneut prüfen.

Fehler, die während eines Ausfalls passieren, zählen nicht als Fehlversuch - Seiten gehen
zurück in die Warteschlange, Downloads laufen danach an derselben Stelle weiter (.part).
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Iterable

import requests

# Wartezeiten zwischen den Prüfungen (Sekunden); danach bleibt es beim letzten Wert
BACKOFF = [10, 30, 60, 120, 300]
# Gut erreichbare Anycast-Adressen: klappt keine, ist das Internet weg (nicht nur eine Seite)
PROBE_ADDRS = [("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)]
PROBE_TIMEOUT = 3

# Typische Texte von Netzwerkfehlern (requests, urllib, yt-dlp, Windows-Socket-Fehler)
NET_ERROR_HINTS = (
    "getaddrinfo failed", "name resolution", "failed to resolve", "nodename nor servname",
    "network is unreachable", "no route to host", "host is unreachable", "connection aborted",
    "connection reset", "remotedisconnected", "remote end closed", "timed out", "timeout",
    "max retries exceeded", "connection refused", "unable to connect",
    "winerror 10051", "winerror 10053", "winerror 10054", "winerror 10060", "winerror 10061",
    "winerror 10065", "errno 11001", "errno 11002", "errno -2", "errno -3",
)


def probe_internet(hosts: Iterable[str] = ()) -> bool:
    """True, wenn Internet UND Namensauflösung der Zielseiten funktionieren."""
    for addr in PROBE_ADDRS:
        try:
            socket.create_connection(addr, timeout=PROBE_TIMEOUT).close()
            break
        except OSError:
            continue
    else:
        return False
    for host in hosts:
        try:
            socket.getaddrinfo(host, 443)
        except OSError:
            return False
    return True


def _chain(e: BaseException) -> Iterable[BaseException]:
    """Die Ausnahme selbst plus alles, was sie verursacht hat (yt-dlp verpackt gern)."""
    seen: set[int] = set()
    stack = [e]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        stack += [cur.__cause__, cur.__context__]
        exc_info = getattr(cur, "exc_info", None)  # yt_dlp.utils.DownloadError
        if isinstance(exc_info, tuple) and len(exc_info) > 1 and isinstance(exc_info[1], BaseException):
            stack.append(exc_info[1])


def is_network_error(e: BaseException) -> bool:
    try:
        from yt_dlp.networking.exceptions import TransportError
    except ImportError:  # sehr alte yt-dlp-Version
        TransportError = ()  # noqa: N806
    for cur in _chain(e):
        if isinstance(cur, requests.HTTPError):
            return False  # Server hat geantwortet -> Netz ist da
        if isinstance(cur, (requests.ConnectionError, requests.Timeout, ConnectionError, TimeoutError,
                            socket.gaierror, socket.timeout) + ((TransportError,) if TransportError else ())):
            return True
    msg = " ".join(str(c) for c in _chain(e)).lower()
    return any(h in msg for h in NET_ERROR_HINTS)


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


class NetGuard:
    """Gemeinsamer Schalter für Crawler und alle Download-Threads."""

    def __init__(self, hosts: Iterable[str] = (),
                 on_status: Callable[[str | None], None] | None = None,
                 on_event: Callable[[str], None] | None = None) -> None:
        self.hosts = sorted(set(hosts))
        self._online = threading.Event()
        self._online.set()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._on_status = on_status or (lambda msg: None)
        self._on_event = on_event or (lambda msg: None)
        self.outages = 0

    @property
    def online(self) -> bool:
        return self._online.is_set()

    def check(self, err: BaseException) -> bool:
        """Nach einem Fehler aufrufen. True = wir sind offline (Aufrufer pausiert, Fehler zählt nicht)."""
        if not is_network_error(err):
            return False
        with self._lock:
            if not self._online.is_set():
                return True  # Ausfall schon bekannt
            if probe_internet(self.hosts):
                return False  # Netz da -> Problem liegt bei der Seite, normaler Fehler
            self._online.clear()
            self.outages += 1
            self._offline_since = time.monotonic()
            self._on_event("⏸ Keine Internetverbindung – pausiere und prüfe in wachsenden Abständen "
                           f"({', '.join(fmt_duration(s) for s in BACKOFF)}, dann alle "
                           f"{fmt_duration(BACKOFF[-1])}) erneut.")
            threading.Thread(target=self._probe_loop, daemon=True, name="netguard").start()
        return True

    def wait(self, should_stop: Callable[[], bool]) -> bool:
        """Blockiert, solange offline. False, wenn währenddessen abgebrochen wurde."""
        while not self._online.wait(0.5):
            if should_stop() or self._closed.is_set():
                return False
        return True

    def close(self) -> None:
        self._closed.set()

    def _probe_loop(self) -> None:
        attempt = 0
        while not self._closed.is_set():
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            deadline = time.monotonic() + delay
            while (left := deadline - time.monotonic()) > 0:
                self._on_status(f"⏸ offline – nächster Versuch in {fmt_duration(left)} "
                                f"(Prüfung {attempt + 1})")
                if self._closed.wait(min(1.0, left)):
                    return
            self._on_status("prüfe Verbindung …")
            if probe_internet(self.hosts):
                break
            attempt += 1
        else:
            return
        took = fmt_duration(time.monotonic() - self._offline_since)
        self._on_status(None)
        self._on_event(f"▶ Wieder online (nach {took}) – es geht weiter.")
        self._online.set()
