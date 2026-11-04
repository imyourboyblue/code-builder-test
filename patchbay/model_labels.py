"""
What the AI model menus show: what each model is good for, instead of its name and version.

Edit the lists below to change the words. The first matching rule wins. A rule matches if its text appears
anywhere in the model's name (for example "27b" matches "qwen3.8:27b"). Models no rule matches get a label
from their size.
"""

import json
import re
import time
import urllib.error
import urllib.request

# Models on your Mac (Ollama). (text in the model name, menu label, one-line description)
LOCAL = [
    ("35b-a3b", "Everyday help", "Fast and smart. Good for most questions, checks and fixes."),
    ("27b", "Hard problems and building", "Slower, but thinks harder. Use it for tricky crashes and building apps."),
    ("gemma", "Second opinion", "A different maker's model (Google). PatchBay asks it when the model you loaded "
                                "gets stuck on the same step twice."),
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


# ---------------------------------------------------------------- which model answers

SECOND = ["gemma4:26b", "gemma4", "gemma"]      # a different maker, for when the usual models are stuck
SECOND_OPINION_MODEL = "gemma4:26b"             # what 🩺 Setup offers to download (about 18 GB)
HARD_WORDS = re.compile(r"(?i)\b(build|compile|rebuild|crash|debug|fix|repair|broken|error|fail|plan|update|"
                        r"upgrade|install|source|code|makefile|cmake|patch)\w*")

# ---------------------------------------------------------------- free models 🩺 Setup offers to download

# (Ollama name, what it's best for, a plain description, rough download size in GB)
# Edit this list to change what Setup offers. Setup checks each name with Ollama's library before showing a
# Download button, and shows the real size from there, so a name that doesn't exist is simply left out.
CATALOG = [
    ("qwen3.6:35b-a3b", "Everyday help", "PatchBay's usual model. Fast for its size because only part of it works "
                                          "on each word. Good for most questions, checks and fixes.", 24),
    ("qwen3.6:35b-a3b-nvfp4", "Everyday, faster", "The everyday model in Apple's MLX format: about twice as fast on "
                                                   "M-series Macs.", 23),
    ("qwen3.6:27b", "Building", "Thinks harder and slower. Good for building apps.", 17),
    ("qwen3.8:27b", "Hard problems and building", "Thinks harder and slower. Best for tricky crashes, plans and "
                                                 "building apps from source.", 17),
    ("gemma4:26b", "Second opinion", "Google's model. PatchBay asks it when your model gets stuck twice.", 18),
    ("qwen3-coder:30b", "Writing code", "Made for reading and writing code. Fast, like the everyday model.", 19),
    ("qwen3-vl:8b", "Reading pictures", "Can look at screenshots and photos, like an error on a TV screen.", 6),
    ("gemma3:12b", "Pictures and quick help", "Mid-size Google model that can also look at pictures.", 8),
    ("gpt-oss:20b", "Quick reasoning", "OpenAI's free model. Thinks step by step but stays fairly quick.", 14),
    ("deepseek-r1:14b", "Careful step-by-step thinking", "Shows its working on hard puzzles. Slow and wordy.", 9),
    ("mistral-small3.2:24b", "Following instructions", "Mistral's model. Good at doing exactly what it's asked "
                                                       "and using tools.", 15),
    ("llama3.2:3b", "Very quick, simple answers", "Tiny and very fast. Fine for simple questions, not for fixes.", 2),
]
REGISTRY = "https://registry.ollama.ai/v2/library/%s/manifests/%s"
_LIB = {}


def library_size(name):
    """(exists, size in GB) from Ollama's own library: True/False, or None when it couldn't be checked.
    Only the model's name is sent. Remembered for a day."""
    hit = _LIB.get(name)
    if hit and time.time() - hit[2] < 86400:
        return hit[0], hit[1]
    base, _, tag = name.partition(":")
    req = urllib.request.Request(REGISTRY % (base, tag or "latest"),
                                 headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            layers = json.loads(r.read()).get("layers") or []
        result = (True, round(sum(l.get("size") or 0 for l in layers) / 1e9, 1))
    except urllib.error.HTTPError as e:
        result = (False, None) if e.code == 404 else (None, None)
    except (OSError, ValueError):
        result = (None, None)
    if result[0] is not None:
        _LIB[name] = (result[0], result[1], time.time())
    return result


def catalog(names, memory_gb=None):
    """The free models, with whether each is installed, its size and whether it fits in memory."""
    import concurrent.futures
    have = {n.lower() for n in names}
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda c: library_size(c[0]), CATALOG))
    out = []
    for (name, best, why, rough), (exists, gb) in zip(CATALOG, found):
        if exists is False:
            continue        # not in Ollama's library (any more): don't offer it
        size = gb or rough
        fits = None
        if memory_gb:
            # The model has to fit in memory with room to spare for the Mac and the chat itself.
            fits = "yes" if size <= memory_gb * 0.7 else ("tight" if size <= memory_gb * 0.85 else "no")
        out.append({"name": name, "best": best, "why": why, "gb": size, "exact": bool(gb),
                    "installed": name.lower() in have or (":" not in name and name.lower() + ":latest" in have),
                    "checked": exists is True, "fits": fits})
    return out


_TAGS = {"at": 0, "names": [], "url": None}
_CAPS = {}
_RENDERER = {}   # (Ollama address, model) -> the prompt format Ollama uses for it, e.g. "qwen3.8" ("" if none)


def capabilities(ollama_url, name):
    """What Ollama says the model can do, e.g. ["completion", "tools", "thinking", "vision"]. None if it doesn't say
    (older Ollama) or couldn't be asked. Asked once per model."""
    if not name:
        return None
    key = (ollama_url, name)
    if key not in _CAPS:
        try:
            req = urllib.request.Request(ollama_url + "/api/show", data=json.dumps({"model": name}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                info = json.loads(r.read())
            caps = info.get("capabilities")
            _CAPS[key] = list(caps) if isinstance(caps, list) else None
            _RENDERER[key] = str(info.get("renderer") or "")
        except (OSError, ValueError):
            return None      # try again next time
    return _CAPS[key]


# ---------------------------------------------------------------- how hard each model thinks

# Per model, by text in its name (first match wins): "off", "low", "medium", "high", or "auto" (think harder only
# on hard jobs, as PatchBay always did). config.json "model_think", like {"orca-coder": "low"}, comes first.
# Qwen 3.8 models take "thinking on" as their slowest level, which went round in circles on some coding jobs.
# On a 36GB Mac, "low" solved 10 of 10 coding tests in about a quarter of the time (4 Nov 2026). The uncensored
# Qwen 3.6 35B did worse with thinking on (6 of 10 instead of 8, and 14 times slower), so it stays off.
THINK = [("orca-coder", "low"), ("qwen36-uncensored-fixed", "off")]
THINK_BY_FORMAT = {"qwen3.8": "low"}            # models whose prompt format (Ollama renderer) is this
LEVELS = {"qwen3.8": {"low": "low", "medium": "medium", "high": "xhigh"}}   # formats that understand levels
THINK_CHOICES = ("off", "low", "medium", "high", "auto")


def think_setting(cfg, ollama_url, name):
    """How hard this model thinks: one of THINK_CHOICES."""
    low = (name or "").lower()
    own = cfg.get("model_think") if isinstance(cfg.get("model_think"), dict) else {}
    for text, value in list(own.items()) + THINK:
        if str(text).lower() in low and str(value).lower() in THINK_CHOICES:
            return str(value).lower()
    capabilities(ollama_url, name)
    return THINK_BY_FORMAT.get(_RENDERER.get((ollama_url, name), ""), "auto")


def think_value(cfg, ollama_url, name, hard):
    """What to send Ollama as "think" for this model: False, True, or a level like "low". hard: the job looks hard
    (only matters for "auto"). The same model always gets the same value for the same kind of job, so Ollama can
    reuse its work from one message to the next."""
    setting = think_setting(cfg, ollama_url, name)
    if setting == "auto":
        return bool(hard)
    if setting == "off":
        return False
    capabilities(ollama_url, name)
    levels = LEVELS.get(_RENDERER.get((ollama_url, name), ""))
    return levels[setting] if levels else True       # models without levels only know on and off


def can_use_tools(ollama_url, name):
    """Can this model use PatchBay's tools (look at devices, read files, make cards)? True, False, or None."""
    caps = capabilities(ollama_url, name)
    return None if caps is None else "tools" in caps


def can_think(ollama_url, name):
    caps = capabilities(ollama_url, name)
    return None if caps is None else "thinking" in caps


def no_tools(ollama_url, name):
    """Remember that Ollama refused tools for this model."""
    caps = _CAPS.get((ollama_url, name)) or ["completion"]
    _CAPS[(ollama_url, name)] = [c for c in caps if c != "tools"]


def no_think(ollama_url, name):
    """Remember that Ollama refused thinking for this model."""
    caps = _CAPS.get((ollama_url, name)) or ["completion", "tools"]
    _CAPS[(ollama_url, name)] = [c for c in caps if c != "thinking"]


def tool_names(ollama_url, names):
    """The installed models that can use tools (unknown counts as yes)."""
    return [n for n in names if can_use_tools(ollama_url, n) is not False]


def installed(ollama_url, fresh=False):
    """Names of the models Ollama has (asked at most once a minute, unless fresh)."""
    if not fresh and _TAGS["url"] == ollama_url and time.time() - _TAGS["at"] < 60:
        return _TAGS["names"]
    try:
        with urllib.request.urlopen(ollama_url + "/api/tags", timeout=8) as r:
            names = [m.get("name") for m in json.loads(r.read()).get("models", []) if m.get("name")]
    except (OSError, ValueError):
        names = _TAGS["names"] if _TAGS["url"] == ollama_url else []
    _TAGS.update(at=time.time(), names=names, url=ollama_url)
    return names


def second(names):
    """The second-opinion model, if one is installed."""
    for w in SECOND:
        for n in names:
            if w in n.lower():
                return n
    return None


FASTER = {"qwen3.6:35b-a3b": "qwen3.6:35b-a3b-nvfp4"}    # the same model in Apple's faster MLX format
NO_MODEL = ("No model is loaded. Load one in Ollama, then ask again. In Terminal: ollama run %s "
            "(then type /bye). PatchBay keeps it loaded until you free the memory.")


def running(ollama_url, timeout=3):
    """Names of the models Ollama has loaded right now (its /api/ps), most recently used first."""
    with urllib.request.urlopen(ollama_url + "/api/ps", timeout=timeout) as r:
        rows = json.loads(r.read()).get("models") or []
    # Ollama counts each model's keep-alive from its last use, so the latest expiry is the latest used.
    rows.sort(key=lambda m: str(m.get("expires_at") or ""), reverse=True)
    return [m.get("name") or m.get("model") for m in rows if m.get("name") or m.get("model")]


def active(loaded):
    """The model PatchBay answers with: the one you loaded in Ollama (no guessing). Of several, the most recently
    used, leaving out search helpers and the second-opinion model while it's only reviewing. None if nothing is."""
    chat = [n for n in loaded or [] if "embed" not in n.lower()]
    main = [n for n in chat if n != second(chat)]
    return (main or chat or [None])[0]


def example(installed_names=()):
    """A model they have, for the load command PatchBay shows."""
    have = [n for n in installed_names if "embed" not in n.lower() and n != second(installed_names)]
    return have[0] if have else "MODEL-NAME"


def no_model(installed_names=()):
    """What to say when nothing is loaded, with a load command for a model they have."""
    return NO_MODEL % example(installed_names)


LOOKUPS = {"web_search", "read_web_page", "read_repo_file", "latest_release", "find_github_repo", "places_nearby"}


def which_layer(messages):
    """(layer, plain reason) for the next answer, from the chat so far.
    Within one question it only ever climbs (quick, everyday, hard, second), never back down, so a hard job
    doesn't swap big models in and out of memory on every step."""
    last_user, tools, plan = "", [], False
    for m in reversed(messages):
        if m.get("role") == "tool":
            text = str(m.get("content") or "")
            tools.append("(lookup) " + text[:60] if m.get("tool_name") in LOOKUPS else text)
        if m.get("role") == "user" and "approved the plan" in str(m.get("content") or ""):
            plan = True
        if m.get("role") == "user" and not str(m.get("content") or "").startswith("["):
            last_user = str(m.get("content") or "")
            break
    # Every failed step since the question counts, so the layer can't drop once a step has failed.
    # Only real failed steps (they start with "Error:" or say a build/command failed), not web pages or searches
    # that happen to contain the word "error", and not a web page that couldn't be read.
    fails = sum(1 for t in tools if not t.startswith("(lookup)") and (
        t.startswith("Error:") or re.search(r"\b(failed|did not work)\b.*\(exit code [1-9]", t[:600])))
    if fails >= 2:
        return "second", "stuck: a second opinion"
    if plan:
        return "hard", "carrying out a plan"
    if fails:
        return "hard", "a step failed"
    m = HARD_WORDS.search(last_user)
    if m:
        return "hard", "%s job" % m.group(1).lower()
    if not tools and len(last_user) <= 80:
        return "quick", "a short question"
    return "everyday", "everyday help"

