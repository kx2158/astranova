"""Active memory: after a chat turn, a quick background pass writes down lasting facts from what you said, and
updates or removes memories that are now wrong ("I moved to Leeds" replaces "Lives in York"). The model can still
call remember itself; this just makes sure nothing important slips through. Runs off the UI thread and never
blocks a reply."""
import re
import threading

from . import services

_busy = threading.Lock()
KEEPS = ["day", "week", "month", "season", "year", "until", "forever"]
KEEP_GUIDE = """How long to keep each fact (keep):
- day: what they're doing right now or tonight (playing a game tonight, watching a movie, a mood today)
- week: short phases (sick this week, playing a new game a lot, busy with one assignment)
- month: current things that change (the show or game they're into, a current project sprint, a temporary job)
- season: semester stuff, a current relationship phase, ongoing projects
- year: their courses this year, where they live while studying, a longer project
- until: tied to a date (exam, trip, appointment, deadline): set until to that date YYYY-MM-DD
- forever: who they are and lasting things (people in their life, studies, identity, tastes, skills, values)"""

SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "keep": {"type": "string", "enum": KEEPS},
            "until": {"type": "string"}}, "required": ["text", "keep"]}},
        "updates": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "text"]}},
        "remove": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["facts", "updates", "remove"],
}

SYSTEM = """You maintain the long-term memory a personal assistant keeps about its user. Output JSON only.
From the latest exchange, find facts about the USER that will still matter in a week or more: people in their life
(names + who they are), plans and dates, projects, studies/uni, work, places, tastes, routines, goals, strong
feelings about something ongoing. Write each as one short third-person line ("Has a ceramics exam on 12 Nov",
"Best friend is called Mira") and decide how long it stays useful (keep). Skip small talk, one-off moods, questions, things the assistant did, and anything
already in EXISTING MEMORIES unless it changed.
- facts: new memories (0-4). Usually 0 - only save what is clearly lasting.
- updates: EXISTING memory ids whose text is now outdated, with the corrected full text.
- remove: EXISTING memory ids that the user said are wrong or no longer true.
Never store passwords, ID or card numbers.
""" + KEEP_GUIDE

_SKIP = re.compile(r"^\s*(/\w+|ok|okay|k|yes|no|thanks|thx|ty|lol|haha|hm+|nice|cool)\W*$", re.I)


def after_turn(mode, user_text, reply):
    if not user_text or len(user_text.strip()) < 12 or _SKIP.match(user_text) or user_text.startswith("["):
        return
    if not services.config.get("setup_done") or not services.config.get("friend", "auto_memory"):
        return
    threading.Thread(target=_run, args=(mode, user_text, reply), daemon=True, name="memory").start()


def _run(mode, user_text, reply):
    if not _busy.acquire(blocking=False):
        return  # one pass at a time is plenty
    try:
        existing = services.db.recall(user_text, 12)
        ex = "\n".join(f"#{r['id']} {r['text']}" for r in existing) or "(none relevant)"
        user = (f"EXISTING MEMORIES:\n{ex}\n\nLATEST EXCHANGE:\nUser: {user_text[:2000]}\n"
                f"{'Nova' if mode == 'friend' else 'Astra'}: {(reply or '')[:800]}")
        try:
            r = services.llm.json_task(SYSTEM, user, SCHEMA)
        except Exception:  # noqa: BLE001
            return
        ids = {m["id"] for m in existing}
        changed = False
        for f in (r.get("facts") or [])[:4]:
            if isinstance(f, str):
                f = {"text": f, "keep": ""}
            text = (f.get("text") or "").strip() if isinstance(f, dict) else ""
            if 6 < len(text) < 300 and not re.search(r"password|passwort|\b\d{12,}\b", text, re.I):
                keep = f.get("keep") if f.get("keep") in KEEPS else ""
                services.db.remember(text, source="auto", keep=keep, until=f.get("until"))
                changed = True
        for u in r.get("updates") or []:
            if u.get("id") in ids and u.get("text"):
                services.db.update_memory(u["id"], u["text"])
                changed = True
        for mid in r.get("remove") or []:
            if mid in ids:
                services.db.forget(mid)
                changed = True
        if changed:
            services.emit({"type": "memory_changed"})
    finally:
        _busy.release()


# ---- lifetimes for memories that don't have one yet (old ones, or saved by the remember tool) -------------
CLASSIFY_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "integer"}, "keep": {"type": "string", "enum": KEEPS}, "until": {"type": "string"}},
    "required": ["id", "keep"]}}}, "required": ["items"]}


def classify_pending(batch=25):
    """Give memories without a lifetime one. Runs from the scheduler, a batch at a time."""
    if not services.config.get("setup_done"):
        return 0
    import datetime
    rows = services.db.q("SELECT id, text, created FROM memories WHERE keep='' OR keep IS NULL ORDER BY id DESC LIMIT ?",
                         (batch,))
    if not rows or not _busy.acquire(blocking=False):
        return 0
    try:
        listing = "\n".join(f"#{r['id']} (saved {datetime.date.fromtimestamp(r['created'])}) {r['text']}" for r in rows)
        try:
            r = services.llm.json_task("Decide how long each memory about the user stays useful. Output JSON.\n" +
                                       KEEP_GUIDE + f"\nToday is {datetime.date.today()}.", listing, CLASSIFY_SCHEMA)
        except Exception:  # noqa: BLE001
            return 0
        ids, n = {x["id"] for x in rows}, 0
        created = {x["id"]: x["created"] for x in rows}
        for it in r.get("items") or []:
            if it.get("id") in ids and it.get("keep") in KEEPS:
                services.db.set_memory_keep(it["id"], it["keep"], it.get("until"))
                if it["keep"] not in ("forever", "until"):   # lifetime counts from when it was saved
                    from .db import KEEP
                    services.db.x("UPDATE memories SET expires=? WHERE id=?",
                                  (created[it["id"]] + KEEP[it["keep"]] * 86400, it["id"]))
                n += 1
        services.db.purge_expired()
        if n:
            services.emit({"type": "memory_changed"})
        return n
    finally:
        _busy.release()
