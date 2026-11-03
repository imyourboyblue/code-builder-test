"""
Tests for crashes inside PatchBay's own code (Python errors such as UnboundLocalError).
- sandbox_run runs through the real code path (the container tool is stubbed out).
- No function hides a module it imports by using the same name for a local variable (the bug behind
  "cannot access local variable 'where'").
- When a tool crashes, PatchBay stops and says it's a PatchBay bug instead of offering to ask Claude.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import ast
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


class SandboxRunTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name)
        proj = tmp / "EglantinePS5"
        proj.mkdir()
        (proj / "Makefile").write_text("all:\n\techo hi\n")
        self.cfg = device_cfg(tmp / "dev", tmp / "data", device_name="PS5")
        (tmp / "dev").mkdir()
        self.st = FakeChat(self.cfg)
        self.events = []
        self.patches = [mock.patch.object(server, "access_cfg", lambda: {"data_folder": str(tmp / "data")})]
        for p in self.patches:
            p.start()
        server.sb(sandbox.create, "EglantinePS5", str(proj), "linux")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._t.cleanup()

    def run_tool(self, command, code=0, out="hi\n"):
        with mock.patch.object(sandbox, "run", return_value=(code, out)) as run:
            result = server.HOME_HANDLERS["sandbox_run"](self.st, self.events.append,
                                                         {"sandbox": "EglantinePS5", "command": command})
        return result, run

    def test_make_runs(self):
        result, run = self.run_tool("make")
        self.assertIn("In the Linux sandbox EglantinePS5, make worked (exit code 0)", result)
        self.assertEqual(run.call_args[0][:2], ("EglantinePS5", "make"))

    def test_failed_make_says_so(self):
        result, _ = self.run_tool("make V=1", 2, "main.c:1: error: oops\n")
        self.assertIn("make V=1 failed (exit code 2)", result)

    def test_device_upload_is_refused(self):
        with self.assertRaises(server.ToolError):
            self.run_tool("PS5_HOST=192.168.50.203 make install")


class SandboxSetupTest(SandboxRunTest):
    """The failures from setting up EglantinePS5: no git, hand-written setup, Docker/Colima not ready."""

    def call(self, name, args):
        return server.HOME_HANDLERS[name](self.st, self.events.append, dict(args, sandbox="EglantinePS5"))

    def test_missing_git_is_added_to_the_setup(self):
        out = "git submodule update --init Eglantine\n/bin/bash: line 1: git: command not found\n"
        result, _ = self.run_tool("make", 2, out)
        self.assertIn("added the Linux package git", result)
        self.assertIn("git", sandbox.load("EglantinePS5")["setup_command"])

    def test_hand_written_setup_keeps_the_preset(self):
        # Made without a recipe, like an older sandbox: the project's preset is still used as the base.
        result = self.call("propose_sandbox_settings", {"setup_command": "apt-get update && apt-get install -y make gcc"})
        after = sandbox.load("EglantinePS5")["setup_command"]
        self.assertIn("Kept the project's setup", result)
        self.assertIn("git", after)
        self.assertIn("gcc", after)

    def test_empty_setup_is_filled_from_the_preset(self):
        s = sandbox.load("EglantinePS5")
        s["setup_command"] = ""
        sandbox.save(s)
        s["network"] = True
        sandbox.save(s)
        ok = {"rows": [], "brew": [], "colima": None, "ok": True}
        with mock.patch.object(sandbox, "preflight", return_value=ok), \
                mock.patch.object(sandbox, "run", return_value=(0, "done")) as run:
            result = self.call("sandbox_build", {"step": "setup"})
        self.assertIn("project's own preset", result)
        self.assertIn("apt-get install", run.call_args[0][1])

    def test_build_stops_at_preflight_card_when_docker_is_missing(self):
        s = sandbox.load("EglantinePS5")
        s["network"] = True
        sandbox.save(s)
        pf = {"rows": [(False, "Docker command-line tool", "missing")], "brew": ["docker", "colima"],
              "colima": None, "ok": False}
        with mock.patch.object(sandbox, "preflight", return_value=pf), \
                mock.patch.object(server.projects, "find_brew", return_value="/opt/homebrew/bin/brew"), \
                mock.patch.object(server, "add_pending", return_value="card shown") as card, \
                mock.patch.object(sandbox, "run") as run:
            result = self.call("sandbox_build", {"step": "setup"})
        run.assert_not_called()
        self.assertIn("card shown", result)
        self.assertEqual(card.call_args.kwargs["extra_fields"]["packages"], ["brew:docker", "brew:colima"])

    def test_small_colima_gets_a_restart_card(self):
        pf = {"rows": [(False, "Docker's Linux machine size", "2 CPUs")], "brew": [], "colima": "resize", "ok": False}
        with mock.patch.object(sandbox, "preflight", return_value=pf):
            result = self.call("sandbox_preflight", {})
        card = [e for e in self.events if e.get("type") == "confirm"][-1]
        self.assertIn("Restart Colima bigger", card["title"])
        self.assertIn("--cpu", card["diff"])
        self.assertIn("card", result)

    def test_plain_colima_start_command_is_refused(self):
        with mock.patch.object(server, "project", return_value={"name": "EglantinePS5"}), \
                self.assertRaises(server.ToolError) as e:
            server.HANDLERS["propose_command"](self.st, self.events.append, {"command": "colima start"})
        self.assertIn("sandbox_preflight", str(e.exception))


class NoHiddenModulesTest(unittest.TestCase):
    """A local variable with the same name as an imported module breaks every use of that module in the function."""

    def test_no_local_variable_hides_an_import(self):
        found = []
        for f in sorted(HERE.rglob("*.py")):
            tree = ast.parse(f.read_text())
            mods = set()
            for n in tree.body:
                if isinstance(n, ast.Import):
                    mods |= {(a.asname or a.name).split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom):
                    mods |= {a.asname or a.name for a in n.names}
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                local = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                for m in mods & local:
                    if any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == m
                           for n in ast.walk(fn)):
                        found.append("%s line %d: %s() sets a variable called %s and also uses the %s module"
                                     % (f.relative_to(HERE), fn.lineno, fn.name, m, m))
        self.assertEqual(found, [])


class CrashStopsTest(unittest.TestCase):
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

    def test_crash_is_called_a_patchbay_bug(self):
        def broken(st, emit, a):
            raise UnboundLocalError("cannot access local variable 'where' where it is not associated with a value")
        call = {"tool_calls": [{"function": {"name": "sandbox_run",
                                             "arguments": {"sandbox": "EglantinePS5", "command": "make"}}}]}
        claude = mock.Mock()
        claude.has_key.return_value = True
        claude.mode.return_value = "stuck"
        with mock.patch.object(server, "ask_model", return_value=call) as ask, \
                mock.patch.object(server, "second_opinion", return_value=""), \
                mock.patch.object(server, "APP", mock.Mock(claude=claude)), \
                mock.patch.object(server.traceback, "print_exc"):
            server._chat_loop(self.st, "build it", self.events.append, self.cfg, "system", [],
                              {"sandbox_run": broken})
        self.assertEqual(ask.call_count, 1)          # stopped straight away, no second try
        tool = next(m for m in self.st.messages if m.get("role") == "tool")
        self.assertTrue(tool["content"].startswith(server.BUG_PREFIX))
        self.assertIn("UnboundLocalError", tool["content"])
        stop = next(e for e in self.events if e["type"] == "choices")
        self.assertIn("bug inside PatchBay itself, not a problem with your project", stop["text"])
        self.assertNotIn("Ask Claude", [o["label"] for o in stop["options"]])
        self.assertFalse(self.st.pending)            # no card offering to send it to Claude


if __name__ == "__main__":
    unittest.main()
