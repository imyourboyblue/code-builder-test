"""
Tests for the loop guard (Joshua's EglantinePS5 build went round and round on a half-started Colima):
- a stuck Colima gets a Repair Colima card, then a Reset Colima card, then plain words: never the same thing again.
- a Colima seen stuck a moment ago is re-checked quickly, not waited on for 3 minutes every time.
- messages never tell the user to type colima commands that PatchBay itself refuses to run.
- Colima's log tail is shown.
- in any chat: the same answer twice switches to diagnosis (logs, look up the error, a different fix on a card);
  a third time stops. A step that failed twice is diagnosed before giving up.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import sandbox  # noqa: E402
import server  # noqa: E402
from tests.test_colima_wait import FakeMac, Patched  # noqa: E402
from tests.test_safety import FakeChat, device_cfg  # noqa: E402


class StuckColima(Patched):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name)
        log = tmp / "colima" / "_lima" / "colima" / "ha.stderr.log"
        log.parent.mkdir(parents=True)
        log.write_text("".join("line %d ok\n" % i for i in range(50)) +
                       'time="2026-11-03" level=fatal msg="error retrieving current runtime: empty value"\n')
        self.home = mock.patch.dict("os.environ", {"COLIMA_HOME": str(tmp / "colima")})
        self.home.start()
        self.addCleanup(self.home.stop)
        self.addCleanup(self._t.cleanup)
        self.st = FakeChat(device_cfg(tmp / "dev", tmp / "data"))
        self.events = []

    def check(self):
        return server.sandbox_preflight(self.st, self.events.append)

    def cards(self):
        return [e for e in self.events if e.get("type") == "confirm"]

    def test_stuck_offers_repair_card_with_log_tail_and_no_terminal_steps(self):
        self.use(FakeMac(colima="Running", docker_fails=10 ** 6))
        with mock.patch.object(sandbox, "COLIMA_WAIT", 0):
            result = self.check()
        self.assertEqual(self.cards()[-1]["title"], "Repair Colima (force stop, start again)")
        self.assertIn("colima stop --force", self.cards()[-1]["diff"])
        self.assertIn("error retrieving current runtime", result)
        self.assertNotIn("in Terminal", result)
        self.assertNotIn("Start Colima", self.cards()[-1]["title"])

    def test_second_check_is_quick_not_another_three_minutes(self):
        self.use(FakeMac(colima="Running", docker_fails=10 ** 6))
        with mock.patch.object(sandbox, "COLIMA_WAIT", 0):
            self.check()
        with mock.patch.object(sandbox, "wait_for_docker", return_value=(False, "")) as wait:
            sandbox.preflight()
        self.assertEqual(wait.call_args[0][3], sandbox.QUICK_WAIT)

    def test_repair_then_reset_then_plain_words(self):
        mac = self.use(FakeMac(colima="Running", docker_fails=10 ** 6))
        with mock.patch.object(sandbox, "COLIMA_WAIT", 0), mock.patch.object(sandbox, "QUICK_WAIT", 0):
            self.check()
            with self.assertRaises(sandbox.SandboxError) as e:
                sandbox.repair_colima()
            self.assertIn("Reset Colima card", str(e.exception))
            self.assertIn(["/opt/homebrew/bin/colima", "stop", "--force"], mac.calls)
            self.check()
            self.assertEqual(self.cards()[-1]["title"], "Reset Colima (delete its Linux machine, make a new one)")
            self.assertIn("installed INSIDE the sandboxes is deleted", self.cards()[-1]["diff"])
            with self.assertRaises(sandbox.SandboxError):
                sandbox.repair_colima(reset=True)
            self.assertIn(["/opt/homebrew/bin/colima", "delete", "--force"], mac.calls)
            n = len(self.cards())
            result = self.check()
        self.assertEqual(len(self.cards()), n)       # nothing left to offer: no third card
        self.assertIn(server.COLIMA_GAVE_UP, result)
        self.assertIn("Stop here", result)

    def test_repair_that_works_clears_the_trouble(self):
        self.use(FakeMac(colima="Running", docker_fails=0))
        sandbox.note_trouble()
        sandbox.TROUBLE["repairs"] = 1
        self.assertIn("repaired", sandbox.repair_colima())
        self.assertEqual(sandbox.TROUBLE["repairs"], 0)
        self.assertFalse(sandbox.recent_trouble())

    def test_failed_start_offers_repair_not_the_same_start_again(self):
        self.use(FakeMac(colima="Stopped", docker_fails=10 ** 6))
        sandbox.note_trouble()           # "colima start" failed a moment ago
        with mock.patch.object(sandbox, "COLIMA_WAIT", 0):
            pf = sandbox.preflight()
        self.assertIsNone(pf["colima"])
        self.assertEqual(pf["fix"], "repair")

    def test_repair_card_carries_on(self):
        self.st.pending["c1"] = {"kind": "sandbox", "remote": "", "source": "", "note": "",
                                 "change": {"action": "colima_repair", "sandbox": ""}}
        with mock.patch.object(server, "sb_ready", lambda: None), \
                mock.patch.object(sandbox, "repair_colima", return_value="Colima was repaired.") as fix, \
                mock.patch.object(server, "chat_turn") as turn:
            server.do_confirm(self.st, "c1", "", self.events.append)
        fix.assert_called_once()
        self.assertFalse(fix.call_args[0][1])        # repair, not reset
        turn.assert_called_once()

    def test_blocked_colima_command_points_at_the_repair_card(self):
        with mock.patch.object(server, "project", return_value={"name": "EglantinePS5"}), \
                self.assertRaises(server.ToolError) as e:
            server.HANDLERS["propose_command"](self.st, self.events.append,
                                               {"command": "colima stop && colima start --cpu 8"})
        self.assertIn("Repair Colima", str(e.exception))


STUCK_TEXT = ("Checklist:\n- ✗ Docker running: Colima is running, but Docker inside it isn't answering\n"
              "Colima says (colima status):\ntime=\"2026-11-03T01:%02d:02-07:00\" level=fatal msg=\"error retrieving "
              "current runtime: empty value\"")


class ChatLoopGuard(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name)
        (tmp / "dev").mkdir()
        self.cfg = device_cfg(tmp / "dev", tmp / "data")
        self.st = FakeChat(self.cfg)
        self.st.timing, self.st.chat_id = [], ""
        self.events = []

    def tearDown(self):
        self._t.cleanup()

    def run_loop(self, handler, rounds=10):
        call = {"tool_calls": [{"function": {"name": "sandbox_build", "arguments": {"sandbox": "EglantinePS5"}}}]}
        with mock.patch.object(server, "ask_model", return_value=call) as ask, \
                mock.patch.object(server, "second_opinion", return_value=""), \
                mock.patch.object(server, "claude_ready", return_value=False):
            server._chat_loop(self.st, "install eglantineps5", self.events.append, self.cfg, "system", [],
                              {"sandbox_build": handler})
        return ask

    def test_same_answer_three_times_stops(self):
        n = iter(range(100))
        ask = self.run_loop(lambda st, emit, a: STUCK_TEXT % next(n))   # the time in it changes each run
        self.assertEqual(ask.call_count, 3)
        notes = [e["text"] for e in self.events if e["type"] == "note"]
        self.assertTrue(any("stopped retrying" in t and "looking into why" in t for t in notes))
        self.assertTrue(any("loop guard" in str(m.get("content")) for m in self.st.messages))
        stop = next(e for e in self.events if e["type"] == "choices")
        self.assertIn("going round in circles", stop["text"])

    def test_failed_twice_diagnoses_before_giving_up(self):
        ask = self.run_loop(lambda st, emit, a: "Error: Couldn't download the Linux image ubuntu:24.04: timeout")
        self.assertEqual(ask.call_count, 3)
        self.assertTrue(any("web_search" in str(m.get("content")) and "loop guard" in str(m.get("content"))
                            for m in self.st.messages))
        stop = next(e for e in self.events if e["type"] == "choices")
        self.assertIn("even after looking into why", stop["text"])

    def test_different_answers_do_not_trip_the_guard(self):
        n = iter(range(100))
        self.run_loop(lambda st, emit, a: "Built part %d of the project: main.c, util.c, extra-%d.c all good."
                      % (next(n), 0) + "x" * next(n))
        self.assertFalse(any("stopped retrying" in e.get("text", "") for e in self.events))

    def test_short_answers_do_not_count(self):
        self.assertEqual(server.repeats([], "x", "Done."), 0)


if __name__ == "__main__":
    unittest.main()
