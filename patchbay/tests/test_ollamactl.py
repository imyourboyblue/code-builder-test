"""Starting and stopping Ollama: which processes count as Ollama's, and what happens when PatchBay closes."""
import unittest
from unittest import mock

import ollamactl

ROWS = [
    (100, 1, "/Applications/Ollama.app/Contents/MacOS/Ollama"),
    (101, 100, "/Applications/Ollama.app/Contents/Resources/ollama serve"),
    (102, 101, "/Applications/Ollama.app/Contents/Resources/ollama runner --model /Users/blue/.ollama/models/blobs/sha256-ab --port 5000"),
    (103, 1, "/Applications/Ollama.app/Contents/Resources/llama-server --model /Users/blue/.ollama/models/blobs/sha256-cd"),
    (200, 1, "/opt/homebrew/bin/llama-server -m /Users/blue/models/other.gguf"),
    (300, 1, "/usr/bin/python3 patchbay.py --no-browser"),
    (301, 1, "/Users/blue/actions-runner/bin/Runner.Listener run"),
]


class PickTest(unittest.TestCase):
    def test_finds_ollama_and_leftovers(self):
        got = {p["pid"]: p for p in ollamactl.pick(ROWS)}
        self.assertEqual(sorted(got), [100, 101, 102, 103, 200])
        for pid in (100, 101, 102, 103):
            self.assertTrue(got[pid]["ollama"], pid)
        self.assertEqual(got[100]["what"], "the Ollama app")
        self.assertEqual(got[101]["what"], "Ollama's background server")
        self.assertTrue(got[102]["what"].startswith("a model in memory"))

    def test_other_llama_server_is_never_ollamas(self):
        got = {p["pid"]: p for p in ollamactl.pick(ROWS)}
        self.assertFalse(got[200]["ollama"])

    def test_skips_itself(self):
        self.assertNotIn(101, [p["pid"] for p in ollamactl.pick(ROWS, me=101)])


class CloseTest(unittest.TestCase):
    def run_close(self, mode, started):
        cfg = {"ollama_url": "http://localhost:11434", "ollama_on_close": mode}
        with mock.patch.object(ollamactl, "here", return_value=True), \
                mock.patch.object(ollamactl, "stop_all", return_value="stopped") as stop, \
                mock.patch.object(ollamactl, "let_go", return_value=[]) as let_go, \
                mock.patch.dict(ollamactl.STATE, {"started_by_us": started}):
            ollamactl.on_close(cfg)
        return stop.called, let_go.called

    def test_default_quits_only_what_patchbay_started(self):
        self.assertEqual(self.run_close("started", True), (True, False))
        self.assertEqual(self.run_close("started", False), (False, True))

    def test_always_and_never(self):
        self.assertEqual(self.run_close("always", False), (True, False))
        self.assertEqual(self.run_close("never", True), (False, True))

    def test_other_computer_untouched(self):
        cfg = {"ollama_url": "http://192.168.50.9:11434", "ollama_on_close": "always"}
        with mock.patch.object(ollamactl, "stop_all") as stop:
            self.assertEqual(ollamactl.on_close(cfg), "")
            self.assertEqual(ollamactl.start(cfg).startswith("The AI runs on"), True)
        self.assertFalse(stop.called)


if __name__ == "__main__":
    unittest.main()
