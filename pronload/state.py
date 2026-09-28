"""Persistenter Stand (SQLite): Crawl-Warteschlange + Video-Status.

Dadurch ist jeder Lauf wiederaufnehmbar: offene Seiten werden weiter gecrawlt,
offene/abgebrochene Videos weiter geladen, fertige nie doppelt. Mehrere Prozesse
(Terminals) dürfen gleichzeitig auf dieselbe Datenbank zugreifen.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# Reservierung eines Videos läuft ab, wenn der Prozess sie so lange nicht erneuert (Absturz)
CLAIM_TTL = 180
# So lange wird bei "database is locked" insgesamt wiederholt, bevor aufgegeben wird
LOCK_PATIENCE = 300


@dataclass
class Video:
    page_url: str
    title: str | None = None
    media: list[str] = field(default_factory=list)
    category: str | None = None
    model: str | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url      TEXT PRIMARY KEY,
    depth    INTEGER NOT NULL,
    prio     INTEGER NOT NULL DEFAULT 1,       -- 0 = vermutlich Videoseite (zuerst), 2 = Sortiervariante
    status   TEXT NOT NULL DEFAULT 'queued',   -- queued | fetching | done | error | skipped
    is_video INTEGER NOT NULL DEFAULT 0,
    added    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_status ON pages(status);
CREATE TABLE IF NOT EXISTS videos (
    page_url TEXT PRIMARY KEY,
    title    TEXT,
    media    TEXT,                             -- direkt gefundene Medien-URLs, \\n-getrennt
    category TEXT,                             -- erste Kategorie laut Videoseite
    model    TEXT,                             -- erstes Model/Creator laut Videoseite
    status   TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    file     TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error    TEXT,
    updated  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_status ON videos(status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _is_lock_error(e: sqlite3.OperationalError) -> bool:
    msg = str(e).lower()
    return "locked" in msg or "busy" in msg


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # timeout = SQLite-eigenes Warten auf fremde Schreibsperren; darüber hinaus wiederholt _run()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._lock = threading.Lock()
        # Eindeutige Kennung dieses Prozesses für Reservierungen (mehrere Terminals, gleicher Ordner)
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

        def setup(db: sqlite3.Connection) -> None:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")  # in WAL sicher und deutlich schneller
            db.executescript(SCHEMA)
            # Migrationen älterer Datenbanken
            for table, column, decl in (("pages", "prio", "INTEGER NOT NULL DEFAULT 1"),
                                        ("pages", "claimed_at", "REAL"),
                                        ("videos", "claimed_by", "TEXT"),
                                        ("videos", "claimed_at", "REAL")):
                if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            db.execute("CREATE INDEX IF NOT EXISTS pages_queue ON pages(status, prio, depth)")
        self._run(setup)

    # --- Low-Level: jeder Zugriff geht hier durch --------------------------------
    def _run(self, fn: Callable[[sqlite3.Connection], object]) -> object:
        """fn unter Thread-Lock ausführen; bei Sperren durch andere Prozesse geduldig wiederholen."""
        deadline = time.monotonic() + LOCK_PATIENCE
        delay = 0.2
        while True:
            try:
                with self._lock:
                    return fn(self._db)
            except sqlite3.OperationalError as e:
                if not _is_lock_error(e) or time.monotonic() > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.5, 3.0)

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        return self._run(lambda db: db.execute(sql, args).fetchall())

    def _n(self, sql: str, args: tuple = ()) -> int:
        """Wie _q, liefert aber die Zahl der geänderten Zeilen."""
        return self._run(lambda db: db.execute(sql, args).rowcount)

    def _many(self, sql: str, rows: list[tuple]) -> int:
        """Viele Schreibzugriffe in EINER Transaktion (sonst ein Commit pro Zeile)."""
        if not rows:
            return 0

        def tx(db: sqlite3.Connection) -> int:
            db.execute("BEGIN IMMEDIATE")
            try:
                n = sum(db.execute(sql, r).rowcount for r in rows)
                db.execute("COMMIT")
                return n
            except BaseException:
                db.rollback()
                raise
        return self._run(tx)

    # --- Einstellungen des Laufs (für Fortsetzen ohne Argumente) --------------
    def get_meta(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._q("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def _host_filter(column: str, hosts: set[str] | None) -> tuple[str, tuple]:
        """SQL-Bedingung 'URL gehört zu einem dieser Hosts' (inkl. www. und Subdomains)."""
        if not hosts:
            return "1", ()
        parts, args = [], []
        for h in sorted(hosts):
            for pattern in (f"%://{h}/%", f"%://{h}", f"%.{h}/%", f"%.{h}", f"%://{h}:%", f"%.{h}:%"):
                parts.append(f"{column} LIKE ?")
                args.append(pattern)
        return "(" + " OR ".join(parts) + ")", tuple(args)

    def queue_sizes(self, hosts: set[str] | None = None) -> tuple[int, int]:
        cond, args = self._host_filter("url", hosts)
        rows = dict(self._q(f"SELECT status='queued', COUNT(*) FROM pages WHERE {cond} "
                            "GROUP BY status='queued'", args))
        return rows.get(0, 0), rows.get(1, 0)  # (erledigt, offen)

    # --- Crawl-Warteschlange -------------------------------------------------
    def enqueue(self, url: str, depth: int, prio: int = 1) -> bool:
        return self.enqueue_many([(url, depth, prio)]) > 0

    def enqueue_many(self, items: Iterable[tuple[str, int, int]]) -> int:
        now = time.time()
        return self._many("INSERT OR IGNORE INTO pages(url, depth, prio, added) VALUES (?, ?, ?, ?)",
                          [(u, d, p, now) for u, d, p in items])

    def next_page(self, hosts: set[str] | None = None) -> tuple[str, int] | None:
        """Nächste Seite holen UND reservieren (atomar), damit parallele Prozesse sie nicht doppelt holen.
        Videoseiten zuerst, dann Breitensuche über den Rest; optional nur bestimmte Seiten."""
        cond, args = self._host_filter("url", hosts)
        rows = self._q(
            "UPDATE pages SET status='fetching', claimed_at=? WHERE rowid = ("
            f"SELECT rowid FROM pages WHERE status='queued' AND {cond} "
            "ORDER BY prio, depth, rowid LIMIT 1) RETURNING url, depth",
            (time.time(), *args))
        return rows[0] if rows else None

    def release_stale(self, page_after: float = 300, video_after: float = CLAIM_TTL) -> None:
        """Reservierungen abgestürzter/abgebrochener Prozesse wieder freigeben."""
        now = time.time()
        self._n("UPDATE pages SET status='queued' WHERE status='fetching' AND "
                "(claimed_at IS NULL OR claimed_at < ?)", (now - page_after,))
        self._n("UPDATE videos SET claimed_by=NULL WHERE claimed_at < ?", (now - video_after,))

    def unfetch(self, url: str) -> None:
        self._n("UPDATE pages SET status='queued' WHERE url=? AND status='fetching'", (url,))

    def reprioritize(self, prio_of: Callable[[str], int]) -> int:
        """Priorität der offenen Seiten neu berechnen (Migration / geändertes --video-pattern)."""
        rows = self._q("SELECT url, prio FROM pages WHERE status='queued'")
        return self._many("UPDATE pages SET prio=? WHERE url=?",
                          [(p, u) for u, old in rows if (p := prio_of(u)) != old])

    def finish_page(self, url: str, status: str, is_video: bool = False) -> None:
        self._n("UPDATE pages SET status=?, is_video=? WHERE url=?", (status, int(is_video), url))

    def refresh_listings(self) -> int:
        """Übersichtsseiten (keine Videoseiten) neu einreihen, um neue Uploads zu finden."""
        return self._n("UPDATE pages SET status='queued' WHERE is_video=0 "
                       "AND status NOT IN ('queued', 'fetching')")

    def requeue_page_errors(self) -> int:
        return self._n("UPDATE pages SET status='queued' WHERE status='error'")

    # --- Videos --------------------------------------------------------------
    def add_video(self, v: Video) -> bool:
        return self._n(
            "INSERT OR IGNORE INTO videos(page_url, title, media, category, model, updated) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (v.page_url, v.title, "\n".join(v.media), v.category, v.model, time.time())) > 0

    def pending_videos(self, max_attempts: int, hosts: set[str] | None = None) -> list[Video]:
        cond, args = self._host_filter("page_url", hosts)
        rows = self._q(
            "SELECT page_url, title, media, category, model FROM videos "
            f"WHERE (status='pending' OR (status='failed' AND attempts < ?)) AND {cond} "
            "AND (claimed_by IS NULL OR claimed_by=? OR claimed_at < ?) ORDER BY rowid",
            (max_attempts, *args, self.owner, time.time() - CLAIM_TTL),
        )
        return [Video(u, t, [m for m in (media or "").split("\n") if m], c, mo)
                for u, t, media, c, mo in rows]

    # --- Reservierungen (mehrere Prozesse auf demselben Ordner) --------------------
    def claim_video(self, page_url: str) -> bool:
        """True, wenn dieser Prozess das Video laden darf (frei, eigene oder abgelaufene Reservierung)."""
        now = time.time()
        return self._n(
            "UPDATE videos SET claimed_by=?, claimed_at=? WHERE page_url=? AND status!='done' AND "
            "(claimed_by IS NULL OR claimed_by=? OR claimed_at < ?)",
            (self.owner, now, page_url, self.owner, now - CLAIM_TTL)) > 0

    def touch_claims(self) -> None:
        self._n("UPDATE videos SET claimed_at=? WHERE claimed_by=?", (time.time(), self.owner))

    def release_video(self, page_url: str) -> None:
        self._n("UPDATE videos SET claimed_by=NULL WHERE page_url=? AND claimed_by=?", (page_url, self.owner))

    def release_all(self) -> None:
        self._n("UPDATE videos SET claimed_by=NULL WHERE claimed_by=?", (self.owner,))

    def video_done(self, page_url: str, file: str | None) -> None:
        self._n("UPDATE videos SET status='done', file=?, error=NULL, updated=?, claimed_by=NULL "
                "WHERE page_url=?", (file, time.time(), page_url))

    def video_failed(self, page_url: str, error: str) -> None:
        self._n("UPDATE videos SET status='failed', attempts=attempts+1, error=?, updated=?, claimed_by=NULL "
                "WHERE page_url=?", (error[:1500], time.time(), page_url))

    def reset_failed(self) -> int:
        return self._n("UPDATE videos SET status='pending', attempts=0 WHERE status='failed'")

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("pages", "videos"):
            for status, n in self._q(f"SELECT status, COUNT(*) FROM {table} GROUP BY status"):
                out[f"{table}.{status}"] = n
        return out

    def failed(self, limit: int = 50) -> list[tuple[str, int, str]]:
        return self._q("SELECT page_url, attempts, error FROM videos WHERE status='failed' "
                       "ORDER BY updated DESC LIMIT ?", (limit,))
