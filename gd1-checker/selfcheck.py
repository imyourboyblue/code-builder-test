"""
The self-check: is everything Patchbay needs working? Ollama, secure connections, the build tools, the internet,
and each device. Each check comes back with a short status the page shows, and some have a Fix button that tries to
put it right. Only reads, except a Fix the user clicks. Built-in parts only.

Checks and fixes are kept here so the home page's "Check setup" panel and the error report can share them.
"""

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import tlsfix


def _ollama(app):
    import gd1chat
    cfg = app.home.cfg()
    try:
        with urllib.request.urlopen(gd1chat.ollama_url(cfg) + "/api/tags", timeout=8) as r:
            models = [m.get("name") for m in json.loads(r.read()).get("models", [])]
    except Exception as e:
        return bad("ollama", "Ollama (the AI)", "Can't reach Ollama: %s" % tlsfix.explain(e),
                   "The AI runs in the Ollama app. Make sure it's installed (ollama.com) and open.", fix="Start Ollama")
    want = cfg.get("ollama_model")
    if want == "auto":
        import model_labels
        fast, strong = model_labels.picks(models)
        if not fast:
            return warn("ollama", "Ollama (the AI)", "Running, but no models are downloaded.",
                        "In Terminal run: ollama pull qwen3.6:35b-a3b")
        return ok("ollama", "Ollama (the AI)", "Running. Automatic uses %s for quick things and %s for hard jobs."
                  % (model_labels.local(fast)[0], model_labels.local(strong)[0]))
    if models and want and want not in models:
        return warn("ollama", "Ollama (the AI)", "Running, but the chosen model %s isn't downloaded. You have: %s"
                    % (want, ", ".join(models[:6]) or "none"),
                    "In Terminal run: ollama pull %s" % want)
    return ok("ollama", "Ollama (the AI)", "Running, %d model%s ready." % (len(models), "" if len(models) == 1 else "s"))


def _certs(app):
    src = {"python": "built in", "certifi": "the certifi package", "mac": "your Mac's certificates",
           "none": None}.get(tlsfix._source or "", tlsfix._source)
    if tlsfix._source == "none":
        return bad("certs", "Secure connections", "No trusted certificates were found, so https may fail.",
                   tlsfix.HELP, fix="Load the Mac's certificates")
    return ok("certs", "Secure connections", "Working (using %s)." % src)


def _internet(app):
    try:
        req = urllib.request.Request("https://api.github.com/", headers={"User-Agent": "patchbay"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(200)
        return ok("internet", "Internet & GitHub", "Reachable (used for updates and research).")
    except Exception as e:
        msg = tlsfix.explain(e)
        return warn("internet", "Internet & GitHub", "Couldn't reach the internet: %s" % str(msg)[:120],
                    "Web lookups and updates won't work, but local jobs still will.",
                    fix="Load the Mac's certificates" if "CERTIFICATE" in str(e) else None,
                    fix_id="certs" if "CERTIFICATE" in str(e) else None)


def _build_tools(app):
    import projects
    rep = projects.toolchain_report()
    if rep.startswith("devkitPro is NOT installed"):
        return warn("build_tools", "Build tools (devkitPro)", "Not installed yet.",
                    "Only needed to rebuild Nintendo homebrew. Patchbay can install it for you.",
                    fix="Install Switch build tools")
    sw = projects.switch_configure()
    if not sw:
        return warn("build_tools", "Build tools (devkitPro)", "Installed, but the Switch cmake files are missing.",
                    "Needed to build Switch apps.", fix="Install Switch build tools")
    return ok("build_tools", "Build tools (devkitPro)", "Ready for Switch builds.")


def _devices(app):
    try:
        rows = app.registry.summary()
    except Exception as e:
        return warn("devices", "Your devices", "Couldn't check: %s" % e, "")
    on = [r for r in rows if r.get("online")]
    if not rows:
        return warn("devices", "Your devices", "No devices set up yet.",
                    "Add one from the Devices screen, or ask Patchbay to add it.")
    if not on:
        return warn("devices", "Your devices", "None of your %d device(s) answered just now." % len(rows),
                    "Check they're switched on and on the same network.")
    return ok("devices", "Your devices", "%d of %d online: %s" % (len(on), len(rows),
              ", ".join(r["name"] for r in on[:8])))


def _disk(app):
    import gd1chat
    try:
        folder = gd1chat.core.data_dir(app.home.cfg())
        total, used, free = shutil.disk_usage(str(folder))
        gb = free / 1e9
        if gb < 2:
            return warn("disk", "Free space", "Only %.1f GB free where backups go." % gb,
                        "Backups and builds may fail. Free some space, or point backups elsewhere.")
        return ok("disk", "Free space", "%.0f GB free for backups and builds." % gb)
    except Exception as e:
        return warn("disk", "Free space", "Couldn't check: %s" % e, "")


def _secrets(app):
    import keychain
    k = keychain.status(Path(__file__).resolve().parent)
    name = "Saved passwords"
    if not k["available"]:
        return ok("keychain", name, "Kept in Patchbay's settings files (the Keychain is only on a Mac).")
    if k["problems"]:
        return warn("keychain", name, k["problems"][-1], "Restart Patchbay after allowing it.")
    if k["plain"]:
        return warn("keychain", name, "Still in a settings file: %s." % ", ".join(k["plain"]),
                    "Patchbay moves them into the Mac's Keychain by itself.", "Move them now", "keychain")
    if not k["in_keychain"]:
        return ok("keychain", name, "No passwords saved (your devices use anonymous logins).")
    return ok("keychain", name, "%d kept safely in the Mac's Keychain (Keychain Access, \"Patchbay\")."
              % k["in_keychain"])


def _scan_data(app):
    import gd1chat
    total, count, per = 0, 0, []
    for _, cfg in app.registry.all():
        try:
            b, n = gd1chat.core.scan_usage(gd1chat.core.data_dir(cfg))
        except Exception:
            continue
        total, count = total + b, count + n
        if n:
            per.append("%s %s" % (cfg["device_name"], gd1chat.core.human_size(b)))
    if not count:
        return ok("scans", "Scan copies", "None saved yet.")
    text = ("%s in %d scan cop%s (%s). Scans keep copies so damaged files can be put back from a good one; "
            "unchanged files share space, and only the newest few are kept."
            % (gd1chat.core.human_size(total), count, "y" if count == 1 else "ies", ", ".join(per)))
    return ok("scans", "Scan copies", text) if count <= 1 else \
        warn("scans", "Scan copies", text, "Clearing keeps each device's newest scan and any last good copies.",
             "Clear old scan copies", "scans") if total > 20e9 else \
        dict(ok("scans", "Scan copies", text), fix="Clear old scan copies", fix_id="scans")


def _claude(app):
    st = app.claude.key_status()
    if st["state"] == "connected":
        return ok("claude", "Claude (cloud AI)", "Connected. Your API key works (checked for free).")
    if st["state"] == "none":
        return ok("claude", "Claude (cloud AI)", "Not connected (optional). Add an API key with the ☁ Claude button.")
    if st["state"] == "rejected":
        return bad("claude", "Claude (cloud AI)", "Anthropic rejected the saved API key.",
                   "Make a new key on platform.claude.com and paste it with the ☁ Claude button.")
    return warn("claude", "Claude (cloud AI)", st["text"], "Check the internet connection, then look again.")


CHECKS = [_ollama, _certs, _internet, _build_tools, _devices, _disk, _scan_data, _secrets, _claude]


def migrate_secrets(app):
    import keychain
    dirs = set()
    for f, cfg in app.registry.all():
        try:
            import gd1check
            dirs.add(str(gd1check.data_dir(cfg)))
        except Exception:
            pass
    return keychain.migrate(Path(__file__).resolve().parent, sorted(dirs))


def run_checks(app):
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(app))
        except Exception as e:  # a broken check shouldn't break the page
            out.append(warn(getattr(fn, "__name__", "check"), "Check", "This check errored: %s" % e, ""))
    return out


def fix(app, check_id, emit):
    """Try to put one thing right. Returns a sentence for the user. emit streams progress lines."""
    if check_id == "keychain":
        moved = migrate_secrets(app)
        return ("Moved into the Mac's Keychain: %s." % ", ".join(moved)) if moved else \
            "Nothing more to move (or the Keychain said no)."
    if check_id == "scans":
        import gd1chat
        gone = 0
        for _, cfg in app.registry.all():
            gone += gd1chat.core.prune_scans(gd1chat.core.data_dir(cfg), 1)
        return "Removed %d older scan cop%s. Each device keeps its newest scan (and any last good copies)." % (
            gone, "y" if gone == 1 else "ies")
    if check_id == "ollama":
        if sys.platform != "darwin":
            raise RuntimeError("Start the Ollama app yourself on this computer.")
        try:
            subprocess.run(["/usr/bin/open", "-a", "Ollama"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError("Couldn't start Ollama: %s. Is it installed? (ollama.com)" % e)
        return "Asked Ollama to start. Give it a few seconds, then check again."
    if check_id == "certs":
        tlsfix.MAC_BUNDLE.unlink() if tlsfix.MAC_BUNDLE.exists() else None
        src = tlsfix.install()
        if src in ("none", None):
            raise RuntimeError(tlsfix.HELP)
        return "Loaded the Mac's certificates. Secure connections should work now."
    if check_id == "build_tools":
        import projects
        emit({"type": "status", "text": "Installing the Switch build tools (your Mac will ask for its password)..."})
        code, out = projects.install_packages("devkitpro", ["switch-dev", "switch-zlib"], False, emit)
        if code != 0:
            raise RuntimeError("The install didn't finish: %s" % (out.strip().splitlines()[-1:] or ["unknown"])[0])
        return "Installed the Switch build tools (switch-dev, switch-zlib)."
    raise RuntimeError("There's no automatic fix for that one.")


# ------------------------------------------------------------------ the error report

def report(app):
    """Everything useful for troubleshooting, as plain text the user can copy. No passwords or keys."""
    import gd1chat
    import mac_app
    lines = ["Patchbay report — %s" % _now()]
    lines.append("Computer: %s, %s, Python %s" % (app.computer, sys.platform, sys.version.split()[0]))
    lines.append("Patchbay version: %s" % _git_commit())
    lines.append("")
    for c in run_checks(app):
        lines.append("[%s] %s — %s" % ({"ok": "OK", "warn": "!", "bad": "X"}.get(c["level"], "?"),
                                       c["name"], c["detail"]))
    m = mac_app.status()
    lines.append("[.] App icon: %s (%s), start at login: %s" % (
        "yes" if m["app"] else "no", "own window" if m.get("native") else "browser launcher",
        "on" if m["login"] else "off"))
    if m.get("note") and not m.get("native"):
        lines.append("    " + m["note"])
    if m.get("build_error"):
        lines.append("    Window version build error (last part):")
        lines += ["      " + x for x in m["build_error"].splitlines()[-30:]]
    try:
        cm = app.claude.settings()
        lines.append("[.] Claude help: mode %s, key %s, model %s" % (cm["mode"], "set" if cm.get("key") else "none",
                                                                     cm["model"]))
    except Exception:
        pass
    lines.append("")
    errs = list(getattr(gd1chat, "RECENT_ERRORS", []))
    lines.append("Recent problems (newest last):" if errs else "No recent problems recorded.")
    lines += ["  " + e for e in errs[-25:]]
    return "\n".join(lines)


def _git_commit():
    try:
        r = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ok(cid, name, detail, advice=""):
    return {"id": cid, "name": name, "level": "ok", "detail": detail, "advice": advice}


def warn(cid, name, detail, advice="", fix=None, fix_id=None):
    return {"id": cid, "name": name, "level": "warn", "detail": detail, "advice": advice, "fix": fix,
            "fix_id": fix_id or cid}


def bad(cid, name, detail, advice="", fix=None, fix_id=None):
    return {"id": cid, "name": name, "level": "bad", "detail": detail, "advice": advice, "fix": fix,
            "fix_id": fix_id or cid}
