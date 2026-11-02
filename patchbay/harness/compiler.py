"""
Step 1: the AI on this Mac (Ollama) turns a raw idea into a generic software spec. Nothing leaves the Mac here.

Also the optional double-check: the same local AI reads the finished spec and lists anything that still looks
private (a name, a place, a brand, a project). That list is only used to stop the send, never sent anywhere.

On its own:  python3 -m harness.compiler idea.txt -o spec_sanitized.md [--local-model NAME]
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SYSTEM_PROMPT = (
    "You are an abstract technical spec compiler. Convert the user's ideas into a clean, standalone technical "
    "software specification. STICK TO STRICT ANONYMIZATION RULES: 1. Strip all project names, brand names, "
    "environment details, internal endpoints, and business reasons. 2. Define only data structures, function "
    "signatures, input/output requirements, algorithm steps, and edge cases. 3. Treat the task strictly as an "
    "abstract utility function or isolated open-source module. Output ONLY Markdown formatted technical specs.\n\n"
    "Also: 4. Never copy any real name of a person, place, company, device, network, computer, user account, file "
    "or folder from the idea. Never include IP or MAC addresses, emails, passwords, keys or home-folder paths. Where "
    "an example value is needed, use neutral ones like ExampleDevice, 192.0.2.10, user@example.com or ./data. "
    "5. Use relative file names only. 6. Use these sections: # Module (one neutral sentence), ## Language and "
    "runtime, ## Files to create, ## Data types, ## Functions (signatures, inputs, outputs, errors), ## Algorithm, "
    "## Edge cases, ## Unit tests (concrete cases with expected results). 7. Write the spec so a programmer who "
    "knows nothing else about it can build it completely.")

RETRY_NOTE = ("Your last spec still contained private details: %s. Write the whole spec again without them (use "
              "neutral placeholders), following every rule.")

CHECK_PROMPT = (
    "Read this software spec. List every word or phrase in it that could identify a real person, place, company, "
    "brand, product, project, device name, computer, network, account, address or file path. Do NOT list ordinary "
    "programming words, language or library names, standard formats or protocols, or neutral placeholders like "
    "ExampleDevice, 192.0.2.10, user@example.com. Copy each one exactly as written. Answer only with JSON: "
    "{\"suspect\": [\"...\"]}. If there are none, answer {\"suspect\": []}.\n\nSPEC:\n")

# Common words the double-check sometimes flags; they never identify anyone.
NOT_PRIVATE = {"python", "javascript", "typescript", "json", "yaml", "markdown", "http", "https", "tcp", "udp", "ftp",
               "sftp", "smb", "utf-8", "c", "c++", "rust", "go", "swift", "linux", "macos", "windows", "unix", "posix",
               "sqlite", "csv", "xml", "html", "css", "api", "cli", "url", "uuid", "example", "exampledevice",
               "pytest", "unittest", "stdlib", "standard library"}


class CompileError(Exception):
    pass


def _ollama_chat(url, model, messages, timeout=900, fmt=None, urlopen=None, think=None):
    body = {"model": model, "stream": False, "messages": messages, "options": {"temperature": 0.2}}
    if fmt:
        body["format"] = fmt
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if think is not None and "think" in detail.lower():
            return _ollama_chat(url, model, messages, timeout, fmt, urlopen, None)   # this model can't think
        if e.code == 404:
            raise CompileError("Ollama doesn't have the model %s. Pick another one, or download it in "
                               "Setup → AI." % model)
        raise CompileError("Ollama said (%s): %s" % (e.code, detail))
    except (OSError, ValueError) as e:
        raise CompileError("Couldn't reach Ollama at %s (%s). Is the Ollama app open?" % (url, e))
    text = (data.get("message") or {}).get("content") or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def tidy(spec):
    """Drop a ```markdown fence around the whole answer, and anything before the first heading."""
    s = (spec or "").strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", s, re.S)
    if m:
        s = m.group(1).strip()
    i = s.find("#")
    if i > 0 and "\n" in s[:i]:
        s = s[i:]
    return s.strip() + "\n"


def compile_spec(idea, url, model, problems=None, previous=None, urlopen=None):
    """The idea as a generic Markdown spec. problems: what the last try leaked (to write it again without)."""
    idea = (idea or "").strip()
    if not idea:
        raise CompileError("There's no idea to turn into a spec.")
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": idea}]
    if problems and previous:
        msgs += [{"role": "assistant", "content": previous},
                 {"role": "user", "content": RETRY_NOTE % ", ".join(problems)}]
    spec = tidy(_ollama_chat(url, model, msgs, urlopen=urlopen, think=True))
    if len(spec) < 40:
        raise CompileError("The local AI didn't write a spec (it answered: %r). Try again, or describe the idea "
                           "in more detail." % spec[:200])
    return spec


def double_check(spec, url, model, allowed=(), urlopen=None):
    """Words the local AI thinks may still be private. Only words that really are in the spec count."""
    try:
        raw = _ollama_chat(url, model, [{"role": "user", "content": CHECK_PROMPT + spec}], timeout=600,
                           fmt="json", urlopen=urlopen, think=False)
        found = json.loads(raw).get("suspect") or []
    except (CompileError, ValueError, AttributeError):
        return []
    ok = {a.strip().lower() for a in allowed}
    out = []
    low = spec.lower()
    for w in found:
        w = str(w).strip().strip("\"'`")
        if len(w) < 3 or w.lower() in NOT_PRIVATE or w.lower() in ok or w.lower() not in low:
            continue
        if re.match(r"^(example|sample|test|foo|bar|my_?|placeholder)", w, re.I) or "example" in w.lower():
            continue
        if w not in out:
            out.append(w)
    return out[:20]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m harness.compiler", description="Turn an idea into a generic spec "
                                 "with the AI on this Mac.")
    ap.add_argument("idea", help="text file with the raw idea")
    ap.add_argument("-o", "--output", default="spec_sanitized.md")
    ap.add_argument("--local-model", help="Ollama model (default: the one PatchBay uses now)")
    ap.add_argument("--ollama-url", help="default: PatchBay's setting, or http://localhost:11434")
    args = ap.parse_args(argv)
    from harness import pipeline
    url, model = pipeline.local_ai(args.ollama_url, args.local_model)
    try:
        spec = compile_spec(Path(args.idea).read_text("utf-8", "replace"), url, model)
    except (OSError, CompileError) as e:
        print("Failed: %s" % e, file=sys.stderr)
        return 1
    Path(args.output).write_text(spec, "utf-8")
    print("Saved %s (%d lines) with %s." % (args.output, spec.count("\n"), model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
