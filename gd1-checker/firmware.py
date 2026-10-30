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


def device_report(cfg):
    dev = core.GD1(cfg)
    try:
        dev.connect(quiet=True)
    except SystemExit as e:
        return "Couldn't connect: %s" % e.code
    out = []
    try:
        top = _names(dev, "/") or []
        out.append("Top-level folders on %s: %s" % (cfg["device_name"], ", ".join(top) or "(none)"))
        has_ams = "atmosphere" in [t.lower() for t in top]
        out.append("Atmosphère folder: %s" % ("found" if has_ams else "NOT found (this may not be a Switch SD card)"))
        if "bootloader" in [t.lower() for t in top]:
            out.append("hekate (bootloader folder) is present.")
        if has_ams:
            # Crash and error reports usually name the firmware and Atmosphère versions.
            for folder in ("/atmosphere/fatal_reports", "/atmosphere/crash_reports", "/atmosphere/erpt_reports"):
                names = [n for n in (_names(dev, folder) or []) if not n.endswith("/")]
                files = [n for n in names if "." in n][-2:]
                for n in reversed(files):
                    try:
                        data = _fetch(dev, "%s/%s" % (folder, n), 512 * 1024)
                    except ftplib.Error:
                        continue
                    if not data or not core.looks_like_text(data[:4096]):
                        continue
                    hits = [l.strip() for l in data.decode("utf-8", "replace").splitlines()[:80]
                            if VERSION_LINE.search(l)][:12]
                    if hits:
                        out.append("From %s/%s:\n  " % (folder, n) + "\n  ".join(hits))
            # The Atmosphère package itself carries its version text.
            for path in ("/atmosphere/package3", "/atmosphere/fusee-secondary.bin", "/payload.bin",
                         "/bootloader/update.bin"):
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
            cfgs = _names(dev, "/atmosphere/contents")
            if cfgs is not None:
                out.append("Installed sysmodules/overrides in /atmosphere/contents: %d" % len(cfgs))
        apps = _names(dev, "/switch")
        if apps is not None:
            out.append("Homebrew in /switch: %s" % (", ".join(apps[:60]) or "(empty)"))
    finally:
        dev.close()
    out.append("If the versions above aren't clear, ask the user: on the Switch, System Settings > System shows the "
               "firmware version, and the Atmosphère version shows there too (e.g. 19.0.1|AMS 1.8.0|S).")
    return "\n".join(out)


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
        fw = re.search(r"(?i)firmware version:\s*([\d.]+)", rep)
        ams = re.search(r"(?i)atmosph\S*\s+version:\s*([\d.]+)", rep) or re.search(r"(?i)atmosph\S*\s+v?(\d+\.\d+\.\d+)", rep)
        if fw or ams:
            guess = "Switch %s, Atmosphère %s" % (fw.group(1) if fw else "(firmware ?)", ams.group(1) if ams else "?")
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
