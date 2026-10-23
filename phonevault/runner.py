"""The plug-in pipeline: back up -> merge messages -> pull photos -> report.
Also the watcher that starts it when a phone is plugged in."""
import os
import pathlib
import shutil
import subprocess
import threading
import time
import traceback

from . import config, device, ingest, vault

STATE = {"running": False, "stage": "idle", "udid": None, "messages": [], "connected": [],
         "pending": {}}  # udid -> {"count": n, "checked": time}
_run_lock = threading.Lock()


def _log(msg: str):
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    STATE["messages"] = (STATE["messages"] + [line])[-200:]


def notify(title: str, body: str = ""):
    from .sysutil import notify as _notify
    _notify(title, body)


def last_good_run(con, udid):
    r = con.execute("SELECT max(finished) FROM run WHERE udid = ? AND status = 'ok'", (udid,)).fetchone()
    return r[0]


def run_pipeline(udid: str, cfg: dict | None = None, photos: bool = True) -> dict | None:
    """One full run for one phone. Only one run at a time."""
    if not _run_lock.acquire(blocking=False):
        _log("A run is already in progress")
        return None
    cfg = cfg or config.load()
    archive = config.archive_root(cfg)
    con = vault.connect(archive)
    STATE.update(running=True, udid=udid, stage="backup")
    run_id = con.execute("INSERT INTO run (udid, started, status) VALUES (?,?,?)",
                         (udid, time.time(), "running")).lastrowid
    con.commit()
    result = {"status": "failed"}
    try:
        is_android = device.platform_of(udid) == "android"
        if is_android:
            from . import android
            engine = "adb"
            notify("Backing up your Android phone", "Keep it unlocked and plugged in.")
            _log("Backing up the Android phone (USB debugging) ...")
            snap = android.snapshot(udid, archive, con, log=_log)
            STATE["stage"] = "messages"
            _log("Merging messages into the vault ...")
            stats = android.ingest_snapshot(con, archive, snap, kind="backup", log=_log)
            android.prune_snapshots(archive, udid)
        else:
            notify("Backing up your iPhone", "Unlock it and enter your passcode if asked.")
            got = device.backup(udid, archive, log=_log)
            if not got:
                raise RuntimeError("Backup failed with both engines - see logs/backup.log")
            engine, folder = got
            STATE["stage"] = "messages"
            _log("Merging messages into the vault ...")
            stats = ingest.ingest(con, archive, folder, kind="backup", log=_log)
        new_photos = 0
        note = ""
        if photos:
            STATE["stage"] = "photos"
            _log("Copying new camera-roll photos and videos ...")
            try:
                if is_android:
                    new_photos = android.pull_photos(udid, archive, con, log=_log)
                    recs = android.pull_recordings(udid, archive, con, log=_log)
                    if recs:
                        _log(f"New voice recordings: {recs:,}")
                else:
                    new_photos = device.pull_photos(udid, archive, con, log=_log)
                _log(f"New photos/videos: {new_photos:,}")
                STATE["pending"][udid] = {"count": 0, "checked": time.time()}
            except Exception as e:  # backup + messages are already safe; don't fail the run
                note = f"Photo copy stopped early ({e}); it resumes on the next run. "
                _log("WARNING: " + note)
        prev = con.execute("""SELECT messages_on_phone FROM run WHERE udid=? AND status='ok'
                              ORDER BY finished DESC LIMIT 1""", (udid,)).fetchone()
        if prev and prev[0] and stats["messages_in_source"] < prev[0]:
            note += (f"Phone now holds {stats['messages_in_source']:,} messages, down from {prev[0]:,}. "
                    "Older ones stay safe in the vault.")
            _log("WARNING: " + note)
        con.execute("""UPDATE run SET finished=?, status='ok', engine=?, messages_on_phone=?,
                       new_messages=?, new_photos=?, note=? WHERE id=?""",
                    (time.time(), engine, stats["messages_in_source"], stats["new_messages"],
                     new_photos, note, run_id))
        con.commit()
        summary = f"+{stats['new_messages']:,} messages, +{new_photos:,} photos. Safe to unplug."
        _log("Done: " + summary)
        notify("iPhone backup finished", summary + (" " + note if note else ""))
        result = {"status": "ok", **stats, "new_photos": new_photos, "note": note}
    except Exception as e:
        _log(f"FAILED: {e}")
        traceback.print_exc()
        con.execute("UPDATE run SET finished=?, status='failed', note=? WHERE id=?",
                    (time.time(), str(e)[:500], run_id))
        con.commit()
        notify("iPhone backup failed", str(e)[:200])
    finally:
        STATE.update(running=False, stage="idle")
        con.close()
        _run_lock.release()
    return result


def check_pending(udid: str, cfg: dict | None = None):
    """Count camera-roll items not yet in the archive (skipped while another job runs)."""
    if STATE["running"]:
        return
    cfg = cfg or config.load()
    con = vault.connect(config.archive_root(cfg))
    try:
        if device.platform_of(udid) == "android":
            from . import android
            n = android.pending_photos(udid, con)
        else:
            n = device.pending_photos(udid, con)
        STATE["pending"][udid] = {"count": n, "checked": time.time()}
    except Exception as e:
        print(f"pending check failed: {e}", flush=True)
    finally:
        con.close()


def run_import(folder: str, cfg: dict | None = None) -> dict | None:
    """Merge an existing backup folder (read-only). Shares the one-job-at-a-time lock."""
    if not _run_lock.acquire(blocking=False):
        _log("Another job is running - try again when it finishes")
        return None
    cfg = cfg or config.load()
    archive = config.archive_root(cfg)
    STATE.update(running=True, stage="import", udid=None)
    con = vault.connect(archive)
    try:
        _log(f"Importing {folder} (read-only) ...")
        s = ingest.ingest(con, archive, folder, kind="import", log=_log)
        _log(f"Import done: {s['new_messages']:,} new messages added")
        notify("Import finished", f"{s['new_messages']:,} new messages added to the vault")
        return {"status": "ok", **s}
    except Exception as e:
        _log(f"Import FAILED: {e}")
        return {"status": "failed", "error": str(e)}
    finally:
        STATE.update(running=False, stage="idle")
        con.close()
        _run_lock.release()


def run_photos(udid: str, cfg: dict | None = None):
    if not _run_lock.acquire(blocking=False):
        _log("Another job is running - try again when it finishes")
        return None
    cfg = cfg or config.load()
    archive = config.archive_root(cfg)
    STATE.update(running=True, stage="photos", udid=udid)
    con = vault.connect(archive)
    try:
        _log("Copying new camera-roll photos and videos ...")
        if device.platform_of(udid) == "android":
            from . import android
            n = android.pull_photos(udid, archive, con, log=_log)
        else:
            n = device.pull_photos(udid, archive, con, log=_log)
        _log(f"Photos done: {n:,} new")
        STATE["pending"][udid] = {"count": 0, "checked": time.time()}
        notify("Photo copy finished", f"{n:,} new photos/videos")
    except Exception as e:
        _log(f"Photo copy FAILED: {e}")
    finally:
        STATE.update(running=False, stage="idle")
        con.close()
        _run_lock.release()


def watch(cfg: dict | None = None, stop: threading.Event | None = None):
    """Poll for plugged-in phones; back up listed ones whose last good run is old enough."""
    cfg = cfg or config.load()
    stop = stop or threading.Event()
    while not config.is_setup(cfg) and not stop.is_set():  # wait for first-run setup in the app
        STATE["connected"] = device.list_devices()
        stop.wait(cfg["poll_seconds"])
    archive = config.archive_root(cfg)
    seen = set()
    last_check = {}
    _log(f"Watching for iPhones (auto-backup for: {', '.join(cfg['devices']) or 'none listed'})")
    while not stop.is_set():
        devs = device.list_devices()
        STATE["connected"] = devs
        devs = [d for d in devs if d.get("state", "ready") == "ready"]
        now = {d["udid"] for d in devs}
        for d in devs:
            if d["udid"] in seen or d["udid"] not in cfg["devices"] or not cfg.get("auto_backup", True):
                continue
            con = vault.connect(archive)
            last = last_good_run(con, d["udid"])
            con.close()
            gap = cfg["min_gap_hours"] * 3600
            if last and time.time() - last < gap:
                _log(f"{d['name'] or d['udid']} connected; last backup is under "
                     f"{cfg['min_gap_hours']} h old, skipping")
            else:
                _log(f"{d['name'] or d['udid']} connected ({d.get('os_label') or 'iOS ' + str(d['ios'])}) - starting run")
                run_pipeline(d["udid"], cfg)
        for d in devs:  # refresh "files that need to be backed up" every 2 minutes while plugged in
            if time.time() - last_check.get(d["udid"], 0) >= cfg.get("pending_check_seconds", 120):
                last_check[d["udid"]] = time.time()
                check_pending(d["udid"], cfg)
        for u in set(last_check) - now:
            last_check.pop(u)  # check right away next time it's plugged in
        seen = now  # a phone counts as new again after it's unplugged
        stop.wait(cfg["poll_seconds"])


def _stage(src: pathlib.Path, dst: pathlib.Path) -> bool:
    """Working copy of a backup for the restore, so the original folder is never touched.
    The numbered data folders are hard-linked (instant, no extra space; the restore only reads
    them); the top-level files the phone link may rewrite are real copies. Falls back to a
    full copy if the archive is on a different drive. Returns True if hard links were used."""
    def link_or_copy(a, b):
        try:
            os.link(a, b)
        except OSError:
            shutil.copy2(a, b)
    dst.mkdir(parents=True)
    linked = True
    for item in src.iterdir():
        if item.is_dir():
            try:
                shutil.copytree(item, dst / item.name, copy_function=os.link)
            except (OSError, shutil.Error):
                shutil.rmtree(dst / item.name, ignore_errors=True)
                need = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                if shutil.disk_usage(dst).free < need + (1 << 30):
                    raise RuntimeError("Not enough free space on the archive drive to stage this backup.")
                linked = False
                shutil.copytree(item, dst / item.name, copy_function=link_or_copy)
        else:
            shutil.copy2(item, dst / item.name)
    return linked


def run_encrypt(udid: str, password: str, cfg: dict | None = None) -> dict | None:
    """Turn on encrypted backups: password goes to the system password vault first,
    then to the phone. Removed from the vault again if the phone refuses."""
    from . import passwords
    if not _run_lock.acquire(blocking=False):
        _log("Another job is running - try again when it finishes")
        return None
    cfg = cfg or config.load()
    archive = config.archive_root(cfg)
    STATE.update(running=True, stage="encrypt", udid=udid)
    try:
        passwords.put(udid, password)
        _log("Password saved in the system password vault. Turning on encryption - "
             "unlock the phone and enter its passcode if it asks ...")
        device.enable_encryption(udid, archive, password)
        _log("Encrypted backups are ON. The next backup is a full one (it takes longer once).")
        notify("Encrypted backups on", "Call history, Health and saved passwords are now included.")
        return {"status": "ok"}
    except Exception as e:
        if device.will_encrypt(udid):  # the phone did switch it on: keep the password
            _log(f"WARNING: {e} - but the phone reports encryption ON, so the saved password is kept.")
            return {"status": "ok", "note": str(e)}
        passwords.remove(udid)
        _log(f"Encryption NOT turned on: {e}")
        notify("Encryption not turned on", str(e)[:200])
        return {"status": "failed", "error": str(e)}
    finally:
        STATE.update(running=False, stage="idle")
        _run_lock.release()


def run_restore(udid: str, source_folder: str, remove: bool = False, cfg: dict | None = None) -> dict | None:
    """Put a saved backup back on the phone. Order: check -> copy the chosen backup aside ->
    safety backup of the phone (kept as a dated snapshot) -> restore."""
    from packaging.version import Version
    from . import passwords
    if not _run_lock.acquire(blocking=False):
        _log("Another job is running - try again when it finishes")
        return None
    cfg = cfg or config.load()
    archive = config.archive_root(cfg)
    STATE.update(running=True, stage="restore-check", udid=udid)
    con = vault.connect(archive)
    staging = archive / "restore-staging"
    try:
        bi = ingest.backup_info(source_folder)
        phone = next((d for d in device.list_devices() if d["udid"] == udid), None)
        if not phone:
            raise RuntimeError("The iPhone isn't plugged in.")
        if bi["ios"] and phone["ios"] and Version(bi["ios"]) > Version(phone["ios"]):
            raise RuntimeError(f"That backup is from iOS {bi['ios']}; the phone runs {phone['ios']}. "
                               "A backup can only go onto the same or a newer iOS.")
        password = passwords.get(bi["udid"]) if bi["encrypted"] else None
        if bi["encrypted"] and not password:
            raise RuntimeError("That backup is encrypted and its password isn't saved.")

        STATE["stage"] = "restore-copy"
        _log(f"Copying the chosen backup aside (the original isn't touched): {source_folder}")
        shutil.rmtree(staging, ignore_errors=True)
        linked = _stage(pathlib.Path(source_folder), staging / bi["udid"])
        _log("Staged with hard links (no extra disk space)" if linked else "Staged as a full copy")

        STATE["stage"] = "backup"
        _log("Safety backup of the phone first, so this restore can be undone ...")
        got = device.backup(udid, archive, log=_log)
        if not got:
            raise RuntimeError("Safety backup failed, so nothing was restored.")
        ingest.ingest(con, archive, got[1], kind="backup", log=_log)
        snap = archive / "snapshots" / time.strftime("pre-restore-%Y%m%d-%H%M") / udid
        _log(f"Keeping the safety backup as {snap.parent.name}")
        shutil.copytree(got[1], snap)
        con.execute("""INSERT INTO run (udid, started, finished, status, engine, note) VALUES (?,?,?,?,?,?)""",
                    (udid, time.time(), time.time(), "ok", got[0], f"Safety backup before restore, kept as {snap.parent.name}"))
        con.commit()

        STATE["stage"] = "restore"
        notify("Restoring your iPhone", "Keep it plugged in and unlocked. It restarts when done.")
        _log("Restoring - keep the phone plugged in. It restarts when finished.")
        device.restore(udid, staging, bi["udid"], password, remove, log=_log)
        con.execute("""INSERT INTO run (udid, started, finished, status, engine, note) VALUES (?,?,?,?,?,?)""",
                    (udid, time.time(), time.time(), "restored", "restore",
                     f"Restored {source_folder}. Undo: restore snapshot {snap.parent.name}"))
        con.commit()
        _log(f"Restore sent. The phone restarts and finishes setting up. Undo point: {snap.parent.name}")
        notify("Restore finished", "The iPhone restarts and finishes on its own.")
        return {"status": "ok", "snapshot": str(snap)}
    except Exception as e:
        _log(f"Restore STOPPED: {e}")
        notify("Restore stopped", str(e)[:200])
        return {"status": "failed", "error": str(e)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        STATE.update(running=False, stage="idle")
        con.close()
        _run_lock.release()


def run_android_import(paths: list[str], udid: str, name: str | None = None, cfg: dict | None = None):
    """Import SMS Backup & Restore files (read-only) into one phone's archive."""
    from . import android
    if not _run_lock.acquire(blocking=False):
        _log("Another job is running - try again when it finishes")
        return None
    cfg = cfg or config.load()
    archive = config.archive_root(cfg)
    STATE.update(running=True, stage="import", udid=udid)
    con = vault.connect(archive)
    try:
        _log(f"Importing Android messages export: {', '.join(pathlib.Path(p).name for p in paths)} (read-only) ...")
        snap = android.xml_to_snapshot(paths, archive, udid, name)
        s = android.ingest_snapshot(con, archive, snap, kind="import", log=_log, udid=udid)
        _log(f"Import done: {s['new_messages']:,} new messages added")
        notify("Import finished", f"{s['new_messages']:,} new messages added to the vault")
        return {"status": "ok", **s}
    except Exception as e:
        _log(f"Import FAILED: {e}")
        return {"status": "failed", "error": str(e)}
    finally:
        STATE.update(running=False, stage="idle")
        con.close()
        _run_lock.release()


def run_android_restore(udid: str, source_udid: str, messages: bool, photos: bool, cfg: dict | None = None):
    """Put archived messages/calls (as an SMS Backup & Restore file) and photos onto an Android phone.
    Nothing on the phone is deleted or overwritten."""
    from . import android
    if not _run_lock.acquire(blocking=False):
        _log("Another job is running - try again when it finishes")
        return None
    cfg = cfg or config.load()
    archive = config.archive_root(cfg)
    STATE.update(running=True, stage="restore", udid=udid)
    con = vault.connect(archive)
    try:
        if messages:
            out = archive / "exports" / source_udid
            files = android.export_xml(con, archive, source_udid, out, log=_log)
            for f in files:
                where = android.push(udid, f, "/sdcard/Download/PhoneVault")
                _log(f"Copied to the phone: {where}")
            _log("On the phone: open SMS Backup & Restore > Restore > choose the files in Download/PhoneVault.")
        if photos:
            n = android.push_photos(udid, archive, con, source_udid, log=_log)
            _log(f"Photos copied to the phone (DCIM/PhoneVault): {n:,}")
        notify("Copied to your Android phone", "Messages: finish in SMS Backup & Restore. Photos: in DCIM/PhoneVault.")
        return {"status": "ok"}
    except Exception as e:
        _log(f"Copy to phone STOPPED: {e}")
        return {"status": "failed", "error": str(e)}
    finally:
        STATE.update(running=False, stage="idle")
        con.close()
        _run_lock.release()
