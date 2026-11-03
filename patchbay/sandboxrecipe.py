"""
Works out what sandbox a project needs, by reading the project's own build files. Read-only: it never changes the
project. The result (a "recipe") is a plain dict that sandbox.create / sandbox.set_recipe take:

  image          the Linux system to start from, e.g. ubuntu:26.04 (from the docs, CI workflow or Dockerfile)
  setup_command  run once inside, with the internet on: apt packages, the project's own setup script
  build_command  run inside to build (plain make, cmake, meson, cargo, npm...); {cpus} = the sandbox's CPUs
  output         the built file or folder to copy out (e.g. build/release/PPSA98515)
  blocked        commands never run inside (e.g. make install that sends to a console): list of {pattern, why}
  upload_hint    the command to run on the MAC after copying out, to send the result to the device
  cpus, memory_gb, memory_max, disk_gb, timeout_min, arch
  recipe_notes   plain notes (what it guessed, ARM worries)
  recipe_from    which files it read

What it reads, in order: Dockerfile / .devcontainer, docs (BUILDING.md, README) and the Makefile for "Ubuntu NN.04",
tools/ci or scripts package lists (*packages*.txt), CI workflows (.github/workflows) for runs-on, apt-get install lines
and setup scripts, then the build system files. The chat's AI can change any part afterwards.

Only uses Python's built-in parts.
"""

import os
import re
from pathlib import Path

UBUNTU_RE = re.compile(r"\bUbuntu[ -](\d\d\.04)\b", re.I)
BASE_PACKAGES = ["sudo", "ca-certificates", "curl", "wget", "git", "make", "build-essential", "python3", "unzip",
                 "xz-utils", "file", "pkgconf"]
HEAVY = re.compile(r"\b(mesa|radv|llvm|emulator|chromium|webkit|qt6?|unreal|godot|dynarmic|ffmpeg)\b", re.I)
SENDS = re.compile(r"\b(ftp|ftplib|sftp|scp|ssh|rsync|curl\s+-T|nc|netcat|adb|_HOST|HOST)\b|install-ftp|upload|deploy",
                   re.I)
ENV_FILE = "/root/.patchbay-build-env"
MAX_READ = 400000


def _read(p):
    try:
        if p.is_file() and p.stat().st_size <= MAX_READ:
            return p.read_text("utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _rel(top, p):
    return str(p.relative_to(top))


def _versions(text):
    return UBUNTU_RE.findall(text or "")


def _make_vars(text):
    """NAME := value lines of a Makefile (simple ones only)."""
    out = {}
    for m in re.finditer(r"^([A-Z][A-Z0-9_]*)[ \t]*[:?+]?=[ \t]*(.*?)[ \t]*$", text, re.M):
        out.setdefault(m.group(1), m.group(2))
    return out


def _expand(s, vars_, depth=0):
    if depth > 4:
        return s
    new = re.sub(r"\$[({]([A-Z][A-Z0-9_]*)[)}]", lambda m: vars_.get(m.group(1), "<%s>" % m.group(1)), s)
    return new if new == s else _expand(new, vars_, depth + 1)


def _target(text, name):
    """The recipe lines (tab-indented) of a Makefile target."""
    m = re.search(r"^%s\s*:(?!=)[^\n]*\n((?:\t.*\n?)*)" % re.escape(name), text, re.M)
    return [ln[1:].strip() for ln in m.group(1).splitlines()] if m else []


def _package_list(p):
    words = []
    for ln in _read(p).splitlines():
        ln = ln.split("#", 1)[0].strip()
        words += [w for w in ln.split() if re.match(r"^[a-z0-9][a-z0-9.+-]*$", w)]
    return words


def _workflows(top):
    d = top / ".github" / "workflows"
    if not d.is_dir():
        return []
    return [p for p in sorted(d.iterdir()) if p.suffix in (".yml", ".yaml")][:20]


def _apt_lines(text):
    pk = []
    for m in re.finditer(r"apt(?:-get)?\s+install\s+([^\n&|;]+)", text):
        pk += [w for w in m.group(1).split() if re.match(r"^[a-z0-9][a-z0-9.+-]*$", w)]
    return pk


def _ci_script_call(text, script_rel):
    """How a CI workflow runs the setup script, made to work in the sandbox (GitHub's own names replaced)."""
    for ln in text.splitlines():
        if script_rel in ln:
            ln = ln.strip().lstrip("-").strip()
            ln = re.sub(r"^run:\s*\|?\s*", "", ln)
            ln = re.sub(r"\d?>>?\s*\"?\$\{?GITHUB_(ENV|PATH|OUTPUT)\}?\"?", "3>%s" % ENV_FILE, ln)
            ln = re.sub(r"\$\{\{[^}]*\}\}", "/opt/patchbay-tools", ln)
            ln = ln.replace("$RUNNER_TEMP", "/tmp").replace("${RUNNER_TEMP}", "/tmp")
            return ln if script_rel in ln else None
    return None


def suggest(source, cores=None, mac_memory_gb=None):
    """A recipe for the project in `source`. Never raises for odd projects: it says what it couldn't tell."""
    top = Path(os.path.expanduser(str(source))).resolve()
    if not top.is_dir():
        raise ValueError("%s isn't a folder." % top)
    cores = cores or os.cpu_count() or 8
    jobs = max(2, min(12, cores - 2))
    notes, read = [], []
    r = {"image": "ubuntu:24.04", "arch": "native", "cpus": jobs, "memory_gb": 8, "memory_max": 28,
         "disk_gb": 40, "timeout_min": 120, "blocked": []}

    makefile = next((p for p in (top / "Makefile", top / "makefile", top / "GNUmakefile") if p.is_file()), None)
    mk = _read(makefile) if makefile else ""
    if makefile:
        read.append(makefile.name)
    mvars = _make_vars(mk)
    docs = [p for p in (top / "docs" / "BUILDING.md", top / "BUILDING.md", top / "docs" / "building.md",
                        top / "README.md") if p.is_file()]
    doc_text = "\n".join(_read(p) for p in docs)
    read += [_rel(top, p) for p in docs]

    # 1) Which Linux. A Dockerfile says it exactly; otherwise what the Makefile/docs say; otherwise the CI runner.
    image, setup_steps, packages = None, [], []
    for df in (top / "Dockerfile", top / ".devcontainer" / "Dockerfile", top / "docker" / "Dockerfile"):
        t = _read(df)
        m = re.search(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", t, re.M | re.I)
        if m and re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$", m.group(1)):
            image = m.group(1)
            packages += _apt_lines(t)
            read.append(_rel(top, df))
            notes.append("Uses the Linux from %s (%s)." % (_rel(top, df), image))
            break
    wanted = _versions(mk[:3000]) or _versions(doc_text[:6000])
    ci_dirs = [d for d in (top / "tools" / "ci", top / "ci", top / "scripts", top / "tools", top / ".ci") if d.is_dir()]
    lists = {}
    for d in ci_dirs:
        for p in sorted(d.glob("*packages*.txt")):
            m = re.search(r"(\d\d\.04)", p.name)
            lists[m.group(1) if m else ""] = p
    setups = {}
    for d in ci_dirs:
        for p in sorted(d.glob("setup*.sh")) + sorted(d.glob("install-deps*.sh")) + sorted(d.glob("bootstrap*.sh")):
            m = re.search(r"(\d\d\.04)", p.name)
            setups.setdefault(m.group(1) if m else "", p)
    flows = _workflows(top)
    flow_text = "\n".join(_read(p) for p in flows)
    read += [_rel(top, p) for p in flows]
    runner = re.findall(r"runs-on:\s*\[?\s*ubuntu-(\d\d\.04)", flow_text)

    if not image:
        ver = wanted[0] if wanted else None
        if ver and (lists.get(ver) or lists.get("")) and not setups.get(ver):
            # The docs' own Ubuntu, with a plain package list: the simplest path (no setup script needed).
            image = "ubuntu:%s" % ver
            lp = lists.get(ver) or lists[""]
            packages += _package_list(lp)
            read.append(_rel(top, lp))
            notes.append("Builds on Ubuntu %s, as the project's docs say, with its package list %s."
                         % (ver, _rel(top, lp)))
        elif setups:
            ver = next((v for v in [ver] + runner + list(setups) if v and v in setups), "") or ver or \
                (runner[0] if runner else "24.04")
            sp = setups.get(ver) or next(iter(setups.values()))
            image = "ubuntu:%s" % (ver or "24.04")
            rel = _rel(top, sp)
            read.append(rel)
            if lists.get(ver):
                packages += _package_list(lists[ver])
            call = _ci_script_call(flow_text, rel)
            if not call:
                needs_arg = re.search(r"\$\{1:\?", _read(sp))
                call = "bash %s%s 3>%s" % (rel, " /opt/patchbay-tools" if needs_arg else "", ENV_FILE)
            elif not call.startswith(("bash ", "sh ", "./")):
                call = "bash " + call
            setup_steps.append(call)
            notes.append("Uses the project's own setup script %s on Ubuntu %s (as its CI does)." % (rel, ver))
            if "cmake" in _read(sp) or "cmake" in mk:
                packages.append("cmake")       # GitHub's runners have cmake built in; plain Ubuntu doesn't
        elif runner:
            image = "ubuntu:%s" % runner[0]
            notes.append("Uses Ubuntu %s, the same as the project's CI." % runner[0])
        elif ver:
            image = "ubuntu:%s" % ver
        else:
            image = "ubuntu:24.04"
            notes.append("The project doesn't say which Linux; using Ubuntu 24.04.")
        packages += _apt_lines(flow_text)
    r["image"] = image

    # 2) The build itself.
    build = None
    if makefile:
        if re.search(r"^JOBS\s*\?=", mk, re.M):
            build = "make JOBS={cpus}"
        else:
            build = "make -j{cpus}"
        extra = set()
        for p in list((top / "tools").glob("*.sh"))[:200] if (top / "tools").is_dir() else []:
            extra.update(re.findall(r"\$\{?([A-Z][A-Z0-9_]*_JOBS)\b", _read(p)))
        extra.discard("JOBS")
        extra = sorted(x for x in extra if not re.search(r"\b%s\b" % x, mk))[:6]
        if extra:
            build = "env %s %s" % (" ".join("%s={cpus}" % x for x in extra), build)
    elif (top / "CMakeLists.txt").is_file():
        build = "cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build -j{cpus}"
        packages += ["cmake", "ninja-build"]
        read.append("CMakeLists.txt")
    elif (top / "meson.build").is_file():
        build = "meson setup build --buildtype=release && meson compile -C build -j {cpus}"
        packages += ["meson", "ninja-build"]
    elif (top / "Cargo.toml").is_file():
        build = "cargo build --release -j {cpus}"
        setup_steps.append("curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal")
        notes.append("Rust is installed with rustup inside the sandbox.")
    elif (top / "package.json").is_file():
        build = "npm ci && npm run build"
        packages += ["nodejs", "npm"]
    else:
        notes.append("Couldn't tell how this project builds. Set the build command.")
    r["build_command"] = build or ""
    if build and "{cpus}" in build:
        notes.append("{cpus} in the build command is the sandbox's CPU count, so the build fits its limits.")

    # 3) Packages and the setup command (needs the internet once).
    seen, pk = set(), []
    for w in BASE_PACKAGES + packages:
        if w not in seen and w not in ("--yes", "-y"):
            seen.add(w)
            pk.append(w)
    steps = ["export DEBIAN_FRONTEND=noninteractive",
             "apt-get update",
             "apt-get install -y --no-install-recommends " + " ".join(pk),
             "git config --global --add safe.directory '*'"]
    if (top / ".gitmodules").is_file() and "submodule" not in mk:
        steps.append("git submodule update --init --recursive")
    r["setup_command"] = " && ".join(steps + setup_steps) if image.startswith(("ubuntu", "debian")) else \
        " && ".join(setup_steps)
    if setup_steps:
        # What the setup script printed for the build (PKG_CONFIG_PATH, LLVM_CONFIG...) is loaded first.
        r["build_command"] = "set -a; [ -f %s ] && . %s; set +a; %s" % (ENV_FILE, ENV_FILE, r["build_command"])

    # 4) What comes out, and what must never run inside (anything that sends to a device).
    for k in ("RELEASE_APP", "OUTPUT", "OUT_DIR", "APP_DIR", "PACKAGE_DIR", "TARGET_DIR", "OUTPUT_DIR"):
        v = mvars.get(k)
        if v and "/" in v and "$" not in v and "(" not in v:
            r["output"] = v.strip("/")
            break
    inst = _target(mk, "install") if mk else []
    if inst and SENDS.search(" ".join(inst) + " " + " ".join("%s=%s" % kv for kv in mvars.items() if "HOST" in kv[0])):
        r["blocked"].append({"pattern": r"\bmake\b[^;&|]*\binstall\b",
                             "why": "make install sends the build to a device. Copy the result out, then send it "
                                    "from the Mac."})
        cmds = [c for c in inst if not c.startswith(("@", "$(MAKE)", "make ")) and SENDS.search(c)]
        if cmds:
            vars_ = dict(mvars, PYTHON="python3", MAKE="make")
            for k in list(vars_):
                if k.endswith("_HOST") and not vars_[k]:
                    vars_[k] = "<device address>"
            hint = _expand(cmds[-1], vars_)
            r["upload_hint"] = "run on the Mac in the project folder: " + hint
            script = re.search(r"([\w./-]+\.(?:py|sh))", hint)
            if script:
                r["blocked"].append({"pattern": re.escape(Path(script.group(1)).name),
                                     "why": "%s sends files to a device. Run it on the Mac after copying the result "
                                            "out." % Path(script.group(1)).name})
    # 5) Size: big builds (Mesa, LLVM, emulators) need more memory and time.
    heavy = HEAVY.findall(mk + doc_text[:20000] + _read(top / "tools" / "deps.json"))
    if heavy:
        r.update(memory_gb=16, memory_max=20, disk_gb=80, timeout_min=360)
        notes.append("Big build (%s): 16 GB memory (up to 20), 80 GB disk, 6 hours per command."
                     % ", ".join(sorted({h.lower() for h in heavy})[:4]))
    if mac_memory_gb:
        cap = max(4, int(mac_memory_gb) - 12)          # leave room for macOS and a loaded AI model
        r["memory_max"] = min(r["memory_max"], cap)
        r["memory_gb"] = min(r["memory_gb"], r["memory_max"])
    notes.append("Runs as native ARM Linux (fast). If a step fails with 'Exec format error', a tool is x86-only: "
                 "switch the sandbox to arch amd64 (x86 through Rosetta, slower).")
    r["recipe_notes"] = notes
    r["recipe_from"] = read
    return r


# ---------------------------------------------------------------- keeping the setup complete

# A command a build says is missing -> the Ubuntu/Debian package that has it.
APT_FOR = {"gcc": "build-essential", "g++": "build-essential", "cc": "build-essential", "c++": "build-essential",
           "make": "make", "git": "git", "curl": "curl", "wget": "wget", "cmake": "cmake", "ninja": "ninja-build",
           "meson": "meson", "pkg-config": "pkgconf", "pkgconf": "pkgconf", "python": "python3", "python3": "python3",
           "pip": "python3-pip", "pip3": "python3-pip", "clang": "clang", "clang++": "clang", "lld": "lld",
           "ld.lld": "lld", "llvm-config": "llvm", "llvm-ar": "llvm", "unzip": "unzip", "zip": "zip", "xz": "xz-utils",
           "file": "file", "bison": "bison", "flex": "flex", "autoconf": "autoconf", "automake": "automake",
           "libtool": "libtool", "libtoolize": "libtool", "nasm": "nasm", "yasm": "yasm", "patch": "patch",
           "rsync": "rsync", "bc": "bc", "gettext": "gettext", "msgfmt": "gettext", "xxd": "xxd",
           "glslangValidator": "glslang-tools", "zstd": "zstd", "lz4": "lz4", "sudo": "sudo", "node": "nodejs",
           "npm": "npm", "cargo": "cargo", "rustc": "rustc", "go": "golang-go", "java": "default-jdk-headless",
           "perl": "perl", "m4": "m4", "gperf": "gperf", "help2man": "help2man", "texinfo": "texinfo",
           "makeinfo": "texinfo", "ccache": "ccache", "jq": "jq", "openssl": "openssl", "7z": "p7zip-full"}
NOT_FOUND = re.compile(r"command not found:\s+([A-Za-z0-9][A-Za-z0-9_.+-]{0,40})|"
                       r"(?:^|[\s:])(?![0-9]+:)([A-Za-z0-9][A-Za-z0-9_.+-]{0,40}):\s+(?:command\s+)?not found", re.M)
APT_INSTALL = re.compile(r"apt(?:-get)?\s+install\s+(?:-y\s+|--yes\s+|--no-install-recommends\s+|-q+\s+)*")


def missing_commands(output):
    """Programs a build said weren't there ('git: command not found', 'sh: 1: make: not found'), in order."""
    out = []
    for m in NOT_FOUND.finditer(output or ""):
        name = m.group(1) or m.group(2)
        if name in ("sh", "bash", "zsh", "line") or name.isdigit() or name in out:
            continue
        out.append(name)
    return out[:8]


def packages_for(commands):
    """(apt packages for the known ones, commands PatchBay doesn't know a package for)."""
    pk, unknown = [], []
    for c in commands:
        p = APT_FOR.get(c)
        if p:
            if p not in pk:
                pk.append(p)
        else:
            unknown.append(c)
    return pk, unknown


def apt_packages(cmd):
    """The packages an apt-get install in a setup command installs."""
    return _apt_lines(cmd or "")


def add_packages(setup, packages):
    """The setup command with these apt packages added (to its apt-get install, or as a new first step)."""
    have = set(apt_packages(setup))
    new = [p for p in packages if p not in have]
    if not new:
        return setup or ""
    m = APT_INSTALL.search(setup or "")
    if m:
        return setup[:m.end()] + " ".join(new) + " " + setup[m.end():]
    first = "export DEBIAN_FRONTEND=noninteractive && apt-get update && apt-get install -y --no-install-recommends " \
            + " ".join(new)
    return first + (" && " + setup if setup else "")


def merge_setup(preset, mine):
    """The AI's own setup command joined onto the project's preset, so nothing the preset installs is lost (a
    hand-written 'apt-get install make gcc' used to replace it and the build then had no git). Its apt packages go
    into the preset's install line; its other steps run after the preset."""
    preset, mine = (preset or "").strip(), (mine or "").strip()
    if not preset or preset == mine or preset in mine:
        return mine or preset
    if not mine:
        return preset
    merged = add_packages(preset, apt_packages(mine))
    extra = []
    for part in re.split(r"\s*&&\s*", mine):
        p = part.strip()
        if not p or re.match(r"^(sudo\s+)?(export\s+DEBIAN_FRONTEND=\S+|apt(-get)?\s+(update|install)\b)", p):
            continue
        if p not in merged:
            extra.append(p)
    return merged + ("".join(" && " + p for p in extra))
