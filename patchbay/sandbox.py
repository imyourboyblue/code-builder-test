"""
Sandboxes: a throwaway copy of a project where PatchBay can build and test programs without touching your real
files, passwords or Keychain.

Each sandbox is a folder in ~/PatchBay-data/sandboxes/<name>/ holding a COPY of the project (work/). Two kinds:

  linux  The copy is the only thing a Linux container can see (mounted at /work). Uses Apple's free `container`
         tool (each sandbox is its own small Linux virtual machine) or, if that isn't installed, Docker
         (Colima, OrbStack or Docker Desktop). CPU and memory limits are enforced. Good for projects that build
         on Linux/Ubuntu.
  mac    Runs on the Mac itself (so devkitPro and Xcode tools work) inside macOS's sandbox-exec: it can only write
         in the sandbox folder and the temp folders, can't read anything in your home folder except the sandbox,
         and can't reach the Keychain. A CPU-time limit is set; memory can't be limited on macOS.

Network is OFF by default. Turning it on (for downloads, like apt-get install) is an explicit switch.
Results only leave a sandbox through copy_out, which the chat shows on a confirm card; the file it replaces is
backed up first and the copy can be undone.

Secrets are never copied in: files like config.json, .env, id_rsa and folders like .ssh are skipped, and the run
environment has only a few plain variables (no tokens or passwords). Build leftovers aren't copied either:
whatever the project's .gitignore marks (build/, dist/, caches, downloaded dependencies) was made on the Mac or by
an older build and only gets in the way, so the sandbox starts like a fresh download (setting copy_ignored).

Only uses Python's built-in parts.
"""

import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import services

HOME = Path.home()
ROOT = HOME / "PatchBay-data" / "sandboxes"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
SKIP_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".docker", "Keychains", "__pycache__", "node_modules", ".patchbay-names"}
SECRET_NAMES = {"config.json", "access.json", "claude.json", "claude-aliases.json", "id_rsa", "id_ed25519", "id_ecdsa",
                ".env", ".netrc", ".git-credentials", "credentials", "credentials.json", "redaction_list.json"}
SECRET_ENDS = (".pem", ".key", ".p12", ".keychain", ".keychain-db", ".mobileprovision")
COPY_LIMIT = 20 * 1024 ** 3          # projects bigger than this aren't copied in
DEFAULTS = {"kind": "linux", "image": "ubuntu:24.04", "network": False, "copy_ignored": False, "cpus": 4, "memory_gb": 8,
            "timeout_min": 60, "disk_gb": 20, "arch": "native", "memory_max": 28,
            # The recipe: how this project is set up and built inside (see sandboxrecipe.py). Empty = none yet.
            "setup_command": "", "build_command": "", "output": "", "blocked": [], "upload_hint": "",
            "recipe_notes": [], "recipe_from": [],
            # What PatchBay and the AI changed or learned in this sandbox, kept so later steps (and resets, make clean,
            # new chats) don't redo or undo it: tool swaps done on every run, fixes redone by every setup, notes.
            "tool_aliases": {}, "fixes": [], "notes": [], "suspect": [], "needs_setup": False}
RECIPE_KEYS = ("setup_command", "build_command", "output", "upload_hint", "recipe_notes", "recipe_from")
ARCHES = ("native", "amd64")         # native = arm64 on Apple silicon (fast); amd64 = x86 Linux through Rosetta
OFFLINE_NET = "patchbay-offline"     # Apple container: a host-only network (macOS 26+), used when network is off
EXTRA_PATHS = ["/usr/local/bin", "/opt/homebrew/bin", str(HOME / ".orbstack/bin"),
               "/Applications/Docker.app/Contents/Resources/bin"]
LOCK = threading.Lock()
# Inside a Linux sandbox: where tool swaps (objcopy -> llvm-objcopy) are linked. /usr/local/bin comes before /usr/bin in
# every standard PATH, also the one build scripts set themselves (export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:
# /usr/bin:/sbin:/bin), so a script that resets PATH still gets the swap. (/opt/patchbay/bin was dropped by those.)
ALIAS_DIR = "/usr/local/bin"
OLD_ALIAS_DIR = "/opt/patchbay/bin"
PLAIN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LABEL = "patchbay.sandbox"          # put on every sandbox container and saved image, so clean-up only finds PatchBay's
TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,60}$")
NOTES_MAX = 30
FIXES_MAX = 20


class SandboxError(Exception):
    pass


# ---------------------------------------------------------------- where things are

def root():
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT


def check_name(name):
    if not NAME_RE.match(name or ""):
        raise SandboxError("Give the sandbox a short name (letters, numbers, - _ or .), like the project's name.")
    return name


def folder(name):
    return root() / check_name(name)


def slug(name):
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-") or "sandbox"


def container_name(name):
    return "patchbay-sb-" + slug(name)


def saved_image(name):
    return "patchbay-sb-%s:saved" % slug(name)


def load(name):
    f = folder(name) / "sandbox.json"
    try:
        sb = json.loads(f.read_text())
    except (OSError, ValueError):
        names = ", ".join(x["name"] for x in all_sandboxes()) or "none yet"
        raise SandboxError("There's no sandbox called %s. Sandboxes: %s." % (name, names))
    return dict(DEFAULTS, **sb)


def save(sb):
    f = folder(sb["name"]) / "sandbox.json"
    tmp = f.with_name("sandbox.json.tmp")
    tmp.write_text(json.dumps(sb, indent=2))
    tmp.replace(f)


def all_sandboxes():
    out = []
    if not ROOT.is_dir():
        return out
    for d in sorted(ROOT.iterdir()):
        try:
            out.append(dict(DEFAULTS, **json.loads((d / "sandbox.json").read_text())))
        except (OSError, ValueError):
            continue
    return out


def work(sb):
    return folder(sb["name"]) / "work"


def inside_work(sb, rel):
    w = work(sb).resolve()
    p = (w / str(rel or "").lstrip("/")).resolve()
    if p != w and w not in p.parents:
        raise SandboxError("%s is outside the sandbox." % rel)
    return p


# ---------------------------------------------------------------- copying the project in

def is_secret(path):
    n = path.name
    return n in SECRET_NAMES or n.lower().endswith(SECRET_ENDS) or n.startswith(".env.")


def size_of(path, stop_at=None, leave=None):
    """Bytes in a folder. leave(rel, is_dir) -> True for things that won't be copied (see left_out)."""
    total = 0
    top = str(path)
    for dirpath, dirnames, filenames in os.walk(path):
        rel_dir = os.path.relpath(dirpath, top)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (leave and leave(rel_dir + d, True))]
        for n in filenames:
            if leave and leave(rel_dir + n, False):
                continue
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
        if stop_at and total > stop_at:
            break
    return total


# Build leftovers. A sandbox should start like a fresh download of the project: whatever the project's .gitignore
# marks as not part of it (build/, dist/, caches, downloaded dependencies, a config.h from an earlier configure)
# was made on the Mac or by an older build, and in a Linux sandbox it only gets in the way. The build makes it again.
NEVER_LEFT_OUT = {".git", ".gitignore", ".gitmodules", ".gitattributes"}


def _git_ignored(source):
    """What git says is ignored in the folder, as relative paths (folders end with /). None: not a git project."""
    git = shutil.which("git") or ("/usr/bin/git" if Path("/usr/bin/git").exists() else None)
    if not git or not _in_git(source):
        return None
    try:
        r = subprocess.run([git, "-C", str(source), "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                            "--directory", "--no-empty-directory"], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    found = [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]
    if any(x in ("./", ".") or x.startswith("../") for x in found):
        return None                   # the whole folder is ignored by a repo around it: copy it all as before
    return found


def _in_git(source):
    for d in [source] + list(source.parents):
        if (d / ".git").exists():
            return True
    return False


def _gitignore_patterns(source):
    """The top .gitignore's patterns, for a project that isn't a git checkout (or has no git)."""
    try:
        lines = (source / ".gitignore").read_text("utf-8", "replace").splitlines()
    except OSError:
        return []
    pats = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        line = line[1:] if neg else line
        dir_only = line.endswith("/")
        line = line.strip("/") if dir_only else line
        anchored = line.startswith("/") or "/" in line
        pats.append((line.lstrip("/"), dir_only, anchored, neg))
    return pats


def _match_gitignore(pats, rel, is_dir):
    left = False
    name = rel.rsplit("/", 1)[-1]
    for pat, dir_only, anchored, neg in pats:
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatchcase(rel if anchored else name, pat):
            left = not neg
    return left


def left_out(source, copy_ignored=False):
    """(leave, listing): leave(rel, is_dir) says whether a path is a build leftover the sandbox copy skips, and
    listing names what is skipped (relative paths, folders end with /). Nothing is skipped with copy_ignored."""
    source = Path(source)
    if copy_ignored:
        return (lambda rel, is_dir: False), []
    found = _git_ignored(source)
    if found is not None:
        found = [x for x in found if x.rstrip("/").split("/")[0] not in NEVER_LEFT_OUT]
        dirs = {x.rstrip("/") for x in found if x.endswith("/")}
        files = {x for x in found if not x.endswith("/")}

        def leave(rel, is_dir):
            return (rel in dirs) if is_dir else (rel in files)
        return leave, found
    pats = _gitignore_patterns(source)
    if not pats:
        return (lambda rel, is_dir: False), []
    listing = []

    def leave(rel, is_dir):
        if rel.split("/")[0] in NEVER_LEFT_OUT:
            return False
        if _match_gitignore(pats, rel, is_dir):
            listing.append(rel + ("/" if is_dir else ""))
            return True
        return False
    return leave, listing


def copy_in(source, dest, copy_ignored=False):
    """Copy a project folder into the sandbox, leaving out secrets and build leftovers (what .gitignore marks).
    Returns (names skipped as secrets, build leftovers left out)."""
    source = Path(source).resolve()
    leave, listing = left_out(source, copy_ignored)
    if size_of(source, COPY_LIMIT, leave) > COPY_LIMIT:
        raise SandboxError("%s is over %d GB, too big to copy into a sandbox." % (source, COPY_LIMIT // 1024 ** 3))
    skipped = []

    def ignore(src, names):
        s = _ignore_from(source, src, names)
        skipped.extend(x for x in s if x not in SKIP_DIRS)
        rel_dir = os.path.relpath(src, str(source))
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for n in names:
            if n not in s and leave(rel_dir + n, os.path.isdir(os.path.join(src, n)) and
                                    not os.path.islink(os.path.join(src, n))):
                s.append(n)
        return s

    if dest.exists():
        shutil.rmtree(str(dest))
    shutil.copytree(str(source), str(dest), symlinks=True, ignore=ignore)
    return skipped, sorted(set(listing))


def _ignore_from(top, src, names):
    skip = []
    for n in names:
        p = Path(src) / n
        if n in SKIP_DIRS or is_secret(p):
            skip.append(n)
        elif p.is_symlink():
            try:
                t = p.resolve()
            except OSError:
                skip.append(n)
                continue
            if t != top and top not in t.parents:
                skip.append(n)
    return skip


def is_left_out(source, rel):
    """Whether a path in the project (relative) is a build leftover by the .gitignore, or inside one."""
    leave, _ = left_out(source)
    parts = str(rel).strip("/").split("/")
    return any(leave("/".join(parts[:i]), True) for i in range(1, len(parts))) or leave("/".join(parts), False)


def left_out_words(sb, limit=6):
    """Plain words on the build leftovers the copy skipped, for the chat."""
    lo = sb.get("left_out") or []
    if not lo:
        return ""
    return (" Left out as build leftovers (the project's .gitignore marks them; the build makes them again): %s%s."
            % (", ".join(lo[:limit]), " and %d more" % (sb.get("left_out_count", len(lo)) - limit)
               if sb.get("left_out_count", len(lo)) > limit else ""))


def platform_words(sb):
    """What the sandbox runs, in plain words (for hints about files built somewhere else)."""
    if sb.get("kind") != "linux":
        return "a locked-down folder on this Mac"
    return "Ubuntu Linux on %s, in a container (Colima's Linux machine on this Mac)" % (
        "x86 (amd64, through Rosetta)" if sb.get("arch") == "amd64" else "ARM (arm64)")


# ---------------------------------------------------------------- engines (what runs the Linux sandboxes)

def _which(exe):
    path = os.environ.get("PATH", "") + ":" + ":".join(EXTRA_PATHS)
    return shutil.which(exe, path=path)


def cli_env():
    env = dict(os.environ)
    env["PATH"] = ":".join([p for p in EXTRA_PATHS if p not in env.get("PATH", "")] + [env.get("PATH", "")])
    if not env.get("DOCKER_HOST"):
        # Point every docker command straight at Colima. Otherwise docker follows its "current context", which can
        # still be Docker Desktop's or the default socket, so "docker info" fails even though Colima is running.
        sock = colima_socket()
        if sock:
            env["DOCKER_HOST"] = "unix://%s" % sock
    return env


def colima_home():
    return Path(os.environ.get("COLIMA_HOME") or (Path.home() / ".colima"))


def colima_socket():
    """Colima's Docker socket (~/.colima/default/docker.sock), or None when Colima hasn't made one."""
    p = colima_home() / "default" / "docker.sock"
    try:
        return p if stat.S_ISSOCK(p.stat().st_mode) else None
    except OSError:
        return None


def _quiet(argv, timeout=60):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env=cli_env(), errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def engine():
    """('apple', path) for Apple's container tool, ('docker', path) for Docker/Colima/OrbStack, or (None, None)."""
    apple = _which("container")
    if apple and "container" in _quiet([apple, "--version"], 15)[1].lower():
        return "apple", apple
    docker = _which("docker")
    if docker:
        return "docker", docker
    return None, None


INSTALL_HELP = ("Linux sandboxes need a container tool on the Mac. Easiest: sandbox_preflight, which puts Colima and "
                "Docker (Homebrew names: colima and docker) on one install card. Apple's free container tool works "
                "too: the installer (.pkg) is on github.com/apple/container/releases.")
# Homebrew's real names. "docker-cli" doesn't exist: the Docker command-line tool's formula is just "docker".
ENGINE_BREW = ["colima", "docker"]


def mac_memory_gb():
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL).stdout.strip()
        if out.isdigit():
            return round(int(out) / 1024 ** 3)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3)
    except (OSError, ValueError, AttributeError):
        return 16


def colima_size(cores=None, mem_gb=None):
    """CPUs, memory (GB) and disk (GB) for Colima's Linux machine, sized from this Mac. Colima's own default is only
    2 CPUs and 2 GB, far too small for big builds. A 14-core 36 GB Mac gets 8 CPUs and 20 GB; macOS and a loaded AI
    model keep the rest."""
    cores = int(cores or os.cpu_count() or 8)
    mem = int(mem_gb or mac_memory_gb() or 16)
    cpus = max(2, min(12, cores // 2 + 1))
    memory = max(4, min(48, int(mem * 0.56), mem - 12))
    return cpus, memory, 100


def colima_start_argv(colima, cores=None, mem_gb=None):
    cpus, memory, disk = colima_size(cores, mem_gb)
    return [colima, "start", "--cpu", str(cpus), "--memory", str(memory), "--disk", str(disk)]


def colima_command(cores=None, mem_gb=None):
    """The Terminal line that restarts Colima with the right size (for messages)."""
    return "colima stop; " + " ".join(colima_start_argv("colima", cores, mem_gb))


def uses_colima(exe=None):
    """Whether Docker here runs through Colima (so PatchBay can size and restart it)."""
    if not _which("colima"):
        return False
    exe = exe or _which("docker")
    if not exe:
        return True
    code, out = _quiet([exe, "context", "show"], 15)
    if code == 0 and "colima" in out.lower():
        return True
    return _quiet([_which("colima"), "status"], 20)[0] == 0


def docker_capacity(exe):
    """(CPUs, memory GB) of the Linux machine Docker runs in, or (None, None) if Docker can't say."""
    code, out = _quiet([exe, "info", "--format", "{{.NCPU}} {{.MemTotal}}"], 20)
    try:
        n, m = out.split()[:2]
        return int(n), int(m) / 1024 ** 3
    except (ValueError, IndexError):
        return None, None


def fit_to_engine(kind, exe, sb):
    """Lower the sandbox's CPUs and memory to what Docker's Linux machine really has (asking for more makes Docker
    refuse to start it). Apple's tool gives each sandbox its own machine, so nothing to fit. Returns a plain note
    ("" when it already fits). Saves the lowered numbers so {cpus} in the build command matches."""
    if kind != "docker":
        return ""
    ncpu, mem = docker_capacity(exe)
    if not ncpu:
        return ""
    changed = []
    if int(sb["cpus"]) > ncpu:
        changed.append("CPUs %d -> %d" % (int(sb["cpus"]), ncpu))
        sb["cpus"] = ncpu
    room = max(1, int(mem) - 1) if mem else None      # the Linux machine itself needs about 1 GB
    if room and int(sb["memory_gb"]) > room:
        changed.append("memory %d -> %d GB" % (int(sb["memory_gb"]), room))
        sb["memory_gb"] = room
    if not changed:
        return ""
    save(sb)
    note = ("Docker's Linux machine only has %d CPUs and %.0f GB, so sandbox %s was lowered to fit (%s)."
            % (ncpu, mem or 0, sb["name"], ", ".join(changed)))
    if uses_colima(exe):
        cpus, memory, _ = colima_size()
        note += (" To give it more, restart Colima bigger (%d CPUs, %d GB): sandbox_preflight shows a card for it."
                 % (cpus, memory))
    return note


def engine_ready(start=True, emit=None, stop=None):
    """The engine, started if it was stopped. Raises SandboxError with plain steps if it can't be used."""
    kind, exe = engine()
    if not kind:
        raise SandboxError(INSTALL_HELP)
    if kind == "apple":
        code, out = _quiet([exe, "system", "status"], 20)
        if code != 0 and start:
            code, out = _quiet([exe, "system", "start", "--enable-kernel-install"], 300)
            if code == 0:
                services.record("apple_container", "system", "Apple's container tool")
        if code != 0:
            raise SandboxError("Apple's container tool isn't running and didn't start: %s\nIn Terminal, run: "
                               "container system start" % out[-400:])
    else:
        code, out = _quiet([exe, "info"], 20)
        if code != 0 and _which("colima"):
            if colima_info()["running"]:
                # Colima is up but Docker inside it isn't answering yet (it's still booting): wait, don't restart.
                ok, out = wait_for_docker(exe, emit, stop, QUICK_WAIT if recent_trouble() else None)
                if not ok:
                    note_trouble()
                    raise SandboxError(colima_stuck_message(out, next_fix()))
                clear_trouble()
                code = 0
            elif start:
                start_colima(emit, stop=stop)     # sized for builds; waits until Docker answers
                code = 0
        if code != 0:
            raise SandboxError("Docker isn't running.\n%s\n%s" % (docker_error(out), (
                "Start Colima with sandbox_preflight (it shows a card)." if _which("colima") else
                "Docker needs Colima to run: sandbox_preflight puts it on an install card (Homebrew name: colima).")))
    return kind, exe


def engine_status():
    kind, exe = engine()
    if not kind:
        return {"engine": None, "running": False, "help": INSTALL_HELP}
    probe = [exe, "system", "status"] if kind == "apple" else [exe, "info"]
    running = _quiet(probe, 20)[0] == 0
    return {"engine": "Apple container" if kind == "apple" else "Docker", "running": running,
            "help": "" if running else ("In Terminal: container system start" if kind == "apple" else
                                        "Start Colima with sandbox_preflight (a card), or OrbStack / Docker Desktop.")}


def preflight(sb=None, emit=None, stop=None, wait=None):
    """Everything a Linux sandbox needs on the Mac, checked in one go BEFORE starting it. Only reads (it may wait
    for Colima to finish booting). Returns {"rows": [(ok, name, words)], "brew": [Homebrew formulas to install],
    "colima": None|"start"|"resize", "stuck": "" or a plain message when Colima runs but Docker won't answer,
    "fix": None|"repair"|"reset" (the card for a stuck Colima; None with stuck = nothing left to try), "ok": bool}."""
    rows, brew, colima, stuck = [], [], None, ""
    kind, exe = engine()
    if kind == "apple":
        code, out = _quiet([exe, "system", "status"], 20)
        rows.append((True, "Apple container tool", exe))
        rows.append((code == 0, "Apple container tool running", "yes" if code == 0 else
                     "no: PatchBay starts it when the sandbox starts"))
        return {"rows": rows, "brew": [], "colima": None, "stuck": "", "ok": True}
    docker, col = _which("docker"), _which("colima")
    rows.append((bool(docker), "Docker command-line tool", docker or "missing (Homebrew: docker)"))
    if not docker:
        brew.append("docker")
    have_desktop = any(Path(p).exists() for p in ("/Applications/Docker.app", "/Applications/OrbStack.app"))
    rows.append((bool(col) or have_desktop, "Colima (runs Docker's Linux machine)",
                 col or ("not needed: Docker Desktop/OrbStack is installed" if have_desktop else
                         "missing (Homebrew: colima)")))
    if not col and not have_desktop:
        brew.append("colima")
    running = bool(docker) and _quiet([docker, "info"], 20)[0] == 0
    want_cpu = int((sb or {}).get("cpus") or DEFAULTS["cpus"])
    want_mem = int((sb or {}).get("memory_gb") or DEFAULTS["memory_gb"])
    if running:
        clear_trouble()
    fix = None
    info = colima_info() if docker and col and not running else {"running": False}
    if info["running"]:
        # Colima already runs: never offer to start it again. Give Docker inside it time to answer, but only once:
        # if it was already stuck a moment ago, a quick look is enough (waiting 3 minutes again changes nothing).
        if wait is None:
            wait = QUICK_WAIT if recent_trouble() else COLIMA_WAIT
        ok, out = wait_for_docker(docker, emit, stop, wait)
        running = ok
        if ok:
            clear_trouble()
        else:
            cpus, memory, _ = colima_size()
            if colima_small(info, cpus, memory):
                rows.append((False, "Docker running", "no, and Colima is too small (%s CPUs, %s GB): a card "
                             "restarts it at the right size" % (info.get("cpus") or "?", _gb(info.get("memory_gb")))))
                colima = "resize"
            else:
                note_trouble()
                fix = next_fix()
                rows.append((False, "Docker running", "Colima is running, but Docker inside it isn't answering"))
                stuck = colima_stuck_message(out, fix)
    if docker and not running and not info["running"] and col and recent_trouble():
        # Colima failed to start a moment ago: starting it the same way again won't help. Offer the stronger fix.
        fix = next_fix()
        rows.append((False, "Docker running", "no: Colima didn't come up last time"))
        stuck = colima_stuck_message("", fix).replace("Colima is running, but Docker inside it isn't answering",
                                                      "Colima didn't start properly last time")
    elif docker and not running and not info["running"]:
        rows.append((False, "Docker running", "no" + (": a card starts Colima at the right size" if col else "")))
        colima = "start" if col else None
    elif running:
        ncpu, mem = docker_capacity(docker)
        cpus, memory, _ = colima_size()
        small = bool(ncpu) and (ncpu < min(want_cpu, cpus) or (mem or 0) + 1 < min(want_mem, memory))
        rows.append((not small, "Docker's Linux machine size",
                     "%s CPUs, %.0f GB%s" % (ncpu or "?", mem or 0,
                                             (" (too small: the sandbox wants %d CPUs, %d GB)" % (want_cpu, want_mem))
                                             if small else "")))
        if small and col and uses_colima(docker):
            colima = "resize"
    return {"rows": rows, "brew": brew, "colima": colima, "stuck": stuck, "fix": fix if stuck else None,
            "ok": not brew and not colima and not stuck and (running or not docker)}


COLIMA_WAIT = 180       # how long to wait for Docker to answer after Colima starts (seconds)
COLIMA_START_LIMIT = 600   # give up on "colima start" after this long
COLIMA_SLOW = 150       # after this long, say it's slower than usual
POLL = 3


def _gb(x):
    return "%.0f" % x if isinstance(x, (int, float)) else "?"


def colima_info():
    """What Colima itself says, without needing Docker: {"running", "cpus", "memory_gb"}."""
    col = _which("colima")
    info = {"running": False, "cpus": None, "memory_gb": None}
    if not col:
        return info
    code, out = _quiet([col, "list", "--json"], 20)
    for line in out.splitlines() if code == 0 else []:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("name", "default") != "default":
            continue
        mem = d.get("memory")
        if isinstance(mem, (int, float)) and mem > 4096:     # bytes (older Colima gives GB)
            mem = mem / 1024 ** 3
        return {"running": str(d.get("status", "")).lower() == "running",
                "cpus": d.get("cpus") if isinstance(d.get("cpus"), int) else None,
                "memory_gb": mem if isinstance(mem, (int, float)) else None}
    info["running"] = _quiet([col, "status"], 20)[0] == 0
    return info


def colima_small(info, cpus, memory):
    """Whether a running Colima is smaller than PatchBay's size for this Mac (unknown size counts as fine)."""
    return bool((info.get("cpus") and info["cpus"] < cpus) or
                (info.get("memory_gb") and info["memory_gb"] + 0.5 < memory))


def colima_status_text():
    col = _which("colima")
    if not col:
        return "(Colima isn't installed)"
    code, out = _quiet([col, "status"], 20)
    return out.strip()[-800:] or "(colima status said nothing, exit code %d)" % code


def docker_error(out):
    """The part of docker's output that says what went wrong (docker info prints a long client section first)."""
    lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
    bad = [x for x in lines if re.search(r"error|cannot|can't|couldn't|denied|refused|no such|timed out|"
                                         r"not running|failed|daemon", x, re.I)]
    return "\n".join(bad[-6:] or lines[-6:])[-800:] or "(docker said nothing)"


# Colima trouble PatchBay has already seen, so it doesn't wait 3 minutes again on every check, and so the next card
# is a different, stronger fix instead of the same one again: repair (force stop + start), then reset (delete +
# start). Cleared as soon as Docker answers.
TROUBLE = {"at": 0.0, "repairs": 0, "resets": 0}
TROUBLE_FRESH = 30 * 60     # a stuck Colima seen this recently is re-checked quickly, not waited on again
QUICK_WAIT = 6


def note_trouble():
    TROUBLE["at"] = time.time()


def clear_trouble():
    TROUBLE.update(at=0.0, repairs=0, resets=0)


def recent_trouble():
    return bool(TROUBLE["at"]) and time.time() - TROUBLE["at"] < TROUBLE_FRESH


def next_fix():
    """The card to offer for a stuck Colima: "repair" first, then "reset", then None (nothing left PatchBay can do)."""
    if not TROUBLE["repairs"]:
        return "repair"
    if not TROUBLE["resets"]:
        return "reset"
    return None


def colima_log():
    return colima_home() / "_lima" / "colima" / "ha.stderr.log"


def colima_log_tail(lines=10):
    """The last few lines of Colima's log that say what went wrong (errors and warnings first), or ""."""
    try:
        with open(colima_log(), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    rows = [x.strip() for x in text.splitlines() if x.strip()][-300:]
    bad = [x for x in rows if re.search(r"level=(error|fatal|warn)|\b(error|failed|fatal|panic)\b", x, re.I)]
    return "\n".join(x[:240] for x in (bad or rows)[-lines:])


FIX_WORDS = {
    "repair": "PatchBay can fix this itself: press the Repair Colima card. It force-stops Colima and starts it "
              "again, sized for this Mac. Your sandboxes and their files stay.",
    "reset": "Repairing didn't help, so the next step is the Reset Colima card. It deletes Colima's Linux machine "
             "and makes a fresh one. Your sandbox folders on this Mac stay, but anything installed inside the "
             "sandboxes is gone and gets downloaded again on the next setup step.",
    None: "PatchBay already tried repairing and resetting Colima, and it still won't come up, so it has stopped "
          "trying. Something outside PatchBay is wrong with Colima on this Mac: updating it (brew upgrade colima "
          "lima) or restarting the Mac usually helps. Then ask me again.",
}


def colima_stuck_message(out="", fix="repair"):
    """Plain words for a Colima whose Docker won't answer. Never tells the user to type commands PatchBay blocks:
    it names the card that fixes it instead."""
    tail = colima_log_tail()
    return ("Colima is running, but Docker inside it isn't answering.\n"
            "Docker said: %s\n"
            "Colima says (colima status):\n%s\n%s"
            "%s"
            % (docker_error(out), colima_status_text(),
               ("Last lines of Colima's log (%s):\n%s\n" % (colima_log(), tail)) if tail else "",
               FIX_WORDS[fix]))


def wait_for_docker(exe, emit=None, stop=None, timeout=None):
    """Ask Docker every few seconds until it answers (Colima's Linux machine can take a minute or two to boot).
    Returns (True, "") or (False, docker's last output)."""
    timeout = COLIMA_WAIT if timeout is None else timeout
    started, out = time.time(), ""
    while True:
        code, out = _quiet([exe, "info"], 20)
        if code == 0:
            return True, ""
        waited = time.time() - started
        if waited >= timeout:
            return False, out
        if emit:
            emit({"type": "status", "text": "Waiting for Docker inside Colima to answer... %d s%s"
                                            % (waited, " (slower than usual, still waiting)"
                                               if waited > COLIMA_SLOW else "")})
        _check_stop(stop)
        time.sleep(POLL)


def _check_stop(stop):
    """stop can be the chat's check function (raises when Stop was pressed) or a threading.Event."""
    if stop is None:
        return
    if isinstance(stop, threading.Event):
        if stop.is_set():
            raise SandboxError("Stopped.")
    else:
        stop()


def _run_progress(argv, timeout, emit, text, stop=None):
    """Run a slow command, showing how long it has been going. Returns (exit code, output)."""
    with tempfile.TemporaryFile("w+", errors="replace") as log:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    env=cli_env(), text=True)
        except OSError as e:
            return 1, str(e)
        started = time.time()
        while proc.poll() is None:
            waited = time.time() - started
            if waited > timeout:
                proc.kill()
                proc.wait()
                log.seek(0)
                return 124, log.read()[-2000:] + "\n(gave up after %d seconds)" % timeout
            if emit:
                emit({"type": "status", "text": "%s %d s%s" % (text, waited, " (slower than usual, still going)"
                                                               if waited > COLIMA_SLOW else "")})
            if stop:
                try:
                    _check_stop(stop)
                except BaseException:
                    proc.kill()
                    raise
            time.sleep(POLL)
        log.seek(0)
        return proc.returncode, log.read()[-4000:]


def start_colima(emit=None, resize=False, stop=None):
    """Start (or restart bigger) Colima, sized for builds, then wait until Docker inside it answers. Only after the
    user confirmed the card (or when a sandbox needs it). If Colima already runs, it isn't started again."""
    col = _which("colima")
    if not col:
        raise SandboxError("Colima isn't installed. Use sandbox_preflight: it shows the install card.")
    argv = colima_start_argv(col)
    info = colima_info()
    started_it = False
    if info["running"] and not resize:
        if emit:
            emit({"type": "status", "text": "Colima is already running. Checking Docker answers..."})
    else:
        if resize and info["running"]:
            code, out = _run_progress([col, "stop"], 300, emit, "Stopping Colima...", stop)
            if code != 0:
                raise SandboxError("Colima didn't stop.\n%s\nColima says (colima status):\n%s"
                                   % (out.strip()[-600:], colima_status_text()))
        code, out = _run_progress(argv, COLIMA_START_LIMIT, emit, "Starting Colima (%s CPUs, %s GB), this can take "
                                  "a minute or two..." % (argv[3], argv[5]), stop)
        if code != 0:
            note_trouble()
            raise SandboxError("Colima didn't start.\nIt said: %s\nColima says (colima status):\n%s\n%s"
                               % (out.strip()[-600:], colima_status_text(), FIX_WORDS[next_fix()]))
        started_it = not resize   # restarting a bigger one keeps it whoever's it was (yours if you had it running)
    if started_it:
        services.record("colima", "default", "Colima (the sandboxes' Linux machine)")
    docker = _which("docker")
    if docker:
        ok, out = wait_for_docker(docker, emit, stop)
        if not ok:
            note_trouble()
            raise SandboxError(colima_stuck_message(out, next_fix()))
    clear_trouble()
    ncpu, mem = docker_capacity(docker) if docker else (None, None)
    return "Colima is running%s and Docker is answering." % (" with %d CPUs and %.0f GB" % (ncpu, mem)
                                                               if ncpu else "")



def repair_colima(emit=None, reset=False, stop=None):
    """Fix a Colima that runs but whose Docker won't answer (or that won't start). Only after the user confirmed the
    card. repair: force-stop, then start sized for this Mac. reset: also delete Colima's Linux machine first (the
    sandboxes' folders on the Mac stay; what was installed inside them goes). Waits until Docker answers."""
    col = _which("colima")
    if not col:
        raise SandboxError("Colima isn't installed. Use sandbox_preflight: it shows the install card.")
    TROUBLE["resets" if reset else "repairs"] += 1
    note_trouble()
    _run_progress([col, "stop", "--force"], 300, emit, "Force-stopping Colima...", stop)   # fine if already stopped
    if reset:
        code, out = _run_progress([col, "delete", "--force"], 300, emit, "Deleting Colima's Linux machine...", stop)
        if code != 0:
            raise SandboxError("Colima's Linux machine couldn't be deleted.\nIt said: %s\n%s"
                               % (out.strip()[-600:], FIX_WORDS[next_fix()]))
    argv = colima_start_argv(col)
    code, out = _run_progress(argv, COLIMA_START_LIMIT, emit, "Starting Colima again (%s CPUs, %s GB)..."
                              % (argv[3], argv[5]), stop)
    if code != 0:
        tail = colima_log_tail()
        raise SandboxError("Colima still didn't start after %s.\nIt said: %s\n%s%s"
                           % ("a reset" if reset else "a forced restart", out.strip()[-600:],
                              ("Last lines of Colima's log (%s):\n%s\n" % (colima_log(), tail)) if tail else "",
                              FIX_WORDS[next_fix()]))
    services.record("colima", "default", "Colima (the sandboxes' Linux machine)")
    docker = _which("docker")
    if docker:
        ok, out = wait_for_docker(docker, emit, stop)
        if not ok:
            raise SandboxError(colima_stuck_message(out, next_fix()))
    clear_trouble()
    ncpu, mem = docker_capacity(docker) if docker else (None, None)
    return "Colima was %s and Docker is answering%s." % ("reset" if reset else "repaired",
                                                        " (%d CPUs, %.0f GB)" % (ncpu, mem) if ncpu else "")

def _exists(kind, exe, ref, image=False):
    if kind == "apple":
        argv = [exe, "image", "inspect", ref] if image else [exe, "inspect", ref]
    else:
        argv = [exe, "image" if image else "container", "inspect", ref]
    code, out = _quiet(argv, 30)
    return code == 0 and out not in ("", "[]")


def _offline_network(exe):
    """Apple container has no 'no network' switch, so an internal (host-only) network is used: no internet."""
    code, out = _quiet([exe, "network", "inspect", OFFLINE_NET], 30)
    if code == 0 and out not in ("", "[]"):
        return
    code, out = _quiet([exe, "network", "create", "--internal", OFFLINE_NET], 60)
    if code != 0:
        raise SandboxError("Couldn't make the offline network (it needs macOS 26 or newer): %s. Turn the sandbox's "
                           "network on, or use Docker (Colima) which can switch it off on older macOS." % out[-300:])


def arch_args(kind, sb):
    """amd64 mode: x86 Linux on Apple silicon. Apple's tool translates it with Rosetta; Docker uses Rosetta too when
    Docker Desktop/OrbStack/Colima have it switched on (otherwise QEMU, which is many times slower)."""
    if sb.get("arch") != "amd64":
        return []
    return ["--arch", "amd64"] if kind == "apple" else ["--platform", "linux/amd64"]


def create_args(kind, exe, sb):
    """The command that starts the sandbox's container: only the work folder is visible, with limits."""
    name, image = container_name(sb["name"]), sb.get("image_in_use") or sb["image"]
    mem = "%dG" % max(1, int(sb["memory_gb"]))
    cpus = str(max(1, int(sb["cpus"])))
    vol = "%s:/work" % work(sb)
    if kind == "apple":
        argv = [exe, "run", "-d", "--name", name, "-c", cpus, "-m", mem, "-v", vol, "-w", "/work"]
        argv += arch_args(kind, sb)
        if not sb["network"]:
            _offline_network(exe)
            argv += ["--network", OFFLINE_NET]
    else:
        argv = [exe, "run", "-d", "--name", name, "--label", "%s=%s" % (LABEL, slug(sb["name"])),
                "--cpus", cpus, "--memory", mem, "--pids-limit", "2048",
                "--security-opt", "no-new-privileges", "-v", vol, "-w", "/work",
                "--network", "bridge" if sb["network"] else "none"] + arch_args(kind, sb)
    return argv + ["-e", "HOME=/root", "-e", "PATCHBAY_SANDBOX=1", image, "sleep", "infinity"]


def ensure_container(sb, emit=None, stop=None):
    """Make sure the sandbox's container exists and is running, with the sandbox's current network setting."""
    kind, exe = engine_ready(emit=emit, stop=stop)
    name = container_name(sb["name"])
    if sb.get("container_network") is not None and sb.get("container_network") != sb["network"]:
        _save_and_remove(kind, exe, sb)        # network changed: keep what was installed, then recreate
    if _exists(kind, exe, name):
        _quiet([exe, "start", name], 120)
        services.record("sandbox", name, "sandbox %s" % sb["name"])
        return kind, exe, name
    if emit:
        emit({"type": "status", "text": "Starting the Linux sandbox (%s)... the first time it downloads the image"
                                        % (sb.get("image_in_use") or sb["image"])})
    image = sb.get("image_in_use") or sb["image"]
    if not _exists(kind, exe, image, image=True):
        # The Mac downloads the image (the starting Linux system); the sandbox itself stays offline.
        pull = [exe, "image", "pull"] if kind == "apple" else [exe, "pull"]
        code, out = _quiet(pull + arch_args(kind, sb) + [image], 1800)
        if code != 0:
            raise SandboxError("Couldn't download the Linux image %s: %s" % (image, out[-400:]))
    note = fit_to_engine(kind, exe, sb)
    if note and emit:
        emit({"type": "note", "text": note})
    code, out = _quiet(create_args(kind, exe, sb), 900)
    if code != 0 and "CPUs available" in out:
        raise SandboxError("Couldn't start the sandbox: %s\nLower its cpus with propose_sandbox_settings, or "
                           "restart Colima bigger with sandbox_preflight." % out[-400:])
    if code != 0:
        raise SandboxError("Couldn't start the sandbox: %s" % out[-600:])
    services.record("sandbox", name, "sandbox %s" % sb["name"])
    sb["container_network"] = sb["network"]
    if not sb.get("image_in_use") and (sb.get("setup_command") or sb.get("fixes")):
        # A brand-new container from the plain image: nothing the setup installed (or the saved fixes did) is in it
        # yet, so the build step runs the setup first.
        sb["needs_setup"] = True
    save(sb)
    return kind, exe, name


def _image_id(exe, ref):
    code, out = _quiet([exe, "image", "inspect", "-f", "{{.Id}}", ref], 30)
    return out.strip() if code == 0 and out.strip().startswith("sha256:") else ""


def _save_and_remove(kind, exe, sb):
    name = container_name(sb["name"])
    if _exists(kind, exe, name):
        saved = saved_image(sb["name"])
        old = _image_id(exe, saved) if kind == "docker" else ""
        argv = [exe, "commit"]
        if kind == "docker":
            argv += ["--change", "LABEL %s=%s" % (LABEL, slug(sb["name"]))]
        code, out = _quiet(argv + [name, saved], 900)
        if code == 0:
            sb["image_in_use"] = saved
            if old and old != _image_id(exe, saved):
                # The new save took the name, so the old one would sit there nameless, still taking disk space.
                _quiet([exe, "image", "rm", old], 120)
        _quiet(_rm_argv(kind, exe, name), 120)
    sb["container_network"] = None
    save(sb)


def _rm_argv(kind, exe, name):
    """Remove a container: its programs are killed first (-f), and its own scratch volumes go too (-v)."""
    return [exe, "rm", "-f", "-v", name] if kind == "docker" else [exe, "delete", "--force", name]


def stop_container(sb):
    stop_container_named(container_name(sb["name"]))


def stop_container_named(name):
    """Stop one sandbox container (what's installed in it stays). Only ever patchbay-sb-... names."""
    if not name.startswith("patchbay-sb-"):
        return
    kind, exe = engine()
    if kind:
        _quiet([exe, "stop", "-t", "3", name] if kind == "docker" else [exe, "stop", name], 60)
    services.forget("sandbox", name)


def stop_engine(kind):
    """Stop Colima ("colima") or Apple's container service ("apple_container"). Only called for one PatchBay
    started. Returns True if it stopped."""
    if kind == "colima":
        col = _which("colima")
        ok = bool(col) and _quiet([col, "stop"], 120)[0] == 0
    else:
        exe = _which("container")
        ok = bool(exe) and _quiet([exe, "system", "stop"], 120)[0] == 0
    services.forget(kind)
    return ok


def remove_container(sb, image_too=True):
    """Kill everything running in the sandbox's container, remove the container, and (image_too) the saved images it
    left. Checks it's really gone. Only ever touches patchbay-sb-... names and images labelled as PatchBay's."""
    kind, exe = engine()
    if not kind:
        return
    name = container_name(sb["name"])
    if kind == "docker":
        _quiet([exe, "kill", name], 30)          # stops any build still running in it, right away
    _quiet(_rm_argv(kind, exe, name), 120)
    services.forget("sandbox", name)
    if image_too:
        _quiet([exe, "image", "rm", saved_image(sb["name"])] if kind == "docker" else
               [exe, "image", "delete", "--force", saved_image(sb["name"])], 120)
        if kind == "docker":
            for iid in _dangling_ids(exe, slug(sb["name"])):
                _quiet([exe, "image", "rm", iid], 120)
    if _exists(kind, exe, name):
        raise SandboxError("The old container %s is still there after removing it, so the sandbox wasn't reset. "
                           "Docker may be stuck: try again in a minute, or restart Colima with sandbox_preflight."
                           % name)


def _dangling_ids(exe, which=None):
    """Nameless old saves of sandbox `which` (None: of any sandbox). Only images carrying PatchBay's label."""
    argv = [exe, "images", "-q", "--no-trunc", "-f", "dangling=true",
            "-f", "label=%s=%s" % (LABEL, which) if which else "label=%s" % LABEL]
    code, out = _quiet(argv, 30)
    return sorted({x.strip() for x in out.splitlines() if x.strip().startswith("sha256:")}) if code == 0 else []


# ---------------------------------------------------------------- memory and disk: what's really used, and clean-up
#
# Colima is a small Linux computer running inside the Mac. Two things about it look like a leak but are normal:
#  - Memory: while Colima runs, the Mac counts the memory Linux has touched (builds, file caches) as used, up to the
#    size Colima was started with (20 GB on a 36 GB Mac). It doesn't shrink when a build ends or a sandbox is reset;
#    only stopping Colima gives it back.
#  - Disk: Colima's disk file on the Mac grows when things are installed and doesn't shrink by itself when they're
#    removed. Clean up asks Linux to hand the freed space back (fstrim).

def _json_lines(out):
    rows = []
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            rows.append(d)
    return rows


def _allocated(path):
    """Space a file really takes on the Mac's disk (Colima's disk file is sparse: its listed size is its limit)."""
    try:
        st = os.stat(str(path))
    except OSError:
        return 0
    return getattr(st, "st_blocks", 0) * 512 or st.st_size


def colima_disk_files():
    """Colima's disk files on the Mac (the Linux machine's hard disk)."""
    lima = colima_home() / "_lima"
    files = []
    for top in (lima / "_disks", lima / "colima"):
        if top.is_dir():
            for p in top.rglob("*"):
                if p.is_file() and not p.is_symlink() and "disk" in p.name.lower():
                    files.append(p)
    return files


def _sandbox_slugs():
    return {slug(x["name"]): x["name"] for x in all_sandboxes()}


def _patchbay_containers(exe):
    """Every PatchBay sandbox container Docker has, running or stopped: [{"name", "state", "sandbox"}]."""
    code, out = _quiet([exe, "ps", "-a", "--filter", "name=^patchbay-sb-", "--format", "{{json .}}"], 30)
    known = _sandbox_slugs()
    rows = []
    for d in _json_lines(out) if code == 0 else []:
        n = str(d.get("Names") or "").split(",")[0]
        if not n.startswith("patchbay-sb-"):
            continue
        rows.append({"name": n, "state": str(d.get("State") or d.get("Status") or "").lower(),
                     "sandbox": known.get(n[len("patchbay-sb-"):])})
    return rows


def _programs_inside(exe, cname):
    """Programs running in a container other than its idle 'sleep infinity'."""
    code, out = _quiet([exe, "top", cname, "-eo", "pid,args"], 20)
    if code != 0:
        code, out = _quiet([exe, "top", cname], 20)
    if code != 0:
        return []
    rows = [l.strip() for l in out.splitlines()[1:] if l.strip()]
    return [r for r in rows if "sleep infinity" not in r]


def usage():
    """What the sandboxes really use right now. Only reads. Returns a dict the Sandbox tab and the chat show."""
    kind, exe = engine()
    u = {"engine": kind, "running": False, "containers": [], "images": [], "dangling": 0, "docker_df": [],
         "colima": None, "words": []}
    if kind != "docker":
        u["words"].append("Usage details are only shown for Docker (Colima)." if kind else "No Linux sandbox tool yet.")
        return u
    if _quiet([exe, "info"], 20)[0] != 0:
        u["words"].append("Docker isn't running, so the sandboxes use no memory right now.")
    else:
        u["running"] = True
        stats = {}
        code, out = _quiet([exe, "stats", "--no-stream", "--format", "{{json .}}"], 40)
        for d in _json_lines(out) if code == 0 else []:
            stats[str(d.get("Name") or "")] = d
        for c in _patchbay_containers(exe):
            st = stats.get(c["name"], {})
            c["memory"] = str(st.get("MemUsage") or "").split("/")[0].strip() if c["state"] == "running" else ""
            c["cpu"] = str(st.get("CPUPerc") or "") if c["state"] == "running" else ""
            c["programs"] = _programs_inside(exe, c["name"]) if c["state"] == "running" else []
            c["busy"] = bool(c["sandbox"] and busy(c["sandbox"]))
            u["containers"].append(c)
        code, out = _quiet([exe, "images", "--filter", "reference=patchbay-sb-*", "--format", "{{json .}}"], 30)
        for d in _json_lines(out) if code == 0 else []:
            u["images"].append({"name": "%s:%s" % (d.get("Repository"), d.get("Tag")), "size": d.get("Size") or ""})
        u["dangling"] = len(_dangling_ids(exe))
        code, out = _quiet([exe, "system", "df", "--format", "{{json .}}"], 60)
        for d in _json_lines(out) if code == 0 else []:
            u["docker_df"].append({"type": d.get("Type"), "size": d.get("Size"), "reclaimable": d.get("Reclaimable")})
    if _which("colima"):
        info = colima_info()
        files = colima_disk_files()
        u["colima"] = {"running": info["running"], "cpus": info["cpus"], "memory_gb": info["memory_gb"],
                       "disk_on_mac": human(sum(_allocated(f) for f in files)) if files else ""}
        if info["running"]:
            u["words"].append(
                "Colima is running with %s GB of memory set aside. While it runs, the Mac shows that memory as used "
                "(it fills up as builds run and doesn't go down after a build or a reset). That's normal. Only stopping "
                "Colima gives it back." % _gb(info["memory_gb"]))
    left = [c for c in u["containers"] if not c["sandbox"]]
    extra = [c for c in u["containers"] if c["programs"] and not c["busy"]]
    if left:
        u["words"].append("%d container%s left from deleted sandboxes: %s." % (
            len(left), "s are" if len(left) > 1 else " is", ", ".join(c["name"] for c in left)))
    if extra:
        u["words"].append("Programs are still running in %s with nothing started by PatchBay: a build that kept "
                          "going after it was stopped." % ", ".join(c["name"] for c in extra))
    if u["dangling"]:
        u["words"].append("%d old saved cop%s of sandboxes without a name (taking disk space)." % (
            u["dangling"], "ies" if u["dangling"] > 1 else "y"))
    return u


def usage_text(u=None):
    u = u or usage()
    lines = list(u["words"])
    for c in u["containers"]:
        lines.append("%s (%s): %s%s%s" % (c["name"], c["sandbox"] or "sandbox deleted", c["state"],
                                          ", memory %s, CPU %s" % (c["memory"], c["cpu"]) if c.get("memory") else "",
                                          ", %d program%s running inside" % (len(c["programs"]),
                                                                             "s" if len(c["programs"]) != 1 else "")
                                          if c.get("programs") else ""))
    for i in u["images"]:
        lines.append("Saved image %s: %s" % (i["name"], i["size"]))
    if u.get("colima") and u["colima"].get("disk_on_mac"):
        lines.append("Colima's disk file takes %s on the Mac." % u["colima"]["disk_on_mac"])
    for d in u["docker_df"]:
        lines.append("All of Docker, %s: %s (%s could be freed)" % (d["type"], d["size"], d["reclaimable"]))
    return "\n".join(lines) or "Nothing to show."


def cleanup(emit=None):
    """Clean up after the sandboxes. Only ever PatchBay's own things (patchbay-sb-... containers and images with
    PatchBay's label); other Docker containers, images and volumes are never touched. Sandboxes that are building
    right now are left alone. What's installed in a sandbox stays (its container is only stopped)."""
    kind, exe = engine()
    if kind != "docker":
        raise SandboxError("Clean up works with Docker (Colima). %s" % (
            "Apple's container tool is in use." if kind else "No Linux sandbox tool is installed."))
    if _quiet([exe, "info"], 20)[0] != 0:
        raise SandboxError("Docker isn't running, so there's nothing in memory to clean up.")
    say = (lambda t: emit({"type": "status", "text": t})) if emit else (lambda t: None)
    done = []
    known = _sandbox_slugs()
    for c in _patchbay_containers(exe):
        if not c["sandbox"]:
            say("Removing %s (its sandbox was deleted)..." % c["name"])
            _quiet([exe, "kill", c["name"]], 30)
            _quiet([exe, "rm", "-f", "-v", c["name"]], 120)
            done.append("removed %s, left from a deleted sandbox" % c["name"])
        elif c["state"] == "running" and not busy(c["sandbox"]):
            say("Stopping %s (nothing is building in it)..." % c["name"])
            _quiet([exe, "stop", "-t", "3", c["name"]], 60)
            services.forget("sandbox", c["name"])
            done.append("stopped %s (what's installed in it stays; it starts again by itself)" % c["name"])
    code, out = _quiet([exe, "images", "--filter", "reference=patchbay-sb-*", "--format", "{{.Repository}}:{{.Tag}}"],
                       30)
    for ref in out.split() if code == 0 else []:
        m = re.match(r"^patchbay-sb-(.+):saved$", ref)
        if m and m.group(1) not in known:
            _quiet([exe, "image", "rm", ref], 120)
            done.append("removed saved image %s (its sandbox was deleted)" % ref)
    old = _dangling_ids(exe)
    for iid in old:
        _quiet([exe, "image", "rm", iid], 120)
    if old:
        done.append("removed %d old nameless sandbox save%s" % (len(old), "s" if len(old) > 1 else ""))
    col = _which("colima")
    if col and uses_colima(exe):
        say("Asking Colima to give freed disk space back to the Mac...")
        before = sum(_allocated(f) for f in colima_disk_files())
        code, out = _quiet([col, "ssh", "--", "sudo", "fstrim", "-a"], 300)
        after = sum(_allocated(f) for f in colima_disk_files())
        if code == 0:
            done.append("gave freed disk space back to the Mac%s" % (
                " (%s)" % human(before - after) if before > after else ""))
    if not done:
        return "Nothing to clean up: no leftover sandbox containers, old saves or stray builds."
    return "Cleaned up: " + "; ".join(done) + ". Nothing else in Docker was touched."


def stop_colima_now():
    """Stop Colima to give its memory back to the Mac. Refused while a sandbox is building."""
    if busy():
        raise SandboxError("A sandbox is building right now. Press Stop first, then stop Colima.")
    col = _which("colima")
    if not col:
        raise SandboxError("Colima isn't installed.")
    if not colima_info()["running"]:
        return "Colima isn't running, so it isn't using any memory."
    if not stop_engine("colima"):
        raise SandboxError("Colima didn't stop. Colima says:\n%s" % colima_status_text())
    return ("Colima is stopped and its memory is back with the Mac. The next sandbox build starts it again "
            "(that takes about a minute).")


# ---------------------------------------------------------------- the Mac sandbox (sandbox-exec)

def mac_profile(sb):
    """macOS sandbox rules: write only in the sandbox and temp folders, read nothing private in the home folder,
    no Keychain, and no network unless it's switched on."""
    box = str(folder(sb["name"]).resolve())
    q = lambda s: json.dumps(s)
    rules = ["(version 1)", "(allow default)",
             "(deny file-write*)",
             "(allow file-write* (subpath %s) (subpath \"/private/tmp\") (subpath \"/private/var/folders\") "
             "(subpath \"/dev\"))" % q(box),
             "(deny file-read-data (subpath %s) (subpath \"/Volumes\"))" % q(str(HOME.resolve())),
             "(allow file-read-data (subpath %s))" % q(box),
             "(deny mach-lookup (global-name \"com.apple.SecurityServer\") (global-name \"com.apple.securityd\") "
             "(global-name \"com.apple.security.agent\") (global-name \"com.apple.pasteboard.1\"))"]
    if not sb["network"]:
        rules.append("(deny network*)")
    return "\n".join(rules) + "\n"


def mac_env(sb, base_path):
    """A small, plain environment: no tokens, passwords or keys from the Mac's own settings."""
    d = folder(sb["name"])
    env = {"PATH": base_path, "HOME": str(d / "home"), "TMPDIR": str(d / "tmp") + "/", "LANG": "en_US.UTF-8",
           "TERM": "dumb", "PATCHBAY_SANDBOX": "1", "SHELL": "/bin/zsh", "USER": "sandbox"}
    for k in ("DEVKITPRO", "DEVKITA64", "DEVKITARM", "DEVKITPPC"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    dkp = env.get("DEVKITPRO") or "/opt/devkitpro"
    if Path(dkp).is_dir():
        env["DEVKITPRO"] = dkp
        for var, sub in (("DEVKITA64", "devkitA64"), ("DEVKITARM", "devkitARM"), ("DEVKITPPC", "devkitPPC")):
            if (Path(dkp) / sub).is_dir():
                env.setdefault(var, str(Path(dkp) / sub))
    return env


def mac_argv(sb, command):
    d = folder(sb["name"])
    prof = d / "sandbox.sb"
    prof.write_text(mac_profile(sb))
    limits = "ulimit -t %d 2>/dev/null; ulimit -f %d 2>/dev/null; " % (
        int(sb["timeout_min"]) * 60 * max(1, int(sb["cpus"])), int(sb["disk_gb"]) * 1024 * 1024 * 2)
    return ["/usr/bin/sandbox-exec", "-f", str(prof), "/bin/zsh", "-c", limits + "setopt pipefail 2>/dev/null; " + command]


# ---------------------------------------------------------------- create / reset / delete / settings

def create(name, source, kind="linux", image=None, emit=None, recipe=None):
    """Make a new sandbox with a copy of the source folder. Nothing outside PatchBay's data folder is changed.
    recipe: settings worked out from the project (sandboxrecipe.suggest), applied before `image`."""
    check_name(name)
    if kind not in ("linux", "mac"):
        raise SandboxError("A sandbox is either linux (a Linux container) or mac (runs on the Mac, locked down).")
    src = Path(os.path.expanduser(str(source or ""))).resolve()
    if not src.is_dir():
        raise SandboxError("%s isn't a folder on the Mac." % src)
    d = folder(name)
    if (d / "sandbox.json").exists():
        raise SandboxError("There's already a sandbox called %s. Reset it, or pick another name." % name)
    if root().resolve() in src.parents or src == root().resolve():
        raise SandboxError("That folder is itself inside a sandbox.")
    if kind == "mac" and not Path("/usr/bin/sandbox-exec").exists():
        raise SandboxError("This Mac has no sandbox-exec, so a Mac sandbox can't be made. Use a linux sandbox.")
    if image and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$", image):
        raise SandboxError("That doesn't look like an image name (e.g. ubuntu:24.04).")
    for sub in ("home", "tmp"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    if emit:
        emit({"type": "status", "text": "Copying %s into the sandbox..." % src.name})
    try:
        skipped, leftovers = copy_in(src, d / "work")
    except Exception:
        shutil.rmtree(str(d), ignore_errors=True)
        raise
    sb = dict(DEFAULTS, name=name, source=str(src), kind=kind, created=time.strftime("%Y-%m-%d %H:%M:%S"),
              skipped=skipped[:50], left_out=leftovers[:50], left_out_count=len(leftovers))
    if recipe:
        sb = apply_recipe(sb, recipe)
    if image:
        sb["image"] = image
    save(sb)
    return sb


def reset(name, emit=None):
    """Throw away everything in the sandbox and copy the project in fresh. Settings are kept.
    Anything still running in it (a build started from the chat or the Sandbox tab) is stopped first, so nothing keeps
    building, writing files or using memory in the background."""
    sb = load(name)
    if stop_runs(name) and emit:
        emit({"type": "status", "text": "Stopped what was still running in sandbox %s." % name})
    remove_container(sb)
    sb.pop("image_in_use", None)
    sb["container_network"] = None
    d = folder(name)
    for sub in ("home", "tmp"):
        shutil.rmtree(str(d / sub), ignore_errors=True)
        (d / sub).mkdir(parents=True, exist_ok=True)
    if emit:
        emit({"type": "status", "text": "Copying %s in fresh..." % Path(sb["source"]).name})
    skipped, leftovers = copy_in(sb["source"], d / "work", bool(sb.get("copy_ignored")))
    sb["skipped"], sb["left_out"], sb["left_out_count"] = skipped[:50], leftovers[:50], len(leftovers)
    sb["last_run"] = None
    sb["suspect"] = []
    sb["needs_setup"] = bool(sb.get("setup_command") or sb.get("fixes"))
    save(sb)
    add_note(name, "Reset: the project was copied in fresh and everything installed inside was thrown away. The saved "
                   "tool swaps and fixes are kept and are redone by the next setup step.", "patchbay")
    return load(name)


def delete(name):
    sb = load(name)
    stop_runs(name)
    remove_container(sb)
    shutil.rmtree(str(folder(name)), ignore_errors=True)
    return "Deleted sandbox %s. Your real project wasn't touched." % name


def set_options(name, **opts):
    """Change network, cpus, memory_gb, timeout_min, disk_gb or image. Returns the sandbox."""
    sb = load(name)
    for k, v in opts.items():
        if v is None:
            continue
        if k == "network":
            sb["network"] = bool(v)
        elif k == "copy_ignored":
            sb["copy_ignored"] = bool(v)      # used at the next reset
        elif k in ("cpus", "memory_gb", "timeout_min", "disk_gb"):
            lim = {"cpus": (1, 14), "memory_gb": (1, int(sb.get("memory_max") or 28)), "timeout_min": (1, 600),
                   "disk_gb": (1, 500)}[k]
            try:
                n = int(v)
            except (TypeError, ValueError):
                raise SandboxError("%s must be a whole number." % k)
            if not lim[0] <= n <= lim[1]:
                raise SandboxError("%s must be between %d and %d." % (k, lim[0], lim[1]))
            sb[k] = n
        elif k == "arch":
            if v not in ARCHES:
                raise SandboxError("arch is native (fast, arm64) or amd64 (x86 Linux through Rosetta).")
            if v != sb.get("arch"):
                sb["arch"] = v
                sb.pop("image_in_use", None)      # what was installed is for the other kind of processor
        elif k == "image":
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$", str(v)):
                raise SandboxError("That doesn't look like an image name (e.g. ubuntu:24.04).")
            sb["image"] = str(v)
            sb.pop("image_in_use", None)
        else:
            raise SandboxError("Unknown sandbox setting %s." % k)
    if sb["kind"] == "linux" and any(k in opts and opts[k] is not None for k in ("cpus", "memory_gb", "image", "arch")):
        remove_container(sb, image_too=any(opts.get(k) is not None for k in ("image", "arch")))
        sb["container_network"] = None
    save(sb)
    return sb


def apply_recipe(sb, recipe):
    """Put a recipe's settings on a sandbox (checked like set_options). Blocked commands are only ever added to:
    taking one away means deleting the sandbox and making it again."""
    sb = dict(sb)
    for k in RECIPE_KEYS:
        if recipe.get(k) not in (None, ""):
            v = recipe[k]
            if k in ("recipe_notes", "recipe_from"):
                v = [str(x)[:300] for x in (v if isinstance(v, list) else [v])][:20]
            else:
                v = str(v)[:4000]
            sb[k] = v
    if sb.get("output"):
        out = str(sb["output"]).strip().lstrip("/")
        if ".." in Path(out).parts:
            raise SandboxError("The build output must be a path inside the project.")
        sb["output"] = out
    have = {r["pattern"] for r in sb.get("blocked") or []}
    for rule in recipe.get("blocked") or []:
        try:
            re.compile(rule["pattern"])
        except (re.error, KeyError, TypeError):
            raise SandboxError("A blocked command pattern isn't valid: %r" % (rule,))
        if rule["pattern"] not in have:
            sb["blocked"] = list(sb.get("blocked") or []) + [{"pattern": rule["pattern"],
                                                               "why": str(rule.get("why") or "")[:300]}]
    if recipe.get("memory_max"):
        sb["memory_max"] = max(1, min(28, int(recipe["memory_max"])))
    for k, lim in (("cpus", (1, 14)), ("memory_gb", (1, int(sb.get("memory_max") or 28))), ("timeout_min", (1, 600)),
                   ("disk_gb", (1, 500))):
        if recipe.get(k) is not None:
            sb[k] = max(lim[0], min(lim[1], int(recipe[k])))
    if recipe.get("image"):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$", str(recipe["image"])):
            raise SandboxError("That doesn't look like an image name (e.g. ubuntu:24.04).")
        sb["image"] = str(recipe["image"])
    if recipe.get("arch"):
        if recipe["arch"] not in ARCHES:
            raise SandboxError("arch is native (fast, arm64) or amd64 (x86 Linux through Rosetta).")
        sb["arch"] = recipe["arch"]
    return sb


def set_recipe(name, recipe):
    """Change a sandbox's recipe (setup/build commands, output, limits...). Returns the sandbox."""
    sb = load(name)
    new = apply_recipe(sb, recipe)
    if sb["kind"] == "linux" and any(new.get(k) != sb.get(k) for k in ("cpus", "memory_gb", "image", "arch")):
        remove_container(sb, image_too=any(new.get(k) != sb.get(k) for k in ("image", "arch")))
        if new.get("image") != sb.get("image") or new.get("arch") != sb.get("arch"):
            new.pop("image_in_use", None)
        new["container_network"] = None
    save(new)
    return new


# ---------------------------------------------------------------- what was changed and learned (kept)

def add_note(name, text, by="ai"):
    """One line in the sandbox's notebook: what was changed or learned, so later steps don't redo or undo it.
    by: ai (the chat's AI) or patchbay (PatchBay itself). The same text again isn't added twice."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:300]
    if not text:
        raise SandboxError("Give the note: one short line on what you changed or learned.")
    sb = load(name)
    notes = [n for n in sb.get("notes") or [] if n.get("text") != text]
    notes.append({"time": time.strftime("%Y-%m-%d %H:%M"), "by": by, "text": text})
    sb["notes"] = notes[-NOTES_MAX:]
    save(sb)
    return sb


def set_alias(name, tool, use, why=""):
    """Make `tool` mean `use` for every command in a Linux sandbox (e.g. objcopy -> llvm-objcopy, whose LLVM tools
    read files for every processor). Done by a link in ALIAS_DIR (before /usr/bin in any PATH), made again before every
    run, so it holds after make clean, a new container and a reset. use '' takes the swap away."""
    sb = load(name)
    if sb["kind"] != "linux":
        raise SandboxError("Tool swaps are for Linux sandboxes.")
    tool, use = str(tool or "").strip(), str(use or "").strip()
    if not TOOL_RE.match(tool) or (use and not TOOL_RE.match(use)):
        raise SandboxError("Give program names only, like objcopy and llvm-objcopy.")
    if tool == use:
        raise SandboxError("A program can't be swapped for itself.")
    aliases = dict(sb.get("tool_aliases") or {})
    if use:
        aliases[tool] = use
    else:
        aliases.pop(tool, None)
    sb["tool_aliases"] = aliases
    save(sb)
    if use:
        add_note(name, "Tool swap kept for every run: %s means %s%s." % (tool, use, " (%s)" % why if why else ""),
                 "patchbay")
    return load(name)


def add_fix(name, command, why=""):
    """Save a fix to the sandbox's own system (a file put in place, a link): it's part of every setup step from now
    on, so a reset or a new container gets it again. Packages belong in the setup's apt-get line instead."""
    command = str(command or "").strip()
    if not command or "\n" in command:
        raise SandboxError("Give the fix as one shell command line.")
    sb = load(name)
    fixes = [f for f in sb.get("fixes") or [] if f.get("command") != command]
    fixes.append({"command": command[:2000], "why": re.sub(r"\s+", " ", str(why or "")).strip()[:300],
                  "time": time.strftime("%Y-%m-%d %H:%M")})
    if len(fixes) > FIXES_MAX:
        raise SandboxError("This sandbox already has %d saved fixes. Take an old one away first." % FIXES_MAX)
    sb["fixes"] = fixes
    save(sb)
    add_note(name, "Saved fix (redone by every setup): %s" % (why or command[:120]), "patchbay")
    return load(name)


def remove_fix(name, number):
    sb = load(name)
    fixes = list(sb.get("fixes") or [])
    if not 1 <= int(number) <= len(fixes):
        raise SandboxError("There's no saved fix number %s (there are %d)." % (number, len(fixes)))
    gone = fixes.pop(int(number) - 1)
    sb["fixes"] = fixes
    save(sb)
    add_note(name, "Took away saved fix: %s" % (gone.get("why") or gone["command"][:120]), "patchbay")
    return load(name)


def _aliases(sb):
    return {k: v for k, v in (sb.get("tool_aliases") or {}).items() if TOOL_RE.match(k) and TOOL_RE.match(v)}


def link_lines(sb):
    """Shell lines that link each tool swap into ALIAS_DIR. A swap is only linked when its program is installed
    (llvm-objcopy, or the newest llvm-objcopy-NN), so a swap saved before the setup installed it never leaves the build
    without objcopy at all. Only PatchBay's own links are ever replaced or removed there, never a real program."""
    lines = []
    for tool, use in sorted(_aliases(sb).items()):
        dest = "%s/%s" % (ALIAS_DIR, tool)
        lines.append('t=$(command -v %s || ls -1 /usr/bin/%s-[0-9]* 2>/dev/null | sort -V | tail -n 1); '
                     'if [ -e %s ] && [ ! -L %s ]; then :; elif [ -n "$t" ]; then ln -sf "$t" %s; else rm -f %s; fi'
                     % (use, use, dest, dest, dest, dest))
    return lines


def check_lines(sb):
    """Shell lines that check each swap holds even under a PATH a build script sets itself; a loud failure if not."""
    lines = []
    for tool, use in sorted(_aliases(sb).items()):
        lines.append('v=$(env -i PATH=%s %s --version 2>&1); case "$v" in *LLVM*|*llvm*) '
                     'echo "PatchBay check: %s is the LLVM version (good)";; *) echo "PatchBay check FAILED: under the '
                     'PATH build scripts use, %s is still not %s: $(echo "$v" | sed -n 1p). The tool swap did not '
                     'take." >&2; exit 97;; esac' % (PLAIN_PATH, tool, tool, tool, use))
    return lines


def prelude(sb):
    """Shell lines run before every command in a Linux sandbox: no install questions, and the tool swaps."""
    if sb.get("kind") != "linux":
        return ""
    lines = ["export DEBIAN_FRONTEND=noninteractive"]
    if _aliases(sb):
        lines += link_lines(sb)
        lines.append("rm -rf %s 2>/dev/null" % OLD_ALIAS_DIR)     # links from the first version of this
    return "; ".join(lines) + "; "


SUSPECT_WINDOW = 90       # files a failed step wrote in its last 90 seconds may be half-made
SUSPECT_MAX = 30          # more than this = a big compile stopped; those files are ninja's/make's business


def recent_files(sb, since, top=None, limit=400000):
    """Files in the sandbox's copy written since `since` that aren't the project's own files (so a build made them).
    Returns [(rel, mtime, size)], or None when there are too many to be a half-made result."""
    w = work(sb)
    src = Path(sb.get("source") or "/nonexistent")
    out, seen = [], 0
    for dirpath, dirs, files in os.walk(str(top or w)):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            seen += 1
            if seen > limit:
                return None
            p = Path(dirpath) / f
            try:
                st = p.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_mtime < since:
                continue
            rel = p.relative_to(w).as_posix()
            if (src / rel).exists():
                continue          # one of the project's own files (maybe just edited): never treated as half-made
            out.append((rel, st.st_mtime, st.st_size))
            if len(out) > SUSPECT_MAX:
                return None
    return out


def note_suspects(name, ended):
    """After a failed run: remember the files it wrote just before it failed (e.g. a library a script wrote and then
    found wrong). A later run of the same build would see them, think that part is done, and skip it."""
    sb = load(name)
    try:
        found = recent_files(sb, ended - SUSPECT_WINDOW)
    except OSError:
        found = None
    new = [{"path": r, "mtime": m, "size": n} for r, m, n in found or []]
    paths = {x["path"] for x in new}
    old = [x for x in sb.get("suspect") or [] if x["path"] not in paths]     # still waiting from an earlier failure
    sb["suspect"] = (old + new)[-SUSPECT_MAX:]
    save(sb)
    return [x["path"] for x in sb["suspect"]]


def clear_suspects(name, emit=None):
    """Before the next build: delete the files a failed step left half-made, if nothing changed them since, so the
    build makes them again instead of skipping that part. Returns the paths removed."""
    sb = load(name)
    gone = []
    for x in sb.get("suspect") or []:
        try:
            p = inside_work(sb, x["path"])
            st = p.lstat()
            if stat.S_ISREG(st.st_mode) and abs(st.st_mtime - x["mtime"]) < 0.01 and st.st_size == x["size"]:
                p.unlink()
                gone.append(x["path"])
        except (SandboxError, OSError, KeyError):
            continue
    sb["suspect"] = []
    save(sb)
    if gone:
        add_note(name, "Removed %s before building again: the step that failed wrote %s just before it failed, so the "
                       "build would have skipped making %s again." % (", ".join(gone[:4]), "it" if len(gone) == 1
                                                                     else "them", "it" if len(gone) == 1 else "them"),
                 "patchbay")
        if emit:
            emit({"type": "note", "text": "Removed %d half-made file%s from the last failed step (%s), so the build "
                                          "makes %s again." % (len(gone), "" if len(gone) == 1 else "s",
                                                               ", ".join(gone[:3]), "it" if len(gone) == 1 else
                                                               "them")})
    return gone


BUILDISH = re.compile(r"(^|[\s;&|(])(make|ninja|cmake|meson|cargo|gradle|\S*build\S*\.sh|\S*prepare\S*\.sh|"
                      r"python3?\s+(-\S+\s+)*\S+\.py)(\s|$|;)")


LOOKS_ONLY = re.compile(r"^\s*(cd\s+\S+\s*&&\s*)?(cat|tail|head|grep|egrep|rg|less|sed\s+-n|awk|find|ls|which|"
                        r"command|type|echo|file|wc|stat|du|df|readelf|objdump|nm|llvm-nm|ar\s+t)\b")


def is_buildish(command):
    """A command that builds (make, ninja, a build or prepare script, a python script), not one that only looks."""
    return bool(BUILDISH.search(command or "")) and not LOOKS_ONLY.search(command or "")


def notes_text(sb, limit=12):
    """The sandbox's notebook in plain lines, for the end of the AI's prompt and for Claude."""
    rows = []
    for tool, use in sorted((sb.get("tool_aliases") or {}).items()):
        rows.append("Tool swap on every run: %s = %s" % (tool, use))
    for i, f in enumerate(sb.get("fixes") or [], 1):
        rows.append("Saved fix %d (redone by every setup): %s" % (i, f.get("why") or f["command"][:160]))
    rows += ["%s: %s" % ("PatchBay" if n.get("by") == "patchbay" else "Note", n["text"])
             for n in (sb.get("notes") or [])[-limit:]
             if not n["text"].startswith(("Tool swap kept", "Saved fix (redone"))]
    if sb.get("needs_setup"):
        rows.append("The container is new: the setup step runs before the next build.")
    return "\n".join("- " + r for r in rows)


# ---------------------------------------------------------------- running a command inside

# Runs going on right now: sandbox name -> list of (cancel, done) events. Reset and Delete use it to stop them first.
_RUNS = {}
_RUNS_LOCK = threading.Lock()


def _begin_run(name):
    ev = (threading.Event(), threading.Event())
    with _RUNS_LOCK:
        _RUNS.setdefault(name, []).append(ev)
    return ev


def _end_run(name, ev):
    ev[1].set()
    with _RUNS_LOCK:
        rows = _RUNS.get(name) or []
        if ev in rows:
            rows.remove(ev)
        if not rows:
            _RUNS.pop(name, None)


def busy(name=None):
    """Whether a command is running in sandbox `name` (None: in any sandbox) right now."""
    with _RUNS_LOCK:
        return bool(_RUNS.get(name)) if name else any(_RUNS.values())


def stop_runs(name, wait=20):
    """Stop every command running in the sandbox and wait (up to `wait` seconds) until they've ended.
    Returns how many were running."""
    with _RUNS_LOCK:
        rows = list(_RUNS.get(name) or [])
    for cancel, _ in rows:
        cancel.set()
    end = time.time() + wait
    for _, done in rows:
        done.wait(max(0.1, end - time.time()))
    return len(rows)


def _stream(argv, cwd, env, emit, timeout, stop, on_kill=None, cancel=None):
    started = time.time()
    # Its own process group, so Stop ends everything the command started (not just the shell).
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)

    def kill():
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()
    lines = []
    killed = None
    watch_done = threading.Event()

    def watch():
        # Checks Stop and the time limit even while the program prints nothing.
        nonlocal killed
        while not watch_done.wait(0.5):
            if stop is not None and stop.is_set():
                killed = "(stopped: you pressed Stop)"
            elif cancel is not None and cancel.is_set():
                killed = "(stopped: the sandbox was reset or deleted)"
            elif time.time() - started > timeout:
                killed = "(stopped: it took longer than %d minutes)" % (timeout // 60)
            if killed:
                if on_kill:
                    on_kill()
                kill()
                return

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        with services.track(proc, "a sandbox command", " ".join(argv)[:300]):
            for line in proc.stdout:
                line = line.rstrip("\n")
                lines.append(line)
                del lines[:-600]
                if line.strip() and emit:
                    emit({"type": "progress", "text": line[:300]})
            code = proc.wait()
    finally:
        watch_done.set()
        if proc.poll() is None:
            # Something went wrong while reading. Killing only the docker command would leave the build running
            # inside the container, so the container's programs are stopped too.
            if on_kill and not killed:
                on_kill()
            kill()
        proc.stdout.close()
    if killed:
        lines.append(killed)
    return code, "\n".join(lines)


# Commands run with pipefail, so "make 2>&1 | tail -80" fails when make fails (plain sh would report tail's exit code
# 0, and a failed build looked like it worked). Ubuntu's sh (dash) has no pipefail, so bash runs the command when the
# sandbox has it. The outer "sh -l" still loads the login settings (PATH and so on) first.
PIPEFAIL_SH = ('if command -v bash >/dev/null 2>&1; then exec bash -o pipefail -c "$1"; '
               'else exec sh -c "$1"; fi')


def run(name, command, emit=None, stop=None, base_path=None, record=True):
    """Run one shell command inside the sandbox, in its copy of the project. Returns (exit code, output).
    record=False: a quiet look PatchBay takes itself (reading a script); the sandbox's last run stays as it was."""
    sb = load(name)
    command = (command or "").strip()
    if not command:
        raise SandboxError("Give a command to run, like make or ./run-tests.sh.")
    for rule in sb.get("blocked") or []:
        if re.search(rule["pattern"], command):
            raise SandboxError("Not run inside the sandbox: %s" % rule["why"])
    timeout = int(sb["timeout_min"]) * 60
    w = work(sb)
    if not w.is_dir():
        raise SandboxError("The sandbox's copy of the project is missing. Reset the sandbox.")
    ev = _begin_run(name)
    try:
        code, out = _run_inside(name, sb, command, emit, stop, base_path, record, timeout, w, ev[0])
        if not record or ev[0].is_set():
            # A reset or delete stopped it: the sandbox is being emptied, so nothing about this run is saved in it.
            return code, out
        if code != 0 and is_buildish(command):
            note_suspects(name, time.time())
        sb = load(name)
        used = size_of(folder(name))
        sb["last_run"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "command": command[:300], "code": code,
                          "output": out[-20000:]}
        save(sb)
    finally:
        _end_run(name, ev)
    if used > int(sb["disk_gb"]) * 1024 ** 3:
        out += ("\n(Warning: the sandbox now uses %.1f GB, over its %d GB limit. Reset it or delete build files.)"
                % (used / 1024 ** 3, sb["disk_gb"]))
    return code, out


def _run_inside(name, sb, command, emit, stop, base_path, record, timeout, w, cancel):
    if sb["kind"] == "mac":
        if not Path("/usr/bin/sandbox-exec").exists():
            raise SandboxError("This Mac has no sandbox-exec.")
        path = base_path or "/opt/devkitpro/tools/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        return _stream(mac_argv(sb, command), w, mac_env(sb, path), emit, timeout, stop, cancel=cancel)
    kind, exe, cname = ensure_container(sb, emit, stop)
    sb = load(name)
    if record and sb.get("suspect") and is_buildish(command):
        clear_suspects(name, emit)
    argv = [exe, "exec", "-w", "/work", cname, "sh", "-lc", PIPEFAIL_SH, "sh", prelude(sb) + command]
    # Stopping only the docker command on the Mac would leave make/clang running inside, so the container is stopped.
    return _stream(argv, w, cli_env(), emit, timeout, stop, cancel=cancel,
                   on_kill=lambda: _quiet([exe, "stop", "-t", "3", cname] if kind == "docker" else [exe, "stop", cname],
                                          60))


def listing(name, sub="", limit=300):
    sb = load(name)
    p = inside_work(sb, sub)
    if not p.is_dir():
        raise SandboxError("%s isn't a folder in the sandbox." % (sub or "/"))
    rows = []
    for x in sorted(p.iterdir())[:limit]:
        try:
            rows.append("%s%s  %s" % (x.name, "/" if x.is_dir() else "", "" if x.is_dir() else
                                      human(x.stat().st_size)))
        except OSError:
            continue
    return "\n".join(rows) or "(empty)"


def changed_files(name, limit=200):
    """Files in the sandbox that are new or different from the real project (what a build made or changed)."""
    sb = load(name)
    w, src = work(sb), Path(sb["source"])
    out = []
    for dirpath, dirnames, filenames in os.walk(w):
        dirnames[:] = [d for d in dirnames if d not in (".git",)]
        for n in filenames:
            p = Path(dirpath) / n
            rel = p.relative_to(w)
            o = src / rel
            try:
                st = p.lstat()
                if not o.exists():
                    out.append(("new", str(rel), st.st_size))
                elif o.stat().st_size != st.st_size or _differs(o, p):
                    out.append(("changed", str(rel), st.st_size))
            except OSError:
                continue
            if len(out) >= limit:
                return out
    return out


def _differs(a, b):
    if a.stat().st_size > 50 * 1024 ** 2:
        return a.stat().st_mtime < b.stat().st_mtime
    return a.read_bytes() != b.read_bytes()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# ---------------------------------------------------------------- copying a result out (after a confirm card)

HISTORY = "copy-out.json"


def _history():
    try:
        return json.loads((root() / HISTORY).read_text())
    except (OSError, ValueError):
        return []


def _save_history(h):
    (root() / HISTORY).write_text(json.dumps(h[-300:], indent=1))


def plan_copy_out(name, rel, to=None):
    """What copy_out would do, for the card. The file (or whole folder, like a built app folder) goes back to the
    same place in the real project unless `to` (a path inside the project) is given."""
    sb = load(name)
    src = inside_work(sb, rel)
    if src.is_symlink():
        raise SandboxError("%s is a link; copy the real file instead." % rel)
    if src.is_dir():
        return _plan_folder_out(sb, name, src, rel, to)
    if not src.is_file():
        raise SandboxError("%s isn't a file or folder in the sandbox." % rel)
    dest_root = Path(sb["source"]).resolve()
    dest = (dest_root / str(to or rel).lstrip("/")).resolve()
    if dest != dest_root and dest_root not in dest.parents:
        raise SandboxError("%s is outside the project folder." % (to or rel))
    if is_secret(dest):
        raise SandboxError("PatchBay doesn't write files named like secrets (%s)." % dest.name)
    change = {"sandbox": name, "from": str(src), "rel": str(src.relative_to(work(sb).resolve())), "to": str(dest),
              "to_rel": str(dest.relative_to(dest_root)), "size": src.stat().st_size}
    words = "Copy %s (%s) out of sandbox %s\n  to %s%s" % (
        change["rel"], human(change["size"]), name, dest,
        "\n  (the file there now is backed up first)" if dest.exists() else "\n  (new file)")
    return change, words


def _folder_files(top):
    """(relative path, size) of every plain file in a folder; links and secret-named files are left out."""
    rows, left_out = [], []
    for dirpath, dirnames, filenames in os.walk(top):
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)) or d in SKIP_DIRS:
                left_out.append(os.path.relpath(os.path.join(dirpath, d), top))
                dirnames.remove(d)
        for n in filenames:
            p = Path(dirpath) / n
            if p.is_symlink() or is_secret(p) or not p.is_file():
                left_out.append(str(p.relative_to(top)))
                continue
            rows.append((str(p.relative_to(top)), p.stat().st_size))
    return sorted(rows), left_out


def _plan_folder_out(sb, name, src, rel, to):
    dest_root = Path(sb["source"]).resolve()
    dest = (dest_root / str(to or rel).lstrip("/")).resolve()
    if dest == dest_root or dest_root not in dest.parents:
        raise SandboxError("%s must be a folder inside the project (not the project itself)." % (to or rel))
    if dest.exists() and not dest.is_dir():
        raise SandboxError("%s is a file in the real project, not a folder." % dest)
    files, left_out = _folder_files(src)
    if not files:
        raise SandboxError("%s is empty in the sandbox. Has it been built?" % rel)
    total = sum(n for _, n in files)
    change = {"sandbox": name, "from": str(src), "rel": str(src.relative_to(work(sb).resolve())), "to": str(dest),
              "to_rel": str(dest.relative_to(dest_root)), "size": total, "folder": True, "files": len(files)}
    words = "Copy the folder %s (%d files, %s) out of sandbox %s\n  to %s%s" % (
        change["rel"], len(files), human(total), name, dest,
        "\n  (the folder there now is backed up first, then replaced)" if dest.exists() else "\n  (new folder)")
    words += "\n\n" + "\n".join("  %s  (%s)" % (r, human(n)) for r, n in files[:40])
    if len(files) > 40:
        words += "\n  ... and %d more" % (len(files) - 40)
    if left_out:
        words += "\nLeft out (links or secret-looking names): %s" % ", ".join(left_out[:10])
    return change, words


def _copy_folder_out(change, entry):
    src, dest = Path(change["from"]), Path(change["to"])
    if dest.exists():
        bk = root() / "_backups" / time.strftime("%Y%m%d-%H%M%S") / ("%d-%s" % (entry["id"], dest.name))
        bk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(dest), str(bk), symlinks=True)
        entry["backup"] = str(bk)
    tmp = dest.with_name(dest.name + ".patchbay-tmp")
    if tmp.exists():
        shutil.rmtree(str(tmp))
    files, _ = _folder_files(src)
    for r, _n in files:
        (tmp / r).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src / r), str(tmp / r))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        old = dest.with_name(dest.name + ".patchbay-old")
        if old.exists():
            shutil.rmtree(str(old))
        dest.replace(old)
        tmp.replace(dest)
        shutil.rmtree(str(old), ignore_errors=True)
    else:
        tmp.replace(dest)
    entry["folder"] = True


def copy_out(change):
    """Do a planned copy (checked again first). The file or folder it replaces is backed up. Returns a plain result."""
    again, _ = plan_copy_out(change["sandbox"], change["rel"], change["to_rel"])
    if again["to"] != change["to"] or bool(again.get("folder")) != bool(change.get("folder")):
        raise SandboxError("The sandbox changed since the card was shown. Ask again.")
    if change.get("folder"):
        with LOCK:
            h = _history()
            entry = {"id": (h[-1]["id"] + 1) if h else 1, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "sandbox": change["sandbox"], "file": change["rel"] + "/", "to": change["to"]}
            _copy_folder_out(again, entry)
            h.append(entry)
            _save_history(h)
        return "Copied the folder %s (%d files) to %s%s. (Copy #%d: it can be undone.)" % (
            change["rel"], again["files"], change["to"], " (the old one is backed up)" if entry.get("backup") else "",
            entry["id"])
    with LOCK:
        h = _history()
        entry = {"id": (h[-1]["id"] + 1) if h else 1, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "sandbox": change["sandbox"], "file": change["rel"], "to": change["to"]}
        dest = Path(change["to"])
        if dest.exists():
            bk = root() / "_backups" / time.strftime("%Y%m%d-%H%M%S") / ("%d-%s" % (entry["id"], dest.name))
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(dest), str(bk))
            entry["backup"] = str(bk)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".patchbay-tmp")
        shutil.copy2(change["from"], str(tmp))
        tmp.replace(dest)
        h.append(entry)
        _save_history(h)
    return "Copied %s to %s%s. (Copy #%d: it can be undone.)" % (
        change["rel"], dest, " (the old one is backed up)" if entry.get("backup") else "", entry["id"])


def recent_copies(limit=15):
    return _history()[-limit:][::-1]


def undo_copy(n):
    with LOCK:
        h = _history()
        e = next((x for x in h if x["id"] == int(n)), None)
        if not e or e.get("undone"):
            raise SandboxError("There's no copy #%s to undo." % n)
        dest = Path(e["to"])
        if e.get("folder"):
            gone = root() / "_backups" / time.strftime("%Y%m%d-%H%M%S") / ("undo-%d-%s" % (e["id"], dest.name))
            gone.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.move(str(dest), str(gone))       # what the copy put there is kept, not deleted
            if e.get("backup"):
                shutil.copytree(e["backup"], str(dest), symlinks=True)
        elif e.get("backup"):
            shutil.copy2(e["backup"], str(dest))
        elif dest.exists():
            bk = root() / "_backups" / time.strftime("%Y%m%d-%H%M%S") / ("undo-%d-%s" % (e["id"], dest.name))
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(bk))
        e["undone"] = True
        _save_history(h)
    return "Undid copy #%d: %s is back the way it was." % (e["id"], dest)


def step_command(sb, step):
    """The setup or build command, with {cpus} filled in (containers don't always tell programs their CPU limit).
    The setup also redoes every saved fix, after the packages are in, so a reset or new container gets them back."""
    cmd = sb.get("setup_command" if step == "setup" else "build_command") or ""
    if step == "setup" and sb.get("fixes"):
        fixes = " && ".join("{ %s ; }" % f["command"].rstrip("; ") for f in sb["fixes"])
        cmd = "%s && %s" % (cmd, fixes) if cmd else fixes
    if step == "setup" and sb.get("kind") == "linux" and _aliases(sb):
        # The setup has just installed what the swaps point to: link them now and check they hold under a plain PATH.
        swaps = "{ %s ; }" % "; ".join(link_lines(sb) + check_lines(sb))
        cmd = "%s && %s" % (cmd, swaps) if cmd else swaps
    return cmd.replace("{cpus}", str(int(sb["cpus"])))


def recipe_text(sb):
    """The sandbox's recipe in plain lines, for the chat."""
    rows = []
    if sb.get("setup_command"):
        rows.append("Setup (once, needs the internet): %s" % sb["setup_command"])
    if sb.get("build_command"):
        rows.append("Build: %s" % sb["build_command"])
    if sb.get("output"):
        rows.append("Result: %s" % sb["output"])
    for r in sb.get("blocked") or []:
        rows.append("Never run inside: %s" % r["why"])
    if sb.get("upload_hint"):
        rows.append("After copying the result out: %s" % sb["upload_hint"])
    for tool, use in sorted((sb.get("tool_aliases") or {}).items()):
        rows.append("Tool swap on every run: %s means %s" % (tool, use))
    for i, f in enumerate(sb.get("fixes") or [], 1):
        rows.append("Saved fix %d (redone by every setup): %s" % (i, f.get("why") or f["command"][:160]))
    rows += ["Note: %s" % n for n in sb.get("recipe_notes") or []]
    if sb.get("recipe_from"):
        rows.append("Worked out from: %s" % ", ".join(sb["recipe_from"][:8]))
    return "\n".join(rows)


def summary(sb):
    """One plain paragraph about a sandbox, for the chat."""
    last = sb.get("last_run") or {}
    return ("Sandbox %s (%s%s): copy of %s. Network %s. Limits: %s, %d min per command, %d GB disk.%s%s" % (
        sb["name"], "Linux, " + (sb.get("image_in_use") or sb["image"]) if sb["kind"] == "linux" else "Mac, locked down",
        " (x86 through Rosetta)" if sb["kind"] == "linux" and sb.get("arch") == "amd64" else "", sb["source"], "ON" if sb["network"] else "off",
        "%d CPUs, %d GB memory" % (sb["cpus"], sb["memory_gb"]) if sb["kind"] == "linux" else
        "CPU time %d min" % (sb["timeout_min"] * sb["cpus"]),
        sb["timeout_min"], sb["disk_gb"],
        " Last run: %s -> exit %s." % (last.get("command"), last.get("code")) if last else "",
        " Left out as secrets: %s." % ", ".join(sb["skipped"][:8]) if sb.get("skipped") else "") + left_out_words(sb))
