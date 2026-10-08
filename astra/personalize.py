"""AstraNova grows with you.

Every few hours (only when you've written enough since last time) AstraNova reads how you've been talking and:
- writes short style notes for Nova: how you text, what kind of replies land, what to do more or less of.
  Nova follows them on top of her personality, so she slowly becomes the friend that fits you.
- invents moods of its own when your chats have a feeling the built-in twenty don't cover (say "deadline
  goblin mode" or "cozy gaming night"): each gets a name, three colours and the words that bring it on, and from
  then on the app's colours and the Horizon light follow it like any other mood.

Only communication style and feelings are learned here. Facts about your life stay in Memory, where you can see
and delete them. Settings > Nova and Settings > Appearance show what was learned and let you reset it."""
import json
import re
import time

from . import services

SCHEMA = {
    "type": "object",
    "properties": {
        "style": {"type": "string"},
        "moods": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "description": {"type": "string"},
            "words": {"type": "array", "items": {"type": "string"}},
            "colors": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
            "required": ["name", "words", "colors"]}},
    },
    "required": ["style", "moods"],
}
BUILT_IN = {"neutral", "happy", "excited", "playful", "calm", "cozy", "sad", "lonely", "tired", "anxious", "stressed",
            "angry", "focused", "creative", "romantic", "dreamy", "nostalgic", "grateful", "hopeful", "confident",
            "bored"}
MAX_MOODS = 8


def cfg():
    p = services.config.get("personal") or {}
    return {"adapt": p.get("adapt", True) is not False, "learn_moods": p.get("learn_moods", True) is not False,
            "style": p.get("learned_style") or "", "moods": p.get("custom_moods") or [], "last": p.get("last_run", 0),
            "seen": p.get("seen_count", 0)}


def _user_messages():
    """What the user wrote lately, to Nova and to Astra (their own words only)."""
    out = []
    rows = services.db.q("SELECT id FROM conversations ORDER BY updated DESC LIMIT 12")
    for r in rows:
        for m in services.db.get_messages(r["id"])[-60:]:
            if m.get("role") == "user" and not m.get("internal") and m.get("content"):
                out.append((m.get("ts") or 0, str(m["content"])[:400]))
    out.sort()
    return [t for _, t in out[-120:]]


def _clean_mood(m, taken):
    name = re.sub(r"[^a-z]", "", str(m.get("name") or "").lower())[:16]
    if len(name) < 3 or name in taken:
        return None
    words = [w.strip().lower() for w in (m.get("words") or []) if isinstance(w, str) and 2 < len(w.strip()) < 30][:12]
    cols = [c for c in (m.get("colors") or []) if isinstance(c, list) and len(c) == 3][:3]
    if len(words) < 3 or len(cols) < 3:
        return None
    cols = [[max(0, min(255, int(v))) for v in c] for c in cols]
    return {"name": name, "description": str(m.get("description") or "")[:120], "words": words, "colors": cols,
            "made": time.time()}


def run(force=False, emit=None):
    c = cfg()
    if not (c["adapt"] or c["learn_moods"]):
        return {"ok": False, "why": "off"}
    said = _user_messages()
    if not force and (len(said) < 15 or len(said) - c["seen"] < 15 and time.time() - c["last"] < 3 * 86400):
        return {"ok": False, "why": "not enough new messages yet"}
    if not said:
        return {"ok": False, "why": "no messages yet"}
    fname = services.config.get("friend", "name") or "Nova"
    taken = BUILT_IN | {m["name"] for m in c["moods"]}
    r = services.llm.json_task(
        f"You study how a person talks in their chats with an AI app, so the app can adapt. Two outputs.\n"
        f"1) style: notes for {fname}, their AI friend, on how to talk with THIS person so it feels natural to them: "
        "their texting style (length, casing, slang, emoji use, language mix), humour, what kind of replies they "
        "respond well to, topics they light up about, what to do less of (e.g. too many questions). Max 8 short "
        "lines, written as instructions to the friend ('keep it short, they text in bursts'). Communication style "
        "only: no personal facts, names, places or health details. Strictly platonic, never flirty. "
        f"{'Refine these existing notes rather than starting over: ' + c['style'][:900] if c['style'] else ''}\n"
        "2) moods: 0 to 2 NEW moods that keep coming up in their messages and that none of these existing moods "
        f"covers: {', '.join(sorted(taken))}. Each: a one-word lowercase name (letters only), a short description, "
        "6 to 12 words or short phrases they actually use that signal it, and 3 RGB colours that feel like it "
        "(a main colour, a secondary, a highlight). Return an empty list if nothing new stands out.",
        "THEIR RECENT MESSAGES (oldest first):\n" + "\n".join(f"- {s}" for s in said), SCHEMA)
    new_style = str(r.get("style") or "").strip()[:900]
    added = []
    if c["learn_moods"]:
        for m in r.get("moods") or []:
            cm = _clean_mood(m, taken)
            if cm:
                added.append(cm)
                taken.add(cm["name"])
    moods = (c["moods"] + added)[-MAX_MOODS:]
    upd = {"last_run": time.time(), "seen_count": len(said), "custom_moods": moods}
    if c["adapt"] and new_style:
        upd["learned_style"] = new_style
    services.config.update("personal", upd)
    reload_moods()
    if emit and added:
        emit({"type": "notify", "text": "New mood learned: " + ", ".join(m["name"] for m in added)})
    if emit:
        emit({"type": "custom_moods", "moods": moods})
    return {"ok": True, "style": upd.get("learned_style", c["style"]), "added": [m["name"] for m in added],
            "moods": moods}


def reload_moods():
    """Put the learned moods into mood detection next to the built-in ones."""
    from . import agent
    for k in [k for k in agent.MOODS if k.startswith("~")]:
        agent.MOODS.pop(k, None)
    for m in cfg()["moods"]:
        words = sorted({w for w in m["words"]}, key=len, reverse=True)
        if words:
            agent.MOODS["~" + m["name"]] = r"\b(" + "|".join(re.escape(w) for w in words) + r")\b"


def style_block():
    c = cfg()
    if not (c["adapt"] and c["style"]):
        return ""
    name = services.config.get("profile", "name") or "the user"
    return f"\nHOW {name.upper()} LIKES TO TALK (learned from your chats, follow it on top of who you are):\n{c['style']}\n"


def forget(what):
    if what == "style":
        services.config.update("personal", {"learned_style": ""})
    elif what == "moods":
        services.config.update("personal", {"custom_moods": []})
    else:
        services.config.update("personal", {"custom_moods": [m for m in cfg()["moods"] if m["name"] != what]})
    reload_moods()
    return cfg()


def public_moods():
    return json.loads(json.dumps(cfg()["moods"]))
