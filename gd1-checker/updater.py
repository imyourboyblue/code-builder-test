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
        out["edited"] = [line.strip().split(None, 1)[-1] for line in
                         _git("status", "--porcelain", "--untracked-files=no")[1].splitlines() if line.strip()]
        out["own"] = bool(out["edited"]) and bool(_own_changes(out["edited"]))
        out["diff"] = local_diff() if out["edited"] else ""
    except RuntimeError as e:
        out["error"] = str(e)
    return out


def update(keep_local=False):
    """Get the newer version. Returns a message. Raises RuntimeError with a plain reason if it can't.
    Files changed on this Mac are never thrown away: changes Patchbay made to itself, or (with keep_local) any other
    change, are first saved to data-cache/local-changes/<time>/ and set aside with git stash, then put back on top
    of the update. If they clash with the update, the updated files are used and the saved copies stay."""
    st = status()
    if st["error"]:
        raise RuntimeError(st["error"])
    if not st["behind"]:
        return "Patchbay is already up to date."
    mine = _own_changes(st["edited"])
    if st["edited"] and not mine and not keep_local:
        raise RuntimeError("LOCAL_CHANGES: These Patchbay files were changed on this Mac: %s."
                           % ", ".join(st["edited"][:8]))
    keep = None
    if st["edited"]:
        keep = _save_copies(st["edited"])
        code, out, err = _git("stash", "push", "--quiet", "-m", "Changes on this Mac before an update")
        if code:
            raise RuntimeError("Couldn't set the changes aside for the update: " + (err or out))
    code, out, err = _git("pull", "--ff-only", "--quiet", timeout=120)
    if code:
        if st["edited"]:
            _git("stash", "pop", "--quiet")
        raise RuntimeError("The update didn't work: " + ((err or out).splitlines() or ["unknown error"])[-1])
    done = "Updated (%d change%s)." % (st["behind"], "" if st["behind"] == 1 else "s")
    if st["edited"]:
        names = ", ".join(e.split("/")[-1] for e in st["edited"])
        code, out, err = _git("stash", "pop", "--quiet")
        if code:
            # They clash with the update: use the updated files; the changes stay saved (stash + copies).
            _git("reset", "--hard", "--quiet", "HEAD")
            return (done + " Your changes to %s clashed with the update, so the updated version is used. Your "
                    "version is saved in %s (and in git's stash), so nothing is lost." % (names, keep))
        done += " Your changes to %s were kept on top of the update (a copy is in %s)." % (names, keep)
    return done


def _save_copies(edited):
    """Copy the changed files and what changed (a diff) into data-cache/local-changes/<time>/."""
    folder = HERE / "data-cache" / "local-changes" / time.strftime("%Y-%m-%d_%H-%M-%S")
    folder.mkdir(parents=True, exist_ok=True)
    top = Path(_git("rev-parse", "--show-toplevel")[1] or HERE)
    for e in edited:
        src = top / e.strip().strip('"').split(" -> ")[-1]
        if src.is_file():
            (folder / src.name).write_bytes(src.read_bytes())
    (folder / "changes.diff").write_text(_git("diff", "HEAD")[1] + "\n")
    return str(folder.relative_to(HERE))


def local_diff(limit=6000):
    """What was changed on this Mac, as a diff (for the page)."""
    try:
        d = _git("diff", "HEAD")[1]
    except RuntimeError:
        return ""
    return d if len(d) <= limit else d[:limit] + "\n… (%d more characters)" % (len(d) - limit)


def _own_changes(edited):
    """The edited files, if every one of them was changed by Patchbay itself (selfedit); else []."""
    try:
        import selfedit
        own = selfedit.edited_files()
    except Exception:
        return []
    top = _git("rev-parse", "--show-prefix")[1]   # e.g. "gd1-checker/"
    names = [e.strip().strip('"') for e in edited]
    names = [n.split(" -> ")[-1] for n in names]
    rel = [n[len(top):] if top and n.startswith(top) else n for n in names]
    return rel if rel and all(r in own for r in rel) else []


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
