"""
Your saved devices: remembers every device you've added, checks which ones are
on the network right now, and keeps each one's picture.

Each device is one settings file:
  - config.json             your first device (GD1)
  - devices/<name>.json     every device you add after that

Only uses Python's built-in parts.
"""

import json
import re
import socket
import threading
import time
from pathlib import Path

import gd1check as core

NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")
CHECK_EVERY = 8          # seconds between network checks of the same device
PORTRAIT_LIMIT = 3 * 1024 * 1024

GD1_ABOUT = ("an embedded FreeBSD-based node reached over FTP. A program on it sometimes freezes during heavy "
             "writes (auto-save or database update), which leaves truncated files, broken headers or null padding.")

# Settings a new device starts with. Shared settings (AI model, data folder) come from config.json.
NEW_DEVICE = {
    "device_name": "", "host": "", "ports": [21, 5000], "username": "anonymous", "password": "anonymous@",
    "passive_mode": True, "timeout_seconds": 20, "remote_folders": ["/"], "include": ["*"],
    "exclude": ["*.mp4", "*.wav", "*.iso"], "max_file_mb": 200, "max_files": 5000, "allow_writes": False,
    "about": "",
}


class DeviceError(Exception):
    pass


def about_text(cfg):
    if cfg.get("about"):
        return cfg["about"]
    return GD1_ABOUT if cfg["device_name"] == "GD1" else "a device on the local network, reached over FTP."


def port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


class Registry:
    def __init__(self, main_config):
        self.main = Path(main_config)
        self.lock = threading.Lock()
        self.status = {}     # name -> {"online": bool, "checked": time}

    # ---- reading

    def all(self):
        """Every saved device as (settings file, settings). Broken files are skipped."""
        out, seen = [], set()
        for f in core.device_config_files(self.main):
            try:
                cfg = core.load_config(f)
            except (SystemExit, ValueError, OSError):
                continue
            key = cfg["device_name"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((f, cfg))
        return out

    def path_for(self, name):
        for f, cfg in self.all():
            if cfg["device_name"].lower() == (name or "").lower():
                return f
        raise DeviceError("There's no saved device called %s." % name)

    # ---- who is on the network

    def _check(self, cfg):
        online = any(port_open(cfg["host"], p) for p in cfg["ports"])
        now = time.time()
        with self.lock:
            self.status[cfg["device_name"]] = {"online": online, "checked": now}
        if online:
            self._remember(cfg, last_seen=now)
        return online

    def refresh(self, force=False):
        """Check every device that hasn't been checked in the last few seconds, all at once."""
        jobs = []
        for f, cfg in self.all():
            st = self.status.get(cfg["device_name"])
            if force or not st or time.time() - st["checked"] > CHECK_EVERY:
                t = threading.Thread(target=self._check, args=(cfg,), daemon=True)
                t.start()
                jobs.append(t)
        for t in jobs:
            t.join(6)

    def summary(self, force=False):
        self.refresh(force)
        rows = []
        for f, cfg in self.all():
            st = self.status.get(cfg["device_name"], {})
            seen = self._seen(cfg)
            rows.append({"name": cfg["device_name"], "host": cfg["host"], "online": bool(st.get("online")),
                         "last_seen": seen.get("last_seen"), "writes_on": bool(cfg.get("allow_writes")),
                         "has_portrait": self.portrait_file(cfg) is not None,
                         "portrait_version": seen.get("portrait_version", 0),
                         "settings_file": str(f.relative_to(core.HERE)) if f.is_relative_to(core.HERE) else str(f)})
        return rows

    # ---- remembered facts, kept in the device's data folder

    def _seen_path(self, cfg):
        return core.data_dir(cfg) / "device.json"

    def _seen(self, cfg):
        p = self._seen_path(cfg)
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def _remember(self, cfg, **facts):
        with self.lock:
            seen = self._seen(cfg)
            seen.update(facts)
            self._seen_path(cfg).write_text(json.dumps(seen, indent=2))

    # ---- adding a device

    def add(self, name, host, ports, username, password, about=""):
        name, host = (name or "").strip(), (host or "").strip()
        if not NAME_OK.match(name):
            raise DeviceError("Give the device a short name using letters, numbers, spaces, - or _ (like GD2).")
        if any(cfg["device_name"].lower() == name.lower() for f, cfg in self.all()):
            raise DeviceError("You already have a device called %s." % name)
        if not re.match(r"^[A-Za-z0-9.\-:\[\]]{1,255}$", host):
            raise DeviceError("That doesn't look like an IP address (it should look like 192.168.1.60).")
        try:
            ports = [int(p) for p in (ports or [21, 5000])]
            assert ports and all(0 < p < 65536 for p in ports)
        except (ValueError, TypeError, AssertionError):
            raise DeviceError("Ports should be numbers like 21, 5000.")
        cfg = dict(NEW_DEVICE, device_name=name, host=host, ports=ports, about=about or "",
                   username=username or "anonymous", password=password or "anonymous@")
        folder = self.main.parent / "devices"
        folder.mkdir(exist_ok=True)
        f = folder / (re.sub(r"[^A-Za-z0-9_-]", "_", name) + ".json")
        n = 2
        while f.exists():
            f = folder / ("%s_%d.json" % (re.sub(r"[^A-Za-z0-9_-]", "_", name), n))
            n += 1
        f.write_text(json.dumps(cfg, indent=2) + "\n")
        self.status.pop(name, None)
        return f

    # ---- pictures

    def portrait_file(self, cfg):
        for ext in ("jpg", "png"):
            p = core.data_dir(cfg) / ("portrait." + ext)
            if p.exists():
                return p
        return None

    def set_portrait(self, cfg, data):
        if len(data) > PORTRAIT_LIMIT:
            raise DeviceError("That picture is too big (3 MB at most).")
        if data.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        else:
            raise DeviceError("Use a JPEG or PNG picture.")
        for old in ("jpg", "png"):
            (core.data_dir(cfg) / ("portrait." + old)).unlink(missing_ok=True)
        (core.data_dir(cfg) / ("portrait." + ext)).write_bytes(data)
        self._remember(cfg, portrait_version=int(time.time()))
