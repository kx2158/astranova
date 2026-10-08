# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""System tools: shell commands, files, media keys, system info, notifications, routines, emergency-stop hotkey."""
import datetime
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import services
from ..paths import workspace_dir
from . import tool
from .desktop import control_off

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0


def _p(path):
    p = Path(os.path.expandvars(os.path.expanduser(path or "")))
    if not str(path or "").strip():
        return workspace_dir()
    if not p.is_absolute():
        aliases = {"desktop": Path.home() / "Desktop", "documents": Path.home() / "Documents",
                   "downloads": Path.home() / "Downloads", "music": Path.home() / "Music",
                   "pictures": Path.home() / "Pictures", "videos": Path.home() / "Videos", "astra": workspace_dir()}
        first = p.parts[0].lower() if p.parts else ""
        if first in aliases:
            return aliases[first].joinpath(*p.parts[1:])
        return Path.home() / p
    return p


PRIVATE = ("\\appdata\\roaming\\astra", "credential", "\\.ssh", "keepass", "1password", "bitwarden", ".kdbx",
           "\\google\\chrome\\user data", "\\microsoft\\edge\\user data", "\\mozilla\\firefox\\profiles",
           "\\bravesoftware", "\\opera software", "\\system32\\config", "\\microsoft\\protect", "wallet.dat",
           "\\discord\\local storage", "\\telegram desktop\\tdata", "\\.gnupg")
SKIP_DIRS = {"windows", "$recycle.bin", "system volume information", "node_modules", ".git", "__pycache__", "windowsapps",
             "temp", "cache", "caches", "code cache", "gpucache", "shadercache", "winsxs", "$windows.~bt", "msocache",
             "programdata\\microsoft", "recovery", "perflogs"}


def _private(p):
    s = str(p).lower()
    return any(x in s for x in PRIVATE)


def _allowed(p):
    """Inside the user's folders, or anywhere (minus private places) when full disk access is on."""
    if _private(p):
        return False
    if services.config.get("control", "full_disk"):
        return True
    try:
        Path(p).resolve().relative_to(Path.home().resolve())
        return True
    except ValueError:
        return False


@tool("run_command", ("Run a Terminal (zsh) command on the Mac" if sys.platform == "darwin" else "Run a PowerShell command on the PC") + " and return its output (installs, system settings, scripts, "
      "file operations). The user approves each command unless they turned that off.",
      {"command": {"type": "string"}, "reason": {"type": "string"}, "timeout": {"type": "integer"}},
      ["command", "reason"], label="Running command")
def run_command(ctx, command, reason, timeout=120):
    if services.config.get("control", "confirm_commands"):
        if not services.bridge.confirm("Run this command?", f"{reason}\n\n{command}"):
            return "DECLINED by the user."
    shell = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command] if IS_WIN
             else ["/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh", "-lc", command])   # Mac: Terminal's zsh
    p = subprocess.run(shell, capture_output=True, text=True, timeout=min(int(timeout or 120), 1800),
                       creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
    services.db.log("command", command[:400])
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    return f"exit code {p.returncode}\n{out[-6000:]}" + (f"\nERRORS:\n{err[-2000:]}" if err else "")


@tool("open_path", "Open a file, folder or URL with its default app (e.g. a .blend, a .mid, a folder, a website).",
      {"path": {"type": "string"}}, ["path"], label="Opening")
def open_path(ctx, path):
    if re.match(r"^[a-z][a-z0-9+.-]*:", path) and not re.match(r"^[a-z]:\\", path, re.I):
        target = path
    else:
        target = str(_p(path))
        if not os.path.exists(target):
            return f"Not found: {target}"
    if IS_WIN:
        os.startfile(target)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])
    return f"Opened {target}."


@tool("list_files", "List files in a folder (default Documents/Astra). Shortcuts: desktop, documents, downloads, "
      "music, pictures, videos, astra. pattern like '*.wav'.",
      {"folder": {"type": "string"}, "pattern": {"type": "string"}, "recursive": {"type": "boolean"}},
      label="Listing files")
def list_files(ctx, folder="", pattern="*", recursive=False):
    d = _p(folder)
    if not _allowed(d):
        return f"Blocked: {d} is outside your user folder (turn on full disk access in Settings > Safety) or private."
    if not d.is_dir():
        return f"Not a folder: {d}"
    items = sorted(d.rglob(pattern or "*") if recursive else d.glob(pattern or "*"),
                   key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)[:200]
    rows = []
    for x in items:
        try:
            rows.append(f"{'[dir] ' if x.is_dir() else ''}{x.relative_to(d)}" +
                        ("" if x.is_dir() else f"  ({x.stat().st_size // 1024} KB, "
                         f"{datetime.datetime.fromtimestamp(x.stat().st_mtime):%Y-%m-%d %H:%M})"))
        except OSError:
            continue
    return f"{d}:\n" + ("\n".join(rows) or "(empty)")


@tool("read_text_file", "Read a text file (notes, lyrics, code, csv...).", {"path": {"type": "string"}}, ["path"],
      label="Reading file")
def read_text_file(ctx, path):
    p = _p(path)
    if not _allowed(p):
        return "Blocked: private file, or outside your user folder without full disk access."
    if not p.is_file():
        return f"Not found: {p}"
    return p.read_text(encoding="utf-8", errors="replace")[:20000]


@tool("write_text_file", "Write a text file (default folder Documents/Astra). Asks before overwriting an existing "
      "file. append=true adds to the end.",
      {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}}, ["path", "content"],
      label="Writing file")
def write_text_file(ctx, path, content, append=False):
    p = Path(path)
    p = _p(path) if (p.is_absolute() or (p.parts and p.parts[0].lower() in
                                          ("desktop", "documents", "downloads", "music", "pictures", "videos", "astra"))) \
        else workspace_dir() / p
    if not _allowed(p):
        return "Blocked: private location, or outside your user folder without full disk access."
    if p.exists() and not append and services.config.get("control", "confirm_files"):
        if not services.bridge.confirm("Overwrite this file?", f"{p}\n\nNew content starts with:\n{content[:500]}"):
            return "DECLINED - file not changed."
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return f"{'Appended to' if append else 'Saved'} {p}."


_VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "volume_up": 0xAF, "volume_down": 0xAE,
       "mute": 0xAD}


@tool("media_key", "Press a media key (controls whatever is playing: Spotify, Apple Music, YouTube...): play_pause, "
      "next, previous, stop, volume_up, volume_down, mute. times = how many presses (volume steps are 2%).",
      {"action": {"type": "string", "enum": list(_VK)}, "times": {"type": "integer"}}, ["action"],
      label="Media key", modes=("astra", "friend"), needs=control_off)
def media_key(ctx, action, times=1):
    import ctypes
    vk = _VK.get(action)
    if vk is None:
        return f"Unknown media key '{action}'."
    for _ in range(max(1, min(int(times or 1), 50))):
        ctypes.windll.user32.keybd_event(vk, 0, 1, 0)        # KEYEVENTF_EXTENDEDKEY
        ctypes.windll.user32.keybd_event(vk, 0, 1 | 2, 0)    # + KEYUP
        time.sleep(0.04)
    return f"Pressed {action}" + (f" x{times}" if times and times > 1 else "") + "."


@tool("system_info", "CPU, RAM, disk space, battery and the heaviest running programs.",
      label="Checking the PC")
def system_info(ctx):
    import psutil
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(str(Path.home().anchor or "/"))
    lines = [f"CPU {psutil.cpu_percent(interval=0.5):.0f}%  ({psutil.cpu_count()} threads)",
             f"RAM {vm.used / 2**30:.1f} / {vm.total / 2**30:.1f} GB ({vm.percent:.0f}%)",
             f"Disk {du.free / 2**30:.0f} GB free of {du.total / 2**30:.0f} GB"]
    b = getattr(psutil, "sensors_battery", lambda: None)()
    if b:
        lines.append(f"Battery {b.percent:.0f}% {'charging' if b.power_plugged else ''}")
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((p.info["memory_info"].rss, p.info["name"]))
        except Exception:
            continue
    procs.sort(reverse=True)
    lines.append("Top memory: " + ", ".join(f"{n} {m / 2**20:.0f} MB" for m, n in procs[:8]))
    return "\n".join(lines)


@tool("notify_me", "Send the user a notification: a toast in the app and a Discord DM if the bot is connected. "
      "Use for reminders and results of background routines.", {"text": {"type": "string"}}, ["text"],
      label="Notifying you", modes=("astra", "friend"))
def notify_me(ctx, text):
    services.emit({"type": "notify", "text": text[:300]})
    sent = False
    if services.discord and services.discord.ready():
        try:
            sent = services.discord.dm_owner(text)
        except Exception:
            sent = False
    return "Notified" + (" (app + Discord DM)." if sent else " (app).")


# ---- routines ---------------------------------------------------------------------------------------------------
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_when(when, now=None):
    """Understand a time the way people and models write it. Always returns local time on this PC: time zones in
    the text are converted, missing ones mean local. Never asks for a time zone."""
    now = now or datetime.datetime.now()
    raw = (when or "").strip()
    s = raw.lower()
    if not s or s == "now":
        return now
    # ISO with a zone (2026-10-05T14:00:00+02:00 / ...Z): convert to local, then drop the zone
    iso = re.sub(r"z$", "+00:00", s.replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2} \d", s) else s)
    try:
        d = datetime.datetime.fromisoformat(iso.upper().replace("T", "T"))
        if d.tzinfo:
            d = d.astimezone().replace(tzinfo=None)
        return d
    except ValueError:
        pass
    # zone words: ignore them (the user means their own local time)
    s = re.sub(r"\b(utc|gmt|cet|cest|est|edt|pst|pdt|bst|local time|my time|europe/\w+)([+-]\d{1,2}(:?\d{2})?)?\b", "", s)
    s = re.sub(r"\s+", " ", s).replace(" at ", " ").replace(",", " ").strip()
    m = re.match(r"in\s+(\d+(?:\.\d+)?|an?|one)\s*(s|sec|second|m|min|minute|h|hr|hour|d|day|w|week)s?$", s)
    if m:
        n = 1.0 if m.group(1) in ("a", "an", "one") else float(m.group(1))
        u = m.group(2)[0]
        return now + datetime.timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[u])
    day, explicit_day = now.date(), False
    words = {"tomorrow": 1, "tmr": 1, "tmrw": 1, "today": 0, "tonight": 0, "day after tomorrow": 2}
    for w in sorted(words, key=len, reverse=True):
        if re.search(rf"\b{w}\b", s):
            day = now.date() + datetime.timedelta(days=words[w])
            s = re.sub(rf"\b{w}\b", "", s).strip()
            explicit_day = True
            if w == "tonight" and not re.search(r"\d", s):
                s = "20:00"
            break
    for i, wd in enumerate(_WEEKDAYS):
        m = re.search(rf"\b(next\s+|this\s+)?({wd}|{wd[:3]})\b", s)
        if m:
            ahead = (i - now.weekday()) % 7
            if ahead == 0 or (m.group(1) or "").strip() == "next":
                ahead = ahead or 7
            day = now.date() + datetime.timedelta(days=ahead)
            s = (s[:m.start()] + s[m.end():]).strip()
            explicit_day = True
            break
    # dates: 2026-10-05, 05.10.2026, 5.10., 5/10, oct 5, 5 october
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))); explicit_day = True
        s = (s[:m.start()] + s[m.end():]).strip()
    else:
        m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\.?(?=\s|$)", s)
        if m and not re.match(r"^\d{1,2}[.:]\d{2}$", s[m.start():m.end()]) or (m and m.group(3)):
            y = int(m.group(3)) if m.group(3) else now.year
            y += 2000 if y < 100 else 0
            day = datetime.date(y, int(m.group(2)), int(m.group(1))); explicit_day = True
            if not m.group(3) and day < now.date():
                day = day.replace(year=day.year + 1)
            s = (s[:m.start()] + s[m.end():]).strip()
        else:
            m = (re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3})[a-z]*\b", s)
                 or re.search(r"\b([a-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", s))
            if m:
                a, b = m.group(1), m.group(2)
                mon, dd = (b, a) if a.isdigit() else (a, b)
                if mon in _MONTHS:
                    day = datetime.date(now.year, _MONTHS.index(mon) + 1, int(dd)); explicit_day = True
                    if day < now.date():
                        day = day.replace(year=day.year + 1)
                    s = (s[:m.start()] + s[m.end():]).strip()
    s = s.strip(" .")
    if not s:
        if explicit_day:
            return datetime.datetime.combine(day, datetime.time(9, 0))
        raise ValueError(f"Can't understand the time '{when}'.")
    for word, hm in (("noon", "12:00"), ("midday", "12:00"), ("midnight", "00:00"), ("morning", "09:00"),
                     ("afternoon", "15:00"), ("evening", "19:00"), ("night", "21:00")):
        if re.fullmatch(rf"(in the )?{word}", s):
            s = hm
    m = re.match(r"^(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|uhr|h)?(?:\s.*)?$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        ap = (m.group(3) or "").replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            raise ValueError(f"Can't understand the time '{when}'.")
        d = datetime.datetime.combine(day, datetime.time(h, mi))
        if d <= now and not explicit_day:
            d += datetime.timedelta(days=1)
        return d
    raise ValueError(f"Can't understand the time '{when}'. Use e.g. '2026-10-04 09:00', '18:30', 'tomorrow 9:00', "
                     "'friday 15:00', 'in 20 minutes'. Times are always the user's local time; don't ask about time zones.")


def next_after(ts, repeat):
    """Next run of a routine. repeat: once | hourly | daily | weekdays | weekends | weekly | biweekly | monthly |
    'every 30 minutes' | 'weekly:mon,thu' (specific days). Missed runs (PC was off) are skipped, not piled up."""
    dt = datetime.datetime.fromtimestamp(ts)
    r = (repeat or "once").lower().strip()
    now = datetime.datetime.now()
    m = re.match(r"every\s+(\d+)\s*(m|min|minute|h|hour)s?$", r)
    if m:
        step = datetime.timedelta(minutes=int(m.group(1)) * (60 if m.group(2)[0] == "h" else 1))
    elif r == "hourly":
        step = datetime.timedelta(hours=1)
    elif r == "daily":
        step = datetime.timedelta(days=1)
    elif r == "weekly":
        step = datetime.timedelta(days=7)
    elif r == "biweekly":
        step = datetime.timedelta(days=14)
    elif r in ("weekdays", "weekends") or r.startswith("weekly:"):
        if r == "weekdays":
            ok = {0, 1, 2, 3, 4}
        elif r == "weekends":
            ok = {5, 6}
        else:
            from .agenda import _days_in
            ok = set(_days_in(r.split(":", 1)[1])) or {dt.weekday()}
        nxt = dt + datetime.timedelta(days=1)
        while nxt.weekday() not in ok or nxt <= now:
            nxt += datetime.timedelta(days=1)
        return nxt.timestamp()
    elif r == "monthly":
        nxt = dt
        while nxt <= now:
            y, mo = divmod(nxt.month, 12)
            try:
                nxt = nxt.replace(year=nxt.year + y, month=mo + 1)
            except ValueError:
                nxt = nxt.replace(year=nxt.year + y, month=mo + 1, day=28)
        return nxt.timestamp()
    else:
        return None
    nxt = dt + step
    while nxt <= now:
        nxt += step
    return nxt.timestamp()


@tool("schedule_task", "Schedule something for later or on repeat: a reminder or any task Astra should do by itself "
      "(e.g. 'every weekday 9:00 summarize my unread emails', 'in 20 min remind me to start the stream'). prompt is "
      "the instruction Astra will run then; for plain reminders write 'Remind the user: ...'. repeat: once | hourly | "
      "daily | weekdays | weekends | weekly | biweekly | monthly | 'every 30 minutes' | 'weekly:mon,thu' for specific "
      "days. For 'every tuesday 9:00' use when='tuesday 9:00', repeat='weekly': it starts on the next tuesday by "
      "itself, never ask which date.",
      {"title": {"type": "string"}, "prompt": {"type": "string"}, "when": {"type": "string"},
       "repeat": {"type": "string"}}, ["title", "prompt", "when"], label="Scheduling")
def schedule_task(ctx, title, prompt, when, repeat="once"):
    from .agenda import infer_repeat
    rep, days, rest = infer_repeat(when, "" if (repeat or "once") == "once" else repeat)
    if rep and (repeat or "once") == "once":
        repeat = rep
    if days and (repeat or "").startswith("weekly") and len(days) > 1:
        repeat = "weekly:" + ",".join(["mon", "tue", "wed", "thu", "fri", "sat", "sun"][d] for d in days)
    dt = parse_when(rest or when) if rep else parse_when(when)
    if days:   # put it on the first of the days that were said, today included if the time hasn't passed
        now = datetime.datetime.now()
        for k in range(8):
            c = (now + datetime.timedelta(days=k)).replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
            if c.weekday() in days and c > now:
                dt = c
                break
    rid = services.db.x("INSERT INTO routines (title, prompt, next_run, repeat, enabled, created) VALUES (?,?,?,?,1,?)",
                        (title[:120], prompt, dt.timestamp(), repeat or "once", time.time()))
    services.emit({"type": "routines_changed"})
    return f"Scheduled #{rid} '{title}' for {dt:%a %d %b %H:%M}" + (f", repeating {repeat}" if repeat and repeat != "once" else "") + "."


@tool("list_routines", "List scheduled routines and reminders.", label="Listing routines")
def list_routines(ctx):
    rows = services.db.q("SELECT * FROM routines ORDER BY enabled DESC, next_run")
    if not rows:
        return "No routines."
    return "\n".join(f"#{r['id']} {'ON ' if r['enabled'] else 'OFF'} {r['title']} - next "
                     f"{datetime.datetime.fromtimestamp(r['next_run']):%a %d %b %H:%M}, {r['repeat']}" for r in rows)


@tool("cancel_routine", "Delete a scheduled routine by its #id.", {"id": {"type": "integer"}}, ["id"],
      label="Cancelling routine")
def cancel_routine(ctx, id):  # noqa: A002
    services.db.x("DELETE FROM routines WHERE id=?", (int(id),))
    services.emit({"type": "routines_changed"})
    return f"Routine #{id} deleted."


# ---- emergency stop hotkey (Ctrl+Alt+X) -------------------------------------------------------------------------
def start_stop_hotkey():
    if not IS_WIN or not services.config.get("control", "stop_hotkey"):
        return

    def loop():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 0xA57, 0x0001 | 0x0002 | 0x4000, 0x58):  # ALT|CTRL|NOREPEAT, 'X'
            services.db.log("hotkey", "Ctrl+Alt+X is taken by another app; use the Stop button instead.")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                services.panic.set()
                services.stop_all()
                services.emit({"type": "panic"})
    threading.Thread(target=loop, daemon=True, name="stop-hotkey").start()


# ---- file index (filename awareness across your drives) -------------------------------------------------------
_index_lock = threading.Lock()


def index_roots():
    if services.config.get("control", "full_disk") and IS_WIN:
        import ctypes
        import string
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        roots = []
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 3:  # fixed drives
                roots.append(f"{letter}:\\")
        return roots
    return [str(Path.home())]


def build_index(force=False):
    """Walk the drives in the background (filenames only, no contents). Refreshes once a day."""
    if not _index_lock.acquire(blocking=False):
        return
    try:
        last = services.db.q("SELECT v FROM kv WHERE k='files_indexed'")
        if not force and last and time.time() - float(last[0]["v"]) < 86400:
            return
        services.db.x("DELETE FROM files")
        batch, n = [], 0
        for root in index_roots():
            for dirpath, dirnames, filenames in os.walk(root):
                low = dirpath.lower()
                dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")
                               and not _private(os.path.join(low, d.lower()))]
                if _private(low):
                    continue
                for f in filenames:
                    full = os.path.join(dirpath, f)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    batch.append((full, f.lower(), dirpath, st.st_size, st.st_mtime))
                    n += 1
                if len(batch) >= 5000:
                    with services.db.lock:
                        services.db.conn.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", batch)
                        services.db.conn.commit()
                    batch = []
                    time.sleep(0.05)  # stay gentle on the disk
                if n > 3_000_000:
                    break
        if batch:
            with services.db.lock:
                services.db.conn.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", batch)
                services.db.conn.commit()
        services.db.x("INSERT OR REPLACE INTO kv VALUES ('files_indexed', ?)", (str(time.time()),))
        services.db.log("files", f"Indexed {n} files")
    finally:
        _index_lock.release()


@tool("search_files", "Find files anywhere Astra may look (your user folder, or every drive with full disk access) "
      "by name words, e.g. 'resume pdf', 'mix final wav', 'cover psd'. kind: optional extension filter like 'wav,flac'.",
      {"query": {"type": "string"}, "kind": {"type": "string"}, "limit": {"type": "integer"}}, ["query"],
      label="Searching files")
def search_files(ctx, query, kind="", limit=40):
    words = [w.lower() for w in re.split(r"\s+", query.strip()) if w]
    exts = [e.strip().lstrip(".").lower() for e in kind.split(",") if e.strip()]
    if not services.db.q("SELECT 1 FROM files LIMIT 1"):
        threading.Thread(target=build_index, kwargs={"force": True}, daemon=True).start()
        return "The file index is being built for the first time (a few minutes). Try again shortly, or use list_files."
    sql = "SELECT path, size, mtime FROM files WHERE " + " AND ".join(["(name LIKE ? OR folder LIKE ?)"] * len(words))
    args = []
    for w in words:
        args += [f"%{w}%", f"%{w}%"]
    if exts:
        sql += " AND (" + " OR ".join(["name LIKE ?"] * len(exts)) + ")"
        args += [f"%.{e}" for e in exts]
    sql += " ORDER BY mtime DESC LIMIT ?"
    rows = services.db.q(sql, args + [int(limit or 40)])
    rows = [r for r in rows if _allowed(r["path"])]
    if not rows:
        return f"No files match '{query}'."
    return "\n".join(f"{r['path']}  ({r['size'] // 1024} KB, {datetime.datetime.fromtimestamp(r['mtime']):%Y-%m-%d})"
                     for r in rows)


@tool("disk_overview", "Drives with free/used space, and the size of the file index.", label="Checking disks")
def disk_overview(ctx):
    import psutil
    out = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        out.append(f"{part.mountpoint} {u.free / 2**30:.0f} GB free of {u.total / 2**30:.0f} GB ({part.fstype})")
    n = services.db.q("SELECT COUNT(*) AS n FROM files")[0]["n"]
    access = "every drive" if services.config.get("control", "full_disk") else "your user folder only"
    return "\n".join(out) + f"\nFile index: {n} files ({access})."
