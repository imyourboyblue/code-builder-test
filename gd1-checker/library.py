"""
The homebrew app library for a Switch: every .nro app in /switch with its name, version, author and icon (read from
the file itself), whether it looks damaged, the background programs (sysmodules), and whether a newer release is
out on the app's GitHub page.

Reading an app means downloading it once; what was read is remembered by file size and date, so the next look only
reads new or changed apps. Release checks ask GitHub's public API for the project name only, and are remembered for
six hours. Nothing on the device is changed here; updating goes through the chat's guided update (backed up first).
"""

import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

import firmware
import gd1check as core
import nro

MAX_NRO = 64 * 1024 * 1024
RELEASE_TTL = 6 * 3600
HERE = Path(__file__).resolve().parent
RELEASE_CACHE = HERE / "data-cache" / "release-cache.json"
_release_lock = threading.Lock()


def _dir(cfg):
    d = core.data_dir(cfg) / "library"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def saved(cfg):
    return _load(_dir(cfg) / "library.json", None)


def repos(cfg):
    return _load(_dir(cfg) / "repos.json", {})


def set_repo(cfg, path, repo):
    repo = (repo or "").strip()
    m = re.match(r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", repo)
    if repo and not m:
        raise ValueError("Give the GitHub project as owner/name (like mtheall/ftpd) or its github.com address.")
    r = repos(cfg)
    if repo:
        r[path] = m.group(1)
    else:
        r.pop(path, None)
    (_dir(cfg) / "repos.json").write_text(json.dumps(r, indent=2))
    lib = saved(cfg)
    if lib:
        for a in lib["apps"] + lib.get("sysmodules", []):
            if a["path"] == path:
                a["repo"], a["repo_guessed"] = r.get(path), False
                a.pop("latest", None)
        (_dir(cfg) / "library.json").write_text(json.dumps(lib, indent=2))
    return r.get(path)


def guess_repo(*names):
    for n in names:
        sq = firmware._squash(n or "")
        if not sq:
            continue
        for key, (repo, _) in firmware.KNOWN_HOMEBREW.items():
            k = firmware._squash(key)
            if sq == k or sq.startswith(k) or k.startswith(sq) and len(sq) >= 4:
                return repo
    return None


def find_nros(dev, root="/switch", depth=2):
    out, stack = [], [(root, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            entries = dev._list(d)
        except Exception:
            continue
        for name, kind, size, modified in entries:
            if name in (".", ".."):
                continue
            full = str(PurePosixPath(d) / name)
            if kind == "dir" and lvl < depth:
                stack.append((full, lvl + 1))
            elif kind == "file" and name.lower().endswith(".nro"):
                out.append((full, size, modified))
    return sorted(out)


class ScanJob:
    def __init__(self, cfg):
        self.cfg, self.progress, self.note, self.error, self.finished = cfg, 0.0, "Starting...", None, None
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            scan(self.cfg, self._step, self.stop)
        except SystemExit as e:
            self.error = str(e.code)
        except Exception as e:  # keep the page alive
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.finished = time.time()

    def _step(self, frac, note):
        self.progress, self.note = frac, note

    def status(self):
        return {"running": self.finished is None, "progress": round(self.progress, 3), "note": self.note,
                "error": self.error}


def scan(cfg, step=lambda f, n: None, stop=None):
    """Read every app on the device. Returns the library (also saved)."""
    before = saved(cfg) or {}
    old = {a["path"]: a for a in before.get("apps", [])}
    chosen = repos(cfg)
    icons = _dir(cfg) / "icons"
    icons.mkdir(exist_ok=True)
    dev = core.GD1(cfg)
    dev.connect(quiet=True)
    try:
        step(0.02, "Looking for apps in /switch...")
        files = find_nros(dev)
        if not files and not firmware._names(dev, "/switch"):
            raise SystemExit("There's no /switch folder on %s, so there's no Switch homebrew to list." % cfg["device_name"])
        apps = []
        for i, (path, size, modified) in enumerate(files):
            if stop is not None and stop.is_set():
                break
            prev = old.get(path)
            if prev and prev.get("size") == size and prev.get("modified") == modified:
                apps.append(prev)
                continue
            step(0.05 + 0.85 * i / max(1, len(files)), "Reading %s (%d of %d)..." % (PurePosixPath(path).name, i + 1, len(files)))
            app = {"path": path, "file": PurePosixPath(path).name, "size": size, "modified": modified}
            if size and size > MAX_NRO:
                app.update(name=PurePosixPath(path).stem, note="Too big to read here (%d MB)." % (size // 1048576))
            else:
                buf = io.BytesIO()
                try:
                    dev.call("retrbinary", "RETR " + path, buf.write)
                except Exception as e:
                    app.update(name=PurePosixPath(path).stem, note="Couldn't read it: %s" % e)
                    apps.append(app)
                    continue
                data = buf.getvalue()
                info, probs = nro.inspect(data)
                errors = [p["text"] for p in probs if p["level"] == "error"]
                app.update(name=info.get("name") or PurePosixPath(path).stem, author=info.get("author") or "",
                           version=info.get("version") or "", problems=errors)
                icon = nro.icon_of(data) if not errors else None
                if icon:
                    key = hashlib.sha1(path.encode()).hexdigest()[:16]
                    (icons / (key + ".jpg")).write_bytes(icon)
                    app["icon"] = key
            apps.append(app)
        step(0.92, "Looking at background programs...")
        try:
            mods = firmware.sysmodules(dev)
        except Exception:
            mods = []
    finally:
        dev.close()
    for a in apps:
        a["repo"] = chosen.get(a["path"]) or guess_repo(a.get("name"), PurePosixPath(a["path"]).stem,
                                                         PurePosixPath(a["path"]).parent.name)
        a["repo_guessed"] = a["path"] not in chosen
    sysmods = []
    for m in mods:
        nm = re.sub(r"\s*\(probably\)$", "", m.get("name") or "")
        sysmods.append({"path": m["path"], "file": m["id"], "name": m.get("name") or m["id"], "version": "",
                        "boot2": m.get("boot2"), "repo": chosen.get(m["path"]) or guess_repo(*nm.split(" or ")),
                        "repo_guessed": m["path"] not in chosen, "sysmodule": True})
    lib = {"device": cfg["device_name"], "checked": time.time(), "apps": sorted(apps, key=lambda a: a["name"].lower()),
           "sysmodules": sysmods}
    if before.get("updates_checked"):
        lib["updates_checked"] = before["updates_checked"]
        latest = {a["path"]: a.get("latest") for a in before.get("sysmodules", []) if a.get("latest")}
        for m in sysmods:
            if m["path"] in latest:
                m["latest"] = latest[m["path"]]
    (_dir(cfg) / "library.json").write_text(json.dumps(lib, indent=2))
    step(1.0, "Done.")
    return lib


# ---------------------------------------------------------------- newer releases

def latest_tag(repo, fetch=None):
    """(tag, published date, page) of a public GitHub project's newest release, remembered for six hours."""
    with _release_lock:
        cache = _load(RELEASE_CACHE, {})
        hit = cache.get(repo)
        if hit and time.time() - hit.get("t", 0) < RELEASE_TTL:
            return hit.get("tag"), hit.get("date"), hit.get("url")
    def get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "patchbay/1.0", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    fetch = fetch or get
    try:
        rel = fetch("https://api.github.com/repos/%s/releases/latest" % repo)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        rel = (fetch("https://api.github.com/repos/%s/releases?per_page=1" % repo) or [None])[0] or {}
    tag, date, url = rel.get("tag_name"), (rel.get("published_at") or "")[:10], rel.get("html_url")
    with _release_lock:
        cache = _load(RELEASE_CACHE, {})
        cache[repo] = {"t": time.time(), "tag": tag, "date": date, "url": url}
        RELEASE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        RELEASE_CACHE.write_text(json.dumps(cache))
    return tag, date, url


def _nums(v):
    m = re.search(r"\d+(?:\.\d+)*", v or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


def compare(installed, latest):
    """'newer' if latest is newer, 'same', 'older' (yours is newer, e.g. your own build), or 'unknown'."""
    a, b = _nums(installed), _nums(latest)
    if not a or not b:
        return "unknown"
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return "newer" if b > a else "same" if a == b else "older"


def check_updates(cfg, fetch=None):
    lib = saved(cfg)
    if not lib:
        raise ValueError("Read the apps first.")
    errors = []
    for a in lib["apps"] + lib.get("sysmodules", []):
        if not a.get("repo"):
            continue
        try:
            tag, date, url = latest_tag(a["repo"], fetch)
        except Exception as e:
            errors.append("%s: %s" % (a["repo"], e))
            continue
        a["latest"] = {"tag": tag, "date": date, "url": url,
                       "status": compare(a.get("version"), tag) if tag else "none"}
    lib["updates_checked"] = time.time()
    (_dir(cfg) / "library.json").write_text(json.dumps(lib, indent=2))
    return lib, errors
