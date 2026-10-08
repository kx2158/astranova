"""Keeps the public edition up to date on its own.

A while after AstraNova opens (and every few hours while it runs) it quietly looks for a newer release. If there is
one, the new installer is downloaded in the background and checked. Nothing is interrupted: the update is put in
place the next time AstraNova closes, or right away if you press "Restart to update". The installer closes every
AstraNova process first, swaps the files and (on restart) opens AstraNova again. Chats and settings are untouched.

Only the public edition updates itself; a personal build is never replaced by a public release."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, services
from .paths import is_public

_K = (107, 120, 50, 49, 53, 56, 47, 97, 115, 116, 114, 97, 110, 111, 118, 97)
ASSET = "AstraNova-Setup.exe"
_state = {"status": "idle", "version": "", "file": "", "error": "", "checked": 0}
_lock = threading.Lock()


def _src():
    return "https://api.github.com/repos/" + bytes(_K).decode() + "/releases/latest"


def _folder():
    p = Path(tempfile.gettempdir()) / "AstraNova-Update"
    p.mkdir(exist_ok=True)
    return p


def _ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", str(s or ""))[:4]) or (0,)


def enabled():
    return is_public() and getattr(sys, "frozen", False) and \
        (services.config.get("updates") or {}).get("auto", True) is not False


def status():
    return dict(_state, current=__version__, available=enabled() or is_public())


def check(force=False, emit=None):
    """Look for a newer release and download it in the background. Safe to call often."""
    if not (force and is_public() and getattr(sys, "frozen", False)) and not enabled():
        return status()
    if not _lock.acquire(blocking=False):
        return status()
    try:
        return _check(emit)
    finally:
        _lock.release()


def _check(emit):
    import requests
    _state.update(status="checking", error="")
    try:
        r = requests.get(_src(), timeout=20, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": "AstraNova/" + __version__})
        if r.status_code == 404:
            _state.update(status="current", checked=time.time())
            return status()
        r.raise_for_status()
        rel = r.json()
        latest = rel.get("tag_name") or rel.get("name") or ""
        _state["checked"] = time.time()
        services.config.update("updates", {"last_check": _state["checked"]})
        if rel.get("draft") or rel.get("prerelease") or _ver(latest) <= _ver(__version__):
            _state.update(status="current", version="")
            return status()
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == ASSET), None)
        if not asset:
            _state.update(status="current")
            return status()
        ver = ".".join(str(x) for x in _ver(latest))
        dest = _folder() / ASSET
        want = (asset.get("digest") or "").split(":", 1)[-1].lower() if str(asset.get("digest", "")).startswith("sha256:") else ""
        if _state.get("version") == ver and dest.exists() and _state["status"] == "ready":
            return status()
        _state.update(status="downloading", version=ver)
        part = dest.with_suffix(".part")
        h = hashlib.sha256()
        with requests.get(asset["browser_download_url"], stream=True, timeout=60,
                          headers={"User-Agent": "AstraNova/" + __version__}) as resp:
            resp.raise_for_status()
            with open(part, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
        size = part.stat().st_size
        if (asset.get("size") and size != asset["size"]) or (want and h.hexdigest() != want):
            part.unlink(missing_ok=True)
            raise ValueError("the download didn't check out, will try again later")
        os.replace(part, dest)
        _state.update(status="ready", file=str(dest))
        (_folder() / "ready.json").write_text(json.dumps({"version": ver, "file": str(dest)}), encoding="utf-8")
        services.db.log("update", f"version {ver} downloaded, installs when AstraNova closes")
        (emit or services.emit or (lambda e: None))({"type": "update_ready", "version": ver})
    except Exception as e:  # noqa: BLE001
        _state.update(status="error", error=str(e)[:200])
        services.db.log("update", f"check failed: {e}")
    return status()


def _launch(relaunch):
    f = _state.get("file") or ""
    if _state.get("status") != "ready" or not f or not Path(f).exists():
        return False
    args = [f, "--quiet"] + (["--relaunch"] if relaunch else [])
    base = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for flags in (base | 0x01000000, base):  # try to break away from any job first
        try:
            subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=str(Path(f).parent))
            _state["status"] = "installing"
            return True
        except OSError:
            continue
    return False


def install_on_exit():
    """Called as AstraNova closes: if an update is waiting, the installer puts it in place in the background."""
    if not enabled():
        return False
    return _launch(relaunch=False)


def install_now():
    """'Restart to update': installs right away and opens AstraNova again."""
    return _launch(relaunch=True)


def after_update(emit):
    """First start after an update: say so once and tidy the downloaded installer away."""
    try:
        ready = json.loads((_folder() / "ready.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ready = {}
    if not ready or _ver(ready.get("version")) <= _ver(__version__):
        for f in list(_folder().iterdir()):
            try:
                f.unlink()
            except OSError:
                pass             # the installer may still be closing; it's cleaned next time
    if "--updated" in sys.argv:
        emit({"type": "notify", "text": f"AstraNova was updated to version {__version__}"})
