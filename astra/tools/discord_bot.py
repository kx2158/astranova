# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Discord bot, friend mode only (Settings > Discord bot).

- Everyone talks to your friend persona. In servers and with other people it is public-safe: no memories, no
  personal context, and every reply is scrubbed for personal details before it is sent.
- Your own DMs (paired account, username your username) get the friend with your memories, plus "play <game>".
- No PC control through Discord. Use the Telegram bot's /menu for that.
"""
import asyncio
import re
import secrets
import threading
import time

from .. import services
from ..bridge import YES
from . import tool

PERMISSIONS = 1024 + 2048 + 16384 + 32768 + 65536 + 64 + 274877906944  # view, send, embed, attach, history, react, threads


def _chunks(text, n=1900):
    text = text or "(empty)"
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    out.append(text)
    return out


class DiscordService:
    def __init__(self):
        self.thread = None
        self.loop = None
        self.client = None
        self.bot_name = ""
        self.app_id = None
        self.connected = False
        self.last_error = ""
        self.pair_code = None
        self.pairing_until = 0
        self.running_token = None
        self.busy = set()
        # set by app
        self.on_owner = None    # fn(text) -> reply (friend, owner DMs)
        self.on_guest = None    # fn(conv_id, author_name, text, where) -> reply (public-safe friend)
        self.on_game = None     # fn(name) -> reply (owner only)
        self.on_stop = None     # fn()

    # ---- state -----------------------------------------------------------------------
    def token(self):
        return (services.config.get_secret("discord_token") or "").strip()

    def owner_id(self):
        return str(services.config.get("discord", "owner_id") or "")

    def ready(self):
        return bool(self.connected and self.owner_id())

    def invite_link(self):
        return (f"https://discord.com/oauth2/authorize?client_id={self.app_id}&scope=bot+applications.commands"
                f"&permissions={PERMISSIONS}") if self.app_id else ""

    def status(self):
        return {"enabled": bool(services.config.get("discord", "enabled")), "has_token": bool(self.token()),
                "connected": self.connected, "bot": self.bot_name, "owner": self.owner_id(),
                "owner_name": services.config.get("discord", "owner_name"),
                "pairing": time.time() < self.pairing_until, "code": self.pair_code if time.time() < self.pairing_until else "",
                "invite": self.invite_link(), "error": self.last_error, "user_install": self.user_install_link(),
                "command": self.command_name()}

    def user_install_link(self):
        """Adds the app to the owner's own Discord account, so /nova works in any DM or group chat."""
        app = getattr(self, "app_id", None)
        return (f"https://discord.com/oauth2/authorize?client_id={app}&integration_type=1&scope=applications.commands"
                if app else "")

    @staticmethod
    def command_name():
        name = re.sub(r"[^a-z0-9_-]", "", (services.config.get("friend", "name") or "nova").lower())[:32]
        return name or "nova"

    def _changed(self):
        services.emit({"type": "discord_status", **self.status()})

    def _err(self, msg):
        self.last_error = msg
        if msg:
            services.db.log("discord", msg)
        self._changed()

    # ---- lifecycle -------------------------------------------------------------------
    def apply(self):
        """Start/stop/restart to match the settings. Safe to call any time."""
        want = bool(services.config.get("discord", "enabled")) and bool(self.token())
        alive = self.thread and self.thread.is_alive()
        if alive and (not want or self.running_token != self.token()):
            self.shutdown()
            alive = False
        if want and not alive:
            self.running_token = self.token()
            self.last_error = ""
            self.thread = threading.Thread(target=self._run, args=(self.running_token,), daemon=True, name="discord")
            self.thread.start()
        self._changed()

    def shutdown(self):
        if self.loop and self.client:
            try:
                asyncio.run_coroutine_threadsafe(self.client.close(), self.loop).result(10)
            except Exception:
                pass
        if self.thread:
            self.thread.join(5)
        self.connected = False
        self.thread = None

    def start_pairing(self, seconds=600):
        self.pair_code = "ASTRA-" + secrets.token_hex(2).upper()
        self.pairing_until = time.time() + seconds
        self._changed()
        return self.pair_code

    def unpair(self):
        services.config.update("discord", {"owner_id": "", "owner_name": ""})
        self._changed()

    # ---- thread-safe calls -------------------------------------------------------------
    def _call(self, coro, timeout=20):
        if not (self.loop and self.connected):
            coro.close()
            return False
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def dm_owner(self, text):
        if not self.ready():
            return False
        return self._call(self._dm(text))

    def send_request(self, p):
        if self.ready() and services.config.get("discord", "approvals_to_dm"):
            try:
                self._call(self._send_request(p), timeout=10)
            except Exception:
                pass

    def post(self, channel_id, text):
        return self._call(self._post(int(channel_id), text))

    # ---- async side ----------------------------------------------------------------------
    async def _dm(self, text):
        user = await self.client.fetch_user(int(self.owner_id()))
        for c in _chunks(text):
            await user.send(c)
        return True

    async def _post(self, channel_id, text):
        ch = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        for c in _chunks(text):
            await ch.send(c)
        return True

    async def _send_request(self, p):
        import discord
        user = await self.client.fetch_user(int(self.owner_id()))
        if p.kind == "ask":
            await user.send(f"**Question:** {p.title}\n{p.details or ''}\n\nJust reply here.")
            return
        your_turn = p.title.startswith("Your turn")
        bridge = services.bridge

        class View(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=1800)

            @discord.ui.button(label="Done" if your_turn else "Approve", style=discord.ButtonStyle.primary)
            async def yes(self, interaction, _button):
                ok = bridge.resolve(p.id, True)
                await interaction.response.edit_message(
                    content=interaction.message.content + ("\n\n**Approved.**" if ok else "\n\n(already handled)"), view=None)

            @discord.ui.button(label="Cancel" if your_turn else "Decline", style=discord.ButtonStyle.secondary)
            async def no(self, interaction, _button):
                ok = bridge.resolve(p.id, False)
                await interaction.response.edit_message(
                    content=interaction.message.content + ("\n\n**Declined.**" if ok else "\n\n(already handled)"), view=None)

        body = p.details or ""
        if len(body) > 1700:
            body = body[:1700] + "..."
        await user.send(f"**{'Action needed' if your_turn else 'Approve?'}** {p.title}\n```\n{body}\n```", view=View())

    def _run(self, token):
        import discord
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        client = discord.Client(intents=intents)
        self.client = client
        from discord import app_commands
        tree = app_commands.CommandTree(client)
        cmd_name = self.command_name()

        # /nova <message>: usable in DMs and group chats with other people once you add the app to your account.
        # Always the public persona: no memories, no PC, nothing personal.
        @tree.command(name=cmd_name, description=f"Talk to {cmd_name.capitalize()} right here (public mode)")
        @app_commands.describe(message="What to say")
        @app_commands.allowed_installs(guilds=True, users=True)
        @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
        async def talk(interaction, message: str):
            await self._slash(interaction, message)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        svc = self

        @client.event
        async def on_ready():
            svc.connected = True
            svc.bot_name = str(client.user)
            svc.app_id = client.application_id or client.user.id
            svc.last_error = ""
            svc._changed()
            try:
                await tree.sync()     # first time can take a few minutes to show up in Discord
            except Exception as e:  # noqa: BLE001
                services.db.log("discord", f"command sync failed: {e}")

        @client.event
        async def on_disconnect():
            svc.connected = False
            svc._changed()

        @client.event
        async def on_resumed():
            svc.connected = True
            svc._changed()

        @client.event
        async def on_message(msg):
            if msg.author.bot or msg.author.id == client.user.id:
                return
            try:
                await svc._handle(msg, client)
            except Exception as e:  # noqa: BLE001
                services.db.log("discord", f"handler error: {e}")
                try:
                    await msg.channel.send(f"Error: {str(e)[:300]}")
                except Exception:
                    pass

        try:
            loop.run_until_complete(client.start(token))
        except discord.LoginFailure:
            self._err("The bot token is wrong. Copy it again from the Developer Portal (Bot > Reset Token).")
        except discord.PrivilegedIntentsRequired:
            self._err("Turn on 'Message Content Intent' in the Developer Portal (your app > Bot > Privileged "
                      "Gateway Intents), then press Save & connect again.")
        except Exception as e:  # noqa: BLE001
            self._err(f"Discord connection failed: {e}")
        finally:
            self.connected = False
            try:
                loop.run_until_complete(client.close())
            except Exception:
                pass
            loop.close()
            self.loop, self.client = None, None
            self._changed()

    async def _handle(self, msg, client):
        import discord
        text = (msg.content or "").strip()
        uid = str(msg.author.id)
        is_dm = isinstance(msg.channel, discord.DMChannel)
        cfg = services.config.get("discord")
        loop = asyncio.get_running_loop()

        if is_dm:
            m = re.match(r"^!?pair\s+(ASTRA-[0-9A-F]{4})$", text, re.I)
            if m:
                if self.pair_code and m.group(1).upper() == self.pair_code and time.time() < self.pairing_until:
                    services.config.update("discord", {"owner_id": uid, "owner_name": msg.author.display_name})
                    self.pair_code, self.pairing_until = None, 0
                    self._changed()
                    await msg.channel.send(f"paired. hi {msg.author.display_name}, it's "
                                           f"{services.config.get('friend', 'name') or 'Nova'}. just talk to me here. "
                                           f"\"play <game>\" starts a game on your pc.")
                else:
                    await msg.channel.send("That code isn't valid (or expired). Press Pair in Astra's Settings > Discord.")
                return

        owner = self.is_owner(msg.author)
        mentioned = client.user in msg.mentions
        if not is_dm and not (mentioned and cfg.get("reply_in_servers")):
            return
        text = re.sub(rf"<@!?{client.user.id}>", "", text).strip()
        if not text:
            return

        # owner-only quick commands work anywhere (no model call, nothing private in the reply)
        if owner:
            g = re.match(r"^(?:/|!)?(?:play|launch|start|open)\s+(.+)$", text, re.I)
            if g and not g.group(1).lower().startswith(("music", "spotify", "a song", "some")):
                reply = await loop.run_in_executor(None, self.on_game, g.group(1).strip())
                await msg.channel.send(reply)
                return
            if is_dm and text.lower() in ("stop", "/stop", "cancel"):
                if self.on_stop:
                    self.on_stop()
                await msg.channel.send("stopped.")
                return
            p = services.bridge.oldest_open()
            if is_dm and p:
                services.bridge.resolve(p.id, text if p.kind == "ask" else (text.lower().strip(" .!") in YES))
                await msg.channel.send("got it.")
                return

        if not owner and cfg.get("others_mode") == "off":
            return
        # owner DMs: the friend, with memories (private channel). Everywhere else: public-safe friend persona.
        if owner and is_dm:
            key, runner, args = "owner-friend", self.on_owner, (text,)
        else:
            key = f"dg-{uid}" if is_dm else f"dc-{msg.channel.id}"
            where = "Discord DMs" if is_dm else f"a Discord server ({getattr(msg.guild, 'name', 'server')})"
            runner, args = self.on_guest, (key, msg.author.display_name, text, where)
        if key in self.busy:
            await msg.reply("one sec, still typing", mention_author=False)
            return
        self.busy.add(key)
        try:
            async with msg.channel.typing():
                reply = await loop.run_in_executor(None, runner, *args)
        finally:
            self.busy.discard(key)
        for i, c in enumerate(_chunks(reply)):
            if i == 0 and not is_dm:
                await msg.reply(c, mention_author=False)
            else:
                await msg.channel.send(c)

    async def _slash(self, interaction, message):
        import discord
        cfg = services.config.get("discord")
        owner = self.is_owner(interaction.user)
        if not owner and cfg.get("others_mode") == "off":
            await interaction.response.send_message("not available right now", ephemeral=True)
            return
        ch = interaction.channel
        where = ("Discord DMs" if isinstance(ch, discord.DMChannel) or interaction.guild is None and not
                 isinstance(ch, discord.GroupChannel) else "a Discord group chat" if isinstance(ch, discord.GroupChannel)
                 else f"a Discord server ({getattr(interaction.guild, 'name', 'server')})")
        key = f"di-{interaction.channel_id or interaction.user.id}"
        if key in self.busy:
            await interaction.response.send_message("one sec, still answering the last one", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        self.busy.add(key)
        try:
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(None, self.on_guest, key, interaction.user.display_name,
                                               message[:1500], where)
        except Exception as e:  # noqa: BLE001
            services.db.log("discord", f"slash error: {e}")
            reply = "something broke on my side, try again in a sec"
        finally:
            self.busy.discard(key)
        quoted = "> " + message.strip().replace("\n", "\n> ")[:300] + "\n"
        parts = list(_chunks(quoted + (reply or "...")))
        await interaction.followup.send(parts[0])
        for c in parts[1:]:
            await interaction.followup.send(c)

    def is_owner(self, user):
        """Owner = the paired account AND the configured username (your username)."""
        want = (services.config.get("discord", "owner_username") or "").lower().lstrip("@")
        uname = (getattr(user, "name", "") or "").lower()
        return str(user.id) == self.owner_id() and (not want or uname == want)


def bot_missing():
    d = services.discord
    if not (d and d.ready()):
        return "Discord bot not connected."
    return None


@tool("discord_bot_post", "Post a message AS THE ASTRA BOT (not as the user) in a Discord server channel by channel "
      "id. The user approves first. To write as the user use send_message(app='discord').",
      {"channel_id": {"type": "string"}, "text": {"type": "string"}}, ["channel_id", "text"],
      label="Posting as bot", needs=bot_missing)
def discord_bot_post(ctx, channel_id, text):
    if not services.bridge.confirm("Post as the Astra bot?", f"Channel {channel_id}\n\n{text}"):
        return "DECLINED - not posted."
    services.discord.post(channel_id, text)
    services.db.log("discord", f"Bot posted in {channel_id}: {text[:200]}")
    return "Posted."
