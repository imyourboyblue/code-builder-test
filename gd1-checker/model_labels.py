"""
What the AI model menus show: what each model is good for, instead of its name and version.

Edit the lists below to change the words. The first matching rule wins. A rule matches if its text appears
anywhere in the model's name (for example "27b" matches "qwen3.8:27b"). Models no rule matches get a label
from their size.
"""

# Models on your Mac (Ollama). (text in the model name, menu label, one-line description)
LOCAL = [
    ("35b-a3b", "Everyday help", "Fast and smart. Good for most questions, checks and fixes."),
    ("27b", "Hard problems and building", "Slower, but thinks harder. Use it for tricky crashes and building apps."),
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
