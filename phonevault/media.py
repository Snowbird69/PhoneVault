"""Make phone media playable in any browser: HEIC -> JPEG, CAF/AMR -> M4A, MOV -> MP4.
Converted copies go in <archive>/cache; originals are never changed."""
import hashlib
import pathlib
import shutil
import subprocess
import threading

from .sysutil import NO_WINDOW, find_tool

IMAGE_CONVERT = {".heic", ".heif", ".tif", ".tiff"}
AUDIO_CONVERT = {".caf", ".amr", ".aiff", ".aif"}
VIDEO_CONVERT = {".mov", ".3gp"}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _magick():
    # Windows has its own unrelated convert.exe, so only trust "magick" there
    return find_tool("magick") or (None if NO_WINDOW else shutil.which("convert"))


def _ffmpeg():
    return find_tool("ffmpeg")


def _pillow(src: pathlib.Path, out: pathlib.Path, thumb: bool) -> bool:
    """HEIC/JPEG conversion without ImageMagick (used on Windows and as a fallback)."""
    try:
        from PIL import Image, ImageOps
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name("tmp-" + out.name)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            if thumb:
                im.thumbnail((320, 320))
            im.save(tmp, "JPEG", quality=85 if thumb else 90)
        tmp.replace(out)
        return True
    except Exception:
        return False


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _cache_path(archive: pathlib.Path, src: pathlib.Path, suffix: str) -> pathlib.Path:
    h = hashlib.sha1(str(src).encode()).hexdigest()
    return archive / "cache" / h[:2] / (h + suffix)


def _convert(cmd, out: pathlib.Path) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name("tmp-" + out.name)
    try:
        subprocess.run([*cmd[:-1], str(tmp)], check=True, capture_output=True, timeout=1800, **NO_WINDOW)
        tmp.replace(out)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def playable(archive: pathlib.Path, src: pathlib.Path, thumb: bool = False) -> pathlib.Path:
    """Return a file the browser can show. Falls back to the original if a tool is missing."""
    ext = src.suffix.lower()
    if thumb:
        if ext in VIDEO_CONVERT | {".mp4", ".m4v"}:
            out = _cache_path(archive, src, ".thumb.jpg")
            if not out.exists() and _ffmpeg():
                with _lock_for(str(out)):
                    if not out.exists():
                        _convert([_ffmpeg(), "-y", "-loglevel", "error", "-ss", "0", "-i", str(src),
                                  "-frames:v", "1", "-vf", "scale=320:-2", "OUT"], out)
            return out if out.exists() else src
        m = _magick()
        if ext not in {".gif"}:
            out = _cache_path(archive, src, ".thumb.jpg")
            if not out.exists():
                with _lock_for(str(out)):
                    if not out.exists():
                        if not (m and _convert([m, str(src) + "[0]", "-auto-orient", "-thumbnail", "320x320", "OUT"], out)):
                            _pillow(src, out, thumb=True)
            if out.exists():
                return out
        return src
    if ext in IMAGE_CONVERT:
        out = _cache_path(archive, src, ".jpg")
        if not out.exists():
            with _lock_for(str(out)):
                if not out.exists():
                    m = _magick()
                    if not (m and _convert([m, str(src) + "[0]", "-auto-orient", "-quality", "90", "OUT"], out)):
                        _pillow(src, out, thumb=False)
        return out if out.exists() else src
    if ext in AUDIO_CONVERT and _ffmpeg():
        out = _cache_path(archive, src, ".m4a")
        if not out.exists():
            with _lock_for(str(out)):
                if not out.exists():
                    _convert([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-vn",
                              "-c:a", "aac", "-b:a", "128k", "OUT"], out)
        return out if out.exists() else src
    if ext in VIDEO_CONVERT and _ffmpeg():
        out = _cache_path(archive, src, ".mp4")
        if not out.exists():
            with _lock_for(str(out)):
                if not out.exists():
                    _convert([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "OUT"], out)
        return out if out.exists() else src
    return src


def kind(name: str, mime: str | None) -> str:
    mime = (mime or "").lower()
    ext = pathlib.PurePath(name or "").suffix.lower()
    if mime.startswith("image/") or ext in IMAGE_CONVERT | {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return "image"
    if mime.startswith("video/") or ext in VIDEO_CONVERT | {".mp4", ".m4v"}:
        return "video"
    if mime.startswith("audio/") or ext in AUDIO_CONVERT | {".m4a", ".mp3", ".aac", ".wav"}:
        return "audio"
    return "file"
