"""Send and read messages as YOU in desktop chat apps (Discord, Slack, WhatsApp, Telegram...).

This drives the real apps on your PC (no self-bots, no API tokens): open the chat with the app's own quick
switcher, check the right chat opened, ask you to approve the exact text, then paste it and press Enter.
"""
import re
import time

from .. import services
from ..text import no_dashes
from . import desktop, tool
from .desktop import control_off

RECIPES = {
    "discord": {"label": "Discord", "app": "discord", "open": ["^k"], "after_type": ["{ENTER}"], "wait": 1.0,
                "box": ("message @", "message #", "message "), "title_check": True},
    "slack": {"label": "Slack", "app": "slack", "open": ["^k"], "after_type": ["{ENTER}"], "wait": 1.2,
              "box": ("message ", "write a message"), "title_check": False},
    "whatsapp": {"label": "WhatsApp", "app": "whatsapp", "open": ["^f"], "after_type": ["{DOWN}", "{ENTER}"],
                 "wait": 1.5, "box": ("type a message", "message"), "title_check": False},
    "telegram": {"label": "Telegram", "app": "telegram", "open": ["{ESC}", "{ESC}"], "after_type": ["{ENTER}"],
                 "wait": 1.2, "box": ("write a message", "message"), "title_check": False},
}
ALIASES = {"discord": "discord", "slack": "slack", "whatsapp": "whatsapp", "whats app": "whatsapp",
           "telegram": "telegram", "telegram desktop": "telegram"}


def _recipe(app):
    return RECIPES.get(ALIASES.get((app or "").lower().strip(), ""))


def _tokens(s):
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 2]


def _ensure_app(ctx, r):
    w = desktop.find_window(r["app"])
    if not w:
        desktop.launch(r["label"])
        for _ in range(40):
            desktop._check_stop(ctx)
            time.sleep(0.5)
            w = desktop.find_window(r["app"])
            if w:
                time.sleep(2.5 if r["app"] == "discord" else 1.5)  # let it finish loading
                break
    if not w:
        raise RuntimeError(f"{r['label']} didn't open.")
    desktop.activate(w)
    return w


def _open_chat(ctx, r, to):
    w = _ensure_app(ctx, r)
    for k in r["open"]:
        desktop.send_keys(k)
        time.sleep(0.25)
    desktop.paste_text(to)
    time.sleep(r["wait"])
    for k in r["after_type"]:
        desktop.send_keys(k)
        time.sleep(0.3)
    time.sleep(1.0)
    w = desktop.find_window(r["app"]) or w
    return w


def _chat_matches(title, to):
    want = [t for t in _tokens(to) if t not in ("dm", "chat")]
    have = set(_tokens(title))
    return bool(want) and all(any(t in h or h in t for h in have if len(h) >= 2) for t in want)


def _focus_box(w, hints, click=True):
    """Find (and click) the message input if UI Automation can see it. Returns its accessible name."""
    try:
        import time as _t
        root = desktop.wrapper(w["hwnd"])
        for el, ctype, name, _r in desktop.walk(root, _t.time() + 3.0):
            if ctype in ("Edit", "Document") and any(h in name.lower() for h in hints):
                if click:
                    el.click_input()
                return name
    except Exception:
        pass
    return None


def _recent_text(w, limit):
    root = desktop.wrapper(w["hwnd"])
    seen, lines = set(), []
    for _el, ctype, name, _r in desktop.walk(root, time.time() + 5.0, max_nodes=4000):
        if ctype in ("ListItem", "Text", "Group", "DataItem") and name and len(name) > 1:
            key = name.strip()
            if key not in seen:
                seen.add(key)
                lines.append(key[:500])
    return lines[-limit:]


@tool("send_message", "Send a message AS THE USER in a desktop chat app: discord, slack, whatsapp or telegram. "
      "'to' is the person, DM, server channel ('#general') or group exactly as it appears in the app. Opens the "
      "chat, verifies it, asks the user to approve, then sends. For other apps use the desktop tools and "
      "request_approval before pressing Enter.",
      {"app": {"type": "string"}, "to": {"type": "string"}, "text": {"type": "string"},
       "confirmed_chat": {"type": "boolean", "description": "true only after the user confirmed an unclear chat match"}},
      ["app", "to", "text"], label="Sending message", needs=control_off)
def send_message(ctx, app, to, text, confirmed_chat=False):
    text = no_dashes(text)
    r = _recipe(app)
    web = services.discord_web
    if r and r["app"] == "discord" and web and web.ready():
        return _send_discord_web(ctx, to, text, confirmed_chat)
    if not r:
        return (f"No built-in recipe for '{app}'. Do it with the desktop tools: open_app, read_window, open the chat, "
                f"type_into the message box WITHOUT pressing Enter, then request_approval with the exact text, then "
                f"press_keys('enter').")
    desktop.need_screen(ctx, f"Open {r['label']} and send a message to {to}")
    w = _open_chat(ctx, r, to)
    title = w["title"] if w else ""
    box_name = _focus_box(w, r["box"], click=False) if w else None
    matched = _chat_matches(title, to) or bool(box_name and _chat_matches(box_name, to))
    if r["title_check"] and not confirmed_chat and not matched:
        desktop.send_keys("{ESC}")
        seen = f"window '{title}'" + (f", message box '{box_name}'" if box_name else "")
        return (f"NOT SENT. After searching for '{to}' {r['label']} shows {seen}, which doesn't clearly match. "
                f"Check the exact name (read_window may help) or ask the user, then retry with confirmed_chat=true "
                f"if that chat is right.")
    text = text.strip()
    if services.config.get("control", "confirm_messages"):
        where = f"{r['label']} -> {to}" + (f"  (open: {box_name or title})" if (box_name or title) else "")
        if not services.bridge.confirm(f"Send this message on {r['label']}?", f"To: {where}\n\n{text}"):
            return "DECLINED by the user - not sent. Ask what to change."
        desktop._check_stop(ctx)
    w = desktop.find_window(r["app"]) or w
    desktop.activate(w)
    time.sleep(0.2)
    box = _focus_box(w, r["box"])
    if box_name and box and box != box_name:
        return f"NOT SENT. The chat changed while waiting (now '{box}', expected '{box_name}'). Retry."
    lines = text.split("\n")
    for i, line in enumerate(lines):  # Shift+Enter between lines so multi-line text stays one message
        if line:
            desktop.paste_text(line)
        if i < len(lines) - 1:
            desktop.send_keys("+{ENTER}")
    time.sleep(0.2)
    desktop.send_keys("{ENTER}")
    time.sleep(0.6)
    services.db.log("message", f"{r['label']} -> {to}: {text[:300]}")
    return f"Sent on {r['label']} to {to}" + (f" (chat: {title})" if title else "") + "."


def _send_discord_web(ctx, to, text, confirmed_chat):
    """Discord through Astra's hidden logged-in session: no screen, no focus stealing."""
    web = services.discord_web
    info = web.open_chat(to)
    seen = info.get("box") or info.get("title") or ""
    if not confirmed_chat and not (_chat_matches(seen, to) or _chat_matches(info.get("title", ""), to)):
        return (f"NOT SENT. Searching '{to}' opened '{seen}', which doesn't clearly match. Check the exact name or ask "
                f"the user, then retry with confirmed_chat=true if it's right.")
    if services.config.get("control", "confirm_messages"):
        if not services.bridge.confirm("Send this message on Discord?", f"To: {to}  (open: {seen})\n\n{text}"):
            return "DECLINED by the user - not sent. Ask what to change."
    web.send(text)
    services.db.log("message", f"Discord (background) -> {to}: {text[:300]}")
    return f"Sent on Discord to {to} (in the background)."


@tool("read_messages", "Read the latest visible messages of a chat in discord, slack, whatsapp or telegram "
      "(omit chat to read whatever chat is open).",
      {"app": {"type": "string"}, "chat": {"type": "string"}, "limit": {"type": "integer"}}, ["app"],
      label="Reading messages", needs=control_off)
def read_messages(ctx, app, chat="", limit=30):
    r = _recipe(app)
    web = services.discord_web
    if r and r["app"] == "discord" and web and web.ready() and chat:
        from .discord_web import discord_read_dm
        return discord_read_dm(ctx, chat, limit)
    if not r:
        return f"No recipe for '{app}' - use read_window or look_at_screen on that app."
    desktop.need_screen(ctx, f"Open {r['label']} to read messages")
    w = _open_chat(ctx, r, chat) if chat else _ensure_app(ctx, r)
    lines = _recent_text(w, int(limit or 30))
    if len(lines) < 4:
        return desktop.look_at_screen(ctx, "Transcribe the visible chat messages in order, as 'Sender: message'.",
                                      r["app"])
    return f'CHAT WINDOW: "{w["title"]}"\n' + "\n".join(lines)
