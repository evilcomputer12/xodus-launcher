# Xodus Launcher

Play PC Game Pass games on Linux. Xodus Launcher is a desktop app that browses the Game Pass
catalog, signs in to your Xbox account, downloads games and runs them through Wine, built on
top of the [xodus](https://github.com/xodus-gaming/xodus) project and
[WineGDK](https://github.com/Weather-OS/WineGDK).

> Unofficial project, not affiliated with or endorsed by Microsoft or Xbox. You need an active
> PC Game Pass subscription; nothing here bypasses licensing.

## Status (October 2026)

- Catalog browsing, sign-in, downloads and library management work.
- Games launch through a patched WineGDK. **Balatro** is the first title confirmed running.
- Games run with an *offline* local player: achievements, cloud saves and multiplayer are not
  available yet (real Xbox Live sign-in is in progress).
- Each additional game may need more of the Xbox GDK runtime implemented; see `docs/`.

## How it works

| Piece | Role |
|---|---|
| `launcher/xodus_launcher.py` | Qt (PySide6) GUI: catalog, downloads, library, Play, Steam shortcuts |
| [xodus](https://github.com/xodus-gaming/xodus) (`xodus-cli`, `xodus-service`) | Xbox sign-in, licence, package download and decryption |
| [WineGDK](https://github.com/Weather-OS/WineGDK) + `patches/winegdk/` | Wine with an open-source `xgameruntime.dll` (Xbox GDK runtime) |
| `scripts/` | Reproducible builds of the two components above |

No Microsoft binaries are shipped or required. Where a feature can optionally use a Microsoft
component, the launcher downloads it on your machine from Microsoft's servers and tells you.

## Install (Arch / CachyOS, other distros soon)

```sh
# 1. xodus-cli + xodus-service (AUR package with our fixes)
scripts/build-xodus.sh
# 2. WineGDK with our patches (takes ~20 minutes)
sudo pacman -S --needed mingw-w64-gcc autoconf bison flex gnutls vulkan-headers
scripts/build-winegdk.sh
# 3. the launcher
sudo pacman -S --needed pyside6 python
./install.sh
```

Then start **Xodus Launcher** from the app menu, sign in, pick a game, Install, Play.

## Patches carried here (to be sent upstream)

- `patches/xodus/0001-fix-prefix-cache-flush.patch`: streaming downloads crashed with
  "cache ended before cached_len" (flush the cache file before reading it back).
- `patches/winegdk/`: a git series on top of WineGDK master (see `BASE-COMMIT`):
  memfd executable loading (from the xodus wine fork), `RegisterAppStateChangeNotification`
  returning success, XAsync buffer-overrun and tracing fixes, `IXSystemImpl2-5` + hex TitleId,
  an offline local XUser, and the remaining GDK runtime classes (XGame, XGameSave,
  XPersistentLocalStorage implemented; XStore, XPackage, XGameUi and 12 more ported as stubs).

## License

GPL-3.0-or-later for the code in this repository. Wine and WineGDK are LGPL-2.1; xodus is GPL-3.0.
The Xodus icon comes from the xodus project.
