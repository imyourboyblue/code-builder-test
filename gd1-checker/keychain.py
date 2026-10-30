"""
Passwords and keys in the Mac's Keychain instead of in Patchbay's settings files.

A settings file then only holds a pointer like "@keychain:device-3f9a1c2b". The secret itself is in the login
keychain (Keychain Access app, items named "Patchbay: ..."). Patchbay reads it back when it loads the settings.

It talks to the Keychain through Apple's own Security framework (so a secret never appears in a command line),
and falls back to the `security` tool for reading. If the Keychain can't be used (not a Mac, or it's locked and
the user said no), the secret simply stays in the settings file as before, so nothing stops working.

The first time a new Python reads an item, macOS may ask "python wants to use your keychain". Choose Always Allow.
"""

import ctypes
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

SERVICE = "Patchbay"
PREFIX = "@keychain:"
NOT_SECRET = ("", "anonymous@", None)
PROBLEMS = []      # recent read failures, for 🩺 Setup
_CACHE = {}        # account -> secret, so settings that are loaded often don't ask the Keychain every time

_sec = None


def available():
    return sys.platform == "darwin" and os.path.exists("/usr/bin/security")


def _lib():
    global _sec
    if _sec is None:
        try:
            lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            u32, vp, cp = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char_p
            lib.SecKeychainAddGenericPassword.argtypes = [vp, u32, cp, u32, cp, u32, cp, ctypes.POINTER(vp)]
            lib.SecKeychainFindGenericPassword.argtypes = [vp, u32, cp, u32, cp, ctypes.POINTER(u32),
                                                           ctypes.POINTER(vp), ctypes.POINTER(vp)]
            lib.SecKeychainItemModifyAttributesAndData.argtypes = [vp, vp, u32, cp]
            lib.SecKeychainItemFreeContent.argtypes = [vp, vp]
            lib.SecKeychainItemDelete.argtypes = [vp]
            for f in ("SecKeychainAddGenericPassword", "SecKeychainFindGenericPassword",
                      "SecKeychainItemModifyAttributesAndData", "SecKeychainItemFreeContent", "SecKeychainItemDelete"):
                getattr(lib, f).restype = ctypes.c_int32
            cf.CFRelease.argtypes = [vp]
            lib._cf = cf
            _sec = lib
        except (OSError, AttributeError):
            _sec = False
    return _sec


def _find_item(account):
    """(status, item ref, secret bytes)."""
    lib = _lib()
    if not lib:
        return -1, None, None
    s, a = SERVICE.encode(), account.encode()
    n, data, item = ctypes.c_uint32(0), ctypes.c_void_p(), ctypes.c_void_p()
    st = lib.SecKeychainFindGenericPassword(None, len(s), s, len(a), a, ctypes.byref(n), ctypes.byref(data),
                                            ctypes.byref(item))
    if st != 0:
        return st, None, None
    secret = ctypes.string_at(data, n.value)
    lib.SecKeychainItemFreeContent(None, data)
    return 0, item, secret


def put(account, secret):
    """Save (or replace) one secret. True if it's in the Keychain now and reads back the same."""
    if not available():
        return False
    lib = _lib()
    if not lib:
        return False
    s, a, w = SERVICE.encode(), account.encode(), secret.encode()
    st, item, _ = _find_item(account)
    if st == 0:
        st = lib.SecKeychainItemModifyAttributesAndData(item, None, len(w), w)
        lib._cf.CFRelease(item)
    else:
        st = lib.SecKeychainAddGenericPassword(None, len(s), s, len(a), a, len(w), w, None)
    _CACHE.pop(account, None)
    return st == 0 and get(account) == secret


def get(account):
    """The secret, or None if it isn't there (or the Keychain said no)."""
    if not available():
        return None
    if account in _CACHE:
        return _CACHE[account]
    st, item, secret = _find_item(account)
    if st == 0:
        _lib()._cf.CFRelease(item)
        _CACHE[account] = secret.decode("utf-8", "replace")
        return _CACHE[account]
    try:   # fallback: Apple's command line tool (the secret comes back on its output, never in a command line)
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            _CACHE[account] = r.stdout.rstrip("\n")
            return _CACHE[account]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def remove(account):
    _CACHE.pop(account, None)
    st, item, _ = _find_item(account)
    if st == 0:
        _lib().SecKeychainItemDelete(item)
        _lib()._cf.CFRelease(item)
        return True
    return False


# ---------------------------------------------------------------- settings values

def is_pointer(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def protect(value, kind="device", account=None):
    """What to save in a settings file for this secret: a Keychain pointer, or the secret itself if the Keychain
    can't be used. Each device password gets its own new item, so Undo of a settings change still finds the old
    one; pass account to replace a fixed item instead (the Claude key)."""
    if value in NOT_SECRET or is_pointer(value) or not available():
        return value
    account = account or "%s-%s" % (kind, uuid.uuid4().hex[:10])
    return PREFIX + account if put(account, value) else value


def reveal(value):
    """The secret for a value from a settings file (a pointer is looked up in the Keychain)."""
    if not is_pointer(value):
        return value
    account = value[len(PREFIX):]
    secret = get(account)
    if secret is None:
        msg = "Couldn't read the saved password %s from the Mac's Keychain (it may be locked, or access was " \
              "refused). Open Keychain Access, find \"Patchbay\", and allow it, or type the password again." % account
        if msg not in PROBLEMS:
            PROBLEMS.append(msg)
        return ""
    return secret


# ---------------------------------------------------------------- moving old files over

def _protect_file(path, keys, fixed=None):
    """Move plain secrets under keys in one JSON file into the Keychain. Returns the keys moved."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    moved = []
    for k in keys:
        v = raw.get(k)
        if isinstance(v, str) and v not in NOT_SECRET and not is_pointer(v):
            new = protect(v, "claude" if k == "key" else "device", account=(fixed or {}).get(k))
            if is_pointer(new):
                raw[k] = new
                moved.append(k)
    if moved:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        try:
            os.chmod(str(tmp), 0o600)
        except OSError:
            pass
        tmp.replace(p)
    return moved


def migrate(here, data_dirs=()):
    """Move every plain password and key Patchbay saved into the Keychain. Returns a list of plain sentences."""
    if not available():
        return []
    here = Path(here)
    done = []
    for f in [here / "config.json"] + sorted((here / "devices").glob("*.json")):
        if _protect_file(f, ["password"]):
            try:
                name = json.loads(f.read_text()).get("device_name") or f.stem
            except (OSError, ValueError):
                name = f.stem
            done.append("the password for %s" % name)
    if _protect_file(here / "claude.json", ["key"], fixed={"key": "claude-api-key"}):
        done.append("your Claude API key")
    # Old copies kept for Undo of settings changes.
    old = 0
    for d in data_dirs:
        for f in Path(d).glob("settings-backups/*/*.json"):
            if _protect_file(f, ["password"]):
                old += 1
    if old:
        done.append("%d older copies kept for Undo" % old)
    return done


def status(here):
    """For 🩺 Setup: how many secrets are in the Keychain and how many are still plain."""
    here = Path(here)
    inside, plain = 0, []
    files = [here / "config.json"] + sorted((here / "devices").glob("*.json"))
    for f in files:
        try:
            raw = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        v = raw.get("password")
        if is_pointer(v):
            inside += 1
        elif v not in NOT_SECRET:
            plain.append(raw.get("device_name") or f.stem)
    try:
        k = json.loads((here / "claude.json").read_text()).get("key")
        if is_pointer(k):
            inside += 1
        elif k:
            plain.append("Claude API key")
    except (OSError, ValueError):
        pass
    return {"available": available(), "in_keychain": inside, "plain": plain, "problems": PROBLEMS[-5:]}
