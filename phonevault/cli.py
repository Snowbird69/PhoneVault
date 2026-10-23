"""Command line: phonevault <command>. Run `phonevault -h` for the list."""
import argparse
import pathlib
import sys
import threading

from . import config, desktop, device, ingest, runner, vault, web


def _need_setup(cfg):
    if not config.is_setup(cfg):
        sys.exit("Choose where PhoneVault stores backups first: open the app (phonevault gui) "
                 "or run: phonevault init --archive <folder>")


def cmd_init(a):
    cfg = config.load()
    if a.archive:
        cfg["archive"] = str(pathlib.Path(a.archive).expanduser())
    if not config.is_setup(cfg):
        sys.exit("No storage folder yet. Run: phonevault init --archive <folder>  (or use the setup screen in the app)")
    devs = device.list_devices()
    for d in devs:
        if d["udid"] not in cfg["devices"]:
            cfg["devices"].append(d["udid"])
    p = config.save(cfg)
    vault.connect(config.archive_root(cfg)).close()
    print(f"Settings saved to {p}")
    print(f"Archive folder: {cfg['archive']}")
    print("Phones set to auto-backup: " + (", ".join(cfg["devices"]) or "none - plug one in and run init again"))


def cmd_import(a):
    cfg = config.load()
    _need_setup(cfg)
    archive = config.archive_root(cfg)
    con = vault.connect(archive)
    for folder in a.folders:
        print(f"Importing {folder} (read-only) ...")
        try:
            s = ingest.ingest(con, archive, folder, kind="import")
            print(f"  phone {s['udid']}: {s['messages_in_source']:,} messages, {s['new_messages']:,} new to the vault")
        except ingest.BackupError as e:
            print(f"  skipped: {e}")
    con.close()


def cmd_backup(a):
    cfg = config.load()
    _need_setup(cfg)
    devs = device.list_devices()
    udid = (a.udid or (devs[0]["udid"] if devs else "")).upper()
    if not udid:
        sys.exit("No iPhone plugged in.")
    r = runner.run_pipeline(udid, cfg, photos=not a.no_photos)
    sys.exit(0 if r and r["status"] == "ok" else 1)


def cmd_photos(a):
    cfg = config.load()
    _need_setup(cfg)
    devs = device.list_devices()
    udid = (a.udid or (devs[0]["udid"] if devs else "")).upper()
    if not udid:
        sys.exit("No iPhone plugged in.")
    con = vault.connect(config.archive_root(cfg))
    n = device.pull_photos(udid, config.archive_root(cfg), con)
    print(f"New photos/videos copied: {n:,}")


def cmd_serve(a):
    web.serve(config.load())


def cmd_run(a):
    cfg = config.load()
    if desktop._port_open(int(cfg["port"])):
        print(f"PhoneVault is already running on port {cfg['port']}.")
        return
    web.serve(cfg, block=False)
    stop = threading.Event()
    try:
        runner.watch(cfg, stop)
    except KeyboardInterrupt:
        stop.set()


def cmd_gui(a):
    how = desktop.open_window(int(config.load()["port"]))
    print(f"Opened PhoneVault ({how}).")


def cmd_install_desktop(a):
    if a.remove:
        gone = desktop.remove_desktop_entry()
        print("Removed: " + (", ".join(gone) if gone else "nothing was installed"))
        return
    r = desktop.install_desktop_entry(desktop_icon=not a.no_desktop_icon)
    print(f"Added to the app menu: {r['path']}")
    if r["desktop"]:
        print(f"Added a Desktop icon: {r['desktop']}")


def cmd_autostart(a):
    r = desktop.set_autostart(a.state == "on")
    if not r["ok"]:
        print("Couldn't change it: " + (r["error"] or "unknown error"))
        raise SystemExit(1)
    print("PhoneVault starts at login: " + ("yes" if r["enabled"] else "no"))
    if a.state == "on":
        print("Start it now too:  systemctl --user start phonevault")


def cmd_make_installer(a):
    from . import installer
    out = installer.make_installer(pathlib.Path(a.out).expanduser() if a.out else None,
                                   song=a.song, no_song=a.no_song)
    print(f"Installer zip: {out['zip']}")
    print(f"Song inside: {out['song'] or 'none'}")
    print(f"SHA-256: {out['sha256']}  (also saved next to the zip as .sha256)")
    print("Copy it to the other computer, unzip it, then run Install-Windows.cmd (Windows) or install-linux.sh (Linux).")


def cmd_uninstall(a):
    r = desktop.set_autostart(False)
    gone = desktop.remove_desktop_entry()
    print("Start at login: off" if r.get("ok") else f"Start at login: {r.get('error')}")
    print("Removed shortcuts: " + (", ".join(gone) if gone else "none found"))
    print(f"Kept: your settings ({config.config_path()}) and your archive folder.")


def cmd_status(a):
    cfg = config.load()
    _need_setup(cfg)
    con = vault.connect(config.archive_root(cfg))
    print(f"Archive: {cfg['archive']}")
    for d in con.execute("SELECT * FROM device"):
        n = con.execute("SELECT count(*) FROM message WHERE udid=?", (d["udid"],)).fetchone()[0]
        p = con.execute("SELECT count(*) FROM photo WHERE udid=?", (d["udid"],)).fetchone()[0]
        print(f"  {d['name'] or ''} {d['udid']}: {n:,} messages, {p:,} photos/videos")
    for r in con.execute("SELECT * FROM run ORDER BY id DESC LIMIT 5"):
        print(f"  run {r['id']}: {r['status']} engine={r['engine']} new_msgs={r['new_messages']} "
              f"new_photos={r['new_photos']} {r['note'] or ''}")


def _quiet_logging():
    """pythonw.exe (Windows, no console) has no stdout/stderr: send output to a log file instead."""
    if sys.stdout is None or sys.stderr is None:
        from .sysutil import config_dir
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = open(d / "phonevault.log", "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f


def main():
    _quiet_logging()
    p = argparse.ArgumentParser(prog="phonevault", description="iPhone backup, message archive and viewer")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="create settings; adds any plugged-in iPhone to auto-backup")
    s.add_argument("--archive", help="storage folder for backups, e.g. /mnt/storage/PhoneVault")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("import", help="merge existing backup folder(s) into the vault, read-only")
    s.add_argument("folders", nargs="+", help="backup folder(s) named after the phone's UDID")
    s.set_defaults(func=cmd_import)
    s = sub.add_parser("backup", help="back up the plugged-in iPhone now, then merge and pull photos")
    s.add_argument("--udid")
    s.add_argument("--no-photos", action="store_true")
    s.set_defaults(func=cmd_backup)
    s = sub.add_parser("photos", help="copy new camera-roll photos only")
    s.add_argument("--udid")
    s.set_defaults(func=cmd_photos)
    sub.add_parser("serve", help="viewer only, at http://127.0.0.1:8737").set_defaults(func=cmd_serve)
    sub.add_parser("run", help="viewer + auto-backup on plug-in (what the service runs)").set_defaults(func=cmd_run)
    sub.add_parser("status", help="counts and recent runs").set_defaults(func=cmd_status)
    sub.add_parser("gui", help="open the PhoneVault app window (starts it if needed)").set_defaults(func=cmd_gui)
    s = sub.add_parser("install-desktop", help="add PhoneVault to the app menu and the Desktop")
    s.add_argument("--no-desktop-icon", action="store_true", help="app menu only")
    s.add_argument("--remove", action="store_true", help="remove the app-menu entry, Desktop icon and icon file")
    s.set_defaults(func=cmd_install_desktop)
    s = sub.add_parser("autostart", help="start PhoneVault in the background at login: on or off")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(func=cmd_autostart)
    s = sub.add_parser("make-installer", help="build a zip that installs PhoneVault on another Windows or Linux computer")
    s.add_argument("--out", help="folder to write the zip to (default: Downloads)")
    s.add_argument("--song", help="song file to include (default: the song playing in the app now)")
    s.add_argument("--no-song", action="store_true", help="don't include a song")
    s.set_defaults(func=cmd_make_installer)
    sub.add_parser("uninstall", help="remove shortcuts and start-at-login (keeps settings and archive)").set_defaults(func=cmd_uninstall)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
