# Xodus Launcher

Play PC Game Pass games on Linux. Xodus Launcher is a desktop app that browses the Game Pass
catalog, signs in to your Xbox account, downloads games and runs them through Wine, built on
top of the [xodus](https://github.com/xodus-gaming/xodus) project and
[WineGDK](https://github.com/Weather-OS/WineGDK).

> Unofficial project, not affiliated with or endorsed by Microsoft or Xbox. You need an active
> PC Game Pass subscription; nothing here bypasses licensing.

## Status (October 2026)

- Catalog browsing, sign-in, downloads and library management work.
- Games launch through a patched WineGDK. Confirmed so far: **Balatro** (playable) and
  **Warhammer 40,000: Space Marine 2** (launches, passes the licence check to the menu; the
  EasyAntiCheat bootstrapper is bypassed, so online modes are unavailable).
- Games sign in with your real Xbox account through xodus-service: identity (gamertag, XUID)
  and signed Xbox Live tokens work, so achievements and other online calls can succeed.
  Cloud save sync is not implemented yet (saves are local).
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
# 2. WineGDK: the launcher's "Set up Wine" button downloads the prebuilt build from the
#    GitHub releases (no compiler needed). To build it yourself instead (~20 minutes):
#    sudo pacman -S --needed mingw-w64-gcc autoconf bison flex gnutls vulkan-headers
#    scripts/build-winegdk.sh
# 3. the launcher
sudo pacman -S --needed pyside6 python
./install.sh
```

Then start **Xodus Launcher** from the app menu, sign in, pick a game, Install, Play.

## Per-title compatibility

`data/compat/titles.json` maps Game Pass titles (by Store ID) to a status, notes and the Wine
configuration the launcher applies when it starts them: Wine build, shared or per-game prefix,
DXVK/VKD3D on or off, environment, DLL overrides, executable override (EasyAntiCheat wrappers),
winetricks verbs and registry keys. The launcher shows the status on the posters and in the
details panel, filters the catalog by it, refreshes the database from this repository daily,
and **Compatibility report…** writes a report (never containing tokens) and opens a prefilled
issue so an entry can be added. Local overrides go in `~/.config/xodus-launcher/compat/<STOREID>.json`.
See [docs/compat.md](docs/compat.md) and [data/compat/README.md](data/compat/README.md).

## Patches carried here (to be sent upstream)

- `patches/xodus/`: a git series on xodus main: the streaming-download crash fix, and
  `TokenAndSignatureRequest` / `UserInfoRequest` in xodus-service (real Xbox Live tokens, request
  signing and identity for games).
- `patches/winegdk/`: a git series on top of WineGDK master (see `BASE-COMMIT`):
  memfd executable loading (from the xodus wine fork), `RegisterAppStateChangeNotification`
  returning success, XAsync buffer-overrun and tracing fixes, `IXSystemImpl2-5` + hex TitleId,
  an XUser that signs in through xodus-service (offline fallback), a force-close watchdog, and the remaining GDK runtime classes (XGame, XGameSave,
  XPersistentLocalStorage implemented; XStore, XPackage, XGameUi and 12 more ported as stubs).

## License

GPL-3.0-or-later for the code in this repository. Wine and WineGDK are LGPL-2.1; xodus is GPL-3.0.
The Xodus icon comes from the xodus project.
