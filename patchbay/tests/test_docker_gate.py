"""
Tests for two fixes:
- When Docker isn't answering because Colima isn't started, PatchBay shows the Start Colima card FIRST (for
  sandbox_run and for Mac commands that use docker), instead of running something that just hangs or fails.
- A backup before a device change copies only the exact files/folders being replaced, never the folder above
  them (a PS5's /user is hundreds of GB).
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

import backup  # noqa: E402
import sandbox  # noqa: E402
import server  # noqa: E402
from tests.test_colima_wait import FakeMac  # noqa: E402
from tests.test_safety import FakeChat, device_cfg  # noqa: E402


class ChatTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = tmp = Path(self._t.name)
        proj = tmp / "EglantinePS5"
        proj.mkdir()
        (proj / "Makefile").write_text("all:\n\techo hi\n")
        (tmp / "dev").mkdir()
        self.st = FakeChat(device_cfg(tmp / "dev", tmp / "data", device_name="PS5"))
        self.events = []
        for p in [mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(tmp / "data")}),
                  mock.patch.object(server, "sb_ready", lambda: None),
                  mock.patch.object(sandbox, "ROOT", tmp / "sandboxes")]:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._t.cleanup)
        self.proj = proj

    def mac(self, colima):
        fake = FakeMac(colima=colima, docker_fails=0 if colima == "Running" else 999)
        for p in fake.patches():
            p.start()
            self.addCleanup(p.stop)
        return fake

    def cards(self):
        return [e for e in self.events if e.get("type") == "confirm"]


class SandboxRunWaitsForColima(ChatTest):
    def setUp(self):
        super().setUp()
        server.sb(sandbox.create, "EglantinePS5", str(self.proj), "linux")

    def run_tool(self):
        with mock.patch.object(sandbox, "run", return_value=(0, "hi\n")) as run:
            result = server.HOME_HANDLERS["sandbox_run"](self.st, self.events.append,
                                                         {"sandbox": "EglantinePS5", "command": "make"})
        return result, run

    def test_stopped_colima_shows_start_card_and_runs_nothing(self):
        fake = self.mac("Stopped")
        result, run = self.run_tool()
        run.assert_not_called()
        self.assertIn("Docker isn't answering", result)
        self.assertEqual(self.cards()[-1]["title"], "Start Colima (8 CPUs, 20 GB)")
        self.assertFalse(any(c[1] == "start" for c in fake.calls if c[0].endswith("colima")),
                         "Colima must not be started before the user presses the card")

    def test_card_then_carries_on(self):
        self.mac("Stopped")
        self.run_tool()
        pid = self.cards()[-1]["id"]
        with mock.patch.object(server, "chat_turn") as turn:
            server.do_confirm(self.st, pid, "", self.events.append)
        turn.assert_called_once()
        self.assertIn("Carry on", self.st.messages[-1]["content"])

    def test_running_docker_runs_straight_away(self):
        self.mac("Running")
        result, run = self.run_tool()
        run.assert_called_once()
        self.assertEqual(self.cards(), [])


class MacDockerCommands(ChatTest):
    def propose(self, command):
        with mock.patch.object(server, "project", return_value={"name": "EglantinePS5"}), \
                mock.patch.object(server.projects, "folder_of", return_value=self.proj), \
                mock.patch.object(server, "runs_by_itself", return_value=False):   # always a card, never runs
            return server.HANDLERS["propose_command"](self.st, self.events.append, {"command": command})

    def test_docker_command_with_colima_stopped_offers_start_card_first(self):
        self.mac("Stopped")
        result = self.propose("docker build -t eglantine .")
        self.assertIn("uses Docker", result)
        self.assertEqual([c["kind"] for c in self.cards()], ["sandbox"])
        self.assertIn("Start Colima", self.cards()[0]["title"])

    def test_docker_command_with_docker_up_gets_its_own_card(self):
        self.mac("Running")
        self.propose("docker build -t eglantine .")
        self.assertNotIn("Start Colima", " ".join(c["title"] for c in self.cards()))

    def test_other_commands_never_check_docker(self):
        with mock.patch.object(sandbox, "preflight") as pf:
            self.propose("make clean")
        pf.assert_not_called()

    def test_docker_down_output_points_at_the_card(self):
        summary, _ = server.show_output(self.events.append, "docker build failed.", 1,
                                        "ERROR: Cannot connect to the Docker daemon at unix:///x. Is the docker "
                                        "daemon running?", "docker build .")
        self.assertIn("sandbox_preflight", summary)


class FakeDevice:
    """A device with /user/app/EGLA00001 (a folder) and /user/app/EGLA00001/eboot.bin."""
    LIST = {"/user/app": [("EGLA00001", "dir", None, None)],
            "/user/app/EGLA00001": [("eboot.bin", "file", 10, "20261103000000")],
            "/user": [("app", "dir", None, None)], "/": [("user", "dir", None, None)]}

    def __init__(self, cfg):
        pass

    def connect(self, quiet=False):
        pass

    def close(self):
        pass

    def _list(self, d):
        if d not in self.LIST:
            raise ftplib.error_perm("550 No such directory")
        return self.LIST[d]


class BackupOnlyWhatIsReplaced(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backup.core, "Device", FakeDevice)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = {"device_name": "PS5"}

    def test_exact_paths_not_their_parent(self):
        items, new = backup.existing_items(self.cfg, ["/user/app/EGLA00001/eboot.bin", "/user/app/EGLA00001",
                                                      "/user/app/NEW000001", "/data/x/y.bin"])
        self.assertEqual(items, [{"path": "/user/app/EGLA00001/eboot.bin", "kind": "file"},
                                 {"path": "/user/app/EGLA00001", "kind": "dir"}])
        self.assertEqual(new, ["/user/app/NEW000001", "/data/x/y.bin"])

    def test_whole_top_level_folder_is_refused(self):
        for p in ("/user", "/"):
            with self.assertRaises(backup.BackupError):
                backup.existing_items(self.cfg, [p])


class PlanBackup(ChatTest):
    def test_plan_with_user_folder_is_refused(self):
        with self.assertRaises(server.ToolError) as e:
            server.HANDLERS["propose_plan"](self.st, self.events.append,
                                            {"title": "Install", "steps": ["a"], "device_writes": ["/user"]})
        self.assertIn("exact files", str(e.exception))

    def test_confirmed_plan_backs_up_only_the_replaced_files(self):
        self.st.pending["p1"] = {"kind": "plan", "remote": "plan", "source": "", "note": "Install",
                                 "steps": ["a"], "writes": ["/user/app/EGLA00001/eboot.bin", "/user/app/NEW000001"],
                                 "commands": [], "needs_typing": True, "type_word": "PS5"}
        jobs = []

        class Job:
            def __init__(self, cfg, items, label):
                jobs.append(items)

            def _run_safe(self):
                pass

            def status(self):
                return {"phase": "done", "message": ""}
        app = mock.MagicMock(backups={})
        with mock.patch.object(backup.core, "Device", FakeDevice), mock.patch.object(backup, "Job", Job), \
                mock.patch.object(server, "APP", app), mock.patch.object(server, "chat_turn"):
            server.do_confirm(self.st, "p1", "PS5", self.events.append)
        self.assertEqual(jobs, [[{"path": "/user/app/EGLA00001/eboot.bin", "kind": "file"}]])
        self.assertTrue(any("Nothing there yet" in e.get("text", "") for e in self.events))


if __name__ == "__main__":
    unittest.main()
