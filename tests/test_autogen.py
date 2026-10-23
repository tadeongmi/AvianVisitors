#!/usr/bin/env python3
"""Checks for the unattended illustration generator's selection logic."""

from __future__ import annotations

import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "avian" / "autogen" / "autogen.py"
SPEC = importlib.util.spec_from_file_location("autogen", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

PAPER = (236, 222, 196)


def render(bird_box: tuple[int, int, int, int]) -> bytes:
    """A cream-ground render with a dark bird filling bird_box (x0, y0, x1, y1)."""
    arr = np.full((200, 200, 3), PAPER, dtype=np.uint8)
    x0, y0, x1, y1 = bird_box
    arr[y0:y1, x0:x1] = (30, 30, 40)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


class AutogenTest(unittest.TestCase):
    def test_station_paths_follow_the_checkout(self) -> None:
        self.assertEqual(MODULE.HERE, SCRIPT.parents[2] / "avian" / "scripts")
        self.assertEqual(MODULE.ILL, SCRIPT.parents[2] / "avian" / "assets" / "illustrations")

    def test_framed_bird_passes_edge_check(self) -> None:
        self.assertLess(MODULE.edge_ink(render((40, 40, 160, 160))), MODULE.EDGE_OK)

    def test_bird_running_off_the_frame_is_rejected(self) -> None:
        self.assertGreater(MODULE.edge_ink(render((40, 40, 200, 160))), MODULE.EDGE_OK)

    def test_missing_species_lists_only_absent_poses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            db = tmp / "birds.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE detections (Sci_Name TEXT, Com_Name TEXT)")
            con.executemany("INSERT INTO detections VALUES (?, ?)", [
                ("Melanerpes candidus", "Carpintero Blanco"),
                ("Melanerpes candidus", "Carpintero Blanco"),
                ("Guira guira", "Pirincho"),
                ("Turdus rufiventris", "Zorzal Colorado"),
            ])
            con.commit()
            con.close()
            ill = tmp / "illustrations"
            ill.mkdir()
            (ill / "guira-guira.png").touch()                 # flight pose missing
            (ill / "turdus-rufiventris.png").touch()          # complete
            (ill / "turdus-rufiventris-2.png").touch()
            with mock.patch.object(MODULE, "DB", db), mock.patch.object(MODULE, "ILL", ill):
                todo = MODULE.missing_species()
        self.assertEqual(todo, [
            ("Melanerpes candidus", "Carpintero Blanco", [1, 2]),
            ("Guira guira", "Pirincho", [2]),
        ])

    def test_anti_reference_is_fetched_once_and_covers_pygochelidon(self) -> None:
        import sys
        sys.path.insert(0, str(MODULE.HERE))
        import pregen
        photo = io.BytesIO()
        Image.new("RGB", (20, 20), (180, 90, 40)).save(photo, "PNG")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(MODULE, "REFS", Path(tmp)), \
                mock.patch.object(pregen, "fetch_wikipedia_thumb", return_value=(photo.getvalue(), ".png")) as fetch:
            key, path = MODULE.anti_reference(pregen, "Pygochelidon cyanoleuca")
            self.assertEqual((key, path), ("barnswallow", Path(tmp) / "_anti_barnswallow.jpg"))
            self.assertEqual(Image.open(path).format, "JPEG")
            self.assertEqual(MODULE.anti_reference(pregen, "Tachycineta leucopyga"), (key, path))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args, ("Hirundo rustica", "Barn Swallow"))
            self.assertEqual(MODULE.anti_reference(pregen, "Turdus rufiventris"), (None, None))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["_anti_barnswallow.jpg"])

    def test_failed_anti_reference_fetch_renders_without_it(self) -> None:
        import sys
        sys.path.insert(0, str(MODULE.HERE))
        import pregen
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(MODULE, "REFS", Path(tmp)), \
                mock.patch.object(pregen, "fetch_wikipedia_thumb", return_value=None):
            self.assertEqual(MODULE.anti_reference(pregen, "Tachycineta leucorrhoa"), (None, None))

    def test_state_resets_each_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text('{"day": "2000-01-01", "made": ["X y"], "fails": {"X y": 3}}')
            with mock.patch.object(MODULE, "STATE", state):
                fresh = MODULE.load_state()
        self.assertEqual(fresh["made"], [])
        self.assertEqual(fresh["fails"], {})


if __name__ == "__main__":
    unittest.main()
