"""
Where a step runs, and what it changes. Every confirm card shows two plain lines:

    Runs on:     Mac Studio (this Mac)
    Changes on:  PS5 (192.168.50.203)

A command typed on the Mac can still reach a device: `make install PS5_HOST=192.168.50.203` builds on the Mac and
then uploads to the PS5 over FTP. This module reads the command text (it never runs anything) and finds those
cases, so a card never says "it doesn't touch the PS5" when it does.

    describe("make install PS5_HOST=192.168.50.203", [("PS5", "192.168.50.203")], "Mac Studio")
      -> {"level": "device", "runs_on": "Mac Studio (this Mac)", "changes_on": "PS5 (192.168.50.203)", ...}

level is "device" (it sends to or changes a device: bold warning), "maybe" (it might, e.g. a bare `make install`:
warning) or "mac" (this Mac only: calm).
"""

import ipaddress
import re

MAC_ONLY = "Nothing outside this Mac"

IP = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")
# Make/env style settings that name where to send things: PS5_HOST=..., SWITCH_IP=..., DEVICE=..., --host ...
HOST_ARG = re.compile(r"(?<![\w$-])([A-Za-z][A-Za-z0-9_]*)=(['\"]?)([^\s'\";&|()$]+)\2")
# The setting's name must have HOST, IP, ADDR... as a whole part: PS5_HOST yes, STRIP or SKIP_TESTS no.
HOST_WORD = re.compile(r"(^|_)(HOST|HOSTNAME|IP|ADDR|ADDRESS|TARGET|SERVER|DEVICE)(_|$)", re.I)
ADDRESS_WORD = re.compile(r"(^|_)(HOST|HOSTNAME|IP|ADDR|ADDRESS|SERVER)(_|$)", re.I)
HOST_FLAG = re.compile(r"(?:^|\s)(?:--host|--ip|--address|--server|--target|--device|-H)(?:=|\s+)(['\"]?)"
                       r"([^\s'\";&|()$]+)\1")
# Addresses written as links. ftp/sftp/scp/smb/ssh always count; http(s) only when it's a local address.
URL = re.compile(r"\b(ftps?|sftp|scp|smb|cifs|ssh|tftp|rsync|https?)://(?:[^@/\s'\"]+@)?(\[[^\]]+\]|[^/:\s'\"]+)", re.I)
# Tools that copy files to or work on another machine.
SEND_TOOL = re.compile(r"(?:^|[;&|(]\s*|\s)(?:\S*/)?(scp|sftp|rsync|ssh|lftp|ftp|ncftpput|ncftp|smbclient|curl|wget|"
                       r"nxlink|3dslink|wiiload|adb|ps4sh|ps5sh|sshpass|nc|netcat|socat|mount_smbfs|mount_afp)\b")
# "user@host:path" or "host:/path" after scp/rsync/sftp/ssh.
REMOTE_PATH = re.compile(r"(?:^|\s)(?:[\w.-]+@)?([\w.-]+):(?!//)(?:/|~|\S)")
SSH_HOST = re.compile(r"\b(?:ssh|sftp)\s+(?:-\S+\s+)*(?:[\w.-]+@)?([\w.-]+)")
# make targets that usually copy the result somewhere.
MAKE_SEND = re.compile(r"\bmake\b[^;&|]*\s(install|send|deploy|upload|push|run|flash|nxlink)\b")
LOCAL_NAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def _is_local_ip(text):
    try:
        ip = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def _is_this_mac(host):
    h = host.lower().strip("[]")
    return h in LOCAL_NAMES or h.startswith("127.")


def _valid_ip(text):
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def find_hosts(cmd):
    """Every other machine the command text points at, as [(host, why)], in order, without repeats."""
    cmd = cmd or ""
    found = []

    def add(host, why):
        host = (host or "").strip().strip("[]").rstrip(".,")
        if not host or _is_this_mac(host) or any(h.lower() == host.lower() for h, _ in found):
            return
        found.append((host, why))

    for m in HOST_ARG.finditer(cmd):
        if not HOST_WORD.search(m.group(1)):
            continue
        value = m.group(3)
        value = re.sub(r"^[a-z]+://", "", value, flags=re.I).split("/")[0]
        host = value.rsplit("@", 1)[-1].split(":")[0] if value.count(":") <= 1 else value
        if _valid_ip(host) or re.match(r"^[A-Za-z][\w-]*(\.[\w-]+)*$", host):
            if not _valid_ip(host) and "." not in host and not host.lower().endswith("local"):
                # DEVICE=switch or TARGET=release: a word, not an address. Only count clear host settings.
                if not ADDRESS_WORD.search(m.group(1)):
                    continue
            add(host, "%s=%s" % (m.group(1), value))
    for m in HOST_FLAG.finditer(cmd):
        add(m.group(2), m.group(0).strip())
    for m in URL.finditer(cmd):
        scheme, host = m.group(1).lower(), m.group(2)
        if scheme.startswith("http") and not (_is_local_ip(host) or host.lower().endswith(".local")):
            continue      # a normal website (github.com and so on), not a device
        add(host, "%s://%s" % (scheme, host))
    tools = {t.lower() for t in SEND_TOOL.findall(cmd)}
    if tools & {"scp", "rsync", "sftp"}:
        for m in REMOTE_PATH.finditer(cmd):
            if not re.match(r"^[A-Za-z]$", m.group(1)):     # skip C:\ style paths
                add(m.group(1), "copies to %s" % m.group(1))
    if tools & {"ssh", "sftp", "sshpass"}:
        for m in SSH_HOST.finditer(cmd):
            if "." in m.group(1) or m.group(1).lower().endswith("local"):
                add(m.group(1), "ssh %s" % m.group(1))
    for m in IP.finditer(cmd):
        if _valid_ip(m.group(1)) and _is_local_ip(m.group(1)):
            add(m.group(1), m.group(1))
    return found


def _match_device(host, devices):
    for name, dev_host in devices:
        if dev_host and host.lower() == str(dev_host).lower():
            return name
    for name, _ in devices:
        if host.lower() in (name.lower(), name.lower() + ".local"):
            return name
    return None


def _guess_label(why):
    """PS5_HOST=... -> "PS5", so an unsaved device still gets a name a person recognises."""
    m = re.match(r"([A-Za-z0-9]+?)_(?:HOST|IP|ADDR|ADDRESS|TARGET|SERVER|DEVICE)", why or "", re.I)
    return m.group(1).upper() if m and m.group(1).upper() not in ("REMOTE", "TARGET", "DEVICE", "MY", "THE") else None


def mac_label(computer):
    computer = (computer or "").strip()
    return "this Mac" if not computer or computer.lower() in ("this mac", "the mac") else "%s (this Mac)" % computer


def describe(cmd, devices=(), computer="this Mac", sends_hint=True):
    """Where a Mac command runs and what it changes. devices = [(name, host)] of the saved devices."""
    targets = []
    for host, why in find_hosts(cmd):
        name = _match_device(host, devices)
        label = name or _guess_label(why)
        targets.append({"name": name or "", "host": host, "why": why,
                        "text": "%s (%s)" % (label, host) if label else "the device at %s" % host})
    tools = {t.lower() for t in SEND_TOOL.findall(cmd or "")}
    out = {"runs_on": mac_label(computer), "targets": targets, "type_word": ""}
    if targets:
        out["level"] = "device"
        out["changes_on"] = ", ".join(t["text"] for t in targets)
        out["note"] = "It runs on this Mac, then sends to %s over the network." % out["changes_on"]
        known = [t["name"] for t in targets if t["name"]]
        out["type_word"] = known[0] if known else ""
        return out
    if sends_hint and (MAKE_SEND.search(cmd or "") or tools & {"nxlink", "3dslink", "wiiload", "adb", "scp", "sftp",
                                                                 "rsync", "ssh", "lftp", "ftp", "ncftpput", "smbclient"}):
        out["level"] = "maybe"
        out["changes_on"] = "Maybe a device. The command doesn't say where it sends, so check before you run it"
        out["note"] = ("This kind of command often copies to a device (for example make install), but it doesn't "
                       "name one.")
        return out
    out["level"] = "mac"
    out["changes_on"] = MAC_ONLY
    return out


def device_label(cfg):
    """'PS5 (192.168.50.203)' for a saved device; a folder device shows its folder."""
    name = cfg.get("device_name") or "the device"
    if cfg.get("protocol") == "folder":
        return "%s (folder %s)" % (name, cfg.get("folder") or "")
    host = cfg.get("host")
    return "%s (%s)" % (name, host) if host else name


def for_device_write(cfg, computer="this Mac"):
    """A file copied, installed, restored or deleted on a device: PatchBay sends it from the Mac."""
    return {"level": "device", "runs_on": mac_label(computer), "changes_on": device_label(cfg),
            "note": "PatchBay sends it from this Mac to %s over %s." % (
                device_label(cfg), (cfg.get("protocol") or "ftp").upper())}


def on_device(cfg):
    """A command run on the device itself (over SSH)."""
    return {"level": "device", "runs_on": device_label(cfg), "changes_on": device_label(cfg)}


def mac_only(computer="this Mac", changes=None, runs=None):
    return {"level": "mac", "runs_on": runs or mac_label(computer), "changes_on": changes or MAC_ONLY}


def rule_text():
    """The wording rule the AI follows, added to its instructions."""
    return ("WHERE THINGS RUN: always say plainly where a step runs and what it changes, e.g. \"This runs on your "
            "Mac, then uploads to your PS5 (192.168.50.203).\" A command run on the Mac still changes a device when "
            "it sends there: PS5_HOST=/SWITCH_IP= style settings, an IP address, ftp/scp/sftp/rsync/curl uploads, "
            "nxlink, or make install/send/deploy. Never say a step \"doesn't touch\" a device when it sends to it. "
            "If you can't tell where make install copies to, say so.")
