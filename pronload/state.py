"""Persistenter Stand (SQLite): Crawl-Warteschlange + Video-Status.

Dadurch ist jeder Lauf wiederaufnehmbar: offene Seiten werden weiter gecrawlt,
offene/abgebrochene Videos weiter geladen, fertige nie doppelt.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


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
    status   TEXT NOT NULL DEFAULT 'queued',   -- queued | done | error | skipped
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


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        # Migration: Datenbanken aus v0.1 haben noch keine Priorität
        if "prio" not in {row[1] for row in self._db.execute("PRAGMA table_info(pages)")}:
            self._db.execute("ALTER TABLE pages ADD COLUMN prio INTEGER NOT NULL DEFAULT 1")
        self._db.execute("CREATE INDEX IF NOT EXISTS pages_queue ON pages(status, prio, depth)")
        self._lock = threading.Lock()

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- Einstellungen des Laufs (für Fortsetzen ohne Argumente) --------------
    def get_meta(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._q("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def queue_sizes(self) -> tuple[int, int]:
        rows = dict(self._q("SELECT status='queued', COUNT(*) FROM pages GROUP BY status='queued'"))
        return rows.get(0, 0), rows.get(1, 0)  # (erledigt, offen)

    # --- Crawl-Warteschlange -------------------------------------------------
    def enqueue(self, url: str, depth: int, prio: int = 1) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO pages(url, depth, prio, added) VALUES (?, ?, ?, ?)",
                (url, depth, prio, time.time()),
            )
            return cur.rowcount > 0

    def next_page(self) -> tuple[str, int] | None:
        # Videoseiten zuerst, dann Breitensuche über den Rest
        rows = self._q("SELECT url, depth FROM pages WHERE status='queued' "
                       "ORDER BY prio, depth, rowid LIMIT 1")
        return rows[0] if rows else None

    def reprioritize(self, prio_of: Callable[[str], int]) -> int:
        """Priorität der offenen Seiten neu berechnen (Migration / geändertes --video-pattern)."""
        rows = self._q("SELECT url, prio FROM pages WHERE status='queued'")
        changes = [(p, u) for u, old in rows if (p := prio_of(u)) != old]
        with self._lock:
            self._db.executemany("UPDATE pages SET prio=? WHERE url=?", changes)
        return len(changes)

    def finish_page(self, url: str, status: str, is_video: bool = False) -> None:
        self._q("UPDATE pages SET status=?, is_video=? WHERE url=?", (status, int(is_video), url))

    def refresh_listings(self) -> int:
        """Übersichtsseiten (keine Videoseiten) neu einreihen, um neue Uploads zu finden."""
        with self._lock:
            return self._db.execute(
                "UPDATE pages SET status='queued' WHERE is_video=0 AND status!='queued'"
            ).rowcount

    def requeue_page_errors(self) -> int:
        with self._lock:
            return self._db.execute("UPDATE pages SET status='queued' WHERE status='error'").rowcount

    # --- Videos --------------------------------------------------------------
    def add_video(self, v: "Video") -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO videos(page_url, title, media, category, model, updated) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (v.page_url, v.title, "\n".join(v.media), v.category, v.model, time.time()),
            )
            return cur.rowcount > 0

    def pending_videos(self, max_attempts: int) -> list["Video"]:
        rows = self._q(
            "SELECT page_url, title, media, category, model FROM videos "
            "WHERE status='pending' OR (status='failed' AND attempts < ?) ORDER BY rowid",
            (max_attempts,),
        )
        return [Video(u, t, [m for m in (media or "").split("\n") if m], c, mo)
                for u, t, media, c, mo in rows]

    def video_done(self, page_url: str, file: str | None) -> None:
        self._q("UPDATE videos SET status='done', file=?, error=NULL, updated=? WHERE page_url=?",
                (file, time.time(), page_url))

    def video_failed(self, page_url: str, error: str) -> None:
        self._q("UPDATE videos SET status='failed', attempts=attempts+1, error=?, updated=? WHERE page_url=?",
                (error[:500], time.time(), page_url))

    def reset_failed(self) -> int:
        with self._lock:
            return self._db.execute("UPDATE videos SET status='pending', attempts=0 WHERE status='failed'").rowcount

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("pages", "videos"):
            for status, n in self._q(f"SELECT status, COUNT(*) FROM {table} GROUP BY status"):
                out[f"{table}.{status}"] = n
        return out

    def failed(self, limit: int = 50) -> list[tuple[str, int, str]]:
        return self._q("SELECT page_url, attempts, error FROM videos WHERE status='failed' "
                       "ORDER BY updated DESC LIMIT ?", (limit,))
