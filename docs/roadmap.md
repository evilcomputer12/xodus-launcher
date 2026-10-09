# Roadmap

The long-term idea is a Windows-apps-on-Linux layer: Wine plus the pieces modern Windows
apps expect (the Xbox Game Development Kit runtime, Windows Store packaging, WinRT APIs),
open source end to end. We get there in milestones that each ship something usable.

## Milestone 1: Game Pass games launcher (in progress)

- [x] Catalog, sign-in, download, library, Play, Steam shortcuts (PySide6 GUI)
- [x] WineGDK build with all 24 GDK runtime classes present (15 still stubs)
- [x] First confirmed title: Balatro
- [ ] Real Xbox Live sign-in for games (`XUserGetTokenAndSignatureAsync` via xodus-service):
      achievements, cloud saves, multiplayer
- [ ] `XStore` licence answers and `XPackage` package info so licence-checking games start
- [ ] Per-game compatibility reports; a tested list of titles
- [ ] One-click setup: build/download WineGDK from the launcher, no terminal needed
- [ ] Packages: AUR, then Flatpak
- [ ] Send the WineGDK and xodus changes upstream

## Milestone 2: the real Xbox app

Run Microsoft's Xbox PC app (React + WebView2 host over the `Microsoft.GamePlatform.Services`
WinRT API) under Wine. Needs WebView2 in Wine and an open implementation of the
`GamePlatform.Services` classes backed by xodus-service. Microsoft's app is never shipped; the
launcher fetches it from Microsoft's servers on the user's machine if they opt in.

## Milestone 3: Windows Store apps in general

MSIX/AppX packaging, app identity, and the WinRT surface (`Windows.ApplicationModel.*`,
`Windows.Storage.*`, `Windows.UI.Xaml`) on top of Wine, so packaged Windows apps run on Linux
the way WSL runs Linux on Windows, in reverse. The reverse-engineering notes and API dumps
from milestones 1 and 2 feed into this.

## Reference material

- `docs/gdk-runtime-status.md`: what the GDK runtime provides and what we cover.
- WinRT metadata from the Xbox app and Gaming Services (decompiled with ILSpy) documents the
  service contracts; see the project wiki once published.
