#!/usr/bin/env bash
# PhoneVault installer for Linux (CachyOS/Arch, Debian/Ubuntu, Fedora, SteamOS desktop mode).
# Run it from any shell:  bash install-linux.sh
# Installs into ~/.venvs/phonevault. Settings and your archive are never touched.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/app"
VENV="$HOME/.venvs/phonevault"
LOG="$HOME/.cache/phonevault-install.log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31mXX %s\033[0m\n' "$*"; printf 'Full log: %s\n' "$LOG"; exit 1; }

step "PhoneVault installer ($(date))"
[ -f "$APP/pyproject.toml" ] || fail "Can't find the app folder next to this script. Unzip the whole PhoneVault folder first."

# ---- 1. system packages: python, venv, usbmuxd (iPhone), adb + USB rules (Android), ffmpeg + imagemagick (media) ----
PM=""
command -v pacman  >/dev/null && PM=pacman
command -v apt-get >/dev/null && PM=apt
command -v dnf     >/dev/null && PM=dnf
need=()
have_py() { for c in python3.13 python3.12 python3.11 python3; do
              if command -v "$c" >/dev/null && "$c" -c 'import sys,venv,ensurepip; sys.exit(sys.version_info < (3,11))' 2>/dev/null; then
                PY="$(command -v "$c")"; return 0; fi; done; return 1; }
PY=""
have_py || need+=(python)
command -v usbmuxd >/dev/null || [ -e /usr/sbin/usbmuxd ] || [ -e /usr/bin/usbmuxd ] || need+=(usbmuxd)
command -v ffmpeg  >/dev/null || need+=(ffmpeg)
command -v magick  >/dev/null || command -v convert >/dev/null || need+=(imagemagick)
command -v adb     >/dev/null || need+=(adb)
ls /usr/lib/udev/rules.d/*android*.rules /etc/udev/rules.d/*android*.rules >/dev/null 2>&1 || need+=(android-udev)

if [ ${#need[@]} -gt 0 ]; then
  step "Installing system packages: ${need[*]} (asks for your password)"
  pk=()
  for n in "${need[@]}"; do
    case "$PM:$n" in
      pacman:python) pk+=(python) ;;           apt:python) pk+=(python3 python3-venv) ;;  dnf:python) pk+=(python3) ;;
      *:usbmuxd) pk+=(usbmuxd) ;;              *:ffmpeg) pk+=(ffmpeg) ;;
      *:imagemagick) pk+=(imagemagick) ;;
      pacman:adb) pk+=(android-tools) ;;        apt:adb) pk+=(adb) ;;                       dnf:adb) pk+=(android-tools) ;;
      pacman:android-udev) pk+=(android-udev) ;;
    esac
  done
  ok=1
  case "$PM" in
    pacman) sudo pacman -S --needed --noconfirm "${pk[@]}" || ok=0 ;;
    apt)    sudo apt-get update && sudo apt-get install -y "${pk[@]}" || ok=0 ;;
    dnf)    sudo dnf install -y "${pk[@]}" || ok=0 ;;
    *)      ok=0 ;;
  esac
  # Android USB permission rules (package name differs between Debian/Ubuntu releases)
  if [[ " ${need[*]} " == *" android-udev "* && "$PM" == apt ]]; then
    sudo apt-get install -y android-udev-rules 2>/dev/null || sudo apt-get install -y android-sdk-platform-tools-common || true
  fi
  if [[ " ${need[*]} " == *" android-udev "* ]]; then
    sudo udevadm control --reload-rules 2>/dev/null || true
    sudo udevadm trigger 2>/dev/null || true
    getent group adbusers >/dev/null && sudo gpasswd -a "$USER" adbusers >/dev/null 2>&1 || true
    echo "Android: unplug and replug the phone once so the new USB rules apply."
  fi
  if [ $ok = 0 ]; then
    warn "Couldn't install: ${pk[*]:-${need[*]}}"
    warn "Steam Deck: system packages need 'sudo steamos-readonly disable' first, or skip them."
    warn "Without usbmuxd the iPhone can't connect; without adb Android phones can't connect;"
    warn "without ffmpeg/imagemagick some media won't preview."
  fi
fi
have_py || fail "Python 3.11 or newer (with venv) is needed. Install it, then run this again."
printf 'Python: %s (%s)\n' "$PY" "$("$PY" -V)"

# ---- 2. stop a running copy so files can be replaced ----
step "Stopping PhoneVault if it's running"
systemctl --user stop phonevault 2>/dev/null || true
pkill -f "phonevault run" 2>/dev/null || true

# ---- 3. app ----
step "Installing PhoneVault into $VENV (downloads about 150 MB the first time)"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV" || fail "Couldn't create $VENV"
"$VENV/bin/python" -m pip install --upgrade --quiet pip || warn "pip self-update failed (continuing)"
"$VENV/bin/python" -m pip install --upgrade --force-reinstall --no-deps --quiet "$APP" || fail "Installing the app failed"
"$VENV/bin/python" -m pip install --upgrade --quiet "$APP" || fail "Installing the app's add-ons failed (internet needed)"
"$VENV/bin/python" -c "import phonevault, pymobiledevice3; print('PhoneVault', phonevault.__version__)" || fail "Install check failed"

# ---- 4. shortcuts + open ----
step "Adding PhoneVault to the app menu and the Desktop"
"$VENV/bin/python" -m phonevault install-desktop || warn "Couldn't add the menu/Desktop icons"
if systemctl --user is-enabled phonevault >/dev/null 2>&1; then
  "$VENV/bin/python" -m phonevault autostart on >/dev/null && systemctl --user start phonevault || true
fi

step "Opening PhoneVault"
"$VENV/bin/python" -m phonevault gui || warn "Open it from the app menu instead"
printf '\nDone. First time: the app asks where to store backups.\nUninstall: bash %s/uninstall-linux.sh\nLog: %s\n' "$HERE" "$LOG"
