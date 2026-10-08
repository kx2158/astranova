# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Text hygiene shared by every outgoing path (chat, messages, posts, bots, voice).

Rule from the user: absolutely no em dashes. En dashes go too, except inside number ranges.
"""
import re

_EM = re.compile(r"\s*[\u2014\u2015]\s*")          # em dash, horizontal bar
_EN_RANGE = re.compile(r"(\d)\s*\u2013\s*(\d)")     # 3–5 -> 3-5
_EN = re.compile(r"\s*\u2013\s*")                   # other en dashes
_DOUBLE_HYPHEN = re.compile(r"(?<=\w)\s*--\s*(?=\w)")


def no_dashes(text):
    """Replace em/en dashes with natural punctuation. Idempotent."""
    if not text:
        return text
    t = _EN_RANGE.sub(r"\1-\2", text)
    t = _EM.sub(", ", t)
    t = _EN.sub(", ", t)
    t = _DOUBLE_HYPHEN.sub(", ", t)
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r"\(\s*,\s*", "(", t)
    t = re.sub(r"^,\s*", "", t, flags=re.M)
    return t


def private_values():
    """Strings that must never reach other people (bots, guests, Auto-Astra, public posts)."""
    from . import services
    p = services.config.get("profile") or {}
    vals = [p.get(k, "") for k in ("legal_name", "full_name", "email", "phone", "address", "birth_date")]
    vals += [v.strip() for v in (services.config.get("privacy", "extra_private") or "").split(",")]
    return [v for v in vals if v and len(v.strip()) >= 4]


def redact(text):
    """Remove personal details from text that leaves to other people."""
    if not text:
        return text
    out = text
    for v in sorted(private_values(), key=len, reverse=True):
        out = re.sub(re.escape(v), "[private]", out, flags=re.I)
    out = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[private]", out)            # any email address
    def _phone(m):
        s = m.group(0)
        digits = sum(c.isdigit() for c in s)
        if digits < 9 or re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?", s.strip()):
            return s  # dates, years, short numbers stay
        return "[private]"
    out = re.sub(r"(?<![\d:])\+?\d[\d ()/.-]{7,}\d(?![\d:])", _phone, out)  # phone-like numbers
    return out


# ---- how Nova texts ------------------------------------------------------------------------------------------
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF\u2B50\u2B55\u2764"
                    "\uFE0F\u200D\u2190-\u21FF\u2300-\u23FF\u25A0-\u25FF]+")
_EMOTICON = re.compile(r"(?<![\w/])(?::|;|=)-?[()DPpOo3/\\|]+(?![\w/])|<3+|\bx[Dd]\b|\^_\^|\bT_T\b")
_URL = re.compile(r"(?:https?://|[A-Za-z]:\\)\S+?(?=[.,;:!?)\]]*(?:\s|$))|`[^`]*`")
_CONTRACTIONS = re.compile(r"\b(don|doesn|didn|isn|aren|wasn|weren|can|couldn|won|wouldn|shouldn|haven|hasn|i|it|that|"
                           r"what|there|you|we|they|he|she|let|who)'(t|m|s|re|ve|ll|d)\b", re.I)


def genz(text, keep_lists=True):
    """Make a reply read like someone in their early twenties texting: lowercase, no periods or exclamation marks,
    no emojis or emoticons, apostrophes dropped in everyday contractions, sentences split into separate texts.
    Links, file paths and list lines stay intact. Deterministic, so it works no matter what the model writes."""
    if not text:
        return text
    keep = {}

    def _protect(m):
        k = f"\u0000{len(keep)}\u0000"
        keep[k] = m.group(0)
        return k
    t = _URL.sub(_protect, text)
    t = re.sub("(?:" + _EMOJI.pattern + "|" + _EMOTICON.pattern + r")\s+(?=[A-Z])", ". ", t)   # emoji ending a sentence
    t = _EMOJI.sub("", t)
    t = _EMOTICON.sub("", t)
    t = t.replace("\u2026", "...")
    t = _CONTRACTIONS.sub(lambda m: m.group(1) + m.group(2), t)
    t = t.replace("\u2019", "'")
    t = _CONTRACTIONS.sub(lambda m: m.group(1) + m.group(2), t)
    out_blocks = []
    for block in re.split(r"\n\s*\n", t):
        lines = []
        for line in block.split("\n"):
            ln = line.strip()
            if not ln:
                continue
            if keep_lists and re.match(r"^([-*\u2022]|\d+[.)])\s+", ln):
                ln = re.sub(r"(?<=[\w\u0000)])[.!]+(\s|$)", r"\1", ln.lower())
                lines.append(re.sub(r"(?<=\w)[,;](?=\s)", "", ln).strip())
                continue
            # split into sentences first (each becomes its own text), then strip the punctuation
            parts = re.split(r"(?<=[.!?\u0000])\s+(?=\S)|\.{3,}\s*|(?<=\w)\s+-\s+", ln)
            for p in parts:
                p = (p or "").strip().lower()
                p = re.sub(r"[!?]+", "", p)
                p = re.sub(r"(?<!\d)\.+$", "", p)          # no full stop at the end
                p = re.sub(r"(?<=\w)\.(?=\s|$)", "", p)
                p = re.sub(r"(?<=\w)[,;](?=\s)", "", p)
                p = re.sub(r":$", "", p).strip()
                if p:
                    lines.append(p)
        if lines:
            out_blocks.append(lines)
    # one text per line, blank line between texts (the app shows each as its own bubble); list lines stay together
    texts = []
    for lines in out_blocks:
        cur = []
        for ln in lines:
            if re.match(r"^([-*\u2022]|\d+[.)])\s+", ln):
                cur.append(ln)
            else:
                if cur:
                    texts.append("\n".join(cur))
                    cur = []
                texts.append(ln)
        if cur:
            texts.append("\n".join(cur))
    texts = [re.sub(r"\s{2,}", " ", x).strip() for x in texts if x.strip()]
    if len(texts) > 5:   # nobody sends ten texts in a row: glue the short ones together
        merged = []
        for x in texts:
            if merged and len(merged[-1]) + len(x) < 70 and "\n" not in merged[-1] + x:
                merged[-1] += " " + x
            else:
                merged.append(x)
        texts = merged
    out = "\n\n".join(texts)
    for k, v in keep.items():
        out = out.replace(k, v)
    return out.strip()
