"""
Spotting commands that can delete or break things, so PatchBay never runs them by itself and the card asks for an
extra "I understand" tick. It reads the command text only; it doesn't run anything.

risky("rm -rf build")  ->  ["deletes folders and everything in them (rm -r)"]
"""

import re

BULK_DELETE = 20     # deleting this many files (or any folder) on the Files screen counts as a bulk delete

RULES = [
    (r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b", "deletes folders and everything in them (rm -r)"),
    (r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*|--force)\b", "deletes without asking (rm -f)"),
    (r"\brm\b[^;&|]*(\*|\s/(\s|$)|\s~/?(\s|$)|\s\.\.?/?(\s|$))", "deletes with a wildcard, or a whole top folder"),
    (r"(^|[;&|]\s*|\s)rm\s", "deletes files, with no backup (rm)"),
    (r"\b(find)\b[^;&|]*\s-(delete|exec\s+rm)\b", "deletes every file a search finds (find -delete)"),
    (r"\bdd\b[^;&|]*\b(of|if)=", "writes raw data straight onto a disk or file (dd)"),
    (r"\b(mkfs(\.\w+)?|newfs\w*|fdisk|gdisk|parted|wipefs|format)\b", "formats or repartitions a disk"),
    (r"\bdiskutil\s+(erase\w*|partition\w*|zero\w*|secureErase|apfs\s+delete\w*)", "erases a disk (diskutil)"),
    (r"\b(shred|srm)\b", "destroys files so they can't be recovered"),
    (r"\btruncate\b", "cuts files down (truncate)"),
    (r"\bchmod\b[^;&|]*(-[a-zA-Z]*R|--recursive)", "changes permissions on everything in a folder (chmod -R)"),
    (r"\bchmod\b[^;&|]*\b(777|666|a\+w|o\+w)\b", "lets anyone change the files (chmod 777)"),
    (r"\bchmod\b[^;&|]*\s/(\s|$)", "changes permissions of the whole system"),
    (r"\bchown\b[^;&|]*(-[a-zA-Z]*R|--recursive)", "changes the owner of everything in a folder (chown -R)"),
    (r"(^|[^>])>\s*/dev/(sd|hd|disk|nvme|mmcblk|rdisk)", "writes straight onto a disk device"),
    (r"(^|[^>])>\s*/(etc|boot|bin|sbin|usr|System)/", "overwrites a system file"),
    (r"\bmv\b[^;&|]*\s/(\s|$)", "moves the whole system folder"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*[fdx]|push\s+[^;&|]*(--force|-f)\b|checkout\s+--\s+\.)",
     "throws away changes in git (reset --hard / clean / force push)"),
    (r"\b(reboot|shutdown|halt|poweroff|init\s+[06])\b", "restarts or switches off the device"),
    (r"\bkill(all)?\b[^;&|]*\s-(9|KILL)\b|\bkill\s+-?\d*\s*1(\s|$)|\bpkill\b", "force-stops programs"),
    (r":\(\)\s*\{", "a fork bomb (makes the device freeze)"),
    (r"\b(curl|wget)\b[^;&|]*\|\s*(sudo\s+)?(sh|bash|zsh|python\d?)\b", "downloads and runs a script from the internet"),
    (r"\b(launchctl\s+(unload|remove|bootout)|systemctl\s+(disable|mask|stop))\b", "stops or disables a system service"),
    (r"\b(iptables|pfctl|ufw)\b[^;&|]*\b(-F|flush|disable|-X)\b", "changes the firewall"),
    (r"\bpasswd\b|\busermod\b|\buserdel\b", "changes user accounts or passwords"),
    (r"\bsudo\b", "runs as administrator (sudo)"),
]
_COMPILED = [(re.compile(p, re.I), why) for p, why in RULES]


def risky(cmd):
    """Plain reasons this command could delete or break things ([] if none were spotted)."""
    cmd = cmd or ""
    out = []
    for rx, why in _COMPILED:
        if rx.search(cmd) and why not in out:
            out.append(why)
    return out


def bulk_delete(steps):
    """How many files a Files-screen plan deletes, and whether it removes a folder."""
    files = sum(1 for s in steps if s[0] == "delete")
    folders = sum(1 for s in steps if s[0] == "rmdir")
    return files, folders
