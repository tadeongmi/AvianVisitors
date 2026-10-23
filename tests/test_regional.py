#!/usr/bin/env python3
"""Checks for the regional bundle review and export tool."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGIONAL = load("regional", ROOT / "avian" / "regional" / "regional.py")
AUTOGEN = load("autogen_for_regional", ROOT / "avian" / "autogen" / "autogen.py")
CONFIG = json.loads((ROOT / "avian" / "regional" / "bundle.json").read_text())


class Station:
    """A throwaway illustration library, catalog and detections DB."""

    def __init__(self, tmp: Path) -> None:
        self.ill = tmp / "illustrations"
        self.ill.mkdir()
        self.catalog = tmp / "catalog.json"
        self.catalog.write_text(json.dumps({"packs": [
            {"id": "official", "availability": "included", "species": ["Turdus migratorius"]},
            {"id": "other", "availability": "installable", "species": ["Furnarius rufus"]},
        ]}))
        self.db = tmp / "birds.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE detections (Sci_Name TEXT, Com_Name TEXT)")
        con.executemany("INSERT INTO detections VALUES (?, ?)", [
            ("Furnarius rufus", "Hornero"), ("Turdus migratorius", "Zorzal Americano"),
            ("Guira guira", "Pirincho"), ("Pitangus sulphuratus", "Benteveo"),
        ])
        con.commit()
        con.close()
        for slug in ("turdus-migratorius", "furnarius-rufus", "pitangus-sulphuratus"):
            self.paint(slug, b"v1")
        (self.ill / "guira-guira.png").write_bytes(b"perched only")

    def paint(self, slug: str, version: bytes) -> None:
        (self.ill / f"{slug}.png").write_bytes(b"perched " + version)
        (self.ill / f"{slug}-2.png").write_bytes(b"flight " + version)

    def birds(self) -> list[dict]:
        return REGIONAL.local_species(self.ill, self.catalog, self.db)


class RegionalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.station = Station(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_only_complete_local_species_are_candidates(self) -> None:
        birds = self.station.birds()
        self.assertEqual([b["slug"] for b in birds], ["furnarius-rufus", "pitangus-sulphuratus"])
        self.assertEqual((birds[0]["sci"], birds[0]["com"]), ("Furnarius rufus", "Hornero"))

    def test_decisions_expire_when_the_images_change(self) -> None:
        curation = {"schema": 1, "species": {}}
        REGIONAL.decide(["furnarius-rufus"], "approved", birds=self.station.birds(), curation=curation)
        REGIONAL.decide(["pitangus-sulphuratus"], "excluded", birds=self.station.birds(), curation=curation)
        self.station.paint("furnarius-rufus", b"v2")
        self.station.paint("pitangus-sulphuratus", b"v2")
        status = {b["slug"]: REGIONAL.status_of(b, curation) for b in self.station.birds()}
        self.assertEqual(status, {"furnarius-rufus": "pending", "pitangus-sulphuratus": "excluded"})

    def test_unknown_species_cannot_be_decided(self) -> None:
        with self.assertRaises(SystemExit):
            REGIONAL.decide(["guira-guira"], "approved", birds=self.station.birds(),
                            curation={"schema": 1, "species": {}})

    def test_rejected_bird_is_queued_until_rerendered(self) -> None:
        curation = {"schema": 1, "species": {}}
        REGIONAL.decide(["pitangus-sulphuratus"], "rejected", "Yellow belly.",
                        birds=self.station.birds(), curation=curation)
        path = Path(self.tmp.name) / "curation.json"
        REGIONAL.save_curation(curation, path)
        with mock.patch.object(AUTOGEN, "CURATION", path), mock.patch.object(AUTOGEN, "ILL", self.station.ill):
            todo, notes = AUTOGEN.rejected_species()
            self.assertEqual(todo, [("Pitangus sulphuratus", "Benteveo", [1, 2])])
            self.assertEqual(notes, {"Pitangus sulphuratus": "Yellow belly."})
            self.station.paint("pitangus-sulphuratus", b"v2")
            self.assertEqual(AUTOGEN.rejected_species(), ([], {}))

    def test_sheet_groups_birds_and_escapes_names(self) -> None:
        from PIL import Image
        for slug in ("furnarius-rufus", "pitangus-sulphuratus"):
            for name in (f"{slug}.png", f"{slug}-2.png"):
                Image.new("RGBA", (40, 30), (120, 60, 20, 255)).save(self.station.ill / name)
        birds = self.station.birds()
        birds[0]["com"] = "<Hornero>"
        curation = {"schema": 1, "species": {}}
        REGIONAL.decide(["pitangus-sulphuratus"], "approved", birds=birds, curation=curation)
        page = REGIONAL.sheet_html(birds, curation, self.station.ill)
        self.assertIn("Pending review (1)", page)
        self.assertIn("Approved (1)", page)
        self.assertIn("&lt;Hornero&gt;", page)
        self.assertEqual(page.count("data:image/jpeg;base64,"), 4)

    def test_manifest_takes_the_bundle_config_and_keeps_the_archive_layout(self) -> None:
        src = Path(self.tmp.name) / "raw.zip"
        with zipfile.ZipFile(src, "w") as z:
            info = zipfile.ZipInfo("illustrations/furnarius-rufus.png", date_time=(1980, 1, 1, 0, 0, 0))
            z.writestr(info, b"png")
            z.writestr(zipfile.ZipInfo("manifest.json", date_time=(1980, 1, 1, 0, 0, 0)), json.dumps({
                "name": "Japanese Woodblock - Local Station", "species": [{"scientific_name": "Furnarius rufus"}],
                "style": {"id": "japanese-woodblock", "name": "Japanese Woodblock"},
                "license": {"file": "LICENSES/CC-BY-NC-SA-4.0.txt", "spdx": "CC-BY-NC-SA-4.0"},
                "coverage": {"label": "Local station illustration library", "region_codes": [], "type": "selection"},
                "attribution": {"creator": "Avian Visitors and station owner"},
                "provenance": {"method": "mixed", "model": "mixed", "review": "contributor-reviewed"},
            }))
        dst = Path(self.tmp.name) / "out.zip"
        REGIONAL.patch_manifest(src, dst, CONFIG)
        with zipfile.ZipFile(dst) as z:
            self.assertEqual([i.filename for i in z.infolist()],
                             ["illustrations/furnarius-rufus.png", "manifest.json"])
            self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) and i.compress_type == zipfile.ZIP_STORED
                                for i in z.infolist()))
            manifest = json.loads(z.read("manifest.json"))
        self.assertEqual(manifest["coverage"], {"label": "Río de la Plata", "region_codes": ["AR-B", "AR-C"],
                                                "type": "selection"})
        self.assertEqual(manifest["attribution"]["creator"], "tadeongmi")
        self.assertEqual(manifest["provenance"]["model"], "gemini-2.5-flash-image")
        claims = REGIONAL.claims(CONFIG, 1)
        sys_path = str(ROOT / "avian" / "scripts")
        with mock.patch("sys.path", [sys_path, *__import__("sys").path]):
            import bundle_submission_check
        bundle_submission_check.check_claims(dst, {"metadata": claims})


if __name__ == "__main__":
    unittest.main()
