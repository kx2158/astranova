# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Tool registry. Each tool is a plain function exposed to the model with a JSON schema.

modes: which agents may use a tool.
  astra  = you, in the app or in your own Discord DMs (full control)
  friend = the Friend companion (light tools + hand-off to Astra)
  guest  = other people talking to the Discord bot (knowledge only; never your PC, files or accounts)
"""
import json
import re
import traceback

REGISTRY = {}

# Tool groups keep the prompt small for local models: "core" is always on, the rest switch on when the task
# needs them (keywords / brief apps) or when the model calls enable_tools. Cloud models get everything.
GROUP_OF = {
    "spotify": "spotify", "apple_music": "apple_music", "browser": "browser", "send_email": "email",
    "read_email": "email", "run_command": "system", "list_files": "system", "read_text_file": "system",
    "write_text_file": "system", "system_info": "system", "schedule_task": "routines", "list_routines": "routines",
    "cancel_routine": "routines", "generate_password": "accounts", "save_account": "accounts",
    "get_account": "accounts", "window_action": "desktop_extra", "drag": "desktop_extra",
    "scroll": "desktop_extra", "take_screenshot": "desktop_extra", "clipboard": "desktop_extra",
    "discord_bot": "discord_bot", "search_files": "system", "disk_overview": "system",
    "list_games": "games", "launch_game": "games",
    "uni_": "university", "culture_news": "culture", "find_events": "culture",
    "inbox_digest": "email", "calendar": "calendar",
}
GROUP_INFO = {
    "spotify": "Spotify search, playback, your playlists and taste",
    "apple_music": "Apple Music app search + playback",
    "browser": "Astra's web browser (sites, logins, forms)",
    "email": "send/read email",
    "system": "files, PowerShell commands, PC info",
    "routines": "schedule reminders and recurring tasks",
    "accounts": "saved website logins, passwords",
    "desktop_extra": "scroll, drag, screenshots, clipboard, minimize/maximize/close windows",
    "discord_bot": "post as the Astra Discord bot",
    "games": "list and launch Steam / Epic games",
    "university": "university courses, materials, deadlines",
    "culture": "pop culture news, releases, fandom chatter, local events",
    "calendar": "the user's calendar (agenda, add events)",
}
KEYWORDS = {
    "spotify": r"spotify|playlist|song|track|album|artist|music|listen|play\b",
    "apple_music": r"apple music|itunes",
    "browser": r"website|browser|\bsite\b|log ?in|sign ?up|form|order|book|\.com|\.net|\.org|youtube|twitch",
    "email": r"e-?mail|inbox|gmail|outlook|\bmail\b|professor",
    "calendar": r"calendar|schedule|agenda|tomorrow|tonight|this week|appointment|lecture|exam|meeting|busy|free\b",
    "system": r"file|folder|command|powershell|install|uninstall|delete|move|rename|cpu|ram|disk|storage|system|"
              r"download|document|script|setting",
    "routines": r"remind|every|schedule|later|tomorrow|tonight|routine|daily|weekly|morning|evening|\bat \d|\bin \d",
    "accounts": r"password|account|credential",
    "desktop_extra": r"drag|scroll|screenshot|clipboard|minimi[sz]e|maximi[sz]e|close",
    "discord_bot": r"\bbot\b|announce",
    "games": r"\bgame|steam|epic|launch|play\b",
    "university": r"\buni\b|university|course|class|moodle|canvas|lecture|assignment|deadline|exam|semester|homework",
    "culture": r"news|release|album|single|tour|drama|stan|fandom|comeback|tea\b|event|concert|festival|happening|gig",
}
APP_GROUPS = {"spotify": ["spotify"], "apple_music": ["apple_music"], "browser": ["browser"], "explorer": ["system"],
              "obs": ["desktop_extra"],
}


def group_of(name):
    for prefix, g in GROUP_OF.items():
        if name == prefix or name.startswith(prefix + "_") or name.startswith(prefix):
            return g
    return "core"


def groups_for_text(text):
    low = (text or "").lower()
    return {g for g, rx in KEYWORDS.items() if re.search(rx, low)}


class ToolContext:
    """Per-run context handed to tools."""

    def __init__(self, emit, page_key="agent", conv_id=None, brief=None, mode="astra", source="app"):
        self.emit = emit
        self.page_key = page_key
        self.conv_id = conv_id
        self.brief = brief or {}
        self.mode = mode
        self.source = source
        self.elements = {}       # desktop element cache from the last read_window: {n: element}
        self.element_window = None
        self.should_stop = lambda: False
        self.groups = None       # active tool groups (None = all)
        self.screen_ok = False   # user allowed mouse/keyboard for this task (background mode)
        self.owner = True        # False when a bot relays someone who isn't the owner
        self.task_id = None      # set when this run is one of Astra's jobs for Nova
        self.handed_task = None  # set by hand_to_astra (Nova)


def tool(name, description, properties=None, required=(), label=None, modes=("astra",), needs=None):
    """needs: optional callable -> str|None. If it returns a string, the tool is hidden and that reason is shown."""
    def deco(fn):
        REGISTRY[name] = {
            "fn": fn,
            "label": label or name.replace("_", " "),
            "modes": tuple(modes),
            "needs": needs,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
                },
            },
        }
        return fn
    return deco


def schemas(mode="astra", groups=None):
    """groups=None -> all tools; otherwise core + the given groups."""
    out = []
    for name, t in REGISTRY.items():
        if mode not in t["modes"]:
            continue
        if groups is not None and mode == "astra" and group_of(name) not in groups and group_of(name) != "core":
            continue
        if t["needs"]:
            try:
                if t["needs"]():
                    continue
            except Exception:
                continue
        out.append(t["schema"])
    return out


def call(name, args, ctx, limit=7000):
    t = REGISTRY.get(name)
    if not t:
        return f"Error: unknown tool '{name}'."
    if ctx.mode not in t["modes"]:
        return f"Blocked: {name} is not available here."
    if t["needs"]:
        try:
            why = t["needs"]()
        except Exception as e:  # noqa: BLE001
            why = str(e)
        if why:
            return f"Blocked: {why}"
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except Exception:
            args = {}
    try:
        res = t["fn"](ctx, **(args or {}))
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        msg = str(e).split("\nCall log:")[0].split("\nBrowser logs:")[0]
        return f"Error while running {name}: {type(e).__name__}: {msg[:600]}"
    if not isinstance(res, str):
        res = json.dumps(res, ensure_ascii=False, indent=1, default=str)
    if len(res) > limit:
        res = res[:limit] + f"\n...[truncated, {len(res) - limit} more characters]"
    return res


def load_all():
    from . import (agenda, browser, culture, desktop, discord_bot, discord_web, games, mail,  # noqa: F401
                   messaging, meta, music, personal, research, system, telegram_bot, university, web)
    from ..paths import is_public
    _mark_windows_only()


# Tools that drive Windows itself (app windows, the Windows game launchers, drive letters, media keys). On a Mac
# they're hidden, so the model never tries them; everything else (chat, calendar, mail, web, browser, Spotify,
# files, commands, bots) works the same.
WINDOWS_ONLY_MODULES = {"astra.tools.desktop", "astra.tools.games"}
WINDOWS_ONLY_TOOLS = {"media_key", "search_files", "disk_overview", "apple_music_search"}


def _mark_windows_only():
    import sys
    if sys.platform == "win32":
        return
    for name, t in REGISTRY.items():
        if t["fn"].__module__ in WINDOWS_ONLY_MODULES or name in WINDOWS_ONLY_TOOLS:
            t["needs"] = lambda: "only available on Windows"
