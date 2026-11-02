"""
Tests for the private builder (harness/). Run from the patchbay folder:

  python3 -m unittest discover -s tests -t .
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import coder, compiler, pipeline, sanitizer  # noqa: E402

CLEAN = """# Module
A function that parses key=value lines into a dictionary.

## Functions
parse(text: str) -> dict

## Unit tests
- parse("a=1") returns {"a": "1"}
- connect to 192.0.2.10 and 127.0.0.1 (example addresses)
- send results to user@example.com
- password: str (an input field)
"""

EMPTY = dict(sanitizer.EMPTY_LIST)


def kinds(found):
    return sorted({f["kind"] for f in found})


class SanitizerTests(unittest.TestCase):
    def test_clean_spec_passes(self):
        self.assertEqual(sanitizer.scan(CLEAN, [], EMPTY), [])

    def test_blocked_terms_whole_words_any_case(self):
        lst = dict(EMPTY, blocked_terms=["ProjectName", "internal-domain.com"])
        found = sanitizer.scan("The PROJECTNAME tool talks to api.internal-domain.com", [], lst)
        self.assertEqual(len(found), 2)
        self.assertEqual(sanitizer.scan("ProjectNameless is fine", [], dict(EMPTY, blocked_terms=["ProjectName"])), [])

    def test_blocked_patterns(self):
        lst = dict(EMPTY, blocked_patterns=[r"\bACME-\d{4}\b"])
        self.assertEqual(kinds(sanitizer.scan("ticket ACME-1234", [], lst)), ["blocked"])

    def test_private_patterns(self):
        text = ("host 192.168.50.120 port 21\nmac 3c:22:fb:12:34:56\nmail joshua@gmail.com\n"
                "path /Users/blue/Patchbay-projects/ftpd\nname switch.local\nkey sk-ant-api03-abcdefghijklmnop\n"
                "password = hunter22\n")
        found = sanitizer.scan(text, [], EMPTY)
        self.assertEqual(kinds(found), ["email", "host", "ip", "key", "mac", "path", "secret"])
        shown = " ".join(f["shown"] for f in found)
        self.assertNotIn("hunter22", shown)          # secrets are never shown on screen
        self.assertNotIn("abcdefghijklmnop", shown)

    def test_version_numbers_are_not_ips(self):
        self.assertEqual(sanitizer.scan("needs libfoo 1.2.3.4.5 or newer", [], EMPTY), [])

    def test_patchbay_terms(self):
        terms = [("device", "Joshua Switch"), ("secret", "s3cretpass"), ("project", "ftpd")]
        found = sanitizer.scan("Build ftpd for Joshua Switch with s3cretpass", terms, EMPTY)
        self.assertEqual(kinds(found), ["device", "project", "secret"])
        self.assertIn("(hidden)", [f["shown"] for f in found if f["kind"] == "secret"])

    def test_allowed_terms_skip_known_values(self):
        lst = dict(EMPTY, allowed_terms=["blue"])
        self.assertEqual(sanitizer.scan("make the button blue", [("user", "blue")], lst), [])
        self.assertEqual(len(sanitizer.scan("make the button blue", [("user", "blue")], EMPTY)), 1)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            good, bad = Path(d) / "good.md", Path(d) / "bad.md"
            good.write_text(CLEAN)
            bad.write_text("connect to 10.0.0.7")
            lst = Path(d) / "list.json"
            lst.write_text(json.dumps(EMPTY))
            out = io.StringIO()
            old, sys.stdout = sys.stdout, out
            try:
                self.assertEqual(sanitizer.main([str(good), "--no-patchbay", "--list", str(lst)]), 0)
                self.assertEqual(sanitizer.main([str(bad), "--no-patchbay", "--list", str(lst)]), 1)
            finally:
                sys.stdout = old

    def test_bad_list_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "list.json"
            p.write_text("{not json")
            with self.assertRaises(ValueError):
                sanitizer.load_list(p)


class FakeOllama:
    """Answers /api/chat like Ollama: first the specs in order, and the double-check's JSON."""

    def __init__(self, specs, suspects=()):
        self.specs, self.suspects, self.calls = list(specs), list(suspects), []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.calls.append(body)
        if body.get("format") == "json":
            text = json.dumps({"suspect": self.suspects})
        else:
            text = self.specs.pop(0)
        return _Resp(json.dumps({"message": {"content": text}}).encode())


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PipelineTests(unittest.TestCase):
    def test_rewrites_once_then_clean(self):
        leaky = CLEAN + "\nRuns on Joshua Switch at 192.168.50.120\n"
        fake = FakeOllama([leaky, CLEAN])
        spec, found, suspects = pipeline.make_spec("idea", "http://localhost:11434", "m",
                                                   [("device", "Joshua Switch")], EMPTY, leak_check=True,
                                                   urlopen=fake)
        self.assertEqual((found, suspects), ([], []))
        retry = fake.calls[1]["messages"][-1]["content"]
        self.assertIn("Joshua Switch", retry)           # told the local AI (on this Mac) what to remove

    def test_hidden_secret_stops_without_retry(self):
        fake = FakeOllama([CLEAN + "\nlogin with s3cretpass\n"])
        spec, found, _ = pipeline.make_spec("idea", "http://x", "m", [("secret", "s3cretpass")], EMPTY,
                                            leak_check=False, urlopen=fake)
        self.assertEqual(kinds(found), ["secret"])
        self.assertEqual(len(fake.calls), 1)

    def test_double_check_only_counts_words_in_spec(self):
        fake = FakeOllama([], suspects=["Zion Homebrew", "Python", "NotInSpec"])
        hits = compiler.double_check("Uses Python. Named Zion Homebrew.", "http://x", "m", urlopen=fake)
        self.assertEqual(hits, ["Zion Homebrew"])

    def test_write_out_and_undo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("old")
            pipeline.write_out(root, [("a.py", "new"), ("pkg/b.py", "b")])
            self.assertEqual((root / "a.py").read_text(), "new")
            pipeline.undo(root)
            self.assertEqual((root / "a.py").read_text(), "old")
            self.assertFalse((root / "pkg" / "b.py").exists())


class CoderTests(unittest.TestCase):
    def test_paths_must_stay_inside(self):
        for bad in ("/etc/passwd", "../x.py", "a/../../x", "~/x", "C:\\x", ".git/config", "keys/server.pem"):
            with self.assertRaises(coder.CodeError, msg=bad):
                coder.clean_path(bad)
        self.assertEqual(coder.clean_path("src/./parser.py"), "src/parser.py")

    def test_request_holds_only_the_spec(self):
        body = coder.request_body("SPEC TEXT")
        self.assertEqual(body["messages"], [{"role": "user", "content": "SPEC TEXT"}])
        self.assertEqual(set(body), {"system", "max_tokens", "messages", "tools", "tool_choice"})

    def test_files_from_answer(self):
        resp = {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "write_files", "input": {
            "files": [{"path": "m.py", "content": "x = 1\n"}], "notes": "run tests"}}]}
        files, notes = coder.files_from(resp)
        self.assertEqual(files, [("m.py", "x = 1\n")])
        with self.assertRaises(coder.CodeError):
            coder.files_from(dict(resp, stop_reason="max_tokens"))

    def test_generate_sends_one_fresh_request(self):
        sent = []

        class FakeClaude:
            def send(self, body, why, model=None, effort=None):
                sent.append((body, model))
                return {"content": [{"type": "tool_use", "name": "write_files",
                                     "input": {"files": [{"path": "m.py", "content": ""}]}}]}
        files, _, _ = coder.generate(FakeClaude(), "SPEC", "claude-sonnet-5-5")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "claude-sonnet-5-5")
        self.assertEqual(len(sent[0][0]["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
