"""Pop culture awareness and local events. No API keys: public RSS feeds, Google News search feeds and Reddit's
public JSON (fandom and stan-community chatter)."""
import html
import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from .. import services
from ..paths import app_dir
from . import tool
from .web import UA

FEEDS = {
    "music": ["https://pitchfork.com/feed/feed-news/rss", "https://www.billboard.com/feed/",
              "https://www.rollingstone.com/music/feed/"],
    "entertainment": ["https://variety.com/feed/"],
    "games": ["https://www.polygon.com/rss/index.xml"],
}
SUBS = {"music": ["popheads", "hiphopheads", "Music"], "kpop": ["kpop"], "games": ["Games", "gaming"],
        "stan": ["popheads", "kpop", "Fauxmoi"], "entertainment": ["Fauxmoi", "television"]}
CACHE = app_dir() / "culture_cache.json"
_refreshing = threading.Lock()


def _rss(url, limit=12):
    r = requests.get(url, headers=UA, timeout=12)
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        date = (it.findtext("pubDate") or "").strip()
        src = it.find("source")
        if title:
            out.append({"title": html.unescape(title), "url": link, "date": date[:16],
                        "source": src.text if src is not None and src.text else urllib.parse.urlparse(url).netloc})
        if len(out) >= limit:
            break
    return out


def google_news(query, days=7, limit=10):
    q = urllib.parse.quote(f"{query} when:{days}d")
    return _rss(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", limit)


def reddit(sub, limit=12, query=""):
    base = f"https://www.reddit.com/r/{sub}/"
    url = (base + f"search.json?q={urllib.parse.quote(query)}&restrict_sr=1&sort=new&t=week&limit={limit}") if query \
        else base + f"hot.json?limit={limit}"
    r = requests.get(url, headers={"User-Agent": "Astra/2.0 (personal assistant)"}, timeout=12)
    out = []
    for c in (r.json().get("data") or {}).get("children", []):
        d = c.get("data") or {}
        if d.get("stickied"):
            continue
        out.append({"title": html.unescape(d.get("title", "")), "url": "https://www.reddit.com" + d.get("permalink", ""),
                    "source": f"r/{sub}", "score": d.get("score", 0), "comments": d.get("num_comments", 0),
                    "flair": d.get("link_flair_text") or ""})
    return out


def refresh_cache(force=False):
    """Background: keep a small set of fresh headlines for the friend's general awareness."""
    try:
        if not force and CACHE.exists() and time.time() - CACHE.stat().st_mtime < 6 * 3600:
            return
    except OSError:
        pass
    if not _refreshing.acquire(blocking=False):
        return
    try:
        items = []
        for url in FEEDS["music"][:2] + FEEDS["entertainment"]:
            try:
                items += _rss(url, 6)
            except Exception:  # noqa: BLE001
                continue
        for sub in ("popheads", "Fauxmoi"):
            try:
                items += [i for i in reddit(sub, 8) if i.get("score", 0) > 200][:5]
            except Exception:  # noqa: BLE001
                continue
        CACHE.write_text(json.dumps({"t": time.time(), "items": items[:30]}, ensure_ascii=False), encoding="utf-8")
    finally:
        _refreshing.release()


def headlines_for_prompt(n=8):
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        threading.Thread(target=refresh_cache, daemon=True).start()
        return ""
    if time.time() - d.get("t", 0) > 6 * 3600:
        threading.Thread(target=refresh_cache, daemon=True).start()
    items = d.get("items", [])[:n]
    if not items:
        return ""
    return ("RECENT POP CULTURE (background awareness, bring up only if it fits):\n"
            + "\n".join(f"- {i['title']} ({i['source']})" for i in items))


@tool("culture_news", "Fresh pop culture: news, album/single/tour/game releases, celebrity and fandom discourse "
      "(Reddit fan communities, stan-twitter-style chatter). topic: an artist, show, game or 'latest'. kind: news | "
      "releases | fandom | all.",
      {"topic": {"type": "string"}, "kind": {"type": "string", "enum": ["news", "releases", "fandom", "all"]},
       "area": {"type": "string", "enum": ["music", "kpop", "games", "entertainment", "stan"]}},
      label="Catching up on culture", modes=("astra", "friend", "guest"))
def culture_news(ctx, topic="latest", kind="all", area="music"):
    t = (topic or "latest").strip()
    latest = t.lower() in ("latest", "", "today", "news")
    out = []
    if kind in ("news", "all"):
        try:
            if latest:
                for url in FEEDS.get(area, FEEDS["music"])[:2]:
                    out += _rss(url, 6)
            else:
                out += google_news(t, 14, 8)
        except Exception as e:  # noqa: BLE001
            out.append({"title": f"(news feed failed: {e})", "url": "", "source": ""})
    if kind in ("releases", "all") and not latest:
        try:
            out += google_news(f"{t} new album OR single OR release OR tour OR announced", 30, 6)
        except Exception:  # noqa: BLE001
            pass
    if kind in ("fandom", "all"):
        for sub in SUBS.get(area, SUBS["music"])[:2]:
            try:
                out += reddit(sub, 8, "" if latest else t)[:6]
            except Exception:  # noqa: BLE001
                continue
    seen, lines = set(), []
    for i in out:
        k = i["title"].lower()[:80]
        if k in seen or not i["title"]:
            continue
        seen.add(k)
        extra = f" [{i['score']} upvotes, {i['comments']} comments]" if i.get("score") else ""
        lines.append(f"- {i['title']} ({i['source']}{', ' + i['date'] if i.get('date') else ''}){extra}\n  {i['url']}")
    return "\n".join(lines[:22]) or "Nothing found. Try a different spelling or a broader topic."


@tool("find_events", "Find events in an area: concerts, club nights, exhibitions, conventions, meetups, game/anime "
      "events. location: city or area; when: e.g. 'this weekend', 'October 2026'; interest: genre/topic (optional).",
      {"location": {"type": "string"}, "when": {"type": "string"}, "interest": {"type": "string"}}, ["location"],
      label="Looking for events", modes=("astra", "friend"))
def find_events(ctx, location, when="", interest=""):
    from .web import _bing, _ddg
    topic = (interest or "events").strip()
    queries = [f"{topic} {location} {when}".strip(), f"site:ra.co events {location} {interest}".strip(),
               f"site:eventbrite.com {location} {topic} {when}".strip(), f"site:songkick.com {location} concerts",
               f"concerts {location} {when}".strip(), f"exhibition OR festival OR convention {location} {when}".strip()]
    seen, lines = set(), []
    for q in queries:
        try:
            res = _ddg(q, "") or _bing(q)
        except Exception:  # noqa: BLE001
            continue
        for r in res[:6]:
            u = r["url"].split("?")[0]
            if u in seen:
                continue
            seen.add(u)
            lines.append(f"- {r['title']}\n  {r['url']}\n  {r['snippet'][:180]}")
    try:
        for n in google_news(f"{topic} {location} {when}".strip(), 30, 5):
            lines.append(f"- {n['title']} ({n['source']})\n  {n['url']}")
    except Exception:  # noqa: BLE001
        pass
    return (f"Events around {location}{' ' + when if when else ''}:\n" + "\n".join(lines[:24])) if lines else \
        "No events found. Try another phrasing or a nearby bigger city."
