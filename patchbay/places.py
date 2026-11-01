"""
Places near a town (restaurants, shops, fuel, pharmacies...) from OpenStreetMap, with their opening hours and
whether they're open right now. Free, no account. Only the place name and the kind of place are sent.

OpenStreetMap is filled in by volunteers: small towns can miss places or hours, so the answer says how many have
no hours listed.
"""

import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "PatchBay/1.0 (personal assistant on a Mac)"}
_CACHE = {}

KINDS = {   # plain word -> OpenStreetMap tags
    "restaurant": [("amenity", "restaurant|fast_food|cafe|food_court|pub|bar|ice_cream")],
    "food": [("amenity", "restaurant|fast_food|cafe|food_court|ice_cream")],
    "cafe": [("amenity", "cafe")], "coffee": [("amenity", "cafe")], "bar": [("amenity", "bar|pub")],
    "fuel": [("amenity", "fuel")], "gas": [("amenity", "fuel")], "pharmacy": [("amenity", "pharmacy")],
    "grocery": [("shop", "supermarket|convenience|grocery")], "shop": [("shop", ".")],
    "hotel": [("tourism", "hotel|motel|guest_house")], "bank": [("amenity", "bank|atm")],
    "hospital": [("amenity", "hospital|clinic|doctors")], "park": [("leisure", "park")],
}
DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


class PlaceError(Exception):
    pass


def _get(url, data=None):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def find_area(place):
    try:
        rows = _get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": place, "format": "jsonv2", "limit": 1}))
    except (OSError, ValueError) as e:
        raise PlaceError("Couldn't reach OpenStreetMap: %s" % e)
    if not rows:
        raise PlaceError("OpenStreetMap doesn't know a place called %s." % place)
    r = rows[0]
    return {"name": r.get("display_name", place), "lat": float(r["lat"]), "lon": float(r["lon"])}


def _days(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a in DAYS and b in DAYS:
                i, j = DAYS.index(a), DAYS.index(b)
                out.update(DAYS[k % 7] for k in range(i, i + ((j - i) % 7) + 1))
                continue
        if part in DAYS:
            out.add(part)
        else:
            return None
    return out


def is_open(hours, now):
    """True / False / None (can't tell) for an OpenStreetMap opening_hours text at a local datetime."""
    h = (hours or "").strip()
    if not h:
        return None
    if h == "24/7":
        return True
    today, yesterday = DAYS[now.weekday()], DAYS[(now.weekday() - 1) % 7]
    minutes = now.hour * 60 + now.minute
    state, understood = None, False
    for rule in [r.strip() for r in h.split(";") if r.strip()]:
        m = re.match(r"^((?:[A-Z][a-z](?:-[A-Z][a-z])?,?)+)?\s*(.*)$", rule)
        days = _days(m.group(1)) if m.group(1) else set(DAYS)
        rest = m.group(2).strip()
        if days is None:
            if rule.startswith(("PH", "SH")):
                continue
            return None
        if rest in ("off", "closed"):
            understood = True
            if today in days:
                state = False
            continue
        spans = re.findall(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", rest)
        if not spans:
            return None
        understood = True
        for a, b, c, d in spans:
            start, end = int(a) * 60 + int(b), int(c) * 60 + int(d)
            if end <= start:                     # past midnight
                if today in days and minutes >= start:
                    state = True
                elif yesterday in days and minutes < end:
                    state = True
                elif today in days and state is None:
                    state = False
            elif today in days:
                if start <= minutes < end:
                    state = True
                elif state is None:
                    state = False
        if today in days and state is None:
            state = False
    if not understood:
        return None
    return bool(state)


def nearby(place, kind="restaurant", radius_km=5, now=None):
    """A plain list of places of that kind within radius_km of the place, with open-now where the hours say."""
    kind_key = next((k for k in KINDS if k in (kind or "").lower()), "restaurant")
    area = find_area(place)
    key = (round(area["lat"], 3), round(area["lon"], 3), kind_key, radius_km)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 900:
        rows = hit[1]
    else:
        r = int(float(radius_km) * 1000)
        parts = "".join('nwr(around:%d,%f,%f)["%s"~"^(%s)$"];' % (r, area["lat"], area["lon"], k, v)
                        if v != "." else 'nwr(around:%d,%f,%f)["%s"];' % (r, area["lat"], area["lon"], k)
                        for k, v in KINDS[kind_key])
        query = "[out:json][timeout:30];(%s);out center tags 200;" % parts
        try:
            data = _get("https://overpass-api.de/api/interpreter", urllib.parse.urlencode({"data": query}).encode())
        except (OSError, ValueError) as e:
            raise PlaceError("Couldn't reach OpenStreetMap's place search: %s" % e)
        rows = []
        for el in data.get("elements") or []:
            t = el.get("tags") or {}
            if not t.get("name"):
                continue
            rows.append({"name": t["name"], "type": t.get("amenity") or t.get("shop") or t.get("tourism") or "",
                         "hours": t.get("opening_hours") or "", "street": " ".join(
                             x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x),
                         "city": t.get("addr:city") or "", "phone": t.get("phone") or t.get("contact:phone") or "",
                         "web": t.get("website") or t.get("contact:website") or ""})
        rows.sort(key=lambda x: x["name"].lower())
        _CACHE[key] = (time.time(), rows)
    now = now or dt.datetime.now()
    if not rows:
        return ("OpenStreetMap lists no %s places within %s km of %s. Its data can be incomplete for small towns."
                % (kind_key, radius_km, area["name"]))
    opened, closed, unknown = [], [], []
    for p in rows:
        o = is_open(p["hours"], now)
        line = "- %s (%s)%s%s%s" % (p["name"], p["type"], ", " + p["street"] if p["street"] else "",
                                    ", " + p["city"] if p["city"] else "",
                                    "; hours: " + p["hours"] if p["hours"] else "; no hours listed")
        if p["web"]:
            line += "; " + p["web"]
        (opened if o else closed if o is False else unknown).append(line)
    head = ("OpenStreetMap, %s places within %s km of %s, checked at %s (this Mac's time):\n"
            % (kind_key, radius_km, area["name"], now.strftime("%A %H:%M")))
    out = head + "Open now (%d):\n%s\n\nClosed now (%d):\n%s\n\nNo hours listed, so open or closed is unknown (%d):\n%s" % (
        len(opened), "\n".join(opened) or "(none)", len(closed), "\n".join(closed) or "(none)",
        len(unknown), "\n".join(unknown) or "(none)")
    return out
