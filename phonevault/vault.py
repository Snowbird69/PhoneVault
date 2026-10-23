"""The archive's own index (vault.sqlite). Append-only: rows are never deleted,
so messages stay in the archive even after the phone drops them."""
import pathlib
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
    udid TEXT PRIMARY KEY, name TEXT, product TEXT, ios TEXT, last_seen REAL
);
CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY, udid TEXT, path TEXT, kind TEXT, imported_at REAL,
    messages_in_source INTEGER, new_messages INTEGER,
    attachments_copied INTEGER, attachments_missing INTEGER
);
CREATE TABLE IF NOT EXISTS chat (
    udid TEXT, guid TEXT, identifier TEXT, display_name TEXT, service TEXT,
    PRIMARY KEY (udid, guid)
);
CREATE TABLE IF NOT EXISTS chat_member (
    udid TEXT, chat_guid TEXT, handle TEXT, PRIMARY KEY (udid, chat_guid, handle)
);
CREATE TABLE IF NOT EXISTS message (
    udid TEXT, guid TEXT, chat_guid TEXT, date REAL, is_from_me INTEGER,
    sender TEXT, text TEXT, assoc_type INTEGER, assoc_guid TEXT,
    first_source INTEGER,
    PRIMARY KEY (udid, guid)
);
CREATE INDEX IF NOT EXISTS message_chat ON message (udid, chat_guid, date);
CREATE TABLE IF NOT EXISTS attachment (
    udid TEXT, guid TEXT, message_guid TEXT, mime TEXT, name TEXT,
    total_bytes INTEGER, stored_path TEXT,
    PRIMARY KEY (udid, guid)
);
CREATE INDEX IF NOT EXISTS attachment_msg ON attachment (udid, message_guid);
CREATE TABLE IF NOT EXISTS contact (
    udid TEXT, handle_key TEXT, name TEXT, PRIMARY KEY (udid, handle_key)
);
CREATE TABLE IF NOT EXISTS photo (
    id INTEGER PRIMARY KEY, udid TEXT, device_path TEXT, size INTEGER,
    mtime REAL, stored_path TEXT, UNIQUE (udid, device_path)
);
CREATE INDEX IF NOT EXISTS photo_date ON photo (udid, mtime);
CREATE TABLE IF NOT EXISTS voicemail (
    udid TEXT, vm_id TEXT, date REAL, sender TEXT, duration REAL, stored_path TEXT,
    PRIMARY KEY (udid, vm_id)
);
CREATE TABLE IF NOT EXISTS memo (
    udid TEXT, name TEXT, date REAL, size INTEGER, stored_path TEXT,
    PRIMARY KEY (udid, name)
);
CREATE TABLE IF NOT EXISTS call (
    udid TEXT, uid TEXT, date REAL, address TEXT, name TEXT, duration REAL,
    outgoing INTEGER, answered INTEGER, kind INTEGER, PRIMARY KEY (udid, uid)
);
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY, udid TEXT, started REAL, finished REAL, status TEXT,
    engine TEXT, messages_on_phone INTEGER, new_messages INTEGER,
    new_photos INTEGER, note TEXT
);
"""

_lock = threading.Lock()


def memory() -> sqlite3.Connection:
    """Empty stand-in used before first-run setup, so nothing is written anywhere."""
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("ALTER TABLE contact ADD COLUMN value TEXT")
    con.execute("ALTER TABLE device ADD COLUMN platform TEXT DEFAULT 'ios'")
    return con


def connect(archive: pathlib.Path) -> sqlite3.Connection:
    archive.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(archive / "vault.sqlite", timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    with _lock:
        con.executescript(SCHEMA)
        # upgrades for vaults made by 0.1.0
        cols = {r[1] for r in con.execute("PRAGMA table_info(contact)")}
        if "value" not in cols:
            con.execute("ALTER TABLE contact ADD COLUMN value TEXT")
        if "platform" not in {r[1] for r in con.execute("PRAGMA table_info(device)")}:
            con.execute("ALTER TABLE device ADD COLUMN platform TEXT DEFAULT 'ios'")
        con.commit()
    return con
