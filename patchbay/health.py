"""
Health checks for every saved device, shown as alerts on the home screen. They run when PatchBay's home screen opens
and when the user presses "Check health", never on a timer.

Per device: is it on the network, how old is its newest complete backup, did the last scan find damaged files, is
writing left on, did the last security check find serious problems, and (for SSH devices, only when asked for a
deep check) how full its disk is. Each alert says what to do and, where it can, which button does it.
"""

import json
import re
import time

import backup
import core
import securitycheck

BACKUP_WARN_DAYS = 14
DISK_WARN = 90


def alert(level, device, title, advice, action=None):
    return {"level": level, "device": device, "title": title, "advice": advice, "action": action}


def _latest_scan(cfg):
    reports = sorted((core.data_dir(cfg) / "reports").glob("*.json"))
    if not reports:
        return None
    try:
        return json.loads(reports[-1].read_text())
    except (OSError, ValueError):
        return None


def _disk(cfg):
    """Fullest disk on an SSH device as (percent, mount), or None."""
    dev = core.Device(cfg)
    try:
        code, out = dev.run_command("df -P", timeout=20)
    except (SystemExit, Exception):
        return None
    finally:
        dev.close()
    if code != 0:
        return None
    worst = None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6 and parts[4].endswith("%") and parts[4][:-1].isdigit():
            mount = parts[5]
            if re.match(r"^/(dev|run|sys|proc|snap)(/|$)", mount) or parts[0] in ("tmpfs", "devtmpfs", "overlay"):
                continue
            pct = int(parts[4][:-1])
            if worst is None or pct > worst[0]:
                worst = (pct, mount)
    return worst


def check(registry, deep=False):
    """Alerts for every saved device, worst first."""
    registry.refresh(force=deep)
    out = []
    now = time.time()
    for f, cfg in registry.all():
        name = cfg["device_name"]
        online = bool(registry.status.get(name, {}).get("online"))
        if not online:
            out.append(alert("info", name, "%s isn't on the network right now" % name,
                             "Switch it on (and start its FTP/SSH app if it needs one). The other checks still use "
                             "what PatchBay saved last time."))
        backups = [b for b in backup.list_backups(cfg) if b.get("complete") and not b.get("partial")]
        if not backups:
            out.append(alert("warn", name, "%s has never had a full backup" % name,
                             "Open it, go to the Backups tab and press Back up now. Then anything that breaks can be "
                             "put back.", {"go": "backups"}))
        else:
            newest = backups[0]
            try:
                age = (now - time.mktime(time.strptime(newest["name"][:19], "%Y-%m-%d_%H-%M-%S"))) / 86400
            except ValueError:
                age = 0
            if age > BACKUP_WARN_DAYS:
                out.append(alert("warn", name, "%s's newest full backup is %d days old" % (name, age),
                                 "Make a fresh one in its Backups tab (unchanged files are linked, so it's quick).",
                                 {"go": "backups"}))
        rep = _latest_scan(cfg)
        if rep:
            bad = [x for x in rep.get("files", []) if any(p["level"] == "error" for p in x.get("problems", []))]
            if bad:
                out.append(alert("bad", name, "The last scan of %s found %d damaged file%s" % (
                    name, len(bad), "" if len(bad) == 1 else "s"),
                    "Ask in its chat: \"what did the last scan find, and can you fix it?\" PatchBay can put back "
                    "good copies.", {"go": "chat", "ask": "What did the last scan find, and can you fix it?"}))
        if cfg.get("allow_writes"):
            out.append(alert("info", name, "Writing to %s is on" % name,
                             "That's fine while you're working on it. Turn it off when you're done: ask its chat "
                             "\"turn off writing\".", {"go": "chat", "ask": "Turn off writing for this device."}))
        if cfg.get("host") and cfg.get("protocol") != "folder":
            sec = securitycheck.load_report(cfg["host"])
            serious = [x for x in (sec or {}).get("findings", []) if x["severity"] in ("critical", "high")]
            if serious:
                out.append(alert("bad", name, "The last security check of %s found %d serious problem%s" % (
                    name, len(serious), "" if len(serious) == 1 else "s"),
                    "Worst: %s. Open Find devices, then the device's Security check, and use Help me fix this."
                    % serious[0]["title"], {"go": "security", "ip": cfg["host"]}))
        if deep and online and cfg.get("protocol") == "sftp":
            d = _disk(cfg)
            if d and d[0] >= DISK_WARN:
                out.append(alert("bad" if d[0] >= 97 else "warn", name, "%s's disk %s is %d%% full" % (name, d[1], d[0]),
                                 "Full disks are a common cause of half-written, damaged files. Ask its chat what "
                                 "takes the most space.", {"go": "chat", "ask": "My disk %s is %d%% full. What takes "
                                                           "the most space, and what can I safely remove?" % (d[1], d[0])}))
    try:
        import selfedit
        rec = selfedit.last_recovery()
    except Exception:
        rec = None
    if rec:
        out.append(alert("warn", "PatchBay", "PatchBay put back an earlier version of itself",
                         rec + " You can ask the home chat to try that change again in a different way.",
                         {"go": "chat", "ask": "PatchBay couldn't start after a change to itself and put the old "
                                               "version back. What went wrong, and can you redo it safely?"}))
    order = {"bad": 0, "warn": 1, "info": 2}
    out.sort(key=lambda a: order[a["level"]])
    return out
