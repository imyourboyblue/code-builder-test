"""
Sending PatchBay's own changes to GitHub.

When PatchBay edits its own code (selfedit.py), the change only lives on this Mac. This sends those changes to your
GitHub repository, on their own branch ("patchbay-edits"), never on main, so they can't clash with other changes
there. You merge the branch on GitHub when you're happy with it.

Rules:
  - Only files PatchBay itself changed (in its edit history) and git tracks or may track. Your settings, passwords,
    backups, chats and device data are never sent (the same list selfedit refuses to edit, plus .gitignore).
  - Nothing is sent until you confirm, after seeing the list of files and the exact changes.
  - Your working copy isn't touched: the branch is built on the side (a separate git index), from what's on this Mac
    (your current version plus PatchBay's changes), and replaces the branch's previous contents.
  - It signs in with your Mac's own GitHub login if git push already works, or with a GitHub token you save here,
    kept in the Mac's Keychain.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

import keychain
import selfedit

HERE = Path(__file__).resolve().parent
BRANCH = "patchbay-edits"
TOKEN_ACCOUNT = "github-token"


class PushError(Exception):
    pass


def _git(*args, env=None, timeout=120, text=True):
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    e.update(env or {})
    try:
        r = subprocess.run(["git", "-C", str(HERE)] + list(args), capture_output=True, text=text, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=e)
    except FileNotFoundError:
        raise PushError("git isn't installed (Apple's command line tools include it).")
    except subprocess.TimeoutExpired:
        raise PushError("git took too long (is the internet working?).")
    return r.returncode, (r.stdout or "").rstrip(), (r.stderr or "").strip()     # rstrip: status lines start with a space


def _blocked(rel):
    parts = rel.split("/")
    return parts[-1] in selfedit.BLOCKED_NAMES or any(p in selfedit.BLOCKED_DIRS for p in parts[:-1])


def repo():
    """('owner/name', web address) of the 'origin' repository, or (None, None)."""
    code, url, _ = _git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]+([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url or "")
    return (m.group(1), "https://github.com/" + m.group(1)) if code == 0 and m else (None, None)


def changes():
    """PatchBay's own changes that are on this Mac: [{"path" (from the repo top), "rel", "state", "added", "removed"}]."""
    code, top, err = _git("rev-parse", "--show-prefix")
    top = top.strip()
    if code:
        raise PushError("This PatchBay folder wasn't downloaded with git, so it can't send changes to GitHub.")
    own = selfedit.edited_files()
    code, out, _ = _git("status", "--porcelain", "--untracked-files=all", "--", ".")
    found = []
    for line in out.splitlines():
        state, path = line[:2], line[3:].strip().strip('"').split(" -> ")[-1]
        rel = path[len(top):] if top and path.startswith(top) else path
        if rel not in own or _blocked(rel):
            continue
        if _git("check-ignore", "-q", "--", rel)[0] == 0:
            continue
        added = removed = 0
        if "?" in state:
            try:
                added = len((HERE / rel).read_text("utf-8", "replace").splitlines())
            except OSError:
                pass
        else:
            stat = _git("diff", "--numstat", "HEAD", "--", rel)[1].split()
            added, removed = (int(stat[0]), int(stat[1])) if len(stat) >= 2 and stat[0].isdigit() else (0, 0)
        found.append({"path": path, "rel": rel, "state": "new" if "?" in state else "changed",
                      "added": added, "removed": removed})
    return found


def diff(limit=60000):
    """The exact changes that would be sent."""
    parts = []
    for c in changes():
        if c["state"] == "new":
            parts.append(_git("diff", "--no-index", "--", "/dev/null", c["rel"])[1])
        else:
            parts.append(_git("diff", "HEAD", "--", c["rel"])[1])
    d = "\n".join(p for p in parts if p)
    return d if len(d) <= limit else d[:limit] + "\n… (%d more characters)" % (len(d) - limit)


def has_token():
    return bool(keychain.get(TOKEN_ACCOUNT))


def save_token(token):
    token = (token or "").strip()
    if not token:
        keychain.remove(TOKEN_ACCOUNT)
        return False
    if not re.match(r"^(gh[pousr]_|github_pat_)[\w]{20,}$", token):
        raise PushError("That doesn't look like a GitHub token (they start with ghp_ or github_pat_).")
    if not keychain.put(TOKEN_ACCOUNT, token):
        raise PushError("The Mac's Keychain didn't accept the token.")
    return True


def _build_commit(files, note):
    """A commit = this Mac's current version (HEAD) + PatchBay's changed files, made without touching the working
    copy or its index. Returns the commit id."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for args in (("read-tree", "HEAD"), ("add", "--") + tuple(c["rel"] for c in files)):
            code, _, err = _git(*args, env=env)
            if code:
                raise PushError("git couldn't prepare the changes: %s" % err[:300])
        code, tree, err = _git("write-tree", env=env)
        if code:
            raise PushError("git couldn't prepare the changes: %s" % err[:300])
    msg = (note or "Changes PatchBay made to itself").strip()[:200]
    msg += "\n\nFiles: %s\n\nSent from PatchBay on this Mac." % ", ".join(c["rel"] for c in files)
    code, commit, err = _git("commit-tree", tree, "-p", "HEAD", "-m", msg,
                             env={"GIT_AUTHOR_NAME": _git("config", "user.name")[1] or "PatchBay",
                                  "GIT_AUTHOR_EMAIL": _git("config", "user.email")[1] or "patchbay@localhost",
                                  "GIT_COMMITTER_NAME": _git("config", "user.name")[1] or "PatchBay",
                                  "GIT_COMMITTER_EMAIL": _git("config", "user.email")[1] or "patchbay@localhost"})
    if code:
        raise PushError("git couldn't make the commit: %s" % err[:300])
    return commit


def push(note=""):
    """Send PatchBay's changes to the patchbay-edits branch. Returns a plain message with the branch's address."""
    files = changes()
    if not files:
        raise PushError("There are no changes PatchBay made to itself to send.")
    name, web = repo()
    if not name:
        raise PushError("This folder's git 'origin' isn't a GitHub repository.")
    commit = _build_commit(files, note)
    target = "%s:refs/heads/%s" % (commit, BRANCH)
    # 1) the Mac's own GitHub login (if git push already works in Terminal)
    code, _, err = _git("push", "--force", "origin", target, timeout=180)
    if code:
        token = keychain.get(TOKEN_ACCOUNT)
        if not token:
            raise PushError("NEEDS_TOKEN GitHub didn't accept this Mac's login (%s). Save a GitHub token below and "
                            "try again." % (err.splitlines() or ["no login"])[-1][:200])
        # 2) the saved token, handed to git by a tiny helper script (never in a command line or a settings file)
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "askpass.sh"
            helper.write_text('#!/bin/sh\ncase "$1" in Username*) echo x-access-token;; '
                              '*) printf %s "$PATCHBAY_GH_TOKEN";; esac\n')
            helper.chmod(0o700)
            code, _, err = _git("-c", "credential.helper=", "push", "--force",
                                "https://github.com/%s.git" % name, target, timeout=180,
                                env={"GIT_ASKPASS": str(helper), "PATCHBAY_GH_TOKEN": token})
        if code:
            raise PushError("GitHub said no: %s" % (err.splitlines() or ["unknown error"])[-1][:300])
    return {"branch": BRANCH, "files": [c["rel"] for c in files], "commit": commit[:7],
            "compare": "%s/compare/%s...%s" % (web, _git("rev-parse", "--abbrev-ref", "HEAD")[1] or "main", BRANCH)}
