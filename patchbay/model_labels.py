"""
What the AI model menus show: what each model is good for, instead of its name and version.

Edit the lists below to change the words. The first matching rule wins. A rule matches if its text appears
anywhere in the model's name (for example "27b" matches "qwen3.8:27b"). Models no rule matches get a label
from their size.
"""

import json
import re
import time
import urllib.request

# Models on your Mac (Ollama). (text in the model name, menu label, one-line description)
LOCAL = [
    ("35b-a3b", "Everyday help", "Fast and smart. Good for most questions, checks and fixes."),
    ("27b", "Hard problems and building", "Slower, but thinks harder. Use it for tricky crashes and building apps."),
    ("gemma", "Second opinion", "A different maker's model (Google). Automatic asks it when the usual models get "
                                "stuck on the same step twice."),
    ("coder", "Writing code", "Made for reading and writing code."),
    ("vl", "Reading pictures", "Can look at screenshots and photos."),
    ("vision", "Reading pictures", "Can look at screenshots and photos."),
    ("embed", "Search helper (not for chat)", "Used for searching text. It can't hold a chat."),
]

# Claude in the cloud. (model id, menu label, one-line description)
CLOUD = {
    "claude-opus-5-5": ("Hardest problems", "Claude's best thinker. Costs more."),
    "claude-sonnet-5-5": ("Everyday help, cheaper", "Faster and about half the price. Good for most jobs."),
}

# For models no rule above matches, by download size in GB.
BY_SIZE = [
    (6, "Quick answers", "Small and very fast. Fine for simple questions."),
    (25, "Everyday help", "A good balance of speed and thinking."),
    (10 ** 6, "Hard problems and building", "Big and slow, but thinks the hardest."),
]


def local(name, size_bytes=0):
    """(label, description) for a model on the Mac."""
    low = (name or "").lower()
    for text, label, why in LOCAL:
        if text in low:
            return label, why
    gb = (size_bytes or 0) / 1e9
    if not gb:
        return "Other model", "A model installed in Ollama."
    for limit, label, why in BY_SIZE:
        if gb <= limit:
            return label, why
    return BY_SIZE[-1][1], BY_SIZE[-1][2]


def label_all(models):
    """Add label and why to a list of {"name", "bytes"} dicts. Two models with the same label get "2", "3"..."""
    seen = {}
    for m in models:
        label, why = local(m.get("name"), m.get("bytes"))
        seen[label] = seen.get(label, 0) + 1
        m["label"] = label if seen[label] == 1 else "%s %d" % (label, seen[label])
        m["why"] = why
    return models


def cloud(model_id, fallback=""):
    return CLOUD.get(model_id, (fallback or model_id, ""))


# ---------------------------------------------------------------- Automatic: pick the model for the job

AUTO = "auto"
AUTO_LABEL = ("Automatic", "Uses the fast model for questions and lookups, and the stronger one for builds, fixes "
                           "and plans.")
# Which local model does which job, by text in its name (first one installed wins). Edit to change the picks.
FAST = ["35b-a3b", "30b-a3b", "a3b", "14b", "12b", "8b", "7b"]
STRONG = ["27b", "32b", "70b", "35b", "30b", "coder"]
SECOND = ["gemma4:26b", "gemma4", "gemma"]      # a different maker, for when the usual models are stuck
SECOND_OPINION_MODEL = "gemma4:26b"             # what 🩺 Setup offers to download (about 18 GB)
HARD_WORDS = re.compile(r"(?i)\b(build|compile|rebuild|crash|debug|fix|repair|broken|error|fail|plan|update|"
                        r"upgrade|install|source|code|makefile|cmake|patch)\w*")

_TAGS = {"at": 0, "names": [], "url": None}


def installed(ollama_url):
    """Names of the models Ollama has (asked at most once a minute)."""
    if _TAGS["url"] == ollama_url and time.time() - _TAGS["at"] < 60:
        return _TAGS["names"]
    try:
        with urllib.request.urlopen(ollama_url + "/api/tags", timeout=8) as r:
            names = [m.get("name") for m in json.loads(r.read()).get("models", []) if m.get("name")]
    except (OSError, ValueError):
        names = _TAGS["names"] if _TAGS["url"] == ollama_url else []
    _TAGS.update(at=time.time(), names=names, url=ollama_url)
    return names


def _first(names, wants, avoid=()):
    for w in wants:
        for n in names:
            low = n.lower()
            if w in low and n not in avoid and not any(x in low for x in ("embed", "vl", "vision")):
                return n
    return None


def second(names):
    """The second-opinion model, if one is installed."""
    for w in SECOND:
        for n in names:
            if w in n.lower():
                return n
    return None


def picks(names):
    """(fast model, strong model) from what's installed. The second-opinion model is kept for when they're stuck."""
    chat = [n for n in names if not any(x in n.lower() for x in ("embed",)) and n != second(names)]
    fast = _first(chat, FAST) or (chat[0] if chat else None)
    strong = _first(chat, STRONG, avoid=(fast,)) or fast
    return fast, strong


def needs_strong(messages):
    """(True/False, plain reason) for the next answer, from the chat so far."""
    last_user, tools = "", []
    for m in reversed(messages):
        if m.get("role") == "tool":
            tools.append(str(m.get("content") or ""))
        if m.get("role") == "user" and not str(m.get("content") or "").startswith("["):
            last_user = str(m.get("content") or "")
            break
        if m.get("role") == "user" and "approved the plan" in str(m.get("content") or ""):
            return True, "carrying out a plan"
    fails = sum(1 for t in tools[:6] if re.search(r"(?i)\b(failed|error)\b", t[:400]))
    if fails >= 2:
        return "second", "stuck: a second opinion"
    if fails:
        return True, "a step failed"
    m = HARD_WORDS.search(last_user)
    if m:
        return True, "%s job" % m.group(1).lower()
    return False, "a quick question"


def choose(ollama_url, messages):
    """The model to use with Automatic on, and a short label like "Everyday help: a quick question"."""
    names = installed(ollama_url)
    fast, strong = picks(names)
    hard, why = needs_strong(messages)
    model = strong if hard else fast
    if hard == "second":
        model = second(names) or strong
        if model == strong:
            why = "a step failed twice"
    if not model:
        return None, ""
    return model, "%s model (%s)" % (local(model)[0], why)
