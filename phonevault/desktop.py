"""Desktop integration: start-menu / app-menu entry, Desktop icon, start at login, app window.
Linux: .desktop files + a systemd user service. Windows: .lnk shortcuts + the per-user Run key."""
import os
import pathlib
import shutil
import socket
import subprocess
import time

from .sysutil import IS_WIN, NO_WINDOW, _powershell, app_command, detach

STATIC = pathlib.Path(__file__).with_name("static")
UNIT = "phonevault.service"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---------------- start at login ----------------
def _unit_path() -> pathlib.Path:
    return pathlib.Path.home() / ".config" / "systemd" / "user" / UNIT


def _systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def autostart_enabled() -> bool:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, "PhoneVault")
                return True
        except OSError:
            return False
    if not shutil.which("systemctl"):
        return False
    try:
        return _systemctl("is-enabled", UNIT).stdout.strip() == "enabled"
    except Exception:
        return False


def _write_unit():
    p = _unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    exe = " ".join(app_command("run"))
    p.write_text("[Unit]\nDescription=PhoneVault - iPhone auto-backup + app\n\n"
                 f"[Service]\nExecStart={exe}\nRestart=on-failure\nRestartSec=10\n\n"
                 "[Install]\nWantedBy=default.target\n")
    _systemctl("daemon-reload")


def set_autostart(enabled: bool) -> dict:
    """Start PhoneVault in the background at login (or stop doing so).
    Doesn't stop the copy that's running now."""
    if IS_WIN:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if enabled:
                cmd = " ".join(f'"{c}"' if " " in c else c for c in app_command("run"))
                winreg.SetValueEx(k, "PhoneVault", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, "PhoneVault")
                except OSError:
                    pass
        return {"ok": True, "enabled": autostart_enabled(), "error": None}
    if not shutil.which("systemctl"):
        return {"ok": False, "error": "systemd isn't available here."}
    if enabled:
        _write_unit()  # rewrite every time so it always points at this install
        r = _systemctl("enable", UNIT)
    else:
        if not _unit_path().exists():
            return {"ok": True, "enabled": False, "error": None}
        r = _systemctl("disable", UNIT)
    ok = r.returncode == 0
    return {"ok": ok, "enabled": autostart_enabled(), "error": None if ok else (r.stderr.strip() or "systemctl failed")}


# ---------------- menu + Desktop icons ----------------
def desktop_dir() -> pathlib.Path:
    """The user's Desktop folder (handles renamed / OneDrive / translated Desktops)."""
    if IS_WIN:
        out = _powershell("[Console]::Out.Write([Environment]::GetFolderPath('Desktop'))", {}, timeout=30)
        return pathlib.Path(out) if out else pathlib.Path.home() / "Desktop"
    if shutil.which("xdg-user-dir"):
        try:
            d = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=10).stdout.strip()
            if d:
                return pathlib.Path(d)
        except Exception:
            pass
    return pathlib.Path.home() / "Desktop"


def _win_start_menu() -> pathlib.Path:
    return pathlib.Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "PhoneVault.lnk"


def _win_shortcut(path: pathlib.Path):
    cmd = app_command("gui")
    _powershell(r"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:PV_LNK)
$s.TargetPath = $env:PV_EXE; $s.Arguments = $env:PV_ARGS; $s.IconLocation = $env:PV_ICO
$s.WorkingDirectory = $env:USERPROFILE; $s.Description = 'Back up your iPhone and browse messages, photos and voicemail'
$s.Save()""", {"PV_LNK": str(path), "PV_EXE": cmd[0], "PV_ARGS": " ".join(cmd[1:]),
               "PV_ICO": str(STATIC / "icons" / "phonevault.ico")}, timeout=60)


def install_desktop_entry(desktop_icon: bool = False) -> dict:
    """Add PhoneVault to the start menu / app menu and, if asked, put an icon on the Desktop."""
    if IS_WIN:
        entry = _win_start_menu()
        entry.parent.mkdir(parents=True, exist_ok=True)
        _win_shortcut(entry)
        out = {"ok": entry.exists(), "path": str(entry), "desktop": None,
               "error": None if entry.exists() else "Windows didn't create the shortcut."}
        if desktop_icon:
            d = desktop_dir() / "PhoneVault.lnk"
            _win_shortcut(d)
            out["desktop"] = str(d) if d.exists() else None
        return out

    share = pathlib.Path.home() / ".local" / "share"
    hicolor = share / "icons" / "hicolor"
    icon_dir = hicolor / "scalable" / "apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STATIC / "phonevault.svg", icon_dir / "phonevault.svg")
    for png in (STATIC / "icons").glob("phonevault-*.png"):
        n = png.stem.split("-")[1]
        d = hicolor / f"{n}x{n}" / "apps"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png, d / "phonevault.png")
    # Full path, so the icon shows even if the icon theme lookup misses it
    icon_ref = hicolor / "256x256" / "apps" / "phonevault.png"
    # KDE caches icon lookups; a stale cache shows a blank icon until it's rebuilt
    (pathlib.Path.home() / ".cache" / "icon-cache.kcache").unlink(missing_ok=True)
    apps = share / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = apps / "phonevault.desktop"
    entry.write_text("[Desktop Entry]\nType=Application\nName=PhoneVault\n"
                     "GenericName=iPhone backup and archive\n"
                     "Comment=Back up your iPhone and browse messages, photos and voicemail\n"
                     f"Exec={' '.join(app_command('gui'))}\nIcon={icon_ref}\nTerminal=false\n"
                     "Categories=Utility;Archiving;\nStartupWMClass=PhoneVault\n")
    entry.chmod(0o755)
    out = {"ok": True, "path": str(entry), "desktop": None}
    if desktop_icon:
        d = desktop_dir()
        d.mkdir(parents=True, exist_ok=True)
        icon = d / "phonevault.desktop"
        shutil.copyfile(entry, icon)
        icon.chmod(0o755)  # KDE only runs Desktop launchers that are marked executable
        out["desktop"] = str(icon)
    for tool in (["update-desktop-database", str(apps)], ["kbuildsycoca6"], ["gtk-update-icon-cache", "-q", str(hicolor)]):
        if shutil.which(tool[0]):
            subprocess.run(tool, capture_output=True, timeout=60)
    return out


def remove_desktop_entry() -> list[str]:
    """Undo install_desktop_entry. Returns what was removed."""
    if IS_WIN:
        paths = [_win_start_menu(), desktop_dir() / "PhoneVault.lnk"]
    else:
        share = pathlib.Path.home() / ".local" / "share"
        paths = [share / "applications" / "phonevault.desktop", desktop_dir() / "phonevault.desktop",
                 *(share / "icons" / "hicolor").glob("*/apps/phonevault.*")]
    gone = []
    for p in paths:
        if p.exists():
            p.unlink()
            gone.append(str(p))
    return gone


# ---------------- opening the app ----------------
def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


APP_BROWSERS = ["chromium", "chromium-browser", "ungoogled-chromium", "google-chrome-stable", "google-chrome",
                "brave", "brave-browser", "vivaldi", "vivaldi-stable", "thorium-browser", "helium-browser",
                "microsoft-edge-stable"]


def _win_browsers() -> list[str]:
    pf, pf86, local = (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
    cands = [rf"{pf86}\Microsoft\Edge\Application\msedge.exe", rf"{pf}\Microsoft\Edge\Application\msedge.exe",
             rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{local}\Google\Chrome\Application\chrome.exe",
             rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe"]
    return [c for c in cands if os.path.exists(c)]


def open_window(port: int):
    """Start PhoneVault if it isn't running, then open it in its own window.
    Chromium-family browsers (incl. Edge) give a separate app window; otherwise a normal tab."""
    if not _port_open(port):
        detach(app_command("run"))
        for _ in range(60):
            if _port_open(port):
                break
            time.sleep(0.25)
    url = f"http://127.0.0.1:{port}/"
    browsers = _win_browsers() if IS_WIN else [shutil.which(b) for b in APP_BROWSERS if shutil.which(b)]
    for b in browsers:
        args = [b, f"--app={url}", "--window-size=1320,860"] + ([] if IS_WIN else ["--class=PhoneVault"])
        if IS_WIN:
            subprocess.Popen(args, **NO_WINDOW)
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return os.path.basename(b)
    if IS_WIN:
        os.startfile(url)
        return "default browser"
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return "xdg-open"
