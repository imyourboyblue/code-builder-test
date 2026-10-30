"""
Full backups: copy everything on a device to a folder you choose.

  - Only reads the device. Nothing on it is changed.
  - Each backup is a dated folder, e.g.  <your folder>/GD1/2026-10-30_02-10-00/
    with the device's files inside, laid out exactly as on the device.
  - Files that haven't changed since the last complete backup are not copied
    again. They're linked to the earlier copy (like Time Machine), so every
    backup looks complete but only takes the space of what changed. If the
    drive can't do links (some USB sticks), the file is simply copied.
  - If a backup stops (you stop it, the device goes off, the Mac sleeps),
    starting again carries on where it left off.

Only uses Python's built-in parts.
"""

import fnmatch
import ftplib
import hashlib
import json
import re
import zipfile
import os
import shutil
import threading
import time
from pathlib import Path, PurePosixPath

import gd1check as core

MANIFEST = "_backup.json"
MAX_FAILS_IN_A_ROW = 20
DEFAULT_FOLDER = "~/Patchbay-backups"


class BackupError(Exception):
    pass


def backup_root(cfg):
    folder = Path(os.path.expanduser(cfg.get("backup_folder") or DEFAULT_FOLDER))
    return folder / cfg["device_name"]


def check_folder(path):
    """The folder must exist and be writable (a drive that isn't plugged in fails here)."""
    p = Path(os.path.expanduser(path or "")).resolve()
    if not p.is_dir():
        raise BackupError("%s isn't a folder on the Mac (if it's a drive, is it plugged in?)." % p)
    test = p / ".patchbay-write-test"
    try:
        test.write_text("ok")
        test.unlink()
    except OSError as e:
        raise BackupError("The Mac can't save into %s (%s)." % (p, e.strerror or e))
    return p


def save_folder_setting(config_path, folder):
    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)
    raw["backup_folder"] = str(folder)
    tmp = Path(str(config_path) + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    tmp.replace(config_path)


def read_manifest(folder):
    try:
        return json.loads((Path(folder) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None


def write_manifest(folder, man):
    tmp = Path(folder) / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(man))
    tmp.replace(Path(folder) / MANIFEST)


def list_backups(cfg):
    root = backup_root(cfg)
    out = []
    if not root.is_dir():
        return out
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        man = read_manifest(d)
        if not man:
            continue
        out.append({"name": d.name, "path": str(d), "complete": bool(man.get("complete")),
                    "files": len(man.get("files", {})), "bytes": sum(f["size"] or 0 for f in man.get("files", {}).values()),
                    "failed": len(man.get("failed", [])), "started": man.get("started"), "finished": man.get("finished"),
                    "linked": man.get("linked", 0), "folders": man.get("folders", ["/"]),
                    "partial": bool(man.get("partial")), "label": man.get("label") or "",
                    "compressed": bool(man.get("compressed")), "verified": man.get("verified"),
                    "mirror": man.get("mirror")})
    return out


def unfinished(cfg):
    """The newest backup, if it never finished. Only the newest one is carried on."""
    backups = [b for b in list_backups(cfg) if not b["partial"]]
    return backups[0] if backups and not backups[0]["complete"] else None


class Job:
    """One backup running in the background. The page asks for .status() every second or two."""

    def __init__(self, cfg, items=None, label=""):
        self.cfg = cfg
        self.items = items     # None = everything; else [{"path": ..., "kind": "file"|"dir"}] picked by the user
        self.label = (label or "").strip()[:120]
        self.stop_flag = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.st = {"phase": "starting", "files_total": 0, "files_done": 0, "bytes_total": 0, "bytes_done": 0,
                   "current": "", "failed": 0, "linked": 0, "message": "", "folder": "", "started": time.time(),
                   "resumed": False}

    def set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def status(self):
        with self.lock:
            return dict(self.st, running=self.thread is not None and self.thread.is_alive())

    def start(self):
        self.thread = threading.Thread(target=self._run_safe, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def _run_safe(self):
        try:
            run(self.cfg, self, self.items, self.label)
        except BackupError as e:
            self.set(phase="failed", message=str(e))
        except SystemExit as e:
            self.set(phase="failed", message=str(e.code))
        except Exception as e:  # keep the app alive and say what happened
            self.set(phase="failed", message="%s: %s" % (type(e).__name__, e))


def run(cfg, job, items=None, label=""):
    root = backup_root(cfg)
    check_folder(root.parent)
    root.mkdir(parents=True, exist_ok=True)
    folders = [i["path"] for i in items] if items else (cfg.get("backup_folders") or ["/"])
    skip = [] if items else (cfg.get("backup_exclude") or [])

    # Carry on an unfinished backup, or start a new one. Backups of picked items always start fresh.
    left = None if items else unfinished(cfg)
    if left:
        dest = Path(left["path"])
        man = read_manifest(dest)
        job.set(resumed=True)
    else:
        dest = core.unique_dir(root, core.now_stamp())
        man = {"device": cfg["device_name"], "host": cfg["host"], "folders": folders, "started": core.now_stamp(),
               "finished": None, "complete": False, "files": {}, "failed": [], "partial": bool(items),
               "label": label}
        write_manifest(dest, man)
    job.set(folder=str(dest))

    # Earlier complete backups (newest first), so unchanged files can be linked instead of copied.
    earlier = {}    # device path -> (backup folder, its manifest entry)
    for b in list_backups(cfg)[:30]:
        if not b["complete"] or b["path"] == str(dest) or b["compressed"]:
            continue
        for path, entry in (read_manifest(b["path"]) or {}).get("files", {}).items():
            earlier.setdefault(path, (Path(b["path"]), entry))
        if not b["partial"]:
            break      # a full backup has everything older ones had
    prev_files = {p: e for p, (d, e) in earlier.items()}

    dev = core.GD1(cfg)
    dev.connect(quiet=True)
    try:
        # 1. List everything first, so we know how big the job is.
        job.set(phase="listing", message="Listing every folder on %s..." % cfg["device_name"])
        todo, seen = [], set()
        for folder in folders:
            kind = next((i.get("kind") for i in items or [] if i["path"] == folder), "dir")
            for path, size, modified in (one_file(dev, folder) if kind == "file" else dev.walk(folder)):
                if job.stop_flag.is_set():
                    break
                if path in seen or any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(PurePosixPath(path).name, p)
                                       for p in skip):
                    continue
                seen.add(path)
                todo.append((path, size, modified))
                if len(todo) % 200 == 0:
                    job.set(files_total=len(todo), current=path)
        total = sum(s or 0 for _, s, _ in todo)
        job.set(files_total=len(todo), bytes_total=total)

        # 2. Is there room? Only files that changed need new space.
        need = sum(s or 0 for p, s, m in todo if not same(prev_files.get(p), s, m) and p not in man["files"])
        free = shutil.disk_usage(dest).free
        if need > free:
            raise BackupError("Not enough room: the backup needs about %s more, but the drive has %s free."
                              % (core.human_size(need), core.human_size(free)))

        # 3. Copy.
        job.set(phase="copying", message="")
        fails_in_a_row = 0
        failed = {f["path"]: f for f in man.get("failed", [])}
        last_save = time.time()
        for path, size, modified in todo:
            if job.stop_flag.is_set():
                break
            local = core.local_path_for(dest, path)
            done = man["files"].get(path)
            if done and local.exists() and local.stat().st_size == (size if size is not None else done["size"]):
                job.set(files_done=job.st["files_done"] + 1, bytes_done=job.st["bytes_done"] + (size or 0))
                continue
            job.set(current=path)
            local.parent.mkdir(parents=True, exist_ok=True)
            try:
                old = prev_files.get(path)
                digest = None
                if old and same(old, size, modified) and link(core.local_path_for(earlier[path][0], path), local):
                    job.set(linked=job.st["linked"] + 1, bytes_done=job.st["bytes_done"] + (size or 0))
                    man["linked"] = man.get("linked", 0) + 1
                    digest = old.get("sha256")
                else:
                    digest = download(dev, path, local, job)   # counts its own bytes as they arrive
                man["files"][path] = {"size": local.stat().st_size, "modified": modified,
                                      "sha256": digest or sha256_of(local)}
                failed.pop(path, None)
                fails_in_a_row = 0
                job.set(files_done=job.st["files_done"] + 1)
            except (ftplib.Error, OSError, EOFError) as e:
                if job.stop_flag.is_set():
                    break   # you pressed Stop in the middle of this file; it's copied again next time
                failed[path] = {"path": path, "error": str(e)[:200]}
                fails_in_a_row += 1
                job.set(failed=len(failed), files_done=job.st["files_done"] + 1)
                if fails_in_a_row >= MAX_FAILS_IN_A_ROW:
                    man["failed"] = list(failed.values())
                    write_manifest(dest, man)
                    raise BackupError("Lost contact with %s (%d files in a row couldn't be read). Check it's on, "
                                      "then press Resume to carry on." % (cfg["device_name"], fails_in_a_row))
                try:
                    dev.close()
                    dev.connect(quiet=True)
                except SystemExit:
                    pass
            if time.time() - last_save > 5:
                man["failed"] = list(failed.values())
                write_manifest(dest, man)
                last_save = time.time()
        man["failed"] = list(failed.values())
        if job.stop_flag.is_set():
            write_manifest(dest, man)
            job.set(phase="stopped", message="Stopped. Press Resume to carry on where it left off.")
            return
        man["complete"], man["finished"] = True, core.now_stamp()
        write_manifest(dest, man)
        notes = after_backup(cfg, dest, man, job)
        msg = "Backup %sfinished: %d files, %s, in %s." % ("of the picked items " if items else "", len(man["files"]),
                                                            core.human_size(total), dest)
        if failed:
            msg += " %d file%s couldn't be read (listed in %s)." % (len(failed), "" if len(failed) == 1 else "s", MANIFEST)
        job.set(phase="done", message=msg + "".join(" " + n for n in notes), current="")
    finally:
        dev.close()


def one_file(dev, path):
    """(path, size, modified) for a single file, read from its folder's listing."""
    folder, name = str(PurePosixPath(path).parent), PurePosixPath(path).name
    try:
        for n, kind, size, modified in dev._list(folder):
            if n == name and kind == "file":
                return [(path, size, modified)]
    except ftplib.error_perm:
        pass
    return [(path, None, None)]


def same(old, size, modified):
    """Unchanged since the last backup? Only trusted when the device gives exact times (to the second)."""
    exact = bool(modified) and len(modified) >= 14 and modified[:14].isdigit()
    return bool(old) and exact and old.get("size") == size and old.get("modified") == modified


def link(src, dst):
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
        return True
    except OSError:
        return False


def download(dev, remote, local, job):
    """Copy one file, counting bytes as they arrive so big files show progress. Returns its sha256."""
    tmp = local.with_name(local.name + ".downloading")
    start = job.st["bytes_done"]
    h = hashlib.sha256()
    try:
        with open(tmp, "wb") as f:
            def write(chunk):
                if job.stop_flag.is_set():
                    raise InterruptedError("stopped")
                f.write(chunk)
                h.update(chunk)
                job.set(bytes_done=job.st["bytes_done"] + len(chunk))
            dev.call("retrbinary", "RETR " + remote, write)
    except BaseException:
        tmp.unlink(missing_ok=True)
        job.set(bytes_done=start)
        raise
    tmp.replace(local)
    return h.hexdigest()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------- after a backup: compress, second copy, keep last N

ZIP_NAME = "files.zip"


def after_backup(cfg, dest, man, job=None):
    notes = []
    if cfg.get("backup_compress"):
        if job:
            job.set(phase="compressing", message="Compressing the backup...")
        compress(dest, man)
        notes.append("Compressed to %s." % core.human_size((dest / ZIP_NAME).stat().st_size))
    mirror = (cfg.get("backup_mirror") or "").strip()
    if mirror:
        if job:
            job.set(phase="mirroring", message="Saving the second copy to %s..." % mirror)
        try:
            target = check_folder(mirror) / cfg["device_name"] / dest.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(dest, target)
            man["mirror"] = str(target)
            write_manifest(dest, man)
            notes.append("Second copy saved to %s." % target)
        except (BackupError, OSError) as e:
            notes.append("The second copy failed: %s." % e)
    keep = int(cfg.get("backup_keep") or 0)
    if keep > 0:
        gone = prune(cfg, keep)
        if gone:
            notes.append("Removed %d older backup%s (keeping the newest %d)." % (len(gone), "" if len(gone) == 1 else "s",
                                                                                 keep))
    return notes


def compress(dest, man):
    """Pack a finished backup's files into files.zip and remove the loose copies."""
    tmp = dest / (ZIP_NAME + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for path in man["files"]:
            local = core.local_path_for(dest, path)
            if local.exists():
                z.write(local, path.lstrip("/"))
    tmp.replace(dest / ZIP_NAME)
    for child in list(dest.iterdir()):
        if child.name in (MANIFEST, ZIP_NAME):
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    man["compressed"] = True
    write_manifest(dest, man)


def prune(cfg, keep):
    """Keep the newest `keep` complete backups of each kind (full, and picked items); delete older ones."""
    gone = []
    for partial in (False, True):
        done = [b for b in list_backups(cfg) if b["complete"] and b["partial"] == partial]
        for b in done[keep:]:
            shutil.rmtree(b["path"], ignore_errors=True)
            if b.get("mirror") and Path(b["mirror"]).name == b["name"]:
                shutil.rmtree(b["mirror"], ignore_errors=True)   # its second copy too
            gone.append(b["name"])
    return gone


def find(cfg, name):
    if not re.match(r"^[\w.-]{1,80}$", name or ""):
        raise BackupError("No such backup.")
    p = backup_root(cfg) / name
    if not (p / MANIFEST).exists():
        raise BackupError("That backup isn't there any more.")
    return p


def set_label(cfg, name, label):
    p = find(cfg, name)
    man = read_manifest(p)
    man["label"] = (label or "").strip()[:120]
    write_manifest(p, man)


def browse(cfg, name, folder="/"):
    """What's inside a backup at one folder (from its list of files, so it works for compressed ones too)."""
    p = find(cfg, name)
    man = read_manifest(p)
    folder = "/" + (folder or "/").strip("/")
    prefix = folder.rstrip("/") + "/"
    dirs, files = {}, []
    for path, f in man.get("files", {}).items():
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if "/" in rest:
            top = rest.split("/", 1)[0]
            d = dirs.setdefault(top, {"name": top, "kind": "dir", "files": 0, "size": 0})
            d["files"] += 1
            d["size"] += f.get("size") or 0
        else:
            files.append({"name": rest, "kind": "file", "size": f.get("size"), "path": path})
    for d in dirs.values():
        d["path"] = prefix + d["name"]
    return {"backup": name, "folder": folder, "label": man.get("label") or "", "compressed": bool(man.get("compressed")),
            "entries": sorted(dirs.values(), key=lambda x: x["name"].lower()) + sorted(files, key=lambda x: x["name"].lower())}


def extract(cfg, name, paths):
    """Local copies of these backed-up paths (files or folders), ready to put back. Returns {device path: local path}."""
    p = find(cfg, name)
    man = read_manifest(p)
    wanted = ["/" + x.strip("/") for x in paths]
    pick = [f for f in man.get("files", {}) if any(f == w or f.startswith(w.rstrip("/") + "/") or w == "/" for w in wanted)]
    if not pick:
        raise BackupError("Those files aren't in this backup.")
    if not man.get("compressed"):
        return {f: core.local_path_for(p, f) for f in pick}, p
    out = core.unique_dir(core.data_dir(cfg) / "restore-unpacked", core.now_stamp())
    with zipfile.ZipFile(p / ZIP_NAME) as z:
        for f in pick:
            target = core.local_path_for(out, f)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(f.lstrip("/")) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return {f: core.local_path_for(out, f) for f in pick}, out


class VerifyJob(Job):
    """Re-read every file in a backup and compare it with the checksum taken when it was made."""

    def __init__(self, cfg, name):
        super().__init__(cfg)
        self.name = name

    def _run_safe(self):
        try:
            self.verify()
        except BackupError as e:
            self.set(phase="failed", message=str(e))
        except Exception as e:
            self.set(phase="failed", message="%s: %s" % (type(e).__name__, e))

    def verify(self):
        p = find(self.cfg, self.name)
        man = read_manifest(p)
        files = man.get("files", {})
        self.set(phase="verifying", files_total=len(files), bytes_total=sum(f.get("size") or 0 for f in files.values()),
                 message="Checking %s..." % self.name, folder=str(p))
        bad, missing, unchecked = [], [], 0
        z = zipfile.ZipFile(p / ZIP_NAME) if man.get("compressed") else None
        try:
            for path, f in files.items():
                if self.stop_flag.is_set():
                    self.set(phase="stopped", message="Stopped checking.")
                    return
                self.set(current=path)
                h = hashlib.sha256()
                try:
                    src = z.open(path.lstrip("/")) if z else open(core.local_path_for(p, path), "rb")
                    with src:
                        for chunk in iter(lambda: src.read(1 << 20), b""):
                            h.update(chunk)
                            self.set(bytes_done=self.st["bytes_done"] + len(chunk))
                except (OSError, KeyError):
                    missing.append(path)
                    continue
                finally:
                    self.set(files_done=self.st["files_done"] + 1)
                if not f.get("sha256"):
                    unchecked += 1
                elif h.hexdigest() != f["sha256"]:
                    bad.append(path)
        finally:
            if z:
                z.close()
        ok = not bad and not missing
        man["verified"] = {"when": core.now_stamp(), "ok": ok, "bad": bad[:200], "missing": missing[:200]}
        write_manifest(p, man)
        msg = ("All %d files in %s are intact." % (len(files), self.name) if ok else
               "%s: %d damaged and %d missing file%s. First: %s" % (self.name, len(bad), len(missing),
                                                                   "" if len(bad) + len(missing) == 1 else "s",
                                                                   (bad + missing)[0]))
        if unchecked:
            msg += " (%d older files had no checksum saved, so they were only read.)" % unchecked
        self.set(phase="done" if ok else "failed", message=msg, current="")


# ----------------------------------------------------------------- from Terminal

def cmd_backup(cfg, args):
    if getattr(args, "to", None):
        cfg["backup_folder"] = str(check_folder(args.to))
    job = Job(cfg)
    core.say("Backing up everything on %s to %s" % (cfg["device_name"], backup_root(cfg)))
    core.say("Press Ctrl+C to stop. Running it again carries on where it left off.")
    job.start()
    try:
        while job.thread.is_alive():
            job.thread.join(2)
            s = job.status()
            if s["phase"] == "listing":
                line = "Listing... %d files found so far" % s["files_total"]
            else:
                line = "%d of %d files, %s of %s" % (s["files_done"], s["files_total"], core.human_size(s["bytes_done"]),
                                                    core.human_size(s["bytes_total"]))
            print("\r" + line.ljust(70), end="", flush=True)
    except KeyboardInterrupt:
        job.stop()
        job.thread.join()
    print()
    core.say(job.status()["message"])
