"""
What PatchBay knows about each kind of device that runs homebrew: where apps live, what their files are, how to
build them on the Mac, and how to tell the firmware. A few are built in. For anything else, the AI researches it on
the web (generic questions only) and saves what it learned here, so next time it already knows.

Learned notes are saved on the Mac in <data folder>/platforms/<name>.json. Only uses Python's built-in parts.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import core

BUILT_IN = {
    "switch": {
        "name": "Nintendo Switch (Atmosphère)",
        "signs": ["atmosphere", "bootloader", "switch"],
        "apps": "/switch/<name>.nro or /switch/<name>/<name>.nro; background programs (sysmodules) in "
                "/atmosphere/contents/<16-hex id>/exefs.nsp; overlays in /switch/.overlays/*.ovl",
        "files": ".nro (app), .nsp (sysmodule or installable title), .ovl (Tesla overlay), .kip (kernel patch)",
        "toolchain": "devkitPro: devkitA64 + libnx (sudo dkp-pacman -S switch-dev); portlibs as switch-<name>",
        "firmware": "device_firmware (crash/error reports) or setup_firmware_helper for exact versions",
        "sources": "Atmosphere-NX/Atmosphere, switchbrew/libnx",
    },
    "3ds": {
        "name": "Nintendo 3DS (Luma3DS)",
        "signs": ["luma", "3ds", "cias", "gm9"],
        "apps": "/3ds/<name>.3dsx or /3ds/<name>/<name>.3dsx; installable titles are .cia files",
        "files": ".3dsx (Homebrew Launcher app), .cia (installable), .firm (Luma payload)",
        "toolchain": "devkitPro: devkitARM + libctru (sudo dkp-pacman -S 3ds-dev)",
        "firmware": "the system version isn't stored as a plain SD file; check /luma/config.ini and any logs, or ask "
                    "the user to look at System Settings (top right)",
        "sources": "LumaTeam/Luma3DS, devkitPro/libctru",
    },
    "wiiu": {
        "name": "Nintendo Wii U (Aroma / Tiramisu)",
        "signs": ["wiiu", "wiiu/apps", "wiiu/environments"],
        "apps": "/wiiu/apps/<name>.wuhb or .rpx; plugins in /wiiu/environments/aroma/plugins/*.wps",
        "files": ".wuhb / .rpx (apps), .wps (Aroma plugins), .wms (modules)",
        "toolchain": "devkitPro: devkitPPC + wut (sudo dkp-pacman -S wiiu-dev)",
        "firmware": "most consoles run 5.5.x; environment version from /wiiu/environments",
        "sources": "devkitPro/wut, wiiu-env",
    },
    "wii": {
        "name": "Nintendo Wii (Homebrew Channel)",
        "signs": ["apps", "private/wii", "wad"],
        "apps": "/apps/<name>/boot.dol (or boot.elf) with meta.xml and icon.png",
        "files": ".dol / .elf (apps), .wad (installable channel)",
        "toolchain": "devkitPro: devkitPPC + libogc (sudo dkp-pacman -S wii-dev)",
        "firmware": "System Menu version (e.g. 4.3U); not stored as a plain SD file",
        "sources": "devkitPro/libogc",
    },
    "vita": {
        "name": "PlayStation Vita (HENkaku / Ensō)",
        "signs": ["ux0:", "app", "tai", "data", "vpk"],
        "apps": "installed apps in ux0:/app/<TITLEID>/ (eboot.bin, sce_sys/param.sfo); plugins in ux0:/tai or "
                "ur0:/tai (config.txt)",
        "files": ".vpk (installer zip), eboot.bin (app), .suprx / .skprx (plugins)",
        "toolchain": "VitaSDK ($VITASDK, vita-* cmake helpers)",
        "firmware": "often 3.60 or 3.65; read param.sfo or tai config clues",
        "sources": "vitasdk/vdpm",
    },
    "psp": {
        "name": "PlayStation Portable (custom firmware)",
        "signs": ["psp", "psp/game", "seplugins"],
        "apps": "/PSP/GAME/<name>/EBOOT.PBP; plugins in /SEPLUGINS",
        "files": "EBOOT.PBP (app), .prx (plugin)",
        "toolchain": "PSPSDK (pspdev toolchain, psp-config)",
        "firmware": "custom firmware name (PRO / ME / LME) and 6.60/6.61",
        "sources": "pspdev/pspsdk",
    },
    "ps4": {
        "name": "PlayStation 4 (GoldHEN)",
        "signs": ["user", "system_data", "mnt", "data/GoldHEN"],
        "apps": "installed from .pkg files; plugins in /data/GoldHEN/plugins/*.prx",
        "files": ".pkg (installer), .prx / .sprx (plugins), eboot.bin",
        "toolchain": "OpenOrbis PS4 Toolchain ($OO_PS4_TOOLCHAIN)",
        "firmware": "system software version, e.g. 9.00 or 11.00 (FTP greeting or /system_data clues)",
        "sources": "OpenOrbis/OpenOrbis-PS4-Toolchain, GoldHEN",
    },
    "linux-handheld": {
        "name": "Linux handheld / retro device (e.g. ArkOS, ROCKNIX, muOS, Batocera)",
        "signs": ["roms", "storage", "etc", "usr"],
        "apps": "ports and scripts in /roms/ports or /storage/roms/ports; emulators and cores in system folders",
        "files": "shell scripts, ELF programs, .so libretro cores",
        "toolchain": "a cross compiler for the device's CPU (often aarch64 Linux), or build on the device",
        "firmware": "identify_firmware (/etc/os-release and similar files)",
        "sources": "the distribution's own GitHub",
    },
}


def folder(cfg):
    f = core.data_dir(cfg).parent / "_platforms"    # shared by every device
    f.mkdir(parents=True, exist_ok=True)
    return f


def key(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40]


def find(cfg, name):
    """Built-in notes merged with anything learned. Returns (key, notes) or (key, None)."""
    k = key(name)
    for bk, v in BUILT_IN.items():
        if k == bk or k in key(v["name"]) or bk in k:
            k = bk
            break
    notes = dict(BUILT_IN.get(k, {}))
    saved = folder(cfg) / (k + ".json")
    if saved.exists():
        try:
            learned = json.loads(saved.read_text())
            notes.setdefault("learned", {}).update(learned.get("learned", learned))
        except ValueError:
            pass
    return k, (notes or None)


def describe(cfg, name):
    k, notes = find(cfg, name)
    if not notes:
        known = ", ".join(v["name"] for v in BUILT_IN.values())
        learned = ", ".join(p.stem for p in folder(cfg).glob("*.json")) or "none yet"
        return ("Nothing saved about %r yet. Research it with web_search and read_web_page (generic questions only, "
                "e.g. '<platform> homebrew app file format install folder' or '<platform> homebrew toolchain'), "
                "then save what you learn with save_platform_notes.\nBuilt in: %s\nLearned before: %s"
                % (name, known, learned))
    rows = ["Platform: %s (%s)" % (notes.get("name") or name, k)]
    for f, label in (("apps", "Where apps live"), ("files", "File types"), ("toolchain", "Building on the Mac"),
                     ("firmware", "Firmware"), ("sources", "Main projects")):
        if notes.get(f):
            rows.append("%s: %s" % (label, notes[f]))
    for f, v in (notes.get("learned") or {}).items():
        rows.append("Learned, %s: %s" % (f, v))
    rows.append("If something needed is missing here, research it (generic questions only) and save it with "
                "save_platform_notes.")
    return "\n".join(rows)


def save(cfg, name, notes):
    """Save learned facts about a platform (merged with earlier ones). Only writes on the Mac."""
    k = key(name)
    if not k:
        raise ValueError("Give the platform a name, e.g. 'vita' or 'my handheld'.")
    k = find(cfg, name)[0] if find(cfg, name)[1] else k
    if not isinstance(notes, dict) or not notes:
        raise ValueError("Give the notes as an object, e.g. {\"apps\": \"...\", \"toolchain\": \"...\"}.")
    f = folder(cfg) / (k + ".json")
    old = {}
    if f.exists():
        try:
            old = json.loads(f.read_text())
        except ValueError:
            old = {}
    learned = old.get("learned", {})
    for nk, nv in notes.items():
        learned[re.sub(r"[^a-z0-9_ ]", "", str(nk).lower())[:40] or "note"] = str(nv)[:2000]
    old.update({"learned": learned, "updated": time.strftime("%Y-%m-%d %H:%M")})
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, indent=2))
    tmp.replace(f)
    return k, f


def guess(top_folders):
    """Which built-in platform a device's top folders look like, or None."""
    tops = {t.lower().strip("/") for t in top_folders}
    best, score = None, 0
    for k, v in BUILT_IN.items():
        s = sum(1 for sign in v["signs"] if sign.split("/")[0] in tops)
        if s > score:
            best, score = k, s
    return best if score >= 2 or (best == "switch" and "atmosphere" in tops) else None


# ----------------------------------------------------------------- build tools on the Mac

def toolchains():
    """Which homebrew build tools are on the Mac. Only reads."""
    env = dict(os.environ)
    dkp = Path(env.get("DEVKITPRO") or "/opt/devkitpro")
    rows = []
    if dkp.is_dir():
        have = [n for n in ("devkitA64", "devkitARM", "devkitPPC", "libnx", "libctru", "libogc", "wut")
                if (dkp / n).exists()]
        rows.append("devkitPro at %s: %s" % (dkp, ", ".join(have) or "installed, but no toolchains yet"))
    else:
        rows.append("devkitPro: not installed (Nintendo homebrew: https://devkitpro.org/wiki/Getting_Started)")
    for var, what, default in (("VITASDK", "VitaSDK (PS Vita)", "/usr/local/vitasdk"),
                               ("PSPDEV", "PSPSDK (PSP)", "/usr/local/pspdev"),
                               ("OO_PS4_TOOLCHAIN", "OpenOrbis (PS4)", None),
                               ("PS3DEV", "ps3dev (PS3)", "/usr/local/ps3dev")):
        p = env.get(var) or default
        rows.append("%s: %s" % (what, ("found at %s" % p) if p and Path(p).is_dir() else "not found"))
    for tool in ("git", "make", "cmake", "ninja", "meson", "clang", "gcc", "python3", "cargo", "go",
                 "aarch64-linux-gnu-gcc", "arm-linux-gnueabihf-gcc", "docker"):
        p = shutil.which(tool)
        if p:
            try:
                v = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=10,
                                   stdin=subprocess.DEVNULL).stdout.splitlines()[:1]
            except (OSError, subprocess.SubprocessError):
                v = []
            rows.append("%s: %s" % (tool, v[0][:80] if v else p))
    rows.append("Missing tools: research the platform's install steps (generic), then give the user the exact "
                "Terminal commands. Commands needing sudo can't run from the chat.")
    return "\n".join(rows)
