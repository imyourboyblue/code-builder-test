"""
Sandbox fixes stick (from Joshua's EglantinePS5 log, 2026-11-03). The sandbox is ARM Linux; the PS5 files are x86.
GNU objcopy there can't read them ("Unable to recognise the architecture"), so tools/isolate-radv.py copied the RADV
library through unchanged and stopped. That half-made library stayed in build/radv-isolated/, the next `make` skipped
the isolation step, and the link failed with hundreds of duplicate symbols. Fixes (llvm, the x86 libclang_rt) were
done by hand with sandbox_run, never saved, and `make clean` brought the first error back.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox  # noqa: E402
import sandboxrecipe as r  # noqa: E402
import server  # noqa: E402

OBJCOPY_FAIL = (
    "objcopy: Unable to recognise the architecture of the input file `/mihawk-vulkan-review/.deps/native/radv-release/"
    "lib/libvulkan_radeon.ps5.a(radv_queue.c.o)'\nTraceback (most recent call last):\n  File \"/work/tools/"
    "isolate-radv.py\", line 56, in <module>\nAssertionError: Unrenamed implementation symbol\n"
    "make: *** [Makefile:46: prepare] Error 1\n")
RT_FAIL = ("FAILED: [code=2] bin/eden-headless\nmissing /usr/lib/llvm-21/lib/clang/21/lib/linux/"
           "libclang_rt.builtins-x86_64.a\nninja: build stopped: subcommand failed.\n")


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.base = Path(self._t.name).resolve()
        self.proj = self.base / "EglantinePS5"
        (self.proj / "tools").mkdir(parents=True)
        (self.proj / "Makefile").write_text("package:\n\tbash tools/prepare-build.sh\n")
        (self.proj / "tools" / "isolate-radv.py").write_text("print('isolate')\n")
        (self.proj / ".gitignore").write_text("build/\n")
        p = mock.patch.object(sandbox, "ROOT", self.base / "sandboxes")
        p.start()
        self.addCleanup(p.stop)
        a = mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(self.base)})
        a.start()
        self.addCleanup(a.stop)
        sandbox.create("EglantinePS5", str(self.proj), "linux",
                       recipe={"setup_command": "apt-get update && apt-get install -y make",
                               "build_command": "make package"})
        self.events = []

    def sb(self):
        return sandbox.load("EglantinePS5")


class ToolSwaps(Base):
    def test_arm_objcopy_failure_swaps_the_tools_for_good(self):
        tips = server.sandbox_hints(self.sb(), 2, OBJCOPY_FAIL, "build", time.time(), "make package")
        s = self.sb()
        for t in ("objcopy", "nm", "ranlib", "ar"):
            self.assertEqual(s["tool_aliases"][t], "llvm-" + t)
        self.assertIn("llvm", r.apt_packages(s["setup_command"]))
        self.assertIn("make", r.apt_packages(s["setup_command"]))          # the rest of the setup stays
        self.assertIn("ARM Linux", tips)
        self.assertIn("Don't edit the project's scripts", tips)
        self.assertIn("after make clean", tips)
        self.assertTrue(any("x86 (PS5)" in n["text"] for n in s["notes"]))
        # The project's own script wasn't touched.
        self.assertEqual((sandbox.work(s) / "tools" / "isolate-radv.py").read_text(), "print('isolate')\n")

    def test_every_run_links_the_swaps_first(self):
        sandbox.set_alias("EglantinePS5", "objcopy", "llvm-objcopy")
        seen = {}

        def fake_stream(argv, cwd, env, emit, timeout, stop, on_kill=None):
            seen["cmd"] = argv[-1]
            return 0, "ok"
        with mock.patch.object(sandbox, "ensure_container", return_value=("docker", "docker", "patchbay-sb-x")), \
                mock.patch.object(sandbox, "_stream", fake_stream):
            sandbox.run("EglantinePS5", "make clean; make -j8")
        self.assertTrue(seen["cmd"].endswith("make clean; make -j8"))
        # Linked in /usr/local/bin: before /usr/bin even in the PATH build scripts set themselves.
        self.assertIn("ln -sf \"$t\" /usr/local/bin/objcopy", seen["cmd"])
        self.assertNotIn("export PATH=", seen["cmd"])
        self.assertEqual(self.sb()["last_run"]["command"], "make clean; make -j8")   # the chat sees its own command

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_prelude_works_in_bash_and_never_leaves_a_dangling_link(self):
        d = self.base / "alias"
        fake = self.base / "fakebin"
        fake.mkdir()
        (fake / "llvm-objcopy").write_text("#!/bin/sh\necho llvm-objcopy\n")
        (fake / "llvm-objcopy").chmod(0o755)
        s = dict(self.sb(), tool_aliases={"objcopy": "llvm-objcopy", "nm": "llvm-nm-missing"})
        pre = sandbox.prelude(s).replace(sandbox.ALIAS_DIR, str(d))
        d.mkdir()
        # The command resets PATH the way tools/build-package.sh does; the swap dir is in it, like /usr/local/bin.
        out = subprocess.run(["bash", "-o", "pipefail", "-c", pre + "export PATH=%s:/usr/bin:/bin; objcopy; ls %s"
                              % (d, d)], capture_output=True, text=True,
                             env={"PATH": "%s:%s:/usr/bin:/bin" % (d, fake)}).stdout
        self.assertIn("llvm-objcopy", out.splitlines()[0])
        self.assertNotIn("nm", out.split("\n", 1)[1])        # not installed: no link, so GNU nm still works

    def test_setup_links_the_swaps_and_checks_them_under_a_plain_path(self):
        sandbox.set_alias("EglantinePS5", "objcopy", "llvm-objcopy")
        setup = sandbox.step_command(self.sb(), "setup")
        self.assertTrue(setup.startswith("apt-get update"))
        self.assertIn("ln -sf \"$t\" /usr/local/bin/objcopy", setup)
        self.assertIn("env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin objcopy --version", setup)
        self.assertIn("PatchBay check FAILED", setup)
        tips = server.sandbox_hints(self.sb(), 97, "PatchBay check FAILED: objcopy is still not llvm-objcopy", "setup")
        self.assertIn("don't build until it's fixed", tips)

    def test_a_real_program_in_usr_local_bin_is_never_replaced(self):
        line = sandbox.link_lines(dict(self.sb(), tool_aliases={"objcopy": "llvm-objcopy"}))[0]
        self.assertIn('[ -e /usr/local/bin/objcopy ] && [ ! -L /usr/local/bin/objcopy ]; then :', line)

    def test_same_error_with_the_swap_already_set_doesnt_loop_the_setup(self):
        server.sandbox_hints(self.sb(), 2, OBJCOPY_FAIL, "build", time.time(), "make package")
        s = self.sb()
        s["needs_setup"] = False          # the setup ran
        sandbox.save(s)
        tips = server.sandbox_hints(self.sb(), 2, OBJCOPY_FAIL, "build", time.time(), "make package")
        self.assertFalse(self.sb()["needs_setup"])
        self.assertIn("full path", tips)
        self.assertNotIn("fixed the sandbox's tools for good", tips)

    def test_swap_checks(self):
        with self.assertRaises(sandbox.SandboxError):
            sandbox.set_alias("EglantinePS5", "objcopy", "objcopy")
        with self.assertRaises(sandbox.SandboxError):
            sandbox.set_alias("EglantinePS5", "objcopy", "llvm-objcopy; rm -rf /")
        sandbox.set_alias("EglantinePS5", "objcopy", "llvm-objcopy")
        sandbox.set_alias("EglantinePS5", "objcopy", "")
        self.assertEqual(self.sb()["tool_aliases"], {})


class SavedFixes(Base):
    def test_missing_x86_runtime_becomes_a_saved_fix(self):
        tips = server.sandbox_hints(self.sb(), 2, RT_FAIL, "build", time.time(), "make package")
        s = self.sb()
        self.assertEqual(len(s["fixes"]), 1)
        cmd = s["fixes"][0]["command"]
        self.assertIn("libclang-rt-21-dev_${v}_amd64.deb", cmd)
        self.assertIn("llvm-toolchain-21", cmd)
        self.assertIn('[ -L "$f" ]', cmd)                      # also replaces a wrong link to the ARM file
        for p in ("curl", "libclang-rt-21-dev"):
            self.assertIn(p, r.apt_packages(s["setup_command"]))
        self.assertIn("Never link the aarch64 file", tips)
        # Every setup step redoes it, after the packages.
        setup = sandbox.step_command(s, "setup")
        self.assertTrue(setup.startswith("apt-get update"))
        self.assertIn(cmd, setup)
        # The same failure again doesn't save it twice.
        server.sandbox_hints(self.sb(), 2, RT_FAIL, "build", time.time(), "make package")
        self.assertEqual(len(self.sb()["fixes"]), 1)

    def test_wrong_processor_link_is_called_out_and_refused(self):
        cmd = ("ln -sf /usr/lib/llvm-21/lib/clang/21/lib/linux/libclang_rt.builtins-aarch64.a "
               "/usr/lib/llvm-21/lib/clang/21/lib/linux/libclang_rt.builtins-x86_64.a")
        tips = server.sandbox_hints(self.sb(), 0, "", None, time.time(), cmd)
        self.assertIn("broken program", tips)
        with self.assertRaises(server.ToolError):
            server.t_sandbox_fix(types.SimpleNamespace(), self.events.append,
                                 {"sandbox": "EglantinePS5", "command": cmd, "why": "x"})

    def test_apt_install_by_hand_is_added_to_the_setup(self):
        tips = server.sandbox_hints(self.sb(), 0, "", None, time.time(),
                                    "apt-get update && apt-get install -y llvm xxd && llvm-objcopy --version")
        self.assertIn("llvm", r.apt_packages(self.sb()["setup_command"]))
        self.assertIn("xxd", r.apt_packages(self.sb()["setup_command"]))
        self.assertIn("a reset or new container installs them again", tips)

    def test_other_system_changes_are_flagged_as_not_saved(self):
        tips = server.sandbox_hints(self.sb(), 0, "", None, time.time(), "cp /tmp/x/libfoo.a /usr/lib/libfoo.a")
        self.assertIn("sandbox_fix", tips)

    def test_sandbox_fix_runs_then_saves(self):
        s = self.sb()
        s["network"] = True
        sandbox.save(s)
        with mock.patch.object(sandbox, "run", return_value=(0, "")) as run:
            out = server.t_sandbox_fix(types.SimpleNamespace(), self.events.append,
                                       {"sandbox": "EglantinePS5", "command": "cp /a /usr/lib/a", "why": "the lib"})
        self.assertEqual(run.call_args[0][1], "cp /a /usr/lib/a")
        self.assertIn("saved", out)
        self.assertEqual(self.sb()["fixes"][0]["why"], "the lib")
        with mock.patch.object(sandbox, "run", return_value=(1, "nope")):
            out = server.t_sandbox_fix(types.SimpleNamespace(), self.events.append,
                                       {"sandbox": "EglantinePS5", "command": "false", "why": "x"})
        self.assertIn("Not saved", out)
        self.assertEqual(len(self.sb()["fixes"]), 1)
        server.t_sandbox_fix(types.SimpleNamespace(), self.events.append, {"sandbox": "EglantinePS5", "remove": 1})
        self.assertEqual(self.sb()["fixes"], [])

    def test_reset_keeps_fixes_and_the_next_build_runs_setup_first(self):
        sandbox.set_alias("EglantinePS5", "objcopy", "llvm-objcopy")
        sandbox.add_fix("EglantinePS5", "true", "test fix")
        with mock.patch.object(sandbox, "remove_container"):
            s = sandbox.reset("EglantinePS5")
        self.assertEqual(s["tool_aliases"], {"objcopy": "llvm-objcopy"})
        self.assertEqual(len(s["fixes"]), 1)
        self.assertTrue(s["needs_setup"])
        steps = []

        def fake_run(st, emit, a):
            steps.append(a["_step"])
            if a["_step"] == "setup":
                server.sandbox_hints(sandbox.load("EglantinePS5"), 0, "", "setup")
            return "ran " + a["_step"]
        s["network"] = True
        sandbox.save(s)
        with mock.patch.object(server, "t_sandbox_run", fake_run), \
                mock.patch.object(server, "complete_setup", lambda s: (s, "")), \
                mock.patch.object(sandbox, "preflight", return_value={"ok": True}):
            out = server.t_sandbox_build(types.SimpleNamespace(), self.events.append,
                                         {"sandbox": "EglantinePS5", "step": "build"})
        self.assertEqual(steps, ["setup", "build"])
        self.assertIn("ran build", out)
        self.assertFalse(self.sb()["needs_setup"])


class HalfMadeFiles(Base):
    def test_a_file_written_just_before_a_failure_is_removed_before_the_next_build(self):
        w = sandbox.work(self.sb())
        lib = w / "build" / "radv-isolated" / "libvulkan_radeon.ps5.a"
        lib.parent.mkdir(parents=True)

        def failing(argv, cwd, env, emit, timeout, stop, on_kill=None):
            lib.write_bytes(b"!<arch>\nnot renamed")
            (w / "tools" / "isolate-radv.py").write_text("print('isolate')\n")   # a project file: never removed
            return 2, OBJCOPY_FAIL
        with mock.patch.object(sandbox, "ensure_container", return_value=("docker", "docker", "c")), \
                mock.patch.object(sandbox, "_stream", failing):
            sandbox.run("EglantinePS5", "make package")
        self.assertEqual([x["path"] for x in self.sb()["suspect"]], ["build/radv-isolated/libvulkan_radeon.ps5.a"])
        tips = server.sandbox_hints(self.sb(), 2, OBJCOPY_FAIL, "build", time.time(), "make package")
        self.assertIn("half-made", tips)
        # A look (not a build) leaves it alone.
        with mock.patch.object(sandbox, "ensure_container", return_value=("docker", "docker", "c")), \
                mock.patch.object(sandbox, "_stream", return_value=(0, "")):
            sandbox.run("EglantinePS5", "ls build")
            self.assertTrue(lib.exists())
            sandbox.run("EglantinePS5", "make clean; make -j8", self.events.append)
        self.assertFalse(lib.exists())
        self.assertTrue((w / "tools" / "isolate-radv.py").exists())
        self.assertEqual(self.sb()["suspect"], [])
        self.assertTrue(any("half-made" in e.get("text", "") for e in self.events))
        self.assertTrue(any("Removed build/radv-isolated" in n["text"] for n in self.sb()["notes"]))

    def test_a_changed_file_is_kept(self):
        w = sandbox.work(self.sb())
        f = w / "build" / "out.a"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"half")
        sandbox.note_suspects("EglantinePS5", time.time())
        f.write_bytes(b"made again, longer")
        self.assertEqual(sandbox.clear_suspects("EglantinePS5"), [])
        self.assertTrue(f.exists())

    def test_a_big_compile_that_stopped_isnt_treated_as_half_made(self):
        w = sandbox.work(self.sb())
        (w / "build").mkdir()
        for i in range(sandbox.SUSPECT_MAX + 5):
            (w / "build" / ("%d.o" % i)).write_bytes(b"o")
        self.assertEqual(sandbox.note_suspects("EglantinePS5", time.time()), [])


class JoshuasExistingSandbox(Base):
    def test_last_failed_run_from_before_is_caught_up(self):
        # His sandbox now: the last run (make clean; make -j8) failed at objcopy and left the unrenamed library.
        w = sandbox.work(self.sb())
        lib = w / "build" / "radv-isolated" / "libvulkan_radeon.ps5.a"
        lib.parent.mkdir(parents=True)
        lib.write_bytes(b"unrenamed")
        s = self.sb()
        s["last_run"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "command": "make clean 2>/dev/null; make -j8",
                         "code": 2, "output": OBJCOPY_FAIL}
        s["network"] = True
        sandbox.save(s)
        steps = []

        def fake_build(st, emit, a):
            steps.append(a["step"])
            cur = sandbox.load("EglantinePS5")
            self.assertEqual(cur["tool_aliases"]["objcopy"], "llvm-objcopy")
            self.assertEqual([x["path"] for x in cur["suspect"]], ["build/radv-isolated/libvulkan_radeon.ps5.a"])
            cur["needs_setup"] = False
            sandbox.save(cur)
            return "setup done"
        # A plain make runs the setup first by itself (it installs llvm), then the command: no refusal to loop on.
        with mock.patch.object(server, "t_sandbox_build", fake_build), \
                mock.patch.object(sandbox, "preflight", return_value={"brew": [], "colima": None, "stuck": None}), \
                mock.patch.object(sandbox, "run", return_value=(0, "built")) as run, \
                mock.patch.object(server, "show_output", return_value=("", "ok")):
            server.t_sandbox_run(types.SimpleNamespace(), self.events.append,
                                 {"sandbox": "EglantinePS5", "command": "make -j8"})
        self.assertEqual(steps, ["setup"])
        self.assertEqual(run.call_args[0][1], "make -j8")
        self.assertTrue(any("From the last run" in e.get("text", "") for e in self.events))
        # Once is enough.
        _, words = server.catch_up(self.sb())
        self.assertEqual(words, "")


class Notebook(Base):
    def test_notes_go_at_the_end_of_every_step_and_to_claude(self):
        st = types.SimpleNamespace(messages=[{"role": "user", "content": "build it"}])
        server.t_sandbox_note(st, self.events.append, {"sandbox": "EglantinePS5",
                                                       "text": "The result is build/release/PPSA98515."})
        sandbox.set_alias("EglantinePS5", "objcopy", "llvm-objcopy")
        msgs = server.with_notes([{"role": "user", "content": "q"}, {"role": "tool", "content": "result"}], st)
        self.assertTrue(msgs[-1]["content"].startswith("result"))
        self.assertIn("build/release/PPSA98515", msgs[-1]["content"])
        self.assertIn("objcopy = llvm-objcopy", msgs[-1]["content"])
        self.assertIn("don't redo or undo", msgs[-1]["content"])
        with mock.patch.object(server, "claude_scrubber", return_value=types.SimpleNamespace(scrub=lambda t: t)):
            text, _ = server.claude_question(st, "what next?", "", "stuck")
        self.assertIn("build/release/PPSA98515", text)
        # A chat that never used a sandbox gets nothing added.
        other = types.SimpleNamespace(messages=[])
        self.assertEqual(server.with_notes([{"role": "user", "content": "q"}], other)[-1]["content"], "q")

    def test_made_up_make_target_is_answered_with_the_real_one(self):
        s = sandbox.set_recipe("EglantinePS5", {"output": "build/release/PPSA98515"})
        tips = server.sandbox_hints(s, 2, "make: *** No rule to make target 'dist/EglantinePS5.self'.  Stop.",
                                    None, time.time(), "make dist/EglantinePS5.self")
        self.assertIn("no make target called dist/EglantinePS5.self", tips)
        self.assertIn("build/release/PPSA98515", tips)


class ProjectEditOnAFolder(unittest.TestCase):
    def test_empty_path_is_a_plain_error_not_a_crash(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root))
        st = types.SimpleNamespace(cfg=lambda: {})
        with mock.patch.object(server, "project", return_value={"name": "EglantinePS5"}), \
                mock.patch.object(server.projects, "inside", return_value=(root, root)):
            with self.assertRaises(server.ToolError) as e:
                server.t_project_edit(st, None, {"project": "EglantinePS5", "path": "", "reason": "x"})
        self.assertIn("path of a file", str(e.exception))


if __name__ == "__main__":
    unittest.main()
