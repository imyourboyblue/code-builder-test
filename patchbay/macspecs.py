"""
PatchBay: what this Mac is, so the AI stops suggesting things that don't fit it.

Checked on the Mac itself (nothing typed by hand): macOS version, chip, memory, free disk, Homebrew, the real names
and paths of the build tools (for example Homebrew's llvm@18 gives .../opt/llvm@18/bin/clang, not a clang-18
command), and whether a Linux container tool is there for sandboxes. The result is a short summary that goes at the
END of the AI's instructions. It stays on this Mac: ask_model takes it out before anything is sent to Claude.

It also looks at a project's files to tell when the project is made for another system (an Ubuntu-only Makefile, a
CI that only runs on Linux), so the AI can say so up front and suggest a Linux sandbox instead of changing the Mac.

Only reads. Cached, so it costs nothing on most questions.
"""

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

START = "[This Mac]"
END = "[/This Mac]"
BLOCK = re.compile(re.escape("\n\n" + START) + r".*?" + re.escape(END), re.S)

TOOL_TTL = 6 * 3600         # tool versions and the container tool: checked again after 6 hours (or after an install)
DISK_TTL = 600              # free disk: every 10 minutes, rounded so the summary rarely changes
_CACHE = {"at": 0.0, "facts": None, "disk_at": 0.0, "disk": None}
_LOCK = threading.Lock()

BREW_PREFIXES = ("/opt/homebrew", "/usr/local")
# Homebrew formulas that are "keg-only": installed but NOT on the PATH, so the AI must use the full path.
KEG_ONLY = ("llvm", "bison", "flex", "make", "binutils", "libtool", "openssl", "curl", "ncurses", "m4", "gnu-sed",
            "python")
VERSION_TOOLS = (("clang", "clang"), ("make", "make"), ("cmake", "cmake"), ("ninja", "ninja"), ("git", "git"),
                 ("python3", "python3"), ("pkg-config", "pkg-config"), ("meson", "meson"), ("cargo", "cargo"),
                 ("go", "go"), ("node", "node"))


def _run(argv, timeout=10):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           errors="replace")
        return r.returncode, (r.stdout or r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _path():
    try:
        import projects
        return projects.build_env()["PATH"]
    except Exception:
        return os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"


def _first_line(text):
    return (text.splitlines() or [""])[0][:90]


def _short_version(text):
    """'cmake version 3.31.6' -> '3.31.6', 'Apple clang version 17.0.0 (...)' -> 'Apple clang 17.0.0'."""
    line = _first_line(text)
    if line.lower().startswith("apple clang"):
        m = re.search(r"\d+(\.\d+)+", line)
        return "Apple clang %s" % (m.group(0) if m else "")
    if "GNU Make" in line:
        return "make (" + line.strip() + ")"
    m = re.search(r"\d+(\.\d+)+", line)
    return m.group(0) if m else line


def _sysctl(name):
    code, out = _run(["sysctl", "-n", name], 5)
    return out if code == 0 else ""


def system_facts():
    """macOS version, chip, cores, memory. Works (with less detail) when not on a Mac."""
    f = {"os": platform.system(), "os_version": platform.release(), "arch": platform.machine(), "chip": "",
         "cores": os.cpu_count() or 0, "memory_gb": 0, "rosetta": False}
    if sys.platform == "darwin":
        code, out = _run(["sw_vers", "-productVersion"], 5)
        f["os"], f["os_version"] = "macOS", out if code == 0 else platform.mac_ver()[0]
        f["chip"] = _sysctl("machdep.cpu.brand_string")
        mem = _sysctl("hw.memsize")
        if mem.isdigit():
            f["memory_gb"] = round(int(mem) / 1024 ** 3)
        if _sysctl("hw.optional.arm64") == "1":
            f["rosetta"] = f["arch"] != "arm64"      # PatchBay's Python itself is running as Intel code
            f["arch"] = "arm64"
    else:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    f["memory_gb"] = round(int(line.split()[1]) / 1024 ** 2)
        except (OSError, ValueError, IndexError):
            pass
    return f


def brew_prefix():
    for p in BREW_PREFIXES:
        if Path(p, "bin", "brew").exists():
            return p
    b = shutil.which("brew")
    return str(Path(b).resolve().parent.parent) if b else None


def keg_tools(prefix):
    """Homebrew tools that are installed but not on the PATH, with the real command path. For example
    {"llvm@18": "/opt/homebrew/opt/llvm@18/bin/clang"}."""
    out = {}
    if not prefix:
        return out
    opt = Path(prefix, "opt")
    try:
        names = sorted(p.name for p in opt.iterdir())
    except OSError:
        return out
    for name in names:
        base = name.split("@", 1)[0]
        if base not in KEG_ONLY or base == "python":
            continue
        binary = {"llvm": "clang", "make": "gmake", "gnu-sed": "gsed", "binutils": "objdump", "libtool": "glibtool",
                  "openssl": "openssl", "curl": "curl", "ncurses": "ncursesw6-config", "m4": "m4",
                  "bison": "bison", "flex": "flex"}.get(base, base)
        p = opt / name / "bin" / binary
        if p.exists():
            out[name] = str(p)
    return out


def versioned_gcc(prefix):
    """Homebrew's GCC installs gcc-15 (with the number), because plain gcc on a Mac is Apple clang."""
    if not prefix:
        return []
    try:
        return sorted({p.name for p in Path(prefix, "bin").glob("gcc-[0-9]*") if re.fullmatch(r"gcc-\d+", p.name)})
    except OSError:
        return []


def tool_versions():
    path = _path()
    out = {}
    for label, exe in VERSION_TOOLS:
        found = shutil.which(exe, path=path)
        if not found:
            continue
        flag = "version" if exe == "go" else "--version"
        out[label] = (found, _short_version(_run([found, flag], 10)[1]))
    return out


def container_facts():
    try:
        import sandbox
        return sandbox.engine_status()
    except Exception:
        return {"engine": None, "running": False}


def devkitpro():
    try:
        import projects
        return projects.find_pacman()
    except Exception:
        return None


def xcode_ok():
    if sys.platform != "darwin":
        return None
    return _run(["xcode-select", "-p"], 10)[0] == 0


def free_gb():
    try:
        return shutil.disk_usage(str(Path.home())).free / 1024 ** 3
    except OSError:
        return None


def _round_disk(gb):
    """Rounded so the summary (and Ollama's saved start of the prompt) doesn't change for small changes."""
    if gb is None:
        return None
    step = 5 if gb < 50 else 25 if gb < 500 else 100
    return int(gb // step * step)


def facts(refresh=False):
    """Everything above, from the cache when it's fresh."""
    now = time.time()
    with _LOCK:
        if refresh or not _CACHE["facts"] or now - _CACHE["at"] > TOOL_TTL:
            prefix = brew_prefix()
            _CACHE["facts"] = {"system": system_facts(), "brew": prefix, "keg": keg_tools(prefix),
                               "gcc": versioned_gcc(prefix), "tools": tool_versions(), "xcode": xcode_ok(),
                               "dkp": devkitpro(), "container": container_facts()}
            _CACHE["at"] = now
        if refresh or _CACHE["disk"] is None or now - _CACHE["disk_at"] > DISK_TTL:
            _CACHE["disk"], _CACHE["disk_at"] = _round_disk(free_gb()), now
        return dict(_CACHE["facts"], disk=_CACHE["disk"])


def forget():
    """Check again next time (after PatchBay installs something)."""
    with _LOCK:
        _CACHE["at"] = 0.0


def summary(f=None):
    """The short plain-text summary for the AI's instructions."""
    f = f or facts()
    s = f["system"]
    mac = s["os"] == "macOS"
    parts = [s["chip"], "arm64 (Apple Silicon)" if mac and s["arch"] == "arm64" else s["arch"]]
    first = "%s %s, %s%s" % (s["os"], s["os_version"], ", ".join(p for p in parts if p),
                             ", %d cores" % s["cores"] if s["cores"] else "")
    if s["memory_gb"]:
        first += ", %d GB memory" % s["memory_gb"]
    if f.get("disk") is not None:
        first += ", about %d GB free disk" % f["disk"]
    lines = ["- " + first + "."]
    if s.get("rosetta"):
        lines.append("- PatchBay's Python is running under Rosetta (Intel mode); builds may come out x86_64.")
    if mac:
        lines.append("- This is NOT Linux: no apt/apt-get, yum, dnf, snap or systemctl, and no /proc. Install "
                     "packages with Homebrew%s or devkitPro (dkp-pacman), never sudo apt. Apple's sed, tar, grep and "
                     "find are BSD versions (different flags from GNU); the system make is GNU Make 3.81. Shared "
                     "libraries are .dylib, not .so." % (" (%s)" % f["brew"] if f.get("brew") else ", which is not "
                                                          "installed yet"))
        if f.get("xcode") is False:
            lines.append("- Apple's Command Line Tools are NOT installed (no clang, make or git until they are).")
    t = f.get("tools") or {}
    if t:
        lines.append("- On the PATH: " + "; ".join(
            ver if label in ver.lower() else "%s %s" % (label, ver) if ver else label
            for label, (_, ver) in t.items()) + ".")
    if "clang" in t and mac and t["clang"][1].startswith("Apple"):
        lines.append("- clang and gcc are Apple's clang (/usr/bin). Numbered names like clang-18, gcc-13 or "
                     "llvm-config-18 do not exist here unless listed below.")
    keg = f.get("keg") or {}
    if keg:
        lines.append("- Installed by Homebrew but NOT on the PATH (use the full path): " + "; ".join(
            "%s: %s" % (k, v) for k, v in keg.items()) + ".")
    if f.get("gcc"):
        lines.append("- Real GCC from Homebrew: " + ", ".join(f["gcc"]) + " (with the number).")
    if f.get("dkp"):
        lines.append("- devkitPro is installed (/opt/devkitpro) for console homebrew.")
    c = f.get("container") or {}
    if c.get("engine"):
        lines.append("- Linux containers: %s (%s). Linux-only projects can be built in a sandbox (sandbox_create)."
                     % (c["engine"], "running" if c.get("running") else "installed, not running"))
    else:
        lines.append("- No Linux container tool yet (sandboxes need Apple's container tool or Docker).")
    return "\n".join(lines)


def prompt_block():
    """The block added at the END of the AI's instructions. Empty if anything goes wrong."""
    try:
        text = summary()
    except Exception as e:     # never let this stop a question
        print("Mac specs: %s" % e)
        return ""
    return ("\n\n%s\nThis computer, checked by PatchBay. Make every command, path and tool name fit it, and say so "
            "up front when something is made for another system:\n%s\n%s" % (START, text, END))


def strip(text):
    """The text without the Mac block (for anything that goes to the cloud)."""
    return BLOCK.sub("", text or "")


# --- Is a project made for another system? -----------------------------------------------------------------------

LINUX_SIGNS = [
    (r"\bapt(-get)?\s+(-\S+\s+)*install\b", "its files install packages with apt (Ubuntu/Debian)"),
    (r"\b(dnf|yum)\s+install\b", "its files install packages with dnf/yum (Fedora/Red Hat)"),
    (r"(?<![-\w])pacman\s+-S", "its files install packages with pacman (Arch Linux)"),
    (r"\bubuntu\b|\bdebian\b", "it mentions Ubuntu/Debian"),
    (r"x86_64-linux-gnu|aarch64-linux-gnu|/usr/lib64\b", "it uses Linux library folders"),
    (r"#\s*include\s*<(linux/|sys/epoll\.h|sys/inotify\.h|sys/sysinfo\.h|sys/prctl\.h)", "its code uses Linux-only headers"),
    (r"\bsystemd\b|\bsystemctl\b", "it uses systemd"),
    (r"(?<![\w-])clang-\d\d\b|(?<![\w-])gcc-\d\d?\b|llvm-config-\d\d\b", "it calls numbered compilers (clang-18, gcc-13) the way Ubuntu names them"),
]
MAC_SIGNS = re.compile(r"\bmacos\b|\bdarwin\b|\bbrew install\b|__APPLE__|\bosx\b|\.dylib\b|runs-on:\s*macos", re.I)
WINDOWS_SIGNS = re.compile(r"\.vcxproj\b|\.sln\b|\bmsbuild\b|\bvcpkg\b|<windows\.h>", re.I)
_TARGET_CACHE = {}


def _project_text(folder):
    folder = Path(folder)
    files = [folder / n for n in ("Makefile", "makefile", "CMakeLists.txt", "meson.build", "configure.ac",
                                  "build.sh", "install.sh", "Dockerfile", "README.md", "README", "BUILDING.md",
                                  "INSTALL.md")]
    files += sorted((folder / ".github" / "workflows").glob("*.y*ml"))[:10]
    src = []
    for d in ("src", "source", "include", "."):
        try:
            src += sorted(p for p in (folder / d).glob("*.c*") if p.is_file())[:30]
        except OSError:
            pass
    text, ci, stamp = "", "", []
    for f in files + src[:40]:
        try:
            body = f.read_text(errors="replace")[:200000]
            stamp.append((str(f), f.stat().st_mtime))
        except OSError:
            continue
        text += "\n" + body
        if ".github" in f.parts:
            ci += "\n" + body
    return text, ci, tuple(stamp)


def target_warning(folder):
    """One plain sentence when the project looks made for another system than this Mac, else ''."""
    if sys.platform != "darwin" and not os.environ.get("PATCHBAY_TEST_MAC"):
        return ""
    try:
        if not Path(folder).is_dir():
            return ""
        import projects
        if projects.console_of(folder):          # console homebrew cross-compiles with devkitPro: fine on a Mac
            return ""
        text, ci, stamp = _project_text(folder)
    except Exception:
        return ""
    key = str(folder)
    hit = _TARGET_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    reasons = []
    runs_on = re.findall(r"runs-on:\s*\[?\s*['\"]?([\w.-]+)", ci)
    if runs_on and all(r.lower().startswith("ubuntu") for r in runs_on):
        reasons.append("its CI only builds on Ubuntu")
    for pattern, why in LINUX_SIGNS:
        if re.search(pattern, text, re.I) and why not in reasons:
            reasons.append(why)
    out = ""
    if reasons and (not MAC_SIGNS.search(text) or "its CI only builds on Ubuntu" in reasons) \
            and (len(reasons) >= 2 or "Linux-only headers" in reasons[0] or "CI only" in reasons[0]):
        out = ("This project looks made for Linux, not macOS (%s). Tell the user that in one line before building, "
               "and suggest building it in a Linux sandbox (sandbox_create) rather than changing the Mac. If it "
               "should build on the Mac, translate its steps: Homebrew instead of apt, real tool names and paths "
               "from the Mac summary." % "; ".join(reasons[:3]))
    elif WINDOWS_SIGNS.search(text) and not MAC_SIGNS.search(text):
        out = ("This project looks made for Windows (Visual Studio files). Tell the user that in one line before "
               "trying to build it on the Mac.")
    _TARGET_CACHE[key] = (stamp, out)
    return out
