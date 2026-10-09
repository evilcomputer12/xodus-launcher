#!/usr/bin/env bash
# Install the launcher for the current user (no root needed).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
install -Dm755 "$here/launcher/xodus_launcher.py" ~/.local/share/xodus-launcher/xodus_launcher.py
install -Dm755 "$here/bin/xodus-launcher" ~/.local/bin/xodus-launcher
install -Dm644 "$here/data/xodus-launcher.desktop" ~/.local/share/applications/xodus-launcher.desktop
install -Dm644 "$here/data/xodus.png" ~/.local/share/icons/hicolor/256x256/apps/xodus.png
sed -i "s|Exec=.*|Exec=$HOME/.local/bin/xodus-launcher|" ~/.local/share/applications/xodus-launcher.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Installed. Launch 'Xodus Launcher' from the app menu or run: xodus-launcher"
