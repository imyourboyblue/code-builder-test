"""
Updating Patchbay with one button: it asks GitHub (through git) whether there are newer changes, gets them, and
restarts itself. It never throws away the user's own changes: if a Patchbay file was edited by hand, it says which
one and stops. Settings, passwords, backups and chats aren't part of the code, so updates never touch them.
Only uses Python's built-in parts and the git command that's already used for "git pull".
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _git(*args, timeout=60):
    try:
        r = subprocess.run(["git", "-C", str(HERE)] + list(args), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except FileNotFoundError:
        raise RuntimeError("git isn't installed, so Patchbay can't update itself. Install the Xcode command line "
                           "tools (macOS will offer them the first time you type git in Terminal).")
    except subprocess.TimeoutExpired:
        raise RuntimeError("git took too long (is the internet connection working?).")
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def status(fetch=True):
    """{current, branch, behind, ahead, changes:[subjects], edited:[files], error}."""
    out = {"current": None, "branch": None, "behind": 0, "ahead": 0, "changes": [], "edited": [], "error": None,
           "checked": time.time()}
    try:
        code, top, err = _git("rev-parse", "--show-toplevel")
        if code:
            out["error"] = "This Patchbay folder wasn't downloaded with git, so it can't update itself."
            return out
        out["current"] = _git("log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d %H:%M")[1]
        out["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")[1]
        if fetch:
            code, _, err = _git("fetch", "--quiet", timeout=90)
            if code:
                out["error"] = "Couldn't reach GitHub to check for updates: " + (err.splitlines() or ["no answer"])[-1]
                return out
        code, counts, err = _git("rev-list", "--left-right", "--count", "HEAD...@{u}")
        if code:
            out["error"] = "This copy isn't linked to the GitHub version, so it can't update itself."
            return out
        ahead, behind = (int(x) for x in counts.split())
        out["ahead"], out["behind"] = ahead, behind
        if behind:
            out["changes"] = _git("log", "HEAD..@{u}", "--format=%s", "-n", "30")[1].splitlines()
        out["edited"] = [line[3:] for line in _git("status", "--porcelain", "--untracked-files=no")[1].splitlines()]
    except RuntimeError as e:
        out["error"] = str(e)
    return out


def update():
    """Get the newer version. Returns a message. Raises RuntimeError with a plain reason if it can't."""
    st = status()
    if st["error"]:
        raise RuntimeError(st["error"])
    if not st["behind"]:
        return "Patchbay is already up to date."
    if st["edited"]:
        raise RuntimeError("These Patchbay files were changed on this Mac, so updating could overwrite your "
                           "changes: %s. Send me the list and I'll help." % ", ".join(st["edited"][:8]))
    code, out, err = _git("pull", "--ff-only", "--quiet", timeout=120)
    if code:
        raise RuntimeError("The update didn't work: " + ((err or out).splitlines() or ["unknown error"])[-1])
    return "Updated (%d change%s)." % (st["behind"], "" if st["behind"] == 1 else "s")


def restart_soon(delay=1.0):
    """Start this same Patchbay again (same Python, same options) after the page got its answer."""
    def go():
        time.sleep(delay)
        args = [a for a in sys.argv if a != "--install-app"]
        if "--no-browser" not in args:
            args.append("--no-browser")   # the page that's already open reconnects by itself
        os.environ["PATCHBAY_RESTARTED"] = "1"
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + args)
    threading.Thread(target=go, daemon=True).start()
