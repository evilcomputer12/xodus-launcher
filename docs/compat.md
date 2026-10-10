# Per-title compatibility

Every Game Pass title can need its own Wine setup: a particular build, its own prefix, DLL
overrides, environment, a different executable (EasyAntiCheat wrappers), winetricks verbs or
registry keys. Xodus Launcher keeps that per title in a database,
[`data/compat/titles.json`](../data/compat/titles.json) (schema:
[`data/compat/README.md`](../data/compat/README.md)), and applies the matching entry every
time it starts a game. Titles without an entry run with the defaults and show as *Untested*.

## What you see in the launcher

- A status badge on the poster (library: always; catalog: once the title has a known status)
  and a *Compatibility* line in the details panel with the notes and when/with what it was tested.
- A compatibility filter next to the genre filter in the Game Pass tab.
- **Compatibility report…** on installed games: asks how far the game got, writes
  `~/.cache/xodus-launcher/reports/<STOREID>-<date>.json` (system and GPU info, the Wine build
  and release, the launcher commit, the entry that was applied, the last 200 lines of the game
  log with anything credential-like removed) and opens a prefilled GitHub issue to attach it to.

The launcher refreshes the database from this repository's `main` branch once a day
(`~/.cache/xodus-launcher/compat/titles.json`); whichever copy is newer by its `updated` date
wins, so a fresh launcher never loses entries to a stale cache.

## The status scale

| Status | Meaning |
|---|---|
| untested | nobody reported anything |
| broken | does not start, or crashes before anything is usable |
| launches | starts and renders, but stops before the menu (licence screen, missing runtime piece…) |
| menu | reaches the main menu; gameplay not confirmed |
| playable | the game plays; some things missing or broken (see notes) |
| perfect | everything works, including Xbox Live features the title uses |

## Titles delivered through a third-party launcher

Ubisoft, EA, Blizzard, Rockstar and Riot titles have no Store package (`xodus-cli` reports
"no PC package"): the Xbox app hands them to the publisher's launcher. Their entries carry
`launcher` (a recipe id from `data/compat/launchers.json`) and `launcher_game_id` (what that
launcher starts the title with, `null` until known). The launcher then offers to install that
client into its own GE-Proton prefix and walks you through the sign-in; see
[launchers.md](launchers.md).

## Anti-cheat

An entry's `anticheat` object records what the title's anti-cheat means on Linux: `kind`
(`eac`, `battleye`, `ricochet`, `vanguard`, `none`, `unknown`), `linux_module` (the game ships
EAC's Linux module / the publisher enabled BattlEye's Proton support, so Steam's "Proton
EasyAntiCheat Runtime" or "Proton BattlEye Runtime" make online modes work), `bypass` (the real
executable started instead of the EAC bootstrapper for single-player, the Space Marine 2
pattern) and `online` (`supported`, `unsupported`, `unknown`). What the entry does not say is
read from the game files (`anticheat_for_game()` in `launcher/thirdparty.py`); the details panel
shows the resulting message. The single-player bypass stays the default: only an entry with
`online: "supported"`, run by a Proton-derived build with Steam's runtime installed, starts the
protected bootstrapper with `PROTON_EAC_RUNTIME` / `PROTON_BATTLEYE_RUNTIME` exported (WineGDK has
no such hooks, so under it they never are). Kernel drivers (Ricochet, Vanguard, EA Javelin) have
no Linux path at all.

## Overriding an entry locally

Create `~/.config/xodus-launcher/compat/<STOREID>.json` with just the fields you want to
change; it is merged over the database entry (objects key by key, lists and scalars replaced):

```json
{
  "runtime": {"wine": "wine-xodus"},
  "prefix": "per-game",
  "env": {"DXVK_HUD": "fps", "WINEDEBUG": "-all"},
  "dll_overrides": {"xinput1_3": "n,b"},
  "launch": {"executable": "bin\\game.exe"}
}
```

The Store ID is the `9XXXXXXXXXXX` product id (shown in the game folder's
`.xodus-launcher.json`, in `MicrosoftGame.config` as `StoreId`, and in the Store URL). A user
override's `runtime.wine` beats the Settings dialog; the details panel says when an override
is active. Registry keys and winetricks verbs are applied once per prefix and remembered in
`<prefix>/.xodus-compat-applied.json`.

## Adding or updating an entry

1. Play the game, then use **Compatibility report…**; the report file has everything the entry
   needs (and the issue template asks for it).
2. Add or edit the title in `data/compat/titles.json`: `status`, `notes`, `tested`
   (`date`, `launcher` short hash, `winegdk` release tag or `branch@commit`, `gpu`, `distro`),
   and only the configuration fields that differ from `defaults`.
3. Bump `updated`, then run `scripts/compat-seed.py --check` (schema validation) and
   `scripts/compat-seed.py --offline` (refreshes `catalog-index.json` and prints the coverage).
4. Send a pull request. Entries are reviewed like code: a tested status needs a `tested.date`.

`scripts/compat-seed.py` imports the launcher to read the catalog exactly the way the GUI does
(the per-market cache in `~/.cache/xodus-launcher`, refetched when older than 12 hours), so the
index always matches what the launcher shows.
