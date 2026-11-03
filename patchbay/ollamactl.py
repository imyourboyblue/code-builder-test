"""
Starting and stopping Ollama on this Mac, so you don't have to.

- When PatchBay opens and Ollama isn't answering, PatchBay starts it (the Ollama app if you have it,
  otherwise `ollama serve`) and the chat says "Starting Ollama..." until it answers.
- Then it loads the model you picked in Setup → AI ("Load this model when PatchBay starts", setting
  ollama_startup_model), like `ollama run <model>` but with no Terminal window left open.
- When PatchBay quits, it unloads the model right away and (setting ollama_on_close) quits Ollama too:
  "started" = only if PatchBay started it (the default), "always", or "never" (then the model is just handed back
  to Ollama's usual 5-minute timer). What PatchBay started is kept in services.py, so a crash is tidied up next time.
- "Stop Ollama completely" (Setup → AI) ends Ollama and any leftover model processes (llama-server, ollama runner).
  It shows the list first. Programs that aren't Ollama's are listed but never stopped.

Only ever acts on Ollama on this Mac. If the AI runs on another computer, nothing here touches it.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import core
import services

APP_PATHS = [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"]
CLI_PATHS = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"]
BREW_PLIST = Path.home() / "Library" / "LaunchAgents" / "homebrew.mxcl.ollama.plist"
START_WAIT = 45          # seconds to wait for Ollama to answer after starting it
ON_CLOSE = ("started", "always", "never")
LOAD_WAIT = 600          # seconds a big model may take to load
# Used when the setting was never saved. Joshua's pick (2026-11-03); Setup → AI changes it ("" = off).
STARTUP_MODEL_DEFAULT = "qwen36-uncensored-fixed"

STATE = {"starting": False, "started_by_us": False, "how": None, "pid": None, "error": "", "since": 0.0,
         "loading": ""}
_LOCK = threading.Lock()


def here(cfg):
    """True when the AI runs on this Mac (the only place PatchBay starts or stops Ollama)."""
    return sys.platform == "darwin" and core.ai_where(cfg) == "this Mac"


def is_up(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=timeout) as r:
            r.read()
        return True
    except (OSError, ValueError):
        return False


def how_installed():
    """How Ollama is set up on this Mac: "app" (the Ollama app), "brew" (a Homebrew service),
    "cli" (just the ollama command), or None."""
    for p in APP_PATHS:
        if p.exists():
            return "app"
    if BREW_PLIST.exists():
        return "brew"
    if ollama_cli():
        return "cli"
    return None


def ollama_cli():
    found = shutil.which("ollama")
    if found:
        return found
    return next((p for p in CLI_PATHS if os.path.exists(p)), None)


def status_text():
    """A line for the chat and Setup while Ollama is starting (or why it couldn't), else ""."""
    if STATE["starting"]:
        return "Starting Ollama... give it a few seconds."
    if STATE["loading"]:
        return "Loading %s..." % STATE["loading"]
    return STATE["error"]


def start(cfg, wait=True):
    """Start Ollama if it's on this Mac and not answering. Returns a plain sentence of what happened."""
    try:
        url = core.check_ai_url(cfg["ollama_url"])
    except (KeyError, ValueError) as e:
        return str(e)
    if not here(cfg):
        return "The AI runs on %s, so PatchBay doesn't start Ollama there." % core.ai_where(cfg)
    if is_up(url):
        return "Ollama is already running."
    with _LOCK:
        if STATE["starting"]:
            return status_text()
        how = how_installed()
        if not how:
            STATE["error"] = "Ollama isn't installed on this Mac. Get it from ollama.com, then open PatchBay again."
            return STATE["error"]
        STATE.update(starting=True, error="", how=how, since=time.time())
    try:
        STATE["pid"] = _launch(how, cfg)
    except (OSError, subprocess.SubprocessError) as e:
        STATE.update(starting=False, error="Couldn't start Ollama (%s). Open the Ollama app yourself." % e)
        return STATE["error"]
    STATE["started_by_us"] = True
    services.record("ollama", "ollama", "Ollama", how=how, pid=STATE["pid"])
    if not wait:
        threading.Thread(target=_wait_up, args=(url,), daemon=True).start()
        return status_text()
    return _wait_up(url)


def _launch(how, cfg):
    if how == "app":   # -g: in the background, so it doesn't jump in front of PatchBay
        subprocess.run(["open", "-g", "-a", "Ollama"], check=True, capture_output=True, timeout=20)
        return None
    if how == "brew":
        brew = shutil.which("brew") or next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                             if os.path.exists(p)), None)
        if brew:
            subprocess.run([brew, "services", "start", "ollama"], check=True, capture_output=True, timeout=60)
            return None
    logs = Path(__file__).resolve().parent / "data-cache"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "ollama-serve.log", "ab")
    p = subprocess.Popen([ollama_cli(), "serve"], stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         start_new_session=True)   # keeps running on its own, even if PatchBay is stopped
    return p.pid


def _wait_up(url):
    end = time.time() + START_WAIT
    while time.time() < end:
        if is_up(url):
            STATE.update(starting=False, error="")
            print("Started Ollama (%s)." % STATE["how"])
            return "Started Ollama."
        time.sleep(0.5)
    STATE.update(starting=False, error="Started Ollama, but it isn't answering yet. Is it asking for something "
                                       "in the menu bar? Try opening the Ollama app.")
    return STATE["error"]


def startup_model(cfg):
    """The model to load when PatchBay starts, or "" for none."""
    m = cfg.get("ollama_startup_model", STARTUP_MODEL_DEFAULT)
    return str(m or "").strip()


def installed(url):
    """Names of the models in `ollama list`, or None if Ollama didn't answer."""
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            return [m.get("name") or m.get("model") for m in json.loads(r.read()).get("models") or []]
    except (OSError, ValueError):
        return None


def same_model(a, b):
    """qwen3 and qwen3:latest are the same model."""
    def norm(n):
        n = (n or "").strip().lower()
        return n if ":" in n else n + ":latest"
    return norm(a) == norm(b)


def load_model(cfg, model):
    """Load a model into memory and keep it there, like `ollama run <model>` without the Terminal window.
    Returns a plain sentence."""
    try:
        url = core.check_ai_url(cfg["ollama_url"])
    except (KeyError, ValueError) as e:
        return str(e)
    names = installed(url)
    if names is None:
        return "Ollama isn't answering, so %s wasn't loaded." % model
    match = next((n for n in names if same_model(n, model)), None)
    if not match:
        STATE["error"] = ("%s isn't one of your models (ollama list), so nothing was loaded. Pick another in "
                          "Setup → AI → Load this model when PatchBay starts." % model)
        return STATE["error"]
    STATE.update(loading=match, error="")
    body = json.dumps({"model": match, "keep_alive": -1}).encode()   # no prompt: just load it
    req = urllib.request.Request(url + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=LOAD_WAIT) as r:
            r.read()
    except (OSError, ValueError) as e:
        STATE.update(loading="", error="Couldn't load %s: %s" % (match, e))
        return STATE["error"]
    STATE["loading"] = ""
    print("Loaded %s." % match)
    return "Loaded %s." % match


def _start_and_load(cfg):
    if cfg.get("ollama_autostart", True) or startup_model(cfg):
        start(cfg, True)
    model = startup_model(cfg)
    if model and here(cfg) and not STATE["starting"] and not STATE["error"]:
        load_model(cfg, model)


def start_in_background(cfg):
    """PatchBay is opening: start Ollama and load the chosen model without holding up the page."""
    if not here(cfg) or not (cfg.get("ollama_autostart", True) or startup_model(cfg)):
        return
    threading.Thread(target=_start_and_load, args=(cfg,), daemon=True).start()


def adopt(cfg, row):
    """A PatchBay that crashed (or this one before an update) started Ollama: look after it as our own."""
    if not here(cfg):
        services.forget("ollama")
        return
    try:
        up = is_up(core.check_ai_url(cfg["ollama_url"]))
    except (KeyError, ValueError):
        up = False
    if not up:
        services.forget("ollama")
        return
    STATE["started_by_us"] = True
    STATE["how"] = STATE["how"] or row.get("how")
    services.record("ollama", "ollama", "Ollama", how=row.get("how"), pid=row.get("pid"))


# ---- what's running

def processes():
    """Ollama's processes on this Mac, plus any llama-server: [{"pid", "what", "ollama": bool, "cmd"}]."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]) if parts[1].isdigit() else 0, parts[2]))
    return pick(rows, os.getpid())


def pick(rows, me=0):
    """From (pid, parent pid, command) rows, the ones that are Ollama or a model server. Pure, for the tests."""
    def name(cmd):
        return os.path.basename(cmd.split()[0]).lower() if cmd.split() else ""

    ours = {pid for pid, _, cmd in rows if name(cmd) == "ollama" or "ollama.app/" in cmd.lower()}
    found = []
    for pid, ppid, cmd in rows:
        n, low = name(cmd), cmd.lower()
        if pid == me:
            continue
        model_server = n in ("llama-server", "ollama_llama_server")
        if not (pid in ours or model_server):
            continue
        is_ollama = pid in ours or ppid in ours or "ollama" in low   # its own folder, or models in ~/.ollama
        if "ollama.app/contents/macos/" in low:
            what = "the Ollama app"
        elif model_server or " runner" in low:
            what = "a model in memory (%s)" % n
        elif " serve" in low:
            what = "Ollama's background server"
        else:
            what = n
        found.append({"pid": pid, "what": what, "ollama": bool(is_ollama), "cmd": cmd[:200]})
    return found


# ---- stopping

def let_go(cfg, unload):
    """Hand loaded models back to Ollama's usual 5-minute timer (or unload them now)."""
    try:
        url = core.check_ai_url(cfg["ollama_url"])
        with urllib.request.urlopen(url + "/api/ps", timeout=2) as r:
            names = [m.get("name") or m.get("model") for m in json.loads(r.read()).get("models") or []]
    except (OSError, ValueError, KeyError):
        return []
    done = []
    for n in names:
        body = json.dumps({"model": n, "messages": [], "keep_alive": 0 if unload else "5m"}).encode()
        req = urllib.request.Request(url + "/api/chat", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            done.append(n)
        except (OSError, ValueError):
            pass
    return done


def stop_all(cfg):
    """Quit Ollama and end leftover model processes on this Mac. Never touches programs that aren't Ollama's.
    Returns a plain report."""
    if not here(cfg):
        return "The AI runs on %s. PatchBay only stops Ollama on this Mac." % core.ai_where(cfg)
    let_go(cfg, unload=True)
    how = how_installed()
    if how == "brew":    # otherwise Homebrew starts it again straight away
        brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
        try:
            subprocess.run([brew, "services", "stop", "ollama"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    try:   # the polite way first: the app quits and takes its server with it
        subprocess.run(["osascript", "-e", 'tell application "Ollama" to quit'], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(10):
        if not [p for p in processes() if p["ollama"]]:
            break
        time.sleep(0.5)
    left = [p for p in processes() if p["ollama"]]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in left:
            try:
                os.kill(p["pid"], sig)
            except OSError:
                pass
        for _ in range(10):
            left = [p for p in processes() if p["ollama"]]
            if not left:
                break
            time.sleep(0.3)
    STATE.update(started_by_us=False, starting=False, error="", pid=None, loading="")
    if not left:
        services.forget("ollama")
    others = [p for p in processes() if not p["ollama"]]
    msg = "Ollama is stopped." if not left else ("Couldn't stop %d Ollama process(es): %s" % (
        len(left), ", ".join(str(p["pid"]) for p in left)))
    if others:
        msg += " Left running (not Ollama's): " + "; ".join("%s, pid %d" % (p["cmd"][:80], p["pid"]) for p in others)
    return msg


def on_close(cfg):
    """PatchBay is quitting. Quit Ollama if the setting says so; otherwise unload the model right away
    ("never": hand it back to Ollama's 5-minute timer instead)."""
    if not here(cfg):
        return ""
    mode = cfg.get("ollama_on_close", "started")
    quit_it = mode == "always" or (mode == "started" and (STATE["started_by_us"] or services.has("ollama")))
    if quit_it:
        return stop_all(cfg)
    if mode == "never":
        let_go(cfg, unload=False)
        return "Left Ollama running (its usual 5-minute timer will free the memory)."
    done = let_go(cfg, unload=True)
    if done:
        return "Unloaded %s. Left Ollama running (you started it, not PatchBay)." % ", ".join(done)
    return "Left Ollama running (you started it, not PatchBay)."
