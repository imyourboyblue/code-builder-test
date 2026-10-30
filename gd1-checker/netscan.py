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
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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


def vendor_of(mac):
    if not mac or ":" not in mac:
        return None
    return OUI_LOOKUP.get(mac.replace(":", "")[:6])


# ---------------------------------------------------------------- names a device announces (mDNS / Bonjour)

def mdns_names(timeout=2.5):
    """ip -> a friendly .local name, and ip -> set of service kinds, heard on the network (Bonjour)."""
    names, services = {}, {}
    q = _dns_query("_services._dns-sd._udp.local", 12)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        try:
            sock.sendto(q, ("224.0.0.251", 5353))
        except OSError:
            return names, services
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            host, svc = _parse_mdns(data)
            if host:
                names.setdefault(ip, host)
            for s in svc:
                services.setdefault(ip, set()).add(s)
    finally:
        sock.close()
    return names, {k: sorted(v) for k, v in services.items()}


def _dns_query(name, qtype):
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return header + q + struct.pack(">HH", qtype, 0x8001)   # 0x8000: ask for replies sent straight back to us


def _parse_mdns(data):
    """Best-effort: pull a hostname and service-type hints out of an mDNS packet."""
    host, svc = None, set()
    try:
        text = data.decode("latin-1")
    except Exception:
        return host, svc
    for m in re.finditer(r"([A-Za-z0-9][A-Za-z0-9\- ]{1,40})\x05local", text):
        name = m.group(1).strip()
        if name and not name.startswith("_") and "." not in name:
            host = host or name
    for m in re.finditer(r"_([a-z0-9\-]{2,20})\x04_tcp", text):
        svc.add(m.group(1))
    for m in re.finditer(r"_([a-z0-9\-]{2,20})\x04_udp", text):
        svc.add(m.group(1))
    return host, svc


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
        name, kind = _upnp_describe(url)
        found[ip] = {"name": name, "kind": kind or (server.split("/")[0] if server else "")}
    return found


def _upnp_describe(url):
    try:
        with urllib.request.urlopen(url, timeout=2.5) as r:
            xml = r.read(8000).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None, None
    name = re.search(r"<friendlyName>([^<]+)</friendlyName>", xml)
    dtype = re.search(r"<deviceType>[^<]*:device:([^:]+):", xml)
    model = re.search(r"<modelName>([^<]+)</modelName>", xml)
    return (name.group(1).strip() if name else (model.group(1).strip() if model else None),
            dtype.group(1) if dtype else None)


# ---------------------------------------------------------------- putting it together

def guess_kind(host, vendor, ports, services, upnp):
    s = " ".join([host or "", vendor or "", " ".join(services or []), (upnp or {}).get("kind", ""),
                  (upnp or {}).get("name", "")]).lower()
    openp = set(ports)
    if 8060 in openp or "roku" in s:
        return "Roku TV / player"
    if 8008 in openp or 8009 in openp or "chromecast" in s or "googlecast" in s:
        return "Chromecast / Google TV"
    if 7000 in openp or "airplay" in s or "appletv" in s:
        return "Apple TV"
    if 1400 in openp or "sonos" in s:
        return "Sonos speaker"
    if 32400 in openp or "plex" in s:
        return "Plex media server"
    if 5555 in openp or "android" in s:
        return "Android device / TV"
    if "mediarenderer" in s or "dial" in services if services else False:
        return "Smart TV"
    if any(v in s for v in ("samsung", "lg", "sony")) and (80 in openp or 8080 in openp or 1900 in openp):
        return "Smart TV (%s)" % vendor
    if 445 in openp or 139 in openp or 548 in openp or 5000 in openp and 548 in openp:
        return "Computer / file server"
    if 9100 in openp or 515 in openp or 631 in openp or "printer" in s or "ipp" in (services or []):
        return "Printer"
    if "nintendo" in s:
        return "Nintendo console"
    if any(v in s for v in ("sony",)) and 22 not in openp:
        return "PlayStation / Sony device"
    if "raspberry" in s:
        return "Raspberry Pi"
    if 22 in openp:
        return "Computer (Linux/Mac/server)"
    if 80 in openp or 443 in openp:
        return "Device with a web page (router, camera, smart home)"
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

    # 2) names the devices announce (Bonjour + UPnP), in the background
    mdns_result, ssdp_result = {}, {}

    def do_mdns():
        try:
            n, s = mdns_names()
            mdns_result.update({"names": n, "services": s})
        except OSError:
            pass

    def do_ssdp():
        try:
            ssdp_result.update(ssdp())
        except OSError:
            pass

    t1, t2 = threading.Thread(target=do_mdns), threading.Thread(target=do_ssdp)
    t1.start()
    t2.start()

    # 3) also treat anything in the ARP table as alive (some devices don't answer ping)
    arp = arp_table()
    for ip in arp:
        base = ip.rsplit(".", 1)[0]
        if any(base == b for b, _ in nets) and ip not in alive:
            alive.append(ip)

    note(len(hosts), len(hosts), "Checking %d device(s) for ways to connect..." % len(alive))

    # 4) port-scan the live hosts
    results = {}

    def probe(ip):
        if stopped():
            return
        openp = [p for p in SCAN_PORTS if _tcp_open(ip, p)]
        banners = {}
        for p in openp:
            if PORTS.get(p, ("", ""))[1] in ("ftp", "ssh", "telnet", "web") and len(banners) < 3:
                b = _banner(ip, p)
                if b:
                    banners[p] = b[:200]
        results[ip] = {"ports": openp, "banners": banners}

    with ThreadPoolExecutor(max_workers=48) as ex:
        list(ex.map(probe, [ip for ip in alive]))

    t1.join(timeout=4)
    t2.join(timeout=4)
    names = mdns_result.get("names", {})
    services = mdns_result.get("services", {})

    own = set(_own_ipv4())
    out = []
    for ip in sorted(alive, key=lambda x: tuple(int(o) for o in x.split("."))):
        if ip in own:
            continue
        mac = arp.get(ip)
        vendor = vendor_of(mac)
        host = names.get(ip)
        up = ssdp_result.get(ip) or {}
        if not host and up.get("name"):
            host = up["name"]
        ports = results.get(ip, {}).get("ports", [])
        svc = services.get(ip, [])
        kind = guess_kind(host, vendor, ports, svc, up)
        out.append({
            "ip": ip, "mac": mac, "vendor": vendor, "name": host, "kind": kind,
            "ports": ports, "port_names": [PORTS[p][0] for p in ports if p in PORTS],
            "services": svc, "banners": results.get(ip, {}).get("banners", {}),
            "access": access_hints(ports),
            "ftp_port": next((p for p in ports if p in (21, 5000)), None),
        })
    return out


def reachable():
    """True if we have a LAN address to scan from at all."""
    return bool(subnets())
