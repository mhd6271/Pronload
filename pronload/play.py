"""`pronload play`: Archiv im Terminal durchstöbern, Wiedergabe in VLC (oder mpv / Windows-Player)."""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Static, Tree

VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv", ".avi", ".wmv", ".ts"}
_TAG = re.compile(r"\s*\[[0-9a-f]{8}\]$")


@dataclass
class Clip:
    path: Path
    title: str
    size: int
    mtime: float


def scan(root: Path) -> list[Clip]:
    clips = []
    for p in root.rglob("*"):
        if p.suffix.lower() in VIDEO_EXT and p.is_file() and not p.name.startswith("."):
            st = p.stat()
            clips.append(Clip(p, _TAG.sub("", p.stem), st.st_size, st.st_mtime))
    return clips


# --- Player ------------------------------------------------------------------
def find_player() -> tuple[str, list[str]] | None:
    """(Name, Befehl ohne Playlist) des besten verfügbaren Players."""
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VideoLAN/VLC/vlc.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "VideoLAN/VLC/vlc.exe",
    ]
    for exe in [shutil.which("vlc"), *map(str, filter(Path.exists, candidates))]:
        if exe:
            # eine laufende VLC-Instanz wiederverwenden statt immer neue Fenster
            return "VLC", [exe, "--one-instance", "--no-playlist-enqueue", "--no-random"]
    if mpv := shutil.which("mpv"):
        return "mpv", [mpv, "--force-window=yes", "--playlist"]
    return None


def play(paths: list[Path]) -> str:
    if not paths:
        return "nichts ausgewählt"
    # Playlist-Datei statt Kommandozeile (Windows begrenzt die Länge auf ~32k Zeichen)
    fd, m3u = tempfile.mkstemp(prefix="pronload-", suffix=".m3u")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for p in paths:
            f.write(f"#EXTINF:-1,{_TAG.sub('', p.stem)}\n{p.resolve()}\n")
    player = find_player()
    if player is None:
        os.startfile(m3u)  # Windows-Standardplayer
        return f"{len(paths)} Video(s) im Standardplayer (kein VLC/mpv gefunden)"
    name, cmd = player
    cmd = cmd[:-1] + [f"--playlist={m3u}"] if name == "mpv" else cmd + [m3u]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(cmd, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    return f"▶ {len(paths)} Video(s) in {name}"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --- Oberfläche ----------------------------------------------------------------
class PlayApp(App):
    TITLE = "pronload play"
    CSS = """
    #folders { width: 34%; min-width: 24; border: round $primary 40%; }
    #right { width: 1fr; }
    #search { margin: 0 0 0 0; }
    #list { height: 1fr; border: round $primary 40%; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("enter", "play_one", "Abspielen", show=True, priority=False),
        Binding("space", "mark", "Markieren"),
        Binding("p", "play_marked", "Markierte"),
        Binding("a", "play_all", "Alle"),
        Binding("z", "shuffle", "Alle zufällig"),
        Binding("o", "sort", "Sortierung"),
        Binding("slash", "search", "Suchen"),
        Binding("e", "explorer", "Im Explorer"),
        Binding("f5", "rescan", "Neu einlesen"),
        Binding("q", "quit", "Beenden"),
    ]
    SORTS = [("Neueste zuerst", lambda c: -c.mtime), ("Titel A–Z", lambda c: c.title.lower()),
             ("Größte zuerst", lambda c: -c.size)]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root_dir = root
        self.clips: list[Clip] = []
        self.folder = root
        self.shown: list[Clip] = []
        self.marked: set[Path] = set()
        self.sort_idx = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree("Archiv", id="folders")
            with Vertical(id="right"):
                yield Input(placeholder="Suchen … (Titel, Model, Kategorie)", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("", key="mark", width=1)
        table.add_column("Titel", key="title")
        table.add_column("Ordner", key="folder")
        table.add_column("Größe", key="size")
        table.add_column("Geladen", key="date")
        self.action_rescan()
        table.focus()

    # Daten ---------------------------------------------------------------
    def action_rescan(self) -> None:
        self.clips = scan(self.root_dir)
        tree = self.query_one(Tree)
        tree.clear()
        tree.root.data = self.root_dir
        tree.root.set_label(f"Archiv ({len(self.clips)})")
        counts: dict[Path, int] = {}
        for c in self.clips:
            for parent in c.path.relative_to(self.root_dir).parents:
                if str(parent) != ".":
                    counts[self.root_dir / parent] = counts.get(self.root_dir / parent, 0) + 1
        nodes = {self.root_dir: tree.root}
        for folder in sorted(counts, key=lambda p: (len(p.parts), str(p).lower())):
            parent = nodes.get(folder.parent, tree.root)
            nodes[folder] = parent.add(f"{folder.name} ({counts[folder]})", data=folder,
                                       expand=len(folder.relative_to(self.root_dir).parts) < 2)
        tree.root.expand()
        self.refresh_list()

    def refresh_list(self) -> None:
        query = self.query_one(Input).value.strip().lower()
        words = query.split()
        items = [c for c in self.clips if self.folder in c.path.parents]
        if words:
            items = [c for c in items
                     if all(w in str(c.path.relative_to(self.root_dir)).lower() for w in words)]
        items.sort(key=self.SORTS[self.sort_idx][1])
        self.shown = items
        table = self.query_one(DataTable)
        table.clear()
        for c in items:
            rel = c.path.parent.relative_to(self.root_dir)
            table.add_row("●" if c.path in self.marked else "", c.title,
                          Text(str(rel), style="dim"), Text(human(c.size), justify="right"),
                          time.strftime("%d.%m.%y %H:%M", time.localtime(c.mtime)), key=str(c.path))
        where = "Archiv" if self.folder == self.root_dir else str(self.folder.relative_to(self.root_dir))
        self.status(f"{where}: {len(items)} Videos, {human(sum(c.size for c in items))}"
                    f" · Sortierung: {self.SORTS[self.sort_idx][0]}"
                    + (f" · {len(self.marked)} markiert" if self.marked else ""))

    def status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def current(self) -> Clip | None:
        table = self.query_one(DataTable)
        if not self.shown or table.cursor_row is None or table.cursor_row >= len(self.shown):
            return None
        return self.shown[table.cursor_row]

    # Events ----------------------------------------------------------------
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data:
            self.folder = event.node.data
            self.refresh_list()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.refresh_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_play_one()

    # Aktionen ------------------------------------------------------------------
    def action_play_one(self) -> None:
        # Enter im Suchfeld/Baum soll nicht abspielen, nur in der Liste
        if not isinstance(self.focused, DataTable):
            return
        c = self.current()
        if c:
            # ab dem gewählten Video weiter durch die Liste - wie in einem normalen Player
            idx = self.shown.index(c)
            self.status(play([x.path for x in self.shown[idx:]]))

    def action_mark(self) -> None:
        c = self.current()
        if not c:
            return
        self.marked.symmetric_difference_update({c.path})
        table = self.query_one(DataTable)
        table.update_cell(str(c.path), "mark", "●" if c.path in self.marked else "")
        if table.cursor_row < len(self.shown) - 1:
            table.move_cursor(row=table.cursor_row + 1)

    def action_play_marked(self) -> None:
        paths = [c.path for c in self.shown if c.path in self.marked] or \
                [p for p in self.marked if p.exists()]
        self.status(play(paths) if paths else "nichts markiert (Leertaste)")

    def action_play_all(self) -> None:
        self.status(play([c.path for c in self.shown]))

    def action_shuffle(self) -> None:
        paths = [c.path for c in self.shown]
        random.shuffle(paths)
        self.status(play(paths) + " (zufällig)" if paths else "keine Videos")

    def action_sort(self) -> None:
        self.sort_idx = (self.sort_idx + 1) % len(self.SORTS)
        self.refresh_list()

    def action_search(self) -> None:
        self.query_one(Input).focus()

    def action_explorer(self) -> None:
        c = self.current()
        if c and sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(c.path)])
        elif sys.platform == "win32":
            os.startfile(self.folder)


def run(root: Path) -> int:
    if not root.exists():
        print(f"Ordner gibt es nicht: {root}")
        return 1
    PlayApp(root).run()
    return 0
