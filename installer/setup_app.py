"""AstraNova Setup: a small installer with the same look as the app.

AstraNova-Setup.exe carries the built app (payload.zip) and installs it per user, no admin rights needed:
  %LOCALAPPDATA%\\Programs\\AstraNova   (changeable)
plus Start menu / desktop shortcuts and an entry in Windows Settings > Apps so it can be uninstalled normally.
Run with --uninstall (that's what Windows calls) to remove it again.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

APP = "AstraNova"
EXE = "AstraNova.exe"
PUBLISHER = "AstraNova"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AstraNova"
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "AstraNova-Public"


def res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def version():
    try:
        return Path(res("version.txt")).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def default_dir():
    return str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP)


def installed_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return ""


def _ps(script):
    flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                   creationflags=flags, capture_output=True, timeout=60)


def _shortcuts(pairs):
    """Create all shortcuts in ONE hidden PowerShell call (starting PowerShell once per shortcut made setup sit at
    90% for a long time), then refresh Windows' icon cache so the flare shows right away."""
    parts = []
    for lnk, target, workdir in pairs:
        lnk, target, workdir = (str(x).replace("'", "''") for x in (lnk, target, workdir))
        parts.append(f"if(Test-Path '{lnk}'){{Remove-Item '{lnk}' -Force}};$s=$w.CreateShortcut('{lnk}');"
                     f"$s.TargetPath='{target}';$s.WorkingDirectory='{workdir}';$s.IconLocation='{target},0';$s.Save()")
    if parts:
        _ps("$w=New-Object -ComObject WScript.Shell;" + ";".join(parts))
    try:
        subprocess.run(["ie4uinit.exe", "-show"], capture_output=True, timeout=20, creationflags=0x08000000)
    except Exception:  # noqa: BLE001
        pass


def _special(name):
    """Desktop / Programs folder of the current user (works with OneDrive-redirected desktops). No PowerShell."""
    try:
        csidl = {"Desktop": 0x10, "Programs": 0x02}[name]
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except Exception:  # noqa: BLE001
        pass
    return Path.home() / ("Desktop" if name == "Desktop" else r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs")


def _log(msg):
    try:
        with open(Path(os.environ.get("TEMP", ".")) / "AstraNova-Setup.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except OSError:
        pass


def _running(dest=None):
    """Is anything from AstraNova still running? (the app itself, or a helper inside its folder)"""
    try:
        out = subprocess.run(["tasklist", "/fi", f"imagename eq {EXE}", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=15, creationflags=0x08000000).stdout
        return EXE.lower() in out.lower()
    except Exception:  # noqa: BLE001
        return False


def _kill_running(dest=None):
    """Close AstraNova and EVERYTHING it runs before files are replaced: the app, anything started from its install
    folder, and its helpers that use its data folder (hidden browser, speech, voice, tunnel). Then wait until
    Windows has really let go of the files."""
    subprocess.run(["taskkill", "/im", EXE, "/t", "/f"], capture_output=True, creationflags=0x08000000)
    folders = [str(d).replace("'", "''") for d in (dest, DATA_DIR, DATA_DIR.parent / "AstraNova") if d]
    cond = " -or ".join(f"($_.ExecutablePath -like '{f}\\*') -or ($_.CommandLine -like '*{f}*')" for f in folders)
    _ps(f"Get-CimInstance Win32_Process | Where-Object {{ $_.ProcessId -ne $PID -and $_.ProcessId -ne {os.getpid()} -and "
        "@('uninstall.exe','AstraNova-Setup.exe') -notcontains $_.Name -and (" + cond + ") } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    for _ in range(20):
        if not _running():
            break
        time.sleep(0.5)
    time.sleep(0.6)


class Api:
    def __init__(self, uninstall=False):
        self.window = None
        self.uninstall_mode = uninstall
        self.target = ""
        self.cancelled = False

    def _emit(self, **e):
        """Progress to the window. Never waits for the window to answer (that could freeze setup), and sends at
        most ~12 updates a second."""
        if not self.window:
            return
        now = time.time()
        final = e.get("done") or e.get("error") or e.get("step") != getattr(self, "_last_step", None)
        if not final and now - getattr(self, "_last_emit", 0) < 0.08:
            return
        self._last_emit, self._last_step = now, e.get("step")
        js = f"window.onSetup && window.onSetup({json.dumps(e)})"
        try:
            run = getattr(self.window, "run_js", None)
            (run or self.window.evaluate_js)(js)
        except Exception:  # noqa: BLE001
            pass

    # ---- info ----------------------------------------------------------------------------------
    def info(self):
        return {"version": version(), "dir": installed_dir() or default_dir(), "uninstall": self.uninstall_mode,
                "update": bool(installed_dir()), "installed": installed_dir()}

    def browse(self):
        import webview
        r = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not r:
            return ""
        p = Path(r[0])
        return str(p if p.name.lower() == APP.lower() else p / APP)

    # ---- install -------------------------------------------------------------------------------
    def install(self, target, desktop=True, startmenu=True):
        t = Path(target or default_dir())
        if t.name.lower() != APP.lower():   # always its own folder, so uninstalling can never remove anything else
            t = t / APP
        self.target = target = str(t)
        threading.Thread(target=self._install, args=(target, desktop, startmenu), daemon=True).start()
        return True

    def _install(self, target, desktop, startmenu):
        try:
            self._emit(step="Closing AstraNova", pct=1)
            dest = Path(target)
            _kill_running(dest)
            dest.mkdir(parents=True, exist_ok=True)
            # an update replaces the program files but never touches the user's data (that's in %APPDATA%)
            if (dest / EXE).exists():
                for child in dest.iterdir():
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
            with zipfile.ZipFile(res("payload.zip")) as z:
                items = z.infolist()
                total = sum(i.file_size for i in items) or 1
                done = 0
                last = 0
                for i in items:
                    if self.cancelled:
                        raise RuntimeError("Cancelled.")
                    for attempt in range(4):        # a file still held by Windows: close everything again, retry
                        try:
                            z.extract(i, dest)
                            break
                        except PermissionError:
                            if attempt == 3:
                                raise RuntimeError(f"{i.filename} is still in use. Close AstraNova and try again.")
                            self._emit(step="Waiting for AstraNova to close", pct=None)
                            _kill_running(dest)
                            time.sleep(1.5)
                    done += i.file_size
                    pct = 3 + int(done / total * 85)
                    if pct != last:
                        last = pct
                        self._emit(step="Copying files", pct=pct, file=i.filename[-60:])
            self._emit(step="Adding shortcuts", pct=90)
            exe = dest / EXE
            _shortcuts(([(_special("Programs") / f"{APP}.lnk", exe, dest)] if startmenu else []) +
                       ([(_special("Desktop") / f"{APP}.lnk", exe, dest)] if desktop else []))
            self._emit(step="Registering with Windows", pct=95)
            unin = dest / "uninstall.exe"   # a small separate build, shipped inside the payload
            self._register(dest, unin)
            self._emit(step="Done", pct=100, done=True)
        except Exception as e:  # noqa: BLE001
            _log(f"install failed: {e!r}")
            self._emit(error=str(e))

    def _register(self, dest, unin):
        try:
            import winreg
        except ImportError:
            return
        size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            for name, val in (("DisplayName", APP), ("DisplayVersion", version()), ("Publisher", PUBLISHER),
                              ("DisplayIcon", str(dest / EXE)), ("InstallLocation", str(dest)),
                              ("UninstallString", f'"{unin}" --uninstall'),
                              ("QuietUninstallString", f'"{unin}" --uninstall --quiet')):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, int(size_kb))
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)

    def launch(self):
        exe = Path(self.target or installed_dir() or default_dir()) / EXE
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=0x00000008)   # DETACHED_PROCESS
        self.close()

    # ---- uninstall -----------------------------------------------------------------------------
    def uninstall(self, remove_data=False):
        threading.Thread(target=self._uninstall, args=(remove_data,), daemon=True).start()
        return True

    def _uninstall(self, remove_data):
        try:
            self._emit(step="Closing AstraNova", pct=10)
            _kill_running(Path(installed_dir()) if installed_dir() else None)
            dest = Path(installed_dir() or os.path.dirname(sys.executable))
            if dest.name.lower() != APP.lower() or not (dest / EXE).exists() and not (dest / "uninstall.exe").exists():
                raise RuntimeError(f"AstraNova doesn't seem to be installed in {dest}. Nothing was removed.")
            self._emit(step="Removing shortcuts", pct=30)
            for p in (_special("Desktop") / f"{APP}.lnk", _special("Programs") / f"{APP}.lnk"):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            self._emit(step="Removing files", pct=55)
            me = Path(sys.executable).resolve()
            for child in dest.iterdir() if dest.exists() else []:
                if child.resolve() == me:
                    continue
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
            if remove_data:
                self._emit(step="Removing your AstraNova data", pct=75)
                shutil.rmtree(DATA_DIR, ignore_errors=True)
            try:
                import winreg
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
            except OSError:
                pass
            # the uninstaller can't delete itself while running: a hidden cmd removes the folder once it exits
            subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{dest}"', shell=True,
                             creationflags=0x08000000)
            self._emit(step="Removed", pct=100, done=True)
        except Exception as e:  # noqa: BLE001
            self._emit(error=str(e))

    def close(self):
        if self.window:
            self.window.destroy()


def _msg(text, title="AstraNova Setup", flags=0x40):
    try:
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:  # noqa: BLE001
        return 0


def _close_splash():
    try:
        import pyi_splash  # noqa: PLC0415 - only exists in the built setup
        pyi_splash.close()
    except Exception:  # noqa: BLE001
        pass


def main():
    sys.excepthook = lambda t, v, tb: _log("crash: " + "".join(__import__("traceback").format_exception(t, v, tb)))
    uninstall = "--uninstall" in sys.argv or "uninstall" in os.path.basename(sys.executable).lower()
    if "--quiet" in sys.argv:            # used by AstraNova's own updater: no window at all
        _close_splash()
        api = Api(uninstall)
        target = installed_dir() or default_dir()
        (api._uninstall(False) if uninstall else api._install(target, not installed_dir(), not installed_dir()))
        if "--relaunch" in sys.argv and not uninstall:
            exe = Path(target) / EXE
            if exe.exists():
                subprocess.Popen([str(exe), "--updated"], cwd=str(exe.parent), creationflags=0x00000008)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    api = Api(uninstall)
    try:
        import webview
        api.window = webview.create_window(
            f"{'Uninstall' if uninstall else 'Install'} AstraNova", url=res("setup.html"), js_api=api,
            width=820, height=560, resizable=False, background_color="#E9E6F5")
        webview.start(_close_splash, gui="edgechromium" if sys.platform == "win32" else None)
    except Exception as e:  # noqa: BLE001 - no WebView2 or a broken one: install without the window
        _log(f"window failed, plain mode: {e!r}")
        _close_splash()
        api.window = None
        if uninstall:
            if _msg("Remove AstraNova from this PC? Your chats and settings stay unless you delete them yourself.",
                    flags=0x24) == 6:
                api._uninstall(False)
                _msg("AstraNova was removed.")
            return
        if _msg("Install AstraNova on this PC?\n\nIt goes to your user folder, no admin rights needed.", flags=0x24) != 6:
            return
        api._install(installed_dir() or default_dir(), True, True)
        _msg("AstraNova is installed. You'll find it on your desktop and in the Start menu.")


if __name__ == "__main__":
    main()
