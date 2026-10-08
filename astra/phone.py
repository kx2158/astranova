"""AstraNova on your iPhone: the PC serves a phone version of the app (chat with Nova, Astra's jobs, approvals)
and opens a secure Cloudflare tunnel, so it works from anywhere - not only on home Wi-Fi. On the iPhone you open the
link once in Safari and choose Share > Add to Home Screen; it then opens like an app. Everything stays on your PC;
the phone is a remote screen for it, protected by a long secret key in the link.

The tunnel uses Cloudflare's free quick tunnels (no account). The address changes when AstraNova restarts, so
AstraNova can DM you the new link on Discord; with your own Cloudflare tunnel you can keep a fixed address."""
import collections
import hmac
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from . import services
from .paths import app_dir, resource_path

CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PHONE_EVENTS = {"token", "assistant_start", "done", "tool_start", "tool_end", "rewrite", "request", "request_done",
                "task_start", "task_done", "notify", "error", "notes", "phase", "nova_checkin"}
STATIC = {"/": ("mobile/index.html", "text/html; charset=utf-8"),
          "/manifest.json": ("mobile/manifest.json", "application/manifest+json"),
          "/icon.png": ("mobile/icon.png", "image/png"),
          "/icon-512.png": ("mobile/icon-512.png", "image/png")}


class PhoneLink:
    def __init__(self):
        self.api = None                    # the desktop Api object (set by app)
        self.server = None
        self.tunnel = None
        self.public_url = ""
        self.error = ""
        self.events = collections.deque(maxlen=600)
        self.seq = 0
        self.cv = threading.Condition()
        self.fails = collections.deque(maxlen=50)

    # ---- events from the app -------------------------------------------------------------
    def push(self, evt):
        if not self.server or evt.get("type") not in PHONE_EVENTS:
            return
        conv = str(evt.get("conv") or "")
        if conv and conv != "friend" and not conv.startswith("nova-task-") and evt.get("type") not in ("request", "request_done", "notify"):
            return
        with self.cv:
            self.seq += 1
            self.events.append((self.seq, evt))
            self.cv.notify_all()

    # ---- lifecycle ----------------------------------------------------------------------------
    def token(self):
        t = services.config.get_secret("phone_token")
        if not t:
            t = secrets.token_urlsafe(24)
            services.config.set_secret("phone_token", t)
        return t

    def new_token(self):
        services.config.set_secret("phone_token", secrets.token_urlsafe(24))
        return self.status()

    def apply(self):
        cfg = services.config.get("phone")
        if cfg.get("enabled") and not self.server:
            self._start_server(int(cfg.get("port") or 8787))
        if not cfg.get("enabled") and self.server:
            self.stop()
            return self.status()
        if self.server and cfg.get("tunnel") and not self.tunnel:
            threading.Thread(target=self._start_tunnel, daemon=True, name="tunnel").start()
        if self.server and not cfg.get("tunnel") and self.tunnel:
            self._stop_tunnel()
        return self.status()

    def stop(self):
        self._stop_tunnel()
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
        self.server = None

    def status(self):
        port = int(services.config.get("phone", "port") or 8787)
        local = _lan_ip() if services.config.get("phone", "lan") else ""
        base = self.public_url or (f"http://{local}:{port}" if self.server and local else "")
        return {"running": bool(self.server), "public": self.public_url, "lan": f"http://{local}:{port}" if local else "",
                "link": f"{base}/#k={self.token()}" if base else "", "error": self.error,
                "tunnel": bool(self.tunnel), "tunnel_wanted": bool(services.config.get("phone", "tunnel"))}

    def _start_server(self, port):
        link = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                key = self.headers.get("X-AstraNova-Key", "")
                now = time.time()
                if sum(1 for t in link.fails if now - t < 60) > 20:
                    return False
                ok = bool(key) and hmac.compare_digest(key, link.token())
                if not ok:
                    link.fails.append(now)
                return ok

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 100_000:
                    return {}
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                if u.path in STATIC:
                    rel, ctype = STATIC[u.path]
                    p = resource_path("ui/" + rel)
                    if p.exists():
                        return self._send(200, p.read_bytes(), ctype)
                    return self._send(404, {"error": "missing"})
                if not u.path.startswith("/api/"):
                    return self._send(404, {"error": "not found"})
                if not self._authed():
                    return self._send(401, {"error": "bad key"})
                qs = parse_qs(u.query)
                if u.path == "/api/state":
                    c = services.config
                    return self._send(200, {"friend": c.get("friend", "name") or "Nova",
                                            "name": c.get("profile", "name") or "",
                                            "theme": c.get("ui", "theme") or "system", "seq": link.seq,
                                            "requests": services.bridge.open_requests(),
                                            "running": link.api.running_list() if link.api else []})
                if u.path == "/api/chat":
                    return self._send(200, link.api.friend_history())
                if u.path == "/api/poll":
                    since = int((qs.get("since") or ["0"])[0])
                    deadline = time.time() + 25
                    with link.cv:
                        while link.seq <= since and time.time() < deadline:
                            link.cv.wait(deadline - time.time())
                        evs = [{"seq": s, **e} for s, e in link.events if s > since]
                    return self._send(200, {"seq": link.seq, "events": evs})
                return self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                u = urlparse(self.path)
                if not self._authed():
                    return self._send(401, {"error": "bad key"})
                b = self._body()
                if u.path == "/api/send":
                    text = str(b.get("text") or "")[:8000]
                    return self._send(200, link.api.send_message(None, text, "friend") or {})
                if u.path == "/api/answer":
                    return self._send(200, {"ok": services.bridge.resolve(str(b.get("id")), b.get("value"))})
                if u.path == "/api/stop":
                    link.api.panic()
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})

        try:
            host = "0.0.0.0" if services.config.get("phone", "lan") else "127.0.0.1"
            self.server = ThreadingHTTPServer((host, port), Handler)
            self.server.daemon_threads = True
        except OSError as e:
            self.error = f"Port {port} is busy ({e}). Pick another port."
            self.server = None
            return
        threading.Thread(target=self.server.serve_forever, daemon=True, name="phone").start()
        self.error = ""

    # ---- Cloudflare quick tunnel ------------------------------------------------------------------
    def _cloudflared(self):
        exe = app_dir() / "bin" / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
        if exe.exists() and exe.stat().st_size > 1_000_000:
            return exe
        import shutil
        found = shutil.which("cloudflared")
        if found:
            return Path(found)
        if sys.platform != "win32":
            raise RuntimeError("Install cloudflared to use the tunnel.")
        exe.parent.mkdir(parents=True, exist_ok=True)
        self.error = "Downloading the tunnel helper (cloudflared, ~60 MB)..."
        services.emit({"type": "phone_status", **self.status()})
        tmp = exe.with_suffix(".part")
        with requests.get(CLOUDFLARED_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(exe)
        return exe

    def _start_tunnel(self):
        try:
            exe = self._cloudflared()
            port = int(services.config.get("phone", "port") or 8787)
            self.tunnel = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           creationflags=NO_WINDOW)
            self.error = "Opening the tunnel..."
            services.emit({"type": "phone_status", **self.status()})
            for line in self.tunnel.stdout:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not self.public_url:
                    self.public_url = m.group(0)
                    self.error = ""
                    services.emit({"type": "phone_status", **self.status()})
                    self._announce()
            self.error = "The tunnel closed." if self.tunnel else ""
        except Exception as e:  # noqa: BLE001
            self.error = f"Tunnel failed: {str(e)[:200]}"
        finally:
            self.public_url = ""
            self.tunnel = None
            services.emit({"type": "phone_status", **self.status()})

    def _announce(self):
        """New address -> tell the owner on Discord so the phone app can be reopened."""
        dc = services.discord
        last = services.db.kv_get("phone_last_url")
        services.db.kv_set("phone_last_url", self.public_url)
        if dc and dc.ready() and last and last != self.public_url:
            try:
                dc.dm_owner(f"AstraNova's phone link changed - open this on your iPhone:\n{self.status()['link']}")
            except Exception:  # noqa: BLE001
                pass

    def _stop_tunnel(self):
        t, self.tunnel = self.tunnel, None
        if t:
            try:
                t.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.public_url = ""


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return ""
    finally:
        s.close()
