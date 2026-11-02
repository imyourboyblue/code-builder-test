"""
PatchBay uses the model you loaded in Ollama (its /api/ps), never a guess.
Uses a small fake Ollama on this computer. Run from the patchbay folder:

  python3 -m unittest discover -s tests -t .
"""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_labels  # noqa: E402
import server  # noqa: E402

INSTALLED = ["qwen3.6:35b-a3b-nvfp4", "qwen3.6:27b", "gemma4:26b"]


class FakeOllama(BaseHTTPRequestHandler):
    loaded = []          # [(name, expires_at)]
    ps_works = True
    chats = []           # bodies of /api/chat requests

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
            self.reply({"models": [{"name": n, "size": 20 * 10 ** 9, "expires_at": e} for n, e in FakeOllama.loaded]})
        else:
            self.reply({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/show":
            self.reply({"capabilities": ["completion", "tools", "thinking"]})
        else:
            if self.path == "/api/chat":
                FakeOllama.chats.append(body)
            self.reply({})


class LoadedModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cfg = {"ollama_url": "http://127.0.0.1:%d" % cls.srv.server_port}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        server._LOADED.clear()
        server._PINNED.clear()
        server._FREED[0] = 0
        FakeOllama.chats, FakeOllama.ps_works = [], True

    def info(self, loaded):
        FakeOllama.loaded = loaded
        server._LOADED.clear()
        return server.models_info(self.cfg)

    def test_uses_what_is_loaded(self):
        j = self.info([("qwen3.6:27b", "2318-01-01T00:00:00Z")])
        self.assertEqual((j["model"], j["loaded"]), ("qwen3.6:27b", ["qwen3.6:27b"]))
        self.assertEqual(server.current_model(self.cfg), "qwen3.6:27b")

    def test_nothing_loaded_says_so(self):
        j = self.info([])
        self.assertIsNone(j["model"])
        self.assertIn("ollama run qwen3.6:35b-a3b-nvfp4", j["no_model"])
        with self.assertRaises(server.ToolError) as e:
            server.current_model(self.cfg)
        self.assertIn("No model is loaded", str(e.exception))

    def test_most_recently_used_wins(self):
        j = self.info([("qwen3.6:27b", "2026-11-02T10:00:00Z"), ("qwen3.6:35b-a3b-nvfp4", "2026-11-02T10:05:00Z")])
        self.assertEqual(j["model"], "qwen3.6:35b-a3b-nvfp4")

    def test_second_opinion_model_is_not_the_main_one(self):
        j = self.info([("gemma4:26b", "2318-01-02T00:00:00Z"), ("qwen3.6:27b", "2318-01-01T00:00:00Z")])
        self.assertEqual(j["model"], "qwen3.6:27b")
        self.assertEqual(self.info([("gemma4:26b", "x")])["model"], "gemma4:26b")   # unless it's the only one

    def test_ollama_cant_say(self):
        FakeOllama.ps_works = False
        j = self.info([("qwen3.6:27b", "x")])
        self.assertIsNone(j["loaded"])
        self.assertIsNone(j["model"])
        with self.assertRaises(server.ToolError):
            server.current_model(self.cfg)

    def wait_for_chats(self, n):
        end = time.time() + 3
        while len(FakeOllama.chats) < n and time.time() < end:
            time.sleep(0.02)

    def test_loaded_model_is_kept_loaded(self):
        self.info([("qwen3.6:27b", "2026-11-02T10:05:00Z")])
        self.wait_for_chats(1)
        self.assertEqual(FakeOllama.chats[0]["model"], "qwen3.6:27b")
        self.assertEqual(FakeOllama.chats[0]["keep_alive"], -1)
        self.info([("qwen3.6:27b", "2318-01-01T00:00:00Z")])      # only once
        time.sleep(0.2)
        self.assertEqual(len(FakeOllama.chats), 1)

    def test_free_up_memory_isnt_undone(self):
        FakeOllama.loaded = [("qwen3.6:27b", "x")]
        server.unload_models(self.cfg)
        FakeOllama.chats = []
        self.info([("qwen3.6:27b", "x")])                        # still on its way out
        time.sleep(0.2)
        self.assertEqual(FakeOllama.chats, [])

    def test_short_memory_then_asks_again(self):
        self.info([])
        FakeOllama.loaded = [("qwen3.6:35b-a3b-nvfp4", "x")]
        self.assertEqual(server.loaded_names(self.cfg), [])          # asked under 2 seconds ago
        server._LOADED.clear()
        self.assertEqual(server.loaded_names(self.cfg), ["qwen3.6:35b-a3b-nvfp4"])

    def test_active_ignores_search_helpers(self):
        self.assertEqual(model_labels.active(["nomic-embed-text", "qwen3.6:27b"]), "qwen3.6:27b")
        self.assertIsNone(model_labels.active([]))
        self.assertIsNone(model_labels.active(None))


if __name__ == "__main__":
    unittest.main()
