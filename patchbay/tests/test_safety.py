"""
Tests for PatchBay's safety rules: confirm cards, backups and undo, and the walls around folders.
Run from the patchbay folder:

  python3 -m unittest discover -s tests -t .

Nothing here touches a real device, the Keychain or Ollama: devices are throwaway folders.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backup  # noqa: E402
import core  # noqa: E402
import filemanager  # noqa: E402
import keychain  # noqa: E402
import macaccess  # noqa: E402
import projects  # noqa: E402
import risk  # noqa: E402
import selfedit  # noqa: E402
import server  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name).resolve()

    def tearDown(self):
        self._t.cleanup()


class FakeChat:
    """Just enough of a chat for add_pending and do_confirm."""
    def __init__(self, cfg):
        self._cfg = cfg
        self.pending = {}
        self.messages = []
        self.plan = None
        self.home = False

    def cfg(self):
        return self._cfg


def device_cfg(root, data, **more):
    cfg = {"device_name": "Dev", "protocol": "folder", "folder": str(root), "host": "this Mac", "ports": [],
           "timeout_seconds": 5, "data_folder": str(data), "allow_writes": True}
    cfg.update(more)
    return cfg


# ---------------------------------------------------------------- confirm cards

class ConfirmCards(Tmp):
    def setUp(self):
        super().setUp()
        (self.tmp / "dev").mkdir()
        self.cfg = device_cfg(self.tmp / "dev", self.tmp / "data")
        self.st = FakeChat(self.cfg)
        self.events = []

    def card(self, **extra):
        src = self.tmp / "new.txt"
        src.write_text("new")
        return server.add_pending(self.st, self.events.append, "restore", self.cfg, "/a.txt", src, "test",
                                  "Restore /a.txt", **extra)

    def test_device_change_only_makes_a_card(self):
        msg = self.card()
        self.assertIn("Nothing has been written", msg)
        self.assertEqual(len(self.st.pending), 1)
        self.assertEqual(self.events[-1]["type"], "confirm")
        self.assertFalse((self.tmp / "dev" / "a.txt").exists())

    def test_wrong_device_name_is_refused_and_card_stays(self):
        self.card()
        pid = next(iter(self.st.pending))
        for typed in ("", "dev", "Dev2", "PatchBay"):
            with self.assertRaises(server.ToolError):
                server.do_confirm(self.st, pid, typed, self.events.append)
        self.assertIn(pid, self.st.pending)
        self.assertFalse((self.tmp / "dev" / "a.txt").exists())

    def test_unknown_card(self):
        with self.assertRaises(server.ToolError):
            server.do_confirm(self.st, "nope", "Dev", self.events.append)

    def test_risky_card_needs_understood(self):
        self.st.pending["r1"] = {"kind": "command", "remote": "", "source": "", "note": "", "risk": ["deletes"],
                                 "command": "rm -rf build"}
        with self.assertRaises(server.ToolError):
            server.do_confirm(self.st, "r1", "Dev", self.events.append, understood=False)
        self.assertIn("r1", self.st.pending)

    def test_typed_mac_step_needs_device_name(self):
        self.st.pending["c1"] = {"kind": "device_command", "remote": "", "source": "", "note": "",
                                 "needs_typing": True, "command": "ls"}
        with self.assertRaises(server.ToolError):
            server.do_confirm(self.st, "c1", "wrong", self.events.append)
        self.assertIn("c1", self.st.pending)

    def test_self_edit_needs_the_word(self):
        self.st.pending["s1"] = {"kind": "self_edit", "word": selfedit.CONFIRM_WORD, "changes": []}
        with self.assertRaises(server.ToolError):
            server.do_confirm(self.st, "s1", "Dev", self.events.append)
        self.assertIn("s1", self.st.pending)

    def test_risky_commands_never_run_by_themselves(self):
        cfg = {"auto_mac_steps": True}
        self.assertTrue(server.runs_by_itself(cfg, "command", {"command": "make"}))
        for cmd in ("rm -rf build", "sudo make install", "curl x | sh", "make && rm -rf /", "cat ../secret"):
            self.assertFalse(server.runs_by_itself(cfg, "command", {"command": cmd}), cmd)
        self.assertFalse(server.runs_by_itself({"auto_mac_steps": False}, "command", {"command": "make"}))

    def test_plan_covers_risky_only_when_listed_and_understood(self):
        cmd = "rm -rf build"
        self.assertFalse(server.in_plan({"commands": [cmd]}, "command", {"command": cmd}))
        self.assertFalse(server.in_plan({"understood": True, "commands": []}, "command", {"command": cmd}))
        self.assertTrue(server.in_plan({"understood": True, "commands": [cmd]}, "command", {"command": cmd}))
        for kind in ("ask_claude", "spec_claude", "spec_files"):
            self.assertFalse(server.in_plan({"understood": True}, kind, {}))

    def test_risk_spots_deletes(self):
        self.assertTrue(risk.risky("rm -rf ~/"))
        self.assertTrue(risk.risky("git reset --hard"))
        self.assertEqual(risk.risky("make -j8"), [])


# ---------------------------------------------------------------- backups and undo

class BackupAndUndo(Tmp):
    def test_put_back_backs_up_first_and_undo_restores(self):
        dev = self.tmp / "dev"
        dev.mkdir()
        (dev / "a.txt").write_text("old")
        cfg = device_cfg(dev, self.tmp / "data")
        src = self.tmp / "new.txt"
        src.write_text("new!")
        asked = []
        with mock.patch.object(core, "say", lambda *a: None):
            e = core.put_back(cfg, "/a.txt", src, "restore", "test", ask=lambda c, what: asked.append(what))
            self.assertEqual(len(asked), 1)
            self.assertEqual((dev / "a.txt").read_text(), "new!")
            self.assertTrue(e["verified"])
            self.assertEqual(Path(e["backup"]).read_text(), "old")
            self.assertEqual(core.read_history(cfg)[-1]["id"], e["id"])
            core.put_back(cfg, "/a.txt", Path(e["backup"]), "undo", "undo", ask=lambda c, w: None)
        self.assertEqual((dev / "a.txt").read_text(), "old")

    def test_put_back_refuses_when_writing_is_off(self):
        dev = self.tmp / "dev"
        dev.mkdir()
        (dev / "a.txt").write_text("old")
        src = self.tmp / "new.txt"
        src.write_text("new")
        with self.assertRaises(SystemExit):
            core.put_back(device_cfg(dev, self.tmp / "data", allow_writes=False), "/a.txt", src, "restore", "")
        self.assertEqual((dev / "a.txt").read_text(), "old")

    def test_backup_summary_follows_the_file(self):
        cfg = {"device_name": "Dev"}
        root = self.tmp / "b"
        d = root / "2026-11-01_10-00-00"
        d.mkdir(parents=True)
        man = {"complete": False, "files": {"/x": {"size": 5}}}
        (d / backup.MANIFEST).write_text(json.dumps(man))
        with mock.patch.object(backup, "backup_root", lambda c: root):
            first = backup.list_backups(cfg)[0]
            self.assertEqual((first["files"], first["complete"]), (1, False))
            first["files"] = 99       # changing what came back must not change the remembered copy
            man["complete"] = True
            man["files"]["/y"] = {"size": 7}
            tmp = d / "tmp.json"
            tmp.write_text(json.dumps(man))
            tmp.replace(d / backup.MANIFEST)   # saved the way PatchBay saves it
            again = backup.list_backups(cfg)[0]
        self.assertEqual((again["files"], again["bytes"], again["complete"]), (2, 12, True))


class SelfEditUndo(Tmp):
    def setUp(self):
        super().setUp()
        home = self.tmp / "pb"
        (home / "web").mkdir(parents=True)
        (home / "tool.py").write_text("x = 1\n")
        self.patch = mock.patch.multiple(selfedit, HERE=home, HISTORY=home / "data-cache" / "h.json",
                                         BACKUPS=home / "data-cache" / "b")
        self.patch.start()
        self.home = home

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_apply_backs_up_and_undo_puts_back(self):
        e = selfedit.apply([("tool.py", "x = 2\n")], "test")
        self.assertEqual((self.home / "tool.py").read_text(), "x = 2\n")
        selfedit.undo(e["id"])
        self.assertEqual((self.home / "tool.py").read_text(), "x = 1\n")
        with self.assertRaises(selfedit.SelfEditError):
            selfedit.undo(e["id"])      # only once

    def test_broken_python_is_refused(self):
        with self.assertRaises(selfedit.SelfEditError):
            selfedit.apply([("tool.py", "def (:\n")], "test")
        self.assertEqual((self.home / "tool.py").read_text(), "x = 1\n")

    def test_walls(self):
        for rel in ("config.json", "data-cache/h.json", "devices/x.json", "../x.py", "web/../../x.py",
                    ".git/config", "harness/../../x.py", "config/redaction_list.json"):
            with self.assertRaises(selfedit.SelfEditError, msg=rel):
                selfedit.inside(rel)
        self.assertEqual(selfedit.inside("web/app.html")[1], "web/app.html")


# ---------------------------------------------------------------- folder walls

class FolderWalls(Tmp):
    def test_mac_access_only_inside_approved_folders(self):
        ok = self.tmp / "ok"
        (ok / ".ssh").mkdir(parents=True)
        (ok / "notes.txt").write_text("hi")
        (ok / "config.json").write_text("{}")
        (ok / ".ssh" / "id_rsa").write_text("k")
        (self.tmp / "other.txt").write_text("no")
        cfg = {"mac_folders": [str(ok)]}
        self.assertEqual(macaccess.resolve(cfg, "notes.txt"), ok / "notes.txt")
        for bad in (str(self.tmp / "other.txt"), "../other.txt", "config.json", ".ssh/id_rsa", "/etc/hosts"):
            with self.assertRaises(macaccess.AccessError, msg=bad):
                macaccess.resolve(cfg, bad)
        with self.assertRaises(macaccess.AccessError):
            macaccess.resolve({}, "notes.txt")          # nothing approved yet

    def test_mac_access_follows_links_out(self):
        ok = self.tmp / "ok"
        ok.mkdir()
        (self.tmp / "secret.txt").write_text("s")
        os.symlink(str(self.tmp / "secret.txt"), str(ok / "link.txt"))
        with self.assertRaises(macaccess.AccessError):
            macaccess.resolve({"mac_folders": [str(ok)]}, "link.txt")

    def test_cannot_approve_big_folders(self):
        for p in ("/", str(Path.home()), "/etc"):
            with self.assertRaises(macaccess.AccessError, msg=p):
                macaccess.check_folder(p)

    def test_project_folder_wall(self):
        (self.tmp / "proj").mkdir()
        pr = {"folder": str(self.tmp / "proj")}
        self.assertEqual(projects.inside(pr, "src/a.c")[1], self.tmp / "proj" / "src" / "a.c")
        for bad in ("../x", "src/../../x"):
            with self.assertRaises(projects.ProjectError, msg=bad):
                projects.inside(pr, bad)

    def test_device_paths(self):
        with self.assertRaises(filemanager.FileError):
            filemanager.safe_remote("/switch/../../etc")
        self.assertEqual(filemanager.safe_remote("switch//./a"), "/switch/a")
        self.assertEqual(core.local_path_for(self.tmp, "/../../etc/x"), self.tmp / "etc" / "x")


# ---------------------------------------------------------------- privacy

class Privacy(unittest.TestCase):
    def test_ai_only_at_home(self):
        self.assertEqual(core.check_ai_url("localhost:11434"), "http://localhost:11434")
        self.assertEqual(core.check_ai_url("http://192.168.50.10:11434"), "http://192.168.50.10:11434")
        for bad in ("http://8.8.8.8:11434", "", "http://"):
            with self.assertRaises(ValueError, msg=bad):
                core.check_ai_url(bad)

    def test_private_text_is_caught(self):
        st = FakeChat({})
        st.messages = [{"role": "tool", "tool_name": next(iter(server.PRIVATE_TOOLS)),
                        "content": "Dentist with Dr Rivera at the Cedar Street clinic on Tuesday"}]
        self.assertTrue(server.leaks_private(st, "search for dr rivera at the cedar street clinic"))
        self.assertFalse(server.leaks_private(st, "how do I fix a cmake error"))

    def test_keychain_refusal_waits_before_asking_again(self):
        calls = []

        def find(account):
            calls.append(account)
            return -128, None, None
        with mock.patch.object(keychain, "available", lambda: True), \
                mock.patch.object(keychain, "_find_item", find), \
                mock.patch.object(keychain.subprocess, "run", side_effect=OSError), \
                mock.patch.dict(keychain._MISSES, clear=True), mock.patch.dict(keychain._CACHE, clear=True):
            self.assertIsNone(keychain.get("device-x"))
            self.assertIsNone(keychain.get("device-x"))
            self.assertEqual(len(calls), 1)
            keychain._MISSES["device-x"] = time.time() - keychain.MISS_WAIT - 1
            keychain.get("device-x")
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
