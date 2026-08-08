#!/usr/bin/env python3
"""Turn the public Google Calendar feed into data/events.json for Hugo.

Hugo builds are static, so the site would otherwise only know about events
that existed the last time someone pushed. This runs in CI (including on a
nightly schedule) to refresh the list.

Deliberately stdlib-only. The calendar uses three recurring events, all
simple monthly nth-weekday rules, which is not worth adding icalendar and
dateutil to CI for. Anything more exotic in the RRULE is skipped and
reported rather than silently mis-expanded -- see expand().

Usage: build_events.py <calendar-id> [--tz America/Chicago] [--out PATH]
                       [--days 120] [--limit 6]
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ICS_URL = "https://calendar.google.com/calendar/ical/{}/public/basic.ics"
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
SUPPORTED_FREQ = {"MONTHLY", "WEEKLY", "DAILY"}


def fetch(calendar_id):
    url = ICS_URL.format(urllib.parse.quote(calendar_id))
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def unfold(text):
    """ICS folds long lines with CRLF + a leading space or tab."""
    return re.sub(r"\r?\n[ \t]", "", text)


def split_blocks(text, kind):
    return re.findall(r"BEGIN:%s\r?\n(.*?)END:%s" % (kind, kind), text, re.S)


def parse_props(block):
    props = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, rawparams = head.partition(";")
        params = {}
        for p in rawparams.split(";"):
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v
        props.setdefault(name.upper(), []).append((params, value.strip()))
    return props


def first(props, name):
    got = props.get(name)
    return got[0] if got else None


def unescape(s):
    return (
        s.replace("\\n", "\n").replace("\\N", "\n")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def parse_dt(entry, tz):
    """Return (datetime|date, is_all_day). Naive wall times are localised."""
    params, value = entry
    if params.get("VALUE") == "DATE" or (len(value) == 8 and "T" not in value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz), False
    zone = ZoneInfo(params["TZID"]) if "TZID" in params else tz
    return naive.replace(tzinfo=zone).astimezone(tz), False


LUMA_RE = re.compile(r"https?://(?:lu\.ma|(?:www\.)?luma\.com)/[^\s\\<>\"]+")


def event_url(description):
    """The Luma page for an event, if its description links one.

    Only Luma links are picked up. Descriptions also contain Zoom and Google
    Meet joining links, and republishing those on a public page invites
    uninvited guests. Luma pages carry their own access token in the query
    string, which is what makes a private event viewable by someone holding
    the link -- that is the intended way in.
    """
    m = LUMA_RE.search(description or "")
    return m.group(0).rstrip(".,;") if m else ""


def parse_rrule(value):
    out = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def nth_weekday(year, month, weekday, n):
    """nth (1-based) weekday of a month; n<0 counts back from the end."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        d += timedelta(weeks=n - 1)
        return d if d.month == month else None
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - timedelta(days=1)
    d -= timedelta(days=(d.weekday() - weekday) % 7)
    d += timedelta(weeks=n + 1)
    return d if d.month == month else None


def expand(start, rrule, horizon, tz, warn):
    """Yield occurrence start datetimes from `start` up to `horizon`."""
    freq = rrule.get("FREQ", "")
    if freq not in SUPPORTED_FREQ:
        warn("unsupported FREQ=%s; showing only its first occurrence" % freq)
        return [start]

    interval = int(rrule.get("INTERVAL", 1))
    count = int(rrule["COUNT"]) if "COUNT" in rrule else None
    until = None
    if "UNTIL" in rrule:
        u = rrule["UNTIL"]
        if u.endswith("Z"):
            until = datetime.strptime(u[:15], "%Y%m%dT%H%M%S").replace(
                tzinfo=ZoneInfo("UTC")).astimezone(tz)
        else:
            until = datetime.strptime(u[:8], "%Y%m%d").replace(tzinfo=tz)

    byday = [d for d in rrule.get("BYDAY", "").split(",") if d]
    out, emitted = [], 0
    is_dt = isinstance(start, datetime)

    def keep(when):
        nonlocal emitted
        if until and when > until:
            return False
        if count is not None and emitted >= count:
            return False
        out.append(when)
        emitted += 1
        return True

    if freq == "MONTHLY" and byday:
        m = re.fullmatch(r"(-?\d+)([A-Z]{2})", byday[0])
        if not m:
            warn("unsupported BYDAY=%s" % byday[0])
            return [start]
        n, day = int(m.group(1)), WEEKDAYS[m.group(2)]
        y, mo = start.year, start.month
        while True:
            d = nth_weekday(y, mo, day, n)
            if d:
                when = (datetime.combine(d, start.timetz()) if is_dt else d)
                if when > horizon:
                    break
                if when >= start and not keep(when):
                    break
            for _ in range(interval):
                mo += 1
                if mo > 12:
                    mo, y = 1, y + 1
            if date(y, mo, 1) > (horizon.date() if is_dt else horizon):
                break
        return out

    step = timedelta(weeks=interval) if freq == "WEEKLY" else timedelta(days=interval)
    when = start
    while when <= horizon:
        if not keep(when):
            break
        when = when + step
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("calendar_id")
    ap.add_argument("--tz", default="America/Chicago")
    ap.add_argument("--out", default="data/events.json")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()

    tz = ZoneInfo(args.tz)
    now = datetime.now(tz)
    horizon = now + timedelta(days=args.days)
    warnings = []

    text = unfold(fetch(args.calendar_id))
    # VTIMEZONE blocks also contain RRULEs (the DST rules) -- drop them first
    # so they are never mistaken for events.
    text = re.sub(r"BEGIN:VTIMEZONE.*?END:VTIMEZONE", "", text, flags=re.S)

    occurrences, overrides = [], {}
    for block in split_blocks(text, "VEVENT"):
        props = parse_props(block)
        dtstart = first(props, "DTSTART")
        if not dtstart:
            continue
        status = (first(props, "STATUS") or ({}, ""))[1]
        summary = unescape((first(props, "SUMMARY") or ({}, ""))[1]).strip()
        location = unescape((first(props, "LOCATION") or ({}, ""))[1]).strip()
        url = event_url(unescape((first(props, "DESCRIPTION") or ({}, ""))[1]))
        uid = (first(props, "UID") or ({}, ""))[1]
        start, all_day = parse_dt(dtstart, tz)

        # A VEVENT carrying RECURRENCE-ID replaces one instance of its series.
        recur_id = first(props, "RECURRENCE-ID")
        if recur_id:
            key = (uid, parse_dt(recur_id, tz)[0])
            overrides[key] = None if status == "CANCELLED" else {
                "summary": summary, "location": location, "start": start,
                "url": url}
            continue
        if status == "CANCELLED":
            continue

        excluded = set()
        for params, value in props.get("EXDATE", []):
            for one in value.split(","):
                excluded.add(parse_dt((params, one), tz)[0])

        rrule = first(props, "RRULE")
        starts = ([start] if not rrule else
                  expand(start, parse_rrule(rrule[1]), horizon, tz,
                         lambda m, s=summary: warnings.append("%s: %s" % (s, m))))

        for when in starts:
            if when in excluded:
                continue
            key = (uid, when)
            if key in overrides:
                ov = overrides[key]
                if ov is None:
                    continue
                occurrences.append({"summary": ov["summary"], "location": ov["location"],
                                    "start": ov["start"], "all_day": all_day,
                                    "url": ov.get("url", "")})
                continue
            occurrences.append({"summary": summary, "location": location,
                                "start": when, "all_day": all_day, "url": url})

    def as_dt(o):
        s = o["start"]
        return datetime.combine(s, datetime.min.time()).replace(tzinfo=tz) \
            if not isinstance(s, datetime) else s

    upcoming = sorted((o for o in occurrences if as_dt(o) >= now.replace(hour=0, minute=0)),
                      key=as_dt)[:args.limit]

    payload = [{
        "title": o["summary"],
        "location": o["location"],
        "allDay": o["all_day"],
        "url": o.get("url", ""),
        "start": as_dt(o).isoformat(),
    } for o in upcoming]

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    print("wrote %d events to %s" % (len(payload), args.out))
    for w in dict.fromkeys(warnings):
        print("  warning: %s" % w, file=sys.stderr)
    for e in payload:
        print("  %s  %-46s %s" % (e["start"][:16], e["title"][:46],
                                   "-> " + e["url"] if e["url"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
