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


def _rewrite_paths(folder, old, new):
    """Paths saved inside JSON files (history, backups for Undo, chats) point at the new folder."""
    old_s, new_s = json.dumps(old)[1:-1], json.dumps(new)[1:-1]
    for p in Path(folder).rglob("*.json"):
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
            shutil.move(str(old), str(new))
            _rewrite_paths(new, str(old), str(new))
            notes.append("Moved your saved data from %s to %s." % (cur, NEW_DATA))
        if new.exists() or not old.is_dir():
            raw["data_folder"] = NEW_DATA
            changed = True
    if changed:
        tmp = cfg_path.with_name("config.json.tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        tmp.replace(cfg_path)


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
