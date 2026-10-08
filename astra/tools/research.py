"""Research with notes: search several queries, read the best pages (plain HTTP first, the hidden browser when a
page needs it), pull the important facts out of each page into structured notes, and keep a running list.

The notes show up live in the chat as a card, are saved in the database for the conversation, and are written to
Documents/AstraNova/Research/<date> <topic>.md so you can open them later."""
import datetime
import json
import re
import time
import urllib.parse

from .. import services
from ..paths import workspace_sub
from . import tool

SKIP_HOSTS = re.compile(r"(^|\.)(youtube\.com|youtu\.be|facebook\.com|instagram\.com|tiktok\.com|pinterest\.|"
                        r"twitter\.com|x\.com|reddit\.com/login|linkedin\.com|amazon\.)", re.I)


def _key(s):
    k = re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")
    return k[:40] or "info"


def _emit(ctx, **e):
    services.emit({"type": "notes", "conv": ctx.conv_id, **e})


def _host(u):
    try:
        return urllib.parse.urlparse(u).hostname.replace("www.", "")
    except Exception:  # noqa: BLE001
        return u


def _queries(question):
    year = datetime.date.today().year
    try:
        r = services.llm.json_task(
            "You write web search queries. Output JSON only.",
            f"Today is {datetime.date.today():%d %B %Y}. Write 3 different short search queries (like a person types "
            f"into Google) that together find the best, most current sources for:\n{question}\n"
            f"Add the year ({year}) where freshness matters.",
            {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
             "required": ["queries"]})
        qs = [q.strip() for q in r.get("queries", []) if q and q.strip()]
    except Exception:  # noqa: BLE001
        qs = []
    return list(dict.fromkeys(qs + [question]))[:4]


def _read(ctx, url):
    from .web import fetch_text, looks_blocked
    try:
        title, final, text, links = fetch_text(url, max_chars=12000, with_links=True)
    except Exception:  # noqa: BLE001
        title, final, text, links = "", url, "", []
    if looks_blocked(text) and services.browser is not None:
        try:
            from .browser import browser_read
            snap = browser_read(ctx, url)
            m = re.search(r"TITLE: (.*)", snap)
            body = snap.split("PAGE TEXT:\n", 1)[-1].split("\n\nINTERACTIVE ELEMENTS", 1)[0]
            return (m.group(1) if m else title), url, body, links
        except Exception:  # noqa: BLE001
            pass
    return title, final, text, links


def _extract(question, fields, title, url, text, links):
    props = {"name": {"type": "string"}, "link": {"type": "string"}}
    for f in fields:
        props[_key(f)] = {"type": "string"}
    props["notes"] = {"type": "string"}
    schema = {"type": "object", "properties": {
        "relevant": {"type": "boolean"},
        "items": {"type": "array", "items": {"type": "object", "properties": props, "required": ["name"]}}},
        "required": ["relevant", "items"]}
    link_lines = "\n".join(f"- {t} -> {h}" for t, h in links[:60])
    sys = ("You take research notes. From ONE web page, extract every distinct thing that answers the question "
           "(e.g. each open call, event, product, place, job). Copy facts exactly as written on the page - dates, "
           "prices, fees, requirements. Never guess; leave a field empty if the page doesn't say. 'link' = the most "
           "specific URL for that item (from the page links if there is one, else the page URL). 'notes' = one short "
           "line with anything else important. If the page has nothing useful, set relevant=false and items=[]. "
           "JSON only.")
    user = (f"QUESTION: {question}\nFIELDS TO FILL: {', '.join(fields)}\nTODAY: {datetime.date.today():%d %B %Y}\n\n"
            f"PAGE: {title}\nURL: {url}\n\n{text[:9000]}\n\nLINKS ON THE PAGE:\n{link_lines}")
    try:
        r = services.llm.json_task(sys, user, schema)
    except Exception:  # noqa: BLE001
        return []
    if not r.get("relevant", True):
        return []
    out = []
    for it in r.get("items") or []:
        name = (it.get("name") or "").strip()
        if not name or len(name) < 3:
            continue
        it = {k: (v.strip() if isinstance(v, str) else v) for k, v in it.items() if v not in (None, "", [])}
        it.setdefault("link", url)
        out.append(it)
    return out[:25]


def _norm(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _write_markdown(question, fields, items, sources):
    slug = re.sub(r"[^\w\- ]+", "", question)[:60].strip() or "research"
    path = workspace_sub("Research") / f"{datetime.date.today():%Y-%m-%d} {slug}.md"
    cols = ["name"] + [_key(f) for f in fields] + ["link"]
    lines = [f"# {question}", "", f"_Researched by Astra on {datetime.datetime.now():%d %b %Y %H:%M}_", ""]
    for it in items:
        lines.append(f"## {it.get('name')}")
        for c in cols[1:]:
            if it.get(c):
                lines.append(f"- **{c.replace('_', ' ')}:** {it[c]}")
        if it.get("notes"):
            lines.append(f"- {it['notes']}")
        lines.append(f"- _source: {it.get('_source', '')}_")
        lines.append("")
    lines += ["## Sources read", ""] + [f"- [{t or _host(u)}]({u})" for t, u in sources]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@tool("research", "Research a topic on the web WITH NOTES: searches several queries, reads the best pages, writes down "
      "the important facts of every item it finds (e.g. each art open call with deadline, fee, eligibility, link) and "
      "returns the full list. Use this for 'find / look for / list / compare / what are the current ...' requests "
      "instead of many manual searches. fields = the facts that matter for each item.",
      {"question": {"type": "string", "description": "what to find, in plain words"},
       "fields": {"type": "array", "items": {"type": "string"},
                  "description": "facts to note per item, e.g. ['deadline','fee','eligibility','location','prize']"},
       "max_sources": {"type": "integer", "description": "pages to read (default 8, max 15)"}},
      ["question"], label="Researching", modes=("astra",))
def research(ctx, question, fields=None, max_sources=8):
    from .web import search
    fields = [f for f in (fields or []) if isinstance(f, str) and f.strip()][:8] or ["summary", "date or deadline", "location"]
    max_sources = max(3, min(int(max_sources or 8), 15))
    _emit(ctx, action="start", topic=question, fields=[_key(f) for f in fields])

    urls, seen = [], set()
    for q in _queries(question):
        if ctx.should_stop():
            break
        _emit(ctx, action="status", text=f"Searching: {q}")
        try:
            results, engine = search(q, allow_browser=True)
        except Exception as e:  # noqa: BLE001
            _emit(ctx, action="status", text=f"Search failed: {str(e)[:80]}")
            continue
        for r in results[:8]:
            u = r["url"]
            k = u.split("#")[0].rstrip("/").lower()
            if k in seen or SKIP_HOSTS.search(_host(u) or ""):
                continue
            seen.add(k)
            urls.append((r["title"], u))
    if not urls:
        _emit(ctx, action="done", count=0)
        return "I couldn't get any search results (every search engine refused). Try again in a minute, or give me a site to look at."

    items, by_name, sources = [], {}, []
    for title, url in urls:
        if len(sources) >= max_sources or ctx.should_stop():
            break
        _emit(ctx, action="source", url=url, title=title or _host(url), state="reading")
        t, final, text, links = _read(ctx, url)
        if len(text) < 200:
            _emit(ctx, action="source", url=url, title=title or _host(url), state="skipped")
            continue
        sources.append((t or title, final))
        found = _extract(question, fields, t or title, final, text, links)
        new = 0
        for it in found:
            n = _norm(it["name"])
            if n in by_name:   # same item from another page: fill in what was missing
                old = by_name[n]
                for k, v in it.items():
                    if v and not old.get(k):
                        old[k] = v
                continue
            it["_source"] = final
            by_name[n] = it
            items.append(it)
            new += 1
            services.db.x("INSERT INTO notes (session, topic, title, data, source, created) VALUES (?,?,?,?,?,?)",
                          (ctx.conv_id or "", question[:200], it["name"][:200], json.dumps(it, ensure_ascii=False),
                           final, time.time()))
            _emit(ctx, action="item", item={k: v for k, v in it.items() if not k.startswith("_")}, source=_host(final))
        _emit(ctx, action="source", url=final, title=t or title or _host(final), state=f"{new} new" if new else "nothing new")

    path = _write_markdown(question, fields, items, sources) if items else None
    _emit(ctx, action="done", count=len(items), path=str(path) if path else "")
    services.db.log("research", f"{question[:120]} -> {len(items)} items from {len(sources)} pages")
    if not items:
        return (f"Read {len(sources)} pages but found nothing that clearly answers this. Pages read:\n"
                + "\n".join(f"- {u}" for _, u in sources))
    cols = [_key(f) for f in fields]
    out = [f"NOTES ({len(items)} items from {len(sources)} pages, saved to {path}):"]
    for i, it in enumerate(items, 1):
        bits = [f"{c.replace('_', ' ')}: {it[c]}" for c in cols if it.get(c)]
        out.append(f"{i}. {it['name']}" + (" | " + " | ".join(bits) if bits else "") +
                   (f" | {it['notes']}" if it.get("notes") else "") + f" | {it.get('link', '')}")
    out.append("\nPresent this as a clean list (most relevant / soonest first). Keep the links. Mention the notes file.")
    return "\n".join(out)


@tool("take_note", "Write down an important fact while browsing (shows in the notes card and is saved), e.g. a "
      "deadline, price or requirement you just read. One item per call.",
      {"title": {"type": "string"}, "details": {"type": "string"}, "source": {"type": "string"}},
      ["title", "details"], label="Taking a note", modes=("astra",))
def take_note(ctx, title, details, source=""):
    item = {"name": title, "notes": details, "link": source}
    services.db.x("INSERT INTO notes (session, topic, title, data, source, created) VALUES (?,?,?,?,?,?)",
                  (ctx.conv_id or "", (ctx.brief or {}).get("goal", "")[:200], title[:200],
                   json.dumps(item, ensure_ascii=False), source, time.time()))
    _emit(ctx, action="item", item=item, source=_host(source) if source else "")
    return "Noted."


@tool("show_notes", "Show all notes taken in this conversation (from research and take_note).",
      label="Reading notes", modes=("astra",))
def show_notes(ctx):
    rows = services.db.q("SELECT title, data, source FROM notes WHERE session=? ORDER BY id", (ctx.conv_id or "",))
    if not rows:
        return "No notes yet in this conversation."
    out = []
    for r in rows:
        d = json.loads(r["data"])
        extra = " | ".join(f"{k}: {v}" for k, v in d.items() if k not in ("name", "_source") and v)
        out.append(f"- {r['title']}" + (f" | {extra}" if extra else ""))
    return "\n".join(out)
