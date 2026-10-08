# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""AstraNova's own browser (Playwright, Microsoft Edge or bundled Chromium) with a persistent profile, so logins
survive restarts.

What makes it reliable (the v1 browser often "just got stuck"):
- every Playwright call runs on one worker thread with a hard time limit; if a page hangs, the worker and its
  browser processes are killed and a fresh one starts, instead of every later call queueing behind the hung one
- runs hidden by default and looks like a normal Edge window to websites (no "HeadlessChrome", no webdriver flag)
- a built-in consent blocker (works like a cookie extension) declines cookie banners in every page and frame,
  including Google's consent page; optional unpacked extensions can be loaded too
- CAPTCHAs/2FA: the window is shown so you can solve it, then it continues
"""
import base64
import concurrent.futures
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from .. import services
from ..paths import browser_profile_dir
from . import tool

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 "
      "Safari/537.36 Edg/129.0.0.0")

SNAPSHOT_JS = r"""
(maxEls) => {
  document.querySelectorAll('[data-astra-id]').forEach(e => e.removeAttribute('data-astra-id'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=combobox],[contenteditable=true],summary';
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = []; let id = 0;
  for (const e of document.querySelectorAll(sel)) {
    if (out.length >= maxEls) break;
    if (!vis(e)) continue;
    id++; e.setAttribute('data-astra-id', String(id));
    const tag = e.tagName.toLowerCase();
    let label = clean(e.getAttribute('aria-label') || e.innerText || e.getAttribute('title') || e.getAttribute('alt'));
    let desc = tag;
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      let l = '';
      if (e.id) { const lab = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (lab) l = clean(lab.innerText); }
      if (!l && e.closest('label')) l = clean(e.closest('label').innerText);
      label = clean(l || e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('name') || '');
      if (tag === 'input') desc += '[' + (e.type || 'text') + ']';
      if (e.type === 'checkbox' || e.type === 'radio') desc += e.checked ? ' (checked)' : ' (unchecked)';
      else if (tag !== 'select' && e.value) desc += ' value="' + clean(e.value).slice(0, 40) + '"';
      if (e.required) desc += ' required';
      if (tag === 'select') desc += ' selected="' + clean(e.options[e.selectedIndex]?.text) + '" options: ' + [...e.options].slice(0, 20).map(o => clean(o.text)).join(' | ');
    }
    if (tag === 'a') { const h = e.getAttribute('href') || ''; if (!h.startsWith('javascript')) desc += ' -> ' + e.href.slice(0, 140); }
    out.push('[' + id + '] ' + desc + (label ? ' "' + label.slice(0, 90) + '"' : ''));
  }
  const root = document.querySelector('main, article, [role=main]') || document.body;
  let text = (root ? root.innerText : '').slice(0, 200000);
  if (text.length < 300 && document.body) text = document.body.innerText.slice(0, 200000);
  text = text.replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n');
  return { title: document.title, url: location.href, elements: out, text: text,
           scroll: Math.round(100 * (window.scrollY + innerHeight) / Math.max(1, document.body ? document.body.scrollHeight : 1)) };
}
"""

STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined }); } catch (e) {}
  try { if (!window.chrome) window.chrome = { runtime: {} }; } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'de'] }); } catch (e) {}
  try { if (!navigator.plugins.length) Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] }); } catch (e) {}
})();
"""

# The consent blocker. Injected into every page and frame before the site's own scripts run.
CONSENT_JS = r"""
(() => {
  if (window.__anConsent) return; window.__anConsent = true;
  const MODE = '__MODE__';
  const REJECT = /^(reject all( cookies)?|reject( cookies)?|decline all|decline( cookies)?|deny all|deny|refuse all|refuse|disagree|i disagree|do not consent|don.t accept|only (strictly )?necessary( cookies)?|necessary (cookies )?only|use (only )?necessary cookies( only)?|only essential( cookies)?|essential (cookies )?only|accept (only )?(essential|necessary)( cookies)?|reject non-essential( cookies)?|continue without (accepting|agreeing)|alle ablehnen|ablehnen|nicht zustimmen|nur notwendige( cookies)?( akzeptieren| zulassen)?|nur (technisch )?(essenzielle|erforderliche|notwendige)( cookies)?( akzeptieren| zulassen| verwenden)?|weiter ohne (einwilligung|zustimmung)|optionale cookies ablehnen|odmietnu. v.etk[oy]|odmietnu.|nesúhlasím|tout refuser|refuser|continuer sans accepter|rifiuta tutto|rifiuta|rechazar todo|rechazar|alles weigeren|weigeren|odrzu. wszystkie|odm.tnout v.e)$/i;
  const ACCEPT = /^(accept all( cookies)?|accept( cookies)?|allow all( cookies)?|allow cookies|agree|i agree|agree and close|got it|ok|okay|alle akzeptieren|alle cookies akzeptieren|akzeptieren|zustimmen|alle zulassen|einverstanden|verstanden|prija. v.etko|prija.|súhlasím|povoli. v.etko|tout accepter|accepter|aceptar todo|aceptar|accetta tutto|accetta|alles accepteren|accepteren)$/i;
  const CONTEXT = /cookie|consent|privacy|datenschutz|einwilligung|zustimmung|tracking|súhlas|gdpr|partners|partner/i;
  const SELECTORS = ['#onetrust-reject-all-handler', '.ot-pc-refuse-all-handler', '#CybotCookiebotDialogBodyButtonDecline',
    '#didomi-notice-disagree-button', '.didomi-continue-without-agreeing', '.cmpboxbtnno', '[data-testid="uc-deny-all-button"]',
    'button[data-testid="uc-deny-all-button"]', '.cky-btn-reject', '.cmplz-deny', '.iubenda-cs-reject-btn',
    '.osano-cm-denyAll', '.cm-btn-decline', '#truste-consent-required', '.fc-cta-do-not-consent',
    '.qc-cmp2-summary-buttons button[mode="secondary"]', '[data-cookiefirst-action="reject"]', '#tarteaucitronAllDenied2',
    '.sp_choice_type_REJECT_ALL', '.sp_choice_type_13', '#cookiescript_reject', '.cc-deny', '.js-cookie-decline',
    'button#W0wltc', '.moove-gdpr-infobar-reject-btn', '#borlabs-cookie .cookie-refuse', '#BorlabsCookieBox a._brlbs-refuse-btn'];
  const vis = el => { try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; } catch (e) { return false; } };
  const txt = el => (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').replace(/\s+/g, ' ').trim();
  function roots() {
    const out = [document]; const walk = n => { for (const el of n.querySelectorAll('*')) { if (el.shadowRoot) { out.push(el.shadowRoot); if (out.length < 30) walk(el.shadowRoot); } } };
    try { walk(document); } catch (e) {}
    return out;
  }
  function inConsent(el) {
    for (let p = el, i = 0; p && i < 9; p = p.parentElement || (p.getRootNode && p.getRootNode().host), i++) {
      const id = ((p.id || '') + ' ' + (typeof p.className === 'string' ? p.className : '')).toLowerCase();
      if (/cookie|consent|cmp|gdpr|privacy|didomi|onetrust|usercentrics|sp_message|banner/.test(id)) return true;
      if (i >= 2 && p.innerText && p.innerText.length < 4000 && CONTEXT.test(p.innerText)) return true;
    }
    return /consent|privacy|cookie/i.test(location.hostname + location.pathname);
  }
  let tries = 0, done = false, firstSeen = 0;
  function attempt() {
    if (done) return;
    tries++;
    const rs = roots();
    for (const r of rs) for (const s of SELECTORS) { const el = r.querySelector(s); if (el && vis(el)) { el.click(); done = true; return; } }
    const btns = [];
    for (const r of rs) btns.push(...[...r.querySelectorAll('button,[role=button],a,input[type=button],input[type=submit]')].slice(0, 500));
    let accept = null;
    for (const b of btns) {
      const t = txt(b); if (!t || t.length > 60 || !vis(b)) continue;
      if (REJECT.test(t) && inConsent(b)) { b.click(); done = true; return; }
      if (!accept && ACCEPT.test(t) && inConsent(b)) accept = b;
    }
    if (accept) {
      if (!firstSeen) firstSeen = Date.now();
      // no way to decline: after a short wait, accept so the page becomes usable (or right away in accept mode)
      if (MODE === 'accept' || Date.now() - firstSeen > 2500) { accept.click(); done = true; }
    }
  }
  const tick = () => { attempt(); if (!done && tries < 25) setTimeout(tick, 600); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', tick); else tick();
  try {
    const mo = new MutationObserver(() => { if (!done) attempt(); });
    const start = () => mo.observe(document.documentElement, { childList: true, subtree: true });
    if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
    setTimeout(() => mo.disconnect(), 20000);
  } catch (e) {}
})();
"""

CAPTCHA_RE = re.compile(r"captcha|kein roboter|not a robot|robot check|unusual traffic|nie som robot|"
                        r"verify you are human|bist du ein mensch|access denied|zugriff verweigert", re.I)

_tls = threading.local()


class _Worker:
    """One browser thread + one Playwright instance. Thrown away completely if it hangs."""

    def __init__(self, svc):
        self.svc = svc
        self.q = queue.Queue()
        self.pw = None
        self.ctx = None
        self.pages = {}
        self.headless = None
        self.dead = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name="browser")
        self.thread.start()

    def _loop(self):
        _tls.worker = self
        while not self.dead:
            try:
                fn, fut = self.q.get(timeout=1)
            except queue.Empty:
                continue
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except Exception as e:  # noqa: BLE001
                    fut.set_exception(e)
        try:
            if self.pw:
                self.pw.stop()
        except Exception:  # noqa: BLE001
            pass


class BrowserService:
    def __init__(self):
        self._lock = threading.Lock()
        self.w = None
        self.force_visible = False  # set when the user must interact (CAPTCHA, 2FA)

    # ---- threading -------------------------------------------------------------
    def call(self, fn, timeout=90):
        with self._lock:
            if self.w is None or self.w.dead or not self.w.thread.is_alive():
                self.w = _Worker(self)
            w = self.w
        if getattr(_tls, "worker", None) is w:   # already on the browser thread
            return fn()
        fut = concurrent.futures.Future()
        w.q.put((fn, fut))
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            self.reset(w)
            raise RuntimeError(f"The page didn't respond for {timeout}s, so I restarted the browser. Try again, or use "
                               "another site.") from None

    def reset(self, w=None):
        """Kill a hung browser so the next call starts fresh."""
        with self._lock:
            w = w or self.w
            if w is None:
                return
            w.dead = True
            if self.w is w:
                self.w = None
        _kill_profile_browsers()

    # ---- lifecycle (browser thread) ---------------------------------------------
    @property
    def _w(self):
        return getattr(_tls, "worker", None) or self.w

    @property
    def headless(self):
        w = self.w
        return w.headless if w else None

    @property
    def ctx(self):
        w = self._w
        return w.ctx if w else None

    @property
    def pw(self):
        w = self._w
        return w.pw if w else None

    def _ensure(self, headless=None):
        w = self._w
        cfg = services.config.get("browser")
        want = (bool(cfg.get("headless", True)) and not self.force_visible) if headless is None else headless
        if w.ctx and w.headless == want:
            try:
                _ = w.ctx.pages
                return
            except Exception:  # noqa: BLE001
                w.ctx = None
        if w.ctx:
            try:
                w.ctx.close()
            except Exception:  # noqa: BLE001
                pass
            w.ctx, w.pages = None, {}
        if not w.pw:
            from playwright.sync_api import sync_playwright
            w.pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate,OptimizationHints,MediaRouter", "--lang=en-US"]
        if not want:
            args.append("--start-maximized")
        kwargs = dict(user_data_dir=str(browser_profile_dir()), headless=want, user_agent=UA, locale="en-US",
                      accept_downloads=True, ignore_https_errors=True, args=args)
        if want:
            kwargs["viewport"] = {"width": 1366, "height": 900}
        else:
            kwargs["no_viewport"] = True
        channel = _mac_channel(cfg.get("channel")) if sys.platform == "darwin" else (cfg.get("channel") or None)
        exts = _extension_dirs(cfg.get("extensions_dir"))
        if exts:  # branded Edge/Chrome ignore --load-extension, so extensions need Playwright's Chromium
            channel = "chromium"
            kwargs["args"] = args + ["--disable-extensions-except=" + ",".join(exts),
                                     "--load-extension=" + ",".join(exts)]
        try:
            w.ctx = w.pw.chromium.launch_persistent_context(channel=channel, **kwargs)
        except Exception:  # noqa: BLE001
            # profile still locked by a killed browser, or Edge missing -> clean up and use bundled Chromium
            _kill_profile_browsers()
            time.sleep(1)
            kwargs["args"] = [a for a in kwargs["args"] if not a.startswith(("--load-extension", "--disable-extensions-except"))]
            try:
                w.ctx = w.pw.chromium.launch_persistent_context(**kwargs)
            except Exception as e:  # noqa: BLE001
                if "Executable doesn't exist" not in str(e) or not _install_chromium():
                    raise
                w.ctx = w.pw.chromium.launch_persistent_context(**kwargs)
        w.ctx.set_default_timeout(15000)
        w.ctx.set_default_navigation_timeout(30000)
        w.ctx.add_init_script(STEALTH_JS)
        if (cfg.get("cookies") or "decline") != "off":
            w.ctx.add_init_script(CONSENT_JS.replace("__MODE__", cfg.get("cookies") or "decline"))
        w.headless = want
        ref = w
        w.ctx.on("close", lambda *_: _closed(ref))

    def _page(self, key):
        self._ensure()
        w = self._w
        p = w.pages.get(key)
        if p is None or p.is_closed():
            existing = [pg for pg in w.ctx.pages if pg not in w.pages.values()]
            p = existing[0] if (existing and existing[0].url == "about:blank") else w.ctx.new_page()
            w.pages[key] = p
        return p

    def set_page(self, key, page):
        self._w.pages[key] = page

    def check(self):
        """Used by setup: launch and close a page to verify the browser works."""
        def _c():
            self._ensure(True)
            pg = self._w.ctx.new_page()
            pg.goto("about:blank")
            pg.close()
            return True
        return self.call(_c, timeout=120)

    # ---- helpers (browser thread) -----------------------------------------------
    @staticmethod
    def _settle(page, idle=3500):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:  # noqa: BLE001
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=idle)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _dismiss_cookies(page):
        """The init script handles banners; this is a last pass for pages that loaded before it could act."""
        try:
            page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass
        return False

    def snapshot(self, page, max_text=4500, max_els=150, ctx=None):
        snap = page.evaluate(SNAPSHOT_JS, max_els)
        text = snap["text"]
        head = ""
        if CAPTCHA_RE.search(snap["title"] + " " + text[:1500]):
            head += ("NOTICE: this page shows a CAPTCHA / robot check. Call browser_wait_for_user so the user can "
                     "solve it, or try a different site.\n\n")
        if len(text) > max_text:
            text = text[:max_text] + "\n...[page text truncated - scroll, or use browser_find to search the page]"
        if ctx is not None:
            _peek(page, ctx)
        return (head + f"URL: {snap['url']}\nTITLE: {snap['title']}\nSCROLL: {snap['scroll']}% of page seen\n\n"
                f"PAGE TEXT:\n{text}\n\nINTERACTIVE ELEMENTS (use the [number] with click/type tools):\n"
                + "\n".join(snap["elements"]))

    def _el(self, page, eid):
        loc = page.locator(f'[data-astra-id="{int(eid)}"]').first
        if loc.count() == 0:
            raise RuntimeError(f"Element [{eid}] not found - call browser_view to refresh element numbers.")
        return loc


def _closed(w):
    w.ctx, w.pages = None, {}


def _extension_dirs(folder):
    """Every unpacked extension (a folder with manifest.json) inside the configured folder."""
    if not folder:
        return []
    root = Path(os.path.expandvars(os.path.expanduser(folder)))
    if (root / "manifest.json").exists():
        return [str(root)]
    if not root.is_dir():
        return []
    return [str(p.parent) for p in sorted(root.glob("*/manifest.json"))][:8]


def _kill_profile_browsers():
    """Kill browser processes that use AstraNova's profile (only ours - never your normal Edge)."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return
    prof = str(browser_profile_dir()).lower()
    for p in psutil.process_iter(["cmdline"]):
        try:
            cl = " ".join(p.info.get("cmdline") or []).lower()
            if prof in cl:
                p.kill()
        except Exception:  # noqa: BLE001
            continue


def _peek(page, ctx):
    """Send a small screenshot to the app so you can see what Astra is looking at."""
    if not ctx or not getattr(ctx, "conv_id", None) or ctx.mode != "astra":
        return
    try:
        img = page.screenshot(type="jpeg", quality=45, timeout=4000)
        services.emit({"type": "browser_peek", "conv": ctx.conv_id, "url": page.url,
                       "img": "data:image/jpeg;base64," + base64.b64encode(img).decode()})
    except Exception:  # noqa: BLE001
        pass


def _svc():
    return services.browser


def _run(ctx, fn, timeout=90):
    return _svc().call(lambda: fn(_svc()._page(ctx.page_key)), timeout=timeout)


def _goto(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:  # noqa: BLE001
        msg = str(e).split("\n")[0]
        if "Timeout" in msg:
            pass  # slow page: work with whatever has loaded so far
        elif "ERR_NAME_NOT_RESOLVED" in msg:
            raise RuntimeError(f"That address doesn't exist: {url}") from None
        elif "ERR_INTERNET_DISCONNECTED" in msg:
            raise RuntimeError("No internet connection.") from None
        else:
            raise RuntimeError(f"Couldn't open {url}: {msg[:200]}") from None
    _svc()._settle(page)


# ---- used by web.py ------------------------------------------------------------------------------------
SEARCH_JS = r"""
() => {
  const out = [];
  const add = (a, root) => {
    if (!a || !a.href || !/^https?:/.test(a.href)) return;
    const h = new URL(a.href).hostname;
    if (/google\.|bing\.com|duckduckgo|microsoft\.com\/.*bing/.test(h)) return;
    const t = (a.querySelector('h3') || a).innerText.trim();
    const box = root || a.closest('div.g, li.b_algo, article, div[data-hveid]') || a.parentElement;
    const snip = box ? box.innerText.replace(t, '').replace(/\s+/g, ' ').trim().slice(0, 300) : '';
    if (t) out.push({ title: t.slice(0, 200), url: a.href, snippet: snip });
  };
  document.querySelectorAll('#search a:has(h3), #rso a:has(h3)').forEach(a => add(a));
  document.querySelectorAll('li.b_algo').forEach(li => add(li.querySelector('h2 a'), li));
  document.querySelectorAll('article[data-testid=result]').forEach(ar => add(ar.querySelector('a[data-testid=result-title-a]'), ar));
  return out.slice(0, 20);
}
"""


def browser_search(query):
    """Search in the real (hidden) browser: Google first, then Bing, then DuckDuckGo."""
    svc = _svc()
    if svc is None:
        raise RuntimeError("browser not ready")
    engines = [f"https://www.google.com/search?hl=en&num=12&q={urllib.parse.quote(query)}",
               f"https://www.bing.com/search?setlang=en&q={urllib.parse.quote(query)}",
               f"https://duckduckgo.com/?q={urllib.parse.quote(query)}"]

    def f():
        page = svc._page("search")
        for url in engines:
            try:
                _goto(page, url)
                page.wait_for_timeout(1200)   # let the consent blocker do its thing
                if "consent." in page.url:     # still on Google's consent page -> go back to the results
                    _goto(page, url)
                res = page.evaluate(SEARCH_JS)
                body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 1500) : ''")
                if res and not CAPTCHA_RE.search(body):
                    return res
            except Exception:  # noqa: BLE001
                continue
        return []
    return svc.call(f, timeout=75)


def browser_read(ctx, url):
    """read_url fallback for pages that need JavaScript or hide behind a cookie wall."""
    def f(page):
        _goto(page, url)
        page.wait_for_timeout(900)
        return _svc().snapshot(page, max_text=8000, max_els=60, ctx=ctx)
    key = ctx.page_key + "-read"
    return _svc().call(lambda: f(_svc()._page(key)), timeout=75)


# ---- tools ---------------------------------------------------------------------

@tool("browser_open", "Open a URL in the browser and return the page text and interactive elements.",
      {"url": {"type": "string"}}, ["url"], label="Opening page")
def browser_open(ctx, url):
    if not re.match(r"^https?://", url):
        url = "https://" + url

    def f(page):
        _goto(page, url)
        page.wait_for_timeout(600)
        return _svc().snapshot(page, ctx=ctx)
    return _run(ctx, f)


@tool("browser_view", "Return the current page's text and numbered interactive elements.", label="Reading page")
def browser_view(ctx):
    return _run(ctx, lambda page: _svc().snapshot(page, max_text=6500, max_els=220, ctx=ctx))


@tool("browser_click", "Click an element by its [number] from the latest page view.",
      {"element": {"type": "integer"}}, ["element"], label="Clicking")
def browser_click(ctx, element):
    def f(page):
        before = len(page.context.pages)
        try:
            _svc()._el(page, element).click(timeout=8000)
        except Exception as e:  # noqa: BLE001
            if "intercepts pointer events" in str(e) or "not visible" in str(e):
                _svc()._el(page, element).click(timeout=5000, force=True)  # an overlay is in the way
            else:
                raise
        page.wait_for_timeout(600)
        if len(page.context.pages) > before:  # opened in new tab -> follow it
            newp = page.context.pages[-1]
            _svc().set_page(ctx.page_key, newp)
            page = newp
        _svc()._settle(page)
        return _svc().snapshot(page, max_text=3500, ctx=ctx)
    return _run(ctx, f)


@tool("browser_type", "Type text into an input/textarea by its [number]. Set submit=true to press Enter afterwards.",
      {"element": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
      ["element", "text"], label="Typing")
def browser_type(ctx, element, text, submit=False):
    def f(page):
        el = _svc()._el(page, element)
        editable = el.evaluate("e => e.isContentEditable || ['INPUT','TEXTAREA'].includes(e.tagName)")
        if not editable:
            raise RuntimeError(f"[{element}] is not a text field. Pick an input/textarea from browser_view.")
        el.click(timeout=8000)
        try:
            el.fill(text, timeout=8000)
        except Exception:  # noqa: BLE001
            page.keyboard.press("Control+A")
            page.keyboard.type(text, delay=15)
        if submit:
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            _svc()._settle(page)
            return f"Typed '{text}' and submitted.\n" + _svc().snapshot(page, max_text=3500, ctx=ctx)
        return f"Typed '{text}' into [{element}]."
    return _run(ctx, f)


@tool("browser_select", "Choose an option in a <select> dropdown by its [number] and the option's visible text.",
      {"element": {"type": "integer"}, "option": {"type": "string"}}, ["element", "option"], label="Selecting")
def browser_select(ctx, element, option):
    def f(page):
        el = _svc()._el(page, element)
        try:
            el.select_option(label=option, timeout=5000)
        except Exception:  # noqa: BLE001
            el.select_option(value=option, timeout=5000)
        page.wait_for_timeout(500)
        return f"Selected '{option}' in [{element}]."
    return _run(ctx, f)


@tool("browser_check", "Tick or untick a checkbox / radio by its [number].",
      {"element": {"type": "integer"}, "checked": {"type": "boolean"}}, ["element"], label="Ticking box")
def browser_check(ctx, element, checked=True):
    def f(page):
        el = _svc()._el(page, element)
        try:
            el.set_checked(bool(checked), timeout=5000)
        except Exception:  # noqa: BLE001
            el.click(timeout=5000)
        return f"[{element}] is now {'checked' if checked else 'unchecked'}."
    return _run(ctx, f)


@tool("browser_scroll", "Scroll the page down or up and return what is visible.",
      {"direction": {"type": "string", "enum": ["down", "up"]}}, label="Scrolling")
def browser_scroll(ctx, direction="down"):
    def f(page):
        page.mouse.wheel(0, 900 if direction == "down" else -900)
        page.wait_for_timeout(800)
        return _svc().snapshot(page, max_text=3500, ctx=ctx)
    return _run(ctx, f)


@tool("browser_back", "Go back to the previous page.", label="Going back")
def browser_back(ctx):
    def f(page):
        page.go_back(timeout=20000)
        _svc()._settle(page)
        return _svc().snapshot(page, max_text=3500, ctx=ctx)
    return _run(ctx, f)


@tool("browser_find", "Search the full text of the current page for a word/phrase and return matching passages.",
      {"query": {"type": "string"}}, ["query"], label="Searching page")
def browser_find(ctx, query):
    def f(page):
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        hits = [m.start() for m in re.finditer(re.escape(query), text, re.I)][:12]
        if not hits:
            return f"'{query}' not found on page."
        return "\n---\n".join(text[max(0, h - 250): h + 350].replace("\n", " ") for h in hits)
    return _run(ctx, f)


@tool("browser_wait_for_user",
      "ONLY for things only the user can do: a CAPTCHA, a login with 2FA, or reviewing a form before submitting. "
      "Asks the user first; if they say no, the browser stays hidden and you continue another way. Never use it "
      "just because a page is slow, has a cookie banner or didn't load: try another source instead.",
      {"reason": {"type": "string"}}, ["reason"], label="Asking for your help")
def browser_wait_for_user(ctx, reason):
    if not services.bridge.confirm("Astra needs you in her browser",
                                   reason + "\n\nShow the hidden browser window so you can do this?"):
        return ("The user said no: the browser stays hidden. Don't ask again for this; use another source or "
                "explain what's blocking.")
    def show():
        svc = _svc()
        url = None
        if svc._w.headless:
            try:
                url = svc._page(ctx.page_key).url
            except Exception:  # noqa: BLE001
                url = None
            svc.force_visible = True
        p = svc._page(ctx.page_key)
        if url and url != "about:blank" and p.url != url:
            _goto(p, url)
        p.bring_to_front()
        return p.url
    _svc().call(show, timeout=120)
    ok = services.bridge.confirm("Your turn in the browser", reason + "\n\nPress Done when finished.")
    snap = _run(ctx, lambda page: _svc().snapshot(page, max_text=3500))
    _svc().force_visible = False   # back to the background on the next launch
    return ("User finished. Current page:\n" if ok else "User declined / did not finish.\n") + snap


def install_chromium():
    """Install Playwright's bundled Chromium (fallback when Edge is missing). Works frozen too."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    exe = compute_driver_executable()
    cmd = list(exe) if isinstance(exe, (tuple, list)) else [str(exe)]
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(), check=True, creationflags=flags)


def _mac_channel(want):
    """Mac: use whichever of Chrome / Edge is installed (Safari can't be automated this way)."""
    apps = {"chrome": "Google Chrome.app", "msedge": "Microsoft Edge.app", "chrome-beta": "Google Chrome Beta.app"}
    for ch in [want] + [c for c in apps if c != want]:
        name = apps.get(ch or "")
        if name and any(os.path.exists(os.path.join(d, name)) for d in ("/Applications", os.path.expanduser("~/Applications"))):
            return ch
    return None    # Playwright's own Chromium (downloaded on first use)


def _install_chromium():
    """No Chrome or Edge on this computer (some Macs): fetch Playwright's own Chromium once (about 150 MB)."""
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        drv = compute_driver_executable()
        cmd = list(drv) if isinstance(drv, (tuple, list)) else [str(drv)]
        services.emit({"type": "notify", "text": "Getting a browser for Astra (one time, about 150 MB)..."})
        r = subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(), capture_output=True, timeout=900,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        services.db.log("browser", f"chromium install failed: {e}")
        return False
