#!/usr/bin/env bash
# Build Weather-OS/WineGDK with the Xodus Launcher patches and install it for the launcher.
#   SRC    - source checkout (default ~/.cache/xodus-launcher/winegdk-src)
#   PREFIX - install prefix   (default ~/.local/share/xodus-launcher/wine-gdk)
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
SRC=${SRC:-$HOME/.cache/xodus-launcher/winegdk-src}
PREFIX=${PREFIX:-$HOME/.local/share/xodus-launcher/wine-gdk}
JOBS=${JOBS:-$(nproc)}
REPO=https://github.com/Weather-OS/WineGDK
PATCH="$here/patches/winegdk/0001-xodus-launcher-fixes.patch"

if [ ! -d "$SRC/.git" ]; then
    git clone --depth 1 --recurse-submodules --shallow-submodules "$REPO" "$SRC"
fi
cd "$SRC"
if git apply --check "$PATCH" 2>/dev/null; then
    git apply "$PATCH"
else
    echo "patch already applied or does not apply cleanly; continuing with the tree as-is" >&2
fi
[ -x configure ] || ./autogen.sh
mkdir -p build && cd build
../configure --prefix="$PREFIX" --enable-archs=i386,x86_64 --disable-tests --without-ffmpeg --without-opencl
make -j"$JOBS"
make install
echo "WineGDK installed to $PREFIX"
