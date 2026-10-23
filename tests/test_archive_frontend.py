import pathlib
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
class ArchiveFrontendTests(unittest.TestCase):
    def test_archive_schedule_controls(self):
        subprocess.run(["node", "tests/smoke_archive_toggle.mjs"], cwd=ROOT, check=True)
