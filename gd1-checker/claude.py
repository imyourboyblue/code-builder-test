"""
Asking Claude (Anthropic's cloud AI) for help when the local model is stuck, for plans of big jobs, or for
everything ("Always"). Uses the user's own API key, kept only on this Mac in claude.json (never in git).

Before anything is sent, personal details are swapped for stand-ins: your Mac user name and home folder, IP
addresses, device names, the Mac's name, .local host names, email addresses, passwords and keys. The stand-ins
are turned back into the real values for anything Claude asks Patchbay to do, so it still works. Every request
that was sent is saved in claude-sent/ so you can read exactly what left the Mac.

Only uses Python's built-in parts (urllib), so nothing extra needs installing.
"""

import getpass
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import tlsfix

API_URL = os.environ.get("PATCHBAY_CLAUDE_URL") or "https://api.anthropic.com/v1/messages"   # override for tests only
API_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MODELS = {
    # model id: (name, $ per million input tokens, $ per million output tokens, $ per million cached tokens read)
    "claude-opus-5-5": ("Claude Opus 5.5 (best)", 4.00, 20.00, 0.20),
    "claude-sonnet-5-5": ("Claude Sonnet 5.5 (cheaper, faster)", 2.00, 10.00, 0.20),
}
MODES = {
    "off": "Off: only the AI on your Mac",
    "stuck": "When stuck: ask Claude when a step fails twice",
    "plan": "Plan + when stuck: Claude plans big jobs and helps when stuck; your Mac does the work",
    "always": "Always: Claude answers everything (sent automatically; costs the most)",
}
DEFAULTS = {"mode": "plan", "model": "claude-opus-5-5", "auto_send": False, "monthly_cap": 20.0, "key": ""}
SENT_KEEP = 200
# Platform words that are fine to send as they are, even when a device is named after them.
GENERIC = {"switch", "psp", "vita", "3ds", "wii", "wiiu", "ps4", "ps3", "ps5", "ds", "gamecube", "xbox", "mac",
           "linux", "android", "pi", "raspberry", "nas", "server", "router", "steam", "deck", "steamdeck"}


class ClaudeError(Exception):
    pass


class Claude:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.file = self.folder / "claude.json"
        self.usage_file = self.folder / "claude-usage.json"
        self.sent_dir = self.folder / "claude-sent"
        self.aliases_file = self.folder / "claude-aliases.json"
        self.lock = threading.Lock()
        self.session_cost = 0.0
        self.session_calls = 0

    # ---------------------------------------------------------------- settings

    def settings(self):
        s = dict(DEFAULTS)
        try:
            s.update(json.loads(self.file.read_text()))
        except (OSError, ValueError):
            pass
        if s.get("mode") not in MODES:
            s["mode"] = DEFAULTS["mode"]
        if s.get("model") not in MODELS:
            s["model"] = DEFAULTS["model"]
        return s

    def save_settings(self, changes):
        s = self.settings()
        for k, v in (changes or {}).items():
            if k == "mode":
                if v not in MODES:
                    raise ClaudeError("mode must be one of: %s" % ", ".join(MODES))
                s[k] = v
            elif k == "model":
                if v not in MODELS:
                    raise ClaudeError("model must be one of: %s" % ", ".join(MODELS))
                s[k] = v
            elif k == "auto_send":
                s[k] = bool(v)
            elif k == "monthly_cap":
                try:
                    s[k] = max(0.0, round(float(v), 2))
                except (TypeError, ValueError):
                    raise ClaudeError("The monthly limit must be a number of dollars, like 20.")
            elif k == "key":
                v = (v or "").strip()
                if v and not re.match(r"^sk-ant-[A-Za-z0-9_\-]{20,}$", v):
                    raise ClaudeError("That doesn't look like an Anthropic API key (they start with sk-ant-).")
                s[k] = v
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2))
        try:
            os.chmod(str(tmp), 0o600)    # only your Mac user can read the key
        except OSError:
            pass
        tmp.replace(self.file)
        return self.public()

    def has_key(self):
        return bool(self.settings().get("key"))

    def mode(self):
        """The mode in effect: 'off' when there's no key yet."""
        s = self.settings()
        return s["mode"] if s.get("key") else "off"

    def public(self):
        """Settings for the page. The key itself never leaves the Mac, only its last 4 characters."""
        s = self.settings()
        key = s.pop("key", "")
        month = self.month_cost()
        return dict(s, key_set=bool(key), key_end=key[-4:] if key else "", modes=MODES,
                    models={k: v[0] for k, v in MODELS.items()}, session_cost=round(self.session_cost, 4),
                    session_calls=self.session_calls, month_cost=round(month, 4),
                    cap_reached=bool(s["monthly_cap"]) and month >= s["monthly_cap"])

    # ---------------------------------------------------------------- cost

    def _usage(self):
        try:
            return json.loads(self.usage_file.read_text())
        except (OSError, ValueError):
            return {}

    def month_cost(self):
        return float(self._usage().get("months", {}).get(time.strftime("%Y-%m"), 0.0))

    def check_cap(self):
        s = self.settings()
        cap = s.get("monthly_cap") or 0
        if cap and self.month_cost() >= cap:
            raise ClaudeError("Claude's monthly limit of $%.2f is reached ($%.2f spent this month), so nothing was "
                              "sent. To keep going, raise the limit in the Claude panel (the ☁ Claude button above "
                              "the chat), or wait until next month." % (cap, self.month_cost()))

    @staticmethod
    def price(model, usage):
        _, inp, out, cached = MODELS.get(model, MODELS["claude-opus-5-5"])
        u = usage or {}
        return (u.get("input_tokens", 0) * inp + u.get("cache_creation_input_tokens", 0) * inp * 1.25 +
                u.get("cache_read_input_tokens", 0) * cached + u.get("output_tokens", 0) * out) / 1e6

    def _add_cost(self, dollars):
        with self.lock:
            self.session_cost += dollars
            self.session_calls += 1
            u = self._usage()
            months = u.setdefault("months", {})
            m = time.strftime("%Y-%m")
            months[m] = round(months.get(m, 0.0) + dollars, 6)
            u["calls"] = u.get("calls", 0) + 1
            tmp = self.usage_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(u, indent=2))
            tmp.replace(self.usage_file)

    # ---------------------------------------------------------------- privacy

    def scrubber(self, known):
        """A Scrubber for these personal values (device names, hosts, the Mac's name...)."""
        try:
            aliases = json.loads(self.aliases_file.read_text())
        except (OSError, ValueError):
            aliases = {}
        return Scrubber(known, aliases)

    def save_aliases(self, sc):
        if sc.changed:
            tmp = self.aliases_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(sc.aliases, indent=2))
            try:
                os.chmod(str(tmp), 0o600)
            except OSError:
                pass
            tmp.replace(self.aliases_file)

    # ---------------------------------------------------------------- sending

    def send(self, body, why):
        """Send one Messages API request (already scrubbed). Returns the response JSON. Saves what was sent."""
        s = self.settings()
        if not s.get("key"):
            raise ClaudeError("There's no Claude API key yet. Add yours in the Claude panel (the ☁ Claude button above "
                              "the chat). Get one at https://platform.claude.com/settings/keys")
        self.check_cap()
        body = dict(body, model=s["model"])
        body.setdefault("max_tokens", 16000)
        body.setdefault("output_config", {"effort": "high"})
        body["cache_control"] = {"type": "ephemeral"}        # reuse the unchanged start of the conversation: cheaper
        body["fallbacks"] = "default"                         # if Claude declines, Anthropic retries on another model
        self._log_sent(body, why)
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "x-api-key": s["key"], "anthropic-version": API_VERSION,
                   "anthropic-beta": FALLBACK_BETA}
        last = None
        for attempt in range(4):
            req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                try:
                    msg = json.loads(detail).get("error", {}).get("message") or detail
                except ValueError:
                    msg = detail
                if e.code in (429, 500, 502, 503, 504, 529) and attempt < 3:
                    last = msg
                    time.sleep(int(e.headers.get("retry-after") or 0) or 2 ** (attempt + 1))
                    continue
                if e.code == 401:
                    raise ClaudeError("Claude said the API key isn't valid. Check it in the Claude panel.")
                if e.code == 400 and "credit" in msg.lower():
                    raise ClaudeError("Your Anthropic account is out of credit. Add some at "
                                      "https://platform.claude.com/settings/billing")
                raise ClaudeError("Claude said (%d): %s" % (e.code, msg[:400]))
            except OSError as e:
                last = str(e)
                if attempt < 3 and "CERTIFICATE_VERIFY_FAILED" not in last:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ClaudeError(tlsfix.HELP if "CERTIFICATE_VERIFY_FAILED" in str(e) else
                                  "Couldn't reach Claude (%s). Is the internet working?" % e)
        else:
            raise ClaudeError("Claude is busy right now (%s). Try again in a minute." % last)
        self._add_cost(self.price(resp.get("model") or s["model"], resp.get("usage")))
        if resp.get("stop_reason") == "refusal":
            raise ClaudeError("Claude declined to answer this one.")
        return resp

    def _log_sent(self, body, why):
        """Keep a copy of exactly what was sent (without the key), so the user can check it."""
        try:
            self.sent_dir.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-%04d.json" % (int(time.time() * 1000) % 10000)
            (self.sent_dir / name).write_text(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "why": why,
                                                          "request": body}, indent=1))
            old = sorted(self.sent_dir.glob("*.json"))[:-SENT_KEEP]
            for f in old:
                f.unlink()
        except OSError:
            pass

    def sent_list(self, n=30):
        rows = []
        for f in sorted(self.sent_dir.glob("*.json"), reverse=True)[:n] if self.sent_dir.is_dir() else []:
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            rows.append({"name": f.name, "time": d.get("time"), "why": d.get("why"), "text": readable(d["request"])})
        return rows

    # ---------------------------------------------------------------- one question

    def ask(self, question, sc, why="question"):
        """One question, no tools. question is already scrubbed. Returns Claude's answer with the real values back."""
        body = {"system": QUESTION_SYSTEM, "messages": [{"role": "user", "content": question}]}
        resp = self.send(body, why)
        text = "\n".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
        return sc.restore(text) or "(Claude gave no answer)"

    # ---------------------------------------------------------------- the whole chat ("Always")

    def chat(self, system, messages, tools, sc):
        """One turn of the chat, in Ollama's message format. Returns an Ollama-style assistant message."""
        body = {"system": sc.scrub(system), "messages": to_anthropic(messages, sc),
                "tools": [{"name": t["function"]["name"], "description": t["function"]["description"],
                           "input_schema": t["function"]["parameters"]} for t in tools]}
        resp = self.send(body, "chat")
        content = resp.get("content") or []
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        calls = [{"function": {"name": b["name"], "arguments": sc.restore_obj(b.get("input") or {})}, "id": b["id"]}
                 for b in content if b.get("type") == "tool_use"]
        if resp.get("stop_reason") == "max_tokens" and not calls:
            text += "\n(Claude's answer was cut off because it got too long.)"
        return {"content": sc.restore(text), "tool_calls": calls, "claude": content}


QUESTION_SYSTEM = (
    "You are helping Patchbay, a tool on a hobbyist's Mac that fixes and rebuilds homebrew programs for game "
    "consoles and other devices on their home network. A smaller local AI is doing the work and is stuck, or needs "
    "a plan. Personal details were replaced with stand-ins like <device-1>, <ip-1> or ~ ; use the stand-ins as they "
    "are. Answer with concrete, numbered steps the local AI can follow with its tools: exact commands, exact file "
    "names, exact package names. Say what to check first if you're unsure. Keep it short. The local AI can: read "
    "and edit project files, run commands in the project folder on the Mac, install devkitPro and Homebrew packages "
    "(the Mac asks the user for the password), build, read files on the device, and write to the device after the "
    "user confirms.")


class Scrubber:
    """Swaps personal values for stand-ins before sending, and back again for Claude's answers."""

    IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    MACADDR = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
    LOCAL = re.compile(r"\b[A-Za-z0-9-]+\.(?:local|lan|home|internal)\b")
    SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+)|((?:password|passwd|token|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
                        r"[^\s\"',}]+", re.I)

    def __init__(self, known, aliases):
        self.aliases = aliases          # real value -> stand-in, kept between runs so stand-ins never change
        self.changed = False
        self.words = []                 # (regex, real value)
        home = str(Path.home())
        self.paths = [(home, "~")]
        user = getpass.getuser()
        for kind, value in known:
            value = (value or "").strip()
            if not value or len(value) < 2 or value.lower() in GENERIC or value.startswith(("127.", "localhost")):
                continue
            self.words.append((kind, value))
        if user and user.lower() not in GENERIC and len(user) > 2:
            self.words.append(("user", user))
        # Longest first, so "Joshuas-Mac-Studio" is replaced before "Joshua".
        self.words.sort(key=lambda kv: -len(kv[1]))

    def _alias(self, kind, value):
        a = self.aliases.get(value)
        if not a:
            n = 1 + sum(1 for v in self.aliases.values() if v.startswith("<%s-" % kind))
            a = "<%s-%d>" % (kind, n)
            self.aliases[value] = a
            self.changed = True
        return a

    def scrub(self, text):
        if not text:
            return text
        t = str(text)
        for real, alias in self.paths:
            t = t.replace(real, alias)
        t = re.sub(r"/Users/[^/\s\"']+", "~", t)
        t = self.SECRET.sub(lambda m: "<secret>" if m.group(1) else m.group(2) + "<secret>", t)
        t = self.EMAIL.sub(lambda m: self._alias("email", m.group(0)), t)
        t = self.MACADDR.sub(lambda m: self._alias("hw", m.group(0)), t)
        for kind, real in self.words:
            t = re.sub(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(real),
                       lambda m, k=kind, r=real: self._alias(k, r), t, flags=re.I)
        t = self.LOCAL.sub(lambda m: self._alias("host", m.group(0)), t)
        t = self.IP.sub(lambda m: m.group(0) if m.group(0).startswith(("0.", "127.", "255.")) else
                        self._alias("ip", m.group(0)), t)
        return t

    def restore(self, text):
        if not text:
            return text
        back = {v: k for k, v in self.aliases.items()}
        return re.sub(r"<(?:device|ip|host|email|hw|user|mac|name)-\d+>", lambda m: back.get(m.group(0), m.group(0)), text)

    def restore_obj(self, obj):
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, list):
            return [self.restore_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.restore_obj(v) for k, v in obj.items()}
        return obj

    def scrub_obj(self, obj):
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, list):
            return [self.scrub_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        return obj


def _image_block(b64):
    kind = "image/jpeg" if b64.startswith("/9j/") else "image/png" if b64.startswith("iVBOR") else \
        "image/gif" if b64.startswith("R0lG") else "image/webp" if b64.startswith("UklG") else None
    return {"type": "image", "source": {"type": "base64", "media_type": kind, "data": b64}} if kind else None


def to_anthropic(messages, sc):
    """Ollama-style chat history -> Messages API history. Claude's own earlier replies are sent back unchanged."""
    out, pending_ids = [], []

    def add(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant":
            if pending_ids:      # a tool call never got a result (e.g. the user pressed Stop)
                add("user", [{"type": "tool_result", "tool_use_id": t, "content": "(no result)"} for t in pending_ids])
                pending_ids = []
            if m.get("claude"):
                blocks = m["claude"]
                ids = [b["id"] for b in blocks if b.get("type") == "tool_use"]
            else:
                blocks, ids = [], []
                text = sc.scrub(m.get("content") or "")
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                if text:
                    blocks.append({"type": "text", "text": text})
                for j, c in enumerate(m.get("tool_calls") or []):
                    fn = c.get("function", {})
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    tid = "pb_%d_%d" % (i, j)
                    blocks.append({"type": "tool_use", "id": tid, "name": fn.get("name") or "unknown",
                                   "input": sc.scrub_obj(args)})
                    ids.append(tid)
                if not blocks:
                    blocks = [{"type": "text", "text": "(no answer)"}]
            add("assistant", blocks)
            pending_ids = ids
        elif role == "tool":
            tid = pending_ids.pop(0) if pending_ids else None
            text = sc.scrub(m.get("content") or "(no output)")
            if tid:
                add("user", [{"type": "tool_result", "tool_use_id": tid, "content": text}])
            else:
                add("user", [{"type": "text", "text": "[Result of %s]\n%s" % (m.get("tool_name") or "a step", text)}])
        else:
            if pending_ids:
                add("user", [{"type": "tool_result", "tool_use_id": t, "content": "(no result)"} for t in pending_ids])
                pending_ids = []
            blocks = [b for b in (_image_block(x) for x in (m.get("images") or [])) if b]
            blocks.append({"type": "text", "text": sc.scrub(m.get("content") or "(empty)")})
            add("user", blocks)
    if pending_ids:
        add("user", [{"type": "tool_result", "tool_use_id": t, "content": "(no result)"} for t in pending_ids])
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})
    return out


def readable(body):
    """What a request sent, as plain text for the page (pictures shortened)."""
    rows = ["Model: %s" % body.get("model")]
    sysm = body.get("system")
    if sysm:
        rows.append("== Instructions ==\n" + (sysm if isinstance(sysm, str) else json.dumps(sysm))[:3000])
    if body.get("tools"):
        rows.append("== Tools offered == " + ", ".join(t["name"] for t in body["tools"]))
    for m in body.get("messages", []):
        c = m["content"]
        if isinstance(c, str):
            rows.append("== %s ==\n%s" % (m["role"], c))
            continue
        for b in c:
            t = b.get("type")
            if t == "text":
                rows.append("== %s ==\n%s" % (m["role"], b["text"]))
            elif t == "tool_use":
                rows.append("== %s asks for %s ==\n%s" % (m["role"], b["name"], json.dumps(b.get("input"))[:2000]))
            elif t == "tool_result":
                rows.append("== step result ==\n%s" % str(b.get("content"))[:4000])
            elif t == "image":
                rows.append("== %s == (a picture)" % m["role"])
    return "\n\n".join(rows)
