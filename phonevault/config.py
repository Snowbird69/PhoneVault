"""Settings file: ~/.config/phonevault/config.toml on Linux, %APPDATA%\\PhoneVault\\config.toml on Windows
(override with PHONEVAULT_CONFIG)."""
import os
import pathlib
import tomllib

DEFAULTS = {
    "archive": "",                          # where everything is stored (chosen in first-run setup)
    "port": 8737,                           # viewer address: http://127.0.0.1:8737
    "min_gap_hours": 12,                    # skip auto-backup if the last good one is newer
    "poll_seconds": 5,                      # how often the watcher looks for a plugged-in phone
    "devices": [],                          # UDIDs that get backed up automatically
    "auto_backup": True,                    # back up listed phones when plugged in
    "phone_port": 8738,                     # phone view port on the home network
    "music": "",                            # background song (empty = the song bundled with this install)
    "music_enabled": True,                  # play background music at all
}


def config_path() -> pathlib.Path:
    env = os.environ.get("PHONEVAULT_CONFIG")
    if env:
        return pathlib.Path(env)
    from .sysutil import config_dir
    return config_dir() / "config.toml"


def load() -> dict:
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        with open(p, "rb") as f:
            cfg.update(tomllib.load(f))
    cfg["devices"] = list(cfg.get("devices") or [])
    return cfg


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def save(cfg: dict) -> pathlib.Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# PhoneVault settings. Edit with any text editor (e.g. Kate), then restart PhoneVault."]
    for k in DEFAULTS:
        lines.append(f"{k} = {_toml_value(cfg.get(k, DEFAULTS[k]))}")
    p.write_text("\n".join(lines) + "\n")
    return p


def is_setup(cfg: dict) -> bool:
    """First-run setup is done once a storage folder has been chosen."""
    return bool(str(cfg.get("archive") or "").strip())


def archive_root(cfg: dict) -> pathlib.Path:
    if not is_setup(cfg):
        raise RuntimeError("Choose where PhoneVault stores backups first (setup screen in the app).")
    return pathlib.Path(cfg["archive"]).expanduser()
