#!/bin/sh
# fanmon: install the GUI, desktop entry and icon for the current user.
set -eu
SRC="$(dirname "$(readlink -f "$0")")"
BIN="${HOME}/.local/bin"
APPS="${HOME}/.local/share/applications"
ICONS="${HOME}/.local/share/icons/hicolor/scalable/apps"

mkdir -p "$BIN" "$APPS" "$ICONS"
sed "s|@FANMON_SRC@|${SRC}|" "$SRC/fanmon-gui.py" > "$BIN/fanmon"
chmod 755 "$BIN/fanmon"
install -m644 "$SRC/fanmon.svg" "$ICONS/fanmon.svg"
sed "s|^Exec=.*|Exec=${BIN}/fanmon|" "$SRC/fanmon.desktop" > "$APPS/fanmon.desktop"
chmod 644 "$APPS/fanmon.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS" || true
fi
echo "fanmon GUI installed: run '${BIN}/fanmon' or open 'Fan Monitor' from the menu."
