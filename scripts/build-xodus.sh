#!/usr/bin/env bash
# Build and install xodus-cli / xodus-service from the AUR xodus-git PKGBUILD with our fixes (Arch-based distros).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
DIR=${DIR:-$HOME/.cache/xodus-launcher/xodus-pkg}
mkdir -p "$DIR" && cd "$DIR"
cp "$here/patches/xodus/PKGBUILD" "$here/patches/xodus/0001-fix-prefix-cache-flush.patch" .
mv -f 0001-fix-prefix-cache-flush.patch fix-prefix-cache-flush.patch
makepkg -sf --noconfirm
sudo pacman -U --noconfirm ./xodus-git-*.pkg.tar.zst
