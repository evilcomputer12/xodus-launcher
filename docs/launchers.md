# Third-party launchers

Some PC Game Pass titles never arrive as a Store package. For Ubisoft, EA (EA Play), Blizzard
(Battle.net), Rockstar and Riot titles the Xbox app only installs a stub that hands the game to
the publisher's own launcher, which downloads, licenses and starts it. `xodus-cli` reports
"no PC package" for them. Xodus Launcher therefore installs those launchers itself and lets
them do their job, inside Wine.

Everything is driven by [`data/compat/launchers.json`](../data/compat/launchers.json) (schema
in [`data/compat/README.md`](../data/compat/README.md)) and implemented in
[`launcher/thirdparty.py`](../launcher/thirdparty.py). No third-party binary is shipped: the
installer is downloaded from the publisher's official URL on your machine when you click
Install, GE-Proton from its GitHub releases, and the anti-cheat runtimes come from your own
Steam installation.

## How it works

1. **GE-Proton.** The launchers are Windows programs that need a current Wine with DXVK,
   CEF/Chromium-friendly patches and the writecopy / ntsync fixes that Ubisoft Connect, the EA
   app and Battle.net broke without. `ensure_ge_proton()` fetches the latest
   [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom) release through the GitHub
   API, verifies the tarball against the release's `.sha512sum` (and GitHub's own SHA-256
   digest), extracts it with Python's `tarfile` "data" filter (no absolute paths, no `..`, no
   links leaving the tree) into `~/.local/share/xodus-launcher/ge-proton/<tag>/` and records the
   tag in `.current`. A GE-Proton that Steam or umu already installed is linked instead of
   downloaded (`ge_proton_reuse_existing` in `config.json`, on by default). Each recipe names
   the oldest tag it is known to work with (`runtime.min_version`).
   A recipe can also name Proton builds to prefer when Steam has them (`runtime.prefer_tools`,
   e.g. `Proton-CachyOS` for Ubisoft Connect while GE-Proton lags behind a client update).
2. **One prefix per launcher.** `~/.local/share/xodus-launcher/prefix-<id>` is created with
   `wineboot` from GE-Proton's `files/bin/wine`, gets GE-Proton's DXVK and VKD3D-Proton DLLs,
   the recipe's `prefix.files` (settings the client must find before its first start), its
   registry entries and winetricks verbs (system `winetricks`, else the copy GE-Proton bundles).
   The games a launcher installs live in that prefix too, so it is never thrown away lightly
   (Rockstar locks accounts that sign in from "new devices" too often).
3. **The installer** runs with the recipe's silent switches. Battle.net Setup has no silent
   mode: its window appears and you click Install. The Rockstar installer never exits: the
   launcher waits for `Launcher.exe` to appear, then stops the prefix. Exit codes other than 0
   are accepted only where the recipe says so (the EA app returns 768 on success). Every run
   is logged to `~/.cache/xodus-launcher/logs/launcher-<id>-install.log`.
4. **Sign-in** happens in the launcher's own window (`ThirdPartyLauncher.open()`): the
   publisher's login, then the Xbox account link in a browser on the publisher's site (see
   below). The guide shows the recipe's `signin.steps` (numbered, scrollable, links open in the
   browser) and its `signin.summary`.
5. **Games** are found by parsing what the launcher wrote: `system.reg` / `user.reg` keys
   (Ubisoft, Rockstar), `__Installer/installerdata.xml` (EA), the Agent's `product.db`
   protobuf (Battle.net), `RiotClientInstalls.json` and the product metadata YAML (Riot). They
   are started through the launcher (`uplay://launch/<id>/0`,
   `origin2://game/launch?offerIds=<id>`, `--exec=launch <code>`,
   `-launchTitleInFolder <dir>`, `--launch-product=<id>`), never directly, because the
   launcher is the DRM.
6. **Stopping.** All of these clients keep background processes alive after their window
   closes (EA background service, Battle.net Agent, Riot tray icon, Ubisoft's web core);
   `stop()` runs `wineserver -k` for the prefix.

The Wine environment (`ThirdPartyLauncher.env()`) is what GE-Proton's `proton` script would
set: GE's `files/bin` and bundled libraries on `PATH` / `LD_LIBRARY_PATH`, `WINEDLLPATH`,
esync/fsync/ntsync on, `WINEDEBUG=-all`, DXVK/VKD3D overrides, no `winemenubuilder` (installers
would otherwise litter the app menu), plus the recipe's `prefix.env` and `dll_overrides`, then
Settings' `extra_env`. Steam's `SteamAppId` / `SteamGameId` are removed on purpose: the
Rockstar launcher takes them as "this is a Steam copy".

## Per-launcher sign-in and Game Pass linking

The entitlement always comes from the Microsoft account that holds the PC Game Pass or
Game Pass Ultimate subscription; each publisher attaches it to its own account differently.

### Ubisoft Connect

1. Open Ubisoft Connect and sign in with your Ubisoft account (the first start checks for
   patches and needs network access).
2. In a browser, open <https://gamepass.ubisoft.com/>, sign in with the Microsoft account that
   has the subscription, grant Ubisoft access, sign in to the Ubisoft account and click
   **Confirm and activate**. This links the two accounts permanently (a Ubisoft account can
   only ever be linked to that one Xbox account).
3. Quit and reopen Ubisoft Connect: the whole current Game Pass Ubisoft catalogue appears in
   **Games**, downloads happen inside Ubisoft Connect, and the Xbox app is never involved. It
   can take a few hours to show up; after a lapsed subscription you re-activate on the web.

### EA app

1. Open the EA app and sign in with your EA Account (complete the e-mail / app verification
   code if it asks).
2. Link the Microsoft account on <https://www.ea.com/myaccount> (Connections), or let the EA
   app do it when it asks. If EA Play was ever used on an Xbox console the link already exists.
3. Restart the EA app: the EA Play catalogue that Game Pass grants becomes installable in the
   app. A title that still shows a price has not received the entitlement (wrong account, not
   linked, or EA Play Pro-only).
4. The EA app must stay running while a game runs; its in-game overlay (Settings >
   Application) is best disabled under Wine.

### Battle.net

1. Click **Install** in the Battle.net Setup window (there is no silent mode); it downloads the
   client from Blizzard's CDN and opens the login window. Sign in with your Battle.net account.
2. In a browser, open <https://account.battle.net/connections>, choose **Xbox** and sign in
   with the Microsoft account that has the subscription. Linking has cooldowns, so pick the
   right Battle.net account the first time.
3. Restart Battle.net: the Game Pass titles (Diablo IV, StarCraft II, Warcraft Remastered,
   the Xbox titles sold on Battle.net such as Avowed and DOOM: The Dark Ages, ...) become
   installable at no charge from their game pages. Call of Duty is not part of this and would
   not run on Linux anyway (Ricochet kernel driver).

If the login window shows only a spinning icon, hardware acceleration must stay off
(`Battle.net.config`, pre-seeded by the recipe) and `drive_c/ProgramData/Battle.net` can be
deleted. Battle.net and its Agent are 32-bit: the host needs 32-bit Vulkan/Mesa libraries.

### Rockstar Games Launcher

1. The installer never exits on its own; Xodus waits for `Launcher.exe` and stops the prefix.
2. Open the launcher: the first start self-updates and installs Social Club (1-3 minutes, may
   crash once; open it again). Sign in in the **Rockstar Games - Sign In** window, tick
   **Remember me**, enter the e-mail code. Keep this prefix: every rebuilt prefix is a "new
   device" and repeated codes lock the account.
3. Link the Xbox account at <https://www.rockstargames.com/account/connections> (**Xbox
   network**) with the Microsoft account that has the subscription.

Game Pass Rockstar titles do **not** appear in the launcher's library: the launcher only lists
Rockstar Store purchases. A Game Pass copy is a full Store package (GTA V Enhanced is
9NXFX68ZNKK0) that the Xbox app starts; the game then starts the launcher, which checks the
entitlement with an Xbox Live token obtained from the GDK runtime and spawns the game through
`RockstarXboxHelper`. That flow needs the game and the launcher in one prefix with a working
GDK runtime (WineGDK), which this module does not provide yet; see the open questions. What
works today: installing and signing in to the launcher, and games bought on the Rockstar
Store. GTA Online is unavailable on Linux (BattlEye without Proton opt-in); story mode runs
with BattlEye unticked in the launcher's Settings.

### Riot Client

1. Open the Riot Client and sign in with your Riot Account.
2. In a browser, open <https://account.riotgames.com/xbox-game-pass>, go to the Xbox sign-in
   page, sign in with the Microsoft account that has the subscription, allow Riot access, sign
   in with the Riot Account. Benefits unlock at the next game login (up to 24 h).

Nothing is "unlocked" as a game licence: every Riot title is free-to-play and Game Pass only
adds in-game perks. League of Legends, Teamfight Tactics, VALORANT and 2XKO require the
Vanguard kernel driver and **cannot run under Wine**; Legends of Runeterra (the one title
without Vanguard) is the only candidate, untested. The recipe therefore installs the client
with the Legends of Runeterra bootstrapper (Americas; the Europe / Asia variants are in its
notes); the League of Legends bootstrapper from Riot's download page is kept under
`installer.alternatives` for a user override.

## Anti-cheat: what is real

Proton's Wine (and GE-Proton's) can replace a game's Windows anti-cheat client with Valve's
Linux builds when two environment variables point at them: `PROTON_EAC_RUNTIME` (the Steam
tool app 1826330 "Proton EasyAntiCheat Runtime", `steamapps/common/Proton EasyAntiCheat
Runtime/`, holding `v2/lib64`) and `PROTON_BATTLEYE_RUNTIME` (app 1161040 "Proton BattlEye
Runtime", holding `v1/lib64/wine` and `BEClient_x64.so`). Under Steam the Proton client shim
sets them; outside Steam the launcher has to. `anticheat_runtimes()` finds them in every Steam
library (`libraryfolders.vdf`), in a `PROTON_*_RUNTIME` already in the environment, or in the
copies Heroic / Lutris keep; `anticheat_env()` exports them when they can help. If you do not
have them, install them in Steam: `steam://install/1826330` and `steam://install/1161040`
(both are hidden tool apps that any EAC/BattlEye Steam game pulls in automatically). Xodus
deliberately does not download the Lutris mirror of these runtimes: it is a third-party
redistribution of Valve's files.

Having the runtime is not enough:

- **The Wine build must be Proton-derived.** The `PROTON_*_RUNTIME` loaders live in Proton's
  `ntdll`; WineGDK (the build that runs Game Pass titles) has none of them, so under it the
  runtime is never exported and the single-player bypass stays the default
  (`wine_has_anticheat_hooks()` checks the build's `ntdll.so`).
- **Easy Anti-Cheat** also needs the game to ship its Linux module
  (`EasyAntiCheat/easyanticheat_x64.so`) and the publisher to have enabled Wine/Proton on the
  EAC backend. Game Pass builds often ship without the module (Space Marine 2 does): then the
  EOS bootstrapper `start_protected_game.exe` can never succeed, Xodus starts the real
  executable named in `EasyAntiCheat/Settings.json` (or the entry's `anticheat.bypass`)
  instead, single-player works and online modes are labelled unavailable. A module found by
  the file scan alone makes the status "unknown": the bypass is still used and only a title
  entry with `anticheat.online: "supported"` (run by a Proton build) starts the bootstrapper.
- **BattlEye** ships no Linux file with the game; the publisher's opt-in is invisible from the
  files, so the compatibility entry (`anticheat.linux_module`) or a test launch decides. The
  runtime is passed along when the entry does not say "unsupported"; Rockstar never opted in
  (GTA Online), Ubisoft did for some titles (Ghost Recon Breakpoint, Watch Dogs: Legion) and
  not for others (Rainbow Six Siege).
- **Kernel drivers** (Riot Vanguard, Activision's Ricochet, EA Javelin / "EA anticheat" in
  Battlefield and EA SPORTS FC) have no Linux path: those games do not start at all, and
  there is nothing to bypass.
- **Warden** (Blizzard) and server-side systems (FairFight, PunkBuster remnants) run under
  Wine without any help.

`anticheat_for_game(game_dir, compat_entry)` combines the game files and the title entry
into `{kind, linux_module, runtime, bypass, online, message}`, which the details panel shows.

## Adding or changing a recipe

1. Research first, with sources: the official installer URL (from the publisher's download
   page, https only), its silent switches, exit codes and whether it exits at all; the
   oldest GE-Proton that runs the client; what Lutris / Heroic / Bottles / umu-protonfixes do to
   the prefix (winetricks, registry, settings files, environment); where the client records
   installed games; the sign-in and Xbox-linking flow; which titles carry which anti-cheat.
2. Add the recipe to `data/compat/launchers.json` with every field from the schema. Do not
   pin `installer.sha256` for an unversioned URL (log the hash in `notes` instead; pin the
   versioned file a rolling link redirects to, as the Battle.net recipe does); write
   `signin.steps` as instructions for the user (research goes in `signin.notes`); set
   `installer.wait` to `exe` only when the installer really never exits; put settings files
   the client needs before its first start in `prefix.files`; keep `known_issues`,
   `sources` and `confidence` honest.
3. If the client records its games in a format none of the scanners understand, add a
   `_scan_<name>` function to `thirdparty.py`, register it in `_SCANNERS` and name it in
   `installed_games.scanner`; registry-based launchers can often use the `generic` scanner.
4. Add the titles that go through the launcher to `titles.json` with `launcher` and
   `launcher_game_id`, bump `updated`, run `scripts/compat-seed.py --check`.
5. Verify on a real machine: install, sign in, install one Game Pass title inside the
   launcher, start it from Xodus, stop it, and note the GE-Proton tag in `runtime.min_version`.

Per-user tweaks go in `~/.config/xodus-launcher/launchers/<id>.json` (merged over the recipe:
objects key by key, lists and scalars replaced), for example the rolling Battle.net link from
`installer.alternatives` with `"sha256": null` once the pinned setup is gone, or the League of
Legends bootstrapper for the Riot Client. Overrides are validated like the recipes
(https URLs, prefix-relative paths); `launchers.json` itself is refreshed from `main` daily
together with `titles.json`.

## Open questions

- Rockstar's Game Pass hand-off (`RockstarXboxHelper`, Xbox Live token "from the GDK") needs
  the game's Store package and the launcher in one prefix with WineGDK; nothing here attempts
  that yet, and the GTA V products are missing from the catalog index.
- Whether the Game Pass / launcher builds of EAC titles (The Division 2, Sea of Thieves on
  Battle.net, STAR WARS: Squadrons) ship the Linux EAC module like their Steam builds.
- Legends of Runeterra's current state under Wine (reports range from playable to crashing at
  launch).
- The GE-Proton binary is run directly, outside the Steam Linux Runtime container that
  GE-Proton officially supports; the launchers' host-library needs are those of any Wine
  (32-bit Vulkan and Mesa for Battle.net, 32-bit libpulse for audio in the 32-bit clients:
  Arch `lib32-libpulse`, Debian/Ubuntu `libpulse0:i386`, Fedora `pulseaudio-libs.i686`; Xodus
  checks `ldconfig -p` and logs a warning when it is missing).
- Ubisoft Connect 174.1.13380 (built 2026-10-01) installs fine under GE-Proton11-7 (the newest
  GE release on 2026-10-10) but never shows its window: the client's UI thread overflows its
  stack right after Wine's `windows.ui` `IUISettings2::get_TextScaleFactor` stub (with or
  without an audio driver), upc.exe and UplayWebCore.exe keep running headless. The same
  prefix shows the login window under Proton-CachyOS 11.0-20261005 (14 s) and under this
  project's own proton-xodus-bleeding-edge 11.0-20260803 (52 s) through umu-run, so Valve's
  current Wine tree is fine and GE-Proton11-7 is behind; until a GE-Proton release catches up
  the Ubisoft recipe is "installs, client does not start" when run with GE-Proton.

## Runner: umu-launcher

When `umu-run` is installed the launchers (installer, client, games) run through it, inside the
Steam Linux Runtime container, exactly like Lutris and Heroic start them; Proton's 32-bit side
needs libraries most hosts lack otherwise (Ubisoft Connect's UI thread overflowed its stack with
a bare `files/bin/wine`). Set `"thirdparty_runner": "wine"` in config.json to run the Proton
build directly instead. Known state on 2026-10-10: Ubisoft Connect 174.1 runs headless on every Wine 11 based build
(GE-Proton 11, Proton-CachyOS 11, plain Wine 11: its UI thread overflows its stack inside Wine's
32-bit ucrtbase after a burst of longjmp unwinds) and works on Wine 10 based builds, so its recipe
prefers Proton 10 builds (`runtime.prefer_tools`) and downloads `UMU-Proton-10.0-4`
(`runtime.release`, sha512-verified) when none is installed.
