"""Run the refresh migration as root only in a disposable Linux container."""

import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REFRESH = ROOT / "scripts/security_refresh.sh"
STATION_UID = 12345
STATION_GID = 12346


@unittest.skipUnless(
    os.geteuid() == 0 and Path("/.dockerenv").is_file(),
    "requires root in a disposable Linux container",
)
class ShiftedDirectoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="avian-shifted-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "station"
        self.by_date = self.home / "BirdSongs/Extracted/By_Date"
        self.shifted = self.by_date / "shifted"
        self.shifted.mkdir(parents=True)
        for path in (self.home, self.home / "BirdSongs", self.by_date.parent, self.by_date):
            os.chown(path, STATION_UID, STATION_GID)

    def migration_function(self):
        return "\n".join(re.findall(
            r"^normalize_legacy_shifted_dirs\(\) \{\n.*?^\}",
            REFRESH.read_text(), re.M | re.S))

    def migrate(self, home=None, uid=STATION_UID, gid=STATION_GID, run_uid=None, cwd=None, env=None):
        return subprocess.run(
            ["bash", "-c", self.migration_function() + '\nnormalize_legacy_shifted_dirs "$@"',
             "migration-test", str(home or self.home), str(uid), str(gid)],
            text=True, capture_output=True, timeout=10,
            cwd=cwd, env=env,
            preexec_fn=(lambda: os.setuid(run_uid)) if run_uid is not None else None,
        )

    def migrate_with_open(self, open_directory):
        code = self.migration_function().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with patch.object(sys, "argv", ["migration", str(self.home), str(STATION_UID), str(STATION_GID)]):
            with patch.object(os, "open", side_effect=open_directory):
                exec(compile(code, str(REFRESH), "exec"), {})

    def assert_owner(self, path, uid, gid):
        info = path.lstat()
        self.assertEqual((info.st_uid, info.st_gid), (uid, gid), str(path))

    def test_root_owned_directories_become_station_owned_without_changing_files_or_modes(self):
        directory = self.shifted / "2026-10-22/Bird"
        directory.mkdir(parents=True)
        directory.chmod(0o755)
        recording = directory / "bird.wav"
        recording.write_bytes(b"original shifted audio")
        recording.chmod(0o640)
        before = recording.stat()
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (self.shifted, directory.parent, directory):
            self.assert_owner(path, STATION_UID, STATION_GID)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
        after = recording.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode, after.st_ino),
                         (before.st_uid, before.st_gid, before.st_mode, before.st_ino))
        self.assertEqual(recording.read_bytes(), b"original shifted audio")

    def test_nonroot_directories_are_unchanged_but_root_owned_children_are_migrated(self):
        directory = self.shifted / "station-owned"
        nested = directory / "old-root-owned"
        nested.mkdir(parents=True)
        os.chown(self.shifted, STATION_UID, STATION_GID)
        os.chown(directory, 23456, 23457)
        directory.chmod(0o750)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner(directory, 23456, 23457)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)
        self.assert_owner(nested, STATION_UID, STATION_GID)

    def test_other_recording_paths_are_not_migrated(self):
        paths = [self.by_date / "2026-10-22/Bird", self.home / "BirdSongs/Processed",
                 self.home / "BirdSongs/Extracted/Charts", self.base / "other-station/shifted"]
        for path in paths:
            path.mkdir(parents=True)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in paths:
            self.assert_owner(path, 0, 0)
        self.assert_owner(self.by_date, STATION_UID, STATION_GID)

    def test_symlinked_storage_root_is_supported(self):
        self.shifted.rmdir()
        self.by_date.rmdir()
        storage = self.base / "external-storage"
        directory = storage / "shifted/2026-10-22/Bird"
        directory.mkdir(parents=True)
        self.by_date.symlink_to(storage, target_is_directory=True)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner(directory, STATION_UID, STATION_GID)
        self.assert_owner(storage, 0, 0)
        self.assertTrue(self.by_date.is_symlink())

    def test_shifted_symlink_is_not_followed(self):
        self.shifted.rmdir()
        outside = self.base / "outside"
        (outside / "nested").mkdir(parents=True)
        self.shifted.symlink_to(outside, target_is_directory=True)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner(outside, 0, 0)
        self.assert_owner(outside / "nested", 0, 0)
        self.assert_owner(self.shifted, 0, 0)

    def test_descendant_and_file_symlinks_are_not_followed(self):
        outside = self.base / "outside"
        (outside / "nested").mkdir(parents=True)
        recording = outside / "bird.wav"
        recording.write_bytes(b"keep")
        (self.shifted / "linked-dir").symlink_to(outside, target_is_directory=True)
        (self.shifted / "linked-file").symlink_to(recording)
        (self.shifted / "broken-link").symlink_to(outside / "missing")
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (outside, outside / "nested", recording, self.shifted / "linked-dir",
                     self.shifted / "linked-file", self.shifted / "broken-link"):
            self.assert_owner(path, 0, 0)
        self.assertEqual(recording.read_bytes(), b"keep")

    def test_absent_tree_is_not_created(self):
        self.shifted.rmdir()
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.shifted.exists())
        missing_home = self.base / "missing-home"
        result = self.migrate(home=missing_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(missing_home.exists())

    def test_second_refresh_is_idempotent(self):
        directory = self.shifted / "2026-10-22/Bird"
        directory.mkdir(parents=True)
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        before = directory.stat()
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = directory.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode, after.st_ctime_ns),
                         (before.st_uid, before.st_gid, before.st_mode, before.st_ctime_ns))

    def test_directory_replaced_with_symlink_before_open_does_not_escape(self):
        victim = self.shifted / "victim"
        victim.mkdir()
        outside = self.base / "outside"
        (outside / "nested").mkdir(parents=True)
        displaced = self.base / "displaced"
        real_open = os.open

        def replace_before_open(path, flags, **kwargs):
            if path == "victim":
                victim.rename(displaced)
                victim.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, **kwargs)

        self.migrate_with_open(replace_before_open)
        self.assert_owner(self.shifted, STATION_UID, STATION_GID)
        for path in (displaced, outside, outside / "nested", victim):
            self.assert_owner(path, 0, 0)

    def test_resolved_root_component_replaced_with_symlink_fails_closed(self):
        outside = self.base / "outside"
        (outside / "shifted").mkdir(parents=True)
        displaced = self.base / "displaced"
        real_open = os.open

        def replace_before_open(path, flags, **kwargs):
            if path == "By_Date":
                self.by_date.rename(displaced)
                self.by_date.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, **kwargs)

        with self.assertRaises(OSError):
            self.migrate_with_open(replace_before_open)
        for path in (outside, outside / "shifted", displaced / "shifted"):
            self.assert_owner(path, 0, 0)

    def test_unknown_file_types_are_untouched(self):
        fifo = self.shifted / "pipe"
        os.mkfifo(fifo)
        before = fifo.stat()
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = fifo.stat()
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode, after.st_ino),
                         (before.st_uid, before.st_gid, before.st_mode, before.st_ino))

    def test_root_privilege_and_nonroot_destination_are_required(self):
        for arguments in ({"run_uid": STATION_UID}, {"uid": 0}, {"gid": 0}):
            with self.subTest(arguments=arguments):
                result = self.migrate(**arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assert_owner(self.shifted, 0, 0)

    def test_station_can_stage_a_root_owned_file_after_directory_migration(self):
        self.base.chmod(0o755)
        directory = self.shifted / "2026-10-22/Bird"
        directory.mkdir(parents=True)
        recording = directory / "bird.wav"
        recording.write_bytes(b"root-owned shifted audio")
        destination = self.by_date / "staged.wav"

        def station_identity():
            os.setgid(STATION_GID)
            os.setuid(STATION_UID)

        def stage():
            return subprocess.run(
                [sys.executable, "-c", "import os, sys; os.rename(sys.argv[1], sys.argv[2])",
                 str(recording), str(destination)],
                text=True, capture_output=True, preexec_fn=station_identity, timeout=10,
            )

        self.assertNotEqual(stage().returncode, 0)
        self.assertTrue(recording.exists())
        result = self.migrate()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner(destination, 0, 0)
        self.assertEqual(destination.read_bytes(), b"root-owned shifted audio")

    def test_inherited_working_directory_and_pythonpath_cannot_supply_root_modules(self):
        poisoned = self.base / "poisoned"
        poisoned.mkdir()
        marker = self.base / "imported-untrusted-pathlib"
        (poisoned / "pathlib.py").write_text(
            f"open({str(marker)!r}, 'w').write('untrusted module executed')\n"
            "raise RuntimeError('untrusted pathlib')\n"
        )
        for arguments in ({"cwd": poisoned}, {"env": {**os.environ, "PYTHONPATH": str(poisoned)}}):
            with self.subTest(arguments=arguments):
                marker.unlink(missing_ok=True)
                result = self.migrate(**arguments)
                self.assertFalse(marker.exists(), "root imported a module from an untrusted path")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_owner(self.shifted, STATION_UID, STATION_GID)


if __name__ == "__main__":
    unittest.main()
