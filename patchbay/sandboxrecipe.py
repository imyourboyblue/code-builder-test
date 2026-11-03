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
                       r"(?:^|[\s:])(?![0-9]+:)([A-Za-z0-9][A-Za-z0-9_.+-]{0,40}):\s+(?:command\s+)?not found|"
                       # Setup scripts that check first: "missing required command: wget", "required command not
                       # found: unzip", "wget is required", "please install unzip".
                       r"(?:missing|required)\s+(?:required\s+)?(?:command|program|tool)s?(?:\s+not\s+found)?:?\s+"
                       r"['\"`]?([A-Za-z0-9][A-Za-z0-9_.+-]{0,40})|"
                       r"\b([A-Za-z0-9][A-Za-z0-9_.+-]{0,40})\s+is\s+(?:required|needed|not installed)\b|"
                       r"[Pp]lease install\s+['\"`]?([A-Za-z0-9][A-Za-z0-9_.+-]{0,40})", re.M)
ARE_REQUIRED = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.+ ,/-]{0,120}?)\s+(?:are|is)\s+(?:required|needed|missing)\b",
                          re.M)
NOT_COMMANDS = {"sh", "bash", "zsh", "line", "it", "this", "that", "which", "what", "python3-module", "module",
                "the", "a", "an", "one", "and", "or", "is", "command", "program", "tool"}
APT_INSTALL = re.compile(r"apt(?:-get)?\s+install\s+(?:-y\s+|--yes\s+|--no-install-recommends\s+|-q+\s+)*")


def missing_commands(output):
    """Programs a build said weren't there ('git: command not found', 'sh: 1: make: not found'), in order."""
    out = []
    for m in NOT_FOUND.finditer(output or ""):
        name = next(g for g in m.groups() if g)
        if name.lower() in NOT_COMMANDS or name.isdigit() or name in out:
            continue
        if (m.group(4) or m.group(5)) and not package_for(name):
            continue        # "zlib is required" is about a library, not a program: only known programs count here
        out.append(name)
    # "clang, an archive tool, and ranlib are required to build zlib": every known program named in the list.
    for m in ARE_REQUIRED.finditer(output or ""):
        for w in re.split(r"[\s,/]+", m.group(1)):
            w = w.strip("'\"`.()")
            if w and w not in out and w.lower() not in NOT_COMMANDS and package_for(w):
                out.append(w)
    return out[:8]


APT_FOR.update({"sha256sum": "coreutils", "sha512sum": "coreutils", "nproc": "coreutils", "tar": "tar",
                "gzip": "gzip", "bzip2": "bzip2", "ar": "binutils", "ranlib": "binutils", "objcopy": "binutils",
                "strip": "binutils", "ld": "binutils", "rev": "bsdextrautils", "spirv-val": "spirv-tools",
                "spirv-as": "spirv-tools", "ccache": "ccache", "python3-config": "python3-dev", "gpg": "gnupg",
                "llvm-ranlib": "llvm", "llvm-nm": "llvm", "llvm-objcopy": "llvm", "llvm-readobj": "llvm",
                "llvm-strip": "llvm", "ld.lld": "lld", "wasm-ld": "lld", "ninja-build": "ninja-build",
                "pkg-config": "pkgconf", "dos2unix": "dos2unix", "sqlite3": "sqlite3", "ssh": "openssh-client",
                "scp": "openssh-client", "svn": "subversion", "hg": "mercurial", "lz4": "lz4", "lzma": "xz-utils",
                "unxz": "xz-utils", "zip": "zip", "xmllint": "libxml2-utils", "doxygen": "doxygen",
                "convert": "imagemagick", "ffmpeg": "ffmpeg"})
# Versioned LLVM programs: clang-18 / clang++-18 -> clang-18, ld.lld-18 / lld-18 -> lld-18, llvm-ar-18 -> llvm-18.
VERSIONED = [(re.compile(r"^clang(?:\+\+)?-(\d{2})$"), "clang-%s"), (re.compile(r"^(?:ld\.)?lld-(\d{2})$"), "lld-%s"),
             (re.compile(r"^llvm-[a-z-]+-(\d{2})$"), "llvm-%s"), (re.compile(r"^llvm-config-(\d{2})$"), "llvm-%s-dev"),
             (re.compile(r"^gcc-(\d{1,2})$"), "gcc-%s"), (re.compile(r"^g\+\+-(\d{1,2})$"), "g++-%s"),
             (re.compile(r"^python3\.(\d{1,2})$"), "python3.%s")]


def package_for(command):
    """The Ubuntu package that has this program, or None when PatchBay doesn't know."""
    if command in APT_FOR:
        return APT_FOR[command]
    for rx, pkg in VERSIONED:
        m = rx.match(command or "")
        if m:
            return pkg % m.group(1)
    return None


def packages_for(commands):
    """(apt packages for the known ones, commands PatchBay doesn't know a package for)."""
    pk, unknown = [], []
    for c in commands:
        p = package_for(c)
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


# ---------------------------------------------------------------- every program a build needs, found up front

# The setup scripts of well-known dependencies a project fetches DURING its build (so their files can't be read
# beforehand): what they check for. The PS5 Native App Boilerplate's tools/setup-native-dependencies.sh stops at the
# first of these it doesn't find, one at a time.
KNOWN_NEEDS = {
    "ps5-native-app-boilerplate": ["wget", "unzip", "sha256sum", "tar", "make", "clang-18", "llvm-ar-18",
                                   "llvm-ranlib-18", "git", "python3", "curl", "xz"],
    "ps5-payload-sdk": ["rev"],         # its prospero-* wrappers use rev (minimal Ubuntu images lack it)
}
CMD_WORD = r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,40}"
CHECK_RE = re.compile(r"\b(?:command\s+-v|which|type\s+-[pP]|hash)\s+['\"]?(%s)['\"]?" % CMD_WORD)
PY_WHICH = re.compile(r"shutil\.which\(\s*['\"](%s)['\"]" % CMD_WORD)
# for command in wget unzip tar; do ... (the loop variable says it's a list of programs)
FOR_LIST = re.compile(r"\bfor\s+(?:command|cmd|tool|prog|program|bin|binary|exe|dep|req|c|t)\s+in\s+([^;\n]+?)\s*;?\s*"
                      r"(?:do\b|\n)")
SCRIPT_RE = re.compile(r"(?:^|[\s'\"(\[,])((?:\.{0,2}/)?(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:sh|bash))\b")
INTO_RE = re.compile(r"\binto\s+(/[A-Za-z0-9_./-]+)")
SCAN_DIRS = ("tools", "scripts", "ci", "tools/ci", ".ci", "build-scripts", "utils", "bin")
SCAN_MAX = 80


def commands_in_text(text):
    """Programs a script or Makefile checks for or loops over, in order (only one of 'a || b' alternatives)."""
    found = []

    def add(w):
        w = w.strip("'\"")
        if w and not w.startswith(("$", "-")) and w.lower() not in NOT_COMMANDS and w not in found:
            found.append(w)
    for line in (text or "").splitlines():
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#!") else ""
        if not code.strip():
            continue
        hits = list(CHECK_RE.finditer(code)) + list(PY_WHICH.finditer(code))
        hits.sort(key=lambda m: m.start())
        if hits:
            add(hits[0].group(1))
            for prev, m in zip(hits, hits[1:]):
                if "||" not in code[prev.end():m.start()]:     # alternatives: the first one is enough
                    add(m.group(1))
    for m in FOR_LIST.finditer(text or ""):
        for w in m.group(1).split():
            if re.match(r"^%s$" % CMD_WORD, w):
                add(w)
    for name, needs in KNOWN_NEEDS.items():
        if name in (text or ""):
            for w in needs:
                add(w)
    return found


def _scan_files(top):
    files = []
    mk = next((p for p in (top / "Makefile", top / "makefile", top / "GNUmakefile") if p.is_file()), None)
    if mk:
        files.append(mk)
    for d in SCAN_DIRS:
        dd = top / d
        if dd.is_dir():
            for pat in ("*.sh", "*.bash", "*.py", "*.json"):
                files += sorted(dd.glob(pat))
    files += _workflows(top)
    files += [p for p in sorted(top.glob("*.sh"))]
    seen, out = set(), []
    for p in files:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:SCAN_MAX]


def needed_commands(source):
    """Every program the project's build files (Makefile, tools/ and scripts/ files, CI workflows, the dependency
    list) check for, so the setup can install them ALL at once before the first build, instead of the build
    stopping at one missing program after another. Returns (commands, files read)."""
    top = Path(os.path.expanduser(str(source))).resolve()
    if not top.is_dir():
        return [], []
    found, read = [], []
    for p in _scan_files(top):
        t = _read(p)
        if not t:
            continue
        cmds = commands_in_text(t)
        if cmds:
            read.append(_rel(top, p))
        for c in cmds:
            if c not in found:
                found.append(c)
    return found, read


def setup_packages(source):
    """(apt packages the project's build needs, programs PatchBay knows no package for, files read)."""
    cmds, read = needed_commands(source)
    pk, unknown = packages_for(cmds)
    return pk, unknown, read


def scripts_named(output):
    """Setup scripts a failed build ran, and the folders it fetched things into ('into /x'), from its output. Only
    plain path characters, so they are safe to look at inside the sandbox."""
    scripts, dirs = [], []
    for m in SCRIPT_RE.finditer(output or ""):
        s = m.group(1)
        if ".." not in s and s not in scripts:
            scripts.append(s)
    for m in INTO_RE.finditer(output or ""):
        d = m.group(1).rstrip("/.")
        if d and ".." not in d and d not in dirs:
            dirs.append(d)
    return scripts[:8], dirs[:4]


def show_scripts_command(scripts, dirs):
    """A read-only shell command that prints those scripts from inside the sandbox (from /work and the fetch
    folders), so PatchBay can read every program they need."""
    import shlex
    places = ["/work"] + dirs
    parts = []
    for s in scripts:
        for d in places:
            p = s if s.startswith("/") else "%s/%s" % (d, s.lstrip("./"))
            q = shlex.quote(p)
            parts.append("[ -f %s ] && head -c 200000 %s" % (q, q))
            if s.startswith("/"):
                break
    return "; ".join(parts) + "; true" if parts else ""


TOLERANT = "PatchBay: no package"


def add_found_packages(setup, packages):
    """The setup command with packages found by reading the build files added as their own install step, right after
    the main apt-get install. If one of them doesn't exist on this Linux, the others still install (each is then
    tried alone) and the setup goes on: a guess from reading files must never break a working setup."""
    have = set(apt_packages(setup))
    new = []
    for p in packages:
        if p not in have and p not in new:
            new.append(p)
    if not new:
        return setup or ""
    names = " ".join(new)
    step = ("{ apt-get install -y --no-install-recommends %s || for p in %s; do apt-get install -y "
            "--no-install-recommends \"$p\" || echo \"%s $p\"; done; }" % (names, names, TOLERANT))
    parts = re.split(r"\s+&&\s+", (setup or "").strip()) if (setup or "").strip() else []
    at = next((i for i, x in enumerate(parts) if APT_INSTALL.search(x)), None)
    if at is None:
        parts = ["export DEBIAN_FRONTEND=noninteractive", "apt-get update", step] + parts
    else:
        parts.insert(at + 1, step)
    return " && ".join(parts)


# ---------------------------------------------------------------- x86-only code (needs an x86 sandbox)
# Some projects build and run small programs for the BUILD machine itself as part of the build (tests, checks, code
# generators), written only for x86 Linux. On Apple silicon the sandbox is ARM Linux, so those fail ("this header is for
# x86 only", gregs[REG_RIP]) after a long build. A cross-compile (clang --target x86_64-...) is fine on ARM: only code
# built for the sandbox's own processor counts.
X86_LINUX_ONLY = re.compile(r"\bgregs\s*\[\s*REG_(RIP|RSP|RAX|RBP)\b|\bREG_RIP\b")
X86_FLAG = re.compile(r"-DARCHITECTURE_x86_64\b|-march=(x86-64(-v\d)?|haswell|skylake\w*|znver\d|core2|nehalem)\b|"
                      r"(?<![\w-])-m(sse[\d.]*|ssse3|avx\d*|avx512\w*|popcnt|bmi2?|pclmul|aes|fma|f16c)\b|"
                      r"(?<![\w-])-m32\b")
HOST_CC = re.compile(r"(?<![\w/.-])(clang\+\+|clang|g\+\+|gcc|cc|c\+\+)(-\d+)?(?![\w.-])")
CROSS_MARK = re.compile(r"--?target\b|x86_64-[a-z0-9]+-[a-z0-9]+|-isysroot\b|--sysroot\b|_COMPILER_TARGET|"
                        r"\$\{?(TARGET|CROSS|SYSROOT)", re.I)
AMD64_PLATFORM = re.compile(r"--platform[= ](linux/)?(amd64|x86_64)\b|runs-on:\s*\[?\s*ubuntu-\S*x64", re.I)
SKIP_WALK = {".git", ".deps", "build", "out", "dist", "node_modules", "third_party", "thirdparty", "external", "vendor",
             "externals", "subprojects", ".cache", "__pycache__", "target"}
SOURCE_ENDS = (".h", ".hpp", ".hh", ".c", ".cc", ".cpp", ".cxx", ".inl")
SOURCE_MAX = 3000
DIR_MAX = 4000


def _source_files(top):
    """Up to SOURCE_MAX of the project's own C/C++ files (not fetched or vendored code), stopping early on huge
    folders."""
    found, dirs = [], 0
    for root, names, files in os.walk(top):
        names[:] = sorted(n for n in names if n not in SKIP_WALK and not n.startswith("."))
        dirs += 1
        if dirs > DIR_MAX:
            break
        for f in sorted(files):
            if f.endswith(SOURCE_ENDS):
                found.append(Path(root) / f)
                if len(found) >= SOURCE_MAX:
                    return found
    return found


def x86_signs(source):
    """Signs that the project's build needs an x86-64 (Intel/AMD) Linux machine, read from its files in a second or
    two: build scripts that compile x86-only code for the build machine itself (an x86 flag with no cross target),
    source that uses x86 Linux's own registers (gregs[REG_RIP]), or Docker/CI files that ask for linux/amd64.
    Returns up to 6 plain lines like "tools/check.py:98: compiles with -DARCHITECTURE_x86_64 for the build machine"."""
    top = Path(os.path.expanduser(str(source))).resolve()
    if not top.is_dir():
        return []
    signs = []
    files = _scan_files(top) + sorted(top.glob("*.py")) + sorted(top.glob("tools/*.mk")) + \
        [p for p in (top / "Dockerfile", top / ".devcontainer" / "Dockerfile", top / "docker" / "Dockerfile")
         if p.is_file()]
    seen = set()
    for p in files:
        if p in seen:
            continue
        seen.add(p)
        lines = _read(p).splitlines()
        for i, line in enumerate(lines):
            if AMD64_PLATFORM.search(line):
                signs.append("%s:%d: asks for an x86 (amd64) Linux machine" % (_rel(top, p), i + 1))
                break
            m = X86_FLAG.search(line)
            if not m:
                continue
            near = "\n".join(lines[max(0, i - 3):i + 4])
            if HOST_CC.search(near) and not CROSS_MARK.search(near):
                signs.append("%s:%d: compiles with %s for the build machine itself (x86 only)"
                             % (_rel(top, p), i + 1, m.group(0)))
                break
        if len(signs) >= 3:
            break
    for p in _source_files(top):
        t = _read(p)
        m = X86_LINUX_ONLY.search(t) if t else None
        if m:
            n = t.count("\n", 0, m.start()) + 1
            signs.append("%s:%d: reads x86 processor registers (REG_%s), which only exist on x86 Linux"
                         % (_rel(top, p), n, m.group(1) or "RIP"))
            break
    return signs[:6]
