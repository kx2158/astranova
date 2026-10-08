# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Process-wide singletons, created once at startup."""
import threading

from .bridge import Bridge
from .config import Config
from .db import DB
from .llm import LLM

config = Config()
db = DB()
llm = LLM(config)
bridge = Bridge()

# Filled in by app._boot(): BrowserService, DiscordService, Scheduler, emit function
browser = None
discord = None          # Discord bot (friend mode)
discord_web = None      # your own Discord account (background session, Auto-Astra)
telegram = None         # Telegram bot
presence = None         # Discord Rich Presence
companion = None        # Nova texting first
phone = None            # iPhone link
scheduler = None
emit = lambda evt: None  # noqa: E731

# Set by the Ctrl+Alt+X hotkey or the Stop button: every running task checks it between steps.
panic = threading.Event()
stop_all = lambda: None  # noqa: E731  (set by app)
