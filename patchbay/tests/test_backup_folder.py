"""
The backup folder: PatchBay makes its own folder in the home folder if it's missing,
but a folder on a drive that isn't plugged in gives a plain message instead.
Run from the patchbay folder:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backup  # noqa: E402


class BackupFolder(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.home = Path(self._t.name).resolve()
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._t.cleanup()

    def test_default_folder_is_made(self):
        cfg = {"device_name": "Switch"}
        folder = backup.ready_folder(cfg)
        self.assertEqual(folder, self.home / "Patchbay-backups")
        self.assertTrue(folder.is_dir())

    def test_saved_default_path_is_made(self):
        # The settings name the default folder by its full path (the bug Joshua hit).
        cfg = {"device_name": "Switch", "backup_folder": str(self.home / "Patchbay-backups")}
        self.assertTrue(backup.ready_folder(cfg).is_dir())

    def test_folder_in_home_is_made(self):
        cfg = {"device_name": "Switch", "backup_folder": str(self.home / "Backups" / "PatchBay")}
        self.assertTrue(backup.ready_folder(cfg).is_dir())

    def test_unplugged_drive_is_reported_not_made(self):
        cfg = {"device_name": "Switch", "backup_folder": "/Volumes/NoSuchDrive123/PatchBay"}
        with self.assertRaises(backup.BackupError) as e:
            backup.ready_folder(cfg)
        self.assertIn("isn't plugged in", str(e.exception))
        self.assertIn("NoSuchDrive123", str(e.exception))
        self.assertFalse(Path("/Volumes/NoSuchDrive123").exists())

    def test_missing_folder_outside_home_is_reported(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        try:
            cfg = {"device_name": "Switch", "backup_folder": str(outside / "gone")}
            with self.assertRaises(backup.BackupError) as e:
                backup.ready_folder(cfg)
            self.assertIn("doesn't exist", str(e.exception))
            self.assertNotIn("plugged in", str(e.exception))
        finally:
            os.rmdir(outside)

    def test_backup_run_makes_the_default_folder(self):
        # The safety backup before a plan calls backup.run directly; it must not fail on a missing folder.
        cfg = {"device_name": "Switch"}
        with mock.patch.object(backup.core, "unique_dir", side_effect=RuntimeError("stop here")):
            with self.assertRaises(RuntimeError):
                backup.run(cfg, None, items=[{"path": "/user", "kind": "dir"}])
        self.assertTrue((self.home / "Patchbay-backups" / "Switch").is_dir())


if __name__ == "__main__":
    unittest.main()
