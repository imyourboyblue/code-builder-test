"""
Finding devices on the user's own home network: a TV, a console, a server, a phone. Patchbay sweeps the Mac's own
local network, sees which addresses answer, checks which normal doors they have open (FTP, SSH, SMB, a TV's network
app, a web page), reads any name the device announces about itself, and guesses what each one is. It then suggests
the normal, documented way to connect to each (using the user's own login), and for devices it doesn't recognise it
can look the model up online later.

This only looks at the user's own network, only when they ask, and only through the interfaces a device openly
offers. It does not try passwords and does not attempt to break in. Built-in parts only (sockets).
"""

import re
import socket
import http.client
import struct
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Doors Patchbay knows how to use or point the user at, with the plain name and the normal way in.
PORTS = {
    21: ("FTP", "ftp"), 22: ("SSH", "ssh"), 23: ("Telnet", "telnet"), 80: ("Web page", "web"),
    139: ("Windows sharing", "smb"), 443: ("Secure web page", "web"), 445: ("Windows/Mac file sharing", "smb"),
    515: ("Printer", "print"), 548: ("Mac file sharing (AFP)", "afp"), 554: ("Streaming (RTSP)", "rtsp"),
    631: ("Printer (IPP)", "print"), 1400: ("Sonos", "sonos"), 3389: ("Windows Remote Desktop", "rdp"),
    5000: ("FTP/UPnP", "ftp"), 5001: ("Server admin", "web"), 5555: ("Android debug (ADB)", "adb"),
    7000: ("AirPlay", "airplay"), 8008: ("Chromecast", "cast"), 8009: ("Chromecast", "cast"),
    8060: ("Roku", "roku"), 8080: ("Web admin", "web"), 8443: ("Secure web admin", "web"),
    9100: ("Printer (raw)", "print"), 32400: ("Plex media server", "plex"), 1900: ("UPnP/DLNA", "dlna"),
    5357: ("Windows device", "web"), 62078: ("iPhone sync", "ios"),
}
SCAN_PORTS = sorted(PORTS)

# A few common maker prefixes of a hardware (MAC) address, so a device can be named even before it's identified.
# Not a full list; unknown ones can be looked up online.
OUI = {
    "Nintendo": ["7CBB8A", "A4C0E1", "98B6E9", "E84ECE", "582F40", "0019FD", "0021BD", "34AF2C", "CCFB65"],
    "Sony": ["FCF152", "D44B5E", "A8E3EE", "00041F", "001315", "0013A9", "0015C1", "28A183", "709E29"],
    "Microsoft": ["000D3A", "7CED8D", "D8CB8A", "5C4296", "F01DBC", "00155D", "C83F26"],
    "Samsung": ["F8042E", "5001BB", "8CC8CD", "BCF5AC", "E4E0C5", "F4428F", "C06599", "D0667B"],
    "LG": ["00E091", "001C62", "001E75", "001F6B", "0022A9", "A816B2", "CC2D8C", "74A722"],
    "Apple": ["F0D1A9", "A8667F", "34C059", "001451", "0017F2", "F4F15A", "DC2B2A", "9C207B", "F0DBF8"],
    "Google": ["F4F5D8", "F88FCA", "A47733", "6466B3", "D8EB46", "1CF29A", "54609E"],
    "Amazon": ["F0272D", "44650D", "68543D", "FC65DE", "0C47C9", "B47C9C", "A002DC"],
    "Roku": ["B0A737", "B83E59", "CC6DA0", "D83134", "08052B", "C83912", "DC3A5E"],
    "Raspberry Pi": ["B827EB", "DCA632", "E45F01", "28CDC1", "D83ADD", "2CCF67"],
    "Espressif (smart home)": ["240AC4", "30AEA4", "3C71BF", "A020A6", "807D3A", "2462AB", "D8A01D"],
    "Ubiquiti": ["24A43C", "788A20", "FCECDA", "687251", "44D9E7", "B4FBE4"],
    "Intel": ["3C9709", "00C2C6", "8086F2", "A4C3F0", "94E23C"],
    "TP-Link": ["50C7BF", "EC086B", "A42BB0", "003192", "1C3BF3", "60A4B7"],
}
OUI_LOOKUP = {p: name for name, prefs in OUI.items() for p in prefs}


def _own_ipv4():
    """This Mac's own LAN IPv4 address (without sending anything)."""
    ips = set()
    for target in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 9))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                ips.add(ip)
        except OSError:
            pass
        finally:
            s.close()
    return sorted(ips)


def subnets():
    """The /24 ranges to sweep, from this Mac's own addresses."""
    nets = []
    for ip in _own_ipv4():
        parts = ip.split(".")
        if len(parts) == 4:
            nets.append((".".join(parts[:3]), ip))
    return nets


def _ping(ip, timeout=1.0):
    """True if the host answers a ping (fills the ARP table as a side effect). Uses the system ping."""
    import subprocess
    import sys
    flag = "-t" if sys.platform == "darwin" else "-W"
    val = str(int(timeout)) if sys.platform != "darwin" else str(int(timeout))
    try:
        r = subprocess.run(["ping", "-c", "1", flag, val, ip], capture_output=True, timeout=timeout + 1.5,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _tcp_open(ip, port, timeout=0.4):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _banner(ip, port, timeout=0.8):
    """A short greeting from a plain-text service (FTP/SSH/Telnet), if it sends one. Never sends a login."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        if port in (80, 8080, 8060, 8008, 8009):
            s.sendall(b"GET / HTTP/1.0\r\nHost: %s\r\n\r\n" % ip.encode())
        data = s.recv(400)
        return data.decode("utf-8", "replace").strip()
    except OSError:
        return ""
    finally:
        s.close()


def arp_table():
    """ip -> MAC address, read from the system's ARP table (no network traffic)."""
    import subprocess
    out = ""
    for cmd in (["arp", "-a", "-n"], ["arp", "-a"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
            if out.strip():
                break
        except (OSError, subprocess.SubprocessError):
            continue
    table = {}
    for ip, mac in re.findall(r"\(?(\d+\.\d+\.\d+\.\d+)\)?\s+at\s+([0-9a-fA-F:]{11,17})", out):
        table[ip] = normalize_mac(mac)
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+[\w.]*\s+([0-9a-fA-F:-]{11,17})", out):
        table.setdefault(m.group(1), normalize_mac(m.group(2)))
    return table


def normalize_mac(mac):
    hexs = re.sub(r"[^0-9a-fA-F]", "", mac).upper()
    if len(hexs) != 12:
        # arp can print single-digit octets (a:b:c:1:2:3); pad each piece to two.
        parts = [p.zfill(2) for p in re.split(r"[:-]", mac)]
        hexs = "".join(parts).upper()
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2)) if len(hexs) == 12 else mac.upper()


# The full list of makers' address prefixes is public. Patchbay downloads it once (a plain public file, nothing about
# the user is sent) and keeps it in data-cache; the small table above is used until then.
DATA = Path(__file__).resolve().parent / "data-cache"
MAKERS_FILE = DATA / "mac-makers.txt"
MAKERS_URLS = ["https://www.wireshark.org/download/automated/data/manuf",
               "https://standards-oui.ieee.org/oui/oui.csv"]
_makers = None
_makers_lock = threading.Lock()
CORP = re.compile(r"[,.]?\s+(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|gmbh|ag|sa|bv|plc|"
                  r"pty|oy|ab|kk|srl|spa)\b\.?", re.I)


def _tidy_maker(name):
    name = re.sub(r"\s+", " ", name.strip().strip('"'))
    for _ in range(3):
        shorter = CORP.sub("", name).strip(" ,.")
        if not shorter or shorter == name:
            break
        name = shorter
    if name.isupper() and len(name) > 5:
        name = name.title()
    return name


def _parse_makers(text):
    table = {}
    if text.startswith("Registry,") or "MA-L," in text[:2000]:
        import csv
        import io
        for row in csv.reader(io.StringIO(text)):
            if len(row) >= 3 and re.fullmatch(r"[0-9A-Fa-f]{6,9}", row[1] or ""):
                table[row[1].upper()] = _tidy_maker(row[2])
        return table
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        pref, _, bits = parts[0].partition("/")
        hexs = re.sub(r"[^0-9A-Fa-f]", "", pref).upper()
        n = int(bits) // 4 if bits.isdigit() else 6
        if len(hexs) >= n >= 6:
            table[hexs[:n]] = _tidy_maker(parts[2] if len(parts) > 2 and parts[2].strip() else parts[1])
    return table


def load_makers(download=True):
    """prefix -> maker name, from the downloaded public list (downloaded once if allowed)."""
    global _makers
    with _makers_lock:
        if _makers is not None:
            return _makers
        text = ""
        if MAKERS_FILE.exists() and time.time() - MAKERS_FILE.stat().st_mtime < 180 * 86400:
            text = MAKERS_FILE.read_text(errors="replace")
        elif download:
            for url in MAKERS_URLS:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Patchbay"})
                    with urllib.request.urlopen(req, timeout=25) as r:
                        got = r.read(12_000_000).decode("utf-8", "replace")
                    if len(_parse_makers(got)) > 10000:
                        DATA.mkdir(parents=True, exist_ok=True)
                        MAKERS_FILE.write_text(got)
                        text = got
                        break
                except (OSError, ValueError, http.client.HTTPException):
                    continue
            if not text and MAKERS_FILE.exists():
                text = MAKERS_FILE.read_text(errors="replace")   # an old copy is better than none
        _makers = _parse_makers(text) if text else {}
        return _makers


def is_private_mac(mac):
    """Phones, tablets and laptops often use a made-up ("private") Wi-Fi address, which has no maker."""
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except (TypeError, ValueError):
        return False


def vendor_of(mac):
    if not mac or ":" not in mac:
        return None
    hexs = mac.replace(":", "").upper()
    makers = _makers or {}
    for n in (9, 7, 6):
        if hexs[:n] in makers:
            return makers[hexs[:n]]
    if hexs[:6] in OUI_LOOKUP:
        return OUI_LOOKUP[hexs[:6]]
    if is_private_mac(mac):
        return "Private address"
    return None


# ---------------------------------------------------------------- names a device announces (mDNS / Bonjour)

# Service types many home devices announce. Asking for them makes devices say their name and model.
MDNS_TYPES = ["_services._dns-sd._udp", "_device-info._tcp", "_airplay._tcp", "_raop._tcp", "_googlecast._tcp",
              "_companion-link._tcp", "_apple-mobdev2._tcp", "_hap._tcp", "_homekit._tcp", "_spotify-connect._tcp",
              "_amzn-wplay._tcp", "_ipp._tcp", "_printer._tcp", "_pdl-datastream._tcp", "_smb._tcp", "_afpovertcp._tcp",
              "_ssh._tcp", "_sftp-ssh._tcp", "_rfb._tcp", "_http._tcp", "_workstation._tcp", "_sonos._tcp",
              "_androidtvremote2._tcp", "_nvstream._tcp", "_matter._tcp", "_meshcop._udp", "_esphomelib._tcp"]
MODEL_KEYS = ("model", "md", "ty", "product", "usb_mdl", "modelname", "mdl", "am", "devicemodel")
NAME_KEYS = ("fn", "n", "name", "friendlyname", "deviceName")


def _read_name(data, off):
    labels, end, hops = [], None, 0
    while True:
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | data[off + 1]
            hops += 1
            if hops > 30:
                raise ValueError("loop")
            continue
        labels.append(data[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if end is not None else off)


def parse_dns(data):
    """The records in a DNS/mDNS packet: list of (name, type, value). Types: 1 A, 12 PTR, 16 TXT, 33 SRV."""
    out = []
    try:
        qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
        off = 12
        for _ in range(qd):
            _, off = _read_name(data, off)
            off += 4
        for _ in range(an + ns + ar):
            name, off = _read_name(data, off)
            typ, _cls, _ttl, rdl = struct.unpack(">HHIH", data[off:off + 10])
            off += 10
            start, off = off, off + rdl
            val = None
            if typ == 1 and rdl == 4:
                val = socket.inet_ntoa(data[start:off])
            elif typ == 12:
                val = _read_name(data, start)[0]
            elif typ == 33:
                val = _read_name(data, start + 6)[0]
            elif typ == 16:
                val, i = {}, start
                while i < off:
                    n = data[i]
                    item = data[i + 1:i + 1 + n].decode("utf-8", "replace")
                    i += 1 + n
                    k, _, v = item.partition("=")
                    if k:
                        val[k.lower()] = v
            out.append((name, typ, val))
    except (IndexError, struct.error, ValueError, OSError):
        pass
    return out


def _instance(name):
    """'Living Room._airplay._tcp.local' -> ('Living Room', 'airplay')."""
    m = re.match(r"(.+?)\._([A-Za-z0-9-]+)\._(?:tcp|udp)\.local\.?$", name or "")
    return (m.group(1).replace("\\032", " "), m.group(2)) if m else (None, None)


def _host_label(name):
    name = (name or "").rstrip(".")
    if name.endswith(".local"):
        name = name[:-6]
    if not name or name.startswith("_") or re.fullmatch(r"[\d.-]+|[0-9a-f-]{20,}", name, re.I):
        return None
    return name


def _absorb(recs, src, info):
    """Put what an mDNS packet says into info[ip] = {host, names, models, services}."""
    host_ip = {}
    for name, typ, val in recs:
        if typ == 1 and val:
            host_ip[name.lower()] = val
    def slot(ip):
        return info.setdefault(ip, {"host": None, "names": [], "models": [], "services": set()})
    for name, typ, val in recs:
        if typ == 1 and val:
            h = _host_label(name)
            if h and not slot(val)["host"]:
                slot(val)["host"] = h
        elif typ == 12 and val:
            if name.endswith("in-addr.arpa") or name.endswith("in-addr.arpa."):
                h = _host_label(val)
                if h and not slot(src)["host"]:
                    slot(src)["host"] = h
                continue
            inst, svc = _instance(val)
            if svc:
                d = slot(src)
                d["services"].add(svc)
                if inst and svc not in ("device-info",) and inst not in d["names"]:
                    d["names"].append(inst)
            else:
                m = re.match(r"_([A-Za-z0-9-]+)\._(?:tcp|udp)", val)
                if m:
                    slot(src)["services"].add(m.group(1))
        elif typ == 33 and val:
            inst, svc = _instance(name)
            ip = host_ip.get(val.lower(), src)
            h = _host_label(val)
            if h and not slot(ip)["host"]:
                slot(ip)["host"] = h
        elif typ == 16 and isinstance(val, dict):
            inst, svc = _instance(name)
            d = slot(src)
            for k in MODEL_KEYS:
                v = val.get(k.lower())
                if v and v not in d["models"] and len(v) < 80:
                    d["models"].append(v)
            for k in NAME_KEYS:
                v = val.get(k.lower())
                if v and v not in d["names"] and len(v) < 80 and svc in ("googlecast", "spotify-connect", None):
                    d["names"].append(v)


def _mdns_packet(names, qtype=12):
    header = struct.pack(">HHHHHH", 0, 0, len(names), 0, 0, 0)
    body = b""
    for name in names:
        body += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p) + b"\x00"
        body += struct.pack(">HH", qtype, 0x8001)   # 0x8000: ask for replies sent straight back to us
    return header + body


def mdns_browse(timeout=3.0, info=None):
    """Ask the network (Bonjour) which devices offer common services, and listen for their names and models."""
    info = {} if info is None else info
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.4)
        try:
            for i in range(0, len(MDNS_TYPES), 9):
                sock.sendto(_mdns_packet([t + ".local" for t in MDNS_TYPES[i:i + 9]]), ("224.0.0.251", 5353))
        except OSError:
            return info
        _listen(sock, timeout, info)
    finally:
        sock.close()
    return info


def mdns_reverse(ips, timeout=2.0, info=None):
    """Ask each device directly for its own Bonjour name (most Apple, Linux and many TV/smart devices answer)."""
    info = {} if info is None else info
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.4)
        for ip in ips:
            rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
            try:
                sock.sendto(_mdns_packet([rev]), (ip, 5353))
                sock.sendto(_mdns_packet(["_device-info._tcp.local", "_services._dns-sd._udp.local"]), (ip, 5353))
            except OSError:
                pass
        _listen(sock, timeout, info)
    finally:
        sock.close()
    return info


def _listen(sock, timeout, info):
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(9000)
        except socket.timeout:
            continue
        except OSError:
            break
        _absorb(parse_dns(data), addr[0], info)


def mdns_names(timeout=2.5):
    """Older helper kept for callers: ip -> name, ip -> services."""
    info = mdns_browse(timeout)
    names = {ip: (d["names"] or [d["host"]])[0] for ip, d in info.items() if d["names"] or d["host"]}
    return names, {ip: sorted(d["services"]) for ip, d in info.items()}


def _dns_query(name, qtype):
    return _mdns_packet([name], qtype)


# ---------------------------------------------------------------- other names a device gives out about itself

def reverse_dns(ip):
    """The name the router's DNS has for this address (often the device's own name, like Living-Room-TV)."""
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return None
    first = name.split(".")[0]
    if not first or first.replace("-", ".") == ip or re.fullmatch(r"[\d-]+|ip-[\d-]+", first):
        return None
    return first


def netbios_name(ip, timeout=0.8):
    """A Windows PC's (or Samba server's) computer name, from the standard NetBIOS name query."""
    q = struct.pack(">HHHHHH", 0x5042, 0, 1, 0, 0, 0) + b"\x20" + b"CK" + b"A" * 30 + b"\x00" + struct.pack(">HH", 0x21, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (ip, 137))
        data = s.recv(2048)
        count = data[56]
        for i in range(count):
            e = data[57 + i * 18:57 + (i + 1) * 18]
            name, suffix, flags = e[:15].decode("ascii", "replace").strip(), e[15], struct.unpack(">H", e[16:18])[0]
            if suffix == 0 and not flags & 0x8000 and name:
                return name
    except (OSError, IndexError, struct.error):
        return None
    finally:
        s.close()
    return None


def playstation_search(timeout=2.0):
    """PlayStation consoles answer the discovery message the PlayStation Remote Play app sends. ip -> info."""
    found = {}
    msg = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.4)
        for port in (9302, 987):
            try:
                s.sendto(msg, ("255.255.255.255", port))
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            kv = dict(re.findall(r"(?m)^([a-z-]+):(.*)$", text))
            if kv.get("host-type") or kv.get("host-name"):
                found[addr[0]] = {"name": kv.get("host-name", "").strip() or None,
                                  "model": kv.get("host-type", "").strip() or "PlayStation"}
    finally:
        s.close()
    return found


def _get(url, timeout=2.0, limit=30000):
    import ssl
    ctx = ssl.create_default_context()
    # Only for reading the title of a device's own page on the home network: those use home-made certificates.
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Patchbay"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(limit).decode("utf-8", "replace"), r.headers.get("Server", "")


def web_title(ip, port):
    """The title of a device's own web page (routers and printers usually put their model there)."""
    try:
        html, server = _get("%s://%s:%d/" % ("https" if port in (443, 8443) else "http", ip, port))
    except urllib.error.HTTPError as e:
        return None, e.headers.get("Server", "") if e.headers else ""
    except (OSError, ValueError, http.client.HTTPException):
        return None, ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    if title and (len(title) > 70 or title.lower() in ("index", "home", "login", "document", "untitled", "redirect")):
        title = None
    return title, server


def roku_info(ip):
    """A Roku's own name and model, from its documented local control (ECP) interface. Read-only."""
    try:
        xml, _ = _get("http://%s:8060/query/device-info" % ip)
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    pick = lambda tag: (re.search(r"<%s>([^<]+)</%s>" % (tag, tag), xml) or [None, None])[1]
    return {"name": pick("user-device-name") or pick("friendly-device-name"),
            "model": _maker_model(pick("vendor-name"), pick("model-name"))}


def _maker_model(maker, model):
    if maker and model and model.lower().startswith(maker.split()[0].lower()):
        return model
    return " ".join(x for x in (maker, model) if x) or None


def cast_info(ip):
    """A Chromecast / Google TV's own name and model, from its local setup page. Read-only."""
    try:
        import json
        text, _ = _get("http://%s:8008/setup/eureka_info?params=name,device_info" % ip)
        j = json.loads(text)
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    di = j.get("device_info") or {}
    return {"name": j.get("name"), "model": _maker_model(di.get("manufacturer"), di.get("model_name"))}


# ---------------------------------------------------------------- media devices (SSDP / UPnP)

SSDP = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
        "MX: 2\r\nST: ssdp:all\r\n\r\n").encode()


def ssdp(timeout=3.0):
    """ip -> {name, kind} from devices that announce over UPnP (many TVs, media players, routers)."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.6)
    locations = {}
    try:
        try:
            sock.sendto(SSDP, ("239.255.255.250", 1900))
        except OSError:
            return found
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            loc = re.search(r"(?im)^LOCATION:\s*(\S+)", data.decode("latin-1"))
            srv = re.search(r"(?im)^SERVER:\s*(.+)$", data.decode("latin-1"))
            if loc:
                locations.setdefault(addr[0], (loc.group(1).strip(), srv.group(1).strip() if srv else ""))
    finally:
        sock.close()
    for ip, (url, server) in locations.items():
        name, kind, model = _upnp_describe(url)
        found[ip] = {"name": name, "kind": kind or (server.split("/")[0] if server else ""), "model": model}
    return found


def _upnp_describe(url):
    try:
        with urllib.request.urlopen(url, timeout=2.5) as r:
            xml = r.read(8000).decode("utf-8", "replace")
    except (OSError, ValueError, http.client.HTTPException):
        return None, None, None
    name = re.search(r"<friendlyName>([^<]+)</friendlyName>", xml)
    dtype = re.search(r"<deviceType>[^<]*:device:([^:]+):", xml)
    model = re.search(r"<modelName>([^<]+)</modelName>", xml)
    maker = re.search(r"<manufacturer>([^<]+)</manufacturer>", xml)
    mtext = " ".join(x.group(1).strip() for x in (maker, model) if x) or None
    if mtext and maker and model and model.group(1).lower().startswith(maker.group(1).split()[0].lower()):
        mtext = model.group(1).strip()
    return (name.group(1).strip() if name else None, dtype.group(1) if dtype else None, mtext)


# ---------------------------------------------------------------- putting it together

APPLE_MODELS = [("AppleTV", "Apple TV"), ("AudioAccessory", "HomePod"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                ("MacBookPro", "MacBook Pro"), ("MacBookAir", "MacBook Air"), ("MacBook", "MacBook"), ("iMac", "iMac"),
                ("Macmini", "Mac mini"), ("MacStudio", "Mac Studio"), ("Mac1", "Mac"), ("Mac", "Mac"),
                ("Watch", "Apple Watch"), ("RealityDevice", "Apple Vision Pro")]


def nice_model(model):
    """'AppleTV11,1' -> 'Apple TV (AppleTV11,1)'. Other models are returned as they are."""
    if not model:
        return None
    for prefix, label in APPLE_MODELS:
        if re.match(re.escape(prefix) + r"\d+,\d+$", model):
            return "%s (%s)" % (label, model)
    return model


def guess_kind(host, vendor, ports, services, upnp, model=None):
    s = " ".join([host or "", vendor or "", model or "", " ".join(services or []), (upnp or {}).get("kind", ""),
                  (upnp or {}).get("name", ""), (upnp or {}).get("model") or ""]).lower()
    openp = set(ports)
    svc = set(services or [])
    if "playstation" in s or re.search(r"\bps[45]\b", s) or "sony interactive" in s:
        return "PlayStation"
    if "nintendo" in s:
        return "Nintendo console"
    if "xbox" in s:
        return "Xbox"
    if 8060 in openp or "roku" in s:
        return "Roku TV / player"
    if 8008 in openp or 8009 in openp or "googlecast" in svc or "chromecast" in s:
        return "Chromecast / Google TV"
    if "apple tv" in s or "appletv" in s:
        return "Apple TV"
    if "homepod" in s or "audioaccessory" in s:
        return "HomePod"
    if "iphone" in s or "ipad" in s or "apple-mobdev2" in svc:
        return "iPhone / iPad"
    if re.search(r"\b(macbook|imac|mac mini|macmini|mac studio|macstudio)\b", s):
        return "Mac"
    if 1400 in openp or "sonos" in s:
        return "Sonos speaker"
    if 32400 in openp or "plex" in s:
        return "Plex media server"
    if "amzn-wplay" in svc or "fire tv" in s or "firetv" in s or "echo" in s:
        return "Amazon Fire TV / Echo"
    if 5555 in openp or "android" in s:
        return "Android device / TV"
    if "mediarenderer" in s or "dial" in svc:
        return "Smart TV / media player"
    if re.search(r"\b(samsung|lg|vizio|tcl|hisense|philips|panasonic|sony)\b", s) and (
            openp & {80, 8080, 1900, 7000, 8001, 8002} or "tv" in s):
        return "Smart TV"
    if 9100 in openp or 515 in openp or 631 in openp or "printer" in s or svc & {"ipp", "printer", "pdl-datastream"}:
        return "Printer"
    if "hap" in svc or "homekit" in svc or "matter" in svc or "espressif" in s or "esphomelib" in svc:
        return "Smart home device"
    if "raspberry" in s:
        return "Raspberry Pi"
    if 445 in openp or 139 in openp or 548 in openp or svc & {"smb", "afpovertcp", "workstation"}:
        return "Computer / file server"
    if "companion-link" in svc or ("apple" in s and not openp):
        return "Apple device (iPhone, iPad, Mac or Apple TV)"
    if 22 in openp or "ssh" in svc:
        return "Computer (Linux/Mac/server)"
    if re.search(r"router|gateway|asus|netgear|tp-link|linksys|eero|ubiquiti|unifi|orbi|deco|mikrotik|arris|technicolor", s):
        return "Router / Wi-Fi"
    if 80 in openp or 443 in openp:
        return "Device with a web page (router, camera, smart home)"
    if vendor == "Private address":
        return "Phone, tablet or laptop (uses a private Wi-Fi address)"
    if vendor:
        return "%s device" % vendor
    return "Unknown device"


def access_hints(ports):
    """The normal, documented ways to connect, from the open doors. Patchbay uses the user's own login."""
    ways = []
    seen = set()
    for p in ports:
        label, kind = PORTS.get(p, (None, None))
        if kind and kind not in seen and kind in CONNECT:
            seen.add(kind)
            ways.append({"port": p, "name": label, "how": CONNECT[kind]})
    return ways


CONNECT = {
    "ftp": "Connect with FTP. Patchbay can add it as a device if you have the username and password (many use anonymous).",
    "ssh": "Connect with SSH using your account on the device. Patchbay can open a terminal session once you add it.",
    "smb": "Connect with Windows/Mac file sharing (SMB) and your login for a shared folder.",
    "afp": "Connect with Mac file sharing (AFP) and your login.",
    "web": "Open its web page in a browser to sign in to its admin or app.",
    "adb": "Android debugging is on. Pair with ADB after you allow this computer on the device.",
    "cast": "A Chromecast/Google TV. Cast to it, or use Google's developer tools if you turn on its developer mode.",
    "roku": "A Roku. Turn on Developer Mode on the Roku to connect, or use its network remote (ECP).",
    "airplay": "AirPlay. Stream to it from your Mac.",
    "sonos": "A Sonos. Use the Sonos app or its local control API.",
    "plex": "A Plex server. Sign in to its web page to manage it.",
    "rdp": "Windows Remote Desktop. Connect with your Windows login.",
    "telnet": "Telnet (old and unencrypted). Only if the device needs it; prefer SSH when it's offered.",
    "print": "A printer. Add it in System Settings, or open its web page.",
    "dlna": "Shares media over UPnP/DLNA. Browse it from a media player.",
    "rtsp": "A camera or media stream (RTSP). Open it in a player like VLC with your login.",
    "ios": "An iPhone/iPad (sync service). Pair it the normal way from Finder.",
    "sonos": "A Sonos speaker. Control it from the Sonos app.",
}


def _usable(ip, mac=None):
    last = ip.rsplit(".", 1)[-1]
    if last in ("0", "255"):
        return False   # the network's own and "everyone" addresses, not devices
    if mac:
        try:
            if int(mac[:2], 16) & 0x01 or mac.upper() == "FF:FF:FF:FF:FF:FF":
                return False   # a group (multicast/broadcast) address
        except ValueError:
            pass
    return True


def scan(progress=None, stop=None, deep=True):
    """Find devices on the Mac's own network. progress(done,total,note) is called as it goes. Returns a list."""
    def note(done, total, text):
        if progress:
            progress(done, total, text)

    def stopped():
        return stop is not None and stop.is_set()

    nets = subnets()
    if not nets:
        return []
    hosts = []
    for base, own in nets:
        hosts += ["%s.%d" % (base, i) for i in range(1, 255)]
    hosts = list(dict.fromkeys(hosts))
    note(0, len(hosts), "Looking for devices on your network...")

    # The makers' list may need a one-time download; start it now so it's ready at the end.
    makers_t = threading.Thread(target=load_makers, daemon=True)
    makers_t.start()

    # 1) ping sweep (fills the ARP table and finds what's alive)
    alive = []
    done = [0]
    lock = threading.Lock()

    def ping_one(ip):
        if stopped():
            return
        up = _ping(ip, 1.0)
        with lock:
            done[0] += 1
            if done[0] % 16 == 0:
                note(done[0], len(hosts), "Checked %d of %d addresses..." % (done[0], len(hosts)))
        if up:
            alive.append(ip)

    with ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(ping_one, hosts))
    if stopped():
        return []

    # 2) names the devices announce (Bonjour, UPnP, PlayStation discovery), in the background
    mdns_info, ssdp_result, ps_result = {}, {}, {}

    def bg(fn):
        def run():
            try:
                fn()
            except OSError:
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    threads = [bg(lambda: mdns_browse(3.0, mdns_info)), bg(lambda: ssdp_result.update(ssdp())),
               bg(lambda: ps_result.update(playstation_search()))]

    # 3) also treat anything in the ARP table as alive (some devices don't answer ping)
    arp = arp_table()
    for ip in arp:
        base = ip.rsplit(".", 1)[0]
        if any(base == b for b, _ in nets) and ip not in alive:
            alive.append(ip)
    alive = [ip for ip in alive if _usable(ip, arp.get(ip))]
    rev_info = {}
    threads.append(bg(lambda: mdns_reverse(alive, 2.5, rev_info)))

    note(len(hosts), len(hosts), "Checking %d device(s) for names and ways to connect..." % len(alive))

    # 4) port-scan the live hosts and read the names they give out
    results = {}

    def probe(ip):
        if stopped():
            return
        openp = [p for p in SCAN_PORTS if _tcp_open(ip, p)]
        banners, extra = {}, {}
        for p in openp:
            if PORTS.get(p, ("", ""))[1] in ("ftp", "ssh", "telnet") and len(banners) < 3:
                b = _banner(ip, p)
                if b:
                    banners[p] = b[:200]
        if 8060 in openp:
            extra.update({k: v for k, v in roku_info(ip).items() if v})
        if 8008 in openp:
            extra.update({k: v for k, v in cast_info(ip).items() if v})
        web = next((p for p in (80, 8080, 443, 8443) if p in openp), None)
        if web:
            title, server = web_title(ip, web)
            if title:
                extra["title"] = title
            if server:
                banners[web] = ("Server: " + server)[:200]
        extra["dns"] = reverse_dns(ip)
        extra["netbios"] = netbios_name(ip)
        results[ip] = {"ports": openp, "banners": banners, "extra": extra}

    with ThreadPoolExecutor(max_workers=48) as ex:
        list(ex.map(probe, list(alive)))

    for t in threads:
        t.join(timeout=5)
    makers_t.join(timeout=20)

    own = set(_own_ipv4())
    out = []
    for ip in sorted(alive, key=lambda x: tuple(int(o) for o in x.split("."))):
        if ip in own:
            continue
        mac = arp.get(ip)
        vendor = vendor_of(mac)
        r = results.get(ip, {})
        ex = r.get("extra", {})
        md = mdns_info.get(ip) or {}
        rv = rev_info.get(ip) or {}
        up = ssdp_result.get(ip) or {}
        ps = ps_result.get(ip) or {}
        names = (md.get("names") or []) + (rv.get("names") or [])
        models = (md.get("models") or []) + (rv.get("models") or [])
        host = next((n for n in [ps.get("name"), ex.get("name")] + names + [
            up.get("name"), md.get("host"), rv.get("host"), ex.get("netbios"), ex.get("dns")] if n), None)
        model = nice_model(next((m for m in [ex.get("model"), ps.get("model"), up.get("model")] + models if m), None))
        if not model and ex.get("title") and re.search(r"[A-Z]{2,}[- ]?\w*\d", ex["title"]):
            model = ex["title"]   # routers and printers often put their model in the page title (like "RT-AX86U")
        ports = r.get("ports", [])
        svc = sorted(set(md.get("services") or []) | set(rv.get("services") or []))
        kind = guess_kind(host, vendor, ports, svc, up, " ".join(x for x in (model, ex.get("title")) if x))
        out.append({
            "ip": ip, "mac": mac, "vendor": vendor, "name": host, "model": model, "kind": kind,
            "page_title": ex.get("title"),
            "ports": ports, "port_names": [PORTS[p][0] for p in ports if p in PORTS],
            "services": svc, "banners": r.get("banners", {}),
            "access": access_hints(ports),
            "ftp_port": next((p for p in ports if p in (21, 5000)), None),
        })
    return out


def reachable():
    """True if we have a LAN address to scan from at all."""
    return bool(subnets())
