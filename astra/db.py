"""SQLite storage: conversations, long-term memory, routines, accounts, activity log."""
import json
import re
import sqlite3
import threading
import time
import uuid

from .paths import app_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL, messages TEXT, kind TEXT DEFAULT 'astra'
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, source TEXT, created REAL, used REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routines (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, prompt TEXT, next_run REAL, repeat TEXT DEFAULT 'once',
  enabled INTEGER DEFAULT 1, last_run REAL DEFAULT 0, last_result TEXT DEFAULT '', created REAL
);
CREATE TABLE IF NOT EXISTS accounts (
  site TEXT, username TEXT, notes TEXT, created REAL, PRIMARY KEY(site, username)
);
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, criteria TEXT, urls TEXT, spec TEXT DEFAULT '',
  interval_min INTEGER DEFAULT 30, enabled INTEGER DEFAULT 1, auto_contact INTEGER DEFAULT 0,
  last_run REAL DEFAULT 0, last_status TEXT DEFAULT '', created REAL
);
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER, url TEXT, title TEXT, price TEXT,
  rooms TEXT, size TEXT, location TEXT, matches INTEGER, reason TEXT, first_seen REAL,
  UNIQUE(watch_id, url)
);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, name TEXT, folder TEXT, size INTEGER, mtime REAL
);
CREATE INDEX IF NOT EXISTS files_name ON files(name);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, origin TEXT, task TEXT, status TEXT, result TEXT DEFAULT '', created REAL, finished REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, topic TEXT, title TEXT, data TEXT, source TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS events (
  uid TEXT, source TEXT, title TEXT, start REAL, end REAL, all_day INTEGER DEFAULT 0, location TEXT DEFAULT '',
  notes TEXT DEFAULT '', reminded INTEGER DEFAULT 0, PRIMARY KEY(uid, source, start)
);
CREATE TABLE IF NOT EXISTS event_series (
  id TEXT PRIMARY KEY, title TEXT, start REAL, minutes INTEGER DEFAULT 60, all_day INTEGER DEFAULT 0,
  location TEXT DEFAULT '', freq TEXT, interval INTEGER DEFAULT 1, byday TEXT DEFAULT '', until REAL DEFAULT 0,
  exdates TEXT DEFAULT '[]', created REAL
);
CREATE TABLE IF NOT EXISTS event_done (
  uid TEXT, start REAL, done_at REAL, PRIMARY KEY(uid, start)
);
CREATE TABLE IF NOT EXISTS mail_seen (
  account TEXT, msgid TEXT, sender TEXT, subject TEXT, category TEXT, important INTEGER DEFAULT 0,
  summary TEXT DEFAULT '', replied INTEGER DEFAULT 0, told INTEGER DEFAULT 0, ts REAL, PRIMARY KEY(account, msgid)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, text TEXT
);
"""

# how long a memory is kept, in days (0 = forever)
KEEP = {"day": 1, "week": 7, "month": 30, "season": 90, "year": 365, "forever": 0}


def expiry(keep, until=None):
    """Timestamp a memory expires at (0 = never)."""
    import datetime
    if keep == "until" and until:
        try:
            d = datetime.datetime.fromisoformat(str(until)[:10]) + datetime.timedelta(days=2)
            return d.timestamp()
        except ValueError:
            keep = "month"
    days = KEEP.get(keep or "forever", 0)
    return time.time() + days * 86400 if days else 0


_WORD = re.compile(r"[\w']{3,}", re.U)


class DB:
    def __init__(self):
        self.conn = sqlite3.connect(str(app_dir() / "astra.db"), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(memories)")}
            if "expires" not in cols:   # memory lifetimes (AstraNova 2.2)
                self.conn.execute("ALTER TABLE memories ADD COLUMN expires REAL DEFAULT 0")
                self.conn.execute("ALTER TABLE memories ADD COLUMN keep TEXT DEFAULT ''")
            self.conn.commit()

    def q(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return [dict(r) for r in cur.fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def inserted(self, sql, args=()):
        """Run an INSERT OR IGNORE; True only if a new row was really added."""
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount > 0

    # ---- conversations ---------------------------------------------------
    def new_conversation(self, title="New chat", conv_id=None, kind="astra"):
        cid = conv_id or uuid.uuid4().hex[:12]
        now = time.time()
        self.x("INSERT OR IGNORE INTO conversations (id,title,created,updated,messages,kind) VALUES (?,?,?,?,?,?)",
               (cid, title, now, now, "[]", kind))
        return cid

    def wipe(self):
        """Empty every table and shrink the file (Settings > Start over)."""
        with self.lock:
            names = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                     "AND name NOT LIKE 'sqlite_%'")]
            for n in names:
                try:
                    self.conn.execute(f'DELETE FROM "{n}"')
                except sqlite3.Error:
                    pass
            self.conn.commit()
            try:
                self.conn.execute("VACUUM")
            except sqlite3.Error:
                pass

    def get_messages(self, cid):
        rows = self.q("SELECT messages FROM conversations WHERE id=?", (cid,))
        return json.loads(rows[0]["messages"]) if rows else []

    def save_messages(self, cid, messages, title=None, kind="astra"):
        if not self.q("SELECT id FROM conversations WHERE id=?", (cid,)):
            self.new_conversation(title or "New chat", cid, kind)
        data = json.dumps(messages, ensure_ascii=False)
        if title:
            self.x("UPDATE conversations SET messages=?, updated=?, title=? WHERE id=?", (data, time.time(), title, cid))
        else:
            self.x("UPDATE conversations SET messages=?, updated=? WHERE id=?", (data, time.time(), cid))

    def kv_get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT OR REPLACE INTO kv (k, v) VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))

    def list_conversations(self, kind="astra"):
        return self.q("SELECT id, title, updated FROM conversations WHERE kind=? ORDER BY updated DESC LIMIT 100", (kind,))

    def delete_conversation(self, cid):
        self.x("DELETE FROM conversations WHERE id=?", (cid,))

    # ---- memory ------------------------------------------------------------
    def remember(self, text, source="astra", keep="", until=None):
        """keep: how long the fact stays (see KEEP): day | week | month | season | year | until | forever.
        '' = not decided yet (the background pass picks one)."""
        text = (text or "").strip().rstrip(".")
        if not text:
            return None
        exp = expiry(keep, until)
        dup = self.similar_memory(text)
        if dup:
            if len(text) > len(dup["text"]) + 8:   # newer, more detailed version of the same fact
                self.x("UPDATE memories SET text=?, created=? WHERE id=?", (text[:600], time.time(), dup["id"]))
            if keep:
                self.x("UPDATE memories SET keep=?, expires=? WHERE id=?", (keep, exp, dup["id"]))
            return dup["id"]
        return self.x("INSERT INTO memories (text, source, created, keep, expires) VALUES (?,?,?,?,?)",
                      (text[:600], source, time.time(), keep or "", exp))

    def set_memory_keep(self, mid, keep, until=None):
        self.x("UPDATE memories SET keep=?, expires=? WHERE id=?", (keep, expiry(keep, until), int(mid)))

    def purge_expired(self):
        n = self.q("SELECT COUNT(*) AS n FROM memories WHERE expires>0 AND expires<?", (time.time(),))[0]["n"]
        if n:
            self.x("DELETE FROM memories WHERE expires>0 AND expires<?", (time.time(),))
        return n

    def update_memory(self, mid, text):
        self.x("UPDATE memories SET text=?, created=? WHERE id=?", (text.strip()[:600], time.time(), int(mid)))

    def similar_memory(self, text, threshold=0.8):
        """Near-duplicate check so the auto memory doesn't save the same thing twice."""
        words = {w.lower() for w in _WORD.findall(text)}
        if not words:
            return None
        best, score = None, 0.0
        for r in self.memories(800):
            other = {w.lower() for w in _WORD.findall(r["text"])}
            if not other:
                continue
            s = len(words & other) / max(1, min(len(words), len(other)))
            if s > score:
                best, score = r, s
        return best if score >= threshold else None

    def memories(self, limit=500):
        return self.q("SELECT * FROM memories WHERE expires=0 OR expires>? ORDER BY id DESC LIMIT ?",
                      (time.time(), limit))

    def forget(self, mid):
        self.x("DELETE FROM memories WHERE id=?", (int(mid),))

    def recall(self, query="", limit=12):
        """Keyword-ranked memory search (no embeddings needed)."""
        rows = self.memories()
        words = {w.lower() for w in _WORD.findall(query or "")}
        if not words:
            return rows[:limit]
        scored = []
        for r in rows:
            t = r["text"].lower()
            s = sum(1 for w in words if w in t)
            if s:
                scored.append((s, r["id"], r))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [r for _, _, r in scored[:limit]]

    def memory_context(self, query="", limit=18):
        """Memories to pin into a system prompt: relevant ones first, then the most recent."""
        picked, seen = [], set()
        for r in self.recall(query, 10) + self.memories(limit):
            if r["id"] not in seen:
                seen.add(r["id"])
                picked.append(r)
            if len(picked) >= limit:
                break
        return picked

    # ---- activity ----------------------------------------------------------
    def log(self, kind, text):
        self.x("INSERT INTO activity (ts, kind, text) VALUES (?,?,?)", (time.time(), kind, str(text)[:2000]))

    def activity(self, limit=200):
        return self.q("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
