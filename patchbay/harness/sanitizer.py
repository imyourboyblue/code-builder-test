"""
The safety gate: checks a spec for anything private before it may leave this Mac.

It looks for:
  - your own words in config/redaction_list.json ("blocked_terms"; also "blocked_patterns" as regular expressions)
  - values PatchBay already knows are yours (device names, addresses, user names, passwords, project names and
    folders, the Mac's name...), passed in by pipeline.auto_terms()
  - things that look private whatever they are: IP and MAC addresses, emails, home-folder paths, local host names,
    API keys, tokens and passwords

Any hit means the spec is not sent. Example addresses that are made for documents (192.0.2.x, 198.51.100.x,
203.0.113.x, 127.0.0.1, anything@example.com) are fine.

On its own:  python3 -m harness.sanitizer spec_sanitized.md   (exit code 0 = clean, 1 = blocked)
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # the patchbay folder
LIST_FILE = HERE / "config" / "redaction_list.json"

EMPTY_LIST = {
    "_help": "Words that must never be sent to Claude by the private builder. blocked_terms: plain words "
             "(not case-sensitive). blocked_patterns: regular expressions. allowed_terms: words the local AI's "
             "double-check flagged that are fine to send. This file stays on your Mac (it is not in git).",
    "blocked_terms": [],
    "blocked_patterns": [],
    "allowed_terms": [],
}

# Addresses and names meant for examples and documents: safe to send.
SAFE_IP = re.compile(r"^(0\.0\.0\.0|127\.\d+\.\d+\.\d+|255\.255\.255\.\d+|192\.0\.2\.\d+|198\.51\.100\.\d+|"
                     r"203\.0\.113\.\d+)$")
SAFE_MAC = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "aa:bb:cc:dd:ee:ff", "00:11:22:33:44:55",
            "01:23:45:67:89:ab"}
SAFE_EMAIL = re.compile(r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|example|test|invalid|localhost)$", re.I)
PLACEHOLDER = re.compile(r"^(?:[<{$\[(*]|x{3,}|\.\.\.|your|example|changeme|placeholder|redacted|secret\b|"
                         r"password\b|null|none|true|false|\"|')", re.I)

# (kind, plain description, pattern)
PATTERNS = [
    ("ip", "an IP address", re.compile(r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)"
                                       r"(?![\w.]*\d)")),
    ("ip", "an IPv6 address", re.compile(r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}|fe80:[0-9a-f:]{3,}|"
                                         r"f[cd][0-9a-f]{2}:[0-9a-f:]{3,})(?![\w:])", re.I)),
    ("mac", "a network card (MAC) address", re.compile(r"(?<![\w:-])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![\w:-])",
                                                       re.I)),
    ("email", "an email address", re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)),
    ("path", "a home-folder path", re.compile(r"(?:/Users/|/home/|/Volumes/|[A-Za-z]:\\Users\\)[^\s/\\\"'`]+|"
                                              r"/mnt/[^\s/\\\"'`]+")),
    ("host", "a home-network host name", re.compile(r"(?<![\w.-])[\w-]+(?:\.[\w-]+)*\.(?:local|lan|home|internal|"
                                                    r"localdomain|corp|intranet|home\.arpa)(?![\w.-])", re.I)),
    ("key", "an API key or token", re.compile(r"sk-ant-[\w-]{8,}|\bsk-[A-Za-z0-9]{20,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
                                              r"github_pat_\w{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-[\w-]{10,}|"
                                              r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}|"
                                              r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("secret", "a password or secret value", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|"
                                                        r"auth)\b[\"']?\s*[:=]\s*[\"']?([^\s\"',;}]{4,})")),
]


def list_path():
    return LIST_FILE


def load_list(path=None, create=True):
    """The block list as a dict. Made (empty) the first time, so there is a file to add words to."""
    p = Path(path) if path else LIST_FILE
    if not p.exists():
        if create and p == LIST_FILE:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(EMPTY_LIST, indent=2) + "\n")
            except OSError:
                pass
        return dict(EMPTY_LIST)
    try:
        data = json.loads(p.read_text("utf-8"))
    except ValueError as e:
        raise ValueError("%s isn't valid JSON (%s). Fix it, or delete it to start a fresh one." % (p, e))
    out = dict(EMPTY_LIST)
    for k in ("blocked_terms", "blocked_patterns", "allowed_terms"):
        v = data.get(k) or []
        if not isinstance(v, list):
            raise ValueError("In %s, %s must be a list of words." % (p, k))
        out[k] = [str(x) for x in v if str(x).strip()]
    return out


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _term_re(term):
    t = re.escape(term.strip())
    # Whole words only when the term starts/ends with a letter or digit, so "Joe" doesn't match "Joel".
    left = r"(?<![A-Za-z0-9])" if term.strip()[:1].isalnum() else ""
    right = r"(?![A-Za-z0-9])" if term.strip()[-1:].isalnum() else ""
    return re.compile(left + t + right, re.I)


def _user_re(name):
    """A user name that is also an everyday word (like "blue") only counts where it is used as a user name:
    /Users/blue, ~blue, blue@host, "user: blue", "logged in as blue", "blue's Mac"."""
    t = re.escape(name.strip())
    before = (r"(?:(?:/Users/|/home/|\\Users\\|~)|\b(?:user(?:[ _-]?name)?|login|account|owner|uid|whoami|"
              r"(?:logged|signed) in as|home folder of|run as|sudo -u|-u)\b[\s:=\"'`]*)")
    return re.compile(r"(?:%s%s(?![A-Za-z0-9])|(?<![A-Za-z0-9])%s(?:@|['’]s\s+(?:mac|computer|laptop|account|"
                      r"home)))" % (before, t, t), re.I)


def scan(text, terms=(), blocked=None):
    """Everything private found in text, as a list of dicts:
         {"kind", "what" (plain description), "shown" (safe to show on screen), "line"}
    terms: (kind, value) pairs PatchBay knows are personal. kind "secret" values are never shown.
    blocked: the block list (load_list()). Empty list = clean."""
    text = str(text or "")
    blocked = blocked or dict(EMPTY_LIST)
    allowed = {a.strip().lower() for a in blocked.get("allowed_terms") or []}
    found, seen = [], set()

    def hit(kind, what, value, pos, hide=False):
        key = (kind, value.lower())
        if key in seen:
            return
        seen.add(key)
        found.append({"kind": kind, "what": what, "shown": "(hidden)" if hide else value, "line": _line_of(text, pos)})

    for word in blocked.get("blocked_terms") or []:
        if len(word.strip()) >= 2:
            m = _term_re(word).search(text)
            if m:
                hit("blocked", "a word on your block list", m.group(0), m.start())
    for pat in blocked.get("blocked_patterns") or []:
        try:
            m = re.search(pat, text, re.I)
        except re.error as e:
            raise ValueError("The block list pattern %r isn't a valid regular expression (%s)." % (pat, e))
        if m:
            hit("blocked", "a pattern on your block list", m.group(0), m.start())
    terms = list(terms)
    # User names that are plain words: only blocked where they're used as a user name (see _user_re).
    users = {v.strip().lower() for k, v in terms if k == "user" and re.match(r"^[A-Za-z]+$", (v or "").strip())}
    for kind, value in terms:
        value = (value or "").strip()
        if len(value) < 3 or value.lower() in allowed:
            continue
        m = (_user_re(value) if value.lower() in users else _term_re(value)).search(text)
        if m:
            hide = kind in ("secret", "password")
            what = {"device": "one of your device names", "ip": "one of your device addresses",
                    "user": "a user name of yours", "secret": "one of your saved passwords",
                    "project": "one of your project names", "folder": "one of your folders",
                    "mac": "this Mac's name", "name": "your name", "email": "your email address",
                    "seen": "a personal detail PatchBay hid from Claude before"}.get(kind, "a personal detail")
            hit(kind, what, m.group(0), m.start(), hide)
    for kind, what, rx in PATTERNS:
        for m in rx.finditer(text):
            value = m.group(0)
            if kind == "ip" and "." in value and SAFE_IP.match(value):
                continue
            if kind == "mac" and value.lower().replace("-", ":") in SAFE_MAC:
                continue
            if kind == "email" and SAFE_EMAIL.search(value):
                continue
            if kind == "secret":
                v = m.group(1)
                if PLACEHOLDER.match(v) or v.lower() in ("required", "optional", "string", "str", "bytes", "input", "bearer",
                                                                "header", "env", "environ"):
                    continue
                hit(kind, what, value[:len(value) - len(v)] + "(hidden)", m.start())
                continue
            hit(kind, what, value, m.start(), hide=(kind == "key"))
    found.sort(key=lambda f: f["line"])
    return found


def explain(findings, limit=12):
    """Plain lines for the user: what was found and on which line of the spec."""
    rows = ["- line %d: %s: %s" % (f["line"], f["what"], f["shown"]) for f in findings[:limit]]
    if len(findings) > limit:
        rows.append("- ...and %d more" % (len(findings) - limit))
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m harness.sanitizer",
                                 description="Check a spec for private details. Exit code 0 = clean, 1 = blocked.")
    ap.add_argument("spec", help="the spec file to check (for example spec_sanitized.md)")
    ap.add_argument("--list", help="block list (default: config/redaction_list.json)")
    ap.add_argument("--no-patchbay", action="store_true", help="don't add PatchBay's own device names, IPs...")
    args = ap.parse_args(argv)
    try:
        text = Path(args.spec).read_text("utf-8", "replace")
        blocked = load_list(args.list)
    except (OSError, ValueError) as e:
        print("Couldn't check it: %s" % e, file=sys.stderr)
        return 1
    terms = []
    if not args.no_patchbay:
        from harness import pipeline
        terms = pipeline.auto_terms()
    found = scan(text, terms, blocked)
    if found:
        print("BLOCKED: %s has %d private detail(s):\n%s" % (args.spec, len(found), explain(found, 50)))
        return 1
    print("Clean: nothing private found in %s." % args.spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
