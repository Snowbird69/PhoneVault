"""Android phones over USB debugging (adb), no root needed.

What Android lets the adb shell read (checked against AOSP source, see README):
  - texts + picture messages (SMS/MMS) and their attachments   content://sms, content://mms, mms/part
    (the shell has READ_SMS; non-default apps see inbox + sent only, which is what a backup wants)
  - RCS from Google Messages: stored as MMS in the same database, so it's included
    (sent RCS may be missing on recent Google Messages builds; Android 17 end-to-end encrypted
    RCS is readable only by the default SMS app - use the SMS Backup & Restore import for those)
  - Samsung "chat" (RCS) messages: content://im/chat, best effort, text only
  - call log (READ_CALL_LOG), contacts (READ_CONTACTS)
  - photos/videos in DCIM, Pictures, Movies; voice recordings in Recordings
Every backup writes a snapshot (snapshot.json + attachment files) that ingest_snapshot() merges
into the same vault tables the iPhone side uses.
"""
import base64
import hashlib
import json
import os
import pathlib
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from .sysutil import IS_WIN, NO_WINDOW, find_tool

MEDIA_DIRS = ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies"]
RECORDING_DIRS = ["/sdcard/Recordings", "/sdcard/Voice Recorder", "/sdcard/Music/Recordings"]
AUDIO_EXT = {".m4a", ".mp3", ".amr", ".3gp", ".wav", ".ogg", ".opus", ".aac", ".flac"}
DEVICE_TMP = "/data/local/tmp/phonevault"
PART_SKIP = {"application/smil"}


class AdbError(RuntimeError):
    pass


# ---------------------------------------------------------------- adb itself
def adb_cmd() -> list[str] | None:
    """The adb program: PHONEVAULT_ADB override, PATH/winget, PhoneVault's own copy, Android SDK."""
    override = os.environ.get("PHONEVAULT_ADB")
    if override:
        return override.split()
    p = find_tool("adb")
    if p:
        return [p]
    exe = "adb.exe" if IS_WIN else "adb"
    home = pathlib.Path.home()
    local = pathlib.Path(os.environ.get("LOCALAPPDATA", home))
    for c in (local / "PhoneVault" / "platform-tools" / exe,
              home / ".local" / "share" / "phonevault" / "platform-tools" / exe,
              local / "Android" / "Sdk" / "platform-tools" / exe,
              home / "Android" / "Sdk" / "platform-tools" / exe):
        if c.exists():
            return [str(c)]
    return None


def available() -> bool:
    return adb_cmd() is not None


def _run(args: list[str], timeout=600, binary=False) -> subprocess.CompletedProcess:
    cmd = adb_cmd()
    if not cmd:
        raise AdbError("adb (Android platform-tools) isn't installed.")
    r = subprocess.run(cmd + args, capture_output=True, timeout=timeout, **NO_WINDOW)
    if not binary:
        r.stdout = r.stdout.decode("utf-8", "replace")
        r.stderr = r.stderr.decode("utf-8", "replace")
    return r


def shell(serial: str, command: str, timeout=900) -> str:
    """Run a command on the phone. -T: no terminal, so output isn't altered and errors come back."""
    r = _run(["-s", serial, "shell", "-T", command], timeout=timeout)
    if r.returncode != 0 and not r.stdout.strip():
        raise AdbError((r.stderr or r.stdout).strip() or f"adb shell failed ({r.returncode})")
    return r.stdout


def q(s: str) -> str:
    return shlex.quote(s)


# ---------------------------------------------------------------- devices
_info_cache: dict[str, tuple[float, dict]] = {}


def _info(serial: str) -> dict:
    hit = _info_cache.get(serial)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    out = shell(serial, "getprop ro.product.manufacturer; getprop ro.product.model; "
                        "getprop ro.build.version.release; getprop ro.serialno; "
                        "settings get global device_name 2>/dev/null", timeout=30)
    lines = (out.splitlines() + [""] * 5)[:5]
    maker, model, release, serialno, devname = (x.strip() for x in lines)
    if devname in ("null", ""):
        devname = ""
    info = {"maker": maker, "model": model, "release": release, "serialno": serialno,
            "name": devname or f"{maker.title()} {model}".strip() or serial}
    _info_cache[serial] = (time.time(), info)
    return info


def list_devices() -> list[dict]:
    """Android phones visible to adb. state: ready | unauthorized | no-permission | offline."""
    if not available():
        return []
    try:
        out = _run(["devices", "-l"], timeout=20).stdout
    except Exception:
        return []
    devs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        serial, _, rest = line.partition(" ")
        rest = rest.strip()
        props = dict(re.findall(r"(\w+):(\S+)", rest))
        if rest.startswith("no permissions"):
            state = "no-permission"
        else:
            state = {"device": "ready", "unauthorized": "unauthorized"}.get(rest.split()[0] if rest else "", "offline")
        d = {"platform": "android", "serial": serial, "state": state, "ios": None,
             "udid": serial.upper(), "name": props.get("model", "Android phone").replace("_", " "),
             "product": props.get("model"), "os_version": None, "os_label": "Android"}
        if state == "ready":
            try:
                inf = _info(serial)
                d.update(udid=(inf["serialno"] or serial).upper(), name=inf["name"],
                         product=f"{inf['maker']} {inf['model']}".strip(), os_version=inf["release"],
                         os_label=f"Android {inf['release']}" if inf["release"] else "Android")
            except Exception:
                d["state"] = "offline"
        devs.append(d)
    return devs


def serial_for(udid: str) -> str:
    for d in list_devices():
        if d["udid"] == udid.upper() and d["state"] == "ready":
            return d["serial"]
    raise AdbError("That Android phone isn't connected (or USB debugging isn't allowed yet).")


# ---------------------------------------------------------------- content provider queries
def parse_rows(text: str, cols: list[str]) -> list[dict]:
    """Parse `content query` output ("Row: N a=1, b=x, c=free text"), format per AOSP Content.java.
    Columns come back in projection order; only the LAST column may contain free text (put it last).
    Rows are split only at the next expected "Row: <n> <first column>=", so message text that
    contains "Row: " or ", name=" on a new line can't break it."""
    if not text.strip() or text.lstrip().startswith("No result found"):
        return []
    starts, n, pos = [], 0, 0
    while True:
        tag = f"Row: {n} {cols[0]}="
        i = text.find(tag, pos)
        while i > 0 and text[i - 1] != "\n":
            i = text.find(tag, i + 1)
        if i < 0:
            break
        starts.append((i, i + len(tag) - len(cols[0]) - 1))
        pos, n = i + len(tag), n + 1
    rows = []
    for k, (_, body_start) in enumerate(starts):
        end = starts[k + 1][0] - 1 if k + 1 < len(starts) else len(text)
        chunk = text[body_start:end]
        if k + 1 == len(starts) and chunk.endswith("\n"):
            chunk = chunk[:-1]
        vals, p, ok = {}, 0, True
        for i, c in enumerate(cols):
            pre = c + "="
            if not chunk.startswith(pre, p):
                ok = False
                break
            p += len(pre)
            if i == len(cols) - 1:
                v = chunk[p:]
            else:
                j = chunk.find(", " + cols[i + 1] + "=", p)
                if j < 0:
                    ok = False
                    break
                v, p = chunk[p:j], j + 2
            vals[c] = None if v == "NULL" else v
        if ok:
            rows.append(vals)
    return rows


def query(serial: str, uri: str, cols: list[str], where: str | None = None, timeout=900) -> list[dict]:
    cmd = f"content query --uri {q(uri)} --projection {q(':'.join(cols))}"
    if where:
        cmd += f" --where {q(where)}"
    out = shell(serial, cmd + " 2>&1", timeout=timeout)
    if "Exception" in out.split("\n", 1)[0] or out.startswith("Error"):
        raise AdbError(out.strip().splitlines()[0][:300])
    return parse_rows(out, cols)


def _int(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


# ---------------------------------------------------------------- identities (shared with XML import)
def handle_key(h):
    from .ingest import handle_key as hk
    return hk(h)


def sms_guid(date_ms: int, me: bool, address: str | None, body: str | None) -> str:
    raw = f"{date_ms}|{int(me)}|{handle_key(address) or ''}|{body or ''}"
    return "a-sms-" + hashlib.sha1(raw.encode()).hexdigest()[:24]


def mms_guid(m_id: str | None, date_s: int, me: bool, participants: list[str], text: str) -> str:
    if m_id and m_id.startswith("pv:"):  # exported by PhoneVault: keep the original ID (no duplicates)
        return m_id[3:]
    if m_id:
        raw = f"mid|{m_id}"
    else:
        raw = f"{date_s}|{int(me)}|{','.join(sorted(filter(None, map(handle_key, participants))))}|{text}"
    return "a-mms-" + hashlib.sha1(raw.encode()).hexdigest()[:24]


def chat_guid(participants: list[str]) -> str:
    keys = sorted({k for k in map(handle_key, participants) if k})
    if len(keys) <= 1:
        return "android:" + (keys[0] if keys else "unknown")
    return "android-group:" + hashlib.sha1(",".join(keys).encode()).hexdigest()[:16]


def att_guid(msg_guid: str, k: int, name: str) -> str:
    """k = position among the message's attachments (text and layout parts don't count), so a
    phone backup, an SMS Backup & Restore file and PhoneVault's own export all agree."""
    return f"{msg_guid}-a{k:02d}-{hashlib.sha1((name or '').encode()).hexdigest()[:6]}"


def call_uid(date_ms: int, number: str | None, ctype: int) -> str:
    return "a-call-" + hashlib.sha1(f"{date_ms}|{handle_key(number) or number}|{ctype}".encode()).hexdigest()[:20]


def _ext_for(ct: str | None) -> str:
    ct = (ct or "").lower()
    return {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/heic": ".heic", "image/webp": ".webp", "video/mp4": ".mp4", "video/3gpp": ".3gp",
            "audio/amr": ".amr", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/mpeg": ".mp3",
            "audio/ogg": ".ogg", "text/x-vcard": ".vcf", "text/vcard": ".vcf"}.get(ct, ".bin")


# ---------------------------------------------------------------- backup: phone -> snapshot
def snapshot(udid: str, archive: pathlib.Path, vault, log=print) -> pathlib.Path:
    """Read messages, calls and contacts from the phone into a snapshot folder.
    Attachments are only fetched for messages the vault doesn't have yet."""
    serial = serial_for(udid)
    inf = _info(serial)
    snap = archive / "android" / udid / time.strftime("snapshot-%Y%m%d-%H%M%S")
    (snap / "parts").mkdir(parents=True)
    data = {"format": 1, "taken": time.time(),
            "device": {"udid": udid, "name": inf["name"], "product": f"{inf['maker']} {inf['model']}".strip(),
                       "android": inf["release"]},
            "messages": [], "calls": [], "contacts": [], "notes": []}
    known = {r[0] for r in vault.execute("SELECT guid FROM message WHERE udid=?", (udid,))}
    have_att = {r[0] for r in vault.execute(
        "SELECT guid FROM attachment WHERE udid=? AND stored_path IS NOT NULL", (udid,))}

    # conversation participants: thread -> recipient ids -> addresses
    threads = {}
    try:
        canon = {r["_id"]: r["address"] for r in
                 query(serial, "content://mms-sms/canonical-addresses", ["_id", "address"])}
        for r in query(serial, "content://mms-sms/conversations?simple=true", ["_id", "recipient_ids"]):
            threads[r["_id"]] = [canon[i] for i in (r["recipient_ids"] or "").split() if i in canon]
    except AdbError as e:
        data["notes"].append(f"Conversation list unavailable: {e}")

    # SMS
    log("Reading text messages ...")
    for r in query(serial, "content://sms", ["_id", "thread_id", "date", "type", "address", "body"]):
        ms, typ = _int(r["date"]), _int(r["type"])
        if typ not in (1, 2):
            continue
        me = typ == 2
        parts_ = threads.get(r["thread_id"]) or ([r["address"]] if r["address"] else [])
        data["messages"].append({
            "guid": sms_guid(ms, me, r["address"], r["body"]), "kind": "sms", "date": ms / 1000,
            "me": me, "sender": None if me else r["address"], "participants": parts_,
            "text": r["body"], "parts": []})
    log(f"  text messages: {sum(1 for m in data['messages'] if m['kind'] == 'sms'):,}")

    # MMS (also Google Messages RCS)
    log("Reading picture/group messages (MMS and RCS) ...")
    try:
        pdus = query(serial, "content://mms", ["_id", "thread_id", "date", "msg_box", "m_id", "sub"])
    except AdbError as e:
        pdus = []
        data["notes"].append(f"MMS unavailable: {e}")
    parts_by_msg = {}
    if pdus:
        for p in query(serial, "content://mms/part", ["_id", "mid", "seq", "ct", "name", "cl", "text"]):
            parts_by_msg.setdefault(p["mid"], []).append(p)
    need_from, fetch_parts = {}, []
    for p in pdus:
        box = _int(p["msg_box"])
        if box not in (1, 2):
            continue
        me = box == 2
        ps = sorted(parts_by_msg.get(p["_id"], []), key=lambda x: _int(x["seq"]))
        text = "\n".join(x["text"] for x in ps if (x["ct"] or "").lower() == "text/plain" and x["text"]) \
            or (p["sub"] or "")
        participants = threads.get(p["thread_id"], [])
        guid = mms_guid(p["m_id"], _int(p["date"]), me, participants, text)
        msg = {"guid": guid, "kind": "mms", "date": float(_int(p["date"])), "me": me,
               "sender": None, "participants": participants, "text": text or None, "parts": [], "_id": p["_id"]}
        if not me:
            if len(participants) == 1:
                msg["sender"] = participants[0]
            elif guid not in known:
                need_from[p["_id"]] = msg
        k = 0
        for x in ps:
            ct = (x["ct"] or "").lower()
            if ct == "text/plain" or ct in PART_SKIP:
                continue
            name = x["cl"] or x["name"] or f"part{k}{_ext_for(ct)}"
            aguid = att_guid(guid, k, name)
            k += 1
            part = {"guid": aguid, "ct": ct, "name": name, "file": None}
            msg["parts"].append(part)
            if aguid not in have_att:
                fetch_parts.append((x["_id"], part))
        data["messages"].append(msg)
    log(f"  picture/group messages: {sum(1 for m in data['messages'] if m['kind'] == 'mms'):,}")

    if need_from:  # who sent each new incoming group message (one lookup per message)
        log(f"  finding senders of {len(need_from):,} new group messages ...")
        ids = list(need_from)
        for i in range(0, len(ids), 80):
            chunk = ids[i:i + 80]
            script = "; ".join(f"echo '@@@ {mid}'; content query --uri content://mms/{mid}/addr "
                               f"--projection type:address" for mid in chunk if mid.isdigit())
            out = shell(serial, script, timeout=1800)
            for sec in out.split("@@@ ")[1:]:
                mid, _, body = sec.partition("\n")
                frm = [r["address"] for r in parse_rows(body, ["type", "address"])
                       if r["type"] == "137" and r["address"] and r["address"] != "insert-address-token"]
                if frm and mid.strip() in need_from:
                    need_from[mid.strip()]["sender"] = frm[0]

    if fetch_parts:
        _fetch_parts(serial, fetch_parts, snap / "parts", log)

    _samsung_chat(serial, data, threads, log)

    log("Reading call history and contacts ...")
    try:
        for r in query(serial, "content://call_log/calls", ["_id", "date", "duration", "type", "features", "number", "name"]):
            data["calls"].append({"date_ms": _int(r["date"]), "number": r["number"], "name": r["name"],
                                  "duration": _int(r["duration"]), "type": _int(r["type"]),
                                  "video": bool(_int(r["features"]) & 1)})
    except AdbError as e:
        data["notes"].append(f"Call history unavailable: {e}")
    try:
        where = ("mimetype='vnd.android.cursor.item/phone_v2' OR mimetype='vnd.android.cursor.item/email_v2'")
        for r in query(serial, "content://com.android.contacts/data", ["contact_id", "mimetype", "data1", "display_name"], where):
            if r["data1"] and r["display_name"]:
                data["contacts"].append({"name": r["display_name"], "value": r["data1"]})
    except AdbError as e:
        data["notes"].append(f"Contacts unavailable: {e}")
    for m in data["messages"]:
        m.pop("_id", None)
    (snap / "snapshot.json").write_text(json.dumps(data))
    for n in data["notes"]:
        log("NOTE: " + n)
    return snap


def _fetch_parts(serial: str, items: list, dest: pathlib.Path, log):
    """Copy attachment files out of the message database: content read -> phone temp folder -> pull."""
    log(f"  copying {len(items):,} new message attachments ...")
    done = 0
    for i in range(0, len(items), 60):
        chunk = items[i:i + 60]
        script = f"rm -rf {DEVICE_TMP}; mkdir -p {DEVICE_TMP}; " + "; ".join(
            f"content read --uri content://mms/part/{pid} > {DEVICE_TMP}/{pid} 2>/dev/null || rm -f {DEVICE_TMP}/{pid}"
            for pid, _ in chunk if str(pid).isdigit())
        shell(serial, script, timeout=1800)
        with tempfile.TemporaryDirectory(dir=dest) as tmp:
            local = pathlib.Path(tmp) / "x"
            _run(["-s", serial, "pull", DEVICE_TMP, str(local)], timeout=3600)
            for pid, part in chunk:
                f = local / str(pid)
                if f.exists() and f.stat().st_size > 0:
                    target = dest / part["guid"]
                    shutil.move(str(f), target)
                    part["file"] = f"parts/{part['guid']}"
                    done += 1
        if (i // 60) % 5 == 4:
            log(f"    attachments so far: {done:,}")
    shell(serial, f"rm -rf {DEVICE_TMP}", timeout=120)
    log(f"  attachments copied: {done:,} of {len(items):,}")


def _samsung_chat(serial, data, threads, log):
    """Samsung Messages keeps chat/RCS messages in its own provider. Undocumented; best effort, text only."""
    for cols in (["_id", "thread_id", "date", "type", "address", "body"],):
        try:
            rows = query(serial, "content://im/chat", cols, timeout=300)
        except AdbError:
            return  # not a Samsung phone, or not readable
        n = 0
        for r in rows:
            typ = _int(r.get("type"))
            if typ not in (1, 2):
                continue
            me = typ == 2
            ms = _int(r["date"])
            parts_ = threads.get(r.get("thread_id")) or ([r["address"]] if r.get("address") else [])
            data["messages"].append({"guid": "a-chat-" + hashlib.sha1(f"{ms}|{int(me)}|{r.get('address')}|{r.get('body')}".encode()).hexdigest()[:24],
                                     "kind": "chat", "date": ms / 1000 if ms > 1e11 else float(ms), "me": me,
                                     "sender": None if me else r.get("address"), "participants": parts_,
                                     "text": r.get("body"), "parts": []})
            n += 1
        if n:
            log(f"  Samsung chat (RCS) messages: {n:,} (text only)")
        return


# ---------------------------------------------------------------- snapshot -> vault
def ingest_snapshot(vault, archive: pathlib.Path, snap: pathlib.Path, kind="backup", log=print, udid=None) -> dict:
    data = json.loads((snap / "snapshot.json").read_text())
    dev = data["device"]
    udid = (udid or dev["udid"]).upper()
    vault.execute("""INSERT INTO device (udid, name, product, ios, platform, last_seen) VALUES (?,?,?,?,?,?)
                     ON CONFLICT(udid) DO UPDATE SET name = coalesce(excluded.name, name),
                       product = coalesce(excluded.product, product), ios = coalesce(excluded.ios, ios),
                       platform = 'android'""",
                  (udid, dev.get("name"), dev.get("product"), dev.get("android"), "android", time.time()))
    source_id = vault.execute("INSERT INTO source (udid, path, kind, imported_at) VALUES (?,?,?,?)",
                              (udid, str(snap), kind, time.time())).lastrowid
    before = vault.execute("SELECT count(*) FROM message WHERE udid=?", (udid,)).fetchone()[0]
    att_root = archive / "library" / udid / "attachments"
    copied = missing = 0
    from .ingest import _safe_name
    for m in data["messages"]:
        cg = chat_guid(m["participants"] or ([m["sender"]] if m.get("sender") else []))
        members = m["participants"] or ([m["sender"]] if m.get("sender") else [])
        vault.execute("""INSERT INTO chat (udid, guid, identifier, display_name, service) VALUES (?,?,?,?,?)
                         ON CONFLICT(udid, guid) DO NOTHING""",
                      (udid, cg, "~".join(members) or None, None, "SMS"))
        for h in members:
            vault.execute("INSERT OR IGNORE INTO chat_member VALUES (?,?,?)", (udid, cg, h))
        vault.execute("""INSERT INTO message (udid, guid, chat_guid, date, is_from_me, sender, text,
                            assoc_type, assoc_guid, first_source) VALUES (?,?,?,?,?,?,?,0,NULL,?)
                         ON CONFLICT(udid, guid) DO UPDATE SET text = coalesce(text, excluded.text),
                           sender = coalesce(sender, excluded.sender)""",
                      (udid, m["guid"], cg, m["date"], int(m["me"]), m.get("sender"), m.get("text"), source_id))
        for p in m["parts"]:
            stored = None
            ex = vault.execute("SELECT stored_path FROM attachment WHERE udid=? AND guid=?", (udid, p["guid"])).fetchone()
            if ex and ex["stored_path"] and (archive / ex["stored_path"]).exists():
                stored = ex["stored_path"]
            elif p.get("file") and (snap / p["file"]).exists():
                dest = att_root / p["guid"][6:8] / p["guid"] / _safe_name(p["name"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(snap / p["file"]), dest)
                stored = str(dest.relative_to(archive))
                copied += 1
            else:
                missing += 1
            size = (archive / stored).stat().st_size if stored else None
            vault.execute("""INSERT INTO attachment (udid, guid, message_guid, mime, name, total_bytes, stored_path)
                             VALUES (?,?,?,?,?,?,?) ON CONFLICT(udid, guid) DO UPDATE SET
                               stored_path = coalesce(excluded.stored_path, stored_path)""",
                          (udid, p["guid"], m["guid"], p["ct"], p["name"], size, stored))
    after = vault.execute("SELECT count(*) FROM message WHERE udid=?", (udid,)).fetchone()[0]
    for c in data["calls"]:
        t = c["type"]
        vault.execute("""INSERT OR IGNORE INTO call (udid, uid, date, address, name, duration, outgoing, answered, kind)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (udid, call_uid(c["date_ms"], c["number"], t), c["date_ms"] / 1000, c["number"], c.get("name"),
                       c["duration"], int(t == 2), int(t in (1, 7) or (t == 2 and c["duration"] > 0)),
                       102 if c.get("video") else 101))
    for c in data["contacts"]:
        k = handle_key(c["value"])
        if k:
            vault.execute("INSERT OR REPLACE INTO contact (udid, handle_key, name, value) VALUES (?,?,?,?)",
                          (udid, k, c["name"], c["value"]))
    total = len(data["messages"])
    vault.execute("""UPDATE source SET messages_in_source=?, new_messages=?, attachments_copied=?,
                     attachments_missing=? WHERE id=?""", (total, after - before, copied, missing, source_id))
    vault.commit()
    shutil.rmtree(snap / "parts", ignore_errors=True)
    log(f"Messages in this backup: {total:,}  new to the vault: {after - before:,}")
    log(f"Attachments copied: {copied:,}  not readable: {missing:,}")
    log(f"Calls: {len(data['calls']):,}  Contact entries: {len(data['contacts']):,}")
    return {"udid": udid, "messages_in_source": total, "new_messages": after - before,
            "attachments_copied": copied, "attachments_missing": missing}


def prune_snapshots(archive: pathlib.Path, udid: str, keep=3):
    snaps = sorted((archive / "android" / udid).glob("snapshot-*"))
    for s in snaps[:-keep]:
        shutil.rmtree(s, ignore_errors=True)


# ---------------------------------------------------------------- photos, recordings
def _list_files(serial: str, dirs: list[str]) -> list[tuple[str, int, float]]:
    """[(path, size, mtime)] for regular, non-hidden files under dirs (skips .thumbnails, .trashed-*)."""
    targets = " ".join(q(d) for d in dirs)
    out = shell(serial, f"for d in {targets}; do [ -d \"$d\" ] && find \"$d\" -type f ! -path '*/.*' "
                        f"-exec stat -c '%s|%Y|%n' {{}} + ; done 2>/dev/null; true", timeout=1800)
    files = []
    for line in out.splitlines():
        size, _, rest = line.partition("|")
        mt, _, path = rest.partition("|")
        if path and size.isdigit():
            files.append((path, int(size), float(_int(mt))))
    return files


def _local_for(archive, udid, sub, remote):
    rel = remote.removeprefix("/sdcard/").removeprefix("/storage/emulated/0/")
    return archive / "library" / udid / sub / pathlib.PurePosixPath(rel)


def pending_photos(udid: str, vault) -> int:
    from .device import photo_group_key
    serial = serial_for(udid)
    known = {r["device_path"]: r["size"] for r in
             vault.execute("SELECT device_path, size FROM photo WHERE udid = ?", (udid,))}
    groups = set()
    for path, size, _ in _list_files(serial, MEDIA_DIRS):
        k = photo_group_key(path)
        if k and known.get(path) != size:
            groups.add(k)
    return len(groups)


def _pull_many(serial: str, files: list[tuple[str, int, float]], local_for, log, label) -> list:
    """adb pull new files, batched per folder. Returns [(remote, size, mtime, local)] copied."""
    by_dir = {}
    for f in files:
        by_dir.setdefault(posixpath.dirname(f[0]), []).append(f)
    copied = []
    for _d, items in by_dir.items():
        for i in range(0, len(items), 40):
            chunk = items[i:i + 40]
            target = local_for(chunk[0][0]).parent
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target, prefix=".pv-") as tmp:
                r = _run(["-s", serial, "pull", "-a", *[c[0] for c in chunk], tmp], timeout=7200)
                for remote, size, mt in chunk:
                    got = pathlib.Path(tmp) / posixpath.basename(remote)
                    if got.exists() and got.stat().st_size == size:
                        dest = local_for(remote)
                        got.replace(dest)
                        copied.append((remote, size, mt, dest))
                if r.returncode != 0:
                    log(f"  some {label} couldn't be copied: {(r.stderr or r.stdout or '').strip()[:200]}")
            log(f"  {label} copied so far: {len(copied):,} of {len(files):,}")
    return copied


def pull_photos(udid: str, archive: pathlib.Path, vault, log=print) -> int:
    serial = serial_for(udid)
    known = {r["device_path"]: r["size"] for r in
             vault.execute("SELECT device_path, size FROM photo WHERE udid = ?", (udid,))}
    new = [f for f in _list_files(serial, MEDIA_DIRS) if known.get(f[0]) != f[1]]
    if not new:
        return 0
    log(f"  {len(new):,} new photos/videos to copy")
    got = _pull_many(serial, new, lambda r: _local_for(archive, udid, "photos", r), log, "photos")
    for remote, size, mt, dest in got:
        vault.execute("""INSERT INTO photo (udid, device_path, size, mtime, stored_path) VALUES (?,?,?,?,?)
                         ON CONFLICT(udid, device_path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                           stored_path=excluded.stored_path""",
                      (udid, remote, size, mt, str(dest.relative_to(archive))))
    vault.commit()
    return len(got)


def pull_recordings(udid: str, archive: pathlib.Path, vault, log=print) -> int:
    serial = serial_for(udid)
    known = {r["name"]: r["size"] for r in vault.execute("SELECT name, size FROM memo WHERE udid=?", (udid,))}
    files = [f for f in _list_files(serial, RECORDING_DIRS)
             if pathlib.PurePosixPath(f[0]).suffix.lower() in AUDIO_EXT and known.get(f[0]) != f[1]]
    if not files:
        return 0
    got = _pull_many(serial, files, lambda r: _local_for(archive, udid, "recordings", r), log, "recordings")
    for remote, size, mt, dest in got:
        vault.execute("INSERT OR REPLACE INTO memo (udid, name, date, size, stored_path) VALUES (?,?,?,?,?)",
                      (udid, remote, mt, size, str(dest.relative_to(archive))))
    vault.commit()
    return len(got)


# ---------------------------------------------------------------- SMS Backup & Restore XML
def xml_to_snapshot(paths: list[str], archive: pathlib.Path, udid: str, name: str | None = None) -> pathlib.Path:
    """Turn SMS Backup & Restore files (sms-*.xml and/or calls-*.xml) into a snapshot folder.
    Read-only: the XML files aren't changed."""
    snap = archive / "android" / udid / time.strftime("import-%Y%m%d-%H%M%S")
    (snap / "parts").mkdir(parents=True)
    data = {"format": 1, "taken": time.time(), "device": {"udid": udid, "name": name, "product": None, "android": None},
            "messages": [], "calls": [], "contacts": [], "notes": []}
    for path in paths:
        for _ev, el in ET.iterparse(path, events=("end",)):
            tag = el.tag
            if tag == "sms":
                typ = _int(el.get("type"))
                if typ in (1, 2, 4, 5, 6):
                    ms, me = _int(el.get("date")), typ != 1
                    addrs = [a for a in (el.get("address") or "").split("~") if a]
                    data["messages"].append({
                        "guid": sms_guid(ms, me, el.get("address"), el.get("body")),
                        "kind": "chat" if el.get("s_chat") == "1" else "sms", "date": ms / 1000, "me": me,
                        "sender": None if me else el.get("address"), "participants": addrs,
                        "text": el.get("body"), "parts": []})
                el.clear()
            elif tag == "mms":
                box = _int(el.get("msg_box"))
                if box in (1, 2):
                    me = box == 2
                    date_s = _int(el.get("date")) // 1000
                    parts_el = el.findall("./parts/part")
                    addrs_el = el.findall("./addrs/addr")
                    text = "\n".join(p.get("text") for p in parts_el
                                     if (p.get("ct") or "").lower() == "text/plain" and p.get("text") not in (None, "null"))
                    participants = [a for a in (el.get("address") or "").split("~") if a]
                    if not participants:
                        participants = sorted({a.get("address") for a in addrs_el
                                               if a.get("address") and a.get("address") != "insert-address-token"})
                    sender = None
                    if not me:
                        frm = [a.get("address") for a in addrs_el if a.get("type") == "137"]
                        sender = frm[0] if frm else (participants[0] if len(participants) == 1 else None)
                    m_id = el.get("m_id") if el.get("m_id") not in (None, "null", "") else None
                    guid = mms_guid(m_id, date_s, me, participants, text)
                    msg = {"guid": guid, "kind": "mms", "date": float(date_s), "me": me, "sender": sender,
                           "participants": participants, "text": text or None, "parts": []}
                    k = 0
                    for p in sorted(parts_el, key=lambda e: _int(e.get("seq"))):
                        ct = (p.get("ct") or "").lower()
                        if ct == "text/plain" or ct in PART_SKIP:
                            continue
                        pname = p.get("cl") if p.get("cl") not in (None, "null") else (
                            p.get("name") if p.get("name") not in (None, "null") else f"part{k}{_ext_for(ct)}")
                        aguid = att_guid(guid, k, pname)
                        k += 1
                        part = {"guid": aguid, "ct": ct, "name": pname, "file": None}
                        if p.get("data"):
                            (snap / "parts" / aguid).write_bytes(base64.b64decode(p.get("data")))
                            part["file"] = f"parts/{aguid}"
                        msg["parts"].append(part)
                    data["messages"].append(msg)
                el.clear()
            elif tag == "call":
                data["calls"].append({"date_ms": _int(el.get("date")), "number": el.get("number"),
                                      "name": el.get("contact_name") if el.get("contact_name") not in (None, "(Unknown)") else None,
                                      "duration": _int(el.get("duration")), "type": _int(el.get("type")), "video": False})
                el.clear()
    (snap / "snapshot.json").write_text(json.dumps(data))
    return snap


def export_xml(vault, archive: pathlib.Path, udid: str, out_dir: pathlib.Path, media=True, log=print) -> list[pathlib.Path]:
    """Write the archive's messages and calls for one phone (iPhone or Android) as SMS Backup &
    Restore files, which that app restores onto any Android phone."""
    from xml.sax.saxutils import quoteattr
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d%H%M%S")
    members = {}
    for r in vault.execute("SELECT chat_guid, handle FROM chat_member WHERE udid=?", (udid,)):
        members.setdefault(r["chat_guid"], []).append(r["handle"])
    idents = {r["guid"]: r["identifier"] for r in vault.execute("SELECT guid, identifier FROM chat WHERE udid=?", (udid,))}
    atts = {}
    for r in vault.execute("""SELECT message_guid, mime, name, stored_path FROM attachment
                              WHERE udid=? AND stored_path IS NOT NULL ORDER BY message_guid, guid""", (udid,)):
        atts.setdefault(r["message_guid"], []).append(r)
    sms_path = out_dir / f"sms-{stamp}.xml"
    n = 0
    rows = vault.execute("""SELECT guid, chat_guid, date, is_from_me, sender, text FROM message
                            WHERE udid=? AND assoc_type=0 AND date IS NOT NULL ORDER BY date""", (udid,)).fetchall()
    with open(sms_path, "w", encoding="utf-8") as f:
        f.write("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n")
        f.write(f'<smses count="{len(rows)}" backup_set="phonevault-{stamp}" backup_date="{int(time.time() * 1000)}">\n')
        for m in rows:
            people = members.get(m["chat_guid"]) or [x for x in [idents.get(m["chat_guid"])] if x]
            people = [p for p in people if p] or ([m["sender"]] if m["sender"] else [])
            ms = int(m["date"] * 1000)
            files = atts.get(m["guid"], []) if media else []
            me = bool(m["is_from_me"])
            if len(people) <= 1 and not files:
                addr = people[0] if people else (m["sender"] or "")
                f.write(f'  <sms protocol="0" address={quoteattr(addr)} date="{ms}" type="{2 if me else 1}" '
                        f'subject="null" body={quoteattr(m["text"] or "")} toa="null" sc_toa="null" '
                        f'service_center="null" read="1" status="-1" locked="0" />\n')
            else:
                f.write(f'  <mms date="{ms}" ct_t="application/vnd.wap.multipart.related" msg_box="{2 if me else 1}" '
                        f'address={quoteattr("~".join(people))} read="1" m_type="{128 if me else 132}" '
                        f'text_only="{0 if files else 1}" m_id={quoteattr("pv:" + m["guid"])} tr_id={quoteattr("pv-" + m["guid"][-20:])}>\n')
                f.write("    <parts>\n")
                seq = 0
                if m["text"]:
                    f.write(f'      <part seq="{seq}" ct="text/plain" name="null" chset="106" cl="text_0.txt" text={quoteattr(m["text"])} />\n')
                    seq += 1
                for a in files:
                    p = archive / a["stored_path"]
                    if not p.exists():
                        continue
                    b64 = base64.b64encode(p.read_bytes()).decode()
                    f.write(f'      <part seq="{seq}" ct={quoteattr(a["mime"] or "application/octet-stream")} '
                            f'name={quoteattr(a["name"] or p.name)} cl={quoteattr(a["name"] or p.name)} data="{b64}" />\n')
                    seq += 1
                f.write("    </parts>\n    <addrs>\n")
                if me:
                    f.write('      <addr address="insert-address-token" type="137" charset="106" />\n')
                    for p in people:
                        f.write(f'      <addr address={quoteattr(p)} type="151" charset="106" />\n')
                else:
                    f.write(f'      <addr address={quoteattr(m["sender"] or (people[0] if people else ""))} type="137" charset="106" />\n')
                    for p in people:
                        if p != m["sender"]:
                            f.write(f'      <addr address={quoteattr(p)} type="151" charset="106" />\n')
                f.write("    </addrs>\n  </mms>\n")
            n += 1
        f.write("</smses>\n")
    calls = vault.execute("SELECT * FROM call WHERE udid=? ORDER BY date", (udid,)).fetchall()
    out = [sms_path]
    if calls:
        calls_path = out_dir / f"calls-{stamp}.xml"
        with open(calls_path, "w", encoding="utf-8") as f:
            f.write("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n")
            f.write(f'<calls count="{len(calls)}" backup_set="phonevault-{stamp}" backup_date="{int(time.time() * 1000)}">\n')
            for c in calls:
                t = 2 if c["outgoing"] else (1 if c["answered"] else 3)
                f.write(f'  <call number={quoteattr(c["address"] or "")} duration="{int(c["duration"] or 0)}" '
                        f'date="{int((c["date"] or 0) * 1000)}" type="{t}" presentation="1" />\n')
            f.write("</calls>\n")
        out.append(calls_path)
    log(f"Exported {n:,} messages and {len(calls):,} calls to {out_dir}")
    return out


def push(udid: str, local: pathlib.Path, remote_dir: str) -> str:
    serial = serial_for(udid)
    shell(serial, f"mkdir -p {q(remote_dir)}", timeout=60)
    r = _run(["-s", serial, "push", str(local), remote_dir + "/"], timeout=7200)
    if r.returncode != 0:
        raise AdbError((r.stderr or r.stdout).strip()[:300])
    return f"{remote_dir}/{local.name}"


def push_photos(udid: str, archive: pathlib.Path, vault, source_udid: str, log=print) -> int:
    """Copy one archived phone's photos onto this Android phone (DCIM/PhoneVault), skipping ones already there."""
    serial = serial_for(udid)
    remote_root = "/sdcard/DCIM/PhoneVault"
    on_phone = {(posixpath.basename(p), s) for p, s, _ in _list_files(serial, [remote_root])}
    rows = vault.execute("SELECT stored_path FROM photo WHERE udid=? AND stored_path IS NOT NULL", (source_udid,)).fetchall()
    todo = [archive / r["stored_path"] for r in rows]
    todo = [p for p in todo if p.exists() and (p.name, p.stat().st_size) not in on_phone
            and p.suffix.lower() != ".aae"]
    shell(serial, f"mkdir -p {remote_root}", timeout=60)
    n = 0
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        r = _run(["-s", serial, "push", *map(str, chunk), remote_root + "/"], timeout=7200)
        if r.returncode == 0:
            n += len(chunk)
        log(f"  photos copied to the phone: {n:,} of {len(todo):,}")
    # ask Android to index the new files so they show in the Gallery/Photos apps
    shell(serial, f"content call --uri content://media --method scan_volume --arg external_primary >/dev/null 2>&1; "
                  f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{remote_root} >/dev/null 2>&1; true",
          timeout=300)
    return n
