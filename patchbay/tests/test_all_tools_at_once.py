"""
Tests for Joshua's EglantinePS5 sandbox build stopping at one missing program after another (git, then python3,
then wget, then unzip, then "clang, an archive tool, and ranlib are required"), and the loop guard counting each new
one as the same failure:
- before the setup step, PatchBay reads the build files and installs every program they check for, all at once;
- the project's own Linux and package list come back when a hand-written setup replaced them;
- a missing program the build names also makes PatchBay read the failing setup script (inside the sandbox) and add
  everything else it checks for;
- a NEW missing program is progress; only the same missing program again counts as a repeat.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import sandbox  # noqa: E402
import sandboxrecipe as r  # noqa: E402
import server  # noqa: E402
from tests.test_safety import FakeChat, device_cfg  # noqa: E402

BOILERPLATE_SETUP = """#!/usr/bin/env bash
set -euo pipefail
for command in wget unzip sha256sum tar make; do
    command -v "$command" >/dev/null || {
        echo "missing required command: $command" >&2
        exit 2
    }
done
compiler=$(command -v clang-18 || command -v clang || true)
archiver=$(command -v llvm-ar-18 || command -v llvm-ar || command -v ar || true)
ranlib=$(command -v llvm-ranlib-18 || command -v llvm-ranlib || command -v ranlib || true)
"""
CHECK_TOOLCHAIN = """need() { :; }
for tool in clang-18 clang++-18 ld.lld-18 llvm-ar-18; do need "$tool" "$tool"; done
for tool in cmake ninja ccache make nasm meson rsync git; do need "$tool" "$tool"; done
for tool in curl wget unzip tar sha256sum; do need "$tool" "$tool"; done
"""
MAKEFILE = """# The build runs on Linux (Ubuntu 26.04; WSL works).
JOBS ?= $(shell nproc)
RELEASE_APP := build/release/PPSA98515
deps:
\tgit submodule update --init Eglantine
\tpython3 -B tools/deps.py fetch
"""
FAIL_OUT = ("git submodule update --init Eglantine\npython3 -B tools/deps.py fetch\n"
            "deps: fetching https://github.com/drakmor/ps5-native-app-boilerplate.git at dd44 into "
            "/ps5-native-app-boilerplate\ndeps: setting up boilerplate: bash tools/setup-native-dependencies.sh\n"
            "missing required command: wget\nmake: *** [Makefile:50: deps] Error 1")


class Parsing(unittest.TestCase):
    def test_missing_program_messages(self):
        self.assertEqual(r.missing_commands("/bin/bash: line 1: git: command not found"), ["git"])
        self.assertEqual(r.missing_commands("missing required command: unzip"), ["unzip"])
        self.assertEqual(r.missing_commands("clang, an archive tool, and ranlib are required to build zlib"),
                         ["clang", "ranlib"])
        self.assertEqual(r.missing_commands("Python 3.11 is required\nzlib is required"), [])

    def test_versioned_llvm_packages(self):
        self.assertEqual(r.packages_for(["clang-18", "clang++-18", "ld.lld-18", "llvm-ar-18", "llvm-ranlib-18"])[0],
                         ["clang-18", "lld-18", "llvm-18"])
        self.assertEqual(r.package_for("sha256sum"), "coreutils")
        self.assertEqual(r.package_for("ranlib"), "binutils")

    def test_reads_every_check_in_a_script(self):
        cmds = r.commands_in_text(BOILERPLATE_SETUP)
        for c in ("wget", "unzip", "sha256sum", "tar", "make", "clang-18", "llvm-ar-18", "llvm-ranlib-18"):
            self.assertIn(c, cmds)
        self.assertNotIn("clang", cmds)          # 'clang-18 || clang': the first one is enough
        self.assertNotIn("ar", cmds)

    def test_scripts_named_in_output_are_safe_paths(self):
        scripts, dirs = r.scripts_named(FAIL_OUT + "\nbash ../../etc/x.sh; rm -rf /")
        self.assertEqual(scripts, ["tools/setup-native-dependencies.sh"])
        self.assertEqual(dirs, ["/ps5-native-app-boilerplate"])
        cmd = r.show_scripts_command(scripts, dirs)
        self.assertIn("/ps5-native-app-boilerplate/tools/setup-native-dependencies.sh", cmd)
        self.assertNotIn("rm ", cmd)

    def test_found_packages_never_break_the_setup_and_add_once(self):
        s = "apt-get update && apt-get install -y make && bash tools/setup.sh"
        s2 = r.add_found_packages(s, ["make", "unzip", "clang-18"])
        self.assertIn(r.TOLERANT, s2)                       # a missing package is skipped, not fatal
        self.assertLess(s2.index("unzip"), s2.index("bash tools/setup.sh"))     # before the project's script
        self.assertEqual(r.add_found_packages(s2, ["unzip", "clang-18"]), s2)
        self.assertEqual(s2.count("make"), 1)


class Project(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.base = Path(self._t.name).resolve()
        self.proj = self.base / "EglantinePS5"
        (self.proj / "tools" / "ci").mkdir(parents=True)
        (self.proj / "Makefile").write_text(MAKEFILE)
        (self.proj / "tools" / "ci" / "ubuntu-packages.txt").write_text("git wget make python3\nclang-18 lld-18\n")
        (self.proj / "tools" / "check-toolchain.sh").write_text(CHECK_TOOLCHAIN)
        (self.proj / "tools" / "deps.json").write_text(json.dumps({"items": [{
            "name": "boilerplate", "kind": "git", "url": "https://github.com/drakmor/ps5-native-app-boilerplate.git",
            "setup": ["bash", "tools/setup-native-dependencies.sh"]}]}))
        self.root = mock.patch.object(sandbox, "ROOT", self.base / "sandboxes")
        self.root.start()
        self.addCleanup(self.root.stop)
        self.access = mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(self.base)})
        self.access.start()
        self.addCleanup(self.access.stop)
        self.addCleanup(self._t.cleanup)

    def test_needed_commands_cover_the_fetched_boilerplate_too(self):
        pk, unknown, read = r.setup_packages(self.proj)
        for p in ("wget", "unzip", "clang-18", "llvm-18", "lld-18", "cmake", "ninja-build", "git", "python3"):
            self.assertIn(p, pk)
        self.assertIn("tools/deps.json", read)
        self.assertIn("tools/check-toolchain.sh", read)

    def test_setup_step_installs_everything_at_once(self):
        # Joshua's sandbox: the plain default Linux and a hand-written setup that replaced the project's.
        sandbox.create("EglantinePS5", str(self.proj), "linux",
                       recipe={"setup_command": "apt-get update && apt-get install -y make gcc pkg-config"})
        s, words = server.complete_setup(sandbox.load("EglantinePS5"))
        self.assertEqual(s["image"], "ubuntu:26.04")
        setup = s["setup_command"]
        for p in ("git", "python3", "wget", "unzip", "clang-18", "llvm-18", "lld-18", "cmake", "gcc", "pkg-config",
                  "coreutils"):
            self.assertIn(p, r.apt_packages(setup), p)
        self.assertIn("everything installs at once", words)
        self.assertIn("ubuntu:26.04", words)
        # Nothing new the second time.
        s2, words2 = server.complete_setup(s)
        self.assertEqual(words2, "")
        self.assertEqual(s2["setup_command"], setup)

    def test_a_linux_that_built_once_is_kept(self):
        s = sandbox.create("EglantinePS5", str(self.proj), "linux", "ubuntu:24.04")
        s["built_ok"] = True
        sandbox.save(s)
        s, _ = server.complete_setup(sandbox.load("EglantinePS5"))
        self.assertEqual(s["image"], "ubuntu:24.04")
        self.assertIn("unzip", r.apt_packages(s["setup_command"]))

    def test_sandbox_create_adds_the_ais_setup_to_the_projects(self):
        rec = server.sandbox_recipe_for(self.proj, {"setup_command": "apt-get install -y make gcc pkg-config"})
        for p in ("git", "python3", "clang-18", "gcc", "pkg-config"):
            self.assertIn(p, r.apt_packages(rec["setup_command"]))

    def test_failed_build_reads_the_fetched_script_inside_the_sandbox(self):
        sandbox.create("EglantinePS5", str(self.proj), "linux", recipe={"setup_command": "apt-get install -y git"})
        s = sandbox.load("EglantinePS5")
        seen = []

        def fake_run(name, cmd, emit=None, stop=None, base=None, record=True):
            seen.append((cmd, record))
            return 0, BOILERPLATE_SETUP
        with mock.patch.object(sandbox, "run", fake_run):
            tips = server.sandbox_hints(s, 2, FAIL_OUT, "build")
        self.assertIn("/ps5-native-app-boilerplate/tools/setup-native-dependencies.sh", seen[0][0])
        self.assertFalse(seen[0][1])          # a quiet look: the sandbox's last run stays the build's
        setup = sandbox.load("EglantinePS5")["setup_command"]
        for p in ("wget", "unzip", "clang-18", "llvm-18", "coreutils"):
            self.assertIn(p, r.apt_packages(setup), p)
        self.assertIn("added everything else it checks for at once", tips)


class LoopGuard(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name)
        (tmp / "dev").mkdir()
        self.cfg = device_cfg(tmp / "dev", tmp / "data")
        self.st = FakeChat(self.cfg)
        self.st.timing, self.st.chat_id = [], ""
        self.events = []
        self.addCleanup(self._t.cleanup)

    def run_loop(self, handler):
        call = {"tool_calls": [{"function": {"name": "sandbox_build", "arguments": {"sandbox": "EglantinePS5"}}}]}
        with mock.patch.object(server, "ask_model", return_value=call) as ask, \
                mock.patch.object(server, "second_opinion", return_value=""), \
                mock.patch.object(server, "claude_ready", return_value=False):
            server._chat_loop(self.st, "build eglantineps5", self.events.append, self.cfg, "system", [],
                              {"sandbox_build": handler})
        return ask

    def test_each_new_missing_program_is_progress(self):
        seen = set()
        for prog in ("git", "python3", "wget", "unzip"):
            self.assertTrue(server.new_missing_programs(
                "sandbox_build", "In the Linux sandbox X, make failed (exit code 2). missing required command: %s"
                % prog, seen))
        self.assertFalse(server.new_missing_programs("sandbox_build", "missing required command: unzip", seen))
        self.assertFalse(server.new_missing_programs("propose_build", "git: command not found", set()))

    def test_new_missing_programs_dont_stop_the_job_but_the_same_one_does(self):
        progs = iter(["git", "python3", "wget", "unzip", "rev", "rev", "rev", "rev", "rev", "rev"])
        msg = "In the Linux sandbox EglantinePS5, make failed (exit code 2). First error: missing required command: %s"
        ask = self.run_loop(lambda st, emit, a: msg % next(progs))
        # 5 different programs go through; then 'rev' again is a repeat: twice -> diagnose, three times -> stop.
        self.assertEqual(ask.call_count, 8)
        notes = [e["text"] for e in self.events if e["type"] == "note"]
        self.assertEqual(sum("stopped retrying" in t for t in notes), 1)
        stop = next(e for e in self.events if e["type"] == "choices")
        self.assertIn("failed 3 times", stop["text"])


if __name__ == "__main__":
    unittest.main()
