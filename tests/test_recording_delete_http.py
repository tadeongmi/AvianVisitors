"""Run only in an explicitly opted-in, disposable Linux container."""

import base64
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
ENABLED = Path("/.dockerenv").exists() and os.environ.get("AVIAN_DELETE_HTTP_TEST") == "1" and os.geteuid() == 0


@unittest.skipUnless(ENABLED, "requires an opted-in disposable container")
class RecordingDeleteHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user = "aviandeletion"
        subprocess.run(["useradd", "--create-home", cls.user], check=True)
        cls.uid = pwd.getpwnam(cls.user).pw_uid
        cls.gid = pwd.getpwnam(cls.user).pw_gid
        cls.home = Path("/home") / cls.user
        cls.repo = cls.home / "BirdNET-Pi"
        cls.audio = cls.home / "BirdSongs/Extracted/By_Date"
        cls.audio.mkdir(parents=True)
        for name in ("scripts", "avian/api"):
            (cls.repo / name).mkdir(parents=True, exist_ok=True)
        for name in ("scripts/play.php", "scripts/species_tools.php", "scripts/common.php", "scripts/recording_delete.php", "scripts/delete_recordings.py", "avian/api/admin-auth.php", "avian/api/admin-state.php", "avian/api/educator-state.php"):
            shutil.copyfile(ROOT / name, cls.repo / name)
        (cls.repo / "play.php").symlink_to("scripts/play.php")
        Path("/etc/birdnet").mkdir(exist_ok=True)
        Path("/etc/birdnet/birdnet.conf").write_text(f"BIRDNET_USER={cls.user}\n")
        cls.state_dir = Path("/var/lib/avian-visitors")
        cls.state_dir.mkdir(exist_ok=True)
        cls.state_dir.chmod(0o755)
        os.chown(cls.state_dir, 0, 0)
        cls.verifier = subprocess.check_output(["php", "-r", "echo password_hash('TestPassword12', PASSWORD_BCRYPT, ['cost' => 14]);"], text=True)
        cls.state = cls.state_dir / "admin-auth.state"
        cls.set_policy(0)
        shutil.copyfile(ROOT / "scripts/admin_control.sh", "/usr/local/sbin/avian-admin-control")
        Path("/usr/local/sbin/avian-admin-control").chmod(0o755)
        policy = Path("/etc/sudoers.d/avian-deletion-test")
        policy.write_text("caddy ALL=(root) NOPASSWD: /usr/local/sbin/avian-admin-control *\n")
        policy.chmod(0o440)
        cls.db = cls.repo / "scripts/birds.db"
        with sqlite3.connect(cls.db) as db:
            db.execute("CREATE TABLE detections (Date, Time, Sci_Name, Com_Name, File_Name)")
        for directory, dirs, files in os.walk(cls.home):
            os.chown(directory, cls.uid, cls.gid)
            for name in files:
                os.chown(Path(directory) / name, cls.uid, cls.gid)
        cls.home.chmod(0o755)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = subprocess.Popen(["runuser", "-u", "caddy", "--", "php", "-S", f"127.0.0.1:{port}", "-t", str(cls.repo)], cwd=cls.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_server)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("PHP server failed to start")

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)

    @classmethod
    def set_policy(cls, required):
        cls.state.write_text(f"v1\t{required}\t1\t{cls.verifier}\n")
        cls.state.chmod(0o640)
        os.chown(cls.state, 0, grp.getgrnam("caddy").gr_gid)

    def setUp(self):
        self.set_policy(0)
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM detections")
            db.execute("INSERT INTO detections VALUES ('2026-10-22', '12:00:00', 'A b', 'Bird', 'bird.wav')")
        self.relative = "2026-10-22/Bird/bird.wav"
        self.file = self.audio / self.relative
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_bytes(b"audio")
        for item in (self.file, self.file.parent, self.file.parent.parent):
            os.chown(item, self.uid, self.gid)
        self.file.parent.chmod(0o755)

    def request(self, endpoint="/play.php?deletefile=1", body=None, method="POST", authenticated=True, headers=None):
        request_headers = {"Content-Type": "application/json", "X-Avian-Action": "1"}
        if authenticated:
            request_headers["Authorization"] = "Basic " + base64.b64encode(b"birdnet:TestPassword12").decode()
        if headers:
            request_headers.update(headers)
        data = json.dumps(body if body is not None else {"file": self.relative}).encode()
        request = urllib.request.Request(self.base + endpoint, data=data if method == "POST" else None, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read().decode()

    def assert_preserved(self):
        self.assertEqual(self.file.read_bytes(), b"audio")
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM detections").fetchone()[0], 1)

    def test_authenticated_post_reaches_station_user_helper(self):
        status, body = self.request()
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"ok": True, "lines": 1, "files": 1})
        self.assertFalse(self.file.exists())

    def test_species_authenticated_post(self):
        status, body = self.request("/scripts/species_tools.php?delete=1", {"species": "A b"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["lines"], 1)

    def test_unauthenticated_both_endpoints_fail_closed(self):
        for endpoint in ("/play.php?deletefile=1", "/scripts/species_tools.php?delete=1"):
            self.assertEqual(self.request(endpoint, authenticated=False)[0], 401)
            self.assert_preserved()

    def test_authenticated_get_cannot_delete(self):
        for endpoint in ("/play.php?deletefile=" + self.relative, "/scripts/species_tools.php?delete=A%20b"):
            self.assertEqual(self.request(endpoint, method="GET")[0], 405)
            self.assert_preserved()

    def test_form_post_and_missing_action_header_cannot_delete(self):
        for headers, status in (({"Content-Type": "application/x-www-form-urlencoded"}, 415), ({"X-Avian-Action": ""}, 403)):
            self.assertEqual(self.request(headers=headers)[0], status)
            self.assert_preserved()

    def test_invalid_selection_and_oversized_request(self):
        for body, expected in (({"file": []}, 400), ({"species": "A b"}, 400), ({"file": "../birds.db"}, 409), ({"file": "x" * 5000}, 413)):
            status, result = self.request(body=body)
            self.assertEqual(status, expected, result)
            self.assert_preserved()

    def test_lan_auth_required_keeps_legacy_mutations_closed(self):
        self.set_policy(1)
        self.assertEqual(self.request()[0], 401)
        self.assert_preserved()

    def test_helper_rejects_extra_arguments_and_root_execution(self):
        response = subprocess.run(["/usr/local/sbin/avian-admin-control", "delete-recordings", "/etc/passwd"], capture_output=True, text=True)
        self.assertNotEqual(response.returncode, 0)
        response = subprocess.run(["python3", str(self.repo / "scripts/delete_recordings.py"), str(self.db), str(self.audio)], input=json.dumps({"file": self.relative}), capture_output=True, text=True)
        self.assertNotEqual(response.returncode, 0)
        self.assert_preserved()

    def test_root_owned_shifted_file_is_restored_after_database_failure(self):
        shifted = self.audio / "shifted" / self.relative
        shifted.parent.mkdir(parents=True, exist_ok=True)
        for parent in (shifted.parent, shifted.parent.parent, shifted.parent.parent.parent):
            os.chown(parent, self.uid, self.gid)
        shifted.write_bytes(b"root-owned shifted audio")
        os.chown(shifted, 0, 0)
        shifted.chmod(0o644)
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TRIGGER refuse BEFORE DELETE ON detections BEGIN SELECT RAISE(ABORT, 'refused'); END")
        try:
            status, body = self.request()
            self.assertEqual(status, 409, body)
            self.assert_preserved()
            self.assertEqual(shifted.read_bytes(), b"root-owned shifted audio")
            self.assertEqual(shifted.stat().st_uid, 0)
        finally:
            with sqlite3.connect(self.db) as db:
                db.execute("DROP TRIGGER refuse")
            shifted.unlink()
