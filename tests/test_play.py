"""Headless-Test des Archiv-Browsers: python tests/test_play.py"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pronload import play as playmod  # noqa: E402

CALLS: list[list[Path]] = []
playmod.play = lambda paths: (CALLS.append(list(paths)), f"{len(paths)} gespielt")[1]


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {
            "site.com/Anna Muster/Erstes Video [aaaaaaaa].mp4": 3,
            "site.com/Anna Muster/Drittes Video [bbbbbbbb].mp4": 2,
            "site.com/Berta Beispiel/Zweites Video [cccccccc].mp4": 1,
            "other.net/Irgendwas [dddddddd].webm": 0,
        }
        for i, (rel, age) in enumerate(files.items()):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * (1000 * (i + 1)))
            t = time.time() - age * 3600
            os.utime(p, (t, t))
        (root / "site.com/Anna Muster/halb [eeeeeeee].mp4.part").write_bytes(b"x")

        app = playmod.PlayApp(root)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            check(len(app.shown) == 4, f"4 Videos, .part ignoriert ({[c.title for c in app.shown]})")
            check(app.shown[0].title == "Irgendwas", "neueste zuerst, Hash aus dem Titel entfernt")

            await pilot.press("a")
            check(len(CALLS[-1]) == 4, "a = alle abspielen")

            await pilot.press("z")
            check(sorted(CALLS[-1]) == sorted(CALLS[-2]), "z = dieselben Videos, gemischt")

            await pilot.press("slash")
            await pilot.press(*"anna")
            await pilot.pause()
            check([c.title for c in app.shown] == ["Drittes Video", "Erstes Video"],
                  "Suche findet auch über den Ordnernamen (Model)")
            await pilot.press("enter")  # zurück in die Liste
            await pilot.press("enter")  # abspielen ab Cursor
            check([p.name for p in CALLS[-1]] == ["Drittes Video [bbbbbbbb].mp4", "Erstes Video [aaaaaaaa].mp4"],
                  "Enter spielt ab dem gewählten Video weiter")

            await pilot.press("down", "space")
            await pilot.press("p")
            check([p.name for p in CALLS[-1]] == ["Erstes Video [aaaaaaaa].mp4"], "Leertaste + p = Markierte")

            await pilot.press("o")
            check(app.SORTS[app.sort_idx][0] == "Titel A–Z", "o wechselt die Sortierung")

            # Ordner im Baum wählen -> Liste zeigt nur diesen Ordner
            app.query_one(playmod.Input).value = ""
            tree = app.query_one(playmod.Tree)
            node = next(n for n in tree.root.children[0].children if n.data.name == "Berta Beispiel") \
                if tree.root.children[0].data.name == "site.com" else None
            if node is None:
                node = next(n for top in tree.root.children for n in top.children if n.data.name == "Berta Beispiel")
            tree.move_cursor(node)
            await pilot.pause()
            check([c.title for c in app.shown] == ["Zweites Video"], "Ordnerwahl im Baum filtert die Liste")
    print("\nalles grün")


if __name__ == "__main__":
    asyncio.run(main())
