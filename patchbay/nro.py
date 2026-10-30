"""
.nro files: the homebrew program format used by the Switch (and devices like it).

An .nro is laid out like this:
  0x00    a small start header (where the MOD0 marker is)
  0x10    the NRO header: "NRO0", total program size, and where the
          code (text), read-only data (ro) and data sections are
  ...     the program itself
  size    optional ASSET section: "ASET", then the icon (JPEG), the NACP
          (name, author, version) and a RomFS (the program's built-in files)

This file can read all of that, spot crash damage in it, and make a new .nro
with a different name, author, version or icon. It can't change the program
code itself: that's compiled, so you change the source code and rebuild.

Only uses Python's built-in parts.
"""

import struct

NRO_MAGIC = b"NRO0"
ASET_MAGIC = b"ASET"
MOD0_MAGIC = b"MOD0"
HEADER_END = 0x80
ASET_HEADER = 0x38
NACP_SIZE = 0x4000
LANGS = ["American English", "British English", "Japanese", "French", "German", "Latin American Spanish", "Spanish",
         "Italian", "Dutch", "Canadian French", "Portuguese", "Russian", "Korean", "Traditional Chinese",
         "Simplified Chinese", "Brazilian Portuguese"]


def p(level, text):
    return {"level": level, "text": text}


def looks_like_nro(data):
    return len(data) >= 0x14 and data[0x10:0x14] == NRO_MAGIC


def _cstr(b):
    return b.split(b"\x00", 1)[0].decode("utf-8", "replace")


def parse_nacp(nacp):
    out = {"titles": {}}
    for i, lang in enumerate(LANGS):
        e = nacp[i * 0x300:(i + 1) * 0x300]
        name, author = _cstr(e[:0x200]), _cstr(e[0x200:0x300])
        if name or author:
            out["titles"][lang] = {"name": name, "author": author}
    first = next(iter(out["titles"].values()), {"name": "", "author": ""})
    out["name"], out["author"] = first["name"], first["author"]
    out["version"] = _cstr(nacp[0x3060:0x3070])
    return out


def inspect(data):
    """Everything about an .nro, plus a list of problems. Never raises on damaged files."""
    info, probs = {"file_size": len(data)}, []
    n = len(data)
    if n < HEADER_END:
        probs.append(p("error", "Only %d bytes: far too short for an .nro (the header alone is 128 bytes). "
                                "The file was cut off." % n))
        return info, probs
    if data[0x10:0x14] != NRO_MAGIC:
        head = data[:HEADER_END]
        if not head.strip(b"\x00"):
            probs.append(p("error", "The first 128 bytes are all zeros. The header was wiped, so this .nro won't start."))
        else:
            probs.append(p("error", "The NRO0 marker at byte 0x10 is missing (found %s). The header is damaged or this "
                                    "isn't an .nro." % data[0x10:0x14].hex(" ")))
        return info, probs

    mod0_off, = struct.unpack_from("<I", data, 0x4)
    version, size, flags = struct.unpack_from("<III", data, 0x14)
    segs = {}
    for i, name in enumerate(("text", "ro", "data")):
        off, sz = struct.unpack_from("<II", data, 0x20 + i * 8)
        segs[name] = {"offset": off, "size": sz}
    bss, = struct.unpack_from("<I", data, 0x38)
    info.update({"nro_size": size, "format_version": version, "flags": flags, "segments": segs, "bss_size": bss,
                 "build_id": data[0x40:0x60].rstrip(b"\x00").hex() or "(none)"})

    if size < HEADER_END:
        probs.append(p("error", "The header says the program is only %d bytes. The header is damaged." % size))
        return info, probs
    if size > n:
        probs.append(p("error", "The header says the program is %d bytes, but the file is only %d. The last %d bytes "
                                "are missing: the file was cut off while it was being written." % (size, n, size - n)))
    end = 0
    for name, s in segs.items():
        stop = s["offset"] + s["size"]
        end = max(end, stop)
        if stop > size:
            probs.append(p("error", "The %s section runs past the end of the program (%d > %d). The header is "
                                    "damaged." % (name, stop, size)))
        elif stop > n:
            probs.append(p("error", "The %s section is cut off (needs bytes up to %d, file stops at %d)." % (name, stop, n)))
    t = segs["text"]
    if t["size"] == 0:
        probs.append(p("error", "The code (text) section is empty. The header is damaged."))
    else:
        code = data[t["offset"]:min(n, t["offset"] + t["size"])]
        if code and not code.strip(b"\x00"):
            probs.append(p("error", "The program code is all zero bytes. It was never written (null padding)."))
        else:
            run = _longest_zero_run(code)
            if run * 2 >= len(code):
                probs.append(p("error", "Over half of the program code (%d bytes in a row) is zero bytes. It was "
                                        "never fully written (null padding)." % run))
            elif run >= 65536:
                probs.append(p("warning", "The program code has a block of %d zero bytes. Part of it may be missing." % run))
    if mod0_off and mod0_off + 4 <= n and data[mod0_off:mod0_off + 4] != MOD0_MAGIC:
        probs.append(p("warning", "The MOD0 marker isn't where the header says (byte 0x%x). The start of the code may "
                                  "be damaged." % mod0_off))

    # Asset section: icon, name/version, built-in files.
    if n > size:
        a = data[size:]
        if len(a) >= ASET_HEADER and a[:4] == ASET_MAGIC:
            entries = {}
            for i, name in enumerate(("icon", "nacp", "romfs")):
                off, sz = struct.unpack_from("<QQ", a, 0x8 + i * 16)
                entries[name] = {"offset": off, "size": sz}
                if sz and off + sz > len(a):
                    probs.append(p("error", "The %s inside the asset section is cut off (needs %d bytes, only %d there). "
                                            "The end of the file is missing." % (name, off + sz, len(a))))
            info["assets"] = entries
            ic = entries["icon"]
            if ic["size"]:
                icon = a[ic["offset"]:ic["offset"] + ic["size"]]
                info["icon_bytes"] = len(icon)
                if not icon.startswith(b"\xff\xd8\xff"):
                    probs.append(p("error", "The icon isn't a valid JPEG (it starts with %s). It's damaged."
                                            % icon[:4].hex(" ")))
            nc = entries["nacp"]
            if nc["size"]:
                if nc["size"] != NACP_SIZE:
                    probs.append(p("warning", "The name/version block (NACP) is %d bytes instead of %d." % (nc["size"], NACP_SIZE)))
                nacp = a[nc["offset"]:nc["offset"] + nc["size"]]
                if len(nacp) >= 0x3070:
                    if not nacp.strip(b"\x00"):
                        probs.append(p("error", "The name/version block (NACP) is all zeros. It was never written."))
                    else:
                        info.update(parse_nacp(nacp))
            rf = entries["romfs"]
            if rf["size"]:
                rom = a[rf["offset"]:rf["offset"] + min(rf["size"], 16)]
                if len(rom) >= 8 and struct.unpack_from("<Q", rom)[0] != 0x50:
                    probs.append(p("warning", "The built-in files (RomFS) don't start the usual way. They may be damaged."))
        elif not a.strip(b"\x00"):
            probs.append(p("error", "After the program there are %d zero bytes where the icon and name should be "
                                    "(null padding). The save stopped part way." % len(a)))
        else:
            probs.append(p("warning", "There are %d extra bytes after the program, but no ASET marker. The icon/name "
                                      "section may be damaged." % len(a)))
    return info, probs


def _longest_zero_run(b):
    best = cur = 0
    i, n = 0, len(b)
    while i < n:
        j = b.find(b"\x00" * 4096, i)
        if j < 0:
            break
        k = j
        while k < n and b[k] == 0:
            k += 1
        best = max(best, k - j)
        i = k
    return best


def describe(data):
    info, probs = inspect(data)
    lines = []
    if "nro_size" in info:
        lines.append("Program: %s %s" % (info.get("name") or "(no name)", ("v" + info["version"]) if info.get("version") else ""))
        if info.get("author"):
            lines.append("Author: %s" % info["author"])
        lines.append("Sizes: file %d bytes, program %d bytes (code %d, read-only data %d, data %d, bss %d)" % (
            info["file_size"], info["nro_size"], info["segments"]["text"]["size"], info["segments"]["ro"]["size"],
            info["segments"]["data"]["size"], info["bss_size"]))
        lines.append("Build ID: %s" % info["build_id"])
        a = info.get("assets")
        if a:
            lines.append("Assets: icon %d bytes, name/version block %d bytes, built-in files (RomFS) %d bytes" % (
                a["icon"]["size"], a["nacp"]["size"], a["romfs"]["size"]))
        elif info["file_size"] < info["nro_size"]:
            lines.append("Assets: missing (the file is cut off before them)")
        else:
            lines.append("Assets: none (no icon or name block)")
        if len(info.get("titles", {})) > 1:
            lines.append("Names by language: " + "; ".join("%s: %s" % (k, v["name"]) for k, v in info["titles"].items()))
    return info, probs, "\n".join(lines)


# ----------------------------------------------------------------- making a changed copy

def _split(data):
    info, probs = inspect(data)
    errors = [x["text"] for x in probs if x["level"] == "error"]
    if errors:
        raise ValueError("This .nro is damaged, so I won't change it: " + errors[0])
    if "assets" not in info:
        raise ValueError("This .nro has no asset section (icon/name), so there's nothing to change there.")
    size = info["nro_size"]
    a = data[size:]
    parts = {}
    for name, e in info["assets"].items():
        parts[name] = a[e["offset"]:e["offset"] + e["size"]]
    return data[:size], a[:ASET_HEADER], parts


def _join(program, aset_head, parts):
    out = bytearray(aset_head[:8])   # "ASET" + version
    body, off = bytearray(), ASET_HEADER
    for name in ("icon", "nacp", "romfs"):
        b = parts.get(name) or b""
        out += struct.pack("<QQ", off if b else 0, len(b))
        body += b
        off += len(b)
    out += b"\x00" * (ASET_HEADER - len(out))
    return bytes(program) + bytes(out) + bytes(body)


def with_metadata(data, name=None, author=None, version=None):
    """A copy of the .nro with a new name, author and/or version. Everything else stays the same."""
    program, head, parts = _split(data)
    nacp = bytearray(parts["nacp"] or b"\x00" * NACP_SIZE)
    if len(nacp) < NACP_SIZE:
        nacp += b"\x00" * (NACP_SIZE - len(nacp))

    def put(off, limit, text):
        raw = text.encode("utf-8")
        if len(raw) >= limit:
            raise ValueError("'%s' is too long (at most %d bytes)." % (text, limit - 1))
        nacp[off:off + limit] = raw + b"\x00" * (limit - len(raw))

    used = [i for i in range(16) if nacp[i * 0x300:(i + 1) * 0x300].strip(b"\x00")] or [0]
    for i in used:
        if name is not None:
            put(i * 0x300, 0x200, name)
        if author is not None:
            put(i * 0x300 + 0x200, 0x100, author)
    if version is not None:
        put(0x3060, 0x10, version)
    parts["nacp"] = bytes(nacp)
    return _join(program, head, parts)


def with_icon(data, jpeg):
    if not jpeg.startswith(b"\xff\xd8\xff"):
        raise ValueError("The icon must be a JPEG picture (256 x 256 is the usual size).")
    program, head, parts = _split(data)
    parts["icon"] = jpeg
    return _join(program, head, parts)


def icon_of(data):
    info, _ = inspect(data)
    a = info.get("assets")
    if not a or not a["icon"]["size"]:
        return None
    start = info["nro_size"] + a["icon"]["offset"]
    return data[start:start + a["icon"]["size"]]
