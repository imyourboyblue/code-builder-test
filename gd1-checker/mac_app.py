"""
One-click start for Patchbay on a Mac: a double-clickable Patchbay app, and (optionally) starting on its own when
the Mac turns on. Everything here only ever touches the user's own folders (~/Applications and ~/Library/LaunchAgents)
and never needs a password. Only uses Python's built-in parts.

The app is a tiny launcher that runs this same Patchbay (same Python, same folder). "Start at login" installs a
LaunchAgent that starts Patchbay quietly in the background when the user logs in.
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
    # If Patchbay is already running (for example started at login), just open its page. Otherwise start it from this
    # exact folder and Python; it opens the browser by itself.
    url = "http://127.0.0.1:%d/" % _port()
    return ("#!/bin/bash\n"
            "if /usr/bin/curl -s -o /dev/null --max-time 2 %s; then /usr/bin/open %s; exit 0; fi\n"
            "cd %s || exit 1\n"
            "mkdir -p data-cache\n"
            'exec %s patchbay.py "$@" >> data-cache/patchbay.log 2>&1\n'
            % (url, url, _q(HERE), _q(python_exe())))


def _q(p):
    return "'" + str(p).replace("'", "'\\''") + "'"


def make_app():
    """Create (or refresh) ~/Applications/Patchbay.app. Returns its path."""
    if not is_mac():
        raise OSError("The Patchbay app is for macOS. On this computer, start it with: python3 patchbay.py")
    macos = APP_DIR / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    runner = macos / "Patchbay"
    runner.write_text(_launcher_script())
    runner.chmod(0o755)
    info = {
        "CFBundleName": "Patchbay",
        "CFBundleDisplayName": "Patchbay",
        "CFBundleIdentifier": LABEL,
        "CFBundleExecutable": "Patchbay",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "11.0",
    }
    with open(APP_DIR / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)
    # Nudge Finder/Launch Services to notice the new app.
    try:
        subprocess.run(["/usr/bin/touch", str(APP_DIR)], timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    return APP_DIR


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
    """Turn "start Patchbay at login" on or off. Returns True if it's on afterwards."""
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
    return {"mac": is_mac(), "app": APP_DIR.exists(), "app_path": str(APP_DIR),
            "login": AGENT.exists(), "script": str(SCRIPT)}
