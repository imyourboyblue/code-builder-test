"""What PatchBay itself saw happen since the user's last question, and a check that the AI's words don't claim more.

The local AI once said "The build completed and the file was copied to /user/EglantinePS5.self on your PS5" after a
run where the build kept failing and nothing was sent anywhere. Its words are only allowed to say a build worked, a
file was copied out of a sandbox, or something was installed on a device when PatchBay's own code saw that happen:
- built:     a build step exited 0 AND its output file is there (checked in the sandbox or the project folder),
- copied:    a sandbox copy-out card was confirmed and the copy worked,
- installed: a device write was confirmed and verified (or a confirmed Mac command that sends to a device exited 0).
Sentences that claim more are taken out and replaced with what really happened.
"""

import re
import time

KINDS = ("built", "copied", "installed")


def record(st, kind, what):
    """Remember that PatchBay saw this happen (kind is one of KINDS)."""
    st.__dict__.setdefault("proof", []).append({"kind": kind, "what": str(what)[:200], "time": time.time()})


def question_started(st):
    """A new question from the user: only what happens from now on backs up claims in the answer to it."""
    st.question_at = time.time()


OLDER = 6 * 3600       # proof from an earlier question still counts this long, but only for the same thing


COMMON = {"build", "built", "builds", "file", "files", "sandbox", "device", "project", "with", "from", "that", "this",
          "your", "into", "onto", "copied", "installed", "output", "release", "linux", "worked", "made", "were", "it's"}


def names(text):
    return {w.lower().strip(".") for w in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.-]{3,}", text or "")} - COMMON


def seen(st, kind, sentence=""):
    """What PatchBay saw of this kind since the user's question. Older proof (from earlier questions in this chat)
    only counts when the sentence names the same thing (a project or file name in it)."""
    since = getattr(st, "question_at", 0) or 0
    words = names(sentence)
    out = []
    for p in getattr(st, "proof", None) or []:
        if p["kind"] != kind:
            continue
        if p["time"] >= since - 1:
            out.append(p)
        elif time.time() - p["time"] < OLDER and words & names(p["what"]):
            out.append(p)
    return out


# What a claim sounds like. Kept to plain statements of fact in the past or present ("was copied", "I installed").
DEVICE_WORDS = (r"(?:\b(?:ps[345]|switch|console|device|xbox|steam ?deck|roku|router|printer|tv|phone|homebrew menu|"
                r"your (?!mac\b|sandbox\b)\w+)\b|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|(?<![\w/.~])/[a-z0-9_])")
CLAIMS = {
    "installed": re.compile(
        r"\b(installed|uploaded|sent|transferred|copied|placed|deployed|put)\b[^!?\n]{0,80}?\b(on|onto|to|into)\b"
        r"[^!?\n]{0,60}?" + DEVICE_WORDS + r"|"
        r"\b(is|are)\s+now\s+(on|installed|in place)\b|"
        r"\byou\s+can\s+now\s+(launch|open|run|start|find|play|use)\b|"
        r"\b(install(ation)?|upload|transfer)\s+(is\s+)?(complete[d]?|done|finished|succeeded|worked|was successful)",
        re.I),
    "built": re.compile(
        r"\bbuild\s+(has\s+|now\s+)?(completed|succeeded|finished|worked|passed|was\s+successful|is\s+done)|"
        r"\b(built|compiled)\b[^!?\n]{0,30}?\bsuccessfully|\bsuccessfully\s+(built|compiled)|"
        r"\bthe\s+build\s+(now\s+)?works\b|\bbuild\s+(is\s+)?(ok|fine|green)\b", re.I),
    "copied": re.compile(
        r"\bcopied\b[^!?\n]{0,60}?\bout\s+of\s+the\s+sandbox|\bcopy[- ]out\s+(worked|succeeded|is\s+done|finished)",
        re.I),
}
# A sentence that is a question, a plan, a condition or a "didn't" is not a claim that it happened.
NOT_A_CLAIM = re.compile(
    r"\?|\b(not|never|no|n't|didn't|couldn't|haven't|hasn't|wasn't|weren't|isn't|aren't|won't|cannot|can't|unable|"
    r"if|once|after|when|until|before|unless|whether|will|would|should|could|might|may|need|needs|next|then|"
    r"let me|i'll|want|try|trying|check|confirm|verify|still|yet|instead|failed|fails|error)\b", re.I)
# Installing a program INSIDE the sandbox or on the Mac is not installing on a device.
NOT_DEVICE = re.compile(r"\b(sandbox|apt-get|apt|brew|homebrew package|pip|package|packages|on (the|this|your) mac)\b",
                        re.I)
# A sentence ends at . ! or ? followed by a space or the end, so "192.168.50.203" and "app.self" stay whole.
SENTENCE = re.compile(r"(?:[^\n.!?]|[.!?](?![\s*_)]|$))+(?:[.!?]+)?[*_)]*[ \t]*\n?|\n")


def claims(sentence):
    """Which kinds of success this sentence states as done."""
    s = sentence.strip()
    if not s or NOT_A_CLAIM.search(s):
        return []
    found = []
    for kind, rx in CLAIMS.items():
        if rx.search(s) and not (kind == "installed" and NOT_DEVICE.search(s)):
            found.append(kind)
    return found


def unproven(text, st):
    """[(sentence, kind)] for each claim in the text that PatchBay didn't see happen in this question."""
    out = []
    for m in SENTENCE.finditer(text or ""):
        for kind in claims(m.group(0)):
            if not seen(st, kind, m.group(0)):
                out.append((m.group(0).strip(), kind))
    return out


FACTS = {"built": "no build has finished with its output file made",
         "copied": "nothing was copied out of the sandbox",
         "installed": "nothing was installed or copied to your device"}


def facts_line(st, kinds=KINDS):
    """One plain sentence of what did NOT happen (only the kinds asked about, and only those PatchBay didn't see)."""
    missing = [FACTS[k] for k in KINDS if k in kinds and not seen(st, k)]
    if not missing:
        return ""
    words = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + ", and " + missing[-1]
    return words[0].upper() + words[1:] + "."


def correct(text, st):
    """The text with every unproven success claim taken out and a plain correction in front.
    Returns (text, list of the kinds it had to correct)."""
    bad = unproven(text, st)
    if not bad:
        return text, []
    kinds = sorted({k for _, k in bad}, key=KINDS.index)
    drop = {s for s, _ in bad}
    kept = "".join(m.group(0) for m in SENTENCE.finditer(text or "") if m.group(0).strip() not in drop)
    kept = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", kept)).strip()
    head = ("**Correction from PatchBay:** the AI said something that didn't happen, so I took it out. "
            + facts_line(st, kinds))
    return (head + ("\n\n" + kept if kept else "")), kinds
