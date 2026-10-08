"""Music: Spotify through its official Web API (playlists, search, playback, your taste) and Apple Music
through its Windows app (Apple has no public desktop API)."""
import os
import re
import threading
import time

from .. import services
from ..paths import app_dir
from . import desktop, tool

SCOPES = ("playlist-modify-private playlist-modify-public playlist-read-private playlist-read-collaborative "
          "user-read-playback-state user-modify-playback-state user-read-currently-playing user-top-read "
          "user-library-read user-read-recently-played")
_lock = threading.Lock()
_client = {"sp": None, "cid": None}


def spotify_missing():
    if not services.config.get("apps", "spotify_client_id"):
        return "Spotify is not connected (Settings > Apps > Spotify)."
    if not (app_dir() / "spotify_token.json").exists():
        return "Spotify is not connected yet (Settings > Apps > Spotify > Connect)."
    return None


def _auth():
    from spotipy.cache_handler import CacheFileHandler
    from spotipy.oauth2 import SpotifyPKCE
    cid = services.config.get("apps", "spotify_client_id")
    return SpotifyPKCE(client_id=cid, redirect_uri=services.config.get("apps", "spotify_redirect"), scope=SCOPES,
                       cache_handler=CacheFileHandler(cache_path=str(app_dir() / "spotify_token.json")),
                       open_browser=True)


def sp():
    import spotipy
    cid = services.config.get("apps", "spotify_client_id")
    with _lock:
        if _client["sp"] is None or _client["cid"] != cid:
            _client.update(sp=spotipy.Spotify(auth_manager=_auth(), requests_timeout=20, retries=2), cid=cid)
        return _client["sp"]


def connect():
    """Run the browser login once (Settings button). Blocks until the user approves in the browser."""
    _client["sp"] = None
    me = sp().current_user()
    return {"ok": True, "user": me.get("display_name") or me.get("id")}


def disconnect():
    p = app_dir() / "spotify_token.json"
    if p.exists():
        p.unlink()
    _client["sp"] = None


def _track_line(t):
    if not t:
        return "?"
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    return f"{artists} - {t['name']}"


def _find_track(query):
    """Best search hit for 'Artist - Title' style text."""
    q = query.strip()
    m = re.match(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$", q)
    attempts = []
    if m:
        artist, title = m.group(1), m.group(2)
        attempts += [f'track:"{title}" artist:"{artist}"', f"{title} {artist}"]
        attempts += [f'track:"{artist}" artist:"{title}"']  # in case it was "Title - Artist"
    attempts.append(q)
    for a in attempts:
        res = sp().search(q=a, type="track", limit=5)
        items = (res.get("tracks") or {}).get("items") or []
        if items:
            if m:
                words = set(re.findall(r"\w+", (m.group(1) + " " + m.group(2)).lower()))
                items.sort(key=lambda t: -len(words & set(re.findall(r"\w+", _track_line(t).lower()))))
            return items[0]
    return None


def _playlist_id(name_or_id):
    s = name_or_id.strip()
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", s)
    if m:
        return m.group(1), s
    if re.fullmatch(r"[A-Za-z0-9]{22}", s):
        return s, s
    offset = 0
    while offset < 500:
        page = sp().current_user_playlists(limit=50, offset=offset)
        for p in page.get("items") or []:
            if p and p["name"].lower() == s.lower():
                return p["id"], p["name"]
        if not page.get("next"):
            break
        offset += 50
    return None, s


def _add_songs(pid, songs):
    found, missing, uris = [], [], []
    for s in songs:
        t = _find_track(str(s))
        if t:
            uris.append(t["uri"])
            found.append(_track_line(t))
        else:
            missing.append(str(s))
    for i in range(0, len(uris), 100):
        sp().playlist_add_items(pid, uris[i:i + 100])
    return found, missing



@tool("spotify_search", "Search Spotify. type: track | artist | album | playlist.",
      {"query": {"type": "string"}, "type": {"type": "string", "enum": ["track", "artist", "album", "playlist"]},
       "limit": {"type": "integer"}}, ["query"], label="Searching Spotify", modes=("astra", "friend"),
      needs=spotify_missing)
def spotify_search(ctx, query, type="track", limit=8):  # noqa: A002
    res = sp().search(q=query, type=type, limit=max(1, min(int(limit or 8), 20)))
    items = (res.get(type + "s") or {}).get("items") or []
    lines = []
    for it in items:
        if not it:
            continue
        if type == "track":
            lines.append(f"{_track_line(it)}  ({it['album']['name']}, {it['album'].get('release_date', '')[:4]})  {it['uri']}")
        elif type == "artist":
            lines.append(f"{it['name']}  genres: {', '.join(it.get('genres', [])[:4])}  {it['uri']}")
        elif type == "album":
            lines.append(f"{', '.join(a['name'] for a in it['artists'])} - {it['name']} ({it.get('release_date', '')[:4]})  {it['uri']}")
        else:
            lines.append(f"{it['name']} by {it['owner'].get('display_name')}  ({it['tracks']['total']} tracks)  {it['uri']}")
    return "\n".join(lines) or "No results."


@tool("spotify_my_playlists", "List the user's Spotify playlists.", {"limit": {"type": "integer"}},
      label="Reading playlists", needs=spotify_missing)
def spotify_my_playlists(ctx, limit=50):
    page = sp().current_user_playlists(limit=max(1, min(int(limit or 50), 50)))
    return "\n".join(f"{p['name']}  ({p['tracks']['total']} tracks)  {p['external_urls']['spotify']}"
                     for p in page.get("items") or [] if p) or "No playlists."


@tool("spotify_playlist_tracks", "List the songs in a Spotify playlist (name, link or id).",
      {"playlist": {"type": "string"}}, ["playlist"], label="Reading playlist", needs=spotify_missing)
def spotify_playlist_tracks(ctx, playlist):
    pid, name = _playlist_id(playlist)
    if not pid:
        return f"No playlist called '{playlist}'."
    items = sp().playlist_items(pid, limit=100).get("items") or []
    return f"{name}:\n" + "\n".join(f"{i + 1}. {_track_line(x.get('track'))}" for i, x in enumerate(items))


@tool("spotify_my_taste", "The user's top artists and tracks on Spotify plus recently played - use it to make "
      "playlists that fit their taste. time_range: short_term (4 weeks) | medium_term (6 months) | long_term.",
      {"time_range": {"type": "string", "enum": ["short_term", "medium_term", "long_term"]}},
      label="Checking your taste", modes=("astra", "friend"), needs=spotify_missing)
def spotify_my_taste(ctx, time_range="medium_term"):
    a = sp().current_user_top_artists(limit=20, time_range=time_range).get("items") or []
    t = sp().current_user_top_tracks(limit=20, time_range=time_range).get("items") or []
    r = sp().current_user_recently_played(limit=15).get("items") or []
    return ("TOP ARTISTS: " + "; ".join(f"{x['name']} ({', '.join(x.get('genres', [])[:2])})" for x in a) +
            "\n\nTOP TRACKS: " + "; ".join(_track_line(x) for x in t) +
            "\n\nRECENTLY PLAYED: " + "; ".join(_track_line(x.get("track")) for x in r))


@tool("spotify_now_playing", "What is playing on Spotify right now.", label="Checking Spotify",
      modes=("astra", "friend"), needs=spotify_missing)
def spotify_now_playing(ctx):
    cur = sp().current_playback()
    if not cur or not cur.get("item"):
        return "Nothing is playing on Spotify."
    it = cur["item"]
    pos, dur = cur.get("progress_ms", 0) // 1000, it.get("duration_ms", 0) // 1000
    return (f"{'Playing' if cur.get('is_playing') else 'Paused'}: {_track_line(it)} ({pos // 60}:{pos % 60:02d}/"
            f"{dur // 60}:{dur % 60:02d}) on {cur.get('device', {}).get('name', '?')}")


def _open_uri(uri):
    os.startfile(uri)  # noqa: S606  (opens in the Spotify app)


@tool("spotify_play", "Play something on Spotify: a song/artist/album/playlist by name, or a spotify: URI / link. "
      "Without Premium it opens it in the Spotify app instead.",
      {"query": {"type": "string"}, "kind": {"type": "string", "enum": ["track", "artist", "album", "playlist"]}},
      ["query"], label="Playing on Spotify", modes=("astra", "friend"), needs=spotify_missing)
def spotify_play(ctx, query, kind="track"):
    uri = query.strip()
    if not uri.startswith("spotify:"):
        m = re.search(r"open\.spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]{22})", uri)
        if m:
            uri = f"spotify:{m.group(1)}:{m.group(2)}"
        elif kind == "playlist":
            pid, _ = _playlist_id(query)
            if pid:
                uri = f"spotify:playlist:{pid}"
            else:
                res = sp().search(q=query, type="playlist", limit=1)["playlists"]["items"]
                uri = res[0]["uri"] if res and res[0] else None
        elif kind == "track":
            t = _find_track(query)
            uri = t["uri"] if t else None
        else:
            res = sp().search(q=query, type=kind, limit=1)[kind + "s"]["items"]
            uri = res[0]["uri"] if res else None
    if not uri:
        return f"Couldn't find '{query}' on Spotify."
    try:
        if uri.startswith("spotify:track:"):
            sp().start_playback(uris=[uri])
        else:
            sp().start_playback(context_uri=uri)
        return f"Playing {uri}."
    except Exception as e:  # noqa: BLE001 - no Premium / no active device
        _open_uri(uri)
        time.sleep(2.5)
        return f"Opened {uri} in the Spotify app (API playback failed: {str(e)[:120]}). Use media_key play_pause if it didn't start."


@tool("spotify_control", "Control Spotify playback: pause, resume, next, previous, shuffle_on, shuffle_off, volume "
      "(0-100), repeat_on, repeat_off. Needs Premium; otherwise use media_key.",
      {"action": {"type": "string"}, "value": {"type": "integer"}}, ["action"], label="Spotify control",
      modes=("astra", "friend"), needs=spotify_missing)
def spotify_control(ctx, action, value=None):
    a = action.lower()
    s = sp()
    try:
        {"pause": s.pause_playback, "resume": s.start_playback, "play": s.start_playback, "next": s.next_track,
         "previous": s.previous_track, "shuffle_on": lambda: s.shuffle(True), "shuffle_off": lambda: s.shuffle(False),
         "repeat_on": lambda: s.repeat("context"), "repeat_off": lambda: s.repeat("off"),
         "volume": lambda: s.volume(max(0, min(int(value or 50), 100)))}[a]()
    except KeyError:
        return f"Unknown action '{action}'."
    except Exception as e:  # noqa: BLE001
        return f"Spotify refused ({str(e)[:160]}). Without Premium use media_key instead."
    return f"Spotify: {action} done."


# ---- Apple Music (Windows app) ------------------------------------------------------------------------------
@tool("apple_music_search", "Open the Apple Music app, search for something and return the result screen with "
      "numbered controls (then use click_element / right-click > Add to Playlist).",
      {"query": {"type": "string"}}, ["query"], label="Searching Apple Music", needs=desktop.control_off)
def apple_music_search(ctx, query):
    desktop.need_screen(ctx, "Search in the Apple Music app")
    w = desktop.find_window("apple music")
    if not w:
        desktop.launch("Apple Music")
        for _ in range(30):
            time.sleep(0.5)
            w = desktop.find_window("apple music")
            if w:
                time.sleep(2)
                break
    if not w:
        return "Apple Music didn't open. Is it installed (Microsoft Store)?"
    desktop.activate(w)
    root = desktop.wrapper(w["hwnd"])
    box = None
    for el, ctype, name, _r in desktop.walk(root, time.time() + 4):
        if ctype == "Edit" and "search" in name.lower():
            box = el
            break
    if box is None:
        loc = desktop.locate("the Search field in the sidebar", "apple music")
        if not loc:
            return "Couldn't find Apple Music's search field. Use read_window / find_on_screen."
        from pywinauto import mouse
        mouse.click(coords=(loc[0], loc[1]))
    else:
        box.click_input()
    time.sleep(0.3)
    desktop.send_keys("^a{BACKSPACE}")
    desktop.paste_text(query)
    desktop.send_keys("{ENTER}")
    time.sleep(2.5)
    return desktop.describe_window(ctx, desktop.find_window("apple music") or w, max_items=120)


