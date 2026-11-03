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
environment has only a few plain variables (no tokens or passwords).

Only uses Python's built-in parts.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

HOME = Path.home()
ROOT = HOME / "PatchBay-data" / "sandboxes"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
SKIP_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".docker", "Keychains", "__pycache__", "node_modules", ".patchbay-names"}
SECRET_NAMES = {"config.json", "access.json", "claude.json", "claude-aliases.json", "id_rsa", "id_ed25519", "id_ecdsa",
                ".env", ".netrc", ".git-credentials", "credentials", "credentials.json", "redaction_list.json"}
SECRET_ENDS = (".pem", ".key", ".p12", ".keychain", ".keychain-db", ".mobileprovision")
COPY_LIMIT = 20 * 1024 ** 3          # projects bigger than this aren't copied in
DEFAULTS = {"kind": "linux", "image": "ubuntu:24.04", "network": False, "cpus": 4, "memory_gb": 8,
            "timeout_min": 60, "disk_gb": 20, "arch": "native", "memory_max": 28,
            # The recipe: how this project is set up and built inside (see sandboxrecipe.py). Empty = none yet.
            "setup_command": "", "build_command": "", "output": "", "blocked": [], "upload_hint": "",
            "recipe_notes": [], "recipe_from": []}
RECIPE_KEYS = ("setup_command", "build_command", "output", "upload_hint", "recipe_notes", "recipe_from")
ARCHES = ("native", "amd64")         # native = arm64 on Apple silicon (fast); amd64 = x86 Linux through Rosetta
OFFLINE_NET = "patchbay-offline"     # Apple container: a host-only network (macOS 26+), used when network is off
EXTRA_PATHS = ["/usr/local/bin", "/opt/homebrew/bin", str(HOME / ".orbstack/bin"),
               "/Applications/Docker.app/Contents/Resources/bin"]
LOCK = threading.Lock()


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


def size_of(path, stop_at=None):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for n in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
        if stop_at and total > stop_at:
            break
    return total


def copy_in(source, dest):
    """Copy a project folder into the sandbox, leaving out secrets. Returns the number of files skipped as secrets."""
    source = Path(source).resolve()
    if size_of(source, COPY_LIMIT) > COPY_LIMIT:
        raise SandboxError("%s is over %d GB, too big to copy into a sandbox." % (source, COPY_LIMIT // 1024 ** 3))
    skipped = []

    def ignore(src, names):
        s = _ignore_from(source, src, names)
        skipped.extend(x for x in s if x not in SKIP_DIRS)
        return s

    if dest.exists():
        shutil.rmtree(str(dest))
    shutil.copytree(str(source), str(dest), symlinks=True, ignore=ignore)
    return skipped


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


# ---------------------------------------------------------------- engines (what runs the Linux sandboxes)

def _which(exe):
    path = os.environ.get("PATH", "") + ":" + ":".join(EXTRA_PATHS)
    return shutil.which(exe, path=path)


def cli_env():
    env = dict(os.environ)
    env["PATH"] = ":".join([p for p in EXTRA_PATHS if p not in env.get("PATH", "")] + [env.get("PATH", "")])
    return env


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


INSTALL_HELP = ("Linux sandboxes need a container tool on the Mac. The simplest is Apple's free one: download the "
                "installer (.pkg) from github.com/apple/container/releases, open it, then in Terminal run: "
                "container system start. (Docker via Colima works too: brew install colima docker, then colima start.)")


def engine_ready(start=True):
    """The engine, started if it was stopped. Raises SandboxError with plain steps if it can't be used."""
    kind, exe = engine()
    if not kind:
        raise SandboxError(INSTALL_HELP)
    if kind == "apple":
        code, out = _quiet([exe, "system", "status"], 20)
        if code != 0 and start:
            code, out = _quiet([exe, "system", "start", "--enable-kernel-install"], 300)
        if code != 0:
            raise SandboxError("Apple's container tool isn't running and didn't start: %s\nIn Terminal, run: "
                               "container system start" % out[-400:])
    else:
        code, out = _quiet([exe, "info"], 20)
        if code != 0 and start and _which("colima"):
            code, out = _quiet([_which("colima"), "start"], 600)
            if code == 0:
                code, out = _quiet([exe, "info"], 20)
        if code != 0:
            raise SandboxError("Docker isn't running: %s\nStart Colima (colima start in Terminal), OrbStack or Docker "
                               "Desktop, then try again." % out[-400:])
    return kind, exe


def engine_status():
    kind, exe = engine()
    if not kind:
        return {"engine": None, "running": False, "help": INSTALL_HELP}
    probe = [exe, "system", "status"] if kind == "apple" else [exe, "info"]
    running = _quiet(probe, 20)[0] == 0
    return {"engine": "Apple container" if kind == "apple" else "Docker", "running": running,
            "help": "" if running else ("In Terminal: container system start" if kind == "apple" else
                                        "Start Colima (colima start), OrbStack or Docker Desktop.")}


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
        argv = [exe, "run", "-d", "--name", name, "--cpus", cpus, "--memory", mem, "--pids-limit", "2048",
                "--security-opt", "no-new-privileges", "-v", vol, "-w", "/work",
                "--network", "bridge" if sb["network"] else "none"] + arch_args(kind, sb)
    return argv + ["-e", "HOME=/root", "-e", "PATCHBAY_SANDBOX=1", image, "sleep", "infinity"]


def ensure_container(sb, emit=None):
    """Make sure the sandbox's container exists and is running, with the sandbox's current network setting."""
    kind, exe = engine_ready()
    name = container_name(sb["name"])
    if sb.get("container_network") is not None and sb.get("container_network") != sb["network"]:
        _save_and_remove(kind, exe, sb)        # network changed: keep what was installed, then recreate
    if _exists(kind, exe, name):
        _quiet([exe, "start", name], 120)
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
    code, out = _quiet(create_args(kind, exe, sb), 900)
    if code != 0:
        raise SandboxError("Couldn't start the sandbox: %s" % out[-600:])
    sb["container_network"] = sb["network"]
    save(sb)
    return kind, exe, name


def _save_and_remove(kind, exe, sb):
    name = container_name(sb["name"])
    if _exists(kind, exe, name):
        code, out = _quiet([exe, "commit", name, saved_image(sb["name"])], 900)
        if code == 0:
            sb["image_in_use"] = saved_image(sb["name"])
        _quiet([exe, "rm", "-f", name] if kind == "docker" else [exe, "delete", "--force", name], 120)
    sb["container_network"] = None
    save(sb)


def stop_container(sb):
    kind, exe = engine()
    if kind:
        _quiet([exe, "stop", container_name(sb["name"])], 60)


def remove_container(sb, image_too=True):
    kind, exe = engine()
    if not kind:
        return
    name = container_name(sb["name"])
    _quiet([exe, "rm", "-f", name] if kind == "docker" else [exe, "delete", "--force", name], 120)
    if image_too:
        _quiet([exe, "image", "rm", saved_image(sb["name"])] if kind == "docker" else
               [exe, "image", "delete", "--force", saved_image(sb["name"])], 120)


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
    return ["/usr/bin/sandbox-exec", "-f", str(prof), "/bin/zsh", "-c", limits + command]


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
        skipped = copy_in(src, d / "work")
    except Exception:
        shutil.rmtree(str(d), ignore_errors=True)
        raise
    sb = dict(DEFAULTS, name=name, source=str(src), kind=kind, created=time.strftime("%Y-%m-%d %H:%M:%S"),
              skipped=skipped[:50])
    if recipe:
        sb = apply_recipe(sb, recipe)
    if image:
        sb["image"] = image
    save(sb)
    return sb


def reset(name, emit=None):
    """Throw away everything in the sandbox and copy the project in fresh. Settings are kept."""
    sb = load(name)
    remove_container(sb)
    sb.pop("image_in_use", None)
    sb["container_network"] = None
    d = folder(name)
    for sub in ("home", "tmp"):
        shutil.rmtree(str(d / sub), ignore_errors=True)
        (d / sub).mkdir(parents=True, exist_ok=True)
    if emit:
        emit({"type": "status", "text": "Copying %s in fresh..." % Path(sb["source"]).name})
    sb["skipped"] = copy_in(sb["source"], d / "work")[:50]
    sb["last_run"] = None
    save(sb)
    return sb


def delete(name):
    sb = load(name)
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


# ---------------------------------------------------------------- running a command inside

def _stream(argv, cwd, env, emit, timeout, stop, on_kill=None):
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
            kill()
        proc.stdout.close()
    if killed:
        lines.append(killed)
    return code, "\n".join(lines)


def run(name, command, emit=None, stop=None, base_path=None):
    """Run one shell command inside the sandbox, in its copy of the project. Returns (exit code, output)."""
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
    if sb["kind"] == "mac":
        if not Path("/usr/bin/sandbox-exec").exists():
            raise SandboxError("This Mac has no sandbox-exec.")
        path = base_path or "/opt/devkitpro/tools/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        code, out = _stream(mac_argv(sb, command), w, mac_env(sb, path), emit, timeout, stop)
    else:
        kind, exe, cname = ensure_container(sb, emit)
        argv = [exe, "exec", "-w", "/work", cname, "sh", "-lc", command]
        code, out = _stream(argv, w, cli_env(), emit, timeout, stop,
                            on_kill=lambda: _quiet([exe, "stop", cname], 60))
    sb = load(name)
    used = size_of(folder(name))
    sb["last_run"] = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "command": command[:300], "code": code,
                      "output": out[-20000:]}
    save(sb)
    if used > int(sb["disk_gb"]) * 1024 ** 3:
        out += ("\n(Warning: the sandbox now uses %.1f GB, over its %d GB limit. Reset it or delete build files.)"
                % (used / 1024 ** 3, sb["disk_gb"]))
    return code, out


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
    """The setup or build command, with {cpus} filled in (containers don't always tell programs their CPU limit)."""
    cmd = sb.get("setup_command" if step == "setup" else "build_command") or ""
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
        " Left out as secrets: %s." % ", ".join(sb["skipped"][:8]) if sb.get("skipped") else ""))
