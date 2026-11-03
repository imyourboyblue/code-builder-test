"""
Simple names for game files: swap long file names for 0, 1, 2... and put them back later, exactly.

Strip (for a folder you pick, e.g. your Virtual PS5 folder):
  1. Every file is given a number, keeping its extension: "My Game v1.2.fpkg" becomes "0.fpkg".
  2. Inside each file's header (the first few MB), every place a file name is written (UTF-8 or UTF-16) is
     swapped for the number too. The new name is padded with empty bytes to the same length, so no file
     changes size and nothing inside moves.
  3. A map of every original name (file names and the exact header bytes) is saved in PatchBay's own folder
     (data-cache/name-maps), NOT in the game folder, so the real names don't travel with the files.
     The folder only gets a small hidden file (.patchbay-names) with the map's number.

Restore puts every file name and every header byte back, then checks each file is byte-for-byte what it was.

Your own games only: there are no certification checks. Nothing is uploaded anywhere.
Command line:  python3 namestrip.py strip FOLDER [--subfolders] [--scan-mb 4|0]   (0 = whole file)
               python3 namestrip.py restore FOLDER
"""

import hashlib
import json
import os
import sys
import time
import unicodedata
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAPS = HERE / "data-cache" / "name-maps"
MARKER = ".patchbay-names"          # hidden file in the folder: holds only the map's id
SCAN_MB = 4                         # how much of each file counts as "the header"
CHUNK = 8 * 1024 * 1024
MIN_STEM = 4                        # shorter names without their extension ("a", "bin") are too common to swap


class StripError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def _folder(path):
    p = Path(os.path.expanduser(str(path or "").strip())).resolve()
    if not p.is_dir():
        raise StripError("There's no folder at %s." % path)
    if p == Path("/") or p == Path.home().resolve():
        raise StripError("Pick a game folder, not your whole disk or home folder.")
    return p


def _files(folder, subfolders):
    """Files to rename, as paths relative to the folder, in a fixed order. Hidden files and links are left alone."""
    found = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if subfolders else []
        for n in names:
            p = Path(root) / n
            if not n.startswith(".") and p.is_file() and not p.is_symlink():
                found.append(p.relative_to(folder).as_posix())
    return sorted(found, key=lambda r: (r.count("/"), r.casefold(), r))


def _new_name(i, rel):
    return str(i) + Path(rel).suffix


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _is_word(b):
    return b is not None and (48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122)


def _variants(names):
    """(text to find, its replacement, whole word only) -> list of (find bytes, padded replacement, encoding, word).
    Full file names are swapped wherever they appear; names without their extension only as whole words."""
    out, seen = [], set()
    for old, new, word in names:
        for form in {unicodedata.normalize("NFC", old), unicodedata.normalize("NFD", old)}:
            for enc in ("utf-8", "utf-16-le"):
                find, rep = form.encode(enc), new.encode(enc)
                if find in seen or len(rep) > len(find):
                    continue
                seen.add(find)
                out.append((find, rep + b"\0" * (len(find) - len(rep)), enc, word))
    return out


def _search(path, variants, scan):
    """Find whole-name matches in the first `scan` bytes (0 = the whole file). Returns (matches, region hash, size).
    Each match is (offset, find, replacement, encoding). Longer names win where two overlap."""
    size = path.stat().st_size
    end = size if not scan else min(size, scan)
    longest = max((len(v[0]) for v in variants), default=1)
    hits, h, pos = {}, hashlib.sha256(), 0
    with open(path, "rb") as f:
        tail = b""
        while pos < end:
            block = f.read(min(CHUNK, end - pos))
            if not block:
                break
            h.update(block)
            data, base = tail + block, pos - len(tail)
            for find, rep, enc, word in variants:
                step = 2 if enc == "utf-16-le" else 1
                i = data.find(find)
                while i >= 0:
                    off = base + i
                    before = data[i - step] if i - step >= 0 and (step == 1 or data[i - 1] == 0) else None
                    j = i + len(find)
                    after = data[j] if j < len(data) and (step == 1 or (j + 1 < len(data) and data[j + 1] == 0)) \
                        else None
                    # a match touching the edge of a block is judged in the block where it isn't at the edge
                    whole = (j + 2 <= len(data) or pos + len(block) >= end) and (i >= 2 or base == 0)
                    if whole and not (word and (_is_word(before) or _is_word(after))):
                        if len(find) > len(hits.get(off, (b"",))[0]):
                            hits[off] = (find, rep, enc)
                    i = data.find(find, i + 1)
            pos += len(block)
            tail = data[-(longest + 2):]
    taken, matches = -1, []
    for off in sorted(hits, key=lambda o: (o, -len(hits[o][0]))):
        find, rep, enc = hits[off]
        if off >= taken:
            matches.append((off, find, rep, enc))
            taken = off + len(find)
    return matches, h.hexdigest(), size


def _region_hash(path, length, patches=()):
    """sha256 of the first `length` bytes, with `patches` [(offset, bytes)] laid over it first."""
    h, pos = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while pos < length:
            block = bytearray(f.read(min(CHUNK, length - pos)))
            if not block:
                break
            for off, b in patches:
                lo, hi = max(off, pos), min(off + len(b), pos + len(block))
                if lo < hi:
                    block[lo - pos:hi - pos] = b[lo - off:hi - off]
            h.update(block)
            pos += len(block)
    return h.hexdigest()


def _rewrite(path, off, expect, new):
    with open(path, "r+b") as f:
        f.seek(off)
        now = f.read(len(expect))
        if now == new:
            return
        if now != expect:
            raise StripError("%s changed since it was checked (at byte %d). Nothing more was changed in it."
                             % (path.name, off))
        f.seek(off)
        f.write(new)


# ---------------------------------------------------------------- map lookup

def map_path(folder, maps=MAPS):
    """The map for a folder that has simple names, or None."""
    marker = Path(folder) / MARKER
    if not marker.is_file():
        return None
    mid = marker.read_text("utf-8", "replace").strip()
    if not mid or "/" in mid or "\\" in mid or mid.startswith("."):
        raise StripError("The hidden %s file in this folder looks damaged." % MARKER)
    p = Path(maps) / (mid + ".json")
    if not p.is_file():
        raise StripError("This folder has simple names, but its map (%s) is missing from %s." % (p.name, maps))
    return p


def status(folder, maps=MAPS):
    folder = _folder(folder)
    mp = map_path(folder, maps)
    if not mp:
        return {"folder": str(folder), "stripped": False}
    m = json.loads(mp.read_text("utf-8"))
    return {"folder": str(folder), "stripped": True, "state": m["state"], "made": m["made"], "map": str(mp),
            "files": len(m["files"]), "header_changes": len(m["header_changes"])}


# ---------------------------------------------------------------- strip

def plan(folder, subfolders=False, scan_mb=SCAN_MB):
    """What a strip would do. Changes nothing."""
    folder = _folder(folder)
    if (folder / MARKER).exists():
        raise StripError("This folder already has simple names. Put the names back first.")
    rels = _files(folder, subfolders)
    if not rels:
        raise StripError("There are no files in %s." % folder)
    files = [{"index": i, "original": r, "stripped": (Path(r).parent / _new_name(i, r)).as_posix()}
             for i, r in enumerate(rels)]
    targets = {f["stripped"] for f in files}
    for t in targets - set(rels):
        if os.path.lexists(folder / t):
            raise StripError("Something not on the list is already called %s, so PatchBay won't overwrite it." % t)
    names = []
    for f in files:
        name, stem, i = Path(f["original"]).name, Path(f["original"]).stem, f["index"]
        if name != _new_name(i, name):
            names.append((name, _new_name(i, name), False))
        if len(stem) >= MIN_STEM and stem != name and stem != str(i):
            names.append((stem, str(i), True))
    names.sort(key=lambda n: -len(n[0]))
    variants = _variants(names)
    try:
        scan = int(float(scan_mb or 0) * 1024 * 1024)
    except (TypeError, ValueError):
        scan = -1
    if scan < 0:
        raise StripError("The header size should be a number of MB (or 0 for the whole file).")
    changes = []
    for f in files:
        p = folder / f["original"]
        matches, digest, size = _search(p, variants, scan)
        f.update(size=size, scanned=size if not scan else min(size, scan), scan_sha256=digest)
        for off, find, rep, enc in matches:
            changes.append({"file": f["index"], "offset": off, "original_hex": find.hex(), "new_hex": rep.hex(),
                            "encoding": enc, "name": find.decode(enc)})
    return {"patchbay_names": 1, "folder": str(folder), "subfolders": bool(subfolders), "scan_mb": scan_mb,
            "files": files, "header_changes": changes}


def preview_text(p, limit=60):
    """A plain list for a card."""
    nf, nh = len(p["files"]), len(p["header_changes"])
    lines = ["%d file%s, %d name%s swapped inside headers." % (
        nf, " gets a new name" if nf == 1 else "s get new names", nh, "" if nh == 1 else "s")]
    for f in p["files"][:limit]:
        n = sum(1 for c in p["header_changes"] if c["file"] == f["index"])
        lines.append("%s  ->  %s%s" % (f["original"], f["stripped"], "   (%d in header)" % n if n else ""))
    if len(p["files"]) > limit:
        lines.append("...and %d more." % (len(p["files"]) - limit))
    return "\n".join(lines)


def _same(a, b):
    key = lambda p: [(f["original"], f["size"]) for f in p["files"]]
    return key(a) == key(b) and len(a["header_changes"]) == len(b["header_changes"])


def strip(folder, subfolders=False, scan_mb=SCAN_MB, expect=None, maps=MAPS):
    """Do it. `expect` is the plan the user saw: if the folder changed since, nothing happens."""
    p = plan(folder, subfolders, scan_mb)
    if expect is not None and not _same(p, expect):
        raise StripError("The folder changed since the preview. Look at the preview again.")
    folder = Path(p["folder"])
    mid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    mp = Path(maps) / (mid + ".json")
    p.update(id=mid, made=time.strftime("%Y-%m-%d %H:%M:%S"), state="stripping")
    _write_json(mp, p)                                  # the map is safe before anything changes
    (folder / MARKER).write_text(mid + "\n", encoding="utf-8")
    by_index = {f["index"]: f for f in p["files"]}
    for c in p["header_changes"]:
        _rewrite(folder / by_index[c["file"]]["original"], c["offset"], bytes.fromhex(c["original_hex"]),
                 bytes.fromhex(c["new_hex"]))
    _rename_all(folder, [(f["original"], f["stripped"], f["index"]) for f in p["files"]])
    p["state"] = "stripped"
    _write_json(mp, p)
    problems = verify(folder, p)
    if problems:
        raise StripError("Stripped, but a check failed, so press Put names back: " + "; ".join(problems[:5]))
    return {"map": str(mp), "files": len(p["files"]), "header_changes": len(p["header_changes"]),
            "text": "Gave %d file%s simple names and swapped %d name%s in headers. The map is saved in %s." % (
                len(p["files"]), "" if len(p["files"]) == 1 else "s", len(p["header_changes"]),
                "" if len(p["header_changes"]) == 1 else "s", mp)}


def _rename_all(folder, moves):
    """Rename (from, to, index) in two steps via hidden temporary names, so names that swap places never clash.
    Files only move within their own folder."""
    tmps = []
    for src, dst, i in moves:
        a, t = folder / src, folder / Path(dst).parent / (".patchbay-tmp-%d" % i)
        if a != t:
            if os.path.lexists(t):
                raise StripError("A leftover temporary file is in the way: %s" % t)
            os.rename(a, t)
        tmps.append((t, folder / dst))
    for t, b in tmps:
        if os.path.lexists(b):
            raise StripError("Something is already called %s, so PatchBay won't overwrite it." % b.name)
        os.rename(t, b)


def verify(folder, m):
    """Check every stripped file turns back into the original exactly (sizes, plus header bytes). [] = all good."""
    problems = []
    for f in m["files"]:
        p = Path(folder) / f["stripped"]
        if not p.is_file():
            problems.append("%s is missing" % f["stripped"])
            continue
        if p.stat().st_size != f["size"]:
            problems.append("%s changed size" % f["stripped"])
            continue
        back = [(c["offset"], bytes.fromhex(c["original_hex"])) for c in m["header_changes"]
                if c["file"] == f["index"]]
        if _region_hash(p, f["scanned"], back) != f["scan_sha256"]:
            problems.append("%s wouldn't come back exactly" % f["stripped"])
    return problems


# ---------------------------------------------------------------- restore

def _is_file(path, f, back):
    """True if the file at path is file f (stripped or not): same size and same header once put back."""
    return path.is_file() and not path.is_symlink() and path.stat().st_size == f["size"] and \
        _region_hash(path, f["scanned"], back) == f["scan_sha256"]


def restore(folder, maps=MAPS):
    """Put back every file name and header byte, check them, then move the map to the 'restored' list.
    Each file is found by its contents (at its number, a temporary name or its old name), so this also
    finishes the job after a strip that was cut off halfway."""
    folder = _folder(folder)
    mp = map_path(folder, maps)
    if not mp:
        raise StripError("This folder doesn't have simple names (there's no %s file in it)." % MARKER)
    m = json.loads(mp.read_text("utf-8"))
    backs = {f["index"]: [(c["offset"], bytes.fromhex(c["original_hex"])) for c in m["header_changes"]
                          if c["file"] == f["index"]] for f in m["files"]}
    where, used = {}, set()
    for f in m["files"]:
        tmp = (Path(f["original"]).parent / (".patchbay-tmp-%d" % f["index"])).as_posix()
        for rel in (tmp, f["stripped"], f["original"]):
            if rel not in used and _is_file(folder / rel, f, backs[f["index"]]):
                where[f["index"]] = rel
                used.add(rel)
                break
        else:
            raise StripError("Couldn't find %s (now called %s). Nothing was changed." % (f["original"], f["stripped"]))
    for f in m["files"]:                              # nothing new may sit at an original name
        o = f["original"]
        if o not in used and os.path.lexists(folder / o):
            raise StripError("Something new is already called %s, so PatchBay won't overwrite it. Move it out "
                             "first. Nothing was changed." % o)
    _rename_all(folder, [(where[f["index"]], f["original"], f["index"]) for f in m["files"]])
    by_index = {f["index"]: f for f in m["files"]}
    for c in m["header_changes"]:
        _rewrite(folder / by_index[c["file"]]["original"], c["offset"], bytes.fromhex(c["new_hex"]),
                 bytes.fromhex(c["original_hex"]))
    problems = []
    for f in m["files"]:
        p = folder / f["original"]
        if not p.is_file():
            problems.append("%s is missing" % f["original"])
        elif p.stat().st_size != f["size"] or _region_hash(p, f["scanned"]) != f["scan_sha256"]:
            problems.append("%s isn't exactly what it was" % f["original"])
    if problems:
        raise StripError("Names are back, but a check failed (the map is kept): " + "; ".join(problems[:5]))
    (folder / MARKER).unlink()
    done = Path(maps) / "restored"
    done.mkdir(parents=True, exist_ok=True)
    mp.replace(done / mp.name)
    return {"files": len(m["files"]), "text": "Put back %d original name%s and every header byte. Every file "
            "checked out exactly as it was." % (len(m["files"]), "" if len(m["files"]) == 1 else "s")}


def recent(maps=MAPS, limit=10):
    """Folders that have simple names right now, newest first."""
    rows = []
    for p in sorted(Path(maps).glob("*.json"), reverse=True)[:limit]:
        try:
            m = json.loads(p.read_text("utf-8"))
            rows.append({"folder": m["folder"], "made": m["made"], "files": len(m["files"]), "state": m["state"]})
        except (OSError, ValueError, KeyError):
            continue
    return rows


# ---------------------------------------------------------------- command line

def main(argv):
    if len(argv) < 2 or argv[0] not in ("strip", "restore", "preview"):
        print(__doc__.strip())
        return 1
    try:
        if argv[0] == "restore":
            print(restore(argv[1])["text"])
            return 0
        scan = SCAN_MB
        if "--scan-mb" in argv:
            scan = float(argv[argv.index("--scan-mb") + 1])
        p = plan(argv[1], "--subfolders" in argv, scan)
        print(preview_text(p))
        if argv[0] == "preview":
            return 0
        if input("\nType the folder's name (%s) to go ahead: " % Path(p["folder"]).name).strip() != \
                Path(p["folder"]).name:
            print("Nothing was changed.")
            return 1
        print(strip(argv[1], "--subfolders" in argv, scan, expect=p)["text"])
        return 0
    except (StripError, OSError) as e:
        print("Stopped: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
