"""Read an iPhone backup folder (read-only) and merge it into the vault.

The backup folder is the one named after the phone's UDID, holding Manifest.db.
Backup files are opened read-only; nothing in the backup folder is ever written.
"""
import pathlib
import plistlib
import re
import shutil
import sqlite3
import time

APPLE_EPOCH = 978307200  # 2001-01-01 in Unix time


class BackupError(Exception):
    pass


def _ro(path: pathlib.Path) -> sqlite3.Connection:
    # immutable=1: SQLite won't create -wal/-shm files next to the backup's databases
    uri = "file:" + str(path).replace("?", "%3F").replace("#", "%23") + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def backup_info(folder) -> dict:
    """Device name, iOS version, date, UDID and encryption flag of a backup folder (no password needed)."""
    folder = pathlib.Path(folder)
    info, enc = {}, False
    try:
        with open(folder / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except Exception:
        pass
    try:
        with open(folder / "Manifest.plist", "rb") as f:
            enc = bool(plistlib.load(f).get("IsEncrypted"))
    except Exception:
        pass
    lbd = info.get("Last Backup Date")
    return {"path": str(folder), "name": info.get("Device Name"), "ios": info.get("Product Version"),
            "udid": (info.get("Unique Identifier") or info.get("Target Identifier") or folder.name).upper(),
            "date": lbd.timestamp() if hasattr(lbd, "timestamp") else None, "encrypted": enc}


class Backup:
    """Read-only view of a backup folder. Encrypted backups are decrypted with the password
    into a private temp folder inside the archive, which is deleted by close()."""

    def __init__(self, folder, password: str | None = None, tmp_root: pathlib.Path | None = None):
        self.folder = pathlib.Path(folder)
        if not (self.folder / "Manifest.db").exists():
            raise BackupError(f"No Manifest.db in {self.folder} - point at the folder named after the phone's UDID")
        bi = backup_info(self.folder)
        self.encrypted = bi["encrypted"]
        self.udid = bi["udid"]
        self.info = {}
        iplist = self.folder / "Info.plist"
        if iplist.exists():
            with open(iplist, "rb") as f:
                self.info = plistlib.load(f)
        self._tmp = None
        self._pyb = None
        if self.encrypted:
            if password is None:
                from . import passwords
                password = passwords.get(self.udid)
            if not password:
                raise BackupError("This backup is encrypted and no password is saved for this phone. "
                                  "Add it on the Call history page.")
            try:
                from pyiosbackup import Backup as PyBackup
                self._pyb = PyBackup.from_path(self.folder, password)
            except Exception as e:
                raise BackupError(f"Couldn't unlock the encrypted backup - wrong password? ({e})")
            self.manifest = sqlite3.connect(str(self._pyb._manifest_db.path))
            self.manifest.row_factory = sqlite3.Row
            import tempfile
            (tmp_root or self.folder.parent).mkdir(parents=True, exist_ok=True)
            self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="decrypt-", dir=tmp_root))
        else:
            self.manifest = _ro(self.folder / "Manifest.db")

    def _local(self, row):
        """Path to the file's readable contents (decrypting into the temp folder if needed)."""
        fid = row["fileID"]
        if not self._pyb:
            p = self.folder / fid[:2] / fid
            return p if p.exists() else None
        out = self._tmp / fid
        if out.exists():
            return out
        from pyiosbackup.entry import Entry
        from pyiosbackup.manifest_dbs.sqlite3 import ManifestDbSqlite3
        try:
            data = Entry(self._pyb, **ManifestDbSqlite3._load_entry(row)).read_bytes()
        except FileNotFoundError:
            return None
        out.write_bytes(data)
        return out

    def resolve(self, domain: str, relative_path: str):
        row = self.manifest.execute(
            "SELECT fileID, file FROM Files WHERE domain = ? AND relativePath = ?",
            (domain, relative_path)).fetchone()
        return self._local(row) if row else None

    def find(self, domain_like: str, path_like: str):
        """[(relativePath, local file, mtime or None)] for files matching LIKE patterns."""
        out = []
        for r in self.manifest.execute(
                "SELECT fileID, relativePath, file FROM Files WHERE domain LIKE ? AND relativePath LIKE ? AND flags = 1",
                (domain_like, path_like)).fetchall():
            p = self._local(r)
            if p:
                out.append((r["relativePath"], p, _manifest_mtime(r["file"])))
        return out

    def close(self):
        try:
            self.manifest.close()
        except Exception:
            pass
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
        if self._pyb:
            try:
                pathlib.Path(self._pyb._manifest_db.path).unlink(missing_ok=True)  # decrypted Manifest copy
            except Exception:
                pass


def _manifest_mtime(blob):
    """Original modification time stored in Manifest.db's per-file record, if readable."""
    if not blob:
        return None
    try:
        objs = plistlib.loads(blob).get("$objects", [])
        for o in objs:
            if isinstance(o, dict) and "LastModified" in o:
                return float(o["LastModified"])
    except Exception:
        return None
    return None


def decode_attributed_body(blob) -> str | None:
    """Pull the plain text out of an attributedBody blob (NSAttributedString in
    Apple's 'typedstream' format). Newer iOS often leaves message.text empty and
    keeps the words only here."""
    if not blob:
        return None
    try:
        data = bytes(blob)
        i = data.find(b"NSString")
        if i < 0:
            return None
        rest = data[i + len(b"NSString"):]
        plus = rest.find(b"+")  # the string's type marker
        if plus < 0 or plus > 16:
            return None
        rest = rest[plus + 1:]
        n = rest[0]
        if n == 0x81:
            length = int.from_bytes(rest[1:3], "little")
            body = rest[3:3 + length]
        elif n == 0x82:
            length = int.from_bytes(rest[1:5], "little")
            body = rest[5:5 + length]
        else:
            body = rest[1:1 + n]
        return body.decode("utf-8", errors="replace")
    except Exception:
        return None


def apple_time(v) -> float | None:
    if v is None:
        return None
    v = float(v)
    if v > 1e11:  # iOS 11+ stores nanoseconds
        v = v / 1e9
    return v + APPLE_EPOCH


def handle_key(h: str | None) -> str | None:
    if not h:
        return None
    h = h.strip()
    if "@" in h:
        return h.lower()
    digits = re.sub(r"\D", "", h)
    return digits[-10:] if len(digits) >= 7 else (digits or h.lower())


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", name or "file").strip(" .") or "file"
    return name[:120]


def _media_relpath(filename: str | None):
    if not filename:
        return None
    for prefix in ("~/", "/var/mobile/", "/private/var/mobile/"):
        if filename.startswith(prefix):
            return filename[len(prefix):]
    return None


def import_contacts(vault, b: Backup) -> int:
    ab = b.resolve("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")
    if not ab:
        return 0
    con = _ro(ab)
    try:
        rows = con.execute("""
            SELECT p.First AS first, p.Last AS last, p.Organization AS org, v.value AS value
            FROM ABMultiValue v JOIN ABPerson p ON p.ROWID = v.record_id
            WHERE v.value IS NOT NULL""").fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for r in rows:
        name = " ".join(x for x in (r["first"], r["last"]) if x) or r["org"]
        key = handle_key(str(r["value"]))
        if name and key:
            vault.execute("INSERT OR REPLACE INTO contact (udid, handle_key, name, value) VALUES (?,?,?,?)",
                          (b.udid, key, name, str(r["value"])))
            n += 1
    return n


def _copy(src: pathlib.Path, dest: pathlib.Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)


def _unix_or_apple(v):
    if v is None:
        return None
    v = float(v)
    if v > 1e11:
        v /= 1e9
    return v + APPLE_EPOCH if v < 1e9 else v  # recent Apple-epoch seconds are < 1e9


def import_voicemail(vault, archive: pathlib.Path, b: Backup) -> int:
    files = {pathlib.PurePosixPath(rel).stem: (p, mt) for rel, p, mt in
             b.find("HomeDomain", "Library/Voicemail/%.amr")}
    if not files:
        return 0
    meta = {}
    dbp = b.resolve("HomeDomain", "Library/Voicemail/voicemail.db")
    if dbp:
        try:
            con = _ro(dbp)
            cols = _columns(con, "voicemail")
            sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in
                            ["ROWID", "date", "sender", "callback_num", "duration", "trashed_date"])
            for r in con.execute(f"SELECT {sel} FROM voicemail"):
                meta[str(r["ROWID"])] = r
        except sqlite3.Error:
            pass
    n = 0
    for vm_id, (src, mt) in files.items():
        m = meta.get(vm_id)
        dest = archive / "library" / b.udid / "voicemail" / f"{vm_id}.amr"
        _copy(src, dest)
        date = _unix_or_apple(m["date"]) if m and m["date"] else mt
        sender = (m["sender"] or m["callback_num"]) if m else None
        vault.execute("""INSERT INTO voicemail (udid, vm_id, date, sender, duration, stored_path)
                         VALUES (?,?,?,?,?,?) ON CONFLICT(udid, vm_id) DO UPDATE SET
                           stored_path = excluded.stored_path,
                           sender = coalesce(excluded.sender, sender), date = coalesce(excluded.date, date)""",
                      (b.udid, vm_id, date, sender, m["duration"] if m else None, str(dest.relative_to(archive))))
        n += 1
    return n


def import_calls(vault, b: Backup) -> int:
    """Call history (CallHistory.storedata). Apple only includes it in encrypted backups."""
    p = b.resolve("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata")
    if not p:
        return 0
    try:
        con = _ro(p)
        cols = _columns(con, "ZCALLRECORD")
        want = ["ZUNIQUE_ID", "ZADDRESS", "ZDATE", "ZDURATION", "ZORIGINATED", "ZANSWERED", "ZCALLTYPE", "ZNAME"]
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)
        rows = con.execute(f"SELECT {sel} FROM ZCALLRECORD").fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for r in rows:
        addr = r["ZADDRESS"]
        if isinstance(addr, bytes):
            addr = addr.decode("utf-8", errors="replace")
        date = apple_time(r["ZDATE"])
        uid = r["ZUNIQUE_ID"] or f"{date}-{addr}"
        vault.execute("""INSERT OR IGNORE INTO call (udid, uid, date, address, name, duration, outgoing, answered, kind)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (b.udid, str(uid), date, addr, r["ZNAME"], r["ZDURATION"], int(r["ZORIGINATED"] or 0),
                       int(r["ZANSWERED"] or 0), r["ZCALLTYPE"]))
        n += 1
    return n


def import_memos(vault, archive: pathlib.Path, b: Backup) -> int:
    n = 0
    for pat in ("%.m4a", "%.qta", "%.caf"):
        for rel, src, mt in b.find("%VoiceMemos%", pat):
            name = pathlib.PurePosixPath(rel).name
            m = re.match(r"(\d{8}) (\d{6})", name)
            date = time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")) if m else mt
            dest = archive / "library" / b.udid / "memos" / _safe_name(name)
            _copy(src, dest)
            vault.execute("""INSERT INTO memo (udid, name, date, size, stored_path) VALUES (?,?,?,?,?)
                             ON CONFLICT(udid, name) DO UPDATE SET stored_path = excluded.stored_path""",
                          (b.udid, name, date, src.stat().st_size, str(dest.relative_to(archive))))
            n += 1
    return n


def ingest(vault, archive: pathlib.Path, folder, kind="import", log=print, password=None) -> dict:
    b = Backup(folder, password, tmp_root=archive / "tmp")
    try:
        if b.encrypted:
            log("Encrypted backup - unlocking with the saved password")
        return _ingest(vault, archive, b, kind, log)
    finally:
        b.close()


def _ingest(vault, archive: pathlib.Path, b: "Backup", kind, log) -> dict:
    sms_path = b.resolve("HomeDomain", "Library/SMS/sms.db")
    if not sms_path:
        raise BackupError("No Messages database (sms.db) in this backup")
    sms = _ro(sms_path)

    dev_name = b.info.get("Device Name") or b.info.get("Display Name")
    vault.execute("""INSERT INTO device (udid, name, product, ios, last_seen) VALUES (?,?,?,?,?)
                     ON CONFLICT(udid) DO UPDATE SET
                       name = coalesce(excluded.name, name), product = coalesce(excluded.product, product),
                       ios = coalesce(excluded.ios, ios)""",
                  (b.udid, dev_name, b.info.get("Product Type"), b.info.get("Product Version"), None))
    cur = vault.execute("INSERT INTO source (udid, path, kind, imported_at) VALUES (?,?,?,?)",
                        (b.udid, str(b.folder), kind, time.time()))
    source_id = cur.lastrowid

    handles = {r["ROWID"]: r["id"] for r in sms.execute("SELECT ROWID, id FROM handle")}
    chats = {}
    for r in sms.execute("SELECT * FROM chat"):
        r = dict(r)
        chats[r["ROWID"]] = r["guid"]
        vault.execute("""INSERT INTO chat (udid, guid, identifier, display_name, service) VALUES (?,?,?,?,?)
                         ON CONFLICT(udid, guid) DO UPDATE SET
                           display_name = coalesce(excluded.display_name, display_name)""",
                      (b.udid, r["guid"], r.get("chat_identifier"),
                       r.get("display_name") or None, r.get("service_name")))
    for r in sms.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
        if r["chat_id"] in chats and r["handle_id"] in handles:
            vault.execute("INSERT OR IGNORE INTO chat_member VALUES (?,?,?)",
                          (b.udid, chats[r["chat_id"]], handles[r["handle_id"]]))
    msg_chat = {}
    for r in sms.execute("SELECT chat_id, message_id FROM chat_message_join"):
        msg_chat.setdefault(r["message_id"], r["chat_id"])

    cols = _columns(sms, "message")
    want = ["ROWID", "guid", "text", "attributedBody", "handle_id", "date", "is_from_me",
            "associated_message_type", "associated_message_guid", "item_type"]
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)

    before = vault.execute("SELECT count(*) FROM message WHERE udid = ?", (b.udid,)).fetchone()[0]
    total = 0
    rowid_to_guid = {}
    batch = []
    for r in sms.execute(f"SELECT {sel} FROM message"):
        total += 1
        rowid_to_guid[r["ROWID"]] = r["guid"]
        if (r["item_type"] or 0) != 0:
            continue  # group renames, member changes etc.
        text = r["text"] or decode_attributed_body(r["attributedBody"])
        chat_guid = chats.get(msg_chat.get(r["ROWID"]))
        sender = None if r["is_from_me"] else handles.get(r["handle_id"])
        batch.append((b.udid, r["guid"], chat_guid, apple_time(r["date"]), int(r["is_from_me"] or 0),
                      sender, text, int(r["associated_message_type"] or 0),
                      r["associated_message_guid"], source_id))
    vault.executemany("""INSERT INTO message (udid, guid, chat_guid, date, is_from_me, sender, text,
                            assoc_type, assoc_guid, first_source) VALUES (?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(udid, guid) DO UPDATE SET
                           text = coalesce(text, excluded.text),
                           chat_guid = coalesce(chat_guid, excluded.chat_guid)""", batch)
    after = vault.execute("SELECT count(*) FROM message WHERE udid = ?", (b.udid,)).fetchone()[0]
    new_messages = after - before
    log(f"Messages in this backup: {total:,}  new to the vault: {new_messages:,}")

    # attachments: copy each file into the archive once, keep it forever
    acols = _columns(sms, "attachment")
    asel = ", ".join(c if c in acols else f"NULL AS {c}" for c in
                     ["ROWID", "guid", "filename", "mime_type", "transfer_name", "total_bytes"])
    links = {}
    for r in sms.execute("SELECT message_id, attachment_id FROM message_attachment_join"):
        links.setdefault(r["attachment_id"], r["message_id"])
    copied = missing = orphans = 0
    att_root = archive / "library" / b.udid / "attachments"
    for r in sms.execute(f"SELECT {asel} FROM attachment"):
        msg_guid = rowid_to_guid.get(links.get(r["ROWID"]))
        name = r["transfer_name"] or pathlib.PurePosixPath(r["filename"] or "file").name
        existing = vault.execute("SELECT stored_path FROM attachment WHERE udid=? AND guid=?",
                                 (b.udid, r["guid"])).fetchone()
        stored = existing["stored_path"] if existing else None
        if not stored or not (archive / stored).exists():
            stored = None
            rel = _media_relpath(r["filename"])
            src = b.resolve("MediaDomain", rel) if rel else None
            if src:
                dest = att_root / r["guid"][:2] / r["guid"] / _safe_name(name)
                _copy(src, dest)
                if b._tmp and src.parent == b._tmp:
                    src.unlink(missing_ok=True)  # decrypted temp copy no longer needed
                stored = str(dest.relative_to(archive))
                copied += 1
            elif msg_guid:
                missing += 1   # a message shows it, but the file isn't in this backup
            else:
                orphans += 1   # left over from a deleted message; nothing shows it
        vault.execute("""INSERT INTO attachment (udid, guid, message_guid, mime, name, total_bytes, stored_path)
                         VALUES (?,?,?,?,?,?,?)
                         ON CONFLICT(udid, guid) DO UPDATE SET
                           stored_path = coalesce(excluded.stored_path, stored_path),
                           message_guid = coalesce(message_guid, excluded.message_guid)""",
                      (b.udid, r["guid"], msg_guid, r["mime_type"], name, r["total_bytes"], stored))
    log(f"Attachments copied: {copied:,}  missing from backup: {missing:,}  "
        f"leftovers from deleted messages (skipped): {orphans:,}")

    contacts = import_contacts(vault, b)
    log(f"Contact entries: {contacts:,}")
    vms = import_voicemail(vault, archive, b)
    memos = import_memos(vault, archive, b)
    calls = import_calls(vault, b)
    log(f"Voicemails: {vms:,}  Voice memos: {memos:,}  Calls: {calls:,}"
        + ("" if b.encrypted or calls else " (call history needs an encrypted backup)"))
    vault.execute("""UPDATE source SET messages_in_source=?, new_messages=?, attachments_copied=?,
                     attachments_missing=? WHERE id=?""",
                  (total, new_messages, copied, missing, source_id))
    vault.commit()
    return {"udid": b.udid, "messages_in_source": total, "new_messages": new_messages,
            "attachments_copied": copied, "attachments_missing": missing}
