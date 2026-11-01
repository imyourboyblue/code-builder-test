"""
Helpers that make the local AI better at changing code:

- apply_edits(): small patches (several find/replace pairs, or a range of line numbers) instead of whole-file
  rewrites. Forgiving about what local models get wrong: line-number prefixes copied from a read, and different
  indentation or spacing at the ends of lines.
- quick_check(): a fast check that a changed file still parses (Python, JSON, JavaScript, HTML scripts, shell),
  run before the card is even shown and again after saving.
- error_context(): turns a build's error output into the error lines plus the source code around each one, so the
  AI sees the real problem instead of guessing.
- project_map(): a short map of a project (files, their functions and classes, what each file uses), so the AI can
  open the right file instead of reading everything.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "build", "dist", "out", "__pycache__", ".venv", "venv", "target", "data-cache",
             "Pods", ".build", "DerivedData"}
CODE_EXT = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".swift", ".c", ".h", ".cpp", ".hpp",
            ".cc", ".cxx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".sh", ".m", ".mm", ".cs", ".lua", ".s",
            ".cmake", ".mk", ".json", ".toml", ".yml", ".yaml", ".md"}
BIG_REWRITE_LINES = 150      # existing files longer than this must be changed with edits, not rewritten


class EditError(Exception):
    pass


# ---------------------------------------------------------------- small patches

_PREFIX = re.compile(r"^\s*\d+\s*[|:]\s?", re.M)     # "  42| " copied from project_read / patchbay_read


def _strip_prefixes(text):
    lines = text.splitlines(True)
    if lines and all(_PREFIX.match(l) or not l.strip() for l in lines) and any(_PREFIX.match(l) for l in lines):
        return "".join(_PREFIX.sub("", l, count=1) for l in lines)
    return text


def _loose_find(old, find):
    """Where find matches old if spacing at the start and end of each line is ignored: (start, end) or None."""
    want = [l.strip() for l in find.strip("\n").splitlines()]
    if not want or not any(want):
        return None
    lines = old.splitlines(True)
    stripped = [l.strip() for l in lines]
    hits = []
    for i in range(len(lines) - len(want) + 1):
        if stripped[i:i + len(want)] == want:
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    start = sum(len(l) for l in lines[:i])
    end = start + sum(len(l) for l in lines[i:i + len(want)])
    return start, end, len(hits)


def _reindent(replace, old_block):
    """Give replacement lines the indentation of the block they replace (models often drop it)."""
    first = next((l for l in old_block.splitlines() if l.strip()), "")
    indent = first[:len(first) - len(first.lstrip())]
    rep_first = next((l for l in replace.splitlines() if l.strip()), "")
    if not indent or rep_first.startswith(indent):
        return replace
    rep_indent = rep_first[:len(rep_first) - len(rep_first.lstrip())]
    out = []
    for l in replace.splitlines(True):
        out.append(indent + l[len(rep_indent):] if l.startswith(rep_indent) and l.strip() else l)
    return "".join(out)


def _nearest(old, find):
    """A hint: the lines that look most like the first line of find."""
    key = next((l.strip() for l in find.splitlines() if l.strip()), "")[:60]
    if not key:
        return ""
    words = set(re.findall(r"\w{3,}", key))
    best = []
    for n, line in enumerate(old.splitlines(), 1):
        score = len(words & set(re.findall(r"\w{3,}", line)))
        if score:
            best.append((score, n, line.strip()[:160]))
    best.sort(key=lambda x: (-x[0], x[1]))
    return "\n".join("  line %d: %s" % (n, l) for _, n, l in best[:3])


def apply_edits(old, edits):
    """Apply a list of small changes to a file's text. Each edit is {"find", "replace"} (find must match exactly
    once) or {"start_line", "end_line", "replace"} (replace those lines, counted in the file as it is now; line edits
    are applied from the bottom up so earlier numbers stay right). Returns the new text, or raises EditError."""
    if not edits:
        raise EditError("Give at least one edit.")
    text = old
    line_edits = []
    for n, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            raise EditError("Edit %d isn't an object with find/replace." % n)
        rep = e.get("replace")
        rep = "" if rep is None else str(rep)
        if e.get("start_line") not in (None, ""):
            line_edits.append((n, e))
            continue
        find = e.get("find")
        if not find:
            raise EditError("Edit %d has no find text (or start_line/end_line)." % n)
        find = str(find)
        count = text.count(find)
        if count == 0:
            clean = _strip_prefixes(find)
            if clean != find:
                find, rep = clean, _strip_prefixes(rep)
                count = text.count(find)
        if count == 1:
            text = text.replace(find, rep, 1)
            continue
        if count > 1:
            raise EditError("Edit %d: the find text appears %d times; add a line or two around it so it's unique."
                            % (n, count))
        loose = _loose_find(text, find)
        if not loose:
            hint = _nearest(text, find)
            raise EditError("Edit %d: the find text isn't in the file. Read the file again and copy the lines "
                            "exactly.%s" % (n, ("\nClosest lines:\n" + hint) if hint else ""))
        start, end, _ = loose
        block = text[start:end]
        new = _reindent(rep, block)
        if block.endswith("\n") and new and not new.endswith("\n"):
            new += "\n"
        text = text[:start] + new + text[end:]
    if line_edits:
        lines = text.splitlines(True)
        spans = []
        for n, e in line_edits:
            try:
                a, b = int(e["start_line"]), int(e.get("end_line") or e["start_line"])
            except (TypeError, ValueError):
                raise EditError("Edit %d: start_line and end_line must be numbers." % n)
            if a < 1 or b < a - 1 or b > len(lines):
                raise EditError("Edit %d: lines %d-%d aren't in the file (it has %d lines)." % (n, a, b, len(lines)))
            spans.append((a, b, n, e))
        spans.sort(reverse=True)
        for i in range(1, len(spans)):
            if spans[i][1] >= spans[i - 1][0]:
                raise EditError("Edits %d and %d change overlapping lines." % (spans[i][2], spans[i - 1][2]))
        for a, b, n, e in spans:
            rep = _strip_prefixes(str(e.get("replace") or ""))
            if rep and not rep.endswith("\n"):
                rep += "\n"
            lines[a - 1:b] = [rep] if rep else []
        text = "".join(lines)
    if text == old:
        raise EditError("Those edits wouldn't change anything.")
    return text


# ---------------------------------------------------------------- quick checks

def _node():
    return shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node")
                                         if os.path.exists(p)), None)


def _node_check(js, line_offset=0, label="script"):
    node = _node()
    if not node:
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        os.unlink(tmp)
    if r.returncode == 0:
        return ""
    err = r.stderr or ""
    m = re.search(r":(\d+)\n", err)
    line = int(m.group(1)) + line_offset if m else None
    msg = next((l for l in err.splitlines() if "Error" in l), err.strip()[:200])
    return "JavaScript error in %s%s: %s" % (label, " at line %d" % line if line else "", msg.strip())


def quick_check(name, text):
    """'' if the file looks fine (or can't be checked quickly), else the error in one or two lines."""
    ext = Path(name).suffix.lower()
    try:
        if ext == ".py":
            compile(text, name, "exec")
        elif ext == ".json":
            json.loads(text)
        elif ext in (".js", ".mjs", ".cjs"):
            return _node_check(text, 0, name)
        elif ext in (".html", ".htm"):
            for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S | re.I):
                if re.search(r'type=["\'](?!text/javascript|module)', m.group(0)[:80]):
                    continue
                offset = text.count("\n", 0, m.start(1))
                problem = _node_check(m.group(1), offset, name)
                if problem:
                    return problem
        elif ext in (".sh", ".bash", ".zsh"):
            r = subprocess.run(["/bin/bash", "-n"], input=text, capture_output=True, text=True, timeout=20)
            if r.returncode:
                return "Shell syntax error: %s" % (r.stderr.strip().splitlines() or ["?"])[-1]
    except SyntaxError as e:
        return "Python syntax error at line %s: %s" % (e.lineno, e.msg)
    except ValueError as e:
        return "JSON error: %s" % e
    except (OSError, subprocess.SubprocessError):
        return ""
    return ""


def show_lines(text, line, around=3):
    rows = text.splitlines()
    a, b = max(1, line - around), min(len(rows), line + around)
    return "\n".join("%s%5d| %s" % (">" if i == line else " ", i, rows[i - 1][:200]) for i in range(a, b + 1))


# ---------------------------------------------------------------- build errors with their code

ERROR_PATTERNS = [
    re.compile(r"^(?P<file>[^\s:][^:\n]*?\.\w+):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?:fatal\s+)?error[:\s](?P<msg>.*)$",
               re.M),                                                      # gcc, clang, swiftc
    re.compile(r"^error(?:\[\w+\])?: (?P<msg>.*)\n\s*--> (?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+)", re.M),   # rust
    re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:.*\n.*\n?\s*\^*\s*\n)?(?P<msg>(?:\w+Error|\w+Exception)'
               r'.*)?', re.M),                                             # python
    re.compile(r"^(?P<file>[^\s(][^(\n]*\.\w+)\((?P<line>\d+),(?P<col>\d+)\): error (?P<msg>.*)$", re.M),        # tsc
    re.compile(r"^(?P<file>[^\s:][^:\n]*?\.\w+):(?P<line>\d+):(?P<col>\d+): (?P<msg>undefined reference.*)$", re.M),
]


def error_context(root, output, limit=4):
    """The first few errors in a build's output, each with the code around it. '' if none could be placed."""
    root = Path(root)
    seen, parts = set(), []
    found = []
    for pat in ERROR_PATTERNS:
        for m in pat.finditer(output or ""):
            found.append((m.start(), m))
    found.sort(key=lambda x: x[0])
    for _, m in found:
        f, line = m.group("file").strip(), int(m.group("line"))
        p = Path(f) if os.path.isabs(f) else root / f
        if not p.is_file():
            alt = list(root.rglob(Path(f).name))[:1] if len(Path(f).name) > 2 else []
            if not alt:
                continue
            p = alt[0]
        try:
            rel = p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue      # a library header outside the project
        if (rel, line) in seen:
            continue
        seen.add((rel, line))
        try:
            text = p.read_text("utf-8", "replace")
        except OSError:
            continue
        msg = (m.groupdict().get("msg") or "").strip()
        parts.append("%s line %d: %s\n%s" % (rel, line, msg[:240], show_lines(text, line)))
        if len(parts) >= limit:
            break
    return ("The code at each error (> marks the line):\n\n" + "\n\n".join(parts)) if parts else ""


# ---------------------------------------------------------------- project map

_SYMBOLS = {
    ".c": r"^[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;]*$",
    ".h": r"^(?:typedef\s+struct\s+(\w+)|struct\s+(\w+)\s*\{|[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^)]*\)\s*;)",
    ".cpp": r"^(?:class\s+(\w+)|[A-Za-z_][\w\s\*&:<>]*?\b([\w:~]+)\s*\([^;]*$)",
    ".swift": r"^\s*(?:(?:public|private|internal|fileprivate|open|static|final|override|@\w+)\s+)*"
              r"(?:func\s+(\w+)|class\s+(\w+)|struct\s+(\w+)|enum\s+(\w+)|protocol\s+(\w+))",
    ".js": r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s*\*?\s*(\w+)|class\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*"
           r"(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)",
    ".go": r"^func\s+(?:\([^)]*\)\s*)?(\w+)|^type\s+(\w+)",
    ".rs": r"^\s*(?:pub\s+)?(?:fn\s+(\w+)|struct\s+(\w+)|enum\s+(\w+)|trait\s+(\w+)|impl(?:<[^>]*>)?\s+(\w+))",
    ".java": r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class\s+(\w+)|[\w<>\[\]]+\s+(\w+)\s*\([^;]*$)",
    ".sh": r"^\s*(?:function\s+)?(\w+)\s*\(\)\s*\{",
    ".lua": r"^\s*(?:local\s+)?function\s+([\w.:]+)",
}
_SYMBOLS.update({".cc": _SYMBOLS[".cpp"], ".cxx": _SYMBOLS[".cpp"], ".hpp": _SYMBOLS[".h"], ".m": _SYMBOLS[".c"],
                 ".mm": _SYMBOLS[".cpp"], ".ts": _SYMBOLS[".js"], ".tsx": _SYMBOLS[".js"], ".jsx": _SYMBOLS[".js"],
                 ".mjs": _SYMBOLS[".js"], ".kt": _SYMBOLS[".java"]})
_NOT_NAMES = {"if", "for", "while", "switch", "return", "sizeof", "else", "do", "catch", "main_"}
_USES = {
    ".py": r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    ".c": r'^\s*#include\s+"([^"]+)"', ".h": r'^\s*#include\s+"([^"]+)"', ".cpp": r'^\s*#include\s+"([^"]+)"',
    ".hpp": r'^\s*#include\s+"([^"]+)"', ".cc": r'^\s*#include\s+"([^"]+)"', ".m": r'^\s*#(?:include|import)\s+"([^"]+)"',
    ".js": r"""(?:import\s.*?from\s+['"](\.[^'"]+)['"]|require\(['"](\.[^'"]+)['"]\))""",
    ".ts": r"""import\s.*?from\s+['"](\.[^'"]+)['"]""", ".go": r'^\s*"([\w./-]+)"', ".rs": r"^\s*(?:use|mod)\s+([\w:]+)",
}


def _py_symbols(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            meths = [b.name for b in node.body if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
            out.append("class %s@%d(%s)" % (node.name, node.lineno, ", ".join(meths[:12]) + (
                " +%d" % (len(meths) - 12) if len(meths) > 12 else "")))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append("%s()@%d" % (node.name, node.lineno))
    return out


def symbols(path, text):
    ext = Path(path).suffix.lower()
    if ext == ".py":
        found = _py_symbols(text)
        if found is not None:
            return found
    if ext in (".html", ".htm"):
        ext = ".js"           # functions in the page's scripts (line numbers stay those of the whole file)
    pat = _SYMBOLS.get(ext)
    if not pat:
        return []
    out, names = [], set()
    for m in re.finditer(pat, text, re.M):
        name = next((g for g in m.groups() if g), None)
        if name and name not in _NOT_NAMES and name not in names:
            names.add(name)
            kind = "" if re.search(r"\b(class|struct|enum|trait|protocol|type)\b", m.group(0)) else "()"
            out.append("%s%s@%d" % (name, kind, text.count("\n", 0, m.start()) + 1))
    return out


def uses(path, text):
    pat = _USES.get(Path(path).suffix.lower())
    if not pat:
        return []
    out = []
    for m in re.finditer(pat, text, re.M):
        name = next((g for g in m.groups() if g), None)
        if name and name not in out:
            out.append(name)
    return out


def project_map(root, sub="", per_file=40, limit=14000):
    """A short map: each code file with its size, what it defines (name@line) and which of the project's files it
    uses."""
    root = Path(root)
    base = root / sub if sub else root
    local = {p.stem for p in root.rglob("*.py") if not any(d in SKIP_DIRS for d in p.parts)} | \
        {p.parent.name for p in root.rglob("__init__.py")}
    rows, total = [], 0
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(files):
            p = Path(dirpath) / fn
            if p.suffix.lower() not in CODE_EXT and fn.lower() not in ("makefile", "cmakelists.txt", "dockerfile"):
                continue
            try:
                size = p.stat().st_size
                text = p.read_text("utf-8", "replace") if size < 3_000_000 else ""
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            lines = text.count("\n") + 1 if text else 0
            row = "%s (%d lines)" % (rel, lines)
            if p.suffix.lower() in (".md", ".json", ".toml", ".yml", ".yaml") or fn.lower() in (
                    "makefile", "cmakelists.txt", "dockerfile"):
                rows.append(row)
                continue
            syms = symbols(rel, text)
            if syms:
                row += "\n    defines: " + ", ".join(syms[:per_file]) + (
                    " ... +%d more (search for them)" % (len(syms) - per_file) if len(syms) > per_file else "")
            u = uses(rel, text)
            if p.suffix.lower() == ".py":
                u = [x for x in u if x.split(".")[0] in local]     # only this project's own files
            if u:
                row += "\n    uses: " + ", ".join(u[:15]) + (" +%d" % (len(u) - 15) if len(u) > 15 else "")
            rows.append(row)
            total += len(row)
            if total > limit:
                rows.append("... (map cut short: ask for a sub-folder to see more)")
                return "\n".join(rows)
    return "\n".join(rows) or "No code files found."
