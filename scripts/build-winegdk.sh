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
PATCHES="$here/patches/winegdk"

if [ ! -d "$SRC/.git" ]; then
    git clone --depth 1 --recurse-submodules --shallow-submodules "$REPO" "$SRC"
fi
cd "$SRC"
# Apply the patch series as commits on a branch (idempotent: skipped if already applied).
if ! git rev-parse --verify -q xodus-launcher >/dev/null; then
    git checkout -q -b xodus-launcher
    GIT_COMMITTER_NAME=${GIT_COMMITTER_NAME:-xodus-launcher} GIT_COMMITTER_EMAIL=${GIT_COMMITTER_EMAIL:-build@localhost} \
        git am --3way "$PATCHES"/*.patch
else
    git checkout -q xodus-launcher
fi
[ -x configure ] || ./autogen.sh
mkdir -p build && cd build
../configure --prefix="$PREFIX" --enable-archs=i386,x86_64 --disable-tests --without-ffmpeg --without-opencl
make -j"$JOBS"
make install
echo "WineGDK installed to $PREFIX"
