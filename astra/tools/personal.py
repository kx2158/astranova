"""You: profile, memory, questions/approvals, saved accounts, and the Friend's hand-off to Astra."""
import json
import re
import secrets
import string
import time

from .. import services
from . import tool


@tool("get_user_profile", "Get the user's details (name, email, phone, city, about) for forms and messages.",
      label="Reading your profile")
def get_user_profile(ctx):
    p = services.config.get("profile")
    return {k: v for k, v in p.items() if v} or "Profile is empty - the user can fill Settings > Profile."


ANSWERED_SCHEMA = {"type": "object", "properties": {"answered": {"type": "boolean"}, "answer": {"type": "string"}},
                   "required": ["answered"]}


def _already_answered(ctx, question):
    """Most questions an assistant asks are already answered somewhere in what the user wrote. Check first."""
    conv = getattr(ctx, "conv_id", "") or ""
    hist = services.db.get_messages(conv) if conv else []
    if conv.startswith("nova-task-"):
        hist = services.db.get_messages("friend")[-30:] + hist
    said = [m.get("content", "") for m in hist if m.get("role") == "user" and m.get("content")
            and not m.get("internal")][-12:]
    task = getattr(ctx, "brief", {}) or {}
    if not said and not task:
        return None
    try:
        r = services.llm.json_task(
            "An assistant is about to ask the user a question. Decide whether the user's messages already answer "
            "it, directly or clearly enough to act on (names, times, places, choices, preferences). If yes, give "
            "the answer in a short sentence taken from what they said. If it's genuinely not there, answered=false.",
            "USER'S MESSAGES:\n" + "\n---\n".join(s[:1500] for s in said) +
            (f"\n\nTASK: {json.dumps(task, ensure_ascii=False)[:1500]}" if task else "") + f"\n\nQUESTION: {question}",
            ANSWERED_SCHEMA)
    except Exception:  # noqa: BLE001
        return None
    ans = (r.get("answer") or "").strip()
    return ans if r.get("answered") and ans else None


@tool("ask_user", "LAST RESORT. Ask the user a question and wait. Only when something essential is truly missing "
      "from everything they said (e.g. who to send a message to) and no reasonable default exists. Small details: "
      "pick a sensible default and mention it at the end instead of asking.", {"question": {"type": "string"}}, ["question"],
      label="Asking you")
def ask_user(ctx, question):
    known = _already_answered(ctx, question)
    if known:
        return (f"Don't ask: the user already told you. From their messages: {known}\n"
                "Continue the task with this. Only if it's really unclear, make the most reasonable choice and say "
                "what you assumed at the end.")
    ans = services.bridge.ask(question)
    return ans if ans else ("No answer from the user (timed out or cancelled). Make the most reasonable choice and "
                            "continue; say what you assumed at the end.")


@tool("request_approval", "Ask the user to approve an important action BEFORE doing it: posting publicly, buying, "
      "deleting, sending personal data, submitting forms, sending a message in an app without a send_message recipe. "
      "Describe exactly what will happen.",
      {"action": {"type": "string"}, "details": {"type": "string"}}, ["action", "details"], label="Requesting approval")
def request_approval(ctx, action, details):
    ok = services.bridge.confirm(action, details)
    return "APPROVED - go ahead." if ok else "DECLINED - do not do it. Ask the user what to change."


@tool("remember", "Save a fact about the user to long-term memory (preferences, people, projects, plans, tastes). "
      "One short fact per call, written in third person. keep = how long it stays useful: day (tonight's game, "
      "today's mood), week, month (the show they're into), season (semester stuff), year, until (tied to a date, "
      "give until=YYYY-MM-DD), forever (people, studies, identity, tastes).",
      {"fact": {"type": "string"}, "keep": {"type": "string", "enum": ["day", "week", "month", "season", "year",
                                                                       "until", "forever"]},
       "until": {"type": "string"}}, ["fact"], label="Remembering", modes=("astra", "friend"))
def remember(ctx, fact, keep="", until=None):
    mid = services.db.remember(fact, source=ctx.mode, keep=keep or "", until=until)
    services.emit({"type": "memory_changed"})
    return f"Remembered (#{mid})."


@tool("recall", "Search long-term memory about the user.", {"query": {"type": "string"}}, ["query"],
      label="Recalling", modes=("astra", "friend"))
def recall(ctx, query):
    rows = services.db.recall(query, 15)
    return "\n".join(f"#{r['id']} {r['text']}" for r in rows) or "Nothing remembered about that."


@tool("forget", "Delete a memory by #id (from recall) when it's wrong or the user asks.", {"id": {"type": "integer"}},
      ["id"], label="Forgetting", modes=("astra", "friend"))
def forget(ctx, id):  # noqa: A002
    services.db.forget(int(id))
    services.emit({"type": "memory_changed"})
    return f"Forgot #{id}."


@tool("generate_password", "Generate a strong random password for a new account.", {"length": {"type": "integer"}},
      label="Generating password")
def generate_password(ctx, length=18):
    alphabet = string.ascii_letters + string.digits + "!#%+-_?"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(max(12, int(length or 18))))
        if any(c.isdigit() for c in pw) and any(c in "!#%+-_?" for c in pw) and any(c.isupper() for c in pw):
            return pw


@tool("save_account", "Save login details for a website account (Windows Credential Manager).",
      {"site": {"type": "string"}, "username": {"type": "string"}, "password": {"type": "string"},
       "notes": {"type": "string"}}, ["site", "username", "password"], label="Saving account")
def save_account(ctx, site, username, password, notes=""):
    services.config.set_site_password(site.lower(), username, password)
    services.db.x("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)", (site.lower(), username, notes, time.time()))
    services.db.log("account", f"Saved account for {site} ({username})")
    return "Saved."


@tool("get_account", "Get saved login details for a website.", {"site": {"type": "string"}}, ["site"],
      label="Loading account")
def get_account(ctx, site):
    rows = services.db.q("SELECT site, username, notes FROM accounts WHERE site LIKE ?", (f"%{site.lower()}%",))
    if not rows:
        return "No saved account for that site."
    for r in rows:
        r["password"] = services.config.get_site_password(r["site"], r["username"])
    return rows


# set by app: fn(task_text, title) -> task id
handoff = None


@tool("hand_to_astra", "Give a job to Astra (the PC/web half of AstraNova): looking things up properly / research / "
      "finding lists of things, sending messages, email, music playback, opening apps, files, calendar. Describe the task clearly and completely, with every detail the user gave. "
      "Astra works inside this chat and her result comes back to you.",
      {"task": {"type": "string"}}, ["task"], label="Handing to Astra", modes=("friend",))
def hand_to_astra(ctx, task):
    if not handoff:
        return "Astra isn't available right now."
    ctx.handed_task = handoff(task, task.split("\n")[0][:60])
    return ("Astra started on it (she works in this chat, the user can watch). You'll get her result as an "
            "[Astra finished] message. Tell the user casually that she's on it, don't make up results.")


# ---- personal context + day journal ----------------------------------------------------------------------
@tool("read_personal_context", "Read the user's personal context document (who they are, projects, people, "
      "preferences). Optional section name.", {"section": {"type": "string"}}, label="Reading your context",
      modes=("astra", "friend"))
def read_personal_context(ctx, section=""):
    from .. import context as pctx
    if section:
        secs = pctx.sections()
        hit = next((v for k, v in secs.items() if k.lower() == section.lower()), None)
        return hit if hit is not None else "No such section. Sections: " + ", ".join(k for k in secs if k)
    return pctx.load() or "Personal context is empty."


@tool("update_personal_context", "Edit the user's personal context when something changed or they ask: replace, "
      "append to, or delete a '## section' (e.g. 'Work', 'Music', 'People', 'Studies').",
      {"section": {"type": "string"}, "content": {"type": "string"},
       "mode": {"type": "string", "enum": ["replace", "append", "delete"]}}, ["section"],
      label="Updating your context", modes=("astra", "friend"))
def update_personal_context(ctx, section, content="", mode="replace"):
    from .. import context as pctx
    key = pctx.update_section(section, content, mode)
    services.db.log("context", f"{mode} section '{key}'")
    return f"Personal context section '{key}' {mode}d."


# things that are about using the app, not about the user's life: left out of day recaps for other people
_CHORE = re.compile(
    r"\b(add(ed)? (an? )?(event|reminder)|calendar|remind me|set (a |an |up )?(timer|alarm|reminder|event|routine)|"
    r"schedule|routine|settings?|time ?zone|astra|nova|astranova|the app|update|install|download|build\.bat|"
    r"play(ing)? (a |some )?(game|valorant|minecraft|league|fortnite|genshin)|launch(ed)? |open (the )?\w+ app|"
    r"spotify|playlist|skip|pause|volume|screenshot|search (for|up)|google|look up|find me|summari[sz]e|"
    r"translate|email|inbox|password|account|login|log in|test|debug|tell \w+ about my day|what happened to me)\b",
    re.I)
TOPICAL_SCHEMA = {"type": "object", "properties": {"keep": {"type": "array", "items": {"type": "integer"}}},
                  "required": ["keep"]}


def topical_day(lines):
    """Only what's worth telling a friend: experiences, feelings, people, news, plans, wins and fails.
    Not app chores (events added, reminders, settings), gaming sessions or look-ups."""
    rough = [ln for ln in lines if not _CHORE.search(ln.split(" ", 1)[-1])]
    if len(rough) <= 1:
        return rough
    try:
        numbered = "\n".join(f"{i}. {ln}" for i, ln in enumerate(rough))
        r = services.llm.json_task(
            "These are notes from someone's day. Pick only the ones worth telling a close friend about: real "
            "experiences, things that happened to them, how they felt, people they saw or talked to, news in their "
            "life, plans, wins and annoyances. Leave out anything about using apps or assistants (setting up events, "
            "reminders, settings, looking things up), playing video games, music playback and small talk. "
            "Return the numbers to keep.", numbered, TOPICAL_SCHEMA)
        keep = [rough[i] for i in r.get("keep", []) if isinstance(i, int) and 0 <= i < len(rough)]
        return keep
    except Exception:  # noqa: BLE001
        return rough


@tool("recall_day", "What happened in the user's day that's worth telling a friend (only the topical things: what "
      "they experienced, felt, who they saw, news, plans; not app chores, calendar setup or gaming). day: today | "
      "yesterday | YYYY-MM-DD. Use before writing someone a recap of the user's day. all=true shows everything.",
      {"day": {"type": "string"}, "all": {"type": "boolean"}}, label="Remembering your day", modes=("astra", "friend"))
def recall_day(ctx, day="today", all=False):  # noqa: A002
    from .. import context as pctx
    date, lines = pctx.journal(day)
    if not all:
        lines = topical_day(lines)
    head = f"{date:%A %d %B}:\n"
    if not lines:
        return head + "Nothing worth telling from that day. Ask the user what they'd like to share."
    return head + "\n".join(lines) + ("\n\nWhen writing the recap: only these topics, in the user's own voice, short. "
                                      "Don't mention apps, calendar events being set up, assistants or gaming."
                                      if not all else "")
