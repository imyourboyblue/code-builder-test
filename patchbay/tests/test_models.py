"""
The chat bar and Setup show the model Ollama really has loaded (its /api/ps), not a guess.
Uses a small fake Ollama on this computer. Run from the patchbay folder:

  python3 -m unittest discover -s tests -t .
"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

INSTALLED = ["qwen3.6:35b-a3b-nvfp4", "qwen3.6:27b"]


class FakeOllama(BaseHTTPRequestHandler):
    loaded = []
    ps_works = True

    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self.reply({"models": [{"name": n, "size": 20 * 10 ** 9} for n in INSTALLED]})
        elif self.path == "/api/ps" and FakeOllama.ps_works:
            self.reply({"models": [{"name": n, "size": 20 * 10 ** 9} for n in FakeOllama.loaded]})
        else:
            self.reply({"error": "not found"}, 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/show":
            self.reply({"capabilities": ["completion", "tools", "thinking"]})
        else:
            self.reply({})


class LoadedModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cfg = {"ollama_url": "http://127.0.0.1:%d" % cls.srv.server_port, "work_mode": "everyday"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def info(self, loaded, ps_works=True):
        FakeOllama.loaded, FakeOllama.ps_works = loaded, ps_works
        server._LOADED.clear()
        return server.modes_info(self.cfg)

    def test_shows_what_is_really_loaded(self):
        j = self.info(["qwen3.6:27b"])
        self.assertEqual(j["loaded"], ["qwen3.6:27b"])
        by = {m["key"]: m for m in j["modes"]}
        self.assertFalse(by["everyday"]["loaded"])
        self.assertTrue(by["building"]["loaded"])

    def test_nothing_loaded(self):
        j = self.info([])
        self.assertEqual(j["loaded"], [])
        self.assertFalse(any(m["loaded"] for m in j["modes"]))

    def test_ollama_cant_say(self):
        j = self.info(["qwen3.6:27b"], ps_works=False)
        self.assertIsNone(j["loaded"])
        self.assertFalse(any(m["loaded"] for m in j["modes"]))

    def test_short_memory_then_asks_again(self):
        self.info([])
        FakeOllama.loaded = ["qwen3.6:35b-a3b-nvfp4"]
        self.assertEqual(server.loaded_names(self.cfg), [])          # asked under 2 seconds ago
        server._LOADED.clear()                                        # what unloading or a mode switch does
        self.assertEqual(server.loaded_names(self.cfg), ["qwen3.6:35b-a3b-nvfp4"])


if __name__ == "__main__":
    unittest.main()
