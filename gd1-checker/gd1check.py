#!/usr/bin/env python3
"""
Patchbay checker (gd1check): finds files damaged by a crash during a save, over FTP.

What it does
  test     Check that the Mac can reach GD1's FTP server.
  browse   Look through GD1's folders, preview files, and pick which folders to scan.
  scan     Copy GD1's files to a dated snapshot on the Mac (read-only on GD1),
           check every file for crash damage, and write a report.
  explain  Ask the AI on your Mac (Ollama) to explain the last report in plain words.
  restore  Put a good copy of one file back on GD1. Backs up the current file
           first and asks you to type the device name before writing.
  undo     Put back the file that a restore replaced.
  history  List restores and undos.

Only uses Python's built-in parts, so there is nothing to install.
Settings live in config.json next to this file.
"""

import argparse
import configparser
import datetime as dt
import fnmatch
import ftplib
import gzip
import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nro  # noqa: E402  (.nro program files)
DEFAULT_CONFIG = HERE / "config.json"

TEXT_EXTS = {
    ".txt", ".cfg", ".conf", ".ini", ".json", ".xml", ".plist", ".yaml", ".yml",
    ".toml", ".csv", ".log", ".sh", ".py", ".lua", ".js", ".c", ".h", ".cpp",
    ".hpp", ".md", ".properties", ".rc",
}
LEFTOVER_PATTERNS = ["*.tmp", "*.temp", "*.part", "*.swp", "*~", "*.bak~",
                     "*-journal", "*-wal", "*.lock", "*.new"]
MAGIC = {
    ".png": b"\x89PNG\r\n\x1a\n",
    ".gz": b"\x1f\x8b",
    ".zip": b"PK",
    ".bz2": b"BZh",
    ".xz": b"\xfd7zXZ\x00",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".db": b"SQLite format 3\x00",
    ".sqlite": b"SQLite format 3\x00",
    ".sqlite3": b"SQLite format 3\x00",
}
SQLITE_MAGIC = b"SQLite format 3\x00"


# ----------------------------------------------------------------- helpers

def say(msg=""):
    print(msg, flush=True)


def now_stamp():
    return dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def unique_dir(parent, stamp):
    """A new folder that never reuses an existing one, even within the same second."""
    d, n = Path(parent) / stamp, 1
    while d.exists():
        n += 1
        d = Path(parent) / ("%s_%d" % (stamp, n))
    d.mkdir(parents=True)
    return d


# Settings shared by every device. Files in devices/ take these from config.json.
GLOBAL_KEYS = ["data_folder", "ollama_url", "ollama_model", "chat_context", "lan_access", "port", "auto_mac_steps"]
DEVICES_DIR = HERE / "devices"


def load_config(path):
    path = Path(path)
    if not path.exists():
        sys.exit("Can't find %s. Copy config.example.json to config.json and fill in GD1's address." % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    main = path.parent.parent / "config.json"
    if path.parent.name == "devices" and main.exists():
        try:
            with open(main, encoding="utf-8") as f:
                shared = json.load(f)
            for k in GLOBAL_KEYS:
                if k in shared:
                    cfg.setdefault(k, shared[k])
        except ValueError:
            pass
    cfg.setdefault("device_name", "GD1")
    cfg.setdefault("ports", [21, 5000])
    cfg.setdefault("username", "anonymous")
    cfg.setdefault("password", "anonymous@")
    cfg.setdefault("passive_mode", True)
    cfg.setdefault("timeout_seconds", 20)
    cfg.setdefault("remote_folders", ["/"])
    cfg.setdefault("include", ["*"])
    cfg.setdefault("exclude", [])
    cfg.setdefault("max_file_mb", 200)
    cfg.setdefault("max_files", 5000)
    cfg.setdefault("data_folder", str(HERE / "gd1-data"))
    cfg.setdefault("ollama_url", "http://localhost:11434")
    cfg.setdefault("ollama_model", "qwen3.6:35b-a3b")
    cfg.setdefault("allow_writes", False)
    if "host" not in cfg or not cfg["host"]:
        sys.exit("config.json needs GD1's IP address under \"host\".")
    return cfg


def device_config_files(main=DEFAULT_CONFIG):
    """config.json first, then one file per extra device in the devices folder."""
    files = [Path(main)] if Path(main).exists() else []
    d = Path(main).parent / "devices"
    if d.is_dir():
        files += sorted(d.glob("*.json"))
    return files


def config_for_device(name, main=DEFAULT_CONFIG):
    for f in device_config_files(main):
        try:
            with open(f, encoding="utf-8") as fh:
                if (json.load(fh).get("device_name") or "GD1").lower() == name.lower():
                    return f
        except ValueError:
            continue
    sys.exit("No device called %s. Devices are listed in config.json and the devices folder." % name)


def data_dir(cfg):
    d = Path(os.path.expanduser(cfg["data_folder"])) / cfg["device_name"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_path_for(root, remote_path):
    rel = PurePosixPath(remote_path).relative_to("/") if remote_path.startswith("/") else PurePosixPath(remote_path)
    parts = [p for p in rel.parts if p not in ("..", ".")]
    return Path(root).joinpath(*parts) if parts else Path(root)


# ----------------------------------------------------------------- FTP

class GD1:
    """A small wrapper around ftplib that reconnects if GD1 drops the line."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.ftp = None
        self.port = None

    def connect(self, quiet=False):
        last_err = None
        for port in self.cfg["ports"]:
            for attempt in range(3):
                try:
                    ftp = ftplib.FTP(timeout=self.cfg["timeout_seconds"])
                    ftp.encoding = "utf-8"
                    ftp.connect(self.cfg["host"], int(port))
                    ftp.login(self.cfg["username"], self.cfg["password"])
                    ftp.set_pasv(bool(self.cfg["passive_mode"]))
                    self.ftp, self.port = ftp, int(port)
                    if not quiet:
                        say("Connected to %s at %s port %s." % (self.cfg["device_name"], self.cfg["host"], port))
                    return ftp
                except (OSError, EOFError, ftplib.Error) as e:
                    last_err = e
                    if isinstance(e, ConnectionRefusedError):
                        break  # nothing listening on this port, try the next one
                    time.sleep(1.5 * (attempt + 1))
        raise SystemExit("Couldn't connect to %s at %s on ports %s. Last error: %s"
                         % (self.cfg["device_name"], self.cfg["host"], self.cfg["ports"], last_err))

    def call(self, fn_name, *args, **kwargs):
        """Run an FTP command, reconnecting once if the connection dropped."""
        for attempt in range(2):
            if self.ftp is None:
                self.connect(quiet=True)
            try:
                return getattr(self.ftp, fn_name)(*args, **kwargs)
            except ftplib.error_temp as e:
                if not str(e).startswith("421"):
                    # 450/451/452 mean "that file isn't there / can't be used right now", not a dropped line.
                    # Some servers (like the Switch's ftpd) say 450 for a missing file, so treat it like 550.
                    raise ftplib.error_perm(str(e))
                self.close()
                if attempt == 1:
                    raise
                say("  %s closed the connection (%s), reconnecting..." % (self.cfg["device_name"], e))
                time.sleep(2)
            except (OSError, EOFError) as e:
                self.close()
                if attempt == 1:
                    raise
                say("  connection dropped (%s), reconnecting..." % e)
                time.sleep(2)

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
        self.ftp = None

    # ---- listing

    def walk(self, folder):
        """Yield (path, size, modified) for every file under folder."""
        stack = [folder]
        seen = set()
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            try:
                entries = self._list(d)
            except ftplib.error_perm as e:
                say("  can't list %s (%s), skipping" % (d, e))
                continue
            for name, kind, size, modified in entries:
                if name in (".", ".."):
                    continue
                full = str(PurePosixPath(d) / name)
                if kind == "dir":
                    stack.append(full)
                elif kind == "file":
                    yield full, size, modified

    def _list(self, d):
        try:
            out = []
            for name, facts in self.call("mlsd", d, ["type", "size", "modify"]):
                t = facts.get("type", "file")
                kind = "dir" if t == "dir" else ("file" if t == "file" else "skip")
                size = int(facts["size"]) if facts.get("size", "").isdigit() else None
                out.append((name, kind, size, facts.get("modify")))
            return out
        except ftplib.error_perm as e:
            if not str(e).startswith(("500", "501", "502", "504")):
                raise
        return self._list_unix(d)

    _LS = re.compile(r"^([\-dlcbps])[rwxsStT\-]{9}\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3}\s+\d+\s+[\d:]+)\s+(.+)$")

    def _list_unix(self, d):
        """Fallback for servers without MLSD: read 'ls -l' style lines."""
        lines = []
        self.call("retrlines", "LIST " + d, lines.append)
        out = []
        for line in lines:
            m = self._LS.match(line)
            if not m:
                continue
            t, size, when, name = m.groups()
            if t == "l":
                name = name.split(" -> ")[0]
                kind = "skip"  # don't follow links; they can loop
            else:
                kind = "dir" if t == "d" else "file"
            out.append((name, kind, int(size), when))
        return out

    # ---- transfer

    def download(self, remote, local, progress=None):
        """progress(n) is called for each piece that arrives (it may pause, or raise to cancel)."""
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".downloading")
        try:
            with open(tmp, "wb") as f:
                def write(chunk):
                    if progress:
                        progress(len(chunk))
                    f.write(chunk)
                self.call("retrbinary", "RETR " + remote, write)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(local)

    def size(self, remote):
        try:
            self.call("voidcmd", "TYPE I")
            return self.call("size", remote)
        except ftplib.Error:
            return None

    def make_dirs(self, folder):
        """Create a folder and any missing parent folders (existing ones are fine)."""
        path = PurePosixPath(folder)
        for i in range(2, len(path.parts) + 1):
            try:
                self.call("mkd", str(PurePosixPath(*path.parts[:i])))
            except ftplib.error_perm:
                pass

    def upload(self, data, remote, progress=None):
        """Upload bytes (or an open file) to a temporary name, then swap it in, so a
        dropped upload never leaves a half-written file in place of the real one."""
        tmp_remote = remote + ".gd1check-upload"
        src = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
        if progress:
            src = _Counted(src, progress)
        try:
            try:
                self.call("storbinary", "STOR " + tmp_remote, src)
            except ftplib.error_perm:
                # Maybe the folder doesn't exist (e.g. undoing a deleted folder). Make it and retry once.
                self.make_dirs(str(PurePosixPath(remote).parent))
                src.seek(0)
                self.call("storbinary", "STOR " + tmp_remote, src)
        except BaseException:
            try:     # don't leave the half-sent temporary file behind
                self.close()
                self.connect(quiet=True)
                self.ftp.delete(tmp_remote)
            except (Exception, SystemExit):
                pass
            raise
        try:
            self.call("rename", tmp_remote, remote)
        except ftplib.error_perm:
            # Some servers won't rename over an existing file.
            self.call("delete", remote)
            self.call("rename", tmp_remote, remote)


class _Counted:
    """Wraps a file being uploaded so each piece read is reported (to show progress, pause or cancel)."""

    def __init__(self, f, progress):
        self.f, self.progress = f, progress

    def read(self, n=-1):
        data = self.f.read(n)
        if data:
            self.progress(len(data))
        return data

    def seek(self, *a):
        return self.f.seek(*a)


# ----------------------------------------------------------------- checks

def problem(level, text):
    return {"level": level, "text": text}


def check_bytes(name, data, remote_size=None):
    """Look for the kinds of damage a crash during a save leaves behind.
    Returns a list of problems (empty list = looks fine)."""
    probs = []
    ext = Path(name).suffix.lower()
    n = len(data)

    if remote_size is not None and remote_size != n:
        probs.append(problem("error", "Copy is %d bytes but GD1 says %d. The transfer may have been cut." % (n, remote_size)))

    if n == 0:
        return probs + [problem("error", "File is empty (0 bytes). A save was likely interrupted before any data was written.")]

    stripped = data.rstrip(b"\x00")
    tail_nulls = n - len(stripped)
    if not stripped:
        return probs + [problem("error", "File is all zero bytes. The space was reserved but the data never got written.")]

    is_text = ext in TEXT_EXTS or looks_like_text(data[:4096])

    if is_text:
        if tail_nulls:
            probs.append(problem("error", "Text file ends with %d zero bytes (null padding). The save stopped part way." % tail_nulls))
        inner = stripped.count(b"\x00")
        if inner:
            probs.append(problem("error", "Text file has %d zero bytes in the middle. Part of it was never written." % inner))
        probs += check_text_format(ext, stripped)
    else:
        if tail_nulls >= 4096 or (tail_nulls >= 64 and tail_nulls * 10 >= n):
            probs.append(problem("warning", "Ends with %d zero bytes (%.0f%% of the file). This often means an interrupted write." % (tail_nulls, 100.0 * tail_nulls / n)))
        big = longest_null_run(stripped)
        if big >= 65536:
            probs.append(problem("warning", "Has a block of %d zero bytes inside it. Part of the file may be missing." % big))

    expected = MAGIC.get(ext)
    if expected and not data.startswith(expected):
        probs.append(problem("error", "Header is wrong for a %s file (starts with %s). The start of the file is damaged." % (ext, data[:8].hex(" "))))

    if ext == ".nro" or nro.looks_like_nro(data):
        probs += nro.inspect(data)[1]
    elif data.startswith(SQLITE_MAGIC):
        probs += check_sqlite(data)
    elif ext == ".gz" and data.startswith(MAGIC[".gz"]):
        try:
            gzip.decompress(data)
        except (OSError, EOFError) as e:
            probs.append(problem("error", "Compressed file is cut short or damaged (%s)." % e))
    elif ext == ".zip" and data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                bad = z.testzip()
            if bad:
                probs.append(problem("error", "Zip file has a damaged entry: %s" % bad))
        except zipfile.BadZipFile as e:
            probs.append(problem("error", "Zip file is cut short or damaged (%s)." % e))
    return probs


def looks_like_text(sample):
    if not sample:
        return False
    if sample.startswith(SQLITE_MAGIC):
        return False
    body = sample.rstrip(b"\x00")
    if not body:
        return False
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for b in body if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    return printable / len(body) > 0.95


def longest_null_run(data):
    best = 0
    for m in re.finditer(rb"\x00{4096,}", data):
        best = max(best, m.end() - m.start())
    return best


def check_text_format(ext, data):
    probs = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return [problem("warning", "Not valid UTF-8 text near byte %d. It may be damaged or use another encoding." % e.start)]
    if ext == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as e:
            near_end = e.pos >= len(text.rstrip()) - 2
            probs.append(problem("error", ("JSON ends early (cut off at line %d). The save stopped part way." if near_end
                                           else "JSON is broken at line %d.") % e.lineno))
    elif ext in (".xml", ".plist"):
        try:
            ET.fromstring(text)
        except ET.ParseError as e:
            probs.append(problem("error", "XML is broken or cut off (%s)." % e))
    elif ext == ".ini":
        try:
            configparser.ConfigParser(strict=False, interpolation=None).read_string(text)
        except configparser.Error as e:
            probs.append(problem("warning", "INI file doesn't parse: %s" % str(e).splitlines()[0]))
    if text and not text.endswith(("\n", "\r")) and ext in (".conf", ".cfg", ".ini", ".csv", ".log"):
        last = text.splitlines()[-1] if text.splitlines() else ""
        probs.append(problem("info", "Last line has no line ending (\"%s\"). It may have been cut off." % last[-60:]))
    return probs


def check_sqlite(data):
    probs = []
    if len(data) < 100:
        return [problem("error", "Database header is cut short (%d bytes)." % len(data))]
    page_size = int.from_bytes(data[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size & (page_size - 1):
        probs.append(problem("error", "Database header has an impossible page size (%d). The header is damaged." % page_size))
        return probs
    pages_in_header = int.from_bytes(data[28:32], "big")
    if len(data) % page_size:
        probs.append(problem("error", "Database size (%d) isn't a whole number of %d-byte pages. The last write was cut off." % (len(data), page_size)))
    if pages_in_header and pages_in_header * page_size > len(data):
        probs.append(problem("error", "Database says it has %d pages but only %d are present. It was truncated."
                             % (pages_in_header, len(data) // page_size)))
    # Full check on a temporary copy.
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / ("gd1check-%d.sqlite" % os.getpid())
    try:
        tmp.write_bytes(data)
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % tmp, uri=True)
        try:
            rows = con.execute("PRAGMA integrity_check(20)").fetchall()
        finally:
            con.close()
        msgs = [r[0] for r in rows if r[0] != "ok"]
        if msgs:
            probs.append(problem("error", "Database integrity check failed: " + "; ".join(msgs[:5])))
    except sqlite3.DatabaseError as e:
        probs.append(problem("error", "Database won't open (%s)." % e))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return probs


def leftover_notes(paths):
    """Temporary or journal files next to real files suggest a save was interrupted."""
    notes = {}
    for p in paths:
        name = PurePosixPath(p).name
        for pat in LEFTOVER_PATTERNS:
            if fnmatch.fnmatch(name, pat):
                notes[p] = problem("info", "Leftover temporary/journal file (%s). Often left behind when a save is interrupted." % pat)
                break
    return notes


# ----------------------------------------------------------------- commands

def cmd_test(cfg, args):
    dev = GD1(cfg)
    ftp = dev.connect()
    say("Server says: %s" % (ftp.getwelcome() or "").strip())
    try:
        say("Current folder: %s" % ftp.pwd())
    except ftplib.Error:
        pass
    for folder in cfg["remote_folders"]:
        try:
            entries = dev._list(folder)
            say("%s holds %d items." % (folder, len(entries)))
        except ftplib.Error as e:
            say("Can't list %s: %s" % (folder, e))
    dev.close()
    say("Connection works.")


def wanted(path, cfg):
    name = PurePosixPath(path).name
    if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in cfg["exclude"]):
        return False
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in cfg["include"])


def cmd_scan(cfg, args):
    root = data_dir(cfg)
    stamp = now_stamp()
    snap = unique_dir(root / "snapshots", stamp)
    stamp = snap.name
    say("Snapshot folder: %s" % snap)
    dev = GD1(cfg)
    dev.connect()

    max_bytes = int(cfg["max_file_mb"] * 1024 * 1024)
    files, skipped = [], []
    for folder in cfg["remote_folders"]:
        say("Listing %s ..." % folder)
        for path, size, modified in dev.walk(folder):
            if not wanted(path, cfg):
                continue
            if size is not None and size > max_bytes:
                skipped.append({"path": path, "size": size, "why": "bigger than max_file_mb"})
                continue
            files.append({"path": path, "size": size, "modified": modified})
            if len(files) >= cfg["max_files"]:
                say("Reached max_files (%d); stopping the listing." % cfg["max_files"])
                break
    say("Found %d files to check (%d skipped as too big)." % (len(files), len(skipped)))

    previous = latest_manifest(root, exclude=stamp)
    leftovers = leftover_notes([f["path"] for f in files])
    report = {"device": cfg["device_name"], "host": cfg["host"], "port": dev.port,
              "time": stamp, "snapshot": str(snap), "files": [], "skipped": skipped}
    manifest = {}

    for i, f in enumerate(files, 1):
        local = local_path_for(snap, f["path"])
        say("[%d/%d] %s" % (i, len(files), f["path"]))
        try:
            dev.download(f["path"], local)
        except (ftplib.Error, OSError, EOFError) as e:
            report["files"].append({"path": f["path"], "problems": [problem("error", "Couldn't download: %s" % e)]})
            continue
        data = local.read_bytes()
        probs = check_bytes(f["path"], data, f["size"])
        if f["path"] in leftovers:
            probs.append(leftovers[f["path"]])
        old = previous.get(f["path"]) if previous else None
        if old and old["size"] > 1024 and len(data) < old["size"] * 0.5:
            probs.append(problem("warning", "File shrank from %d to %d bytes since the last scan." % (old["size"], len(data))))
        sha = hashlib.sha256(data).hexdigest()
        manifest[f["path"]] = {"size": len(data), "sha256": sha, "modified": f["modified"],
                               "ok": not any(p["level"] == "error" for p in probs)}
        report["files"].append({"path": f["path"], "size": len(data), "sha256": sha, "problems": probs})
    dev.close()

    (snap / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    rpath = root / "reports" / ("%s.json" % stamp)
    rpath.parent.mkdir(exist_ok=True)
    rpath.write_text(json.dumps(report, indent=2))
    tpath = rpath.with_suffix(".txt")
    tpath.write_text(report_text(report))
    say()
    say(report_text(report))
    say("Report saved: %s" % tpath)


def latest_manifest(root, exclude=None):
    snaps = sorted(p for p in (root / "snapshots").glob("*") if p.is_dir() and p.name != exclude)
    for s in reversed(snaps):
        m = s / "_manifest.json"
        if m.exists():
            return json.loads(m.read_text())
    return None


def report_text(report):
    bad = [f for f in report["files"] if f["problems"]]
    errors = sum(1 for f in bad for p in f["problems"] if p["level"] == "error")
    lines = ["%s scan, %s: %d files checked, %d with problems (%d errors)."
             % (report["device"], report["time"], len(report["files"]), len(bad), errors)]
    order = {"error": 0, "warning": 1, "info": 2}
    for f in sorted(bad, key=lambda f: min(order[p["level"]] for p in f["problems"])):
        lines.append("")
        lines.append(f["path"])
        for p in f["problems"]:
            lines.append("  %-7s %s" % (p["level"].upper(), p["text"]))
    if report.get("skipped"):
        lines.append("")
        lines.append("Skipped %d files bigger than max_file_mb." % len(report["skipped"]))
    if not bad:
        lines.append("No crash damage found.")
    return "\n".join(lines) + "\n"


def latest_report(cfg):
    reports = sorted((data_dir(cfg) / "reports").glob("*.json"))
    if not reports:
        sys.exit("No scan yet. Run: python3 gd1check.py scan")
    return json.loads(reports[-1].read_text())


def cmd_explain(cfg, args):
    url = cfg["ollama_url"].rstrip("/")
    host = re.sub(r"^https?://", "", url).split(":")[0].split("/")[0]
    if host not in ("localhost", "127.0.0.1", "::1"):
        sys.exit("ollama_url must point at this Mac (localhost) so your files never leave it.")
    report = latest_report(cfg)
    bad = [f for f in report["files"] if any(p["level"] in ("error", "warning") for p in f["problems"])]
    if not bad:
        say("The last scan found no damage, so there's nothing to explain.")
        return
    snap = Path(report["snapshot"])
    parts = []
    for f in bad[:15]:
        local = local_path_for(snap, f["path"])
        tail = local.read_bytes()[-96:] if local.exists() else b""
        parts.append("File: %s (%s bytes)\nProblems:\n%s\nLast bytes (hex): %s"
                     % (f["path"], f.get("size"), "\n".join("- " + p["text"] for p in f["problems"]), tail.hex(" ")))
    prompt = (
        "You are helping someone who is not a programmer. A program on their device %s "
        "(an embedded FreeBSD-based system) freezes during heavy file writes, such as an auto-save or a "
        "database update. A scan of its files found the problems below.\n\n%s\n\n"
        "In plain, short language: 1) say what most likely happened to each file, 2) say which files matter most, "
        "3) say what to do next (for example restore a good copy from an earlier snapshot), and "
        "4) suggest what the program should change so a freeze can't damage files again (for example writing to "
        "a temporary file and renaming it, or using a database journal). Don't invent facts you can't see."
        % (report["device"], "\n\n".join(parts)))
    body = json.dumps({"model": cfg["ollama_model"], "stream": False,
                       "messages": [{"role": "user", "content": prompt}],
                       "options": {"num_ctx": 32768}}).encode()
    say("Asking %s on this Mac (this can take a minute)..." % cfg["ollama_model"])
    req = urllib.request.Request(url + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            answer = json.loads(r.read())["message"]["content"]
    except OSError as e:
        sys.exit("Couldn't reach Ollama at %s (%s). Is the Ollama app running?" % (url, e))
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip()
    say()
    say(answer)
    out = data_dir(cfg) / "reports" / ("%s-explained.txt" % report["time"])
    out.write_text(answer + "\n")
    say()
    say("Saved: %s" % out)


def history_path(cfg):
    return data_dir(cfg) / "history.json"


def read_history(cfg):
    p = history_path(cfg)
    return json.loads(p.read_text()) if p.exists() else []


def confirm(cfg, what):
    say()
    say(what)
    typed = input("Type %s to go ahead, or anything else to cancel: " % cfg["device_name"]).strip()
    if typed != cfg["device_name"]:
        sys.exit("Cancelled. Nothing was changed.")


def put_back(cfg, remote, source, action, note, ask=None):
    """Back up the current remote file, then upload source over it.
    ask: how to get the go-ahead (default: type the device name in Terminal).
    The chat page passes its own, because you confirm there with a button."""
    if not cfg.get("allow_writes"):
        sys.exit("Writing to %s is turned off. Set \"allow_writes\": true in config.json to allow it."
                 % cfg["device_name"])
    data = source.read_bytes()
    probs = [p for p in check_bytes(remote, data) if p["level"] == "error"]
    if probs:
        say("Warning: the copy you're putting back also has problems:")
        for p in probs:
            say("  " + p["text"])
    dev = GD1(cfg)
    dev.connect()
    stamp = now_stamp()
    backup = local_path_for(unique_dir(data_dir(cfg) / "backups", stamp), remote)
    current = dev.size(remote)
    (ask or confirm)(cfg, "%s will replace %s on %s\n  with: %s (%d bytes)\n  current file on %s: %s bytes\n"
                 "  the current file is backed up first to: %s"
            % (action.capitalize(), remote, cfg["device_name"], source, len(data), cfg["device_name"],
               current if current is not None else "unknown", backup))
    try:
        dev.download(remote, backup)
        say("Backed up the current file.")
    except ftplib.error_perm as e:
        if current is not None:
            dev.close()
            sys.exit("Backup failed (%s). Nothing was changed." % e)
        say("No current file on %s to back up (it doesn't exist)." % cfg["device_name"])
        backup = None
    dev.upload(data, remote)
    after = dev.size(remote)
    dev.close()
    ok = after is None or after == len(data)
    hist = read_history(cfg)
    entry = {"id": len(hist) + 1, "time": stamp, "action": action, "remote": remote,
             "from": str(source), "backup": str(backup) if backup else None,
             "verified": ok, "note": note}
    hist.append(entry)
    history_path(cfg).write_text(json.dumps(hist, indent=2))
    if ok:
        say("Done. %s now matches the copy you chose (change #%d). Undo with: python3 gd1check.py undo %d"
            % (remote, entry["id"], entry["id"]))
    else:
        say("Uploaded, but GD1 reports %s bytes instead of %d. Check the file, or undo with: python3 gd1check.py undo %d"
            % (after, len(data), entry["id"]))
    return entry


def cmd_restore(cfg, args):
    root = data_dir(cfg)
    remote = args.file if args.file.startswith("/") else "/" + args.file
    snaps = sorted(p for p in (root / "snapshots").glob("*") if p.is_dir())
    if args.snapshot:
        chosen = root / "snapshots" / args.snapshot
    else:
        # Newest snapshot where this file passed every check.
        chosen = None
        for s in reversed(snaps):
            m = s / "_manifest.json"
            if m.exists() and json.loads(m.read_text()).get(remote, {}).get("ok"):
                chosen = s
                break
        if chosen is None:
            sys.exit("No snapshot has a good copy of %s. Pick one with --snapshot (folders in %s)."
                     % (remote, root / "snapshots"))
    source = local_path_for(chosen, remote)
    if not source.exists():
        sys.exit("%s isn't in snapshot %s." % (remote, chosen.name))
    say("Using the copy from snapshot %s." % chosen.name)
    put_back(cfg, remote, source, "restore", "from snapshot %s" % chosen.name)


def cmd_undo(cfg, args):
    hist = read_history(cfg)
    match = [h for h in hist if h["id"] == args.change]
    if not match:
        sys.exit("No change #%d. See: python3 gd1check.py history" % args.change)
    h = match[0]
    if not h.get("backup"):
        sys.exit("Change #%d created a new file, so there's no earlier version to put back." % h["id"])
    put_back(cfg, h["remote"], Path(h["backup"]), "undo", "undo of change #%d" % h["id"])


def cmd_history(cfg, args):
    hist = read_history(cfg)
    if not hist:
        say("No changes made yet.")
    for h in hist:
        say("#%d  %s  %-7s %s  (%s)%s" % (h["id"], h["time"], h["action"], h["remote"], h["note"],
                                          "" if h["verified"] else "  NOT VERIFIED"))


def human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def save_folder_choice(config_path, folder, replace):
    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)
    folders = [] if replace else list(raw.get("remote_folders", []))
    if replace or folder not in folders:
        folders.append(folder)
    raw["remote_folders"] = folders
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")
    return folders


def preview_file(dev, path, size):
    if size is not None and size > 5 * 1024 * 1024:
        say("  %s is %s, too big to preview here. Add its folder and run a scan instead." % (path, human_size(size)))
        return
    buf = io.BytesIO()
    try:
        dev.call("retrbinary", "RETR " + path, buf.write)
    except ftplib.Error as e:
        say("  Can't read it: %s" % e)
        return
    data = buf.getvalue()
    say("")
    say("  %s  (%s)" % (path, human_size(len(data))))
    probs = check_bytes(path, data, size)
    if probs:
        for p in probs:
            say("  %-7s %s" % (p["level"].upper(), p["text"]))
    else:
        say("  Looks fine: no crash damage found.")
    if looks_like_text(data[:4096]):
        lines = data.rstrip(b"\x00").decode("utf-8", "replace").splitlines()
        say("  --- first %d of %d lines ---" % (min(20, len(lines)), len(lines)))
        for line in lines[:20]:
            say("  | " + line[:120])
    else:
        say("  (binary file) first bytes: " + data[:32].hex(" "))
    say("")


def cmd_browse(cfg, args):
    dev = GD1(cfg)
    dev.connect()
    here = args.start if args.start.startswith("/") else "/" + args.start
    say("Browsing %s. Type a number to open a folder or preview a file." % cfg["device_name"])
    while True:
        try:
            entries = dev._list(here)
        except ftplib.Error as e:
            say("Can't open %s (%s). Going back up." % (here, e))
            here = str(PurePosixPath(here).parent)
            continue
        entries = [e for e in entries if e[0] not in (".", "..") and e[1] != "skip"]
        dirs = sorted([e for e in entries if e[1] == "dir"], key=lambda e: e[0].lower())
        files = sorted([e for e in entries if e[1] == "file"], key=lambda e: e[0].lower())
        items = dirs + files
        say("")
        say("== %s  (%d folders, %d files)" % (here, len(dirs), len(files)))
        for i, (name, kind, size, modified) in enumerate(items, 1):
            if kind == "dir":
                say("  %3d  [%s/]" % (i, name))
            else:
                say("  %3d   %-40s %10s" % (i, name, human_size(size)))
        say("")
        say("  number = open    .. = up    /some/path = jump    pick = use this folder    q = quit")
        try:
            choice = input("> ").strip()
        except EOFError:
            choice = "q"
        if choice in ("q", "quit", "exit"):
            break
        elif choice == "..":
            here = str(PurePosixPath(here).parent)
        elif choice.startswith("/"):
            here = choice.rstrip("/") or "/"
        elif choice == "pick":
            ans = input("Add %s to the folders to scan? (a = add to the list, r = replace the list, anything else = cancel) "
                        % here).strip().lower()
            if ans in ("a", "r"):
                folders = save_folder_choice(args.config, here, ans == "r")
                say("Saved. Scans will now check: %s" % ", ".join(folders))
                say("Run: python3 gd1check.py scan")
                break
            say("Not saved.")
        elif choice.isdigit() and 1 <= int(choice) <= len(items):
            name, kind, size, _ = items[int(choice) - 1]
            full = str(PurePosixPath(here) / name)
            if kind == "dir":
                here = full
            else:
                preview_file(dev, full, size)
                input("Press Enter to go back to the list.")
        elif choice:
            say("Didn't understand that.")
    dev.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Find and fix files damaged by crashes on GD1 (over FTP).")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="settings file (default: config.json)")
    ap.add_argument("--device", help="use another saved device by name, e.g. --device GD2")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("test", help="check the connection to GD1")
    b = sub.add_parser("browse", help="look through GD1's folders and pick which to scan")
    b.add_argument("start", nargs="?", default="/", help="folder to start in (default: /)")
    sub.add_parser("scan", help="copy GD1's files to a snapshot and check them for damage")
    sub.add_parser("explain", help="ask the local AI to explain the last scan")
    r = sub.add_parser("restore", help="put a good copy of one file back on GD1")
    r.add_argument("file", help="path of the file on GD1, e.g. /data/save.db")
    r.add_argument("--snapshot", help="snapshot folder name to copy from (default: newest good copy)")
    u = sub.add_parser("undo", help="put back what a restore replaced")
    u.add_argument("change", type=int, help="change number from 'history'")
    sub.add_parser("history", help="list restores and undos")
    bk = sub.add_parser("backup", help="copy everything on the device to your backup folder")
    bk.add_argument("--to", help="backup folder on the Mac (default: backup_folder in the settings, or ~/Patchbay-backups)")
    args = ap.parse_args(argv)
    if args.device:
        args.config = str(config_for_device(args.device, args.config))
    if not args.cmd:
        # No command typed: show a simple menu instead of an error.
        say("What do you want to do?")
        say("  1  Test the connection to GD1")
        say("  2  Browse GD1's folders")
        say("  3  Scan for crash damage")
        say("  4  Explain the last scan (local AI)")
        say("  5  Show the history of changes")
        pick = input("Type a number: ").strip()
        menu = {"1": ["test"], "2": ["browse"], "3": ["scan"], "4": ["explain"], "5": ["history"]}
        if pick not in menu:
            sys.exit("Nothing chosen. You can also type a command, like: python3 gd1check.py browse")
        args = ap.parse_args(["--config", args.config] + menu[pick])
    cfg = load_config(args.config)
    {"test": cmd_test, "browse": cmd_browse, "scan": cmd_scan, "explain": cmd_explain, "restore": cmd_restore,
     "undo": cmd_undo, "history": cmd_history, "backup": cmd_backup}[args.cmd](cfg, args)


def cmd_backup(cfg, args):
    import backup   # lives in its own file next to this one
    backup.cmd_backup(cfg, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped.")
    except socket.timeout:
        sys.exit("GD1 stopped answering (timed out). Check it's on and try again.")
