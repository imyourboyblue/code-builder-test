"""
The private builder's steps, shared by PatchBay's chat (server.py) and the command line (run.py):

  make_spec()   idea -> generic spec on this Mac, checked (one automatic rewrite if it still leaks)
  auto_terms()  everything PatchBay knows is yours, for the check
  estimate()    a rough cost before anything is sent
  send()        the spec to Claude (one fresh request) -> files
  write_out()   files into a folder, old versions backed up first, with undo() (the command line; the chat uses
                Setup → Mac's card, backup and Undo instead)

Personal memory, passwords and device secrets are never put into the spec: the only input is the idea you typed.
"""

import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from harness import coder, compiler, sanitizer

HERE = sanitizer.HERE                     # the patchbay folder
CONFIG = HERE / "config.json"
BUILDS = HERE / "data-cache" / "private-builds"
UNDO_DIR = ".patchbay-undo"

SKIP = {"my device", "this mac", "anonymous", "anonymous@", "ftp", "root", "admin", "user", "guest", "localhost",
        "patchbay", "documents", "desktop", "downloads", "projects", "src", "home", "users", "data"}


class PipelineError(Exception):
    pass


def _json(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _generic():
    try:
        import claude
        return claude.GENERIC
    except ImportError:
        return set()


def _computer_name():
    try:
        out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def auto_terms(config_path=None, extra=()):
    """(kind, value) for every personal value PatchBay knows: device names, addresses, logins, passwords, project
    names and folders, approved Mac folders, the Mac's name, your user name, home folder and git email, and the
    values the Claude helper already hides behind stand-ins. extra: more (kind, value) pairs."""
    main = Path(config_path) if config_path else CONFIG
    files = [main] if main.exists() else []
    if (main.parent / "devices").is_dir():
        files += sorted((main.parent / "devices").glob("*.json"))
    terms = list(extra)
    try:
        import keychain
        reveal = keychain.reveal
    except Exception:
        reveal = lambda v: v
    for f in files:
        c = _json(f)
        terms += [("device", c.get("device_name")), ("ip", c.get("host")), ("user", c.get("username"))]
        if c.get("password"):
            try:
                terms.append(("secret", reveal(c["password"])))
            except Exception:
                pass
        for pr in c.get("projects") or []:
            terms.append(("project", pr.get("name")))
            if pr.get("folder"):
                full = os.path.expanduser(pr["folder"])
                terms += [("folder", full), ("project", Path(full).name)]
        for folder in c.get("mac_folders") or []:
            terms.append(("folder", os.path.expanduser(folder)))
    home = str(Path.home())
    terms += [("folder", home), ("user", getpass.getuser())]
    for n in {socket.gethostname().split(".")[0], _computer_name()}:
        terms.append(("mac", n))
        m = re.match(r"^([A-Z][a-z]+)(?:['’]s|s)?[\s-]+(?:Mac|MacBook|iMac)", n or "")
        if m:
            terms.append(("name", m.group(1)))
    git = Path.home() / ".gitconfig"
    try:
        for m in re.finditer(r"(?m)^\s*(email|name)\s*=\s*(.+?)\s*$", git.read_text("utf-8", "replace")):
            terms.append(("email" if m.group(1) == "email" else "name", m.group(2)))
    except OSError:
        pass
    for real in _json(main.parent / "claude-aliases.json"):
        terms.append(("seen", real))
    generic = _generic()
    out, seen = [], set()
    for kind, value in terms:
        v = str(value or "").strip()
        low = v.lower()
        if len(v) < 3 or low in SKIP or low in generic or low.startswith(("127.", "0.0.0.")) or (kind, low) in seen:
            continue
        seen.add((kind, low))
        out.append((kind, v))
    return out


def local_ai(url=None, model=None, settings=None):
    """(Ollama address, model) for writing the spec: PatchBay's own settings unless given."""
    import core
    import model_labels
    cfg = _json(CONFIG)
    try:
        url = core.check_ai_url(url or cfg.get("ollama_url") or "http://localhost:11434")
    except ValueError as e:
        raise PipelineError(str(e))
    model = model or (settings or {}).get("spec_local_model")
    if not model:
        names = model_labels.installed(url)
        if not names:
            raise PipelineError("Ollama at %s has no models (or isn't open)." % url)
        mode = cfg.get("work_mode") if cfg.get("work_mode") in model_labels.MODE_KEYS else "everyday"
        model = model_labels.mode_model(names, mode) or names[0]
    return url, model


def make_spec(idea, url, model, terms, blocked, leak_check=True, allowed=(), log=lambda t: None, urlopen=None):
    """Write the spec on this Mac and check it. If it still leaks something the local AI can be told about, it
    rewrites it once. Returns (spec, findings, suspects): send only when both lists are empty."""
    allowed = list(allowed) + list(blocked.get("allowed_terms") or [])
    log("Writing a generic spec with %s on this Mac (nothing leaves the Mac)..." % model)
    spec = compiler.compile_spec(idea, url, model, urlopen=urlopen)
    found, suspects = check(spec, url, model, terms, blocked, leak_check, allowed, log, urlopen)
    if found or suspects:
        named = [f["shown"] for f in found if "(hidden)" not in f["shown"]] + suspects
        if named and len(named) == len(found) + len(suspects):
            log("The spec still had %d private detail(s). Asking the local AI to write it again without them..."
                % len(named))
            spec = compiler.compile_spec(idea, url, model, problems=named, previous=spec, urlopen=urlopen)
            found, suspects = check(spec, url, model, terms, blocked, leak_check, allowed, log, urlopen)
    return spec, found, suspects


def check(spec, url, model, terms, blocked, leak_check, allowed, log, urlopen=None):
    log("Checking the spec for anything private...")
    found = sanitizer.scan(spec, terms, blocked)
    suspects = []
    if leak_check and not found:
        log("Double-checking the spec with the local AI...")
        suspects = compiler.double_check(spec, url, model, allowed, urlopen=urlopen)
    return found, suspects


def problems_text(found, suspects):
    rows = []
    if found:
        rows.append(sanitizer.explain(found))
    if suspects:
        rows.append("- the local AI's double-check thinks these may be private: %s" % ", ".join(suspects))
    return "\n".join(rows)


def prices(model):
    import claude
    m = claude.MODELS.get(model) or claude.MODELS[claude.DEFAULTS["model"]]
    return m[1], m[2]


def estimate(spec, model):
    """(input tokens, plain cost line)."""
    tokens, low, high = coder.estimate(spec, prices(model))
    return tokens, "about %s to %s (about %d tokens in; the code it writes costs most). Billed by Anthropic to " \
                   "your API account, separate from a Claude Pro plan." % (_money(low), _money(high), tokens)


def _money(x):
    return "$%.3f" % x if x < 0.1 else "$%.2f" % x


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:40].strip("-") or "build")


def record_dir(name):
    """A folder on this Mac (never sent) that keeps the idea, the spec and Claude's answer for this build."""
    d = BUILDS / ("%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), slug(name)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def send(claude_client, spec, model, terms, blocked):
    """Check once more, then send. Returns ([(path, content)], notes)."""
    found = sanitizer.scan(spec, terms, blocked)
    if found:
        raise PipelineError("The spec has private details, so nothing was sent:\n" + sanitizer.explain(found))
    files, notes, _ = coder.generate(claude_client, spec, model)
    return files, notes


# ---------------------------------------------------------------- writing (command line)

def write_out(out_dir, files):
    """Write the files into out_dir. Files that are already there are backed up first into
    out_dir/.patchbay-undo/<time>/, with a list, so undo() can put everything back. Returns that folder."""
    root = Path(out_dir).expanduser().resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    undo = root / UNDO_DIR / stamp
    undo.mkdir(parents=True, exist_ok=True)
    made = []
    for rel, content in files:
        rel = coder.clean_path(rel)
        p = (root / rel).resolve()
        if root not in p.parents:
            raise PipelineError("%s is outside %s." % (rel, root))
        entry = {"path": rel, "existed": p.exists()}
        if p.exists():
            keep = undo / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(keep))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".patchbay-tmp")
        tmp.write_text(content, "utf-8")
        tmp.replace(p)
        made.append(entry)
    (undo / "_written.json").write_text(json.dumps(made, indent=1))
    return undo


def undo(out_dir):
    """Put the folder back the way it was before the last write_out(). Returns a plain result."""
    root = Path(out_dir).expanduser().resolve()
    runs = sorted((root / UNDO_DIR).glob("*/_written.json")) if (root / UNDO_DIR).is_dir() else []
    if not runs:
        raise PipelineError("There's nothing to undo in %s." % root)
    last = runs[-1].parent
    made = _json(runs[-1]) or []
    for e in made:
        p = root / e["path"]
        if e.get("existed"):
            shutil.copy2(str(last / e["path"]), str(p))
        elif p.exists():
            p.unlink()
    shutil.rmtree(str(last), ignore_errors=True)
    return "Undid the last build in %s (%d files put back or removed)." % (root, len(made))
