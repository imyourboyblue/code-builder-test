#!/usr/bin/env python3
"""
Patchbay: your devices, an AI chat and a file manager, in your web browser.

    python3 patchbay.py           only this Mac can open it
    python3 patchbay.py --lan     your laptop can open it too (needs a password)

The home screen shows a picture for every device you've added that is on the
network right now. Click one to chat with the AI about it ("check it for crash
damage", "fix settings.json") or to open its Files screen and drag files
around. All the work (scanning, the AI, copying) happens on this Mac, even
when you're using the page from your laptop.

Safety
  - The AI can look at a device freely (reading never changes anything).
  - Nothing is written to a device until you type the device's name on a
    card and press Confirm. The old file is backed up first and can be undone.
  - From another computer you must sign in with your password first.
  - The AI must be Ollama on this Mac (localhost). Nothing goes to the internet.

Only uses Python's built-in parts, so there is nothing to install.
"""

import argparse
import base64
import contextlib
import difflib
import errno
import ftplib
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlparse

import gd1check as core
import backup
import devices
import firmware
import nro
import projects
import filemanager as fm
import webauth

TOOL_RESULT_LIMIT = 12000   # characters of tool output the AI sees at once
MAX_TOOL_ROUNDS = 12        # stop runaway tool loops
TEXT_EDIT_LIMIT = 2 * 1024 * 1024
NRO_LIMIT = 512 * 1024 * 1024   # .nro programs with built-in files can be big


# ----------------------------------------------------------------- state

BUSY = threading.Lock()     # one chat job at a time (scans print to stdout, which is shared)
BUSY_WITH = [""]
WRITES = threading.Lock()   # one write to a device at a time, so the history list stays in order
TRANSCRIPT_LIMIT = 400


class State:
    """One device's chat: what was said, and changes waiting for you to confirm."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.messages = []      # the conversation, as Ollama sees it
        self.pending = {}       # chat changes waiting for you to confirm
        self.file_pending = {}  # Files-screen changes waiting for you to confirm
        self.transcript = []    # what the page shows, so a reload or another computer sees the same chat

    def record(self, ev):
        if ev.get("type") in ("user", "answer", "note", "error", "confirm", "tool_result"):
            if ev["type"] == "tool_result":
                ev = dict(ev, text=clip(ev.get("text") or "", 3000))
            self.transcript.append(ev)
            del self.transcript[:-TRANSCRIPT_LIMIT]

    def cfg(self):
        try:
            return core.load_config(self.config_path)
        except SystemExit as e:
            raise ToolError(str(e.code))


class ToolError(Exception):
    pass


class LineSink(io.TextIOBase):
    """Catches what the checker prints and passes each line to the page."""

    def __init__(self, emit):
        self.emit, self.buf, self.lines = emit, "", []

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.lines.append(line)
            if line.strip():
                self.emit({"type": "progress", "text": line})
        return len(s)

    def text(self):
        return "\n".join(self.lines + ([self.buf] if self.buf else []))


def run_captured(emit, fn, *args, **kwargs):
    """Run a checker function, showing its printed lines live, and return
    (result, printed text). A 'sys.exit' inside becomes a normal message."""
    sink = LineSink(emit)
    result = None
    with contextlib.redirect_stdout(sink):
        try:
            result = fn(*args, **kwargs)
        except SystemExit as e:
            if e.code not in (None, 0):
                print(str(e.code))
        except (ftplib.Error, OSError, EOFError) as e:
            print("%s stopped answering or refused: %s" % ("The device", e))
    return result, sink.text()


def clip(text, limit=TOOL_RESULT_LIMIT):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (cut off: %d more characters)" % (len(text) - limit)


def norm(path):
    path = (path or "/").strip()
    return path if path.startswith("/") else "/" + path


# ----------------------------------------------------------------- tools

TOOLS = []
HANDLERS = {}


def tool(name, description, params=None, required=()):
    def deco(fn):
        TOOLS.append({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": params or {}, "required": list(required)}}})
        HANDLERS[name] = fn
        return fn
    return deco


S = {"type": "string"}
I = {"type": "integer"}


@tool("test_connection", "Check that this Mac can reach the device over FTP and list the folders being scanned.")
def t_test(st, emit, a):
    _, out = run_captured(emit, core.cmd_test, st.cfg(), None)
    return out


@tool("list_folder", "List the folders and files in one folder on the device (read-only).",
      {"path": dict(S, description="Folder on the device, e.g. /data")}, ["path"])
def t_list(st, emit, a):
    cfg, path = st.cfg(), norm(a.get("path"))
    dev = core.GD1(cfg)

    def go():
        dev.connect(quiet=True)
        try:
            return dev._list(path)
        finally:
            dev.close()
    entries, out = run_captured(emit, go)
    if entries is None:
        return out or "Couldn't list %s." % path
    entries = [e for e in entries if e[0] not in (".", "..")]
    dirs = sorted(e[0] + "/" for e in entries if e[1] == "dir")
    files = sorted("%s  (%s)" % (e[0], core.human_size(e[2])) for e in entries if e[1] == "file")
    return "%s holds %d folders and %d files.\n%s" % (path, len(dirs), len(files), "\n".join(dirs + files))


def fetch_from_device(cfg, emit, path, limit):
    dev = core.GD1(cfg)

    def go():
        dev.connect(quiet=True)
        try:
            size = dev.size(path)
            if size is not None and size > limit:
                raise ToolError("%s is %s, too big to read here (limit %s). Scan its folder instead."
                                % (path, core.human_size(size), core.human_size(limit)))
            buf = io.BytesIO()
            dev.call("retrbinary", "RETR " + path, buf.write)
            return buf.getvalue(), size
        finally:
            dev.close()
    got, out = run_captured(emit, go)
    if got is None:
        raise ToolError(out or "Couldn't read %s." % path)
    return got[0]


def snapshot_dir(cfg, name):
    root = core.data_dir(cfg) / "snapshots"
    snaps = sorted(p for p in root.glob("*") if p.is_dir())
    if not snaps:
        raise ToolError("There are no snapshots yet. Run a scan first.")
    if name in (None, "", "latest"):
        return snaps[-1]
    d = root / name
    if not d.is_dir():
        raise ToolError("No snapshot called %s. Available: %s" % (name, ", ".join(p.name for p in snaps[-10:])))
    return d


def describe_file(path, data, size, start_line, max_lines):
    lines_out = ["%s  (%s)" % (path, core.human_size(len(data)))]
    probs = core.check_bytes(path, data, size)
    if probs:
        lines_out.append("Damage checks:")
        lines_out += ["  %s: %s" % (p["level"].upper(), p["text"]) for p in probs]
    else:
        lines_out.append("Damage checks: no crash damage found.")
    if nro.looks_like_nro(data) or path.lower().endswith(".nro"):
        lines_out.append("--- .nro program ---")
        lines_out.append(nro.describe(data)[2] or "(the header is too damaged to read the details)")
        return "\n".join(lines_out)
    if core.looks_like_text(data[:4096]):
        text = data.rstrip(b"\x00").decode("utf-8", "replace").splitlines()
        start = max(1, start_line)
        end = min(len(text), start + max_lines - 1)
        lines_out.append("--- lines %d to %d of %d ---" % (start, end, len(text)))
        lines_out += ["%5d| %s" % (i, text[i - 1][:400]) for i in range(start, end + 1)]
    else:
        lines_out.append("(binary file) first bytes: " + data[:64].hex(" "))
        lines_out.append("last bytes: " + data[-64:].hex(" "))
    return "\n".join(lines_out)


@tool("read_file",
      "Read one file and run the crash-damage checks on it. Shows text with line numbers, or the first and last "
      "bytes of a binary file. By default reads the live file on the device; set snapshot to read the copy saved "
      "by an earlier scan ('latest' or a snapshot name).",
      {"path": dict(S, description="File path on the device, e.g. /data/settings.json"),
       "snapshot": dict(S, description="Optional: 'latest' or a snapshot name. Leave out to read the live device."),
       "start_line": dict(I, description="Optional: first line to show (default 1)"),
       "max_lines": dict(I, description="Optional: how many lines to show (default 300)")}, ["path"])
def t_read(st, emit, a):
    cfg, path = st.cfg(), norm(a.get("path"))
    start, count = int(a.get("start_line") or 1), min(int(a.get("max_lines") or 300), 1000)
    if a.get("snapshot"):
        snap = snapshot_dir(cfg, a["snapshot"])
        local = core.local_path_for(snap, path)
        if not local.exists():
            raise ToolError("%s isn't in snapshot %s." % (path, snap.name))
        return "From snapshot %s:\n%s" % (snap.name, describe_file(path, local.read_bytes(), None, start, count))
    data = fetch_from_device(cfg, emit, path, NRO_LIMIT if path.lower().endswith(".nro") else 5 * 1024 * 1024)
    return "Live on %s:\n%s" % (cfg["device_name"], describe_file(path, data, len(data), start, count))


@tool("scan_device",
      "Copy every file in the configured folders to a new snapshot on this Mac and check each one for crash damage "
      "(truncation, bad headers, null padding, broken JSON/XML/SQLite, leftover temp files). Read-only on the "
      "device. Can take a while on big folders.")
def t_scan(st, emit, a):
    cfg = st.cfg()
    _, out = run_captured(emit, core.cmd_scan, cfg, None)
    try:
        rep = latest_report_dict(cfg)
        return core.report_text(rep) + "\nSnapshot: %s" % Path(rep["snapshot"]).name
    except ToolError:
        return out


def latest_report_dict(cfg):
    reports = sorted((core.data_dir(cfg) / "reports").glob("*.json"))
    if not reports:
        raise ToolError("No scan yet. Use scan_device first.")
    return json.loads(reports[-1].read_text())


@tool("last_report", "Show what the most recent scan found, without scanning again.")
def t_report(st, emit, a):
    rep = latest_report_dict(st.cfg())
    return core.report_text(rep) + "\nSnapshot: %s" % Path(rep["snapshot"]).name


@tool("find_good_copies", "List the snapshots that hold a copy of one file, and whether each copy passed the damage checks.",
      {"path": dict(S, description="File path on the device")}, ["path"])
def t_copies(st, emit, a):
    cfg, path = st.cfg(), norm(a.get("path"))
    rows = []
    for s in sorted(p for p in (core.data_dir(cfg) / "snapshots").glob("*") if p.is_dir()):
        m = s / "_manifest.json"
        if not m.exists():
            continue
        info = json.loads(m.read_text()).get(path)
        if info:
            rows.append("snapshot %s  %s  %s" % (s.name, core.human_size(info["size"]), "good" if info["ok"] else "DAMAGED"))
    for b, local, ok in backup_copies(cfg, path):
        rows.append("backup %s  %s  %s" % (b["name"], core.human_size(local.stat().st_size), "good" if ok else "DAMAGED"))
    return "\n".join(rows) if rows else "No snapshot or backup holds %s." % path


def backup_copies(cfg, path, newest_first=False):
    """(backup, local file, passed the checks) for each full backup holding this file."""
    out = []
    for b in backup.list_backups(cfg):
        local = core.local_path_for(Path(b["path"]), path)
        if local.is_file() and local.stat().st_size <= NRO_LIMIT:
            out.append((b, local, not [x for x in core.check_bytes(path, local.read_bytes()) if x["level"] == "error"]))
    return out if newest_first else list(reversed(out))


@tool("search_snapshot",
      "Search the text files in a snapshot for a word or phrase (like grep). Good for finding where a setting or "
      "function lives in the device's code.",
      {"text": dict(S, description="Text to look for (not case-sensitive)"),
       "snapshot": dict(S, description="Optional: snapshot name, default latest"),
       "folder": dict(S, description="Optional: only search under this folder on the device")}, ["text"])
def t_search(st, emit, a):
    cfg = st.cfg()
    snap = snapshot_dir(cfg, a.get("snapshot"))
    base = core.local_path_for(snap, norm(a["folder"])) if a.get("folder") else snap
    needle = (a.get("text") or "").lower()
    if not needle:
        raise ToolError("Give some text to search for.")
    hits = []
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.name == "_manifest.json" or f.stat().st_size > TEXT_EDIT_LIMIT:
            continue
        data = f.read_bytes()
        if not core.looks_like_text(data[:4096]):
            continue
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if needle in line.lower():
                hits.append("/%s:%d: %s" % (f.relative_to(snap).as_posix(), n, line.strip()[:200]))
                if len(hits) >= 80:
                    return "\n".join(hits) + "\n(stopped at 80 matches)"
    return "\n".join(hits) if hits else "No matches in snapshot %s." % snap.name


@tool("change_history", "List every change made to the device (restores, edits, undos) with their numbers.")
def t_history(st, emit, a):
    _, out = run_captured(emit, core.cmd_history, st.cfg(), None)
    return out


@tool("set_scan_folders", "Change which folders on the device future scans check. This only edits config.json on this Mac.",
      {"folders": {"type": "array", "items": S, "description": "Folders, e.g. [\"/data\", \"/etc\"]"}}, ["folders"])
def t_folders(st, emit, a):
    folders = [norm(f) for f in a.get("folders") or []]
    if not folders:
        raise ToolError("Give at least one folder.")
    for i, f in enumerate(folders):
        core.save_folder_choice(st.config_path, f, replace=(i == 0))
    return "Saved. Scans will now check: %s" % ", ".join(folders)


# ---- write proposals: these never write. They make a card you must confirm.

def add_pending(st, emit, kind, cfg, remote, source, note, summary, diff=None, on_mac=False, extra_fields=None):
    if on_mac:
        # Changes on the Mac (project files, builds): pressing Confirm is enough.
        pid = uuid.uuid4().hex[:8]
        st.pending[pid] = dict({"kind": kind, "remote": remote, "source": str(source), "note": note}, **(extra_fields or {}))
        emit({"type": "confirm", "id": pid, "title": summary, "remote": remote, "diff": diff, "on_mac": True,
              "typing": bool((extra_fields or {}).get("needs_typing")),
              "device": cfg["device_name"], "writes_on": True, "computer": APP.computer if APP else "the Mac"})
        return ("A confirmation card is now showing on the page. Nothing has happened yet. The user must press Confirm. "
                "Tell the user what it does and ask them to confirm it on the card.")
    if not cfg.get("allow_writes"):
        extra = (" Writing is turned off right now, so even after you confirm it will be refused until "
                 "\"allow_writes\" is true in config.json.")
    else:
        extra = ""
    pid = uuid.uuid4().hex[:8]
    st.pending[pid] = {"kind": kind, "remote": remote, "source": str(source), "note": note}
    emit({"type": "confirm", "id": pid, "title": summary, "remote": remote, "diff": diff,
          "device": cfg["device_name"], "writes_on": bool(cfg.get("allow_writes"))})
    return ("A confirmation card is now showing on the page. Nothing has been written yet. The user must type %s "
            "and press Confirm.%s Tell the user what the change does and ask them to confirm it on the card."
            % (cfg["device_name"], extra))


@tool("propose_restore",
      "Suggest putting a good copy of a file back on the device from a scan snapshot or a full backup. This does "
      "NOT write: it shows the user a confirmation card. The current file is backed up first and can be undone.",
      {"path": dict(S, description="File path on the device"),
       "snapshot": dict(S, description="Optional: snapshot name. Default: newest copy that passed every check."),
       "backup": dict(S, description="Optional: full backup name (from find_good_copies) to copy from instead")},
      ["path"])
def t_prop_restore(st, emit, a):
    cfg, remote = st.cfg(), norm(a.get("path"))
    root = core.data_dir(cfg) / "snapshots"
    if a.get("backup"):
        hit = [x for x in backup_copies(cfg, remote) if x[0]["name"] == a["backup"]]
        if not hit:
            raise ToolError("Backup %s doesn't hold %s." % (a["backup"], remote))
        b, local, ok = hit[0]
        return add_pending(st, emit, "restore", cfg, remote, local, "from backup %s" % b["name"],
                           "Restore %s from backup %s (%s)%s" % (remote, b["name"], core.human_size(local.stat().st_size),
                                                                "" if ok else " WARNING: this copy also fails the checks"))
    if a.get("snapshot"):
        chosen = snapshot_dir(cfg, a["snapshot"])
    else:
        chosen = None
        for s in sorted((p for p in root.glob("*") if p.is_dir()), reverse=True):
            m = s / "_manifest.json"
            if m.exists() and json.loads(m.read_text()).get(remote, {}).get("ok"):
                chosen = s
                break
        if chosen is None:
            good = [x for x in backup_copies(cfg, remote, newest_first=True) if x[2]]
            if good:
                b, local, _ = good[0]
                return add_pending(st, emit, "restore", cfg, remote, local, "from backup %s" % b["name"],
                                   "Restore %s from backup %s (%s)" % (remote, b["name"], core.human_size(local.stat().st_size)))
            raise ToolError("No snapshot or backup has a good copy of %s." % remote)
    source = core.local_path_for(chosen, remote)
    if not source.exists():
        raise ToolError("%s isn't in snapshot %s." % (remote, chosen.name))
    return add_pending(st, emit, "restore", cfg, remote, source, "from snapshot %s" % chosen.name,
                       "Restore %s from snapshot %s (%s)" % (remote, chosen.name, core.human_size(source.stat().st_size)))


@tool("propose_edit",
      "Suggest a change to a text or code file on the device. This does NOT write: it shows the user the exact "
      "difference and a confirmation card. Either give find + replace (find must appear exactly once in the "
      "current file; best for small fixes), or give new_content with the whole new file. Always read_file first.",
      {"path": dict(S, description="File path on the device"),
       "find": dict(S, description="Exact text to replace (copy it from read_file, without the line numbers)"),
       "replace": dict(S, description="Text to put in its place"),
       "new_content": dict(S, description="The whole new file, instead of find/replace"),
       "reason": dict(S, description="One short sentence on why")}, ["path", "reason"])
def t_prop_edit(st, emit, a):
    cfg, remote = st.cfg(), norm(a.get("path"))
    old = fetch_from_device(cfg, emit, remote, TEXT_EDIT_LIMIT)
    if not core.looks_like_text(old[:4096]) and old:
        raise ToolError("%s isn't a text file, so it can't be edited this way. Use propose_restore instead." % remote)
    old_text = old.decode("utf-8", "replace")
    if a.get("new_content") is not None:
        new_text = a["new_content"]
    elif a.get("find"):
        n = old_text.count(a["find"])
        if n != 1:
            raise ToolError("The find text appears %d times in %s; it must appear exactly once. Read the file "
                            "again and copy the text exactly, or include more surrounding lines." % (n, remote))
        new_text = old_text.replace(a["find"], a.get("replace") or "", 1)
    else:
        raise ToolError("Give either find + replace, or new_content.")
    if new_text == old_text:
        raise ToolError("That change wouldn't change anything.")
    diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                        "before: " + remote, "after: " + remote, n=3))
    folder = core.unique_dir(core.data_dir(cfg) / "edits", core.now_stamp())
    source = core.local_path_for(folder, remote)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(new_text.encode("utf-8"))
    probs = [p["text"] for p in core.check_bytes(remote, new_text.encode("utf-8")) if p["level"] == "error"]
    if probs:
        diff = "WARNING, the new version fails these checks: %s\n\n%s" % ("; ".join(probs), diff)
    return add_pending(st, emit, "edit", cfg, remote, source, a.get("reason") or "edit from chat",
                       "Edit %s: %s" % (remote, a.get("reason") or ""), clip(diff, 40000))


@tool("propose_undo", "Suggest undoing an earlier change by its number from change_history. Does NOT write: shows a confirmation card.",
      {"change": dict(I, description="Change number")}, ["change"])
def t_prop_undo(st, emit, a):
    cfg = st.cfg()
    hist = core.read_history(cfg)
    match = [h for h in hist if h["id"] == int(a.get("change") or 0)]
    if not match:
        raise ToolError("No change #%s." % a.get("change"))
    h = match[0]
    if h.get("action") == "rename":
        raise ToolError("Change #%d was a rename. To undo it, rename %s back on the Files screen." % (h["id"], h["remote"]))
    if not h.get("backup"):
        raise ToolError("Change #%d created a new file or folder, so there's no earlier version to put back. "
                        "To undo it, delete %s on the Files screen." % (h["id"], h["remote"]))
    return add_pending(st, emit, "undo", cfg, h["remote"], h["backup"], "undo of change #%d" % h["id"],
                       "Undo change #%d (%s %s)" % (h["id"], h["action"], h["remote"]))


# ---- .nro programs

def staged_copy(cfg, remote, data):
    folder = core.unique_dir(core.data_dir(cfg) / "edits", core.now_stamp())
    path = core.local_path_for(folder, remote)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@tool("propose_nro_info_change",
      "Suggest a new name, author and/or display version for an .nro program on the device (they're stored in its "
      "NACP block). Does NOT write: shows a confirmation card. The program code isn't touched.",
      {"path": dict(S, description="The .nro on the device"), "name": dict(S, description="Optional: new name"),
       "author": dict(S, description="Optional: new author"), "version": dict(S, description="Optional: new version, e.g. 1.2.0")},
      ["path"])
def t_nro_info(st, emit, a):
    cfg, remote = st.cfg(), norm(a.get("path"))
    if all(a.get(k) in (None, "") for k in ("name", "author", "version")):
        raise ToolError("Give a new name, author or version.")
    old = fetch_from_device(cfg, emit, remote, NRO_LIMIT)
    try:
        new = nro.with_metadata(old, a.get("name") or None, a.get("author") or None, a.get("version") or None)
    except ValueError as e:
        raise ToolError(str(e))
    before, after = nro.inspect(old)[0], nro.inspect(new)[0]
    diff = "\n".join("%s: %s  ->  %s" % (k, before.get(k) or "(empty)", after.get(k) or "(empty)")
                     for k in ("name", "author", "version") if before.get(k) != after.get(k))
    source = staged_copy(cfg, remote, new)
    return add_pending(st, emit, "edit", cfg, remote, source, "new .nro details", "Change the details of %s" % remote, diff)


@tool("propose_nro_icon",
      "Suggest a new icon for an .nro program on the device, from a JPEG picture on the Mac. Does NOT write: shows a "
      "confirmation card.",
      {"path": dict(S, description="The .nro on the device"),
       "picture": dict(S, description="Path of a JPEG on the Mac, e.g. ~/Pictures/icon.jpg (256 x 256 is usual)")},
      ["path", "picture"])
def t_nro_icon(st, emit, a):
    cfg, remote = st.cfg(), norm(a.get("path"))
    pic = Path(os.path.expanduser(a.get("picture") or "")).resolve()
    if not pic.is_file():
        raise ToolError("%s isn't a file on the Mac." % pic)
    old = fetch_from_device(cfg, emit, remote, NRO_LIMIT)
    try:
        new = nro.with_icon(old, pic.read_bytes())
    except ValueError as e:
        raise ToolError(str(e))
    source = staged_copy(cfg, remote, new)
    return add_pending(st, emit, "edit", cfg, remote, source, "new icon from %s" % pic.name,
                       "Give %s the icon %s" % (remote, pic.name))


# ---- source projects on the Mac (the way to change what a program does, e.g. for newer firmware)

def project(st, name):
    try:
        return projects.get(st.cfg(), name)
    except projects.ProjectError as e:
        raise ToolError(str(e))


def pe(fn, *args):
    try:
        return fn(*args)
    except projects.ProjectError as e:
        raise ToolError(str(e))


@tool("set_project",
      "Save (or update) a source code project on the Mac for this device: where its folder is, the command that "
      "builds it, the file the build makes, and where that file goes on the device. Only edits the settings on the Mac.",
      {"name": dict(S, description="Short project name"), "folder": dict(S, description="Project folder on the Mac, e.g. ~/code/mygame"),
       "build": dict(S, description="Build command run in that folder, usually: make"),
       "output": dict(S, description="The built file, relative to the folder, e.g. mygame.nro"),
       "device_path": dict(S, description="Where the built file goes on the device, e.g. /switch/mygame/mygame.nro")},
      ["name", "folder"])
def t_set_project(st, emit, a):
    pr = pe(projects.make_project, a.get("name"), a.get("folder"), a.get("build"), a.get("output"), a.get("device_path"))
    projects.save(st.config_path, pr)
    return "Saved project %s. Build command: %s. Built file: %s. Goes to: %s" % (
        pr["name"], pr["build"], pr["output"] or "(not set)", pr["device_path"] or "(not set)")


@tool("list_projects", "List the source code projects saved for this device.")
def t_projects(st, emit, a):
    prs = projects.all_projects(st.cfg())
    if not prs:
        return "No projects yet. Ask the user where the source code folder is, then use set_project."
    return "\n".join("%s: %s (build: %s, makes %s, goes to %s)" % (p["name"], p["folder"], p["build"],
                     p.get("output") or "?", p.get("device_path") or "?") for p in prs)


@tool("project_files", "List the files in a project (read-only).",
      {"project": S, "folder": dict(S, description="Optional: only this sub-folder")}, ["project"])
def t_project_files(st, emit, a):
    return pe(projects.tree, project(st, a.get("project")), a.get("folder") or "")


@tool("project_read", "Read a source file in a project, with line numbers (read-only).",
      {"project": S, "path": dict(S, description="File path inside the project, e.g. source/main.c"),
       "start_line": I, "max_lines": I}, ["project", "path"])
def t_project_read(st, emit, a):
    return pe(projects.read, project(st, a.get("project")), a.get("path"), int(a.get("start_line") or 1),
              min(int(a.get("max_lines") or 300), 1000))


@tool("project_search", "Search every source file in a project for a word or phrase (read-only).",
      {"project": S, "text": S}, ["project", "text"])
def t_project_search(st, emit, a):
    if not a.get("text"):
        raise ToolError("Give some text to search for.")
    return pe(projects.search, project(st, a.get("project")), a["text"])


@tool("propose_project_edit",
      "Suggest a change to a source file in a project on the Mac (or a new file). Does NOT write: shows the exact "
      "difference on a card the user confirms. Give find + replace (find must appear exactly once), or new_content. "
      "Always project_read the file first.",
      {"project": S, "path": dict(S, description="File path inside the project"),
       "find": S, "replace": S, "new_content": dict(S, description="The whole new file, instead of find/replace"),
       "reason": dict(S, description="One short sentence on why")}, ["project", "path", "reason"])
def t_project_edit(st, emit, a):
    cfg, pr = st.cfg(), project(st, a.get("project"))
    root, p = pe(projects.inside, pr, a.get("path"))
    rel = p.relative_to(root).as_posix()
    old_text = p.read_text("utf-8", "replace") if p.exists() else ""
    if p.exists() and not projects.is_text(p):
        raise ToolError("%s isn't a text file." % rel)
    if a.get("new_content") is not None:
        new_text = a["new_content"]
    elif a.get("find"):
        n = old_text.count(a["find"])
        if n != 1:
            raise ToolError("The find text appears %d times in %s; it must appear exactly once." % (n, rel))
        new_text = old_text.replace(a["find"], a.get("replace") or "", 1)
    else:
        raise ToolError("Give either find + replace, or new_content.")
    if new_text == old_text:
        raise ToolError("That change wouldn't change anything.")
    diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                        "before: " + rel, "after: " + rel, n=3))
    folder = core.unique_dir(core.data_dir(cfg) / "edits", core.now_stamp())
    staged = folder / "new-file.txt"
    staged.write_text(new_text, "utf-8")
    return add_pending(st, emit, "project_edit", cfg, rel, staged, a.get("reason") or "",
                       "%s %s in project %s: %s" % ("Edit" if p.exists() else "Create", rel, pr["name"], a.get("reason") or ""),
                       clip(diff, 40000), on_mac=True, extra_fields={"project": pr["name"]})


@tool("propose_build",
      "Suggest building a project on the Mac with its build command. Does NOT run: shows a card the user confirms. "
      "Afterwards you get the build output, so you can fix errors.", {"project": S}, ["project"])
def t_build(st, emit, a):
    cfg, pr = st.cfg(), project(st, a.get("project"))
    root = pe(projects.folder_of, pr)
    return add_pending(st, emit, "build", cfg, pr["name"], root, "",
                       "Build project %s on %s" % (pr["name"], APP.computer if APP else "the Mac"),
                       "In folder: %s\nRun: %s" % (root, pr["build"]), on_mac=True, extra_fields={"project": pr["name"]})


@tool("propose_install",
      "Suggest copying a project's freshly built file (e.g. the new .nro) to its place on the device. Does NOT write: "
      "shows a card; the device's current file is backed up first.", {"project": S}, ["project"])
def t_install(st, emit, a):
    cfg, pr = st.cfg(), project(st, a.get("project"))
    if not pr.get("output") or not pr.get("device_path"):
        raise ToolError("Set the project's output file and device_path first (set_project).")
    _, out = pe(projects.inside, pr, pr["output"])
    if not out.is_file():
        raise ToolError("%s hasn't been built yet. Use propose_build first." % pr["output"])
    probs = [x["text"] for x in core.check_bytes(out.name, out.read_bytes()) if x["level"] == "error"]
    if probs:
        raise ToolError("The built file fails the damage checks, so I won't install it: %s" % probs[0])
    age = time.time() - out.stat().st_mtime
    return add_pending(st, emit, "install", cfg, norm(pr["device_path"]), out, "built from project %s" % pr["name"],
                       "Install %s (%s, built %s ago) to %s" % (pr["output"], core.human_size(out.stat().st_size),
                                                              "%d min" % (age // 60) if age < 7200 else "%d h" % (age // 3600),
                                                              pr["device_path"]))


# ---- settings

@tool("show_settings", "Show every setting for this device and the shared app settings (passwords hidden).")
def t_show_settings(st, emit, a):
    cfg = st.cfg()
    rows = ["Settings for %s (file %s):" % (cfg["device_name"], Path(st.config_path).name)]
    for k, (kind, what) in devices.SETTINGS.items():
        rows.append("  %s = %s   (%s)" % (k, devices.show(k, cfg.get(k)), what))
    return "\n".join(rows)


@tool("propose_settings",
      "Suggest changing one or more settings (see show_settings), e.g. {\"host\": \"192.168.50.203\", \"ports\": "
      "[8080]}. Shared settings go in config.json, the rest in this device's settings file. Does NOT write: shows "
      "a card with each old and new value. The settings file is backed up first.",
      {"changes": {"type": "object", "description": "setting name -> new value"},
       "reason": dict(S, description="One short sentence on why")}, ["changes"])
def t_prop_settings(st, emit, a):
    cfg = st.cfg()
    changes = a.get("changes") or {}
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except ValueError:
            raise ToolError("changes must be an object like {\"host\": \"192.168.1.60\"}.")
    try:
        planned = devices.plan_changes(APP.registry, st.config_path, changes)
    except devices.DeviceError as e:
        raise ToolError(str(e))
    lines = "\n".join("%s:  %s  ->  %s   (in %s)" % (k, devices.show(k, old), devices.show(k, new), Path(f).name)
                      for f, k, old, new in planned)
    typing = any(k == "allow_writes" and new for f, k, old, new in planned)
    return add_pending(st, emit, "settings", cfg, "settings", st.config_path, a.get("reason") or "",
                       "Change settings for %s%s" % (cfg["device_name"], ": " + a["reason"] if a.get("reason") else ""),
                       lines, on_mac=True, extra_fields={"planned": planned, "needs_typing": typing})


@tool("check_address", "See whether anything answers at an IP address and port on the network, and what it says "
      "(FTP, SSH, web...). Read-only.", {"host": S, "port": I}, ["host", "port"])
def t_check_address(st, emit, a):
    return devices.probe((a.get("host") or "").strip(), int(a.get("port") or 21))


@tool("add_device",
      "Save a new device (it then gets its own picture on the home screen). Logs in to its FTP server first to check "
      "it works. Only changes settings on the Mac.",
      {"name": dict(S, description="Short name like GD2"), "host": S,
       "ports": {"type": "array", "items": I, "description": "FTP ports, e.g. [21] or [8080]"},
       "username": S, "password": S, "about": dict(S, description="Optional: what the device is")}, ["name", "host"])
def t_add_device(st, emit, a):
    ports = a.get("ports") or [21, 5000]
    test = dict(devices.NEW_DEVICE, device_name=a.get("name") or "device", host=(a.get("host") or "").strip(),
                ports=ports if isinstance(ports, list) else [ports], username=a.get("username") or "anonymous",
                password=a.get("password") or "anonymous@", timeout_seconds=6)
    dev = core.GD1(test)
    try:
        dev.connect(quiet=True)
        dev.close()
    except SystemExit as e:
        raise ToolError("Couldn't log in to an FTP server there, so I didn't save it: %s" % e.code)
    try:
        f = APP.registry.add(a.get("name"), a.get("host"), test["ports"], a.get("username"), a.get("password"),
                             a.get("about"))
    except devices.DeviceError as e:
        raise ToolError(str(e))
    return "Saved %s (settings in %s). It's on the home screen now; open it there to work on it." % (a.get("name"), f.name)


@tool("identify_firmware",
      "Work out which firmware / operating system ANY device runs: reads its FTP greeting, version files (like "
      "/etc/os-release or build.prop), folder names, and the greetings of its other services (SSH, web). Read-only. "
      "Saves the best guess so it shows on the device's picture.")
def t_identify(st, emit, a):
    cfg = st.cfg()
    emit({"type": "status", "text": "Looking for firmware clues on %s..." % cfg["device_name"]})
    report, guess = firmware.identify(cfg)
    if guess:
        APP.registry.set_firmware(cfg, guess)
    return clip(report)


@tool("save_firmware_name",
      "Save a short name for the device's firmware (e.g. 'FreeBSD 14.1-RELEASE' or 'Switch 19.0.1, Atmosphère "
      "1.8.0') once you're sure, so it shows on the device's picture. Only edits notes on the Mac.",
      {"name": S}, ["name"])
def t_save_fw(st, emit, a):
    APP.registry.set_firmware(st.cfg(), a.get("name"))
    return "Saved. The device's picture now says: %s" % a.get("name")


@tool("web_search",
      "Search the web. ONLY for generic text such as a version string, a server name or an error message; never "
      "include the user's file contents, code, names or addresses.", {"query": S}, ["query"])
def t_search_web(st, emit, a):
    q = (a.get("query") or "").strip()
    if not q:
        raise ToolError("Give something to search for.")
    try:
        return clip(firmware.web_search(q[:200]))
    except (OSError, ValueError) as e:
        raise ToolError("The search didn't work: %s" % e)


@tool("device_firmware",
      "Find out which firmware and custom firmware (Atmosphère, hekate) the device runs, and which homebrew is "
      "installed in /switch. Best effort; read-only.")
def t_firmware(st, emit, a):
    return firmware.device_report(st.cfg())


@tool("latest_release",
      "Get the newest releases (version, date, release notes) of a public GitHub project, e.g. "
      "Atmosphere-NX/Atmosphere, switchbrew/libnx, or the homebrew's own repository. General research only.",
      {"repo": dict(S, description="owner/name, e.g. switchbrew/libnx")}, ["repo"])
def t_latest(st, emit, a):
    try:
        return clip(firmware.latest_release(a.get("repo")))
    except (OSError, ValueError) as e:
        raise ToolError("Couldn't get the releases: %s" % e)


@tool("check_build_tools",
      "Check what's installed on the Mac for building Switch homebrew (devkitPro, libnx, devkitA64, portlibs) and "
      "their versions. Read-only.")
def t_tools(st, emit, a):
    return projects.toolchain_report()


@tool("propose_get_source",
      "Suggest downloading a homebrew program's public source code (git clone with submodules) to the Mac, into "
      "~/Patchbay-projects/<name>, and saving it as a project. Does NOT run: shows a card the user confirms.",
      {"url": dict(S, description="Public git address, e.g. https://github.com/owner/project"),
       "name": dict(S, description="Optional short project name"),
       "build": dict(S, description="Build command, if known (default: make)"),
       "output": dict(S, description="The built file, relative to the project folder, if known"),
       "device_path": dict(S, description="Where the built file goes on the device, if known")}, ["url"])
def t_get_source(st, emit, a):
    cfg = st.cfg()
    name, dest = pe(projects.clone_target, (a.get("url") or "").strip(), a.get("name"))
    if dest.exists() and any(dest.iterdir()):
        raise ToolError("%s already exists on the Mac. Use set_project with that folder instead." % dest)
    return add_pending(st, emit, "get_source", cfg, name, dest, "",
                       "Download the source code of %s" % name,
                       "Run: git clone --recurse-submodules %s\nInto: %s" % (a["url"].strip(), dest), on_mac=True,
                       extra_fields={"project": name, "url": a["url"].strip(), "build": a.get("build") or "make",
                                     "output": a.get("output") or "", "device_path": a.get("device_path") or ""})


@tool("propose_command",
      "Suggest running one command in a project's folder on the Mac, e.g. a cmake setup step, 'git pull', 'git "
      "submodule update --init --recursive', or 'make clean'. Does NOT run: shows the exact command on a card the "
      "user confirms. Commands needing sudo can't run here; give those to the user to type in Terminal.",
      {"project": S, "command": S, "reason": dict(S, description="One short sentence on why")}, ["project", "command"])
def t_command(st, emit, a):
    cfg, pr = st.cfg(), project(st, a.get("project"))
    cmd = (a.get("command") or "").strip()
    if not cmd:
        raise ToolError("Give the command to run.")
    if re.search(r"(^|[;&|]\s*)sudo\b", cmd):
        raise ToolError("Commands with sudo need the Mac password, so they can't run from the chat. Ask the user to "
                        "type it in Terminal.")
    root = pe(projects.folder_of, pr)
    return add_pending(st, emit, "command", cfg, pr["name"], root, a.get("reason") or "",
                       "Run a command in project %s: %s" % (pr["name"], a.get("reason") or ""),
                       "In folder: %s\nRun: %s" % (root, cmd), on_mac=True,
                       extra_fields={"project": pr["name"], "command": cmd})


@tool("read_web_page",
      "Read a public web page (documentation, changelogs, e.g. libnx release notes on GitHub) as plain text. Only for "
      "general research: never put the user's file names, code or device details in the address.",
      {"url": dict(S, description="https:// address")}, ["url"])
def t_web(st, emit, a):
    url = (a.get("url") or "").strip()
    if not url.startswith("https://"):
        raise ToolError("Only https:// addresses.")
    req = urllib.request.Request(url, headers={"User-Agent": "patchbay/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(3 * 1024 * 1024).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except (OSError, ValueError) as e:
        raise ToolError("Couldn't read that page: %s" % e)
    text = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", re.sub(r"&amp;", "&", text)))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return clip(text)


# ----------------------------------------------------------------- the AI

SYSTEM = """You are Patchbay, an assistant running fully on the user's own Mac. You help the user find and fix \
files on their device %(device)s (%(host)s), %(about)s

The user is not a programmer. Answer in short, plain sentences. Explain technical words when you use them.

You have tools. Use them instead of guessing: look before you answer, and never invent file contents or results.
- Reading tools (list_folder, read_file, scan_device, last_report, find_good_copies, search_snapshot, \
change_history, test_connection) never change the device. Use them freely.
- You can NOT write to the device yourself. To change a file, call propose_restore, propose_edit or propose_undo. \
That only shows the user a card; the user decides. Never say a change is done until a message tells you it was \
confirmed and succeeded.
- Before proposing an edit, read the file. Prefer small find/replace edits. Keep the file's style.
- Fixes that stop the damage for good usually mean changing how the program saves: write to a temporary file, \
flush it, then rename it over the real one; use a SQLite journal (WAL); keep a backup copy.
- The app also has a Files screen where the user can drag files between the Mac and the device. Point them to it \
for copying, uploading, renaming or deleting whole files and folders.
- .nro files are compiled homebrew programs. read_file shows an .nro's name, version, sizes and damage checks. To \
fix a damaged .nro, put back a good copy (find_good_copies looks in scan snapshots and full backups, then \
propose_restore) or rebuild it from source. You can change its name, author or version (propose_nro_info_change) \
or its icon (propose_nro_icon). You can NOT change an .nro's program code directly: it is compiled.
- To change what a program does (for example update it for newer firmware), work on its source code project on \
the Mac: list_projects (or ask where the source folder is and use set_project), project_files, project_search, \
project_read, then propose_project_edit, then propose_build. Read the build output, fix errors with more edits \
and builds, and when it builds cleanly offer propose_install to put the new file on the device. Switch homebrew is \
usually built with devkitPro and libnx; firmware updates usually mean building with the newest libnx and fixing code \
where its functions changed. read_web_page can fetch public release notes; never put the user's code or device \
details into an address.
- The user can change any setting by asking ("search 192.168.50.203 port 8080", "scan only /data", "use model X"). \
Use show_settings, then propose_settings with exactly what they asked. For a new address: check_address first. If \
it's a different device than this one, offer add_device; if they mean this device moved, propose_settings for \
host/ports. After a change to host or ports, use test_connection.
- To tell what firmware or operating system a device runs, use identify_firmware first. If its guess is vague, \
look up the most specific clue with web_search (generic text only), then save_firmware_name with a short answer.
- When the user asks to update a homebrew program for their firmware (like a newer Atmosphère), follow these \
steps, telling the user briefly what you're doing at each one:
  1. device_firmware to learn the firmware and Atmosphère versions (ask the user if it's unclear), and read_file \
the program's .nro to see its current version.
  2. latest_release for Atmosphere-NX/Atmosphere and switchbrew/libnx, and for the program's own repository, to \
see what changed. Maybe a newer release of the program already supports the firmware: then say so first.
  3. check_build_tools. If devkitPro or libnx is missing or old, give the user the exact Terminal command \
(e.g. sudo dkp-pacman -Syu) and wait until they say it's done.
  4. Get the source: list_projects, or propose_get_source with the program's public git address.
  5. Read the project's build instructions (README, Makefile, CMakeLists.txt). Use propose_command for setup steps \
(cmake configure, git submodule update) and set_project to save the right build command and output file.
  6. propose_build. Read the errors; fix the code with propose_project_edit (libnx renames and changed functions \
are the usual cause; look up the libnx changelog); build again until it builds cleanly.
  7. propose_install (the device's current file is backed up first). Then ask the user to test it on the console.
- If something fails, say what failed in one line and what to try next.

Scanning settings: folders %(folders)s. Writing to the device is currently %(writes)s."""


def ollama_url(cfg):
    url = cfg["ollama_url"].rstrip("/")
    host = re.sub(r"^https?://", "", url).split("/")[0].rsplit(":", 1)[0].strip("[]")
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise ToolError("ollama_url must point at this Mac (localhost) so your files never leave it.")
    return url


def ask_model(cfg, messages):
    body = json.dumps({"model": cfg["ollama_model"], "stream": False, "messages": messages, "tools": TOOLS,
                       "options": {"num_ctx": int(cfg.get("chat_context", 32768))}}).encode()
    req = urllib.request.Request(ollama_url(cfg) + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read())["message"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if "not found" in detail:
            raise ToolError("Ollama doesn't have the model %s yet. In Terminal run: ollama pull %s"
                            % (cfg["ollama_model"], cfg["ollama_model"]))
        raise ToolError("Ollama said: %s" % detail)
    except OSError as e:
        raise ToolError("Couldn't reach Ollama (%s). Is the Ollama app running?" % e)


def clean_answer(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def chat_turn(st, user_text, emit):
    cfg = st.cfg()
    system = SYSTEM % {"device": cfg["device_name"], "host": cfg["host"], "about": devices.about_text(cfg),
                       "folders": ", ".join(cfg["remote_folders"]),
                       "writes": "ON" if cfg.get("allow_writes") else "OFF (allow_writes is false in its settings file)"}
    if user_text:
        st.messages.append({"role": "user", "content": user_text})
    for _ in range(MAX_TOOL_ROUNDS):
        emit({"type": "status", "text": "Thinking..."})
        msg = ask_model(cfg, [{"role": "system", "content": system}] + st.messages)
        calls = msg.get("tool_calls") or []
        st.messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
                           if calls else {"role": "assistant", "content": msg.get("content") or ""})
        if not calls:
            emit({"type": "answer", "text": clean_answer(msg.get("content"))})
            return
        said = clean_answer(msg.get("content"))
        if said:
            emit({"type": "note", "text": said})
        for c in calls:
            fn = c.get("function", {})
            name, args = fn.get("name"), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            emit({"type": "tool", "name": name, "args": args})
            try:
                if name not in HANDLERS:
                    raise ToolError("There's no tool called %s." % name)
                result = HANDLERS[name](st, emit, args)
            except ToolError as e:
                result = "Error: %s" % e
            except Exception as e:  # keep the chat alive and let the AI explain
                traceback.print_exc()
                result = "Error: %s: %s" % (type(e).__name__, e)
            result = clip(result or "(no output)")
            emit({"type": "tool_result", "name": name, "text": result})
            st.messages.append({"role": "tool", "content": result, "tool_name": name})
    emit({"type": "answer", "text": "I stopped after %d steps so I don't loop forever. Tell me how to continue."
          % MAX_TOOL_ROUNDS})


def do_confirm(st, pid, typed, emit):
    cfg = st.cfg()
    p = st.pending.get(pid)
    if not p:
        raise ToolError("That change is no longer waiting (it was already handled, or the app was restarted).")
    if p["kind"] in ("project_edit", "build", "get_source", "command", "settings"):
        if p.get("needs_typing") and typed.strip() != cfg["device_name"]:
            raise ToolError("Turning writing on needs you to type %s exactly." % cfg["device_name"])
        del st.pending[pid]
        return confirm_on_mac(st, cfg, p, emit, typed)
    if typed.strip() != cfg["device_name"]:
        raise ToolError("You need to type %s exactly to confirm." % cfg["device_name"])
    del st.pending[pid]
    with WRITES:
        entry, out = run_captured(emit, core.put_back, cfg, p["remote"], Path(p["source"]), p["kind"], p["note"],
                                  ask=lambda c, what: print(what))
    if entry and entry.get("verified"):
        result = "Done: change #%d. %s on %s was replaced, and the old file was backed up." % (
            entry["id"], p["remote"], cfg["device_name"])
    elif entry:
        result = "Uploaded as change #%d, but the size on the device doesn't match. Check the file." % entry["id"]
    else:
        result = "The change was NOT made. " + (out.strip().splitlines() or ["Unknown error."])[-1]
    emit({"type": "answer", "text": result})
    st.messages.append({"role": "user", "content": "[I confirmed the %s of %s on the card. Result: %s]"
                                                   % (p["kind"], p["remote"], result)})


def confirm_on_mac(st, cfg, p, emit, typed=""):
    if p["kind"] == "settings":
        try:
            folder = devices.apply_changes(cfg, p["planned"])
        except (OSError, ValueError) as e:
            raise ToolError("Couldn't save the settings: %s" % e)
        result = "Saved: %s. The old settings were backed up to %s." % (
            ", ".join("%s = %s" % (k, devices.show(k, new)) for f, k, old, new in p["planned"]), folder)
        if any(k in ("lan_access", "port") for f, k, o, n in p["planned"]):
            result += " Restart the app for that to take effect."
        APP.registry.status.clear()
        emit({"type": "answer", "text": result})
        st.messages.append({"role": "user", "content": "[I confirmed the settings change. %s]" % result})
        return
    if p["kind"] == "get_source":
        emit({"type": "status", "text": "Downloading %s..." % p["project"]})
        dest = Path(p["source"])
        try:
            projects.clone(p["url"], dest, emit)
            pr = projects.make_project(p["project"], str(dest), p["build"], p["output"], p["device_path"])
            projects.save(st.config_path, pr)
        except projects.ProjectError as e:
            result = str(e)
        else:
            result = "Downloaded %s to %s and saved it as project %s." % (p["url"], dest, pr["name"])
        emit({"type": "answer", "text": result.splitlines()[0]})
        st.messages.append({"role": "user", "content": "[I confirmed the download. Result: %s]" % result})
        chat_turn(st, None, emit)
        return
    pr = project(st, p["project"])
    if p["kind"] == "command":
        emit({"type": "status", "text": "Running %s..." % p["command"][:60]})
        try:
            code, out = projects.run_shell(projects.folder_of(pr), p["command"], emit)
        except (OSError, projects.ProjectError) as e:
            raise ToolError("Couldn't run it: %s" % e)
        result = "The command %s (exit code %d)." % ("worked" if code == 0 else "failed", code)
        emit({"type": "answer", "text": result})
        st.messages.append({"role": "user", "content": "[I confirmed running: %s. %s Last part of its output:\n%s]"
                                                       % (p["command"], result, out[-6000:])})
        chat_turn(st, None, emit)
        return
    if p["kind"] == "project_edit":
        try:
            bk = projects.write_file(cfg, pr, p["remote"], Path(p["source"]).read_text("utf-8"))
        except (OSError, projects.ProjectError) as e:
            raise ToolError("Couldn't save %s: %s" % (p["remote"], e))
        result = "Saved %s in project %s.%s" % (p["remote"], pr["name"],
                                                " The old version was backed up to %s." % bk if bk else " (new file)")
        emit({"type": "answer", "text": result})
        st.messages.append({"role": "user", "content": "[I confirmed the edit of %s. Result: %s]" % (p["remote"], result)})
        return
    emit({"type": "status", "text": "Building %s..." % pr["name"]})
    try:
        code, out, built = projects.run_build(pr, emit)
    except (OSError, projects.ProjectError) as e:
        raise ToolError("Couldn't start the build: %s" % e)
    if code == 0:
        result = "The build of %s worked." % pr["name"]
        if built and built.is_file():
            probs = [x["text"] for x in core.check_bytes(built.name, built.read_bytes()) if x["level"] == "error"]
            result += " It made %s (%s)%s." % (pr["output"], core.human_size(built.stat().st_size),
                                               ", but it fails the damage checks: " + probs[0] if probs else
                                               ", which passes the damage checks")
        elif pr.get("output"):
            result += " But %s wasn't made. Check the project's output setting." % pr["output"]
    else:
        result = "The build of %s failed (exit code %d)." % (pr["name"], code)
    emit({"type": "answer", "text": result})
    st.messages.append({"role": "user", "content": "[I confirmed the build. %s Last part of the build output:\n%s]"
                                                   % (result, out[-6000:])})
    # Let the AI read the output and say what's next (fix errors, or offer to install).
    chat_turn(st, None, emit)


def do_cancel(st, pid, emit):
    p = st.pending.pop(pid, None)
    if p:
        st.messages.append({"role": "user", "content": "[I cancelled the %s of %s. Nothing was written.]"
                                                       % (p["kind"], p["remote"])})
    emit({"type": "answer", "text": "Cancelled. Nothing was written."})


# ----------------------------------------------------------------- the app

class App:
    def __init__(self, config_path, lan):
        self.config_path = config_path
        self.lan = lan
        self.registry = devices.Registry(config_path)
        self.access = webauth.Access(Path(config_path).parent / "access.json")
        self.states = {}
        self.backups = {}       # device name -> the backup running (or last run) for it
        self.lock = threading.Lock()
        self.computer = computer_name()

    def device(self, name):
        """The chat state for one saved device, by name."""
        try:
            path = self.registry.path_for(name)
        except devices.DeviceError as e:
            raise ToolError(str(e))
        with self.lock:
            st = self.states.get(str(path))
            if st is None:
                st = self.states[str(path)] = State(str(path))
        return st


APP = None
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
WEB = core.HERE / "web"


def computer_name():
    try:
        out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().split(".")[0] or "This Mac"


def lan_addresses():
    """Addresses your laptop can use to reach this Mac."""
    names = []
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            names.append(out.stdout.strip() + ".local")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # picks the network card; sends nothing
        names.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return names


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ---- who is asking

    def host_is_local(self):
        port = self.server.server_port
        return self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port)

    def from_this_mac(self):
        # Refuse requests that reach us under another name (a trick some websites use).
        return self.client_address[0] in LOOPBACK and self.host_is_local()

    def session_token(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        m = c.get(webauth.COOKIE)
        return m.value if m else None

    def signed_in(self):
        if self.from_this_mac():
            return True
        return APP.lan and APP.access.session_ok(self.session_token())

    def same_site(self):
        """Writes must come from this app's own page, never from another website."""
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        return not origin or origin == "http://" + (self.headers.get("Host") or "")

    # ---- replies

    def send_body(self, body, ctype, code=200, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200, extra=()):
        self.send_body(json.dumps(obj).encode(), "application/json", code, extra)

    def page(self, name):
        self.send_body((WEB / name).read_bytes(), "text/html; charset=utf-8")

    def start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev):
            try:
                self.wfile.write((json.dumps(ev) + "\n").encode())
                self.wfile.flush()
            except OSError:
                pass
        return emit

    # ---- GET

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not self.from_this_mac() and not APP.lan:
            self.send_error(403)
            return
        if url.path in ("/", "/index.html"):
            self.page("app.html" if self.signed_in() else "login.html")
            return
        if not self.signed_in():
            self.json({"error": "Sign in first."}, 401)
            return
        try:
            self.get_api(url.path, q)
        except (ToolError, fm.FileError, devices.DeviceError, backup.BackupError) as e:
            self.json({"error": str(e)}, 400)
        except (ftplib.Error, OSError, EOFError) as e:
            self.json({"error": "The device stopped answering or refused: %s" % e}, 502)

    def get_api(self, path, q):
        if path == "/api/devices":
            self.json({"devices": APP.registry.summary(force=q.get("force") == "1"), "computer": APP.computer,
                       "lan": APP.lan, "remote": not self.from_this_mac()})
        elif path == "/api/info":
            st = APP.device(q.get("device"))
            cfg = st.cfg()
            self.json({"device": cfg["device_name"], "host": cfg["host"], "model": cfg["ollama_model"],
                       "writes_on": bool(cfg.get("allow_writes")), "folders": cfg["remote_folders"],
                       "firmware": APP.registry.firmware(cfg),
                       "computer": APP.computer, "remote": not self.from_this_mac(),
                       "settings_file": str(Path(st.config_path).relative_to(core.HERE))
                       if Path(st.config_path).is_relative_to(core.HERE) else st.config_path})
        elif path == "/api/history":
            st = APP.device(q.get("device"))
            self.json({"events": st.transcript, "pending": list(st.pending)})
        elif path == "/api/portrait":
            cfg = APP.device(q.get("device")).cfg()
            f = APP.registry.portrait_file(cfg)
            if not f:
                self.send_error(404)
                return
            self.send_body(f.read_bytes(), "image/png" if f.suffix == ".png" else "image/jpeg")
        elif path == "/api/backup/status":
            cfg = APP.device(q.get("device")).cfg()
            job = APP.backups.get(cfg["device_name"])
            self.json({"job": job.status() if job else None, "backups": backup.list_backups(cfg)[:50],
                       "folder": str(backup.backup_root(cfg).parent), "device_folder": str(backup.backup_root(cfg)),
                       "unfinished": bool(backup.unfinished(cfg)), "computer": APP.computer})
        elif path == "/api/files/list":
            if q.get("side") == "mac":
                self.json(fm.list_mac(q.get("path") or str(fm.mac_home()), q.get("hidden") == "1"))
            else:
                self.json(fm.list_device(APP.device(q.get("device")).cfg(), q.get("path") or "/"))
        elif path == "/api/files/download":
            cfg = APP.device(q.get("device")).cfg()
            remote = fm.safe_remote(q.get("path"))
            name = PurePosixPath(remote).name or "download"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(name))
            self.end_headers()
            fm.send_to_browser(cfg, remote, self.wfile.write)
        else:
            self.send_error(404)

    # ---- POST

    def do_POST(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not self.same_site() or (not self.from_this_mac() and not APP.lan):
            self.send_error(403)
            return
        n = int(self.headers.get("Content-Length") or 0)
        if url.path == "/api/files/stage":
            # A file dropped in from Finder: the body is the file itself.
            if not self.signed_in():
                self.json({"error": "Sign in first."}, 401)
                return
            try:
                cfg = APP.device(q.get("device")).cfg()
                self.json({"ref": fm.stage_upload(cfg, q.get("name"), self.rfile, n)})
            except (ToolError, fm.FileError, OSError) as e:
                self.json({"error": str(e)}, 400)
            return
        if n > 8 * 1024 * 1024:
            self.send_error(413)
            return
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            data = {}
        if url.path == "/api/login":
            self.login(data)
            return
        if not self.signed_in():
            self.json({"error": "Sign in first."}, 401)
            return
        try:
            self.post_api(url.path, data)
        except (ToolError, fm.FileError, devices.DeviceError, backup.BackupError) as e:
            self.json({"error": str(e)}, 400)

    def login(self, data):
        ip = self.client_address[0]
        if not APP.lan:
            self.json({"error": "Signing in is only needed when the app is started with --lan."}, 400)
            return
        wait = APP.access.blocked_for(ip)
        if wait:
            self.json({"error": "Too many wrong passwords. Try again in %d seconds." % wait}, 429)
            return
        if not APP.access.check_password(ip, data.get("password")):
            self.json({"error": "That password isn't right."}, 401)
            return
        token = APP.access.new_session()
        self.json({"ok": True}, extra=[("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
                                        % (webauth.COOKIE, token, webauth.SESSION_DAYS * 86400))])

    def post_api(self, path, data):
        if path == "/api/logout":
            APP.access.end_session(self.session_token())
            self.json({"ok": True}, extra=[("Set-Cookie", "%s=; Path=/; Max-Age=0" % webauth.COOKIE)])
        elif path == "/api/identify":
            cfg = APP.device(data.get("device")).cfg()
            report, guess = firmware.identify(cfg)
            if guess:
                APP.registry.set_firmware(cfg, guess)
            self.json({"firmware": guess, "report": report})
        elif path == "/api/devices/add":
            self.add_device(data)
        elif path == "/api/portrait":
            cfg = APP.device(data.get("device")).cfg()
            raw = data.get("data") or ""
            try:
                img = base64.b64decode(raw.split(",", 1)[-1], validate=True)
            except ValueError:
                raise devices.DeviceError("That picture didn't come through. Try another one.")
            APP.registry.set_portrait(cfg, img)
            self.json({"ok": True})
        elif path == "/api/reset":
            st = APP.device(data.get("device"))
            st.messages, st.pending, st.transcript = [], {}, []
            self.json({"ok": True})
        elif path in ("/api/chat", "/api/confirm", "/api/cancel"):
            self.chat(path, data)
        elif path == "/api/files/plan":
            st = APP.device(data.get("device"))
            cfg = st.cfg()
            title, lines, steps = fm.plan(cfg, data.get("op"), data)
            pid = uuid.uuid4().hex[:10]
            st.file_pending = {pid: {"title": title, "steps": steps}}   # only the newest plan can be confirmed
            self.json({"id": pid, "title": title, "lines": lines[:200], "more": max(0, len(lines) - 200),
                       "device": cfg["device_name"], "writes_on": bool(cfg.get("allow_writes"))})
        elif path == "/api/backup/start":
            cfg = APP.device(data.get("device")).cfg()
            with APP.lock:
                job = APP.backups.get(cfg["device_name"])
                if job and job.status()["running"]:
                    raise ToolError("A backup of %s is already running." % cfg["device_name"])
                folder = backup.backup_root(cfg).parent
                if not cfg.get("backup_folder"):
                    folder.mkdir(exist_ok=True)   # the default folder is made for you; a chosen one must exist
                backup.check_folder(folder)
                job = APP.backups[cfg["device_name"]] = backup.Job(cfg)
                job.start()
            self.json({"ok": True})
        elif path == "/api/backup/stop":
            job = APP.backups.get(APP.device(data.get("device")).cfg()["device_name"])
            if job:
                job.stop()
            self.json({"ok": True})
        elif path == "/api/backup/folder":
            st = APP.device(data.get("device"))
            folder = backup.check_folder(data.get("folder"))
            backup.save_folder_setting(st.config_path, folder)
            self.json({"ok": True, "folder": str(folder)})
        elif path == "/api/files/apply":
            self.files_apply(data)
        elif path == "/api/files/copy_to_mac":
            st = APP.device(data.get("device"))
            emit = self.start_stream()
            try:
                n = fm.copy_to_mac(st.cfg(), data.get("items") or [], data.get("mac_folder") or "~", emit)
                emit({"type": "answer", "text": "Copied %d file%s to %s on %s." % (
                    n, "" if n == 1 else "s", data.get("mac_folder"), APP.computer)})
            except (fm.FileError, ToolError) as e:
                emit({"type": "error", "text": str(e)})
            except (ftplib.Error, OSError, EOFError) as e:
                emit({"type": "error", "text": "Copy stopped: %s" % e})
            emit({"type": "done"})
        else:
            self.send_error(404)

    def add_device(self, data):
        try:
            ports = [int(p) for p in re.split(r"[,\s]+", str(data.get("ports") or "21, 5000").strip()) if p]
        except ValueError:
            raise devices.DeviceError("Ports should be numbers like 21, 5000.")
        if not data.get("save_anyway"):
            test = dict(devices.NEW_DEVICE, device_name=data.get("name") or "device", host=(data.get("host") or "").strip(),
                        ports=ports, username=data.get("username") or "anonymous",
                        password=data.get("password") or "anonymous@", timeout_seconds=6)
            if not test["host"]:
                raise devices.DeviceError("Type the device's IP address.")
            dev = core.GD1(test)
            try:
                dev.connect(quiet=True)
                dev.close()
            except SystemExit as e:
                self.json({"error": "I couldn't log in to it: %s" % e.code, "can_save_anyway": True}, 400)
                return
        f = APP.registry.add(data.get("name"), data.get("host"), ports, data.get("username"), data.get("password"),
                             data.get("about"))

        def look():   # name its firmware in the background, so the picture shows it soon
            try:
                cfg = core.load_config(f)
                guess = firmware.identify(cfg)[1]
                if guess:
                    APP.registry.set_firmware(cfg, guess)
            except Exception:
                pass
        threading.Thread(target=look, daemon=True).start()
        self.json({"ok": True, "settings_file": str(f.relative_to(core.HERE))})

    def chat(self, path, data):
        st = APP.device(data.get("device"))
        emit_raw = self.start_stream()

        def emit(ev):
            st.record(ev)
            emit_raw(ev)

        if not BUSY.acquire(blocking=False):
            emit_raw({"type": "error", "text": "I'm still working on a request for %s. Wait for it to finish."
                      % BUSY_WITH[0]})
            emit_raw({"type": "done"})
            return
        BUSY_WITH[0] = st.cfg()["device_name"]
        try:
            if path == "/api/chat":
                text = (data.get("text") or "").strip()
                if text:
                    st.record({"type": "user", "text": text})
                chat_turn(st, text, emit)
            elif path == "/api/confirm":
                do_confirm(st, data.get("id", ""), data.get("typed", ""), emit)
            else:
                do_cancel(st, data.get("id", ""), emit)
        except ToolError as e:
            emit({"type": "error", "text": str(e)})
        except Exception as e:
            traceback.print_exc()
            emit({"type": "error", "text": "Something went wrong: %s" % e})
        finally:
            BUSY.release()
            emit_raw({"type": "done"})

    def files_apply(self, data):
        st = APP.device(data.get("device"))
        cfg = st.cfg()
        emit = self.start_stream()
        p = st.file_pending.get(data.get("id") or "")
        try:
            if not p:
                raise fm.FileError("That change is no longer waiting. Try it again.")
            if (data.get("typed") or "").strip() != cfg["device_name"]:
                raise fm.FileError("You need to type %s exactly to confirm." % cfg["device_name"])
            st.file_pending.pop(data.get("id"), None)
            with WRITES:
                done = fm.apply(cfg, p["steps"], "from the Files screen", emit)
            first, last = (done[0]["id"], done[-1]["id"]) if done else (0, 0)
            result = "Done: %s. %s" % (p["title"], "Logged as change #%d." % first if first == last and first else
                                       "Logged as changes #%d to #%d." % (first, last) if done else "")
            emit({"type": "answer", "text": result})
            st.messages.append({"role": "user", "content": "[On the Files screen I confirmed: %s. Result: %s]"
                                                           % (p["title"], result)})
        except fm.FileError as e:
            emit({"type": "error", "text": str(e)})
        except Exception as e:
            traceback.print_exc()
            emit({"type": "error", "text": "Something went wrong: %s" % e})
        emit({"type": "done"})


class DualStackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        # Accept both IPv4 and IPv6, so names like mac-studio.local always work.
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(lan, port):
    if not lan:
        ThreadingHTTPServer.daemon_threads = True
        return ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        return DualStackServer(("::", port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise
        ThreadingHTTPServer.daemon_threads = True
        return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main():
    global APP
    ap = argparse.ArgumentParser(description="Your devices, an AI chat and a file manager, in your browser.")
    ap.add_argument("--config", default=str(core.DEFAULT_CONFIG), help="settings file (default: config.json)")
    ap.add_argument("--port", type=int, help="web page port (default 8765)")
    ap.add_argument("--lan", action="store_true", help="let other computers on your network (like your laptop) open it")
    ap.add_argument("--set-password", action="store_true", help="choose or change the password for other computers")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()
    try:
        main_cfg = core.load_config(args.config)
    except SystemExit as e:
        sys.exit(e.code)
    lan = args.lan or bool(main_cfg.get("lan_access"))
    port = args.port or int(main_cfg.get("port") or 8765)
    APP = App(args.config, lan)
    if args.set_password:
        APP.access.ask_for_new_password()
        if not args.lan:
            print("Now start the app with: python3 patchbay.py --lan")
            return
    try:
        ollama_url(main_cfg)
    except ToolError as e:
        sys.exit(str(e))
    if lan and not APP.access.has_password():
        if not sys.stdin.isatty():
            sys.exit("Opening the app to your network needs a password. Run: python3 patchbay.py --set-password")
        APP.access.ask_for_new_password()
    try:
        server = make_server(lan, port)
    except OSError:
        sys.exit("Port %d is already in use. Is the app already open? Or try: python3 patchbay.py --port %d"
                 % (port, port + 1))
    url = "http://127.0.0.1:%d" % port
    print("Patchbay is running. On this Mac, open: %s" % url)
    if lan:
        addrs = lan_addresses()
        print("On your laptop (same Wi-Fi or network), open one of these and sign in with your password:")
        for a in addrs or ["http://<this Mac's IP address>:%d" % port]:
            print("    http://%s:%d" % (a, port) if not a.startswith("http") else "    " + a)
        print("If the Mac asks whether Python may accept incoming network connections, click Allow.")
    else:
        print("Only this Mac can open it. To use it from your laptop too, start it with: python3 patchbay.py --lan")
    print("Leave this Terminal window open. Press Ctrl+C here to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
