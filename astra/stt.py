"""Speech to text: talk instead of typing.

Two ways, chosen in Settings > Voice:
  windows - Windows voice typing (Win+H). Instant and very accurate, words appear live as you talk.
            Uses Microsoft's online speech service.
  whisper - OpenAI Whisper running on this PC (whisper.cpp, on the CPU so the GPU stays free for the language
            model). Private: nothing leaves the PC. Downloaded once (about 60 MB for 'base').
For Whisper, AstraNova records the microphone itself, stops on its own after you go quiet, and puts the text in
the message box."""
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
import zipfile

import requests

from . import services
from .paths import app_dir

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
RATE = 16000
RELEASES = "https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest"
FALLBACK_ZIP = "https://github.com/ggml-org/whisper.cpp/releases/download/v1.7.6/whisper-bin-x64.zip"
MODELS = {"base": "ggml-base-q5_1.bin", "small": "ggml-small-q5_1.bin"}
HF = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"


def cfg():
    c = services.config.get("voice_input") or {}
    return {"engine": c.get("engine") or "windows", "model": c.get("model") or "base",
            "auto_send": bool(c.get("auto_send")), "language": c.get("language") or "auto"}


# ---- Windows voice typing ------------------------------------------------------------------------------------
def windows_voice_typing():
    """Press Win+H: Windows opens its voice typing bar and types into the focused text box (our message box)."""
    if sys.platform != "win32":
        return False
    import ctypes
    k = ctypes.windll.user32.keybd_event
    VK_LWIN, VK_H, UP = 0x5B, 0x48, 0x0002
    time.sleep(0.12)
    k(VK_LWIN, 0, 0, 0)
    k(VK_H, 0, 0, 0)
    k(VK_H, 0, UP, 0)
    k(VK_LWIN, 0, UP, 0)
    return True


# ---- Whisper (local) -----------------------------------------------------------------------------------------
def _dir():
    p = app_dir() / "whisper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _exe():
    for name in ("whisper-cli.exe", "main.exe", "whisper-cli", "main"):
        for f in _dir().rglob(name):
            return f
    return None


def _download(url, dest, progress, label):
    with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("content-length", 0)), 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(f"{label} {int(done * 100 / total)}%")
        tmp.replace(dest)


def install(progress=lambda s: None):
    """Get whisper.cpp and the speech model (once)."""
    if not _exe():
        url = FALLBACK_ZIP
        try:
            rel = requests.get(RELEASES, timeout=20).json()
            url = next(a["browser_download_url"] for a in rel.get("assets", [])
                       if re.fullmatch(r"whisper-bin-x64\.zip", a.get("name", "")))
        except Exception:  # noqa: BLE001
            pass
        progress("Downloading speech recognition")
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        zipfile.ZipFile(io.BytesIO(r.content)).extractall(_dir() / "bin")
        if not _exe():
            raise RuntimeError("The speech recognition download didn't contain whisper-cli.exe")
    model = _dir() / MODELS.get(cfg()["model"], MODELS["base"])
    if not model.exists():
        _download(HF + model.name, model, progress, "Downloading speech model")
    return True


def ready():
    return bool(_exe()) and (_dir() / MODELS.get(cfg()["model"], MODELS["base"])).exists()


def transcribe(wav_path):
    install()
    exe, model = _exe(), _dir() / MODELS.get(cfg()["model"], MODELS["base"])
    lang = cfg()["language"]
    if lang == "auto":
        lang = (services.config.get("assistant", "language") or "auto")
        lang = lang if re.fullmatch(r"[a-z]{2}", lang or "") else "auto"
    threads = str(max(2, min((os.cpu_count() or 4) - 1, 8)))
    p = subprocess.run([str(exe), "-m", str(model), "-f", str(wav_path), "-l", lang, "-nt", "-np", "-t", threads],
                       capture_output=True, cwd=str(exe.parent), creationflags=NO_WINDOW, timeout=180)
    out = p.stdout.decode("utf-8", "replace")
    text = " ".join(l.strip() for l in out.splitlines() if l.strip() and not l.startswith(("whisper_", "main:", "system_info")))
    text = re.sub(r"\[(BLANK_AUDIO|MUSIC|NOISE|SILENCE)[^\]]*\]|\((?:music|silence)[^)]*\)", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


class Recorder:
    """Records the default microphone until stop() or until you've been quiet for a moment."""

    def __init__(self, emit):
        self.emit = emit
        self.active = False
        self.lock = threading.Lock()

    def start(self, target):
        with self.lock:
            if self.active:
                return False
            self.active = True
        self.target = target
        self.stop_flag = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="stt").start()
        return True

    def stop(self):
        if self.active:
            self.stop_flag.set()

    def _run(self):
        try:
            if not ready():
                install(lambda s: self.emit({"type": "stt", "state": "installing", "text": s, "target": self.target}))
            import sounddevice as sd
            frames, heard, quiet_since, t0 = [], False, None, time.time()
            self.emit({"type": "stt", "state": "listening", "target": self.target})
            with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=1600) as stream:
                while not self.stop_flag.is_set() and time.time() - t0 < 120:
                    data, _ = stream.read(1600)          # 0.1 s
                    frames.append(bytes(data))
                    level = _rms(data)
                    self.emit({"type": "stt", "state": "level", "level": round(min(level / 3000, 1), 3), "target": self.target})
                    if level > 700:
                        heard, quiet_since = True, None
                    elif heard:
                        quiet_since = quiet_since or time.time()
                        if time.time() - quiet_since > 1.6:      # stopped talking
                            break
                    elif time.time() - t0 > 8:                   # never said anything
                        break
            if not heard:
                self.emit({"type": "stt", "state": "done", "text": "", "target": self.target})
                return
            self.emit({"type": "stt", "state": "transcribing", "target": self.target})
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(b"".join(frames))
            try:
                text = transcribe(tmp.name)
            finally:
                os.unlink(tmp.name)
            self.emit({"type": "stt", "state": "done", "text": text, "target": self.target, "auto_send": cfg()["auto_send"]})
        except Exception as e:  # noqa: BLE001
            services.db.log("stt", str(e))
            self.emit({"type": "stt", "state": "error", "text": _friendly(e), "target": self.target})
        finally:
            self.active = False


def _rms(data):
    import array
    a = array.array("h", bytes(data))
    if not a:
        return 0
    step = 4
    s = sum(x * x for x in a[::step])
    return (s / (len(a) / step)) ** 0.5


def _friendly(e):
    t = str(e)
    if "PortAudio" in t or "Invalid device" in t or "No Default Input" in t or "-9996" in t:
        return "No microphone found. Plug one in or pick a default microphone in Windows sound settings."
    if "HTTP" in t or "Connection" in t:
        return "Couldn't download speech recognition. Check the internet connection and try again."
    return f"Speech to text failed: {t[:160]}"
