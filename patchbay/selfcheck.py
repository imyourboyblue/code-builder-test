"""
The self-check: is everything PatchBay needs working? Ollama, secure connections, the build tools, the internet,
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
import core


def _ollama(app):
    import server
    cfg = app.home.cfg()
    try:
        with urllib.request.urlopen(server.ollama_url(cfg) + "/api/tags", timeout=8) as r:
            models = [m.get("name") for m in json.loads(r.read()).get("models", [])]
    except Exception as e:
        if core.ai_where(cfg) != "this Mac":
            return bad("ollama", "Ollama (the AI)", "Can't reach the AI on %s: %s" % (core.ai_where(cfg), tlsfix.explain(e)),
                       "Turn that computer on and open Ollama there, or switch back under Where the AI runs below.")
        return bad("ollama", "Ollama (the AI)", "Can't reach Ollama: %s" % tlsfix.explain(e),
                   "The AI runs in the Ollama app. Make sure it's installed (ollama.com) and open.", fix="Start Ollama")
    want = cfg.get("ollama_model")
    if want == "auto":
        import model_labels
        lay = model_labels.layer_models(model_labels.tool_names(server.ollama_url(cfg), models), cfg.get("model_layers"))
        fast, strong = lay["everyday"], lay["hard"]
        if not fast:
            return warn("ollama", "Ollama (the AI)", "Running, but no models are downloaded.",
                        "In Terminal run: ollama pull qwen3.6:35b-a3b")
        return ok("ollama", "Ollama (the AI)", "Running on %s. Automatic uses %s for quick things and %s for hard jobs."
                  % (core.ai_where(cfg), model_labels.local(fast)[0], model_labels.local(strong)[0]))
    if models and want and want not in models:
        return warn("ollama", "Ollama (the AI)", "Running, but the chosen model %s isn't downloaded. You have: %s"
                    % (want, ", ".join(models[:6]) or "none"),
                    "In Terminal run: ollama pull %s" % want)
    return ok("ollama", "Ollama (the AI)", "Running on %s, %d model%s ready." % (core.ai_where(cfg), len(models), "" if len(models) == 1 else "s"))


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
                    "Only needed to rebuild Nintendo homebrew. PatchBay can install it for you.",
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
                    "Add one from the Devices screen, or ask PatchBay to add it.")
    if not on:
        return warn("devices", "Your devices", "None of your %d device(s) answered just now." % len(rows),
                    "Check they're switched on and on the same network.")
    return ok("devices", "Your devices", "%d of %d online: %s" % (len(on), len(rows),
              ", ".join(r["name"] for r in on[:8])))


def _disk(app):
    import server
    try:
        folder = server.core.data_dir(app.home.cfg())
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
        return ok("keychain", name, "Kept in PatchBay's settings files (the Keychain is only on a Mac).")
    if k["problems"]:
        return warn("keychain", name, k["problems"][-1], "Restart PatchBay after allowing it.")
    if k["plain"]:
        return warn("keychain", name, "Still in a settings file: %s." % ", ".join(k["plain"]),
                    "PatchBay moves them into the Mac's Keychain by itself.", "Move them now", "keychain")
    if not k["in_keychain"]:
        return ok("keychain", name, "No passwords saved (your devices use anonymous logins).")
    return ok("keychain", name, "%d kept safely in the Mac's Keychain (Keychain Access, \"PatchBay\")."
              % k["in_keychain"])


def _scan_data(app):
    import server
    total, count, waiting = 0, 0, 0
    for _, cfg in app.registry.all():
        try:
            b, n = server.old_snapshot_usage(cfg)
            waiting += len(server.core.attention(cfg))
        except Exception:
            continue
        total, count = total + b, count + n
    att = (" %d problem file%s kept under Needs attention." % (waiting, "" if waiting == 1 else "s")) if waiting else ""
    if not count:
        return ok("scans", "Scan copies", "Scans don't keep copies: clean files are deleted after checking." + att)
    return warn("scans", "Scan copies", "%s in %d old scan cop%s from before scans stopped keeping copies.%s"
                % (server.core.human_size(total), count, "y" if count == 1 else "ies", att),
                "They're not needed any more. Deleting them keeps a good copy of any file that's on Needs attention.",
                "Delete old scan copies", "scans")


PULL = {"model": None, "state": None, "done": 0, "total": 0, "error": ""}


def start_pull(url, model):
    """Start downloading a model in the background (one at a time). Returns False if one is already going."""
    import threading
    if PULL["state"] == "downloading":
        return False
    PULL.update(model=model, state="downloading", done=0, total=0, error="")
    threading.Thread(target=_pull, args=(url, model), daemon=True).start()
    return True


def _pull(url, model):
    """Download a model with Ollama, in the background. Progress goes to PULL for 🩺 Setup."""
    PULL.update(model=model, state="downloading", done=0, total=0, error="")
    try:
        req = urllib.request.Request(url + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise OSError(ev["error"])
                if ev.get("total"):
                    PULL.update(done=ev.get("completed") or 0, total=ev["total"])
                if ev.get("status") == "success":
                    PULL["state"] = "done"
        if PULL["state"] != "done":
            raise OSError("the download stopped before it finished")
    except Exception as e:
        PULL.update(state="failed", error=str(e))


def _second_opinion(app):
    import model_labels
    import server
    name = "Second opinion AI model"
    cfg = app.home.cfg()
    names = model_labels.installed(server.ollama_url(cfg))
    picked = (cfg.get("model_layers") or {}).get("second")
    if picked and picked in names:
        return ok("second", name, "%s (picked in Setup → AI). Automatic asks it when the usual models fail the same "
                                  "step twice." % picked)
    if model_labels.second(names):
        return ok("second", name, "Installed (%s). Automatic asks it when the usual models fail the same step twice."
                  % model_labels.second(names))
    if PULL["state"] == "downloading":
        pct = (" %d%%" % (100 * PULL["done"] / PULL["total"])) if PULL["total"] else ""
        return ok("second", name, "Downloading %s...%s It keeps going if you close this. Check again later."
                  % (PULL["model"], pct))
    extra = " The last download didn't finish: %s." % PULL["error"] if PULL["state"] == "failed" else ""
    return dict(ok("second", name, "Not installed. Gemma 4 (26B, from Google) gives a second opinion from a different "
                                   "maker when the usual models get stuck. About 18 GB to download.%s" % extra),
                fix="Download it (18 GB)", fix_id="second")


def _renamed(app):
    import migrate
    try:
        notes = json.loads(migrate.NOTES.read_text())
    except (OSError, ValueError):
        notes = []
    old = [cfg["device_name"] for _, cfg in app.registry.all() if cfg["device_name"].upper() == "GD1"]
    if old:
        return warn("renamed", "After the rename", "A device is still called %s (from the old GD1 checker). Its data "
                    "is safe." % old[0], "Rename it with ✎ Rename device on its Files tab, if you like.")
    if notes:
        return ok("renamed", "After the rename", " ".join(notes[-3:]))
    return None


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


CHECKS = [_ollama, _certs, _internet, _build_tools, _devices, _disk, _scan_data, _secrets, _second_opinion, _claude, _renamed]


def migrate_secrets(app):
    import keychain
    dirs = set()
    for f, cfg in app.registry.all():
        try:
            import core
            dirs.add(str(core.data_dir(cfg)))
        except Exception:
            pass
    return keychain.migrate(Path(__file__).resolve().parent, sorted(dirs))


def run_checks(app):
    out = []
    for fn in CHECKS:
        try:
            r = fn(app)
            if r:
                out.append(r)
        except Exception as e:  # a broken check shouldn't break the page
            out.append(warn(getattr(fn, "__name__", "check"), "Check", "This check errored: %s" % e, ""))
    return out


def fix(app, check_id, emit):
    """Try to put one thing right. Returns a sentence for the user. emit streams progress lines."""
    if check_id == "keychain":
        moved = migrate_secrets(app)
        return ("Moved into the Mac's Keychain: %s." % ", ".join(moved)) if moved else \
            "Nothing more to move (or the Keychain said no)."
    if check_id == "second":
        import model_labels
        import server
        import threading
        if not start_pull(server.ollama_url(app.home.cfg()), model_labels.SECOND_OPINION_MODEL):
            return "%s is still downloading. Wait for it to finish first." % PULL["model"]
        return ("Started downloading %s (about 18 GB) through Ollama. It keeps going in the background; press Check "
                "again to see how far it is." % model_labels.SECOND_OPINION_MODEL)
    if check_id == "scans":
        import server
        gone = sum(server.clear_old_snapshots(cfg) for _, cfg in app.registry.all())
        return "Deleted %d old scan cop%s. Good copies of files on Needs attention were kept." % (
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
    import server
    import mac_app
    lines = ["PatchBay report — %s" % _now()]
    lines.append("Computer: %s, %s, Python %s" % (app.computer, sys.platform, sys.version.split()[0]))
    lines.append("PatchBay version: %s" % _git_commit())
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
    errs = list(getattr(server, "RECENT_ERRORS", []))
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
