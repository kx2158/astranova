"""Background loop: runs due routines (scheduled tasks and reminders) plus periodic jobs (mail watcher, calendar
refresh, Nova's check-ins)."""
import threading
import time

from . import services


class Scheduler:
    def __init__(self):
        self.thread = None
        self.run_task = None   # set by app: fn(prompt, title, conv_id) -> final text
        self.jobs = []         # [{name, every (seconds or callable), fn, last, running}]

    def every(self, name, seconds, fn, first_delay=20):
        self.jobs.append({"name": name, "every": seconds, "fn": fn, "last": time.time() - _secs(seconds) + first_delay,
                          "running": False})

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self.thread.start()

    def _loop(self):
        time.sleep(8)
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                services.db.log("routine", f"scheduler error: {e}")
            time.sleep(15)

    def tick(self):
        now = time.time()
        self._watches(now)
        for job in self.jobs:
            if not job["running"] and now - job["last"] >= _secs(job["every"]):
                job["last"] = now
                job["running"] = True
                threading.Thread(target=self._job, args=(job,), daemon=True, name=job["name"]).start()
        due = services.db.q("SELECT * FROM routines WHERE enabled=1 AND next_run<=?", (now,))
        for r in due:
            from .tools.system import next_after
            nxt = next_after(r["next_run"], r["repeat"])
            if nxt:
                services.db.x("UPDATE routines SET next_run=?, last_run=? WHERE id=?", (nxt, now, r["id"]))
            else:
                services.db.x("UPDATE routines SET enabled=0, last_run=? WHERE id=?", (now, r["id"]))
            services.emit({"type": "routines_changed"})
            threading.Thread(target=self._run, args=(r,), daemon=True).start()

    @staticmethod
    def _job(job):
        try:
            job["fn"]()
        except Exception as e:  # noqa: BLE001
            services.db.log("routine", f"{job['name']} error: {str(e)[:300]}")
        finally:
            job["running"] = False

    def _watches(self, now):
        return

    def run_now(self, rid):
        rows = services.db.q("SELECT * FROM routines WHERE id=?", (int(rid),))
        if rows:
            threading.Thread(target=self._run, args=(rows[0],), daemon=True).start()

    def _run(self, r):
        if not self.run_task:
            return
        prompt = (f"[Scheduled routine #{r['id']} '{r['title']}' is due now.] {r['prompt']}\n"
                  f"If this is a reminder, call notify_me with a short friendly reminder text. Otherwise do the task, "
                  f"then call notify_me with a one-line result.")
        try:
            res = self.run_task(prompt, f"Routine: {r['title']}", f"routine-{r['id']}")
        except Exception as e:  # noqa: BLE001
            res = f"Failed: {e}"
        services.db.x("UPDATE routines SET last_result=? WHERE id=?", ((res or "")[:500], r["id"]))
        services.db.log("routine", f"{r['title']}: {(res or '')[:300]}")
        services.emit({"type": "routines_changed"})


def _secs(every):
    return every() if callable(every) else every
