# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Voice output. Runs on the CPU, so it doesn't compete with the language model for your GPU.

engines:
  windows - the voices built into Windows (instant, no download)
  piper   - Piper neural TTS (natural, ~60 MB per voice, still CPU-only and fast)
"""
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import zipfile

import requests

from . import services
from .paths import app_dir
from .text import no_dashes

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PIPER_ZIP = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
PIPER_VOICES = {
    # the most natural ones first ("high" = Piper's best quality, a bit bigger)
    "en_US-lessac-high": "en/en_US/lessac/high/en_US-lessac-high",
    "en_GB-cori-high": "en/en_GB/cori/high/en_GB-cori-high",
    "en_US-ljspeech-high": "en/en_US/ljspeech/high/en_US-ljspeech-high",
    "en_US-ryan-high": "en/en_US/ryan/high/en_US-ryan-high",
    "en_US-kristin-medium": "en/en_US/kristin/medium/en_US-kristin-medium",
    "en_GB-alba-medium": "en/en_GB/alba/medium/en_GB-alba-medium",
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_US-hfc_female-medium": "en/en_US/hfc_female/medium/en_US-hfc_female-medium",
    "en_US-ryan-medium": "en/en_US/ryan/medium/en_US-ryan-medium",
    "en_GB-jenny_dioco-medium": "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium",
    "de_DE-thorsten-high": "de/de_DE/thorsten/high/de_DE-thorsten-high",
    "de_DE-thorsten-medium": "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "sk_SK-lili-medium": "sk/sk_SK/lili/medium/sk_SK-lili-medium",
}
VOICE_LABELS = {
    "en_US-lessac-high": "Lessac, US, warm and clear (best quality)",
    "en_GB-cori-high": "Cori, British, soft (best quality)",
    "en_US-ljspeech-high": "Linda, US, calm narrator (best quality)",
    "en_US-ryan-high": "Ryan, US, male (best quality)",
    "en_US-kristin-medium": "Kristin, US, friendly",
    "en_GB-alba-medium": "Alba, Scottish, gentle",
    "en_US-amy-medium": "Amy, US, bright",
    "en_US-lessac-medium": "Lessac, US (lighter)",
    "en_US-hfc_female-medium": "HFC, US, female",
    "en_US-ryan-medium": "Ryan, US, male (lighter)",
    "en_GB-jenny_dioco-medium": "Jenny, British",
    "de_DE-thorsten-high": "Thorsten, German, male (best quality)",
    "de_DE-thorsten-medium": "Thorsten, German, male (lighter)",
    "sk_SK-lili-medium": "Lili, Slovak, female",
}
_proc = {"p": None}
_lock = threading.Lock()


def piper_dir():
    p = app_dir() / "piper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def piper_exe():
    for c in (piper_dir() / "piper" / "piper.exe", piper_dir() / "piper.exe"):
        if c.exists():
            return c
    return None


def install_piper(voice, progress=lambda s: None):
    if not piper_exe():
        progress("Downloading Piper")
        r = requests.get(PIPER_ZIP, timeout=120)
        r.raise_for_status()
        zipfile.ZipFile(io.BytesIO(r.content)).extractall(piper_dir())
    rel = PIPER_VOICES.get(voice)
    if not rel:
        raise ValueError(f"Unknown voice {voice}")
    for ext in (".onnx", ".onnx.json"):
        dest = piper_dir() / f"{voice}{ext}"
        if not dest.exists():
            progress(f"Downloading voice {voice}")
            with requests.get(HF + rel + ext, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
    return True


def windows_voices():
    """The computer's own voices: Windows' voices, or on a Mac the ones in System Settings > Accessibility >
    Spoken Content (download the "Premium" ones there for the most natural sound)."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
            return [re.split(r"\s{2,}", l.strip())[0] for l in out.splitlines() if l.strip()]
        except Exception:  # noqa: BLE001
            return []
    if sys.platform != "win32":
        return []
    ps = ("Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
          ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW).stdout
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def speakable(text):
    t = no_dashes(text or "")
    t = re.sub(r"```.*?```", " (code) ", t, flags=re.S)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"[A-Za-z]:\\[^\s]+", "a file", t)
    t = re.sub(r"[*_`#>\[\]]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:1500]


def stop():
    with _lock:
        p = _proc["p"]
        _proc["p"] = None
    if p and p.poll() is None:
        p.kill()
    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:  # noqa: BLE001
            pass


def speak(text, who="astra", force=False):
    cfg = services.config.get("voice")
    if not force and not (cfg.get("enabled") and cfg.get(who, True)):
        return False
    t = speakable(text)
    if not t or sys.platform not in ("win32", "darwin"):
        return False
    stop()
    threading.Thread(target=_speak, args=(t, cfg), daemon=True).start()
    return True


def _speak(t, cfg):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(t)
    tmp.close()
    try:
        if sys.platform == "darwin":
            voice = (cfg.get("voice") or "").strip()
            rate = 175 + 18 * max(-10, min(int(cfg.get("rate") or 0), 10))
            p = subprocess.Popen(["say", "-r", str(rate)] + (["-v", voice] if voice and voice in windows_voices() else [])
                                 + ["-f", tmp.name])
            _proc["p"] = p
            p.wait(300)
        elif cfg.get("engine") == "piper" and piper_exe():
            voice = cfg.get("voice") if cfg.get("voice") in PIPER_VOICES else "en_US-lessac-high"
            model = piper_dir() / f"{voice}.onnx"
            if not model.exists():
                install_piper(voice)
            wav = tmp.name.replace(".txt", ".wav")
            with open(tmp.name, "rb") as fin:
                p = subprocess.Popen([str(piper_exe()), "--model", str(model), "--output_file", wav, "--sentence_silence", "0.3"],
                                     stdin=fin, creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _proc["p"] = p
                p.wait(120)
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME)
            os.unlink(wav)
        else:
            voice = (cfg.get("voice") or "").replace("'", "")
            rate = max(-10, min(int(cfg.get("rate") or 0), 10))
            ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  + (f"try {{ $s.SelectVoice('{voice}') }} catch {{}}; " if voice else "")
                  + f"$s.Rate={rate}; $s.Speak((Get-Content -Raw -Encoding UTF8 '{tmp.name}'))")
            p = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW)
            _proc["p"] = p
            p.wait(300)
    except Exception as e:  # noqa: BLE001
        services.db.log("voice", f"speak failed: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
