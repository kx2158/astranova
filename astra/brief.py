# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Task brief: before acting on a real task, Astra writes down what you actually want (goal, apps, done-when,
plan). The brief is pinned into every model step so long multi-app tasks stay on track. Small talk and quick
questions skip it (no extra model call)."""
import re

from . import playbooks, services

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "task_type": {"type": "string", "enum": ["chat", "question", "pc_control", "message", "music", "creative_app",
                                                  "web", "email", "files", "routine", "other"]},
        "apps": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "deliverable": {"type": "string"},
        "plan": {"type": "array", "items": {"type": "string"}},
        "reply_language": {"type": "string"},
    },
    "required": ["goal", "task_type", "apps", "deliverable", "plan", "reply_language"],
}

SYSTEM = """You turn a user's request to their PC assistant into a precise task brief. Output JSON only.
- goal: one clear English sentence of what the user wants done.
- task_type: chat | question | pc_control | message (sending/reading chats) | music (playback) | web | email | files | routine (do something later/regularly) | other.
- apps: apps involved, lowercase short names (discord, spotify, apple music, whatsapp,
  telegram, slack, browser, explorer, obs, or any other app name). Empty if none.
- constraints: every specific wish (recipient, wording, tempo, key, style, count...), short phrases.
- deliverable: what the user expects at the end.
- plan: 2-6 short concrete steps.
- reply_language: language name the user wrote in (e.g. English).
If a PREVIOUS BRIEF is given and the message is a follow-up, keep its details unless changed."""

_TASK_WORDS = re.compile(
    r"\b(open|send|message|dm|text|write|make|create|build|add|play|pause|skip|playlist|track|song|beat|melody|chord|"
    r"render|model|scene|export|import|edit|resize|crop|download|install|find|search|look up|book|schedule|remind|"
    r"every|tomorrow|tonight|email|mail|click|type|close|launch|start|stop|record|save|delete|move|copy|rename|"
    r"organi[sz]e|clean|set|turn|change|check|read|reply|post|upload)\b", re.I)


def needs_brief(text, prev):
    t = (text or "").strip()
    if prev and len(t) < 40 and not _TASK_WORDS.search(t):
        return False  # short follow-up like "thanks" / "and the other one?"
    return bool(_TASK_WORDS.search(t)) or len(t) > 160 or bool(playbooks.detect(t))


def make_brief(user_text, prev=None):
    import json
    prompt = ""
    if prev:
        prompt += "PREVIOUS BRIEF (this message may be a follow-up):\n" + json.dumps(prev, ensure_ascii=False) + "\n\n"
    prompt += "USER MESSAGE:\n" + user_text
    try:
        b = services.llm.json_task(SYSTEM, prompt, BRIEF_SCHEMA)
    except Exception:  # noqa: BLE001
        b = dict(prev or {})
        b.update({"goal": user_text[:200], "task_type": (prev or {}).get("task_type", "other"),
                  "deliverable": "", "plan": [], "reply_language": "the user's language"})
    b = {k: v for k, v in (b or {}).items() if v not in (None, "", [], 0)}
    b["apps"] = list(dict.fromkeys((b.get("apps") or []) + playbooks.detect(user_text)))
    return b


def render(b, steps):
    if not b:
        return ""
    lines = ["CURRENT TASK BRIEF (re-read before every step; stay on this task):", f"- Goal: {b.get('goal', '')}"]
    if b.get("apps"):
        lines.append("- Apps: " + ", ".join(b["apps"]))
    if b.get("constraints"):
        lines.append("- Must respect: " + "; ".join(b["constraints"]))
    if b.get("deliverable"):
        lines.append(f"- Done when: {b['deliverable']}")
    if b.get("plan"):
        lines.append("- Plan: " + " -> ".join(b["plan"]))
    if b.get("reply_language"):
        lines.append(f"- Reply in: {b['reply_language']}")
    if steps:
        lines.append("PROGRESS SO FAR (do not repeat finished steps):")
        lines += [f"  {i + 1}. {s}" for i, s in enumerate(steps[-15:])]
    return "\n".join(lines)


def summary_for_ui(b):
    tags = [playbooks.LABELS.get(k, k) for k in playbooks.keys_for(b.get("apps", [])) if k not in playbooks.HIDDEN]
    tags += [a for a in b.get("apps", []) if not playbooks.keys_for([a])][:3]
    tags += b.get("constraints", [])[:3]
    return {"goal": b.get("goal", ""), "tags": tags, "plan": b.get("plan", [])}
