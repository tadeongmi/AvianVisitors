import importlib.util
import json
import pathlib
import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("delete_recordings", ROOT / "scripts/delete_recordings.py")
deletion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deletion)


class RecordingDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.audio = self.root / "By_Date"
        self.audio.mkdir()
        self.db = self.root / "birds.db"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE detections (Date, Time, Sci_Name, Com_Name, File_Name)")

    def record(self, name="bird.wav", species="A b", common="Bird", date="2026-10-22", files=True):
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO detections VALUES (?, '12:00:00', ?, ?, ?)", (date, species, common, name))
        relative = f"{date}/{common.replace(' ', '_').replace(chr(39), '')}/{name}"
        if files:
            for prefix in ("", "shifted/"):
                path = self.audio / (prefix + relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
                pathlib.Path(str(path) + ".png").write_bytes(b"spectrogram")
        return relative

    def rows(self):
        with sqlite3.connect(self.db) as db:
            return db.execute("SELECT * FROM detections").fetchall()

    def delete(self, **request):
        return deletion.delete_recordings(self.db, self.audio, request)

    def test_single_deletes_audio_spectrogram_and_shifted_variants(self):
        path = self.record()
        other = self.record("keep.wav")
        self.assertEqual(self.delete(file=path), {"ok": True, "lines": 1, "files": 4})
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse((self.audio / path).exists())
        self.assertTrue((self.audio / other).exists())

    def test_species_keeps_other_species_and_untracked_files_in_same_directory(self):
        path = self.record()
        other = self.record("other.wav", species="C d")
        extra = self.audio / pathlib.Path(path).parent / "untracked.wav"
        extra.write_bytes(b"keep")
        self.assertEqual(self.delete(species="A b")["lines"], 1)
        self.assertTrue((self.audio / other).exists())
        self.assertEqual(extra.read_bytes(), b"keep")

    def test_request_paths_are_exact_database_selectors(self):
        path = self.record()
        for value in ("../birds.db", "/etc/passwd", path + ";touch hacked", path + "\x00", "2026-10-22/../bird.wav"):
            with self.subTest(value=value), self.assertRaises(deletion.DeleteError):
                self.delete(file=value)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue((self.audio / path).exists())

    def test_shell_characters_are_literal_when_present_in_database(self):
        path = self.record("bird;$(touch nope).wav", common="Bird & Friend")
        self.assertEqual(self.delete(file=path)["files"], 4)
        self.assertFalse((self.root / "nope").exists())

    def test_duplicate_filename_different_date_does_not_delete_wrong_row(self):
        path = self.record()
        other = self.record(date="2026-10-21")
        self.assertEqual(self.delete(file=path)["lines"], 1)
        self.assertTrue((self.audio / other).exists())

    def test_missing_audio_still_allows_removing_detection(self):
        path = self.record(files=False)
        self.assertEqual(self.delete(file=path), {"ok": True, "lines": 1, "files": 0})

    def test_invalid_database_path_fails_before_mutation(self):
        path = self.record()
        self.record("../../outside", files=False)
        with self.assertRaises(deletion.DeleteError):
            self.delete(species="A b")
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue((self.audio / path).exists())

    def test_symlinked_recording_parent_is_rejected(self):
        path = self.record(files=False)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "bird.wav").write_bytes(b"keep")
        (self.audio / "2026-10-22").mkdir()
        (self.audio / "2026-10-22/Bird").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(deletion.DeleteError):
            self.delete(file=path)
        self.assertEqual((outside / "bird.wav").read_bytes(), b"keep")
        self.assertEqual(len(self.rows()), 1)

    def test_symlinked_file_is_rejected(self):
        path = self.record()
        outside = self.root / "outside.wav"
        outside.write_bytes(b"keep")
        (self.audio / path).unlink()
        (self.audio / path).symlink_to(outside)
        with self.assertRaises(deletion.DeleteError):
            self.delete(file=path)
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual(len(self.rows()), 1)

    def test_configured_root_can_be_a_symlink(self):
        path = self.record()
        link = self.root / "configured-root"
        link.symlink_to(self.audio, target_is_directory=True)
        self.assertEqual(deletion.delete_recordings(self.db, link, {"file": path})["lines"], 1)

    def test_ambiguous_record_is_rejected(self):
        path = self.record()
        self.record(files=False)
        with self.assertRaises(deletion.DeleteError):
            self.delete(file=path)
        self.assertEqual(len(self.rows()), 2)

    def test_species_does_not_remove_files_referenced_by_another_species(self):
        path = self.record()
        self.record(species="C d", files=False)
        with self.assertRaises(deletion.DeleteError):
            self.delete(species="A b")
        self.assertTrue((self.audio / path).exists())
        self.assertEqual(len(self.rows()), 2)

    def test_database_failure_restores_files(self):
        path = self.record()
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TRIGGER refuse BEFORE DELETE ON detections BEGIN SELECT RAISE(ABORT, 'refused'); END")
        with self.assertRaises(deletion.DeleteError):
            self.delete(file=path)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((self.audio / path).read_bytes(), b"audio")
        self.assertEqual(len(list(self.audio.rglob("*.png"))), 2)

    def test_second_move_failure_restores_first_file(self):
        path = self.record()
        rename = deletion.os.rename
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("test filesystem failure")
            return rename(*args, **kwargs)

        with patch.object(deletion.os, "rename", side_effect=fail_second):
            with self.assertRaises(deletion.DeleteError):
                self.delete(file=path)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((self.audio / path).read_bytes(), b"audio")
        self.assertEqual(len(list(self.audio.rglob("*.png"))), 2)

    def test_invalid_request_shapes_fail_closed(self):
        self.record()
        for request in ({}, {"file": []}, {"species": ""}, {"species": "A b", "file": "x"}, {"species": "A b", "extra": True}):
            with self.subTest(request=request), self.assertRaises(deletion.DeleteError):
                self.delete(**request)
        self.assertEqual(len(self.rows()), 1)

    def test_interrupted_move_is_recovered_before_next_deletion(self):
        path = self.record()
        rename = deletion.os.rename

        def interrupted(*args, **kwargs):
            rename(*args, **kwargs)
            if args[0] != "paths.tmp":
                raise SystemExit("interrupted before database commit")

        with patch.object(deletion.os, "rename", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.delete(file=path)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.delete(file=path)["files"], 4)

    def test_interrupted_cleanup_finishes_on_next_deletion(self):
        path = self.record()
        with patch.object(deletion.os, "unlink", side_effect=SystemExit("interrupted after commit")):
            with self.assertRaises(SystemExit):
                self.delete(file=path)
        other = self.record("other.wav")
        self.assertEqual(self.delete(file=other)["files"], 4)
        self.assertFalse(list(self.audio.glob(".avian-delete-*")))

    def test_replaced_quarantine_path_cannot_delete_outside_files(self):
        path = self.record()
        outside = self.root / "outside"
        outside.mkdir()
        for name in ("0", "1", "2", "3", "paths.json"):
            (outside / name).write_bytes(b"keep")
        rename = deletion.os.rename
        replaced = False

        def replace_quarantine(*args, **kwargs):
            nonlocal replaced
            result = rename(*args, **kwargs)
            if not replaced and args[0] != "paths.tmp":
                replaced = True
                queue = next(self.audio.glob(".avian-delete-*"))
                rename(queue, self.audio / "moved-quarantine")
                queue.symlink_to(outside, target_is_directory=True)
            return result

        with patch.object(deletion.os, "rename", side_effect=replace_quarantine):
            try:
                self.delete(file=path)
            except deletion.DeleteError:
                pass
        for name in ("0", "1", "2", "3", "paths.json"):
            self.assertEqual((outside / name).read_bytes(), b"keep")

    def test_shared_path_check_does_not_query_once_per_detection(self):
        with sqlite3.connect(self.db) as db:
            db.executemany("INSERT INTO detections VALUES ('2026-10-22', '12:00:00', 'A b', 'Bird', ?)", [(f"bird{i}.wav",) for i in range(1000)])
        connect = sqlite3.connect
        statements = []

        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch.object(deletion.sqlite3, "connect", side_effect=traced):
            self.assertEqual(self.delete(species="A b")["lines"], 1000)
        self.assertLessEqual(sum(statement.startswith("SELECT") for statement in statements), 3)

    def test_untrusted_recovery_map_cannot_escape_root(self):
        path = self.record()
        outside = self.root / "outside"
        outside.write_bytes(b"keep")
        queue = self.audio / (".avian-delete-" + "a" * 32)
        queue.mkdir(mode=0o700)
        (queue / "0").write_bytes(b"staged")
        for value in (["../../outside"], [{}], ["2026-10-22/Bird/bird.wav"] * 2, "invalid"):
            (queue / "paths.json").write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(deletion.DeleteError):
                self.delete(file=path)
            self.assertEqual(outside.read_bytes(), b"keep")
            self.assertEqual((queue / "0").read_bytes(), b"staged")

    def test_interrupted_manifest_write_without_staged_files_is_recoverable(self):
        path = self.record()
        queue = self.audio / (".avian-delete-" + "b" * 32)
        queue.mkdir(mode=0o700)
        (queue / "paths.json").write_text("[")
        self.assertEqual(self.delete(file=path)["files"], 4)
        self.assertFalse(queue.exists())

    def test_incomplete_temporary_manifest_is_recoverable(self):
        path = self.record()
        queue = self.audio / (".avian-delete-" + "b" * 32)
        queue.mkdir(mode=0o700)
        (queue / "paths.tmp").write_text("[")
        self.assertEqual(self.delete(file=path)["files"], 4)
        self.assertFalse(queue.exists())

    def interrupted_stage(self, path):
        rename = deletion.os.rename

        def interrupted(*args, **kwargs):
            rename(*args, **kwargs)
            if args[0] != "paths.tmp":
                raise SystemExit("interrupted")

        with patch.object(deletion.os, "rename", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.delete(file=path)

    def test_recovery_does_not_overwrite_new_recording(self):
        path = self.record()
        self.interrupted_stage(path)
        move = deletion.move_no_replace

        def create_new_recording(*args, **kwargs):
            (self.audio / path).write_bytes(b"new recording")
            return move(*args, **kwargs)

        with patch.object(deletion, "move_no_replace", side_effect=create_new_recording):
            with self.assertRaises(deletion.DeleteError):
                self.delete(file=path)
        self.assertEqual((self.audio / path).read_bytes(), b"new recording")
        self.assertEqual(len(self.rows()), 1)

    def test_recovery_can_resume_after_restoring_one_file(self):
        path = self.record()
        self.interrupted_stage(path)
        move = deletion.move_no_replace

        def interrupted(*args, **kwargs):
            move(*args, **kwargs)
            raise SystemExit("interrupted during restore")

        with patch.object(deletion, "move_no_replace", side_effect=interrupted):
            with self.assertRaises(SystemExit):
                self.delete(file=path)
        self.assertEqual(self.delete(file=path)["files"], 4)

    def test_manifest_limit_is_checked_before_moving_files(self):
        path = self.record()
        with patch.object(deletion, "MAX_MANIFEST_BYTES", 1, create=True):
            with self.assertRaises(deletion.DeleteError):
                self.delete(file=path)
        self.assertEqual((self.audio / path).read_bytes(), b"audio")
        self.assertEqual(len(self.rows()), 1)

    def test_resolved_storage_ancestor_cannot_be_replaced_with_symlink(self):
        holder = self.root / "holder"
        holder.mkdir()
        self.audio = holder / "By_Date"
        self.audio.mkdir()
        path = self.record()
        outside = self.root / "outside"
        target = outside / "By_Date" / path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"keep")
        open_file = deletion.os.open
        replaced = False

        def replace_ancestor(name, *args, **kwargs):
            nonlocal replaced
            if not replaced and str(name) in (str(self.audio), "holder"):
                replaced = True
                holder.rename(self.root / "original-holder")
                holder.symlink_to(outside, target_is_directory=True)
            return open_file(name, *args, **kwargs)

        with patch.object(deletion.os, "open", side_effect=replace_ancestor):
            with self.assertRaises((OSError, deletion.DeleteError)):
                self.delete(file=path)
        self.assertTrue(replaced)
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertEqual(len(self.rows()), 1)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_frontend_requests_and_failures(self):
        subprocess.run(["node", "--test", "tests/recording_delete.test.js"], cwd=ROOT, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
