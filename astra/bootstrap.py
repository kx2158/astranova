"""First-run setup: installs the local AI engine (Ollama), downloads the language + screen-vision models, checks
the browser. With a cloud model selected, the engine and model downloads are skipped."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

from . import services

OLLAMA_INSTALLER = "https://ollama.com/download/OllamaSetup.exe"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_ollama():
    exe = shutil.which("ollama")
    if exe:
        return exe
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for p in (Path(base) / "Programs" / "Ollama" / "ollama.exe", Path(base) / "Ollama" / "ollama.exe"):
            if p.exists():
                return str(p)
    return None


def ollama_up():
    try:
        return requests.get("http://127.0.0.1:11434/api/version", timeout=2).ok
    except Exception:
        return False


def start_engine():
    """Start the Ollama server if it isn't running. Tuned for a 16 GB GPU."""
    if services.llm.cloud():
        return True
    tune_ollama()
    if ollama_up():
        return True
    exe = find_ollama()
    if not exe:
        return False
    env = dict(os.environ)
    env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
    env.setdefault("OLLAMA_KV_CACHE_TYPE", "q8_0")
    env.setdefault("OLLAMA_KEEP_ALIVE", "10m")
    from .hardware import recommend
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", str(recommend()["loaded"]))   # 1 on small cards: vision swaps in
    subprocess.Popen([exe, "serve"], env=env, creationflags=NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if ollama_up():
            return True
        time.sleep(0.5)
    return ollama_up()


def _download(url, dest, emit, step):
    with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 512):
                f.write(chunk)
                done += len(chunk)
                emit({"type": "setup", "step": step, "status": "Downloading AI engine",
                      "progress": done / total if total else None})


def _pull(name, step, label, emit):
    if services.llm.has_model(name):
        return
    def prog(status, completed, total):
        emit({"type": "setup", "step": step, "status": f"Downloading {label}",
              "progress": (completed / total) if completed and total else None})
    services.llm.pull(name, prog)


class _Hush:
    def __init__(self):
        self.until = time.time() + 900

    def set_deadline(self, sec):
        self.until = time.time() + sec


def _hush_ollama_welcome():
    """Ollama's installer opens a PowerShell 'welcome' window on first install. Setup should stay in the
    background, so close any console that Ollama opens while we install it (never touches anything else)."""
    h = _Hush()

    def watch():
        try:
            import psutil
        except Exception:  # noqa: BLE001
            return
        consoles = {"powershell.exe", "pwsh.exe", "cmd.exe", "windowsterminal.exe", "conhost.exe", "openconsole.exe"}
        while time.time() < h.until:
            for p in psutil.process_iter(["name", "cmdline", "ppid"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    if name not in consoles:
                        continue
                    cl = " ".join(p.info.get("cmdline") or []).lower()
                    parent = (psutil.Process(p.info["ppid"]).name() or "").lower() if p.info.get("ppid") else ""
                    if "ollama" in cl or "ollama" in parent:
                        p.kill()
                except Exception:  # noqa: BLE001
                    continue
            time.sleep(0.4)
    threading.Thread(target=watch, daemon=True, name="hush").start()
    return h


def tune_ollama():
    """Make Ollama lighter on the graphics card even when it was started by its own tray app (which doesn't get
    AstraNova's settings): flash attention and an 8-bit context cache roughly halve the memory the context takes,
    with no visible quality loss. Saved as user settings, so they apply the next time Ollama starts."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        want = {"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0"}
        changed = False
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            for name, val in want.items():
                try:
                    cur = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    cur = None
                if cur is None:            # never override something the user set themselves
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
                    changed = True
        if changed:   # tell Windows the environment changed (no reboot needed for newly started programs)
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, None)
        return changed
    except Exception:  # noqa: BLE001
        return False


def auto_pick(force=False):
    """First start (or on request): choose the model, vision model and context that fit this graphics card."""
    from .hardware import gpu, recommend
    g, rec = gpu(), recommend()
    m = services.config.get("model")
    if force or not services.config.get("model_auto_done"):
        services.config.update("model", {"name": rec["model"], "vision": rec["vision"], "num_ctx": rec["ctx"],
                                         "max_ctx": rec["max_ctx"], "auto_ctx": True})
        services.config.update("model_auto_done", True)
    return {"gpu": g, "picked": rec, "previous": m.get("name")}


def run(emit):
    try:
        cloud = services.llm.cloud()
        if not cloud and not services.config.get("setup_done"):
            auto_pick()
        # 1. Engine ---------------------------------------------------------------
        emit({"type": "setup", "step": "engine", "status": "Checking AI engine", "progress": None})
        if not cloud:
            if not ollama_up() and not find_ollama():
                if sys.platform != "win32":
                    raise RuntimeError("Please install Ollama from https://ollama.com and restart Astra.")
                dest = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
                _download(OLLAMA_INSTALLER, dest, emit, "engine")
                emit({"type": "setup", "step": "engine", "status": "Installing AI engine", "progress": None})
                hush = _hush_ollama_welcome()
                subprocess.run([str(dest), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/SP-"], check=False,
                               creationflags=NO_WINDOW)
                hush.set_deadline(90)
                for _ in range(120):
                    if find_ollama():
                        break
                    time.sleep(1)
            if not start_engine():
                for _ in range(30):
                    if ollama_up():
                        break
                    time.sleep(1)
            if not ollama_up():
                raise RuntimeError("The AI engine did not start. Restart your PC and open Astra again.")
        emit({"type": "setup", "step": "engine", "status": "Cloud model" if cloud else "AI engine ready",
              "progress": 1, "done": True})

        # 2. Language model ----------------------------------------------------------
        if cloud:
            services.llm.test_cloud()
            emit({"type": "setup", "step": "model", "status": f"{services.llm.model_label()} ready", "progress": 1, "done": True})
            emit({"type": "setup", "step": "vision", "status": "Uses the cloud model", "progress": 1, "done": True})
        else:
            model = services.config.get("model", "name")
            _pull(model, "model", model, emit)
            emit({"type": "setup", "step": "model", "status": "Loading model into GPU", "progress": None})
            services.llm.chat([{"role": "user", "content": "Hi"}], think=False)
            emit({"type": "setup", "step": "model", "status": f"{model} ready", "progress": 1, "done": True})
            # 3. Vision model ------------------------------------------------------------
            vis = services.config.get("model", "vision")
            try:
                _pull(vis, "vision", vis, emit)
                emit({"type": "setup", "step": "vision", "status": f"{vis} ready", "progress": 1, "done": True})
            except Exception as e:  # noqa: BLE001 - optional
                emit({"type": "setup", "step": "vision", "status": f"Skipped ({str(e)[:60]})", "progress": 1, "done": True})

        # 4. Browser --------------------------------------------------------------
        emit({"type": "setup", "step": "browser", "status": "Checking browser", "progress": None})
        try:
            services.browser.check()
        except Exception:
            emit({"type": "setup", "step": "browser", "status": "Installing browser", "progress": None})
            from .tools.browser import install_chromium
            install_chromium()
            services.config.update("browser", {"channel": ""})
            services.browser.check()
        emit({"type": "setup", "step": "browser", "status": "Browser ready", "progress": 1, "done": True})

        services.config.update("setup_done", True)
        emit({"type": "setup_complete"})
    except Exception as e:  # noqa: BLE001
        emit({"type": "setup_error", "text": str(e)})


def pull_model(name, kind, emit):
    """Download a model from Settings and switch to it. kind: 'model' | 'vision'.
    The previous model is unloaded from the graphics card first, otherwise the new one ends up partly in system
    RAM and every reply crawls (this was the 'chat stalls after switching models' bug)."""
    try:
        if not start_engine():
            raise RuntimeError("The AI engine isn't running.")
        if not services.llm.has_model(name):
            services.llm.pull(name, lambda s, c, t: emit({"type": "model_progress", "status": s, "name": name,
                                                          "progress": (c / t) if c and t else None}))
        services.config.update("model", {"name" if kind == "model" else "vision": name})
        services.llm.no_tools = None
        services.llm._ctx_last = (services.llm.base_ctx(), 0)
        m = services.config.get("model")
        services.llm.unload_others(keep=(m.get("name"),) if kind == "model" else (m.get("name"), m.get("vision")))
        emit({"type": "model_ready", "name": name, "kind": kind})
        if kind == "model":
            emit({"type": "engine", "up": True, "warm": False})
            try:
                services.llm.chat([{"role": "user", "content": "hi"}], think=False)
            except Exception:  # noqa: BLE001
                pass
            emit({"type": "engine", "up": True, "warm": True})
    except Exception as e:  # noqa: BLE001
        emit({"type": "model_error", "text": str(e)})
