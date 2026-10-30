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


# PlayStation homebrew whose GitHub home is well known (the user can set any other one).
PS_KNOWN = {"etahen": ("etaHEN/etaHEN", "etaHEN"), "goldhen": ("GoldHEN/GoldHEN_Releases", "GoldHEN"),
            "ftpsrv": ("ps5-payload-dev/ftpsrv", "ftpsrv"), "websrv": ("ps5-payload-dev/websrv", "websrv"),
            "elfldr": ("ps5-payload-dev/elfldr", "elfldr"), "shsrv": ("ps5-payload-dev/shsrv", "shsrv"),
            "klogsrv": ("ps5-payload-dev/klogsrv", "klogsrv")}


def guess_repo(*names):
    for n in names:
        sq = firmware._squash(n or "")
        if not sq:
            continue
        for key, (repo, _) in list(firmware.KNOWN_HOMEBREW.items()) + list(PS_KNOWN.items()):
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


# ---------------------------------------------------------------- PlayStation 4 / 5 (homebrew FTP with full access)

PS_META = ["/system_data/priv/appmeta", "/user/appmeta"]
PS_APPS = ["/user/app", "/system_ex/app"]
PS_PAYLOADS = ["/data", "/data/payloads", "/data/etaHEN/payloads", "/data/etaHEN/plugins", "/data/GoldHEN/payloads",
               "/data/GoldHEN/plugins", "/mnt/usb0", "/mnt/usb0/payloads", "/mnt/usb1"]
OFFICIAL_ID = re.compile(r"^(CUSA|PPSA|PCJS|PLJS|PCAS|PLAS|ELJS|ECAS)\d{5}$")
SYSTEM_ID = re.compile(r"^NPXS\d{5}$")


def is_playstation(dev):
    return any(firmware._names(dev, d) for d in PS_META + PS_APPS)


def parse_sfo(data):
    """A PS4 param.sfo as a dict (TITLE, TITLE_ID, APP_VER, VERSION, ...)."""
    import struct
    if data[:4] != b"\x00PSF":
        return {}
    keys_at, data_at, count = struct.unpack_from("<III", data, 8)
    out = {}
    for i in range(count):
        koff, fmt, ln, _mx, doff = struct.unpack_from("<HHIII", data, 20 + i * 16)
        key = data[keys_at + koff:data.index(b"\x00", keys_at + koff)].decode("ascii", "replace")
        raw = data[data_at + doff:data_at + doff + ln]
        out[key] = struct.unpack("<I", raw[:4])[0] if fmt == 0x0404 else raw.rstrip(b"\x00").decode("utf-8", "replace")
    return out


def _small(dev, path, limit=4 * 1024 * 1024):
    try:
        return firmware._fetch(dev, path, limit)
    except Exception:
        return None


def ps_app_info(dev, tid):
    """(name, version, meta folder, icon bytes) for one installed title."""
    for base in PS_META + [d + "/" + tid + "/sce_sys" for d in PS_APPS]:
        folder = base if base.endswith("sce_sys") else base + "/" + tid
        names = [n.lower() for n in (firmware._names(dev, folder) or [])]
        if not names:
            continue
        name = ver = None
        if "param.json" in names:
            try:
                j = json.loads((_small(dev, folder + "/param.json") or b"{}").decode("utf-8", "replace"))
                loc = j.get("localizedParameters") or {}
                lang = loc.get(loc.get("defaultLanguage") or "en-US") or loc.get("en-US") or {}
                name = lang.get("titleName") or j.get("titleName")
                ver = j.get("contentVersion") or j.get("masterVersion")
            except ValueError:
                pass
        elif "param.sfo" in names:
            sfo = parse_sfo(_small(dev, folder + "/param.sfo") or b"")
            name, ver = sfo.get("TITLE"), sfo.get("APP_VER") or sfo.get("VERSION")
        icon = _small(dev, folder + "/icon0.png") if "icon0.png" in names else None
        if name or icon:
            return name, ver, folder, icon
    return None, None, None, None


def ps_scan(dev, step, stop, old, icons):
    ids = []
    for d in PS_META + PS_APPS:
        for n in firmware._names(dev, d) or []:
            if re.match(r"^[A-Z]{4}\d{5}$", n) and not SYSTEM_ID.match(n) and n not in ids:
                ids.append(n)
    apps = []
    for i, tid in enumerate(sorted(ids)):
        if stop is not None and stop.is_set():
            break
        prev = old.get("app:" + tid)
        if prev and prev.get("name") != tid:
            apps.append(prev)
            continue
        step(0.05 + 0.8 * i / max(1, len(ids)), "Reading %s (%d of %d)..." % (tid, i + 1, len(ids)))
        name, ver, folder, icon = ps_app_info(dev, tid)
        app = {"path": "app:" + tid, "file": tid, "name": name or tid, "version": ver or "",
               "author": "game or official app" if OFFICIAL_ID.match(tid) else "homebrew", "where": folder,
               "homebrew": not OFFICIAL_ID.match(tid)}
        if icon:
            key = hashlib.sha1(tid.encode()).hexdigest()[:16]
            (icons / (key + ".png")).write_bytes(icon)
            app["icon"] = key
        apps.append(app)
    step(0.9, "Looking for payloads and plugins...")
    payloads, seen = [], set()
    for d in PS_PAYLOADS:
        try:
            entries = dev._list(d)
        except Exception:
            continue
        for name, kind, size, modified in entries:
            if kind == "file" and name.lower().endswith((".elf", ".bin", ".prx", ".sprx")):
                full = str(PurePosixPath(d) / name)
                if full in seen:
                    continue
                seen.add(full)
                payloads.append({"path": full, "file": name, "name": name, "version": "", "size": size,
                                 "sysmodule": True, "kind": "payload"})
    apps.sort(key=lambda a: (not a.get("homebrew"), a["name"].lower()))
    return apps, payloads


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
        step(0.02, "Looking for apps...")
        files = find_nros(dev)
        if not files and not firmware._names(dev, "/switch"):
            if is_playstation(dev):
                return _finish(cfg, before, chosen, *ps_scan(dev, step, stop, old, icons), platform="playstation",
                               step=step)
            raise SystemExit("Patchbay doesn't know where %s keeps its apps yet (it knows Nintendo Switch and "
                             "PlayStation 4/5). Ask the chat to look around its folders." % cfg["device_name"])
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
    return _finish(cfg, before, chosen, apps, sysmods, platform="switch", step=step)


def _finish(cfg, before, chosen, apps, sysmods, platform="switch", step=lambda f, n: None):
    for a in apps + sysmods:
        if "repo" not in a:
            a["repo"] = chosen.get(a["path"]) or guess_repo(a.get("name"), PurePosixPath(a["path"]).stem)
            a["repo_guessed"] = a["path"] not in chosen
    lib = {"device": cfg["device_name"], "platform": platform, "checked": time.time(), "apps": sorted(apps, key=lambda a: (a.get("homebrew") is False, a["name"].lower())),
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


def code_since(repo, tag, fetch=None):
    """How much newer the source code is than a release: (number of changes, date of the newest one), or None."""
    def get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "patchbay/1.0", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    fetch = fetch or get
    try:
        branch = fetch("https://api.github.com/repos/%s" % repo).get("default_branch") or "HEAD"
        c = fetch("https://api.github.com/repos/%s/compare/%s...%s" % (repo, tag, branch))
    except Exception:
        return None
    commits = c.get("commits") or []
    newest = ((commits[-1].get("commit") or {}).get("committer") or {}).get("date", "")[:10] if commits else ""
    return int(c.get("ahead_by") or 0), newest


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
