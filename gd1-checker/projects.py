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


def build_env():
    env = dict(os.environ)
    if "DEVKITPRO" not in env and Path("/opt/devkitpro").is_dir():
        env["DEVKITPRO"] = "/opt/devkitpro"
        env["DEVKITARM"] = "/opt/devkitpro/devkitARM"
        env["DEVKITPPC"] = "/opt/devkitpro/devkitPPC"
        env["PATH"] = "/opt/devkitpro/tools/bin:/opt/devkitpro/devkitA64/bin:" + env.get("PATH", "")
    return env


def run_shell(cwd, command, emit, timeout=BUILD_TIMEOUT):
    """Run one command in a folder on the Mac, showing its output live. Returns (exit code, output)."""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    if not Path(shell).exists():
        shell = "/bin/sh"
    started = time.time()
    # No keyboard input: a command that asks for a password (like sudo) stops instead of waiting forever.
    proc = subprocess.Popen([shell, "-lc", command], cwd=str(cwd), env=build_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            del lines[:-400]
            if line.strip():
                emit({"type": "progress", "text": line[:300]})
            if time.time() - started > timeout:
                proc.kill()
                lines.append("(stopped: it took longer than %d minutes)" % (timeout // 60))
                break
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    return code, "\n".join(lines)


def run_build(pr, emit):
    """Run the project's build command in its folder."""
    code, out = run_shell(folder_of(pr), pr["build"], emit)
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

def toolchain_report():
    """What's installed for building Switch homebrew. Only reads."""
    lines = []
    dkp = Path(build_env().get("DEVKITPRO") or "/opt/devkitpro")
    if not dkp.is_dir():
        return ("devkitPro is NOT installed on this Mac. It's needed to build Switch homebrew. The user installs it "
                "from https://devkitpro.org/wiki/Getting_Started (the macOS installer package), then in Terminal: "
                "sudo dkp-pacman -S switch-dev")
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
    lines.append("To update everything, the user runs this in Terminal (it asks for the Mac password): "
                 "sudo dkp-pacman -Syu   To add a library: sudo dkp-pacman -S <name>, e.g. switch-ffmpeg. "
                 "Commands that need sudo can't run from the chat.")
    return "\n".join(lines)
