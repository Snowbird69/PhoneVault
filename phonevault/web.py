"""The app's screens and the API behind them, served on 127.0.0.1 (this computer only)."""
import http.cookies
import json
import mimetypes
import os
import pathlib
import plistlib
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, desktop, device, ingest, media, passwords, runner, sysutil, vault
from .ingest import handle_key

STATIC = pathlib.Path(__file__).with_name("static")
REACTIONS = {2000: "❤️", 2001: "👍", 2002: "👎", 2003: "😂", 2004: "‼️", 2005: "❓"}


def _target_guid(assoc):
    if not assoc:
        return None
    return assoc.split("/")[-1].removeprefix("bp:")


def _q(q, key, default=""):
    return q.get(key, [default])[0]


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.local = threading.local()

    @property
    def ready(self) -> bool:
        return config.is_setup(self.cfg)

    @property
    def archive(self) -> pathlib.Path:
        # Before setup: a path that is never created (every write is blocked until setup)
        return config.archive_root(self.cfg) if self.ready else pathlib.Path(tempfile.gettempdir()) / "phonevault-not-set-up"

    def db(self):
        key = str(self.archive) if self.ready else ":memory:"
        if getattr(self.local, "key", None) != key:
            self.local.con = vault.connect(self.archive) if self.ready else vault.memory()
            self.local.key = key
        return self.local.con

    def names(self, udid):
        return {r["handle_key"]: r["name"] for r in
                self.db().execute("SELECT handle_key, name FROM contact WHERE udid = ?", (udid,))}

    def label(self, names, handle):
        return names.get(handle_key(handle)) or handle

    def one(self, sql, *args):
        return self.db().execute(sql, args).fetchone()[0]

    # ---------- overview ----------
    def devices(self, q):
        rows = self.db().execute("""SELECT d.udid, d.name, d.ios, d.product, coalesce(d.platform, 'ios') AS platform,
                (SELECT count(*) FROM message m WHERE m.udid = d.udid) AS messages,
                (SELECT count(*) FROM photo p WHERE p.udid = d.udid) AS photos
              FROM device d ORDER BY messages DESC""").fetchall()
        return [dict(r) for r in rows]

    def overview(self, q):
        udid = _q(q, "udid")
        o = {"archive": str(self.archive) if self.ready else None, "setup": self.ready, "free_bytes": None,
             "state": {k: runner.STATE[k] for k in ("running", "stage", "udid", "connected")},
             "log": runner.STATE["messages"][-12:], "auto_backup": self.cfg.get("auto_backup", True),
             "auto_devices": self.cfg["devices"], "autostart": desktop.autostart_enabled(),
             "pending": runner.STATE["pending"].get(_q(q, "udid").upper())}
        try:
            o["free_bytes"] = shutil.disk_usage(self.archive).free
        except OSError:
            pass
        if udid:
            o["counts"] = {
                "messages": self.one("SELECT count(*) FROM message WHERE udid=? AND assoc_type=0", udid),
                "chats": self.one("SELECT count(DISTINCT chat_guid) FROM message WHERE udid=?", udid),
                "attachments": self.one("SELECT count(*) FROM attachment WHERE udid=? AND stored_path IS NOT NULL", udid),
                "attachments_missing": self.one("""SELECT count(*) FROM attachment WHERE udid=?
                                                   AND stored_path IS NULL AND message_guid IS NOT NULL""", udid),
                "photos": len(self.photo_items(udid)),
                "photo_files": self.one("SELECT count(*) FROM photo WHERE udid=?", udid),
                "voicemail": self.one("SELECT count(*) FROM voicemail WHERE udid=?", udid),
                "memos": self.one("SELECT count(*) FROM memo WHERE udid=?", udid),
                "contacts": self.one("SELECT count(DISTINCT name) FROM contact WHERE udid=?", udid),
                "calls": self.one("SELECT count(*) FROM call WHERE udid=?", udid),
                "oldest": self.one("SELECT min(date) FROM message WHERE udid=? AND date > 0", udid),
                "newest": self.one("SELECT max(date) FROM message WHERE udid=?", udid),
            }
            r = self.db().execute("SELECT * FROM run WHERE udid=? ORDER BY id DESC LIMIT 1", (udid,)).fetchone()
            o["last_run"] = dict(r) if r else None
        return o

    # ---------- messages ----------
    def chats(self, q):
        udid = _q(q, "udid")
        names = self.names(udid)
        members = {}
        for r in self.db().execute("SELECT chat_guid, handle FROM chat_member WHERE udid = ?", (udid,)):
            members.setdefault(r["chat_guid"], []).append(r["handle"])
        out = []
        for r in self.db().execute("""SELECT c.guid, c.identifier, c.display_name, c.service,
                     count(m.guid) AS n, max(m.date) AS last
                   FROM chat c JOIN message m ON m.udid = c.udid AND m.chat_guid = c.guid
                   WHERE c.udid = ? AND m.assoc_type = 0
                   GROUP BY c.guid ORDER BY last DESC""", (udid,)):
            who = members.get(r["guid"]) or [r["identifier"]]
            title = r["display_name"] or ", ".join(self.label(names, h) for h in who if h)
            out.append({"guid": r["guid"], "title": title or "(unknown)", "count": r["n"],
                        "last": r["last"], "service": r["service"]})
        return out

    def _attachments(self, udid, guids):
        res = {}
        for i in range(0, len(guids), 500):
            chunk = guids[i:i + 500]
            qs = ",".join("?" * len(chunk))
            for a in self.db().execute(f"""SELECT guid, message_guid, mime, name, stored_path FROM attachment
                                           WHERE udid = ? AND message_guid IN ({qs})""", (udid, *chunk)):
                res.setdefault(a["message_guid"], []).append({
                    "id": a["guid"], "name": a["name"], "kind": media.kind(a["name"], a["mime"]),
                    "available": bool(a["stored_path"])})
        return res

    def messages(self, q):
        udid, chat = _q(q, "udid"), _q(q, "chat")
        before = float(_q(q, "before", "1e12"))
        limit = 1_000_000 if _q(q, "all") == "1" else min(int(_q(q, "limit", "300")), 2000)
        names = self.names(udid)
        rows = self.db().execute("""SELECT guid, date, is_from_me, sender, text FROM message
                                    WHERE udid = ? AND chat_guid = ? AND assoc_type = 0 AND date < ?
                                    ORDER BY date DESC LIMIT ?""", (udid, chat, before, limit)).fetchall()
        rows = list(reversed(rows))
        guids = [r["guid"] for r in rows]
        atts = self._attachments(udid, guids)
        reacts, state = {}, {}
        for r in self.db().execute("""SELECT assoc_type, assoc_guid, is_from_me, sender FROM message
                                      WHERE udid = ? AND chat_guid = ? AND assoc_type BETWEEN 2000 AND 3999
                                      ORDER BY date""", (udid, chat)):
            key = (_target_guid(r["assoc_guid"]), r["is_from_me"], r["sender"])
            if r["assoc_type"] >= 3000:
                state.pop(key, None)
            else:
                state[key] = REACTIONS.get(r["assoc_type"], "•")
        for (target, _me, _s), emoji in state.items():
            reacts.setdefault(target, []).append(emoji)
        return [{"guid": r["guid"], "date": r["date"], "me": bool(r["is_from_me"]),
                 "sender": None if r["is_from_me"] else self.label(names, r["sender"]),
                 "text": (r["text"] or "").replace("￼", "").strip(),
                 "attachments": atts.get(r["guid"], []), "reactions": reacts.get(r["guid"], [])}
                for r in rows]

    def search(self, q):
        udid, term = _q(q, "udid"), _q(q, "q").strip()
        if len(term) < 2:
            return []
        names = self.names(udid)
        rows = self.db().execute("""SELECT guid, chat_guid, date, is_from_me, sender, text FROM message
                                    WHERE udid = ? AND assoc_type = 0 AND text LIKE ?
                                    ORDER BY date DESC LIMIT 300""", (udid, f"%{term}%"))
        return [{"guid": r["guid"], "chat": r["chat_guid"], "date": r["date"], "me": bool(r["is_from_me"]),
                 "sender": None if r["is_from_me"] else self.label(names, r["sender"]),
                 "text": r["text"]} for r in rows]

    # ---------- other data ----------
    def photo_items(self, udid):
        """Group camera-roll files the way the Photos app shows them: a Live Photo
        (IMG_1234.HEIC + IMG_1234.MOV) and an edited copy (IMG_E1234.*) are one item."""
        groups = {}
        for r in self.db().execute("SELECT id, device_path, mtime FROM photo WHERE udid = ?", (udid,)):
            path = r["device_path"]
            folder, _, name = path.rpartition("/")
            stem, _, ext = name.rpartition(".")
            ext = ext.lower()
            if ext == "aae":
                continue
            edited = stem.upper().startswith("IMG_E")
            key = (folder, ("IMG_" + stem[5:]) if edited else stem)
            g = groups.setdefault(key, {})
            kind = media.kind(name, None)
            slot = ("edited_" if edited else "") + ("video" if kind == "video" else "image")
            g[slot] = r
        items = []
        for (_folder, stem), g in groups.items():
            img = g.get("edited_image") or g.get("image")
            vid = g.get("edited_video") or g.get("video")
            main = img or vid
            items.append({"id": main["id"], "name": main["device_path"].rsplit("/", 1)[-1],
                          "date": main["mtime"], "kind": "image" if img else "video",
                          "live": vid["id"] if (img and vid) else None, "edited": bool(g.get("edited_image"))})
        items.sort(key=lambda i: -(i["date"] or 0))
        return items

    def photos(self, q):
        udid, offset = _q(q, "udid"), int(_q(q, "offset", "0"))
        return self.photo_items(udid)[offset:offset + 240]

    def voicemail(self, q):
        udid = _q(q, "udid")
        names = self.names(udid)
        return [{"id": r["vm_id"], "date": r["date"], "duration": r["duration"],
                 "from": self.label(names, r["sender"]) if r["sender"] else "Unknown caller"}
                for r in self.db().execute("SELECT * FROM voicemail WHERE udid=? ORDER BY date DESC", (udid,))]

    def memos(self, q):
        udid = _q(q, "udid")
        return [{"name": r["name"], "date": r["date"], "size": r["size"]}
                for r in self.db().execute("SELECT * FROM memo WHERE udid=? ORDER BY date DESC", (udid,))]

    def contacts(self, q):
        udid = _q(q, "udid")
        counts = {}
        for r in self.db().execute("""SELECT sender, count(*) AS n FROM message
                                      WHERE udid=? AND sender IS NOT NULL GROUP BY sender""", (udid,)):
            k = handle_key(r["sender"])
            counts[k] = counts.get(k, 0) + r["n"]
        people = {}
        for r in self.db().execute("SELECT handle_key, name, value FROM contact WHERE udid=? ORDER BY name", (udid,)):
            p = people.setdefault(r["name"], {"name": r["name"], "values": [], "messages": 0})
            if r["value"] and r["value"] not in p["values"]:
                p["values"].append(r["value"])
            p["messages"] += counts.get(r["handle_key"], 0)
        return sorted(people.values(), key=lambda p: (-p["messages"], p["name"].lower()))

    def vcf(self, udid) -> bytes:
        out = []
        for p in self.contacts({"udid": [udid]}):
            out += ["BEGIN:VCARD", "VERSION:3.0", f"FN:{p['name']}"]
            for v in p["values"]:
                out.append(f"EMAIL:{v}" if "@" in v else f"TEL:{v}")
            out.append("END:VCARD")
        return ("\r\n".join(out) + "\r\n").encode()

    def calls(self, q):
        udid = _q(q, "udid")
        names = self.names(udid)
        out = []
        for r in self.db().execute("SELECT * FROM call WHERE udid=? ORDER BY date DESC LIMIT 5000", (udid,)):
            who = r["name"] or (self.label(names, r["address"]) if r["address"] else None)
            kind = {1: "Phone", 8: "FaceTime video", 16: "FaceTime audio", 101: "Phone", 102: "Video call"}.get(r["kind"], "Call")
            out.append({"date": r["date"], "who": who or "Unknown", "number": r["address"],
                        "duration": r["duration"] or 0, "outgoing": bool(r["outgoing"]),
                        "answered": bool(r["answered"]), "kind": kind})
        return out

    def encryption(self, q):
        """Is backup encryption on (asks the phone if it's plugged in and idle), is the
        password saved, and is the system password vault usable."""
        udid = _q(q, "udid").upper()
        on = None
        if self.platform(udid) == "android":
            return {"platform": "android", "calls": self.one("SELECT count(*) FROM call WHERE udid=?", udid)}
        if udid and not runner.STATE["running"] and udid in {d["udid"] for d in runner.STATE["connected"] or []}:
            on = device.will_encrypt(udid)
        latest = self.archive / "backups" / udid
        bi = ingest.backup_info(latest) if (latest / "Manifest.db").exists() else None
        return {"phone_encrypts": on, "saved": bool(udid and passwords.get(udid)),
                "latest_backup_encrypted": bi["encrypted"] if bi else None,
                "vault": passwords.vault_status() if _q(q, "check") == "1" else None,
                "calls": self.one("SELECT count(*) FROM call WHERE udid=?", udid) if udid else 0}

    def platform(self, udid: str) -> str:
        """'android' or 'ios' for a phone (connected or in the archive)."""
        for d in runner.STATE["connected"] or []:
            if d["udid"] == (udid or "").upper():
                return d.get("platform", "ios")
        r = self.db().execute("SELECT platform FROM device WHERE udid=?", ((udid or "").upper(),)).fetchone()
        return (r["platform"] if r and r["platform"] else "ios")

    def android_info(self, q):
        from . import android
        return {"adb": android.available(), "windows": sysutil.IS_WIN,
                "phones": [d for d in runner.STATE["connected"] or [] if d.get("platform") == "android"]}

    def act_pick_xml(self, body):
        paths = sysutil.pick_files("Choose SMS Backup & Restore files (sms-*.xml, calls-*.xml)", "Backup files", ["xml"])
        return {"ok": bool(paths), "paths": paths or [], "error": None if paths else "No file chosen."}

    def act_android_import(self, body):
        paths = [p for p in body.get("paths") or [] if p]
        bad = [p for p in paths if not pathlib.Path(p).is_file()]
        if not paths or bad:
            return {"ok": False, "error": "Choose the sms-….xml (and calls-….xml) files from SMS Backup & Restore."}
        udid = (body.get("udid") or "").upper()
        name = None
        if not udid:  # no phone chosen: keep it as its own entry
            udid, name = "ANDROID-IMPORT", "Imported Android messages"
        if runner.STATE["running"]:
            return {"ok": False, "error": "Another job is running. Wait for it to finish."}
        threading.Thread(target=runner.run_android_import, args=(paths, udid, name, self.cfg), daemon=True).start()
        return {"ok": True}

    def act_export_android(self, body):
        """Messages + calls of any archived phone as SMS Backup & Restore files (for moving to Android)."""
        from . import android
        udid = (body.get("udid") or "").upper()
        out = pathlib.Path.home() / "Downloads"
        out = (out if out.is_dir() else pathlib.Path.home()) / f"PhoneVault-export-{time.strftime('%Y%m%d-%H%M')}"
        try:
            files = android.export_xml(self.db(), self.archive, udid, out, media=bool(body.get("media", True)), log=runner._log)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "folder": str(out), "files": [f.name for f in files]}

    def act_android_restore(self, body):
        udid, src = (body.get("udid") or "").upper(), (body.get("source") or "").upper()
        phone = next((d for d in runner.STATE["connected"] or device.list_devices() if d["udid"] == udid), None)
        if not phone or phone.get("platform") != "android" or phone.get("state") != "ready":
            return {"ok": False, "error": "Plug in the Android phone and allow USB debugging on it."}
        if not (body.get("messages") or body.get("photos")):
            return {"ok": False, "error": "Choose messages, photos, or both."}
        if runner.STATE["running"]:
            return {"ok": False, "error": "Another job is running. Wait for it to finish."}
        threading.Thread(target=runner.run_android_restore,
                         args=(udid, src or udid, bool(body.get("messages")), bool(body.get("photos")), self.cfg),
                         daemon=True).start()
        return {"ok": True}

    def restore_sources(self, q):
        """Backups that can go back on a phone: the archive's own, the pre-restore snapshots,
        and folders imported earlier."""
        cands = []
        for pat in ("backups/*", "backups-idb/*", "snapshots/*/*"):
            cands += sorted(self.archive.glob(pat))
        cands += [pathlib.Path(r[0]) for r in self.db().execute("SELECT DISTINCT path FROM source")]
        seen, out = set(), []
        for p in cands:
            key = str(p.resolve()) if p.exists() else str(p)
            if key in seen or not (p / "Manifest.db").exists() or not (p / "Info.plist").exists():
                continue
            seen.add(key)
            bi = ingest.backup_info(p)
            rel = str(p.relative_to(self.archive)) if str(p).startswith(str(self.archive)) else None
            bi["where"] = ("Snapshot before a restore" if rel and rel.startswith("snapshots") else
                           "Latest backup (this archive)" if rel else "Imported folder")
            bi["password_saved"] = bool(bi["encrypted"] and passwords.get(bi["udid"]))
            out.append(bi)
        return sorted(out, key=lambda b: -(b["date"] or 0))

    def status(self, q):
        return {"runs": [dict(r) for r in self.db().execute("SELECT * FROM run ORDER BY id DESC LIMIT 30")],
                "sources": [dict(r) for r in self.db().execute("SELECT * FROM source ORDER BY id DESC LIMIT 30")],
                "log": runner.STATE["messages"][-60:]}

    def scan(self, q):
        """Find iPhone backup folders (they contain Manifest.db) in the usual places."""
        home = pathlib.Path.home()
        roots = sysutil.scan_roots(home)
        imported = {r[0] for r in self.db().execute("SELECT DISTINCT path FROM source")}
        found = []
        for root in roots:
            if not root.is_dir():
                continue
            base = len(root.parts)
            for dirpath, dirs, files in os.walk(root, onerror=lambda e: None):
                p = pathlib.Path(dirpath)
                if "Manifest.db" in files:
                    dirs[:] = []
                    info = {}
                    try:
                        with open(p / "Info.plist", "rb") as f:
                            info = plistlib.load(f)
                    except Exception:
                        pass
                    lbd = info.get("Last Backup Date")
                    found.append({"path": str(p), "name": info.get("Device Name"),
                                  "ios": info.get("Product Version"),
                                  "date": lbd.timestamp() if hasattr(lbd, "timestamp") else None,
                                  "imported": str(p) in imported,
                                  "live": str(p).startswith(str(self.archive / "backups"))})
                    continue
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in sysutil.SCAN_SKIP]
                if len(p.parts) - base >= 5:
                    dirs[:] = []
        return sorted(found, key=lambda f: -(f["date"] or 0))

    def music_path(self) -> pathlib.Path | None:
        """The chosen song, else the one bundled with this install; None if music is off."""
        if not self.cfg.get("music_enabled", True):
            return None
        p = pathlib.Path(self.cfg.get("music") or "")
        if self.cfg.get("music") and p.is_file():
            return p
        return bundled_song()

    def music(self, q):
        p = self.music_path()
        b = bundled_song()
        return {"set": bool(p), "name": p.stem if p else None, "bundled": b.stem if b else None,
                "is_bundled": bool(p and b and p == b), "enabled": self.cfg.get("music_enabled", True)}

    def act_pick_music(self, body):
        path = sysutil.pick_audio("Choose a song for PhoneVault")
        if not path:
            return {"ok": False, "error": "No song chosen."}
        self.cfg["music"] = path
        self.cfg["music_enabled"] = True
        config.save(self.cfg)
        return {"ok": True} | self.music({})

    def act_clear_music(self, body):
        """Turn music off."""
        self.cfg["music_enabled"] = False
        config.save(self.cfg)
        return {"ok": True}

    def act_bundled_music(self, body):
        """Go back to the song that came with this install."""
        self.cfg["music"] = ""
        self.cfg["music_enabled"] = True
        config.save(self.cfg)
        return {"ok": True} | self.music({})

    # ---------- first-run setup ----------
    def setup_info(self, q):
        """Suggested storage folders (largest free space first) for the setup screen."""
        home = pathlib.Path.home()
        if sysutil.IS_WIN:
            import string
            bases = [pathlib.Path(f"{d}:\\") for d in string.ascii_uppercase[2:] if os.path.exists(f"{d}:\\")]
        else:
            bases = [*pathlib.Path("/mnt").glob("*"), *pathlib.Path("/run/media").glob("*/*"), *pathlib.Path("/media").glob("*/*")]
        sugg, seen = [], set()
        for base in [home, *bases]:
            try:
                if not base.is_dir():
                    continue
                du = shutil.disk_usage(base)
            except OSError:
                continue
            dev = (du.total, du.free // (1 << 30))  # same drive mounted twice -> show once
            if dev in seen:
                continue
            seen.add(dev)
            sugg.append({"path": str(base / "PhoneVault"), "free": du.free, "total": du.total,
                         "label": "Home folder" if base == home else str(base)})
        sugg.sort(key=lambda s: -s["free"])
        return {"done": self.ready, "archive": self.cfg.get("archive") or None, "suggestions": sugg,
                "connected": runner.STATE["connected"] or device.list_devices(), "music": self.music({}),
                "autostart": desktop.autostart_enabled(), "windows": sysutil.IS_WIN, "adb": self.android_info({})["adb"]}

    def check_folder(self, path: str, create: bool = False) -> dict:
        if not path:
            return {"ok": False, "error": "Choose a folder."}
        p = pathlib.Path(path).expanduser()
        if not p.is_absolute():
            return {"ok": False, "error": "Use a full path (for example " + ("D:\\PhoneVault" if sysutil.IS_WIN else "/mnt/storage/PhoneVault") + ")."}
        probe = p
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError as e:
            return {"ok": False, "error": f"Can't reach that drive: {e}"}
        try:  # only creates the folder on Finish; the check writes a test file in the nearest existing folder
            if create:
                p.mkdir(parents=True, exist_ok=True)
                probe = p
            if not probe.is_dir():
                return {"ok": False, "error": f"{probe} is a file, not a folder."}
            with tempfile.NamedTemporaryFile(dir=probe, prefix=".phonevault-write-test"):
                pass
        except OSError as e:
            return {"ok": False, "error": f"Can't write there ({probe}): {e.strerror or e}"}
        existing = (p / "vault.sqlite").exists()
        warn = None
        if free < 100 * (1 << 30):
            warn = f"Only {free / 1e9:.0f} GB free. A full iPhone backup can need as much space as the phone uses, plus photos."
        return {"ok": True, "path": str(p), "free": free, "existing": existing, "warning": warn}

    def act_setup_check(self, body):
        return self.check_folder((body.get("path") or "").strip())

    def act_pick_archive(self, body):
        path = sysutil.pick_folder("Choose where PhoneVault keeps your backups")
        if not path:
            return {"ok": False, "error": "No folder chosen."}
        return self.check_folder(path)

    def act_setup(self, body):
        chk = self.check_folder((body.get("archive") or "").strip(), create=True)
        if not chk["ok"]:
            return chk
        cfg = self.cfg
        cfg["archive"] = chk["path"]
        cfg["devices"] = [str(u).upper() for u in body.get("devices") or []]
        cfg["music_enabled"] = bool(body.get("music", True))
        vault.connect(pathlib.Path(chk["path"])).close()
        config.save(cfg)
        notes = []
        if "autostart" in body:
            r = desktop.set_autostart(bool(body["autostart"]))
            if not r["ok"]:
                notes.append("Start at login: " + (r["error"] or "failed"))
        runner._log(f"Setup done. Storage: {chk['path']}")
        return {"ok": True, "archive": chk["path"], "notes": notes}

    def settings(self, q):
        return {k: self.cfg.get(k) for k in config.DEFAULTS} | {"autostart": desktop.autostart_enabled()}

    # ---------- actions ----------
    def act_backup(self, body):
        udid = (body.get("udid") or "").upper()
        if runner.STATE["running"]:
            return {"ok": False, "error": "Another job is running. Wait for it to finish."}
        devs = runner.STATE["connected"] or device.list_devices()
        d = next((x for x in devs if x["udid"] == udid), None)
        if not d:
            return {"ok": False, "error": "That phone isn't plugged in."}
        if d.get("state", "ready") != "ready":
            return {"ok": False, "error": ANDROID_STATE_HELP.get(d["state"], "The phone isn't ready yet.")}
        if body.get("photos_only"):
            threading.Thread(target=runner.run_photos, args=(udid, self.cfg), daemon=True).start()
        else:
            threading.Thread(target=runner.run_pipeline, args=(udid, self.cfg), daemon=True).start()
        return {"ok": True}

    def act_import(self, body):
        path = (body.get("path") or "").strip()
        if not path or not (pathlib.Path(path) / "Manifest.db").exists():
            return {"ok": False, "error": "That folder isn't an iPhone backup. Pick the folder that holds Manifest.db."}
        if runner.STATE["running"]:
            return {"ok": False, "error": "Another job is running. Wait for it to finish."}
        threading.Thread(target=runner.run_import, args=(path, self.cfg), daemon=True).start()
        return {"ok": True}

    def act_pick_folder(self, body):
        path = sysutil.pick_folder("Choose an iPhone backup folder")
        return {"ok": bool(path), "path": path, "error": None if path else "No folder chosen."}

    def act_settings(self, body):
        cfg = self.cfg
        if "min_gap_hours" in body:
            cfg["min_gap_hours"] = max(0, float(body["min_gap_hours"]))
        if "auto_backup" in body:
            cfg["auto_backup"] = bool(body["auto_backup"])
        if "devices" in body:
            cfg["devices"] = [str(u).upper() for u in body["devices"]]
        note = None
        if body.get("archive") and body["archive"] != cfg["archive"]:
            cfg["archive"] = body["archive"]
            note = "Archive folder changed. Restart PhoneVault for it to take effect."
        config.save(cfg)
        return {"ok": True, "note": note}

    def act_encrypt(self, body):
        udid, pw = (body.get("udid") or "").upper(), body.get("password") or ""
        if len(pw) < 8:
            return {"ok": False, "error": "Use at least 8 characters."}
        if runner.STATE["running"]:
            return {"ok": False, "error": "Another job is running. Wait for it to finish."}
        if udid not in {d["udid"] for d in (runner.STATE["connected"] or device.list_devices())}:
            return {"ok": False, "error": "That iPhone isn't plugged in."}
        if self.platform(udid) == "android":
            return {"ok": False, "error": "Encrypted backups are an iPhone setting. Android call history is backed up without it."}
        vs = passwords.vault_status()
        if not vs["ok"]:
            return {"ok": False, "error": vs["error"] + " Nothing was changed on the phone."}
        threading.Thread(target=runner.run_encrypt, args=(udid, pw, self.cfg), daemon=True).start()
        return {"ok": True}

    def act_save_password(self, body):
        """Encryption is already on and the user knows the password: check it against the
        newest encrypted backup (if there is one), then keep it in the password vault."""
        udid, pw = (body.get("udid") or "").upper(), body.get("password") or ""
        if not udid or not pw:
            return {"ok": False, "error": "Enter the password."}
        vs = passwords.vault_status()
        if not vs["ok"]:
            return {"ok": False, "error": vs["error"]}
        folder = self.archive / "backups" / udid
        checked = False
        if (folder / "Manifest.db").exists() and ingest.backup_info(folder)["encrypted"]:
            try:
                ingest.Backup(folder, password=pw, tmp_root=self.archive / "tmp").close()
                checked = True
            except Exception as e:
                return {"ok": False, "error": str(e)}
        passwords.put(udid, pw)
        return {"ok": True, "checked": checked}

    def act_forget_password(self, body):
        passwords.remove((body.get("udid") or "").upper())
        return {"ok": True}

    def act_restore(self, body):
        udid = (body.get("udid") or "").upper()
        src = body.get("source") or ""
        if runner.STATE["running"]:
            return {"ok": False, "error": "Another job is running. Wait for it to finish."}
        phone = next((d for d in (runner.STATE["connected"] or device.list_devices()) if d["udid"] == udid), None)
        if not phone:
            return {"ok": False, "error": "That iPhone isn't plugged in."}
        if phone.get("platform") == "android":
            return {"ok": False, "error": "For Android phones use 'Copy to this Android phone' on this page."}
        if src not in {b["path"] for b in self.restore_sources({})}:
            return {"ok": False, "error": "Pick a backup from the list."}
        typed = (body.get("confirm") or "").strip().casefold()
        if not typed or typed != (phone["name"] or "").strip().casefold():
            return {"ok": False, "error": f"Type the phone's name exactly ({phone['name']}) to confirm."}
        threading.Thread(target=runner.run_restore, args=(udid, src, bool(body.get("remove")), self.cfg),
                         daemon=True).start()
        return {"ok": True}

    def act_desktop_icon(self, body):
        return desktop.install_desktop_entry(desktop_icon=True)

    def act_autostart(self, body):
        return desktop.set_autostart(bool(body.get("enabled")))

    def act_menu_entry(self, body):
        return desktop.install_desktop_entry()

    # ---------- files ----------
    def file_for(self, kind, udid, ident, q):
        if kind == "media":
            r = self.db().execute("SELECT stored_path FROM attachment WHERE udid=? AND guid=?", (udid, ident)).fetchone()
        elif kind == "photo":
            r = self.db().execute("SELECT stored_path FROM photo WHERE udid=? AND id=?", (udid, int(ident))).fetchone()
        elif kind == "vm":
            r = self.db().execute("SELECT stored_path FROM voicemail WHERE udid=? AND vm_id=?", (udid, ident)).fetchone()
        elif kind == "memo":
            r = self.db().execute("SELECT stored_path FROM memo WHERE udid=? AND name=?", (udid, ident)).fetchone()
        else:
            return None
        if not r or not r["stored_path"]:
            return None
        root = self.archive.resolve()
        src = (self.archive / r["stored_path"]).resolve()
        if not str(src).startswith(str(root) + os.sep) or not src.exists():
            return None
        return media.playable(self.archive, src, thumb=_q(q, "thumb") == "1")


def make_handler(app: App, token: str | None = None):
    """token=None: the full app on 127.0.0.1. token set: read-only phone view on the network."""
    gets = {"devices": app.devices, "overview": app.overview, "chats": app.chats, "messages": app.messages,
            "search": app.search, "photos": app.photos, "voicemail": app.voicemail, "memos": app.memos,
            "contacts": app.contacts, "status": app.status, "scan": app.scan, "settings": app.settings,
            "calls": app.calls, "encryption": app.encryption, "restore-sources": app.restore_sources,
            "music": app.music, "setup": app.setup_info, "android": app.android_info,
            "phoneview": lambda q: phone_status()}
    posts = {"backup": app.act_backup, "import": app.act_import, "pick-folder": app.act_pick_folder,
             "settings": app.act_settings, "autostart": app.act_autostart, "menu-entry": app.act_menu_entry,
             "encrypt": app.act_encrypt, "save-password": app.act_save_password,
             "forget-password": app.act_forget_password, "restore": app.act_restore,
             "desktop-icon": app.act_desktop_icon, "pick-music": app.act_pick_music,
             "clear-music": app.act_clear_music, "bundled-music": app.act_bundled_music,
             "setup": app.act_setup, "setup-check": app.act_setup_check, "pick-archive": app.act_pick_archive,
             "pick-xml": app.act_pick_xml, "android-import": app.act_android_import,
             "export-android": app.act_export_android, "android-restore": app.act_android_restore,
             "phoneview": lambda b: start_phone_view(app) if b.get("on") else stop_phone_view()}
    if token:
        gets = {k: v for k, v in gets.items() if k in
                ("devices", "overview", "chats", "messages", "search", "photos", "voicemail", "memos", "contacts", "calls")}
        posts = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _local_host(self) -> bool:
            """Refuse requests whose Host isn't this computer. Stops DNS rebinding, where a
            website points its own name at 127.0.0.1 to read this app through the browser."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def _authorized(self, q) -> bool:
            if not token:
                return self._local_host()
            if token != PHONE["token"]:
                return False
            if secrets.compare_digest(_q(q, "t"), token):
                self._set_cookie = True
                return True
            c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return "pv" in c and secrets.compare_digest(c["pv"].value, token)

        def end_headers(self):
            if getattr(self, "_set_cookie", False):
                self.send_header("Set-Cookie", f"pv={token}; Path=/; Max-Age={PHONE_MINUTES * 60}; HttpOnly; SameSite=Strict")
                self._set_cookie = False
            super().end_headers()

        def _send(self, data: bytes, ctype: str, code=200, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj).encode(), "application/json", code, {"Cache-Control": "no-store"})

        def _file(self, path: pathlib.Path, ctype=None, cache="private, max-age=86400"):
            size = path.stat().st_size
            ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].partition("-")
                if a:
                    start, end = int(a), (int(b) if b else end)
                else:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
            if not self._authorized(q):
                return self._send(b"<!doctype html><meta name=viewport content='width=device-width'>"
                                  b"<body style='font-family:sans-serif;background:#14112a;color:#eeeaff;padding:24px'>"
                                  b"<h2>This link has expired</h2><p>Start phone view again in PhoneVault on your computer "
                                  b"and scan the new code.</p>", "text/html; charset=utf-8", 403)
            try:
                if parts == ["api", "overview"] and token:
                    return self._json(app.overview(q) | {"readonly": True})
                if not parts:
                    return self._file(STATIC / "index.html", "text/html; charset=utf-8", "no-store")
                if parts[0] == "static" and len(parts) >= 2:
                    f = (STATIC / "/".join(parts[1:])).resolve()
                    if str(f).startswith(str(STATIC.resolve()) + os.sep) and f.is_file():
                        return self._file(f)
                if parts == ["api", "contacts.vcf"]:
                    return self._send(app.vcf(_q(q, "udid")), "text/vcard",
                                      extra={"Content-Disposition": 'attachment; filename="contacts.vcf"'})
                if parts[0] == "api" and len(parts) == 2 and parts[1] in gets:
                    return self._json(gets[parts[1]](q))
                if parts == ["music"] and not token and app.music_path():
                    return self._file(app.music_path(), cache="no-store")
                if parts[0] in ("media", "photo", "vm", "memo") and len(parts) == 3:
                    f = app.file_for(parts[0], parts[1], parts[2], q)
                    if f:
                        return self._file(f)
                self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self._json({"error": str(e)}, 500)
                except Exception:
                    pass

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            parts = [p for p in u.path.strip("/").split("/") if p]
            n = int(self.headers.get("Content-Length") or 0)
            if token:
                return self._json({"ok": False, "error": "Phone view is read-only"}, 403)
            if not self._local_host():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                # same-origin check: only this app's own page may trigger actions
                origin = self.headers.get("Origin")
                if origin and urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                    return self._json({"ok": False, "error": "forbidden"}, 403)
                if len(parts) == 2 and parts[0] == "api" and parts[1] in posts:
                    if not app.ready and parts[1] not in SETUP_SAFE:
                        return self._json({"ok": False, "error": "Finish setup first: choose where PhoneVault stores backups."})
                    return self._json(posts[parts[1]](body))
                self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)

    return H


ANDROID_STATE_HELP = {
    "unauthorized": "Unlock the Android phone and tap Allow on 'Allow USB debugging?' (tick Always allow).",
    "no-permission": "This computer isn't allowed to use the phone's USB port yet. Re-run the installer (it adds "
                     "Android USB rules), then unplug and replug the phone.",
    "offline": "The phone isn't answering. Unplug it, unlock it and plug it back in.",
}

# actions allowed before a storage folder is chosen
SETUP_SAFE = {"setup", "setup-check", "pick-archive", "pick-music", "clear-music", "bundled-music",
              "autostart", "menu-entry", "desktop-icon"}


def bundled_song() -> pathlib.Path | None:
    """A song shipped inside this install (static/music/), used when none is chosen."""
    d = STATIC / "music"
    songs = sorted(p for p in d.glob("*") if p.suffix.lower().lstrip(".") in sysutil.AUDIO_EXT) if d.is_dir() else []
    return songs[0] if songs else None


PHONE = {"srv": None, "token": None, "expires": 0, "url": None, "timer": None}
PHONE_MINUTES = 30


def lan_ip() -> str:
    """This computer's address on the home network (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def phone_status():
    left = max(0, int(PHONE["expires"] - time.time())) if PHONE["srv"] else 0
    out = {"on": bool(PHONE["srv"]), "url": PHONE["url"], "seconds_left": left, "qr_svg": None}
    if PHONE["srv"] and PHONE["url"]:
        try:
            import segno
            out["qr_svg"] = segno.make(PHONE["url"], error="m").svg_inline(scale=7, dark="#14112a", light="#ffffff", border=2)
        except ImportError:
            out["qr_svg"] = None
    return out


def stop_phone_view():
    srv = PHONE["srv"]
    if PHONE["timer"]:
        PHONE["timer"].cancel()
    PHONE.update(srv=None, token=None, expires=0, url=None, timer=None)
    if srv:
        threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
    return phone_status()


def start_phone_view(app: "App"):
    """Read-only copy of the app on the home network for PHONE_MINUTES, behind a private link."""
    if PHONE["srv"]:
        return phone_status()
    port = int(app.cfg.get("phone_port", 8738))
    token = secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app, token=token))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    timer = threading.Timer(PHONE_MINUTES * 60, stop_phone_view)
    timer.daemon = True
    timer.start()
    PHONE.update(srv=srv, token=token, expires=time.time() + PHONE_MINUTES * 60,
                 url=f"http://{lan_ip()}:{port}/?t={token}", timer=timer)
    runner._log(f"Phone view on for {PHONE_MINUTES} minutes")
    return phone_status()


def serve(cfg=None, block=True):
    cfg = cfg or config.load()
    app = App(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", int(cfg["port"])), make_handler(app))
    srv.daemon_threads = True
    print(f"PhoneVault running at http://127.0.0.1:{cfg['port']}  (Ctrl+C to stop)", flush=True)
    if block:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
    else:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
