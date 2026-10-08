# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Telegram bot. Your friend by default; /menu turns it into a remote control for Astra on your PC.

Connect: Settings > Telegram > paste the @BotFather token > Connect > tap the link (press Start).
Only your paired chat, sent from the Telegram username in settings , can use it.
Commands: /menu, /friend, /astra, /play <game>, /games, /status, /stop
"""
import secrets
import threading
import re
import time

import requests

from .. import services
from ..bridge import YES
from ..text import no_dashes

API_BASE = "https://api.telegram.org"


class TelegramService:
    def __init__(self):
        self.thread = None
        self.offset = None
        self.pair_code = None
        self.pairing_until = 0
        self.bot_username = ""
        self.last_error = ""
        self.last_poll = 0
        self.mode = "friend"          # friend | astra
        self.busy = False
        self.games_page = []
        self._token_seen = None
        # set by app
        self.on_message = None        # fn(text, mode) -> reply
        self.on_game = None           # fn(name) -> reply
        self.on_stop = None
        self.on_status = None         # fn() -> text

    def token(self):
        return (services.config.get_secret("telegram_token") or "").strip()

    def chat_id(self):
        return str(services.config.get("telegram", "chat_id") or "")

    def ready(self):
        return bool(self.token() and self.chat_id())

    def api(self, method, http_timeout=20, **params):
        try:
            r = requests.post(f"{API_BASE}/bot{self.token()}/{method}", json=params, timeout=http_timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"cannot reach Telegram ({type(e).__name__})") from e
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not d.get("ok"):
            desc = d.get("description", f"HTTP {r.status_code}")
            if r.status_code == 401:
                desc = "the bot token is wrong. Copy it again from @BotFather."
            elif r.status_code == 409:
                desc = "another program is reading this bot's messages. Close it or use a different bot."
            raise RuntimeError(desc)
        return d["result"]

    def status(self):
        return {"has_token": bool(self.token()), "bot": self.bot_username, "connected": bool(self.chat_id()),
                "listening": bool(self.thread and self.thread.is_alive() and time.time() - self.last_poll < 90),
                "pairing": time.time() < self.pairing_until, "link": self.pair_link(), "error": self.last_error,
                "mode": self.mode}

    def pair_link(self):
        if self.bot_username and self.pair_code and time.time() < self.pairing_until:
            return f"https://t.me/{self.bot_username}?start={self.pair_code}"
        return ""

    def _set_error(self, msg):
        if msg != self.last_error:
            self.last_error = msg
            services.emit({"type": "telegram_status", **self.status()})

    # ---- sending ----------------------------------------------------------------------------------
    def send(self, text, buttons=None):
        if not self.ready():
            return False
        text = no_dashes(text or "(empty)")
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
        for i, chunk in enumerate(chunks):
            params = {"chat_id": self.chat_id(), "text": chunk, "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = {"inline_keyboard": buttons}
            self.api("sendMessage", **params)
        return True

    def send_texts(self, text):
        """Nova's reply as separate texts with a little typing pause in between, exactly like in the app."""
        parts = [p.strip() for p in re.split(r"\n\s*\n", no_dashes(text or "")) if p.strip()]
        if len(parts) == 1 and "\n" in parts[0]:
            lines = [l.strip() for l in parts[0].split("\n") if l.strip()]
            if all(len(l) < 220 for l in lines) and not any(l.startswith(("-", "*", "•")) or l[:2].rstrip(".").isdigit()
                                                          for l in lines):
                parts = lines            # short texts each on their own line, not a list
        for i, part in enumerate(parts[:12]):
            if i:
                try:
                    self.api("sendChatAction", chat_id=self.chat_id(), action="typing")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(min(1.4, 0.35 + len(part) * 0.018))
            self.send(part)
        if len(parts) > 12:
            self.send("\n".join(parts[12:]))
        return True

    def send_request(self, p):
        if p.kind == "confirm":
            done = p.title.startswith("Your turn")
            self.send(f"{'Action needed' if done else 'Approve?'} {p.title}\n\n{p.details}"[:3900],
                      buttons=[[{"text": "Done" if done else "Approve", "callback_data": f"y:{p.id}"},
                                {"text": "Cancel" if done else "Decline", "callback_data": f"n:{p.id}"}]])
        else:
            self.send(f"Question: {p.title}\n{p.details}\n\nJust reply here.".strip())

    def menu(self):
        fname = services.config.get("friend", "name") or "Nova"
        on = "✓ " 
        self.send(f"menu. right now you're talking to {'Astra (PC tasks)' if self.mode == 'astra' else fname}.", buttons=[
            [{"text": (on if self.mode == "friend" else "") + f"{fname} (friend)", "callback_data": "m:friend"},
             {"text": (on if self.mode == "astra" else "") + "Astra (PC tasks)", "callback_data": "m:astra"}],
            [{"text": "Games", "callback_data": "m:games"}, {"text": "Status", "callback_data": "m:status"}],
            [{"text": "Stop everything", "callback_data": "m:stop"}]])

    def games_menu(self):
        from .games import all_games
        g = all_games(refresh=True)[:40]
        self.games_page = g
        if not g:
            self.send("no installed Steam or Epic games found.")
            return
        rows, row = [], []
        for i, game in enumerate(g):
            row.append({"text": game["name"][:28], "callback_data": f"g:{i}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        self.send("which game?", buttons=rows)

    # ---- pairing -------------------------------------------------------------------------------------
    def check_token(self):
        me = self.api("getMe", http_timeout=15)
        self.bot_username = me.get("username", "")
        return me

    def pair(self, seconds=600):
        self.check_token()
        self.pair_code = secrets.token_hex(4)
        self.pairing_until = time.time() + seconds
        self.start()
        return self.pair_link()

    # ---- receiving -------------------------------------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._poll, daemon=True, name="telegram")
        self.thread.start()

    def _poll(self):
        backoff = 3
        while True:
            tok = self.token()
            if not tok or not services.config.get("telegram", "enabled"):
                time.sleep(3)
                continue
            if tok != self._token_seen:
                self._token_seen, self.offset = tok, None
                try:
                    self.check_token()
                    self.api("deleteWebhook", http_timeout=15, drop_pending_updates=False)
                    self.api("setMyCommands", commands=[
                        {"command": "menu", "description": "switch modes, games, status"},
                        {"command": "friend", "description": "talk to your friend"},
                        {"command": "astra", "description": "give Astra PC tasks"},
                        {"command": "play", "description": "launch a game: /play bg3"},
                        {"command": "stop", "description": "stop everything"}])
                except Exception as e:  # noqa: BLE001
                    self._set_error(str(e))
                    self._token_seen = None
                    time.sleep(10)
                    continue
            try:
                params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if self.offset is not None:
                    params["offset"] = self.offset
                updates = self.api("getUpdates", http_timeout=40, **params)
                self.last_poll = time.time()
                if self.last_error:
                    self._set_error("")
                backoff = 3
            except Exception as e:  # noqa: BLE001
                self._set_error(str(e))
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                try:
                    self._handle(u)
                except Exception as e:  # noqa: BLE001
                    services.db.log("telegram", f"handler error: {e}")

    def _is_owner(self, frm, chat):
        want = (services.config.get("telegram", "owner_username") or "").lower().lstrip("@")
        return chat == self.chat_id() and (not want or (frm.get("username") or "").lower() == want)

    def _handle(self, u):
        if "callback_query" in u:
            cq = u["callback_query"]
            chat = str(cq["message"]["chat"]["id"])
            if not self._is_owner(cq.get("from", {}), chat):
                return
            kind, val = cq["data"].split(":", 1)
            self.api("answerCallbackQuery", callback_query_id=cq["id"])
            if kind in ("y", "n"):
                services.bridge.resolve(val, kind == "y")
            elif kind == "m":
                if val in ("friend", "astra"):
                    self.mode = val
                    self.send("ok, talking to " + ("Astra. PC tasks work now, /friend to switch back."
                                                   if val == "astra" else (services.config.get("friend", "name") or "Nova") + " again."))
                elif val == "games":
                    self.games_menu()
                elif val == "status":
                    self.send(self.on_status() if self.on_status else "ok")
                elif val == "stop" and self.on_stop:
                    self.on_stop()
                    self.send("stopped everything.")
            elif kind == "g" and self.on_game:
                i = int(val)
                if 0 <= i < len(self.games_page):
                    self.send(self.on_game(self.games_page[i]["name"]))
            return

        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))
        frm = msg.get("from", {})
        if not text or not chat:
            return
        if text.startswith("/start"):
            arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
            in_window = time.time() < self.pairing_until
            want = (services.config.get("telegram", "owner_username") or "").lower().lstrip("@")
            if in_window and (not arg or arg == self.pair_code):
                if want and (frm.get("username") or "").lower() != want:
                    self._reply(chat, f"this bot only pairs with @{want}.")
                    return
                services.config.update("telegram", {"chat_id": chat})
                self.pairing_until, self.pair_code = 0, None
                self.send(f"connected. it's {services.config.get('friend', 'name') or 'Nova'} here, just talk to me. "
                          "/menu switches to Astra for PC stuff, launches games and more.")
                services.emit({"type": "telegram_status", **self.status()})
                return
            self._reply(chat, "this bot is private.")
            return
        if not self._is_owner(frm, chat):
            self._reply(chat, "this bot is private.")
            return

        low = text.lower()
        q = services.bridge.oldest_open()
        if q and not low.startswith("/"):
            services.bridge.resolve(q.id, text if q.kind == "ask" else (low.strip(" .!") in YES))
            self.send("got it.")
            return
        if low in ("/menu", "menu"):
            return self.menu()
        if low.startswith("/friend"):
            self.mode = "friend"
            text = text[7:].strip()
            if not text:
                return self.send("ok, it's just us again.")
        elif low.startswith("/astra"):
            self.mode = "astra"
            text = text[6:].strip()
            if not text:
                return self.send("Astra here. what should I do on your PC?")
        if low in ("/stop", "stop"):
            if self.on_stop:
                self.on_stop()
            return self.send("stopped everything.")
        if low in ("/games", "/play"):
            return self.games_menu()
        if low.startswith("/play ") and self.on_game:
            return self.send(self.on_game(text[6:].strip()))
        if low == "/status":
            return self.send(self.on_status() if self.on_status else "ok")
        if self.busy:
            return self.send("still on your last message. /stop cancels.")
        if self.on_message:
            self.api("sendChatAction", chat_id=chat, action="typing")
            threading.Thread(target=self._run, args=(text, self.mode), daemon=True).start()

    def _reply(self, chat, text):
        try:
            self.api("sendMessage", chat_id=chat, text=text)
        except Exception:  # noqa: BLE001
            pass

    def _run(self, text, mode):
        self.busy = True
        try:
            reply = self.on_message(text, mode)
            if mode == "astra":
                self.send(reply or "done.")
            else:
                self.send_texts(reply or "ok")
        except Exception as e:  # noqa: BLE001
            self.send(f"error: {e}")
        finally:
            self.busy = False
