#!/usr/bin/env python3
"""Third-party launchers for Xodus Launcher, plus the anti-cheat runtime helpers.

Some PC Game Pass titles have no Store package: the Xbox app only installs a stub that hands
the game to the publisher's own launcher (Ubisoft Connect, the EA app, Battle.net, the
Rockstar Games Launcher, the Riot Client), which downloads and runs it. This module:

- installs those launchers from the publisher's official installer URL into a Wine prefix of
  their own (``DATA_DIR/prefix-<launcher id>``) run by a checksum-verified GE-Proton build
  downloaded from its GitHub releases (``ensure_ge_proton``),
- signs the user in in the publisher's own window and shows where, on the publisher's site,
  the Xbox account that holds the subscription gets linked (so the Game Pass catalogue appears),
- lists the games each launcher has installed (registry files / data files parsed in Python)
  and starts them through the launcher,
- finds Steam's "Proton EasyAntiCheat Runtime" / "Proton BattlEye Runtime" and decides, per
  game folder and compatibility entry, whether online modes can work (``anticheat_for_game``).

Everything about a launcher is data: ``data/compat/launchers.json`` (schema in
``data/compat/README.md``), overridable per launcher in
``~/.config/xodus-launcher/launchers/<id>.json``. Nothing third-party is shipped: installers
come from the publisher at install time, GE-Proton from GitHub, anti-cheat runtimes from the
user's own Steam install. Only the standard library is used; ``xodus_launcher.py`` imports this
module from its own directory and never the other way round.
"""

import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOME = Path.home()
DATA_DIR = HOME / ".local/share/xodus-launcher"
CACHE_DIR = HOME / ".cache/xodus-launcher"
LOG_DIR = CACHE_DIR / "logs"
CONFIG_PATH = HOME / ".config/xodus-launcher/config.json"
LAUNCHERS_USER_DIR = HOME / ".config/xodus-launcher/launchers"
UA = "xodus-launcher/1.0"
_SELF = Path(__file__).resolve()
# bundled next to titles.json: in a checkout, next to the script (install.sh) or system-wide
LAUNCHERS_DB = next((p for p in (_SELF.parent.parent / "data/compat/launchers.json",
                                 _SELF.parent / "compat/launchers.json",
                                 Path("/usr/share/xodus-launcher/compat/launchers.json"))
                     if p.exists()), None)
LAUNCHERS_URL = ("https://raw.githubusercontent.com/evilcomputer12/xodus-launcher/main/"
                 "data/compat/launchers.json")
LAUNCHERS_CACHE = CACHE_DIR / "compat/launchers.json"
LAUNCHERS_MAX_AGE = 24 * 3600

GE_RELEASES_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
GE_TAG_RE = re.compile(r"^GE-Proton(\d+)-(\d+)$")
GE_MIN_FREE_BYTES = 3 << 30  # ~564 MB archive + ~1.6 GB extracted, with headroom

# Valve's Linux builds of the two anti-cheat client modules, as hidden Steam tool apps
STEAM_INSTALL_URLS = {"eac": "steam://install/1826330", "battleye": "steam://install/1161040"}
ANTICHEAT_RUNTIME_DIRS = {"eac": "Proton EasyAntiCheat Runtime", "battleye": "Proton BattlEye Runtime"}
ANTICHEAT_RUNTIME_MARKER = {"eac": "v2", "battleye": "v1"}  # the directory Proton's Wine looks into
ANTICHEAT_ENV = {"eac": "PROTON_EAC_RUNTIME", "battleye": "PROTON_BATTLEYE_RUNTIME"}
ANTICHEAT_KINDS = ("eac", "battleye", "ricochet", "vanguard", "none", "unknown")
ANTICHEAT_LABELS = {"eac": "Easy Anti-Cheat", "battleye": "BattlEye", "ricochet": "Ricochet (kernel driver)",
                    "vanguard": "Riot Vanguard (kernel driver)", "none": "none", "unknown": "unknown"}
ONLINE_STATES = ("supported", "unsupported", "unknown")
EAC_FILES = {"easyanticheat_x64.dll", "easyanticheat_x86.dll", "easyanticheat_launcher.exe",
             "easyanticheat_setup.exe", "easyanticheat_eos_setup.exe"}
EAC_LINUX_FILES = {"easyanticheat_x64.so", "easyanticheat_x86.so", "libeasyanticheat.so"}
BATTLEYE_FILES = {"beclient_x64.dll", "beclient.dll", "beservice_x64.exe", "beservice.exe", "belauncher.ini"}

PREFIX_MARKER = ".xodus-launcher-prefix.json"      # which launcher / GE-Proton made the prefix
TWEAKS_MARKER = ".xodus-compat-applied.json"       # same file and shape as the per-title tweaks
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Cancelled(RuntimeError):
    """Raised when a cancel_event is set during a long operation."""


# --------------------------------------------------------------------------
# small helpers (kept local: this module must not import xodus_launcher)
# --------------------------------------------------------------------------

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


def human_size(n):
    if not n:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("cancelled")


class _NotHttps(urllib.error.URLError):
    """A plain-http URL, or a redirect to one: never followed (the installers and GE-Proton
    are executed, so an unverified download over http would be a downgrade attack)."""


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).lower().startswith("https://"):
            raise _NotHttps(f"refusing to follow the redirect to a non-https URL {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnlyRedirects())


def _urlopen(req, timeout):
    """One place to patch in tests. https only, before and after redirects."""
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    if not url.lower().startswith("https://"):
        raise _NotHttps(f"refusing the non-https URL {url}")
    return _opener.open(req, timeout=timeout)


def _http_error_text(url, e):
    msg = f"HTTP {e.code} from {url}"
    if "api.github.com" in url and e.code in (403, 429):
        msg += " (GitHub API rate limit: 60 requests per hour without a token; try again later)"
    return msg


def _http_get(url, timeout=60, accept=None):
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    try:
        with _urlopen(urllib.request.Request(url, headers=headers), timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(_http_error_text(url, e)) from None
    except (OSError, TimeoutError) as e:  # URLError is an OSError
        raise RuntimeError(f"could not reach {url}: {e}") from None


def _http_json(url, timeout=60):
    try:
        return json.loads(_http_get(url, timeout, accept="application/vnd.github+json"))
    except ValueError:
        raise RuntimeError(f"{url} did not return JSON") from None


def _strong_validator(headers):
    """The ETag (a strong one) or Last-Modified of a response: what If-Range needs so that a
    resumed download is only continued when the server still has the same file."""
    etag = str(headers.get("ETag") or "").strip()
    if etag and not etag.upper().startswith("W/"):
        return etag
    return str(headers.get("Last-Modified") or "").strip()


def download_file(url, dest, progress=None, stage="download", cancel_event=None, log=print, attempts=8):
    """Stream url into dest with HTTP Range resume: <dest>.part is kept between attempts and
    continued only with If-Range on the validator (ETag / Last-Modified) recorded in
    <dest>.part.meta, so a leftover of an older build is never completed with the tail of a
    newer one. https only. progress(stage, pct or None, text). Raises RuntimeError with a
    readable message."""
    dest = Path(dest)
    if not str(url).lower().startswith("https://"):
        raise RuntimeError(f"refusing to download {dest.name} from a non-https URL: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    meta_path = part.with_name(part.name + ".meta")
    meta = _read_json(meta_path) or {}
    got = part.stat().st_size if part.exists() else 0
    validator = str(meta.get("validator") or "") if meta.get("url") == url else ""
    if got and not validator:
        got = 0  # another URL, or no validator to resume against: start over
    total, last, err = int(meta.get("total") or 0) if got else 0, 0.0, None
    for attempt in range(attempts):
        headers = {"User-Agent": UA}
        if got:
            headers["Range"] = f"bytes={got}-"
            headers["If-Range"] = validator
        try:
            with _urlopen(urllib.request.Request(url, headers=headers), 60) as r:
                if got and getattr(r, "status", 200) != 206:
                    got = 0  # the file changed, or the server ignored the range: start over
                m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range") or "")
                if m and got:
                    total = int(m.group(1))
                elif not got:
                    total = int(r.headers.get("Content-Length") or 0)
                    validator = _strong_validator(r.headers)
                    meta_path.write_text(json.dumps({"url": url, "validator": validator, "total": total}))
                with open(part, "ab" if got else "wb") as f:
                    while True:
                        _check_cancel(cancel_event)
                        block = r.read(1 << 20)
                        if not block:
                            break
                        f.write(block)
                        got += len(block)
                        now = time.monotonic()
                        if progress and now - last > 0.5:
                            last = now
                            progress(stage, int(got * 100 / total) if total else None,
                                     f"{human_size(got)} / {human_size(total) if total else '?'}")
            if not total or got >= total:
                part.replace(dest)
                meta_path.unlink(missing_ok=True)
                if progress:
                    progress(stage, 100 if total else None, f"{human_size(got)} downloaded")
                return dest
            err = RuntimeError("the connection closed before the end of the file")
        except _NotHttps as e:
            part.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise RuntimeError(f"could not download {url}: {e.reason}") from None
        except urllib.error.HTTPError as e:
            if e.code == 416 and got and total and got >= total:  # the partial file is complete
                part.replace(dest)
                meta_path.unlink(missing_ok=True)
                return dest
            if e.code == 416:
                got = 0  # the server disagrees about the size: start over
                part.unlink(missing_ok=True)
            elif e.code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(_http_error_text(url, e)) from None
            err = e
        except (OSError, TimeoutError) as e:
            err = e
        log(f"download of {dest.name} interrupted at {human_size(got)} ({err}); retrying")
        time.sleep(min(2 + attempt, 10))
    raise RuntimeError(f"could not download {url} after {attempts} attempts: {err}")


def hash_file(path, algorithms=("sha256",)):
    hs = {a: hashlib.new(a) for a in algorithms}
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            for h in hs.values():
                h.update(block)
    return {a: h.hexdigest() for a, h in hs.items()}


def _kill_group(p):
    """SIGTERM, then SIGKILL, the process group of a Popen started with start_new_session."""
    for sig in (15, 9):
        if p.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(p.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        for _ in range(25):
            if p.poll() is not None:
                return
            time.sleep(0.2)


def _run_logged(cmd, env, logfile, timeout, cwd=None, cancel_event=None):
    """Run cmd in its own session with its output appended to logfile; returns the exit code.
    Polls so that a cancel_event ends it promptly (the process group is killed and Cancelled
    raised). Raises RuntimeError on a missing binary or a timeout."""
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "a") as lf:
        lf.write("$ " + " ".join(str(c) for c in cmd) + "\n")
        lf.flush()
        try:
            p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise RuntimeError(f"could not run {cmd[0]}: {e}") from None
    deadline = time.monotonic() + timeout
    while p.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            _kill_group(p)
            raise Cancelled("cancelled")
        if time.monotonic() > deadline:
            _kill_group(p)
            raise RuntimeError(f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} did not finish "
                               f"within {timeout // 60} minutes (log: {logfile})")
        time.sleep(0.5)
    return p.returncode


def _log_tail(logfile, lines=15):
    try:
        return "\n".join(Path(logfile).read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def scope_cmd(name):
    """systemd-run prefix that puts a process in its own transient user scope, or [] without
    systemd. systemd-oomd kills whole cgroups: a game that eats the RAM must not take the launcher
    (or the terminal it was started from) down with it."""
    if not shutil.which("systemd-run") or os.environ.get("XODUS_NO_SCOPE"):
        return []
    unit = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{name}-{os.getpid()}-{int(time.time())}")
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]


def prefix_activity(prefix, window=90):
    """Bytes written in the last `window` seconds under the prefix's drive_c (temp downloads,
    program files): lets a silent installer show that it is still doing something."""
    now = time.time(); total = 0; newest = 0
    for base in ("drive_c/users", "drive_c/Program Files", "drive_c/Program Files (x86)", "drive_c/ProgramData"):
        root = Path(prefix) / base
        if not root.is_dir():
            continue
        for dirpath, dirs, files in os.walk(root):
            if "/cache/" in dirpath.replace("\\", "/") + "/":
                dirs[:] = []
                continue
            for f in files:
                try:
                    st = os.stat(os.path.join(dirpath, f))
                except OSError:
                    continue
                if now - st.st_mtime <= window:
                    total += st.st_size
                    newest = max(newest, st.st_mtime)
    return total, newest


class _Fmt(dict):
    """str.format_map helper that leaves unknown {placeholders} alone."""

    def __missing__(self, key):
        return "{" + key + "}"


# --------------------------------------------------------------------------
# the launcher database (data/compat/launchers.json)
# --------------------------------------------------------------------------

_launchers_db = None


def _valid_launchers_db(db):
    return isinstance(db, dict) and db.get("schema_version") == 1 and isinstance(db.get("launchers"), dict)


def load_launchers_db(force=False):
    """{"schema_version": 1, "launchers": {id: recipe}}: the bundled launchers.json or the
    downloaded copy, whichever is newer by its `updated` date. Never touches the network."""
    global _launchers_db
    if _launchers_db is not None and not force:
        return _launchers_db
    best = None
    for p in (LAUNCHERS_DB, LAUNCHERS_CACHE):
        db = _read_json(p) if p and p.exists() else None
        if _valid_launchers_db(db) and (best is None or
                                        (db.get("updated") or "") > (best.get("updated") or "")):
            best = db
    _launchers_db = best or {"schema_version": 1, "launchers": {}}
    return _launchers_db


def refresh_launchers_db(force=False):
    """Download launchers.json from the project repository into the cache when the cached copy
    is older than a day; a copy that fails validate_launchers_db() is rejected. Returns True
    when a new copy arrived. Meant for a background thread."""
    if not force and LAUNCHERS_CACHE.exists() and \
            time.time() - LAUNCHERS_CACHE.stat().st_mtime < LAUNCHERS_MAX_AGE:
        return False
    data = _http_get(LAUNCHERS_URL, 30)
    db = json.loads(data)
    errors = validate_launchers_db(db)
    if errors:
        raise ValueError("the downloaded launcher database is invalid: " + "; ".join(errors[:3]))
    LAUNCHERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHERS_CACHE.write_bytes(data)
    load_launchers_db(force=True)
    return True


def launcher_ids(db=None):
    return sorted((db or load_launchers_db()).get("launchers") or {})


_LAUNCHER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _prefix_relative(path):
    """True for a non-empty, relative path without '..' components (what may be joined onto
    a prefix or a cache directory)."""
    s = str(path or "")
    return bool(s) and not s.startswith(("/", "\\")) and ".." not in s.replace("\\", "/").split("/")


def _recipe_path_errors(where, r):
    """The recipe fields that become file system paths, URLs or commands, checked before
    anything acts on them (both validate_launchers_db() and launcher_recipe() use this, so a
    user override or a downloaded database cannot point outside the launcher's own directories)."""
    errors = []
    inst = r.get("installer") if isinstance(r.get("installer"), dict) else {}
    if not str(inst.get("url", "")).startswith("https://"):
        errors.append(f"{where}.installer.url: must be an https:// URL")
    if not inst.get("filename") or "/" in str(inst.get("filename")) or "\\" in str(inst.get("filename")) \
            or str(inst.get("filename")) in (".", ".."):
        errors.append(f"{where}.installer.filename: a bare file name")
    for i, alt in enumerate(inst.get("alternatives") or []):
        if not isinstance(alt, dict) or not str(alt.get("url", "")).startswith("https://") \
                or not alt.get("filename") or "/" in str(alt.get("filename")) or "\\" in str(alt.get("filename")):
            errors.append(f"{where}.installer.alternatives[{i}]: an object with an https url and a bare filename")
    pf = r.get("prefix") if isinstance(r.get("prefix"), dict) else {}
    for f in pf.get("files") or []:
        if not isinstance(f, dict) or not _prefix_relative(f.get("path")) or not isinstance(f.get("content"), str):
            errors.append(f"{where}.prefix.files: entries need a prefix-relative path and a content string")
    exe = r.get("exe") if isinstance(r.get("exe"), str) else ""
    if not _prefix_relative(exe):
        errors.append(f"{where}.exe: a path relative to the prefix")
    globs = r.get("exe_glob") or []
    if not isinstance(globs, list) or not all(isinstance(g, str) and _prefix_relative(g) for g in globs):
        errors.append(f"{where}.exe_glob: a list of prefix-relative glob patterns")
    return errors


def launcher_recipe(lid, db=None):
    """The recipe for one launcher ⊕ the user's override file (~/.config/xodus-launcher/launchers/<id>.json).
    Raises ValueError for an unknown id or a recipe whose paths / URLs are unsafe."""
    db = db or load_launchers_db()
    if not isinstance(lid, str) or not _LAUNCHER_ID_RE.match(lid):
        raise ValueError(f"invalid launcher id {lid!r}")
    recipe = (db.get("launchers") or {}).get(lid)
    if not isinstance(recipe, dict):
        known = ", ".join(launcher_ids(db)) or "none"
        raise ValueError(f"unknown launcher {lid!r} (known: {known})")
    user = _read_json(LAUNCHERS_USER_DIR / f"{lid}.json") or {}
    out = _merge(recipe, user)
    problems = _recipe_path_errors(f"launchers[{lid}]", out)
    if problems:
        raise ValueError(f"the {lid} recipe" + (" (with your override)" if user else "") + " is invalid: "
                         + "; ".join(problems))
    out["id"] = lid
    out["_user"] = user
    return out


def launcher_for_publisher(publisher, db=None):
    """Id of the recipe whose publisher_match prefixes fit a catalog publisher name
    ("Ubisoft Entertainment" -> ubisoft-connect), else "". Only recipes that list prefixes
    take part: Rockstar's Game Pass titles are Store packages, so it lists none."""
    pub = re.sub(r"\s+", " ", str(publisher or "")).strip().lower()
    if not pub:
        return ""
    db = db or load_launchers_db()
    for lid in launcher_ids(db):
        r = db["launchers"].get(lid) or {}
        for pat in (r.get("publisher_match") or []) if isinstance(r, dict) else []:
            pat = str(pat).strip().lower()
            if pat and (pub == pat or pub.startswith(pat + " ") or pub.startswith(pat)):
                return lid
    return ""


def launcher_for_title(entry, cfg, log=print):
    """ThirdPartyLauncher for a compatibility entry with a "launcher" field, else None."""
    lid = (entry or {}).get("launcher")
    if not lid:
        return None
    try:
        return ThirdPartyLauncher(lid, cfg, log)
    except ValueError as e:
        log(f"launchers: {e}")
        return None


_RECIPE_TYPES = {"name": str, "installer": dict, "runtime": dict, "prefix": dict, "exe": str,
                 "launch": dict, "installed_games": dict, "signin": dict, "sources": list,
                 "confidence": str}


def validate_launchers_db(db):
    """Schema check used by scripts/compat-seed.py --check. Returns a list of error strings."""
    errors = []
    if not _valid_launchers_db(db):
        return ["launchers.json: schema_version must be 1 and launchers an object"]
    for k in ("generated", "updated"):
        if not DATE_RE.match(str(db.get(k, ""))):
            errors.append(f"launchers.json: {k}: use YYYY-MM-DD")
    for lid, r in db["launchers"].items():
        where = f"launchers[{lid}]"
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", lid):
            errors.append(f"{where}: ids are lowercase letters, digits and dashes")
        if not isinstance(r, dict):
            errors.append(f"{where}: must be an object")
            continue
        for k, t in _RECIPE_TYPES.items():
            if not isinstance(r.get(k), t):
                errors.append(f"{where}: {k} must be a {t.__name__}")
        if not isinstance(r.get("publisher", ""), str):
            errors.append(f"{where}.publisher: a string")
        pm = r.get("publisher_match", [])
        if not isinstance(pm, list) or not all(isinstance(p, str) and p.strip() for p in pm):
            errors.append(f"{where}.publisher_match: a list of non-empty strings")
        errors += _recipe_path_errors(where, r)
        inst = r.get("installer") if isinstance(r.get("installer"), dict) else {}
        if not isinstance(inst.get("silent_args", []), list):
            errors.append(f"{where}.installer.silent_args: must be a list")
        sha = inst.get("sha256")
        if sha is not None and not re.match(r"^[0-9a-f]{64}$", str(sha)):
            errors.append(f"{where}.installer.sha256: 64 lowercase hex digits or null")
        if inst.get("wait", "exit") not in ("exit", "exe"):
            errors.append(f"{where}.installer.wait: exit or exe")
        if not isinstance(inst.get("success_codes", [0]), list) or \
                not all(isinstance(c, int) and not isinstance(c, bool) for c in inst.get("success_codes", [0])):
            errors.append(f"{where}.installer.success_codes: a list of integers")
        for k in ("timeout_seconds", "settle_seconds"):
            v = inst.get(k, 0)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                errors.append(f"{where}.installer.{k}: a non-negative integer")
        if not isinstance(inst.get("interactive", False), bool):
            errors.append(f"{where}.installer.interactive: true or false")
        if not isinstance(inst.get("interactive_note", ""), str):
            errors.append(f"{where}.installer.interactive_note: a string")
        rt = r.get("runtime") if isinstance(r.get("runtime"), dict) else {}
        if rt.get("kind") != "ge-proton":
            errors.append(f"{where}.runtime.kind: only ge-proton is implemented")
        if rt.get("min_version") is not None and not GE_TAG_RE.match(str(rt.get("min_version"))):
            errors.append(f"{where}.runtime.min_version: GE-ProtonX-Y or null")
        if not all(isinstance(v, str) and v for v in rt.get("prefer_tools", []) or []):
            errors.append(f"{where}.runtime.prefer_tools: a list of directory-name prefixes")
        st = rt.get("steam_tool")
        if st is not None and not (isinstance(st, dict) and isinstance(st.get("name"), str) and isinstance(st.get("appid"), int)):
            errors.append(f"{where}.runtime.steam_tool: {{name, appid}}")
        relz = rt.get("release")
        if relz is not None and not (isinstance(relz, dict) and re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", str(relz.get("repo", "")))
                                     and re.match(r"^[A-Za-z0-9._-]+$", str(relz.get("tag", "")))):
            errors.append(f"{where}.runtime.release: {{repo: owner/name, tag: release tag}}")
        pf = r.get("prefix") if isinstance(r.get("prefix"), dict) else {}
        if not all(isinstance(v, str) for v in pf.get("winetricks", [])):
            errors.append(f"{where}.prefix.winetricks: a list of verbs")
        for k in ("dll_overrides", "env"):
            if not isinstance(pf.get(k, {}), dict) or not all(isinstance(v, str) for v in pf.get(k, {}).values()):
                errors.append(f"{where}.prefix.{k}: must be an object of strings")
        for reg in pf.get("registry", []):
            if not isinstance(reg, dict) or not reg.get("key"):
                errors.append(f"{where}.prefix.registry: every entry needs a key")
        if pf.get("arch", "win64") not in ("win64", "win32"):
            errors.append(f"{where}.prefix.arch: win64 or win32")
        la = r.get("launch") if isinstance(r.get("launch"), dict) else {}
        for k in ("open_args", "game_args", "install_args"):
            if not isinstance(la.get(k, []), list) or not all(isinstance(a, str) for a in la.get(k, [])):
                errors.append(f"{where}.launch.{k}: must be a list of strings")
        if la.get("game_url") is not None and not isinstance(la.get("game_url"), str):
            errors.append(f"{where}.launch.game_url: a string or null")
        ig = r.get("installed_games") if isinstance(r.get("installed_games"), dict) else {}
        if ig.get("method") not in ("registry", "directory"):
            errors.append(f"{where}.installed_games.method: registry or directory")
        if ig.get("scanner") is not None and ig.get("scanner") not in _SCANNERS:
            errors.append(f"{where}.installed_games.scanner: one of {', '.join(sorted(_SCANNERS))}")
        si = r.get("signin") if isinstance(r.get("signin"), dict) else {}
        if not si.get("steps") or not all(isinstance(s, str) and s.strip() for s in si.get("steps", [])):
            errors.append(f"{where}.signin.steps: a non-empty list of strings")
        if not isinstance(si.get("summary", ""), str) or not isinstance(si.get("xbox_link", ""), str):
            errors.append(f"{where}.signin.summary / xbox_link: strings")
        if not isinstance(si.get("notes", []), list) or not all(isinstance(s, str) for s in si.get("notes", [])):
            errors.append(f"{where}.signin.notes: a list of strings")
        if not isinstance(si.get("game_pass_library", False), bool):
            errors.append(f"{where}.signin.game_pass_library: true or false")
        if r.get("confidence") not in ("low", "medium", "high"):
            errors.append(f"{where}.confidence: low, medium or high")
        if not r.get("sources"):
            errors.append(f"{where}.sources: cite at least one source")
        if not isinstance(r.get("known_issues", []), list):
            errors.append(f"{where}.known_issues: must be a list")
    return errors


# --------------------------------------------------------------------------
# Wine registry files (system.reg / user.reg) and Windows paths
# --------------------------------------------------------------------------

_REG_KEY_RE = re.compile(r"^\[(.*)\](?:\s+\d+)?\s*$")
_REG_VALUE_RE = re.compile(r'^(?:"((?:[^"\\]|\\.)*)"|@)=(.*)$')
_REG_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t", "0": "\0", "a": "\a",
                "b": "\b", "v": "\v", "f": "\f"}


def _reg_unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            i += 2
            if n == "x":
                m = re.match(r"[0-9a-fA-F]{1,4}", s[i:])
                if m:
                    out.append(chr(int(m.group(0), 16)))
                    i += len(m.group(0))
                    continue
            out.append(_REG_ESCAPES.get(n, n))
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _reg_decode(data):
    """A value's right-hand side: "string", str(N):"string", dword:hex, hex[(N)]:aa,bb -> str / int / bytes."""
    if data.startswith('"') and data.endswith('"') and len(data) >= 2:
        return _reg_unescape(data[1:-1])
    m = re.match(r'^str\(\d+\):"(.*)"$', data)
    if m:
        return _reg_unescape(m.group(1))
    if data.startswith("dword:"):
        try:
            return int(data[6:], 16)
        except ValueError:
            return data
    m = re.match(r"^hex(?:\(\d+\))?:(.*)$", data)
    if m:
        try:
            return bytes(int(b, 16) for b in m.group(1).replace("\\", "").split(",") if b.strip())
        except ValueError:
            return data
    return data


def parse_wine_reg(path):
    """<prefix>/system.reg or user.reg -> {key path lowercased: {"path": original, "values": {name: value}}}.
    Key paths use single backslashes and are relative to the file's hive."""
    keys = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys
    current = None
    pending = ""
    for raw in text.splitlines():
        if pending:
            raw = pending + raw.strip()
            pending = ""
        if raw.endswith("\\") and not raw.endswith("\\\\") and current is not None and "=" in raw:
            pending = raw[:-1]
            continue
        line = raw.rstrip()
        if not line or line.startswith(";;") or line.startswith("#"):
            continue
        m = _REG_KEY_RE.match(line)
        if m:
            path_ = _reg_unescape(m.group(1))
            current = {"path": path_, "values": {}}
            keys[path_.lower()] = current
            continue
        m = _REG_VALUE_RE.match(line)
        if m and current is not None:
            name = _reg_unescape(m.group(1)) if m.group(1) is not None else ""
            current["values"][name] = _reg_decode(m.group(2))
    return keys


_HIVES = {"hklm": "system.reg", "hkey_local_machine": "system.reg", "hkcu": "user.reg",
          "hkey_current_user": "user.reg", "hkcr": "system.reg", "hkey_classes_root": "system.reg"}


def _split_hive(key):
    """"HKLM\\Software\\X" -> ("system.reg", "Software\\X"); HKCR maps onto Software\\Classes."""
    hive, _, rest = key.replace("/", "\\").partition("\\")
    h = hive.lower()
    if h not in _HIVES:
        return "system.reg", key.replace("/", "\\")
    if h in ("hkcr", "hkey_classes_root"):
        rest = "Software\\Classes\\" + rest
    return _HIVES[h], rest


def _reg_file(prefix, key):
    file_, rest = _split_hive(key)
    return parse_wine_reg(Path(prefix) / file_), rest


def reg_values(prefix, key):
    """Values of one key ({name: value}) from the prefix's registry files, or None."""
    reg, rest = _reg_file(prefix, key)
    k = reg.get(rest.lower())
    return dict(k["values"]) if k else None


def reg_subkeys(prefix, key):
    """[(subkey name, {name: value})] for the direct children of key (case-insensitive)."""
    reg, rest = _reg_file(prefix, key)
    base = rest.lower().strip("\\") + "\\"
    out = []
    for lk, k in reg.items():
        if lk.startswith(base) and "\\" not in lk[len(base):]:
            out.append((k["path"][len(base):], dict(k["values"])))
    out.sort(key=lambda t: t[0].lower())
    return out


def _val(values, name, default=None):
    """Case-insensitive value lookup."""
    for k, v in (values or {}).items():
        if k.lower() == name.lower():
            return v
    return default


def win_to_host(prefix, path):
    """"C:/Program Files/X" or "Z:\\home\\me" -> the host Path (through dosdevices), or None."""
    if not path or not isinstance(path, str):
        return None
    p = path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/?(.*)$", p)
    if not m:
        return Path(p) if p.startswith("/") else None
    drive, rest = m.group(1).lower(), m.group(2)
    if drive == "c":
        root = Path(prefix) / "drive_c"
    elif drive == "z":
        root = Path("/")
    else:
        link = Path(prefix) / "dosdevices" / f"{drive}:"
        try:
            root = link.resolve(strict=True)
        except OSError:
            return None
    return root / rest.strip("/") if rest.strip("/") else root


# --------------------------------------------------------------------------
# GE-Proton (GloriousEggroll/proton-ge-custom releases)
# --------------------------------------------------------------------------

def ge_version_key(tag):
    m = GE_TAG_RE.match(tag or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _ge_satisfies(tag, min_version):
    return not min_version or ge_version_key(tag) >= ge_version_key(min_version)


def _ge_root():
    return DATA_DIR / "ge-proton"


def ge_proton_installed():
    """Tag of the GE-Proton build under DATA_DIR/ge-proton (the one .current names, else the
    newest with files/bin/wine), or None."""
    root = _ge_root()
    try:
        cur = (root / ".current").read_text().strip()
    except OSError:
        cur = ""
    if cur and (root / cur / "files/bin/wine").exists():
        return cur
    try:
        tags = [d.name for d in root.iterdir() if GE_TAG_RE.match(d.name) and (d / "files/bin/wine").exists()]
    except OSError:
        return None
    return max(tags, key=ge_version_key) if tags else None


def ge_proton_wine(tag=None):
    tag = tag or ge_proton_installed()
    return str(_ge_root() / tag / "files/bin/wine") if tag else ""


def ge_proton_dir(wine):
    """<GE>/files/bin/wine -> <GE>."""
    return Path(wine).resolve().parent.parent.parent


_LDCONFIG_CACHE = {}


def _ldconfig_arches(soname):
    """{"32", "64"}: the word sizes `ldconfig -p` lists soname for (glibc hosts; empty when
    ldconfig is missing). 64-bit entries are tagged x86-64 / AArch64 / ... in the parentheses."""
    if "text" not in _LDCONFIG_CACHE:
        try:
            _LDCONFIG_CACHE["text"] = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True,
                                                     timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            _LDCONFIG_CACHE["text"] = ""
    found = set()
    for line in _LDCONFIG_CACHE["text"].splitlines():
        name, _, rest = line.strip().partition(" ")
        if name != soname:
            continue
        tags = rest.split("=>", 1)[0].lower()
        found.add("64" if ("64" in tags or "aarch" in tags) else "32")
    return found


def host_audio_note(ge):
    """A warning when this GE-Proton ships 32-bit unix drivers but the host has 64-bit and no
    32-bit libpulse.so.0, else "". GE-Proton is run outside the Steam Linux Runtime here, so its
    32-bit winepulse.so needs the host's 32-bit libpulse; without it mmdevapi cannot load
    winepulse.drv in a 32-bit process (STATUS_DLL_NOT_FOUND), falls back to winealsa.drv (or
    runs without audio), and 32-bit launcher clients and games have no sound."""
    if not (Path(ge) / "files/lib/wine/i386-unix/winepulse.so").exists():
        return ""
    arches = _ldconfig_arches("libpulse.so.0")
    if "64" in arches and "32" not in arches:
        return ("this host has no 32-bit libpulse.so.0 (Arch: lib32-libpulse, Debian/Ubuntu: libpulse0:i386, "
                "Fedora: pulseaudio-libs.i686): 32-bit processes in the launcher prefixes run without audio")
    return ""


def _ge_tag_of_dir(d):
    try:
        words = (Path(d) / "version").read_text().split()
    except OSError:
        return ""
    return words[-1] if words and GE_TAG_RE.match(words[-1]) else ""


COMPAT_TOOL_DIRS = (HOME / ".local/share/Steam/compatibilitytools.d", HOME / ".steam/root/compatibilitytools.d",
                    HOME / ".steam/steam/compatibilitytools.d", HOME / ".local/share/umu/compatibilitytools",
                    HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d",
                    Path("/usr/share/steam/compatibilitytools.d"))


def find_proton_tool(prefixes):
    """A Proton-style build (files/bin/wine) already installed by Steam / umu whose directory name
    starts with one of the prefixes, in prefix order, newest copy first. Lets a recipe prefer e.g.
    Proton-CachyOS while GE-Proton lags behind a launcher update. Returns the directory or None."""
    found = []
    bases = list(COMPAT_TOOL_DIRS) + [_ge_root()]
    try:  # Valve's own Proton builds ("Proton - Experimental", "Proton 10.0") live in the Steam libraries
        bases += [lib / "steamapps/common" for lib in steam_libraries()]
    except Exception:
        pass
    for base in bases:
        try:
            dirs = list(base.iterdir())
        except OSError:
            continue
        for d in dirs:
            if (d / "files/bin/wine").exists():
                try:
                    found.append((d.name, d.resolve(), (d / "files/bin/wine").stat().st_mtime))
                except OSError:
                    continue
    for pref in prefixes or []:
        hits = sorted((f for f in found if f[0].startswith(str(pref))), key=lambda f: f[2], reverse=True)
        if hits:
            return hits[0][1]
    return None


def ensure_proton_release(cfg, repo, tag, log=print, progress=None, cancel_event=None):
    """A specific Proton release of a GitHub repo with GE's asset layout (<tag>[-x86_64].tar.gz +
    .sha512sum), e.g. Open-Wine-Components/umu-proton UMU-Proton-10.0-4, installed under
    DATA_DIR/ge-proton/<tag> (or reused from Steam / umu). Returns the directory."""
    root = _ge_root()
    if (root / tag / "files/bin/wine").exists():
        return root / tag
    tool = find_proton_tool([tag])
    if tool:
        return tool
    if not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repo) or not re.match(r"^[A-Za-z0-9._-]+$", tag):
        raise RuntimeError(f"invalid Proton release {repo} {tag}")
    rel = _http_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    _install_proton_release(rel, tag, log, progress or (lambda *a: None), cancel_event, name=tag)
    return root / tag


def _foreign_ge_protons():
    """GE-Proton builds other tools installed (Steam's compatibilitytools.d, umu), newest first."""
    out, seen = [], set()
    for base in (HOME / ".local/share/Steam/compatibilitytools.d", HOME / ".steam/root/compatibilitytools.d",
                 HOME / ".steam/steam/compatibilitytools.d", HOME / ".local/share/umu/compatibilitytools",
                 HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d",
                 Path("/usr/share/steam/compatibilitytools.d")):
        try:
            dirs = list(base.iterdir())
        except OSError:
            continue
        for d in dirs:
            try:
                real = d.resolve()
            except OSError:
                continue
            if real in seen or not (d / "files/bin/wine").exists():
                continue
            seen.add(real)
            tag = _ge_tag_of_dir(d)
            if tag:
                out.append((tag, d))
    out.sort(key=lambda t: ge_version_key(t[0]), reverse=True)
    return out


def _ge_release_assets(rel):
    """(tag, tarball asset, checksum asset) with GE's naming: <tag>-x86_64.tar.gz since
    GE-Proton11-4, <tag>.tar.gz before; the checksum file is the same stem + .sha512sum."""
    tag = rel.get("tag_name") or ""
    assets = [a for a in rel.get("assets") or [] if isinstance(a, dict)]
    tar = next((a for a in assets if str(a.get("name", "")).endswith("-x86_64.tar.gz")), None) \
        or next((a for a in assets if a.get("name") == f"{tag}.tar.gz"), None)
    if not tar or not tar.get("browser_download_url"):
        raise RuntimeError(f"GE-Proton release {tag or '?'} has no x86_64 tarball")
    stem = tar["name"][:-len(".tar.gz")]
    chk = next((a for a in assets if a.get("name") == stem + ".sha512sum"), None)
    if not chk or not chk.get("browser_download_url"):
        raise RuntimeError(f"GE-Proton release {tag} has no {stem}.sha512sum checksum file")
    return tag, tar, chk


def extract_tarball(tpath, dest, progress=None, stage="runtime", cancel_event=None):
    """Extract tpath under dest with tarfile's 'data' filter (no absolute paths, no traversal,
    no links escaping dest). Returns the number of entries."""
    tpath, dest = Path(tpath), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    size = tpath.stat().st_size
    n, last = 0, 0.0
    with open(tpath, "rb") as raw, tarfile.open(fileobj=raw, mode="r:*") as tar:
        for member in tar:
            _check_cancel(cancel_event)
            try:
                tar.extract(member, dest, set_attrs=not member.isdir(), filter="data")
            except tarfile.FilterError as e:
                raise RuntimeError(f"{tpath.name} contains an unsafe entry ({e}); refusing to extract it") from None
            n += 1
            now = time.monotonic()
            if progress and now - last > 0.5:
                last = now
                progress(stage, min(99, int(raw.tell() * 100 / size)) if size else None,
                         f"Extracting {tpath.name}: {n} entries")
    return n


def ensure_ge_proton(cfg, log=print, progress=None, min_version=None, cancel_event=None, latest=False):
    """Path of GE-Proton's files/bin/wine under DATA_DIR/ge-proton/<tag>/, downloading the
    latest release (sha512-verified) when nothing suitable is installed. min_version is a
    "GE-ProtonX-Y" tag. latest=True (Settings > Update) asks GitHub for the newest release and
    installs it unless the installed build already is it. progress(stage, pct, text) uses the
    stage "runtime"."""
    progress = progress or (lambda stage, pct, text: None)
    root = _ge_root()
    tag = ge_proton_installed()
    rel = None
    if latest:
        progress("runtime", None, "Looking up the latest GE-Proton release")
        log("Looking up the latest GE-Proton release ...")
        rel = _http_json(GE_RELEASES_API)
        newest = _ge_release_assets(rel)[0]
        if tag and ge_version_key(tag) >= ge_version_key(newest):
            log(f"GE-Proton {tag} is already the latest release")
            progress("runtime", 100, f"GE-Proton {tag} is up to date")
            return ge_proton_wine(tag)
        log(f"GE-Proton {newest} is available" + (f" ({tag} is installed)" if tag else ""))
        min_version = newest  # only the newest build (or a copy of it Steam / umu has) will do
    elif tag and _ge_satisfies(tag, min_version):
        return ge_proton_wine(tag)
    elif tag:
        log(f"GE-Proton {tag} is installed but {min_version} or newer is wanted")
    if (cfg or {}).get("ge_proton_reuse_existing", True):
        for ftag, fdir in _foreign_ge_protons():
            if not _ge_satisfies(ftag, min_version):
                continue
            root.mkdir(parents=True, exist_ok=True)
            link = root / ftag
            if link.is_symlink() and not link.exists():
                link.unlink()  # dangling: the build it pointed at moved or was removed
            if link.exists() and not link.is_symlink():
                log(f"{link} exists but is not a usable GE-Proton build; not linking {fdir} over it")
                continue
            if not link.exists():
                link.symlink_to(fdir.resolve(), target_is_directory=True)
            (root / ".current").write_text(ftag + "\n")
            log(f"Using the GE-Proton {ftag} already installed at {fdir} (linked from {link})")
            return ge_proton_wine(ftag)
    if rel is None:
        progress("runtime", None, "Looking up the latest GE-Proton release")
        log("Looking up the latest GE-Proton release ...")
        rel = _http_json(GE_RELEASES_API)
    tag, tar, chk = _ge_release_assets(rel)
    if not _ge_satisfies(tag, min_version):
        log(f"WARNING: the latest GE-Proton is {tag}, older than the {min_version} this launcher wants")
    root.mkdir(parents=True, exist_ok=True)
    if (root / tag / "files/bin/wine").exists():
        (root / ".current").write_text(tag + "\n")
        return ge_proton_wine(tag)
    _install_proton_release(rel, tag, log, progress, cancel_event)
    (root / ".current").write_text(tag + "\n")
    log(f"GE-Proton {tag} installed in {root / tag}")
    return ge_proton_wine(tag)


def _install_proton_release(rel, tag, log, progress, cancel_event, name="GE-Proton"):
    """Download, verify (sha512sum asset + GitHub digest) and extract a Proton release into
    DATA_DIR/ge-proton/<tag>."""
    root = _ge_root()
    root.mkdir(parents=True, exist_ok=True)
    _, tar, chk = _ge_release_assets(rel)
    free = shutil.disk_usage(root).free
    if free < GE_MIN_FREE_BYTES:
        raise RuntimeError(f"not enough free space in {root} for GE-Proton ({human_size(free)} free, "
                           f"about {human_size(GE_MIN_FREE_BYTES)} needed)")
    cache = CACHE_DIR / "ge-proton"
    tpath = cache / tar["name"]
    log(f"Downloading {tar['name']} ({human_size(tar.get('size'))}) ...")
    download_file(tar["browser_download_url"], tpath, progress, "runtime", cancel_event, log)
    expected = _http_get(chk["browser_download_url"]).decode(errors="replace").split()
    if not expected or not re.match(r"^[0-9a-f]{128}$", expected[0]):
        tpath.unlink(missing_ok=True)
        raise RuntimeError(f"{chk['name']} is not a sha512sum file")
    progress("runtime", None, "Verifying the checksum")
    sums = hash_file(tpath, ("sha512", "sha256"))
    if sums["sha512"] != expected[0]:
        tpath.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-512 checksum mismatch for {tar['name']} (the file was deleted; try again)")
    digest = str(tar.get("digest") or "")
    if digest.startswith("sha256:") and digest[7:] != sums["sha256"]:
        tpath.unlink(missing_ok=True)
        raise RuntimeError(f"{tar['name']} does not match the SHA-256 digest GitHub reports for it")
    log("Checksum OK")
    staging = root / f".staging-{tag}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        extract_tarball(tpath, staging, progress, "runtime", cancel_event)
        tops = [d for d in staging.iterdir()]
        if len(tops) != 1 or not (tops[0] / "files/bin/wine").exists():
            raise RuntimeError(f"{tar['name']} does not look like a Proton archive (no files/bin/wine)")
        target = root / tag
        if target.is_symlink() or target.exists():
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
        tops[0].rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    tpath.unlink(missing_ok=True)
    progress("runtime", 100, f"GE-Proton {tag} ready")
    return ge_proton_wine(tag)


# --------------------------------------------------------------------------
# installed-game scanners (one per launcher; the recipe names it in installed_games.scanner)
# --------------------------------------------------------------------------

def _game(lid, gid, name, host_dir, win_dir, **extra):
    g = {"id": str(gid), "name": name or str(gid), "dir": str(host_dir) if host_dir else "",
         "win_dir": win_dir or "", "launcher": lid}
    g.update(extra)
    return g


def _scan_ubisoft(launcher):
    """HKLM\\Software\\Wow6432Node\\Ubisoft\\Launcher\\Installs\\<id> -> InstallDir; the name from the
    matching Uninstall entry, else the folder name; complete when uplay_install.state starts with 0x0A."""
    prefix = launcher.prefix
    base = launcher.recipe["installed_games"].get("registry_key") or "HKLM\\Software\\Wow6432Node\\Ubisoft\\Launcher\\Installs"
    games = []
    for sub, vals in reg_subkeys(prefix, base):
        if not sub.isdigit():
            continue
        win_dir = str(_val(vals, "InstallDir") or "")
        host = win_to_host(prefix, win_dir)
        uninstall = reg_values(prefix, f"HKLM\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Uplay Install {sub}")
        name = str(_val(uninstall, "DisplayName") or "") or Path(win_dir.rstrip("/\\")).name or f"Ubisoft game {sub}"
        complete = False
        try:
            with open(host / "uplay_install.state", "rb") as f:
                complete = f.read(1) == b"\x0a"
        except (OSError, TypeError):
            pass
        games.append(_game(launcher.id, sub, name, host, win_dir, complete=complete))
    return games


def _parse_installerdata(path):
    """EA __Installer/installerdata.xml (UTF-8 or UTF-16) -> (content id, title) or (None, None)."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, None
    cid = None
    for el in root.iter():
        if el.tag.split("}")[-1] == "contentID" and el.text and el.text.strip():
            cid = el.text.strip()
            break
    titles = [el for el in root.iter() if el.tag.split("}")[-1] == "gameTitle" and el.text and el.text.strip()]
    name = next((t.text.strip() for t in titles if t.get("locale", "").lower() == "en_us"),
                titles[0].text.strip() if titles else None)
    return cid, name


def _scan_ea(launcher):
    """<prefix>/drive_c/Program Files/EA Games/*/__Installer/installerdata.xml (plus the older
    Origin Games folders and the download folder chosen in the EA app's user_<id>.ini)."""
    prefix = launcher.prefix
    roots = [prefix / "drive_c/Program Files/EA Games", prefix / "drive_c/Program Files (x86)/EA Games",
             prefix / "drive_c/Program Files/Origin Games", prefix / "drive_c/Program Files (x86)/Origin Games"]
    for ini in prefix.glob("drive_c/users/*/AppData/Local/Electronic Arts/EA Desktop/user_*.ini"):
        try:
            for line in ini.read_text(errors="replace").splitlines():
                if line.lower().startswith("user.downloadinplacedir"):
                    host = win_to_host(prefix, line.split("=", 1)[1].strip().strip('"'))
                    if host:
                        roots.append(host)
        except OSError:
            pass
    games, seen = [], set()
    for root in roots:
        try:
            dirs = sorted(d for d in root.iterdir() if d.is_dir())
        except OSError:
            continue
        for d in dirs:
            xml = d / "__Installer" / "installerdata.xml"
            if not xml.exists():
                continue
            try:
                cid, name = _parse_installerdata(xml)
            except OSError:
                continue
            gid = cid or d.name
            if gid in seen:
                continue
            seen.add(gid)
            win_dir = "C:" + "/" + d.relative_to(prefix / "drive_c").as_posix() if (prefix / "drive_c") in d.parents else ""
            games.append(_game(launcher.id, gid, name or d.name, d, win_dir, content_id=cid))
    return games


# Battle.net products: product.db product_code -> (uid, --exec launch code, name)
BNET_PRODUCTS = {
    "fenris": ("fenris", "Fen", "Diablo IV"), "d3": ("diablo3", "D3", "Diablo III"),
    "pro": ("prometheus", "Pro", "Overwatch 2"), "s1": ("s1", "S1", "StarCraft Remastered"),
    "s2": ("s2", "S2", "StarCraft II"), "rtro": ("rtro", "RTRO", "Blizzard Arcade Collection"),
    "hero": ("heroes", "Hero", "Heroes of the Storm"), "w1r": ("w1r", "W1R", "Warcraft I: Remastered"),
    "w2r": ("w2r", "W2R", "Warcraft II: Remastered"), "w3": ("w3", "W3", "Warcraft III: Reforged"),
    "hsb": ("hs_beta", "WTCG", "Hearthstone"), "wlby": ("wlby", "WLBY", "Crash Bandicoot 4: It's About Time"),
    "osi": ("osi", "OSI", "Diablo II: Resurrected"), "wow": ("wow", "WoW", "World of Warcraft"),
    "gryphon": ("gryphon", "GRY", "Warcraft Rumble"), "anbs": ("anbs", "ANBS", "Diablo Immortal"),
    "codhq": ("codhq", "CODHQ", "Call of Duty HQ"), "d1": ("d1", "D1", "Diablo"),
    # Xbox titles sold on Battle.net (uid and launch code from Playnite; product codes unverified)
    "aqua": ("aqua", "AQUA", "Avowed"), "aris": ("aris", "ARIS", "DOOM: The Dark Ages"),
    "scorpio": ("scorpio", "SCOR", "Sea of Thieves"), "libra": ("libra", "LBRA", "Tony Hawk's Pro Skater 3+4"),
    "arkansas": ("arkansas", "ARK", "The Outer Worlds 2"), "pinta": ("pinta", "PNTA", "Call of Duty: Modern Warfare III"),
}
BNET_SKIP = {"bna", "agent", "battle.net"}


def _bnet_lookup(product_code, uid):
    code, u = (product_code or "").lower(), (uid or "").lower()
    if code in BNET_PRODUCTS:
        return BNET_PRODUCTS[code]
    for row in BNET_PRODUCTS.values():
        if row[0].lower() == u or (u and u.startswith(row[1].lower())):
            return row
    return (uid, uid, uid)


def _pb_varint(buf, i):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _pb_fields(buf):
    """[(field number, wire type, value)] of one protobuf message (values: int or bytes)."""
    i, out = 0, []
    while i < len(buf):
        key, i = _pb_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _pb_varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _pb_varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        out.append((fn, wt, v))
    return out


def _pb_str(v):
    return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else str(v)


def parse_product_db(path):
    """Battle.net Agent product.db (protobuf) -> [{uid, product_code, install_path, play_region,
    installed, playable, version}] (Lutris lutris/util/battlenet/product_db.py layout)."""
    products = []
    try:
        data = Path(path).read_bytes()
    except OSError:
        return products
    try:
        for fn, wt, v in _pb_fields(data):
            if fn != 1 or wt != 2:
                continue
            p = {"uid": "", "product_code": "", "install_path": "", "play_region": "",
                 "installed": False, "playable": False, "version": ""}
            for f, w, val in _pb_fields(v):
                if f == 1 and w == 2:
                    p["uid"] = _pb_str(val)
                elif f == 2 and w == 2:
                    p["product_code"] = _pb_str(val)
                elif f == 3 and w == 2:
                    for sf, sw, sv in _pb_fields(val):
                        if sf == 1 and sw == 2:
                            p["install_path"] = _pb_str(sv)
                        elif sf == 2 and sw == 2:
                            p["play_region"] = _pb_str(sv)
                elif f == 4 and w == 2:
                    for sf, sw, sv in _pb_fields(val):
                        if sf == 1 and sw == 2:
                            for bf, bw, bv in _pb_fields(sv):
                                if bf == 1 and bw == 0:
                                    p["installed"] = bool(bv)
                                elif bf == 2 and bw == 0:
                                    p["playable"] = bool(bv)
                                elif bf == 7 and bw == 2:
                                    p["version"] = _pb_str(bv)
            products.append(p)
    except (IndexError, ValueError):
        pass  # truncated / unexpected: return what was decoded
    return products


def _scan_battlenet(launcher):
    """Agent/product.db first (what Lutris, Playnite and the Galaxy plugin read), then the
    Uninstall registry entries whose UninstallString carries --uid=<uid>."""
    prefix = launcher.prefix
    games, seen = [], set()
    for p in parse_product_db(prefix / "drive_c/ProgramData/Battle.net/Agent/product.db"):
        if p["product_code"].lower() in BNET_SKIP or p["uid"].lower() in BNET_SKIP:
            continue
        uid, code, name = _bnet_lookup(p["product_code"], p["uid"])
        if code in seen:
            continue
        seen.add(code)
        host = win_to_host(prefix, p["install_path"])
        games.append(_game(launcher.id, code, name, host, p["install_path"], uid=p["uid"] or uid,
                           product_code=p["product_code"], installed=p["installed"], playable=p["playable"],
                           version=p["version"]))
    for sub, vals in reg_subkeys(prefix, "HKLM\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"):
        m = re.search(r"Battle\.net.*--uid=(\S+)", str(_val(vals, "UninstallString") or ""))
        if not m:
            continue
        ruid = m.group(1).strip('"')
        if ruid.lower() in BNET_SKIP:
            continue
        uid, code, name = _bnet_lookup("", ruid)
        if code in seen:
            continue
        seen.add(code)
        win_dir = str(_val(vals, "InstallLocation") or "")
        games.append(_game(launcher.id, code, str(_val(vals, "DisplayName") or name), win_to_host(prefix, win_dir),
                           win_dir, uid=ruid, product_code="", installed=True, playable=True, version=""))
    return games


# Rockstar registry key name -> (title id, main exe); the public title_metadata.json has the rest
ROCKSTAR_TITLES = {
    "Grand Theft Auto V": ("gta5", "GTA5.exe"), "Grand Theft Auto V Enhanced": ("gta5_gen9", "GTA5_Enhanced.exe"),
    "Red Dead Redemption 2": ("rdr2", "RDR2.exe"), "Red Dead Redemption": ("rdr", "RDR.exe"),
    "LANoireMTL": ("lanoire", "LANoire.exe"), "LANoireVR": ("lanoirevr", ""), "MP3MTL": ("mp3", "MaxPayne3.exe"),
    "GTA: San Andreas": ("gtasa", "gta_sa.exe"), "Grand Theft Auto III": ("gta3", "gta3.exe"),
    "Grand Theft Auto: Vice City": ("gtavc", "gta-vc.exe"), "Bully": ("bully", "Bully.exe"),
    "Grand Theft Auto IV": ("gta4", "GTAIV.exe"), "GTA III - DE": ("gta3unreal", "Gameface\\Binaries\\Win64\\LibertyCity.exe"),
    "GTA Vice City - DE": ("gtavcunreal", "Gameface\\Binaries\\Win64\\ViceCity.exe"),
    "GTA San Andreas - DE": ("gtasaunreal", "Gameface\\Binaries\\Win64\\SanAndreas.exe"),
}
ROCKSTAR_SKIP = {"launcher", "rockstar games social club"}


def _scan_rockstar(launcher):
    """Subkeys of HKLM\\SOFTWARE\\WOW6432Node\\Rockstar Games with an InstallFolder* value
    (InstallFolderXboxPc = a Game Pass copy), plus the Uninstall entries with uninstall=<titleId>."""
    prefix = launcher.prefix
    base = launcher.recipe["installed_games"].get("registry_key") or "HKLM\\SOFTWARE\\WOW6432Node\\Rockstar Games"
    games, seen = [], set()
    for sub, vals in reg_subkeys(prefix, base):
        if sub.lower() in ROCKSTAR_SKIP:
            continue
        folders = {k: str(v) for k, v in vals.items() if k.lower().startswith("installfolder") and v}
        if not folders:
            continue
        win_dir = folders.get("InstallFolder") or next(iter(folders.values()))
        title_id, exe = ROCKSTAR_TITLES.get(sub, ("", ""))
        stores = sorted(k[len("InstallFolder"):].lower() or "rockstar" for k in folders)
        seen.add(sub.lower())
        games.append(_game(launcher.id, sub, sub, win_to_host(prefix, win_dir), win_dir, title_id=title_id,
                           exe=exe, stores=stores, game_pass="xboxpc" in stores))
    for sub, vals in reg_subkeys(prefix, "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"):
        m = re.search(r"(?:Launcher|uninstall)\.exe.+uninstall=(.+)$", str(_val(vals, "UninstallString") or ""))
        if not m or sub.lower() in seen or sub.lower() in ROCKSTAR_SKIP:
            continue
        win_dir = str(_val(vals, "InstallLocation") or "")
        seen.add(sub.lower())
        games.append(_game(launcher.id, sub, str(_val(vals, "DisplayName") or sub), win_to_host(prefix, win_dir),
                           win_dir, title_id=m.group(1).strip().strip('"'), exe="", stores=["rockstar"], game_pass=False))
    return games


RIOT_PRODUCTS = {"league_of_legends": ("League of Legends", "LeagueClient.exe"), "valorant": ("VALORANT", "VALORANT.exe"),
                 "bacon": ("Legends of Runeterra", "LoR.exe"), "lion": ("2XKO", "Lion.exe")}


def _parse_simple_yaml(path):
    """key: value lines (what Riot's product_settings.yaml holds) -> dict of strings."""
    out = {}
    try:
        for line in Path(path).read_text(errors="replace").splitlines():
            m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _scan_riot(launcher):
    """ProgramData/Riot Games/Metadata/<product>.<patchline>/*.product_settings.yaml, then the
    associated_client entries of RiotClientInstalls.json for anything the metadata misses."""
    prefix = launcher.prefix
    data = prefix / "drive_c/ProgramData/Riot Games"
    games, seen = [], set()
    try:
        meta_dirs = sorted(d for d in (data / "Metadata").iterdir() if d.is_dir() and "." in d.name)
    except OSError:
        meta_dirs = []
    for d in meta_dirs:
        product, _, patchline = d.name.partition(".")
        settings = _parse_simple_yaml(d / f"{d.name}.product_settings.yaml")
        win_dir = settings.get("product_install_full_path") or ""
        if not win_dir:
            continue  # only product_install_root left behind: uninstalled
        host = win_to_host(prefix, win_dir)
        name, exe = RIOT_PRODUCTS.get(product, (product, ""))
        seen.add(product)
        games.append(_game(launcher.id, product, name, host, win_dir, patchline=patchline, exe=exe,
                           installed=bool(host and host.exists())))
    installs = _read_json(data / "RiotClientInstalls.json") or {}
    for win_dir in (installs.get("associated_client") or {}):
        if Path(win_dir.rstrip("/\\")).name.lower() == "riot client":
            continue
        host = win_to_host(prefix, win_dir)
        product = next((p for p, (n, exe) in RIOT_PRODUCTS.items()
                        if host and exe and (host / exe).exists() or n.lower() == Path(win_dir.rstrip("/\\")).name.lower()),
                       Path(win_dir.rstrip("/\\")).name)
        if product in seen:
            continue
        seen.add(product)
        name, exe = RIOT_PRODUCTS.get(product, (product, ""))
        games.append(_game(launcher.id, product, name, host, win_dir, patchline="live", exe=exe,
                           installed=bool(host and host.exists())))
    return games


def _scan_generic(launcher):
    """Fallback for recipes without a dedicated scanner: registry subkeys with an Install* value,
    or the subdirectories of installed_games.directory."""
    prefix = launcher.prefix
    ig = launcher.recipe.get("installed_games") or {}
    games = []
    if ig.get("method") == "registry" and ig.get("registry_key"):
        for sub, vals in reg_subkeys(prefix, ig["registry_key"]):
            win_dir = next((str(v) for k, v in vals.items() if k.lower().startswith("install") and isinstance(v, str)), "")
            if win_dir:
                games.append(_game(launcher.id, sub, sub, win_to_host(prefix, win_dir), win_dir))
    elif ig.get("directory"):
        try:
            for d in sorted((prefix / ig["directory"]).iterdir()):
                if d.is_dir():
                    games.append(_game(launcher.id, d.name, d.name, d, ""))
        except OSError:
            pass
    return games


_SCANNERS = {"ubisoft": _scan_ubisoft, "ea": _scan_ea, "battlenet": _scan_battlenet,
             "rockstar": _scan_rockstar, "riot": _scan_riot, "generic": _scan_generic}


# --------------------------------------------------------------------------
# one third-party launcher in its own GE-Proton prefix
# --------------------------------------------------------------------------

class ThirdPartyLauncher:
    """A launcher recipe from launchers.json bound to its prefix DATA_DIR/prefix-<id>.

    log(str) receives progress lines; progress(stage, pct or None, text) the stages
    "runtime" (GE-Proton), "download" (installer), "prefix", "tweaks", "install", "done".
    Every failure is a RuntimeError with a readable message (Cancelled when cancel_event fires)."""

    def __init__(self, lid, cfg, log=print):
        self.recipe = launcher_recipe(lid)
        self.id = lid
        self.cfg = cfg or {}
        self.log = log
        self.name = self.recipe.get("name") or lid
        self._audio_note = None  # host_audio_note(), computed once GE-Proton is there

    # ---------- paths and environment ----------

    @property
    def prefix(self):
        return DATA_DIR / f"prefix-{self.id}"

    def _logfile(self, kind):
        return LOG_DIR / f"launcher-{self.id}-{kind}.log"

    def ge_dir(self):
        """The Proton build directory this launcher runs with (the one wine() selected, else the
        installed GE-Proton), or None (never downloads)."""
        if getattr(self, "_tool_dir", None):
            return self._tool_dir
        tag = ge_proton_installed()
        return _ge_root() / tag if tag else None

    def runtime_hint(self):
        """Readable message when none of the Proton builds the recipe needs is installed."""
        rt = self.recipe.get("runtime") or {}
        st = rt.get("steam_tool") or {}
        msg = f"{self.name} needs one of these Proton builds installed by Steam or umu: " + ", ".join(rt.get("prefer_tools") or [])
        if st:
            msg += f". In Steam, install \"{st['name']}\" (steam://install/{st['appid']}) and try again"
        return msg

    def use_umu(self):
        """Run through umu-launcher (Steam Linux Runtime container, what Lutris/Heroic do) when it
        is installed: Proton's 32-bit side needs libraries the host rarely has (Ubisoft Connect's
        UI thread overflowed its stack without them). Settings: thirdparty_runner = umu|wine."""
        return str((self.cfg or {}).get("thirdparty_runner", "umu")) == "umu" and bool(shutil.which("umu-run"))

    def runner_cmd(self, wine, exe, args=()):
        """(argv, env) to run a Windows exe in this prefix with the selected Proton build, in its
        own systemd scope when available (an out-of-memory kill then takes the client, not the
        launcher or the terminal it was started from)."""
        tool = Path(wine).parent.parent.parent
        self._tool_dir = tool
        if self.use_umu():
            env = self.env(runner="umu")
            env.update({"PROTONPATH": str(tool), "GAMEID": "umu-default", "STORE": "none"})
            cmd = ["umu-run", str(exe)] + [str(a) for a in args]
        else:
            cmd, env = [wine, str(exe)] + [str(a) for a in args], self.env()
        return scope_cmd(f"xodus-{self.id}") + cmd, env

    def wine(self):
        """GE-Proton's files/bin/wine for this launcher (downloads GE-Proton when needed)."""
        rt = self.recipe.get("runtime") or {}
        if rt.get("kind", "ge-proton") != "ge-proton":
            raise RuntimeError(f"{self.name}: runtime kind {rt.get('kind')!r} is not supported (only ge-proton)")
        tool = find_proton_tool(rt.get("prefer_tools"))
        if not tool and rt.get("release"):
            tool = ensure_proton_release(self.cfg, rt["release"]["repo"], rt["release"]["tag"], self.log)
        if not tool and rt.get("prefer_tools") and not rt.get("release"):
            raise RuntimeError(self.runtime_hint())
        wine = str(tool / "files/bin/wine") if tool else \
            ensure_ge_proton(self.cfg, self.log, min_version=rt.get("min_version"))
        if not wine or not Path(wine).exists():
            raise RuntimeError("GE-Proton is not installed and could not be downloaded")
        self._tool_dir = Path(wine).parent.parent.parent
        return wine

    def dll_overrides(self):
        """DXVK / VKD3D-Proton from the prefix, no menu entries from installers, no Bluetooth
        driver, plus the recipe's own overrides."""
        ov = {"winemenubuilder.exe": "d", "winebth.sys": "d"}
        for d in ("d3d8", "d3d9", "d3d10core", "d3d11", "dxgi", "d3d12", "d3d12core"):
            ov[d] = "n,b"
        ov.update({k: str(v) for k, v in ((self.recipe.get("prefix") or {}).get("dll_overrides") or {}).items()})
        return ";".join(f"{k}={v}" for k, v in ov.items())

    def audio_note(self):
        """host_audio_note() for the installed GE-Proton ("" when none is installed yet or the
        host is fine); logged once as a warning the first time it applies."""
        if self._audio_note is None:
            ge = self.ge_dir()
            if ge is None:
                return ""
            self._audio_note = host_audio_note(ge)
            if self._audio_note:
                self.log(f"WARNING: {self._audio_note}")
        return self._audio_note

    def env(self, extra=None, runner="wine"):
        """Full environment for running GE-Proton's wine in this prefix: what the proton
        script would set (PATH, LD_LIBRARY_PATH, WINEDLLPATH, esync/fsync/ntsync, quiet
        logging), the recipe's prefix.env and dll_overrides, Settings' extra_env, then extra.
        runner="umu" leaves the library wiring to umu-launcher / the proton script."""
        env = dict(os.environ)
        # a launcher started from a Steam shortcut must not look like a Steam game to Wine
        for k in ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID", "WINEARCH", "WINEDLLOVERRIDES"):
            env.pop(k, None)
        ge = self.ge_dir() if runner == "wine" else None
        if ge:
            files = ge / "files"
            env["PATH"] = os.pathsep.join([str(files / "bin"), str(ge / "protonfixes/files/bin"), env.get("PATH", "")])
            if env.get("LD_LIBRARY_PATH"):
                env["ORIG_LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH"]
            env["LD_LIBRARY_PATH"] = os.pathsep.join(p for p in (str(files / "lib/x86_64-linux-gnu"),
                                                                 str(files / "lib/i386-linux-gnu"),
                                                                 env.get("LD_LIBRARY_PATH", "")) if p)
            env["WINEDLLPATH"] = os.pathsep.join(p for p in (str(files / "lib/vkd3d"), str(files / "lib/wine"),
                                                             env.get("WINEDLLPATH", "")) if p)
            env["XLOCALEDIR"] = str(files / "share/X11/locale")
            env["ESPEAK_DATA_PATH"] = str(files / "share")
            env["MEDIACONV_BLANK_VIDEO_FILE"] = str(files / "share/media/blank.mkv")
            env["MEDIACONV_BLANK_AUDIO_FILE"] = str(files / "share/media/blank.ptna")
        env.update({
            "WINEPREFIX": str(self.prefix),
            "WINEDEBUG": "-all",
            "WINEESYNC": "1",
            "WINEFSYNC": "1",
            "WINENTSYNC": "1",  # Wine falls back when /dev/ntsync is missing
            "WINE_LARGE_ADDRESS_AWARE": "1",
            "DXVK_LOG_LEVEL": "none",
            "VKD3D_DEBUG": "none",
            "VKD3D_SHADER_DEBUG": "none",
            "WINEDLLOVERRIDES": self.dll_overrides(),
        })
        env.update({k: str(v) for k, v in (self.cfg.get("extra_env") or {}).items()})
        env.update({k: str(v) for k, v in ((self.recipe.get("prefix") or {}).get("env") or {}).items()})
        env.update({k: str(v) for k, v in (extra or {}).items()})
        return env

    def exe_path(self):
        """The launcher executable inside the prefix (recipe exe, else the exe_glob patterns), or None."""
        exe = self.prefix / self.recipe["exe"]
        if exe.exists():
            return exe
        for pattern in self.recipe.get("exe_glob") or []:
            matches = [p for p in self.prefix.glob(pattern) if p.is_file()]
            if matches:
                return max(matches, key=lambda p: p.stat().st_mtime)
        return None

    def is_installed(self):
        return self.exe_path() is not None

    def installer_path(self):
        return CACHE_DIR / "installers" / self.id / self.recipe["installer"]["filename"]

    def wine_user(self):
        """The user directory name under drive_c/users (the Unix user name for a prefix made
        with wine directly, "steamuser" for one the proton script made)."""
        try:
            names = sorted(d.name for d in (self.prefix / "drive_c/users").iterdir() if d.is_dir() and d.name != "Public")
        except OSError:
            names = []
        try:
            me = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            me = os.environ.get("USER", "user")
        if me in names or not names:
            return me
        return names[0]

    # ---------- install ----------

    def download_installer(self, progress=None, cancel_event=None):
        """The publisher's installer into CACHE_DIR/installers/<id>/, sha256-checked when the
        recipe pins one (the computed hash is always logged)."""
        progress = progress or (lambda stage, pct, text: None)
        inst = self.recipe["installer"]
        dest = self.installer_path()
        if dest.exists() and inst.get("sha256") and hash_file(dest)["sha256"] == inst["sha256"]:
            self.log(f"using the cached installer {dest}")
            return dest
        progress("download", None, f"Downloading the {self.name} installer")
        self.log(f"Downloading {inst['url']} ...")
        download_file(inst["url"], dest, progress, "download", cancel_event, self.log)
        with open(dest, "rb") as f:
            magic = f.read(2)
        if magic != b"MZ":
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"{inst['filename']} is not a Windows executable (the download may have been "
                               f"an error page from {inst['url']}); try again later")
        sha = hash_file(dest)["sha256"]
        self.log(f"{inst['filename']}: {human_size(dest.stat().st_size)}, sha256 {sha}")
        if inst.get("sha256") and sha != inst["sha256"]:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA-256 mismatch for {inst['filename']}: the recipe expects {inst['sha256']}, the download is {sha} "
                f"(the file was deleted). Either the publisher shipped a new build this recipe does not know yet, or "
                f"the download was tampered with. Check the publisher's current release (for Battle.net the "
                f"microsoft/winget-pkgs manifest under manifests/b/Blizzard/BattleNet lists url and InstallerSha256), "
                f"then update installer.url and installer.sha256 in data/compat/launchers.json - or wait for the "
                f"daily database refresh to bring the updated recipe.")
        return dest

    def ensure_prefix_files(self):
        """Write the recipe's prefix.files (settings the client must find before its first start)
        when they are missing; {user} is the Wine user name. Returns the paths written."""
        written = []
        user = self.wine_user()
        for f in (self.recipe.get("prefix") or {}).get("files") or []:
            rel = str(f.get("path", "")).replace("{user}", user)
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue
            path = self.prefix / rel
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(f.get("content", "")))
            written.append(path)
            self.log(f"wrote {path}")
        return written

    def _copy_graphics_dlls(self, ge):
        """DXVK and VKD3D-Proton from the GE-Proton tree into system32 / syswow64 (what the
        proton script's setup_prefix does)."""
        copied = 0
        for comp in ("dxvk", "vkd3d-proton"):
            for arch, sysdir in (("x86_64-windows", "system32"), ("i386-windows", "syswow64")):
                target = self.prefix / "drive_c/windows" / sysdir
                if not target.is_dir():
                    continue
                for dll in sorted((Path(ge) / "files/lib/wine" / comp / arch).glob("*.dll")):
                    dst = target / dll.name
                    dst.unlink(missing_ok=True)
                    shutil.copyfile(dll, dst)
                    copied += 1
        return copied

    def create_prefix(self, wine, progress=None, cancel_event=None):
        """wineboot a fresh win64 prefix with GE-Proton's wine, then copy DXVK / VKD3D-Proton in."""
        progress = progress or (lambda stage, pct, text: None)
        ge = ge_proton_dir(wine)
        tag = _ge_tag_of_dir(ge) or ge.name
        self.prefix.mkdir(parents=True, exist_ok=True)
        progress("prefix", None, f"Creating the Wine prefix for {self.name} (about a minute)")
        self.log(f"Creating Wine prefix {self.prefix} with {tag} ...")
        env = self.env()
        # GE-Proton ships Mono and Gecko as shared installs next to wine; only without them
        # would wineboot stop at the "download Wine Mono?" dialog
        if not ((ge / "files/share/wine/mono").is_dir() and (ge / "files/share/wine/gecko").is_dir()):
            env["WINEDLLOVERRIDES"] += ";mscoree=d;mshtml=d"
        logfile = self._logfile("setup")
        try:
            rc = _run_logged([wine, "wineboot", "-u"], env, logfile, 900, cancel_event=cancel_event)
            _run_logged([str(Path(wine).parent / "wineserver"), "-w"], env, logfile, 900, cancel_event=cancel_event)
        except Cancelled:
            self.stop()  # wineboot's children die with the prefix
            raise
        if not (self.prefix / "system.reg").exists():
            raise RuntimeError(f"wineboot did not create {self.prefix} (exit code {rc}); last lines:\n{_log_tail(logfile)}")
        copied = self._copy_graphics_dlls(ge)
        self.log(f"Installed {copied} DXVK / VKD3D-Proton DLLs into the prefix")
        (self.prefix / PREFIX_MARKER).write_text(json.dumps(
            {"launcher": self.id, "ge_proton": tag, "created": time.strftime("%Y-%m-%d")}, indent=2))

    def _winetricks(self, wine):
        wt = shutil.which("winetricks")
        if wt:
            return wt
        bundled = ge_proton_dir(wine) / "protonfixes/winetricks"
        return str(bundled) if bundled.exists() and os.access(bundled, os.X_OK) else ""

    def apply_prefix_tweaks(self, wine, progress=None, cancel_event=None):
        """The recipe's registry entries and winetricks verbs, each applied once per prefix
        (tracked in <prefix>/.xodus-compat-applied.json like the per-title tweaks). Cancelled
        (with the prefix stopped) when cancel_event fires; what was applied is remembered."""
        progress = progress or (lambda stage, pct, text: None)
        try:
            return self._apply_prefix_tweaks(wine, progress, cancel_event)
        except Cancelled:
            self.stop()
            raise

    def _apply_prefix_tweaks(self, wine, progress, cancel_event):
        pf = self.recipe.get("prefix") or {}
        marker = self.prefix / TWEAKS_MARKER
        done = _read_json(marker) or {}
        done.setdefault("registry", [])
        done.setdefault("winetricks", [])
        env = self.env()
        logfile = self._logfile("setup")
        changed = False
        for reg in pf.get("registry") or []:
            if not isinstance(reg, dict) or not reg.get("key"):
                self.log(f"ignoring registry entry without a key: {reg}")
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
            self.log("$ " + " ".join(cmd))
            try:
                rc = _run_logged(cmd, env, logfile, 120, cancel_event=cancel_event)
            except Cancelled:
                if changed:
                    marker.write_text(json.dumps(done, indent=2))
                raise
            except RuntimeError as e:
                self.log(f"registry entry failed: {e}")
                continue
            if rc == 0:
                done["registry"].append(key)
                changed = True
            else:
                self.log(f"registry entry failed (exit code {rc}): {_log_tail(logfile, 3)}")
        verbs = [v for v in pf.get("winetricks") or [] if v not in done["winetricks"]]
        if verbs:
            wt = self._winetricks(wine)
            if not wt:
                self.log(f"WARNING: {self.name} wants winetricks verbs ({' '.join(verbs)}) but winetricks is "
                         f"not installed and this GE-Proton bundles none; the client may misbehave")
            else:
                progress("tweaks", None, f"winetricks {' '.join(verbs)} (downloads the verbs' upstream files)")
                e = dict(env)
                e.update({"WINE": wine, "WINESERVER": str(Path(wine).parent / "wineserver"),
                          "WINETRICKS_LATEST_VERSION_CHECK": "disabled"})
                self.log(f"$ {wt} -q " + " ".join(verbs))
                try:
                    rc = _run_logged([wt, "-q"] + verbs, e, logfile, 3600, cancel_event=cancel_event)
                except Cancelled:
                    if changed:
                        marker.write_text(json.dumps(done, indent=2))
                    raise
                except RuntimeError as ex:
                    self.log(f"winetricks failed: {ex}")
                    rc = 1
                if rc == 0:
                    done["winetricks"] += verbs
                    changed = True
                else:
                    self.log(f"winetricks exited with code {rc}; the verbs are retried next time (log: {logfile})")
        if changed:
            marker.write_text(json.dumps(done, indent=2))
        return changed

    _kill = staticmethod(_kill_group)

    def run_installer(self, wine, installer, progress=None, cancel_event=None):
        """Run the installer with its silent arguments and wait for it: for its exit (the
        exit code must be in installer.success_codes), or, with installer.wait = "exe", for
        the launcher executable to appear (installers that never exit, like Rockstar's)."""
        progress = progress or (lambda stage, pct, text: None)
        inst = self.recipe["installer"]
        args = [str(a) for a in inst.get("silent_args") or []]
        note = str(inst.get("interactive_note") or "")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = self._logfile("install")
        cmd, run_env = self.runner_cmd(wine, installer, args)
        progress("install", None, f"Running the {self.name} installer" + (f": {note}" if note else ""))
        self.log("$ " + " ".join(cmd))
        if note:
            self.log(note)
        with open(logfile, "a") as lf:
            lf.write("$ " + " ".join(cmd) + "\n")
            lf.flush()
            try:
                p = subprocess.Popen(cmd, env=run_env, cwd=str(Path(installer).parent), stdout=lf,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                raise RuntimeError(f"could not start the {self.name} installer: {e}") from None
        wait = inst.get("wait", "exit")
        timeout = int(inst.get("timeout_seconds", 3600))
        settle = int(inst.get("settle_seconds", 10))
        deadline = time.monotonic() + timeout
        appeared = False
        last_report, started = 0.0, time.monotonic()
        while True:
            rc = p.poll()
            if time.monotonic() - last_report >= 10:
                last_report = time.monotonic()
                written, newest = prefix_activity(self.prefix)
                mins = int((time.monotonic() - started) // 60)
                if written:
                    progress("install", None, f"{self.name} installer running for {mins} min: "
                                              f"{human_size(written)} written in the last 90 s")
                elif newest and time.time() - newest > 300:
                    progress("install", None, f"{self.name} installer running for {mins} min: no file activity "
                                              f"for {int((time.time() - newest) // 60)} min (it may be waiting for a window)")
                else:
                    progress("install", None, f"{self.name} installer running for {mins} min")
            if cancel_event is not None and cancel_event.is_set():
                self._kill(p)
                self.stop()
                raise Cancelled("installation cancelled")
            if wait == "exe" and self.is_installed():
                self.log(f"{self.recipe['exe']} appeared; giving the installer {settle} s to settle")
                time.sleep(settle)
                appeared = True
                self.stop()  # this installer never exits on its own
                self._kill(p)
                break
            if rc is not None:
                break
            if time.monotonic() > deadline:
                self._kill(p)
                self.stop()
                raise RuntimeError(f"the {self.name} installer did not finish within {timeout // 60} minutes "
                                   f"(log: {logfile})")
            time.sleep(2)
        if not appeared and rc not in (inst.get("success_codes") or [0]):
            raise RuntimeError(f"the {self.name} installer exited with code {rc} (log: {logfile}); last lines:\n"
                               + _log_tail(logfile))

    def install(self, progress=None, cancel_event=None, force=False):
        """GE-Proton, the installer download, the prefix (wineboot, DXVK/VKD3D, files, tweaks),
        the installer run, then stop everything in the prefix and verify the launcher is there."""
        progress = progress or (lambda stage, pct, text: None)
        rt = self.recipe.get("runtime") or {}
        progress("runtime", None, "Checking GE-Proton")
        tool = find_proton_tool(rt.get("prefer_tools"))
        if not tool and rt.get("prefer_tools") and not rt.get("release"):
            raise RuntimeError(self.runtime_hint())
        if not tool and rt.get("release"):
            progress("runtime", None, f"Fetching {rt['release']['tag']}")
            tool = ensure_proton_release(self.cfg, rt["release"]["repo"], rt["release"]["tag"], self.log,
                                         progress, cancel_event)
        if tool:
            self.log(f"Using the Proton build {tool.name} at {tool} (preferred by the {self.name} recipe)")
            wine = str(tool / "files/bin/wine")
        else:
            wine = ensure_ge_proton(self.cfg, self.log, progress, rt.get("min_version"), cancel_event)
        _check_cancel(cancel_event)
        if self.is_installed() and not force:
            self.log(f"{self.name} is already installed in {self.prefix}")
            self.ensure_prefix_files()
            self.apply_prefix_tweaks(wine, progress, cancel_event)
            progress("done", 100, f"{self.name} is installed")
            return
        installer = self.download_installer(progress, cancel_event)
        _check_cancel(cancel_event)
        if not (self.prefix / "system.reg").exists():
            self.create_prefix(wine, progress, cancel_event)
        else:
            self.log(f"reusing the existing prefix {self.prefix}")
        self.ensure_prefix_files()
        progress("tweaks", None, f"Preparing the prefix for {self.name}")
        self.apply_prefix_tweaks(wine, progress, cancel_event)
        _check_cancel(cancel_event)
        self.run_installer(wine, installer, progress, cancel_event)
        progress("install", None, "Stopping what the installer left running")
        self.stop()
        if not self.is_installed():
            raise RuntimeError(f"{self.name} did not install: {self.recipe['exe']} is missing from {self.prefix} "
                               f"after the installer finished (log: {self._logfile('install')}); last lines:\n"
                               + _log_tail(self._logfile("install")))
        self.ensure_prefix_files()  # some installers wipe the pre-seeded settings
        marker = self.prefix / PREFIX_MARKER
        info = _read_json(marker) or {"launcher": self.id}
        info["installed"] = time.strftime("%Y-%m-%d")
        marker.write_text(json.dumps(info, indent=2))
        self.log(f"{self.name} installed in {self.prefix}")
        progress("done", 100, f"{self.name} installed; open it and sign in")

    # ---------- sign-in, run, list ----------

    def signin_steps(self):
        """The user-facing steps (shown numbered in the sign-in guide)."""
        return [str(s) for s in (self.recipe.get("signin") or {}).get("steps") or []]

    def signin_summary(self):
        """One or two sentences on how the Game Pass entitlement reaches this launcher, for the
        guide; the first sentence of xbox_link when the recipe has no summary."""
        si = self.recipe.get("signin") or {}
        if si.get("summary"):
            return str(si["summary"])
        return re.split(r"(?<=[.!?])\s+", self.xbox_link().strip(), maxsplit=1)[0]

    def game_pass_library(self):
        """True when the launcher lists the Game Pass titles in its own library once the
        accounts are linked (so "install it there, then Link installed game…" applies)."""
        return bool((self.recipe.get("signin") or {}).get("game_pass_library"))

    def signin_notes(self):
        """The research behind the steps (not shown in the guide)."""
        return [str(s) for s in (self.recipe.get("signin") or {}).get("notes") or []]

    def xbox_link(self):
        return str((self.recipe.get("signin") or {}).get("xbox_link") or "")

    def can_install_games(self):
        """True when launch.install_args let Play ask the launcher to install a game it does
        not have yet."""
        return bool((self.recipe.get("launch") or {}).get("install_args"))

    def known_issues(self):
        return list(self.recipe.get("known_issues") or [])

    def describe(self):
        """A summary for the UI."""
        exe = self.exe_path()
        return {"id": self.id, "name": self.name, "publisher": self.recipe.get("publisher", ""),
                "installed": exe is not None, "prefix": str(self.prefix), "exe": str(exe or ""),
                "ge_proton": ge_proton_installed(), "min_ge_proton": (self.recipe.get("runtime") or {}).get("min_version"),
                "confidence": self.recipe.get("confidence", ""), "known_issues": self.known_issues(),
                "user_override": bool(self.recipe.get("_user")), "host_notes": [n for n in (self.audio_note(),) if n]}

    def open(self, on_start=None, args=None):
        """Start the launcher UI in its prefix (detached session). Returns the Popen."""
        exe = self.exe_path()
        if exe is None:
            raise RuntimeError(f"{self.name} is not installed (expected {self.prefix / self.recipe['exe']}); install it first")
        wine = self.wine()
        self.ensure_prefix_files()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logfile = self._logfile("run")
        if args is None:
            args = (self.recipe.get("launch") or {}).get("open_args") or []
        cmd, run_env = self.runner_cmd(wine, exe, args)
        self.log("$ " + " ".join(cmd))
        with open(logfile, "a") as lf:
            lf.write("$ " + " ".join(cmd) + "\n")
            lf.flush()
            try:
                p = subprocess.Popen(cmd, env=run_env, cwd=str(exe.parent), stdout=lf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                raise RuntimeError(f"could not start {self.name}: {e}") from None
        if on_start:
            on_start(p)
        return p

    def launch_game(self, game_id=None, on_start=None):
        """Start a game through the launcher: launch.game_args / game_url with {game_id} (and
        {install_dir}, the game's Windows folder, for Rockstar). A game the launcher has not
        installed yet is started with launch.install_args when the recipe has them (the
        launcher then downloads it), else the launcher is just opened."""
        launch = self.recipe.get("launch") or {}
        if not game_id:
            return self.open(on_start)
        game = next((g for g in self.installed_games() if g["id"] == str(game_id)), None) or {}
        install_dir = game.get("win_dir") or ""
        if not install_dir and ("\\" in str(game_id) or "/" in str(game_id)):
            install_dir = str(game_id)  # a game folder given directly
        templates = [str(a) for a in launch.get("game_args") or []]
        if not templates and launch.get("game_url"):
            templates = [str(launch["game_url"])]
        if not game and not install_dir and launch.get("install_args"):
            self.log(f"{self.name}: {game_id!r} is not installed there yet; asking {self.name} to install it")
            templates = [str(a) for a in launch["install_args"]]
        if not templates or (not install_dir and any("{install_dir}" in a for a in templates)):
            self.log(f"{self.name}: no launch arguments for {game_id!r}; opening the launcher instead")
            return self.open(on_start)
        fmt = _Fmt(game_id=str(game_id), install_dir=install_dir, name=game.get("name", ""))
        return self.open(on_start, args=[a.format_map(fmt) for a in templates])

    def installed_games(self):
        """[{id, name, dir (host path), win_dir, launcher, ...}] per the recipe's scanner."""
        if not (self.prefix / "drive_c").is_dir():
            return []
        scanner = (self.recipe.get("installed_games") or {}).get("scanner") or "generic"
        try:
            return _SCANNERS.get(scanner, _scan_generic)(self)
        except (OSError, ValueError) as e:
            self.log(f"{self.name}: could not list installed games: {e}")
            return []

    # ---------- lifecycle ----------

    def is_running(self):
        """True when a wineserver for this prefix is alive."""
        ge = self.ge_dir()
        if not ge or not (self.prefix / "system.reg").exists():
            return False
        try:
            subprocess.run([str(ge / "files/bin/wineserver"), "-w"], env=self.env(), timeout=1,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return False
        except subprocess.TimeoutExpired:
            return True
        except OSError:
            return False

    def wait(self, timeout=None):
        """Block until nothing runs in this prefix any more (wineserver -w). The process open()
        returns is only the first one: UbisoftConnect.exe hands off to upc.exe within seconds,
        Battle.net and EA spawn their real client the same way, so a caller that wants "until
        the launcher (and the game it started) is closed" waits here. stop() ends the wait."""
        ge = self.ge_dir()
        if not ge or not (self.prefix / "system.reg").exists():
            return
        try:
            subprocess.run([str(ge / "files/bin/wineserver"), "-w"], env=self.env(), timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    def stop(self):
        """wineserver -k for this prefix (launchers keep background processes alive)."""
        ge = self.ge_dir()
        if not ge:
            return
        ws = str(ge / "files/bin/wineserver")
        env = self.env()
        try:
            subprocess.run([ws, "-k"], env=env, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([ws, "-w"], env=env, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    def remove_prefix(self):
        """Stop the prefix and delete it (the launcher, its games and its sign-in state)."""
        prefix = self.prefix
        root = DATA_DIR.resolve()
        real = prefix.resolve()
        if real == root or not real.is_relative_to(root) or prefix.is_symlink():
            raise RuntimeError(f"refusing to delete {prefix}: not a launcher prefix under {DATA_DIR}")
        self.stop()
        if prefix.exists():
            shutil.rmtree(prefix)
            self.log(f"removed {prefix}")


# --------------------------------------------------------------------------
# anti-cheat: Steam's Proton runtimes and what a game folder needs
# --------------------------------------------------------------------------

def _vdf_paths(text):
    """The "path" values of a text VDF (libraryfolders.vdf)."""
    return [m.group(1).replace("\\\\", "\\") for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text)]


def steam_roots():
    out, seen = [], set()
    for p in (HOME / ".local/share/Steam", HOME / ".steam/steam", HOME / ".steam/root",
              HOME / ".steam/debian-installation", HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
              HOME / "snap/steam/common/.local/share/Steam"):
        try:
            real = p.resolve(strict=True)
        except OSError:
            continue
        if real in seen or not real.is_dir():
            continue
        seen.add(real)
        out.append(p)
    return out


def steam_libraries():
    """Every Steam library (directory with steamapps/): the install roots and the entries of
    their libraryfolders.vdf."""
    libs, seen = [], set()
    for root in steam_roots():
        cands = [root]
        for vdf in (root / "steamapps/libraryfolders.vdf", root / "config/libraryfolders.vdf"):
            try:
                cands += [Path(p) for p in _vdf_paths(vdf.read_text(errors="replace"))]
            except OSError:
                pass
        for lib in cands:
            try:
                real = lib.resolve(strict=True)
            except OSError:
                continue
            if real in seen or not (lib / "steamapps").is_dir():
                continue
            seen.add(real)
            libs.append(lib)
    return libs


def anticheat_runtimes():
    """{"eac": Path or None, "battleye": Path or None}: Valve's runtimes as installed by Steam
    (apps 1826330 / 1161040, any library), a PROTON_*_RUNTIME already in the environment, or
    the copies Heroic / Lutris keep. The path is the directory holding v2/ (EAC) or v1/ (BattlEye)."""
    out = {}
    for kind, name in ANTICHEAT_RUNTIME_DIRS.items():
        marker = ANTICHEAT_RUNTIME_MARKER[kind]
        cands = []
        envdir = os.environ.get(ANTICHEAT_ENV[kind])
        if envdir:
            cands.append(Path(envdir))
        cands += [lib / "steamapps/common" / name for lib in steam_libraries()]
        other = "eac_runtime" if kind == "eac" else "battleye_runtime"
        cands += [HOME / ".config/heroic/tools/runtimes" / other, HOME / ".local/share/lutris/runtime" / other]
        out[kind] = next((c for c in cands if (c / marker).is_dir()), None)
    return out


def scan_anticheat_files(game_dir, max_depth=5):
    """What the game folder ships: EAC files, the EAC Linux module (easyanticheat_x64.so),
    BattlEye files, the EOS bootstrapper (start_protected_game.exe) and EasyAntiCheat/Settings.json."""
    root = Path(game_dir)
    found = {"eac": False, "eac_linux": False, "battleye": False, "bootstrapper": None, "eac_settings": None}
    if not root.is_dir():
        return found
    base = str(root)
    for dirpath, dirnames, filenames in os.walk(base):
        depth = dirpath[len(base):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        dname = Path(dirpath).name.lower()
        lower = {f.lower(): f for f in filenames}
        if dname == "easyanticheat":
            found["eac"] = True
            if "settings.json" in lower and found["eac_settings"] is None:
                found["eac_settings"] = Path(dirpath) / lower["settings.json"]
        if dname == "battleye":
            found["battleye"] = True
        for lf, f in lower.items():
            if lf in EAC_FILES:
                found["eac"] = True
            elif lf in EAC_LINUX_FILES:
                found["eac"] = found["eac_linux"] = True
            elif lf in BATTLEYE_FILES:
                found["battleye"] = True
            elif lf == "start_protected_game.exe" and found["bootstrapper"] is None:
                found["bootstrapper"] = str((Path(dirpath) / f).relative_to(root)).replace("/", "\\")
    return found


_HOOK_STRINGS = (b"PROTON_EAC_RUNTIME", b"PROTON_BATTLEYE_RUNTIME")
_hooks_cache = {}


def wine_has_anticheat_hooks(wine):
    """True when the Wine build at `wine` (a path to its bin/wine) is Proton-derived and
    carries the PROTON_EAC_RUNTIME / PROTON_BATTLEYE_RUNTIME loader hooks (its unix ntdll.so
    names them; GE-Proton and Proton do, WineGDK / plain Wine do not). The runtimes are useless
    - and the EAC bootstrapper hopeless - under a build without them."""
    if not wine:
        return False
    try:
        bin_dir = Path(wine).resolve().parent
    except OSError:
        return False
    cands = [bin_dir.parent / "lib/wine/x86_64-unix/ntdll.so", bin_dir.parent / "lib64/wine/x86_64-unix/ntdll.so",
             bin_dir.parent / "lib/wine/i386-unix/ntdll.so"]
    for ntdll in cands:
        try:
            st = ntdll.stat()
        except OSError:
            continue
        key = (str(ntdll), st.st_mtime_ns, st.st_size)
        if key not in _hooks_cache:
            try:
                data = ntdll.read_bytes()
            except OSError:
                continue
            _hooks_cache.clear()
            _hooks_cache[key] = all(s in data for s in _HOOK_STRINGS)
        return _hooks_cache[key]
    # no unix ntdll found: fall back to the Proton tree layout (<tool>/proton, lsteamclient.so)
    return (bin_dir.parent.parent / "proton").is_file() or \
        (bin_dir.parent / "lib/wine/x86_64-unix/lsteamclient.so").is_file()


def _bypass_from_settings(game_dir, settings):
    """The real game exe named by EasyAntiCheat/Settings.json (EOS layout), relative with
    backslashes, when it exists."""
    if not settings:
        return None
    try:
        exe = str(json.loads(Path(settings).read_text()).get("executable") or "")
    except (OSError, ValueError):
        return None
    exe = exe.replace("/", "\\").lstrip("\\")
    return exe if exe and (Path(game_dir) / exe.replace("\\", "/")).exists() else None


def anticheat_for_game(game_dir, compat_entry=None, wine=None):
    """What the anti-cheat situation of an installed game is.

    compat_entry is the title's compatibility entry (its "anticheat" object wins over what
    the files say) or that object itself; wine, when given, is the Wine build that will run
    the game (the Steam runtimes only work under a Proton-derived build, see
    wine_has_anticheat_hooks()). Returns {"kind": eac|battleye|ricochet|vanguard|none|unknown,
    "linux_module": bool, "runtime": Path or None, "runtime_usable": bool, "bypass": relative
    exe or None, "online": supported|unsupported|unknown, "message": str, "files": the scan}.

    "online" is "supported" only when the entry says so (a verified title) and the runtime is
    usable; a module found by the file scan alone gives "unknown", so the caller keeps the
    single-player bypass (the real executable) as the default and only exports the runtime."""
    ac = {}
    if isinstance(compat_entry, dict):
        if isinstance(compat_entry.get("anticheat"), dict):
            ac = compat_entry["anticheat"]
        elif "kind" in compat_entry:
            ac = compat_entry
    scan = scan_anticheat_files(game_dir)
    kind = ac.get("kind") if ac.get("kind") in ANTICHEAT_KINDS else None
    if kind is None:
        kind = "eac" if scan["eac"] else "battleye" if scan["battleye"] else "none"
    db_online = ac.get("online") if ac.get("online") in ONLINE_STATES else None
    db_module = ac.get("linux_module") if isinstance(ac.get("linux_module"), bool) else None
    runtimes = anticheat_runtimes() if kind in ANTICHEAT_RUNTIME_DIRS else {}
    runtime = runtimes.get(kind)
    hooks = None if wine is None else wine_has_anticheat_hooks(wine)
    usable = runtime is not None and hooks is not False
    bypass = None
    missing_bypass = ""
    if ac.get("bypass"):
        cand = str(ac["bypass"]).replace("/", "\\").lstrip("\\")
        if (Path(game_dir) / cand.replace("\\", "/")).exists():
            bypass = cand
        else:
            missing_bypass = cand
    if bypass is None:
        bypass = _bypass_from_settings(game_dir, scan["eac_settings"])
    label = ANTICHEAT_LABELS.get(kind, kind)
    info = {"kind": kind, "linux_module": False, "runtime": runtime, "runtime_usable": usable, "bypass": bypass,
            "online": "unknown", "message": "", "files": scan}
    no_hooks = (f" The runtime only works under a Proton build; the Wine in use ({Path(wine).name if wine else '?'}"
                " build) has no EasyAntiCheat / BattlEye hooks, so online modes are unavailable here.")
    verified = "" if db_online == "supported" else " (not verified for this title yet)"
    if kind == "eac":
        module = db_module if db_module is not None else scan["eac_linux"]
        info["linux_module"] = module
        if module and usable:
            info["online"] = "supported" if db_online == "supported" else "unknown"
            info["message"] = (f"{label} with a Linux module: online modes may work with Steam's Proton "
                               f"EasyAntiCheat Runtime ({runtime}){verified}.")
        elif module and runtime:
            info["online"] = "unsupported"
            info["message"] = f"{label} with a Linux module and Steam's runtime installed." + no_hooks
        elif module:
            info["online"] = "unsupported"
            info["message"] = (f"{label} with a Linux module, but Steam's Proton EasyAntiCheat Runtime is not "
                               f"installed: install it in Steam ({STEAM_INSTALL_URLS['eac']}) to enable online modes.")
        else:
            info["online"] = "unsupported"
            info["message"] = f"{label} without a Linux module: online / anti-cheat-protected modes are unavailable."
        if info["online"] != "supported":
            info["message"] += (f" Single-player starts {bypass} instead of the EAC bootstrapper." if bypass
                                else " No single-player bypass is known.")
    elif kind == "battleye":
        info["linux_module"] = bool(db_module)
        if db_module and usable:
            info["online"] = "supported" if db_online == "supported" else "unknown"
            info["message"] = (f"{label} with Proton support enabled by the publisher: online modes may work with "
                               f"Steam's Proton BattlEye Runtime ({runtime}){verified}.")
        elif db_module and runtime:
            info["online"] = "unsupported"
            info["message"] = f"{label} with Proton support from the publisher and Steam's runtime installed." + no_hooks
        elif db_module:
            info["online"] = "unsupported"
            info["message"] = (f"{label} with Proton support, but Steam's Proton BattlEye Runtime is not installed: "
                               f"install it in Steam ({STEAM_INSTALL_URLS['battleye']}) to enable online modes.")
        elif db_module is False:
            info["online"] = "unsupported"
            info["message"] = f"{label} without Proton support from the publisher: online modes are unavailable."
        elif usable:
            info["online"] = "unknown"
            info["message"] = (f"{label}: whether the publisher enabled Proton support cannot be seen from the "
                               f"game files; the Proton BattlEye Runtime ({runtime}) is passed to Wine, so online "
                               "modes may work.")
        elif runtime:
            info["online"] = "unsupported"
            info["message"] = f"{label} (publisher opt-in unknown)." + no_hooks
        else:
            info["online"] = db_online if db_online == "unsupported" else "unknown"
            info["message"] = (f"{label}: whether the publisher enabled Proton support cannot be seen from the "
                               f"game files; install Steam's Proton BattlEye Runtime ({STEAM_INSTALL_URLS['battleye']}) "
                               "to try online modes.")
        if bypass:
            info["message"] += f" Single-player starts {bypass}."
    elif kind in ("ricochet", "vanguard"):
        info["online"] = "unsupported"
        info["message"] = f"{label}: a Windows kernel driver with no Linux support; the game cannot run under Wine."
    elif kind == "none":
        info["online"] = db_online or "supported"
        info["message"] = "No anti-cheat found in the game files."
    else:
        info["online"] = db_online or "unknown"
        info["message"] = "Anti-cheat status unknown."
    if db_online == "unsupported" and kind in ANTICHEAT_RUNTIME_DIRS:
        info["online"] = "unsupported"  # the entry knows better than the file scan
    if missing_bypass:
        info["message"] += f" (The configured bypass {missing_bypass} is missing from the game folder.)"
    return info


def anticheat_env(info):
    """PROTON_EAC_RUNTIME / PROTON_BATTLEYE_RUNTIME for Proton's Wine when the runtime can do
    something: a usable runtime (anticheat_for_game() given the Wine build), EAC needs the
    game's Linux module, BattlEye is tried unless known unsupported. {} otherwise."""
    kind, runtime = info.get("kind"), info.get("runtime")
    if not runtime or kind not in ANTICHEAT_ENV or info.get("online") == "unsupported":
        return {}
    if not info.get("runtime_usable", True):
        return {}
    if kind == "eac" and not info.get("linux_module"):
        return {}
    return {ANTICHEAT_ENV[kind]: str(runtime)}


def anticheat_dll_overrides(info):
    """What the proton script adds for BattlEye (the bridge builtin over the game's BEClient DLLs); EAC needs none."""
    if anticheat_env(info) and info.get("kind") == "battleye":
        return {"beclient": "b,n", "beclient_x64": "b,n"}
    return {}
