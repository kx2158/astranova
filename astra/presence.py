# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Discord Rich Presence: shows that you're in AstraNova on your Discord profile, only while you're actually using it
(its window is in front, with a short grace period when you switch away), and without an elapsed-time counter.

Talks to the Discord desktop app over its local IPC pipe. Needs a Discord application id (any app you created at
discord.com/developers; the bot's app works). Upload an image named "astra" under Rich Presence > Art Assets to
get the icon on your profile (use assets/discord-presence.png, 1024x1024).
"""
import json
import os
import struct
import sys
import threading
import time
import uuid

from . import services


class Presence:
    def __init__(self):
        self.pipe = None
        self.client_id = None
        self.start = int(time.time())
        self.view = "chat"
        self.lock = threading.Lock()
        self.last = None
        self.error = ""
        self.active_at = 0.0
        self.GRACE = 45   # seconds it stays after you switch to another window

    # ---- IPC framing -----------------------------------------------------------------------------------------
    def _send(self, op, payload):
        data = json.dumps(payload).encode("utf-8")
        self.pipe.write(struct.pack("<II", op, len(data)) + data)
        self.pipe.flush()

    def _recv(self):
        head = self.pipe.read(8)
        if len(head) < 8:
            raise OSError("pipe closed")
        op, n = struct.unpack("<II", head)
        return op, json.loads(self.pipe.read(n) or b"{}")

    def _connect(self, client_id):
        last = None
        for i in range(10):
            try:
                if sys.platform == "win32":
                    self.pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
                else:   # Mac: Discord listens on a socket in the temp folder
                    import socket
                    import tempfile
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(os.path.join(os.environ.get("TMPDIR") or tempfile.gettempdir(), f"discord-ipc-{i}"))
                    self.pipe = sock.makefile("rwb", buffering=0)
                break
            except OSError as e:
                last = e
        if not self.pipe:
            raise OSError(f"Discord app isn't running ({last})")
        self._send(0, {"v": 1, "client_id": str(client_id)})
        op, data = self._recv()
        if data.get("evt") != "READY":
            raise OSError(data.get("data", {}).get("message", "handshake failed"))
        self.client_id = client_id

    def _close(self):
        try:
            if self.pipe:
                self.pipe.close()
        except OSError:
            pass
        self.pipe = None

    # ---- are you actually in AstraNova? -----------------------------------------------------------------------
    def _in_front(self):
        """True when AstraNova's own window is the foreground window and not minimised."""
        if sys.platform != "win32":
            return True
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd or u.IsIconic(hwnd):
            return False
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def in_use(self):
        try:
            if self._in_front():
                self.active_at = time.time()
        except Exception:  # noqa: BLE001
            self.active_at = time.time()
        return time.time() - self.active_at < self.GRACE

    # ---- activity ----------------------------------------------------------------------------------------------
    def wanted(self):
        cfg = services.config
        friend = cfg.get("friend", "name") or "Nova"
        busy = bool(getattr(services, "running_ids", lambda: [])())
        if self.view == "friend":
            details, state = f"Talking to {friend}", "on AstraNova"
        elif busy:
            details, state = "Astra is on it", "on AstraNova"
        else:
            details, state = "Talking to Astra", "on AstraNova"
        # no "timestamps": Discord would show an elapsed timer
        return {"details": details, "state": state,
                "assets": {"large_image": "astra", "large_text": "AstraNova"}}

    def set_view(self, view):
        self.view = view
        threading.Thread(target=self.update, daemon=True).start()

    def update(self):
        dc = services.config.get("discord_account")
        cid = dc.get("rpc_client_id") or (services.discord.app_id if services.discord and services.discord.app_id else "")
        if not dc.get("rich_presence") or not cid or not self.in_use():
            self.clear()
            return
        act = self.wanted()
        with self.lock:
            if act == self.last and self.pipe:
                return
            try:
                if not self.pipe or self.client_id != cid:
                    self._close()
                    self._connect(cid)
                self._send(1, {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": act},
                               "nonce": uuid.uuid4().hex})
                self._recv()
                self.last, self.error = act, ""
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:160]
                self._close()
                self.last = None

    def clear(self):
        with self.lock:
            if self.pipe and self.last is not None:
                try:
                    self._send(1, {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid()}, "nonce": uuid.uuid4().hex})
                    self._recv()
                except Exception:  # noqa: BLE001
                    self._close()
            self.last = None

    def run(self):
        def loop():
            while True:
                self.update()
                time.sleep(5)
        threading.Thread(target=loop, daemon=True, name="presence").start()

    def status(self):
        return {"showing": self.last is not None, "in_use": time.time() - self.active_at < self.GRACE,
                "error": self.error}
