"""
Build leftovers stay out of sandboxes (from Joshua's EglantinePS5 log, 2026-11-03): the Mac folder held .deps/ and
.local/ (gitignored) with an FFmpeg config.h from an earlier configure, so every sandbox build failed with
"Out of tree builds are impossible with config.h in source dir", even after a reset, and the AI then wanted to change
the project's own build script on the Mac.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox  # noqa: E402
import server  # noqa: E402

FFMPEG_FAIL = ("== FFmpeg for the PS5\nOut of tree builds are impossible with config.h in source dir.\n"
               "make: *** [Makefile:46: prepare] Error 1\n")


def make_project(top, git):
    p = top / "EglantinePS5"
    (p / "tools").mkdir(parents=True)
    (p / "tools" / "build-headless-ffmpeg.sh").write_text("#!/usr/bin/env bash\necho ffmpeg\n")
    (p / "Makefile").write_text("all:\n\tbash tools/prepare-build.sh\n")
    (p / ".gitignore").write_text(".deps/\nbuild/\ndist/\n.local/\n__pycache__/\n*.log\n!keep.log\n")
    (p / ".deps" / "ffmpeg-c7b5f1537d").mkdir(parents=True)
    (p / ".deps" / "ffmpeg-c7b5f1537d" / "config.h").write_text("/* from a Mac configure */\n")
    (p / ".local").mkdir()
    (p / ".local" / "headless-cache").write_text("/Users/blue/.cache/ps5-eden-headless.abc\n")
    (p / "dist").mkdir()
    (p / "dist" / "EglantinePS5.self").write_bytes(b"old mac build")
    (p / "build.log").write_text("old\n")
    (p / "keep.log").write_text("keep\n")
    if git:
        subprocess.run(["git", "init", "-q", str(p)], check=True)
    return p


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitProjectTest(unittest.TestCase):
    git = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = make_project(base, self.git)
        self.root = mock.patch.object(sandbox, "ROOT", base / "sandboxes")
        self.root.start()

    def tearDown(self):
        self.root.stop()
        self.tmp.cleanup()

    def test_copy_leaves_build_leftovers_out(self):
        sb = sandbox.create("EglantinePS5", str(self.proj))
        w = sandbox.work(sb)
        self.assertTrue((w / "tools" / "build-headless-ffmpeg.sh").exists())
        self.assertTrue((w / "Makefile").exists())
        self.assertTrue((w / ".gitignore").exists())
        self.assertTrue((w / "keep.log").exists())                 # un-ignored with !
        for gone in (".deps", ".local", "dist", "build.log"):
            self.assertFalse((w / gone).exists(), gone)
        self.assertIn(".deps/", sb["left_out"])
        self.assertIn("Left out as build leftovers", sandbox.summary(sb))
        if self.git:
            self.assertTrue((w / ".git").exists())                  # the Makefile uses git submodule

    def test_reset_copies_clean_again_and_setting_copies_all(self):
        sandbox.create("EglantinePS5", str(self.proj))
        (sandbox.work(sandbox.load("EglantinePS5")) / ".deps").mkdir()
        sb = sandbox.reset("EglantinePS5")
        self.assertFalse((sandbox.work(sb) / ".deps").exists())
        sandbox.set_options("EglantinePS5", copy_ignored=True)
        sb = sandbox.reset("EglantinePS5")
        self.assertTrue((sandbox.work(sb) / ".deps" / "ffmpeg-c7b5f1537d" / "config.h").exists())
        self.assertEqual(sb["left_out_count"], 0)

    def test_is_left_out(self):
        self.assertTrue(sandbox.is_left_out(self.proj, ".deps/ffmpeg-c7b5f1537d/config.h"))
        self.assertTrue(sandbox.is_left_out(self.proj, "dist/EglantinePS5.self"))
        self.assertFalse(sandbox.is_left_out(self.proj, "tools/build-headless-ffmpeg.sh"))


class PlainFolderTest(GitProjectTest):
    """Not a git checkout: the top .gitignore is read directly."""
    git = False


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = make_project(base, False)
        self.root = mock.patch.object(sandbox, "ROOT", base / "sandboxes")
        self.root.start()
        self.access = mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(base)})
        self.access.start()
        self.sb = sandbox.create("EglantinePS5", str(self.proj))
        self.sb = sandbox.set_recipe("EglantinePS5", {"output": "dist/EglantinePS5.self"}) or \
            sandbox.load("EglantinePS5")
        server.LEFTOVERS.clear()

    def tearDown(self):
        self.access.stop()
        self.root.stop()
        self.tmp.cleanup()
        server.LEFTOVERS.clear()

    def test_leftover_error_says_clean_inside_not_edit_project(self):
        tips = server.sandbox_hints(self.sb, 2, FFMPEG_FAIL, "build")
        self.assertIn("leftovers from an earlier build", tips)
        self.assertIn("ARM (arm64)", tips)
        self.assertIn("Don't edit the project's own scripts", tips)
        self.assertTrue(server.LEFTOVERS.get("EglantinePS5"))

    def test_old_sandbox_is_told_to_reset(self):
        old = dict(self.sb)
        old.pop("left_out_count", None)
        tips = server.sandbox_hints(old, 2, FFMPEG_FAIL, "build")
        self.assertIn("propose_sandbox_reset", tips)

    def _changed_script(self):
        f = sandbox.work(self.sb) / "tools" / "build-headless-ffmpeg.sh"
        f.write_text("#!/usr/bin/env bash\nrm -f config.h\necho ffmpeg\n")

    def test_copying_out_a_script_after_leftover_error_is_refused(self):
        server.sandbox_hints(self.sb, 2, FFMPEG_FAIL, "build")
        self._changed_script()
        with self.assertRaises(server.ToolError) as e:
            server.t_sandbox_copy_out(None, lambda x: None, {"sandbox": "EglantinePS5",
                                                             "path": "tools/build-headless-ffmpeg.sh", "why": "x"})
        self.assertIn("leftovers", str(e.exception))

    def test_copying_out_own_file_needs_a_reason(self):
        self._changed_script()
        with self.assertRaises(server.ToolError) as e:
            server.t_sandbox_copy_out(None, lambda x: None, {"sandbox": "EglantinePS5",
                                                             "path": "tools/build-headless-ffmpeg.sh"})
        self.assertIn("last resort", str(e.exception))
        shown = {}
        with mock.patch.object(server, "sandbox_card", lambda st, emit, ch, title, diff, *r: shown.update(d=diff)):
            server.t_sandbox_copy_out(None, lambda x: None, {"sandbox": "EglantinePS5",
                                                             "path": "tools/build-headless-ffmpeg.sh",
                                                             "why": "The script has a typo on Linux too."})
        self.assertIn("Why: The script has a typo", shown["d"])

    def test_build_result_copies_out_without_a_reason(self):
        out = sandbox.work(self.sb) / "dist"
        out.mkdir()
        (out / "EglantinePS5.self").write_bytes(b"new linux build")
        shown = {}
        with mock.patch.object(server, "sandbox_card", lambda st, emit, ch, title, diff, *r: shown.update(d=diff)):
            server.t_sandbox_copy_out(None, lambda x: None, {"sandbox": "EglantinePS5",
                                                             "path": "dist/EglantinePS5.self"})
        self.assertNotIn("Why:", shown["d"])

    def test_plain_run_that_builds_is_noticed(self):
        started = time.time()
        out = sandbox.work(self.sb) / "dist"
        out.mkdir()
        (out / "EglantinePS5.self").write_bytes(b"new linux build")
        server.LEFTOVERS["EglantinePS5"] = True
        tips = server.sandbox_hints(self.sb, 0, "", None, started)
        self.assertIn("propose_sandbox_copy_out path dist/EglantinePS5.self", tips)
        self.assertNotIn("EglantinePS5", server.LEFTOVERS)


if __name__ == "__main__":
    unittest.main()
