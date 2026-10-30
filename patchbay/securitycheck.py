"""
A security check for one of the user's own devices on their home network. It only looks, the way a home security
scanner does:

  - which network doors (ports) are open, and what software answers on each (from the greeting it sends),
  - known security problems (CVEs) for that software version, looked up in the public US National Vulnerability
    Database (only the product name and version are sent, never an address or anything about the user),
  - the secure-page (TLS) setup: old protocol versions, expired or home-made certificates,
  - risky settings, like Telnet, Android debugging, an open database, or FTP that lets anyone in.

Then it explains each finding in plain words with how to fix it. It does not try to break in: no exploits, no password
guessing. The only login it tries is the documented, public "anonymous" FTP login, to tell the user whether anyone on
the network can read the device's files. Only for private addresses on the Mac's own network, and only when the user
confirms the device is theirs. Built-in parts only.
"""

import ftplib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import netscan

DATA = Path(__file__).resolve().parent / "data-cache"
REPORTS = DATA / "security"
CVE_CACHE = DATA / "cve-cache.json"
NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0"

EXTRA_PORTS = {
    25: "Email server (SMTP)", 53: "DNS", 110: "Email (POP3)", 111: "RPC", 143: "Email (IMAP)", 389: "Directory (LDAP)",
    990: "Secure FTP", 993: "Email (IMAPS)", 1883: "MQTT (smart home)", 2049: "NFS file sharing", 2323: "Telnet (alt)",
    3306: "MySQL database", 5432: "PostgreSQL database", 5900: "Screen sharing (VNC)", 6379: "Redis database",
    7547: "ISP remote management (TR-069)", 8000: "Web (alt)", 8081: "Web (alt)", 8291: "MikroTik admin",
    8888: "Web (alt)", 9000: "Web (alt)", 9200: "Elasticsearch database", 10000: "Webmin admin",
    11211: "Memcached", 27017: "MongoDB database", 49152: "UPnP control",
}
PORT_NAMES = {p: v[0] for p, v in netscan.PORTS.items()}
PORT_NAMES.update(EXTRA_PORTS)
ALL_PORTS = sorted(PORT_NAMES)
TLS_PORTS = {443, 8443, 5001, 993, 990}
WEB_PORTS = {80, 8080, 8000, 8081, 8888, 9000, 5000, 10000, 443, 8443, 5001}

# Products we can look up by their official name in the vulnerability database (CPE vendor:product).
CPE = {
    "openssh": "openbsd:openssh", "dropbear": "dropbear_ssh_project:dropbear_ssh", "apache": "apache:http_server",
    "nginx": "f5:nginx", "lighttpd": "lighttpd:lighttpd", "vsftpd": "beasts:vsftpd", "proftpd": "proftpd:proftpd",
    "pure-ftpd": "pureftpd:pure-ftpd", "boa": "boa:boa", "mini_httpd": "acme:mini_httpd",
    "microsoft-iis": "microsoft:internet_information_services", "samba": "samba:samba",
    "filezilla server": "filezilla-project:filezilla_server", "openssl": "openssl:openssl",
    "jetty": "eclipse:jetty", "micro_httpd": "acme:micro_httpd", "thttpd": "acme:thttpd", "bftpd": "bftpd_project:bftpd",
    "goahead": "embedthis:goahead", "cups": "apple:cups", "dnsmasq": "thekelleys:dnsmasq", "exim": "exim:exim",
    "postfix": "postfix:postfix", "redis": "redis:redis", "mysql": "oracle:mysql",
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class NotAllowed(Exception):
    pass


def check_target(ip):
    """Only private addresses on the Mac's own network."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        raise NotAllowed("That isn't an IP address.")
    if addr.version != 4 or not addr.is_private or addr.is_loopback:
        raise NotAllowed("The security check only works on devices on your own home network.")
    own = [base for base, _ in netscan.subnets()]
    if own and str(addr).rsplit(".", 1)[0] not in own:
        raise NotAllowed("%s isn't on this Mac's own network (%s)." % (addr, ", ".join(b + ".x" for b in own)))
    return str(addr)


# ---------------------------------------------------------------- what software answers

def _greeting(ip, port, timeout=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        try:
            return s.recv(512).decode("utf-8", "replace").strip()
        except socket.timeout:
            return ""
    except OSError:
        return ""
    finally:
        s.close()


def _http_server(ip, port):
    scheme = "https" if port in TLS_PORTS else "http"
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # reading the device's own home-made certificate page
    req = urllib.request.Request("%s://%s:%d/" % (scheme, ip, port), headers={"User-Agent": "PatchBay"})
    try:
        with urllib.request.urlopen(req, timeout=3, context=ctx) as r:
            return r.headers.get("Server", ""), r.headers.get("X-Powered-By", ""), r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return (e.headers.get("Server", "") if e.headers else ""), "", ""
    except (OSError, ValueError, http.client.HTTPException):   # e.g. an FTP server on a "web" port
        return "", "", ""


def software_from(text):
    """[(product, version)] named in a greeting or Server header, e.g. ('openssh', '7.4')."""
    found = []
    t = text or ""
    m = re.search(r"SSH-[\d.]+-([A-Za-z]+)[_-]v?(\d+(?:\.\d+)*)", t)
    if m:
        found.append((m.group(1).lower(), m.group(2)))
    for m in re.finditer(r"(vsFTPd|ProFTPD|Pure-FTPd|FileZilla Server|bftpd|Microsoft FTP Service)[ /]*v?(\d+(?:\.\d+)+)?", t, re.I):
        found.append((m.group(1).lower(), m.group(2) or ""))
    for m in re.finditer(r"\b(Apache|nginx|lighttpd|Boa|mini_httpd|micro_httpd|thttpd|Microsoft-IIS|Jetty|GoAhead[\w-]*|"
                         r"OpenSSL|CUPS|dnsmasq|Exim|Postfix|Samba)[/ (]v?(\d+(?:\.\d+)+)", t, re.I):
        prod = m.group(1).lower()
        prod = "goahead" if prod.startswith("goahead") else prod
        found.append((prod, m.group(2)))
    out = []
    for p, v in found:
        if (p, v) not in out:
            out.append((p, v))
    return out


# ---------------------------------------------------------------- known problems (CVEs)

def _cache():
    try:
        return json.loads(CVE_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(c):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        CVE_CACHE.write_text(json.dumps(c))
    except OSError:
        pass


def _score(cve):
    metrics = cve.get("metrics") or {}
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        for m in metrics.get(key) or []:
            data = m.get("cvssData") or {}
            score = data.get("baseScore")
            sev = (data.get("baseSeverity") or m.get("baseSeverity") or "").lower()
            if score is not None:
                if not sev:
                    sev = "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low"
                return float(score), sev
    return 0.0, "low"


def parse_nvd(data):
    """The useful parts of an NVD answer: [{id, score, severity, summary}], worst first."""
    out = []
    for item in (data or {}).get("vulnerabilities") or []:
        cve = item.get("cve") or {}
        if cve.get("vulnStatus") == "Rejected":
            continue
        desc = next((d.get("value", "") for d in cve.get("descriptions") or [] if d.get("lang") == "en"), "")
        first = re.split(r"(?<=[.!?])\s", desc.strip(), 1)[0][:300]
        score, sev = _score(cve)
        out.append({"id": cve.get("id"), "score": score, "severity": sev, "summary": first})
    out.sort(key=lambda c: -c["score"])
    return out


def lookup_cves(product, version, fetch=None):
    """Known problems for one product version. Only the product name and version are sent."""
    if not version or not re.match(r"\d", version):
        return None
    ver = re.match(r"\d+(?:\.\d+)*", version).group(0)
    key = "%s %s" % (product, ver)
    cache = _cache()
    hit = cache.get(key)
    if hit and time.time() - hit.get("t", 0) < 7 * 86400:
        return hit["cves"]
    cpe = CPE.get(product)
    urls = []
    if cpe:
        urls.append(NVD + "?" + urllib.parse.urlencode({"cpeName": "cpe:2.3:a:%s:%s:*:*:*:*:*:*:*" % (cpe, ver),
                                                        "resultsPerPage": 200}))
    urls.append(NVD + "?" + urllib.parse.urlencode({"keywordSearch": "%s %s" % (product, ver),
                                                    "resultsPerPage": 100}))
    fetch = fetch or _fetch_json
    for url in urls:
        try:
            data = fetch(url)
        except (OSError, ValueError, http.client.HTTPException):
            continue
        cves = parse_nvd(data)
        if cves or url is urls[-1]:
            cache[key] = {"t": time.time(), "cves": cves}
            _save_cache(cache)
            return cves
    return None


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "PatchBay security check"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503) and attempt < 2:   # the database asks for a pause between lookups
                time.sleep(7)
                continue
            if e.code == 404:
                return {}
            raise
    return {}


# ---------------------------------------------------------------- secure pages (TLS)

def tls_check(ip, port):
    """Facts about a secure (https) door: version, certificate problems, and whether it still allows old TLS."""
    info = {"port": port}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False   # it's reached by address, so the name can't match anyway
    try:
        with socket.create_connection((ip, port), timeout=3) as raw:
            with ctx.wrap_socket(raw) as s:
                info["version"] = s.version()
                info["cert"] = "trusted"
    except ssl.SSLCertVerificationError as e:
        msg = (getattr(e, "verify_message", "") or str(e)).lower()
        info["cert"] = "expired" if "expired" in msg else "self-signed" if "self" in msg or "local issuer" in msg else msg
    except (OSError, ssl.SSLError) as e:
        info["error"] = str(e)[:200]
        return info
    if "version" not in info:
        loose = ssl.create_default_context()
        loose.check_hostname, loose.verify_mode = False, ssl.CERT_NONE
        try:
            with socket.create_connection((ip, port), timeout=3) as raw:
                with loose.wrap_socket(raw) as s:
                    info["version"] = s.version()
        except (OSError, ssl.SSLError):
            pass
    old = []
    for name, ver in (("TLS 1.0", getattr(ssl.TLSVersion, "TLSv1", None)), ("TLS 1.1", getattr(ssl.TLSVersion, "TLSv1_1", None))):
        if ver is None:
            continue
        try:
            c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
            c.minimum_version = c.maximum_version = ver
            try:
                c.set_ciphers("ALL:@SECLEVEL=0")
            except ssl.SSLError:
                pass
            with socket.create_connection((ip, port), timeout=3) as raw:
                with c.wrap_socket(raw):
                    old.append(name)
        except (OSError, ssl.SSLError, ValueError):
            pass
    info["old_versions"] = old
    return info


# ---------------------------------------------------------------- anonymous FTP (the public, documented login)

def anonymous_ftp(ip, port):
    """True if FTP lets anyone in with the public 'anonymous' login. Only logs in and out; reads nothing."""
    try:
        f = ftplib.FTP()
        f.connect(ip, port, timeout=5)
        try:
            f.login("anonymous", "anonymous@")
            return True
        except ftplib.error_perm:
            return False
        finally:
            try:
                f.quit()
            except (OSError, ftplib.Error):
                f.close()
    except (OSError, ftplib.Error, EOFError, UnicodeError):
        return None


# ---------------------------------------------------------------- the check itself

def finding(severity, title, why, fix, port=None, cves=None):
    return {"severity": severity, "title": title, "why": why, "fix": fix, "port": port, "cves": cves or []}


def rules(ports, kind):
    out = []
    p = set(ports)
    for port in (23, 2323):
        if port in p:
            out.append(finding("high", "Telnet is on", "Telnet sends everything, including passwords, as plain text, "
                               "and it's a common way in for attacks on home devices.",
                               "Turn Telnet off in the device's settings (or its admin web page). Use SSH instead if "
                               "it offers it.", port))
    if 5555 in p:
        out.append(finding("high", "Android debugging (ADB) is open on the network",
                           "Anyone on your network can install apps or run commands on it if it's not locked to one "
                           "computer.", "Turn off 'Network debugging' / 'ADB over network' in Developer options when "
                           "you're not using it.", 5555))
    dbs = {6379: "Redis", 27017: "MongoDB", 9200: "Elasticsearch", 11211: "Memcached", 3306: "MySQL", 5432: "PostgreSQL"}
    for port, name in dbs.items():
        if port in p:
            out.append(finding("medium", "%s database is reachable on the network" % name,
                               "Databases are often set up without a password, so anything on your network could read "
                               "or change the data.", "Set a password for it, or make it listen only on the device "
                               "itself (localhost) if nothing else needs it.", port))
    if 1883 in p:
        out.append(finding("medium", "MQTT (smart home messages) is open", "Many MQTT servers allow anyone to read and "
                           "send smart-home commands.", "Turn on a username and password in the MQTT server settings.",
                           1883))
    if 5900 in p:
        out.append(finding("medium", "Screen sharing (VNC) is on", "VNC passwords are often short, and older VNC isn't "
                           "encrypted.", "Use a long password, or turn screen sharing off when not needed.", 5900))
    if 3389 in p:
        out.append(finding("low", "Windows Remote Desktop is on", "It's safe on a home network if Windows is up to date "
                           "and the account has a strong password.", "Keep Windows updated. Turn it off if you don't "
                           "use it.", 3389))
    if 7547 in p:
        out.append(finding("medium", "ISP remote management (TR-069) answers on your network",
                           "It's meant for your internet provider; old versions have had serious problems.",
                           "Update the router's firmware. Ask your provider if it can be turned off.", 7547))
    if p & {1900, 49152}:
        out.append(finding("low", "UPnP is on", "UPnP lets apps on your network open doors in the router to the "
                           "internet by themselves." if "router" in (kind or "").lower() else
                           "UPnP lets other devices control or discover this one without a password.",
                           "If you don't need it (game consoles sometimes do), turn UPnP off in the router or device "
                           "settings.", 1900 if 1900 in p else 49152))
    if p & {139, 445}:
        out.append(finding("info", "File sharing (SMB) is on", "Fine on a home network with a password on every share.",
                           "Make sure guest access is off and every shared folder needs a login.", 445 if 445 in p else 139))
    plain_web = sorted(p & {80, 8080, 8000, 8081, 8888, 9000, 10000})
    if plain_web and not p & {443, 8443, 5001}:
        out.append(finding("low", "Its admin page isn't encrypted", "When you sign in over plain http, the password "
                           "crosses your network as plain text.", "If the device has an https option, turn it on and "
                           "use https://. Otherwise only sign in from a trusted network.", plain_web[0]))
    if p & {80, 443, 8080, 8443} and re.search(r"router|camera|printer|web page|smart", kind or "", re.I):
        out.append(finding("info", "Check its admin password", "Routers, cameras and printers often still have the "
                           "password from the box, which is published online for every model.",
                           "Sign in to its web page and change the admin password to a long, unique one. PatchBay "
                           "doesn't try passwords for you.", next(x for x in (443, 8443, 80, 8080) if x in p)))
    return out


class SecurityCheck:
    """One running security check of one device."""

    def __init__(self, ip, kind=None):
        self.ip, self.kind = ip, kind
        self.started, self.finished = time.time(), None
        self.progress, self.note, self.report, self.error = 0.0, "Starting...", None, None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _step(self, frac, note):
        self.progress, self.note = frac, note

    def _run(self):
        try:
            self.report = run(self.ip, self.kind, self._step, self.stop)
            save_report(self.report)
        except Exception as e:  # keep the page alive
            self.error = "The check stopped early (%s: %s). Try again, and send this if it keeps happening." % (
                type(e).__name__, e)
        finally:
            self.progress, self.finished = 1.0, time.time()

    def status(self):
        return {"ip": self.ip, "running": self.finished is None, "progress": round(self.progress, 3), "note": self.note,
                "report": self.report, "error": self.error}


def run(ip, kind=None, step=None, stop=None, lookup=lookup_cves):
    step = step or (lambda f, n: None)
    stopped = lambda: stop is not None and stop.is_set()
    step(0.05, "Checking which doors are open...")
    with ThreadPoolExecutor(max_workers=32) as ex:
        flags = list(ex.map(lambda p: netscan._tcp_open(ip, p, 0.8), ALL_PORTS))
    ports = [p for p, ok in zip(ALL_PORTS, flags) if ok]
    if stopped():
        raise RuntimeError("Stopped.")

    step(0.3, "Reading what software answers on each door...")
    services, software = [], []
    for p in ports:
        entry = {"port": p, "name": PORT_NAMES.get(p, "Port %d" % p), "software": []}
        text = ""
        try:
            # Servers like FTP and SSH speak first; only ask web questions if nothing did (port 5000 can be FTP).
            if p not in (5555, 9100, 515):
                text = _greeting(ip, p, 1.2 if p in WEB_PORTS else 1.5)
            if not text and p in WEB_PORTS:
                server, powered, _ = _http_server(ip, p)
                text = " ".join(x for x in (server, powered) if x)
            if text.startswith("220") and p not in (21, 990):
                entry["name"] = "FTP"
        except Exception as e:  # one odd door must not stop the whole check
            text = ""
            entry["error"] = str(e)[:200]
        entry["greeting"] = text[:200]
        for prod, ver in software_from(text):
            entry["software"].append({"product": prod, "version": ver})
            if (prod, ver) not in software:
                software.append((prod, ver))
        services.append(entry)
        if stopped():
            raise RuntimeError("Stopped.")

    findings = rules(ports, kind)

    ftp_ports = [e["port"] for e in services if e["port"] == 21 or e["greeting"].startswith("220")]
    for p in ftp_ports:
        if stopped():
            break
        step(0.45, "Checking whether FTP lets anyone in...")
        anon = anonymous_ftp(ip, p)
        if anon:
            findings.append(finding("medium", "FTP lets anyone in (port %d)" % p,
                                    "Anyone on your network can open this device's files without a password, and FTP "
                                    "isn't encrypted. For homebrew FTP on a console this is normal while you use it.",
                                    "Set a username and password in the FTP app's settings, or turn the FTP app off "
                                    "when you're not using it.", p))
        elif anon is False:
            findings.append(finding("low", "FTP is on (port %d)" % p, "FTP isn't encrypted, so the login and files "
                                    "cross your network as plain text.", "Turn it off when you don't need it, or use "
                                    "SFTP if the device offers it.", p))

    tls = []
    for p in [x for x in ports if x in TLS_PORTS]:
        if stopped():
            break
        step(0.55, "Checking the secure page on port %d..." % p)
        try:
            t = tls_check(ip, p)
        except Exception as e:
            t = {"port": p, "error": str(e)[:200]}
        tls.append(t)
        if t.get("old_versions"):
            findings.append(finding("medium", "It still accepts old encryption (%s)" % " and ".join(t["old_versions"]),
                                    "TLS 1.0 and 1.1 have known weaknesses, and it usually means old firmware.",
                                    "Update the device's firmware. Some admin pages have a setting for the minimum TLS "
                                    "version: set it to TLS 1.2.", p))
        if t.get("cert") == "expired":
            findings.append(finding("low", "Its secure certificate has expired", "Browsers will warn every time, which "
                                    "trains you to click through warnings.", "Update the firmware or renew the "
                                    "certificate in its settings.", p))
        elif t.get("cert") == "self-signed":
            findings.append(finding("info", "It uses a home-made certificate", "That's normal for home devices. Your "
                                    "browser will show a warning the first time.", "Nothing to do. Accept it once in "
                                    "your browser if you trust the device.", p))

    if software:
        for i, (prod, ver) in enumerate(software):
            if stopped():
                break
            step(0.65 + 0.3 * i / len(software), "Looking up known problems for %s %s..." % (prod, ver or ""))
            if not ver:
                continue
            try:
                cves = lookup(prod, ver)
            except Exception:
                cves = None
            port = next((s["port"] for s in services if any(x["product"] == prod for x in s["software"])), None)
            if cves is None:
                findings.append(finding("info", "Couldn't look up %s %s" % (prod, ver), "The vulnerability database "
                                        "didn't answer.", "Run the check again later.", port))
            elif cves:
                worst = cves[0]["severity"] if cves[0]["severity"] in SEVERITY_ORDER else "medium"
                serious = [c for c in cves if c["score"] >= 7]
                findings.append(finding(worst, "%s %s has %d known security problem%s" % (
                    prod, ver, len(cves), "" if len(cves) == 1 else "s"),
                    (("1 is serious. " if len(serious) == 1 else "%d are serious. " % len(serious)) if serious else "") +
                    "These are published problems for this version. Whether each one affects this device depends on "
                    "how the maker set it up.",
                    "Update the device's firmware or software to the newest version. If there's no update, turn this "
                    "service off if you don't use it.", port, cves[:6]))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    step(1.0, "Done.")
    return {"ip": ip, "kind": kind, "checked": time.time(), "ports": ports, "services": services,
            "tls": tls, "findings": findings}


def save_report(rep):
    try:
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / ("%s.json" % rep["ip"])).write_text(json.dumps(rep))
    except OSError:
        pass


def load_report(ip):
    try:
        return json.loads((REPORTS / ("%s.json" % ip)).read_text())
    except (OSError, ValueError):
        return None


def summary(rep):
    """A plain-text version of the report, for the chat."""
    f = rep.get("findings") or []
    counts = {}
    for x in f:
        counts[x["severity"]] = counts.get(x["severity"], 0) + 1
    head = "Security check of %s: %s." % (rep["ip"], ", ".join("%d %s" % (counts[k], k) for k in SEVERITY_ORDER
                                                               if k in counts) or "nothing found")
    lines = [head, "Open doors: %s." % (", ".join("%d (%s)" % (s["port"], s["name"]) for s in rep["services"]) or "none")]
    for x in f:
        lines.append("- [%s] %s. %s Fix: %s" % (x["severity"].upper(), x["title"], x["why"], x["fix"]))
        for c in x["cves"][:3]:
            lines.append("    %s (score %.1f): %s" % (c["id"], c["score"], c["summary"]))
    return "\n".join(lines)
