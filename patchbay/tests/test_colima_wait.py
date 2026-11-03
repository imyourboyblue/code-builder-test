"""
Tests for the Colima start fixes:
- docker commands talk straight to Colima's socket (DOCKER_HOST), not whatever docker context is current.
- after Colima starts, PatchBay waits until Docker answers, and says plainly when it's stuck.
- an already-running Colima is never offered the Start card again.
- after the Colima card is confirmed, the chat carries on by itself.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import sandbox  # noqa: E402
import server  # noqa: E402
from tests.test_safety import FakeChat, device_cfg  # noqa: E402

WHICH = {"docker": "/opt/homebrew/bin/docker", "colima": "/opt/homebrew/bin/colima"}
DOCKER_DOWN = (1, "Client: Docker Engine - Community\n Version: 28.0\n\nServer:\nERROR: Cannot connect to the Docker "
                  "daemon at unix:///var/run/docker.sock. Is the docker daemon running?")


def colima_list(status="Running", cpus=8, mem_gb=20):
    return json.dumps({"name": "default", "status": status, "arch": "aarch64", "cpus": cpus,
                       "memory": mem_gb * 1024 ** 3, "disk": 100 * 1024 ** 3})


class FakeMac:
    """Stands in for colima and docker. docker info fails `docker_fails` times, then answers."""

    def __init__(self, colima="Running", docker_fails=0, cpus=8, mem=20):
        self.colima, self.docker_fails, self.cpus, self.mem = colima, docker_fails, cpus, mem
        self.calls = []

    def quiet(self, argv, timeout=60):
        self.calls.append(argv)
        if argv[0].endswith("colima"):
            if argv[1:3] == ["list", "--json"]:
                return 0, colima_list(self.colima, self.cpus, self.mem)
            if argv[1] == "status":
                return (0, "colima is running using macOS Virtualization.Framework") if self.colima == "Running" \
                    else (1, "colima is not running")
        if argv[0].endswith("docker") and argv[1] == "info":
            if self.docker_fails:
                self.docker_fails -= 1
                return DOCKER_DOWN
            return 0, "%d %d" % (self.cpus, self.mem * 1024 ** 3) if "--format" in argv else "Server Version: 28"
        return 0, ""

    def progress(self, argv, timeout, emit, text, stop=None):
        self.calls.append(argv)
        if argv[1] == "start":
            self.colima = "Running"
        return 0, ""

    def patches(self):
        return [mock.patch.object(sandbox, "_which", side_effect=WHICH.get),
                mock.patch.object(sandbox, "engine", return_value=("docker", WHICH["docker"])),
                mock.patch.object(sandbox, "_quiet", side_effect=self.quiet),
                mock.patch.object(sandbox, "_run_progress", side_effect=self.progress),
                mock.patch.object(sandbox, "colima_size", return_value=(8, 20, 100)),
                mock.patch.object(sandbox.time, "sleep"),
                mock.patch.object(sandbox.services, "record")]


class Patched(unittest.TestCase):
    mac = None

    def use(self, mac):
        self.mac = mac
        for p in mac.patches():
            p.start()
            self.addCleanup(p.stop)
        return mac


class DockerTalksToColima(unittest.TestCase):
    def test_docker_host_points_at_colima_socket(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "default").mkdir()
            path = str(Path(d) / "default" / "docker.sock")
            s = socket.socket(socket.AF_UNIX)
            try:
                s.bind(path)
            except OSError:
                self.skipTest("can't make a socket here")
            try:
                with mock.patch.dict(os.environ, {"COLIMA_HOME": d}):
                    os.environ.pop("DOCKER_HOST", None)
                    self.assertEqual(sandbox.cli_env()["DOCKER_HOST"], "unix://" + path)
                with mock.patch.dict(os.environ, {"COLIMA_HOME": d, "DOCKER_HOST": "tcp://mine:2375"}):
                    self.assertEqual(sandbox.cli_env()["DOCKER_HOST"], "tcp://mine:2375")   # the user's own wins
            finally:
                s.close()

    def test_no_socket_no_docker_host(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"COLIMA_HOME": d}):
            os.environ.pop("DOCKER_HOST", None)
            self.assertNotIn("DOCKER_HOST", sandbox.cli_env())

    def test_docker_error_shows_the_server_part(self):
        words = sandbox.docker_error(DOCKER_DOWN[1])
        self.assertIn("Cannot connect to the Docker daemon", words)
        self.assertNotIn("Version: 28.0", words)


class WaitForDocker(Patched):
    def test_waits_while_colima_boots(self):
        self.use(FakeMac(docker_fails=3))
        events = []
        ok, _ = sandbox.wait_for_docker(WHICH["docker"], events.append, timeout=60)
        self.assertTrue(ok)
        self.assertTrue(any("Waiting for Docker" in e["text"] for e in events))

    def test_gives_up_after_the_limit(self):
        self.use(FakeMac(docker_fails=99))
        ok, out = sandbox.wait_for_docker(WHICH["docker"], None, timeout=0)
        self.assertFalse(ok)
        self.assertIn("Cannot connect", out)

    def test_stop_button_ends_the_wait(self):
        self.use(FakeMac(docker_fails=99))
        stop = mock.Mock(side_effect=server.Stopped())
        with self.assertRaises(server.Stopped):
            sandbox.wait_for_docker(WHICH["docker"], None, stop, timeout=60)


class StartColima(Patched):
    def test_start_then_wait_until_docker_answers(self):
        mac = self.use(FakeMac(colima="Stopped", docker_fails=2))
        result = sandbox.start_colima(lambda e: None)
        self.assertIn("Docker is answering", result)
        self.assertIn("8 CPUs", result)
        self.assertTrue(any(c[1] == "start" for c in mac.calls if c[0].endswith("colima")))
        sandbox.services.record.assert_called_once()

    def test_already_running_is_not_started_again(self):
        mac = self.use(FakeMac(colima="Running", docker_fails=1))
        sandbox.start_colima()
        self.assertFalse(any(len(c) > 1 and c[1] == "start" for c in mac.calls))
        sandbox.services.record.assert_not_called()      # PatchBay didn't start it, so it won't stop it on quit

    def test_stuck_docker_gives_plain_message_with_colima_status(self):
        self.use(FakeMac(colima="Running", docker_fails=999))
        with mock.patch.object(sandbox, "COLIMA_WAIT", 0), self.assertRaises(sandbox.SandboxError) as e:
            sandbox.start_colima()
        words = str(e.exception)
        self.assertIn("didn't answer", words)
        self.assertIn("colima is running using macOS", words)
        self.assertIn("colima stop", words)
        self.assertIn("Cannot connect to the Docker daemon", words)


class PreflightLoop(Patched):
    def test_running_colima_booting_docker_is_ready_no_card(self):
        self.use(FakeMac(colima="Running", docker_fails=2))
        pf = sandbox.preflight({"cpus": 8, "memory_gb": 16})
        self.assertIsNone(pf["colima"])
        self.assertTrue(pf["ok"])

    def test_running_colima_stuck_docker_never_offers_start(self):
        self.use(FakeMac(colima="Running", docker_fails=999))
        pf = sandbox.preflight({"cpus": 8, "memory_gb": 16}, wait=0)
        self.assertIsNone(pf["colima"])
        self.assertFalse(pf["ok"])
        self.assertIn("colima status", pf["stuck"])

    def test_running_small_colima_stuck_offers_restart(self):
        self.use(FakeMac(colima="Running", docker_fails=999, cpus=2, mem=2))
        pf = sandbox.preflight({"cpus": 8, "memory_gb": 16}, wait=0)
        self.assertEqual(pf["colima"], "resize")

    def test_stopped_colima_offers_start(self):
        self.use(FakeMac(colima="Stopped", docker_fails=999))
        pf = sandbox.preflight(wait=0)
        self.assertEqual(pf["colima"], "start")

    def test_engine_ready_waits_for_running_colima(self):
        mac = self.use(FakeMac(colima="Running", docker_fails=3))
        self.assertEqual(sandbox.engine_ready()[0], "docker")
        self.assertFalse(any(len(c) > 1 and c[1] == "start" for c in mac.calls))


class CarryOnAfterCard(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name)
        (tmp / "dev").mkdir()
        self.st = FakeChat(device_cfg(tmp / "dev", tmp / "data", device_name="PS5"))
        self.events = []
        p = mock.patch.object(server, "sb_ready", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self._t.cleanup()

    def confirm(self, action="colima_start"):
        self.st.pending["c1"] = {"kind": "sandbox", "remote": "EglantinePS5", "source": "", "note": "",
                                 "change": {"action": action, "sandbox": "EglantinePS5"}}
        server.do_confirm(self.st, "c1", "", self.events.append)

    def test_chat_carries_on_after_colima_starts(self):
        with mock.patch.object(sandbox, "start_colima", return_value="Colima is running and Docker is answering."), \
                mock.patch.object(server, "chat_turn") as turn:
            self.confirm()
        turn.assert_called_once_with(self.st, None, self.events.append)
        self.assertIn("Carry on", self.st.messages[-1]["content"])
        self.assertIn("sandbox_build step setup", self.st.messages[-1]["content"])
        self.assertTrue(any("minute or two" in e.get("text", "") for e in self.events))

    def test_stuck_colima_stops_with_plain_words_not_another_card(self):
        with mock.patch.object(sandbox, "start_colima", side_effect=sandbox.SandboxError("Docker didn't answer")), \
                mock.patch.object(server, "chat_turn") as turn:
            self.confirm()
        turn.assert_not_called()
        self.assertIn("Don't offer the Start Colima card again", self.st.messages[-1]["content"])
        self.assertEqual(self.events[-1]["text"], "Docker didn't answer")

    def test_copy_out_does_not_carry_on(self):
        with mock.patch.object(sandbox, "copy_out", return_value="Copied."), \
                mock.patch.object(server, "chat_turn") as turn:
            self.confirm("copy_out")
        turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
