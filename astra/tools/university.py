# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Your university: courses, course contents and deadlines.

- Moodle: the official mobile-app web service (username + password, or SSO portals via the browser fallback).
- Canvas: a personal access token (Canvas > Account > Settings > New access token).
- Anything else: Astra's browser opens the portal with your saved login (you log in once).
"""
import datetime
import time

import requests

from .. import services
from . import tool

_tok = {"moodle": None, "uid": None}


def _cfg():
    return services.config.get("university")


def uni_missing():
    c = _cfg()
    if not c.get("url"):
        return "University not set up (Settings > Accounts > University)."
    return None


def _base():
    return _cfg()["url"].rstrip("/")


# ---- Moodle ----------------------------------------------------------------------------------------------
def _moodle_token():
    if _tok["moodle"]:
        return _tok["moodle"]
    c = _cfg()
    r = requests.post(_base() + "/login/token.php", timeout=20, data={
        "username": c.get("username"), "password": services.config.get_secret("uni_password"), "service": "moodle_mobile_app"})
    d = r.json()
    if "token" not in d:
        raise RuntimeError(f"Moodle login failed ({d.get('error', 'no token')}). If your uni uses single sign-on, set "
                           f"the provider to 'browser' instead.")
    _tok["moodle"] = d["token"]
    return d["token"]


def moodle(fn, **params):
    q = {"wstoken": _moodle_token(), "wsfunction": fn, "moodlewsrestformat": "json"}
    for k, v in params.items():
        if isinstance(v, list):
            for i, x in enumerate(v):
                q[f"{k}[{i}]"] = x
        else:
            q[k] = v
    r = requests.post(_base() + "/webservice/rest/server.php", data=q, timeout=30)
    d = r.json()
    if isinstance(d, dict) and d.get("exception"):
        raise RuntimeError(f"Moodle: {d.get('message')}")
    return d


def _moodle_uid():
    if not _tok["uid"]:
        _tok["uid"] = moodle("core_webservice_get_site_info")["userid"]
    return _tok["uid"]


# ---- Canvas ----------------------------------------------------------------------------------------------
def canvas(path, **params):
    r = requests.get(_base() + "/api/v1" + path, params=params, timeout=30,
                     headers={"Authorization": f"Bearer {services.config.get_secret('canvas_token')}"})
    if r.status_code == 401:
        raise RuntimeError("Canvas token is wrong or expired (Settings > Accounts > University).")
    r.raise_for_status()
    return r.json()


def _fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "no date"


def _browser_open(ctx, path=""):
    from .browser import browser_open
    return browser_open(ctx, _base() + path)


def _match(courses, name, key="fullname"):
    n = str(name).lower()
    return next((c for c in courses if str(c.get("id")) == n or n in str(c.get(key, "")).lower()
                 or n in str(c.get("shortname", c.get("course_code", ""))).lower()), None)


@tool("uni_courses", "List the user's university courses.", label="Checking your courses", needs=uni_missing)
def uni_courses(ctx):
    p = _cfg().get("provider")
    if p == "moodle":
        cs = moodle("core_enrol_get_users_courses", userid=_moodle_uid())
        return "\n".join(f"- {c['fullname']} ({c.get('shortname', '')}) id {c['id']}" for c in cs) or "No courses."
    if p == "canvas":
        cs = canvas("/courses", enrollment_state="active", per_page=100)
        return "\n".join(f"- {c.get('name')} ({c.get('course_code', '')}) id {c['id']}" for c in cs if c.get("name")) or "No courses."
    return "Opened your university portal:\n" + _browser_open(ctx)


@tool("uni_course", "Look inside one course: sections, materials, links, announcements. course: name, code or id.",
      {"course": {"type": "string"}}, ["course"], label="Opening course", needs=uni_missing)
def uni_course(ctx, course):
    p = _cfg().get("provider")
    if p == "moodle":
        cs = moodle("core_enrol_get_users_courses", userid=_moodle_uid())
        c = _match(cs, course)
        if not c:
            return f"No course matches '{course}'."
        secs = moodle("core_course_get_contents", courseid=c["id"])
        out = [f"{c['fullname']}"]
        for s in secs:
            mods = [f"    - {m['name']} [{m.get('modname')}]" + (f" {m['url']}" if m.get("url") else "") for m in s.get("modules", [])]
            if mods:
                out.append(f"  {s.get('name') or 'Section'}")
                out += mods[:25]
        return "\n".join(out)[:7000]
    if p == "canvas":
        cs = canvas("/courses", enrollment_state="active", per_page=100)
        c = _match(cs, course, "name")
        if not c:
            return f"No course matches '{course}'."
        mods = canvas(f"/courses/{c['id']}/modules", **{"include[]": "items", "per_page": 50})
        out = [c.get("name", "")]
        for m in mods:
            out.append(f"  {m['name']}")
            out += [f"    - {i['title']} [{i['type']}]" + (f" {i.get('html_url', '')}" if i.get("html_url") else "")
                    for i in m.get("items", [])[:25]]
        return "\n".join(out)[:7000]
    return _browser_open(ctx) + f"\n\nFind the course '{course}' on this page with the browser tools."


@tool("uni_deadlines", "Upcoming assignments, quizzes and events across the user's courses.", label="Checking deadlines",
      needs=uni_missing)
def uni_deadlines(ctx):
    p = _cfg().get("provider")
    if p == "moodle":
        now = int(time.time())
        ev = moodle("core_calendar_get_action_events_by_timesort", timesortfrom=now, timesortto=now + 60 * 86400, limitnum=40)
        items = ev.get("events", [])
        return "\n".join(f"- {_fmt_ts(e.get('timesort'))}: {e.get('name')} ({e.get('course', {}).get('fullname', '')})"
                         for e in items) or "Nothing due in the next 60 days."
    if p == "canvas":
        ev = canvas("/users/self/upcoming_events")
        return "\n".join(f"- {e.get('start_at') or (e.get('assignment') or {}).get('due_at', '')}: {e.get('title')}"
                         for e in ev) or "Nothing upcoming."
    return _browser_open(ctx) + "\n\nLook for the calendar/deadlines section with the browser tools."
