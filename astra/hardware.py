"""What the PC can run: the graphics card and how much video memory (VRAM) it has, so AstraNova can pick a model
that fits instead of one that spills into system RAM and crawls."""
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
                if gb > 0.5 and (not best or gb > best["vram_gb"]):
                    vendor = "amd" if "amd" in name.lower() or "radeon" in name.lower() else (
                        "intel" if "intel" in name.lower() else "nvidia" if "nvidia" in name.lower() else "other")
                    best = {"name": name, "vram_gb": round(gb, 1), "vendor": vendor}
        return best
    except Exception:  # noqa: BLE001
        return None


def gpu():
    """{"name", "vram_gb", "vendor"} of the biggest graphics card, or {"name": "", "vram_gb": 0} if unknown."""
    if "gpu" not in _cache:
        _cache["gpu"] = _nvidia() or _windows_registry() or {"name": "", "vram_gb": 0, "vendor": ""}
    return dict(_cache["gpu"])


# ---- which model fits which card ------------------------------------------------------------------------------
# tier: the smallest VRAM (GB) a setup is meant for. ctx = normal context, max_ctx = how far it may grow.
TIERS = [
    {"min": 22, "model": "qwen3:30b-a3b", "vision": "qwen2.5vl:7b", "ctx": 32768, "max_ctx": 65536, "loaded": 2},
    {"min": 15, "model": "qwen3:14b", "vision": "qwen2.5vl:7b", "ctx": 24576, "max_ctx": 49152, "loaded": 2},
    {"min": 11, "model": "qwen3:14b", "vision": "qwen2.5vl:3b", "ctx": 16384, "max_ctx": 24576, "loaded": 1},
    {"min": 7, "model": "qwen3:8b", "vision": "qwen2.5vl:3b", "ctx": 12288, "max_ctx": 16384, "loaded": 1},
    {"min": 0, "model": "qwen3:4b", "vision": "qwen2.5vl:3b", "ctx": 8192, "max_ctx": 12288, "loaded": 1},
]


def recommend(vram_gb=None):
    v = gpu()["vram_gb"] if vram_gb is None else vram_gb
    if not v:            # unknown card: assume a common 8 GB one, safe everywhere
        v = 8
    return next(dict(t) for t in TIERS if v >= t["min"])
