"""When AstraNova ends, everything it started ends with it.

Two layers:
1. A Windows job object that AstraNova puts itself in at startup, with "kill on close". Every program it starts
   afterwards (its browser, the speech and voice engines, the Ollama server it started, the phone tunnel...)
   joins the job, and Windows ends them all the moment AstraNova's process ends, even when it's killed from
   Task Manager or crashes.
2. On a normal close, a sweep that also ends anything still running from AstraNova's own folders (the install
   folder and %APPDATA%\\AstraNova) or using its browser profiles, and frees the graphics card memory the models
   were using."""
import os
import sys

_job = None
_adopted = set()
HELPERS = {"node.exe", "ollama.exe", "piper.exe", "whisper-cli.exe", "cloudflared.exe", "chrome.exe", "msedge.exe",
           "chromium.exe", "headless_shell.exe"}


def _make_job():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                     "OtherOperationCount", "ReadTransferCount",
                                                     "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None, None
    return job, k32


def bind_children():
    """Layer 1. AstraNova's own helpers (its hidden browser, the Ollama server it started, voice, speech, the phone
    tunnel, hidden commands) are put in a job that Windows ends together with AstraNova. Apps Astra opens for you
    (games, Spotify, files) are NOT in it, so they stay open."""
    global _job
    if _job or sys.platform != "win32":
        return bool(_job)
    try:
        job, k32 = _make_job()
    except Exception:  # noqa: BLE001
        return False
    if not job:
        return False
    _job = (job, k32)
    import threading
    threading.Thread(target=_adopt_loop, daemon=True, name="procs").start()
    return True


def _is_helper(p, dirs, root):
    try:
        name = (p.name() or "").lower()
        exe = (p.exe() or "").lower()
        if any(exe.startswith(d) for d in dirs):
            return True
        if name not in HELPERS:
            return False
        if name in ("node.exe", "ollama.exe"):
            return True                                   # only our own descendants get here (Playwright, our Ollama)
        return root in " ".join(p.cmdline() or []).lower()   # browsers: only the ones using AstraNova's profiles
    except Exception:  # noqa: BLE001
        return False


def _adopt_loop():
    import time
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return
    job, k32 = _job
    me = psutil.Process(os.getpid())
    dirs = _our_dirs()
    from .paths import app_dir
    root = str(app_dir()).lower()
    while True:
        try:
            for c in me.children(recursive=True):
                if c.pid in _adopted:
                    continue
                if _is_helper(c, dirs, root):
                    h = k32.OpenProcess(0x0101, False, c.pid)     # PROCESS_SET_QUOTA | PROCESS_TERMINATE
                    if h:
                        k32.AssignProcessToJobObject(job, h)
                        k32.CloseHandle(h)
                _adopted.add(c.pid)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5)


def _our_dirs():
    from .paths import app_dir
    dirs = [str(app_dir()).lower()]
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable).lower())
    return [d.rstrip("\\/") + os.sep for d in dirs]


def sweep(unload_models=True):
    """Layer 2: end everything AstraNova left running. Never touches your normal browser or other apps."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return 0
    me = os.getpid()
    dirs = _our_dirs()
    profiles = [p for p in ("browser-profile", "discord-profile", "vr-browser")]
    from .paths import app_dir
    root = str(app_dir()).lower()
    victims = []
    try:
        victims += [c for c in psutil.Process(me).children(recursive=True) if _is_helper(c, dirs, root)]
    except Exception:  # noqa: BLE001
        pass
    for p in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if p.info["pid"] == me:
                continue
            exe = (p.info.get("exe") or "").lower()
            cl = " ".join(p.info.get("cmdline") or []).lower()
            if any(exe.startswith(d) for d in dirs) or any(f"{root}{os.sep}{n}" in cl for n in profiles):
                victims.append(p)
        except Exception:  # noqa: BLE001
            continue
    if unload_models:
        try:
            from . import services
            if not services.llm.cloud():
                for n in services.llm.loaded_models():
                    services.llm.unload(n)
        except Exception:  # noqa: BLE001
            pass
    seen, n = set(), 0
    for p in victims:
        if p.pid in seen or p.pid == me:
            continue
        seen.add(p.pid)
        try:
            p.kill()
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def unthrottle_engine():
    """Laptops: Windows' power saving ("EcoQoS") slows background programs right down, and the AI engine runs in
    the background, so replies crawl, worst of all on battery. This opts the engine's processes out of it."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        import psutil
        from ctypes import wintypes

        class STATE(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        st = STATE(1, 0x1, 0x0)       # control execution speed, and turn throttling off
        n = 0
        for p in psutil.process_iter(["pid", "name"]):
            if (p.info.get("name") or "").lower() not in ("ollama.exe", "ollama_llama_server.exe", "llama-server.exe"):
                continue
            if p.pid in _unthrottled:
                continue
            h = k32.OpenProcess(0x0200, False, p.pid)     # PROCESS_SET_INFORMATION
            if h:
                if k32.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st)):   # ProcessPowerThrottling
                    _unthrottled.add(p.pid)
                    n += 1
                k32.CloseHandle(h)
        return n
    except Exception:  # noqa: BLE001
        return 0


_unthrottled = set()
