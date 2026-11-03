"""
What PatchBay started, so it can stop exactly those things when you quit PatchBay.

PatchBay writes a line in data-cache/started.json each time it starts something that should only run while
PatchBay runs, and takes it off again when that thing stops:

  ollama           Ollama, if PatchBay started it (not if it was already running)
  colima           Colima's Linux machine (Docker for the Linux sandboxes), if PatchBay started it
  apple_container  Apple's container tool's background service, if PatchBay started it
  sandbox          a sandbox's Linux container (always PatchBay's)
  process          a build or command PatchBay is running right now

When PatchBay quits (the app's Quit or window close, Ctrl+C, or the Mac asking it to stop) on_quit() stops those
right away and prints one "Stopped: ..." line. If PatchBay crashed instead, the next start finds the leftovers and
stops them (sweep()). Things you started yourself are never on the list, so they are never stopped.
Never touched: start at login, backups, settings and anything else meant to stay.

Setup → App → When PatchBay quits has the switches (all on by default).
"""
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

FILE = Path(__file__).resolve().parent / "data-cache" / "started.json"
KINDS = ("process", "ollama", "sandbox", "colima", "apple_container")
# Settings (in the main settings file). All default to True: stop it when PatchBay quits.
SETTINGS = {
    "quit_stops_sandboxes": "Stop the sandboxes' Linux containers",
    "quit_stops_linux_machine": "Stop the Linux machine (Colima or Apple's container tool) if PatchBay started it",
    "quit_with_app": "Quitting the PatchBay app quits PatchBay too, even when it started at login",
}
_LOCK = threading.RLock()
_PROCS = {}          # pid -> Popen, the builds and commands running right now
_DONE = threading.Event()


def setting(cfg, key):
    return bool((cfg or {}).get(key, True))


# ---- the list

def _read():
    try:
        rows = json.loads(FILE.read_text())
        return [r for r in rows if isinstance(r, dict) and r.get("kind") in KINDS and r.get("key")]
    except (OSError, ValueError):
        return []


def _write(rows):
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_name(FILE.name + ".tmp")
        tmp.write_text(json.dumps(rows, indent=1))
        tmp.replace(FILE)
    except OSError:
        pass


def record(kind, key, what, **extra):
    """PatchBay just started this. Owned by this PatchBay (its process id)."""
    with _LOCK:
        rows = [r for r in _read() if not (r["kind"] == kind and r["key"] == key)]
        rows.append(dict(extra, kind=kind, key=str(key), what=what, owner=os.getpid(), at=int(time.time())))
        _write(rows)


def forget(kind, key=None):
    """It stopped (or it's yours now). key None: every one of that kind."""
    with _LOCK:
        rows = _read()
        keep = [r for r in rows if not (r["kind"] == kind and (key is None or r["key"] == str(key)))]
        if len(keep) != len(rows):
            _write(keep)


def started(kind=None):
    rows = _read()
    return [r for r in rows if kind is None or r["kind"] == kind]


def has(kind, key=None):
    return any(key is None or r["key"] == str(key) for r in started(kind))


def listing():
    """What PatchBay started and is still running, for Setup."""
    return [{"kind": r["kind"], "what": r["what"]} for r in started()]


# ---- builds and commands that are running

class track:
    """with services.track(proc, "build of ftpd"): ... (the process must have start_new_session=True, so stopping it
    also stops everything it started)."""

    def __init__(self, proc, what, cmd=""):
        self.proc, self.what, self.cmd = proc, what, cmd

    def __enter__(self):
        _PROCS[self.proc.pid] = self.proc
        record("process", self.proc.pid, self.what, pgid=self.proc.pid, cmd=_args_of(self.proc.pid) or self.cmd)
        return self.proc

    def __exit__(self, *a):
        _PROCS.pop(self.proc.pid, None)
        forget("process", self.proc.pid)
        return False


def _kill_group(pgid, alive=None):
    """Polite stop, then a firm one after 2 seconds. Returns True if it's gone."""
    alive = alive or (lambda: _group_alive(pgid))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return True
        end = time.time() + 2
        while time.time() < end:
            if not alive():
                return True
            time.sleep(0.1)
    return not alive()


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def _args_of(pid):
    try:
        return subprocess.run(["ps", "-o", "args=", "-p", str(int(pid))], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _is_patchbay(pid):
    """Whether this process id is a PatchBay that is still running (not a new program that got the same number)."""
    if int(pid) == os.getpid():
        return True
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return "patchbay" in _args_of(pid).lower()


def _same_process(row):
    """A leftover build is only stopped if it is still the same program PatchBay started."""
    now = _args_of(row["key"])
    return bool(now) and bool(row.get("cmd")) and now == row["cmd"]


# ---- stopping

def _stop_row(row, cfg, stop_ollama=None):
    """Stop one thing on the list. Returns its name if stopped, "" if it was left running (and is yours now)."""
    kind = row["kind"]
    if kind == "process":
        p = _PROCS.get(int(row["key"]))
        if p is not None:
            _kill_group(p.pid, alive=lambda: p.poll() is None)
        elif _same_process(row):
            _kill_group(int(row.get("pgid") or row["key"]))
        else:
            forget("process", row["key"])
            return ""
        forget("process", row["key"])
        return row["what"]
    import sandbox
    if kind == "sandbox":
        if not setting(cfg, "quit_stops_sandboxes"):
            forget(kind, row["key"])
            return ""
        sandbox.stop_container_named(row["key"])
        forget(kind, row["key"])
        return row["what"]
    if kind in ("colima", "apple_container"):
        if not setting(cfg, "quit_stops_linux_machine"):
            forget(kind, row["key"])
            return ""
        ok = sandbox.stop_engine(kind)
        forget(kind, row["key"])
        return row["what"] if ok else ""
    return ""


def on_quit(cfg, ollama_close=None):
    """PatchBay is quitting: stop everything on the list, now. Prints and returns "Stopped: ..."."""
    if _DONE.is_set():
        return ""
    _DONE.set()
    stopped, notes = [], []
    rows = [r for r in started() if r["kind"] != "ollama" and _is_patchbay(r["owner"])]
    # 1. builds and commands first (they may be using a sandbox)
    for r in [r for r in rows if r["kind"] == "process"]:
        if _stop_row(r, cfg):
            stopped.append(r["what"])
    for p in list(_PROCS.values()):     # anything running that never made it to the file
        if p.poll() is None:
            _kill_group(p.pid, alive=lambda p=p: p.poll() is None)

    # 2. Ollama and the Linux side at the same time, so quitting is quick
    def ollama_side():
        if ollama_close is None:
            return
        try:
            msg = ollama_close(cfg)
        except Exception as e:
            msg = "Couldn't tidy up Ollama: %s" % e
        if msg:
            notes.append(msg)
        if msg.startswith("Ollama is stopped"):
            stopped.append("Ollama")
        elif msg.startswith("Unloaded"):
            stopped.append("the model in memory")

    def linux_side():
        for kind in ("sandbox", "colima", "apple_container"):
            for r in [r for r in rows if r["kind"] == kind]:
                try:
                    if _stop_row(r, cfg):
                        stopped.append(r["what"])
                except Exception as e:
                    notes.append("Couldn't stop %s: %s" % (r["what"], e))
    threads = [threading.Thread(target=f, daemon=True) for f in (ollama_side, linux_side)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    line = ("Stopped: %s." % ", ".join(stopped)) if stopped else "Nothing PatchBay started was still running."
    for n in notes:
        if not n.startswith("Ollama is stopped") and not n.startswith("Unloaded"):
            line += " " + n
    print(line)
    return line


def sweep(cfg, ollama_adopt=None):
    """PatchBay is starting. Anything a PatchBay that crashed (or was force quit) left running gets stopped now.
    Ollama is the exception: PatchBay is about to use it, so it carries on looking after it (ollama_adopt)."""
    stopped = []
    for r in started():
        if r["owner"] == os.getpid():          # restarted after an update: same PatchBay, nothing left over
            if r["kind"] == "ollama" and ollama_adopt:
                ollama_adopt(cfg, r)
            continue
        if _is_patchbay(r["owner"]):             # another PatchBay is still running: leave its things alone
            continue
        if r["kind"] == "ollama":
            if ollama_adopt:
                ollama_adopt(cfg, r)
            continue
        try:
            if _stop_row(r, cfg):
                stopped.append(r["what"])
        except Exception as e:
            print("Couldn't stop %s left over from last time: %s" % (r["what"], e))
    if stopped:
        print("Left over from last time (PatchBay didn't quit normally), stopped: %s." % ", ".join(stopped))
    return stopped
