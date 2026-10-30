"""
Clearing what PatchBay keeps on the Mac for a device (backups, scan results, history, chats, ...), to remove a
device completely or start over with it. Nothing on the device itself is touched.

Each kind of data is its own tick box. The saved password is separate and never removed unless asked for.
"""

import json
import os
import shutil
from pathlib import Path

import backup
import core
import keychain

# kind -> (label, what it means, ticked by default, names inside the device's data folder)
PARTS = {
    "backups": ("Backups", "Full and partial backups of the device. Without them you can't put old files back.",
                True, None),
    "scans": ("Scan results", "Reports, file fingerprints and the files on Needs attention.", True,
              ["reports", "fingerprints.json", "attention.json", "accepted.json", "needs-attention", "snapshots",
               "checking"]),
    "history": ("Change history and Undo", "The Activity list for this device and the old copies Undo uses. After "
                "clearing, those changes can't be undone.", True,
                ["history.json", "project-history.json", "settings-history.json", "settings-backups", "edits",
                 "uploads", "commands.json"]),
    "chats": ("Chats", "Saved chats with this device, and files dropped into them.", True, ["chats"]),
    "info": ("Device info and picture", "Its picture, firmware notes and the app list PatchBay read.", True,
             ["device.json", "portrait.jpg", "portrait.png", "library"]),
}


def _size(p):
    p = Path(p)
    if not p.exists():
        return 0, 0
    if p.is_file():
        return p.stat().st_size, 1
    total = count = 0
    for dirpath, _, names in os.walk(str(p)):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
                count += 1
            except OSError:
                pass
    return total, count


def _paths(cfg, kind):
    if kind == "backups":
        return [backup.backup_root(cfg)]
    d = core.data_dir(cfg)
    return [d / n for n in PARTS[kind][3]]


def summary(cfg, settings_file, main_config):
    """What can be cleared for one device, with sizes, for the confirm screen."""
    parts = []
    for kind, (label, what, default, _) in PARTS.items():
        b = n = 0
        for p in _paths(cfg, kind):
            s, c = _size(p)
            b, n = b + s, n + c
        parts.append({"id": kind, "label": label, "what": what, "default": default, "bytes": b, "files": n,
                      "size": core.human_size(b)})
    pw = cfg.get("password") or ""
    is_main = Path(settings_file).resolve() == Path(main_config).resolve()
    return {"device": cfg["device_name"], "parts": parts,
            "password": {"saved": bool(pw) and pw not in keychain.NOT_SECRET,
                         "keychain": bool(keychain.is_pointer(_raw_password(settings_file)))},
            "can_remove": not is_main,
            "total": core.human_size(sum(p["bytes"] for p in parts))}


def _raw_password(settings_file):
    try:
        return json.loads(Path(settings_file).read_text()).get("password")
    except (OSError, ValueError):
        return None


def clear(cfg, settings_file, main_config, parts, password=False, remove=False):
    """Delete the chosen kinds of data. Returns plain sentences saying what went."""
    done = []
    for kind in parts:
        if kind not in PARTS:
            continue
        freed = 0
        for p in _paths(cfg, kind):
            freed += _size(p)[0]
            if p.is_dir():
                shutil.rmtree(str(p), ignore_errors=True)
            elif p.exists():
                p.unlink()
        done.append("%s (%s)" % (PARTS[kind][0], core.human_size(freed)))
    if password:
        raw = _raw_password(settings_file)
        if keychain.is_pointer(raw):
            keychain.remove(raw[len(keychain.PREFIX):])
        if raw not in (None, ""):
            _set_password(settings_file, "")
        done.append("the saved password")
    is_main = Path(settings_file).resolve() == Path(main_config).resolve()
    if remove and not is_main:
        d = core.data_dir(cfg)
        Path(settings_file).unlink()
        try:     # the data folder is gone too if nothing is left in it
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
        done.append("the device itself from PatchBay's list")
    return done


def _set_password(settings_file, value):
    p = Path(settings_file)
    raw = json.loads(p.read_text())
    raw["password"] = value
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    tmp.replace(p)
