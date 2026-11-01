"""
🔎 Deep research: PatchBay researches a question on the web by itself and writes a report with sources.

The software does the legwork (splitting the question up, searching, reading pages, keeping track of sources); the
local AI only does the thinking (planning, taking notes, writing). Everything runs on this Mac with the Everyday or
Building model. Only search words leave the Mac, and personal details (device names, addresses, your user name...)
are taken out of them first.

Steps: plan (3-5 smaller questions and search words) → search and read pages for each → notes per question, with
numbered sources → one follow-up round for what's still missing → the report. It's saved in the data folder
under research/.
"""

import concurrent.futures
import json
import re
import time
import urllib.parse
from pathlib import Path

PAGES_PER_QUESTION = 4
PAGE_CHARS = 7000           # of each page, so the notes step fits the model's memory
MAX_QUESTIONS = 5

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "questions": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "searches": {"type": "array", "items": {"type": "string"}}},
            "required": ["question", "searches"]}},
    },
    "required": ["title", "questions"],
}
GAPS_SCHEMA = {
    "type": "object",
    "properties": {"missing": {"type": "array", "items": {
        "type": "object",
        "properties": {"question": {"type": "string"},
                       "searches": {"type": "array", "items": {"type": "string"}}},
        "required": ["question", "searches"]}}},
    "required": ["missing"],
}


class Research:
    """One research job. ask(messages, think, fmt, stream) -> text; search(q) -> text; page(url) -> text;
    scrub(text) -> text without personal details; status(text) shows progress; stop() raises if Stop was pressed."""

    def __init__(self, question, ask, search, page, scrub, status, stop, note):
        self.question, self.ask, self.search, self.page = question, ask, search, page
        self.scrub, self.status, self.stop, self.note = scrub, status, stop, note
        self.sources = []        # [{"n", "url", "title"}]
        self.notes = []          # [(question, notes text)]
        self.searched = set()

    # ---- small helpers

    def _json(self, prompt, schema):
        text = self.ask([{"role": "user", "content": prompt}], think=False, fmt=schema)
        try:
            return json.loads(text)
        except ValueError:
            m = re.search(r"\{.*\}", text or "", re.S)
            return json.loads(m.group(0)) if m else {}

    def _safe_query(self, q):
        """Search words only: personal details are swapped out by the scrubber, then the stand-ins removed."""
        q = self.scrub(str(q or ""))[:150]
        q = re.sub(r"<[^<>]{1,40}>", " ", q)
        return re.sub(r"\s+", " ", q).strip()

    def _source(self, url, title):
        for s in self.sources:
            if s["url"] == url:
                return s["n"]
        self.sources.append({"n": len(self.sources) + 1, "url": url, "title": title or url})
        return len(self.sources)

    # ---- steps

    def plan(self):
        self.status("Planning the research...")
        p = self._json(
            "Plan web research for this request. Split it into 3 to %d smaller questions that together answer it, "
            "and give 2 short web search queries for each (plain search words, no personal names, addresses or file "
            "paths). Also give a short title (3-6 words).\n\nRequest: %s" % (MAX_QUESTIONS, self.question),
            PLAN_SCHEMA)
        qs = [q for q in (p.get("questions") or []) if q.get("question")][:MAX_QUESTIONS]
        if not qs:
            qs = [{"question": self.question, "searches": [self.question]}]
        return (p.get("title") or self.question[:60]).strip(), qs

    def find_pages(self, searches):
        """Search, then pick the first few new addresses."""
        urls = []
        for q in searches[:3]:
            q = self._safe_query(q)
            if not q or q.lower() in self.searched:
                continue
            self.searched.add(q.lower())
            self.stop()
            self.status("Searching: %s" % q)
            try:
                found = self.search(q)
            except Exception:
                continue
            for title, url in re.findall(r"(?m)^(.+)\n  (https?://\S+)", found or ""):
                host = urllib.parse.urlparse(url).netloc
                if url.startswith("https://") and url not in [u for u, _ in urls] and \
                        not any(x in host for x in ("duckduckgo.", "youtube.", "facebook.", "x.com", "twitter.")):
                    urls.append((url, title.strip()))
        return urls

    def read_pages(self, urls):
        """Read pages in parallel. Returns [(source number, title, text)]."""
        out = []

        def get(u):
            try:
                return u, self.page(u[0])
            except Exception:
                return u, ""
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            for (url, title), text in pool.map(get, urls[:PAGES_PER_QUESTION * 2]):
                if len(text or "") > 400 and len(out) < PAGES_PER_QUESTION:
                    out.append((self._source(url, title), title, text[:PAGE_CHARS]))
        return out

    def take_notes(self, question, pages):
        if not pages:
            return "Nothing useful was found on the web for this."
        self.status("Taking notes: %s" % question[:80])
        body = "\n\n".join("[%d] %s\n%s" % (n, t, text) for n, t, text in pages)
        return self.ask([{"role": "user", "content":
                          "Question: %s\n\nSources:\n%s\n\nWrite dense notes that answer the question using ONLY these "
                          "sources. Put the source number in brackets after each fact, like [2]. Include numbers, "
                          "dates and versions. Say plainly what the sources don't answer or disagree on. "
                          "No introduction." % (question, body)}], think=False)

    def research(self, qs):
        for i, q in enumerate(qs, 1):
            self.stop()
            self.status("Researching %d of %d: %s" % (i, len(qs), q["question"][:80]))
            pages = self.read_pages(self.find_pages(q.get("searches") or [q["question"]]))
            self.notes.append((q["question"], self.take_notes(q["question"], pages)))

    def gaps(self):
        self.status("Checking what's still missing...")
        g = self._json("Request: %s\n\nNotes so far:\n%s\n\nWhat important parts of the request are still unanswered? "
                       "Give at most 2 follow-up questions with 2 search queries each, or an empty list if the notes "
                       "are enough." % (self.question, self._all_notes(4000)), GAPS_SCHEMA)
        return [m for m in (g.get("missing") or []) if m.get("question")][:2]

    def _all_notes(self, per=None):
        return "\n\n".join("## %s\n%s" % (q, n[:per] if per else n) for q, n in self.notes)

    def write(self, title):
        self.status("Writing the report...")
        sources = "\n".join("[%d] %s - %s" % (s["n"], s["title"], s["url"]) for s in self.sources)
        report = self.ask([{"role": "user", "content":
                            "Write a research report that answers this request: %s\n\nUse ONLY these notes (the "
                            "numbers in brackets are sources):\n%s\n\nFormat (Markdown):\n- First line: '# %s'\n"
                            "- A short plain-language answer first (3-6 sentences), for someone who isn't an expert.\n"
                            "- Then sections with headings, keeping the [n] source numbers after facts.\n"
                            "- Then 'What's still unclear' (gaps or disagreements).\n"
                            "Don't add a sources list (it's added for you). Don't invent facts."
                            % (self.question, self._all_notes(), title)}], think=True, stream=True)
        report = re.sub(r"(?is)\n#+\s*sources\b.*$", "", (report or "").strip())
        return report + "\n\n## Sources\n\n" + "\n".join("%d. [%s](%s)" % (s["n"], s["title"], s["url"])
                                                           for s in self.sources) + "\n"

    def run(self):
        started = time.time()
        title, qs = self.plan()
        self.note("**Researching:** %s\n\n%s" % (title, "\n".join("- " + q["question"] for q in qs)))
        self.research(qs)
        more = self.gaps()
        if more:
            self.note("**Looking further into:**\n\n" + "\n".join("- " + q["question"] for q in more))
            self.research(more)
        report = self.write(title)
        return title, report, time.time() - started


def save(folder, title, question, report):
    """Save the report as Markdown. Returns its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w -]", "", title).strip()[:60] or "Research"
    path = folder / ("%s %s.md" % (time.strftime("%Y-%m-%d %H%M"), name))
    path.write_text("<!-- Asked: %s -->\n\n%s" % (question.replace("--", "-"), report), encoding="utf-8")
    return path
