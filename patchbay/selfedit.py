"""
PatchBay changing its own code, safely.

The home chat can read PatchBay's own files and suggest changes to them. Nothing is changed until the user presses
Confirm on a card that shows the exact difference. Before a change is written:
  - the new file is checked (Python must compile, JSON must load, the page's scripts must parse when node is there),
  - the old file is backed up to data-cache/self-backups/<time>/,
  - the change is written down in data-cache/self-history.json, so the Activity log can undo it.

Changes that touch PatchBay's safety rules (typed device names, writing switches, the local-only AI address, what
may be sent to the cloud, network-scan limits, passwords) are marked on the card and need "PatchBay" typed to confirm.

If PatchBay can't start after a change, patchbay.py puts the last change back by itself (recover()). If the page
itself breaks, http://127.0.0.1:8765/fix is a plain page (no scripts) that can undo changes.
"""

import ast
import difflib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HISTORY = HERE / "data-cache" / "self-history.json"
BACKUPS = HERE / "data-cache" / "self-backups"
RECOVERED = HERE / "data-cache" / "self-recovered.json"

TEXT_TYPES = {".py", ".html", ".js", ".css", ".md", ".json", ".swift", ".txt", ".svg", ".c", ".h", ".cpp", ".sh",
              ".plist", ".toml", ".cfg", ".ini", ""}
# Never edited this way: the user's own settings, passwords and data, and git's own files.
BLOCKED_NAMES = {"config.json", "access.json", "claude.json", "claude-usage.json", "claude-aliases.json"}
BLOCKED_DIRS = {"data-cache", "devices", "claude-sent", ".git", "__pycache__"}

SAFETY = re.compile(r"needs_typing|typed|allow_writes|enable_writes|writes_on|device_name\b|ollama_url|localhost|"
                    r"127\.0\.0\.1|LOOPBACK|from_this_mac|same_site|signed_in|session_ok|password|is_private|"
                    r"private_ip|own subnet|brute|default.cred|redact|strip_private|never send|claude_ok|"
                    r"safe_for_cloud|SAFE_COMMAND|SAFE_PART|runs_by_itself|auto_mac_steps|BLOCKED_|SAFETY",
                    re.I)
RENAMED = {"gd1chat.py": "server.py", "gd1check.py": "core.py"}   # old file names, for undoing older changes
CONFIRM_WORD = "PatchBay"


class SelfEditError(Exception):
    pass


# ---------------------------------------------------------------- reading

def inside(rel):
    """PatchBay's own file for a relative path like 'web/app.html', refusing anything outside or off-limits."""
    rel = (rel or "").strip().lstrip("/")
    for top in ("patchbay/", "gd1-checker/"):
        if rel.startswith(top):
            rel = rel[len(top):]
    rel = RENAMED.get(rel, rel)
    if not rel:
        raise SelfEditError("Say which file, e.g. server.py or web/app.html.")
    p = (HERE / rel).resolve()
    try:
        parts = p.relative_to(HERE).parts
    except ValueError:
        raise SelfEditError("%s isn't inside PatchBay's folder." % rel)
    if p.name in BLOCKED_NAMES or any(x in BLOCKED_DIRS for x in parts):
        raise SelfEditError("%s holds your own settings or data, not PatchBay's code, so it can't be changed this "
                            "way. (Settings are changed with propose_settings.)" % rel)
    if p.suffix.lower() not in TEXT_TYPES:
        raise SelfEditError("%s isn't a text file PatchBay can edit." % rel)
    return p, Path(*parts).as_posix()


def files():
    out = []
    for p in sorted(HERE.rglob("*")):
        try:
            parts = p.relative_to(HERE).parts
        except ValueError:
            continue
        if not p.is_file() or p.name in BLOCKED_NAMES or any(x in BLOCKED_DIRS for x in parts):
            continue
        if p.suffix.lower() not in TEXT_TYPES or p.name.endswith(".tmp"):
            continue
        try:
            lines = p.read_text("utf-8", "replace").count("\n") + 1
        except OSError:
            continue
        out.append("%s (%d lines)" % (Path(*parts).as_posix(), lines))
    return "PatchBay's own files (in %s):\n%s" % (HERE, "\n".join(out))


def read(rel, start=1, count=300):
    p, rel = inside(rel)
    if not p.exists():
        raise SelfEditError("There's no file %s." % rel)
    lines = p.read_text("utf-8", "replace").splitlines()
    start = max(1, int(start or 1))
    count = max(1, min(int(count or 300), 800))
    part = lines[start - 1:start - 1 + count]
    body = "\n".join("%5d  %s" % (start + i, x) for i, x in enumerate(part))
    more = "" if start - 1 + count >= len(lines) else "\n(%d more lines; read on with start_line=%d)" % (
        len(lines) - (start - 1 + count), start + count)
    return "%s, lines %d-%d of %d:\n%s%s" % (rel, start, start + len(part) - 1, len(lines), body, more)


def search(text, limit=80):
    if not text:
        raise SelfEditError("Give some text to search for.")
    hits = []
    needle = text.lower()
    for line in files().splitlines()[1:]:
        rel = line.rsplit(" (", 1)[0]
        try:
            content = (HERE / rel).read_text("utf-8", "replace").splitlines()
        except OSError:
            continue
        for i, x in enumerate(content, 1):
            if needle in x.lower():
                hits.append("%s:%d: %s" % (rel, i, x.strip()[:200]))
                if len(hits) >= limit:
                    return "\n".join(hits) + "\n(stopped at %d matches; search for something more exact)" % limit
    return "\n".join(hits) or "No matches for %r." % text


# ---------------------------------------------------------------- checking a change

def check(rel, text):
    """None if the new file looks right, else a plain reason."""
    suffix = Path(rel).suffix.lower()
    if suffix == ".py":
        try:
            ast.parse(text, rel, feature_version=(3, 9))
            compile(text, rel, "exec")
        except SyntaxError as e:
            return "Python error in %s line %s: %s (%s)" % (rel, e.lineno, e.msg, (e.text or "").strip()[:120])
        except ValueError as e:
            return "Python error in %s: %s" % (rel, e)
    elif suffix == ".json":
        try:
            json.loads(text)
        except ValueError as e:
            return "JSON error in %s: %s" % (rel, e)
    elif suffix in (".html", ".js"):
        script = text if suffix == ".js" else "\n;\n".join(re.findall(r"<script>(.*?)</script>", text, re.S))
        node = shutil.which("node")
        if script.strip() and node:
            tmp = BACKUPS / "check.js"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(script, "utf-8")
            try:
                r = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True, timeout=30,
                                   stdin=subprocess.DEVNULL)
                if r.returncode:
                    err = [x for x in (r.stderr or "").splitlines() if x.strip()]
                    return "Script error in %s: %s" % (rel, " / ".join(err[:4])[:500])
            except (OSError, subprocess.SubprocessError):
                pass
        if suffix == ".html" and text.count("<script>") != text.count("</script>"):
            return "%s has a <script> without its </script>." % rel
    return None


def plan(rel, find=None, replace=None, new_content=None):
    """Work out one change without writing it. Returns a dict for the card."""
    p, rel = inside(rel)
    old = p.read_text("utf-8", "replace") if p.exists() else ""
    if new_content is not None:
        new = new_content
    elif find:
        n = old.count(find)
        if n != 1:
            raise SelfEditError("The find text appears %d times in %s; it must appear exactly once (include a "
                                "little more of the line around it)." % (n, rel))
        new = old.replace(find, replace or "", 1)
    else:
        raise SelfEditError("Give either find + replace, or new_content.")
    if new == old:
        raise SelfEditError("That change wouldn't change anything.")
    problem = check(rel, new)
    if problem:
        raise SelfEditError(problem + " Nothing was changed. Fix the change and suggest it again.")
    diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "before: " + rel,
                                        "after: " + rel, n=3))
    touched = [x[1:] for x in diff.splitlines() if x[:1] in "+-" and not x.startswith(("+++", "---"))]
    safety = sorted({m.group(0) for x in touched for m in [SAFETY.search(x)] if m}, key=str.lower)
    return {"path": rel, "new": new, "existed": p.exists(), "diff": diff, "safety": safety,
            "added": sum(1 for x in diff.splitlines() if x.startswith("+") and not x.startswith("+++")),
            "removed": sum(1 for x in diff.splitlines() if x.startswith("-") and not x.startswith("---"))}


# ---------------------------------------------------------------- writing and undoing

def _history():
    try:
        return json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return []


def _save_history(hist):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_name(HISTORY.name + ".tmp")
    tmp.write_text(json.dumps(hist, indent=2))
    tmp.replace(HISTORY)


def history():
    return _history()


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, "utf-8")
    try:
        shutil.copymode(str(p), str(tmp))
    except OSError:
        pass
    tmp.replace(p)


def apply(changes, note, undo_of=None):
    """Write [(rel, new_text or None to delete)] after backing up the old files. Returns the history entry."""
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    folder = BACKUPS / stamp
    n = 2
    while folder.exists():
        folder = BACKUPS / ("%s_%d" % (stamp, n))
        n += 1
    entries = []
    for rel, text in changes:
        p, rel = inside(rel)
        if text is not None:
            problem = check(rel, text)
            if problem:
                raise SelfEditError(problem + " Nothing was changed.")
        existed = p.exists()
        if existed:
            keep = folder / "before" / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(keep))
        if text is not None:
            after = folder / "after" / rel
            after.parent.mkdir(parents=True, exist_ok=True)
            after.write_text(text, "utf-8")
        entries.append({"path": rel, "existed": existed, "deleted": text is None})
    for (rel, text), e in zip(changes, entries):
        p = HERE / e["path"]
        if text is None:
            if p.exists():
                p.unlink()
        else:
            _write(p, text)
    hist = _history()
    entry = {"id": (hist[-1]["id"] + 1) if hist else 1, "time": stamp, "files": entries, "backup": str(folder),
             "note": note or "", "undo_of": undo_of, "undone_by": None}
    hist.append(entry)
    if undo_of:
        for h in hist:
            if h["id"] == undo_of:
                h["undone_by"] = entry["id"]
    _save_history(hist)
    return entry


def undo(entry_id):
    """Put back the files from before one change (the current files are backed up first). Returns a message."""
    hist = _history()
    h = next((x for x in hist if str(x["id"]) == str(entry_id)), None)
    if not h:
        raise SelfEditError("There's no PatchBay change #%s." % entry_id)
    if h.get("undone_by"):
        raise SelfEditError("Change #%s was already undone (by #%s)." % (h["id"], h["undone_by"]))
    changes = []
    for f in h["files"]:
        if f["existed"]:
            src = Path(h["backup"]) / "before" / f["path"]
            if not src.exists():
                raise SelfEditError("The backup of %s from before change #%s is missing." % (f["path"], h["id"]))
            changes.append((RENAMED.get(f["path"], f["path"]), src.read_text("utf-8", "replace")))
        else:
            changes.append((RENAMED.get(f["path"], f["path"]), None))
    e = apply(changes, "undo of #%s" % h["id"], undo_of=h["id"])
    return "Put back %s as it was before change #%s (saved as #%s). Restart PatchBay to use it." % (
        ", ".join(f["path"] for f in h["files"]), h["id"], e["id"])


def pending_restart(started_at):
    """Changes to PatchBay's code since it started (so the page can offer a restart)."""
    out = []
    for h in _history():
        try:
            t = time.mktime(time.strptime(h["time"][:19], "%Y-%m-%d_%H-%M-%S"))
        except ValueError:
            continue
        if t >= started_at - 1:
            out.append(h)
    return out


def recover(error, where=()):
    """Called by patchbay.py when PatchBay can't start. Undoes the newest change if it came in the last day, hasn't
    been undone, and changed one of the files the error happened in (where). Returns a message, or None."""
    hist = [h for h in _history() if not h.get("undone_by") and not h.get("undo_of")]
    if not hist:
        return None
    h = hist[-1]
    broken = set()
    for w in where:
        try:
            broken.add(Path(w).resolve().relative_to(HERE).as_posix())
        except (ValueError, OSError):
            pass
    if not broken & {f["path"] for f in h["files"]}:
        return None
    try:
        age = time.time() - time.mktime(time.strptime(h["time"][:19], "%Y-%m-%d_%H-%M-%S"))
    except ValueError:
        age = 0
    if age > 86400:
        return None
    try:
        msg = undo(h["id"])
    except SelfEditError as e:
        return "PatchBay couldn't start (%s), and putting back the last change didn't work either: %s" % (error, e)
    text = ("PatchBay couldn't start after change #%s (%s), so it put the old version back by itself. %s"
            % (h["id"], error, msg.replace(" Restart PatchBay to use it.", "")))
    try:
        RECOVERED.write_text(json.dumps({"time": time.time(), "message": text}))
    except OSError:
        pass
    return text


def last_recovery(days=2):
    """The message from the last time PatchBay put itself back, if it was recent."""
    try:
        r = json.loads(RECOVERED.read_text())
    except (OSError, ValueError):
        return None
    return r["message"] if time.time() - r.get("time", 0) < days * 86400 else None


def edited_files():
    """Files PatchBay changed in itself that are still changed (for the updater)."""
    out = set()
    for h in _history():
        for f in h["files"]:
            out.add(f["path"])
    return out
