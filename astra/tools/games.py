"""Your Steam and Epic Games libraries: list installed games and launch them (also remotely through the Discord or
Telegram bot, but only when the message comes from the owner account)."""
import difflib
import glob
import json
import os
import re
import sys
import time

from .. import services
from . import tool

IS_WIN = sys.platform == "win32"
SKIP = re.compile(r"redistributable|steamworks|proton|steamvr|soundtrack|dedicated server|sdk|benchmark", re.I)
_cache = {"t": 0, "games": []}


def _steam_root():
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                return winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
        except OSError:
            pass
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam", os.path.expanduser("~/.steam/steam")):
        if os.path.isdir(p):
            return p
    return None


def steam_games():
    root = _steam_root()
    if not root:
        return []
    libs = {os.path.join(root, "steamapps")}
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.exists(vdf):
        for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf, encoding="utf-8", errors="ignore").read()):
            libs.add(os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps"))
    games = []
    for lib in libs:
        for acf in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
            txt = open(acf, encoding="utf-8", errors="ignore").read()
            aid = re.search(r'"appid"\s+"(\d+)"', txt)
            name = re.search(r'"name"\s+"([^"]+)"', txt)
            if aid and name and not SKIP.search(name.group(1)):
                games.append({"name": name.group(1), "store": "Steam", "id": aid.group(1),
                              "launch": f"steam://rungameid/{aid.group(1)}"})
    return games


def epic_games():
    base = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Epic", "EpicGamesLauncher", "Data", "Manifests")
    games = []
    for item in glob.glob(os.path.join(base, "*.item")):
        try:
            d = json.load(open(item, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if d.get("bIsApplication") is False or not d.get("DisplayName"):
            continue
        ns, cid, app = d.get("CatalogNamespace", ""), d.get("CatalogItemId", ""), d.get("AppName", "")
        games.append({"name": d["DisplayName"], "store": "Epic", "id": app,
                      "launch": f"com.epicgames.launcher://apps/{ns}%3A{cid}%3A{app}?action=launch&silent=true"})
    return games


def all_games(refresh=False):
    if refresh or time.time() - _cache["t"] > 300:
        g = steam_games() + epic_games()
        g.sort(key=lambda x: x["name"].lower())
        _cache.update(t=time.time(), games=g)
    return _cache["games"]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"['\u2019]", "", s.lower())).strip()


def match(name):
    games = all_games()
    n = _norm(name)
    exact = [g for g in games if _norm(g["name"]) == n]
    if exact:
        return exact[0], []
    contains = [g for g in games if n and n in _norm(g["name"])]
    if len(contains) == 1:
        return contains[0], []
    # abbreviations like "bg3", "cs2", "gta"
    initials = [g for g in games if "".join(w[0] for w in _norm(g["name"]).split() if w) .startswith(n.replace(" ", ""))]
    pool = contains or initials
    if len(pool) == 1:
        return pool[0], []
    close = difflib.get_close_matches(n, [_norm(g["name"]) for g in games], n=4, cutoff=0.5)
    cands = pool or [g for g in games if _norm(g["name"]) in close]
    return (cands[0] if len(cands) == 1 else None), cands[:6]


def owner_only(ctx):
    if getattr(ctx, "owner", True):
        return None
    return "Only the owner account can launch games."


@tool("list_games", "List installed Steam and Epic Games titles (optional filter).", {"filter": {"type": "string"}},
      label="Checking your games", modes=("astra", "friend"))
def list_games(ctx, filter=""):  # noqa: A002
    if owner_only(ctx):
        return owner_only(ctx)
    games = [g for g in all_games(refresh=True) if _norm(filter) in _norm(g["name"])]
    if not games:
        return "No installed games found." if not filter else f"No installed game matches '{filter}'."
    return f"{len(games)} games:\n" + "\n".join(f"- {g['name']} ({g['store']})" for g in games[:150])


@tool("launch_game", "Start an installed Steam or Epic game by name (abbreviations work, e.g. 'bg3').",
      {"name": {"type": "string"}}, ["name"], label="Launching game", modes=("astra", "friend"))
def launch_game(ctx, name):
    why = owner_only(ctx)
    if why:
        return why
    g, cands = match(name)
    if not g:
        if cands:
            return "Which one? " + "; ".join(f"{c['name']} ({c['store']})" for c in cands)
        return f"'{name}' isn't installed (Steam/Epic). Use list_games."
    if IS_WIN:
        os.startfile(g["launch"])  # noqa: S606
    services.db.log("game", f"Launched {g['name']} ({g['store']}) from {getattr(ctx, 'source', 'app')}")
    return f"Launching {g['name']} on {g['store']}."
