# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""App playbooks: short, practical know-how for operating specific apps. Only the playbooks for apps the
current task touches are pinned into the prompt. Users can override any of them in Settings > Apps."""
from . import services

DEFAULTS = {
    "discord": """DISCORD (desktop app, process Discord.exe)
- Prefer send_message(app="discord", to=..., text=...). It opens the chat with the Ctrl+K quick switcher, checks the
  window title shows the right chat, asks the user to approve, then sends.
- Quick switcher prefixes: "@name" = DM/user, "#name" = text channel, "*name" = server, "!name" = voice channel.
- To read a chat: read_messages(app="discord", chat=...). Esc closes popups. Ctrl+Alt+Up/Down = previous/next server.
- Shift+Enter = new line inside a message. Never ping @everyone unless asked.""",

    "spotify": """SPOTIFY
- Use the spotify_* tools (Web API) for search, playlists and playback. They need Settings > Apps > Spotify connected.
- Playback control through the API needs Spotify Premium. Without it use media_key (play_pause / next / previous).""",

    "apple_music": """APPLE MUSIC (Windows app, process AppleMusic.exe)
- No public desktop API: operate the app with the desktop tools. open_app("Apple Music"), then read_window.
- Search: click the search field in the sidebar (or the element named "Search"), type, press Enter, then
  read_window again to see results.
- If elements are missing from read_window, use find_on_screen / look_at_screen and click_at.
- Playback: media_key works for play/pause/next.""",
    "university": """UNIVERSITY
- uni_courses, uni_course(course), uni_deadlines. With the 'browser' provider these open the portal in Astra's
  browser: then read it with browser_view/browser_click. Single sign-on/2FA: browser_wait_for_user.""",
    "whatsapp": """WHATSAPP (desktop app)
- send_message(app="whatsapp", to=..., text=...) searches the chat with Ctrl+F and opens the first match.
- Verify the opened chat with read_window before sending if the name is ambiguous.""",

    "telegram": """TELEGRAM DESKTOP
- send_message(app="telegram", to=..., text=...): Esc, type the name into search, Enter opens the top result.""",

    "slack": """SLACK
- send_message(app="slack", to=..., text=...) uses Ctrl+K (jump to). "#channel" or a person's name.""",

    "browser": """WEB BROWSER
- For websites use browser_* tools (Astra's own browser with saved logins), not the desktop tools.
- For quick facts use web_search + read_url.""",

    "explorer": """FILES
- list_files / read_text_file / write_text_file work in your user folders. Open anything with open_path.
- Astra's own output goes to Documents/Astra.""",


    "obs": """OBS STUDIO
- Operate with desktop tools. Scenes/sources are listed in the docks; read_window finds them.
- Hotkeys only work if the user assigned them in OBS Settings > Hotkeys.""",
}

ALIASES = {
    "discord": "discord", "spotify": "spotify", "apple music": "apple_music", "applemusic": "apple_music",
    "itunes": "apple_music", "whatsapp": "whatsapp", "telegram": "telegram", "slack": "slack",
    "chrome": "browser", "edge": "browser", "firefox": "browser", "browser": "browser", "explorer": "explorer",
    "files": "explorer", "obs": "obs",
    "university": "university", "moodle": "university",
    "canvas": "university", "course": "university", }

LABELS = {"discord": "Discord", "spotify": "Spotify", "apple_music": "Apple Music", "whatsapp": "WhatsApp", "telegram": "Telegram",
          "slack": "Slack", "browser": "Web browser", "explorer": "Files", "obs": "OBS Studio",
          "university": "University"}
HIDDEN = set()


def get(key):
    over = (services.config.get("apps", "playbooks") or {}).get(key)
    return over.strip() if over and over.strip() else DEFAULTS.get(key, "")


def keys_for(names):
    out = []
    for n in names or []:
        k = ALIASES.get(str(n).lower().strip()) or (str(n).lower() if str(n).lower() in DEFAULTS else None)
        if k and k not in out:
            out.append(k)
    return out


def detect(text):
    low = (text or "").lower()
    return keys_for([a for a in ALIASES if a in low and True
                     and True])


def render(keys):
    parts = [get(k) for k in keys if get(k)]
    return ("APP PLAYBOOKS:\n" + "\n\n".join(parts)) if parts else ""


def all_for_ui():
    return [{"key": k, "label": LABELS.get(k, k), "default": v, "custom": (services.config.get("apps", "playbooks") or {}).get(k, "")}
            for k, v in DEFAULTS.items() if k not in HIDDEN]
