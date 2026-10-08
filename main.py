# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""AstraNova - Astra + Nova, a local AI assistant and friend that can operate your PC. Entry point."""
import os
import sys

# Make the app's code importable no matter how/where Astra is started:
#  - from source: the folder containing this file
#  - as Astra.exe: the bundle folder (_internal), where the spec also copies the source package
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (getattr(sys, "_MEIPASS", None), _HERE, os.path.dirname(sys.executable)):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)


def _dpi_aware():
    """Physical pixels everywhere, so screen coordinates, screenshots and clicks line up on any scaling."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def selftest():
    """`Astra.exe --selftest`: verify every module is bundled, then exit (used by build.bat)."""
    import importlib
    import pkgutil

    import astra
    from astra import tools
    tools.load_all()
    mods = [m.name for m in pkgutil.walk_packages(astra.__path__, "astra.")]
    for m in mods:
        importlib.import_module(m)
    import webview  # noqa: F401
    import playwright.sync_api  # noqa: F401
    import discord  # noqa: F401
    import spotipy  # noqa: F401
    import mss  # noqa: F401
    import PIL.Image  # noqa: F401
    import psutil  # noqa: F401
    import pyperclip  # noqa: F401
    if sys.platform == "win32":
        import pywinauto.uia_element_info  # noqa: F401
        import pywinauto.keyboard  # noqa: F401
    from astra.paths import resource_path
    assert resource_path("ui/index.html").exists(), "ui folder missing"
    assert len(tools.REGISTRY) >= 80, f"only {len(tools.REGISTRY)} tools registered"
    assert resource_path("ui/mobile/index.html").exists(), "phone app missing"
    return len(mods), len(tools.REGISTRY)


def _diagnostics():
    lines = [f"python {sys.version}", f"frozen={getattr(sys, 'frozen', False)}",
             f"_MEIPASS={getattr(sys, '_MEIPASS', None)}", f"executable={sys.executable}", "sys.path:"]
    lines += ["  " + p for p in sys.path]
    base = getattr(sys, "_MEIPASS", _HERE)
    try:
        lines.append(f"contents of {base}: " + ", ".join(sorted(os.listdir(base))[:80]))
        pkg = os.path.join(base, "astra")
        if os.path.isdir(pkg):
            lines.append("astra/: " + ", ".join(sorted(os.listdir(pkg))))
    except OSError as e:
        lines.append(f"listdir failed: {e}")
    return "\n".join(lines)


def _fresh_webview_dir():
    """The window's own cache, kept per app version, so an update never shows the previous version's screens."""
    import shutil

    from astra import __version__
    from astra.paths import app_dir
    root = app_dir()
    for old in list(root.glob("webview*")):
        if old.name != f"webview-{__version__}":
            shutil.rmtree(old, ignore_errors=True)
    return root / f"webview-{__version__}"


def _finish_reset():
    """A Start over couldn't delete a browser profile that was still open: do it now, before anything opens it."""
    import json
    import shutil

    from astra.paths import app_dir
    marker = app_dir() / "reset.pending"
    if not marker.exists():
        return
    try:
        for name in json.loads(marker.read_text(encoding="utf-8")).get("dirs", []):
            shutil.rmtree(app_dir() / name, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    marker.unlink(missing_ok=True)


def main():
    if "--selftest" in sys.argv:
        out = os.path.join(os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__), "selftest.txt")
        try:
            n_mods, n_tools = selftest()
            msg, code = f"OK: {n_mods} modules, {n_tools} tools", 0
        except Exception as e:  # noqa: BLE001
            import traceback
            msg = "FAILED: " + "".join(traceback.format_exception(type(e), e, e.__traceback__)) + "\n" + _diagnostics()
            code = 1
        with open(out, "w", encoding="utf-8") as f:
            f.write(msg)
        print(msg)
        sys.exit(code)

    _dpi_aware()
    _finish_reset()
    from astra import procs
    procs.bind_children()      # AstraNova's helpers end together with it, even if it's killed
    import webview

    from astra.app import Api
    from astra.paths import app_dir, resource_path

    api = Api()
    from astra import services
    from astra.paths import is_public
    window = webview.create_window(
        "AstraNova",
        url=str(resource_path("ui/index.html")),
        js_api=api,
        width=1440,
        height=920,
        min_size=(980, 660),
        background_color="#09090B" if services.config.get("appearance", "background") == "dark" else "#F8F8FA",
        text_select=True,
    )
    api._attach(window)
    gui = "edgechromium" if sys.platform == "win32" else None
    webview.start(api._boot, gui=gui, debug="--debug" in sys.argv, private_mode=False,
                  storage_path=str(_fresh_webview_dir()))
    # the window is closed: end everything AstraNova started and free the graphics card, then really exit
    try:
        from astra import updater
        updater.install_on_exit()    # a downloaded update goes in now, in the background
    except Exception:  # noqa: BLE001
        pass
    try:
        procs.sweep()
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
