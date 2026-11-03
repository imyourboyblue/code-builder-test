"""Long-chat memory: keeps long chats sharp without filling the AI's memory.

Short chats are sent to the AI whole, as before. Once a chat gets too long, older messages move out of the prompt
into a memory file next to the saved chat:
- Notes: every 10 or so messages, the AI on this Mac writes short notes of the specific facts said (names, numbers,
  dates, versions), each tagged with the messages it came from, like "Router admin port is 8443 [t42]".
- Search: for each new question, a keyword search finds the older messages that matter and puts them back, word for
  word, just before the question. Notes point the search at the right messages.

Tested on a 36GB Mac with Qwen 3.6 35B: about 54 of 60 questions right on two long test chats, against 17 of 30 for
keeping as much of the chat as fits. The prompt order (notes first, a recent block that only grows, the question
last) lets Ollama reuse most of its work from one message to the next.

Notes are written by the AI on this Mac only, never by Claude, and they're written after an answer so you never
wait for them. Python standard library only.
"""

import json
import math
import re
import threading
import urllib.request
from pathlib import Path

FOLD_EVERY = 10          # messages moved out of the prompt at a time
RECENT_SHARE = 0.45      # the recent block may grow to this share of the room before older messages move out
RECENT_KEEP = 0.2        # ...and is cut back to about this share
EXCERPT_SHARE = 0.25     # room for search results
NOTE_SHARE = 0.15        # room for notes
TOOL_TEXT = 1500         # characters of a step's result kept for search
TOP_TURNS = 5
TOP_NOTES = 3
EXTRA_TURNS = 3

WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*")
STOP = set("""a an and are as at be but by can could did do does for from had has have how i if in into is it its
me my of on or our so than that the their them then there these they this to was we were what when where which who
why will with would you your about after again all also any been before being both just more most no not now only
other out over same should some such too up very want well yes ok okay please thanks""".split())

NOTE_PROMPT = """You keep notes on a long chat so details can be found again later.
Below are messages from the chat, each starting with its id like [t12].
Write one short bullet per fact. Record every specific value someone stated: names, numbers, prices, dates, codes,
versions, file paths, device names, settings, decisions and preferences. If a value replaces an earlier one, write
UPDATED: old -> new. End each bullet with the ids it came from, like [t12] or [t12,t15].
Skip greetings and general advice. Write only the bullets.

Notes so far (don't repeat these):
%s

Messages:
%s"""


def words(text):
    return [w.lower().strip(".:/-") for w in WORD.findall(text or "") if w.lower() not in STOP and len(w) > 1]


def bm25(query, docs, k1=1.4, b=0.75):
    """Scores for each doc (a list of words) against the query words."""
    if not docs or not query:
        return [0.0] * len(docs)
    n, avg = len(docs), sum(len(d) for d in docs) / len(docs) or 1
    df = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1
    out = []
    for d in docs:
        tf, s = {}, 0.0
        for w in d:
            tf[w] = tf.get(w, 0) + 1
        for w in set(query):
            if w in tf:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(d) / avg))
        out.append(s)
    return out


def message_text(m):
    """What a message says, for notes and search."""
    c = m.get("content")
    if not isinstance(c, str):
        c = json.dumps(c) if c else ""
    if m.get("role") == "tool":
        c = "(result of %s) %s" % (m.get("tool_name") or "a step", c[:TOOL_TEXT])
    calls = m.get("tool_calls") or []
    if calls:
        c += " " + " ".join("(ran %s %s)" % ((x.get("function") or {}).get("name"),
                                              json.dumps((x.get("function") or {}).get("arguments"))[:200])
                            for x in calls)
    return c.strip()


def fingerprint(m):
    return "%s:%d:%s" % (m.get("role"), len(str(m.get("content") or "")), str(m.get("content") or "")[:80])


def is_question(m):
    return m.get("role") == "user" and not str(m.get("content") or "").startswith("[")


def size(messages):
    return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages)


class ChatMemory:
    """The memory file of one chat: older messages (kept whole, with ids) and notes about them."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.turns, self.notes, self.cut, self.noted, self.mark = [], [], 0, 0, ""
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.turns, self.notes = d.get("turns", []), d.get("notes", [])
                self.cut, self.noted = int(d.get("cut", 0)), int(d.get("noted", 0))
                self.mark = d.get("mark", "")
            except (OSError, ValueError):
                pass

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"cut": self.cut, "mark": self.mark, "noted": self.noted, "turns": self.turns,
                                   "notes": self.notes}))
        tmp.replace(self.path)

    # ---- moving older messages out of the prompt

    def move_out(self, messages, upto):
        """Messages[cut:upto] leave the prompt and go into the memory file."""
        for m in messages[self.cut:upto]:
            text = message_text(m)
            if text:
                self.turns.append({"id": len(self.turns) + 1, "role": m.get("role"), "text": text})
        self.cut = upto
        self.mark = fingerprint(messages[upto]) if upto < len(messages) else ""

    def find_cut(self, messages):
        """Where the recent block starts. A saved chat keeps only its newest messages, so the place is found
        again by the message that starts it."""
        if not self.mark:
            return min(self.cut, len(messages))
        if self.cut < len(messages) and fingerprint(messages[self.cut]) == self.mark:
            return self.cut
        for i in range(min(self.cut, len(messages) - 1), -1, -1):
            if fingerprint(messages[i]) == self.mark:
                return i
        return 0

    def notes_text(self, room):
        lines, used = [], 0
        for n in reversed(self.notes):          # newest notes win when there's too much
            if used + len(n) > room:
                break
            lines.append(n)
            used += len(n) + 1
        return "\n".join(reversed(lines))

    # ---- search

    def search(self, question, room):
        """Older messages that matter for this question, word for word, oldest first."""
        if not self.turns:
            return []
        q = words(question)
        docs = [words(t["text"]) for t in self.turns]
        scores = bm25(q, docs)
        picked = [i for i, s in sorted(enumerate(scores), key=lambda x: -x[1])[:TOP_TURNS] if s > 0]
        # Notes point at the messages they came from: a note that matches pulls its messages in too.
        nscores = bm25(q, [words(n) for n in self.notes])
        for i, s in sorted(enumerate(nscores), key=lambda x: -x[1])[:TOP_NOTES]:
            if s <= 0:
                continue
            for tid in re.findall(r"t(\d+)", (re.findall(r"\[(t[\dt, ]+)\]", self.notes[i]) or [""])[-1]):
                j = int(tid) - 1
                if 0 <= j < len(self.turns) and j not in picked:
                    picked.append(j)
        # A second pass with the names, codes and numbers found so far, for questions that join two facts.
        found = set()
        for j in picked:
            found |= {w.lower() for w in re.findall(r"\b(?:[A-Z][\w-]{2,}|[A-Z0-9]+-\d+|\d[\d.,:/-]*)\b",
                                                    self.turns[j]["text"])}
        found -= set(q)
        if found:
            extra = bm25(list(found), docs)
            for j, s in sorted(enumerate(extra), key=lambda x: -x[1]):
                if s <= 0 or len(picked) >= TOP_TURNS + TOP_NOTES + EXTRA_TURNS:
                    break
                if j not in picked:
                    picked.append(j)
        out, used = [], 0
        for j in picked:                           # best first until the room is used
            t = self.turns[j]
            line = "[t%d] %s: %s" % (t["id"], t["role"], t["text"][:4000])
            if used + len(line) > room:
                continue
            out.append((t["id"], line))
            used += len(line)
        return [line for _, line in sorted(out)]

    # ---- notes, written after an answer

    def pending_notes(self):
        return len(self.turns) - self.noted

    def write_notes(self, ask):
        """ask(prompt) -> the AI's text. Notes the messages that have no notes yet."""
        with self.lock:
            todo = self.turns[self.noted:]
            if not todo:
                return
            batch = "\n".join("[t%d] %s: %s" % (t["id"], t["role"], t["text"][:2000]) for t in todo)
            text = ask(NOTE_PROMPT % (self.notes_text(3000) or "(none yet)", batch[:24000]))
            new = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip() for line in (text or "").splitlines()]
            self.notes += [n for n in new if n and re.search(r"\[t\d", n)]
            self.noted = len(self.turns)
            self.save()


def memory_for(st):
    """The memory of the chat st has open (made the first time it's needed)."""
    if not st.chat_id:
        import core
        st.chat_id = core.now_stamp()
    m = getattr(st, "memory", None)
    if m is None or m.path.name != st.chat_id + ".memory":
        # Not .json, so it isn't listed as a chat of its own.
        m = ChatMemory(st.chats_dir() / (st.chat_id + ".memory"))
        st.memory = m
    return m


def view(st, messages, budget, compact, excerpts=True):
    """What the AI is sent instead of the whole chat. compact(messages, budget) shortens long step results, as
    before. Returns the messages to send (without the system message). excerpts=False leaves out the search
    results, for warming up the start of the next prompt."""
    has_memory = bool(st.chat_id) and (st.chats_dir() / (st.chat_id + ".memory")).exists()
    if size(messages) <= budget and not has_memory:
        return compact(messages, budget)
    mem = memory_for(st)
    with mem.lock:
        mem.cut = mem.find_cut(messages)
        recent = messages[mem.cut:]
        if size(recent) > budget * RECENT_SHARE:
            # Move older messages out, in one step, at the start of a question, so the recent block then only grows
            # for a while (and Ollama can reuse its work).
            want = budget * RECENT_KEEP
            starts = [i for i in range(mem.cut + 1, len(messages)) if is_question(messages[i])]
            upto = mem.cut
            for i in starts:
                if size(messages[i:]) <= want:
                    upto = i
                    break
            else:
                upto = starts[-1] if starts else mem.cut
            if upto > mem.cut:
                mem.move_out(messages, upto)
                mem.save()
            recent = messages[mem.cut:]
        question = next((str(m.get("content")) for m in reversed(recent) if is_question(m)), "")
        found = mem.search(question, int(budget * EXCERPT_SHARE)) if excerpts else []
        notes = mem.notes_text(int(budget * NOTE_SHARE))
    out = []
    if notes:
        out.append({"role": "user", "content": "[Notes on the earlier part of this chat. Each ends with the ids of "
                                               "the messages it came from.]\n" + notes})
        out.append({"role": "assistant", "content": "Noted."})
    recent = compact(recent, max(4000, budget - size(out) - sum(len(f) for f in found)))
    if found:
        # Just before the question, so everything before it stays the same from one message to the next.
        last_q = max((i for i, m in enumerate(recent) if is_question(m)), default=len(recent))
        excerpt = {"role": "user", "content": "[Messages from earlier in this chat that may matter for the next "
                                              "question, word for word. Newer messages win if they disagree.]\n"
                                              + "\n".join(found)}
        recent = recent[:last_q] + [excerpt, {"role": "assistant", "content": "OK."}] + recent[last_q:]
    return out + recent


def after_answer(st, ask, then=None):
    """In the background, so the next question doesn't wait: write notes for messages that moved out, then run
    then() (warming up the next prompt). Notes come first, because on some engines any other request to the model
    replaces the prompt it has kept."""
    mem = getattr(st, "memory", None)
    due = bool(mem and mem.pending_notes() and not mem.lock.locked()
               and (mem.pending_notes() >= FOLD_EVERY or not mem.noted))
    if not due and not then:
        return

    def run():
        if due:
            try:
                mem.write_notes(ask)
            except Exception as e:      # notes are a bonus: the search still works without them
                print("Memory notes failed: %s" % e)
        if then:
            try:
                then()
            except Exception as e:
                print("Warm-up failed: %s" % e)
    threading.Thread(target=run, daemon=True).start()


def strip_thinking(text):
    """Drop reasoning a model wrote into its answer anyway (some packagings ignore think: false)."""
    text = text or ""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.replace("<think>", "").strip()


def ollama_ask(url, model, num_ctx, timeout=600):
    """A plain question to the AI on this Mac (never Claude), for writing notes. Same context size as the chat,
    or Ollama reloads the model."""
    def ask(prompt):
        body = {"model": model, "stream": False, "keep_alive": -1, "think": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0, "num_ctx": int(num_ctx)}}
        req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return strip_thinking((json.loads(r.read()).get("message") or {}).get("content", ""))
    return ask
