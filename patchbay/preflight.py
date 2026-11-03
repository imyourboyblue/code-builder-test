"""
PatchBay: check every tool a job needs BEFORE installing or building anything.

The AI (or a project's build files) gives a list of tools. PatchBay checks which are already on the Mac, shows a
found/missing checklist, and turns everything missing into ONE install card, instead of one card per failure.
This file only reads and plans. Nothing is installed here: projects.install_packages runs the plan after the user
presses Confirm on the card.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import projects

# Where each missing tool comes from. "brew" = a Homebrew formula. "xcode" = Apple's Command Line Tools.
# "manual" = can't be installed from a card; the words say how.
TOOLS = {
    "cmake": {"bins": ["cmake"], "brew": "cmake"},
    "make": {"bins": ["make"], "xcode": True},
    "git": {"bins": ["git"], "xcode": True},
    "clang": {"bins": ["clang"], "xcode": True},
    "xcode-tools": {"bins": [], "xcode": True},
    "llvm": {"bins": ["/opt/homebrew/opt/llvm/bin/clang", "/usr/local/opt/llvm/bin/clang", "llvm-config"],
             "brew": "llvm"},
    "pkg-config": {"bins": ["pkg-config", "pkgconf"], "brew": "pkgconf"},
    "ninja": {"bins": ["ninja"], "brew": "ninja"},
    "meson": {"bins": ["meson"], "brew": "meson"},
    "autoconf": {"bins": ["autoconf"], "brew": "autoconf"},
    "automake": {"bins": ["automake"], "brew": "automake"},
    "libtool": {"bins": ["glibtool", "libtoolize", "glibtoolize"], "brew": "libtool"},
    "imagemagick": {"bins": ["magick"], "brew": "imagemagick"},
    "python3": {"bins": ["python3"], "brew": "python"},
    "nasm": {"bins": ["nasm"], "brew": "nasm"},
    "bison": {"bins": ["/opt/homebrew/opt/bison/bin/bison", "bison"], "brew": "bison"},
    "flex": {"bins": ["flex"], "brew": "flex"},
    "wget": {"bins": ["wget"], "brew": "wget"},
    "7zip": {"bins": ["7zz", "7z"], "brew": "sevenzip"},
    "lz4": {"bins": ["lz4"], "brew": "lz4"},
    "xz": {"bins": ["xz"], "brew": "xz"},
    "zstd": {"bins": ["zstd"], "brew": "zstd"},
    "rust": {"bins": ["cargo"], "brew": "rust"},
    "go": {"bins": ["go"], "brew": "go"},
    "node": {"bins": ["node", "npm"], "brew": "node"},
    "docker": {"bins": ["docker"], "manual": "Docker Desktop, from docker.com (an app you install yourself)."},
    "homebrew": {"bins": [], "manual": "Homebrew: a one-time setup in Terminal, the command is on https://brew.sh"},
}
ALIASES = {"cmake3": "cmake", "gmake": "make", "pkgconf": "pkg-config", "pkgconfig": "pkg-config",
           "magick": "imagemagick", "convert": "imagemagick", "python": "python3", "cargo": "rust",
           "rustc": "rust", "golang": "go", "npm": "node", "nodejs": "node", "7z": "7zip", "7zz": "7zip",
           "sevenzip": "7zip", "glibtool": "libtool", "xcode": "xcode-tools", "command-line-tools": "xcode-tools",
           "clt": "xcode-tools", "brew": "homebrew", "dkp": "devkitpro", "devkitpro-pacman": "devkitpro"}

# A devkitPro "group" (like switch-dev) isn't one package, so check a package that always comes with it.
DKP_GROUPS = {"switch-dev": ["libnx", "devkitA64"], "3ds-dev": ["libctru", "devkitARM"],
              "wiiu-dev": ["wut", "devkitPPC"], "wii-dev": ["libogc", "devkitPPC"],
              "gamecube-dev": ["libogc", "devkitPPC"], "nds-dev": ["libnds", "devkitARM"],
              "gba-dev": ["libgba", "devkitARM"]}
DKP_BASE = {"devkitA64", "devkitARM", "devkitPPC", "libnx", "libctru", "libogc", "wut", "libnds", "libgba",
            "general-tools", "switch-tools", "3ds-tools", "wiiu-tools", "devkit-env", "dkp-toolchain-vars"}
SOURCES = {"brew": "Homebrew (formulae.brew.sh)", "devkitpro": "devkitPro (github.com/devkitPro, devkitpro.org)",
           "xcode": "Apple (Command Line Tools for Xcode)"}


NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+.@-]{0,80}$")


class Row(dict):
    """One line of the checklist: name, found (True/False), where (path or version), via, package, how."""


def _which(bins):
    path = projects.build_env()["PATH"]
    for b in bins:
        if b.startswith("/"):
            if Path(b).exists():
                return b
        else:
            p = shutil.which(b, path=path)
            if p:
                return p
    return None


def _version(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL)
        line = ((out.stdout or out.stderr).strip().splitlines() or [""])[0]
        return line[:80]
    except (OSError, subprocess.SubprocessError):
        return ""


def xcode_tools_ok():
    """Apple's Command Line Tools (make, git, clang). On a Mac /usr/bin/make exists even without them (it only
    asks to install), so ask xcode-select instead of looking for the file."""
    if sys.platform != "darwin":
        return True
    try:
        return subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=10,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def brew_has_formula(name):
    """Whether Homebrew knows a formula with this exact name (checked on the Mac, nothing is downloaded)."""
    brew = projects.find_brew()
    if not brew or not projects.PACKAGE_NAME.match(name):
        return False
    try:
        return subprocess.run([brew, "info", "--formula", name], capture_output=True, timeout=60,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def is_dkp(name):
    return name in DKP_GROUPS or name in DKP_BASE or name.startswith(projects.DKP_PREFIXES)


def check_one(name):
    """Check one tool. Never installs anything."""
    raw = str(name or "").strip()
    via = None
    if ":" in raw:                      # brew:cmake or devkitpro:switch-zlib
        via, raw = [x.strip() for x in raw.split(":", 1)]
        via = {"homebrew": "brew", "dkp": "devkitpro"}.get(via.lower(), via.lower())
    low = raw.lower()
    key = ALIASES.get(low, low if low in TOOLS else raw)
    row = Row(name=raw, found=False, where="", via=None, package=None, how="")
    if not raw or not NAME.match(raw):
        row.update(how="doesn't look like a tool or package name")
        return row
    if key == "devkitpro":
        pac = projects.find_pacman()
        row.update(name="devkitPro", found=bool(pac), where=pac or "", via="devkitpro", package=None)
        if not pac:
            row["how"] = "installed with the first devkitPro package below"
        return row
    if via == "devkitpro" or (via is None and is_dkp(key)):
        pkg = projects.dkp_name(key) if key.startswith(projects.DKP_PREFIXES) and key not in DKP_GROUPS else key
        if pkg in DKP_GROUPS:
            have = [projects.dkp_installed(x) for x in DKP_GROUPS[pkg]]
            ok = all(have)
            ver = ", ".join(h for h in have if h)
        else:
            ver = projects.dkp_installed(pkg)
            ok = bool(ver)
        row.update(name=pkg, found=ok, where=ver or "", via="devkitpro", package=pkg)
        return row
    if key == "homebrew":
        brew = projects.find_brew()
        row.update(name="Homebrew", found=bool(brew), where=brew or "", how="" if brew else TOOLS["homebrew"]["manual"])
        return row
    if via == "brew":
        info = next((v for v in TOOLS.values() if v.get("brew") == key), None) or TOOLS.get(key) or {}
        bins = info.get("bins") or [key]
        found = _which(bins)
        row.update(found=bool(found), where=(found and ("%s %s" % (found, _version(found))).strip()) or "",
                   via="brew", package=info.get("brew") or key)
        return row
    info = TOOLS.get(key)
    if info is None:                    # not one PatchBay knows: look for it on the PATH, then in Homebrew
        found = _which([raw])
        row.update(found=bool(found), where=(found and ("%s %s" % (found, _version(found))).strip()) or "")
        if not found:
            if brew_has_formula(raw.lower()):
                row.update(via="brew", package=raw.lower())
            else:
                row["how"] = "PatchBay doesn't know where this comes from: research it (generic search), then " \
                             "check again with brew:<name> or devkitpro:<name>"
        return row
    row["name"] = key
    if info.get("xcode"):
        ok = xcode_tools_ok() and (not info["bins"] or bool(_which(info["bins"])))
        found = _which(info["bins"]) if info["bins"] else ""
        row.update(found=ok, where=(found and ("%s %s" % (found, _version(found))).strip()) or
                   ("Apple Command Line Tools" if ok else ""), via=None if ok else "xcode", package="xcode-tools")
        return row
    found = _which(info["bins"])
    row.update(found=bool(found), where=(found and ("%s %s" % (found, _version(found))).strip()) or "")
    if not found:
        if info.get("brew"):
            row.update(via="brew", package=info["brew"])
        else:
            row["how"] = info.get("manual", "")
    return row


def needed_for_project(folder):
    """The tools a project's own files ask for (build files and CI workflow). Only reads the project folder."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    want = []
    text = ""
    files = [folder / n for n in ("Makefile", "makefile", "CMakeLists.txt", "meson.build", "configure.ac",
                                  "autogen.sh", "Cargo.toml", "go.mod", "package.json", ".gitmodules")]
    files += sorted((folder / ".github" / "workflows").glob("*.y*ml"))[:10]
    for f in files:
        try:
            text += "\n" + f.read_text(errors="replace")[:300000]
        except OSError:
            pass
    has = lambda n: (folder / n).exists()
    console = projects.console_of(folder)
    if has("CMakeLists.txt"):
        want.append("cmake")
        if console:
            want.append("%s-cmake" % console)
    if has("Makefile") or has("makefile"):
        want.append("make")
    if console:
        want.append("%s-dev" % console)
    if has("meson.build"):
        want += ["meson", "ninja"]
    elif re.search(r"-G\s*\"?Ninja|\bninja\b", text):
        want.append("ninja")
    if has("configure.ac") or has("autogen.sh"):
        want += ["autoconf", "automake", "libtool"]
    if has(".gitmodules") or has(".git"):
        want.append("git")
    if re.search(r"\bpkg-config\b|pkg_check_modules|PkgConfig", text):
        want.append("pkg-config")
    if re.search(r"\bmagick\b", text):
        want.append("imagemagick")
    if has("Cargo.toml"):
        want.append("rust")
    if has("go.mod"):
        want.append("go")
    if has("package.json"):
        want.append("node")
    # What the project's own macOS CI installs with Homebrew (real formula names).
    for line in re.findall(r"\bbrew install\s+([^\n&|;]+)", text):
        want += ["brew:" + w for w in re.findall(r"(?<![-\w])([a-z0-9][a-z0-9_+.@-]*)", line.lower())
                 if not w.startswith("-")]
    try:
        want += projects.needed_packages(folder)[1]       # the devkitPro libraries it links
    except Exception:       # never let this check stop anything
        pass
    out, seen = [], set()
    for w in want:
        k = w.split(":", 1)[-1]
        if k not in seen:
            seen.add(k)
            out.append(w)
    return out


def check(tools):
    """Check a list of tools. Returns the checklist rows (found ones and missing ones)."""
    rows, seen = [], set()
    for t in tools or []:
        r = check_one(t)
        k = r["name"].lower()
        if k in seen:
            continue
        seen.add(k)
        rows.append(r)
    return rows


def install_list(rows):
    """The missing rows a card can install, as projects.install_plan "mixed" names (brew:x, devkitpro:x, xcode)."""
    out = []
    for r in rows:
        if r["found"] or not r.get("via"):
            continue
        if r["via"] == "xcode":
            out.append("xcode:xcode-tools")
        elif r["via"] == "brew":
            out.append("brew:" + r["package"])
        elif r["via"] == "devkitpro" and r.get("package"):
            out.append("devkitpro:" + r["package"])
    return list(dict.fromkeys(out))


def checklist_text(rows):
    """The found/missing checklist in plain words (Markdown list)."""
    lines = []
    for r in rows:
        if r["found"]:
            lines.append("- ✓ **%s**: found%s" % (r["name"], (" (%s)" % r["where"]) if r["where"] else ""))
        elif r.get("via"):
            lines.append("- ✗ **%s**: missing, will install from %s" % (r["name"], SOURCES.get(r["via"], r["via"])))
        else:
            lines.append("- ✗ **%s**: missing. %s" % (r["name"], r.get("how") or "Can't be installed from a card."))
    return "\n".join(lines)
