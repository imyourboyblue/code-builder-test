"""
x86 projects in an ARM sandbox (from Joshua's EglantinePS5 log, 2026-11-03). tools/check-startup-performance.py builds
a small x86-only program for the build machine itself (clang++-18 -DARCHITECTURE_x86_64, cpuid.h, gregs[REG_RIP]). On
the M4 Mac the sandbox is ARM Linux, so it failed after a 7-minute build, then the AI hunted a package that doesn't
exist (clang-18-x86-64-linux-gnu). Now PatchBay spots it (early, from the files, and from the errors), offers ONE card
to make the sandbox x86 (amd64) through Rosetta, and Colima starts with Rosetta when an x86 sandbox wants it.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

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

X86_LOG = """In file included from /work/headless/performance.cpp:14:
/usr/lib/llvm-18/lib/clang/18/include/cpuid.h:14:2: error: this header is for x86 only
/usr/lib/llvm-18/lib/clang/18/include/cpuid.h:298:5: error: invalid output constraint '=a' in asm
/work/src/fastmem.h:48:94: error: no member named 'gregs' in 'mcontext_t'; did you mean 'regs'?
/work/src/fastmem.h:48:100: error: use of undeclared identifier 'REG_RIP'
make: *** [Makefile:39: package] Error 1
"""
CHECK_PY = """import subprocess
subprocess.run(['clang++-18', '-std=c++20', '-O2', '-pthread', '-DARCHITECTURE_x86_64',
                '-I', str(root / 'headless'), '-o', str(executable)], check=True)
"""
RT_FIX = ('f=/usr/lib/llvm-18/lib/clang/18/lib/linux/libclang_rt.builtins-x86_64.a; if [ ! -f "$f" ]; then :; fi')


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.base = Path(self._t.name).resolve()
        self.proj = self.base / "EglantinePS5"
        (self.proj / "tools").mkdir(parents=True)
        (self.proj / "src").mkdir()
        (self.proj / "Makefile").write_text("package:\n\tbash tools/prepare-build.sh\n")
        # A PS5 cross-compile: x86 too, but fine on ARM (it names its target).
        (self.proj / "tools" / "ps5.cmake").write_text("set(CMAKE_C_COMPILER clang-18)\n"
                                                       "set(CMAKE_C_COMPILER_TARGET x86_64-sie-ps5)\n")
        (self.proj / "tools" / "build.sh").write_text("clang-18 --target=x86_64-sie-ps5 -mavx2 -c a.c\n")
        for p in (mock.patch.object(sandbox, "ROOT", self.base / "sandboxes"),
                  mock.patch.object(sandbox, "colima_home", return_value=self.base / ".colima"),
                  mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(self.base)})):
            p.start()
            self.addCleanup(p.stop)
        sandbox.create("EglantinePS5", str(self.proj), "linux",
                       recipe={"setup_command": "apt-get update && apt-get install -y make",
                               "build_command": "make package"})
        self.events = []
        self.st = types.SimpleNamespace(pending={}, messages=[], home=False)

    def sb(self):
        return sandbox.load("EglantinePS5")

    def x86_files(self):
        work = sandbox.work(self.sb())
        (work / "tools" / "check-startup-performance.py").write_text(CHECK_PY)
        (work / "src" / "fastmem.h").write_text("auto f(void* c) {\n  return ((ucontext_t*)c)->uc_mcontext."
                                                "gregs[REG_RIP];\n}\n")

    def cards(self):
        return [e for e in self.events if e.get("type") == "confirm"]

    def colima_yaml(self, vm="vz", rosetta=False):
        d = self.base / ".colima" / "default"
        d.mkdir(parents=True, exist_ok=True)
        (d / "colima.yaml").write_text("cpu: 8\nvmType: %s\nrosetta: %s\n" % (vm, "true" if rosetta else "false"))


class EarlyCheck(Base):
    def test_finds_x86_only_build_machine_code_but_not_the_ps5_cross_compile(self):
        self.assertEqual(r.x86_signs(sandbox.work(self.sb())), [])
        self.x86_files()
        signs = r.x86_signs(sandbox.work(self.sb()))
        self.assertTrue(any("check-startup-performance.py:2" in x and "-DARCHITECTURE_x86_64" in x for x in signs))
        self.assertTrue(any("fastmem.h:2" in x and "REG_RIP" in x for x in signs))

    def test_build_shows_the_x86_card_before_building(self):
        self.x86_files()
        with mock.patch.object(server, "t_sandbox_run") as run, \
                mock.patch.object(sandbox, "engine", return_value=("docker", "/opt/homebrew/bin/docker")), \
                mock.patch.object(sandbox, "_which", return_value=None):
            out = server.t_sandbox_build(self.st, self.events.append, {"sandbox": "EglantinePS5", "step": "build"})
        run.assert_not_called()
        self.assertIn("Not built yet", out)
        card = self.cards()[0]
        self.assertIn("x86", card["title"])
        self.assertIn("check-startup-performance.py", card["diff"])
        self.assertIn("slower than ARM", card["diff"])
        self.assertTrue(self.sb()["x86_checked"])
        self.assertEqual(self.sb()["arch"], "native")          # nothing changes before the card

    def test_checked_only_once(self):
        with mock.patch.object(r, "x86_signs", return_value=[]) as signs:
            self.assertEqual(server.x86_early_check(self.st, self.events.append, self.sb()), "")
            self.assertEqual(server.x86_early_check(self.st, self.events.append, self.sb()), "")
        self.assertEqual(signs.call_count, 1)


class FromErrors(Base):
    def test_x86_errors_are_spotted_and_remembered(self):
        lines = server.x86_errors(self.sb(), 2, X86_LOG)
        self.assertTrue(any("this header is for x86 only" in x for x in lines))
        self.assertTrue(self.sb()["x86_needed"])
        # Not on an x86 sandbox, and not for a PS5 cross-compile error.
        self.assertEqual(server.x86_errors(dict(self.sb(), arch="amd64"), 2, X86_LOG), [])
        self.assertEqual(server.x86_errors(self.sb(), 2, "a.c:1:1: error: unknown type name 'foo'"), [])

    def test_hunting_x86_packages_is_held_and_points_to_the_card(self):
        server.x86_errors(self.sb(), 2, X86_LOG)
        with mock.patch.object(sandbox, "engine", return_value=("docker", "/opt/homebrew/bin/docker")), \
                mock.patch.object(sandbox, "_which", return_value=None):
            held = server.x86_hold(self.st, self.events.append, self.sb(),
                                   "apt-get install -y clang-18-x86-64-linux-gnu")
            self.assertIn("Not run", held)
            self.assertEqual(len(self.cards()), 1)
            again = server.x86_hold(self.st, self.events.append, self.sb(), "make 2>&1 | tail -100")
        self.assertIn("already on the page", again)
        self.assertEqual(len(self.cards()), 1)                 # not a second card
        self.assertEqual(server.x86_hold(self.st, self.events.append, self.sb(), "cat tools/ps5.cmake"), "")

    def test_no_such_x86_package_tip(self):
        tips = server.sandbox_hints(self.sb(), 100, "E: Unable to locate package clang-18-x86-64-linux-gnu",
                                    None, time.time(), "apt-get install -y clang-18-x86-64-linux-gnu")
        self.assertIn("There is no package clang-18-x86-64-linux-gnu", tips)

    def test_saying_no_keeps_arm_and_stops_asking(self):
        server.x86_errors(self.sb(), 2, X86_LOG)
        with mock.patch.object(sandbox, "engine", return_value=("docker", "/opt/homebrew/bin/docker")), \
                mock.patch.object(sandbox, "_which", return_value=None):
            server.x86_hold(self.st, self.events.append, self.sb(), "make")
        pid = self.cards()[0]["id"]
        server.do_cancel(self.st, pid, self.events.append)
        self.assertTrue(self.sb()["x86_declined"])
        self.assertEqual(self.sb()["arch"], "native")
        self.assertEqual(server.x86_hold(self.st, self.events.append, self.sb(), "make"), "")

    def test_settings_arch_amd64_is_a_card_not_immediate(self):
        with mock.patch.object(sandbox, "engine", return_value=("docker", "/opt/homebrew/bin/docker")), \
                mock.patch.object(sandbox, "_which", return_value=None):
            out = server.t_sandbox_settings(self.st, self.events.append, {"sandbox": "EglantinePS5", "arch": "amd64"})
        self.assertIn("card", out)
        self.assertEqual(self.sb()["arch"], "native")
        self.assertEqual(self.cards()[0]["where"]["changes_on"].split(" ")[0], "Sandbox")


class Switch(Base):
    def test_switch_drops_the_arm_only_fix_keeps_swaps_and_turns_on_rosetta(self):
        sandbox.set_alias("EglantinePS5", "objcopy", "llvm-objcopy")
        sandbox.add_fix("EglantinePS5", RT_FIX, "the x86 libclang_rt.builtins (clang 18) from Ubuntu's x86 package")
        sandbox.add_fix("EglantinePS5", "ln -sf /a /usr/lib/a", "something else")
        server.x86_errors(self.sb(), 2, X86_LOG)
        with mock.patch.object(sandbox, "engine", return_value=("docker", "/opt/homebrew/bin/docker")), \
                mock.patch.object(sandbox, "_which", return_value="/opt/homebrew/bin/colima"), \
                mock.patch.object(sandbox, "rosetta_plan", return_value={"ok": False, "steps": [], "commands": []}), \
                mock.patch.object(sandbox, "enable_rosetta", return_value="Colima is running with Rosetta.") as ros, \
                mock.patch.object(sandbox, "remove_container"):
            words = server.switch_to_x86("EglantinePS5", self.events.append)
        ros.assert_called_once()
        s = self.sb()
        self.assertEqual(s["arch"], "amd64")
        self.assertEqual([f["why"] for f in s["fixes"]], ["something else"])
        self.assertEqual(s["tool_aliases"]["objcopy"], "llvm-objcopy")
        self.assertTrue(s["needs_setup"])
        self.assertNotIn("x86_needed", s)
        self.assertIn("Rosetta", words)
        self.assertTrue(any("Switched to x86" in n["text"] for n in s["notes"]))


class ColimaRosetta(Base):
    def test_start_argv_adds_rosetta_only_when_an_x86_sandbox_wants_it(self):
        argv = sandbox.colima_start_argv("colima", 14, 36)
        self.assertNotIn("--vz-rosetta", argv)
        s = self.sb()
        s["arch"] = "amd64"
        sandbox.save(s)
        argv = sandbox.colima_start_argv("colima", 14, 36)
        self.assertEqual(argv[:8], ["colima", "start", "--cpu", "8", "--memory", "20", "--disk", "100"])
        self.assertEqual(argv[8:], ["--vm-type", "vz", "--vz-rosetta"])

    def test_keeps_rosetta_if_colima_already_has_it(self):
        self.colima_yaml("vz", True)
        self.assertIn("--vz-rosetta", sandbox.colima_start_argv("colima", 14, 36))

    def test_colima_config(self):
        self.assertFalse(sandbox.colima_config()["exists"])
        self.colima_yaml("qemu", True)
        c = sandbox.colima_config()
        self.assertEqual(c["vm_type"], "qemu")
        self.assertFalse(c["rosetta"])                 # QEMU can't use Rosetta
        self.assertTrue(sandbox.colima_needs_new_vm())

    def plan(self, running, vm="vz", installed=True):
        self.colima_yaml(vm, False)
        with mock.patch.object(sandbox, "rosetta_installed", return_value=installed), \
                mock.patch.object(sandbox, "colima_info", return_value={"running": running}), \
                mock.patch.object(sandbox, "colima_size", return_value=(8, 20, 100)):
            return sandbox.rosetta_plan()

    def test_plan_words(self):
        p = self.plan(True)
        self.assertEqual(p["commands"], ["colima stop", "colima start --cpu 8 --memory 20 --disk 100 --vm-type vz "
                                                        "--vz-rosetta"])
        p = self.plan(True, vm="qemu", installed=False)
        self.assertTrue(p["delete"])
        self.assertIn("softwareupdate --install-rosetta --agree-to-license", p["commands"])
        self.assertIn("colima delete --force", p["commands"])

    def test_preflight_asks_for_rosetta_for_an_x86_sandbox(self):
        self.colima_yaml("vz", False)
        s = self.sb()
        s["arch"] = "amd64"
        sandbox.save(s)
        which = {"docker": "/opt/homebrew/bin/docker", "colima": "/opt/homebrew/bin/colima"}

        def quiet(argv, timeout=60):
            if argv[1] == "info":
                return 0, "8 %d" % (20 * 1024 ** 3) if "--format" in argv else "ok"
            if argv[1] == "context":
                return 0, "colima"
            return 0, ""
        with mock.patch.object(sandbox, "engine", return_value=("docker", which["docker"])), \
                mock.patch.object(sandbox, "_which", side_effect=which.get), \
                mock.patch.object(sandbox, "_quiet", side_effect=quiet), \
                mock.patch.object(sandbox, "rosetta_installed", return_value=True), \
                mock.patch.object(sandbox, "colima_size", return_value=(8, 20, 100)):
            pf = sandbox.preflight(self.sb())
            self.assertEqual(pf["colima"], "rosetta")
            self.assertFalse(pf["ok"])
            out = server.sandbox_preflight(self.st, self.events.append, self.sb(), pf)
            self.colima_yaml("vz", True)
            self.assertTrue(sandbox.preflight(self.sb())["ok"])
            # An ARM sandbox never asks for it.
            s["arch"] = "native"
            sandbox.save(s)
            self.colima_yaml("vz", False)
            self.assertTrue(sandbox.preflight(self.sb())["ok"])
        self.assertIn("card", out)
        self.assertIn("--vz-rosetta", self.cards()[0]["diff"])
        self.assertEqual(self.cards()[0]["title"], "Turn on Rosetta in Colima (for x86 sandboxes)")


if __name__ == "__main__":
    unittest.main()
