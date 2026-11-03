"""
Tests for sandbox recipes (sandboxrecipe.py) and what they add to sandboxes: working out the right Linux from a
project's own files, folder copy-out with undo, commands that must never run inside, and x86 (Rosetta) mode.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sandbox  # noqa: E402
import sandboxrecipe  # noqa: E402

MAKEFILE = """# Builds on Linux (Ubuntu 26.04).
JOBS ?= $(shell nproc)
export EDEN_BUILD_JOBS := $(JOBS)
PS5_HOST ?=
PS5_PORT ?= 2121
PYTHON := python3 -B
RELEASE_APP := build/release/PPSA98515

release: package
\techo release

install: ## Build, then copy to a console over FTP (PS5_HOST=<address>)
\t@[[ -n "$(PS5_HOST)" ]] || { echo 'Set PS5_HOST'; exit 2; }
\t$(MAKE) package
\t$(PYTHON) tools/install-ftp.py $(PS5_HOST) $(RELEASE_APP) --port $(PS5_PORT)
"""


class RecipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.proj = self.base / "EglantinePS5"
        (self.proj / "tools" / "ci").mkdir(parents=True)
        (self.proj / "Makefile").write_text(MAKEFILE)
        (self.proj / "tools" / "ci" / "ubuntu-packages.txt").write_text("# host tools\nclang-21 lld-21\nmeson\n")
        (self.proj / "tools" / "build-package.sh").write_text('jobs=${RADV_BUILD_JOBS:-24}\n')
        (self.proj / "README.md").write_text("A Switch 2 emulator port. Builds Mesa RADV.\n")
        self.root = mock.patch.object(sandbox, "ROOT", self.base / "sandboxes")
        self.root.start()

    def tearDown(self):
        self.root.stop()
        self.tmp.cleanup()

    def test_reads_the_projects_own_linux_and_packages(self):
        r = sandboxrecipe.suggest(self.proj, cores=14, mac_memory_gb=36)
        self.assertEqual(r["image"], "ubuntu:26.04")
        self.assertIn("clang-21", r["setup_command"])
        self.assertIn("meson", r["setup_command"])
        self.assertEqual(r["output"], "build/release/PPSA98515")
        self.assertIn("make JOBS={cpus}", r["build_command"])
        self.assertIn("RADV_BUILD_JOBS={cpus}", r["build_command"])
        sb = sandbox.create("eg", str(self.proj), recipe=dict(r, cpus=6))
        self.assertIn("RADV_BUILD_JOBS=6 make JOBS=6", sandbox.step_command(sb, "build"))
        self.assertNotIn("EDEN_BUILD_JOBS=", r["build_command"])      # the Makefile already sets it
        self.assertEqual(r["memory_gb"], 16)                           # big build (Mesa, emulator)
        self.assertEqual(r["memory_max"], 20)
        self.assertEqual(r["arch"], "native")

    def test_device_upload_is_blocked_inside_and_moved_to_the_mac(self):
        r = sandboxrecipe.suggest(self.proj, cores=8)
        pats = [b["pattern"] for b in r["blocked"]]
        self.assertTrue(any("install" in p for p in pats))
        self.assertIn("tools/install-ftp.py <device address> build/release/PPSA98515 --port 2121", r["upload_hint"])
        sb = sandbox.create("eg", str(self.proj), recipe=r)
        for bad in ("make install PS5_HOST=192.168.50.203", "cd x && make  install",
                    "python3 tools/install-ftp.py 1.2.3.4 build/release/PPSA98515"):
            with self.assertRaises(sandbox.SandboxError, msg=bad):
                sandbox.run("eg", bad)
        # Recipes can add blocks but never take them away.
        sb = sandbox.set_recipe("eg", {"blocked": [], "build_command": "make package"})
        self.assertEqual(len(sb["blocked"]), 2)
        self.assertEqual(sb["build_command"], "make package")

    def test_older_project_uses_its_ci_setup_script(self):
        (self.proj / "Makefile").write_text(MAKEFILE.replace("26.04", "24.04"))
        (self.proj / "tools" / "ci" / "ubuntu-packages.txt").unlink()
        (self.proj / "tools" / "ci" / "ubuntu-24.04-packages.txt").write_text("clang-18\n")
        (self.proj / "tools" / "ci" / "setup-ubuntu-24.04.sh").write_text(
            'prefix=${1:?the folder}\nsudo apt-get install cmake\n')
        wf = self.proj / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "release.yml").write_text("jobs:\n  b:\n    runs-on: ubuntu-24.04\n    steps:\n"
                                        "      - run: tools/ci/setup-ubuntu-24.04.sh \"${{ runner.temp }}/spirv\" "
                                        "3>> \"$GITHUB_ENV\"\n")
        r = sandboxrecipe.suggest(self.proj, cores=8)
        self.assertEqual(r["image"], "ubuntu:24.04")
        self.assertIn("bash tools/ci/setup-ubuntu-24.04.sh", r["setup_command"])
        self.assertIn("3>%s" % sandboxrecipe.ENV_FILE, r["setup_command"])
        self.assertNotIn("GITHUB", r["setup_command"])
        self.assertIn("clang-18", r["setup_command"])
        self.assertIn(" cmake", r["setup_command"])
        self.assertTrue(r["build_command"].startswith("set -a;"))      # loads what the setup script printed

    def test_other_kinds_of_project(self):
        other = self.base / "tool"
        other.mkdir()
        (other / "CMakeLists.txt").write_text("project(x)\n")
        r = sandboxrecipe.suggest(other, cores=4)
        self.assertIn("cmake --build", r["build_command"])
        self.assertEqual(r["memory_gb"], 8)
        (other / "Dockerfile").write_text("FROM debian:12\nRUN apt-get install -y libfoo-dev\n")
        r = sandboxrecipe.suggest(other, cores=4)
        self.assertEqual(r["image"], "debian:12")
        self.assertIn("libfoo-dev", r["setup_command"])

    def test_memory_is_capped_to_fit_the_mac(self):
        r = sandboxrecipe.suggest(self.proj, cores=8, mac_memory_gb=16)
        self.assertLessEqual(r["memory_max"], 4)
        sb = sandbox.create("eg", str(self.proj), recipe=sandboxrecipe.suggest(self.proj, mac_memory_gb=36))
        with self.assertRaises(sandbox.SandboxError):
            sandbox.set_options("eg", memory_gb=24)                     # over this sandbox's 20 GB
        self.assertEqual(sandbox.set_options("eg", memory_gb=20)["memory_gb"], 20)

    def test_x86_mode_through_rosetta(self):
        sb = sandbox.create("eg", str(self.proj), recipe=sandboxrecipe.suggest(self.proj))
        self.assertNotIn("--platform", sandbox.create_args("docker", "docker", sb))
        sb = sandbox.set_options("eg", arch="amd64")
        argv = sandbox.create_args("docker", "docker", sb)
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/amd64")
        sb = sandbox.set_options("eg", network=True)
        argv = sandbox.create_args("apple", "container", sb)
        self.assertEqual(argv[argv.index("--arch") + 1], "amd64")
        with self.assertRaises(sandbox.SandboxError):
            sandbox.set_options("eg", arch="sparc")
        self.assertIn("Rosetta", sandbox.summary(sb))

    def test_folder_copy_out_and_undo(self):
        sb = sandbox.create("eg", str(self.proj), recipe=sandboxrecipe.suggest(self.proj))
        app = sandbox.work(sb) / "build" / "release" / "PPSA98515"
        (app / "sce_sys").mkdir(parents=True)
        (app / "eboot.bin").write_bytes(b"new-eboot")
        (app / "sce_sys" / "param.json").write_text("{}")
        (app / ".env").write_text("SECRET=1")
        os.symlink("/etc/passwd", str(app / "link"))
        real = self.proj / "build" / "release" / "PPSA98515"
        real.mkdir(parents=True)
        (real / "eboot.bin").write_bytes(b"old-eboot")
        (real / "stale.txt").write_text("old")
        change, words = sandbox.plan_copy_out("eg", sb["output"])
        self.assertTrue(change["folder"])
        self.assertEqual(change["files"], 2)
        self.assertIn("backed up", words)
        self.assertIn("eboot.bin", words)
        sandbox.copy_out(change)
        self.assertEqual((real / "eboot.bin").read_bytes(), b"new-eboot")
        self.assertTrue((real / "sce_sys" / "param.json").exists())
        self.assertFalse((real / "stale.txt").exists())
        self.assertFalse(os.path.lexists(real / ".env"))
        self.assertFalse(os.path.lexists(real / "link"))
        sandbox.undo_copy(sandbox.recent_copies()[0]["id"])
        self.assertEqual((real / "eboot.bin").read_bytes(), b"old-eboot")
        self.assertTrue((real / "stale.txt").exists())
        with self.assertRaises(sandbox.SandboxError):
            sandbox.plan_copy_out("eg", "build", "..")                  # never the project folder itself


if __name__ == "__main__":
    unittest.main()
