"""
Find out which firmware / operating system a device runs. Only reads.

  identify()       works on any device: it collects clues (the FTP server's
                   greeting, version files like /etc/os-release, folder names,
                   and the greetings of other services such as SSH or a web
                   page), then makes a best guess. The AI can look the clues
                   up on the web (only the generic clue text is searched).
  device_report()  extra detail for Switch consoles running Atmosphère.

Only uses Python's built-in parts.
"""

import ftplib
import io
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

import gd1check as core

VERSION_LINE = re.compile(r"(?i)(firmware|atmosph|ams|hos|system version|fusee|hekate|version)")
VERSION_STR = re.compile(rb"(?i)(atmosph[\x00-\xff]{0,20}?\d+\.\d+\.\d+|v?\d{1,2}\.\d{1,2}\.\d{1,2}(?:-[0-9a-f]{6,})?)")


def _fetch(dev, path, limit):
    size = dev.size(path)
    if size is not None and size > limit:
        return None
    buf = io.BytesIO()
    dev.call("retrbinary", "RETR " + path, buf.write)
    return buf.getvalue()


def _names(dev, folder):
    try:
        return sorted(e[0] for e in dev._list(folder) if e[0] not in (".", ".."))
    except ftplib.Error:
        return None


def _newest_files(dev, folder, n, with_time=False):
    """The n newest files in a folder (by date if the server gives one, else by name)."""
    try:
        entries = [e for e in dev._list(folder) if e[1] == "file"]
    except ftplib.Error:
        return []
    entries.sort(key=lambda e: (str(e[3] or ""), e[0]))
    pick = entries[-n:][::-1]
    return [(e[0], str(e[3] or "")) for e in pick] if with_time else [e[0] for e in pick]


# ----------------------------------------------------------------- Switch (Atmosphère)

# Written by Patchbay's helper app (helper/patchbay-sysinfo) when it's opened once on the console.
HELPER_FILE = "/switch/patchbay-sysinfo/sysinfo.txt"
HELPER_NRO = "/switch/patchbay-sysinfo/patchbay-sysinfo.nro"
# Atmosphère fatal reports: "Firmware: 19.0.1 (Atmosphère 1.8.0-master-...)"
FATAL_FW = re.compile(r"(?i)firmware:\s*(\d+\.\d+\.\d+)\s*\(\s*atmosph\S*\s+v?(\d+\.\d+\.\d+)")
TEXT_FW = re.compile(r"(?i)(?:firmware|system|hos|os)[ _-]*version\W{0,3}(\d{1,2}\.\d{1,2}\.\d{1,2})")
TEXT_AMS = re.compile(r"(?i)(?:atmosph\S*|ams)[ _-]*(?:version)?\W{0,3}v?(\d+\.\d+\.\d+)")


def _msgpack_str(data, key):
    """Find key in Atmosphère's error reports (msgpack) and return the text stored after it."""
    found = []
    for m in re.finditer(re.escape(key), data):
        i = m.end()
        if i >= len(data):
            continue
        t = data[i]
        if 0xa0 <= t <= 0xbf:
            n, i = t - 0xa0, i + 1
        elif t == 0xd9 and i + 1 < len(data):
            n, i = data[i + 1], i + 2
        else:
            continue
        val = data[i:i + n].decode("utf-8", "replace")
        if re.match(r"^\d+\.\d+\.\d+", val):
            found.append(val)
    return found


def switch_versions(dev):
    """Work out the Switch firmware and Atmosphère versions from files on the SD card. The newest clue wins.

    Returns {"firmware": str|None, "atmosphere": str|None, "source": str, "notes": [str], "helper": bool}."""
    res = {"firmware": None, "atmosphere": None, "source": "", "notes": [], "helper": False}
    clues = []    # (time, firmware, atmosphere, where it came from)

    def get(path, limit=512 * 1024):
        try:
            return _fetch(dev, path, limit) or b""
        except ftplib.Error:
            return b""

    # 1. The helper app's file: exact, straight from the console.
    for n, when in _newest_files(dev, "/switch/patchbay-sysinfo", 10, True):
        if n != "sysinfo.txt":
            continue
        data = get(HELPER_FILE, 64 * 1024)
        if data:
            res["helper"] = True
            kv = dict(re.findall(r"(?m)^\s*([a-z_]+)\s*=\s*(.*?)\s*$", data.decode("utf-8", "replace")))
            clues.append((when or "9", kv.get("firmware") or None, kv.get("atmosphere") or None,
                          "the helper app's file %s (written %s)" % (HELPER_FILE, kv.get("written") or "?")))
            if kv.get("emummc"):
                res["notes"].append("Running from: %s" % kv["emummc"])
    # 2. Fatal reports name both versions.
    for n, when in _newest_files(dev, "/atmosphere/fatal_reports", 3, True):
        m = FATAL_FW.search(get("/atmosphere/fatal_reports/" + n).decode("utf-8", "replace"))
        if m:
            clues.append((when, m.group(1), m.group(2), "the crash report /atmosphere/fatal_reports/%s" % n))
    # 3. Error reports (msgpack) carry the system version.
    for n, when in _newest_files(dev, "/atmosphere/erpt_reports", 3, True):
        vals = _msgpack_str(get("/atmosphere/erpt_reports/" + n, 1024 * 1024), b"OsVersion")
        if vals:
            clues.append((when, vals[0].split("-")[0], None, "the error report /atmosphere/erpt_reports/%s" % n))
    # 4. Other text reports and logs, in case their wording differs.
    for folder in ("/atmosphere/crash_reports", "/atmosphere/logs", "/bootloader"):
        for n, when in _newest_files(dev, folder, 3, True):
            if not n.lower().endswith((".log", ".txt")):
                continue
            text = get("%s/%s" % (folder, n)).decode("utf-8", "replace")
            # "Atmosphère Crash Report (v1.7)" is the report's own format number, not the Atmosphère version.
            text = re.sub(r"(?i)report\s*\(v[\d.]+\)", "report", text)
            fw, ams = TEXT_FW.search(text), TEXT_AMS.search(text)
            if fw or ams:
                clues.append((when, fw and fw.group(1), ams and ams.group(1), "%s/%s" % (folder, n)))
    clues.sort(key=lambda c: c[0], reverse=True)
    where = []
    for when, fw, ams, src in clues:
        used = False
        if fw and not res["firmware"]:
            res["firmware"], used = fw, True
        if ams and not res["atmosphere"]:
            res["atmosphere"], used = ams, True
        if used:
            where.append(src + ("" if src.startswith("the helper") else " (from %s)" % _nice_time(when)))
    res["source"] = "; ".join(where)
    return res


def _nice_time(t):
    m = re.match(r"^(\d{4})(\d\d)(\d\d)(\d\d)(\d\d)", t or "")
    return "%s-%s-%s %s:%s" % m.groups() if m else (t or "an unknown time")


def _package_strings(dev, out):
    for path in ("/atmosphere/package3", "/atmosphere/stratosphere.romfs", "/payload.bin", "/bootloader/update.bin"):
        try:
            data = _fetch(dev, path, 16 * 1024 * 1024)
        except ftplib.Error:
            continue
        if not data:
            continue
        found = []
        for m in VERSION_STR.finditer(data):
            s = re.sub(rb"[^\x20-\x7e]", b"", m.group(0)).decode()
            if s and s not in found:
                found.append(s)
        out.append("%s (%s): version-like text inside: %s" % (
            path, core.human_size(len(data)), ", ".join(found[:15]) or "none found"))


def device_report(cfg):
    dev = core.GD1(cfg)
    try:
        dev.connect(quiet=True)
    except SystemExit as e:
        return "Couldn't connect: %s" % e.code
    out = []
    try:
        top = _names(dev, "/") or []
        has_ams = "atmosphere" in [t.lower() for t in top]
        if has_ams:
            v = switch_versions(dev)
            out.append("FIRMWARE: %s" % (v["firmware"] or "not found on the SD card"))
            out.append("ATMOSPHERE: %s" % (v["atmosphere"] or "not found on the SD card"))
            if v["source"]:
                out.append("Found in: %s" % v["source"])
            out.extend(v["notes"])
            if not v["helper"]:
                out.append("For exact, current versions: offer setup_firmware_helper (Patchbay installs a tiny app; the "
                           "user opens it once from the Homebrew Menu). Don't ask the user to type the versions, and "
                           "don't wait for them: building with the newest libnx works for current firmware.")
        out.append("Top-level folders on %s: %s" % (cfg["device_name"], ", ".join(top) or "(none)"))
        if not has_ams:
            out.append("No /atmosphere folder (this may not be a Switch SD card).")
        if "bootloader" in [t.lower() for t in top]:
            out.append("hekate (bootloader folder) is present.")
        if has_ams:
            _package_strings(dev, out)
        apps = _names(dev, "/switch")
        if apps is not None:
            out.append("Homebrew in /switch: %s" % (", ".join(apps[:60]) or "(empty)"))
        if has_ams:
            mods = sysmodules(dev)
            if mods:
                out.append("Background programs (sysmodules) in /atmosphere/contents: " +
                           ", ".join("%s (%s)" % (m["name"], m["id"]) for m in mods))
    finally:
        dev.close()
    return "\n".join(out)


# Well-known homebrew and where its source code lives. The AI checks them with latest_release.
KNOWN_HOMEBREW = {
    "ftpd": ("mtheall/ftpd", "ftpd (FTP server app)"),
    "sys-ftpd-light": ("cathery/sys-ftpd-light", "sys-ftpd-light (background FTP server)"),
    "sys-ftpd": ("jakibaki/sys-ftpd", "sys-ftpd (background FTP server)"),
    "nx-hbmenu": ("switchbrew/nx-hbmenu", "Homebrew Menu"),
    "hbmenu": ("switchbrew/nx-hbmenu", "Homebrew Menu"),
    "sys-clk": ("retronx-team/sys-clk", "sys-clk (overclocking)"),
    "nx-ovlloader": ("WerWolv/nx-ovlloader", "Tesla overlay loader"),
    "tesla-menu": ("WerWolv/Tesla-Menu", "Tesla overlay menu"),
    "ldn_mitm": ("spacemeowx2/ldn_mitm", "ldn_mitm (LAN play)"),
    "sys-con": ("cathery/sys-con", "sys-con (third-party controllers)"),
    "missioncontrol": ("ndeadly/MissionControl", "MissionControl (Bluetooth controllers)"),
    "emuiibo": ("XorTroll/emuiibo", "emuiibo"),
    "goldleaf": ("XorTroll/Goldleaf", "Goldleaf"),
    "jksv": ("J-D-K/JKSV", "JKSV (save backups)"),
    "nx-shell": ("joel16/NX-Shell", "NX-Shell (file manager)"),
    "checkpoint": ("BernardoGiordano/Checkpoint", "Checkpoint (save manager)"),
    "moonlight": ("XITRIX/Moonlight-Switch", "Moonlight (game streaming)"),
    "nxdumptool": ("DarkMatterCore/nxdumptool", "nxdumptool"),
    "sys-patch": ("impeeza/sys-patch", "sys-patch"),
    "chiaki": ("streetpea/chiaki-ng", "Chiaki (PlayStation Remote Play); check which fork the user had"),
}


# Program ids that well-known sysmodules use, for ones without a toolbox.json name.
KNOWN_IDS = {
    "420000000000000E": "sys-ftpd-light or sys-ftpd (probably)", "00FF0000636C6BFF": "sys-clk (probably)",
    "420000000007E51A": "nx-ovlloader (probably)", "4200000000000010": "ldn_mitm (probably)",
    "690000000000000D": "sys-con (probably)", "010000000000BD00": "MissionControl (probably)",
    "0100000000000352": "emuiibo (probably)", "420000000000000B": "sys-patch (probably)",
}


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def sysmodules(dev):
    """Background programs in /atmosphere/contents/<id>/ that have their own code (exefs.nsp)."""
    mods = []
    for tid in _names(dev, "/atmosphere/contents") or []:
        if not re.match(r"^[0-9A-Fa-f]{16}$", tid):
            continue
        files = _names(dev, "/atmosphere/contents/" + tid) or []
        if "exefs.nsp" not in [f.lower() for f in files]:
            continue
        name = None
        if "toolbox.json" in [f.lower() for f in files]:
            try:
                info = json.loads(_fetch(dev, "/atmosphere/contents/%s/toolbox.json" % tid, 64 * 1024) or b"{}")
                name = info.get("name")
            except (ftplib.Error, ValueError, AttributeError):
                pass
        mods.append({"id": tid.upper(), "name": name or KNOWN_IDS.get(tid.upper(), "unnamed"), "path": "/atmosphere/contents/%s/exefs.nsp" % tid,
                     "boot2": "boot2.flag" in [f.lower() for f in (_names(dev, "/atmosphere/contents/%s/flags" % tid)
                                                                   or [])]})
    return mods


def find_homebrew(cfg, words):
    """Find the homebrew the user means ("FTPD light", "chiaki") among the .nro apps and sysmodules on the SD card."""
    import nro
    if not _squash(words):
        raise ValueError("Say which program, e.g. ftpd.")
    dev = core.GD1(cfg)
    try:
        dev.connect(quiet=True)
    except SystemExit as e:
        raise ValueError("Couldn't connect: %s" % e.code)
    found = []    # (score, description, device path, name)
    try:
        if "atmosphere" not in [t.lower() for t in (_names(dev, "/") or [])]:
            return _find_anywhere(dev, cfg, words)
        nros = []
        try:
            top = dev._list("/switch")
        except ftplib.Error:
            top = []
        for e in top:
            if e[0] in (".", ".."):
                continue
            if e[1] == "file" and e[0].lower().endswith(".nro"):
                nros.append("/switch/" + e[0])
            elif e[1] == "dir":
                for n in _names(dev, "/switch/" + e[0]) or []:
                    if n.lower().endswith(".nro"):
                        nros.append("/switch/%s/%s" % (e[0], n))
        for path in nros:
            stem = path.rsplit("/", 1)[-1][:-4]
            score = max(_match(words, stem), _match(words, path.split("/")[2]))
            if not score:
                continue
            info, name = "no name inside", stem
            try:
                data = _fetch(dev, path, 64 * 1024 * 1024)
                meta = nro.inspect(data)[0] if data else {}
                if meta.get("name"):
                    name = meta["name"]
                    info = "name %s, version %s, author %s" % (name, meta.get("version") or "?", meta.get("author") or "?")
            except Exception:   # a damaged file still counts as found
                info = "couldn't read its name and version"
            found.append((score, "App %s (%s)" % (path, info), path, name))
        for m in sysmodules(dev):
            score = max(_match(words, m["name"]), _match(words, m["id"]))
            if score:
                found.append((score, "Background program (sysmodule) %s, id %s, code at %s%s" % (
                    m["name"], m["id"], m["path"], ", starts at boot" if m["boot2"] else ""), m["path"], m["name"]))
    finally:
        dev.close()
    found.sort(key=lambda x: -x[0])
    lines = []
    if found:
        lines.append("Best match: %s" % found[0][1])
        lines.extend("Also matches: %s" % f[1] for f in found[1:6])
    else:
        lines.append("Nothing on the SD card matches %r. Check list_folder /switch and /atmosphere/contents." % words)
    names = [words] + ([found[0][3]] if found else [])
    repos = []
    for k, (repo, what) in KNOWN_HOMEBREW.items():
        score = max(_match(n, k) for n in names)
        if score:
            repos.append((score, len(k), repo, what))
    for score, _, repo, what in sorted(repos, reverse=True)[:3]:
        lines.append("Known source for %s: https://github.com/%s (check it with latest_release %s)" % (what, repo, repo))
    if found and "/atmosphere/contents/" in found[0][2]:
        lines.append("A sysmodule's built file is usually an .nsp; it goes to %s (as exefs.nsp). The console needs a "
                     "restart to load the new one." % found[0][2])
    return "\n".join(lines)


def _find_anywhere(dev, cfg, words, max_dirs=400, depth=6):
    """Any device: look through its folders (a limited number) for files or folders named like the user's words."""
    import platforms
    top = _names(dev, "/") or []
    plat = platforms.guess(top)
    hits, queue, listed = [], [("/", 0)], 0
    while queue and listed < max_dirs:
        d, lvl = queue.pop(0)
        try:
            entries = dev._list(d)
        except (ftplib.Error, OSError, EOFError):
            continue
        listed += 1
        for name, kind, size, modified in entries:
            if name in (".", "..") or kind == "skip":
                continue
            full = str(PurePosixPath(d) / name)
            score = _match(words, name.rsplit(".", 1)[0] if kind == "file" else name)
            if score:
                hits.append((score, "%s %s%s" % ("Folder" if kind == "dir" else "File", full,
                                                 "" if kind == "dir" else " (%s)" % core.human_size(size or 0)), full))
            if kind == "dir" and lvl < depth and name.lower() not in ("proc", "sys", "dev", "tmp", "run"):
                queue.append((full, lvl + 1))
    hits.sort(key=lambda h: -h[0])
    lines = ["Device type: %s" % (platforms.BUILT_IN[plat]["name"] if plat else
                                  "not one Patchbay knows yet (use identify_firmware, then research it)")]
    if hits:
        lines.append("Best match: %s" % hits[0][1])
        lines.extend("Also matches: %s" % h[1] for h in hits[1:8])
    else:
        lines.append("Nothing named like %r in the first %d folders. Try list_folder, or other words." % (words, listed))
    lines.append("Use platform_notes for where this kind of device keeps apps and how to build them.")
    return "\n".join(lines)


def _match(words, text):
    """How well the user's words match a name: 0 = not at all, 3 = exactly."""
    want, t = _squash(words), _squash(text)
    if not t or not want:
        return 0
    if want == t:
        return 3
    if want in t or (len(t) > 2 and t in want):
        return 2
    # "light ftpd" vs "sys-ftpd-light": every word the user said appears in the name
    pieces = re.findall(r"[a-z]+|\d+", (words or "").lower())
    return 1 if pieces and all(w in t for w in pieces) else 0


def latest_release(repo):
    """The newest release of a public GitHub project, e.g. Atmosphere-NX/Atmosphere or switchbrew/libnx."""
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo or ""):
        raise ValueError("Give it as owner/name, e.g. switchbrew/libnx.")
    req = urllib.request.Request("https://api.github.com/repos/%s/releases?per_page=3" % repo,
                                 headers={"User-Agent": "patchbay/1.0", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.loads(r.read())
    if not rel:
        return "%s has no releases." % repo
    parts = []
    for x in rel:
        parts.append("%s  (%s, published %s)\n%s" % (x.get("tag_name"), x.get("name") or "",
                                                    (x.get("published_at") or "")[:10], (x.get("body") or "")[:2500]))
    return ("Newest releases of %s:\n\n" % repo) + "\n\n---\n\n".join(parts)


# ----------------------------------------------------------------- any device

VERSION_FILES = [
    "/etc/os-release", "/usr/lib/os-release", "/etc/lsb-release", "/etc/openwrt_release", "/etc/debian_version",
    "/etc/redhat-release", "/etc/alpine-release", "/etc/issue", "/etc/version", "/etc/release", "/etc/sw_version",
    "/etc/firmware_version", "/etc/fw_version", "/etc/build", "/etc/buildinfo", "/bin/freebsd-version",
    "/etc/freebsd-update.conf", "/system/build.prop", "/build.prop", "/default.prop", "/version", "/VERSION",
    "/version.txt", "/firmware_version", "/firmware.txt", "/var/version", "/boot/issue.txt", "/boot/config.txt",
    "/System/Library/CoreServices/SystemVersion.plist", "/proc/version", "/info.txt", "/release.txt",
]
FOLDER_HINTS = {
    "atmosphere": "Nintendo Switch with Atmosphère custom firmware", "bootloader": "hekate bootloader (Switch)",
    "switch": "Switch homebrew folder", "nintendo": "Nintendo console SD card", "ps3": "PlayStation 3 files",
    "dev_hdd0": "PlayStation 3 (internal drive)", "ps4": "PlayStation 4 files", "data/gametdb": "Wii/Wii U homebrew",
    "apps": "Wii/Wii U Homebrew Channel apps", "wiiu": "Wii U homebrew", "3ds": "Nintendo 3DS homebrew",
    "luma": "Nintendo 3DS with Luma3DS", "_nds": "Nintendo DS homebrew", "retroarch": "RetroArch",
    "system": "Android-style system folder", "storage": "Android-style storage", "boot": "a /boot folder (Linux or BSD)",
    "etc": "Unix-style /etc (Linux or BSD)", "usr": "Unix-style /usr", "windows": "Windows", "users": "Windows or macOS",
    "applications": "macOS", "dcim": "camera or phone storage", "mnt": "Unix-style /mnt", "jffs": "router (Asus-style)",
    "overlay": "OpenWrt-style overlay", "flash": "embedded flash storage",
}
PORTS = [(22, "SSH"), (23, "Telnet"), (80, "web page"), (8080, "web page"), (443, "secure web page"), (445, "SMB file sharing"),
         (5000, "FTP or web"), (548, "AFP (Mac file sharing)"), (5555, "Android debugging"), (2323, "Telnet")]


def banner(host, port, send=None, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            if send:
                s.sendall(send)
            data = s.recv(600)
        return re.sub(r"[^\x20-\x7e\n]", "", data.decode("latin-1")).strip()[:300]
    except OSError:
        return None


def http_server(host, port, tls=False):
    import ssl
    try:
        url = "%s://%s:%d/" % ("https" if tls else "http", host, port)
        ctx = ssl._create_unverified_context() if tls else None   # LAN devices use self-made certificates
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "patchbay/1.0"})
        with urllib.request.urlopen(req, timeout=2, context=ctx) as r:
            h = r.headers
    except urllib.error.HTTPError as e:
        h = e.headers
    except (OSError, ValueError):
        return None
    bits = ["%s: %s" % (k, h.get(k)) for k in ("Server", "X-Powered-By", "WWW-Authenticate") if h.get(k)]
    return "; ".join(bits) or "(no server name given)"


def guess_from(clues):
    """Turn clues into one short label, best evidence first."""
    text = {k: v for k, v in clues.get("files", {}).items()}
    def kv(body, key):
        m = re.search(r'(?m)^%s=["\']?([^"\'\n]+)' % re.escape(key), body)
        return m.group(1).strip() if m else None
    for f in ("/etc/os-release", "/usr/lib/os-release"):
        if f in text and kv(text[f], "PRETTY_NAME"):
            return kv(text[f], "PRETTY_NAME")
    if "/bin/freebsd-version" in text and kv(text["/bin/freebsd-version"], "USERLAND_VERSION"):
        return "FreeBSD " + kv(text["/bin/freebsd-version"], "USERLAND_VERSION")
    if "/etc/openwrt_release" in text and kv(text["/etc/openwrt_release"], "DISTRIB_DESCRIPTION"):
        return kv(text["/etc/openwrt_release"], "DISTRIB_DESCRIPTION")
    for f in ("/system/build.prop", "/build.prop", "/default.prop"):
        if f in text and kv(text[f], "ro.build.version.release"):
            return "Android %s (%s)" % (kv(text[f], "ro.build.version.release"), kv(text[f], "ro.build.display.id") or "?")
    if "/System/Library/CoreServices/SystemVersion.plist" in text:
        m = re.findall(r"<string>([^<]+)</string>", text["/System/Library/CoreServices/SystemVersion.plist"])
        if m:
            return "macOS " + " ".join(m[:3])
    if "/proc/version" in text:
        return text["/proc/version"].split("(")[0].strip()
    for f in ("/etc/lsb-release",):
        if f in text and kv(text[f], "DISTRIB_DESCRIPTION"):
            return kv(text[f], "DISTRIB_DESCRIPTION")
    for f in ("/etc/redhat-release", "/etc/alpine-release", "/etc/debian_version", "/etc/version", "/etc/sw_version",
              "/etc/firmware_version", "/version", "/VERSION", "/version.txt", "/firmware_version"):
        if f in text and text[f].strip():
            first = text[f].strip().splitlines()[0][:80]
            return ("Debian " + first) if f == "/etc/debian_version" else ("Alpine Linux " + first) if "alpine" in f else first
    tops = [t.lower() for t in clues.get("top", [])]
    if "atmosphere" in tops:
        return "Nintendo Switch (Atmosphère)"
    ssh = clues.get("ports", {}).get(22) or ""
    for os_name in ("FreeBSD", "Ubuntu", "Debian", "Raspbian", "OpenWrt", "NetBSD", "OpenBSD", "Synology"):
        if os_name.lower() in ssh.lower():
            return "%s (from its SSH greeting)" % os_name
    if "dropbear" in ssh.lower():
        return "Embedded Linux (Dropbear SSH)"
    syst = (clues.get("ftp_system") or "").lower()
    for os_name in ("freebsd", "windows", "linux", "unix", "macos", "netbsd"):
        if os_name in syst or os_name in (clues.get("ftp_welcome") or "").lower():
            return "Unknown %s-style system (from its FTP server)" % os_name.capitalize()
    return None


def identify(cfg):
    """Collect every clue about what the device runs. Returns (report text, best guess or None)."""
    clues = {"files": {}, "ports": {}}
    host = cfg["host"]
    dev = core.GD1(cfg)
    try:
        ftp = dev.connect(quiet=True)
        clues["ftp_welcome"] = (ftp.getwelcome() or "").strip()
        for cmd in ("SYST", "STAT"):
            try:
                clues["ftp_system" if cmd == "SYST" else "ftp_stat"] = dev.call("sendcmd", cmd)[:400]
            except ftplib.Error:
                pass
        try:
            clues["ftp_features"] = dev.call("sendcmd", "FEAT")[:400]
        except ftplib.Error:
            pass
        clues["top"] = _names(dev, "/") or []
        for sub in ("/etc", "/system", "/boot"):
            if sub[1:] in [t.lower() for t in clues["top"]]:
                clues["top_" + sub[1:]] = (_names(dev, sub) or [])[:80]
        for f in VERSION_FILES:
            try:
                data = _fetch(dev, f, 256 * 1024)
            except (ftplib.Error, OSError, EOFError):
                continue
            if data and core.looks_like_text(data[:4096]):
                clues["files"][f] = data.decode("utf-8", "replace")[:3000]
    except SystemExit as e:
        clues["ftp_error"] = str(e.code)
    finally:
        dev.close()
    for port, what in PORTS:
        if port in [int(p) for p in cfg["ports"]]:
            continue
        if port in (80, 8080, 5000):
            b = http_server(host, port)
        elif port == 443:
            b = http_server(host, port, tls=True)
        else:
            b = banner(host, port)
        if b is not None:
            clues["ports"][port] = "%s: %s" % (what, b or "(open, no greeting)")

    guess = guess_from(clues)
    out = ["Best guess: %s" % (guess or "not sure yet (look up the clues below)"), "", "Clues:"]
    if clues.get("ftp_error"):
        out.append("- FTP: couldn't connect (%s)" % clues["ftp_error"])
    else:
        out.append("- FTP greeting: %s" % (clues.get("ftp_welcome") or "(none)"))
        out.append("- FTP system type: %s" % (clues.get("ftp_system") or "(not given)"))
        if clues.get("ftp_stat"):
            out.append("- FTP status: %s" % clues["ftp_stat"].replace("\n", " | ")[:300])
        tops = clues.get("top", [])
        out.append("- Top-level folders: %s" % (", ".join(tops[:60]) or "(none)"))
        hints = sorted({FOLDER_HINTS[t.lower()] for t in tops if t.lower() in FOLDER_HINTS})
        if hints:
            out.append("- Folder names suggest: %s" % "; ".join(hints))
        for k in ("top_etc", "top_system", "top_boot"):
            if clues.get(k):
                out.append("- /%s holds: %s" % (k[4:], ", ".join(clues[k][:50])))
        for f, body in clues["files"].items():
            out.append("- %s:\n    %s" % (f, "\n    ".join(body.strip().splitlines()[:15])))
        if not clues["files"]:
            out.append("- No standard version files were readable over FTP.")
    for port, b in sorted(clues["ports"].items()):
        out.append("- Port %d open, %s" % (port, b))
    if "atmosphere" in [t.lower() for t in clues.get("top", [])]:
        rep = device_report(cfg)
        fw = re.search(r"(?m)^FIRMWARE: (\d+\.\d+\.\d+)", rep)
        ams = re.search(r"(?m)^ATMOSPHERE: (\d+\.\d+\.\d+)", rep)
        if fw or ams:
            guess = "Switch %s%s" % (fw.group(1) if fw else "(firmware unknown)", ", Atmosphère " + ams.group(1) if ams else "")
            out[0] = "Best guess: %s" % guess
        out.append("")
        out.append(rep)
    out.append("")
    out.append("If the guess is unclear, search the web for the most specific clue (a version string, a server name, "
               "an unusual folder name) with web_search, using only that generic text. Then save the answer with "
               "save_firmware_name.")
    return "\n".join(out), guess


def web_search(query):
    """A plain web search (DuckDuckGo). Returns titles, addresses and snippets."""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) patchbay/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
    results = []
    for m in re.finditer(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                         r'class="result__snippet"[^>]*>(.*?)</a>', html):
        href, title, snip = m.groups()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        href = q[0] if q else href
        clean = lambda x: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).replace("&amp;", "&").replace("&#x27;", "'").strip()
        results.append("%s\n  %s\n  %s" % (clean(title), href, clean(snip)))
        if len(results) >= 8:
            break
    return "\n".join(results) if results else "No results (the search site may have refused). Try read_web_page on a known site."
