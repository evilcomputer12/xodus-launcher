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
CATALOG_MAX_AGE = 12 * 3600
UA = "xodus-launcher/1.0"

DEFAULT_CONFIG = {
    "games_dir": str(HOME / "Games/GamePass"),
    "market": "TR",
    "language": "en-us",
    "xodus_cli": "xodus-cli",
    "wine": "",
    "prefix": str(DATA_DIR / "prefix-gdk"),
    "parallel": 4,
    "extra_env": {},
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


def _parse_product(p):
    lp = (p.get("LocalizedProperties") or [{}])[0]
    images = lp.get("Images") or []
    size = 0
    for avail in p.get("DisplaySkuAvailabilities") or []:
        for pkg in (avail.get("Sku", {}).get("Properties", {}).get("Packages") or []):
            deps = [d.get("PlatformName") for d in pkg.get("PlatformDependencies") or []]
            if "Windows.Desktop" in deps:
                size = max(size, pkg.get("MaxDownloadSizeInBytes") or 0)
    props = p.get("Properties") or {}
    return {
        "id": p["ProductId"],
        "title": lp.get("ProductTitle", p["ProductId"]),
        "publisher": lp.get("PublisherName", ""),
        "description": lp.get("ShortDescription") or lp.get("ProductDescription", ""),
        "genres": props.get("Categories") or ([props["Category"]] if props.get("Category") else []),
        "size": size,
        "poster": _image_url(images, ["Poster", "BoxArt", "BrandedKeyArt"], 300),
        "poster_big": _image_url(images, ["Poster", "BoxArt"], 600),
        "hero": _image_url(images, ["TitledHeroArt", "SuperHeroArt", "Screenshot"], 960),
    }


def fetch_catalog(market, language, progress=None):
    sigl = http_json(f"https://catalog.gamepass.com/sigls/v2?id={ALL_PC_GAMES_SIGL}"
                     f"&language={language}&market={market}")
    ids = [x["id"] for x in sigl if "id" in x]
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
                    games.append(_parse_product(p))
                except (KeyError, IndexError, TypeError):
                    pass
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


def find_executable(game_dir):
    """Main PC executable from MicrosoftGame.config (same rules as xodus PR #124)."""
    manifest = find_manifest(game_dir)
    if manifest is None:
        return None
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
    buf, errors, answered = "", [], False
    for chunk in proc.chunks():
        buf += chunk
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
        # interactive "pick a sub-product" prompt: take the first entry
        if not answered and "Select files to download" in strip_ansi(buf + chunk):
            answered = True
            on_log("(xodus asked to pick a sub-product - choosing the first one)")
            proc.write("\r")
    code = proc.wait()
    cancelled = cancel_event.is_set()
    cancel_event.set()  # release the watcher thread
    if cancelled:
        return False, "Cancelled"
    if code == 0 and (Path(dest) / ".xodus-streaming.msixvc").exists() and not errors:
        return True, "Installed"
    if errors:
        return False, errors[-1]
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


def wine_env(cfg, wine):
    env = dict(os.environ)
    env.update({
        "WINEPREFIX": cfg["prefix"],
        "WINEDLLOVERRIDES": "d3d8,d3d9,d3d10core,d3d11,dxgi,d3d12,d3d12core=n,b;"
                            "xaudio2_7=n,b;x3daudio1_7=n,b;xgameruntime=n,b;winegstreamer=d;"
                            "mscoree,mshtml=",  # never block on the Mono/Gecko install prompt
        "WINEDEBUG": "-all",
        "WINEFSYNC": "1",
        "WINEESYNC": "1",
        "VKD3D_CONFIG": "dxr11",
        "PULSE_LATENCY_MSEC": "60",
    })
    bindir = str(Path(wine).parent)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env.update(cfg.get("extra_env") or {})
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
    dist = Path(wine).parent.parent  # Proton dist: source of DXVK / VKD3D-Proton
    if not any(w.exists() for w in BUILT_WINES):
        download_winegdk_release(log)
    wine = find_wine()
    log(f"Wine: {wine}")
    if list(Path(wine).parent.parent.glob("lib*/wine/*/xgameruntime.dll")):
        log("xgameruntime.dll found in this Wine build")
    else:
        log("WARNING: xgameruntime.dll not found in this Wine build - games may not start")

    prefix = Path(cfg["prefix"])
    env = wine_env(cfg, wine)
    # create the prefix, or update it after switching Wine builds; mscoree/mshtml disabled so
    # wineboot never waits on the "install Wine Mono/Gecko" dialog
    if True:
        log("Creating / updating Wine prefix (takes a minute) ...")
        prefix.mkdir(parents=True, exist_ok=True)
        e = dict(env)
        e["WINEDLLOVERRIDES"] = "mscoree,mshtml="
        subprocess.run([wine, "wineboot", "-u"], env=e, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([str(Path(wine).parent / "wineserver"), "-w"], env=e, timeout=600)
    copied = 0
    archmap = {"x86_64-windows": "system32", "i386-windows": "syswow64"}
    for comp in ("dxvk", "vkd3d-proton"):
        for arch, sysdir in archmap.items():
            for dll in glob.glob(str(dist / "lib*" / "wine" / comp / arch / "*.dll")):
                target = prefix / "drive_c/windows" / sysdir
                if target.is_dir():
                    dst = target / Path(dll).name
                    dst.unlink(missing_ok=True)  # Proton ships these read-only
                    shutil.copyfile(dll, dst)
                    copied += 1
    log(f"Installed {copied} DXVK/VKD3D-Proton DLLs into the prefix")
    cfg["wine"] = wine
    save_config(cfg)
    log("Wine is ready.")
    return wine


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


def stop_wine(cfg):
    """Kill everything in the game's Wine prefix (used to force-stop a running game)."""
    wine = cfg.get("wine") or find_wine()
    if not wine:
        return
    env = dict(os.environ)
    env["WINEPREFIX"] = cfg["prefix"]
    try:
        subprocess.run([str(Path(wine).parent / "wineserver"), "-k"], env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def launch_game(cfg, game_dir, log=print, on_start=None):
    """Run the game via `xodus-cli run`. Blocks until it exits. Returns (code, tail).

    on_start(popen) is called once the process is spawned, so a caller can stop it."""
    wine = cfg.get("wine") or find_wine()
    if not wine or not Path(wine).exists():
        return 1, "Wine is not set up yet - open Xodus Launcher and use Settings > Set up Wine."
    exe = find_executable(game_dir)
    candidates = exe_candidates(exe) if exe else [None]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / (safe_dirname(Path(game_dir).name) + ".log")
    env = wine_env(cfg, wine)
    if ensure_xodus_service(log):
        threading.Thread(target=prewarm_identity, args=(game_dir, log), daemon=True).start()
    code, tail = 1, ""
    for cand in candidates:
        cmd = [cfg["xodus_cli"], "run", str(game_dir), wine, "-m", cfg["market"]]
        if cand:
            cmd += ["--exe", cand]
        log("$ " + " ".join(cmd))
        with open(logfile, "w") as lf:
            lf.write("$ " + " ".join(cmd) + "\n")
            lf.flush()
            # new session so the whole wine process tree can be signalled as a group
            p = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT,
                                 cwd=game_dir, start_new_session=True)
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
    from PySide6.QtCore import (QObject, QSize, QSortFilterProxyModel, Qt, QThread, QUrl,
                                Signal, QStandardPaths)
    from PySide6.QtGui import QDesktopServices, QIcon, QPixmap, QStandardItem, QStandardItemModel
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest
    from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                                   QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                   QListView, QMainWindow, QMessageBox, QPlainTextEdit,
                                   QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
                                   QTabWidget, QVBoxLayout, QWidget)

    ROLE_GAME = Qt.UserRole + 1

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
            stop_wine(self.cfg)  # bring down any detached wine processes in the prefix
            if p and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    class FilterModel(QSortFilterProxyModel):
        def __init__(self):
            super().__init__()
            self.text, self.genre = "", ""

        def filterAcceptsRow(self, row, parent):
            g = self.sourceModel().index(row, 0, parent).data(ROLE_GAME)
            if not g:
                return False
            if self.text and self.text not in (g["title"] + " " + g.get("publisher", "")).lower():
                return False
            if self.genre and self.genre not in (g.get("genres") or []):
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
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            form.addRow(bb)

        def pick_dir(self):
            d = QFileDialog.getExistingDirectory(self, "Games folder", self.games_dir.text())
            if d:
                self.games_dir.setText(d)

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
                cur.movePosition(cur.End)
                cur.select(cur.BlockUnderCursor)
                if cur.selectedText().strip().endswith(("B", "?")):
                    cur.removeSelectedText()
            self.text.appendPlainText(line)

    class Main(QMainWindow):
        def __init__(self):
            super().__init__()
            self.cfg = load_config()
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
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(lambda: self.load_catalog(force=True))
            self.count = QLabel()
            bar.addWidget(self.search, 1)
            bar.addWidget(self.genre)
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
            btns = QVBoxLayout()
            self.b_install = QPushButton("Install")
            self.b_play = QPushButton("▶  Play")
            self.b_steam = QPushButton("Add to Steam")
            self.b_folder = QPushButton("Open folder")
            self.b_remove = QPushButton("Uninstall")
            self.b_install.clicked.connect(self.install_current)
            self.b_play.clicked.connect(self.play_current)
            self.b_steam.clicked.connect(self.steam_current)
            self.b_folder.clicked.connect(self.folder_current)
            self.b_remove.clicked.connect(self.remove_current)
            for b in (self.b_play, self.b_install, self.b_steam, self.b_folder, self.b_remove):
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
            self.b_install.setVisible(not busy and status != "installed" and bool(g.get("id")))
            self.b_install.setText("Resume / repair download" if status == "incomplete" else "Install")
            self.b_play.setVisible(status == "installed")
            self.b_steam.setVisible(status == "installed" and bool(g.get("id")))
            self.b_folder.setVisible(bool(inst))
            self.b_remove.setVisible(bool(inst) and not busy)
            if busy:
                self.d_status.setText("<i>Downloading – see the Downloads tab</i>")
            elif status == "installed":
                exe = find_executable(inst["dir"]) or "?"
                self.d_status.setText(f"<b>Installed</b> in {inst['dir']}<br>Executable: {exe}")
            elif status == "incomplete":
                self.d_status.setText("<b>Download incomplete</b>")
            else:
                self.d_status.setText("")

        # ---------- images ----------
        def on_image(self, url, pm):
            for model in (self.cat_model, self.lib_model):
                for row in range(model.rowCount()):
                    item = model.item(row)
                    g = item.data(ROLE_GAME)
                    if g and g.get("poster") == url:
                        item.setIcon(QIcon(pm.scaled(150, 225, Qt.KeepAspectRatioByExpanding,
                                                     Qt.SmoothTransformation)))
            if self.current and url in (self.current.get("hero"), self.current.get("poster_big")):
                self.set_hero(pm)

        def make_item(self, game, suffix=""):
            item = QStandardItem(game["title"] + suffix)
            item.setData(game, ROLE_GAME)
            item.setToolTip(game["title"])
            pm = self.images.get(game.get("poster"))
            if pm:
                item.setIcon(QIcon(pm.scaled(150, 225, Qt.KeepAspectRatioByExpanding,
                                             Qt.SmoothTransformation)))
            else:
                ph = QPixmap(150, 225)
                ph.fill(Qt.darkGray)
                item.setIcon(QIcon(ph))
            return item

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
            self.cat_proxy.invalidateFilter()
            self.count.setText(f"{self.cat_proxy.rowCount()} games")

        # ---------- library ----------
        def refresh_library(self):
            self.lib_model.clear()
            games = installed_games(self.cfg)
            for g in games:
                suffix = "" if g.get("status") == "installed" else f"\n({g.get('status')})"
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

        def current_installed(self):
            g = self.current
            if not g:
                return None
            if g.get("id"):
                return find_installed(self.cfg, g["id"])
            return g if g.get("dir") else None

        def play_current(self):
            inst = self.current_installed()
            if not inst:
                return
            wine = self.cfg.get("wine") or find_wine()
            if not wine or not Path(wine).exists():
                if QMessageBox.question(self, APP_NAME,
                                        "Games need the xodus Wine build (~280 MB download). "
                                        "Set it up now?") == QMessageBox.Yes:
                    self.setup_wine(then=self.play_current)
                return
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
                QDesktopServices.openUrl(QUrl.fromLocalFile(inst["dir"]))

        def remove_current(self):
            inst = self.current_installed()
            if not inst:
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
            if dlg.exec():
                old = (self.cfg["market"], self.cfg["language"])
                dlg.apply()
                self.refresh_library()
                self.refresh_wine_btn()
                if old != (self.cfg["market"], self.cfg["language"]):
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
