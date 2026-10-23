"""Builds the hand-out zip: PhoneVault-<version>-setup.zip.

Layout inside the zip:
  PhoneVault/
    READ-ME-FIRST.txt
    Install-Windows.cmd      double-click on Windows
    Uninstall-Windows.cmd
    install-linux.sh         bash install-linux.sh  (works from fish too)
    uninstall-linux.sh
    setup/install.ps1, setup/uninstall.ps1
    app/pyproject.toml, app/README.md, app/phonevault/...   (the app itself, plus the song)
"""
import pathlib
import shutil
import stat
import tempfile
import time
import zipfile

from . import __version__, config, sysutil

PKG = pathlib.Path(__file__).parent
DIST = PKG / "_dist"
TOP_FILES = {"READ-ME-FIRST.txt": "READ-ME-FIRST.txt", "Install-Windows.cmd": "Install-Windows.cmd",
             "Uninstall-Windows.cmd": "Uninstall-Windows.cmd", "install-linux.sh": "install-linux.sh",
             "uninstall-linux.sh": "uninstall-linux.sh", "install.ps1": "setup/install.ps1",
             "uninstall.ps1": "setup/uninstall.ps1"}


def _current_song() -> pathlib.Path | None:
    cfg = config.load()
    if not cfg.get("music_enabled", True):
        return None
    p = pathlib.Path(cfg.get("music") or "")
    if cfg.get("music") and p.is_file():
        return p
    from .web import bundled_song
    return bundled_song()


def _downloads() -> pathlib.Path:
    d = pathlib.Path.home() / "Downloads"
    return d if d.is_dir() else pathlib.Path.home()


def make_installer(out_dir: pathlib.Path | None = None, song: str | None = None, no_song: bool = False) -> dict:
    out_dir = out_dir or _downloads()
    out_dir.mkdir(parents=True, exist_ok=True)
    song_path = None if no_song else (pathlib.Path(song).expanduser() if song else _current_song())
    if song_path and (not song_path.is_file() or song_path.suffix.lower().lstrip(".") not in sysutil.AUDIO_EXT):
        raise SystemExit(f"Song not found or not an audio file: {song_path}")

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "PhoneVault"
        app = root / "app"
        shutil.copytree(PKG, app / "phonevault",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "music"))
        shutil.copyfile(DIST / "pyproject.toml", app / "pyproject.toml")
        shutil.copyfile(DIST / "README.md", app / "README.md")
        if song_path:
            m = app / "phonevault" / "static" / "music"
            m.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(song_path, m / song_path.name)
        for src, dst in TOP_FILES.items():
            (root / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DIST / src, root / dst)

        zpath = out_dir / f"PhoneVault-{__version__}-setup.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(root.rglob("*")):
                if f.is_dir():
                    continue
                info = zipfile.ZipInfo(str(f.relative_to(root.parent)).replace("\\", "/"),
                                       time.localtime(f.stat().st_mtime)[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if f.suffix == ".sh" else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16  # keeps install-linux.sh runnable
                z.writestr(info, f.read_bytes())
    import hashlib
    h = hashlib.sha256()
    with open(zpath, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    digest = h.hexdigest()
    # lets the person you send it to check the zip arrived unchanged
    (zpath.parent / (zpath.name + ".sha256")).write_text(f"{digest}  {zpath.name}\n")
    return {"zip": str(zpath), "song": song_path.name if song_path else None, "sha256": digest}
