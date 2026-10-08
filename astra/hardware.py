# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""What the PC can run: the graphics card and how much video memory (VRAM) it has, so AstraNova can pick a model
that fits instead of one that spills into system RAM and crawls."""
import re
import subprocess
import sys

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_cache = {}


def _nvidia():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW).stdout
        best = None
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[1].isdigit():
                gb = int(parts[1]) / 1024
                if not best or gb > best["vram_gb"]:
                    best = {"name": parts[0], "vram_gb": round(gb, 1), "vendor": "nvidia"}
        return best
    except Exception:  # noqa: BLE001
        return None


def _mac():
    """Apple Silicon (M1 and newer): graphics share the Mac's memory, and macOS lets them use about two thirds of
    it. Intel Macs run the model on the processor."""
    import platform
    if platform.machine() != "arm64":
        return {"name": "Intel Mac", "vram_gb": 0, "vendor": "integrated", "integrated": True}
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or "Apple Silicon"
    except Exception:  # noqa: BLE001
        chip = "Apple Silicon"
    usable = ram_gb() * (0.75 if ram_gb() >= 32 else 0.66) - 2    # leave room for macOS and your apps
    return {"name": chip, "vram_gb": round(max(0, usable), 1), "vendor": "apple"}


def _windows_registry():
    """AMD / Intel / any card: Windows keeps the real VRAM size (also above 4 GB) in the display driver's key."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        root = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        best = None
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as cls:
            for i in range(32):
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        try:
                            size = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0]
                        except OSError:
                            size = winreg.QueryValueEx(k, "HardwareInformation.MemorySize")[0]
                        if isinstance(size, bytes):
                            size = int.from_bytes(size[:8], "little")
                        name = winreg.QueryValueEx(k, "DriverDesc")[0]
                except OSError:
                    continue
                gb = int(size) / 1024 ** 3
                if INTEGRATED.search(name):
                    if not best:
                        best = {"name": name, "vram_gb": 0, "vendor": "integrated", "integrated": True}
                    continue
                if gb > 0.5 and (not best or best.get("integrated") or gb > best["vram_gb"]):
                    vendor = "amd" if "amd" in name.lower() or "radeon" in name.lower() else (
                        "intel" if "intel" in name.lower() else "nvidia" if "nvidia" in name.lower() else "other")
                    best = {"name": name, "vram_gb": round(gb, 1), "vendor": vendor}
        return best
    except Exception:  # noqa: BLE001
        return None


# graphics built into the processor (most laptops without a gaming GPU): they share normal RAM and Ollama runs the
# model on the processor there, so they count as "no graphics card"
INTEGRATED = re.compile(r"intel.*\b(uhd|iris|hd graphics)\b|intel\(r\) arc\(tm\) graphics$|\bradeon\(tm\) graphics$|"
                        r"amd radeon graphics$|radeon vega|radeon\(tm\) \d{3}m\b|microsoft basic|virtual|vmware|parsec|citrix",
                        re.I)


def gpu():
    """{"name", "vram_gb", "vendor"} of the biggest graphics card, or {"name": "", "vram_gb": 0} if unknown."""
    if "gpu" not in _cache:
        _cache["gpu"] = (_mac() if sys.platform == "darwin" else None) or _nvidia() or _windows_registry() or \
            {"name": "", "vram_gb": 0, "vendor": ""}
    return dict(_cache["gpu"])


def ram_gb():
    try:
        import psutil
        return round(psutil.virtual_memory().total / 1024 ** 3)
    except Exception:  # noqa: BLE001
        return 16


def cpu_only():
    """True when the model will run on the processor: no graphics card, a built-in one, or one that's too small."""
    g = gpu()
    return g.get("integrated") or g.get("vram_gb", 0) < 3


# ---- which model fits which card ------------------------------------------------------------------------------
# tier: the smallest VRAM (GB) a setup is meant for. ctx = normal context, max_ctx = how far it may grow.
TIERS = [
    {"min": 22, "model": "qwen3:30b-a3b", "vision": "qwen2.5vl:7b", "ctx": 32768, "max_ctx": 65536, "loaded": 2},
    {"min": 15, "model": "qwen3:14b", "vision": "qwen2.5vl:7b", "ctx": 24576, "max_ctx": 49152, "loaded": 2},
    {"min": 11, "model": "qwen3:14b", "vision": "qwen2.5vl:3b", "ctx": 16384, "max_ctx": 24576, "loaded": 1},
    {"min": 7, "model": "qwen3:8b", "vision": "qwen2.5vl:3b", "ctx": 12288, "max_ctx": 16384, "loaded": 1},
    {"min": 3, "model": "qwen3:4b", "vision": "qwen2.5vl:3b", "ctx": 8192, "max_ctx": 12288, "loaded": 1},
    # no usable graphics card (most laptops): a small, quick model, a compact window and lean prompts
    {"min": 0, "model": "qwen3:4b", "vision": "qwen2.5vl:3b", "ctx": 8192, "max_ctx": 8192, "loaded": 1, "light": True},
]


def recommend(vram_gb=None):
    v = gpu()["vram_gb"] if vram_gb is None else vram_gb
    # unknown or built-in graphics: the light setup. A wrong guess upwards makes chats crawl on laptops.
    return next(dict(t) for t in TIERS if v >= t["min"])
