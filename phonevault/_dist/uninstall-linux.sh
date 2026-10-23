#!/usr/bin/env bash
# Removes the PhoneVault program, shortcuts and start-at-login.
# KEEPS your settings (~/.config/phonevault) and your archive folder (backups, messages, photos).
set -uo pipefail
VENV="$HOME/.venvs/phonevault"
if [ -x "$VENV/bin/python" ]; then
  "$VENV/bin/python" -m phonevault uninstall || true
fi
systemctl --user stop phonevault 2>/dev/null || true
pkill -f "phonevault run" 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/phonevault.service"
systemctl --user daemon-reload 2>/dev/null || true
if [ -d "$VENV" ]; then
  rm -rf "$VENV" && echo "Removed the program: $VENV"
fi
echo "Kept: ~/.config/phonevault (settings) and your archive folder."
echo "To reinstall later: bash install-linux.sh"
