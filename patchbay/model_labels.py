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
AUTO_LABEL = ("Automatic", "Picks one of four layers for each answer: quick, everyday, hard, and a second opinion "
                           "when stuck. Choose each layer's model in Setup → AI.")
# Which local model does which job, by text in its name (first one installed wins). Edit to change the picks.
FAST = ["35b-a3b", "30b-a3b", "a3b", "14b", "12b", "8b", "7b"]
STRONG = ["27b", "32b", "70b", "35b", "30b", "coder"]
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
                                                   "M-series Macs. Replaces the normal one in Everyday mode.", 23),
    ("qwen3.6:27b", "Building", "Thinks harder and slower. Used in Building mode.", 17),
    ("qwen3.8:27b", "Hard problems and building", "Thinks harder and slower. Best for tricky crashes, plans and "
                                                 "building apps from source.", 17),
    ("gemma4:26b", "Second opinion", "Google's model. Automatic asks it when the usual models get stuck twice.", 18),
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


def capabilities(ollama_url, name):
    """What Ollama says the model can do, e.g. ["completion", "tools", "thinking", "vision"]. None if it doesn't say
    (older Ollama) or couldn't be asked. Asked once per model."""
    if not name or name == AUTO:
        return None
    key = (ollama_url, name)
    if key not in _CAPS:
        try:
            req = urllib.request.Request(ollama_url + "/api/show", data=json.dumps({"model": name}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                caps = json.loads(r.read()).get("capabilities")
            _CAPS[key] = list(caps) if isinstance(caps, list) else None
        except (OSError, ValueError):
            return None      # try again next time
    return _CAPS[key]


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


# ---------------------------------------------------------------- the two work modes

# Only one big model fits in the Mac's memory at a time, so the mode decides which one stays loaded.
# (key, name, what it's for, models to use in order: an exact name or text in the name; first installed wins)
MODES = [
    ("everyday", "Everyday", "Questions, device checks, research, documents and small fixes. Fast.",
     ["qwen3.6:35b-a3b-nvfp4", "qwen3.6:35b-a3b", "35b-a3b", "30b-a3b", "a3b", "26b-a4b", "14b", "12b", "8b"]),
    ("building", "Building", "Building apps, rebuilding homebrew and tricky crashes. Slower, thinks harder.",
     ["qwen3.6:27b-nvfp4", "qwen3.6:27b", "qwen3.8:27b", "27b", "32b", "31b", "coder"]),
]
MODE_KEYS = [m[0] for m in MODES]
FASTER = {"qwen3.6:35b-a3b": "qwen3.6:35b-a3b-nvfp4"}    # the same model in Apple's faster MLX format


def mode_model(names, mode):
    """The model a mode keeps loaded, from the installed models that can use tools."""
    wants = dict((k, w) for k, _, _, w in MODES).get(mode) or MODES[0][3]
    chat = [n for n in names if "embed" not in n.lower() and n != second(names)]
    for w in wants:
        for n in chat:
            if n == w or (":" not in w and w in n.lower()):
                return n
    fast, strong = picks(chat)
    return strong if mode == "building" else fast


# The four layers Automatic climbs: (key, name in Setup, when it's used)
LAYERS = [
    ("quick", "1. Quick answers", "Short questions and lookups."),
    ("everyday", "2. Everyday help", "Most questions, checks and small fixes."),
    ("hard", "3. Hard problems", "Builds, plans, crashes and anything that failed once."),
    ("second", "4. Second opinion", "When the step failed twice: a different model takes a fresh look."),
]
LAYER_KEYS = [k for k, _, _ in LAYERS]


def layer_models(names, chosen=None):
    """{layer: model} from what's installed. A model picked in Setup wins if it's still installed;
    otherwise PatchBay picks: quick = everyday = the fast model, hard = the strong one, second = Gemma or hard."""
    chosen = chosen or {}
    fast, strong = picks(names)
    default = {"quick": fast, "everyday": fast, "hard": strong, "second": second(names) or strong}
    return {k: chosen.get(k) if chosen.get(k) in names else default[k] for k in LAYER_KEYS}


LOOKUPS = {"web_search", "read_web_page", "read_repo_file", "latest_release", "find_github_repo"}


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


def choose(ollama_url, messages, chosen=None):
    """The model to use with Automatic on, and a short label like "Quick answers: a short question".
    Only models that can use PatchBay's tools are picked."""
    names = tool_names(ollama_url, installed(ollama_url))
    layers = layer_models(names, chosen)
    layer, why = which_layer(messages)
    model = layers[layer]
    if not model:
        return None, ""
    name = dict((k, n) for k, n, _ in LAYERS)[layer].split(". ", 1)[1]
    return model, "%s: %s, %s" % (name, why, local(model)[0])
