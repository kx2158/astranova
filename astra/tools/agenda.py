# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Calendar: import calendars (.ics files, or the private iCal link from Google Calendar / Outlook / Apple), so
Astra and Nova know what your day looks like. Also a small local calendar for things you tell them.

Google Calendar: Settings > (your calendar) > Integrate calendar > "Secret address in iCal format".
Outlook: Settings > Calendar > Shared calendars > Publish a calendar > ICS link.
"""
import datetime as dt
import json
import re
import time
import uuid
from pathlib import Path

import requests

from .. import services
from . import tool

WINDOW_PAST, WINDOW_FUTURE = 2, 120      # days of events kept around

# Outlook writes Windows zone names; map the common ones
WIN_TZ = {"W. Europe Standard Time": "Europe/Berlin", "Central Europe Standard Time": "Europe/Budapest",
          "Central European Standard Time": "Europe/Warsaw", "Romance Standard Time": "Europe/Paris",
          "GMT Standard Time": "Europe/London", "UTC": "UTC", "Eastern Standard Time": "America/New_York",
          "Pacific Standard Time": "America/Los_Angeles", "Central Standard Time": "America/Chicago",
          "E. Europe Standard Time": "Europe/Bucharest", "FLE Standard Time": "Europe/Kiev"}
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


# ---- tiny ICS parser (no extra packages needed) ------------------------------------------------------
def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _unescape(v):
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _tz(name):
    if not name:
        return None
    name = WIN_TZ.get(name.strip('"'), name.strip('"'))
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone (or no tzdata): treat as local time
        return None


def _parse_dt(value, params):
    """-> (datetime in local naive time, all_day)"""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        d = dt.datetime.strptime(v[:8], "%Y%m%d")
        return d, True
    utc = v.endswith("Z")
    d = dt.datetime.strptime(v.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        d = d.replace(tzinfo=dt.timezone.utc)
    else:
        z = _tz(params.get("TZID"))
        if z:
            d = d.replace(tzinfo=z)
    if d.tzinfo:
        d = d.astimezone().replace(tzinfo=None)   # to this PC's local time
    return d, False


def _line(line):
    name, _, value = line.partition(":")
    parts = name.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v
    return parts[0].upper(), params, value


def parse_ics(text):
    events, cur = [], None
    for raw in _unfold(text):
        if raw == "BEGIN:VEVENT":
            cur = {"exdates": set()}
            continue
        if raw == "END:VEVENT":
            if cur and cur.get("start"):
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in raw:
            continue
        key, params, value = _line(raw)
        try:
            if key == "DTSTART":
                cur["start"], cur["all_day"] = _parse_dt(value, params)
            elif key == "DTEND":
                cur["end"], _ = _parse_dt(value, params)
            elif key == "DURATION":
                cur["duration"] = _duration(value)
            elif key == "SUMMARY":
                cur["title"] = _unescape(value)
            elif key == "LOCATION":
                cur["location"] = _unescape(value)
            elif key == "DESCRIPTION":
                cur["notes"] = _unescape(value)[:500]
            elif key == "UID":
                cur["uid"] = value
            elif key == "RRULE":
                cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
            elif key == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].add(_parse_dt(v, params)[0])
            elif key == "RECURRENCE-ID":
                cur["recurrence_id"], _ = _parse_dt(value, params)
            elif key == "STATUS":
                cur["status"] = value.upper()
        except Exception:  # noqa: BLE001 - skip broken lines, keep the event
            continue
    return events


def _duration(v):
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return dt.timedelta(hours=1)
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def expand(ev, start, end):
    """Occurrences of one event between start and end (local naive datetimes)."""
    s0 = ev["start"]
    length = (ev["end"] - s0) if ev.get("end") else ev.get("duration") or (dt.timedelta(days=1) if ev.get("all_day") else dt.timedelta(hours=1))
    rr = ev.get("rrule")
    if not rr:
        return [(s0, s0 + length)] if s0 < end and s0 + length > start else []
    freq = rr.get("FREQ", "DAILY")
    interval = max(1, int(rr.get("INTERVAL", 1) or 1))
    count = int(rr["COUNT"]) if rr.get("COUNT") else None
    until = None
    if rr.get("UNTIL"):
        try:
            until = _parse_dt(rr["UNTIL"], {})[0]
        except Exception:  # noqa: BLE001
            until = None
    byday = [DAYS[d[-2:]] for d in rr.get("BYDAY", "").split(",") if d[-2:] in DAYS]
    out, n, cur = [], 0, s0
    limit_end = min(end, until + dt.timedelta(days=1)) if until else end
    guard = 0
    while cur < limit_end and guard < 5000:
        guard += 1
        if freq == "WEEKLY" and byday:
            week_start = cur - dt.timedelta(days=cur.weekday())
            for wd in sorted(byday):
                occ = week_start + dt.timedelta(days=wd)
                occ = occ.replace(hour=s0.hour, minute=s0.minute, second=s0.second)
                if occ < s0 or (until and occ > until):
                    continue
                n += 1
                if count and n > count:
                    return out
                if occ not in ev["exdates"] and occ < end and occ + length > start:
                    out.append((occ, occ + length))
            cur = week_start + dt.timedelta(weeks=interval)
            cur = cur.replace(hour=s0.hour, minute=s0.minute, second=s0.second)
            continue
        n += 1
        if count and n > count:
            break
        if (not until or cur <= until) and cur not in ev["exdates"] and cur + length > start:
            out.append((cur, cur + length))
        if freq == "DAILY":
            cur += dt.timedelta(days=interval)
        elif freq == "WEEKLY":
            cur += dt.timedelta(weeks=interval)
        elif freq == "MONTHLY":
            y, m = divmod(cur.month - 1 + interval, 12)
            try:
                cur = cur.replace(year=cur.year + y, month=m + 1)
            except ValueError:
                cur = cur.replace(year=cur.year + y, month=m + 1, day=28)
        elif freq == "YEARLY":
            try:
                cur = cur.replace(year=cur.year + interval)
            except ValueError:
                cur = cur.replace(year=cur.year + interval, day=28)
        else:
            break
    return out


# ---- storage -----------------------------------------------------------------------------------------
def _read_source(src):
    loc = (src.get("url") or src.get("path") or "").strip()
    if loc.startswith("webcal://"):
        loc = "https://" + loc[len("webcal://"):]
    if loc.startswith("http"):
        r = requests.get(loc, timeout=30, headers={"User-Agent": "AstraNova calendar"})
        r.raise_for_status()
        return r.content.decode("utf-8", "replace")
    return Path(loc).read_text(encoding="utf-8", errors="replace")


def refresh(source_id=None):
    """Re-read every imported calendar. Returns {source_id: count | 'error ...'}."""
    now = dt.datetime.now()
    start, end = now - dt.timedelta(days=WINDOW_PAST), now + dt.timedelta(days=WINDOW_FUTURE)
    result = {}
    for src in services.config.get("calendar", "sources") or []:
        if source_id and src.get("id") != source_id:
            continue
        try:
            evs = parse_ics(_read_source(src))
        except Exception as e:  # noqa: BLE001
            result[src["id"]] = f"error: {str(e)[:120]}"
            continue
        overrides = {(e.get("uid"), e["recurrence_id"]) for e in evs if e.get("recurrence_id")}
        rows = []
        for e in evs:
            if e.get("status") == "CANCELLED":
                continue
            for s, f in expand(e, start, end):
                if not e.get("recurrence_id") and (e.get("uid"), s) in overrides:
                    continue  # this occurrence was moved/edited - the override event has it
                rows.append((e.get("uid") or uuid.uuid4().hex, src["id"], e.get("title") or "(busy)", s.timestamp(),
                             f.timestamp(), int(bool(e.get("all_day"))), e.get("location") or "", e.get("notes") or ""))
        with services.db.lock:
            reminded = {(r["uid"], r["start"]) for r in services.db.q(
                "SELECT uid, start FROM events WHERE source=? AND reminded=1", (src["id"],))}
            services.db.x("DELETE FROM events WHERE source=?", (src["id"],))
            for r in rows:
                services.db.x("INSERT OR REPLACE INTO events (uid, source, title, start, end, all_day, location, notes, "
                              "reminded) VALUES (?,?,?,?,?,?,?,?,?)", r + (int((r[0], r[3]) in reminded),))
        result[src["id"]] = len(rows)
    try:
        sync_series()       # keeps your repeating events filled in as the months go by
    except Exception:  # noqa: BLE001
        pass
    services.db.kv_set("calendar_refreshed", time.time())
    services.emit({"type": "calendar_changed"})
    return result


# things you have to do (rather than just attend) stay open until you say they're done
TASKLIKE = re.compile(r"\b(deadline|due|submit|submission|hand ?in|abgabe|todo|to-do|finish|send|pay|apply|application|"
                      r"register|sign up|book|renew|return|odovzda|termín)\b", re.I)


def is_tasklike(e):
    return bool(TASKLIKE.search(e["title"] or ""))


def events_between(start, end):
    rows = services.db.q("SELECT e.*, d.done_at AS done_at FROM events e LEFT JOIN event_done d ON d.uid=e.uid AND "
                         "d.start=e.start WHERE e.start < ? AND e.end > ? ORDER BY e.start",
                         (end.timestamp(), start.timestamp()))
    now = time.time()
    for r in rows:
        # marked done by you, or (for appointments) automatically once they're over
        r["done"] = bool(r.get("done_at")) or (not is_tasklike(r) and r["end"] <= now)
        r["task"] = is_tasklike(r)
    return rows


def set_done(uid, start, done=True):
    if done:
        services.db.x("INSERT OR REPLACE INTO event_done (uid, start, done_at) VALUES (?,?,?)", (uid, start, time.time()))
    else:
        services.db.x("DELETE FROM event_done WHERE uid=? AND start=?", (uid, start))
    services.emit({"type": "calendar_changed"})


def find_event(title, day=""):
    """Best match for a title the user mentions, around today (or around the given day)."""
    base = dt.datetime.now()
    if day:
        try:
            from .system import parse_when
            base = parse_when(day)
        except ValueError:
            pass
    rows = events_between(base - dt.timedelta(days=14), base + dt.timedelta(days=30))
    words = set(re.findall(r"\w{3,}", (title or "").lower()))
    best, score = None, 0.0
    for e in rows:
        ew = set(re.findall(r"\w{3,}", e["title"].lower()))
        if not ew:
            continue
        sc = len(words & ew) / max(1, len(ew)) - abs(e["start"] - base.timestamp()) / (86400 * 400)
        if sc > score:
            best, score = e, sc
    return best if score > 0.3 else None


def _fmt(e, with_day=False):
    s, f = dt.datetime.fromtimestamp(e["start"]), dt.datetime.fromtimestamp(e["end"])
    when = "all day" if e["all_day"] else f"{s:%H:%M}-{f:%H:%M}"
    if with_day:
        when = f"{s:%a %d %b} " + when
    mark = " [done]" if e.get("done_at") else (" [open to-do]" if e.get("task") and e["end"] <= time.time() else "")
    return f"{when} {e['title']}" + (f" ({e['location']})" if e.get("location") else "") + mark


def day_summary(day=None):
    day = day or dt.date.today()
    a = dt.datetime.combine(day, dt.time())
    evs = events_between(a, a + dt.timedelta(days=1))
    return [_fmt(e) for e in evs]


def prompt_block():
    """Compact schedule for the system prompts."""
    if not services.db.q("SELECT 1 FROM events LIMIT 1"):
        return ""
    now = dt.datetime.now()
    today = [_fmt(e) + (" (now)" if e["start"] <= now.timestamp() < e["end"] else "")
             for e in events_between(now.replace(hour=0, minute=0), now.replace(hour=23, minute=59))]
    overdue = [e for e in events_between(now - dt.timedelta(days=7), now) if e["task"] and not e.get("done_at")
               and e["end"] <= now.timestamp()]
    tmr = day_summary(dt.date.today() + dt.timedelta(days=1))
    lines = ["CALENDAR:", "- Today: " + ("; ".join(today) if today else "nothing scheduled")]
    lines.append("- Tomorrow: " + ("; ".join(tmr) if tmr else "nothing scheduled"))
    if overdue:
        lines.append("- Not marked done yet: " + "; ".join(_fmt(e, True) for e in overdue[:5]) +
                     " (ask casually if they did it; calendar_done when they say yes)")
    return "\n".join(lines)


def add_source(name, location):
    src = {"id": uuid.uuid4().hex[:8], "name": name or "Calendar"}
    if re.match(r"^(https?|webcal)://", location.strip(), re.I):
        src["url"] = location.strip()
    else:
        src["path"] = location.strip()
    srcs = services.config.get("calendar", "sources") or []
    srcs.append(src)
    services.config.update("calendar", {"sources": srcs})
    return src


def remove_source(sid):
    srcs = [s for s in services.config.get("calendar", "sources") or [] if s.get("id") != sid]
    services.config.update("calendar", {"sources": srcs})
    services.db.x("DELETE FROM events WHERE source=?", (sid,))


# ---- repeating events of your own ------------------------------------------------------------------------
# "yoga every tuesday 18:00", "lectures mondays and thursdays 10:15", "rent on the 1st every month": stored once as a
# series and written into the calendar for the months around today, always on the days that were said.
_DAYNAMES = {"mon": 0, "monday": 0, "mondays": 0, "tue": 1, "tues": 1, "tuesday": 1, "tuesdays": 1, "wed": 2,
             "wednesday": 2, "wednesdays": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "thursdays": 3,
             "fri": 4, "friday": 4, "fridays": 4, "sat": 5, "saturday": 5, "saturdays": 5, "sun": 6, "sunday": 6,
             "sundays": 6,
             # German / Slovak, the way people actually type them
             "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
             "pondelok": 0, "utorok": 1, "streda": 2, "stvrtok": 3, "štvrtok": 3, "piatok": 4, "sobota": 5,
             "nedela": 6, "nedeľa": 6}
_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
REPEATS = ("daily", "weekdays", "weekly", "biweekly", "monthly", "yearly")


def _days_in(text):
    found = []
    for w in re.findall(r"[a-zA-ZšťčžýáíéôäľňŠ]+", (text or "").lower()):
        d = _DAYNAMES.get(w)
        if d is not None and d not in found:
            found.append(d)
    return sorted(found)


def infer_repeat(when, repeat="", days=""):
    """Work out the repeat even when it's only in the wording ('every tuesday', 'mondays and thursdays',
    'every day', 'weekly', 'each month'). Returns (repeat or '', [weekday numbers], when without the repeat words)."""
    w = (when or "").strip()
    low = w.lower()
    rep = (repeat or "").strip().lower().replace("every week", "weekly").replace("every day", "daily")
    rep = {"week": "weekly", "day": "daily", "month": "monthly", "year": "yearly", "fortnightly": "biweekly",
           "every 2 weeks": "biweekly", "every other week": "biweekly", "once": "", "none": "", "no": ""}.get(rep, rep)
    dlist = _days_in(days) or []
    if not rep:
        if re.search(r"\b(every|each|jeden|jede[nm]?|každ\w*)\s+(other|2nd|second)\s+week|\bbiweekly|fortnight", low):
            rep = "biweekly"
        elif re.search(r"\b(every ?day|daily|each day|täglich|denne|každý deň)\b", low):
            rep = "daily"
        elif re.search(r"\b(weekdays|every weekday|mon(day)?\s*(-|to|through)\s*fri(day)?|werktags)\b", low):
            rep = "weekdays"
        elif re.search(r"\b(every|each) month\b|\bmonthly\b|\bmonatlich\b", low):
            rep = "monthly"
        elif re.search(r"\b(every|each) year\b|\byearly\b|\bannually\b", low):
            rep = "yearly"
        elif re.search(r"\b(every|each|weekly|jeden|jede[nm]?|každ\w*)\b", low) or re.search(
                r"\b(mon|tues|wednes|thurs|fri|satur|sun)days\b", low):
            rep = "weekly"
    if rep in ("weekly", "biweekly") and not dlist:
        dlist = _days_in(w)
    clean = re.sub(r"\b(every|each|other|weekly|biweekly|daily|monthly|yearly|annually|on|weekdays|weekday|"
                   r"fortnightly|jeden|jede[nm]?|každ\w*|day|days|week|weeks|month|months|year|years|the|"
                   r"täglich|denne|deň|starting|from|beginning)\b", " ", low)
    for name in sorted(_DAYNAMES, key=len, reverse=True):
        clean = re.sub(rf"\b{name}\b", " ", clean)
    clean = re.sub(r"\b(and|und|a|,|&)\b", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return (rep if rep in REPEATS else ""), dlist, clean


def _first_occurrence(start, rep, days):
    """The first time it happens from now on, on one of the days that were said."""
    now = dt.datetime.now()
    if rep in ("weekly", "biweekly") and days:
        base = start if start > now - dt.timedelta(days=1) else now
        for k in range(0, 8):
            c = (base + dt.timedelta(days=k)).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            if c.weekday() in days and c >= now - dt.timedelta(minutes=1):
                return c
    if rep == "weekdays":
        c = start
        while c.weekday() >= 5 or c < now - dt.timedelta(minutes=1):
            c += dt.timedelta(days=1)
        return c
    if rep == "daily" and start < now - dt.timedelta(minutes=1):
        c = start.replace(year=now.year, month=now.month, day=now.day)
        return c if c >= now - dt.timedelta(minutes=1) else c + dt.timedelta(days=1)
    return start


def _series_rrule(row):
    rep, byday = row["freq"], [c for c in (row.get("byday") or "").split(",") if c]
    if rep == "weekdays":
        return {"FREQ": "WEEKLY", "BYDAY": "MO,TU,WE,TH,FR"}
    if rep in ("weekly", "biweekly"):
        rr = {"FREQ": "WEEKLY", "INTERVAL": "2" if rep == "biweekly" else "1"}
        if byday:
            rr["BYDAY"] = ",".join(byday)
        return rr
    return {"FREQ": {"daily": "DAILY", "monthly": "MONTHLY", "yearly": "YEARLY"}.get(rep, "DAILY")}


def sync_series(sid=None):
    """Write the occurrences of your repeating events into the calendar (about 2 months back, 13 ahead)."""
    now = dt.datetime.now()
    a, b = now - dt.timedelta(days=60), now + dt.timedelta(days=400)
    rows = services.db.q("SELECT * FROM event_series" + (" WHERE id=?" if sid else ""), (sid,) if sid else ())
    with services.db.lock:
        if sid:
            services.db.x("DELETE FROM events WHERE source='series' AND uid=?", (sid,))
        else:
            services.db.x("DELETE FROM events WHERE source='series'")
        for r in rows:
            s0 = dt.datetime.fromtimestamp(r["start"])
            length = dt.timedelta(days=1) if r["all_day"] else dt.timedelta(minutes=int(r["minutes"] or 60))
            rr = _series_rrule(r)
            if r.get("until"):
                rr["UNTIL"] = dt.datetime.fromtimestamp(r["until"]).strftime("%Y%m%dT235959")
            try:
                ex = {dt.datetime.fromtimestamp(x) for x in json.loads(r.get("exdates") or "[]")}
            except ValueError:
                ex = set()
            ev = {"start": s0, "end": s0 + length, "rrule": rr, "exdates": ex, "all_day": r["all_day"]}
            for s, f in expand(ev, a, b):
                services.db.x("INSERT OR REPLACE INTO events (uid, source, title, start, end, all_day, location) "
                              "VALUES (?,?,?,?,?,?,?)", (r["id"], "series", r["title"], s.timestamp(), f.timestamp(),
                                                         int(r["all_day"]), r["location"] or ""))
    services.emit({"type": "calendar_changed"})


def _repeat_words(rep, days):
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if rep in ("weekly", "biweekly") and days:
        ds = [names[d] for d in days]
        joined = ds[0] if len(ds) == 1 else ", ".join(ds[:-1]) + " and " + ds[-1]
        return ("every other " if rep == "biweekly" else "every ") + joined
    return {"daily": "every day", "weekdays": "every weekday", "weekly": "every week", "biweekly": "every other week",
            "monthly": "every month", "yearly": "every year"}.get(rep, rep)


def add_event(title, when, minutes=60, location="", repeat="", days="", until=""):
    """Shared by Astra, Nova and the calendar page. Understands one-off and repeating events."""
    from .system import parse_when
    rep, dlist, rest = infer_repeat(when, repeat, days)
    w = (rest if rep else when).strip()
    dom = None
    if rep in ("monthly", "yearly"):   # "on the 1st", "15th"
        m = re.search(r"\b(\d{1,2})\s*(st|nd|rd|th|\.)(?!\d)", w)
        if m and 1 <= int(m.group(1)) <= 31 and not re.search(r"\d{4}-\d{2}-\d{2}", w):
            dom = int(m.group(1))
            w = (w[:m.start()] + w[m.end():]).strip()
    if rep and not re.search(r"\d", w):        # "every tuesday" with no time: all day on those days
        w = ""
    all_day = not w or bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", w))
    now = dt.datetime.now()
    if rep:
        try:
            if not w:
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif all_day:
                start = dt.datetime.fromisoformat(w)
            else:
                start = parse_when(w)
                if dlist:   # parse_when may have pushed a time to tomorrow: the weekday decides, not that
                    start = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0) \
                        if not re.search(r"\d{4}-\d{2}-\d{2}|tomorrow|next", w) else start
        except ValueError:
            return None, ("Couldn't read that time. Use 'HH:MM' with repeat and days, e.g. when='18:00', "
                          "repeat='weekly', days='tue'.")
        if rep in ("weekly", "biweekly") and not dlist:
            dlist = [start.weekday()]
        if dom:
            import calendar as _cal
            y, mo = now.year, now.month
            for _ in range(13):
                c = start.replace(year=y, month=mo, day=min(dom, _cal.monthrange(y, mo)[1]))
                if c >= now - dt.timedelta(minutes=1):
                    start = c
                    break
                y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        start = _first_occurrence(start, rep, dlist)
        until_ts = 0
        if until:
            try:
                until_ts = parse_when(until).replace(hour=23, minute=59).timestamp()
            except ValueError:
                until_ts = 0
        sid = "s" + uuid.uuid4().hex[:11]
        services.db.x("INSERT INTO event_series (id, title, start, minutes, all_day, location, freq, interval, byday, "
                      "until, exdates, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, title[:200], start.timestamp(), int(minutes or 60), int(all_day), location or "", rep,
                       2 if rep == "biweekly" else 1, ",".join(_CODES[d] for d in dlist), until_ts, "[]", time.time()))
        sync_series(sid)
        when_txt = "all day" if all_day else f"{start:%H:%M}"
        return sid, (f"Added: {title}, {_repeat_words(rep, dlist)}, {when_txt}, starting {start:%a %d %b}"
                     + (f", until {dt.datetime.fromtimestamp(until_ts):%d %b %Y}" if until_ts else "") + ".")
    try:
        start = dt.datetime.fromisoformat(w) if all_day else parse_when(w)
    except ValueError:
        return None, ("Couldn't read that time. Ask the user for the day and time in plain words (no time zone needed, "
                      "it's always their local time), then call calendar_add with 'YYYY-MM-DD HH:MM'.")
    end = start + (dt.timedelta(days=1) if all_day else dt.timedelta(minutes=int(minutes or 60)))
    uid = uuid.uuid4().hex
    services.db.x("INSERT OR REPLACE INTO events (uid, source, title, start, end, all_day, location) VALUES (?,?,?,?,?,?,?)",
                  (uid, "local", title[:200], start.timestamp(), end.timestamp(), int(all_day), location or ""))
    services.emit({"type": "calendar_changed"})
    return uid, "Added: " + _fmt({"start": start.timestamp(), "end": end.timestamp(), "all_day": all_day,
                                  "title": title, "location": location}, True)


def remove_event(uid, start=None, scope="one"):
    """Remove something you added. For a repeating event: just that day (scope='one') or the whole series."""
    if services.db.q("SELECT 1 FROM event_series WHERE id=?", (uid,)):
        if scope == "all" or start is None:
            services.db.x("DELETE FROM event_series WHERE id=?", (uid,))
            services.db.x("DELETE FROM events WHERE source='series' AND uid=?", (uid,))
        else:
            r = services.db.q("SELECT exdates FROM event_series WHERE id=?", (uid,))[0]
            ex = json.loads(r["exdates"] or "[]") + [float(start)]
            services.db.x("UPDATE event_series SET exdates=? WHERE id=?", (json.dumps(ex), uid))
            sync_series(uid)
    else:
        services.db.x("DELETE FROM events WHERE uid=? AND source='local'", (uid,))
    services.emit({"type": "calendar_changed"})
    return True


# ---- tools ------------------------------------------------------------------------------------------
@tool("calendar_agenda", "What's on the user's calendar. start: 'today', 'tomorrow' or a date YYYY-MM-DD; days: how "
      "many days to show (default 1).", {"start": {"type": "string"}, "days": {"type": "integer"}},
      label="Checking your calendar", modes=("astra", "friend"))
def calendar_agenda(ctx, start="today", days=1):
    s = (start or "today").strip().lower()
    base = dt.date.today()
    if s == "tomorrow":
        base += dt.timedelta(days=1)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        base = dt.date.fromisoformat(s)
    a = dt.datetime.combine(base, dt.time())
    evs = events_between(a, a + dt.timedelta(days=max(1, min(int(days or 1), 31))))
    if not services.config.get("calendar", "sources") and not evs:
        return "No calendar connected yet (Settings > Calendar). Nothing known."
    return "\n".join(_fmt(e, with_day=True) for e in evs) or "Nothing scheduled then."


@tool("calendar_add", "Add an event to AstraNova's own calendar (things the user tells you: appointments, classes, "
      "deadlines). when: 'YYYY-MM-DD HH:MM', 'tomorrow 14:00', 'friday 18:00' or 'YYYY-MM-DD' for all day. "
      "REPEATING things ('every Tuesday', 'Mondays and Thursdays', 'every day', 'every other week', 'monthly'): set "
      "repeat (daily | weekdays | weekly | biweekly | monthly | yearly) and for weekly ones days (e.g. 'tue' or "
      "'mon,thu'), with when = just the time ('18:00'). It's put on the next matching day automatically: never ask "
      "which date it starts. until: optional last day.",
      {"title": {"type": "string"}, "when": {"type": "string"}, "minutes": {"type": "integer"},
       "location": {"type": "string"}, "repeat": {"type": "string"}, "days": {"type": "string"},
       "until": {"type": "string"}}, ["title", "when"], label="Adding to calendar", modes=("astra", "friend"))
def calendar_add(ctx, title, when, minutes=60, location="", repeat="", days="", until=""):
    # don't add the same repeating thing twice
    rep, dlist, _ = infer_repeat(when, repeat, days)
    if rep:
        words = set(re.findall(r"\w{3,}", (title or "").lower()))
        codes = ",".join(_CODES[d] for d in dlist)
        for r in services.db.q("SELECT * FROM event_series"):
            other = set(re.findall(r"\w{3,}", r["title"].lower()))
            same_days = not codes or not r["byday"] or r["byday"] == codes
            if words and other and len(words & other) / len(words | other) >= 0.6 and r["freq"] == rep and same_days:
                return f"Already in the calendar as a repeating event: {r['title']} ({_repeat_words(r['freq'], [_CODES.index(c) for c in (r['byday'] or '').split(',') if c in _CODES])})."
    _, msg = add_event(title, when, minutes, location, repeat, days, until)
    return msg


@tool("calendar_remove", "Remove something the user added to AstraNova's calendar. title: words from its name; day: "
      "optional date to pick the right one; all_repeats=true removes every occurrence of a repeating event "
      "(e.g. 'I quit yoga'), false only that day ('no yoga this tuesday').",
      {"title": {"type": "string"}, "day": {"type": "string"}, "all_repeats": {"type": "boolean"}}, ["title"],
      label="Updating your calendar", modes=("astra", "friend"))
def calendar_remove(ctx, title, day="", all_repeats=False):
    e = find_event(title, day)
    if not e or e["source"] not in ("local", "series"):
        return f"No event of your own matching '{title}' found (imported calendars can't be changed from here)."
    remove_event(e["uid"], e["start"], "all" if all_repeats else "one")
    if e["source"] == "series":
        return ("Removed every occurrence of " if all_repeats else "Skipped this one: ") + _fmt(e, True)
    return "Removed: " + _fmt(e, True)


# ---- events from conversation (Nova) -------------------------------------------------------------------
EVENT_HINT = re.compile(
    r"\b(every|each|daily|weekly|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|\d{1,2}[:.]\d{2}|\d{1,2}\s?(am|pm|uhr|h)\b|noon|tomorrow|tmrw|tmr|tonight|this (morning|afternoon|evening|"
    r"weekend)|next (week|mon|tue|wed|thu|fri|sat|sun)|(mon|tues?|wed(nes)?|thu(rs)?|fri|sat(ur)?|sun)(day)?|"
    r"\d{1,2}(st|nd|rd|th)|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}|\d{1,2}\.\d{1,2}\.)\b",
    re.I)
EVENT_SCHEMA = {"type": "object", "properties": {
    "is_event": {"type": "boolean"}, "title": {"type": "string"}, "when": {"type": "string"},
    "minutes": {"type": "integer"}, "location": {"type": "string"}, "repeat": {"type": "string"},
    "days": {"type": "string"}}, "required": ["is_event"]}


def auto_event(user_text):
    """If the user mentions their own upcoming plan with a concrete day/time, put it in the calendar.
    Returns a short note for the chat, or None."""
    if not EVENT_HINT.search(user_text or "") or len(user_text) > 1500:
        return None
    now = dt.datetime.now()
    r = services.llm.json_task(
        f"Today is {now:%A %Y-%m-%d}, time {now:%H:%M}. Read the user's message. is_event=true only if it states a "
        "concrete upcoming plan, appointment, meeting, class, exam, deadline or meetup THE USER has, with a specific "
        "day and/or time. Not for past events, maybes, questions, other people's plans, or vague times. title: short "
        "like a calendar entry (e.g. 'Dentist', 'Coffee with Sam', 'Art history exam'). when: 'YYYY-MM-DD HH:MM', or "
        "'YYYY-MM-DD' if no time. minutes: duration if said, else 60. location if said. If it REPEATS ('every "
        "tuesday', 'mondays and thursdays', 'every day'), set repeat (daily | weekdays | weekly | biweekly | monthly) "
        "and days (e.g. 'tue' or 'mon,thu'), and put only the time in when ('HH:MM', empty if none).",
        user_text, EVENT_SCHEMA)
    if not r.get("is_event") or not (r.get("title") or "").strip():
        return None
    rep, dlist, _ = infer_repeat(user_text if not r.get("repeat") else (r.get("when") or ""), r.get("repeat") or "",
                                 r.get("days") or "")
    if rep:
        out = calendar_add(None, r["title"].strip()[:120], (r.get("when") or "").strip() or "", r.get("minutes") or 60,
                           (r.get("location") or "")[:120], rep, ",".join(_CODES[d] for d in dlist), "")
        return out.replace("Added:", "Added to your calendar:") if out.startswith("Added") else None
    if not (r.get("when") or "").strip():
        return None
    from .system import parse_when
    w = r["when"].strip()
    all_day = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", w))
    try:
        start = dt.datetime.fromisoformat(w) if all_day else parse_when(w)
    except ValueError:
        return None
    if start < now - dt.timedelta(hours=1) or start > now + dt.timedelta(days=400):
        return None
    title = r["title"].strip()[:120]
    words = set(re.findall(r"\w{3,}", title.lower()))
    for e in events_between(start - dt.timedelta(hours=3), start + dt.timedelta(hours=3)):
        if words & set(re.findall(r"\w{3,}", e["title"].lower())) or abs(e["start"] - start.timestamp()) < 60:
            return None   # already there
    end = start + (dt.timedelta(days=1) if all_day else dt.timedelta(minutes=max(15, min(int(r.get("minutes") or 60), 720))))
    services.db.x("INSERT OR REPLACE INTO events (uid, source, title, start, end, all_day, location) VALUES (?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex, "local", title, start.timestamp(), end.timestamp(), int(all_day),
                   (r.get("location") or "")[:120]))
    services.emit({"type": "calendar_changed"})
    return "Added to your calendar: " + _fmt({"start": start.timestamp(), "end": end.timestamp(), "all_day": all_day,
                                              "title": title, "location": r.get("location") or ""}, True)



@tool("calendar_done", "Mark a calendar event or to-do as done (or not done with undo=true). title: words from the "
      "event's name; day: optional date or 'yesterday'/'today' to pick the right one.",
      {"title": {"type": "string"}, "day": {"type": "string"}, "undo": {"type": "boolean"}}, ["title"],
      label="Ticking off", modes=("astra", "friend"))
def calendar_done(ctx, title, day="", undo=False):
    e = find_event(title, day)
    if not e:
        return f"No event matching '{title}' found around then. Check with calendar_agenda."
    set_done(e["uid"], e["start"], not undo)
    return ("Marked as not done: " if undo else "Marked done: ") + _fmt(e, True).replace(" [done]", "")


DONE_HINT = re.compile(r"\b(done|finished|submitted|handed (it )?in|turned in|sent (it|the)|paid|went to|did (it|the|my)|"
                       r"completed|got it done|fertig|erledigt|abgegeben|hotov[eéoá]|odovzdal)\b", re.I)
DONE_SCHEMA = {"type": "object", "properties": {"done": {"type": "array", "items": {"type": "integer"}}},
               "required": ["done"]}


def auto_done(user_text):
    """'just submitted the essay' -> tick off the matching open item. Returns a note or None."""
    if not DONE_HINT.search(user_text or ""):
        return None
    now = dt.datetime.now()
    open_items = [e for e in events_between(now - dt.timedelta(days=7), now + dt.timedelta(days=14))
                  if not e["done"] and (e["task"] or e["start"] <= now.timestamp() + 3 * 3600)]
    if not open_items:
        return None
    listing = "\n".join(f"{i}. {_fmt(e, True)}" for i, e in enumerate(open_items[:25]))
    r = services.llm.json_task(
        "The user wrote a message. Which of these calendar items do they say they have now done, finished, submitted "
        "or attended? Only items they clearly say are done. Return their numbers (empty if none).",
        f"ITEMS:\n{listing}\n\nMESSAGE: {user_text}", DONE_SCHEMA)
    ticked = []
    for i in r.get("done") or []:
        if isinstance(i, int) and 0 <= i < len(open_items[:25]):
            e = open_items[i]
            set_done(e["uid"], e["start"], True)
            ticked.append(e["title"])
    return ("Ticked off in your calendar: " + ", ".join(ticked)) if ticked else None
