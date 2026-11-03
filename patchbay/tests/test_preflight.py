"""
Tests for the tool check before installs (preflight.py and the check_tools chat tool).
Run from the patchbay folder:

  python3 -m unittest discover -s tests -t .

Nothing is installed: the Mac's tools, Homebrew and devkitPro are faked.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import preflight  # noqa: E402
import projects  # noqa: E402
import server  # noqa: E402
from tests.test_safety import FakeChat, device_cfg  # noqa: E402


def fake_mac(have=(), dkp=(), brew=True, xcode=True):
    """Pretend the Mac has these programs and devkitPro packages."""
    have = set(have)
    return [
        mock.patch.object(preflight, "_which", lambda bins: next((b for b in bins if b in have), None)),
        mock.patch.object(preflight, "_version", lambda p: ""),
        mock.patch.object(preflight, "xcode_tools_ok", lambda: xcode),
        mock.patch.object(projects, "dkp_installed", lambda p: "%s 1.0" % p if p in dkp else None),
        mock.patch.object(projects, "find_pacman", lambda: "/opt/devkitpro/pacman/bin/pacman" if dkp else None),
        mock.patch.object(projects, "find_brew", lambda: "/opt/homebrew/bin/brew" if brew else None),
        mock.patch.object(projects, "dkp_available", lambda c: None),
        mock.patch.object(preflight, "brew_has_formula", lambda n: n == "sdl2"),
    ]


class Fake(unittest.TestCase):
    def mac(self, **kw):
        for p in fake_mac(**kw):
            p.start()
            self.addCleanup(p.stop)


class Checklist(Fake):
    def test_found_and_missing(self):
        self.mac(have={"cmake", "make", "git"}, dkp={"libnx", "devkitA64"})
        rows = {r["name"]: r for r in preflight.check(["cmake", "make", "imagemagick", "switch-dev", "switch-zlib"])}
        self.assertTrue(rows["cmake"]["found"])
        self.assertTrue(rows["switch-dev"]["found"])          # a group: checked through libnx + devkitA64
        self.assertFalse(rows["imagemagick"]["found"])
        self.assertEqual(rows["imagemagick"]["via"], "brew")
        self.assertEqual(rows["switch-zlib"]["via"], "devkitpro")

    def test_install_list_is_one_list_for_every_kind(self):
        self.mac(have=set(), dkp={"libnx"}, xcode=False)
        rows = preflight.check(["make", "git", "cmake", "brew:sdl2", "switch-zlib", "pkgconf"])
        self.assertEqual(preflight.install_list(rows),
                         ["xcode:xcode-tools", "brew:cmake", "brew:sdl2", "devkitpro:switch-zlib", "brew:pkgconf"])

    def test_unknown_tool_says_to_research(self):
        self.mac()
        row = preflight.check_one("weirdtool")
        self.assertFalse(row["found"])
        self.assertIsNone(row["via"])
        self.assertIn("research", row["how"])

    def test_bad_names_are_refused(self):
        self.mac()
        self.assertIn("doesn't look", preflight.check_one("cmake; rm -rf ~")["how"])


class ProjectNeeds(unittest.TestCase):
    def test_reads_build_files_and_ci(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "CMakeLists.txt").write_text("include(/opt/devkitpro/portlibs/switch/share/x)\npkg_check_modules(A)\n")
            (d / ".gitmodules").write_text("")
            (d / ".github" / "workflows").mkdir(parents=True)
            (d / ".github" / "workflows" / "mac.yml").write_text("run: brew install ninja imagemagick\n")
            with mock.patch.object(projects, "needed_packages", lambda f: ("switch", ["switch-zlib"], [])):
                need = preflight.needed_for_project(d)
        for x in ("cmake", "switch-cmake", "switch-dev", "git", "pkg-config", "ninja", "brew:imagemagick",
                  "switch-zlib"):
            self.assertIn(x, need)


class MixedPlan(unittest.TestCase):
    def test_brew_runs_as_user_and_devkitpro_asks_for_the_password(self):
        with mock.patch.object(projects, "find_brew", lambda: "/opt/homebrew/bin/brew"), \
                mock.patch.object(projects, "find_pacman", lambda: "/opt/devkitpro/pacman/bin/pacman"), \
                mock.patch.object(projects, "dkp_available", lambda c: None):
            user, admin, words = projects.mixed_plan(["brew:cmake", "brew:ninja", "devkitpro:switch-zlib"])
        self.assertEqual(len(user), 1)
        self.assertIn("install cmake ninja", user[0])
        self.assertIn("switch-zlib", admin[0])
        self.assertIn("password", words)

    def test_refuses_anything_else(self):
        with self.assertRaises(projects.ProjectError):
            projects.mixed_plan(["sh:rm -rf /"])
        with self.assertRaises(projects.ProjectError):
            projects.mixed_plan(["brew:cmake && curl evil"])


class CheckToolsTool(Fake):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        tmp = Path(self._t.name)
        (tmp / "dev").mkdir()
        self.cfg = device_cfg(tmp / "dev", tmp / "data", auto_mac_steps=False)
        self.st = FakeChat(self.cfg)
        self.events = []

    def test_missing_tools_make_one_card(self):
        self.mac(have={"cmake"}, dkp={"libnx"})
        out = server.t_check_tools(self.st, self.events.append, {"tools": ["cmake", "ninja", "imagemagick",
                                                                         "switch-zlib"]})
        self.assertEqual(len(self.st.pending), 1)
        card = next(iter(self.st.pending.values()))
        self.assertEqual(card["kind"], "install_packages")
        self.assertEqual(card["manager"], "mixed")
        self.assertEqual(card["packages"], ["brew:ninja", "brew:imagemagick", "devkitpro:switch-zlib"])
        self.assertIn("Nothing has happened yet", out)
        confirm = [e for e in self.events if e["type"] == "confirm"][0]
        self.assertIn("Commands, in order", confirm["diff"])
        self.assertIn("password", confirm["diff"])
        self.assertTrue(any(e["type"] == "note" and "✗" in e["text"] for e in self.events))

    def test_nothing_missing_means_no_card(self):
        self.mac(have={"cmake", "ninja"})
        out = server.t_check_tools(self.st, self.events.append, {"tools": ["cmake", "ninja"]})
        self.assertEqual(self.st.pending, {})
        self.assertIn("already on the Mac", out)

    def test_no_homebrew_leaves_brew_tools_off_the_card(self):
        self.mac(have=set(), dkp={"libnx"}, brew=False)
        server.t_check_tools(self.st, self.events.append, {"tools": ["cmake", "switch-zlib"]})
        card = next(iter(self.st.pending.values()))
        self.assertEqual(card["packages"], ["devkitpro:switch-zlib"])
        note = [e for e in self.events if e["type"] == "note"][0]["text"]
        self.assertIn("brew.sh", note)


if __name__ == "__main__":
    unittest.main()
