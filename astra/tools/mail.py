# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Email for several accounts (e.g. two Google accounts + uni mail): send via SMTP, read via IMAP.

Gmail / Google accounts: turn on 2-step verification, create an App password (myaccount.google.com/apppasswords)
and use it here. Outlook/Hotmail, GMX, web.de, iCloud, Yahoo, Seznam, Azet work the same way.

The watcher (runs in the background every few minutes) looks at new mail in the accounts you marked "watch",
sorts it (uni, work, proposal, commission, newsletter...), keeps the important ones for Nova to tell you about,
and - if you turned it on - sends a short auto reply to proposals/commissions ("I've passed this on...").
Blacklisted addresses never get an auto reply."""
import datetime
import email
import email.policy
import imaplib
import mimetypes
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from bs4 import BeautifulSoup

from .. import services
from ..paths import documents_dir
from . import tool

PRESETS = {
    "gmail.com": ("smtp.gmail.com", 587, "starttls", "imap.gmail.com"),
    "googlemail.com": ("smtp.gmail.com", 587, "starttls", "imap.gmail.com"),
    "outlook.com": ("smtp-mail.outlook.com", 587, "starttls", "outlook.office365.com"),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "starttls", "outlook.office365.com"),
    "live.com": ("smtp-mail.outlook.com", 587, "starttls", "outlook.office365.com"),
    "gmx.at": ("mail.gmx.net", 587, "starttls", "imap.gmx.net"),
    "gmx.de": ("mail.gmx.net", 587, "starttls", "imap.gmx.net"),
    "gmx.net": ("mail.gmx.net", 587, "starttls", "imap.gmx.net"),
    "web.de": ("smtp.web.de", 587, "starttls", "imap.web.de"),
    "icloud.com": ("smtp.mail.me.com", 587, "starttls", "imap.mail.me.com"),
    "yahoo.com": ("smtp.mail.yahoo.com", 465, "ssl", "imap.mail.yahoo.com"),
    "azet.sk": ("smtp.azet.sk", 465, "ssl", "imap.azet.sk"),
    "centrum.sk": ("smtp.centrum.sk", 465, "ssl", "imap.centrum.sk"),
    "seznam.cz": ("smtp.seznam.cz", 465, "ssl", "imap.seznam.cz"),
}
CATEGORIES = ["uni", "work", "proposal", "commission", "collaboration", "booking", "personal", "account", "security",
              "shopping", "promotion", "newsletter", "social", "spam", "other"]
# kinds of mail the filter hides by default (Settings > Email > Filter)
DEFAULT_HIDE = ["security", "account", "shopping", "promotion", "newsletter", "social", "spam"]

# fast rules that run before the model: obvious noise never needs an opinion
_SECURITY_RE = re.compile(
    r"security alert|new sign-?in|sign-?in (attempt|alert|from)|signed in|new login|login (alert|attempt)|"
    r"verification code|your code|one-time (pass)?code|confirm your (email|sign)|password (reset|changed)|"
    r"2-step|two-factor|2fa|was your account|unusual activity|new device|"
    r"sicherheitswarnung|neue anmeldung|anmeldung bei|bestätigungscode|sicherheitscode|"
    r"bezpečnostné upozornenie|nové prihlásenie|overovací kód|alerte de sécurité|nueva sesión|accesso", re.I)
_SECURITY_SENDERS = re.compile(r"accounts\.google\.com|no-?reply@accounts\.|security@|account-security|"
                               r"@id\.apple\.com|microsoftonline|account\.microsoft|@discord\.com|login@|verify@", re.I)
_PROMO_RE = re.compile(r"\b(\d+ ?% off|sale|deal|discount|coupon|promo|offer ends|limited time|free shipping|"
                       r"newsletter|weekly digest|unsubscribe|rabatt|angebot|zľava|akcia)\b", re.I)
_SOCIAL_SENDERS = re.compile(r"@(facebookmail|instagram|mail\.instagram|linkedin|twitter|x|tiktok|pinterest|reddit"
                             r"|redditmail|youtube|quora)\.com", re.I)


def filter_cfg():
    f = services.config.get("mail", "filter") or {}
    return {"hide": f.get("hide", DEFAULT_HIDE), "always": f.get("always", ""), "never": f.get("never", ""),
            "only_important": f.get("only_important", True)}


def _words(text):
    return [p.strip().lower() for p in re.split(r"[,\n]", text or "") if p.strip()]


def quick_sort(msg, sender, subject):
    """Rule-based category for obvious noise, or None to let the model decide."""
    hay = f"{sender} {subject}"
    f = filter_cfg()
    if any(w in hay.lower() for w in _words(f["never"])):
        return "spam"
    if any(w in hay.lower() for w in _words(f["always"])):
        return None
    if _SECURITY_SENDERS.search(sender) or _SECURITY_RE.search(subject):
        return "security"
    if _SOCIAL_SENDERS.search(sender):
        return "social"
    bulk = bool(msg.get("List-Unsubscribe")) or (msg.get("Precedence", "").lower() in ("bulk", "list"))
    if bulk and _PROMO_RE.search(hay):
        return "promotion"
    if bulk and NOREPLY_RE.search(sender):
        return "newsletter"
    return None


def is_noise(category, sender="", subject=""):
    f = filter_cfg()
    hay = f"{sender} {subject}".lower()
    if any(w in hay for w in _words(f["always"])):
        return False
    if any(w in hay for w in _words(f["never"])):
        return True
    return category in f["hide"]
NOREPLY_RE = re.compile(r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|notification|notifications@|bounce", re.I)


def preset_for(address):
    dom = (address or "").split("@")[-1].lower().strip()
    p = PRESETS.get(dom)
    if not p:
        return {}
    return {"smtp_host": p[0], "smtp_port": p[1], "smtp_security": p[2], "imap_host": p[3], "imap_port": 993}


# ---- accounts ------------------------------------------------------------------------------------------
def accounts():
    return services.config.get("mail", "accounts") or []


def password(acct):
    pw = services.config.get_secret("mail_pw:" + acct["id"])
    if not pw and acct["id"] == "main":
        pw = services.config.get_secret("email_password")   # saved by Astra v1
    return pw


def find_account(name=""):
    accs = accounts()
    if not accs:
        raise RuntimeError("No email account is set up yet. Ask the user to open Settings > Email.")
    n = (name or "").strip().lower()
    if not n:
        return accs[0]
    for a in accs:
        if n in (a["id"].lower(), (a.get("label") or "").lower(), (a.get("address") or "").lower()):
            return a
    for a in accs:
        if n in (a.get("label") or "").lower() or n in (a.get("address") or "").lower():
            return a
    raise RuntimeError(f"No email account called '{name}'. Accounts: " + ", ".join(account_names()))


def account_names():
    return [f"{a.get('label') or a['id']} <{a.get('address')}>" for a in accounts()]


def _full(acct):
    c = dict(acct)
    c.update({k: v for k, v in preset_for(c.get("address", "")).items() if not c.get(k)})
    c["password"] = password(acct)
    c["username"] = c.get("username") or c.get("address")
    if not (c.get("address") and c.get("smtp_host") and c["password"]):
        raise RuntimeError(f"The account {c.get('address') or c['id']} is missing its server or password (Settings > Email).")
    return c


def email_missing():
    if not any(a.get("address") and password(a) for a in accounts()):
        return "Email is not set up (Settings > Email)."
    return None


def smtp_send(msg, acct=None):
    c = _full(acct or find_account())
    port = int(c.get("smtp_port") or 587)
    if c.get("smtp_security") == "ssl" or port == 465:
        with smtplib.SMTP_SSL(c["smtp_host"], port, context=ssl.create_default_context(), timeout=30) as s:
            s.login(c["username"], c["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(c["smtp_host"], port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(c["username"], c["password"])
            s.send_message(msg)


def _imap(acct):
    c = _full(acct)
    m = imaplib.IMAP4_SSL(c["imap_host"], int(c.get("imap_port") or 993), timeout=30)
    m.login(c["username"], c["password"])
    return m


def test_connection(acct_id=""):
    acct = find_account(acct_id)
    c = _full(acct)
    port = int(c.get("smtp_port") or 587)
    if c.get("smtp_security") == "ssl" or port == 465:
        s = smtplib.SMTP_SSL(c["smtp_host"], port, timeout=20)
    else:
        s = smtplib.SMTP(c["smtp_host"], port, timeout=20)
        s.starttls()
    s.login(c["username"], c["password"])
    s.quit()
    m = _imap(acct)
    m.logout()
    return True


def _body_text(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001
        content = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        soup = BeautifulSoup(content, "html.parser")
        links = [f"{a.get_text(' ', strip=True)[:60]} -> {a['href']}" for a in soup.find_all("a", href=True)
                 if a["href"].startswith("http")]
        text = soup.get_text("\n")
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if links:
            text += "\n\nLINKS:\n" + "\n".join(links[:40])
        return text
    return content


def _from_line(acct):
    name = services.config.get("profile", "full_name")
    return f"{name} <{acct['address']}>" if name else acct["address"]


# ---- tools -------------------------------------------------------------------------------------------------
@tool("send_email",
      "Send an email from one of the user's accounts (account = label or address; empty = the first one). The user "
      "approves it first. attachments = file names in Documents/AstraNova/Files.",
      {"to": {"type": "string", "description": "comma separated"}, "subject": {"type": "string"},
       "body": {"type": "string"}, "cc": {"type": "string"}, "account": {"type": "string"},
       "attachments": {"type": "array", "items": {"type": "string"}},
       "in_reply_to": {"type": "string", "description": "Message-ID being answered (optional)"}},
      ["to", "subject", "body"], label="Sending email", needs=email_missing)
def send_email(ctx, to, subject, body, cc="", account="", attachments=None, in_reply_to=""):
    acct = find_account(account)
    attachments = attachments or []
    files = []
    for name in attachments:
        p = (documents_dir() / name).resolve()
        if documents_dir().resolve() not in p.parents or not p.exists():
            return f"Attachment '{name}' not found. Put it in {documents_dir()} first."
        files.append(p)
    if services.config.get("approvals", "send_email"):
        details = (f"From: {acct['address']}\nTo: {to}\n" + (f"Cc: {cc}\n" if cc else "") + f"Subject: {subject}\n"
                   + (f"Attachments: {', '.join(attachments)}\n" if attachments else "") + f"\n{body}")
        if not services.bridge.confirm("Send this email?", details):
            return "The user declined. The email was NOT sent. Ask what to change."
    msg = EmailMessage()
    msg["From"] = _from_line(acct)
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    for p in files:
        ctype, _ = mimetypes.guess_type(str(p))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    smtp_send(msg, acct)
    services.db.log("email", f"Sent from {acct['address']} to {to}: {subject}")
    return f"Email sent to {to} from {acct['address']}."


@tool("read_emails",
      "List recent emails (newest first). Ads, newsletters, social notifications and security alerts (new sign-in, "
      "codes) are filtered out per the user's settings unless show_all=true or you search for a sender/subject. "
      "account = label/address, or empty for ALL accounts. important_only = only mail marked important.",
      {"account": {"type": "string"}, "from_contains": {"type": "string"}, "subject_contains": {"type": "string"},
       "unread_only": {"type": "boolean"}, "important_only": {"type": "boolean"}, "days": {"type": "integer"},
       "limit": {"type": "integer"}, "show_all": {"type": "boolean"}},
      label="Checking inbox", needs=email_missing)
def read_emails(ctx, account="", from_contains="", subject_contains="", unread_only=False, important_only=False,
                days=7, limit=10, show_all=False):
    if important_only or (filter_cfg()["only_important"] and not (show_all or from_contains or subject_contains)):
        out = inbox_digest(ctx, days=days)
        if not out.startswith(("No important", "Email isn't")):
            return out + "\n(Filtered: ads, newsletters and security alerts are hidden. show_all=true lists everything.)"
        if important_only:
            return out
    accs = [find_account(account)] if account else [a for a in accounts() if password(a)]
    out = []
    for acct in accs:
        try:
            out += _list(acct, from_contains, subject_contains, unread_only, days, limit,
                         show_all or bool(from_contains or subject_contains))
        except Exception as e:  # noqa: BLE001
            out.append(f"[{acct.get('label') or acct['address']}] error: {str(e)[:150]}")
    return "\n".join(out) or "No matching emails."


def _list(acct, from_contains, subject_contains, unread_only, days, limit, show_all=True):
    m = _imap(acct)
    tag = acct.get("label") or acct["address"]
    try:
        m.select("INBOX", readonly=True)
        since = (datetime.date.today() - datetime.timedelta(days=int(days or 7))).strftime("%d-%b-%Y")
        crit = ["SINCE", since] + (["UNSEEN"] if unread_only else [])
        _typ, data = m.uid("search", None, *crit)
        uids = data[0].split()[::-1]
        out = []
        for uid in uids[:200]:
            _typ, d = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE PRECEDENCE)])")
            hdr = email.message_from_bytes(d[0][1], policy=email.policy.default)
            frm, subj = str(hdr.get("From", "")), str(hdr.get("Subject", ""))
            if not show_all:
                seen = services.db.q("SELECT category FROM mail_seen WHERE account=? AND msgid=?",
                                     (acct["id"], str(hdr.get("Message-ID") or "")))
                cat = seen[0]["category"] if seen else quick_sort(hdr, frm, subj)
                if cat and is_noise(cat, frm, subj):
                    continue
            if from_contains and from_contains.lower() not in frm.lower():
                continue
            if subject_contains and subject_contains.lower() not in subj.lower():
                continue
            out.append(f"[{tag}] uid={uid.decode()} | {hdr.get('Date', '')} | {frm} | {subj}")
            if len(out) >= int(limit or 10):
                break
        return out
    finally:
        m.logout()


@tool("read_email", "Read one email fully (text + links) by its uid from read_emails (and the account tag shown).",
      {"uid": {"type": "string"}, "account": {"type": "string"}}, ["uid"], label="Reading email", needs=email_missing)
def read_email(ctx, uid, account=""):
    acct = find_account(account)
    m = _imap(acct)
    try:
        m.select("INBOX", readonly=True)
        _typ, d = m.uid("fetch", str(uid).encode(), "(BODY.PEEK[])")
        if not d or not d[0]:
            return "Email not found."
        msg = email.message_from_bytes(d[0][1], policy=email.policy.default)
        body = re.sub(r"\n{3,}", "\n\n", _body_text(msg))
        return (f"Account: {acct['address']}\nFrom: {msg['From']}\nTo: {msg['To']}\nDate: {msg['Date']}\n"
                f"Subject: {msg['Subject']}\nMessage-ID: {msg['Message-ID']}\n\n{body[:7000]}")
    finally:
        m.logout()


@tool("inbox_digest", "Important emails the background watcher found recently across all accounts (uni, work, "
      "proposals...), with a one-line summary each.", {"days": {"type": "integer"}},
      label="Checking important mail", modes=("astra", "friend"))
def inbox_digest(ctx, days=3):
    since = time.time() - int(days or 3) * 86400
    rows = services.db.q("SELECT * FROM mail_seen WHERE important=1 AND ts>? ORDER BY ts DESC LIMIT 30", (since,))
    if not rows:
        return "No important mail recently." if accounts() else "Email isn't set up (Settings > Email)."
    labels = {a["id"]: a.get("label") or a.get("address") for a in accounts()}
    return "\n".join(f"[{labels.get(r['account'], r['account'])}] {datetime.datetime.fromtimestamp(r['ts']):%a %H:%M} {r['category']} - "
                     f"{r['sender']}: {r['subject']} -> {r['summary']}" + (" (auto-replied)" if r["replied"] else "")
                     for r in rows)


# ---- background watcher ----------------------------------------------------------------------------------------
def _rules_important(acct, sender, subject):
    pats = [p.strip().lower() for p in re.split(r"[,\n]", acct.get("important") or "") if p.strip()]
    hay = f"{sender} {subject}".lower()
    return any(p in hay for p in pats)


def _blacklisted(addr):
    bl = [b.strip().lower() for b in re.split(r"[,\n]", services.config.get("mail", "blacklist") or "") if b.strip()]
    a = addr.lower()
    return any(a == b or (b.startswith("@") and a.endswith(b)) or (b.startswith("*") and a.endswith(b[1:])) for b in bl)


def _classify(acct, sender, subject, body):
    schema = {"type": "object", "properties": {
        "category": {"type": "string", "enum": CATEGORIES}, "important": {"type": "boolean"},
        "summary": {"type": "string"}, "sender_name": {"type": "string"}},
        "required": ["category", "important", "summary"]}
    about = services.config.get("profile", "about") or ""
    sys = ("You sort a person's incoming email. Output JSON only. category: uni (university, courses, exams, "
           "professors, student admin), work, proposal (someone proposes a project/job/paid opportunity), commission "
           "(someone wants to commission/buy art or music), collaboration, booking (gigs, events, venues), personal, "
           "account (receipts of account changes, terms updates), security (sign-in alerts, codes, password resets), "
           "shopping (orders, shipping), promotion (ads, sales), newsletter, social (notifications from social "
           "networks), spam, other. important = a real person or institution wrote to THEM about something they "
           "need to know or act on (uni, work, deadlines, money, opportunities). Security alerts about their own "
           "sign-ins, ads, newsletters and automatic notifications are never important. summary = one short line of what it's about and "
           "anything they must do (with dates). sender_name = the sender's first name if a person.")
    user = (f"About the person: {about}\nAccount: {acct.get('label') or ''} {acct['address']}\n"
            f"From: {sender}\nSubject: {subject}\n\n{body[:3500]}")
    try:
        return services.llm.json_task(sys, user, schema)
    except Exception:  # noqa: BLE001
        return {"category": "other", "important": False, "summary": subject[:120]}


def _auto_reply(acct, msg, sender_addr, sender_name, category):
    cfg = services.config.get("mail")
    owner = services.config.get("profile", "name") or services.config.get("profile", "full_name") or "them"
    text = (cfg.get("auto_reply_text") or "").replace("{owner}", owner).replace(
        "{name}", f" {sender_name}" if sender_name else "")
    reply = EmailMessage()
    reply["From"] = _from_line(acct)
    reply["To"] = sender_addr
    subj = str(msg.get("Subject", ""))
    reply["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    reply["Date"] = formatdate(localtime=True)
    reply["Message-ID"] = make_msgid()
    reply["Auto-Submitted"] = "auto-replied"
    if msg.get("Message-ID"):
        reply["In-Reply-To"] = msg["Message-ID"]
        reply["References"] = msg["Message-ID"]
    reply.set_content(text)
    smtp_send(reply, acct)
    services.db.log("email", f"Auto-replied ({category}) from {acct['address']} to {sender_addr}: {subj[:120]}")


def check_new(on_important=None):
    """One pass over every watched account. on_important(row) is called for each new important mail."""
    cfg = services.config.get("mail")
    for acct in accounts():
        if not acct.get("watch", True) or not password(acct):
            continue
        try:
            _check_account(acct, cfg, on_important)
        except Exception as e:  # noqa: BLE001
            services.db.log("email", f"watcher: {acct.get('address')}: {str(e)[:200]}")


def _check_account(acct, cfg, on_important):
    key = f"mail_last_uid:{acct['id']}"
    state = services.db.kv_get(key) or {}
    m = _imap(acct)
    try:
        _typ, sel = m.select("INBOX", readonly=True)
        _t, val = m.response("UIDVALIDITY")
        validity = (val[0].decode() if val and val[0] else "")
        since = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%d-%b-%Y")
        _typ, data = m.uid("search", None, "SINCE", since)
        uids = [int(u) for u in data[0].split()]
        first_run = state.get("validity") != validity
        last = 0 if first_run else int(state.get("last", 0))
        new = [u for u in uids if u > last]
        if first_run:
            new = new[-12:]   # first look: only sort the latest few, never auto-reply to old mail
        for uid in new:
            _typ, d = m.uid("fetch", str(uid).encode(), "(BODY.PEEK[])")
            if not d or not d[0] or not isinstance(d[0], tuple):
                continue
            msg = email.message_from_bytes(d[0][1], policy=email.policy.default)
            mid = str(msg.get("Message-ID") or f"uid-{uid}")
            if services.db.q("SELECT 1 FROM mail_seen WHERE account=? AND msgid=?", (acct["id"], mid)):
                continue
            sender = str(msg.get("From", ""))
            sname, saddr = parseaddr(sender)
            subject = str(msg.get("Subject", ""))
            own = {a.get("address", "").lower() for a in accounts()}
            if saddr.lower() in own:
                continue
            body = _body_text(msg)
            quick = quick_sort(msg, sender, subject)
            c = ({"category": quick, "important": False, "summary": subject[:160]} if quick
                 else _classify(acct, sender, subject, body))
            cat = c.get("category") or "other"
            important = bool(c.get("important")) or _rules_important(acct, sender, subject)
            if is_noise(cat, sender, subject) and not _rules_important(acct, sender, subject):
                important = False
            elif any(w in f"{sender} {subject}".lower() for w in _words(filter_cfg()["always"])):
                important = True
            replied = 0
            if (cfg.get("auto_reply") and not first_run and cat in (cfg.get("auto_reply_categories") or [])
                    and saddr and not NOREPLY_RE.search(saddr) and not _blacklisted(saddr)
                    and not msg.get("List-Unsubscribe") and not msg.get("Auto-Submitted", "no").lower().startswith("auto")
                    and not services.db.q("SELECT 1 FROM mail_seen WHERE replied=1 AND sender LIKE ? AND ts>?",
                                          (f"%{saddr}%", time.time() - 7 * 86400))):
                try:
                    _auto_reply(acct, msg, saddr, c.get("sender_name") or (sname.split(" ")[0] if sname else ""), cat)
                    replied = 1
                except Exception as e:  # noqa: BLE001
                    services.db.log("email", f"auto reply failed: {e}")
            row = {"account": acct.get("label") or acct["address"], "msgid": mid, "sender": sender[:200],
                   "subject": subject[:300], "category": cat, "important": int(important),
                   "summary": (c.get("summary") or "")[:400], "replied": replied, "ts": time.time()}
            services.db.x("INSERT OR IGNORE INTO mail_seen (account, msgid, sender, subject, category, important, summary, "
                          "replied, told, ts) VALUES (?,?,?,?,?,?,?,?,0,?)",
                          (acct["id"], mid, row["sender"], row["subject"], cat, row["important"], row["summary"],
                           replied, row["ts"]))
            if important and on_important and not first_run:
                on_important(row)
        services.db.kv_set(key, {"validity": validity, "last": max(uids) if uids else last})
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
