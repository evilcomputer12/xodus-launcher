#!/usr/bin/env python3
"""Xodus Launcher - a desktop front-end for xodus-cli (PC Game Pass on Linux).

Browse the PC Game Pass catalog, sign in, download games with
`xodus-cli streaming`, launch them with `xodus-cli run` through the xodus
Proton/Wine build, and add them to Steam.

Usage:
    xodus-launcher                 open the GUI
    xodus-launcher --play <ID>     launch an installed game (used by Steam shortcuts)
"""

import concurrent.futures
import fcntl
import glob
import hashlib
import html
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import termios
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

HOME = Path.home()
APP_NAME = "Xodus Launcher"
DATA_DIR = HOME / ".local/share/xodus-launcher"
CACHE_DIR = HOME / ".cache/xodus-launcher"
CONFIG_FILE = HOME / ".config/xodus-launcher/config.json"
LOG_DIR = CACHE_DIR / "logs"
META_NAME = ".xodus-launcher.json"
# installed either per-user (install.sh) or system-wide (package); find whichever exists
ICON_PATH = next((p for p in (HOME / ".local/share/icons/hicolor/256x256/apps/xodus.png",
                              Path("/usr/share/icons/hicolor/256x256/apps/xodus.png"))
                  if p.exists()), HOME / ".local/share/icons/hicolor/256x256/apps/xodus.png")
SELF_CMD = shutil.which("xodus-launcher") or str(HOME / ".local/bin/xodus-launcher")

ALL_PC_GAMES_SIGL = "fdd9e2a7-0fee-49f6-ad69-4354098401ff"
# EA Play (included with PC Game Pass) is a separate collection, mixed console/PC; PC titles are
# delivered through the EA app, so they have no Windows.Desktop package (console-only ones have
# Windows.Xbox packages and are dropped)
EA_PLAY_SIGLS = ("1d33fbb9-b895-4732-a8ca-a55c8b99fa2c", "b8900d09-a491-44cc-916e-32b5acae621b")
CATALOG_MAX_AGE = 12 * 3600
UA = "xodus-launcher/1.0"

# per-title compatibility database: bundled with the launcher, refreshed from the repository
COMPAT_URL = ("https://raw.githubusercontent.com/evilcomputer12/xodus-launcher/main/"
              "data/compat/titles.json")
COMPAT_MAX_AGE = 24 * 3600
COMPAT_CACHE = CACHE_DIR / "compat/titles.json"
COMPAT_USER_DIR = HOME / ".config/xodus-launcher/compat"
COMPAT_STATUSES = ("untested", "broken", "launches", "menu", "playable", "perfect")
STATUS_LABELS = {"untested": "Untested", "broken": "Broken", "launches": "Launches",
                 "menu": "Menu", "playable": "Playable", "perfect": "Perfect"}
STATUS_COLORS = {"untested": "#6c757d", "broken": "#c0392b", "launches": "#d35400",
                 "menu": "#b7950b", "playable": "#1e8449", "perfect": "#117a65"}
REPORT_ISSUES_URL = "https://github.com/evilcomputer12/xodus-launcher/issues/new"
_SELF = Path(__file__).resolve()
# next to data/ in a checkout, or next to the script where install.sh / the package copy it
BUNDLED_COMPAT = next((p for p in (_SELF.parent.parent / "data/compat/titles.json",
                                   _SELF.parent / "compat/titles.json",
                                   Path("/usr/share/xodus-launcher/compat/titles.json"))
                       if p.exists()), None)

DEFAULT_CONFIG = {
    "games_dir": str(HOME / "Games/GamePass"),
    "market": "TR",
    "language": "en-us",
    "xodus_cli": "xodus-cli",
    "wine": "",
    "prefix": str(DATA_DIR / "prefix-gdk"),
    "parallel": 4,
    "extra_env": {},
    "anticheat_warned": [],  # Store IDs whose "online modes unavailable" notice was shown once
    # third-party launchers: link a GE-Proton that Steam / umu already installed instead of
    # downloading one (launcher/thirdparty.py ensure_ge_proton)
    "ge_proton_reuse_existing": True,
}


# --------------------------------------------------------------------------
# config / helpers
# --------------------------------------------------------------------------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=30):
    return json.loads(http_get(url, timeout))


def human_size(n):
    if not n:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024


def safe_dirname(title):
    name = re.sub(r"[^\w\s.\-()&']", "", title).strip()
    return re.sub(r"\s+", " ", name)[:80] or "game"


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\x1b\][^\x07]*\x07")


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def is_logged_in():
    try:
        r = subprocess.run(
            ["secret-tool", "search", "service", "Xodus Service", "username", "user-tokens"],
            capture_output=True, text=True, timeout=10)
        return "Xodus Service" in r.stdout + r.stderr
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------
# Game Pass catalog
# --------------------------------------------------------------------------

def _image_url(images, purposes, width):
    for purpose in purposes:
        for img in images:
            if img.get("ImagePurpose") == purpose and img.get("Uri"):
                uri = img["Uri"]
                if uri.startswith("//"):
                    uri = "https:" + uri
                return f"{uri}?w={width}"
    return ""


def _parse_product(p, collection=None):
    lp = (p.get("LocalizedProperties") or [{}])[0]
    images = lp.get("Images") or []
    size, platforms = 0, set()
    for avail in p.get("DisplaySkuAvailabilities") or []:
        for pkg in (avail.get("Sku", {}).get("Properties", {}).get("Packages") or []):
            deps = [d.get("PlatformName") for d in pkg.get("PlatformDependencies") or []]
            platforms.update(d for d in deps if d)
            if "Windows.Desktop" in deps:
                size = max(size, pkg.get("MaxDownloadSizeInBytes") or 0)
    if collection == "EA Play" and platforms and "Windows.Desktop" not in platforms:
        return None  # console-only EA Play title
    props = p.get("Properties") or {}
    genres = list(props.get("Categories") or ([props["Category"]] if props.get("Category") else []))
    if collection and collection not in genres:
        genres.append(collection)
    return {
        "collection": collection or "Game Pass",
        "id": p["ProductId"],
        "title": lp.get("ProductTitle", p["ProductId"]),
        "publisher": lp.get("PublisherName", ""),
        "description": lp.get("ShortDescription") or lp.get("ProductDescription", ""),
        "genres": genres,
        "size": size,
        "poster": _image_url(images, ["Poster", "BoxArt", "BrandedKeyArt"], 300),
        "poster_big": _image_url(images, ["Poster", "BoxArt"], 600),
        "hero": _image_url(images, ["TitledHeroArt", "SuperHeroArt", "Screenshot"], 960),
    }


def fetch_catalog(market, language, progress=None):
    def sigl_ids(sigl_id):
        sigl = http_json(f"https://catalog.gamepass.com/sigls/v2?id={sigl_id}&language={language}&market={market}")
        return [x["id"] for x in sigl if "id" in x]

    ids = sigl_ids(ALL_PC_GAMES_SIGL)
    seen = set(ids)
    ea_ids = []
    for sid in EA_PLAY_SIGLS:
        try:
            ea_ids += [i for i in sigl_ids(sid) if i not in seen and not seen.add(i)]
        except (OSError, ValueError):
            pass  # EA Play is optional: the Game Pass list still loads
    collection_of = {i: "EA Play" for i in ea_ids}
    ids = ids + ea_ids
    batches = [ids[i:i + 20] for i in range(0, len(ids), 20)]
    games, done = [], 0

    def fetch(batch):
        url = ("https://displaycatalog.mp.microsoft.com/v7.0/products?bigIds="
               f"{','.join(batch)}&market={market}&languages={language},en-us,neutral")
        for attempt in range(4):
            try:
                return http_json(url, timeout=60).get("Products", [])
            except (OSError, ValueError):
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        for products in ex.map(fetch, batches):
            for p in products:
                try:
                    g = _parse_product(p, collection_of.get(p.get("ProductId")))
                except (KeyError, IndexError, TypeError):
                    continue
                if g:
                    games.append(g)
            done += 1
            if progress:
                progress(done, len(batches))
    games.sort(key=lambda g: g["title"].lower())
    return games


def load_catalog(cfg, force=False, progress=None):
    cache = CACHE_DIR / f"catalog-{cfg['market']}-{cfg['language']}.json"
    if not force and cache.exists() and time.time() - cache.stat().st_mtime < CATALOG_MAX_AGE:
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    try:
        games = fetch_catalog(cfg["market"], cfg["language"], progress)
    except Exception:
        if cache.exists():
            return json.loads(cache.read_text())
        raise
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(games))
    return games


# --------------------------------------------------------------------------
# installed games
# --------------------------------------------------------------------------

def read_meta(game_dir):
    try:
        return json.loads((Path(game_dir) / META_NAME).read_text())
    except (OSError, ValueError):
        return None


def write_meta(game_dir, meta):
    Path(game_dir).mkdir(parents=True, exist_ok=True)
    (Path(game_dir) / META_NAME).write_text(json.dumps(meta, indent=2))


def installed_games(cfg):
    root = Path(cfg["games_dir"])
    games = []
    if not root.is_dir():
        return games
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = read_meta(d)
        if meta is None:
            if find_manifest(d) is None:
                continue
            meta = {"id": "", "title": d.name, "status": "installed"}
        meta["dir"] = str(d)
        if meta.get("status") == "installed" and not (d / ".xodus-streaming.msixvc").exists():
            meta["status"] = "incomplete"
        games.append(meta)
    return games


def find_installed(cfg, product_id):
    for g in installed_games(cfg):
        if g.get("id") == product_id:
            return g
    return None


def find_manifest(game_dir):
    for f in Path(game_dir).iterdir():
        if f.name.lower() == "microsoftgame.config":
            return f
    return None


def game_identity(game_dir):
    """(MSAAppId, TitleId as decimal) from MicrosoftGame.config, or (None, None)."""
    manifest = find_manifest(game_dir)
    if manifest is None:
        return None, None
    try:
        root = ET.parse(manifest).getroot()
    except ET.ParseError:
        return None, None
    msa = title = None
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "MSAAppId" and el.text:
            msa = el.text.strip()
        elif tag == "TitleId" and el.text:
            try:
                title = int(el.text.strip(), 16)  # hex in the config, decimal on the wire
            except ValueError:
                pass
    return msa, title


def prewarm_identity(game_dir, log=print):
    """Ask xodus-service for the signed-in user's identity before the game does.

    The first lookup after the service starts can take 30+ s (full Xbox Live flow);
    the service caches it, so doing it while the game loads makes sign-in instant."""
    import socket
    import struct
    msa, title = game_identity(game_dir)
    sock_path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "xodus.sock"
    ident = ""
    if msa and title:
        ident = f"<ClientId>{msa}</ClientId><TitleId>{title}</TitleId>"
    body = f"<UserInfoRequest>{ident}</UserInfoRequest>".encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(120)
            s.connect(str(sock_path))
            s.sendall(struct.pack("<IHH", 0x58445358, 7, len(body)) + body)
            hdr = b""
            while len(hdr) < 8:
                chunk = s.recv(8 - len(hdr))
                if not chunk:
                    return
                hdr += chunk
            _, rtype, size = struct.unpack("<IHH", hdr)
            data = b""
            while len(data) < size:
                chunk = s.recv(size - len(data))
                if not chunk:
                    break
                data += chunk
        log("identity pre-warm: " + ("signed in" if b"<SignedIn>true" in data else
                                    "not signed in" if b"<SignedIn>false" in data else f"reply type {rtype}"))
    except OSError as e:
        log(f"identity pre-warm skipped: {e}")


def eac_real_executable(game_dir):
    """If the game is wrapped by the EasyAntiCheat bootstrapper, the real game exe from
    EasyAntiCheat/Settings.json (EAC has no Linux runtime in Game Pass builds, so the
    bootstrapper itself can never run under Wine)."""
    settings = Path(game_dir) / "EasyAntiCheat" / "Settings.json"
    if not settings.exists():
        return None
    try:
        exe = json.loads(settings.read_text()).get("executable", "")
    except (OSError, ValueError):
        return None
    exe = exe.replace("/", "\\")
    return exe if exe and (Path(game_dir) / exe.replace("\\", "/")).exists() else None


def find_executable(game_dir, eac_bypass=None):
    """Main PC executable from MicrosoftGame.config (same rules as xodus PR #124),
    bypassing an EasyAntiCheat bootstrapper when the config names one.

    eac_bypass: None = bypass when the game ships the bootstrapper (auto), False = never,
    True = the compat entry asks for it (same as auto; it is recorded for the report)."""
    manifest = find_manifest(game_dir)
    if manifest is None:
        return None
    real = eac_real_executable(game_dir) if eac_bypass is not False else None
    if real:
        return real
    try:
        root = ET.parse(manifest).getroot()
    except ET.ParseError:
        return None
    fallback = None
    for el in root.iter():
        if not el.tag.split("}")[-1] == "Executable":
            continue
        name = el.get("Name")
        if not name or el.get("IsDevOnly", "").lower() == "true":
            continue
        if el.get("TargetDeviceFamily", "PC").upper() == "PC":
            return name
        fallback = fallback or name
    return fallback


# --------------------------------------------------------------------------
# per-title compatibility database (data/compat/titles.json)
# --------------------------------------------------------------------------

COMPAT_DEFAULTS = {
    "status": "untested",
    "notes": "",
    "tested": {},
    "runtime": {"wine": "wine-gdk", "min_winegdk": ""},
    "prefix": "shared",
    "dxvk": True,
    "vkd3d": True,
    "env": {},
    "dll_overrides": {},
    "launch": {"executable": "", "args": [], "eac_bypass": None, "working_dir": ""},
    "winetricks": [],
    "registry": [],
    # titles delivered through a third-party launcher (data/compat/launchers.json) and their
    # anti-cheat: {"kind": "eac|battleye|...", "linux_module": bool, "bypass": "exe", "online": ...}
    "launcher": "",
    "launcher_game_id": None,
    "anticheat": {},
}
_compat_db = None


def _merge(base, over):
    """Dicts merge recursively; everything else (lists included) is replaced."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _valid_compat_db(db):
    return isinstance(db, dict) and db.get("schema_version") == 1 and isinstance(db.get("titles"), dict)


def load_compat_db(force=False):
    """The database: the bundled titles.json or the downloaded copy, whichever is newer
    (by its `updated` date). Never touches the network - see refresh_compat_db()."""
    global _compat_db
    if _compat_db is not None and not force:
        return _compat_db
    best = None
    for p in (BUNDLED_COMPAT, COMPAT_CACHE):
        db = _read_json(p) if p and p.exists() else None
        if _valid_compat_db(db) and (best is None or
                                     (db.get("updated") or "") > (best.get("updated") or "")):
            best = db
    _compat_db = best or {"schema_version": 1, "defaults": {}, "titles": {}}
    return _compat_db


def refresh_compat_db(force=False):
    """Download titles.json from the project repository into the cache when the cached copy is
    older than a day. Returns True when a new copy arrived. Meant for a background thread."""
    if not force and COMPAT_CACHE.exists() and \
            time.time() - COMPAT_CACHE.stat().st_mtime < COMPAT_MAX_AGE:
        return False
    data = http_get(COMPAT_URL, 30)
    if not _valid_compat_db(json.loads(data)):
        raise ValueError("unexpected compatibility database format")
    COMPAT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    COMPAT_CACHE.write_bytes(data)
    load_compat_db(force=True)
    return True


def user_compat_override(store_id):
    """~/.config/xodus-launcher/compat/<STOREID>.json: the user's own tweaks for one title."""
    if not store_id:
        return {}
    return _read_json(COMPAT_USER_DIR / f"{store_id}.json") or {}


def compat_entry(store_id, db=None):
    """defaults ⊕ database defaults ⊕ the title's entry ⊕ the user's override. Always returns
    an entry; unknown titles get the defaults with status "untested"."""
    db = db or load_compat_db()
    title_entry = (db.get("titles") or {}).get(store_id or "") or {}
    user = user_compat_override(store_id)
    entry = _merge(_merge(_merge(COMPAT_DEFAULTS, db.get("defaults")), title_entry), user)
    if entry.get("status") not in COMPAT_STATUSES:
        entry["status"] = "untested"
    entry["id"] = store_id or ""
    entry["_db"] = title_entry  # what the database itself pins for this title
    entry["_user"] = user       # what the user overrode (beats everything, Settings included)
    return entry


def game_store_id(game_dir):
    """Store ID of an installed game: the launcher's meta file, else MicrosoftGame.config."""
    meta = read_meta(game_dir)
    if meta and meta.get("id"):
        return meta["id"]
    manifest = find_manifest(game_dir)
    if manifest is not None:
        try:
            for el in ET.parse(manifest).getroot().iter():
                if el.tag.split("}")[-1] == "StoreId" and el.text:
                    return el.text.strip()
        except ET.ParseError:
            pass
    return ""


def wine_from_name(name):
    """"wine-gdk" / "wine-xodus" / another build dir under DATA_DIR, "proton", or a path."""
    if not name:
        return ""
    if name == "proton":
        return find_proton_wine()
    p = Path(name).expanduser()
    if p.is_absolute():
        return str(p) if p.exists() else ""
    w = DATA_DIR / name / "bin/wine"
    return str(w) if w.exists() else ""


def resolve_wine(cfg, entry):
    """Wine for a title: the user's per-title override, then the build the database pins for
    this title, then the Settings choice, then the database default, then whatever is installed."""
    user_wine = ((entry.get("_user") or {}).get("runtime") or {}).get("wine")
    db_wine = ((entry.get("_db") or {}).get("runtime") or {}).get("wine")
    default_wine = (entry.get("runtime") or {}).get("wine")
    return (wine_from_name(user_wine) or wine_from_name(db_wine) or cfg.get("wine")
            or wine_from_name(default_wine) or find_wine())


def game_prefix(cfg, game_dir, entry=None):
    """The prefix a title runs in: the shared one from Settings, or DATA_DIR/prefix-<STOREID>
    when its entry says "per-game"."""
    if entry is None:
        entry = compat_entry(game_store_id(game_dir))
    if entry.get("prefix") == "per-game" and entry.get("id"):
        return str(DATA_DIR / f"prefix-{entry['id']}")
    return cfg["prefix"]


def winegdk_release(wine):
    """Release tag of a prebuilt WineGDK (download_winegdk_release writes it); '' for source builds."""
    try:
        return (Path(wine).parent.parent / ".release").read_text().strip()
    except OSError:
        return ""


def wine_version(wine):
    try:
        return subprocess.run([wine, "--version"], capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _release_date(tag):
    m = re.match(r"winegdk-(\d{8})", tag or "")
    return m.group(1) if m else ""


def apply_prefix_tweaks(cfg, wine, prefix, entry, log=print):
    """Registry entries and winetricks verbs from the entry, each applied once per prefix
    (tracked in <prefix>/.xodus-compat-applied.json). Returns True when something was applied."""
    marker = Path(prefix) / ".xodus-compat-applied.json"
    done = _read_json(marker) or {}
    done.setdefault("registry", [])
    done.setdefault("winetricks", [])
    env = wine_env(cfg, wine, entry, prefix)
    changed = False
    for reg in entry.get("registry") or []:
        if not isinstance(reg, dict) or not reg.get("key"):
            log(f"compat: ignoring registry entry without a key: {reg}")
            continue
        key = json.dumps(reg, sort_keys=True)
        if key in done["registry"]:
            continue
        cmd = [wine, "reg", "add", reg["key"], "/f"]
        if reg.get("name"):
            cmd += ["/v", str(reg["name"])]
        if reg.get("type"):
            cmd += ["/t", str(reg["type"])]
        if reg.get("value") is not None:
            cmd += ["/d", str(reg["value"])]
        log("$ " + " ".join(cmd))
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"compat: registry entry failed: {e}")
            continue
        if r.returncode == 0:
            done["registry"].append(key)
            changed = True
        else:
            log("compat: registry entry failed: " + (r.stdout + r.stderr).strip()[-300:])
    verbs = [v for v in entry.get("winetricks") or [] if v not in done["winetricks"]]
    if verbs and not shutil.which("winetricks"):
        log("WARNING: this title wants winetricks verbs (" + " ".join(verbs) +
            ") but winetricks is not installed")
    elif verbs:
        log("$ winetricks -q " + " ".join(verbs))
        e = dict(env)
        e["WINE"] = wine
        e["WINESERVER"] = str(Path(wine).parent / "wineserver")
        try:
            ok = subprocess.run(["winetricks", "-q"] + verbs, env=e, timeout=1800,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError) as ex:
            log(f"compat: winetricks failed: {ex}")
            ok = False
        if ok:
            done["winetricks"] += verbs
            changed = True
        else:
            log("compat: winetricks failed; the verbs are retried at the next launch")
    if changed:
        marker.write_text(json.dumps(done, indent=2))
    return changed


# ---------- compatibility reports ----------

CREDENTIAL_WORDS = ("xbl3.0", "signature", "token", "authorization")


def scrub_log_lines(lines):
    """Drop anything that could carry a credential: tokens and signatures never leave the machine."""
    return ["[line removed: may contain credentials]"
            if any(w in line.lower() for w in CREDENTIAL_WORDS) else line for line in lines]


def system_info():
    info = {"kernel": os.uname().release, "distro": "", "gpu": "", "vulkan": ""}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                info["distro"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    probes = (
        (["lspci"], "gpu", r"VGA|3D controller", ": "),
        (["vulkaninfo", "--summary"], "vulkan", r"deviceName|driverName|driverInfo", "="),
    )
    for cmd, key, pattern, sep in probes:
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = [line.split(sep, 1)[-1].strip() for line in out.splitlines()
                 if re.search(pattern, line)]
        info[key] = "; ".join(dict.fromkeys(found))
    return info


def launcher_commit():
    repo = _SELF.parent.parent
    if not (repo / ".git").exists():
        return ""
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def write_compat_report(cfg, game, entry, status="", notes=""):
    """Write CACHE_DIR/reports/<STOREID>-<date>.json (for a bug report or a database entry) and
    return its path. The game log excerpt is scrubbed of anything credential-like."""
    game_dir = game.get("dir") or ""
    wine = resolve_wine(cfg, entry)
    launch = entry.get("launch") or {}
    tail = []
    if game_dir:
        logfile = LOG_DIR / (safe_dirname(Path(game_dir).name) + ".log")
        if logfile.exists():
            tail = scrub_log_lines(logfile.read_text(errors="replace").splitlines()[-200:])
    report = {
        "schema_version": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "game": {
            "id": game.get("id") or entry.get("id") or "",
            "title": game.get("title", ""),
            "dir": game_dir,
            "executable": (launch.get("executable") or
                           find_executable(game_dir, launch.get("eac_bypass"))) if game_dir else None,
        },
        "result": {"status": status or entry.get("status", "untested"), "notes": notes},
        "system": system_info(),
        "launcher": {"commit": launcher_commit(), "path": str(_SELF)},
        "wine": {
            "path": wine,
            "version": wine_version(wine) if wine else "",
            "winegdk_release": winegdk_release(wine) if wine else "",
            "prefix": game_prefix(cfg, game_dir, entry) if game_dir else cfg["prefix"],
        },
        "compat_entry": {k: v for k, v in entry.items() if not k.startswith("_")},
        "user_override": entry.get("_user") or {},
        "log_tail": tail,
    }
    path = CACHE_DIR / "reports" / \
        f"{report['game']['id'] or 'unknown'}-{time.strftime('%Y-%m-%d-%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))
    return path


def report_issue_url(report, path):
    """A prefilled GitHub issue for a report (kept short: the file has the details)."""
    g, s, w = report["game"], report["system"], report["wine"]
    status = report["result"]["status"]
    body = (f"**Game:** {g['title']} ({g['id']})\n"
            f"**Result:** {STATUS_LABELS.get(status, status)}\n\n{report['result']['notes']}\n\n"
            f"**System:** {s['distro']}, kernel {s['kernel']}\n**GPU:** {s['gpu']}\n"
            f"**Vulkan:** {s['vulkan']}\n**Wine:** {w['version']} {w['winegdk_release']}\n"
            f"**Launcher:** {report['launcher']['commit'] or 'unknown'}\n\n"
            f"Report file (please attach it): `{path}`\n")
    q = urllib.parse.urlencode({"title": f"Compat: {g['title']} ({g['id']})",
                                "labels": "compat-report", "body": body})
    return f"{REPORT_ISSUES_URL}?{q}"


# --------------------------------------------------------------------------
# third-party launchers and anti-cheat (launcher/thirdparty.py, data/compat/launchers.json)
# --------------------------------------------------------------------------

ANTICHEAT_LABELS = {"eac": "Easy Anti-Cheat", "battleye": "BattlEye", "ricochet": "Ricochet",
                    "vanguard": "Vanguard", "none": "none", "unknown": "unknown"}


def thirdparty_module():
    """launcher/thirdparty.py (third-party launchers in GE-Proton prefixes, anti-cheat runtimes),
    imported on first use from the launcher's own directory. Raises RuntimeError with a readable
    message when it is missing or broken, so every caller can fall back or show it."""
    d = str(_SELF.parent)
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import thirdparty
    except Exception as e:  # noqa: BLE001 - ImportError, or a syntax error in the module
        raise RuntimeError("third-party launcher support is unavailable: launcher/thirdparty.py "
                           f"could not be imported ({e})") from e
    return thirdparty


def launchers_db():
    """{launcher id: recipe} from data/compat/launchers.json: through the thirdparty module when
    it imports, else read next to titles.json (names and tags still work), else {}."""
    try:
        return dict(thirdparty_module().load_launchers_db().get("launchers") or {})
    except Exception:  # noqa: BLE001 - the module reports its own problems where it matters
        pass
    p = BUNDLED_COMPAT.parent / "launchers.json" if BUNDLED_COMPAT else None
    db = _read_json(p) if p and p.exists() else None
    return dict(db.get("launchers") or {}) if isinstance(db, dict) else {}


def launcher_name(lid, db=None):
    rec = (launchers_db() if db is None else db).get(lid) or {}
    return str(rec.get("name") or lid)


def launcher_for_publisher(publisher, db=None):
    """Id of the recipe whose `publisher_match` prefixes fit a catalog publisher name
    ("Ubisoft Entertainment" -> ubisoft-connect): the Ubisoft, EA, Blizzard and Riot titles never
    have a Store package, so they are treated as launcher titles before a download is even
    tried. Recipes without prefixes (Rockstar: its Game Pass titles are Store packages) never
    match. "" when nothing fits."""
    pub = re.sub(r"\s+", " ", str(publisher or "")).strip().lower()
    if not pub:
        return ""
    for lid, rec in sorted((launchers_db() if db is None else db).items()):
        pats = (rec.get("publisher_match") or []) if isinstance(rec, dict) else []
        for pat in pats:
            pat = str(pat).strip().lower()
            if pat and pub.startswith(pat):
                return lid
    return ""


def title_launcher(entry, game=None, inst=None, db=None):
    """Which third-party launcher delivers a title: the compatibility entry's `launcher`, the
    library link's, else the recipe matching the catalog publisher; "" for a Store title."""
    lid = (entry or {}).get("launcher") or (inst or {}).get("launcher") or ""
    if lid:
        return str(lid)
    pub = (game or {}).get("publisher") or (inst or {}).get("publisher") or ""
    return launcher_for_publisher(pub, db)


def launcher_meta(game_dir):
    """The library meta of a title linked to a game inside a third-party launcher (status
    "launcher": the folder under games_dir holds only the meta file), or None."""
    meta = read_meta(game_dir)
    if meta and meta.get("status") == "launcher" and meta.get("launcher"):
        return meta
    return None


def link_launcher_game(cfg, game, lid, game_id, game_name="", game_dir=""):
    """Record that a Game Pass title is installed inside a third-party launcher: a folder named
    like the title under games_dir with just the meta file (status "launcher", the launcher and
    the game's id there), so the library lists it and Play / --play know what to start."""
    dest = Path(cfg["games_dir"]) / safe_dirname(game.get("title") or game_id)
    meta = read_meta(dest) or {}
    meta.update({k: game.get(k) for k in ("id", "title", "publisher", "poster", "poster_big",
                                          "hero", "genres") if game.get(k) is not None})
    meta.update({"status": "launcher", "launcher": lid, "launcher_game_id": str(game_id),
                 "launcher_game_name": game_name or "", "launcher_game_dir": str(game_dir or "")})
    meta.pop("dir", None)  # installed_games() adds it at read time
    write_meta(dest, meta)
    return dest


def unlink_launcher_game(game_dir):
    """Remove a launcher title's library link: the meta file, and its folder when nothing else
    is in it. The game itself stays installed in the launcher."""
    d = Path(game_dir)
    (d / META_NAME).unlink(missing_ok=True)
    try:
        d.rmdir()
    except OSError:
        pass


def anticheat_status(game_dir, entry=None, wine=None):
    """(info, env) from the thirdparty module: anticheat_for_game() for the Wine build that will
    run the game (the Steam runtimes only work under a Proton-derived build, never under
    WineGDK) and the PROTON_*_RUNTIME variables of anticheat_env(). A neutral answer (kind
    "unknown", no env) when the module is missing or the check fails, so launching never
    depends on it."""
    try:
        tp = thirdparty_module()
        info = dict(tp.anticheat_for_game(game_dir, entry, wine=wine))
        env = {k: str(v) for k, v in (tp.anticheat_env(info) or {}).items()}
    except Exception as e:  # noqa: BLE001
        info = {"message": f"anti-cheat check skipped: {e}"}
        env = {}
    for k, v in (("kind", "unknown"), ("linux_module", False), ("runtime", None),
                 ("runtime_usable", False), ("bypass", None), ("online", "unknown"), ("message", "")):
        info.setdefault(k, v)
    return info, env


def anticheat_summary(info):
    """One line for the game log and reports."""
    kind = info.get("kind") or "unknown"
    if kind == "none":
        return "anti-cheat: none"
    return (f"anti-cheat: {ANTICHEAT_LABELS.get(kind, kind)}, Linux module: "
            f"{'yes' if info.get('linux_module') else 'no'}, runtime: "
            f"{info.get('runtime') or 'not installed'}, online: {info.get('online') or 'unknown'}"
            + (f" - {info['message']}" if info.get("message") else ""))


def launch_launcher_game(cfg, game_dir, log=print, on_start=None):
    """Start a linked title through its third-party launcher (ThirdPartyLauncher.launch_game with
    the game id from the compatibility entry, else from the meta) and wait until the launcher's
    prefix is idle (the client and the game are closed). Returns (code, tail) like launch_game()."""
    meta = launcher_meta(game_dir) or {}
    try:
        tpl = thirdparty_module().ThirdPartyLauncher(meta.get("launcher", ""), cfg, log=log)
    except Exception as e:  # noqa: BLE001 - module missing, unknown launcher id
        return 1, f"Cannot start {meta.get('launcher') or 'the launcher'}: {e}"
    if not tpl.is_installed():
        return 1, f"{tpl.name} is not set up yet - open Xodus Launcher and use Set up {tpl.name}."
    entry = compat_entry(meta.get("id") or game_store_id(game_dir))
    # the user's own link (Link / Re-link installed game…) beats what the database pins
    gid = meta.get("launcher_game_id") or entry.get("launcher_game_id") or None
    header = [f"compat: {entry['id'] or 'unknown title'} status={entry['status']} via {tpl.name} "
              f"game={gid or '?'} prefix={tpl.prefix}"]
    if entry.get("status") == "broken":
        header.append("compat: this title is known not to work yet. " + (entry.get("notes") or ""))
    for line in header:
        log(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / (safe_dirname(Path(game_dir).name) + ".log")).write_text("\n".join(header) + "\n")
    try:
        p = tpl.launch_game(gid, on_start=on_start)
    except Exception as e:  # noqa: BLE001 - GE-Proton download, missing executable, ...
        return 1, f"Could not start {tpl.name}: {e}"
    code = p.wait()
    if code == 0:
        # the executable hands off to the real client within seconds (UbisoftConnect.exe ->
        # upc.exe); "running" means until nothing is left in the launcher's prefix
        tpl.wait()
    return code, "" if code == 0 else f"{tpl.name} exited with code {code}"


def stop_game(cfg, game_dir):
    """Force-stop whatever a title runs in: its third-party launcher's prefix, or its Wine prefix."""
    meta = launcher_meta(game_dir)
    if meta:
        try:
            thirdparty_module().ThirdPartyLauncher(meta["launcher"], cfg).stop()
        except Exception:  # noqa: BLE001 - best effort, like stop_wine
            pass
        return
    stop_wine(cfg, game_prefix(cfg, game_dir))


# --------------------------------------------------------------------------
# running xodus-cli under a pseudo terminal (so progress bars render)
# --------------------------------------------------------------------------

class PtyProcess:
    def __init__(self, cmd, env=None, cwd=None):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 240, 0, 0))

        def preexec():
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env,
                                     cwd=cwd, start_new_session=True, preexec_fn=preexec)
        os.close(slave)

    def chunks(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            yield data.decode("utf-8", "replace")
        os.close(self.master)

    def write(self, text):
        try:
            os.write(self.master, text.encode())
        except OSError:
            pass

    def kill(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def wait(self):
        return self.proc.wait()


UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}


def parse_size(text):
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", text.strip())
    return float(m.group(1)) * UNITS.get(m.group(2), 1) if m else 0


TOTAL_BAR_RE = re.compile(
    r"^(Initializing|Downloading ntfs\.\.\.|Downloading)\s+(.+?)/\s*(.+?)\s{2,}(\S+ ?\S*/s)?.*?(\d+)%\s*$")


def run_download(cfg, product_id, dest, on_progress, on_log, cancel_event):
    """Run `xodus-cli streaming`. Returns (ok, message)."""
    cmd = [cfg["xodus_cli"], "streaming", product_id, str(dest),
           "-m", cfg["market"], "-p", str(cfg.get("parallel", 4))]
    on_log("$ " + " ".join(cmd))
    env = dict(os.environ)
    env.pop("XODUS_LOG", None)
    env["TERM"] = "xterm-256color"  # without TERM (app-menu launch) xodus-cli hides its progress bars
    proc = PtyProcess(cmd, env=env)
    watcher = threading.Thread(target=lambda: (cancel_event.wait(), proc.kill()), daemon=True)
    watcher.start()
    buf, errors = "", []
    recent, tried, awaiting = "", [], None  # the interactive sub-product prompt, see below
    for chunk in proc.chunks():
        buf += chunk
        recent = (recent + strip_ansi(chunk))[-4000:]
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = strip_ansi(raw).strip()
            if not line:
                continue
            m = TOTAL_BAR_RE.match(line)
            if m:
                done_b, total_b = parse_size(m.group(2)), parse_size(m.group(3))
                pct = int(done_b * 100 / total_b) if total_b else 0
                on_progress(m.group(1), min(pct, 100), m.group(2).strip(), m.group(3).strip(),
                            (m.group(4) or "").strip())
                continue
            if "%" in line and "[" in line:
                continue  # per-file bar
            on_log(line)
            low = line.lower()
            if "error" in low or "panicked" in low or "not found" in low or "not enough" in low \
                    or "could not" in low or "failed" in low or "unexpected" in low:
                errors.append(line)
        # xodus-cli asks "Select files to download" when the product has no PC package itself and
        # lists the products that license it. Those are usually the Game Pass subscriptions (no
        # package either) and the product itself (an endless loop), so try each other entry once
        # by typing it into the filter, and give up with a clear message when none is left.
        if awaiting and f"> Select files to download {awaiting}" in recent:
            awaiting, recent = None, ""
        if not awaiting and "Select files to download" in recent and "to filter]" in recent:
            options = re.findall(r"(?m)^[>\s]*([0-9A-Z]{12})\s*$", recent)
            choice = next((o for o in options if o != product_id and o not in tried), None)
            if choice is None or len(tried) >= 4:
                errors.append("This title has no PC package in the Store (it is probably delivered "
                              "through a third-party launcher such as Ubisoft Connect), so xodus "
                              "cannot download it.")
                on_log(errors[-1])
                proc.kill()
                break
            tried.append(choice)
            awaiting = choice
            on_log(f"(xodus asked to pick a sub-product - trying {choice})")
            proc.write(choice + "\r")
    code = proc.wait()
    if tried and any("displaycatalog" in e for e in errors):
        # the entries xodus offered were subscriptions, not packages: say what that means
        errors.insert(0, "This title has no PC package in the Store (it is probably delivered through "
                         "a third-party launcher such as Ubisoft Connect), so xodus cannot download it.")
        on_log(errors[0])
    cancelled = cancel_event.is_set()
    cancel_event.set()  # release the watcher thread
    if cancelled:
        return False, "Cancelled"
    if code == 0 and (Path(dest) / ".xodus-streaming.msixvc").exists() and not errors:
        return True, "Installed"
    if errors:
        return False, errors[0] if tried else errors[-1]  # the "no PC package" explanation first
    return False, f"xodus-cli exited with code {code}"


# --------------------------------------------------------------------------
# Wine / Proton (xodus build)
# --------------------------------------------------------------------------

PROTON_DIR = DATA_DIR / "proton"
# source builds, best first: WineGDK (+xodus memfd loading and local fixes), then the xodus wine fork
BUILT_WINES = [DATA_DIR / "wine-gdk/bin/wine", DATA_DIR / "wine-xodus/bin/wine"]


def find_proton_wine():
    for wine in sorted(PROTON_DIR.glob("*/files/bin/wine"), reverse=True):
        return str(wine)
    return ""


def find_wine():
    """Prefer the source-built xodus wine (supports loading the decrypted exe from memfd)."""
    for wine in BUILT_WINES:
        if wine.exists():
            return str(wine)
    return find_proton_wine()


def latest_proton_artifact():
    runs = http_json("https://api.github.com/repos/xodus-gaming/Proton/actions/runs"
                     "?status=success&per_page=20")
    for run in runs.get("workflow_runs", []):
        if run.get("name") != "Snapshot":
            continue
        arts = http_json(run["artifacts_url"]).get("artifacts", [])
        tar = next((a for a in arts if a["name"].endswith("x86_64.tar.xz") and not a["expired"]), None)
        sha = next((a for a in arts if a["name"].endswith("x86_64.sha512sum") and not a["expired"]), None)
        if tar:
            base = f"https://nightly.link/xodus-gaming/Proton/actions/runs/{run['id']}/"
            return tar["name"], base + tar["name"] + ".zip", (base + sha["name"] + ".zip") if sha else None
    raise RuntimeError("No downloadable xodus Proton build found on GitHub")


def dll_overrides(compat=None):
    """WINEDLLOVERRIDES: DXVK / VKD3D-Proton from the prefix (unless the title's entry turns
    them off), xgameruntime native, no Mono/Gecko prompt, plus the entry's own overrides."""
    ov = {}
    dx = "n,b" if compat is None or compat.get("dxvk", True) else "b"
    for d in ("d3d8", "d3d9", "d3d10core", "d3d11", "dxgi"):
        ov[d] = dx
    d12 = "n,b" if compat is None or compat.get("vkd3d", True) else "b"
    for d in ("d3d12", "d3d12core"):
        ov[d] = d12
    ov.update({"xaudio2_7": "n,b", "x3daudio1_7": "n,b", "xgameruntime": "n,b",
               "winegstreamer": "d",
               "mscoree": "", "mshtml": ""})  # never block on the Mono/Gecko install prompt
    if compat:
        ov.update({k: str(v) for k, v in (compat.get("dll_overrides") or {}).items()})
    return ";".join(f"{k}={v}" for k, v in ov.items())


def wine_env(cfg, wine, compat=None, prefix=None):
    """Environment for the Wine build; compat is the title's compatibility entry, if any."""
    env = dict(os.environ)
    env.update({
        "WINEPREFIX": prefix or cfg["prefix"],
        "WINEDLLOVERRIDES": dll_overrides(compat),
        "WINEDEBUG": "fixme+gdkc,fixme+xgameruntime,err+all",  # cheap: only our runtime's gaps and errors
        "WINEFSYNC": "1",
        "WINEESYNC": "1",
        "VKD3D_CONFIG": "dxr11",
        "PULSE_LATENCY_MSEC": "60",
    })
    bindir = str(Path(wine).parent)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env.update(cfg.get("extra_env") or {})
    if compat:  # the title's entry (and the user's override of it) is the most specific layer
        env.update({k: str(v) for k, v in (compat.get("env") or {}).items()})
    return env


def download_resumable(url, path, log, attempts=30):
    """Download with HTTP Range resume; the artifact host tends to stall mid-transfer."""
    got, total, last = 0, 0, 0
    with open(path, "wb") as f:
        for attempt in range(attempts):
            headers = {"User-Agent": UA}
            if got:
                headers["Range"] = f"bytes={got}-"
            try:
                # nightly.link answers with a short-lived signed redirect; resolve it every time
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                            timeout=30) as r:
                    if got and r.status != 206:
                        f.seek(0)
                        f.truncate()
                        got = 0
                    if not total:
                        total = got + int(r.headers.get("Content-Length") or 0)
                    while True:
                        block = r.read(1 << 18)
                        if not block:
                            break
                        f.write(block)
                        got += len(block)
                        if time.time() - last > 1:
                            last = time.time()
                            log(f"  {human_size(got)} / {human_size(total)}", replace=True)
                if total and got >= total:
                    return
            except (OSError, TimeoutError) as e:
                log(f"  connection stalled at {human_size(got)} ({e.__class__.__name__}), "
                    f"resuming…")
                time.sleep(min(2 + attempt, 10))
    raise RuntimeError(f"Download failed after {attempts} attempts ({human_size(got)} received)")


RELEASES_API = "https://api.github.com/repos/evilcomputer12/xodus-launcher/releases/latest"


def download_winegdk_release(log):
    """Fetch the prebuilt WineGDK tarball from the project's latest GitHub release into DATA_DIR."""
    rel = http_json(RELEASES_API, 60)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    tar_name = next((n for n in assets if n.endswith("-x86_64.tar.zst")), None)
    if not tar_name:
        raise RuntimeError("the latest release has no WineGDK tarball")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tpath = CACHE_DIR / tar_name
    log(f"Downloading prebuilt WineGDK {rel.get('tag_name')} (~420 MB) ...")
    download_resumable(assets[tar_name], tpath, log)
    if tar_name + ".sha256" in assets:
        expected = http_get(assets[tar_name + ".sha256"], 60).decode().split()[0]
        h = hashlib.sha256()
        with open(tpath, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != expected:
            tpath.unlink()
            raise RuntimeError("SHA-256 checksum mismatch for the WineGDK download")
        log("Checksum OK")
    log("Extracting ...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.which("tar") and shutil.which("zstd"):
        subprocess.run(["tar", "-I", "zstd", "-xf", str(tpath), "-C", str(DATA_DIR)], check=True)
    else:
        with tarfile.open(tpath, "r:zst") as t:  # Python >= 3.14
            t.extractall(DATA_DIR, filter="tar")
    tpath.unlink()
    if not (DATA_DIR / "wine-gdk/bin/wine").exists():
        raise RuntimeError("the WineGDK tarball did not contain wine-gdk/bin/wine")
    # remembered for compatibility entries / reports (runtime.min_winegdk)
    (DATA_DIR / "wine-gdk/.release").write_text(str(rel.get("tag_name", "")) + "\n")
    log("WineGDK installed")


def setup_wine(cfg, log):
    """Get WineGDK (prebuilt release, or a local build), DXVK/VKD3D from the xodus Proton build,
    and create the prefix. Returns the wine path."""
    PROTON_DIR.mkdir(parents=True, exist_ok=True)
    wine = find_proton_wine()
    if not wine:
        name, url, sha_url = latest_proton_artifact()
        log(f"Downloading {name} (~280 MB) ...")
        zpath = CACHE_DIR / (name + ".zip")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        download_resumable(url, zpath, log)
        log("Extracting ...")
        with zipfile.ZipFile(zpath) as z:
            inner = next(n for n in z.namelist() if n.endswith(".tar.xz"))
            tpath = CACHE_DIR / inner
            z.extract(inner, CACHE_DIR)
        if sha_url:
            sha_zip = CACHE_DIR / "sha.zip"
            sha_zip.write_bytes(http_get(sha_url, 60))
            with zipfile.ZipFile(sha_zip) as z:
                expected = z.read(z.namelist()[0]).decode().split()[0]
            h = hashlib.sha512()
            with open(tpath, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != expected:
                raise RuntimeError("SHA-512 checksum mismatch for the Proton download")
            log("Checksum OK")
            sha_zip.unlink()
        with tarfile.open(tpath) as t:
            t.extractall(PROTON_DIR, filter="tar")
        tpath.unlink()
        zpath.unlink()
        wine = find_proton_wine()
        if not wine:
            raise RuntimeError("Proton archive did not contain files/bin/wine")
    if not any(w.exists() for w in BUILT_WINES):
        download_winegdk_release(log)
    wine = find_wine()
    log(f"Wine: {wine}")
    if list(Path(wine).parent.parent.glob("lib*/wine/*/xgameruntime.dll")):
        log("xgameruntime.dll found in this Wine build")
    else:
        log("WARNING: xgameruntime.dll not found in this Wine build - games may not start")

    prepare_prefix(cfg, wine, cfg["prefix"], log)
    cfg["wine"] = wine
    save_config(cfg)
    log("Wine is ready.")
    return wine


def prepare_prefix(cfg, wine, prefix, log=print):
    """Create the prefix, or update it after switching Wine builds, and copy DXVK / VKD3D-Proton
    into it from the xodus Proton dist (setup_wine downloads that). Used for the shared prefix
    and for the per-game ones the compatibility database asks for."""
    prefix = Path(prefix)
    log(f"Creating / updating Wine prefix {prefix} (takes a minute) ...")
    prefix.mkdir(parents=True, exist_ok=True)
    # mscoree/mshtml disabled so wineboot never waits on the "install Wine Mono/Gecko" dialog
    e = wine_env(cfg, wine, prefix=str(prefix))
    e["WINEDLLOVERRIDES"] = "mscoree,mshtml="
    subprocess.run([wine, "wineboot", "-u"], env=e, timeout=600,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([str(Path(wine).parent / "wineserver"), "-w"], env=e, timeout=600)
    copied = 0
    proton = find_proton_wine()
    dist = Path(proton).parent.parent if proton else None  # source of DXVK / VKD3D-Proton
    archmap = {"x86_64-windows": "system32", "i386-windows": "syswow64"}
    for comp in ("dxvk", "vkd3d-proton"):
        for arch, sysdir in archmap.items():
            dlls = glob.glob(str(dist / "lib*" / "wine" / comp / arch / "*.dll")) if dist else []
            for dll in dlls:
                target = prefix / "drive_c/windows" / sysdir
                if target.is_dir():
                    dst = target / Path(dll).name
                    dst.unlink(missing_ok=True)  # Proton ships these read-only
                    shutil.copyfile(dll, dst)
                    copied += 1
    log(f"Installed {copied} DXVK/VKD3D-Proton DLLs into the prefix")
    return copied


# --------------------------------------------------------------------------
# launching
# --------------------------------------------------------------------------

def exe_candidates(name):
    win = name.replace("/", "\\")
    out = []
    for c in (name, win, "\\" + win.lstrip("\\"), win.lstrip("\\")):
        if c not in out:
            out.append(c)
    return out


def ensure_xodus_service(log=print):
    """WineGDK's xgameruntime talks to xodus-service over $XDG_RUNTIME_DIR/xodus.sock."""
    import socket
    sock_path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "xodus.sock"

    def alive():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(str(sock_path))
            return True
        except OSError:
            return False

    if alive():
        return True
    if not shutil.which("xodus-service"):
        log("xodus-service not installed")
        return False
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)  # stale socket from a crashed service
    log("starting xodus-service")
    subprocess.Popen(["xodus-service"], stdout=open(LOG_DIR / "xodus-service.log", "a"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if alive():
            return True
        time.sleep(0.2)
    log("xodus-service did not come up, see " + str(LOG_DIR / "xodus-service.log"))
    return False


def stop_wine(cfg, prefix=None):
    """Kill everything in the game's Wine prefix (used to force-stop a running game)."""
    wine = cfg.get("wine") or find_wine()
    if not wine:
        return
    env = dict(os.environ)
    env["WINEPREFIX"] = prefix or cfg["prefix"]
    try:
        subprocess.run([str(Path(wine).parent / "wineserver"), "-k"], env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def launch_game(cfg, game_dir, log=print, on_start=None):
    """Run the game via `xodus-cli run` with its compatibility entry applied (Wine build,
    prefix, environment, executable). Blocks until it exits. Returns (code, tail).

    on_start(popen) is called once the process is spawned, so a caller can stop it.

    A title linked to a game inside a third-party launcher (meta status "launcher") is started
    through that launcher instead: see launch_launcher_game()."""
    if launcher_meta(game_dir):
        return launch_launcher_game(cfg, game_dir, log, on_start)
    entry = compat_entry(game_store_id(game_dir))
    wine = resolve_wine(cfg, entry)
    if not wine or not Path(wine).exists():
        return 1, "Wine is not set up yet - open Xodus Launcher and use Settings > Set up Wine."
    prefix = game_prefix(cfg, game_dir, entry)
    launch = entry.get("launch") or {}
    # anti-cheat: the single-player bypass (the real executable instead of the EAC bootstrapper)
    # stays the default. Only a title whose entry says online is supported, run by a
    # Proton-derived build with Steam's runtime present, starts the protected bootstrapper
    # with PROTON_*_RUNTIME in the environment; WineGDK has no such hooks, so under it the
    # runtime is never exported and the bootstrapper never forced
    ac, ac_env = anticheat_status(game_dir, entry, wine)
    eac_bypass = launch.get("eac_bypass")
    protected = ac["online"] == "supported" and bool(ac_env)
    if protected and eac_bypass is None:
        eac_bypass = False
    bypass = ac.get("bypass") if not protected and eac_bypass is not False else None
    if bypass and not (Path(game_dir) / str(bypass).replace("\\", "/")).exists():
        bypass = None
    exe = launch.get("executable") or bypass or find_executable(game_dir, eac_bypass)
    candidates = exe_candidates(exe) if exe else [None]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / (safe_dirname(Path(game_dir).name) + ".log")
    env = wine_env(cfg, wine, entry, prefix)
    for k, v in ac_env.items():  # an explicit env entry (title or user) keeps precedence
        env.setdefault(k, v)
    if ac_env:  # what the proton script adds for BattlEye (the bridge builtin); EAC needs none
        try:
            ac_ov = thirdparty_module().anticheat_dll_overrides(ac) or {}
        except Exception:  # noqa: BLE001
            ac_ov = {}
        if ac_ov:
            env["WINEDLLOVERRIDES"] = ";".join(
                p for p in [env.get("WINEDLLOVERRIDES", "")] + [f"{k}={v}" for k, v in ac_ov.items()] if p)
    header = [f"compat: {entry['id'] or 'unknown title'} status={entry['status']} wine={wine} "
              f"prefix={prefix}" + (" (user override)" if entry.get("_user") else ""),
              "compat: " + anticheat_summary(ac)]
    bypassed = bool(launch.get("executable") or bypass or
                    (eac_bypass is not False and eac_real_executable(game_dir)))
    if protected:
        header.append("compat: the protected bootstrapper runs with Steam's anti-cheat runtime "
                      f"({', '.join(ac_env)}): online modes should work")
    elif ac["online"] == "unsupported":
        if bypassed:
            header.append("compat: online modes are unavailable - the game is started past its "
                          "anti-cheat (single-player bypass)")
        else:
            header.append("compat: WARNING no single-player bypass is known for this anti-cheat; "
                          "the game may not start")
    elif ac.get("kind") in ("eac", "battleye") and bypassed:
        header.append("compat: online support is not verified for this title - the game is started "
                      "past its anti-cheat (single-player bypass)" +
                      ("; the runtime is exported, set launch.eac_bypass to false in the title's "
                       "override to try the protected bootstrapper" if ac_env else ""))
    if entry["status"] == "broken":
        header.append("compat: this title is known not to work yet. " + (entry.get("notes") or ""))
    need = (entry.get("runtime") or {}).get("min_winegdk") or ""
    have = winegdk_release(wine)
    if _release_date(need) and _release_date(have) and _release_date(have) < _release_date(need):
        header.append(f"compat: WARNING this title was tested with WineGDK {need} or newer; "
                      f"{have} is installed")
    if launch.get("args"):
        header.append("compat: launch.args are recorded but xodus-cli run cannot pass them yet")
    for line in header:
        log(line)
    if prefix != cfg["prefix"] and not (Path(prefix) / "system.reg").exists():
        prepare_prefix(cfg, wine, prefix, log)
    if entry.get("registry") or entry.get("winetricks"):
        apply_prefix_tweaks(cfg, wine, prefix, entry, log)
    cwd = game_dir
    if launch.get("working_dir") and (Path(game_dir) / launch["working_dir"]).is_dir():
        cwd = str(Path(game_dir) / launch["working_dir"])
    if ensure_xodus_service(log):
        threading.Thread(target=prewarm_identity, args=(game_dir, log), daemon=True).start()
    code, tail = 1, ""
    for cand in candidates:
        cmd = [cfg["xodus_cli"], "run", str(game_dir), wine, "-m", cfg["market"]]
        if cand:
            cmd += ["--exe", cand]
        log("$ " + " ".join(cmd))
        with open(logfile, "w") as lf:
            lf.write("\n".join(header + ["$ " + " ".join(cmd)]) + "\n")
            lf.flush()
            # new session so the whole wine process tree can be signalled as a group
            p = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT,
                                 cwd=cwd, start_new_session=True)
            if on_start:
                on_start(p)
            p.wait()
        code = p.returncode
        tail = "\n".join(logfile.read_text(errors="replace").splitlines()[-25:])
        if "Could not find .exe" not in tail:
            break
    return code, tail


# --------------------------------------------------------------------------
# Steam shortcuts (binary VDF)
# --------------------------------------------------------------------------

def _vdf_read(data, i=0):
    obj = {}
    while i < len(data):
        t = data[i]
        i += 1
        if t == 0x08:
            return obj, i
        end = data.index(b"\x00", i)
        key = data[i:end].decode("utf-8", "replace")
        i = end + 1
        if t == 0x00:
            obj[key], i = _vdf_read(data, i)
        elif t == 0x01:
            end = data.index(b"\x00", i)
            obj[key] = data[i:end].decode("utf-8", "replace")
            i = end + 1
        elif t == 0x02:
            obj[key] = struct.unpack("<i", data[i:i + 4])[0]
            i += 4
        else:
            raise ValueError(f"unsupported VDF type {t}")
    return obj, i


def _vdf_write(obj):
    out = b""
    for k, v in obj.items():
        kb = k.encode() + b"\x00"
        if isinstance(v, dict):
            out += b"\x00" + kb + _vdf_write(v) + b"\x08"
        elif isinstance(v, int):
            out += b"\x02" + kb + struct.pack("<i", v)
        else:
            out += b"\x01" + kb + str(v).encode() + b"\x00"
    return out


def steam_user_dirs():
    return [d for d in (HOME / ".steam/steam/userdata").glob("*") if d.name.isdigit() and d.name != "0"]


def steam_running():
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0


def add_steam_shortcut(game, poster_file=None, hero_file=None):
    exe = f'"{SELF_CMD}"'
    name = game["title"]
    appid_u = (zlib.crc32((exe + name).encode()) & 0xFFFFFFFF) | 0x80000000
    appid = struct.unpack("<i", struct.pack("<I", appid_u))[0]
    users = steam_user_dirs()
    if not users:
        raise RuntimeError("No Steam user found in ~/.steam/steam/userdata")
    for user in users:
        cfgdir = user / "config"
        cfgdir.mkdir(parents=True, exist_ok=True)
        vdf_path = cfgdir / "shortcuts.vdf"
        root = {"shortcuts": {}}
        if vdf_path.exists() and vdf_path.stat().st_size > 0:
            shutil.copy2(vdf_path, vdf_path.with_suffix(".vdf.bak"))
            root, _ = _vdf_read(vdf_path.read_bytes())
            root.setdefault("shortcuts", {})
        shortcuts = root["shortcuts"]
        entries = [v for v in shortcuts.values() if v.get("appid") != appid]
        entries.append({
            "appid": appid,
            "AppName": name,
            "Exe": exe,
            "StartDir": f'"{HOME}"',
            "icon": str(ICON_PATH),
            "ShortcutPath": "",
            "LaunchOptions": f"--play {game['id']}",
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "OpenVR": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "DevkitOverrideAppID": 0,
            "LastPlayTime": 0,
            "FlatpakAppID": "",
            "tags": {"0": "Game Pass"},
        })
        root["shortcuts"] = {str(i): e for i, e in enumerate(entries)}
        vdf_path.write_bytes(_vdf_write(root) + b"\x08")
        grid = cfgdir / "grid"
        grid.mkdir(exist_ok=True)
        if poster_file and Path(poster_file).exists():
            shutil.copy2(poster_file, grid / f"{appid_u}p.jpg")
        if hero_file and Path(hero_file).exists():
            shutil.copy2(hero_file, grid / f"{appid_u}_hero.jpg")
    return len(users)


# --------------------------------------------------------------------------
# headless --play
# --------------------------------------------------------------------------

def cli_play(product_id):
    cfg = load_config()
    game = find_installed(cfg, product_id)
    if not game:
        print(f"Game {product_id} is not installed in {cfg['games_dir']}", file=sys.stderr)
        return 1
    code, tail = launch_game(cfg, game["dir"])
    if code:
        print(tail, file=sys.stderr)
    return code


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(install_id=None):
    from PySide6.QtCore import (QObject, QRect, QSize, QSortFilterProxyModel, Qt, QThread, QUrl,
                                Signal, QStandardPaths)
    from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap,
                               QStandardItem, QStandardItemModel, QTextCursor)
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest
    from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                                   QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                                   QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
                                   QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                                   QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget,
                                   QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

    ROLE_GAME = Qt.UserRole + 1
    ROLE_STATUS = Qt.UserRole + 2  # compatibility status, for the catalog filter

    def badge_icon(pm, status, show, tag=""):
        """Poster cropped to the grid cell, with the compatibility status painted on it and, for
        titles delivered through a third-party launcher, a "via <Launcher>" tag at the top."""
        pm = pm.scaled(150, 225, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        pm = pm.copy((pm.width() - 150) // 2, (pm.height() - 225) // 2, 150, 225)
        if not show and not tag:
            return QIcon(pm)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        p.setFont(font)
        if show:
            label = STATUS_LABELS.get(status, status)
            rect = QRect(6, 225 - 22 - 6, p.fontMetrics().horizontalAdvance(label) + 14, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(STATUS_COLORS.get(status, STATUS_COLORS["untested"])))
            p.drawRoundedRect(rect, 5, 5)
            p.setPen(QColor("white"))
            p.drawText(rect, Qt.AlignCenter, label)
        if tag:
            label = p.fontMetrics().elidedText(tag, Qt.ElideRight, 150 - 12 - 14)
            rect = QRect(6, 6, p.fontMetrics().horizontalAdvance(label) + 14, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(20, 20, 20, 200))
            p.drawRoundedRect(rect, 5, 5)
            p.setPen(QColor("white"))
            p.drawText(rect, Qt.AlignCenter, label)
        p.end()
        return QIcon(pm)

    class Images(QObject):
        """Async image loader with on-disk cache."""
        loaded = Signal(str, QPixmap)

        def __init__(self):
            super().__init__()
            self.nam = QNetworkAccessManager(self)
            cache = QNetworkDiskCache(self)
            cache.setCacheDirectory(str(CACHE_DIR / "images"))
            cache.setMaximumCacheSize(500 * 1024 * 1024)
            self.nam.setCache(cache)
            self.mem = {}
            self.pending = set()

        def get(self, url):
            if not url:
                return None
            if url in self.mem:
                return self.mem[url]
            if url not in self.pending:
                self.pending.add(url)
                req = QNetworkRequest(QUrl(url))
                req.setAttribute(QNetworkRequest.CacheLoadControlAttribute,
                                 QNetworkRequest.PreferCache)
                reply = self.nam.get(req)
                reply.finished.connect(lambda r=reply, u=url: self._done(r, u))
            return None

        def _done(self, reply, url):
            self.pending.discard(url)
            pm = QPixmap()
            pm.loadFromData(bytes(reply.readAll()))
            reply.deleteLater()
            if not pm.isNull():
                self.mem[url] = pm
                self.loaded.emit(url, pm)

        def save(self, url, path):
            pm = self.mem.get(url)
            if pm:
                pm.save(str(path), "JPG", 92)
                return True
            return False

    class Worker(QThread):
        log = Signal(str, bool)
        progress = Signal(int, int)
        done = Signal(bool, str, object)

        def __init__(self, fn):
            super().__init__()
            self.fn = fn

        def run(self):
            try:
                res = self.fn(self)
                self.done.emit(True, "", res)
            except Exception as e:  # noqa: BLE001 - surface any failure in the UI
                self.done.emit(False, str(e), None)

    class DownloadJob(QThread):
        progress = Signal(str, int, str)
        log = Signal(str)
        finished_job = Signal(bool, str)

        def __init__(self, cfg, game, dest):
            super().__init__()
            self.cfg, self.game, self.dest = cfg, game, dest
            self.cancel = threading.Event()

        def run(self):
            meta = {k: self.game.get(k) for k in ("id", "title", "publisher", "poster",
                                                  "poster_big", "hero", "size", "genres")}
            meta["status"] = "downloading"
            write_meta(self.dest, meta)

            def on_progress(stage, pct, done, total, rate):
                text = f"{stage}  {done} / {total}" + (f"  ({rate})" if rate else "")
                self.progress.emit(stage, pct, text)

            ok, msg = run_download(self.cfg, self.game["id"], self.dest, on_progress,
                                   self.log.emit, self.cancel)
            meta["status"] = "installed" if ok else "incomplete"
            meta["exe"] = find_executable(self.dest) if ok else None
            write_meta(self.dest, meta)
            self.finished_job.emit(ok, msg)

    class LaunchJob(QThread):
        finished_job = Signal(int, str)

        def __init__(self, cfg, game_dir):
            super().__init__()
            self.cfg, self.game_dir = cfg, game_dir
            self.proc = None
            self.stopped = False

        def run(self):
            code, tail = launch_game(self.cfg, self.game_dir, log=lambda s: None,
                                     on_start=lambda p: setattr(self, "proc", p))
            if self.stopped:
                tail = "Stopped."
            self.finished_job.emit(code, tail)

        def stop(self):
            """Force-stop the running game (used by the Stop button)."""
            self.stopped = True
            p = self.proc
            if p and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            # bring down any detached wine processes in the game's prefix (or in its
            # third-party launcher's prefix)
            stop_game(self.cfg, self.game_dir)
            if p and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    class LauncherInstallJob(QThread):
        """ThirdPartyLauncher.install() off the GUI thread: stage/progress/log signals, a cancel
        event, and any failure as a readable message."""
        progress = Signal(str, object, str)  # stage, percent (None = busy), text
        log = Signal(str)
        finished_job = Signal(bool, str)

        def __init__(self, cfg, lid):
            super().__init__()
            self.cfg, self.lid = cfg, lid
            self.cancel = threading.Event()

        def run(self):
            try:
                tpl = thirdparty_module().ThirdPartyLauncher(
                    self.lid, self.cfg, log=lambda s, *a, **k: self.log.emit(str(s)))
                tpl.install(progress=lambda stage, pct=None, text="": self.progress.emit(
                    str(stage), pct, str(text)), cancel_event=self.cancel)
                if not tpl.is_installed():
                    raise RuntimeError(f"{tpl.name} did not install: its executable is missing "
                                       f"from {tpl.prefix}")
            except Exception as e:  # noqa: BLE001 - shown in the dialog
                self.finished_job.emit(False, "Cancelled" if self.cancel.is_set() else str(e))
                return
            self.finished_job.emit(True, "")

    class FilterModel(QSortFilterProxyModel):
        def __init__(self):
            super().__init__()
            self.text, self.genre, self.status = "", "", ""

        def filterAcceptsRow(self, row, parent):
            idx = self.sourceModel().index(row, 0, parent)
            g = idx.data(ROLE_GAME)
            if not g:
                return False
            if self.text and self.text not in (g["title"] + " " + g.get("publisher", "")).lower():
                return False
            if self.genre and self.genre not in (g.get("genres") or []):
                return False
            if self.status and idx.data(ROLE_STATUS) != self.status:
                return False
            return True

    class GameGrid(QListView):
        def __init__(self):
            super().__init__()
            self.setViewMode(QListView.IconMode)
            self.setIconSize(QSize(150, 225))
            self.setGridSize(QSize(176, 290))
            self.setResizeMode(QListView.Adjust)
            self.setMovement(QListView.Static)
            self.setWordWrap(True)
            self.setUniformItemSizes(True)
            self.setSpacing(6)
            self.setEditTriggers(QListView.NoEditTriggers)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
            self.verticalScrollBar().setSingleStep(40)
            self.setStyleSheet("QListView QScrollBar:vertical { width: 16px; }"
                               "QListView QScrollBar::handle:vertical { min-height: 40px; }")

    class DownloadRow(QWidget):
        def __init__(self, title):
            super().__init__()
            lay = QVBoxLayout(self)
            top = QHBoxLayout()
            self.title = QLabel(f"<b>{title}</b>")
            self.status = QLabel("Queued")
            self.cancel_btn = QPushButton("Cancel")
            top.addWidget(self.title, 1)
            top.addWidget(self.status)
            top.addWidget(self.cancel_btn)
            lay.addLayout(top)
            self.bar = QProgressBar()
            self.bar.setRange(0, 100)
            lay.addWidget(self.bar)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumBlockCount(400)
            self.log.setFixedHeight(90)
            lay.addWidget(self.log)

    class SettingsDialog(QDialog):
        def __init__(self, parent, cfg):
            super().__init__(parent)
            self.setWindowTitle("Settings")
            self.cfg = cfg
            form = QFormLayout(self)
            self.games_dir = QLineEdit(cfg["games_dir"])
            browse = QPushButton("Browse…")
            browse.clicked.connect(self.pick_dir)
            row = QHBoxLayout()
            row.addWidget(self.games_dir)
            row.addWidget(browse)
            form.addRow("Games folder", row)
            self.market = QLineEdit(cfg["market"])
            form.addRow("Market (country code)", self.market)
            self.language = QLineEdit(cfg["language"])
            form.addRow("Catalog language", self.language)
            self.parallel = QSpinBox()
            self.parallel.setRange(1, 32)
            self.parallel.setValue(int(cfg.get("parallel", 4)))
            form.addRow("Parallel download streams", self.parallel)
            self.wine = QLineEdit(cfg.get("wine", ""))
            self.wine.setPlaceholderText("auto: xodus Proton build")
            form.addRow("Wine binary", self.wine)
            self.prefix = QLineEdit(cfg["prefix"])
            form.addRow("Wine prefix", self.prefix)
            form.addRow(self.build_launchers())
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            form.addRow(bb)

        def pick_dir(self):
            d = QFileDialog.getExistingDirectory(self, "Games folder", self.games_dir.text())
            if d:
                self.games_dir.setText(d)

        # ---------- third-party launchers ----------
        def build_launchers(self):
            """The "Third-party launchers" group: every recipe with its state and Set up / Open /
            Remove prefix, the GE-Proton build they run with, and Steam's anti-cheat runtimes."""
            self.workers = []
            self.procs = []
            self.launcher_rows = {}
            self.launchers = launchers_db()
            self.tp = None
            box = QGroupBox("Third-party launchers")
            grid = QGridLayout(box)
            try:
                self.tp = thirdparty_module()
            except RuntimeError as e:
                lbl = QLabel(str(e))
                lbl.setWordWrap(True)
                grid.addWidget(lbl, 0, 0, 1, 5)
                return box
            order = sorted(self.launchers, key=lambda l: launcher_name(l, self.launchers).lower())
            for row, lid in enumerate(order):
                state = QLabel()
                setup = QPushButton("Set up")
                setup.clicked.connect(lambda _=False, l=lid: self.setup_launcher(l))
                open_btn = QPushButton("Open")
                open_btn.clicked.connect(lambda _=False, l=lid: self.open_launcher(l))
                remove = QPushButton("Remove prefix")
                remove.clicked.connect(lambda _=False, l=lid: self.remove_prefix(l))
                grid.addWidget(QLabel(f"<b>{html.escape(launcher_name(lid, self.launchers))}</b>"),
                               row, 0)
                grid.addWidget(state, row, 1)
                for col, b in enumerate((setup, open_btn, remove), 2):
                    grid.addWidget(b, row, col)
                self.launcher_rows[lid] = (state, setup, open_btn, remove)
            row = len(order)
            self.ge_label = QLabel()
            self.ge_btn = QPushButton("Update")
            self.ge_btn.clicked.connect(self.update_ge_proton)
            grid.addWidget(QLabel("GE-Proton"), row, 0)
            grid.addWidget(self.ge_label, row, 1, 1, 2)
            grid.addWidget(self.ge_btn, row, 3)
            self.ac_label = QLabel()
            self.ac_label.setWordWrap(True)
            self.ac_btn = QPushButton("Install via Steam")
            self.ac_btn.clicked.connect(self.install_anticheat)
            grid.addWidget(QLabel("Anti-cheat runtimes"), row + 1, 0)
            grid.addWidget(self.ac_label, row + 1, 1, 1, 2)
            grid.addWidget(self.ac_btn, row + 1, 3)
            self.refresh_launchers()
            return box

        def launcher(self, lid):
            return self.tp.ThirdPartyLauncher(lid, self.cfg)

        def refresh_launchers(self):
            for lid, (state, setup, open_btn, remove) in self.launcher_rows.items():
                try:
                    tpl = self.launcher(lid)
                    installed = tpl.is_installed()
                    has_prefix = Path(tpl.prefix).exists()
                except Exception as e:  # noqa: BLE001 - a broken recipe must not break Settings
                    state.setText(f"error: {e}")
                    installed = has_prefix = False
                else:
                    state.setText("Installed" if installed else "Not installed")
                setup.setText("Sign-in guide" if installed else "Set up")
                open_btn.setEnabled(installed)
                remove.setEnabled(has_prefix)
            try:
                tag = self.tp.ge_proton_installed() or ""
            except Exception as e:  # noqa: BLE001
                tag = f"error: {e}"
            self.ge_label.setText(tag or "not installed")
            self.ge_btn.setText("Update" if tag else "Install")
            rt = {}
            try:
                rt = dict(self.tp.anticheat_runtimes() or {})
            except Exception as e:  # noqa: BLE001
                self.ac_label.setText(f"error: {e}")
            else:
                self.ac_label.setText(" · ".join(
                    f"{ANTICHEAT_LABELS[k]}: " + (f"✔ {rt[k]}" if rt.get(k) else "not installed")
                    for k in ("eac", "battleye")))
            self.ac_btn.setEnabled(not all(rt.get(k) for k in ("eac", "battleye")))

        def setup_launcher(self, lid):
            try:
                installed = self.launcher(lid).is_installed()
                dlg = LauncherSetupDialog(self, self.cfg, lid, install=not installed)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME,
                                    f"Cannot set up {launcher_name(lid, self.launchers)}:\n{e}")
                return
            dlg.exec()
            self.refresh_launchers()

        def open_launcher(self, lid):
            try:
                tpl = self.launcher(lid)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Cannot open {lid}:\n{e}")
                return
            w = Worker(lambda _w: tpl.open())  # .wine() may fetch GE-Proton: off the GUI thread

            def done(ok, err, proc):
                if ok:
                    self.procs.append(proc)
                else:
                    QMessageBox.warning(self, APP_NAME, f"Could not open {tpl.name}:\n{err}")

            w.done.connect(done)
            self.workers.append(w)
            w.start()

        def remove_prefix(self, lid):
            try:
                tpl = self.launcher(lid)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Cannot remove {lid}:\n{e}")
                return
            if QMessageBox.question(
                    self, APP_NAME, f"Delete the {tpl.name} prefix?\n\n{tpl.prefix}\n\n"
                    "This removes the launcher and every game installed in it.") != QMessageBox.Yes:
                return
            state, *buttons = self.launcher_rows[lid]
            state.setText("Removing…")
            for b in buttons:
                b.setEnabled(False)
            w = Worker(lambda _w: tpl.remove_prefix())

            def done(ok, err, _):
                if not ok:
                    QMessageBox.warning(self, APP_NAME, f"Could not remove the prefix:\n{err}")
                self.refresh_launchers()

            w.done.connect(done)
            self.workers.append(w)
            w.start()

        def update_ge_proton(self):
            dlg = LogDialog(self, "GE-Proton")
            dlg.add("Fetching the latest GE-Proton release (GloriousEggroll/proton-ge-custom on "
                    "GitHub, checksum-verified); third-party launchers and their games run with "
                    "it.\n")
            try:  # "Update" with a build installed: ask GitHub for a newer tag ("Install": any)
                latest = bool(self.tp.ge_proton_installed())
            except Exception:  # noqa: BLE001
                latest = False

            def job(w):
                return self.tp.ensure_ge_proton(
                    self.cfg, log=lambda s, *a, **k: w.log.emit(str(s), False),
                    progress=lambda stage, pct=None, text="": w.log.emit(
                        f"{stage}: {text}" + (f" {int(pct)}%" if pct is not None else ""),
                        pct is not None),
                    latest=latest)

            w = Worker(job)
            w.log.connect(dlg.add)

            def done(ok, err, wine):
                dlg.close_btn.setEnabled(True)
                dlg.add(f"\nGE-Proton ready: {wine}" if ok else "\nERROR: " + err)
                self.refresh_launchers()

            w.done.connect(done)
            self.workers.append(w)
            w.start()
            dlg.exec()

        def install_anticheat(self):
            """Open Steam on the Proton anti-cheat runtimes that are missing (steam:// URLs)."""
            try:
                rt = dict(self.tp.anticheat_runtimes() or {})
            except Exception:  # noqa: BLE001
                rt = {}
            opened = []
            for key, url in self.tp.STEAM_INSTALL_URLS.items():
                if not rt.get(key):
                    QDesktopServices.openUrl(QUrl(url))
                    opened.append(ANTICHEAT_LABELS.get(key, key))
            if opened:
                QMessageBox.information(
                    self, APP_NAME, "Steam opens the install page of: " + ", ".join(opened) +
                    ".\n\nThese are Valve's free Proton anti-cheat runtimes. Once Steam has "
                    "installed them the launcher finds them by itself.")
            self.refresh_launchers()

        def apply(self):
            self.cfg.update({
                "games_dir": self.games_dir.text().strip(),
                "market": self.market.text().strip().upper() or "TR",
                "language": self.language.text().strip().lower() or "en-us",
                "parallel": self.parallel.value(),
                "wine": self.wine.text().strip(),
                "prefix": self.prefix.text().strip(),
            })
            save_config(self.cfg)

    class ReportDialog(QDialog):
        """Asks how far a game got before a compatibility report is written."""
        def __init__(self, parent, game, entry):
            super().__init__(parent)
            self.setWindowTitle(f"Compatibility report: {game.get('title', '')}")
            form = QFormLayout(self)
            self.status = QComboBox()
            for s in COMPAT_STATUSES[1:]:
                self.status.addItem(STATUS_LABELS[s], s)
            if entry.get("status") in COMPAT_STATUSES[1:]:
                self.status.setCurrentIndex(COMPAT_STATUSES[1:].index(entry["status"]))
            form.addRow("How far does it get?", self.status)
            self.notes = QPlainTextEdit()
            self.notes.setPlaceholderText("What works, what breaks, workarounds…")
            form.addRow("Notes", self.notes)
            info = QLabel("The report (system and GPU info, the Wine build, this title's "
                          "compatibility entry and the last 200 lines of its log with anything "
                          "credential-like removed) is saved locally, and a prefilled GitHub "
                          "issue opens in your browser so you can attach it.")
            info.setWordWrap(True)
            form.addRow(info)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            form.addRow(bb)

    class LogDialog(QDialog):
        def __init__(self, parent, title):
            super().__init__(parent)
            self.setWindowTitle(title)
            self.resize(700, 400)
            lay = QVBoxLayout(self)
            self.text = QPlainTextEdit()
            self.text.setReadOnly(True)
            lay.addWidget(self.text)
            self.close_btn = QPushButton("Close")
            self.close_btn.setEnabled(False)
            self.close_btn.clicked.connect(self.accept)
            lay.addWidget(self.close_btn)

        def add(self, line, replace=False):
            if replace:
                cur = self.text.textCursor()
                cur.movePosition(QTextCursor.MoveOperation.End)
                cur.select(QTextCursor.SelectionType.BlockUnderCursor)
                if cur.selectedText().strip().endswith(("B", "?", "%")):
                    cur.removeSelectedText()
            self.text.appendPlainText(line)

    class LauncherSetupDialog(QDialog):
        """Installs a third-party launcher (its publisher's official installer, downloaded on this
        machine, into the launcher's own GE-Proton prefix) with stage, progress and log, then
        shows the sign-in guide. install=False goes straight to the guide for a launcher that
        is already installed. .ok says whether the launcher is installed when the dialog closes."""

        def __init__(self, parent, cfg, lid, install=True):
            super().__init__(parent)
            self.cfg, self.lid = cfg, lid
            self.launcher = thirdparty_module().ThirdPartyLauncher(lid, cfg)
            self.name = self.launcher.name
            self.job = None
            self.ok = False
            self.last_stage = None
            self.procs = []
            self.workers = []
            self.setWindowTitle(f"Set up {self.name}")
            self.resize(680, 480)
            lay = QVBoxLayout(self)
            self.pages = QStackedWidget()
            lay.addWidget(self.pages)
            # page 0: install
            page = QWidget()
            pl = QVBoxLayout(page)
            intro = QLabel(f"<b>{html.escape(self.name)}</b> is downloaded from its publisher's "
                           "official installer and installed into its own Wine prefix (GE-Proton), "
                           "shared by all of its games.<br>Prefix: "
                           f"{html.escape(str(self.launcher.prefix))}")
            intro.setWordWrap(True)
            pl.addWidget(intro)
            self.stage = QLabel("Starting…")
            self.stage.setWordWrap(True)
            pl.addWidget(self.stage)
            self.bar = QProgressBar()
            self.bar.setRange(0, 0)
            pl.addWidget(self.bar)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumBlockCount(2000)
            pl.addWidget(self.log, 1)
            row = QHBoxLayout()
            row.addStretch(1)
            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.clicked.connect(self.cancel)
            self.close_btn = QPushButton("Close")
            self.close_btn.setEnabled(False)
            self.close_btn.clicked.connect(self.reject)
            row.addWidget(self.cancel_btn)
            row.addWidget(self.close_btn)
            pl.addLayout(row)
            self.pages.addWidget(page)
            # page 1: sign-in guide
            page = QWidget()
            pl = QVBoxLayout(page)
            pl.addWidget(QLabel(f"<h3>{html.escape(self.name)} is ready - sign in</h3>"))
            # the numbered steps scroll and wrap, URLs open in the browser
            self.steps = QTextBrowser()
            self.steps.setOpenExternalLinks(True)
            self.steps.setHtml(self.guide_html())
            pl.addWidget(self.steps, 1)
            self.note = QLabel(self.guide_note())
            self.note.setWordWrap(True)
            self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)
            pl.addWidget(self.note)
            row = QHBoxLayout()
            row.addStretch(1)
            self.open_btn = QPushButton(f"Open {self.name}")
            self.open_btn.clicked.connect(self.open_launcher)
            self.done_btn = QPushButton("Done")
            self.done_btn.setDefault(True)
            self.done_btn.clicked.connect(self.accept)
            row.addWidget(self.open_btn)
            row.addWidget(self.done_btn)
            pl.addLayout(row)
            self.pages.addWidget(page)
            if install:
                self.start_install()
            else:
                self.ok = True
                self.pages.setCurrentIndex(1)

        @staticmethod
        def linkify(text):
            """html-escaped text with its http(s) URLs as links."""
            out, pos = [], 0
            for m in re.finditer(r"https?://[^\s<>\"']+", text):
                url = m.group(0).rstrip(".,;:)")
                out.append(html.escape(text[pos:m.start()]))
                out.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>')
                pos = m.start() + len(url)
            out.append(html.escape(text[pos:]))
            return "".join(out)

        def guide_html(self):
            """The recipe's sign-in steps as a numbered list."""
            steps = [str(s) for s in self.launcher.signin_steps()]
            return ("<ol>" + "".join(f"<li style='margin-bottom: 8px'>{self.linkify(s)}</li>"
                                     for s in steps) + "</ol>")

        def guide_note(self):
            """How the entitlement reaches this launcher (the recipe's own words), and the Link
            step for launchers that list the Game Pass titles in their library."""
            try:
                summary = str(self.launcher.signin_summary() or "")
            except Exception:  # noqa: BLE001 - an older recipe / module
                summary = ""
            try:
                library = bool(self.launcher.game_pass_library())
            except Exception:  # noqa: BLE001
                library = False
            text = html.escape(summary)
            if library:
                text += ((" " if text else "") + "After installing the game there, select the title in "
                         f"{APP_NAME} and use <b>Link installed game…</b> so that Play starts it.")
            return text

        def start_install(self):
            self.pages.setCurrentIndex(0)
            self.job = LauncherInstallJob(self.cfg, self.lid)
            self.job.progress.connect(self.on_progress)
            self.job.log.connect(self.log.appendPlainText)
            self.job.finished_job.connect(self.install_done)
            self.job.start()

        def on_progress(self, stage, pct, text):
            self.stage.setText(text or stage)
            if pct is None:
                self.bar.setRange(0, 0)
                self.bar.setFormat(stage)
            else:
                self.bar.setRange(0, 100)
                self.bar.setValue(max(0, min(int(pct), 100)))
                self.bar.setFormat(f"{stage}  %p%")
            if stage != self.last_stage:
                self.last_stage = stage
                self.log.appendPlainText(f"== {stage}" + (f": {text}" if text else ""))

        def install_done(self, ok, err):
            self.bar.setRange(0, 100)
            self.cancel_btn.setEnabled(False)
            self.close_btn.setEnabled(True)
            if ok:
                self.ok = True
                self.bar.setValue(100)
                self.stage.setText(f"{self.name} is installed.")
                self.pages.setCurrentIndex(1)
                return
            self.stage.setText(f"<span style='color:{STATUS_COLORS['broken']}'><b>Failed:</b> "
                               f"{html.escape(err)}</span>")
            self.log.appendPlainText("ERROR: " + err)

        def cancel(self):
            if self.job and self.job.isRunning():
                self.job.cancel.set()
                self.cancel_btn.setEnabled(False)
                self.stage.setText("Cancelling…")
            else:
                self.reject()

        def reject(self):
            if self.job and self.job.isRunning():  # never destroy a running QThread: cancel first
                self.cancel()
                return
            super().reject()

        def open_launcher(self):
            """Open <Launcher>: off the GUI thread, because open() -> wine() may download
            GE-Proton first. The parent (Main / Settings) keeps the worker and the process."""
            parent = self.parent()
            if parent is not None and hasattr(parent, "open_launcher"):
                parent.open_launcher(self.lid)
                return
            w = Worker(lambda _w: self.launcher.open())

            def done(ok, err, proc):
                if ok:
                    self.procs.append(proc)
                else:
                    QMessageBox.warning(self, APP_NAME, f"Could not open {self.name}:\n{err}")

            w.done.connect(done)
            self.workers.append(w)
            w.start()

    class LinkGameDialog(QDialog):
        """Which game inside a third-party launcher a Game Pass title is (Link installed game…)."""

        def __init__(self, parent, title, launcher, games):
            super().__init__(parent)
            self.setWindowTitle(f"Link installed game: {title}")
            self.resize(520, 420)
            lay = QVBoxLayout(self)
            info = QLabel(f"Which game in <b>{html.escape(launcher)}</b> is "
                          f"<b>{html.escape(title)}</b>? Pick it among the games installed there, "
                          "or type its id when the launcher lists it differently.")
            info.setWordWrap(True)
            lay.addWidget(info)
            self.list = QListWidget()
            key = re.sub(r"[^a-z0-9]", "", title.lower())
            for g in games:
                it = QListWidgetItem(f"{g.get('name') or g.get('id')}   ({g.get('id')})")
                it.setData(Qt.UserRole, dict(g))
                self.list.addItem(it)
                gkey = re.sub(r"[^a-z0-9]", "", str(g.get("name") or "").lower())
                if key and gkey and (key in gkey or gkey in key) and not self.list.currentItem():
                    self.list.setCurrentItem(it)
            self.list.itemDoubleClicked.connect(lambda _it: self.accept())
            lay.addWidget(self.list, 1)
            if not games:
                empty = QLabel(f"<i>{html.escape(launcher)} has no installed games yet: install "
                               "the title there first, or enter its id below.</i>")
                empty.setWordWrap(True)
                lay.addWidget(empty)
            self.game_id = QLineEdit()
            self.game_id.setPlaceholderText("game id (only when the game is not listed above)")
            lay.addWidget(self.game_id)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

        def chosen(self):
            """{id, name, dir} of the pick, or None."""
            typed = self.game_id.text().strip()
            if typed:
                return {"id": typed, "name": "", "dir": ""}
            it = self.list.currentItem()
            return dict(it.data(Qt.UserRole)) if it else None

    class Main(QMainWindow):
        def __init__(self):
            super().__init__()
            self.cfg = load_config()
            self.compat = load_compat_db()
            self.launchers = launchers_db()  # {id: recipe}: names for tags and buttons
            self.launcher_procs = []
            self.images = Images()
            self.images.loaded.connect(self.on_image)
            self.catalog = []
            self.queue = []
            self.active = None
            self.rows = {}
            self.workers = []
            self.current = None
            self.pending_install = None
            self.play_job = None
            self.setWindowTitle(APP_NAME)
            self.setWindowIcon(QIcon(str(ICON_PATH)))
            self.resize(1280, 820)

            central = QWidget()
            outer = QVBoxLayout(central)
            top = QHBoxLayout()
            self.account = QLabel()
            self.login_btn = QPushButton()
            self.login_btn.clicked.connect(self.toggle_login)
            settings_btn = QPushButton("Settings")
            settings_btn.clicked.connect(self.open_settings)
            wine_btn = QPushButton("Set up Wine")
            wine_btn.clicked.connect(self.setup_wine)
            self.wine_btn = wine_btn
            top.addWidget(QLabel(f"<h2>{APP_NAME}</h2>"))
            top.addStretch(1)
            top.addWidget(self.account)
            top.addWidget(self.login_btn)
            top.addWidget(wine_btn)
            top.addWidget(settings_btn)
            outer.addLayout(top)

            split = QSplitter()
            self.tabs = QTabWidget()
            split.addWidget(self.tabs)
            split.addWidget(self.build_details())
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 1)
            split.setSizes([900, 380])
            outer.addWidget(split, 1)
            self.setCentralWidget(central)

            # catalog tab
            cat = QWidget()
            cl = QVBoxLayout(cat)
            bar = QHBoxLayout()
            self.search = QLineEdit()
            self.search.setPlaceholderText("Search Game Pass games…")
            self.search.textChanged.connect(self.apply_filter)
            self.genre = QComboBox()
            self.genre.currentTextChanged.connect(self.apply_filter)
            self.status_filter = QComboBox()
            self.status_filter.addItem("Any compatibility")
            for s in COMPAT_STATUSES:
                self.status_filter.addItem(STATUS_LABELS[s], s)
            self.status_filter.currentIndexChanged.connect(self.apply_filter)
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(lambda: self.load_catalog(force=True))
            self.count = QLabel()
            bar.addWidget(self.search, 1)
            bar.addWidget(self.genre)
            bar.addWidget(self.status_filter)
            bar.addWidget(refresh)
            bar.addWidget(self.count)
            cl.addLayout(bar)
            self.cat_model = QStandardItemModel()
            self.cat_proxy = FilterModel()
            self.cat_proxy.setSourceModel(self.cat_model)
            self.cat_view = GameGrid()
            self.cat_view.setModel(self.cat_proxy)
            self.cat_view.selectionModel().currentChanged.connect(
                lambda cur, _: self.show_game(cur.data(ROLE_GAME)))
            cl.addWidget(self.cat_view)
            self.tabs.addTab(cat, "Game Pass")

            # library tab
            lib = QWidget()
            ll = QVBoxLayout(lib)
            self.lib_model = QStandardItemModel()
            self.lib_view = GameGrid()
            self.lib_view.setModel(self.lib_model)
            self.lib_view.selectionModel().currentChanged.connect(
                lambda cur, _: self.show_game(cur.data(ROLE_GAME)))
            self.lib_empty = QLabel("No games installed yet. Pick one in the Game Pass tab.")
            ll.addWidget(self.lib_empty)
            ll.addWidget(self.lib_view)
            self.tabs.addTab(lib, "Library")

            # downloads tab
            dl = QWidget()
            dlay = QVBoxLayout(dl)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            inner = QWidget()
            self.dl_layout = QVBoxLayout(inner)
            self.dl_layout.addStretch(1)
            scroll.setWidget(inner)
            dlay.addWidget(scroll)
            self.tabs.addTab(dl, "Downloads")

            self.refresh_account()
            self.refresh_wine_btn()
            self.refresh_library()
            self.load_catalog()
            self.refresh_compat()

        # ---------- details panel ----------
        def build_details(self):
            w = QWidget()
            w.setMinimumWidth(340)
            lay = QVBoxLayout(w)
            self.d_image = QLabel()
            self.d_image.setAlignment(Qt.AlignCenter)
            self.d_image.setMinimumHeight(190)
            self.d_title = QLabel("<h2>Select a game</h2>")
            self.d_title.setWordWrap(True)
            self.d_info = QLabel()
            self.d_info.setWordWrap(True)
            self.d_desc = QLabel()
            self.d_desc.setWordWrap(True)
            self.d_desc.setAlignment(Qt.AlignTop)
            self.d_status = QLabel()
            self.d_status.setWordWrap(True)
            self.d_compat = QLabel()
            self.d_compat.setWordWrap(True)
            btns = QVBoxLayout()
            self.b_install = QPushButton("Install")
            self.b_play = QPushButton("▶  Play")
            self.b_link = QPushButton("Link installed game…")
            self.b_steam = QPushButton("Add to Steam")
            self.b_folder = QPushButton("Open folder")
            self.b_report = QPushButton("Compatibility report…")
            self.b_remove = QPushButton("Uninstall")
            self.b_install.clicked.connect(self.install_current)
            self.b_play.clicked.connect(self.play_current)
            self.b_link.clicked.connect(self.link_current)
            self.b_steam.clicked.connect(self.steam_current)
            self.b_folder.clicked.connect(self.folder_current)
            self.b_report.clicked.connect(self.report_current)
            self.b_remove.clicked.connect(self.remove_current)
            for b in (self.b_play, self.b_install, self.b_link, self.b_steam, self.b_folder,
                      self.b_report, self.b_remove):
                b.setMinimumHeight(34)
                btns.addWidget(b)
                b.hide()
            desc_scroll = QScrollArea()
            desc_scroll.setWidgetResizable(True)
            desc_scroll.setWidget(self.d_desc)
            desc_scroll.setFrameShape(QScrollArea.NoFrame)
            lay.addWidget(self.d_image)
            lay.addWidget(self.d_title)
            lay.addWidget(self.d_info)
            lay.addWidget(self.d_status)
            lay.addWidget(self.d_compat)
            lay.addLayout(btns)
            lay.addWidget(desc_scroll, 1)
            return w

        def show_game(self, game):
            if not game:
                return
            self.current = game
            self.d_title.setText(f"<h2>{game['title']}</h2>")
            genres = ", ".join(game.get("genres") or [])
            self.d_info.setText(
                f"{game.get('publisher', '')}<br>{genres}<br>Download size: {human_size(game.get('size'))}")
            self.d_desc.setText(game.get("description", ""))
            pm = self.images.get(game.get("hero") or game.get("poster_big"))
            self.set_hero(pm)
            self.update_buttons()

        def set_hero(self, pm):
            if pm:
                self.d_image.setPixmap(pm.scaled(self.d_image.width() - 4, 200,
                                                 Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.d_image.clear()

        def update_buttons(self):
            g = self.current
            if not g:
                return
            inst = find_installed(self.cfg, g.get("id")) if g.get("id") else (g if g.get("dir") else None)
            busy = (self.active and self.active.game["id"] == g.get("id")) or \
                any(j.game["id"] == g.get("id") for j in self.queue)
            status = inst.get("status") if inst else None
            self.d_compat.setText(self.compat_text(g, inst))
            entry = compat_entry(g.get("id") or (game_store_id(inst["dir"]) if inst else ""),
                                 self.compat)
            lid = title_launcher(entry, g, inst, self.launchers)
            if lid:
                self.update_launcher_buttons(g, inst, entry, lid, busy)
                return
            self.b_link.hide()
            self.b_install.setVisible(not busy and status != "installed" and bool(g.get("id")))
            self.b_install.setText("Resume / repair download" if status == "incomplete" else "Install")
            self.b_play.setVisible(status == "installed")
            self.b_steam.setVisible(status == "installed" and bool(g.get("id")))
            self.b_folder.setVisible(bool(inst))
            self.b_report.setVisible(status == "installed")
            self.b_remove.setVisible(bool(inst) and not busy)
            self.b_remove.setText("Uninstall")
            if busy:
                self.d_status.setText("<i>Downloading – see the Downloads tab</i>")
            elif status == "installed":
                exe = find_executable(inst["dir"]) or "?"
                self.d_status.setText(f"<b>Installed</b> in {inst['dir']}<br>Executable: {exe}")
            elif status == "incomplete":
                self.d_status.setText("<b>Download incomplete</b>")
            else:
                self.d_status.setText("")

        def update_launcher_buttons(self, g, inst, entry, lid, busy):
            """Buttons for a title delivered through a third-party launcher: Set up / Open the
            launcher, Link installed game…, and Play once the game's id there is known (from the
            compatibility entry or the link)."""
            name = launcher_name(lid, self.launchers)
            have = self.launcher_installed(lid)
            linked = bool(inst) and inst.get("status") == "launcher"
            # the user's link beats the id the database pins
            gid = (inst.get("launcher_game_id") if linked else None) or \
                entry.get("launcher_game_id")
            installs = bool(((self.launchers.get(lid) or {}).get("launch") or {}).get("install_args"))
            self.b_install.setVisible(not busy)
            self.b_install.setText(f"Open {name}" if have else f"Set up {name}")
            self.b_play.setVisible(have and bool(gid) and not busy)
            self.b_link.setVisible(have and not busy)
            self.b_link.setText("Re-link installed game…" if linked else "Link installed game…")
            self.b_steam.setVisible(linked and bool(g.get("id")))
            if linked:
                self.b_folder.setVisible(bool(inst.get("launcher_game_dir")) and
                                         Path(inst["launcher_game_dir"]).is_dir())
            else:  # a leftover folder of a failed download: Open folder / Uninstall still work
                self.b_folder.setVisible(bool(inst))
            self.b_report.setVisible(linked)
            self.b_remove.setVisible(bool(inst) and not busy)
            self.b_remove.setText("Remove from library" if linked else "Uninstall")
            if busy:
                self.d_status.setText("<i>Downloading – see the Downloads tab</i>")
            elif linked:
                self.d_status.setText(
                    f"<b>Installed in {html.escape(name)}</b> as "
                    f"{html.escape(inst.get('launcher_game_name') or str(gid or '?'))} "
                    f"(id {html.escape(str(inst.get('launcher_game_id') or gid or '?'))})")
            elif inst:
                self.d_status.setText(
                    f"<i>An earlier download left {html.escape(inst['dir'])} behind: this title has "
                    f"no Store package and comes through {html.escape(name)}. Uninstall removes "
                    "the folder.</i>")
            elif have and gid and installs:
                self.d_status.setText(f"<i>Delivered through {html.escape(name)}; Play starts it "
                                      f"there, asking {html.escape(name)} to install it first "
                                      "when needed.</i>")
            elif have and gid:
                self.d_status.setText(f"<i>Delivered through {html.escape(name)}: install it there "
                                      "first, then Play starts it.</i>")
            elif have:
                self.d_status.setText(f"<i>Delivered through {html.escape(name)}: install it "
                                      "there, then use Link installed game…</i>")
            else:
                self.d_status.setText(f"<i>Delivered through {html.escape(name)}, which is not "
                                      "set up yet.</i>")

        def compat_text(self, game, inst):
            """The compatibility line of the details panel."""
            store_id = game.get("id") or (game_store_id(inst["dir"]) if inst else "")
            entry = compat_entry(store_id, self.compat)
            status = entry["status"]
            parts = [f"Compatibility: <span style='color:{STATUS_COLORS[status]}'>●</span> "
                     f"<b>{STATUS_LABELS[status]}</b>"]
            if entry.get("notes"):
                parts.append(html.escape(entry["notes"]))
            t = entry.get("tested") or {}
            if t.get("date"):
                parts.append(f"<i>tested {html.escape(str(t['date']))} with WineGDK "
                             f"{html.escape(str(t.get('winegdk') or '?'))}, launcher "
                             f"{html.escape(str(t.get('launcher') or '?'))}</i>")
            lid = title_launcher(entry, game, inst, self.launchers)
            if lid:
                name = launcher_name(lid, self.launchers)
                state = "installed" if self.launcher_installed(lid) else "not set up yet"
                parts.append(f"Delivered through <b>{html.escape(name)}</b> ({state}): it runs in "
                             "that launcher's own GE-Proton prefix.")
            ac = entry.get("anticheat") or {}
            if ac.get("kind") and ac.get("kind") != "none":
                online = {
                    "supported": "Linux module present: online play works with the Steam Proton "
                                 "runtime",
                    "unsupported": "no Linux module: online modes are unavailable" +
                                   (", single-player bypass applied" if ac.get("bypass") else ""),
                }.get(ac.get("online"), "Linux support unknown")
                parts.append("Anti-cheat: "
                             f"{html.escape(ANTICHEAT_LABELS.get(ac['kind'], str(ac['kind'])))} "
                             f"({online})")
            if status == "untested":
                parts.append("<i>Not tested yet - play it and send a compatibility report.</i>")
            if entry.get("_user"):
                parts.append(f"<i>user override: {COMPAT_USER_DIR / (store_id + '.json')}</i>")
            return "<br>".join(parts)

        # ---------- compatibility database ----------
        def refresh_compat(self):
            """Fetch a newer titles.json and launchers.json in the background (at most once a
            day each)."""
            def job(_w):
                fetched = refresh_compat_db()
                try:
                    fetched = thirdparty_module().refresh_launchers_db() or fetched
                except Exception as e:  # noqa: BLE001 - module missing, offline, invalid copy
                    print(f"launchers.json refresh skipped: {e}", file=sys.stderr)
                return fetched

            w = Worker(job)
            w.done.connect(self.compat_refreshed)
            self.workers.append(w)
            w.start()

        def compat_refreshed(self, ok, err, fetched):
            if ok and fetched:
                self.compat = load_compat_db()
                self.launchers = launchers_db()
                self.restyle_items()
                self.update_buttons()

        def restyle_items(self):
            for model in (self.cat_model, self.lib_model):
                for row in range(model.rowCount()):
                    self.style_item(model.item(row))
            self.cat_proxy.invalidateFilter()

        # ---------- third-party launchers / anti-cheat ----------
        def launcher_for(self, lid):
            """ThirdPartyLauncher for a recipe id, or None when the thirdparty module is missing
            or the recipe unknown (setup_launcher() shows the reason)."""
            try:
                return thirdparty_module().ThirdPartyLauncher(lid, self.cfg)
            except Exception:  # noqa: BLE001
                return None

        def launcher_installed(self, lid):
            tpl = self.launcher_for(lid)
            try:
                return bool(tpl and tpl.is_installed())
            except Exception:  # noqa: BLE001
                return False

        def launcher_action(self, lid):
            """The Set up <Launcher> / Open <Launcher> button."""
            if self.launcher_installed(lid):
                self.open_launcher(lid)
            else:
                self.setup_launcher(lid)

        def setup_launcher(self, lid, then=None):
            """Install a third-party launcher (or only show its sign-in guide when it is already
            installed); then() runs when the launcher is installed afterwards."""
            try:
                dlg = LauncherSetupDialog(self, self.cfg, lid,
                                          install=not self.launcher_installed(lid))
            except Exception as e:  # noqa: BLE001 - module missing, unknown recipe
                QMessageBox.warning(self, APP_NAME,
                                    f"Cannot set up {launcher_name(lid, self.launchers)}:\n{e}")
                return False
            dlg.exec()
            self.update_buttons()
            if dlg.ok and then:
                then()
            return dlg.ok

        def open_launcher(self, lid):
            tpl = self.launcher_for(lid)
            if not tpl:
                self.setup_launcher(lid)  # reports why the launcher is unavailable
                return
            w = Worker(lambda _w: tpl.open())  # .wine() may fetch GE-Proton: off the GUI thread

            def done(ok, err, proc):
                if ok:
                    self.launcher_procs = [p for p in self.launcher_procs
                                           if p.poll() is None] + [proc]
                else:
                    QMessageBox.warning(self, APP_NAME, f"Could not open {tpl.name}:\n{err}")

            w.done.connect(done)
            self.workers.append(w)
            w.start()

        def link_current(self):
            """Link installed game…: which game inside the launcher this title is. The pick is
            stored in the title's meta under games_dir (status "launcher") so Play and
            --play can start it."""
            g = self.current
            if not g:
                return
            inst = self.current_installed()
            store_id = g.get("id") or (game_store_id(inst["dir"]) if inst else "")
            entry = compat_entry(store_id, self.compat)
            lid = title_launcher(entry, g, inst, self.launchers)
            tpl = self.launcher_for(lid) if lid else None
            if not tpl:
                return
            if not self.launcher_installed(lid):
                self.setup_launcher(lid)
                return
            self.b_link.setEnabled(False)
            w = Worker(lambda _w: list(tpl.installed_games()))

            def done(ok, err, games):
                self.b_link.setEnabled(True)
                if not ok:
                    QMessageBox.warning(self, APP_NAME, "Could not list the games installed in "
                                        f"{tpl.name}:\n{err}")
                    return
                dlg = LinkGameDialog(self, g.get("title") or store_id, tpl.name, games)
                if not dlg.exec():
                    return
                pick = dlg.chosen()
                if not pick or not pick.get("id"):
                    QMessageBox.information(self, APP_NAME,
                                            "Pick a game from the list or enter its id.")
                    return
                full = next((c for c in self.catalog if c["id"] == store_id), g)
                if not full.get("id"):
                    full = dict(full, id=store_id)
                link_launcher_game(self.cfg, full, lid, pick["id"], pick.get("name") or "",
                                   pick.get("dir") or "")
                self.refresh_library()

            w.done.connect(done)
            self.workers.append(w)
            w.start()

        def play_launcher_title(self, g, inst, entry, lid):
            """Play for a title delivered through a third-party launcher: set the launcher up or
            link the game when needed, then start it there (Play becomes Stop as usual; Stop
            brings down the launcher's prefix)."""
            if not self.launcher_installed(lid):
                self.setup_launcher(lid, then=self.play_current)
                return
            if not (inst and inst.get("status") == "launcher"):
                gid = entry.get("launcher_game_id")
                if not gid:
                    self.link_current()
                    return
                full = next((c for c in self.catalog if c["id"] == g.get("id")), g)
                link_launcher_game(self.cfg, full, lid, gid)
                self.refresh_library()
                inst = find_installed(self.cfg, g.get("id"))
                if not inst:
                    return
            self.start_play_job(inst)

        def offer_launcher(self, game, dest):
            """A download failed because the title has no Store package: when its entry names a
            third-party launcher, or its publisher matches a recipe (launchers.json
            publisher_match), offer to set it up / open it instead of a bare error."""
            lid = title_launcher(compat_entry(game.get("id"), self.compat), game, None, self.launchers)
            if not lid:
                return
            try:  # the folder the download created holds only our meta: drop the "incomplete" ghost
                if dest and {p.name for p in Path(dest).iterdir()} == {META_NAME}:
                    shutil.rmtree(dest)
                    self.refresh_library()
            except OSError:
                pass
            name = launcher_name(lid, self.launchers)
            have = self.launcher_installed(lid)
            q = (f"{game.get('title')} is not delivered as a Store package but through {name}.\n\n"
                 + (f"Open {name} to install it there?" if have else f"Set up {name} now?"))
            if QMessageBox.question(self, APP_NAME, q) != QMessageBox.Yes:
                return
            self.show_game(next((c for c in self.catalog if c["id"] == game.get("id")), game))
            self.launcher_action(lid)

        def anticheat_warning(self, inst, entry):
            """Before the first Play of a title whose anti-cheat has no Linux module: say that
            online modes are unavailable (once per title, remembered in the config). False when
            the user cancels."""
            store_id = inst.get("id") or entry.get("id") or ""
            if store_id and store_id in (self.cfg.get("anticheat_warned") or []):
                return True
            info, _ = anticheat_status(inst["dir"], entry, resolve_wine(self.cfg, entry))
            if info.get("online") != "unsupported" or info.get("kind") in ("none", "unknown"):
                return True
            launch = entry.get("launch") or {}
            bypassed = bool(launch.get("executable") or info.get("bypass") or
                            (launch.get("eac_bypass") is not False and
                             eac_real_executable(inst["dir"])))
            text = (f"{inst.get('title') or store_id} uses "
                    f"{ANTICHEAT_LABELS.get(info['kind'], str(info['kind']))}, and this build "
                    "ships no Linux module for it.\n\n" +
                    ("The launcher starts the game past the anti-cheat, so only offline / "
                     "single-player modes work: online modes are unavailable." if bypassed else
                     "No single-player bypass is known for it, so the game may not start."))
            if info.get("message"):
                text += "\n\n" + str(info["message"])
            box = QMessageBox(QMessageBox.Warning, APP_NAME, text,
                              QMessageBox.Ok | QMessageBox.Cancel, self)
            box.setInformativeText("Shown once per title.")
            if box.exec() != QMessageBox.Ok:
                return False
            if store_id:
                self.cfg["anticheat_warned"] = sorted(
                    set(self.cfg.get("anticheat_warned") or []) | {store_id})
                save_config(self.cfg)
            return True

        def report_current(self):
            inst = self.current_installed()
            if not inst:
                return
            game = dict(self.current)
            game["dir"] = inst["dir"]
            game["id"] = inst.get("id") or game.get("id") or game_store_id(inst["dir"])
            entry = compat_entry(game["id"], self.compat)
            dlg = ReportDialog(self, game, entry)
            if not dlg.exec():
                return
            try:
                path = write_compat_report(self.cfg, game, entry, dlg.status.currentData(),
                                           dlg.notes.toPlainText().strip())
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not write the report:\n{e}")
                return
            QDesktopServices.openUrl(QUrl(report_issue_url(json.loads(path.read_text()), path)))
            QMessageBox.information(self, APP_NAME,
                                    f"Report saved to\n{path}\n\nA GitHub issue form opened in "
                                    "your browser - attach the file there (or send it any other way).")

        # ---------- images ----------
        def on_image(self, url, pm):
            for model in (self.cat_model, self.lib_model):
                for row in range(model.rowCount()):
                    item = model.item(row)
                    g = item.data(ROLE_GAME)
                    if g and g.get("poster") == url:
                        self.style_item(item, pm)
            if self.current and url in (self.current.get("hero"), self.current.get("poster_big")):
                self.set_hero(pm)

        def make_item(self, game, suffix=""):
            item = QStandardItem(game["title"] + suffix)
            item.setData(game, ROLE_GAME)
            self.style_item(item)
            return item

        def style_item(self, item, pm=None):
            """Icon (poster plus compatibility badge), tooltip and status role of a grid item.
            Catalog items only get a badge once the title has a known status; installed ones
            always (an "Untested" badge is the nudge to send a report)."""
            game = item.data(ROLE_GAME)
            entry = compat_entry(game.get("id"), self.compat)
            item.setData(entry["status"], ROLE_STATUS)
            known = entry["status"] != "untested" or bool(game.get("dir"))
            lid = title_launcher(entry, game, game, self.launchers)
            tag = f"via {launcher_name(lid, self.launchers)}" if lid else ""
            tip = game["title"]
            if tag:
                tip += "\n" + tag
            if known:
                tip += f"\nCompatibility: {STATUS_LABELS[entry['status']]}"
                if entry.get("notes"):
                    tip += "\n" + entry["notes"]
            item.setToolTip(tip)
            pm = pm or self.images.get(game.get("poster"))
            if not pm:
                pm = QPixmap(150, 225)
                pm.fill(Qt.darkGray)
            item.setIcon(badge_icon(pm, entry["status"], known, tag))

        # ---------- catalog ----------
        def load_catalog(self, force=False):
            self.count.setText("Loading catalog…")

            def job(w):
                return load_catalog(self.cfg, force,
                                    lambda d, t: w.progress.emit(d, t))

            w = Worker(job)
            w.progress.connect(lambda d, t: self.count.setText(f"Loading catalog… {d}/{t}"))
            w.done.connect(self.catalog_loaded)
            self.workers.append(w)
            w.start()

        def catalog_loaded(self, ok, err, games):
            if not ok:
                self.count.setText("Catalog failed")
                QMessageBox.warning(self, APP_NAME, f"Could not load the Game Pass catalog:\n{err}")
                return
            self.catalog = games
            self.cat_model.clear()
            genres = set()
            for g in games:
                self.cat_model.appendRow(self.make_item(g))
                genres.update(g.get("genres") or [])
            self.genre.blockSignals(True)
            self.genre.clear()
            self.genre.addItem("All genres")
            self.genre.addItems(sorted(genres))
            self.genre.blockSignals(False)
            self.apply_filter()
            if self.pending_install:  # --install <ID>
                game = next((c for c in games if c["id"] == self.pending_install), None)
                self.pending_install = None
                if game:
                    self.show_game(game)
                    self.install_current(confirm=False)

        def apply_filter(self):
            self.cat_proxy.text = self.search.text().strip().lower()
            gtext = self.genre.currentText()
            self.cat_proxy.genre = "" if gtext in ("", "All genres") else gtext
            self.cat_proxy.status = self.status_filter.currentData() or ""
            self.cat_proxy.invalidateFilter()
            self.count.setText(f"{self.cat_proxy.rowCount()} games")

        # ---------- library ----------
        def refresh_library(self):
            self.lib_model.clear()
            games = installed_games(self.cfg)
            for g in games:
                suffix = "" if g.get("status") in ("installed", "launcher") else \
                    f"\n({g.get('status')})"
                self.lib_model.appendRow(self.make_item(g, suffix))
            self.lib_empty.setVisible(not games)
            self.update_buttons()

        # ---------- account ----------
        def refresh_account(self):
            if is_logged_in():
                self.account.setText("✔ Signed in")
                self.login_btn.setText("Sign out")
            else:
                self.account.setText("Not signed in")
                self.login_btn.setText("Sign in")

        def toggle_login(self):
            if self.login_btn.text() == "Sign out":
                if QMessageBox.question(self, APP_NAME, "Sign out of your Xbox account?") \
                        != QMessageBox.Yes:
                    return
                subprocess.run([self.cfg["xodus_cli"], "logout"])
                self.refresh_account()
                return
            self.login_btn.setEnabled(False)
            self.account.setText("Sign-in window open…")

            def job(w):
                return subprocess.run([self.cfg["xodus_cli"], "login"],
                                      capture_output=True, text=True)

            w = Worker(job)
            w.done.connect(self.login_done)
            self.workers.append(w)
            w.start()

        def login_done(self, ok, err, res):
            self.login_btn.setEnabled(True)
            self.refresh_account()
            if not is_logged_in():
                msg = err or (res.stderr[-800:] if res else "")
                QMessageBox.warning(self, APP_NAME, f"Sign-in did not complete.\n{msg}")

        # ---------- wine ----------
        def refresh_wine_btn(self):
            wine = self.cfg.get("wine") or find_wine()
            self.wine_btn.setText("Wine ✔" if wine and Path(wine).exists() else "Set up Wine")

        def setup_wine(self, then=None):
            dlg = LogDialog(self, "Set up xodus Wine")
            dlg.add("This downloads the xodus team's Proton build from their GitHub CI\n"
                    "and prepares a Wine prefix with DXVK / VKD3D-Proton.\n")

            def job(w):
                return setup_wine(self.cfg, lambda s, replace=False: w.log.emit(s, replace))

            w = Worker(job)
            w.log.connect(dlg.add)

            def done(ok, err, _):
                dlg.close_btn.setEnabled(True)
                if not ok:
                    dlg.add("\nERROR: " + err)
                self.refresh_wine_btn()
                if ok and then:
                    dlg.accept()
                    then()

            w.done.connect(done)
            self.workers.append(w)
            w.start()
            dlg.exec()

        # ---------- actions ----------
        def install_current(self, confirm=True):
            g = self.current
            if not g or not g.get("id"):
                return
            lid = title_launcher(compat_entry(g["id"], self.compat), g, None, self.launchers)
            if lid:  # no Store package: the title comes through a third-party launcher
                self.launcher_action(lid)
                return
            if not is_logged_in():
                QMessageBox.information(self, APP_NAME, "Sign in first (top right).")
                return
            inst = find_installed(self.cfg, g["id"])
            dest = Path(inst["dir"]) if inst else Path(self.cfg["games_dir"]) / safe_dirname(g["title"])
            Path(self.cfg["games_dir"]).mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.cfg["games_dir"]).free
            if g.get("size") and g["size"] > free:
                QMessageBox.warning(self, APP_NAME,
                                    f"Not enough space: needs {human_size(g['size'])}, "
                                    f"{human_size(free)} free.")
                return
            if confirm and QMessageBox.question(
                    self, APP_NAME,
                    f"Install {g['title']}?\n\nSize: {human_size(g.get('size'))}\n"
                    f"Free: {human_size(free)}\nFolder: {dest}") != QMessageBox.Yes:
                return
            # if the selected item came from the library, use the catalog entry for metadata
            full = next((c for c in self.catalog if c["id"] == g["id"]), g)
            job = DownloadJob(self.cfg, full, dest)
            row = DownloadRow(full["title"])
            self.dl_layout.insertWidget(self.dl_layout.count() - 1, row)
            self.rows[id(job)] = row
            row.cancel_btn.clicked.connect(lambda: self.cancel_job(job))
            job.progress.connect(lambda stage, pct, text: self.job_progress(row, stage, pct, text))
            job.log.connect(row.log.appendPlainText)
            job.finished_job.connect(lambda ok, msg: self.job_done(job, ok, msg))
            self.queue.append(job)
            self.tabs.setCurrentIndex(2)
            self.pump_queue()
            self.update_buttons()

        def job_progress(self, row, stage, pct, text):
            row.status.setText(stage)
            row.bar.setValue(pct if stage == "Downloading" else 0)
            row.bar.setFormat(text if stage == "Downloading" else stage)
            if stage != "Downloading":
                row.bar.setRange(0, 0)
            else:
                row.bar.setRange(0, 100)

        def pump_queue(self):
            if self.active or not self.queue:
                return
            self.active = self.queue.pop(0)
            self.rows[id(self.active)].status.setText("Starting…")
            self.active.start()

        def cancel_job(self, job):
            row = self.rows[id(job)]
            if job in self.queue:
                self.queue.remove(job)
                row.status.setText("Cancelled")
                row.cancel_btn.setEnabled(False)
            elif job is self.active:
                job.cancel.set()
                row.status.setText("Cancelling…")
            self.update_buttons()

        def job_done(self, job, ok, msg):
            row = self.rows[id(job)]
            row.bar.setRange(0, 100)
            row.bar.setValue(100 if ok else row.bar.value())
            row.bar.setFormat("Done" if ok else "Failed")
            row.status.setText("✔ Installed" if ok else "✖ " + msg[:120])
            row.cancel_btn.setEnabled(False)
            if not ok:
                row.log.appendPlainText("FAILED: " + msg)
            self.active = None
            self.refresh_library()
            self.pump_queue()
            if not ok and "no PC package" in msg:
                self.offer_launcher(job.game, job.dest)

        def current_installed(self):
            g = self.current
            if not g:
                return None
            if g.get("id"):
                return find_installed(self.cfg, g["id"])
            return g if g.get("dir") else None

        def play_current(self):
            g = self.current
            inst = self.current_installed()
            if not g:
                return
            entry = compat_entry(g.get("id") or (game_store_id(inst["dir"]) if inst else ""),
                                 self.compat)
            lid = title_launcher(entry, g, inst, self.launchers)
            if lid:
                self.play_launcher_title(g, inst, entry, lid)
                return
            if not inst:
                return
            wine = self.cfg.get("wine") or find_wine()
            if not wine or not Path(wine).exists():
                if QMessageBox.question(self, APP_NAME,
                                        "Games need the xodus Wine build (~280 MB download). "
                                        "Set it up now?") == QMessageBox.Yes:
                    self.setup_wine(then=self.play_current)
                return
            if not self.anticheat_warning(inst, entry):
                return
            self.start_play_job(inst)

        def start_play_job(self, inst):
            job = LaunchJob(self.cfg, inst["dir"])
            self.play_job = job
            # the Play button becomes Stop while the game runs (Alt+F4 may not reach
            # the game under some compositors; this always works)
            self.b_play.setText("⏹  Stop")
            try:
                self.b_play.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.b_play.clicked.connect(self.stop_current)

            def done(code, tail):
                try:
                    self.b_play.clicked.disconnect()
                except (RuntimeError, TypeError):
                    pass
                self.b_play.clicked.connect(self.play_current)
                self.b_play.setText("▶  Play")
                self.b_play.setEnabled(True)
                self.play_job = None
                if code != 0 and not job.stopped:
                    box = QMessageBox(self)
                    box.setWindowTitle(APP_NAME)
                    box.setText(f"The game exited with code {code}.\n"
                                "Xodus game launching is still experimental - many games "
                                "don't run yet.")
                    box.setDetailedText(tail)
                    box.exec()

            job.finished_job.connect(done)
            self.workers.append(job)
            job.start()

        def stop_current(self):
            job = getattr(self, "play_job", None)
            if not job:
                return
            self.b_play.setText("Stopping…")
            self.b_play.setEnabled(False)
            threading.Thread(target=job.stop, daemon=True).start()

        def steam_current(self):
            inst = self.current_installed()
            if not inst:
                return
            if steam_running():
                QMessageBox.information(self, APP_NAME,
                                        "Please close Steam completely first (Steam > Exit), "
                                        "then click Add to Steam again.")
                return
            game = dict(self.current)
            game["id"] = inst.get("id") or game.get("id")
            tmp = CACHE_DIR / "steam-art"
            tmp.mkdir(parents=True, exist_ok=True)
            poster = tmp / f"{game['id']}p.jpg"
            hero = tmp / f"{game['id']}_hero.jpg"
            self.images.save(game.get("poster_big") or game.get("poster"), poster) or \
                self.images.save(game.get("poster"), poster)
            self.images.save(game.get("hero"), hero)
            try:
                n = add_steam_shortcut(game, poster, hero)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Could not add Steam shortcut:\n{e}")
                return
            QMessageBox.information(self, APP_NAME,
                                    f"Added “{game['title']}” to Steam ({n} account(s)).\n"
                                    "Start Steam to see it in your library.")

        def folder_current(self):
            inst = self.current_installed()
            if inst:
                d = inst.get("launcher_game_dir") if inst.get("status") == "launcher" else ""
                QDesktopServices.openUrl(QUrl.fromLocalFile(d or inst["dir"]))

        def remove_current(self):
            inst = self.current_installed()
            if not inst:
                return
            if inst.get("status") == "launcher":
                name = launcher_name(inst.get("launcher") or "", self.launchers)
                if QMessageBox.question(
                        self, APP_NAME,
                        f"Remove {inst.get('title')} from the library?\n\nThe game stays "
                        f"installed in {name}; only the link\n{inst['dir']}\nis removed.") \
                        != QMessageBox.Yes:
                    return
                unlink_launcher_game(inst["dir"])
                self.refresh_library()
                return
            if QMessageBox.question(
                    self, APP_NAME,
                    f"Uninstall {inst.get('title')}?\n\nThis deletes the folder:\n{inst['dir']}") \
                    != QMessageBox.Yes:
                return
            shutil.rmtree(inst["dir"], ignore_errors=True)
            self.refresh_library()

        def open_settings(self):
            dlg = SettingsDialog(self, self.cfg)
            old = (self.cfg["market"], self.cfg["language"])
            accepted = dlg.exec()
            if accepted:
                dlg.apply()
                self.refresh_wine_btn()
            # launchers may have been set up or removed from the dialog, even when cancelled
            self.launchers = launchers_db()
            self.refresh_library()
            if accepted and old != (self.cfg["market"], self.cfg["language"]):
                self.load_catalog(force=True)

        def closeEvent(self, ev):
            if self.active:
                if QMessageBox.question(self, APP_NAME,
                                        "A download is running. Quit and cancel it?") \
                        != QMessageBox.Yes:
                    ev.ignore()
                    return
                self.active.cancel.set()
                self.active.wait(5000)
            if self.play_job:  # stop a running game so it doesn't outlive the launcher
                self.play_job.stop()
            ev.accept()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("xodus-launcher")
    win = Main()
    win.pending_install = install_id
    win.show()
    return app.exec()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--play":
        sys.exit(cli_play(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--install":
        sys.exit(run_gui(sys.argv[2]))
    sys.exit(run_gui())
