# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Human-in-the-loop: approvals and questions, answerable from the app window OR your Discord DMs."""
import threading
import uuid

YES = ("yes", "y", "ok", "okay", "approve", "approved", "sure", "go", "send", "do it", "ja", "da", "áno", "ano")


_local = threading.local()


class Pending:
    def __init__(self, kind, title, details):
        self.id = uuid.uuid4().hex[:10]
        self.conv = getattr(_local, "conv", None)   # which chat asked (so the card shows up in the right place)
        self.kind = kind  # "confirm" | "ask"
        self.title = title
        self.details = details
        self.event = threading.Event()
        self.answer = None


class Bridge:
    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()
        self.emit = lambda evt: None        # set by app (UI events)
        self.discord = None                 # set by app
        self.telegram = None                # set by app

    @staticmethod
    def set_origin(conv):
        """Called by each agent on its own thread: approvals it asks for are tagged with its conversation."""
        _local.conv = conv

    def _wait(self, p, timeout):
        with self.lock:
            self.pending[p.id] = p
        self.emit({"type": "request", "id": p.id, "kind": p.kind, "title": p.title, "details": p.details, "conv": p.conv})
        for bot in (self.discord, self.telegram):
            if bot and bot.ready():
                try:
                    bot.send_request(p)
                except Exception:
                    pass
        p.event.wait(timeout)
        with self.lock:
            self.pending.pop(p.id, None)
        self.emit({"type": "request_done", "id": p.id, "answer": p.answer})
        return p.answer

    def confirm(self, title, details="", timeout=1800):
        ans = self._wait(Pending("confirm", title, details), timeout)
        return ans is True or (isinstance(ans, str) and ans.strip().lower() in YES)

    def ask(self, question, details="", timeout=1800):
        ans = self._wait(Pending("ask", question, details), timeout)
        return ans if isinstance(ans, str) else None

    def resolve(self, pid, answer):
        with self.lock:
            p = self.pending.get(pid)
        if not p:
            return False
        p.answer = answer
        p.event.set()
        return True

    def oldest_open(self):
        with self.lock:
            return next(iter(self.pending.values()), None)

    def open_requests(self):
        with self.lock:
            return [{"id": p.id, "kind": p.kind, "title": p.title, "details": p.details, "conv": p.conv}
                    for p in self.pending.values()]

    def cancel_all(self):
        for r in self.open_requests():
            self.resolve(r["id"], None)
