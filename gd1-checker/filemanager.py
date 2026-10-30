"""
The Files screen: browse a device's folders next to the Mac Studio's folders,
drag files between them, and drop files from Finder onto the device.

  - Copying from the device to the Mac only reads the device. It never
    overwrites anything on the Mac (a copy gets a new name like "file (2).db").
  - Anything that writes to the device (upload, new folder, rename, move,
    delete) is first described to you, and only happens after you type the
    device's name. Every file that gets replaced or deleted is backed up to the
    Mac first, and the change is listed in the device's history, so it can be
    undone.

Only uses Python's built-in parts.
"""

import ftplib
import json
import os
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath

import gd1check as core

MAX_PLAN_ITEMS = 20000


class FileError(Exception):
    pass


def safe_remote(path):
    path = (path or "/").strip() or "/"
    if any(c in path for c in "\r\n\0"):
        raise FileError("That name has characters the device can't take.")
    if not path.startswith("/"):
        path = "/" + path
    parts = [p for p in PurePosixPath(path).parts[1:] if p not in (".", "")]
    if ".." in parts:
        raise FileError("Paths can't use '..'.")
    return "/" + "/".join(parts)


def safe_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or any(c in name for c in "\r\n\0"):
        raise FileError("Give a plain name without slashes.")
    return name


def join(folder, name):
    return safe_remote(str(PurePosixPath(folder) / name))


# ----------------------------------------------------------------- listing

def connect(cfg):
    dev = core.GD1(cfg)
    try:
        dev.connect(quiet=True)
    except SystemExit as e:
        raise FileError(str(e.code))
    return dev


def list_device(cfg, path):
    path = safe_remote(path)
    dev = connect(cfg)
    try:
        try:
            entries = dev._list(path)
        except ftplib.Error as e:
            raise FileError("Can't open %s on %s: %s" % (path, cfg["device_name"], e))
    finally:
        dev.close()
    rows = []
    for name, kind, size, modified in entries:
        if name in (".", ".."):
            continue
        rows.append({"name": name, "kind": {"dir": "dir", "file": "file"}.get(kind, "link"),
                     "size": size, "modified": pretty_ftp_time(modified)})
    rows.sort(key=lambda r: (r["kind"] != "dir", r["name"].lower()))
    return {"path": path, "entries": rows}


def pretty_ftp_time(t):
    t = t or ""
    if len(t) >= 12 and t[:12].isdigit():  # MLSD gives 20261029153000
        return "%s-%s-%s %s:%s" % (t[:4], t[4:6], t[6:8], t[8:10], t[10:12])
    return t


def mac_home():
    return Path.home()


def list_mac(path, show_hidden=False):
    p = Path(os.path.expanduser(path or "~")).resolve()
    if not p.is_dir():
        raise FileError("%s isn't a folder on the Mac." % p)
    rows = []
    try:
        items = list(p.iterdir())
    except OSError as e:
        raise FileError("The Mac won't let me open %s (%s). In System Settings > Privacy & Security, you can "
                        "give Terminal 'Full Disk Access'." % (p, e.strerror or e))
    for f in items:
        if f.name.startswith(".") and not show_hidden:
            continue
        try:
            st = f.stat()
            is_dir = f.is_dir()
        except OSError:
            continue
        rows.append({"name": f.name, "kind": "dir" if is_dir else "file", "size": None if is_dir else st.st_size,
                     "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    rows.sort(key=lambda r: (r["kind"] != "dir", r["name"].lower()))
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "entries": rows}


# ----------------------------------------------------------------- device -> Mac (read-only on the device)

def unique_local(path):
    path = Path(path)
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name("%s (%d)%s" % (path.stem, n, path.suffix)) if path.suffix else \
            path.with_name("%s (%d)" % (path.name, n))
        if not cand.exists():
            return cand
        n += 1


def copy_to_mac(cfg, items, mac_folder, emit):
    """items: [{"path": "/data/x", "kind": "file"|"dir"}]. Returns how many files were copied."""
    dest = Path(os.path.expanduser(mac_folder)).resolve()
    if not dest.is_dir():
        raise FileError("%s isn't a folder on the Mac." % dest)
    dev = connect(cfg)
    copied = 0
    try:
        for it in items:
            remote = safe_remote(it.get("path"))
            name = PurePosixPath(remote).name or cfg["device_name"]
            if it.get("kind") == "dir":
                top = unique_local(dest / name)
                top.mkdir(parents=True)
                for path, size, _ in dev.walk(remote):
                    rel = PurePosixPath(path).relative_to(remote)
                    local = top.joinpath(*rel.parts)
                    emit({"type": "progress", "text": "Copying %s (%s)" % (path, core.human_size(size))})
                    dev.download(path, local)
                    copied += 1
            else:
                local = unique_local(dest / name)
                emit({"type": "progress", "text": "Copying %s" % remote})
                dev.download(remote, local)
                copied += 1
    finally:
        dev.close()
    return copied


def send_to_browser(cfg, remote, write):
    remote = safe_remote(remote)
    dev = connect(cfg)
    try:
        dev.call("retrbinary", "RETR " + remote, write)
    finally:
        dev.close()


# ----------------------------------------------------------------- files dropped in from the browser

def stage_upload(cfg, name, rfile, length):
    """Save a file sent by the browser to the Mac first. It goes to the device only after you confirm."""
    name = safe_name(name)
    folder = core.data_dir(cfg) / "uploads" / ("%s_%s" % (core.now_stamp(), uuid.uuid4().hex[:6]))
    folder.mkdir(parents=True)
    path = folder / name
    left = length
    with open(path, "wb") as f:
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left:
        shutil.rmtree(folder, ignore_errors=True)
        raise FileError("The upload of %s was cut off. Try again." % name)
    return str(path.relative_to(core.data_dir(cfg)))


def staged_path(cfg, ref):
    root = (core.data_dir(cfg) / "uploads").resolve()
    p = (core.data_dir(cfg) / ref).resolve()
    if root not in p.parents or not p.is_file():
        raise FileError("That uploaded file is gone. Drop it again.")
    return p


# ----------------------------------------------------------------- planning writes (nothing is written here)

def plan(cfg, op, a):
    """Work out exactly what a write would do. Returns (title, lines for you to read, steps)."""
    steps, lines = [], []
    dev = connect(cfg)
    try:
        def existing(folder):
            try:
                return {e[0]: e[1] for e in dev._list(folder)}
            except ftplib.Error:
                return {}

        if op == "upload":
            target = safe_remote(a.get("target"))
            there = existing(target)
            for it in a.get("items") or []:
                if it.get("from") == "staged":
                    local = staged_path(cfg, it.get("ref"))
                else:
                    local = Path(os.path.expanduser(it.get("path") or "")).resolve()
                if not local.exists():
                    raise FileError("%s isn't on the Mac any more." % local)
                name = safe_name(local.name)
                if local.is_dir():
                    top = join(target, name)
                    lines.append("Copy the folder %s into %s" % (name, target)
                                 + (" (merging with the folder already there)" if name in there else ""))
                    steps.append(("mkdir", top))
                    for root, dirs, files in os.walk(local):
                        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                        rel = Path(root).relative_to(local)
                        rdir = top if str(rel) == "." else join(top, rel.as_posix())
                        if rdir != top:
                            steps.append(("mkdir", rdir))
                        for fn in sorted(files):
                            if fn.startswith("."):
                                continue
                            steps.append(("put", str(Path(root) / fn), join(rdir, safe_name(fn))))
                else:
                    remote = join(target, name)
                    if there.get(name) == "dir":
                        raise FileError("%s already has a folder called %s." % (target, name))
                    lines.append(("Replace %s (%s). The current one is backed up first." if name in there
                                  else "Add %s (%s)") % (remote, core.human_size(local.stat().st_size)))
                    steps.append(("put", str(local), remote))
            n_files = sum(1 for s in steps if s[0] == "put")
            title = "Upload %d file%s to %s on %s" % (n_files, "" if n_files == 1 else "s", target, cfg["device_name"])

        elif op == "mkdir":
            target = safe_remote(a.get("target"))
            name = safe_name(a.get("name"))
            if name in existing(target):
                raise FileError("%s already has something called %s." % (target, name))
            steps.append(("mkdir", join(target, name)))
            title = "Make a new folder %s on %s" % (join(target, name), cfg["device_name"])
            lines.append("Create the empty folder %s" % join(target, name))

        elif op in ("rename", "move"):
            if op == "rename":
                src = safe_remote(a.get("path"))
                pairs = [(src, join(str(PurePosixPath(src).parent), safe_name(a.get("new_name"))))]
            else:
                target = safe_remote(a.get("target"))
                pairs = [(safe_remote(p), join(target, PurePosixPath(safe_remote(p)).name)) for p in a.get("paths") or []]
            for old, new in pairs:
                if old == new:
                    continue
                if new.startswith(old + "/"):
                    raise FileError("Can't move %s into itself." % old)
                if PurePosixPath(new).name in existing(str(PurePosixPath(new).parent)):
                    raise FileError("%s already exists. Rename or delete that one first." % new)
                steps.append(("rename", old, new))
                lines.append("%s  ->  %s" % (old, new))
            if not steps:
                raise FileError("That wouldn't change anything.")
            title = ("Rename %s on %s" if op == "rename" else "Move %s on %s") % (
                pairs[0][0] if len(pairs) == 1 else "%d items" % len(pairs), cfg["device_name"])

        elif op == "delete":
            for p in a.get("paths") or []:
                p = safe_remote(p)
                if p == "/":
                    raise FileError("I won't delete the whole device.")
                parent = str(PurePosixPath(p).parent)
                kind = existing(parent).get(PurePosixPath(p).name)
                if kind == "dir":
                    files, dirs = [], []
                    stack = [p]
                    while stack:
                        d = stack.pop()
                        dirs.append(d)
                        for name, k, size, _ in dev._list(d):
                            if name in (".", ".."):
                                continue
                            full = join(d, name)
                            if k == "dir":
                                stack.append(full)
                            else:
                                files.append(full)
                        if len(files) > MAX_PLAN_ITEMS:
                            raise FileError("%s holds more than %d files. Delete smaller parts of it."
                                            % (p, MAX_PLAN_ITEMS))
                    steps += [("delete", f) for f in files] + [("rmdir", d) for d in reversed(dirs)]
                    lines.append("Delete the folder %s and the %d file%s in it (each backed up first)"
                                 % (p, len(files), "" if len(files) == 1 else "s"))
                elif kind:
                    steps.append(("delete", p))
                    lines.append("Delete %s (backed up first)" % p)
                else:
                    raise FileError("%s isn't there any more." % p)
            paths = a.get("paths") or []
            title = "Delete %s on %s" % (safe_remote(paths[0]) if len(paths) == 1 else "%d items" % len(paths),
                                         cfg["device_name"])
        else:
            raise FileError("Unknown action %s." % op)
    finally:
        dev.close()
    if not steps:
        raise FileError("Nothing to do.")
    return title, lines, steps


# ----------------------------------------------------------------- doing the writes (after you confirmed)

def remote_exists(dev, remote):
    if dev.size(remote) is not None:
        return True
    parent, name = str(PurePosixPath(remote).parent), PurePosixPath(remote).name
    try:
        return any(e[0] == name for e in dev._list(parent))
    except ftplib.Error:
        return False


def apply(cfg, steps, note, emit):
    if not cfg.get("allow_writes"):
        raise FileError("Writing to %s is turned off. Set \"allow_writes\": true in its settings file to allow it."
                        % cfg["device_name"])
    dev = connect(cfg)
    stamp = core.now_stamp()
    backup_root = [None]
    done = []

    def backup_of(remote, must_exist):
        if backup_root[0] is None:
            backup_root[0] = core.unique_dir(core.data_dir(cfg) / "backups", stamp)
        local = core.local_path_for(backup_root[0], remote)
        try:
            dev.download(remote, local)
            return local
        except ftplib.error_perm as e:
            if must_exist:
                raise FileError("Couldn't back up %s (%s), so I stopped before changing it." % (remote, e))
            return None

    def record(action, remote, source=None, backup=None, verified=True, extra=""):
        hist = core.read_history(cfg)
        entry = {"id": len(hist) + 1, "time": stamp, "action": action, "remote": remote,
                 "from": str(source) if source else None, "backup": str(backup) if backup else None,
                 "verified": verified, "note": (note + extra).strip()}
        hist.append(entry)
        core.history_path(cfg).write_text(json.dumps(hist, indent=2))
        done.append(entry)

    try:
        for i, step in enumerate(steps, 1):
            kind = step[0]
            emit({"type": "progress", "text": "(%d of %d) %s %s" % (i, len(steps), kind, step[-1])})
            if kind == "mkdir":
                try:
                    dev.call("mkd", step[1])
                    record("mkdir", step[1])
                except ftplib.error_perm:
                    pass  # already there
            elif kind == "put":
                local, remote = Path(step[1]), step[2]
                backup = backup_of(remote, must_exist=True) if remote_exists(dev, remote) else None
                with open(local, "rb") as f:
                    dev.upload(f, remote)
                after = dev.size(remote)
                record("upload", remote, local, backup, after is None or after == local.stat().st_size)
            elif kind == "delete":
                backup = backup_of(step[1], must_exist=True)
                dev.call("delete", step[1])
                record("delete", step[1], None, backup)
            elif kind == "rmdir":
                dev.call("rmd", step[1])
                record("rmdir", step[1])
            elif kind == "rename":
                dev.call("rename", step[1], step[2])
                record("rename", step[2], extra=" (was %s)" % step[1])
    except (ftplib.Error, OSError, EOFError) as e:
        raise FileError("Stopped after %d of %d steps: %s. Everything done so far is in the history and backed up."
                        % (len(done), len(steps), e))
    finally:
        dev.close()
    return done


# ----------------------------------------------------------------- on the Mac side (no device is touched)

def mac_rename(path, new_name):
    p = Path(os.path.expanduser(path or "")).resolve()
    if not p.exists():
        raise FileError("%s isn't on the Mac any more." % p)
    if p == Path.home() or p.parent == p:
        raise FileError("I won't rename that folder.")
    dest = p.with_name(safe_name(new_name))
    if dest.exists():
        raise FileError("%s already has something called %s." % (p.parent, dest.name))
    p.rename(dest)
    return str(dest)


def mac_mkdir(folder, name):
    d = Path(os.path.expanduser(folder or "")).resolve() / safe_name(name)
    if d.exists():
        raise FileError("%s already exists." % d)
    d.mkdir()
    return str(d)
