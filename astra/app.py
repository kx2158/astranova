# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""JavaScript <-> Python API used by the UI (pywebview)."""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from . import __version__, bootstrap, hardware, playbooks, services, tools
from .agent import Agent, display_messages, task_view
from .llm import MODEL_CHOICES, VISION_CHOICES
from .paths import app_dir, is_public, workspace_dir

FRIEND_CONV = "friend"
DISCORD_CONV = "discord-dm"


# follow-ups that belong to the running job (they wait for it instead of starting a new one)
STEER_RE = re.compile(r"^\s*(stop|wait|no\b|nope|actually|also|instead|and (also|then)|then|after (that|this)|"
                      r"cancel|don'?t|dont|use|make it|change|fix|continue|keep going|go on|ok|okay|yes|yeah|sure)\b", re.I)


class Api:
    def __init__(self):
        self._window = None
        self._agents = {}
        self._queues = {}      # conv id -> messages waiting to run after the current turn
        self._lock = threading.Lock()
        self._task_source = {}

    # ---- wiring ----------------------------------------------------------------------
    def _attach(self, window):
        self._window = window

    def _emit(self, evt):
        if services.phone:
            try:
                services.phone.push(evt)
            except Exception:  # noqa: BLE001
                pass
        if not self._window:
            return
        try:   # run_js doesn't wait for the window to answer, so a busy UI can never block Astra (or freeze)
            js = f"window.astra && window.astra.onEvent({json.dumps(evt, default=str)})"
            (getattr(self._window, "run_js", None) or self._window.evaluate_js)(js)
        except Exception:
            pass
        if evt.get("type") == "request":
            threading.Thread(target=self._surface, daemon=True).start()

    def _surface(self):
        """Bring Astra to the front when it needs you (it may be busy driving another app)."""
        try:
            self._window.restore()
            self._window.on_top = True
            time.sleep(0.6)
            self._window.on_top = False
        except Exception:
            pass

    def _boot(self):
        """Runs once after the window opens."""
        from .companion import Companion
        from .phone import PhoneLink
        from .presence import Presence
        from .tools import agenda, mail
        from .scheduler import Scheduler
        from .tools import personal
        from .tools.browser import BrowserService
        from .tools.culture import refresh_cache
        from .tools.discord_bot import DiscordService
        from .tools.discord_web import DiscordWeb
        from .tools.system import build_index, start_stop_hotkey
        from .tools.telegram_bot import TelegramService

        tools.load_all()
        services.emit = self._emit
        services.bridge.emit = self._emit
        services.stop_all = self.stop_everything
        services.running_ids = lambda: [c for c in self._agents if not c.startswith(("dg-", "dc-"))]
        services.browser = BrowserService()
        # Discord bot: friend mode only
        services.discord = DiscordService()
        services.discord.on_owner = self._discord_owner
        services.discord.on_guest = self._guest
        services.discord.on_game = self._game
        services.discord.on_stop = self.stop_everything
        services.bridge.discord = services.discord
        # Telegram bot: friend by default, /menu for Astra
        services.telegram = TelegramService()
        services.telegram.on_message = self._telegram
        services.telegram.on_game = self._game
        services.telegram.on_stop = self.stop_everything
        services.telegram.on_status = self._status_text
        services.bridge.telegram = services.telegram
        services.telegram.start()
        # your own Discord account (background) + Auto-Astra
        services.discord_web = DiscordWeb()
        services.discord_web.start_auto()
        # Rich Presence
        services.presence = Presence()
        services.presence.run()
        personal.handoff = self._start_task
        services.companion = Companion()
        services.companion.checkin = self.nova_checkin
        services.phone = PhoneLink()
        services.phone.api = self
        services.scheduler = Scheduler()
        services.scheduler.run_task = self._run_blocking
        services.scheduler.every("mail", lambda: max(3, int(services.config.get("mail", "check_minutes") or 10)) * 60,
                                 lambda: mail.check_new(services.companion.queue_mail), first_delay=45)
        services.scheduler.every("calendar", 30 * 60, agenda.refresh, first_delay=5)
        from . import procs
        services.scheduler.every("engine-speed", 30, procs.unthrottle_engine, first_delay=10)
        services.scheduler.every("nova", 60, services.companion.tick, first_delay=90)
        from . import memory as memmod
        services.scheduler.every("memory", 20 * 60, memmod.classify_pending, first_delay=120)
        services.scheduler.every("memory-purge", 60 * 60, services.db.purge_expired, first_delay=30)
        from . import personalize
        personalize.reload_moods()
        services.scheduler.every("personalize", 6 * 3600, lambda: personalize.run(emit=self._emit), first_delay=20 * 60)
        from . import updater
        updater.after_update(self._emit)
        services.scheduler.every("updates", 6 * 3600, lambda: updater.check(emit=self._emit), first_delay=60)
        services.scheduler.start()
        try:
            services.phone.apply()
        except Exception:  # noqa: BLE001
            pass
        services.db.x("UPDATE tasks SET status='interrupted', finished=? WHERE status='running'", (time.time(),))
        start_stop_hotkey()
        services.discord.apply()
        threading.Thread(target=refresh_cache, daemon=True).start()
        threading.Thread(target=lambda: (time.sleep(90), build_index()), daemon=True).start()
        if services.config.get("setup_done"):
            threading.Thread(target=self._start_engine, daemon=True).start()

    def _start_engine(self):
        ok = bootstrap.start_engine()
        self._emit({"type": "engine", "up": ok})
        if ok and not services.llm.cloud():
            try:  # load the model into VRAM so the first message is instant
                services.llm.chat([{"role": "user", "content": "hi"}], think=False)
            except Exception:
                pass
        self._emit({"type": "engine", "up": ok, "warm": ok})
        if ok and not services.llm.cloud():
            self._check_fit()

    def _check_fit(self):
        """A model too big for this PC makes every reply crawl (laptops especially). Offer the setup that fits."""
        rec, name = hardware.recommend(), services.config.get("model", "name")
        if name == rec["model"]:
            return
        share = services.llm.gpu_share(name)
        light = services.llm.light()
        heavy = light and rec.get("light") and _size_rank(name) > _size_rank(rec["model"])
        if heavy or (share is not None and share < 0.8):
            self._emit({"type": "model_too_big", "model": name, "suggest": rec["model"],
                        "text": (f"{name} is too heavy for this PC, so replies are slow. Click to switch to "
                                 f"{rec['model']}, made for it (your chats stay).")})

    # ---- running agents -----------------------------------------------------------------
    def _register(self, cid, agent):
        with self._lock:
            if cid in self._agents:
                return False
            self._agents[cid] = agent
            return True

    def _release(self, cid):
        with self._lock:
            self._agents.pop(cid, None)
        self._emit({"type": "conv_list_changed"})
        if services.presence:
            threading.Thread(target=services.presence.update, daemon=True).start()

    def _run_blocking(self, text, title, cid, mode="astra", source="app", guest=None, owner=True, internal=False,
                      origin=None, task_id=None, kind=None):
        kind = kind or ("friend" if mode == "friend" else ("guest" if mode == "guest" else "astra"))
        services.db.new_conversation(title, cid, kind)
        agent = Agent(cid, self._emit, mode=mode, page_key=cid if mode == "astra" else "agent", source=source,
                      guest=guest, owner=owner, origin=origin, task_id=task_id)
        waited = 0
        while not self._register(cid, agent):
            time.sleep(1)
            waited += 1
            if waited > 600:
                return "Busy."
        self._emit({"type": "conv_list_changed"})
        try:
            if mode == "astra" and origin != "nova":
                services.panic.clear()
            return agent.run(text, internal=internal)
        finally:
            self._release(cid)

    def _start_task(self, text, title):
        """Nova handed Astra a job: run it in the background, show it live inside Nova's chat, hand the result back."""
        tid = uuid.uuid4().hex[:8]
        cid = f"nova-task-{tid}"
        services.db.x("INSERT INTO tasks (id, origin, task, status, created) VALUES (?,?,?,?,?)",
                      (tid, "nova", text, "running", time.time()))
        services.db.new_conversation(title or "Job from Nova", cid, "task")
        nova_agent = self._agents.get(FRIEND_CONV)
        self._task_source[tid] = getattr(nova_agent, "source", "app")
        services.panic.clear()
        self._emit({"type": "task_start", "conv": FRIEND_CONV, "task": tid, "text": text})
        threading.Thread(target=self._task_worker, args=(tid, cid, text, title), daemon=True, name=f"task-{tid}").start()
        return tid

    def _task_worker(self, tid, cid, text, title):
        status, result = "done", ""
        try:
            result = self._run_blocking(text, title, cid, mode="astra", origin="nova", task_id=tid, kind="task")
            if result == "Stopped.":
                status = "stopped"
            elif result.startswith("Something went wrong"):
                status = "failed"
        except Exception as e:  # noqa: BLE001
            status, result = "failed", f"Something went wrong: {e}"
        services.db.x("UPDATE tasks SET status=?, result=?, finished=? WHERE id=?",
                      (status, (result or "")[:6000], time.time(), tid))
        self._emit({"type": "task_done", "conv": FRIEND_CONV, "task": tid, "status": status, "text": (result or "")[:3000]})
        if status == "stopped":
            return   # you stopped it yourself - Nova doesn't need to report on it
        note = {"done": "[Astra finished]", "failed": "[Astra finished - it didn't work]"}.get(status, "[Astra finished]")
        msg = f"{note} Job: {text[:600]}\n\nAstra's report:\n{result[:6000]}"
        source = self._task_source.pop(tid, "app")
        reply = self._run_blocking(msg, "Nova", FRIEND_CONV, mode="friend", internal=True, source="app")
        if reply and reply != "Stopped.":
            self._relay_to_bot(source, reply)

    def nova_checkin(self, reason):
        """Nova texts first (calendar heads-up, important mail, how was your day...)."""
        def work():
            reply = self._run_blocking(f"[check-in] {reason}", "Nova", FRIEND_CONV, mode="friend", internal=True)
            if not reply or reply == "Stopped." or reply.startswith("ugh something broke"):
                return
            fname = services.config.get("friend", "name") or "Nova"
            self._emit({"type": "nova_checkin", "conv": FRIEND_CONV, "text": reply[:300], "name": fname})
            if services.config.get("friend", "proactive_discord"):
                self._relay_to_bot("discord", reply)
        threading.Thread(target=work, daemon=True, name="nova-checkin").start()

    def _relay_to_bot(self, source, text):
        """Nova's follow-up (Astra's result) goes back to where you asked: Telegram or Discord."""
        try:
            if source == "telegram" and services.telegram and services.telegram.ready():
                services.telegram.send(text)
            elif source == "discord" and services.discord and services.discord.ready():
                services.discord.dm_owner(text)
        except Exception:  # noqa: BLE001
            pass

    # bots
    def _discord_owner(self, text):
        if services.companion:
            services.companion.last_user_msg = time.time()
        return self._run_blocking(text, services.config.get("friend", "name") or "Friend", FRIEND_CONV, mode="friend",
                                  source="discord")

    def _guest(self, conv, author, text, where="Discord"):
        msgs = services.db.get_messages(conv)
        if len(msgs) > 40:  # keep guest memory short
            services.db.save_messages(conv, msgs[-24:], kind="guest")
        return self._run_blocking(f"{author}: {text}", "Guest", conv, mode="guest", source="discord",
                                  guest={"author": author, "where": where}, owner=False)

    def _telegram(self, text, mode):
        if mode != "astra" and services.companion:
            services.companion.last_user_msg = time.time()
        if mode == "astra":
            return self._run_blocking(text, "Telegram", "telegram", mode="astra", source="telegram")
        return self._run_blocking(text, services.config.get("friend", "name") or "Friend", FRIEND_CONV, mode="friend",
                                  source="telegram")

    def _game(self, name):
        from .tools import ToolContext
        from .tools.games import launch_game
        try:
            return launch_game(ToolContext(self._emit, source="remote"), name)
        except Exception as e:  # noqa: BLE001
            return f"couldn't launch: {e}"

    def _status_text(self):
        busy = [c for c in self._agents if not c.startswith(("dg-", "dc-"))]
        auto = services.config.get("auto_astra", "enabled")
        return (f"{'working on ' + str(len(busy)) + ' task(s)' if busy else 'idle'}. "
                f"Auto-Astra {'on' if auto else 'off'}. Model: {services.llm.model_label()}.")

    def stop_everything(self):
        with self._lock:
            agents = list(self._agents.values())
        for a in agents:
            a.stop()
        self._queues.clear()
        services.bridge.cancel_all()
        from . import voice
        voice.stop()
        return True

    # ---- state -------------------------------------------------------------------------------
    def get_state(self):
        return {
            "config": services.config.public(),
            "engine_up": services.llm.is_up(),
            "models": MODEL_CHOICES,
            "gpu": hardware.gpu(),
            "recommended": hardware.recommend(),
            "vision_models": VISION_CHOICES,
            "installed": services.llm.installed_models() if not services.llm.cloud() else [],
            "conversations": services.db.list_conversations(),
            "workspace": str(workspace_dir()),
            "requests": services.bridge.open_requests(),
            "running": list(self._agents.keys()),
            "tools": len(tools.REGISTRY),
            "windows": sys.platform == "win32",
            "public": is_public(),
            "version": __version__,
        }

    def welcome(self):
        """Data for the start screen."""
        from .tools import agenda
        now = datetime.datetime.now()
        evs = agenda.events_between(now, now.replace(hour=23, minute=59, second=59))
        nxt = services.db.q("SELECT title, start, location, all_day FROM events WHERE start>? AND all_day=0 "
                            "ORDER BY start LIMIT 1", (now.timestamp(),))
        mail_n = services.db.q("SELECT COUNT(*) AS n FROM mail_seen WHERE important=1 AND ts>?",
                               (time.time() - 86400,))[0]["n"]
        last_nova = next((m for m in reversed(display_messages(FRIEND_CONV)) if m["role"] == "assistant"), None)
        return {"today": [agenda._fmt(e) for e in evs][:6],
                "next": ({"title": nxt[0]["title"], "when": datetime.datetime.fromtimestamp(nxt[0]["start"]).strftime("%a %H:%M"),
                          "location": nxt[0]["location"]} if nxt else None),
                "mail": mail_n, "last_nova": (last_nova or {}).get("text", "")[:200],
                "tasks": services.db.q("SELECT COUNT(*) AS n FROM tasks WHERE status='running'")[0]["n"]}


    def running_list(self):
        return list(self._agents.keys())

    def run_setup(self):
        threading.Thread(target=bootstrap.run, args=(self._emit,), daemon=True).start()
        return True

    # ---- chat ----------------------------------------------------------------------------------
    def list_conversations(self):
        return services.db.list_conversations()

    def load_conversation(self, cid):
        return {"items": display_messages(cid), "running": cid in self._agents, "notes": self.get_notes(cid)}

    def delete_conversation(self, cid):
        self.stop(cid)
        services.db.delete_conversation(cid)
        services.db.x("DELETE FROM notes WHERE session=?", (cid,))
        return True

    def send_message(self, cid, text, mode="astra"):
        """Start a turn; if that chat is busy, queue the message to run right after."""
        text = (text or "").strip()
        if not text:
            return None
        if mode == "friend":
            cid = FRIEND_CONV
            services.db.new_conversation(services.config.get("friend", "name") or "Nova", FRIEND_CONV, "friend")
            if services.companion:
                services.companion.last_user_msg = time.time()
        elif not cid:
            cid = services.db.new_conversation()
        with self._lock:
            busy = cid in self._agents
            if busy:
                self._queues.setdefault(cid, []).append(text)
                n = len(self._queues[cid])
        if busy and mode == "astra" and not STEER_RE.match(text):
            # Astra is busy with something else: don't make this wait, run it alongside as its own task
            with self._lock:
                q = self._queues.get(cid) or []
                if q and q[-1] == text:
                    q.pop()
            side = services.db.new_conversation(text.split("\n")[0][:60])
            self._emit({"type": "side_task", "conv": side, "from": cid, "text": text[:120]})
            self._emit({"type": "conv_list_changed"})
            self._start_turn(side, text, mode)
            return {"conv": side, "side": True}
        if busy:
            self._emit({"type": "queue", "conv": cid, "count": n})
            return {"conv": cid, "queued": n}
        self._start_turn(cid, text, mode)
        return {"conv": cid}

    def _start_turn(self, cid, text, mode):
        agent = Agent(cid, self._emit, mode=mode, page_key="agent" if mode == "friend" else cid)
        if not self._register(cid, agent):
            with self._lock:
                self._queues.setdefault(cid, []).insert(0, text)
            return
        if mode == "astra":
            services.panic.clear()

        def work():
            final, tid = "", None
            if mode == "astra":   # Nova knows what Astra did in her own tab too
                tid = uuid.uuid4().hex[:8]
                services.db.x("INSERT INTO tasks (id, origin, task, status, created) VALUES (?,?,?,?,?)",
                              (tid, "astra", text[:1000], "running", time.time()))
            try:
                final = agent.run(text)
            finally:
                self._release(cid)
                if tid:
                    services.db.x("UPDATE tasks SET status=?, result=?, finished=? WHERE id=?",
                                  ("stopped" if final == "Stopped." else "done", (final or "")[:4000], time.time(), tid))
            if final and final != "Stopped.":
                from . import voice
                voice.speak(final, "friend" if mode == "friend" else "astra")
            with self._lock:
                q = self._queues.get(cid) or []
                nxt = q.pop(0) if q else None
                left = len(q)
            if nxt and not agent.stop_event.is_set():
                self._emit({"type": "queue", "conv": cid, "count": left, "started": nxt})
                self._start_turn(cid, nxt, mode)
        threading.Thread(target=work, daemon=True).start()

    def stop(self, cid):
        with self._lock:
            a = self._agents.get(cid)
            self._queues.pop(cid, None)
        if a:
            a.stop()
            services.bridge.cancel_all()
        return True

    def answer(self, pid, value):
        return services.bridge.resolve(pid, value)

    def friend_history(self):
        return {"items": display_messages(FRIEND_CONV), "running": FRIEND_CONV in self._agents,
                "tasks_running": [k[len("nova-task-"):] for k in self._agents if k.startswith("nova-task-")]}

    def task_details(self, tid):
        return {"id": tid, **task_view(tid), "running": f"nova-task-{tid}" in self._agents,
                "notes": self.get_notes(f"nova-task-{tid}")}

    def stop_task(self, tid):
        return self.stop(f"nova-task-{tid}")

    def get_notes(self, cid):
        rows = services.db.q("SELECT topic, data, source FROM notes WHERE session=? ORDER BY id", (cid,))
        out = []
        for r in rows:
            try:
                out.append({"topic": r["topic"], "item": json.loads(r["data"]), "source": r["source"]})
            except ValueError:
                continue
        return out

    def friend_clear(self):
        self.stop(FRIEND_CONV)
        services.db.delete_conversation(FRIEND_CONV)
        return True

    # ---- memory ----------------------------------------------------------------------------------
    def list_memories(self):
        return services.db.memories()

    def add_memory(self, text):
        services.db.remember(text, "you")
        return services.db.memories()

    def delete_memory(self, mid):
        services.db.forget(mid)
        return services.db.memories()

    def set_memory_keep(self, mid, keep):
        services.db.set_memory_keep(mid, keep)
        return services.db.memories()

    def edit_memory(self, mid, text):
        if (text or "").strip():
            services.db.update_memory(mid, text)
        return services.db.memories()

    # ---- routines --------------------------------------------------------------------------------
    def list_routines(self):
        return services.db.q("SELECT * FROM routines ORDER BY enabled DESC, next_run")

    def save_routine(self, data):
        from .tools.system import parse_when
        try:
            dt = parse_when(data.get("when") or "")
        except ValueError as e:
            return {"error": str(e)}
        title, prompt = (data.get("title") or "").strip(), (data.get("prompt") or "").strip()
        if not prompt:
            return {"error": "Describe what Astra should do."}
        title = title or prompt[:50]
        if data.get("id"):
            services.db.x("UPDATE routines SET title=?, prompt=?, next_run=?, repeat=?, enabled=1 WHERE id=?",
                          (title, prompt, dt.timestamp(), data.get("repeat") or "once", int(data["id"])))
        else:
            services.db.x("INSERT INTO routines (title, prompt, next_run, repeat, enabled, created) VALUES (?,?,?,?,1,?)",
                          (title, prompt, dt.timestamp(), data.get("repeat") or "once", time.time()))
        return {"ok": True}

    def toggle_routine(self, rid, enabled):
        services.db.x("UPDATE routines SET enabled=? WHERE id=?", (int(bool(enabled)), int(rid)))
        return True

    def delete_routine(self, rid):
        services.db.x("DELETE FROM routines WHERE id=?", (int(rid),))
        return True

    def run_routine(self, rid):
        services.scheduler.run_now(rid)
        return True

    # ---- settings ------------------------------------------------------------------------------------
    # ---- updates (public edition) ---------------------------------------------------------
    def update_status(self):
        from . import updater
        return updater.status()

    def update_check(self):
        from . import updater
        return updater.check(force=True, emit=self._emit)

    def update_restart(self):
        """Install the downloaded update now and open AstraNova again."""
        from . import updater
        if not updater.install_now():
            return {"ok": False}
        threading.Thread(target=lambda: (time.sleep(0.4), self._window.destroy()), daemon=True).start()
        return {"ok": True}

    def save_settings(self, section, values):
        if section == "email" and values.get("address") and not values.get("smtp_host"):
            from .tools.mail import preset_for
            values = {**preset_for(values["address"]), **{k: v for k, v in values.items() if v}}
        services.config.update(section, values)
        if section == "discord":
            services.discord.apply()
        if section == "model":
            threading.Thread(target=self._start_engine, daemon=True).start()
        if section == "phone":
            threading.Thread(target=services.phone.apply, daemon=True).start()
        if section == "browser":
            services.browser.reset()   # relaunch with the new options on next use
        return services.config.public()

    def set_secret(self, name, value):
        services.config.set_secret(name, (value or "").strip())
        if name == "discord_token":
            services.discord.apply()
        return services.config.public()

    def email_preset(self, address):
        from .tools.mail import preset_for
        return preset_for(address or "")

    def save_mail_account(self, data, password=""):
        from .tools.mail import preset_for
        accs = services.config.get("mail", "accounts") or []
        aid = data.get("id") or uuid.uuid4().hex[:8]
        acct = next((a for a in accs if a["id"] == aid), None)
        if acct is None:
            acct = {"id": aid}
            accs.append(acct)
        addr = (data.get("address") or "").strip()
        if not addr or "@" not in addr:
            return {"error": "Enter an email address."}
        pre = preset_for(addr)
        acct.update({"label": (data.get("label") or "").strip() or addr.split("@")[0], "address": addr,
                     "username": (data.get("username") or "").strip(),
                     "smtp_host": (data.get("smtp_host") or "").strip() or pre.get("smtp_host", ""),
                     "smtp_port": int(data.get("smtp_port") or pre.get("smtp_port") or 587),
                     "imap_host": (data.get("imap_host") or "").strip() or pre.get("imap_host", ""),
                     "imap_port": int(data.get("imap_port") or 993), "watch": bool(data.get("watch", True)),
                     "important": (data.get("important") or "").strip()})
        acct["smtp_security"] = "ssl" if acct["smtp_port"] == 465 else "starttls"
        services.config.update("mail", {"accounts": accs})
        if password:
            services.config.set_secret("mail_pw:" + aid, password)
        return {"ok": True, "id": aid, "config": services.config.public()}

    def delete_mail_account(self, aid):
        accs = [a for a in services.config.get("mail", "accounts") or [] if a["id"] != aid]
        services.config.update("mail", {"accounts": accs})
        services.config.set_secret("mail_pw:" + aid, "")
        return services.config.public()

    def test_email(self, aid=""):
        from .tools.mail import test_connection
        try:
            test_connection(aid)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def check_mail_now(self):
        from .tools import mail
        threading.Thread(target=mail.check_new, args=(services.companion.queue_mail,), daemon=True).start()
        return True

    def mail_digest(self):
        return services.db.q("SELECT account, sender, subject, category, important, summary, replied, ts FROM mail_seen "
                             "ORDER BY ts DESC LIMIT 60")

    # calendar
    def calendar_add_source(self, name, location):
        from .tools import agenda
        if not (location or "").strip():
            return {"error": "Paste the calendar link or pick an .ics file."}
        src = agenda.add_source(name, location)
        res = agenda.refresh(src["id"])
        r = res.get(src["id"])
        if isinstance(r, str) and r.startswith("error"):
            agenda.remove_source(src["id"])
            return {"error": "Couldn't read that calendar: " + r[7:]}
        return {"ok": True, "count": r, "config": services.config.public()}

    def calendar_remove_source(self, sid):
        from .tools import agenda
        agenda.remove_source(sid)
        return services.config.public()

    def calendar_refresh(self):
        from .tools import agenda
        return agenda.refresh()

    def calendar_week(self):
        from .tools import agenda
        now = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = agenda.events_between(now, now + datetime.timedelta(days=7))
        return [{"title": r["title"], "start": r["start"], "end": r["end"], "all_day": r["all_day"],
                 "location": r["location"], "source": r["source"]} for r in rows]

    def calendar_range(self, start_ts, end_ts):
        from .tools import agenda
        rows = agenda.events_between(datetime.datetime.fromtimestamp(start_ts), datetime.datetime.fromtimestamp(end_ts))
        names = {s.get("id"): s.get("name") for s in services.config.get("calendar", "sources") or []}
        return [{"uid": r["uid"], "title": r["title"], "start": r["start"], "end": r["end"], "all_day": r["all_day"],
                 "location": r["location"], "source": r["source"], "done": r["done"], "task": r["task"],
                 "repeat": r["source"] == "series", "mine": r["source"] in ("local", "series"),
                 "cal": "AstraNova" if r["source"] in ("local", "series") else names.get(r["source"], "Calendar")} for r in rows]

    def calendar_add_event(self, title, when, minutes=60, repeat=""):
        from .tools import agenda
        out = agenda.calendar_add(None, title, when, minutes, "", repeat or "", "", "")
        return {"ok": out.startswith("Added"), "text": out}

    def calendar_set_done(self, uid, start, done=True):
        from .tools import agenda
        agenda.set_done(uid, start, bool(done))
        return True

    def calendar_delete_event(self, uid, start=None, scope="one"):
        from .tools import agenda
        return agenda.remove_event(uid, start, scope)

    def pick_file(self, kind="ics"):
        import webview
        types = {"ics": ("Calendar files (*.ics)", "All files (*.*)"), "folder": ()}
        try:
            if kind == "folder":
                res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            else:
                res = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=types.get(kind, ()))
        except Exception:  # noqa: BLE001
            return ""
        return res[0] if res else ""

    # presence / phone
    def presence_image(self):
        """Copy the Rich Presence image next to your AstraNova files and show it in Explorer."""
        import shutil
        import subprocess
        from .paths import resource_path, workspace_dir
        dst = workspace_dir() / "discord-presence.png"
        shutil.copy2(resource_path("assets/discord-presence.png"), dst)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(dst)])
        return str(dst)

    def presence_status(self):
        return services.presence.status()

    def phone_status(self):
        return services.phone.status()

    def phone_new_key(self):
        return services.phone.new_token()

    def phone_send_link(self):
        st = services.phone.status()
        if not st.get("link"):
            return {"ok": False, "error": "The phone link isn't running yet."}
        if not (services.discord and services.discord.ready()):
            return {"ok": False, "error": "Connect the Discord bot first (Settings > Discord bot)."}
        services.discord.dm_owner(f"Your AstraNova phone link (open in Safari, then Share > Add to Home Screen):\n{st['link']}")
        return {"ok": True}

    def test_cloud(self):
        try:
            services.llm.test_cloud()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def reset_everything(self, phrase, workspace=False):
        """Settings > Start over: delete every chat, memory, setting, connection and saved password.
        Keeps only big downloads that hold nothing personal (AI models, voices, helper programs)."""
        if str(phrase or "").strip().upper() != "DELETE":
            return {"ok": False, "error": "Type DELETE to confirm."}
        import shutil
        from .paths import app_dir
        self.stop_everything()
        for svc in (services.phone, ):
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            services.discord_web.logout()
        except Exception:  # noqa: BLE001
            pass
        try:
            services.browser.reset()
        except Exception:  # noqa: BLE001
            pass
        services.config.reset()          # settings + every saved password and token
        try:
            services.discord.apply()     # bot goes offline (no token any more)
        except Exception:  # noqa: BLE001
            pass
        services.db.wipe()               # chats, memories, calendar, routines, tasks, mail history, file index
        root = app_dir()
        for name in ("personal_context.md", "culture_cache.json", "spotify_token.json", "local_api.json", "vr.log"):
            try:
                (root / name).unlink()
            except OSError:
                pass
        leftovers = []
        for name in ("browser-profile", "discord-profile", "vr-browser"):
            p = root / name
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                if p.exists():
                    leftovers.append(name)
        if leftovers:   # a browser still holds them open: finish on the next start
            (root / "reset.pending").write_text(json.dumps({"dirs": leftovers}), encoding="utf-8")
        if workspace:
            ws = workspace_dir()
            for child in ws.iterdir():
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        with self._lock:
            self._queues.clear()
        return {"ok": True}

    def local_models(self):
        """Settings > Model > Downloaded models: everything Ollama has on disk, and which ones AstraNova uses."""
        m = services.config.get("model")
        used = {m.get("name"), m.get("vision")}
        used |= {f"{u}:latest" for u in list(used) if u and ":" not in u}
        rows = [{**x, "in_use": x["name"] in used} for x in services.llm.models_detail()]
        rows.sort(key=lambda x: (not x["in_use"], -x["size"]))
        return {"models": rows, "unused_bytes": sum(x["size"] for x in rows if not x["in_use"])}

    def delete_model(self, name):
        if any(x["name"] == name and x["in_use"] for x in self.local_models()["models"]):
            return {"ok": False, "error": "That model is in use. Switch to another one first."}
        try:
            services.llm.delete_model(name)
            return {"ok": True, **self.local_models()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def delete_unused_models(self):
        gone = []
        for x in self.local_models()["models"]:
            if not x["in_use"]:
                try:
                    services.llm.delete_model(x["name"])
                    gone.append(x["name"])
                except Exception:  # noqa: BLE001
                    pass
        return {"ok": True, "deleted": gone, **self.local_models()}

    # ---- learning: Nova's style and AstraNova's own moods ------------------------------------------------------------
    def personal_learn_now(self):
        from . import personalize
        try:
            return personalize.run(force=True, emit=self._emit)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "why": str(e)[:200]}

    def personal_forget(self, what):
        from . import personalize
        personalize.forget(what)
        self._emit({"type": "custom_moods", "moods": personalize.public_moods()})
        return services.config.public()

    def use_best_model(self):
        """Settings > Model: pick what fits this graphics card and download it if needed."""
        res = bootstrap.auto_pick(force=True)
        m = services.config.get("model")
        if services.llm.has_model(m["name"]) and services.llm.has_model(m["vision"]):
            threading.Thread(target=bootstrap.pull_model, args=(m["name"], "model", self._emit), daemon=True).start()
        else:
            def work():
                bootstrap.pull_model(m["vision"], "vision", self._emit)
                bootstrap.pull_model(m["name"], "model", self._emit)
            threading.Thread(target=work, daemon=True).start()
        return {**res, "config": services.config.public()}

    def pull_model(self, name, kind="model"):
        threading.Thread(target=bootstrap.pull_model, args=(name, kind, self._emit), daemon=True).start()
        return True

    def get_playbooks(self):
        return playbooks.all_for_ui()

    def save_playbook(self, key, text):
        pb = services.config.get("apps", "playbooks") or {}
        if (text or "").strip() and text.strip() != playbooks.DEFAULTS.get(key, "").strip():
            pb[key] = text
        else:
            pb.pop(key, None)
        services.config.update("apps", {"playbooks": pb})
        return playbooks.all_for_ui()

    # ---- integrations -----------------------------------------------------------------------------------
    def spotify_connect(self):
        from .tools import music
        def work():
            try:
                r = music.connect()
                self._emit({"type": "spotify", "ok": True, "user": r["user"]})
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "spotify", "ok": False, "error": str(e)[:300]})
        threading.Thread(target=work, daemon=True).start()
        return True

    def spotify_status(self):
        from .tools import music
        if music.spotify_missing():
            return {"connected": False}
        try:
            me = music.sp().current_user()
            return {"connected": True, "user": me.get("display_name") or me.get("id")}
        except Exception as e:  # noqa: BLE001
            return {"connected": False, "error": str(e)[:200]}

    def spotify_disconnect(self):
        from .tools import music
        music.disconnect()
        return {"connected": False}

    def app_action(self, name):
        """One-click setup/check buttons in Settings > Apps."""
        from .tools import ToolContext, call
        ctx = ToolContext(self._emit)
        return call(name, {}, ctx)

    def discord_status(self):
        return services.discord.status()

    def discord_pair(self):
        if not services.discord.connected:
            return {"ok": False, "error": "Connect the bot first (paste the token, turn it on)."}
        return {"ok": True, "code": services.discord.start_pairing(), **services.discord.status()}

    def discord_unpair(self):
        services.discord.unpair()
        return services.discord.status()

    def discord_test(self):
        try:
            ok = services.discord.dm_owner("Test from AstraNova. DMs work.")
            return {"ok": bool(ok), "error": None if ok else "Not paired yet."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def panic(self):
        services.panic.set()
        self.stop_everything()
        return True

    def list_watches(self):
        return []

    # ---- personal context -------------------------------------------------------------------------------
    def get_context(self):
        from . import context
        return context.load()

    def save_context(self, text):
        from . import context
        context.save(text)
        return True

    def import_context(self):
        """Native file picker, then import in the background (ChatGPT exports can be big)."""
        import webview
        from . import context
        picked = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                                 file_types=("Exports and notes (*.json;*.txt;*.md;*.html)", "All files (*.*)"))
        if not picked:
            return {"ok": False}
        path = picked[0]

        def work():
            try:
                raw = open(path, encoding="utf-8", errors="replace").read()
                msg = context.import_text(raw, os.path.basename(path))
            except Exception as e:  # noqa: BLE001
                msg = f"Import failed: {e}"
            self._emit({"type": "context_imported", "text": msg})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "file": os.path.basename(path)}

    # ---- voice ------------------------------------------------------------------------------------------------
    def speak(self, text):
        from . import voice
        return voice.speak(text, force=True)

    def stop_speaking(self):
        from . import voice
        voice.stop()
        return True

    # ---- speech to text ---------------------------------------------------------------------------
    def stt_start(self, target="astra"):
        from . import stt
        c = stt.cfg()
        if sys.platform == "darwin":     # Mac: its own dictation types straight into the focused message box
            return {"mode": "mac"}
        if c["engine"] == "windows" and sys.platform == "win32":
            threading.Thread(target=stt.windows_voice_typing, daemon=True).start()
            return {"mode": "windows"}
        if not hasattr(self, "_rec"):
            self._rec = stt.Recorder(self._emit)
        return {"mode": "whisper", "started": self._rec.start(target)}

    def stt_stop(self):
        if hasattr(self, "_rec"):
            self._rec.stop()
        return True

    def stt_prepare(self):
        """Settings: download Whisper now instead of on first use."""
        from . import stt

        def work():
            try:
                stt.install(lambda s: self._emit({"type": "notify", "text": s}))
                self._emit({"type": "notify", "text": "Speech recognition is ready"})
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "notify", "text": stt._friendly(e)})
        threading.Thread(target=work, daemon=True).start()
        return True

    def voice_options(self):
        from . import voice
        return {"windows": voice.windows_voices(), "piper": list(voice.PIPER_VOICES), "piper_installed": bool(voice.piper_exe()),
                "labels": voice.VOICE_LABELS, "have": [v for v in voice.PIPER_VOICES if (voice.piper_dir() / f"{v}.onnx").exists()]}

    def voice_install(self, name):
        from . import voice

        def work():
            try:
                voice.install_piper(name, lambda s: self._emit({"type": "notify", "text": s}))
                self._emit({"type": "notify", "text": f"Voice {name} ready"})
                voice.speak("Hi, it's Astra. This is how I sound now.", force=True)
            except Exception as e:  # noqa: BLE001
                self._emit({"type": "notify", "text": f"Voice download failed: {e}"})
        threading.Thread(target=work, daemon=True).start()
        return True

    # ---- your Discord account + Auto-Astra + presence ---------------------------------------------------
    def discord_account_state(self):
        st = services.discord_web.state()
        st["presence_error"] = services.presence.error if services.presence else ""
        return st

    def discord_login(self):
        threading.Thread(target=services.discord_web.login, daemon=True).start()
        return True

    def discord_logout(self):
        services.discord_web.logout()
        return services.discord_web.state()

    def ui_view(self, view):
        if services.presence:
            services.presence.set_view(view)
        return True

    # ---- telegram ----------------------------------------------------------------------------------------------
    def telegram_status(self):
        return services.telegram.status()

    def telegram_pair(self):
        if not services.telegram.token():
            return {"ok": False, "error": "Paste the bot token first."}
        services.config.update("telegram", {"enabled": True})
        try:
            link = services.telegram.pair()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, "bot": services.telegram.bot_username, "link": link}

    def telegram_disconnect(self):
        services.config.update("telegram", {"chat_id": ""})
        return services.telegram.status()

    def telegram_test(self):
        try:
            ok = services.telegram.send("hi, it's me. Telegram works.")
            return {"ok": ok, "error": None if ok else "Not connected yet. Press Connect and tap the link."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---- accounts (university) --------------------------------------------------------------
    def test_university(self):
        from .tools import ToolContext
        from .tools.university import uni_courses
        try:
            out = uni_courses(ToolContext(self._emit))
            return {"ok": True, "text": out[:400]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}

    def games(self):
        from .tools.games import all_games
        return all_games(refresh=True)

    def rebuild_file_index(self):
        from .tools.system import build_index
        threading.Thread(target=build_index, kwargs={"force": True}, daemon=True).start()
        return True

    # ---- misc ---------------------------------------------------------------------------------------------
    def activity(self):
        return services.db.activity()

    def open_workspace(self):
        _open_path(workspace_dir())
        return True

    def open_data_folder(self):
        _open_path(app_dir())
        return True

    def open_url(self, url):
        import webbrowser
        webbrowser.open(url)
        return True

    def open_local(self, path):
        if os.path.exists(path):
            _open_path(path)
            return True
        return False


def _open_path(p):
    if sys.platform == "win32":
        os.startfile(str(p))  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])


def _size_rank(name):
    """Rough model size from its tag (qwen3:14b -> 14), to compare against what fits."""
    import re as _re
    mt = _re.search(r"(\d+(?:\.\d+)?)b\b", name or "", _re.I)
    return float(mt.group(1)) if mt else 0
