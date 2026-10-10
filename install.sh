#!/usr/bin/env bash
# Install the launcher for the current user (no root needed).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
install -Dm755 "$here/launcher/xodus_launcher.py" ~/.local/share/xodus-launcher/xodus_launcher.py
install -Dm755 "$here/bin/xodus-launcher" ~/.local/bin/xodus-launcher
install -Dm644 "$here/data/compat/titles.json" ~/.local/share/xodus-launcher/compat/titles.json
# third-party launcher support (Ubisoft Connect, EA app, ...): imported next to the launcher
if [ -f "$here/launcher/thirdparty.py" ]; then
    install -Dm644 "$here/launcher/thirdparty.py" ~/.local/share/xodus-launcher/thirdparty.py
fi
if [ -f "$here/data/compat/launchers.json" ]; then
    install -Dm644 "$here/data/compat/launchers.json" ~/.local/share/xodus-launcher/compat/launchers.json
fi
install -Dm644 "$here/data/xodus-launcher.desktop" ~/.local/share/applications/xodus-launcher.desktop
install -Dm644 "$here/data/xodus.png" ~/.local/share/icons/hicolor/256x256/apps/xodus.png
sed -i "s|Exec=.*|Exec=$HOME/.local/bin/xodus-launcher|" ~/.local/share/applications/xodus-launcher.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Installed. Launch 'Xodus Launcher' from the app menu or run: xodus-launcher"
