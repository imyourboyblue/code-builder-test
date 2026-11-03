"""
Tests for the Mac summary the AI gets (macspecs.py). Run from the patchbay folder:

  python3 -m unittest discover -s tests -t .

The Mac is faked, so these run anywhere.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import macspecs  # noqa: E402
import server  # noqa: E402

FAKE = {
    "system": {"os": "macOS", "os_version": "26.1", "arch": "arm64", "chip": "Apple M4 Max", "cores": 14,
               "memory_gb": 36, "rosetta": False},
    "brew": "/opt/homebrew",
    "keg": {"llvm@18": "/opt/homebrew/opt/llvm@18/bin/clang"},
    "gcc": [],
    "tools": {"clang": ("/usr/bin/clang", "Apple clang 17.0.0"), "make": ("/usr/bin/make", "make (GNU Make 3.81)"),
              "cmake": ("/opt/homebrew/bin/cmake", "4.1.2")},
    "xcode": True, "dkp": "/opt/devkitpro/pacman/bin/pacman",
    "container": {"engine": "Apple container", "running": True},
    "disk": 1200,
}


def project(files):
    d = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    return d


class Summary(unittest.TestCase):
    def test_mac_facts(self):
        text = macspecs.summary(FAKE)
        self.assertIn("macOS 26.1, Apple M4 Max, arm64 (Apple Silicon), 14 cores, 36 GB memory", text)
        self.assertIn("NOT Linux", text)
        self.assertIn("Homebrew (/opt/homebrew)", text)
        self.assertIn("llvm@18: /opt/homebrew/opt/llvm@18/bin/clang", text)
        self.assertIn("clang-18", text)            # told that numbered names don't exist
        self.assertIn("Apple container (running)", text)
        self.assertIn("devkitPro", text)

    def test_block_is_stripped_for_cloud(self):
        with mock.patch.object(macspecs, "summary", lambda: "- macOS 26.1"):
            system = "You are PatchBay." + macspecs.prompt_block() + "\n\nToday is Monday."
        self.assertIn("macOS 26.1", system)
        self.assertEqual(macspecs.strip(system), "You are PatchBay.\n\nToday is Monday.")

    def test_claude_never_gets_it(self):
        sent = {}

        class FakeClaude:
            def mode(self):
                return "always"

            def scrubber(self, known):
                return None

            def chat(self, system, messages, tools, sc):
                sent["system"] = system
                return {"role": "assistant", "content": "ok"}

            def save_aliases(self, sc):
                pass

            def public(self):
                return {"session_cost": 0, "month_cost": 0}

        app = mock.Mock(claude=FakeClaude(), computer="the Mac")
        with mock.patch.object(server, "APP", app), mock.patch.object(server, "claude_known", lambda: []), \
                mock.patch.object(macspecs, "summary", lambda: "- macOS 26.1, Apple M4 Max"):
            system = "Rules." + macspecs.prompt_block()
            server.ask_model({}, [{"role": "system", "content": system}, {"role": "user", "content": "hi"}], [])
        self.assertEqual(sent["system"], "Rules.")

    def test_disk_rounding(self):
        self.assertEqual(macspecs._round_disk(1234.5), 1200)
        self.assertEqual(macspecs._round_disk(47.9), 45)

    def test_cached(self):
        calls = []
        with mock.patch.object(macspecs, "system_facts", lambda: calls.append(1) or FAKE["system"]), \
                mock.patch.object(macspecs, "tool_versions", lambda: {}), \
                mock.patch.object(macspecs, "container_facts", lambda: {}), \
                mock.patch.object(macspecs, "devkitpro", lambda: None):
            macspecs.forget()
            macspecs.facts()
            macspecs.facts()
            self.assertEqual(len(calls), 1)
            macspecs.forget()
            macspecs.facts()
            self.assertEqual(len(calls), 2)


@mock.patch.dict(os.environ, {"PATCHBAY_TEST_MAC": "1"})
class Target(unittest.TestCase):
    def test_ubuntu_only_project(self):
        d = project({"Makefile": "CC=clang-18\nall:\n\t$(CC) main.c\n",
                     "README.md": "Build on Ubuntu 24.04:\n  sudo apt-get install -y build-essential\n",
                     ".github/workflows/ci.yml": "jobs:\n  build:\n    runs-on: ubuntu-latest\n"})
        warn = macspecs.target_warning(d)
        self.assertIn("made for Linux", warn)
        self.assertIn("sandbox", warn)

    def test_mac_friendly_project(self):
        d = project({"Makefile": "all:\n\tcc main.c\n",
                     "README.md": "Ubuntu: apt install libfoo-dev\nmacOS: brew install libfoo\n"})
        self.assertEqual(macspecs.target_warning(d), "")

    def test_console_project_is_fine(self):
        d = project({"Makefile": "include $(DEVKITPRO)/libnx/switch_rules\n",
                     "README.md": "On Ubuntu: apt-get install ...\n"})
        self.assertEqual(macspecs.target_warning(d), "")

    def test_linux_header(self):
        d = project({"src/main.c": "#include <sys/epoll.h>\nint main(){}\n"})
        self.assertIn("Linux-only headers", macspecs.target_warning(d))


if __name__ == "__main__":
    unittest.main()
