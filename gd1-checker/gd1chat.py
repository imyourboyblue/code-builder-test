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
import hashlib
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlparse

import tlsfix  # https certificates for python.org Python on a Mac (must come first)
import gd1check as core
import backup
import claude
import collections
import mac_app
import netscan
import securitycheck
import activity
import health
import library
import updater
import selfcheck
import selfedit
import devices
import firmware
import nro
import platforms
import projects
import filemanager as fm
import webauth

TOOL_RESULT_LIMIT = 12000   # characters of tool output the AI sees at once
MAX_TOOL_ROUNDS = 25        # stop runaway tool loops
PLAN_TOOL_ROUNDS = 80       # an approved plan may take many steps
ATTACH_TEXT_LIMIT = 15000   # characters of a dropped-in text file the AI sees straight away
TEXT_EDIT_LIMIT = 2 * 1024 * 1024
NRO_LIMIT = 512 * 1024 * 1024   # .nro programs with built-in files can be big


# ----------------------------------------------------------------- state

BUSY = threading.Lock()     # one chat job at a time (scans print to stdout, which is shared)
BUSY_WITH = [""]
WRITES = threading.Lock()   # one write to a device at a time, so the history list stays in order
TRANSCRIPT_LIMIT = 400
STOP = threading.Event()    # set by the Stop button; checked while the AI writes and between steps


class Stopped(Exception):
    pass


def check_stop():
    if STOP.is_set():
        raise Stopped()


class State:
    """One device's chat: what was said, and changes waiting for you to confirm."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.messages = []      # the conversation, as Ollama sees it
        self.pending = {}       # chat changes waiting for you to confirm
        self.file_pending = {}  # Files-screen changes waiting for you to confirm
        self.transcript = []    # what the page shows, so a reload or another computer sees the same chat
        self.plan = None        # an approved plan: {"steps": [...], "writes": [device paths]}
        self.chat_id = None
        self.title = ""
        self.loaded = False

    # ---- saved conversations (one JSON file per chat, on the Mac)

    def chats_dir(self):
        cfg = self.cfg()
        d = (Path(os.path.expanduser(cfg["data_folder"])) / "_home" if getattr(self, "home", False)
             else core.data_dir(cfg)) / "chats"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ensure_loaded(self):
        """The first time a chat is opened, carry on the newest saved conversation."""
        if self.loaded:
            return
        self.loaded = True
        try:
            chats = self.list_chats()
            if chats:
                self.open_chat(chats[0]["id"])
        except (ToolError, OSError, ValueError):
            pass

    def save_chat(self):
        if not self.transcript:
            return
        if not self.chat_id:
            self.chat_id = core.now_stamp()
        if not self.title:
            first = next((e.get("text") for e in self.transcript if e.get("type") == "user"), "") or "Chat"
            self.title = re.sub(r"\s+", " ", first).strip()[:70]
        f = self.chats_dir() / (self.chat_id + ".json")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": self.chat_id, "title": self.title, "updated": time.strftime("%Y-%m-%d %H:%M"),
                                   "messages": self.messages[-300:], "transcript": self.transcript}))
        tmp.replace(f)

    def list_chats(self):
        out = []
        for f in sorted(self.chats_dir().glob("*.json"), reverse=True):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            out.append({"id": d.get("id") or f.stem, "title": d.get("title") or "Chat", "updated": d.get("updated"),
                        "count": sum(1 for e in d.get("transcript", []) if e.get("type") == "user")})
        return out

    def open_chat(self, chat_id):
        if not re.match(r"^[\w.-]{1,60}$", chat_id or ""):
            raise ToolError("No such chat.")
        f = self.chats_dir() / (chat_id + ".json")
        if not f.exists():
            raise ToolError("That chat isn't saved any more.")
        d = json.loads(f.read_text())
        self.chat_id, self.title = d.get("id") or chat_id, d.get("title") or ""
        self.messages, self.transcript = d.get("messages", []), d.get("transcript", [])
        self.pending, self.plan = {}, None

    def new_chat(self):
        self.save_chat()
        self.messages, self.pending, self.transcript, self.plan = [], {}, [], None
        self.chat_id, self.title = None, ""

    def delete_chat(self, chat_id):
        if not re.match(r"^[\w.-]{1,60}$", chat_id or ""):
            raise ToolError("No such chat.")
        f = self.chats_dir() / (chat_id + ".json")
        if f.exists():
            f.unlink()
        if chat_id == self.chat_id:
            self.messages, self.pending, self.transcript, self.plan = [], {}, [], None
            self.chat_id, self.title = None, ""

    def record(self, ev):
        t = ev.get("type")
        if t == "error" or (t == "tool_result" and failed(ev.get("text") or "")):
            note_problem("%s: %s" % (getattr(self, "name", "") or "chat", ev.get("summary") or ev.get("text") or ""))
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


ATTACH_LIMIT = 512 * 1024 * 1024
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def attach_dir(st):
    d = st.chats_dir().parent / "chat-files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_attachment(st, name, rfile, n):
    """A file dropped into the chat: saved on the Mac next to the chats."""
    name = re.sub(r"[^\w .()+-]", "_", Path(name or "file").name).strip() or "file"
    if n > ATTACH_LIMIT:
        raise ToolError("That file is bigger than %s." % core.human_size(ATTACH_LIMIT))
    ref = "%s-%s" % (core.now_stamp(), name[:80])
    f = attach_dir(st) / ref
    left = n
    with open(f, "wb") as out:
        while left > 0:
            chunk = rfile.read(min(left, 1024 * 1024))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
    return {"ref": ref, "name": name, "size": n - left}


def attachment_name(ref):
    """The file's own name, without the date Patchbay put in front of it."""
    return re.sub(r"^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d-", "", Path(ref or "").name)


def find_attachment(st, ref_or_name):
    v = Path(ref_or_name or "").name
    d = attach_dir(st)
    if v and (d / v).is_file():
        return d / v
    hits = sorted(d.glob("*-" + v)) if v and "*" not in v and "?" not in v else []
    if hits:
        return hits[-1]
    raise ToolError("There's no attached file called %s." % ref_or_name)


def attachment_message(st, text, refs):
    """The user's message plus what's in the files they dropped in (pictures go to the AI as pictures)."""
    parts, images = [text or "(see the attached files)"], []
    for ref in refs[:10]:
        f = find_attachment(st, ref)
        name = attachment_name(f.name)
        data = f.read_bytes()
        if f.suffix.lower() in IMAGE_TYPES and len(data) <= 20 * 1024 * 1024:
            images.append(base64.b64encode(data).decode())
            parts.append("[Picture attached: %s]" % name)
        elif f.suffix.lower() == ".nro" or nro.looks_like_nro(data):
            info, probs, desc = nro.describe(data)
            parts.append("[Attached .nro %s]\n%s\nChecks: %s" % (name, desc, "; ".join(x["text"] for x in probs) or
                                                                 "no problems found"))
        elif core.looks_like_text(data[:4096]):
            body = data.decode("utf-8", "replace")
            parts.append("[Attached file %s, %d lines]\n%s%s" % (
                name, body.count("\n") + 1, body[:ATTACH_TEXT_LIMIT],
                "\n... (more: read_attachment %s)" % f.name if len(body) > ATTACH_TEXT_LIMIT else ""))
        else:
            probs = core.check_bytes(name, data)
            parts.append("[Attached binary file %s, %s] Checks: %s. First bytes: %s" % (
                name, core.human_size(len(data)), "; ".join(x["text"] for x in probs) or "none", data[:48].hex(" ")))
    msg = {"role": "user", "content": "\n\n".join(parts)}
    if images:
        msg["images"] = images
    return msg


HOME = "__home__"


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

AUTO_KINDS = ("get_source", "project_edit", "build")
# Setup commands that only touch the project folder may run without a card when automatic Mac steps are on.
SAFE_PART = re.compile(r"^(\S*/)?(git (submodule|pull|fetch|checkout|switch|status|log|tag)|cmake|[a-z0-9_-]+-cmake|make|"
                       r"ninja|meson|mkdir|ls|which|pwd)\b[^;|&<>`$\\]*$")


class _SafeCommand:
    """Simple setup commands (cmake, make, git pull...) joined with && count as safe; anything else needs a card."""
    def match(self, cmd):
        parts = [x.strip() for x in cmd.split("&&")]
        return all(x and SAFE_PART.match(x) for x in parts)


SAFE_COMMAND = _SafeCommand()


def runs_by_itself(cfg, kind, extra):
    if not cfg.get("auto_mac_steps", True):
        return False
    if kind in AUTO_KINDS:
        return True
    cmd = (extra or {}).get("command") or ""
    return kind == "command" and bool(SAFE_COMMAND.match(cmd.strip())) and ".." not in cmd


def add_pending(st, emit, kind, cfg, remote, source, note, summary, diff=None, on_mac=False, extra_fields=None):
    if on_mac:
        before = getattr(st, "failed_steps", {}).get(step_key(dict({"kind": kind, "remote": remote}, **(extra_fields or {}))))
        if before:
            raise ToolError("This exact step already failed: %s Don't try it again unchanged and don't call it "
                            "temporary. Read the error, fix the cause (a different command, a missing package, a code "
                            "change), or call ask_claude if you're stuck." % before)
    if on_mac and runs_by_itself(cfg, kind, extra_fields):
        # Automatic Mac step (setting auto_mac_steps): project files are backed up first, downloads go to a new folder.
        p = dict({"kind": kind, "remote": remote, "source": str(source), "note": note}, **(extra_fields or {}))
        emit({"type": "note", "text": "On %s: %s" % (APP.computer if APP else "the Mac", summary)})
        short, detail = run_mac_step(st, cfg, p, emit)
        if kind not in ("command", "build"):     # those already show their output box
            emit({"type": "note", "text": short})
        return "Done automatically (no card needed). " + detail
    if on_mac:
        # Changes on the Mac (project files, builds): pressing Confirm is enough.
        pid = uuid.uuid4().hex[:8]
        st.pending[pid] = dict({"kind": kind, "remote": remote, "source": str(source), "note": note}, **(extra_fields or {}))
        emit({"type": "confirm", "id": pid, "title": summary, "remote": remote, "diff": diff, "on_mac": True, "kind": kind,
              "typing": bool((extra_fields or {}).get("needs_typing")),
              "device": cfg["device_name"], "writes_on": True, "computer": APP.computer if APP else "the Mac"})
        return ("A confirmation card is now showing on the page. Nothing has happened yet. The user must press Confirm. "
                "Tell the user what it does and ask them to confirm it on the card.")
    if st.plan and remote in st.plan.get("writes", []) and cfg.get("allow_writes") and kind != "helper":
        # Covered by the plan the user approved (they typed the device name for it). Still backed up first.
        emit({"type": "note", "text": "Approved in the plan: %s" % summary})
        entry, result = device_write(cfg, {"kind": kind, "remote": remote, "source": str(source), "note": note}, emit)
        emit({"type": "note", "text": result})
        return "Done without a card (approved in the plan). " + result
    if not cfg.get("allow_writes"):
        extra = (" Writing is off for this device right now. The card has a box (ticked) that turns it on when "
                 "the user confirms, so they don't need to change any settings. Don't send them to config.json.")
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
    cfg = st.cfg()
    if not (name or "").strip() or str(name).strip().lower() in ("none", "null"):
        # No name given: use the last project worked on, or the only one there is.
        known = projects.all_projects(cfg)
        last = getattr(st, "last_project", None)
        if last and any(p["name"] == last for p in known):
            name = last
        elif len(known) == 1:
            name = known[0]["name"]
        else:
            raise ToolError("Say which project: %s." % (", ".join(p["name"] for p in known) or
                                                         "there are none yet (use propose_get_source or set_project)"))
    try:
        pr = projects.get(cfg, name)
    except projects.ProjectError as e:
        raise ToolError(str(e))
    st.last_project = pr["name"]
    return pr


def pe(fn, *args):
    try:
        return fn(*args)
    except projects.ProjectError as e:
        raise ToolError(str(e))


def is_switch(cfg):
    words = " ".join(str(x) for x in (cfg.get("device_name"), cfg.get("about"),
                                      APP.registry.firmware(cfg) if APP else "")).lower()
    return "switch" in words or "atmosph" in words


def checked_build(st, cfg, pr, emit, save=False):
    """Switch projects: use the known recipe (ftpd), drop toolchain files that don't exist, and always configure
    with devkitPro's Switch wrapper. Plain cmake builds for the Mac and can't find Switch libraries."""
    if not is_switch(cfg):
        return pr
    build = pr.get("build") or ""
    if Path(os.path.expanduser(pr.get("folder") or "")).name.lower() == "ftpd" and \
            ("classic" in (pr.get("output") or "").lower() or "FTPD_CLASSIC" in build) and \
            build != firmware.FTPD_CLASSIC_BUILD:
        pr = dict(pr, build=firmware.FTPD_CLASSIC_BUILD, output="build-classic/ftpd-classic.nro")
        msg = "I reset ftpd classic's build to its checked recipe (from ftpd's own build files)."
    else:
        fixed, changes = projects.fix_switch_build(build)
        if isinstance(changes, str):
            raise ToolError(changes)
        if not changes:
            return pr
        pr = dict(pr, build=fixed)
        msg = "I fixed the build command: " + "; ".join(changes) + "."
    if save:
        projects.save(st.config_path, pr)
    emit({"type": "note", "text": msg})
    return pr


@tool("set_project",
      "Save (or update) a source code project on the Mac for this device: where its folder is, the command that "
      "builds it, the file the build makes, and where that file goes on the device. Only edits the settings on the Mac.",
      {"name": dict(S, description="Short project name"), "folder": dict(S, description="Project folder on the Mac, e.g. ~/code/mygame"),
       "build": dict(S, description="Build command run in that folder, usually: make"),
       "output": dict(S, description="The built file, relative to the folder, e.g. mygame.nro"),
       "device_path": dict(S, description="Where the built file goes on the device, e.g. /switch/mygame/mygame.nro")},
      ["name", "folder"])
def t_set_project(st, emit, a):
    if a.get("build"):
        tmp = checked_build(st, st.cfg(), {"build": a["build"], "folder": a.get("folder"),
                                           "output": a.get("output")}, emit)
        a = dict(a, build=tmp["build"], output=tmp.get("output") or a.get("output"))
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
        rows.append("  %s = %s   (%s)" % (k, devices.show(k, cfg.get(k, True if k == "auto_mac_steps" else None)), what))
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


def test_device(a, timeout=8):
    """A temporary device from the add form/tool, logged in once to check it works. Raises SystemExit on failure."""
    proto = (a.get("protocol") or "ftp").lower()
    ports = a.get("ports") or devices.DEFAULT_PORTS.get(proto, [21, 5000])
    test = dict(devices.NEW_DEVICE, device_name=a.get("name") or "device", host=(a.get("host") or "").strip(),
                ports=ports if isinstance(ports, list) else [ports], timeout_seconds=timeout, protocol=proto,
                username=a.get("username") or ("anonymous" if proto == "ftp" else ""),
                password=a.get("password") or ("anonymous@" if proto == "ftp" else ""),
                share=(a.get("share") or "").strip(), folder=os.path.expanduser((a.get("folder") or "").strip()))
    if proto == "folder":
        test["host"], test["ports"] = "this Mac", []
    dev = core.GD1(test)
    dev.connect(quiet=True)
    welcome = dev.ftp.getwelcome() if dev.ftp else ""
    dev.close()
    return test, welcome


@tool("add_device",
      "Save a new device (it then gets its own picture on the home screen). Logs in first to check it works. "
      "protocol: ftp (default), sftp (SSH; also lets Patchbay run commands on it), smb (Windows/Mac file sharing; "
      "needs share, the shared folder's name), or folder (a folder already on this Mac, like /Volumes/Media). Only "
      "changes settings on the Mac.",
      {"name": dict(S, description="Short name like GD2"), "host": S,
       "protocol": {"type": "string", "enum": ["ftp", "sftp", "smb", "folder"]},
       "ports": {"type": "array", "items": I, "description": "e.g. [21] for FTP, [22] for SFTP, [445] for SMB"},
       "username": S, "password": S, "share": dict(S, description="SMB only: the shared folder's name"),
       "folder": dict(S, description="folder only: the path on this Mac"),
       "about": dict(S, description="Optional: what the device is")}, ["name"])
def t_add_device(st, emit, a):
    try:
        test, welcome = test_device(a)
    except SystemExit as e:
        raise ToolError("Couldn't connect, so I didn't save it: %s" % e.code)
    try:
        f = APP.registry.add(a.get("name"), test["host"], test["ports"], a.get("username"), a.get("password"),
                             a.get("about"), test["protocol"], test.get("share"), test.get("folder"))
    except devices.DeviceError as e:
        raise ToolError(str(e))
    return "Saved %s (settings in %s, connected with %s). It's on the home screen now; open it there to work on it." % (
        a.get("name"), f.name, welcome or test["protocol"].upper())


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
        raise ToolError("The search didn't work: %s" % tlsfix.explain(e))


def clean_items(items):
    """Picked files/folders for a backup of just those: [{"path": "/switch/ftpd", "kind": "dir"}]."""
    if not items:
        return None
    out = []
    for i in items:
        path = norm(i.get("path") if isinstance(i, dict) else i)
        kind = (i.get("kind") if isinstance(i, dict) else None) or "dir"
        if path and path not in [o["path"] for o in out]:
            out.append({"path": path, "kind": "file" if kind == "file" else "dir"})
    return out or None


def start_backup(cfg, items=None, label=""):
    """Start a backup (everything, or just the picked items) in the background. Only reads the device."""
    with APP.lock:
        job = APP.backups.get(cfg["device_name"])
        if job and job.status()["running"]:
            raise ToolError("A backup of %s is already running. Wait for it, or stop it on the Backups tab."
                            % cfg["device_name"])
        folder = backup.backup_root(cfg).parent
        if not cfg.get("backup_folder"):
            folder.mkdir(exist_ok=True)   # the default folder is made for you; a chosen one must exist
        try:
            backup.check_folder(folder)
        except backup.BackupError as e:
            raise ToolError(str(e))
        job = APP.backups[cfg["device_name"]] = backup.Job(cfg, clean_items(items), label)
        job.start()
    return job


@tool("backup",
      "Back up files from the device to the user's backup folder on the Mac: give paths for just those files or "
      "folders, or nothing for a full backup. Only reads the device, so no card is needed. Runs in the background; "
      "progress shows on the Backups tab.",
      {"paths": {"type": "array", "items": S, "description": "Files or folders on the device; leave out for everything"},
       "label": dict(S, description="Optional name, e.g. 'before the Atmosphère 1.8 update'")})
def t_backup(st, emit, a):
    cfg = st.cfg()
    paths = a.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    items = []
    if paths:
        dev = core.GD1(cfg)

        def kinds():
            dev.connect(quiet=True)
            try:
                for p in paths:
                    p = norm(p)
                    parent, name = str(PurePosixPath(p).parent), PurePosixPath(p).name
                    entry = next((e for e in dev._list(parent) if e[0] == name), None) if p != "/" else ("/", "dir")
                    if not entry:
                        raise ToolError("%s isn't on %s." % (p, cfg["device_name"]))
                    items.append({"path": p, "kind": entry[1]})
            finally:
                dev.close()
        run_captured(emit, kinds)
        if len(items) != len(paths):
            raise ToolError("Couldn't look at those paths on %s." % cfg["device_name"])
    start_backup(cfg, items or None, a.get("label") or "")
    return ("Started backing up %s to %s. It runs in the background; the Backups tab shows progress." % (
        ", ".join(i["path"] for i in items) if items else "everything on " + cfg["device_name"],
        backup.backup_root(cfg)))


@tool("platform_notes",
      "What Patchbay knows about a kind of device that runs homebrew (switch, 3ds, wiiu, wii, vita, psp, ps4, "
      "linux-handheld, or any platform saved before): where apps live, file types, build tools, firmware. If it "
      "knows nothing, research it on the web and save it with save_platform_notes.",
      {"platform": dict(S, description="e.g. switch, vita, or the name of the user's own device")}, ["platform"])
def t_platform(st, emit, a):
    return platforms.describe(st.cfg(), a.get("platform"))


@tool("save_platform_notes",
      "Save facts you researched about a kind of device (generic facts only, no user files), so next time you "
      "already know. Only writes a notes file on the Mac; no card needed.",
      {"platform": S, "notes": {"type": "object", "description": "e.g. {\"apps\": \"where apps live\", "
                                "\"files\": \"app file types\", \"toolchain\": \"how to build\", "
                                "\"install\": \"how to install\", \"firmware\": \"how to tell the version\"}"}},
      ["platform", "notes"])
def t_save_platform(st, emit, a):
    notes = a.get("notes")
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except ValueError:
            notes = {"note": notes}
    try:
        k, f = platforms.save(st.cfg(), a.get("platform"), notes)
    except ValueError as e:
        raise ToolError(str(e))
    return "Saved notes for %s (%s). platform_notes will show them from now on." % (k, f)


@tool("propose_plan",
      "For a job with several steps (more than 3), first show the user a short numbered plan to approve once. "
      "After approval you carry it out without stopping to ask. List any device files the plan will write in "
      "device_writes: approving the plan (the user types the device name) covers those writes, each backed up "
      "first. Does NOT run anything by itself.",
      {"title": S, "steps": {"type": "array", "items": S, "description": "Plain steps, in order"},
       "device_writes": {"type": "array", "items": S, "description": "Device paths the plan will change, if any"}},
      ["title", "steps"])
def t_plan(st, emit, a):
    cfg = st.cfg()
    steps = [str(x).strip() for x in (a.get("steps") or []) if str(x).strip()][:30]
    if not steps:
        raise ToolError("Give the steps of the plan.")
    writes = [norm(x) for x in (a.get("device_writes") or []) if str(x).strip()][:50]
    text = "\n".join("%d. %s" % (i + 1, x) for i, x in enumerate(steps))
    if writes:
        text += "\n\nChanges to %s (each backed up first):\n" % cfg["device_name"] + "\n".join("- " + w for w in writes)
    return add_pending(st, emit, "plan", cfg, "plan", "", a.get("title") or "",
                       "Plan: %s" % (a.get("title") or "what I'll do"), text, on_mac=True,
                       extra_fields={"steps": steps, "writes": writes, "needs_typing": bool(writes)})


@tool("read_attachment",
      "Read more of a file the user dropped into the chat (text files page by page).",
      {"name": dict(S, description="The attached file's name"), "start": dict(I, description="First line, from 1")},
      ["name"])
def t_attachment(st, emit, a):
    f = find_attachment(st, a.get("name"))
    data = f.read_bytes()
    if f.suffix.lower() == ".nro" or nro.looks_like_nro(data):
        return nro.describe(data)[2] + "\nChecks: " + "; ".join(x["text"] for x in nro.inspect(data)[1])
    if not core.looks_like_text(data[:4096]):
        probs = core.check_bytes(f.name, data)
        return "%s is a binary file (%s). First bytes: %s\nChecks: %s" % (
            f.name, core.human_size(len(data)), data[:64].hex(" "), "; ".join(x["text"] for x in probs) or "none")
    lines = data.decode("utf-8", "replace").splitlines()
    start = max(1, int(a.get("start") or 1))
    end = min(len(lines), start + 399)
    return "%s (%d lines), lines %d to %d:\n%s" % (f.name, len(lines), start, end, "\n".join(lines[start - 1:end]))


@tool("device_firmware",
      "Find out the Switch's firmware and Atmosphère versions from the SD card (the helper app's file, crash and "
      "error reports), plus hekate, the homebrew in /switch and the background programs. Read-only.")
def t_firmware(st, emit, a):
    emit({"type": "status", "text": "Reading version clues on %s..." % st.cfg()["device_name"]})
    rep = firmware.device_report(st.cfg())
    fw = re.search(r"(?m)^FIRMWARE: (\d+\.\d+\.\d+)", rep)
    ams = re.search(r"(?m)^ATMOSPHERE: (\d+\.\d+\.\d+)", rep)
    if fw:
        APP.registry.set_firmware(st.cfg(), "Switch %s%s" % (fw.group(1), ", Atmosphère " + ams.group(1) if ams else ""))
    return rep


HELPER_NRO = Path(__file__).resolve().parent / "helper" / "patchbay-sysinfo" / "prebuilt" / "patchbay-sysinfo.nro"


@tool("setup_firmware_helper",
      "Put Patchbay's tiny helper app on the Switch (/switch/patchbay-sysinfo). When the user opens it once from "
      "the Homebrew Menu, it saves the exact firmware and Atmosphère versions to the SD card, and Patchbay picks "
      "them up by itself. Does NOT write: shows a card the user confirms.")
def t_helper(st, emit, a):
    cfg = st.cfg()
    if not HELPER_NRO.is_file():
        raise ToolError("The helper app file is missing from Patchbay's folder (%s). Run git pull." % HELPER_NRO)
    return add_pending(st, emit, "helper", cfg, firmware.HELPER_NRO, HELPER_NRO, "Patchbay helper app",
                       "Put the Patchbay helper app on %s (%s, %s). Then open it once from the Homebrew Menu."
                       % (cfg["device_name"], firmware.HELPER_NRO, core.human_size(HELPER_NRO.stat().st_size)))


@tool("find_homebrew",
      "Find the homebrew the user means from their words (e.g. 'FTPD light', 'chiaki', 'sys-clk'): searches the "
      ".nro apps in /switch and the background programs in /atmosphere/contents, and says where its source code "
      "probably lives. Read-only. Use this instead of asking the user which file.",
      {"name": dict(S, description="The user's words for the program")}, ["name"])
def t_find_hb(st, emit, a):
    emit({"type": "status", "text": "Looking for %s on %s..." % (a.get("name"), st.cfg()["device_name"])})
    try:
        return firmware.find_homebrew(st.cfg(), a.get("name"))
    except ValueError as e:
        raise ToolError(str(e))


@tool("prepare_homebrew_update",
      "First step of updating or changing a homebrew program on ANY device. In one go: finds the program on the "
      "device from the user's words, works out the kind of device and its firmware, gives what Patchbay knows "
      "about that platform, checks newest releases (Switch), checks the Mac's build tools, and lists matching "
      "source projects. Read-only.",
      {"name": dict(S, description="The user's words for the program, e.g. 'FTPD light'")}, ["name"])
def t_prepare(st, emit, a):
    cfg, name = st.cfg(), (a.get("name") or "").strip()
    parts = []
    emit({"type": "status", "text": "Looking for %s on %s..." % (name, cfg["device_name"])})
    try:
        found = firmware.find_homebrew(cfg, name)
    except ValueError as e:
        found = "Couldn't search the device: %s" % e
    parts.append("== The program ==\n" + found)
    switch = "/atmosphere/" in found or "Background program" in found or "App /switch/" in found
    if not switch:
        dev = core.GD1(cfg)

        def tops():
            dev.connect(quiet=True)
            try:
                return firmware._names(dev, "/") or []
            finally:
                dev.close()
        top, _ = run_captured(emit, tops)
        switch = "atmosphere" in [t.lower() for t in top or []]
    if switch:
        emit({"type": "status", "text": "Reading firmware versions..."})
        rep = t_firmware(st, emit, {})
        parts.append("== The console ==\n" + "\n".join(l for l in rep.splitlines()
                                                        if not l.startswith(("Homebrew in", "Top-level"))))
        parts.append("== Platform ==\n" + platforms.describe(cfg, "switch"))
        repos = ["Atmosphere-NX/Atmosphere", "switchbrew/libnx"] + re.findall(r"latest_release (\S+)\)", found)[:1]
        for repo in repos:
            emit({"type": "status", "text": "Checking the newest %s..." % repo})
            try:
                parts.append("== Newest %s ==\n%s" % (repo, firmware.latest_release(repo)[:1800]))
            except (OSError, ValueError) as e:
                parts.append("== %s == couldn't check (%s)" % (repo, e))
    else:
        emit({"type": "status", "text": "Working out what %s runs..." % cfg["device_name"]})
        report, guess = firmware.identify(cfg)
        if guess:
            APP.registry.set_firmware(cfg, guess)
        parts.append("== The device ==\n" + report[:2500])
        plat = platforms.guess(top or [])
        parts.append("== Platform ==\n" + (platforms.describe(cfg, plat) if plat else
                     "Patchbay doesn't know this kind of device yet. Research it (generic questions only): where its "
                     "homebrew lives, the file format, the build toolchain, how to install. Then save_platform_notes."))
    parts.append("== Build tools on the Mac ==\n" + t_tools(st, emit, {}))
    mine = [p for p in projects.all_projects(cfg) if firmware._match(name, p["name"]) or firmware._match(p["name"], name)]
    parts.append("== Source projects already on the Mac ==\n" + ("\n".join(
        "%s in %s (build: %s, output: %s, goes to: %s)" % (p["name"], p["folder"], p["build"], p.get("output") or "?",
                                                         p.get("device_path") or "?") for p in mine)
        or "None yet: get it with propose_get_source (a known source address above, or find it with web_search)."))
    parts.append("NEXT, without asking the user anything: if a newer release of the program already exists, say so "
                 "and offer it. Otherwise get the source (propose_get_source), read its README / build files, "
                 "set_project with the right output file and device_path (where it was found above), "
                 "propose_build, fix errors, and propose_install.")
    return clip("\n\n".join(parts))


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
      "Check what's installed on the Mac for building homebrew: devkitPro (Switch, 3DS, Wii, Wii U) with package "
      "versions, VitaSDK, PSPSDK, OpenOrbis, compilers, cmake, git. Read-only.")
def t_tools(st, emit, a):
    return projects.toolchain_report() + "\n\nOther build tools:\n" + platforms.toolchains()


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
      "user confirms. To install build packages, use propose_install_packages instead.",
      {"project": S, "command": S, "reason": dict(S, description="One short sentence on why")}, ["project", "command"])
def t_command(st, emit, a):
    cfg, pr = st.cfg(), project(st, a.get("project"))
    cmd = (a.get("command") or "").strip()
    if not cmd:
        raise ToolError("Give the command to run.")
    if re.search(r"(^|[;&|]\s*)sudo\b", cmd):
        raise ToolError("Don't use sudo here. To install or update build packages use propose_install_packages "
                        "(devkitPro or Homebrew): the Mac shows its own password box. Never ask the user to type "
                        "commands in Terminal.")
    if re.match(r"^(\S*/)?(brew|dkp-pacman|pacman)\s+(install|upgrade|-S)", cmd):
        raise ToolError("Use propose_install_packages to install packages (it handles the password).")
    root = pe(projects.folder_of, pr)
    return add_pending(st, emit, "command", cfg, pr["name"], root, a.get("reason") or "",
                       "Run a command in project %s: %s" % (pr["name"], a.get("reason") or ""),
                       "In folder: %s\nRun: %s" % (root, cmd), on_mac=True,
                       extra_fields={"project": pr["name"], "command": cmd})


@tool("propose_device_command",
      "Suggest running one command ON THE DEVICE itself over SSH (only for devices added with SFTP/SSH), e.g. "
      "restarting an app or service, checking disk space (df -h), reading a log, listing processes. Does NOT run: "
      "shows the exact command on a card; the user types the device name to run it. Use read-only commands when "
      "they're enough.",
      {"command": S, "reason": dict(S, description="One short sentence on why")}, ["command"])
def t_device_command(st, emit, a):
    cfg = st.cfg()
    if cfg.get("protocol") != "sftp":
        raise ToolError("%s is reached over %s, which can't run commands. Only devices added with SFTP/SSH can."
                        % (cfg["device_name"], cfg.get("protocol", "ftp").upper()))
    cmd = (a.get("command") or "").strip()
    if not cmd or len(cmd) > 2000:
        raise ToolError("Give one command to run.")
    pid = uuid.uuid4().hex[:8]
    st.pending[pid] = {"kind": "device_command", "remote": cfg["device_name"], "source": "", "command": cmd,
                       "note": a.get("reason") or "", "needs_typing": True}
    emit({"type": "confirm", "id": pid, "title": "Run a command on %s: %s" % (cfg["device_name"], a.get("reason") or cmd[:60]),
          "remote": cfg["device_name"], "diff": cmd, "on_mac": True, "kind": "device_command", "typing": True,
          "device": cfg["device_name"], "writes_on": True, "computer": APP.computer if APP else "the Mac"})
    return ("A confirmation card is now showing. Nothing has run yet. The user must type the device name and press the "
            "button. Tell them what the command does.")


def run_device_command(st, cfg, p, emit):
    emit({"type": "status", "text": "Running on %s: %s" % (cfg["device_name"], p["command"][:80])})
    dev = core.GD1(cfg)
    try:
        code, out = dev.run_command(p["command"], timeout=int(cfg.get("command_timeout", 120)))
    except SystemExit as e:
        code, out = -1, "Couldn't connect: %s" % e.code
    except (ftplib.Error, OSError) as e:
        code, out = -1, str(e)
    finally:
        dev.close()
    result = "Ran on %s (exit code %s)." % (cfg["device_name"], code)
    show_output(emit, result, code, out, p["command"])
    entry = {"time": core.now_stamp(), "action": "command", "remote": p["command"], "note": p.get("note") or "",
             "exit_code": code}
    try:
        hist_file = core.data_dir(cfg) / "commands.json"
        hist = json.loads(hist_file.read_text()) if hist_file.exists() else []
        hist.append(entry)
        hist_file.write_text(json.dumps(hist[-500:], indent=2))
    except (OSError, ValueError):
        pass
    st.messages.append({"role": "user", "content": "[I confirmed the command. %s Output (end):\n%s]"
                                                   % (result, output_tail(out)[-3000:])})
    chat_turn(st, None, emit)


@tool("propose_install_packages",
      "Install or update build packages on the Mac: devkitPro packages (switch-dev, switch-zlib, 3ds-dev...; installs "
      "devkitPro itself first if it's missing) or Homebrew packages (cmake, pkg-config...). Shows a card; after the "
      "user confirms, the Mac shows its own password box for devkitPro. Never tell the user to run these in Terminal.",
      {"manager": dict(S, description="devkitpro or homebrew"),
       "packages": {"type": "array", "items": S, "description": "Package names, e.g. [\"switch-dev\", \"switch-zlib\"]"},
       "update_all": {"type": "boolean", "description": "Also update everything already installed (devkitPro -Syu)"},
       "reason": dict(S, description="One short sentence on why")}, ["manager"])
def t_install_packages(st, emit, a):
    cfg = st.cfg()
    manager = (a.get("manager") or "devkitpro").strip().lower().replace("devkitpro-pacman", "devkitpro")
    manager = "homebrew" if manager in ("brew", "homebrew") else manager
    pkgs = a.get("packages") or []
    if isinstance(pkgs, str):
        pkgs = pkgs.replace(",", " ").split()
    try:
        cmds, admin, words = projects.install_plan(manager, pkgs, bool(a.get("update_all")))
    except projects.ProjectError as e:
        raise ToolError(str(e))
    note = a.get("reason") or ""
    detail = words + ("\n\nAfter you press Confirm, your Mac asks for its own password (the one you log in with). "
                      "Patchbay never sees it." if admin else "")
    return add_pending(st, emit, "install_packages", cfg, words, Path.home(), note, words, detail, on_mac=True,
                       extra_fields={"manager": manager, "packages": pkgs, "update_all": bool(a.get("update_all"))})


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
        raise ToolError("Couldn't read that page: %s" % tlsfix.explain(e))
    text = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", re.sub(r"&amp;", "&", text)))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return clip(text)


# ----------------------------------------------------------------- the home screen's chat

class HomeState(State):
    """The chat on the home screen: about all devices, not one."""
    home = True


HOME_TOOLS, HOME_HANDLERS = [], {}


def home_tool(name, description, params=None, required=()):
    def deco(fn):
        HOME_TOOLS.append({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": params or {}, "required": list(required)}}})
        HOME_HANDLERS[name] = fn
        return fn
    return deco


def for_device(name, a):
    st = APP.device(a.get("device") or "")
    if getattr(st, "home", False):
        raise ToolError("Say which device.")
    return st


@home_tool("list_devices", "List every saved device: address, whether it's on the network now, its firmware, "
           "and whether writing to it is on.")
def h_list(st, emit, a):
    rows = APP.registry.summary()
    if not rows:
        return "No devices saved yet."
    return "\n".join("%s at %s: %s; firmware: %s; writing %s" % (
        d["name"], d["host"], "on the network" if d["online"] else "NOT found on the network",
        d.get("firmware") or "unknown", "ON" if d["writes_on"] else "off") for d in rows)


@home_tool("open_device", "Open a device's own screen (its chat, files and backups) on the page. Use this when the "
           "user wants to work on one device: check it, read or fix files, scan, back up, or update its homebrew.",
           {"device": S, "tab": dict(S, description="chat, files or backups (default chat)")}, ["device"])
def h_open(st, emit, a):
    d = for_device(st, a)
    name = d.cfg()["device_name"]
    tab = a.get("tab") if a.get("tab") in ("chat", "files", "backups") else "chat"
    emit({"type": "open", "device": name, "tab": tab})
    return "Opened %s's %s screen for the user." % (name, tab)


@home_tool("scan_network",
            "Find the devices on the user's own home network (TVs, consoles, computers, printers...): their address, "
            "name, maker, what they probably are, and the normal ways to connect to each. Only looks; changes "
            "nothing. Takes about 20 seconds.")
def h_scan_network(st, emit, a):
    job = getattr(APP, "netscan", None)
    if not (job and job.finished and time.time() - job.finished < 600 and not job.error):
        job = APP.netscan = NetScanJob()
        while job.finished is None:
            check_stop()
            emit({"type": "status", "text": job.note})
            time.sleep(1)
    if job.error:
        raise ToolError("The scan didn't work: %s" % job.error)
    return scan_summary(job.results) + ("\n\nTo add one with an FTP server: add_device. For any other kind, explain "
                                        "the normal way to connect (from 'Ways in'), and if needed look up that model's "
                                        "documented access (developer mode, network settings) with web_search, "
                                        "using generic words only. Never try passwords or try to break in.")


SECURITY = {}
LIBRARY_JOBS = {}


@home_tool("security_check",
           "Check one of the user's OWN devices for security problems: open doors, software versions with known "
           "problems (CVEs), old encryption, risky settings like Telnet or FTP without a password. Only looks: no "
           "exploits, no password guessing. Only call it after the user has said in this chat that the device is "
           "theirs (or they're allowed to check it); set owner_confirmed only then. Takes up to a few minutes.",
           {"ip": S, "owner_confirmed": {"type": "boolean"}}, ["ip", "owner_confirmed"])
def h_security_check(st, emit, a):
    if a.get("owner_confirmed") is not True:
        return ("Ask the user to confirm the device is theirs (or that they're allowed to check it) before running the "
                "security check.")
    try:
        ip = securitycheck.check_target(str(a.get("ip") or ""))
    except securitycheck.NotAllowed as e:
        raise ToolError(str(e))
    job = SECURITY.get(ip)
    if not (job and job.finished is None):
        kind = next((d.get("kind") for d in (last_scan().get("results") or []) if d.get("ip") == ip), None)
        job = SECURITY[ip] = securitycheck.SecurityCheck(ip, kind)
    while job.finished is None:
        if STOP.is_set():
            job.stop.set()
        check_stop()
        emit({"type": "status", "text": job.note})
        time.sleep(1)
    if job.error:
        raise ToolError("The security check didn't work: %s" % job.error)
    return (securitycheck.summary(job.report) + "\n\nExplain the findings in plain words, worst first, and offer to "
            "walk the user through each fix (device settings, firmware update, turning a service off). Never try to "
            "exploit a problem or guess passwords.")


@home_tool("check_address", "See whether anything answers at an IP address and port, and what it says (FTP, SSH, "
           "web...). Read-only.", {"host": S, "port": I}, ["host", "port"])
def h_check(st, emit, a):
    return devices.probe((a.get("host") or "").strip(), int(a.get("port") or 21))


@home_tool("add_device", "Save a new device after checking it answers (FTP, SFTP/SSH, SMB file sharing, or a folder "
           "on this Mac). It then gets a picture on the home screen.", {"name": dict(S, description="Short name like GD2"),
           "host": S, "protocol": {"type": "string", "enum": ["ftp", "sftp", "smb", "folder"]},
           "ports": {"type": "array", "items": I}, "username": S, "password": S, "share": S, "folder": S, "about": S},
           ["name"])
def h_add(st, emit, a):
    result = HANDLERS["add_device"](st, emit, a)
    emit({"type": "devices_changed"})
    return result


@home_tool("identify_firmware", "Work out which firmware / operating system a device runs (read-only) and show it on "
           "its picture.", {"device": S}, ["device"])
def h_identify(st, emit, a):
    result = HANDLERS["identify_firmware"](for_device(st, a), emit, {})
    emit({"type": "devices_changed"})
    return result


@home_tool("save_firmware_name", "Save a short firmware name for a device once you're sure.", {"device": S, "name": S},
           ["device", "name"])
def h_save_fw(st, emit, a):
    result = HANDLERS["save_firmware_name"](for_device(st, a), emit, a)
    emit({"type": "devices_changed"})
    return result


@home_tool("show_settings", "Show every setting of one device, plus the shared app settings.", {"device": S}, ["device"])
def h_show(st, emit, a):
    return HANDLERS["show_settings"](for_device(st, a), emit, {})


@home_tool("propose_settings", "Suggest changing settings of one device (or shared ones), e.g. {\"host\": "
           "\"192.168.50.203\", \"ports\": [8080]}. Shows a card the user confirms; settings are backed up first.",
           {"device": S, "changes": {"type": "object"}, "reason": S}, ["device", "changes"])
def h_settings(st, emit, a):
    target = for_device(st, a)
    shown = []
    result = HANDLERS["propose_settings"](target, shown.append, a)
    # The card belongs to this chat, so move it here and remember which device it's for.
    for ev in shown:
        if ev.get("type") == "confirm":
            p = target.pending.pop(ev["id"])
            p["device"] = target.cfg()["device_name"]
            st.pending[ev["id"]] = p
        emit(ev)
    return result


@home_tool("test_connection", "Check that the Mac can reach one device over FTP.", {"device": S}, ["device"])
def h_test(st, emit, a):
    return HANDLERS["test_connection"](for_device(st, a), emit, {})


# ---- Patchbay changing its own code (only from the home chat; every change needs the user's Confirm)

STARTED_AT = time.time()


def se(fn, *args):
    try:
        return fn(*args)
    except selfedit.SelfEditError as e:
        raise ToolError(str(e))


@home_tool("patchbay_files", "List Patchbay's OWN program files (its Python code, its web page web/app.html, the Mac "
           "app source). Use this when the user wants Patchbay itself changed: a new button, a fix, a new feature.")
def h_self_files(st, emit, a):
    return selfedit.files()


@home_tool("patchbay_read", "Read one of Patchbay's own files with line numbers (read-only). Read before you change.",
           {"path": dict(S, description="e.g. gd1chat.py or web/app.html"), "start_line": I, "max_lines": I}, ["path"])
def h_self_read(st, emit, a):
    return se(selfedit.read, a.get("path"), a.get("start_line") or 1, a.get("max_lines") or 300)


@home_tool("patchbay_search", "Search all of Patchbay's own files for a word or phrase (read-only). Good for finding "
           "where a button, message or function is.", {"text": S}, ["text"])
def h_self_search(st, emit, a):
    return se(selfedit.search, a.get("text"))


@home_tool("propose_patchbay_change",
           "Suggest a change to one of Patchbay's OWN files. Does NOT write: shows the exact difference on a card the "
           "user confirms. The new file is checked first (Python must compile, the page's scripts must parse) and the "
           "old one is backed up, with Undo in the Activity log. Give find + replace (find must appear exactly once; "
           "keep it small) or new_content for a new file. Always patchbay_read the place first. One file per card; for "
           "a change across files, suggest each one.",
           {"path": S, "find": S, "replace": S, "new_content": dict(S, description="Whole file, only for a new file"),
            "reason": dict(S, description="One short plain sentence on what this does for the user")},
           ["path", "reason"])
def h_self_change(st, emit, a):
    pl = se(selfedit.plan, a.get("path"), a.get("find"), a.get("replace"), a.get("new_content"))
    pid = uuid.uuid4().hex[:8]
    staged = selfedit.BACKUPS / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / (pid + ".txt")).write_text(pl["new"], "utf-8")
    cur = selfedit.HERE / pl["path"]
    old_sha = hashlib.sha1(cur.read_bytes()).hexdigest() if cur.exists() else None
    word = selfedit.CONFIRM_WORD if pl["safety"] else ""
    st.pending[pid] = {"kind": "self_edit", "remote": pl["path"], "source": str(staged / (pid + ".txt")),
                       "note": a.get("reason") or "", "old_sha": old_sha, "word": word}
    page_only = pl["path"].startswith("web/")
    emit({"type": "confirm", "id": pid, "kind": "self_edit", "on_mac": True, "self": True, "remote": pl["path"],
          "title": "Change Patchbay itself: %s" % (a.get("reason") or pl["path"]),
          "diff": clip(pl["diff"], 60000), "safety": pl["safety"], "word": word, "page_only": page_only,
          "stats": "%s: %d line%s added, %d removed" % (pl["path"], pl["added"], "" if pl["added"] == 1 else "s",
                                                         pl["removed"]),
          "computer": APP.computer if APP else "the Mac", "device": "Patchbay", "writes_on": True})
    return ("A card showing the exact change is on the page. Nothing has been changed yet; the user must press "
            "Confirm%s. Tell them in one or two plain sentences what it does.%s" % (
                " and type Patchbay (it touches a safety rule: %s; explain that plainly)" % ", ".join(pl["safety"])
                if word else "",
                " After it's saved they only need to reload the page." if page_only else
                " After it's saved, Patchbay needs a restart (propose_patchbay_restart) for it to take effect."))


@home_tool("propose_patchbay_restart", "Suggest restarting Patchbay so changes to its own Python code take effect. "
           "Shows a card; the page reconnects by itself. Not needed for changes to web/app.html (a reload is enough).")
def h_self_restart(st, emit, a):
    pid = uuid.uuid4().hex[:8]
    st.pending[pid] = {"kind": "self_restart", "remote": "Patchbay", "source": "", "note": ""}
    changed = selfedit.pending_restart(STARTED_AT)
    emit({"type": "confirm", "id": pid, "kind": "self_restart", "on_mac": True, "self": True, "remote": "Patchbay",
          "title": "Restart Patchbay to use the changes",
          "diff": "\n".join("#%s %s: %s" % (h["id"], ", ".join(f["path"] for f in h["files"]), h["note"])
                            for h in changed) or "(no saved changes since it started)",
          "computer": APP.computer if APP else "the Mac", "device": "Patchbay", "writes_on": True})
    return "A restart card is showing. Nothing has happened yet; the user presses Restart on it."


def confirm_self(st, p, emit):
    if p["kind"] == "self_restart":
        emit({"type": "answer", "text": "Restarting Patchbay now. The page reconnects by itself in a few seconds. If "
                                        "it doesn't come back, Patchbay puts the last change back by itself."})
        emit({"type": "restart"})
        updater.restart_soon(1.5)
        return
    path = selfedit.HERE / p["remote"]
    now_sha = hashlib.sha1(path.read_bytes()).hexdigest() if path.exists() else None
    if now_sha != p.get("old_sha"):
        msg = "%s changed since this card was made, so I didn't save it. Ask again and I'll redo the change." % p["remote"]
        emit({"type": "answer", "text": msg})
        st.messages.append({"role": "user", "content": "[The change wasn't saved: %s]" % msg})
        return
    try:
        entry = selfedit.apply([(p["remote"], Path(p["source"]).read_text("utf-8"))], p.get("note") or "")
    except (selfedit.SelfEditError, OSError) as e:
        emit({"type": "answer", "text": "I couldn't save that change: %s" % e})
        st.messages.append({"role": "user", "content": "[Saving the Patchbay change failed: %s]" % e})
        return
    page = p["remote"].startswith("web/")
    msg = "Saved change #%s to %s (the old version is backed up; Undo is in 🕘 Activity). %s" % (
        entry["id"], p["remote"], "Reload the page to see it." if page else "Restart Patchbay to use it.")
    emit({"type": "answer", "text": msg})
    if page:
        emit({"type": "reload_hint"})
    st.messages.append({"role": "user", "content": "[I confirmed the Patchbay change. %s]" % msg})


for _n in ("check_build_tools", "latest_release", "web_search", "read_web_page"):
    _spec = next(t for t in TOOLS if t["function"]["name"] == _n)
    HOME_TOOLS.append(_spec)
    HOME_HANDLERS[_n] = HANDLERS[_n]


def home_system():
    return """You are Patchbay, an assistant running fully on the user's own Mac (%s). This is the home screen chat, \
about all of the user's devices rather than one. The user is not a programmer: answer in short, plain sentences.

Use tools instead of guessing.
- list_devices shows every saved device and whether it's on the network.
- For "find/scan devices on my network" (or "what's on my network", "find my TV"): scan_network. Then for each \
device say in plain words what it is and the normal way to connect (its "Ways in"). If the user wants to use one \
that has no open door yet, look up that model's documented way to turn on access (developer mode, network/remote \
settings, file sharing) with web_search using generic words, and walk them through it step by step. Connect only \
with the user's own login. Never guess or try passwords, and never try to break in.
- For "is my X secure", "check my devices for security problems" and the like: security_check, only after the user \
confirms the device is theirs. Never exploit a problem or try passwords, even if asked; help them fix it instead.
- When the user wants to connect Patchbay to a found device or "see its files": the goal is a way for PATCHBAY to \
read the device's files (FTP, SFTP/SSH, SMB file sharing, or a documented developer mode / debug bridge such as ADB \
on Android/Google/Fire TV). Look up that model's documented options with web_search (generic words, no addresses). \
Don't give screen mirroring, casting, AirPlay or remote-control app steps: they don't help. If the platform has no \
file access at all (for example a Roku: its developer mode only installs your own apps; a PlayStation), say that \
plainly in one or two sentences and say what it does allow. When there is a way, walk them through turning it on, \
then add it with add_device (FTP) or explain the next step.
- For "search <address> port <n>" or "find/connect <address>": check_address. If an FTP server answers and it isn't \
a saved device, offer add_device (ask for a short name if they didn't give one). If they mean a saved device moved, \
propose_settings for its host/ports, then test_connection.
- Settings: show_settings, then propose_settings. That only shows a card; never say it's changed until you're told.
- Work on one device (checking, files, scans, backups, fixing, updating homebrew): open_device, and tell the user \
they can carry on in that device's chat.
- identify_firmware tells what a device runs; web_search only with generic text, never the user's files or code.
- Changing PATCHBAY ITSELF (a new button, a fix to Patchbay, "make Patchbay do X"): find the place with \
patchbay_search, read it with patchbay_read, then propose_patchbay_change with a small find/replace that matches the \
existing style. web/app.html is the page (HTML, CSS and JavaScript); gd1chat.py is the server and the chat tools. \
Explain each change in plain words. Never weaken a safety rule (typed device names, backups, local-only AI, what goes \
to the cloud, scanning limits) unless the user clearly asks, and then say plainly what it changes. After Python \
changes, offer propose_patchbay_restart; page changes only need a page reload.""" % (
        APP.computer if APP else "the Mac")


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
- If the device is reached over SFTP/SSH, you can also suggest running a command on it with propose_device_command \
(restart an app or service, check disk space, read a log). The user types the device name on the card to run it. \
Prefer read-only commands; explain what a command does first. Other devices (FTP, SMB, folder) can't run commands.
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
- Never ask the user for something a tool can find: the firmware and Atmosphère versions (device_firmware), \
which file a program is (find_homebrew: the user's own words like "FTPD light" are enough), its version \
(read_file), or its source code address (find_homebrew, then web_search). If something truly can't be found, \
pick the most likely answer, say which you picked, and carry on.
- If the firmware version isn't on the SD card, offer setup_firmware_helper once (a tiny app the user opens one \
time from the Homebrew Menu). Don't wait for it: building with the newest libnx works for current firmware.
- Homebrew works the same way on any device (Switch, 3DS, Wii, Wii U, Vita, PSP, PS4, Linux handhelds, the user's \
own devices): platform_notes says what Patchbay knows about that kind of device. When it doesn't know something \
(where apps live, the file format, the toolchain, how to install or tell the firmware), research it yourself with \
web_search and read_web_page using generic questions only (never the user's files, code, names or addresses), \
then save_platform_notes so it's known next time. Don't ask the user to research.
- When the user asks to update or change a homebrew program (for example for newer firmware), do the whole job \
yourself, saying in one short line what you're doing at each step:
  1. prepare_homebrew_update with the user's words for the program. It finds the program, the versions, the newest \
releases, the build tools and any source project. If a newer release of the program already supports the \
firmware, say so first.
  2. If build tools or packages are missing or old (check_build_tools), install them yourself with \
propose_install_packages (devkitPro packages like switch-dev and switch-zlib, or Homebrew ones like cmake). The \
Mac shows its own password box after the user confirms. Never ask the user to type commands in Terminal, and \
never tell them to export variables or edit shell files: Patchbay sets up devkitPro and Homebrew paths itself. \
Console builds link the console's own libraries from devkitPro (switch-zlib, switch-curl, switch-libzstd...), \
never the Mac's (brew zlib won't work).
  3. Get the source with propose_get_source (or use the existing project).
  4. Use a build recipe when find_homebrew gives one. Otherwise read the real build files (CMakeLists.txt, \
Makefile, and the CI workflow in .github/workflows, which shows the exact commands that work); READMEs are often \
out of date. Never invent tool names, paths or scripts: only use ones you saw in a file or tool result. For a \
devkitPro CMake project on Switch the build is: aarch64-none-elf-cmake -S . -B build <options> && cmake --build \
build -j8 (3DS: arm-none-eabi-cmake). For a Makefile project it's make. Put the whole build command, the built \
file and device_path (where find_homebrew found it on the device; a background program's file is \
/atmosphere/contents/<id>/exefs.nsp) in set_project, then propose_build.
  5. propose_build. Read the errors; fix the code with propose_project_edit (libnx renames and changed functions \
are the usual cause on Switch; look up the library's changelog); build again until it builds cleanly.
  6. propose_install (the device's current file is backed up first). Then tell the user how to test it (restart \
the console for a background program).
  Downloading, project edits, builds and simple setup commands on the Mac may run by themselves (the tool result \
says "Done automatically"); then just keep going. Writing to the device always needs the user's card.
- For a job with more than 3 steps, first use propose_plan: a short numbered plan, plus the device files it will \
change. The user approves it once; then do every step without stopping to ask (approved device writes go through \
without another card).
- The user can drop files into the chat (logs, pictures, .nro files); their contents come with the message. \
read_attachment reads more of a long one.
- If something fails, read the output (it shows the error lines), say what failed in one line, and fix it \
yourself: install a missing package, correct the command, or edit the code. Exit code 127 means a program isn't \
installed. A tool result starting "A confirmation card" means nothing has run yet: wait for the user's card. \
Never call an error temporary or retry the exact same step: something has to change first.

Scanning settings: folders %(folders)s. Writing to the device is currently %(writes)s."""


def ollama_url(cfg):
    url = cfg["ollama_url"].rstrip("/")
    host = re.sub(r"^https?://", "", url).split("/")[0].rsplit(":", 1)[0].strip("[]")
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise ToolError("ollama_url must point at this Mac (localhost) so your files never leave it.")
    return url


class ThinkFilter:
    """Drops the model's <think>...</think> notes from the live text, even when a tag is split over two pieces."""

    def __init__(self):
        self.buf, self.inside = "", False

    def feed(self, piece):
        self.buf += piece
        out = ""
        while True:
            tag = "</think>" if self.inside else "<think>"
            i = self.buf.find(tag)
            if i >= 0:
                if not self.inside:
                    out += self.buf[:i]
                self.buf, self.inside = self.buf[i + len(tag):], not self.inside
                continue
            keep = next((k for k in range(min(len(tag) - 1, len(self.buf)), 0, -1) if tag.startswith(self.buf[-k:])), 0)
            if not self.inside:
                out += self.buf[:len(self.buf) - keep]
            self.buf = self.buf[len(self.buf) - keep:]
            return out


# ----------------------------------------------------------------- Claude (cloud help, with the user's own key)

def claude_known():
    """Personal values that are swapped for stand-ins before anything goes to Claude."""
    known = []
    try:
        for _, c in APP.registry.all():
            known += [("device", c.get("device_name")), ("ip", c.get("host")), ("secret", c.get("password"))
                      if c.get("password") not in ("anonymous@", "", None) else ("x", ""),
                      ("user", c.get("username")) if c.get("username") not in ("anonymous", "ftp", None) else ("x", "")]
    except Exception:
        traceback.print_exc()
    names = {APP.computer, socket.gethostname().split(".")[0]}
    for n in names:
        known.append(("mac", n))
        m = re.match(r"^([A-Z][a-z]+)(?:['’]s|s)?[\s-]+(?:Mac|MacBook|iMac)", n or "")
        if m:
            known.append(("name", m.group(1)))
    return [(k, v) for k, v in known if v]


def claude_scrubber():
    return APP.claude.scrubber(claude_known())


def claude_cost(emit):
    if emit and APP:
        p = APP.claude.public()
        emit({"type": "claude_cost", "session": p["session_cost"], "month": p["month_cost"],
              "cap": p["monthly_cap"]})


def claude_question(st, question, details, purpose):
    """The exact text that would go to Claude for one question (already scrubbed), and the scrubber."""
    sc = claude_scrubber()
    parts = ["Purpose: %s" % ("write a step-by-step plan for this job" if purpose == "plan"
                              else "the local AI is stuck; say what to do next")]
    last_user = next((m.get("content") for m in reversed(st.messages) if m.get("role") == "user"
                      and not str(m.get("content", "")).startswith("[")), "")
    if last_user:
        parts.append("What the user asked:\n" + str(last_user)[:2000])
    parts.append("Question:\n" + (question or "").strip())
    if details:
        parts.append("Details:\n" + str(details)[:12000])
    recent = [m for m in st.messages if m.get("role") == "tool"
              and str(m.get("content"))[-2000:] not in str(details or "")][-3:]
    if recent:
        parts.append("Recent step results (newest last):\n" + "\n---\n".join(
            "[%s]\n%s" % (m.get("tool_name"), str(m.get("content"))[-3500:]) for m in recent))
    return sc.scrub("\n\n".join(parts)), sc


def send_question(st, emit, text, sc, why):
    emit({"type": "status", "text": "Asking Claude..."})
    try:
        answer = APP.claude.ask(text, sc, why)
    finally:
        APP.claude.save_aliases(sc)
        claude_cost(emit)
    return answer


def offer_question(st, emit, cfg, question, details, purpose):
    """Ask Claude now (automatic sending on) or show a card with exactly what will be sent."""
    if APP.claude.mode() == "off":
        raise ToolError("Asking Claude is off (or there's no API key yet). Carry on yourself.")
    try:
        APP.claude.check_cap()
    except claude.ClaudeError as e:
        raise ToolError(str(e))
    text, sc = claude_question(st, question, details, purpose)
    if APP.claude.settings().get("auto_send"):
        try:
            answer = send_question(st, emit, text, sc, purpose)
        except claude.ClaudeError as e:
            raise ToolError(str(e))
        emit({"type": "note", "text": "**Claude's advice:**\n\n" + answer})
        return "Claude's answer (follow it):\n" + answer
    APP.claude.save_aliases(sc)
    return add_pending(st, emit, "ask_claude", cfg, "Claude", Path.home(), purpose,
                       "Ask Claude for %s" % ("a plan" if purpose == "plan" else "help with the failing step"),
                       text, on_mac=True, extra_fields={"text": text, "purpose": purpose})


@tool("ask_claude",
      "Ask Claude, a much stronger cloud AI, instead of guessing. Use it when a step has failed twice, when you don't "
      "know how to do something, or (purpose plan) to get a step-by-step plan for a big job after gathering the "
      "facts. Personal details are removed automatically; the user sees what is sent. Never include file contents "
      "the user didn't ask to share, except build errors and logs.",
      {"question": dict(S, description="What you need to know, in general terms"),
       "details": dict(S, description="Error lines, build output, versions, what you already tried"),
       "purpose": dict(S, description="'stuck' (default) or 'plan'")}, ["question"])
def t_ask_claude(st, emit, a):
    purpose = "plan" if (a.get("purpose") or "").strip().lower() == "plan" else "stuck"
    return offer_question(st, emit, st.cfg(), a.get("question"), a.get("details"), purpose)


def claude_tools(tools):
    seen, uniq = set(), []
    for t in tools:          # the same tool twice would be refused by Claude
        n = t["function"]["name"]
        if n not in seen:
            seen.add(n)
            uniq.append(t)
    tools = uniq
    mode = APP.claude.mode() if APP else "off"
    if mode in ("off", "always"):
        return [t for t in tools if t["function"]["name"] != "ask_claude"]
    return tools


def claude_system(system):
    mode = APP.claude.mode() if APP else "off"
    if mode == "plan":
        return system + ("\n\nClaude, a stronger cloud AI, plans big jobs. For a job with more than 3 steps (like "
                         "updating or rebuilding an app): first gather the facts (prepare_homebrew_update, or read the "
                         "files), then call ask_claude with purpose plan and those facts, then propose_plan using "
                         "Claude's steps and carry them out yourself. If a step fails twice, call ask_claude instead "
                         "of guessing. Simple things (browsing, questions, backups) never need Claude.")
    if mode == "stuck":
        return system + ("\n\nIf a step fails twice, or you don't know how to do something, call ask_claude "
                         "(a stronger cloud AI) instead of guessing. Simple things never need it.")
    return system


# Steps that move a job forward (as opposed to only reading).
PROGRESS_TOOLS = {"propose_get_source", "set_project", "propose_project_edit", "propose_build", "propose_install",
                  "propose_command", "propose_install_packages", "propose_plan", "backup", "propose_edit",
                  "propose_restore", "ask_claude", "save_platform_notes", "setup_firmware_helper",
                  "propose_device_command"}
ASK_CLAUDE_CHOICE = "Ask Claude for help with this"


def claude_ready():
    return bool(APP) and APP.claude.mode() in ("stuck", "plan") and APP.claude.has_key()


def ask_user(st, emit, why, tried):
    """Stop instead of going round in circles: say what was tried and let the user pick what's next."""
    steps = "\n".join("- " + t for t in tried[-8:])
    options = []
    if APP and APP.claude.has_key() and APP.claude.mode() != "always":
        options.append({"label": "Ask Claude", "send": ASK_CLAUDE_CHOICE})
    options += [{"label": "Try another way", "send": "Try a different way. Don't repeat what already failed."},
                {"label": "Stop here", "send": "Stop here for now and sum up what's wrong in two sentences."}]
    text = "I'm stopping to check with you. %s\n\nWhat I tried:\n%s" % (why, steps)
    if not (APP and APP.claude.has_key()):
        text += "\n\nTip: with a Claude API key (☁ Claude button), I could ask Claude instead of guessing."
    emit({"type": "choices", "text": text, "options": options})
    st.messages.append({"role": "assistant", "content": text})
    return None


def ask_claude_choice(st, emit):
    """The user picked "Ask Claude" after Patchbay stopped."""
    name, args, result = getattr(st, "last_stuck", None) or ("", {}, "")
    question = ("The local AI got stuck on this job and stopped. Last failing step: %s %s. What is wrong, and "
                "exactly what should be done next?" % (name, json.dumps(args)[:600]) if name else
                "The local AI got stuck on this job. What should be done next?")
    st.messages.append({"role": "user", "content": "[I asked for Claude's help.]"})
    try:
        out = offer_question(st, emit, st.cfg(), question, (result or "")[-6000:], "stuck")
    except ToolError as e:
        emit({"type": "answer", "text": str(e)})
        return
    if out.startswith("A confirmation card"):
        emit({"type": "answer", "text": "Check what will be sent on the card, then press Send to Claude."})
        return
    st.messages.append({"role": "user", "content": "[" + out + "]"})
    chat_turn(st, None, emit)


def stuck_help(st, emit, cfg, name, args, result):
    """The same step failed twice: ask Claude. Returns True when a card is now waiting for the user."""
    question = "This step failed twice: %s %s. What is wrong, and exactly what should be done next?" % (
        name, json.dumps(args)[:600])
    try:
        out = offer_question(st, emit, cfg, question, result[-6000:], "stuck")
    except ToolError as e:
        st.messages.append({"role": "user", "content": "[Patchbay couldn't ask Claude: %s]" % e})
        return False
    if out.startswith("A confirmation card"):
        emit({"type": "answer", "text": "That step failed twice, so I'd like to ask Claude for help instead of "
                                        "guessing. The card shows exactly what will be sent; personal details "
                                        "are already removed."})
        return True
    st.messages.append({"role": "user", "content": "[That step failed twice, so Patchbay asked Claude. " + out + "]"})
    return False


TERMINAL_CMD = re.compile(r"(?im)^\s*(\$\s*)?(sudo|brew|export|cmake|make|git|dkp-pacman|pacman|cd|source|"
                          r"aarch64-none-elf-\S+|arm-none-eabi-\S+|ninja|meson|chmod|ln|rm|mkdir|curl|echo)\b")
TERMINAL_WORDS = re.compile(r"(?i)\b(run|type|paste|enter|copy)\b[^.\n]{0,80}\b(terminal|command line|shell)\b|"
                            r"\bin (the )?terminal\b")
TERMINAL_NUDGE = ("[Patchbay: Don't ask the user to run commands in Terminal or set environment variables. Do it "
                  "yourself now with your tools: propose_install_packages for missing packages (devkitPro ones like "
                  "switch-zlib, or Homebrew ones like cmake), propose_command for project commands, set_project and "
                  "propose_build for builds. Console builds use devkitPro libraries (switch-zlib, switch-curl...), "
                  "never the Mac's own (brew zlib). Builds already have devkitPro and Homebrew on the PATH.]")


def sends_to_terminal(text):
    t = clean_answer(text)
    if re.search(r"(?i)brew\.sh|ollama pull|python3 patchbay\.py|git pull", t) and not re.search(
            r"(?im)^\s*(sudo|export|cmake|make|brew install|dkp-pacman)\b", t):
        return False      # the few things only the user can do (install Homebrew, get a model, restart Patchbay)
    blocks = re.findall(r"```[^\n]*\n(.*?)```", t, re.S)
    if any(TERMINAL_CMD.search(b) for b in blocks):
        return True
    return bool(TERMINAL_WORDS.search(t)) and bool(TERMINAL_CMD.search(t) or "`" in t)


def failed(result):
    r = result or ""
    return r.startswith("Error:") or bool(re.search(r"\b(failed|did not work)\b.*\(exit code [1-9]", r[:600]))


def ask_model(cfg, messages, tools=None, emit=None, model=None):
    """Ask the AI. Its words are passed to emit as they arrive ("delta" events), so the page shows them live."""
    if APP and APP.claude.mode() == "always" and not model:
        try:
            emit and emit({"type": "status", "text": "Asking Claude..."})
            sc = claude_scrubber()
            system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
            msg = APP.claude.chat(system, [m for m in messages if m.get("role") != "system"], tools or TOOLS, sc)
            APP.claude.save_aliases(sc)
            claude_cost(emit)
            return msg
        except claude.ClaudeError as e:
            claude_cost(emit)
            if emit:
                emit({"type": "note", "text": "I couldn't use Claude (%s) Using the AI on %s for this answer instead."
                                              % (str(e).rstrip(".") + ".", APP.computer)})
    messages = [{k: v for k, v in m.items() if k != "claude"} if "claude" in m else m for m in messages]
    model = model or cfg["ollama_model"]
    body = json.dumps({"model": model, "stream": True, "messages": messages, "tools": tools or TOOLS,
                       "options": {"num_ctx": int(cfg.get("chat_context", 32768))}}).encode()
    req = urllib.request.Request(ollama_url(cfg) + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            content, calls, think = "", [], ThinkFilter()
            for line in r:
                check_stop()
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise ToolError("Ollama said: %s" % chunk["error"])
                m = chunk.get("message") or {}
                piece = m.get("content") or ""
                if piece:
                    content += piece
                    shown = think.feed(piece)
                    if shown and emit:
                        emit({"type": "delta", "text": shown})
                calls.extend(m.get("tool_calls") or [])
                if chunk.get("done"):
                    break
            return {"content": content, "tool_calls": calls}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if "not found" in detail:
            raise ToolError("Ollama doesn't have the model %s yet. In Terminal run: ollama pull %s" % (model, model))
        if any(m.get("images") for m in messages):
            # This model can't look at pictures: try again without them.
            plain = [dict({k: v for k, v in m.items() if k != "images"},
                          content=(m.get("content") or "") + "\n[A picture was attached, but this AI model can't see "
                                                             "pictures. A model with vision, e.g. qwen2.5vl, can.]")
                     if m.get("images") else m for m in messages]
            return ask_model(cfg, plain, tools, emit, model)
        raise ToolError("Ollama said: %s" % detail)
    except OSError as e:
        raise ToolError("Couldn't reach Ollama (%s). Is the Ollama app running?" % e)


def clean_answer(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def chat_turn(st, user_text, emit, user_msg=None):
    cfg = st.cfg()
    if getattr(st, "home", False):
        return chat_loop(st, user_text, emit, cfg, home_system(), claude_tools(HOME_TOOLS), HOME_HANDLERS, user_msg)
    system = SYSTEM % {"device": cfg["device_name"], "host": cfg["host"], "about": devices.about_text(cfg),
                       "folders": ", ".join(cfg["remote_folders"]),
                       "writes": "ON" if cfg.get("allow_writes") else "OFF (allow_writes is false in its settings file)"}
    return chat_loop(st, user_text, emit, cfg, claude_system(system), claude_tools(TOOLS), HANDLERS, user_msg)


def chat_loop(st, user_text, emit, cfg, system, tools, handlers, user_msg=None):
    if user_msg:
        st.messages.append(user_msg)
    elif user_text:
        st.messages.append({"role": "user", "content": user_text})
    if st.plan:
        system += ("\n\nThe user approved this plan. Carry it out now, step by step, without stopping to ask. "
                   "Say in one short line what each step does.\n" + "\n".join(
                       "%d. %s" % (i + 1, x) for i, x in enumerate(st.plan["steps"])))
    fails, nudges, tried, idle = {}, 0, [], 0
    idle_limit = 15 if st.plan else 8
    for _ in range(PLAN_TOOL_ROUNDS if st.plan else MAX_TOOL_ROUNDS):
        check_stop()
        emit({"type": "status", "text": "Thinking..."})
        msg = ask_model(cfg, [{"role": "system", "content": system}] + st.messages, tools, emit)
        calls = msg.get("tool_calls") or []
        st.messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
                           if calls else {"role": "assistant", "content": msg.get("content") or ""})
        if msg.get("claude"):
            st.messages[-1]["claude"] = msg["claude"]
        if not calls and sends_to_terminal(msg.get("content")) and nudges < 2:
            # Never hand the user commands to type: do it with the tools (install cards, project commands).
            nudges += 1
            st.messages.append({"role": "user", "content": TERMINAL_NUDGE})
            emit({"type": "status", "text": "Doing that step myself instead..."})
            continue
        if not calls:
            emit({"type": "answer", "text": clean_answer(msg.get("content"))})
            return
        said = clean_answer(msg.get("content"))
        if said:
            emit({"type": "note", "text": said})
        for c in calls:
            check_stop()
            fn = c.get("function", {})
            name, args = fn.get("name"), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            emit({"type": "tool", "name": name, "args": args, "text": step_doing(name, args)})
            try:
                if name not in handlers:
                    raise ToolError("There's no tool called %s." % name)
                result = handlers[name](st, emit, args)
            except ToolError as e:
                result = "Error: %s" % e
            except Stopped:
                raise
            except Exception as e:  # keep the chat alive and let the AI explain
                traceback.print_exc()
                result = "Error: %s: %s" % (type(e).__name__, e)
            result = clip(result or "(no output)")
            summary = step_summary(name, args, result)
            emit({"type": "tool_result", "name": name, "text": result, "summary": summary})
            st.messages.append({"role": "tool", "content": result, "tool_name": name})
            tried.append(summary)
            bad = failed(result)
            idle = 0 if (not bad and name in PROGRESS_TOOLS) else idle + 1
            if bad and name != "ask_claude":
                # The same goal (e.g. building this project), even if the command changed in between.
                key = name + ":" + str(args.get("project") or args.get("path") or args.get("manager") or "")
                fails[key] = fails.get(key, 0) + 1
                st.last_stuck = (name, args, result)
                if fails[key] == 2 and claude_ready():
                    if stuck_help(st, emit, cfg, name, args, result):
                        return          # a card is waiting for the user
                elif fails[key] >= 2:
                    return ask_user(st, emit, "That step failed %s." % ("twice" if fails[key] == 2 else
                                                                     "%d times" % fails[key]), tried)
            if idle >= idle_limit:
                return ask_user(st, emit, "I've taken %d steps without getting any further." % idle, tried)
    emit({"type": "answer", "text": "I stopped after %d steps so I don't loop forever. Tell me how to continue."
          % (PLAN_TOOL_ROUNDS if st.plan else MAX_TOOL_ROUNDS)})


# What each step is doing / did, in plain words for the page. {x} is filled from the step's arguments.
STEP_WORDS = {
    "test_connection": "Checking the connection", "list_folder": "Looking in {path}", "read_file": "Reading {path}",
    "scan_device": "Scanning for damage", "last_report": "Reading the last scan report",
    "find_good_copies": "Looking for good copies of {path}", "search_snapshot": "Searching saved copies for {text}",
    "change_history": "Reading the change history", "set_scan_folders": "Saving which folders to scan",
    "propose_restore": "Preparing to put back {path}", "propose_edit": "Preparing a change to {path}",
    "propose_undo": "Preparing to undo a change", "propose_nro_info_change": "Preparing a name/version change",
    "propose_nro_icon": "Preparing a new icon", "set_project": "Saving project {name}", "list_projects": "Listing projects",
    "project_files": "Listing the files of {project}", "project_read": "Reading {path} in {project}",
    "project_search": "Searching {project} for {text}", "propose_project_edit": "Changing {path} in {project}",
    "patchbay_files": "Listing Patchbay's own files", "patchbay_read": "Reading Patchbay's {path}",
    "patchbay_search": "Searching Patchbay for {text}", "propose_patchbay_change": "Preparing a change to Patchbay's {path}",
    "propose_patchbay_restart": "Preparing a restart",
    "propose_build": "Building {project}", "propose_install": "Preparing to install {project}",
    "show_settings": "Reading the settings", "propose_settings": "Preparing a settings change",
    "check_address": "Checking {host} port {port}", "add_device": "Adding a device",
    "identify_firmware": "Working out the firmware", "save_firmware_name": "Saving the firmware name",
    "web_search": "Searching the web for {query}", "device_firmware": "Reading the firmware versions",
    "latest_release": "Checking the newest release of {repo}", "check_build_tools": "Checking the build tools",
    "propose_get_source": "Getting the source code from {url}", "propose_command": "Running {command}",
    "read_web_page": "Reading {url}", "setup_firmware_helper": "Preparing the helper app",
    "find_homebrew": "Looking for {name} on the device", "prepare_homebrew_update": "Getting ready to update {name}",
    "platform_notes": "Reading notes about {platform}", "save_platform_notes": "Saving notes about {platform}",
    "backup": "Starting a backup", "read_attachment": "Reading the attached file {name}",
    "propose_plan": "Writing a plan", "ask_claude": "Asking Claude", "scan_network": "Looking for devices on your network", "propose_install_packages": "Installing {packages}", "list_devices": "Listing your devices", "open_device": "Opening {device}",
}


def step_doing(name, args):
    words = STEP_WORDS.get(name) or ("Using " + (name or "a tool").replace("_", " "))
    try:
        return words.format(**{k: (", ".join(map(str, v)) if isinstance(v, list) else str(v))[:60]
                               for k, v in (args or {}).items()}) if "{" in words else words
    except (KeyError, IndexError, ValueError):
        return re.sub(r"\s*\{\w+\}", "", words)


def step_summary(name, args, result):
    """One plain line for a finished step: what it did, and the first line of what it found."""
    first = next((l.strip() for l in (result or "").splitlines() if l.strip()), "")
    doing = step_doing(name, args)
    w, _, rest = doing.partition(" ")
    if w.endswith("ing"):
        stem = w[:-3].lower()
        w = PAST.get(stem, stem.capitalize() + "ed")
    doing = (w + " " + rest).strip()
    if first.startswith("A confirmation card"):
        return "Waiting for you to confirm on the card: " + step_doing(name, args)
    if first.startswith("Done automatically"):
        first = first[len("Done automatically (no card needed). "):] or first
    if failed(result):
        doing = "Didn't work: " + step_doing(name, args)[:1].lower() + step_doing(name, args)[1:]
    return "%s: %s" % (doing, first[:160]) if first else doing


PAST = {"check": "Checked", "look": "Looked", "read": "Read", "scann": "Scanned", "search": "Searched",
        "sav": "Saved", "prepar": "Prepared", "list": "Listed", "chang": "Changed", "build": "Built",
        "add": "Added", "work": "Worked", "gett": "Got", "runn": "Ran", "start": "Started", "writ": "Wrote",
        "open": "Opened", "us": "Used"}


def do_confirm(st, pid, typed, emit, enable_writes=False):
    cfg = st.cfg()
    p = st.pending.get(pid)
    if not p:
        raise ToolError("That change is no longer waiting (it was already handled, or the app was restarted).")
    if p["kind"] in ("self_edit", "self_restart"):
        if p.get("word") and typed.strip().lower() != p["word"].lower():
            raise ToolError("This change touches one of Patchbay's safety rules, so type %s to confirm it." % p["word"])
        del st.pending[pid]
        return confirm_self(st, p, emit)
    if getattr(st, "home", False):
        cfg = APP.device(p["device"]).cfg()   # a change made from the home chat, for one device
    if p["kind"] in ("project_edit", "build", "get_source", "command", "settings", "plan", "install_packages",
                     "ask_claude", "device_command"):
        if p.get("needs_typing") and typed.strip() != cfg["device_name"]:
            raise ToolError("%s needs you to type %s exactly." % (
                "Running a command on the device" if p["kind"] == "device_command" else "Turning writing on",
                cfg["device_name"]))
        del st.pending[pid]
        return confirm_on_mac(st, cfg, p, emit, typed)
    if typed.strip() != cfg["device_name"]:
        raise ToolError("You need to type %s exactly to confirm." % cfg["device_name"])
    if enable_writes and not cfg.get("allow_writes"):
        # The user ticked "turn on writing" on the card and typed the device name.
        planned = devices.plan_changes(APP.registry, APP.registry.path_for(cfg["device_name"]), {"allow_writes": True})
        devices.apply_changes(cfg, planned)
        APP.registry.status.clear()
        cfg = (APP.device(p["device"]) if p.get("device") else st).cfg()
        emit({"type": "note", "text": "Writing is now on for %s. You can turn it off again any time by asking me to "
                                      "turn off writing." % cfg["device_name"]})
    del st.pending[pid]
    entry, result = device_write(cfg, p, emit)
    emit({"type": "answer", "text": result})
    st.messages.append({"role": "user", "content": "[I confirmed the %s of %s on the card. Result: %s]"
                                                   % (p["kind"], p["remote"], result)})
    if p["kind"] == "helper" and entry:
        wait_for_helper(st, cfg, emit)


HELPER_WAIT = 300


def read_helper(cfg):
    dev = core.GD1(cfg)
    try:
        dev.connect(quiet=True)
        try:
            return firmware._fetch(dev, firmware.HELPER_FILE, 64 * 1024)
        finally:
            dev.close()
    except (SystemExit, ftplib.Error, OSError, EOFError):
        return None


def wait_for_helper(st, cfg, emit):
    """After the helper app is installed: wait for the user to open it on the console, then read the versions."""
    before = read_helper(cfg)
    emit({"type": "answer", "text": "Now on the Switch: open the Homebrew Menu and start \"Patchbay system info\". "
                                    "I'll notice by myself when it has run. (If you use the ftpd app for FTP, start "
                                    "it again afterwards.)"})
    start = time.time()
    while time.time() - start < HELPER_WAIT:
        for _ in range(8):
            time.sleep(0.5)
            check_stop()
        now = read_helper(cfg)
        if now and now != before:
            kv = dict(re.findall(r"(?m)^\s*([a-z_]+)\s*=\s*(.*?)\s*$", now.decode("utf-8", "replace")))
            label = "Switch %s, Atmosphère %s" % (kv.get("firmware") or "?", kv.get("atmosphere") or "?")
            APP.registry.set_firmware(cfg, label)
            emit({"type": "answer", "text": "Got it: %s (%s)." % (label, kv.get("emummc") or "")})
            st.messages.append({"role": "user", "content": "[The helper app ran on the console: firmware %s, "
                                "Atmosphère %s, %s. Carry on with what I asked before, without asking me again.]"
                                % (kv.get("firmware"), kv.get("atmosphere"), kv.get("emummc"))})
            chat_turn(st, None, emit)
            return
        emit({"type": "status", "text": "Waiting for Patchbay system info to run on the Switch... (%d s)"
                                        % (time.time() - start)})
    emit({"type": "answer", "text": "I didn't see the helper app run yet. Open it from the Homebrew Menu, then tell "
                                    "me \"done\"."})


def device_write(cfg, p, emit):
    """Put a file on the device (the current one is backed up first). Returns (history entry or None, result text)."""
    with WRITES:
        entry, out = run_captured(emit, core.put_back, cfg, p["remote"], Path(p["source"]), p["kind"], p["note"],
                                  ask=lambda c, what: print(what))
    if entry and entry.get("verified"):
        result = "Done: change #%d. %s on %s was written, and any old file was backed up first." % (
            entry["id"], p["remote"], cfg["device_name"])
    elif entry:
        result = "Uploaded as change #%d, but the size on the device doesn't match. Check the file." % entry["id"]
    else:
        result = "The change was NOT made. " + (out.strip().splitlines() or ["Unknown error."])[-1]
    return entry, result


def confirm_on_mac(st, cfg, p, emit, typed=""):
    if p["kind"] == "plan":
        if p["writes"]:
            # Back up the folders the plan will change, before anything is touched.
            folders = sorted({str(PurePosixPath(w).parent) for w in p["writes"]})
            emit({"type": "status", "text": "Backing up %s first..." % ", ".join(folders)})
            job = backup.Job(cfg, [{"path": f, "kind": "dir"} for f in folders], "Before: " + (p.get("note") or "plan"))
            APP.backups[cfg["device_name"]] = job
            job._run_safe()
            done = job.status()
            if done["phase"] != "done":
                emit({"type": "answer", "text": "I couldn't back up %s first, so I haven't started the plan: %s"
                                                % (", ".join(folders), done["message"])})
                st.messages.append({"role": "user", "content": "[The safety backup before the plan failed: %s]"
                                                               % done["message"]})
                return
            emit({"type": "note", "text": "Backed up %s first (Backups tab)." % ", ".join(folders)})
        st.plan = {"steps": p["steps"], "writes": p["writes"]}
        emit({"type": "answer", "text": "Plan approved. Working through it now; press Stop any time."})
        st.messages.append({"role": "user", "content": "[I approved the plan. Carry it out now without asking me again."
                                                       "%s]" % (" These device changes are approved too: " +
                                                                ", ".join(p["writes"]) if p["writes"] else "")})
        chat_turn(st, None, emit)
        return
    if p["kind"] == "ask_claude":
        sc = claude_scrubber()
        try:
            answer = send_question(st, emit, p["text"], sc, p.get("purpose") or "stuck")
        except claude.ClaudeError as e:
            emit({"type": "answer", "text": str(e)})
            st.messages.append({"role": "user", "content": "[Asking Claude failed: %s. Carry on yourself.]" % e})
            return
        emit({"type": "note", "text": "**Claude's advice:**\n\n" + answer})
        st.messages.append({"role": "user", "content": "[I sent the question to Claude. Its answer, which you should "
                                                       "follow now:\n%s]" % answer})
        chat_turn(st, None, emit)
        return
    if p["kind"] == "device_command":
        run_device_command(st, cfg, p, emit)
        return
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
    short, detail = run_mac_step(st, cfg, p, emit)
    emit({"type": "answer", "text": short})
    st.messages.append({"role": "user", "content": "[I confirmed the %s. %s]" % (p["kind"].replace("_", " "), detail)})
    if p["kind"] != "project_edit":
        # Let the AI read the output and say what's next (fix errors, or offer to install).
        chat_turn(st, None, emit)


def step_key(p):
    return json.dumps([p.get("kind"), p.get("project"), p.get("command"), p.get("packages"), p.get("update_all"),
                       p.get("remote") if p.get("kind") == "project_edit" else None], sort_keys=True, default=str)


def run_mac_step(st, cfg, p, emit):
    """Do one confirmed (or automatic) step on the Mac, and remember it if it failed."""
    short, detail = _run_mac_step(st, cfg, p, emit)
    failed_steps = st.__dict__.setdefault("failed_steps", {})
    if re.search(r"\bfailed\b", short) and p.get("kind") in ("command", "install_packages") \
            and "password box was cancelled" not in detail:
        failed_steps[step_key(p)] = short
    else:
        failed_steps.pop(step_key(p), None)
    return short, detail


def _run_mac_step(st, cfg, p, emit):
    """Do one confirmed (or automatic) step on the Mac. Returns (one line for the user, full result for the AI)."""
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
        return result.splitlines()[0], "Result: " + result
    if p["kind"] == "install_packages":
        emit({"type": "status", "text": "Installing %s..." % (", ".join(p["packages"]) or "updates")})
        try:
            code, out = projects.install_packages(p["manager"], p["packages"], p["update_all"], emit, STOP)
        except (OSError, projects.ProjectError) as e:
            raise ToolError("Couldn't install: %s" % e)
        result = "Installing %s %s (exit code %d)." % (", ".join(p["packages"]) or "updates",
                                                       "worked" if code == 0 else "failed", code)
        return show_output(emit, result, code, out)
    pr = project(st, p["project"])
    if p["kind"] == "command":
        emit({"type": "status", "text": "Running %s..." % p["command"][:60]})
        try:
            code, out = projects.run_shell(projects.folder_of(pr), p["command"], emit, stop=STOP)
        except (OSError, projects.ProjectError) as e:
            raise ToolError("Couldn't run it: %s" % e)
        result = "The command %s %s (exit code %d)." % (p["command"][:80], "worked" if code == 0 else "failed", code)
        return show_output(emit, result, code, out, p["command"])
    if p["kind"] == "project_edit":
        try:
            bk = projects.write_file(cfg, pr, p["remote"], Path(p["source"]).read_text("utf-8"))
        except (OSError, projects.ProjectError) as e:
            raise ToolError("Couldn't save %s: %s" % (p["remote"], e))
        result = "Saved %s in project %s.%s" % (p["remote"], pr["name"],
                                                " The old version was backed up to %s." % bk if bk else " (new file)")
        return result, "Result: " + result
    pr = checked_build(st, cfg, pr, emit, save=True)
    emit({"type": "status", "text": "Building %s..." % pr["name"]})
    try:
        code, out, built = projects.run_build(pr, emit, STOP)
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
            result += " But %s wasn't made. Find the built file (project_files) and fix the output with set_project." \
                      % pr["output"]
    else:
        result = "The build of %s failed (exit code %d)." % (pr["name"], code)
    return show_output(emit, result, code, out, pr["build"])


ERROR_LINE = re.compile(r"(?i)(\berror\b|command not found|not found|no such file|fatal|cannot|could not|failed|"
                        r"undefined reference|missing)")


def output_tail(out, lines=80):
    """The last lines of a command's output, plus the error lines from earlier on (so the cause isn't cut off)."""
    rows = [l.rstrip() for l in (out or "").splitlines()]
    tail = rows[-lines:]
    early = [l for l in rows[:-lines] if ERROR_LINE.search(l)][-20:]
    text = "\n".join(tail)
    if early:
        text = "Error lines from earlier in the output:\n" + "\n".join(early) + "\n...\nLast %d lines:\n%s" % (len(tail), text)
    return text[-9000:]


def first_error(out):
    for l in (out or "").splitlines():
        if ERROR_LINE.search(l) and not re.search(r"(?i)\b0 errors?\b|-- (looking|performing|check)", l):
            return l.strip()[:200]
    return ""


def show_output(emit, result, code, out, command=""):
    """Shows the output on the page (open, so it's easy to copy) and gives the AI the same text."""
    tail = output_tail(out)
    err = first_error(out) if code != 0 else ""
    if code == 127 or "command not found" in (out or ""):
        err = (err or "command not found") + " (exit code 127 means a program isn't installed or isn't on the PATH)"
        if re.search(r"(-cmake|aarch64-none-elf|arm-none-eabi|dkp-|elf2nro|nacptool)", out or ""):
            err += ". That's a devkitPro tool: install devkitPro's packages with propose_install_packages"
        elif re.search(r"\bcmake\b", out or "") and not re.search(r"-cmake", out or ""):
            err += ". Install cmake with propose_install_packages (homebrew)"
    m = re.search(r"Could NOT find (\w+)|Package '?([\w-]+)'?,? (?:was )?not found|No package '([\w-]+)' found",
                  out or "")
    if code != 0 and m:
        lib = (m.group(1) or m.group(2) or m.group(3) or "").lower() or "?"
        have = projects.dkp_installed("switch-" + lib)
        wrapper = bool(re.search(r"\b[a-z0-9_]+-none-[a-z]+-cmake\b|\b[a-z0-9_]+-[a-z]+-cmake\b", command or ""))
        if have:
            err += (". devkitPro's %s is already installed, so installing or updating won't help. The build must "
                    "configure with aarch64-none-elf-cmake (not plain cmake)%s, in a fresh build folder (Patchbay clears old cmake settings by itself after a failed setup). If it still fails, \
add -DZLIB_ROOT=/opt/devkitpro/portlibs/switch"
                    % (have, " - this build command uses plain cmake, fix it with set_project" if command and not
                       wrapper else ""))
        else:
            err += (". A library is missing: for a console build install devkitPro's version (for example switch-%s) "
                    "with propose_install_packages" % lib)
    summary = result + (" First error: " + err if err else "")
    emit({"type": "tool_result", "name": "output", "summary": summary, "text": tail or "(no output)", "open": code != 0})
    return summary, "%s\nOutput (last lines):\n%s" % (summary, tail or "(no output)")


def do_cancel(st, pid, emit):
    p = st.pending.pop(pid, None)
    if p and p["kind"] == "ask_claude":
        st.messages.append({"role": "user", "content": "[I chose not to send the question to Claude. Carry on "
                                                       "yourself.]"})
        emit({"type": "answer", "text": "Not sent. Nothing went to Claude."})
        return
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
        self.home = HomeState(config_path)
        self.chat_job = None
        self.transfers = {}     # transfer id -> fm.Transfer (copies shown in the Files transfer queue)
        self.lock = threading.Lock()
        self.computer = computer_name()
        self.claude = claude.Claude(Path(config_path).parent)

    def device(self, name):
        """The chat state for one saved device, by name. "__home__" is the home screen's own chat."""
        if name == HOME:
            return self.home
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

    def fix_page(self, msg):
        """A plain page with no scripts, for when a change to Patchbay broke its own page."""
        import html as _h
        rows = []
        for h in reversed(selfedit.history()[-40:]):
            what = ", ".join(f["path"] for f in h["files"])
            state = ("undone by #%s" % h["undone_by"]) if h.get("undone_by") else (
                "undo of #%s" % h["undo_of"] if h.get("undo_of") else "")
            btn = "" if h.get("undone_by") else ('<form method="post" action="/fix" style="margin:0"><input type="hidden" '
                                                  'name="undo" value="%s"><button>Undo</button></form>' % h["id"])
            rows.append("<tr><td>#%s</td><td>%s</td><td><b>%s</b><br><small>%s</small></td><td>%s</td><td>%s</td></tr>"
                        % (h["id"], _h.escape(h["time"].replace("_", " ")), _h.escape(what), _h.escape(h["note"]),
                           _h.escape(state), btn))
        body = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Patchbay repair</title><style>body{font:15px -apple-system,system-ui,sans-serif;max-width:900px;margin:30px auto;
padding:0 16px;background:#141413;color:#ecece8}td{padding:8px;border-bottom:1px solid #33332f;vertical-align:top}
button{font:inherit;padding:4px 12px;border-radius:8px}a{color:#7c9cf0}.m{background:#26324f;padding:10px;
border-radius:8px}</style><h1>Patchbay repair</h1>
<p>Changes Patchbay made to its own code, newest first. Undo one to put the version from before it back, then
restart Patchbay. <a href="/">Back to Patchbay</a></p>%s
<table>%s</table><form method="post" action="/fix" style="margin-top:16px"><input type="hidden" name="restart"
value="1"><button>Restart Patchbay</button></form>""" % (
            ('<p class="m">%s</p>' % _h.escape(msg)) if msg else "",
            "".join(rows) or "<tr><td>Patchbay hasn't changed its own code yet.</td></tr>")
        self.send_body(body.encode(), "text/html; charset=utf-8")

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
                return True
            except OSError:
                return False
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
        if url.path == "/fix":
            self.fix_page(q.get("msg") or "")
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
            job = APP.chat_job
            running = bool(job and not job.done and job.device == q.get("device"))
            if not running:
                st.ensure_loaded()
            self.json({"events": st.transcript, "pending": list(st.pending), "running": running,
                       "job_at": len(job.events) if running else 0, "chat_id": st.chat_id, "title": st.title})
        elif path == "/api/transfers":
            dev = q.get("device")
            for t in list(APP.transfers.values()):
                fin = t.status()["finished"]
                if fin and time.time() - fin > 900:
                    APP.transfers.pop(t.id, None)   # finished more than 15 minutes ago
            self.json({"transfers": [t.status() for t in list(APP.transfers.values()) if not dev or t.device == dev]})
        elif path == "/api/files/preview":
            cfg = APP.device(q.get("device")).cfg()
            try:
                self.json(fm.preview(cfg, q.get("side"), q.get("path")))
            except (fm.FileError, ftplib.Error, OSError, EOFError) as e:
                self.json({"error": str(e)}, 400)
        elif path == "/api/bookmarks":
            self.json(fm.bookmarks(APP.device(q.get("device")).cfg()))
        elif path == "/api/chats":
            st = APP.device(q.get("device"))
            st.ensure_loaded()
            self.json({"chats": st.list_chats()[:200], "current": st.chat_id})
        elif path == "/api/netscan":
            self.json(last_scan())
        elif path == "/api/library":
            cfg = APP.device(q.get("device")).cfg()
            job = LIBRARY_JOBS.get(cfg["device_name"])
            self.json({"job": job.status() if job else None, "library": library.saved(cfg)})
        elif path == "/api/library/icon":
            cfg = APP.device(q.get("device")).cfg()
            key = q.get("id") or ""
            folder = core.data_dir(cfg) / "library" / "icons"
            f = next((folder / (key + ext) for ext in (".jpg", ".png") if (folder / (key + ext)).exists()), None)
            if not re.match(r"^[0-9a-f]{16}$", key) or not f:
                self.send_error(404)
                return
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if f.suffix == ".png" else "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/health":
            self.json({"alerts": health.check(APP.registry, deep=q.get("deep") == "1"), "checked": time.time()})
        elif path == "/api/prompts":
            self.json({"prompts": load_prompts()})
        elif path == "/api/activity":
            items = activity.collect(APP.registry)
            writes = {cfg["device_name"]: bool(cfg.get("allow_writes")) for _, cfg in APP.registry.all()}
            self.json({"items": items, "writes_on": writes})
        elif path == "/api/update":
            self.json(updater.status(fetch=q.get("fetch") != "0"))
        elif path == "/api/security":
            ip = (q.get("ip") or "").strip()
            job = SECURITY.get(ip)
            if job:
                self.json(job.status())
            else:
                self.json({"ip": ip, "running": False, "report": securitycheck.load_report(ip), "error": None})
        elif path == "/api/selfcheck":
            self.json({"checks": selfcheck.run_checks(APP), "app": mac_app.status()})
        elif path == "/api/report":
            self.json({"text": selfcheck.report(APP)})
        elif path == "/api/claude":
            self.json(APP.claude.public())
        elif path == "/api/claude/sent":
            self.json({"items": APP.claude.sent_list()})
        elif path == "/api/models":
            cfg = APP.home.cfg()
            try:
                req = urllib.request.Request(ollama_url(cfg) + "/api/tags")
                with urllib.request.urlopen(req, timeout=10) as r:
                    tags = json.loads(r.read()).get("models", [])
                models = [{"name": m.get("name"), "size": core.human_size(m.get("size") or 0)} for m in tags]
                err = None
            except (OSError, ValueError, ToolError) as e:
                models, err = [], "Couldn't ask Ollama for its models (%s). Is the Ollama app running?" % e
            if cfg["ollama_model"] not in [m["name"] for m in models]:
                models.insert(0, {"name": cfg["ollama_model"], "size": ""})
            self.json({"current": cfg["ollama_model"], "models": models, "error": err})
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
                       "settings": {"keep": int(cfg.get("backup_keep") or 0), "mirror": cfg.get("backup_mirror") or "",
                                    "compress": bool(cfg.get("backup_compress"))},
                       "folder": str(backup.backup_root(cfg).parent), "device_folder": str(backup.backup_root(cfg)),
                       "unfinished": bool(backup.unfinished(cfg)), "computer": APP.computer})
        elif path == "/api/backup/browse":
            try:
                self.json(backup.browse(APP.device(q.get("device")).cfg(), q.get("name"), q.get("folder")))
            except backup.BackupError as e:
                self.json({"error": str(e)}, 400)
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
        if url.path == "/api/chat/attach":
            # A file dropped into the chat: the body is the file itself.
            if not self.signed_in():
                self.json({"error": "Sign in first."}, 401)
                return
            try:
                self.json(save_attachment(APP.device(q.get("device")), q.get("name"), self.rfile, n))
            except (ToolError, OSError) as e:
                self.json({"error": str(e)}, 400)
            return
        if url.path == "/fix":
            if not self.signed_in():
                self.send_error(401)
                return
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(n, 10000)).decode("utf-8", "replace")).items()}
            try:
                if form.get("restart"):
                    msg = "Restarting Patchbay. Wait a few seconds, then open the page again."
                    updater.restart_soon(1.0)
                else:
                    msg = selfedit.undo(form.get("undo"))
            except selfedit.SelfEditError as e:
                msg = str(e)
            self.send_response(303)
            self.send_header("Location", "/fix?msg=" + quote(msg))
            self.send_header("Content-Length", "0")
            self.end_headers()
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
            if self.busy_for(data.get("device")):
                raise ToolError("Wait for the current request to finish (or press Stop) first.")
            st.ensure_loaded()
            st.new_chat()
            self.json({"ok": True})
        elif path == "/api/chats/open":
            st = APP.device(data.get("device"))
            if self.busy_for(data.get("device")):
                raise ToolError("Wait for the current request to finish (or press Stop) first.")
            st.ensure_loaded()
            st.save_chat()
            st.open_chat(data.get("id"))
            self.json({"ok": True})
        elif path == "/api/chats/delete":
            st = APP.device(data.get("device"))
            if self.busy_for(data.get("device")) and data.get("id") == st.chat_id:
                raise ToolError("Wait for the current request to finish first.")
            st.delete_chat(data.get("id"))
            self.json({"ok": True})
        elif path == "/api/stop":
            job = APP.chat_job
            if job and not job.done:
                STOP.set()
            self.json({"ok": True})
        elif path == "/api/library/scan":
            cfg = APP.device(data.get("device")).cfg()
            job = LIBRARY_JOBS.get(cfg["device_name"])
            if not (job and job.finished is None):
                LIBRARY_JOBS[cfg["device_name"]] = library.ScanJob(cfg)
            self.json({"ok": True})
        elif path == "/api/library/updates":
            cfg = APP.device(data.get("device")).cfg()
            try:
                lib, errors = library.check_updates(cfg)
            except ValueError as e:
                raise ToolError(str(e))
            self.json({"library": lib, "errors": errors})
        elif path == "/api/library/repo":
            cfg = APP.device(data.get("device")).cfg()
            try:
                repo = library.set_repo(cfg, data.get("path") or "", data.get("repo") or "")
            except ValueError as e:
                raise ToolError(str(e))
            self.json({"repo": repo})
        elif path == "/api/prompts":
            items = load_prompts()
            name = (data.get("name") or "").strip()[:80]
            if data.get("action") == "delete":
                items = [x for x in items if x["name"] != name]
            elif data.get("action") == "save":
                text = (data.get("text") or "").strip()
                if not name or not text:
                    raise ToolError("Give the prompt a name and some text.")
                if len(text) > 200000:
                    raise ToolError("That prompt is too long to save (over 200,000 characters).")
                old_name = (data.get("old_name") or name).strip()
                now = time.strftime("%Y-%m-%d %H:%M")
                keep = next((x for x in items if x["name"] == old_name), None)
                items = [x for x in items if x["name"] not in (old_name, name)]
                items.append({"name": name, "text": text, "saved": now, "runs": (keep or {}).get("runs", 0)})
            elif data.get("action") == "ran":
                for x in items:
                    if x["name"] == name:
                        x["runs"] = x.get("runs", 0) + 1
                        x["last_run"] = time.strftime("%Y-%m-%d %H:%M")
            else:
                raise ToolError("Unknown action.")
            items.sort(key=lambda x: x["name"].lower())
            save_prompts(items)
            self.json({"prompts": items})
        elif path == "/api/activity/undo":
            item = str(data.get("id") or "")
            name, kind = (item.split("|") + ["", ""])[:2]
            job = APP.chat_job
            if job and not job.done:
                raise ToolError("Wait for the chat to finish (or press Stop) first.")
            if kind == "device":
                if (data.get("typed") or "").strip() != name:
                    raise ToolError("Type %s exactly to undo a change on the device." % name)
                cfg = APP.device(name).cfg()
                if data.get("enable_writes") and not cfg.get("allow_writes"):
                    planned = devices.plan_changes(APP.registry, APP.registry.path_for(name), {"allow_writes": True})
                    devices.apply_changes(cfg, planned)
                    APP.registry.status.clear()
            try:
                with WRITES:
                    msg = activity.undo(APP.registry, item)
            except activity.UndoError as e:
                raise ToolError(str(e))
            self.json({"message": msg})
        elif path == "/api/update/apply":
            job = APP.chat_job
            if job and not job.done:
                raise ToolError("Wait for the chat to finish (or press Stop) first, then update.")
            try:
                msg = updater.update()
            except RuntimeError as e:
                raise ToolError(str(e))
            restart = msg.startswith("Updated")
            if restart:
                updater.restart_soon()
            self.json({"message": msg, "restarting": restart})
        elif path == "/api/security/start":
            if not data.get("owner_confirmed"):
                raise ToolError("First confirm that this device is yours (or that you're allowed to check it).")
            try:
                ip = securitycheck.check_target(str(data.get("ip") or ""))
            except securitycheck.NotAllowed as e:
                raise ToolError(str(e))
            job = SECURITY.get(ip)
            if not (job and job.finished is None):
                job = SECURITY[ip] = securitycheck.SecurityCheck(ip, data.get("kind"))
            self.json(job.status())
        elif path == "/api/security/stop":
            job = SECURITY.get(str(data.get("ip") or ""))
            if job:
                job.stop.set()
            self.json({"ok": True})
        elif path == "/api/netscan/start":
            job = getattr(APP, "netscan", None)
            if not (job and job.finished is None):
                APP.netscan = NetScanJob()
            self.json(APP.netscan.status())
        elif path == "/api/netscan/stop":
            job = getattr(APP, "netscan", None)
            if job:
                job.stop.set()
            self.json({"ok": True})
        elif path == "/api/selfcheck/fix":
            try:
                msg = selfcheck.fix(APP, data.get("id"), lambda ev: None)
            except RuntimeError as e:
                raise ToolError(str(e))
            self.json({"message": msg})
        elif path == "/api/macapp":
            act = data.get("action")
            try:
                if act == "make_app":
                    mac_app.make_app()
                elif act == "remove_app":
                    mac_app.remove_app()
                elif act == "install_tools":
                    mac_app.install_tools()
                elif act in ("login_on", "login_off"):
                    mac_app.set_login(act == "login_on")
                else:
                    raise ToolError("Unknown action.")
            except OSError as e:
                raise ToolError(str(e))
            self.json(mac_app.status())
        elif path == "/api/claude/settings":
            try:
                self.json(APP.claude.save_settings({k: v for k, v in data.items() if k in claude.DEFAULTS}))
            except claude.ClaudeError as e:
                raise ToolError(str(e))
        elif path == "/api/claude/test":
            sc = claude_scrubber()
            try:
                answer = APP.claude.ask("Patchbay is checking that its API key works. Reply with just: OK", sc, "key test")
            except claude.ClaudeError as e:
                raise ToolError(str(e))
            self.json(dict(APP.claude.public(), answer=answer[:200]))
        elif path == "/api/model":
            name = (data.get("model") or "").strip()
            if not re.match(r"^[\w.:/-]{1,100}$", name):
                raise ToolError("That isn't a model name.")
            planned = devices.plan_changes(APP.registry, APP.config_path, {"ollama_model": name})
            devices.apply_changes(APP.home.cfg(), planned)
            self.json({"ok": True, "model": name})
        elif path in ("/api/chat", "/api/confirm", "/api/cancel"):
            self.chat(path, data)
        elif path == "/api/follow":
            emit = self.start_stream()
            job = APP.chat_job
            if job and job.device == data.get("device"):
                self.follow(job, int(data.get("since") or 0), emit)
            else:
                emit({"type": "done"})
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
            start_backup(cfg, data.get("items"), data.get("label"))
            self.json({"ok": True})
        elif path == "/api/backup/settings":
            st = APP.device(data.get("device"))
            changes = {k: data[k] for k in ("backup_keep", "backup_mirror", "backup_compress") if k in data}
            planned = devices.plan_changes(APP.registry, st.config_path, changes)
            if planned:
                devices.apply_changes(st.cfg(), planned)
            self.json({"ok": True})
        elif path == "/api/backup/label":
            backup.set_label(APP.device(data.get("device")).cfg(), data.get("name"), data.get("label"))
            self.json({"ok": True})
        elif path == "/api/backup/verify":
            cfg = APP.device(data.get("device")).cfg()
            with APP.lock:
                job = APP.backups.get(cfg["device_name"])
                if job and job.status()["running"]:
                    raise ToolError("Wait for the current backup or check to finish.")
                job = APP.backups[cfg["device_name"]] = backup.VerifyJob(cfg, data.get("name"))
                job.start()
            self.json({"ok": True})
        elif path == "/api/backup/restore_plan":
            st = APP.device(data.get("device"))
            cfg = st.cfg()
            mapping, _ = backup.extract(cfg, data.get("name"), data.get("paths") or ["/"])
            title, lines, steps = fm.restore_plan(cfg, mapping, "From the backup " + (data.get("name") or ""))
            pid = uuid.uuid4().hex[:10]
            st.file_pending = {pid: {"title": title, "steps": steps}}
            self.json({"id": pid, "title": title, "lines": lines[:200], "more": max(0, len(lines) - 200),
                       "device": cfg["device_name"], "writes_on": bool(cfg.get("allow_writes"))})
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
        elif path == "/api/files/mac_rename":
            self.json({"ok": True, "path": fm.mac_rename(data.get("path"), data.get("new_name"))})
        elif path == "/api/files/mac_mkdir":
            self.json({"ok": True, "path": fm.mac_mkdir(data.get("folder"), data.get("name"))})
        elif path == "/api/devices/rename":
            st = APP.device(data.get("device"))
            cfg = st.cfg()
            job = APP.backups.get(cfg["device_name"])
            if job and job.status()["running"]:
                raise ToolError("A backup of %s is running. Rename it after the backup finishes." % cfg["device_name"])
            if BUSY.locked() and BUSY_WITH[0] == cfg["device_name"]:
                raise ToolError("The chat is still working on %s. Rename it when it's done." % cfg["device_name"])
            new = (data.get("new_name") or "").strip()
            extra = []
            if devices.NAME_OK.match(new) and backup.backup_root(cfg).exists():
                extra.append((backup.backup_root(cfg), backup.backup_root(dict(cfg, device_name=new))))
            name = devices.rename_device(APP.registry, st.config_path, new, extra)
            APP.backups.pop(cfg["device_name"], None)
            self.json({"ok": True, "name": name})
        elif path == "/api/files/apply":
            self.files_apply(data)
        elif path == "/api/files/copy_to_mac":
            st = APP.device(data.get("device"))
            cfg, items, folder = st.cfg(), data.get("items") or [], data.get("mac_folder") or "~"
            names = ", ".join(PurePosixPath(i.get("path") or "").name for i in items[:3]) + (
                " and %d more" % (len(items) - 3) if len(items) > 3 else "")
            tr = fm.Transfer(cfg["device_name"], "Copy %s to %s" % (names, folder), "download")

            def go():
                n = fm.copy_to_mac(cfg, items, folder, tr.emit, tr)
                return "Copied %d file%s to %s on %s." % (n, "" if n == 1 else "s", folder, APP.computer)
            self.json({"id": run_transfer(tr, go)})
        elif path == "/api/transfers/control":
            tr = APP.transfers.get(data.get("id") or "")
            if not tr:
                raise ToolError("That transfer is gone.")
            act = data.get("action")
            if act == "pause":
                tr.paused.set()
            elif act == "resume":
                tr.paused.clear()
            elif act == "cancel":
                tr.cancelled.set()
                tr.paused.clear()
            elif act == "clear":
                if tr.status()["state"] in ("done", "failed", "cancelled"):
                    APP.transfers.pop(tr.id, None)
            self.json({"ok": True})
        elif path == "/api/files/compare":
            a, b = data.get("a") or {}, data.get("b") or {}
            cfg = APP.device(data.get("device")).cfg()
            self.json(fm.compare(cfg, a, cfg, b))
        elif path == "/api/files/search":
            cfg = APP.device(data.get("device")).cfg()
            emit = self.start_stream()
            try:
                fm.search(cfg, data.get("folder") or "/", data.get("q"), emit)
            except fm.FileError as e:
                emit({"type": "error", "text": str(e)})
            except (ftplib.Error, OSError, EOFError) as e:
                emit({"type": "error", "text": "The search stopped: %s" % e})
            emit({"type": "done"})
        elif path == "/api/files/mac_save":
            cfg = APP.device(data.get("device")).cfg()
            bk = fm.mac_save(cfg, data.get("path"), data.get("text") or "")
            self.json({"ok": True, "backup": str(bk)})
        elif path == "/api/bookmarks":
            cfg = APP.device(data.get("device")).cfg()
            self.json(fm.save_bookmarks(cfg, data.get("marks") or {}))
        elif path == "/api/chat/attach_path":
            # "Ask AI about this" on a Mac file: copy it in as a chat attachment.
            st = APP.device(data.get("device"))
            src = Path(os.path.expanduser(data.get("path") or "")).resolve()
            if not src.is_file():
                raise ToolError("%s isn't a file on the Mac." % src)
            with open(src, "rb") as f:
                self.json(save_attachment(st, src.name, f, src.stat().st_size))
        else:
            self.send_error(404)

    def add_device(self, data):
        proto = (data.get("protocol") or "ftp").lower()
        try:
            ports = [int(p) for p in re.split(r"[,\s]+", str(data.get("ports") or "").strip()) if p] or \
                devices.DEFAULT_PORTS.get(proto, [21, 5000])
        except ValueError:
            raise devices.DeviceError("Ports should be numbers like 21, 5000.")
        args = dict(data, ports=ports, protocol=proto)
        if proto != "folder" and not (data.get("host") or "").strip():
            raise devices.DeviceError("Type the device's IP address.")
        test = None
        if not data.get("save_anyway"):
            try:
                test, _ = test_device(args, timeout=8)
            except SystemExit as e:
                self.json({"error": "I couldn't connect to it: %s" % e.code, "can_save_anyway": True}, 400)
                return
        f = APP.registry.add(data.get("name"), (test or args).get("host") or data.get("host"),
                             (test or {}).get("ports", ports), data.get("username"), data.get("password"),
                             data.get("about"), proto, data.get("share"), data.get("folder"))

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

    def busy_for(self, device):
        job = APP.chat_job
        return bool(job and not job.done and job.device == device)

    def chat(self, path, data):
        """Chat work runs in the background. This request only follows along, so if the page loses its
        connection (Wi-Fi blip, laptop asleep, a very slow AI answer) the work carries on and the page
        picks it up again with /api/follow."""
        st = APP.device(data.get("device"))
        emit_raw = self.start_stream()
        if not BUSY.acquire(blocking=False):
            emit_raw({"type": "error", "text": "I'm still working on a request for %s. Wait for it to finish."
                      % BUSY_WITH[0]})
            emit_raw({"type": "done"})
            return
        BUSY_WITH[0] = "the home chat" if getattr(st, "home", False) else st.cfg()["device_name"]
        job = ChatJob(data.get("device"))
        APP.chat_job = job
        STOP.clear()

        def work():
            def emit(ev):
                st.record(ev)
                job.emit(ev)
            try:
                st.ensure_loaded()
                if path == "/api/chat":
                    text = (data.get("text") or "").strip()
                    refs = [str(r) for r in (data.get("attachments") or [])][:10]
                    st.plan = None    # a new message ends any plan approved earlier
                    st.failed_steps = {}    # the user may have fixed something, so failed steps may be tried again
                    if NEW_JOB.match(text) and any(m.get("role") == "tool" for m in st.messages):
                        # A new big job gets a fresh chat, so old answers don't steer it. The old one is saved.
                        st.new_chat()
                        emit({"type": "new_chat", "text": text})
                        emit({"type": "note", "text": "I started a new chat for this job, so the earlier "
                                                      "conversation doesn't get in the way. It's saved under "
                                                      "Past chats."})
                    if text or refs:
                        st.record({"type": "user", "text": text, "files": [attachment_name(r) for r in refs]})
                    if text == ASK_CLAUDE_CHOICE and not refs:
                        ask_claude_choice(st, emit)
                    elif refs:
                        chat_turn(st, text, emit, attachment_message(st, text, refs))
                    else:
                        chat_turn(st, text, emit)
                elif path == "/api/confirm":
                    do_confirm(st, data.get("id", ""), data.get("typed", ""), emit, bool(data.get("enable_writes")))
                else:
                    do_cancel(st, data.get("id", ""), emit)
            except Stopped:
                st.plan = None
                emit({"type": "answer", "text": "Stopped."})
                st.messages.append({"role": "user", "content": "[I pressed Stop. Don't carry on with that unless I ask.]"})
            except ToolError as e:
                emit({"type": "error", "text": str(e)})
            except Exception as e:
                traceback.print_exc()
                note_problem("crash: " + traceback.format_exc()[-600:].replace("\n", " | "))
                emit({"type": "error", "text": "Something went wrong: %s" % e})
            finally:
                try:
                    st.save_chat()
                except (OSError, ToolError) as e:
                    print("Couldn't save the chat: %s" % e)
                BUSY.release()
                job.finish()
        threading.Thread(target=work, daemon=True).start()
        self.follow(job, 0, emit_raw)

    def follow(self, job, since, emit_raw):
        """Send the job's events from number `since` on, with a ping every few seconds so nothing
        between here and the browser decides the connection is dead."""
        i = since
        while True:
            with job.cond:
                if len(job.events) <= i and not job.done:
                    job.cond.wait(5)
                new = job.events[i:]
                done = job.done
            for ev in new:
                if not emit_raw(ev):
                    return   # the page went away; the work carries on
            i += len(new)
            if done and i >= len(job.events):
                emit_raw({"type": "done"})
                return
            if not new and not emit_raw({"type": "ping"}):
                return

    def files_apply(self, data):
        st = APP.device(data.get("device"))
        cfg = st.cfg()
        p = st.file_pending.get(data.get("id") or "")
        if not p:
            raise fm.FileError("That change is no longer waiting. Try it again.")
        if (data.get("typed") or "").strip() != cfg["device_name"]:
            raise fm.FileError("You need to type %s exactly to confirm." % cfg["device_name"])
        st.file_pending.pop(data.get("id"), None)
        tr = fm.Transfer(cfg["device_name"], p["title"], "upload")

        def go():
            with WRITES:
                done = fm.apply(cfg, p["steps"], "from the Files screen", tr.emit, tr)
            first, last = (done[0]["id"], done[-1]["id"]) if done else (0, 0)
            result = "Done: %s. %s" % (p["title"], "Logged as change #%d." % first if first == last and first else
                                       "Logged as changes #%d to #%d." % (first, last) if done else "")
            st.messages.append({"role": "user", "content": "[On the Files screen I confirmed: %s. Result: %s]"
                                                           % (p["title"], result)})
            return result
        self.json({"id": run_transfer(tr, go)})


def run_transfer(tr, work):
    """Run a copy in the background so the transfer queue can show it, pause it or cancel it."""
    APP.transfers[tr.id] = tr
    tr.set(state="running")

    def go():
        try:
            tr.set(state="done", message=work(), finished=time.time())
        except fm.Cancelled:
            tr.set(state="cancelled", message="Cancelled. Anything already copied stays where it is.",
                   finished=time.time())
        except (fm.FileError, ToolError) as e:
            tr.set(state="cancelled" if tr.cancelled.is_set() else "failed", message=str(e), finished=time.time())
        except (ftplib.Error, OSError, EOFError) as e:
            tr.set(state="failed", message="It stopped: %s" % e, finished=time.time())
        except Exception as e:
            traceback.print_exc()
            tr.set(state="failed", message="Something went wrong: %s" % e, finished=time.time())
    threading.Thread(target=go, daemon=True).start()
    return tr.id


NEW_JOB = re.compile(r"(?i)^\s*(please\s+|can you\s+|could you\s+)?(update|rebuild|recompile|port)\s+"
                     r"(?!it\b|that\b|this\b|them\b|again\b)\S")


class NetScanJob:
    """One network scan, running in the background so the page can show progress."""
    def __init__(self):
        self.started, self.finished = time.time(), None
        self.progress, self.note, self.results, self.error = 0.0, "Starting...", [], None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        def prog(done, total, note):
            self.progress, self.note = (done / total if total else 0), note
        try:
            found = netscan.scan(prog, self.stop)
            saved = {}
            for _, c in APP.registry.all():
                saved[c["host"]] = c["device_name"]
            for d in found:
                d["saved_as"] = saved.get(d["ip"])
            self.results = found
            self.note = "Found %d device(s)." % len(found)
            if not self.stop.is_set():
                save_scan({"results": found, "finished": time.time()})
        except Exception as e:  # keep the page alive
            traceback.print_exc()
            self.error = str(e)
            note_problem("network scan: %s" % e)
        finally:
            self.progress, self.finished = 1.0, time.time()

    def status(self):
        return {"running": self.finished is None, "progress": round(self.progress, 3), "note": self.note,
                "results": self.results, "error": self.error, "finished": self.finished}


PROMPTS_FILE = core.HERE / "data-cache" / "prompts.json"


def load_prompts():
    try:
        return json.loads(PROMPTS_FILE.read_text())
    except (OSError, ValueError):
        return []


def save_prompts(items):
    PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROMPTS_FILE.with_name("prompts.json.tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(PROMPTS_FILE)


SCAN_FILE = core.HERE / "data-cache" / "netscan-last.json"


def save_scan(data):
    """Keep the last finished scan, so the list is still there after a restart."""
    try:
        SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SCAN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(SCAN_FILE)
    except OSError:
        pass


def last_scan():
    job = getattr(APP, "netscan", None)
    if job and (job.finished is None or job.results or job.error):
        return job.status()
    try:
        data = json.loads(SCAN_FILE.read_text())
        return {"running": False, "progress": 1.0, "note": "", "results": data.get("results") or [],
                "error": None, "finished": data.get("finished")}
    except (OSError, ValueError):
        return {"running": False, "results": [], "finished": None}


def scan_summary(found):
    if not found:
        return "No other devices answered on your network."
    rows = []
    for d in found:
        ways = ", ".join(a["name"] for a in d["access"]) or "no open doors Patchbay knows"
        rows.append("%s — %s%s%s%s. Ways in: %s.%s" % (
            d["ip"], d["kind"], (" called %s" % d["name"]) if d["name"] else "",
            (", model %s" % d["model"]) if d.get("model") else "",
            (" (made by %s)" % d["vendor"]) if d["vendor"] and d["vendor"] != "Private address" else "", ways,
            (" Already saved as %s." % d["saved_as"]) if d.get("saved_as") else ""))
    return "Devices on your network:\n" + "\n".join(rows)


RECENT_ERRORS = collections.deque(maxlen=60)    # for the error report


def note_problem(text):
    RECENT_ERRORS.append("%s %s" % (time.strftime("%H:%M:%S"), re.sub(r"\s+", " ", str(text))[:400]))


class ChatJob:
    """One chat request being worked on: everything it has shown so far."""

    def __init__(self, device):
        self.device = device
        self.events = []
        self.done = False
        self.cond = threading.Condition()

    def emit(self, ev):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()


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
    ap.add_argument("--install-app", action="store_true",
                    help="make a Patchbay app icon (in your Applications folder) and start Patchbay when you log in")
    args = ap.parse_args()
    if args.install_app:
        try:
            path = mac_app.make_app()
            mac_app.set_login(True)
        except OSError as e:
            sys.exit(str(e))
        print("Done. Patchbay is now in your Applications folder (%s), and it starts by itself when you log in." % path)
        print("To put it in the Dock: open Applications in Finder and drag Patchbay onto the Dock.")
        return
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
    try:   # passwords and keys saved in plain settings files move into the Mac's Keychain
        moved = selfcheck.migrate_secrets(APP)
        if moved:
            print("Moved into the Mac's Keychain: %s. The settings files now only point to them." % ", ".join(moved))
    except Exception as e:
        print("Couldn't move passwords into the Keychain (they stay where they were): %s" % e)
    if os.environ.pop("PATCHBAY_RESTARTED", None):
        for _ in range(20):   # just restarted after an update: the old copy may still be letting go of the port
            try:
                make_server(lan, port).server_close()
                break
            except OSError:
                time.sleep(0.5)
    try:
        server = make_server(lan, port)
    except OSError:
        url = "http://127.0.0.1:%d" % port
        try:
            with urllib.request.urlopen(url + "/", timeout=3) as r:
                mine = b"Patchbay" in r.read(20000)
        except OSError:
            mine = False
        if mine:
            print("Patchbay is already running. Opening it: %s" % url)
            if not args.no_browser:
                webbrowser.open(url)
            return
        sys.exit("Port %d is already in use by another program. Try: python3 patchbay.py --port %d" % (port, port + 1))
    threading.Thread(target=mac_app.refresh_app, daemon=True).start()   # building the app can take a minute
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
