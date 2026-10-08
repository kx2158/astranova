# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Web search and fast page reading (no browser window needed).

Search tries several engines in turn, because any single one sometimes blocks automated requests (DuckDuckGo
answers with a robot check, Bing with a consent page...). If every plain HTTP engine fails, the search runs in
AstraNova's own (hidden) browser, which handles cookie banners itself."""
import base64
import re
import threading
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

from . import tool

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
]
UA = {"User-Agent": UAS[0], "Accept-Language": "en-US,en;q=0.9"}
REGIONS = {"at": "at-de", "de": "de-de", "sk": "sk-sk", "ch": "ch-de", "us": "us-en", "uk": "uk-en", "fr": "fr-fr",
           "es": "es-es", "it": "it-it", "nl": "nl-nl", "pl": "pl-pl", "": "wt-wt"}
BLOCK_RE = re.compile(r"anomaly|unusual traffic|are you a robot|captcha|verify you are human|bots use duckduckgo", re.I)

_session = None
_lock = threading.Lock()
_cache = {}          # query -> (time, results)
_engine_fail = {}    # engine -> time it last failed (skip it for a while)


def _s():
    global _session
    with _lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update({"User-Agent": UAS[0], "Accept-Language": "en-US,en;q=0.9,de;q=0.7",
                                     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            # pre-answer the consent pages of the big engines
            _session.cookies.set("CONSENT", "YES+", domain=".google.com")
            _session.cookies.set("SOCS", "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg", domain=".google.com")
            _session.cookies.set("BCP", "AD=0&AL=0&SM=0", domain=".bing.com")
        return _session


def _get(url, **kw):
    kw.setdefault("timeout", 15)
    return _s().get(url, **kw)


def _clean_results(items):
    out, seen = [], set()
    for r in items:
        u = (r.get("url") or "").strip()
        if not u.startswith("http"):
            continue
        key = u.split("#")[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": (r.get("title") or "").strip()[:200], "url": u, "snippet": (r.get("snippet") or "").strip()[:400]})
    return out


# ---- engines -----------------------------------------------------------------------------------------
def _ddg(query, region):
    r = _s().post("https://html.duckduckgo.com/html/", data={"q": query, "kl": REGIONS.get(region, "wt-wt")}, timeout=15)
    if r.status_code != 200 or BLOCK_RE.search(r.text[:6000]):
        raise RuntimeError("blocked")
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for res in soup.select(".result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:
            continue  # ad
        snip = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": href, "snippet": snip.get_text(" ", strip=True) if snip else ""})
    return out


def _ddg_lite(query, region):
    r = _s().post("https://lite.duckduckgo.com/lite/", data={"q": query, "kl": REGIONS.get(region, "wt-wt")}, timeout=15)
    if r.status_code != 200 or BLOCK_RE.search(r.text[:6000]):
        raise RuntimeError("blocked")
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a.result-link"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        snip = ""
        tr = a.find_parent("tr")
        if tr:
            nxt = tr.find_next_sibling("tr")
            if nxt:
                snip = nxt.get_text(" ", strip=True)
        out.append({"title": a.get_text(" ", strip=True), "url": href, "snippet": snip})
    return out


def _bing_url(href):
    """Bing wraps results in /ck/a?...&u=a1<base64url>."""
    if "bing.com/ck/a" not in href:
        return href
    u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        b = u[2:] + "=" * (-len(u[2:]) % 4)
        try:
            return base64.urlsafe_b64decode(b).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return href
    return href


def _bing(query, region):
    params = {"q": query, "setlang": "en", "form": "QBLH"}
    if region:
        params["cc"] = region
    r = _get("https://www.bing.com/search", params=params)
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        p = li.select_one(".b_caption p, p")
        out.append({"title": a.get_text(" ", strip=True), "url": _bing_url(a.get("href", "")),
                    "snippet": p.get_text(" ", strip=True) if p else ""})
    return out


def _mojeek(query, region):
    r = _get("https://www.mojeek.com/search", params={"q": query})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for li in soup.select("ul.results-standard li"):
        a = li.select_one("a.title") or li.select_one("h2 a")
        if not a:
            continue
        p = li.select_one("p.s")
        out.append({"title": a.get_text(" ", strip=True), "url": a.get("href", ""), "snippet": p.get_text(" ", strip=True) if p else ""})
    return out


def _brave(query, region):
    r = _get("https://search.brave.com/search", params={"q": query, "source": "web"})
    if r.status_code != 200 or BLOCK_RE.search(r.text[:6000]):
        raise RuntimeError("blocked")
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for block in soup.select("div.snippet, div[data-type=web]"):
        a = block.select_one("a[href^=http]")
        if not a or "brave.com" in a.get("href", ""):
            continue
        t = block.select_one(".title, .snippet-title") or a
        d = block.select_one(".snippet-description, .description, p")
        out.append({"title": t.get_text(" ", strip=True), "url": a.get("href"), "snippet": d.get_text(" ", strip=True) if d else ""})
    return out


ENGINES = [("duckduckgo", _ddg), ("bing", _bing), ("duckduckgo-lite", _ddg_lite), ("brave", _brave), ("mojeek", _mojeek)]


def search(query, region="", want=8, allow_browser=True):
    """Returns (results, engine_name). Used by web_search and the research tool."""
    key = (query.strip().lower(), region)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1], hit[2]
    errors = []
    for name, fn in ENGINES:
        if time.time() - _engine_fail.get(name, 0) < 300:
            continue  # it blocked us a minute ago - don't hammer it
        try:
            res = _clean_results(fn(query, region))
        except Exception as e:  # noqa: BLE001
            _engine_fail[name] = time.time()
            errors.append(f"{name}: {str(e)[:60]}")
            continue
        if len(res) >= 2:
            _cache[key] = (time.time(), res[:want * 2], name)
            return res[:want * 2], name
        errors.append(f"{name}: no results")
    if allow_browser:
        try:
            from .browser import browser_search
            res = _clean_results(browser_search(query))
            if res:
                _cache[key] = (time.time(), res[:want * 2], "browser")
                return res[:want * 2], "browser"
        except Exception as e:  # noqa: BLE001
            errors.append(f"browser: {str(e)[:80]}")
    raise RuntimeError("; ".join(errors) or "no results")


@tool("web_search", "Search the web for current information. Returns titles, URLs and snippets. Optional region "
      "code like us, uk, de, at.", {"query": {"type": "string"}, "region": {"type": "string"}}, ["query"],
      label="Searching the web", modes=("astra", "friend", "guest"))
def web_search(ctx, query, region=""):
    try:
        results, engine = search(query, region, allow_browser=ctx.mode == "astra")
    except Exception as e:  # noqa: BLE001
        return f"Search failed ({e}). Try other words, or open a site directly with browser_open."
    return "\n\n".join(f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results[:10]))


# ---- reading pages -----------------------------------------------------------------------------------
WALL_RE = re.compile(r"enable javascript|javascript is (disabled|required)|please turn on javascript|"
                     r"checking your browser|just a moment|cookie|consent|access denied|captcha", re.I)


def fetch_text(url, max_chars=9000, with_links=False):
    """Plain HTTP read. Returns (title, final_url, text, links). Raises on failure."""
    r = _get(url, timeout=20, allow_redirects=True)
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return "PDF", r.url, _pdf_text(r.content, max_chars), []
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    links = []
    if with_links:
        for a in soup.find_all("a", href=True):
            t = a.get_text(" ", strip=True)
            href = urllib.parse.urljoin(r.url, a["href"])
            if t and len(t) > 3 and href.startswith("http"):
                links.append((t[:90], href))
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"]):
        t.decompose()
    main = soup.select_one("main, article, [role=main], #content, .content") or soup
    text = "\n".join(line.strip() for line in main.get_text("\n").splitlines() if line.strip())
    if len(text) < 300 and main is not soup:
        text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    return title, r.url, text[:max_chars], links


def _pdf_text(data, max_chars):
    try:
        import io

        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in rd.pages[:30])[:max_chars]
    except Exception:  # noqa: BLE001
        return "(PDF - could not extract text here; open it with browser_open)"


def looks_blocked(text):
    return len(text) < 400 or (len(text) < 1500 and WALL_RE.search(text[:1500]) is not None)


@tool("read_url", "Quickly read the text of a web page (articles, docs, lists, wikis, PDFs). Use browser_open for "
      "interactive sites, logins or forms.",
      {"url": {"type": "string"}, "links": {"type": "boolean", "description": "also list the page's links"}}, ["url"],
      label="Reading", modes=("astra", "friend", "guest"))
def read_url(ctx, url, links=False):
    if not url.startswith("http"):
        url = "https://" + url
    try:
        title, final, text, lk = fetch_text(url, with_links=links)
    except Exception as e:  # noqa: BLE001
        title, final, text, lk = "", url, "", []
        err = str(e)
    else:
        err = ""
    if looks_blocked(text) and ctx.mode == "astra":
        from .browser import browser_read
        return browser_read(ctx, url)
    if not text:
        return f"Could not read {url}: {err or 'empty page'}."
    out = f"TITLE: {title}\nURL: {final}\n\n{text}"
    if lk:
        out += "\n\nLINKS:\n" + "\n".join(f"- {t} -> {h}" for t, h in lk[:50])
    return out
