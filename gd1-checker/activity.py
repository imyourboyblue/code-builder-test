"""
The activity log: everything Patchbay did, for every device, newest first, with Undo where a change can be undone.

It reads what each part of Patchbay already writes down in the device's data folder:
  history.json          file changes on the device (each with a backup of the old file when there was one)
  project-history.json  source code edits on the Mac (each with a backup of the old file)
  settings-history.json settings changes (with a copy of the old settings file)
  commands.json         commands run on SSH devices (these can't be undone)
  and the device's backups.

Undo always goes through the same safe paths as the original change: the current state is backed up first, and the
undo itself is written down too (so an undo can be undone).
"""

import json
import re
import shutil
import time
from pathlib import Path

import backup
import gd1check as core
import filemanager as fm

UNDO_NOTE = re.compile(r"undo of (?:change )?#(\d+)")


class UndoError(Exception):
    pass


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []


def _when(stamp):
    """'2026-10-30_05-49-04' -> seconds since 1970 (local time)."""
    try:
        return time.mktime(time.strptime(stamp[:19], "%Y-%m-%d_%H-%M-%S"))
    except (TypeError, ValueError):
        return 0


DEVICE_WORDS = {"upload": "Put a file on the device", "restore": "Put back a good copy", "edit": "Edited a file",
                "undo": "Undid a change", "delete": "Deleted a file", "mkdir": "Made a folder",
                "rmdir": "Removed a folder", "rename": "Renamed", "helper": "Installed the helper app",
                "install": "Installed a program", "nro_info": "Changed a program's name/version",
                "nro_icon": "Changed a program's icon"}


def device_undo(h):
    """How to undo one device change, or None. Returns (description, steps for filemanager.apply)."""
    a, remote = h.get("action"), h.get("remote")
    if a == "rename":
        m = re.search(r"\(was (.+)\)\s*$", h.get("note") or "")
        return ("Rename %s back to %s" % (remote, m.group(1)), [("rename", remote, m.group(1))]) if m else None
    if a == "mkdir":
        return "Remove the folder %s (only if it's empty)" % remote, [("rmdir", remote)]
    if a == "rmdir":
        return "Make the folder %s again" % remote, [("mkdir", remote)]
    if h.get("backup") and Path(h["backup"]).exists():
        return "Put back the version from before (%s)" % remote, [("put", h["backup"], remote)]
    if a in ("upload", "restore", "edit", "install", "helper", "nro_info", "nro_icon") and not h.get("backup"):
        return "Delete %s (it didn't exist before; it's backed up first)" % remote, [("delete", remote)]
    return None


def collect(registry, limit=400):
    """Every recorded action for every saved device, newest first."""
    items = []
    for f, cfg in registry.all():
        name = cfg["device_name"]
        d = core.data_dir(cfg)
        hist = _read(core.history_path(cfg))
        undone = {int(m.group(1)) for h in hist for m in [UNDO_NOTE.search(h.get("note") or "")] if m}
        for h in hist:
            u = device_undo(h)
            items.append({"id": "%s|device|%s" % (name, h["id"]), "device": name, "when": _when(h.get("time")),
                          "what": "%s: %s" % (DEVICE_WORDS.get(h.get("action"), h.get("action", "Change")), h.get("remote")),
                          "detail": (h.get("note") or "") + ("" if h.get("verified", True) else " (size didn't match)"),
                          "where": "device", "undo": u[0] if u and h["id"] not in undone else None,
                          "undone": h["id"] in undone})
        phist = _read(d / "project-history.json")
        undone_p = {int(m.group(1)) for h in phist for m in [UNDO_NOTE.search(h.get("note") or "")] if m}
        for h in phist:
            ok = h.get("backup") and Path(h["backup"]).exists() and h["id"] not in undone_p
            items.append({"id": "%s|project|%s" % (name, h["id"]), "device": name, "when": _when(h.get("time")),
                          "what": "Edited source code: %s / %s" % (h.get("project"), h.get("file")),
                          "detail": h.get("note") or "", "where": "mac",
                          "undo": "Put back the version of %s from before" % h.get("file") if ok else None,
                          "undone": h["id"] in undone_p})
        shist = _read(d / "settings-history.json")
        undone_s = {int(m.group(1)) for h in shist for m in [UNDO_NOTE.search(h.get("note") or "")] if m}
        for h in shist:
            ok = h.get("backup") and Path(h["backup"]).is_dir() and h["id"] not in undone_s
            items.append({"id": "%s|settings|%s" % (name, h["id"]), "device": name, "when": _when(h.get("time")),
                          "what": "Changed settings: %s" % ", ".join(h.get("changes") or []),
                          "detail": h.get("note") or "", "where": "mac",
                          "undo": "Put the old settings back" if ok else None, "undone": h["id"] in undone_s})
        for i, h in enumerate(_read(d / "commands.json"), 1):
            items.append({"id": "%s|command|%d" % (name, i), "device": name, "when": _when(h.get("time")),
                          "what": "Ran a command on the device: %s" % h.get("remote"),
                          "detail": "%sexit code %s. Commands can't be undone by Patchbay." % (
                              (h.get("note") + "; ") if h.get("note") else "", h.get("exit_code")),
                          "where": "device", "undo": None, "undone": False})
        try:
            for b in backup.list_backups(cfg):
                items.append({"id": "%s|backup|%s" % (name, b.get("name")), "device": name,
                              "when": _when(b.get("name")),
                              "what": "Backup%s: %s" % ("" if b.get("complete") else " (not finished)",
                                                        b.get("label") or ", ".join(b.get("folders") or ["/"])),
                              "detail": "%d files, kept on this Mac" % b.get("files", 0),
                              "where": "mac", "undo": None, "undone": False})
        except Exception:
            pass
    try:
        import selfedit
        for h in selfedit.history():
            items.append({"id": "Patchbay|self|%s" % h["id"], "device": "Patchbay", "when": _when(h.get("time")),
                          "what": "Changed Patchbay itself: %s" % ", ".join(f["path"] for f in h["files"]),
                          "detail": h.get("note") or "", "where": "mac",
                          "undo": None if h.get("undone_by") else "Put back the version from before",
                          "undone": bool(h.get("undone_by"))})
    except Exception:
        pass
    for x in items:   # same second: the later entry (higher number) first
        tail = x["id"].rsplit("|", 1)[-1]
        x["_n"] = int(tail) if tail.isdigit() else 0
    items.sort(key=lambda x: (-(x["when"] or 0), -x.pop("_n")))
    return items[:limit]


def undo(registry, item_id, emit=lambda ev: None):
    """Undo one recorded change. Returns a plain message."""
    try:
        name, kind, ref = item_id.split("|", 2)
    except ValueError:
        raise UndoError("That isn't a change Patchbay knows.")
    if kind == "self":
        import selfedit
        try:
            return selfedit.undo(ref)
        except selfedit.SelfEditError as e:
            raise UndoError(str(e))
    try:
        cfg = core.load_config(registry.path_for(name))
    except Exception:
        raise UndoError("There's no saved device called %s any more." % name)
    d = core.data_dir(cfg)
    if kind == "device":
        h = next((x for x in _read(core.history_path(cfg)) if str(x["id"]) == ref), None)
        u = device_undo(h) if h else None
        if not u:
            raise UndoError("That change can't be undone.")
        if not cfg.get("allow_writes"):
            raise UndoError("Writing to %s is turned off, so it can't be changed." % name)
        try:
            done = fm.apply(cfg, u[1], "undo of change #%s" % ref, emit)
        except fm.FileError as e:
            raise UndoError(str(e))
        return "Undone: %s. (Recorded as change #%s.)" % (u[0], ", #".join(str(x["id"]) for x in done) or "?")
    if kind == "project":
        log = d / "project-history.json"
        hist = _read(log)
        h = next((x for x in hist if str(x["id"]) == ref), None)
        if not h or not h.get("backup") or not Path(h["backup"]).exists():
            raise UndoError("There's no earlier version of that file to put back.")
        import projects
        try:
            pr = projects.get(cfg, h["project"])
        except Exception:
            pr = None
        if not pr:
            raise UndoError("The project %s isn't saved any more." % h["project"])
        projects.write_file(cfg, pr, h["file"], Path(h["backup"]).read_text("utf-8", "replace"))
        hist = _read(log)
        if hist:
            hist[-1]["note"] = "undo of #%s" % ref
            log.write_text(json.dumps(hist, indent=2))
        return "Put back the earlier version of %s (the current one was backed up first)." % h["file"]
    if kind == "settings":
        log = d / "settings-history.json"
        hist = _read(log)
        h = next((x for x in hist if str(x["id"]) == ref), None)
        if not h or not Path(h.get("backup") or "").is_dir():
            raise UndoError("The old settings aren't there any more.")
        stamp = core.now_stamp()
        keep = core.unique_dir(d / "settings-backups", stamp)
        for f in h.get("files") or []:
            src, dst = Path(h["backup"]) / Path(f).name, Path(f)
            if src.exists():
                if dst.exists():
                    shutil.copy2(dst, keep / dst.name)
                tmp = dst.with_name(dst.name + ".tmp")
                shutil.copy2(src, tmp)
                tmp.replace(dst)
        hist.append({"id": len(hist) + 1, "time": stamp, "changes": ["(undo)"] + (h.get("changes") or []),
                     "files": h.get("files"), "backup": str(keep), "note": "undo of #%s" % ref})
        log.write_text(json.dumps(hist, indent=2))
        registry.status.clear()
        return "Put the old settings back (the settings from just now were saved first). Some settings need a restart."
    raise UndoError("That kind of action can't be undone.")
