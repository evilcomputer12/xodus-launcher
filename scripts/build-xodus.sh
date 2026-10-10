#!/usr/bin/env bash
# Build and install xodus-cli / xodus-service from the AUR xodus-git PKGBUILD with our fixes (Arch-based distros).
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
DIR=${DIR:-$HOME/.cache/xodus-launcher/xodus-pkg}
mkdir -p "$DIR" && cd "$DIR"
rm -rf "$DIR/src"   # a dirty checkout from the previous build makes git apply fail
cp "$here/patches/xodus/PKGBUILD" "$here"/patches/xodus/0*.patch .
makepkg -sf --noconfirm
sudo pacman -U --noconfirm ./xodus-git-*.pkg.tar.zst
