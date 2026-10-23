#!/usr/bin/env python3
"""pregen.gen_one routed through OpenRouter, against a local stand-in server."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "avian" / "scripts"))
import pregen  # noqa: E402

SPEC = importlib.util.spec_from_file_location("openrouter", ROOT / "avian" / "autogen" / "openrouter.py")
assert SPEC and SPEC.loader
OPENROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPENROUTER)

PNG = b"\x89PNG\r\n\x1a\n rendered bird"


class StandIn(BaseHTTPRequestHandler):
    status = 200
    field = "image_url"
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.status != 200:
            self.send_response(self.status)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"credits"}}')
            return
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        reply = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "", "images": [{"type": "image_url", self.field: {"url": url}}]}}]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"passthrough")

    def log_message(self, *args) -> None:
        pass


class OpenRouterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), StandIn)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        StandIn.status, StandIn.field, StandIn.seen = 200, "image_url", []
        self.tmp = tempfile.TemporaryDirectory()
        self.ref = Path(self.tmp.name) / "ref.png"
        from PIL import Image
        Image.new("RGB", (8, 8), (200, 120, 40)).save(self.ref)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def render(self) -> bytes:
        with mock.patch.object(OPENROUTER, "URL", self.base + "/api/v1/chat/completions"), \
                OPENROUTER.routed(pregen.GEMINI_URL, "sk-or-test", "google/gemini-2.5-flash-image"):
            return pregen.gen_one("", "Paint {sci_name} ({com_name}), {pose}.", "Machetornis rixosa",
                                  "Picabuey", 1, positive_ref=self.ref, species_note="Yellow belly.",
                                  style_ref=self.ref)

    def test_request_keeps_every_part_in_order_and_image_comes_back(self) -> None:
        self.assertEqual(self.render(), PNG)
        (call,) = StandIn.seen
        self.assertEqual(call["path"], "/api/v1/chat/completions")
        self.assertEqual(call["auth"], "Bearer sk-or-test")
        body = call["body"]
        self.assertEqual((body["model"], body["modalities"]), ("google/gemini-2.5-flash-image", ["image", "text"]))
        kinds = [p["type"] for p in body["messages"][0]["content"]]
        self.assertEqual(kinds, ["text", "text", "image_url", "text", "image_url"])
        first = body["messages"][0]["content"][0]["text"]
        self.assertIn("Paint Machetornis rixosa (Picabuey), perched.", first)
        self.assertIn("Species-specific note: Yellow belly.", first)
        self.assertTrue(body["messages"][0]["content"][2]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_camel_case_image_field_is_read_too(self) -> None:
        StandIn.field = "imageUrl"
        self.assertEqual(self.render(), PNG)

    def test_empty_balance_surfaces_as_http_402(self) -> None:
        StandIn.status = 402
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.render()
        self.assertEqual(raised.exception.code, 402)

    def test_other_requests_pass_through_and_urlopen_is_restored(self) -> None:
        real = urllib.request.urlopen
        with OPENROUTER.routed(pregen.GEMINI_URL, "sk-or-test"):
            with urllib.request.urlopen(self.base + "/wiki") as r:
                self.assertEqual(r.read(), b"passthrough")
        self.assertIs(urllib.request.urlopen, real)
        self.assertEqual(StandIn.seen, [])


if __name__ == "__main__":
    unittest.main()
