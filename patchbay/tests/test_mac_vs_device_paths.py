"""
Tests for the mix-up in the "install eglantineps5 onto my ps5" chat: the AI gave Mac project paths
(/Users/.../EglantinePS5/Makefile) to the device tools, so the PS5 answered "550 No such file" every time, and it
checked devkitPro although the project builds in a Linux sandbox.
- Device tools given a project path read it from the Mac instead, and never contact the device.
- Other Mac paths get a clear error naming the Mac tools.
- A failed device listing shows as a failure.
- A project with a Linux sandbox is sent to the sandbox steps: no devkitPro build, no install of an old build.
Nothing here contacts a real device: core.Device is replaced so any attempt fails the test.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import ftplib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import sandbox  # noqa: E402
import server  # noqa: E402
from tests.test_safety import FakeChat  # noqa: E402


class NoDevice:
    """Stands in for core.Device: any use means a tool tried to reach the PS5."""
    def __init__(self, cfg):
        raise AssertionError("a tool tried to contact the device")


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name).resolve()
        self.proj = tmp / "Patchbay-projects" / "EglantinePS5"
        (self.proj / "dist").mkdir(parents=True)
        (self.proj / "Makefile").write_text("all:\n\tprospero-clang -o dist/EglantinePS5.self main.c\n")
        self.cfg = {"device_name": "Ps5", "protocol": "ftp", "host": "192.0.2.1", "ports": [2121],
                    "timeout_seconds": 1, "data_folder": str(tmp / "data"), "allow_writes": True,
                    "projects": [{"name": "EglantinePS5", "folder": str(self.proj), "build": "make",
                                  "output": "dist/EglantinePS5.self", "device_path": "/data/EglantinePS5.self"}]}
        self.st = FakeChat(self.cfg)
        self.events = []
        self.patches = [mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(tmp / "data")}),
                        mock.patch.object(server.core, "Device", NoDevice)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._t.cleanup()

    def call(self, name, **args):
        return server.HANDLERS[name](self.st, self.events.append, args)


class MacPathsTest(Base):
    def test_read_file_with_project_path_reads_the_mac(self):
        out = self.call("read_file", path=str(self.proj / "Makefile"))
        self.assertIn("prospero-clang", out)
        self.assertIn("on this Mac", out)

    def test_list_folder_with_project_path_lists_the_mac(self):
        out = self.call("list_folder", path=str(self.proj))
        self.assertIn("Makefile", out)
        self.assertIn("on this Mac", out)
        self.assertIn("on this Mac", self.call("list_folder", path=str(self.proj / "dist")))

    def test_other_mac_paths_name_the_mac_tools(self):
        for path in ("/Users/blue/Desktop", "~/Downloads/x.txt", "/opt/homebrew/bin"):
            with self.assertRaises(server.ToolError) as e:
                self.call("read_file", path=path)
            self.assertIn("mac_read", str(e.exception))
            with self.assertRaises(server.ToolError):
                self.call("list_folder", path=path)

    def test_device_edits_refuse_project_paths(self):
        with self.assertRaises(server.ToolError) as e:
            self.call("propose_edit", path=str(self.proj / "Makefile"), find="all", replace="x", reason="t")
        self.assertIn("propose_project_edit", str(e.exception))
        with self.assertRaises(server.ToolError):
            self.call("propose_restore", path=str(self.proj / "Makefile"), snapshot="latest")
        self.assertEqual(self.st.pending, {})

    def test_project_read_takes_a_full_mac_path(self):
        out = self.call("project_read", project="EglantinePS5", path=str(self.proj / "Makefile"))
        self.assertIn("prospero-clang", out)

    def test_project_read_still_refuses_outside_paths(self):
        with self.assertRaises(server.ToolError):
            self.call("project_read", project="EglantinePS5", path="/etc/passwd")
        with self.assertRaises(server.ToolError):
            self.call("project_read", project="EglantinePS5", path="../../secret")

    def test_device_paths_still_go_to_the_device(self):
        with self.assertRaises(AssertionError):     # it tried the (fake) device, as it should
            self.call("list_folder", path="/user")

    def test_failed_device_listing_is_a_failure(self):
        class Refuses:
            def __init__(self, cfg):
                pass

            def connect(self, quiet=False):
                pass

            def close(self):
                pass

            def _list(self, path):
                raise ftplib.error_perm("550 No such file or directory")
        with mock.patch.object(server.core, "Device", Refuses):
            with self.assertRaises(server.ToolError) as e:
                self.call("list_folder", path="/user/nothing")
        self.assertIn("550", str(e.exception))


class SandboxRouteTest(Base):
    def setUp(self):
        super().setUp()
        ready = {"rows": [(True, "Docker running", "yes")], "brew": [], "colima": None, "stuck": "", "ok": True}
        p = mock.patch.object(sandbox, "preflight", return_value=ready)
        p.start()
        self.patches.append(p)
        server.sb(sandbox.create, "EglantinePS5", str(self.proj), "linux")

    def test_list_projects_says_sandbox_and_mac(self):
        out = self.call("list_projects")
        self.assertIn("on this Mac", out)
        self.assertIn("Linux sandbox", out)
        self.assertIn("sandbox_build", out)

    def test_check_build_tools_points_at_the_sandbox(self):
        with mock.patch.object(server.projects, "toolchain_report", return_value="devkitPro is installed"), \
                mock.patch.object(server.platforms, "toolchains", return_value=""):
            out = self.call("check_build_tools")
        self.assertIn("no devkitPro", out)
        self.assertIn("sandbox_build sandbox EglantinePS5", out)

    def test_build_on_mac_is_redirected(self):
        out = self.call("propose_build", project="EglantinePS5")
        self.assertTrue(out.startswith("Not built"))
        self.assertIn("sandbox_build", out)
        self.assertEqual(self.st.pending, {})

    def test_no_install_of_a_build_that_did_not_come_from_the_sandbox(self):
        with self.assertRaises(server.ToolError) as e:        # nothing built yet
            self.call("propose_install", project="EglantinePS5")
        self.assertIn("sandbox", str(e.exception))
        (self.proj / "dist" / "EglantinePS5.self").write_bytes(b"\x7fELF" + b"\0" * 60)
        with self.assertRaises(server.ToolError) as e:        # an old Mac build, not copied out
            self.call("propose_install", project="EglantinePS5")
        self.assertIn("copied out of the sandbox", str(e.exception))
        self.assertEqual(self.st.pending, {})

    def test_a_sandbox_copy_out_counts_as_the_build(self):
        out = self.proj / "dist" / "EglantinePS5.self"
        self.assertFalse(server.copied_from_sandbox(self.cfg["projects"][0], out))
        server.sb_ready()
        sandbox._save_history([{"id": 1, "sandbox": "EglantinePS5", "file": "dist/", "to": str(self.proj / "dist")}])
        self.assertTrue(server.copied_from_sandbox(self.cfg["projects"][0], out))
        sandbox._save_history([{"id": 1, "sandbox": "EglantinePS5", "file": "dist/", "to": str(self.proj / "dist"),
                                "undone": True}])
        self.assertFalse(server.copied_from_sandbox(self.cfg["projects"][0], out))

    def test_install_question_switches_on_sandbox_tools(self):
        groups = server.pick_groups(self.st, server.DEVICE_GROUPS, "install eglantineps5 onto my ps5")
        self.assertIn("code", groups)
        self.assertIn("sandbox", groups)


class PromptTest(unittest.TestCase):
    def test_system_prompt_separates_device_and_mac(self):
        self.assertIn("WHERE FILES ARE", server.FILE_PLACES)
        self.assertIn("FILE_PLACES", "".join((HERE / "server.py").read_text().split("SELF_EDIT_RULES + ")[1:3]))


if __name__ == "__main__":
    unittest.main()
