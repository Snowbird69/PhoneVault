"""Talking to the phone: list connected phones, run backups, pull camera-roll photos."""
import asyncio
import json
import os
import pathlib
import posixpath
import shutil
import subprocess
import sys
import time

from .sysutil import NO_WINDOW


def pmd3_cmd() -> list[str]:
    """The pymobiledevice3 command-line tool installed next to this Python."""
    override = os.environ.get("PHONEVAULT_PMD3")
    if override:
        return override.split()
    from .sysutil import python_console
    py = pathlib.Path(python_console())
    for name in ("pymobiledevice3", "pymobiledevice3.exe"):
        exe = py.with_name(name)
        if exe.exists():
            return [str(exe)]
    return [str(py), "-m", "pymobiledevice3"]


def list_devices() -> list[dict]:
    """Every phone plugged in now, iPhones and Android phones:
    [{'udid', 'name', 'platform', 'state', 'os_label', 'ios', 'product'}]. Only state 'ready' can be backed up."""
    from . import android
    return ios_devices() + android.list_devices()


def platform_of(udid: str, devs: list[dict] | None = None) -> str | None:
    for d in devs if devs is not None else list_devices():
        if d["udid"] == udid.upper():
            return d.get("platform", "ios")
    return None


def ios_devices() -> list[dict]:
    """iPhones/iPads connected by USB right now."""
    try:
        out = subprocess.run(pmd3_cmd() + ["usbmux", "list"], capture_output=True,
                             text=True, timeout=30, **NO_WINDOW)
        data = json.loads(out.stdout or "[]")
    except Exception:
        return []
    devs = []
    for d in data:
        if d.get("ConnectionType", "USB") != "USB":
            continue
        devs.append({"udid": (d.get("UniqueDeviceID") or d.get("Identifier") or "").upper(),
                     "name": d.get("DeviceName"), "ios": d.get("ProductVersion"),
                     "product": d.get("ProductType"), "platform": "ios", "state": "ready",
                     "os_label": f"iOS {d.get('ProductVersion') or '?'}"})
    return [d for d in devs if d["udid"]]


def _run_logged(cmd: list[str], logfile: pathlib.Path, log) -> int:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as lf:
        lf.write(f"\n=== {time.ctime()} :: {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", **NO_WINDOW)
        for line in proc.stdout:
            lf.write(line)
            lf.flush()
            s = line.strip()
            if "passcode" in s.lower() or "error" in s.lower():
                log(s)
        return proc.wait()


def backup(udid: str, archive: pathlib.Path, log=print) -> tuple[str, pathlib.Path] | None:
    """Incremental backup into <archive>/backups/<UDID>. Falls back to idevicebackup2
    (into <archive>/backups-idb/<UDID>) if pymobiledevice3 fails.
    Returns (engine, backup folder) or None if both failed."""
    logfile = archive / "logs" / "backup.log"
    dest = archive / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    log("Backing up - unlock the phone and enter your passcode if it asks.")
    rc = _run_logged(pmd3_cmd() + ["backup2", "backup", "--udid", udid, str(dest)], logfile, log)
    if rc == 0 and (dest / udid / "Manifest.db").exists():
        return "pymobiledevice3", dest / udid
    log(f"pymobiledevice3 backup failed (exit {rc}); details in {logfile}")
    idb = shutil.which("idevicebackup2")
    if idb:
        dest2 = archive / "backups-idb"
        dest2.mkdir(parents=True, exist_ok=True)
        log("Retrying with idevicebackup2 ...")
        rc = _run_logged([idb, "-u", udid, "backup", str(dest2)], logfile, log)
        if rc == 0 and (dest2 / udid / "Manifest.db").exists():
            return "idevicebackup2", dest2 / udid
        log(f"idevicebackup2 also failed (exit {rc})")
    return None


async def _pull_photos(udid: str, archive: pathlib.Path, vault, log) -> int:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    known = {r["device_path"]: r["size"] for r in
             vault.execute("SELECT device_path, size FROM photo WHERE udid = ?", (udid,))}
    root = archive / "library" / udid / "photos"
    lockdown = await create_using_usbmux(serial=udid)
    new = 0
    try:
        async with AfcService(lockdown) as afc:
            if not await afc.exists("/DCIM"):
                log("No /DCIM folder on the phone")
                return 0
            async for dirpath, _dirs, files in afc.walk("/DCIM"):
                for name in sorted(files):
                    if name.startswith("."):
                        continue
                    rp = posixpath.join(dirpath, name)
                    st = await afc.stat(rp)
                    size = int(st.get("st_size", 0))
                    if known.get(rp) == size:
                        continue
                    local = root / rp.lstrip("/").removeprefix("DCIM/")
                    local.parent.mkdir(parents=True, exist_ok=True)
                    tmp = local.with_name(local.name + ".part")
                    await afc.pull(rp, str(tmp), progress_bar=False)
                    tmp.replace(local)
                    mt = st.get("st_mtime")
                    mtime = mt.timestamp() if hasattr(mt, "timestamp") else local.stat().st_mtime
                    vault.execute("""INSERT INTO photo (udid, device_path, size, mtime, stored_path)
                                     VALUES (?,?,?,?,?) ON CONFLICT(udid, device_path) DO UPDATE SET
                                       size=excluded.size, mtime=excluded.mtime,
                                       stored_path=excluded.stored_path""",
                                  (udid, rp, size, mtime, str(local.relative_to(archive))))
                    new += 1
                    if new % 50 == 0:
                        vault.commit()
                        log(f"  photos copied so far: {new:,}")
    finally:
        vault.commit()
        try:
            await lockdown.close()
        except Exception:
            pass
    return new


def pull_photos(udid: str, archive: pathlib.Path, vault, log=print) -> int:
    """Copy camera-roll files (DCIM) that aren't in the archive yet. Originals untouched."""
    return asyncio.run(_pull_photos(udid, archive, vault, log))


# ---------- backup encryption ----------
async def _will_encrypt(udid: str) -> bool:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
    lockdown = await create_using_usbmux(serial=udid)
    try:
        async with Mobilebackup2Service(lockdown) as bk:
            return await bk.get_will_encrypt()
    finally:
        await lockdown.close()


def will_encrypt(udid: str) -> bool | None:
    """True/False from the phone; None if it can't be asked right now."""
    try:
        return asyncio.run(_will_encrypt(udid))
    except Exception:
        return None


async def _set_password(udid: str, archive: pathlib.Path, new: str):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
    lockdown = await create_using_usbmux(serial=udid)
    try:
        async with Mobilebackup2Service(lockdown) as bk:
            if await bk.get_will_encrypt():
                raise RuntimeError("Backup encryption is already on for this phone. Use 'I already have a password'.")
            d = archive / "backups"
            d.mkdir(parents=True, exist_ok=True)
            await bk.change_password(str(d), new=new)
            if not await bk.get_will_encrypt():
                raise RuntimeError("The phone didn't turn encryption on. Unlock it and try again.")
    finally:
        await lockdown.close()


def enable_encryption(udid: str, archive: pathlib.Path, password: str):
    """Turn on encrypted backups on the phone (it may ask for the passcode)."""
    asyncio.run(_set_password(udid, archive, password))


# ---------- restore ----------
async def _restore(udid, backup_parent, source, password, remove, progress):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
    lockdown = await create_using_usbmux(serial=udid)
    try:
        async with Mobilebackup2Service(lockdown) as bk:
            await bk.restore(str(backup_parent), system=False, reboot=True, copy=False, settings=True,
                             remove=remove, password=password or "", source=source,
                             progress_callback=progress)
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass


def restore(udid: str, backup_parent: pathlib.Path, source: str, password: str | None, remove: bool, log=print):
    """Restore <backup_parent>/<source> onto the plugged-in phone. Replaces the phone's data."""
    last = {"p": -10}

    def progress(p):
        if p - last["p"] >= 10:
            last["p"] = p
            log(f"  restore {int(p)}%")
    asyncio.run(_restore(udid, backup_parent, source, password, remove, progress))


# ---------- what still needs backing up ----------
def photo_group_key(path: str):
    """Files the Photos app shows as one item share a key: IMG_1234.HEIC + IMG_1234.MOV (Live Photo)
    + IMG_E1234.JPG (edited copy). .AAE edit sidecars are ignored."""
    folder, _, name = path.rpartition("/")
    stem, _, ext = name.rpartition(".")
    if ext.lower() == "aae":
        return None
    if stem.upper().startswith("IMG_E"):
        stem = "IMG_" + stem[5:]
    return (folder, stem)


async def _pending_photos(udid: str, known: dict) -> set:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    lockdown = await create_using_usbmux(serial=udid)
    groups = set()
    try:
        async with AfcService(lockdown) as afc:
            if not await afc.exists("/DCIM"):
                return groups
            async for dirpath, _dirs, files in afc.walk("/DCIM"):
                for name in files:
                    if name.startswith("."):
                        continue
                    rp = posixpath.join(dirpath, name)
                    key = photo_group_key(rp)
                    if key is None or key in groups:
                        continue
                    st = await afc.stat(rp)
                    if known.get(rp) != int(st.get("st_size", 0)):
                        groups.add(key)
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass
    return groups


def pending_photos(udid: str, vault) -> int:
    """How many camera-roll items (counted like the Photos app) are on the phone but not in
    the archive yet. Reads file sizes only; nothing is copied."""
    known = {r["device_path"]: r["size"] for r in
             vault.execute("SELECT device_path, size FROM photo WHERE udid = ?", (udid,))}
    return len(asyncio.run(_pending_photos(udid, known)))
