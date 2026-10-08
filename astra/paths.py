# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Filesystem locations used by AstraNova."""
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "AstraNova"
DATA_NAME = "AstraNova-Public"   # private data folder
OLD_APP_NAME = "Astra"          # v1.x data folder - copied over once so nothing is lost


def _migrate(old: Path, new: Path):
    """First start of AstraNova: copy the old Astra data (settings, memory, chats, browser logins)."""
    if new.exists() or not old.exists():
        return
    try:
        shutil.copytree(old, new, ignore=shutil.ignore_patterns("*.lock", "Singleton*", "lockfile", "*.tmp", "webview"),
                        dirs_exist_ok=True)
    except Exception:  # noqa: BLE001 - a partial copy is still better than nothing
        pass


def app_dir() -> Path:
    """Private app data (settings, database, caches): %APPDATA%/AstraNova."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("APPDATA") or str(Path.home() / ".config"))
    p = base / DATA_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_dir() -> Path:
    """Visible folder for everything AstraNova creates: Documents/AstraNova (keeps Documents/Astra if you had it)."""
    docs = Path.home() / "Documents"
    root = docs if docs.exists() else Path.home()
    old, new = root / OLD_APP_NAME, root / APP_NAME
    p = old if (old.exists() and not new.exists()) else new
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_sub(name: str) -> Path:
    p = workspace_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def documents_dir() -> Path:
    """Files the user hands to AstraNova (attachments for emails etc.)."""
    return workspace_sub("Files")


def browser_profile_dir() -> Path:
    p = app_dir() / "browser-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_path(rel: str) -> Path:
    """Resolve bundled resources both in dev and in a PyInstaller build."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return Path(__file__).resolve().parent.parent / rel


def edition() -> str:
    """This build is always the public edition."""
    return "public"
    env = os.environ.get("ASTRANOVA_EDITION")
    if env:
        return env.strip().lower()
    f = resource_path("edition.txt")
    try:
        return f.read_text(encoding="utf-8").strip().lower() or "personal"
    except OSError:
        return "personal"


def is_public() -> bool:
    return edition() == "public"


def personal_context_path() -> Path:
    return app_dir() / "personal_context.md"
