"""
One-click start for PatchBay on a Mac: a double-clickable PatchBay app, and (optionally) starting on its own when
the Mac turns on. Everything here only ever touches the user's own folders (~/Applications and ~/Library/LaunchAgents)
and never needs a password. Only uses Python's built-in parts.

The app is a tiny launcher that runs this same PatchBay (same Python, same folder). "Start at login" installs a
LaunchAgent that starts PatchBay quietly in the background when the user logs in.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.patchbay.patchbay"
HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "patchbay.py"
APP_DIR = Path.home() / "Applications" / "Patchbay.app"
AGENT = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
LOG = HERE / "data-cache" / "patchbay.log"


def is_mac():
    return sys.platform == "darwin"


def python_exe():
    return sys.executable or "/usr/bin/python3"


def _port():
    try:
        import json
        return int(json.loads((HERE / "config.json").read_text()).get("port") or 8765)
    except (OSError, ValueError):
        return 8765


def _launcher_script():
    # If PatchBay is already running (for example started at login), just open its page. Otherwise start it from this
    # exact folder and Python in the background, wait until its page answers, then open it. If anything goes wrong it
    # shows a message box (an app started from the Dock has no Terminal window to show errors in).
    url = "http://127.0.0.1:%d/" % _port()
    return """#!/bin/bash
URL=%(url)s
DIR=%(dir)s
PY=%(py)s
alert() {
  /usr/bin/osascript -e 'on run argv' -e 'display alert "PatchBay didn'"'"'t start" message (item 1 of argv) as critical' -e 'end run' "$1" >/dev/null 2>&1
}
up() { /usr/bin/curl -s -o /dev/null --max-time 2 "$URL"; }
if up; then /usr/bin/open "$URL"; exit 0; fi
if ! cd "$DIR" 2>/dev/null || ! ls patchbay.py >/dev/null 2>&1; then
  alert "It can't open its folder: $DIR

If you moved or renamed the PatchBay folder, start it once from Terminal with: python3 patchbay.py --install-app

If the folder is still there, macOS may be blocking it: open System Settings > Privacy & Security > Files and Folders (or Full Disk Access), and allow PatchBay."
  exit 1
fi
if [ ! -x "$PY" ]; then
  alert "The Python it was set up with is gone: $PY

Start it once from Terminal with: python3 patchbay.py --install-app"
  exit 1
fi
mkdir -p data-cache
echo "--- started from the app $(date) ---" >> data-cache/patchbay.log
"$PY" patchbay.py --no-browser "$@" >> data-cache/patchbay.log 2>&1 &
PID=$!
for i in $(seq 1 60); do
  if up; then /usr/bin/open "$URL"; exit 0; fi
  if ! kill -0 $PID 2>/dev/null; then
    alert "Here is the end of its log (data-cache/patchbay.log):

$(tail -n 12 data-cache/patchbay.log)"
    exit 1
  fi
  sleep 0.5
done
alert "It is still starting after 30 seconds. Try the icon again in a moment. Log: $DIR/data-cache/patchbay.log"
""" % {"url": _q(url), "dir": _q(HERE), "py": _q(python_exe())}


def _q(p):
    return "'" + str(p).replace("'", "'\\''") + "'"


SWIFT_SRC = HERE / "macapp" / "main.swift"
BUILD_DIR = HERE / "data-cache" / "macapp-build"
LAST = {"native": None, "note": ""}


def _stamp():
    """What the app was made for; if any of it changes, the app is made again."""
    import hashlib
    src = SWIFT_SRC.read_bytes() if SWIFT_SRC.exists() else b""
    return {"python": python_exe(), "folder": str(HERE), "port": _port(), "launcher": hashlib.sha1(
        _launcher_script().encode()).hexdigest()[:12], "swift": hashlib.sha1(src).hexdigest()[:12],
        "icon": ICON_VERSION}


def _swift_string(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _build_native(target):
    """Compile the small native window app (needs Apple's command line tools). Returns (ok, note)."""
    if not SWIFT_SRC.exists():
        return False, "The app's source file is missing."
    try:
        found = subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True, text=True, timeout=60,
                               stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False, "NO_TOOLS"
    if found.returncode:
        return False, "NO_TOOLS"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (BUILD_DIR / "build-error.txt").unlink()
    except OSError:
        pass
    src = SWIFT_SRC.read_text()
    src = src.replace("__PYTHON__", _swift_string(python_exe())).replace("__FOLDER__", _swift_string(HERE)) \
             .replace("__PORT__", str(_port()))
    (BUILD_DIR / "main.swift").write_text(src)
    out = BUILD_DIR / "PatchBay"
    try:
        r = subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-o", str(out), str(BUILD_DIR / "main.swift"),
                            "-framework", "Cocoa", "-framework", "WebKit"], capture_output=True, text=True,
                           timeout=600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "Building the app failed: %s" % e
    if r.returncode or not out.exists():
        (BUILD_DIR / "build-error.txt").write_text((r.stderr or "") + (r.stdout or ""))
        return False, "Building the app failed (details in data-cache/macapp-build/build-error.txt)."
    tmp = target.with_name("Patchbay.tmp")
    import shutil
    shutil.copy2(str(out), str(tmp))
    tmp.chmod(0o755)
    tmp.replace(target)
    return True, "Built the PatchBay app with its own window."


_BUILD_LOCK = __import__("threading").Lock()


def make_app(native=True):
    """See _make_app. Only one build at a time (the start-up refresh and the Setup button could overlap)."""
    with _BUILD_LOCK:
        return _make_app(native)


def _make_app(native=True):
    """Create (or refresh) ~/Applications/Patchbay.app. Returns its path.
    It's a real Mac app with its own window when Apple's command line tools are there (they come with git and
    devkitPro setups); otherwise a small launcher that opens PatchBay in the browser."""
    if not is_mac():
        raise OSError("The PatchBay app is for macOS. On this computer, start it with: python3 patchbay.py")
    macos = APP_DIR / "Contents" / "MacOS"
    res = APP_DIR / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    runner = macos / "PatchBay"
    ok, note = _build_native(runner) if native else (False, "")
    if not ok:
        tmp = macos / "Patchbay.tmp"   # write a new file then swap, so a launcher that is running right now isn't cut off
        tmp.write_text(_launcher_script())
        tmp.chmod(0o755)
        tmp.replace(runner)
        note = (note + " " if note else "") + "Using the simple launcher (opens PatchBay in your browser)."
    icon = make_icon(res)
    info = {
        "CFBundleName": "PatchBay",
        "CFBundleDisplayName": "PatchBay",
        "CFBundleIdentifier": LABEL,
        "CFBundleExecutable": "PatchBay",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSLocalNetworkUsageDescription": "PatchBay finds and talks to the devices on your home network.",
    }
    if icon:
        info["CFBundleIconFile"] = "PatchBay"
    with open(APP_DIR / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    (res / "patchbay-build.json").write_text(__import__("json").dumps(dict(_stamp(), native=ok, note=note)))
    for cmd in (["/usr/bin/codesign", "--force", "--deep", "-s", "-", str(APP_DIR)], ["/usr/bin/touch", str(APP_DIR)]):
        try:   # an ad-hoc signature so Apple silicon Macs run it; touch so Finder notices the change
            subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    LAST.update(native=ok, note=note)
    return APP_DIR


def refresh_app():
    """Keep an existing PatchBay app up to date with this folder, Python, port and app code (called at every start)."""
    if is_mac() and AGENT.exists():     # "start at login" points at this folder too (it may have moved)
        try:
            with open(AGENT, "rb") as f:
                if plistlib.load(f) != _agent_plist():
                    with open(AGENT, "wb") as g:
                        plistlib.dump(_agent_plist(), g)
        except (OSError, ValueError):
            pass
    if not is_mac() or not APP_DIR.exists():
        return False
    import json
    try:
        built = json.loads((APP_DIR / "Contents" / "Resources" / "patchbay-build.json").read_text())
    except (OSError, ValueError):
        built = {}
    want = _stamp()
    if all(built.get(k) == v for k, v in want.items()):
        return False
    try:
        # Rebuild in the background-friendly way: same kind as before (native if it was, or if it never was tried).
        make_app(native=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- the app icon

ICON_VERSION = 3
ICON_PNG = HERE / "macapp" / "icon.png"   # 1024x1024; drawn in macapp/icon.svg.html


def make_icon(res_dir):
    """Write Patchbay.icns into the app's Resources (using the Mac's own sips and iconutil). True if it worked."""
    import shutil
    target = res_dir / "Patchbay.icns"
    cache = HERE / "data-cache" / ("Patchbay-icon-%d.icns" % ICON_VERSION)
    if cache.exists():
        shutil.copy2(str(cache), str(target))
        return True
    work = BUILD_DIR / "Patchbay.iconset"
    try:
        work.mkdir(parents=True, exist_ok=True)
        if not ICON_PNG.exists():
            return False
        big = ICON_PNG
        for px in (16, 32, 64, 128, 256, 512, 1024):
            for scale, name in ((1, "icon_%dx%d.png" % (px, px)), (2, "icon_%dx%d@2x.png" % (px // 2, px // 2))):
                if (scale == 2 and px < 32) or (scale == 1 and px > 512):
                    continue
                subprocess.run(["/usr/bin/sips", "-z", str(px), str(px), str(big), "--out", str(work / name)],
                               capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        r = subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(work), "-o", str(cache)], capture_output=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and cache.exists():
            shutil.copy2(str(cache), str(target))
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def install_tools():
    """Open Apple's own installer for the free command line tools (it asks the user before installing anything)."""
    if not is_mac():
        raise OSError("This is a macOS feature.")
    subprocess.Popen(["/usr/bin/xcode-select", "--install"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)


def remove_app():
    if APP_DIR.exists():
        import shutil
        shutil.rmtree(str(APP_DIR), ignore_errors=True)


def _agent_plist():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        # --no-browser: at login it just runs quietly; the user opens the page when they want it.
        "ProgramArguments": [python_exe(), str(SCRIPT), "--no-browser"],
        "WorkingDirectory": str(HERE),
        "RunAtLoad": True,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "ProcessType": "Background",
    }


def set_login(on):
    """Turn "start PatchBay at login" on or off. Returns True if it's on afterwards."""
    if not is_mac():
        raise OSError("Starting at login is a macOS feature.")
    if on:
        AGENT.parent.mkdir(parents=True, exist_ok=True)
        with open(AGENT, "wb") as f:
            plistlib.dump(_agent_plist(), f)
        _launchctl("load", AGENT)
        return True
    if AGENT.exists():
        _launchctl("unload", AGENT)
        AGENT.unlink()
    return False


def _launchctl(action, plist):
    try:
        subprocess.run(["/bin/launchctl", action, "-w", str(plist)], capture_output=True, text=True, timeout=20,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def status():
    native, note = None, ""
    try:
        import json
        built = json.loads((APP_DIR / "Contents" / "Resources" / "patchbay-build.json").read_text())
        native, note = built.get("native"), built.get("note") or ""
    except (OSError, ValueError):
        pass
    error = ""
    try:
        error = (BUILD_DIR / "build-error.txt").read_text("utf-8", "replace")
        error = error.replace(str(Path.home()), "~")[-6000:]
    except OSError:
        pass
    no_tools = "NO_TOOLS" in (LAST.get("note") or note)
    note = (LAST.get("note") or note).replace("NO_TOOLS", "Apple's command line tools aren't installed.")
    return {"mac": is_mac(), "app": APP_DIR.exists(), "app_path": str(APP_DIR), "native": native,
            "note": note, "build_error": "" if native else error, "no_tools": no_tools and not native,
            "building": _BUILD_LOCK.locked(),
            "login": AGENT.exists(), "script": str(SCRIPT)}
