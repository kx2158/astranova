"""Nova texting first: now and then (never spammy) Nova sends you a message on her own - a heads-up before
something in your calendar, a ping about important mail (uni stuff etc.), a "how was your day" in the evening,
or a follow-up on something you told her. Quiet hours and a daily limit keep it human."""
import datetime as dt
import random
import time

from . import services

LEVELS = {  # min hours between check-ins, max per day (calendar reminders don't count)
    "rare": (8, 1),
    "normal": (3, 3),
    "often": (1.5, 5),
}
PLAN_WORDS = ("exam", "test", "deadline", "interview", "trip", "concert", "date", "appointment", "submit", "presentation",
              "project", "moving", "birthday", "gig", "stream", "doctor", "prüfung", "abgabe")


def _hm(s, default):
    try:
        h, m = (int(x) for x in (s or default).split(":"))
        return dt.time(h % 24, m % 60)
    except Exception:  # noqa: BLE001
        h, m = (int(x) for x in default.split(":"))
        return dt.time(h, m)


def quiet_now(now=None):
    f = services.config.get("friend")
    now = (now or dt.datetime.now()).time()
    a, b = _hm(f.get("quiet_start"), "23:00"), _hm(f.get("quiet_end"), "09:00")
    return (now >= a or now < b) if a > b else (a <= now < b)


class Companion:
    def __init__(self):
        self.checkin = None      # set by app: fn(reason) -> None (runs Nova with a [check-in] message)
        self.last_user_msg = 0   # set by app whenever you write to Nova
        self._pending_mail = []

    # called by the mail watcher (any thread)
    def queue_mail(self, row):
        self._pending_mail.append(row)

    def _state(self):
        today = dt.date.today().isoformat()
        st = services.db.kv_get("nova_checkins") or {}
        if st.get("day") != today:
            st = {"day": today, "count": 0, "last": st.get("last", 0), "done": []}
        return st

    def _save(self, st):
        services.db.kv_set("nova_checkins", st)

    def tick(self):
        f = services.config.get("friend")
        if not (f.get("proactive") and services.config.get("setup_done") and self.checkin):
            return
        if not services.llm.is_up():
            return
        now = dt.datetime.now()
        st = self._state()

        # 1. calendar heads-up (allowed even in a busy chat, but not in quiet hours)
        if not quiet_now(now):
            soon = self._upcoming(now)
            if soon:
                services.db.x("UPDATE events SET reminded=1 WHERE uid=? AND source=? AND start=?",
                              (soon["uid"], soon["source"], soon["start"]))
                s = dt.datetime.fromtimestamp(soon["start"])
                mins = max(1, int((soon["start"] - now.timestamp()) // 60))
                self._fire(st, f"Heads-up: '{soon['title']}' starts at {s:%H:%M} (in {mins} min)"
                           + (f" at {soon['location']}" if soon.get("location") else "")
                           + ". Remind them casually, maybe ask if they're ready.", count=False)
                return

        if quiet_now(now):
            return
        gap_h, per_day = LEVELS.get(f.get("proactive_level") or "normal", LEVELS["normal"])
        recently_talked = time.time() - self.last_user_msg < 25 * 60

        # 2. important mail - mention it soon, but don't interrupt an ongoing conversation
        if self._pending_mail and not recently_talked and time.time() - st["last"] > 20 * 60:
            rows, self._pending_mail = self._pending_mail[:4], self._pending_mail[4:]
            lines = "; ".join(f"{r['category']} mail from {r['sender'][:50]}: {r['summary'][:140]}" for r in rows)
            self._fire(st, f"New important email: {lines}. Tell them in a sentence or two (what it is, anything they "
                           f"need to do and when).", count=False)
            return

        if st["count"] >= per_day or time.time() - st["last"] < gap_h * 3600 or recently_talked:
            return

        # 3. morning heads-up about today's plan
        if 8 <= now.hour < 12 and "morning" not in st["done"]:
            from .tools import agenda
            today = agenda.day_summary()
            if today:
                st["done"].append("morning")
                self._fire(st, "Morning heads-up about their day: " + "; ".join(today) +
                           ". Say good morning like a friend and mention what's coming up (not as a list).")
                return

        # 4. evening "how was your day"
        if 19 <= now.hour < 22 and "evening" not in st["done"] and random.random() < 0.35:
            st["done"].append("evening")
            from .tools import agenda
            today = agenda.day_summary()
            self._fire(st, "Evening check-in: ask how their day went" +
                       (f" (today they had: {'; '.join(today)})" if today else "") + ". Keep it light.")
            return

        # 5. follow up on something they told you
        if 12 <= now.hour < 21 and "followup" not in st["done"] and random.random() < 0.15:
            mems = [m for m in services.db.memories(200) if any(w in m["text"].lower() for w in PLAN_WORDS)
                    and time.time() - (m["created"] or 0) < 21 * 86400]
            if mems:
                m = random.choice(mems)
                st["done"].append("followup")
                self._fire(st, f"Follow up on something they told you before: '{m['text']}'. Ask about it naturally.")

    def _upcoming(self, now):
        mins = int(services.config.get("calendar", "remind_minutes") or 30)
        rows = services.db.q("SELECT e.* FROM events e LEFT JOIN event_done d ON d.uid=e.uid AND d.start=e.start "
                             "WHERE e.all_day=0 AND e.reminded=0 AND d.uid IS NULL AND e.start>? AND e.start<=? "
                             "ORDER BY e.start LIMIT 1", (now.timestamp() + 4 * 60, now.timestamp() + mins * 60))
        return rows[0] if rows else None

    def _fire(self, st, reason, count=True):
        if count:
            st["count"] += 1
        st["last"] = time.time()
        self._save(st)
        services.db.log("nova", f"check-in: {reason[:200]}")
        try:
            self.checkin(reason)
        except Exception as e:  # noqa: BLE001
            services.db.log("nova", f"check-in failed: {e}")
