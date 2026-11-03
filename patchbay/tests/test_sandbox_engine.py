"""
Tests for the sandbox setup fixes: right Homebrew names, Colima sized for the Mac, sandboxes fit Docker's machine,
the project's preset setup is kept, and missing programs are added to the setup.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import projects  # noqa: E402
import sandbox  # noqa: E402
import sandboxrecipe  # noqa: E402


class BrewNames(unittest.TestCase):
    def test_docker_cli_becomes_docker(self):
        with mock.patch.object(projects, "find_brew", return_value="/opt/homebrew/bin/brew"):
            cmds, admin, words = projects.install_plan("homebrew", ["colima", "docker-cli"])
        self.assertIn("install colima docker", cmds[0])
        self.assertNotIn("docker-cli", " ".join(cmds))
        self.assertFalse(admin)

    def test_install_help_names_real_formulas(self):
        self.assertNotIn("docker-cli", sandbox.INSTALL_HELP)
        self.assertEqual(sandbox.ENGINE_BREW, ["colima", "docker"])


class ColimaSize(unittest.TestCase):
    def test_36_gb_mac_gets_about_8_cpus_and_20_gb(self):
        cpus, mem, disk = sandbox.colima_size(14, 36)
        self.assertEqual((cpus, mem), (8, 20))
        self.assertGreaterEqual(disk, 80)

    def test_small_mac_keeps_room_for_macos(self):
        cpus, mem, _ = sandbox.colima_size(8, 16)
        self.assertLessEqual(mem, 8)
        self.assertGreaterEqual(cpus, 2)

    def test_start_command_is_sized(self):
        argv = sandbox.colima_start_argv("/opt/homebrew/bin/colima", 14, 36)
        self.assertEqual(argv, ["/opt/homebrew/bin/colima", "start", "--cpu", "8", "--memory", "20", "--disk", "100"])


class FitToEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = mock.patch.object(sandbox, "ROOT", Path(self.tmp.name) / "sandboxes")
        self.root.start()
        proj = Path(self.tmp.name) / "p"
        proj.mkdir()
        (proj / "Makefile").write_text("all:\n\techo hi\n")
        self.sb = sandbox.create("p", str(proj), "linux", recipe={"cpus": 4, "memory_gb": 8})

    def tearDown(self):
        self.root.stop()
        self.tmp.cleanup()

    def test_lowered_to_what_docker_has(self):
        with mock.patch.object(sandbox, "docker_capacity", return_value=(2, 2.0)), \
                mock.patch.object(sandbox, "uses_colima", return_value=True):
            note = sandbox.fit_to_engine("docker", "docker", self.sb)
        self.assertIn("2 CPUs", note)
        self.assertIn("sandbox_preflight", note)
        saved = sandbox.load("p")
        self.assertEqual(saved["cpus"], 2)
        self.assertEqual(saved["memory_gb"], 1)
        self.assertEqual(sandbox.step_command(dict(saved, build_command="make -j{cpus}"), "build"), "make -j2")

    def test_no_change_when_it_fits(self):
        with mock.patch.object(sandbox, "docker_capacity", return_value=(8, 20.0)):
            self.assertEqual(sandbox.fit_to_engine("docker", "docker", self.sb), "")
        self.assertEqual(sandbox.fit_to_engine("apple", "container", self.sb), "")


class Preflight(unittest.TestCase):
    def test_missing_docker_and_colima_go_on_one_brew_list(self):
        with mock.patch.object(sandbox, "engine", return_value=(None, None)), \
                mock.patch.object(sandbox, "_which", return_value=None), \
                mock.patch.object(sandbox.Path, "exists", return_value=False):
            pf = sandbox.preflight()
        self.assertEqual(pf["brew"], ["docker", "colima"])
        self.assertFalse(pf["ok"])

    def test_small_colima_asks_for_resize(self):
        which = {"docker": "/opt/homebrew/bin/docker", "colima": "/opt/homebrew/bin/colima"}
        with mock.patch.object(sandbox, "engine", return_value=("docker", which["docker"])), \
                mock.patch.object(sandbox, "_which", side_effect=which.get), \
                mock.patch.object(sandbox, "_quiet", return_value=(0, "")), \
                mock.patch.object(sandbox, "docker_capacity", return_value=(2, 2.0)), \
                mock.patch.object(sandbox, "uses_colima", return_value=True):
            pf = sandbox.preflight({"cpus": 8, "memory_gb": 16})
        self.assertEqual(pf["colima"], "resize")
        self.assertEqual(pf["brew"], [])

    def test_stopped_colima_asks_for_start(self):
        which = {"docker": "/opt/homebrew/bin/docker", "colima": "/opt/homebrew/bin/colima"}
        with mock.patch.object(sandbox, "engine", return_value=("docker", which["docker"])), \
                mock.patch.object(sandbox, "_which", side_effect=which.get), \
                mock.patch.object(sandbox, "_quiet", return_value=(1, "Cannot connect")):
            pf = sandbox.preflight()
        self.assertEqual(pf["colima"], "start")


class SetupRecipe(unittest.TestCase):
    PRESET = ("export DEBIAN_FRONTEND=noninteractive && apt-get update && apt-get install -y --no-install-recommends "
              "git make build-essential && bash tools/ci/setup-ubuntu-24.04.sh 3>/root/.patchbay-build-env")

    def test_hand_written_setup_is_added_to_the_preset(self):
        got = sandboxrecipe.merge_setup(self.PRESET, "apt-get update && apt-get install -y make gcc pkg-config")
        self.assertIn("git", got)
        self.assertIn("gcc", got)
        self.assertIn("pkg-config", got)
        self.assertIn("setup-ubuntu-24.04.sh", got)
        self.assertEqual(got.count("apt-get install"), 1)

    def test_extra_steps_run_after_the_preset(self):
        got = sandboxrecipe.merge_setup(self.PRESET, "pip install meson")
        self.assertTrue(got.startswith(self.PRESET))
        self.assertTrue(got.endswith("&& pip install meson"))

    def test_missing_programs_are_found(self):
        out = ("git submodule update --init Eglantine\n/bin/bash: line 1: git: command not found\n"
               "sh: 1: make: not found\nzsh:1: command not found: ninja")
        self.assertEqual(sandboxrecipe.missing_commands(out), ["git", "make", "ninja"])
        pk, unknown = sandboxrecipe.packages_for(["git", "gcc", "ninja", "weirdtool"])
        self.assertEqual(pk, ["git", "build-essential", "ninja-build"])
        self.assertEqual(unknown, ["weirdtool"])

    def test_add_packages(self):
        self.assertIn("install -y --no-install-recommends curl git",
                      sandboxrecipe.add_packages(self.PRESET, ["curl", "git"]))
        self.assertTrue(sandboxrecipe.add_packages("", ["git"]).endswith("install -y --no-install-recommends git"))

    def test_preset_has_the_basics(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Makefile").write_text("all:\n\tgit submodule update --init x\n")
            r = sandboxrecipe.suggest(d, cores=14, mac_memory_gb=36)
        for pkg in ("git", "build-essential", "curl", "ca-certificates", "python3", "pkgconf"):
            self.assertIn(pkg, r["setup_command"])


if __name__ == "__main__":
    unittest.main()
