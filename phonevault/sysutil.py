"""The few things that differ between Windows and Linux: where settings live, file pickers,
notifications, finding tools like ffmpeg, and starting background programs without a window."""
import glob
import os
import pathlib
import shutil
import string
import subprocess
import sys

IS_WIN = os.name == "nt"
# On Windows every helper program must start without flashing a console window
NO_WINDOW = {"creationflags": 0x08000000} if IS_WIN else {}  # CREATE_NO_WINDOW


def config_dir() -> pathlib.Path:
    if IS_WIN:
        return pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home())) / "PhoneVault"
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")) / "phonevault"


def python_console() -> str:
    """python.exe (not pythonw.exe) - helper programs need a real stdout."""
    exe = pathlib.Path(sys.executable)
    if IS_WIN and exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def python_quiet() -> str:
    """pythonw.exe on Windows (no console window), the normal python elsewhere."""
    exe = pathlib.Path(sys.executable)
    if IS_WIN and exe.with_name("pythonw.exe").exists():
        return str(exe.with_name("pythonw.exe"))
    return str(exe)


def app_command(*args) -> list[str]:
    """Command that runs PhoneVault itself from this install."""
    return [python_quiet(), "-m", "phonevault", *args]


def find_tool(name: str) -> str | None:
    """shutil.which, plus where winget puts tools on Windows (works before PATH refreshes)."""
    p = shutil.which(name)
    if p or not IS_WIN:
        return p
    local = os.environ.get("LOCALAPPDATA", "")
    for pattern in (rf"{local}\Microsoft\WinGet\Links\{name}.exe",
                    rf"{local}\Microsoft\WinGet\Packages\*\**\bin\{name}.exe",
                    rf"{os.environ.get('ProgramFiles', '')}\ImageMagick*\{name}.exe"):
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return hits[0]
    return None


def detach(cmd: list[str]):
    """Start a program that keeps running after this one exits."""
    if IS_WIN:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _powershell(script: str, env: dict, timeout=600, wait=True):
    cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script]
    e = {**os.environ, **env}
    if not wait:
        subprocess.Popen(cmd, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
        return ""
    r = subprocess.run(cmd, env=e, capture_output=True, timeout=timeout, **NO_WINDOW)
    return r.stdout.decode("utf-8", "replace").strip()


_PS_DIALOG = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true; ShowInTaskbar = $false }
"""


def pick_folder(title: str, start: str | None = None) -> str | None:
    """Folder chooser. Returns the path, or None if cancelled / no chooser available."""
    start = start or str(pathlib.Path.home())
    if IS_WIN:
        out = _powershell(_PS_DIALOG + r"""
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = $env:PV_TITLE; $d.ShowNewFolderButton = $true; $d.SelectedPath = $env:PV_START
if ($d.ShowDialog($owner) -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }""", {"PV_TITLE": title, "PV_START": start})
        return out or None
    for cmd in (["kdialog", "--title", title, "--getexistingdirectory", start],
                ["zenity", "--file-selection", "--directory", f"--title={title}"]):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            return r.stdout.strip() or None
    return _tk_pick(title, folder=True)


AUDIO_EXT = ["mp3", "m4a", "aac", "flac", "ogg", "opus", "wav"]


def pick_audio(title: str) -> str | None:
    return pick_files(title, "Audio", AUDIO_EXT, multiple=False)


def pick_files(title: str, label: str, exts: list[str], multiple=True):
    """File chooser. Returns a path (multiple=False) or a list of paths; None if cancelled."""
    if IS_WIN:
        out = _powershell(_PS_DIALOG + r"""
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = $env:PV_TITLE; $d.Filter = $env:PV_FILTER; $d.Multiselect = ($env:PV_MULTI -eq '1')
if ($d.ShowDialog($owner) -eq 'OK') { [Console]::Out.Write(($d.FileNames -join "`n")) }""",
                          {"PV_TITLE": title, "PV_FILTER": f"{label}|" + ";".join(f"*.{e}" for e in exts),
                           "PV_MULTI": "1" if multiple else "0"})
        paths = [p for p in out.splitlines() if p.strip()]
    else:
        paths = None
        pats = " ".join(f"*.{e}" for e in exts)
        for cmd in (["kdialog", "--title", title, "--getopenfilename", str(pathlib.Path.home()), f"{label} ({pats})"]
                    + (["--multiple", "--separate-output"] if multiple else []),
                    ["zenity", "--file-selection", f"--title={title}", f"--file-filter={label} | {pats}"]
                    + (["--multiple", "--separator=\n"] if multiple else [])):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                paths = [p for p in r.stdout.splitlines() if p.strip()]
                break
        if paths is None:
            p = _tk_pick(title, folder=False, label=label, exts=exts)
            paths = [p] if p else []
    if not paths:
        return None
    return paths if multiple else paths[0]


def _tk_pick(title, folder, label="Audio", exts=AUDIO_EXT):
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askdirectory(title=title) if folder else filedialog.askopenfilename(
            title=title, filetypes=[(label, " ".join(f"*.{e}" for e in exts))])
        root.destroy()
        return p or None
    except Exception:
        return None


def notify(title: str, body: str = ""):
    """Desktop notification; silently skipped if the system has no way to show one."""
    try:
        if IS_WIN:
            _powershell(r"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true
$n.ShowBalloonTip(8000, $env:PV_T, $env:PV_B, 'Info'); Start-Sleep 9; $n.Dispose()""",
                        {"PV_T": title, "PV_B": body or " "}, wait=False)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "PhoneVault", title, body], timeout=10)
    except Exception:
        pass


def scan_roots(home: pathlib.Path) -> list[pathlib.Path]:
    """Where to look for existing iPhone backups."""
    if IS_WIN:
        roots = [pathlib.Path(os.environ.get("APPDATA", "")) / "Apple Computer" / "MobileSync" / "Backup",
                 home / "Apple" / "MobileSync" / "Backup", home]
        roots += [pathlib.Path(f"{d}:\\") for d in string.ascii_uppercase[2:] if os.path.exists(f"{d}:\\")]
        return roots
    return [home, *pathlib.Path("/mnt").glob("*"), *pathlib.Path("/run/media").glob("*/*"),
            *pathlib.Path("/media").glob("*/*")]


SCAN_SKIP = {"library", "cache", "node_modules", "proc", "sys", "Windows", "Program Files",
             "Program Files (x86)", "ProgramData", "$Recycle.Bin", "AppData", "System Volume Information"}
