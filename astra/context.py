# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Personal context: a markdown document about you (imported from ChatGPT etc, edited by you or by Astra),
plus the day journal used for "tell sam what happened to me today"."""
import datetime
import json
import re
import threading

from . import services
from .paths import personal_context_path
from .text import no_dashes

_lock = threading.Lock()


def load():
    p = personal_context_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def save(text):
    with _lock:
        personal_context_path().write_text(no_dashes(text or "").strip() + "\n", encoding="utf-8")
    services.emit({"type": "context_changed"})


def sections(text=None):
    """{heading: body} for '## Heading' sections. Text before the first heading goes under ''."""
    text = load() if text is None else text
    out, cur = {}, ""
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1).strip()
            out.setdefault(cur, [])
            continue
        out.setdefault(cur, []).append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def update_section(section, content, mode="replace"):
    secs = sections()
    key = next((k for k in secs if k.lower() == section.lower().strip()), section.strip())
    if mode == "append" and secs.get(key):
        secs[key] = secs[key].rstrip() + "\n" + content.strip()
    elif mode == "delete":
        secs.pop(key, None)
    else:
        secs[key] = content.strip()
    body = secs.pop("", "")
    doc = (body + "\n\n" if body else "") + "\n\n".join(f"## {k}\n{v}" for k, v in secs.items())
    save(doc)
    return key


def for_prompt(limit):
    t = load().strip()
    if not t:
        return ""
    if len(t) > limit:
        t = t[:limit] + "\n...(more in personal context; use read_personal_context)"
    return "PERSONAL CONTEXT (written by/for the user; private):\n" + t


# ---- importing -------------------------------------------------------------------------------------
SUMMARY_SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
                  "required": ["facts"]}


def _chatgpt_user_text(data, max_chars=60000):
    """Pull the user's own messages (newest first) out of a ChatGPT conversations.json export."""
    convs = data if isinstance(data, list) else data.get("conversations", [])
    convs = sorted(convs, key=lambda c: c.get("update_time") or c.get("create_time") or 0, reverse=True)
    chunks, total = [], 0
    for c in convs:
        for node in (c.get("mapping") or {}).values():
            msg = (node or {}).get("message") or {}
            if (msg.get("author") or {}).get("role") != "user":
                continue
            parts = (msg.get("content") or {}).get("parts") or []
            t = " ".join(p for p in parts if isinstance(p, str)).strip()
            if len(t) > 15:
                chunks.append(t[:800])
                total += len(t[:800])
        if total > max_chars:
            break
    return "\n---\n".join(chunks)[:max_chars]


def import_text(raw, filename=""):
    """Import a ChatGPT/Claude/Gemini export or any notes. Returns a short report."""
    raw = raw or ""
    name = (filename or "").lower()
    source = "notes"
    material = raw
    try:
        data = json.loads(raw)
        if isinstance(data, (list, dict)) and ("mapping" in json.dumps(data)[:200000]):
            material = _chatgpt_user_text(data)
            source = "ChatGPT export"
        elif isinstance(data, dict) or isinstance(data, list):
            material = json.dumps(data, ensure_ascii=False)[:60000]
            source = "JSON export"
    except ValueError:
        if "chatgpt" in name:
            source = "ChatGPT"
    if not material.strip():
        return "Nothing usable found in that file."
    if source == "notes" and len(material) < 6000:
        update_section("Imported notes", material.strip(), mode="append")
        return "Added your notes to personal context."
    facts = []
    for i in range(0, min(len(material), 60000), 15000):
        try:
            r = services.llm.json_task(
                "Extract lasting personal facts about the USER from their messages: identity, work/studies, "
                "projects, tastes (music, art, games), people in their life, habits, goals, preferences for how "
                "assistants should talk to them. Short third-person facts. No secrets/passwords. No dashes.",
                material[i:i + 15000], SUMMARY_SCHEMA)
            facts += [f for f in r.get("facts", []) if isinstance(f, str)]
        except Exception:  # noqa: BLE001
            continue
    seen, uniq = set(), []
    for f in facts:
        k = f.lower().strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(no_dashes(f.strip()))
    if not uniq:
        return "Couldn't extract facts (is the model running?)."
    update_section(f"From {source}", "\n".join(f"- {f}" for f in uniq[:120]), mode="append")
    return f"Imported {len(uniq[:120])} facts from {source}. Review them in Settings > Personal context."


# ---- day journal -------------------------------------------------------------------------------------
def day_bounds(day="today"):
    today = datetime.date.today()
    d = (day or "today").strip().lower()
    if d in ("today", ""):
        date = today
    elif d == "yesterday":
        date = today - datetime.timedelta(days=1)
    else:
        date = datetime.date.fromisoformat(d[:10])
    start = datetime.datetime.combine(date, datetime.time())
    return date, start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()


def journal(day="today", limit=80):
    """What the user said and what was remembered on a given day, in order."""
    date, t0, t1 = day_bounds(day)
    items = []
    for m in services.db.q("SELECT text, created FROM memories WHERE created>=? AND created<?", (t0, t1)):
        items.append((m["created"], f"[memory] {m['text']}"))
    rows = services.db.q("SELECT id, kind, messages FROM conversations WHERE updated>=? AND kind IN ('astra','friend')", (t0,))
    for r in rows:
        for msg in json.loads(r["messages"] or "[]"):
            ts = msg.get("ts")
            if msg.get("role") == "user" and ts and t0 <= ts < t1:
                txt = msg.get("content", "")
                if txt.startswith("["):
                    continue
                items.append((ts, f"[{'to friend' if r['kind'] == 'friend' else 'to Astra'}] {txt[:600]}"))
    items.sort()
    return date, [f"{datetime.datetime.fromtimestamp(t):%H:%M} {s}" for t, s in items[-limit:]]
