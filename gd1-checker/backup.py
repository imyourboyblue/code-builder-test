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
import json
import os
import shutil
import threading
import time
from pathlib import Path, PurePosixPath

import gd1check as core

MANIFEST = "_backup.json"
MAX_FAILS_IN_A_ROW = 20
DEFAULT_FOLDER = "~/Device-backups"


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
    test = p / ".device-agent-write-test"
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
                    "linked": man.get("linked", 0), "folders": man.get("folders", ["/"])})
    return out


def unfinished(cfg):
    """The newest backup, if it never finished. Only the newest one is carried on."""
    backups = list_backups(cfg)
    return backups[0] if backups and not backups[0]["complete"] else None


class Job:
    """One backup running in the background. The page asks for .status() every second or two."""

    def __init__(self, cfg):
        self.cfg = cfg
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
            run(self.cfg, self)
        except BackupError as e:
            self.set(phase="failed", message=str(e))
        except SystemExit as e:
            self.set(phase="failed", message=str(e.code))
        except Exception as e:  # keep the app alive and say what happened
            self.set(phase="failed", message="%s: %s" % (type(e).__name__, e))


def run(cfg, job):
    root = backup_root(cfg)
    check_folder(root.parent)
    root.mkdir(parents=True, exist_ok=True)
    folders = cfg.get("backup_folders") or ["/"]
    skip = cfg.get("backup_exclude") or []

    # Carry on an unfinished backup, or start a new one.
    left = unfinished(cfg)
    if left:
        dest = Path(left["path"])
        man = read_manifest(dest)
        job.set(resumed=True)
    else:
        dest = core.unique_dir(root, core.now_stamp())
        man = {"device": cfg["device_name"], "host": cfg["host"], "folders": folders, "started": core.now_stamp(),
               "finished": None, "complete": False, "files": {}, "failed": []}
        write_manifest(dest, man)
    job.set(folder=str(dest))

    # The newest complete backup, so unchanged files can be linked instead of copied.
    prev_dir, prev_files = None, {}
    for b in list_backups(cfg):
        if b["complete"] and b["path"] != str(dest):
            prev_dir = Path(b["path"])
            prev_files = (read_manifest(prev_dir) or {}).get("files", {})
            break

    dev = core.GD1(cfg)
    dev.connect(quiet=True)
    try:
        # 1. List everything first, so we know how big the job is.
        job.set(phase="listing", message="Listing every folder on %s..." % cfg["device_name"])
        todo, seen = [], set()
        for folder in folders:
            for path, size, modified in dev.walk(folder):
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
                if prev_dir and same(old, size, modified) and link(core.local_path_for(prev_dir, path), local):
                    job.set(linked=job.st["linked"] + 1, bytes_done=job.st["bytes_done"] + (size or 0))
                    man["linked"] = man.get("linked", 0) + 1
                else:
                    download(dev, path, local, job)   # counts its own bytes as they arrive
                man["files"][path] = {"size": local.stat().st_size, "modified": modified}
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
        msg = "Backup finished: %d files, %s, in %s." % (len(man["files"]), core.human_size(total), dest)
        if failed:
            msg += " %d file%s couldn't be read (listed in %s)." % (len(failed), "" if len(failed) == 1 else "s", MANIFEST)
        job.set(phase="done", message=msg, current="")
    finally:
        dev.close()


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
    """Copy one file, counting bytes as they arrive so big files show progress."""
    tmp = local.with_name(local.name + ".downloading")
    start = job.st["bytes_done"]
    try:
        with open(tmp, "wb") as f:
            def write(chunk):
                if job.stop_flag.is_set():
                    raise InterruptedError("stopped")
                f.write(chunk)
                job.set(bytes_done=job.st["bytes_done"] + len(chunk))
            dev.call("retrbinary", "RETR " + remote, write)
    except BaseException:
        tmp.unlink(missing_ok=True)
        job.set(bytes_done=start)
        raise
    tmp.replace(local)


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
