"""
Source projects: the source code of a program that runs on a device, kept in
a folder on the Mac Studio. The AI can read and search it freely. Changing a
file or running the build only happens after you press Confirm on a card,
and every changed file is backed up first.

A project is saved in the device's settings file under "projects", e.g.
  {"name": "mygame", "folder": "~/code/mygame", "build": "make",
   "output": "mygame.nro", "device_path": "/switch/mygame/mygame.nro"}

Only uses Python's built-in parts.
"""

import json
import os
import re
import shutil
import sys
import subprocess
import time
from pathlib import Path

import gd1check as core

SKIP_DIRS = {".git", "build", "out", "node_modules", "__pycache__", ".vscode", ".idea"}
TEXT_LIMIT = 2 * 1024 * 1024
BUILD_TIMEOUT = 60 * 60


class ProjectError(Exception):
    pass


def all_projects(cfg):
    return cfg.get("projects") or []


def get(cfg, name):
    for pr in all_projects(cfg):
        if pr["name"].lower() == (name or "").lower():
            return pr
    names = ", ".join(p["name"] for p in all_projects(cfg)) or "none yet"
    raise ProjectError("There's no project called %s for %s. Projects: %s. Use set_project to add one."
                       % (name, cfg["device_name"], names))


def folder_of(pr):
    f = Path(os.path.expanduser(pr["folder"])).resolve()
    if not f.is_dir():
        raise ProjectError("The project folder %s isn't on the Mac (moved or renamed?)." % f)
    return f


def inside(pr, rel):
    """A path inside the project folder. Refuses anything that points outside it."""
    root = folder_of(pr)
    p = (root / (rel or "").lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise ProjectError("%s is outside the project folder." % rel)
    return root, p


def save(config_path, project):
    with open(config_path, encoding="utf-8") as f:
        raw = json.load(f)
    projects = [p for p in raw.get("projects", []) if p["name"].lower() != project["name"].lower()]
    projects.append(project)
    raw["projects"] = projects
    tmp = Path(str(config_path) + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    tmp.replace(config_path)


def make_project(name, folder, build, output, device_path):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$", name or ""):
        raise ProjectError("Give the project a short name (letters, numbers, - or _).")
    f = Path(os.path.expanduser(folder or "")).resolve()
    if not f.is_dir():
        raise ProjectError("%s isn't a folder on the Mac." % f)
    return {"name": name, "folder": str(f), "build": (build or "make").strip(), "output": (output or "").strip(),
            "device_path": (device_path or "").strip()}


# ----------------------------------------------------------------- reading

def is_text(path):
    try:
        with open(path, "rb") as fh:
            return core.looks_like_text(fh.read(4096))
    except OSError:
        return False


def tree(pr, sub="", limit=400):
    root, base = inside(pr, sub)
    rows, n = [], 0
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(dirpath).relative_to(root)
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            n += 1
            if n <= limit:
                full = Path(dirpath) / fn
                rows.append("%s  (%s)" % ((rel / fn).as_posix(), core.human_size(full.stat().st_size)))
    more = "\n... and %d more files" % (n - limit) if n > limit else ""
    return "Project %s in %s\nBuild command: %s\nBuilt file: %s\nGoes on the device at: %s\n%d files:\n%s%s" % (
        pr["name"], root, pr["build"], pr.get("output") or "(not set)", pr.get("device_path") or "(not set)",
        n, "\n".join(rows), more)


def read(pr, rel, start=1, count=300):
    root, p = inside(pr, rel)
    if not p.is_file():
        raise ProjectError("%s isn't a file in the project." % rel)
    if not is_text(p):
        raise ProjectError("%s isn't a text file." % rel)
    lines = p.read_text("utf-8", "replace").splitlines()
    start = max(1, start)
    end = min(len(lines), start + count - 1)
    return "%s  (%d lines)\n--- lines %d to %d ---\n%s" % (rel, len(lines), start, end, "\n".join(
        "%5d| %s" % (i, lines[i - 1][:400]) for i in range(start, end + 1)))


def search(pr, text, limit=80):
    root = folder_of(pr)
    needle, hits = text.lower(), []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(files):
            f = Path(dirpath) / fn
            try:
                if f.stat().st_size > TEXT_LIMIT or not is_text(f):
                    continue
                for n, line in enumerate(f.read_text("utf-8", "replace").splitlines(), 1):
                    if needle in line.lower():
                        hits.append("%s:%d: %s" % (f.relative_to(root).as_posix(), n, line.strip()[:200]))
                        if len(hits) >= limit:
                            return "\n".join(hits) + "\n(stopped at %d matches)" % limit
            except OSError:
                continue
    return "\n".join(hits) if hits else "No matches in project %s." % pr["name"]


# ----------------------------------------------------------------- changing (only after Confirm)

def write_file(cfg, pr, rel, new_text):
    """Back up the current file to the device's data folder, then write the new one."""
    root, p = inside(pr, rel)
    stamp = core.now_stamp()
    backup = None
    if p.exists():
        backup = core.local_path_for(core.unique_dir(core.data_dir(cfg) / "project-backups" / pr["name"], stamp), rel)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, backup)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".saving")
    tmp.write_text(new_text, "utf-8")
    tmp.replace(p)
    log = core.data_dir(cfg) / "project-history.json"
    hist = json.loads(log.read_text()) if log.exists() else []
    hist.append({"id": len(hist) + 1, "time": stamp, "project": pr["name"], "file": rel,
                 "backup": str(backup) if backup else None})
    log.write_text(json.dumps(hist, indent=2))
    return backup


EXTRA_PATHS = ["/opt/devkitpro/tools/bin", "/opt/devkitpro/devkitA64/bin", "/opt/devkitpro/devkitARM/bin",
               "/opt/devkitpro/devkitPPC/bin", "/opt/devkitpro/portlibs/switch/bin", "/opt/devkitpro/portlibs/3ds/bin",
               "/opt/devkitpro/pacman/bin", "/opt/homebrew/bin", "/usr/local/bin"]


def build_env():
    """The environment builds run in: devkitPro's variables and tools, and Homebrew, are always there."""
    env = dict(os.environ)
    dkp = env.get("DEVKITPRO") or "/opt/devkitpro"
    if Path(dkp).is_dir():
        env["DEVKITPRO"] = dkp
        for var, sub in (("DEVKITA64", "devkitA64"), ("DEVKITARM", "devkitARM"), ("DEVKITPPC", "devkitPPC")):
            if (Path(dkp) / sub).is_dir():
                env.setdefault(var, str(Path(dkp) / sub))
    have = env.get("PATH", "").split(":")
    add = [p for p in EXTRA_PATHS if Path(p).is_dir() and p not in have]
    env["PATH"] = ":".join(add + have)
    env["PATCHBAY_PATH"] = env["PATH"]
    return env


def run_shell(cwd, command, emit, timeout=BUILD_TIMEOUT, stop=None):
    """Run one command in a folder on the Mac, showing its output live. Returns (exit code, output)."""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    if not Path(shell).exists():
        shell = "/bin/sh"
    started = time.time()
    # No keyboard input: a command that asks for a password (like sudo) stops instead of waiting forever.
    proc = subprocess.Popen([shell, "-lc", 'export PATH="$PATCHBAY_PATH:$PATH"; ' + command], cwd=str(cwd),
                            env=build_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            del lines[:-400]
            if line.strip():
                emit({"type": "progress", "text": line[:300]})
            if stop is not None and stop.is_set():
                proc.kill()
                lines.append("(stopped: you pressed Stop)")
                break
            if time.time() - started > timeout:
                proc.kill()
                lines.append("(stopped: it took longer than %d minutes)" % (timeout // 60))
                break
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    return code, "\n".join(lines)


def cmake_build_dirs(command):
    """The build folders a command configures with cmake (-B dir or -Bdir)."""
    return [m.group(1) for m in re.finditer(r"-B\s*([^\s;&|]+)", command or "")]


def clear_cmake_cache(root, command):
    """Remove the saved cmake settings in the build folders, so the next configure starts fresh. Returns what went."""
    gone = []
    for d in cmake_build_dirs(command):
        b = (root / d).resolve()
        if b == root or root not in b.parents:
            continue
        for name in ("CMakeCache.txt", "CMakeFiles"):
            f = b / name
            if f.is_dir():
                shutil.rmtree(str(f), ignore_errors=True)
                gone.append(str(f))
            elif f.exists():
                f.unlink()
                gone.append(str(f))
    return gone


def run_build(pr, emit, stop=None):
    """Run the project's build command in its folder. If cmake's setup step fails, the build folder's old saved
    settings (often from an earlier try with different tools) are cleared and it's tried once more."""
    root = folder_of(pr)
    code, out = run_shell(root, pr["build"], emit, stop=stop)
    if code != 0 and "Configuring incomplete" in out and not (stop is not None and stop.is_set()):
        gone = clear_cmake_cache(root, pr["build"])
        if gone:
            emit({"type": "note", "text": "The setup step failed, so I cleared the build folder's old cmake settings "
                                          "and I'm trying once more."})
            code, out2 = run_shell(root, pr["build"], emit, stop=stop)
            out = ("(First try failed; cleared %s and tried again. Second try:)\n" % ", ".join(gone)) + out2
    built = None
    if pr.get("output"):
        _, built = inside(pr, pr["output"])
    return code, out, built


# ----------------------------------------------------------------- getting source code

PROJECTS_HOME = Path("~/Patchbay-projects")
GIT_URL = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9._~/-]+?(\.git)?/?$")


def clone_target(url, name=None):
    if not GIT_URL.match(url or ""):
        raise ProjectError("That doesn't look like a public git address (like https://github.com/owner/project).")
    name = name or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:40] or "project"
    dest = Path(os.path.expanduser(str(PROJECTS_HOME))) / name
    return name, dest


def clone(url, dest, emit):
    """Download a project's source code (with its submodules) into dest."""
    if dest.exists() and any(dest.iterdir()):
        raise ProjectError("%s already exists. Use that project, or pick another name." % dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, out = run_shell(dest.parent, "git clone --recurse-submodules %s %s" % (quote(url), quote(dest.name)), emit,
                          timeout=30 * 60)
    if code != 0:
        raise ProjectError("Downloading failed:\n" + out[-2000:])
    return out


def quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


# ----------------------------------------------------------------- the build tools (devkitPro)

DKP_INSTALLER = "https://github.com/devkitPro/pacman/releases/latest/download/devkitpro-pacman-installer.pkg"
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9_+.@-]{0,80}$")


def find_pacman():
    for cand in ("/opt/devkitpro/pacman/bin/pacman", "/usr/local/bin/dkp-pacman", "/usr/bin/dkp-pacman",
                 "/opt/homebrew/bin/dkp-pacman"):
        if Path(cand).exists():
            return cand
    return shutil.which("dkp-pacman")


def find_brew():
    for cand in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(cand).exists():
            return cand
    return shutil.which("brew")


def dkp_installed(package):
    """The installed version of a devkitPro package, or None."""
    pac = find_pacman()
    if not pac:
        return None
    try:
        r = subprocess.run([pac, "-Q", package], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def install_plan(manager, packages, update_all=False):
    """What installing packages will do, as (commands, needs_password, words for the card). Raises ProjectError."""
    pkgs = [str(x).strip() for x in (packages or []) if str(x).strip()]
    bad = [x for x in pkgs if not PACKAGE_NAME.match(x)]
    if bad:
        raise ProjectError("These don't look like package names: %s" % ", ".join(bad))
    if not pkgs and not update_all:
        raise ProjectError("Say which packages to install (e.g. switch-dev, switch-zlib), or update_all.")
    if manager == "homebrew":
        brew = find_brew()
        if not brew:
            raise ProjectError("Homebrew isn't installed on this Mac. It's a one-time setup the user does in Terminal: "
                               "the command is on https://brew.sh (copy it from that page). Then try again.")
        cmds = []
        if pkgs:
            cmds.append("%s install %s" % (quote(brew), " ".join(pkgs)))
        if update_all:
            cmds.append("%s upgrade %s" % (quote(brew), " ".join(pkgs)))
        return cmds, False, "Homebrew: " + " ".join(cmds).replace(quote(brew), "brew")
    if manager != "devkitpro":
        raise ProjectError("manager must be devkitpro or homebrew.")
    pac = find_pacman()
    cmds = []
    if not pac:
        if sys.platform != "darwin":
            raise ProjectError("devkitPro isn't installed, and the automatic installer is only for macOS. See "
                               "https://devkitpro.org/wiki/Getting_Started")
        # devkitPro's own macOS package manager installer, from its official GitHub releases.
        cmds.append("curl -fsSL -o /tmp/devkitpro-pacman-installer.pkg %s && installer -pkg "
                    "/tmp/devkitpro-pacman-installer.pkg -target /" % DKP_INSTALLER)
        pac = "/opt/devkitpro/pacman/bin/pacman"
    cmds.append("%s -S%s --needed --noconfirm %s" % (pac, "yu" if update_all else "y", " ".join(pkgs)))
    words = ("First install devkitPro's package manager (official installer from github.com/devkitPro). "
             if len(cmds) > 1 else "") + "devkitPro: %s%s" % (("update everything" + (" and install" if pkgs else "")) if update_all else "install",
                                                                (" " + ", ".join(pkgs)) if pkgs else "")
    return cmds, True, words


def run_admin(commands, emit, stop=None):
    """Run commands that need the Mac password. On macOS the Mac shows its own password box (nothing is typed here)."""
    # Note: never start with "((": the shell reads that as arithmetic.
    script = "{ %s; } 2>&1" % " && ".join(commands)
    if sys.platform == "darwin":
        apple = 'do shell script "%s" with administrator privileges' % script.replace("\\", "\\\\").replace('"', '\\"')
        emit({"type": "status", "text": "Waiting for the Mac's password box..."})
        command = "osascript -e %s" % quote(apple)
    elif os.geteuid() == 0:
        command = script
    else:
        command = "sudo -n sh -c %s" % quote(script)
    code, out = run_shell(Path.home(), command, emit, timeout=60 * 60, stop=stop)
    if code != 0 and "-128" in out:
        out += "\n(The password box was cancelled, so nothing was installed.)"
    return code, out


def install_packages(manager, packages, update_all, emit, stop=None):
    cmds, admin, _ = install_plan(manager, packages, update_all)
    if admin:
        return run_admin(cmds, emit, stop)
    return run_shell(Path.home(), " && ".join(cmds), emit, timeout=60 * 60, stop=stop)


def toolchain_report():
    """What's installed for building Switch homebrew. Only reads."""
    lines = []
    dkp = Path(build_env().get("DEVKITPRO") or "/opt/devkitpro")
    if not dkp.is_dir():
        return ("devkitPro is NOT installed on this Mac. It's needed to build Switch homebrew. Install it with "
                "propose_install_packages (manager devkitpro, packages [switch-dev] plus any the project needs): it "
                "installs devkitPro's package manager first. The Mac shows its own password box.")
    lines.append("devkitPro is installed at %s." % dkp)
    pac = None
    for cand in ("dkp-pacman", "/opt/devkitpro/pacman/bin/pacman"):
        if shutil.which(cand) or Path(cand).exists():
            pac = cand
            break
    if pac:
        try:
            out = subprocess.run([pac, "-Q"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout
            wanted = [l for l in out.splitlines() if l.split(" ")[0] in
                      ("libnx", "devkitA64", "switch-tools", "general-tools", "devkit-env", "switch-cmake",
                       "switch-pkg-config", "deko3d") or l.startswith("switch-")]
            lines.append("Installed packages (name version):\n" + "\n".join(wanted[:150]))
        except (OSError, subprocess.SubprocessError) as e:
            lines.append("Couldn't list the packages: %s" % e)
    else:
        lines.append("dkp-pacman wasn't found, so package versions are unknown.")
    env = build_env()
    for tool in ("cmake", "aarch64-none-elf-cmake", "arm-none-eabi-cmake", "make", "git", "pkg-config", "patch"):
        found = shutil.which(tool, path=env["PATH"])
        ver = ""
        if found and tool in ("cmake", "make", "git"):
            try:
                ver = (subprocess.run([found, "--version"], capture_output=True, text=True, timeout=10,
                                      stdin=subprocess.DEVNULL).stdout.splitlines() or [""])[0]
            except (OSError, subprocess.SubprocessError):
                pass
        lines.append("%s: %s" % (tool, ("%s %s" % (found, ver)).strip() if found else "NOT found"))
    if not shutil.which("cmake", path=env["PATH"]):
        lines.append("cmake is missing: the devkitPro cmake wrappers (aarch64-none-elf-cmake) need it. Install it with "
                     "propose_install_packages (manager homebrew, packages [cmake]).")
    lines.append("Missing devkitPro packages: install them with propose_install_packages (the Mac shows its own "
                 "password box). To update everything: propose_install_packages with update_all.")
    return "\n".join(lines)
