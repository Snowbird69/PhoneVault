# PhoneVault 0.6 (Windows 10/11 and Linux; iPhone and Android)

Plug in an iPhone or an Android phone and it backs up. Messages are merged into one permanent archive, so
nothing disappears when the phone deletes old texts. Browse chats, photos and voice notes
in the app (this computer only; optional read-only phone view on home Wi-Fi).

## Download
Get `PhoneVault-<version>-setup.zip` from this repository's **Releases** page.
Optional: compare its SHA-256 with the `.sha256` file published next to it.

## Install
Unzip `PhoneVault-<version>-setup.zip`, then:
- **Windows 10/11:** double-click `Install-Windows.cmd`. It installs what's missing: Python 3.13,
  Apple Devices (Apple's iPhone driver, Microsoft Store), FFmpeg and Android platform-tools (adb),
  then PhoneVault. The installer isn't code-signed: to skip the "Windows protected your PC"
  prompt, right-click the zip > Properties > Unblock before unzipping.
- **Linux:** `bash install-linux.sh` (asks for your password to add what's missing: usbmuxd,
  adb + Android USB rules, ffmpeg, imagemagick).

From source instead: `python -m venv ~/.venvs/phonevault && ~/.venvs/phonevault/bin/pip install .`
then `~/.venvs/phonevault/bin/phonevault gui`.

The first time the app opens it asks where to store backups, which iPhone to back up
automatically, and whether to start at login. Full details: `READ-ME-FIRST.txt` in the zip.

## Android
Any Android phone with USB debugging turned on (steps in READ-ME-FIRST.txt); no root.
Backed up: texts + picture messages and their attachments, Google Messages RCS (stored by
Google Messages as MMS), Samsung chat text (best effort), call history, contacts, photos and
videos (DCIM, Pictures, Movies), voice recordings (Recordings). Everything merges into the same
archive and pages as the iPhone. How it reads them: Android's own `content` command through adb,
using the READ_SMS / READ_CALL_LOG / READ_CONTACTS permissions the adb shell has (AOSP
`packages/Shell/AndroidManifest.xml`); output parsed per AOSP `Content.java`.
Limits: Android shows other apps only inbox + sent messages (no drafts/failed); messages tied to
a SIM the phone no longer knows may be hidden; Android 17 encrypted RCS and some sent RCS need the
SMS Backup & Restore import; app data (WhatsApp etc.) isn't reachable.
- **Import** SMS Backup & Restore `.xml` files: Backups & import > Android.
- **Export** any phone's messages/calls as SMS Backup & Restore files, or copy them plus photos
  straight onto a plugged-in Android phone: Restore to phone (Android).

## Make an installer for another computer
`phonevault make-installer` writes `PhoneVault-<version>-setup.zip` to Downloads, including
the song currently playing in the app (it becomes the default song there, on loop).
Options: `--song <file>`, `--no-song`, `--out <folder>`.

## What it does on plug-in
1. Spots the phone (checks every 5 s). Only phones listed in the settings auto-backup.
2. Skips if the last good backup is under 12 h old (so charging doesn't trigger repeats).
3. Backs up into `<archive>/backups/<UDID>` with pymobiledevice3; retries with
   idevicebackup2 into `<archive>/backups-idb/<UDID>` if that fails.
   **You must unlock the phone and enter the passcode when it asks.**
4. Merges messages into `<archive>/vault.sqlite` and copies attachments to
   `<archive>/library/<UDID>/attachments/`. Rows are never deleted.
5. Copies new camera-roll files (DCIM) to `<archive>/library/<UDID>/photos/`.
6. Desktop notification with the counts; warns if the phone holds fewer messages than last time.

Old backups you import are opened read-only - nothing is written into them.

## The app
Open it from the app menu (PhoneVault) or with `phonevault gui`. With Chromium, Chrome, Brave
or Vivaldi installed it opens in its own window; otherwise in your default browser.

| Section | What it does |
| --- | --- |
| Overview | Phone status, Back up now, Import a backup, Copy new photos, totals, activity. "Files that need to be backed up" counts camera-roll photos/videos on the phone that aren't in the archive yet (checked every 2 minutes while plugged in; a Live Photo or edited photo counts as 1) |
| Messages | Every conversation merged across backups; search; Save as PDF |
| Photos | Camera-roll copies grouped like the Photos app (Live Photo still + clip = one item, edited copy shown instead of the original); view, play, save |
| Voicemail / Voice memos | Play recordings found in backups |
| Contacts | Names and numbers; export as .vcf |
| Call history | Calls from encrypted backups; turn on encrypted backups here (password kept in KDE Wallet) |
| Backups & import | Choose a folder, find backups automatically, or paste a path; run history |
| Phone view | Start it, scan the QR code with the iPhone: a read-only copy of the app in the phone's browser on home Wi-Fi for 30 minutes (port 8738) |
| Restore to phone | iPhone: put a saved backup back (safety backup + dated snapshot first, iOS version check, type the phone's name). Android: copy messages/calls (via SMS Backup & Restore) and photos onto the phone |
| Settings | Background music (pick any song file you own; plays on loop, play/pause + volume in the sidebar), auto-backup on/off, phones, wait time, start at login, archive folder, add to app menu |

## Commands
| Command | What it does |
| --- | --- |
| `phonevault import <folder> [...]` | Merge existing backup folder(s) (the one named after the UDID), read-only |
| `phonevault backup` | Back up the plugged-in phone now, merge, pull photos |
| `phonevault photos` | Copy new camera-roll photos only |
| `phonevault serve` | Viewer only |
| `phonevault run` | Viewer + auto-backup on plug-in (what the service runs) |
| `phonevault status` | Counts and the last 5 runs |
| `phonevault gui` | Open the app window (starts PhoneVault if needed) |
| `phonevault install-desktop` | Add PhoneVault to the app menu and the Desktop (`--no-desktop-icon`, `--remove`) |
| `phonevault autostart on` / `off` | Start PhoneVault in the background at login |
| `phonevault make-installer` | Build the setup zip for another Windows/Linux computer (includes the current song) |
| `phonevault uninstall` | Remove shortcuts and start-at-login (keeps settings and archive) |

Full path: Linux `~/.venvs/phonevault/bin/phonevault`; Windows `%LOCALAPPDATA%\\PhoneVault\\venv\\Scripts\\phonevault.exe`.

## Run it automatically at login
Settings > Start PhoneVault at login (switch). It installs a systemd user service named
`phonevault`. Check it: `systemctl --user status phonevault`. Live log: `journalctl --user -u phonevault -f`.

## Settings
`~/.config/phonevault/config.toml` (open in Kate). Keys: `archive`, `port`, `min_gap_hours`,
`poll_seconds`, `devices` (list of UDIDs to auto-backup). Restart the service after editing:
`systemctl --user restart phonevault`.

## Where things live
| Path | Contents |
| --- | --- |
| `<archive>/vault.sqlite` | Merged messages, contacts, photo index, run history |
| `<archive>/backups/<UDID>` | Latest Apple-format backup (updated each run) |
| `<archive>/library/<UDID>/attachments` | Every message attachment ever seen |
| `<archive>/library/<UDID>/photos` | Camera-roll originals |
| `<archive>/cache` | Browser-friendly copies (HEIC->JPEG, CAF->M4A, MOV->MP4). Safe to delete; rebuilt on demand |
| `<archive>/logs/backup.log` | Full backup tool output |
| `<archive>/snapshots/pre-restore-*` | Safety backups taken right before a restore |

## Encrypted backups (call history)
Call history, saved passwords, Wi-Fi and Health are only in encrypted backups.
1. Call history page > type a password twice > tick "written on paper" > Turn on encrypted backups.
2. Unlock the phone and enter its passcode if it asks.
3. The password goes into the system password vault (KDE Wallet, service "PhoneVault iPhone backup"),
   never into PhoneVault's files. See it in KWalletManager.
4. The next backup is a full one. PhoneVault decrypts into a temporary folder inside the archive and
   deletes it after each import.

Lost the password? On the iPhone: Settings > General > Transfer or Reset iPhone > Reset > Reset All
Settings removes it (resets Wi-Fi and wallpaper, deletes no data). Then set a new one.

## Restore to phone
1. On the iPhone turn off Find My iPhone (iOS refuses a restore while it's on).
2. Restore to phone page > pick a backup > type the phone's name > Restore.
3. PhoneVault backs up the phone first and keeps it as `<archive>/snapshots/pre-restore-<date>/` - restore
   that one to undo. The chosen backup is never changed (a hard-linked working copy is used).
4. A backup can only go onto the same or a newer iOS version.

## Security
- The app only answers this computer (127.0.0.1) and refuses requests addressed to any other name,
  which blocks websites from reading it through your browser (DNS rebinding).
- Buttons only work from the app's own page; other websites get "forbidden".
- Phone view is read-only, needs the private QR link, and turns itself off after 30 minutes.

## Known limits
- Attachments that were never on the phone (iCloud-only) show as "not in any backup".
- Windows: tested by script checks only so far (the Linux install was tested end to end).
- macOS isn't supported.
- Phone view needs the computer to accept connections on port 8738. If the phone can't load the page and a firewall is on, allow that port for your home network only.
- Voicemail dates and callers come from the phone's voicemail database; files without a database entry show the backup's file date.
- Message text on newer iOS is read from Apple's attributedBody format with a decoder that
  handles the common layout; a message it can't read shows without text.

## Uninstall
- Windows: `Uninstall-Windows.cmd` from the setup zip.
- Linux: `bash uninstall-linux.sh` from the setup zip.
Both remove the program, shortcuts and start-at-login. Your settings and archive folder are kept.

## Background music
No song is included in this repository. Choose one of your own in Settings > Background music;
`phonevault make-installer` can put it into a setup zip for your own computers.

## License
GPL-3.0-or-later (see `LICENSE`). PhoneVault uses pymobiledevice3 and pyiosbackup, which are GPL.
Font: Chakra Petch, SIL Open Font License (`phonevault/static/fonts/OFL-ChakraPetch.txt`).
