"""Desktop control for any Windows app.

Primary path: Windows UI Automation (pywinauto). read_window lists an app's controls with [numbers]; the model
then clicks / types by number. This works in most apps (Discord, Spotify, Office, Explorer, settings, Ableton's
dialogs...). Fallback for apps that draw their own UI (games, canvases, some creative apps): screen vision -
look_at_screen / find_on_screen + click_at.

All Windows-only libraries are imported lazily so the module also imports on other systems (self-test).
"""
import base64
import ctypes
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

from .. import services
from ..paths import workspace_sub
from . import tool

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0

INTERACTIVE = {"Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "MenuItem", "ListItem", "TabItem",
               "Hyperlink", "TreeItem", "SplitButton", "Slider", "Spinner", "DataItem", "Document", "MenuBar",
               "Menu", "ToolBar", "Thumb", "ScrollBar"}
CLICKABLE = INTERACTIVE - {"MenuBar", "Menu", "ToolBar", "ScrollBar"}
TEXTUAL = {"Text", "Group", "Pane", "Custom", "Header", "HeaderItem", "StatusBar", "Image", "List", "Table"}


# ---------------------------------------------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------------------------------------------
def control_off():
    if not IS_WIN:
        return "Desktop control only works on Windows."
    if not services.config.get("control", "enabled"):
        return "Desktop control is turned off (Settings > Safety)."
    return None


def _blocked(*names):
    raw = services.config.get("control", "blocked_apps") or ""
    blocked = [b.strip().lower() for b in raw.split(",") if b.strip()]
    hay = " ".join(n or "" for n in names).lower()
    return next((b for b in blocked if b in hay), None)


def _check_stop(ctx):
    if ctx is not None and ctx.should_stop():
        from ..llm import Cancelled
        raise Cancelled()


# ---------------------------------------------------------------------------------------------------------------
# windows (ctypes for z-order, pywinauto for UI Automation)
# ---------------------------------------------------------------------------------------------------------------
def _proc_name(pid):
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return ""


def top_windows():
    """Visible top-level windows in z-order (front first), excluding Astra itself."""
    if not IS_WIN:
        return []
    from ctypes import wintypes
    user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    out, me = [], os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER -> skip owned popups
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        ex = user32.GetWindowLongW(hwnd, -20)
        if ex & 0x80:  # WS_EX_TOOLWINDOW
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me or buf.value in ("Program Manager",):
            return True
        out.append({"hwnd": hwnd, "title": buf.value, "pid": pid.value})
        return True

    user32.EnumWindows(cb, 0)
    for w in out:
        w["process"] = _proc_name(w["pid"])
        w["minimized"] = bool(user32.IsIconic(w["hwnd"]))
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


APP_PROCESS = {  # friendly name -> process name
    "discord": "discord.exe", "spotify": "spotify.exe", "apple music": "applemusic.exe", "ableton": "ableton live",
    "blender": "blender.exe", "affinity": "affinity", "photo": "photo.exe", "whatsapp": "whatsapp",
    "telegram": "telegram.exe", "slack": "slack.exe", "chrome": "chrome.exe", "edge": "msedge.exe",
    "firefox": "firefox.exe", "explorer": "explorer.exe", "file explorer": "explorer.exe", "obs": "obs64.exe",
    "steam": "steam", "notepad": "notepad.exe", "vscode": "code.exe", "vs code": "code.exe", "teams": "teams",
    "outlook": "outlook", "word": "winword.exe", "excel": "excel.exe", "fl studio": "fl64.exe", "photoshop": "photoshop.exe",
}


def find_window(name):
    """Best match for an app/window name (process name or title). None = frontmost non-Astra window."""
    wins = top_windows()
    if not wins:
        return None
    if not name:
        return wins[0]
    n = _norm(name)
    proc_hint = APP_PROCESS.get(n, "")
    best, best_score = None, 0
    for i, w in enumerate(wins):
        title, proc = _norm(w["title"]), (w["process"] or "").lower()
        s = 0
        if proc_hint and proc_hint in proc:
            s = 100
        elif proc_hint and proc_hint.replace(".exe", "") in title:
            s = 70
        elif n and n == _norm(proc.replace(".exe", "")):
            s = 95
        elif n and n in title:
            s = 80
        elif n and n.replace(" ", "") in proc.replace(" ", ""):
            s = 75
        elif n and all(part in title for part in n.split()):
            s = 60
        s -= i * 0.01  # prefer front-most
        if s > best_score:
            best, best_score = w, s
    return best if best_score > 0 else None


_tls = threading.local()


def _com():
    """UI Automation is COM: every worker thread must initialise it (multi-threaded apartment) once."""
    if getattr(_tls, "ok", False) or not IS_WIN:
        return
    sys.coinit_flags = 0  # COINIT_MULTITHREADED, read by comtypes/pywinauto on first import
    try:
        import comtypes
        comtypes.CoInitializeEx(0)
    except OSError:
        pass  # already initialised in this thread
    _tls.ok = True


def wrapper(hwnd):
    _com()
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo
    return UIAWrapper(UIAElementInfo(handle=hwnd))


def activate(w):
    """Bring a window (dict from top_windows) to the front."""
    user32 = ctypes.windll.user32
    hwnd = w["hwnd"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    try:
        wrapper(hwnd).set_focus()
    except Exception:
        # Alt-tap trick lets a background process take the foreground
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.35)
    return user32.GetForegroundWindow() == hwnd


def _require_window(name):
    off = control_off()
    if off:
        raise RuntimeError(off)
    w = find_window(name)
    if not w:
        raise RuntimeError(f"No open window matches '{name}'. Use list_windows or open_app first.")
    b = _blocked(w["title"], w["process"])
    if b:
        raise RuntimeError(f"Blocked: '{b}' is on the blocked apps list (Settings > Safety).")
    return w


# ---------------------------------------------------------------------------------------------------------------
# background mode: never take over the screen without asking, and give it back afterwards
# ---------------------------------------------------------------------------------------------------------------
def background_mode():
    return bool(services.config.get("control", "background_mode"))


def need_screen(ctx, why):
    """Ask once per task before using the real mouse/keyboard (background mode)."""
    if not background_mode() or (ctx is not None and getattr(ctx, "screen_ok", False)):
        return True
    ok = services.bridge.confirm("Astra needs your screen for a moment",
                                 f"{why}\n\nShe'll use the mouse and keyboard briefly, then put your window back. "
                                 f"Approve to allow it for this task.")
    if not ok:
        raise RuntimeError("The user didn't allow using the screen right now. Use a background method "
                           "(read_window + click_element/type_into, APIs) or ask later.")
    if ctx is not None:
        ctx.screen_ok = True
    return True


class foreground:
    """with foreground(ctx, hwnd, why): ... -> brings hwnd up, then restores the user's previous window."""

    def __init__(self, ctx, hwnd, why):
        self.ctx, self.hwnd, self.why, self.prev = ctx, hwnd, why, None

    def __enter__(self):
        need_screen(self.ctx, self.why)
        user32 = ctypes.windll.user32
        self.prev = user32.GetForegroundWindow()
        if self.hwnd and self.prev != self.hwnd:
            activate({"hwnd": self.hwnd})
        return self

    def __exit__(self, *exc):
        if background_mode() and self.prev and self.prev != self.hwnd:
            time.sleep(0.25)
            try:
                activate({"hwnd": self.prev})
            except Exception:  # noqa: BLE001
                pass
        return False


def _give_back_focus(prev, new_hwnd):
    """A freshly launched app grabs the foreground: hand it straight back to what the user was doing and leave the
    new window behind it (not minimised, so accessibility reading still works)."""
    user32 = ctypes.windll.user32
    if not prev or prev == new_hwnd:
        return
    for _ in range(6):
        if user32.GetForegroundWindow() == prev:
            break
        try:
            activate({"hwnd": prev})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    try:  # tuck the new window behind the user's one without activating it
        HWND_BOTTOM, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 1, 0x1, 0x2, 0x10
        user32.SetWindowPos(new_hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
    except Exception:  # noqa: BLE001
        pass


def _target_hwnd(ctx):
    if ctx is not None and ctx.element_window:
        return ctx.element_window
    w = find_window(None)
    return w["hwnd"] if w else None


# ---------------------------------------------------------------------------------------------------------------
# launching apps
# ---------------------------------------------------------------------------------------------------------------
_START_APPS = {"t": 0, "items": []}


def start_apps():
    """Everything in the Start menu (desktop + Store apps): [{'Name', 'AppID'}]. Cached 10 min."""
    if time.time() - _START_APPS["t"] < 600 and _START_APPS["items"]:
        return _START_APPS["items"]
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW).stdout
        items = json.loads(out or "[]")
        items = items if isinstance(items, list) else [items]
    except Exception:
        items = []
    _START_APPS.update(t=time.time(), items=items)
    return items


def _best_start_app(name):
    n = _norm(name)
    best, score = None, 0
    for it in start_apps():
        an = _norm(it.get("Name"))
        if not an:
            continue
        s = 100 if an == n else 85 if an.startswith(n) else 70 if n in an else \
            50 if all(p in an for p in n.split()) else 0
        if "uninstall" in an or "readme" in an or "help" in an:
            s -= 40
        if s > score:
            best, score = it, s
    return best if score >= 50 else None


def launch(name):
    """Start an app by name. Returns a description of what was launched."""
    low = _norm(name)
    special = {
        "spotify": "spotify:",
        "discord": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Discord", "Update.exe"),
    }
    if low == "discord" and os.path.exists(special["discord"]):
        subprocess.Popen([special["discord"], "--processStart", "Discord.exe"], creationflags=NO_WINDOW)
        return "Discord"
    app = _best_start_app(name)
    if app:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"], creationflags=NO_WINDOW)
        return app["Name"]
    if low in special:
        os.startfile(special[low])  # noqa: S606
        return name
    if os.path.exists(name):
        os.startfile(name)  # noqa: S606
        return name
    raise RuntimeError(f"Couldn't find an app called '{name}' in the Start menu.")


# ---------------------------------------------------------------------------------------------------------------
# UI tree
# ---------------------------------------------------------------------------------------------------------------
def _value(el):
    try:
        v = el.iface_value.CurrentValue
        return (v or "").strip()
    except Exception:
        return ""


def _toggle(el):
    try:
        return {0: "off", 1: "on", 2: "mixed"}.get(el.iface_toggle.CurrentToggleState)
    except Exception:
        return None


def walk(root, deadline, max_nodes=2500):
    """Breadth-first walk of on-screen elements. Yields (element, control_type, name, rect)."""
    queue, seen = [root], 0
    while queue and seen < max_nodes and time.time() < deadline:
        el = queue.pop(0)
        try:
            kids = el.children()
        except Exception:
            continue
        for k in kids:
            seen += 1
            try:
                ei = k.element_info
                if getattr(ei, "visible", True) is False:
                    continue
                r = ei.rectangle
                if r.width() <= 1 or r.height() <= 1:
                    continue
                yield k, ei.control_type or "", (ei.name or "").strip(), r
            except Exception:
                continue
            queue.append(k)


def describe_window(ctx, w, filter_text="", max_items=140, include_text=True, budget=6.0):
    root = wrapper(w["hwnd"])
    wr = root.rectangle()
    deadline = time.time() + budget
    f = (filter_text or "").lower()
    controls, texts, seen_txt = [], [], set()
    ctx.elements, ctx.element_window = {}, w["hwnd"]
    n = 0
    for el, ctype, name, r in walk(root, deadline):
        if r.right < wr.left or r.left > wr.right or r.bottom < wr.top or r.top > wr.bottom:
            continue
        val = _value(el) if ctype in ("Edit", "ComboBox", "Document", "Slider", "Spinner") else ""
        label = name or val
        if f and f not in (label + " " + ctype).lower():
            continue
        if ctype in CLICKABLE and (label or ctype in ("Edit", "Document", "ComboBox")):
            if len(controls) >= max_items:
                continue
            n += 1
            ctx.elements[n] = el
            extra = ""
            if val and val != name:
                extra += f' value="{val[:60]}"'
            t = _toggle(el) if ctype in ("CheckBox", "Button", "RadioButton") else None
            if t:
                extra += f" ({t})"
            try:
                if not el.element_info.enabled:
                    extra += " (disabled)"
            except Exception:
                pass
            controls.append(f'[{n}] {ctype} "{label[:90]}"{extra}')
        elif include_text and label and ctype in TEXTUAL | {"Text"} and len(label) > 1:
            key = label[:200]
            if key not in seen_txt and len(texts) < 120:
                seen_txt.add(key)
                texts.append(label[:300])
    head = f'WINDOW: "{w["title"]}" ({w["process"]})  size {wr.width()}x{wr.height()} at ({wr.left},{wr.top})'
    body = "CONTROLS (use the [number] with click_element / type_into):\n" + ("\n".join(controls) or "(none found)")
    if include_text and texts:
        body += "\n\nVISIBLE TEXT:\n" + "\n".join(texts)
    if len(controls) < 3:
        body += ("\n\nNOTE: this app exposes few accessible controls. Use look_at_screen / find_on_screen and "
                 "click_at, or keyboard shortcuts.")
    if time.time() > deadline:
        body += "\n(Listing stopped early - the window is very large. Use the filter argument to narrow it.)"
    return head + "\n" + body


def _el(ctx, element):
    el = ctx.elements.get(int(element))
    if el is None:
        raise RuntimeError(f"Element [{element}] unknown - call read_window first (numbers change after each read).")
    return el


def _pattern_action(el, ctype, action):
    """Operate a control through UI Automation patterns: no mouse, no keyboard, no focus change."""
    if action != "click":
        return False
    try:
        if ctype in ("CheckBox",):
            el.iface_toggle.Toggle()
            return True
        if ctype in ("ListItem", "TabItem", "TreeItem", "DataItem", "RadioButton"):
            try:
                el.iface_selection_item.Select()
                return True
            except Exception:  # noqa: BLE001
                pass
        if ctype in ("ComboBox", "SplitButton"):
            try:
                el.iface_expand_collapse.Expand()
                return True
            except Exception:  # noqa: BLE001
                pass
        el.iface_invoke.Invoke()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------------------------
# keyboard / clipboard
# ---------------------------------------------------------------------------------------------------------------
SPECIAL = {
    "enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}", "escape": "{ESC}", "space": "{SPACE}",
    "backspace": "{BACKSPACE}", "delete": "{DELETE}", "del": "{DELETE}", "insert": "{INSERT}", "home": "{HOME}",
    "end": "{END}", "pageup": "{PGUP}", "pgup": "{PGUP}", "pagedown": "{PGDN}", "pgdn": "{PGDN}", "up": "{UP}",
    "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}", "printscreen": "{PRTSC}", "capslock": "{CAPSLOCK}",
    "menu": "{APPS}", "apps": "{APPS}",
    **{f"f{i}": f"{{F{i}}}" for i in range(1, 25)},
    "plus": "{+}", "minus": "-", "comma": ",", "period": ".",
}
MODS = {"ctrl": "^", "control": "^", "shift": "+", "alt": "%"}


def to_send_keys(combo_text):
    """'ctrl+shift+n, enter' -> pywinauto send_keys syntax."""
    out = []
    text = re.sub(r"\s*\+\s*(?=\S)", "+", combo_text.strip())
    for combo in re.split(r"[,\s]+", text):
        if not combo:
            continue
        parts = [p for p in re.split(r"\+(?=.)", combo.lower()) if p]
        mods, key, win = "", "", False
        for p in parts:
            p = p.strip()
            if p in MODS:
                mods += MODS[p]
            elif p in ("win", "windows", "super", "meta", "cmd"):
                win = True
            else:
                key = p
        if key in SPECIAL:
            k = SPECIAL[key]
        elif len(key) == 1:
            k = "{" + key + "}" if key in "+^%~(){}[]" else key
        elif key:
            k = "{" + key.upper() + "}"
        else:
            k = ""
        seq = mods + k
        if win:
            seq = "{VK_LWIN down}" + seq + "{VK_LWIN up}"
        out.append(seq)
    return "".join(out)


def _mouse():
    _com()
    from pywinauto import mouse
    return mouse


def send_keys(seq, pause=0.03):
    _com()
    from pywinauto.keyboard import send_keys as sk
    sk(seq, pause=pause, with_spaces=True, with_tabs=True, with_newlines=False)


def clipboard_get_text():
    import pyperclip
    try:
        return pyperclip.paste()
    except Exception:
        return ""


def clipboard_set_text(t):
    import pyperclip
    pyperclip.copy(t)


def paste_text(text):
    """Type any text (unicode, emoji, long) by pasting it, then restore the clipboard."""
    old = clipboard_get_text()
    clipboard_set_text(text)
    time.sleep(0.08)
    send_keys("^v")
    time.sleep(0.35)
    try:
        clipboard_set_text(old)
    except Exception:
        pass


def type_keys(text):
    esc = "".join("{" + c + "}" if c in "+^%~(){}[]" else ("{ENTER}" if c == "\n" else c) for c in text)
    send_keys(esc, pause=0.015)


# ---------------------------------------------------------------------------------------------------------------
# screen capture + vision
# ---------------------------------------------------------------------------------------------------------------
def capture(rect=None):
    """Screenshot a window (left, top, right, bottom, hwnd) in the background, or the whole virtual screen."""
    import mss
    from PIL import Image
    if rect and len(rect) == 5 and IS_WIN:
        try:
            return capture_hwnd(rect[4])
        except Exception:  # noqa: BLE001
            rect = rect[:4]
    with mss.mss() as s:
        if rect:
            l, t, r, b = rect[:4]
            mon = {"left": int(l), "top": int(t), "width": max(1, int(r - l)), "height": max(1, int(b - t))}
        else:
            mon = s.monitors[0]
        shot = s.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, (mon["left"], mon["top"])


def capture_hwnd(hwnd):
    """Screenshot one window even if it's behind others (PrintWindow). Returns (PIL image, (left, top))."""
    from ctypes import wintypes
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)
        time.sleep(0.5)
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = max(1, r.right - r.left), max(1, r.bottom - r.top)
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    img = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    if not ok or img.getextrema() == ((0, 0), (0, 0), (0, 0)):
        raise RuntimeError("window capture failed")
    return img, (r.left, r.top)


def _fit(img, max_pixels=1_000_000, max_w=1288, step=28):
    w, h = img.size
    scale = min(1.0, max_w / w, (max_pixels / (w * h)) ** 0.5)
    nw, nh = max(step, int(w * scale) // step * step), max(step, int(h * scale) // step * step)
    return img.resize((nw, nh)) if (nw, nh) != (w, h) else img


def _b64(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _target_rect(window):
    if window in (None, "", "screen", "full", "all"):
        if window in ("screen", "full", "all"):
            return None, "the whole screen"
        w = find_window(None)
    else:
        w = _require_window(window)
    if not w:
        return None, "the whole screen"
    if not background_mode():
        activate(w)  # make sure Astra's own window isn't covering it
        time.sleep(0.25)
    r = wrapper(w["hwnd"]).rectangle()
    return (r.left, r.top, r.right, r.bottom, w["hwnd"]), f'window "{w["title"]}"'


def locate(description, window=None):
    """Ask the vision model where something is. Returns (x, y, label) in screen pixels or None."""
    rect, what = _target_rect(window)
    img, (ox, oy) = capture(rect)
    small = _fit(img)
    sw, sh = small.size
    mode = services.llm.coords_mode()
    unit = {"pixels": f"pixel coordinates in this {sw}x{sh} image",
            "norm1000": "coordinates on a 0-1000 scale (0,0 top-left, 1000,1000 bottom-right)",
            "percent": "percentages of the image width and height (0-100)"}[mode]
    prompt = (f"This is a screenshot of {what}. Find this UI element: {description}.\n"
              f"Reply with JSON only: {{\"found\": true/false, \"x\": number, \"y\": number, \"label\": \"what you see there\"}}"
              f" where x,y is the CENTER of the element as {unit}.")
    raw = services.llm.vision(prompt, _b64(small), as_json=True)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(m.group(0) if m else raw)
    except Exception:
        nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
        if len(nums) < 2:
            return None
        d = {"found": True, "x": float(nums[0]), "y": float(nums[1]), "label": raw[:80]}
    if not d.get("found", True) or d.get("x") is None:
        return None
    x, y = float(d["x"]), float(d["y"])
    if isinstance(d.get("bbox_2d"), list) and len(d["bbox_2d"]) == 4:
        x0, y0, x1, y1 = d["bbox_2d"]
        x, y = (x0 + x1) / 2, (y0 + y1) / 2
    if mode == "norm1000" or (mode == "pixels" and x <= 1 and y <= 1):
        fx, fy = (x / 1000, y / 1000) if mode == "norm1000" else (x, y)
    elif mode == "percent":
        fx, fy = x / 100, y / 100
    else:
        fx, fy = x / sw, y / sh
    fx, fy = min(max(fx, 0), 1), min(max(fy, 0), 1)
    W, H = img.size
    return int(ox + fx * W), int(oy + fy * H), d.get("label", "")


# ---------------------------------------------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------------------------------------------
@tool("list_windows", "List the app windows open on the PC (front-most first) with their process names.",
      label="Listing windows", needs=control_off)
def list_windows(ctx):
    ws = top_windows()
    if not ws:
        return "No windows open."
    return "\n".join(f"{i + 1}. {w['title'][:90]}  [{w['process']}]" + ("  (minimized)" if w["minimized"] else "")
                     for i, w in enumerate(ws[:40]))


@tool("open_app", "Open an app by name, e.g. 'Discord', 'Spotify', 'Apple Music', 'Notepad'. Waits until its window "
      "appears. In background mode it stays behind the user's window (read_window still works).",
      {"name": {"type": "string"}}, ["name"], label="Opening app", needs=control_off)
def open_app(ctx, name):
    off = control_off()
    if off:
        return off
    b = _blocked(name)
    if b:
        return f"Blocked: '{b}' is on the blocked apps list."
    bg = background_mode()
    w = find_window(name)
    if w and not _norm(name) in ("explorer", "file explorer"):
        if bg:   # already open: work with it where it is, don't pull it in front of the user
            return f'"{w["title"]}" ({w["process"]}) is open. Next: read_window window="{name}" to see its controls.'
        activate(w)
        return f'Switched to "{w["title"]}" ({w["process"]}). Next: read_window to see its controls.'
    user32 = ctypes.windll.user32
    prev = user32.GetForegroundWindow()
    launched = launch(name)
    for _ in range(40):
        _check_stop(ctx)
        time.sleep(0.5)
        w = find_window(name) or find_window(launched)
        if w:
            time.sleep(1.0)
            if bg:
                _give_back_focus(prev, w["hwnd"])
                return (f'Opened {launched} in the background: window "{w["title"]}". Next: read_window '
                        f'window="{name}" to see its controls.')
            activate(w)
            return f'Opened {launched}: window "{w["title"]}". Next: read_window to see its controls.'
    return f"Started {launched}, but its window hasn't appeared yet (may still be loading). Try list_windows in a moment."


@tool("focus_window", "Bring an open window to the front by app name or part of its title.",
      {"window": {"type": "string"}}, ["window"], label="Switching window", needs=control_off)
def focus_window(ctx, window):
    w = _require_window(window)
    if background_mode():
        return (f'"{w["title"]}" is there. No need to bring it to the front: read_window, click_element and '
                'type_into work in the background. Only mouse/keyboard tools take the screen, and they ask first.')
    ok = activate(w)
    return f'Focused "{w["title"]}".' if ok else f'Tried to focus "{w["title"]}" (Windows may have blocked it - click_at on it if needed).'


@tool("window_action", "Minimize, maximize, restore or close a window.",
      {"window": {"type": "string"}, "action": {"type": "string", "enum": ["minimize", "maximize", "restore", "close"]}},
      ["window", "action"], label="Window", needs=control_off)
def window_action(ctx, window, action):
    w = _require_window(window)
    cmd = {"minimize": 6, "maximize": 3, "restore": 9}.get(action)
    if action == "close":
        ctypes.windll.user32.PostMessageW(w["hwnd"], 0x0010, 0, 0)  # WM_CLOSE (app may ask to save)
    else:
        ctypes.windll.user32.ShowWindow(w["hwnd"], cmd)
    return f'{action.capitalize()}d "{w["title"]}".'


@tool("read_window", "Read an app window: its numbered controls (buttons, fields, list items, menus) and visible text. "
      "Omit window for the front-most app. Use filter to only list controls containing a word (big apps).",
      {"window": {"type": "string"}, "filter": {"type": "string"}}, label="Reading window", needs=control_off)
def read_window(ctx, window="", filter=""):  # noqa: A002
    w = _require_window(window or None)
    if ctypes.windll.user32.IsIconic(w["hwnd"]):
        ctypes.windll.user32.ShowWindow(w["hwnd"], 4)  # SW_SHOWNOACTIVATE: un-minimise without stealing focus
        time.sleep(0.4)
    if window and not background_mode():
        activate(w)
        time.sleep(0.2)
    return describe_window(ctx, w, filter_text=filter)


@tool("click_element", "Click a control by its [number] from the latest read_window. action: click | double | right.",
      {"element": {"type": "integer"}, "action": {"type": "string", "enum": ["click", "double", "right"]}},
      ["element"], label="Clicking", needs=control_off)
def click_element(ctx, element, action="click"):
    el = _el(ctx, element)
    name = el.element_info.name or el.element_info.control_type
    if _pattern_action(el, el.element_info.control_type, action):
        time.sleep(0.4)
        return f'Clicked [{element}] "{name}" (in the background). Call read_window to see the result.'
    with foreground(ctx, ctx.element_window, f'Click "{name}"'):
        if action == "double":
            el.click_input(double=True)
        elif action == "right":
            el.click_input(button="right")
        else:
            el.click_input()
        time.sleep(0.5)
    return f'{"Double-clicked" if action == "double" else "Right-clicked" if action == "right" else "Clicked"} [{element}] "{name}". Call read_window to see the result.'


@tool("type_into", "Click a text field by its [number] and type text into it (replaces existing text unless "
      "append=true). enter=true presses Enter afterwards.",
      {"element": {"type": "integer"}, "text": {"type": "string"}, "enter": {"type": "boolean"},
       "append": {"type": "boolean"}}, ["element", "text"], label="Typing", needs=control_off)
def type_into(ctx, element, text, enter=False, append=False):
    el = _el(ctx, element)
    if not enter:
        try:  # background: set the value directly
            cur = _value(el) if append else ""
            el.iface_value.SetValue(cur + text)
            return f"Typed {len(text)} characters into [{element}] (in the background)."
        except Exception:  # noqa: BLE001
            pass
    with foreground(ctx, ctx.element_window, f"Type into a field and press Enter" if enter else "Type into a field"):
        el.click_input()
        time.sleep(0.15)
        if not append:
            send_keys("^a{BACKSPACE}")
        paste_text(text)
        if enter:
            time.sleep(0.1)
            send_keys("{ENTER}")
        time.sleep(0.3)
    return f"Typed {len(text)} characters into [{element}]" + (" and pressed Enter." if enter else ".")


@tool("type_text", "Type text into whatever has keyboard focus right now. method 'paste' (default, fast, any "
      "characters) or 'keys' (real keystrokes, for apps/games that ignore paste).",
      {"text": {"type": "string"}, "enter": {"type": "boolean"}, "method": {"type": "string", "enum": ["paste", "keys"]}},
      ["text"], label="Typing", needs=control_off)
def type_text(ctx, text, enter=False, method="paste"):
    off = control_off()
    if off:
        return off
    fg = find_window(None)
    if fg and _blocked(fg["title"], fg["process"]):
        return "Blocked: the focused app is on the blocked list."
    with foreground(ctx, _target_hwnd(ctx), "Type text into the focused app"):
        paste_text(text) if method == "paste" else type_keys(text)
        if enter:
            send_keys("{ENTER}")
    return f"Typed {len(text)} characters" + (" + Enter." if enter else ".")


@tool("press_keys", "Press a keyboard shortcut or key sequence in the focused app, e.g. 'ctrl+k', 'enter', "
      "'ctrl+shift+n', 'alt, f, n', 'win+d', 'down down enter'. repeat presses it several times.",
      {"keys": {"type": "string"}, "repeat": {"type": "integer"}}, ["keys"], label="Pressing keys", needs=control_off)
def press_keys(ctx, keys, repeat=1):
    off = control_off()
    if off:
        return off
    seq = to_send_keys(keys)
    if not seq:
        return "Error: no keys recognised."
    with foreground(ctx, _target_hwnd(ctx), f"Press {keys}"):
        for _ in range(max(1, min(int(repeat or 1), 50))):
            send_keys(seq)
            time.sleep(0.05)
        time.sleep(0.3)
    return f"Pressed {keys}" + (f" x{repeat}" if repeat and repeat > 1 else "") + "."


@tool("click_at", "Click at screen pixel coordinates (from find_on_screen). button: left | right | middle.",
      {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "double": {"type": "boolean"}}, ["x", "y"], label="Clicking", needs=control_off)
def click_at(ctx, x, y, button="left", double=False):
    off = control_off()
    if off:
        return off
    mouse = _mouse()
    with foreground(ctx, _target_hwnd(ctx), f"Click at ({x}, {y})"):
        if double:
            mouse.double_click(button=button, coords=(int(x), int(y)))
        else:
            mouse.click(button=button, coords=(int(x), int(y)))
        time.sleep(0.4)
    return f"{'Double-clicked' if double else 'Clicked'} {button} at ({x}, {y})."


@tool("drag", "Drag with the left mouse button from one screen point to another (sliders, moving items, drawing).",
      {"x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"}},
      ["x1", "y1", "x2", "y2"], label="Dragging", needs=control_off)
def drag(ctx, x1, y1, x2, y2):
    mouse = _mouse()
    need_screen(ctx, "Drag with the mouse")
    mouse.press(coords=(int(x1), int(y1)))
    steps = 12
    for i in range(1, steps + 1):
        mouse.move(coords=(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps)))
        time.sleep(0.02)
    mouse.release(coords=(int(x2), int(y2)))
    return f"Dragged ({x1},{y1}) -> ({x2},{y2})."


@tool("scroll", "Scroll in the focused window (or at x,y). direction up/down, amount in notches (default 5).",
      {"direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer"},
       "x": {"type": "integer"}, "y": {"type": "integer"}}, ["direction"], label="Scrolling", needs=control_off)
def scroll(ctx, direction, amount=5, x=None, y=None):
    mouse = _mouse()
    if x is None or y is None:
        w = find_window(None)
        r = wrapper(w["hwnd"]).rectangle() if w else None
        x, y = (r.mid_point().x, r.mid_point().y) if r else (500, 500)
    need_screen(ctx, "Scroll with the mouse wheel")
    mouse.scroll(coords=(int(x), int(y)), wheel_dist=(-1 if direction == "down" else 1) * int(amount or 5))
    time.sleep(0.4)
    return f"Scrolled {direction}."


@tool("look_at_screen", "Look at the screen (or one window) with vision and answer a question about it - e.g. "
      "'what is on screen?', 'transcribe the chat', 'is the render finished?'. window: app name, or 'screen'.",
      {"question": {"type": "string"}, "window": {"type": "string"}}, ["question"], label="Looking at screen",
      needs=control_off)
def look_at_screen(ctx, question, window=""):
    rect, what = _target_rect(window or None)
    img, _ = capture(rect)
    ans = services.llm.vision(f"This is a screenshot of {what}. {question}\nAnswer precisely and completely.",
                              _b64(_fit(img, max_pixels=1_400_000, max_w=1680)))
    return f"[{what}] {ans}"


@tool("find_on_screen", "Find a button/icon/area on screen with vision and return its screen coordinates for "
      "click_at. Set click=true to click it right away. Use when read_window doesn't list the control.",
      {"description": {"type": "string"}, "window": {"type": "string"}, "click": {"type": "boolean"}},
      ["description"], label="Finding on screen", needs=control_off)
def find_on_screen(ctx, description, window="", click=False):
    res = locate(description, window or None)
    if not res:
        return f"Couldn't find '{description}' on screen. Try another description, scroll, or read_window."
    x, y, label = res
    if click:
        click_at(ctx, x, y)
        return f"Found '{label or description}' at ({x}, {y}) and clicked it."
    return f"Found '{label or description}' at ({x}, {y}). Use click_at with these coordinates."


@tool("take_screenshot", "Save a screenshot (whole screen or one window) to Documents/Astra/Screenshots.",
      {"window": {"type": "string"}}, label="Taking screenshot", needs=control_off)
def take_screenshot(ctx, window=""):
    rect, what = _target_rect(window or None) if window else (None, "the whole screen")
    img, _ = capture(rect)
    p = workspace_sub("Screenshots") / time.strftime("screenshot-%Y%m%d-%H%M%S.png")
    img.save(p)
    return f"Saved screenshot of {what}: {p}"


@tool("clipboard", "Read or set the clipboard text. action: get | set.",
      {"action": {"type": "string", "enum": ["get", "set"]}, "text": {"type": "string"}}, ["action"],
      label="Clipboard", needs=control_off)
def clipboard(ctx, action, text=""):
    if action == "set":
        clipboard_set_text(text)
        return "Clipboard set."
    t = clipboard_get_text()
    return t[:6000] if t else "(clipboard is empty or not text)"


@tool("wait", "Wait a few seconds (for an app to load, a render to progress...).",
      {"seconds": {"type": "number"}}, ["seconds"], label="Waiting", modes=("astra",))
def wait(ctx, seconds):
    end = time.time() + min(float(seconds or 1), 120)
    while time.time() < end:
        _check_stop(ctx)
        time.sleep(0.25)
    return f"Waited {seconds}s."
