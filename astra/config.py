"""Settings (JSON in %APPDATA%/AstraNova) and secrets (Windows Credential Manager via keyring)."""
import copy
import json
import threading

from .paths import app_dir

DEFAULTS = {
    "profile": {
        "name": "",          # what Astra calls you
        "full_name": "",
        "legal_name": "",    # private: forms only, never shared with other people
        "address": "",
        "birth_date": "",
        "email": "",
        "phone": "",
        "city": "",
        "about": "",         # free text: what you do, your style, anything Astra should know
    },
    "assistant": {
        "style": "concise",   # concise | detailed
        "language": "auto",
    },
    "friend": {
        "name": "Nova",
        "personality": ("Early twenties, warm, funny and a little chaotic in a good way. Into music, shows, games and "
                        "late-night conversations. Teases a bit, hypes you up when you deserve it and is honest when "
                        "you don't. Remembers what you tell her and actually follows up on it."),
        "can_hand_off": True,   # Nova passes PC / web jobs to Astra; they run inside Nova's chat
        "texting": "genz",      # genz: lowercase, no periods, no emojis (enforced) | normal
        "proactive": True,      # Nova may text you first (check-ins, reminders)
        "proactive_level": "normal",   # rare | normal | often
        "quiet_start": "23:00",
        "quiet_end": "09:00",
        "proactive_discord": False,
        "auto_memory": True,    # save lasting facts from chats automatically (background)
        "auto_events": True,    # plans with a day/time mentioned to Nova go straight into the calendar
    },
    "updates": {
        "auto": True,            # public edition: download new versions in the background, install on close
        "last_check": 0,
    },
    "model": {
        "provider": "ollama",           # ollama | openai (any OpenAI-compatible API: OpenAI, OpenRouter, Groq...)
        "name": "qwen3:14b",
        "vision": "qwen2.5vl:7b",       # used to look at the screen (local)
        "think": False,
        "num_ctx": 24576,
        "auto_ctx": True,                # grow the context window automatically for big tasks
        "max_ctx": 49152,                # upper limit for auto context (VRAM)
        "temperature": 0.4,
        "keep_alive": "10m",
        "light": "auto",                 # light mode for PCs without a graphics card: auto | on | off             # free the graphics card after this long idle (2m | 10m | 30m | always)
        "api_base": "https://api.openai.com/v1",
        "api_model": "gpt-4.1-mini",
        "api_vision_model": "",          # empty = same as api_model
    },
    "appearance": {
        "background": "light",           # light | dark
        "accent": "lavender",            # lavender | rose | ocean | mint | sunset | aurora | mono
        "mood_accent": True,             # accent follows the mood of the conversation
    },
    "privacy": {"extra_private": ""},    # extra words/names that must never reach other people
    "control": {
        "background_mode": True,         # never take over the screen without asking
        "full_disk": False,              # file tools may read every drive (private folders stay blocked)
        "enabled": True,                 # allow Astra to operate apps on this PC
        "confirm_messages": True,        # ask before sending a message to anyone
        "confirm_commands": True,        # ask before running shell commands
        "confirm_files": True,           # ask before deleting/overwriting files
        "blocked_apps": "KeePass, 1Password, Bitwarden, Banking",
        "stop_hotkey": True,             # Ctrl+Alt+X stops everything
    },
    "apps": {
        "spotify_client_id": "",
        "spotify_redirect": "http://127.0.0.1:8765/callback",
        "music_service": "spotify",      # spotify | apple_music
        "playbooks": {},                 # user overrides of app tips: {app_key: text}
    },
    "discord_account": {                 # your own Discord account, logged in inside Astra's browser
        "connected": False,
        "rich_presence": True,
        "rpc_client_id": "",             # Discord application id for the "Talking with Astra" status
    },
    "auto_astra": {
        "enabled": False,
        "who": "allowlist",              # allowlist | everyone
        "allow": "",                     # comma separated names
        "disclose": True,                # add a small note that Astra is answering
        "style_notes": "",
        "check_seconds": 45,
    },
    "telegram": {"chat_id": "", "owner_username": "", "enabled": False},
    "personal": {"adapt": True, "learn_moods": True, "learned_style": "", "custom_moods": []},
    "voice_input": {"engine": "windows", "model": "base", "auto_send": False, "language": "auto"},
    "voice": {"enabled": False, "engine": "windows", "voice": "", "astra": True, "friend": True, "rate": 0},
    "university": {"provider": "moodle", "url": "", "username": ""},
    "discord": {
        "enabled": False,
        "owner_id": "",                  # your Discord user id (set by pairing)
        "owner_name": "",
        "owner_username": "",    # only this account may launch games through the bot
        "reply_in_servers": True,        # answer when @mentioned in servers
        "others_mode": "chat",           # off | chat | friend   (what non-owners get; never PC control)
        "owner_pc_control": True,        # you can run PC tasks from Discord DMs
        "task_updates": True,            # DM you when a longer desktop task finishes
        "approvals_to_dm": True,         # approvals/questions also go to your DMs
        "persona": "astra",              # astra | friend  (who answers in servers)
    },
    "email": {
        "address": "",
        "username": "",
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "imap_host": "",
        "imap_port": 993,
    },
    "mail": {
        "accounts": [],                  # [{id, label, address, username, smtp_host, smtp_port, smtp_security,
                                         #   imap_host, imap_port, watch, important}]  passwords in Credential Manager
        "check_minutes": 10,
        "notify_important": True,
        "auto_reply": False,
        "auto_reply_categories": ["proposal", "commission", "collaboration", "booking"],
        "auto_reply_text": ("Hi{name},\n\nthanks for reaching out! I've passed your message on to {owner}, "
                            "they'll get back to you as soon as possible.\n\nBest,\n{owner}'s assistant"),
        "blacklist": "",
        # what counts as noise: these kinds are never shown or announced. always/never: senders or words
        "filter": {"hide": ["security", "account", "shopping", "promotion", "newsletter", "social", "spam"],
                   "always": "", "never": "", "only_important": True},
    },
    "calendar": {"sources": [], "remind_minutes": 30},
    "phone": {"enabled": False, "port": 8787, "tunnel": True, "lan": False},
    "ui": {"welcome": True, "peek": True},
    "browser": {"channel": "msedge", "headless": True, "cookies": "decline", "extensions_dir": ""},
    "approvals": {"send_email": True, "submit_forms": True},
    "setup_done": False,
}

SECRET_NAMES = ("email_password", "discord_token", "api_key", "telegram_token", "uni_password", "canvas_token",
                "hf_key", "phone_token")
_SECRET_PREFIXES = ("mail_pw:",)
_KEYRING_SERVICE = "Astra-Assistant"   # unchanged on purpose: keeps every secret saved by Astra


def _secret_ok(name):
    return name in SECRET_NAMES or name.startswith(_SECRET_PREFIXES)


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self):
        self.path = app_dir() / "config.json"
        self._lock = threading.RLock()
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        self.data = _merge(DEFAULTS, data)
        self._upgrade(data)
        self.save()

    def _upgrade(self, raw):
        """Bring settings from Astra forward."""
        d = self.data
        old = d.get("email") or {}
        if old.get("address") and not d["mail"]["accounts"]:   # the single email account -> first of the list
            d["mail"]["accounts"].append({"id": "main", "label": "Main", "watch": True, "important": "",
                                          **{k: old.get(k) for k in ("address", "username", "smtp_host", "smtp_port",
                                                                     "smtp_security", "imap_host", "imap_port")}})
        if "browser" in raw and "cookies" not in raw.get("browser", {}):
            d["browser"]["headless"] = True   # the browser now works in the background by default
        if (raw.get("friend") or {}).get("personality", "").startswith("Warm, curious and a bit witty."):
            d["friend"]["personality"] = DEFAULTS["friend"]["personality"]
        if int(raw.get("cfg_version") or 0) < 22:   # 2.2: Astra works fully in the background
            d["browser"]["headless"] = True
            d["control"]["background_mode"] = True
        d["cfg_version"] = 22

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, section, key=None, default=None):
        with self._lock:
            sec = self.data.get(section, default)
            if key is None:
                return copy.deepcopy(sec)
            if isinstance(sec, dict):
                return copy.deepcopy(sec.get(key, default))
            return default

    def update(self, section, values):
        with self._lock:
            if isinstance(self.data.get(section), dict) and isinstance(values, dict):
                self.data[section].update(values)
            else:
                self.data[section] = values
            self.save()

    def reset(self):
        """Back to factory settings (used by Settings > Start over)."""
        mail_ids = [x.get("id") for x in (self.data.get("mail", {}) or {}).get("accounts", []) if x.get("id")]
        for name in list(SECRET_NAMES) + ["mail_pw:" + i for i in mail_ids]:
            try:
                self.set_secret(name, "")
            except Exception:  # noqa: BLE001
                pass
        try:
            import keyring
            for key in list((self.data.get("_site_index") or [])):
                try:
                    keyring.delete_password(_KEYRING_SERVICE + "-sites", key)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self.data = copy.deepcopy(DEFAULTS)
            self.save()

    # ---- secrets -------------------------------------------------------
    def set_secret(self, name, value):
        if not _secret_ok(name):
            raise ValueError("unknown secret")
        try:
            import keyring
            if value:
                keyring.set_password(_KEYRING_SERVICE, name, value)
            else:
                try:
                    keyring.delete_password(_KEYRING_SERVICE, name)
                except Exception:
                    pass
            with self._lock:  # drop any old fallback copy
                if self.data.get("_secrets", {}).pop(name, None) is not None:
                    self.save()
            return
        except Exception:
            pass
        with self._lock:
            self.data.setdefault("_secrets", {})[name] = value
            self.save()

    def get_secret(self, name):
        try:
            import keyring
            v = keyring.get_password(_KEYRING_SERVICE, name)
            if v:
                return v
        except Exception:
            pass
        return self.data.get("_secrets", {}).get(name, "")

    def set_site_password(self, site, username, password):
        try:
            import keyring
            keyring.set_password(_KEYRING_SERVICE + "-sites", f"{site}|{username}", password)
            return True
        except Exception:
            with self._lock:
                self.data.setdefault("_site_secrets", {})[f"{site}|{username}"] = password
                self.save()
            return True

    def get_site_password(self, site, username):
        try:
            import keyring
            v = keyring.get_password(_KEYRING_SERVICE + "-sites", f"{site}|{username}")
            if v:
                return v
        except Exception:
            pass
        return self.data.get("_site_secrets", {}).get(f"{site}|{username}", "")

    def public(self):
        with self._lock:
            d = {k: copy.deepcopy(v) for k, v in self.data.items() if not k.startswith("_")}
        d["secrets"] = {n: bool(self.get_secret(n)) for n in SECRET_NAMES}
        d["mail_secrets"] = {a["id"]: bool(self.get_secret("mail_pw:" + a["id"]) or
                                           (a["id"] == "main" and self.get_secret("email_password")))
                             for a in d.get("mail", {}).get("accounts", [])}
        from .paths import edition
        d["edition"] = edition()
        return d
