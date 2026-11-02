"""
Step 3: send the checked spec to Claude and get the code back as files.

One fresh request each time: the instructions below, the spec, and a write_files tool Claude must answer with.
No chat history, no memory, no file paths, no device details. Uses PatchBay's Claude settings (API key from the
Keychain, monthly limit, the "what was sent" log in claude-sent/).
"""

import posixpath
import re

CODE_SYSTEM = (
    "You are an implementation engine. You get one self-contained technical specification and nothing else. Write "
    "complete, working source files that implement it exactly, plus the unit tests it lists. Rules: use relative "
    "file paths only (no leading slash, no '..'); no placeholder code or TODOs; keep dependencies to what the spec "
    "allows; if the spec leaves a choice open, make a sensible one and say so in notes. Return everything with one "
    "call to write_files.")

WRITE_FILES = {
    "name": "write_files",
    "description": "Return every file of the implementation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path, like src/parser.py"},
                               "content": {"type": "string", "description": "The whole file"}},
                "required": ["path", "content"]}},
            "notes": {"type": "string", "description": "Short notes: how to run the tests, choices made"},
        },
        "required": ["files"],
    },
}

MAX_TOKENS = 20000
MAX_FILES = 60
MAX_FILE_BYTES = 400 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
OUTPUT_GUESS = (1500, MAX_TOKENS)       # likely output tokens: small module .. the most it may write


class CodeError(Exception):
    pass


def request_body(spec):
    """Exactly what goes to Claude (PatchBay adds the model name). Nothing but the spec."""
    return {"system": CODE_SYSTEM, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": spec}],
            "tools": [WRITE_FILES], "tool_choice": {"type": "tool", "name": "write_files"}}


def estimate(spec, prices):
    """(input tokens, lowest $, highest $) as a rough guess. prices: ($ per M input, $ per M output)."""
    chars = len(CODE_SYSTEM) + len(spec or "") + len(str(WRITE_FILES)) + 400
    tokens = int(chars / 3.5)
    inp, out = prices
    low = (tokens * inp + OUTPUT_GUESS[0] * out) / 1e6
    high = (tokens * inp + OUTPUT_GUESS[1] * out) / 1e6
    return tokens, low, high


def clean_path(path):
    """A safe relative path inside the output folder, or CodeError."""
    p = str(path or "").strip().replace("\\", "/")
    if not p or p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p):
        raise CodeError("Claude gave a file path that isn't relative: %r" % path)
    norm = posixpath.normpath(p)
    parts = norm.split("/")
    if norm.startswith("..") or ".." in parts or norm == ".":
        raise CodeError("Claude gave a file path outside the folder: %r" % path)
    for part in parts:
        if part in (".git", ".ssh", ".gnupg", ".aws") or part.endswith((".pem", ".key", ".p12")):
            raise CodeError("Claude gave a file path PatchBay won't write: %r" % path)
    return norm


def files_from(resp):
    """[(path, content)], notes from Claude's answer. Checks every path and the sizes."""
    if resp.get("stop_reason") == "max_tokens":
        raise CodeError("Claude's answer was cut off because the code got too long. Split the idea into smaller "
                        "parts and build them one at a time.")
    block = next((b for b in resp.get("content") or [] if b.get("type") == "tool_use"
                  and b.get("name") == "write_files"), None)
    if not block:
        raise CodeError("Claude didn't send any files back.")
    data = block.get("input") or {}
    items = data.get("files") or []
    if not items:
        raise CodeError("Claude sent an empty list of files.")
    if len(items) > MAX_FILES:
        raise CodeError("Claude sent %d files; the most PatchBay takes at once is %d." % (len(items), MAX_FILES))
    out, seen, total = [], set(), 0
    for f in items:
        path = clean_path((f or {}).get("path"))
        content = str((f or {}).get("content") or "")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise CodeError("%s is too big (%d KB)." % (path, size // 1024))
        total += size
        if path.lower() in seen:
            raise CodeError("Claude sent %s twice." % path)
        seen.add(path.lower())
        out.append((path, content))
    if total > MAX_TOTAL_BYTES:
        raise CodeError("The files are too big all together (%d KB)." % (total // 1024))
    return out, str(data.get("notes") or "").strip()


def generate(claude_client, spec, model, why="private build"):
    """Send the spec (one stateless request) and return ([(path, content)], notes, response)."""
    resp = claude_client.send(request_body(spec), why, model=model, effort="high")
    files, notes = files_from(resp)
    return files, notes, resp
