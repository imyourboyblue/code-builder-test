"""
Tests for the simple-names tool (namestrip.py): strip then restore must give back every file byte-for-byte.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import namestrip  # noqa: E402


def fake_pkg(names, body=b"\xAB" * 5000, utf16=()):
    """A made-up package: magic, a table of NUL-ended names (some in UTF-16), then data."""
    table = b"".join(n.encode() + b"\0" for n in names) + b"\0\0" + b"".join(n.encode("utf-16-le") + b"\0\0" for n in utf16)
    return b"\x7fFKG" + struct.pack(">I", len(table)) + table + body


def snapshot(folder):
    out = {}
    for root, _, files in os.walk(folder):
        for n in files:
            p = Path(root) / n
            out[p.relative_to(folder).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class StripTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        base = Path(self._t.name).resolve()
        self.dir, self.maps = base / "Virtual PS5", base / "maps"
        self.dir.mkdir()
        names = ["My Game - Deluxe (v1.02).fpkg", "patch_01 #2.pkg", "eboot data.bin"]
        (self.dir / names[0]).write_bytes(fake_pkg(names, utf16=["eboot data.bin"]))
        (self.dir / names[1]).write_bytes(fake_pkg(["My Game - Deluxe (v1.02).fpkg", "xeboot data", "eboot data"]))
        (self.dir / names[2]).write_bytes(os.urandom(3000))
        (self.dir / "ünïcode name.bin").write_bytes("ünïcode name.bin".encode() + b"\0" * 100)
        (self.dir / ".DS_Store").write_bytes(b"hidden")

    def tearDown(self):
        self._t.cleanup()

    def strip(self, **kw):
        return namestrip.strip(self.dir, maps=self.maps, **kw)

    def test_round_trip_is_exact(self):
        before = snapshot(self.dir)
        size0 = (self.dir / "My Game - Deluxe (v1.02).fpkg").stat().st_size
        self.strip()
        after = sorted(os.listdir(self.dir))
        self.assertEqual(after, [".DS_Store", ".patchbay-names", "0.bin", "1.fpkg", "2.pkg", "3.bin"])
        head = (self.dir / "1.fpkg").read_bytes()[:400]
        self.assertNotIn(b"My Game", head)
        self.assertNotIn(b"patch_01", head)
        self.assertNotIn("eboot data.bin".encode("utf-16-le"), head)
        self.assertIn(b"1.fpkg\0", head)
        self.assertEqual((self.dir / "1.fpkg").stat().st_size, size0)     # sizes never change
        namestrip.restore(self.dir, maps=self.maps)
        self.assertEqual(snapshot(self.dir), before)
        self.assertFalse(list(self.maps.glob("*.json")))          # map moved to restored/
        self.assertTrue(list((self.maps / "restored").glob("*.json")))

    def test_names_inside_other_words_are_left(self):
        self.strip()
        head = (self.dir / "2.pkg").read_bytes()
        self.assertIn(b"xeboot data\0", head)      # part of another word: left alone
        self.assertNotIn(b"\0eboot data\0", head)    # on its own: swapped for 0

    def test_map_keeps_real_names_out_of_the_folder(self):
        self.strip()
        marker = (self.dir / ".patchbay-names").read_text()
        self.assertNotIn("Game", marker)
        m = json.loads(next(self.maps.glob("*.json")).read_text("utf-8"))
        self.assertIn("My Game - Deluxe (v1.02).fpkg", [f["original"] for f in m["files"]])
        for root, _, files in os.walk(self.dir):
            for n in files:
                self.assertNotIn(b"Deluxe", (Path(root) / n).read_bytes())

    def test_refuses_twice_and_refuses_to_overwrite(self):
        self.strip()
        with self.assertRaises(namestrip.StripError):
            self.strip()
        namestrip.restore(self.dir, maps=self.maps)
        os.mkdir(self.dir / "3.bin")       # a folder sits where a new name would go
        with self.assertRaises(namestrip.StripError):
            self.strip()

    def test_restore_refuses_new_file_at_old_name(self):
        self.strip()
        (self.dir / "patch_01 #2.pkg").write_bytes(b"new")
        with self.assertRaises(namestrip.StripError):
            namestrip.restore(self.dir, maps=self.maps)
        self.assertTrue((self.dir / "2.pkg").exists())             # nothing moved

    def test_preview_changes_nothing_and_stale_preview_is_refused(self):
        before = snapshot(self.dir)
        p = namestrip.plan(self.dir)
        self.assertEqual(snapshot(self.dir), before)
        self.assertIn("->", namestrip.preview_text(p))
        (self.dir / "extra.bin").write_bytes(b"x")
        with self.assertRaises(namestrip.StripError):
            self.strip(expect=p)
        self.assertEqual(sorted(os.listdir(self.dir))[0], ".DS_Store")
        self.assertFalse((self.dir / ".patchbay-names").exists())

    def test_names_that_swap_places(self):
        for f in list(self.dir.iterdir()):
            f.unlink()
        (self.dir / "1.pkg").write_bytes(b"one 1.pkg x")
        (self.dir / "0.pkg").write_bytes(b"zero")
        (self.dir / "a.pkg").write_bytes(b"a")
        before = snapshot(self.dir)
        self.strip()
        namestrip.restore(self.dir, maps=self.maps)
        self.assertEqual(snapshot(self.dir), before)

    def test_subfolders_and_whole_file_scan(self):
        sub = self.dir / "sce_sys"
        sub.mkdir()
        big = b"\0" * (namestrip.CHUNK + 5) + b"/My Game - Deluxe (v1.02).fpkg\0"   # past the first block
        (sub / "param long name.json").write_bytes(big)
        before = snapshot(self.dir)
        self.strip(subfolders=True, scan_mb=0)
        self.assertTrue((sub / "4.json").exists())
        self.assertNotIn(b"Deluxe", (sub / "4.json").read_bytes())
        namestrip.restore(self.dir, maps=self.maps)
        self.assertEqual(snapshot(self.dir), before)

    def test_match_across_a_block_edge(self):
        with mock.patch.object(namestrip, "CHUNK", 64):
            for cut in range(1, 40):
                d = self.dir / ("edge%d" % cut)
                d.mkdir()
                (d / "Long Name Here.bin").write_bytes(b"\x01" * (64 - cut) + b"Long Name Here.bin\0" + b"\x02" * 80)
                (d / "x").write_bytes(b"\x01" * (64 - cut) + b"zLong Name Here\0")   # a word, not the name
                p = namestrip.plan(d)
                hits = [(c["file"], c["offset"]) for c in p["header_changes"]]
                self.assertEqual(hits, [(0, 64 - cut)], cut)

    def test_cut_off_strip_can_be_restored(self):
        before = snapshot(self.dir)
        real = os.rename
        calls = []

        def flaky(a, b):
            calls.append(a)
            if len(calls) == 6:                 # stop halfway through the renames
                raise OSError("disk went away")
            return real(a, b)
        with mock.patch.object(namestrip.os, "rename", flaky):
            with self.assertRaises(OSError):
                self.strip()
        namestrip.restore(self.dir, maps=self.maps)
        self.assertEqual(snapshot(self.dir), before)


if __name__ == "__main__":
    unittest.main()
