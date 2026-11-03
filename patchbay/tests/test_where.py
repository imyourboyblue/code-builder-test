"""
Tests for the "Runs on / Changes on" lines on confirm cards (where.py), and that a Mac command which sends to a
device is treated like a device change: always a card, and the device name must be typed.

Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
import where  # noqa: E402
from tests.test_safety import FakeChat, device_cfg  # noqa: E402

DEVICES = [("PS5", "192.168.50.203"), ("Switch", "192.168.50.120")]
PS5_INSTALL = "make install PS5_HOST=192.168.50.203"


class Describe(unittest.TestCase):
    def test_make_install_to_ps5_changes_the_ps5(self):
        w = where.describe(PS5_INSTALL, DEVICES, "Mac Studio")
        self.assertEqual(w["level"], "device")
        self.assertEqual(w["runs_on"], "Mac Studio (this Mac)")
        self.assertEqual(w["changes_on"], "PS5 (192.168.50.203)")
        self.assertEqual(w["type_word"], "PS5")
        self.assertNotIn("touch", w["changes_on"].lower())

    def test_unsaved_device_still_named_from_the_setting(self):
        w = where.describe(PS5_INSTALL, [], "Mac Studio")
        self.assertEqual(w["level"], "device")
        self.assertEqual(w["changes_on"], "PS5 (192.168.50.203)")
        self.assertEqual(w["type_word"], "")

    def test_uploads_are_spotted(self):
        for cmd, name in [
                ("curl -T ftpd.nro ftp://192.168.50.120:5000/switch/", "Switch"),
                ("scp out/app.nro root@192.168.50.120:/switch/", "Switch"),
                ("rsync -av build/ me@192.168.50.203:/data/", "PS5"),
                ("nxlink -a 192.168.50.120 ftpd.nro", "Switch"),
                ("lftp -e 'put a.bin' 192.168.50.203", "PS5"),
                ("make && make send SWITCH_IP=192.168.50.120", "Switch"),
                ("cmake --build build && ./upload.sh --host 192.168.50.203", "PS5")]:
            w = where.describe(cmd, DEVICES)
            self.assertEqual(w["level"], "device", cmd)
            self.assertIn(name, w["changes_on"], cmd)

    def test_mac_only_commands_stay_calm(self):
        for cmd in ("make", "make clean", "cmake -B build", "git submodule update --init --recursive",
                    "make STRIP=arm-none-eabi-strip SKIP_TESTS=1", "make TARGET=release", "cmake --install build",
                    "git clone https://github.com/mtheall/ftpd", "curl -L https://example.com/x.tar.gz -o x.tar.gz",
                    "make install HOST=localhost PREFIX=/opt/devkitpro"):
            w = where.describe(cmd, DEVICES)
            self.assertNotEqual(w["level"], "device", cmd)
        self.assertEqual(where.describe("make", DEVICES)["changes_on"], where.MAC_ONLY)

    def test_bare_make_install_is_a_maybe(self):
        w = where.describe("make install", DEVICES)
        self.assertEqual(w["level"], "maybe")
        self.assertIn("Maybe a device", w["changes_on"])

    def test_device_write_and_device_command_labels(self):
        cfg = {"device_name": "PS5", "host": "192.168.50.203", "protocol": "ftp"}
        w = where.for_device_write(cfg, "Mac Studio")
        self.assertEqual((w["level"], w["runs_on"], w["changes_on"]),
                         ("device", "Mac Studio (this Mac)", "PS5 (192.168.50.203)"))
        self.assertEqual(where.on_device(cfg)["runs_on"], "PS5 (192.168.50.203)")

    def test_ai_rule_names_the_ps5_example(self):
        self.assertIn("doesn't touch", where.rule_text())
        self.assertIn("PS5_HOST", where.rule_text())
        self.assertIn(where.rule_text(), server.SYSTEM)


class CommandCards(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        tmp = Path(self._t.name)
        (tmp / "dev").mkdir()
        self.cfg = device_cfg(tmp / "dev", tmp / "data", device_name="Switch", auto_mac_steps=True)
        self.st = FakeChat(self.cfg)
        self.events = []
        self.root = tmp

    def tearDown(self):
        self._t.cleanup()

    def command_card(self, cmd):
        return server.add_pending(self.st, self.events.append, "command", self.cfg, "ftpd", self.root, "",
                                  "Run a command in project ftpd", "In folder: %s\nRun: %s" % (self.root, cmd),
                                  on_mac=True, extra_fields={"project": "ftpd", "command": cmd, "risk": []})

    def test_ps5_install_never_runs_by_itself(self):
        self.assertTrue(server.runs_by_itself({"auto_mac_steps": True}, "command", {"command": "make"}))
        self.assertFalse(server.runs_by_itself({"auto_mac_steps": True}, "command", {"command": PS5_INSTALL}))
        self.assertFalse(server.runs_by_itself({"auto_mac_steps": True}, "command", {"command": "make install"}))

    def test_ps5_install_card_says_ps5_and_needs_its_name(self):
        with mock.patch.object(server, "known_devices", return_value=DEVICES):
            msg = self.command_card(PS5_INSTALL)
        ev = self.events[-1]
        self.assertEqual(ev["type"], "confirm")
        self.assertEqual(ev["where"]["level"], "device")
        self.assertEqual(ev["where"]["changes_on"], "PS5 (192.168.50.203)")
        self.assertTrue(ev["typing"])
        self.assertEqual(ev["type_word"], "PS5")
        self.assertIn("PS5 (192.168.50.203)", msg)       # the AI is told to say so
        self.assertIn("Never say it doesn't touch", msg)
        pid = ev["id"]
        for typed in ("", "Switch", "ps5"):
            with self.assertRaises(server.ToolError):
                server.do_confirm(self.st, pid, typed, self.events.append)
        self.assertIn(pid, self.st.pending)

    def test_plain_build_command_says_mac_only(self):
        self.cfg["auto_mac_steps"] = False
        self.command_card("make clean")
        ev = self.events[-1]
        self.assertEqual(ev["where"]["level"], "mac")
        self.assertEqual(ev["where"]["changes_on"], where.MAC_ONLY)
        self.assertFalse(ev["typing"])

    def test_plan_covers_device_command_only_when_typed_and_listed(self):
        extra = {"command": PS5_INSTALL}
        self.assertFalse(server.in_plan({"commands": [PS5_INSTALL]}, "command", extra))
        self.assertFalse(server.in_plan({"typed": True, "commands": []}, "command", extra))
        self.assertTrue(server.in_plan({"typed": True, "commands": [PS5_INSTALL]}, "command", extra))

    def test_device_write_card_names_the_device(self):
        src = self.root / "new.txt"
        src.write_text("new")
        self.cfg.update(protocol="ftp", host="192.168.50.120")
        server.add_pending(self.st, self.events.append, "install", self.cfg, "/switch/ftpd.nro", src, "", "Install")
        self.assertEqual(self.events[-1]["where"]["changes_on"], "Switch (192.168.50.120)")
        self.assertEqual(self.events[-1]["where"]["level"], "device")


if __name__ == "__main__":
    unittest.main()
