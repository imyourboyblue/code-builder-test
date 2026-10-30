"""
Moving things over after PatchBay was renamed (it used to be called the GD1 checker).

Runs at every start and does nothing once everything is in place:
  1. Settings, passwords pointers, caches and chats left in the old "gd1-checker" folder move into this folder.
  2. The data folder "~/GD1-checker-data" becomes "~/PatchBay-data" (and the paths saved inside it are updated).
  3. A first device that never had a name kept the old default name "GD1", so its data is still found.
Nothing on any device is touched. What happened is saved in data-cache/renamed.json for 🩺 Setup.
"""

import json
import os
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD_FOLDER = HERE.parent / "gd1-checker"
NEW_DATA = "~/PatchBay-data"
OLD_DATA = ("~/GD1-checker-data",)
STUBS = {"patchbay.py", "gd1chat.py", "gd1check.py", "README.md", "__pycache__", ".DS_Store"}
NOTES = HERE / "data-cache" / "renamed.json"


def _merge(src, dst, moved):
    """Move src to dst. If both are folders, move what's inside that dst doesn't have yet."""
    if not dst.exists():
        shutil.move(str(src), str(dst))
        moved.append(dst.name)
        return
    if src.is_dir() and dst.is_dir():
        for child in list(src.iterdir()):
            _merge(child, dst / child.name, moved)
        try:
            src.rmdir()
        except OSError:
            pass


def _old_folder():
    if not OLD_FOLDER.is_dir() or OLD_FOLDER.resolve() == HERE:
        return []
    moved = []
    for item in sorted(OLD_FOLDER.iterdir()):
        if item.name in STUBS:
            continue
        try:
            _merge(item, HERE / item.name, moved)
        except OSError:
            pass
    return moved


# Big folders full of device files: nothing in them saves a path to the data folder, so they are skipped
# (reading them all made the first start take minutes).
SKIP_DIRS = {"snapshots", "checking", "needs-attention", "library", "edits", "uploads", "backups",
             "restore-unpacked", "chat-files", "staged", "project-backups"}
PENDING = HERE / "data-cache" / "rename-pending.json"


def _small_json(folder, depth=3):
    """The JSON files PatchBay itself writes (history, Undo backups list, chats), not copies from devices."""
    folder = Path(folder)
    if depth < 0 or not folder.is_dir():
        return
    try:
        items = list(folder.iterdir())
    except OSError:
        return
    for p in items:
        if p.is_dir():
            if p.name not in SKIP_DIRS and not p.is_symlink():
                yield from _small_json(p, depth - 1)
        elif p.suffix == ".json" and p.stat().st_size < 20_000_000:
            yield p


def _rewrite_paths(folder, old, new):
    """Paths saved inside JSON files (history, backups for Undo, chats) point at the new folder."""
    old_s, new_s = json.dumps(old)[1:-1], json.dumps(new)[1:-1]
    for p in _small_json(folder):
        try:
            text = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if old_s in text:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(text.replace(old_s, new_s))
            tmp.replace(p)


def _data_folder(notes):
    cfg_path = HERE / "config.json"
    try:
        raw = json.loads(cfg_path.read_text())
    except (OSError, ValueError):
        return
    changed = False
    if not raw.get("device_name"):
        raw["device_name"] = "GD1"       # the old default; keeps its data and history where they are
        changed = True
        notes.append("Your first device had no name, so it kept the old name GD1. Rename it any time with "
                     "✎ Rename device on its Files tab.")
    cur = raw.get("data_folder")
    old_paths = [os.path.expanduser(x) for x in OLD_DATA]
    if cur is None:
        old_paths.append(str(HERE / "gd1-data"))       # the old built-in default
        cur = str(HERE / "gd1-data")
    if os.path.expanduser(cur) in old_paths:
        old, new = Path(os.path.expanduser(cur)), Path(os.path.expanduser(NEW_DATA))
        if old.is_dir() and not new.exists():
            PENDING.parent.mkdir(parents=True, exist_ok=True)
            PENDING.write_text(json.dumps({"old": str(old), "new": str(new)}))
            shutil.move(str(old), str(new))      # same disk: a quick rename, nothing is copied
            notes.append("Moved your saved data from %s to %s." % (cur, NEW_DATA))
        if new.exists() or not old.is_dir():
            raw["data_folder"] = NEW_DATA
            changed = True
    if changed:     # saved before the slower step below, so a quit halfway can't lose track of the data
        tmp = cfg_path.with_name("config.json.tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        tmp.replace(cfg_path)
    _finish_rewrite()


def _finish_rewrite():
    """Update the saved paths (resumes at the next start if PatchBay was closed halfway)."""
    try:
        job = json.loads(PENDING.read_text())
    except (OSError, ValueError):
        return
    if Path(job["new"]).is_dir():
        _rewrite_paths(job["new"], job["old"], job["new"])
    PENDING.unlink()


def run():
    notes = []
    try:
        moved = _old_folder()
        if moved:
            notes.append("Moved your settings and saved files from the old gd1-checker folder: %s."
                         % ", ".join(sorted(set(moved))))
        _data_folder(notes)
    except Exception as e:      # never stop PatchBay from starting
        notes.append("Couldn't finish moving things over after the rename: %s" % e)
    if notes:
        try:
            NOTES.parent.mkdir(parents=True, exist_ok=True)
            old = json.loads(NOTES.read_text()) if NOTES.exists() else []
            NOTES.write_text(json.dumps(old + notes, indent=2))
        except (OSError, ValueError):
            pass
        for n in notes:
            print(n)
    return notes
