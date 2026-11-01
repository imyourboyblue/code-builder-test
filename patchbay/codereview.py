"""
🧐 Reviewing a whole code project: a GitHub repository, or a folder on this Mac.

Like deep research, the software does the legwork and the local AI only does the thinking:
  1. list every file (one request to GitHub, or a walk of the folder), and pick the ones worth reading (code, build
     files, docs), skipping pictures, downloaded libraries, lock files and generated files;
  2. read them in pieces that fit the model (small files together, big ones in parts with line numbers), and keep
     short notes for each piece: what it does, bugs, security, fragile code, UX;
  3. squeeze the notes per part of the project, then write one report: what the project is, how it's built, what's
     good, problems by priority with file and line, and the fixes to do first.
Nothing is sent anywhere except the requests that fetch the files. The report is saved under reviews/.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PIECE = 14000            # characters of code per AI step (fits a 32k-token context with room for the answer)
BUDGET = 1_400_000       # characters read in total; bigger projects are reviewed in part, and the report says so
MAX_FILE = 600_000       # bigger single files are almost always generated or data
NOTES_GROUP = 36000      # notes squeezed together per step

SKIP_DIRS = {".git", "node_modules", "vendor", "third_party", "third-party", "external", "dist", "build", "out",
             "__pycache__", ".venv", "venv", "env", "target", "Pods", "deps", ".idea", ".vscode", "coverage",
             ".next", ".cache", "site-packages", "bower_components", "data-cache", "prebuilt", "libs"}
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss", ".swift", ".c",
            ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh", ".go", ".rs", ".java", ".kt", ".kts", ".rb", ".php", ".sh",
            ".bash", ".zsh", ".m", ".mm", ".cs", ".lua", ".toml", ".yml", ".yaml", ".cmake", ".mk", ".gradle", ".sql",
            ".vue", ".svelte", ".dart", ".ex", ".exs", ".zig", ".nim", ".ps1", ".bat", ".ini", ".cfg", ".plist",
            ".s", ".asm", ".ld", ".json", ".md", ".rst", ".txt"}
BUILD_NAMES = {"makefile", "cmakelists.txt", "dockerfile", "package.json", "pyproject.toml", "setup.py",
               "cargo.toml", "go.mod", "requirements.txt", "build.gradle", "meson.build", "podfile",
               "package.swift", ".gitignore", "readme.md", "readme", "readme.rst", "license", "contributing.md"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "go.sum",
              "composer.lock", "gemfile.lock", "podfile.lock"}

PRIVATE_NAMES = {"config.json", "access.json", "claude.json", "claude-aliases.json", "credentials.json", ".env",
                 "id_rsa", "id_ed25519", ".netrc", "secrets.json", "secrets.yml"}

NOTES_PROMPT = """You are reviewing the code of a project, one piece at a time. Project: %s
%s
Below is %s. Lines are numbered.

Write short, dense notes (plain text, no introduction):
- WHAT: one or two lines on what this code does and how it fits the project.
- Then only concrete findings, each on its own line, starting with a tag and the file:line:
  BUG (wrong behaviour, crashes, race conditions, unhandled errors), SECURITY (injection, secrets, unsafe input,
  permissions), FRAGILE (hard-coded values, duplicated logic, very long functions, hidden coupling), UX (confusing
  messages or flows for the user), PERF (slow or wasteful), GOOD (something done notably well).
Only report what the code shows. If a piece looks fine, say so in one line. At most 15 findings.

%s"""

SQUEEZE_PROMPT = """These are review notes on part of the project %s (%s). Merge them into one shorter set of notes
(at most %d words): keep every BUG and SECURITY finding with its file:line, the most important FRAGILE, UX and PERF
ones, and a few GOOD ones. Drop duplicates. Start with 2-3 lines on what this part of the project does.

%s"""

REPORT_PROMPT = """Write a code review report of the project %s for its owner, who is not a programmer (explain
technical words briefly). %s

Use ONLY these notes (from reading %s):
%s

Markdown format:
# Review: %s
- **In short**: 4-6 plain sentences: what the project is, how healthy the code is, and the biggest risks.
- ## How it's built: its main parts and what each does (a short list).
- ## What's good
- ## Problems, most important first: subsections **Critical**, **High**, **Medium**, **Low**. Each item: what's wrong,
  where (file:line), why it matters, and how to fix it, in one or two sentences.
- ## Security
- ## Ease of use (for the person using it)
- ## Fix these first: a numbered list of the 5-10 most valuable fixes.
- ## Not reviewed: what was skipped (it's listed below the notes).
Don't invent problems that aren't in the notes."""


class ReviewError(Exception):
    pass


def _numbered(text, start=1):
    return "\n".join("%5d  %s" % (start + i, line) for i, line in enumerate(text.splitlines()))


def pick(files):
    """files: [(path, size)]. Returns (chosen [(path, size)], skipped [(path, why)])."""
    chosen, skipped = [], []
    for path, size in files:
        parts = path.split("/")
        name = parts[-1].lower()
        ext = os.path.splitext(name)[1]
        if any(p in SKIP_DIRS or (p.startswith(".") and p not in (".github",)) for p in parts[:-1]):
            continue
        if name in PRIVATE_NAMES or ext in (".pem", ".key", ".p12", ".keychain"):
            skipped.append((path, "may hold passwords or keys"))
        elif name in SKIP_NAMES or name.endswith((".min.js", ".min.css", ".map", ".pb.go", "_pb2.py")):
            skipped.append((path, "generated or lock file"))
        elif name not in BUILD_NAMES and ext not in CODE_EXT:
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".svg", ".webp", ".ttf", ".otf",
                           ".woff", ".woff2", ".mp3", ".wav", ".mp4", ".zip", ".gz"):
                skipped.append((path, "not code"))
        elif size > MAX_FILE:
            skipped.append((path, "too big (%d KB), probably generated" % (size // 1024)))
        elif ext in (".json", ".txt", ".md", ".rst") and name not in BUILD_NAMES and size > 40000:
            skipped.append((path, "large data or text file"))
        elif size == 0:
            continue
        else:
            chosen.append((path, size))

    def rank(f):    # build files and READMEs first, then code from the top of the project down
        p, size = f
        n = p.split("/")[-1].lower()
        return (0 if n in BUILD_NAMES else 2 if n.startswith("test") or "/test" in p else 1, p.count("/"), p)
    chosen.sort(key=rank)
    out, total = [], 0
    for p, size in chosen:
        if total + size > BUDGET:
            skipped.append((p, "over the size limit for one review"))
            continue
        out.append((p, size))
        total += size
    return out, skipped


class GitHubSource:
    def __init__(self, repo, token=None):
        m = re.search(r"(?:github\.com[/:])?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/|$)", (repo or "").strip())
        if not m:
            raise ReviewError("Give the repository as a GitHub link or owner/name.")
        self.repo = "%s/%s" % (m.group(1), m.group(2))
        self.name = self.repo
        self.token = token
        self.ref = "HEAD"

    def _get(self, url, api=False, limit=None):
        hdr = {"User-Agent": "patchbay/1.0"}
        if api:
            hdr["Accept"] = "application/vnd.github+json"
        if self.token:
            hdr["Authorization"] = "Bearer " + self.token
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
            return r.read(limit) if limit else r.read()

    def files(self):
        try:
            info = json.loads(self._get("https://api.github.com/repos/%s" % self.repo, api=True))
            self.ref = info.get("default_branch") or "HEAD"
            tree = json.loads(self._get("https://api.github.com/repos/%s/git/trees/%s?recursive=1"
                                        % (self.repo, urllib.parse.quote(self.ref)), api=True))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ReviewError("GitHub has no public repository %s (or it's private)." % self.repo)
            if e.code == 403:
                raise ReviewError("GitHub is limiting requests right now. Try again in an hour, or save a GitHub "
                                  "token in Setup → Updates & data.")
            raise ReviewError("GitHub said no: %s" % e)
        except (OSError, ValueError) as e:
            raise ReviewError("Couldn't reach GitHub: %s" % e)
        return [(t["path"], int(t.get("size") or 0)) for t in tree.get("tree") or [] if t.get("type") == "blob"]

    def read(self, path):
        url = "https://raw.githubusercontent.com/%s/%s/%s" % (self.repo, urllib.parse.quote(self.ref),
                                                             urllib.parse.quote(path))
        return self._get(url, limit=MAX_FILE + 1).decode("utf-8", "replace")


class FolderSource:
    def __init__(self, folder, name=None):
        self.root = Path(folder)
        self.name = name or self.root.name

    def files(self):
        out = []
        for root, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for f in sorted(files):
                p = Path(root) / f
                try:
                    out.append((str(p.relative_to(self.root)).replace(os.sep, "/"), p.stat().st_size))
                except OSError:
                    pass
        return out

    def read(self, path):
        data = (self.root / path).read_bytes()[:MAX_FILE + 1]
        if b"\0" in data[:4000]:
            return ""
        return data.decode("utf-8", "replace")


class Review:
    """ask(messages, think, stream, max_words) -> text; status(text); stop() raises if Stop was pressed; note(text).
    progress: a file where the notes are saved after every piece, so a review that was stopped or lost the AI
    carries on where it left off next time."""

    def __init__(self, source, focus, ask, status, stop, note, progress=None):
        self.src, self.focus, self.ask, self.status, self.stop, self.note = source, focus, ask, status, stop, note
        self.progress = Path(progress) if progress else None
        self.notes = []        # [(group, text)]
        self.read_files = []
        self.skipped = []
        self.readme = ""

    def _pieces(self, chosen):
        """Pieces of at most PIECE characters: [(label, group, text)], small files together."""
        batch, size, out = [], 0, []

        def flush():
            nonlocal batch, size
            if batch:
                label = "the files " + ", ".join(p for p, _ in batch) if len(batch) > 1 else "the file " + batch[0][0]
                body = "\n\n".join("=== %s ===\n%s" % (p, _numbered(t)) for p, t in batch)
                out.append((label, _group(batch[0][0]), body))
            batch, size = [], 0
        for path, _ in chosen:
            self.stop()
            try:
                text = self.src.read(path)
            except (OSError, ValueError, urllib.error.URLError) as e:
                self.skipped.append((path, "couldn't be read (%s)" % e))
                continue
            if not text.strip():
                continue
            self.read_files.append(path)
            if path.split("/")[-1].lower().startswith("readme") and not self.readme:
                self.readme = text[:3000]
            if len(text) <= PIECE:
                if size + len(text) > PIECE or (batch and _group(batch[0][0]) != _group(path)):
                    flush()
                batch.append((path, text))
                size += len(text)
                continue
            flush()
            lines, start, cur = text.splitlines(), 0, []
            n = 1 + len(text) // PIECE
            part = 0
            for i, line in enumerate(lines):
                cur.append(line)
                if sum(len(x) + 1 for x in cur) >= PIECE or i == len(lines) - 1:
                    part += 1
                    out.append(("part %d of about %d of the file %s (lines %d-%d)" % (part, n, path, start + 1, i + 1),
                                _group(path), "=== %s ===\n%s" % (path, _numbered("\n".join(cur), start + 1))))
                    start, cur = i + 1, []
        flush()
        return out

    def _squeeze(self, label, texts, words):
        """Merge notes until they fit in one step."""
        while len(texts) > 1 or (texts and len(texts[0]) > NOTES_GROUP):
            self.stop()
            groups, cur = [], ""
            for t in texts:
                if cur and len(cur) + len(t) > NOTES_GROUP:
                    groups.append(cur)
                    cur = ""
                cur += t + "\n\n"
            groups.append(cur)
            if len(groups) == 1 and len(texts) == 1:
                groups = [texts[0][:NOTES_GROUP]]
            texts = [self.ask([{"role": "user", "content": SQUEEZE_PROMPT % (self.src.name, label, words, g)}],
                              think=False, max_words=words + 300) for g in groups]
            if len(groups) == 1:
                break
        return texts[0] if texts else ""

    def run(self):
        started = time.time()
        self.status("Listing the files of %s..." % self.src.name)
        chosen, self.skipped = pick(self.src.files())
        if not chosen:
            raise ReviewError("There's no code to review in %s." % self.src.name)
        self.note("**Reviewing %s:** %d files (%d KB of code)%s. This takes a while; you can press Stop any time."
                  % (self.src.name, len(chosen), sum(s for _, s in chosen) // 1024,
                     ", skipping %d (pictures, generated and library files)" % len(self.skipped)
                     if self.skipped else ""))
        self.status("Reading the files...")
        pieces = self._pieces(chosen)
        about = ("What the README says (it may be out of date):\n" + self.readme[:2500]) if self.readme else ""
        if self.focus:
            about += "\nThe owner especially wants to know about: %s" % self.focus
        saved = self._load()
        if saved:
            self.note("Carrying on from the last time: %d of %d parts were already reviewed."
                      % (sum(1 for _, _, b in pieces if _key(b) in saved), len(pieces)))
        for i, (label, group, body) in enumerate(pieces, 1):
            self.stop()
            key = _key(body)
            if key in saved:
                self.notes.append((group, saved[key]))
                continue
            self.status("Reviewing %d of %d: %s" % (i, len(pieces), label[:90]))
            try:
                text = self.ask([{"role": "user", "content": NOTES_PROMPT % (self.src.name, about, label, body)}],
                                think=False, max_words=700)
            except Exception as e:
                if type(e).__name__ == "Stopped":
                    raise
                done = len(saved)
                raise ReviewError("%s\nThe review stopped at part %d of %d. The notes on the %d parts done so far are "
                                  "saved: run the same review again and it carries on from there." % (
                                      e, i, len(pieces), done))
            saved[key] = "[%s]\n%s" % (label, (text or "").strip())
            self.notes.append((group, saved[key]))
            self._save(saved)
        # Squeeze the notes per part of the project, then all together if still too long.
        groups = {}
        for g, t in self.notes:
            groups.setdefault(g, []).append(t)
        parts = []
        for g, texts in groups.items():
            total = sum(len(t) for t in texts)
            if total > NOTES_GROUP // max(1, min(len(groups), 4)):
                self.status("Summing up the notes on %s..." % g)
                parts.append("## %s\n%s" % (g, self._squeeze(g, texts, 600)))
            else:
                parts.append("## %s\n%s" % (g, "\n\n".join(texts)))
        allnotes = "\n\n".join(parts)
        if len(allnotes) > NOTES_GROUP * 3 // 2:
            self.status("Summing up all the notes...")
            allnotes = self._squeeze("the whole project", parts, 1800)
        self.status("Writing the review...")
        skipped = "\n".join("- %s: %s" % (p, why) for p, why in self.skipped[:60]) + (
            "\n- ... and %d more" % (len(self.skipped) - 60) if len(self.skipped) > 60 else "")
        report = self.ask([{"role": "user", "content": REPORT_PROMPT % (
            self.src.name, ("The owner especially wants to know about: %s" % self.focus) if self.focus else "",
            "%d files" % len(self.read_files), allnotes + "\n\nNot reviewed:\n" + (skipped or "(nothing)"),
            self.src.name)}], think=True, stream=False, max_words=2500)
        report = (report or "").strip()
        if self.progress:
            try:
                self.progress.unlink()
            except OSError:
                pass
        report += "\n\n---\n*Reviewed %d files in %d min.*\n" % (len(self.read_files), (time.time() - started) // 60)
        return report


    def _load(self):
        if not self.progress:
            return {}
        try:
            data = json.loads(self.progress.read_text())
            return data.get("notes") or {} if data.get("name") == self.src.name else {}
        except (OSError, ValueError):
            return {}

    def _save(self, saved):
        if not self.progress:
            return
        try:
            self.progress.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.progress.with_name(self.progress.name + ".tmp")
            tmp.write_text(json.dumps({"name": self.src.name, "time": time.time(), "notes": saved}))
            tmp.replace(self.progress)
        except OSError:
            pass


def _key(body):
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]


def progress_file(folder, name):
    return Path(folder) / ".unfinished" / (re.sub(r"[^\w.-]", "-", name)[:80] + ".json")


def _group(path):
    """Which part of the project a file belongs to: its top folder (or two levels for one-folder projects)."""
    parts = path.split("/")
    if len(parts) == 1:
        return "(top folder)"
    if len(parts) > 2 and parts[0] in ("src", "lib", "app", "source", "Sources", "packages"):
        return "/".join(parts[:2])
    return parts[0] + ("/" + parts[1] if len(parts) > 2 and parts[1] in ("web", "src", "lib") else "")


def save(folder, name, report):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%s %s.md" % (time.strftime("%Y-%m-%d %H%M"), re.sub(r"[^\w .-]", "-", name)[:60]))
    path.write_text(report, encoding="utf-8")
    return path
