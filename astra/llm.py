"""Model client. Default: local Ollama on your GPU. Optional: any OpenAI-compatible cloud API
(OpenAI, OpenRouter, Groq, LM Studio...) for extra intelligence. Also handles screen vision.

Internal message format (Ollama style):
  {"role": "assistant", "content": str, "tool_calls": [{"id": str, "function": {"name": str, "arguments": dict}}]}
  {"role": "tool", "content": str, "tool_name": str, "tool_call_id": str}
"""
import json
import re

import requests

BASE = "http://127.0.0.1:11434"

# min_vram: the smallest graphics card (GB) it runs on fully, with room for the context window
MODEL_CHOICES = [
    {"name": "qwen3:4b", "label": "Light", "size": "2.6 GB", "min_vram": 4,
     "note": "For 4 to 6 GB graphics cards. Quick, fine for everyday things, weaker on long tasks."},
    {"name": "qwen3:8b", "label": "Fast", "size": "5.2 GB", "min_vram": 7,
     "note": "Made for 8 GB graphics cards. Quick replies, good tool use."},
    {"name": "qwen2.5:7b", "label": "Steady", "size": "4.7 GB", "min_vram": 7,
     "note": "Also fits 8 GB. Doesn't think out loud, very reliable at using tools."},
    {"name": "qwen3:14b", "label": "Balanced", "size": "9.3 GB", "min_vram": 11,
     "note": "For 12 to 16 GB graphics cards. Strong reasoning and tool use."},
    {"name": "qwen3:30b-a3b", "label": "Smartest local", "size": "19 GB", "min_vram": 22,
     "note": "For 24 GB graphics cards. On smaller cards part of it runs in system RAM: slower."},
]
VISION_CHOICES = [
    {"name": "qwen2.5vl:3b", "label": "Screen vision, light", "size": "3.2 GB", "min_vram": 4,
     "note": "Reads the screen on smaller graphics cards (loads only when needed)."},
    {"name": "qwen2.5vl:7b", "label": "Screen vision", "size": "6.0 GB", "min_vram": 11,
     "note": "Reads the screen and finds buttons when an app has no accessible controls."},
    {"name": "qwen3-vl:8b", "label": "Screen vision, newer", "size": "6.1 GB", "min_vram": 11,
     "note": "Newer vision model. Better at small text."},
]


class Cancelled(Exception):
    pass


# ---- reasoning that must never reach the chat ------------------------------------------------------------------
# Different models wrap their thinking differently: <think>, <thinking>, <reasoning>, gpt-oss channels, the old
# Ollama "Thinking... / ...done thinking." form, or only a closing </think> (the opening tag was in the template).
_THINK_PAIRS = [
    (re.compile(r"<think(?:ing)?>", re.I), re.compile(r"</think(?:ing)?>", re.I)),
    (re.compile(r"<reasoning>", re.I), re.compile(r"</reasoning>", re.I)),
    (re.compile(r"<\|channel\|>\s*analysis\s*<\|message\|>", re.I), re.compile(r"<\|end\|>|<\|start\|>", re.I)),
    (re.compile(r"^\s*Thinking\.\.\.\s*\n"), re.compile(r"\.\.\.done thinking\.?", re.I)),
]
_ORPHAN_CLOSE = re.compile(r"</think(?:ing)?>|</reasoning>", re.I)
_FINAL_CHANNEL = re.compile(r"<\|start\|>\s*assistant\s*|<\|channel\|>\s*(?:final|commentary)\s*<\|message\|>|<\|(?:return|end|call)\|>", re.I)


def _strip_think(t):
    t = t or ""
    for op, cl in _THINK_PAIRS:
        while True:
            mo = op.search(t)
            if not mo:
                break
            mc = cl.search(t, mo.end())
            t = t[:mo.start()] + (t[mc.end():] if mc else "")   # unclosed: everything after it was thinking
    mo = _ORPHAN_CLOSE.search(t)
    if mo:   # thinking whose opening tag was part of the prompt template
        t = t[mo.end():]
    t = _FINAL_CHANNEL.sub("", t)
    return t.strip()


class ThinkFilter:
    """Streaming version of _strip_think: passes visible text on, routes thinking to on_thinking.
    Holds back the start of a reply briefly so an orphan </think> can still be caught."""
    HOLD = 1600

    def __init__(self, on_token=None, on_thinking=None):
        self.on_token, self.on_thinking = on_token, on_thinking
        self.buf, self.sent, self.decided = "", 0, False

    def feed(self, chunk):
        self.buf += chunk or ""
        if not self.decided:
            low = self.buf.lower()
            if _ORPHAN_CLOSE.search(self.buf) or len(self.buf) > self.HOLD or (
                    len(self.buf) > 120 and not re.match(r"\s*(<|thinking|okay|alright|hmm|let me|so,? the user|the user|first,? )",
                                                         low)):
                self.decided = True
            else:
                return
        self._emit()

    def _emit(self, final=False):
        visible = _strip_think(self.buf)
        # while a think block is still open, nothing after it is visible yet
        if not final:
            for op, cl in _THINK_PAIRS:
                mo = op.search(self.buf)
                if mo and not cl.search(self.buf, mo.end()):
                    break
        if len(visible) > self.sent and self.on_token:
            self.on_token(visible[self.sent:])
            self.sent = len(visible)

    def close(self):
        self.decided = True
        self._emit(final=True)


_TEXT_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>|```(?:json)?\s*(\{[^`]*?\"(?:name|tool)\"[^`]*?\})\s*```", re.S)


def _text_tool_calls(content, tools):
    """Some models write their tool calls as text instead of using the API. Turn those into real calls."""
    names = {t.get("function", {}).get("name") for t in tools or []}
    found, cleaned = [], content or ""
    candidates = [m.group(1) or m.group(2) for m in _TEXT_CALL.finditer(cleaned)]
    if not candidates and cleaned.strip().startswith("{") and cleaned.strip().endswith("}"):
        candidates = [cleaned.strip()]
    for raw in candidates:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        name = d.get("name") or d.get("tool") or (d.get("function") or {}).get("name")
        args = d.get("arguments") or d.get("parameters") or d.get("args") or (d.get("function") or {}).get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name in names:
            found.append({"id": "t" + str(len(found)), "function": {"name": name, "arguments": args}})
    if found:
        cleaned = _TEXT_CALL.sub("", cleaned)
        if cleaned.strip().startswith("{"):
            cleaned = ""
    return found, cleaned.strip()


def _tools_as_text(tools):
    lines = []
    for t in tools or []:
        f = t.get("function", {})
        props = ", ".join(f"{k}" for k in (f.get("parameters", {}).get("properties") or {}))
        lines.append(f"- {f.get('name')}({props}): {(f.get('description') or '')[:160]}")
    return ("\n\nTOOLS: this model can't call functions natively. To use a tool, reply with ONLY this and nothing "
            "else:\n<tool_call>{\"name\": \"tool_name\", \"arguments\": {...}}</tool_call>\nYou'll get the result "
            "back, then continue. When you're done, answer normally without a tool call.\nAvailable tools:\n"
            + "\n".join(lines))


class LLM:
    def __init__(self, config):
        self.config = config
        self._fg = 0            # chats being answered right now (you're waiting on these)
        self._fg_end = 0.0

    # ---- light mode: the model runs on the processor (laptops) ---------------------
    def light(self):
        """'auto' (default) turns it on when there's no usable graphics card. Lean prompts that the engine can reuse
        between messages, a compact window, and background jobs wait while you chat."""
        if self.cloud():
            return False
        v = str(self.config.get("model", "light") or "auto")
        if v in ("on", "off"):
            return v == "on"
        from .hardware import cpu_only
        return bool(cpu_only())

    def busy(self):
        """Someone is waiting for a reply (or just got one and is likely typing the next message)."""
        import time
        return self._fg > 0 or time.time() - self._fg_end < 45

    # ---- provider ----------------------------------------------------------------
    def provider(self):
        return self.config.get("model", "provider") or "ollama"

    def cloud(self):
        return self.provider() == "openai"

    def model_label(self):
        m = self.config.get("model")
        return m.get("api_model") if self.cloud() else m.get("name")

    def _api(self):
        m = self.config.get("model")
        from . import services
        key = services.config.get_secret("api_key")
        base = (m.get("api_base") or "https://api.openai.com/v1").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if "openrouter.ai" in base:
            headers["HTTP-Referer"] = "https://astranova.local"
            headers["X-Title"] = "AstraNova"
        return base, headers

    # ---- server (Ollama) ---------------------------------------------------------
    def is_up(self):
        if self.cloud():
            return True
        try:
            return requests.get(BASE + "/api/version", timeout=2).ok
        except Exception:
            return False

    def installed_models(self):
        try:
            r = requests.get(BASE + "/api/tags", timeout=5)
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []

    def keep_alive(self):
        """How long the model stays in the graphics card after the last message. Shorter = the GPU is free for
        games and other apps sooner, longer = the next reply starts faster."""
        v = str(self.config.get("model", "keep_alive") or "10m")
        if v == "always":
            return -1
        if self.light() and v in ("2m", "10m"):
            return "30m"    # on the processor, loading the model again takes long; it only uses normal RAM
        return v

    def loaded_models(self):
        try:
            return [m["name"] for m in requests.get(BASE + "/api/ps", timeout=4).json().get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def gpu_share(self, name=None):
        """How much of the loaded model sits in the graphics card (1.0 = all of it, 0 = all on the processor)."""
        name = name or self.config.get("model", "name")
        try:
            for m in requests.get(BASE + "/api/ps", timeout=4).json().get("models", []):
                if m.get("name") == name or m.get("model") == name:
                    return (m.get("size_vram") or 0) / max(1, m.get("size") or 1)
        except Exception:  # noqa: BLE001
            pass
        return None

    def is_loaded(self, name=None):
        name = name or self.config.get("model", "name")
        return any(n == name or n == f"{name}:latest" for n in self.loaded_models())

    def unload(self, name):
        try:
            requests.post(BASE + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def unload_others(self, keep=()):
        """After switching models the old one would sit in VRAM and push the new one into slow system RAM."""
        keep = {k for k in keep if k} | {f"{k}:latest" for k in keep if k and ":" not in k}
        for n in self.loaded_models():
            if n not in keep:
                self.unload(n)

    def models_detail(self):
        try:
            r = requests.get(BASE + "/api/tags", timeout=5)
            return [{"name": m["name"], "size": int(m.get("size") or 0), "modified": m.get("modified_at", "")}
                    for m in r.json().get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def delete_model(self, name):
        self.unload(name)
        r = requests.delete(BASE + "/api/delete", json={"model": name, "name": name}, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(r.text[:200] or f"HTTP {r.status_code}")
        return True

    def has_model(self, name):
        names = self.installed_models()
        return name in names or (":" not in name and f"{name}:latest" in names)

    def pull(self, name, on_progress):
        with requests.post(BASE + "/api/pull", json={"model": name, "stream": True}, stream=True, timeout=None) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise RuntimeError(d["error"])
                on_progress(d.get("status", ""), d.get("completed"), d.get("total"))

    def test_cloud(self):
        base, headers = self._api()
        m = self.config.get("model")
        r = requests.post(base + "/chat/completions", headers=headers, timeout=30, json={
            "model": m.get("api_model"), "messages": [{"role": "user", "content": "Reply with: ok"}], "max_tokens": 5})
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return True

    # ---- chat ----------------------------------------------------------------------
    def _options(self, num_ctx=None):
        m = self.config.get("model")
        ctx = self.LIGHT_CTX if self.light() else int(num_ctx or m.get("num_ctx", 16384))
        return {"num_ctx": ctx, "temperature": float(m.get("temperature", 0.4))}

    # ---- context window ------------------------------------------------------------
    LIGHT_CTX = 8192    # one fixed window in light mode: changing it makes the engine reload the model

    def base_ctx(self):
        if self.light():
            return self.LIGHT_CTX
        return int(self.config.get("model", "num_ctx") or 16384)

    def max_ctx(self):
        m = self.config.get("model")
        if self.cloud():
            return 128000
        if self.light():
            return self.LIGHT_CTX
        if not m.get("auto_ctx", True):
            return self.base_ctx()
        return max(self.base_ctx(), int(m.get("max_ctx") or self.base_ctx()))

    def pick_ctx(self, needed_tokens):
        """Auto context: the smallest window (in steps, so the model isn't reloaded all the time) that fits."""
        import time
        base, top = self.base_ctx(), self.max_ctx()
        if self.cloud():
            return top
        want = int(needed_tokens * 1.25) + 2048   # room for the answer
        pick = top
        for step in (base, 32768, 40960, 49152, 65536, 98304, 131072):
            if base <= step <= top and step >= want:
                pick = step
                break
        # Ollama reloads the model whenever the window changes, so stay on a bigger window for a while
        last, when = getattr(self, "_ctx_last", (base, 0))
        if base <= last <= top and pick < last and time.time() - when < 600:
            pick = last
        self._ctx_last = (pick, time.time())
        return pick

    def chat(self, messages, tools=None, on_token=None, on_thinking=None, should_stop=None, think=None,
             temperature=None, num_ctx=None, fresh=False):
        """Streams a reply. Returns {'content': str, 'tool_calls': list, 'thinking': str}."""
        if self.cloud():
            return self._chat_openai(messages, tools, on_token, should_stop, temperature, fresh)
        import time
        self._fg += 1
        try:
            return self._chat_ollama(messages, tools, on_token, on_thinking, should_stop, think, temperature, num_ctx,
                                     fresh)
        finally:
            self._fg -= 1
            self._fg_end = time.time()

    def _chat_ollama(self, messages, tools, on_token, on_thinking, should_stop, think, temperature, num_ctx=None,
                     fresh=False):
        m = self.config.get("model")
        opts = self._options(num_ctx)
        if temperature is not None:
            opts["temperature"] = temperature
        if fresh:   # discourage repeating the same words and phrases (Nova)
            opts.update(repeat_penalty=1.18, repeat_last_n=512, presence_penalty=0.6, frequency_penalty=0.4)
        payload = {"model": m["name"], "messages": [_ollama_msg(x) for x in messages], "stream": True,
                   "options": opts, "keep_alive": self.keep_alive()}
        if tools:
            payload["tools"] = tools
            if getattr(self, "no_tools", None) == m["name"]:    # known not to support tools: describe them as text
                payload.pop("tools")
                payload["messages"] = _with_text_tools(payload["messages"], tools)
        payload["think"] = bool(m.get("think")) if think is None else think
        for attempt in range(4):
            content, thinking, tool_calls = [], [], []
            flt = ThinkFilter(on_token)
            try:
                resp = requests.post(BASE + "/api/chat", json=payload, stream=True, timeout=(10, 300))
            except requests.exceptions.ReadTimeout:
                raise RuntimeError(f"{m['name']} didn't respond for 5 minutes. It's probably too big for your graphics "
                                   "card: pick a smaller model in Settings > Model.") from None
            with resp as r:
                if r.status_code == 400 and "think" in r.text and "think" in payload:
                    payload.pop("think", None)  # model without thinking support
                    continue
                if r.status_code == 400 and "support tools" in r.text and payload.get("tools"):
                    payload.pop("tools", None)   # model without native tool support: describe the tools as text
                    payload["messages"] = _with_text_tools(payload["messages"], tools)
                    self.no_tools = m["name"]
                    continue
                if r.status_code == 404 and "not found" in r.text:
                    raise RuntimeError(f"The model {m['name']} isn't downloaded yet. Pick it again in Settings > Model "
                                       "to download it.")
                if r.status_code >= 400:
                    raise RuntimeError(f"model error {r.status_code}: {r.text[:300]}")
                for line in r.iter_lines():
                    if should_stop and should_stop():
                        raise Cancelled()
                    if not line:
                        continue
                    d = json.loads(line)
                    if d.get("error"):
                        raise RuntimeError(d["error"])
                    msg = d.get("message") or {}
                    if msg.get("thinking"):
                        thinking.append(msg["thinking"])
                        if on_thinking:
                            on_thinking(msg["thinking"])
                    if msg.get("content"):
                        content.append(msg["content"])
                        flt.feed(msg["content"])
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if d.get("done"):
                        break
            flt.close()
            text = _strip_think("".join(content))
            if tools and not tool_calls:
                tool_calls, text = _text_tool_calls(text, tools)
                if tool_calls is None:
                    tool_calls = []
            return {"content": text, "tool_calls": tool_calls, "thinking": "".join(thinking)}
        raise RuntimeError("model did not answer")

    def _chat_openai(self, messages, tools, on_token, should_stop, temperature, fresh=False):
        base, headers = self._api()
        m = self.config.get("model")
        payload = {"model": m.get("api_model"), "messages": _openai_msgs(messages), "stream": True,
                   "temperature": float(m.get("temperature", 0.4)) if temperature is None else temperature}
        if fresh:
            payload.update(presence_penalty=0.6, frequency_penalty=0.5)
        if tools:
            payload["tools"] = tools
        content, calls = [], {}
        flt = ThinkFilter(on_token)
        with requests.post(base + "/chat/completions", headers=headers, json=payload, stream=True,
                           timeout=(15, 600)) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"cloud model error {r.status_code}: {r.text[:400]}")
            for raw in r.iter_lines():
                if should_stop and should_stop():
                    raise Cancelled()
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                if d.get("error"):
                    raise RuntimeError(str(d["error"])[:400])
                for ch in d.get("choices") or []:
                    delta = ch.get("delta") or {}
                    # reasoning models send their thinking separately: never show it
                    if delta.get("content"):
                        content.append(delta["content"])
                        flt.feed(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["args"] += fn.get("arguments") or ""
        flt.close()
        tool_calls = []
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"] or "{}")
            except ValueError:
                args = {}
            tool_calls.append({"id": c["id"] or f"call_{i}", "function": {"name": c["name"], "arguments": args}})
        text = _strip_think("".join(content))
        if tools and not tool_calls:
            tool_calls, text = _text_tool_calls(text, tools)
        return {"content": text, "tool_calls": tool_calls, "thinking": ""}

    # ---- structured one-shot -------------------------------------------------------
    def text_task(self, system, user, temperature=0.3, max_chars=4000):
        """One short non-streaming answer (summaries, small rewrites)."""
        res = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user[-60000:]}],
                        think=False, temperature=temperature,
                        num_ctx=self.pick_ctx(len(system + user) // 3))
        return (res.get("content") or "").strip()[:max_chars]

    def json_task(self, system, user, schema):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.cloud():
            base, headers = self._api()
            m = self.config.get("model")
            r = requests.post(base + "/chat/completions", headers=headers, timeout=(15, 300), json={
                "model": m.get("api_model"), "messages": msgs + [{"role": "system", "content":
                    "Return only a JSON object matching this schema: " + json.dumps(schema)}],
                "temperature": 0.1, "response_format": {"type": "json_object"}})
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"]
        else:
            payload = {"model": self.config.get("model", "name"), "messages": msgs, "stream": False,
                       "format": schema, "think": False,
                       "options": {**self._options(self.pick_ctx((len(system) + len(user)) // 3)), "temperature": 0.1},
                       "keep_alive": self.keep_alive()}
            r = requests.post(BASE + "/api/chat", json=payload, timeout=(10, 600))
            if r.status_code == 400 and "think" in r.text:
                payload.pop("think")
                r = requests.post(BASE + "/api/chat", json=payload, timeout=(10, 600))
            r.raise_for_status()
            txt = r.json()["message"]["content"]
        txt = _strip_think(txt)
        mt = re.search(r"\{.*\}", txt, re.S)
        return json.loads(mt.group(0) if mt else txt)

    # ---- vision ----------------------------------------------------------------------
    def vision_model(self):
        m = self.config.get("model")
        return (m.get("api_vision_model") or m.get("api_model")) if self.cloud() else m.get("vision")

    def coords_mode(self):
        """How the vision model reports positions: 'pixels' | 'norm1000' | 'percent'."""
        if self.cloud():
            return "percent"
        name = (self.vision_model() or "").lower()
        return "norm1000" if "qwen3-vl" in name else "pixels"

    def vision(self, prompt, image_b64, as_json=False):
        if self.cloud():
            base, headers = self._api()
            body = {"model": self.vision_model(), "temperature": 0.1, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}}]}]}
            if as_json:
                body["response_format"] = {"type": "json_object"}
            r = requests.post(base + "/chat/completions", headers=headers, json=body, timeout=(15, 300))
            if r.status_code >= 400:
                raise RuntimeError(f"vision error {r.status_code}: {r.text[:300]}")
            return _strip_think(r.json()["choices"][0]["message"]["content"])
        name = self.vision_model()
        if not self.has_model(name):
            raise RuntimeError(f"The screen-vision model {name} is not installed yet. Open Settings > Model and "
                               f"press Download next to it.")
        # vision is used in short bursts: keep it a minute for the next look, then free the graphics card
        payload = {"model": name, "stream": False, "keep_alive": "60s", "options": {"temperature": 0.1, "num_ctx": 8192},
                   "messages": [{"role": "user", "content": prompt, "images": [image_b64]}]}
        if as_json:
            payload["format"] = "json"
        r = requests.post(BASE + "/api/chat", json=payload, timeout=(10, 600))
        if r.status_code >= 400:
            raise RuntimeError(f"vision error {r.status_code}: {r.text[:300]}")
        return _strip_think(r.json()["message"]["content"])


# ---- message format conversion -------------------------------------------------------
def _ollama_msg(m):
    out = {"role": m["role"], "content": m.get("content") or ""}
    if m.get("tool_calls"):
        out["tool_calls"] = [{"function": {"name": c["function"]["name"],
                                           "arguments": _as_dict(c["function"].get("arguments"))}}
                             for c in m["tool_calls"]]
    if m["role"] == "tool" and m.get("tool_name"):
        out["tool_name"] = m["tool_name"]
    if m.get("images"):
        out["images"] = m["images"]
    return out


def _with_text_tools(msgs, tools):
    msgs = [dict(x) for x in msgs]
    if msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] = (msgs[0].get("content") or "") + _tools_as_text(tools)
    else:
        msgs.insert(0, {"role": "system", "content": _tools_as_text(tools).strip()})
    # earlier tool results become plain text for models that don't know the tool role
    for x in msgs:
        if x.get("role") == "tool":
            x["role"] = "user"
            x["content"] = f"[result of {x.pop('tool_name', 'tool')}]\n{x.get('content', '')}"
            x.pop("tool_call_id", None)
        elif x.get("role") == "assistant" and x.get("tool_calls"):
            calls = x.pop("tool_calls")
            x["content"] = ((x.get("content") or "") + "\n" + "\n".join(
                "<tool_call>" + json.dumps({"name": c.get("function", {}).get("name"),
                                            "arguments": c.get("function", {}).get("arguments")}, ensure_ascii=False)
                + "</tool_call>" for c in calls)).strip()
    return msgs


def _openai_msgs(messages):
    out, open_ids = [], set()
    for m in messages:
        if m["role"] == "assistant":
            o = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                o["tool_calls"] = []
                for i, c in enumerate(m["tool_calls"]):
                    cid = c.get("id") or f"call_{len(out)}_{i}"
                    c["id"] = cid
                    open_ids.add(cid)
                    o["tool_calls"].append({"id": cid, "type": "function", "function": {
                        "name": c["function"]["name"],
                        "arguments": json.dumps(_as_dict(c["function"].get("arguments")), ensure_ascii=False)}})
            out.append(o)
        elif m["role"] == "tool":
            cid = m.get("tool_call_id")
            if not cid or cid not in open_ids:  # orphaned tool output (trimmed history) -> pass as text
                out.append({"role": "user", "content": f"[result of {m.get('tool_name', 'tool')}]\n{m.get('content', '')}"})
                continue
            out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content") or ""})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    # every assistant tool_call needs a matching tool message, otherwise the API rejects the request
    answered = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
    for o in out:
        if o.get("tool_calls"):
            o["tool_calls"] = [c for c in o["tool_calls"] if c["id"] in answered]
            if not o["tool_calls"]:
                o.pop("tool_calls")
    return out


def _as_dict(a):
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a or "{}")
    except Exception:
        return {}
