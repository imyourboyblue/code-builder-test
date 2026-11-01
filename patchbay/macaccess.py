"""
The chat working with files on this Mac and with Apple Calendar, only where you allowed it.

Files: you approve folders in 🩺 Setup → Mac. The chat may look and read inside them freely. Every change
(write, rename/move, delete) shows a card first; the old version is backed up and the change can be undone.
Deleting moves to the Trash. Secrets are refused even inside an approved folder (Keychains, .ssh, PatchBay's own
password and key files...), and system folders can't be approved at all.

Calendar: off until you turn it on in Setup. Reading is free; adding, changing or deleting an event shows a card
first, and can be undone. It talks to the Calendar app through AppleScript; macOS asks once for permission.

Nothing from your files or calendar is sent to Claude or used in web searches (see PRIVATE_TOOLS in server.py).
"""

import datetime as dt
import difflib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HISTORY = HERE / "data-cache" / "mac-changes.json"
BACKUPS = HERE / "data-cache" / "mac-backups"
HOME = Path.home()

NEVER_APPROVE = [Path("/"), HOME, Path("/System"), Path("/Library"), Path("/usr"), Path("/bin"), Path("/sbin"),
                 Path("/private"), Path("/etc"), Path("/var"), Path("/Applications"), HOME / "Library"]
SECRET_PARTS = {".ssh", ".gnupg", ".aws", ".kube", "Keychains", ".docker", ".netrc", ".git-credentials"}
SECRET_NAMES = {"config.json", "access.json", "claude.json", "claude-aliases.json", "id_rsa", "id_ed25519",
                ".env", "credentials", "credentials.json"}
MAX_READ = 400_000


class AccessError(Exception):
    pass


# ---------------------------------------------------------------- folders

def folders(cfg):
    return [Path(os.path.expanduser(f)) for f in (cfg.get("mac_folders") or [])]


def check_folder(path):
    """A folder that may be approved, or AccessError."""
    p = Path(os.path.expanduser(str(path or "").strip())).resolve()
    if not p.is_dir():
        raise AccessError("There's no folder at %s." % path)
    for bad in NEVER_APPROVE:
        if p == bad.resolve():
            raise AccessError("%s is too big or too important to hand over. Pick a folder inside it, like "
                              "~/Documents or a project folder." % p)
    if any(x in SECRET_PARTS for x in p.parts) or str(p).startswith(str((HOME / "Library").resolve())):
        raise AccessError("That folder holds private or system files, so it can't be approved.")
    return p


def resolve(cfg, path, must_exist=True):
    """The real path, if it's inside an approved folder and isn't a secret. Otherwise AccessError."""
    ok = folders(cfg)
    if not ok:
        raise AccessError("No Mac folders are approved yet. The user can add some in 🩺 Setup → Mac.")
    raw = os.path.expanduser(str(path or "").strip())
    p = Path(raw) if os.path.isabs(raw) else ok[0] / raw
    p = p.resolve()
    if must_exist and not p.exists():
        raise AccessError("There's nothing at %s." % p)
    if not any(p == f.resolve() or f.resolve() in p.parents for f in ok):
        raise AccessError("%s isn't inside a folder the user approved (%s). They can add folders in 🩺 Setup → Mac "
                          "access." % (p, ", ".join(str(f) for f in ok)))
    if any(x in SECRET_PARTS for x in p.parts) or p.name in SECRET_NAMES or p.suffix in (".keychain", ".keychain-db",
                                                                                         ".pem", ".key", ".p12"):
        raise AccessError("%s looks like a password or key file, so PatchBay won't touch it." % p.name)
    return p


def listing(cfg, path, limit=300):
    p = resolve(cfg, path or "")          # no path: the first approved folder
    if p.is_file():
        st = p.stat()
        return "%s: a file, %d bytes, changed %s" % (p, st.st_size, time.strftime("%Y-%m-%d %H:%M",
                                                                                    time.localtime(st.st_mtime)))
    rows = []
    for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:limit]:
        if c.name.startswith(".") and c.name != ".gitignore":
            continue
        try:
            rows.append("%s%s  %s" % (c.name, "/" if c.is_dir() else "", "" if c.is_dir() else _size(c.stat().st_size)))
        except OSError:
            pass
    return "%s\n%s" % (p, "\n".join(rows) or "(empty)")


def find(cfg, text, limit=80):
    """Files whose names contain the text, in all approved folders."""
    out, low = [], text.lower()
    for f in folders(cfg):
        for root, dirs, files in os.walk(f):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SECRET_PARTS and d != "node_modules"]
            for n in files + dirs:
                if low in n.lower():
                    out.append(os.path.join(root, n))
                    if len(out) >= limit:
                        return out
    return out


def read(cfg, path):
    p = resolve(cfg, path)
    if p.is_dir():
        return listing(cfg, path)
    if p.suffix.lower() == ".pdf":
        return _pdf_text(p)
    data = p.read_bytes()[:MAX_READ]
    if b"\0" in data[:4000]:
        return "%s is a binary file (%s), not text." % (p.name, _size(p.stat().st_size))
    text = data.decode("utf-8", "replace")
    return text + ("\n… (cut at %d characters)" % MAX_READ if p.stat().st_size > MAX_READ else "")


def _pdf_text(p):
    """A PDF's text, with the Mac's own PDF reader (PDFKit, through JavaScript for Automation)."""
    script = ('ObjC.import("PDFKit");var d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(%s));'
              'd?d.string.js:"";' % json.dumps(str(p)))
    try:
        r = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        return (r.stdout or "").strip()[:MAX_READ] or "Couldn't read text from that PDF (it may be scanned pictures)."
    except (OSError, subprocess.SubprocessError) as e:
        return "Couldn't read that PDF: %s" % e


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# ---- changes (planned for a card, then applied)

def plan_write(cfg, path, content):
    p = resolve(cfg, path, must_exist=False)
    if p.is_dir():
        raise AccessError("%s is a folder." % p)
    resolve(cfg, str(p.parent))      # the folder it goes in must be approved too
    old = p.read_text("utf-8", "replace") if p.exists() else ""
    diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), "before: %s" % p.name,
                                        "after: %s" % p.name, n=3)) if p.exists() else "New file:\n" + content[:6000]
    return {"action": "write", "path": str(p), "content": content}, diff


def plan_move(cfg, path, to):
    p, q = resolve(cfg, path), resolve(cfg, to, must_exist=False)
    if q.exists():
        raise AccessError("Something is already at %s." % q)
    resolve(cfg, str(q.parent))
    return {"action": "move", "path": str(p), "to": str(q)}, "Move %s\n  to %s" % (p, q)


def plan_delete(cfg, path):
    p = resolve(cfg, path)
    if any(p == f.resolve() for f in folders(cfg)):
        raise AccessError("That's an approved folder itself; remove it from Setup instead.")
    return {"action": "delete", "path": str(p)}, "Move to the Trash: %s" % p


def _history():
    try:
        return json.loads(HISTORY.read_text())
    except (OSError, ValueError):
        return []


def _save(hist):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(hist[-500:], indent=1))


def apply(cfg, change, note=""):
    """Do a planned change (checked again first). Returns a plain result with the change number for Undo."""
    a = change["action"]
    hist = _history()
    entry = {"id": (hist[-1]["id"] + 1) if hist else 1, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
             "kind": "file", "action": a, "path": change["path"], "note": note}
    if a == "write":
        p = resolve(cfg, change["path"], must_exist=False)
        if p.exists():
            bk = BACKUPS / time.strftime("%Y%m%d-%H%M%S") / p.name
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(bk))
            entry["backup"] = str(bk)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".patchbay-tmp")
        tmp.write_text(change["content"], encoding="utf-8")
        tmp.replace(p)
        result = "Saved %s%s." % (p, " (the old version is backed up)" if entry.get("backup") else " (new file)")
    elif a == "move":
        p, q = resolve(cfg, change["path"]), resolve(cfg, change["to"], must_exist=False)
        shutil.move(str(p), str(q))
        entry["to"] = str(q)
        result = "Moved %s to %s." % (p, q)
    elif a == "delete":
        p = resolve(cfg, change["path"])
        entry["trash"] = to_trash(p)
        result = "Moved %s to the Trash." % p
    else:
        raise AccessError("Unknown change.")
    hist.append(entry)
    _save(hist)
    return "%s (Change #%d: it can be undone.)" % (result, entry["id"])


def to_trash(p):
    """Move to the Trash the Mac's own way (Finder's Put Back works too). Returns where it ended up."""
    if os.path.exists("/usr/bin/osascript"):
        script = ('ObjC.import("Foundation");var r=Ref();var ok=$.NSFileManager.defaultManager.'
                  'trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(%s),r,null);ok?r[0].path.js:"";'
                  % json.dumps(str(p)))
        try:
            out = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script], capture_output=True,
                                 text=True, timeout=60, stdin=subprocess.DEVNULL).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out and not p.exists():
            return out
    trash = HOME / ".Trash"
    trash.mkdir(exist_ok=True)
    dest = trash / p.name
    if dest.exists():
        dest = trash / ("%s %s%s" % (p.stem, time.strftime("%H.%M.%S"), p.suffix))
    shutil.move(str(p), str(dest))
    return str(dest)


def undo_plan(n):
    e = next((h for h in _history() if h["id"] == int(n)), None)
    if not e or e.get("undone"):
        raise AccessError("There's no change #%s to undo." % n)
    words = {"write": "put back the old version of %s" % e["path"] if e.get("backup") else "delete the new file %s"
             % e["path"], "move": "move %s back to %s" % (e.get("to"), e["path"]),
             "delete": "take %s back out of the Trash" % e["path"],
             "event_add": "delete the event that was added", "event_edit": "put the event back the way it was",
             "event_delete": "add the deleted event back"}.get(e["action"], "undo it")
    return e, "Undo change #%d: %s" % (e["id"], words)


def undo(n):
    e, _ = undo_plan(n)
    a = e["action"]
    if a == "write":
        if e.get("backup"):
            shutil.copy2(e["backup"], e["path"])
        else:
            to_trash(Path(e["path"]))
    elif a == "move":
        shutil.move(e["to"], e["path"])
    elif a == "delete":
        if os.path.exists(e["path"]):
            raise AccessError("Something new is already at %s." % e["path"])
        try:
            shutil.move(e["trash"], e["path"])
        except OSError:
            raise AccessError("Couldn't take it out of the Trash (it may have been emptied, or macOS said no). If "
                              "it's still there, open the Trash in Finder, right-click it and choose Put Back.")
    elif a == "event_add":
        cal_delete(e["uid"])
    elif a == "event_edit":
        cal_edit(e["uid"], e["before"])
    elif a == "event_delete":
        cal_add(e["before"])
    hist = _history()
    for h in hist:
        if h["id"] == e["id"]:
            h["undone"] = True
    _save(hist)
    return "Undid change #%d." % e["id"]


def recent(limit=15):
    return [h for h in _history()[-limit:]][::-1]


# ---------------------------------------------------------------- Apple Calendar (AppleScript)

AS_HELPERS = '''
on mk(y, m, d, hh, mi)
  set t to current date
  set day of t to 1
  set year of t to y
  set month of t to m
  set day of t to d
  set hours of t to hh
  set minutes of t to mi
  set seconds of t to 0
  return t
end mk
on iso(t)
  return (year of t as string) & "-" & text -2 thru -1 of ("0" & ((month of t) as integer)) & "-" & ¬
    text -2 thru -1 of ("0" & (day of t)) & " " & text -2 thru -1 of ("0" & (hours of t)) & ":" & ¬
    text -2 thru -1 of ("0" & (minutes of t))
end iso
on clean(s)
  try
    if s is missing value then return ""
    set AppleScript's text item delimiters to {tab, return, linefeed}
    set parts to text items of (s as string)
    set AppleScript's text item delimiters to " "
    set r to parts as string
    set AppleScript's text item delimiters to ""
    return r
  on error
    return ""
  end try
end clean
'''


def _osa(script, timeout=60):
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", AS_HELPERS + script], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise AccessError("Calendar only works on a Mac.")
    except subprocess.TimeoutExpired:
        raise AccessError("Calendar took too long to answer.")
    if r.returncode:
        err = (r.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise AccessError("macOS hasn't allowed PatchBay to use Calendar. Open System Settings → Privacy & "
                              "Security → Automation (and Calendars) and allow it for Python or PatchBay.")
        raise AccessError("Calendar said: %s" % err[-300:])
    return r.stdout


def _when(text):
    """'2026-11-03 14:30' or '2026-11-03' -> AppleScript mk(...) call."""
    text = str(text or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            t = dt.datetime.strptime(text[:19] if "%S" in fmt else text[:16] if "%M" in fmt else text[:10], fmt)
            return "my mk(%d, %d, %d, %d, %d)" % (t.year, t.month, t.day, t.hour, t.minute)
        except ValueError:
            continue
    raise AccessError("Give dates like 2026-11-03 14:30 (got %r)." % text)


def _q(s):
    return '"%s"' % str(s or "").replace("\\", "\\\\").replace('"', '\\"')


def choose_folder():
    """Ask the Mac to show its own "choose a folder" window. The chosen folder, or None if cancelled."""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", 'POSIX path of (choose folder with prompt "Pick a folder '
                            'PatchBay may use")'], capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise AccessError("The Mac's folder picker didn't open. Type the folder's path instead.")
    if r.returncode:
        return None
    return r.stdout.strip().rstrip("/") or None


def cal_calendars():
    out = _osa('tell application "Calendar"\nset r to ""\nrepeat with c in calendars\nset r to r & (name of c) & '
               'linefeed\nend repeat\nreturn r\nend tell')
    return [x for x in out.splitlines() if x.strip()]


def cal_events(start, end, calendar=None, text=None):
    """Events between two dates: [{"uid", "calendar", "title", "start", "end", "location", "all_day"}]."""
    which = "{calendar %s}" % _q(calendar) if calendar else "calendars"
    script = '''
set d1 to %s
set d2 to %s
set r to ""
tell application "Calendar"
  repeat with c in %s
    set evs to (every event of c whose start date < d2 and end date > d1)
    repeat with e in evs
      set r to r & (uid of e) & tab & (name of c) & tab & my clean(summary of e) & tab & my iso(start date of e) & tab & my iso(end date of e) & tab & my clean(location of e) & tab & (allday event of e as string) & linefeed
    end repeat
  end repeat
end tell
return r''' % (_when(start), _when(end), which)
    rows = []
    for line in _osa(script, timeout=120).splitlines():
        f = line.split("\t")
        if len(f) >= 7:
            ev = {"uid": f[0], "calendar": f[1], "title": f[2], "start": f[3], "end": f[4], "location": f[5],
                  "all_day": f[6] == "true"}
            if not text or text.lower() in (ev["title"] + ev["location"]).lower():
                rows.append(ev)
    return sorted(rows, key=lambda e: e["start"])


def cal_get(uid):
    script = '''
set r to ""
tell application "Calendar"
  repeat with c in calendars
    set evs to (every event of c whose uid is %s)
    repeat with e in evs
      set r to (uid of e) & tab & (name of c) & tab & my clean(summary of e) & tab & my iso(start date of e) & tab & my iso(end date of e) & tab & my clean(location of e) & tab & (allday event of e as string) & tab & my clean(description of e)
    end repeat
  end repeat
end tell
return r''' % _q(uid)
    f = _osa(script, timeout=120).rstrip("\n").split("\t")
    if len(f) < 7:
        raise AccessError("There's no event with that id. Look it up again with calendar_events.")
    return {"uid": f[0], "calendar": f[1], "title": f[2], "start": f[3], "end": f[4], "location": f[5],
            "all_day": f[6] == "true", "notes": f[7] if len(f) > 7 else ""}


def cal_add(ev):
    props = ["summary:%s" % _q(ev.get("title")), "start date:%s" % _when(ev.get("start")),
             "end date:%s" % _when(ev.get("end") or ev.get("start"))]
    if ev.get("location"):
        props.append("location:%s" % _q(ev["location"]))
    if ev.get("notes"):
        props.append("description:%s" % _q(ev["notes"]))
    if ev.get("all_day"):
        props.append("allday event:true")
    cal = ev.get("calendar") or cal_calendars()[0]
    out = _osa('tell application "Calendar"\nset e to make new event at end of events of calendar %s with properties '
               '{%s}\nreturn uid of e\nend tell' % (_q(cal), ", ".join(props)))
    return out.strip()


def cal_edit(uid, fields):
    sets = []
    for k, prop in (("title", "summary"), ("location", "location"), ("notes", "description")):
        if k in fields:
            sets.append("set %s of e to %s" % (prop, _q(fields[k])))
    for k, prop in (("start", "start date"), ("end", "end date")):
        if fields.get(k):
            sets.append("set %s of e to %s" % (prop, _when(fields[k])))
    if "all_day" in fields:
        sets.append("set allday event of e to %s" % ("true" if fields["all_day"] else "false"))
    _osa('tell application "Calendar"\nrepeat with c in calendars\nset evs to (every event of c whose uid is %s)\n'
         'repeat with e in evs\n%s\nend repeat\nend repeat\nend tell' % (_q(uid), "\n".join(sets)), timeout=120)


def cal_delete(uid):
    _osa('tell application "Calendar"\nrepeat with c in calendars\ndelete (every event of c whose uid is %s)\n'
         'end repeat\nend tell' % _q(uid), timeout=120)


def cal_apply(change, note=""):
    """Do a planned calendar change; remember how to undo it."""
    hist = _history()
    entry = {"id": (hist[-1]["id"] + 1) if hist else 1, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
             "kind": "calendar", "action": change["action"], "path": change.get("title") or "", "note": note}
    if change["action"] == "event_add":
        entry["uid"] = cal_add(change["event"])
        result = "Added \"%s\" on %s." % (change["event"].get("title"), change["event"].get("start"))
    elif change["action"] == "event_edit":
        entry["uid"], entry["before"] = change["uid"], change["before"]
        cal_edit(change["uid"], change["fields"])
        result = "Changed \"%s\"." % change["before"].get("title")
    else:
        entry["before"] = change["before"]
        cal_delete(change["uid"])
        result = "Deleted \"%s\" (%s)." % (change["before"].get("title"), change["before"].get("start"))
    hist.append(entry)
    _save(hist)
    return "%s (Change #%d: it can be undone.)" % (result, entry["id"])
