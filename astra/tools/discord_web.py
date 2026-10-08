"""Your own Discord account inside Astra.

You log in once in a visible window (Settings > Discord account). After that Astra keeps Discord web open in a
hidden browser with its own profile, so it can read and send DMs in the background without touching your
screen or the Discord app.

Auto-Astra: while it's on, Astra watches your DMs and answers the people you allowed, in your style.

Note: Discord's terms don't allow automating user accounts. This is opt-in and low-volume (it behaves like a
person typing), but there is a risk of account action. You decide.
"""
import concurrent.futures
import json
import queue
import re
import threading
import time

from .. import services
from ..paths import app_dir
from ..text import no_dashes, redact
from . import tool

LOGIN_URL = "https://discord.com/login"
HOME_URL = "https://discord.com/channels/@me"

READ_JS = r"""
(limit) => {
  const items = [...document.querySelectorAll('li[id^="chat-messages-"]')];
  const out = []; let author = '';
  for (const li of items) {
    const h = li.querySelector('h3 [class*="username"]');
    if (h) author = h.textContent.trim();
    const tm = li.querySelector('time');
    const c = li.querySelector('[id^="message-content-"]');
    const text = c ? c.innerText.trim() : '';
    const attach = li.querySelector('[class*="imageWrapper"], [class*="attachment"]') ? ' [attachment]' : '';
    if (text || attach) out.push({ id: li.id, author, text: text + attach, at: tm ? Date.parse(tm.getAttribute('datetime')) / 1000 : 0 });
  }
  return out.slice(-limit);
}
"""
UNREAD_JS = r"""
() => {
  const nav = document.querySelector('nav[aria-label*="Servers"], [data-list-id="guildsnav"]') || document;
  const hrefs = new Set();
  nav.querySelectorAll('a[href^="/channels/@me/"]').forEach(a => hrefs.add(a.getAttribute('href')));
  return [...hrefs];
}
"""
DMS_JS = r"""
() => [...document.querySelectorAll('a[href^="/channels/@me/"]')].map(a => ({ href: a.getAttribute('href'),
  name: (a.getAttribute('aria-label') || a.innerText || '').split('\n')[0].replace(/\s*\(direct message\)\s*/i, '').trim() }))
"""
ME_JS = r"""
() => {
  const area = document.querySelector('section[aria-label="User area"], [class*="panels"]');
  if (!area) return '';
  const el = area.querySelector('[class*="panelTitleContainer"], [class*="nameTag"] [class*="title"], [class*="avatarWrapper"] + div');
  return el ? el.innerText.split('\n')[0].trim() : '';
}
"""


class DiscordWeb:
    def __init__(self):
        self.q = queue.Queue()
        self.thread = None
        self.pw = None
        self.ctx = None
        self.page = None
        self.headless = None
        self.me = ""
        self.status = "not connected"
        self.handled = {}           # dm href -> last message id answered/seen
        self.auto_thread = None
        self.auto_log = []

    # ---- browser thread -------------------------------------------------------------------------------
    def _loop(self):
        while True:
            fn, fut = self.q.get()
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except Exception as e:  # noqa: BLE001
                    fut.set_exception(e)

    def call(self, fn, timeout=120):
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="discord-web")
            self.thread.start()
        fut = concurrent.futures.Future()
        self.q.put((fn, fut))
        return fut.result(timeout)

    def _open(self, headless):
        if self.ctx and self.headless == headless:
            try:
                _ = self.ctx.pages
                return
            except Exception:  # noqa: BLE001
                self.ctx = None
        if self.ctx:
            try:
                self.ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if not self.pw:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
        prof = app_dir() / "discord-profile"
        prof.mkdir(exist_ok=True)
        kw = dict(user_data_dir=str(prof), headless=headless, viewport={"width": 1280, "height": 860},
                  args=["--disable-blink-features=AutomationControlled"])
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(channel="msedge", **kw)
        except Exception:  # noqa: BLE001
            self.ctx = self.pw.chromium.launch_persistent_context(**kw)
        self.ctx.set_default_timeout(20000)
        self.headless = headless
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def _ready_page(self):
        self._open(True)
        if "/channels/" not in (self.page.url or ""):
            self.page.goto(HOME_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(2500)
        if "/login" in self.page.url:
            services.config.update("discord_account", {"connected": False})
            raise RuntimeError("Discord account is logged out. Settings > Discord account > Log in.")
        return self.page

    # ---- login ----------------------------------------------------------------------------------------------
    def login(self, timeout=600):
        def go():
            self._open(False)
            self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
            self.page.bring_to_front()
            end = time.time() + timeout
            while time.time() < end:
                if "/channels/" in (self.page.url or ""):
                    self.page.wait_for_timeout(3000)
                    self.me = self.page.evaluate(ME_JS) or ""
                    return True
                self.page.wait_for_timeout(1000)
            return False
        self.status = "waiting for login"
        services.emit({"type": "discord_account", **self.state()})
        ok = self.call(go, timeout=timeout + 30)
        services.config.update("discord_account", {"connected": bool(ok), "me": self.me or
                                                   services.config.get("discord_account", "me") or ""})
        self.status = "connected" if ok else "login timed out"
        if ok:
            self.call(lambda: self._open(True))  # continue hidden
        services.emit({"type": "discord_account", **self.state()})
        return ok

    def logout(self):
        def go():
            if self.ctx:
                self.ctx.clear_cookies()
                self.ctx.close()
            self.ctx = None
        self.call(go)
        services.config.update("discord_account", {"connected": False})
        self.status = "not connected"

    def state(self):
        c = services.config.get("discord_account")
        return {"connected": bool(c.get("connected")), "status": self.status, "me": c.get("me") or self.me,
                "auto": bool(services.config.get("auto_astra", "enabled")), "log": self.auto_log[-30:]}

    def ready(self):
        return bool(services.config.get("discord_account", "connected"))

    # ---- actions (background, no screen) ---------------------------------------------------------------
    def open_chat(self, to):
        def go():
            p = self._ready_page()
            p.keyboard.press("Control+K")
            p.wait_for_timeout(500)
            p.keyboard.insert_text(to)
            p.wait_for_timeout(1300)
            p.keyboard.press("Enter")
            p.wait_for_timeout(1800)
            box = p.query_selector('[role="textbox"][data-slate-editor="true"]')
            label = box.get_attribute("aria-label") if box else ""
            return {"title": p.title(), "box": label or "", "url": p.url}
        return self.call(go)

    def send(self, text):
        def go():
            p = self._ready_page()
            box = p.wait_for_selector('[role="textbox"][data-slate-editor="true"]', timeout=10000)
            box.click()
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if line:
                    p.keyboard.insert_text(line)
                if i < len(lines) - 1:
                    p.keyboard.press("Shift+Enter")
            p.wait_for_timeout(250)
            p.keyboard.press("Enter")
            p.wait_for_timeout(900)
            return True
        return self.call(go)

    def read(self, limit=30):
        def go():
            p = self._ready_page()
            p.wait_for_timeout(600)
            return {"title": p.title(), "messages": p.evaluate(READ_JS, limit)}
        return self.call(go)

    def goto(self, href):
        def go():
            p = self._ready_page()
            p.goto("https://discord.com" + href, wait_until="domcontentloaded")
            p.wait_for_selector('li[id^="chat-messages-"]', timeout=15000)
            p.wait_for_timeout(800)
            return {"title": p.title(), "messages": p.evaluate(READ_JS, 25)}
        return self.call(go)

    def dm_list(self):
        def go():
            p = self._ready_page()
            if not p.url.rstrip("/").endswith("@me"):
                p.goto(HOME_URL, wait_until="domcontentloaded")
                p.wait_for_timeout(2000)
            if not self.me:
                self.me = p.evaluate(ME_JS) or ""
            return {"unread": p.evaluate(UNREAD_JS), "dms": p.evaluate(DMS_JS)}
        return self.call(go)

    # ---- Auto-Astra -------------------------------------------------------------------------------------
    def start_auto(self):
        if self.auto_thread and self.auto_thread.is_alive():
            return
        self.auto_thread = threading.Thread(target=self._auto_loop, daemon=True, name="auto-astra")
        self.auto_thread.start()

    def _log(self, text):
        self.auto_log.append({"t": time.time(), "text": text})
        self.auto_log = self.auto_log[-200:]
        services.db.log("auto-astra", text)
        services.emit({"type": "discord_account", **self.state()})

    def _auto_loop(self):
        while True:
            cfg = services.config.get("auto_astra")
            if not cfg.get("enabled"):
                time.sleep(5)
                continue
            if not self.ready():
                time.sleep(20)
                continue
            try:
                self._auto_tick(cfg)
            except Exception as e:  # noqa: BLE001
                self._log(f"error: {str(e)[:200]}")
                time.sleep(30)
            time.sleep(max(20, int(cfg.get("check_seconds") or 45)))

    def _allowed(self, name, cfg):
        if cfg.get("who") == "everyone":
            return True
        allow = [a.strip().lower().lstrip("@") for a in (cfg.get("allow") or "").split(",") if a.strip()]
        n = (name or "").lower()
        return any(a and (a == n or a in n) for a in allow)

    def _auto_tick(self, cfg):
        me = (services.config.get("discord_account", "me") or self.me or "").strip()
        info = self.dm_list()
        names = {d["href"]: d["name"] for d in info["dms"]}
        targets = list(info["unread"])
        if cfg.get("who") != "everyone":   # also check allow-listed chats directly
            targets += [h for h, n in names.items() if self._allowed(n, cfg) and h not in targets]
        for href in targets[:8]:
            name = names.get(href, "")
            if name and not self._allowed(name, cfg):
                continue
            chat = self.goto(href)
            msgs = chat["messages"]
            if not msgs:
                continue
            partner = name or re.sub(r"^.*?@|\s*[|-].*$", "", chat["title"]).strip()
            if not self._allowed(partner, cfg):
                continue
            last = msgs[-1]
            if self.handled.get(href) == last["id"]:
                continue
            self.handled[href] = last["id"]
            author = (last.get("author") or "").lower()
            if me:
                their_turn = author != me.lower()
            elif partner:
                their_turn = author == partner.lower()
            else:
                their_turn = False
            fresh = last.get("at") and time.time() - last["at"] < 1800
            if not their_turn or not fresh:
                continue
            reply = self._compose(partner or last["author"], msgs, me, cfg)
            if not reply:
                continue
            text = reply["reply"]
            self.send(text)
            self._log(f"Replied to {partner}: {text[:160]}")
            services.emit({"type": "notify", "text": f"Auto-Astra answered {partner}"})
            if reply.get("needs_user"):
                note = f"Auto-Astra: {partner} needs you. {reply.get('summary', '')}"
                services.emit({"type": "notify", "text": note})
                if services.telegram and services.telegram.ready():
                    try:
                        services.telegram.send(note)
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(4)

    def _compose(self, partner, msgs, me, cfg):
        from ..agent import AUTO_PROMPT, STYLE_RULE, _now
        p = services.config.get("profile")
        name = p.get("name") or me or "the user"
        mine = [m["text"] for m in msgs if me and m["author"].lower() == me.lower()][-8:]
        style = cfg.get("style_notes") or "casual"
        if mine:
            style += ". Examples of how they write: " + " | ".join(mine)
        disclose = ("End the message with ' (astra)' so they know it's the assistant." if cfg.get("disclose") else "")
        system = AUTO_PROMPT.format(name=name, author=partner, app="Discord", today=_now(), style=style,
                                    about=p.get("about") or "", disclose=disclose, rule=STYLE_RULE)
        transcript = "\n".join(f"{m['author'] or '?'}: {m['text']}" for m in msgs[-20:])
        schema = {"type": "object", "properties": {"reply": {"type": "string"}, "needs_user": {"type": "boolean"},
                                                   "summary": {"type": "string"}}, "required": ["reply", "needs_user"]}
        r = services.llm.json_task(system, "CONVERSATION (newest last):\n" + transcript, schema)
        text = redact(no_dashes((r.get("reply") or "").strip()))
        if not text:
            return None
        if cfg.get("disclose") and "(astra)" not in text.lower():
            text += " (astra)"
        r["reply"] = text
        return r


def account_missing():
    d = services.discord_web
    if not (d and d.ready()):
        return "Discord account not connected (Settings > Discord account)."
    return None


@tool("discord_read_dm", "Read the latest messages with a person/channel on the user's own Discord account, in the "
      "background (no screen).", {"chat": {"type": "string"}, "limit": {"type": "integer"}}, ["chat"],
      label="Reading Discord", needs=account_missing)
def discord_read_dm(ctx, chat, limit=30):
    info = services.discord_web.open_chat(chat)
    r = services.discord_web.read(int(limit or 30))
    lines = [f"{m['author']}: {m['text']}" for m in r["messages"]]
    return f"CHAT: {info.get('box') or r['title']}\n" + ("\n".join(lines) or "(no messages)")
