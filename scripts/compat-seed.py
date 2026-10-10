#!/usr/bin/env python3
"""Keep data/compat in step with the PC Game Pass catalog.

    scripts/compat-seed.py            write data/compat/catalog-index.json ({StoreId: title} for
                                      every title) from the launcher's catalog cache, fetching the
                                      catalog the way the launcher does when the cache is stale
    scripts/compat-seed.py --offline  use the cache whatever its age (no network)
    scripts/compat-seed.py --check    validate data/compat/titles.json (schema, statuses, ids)

Both modes print how much of the catalog titles.json covers, by status.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPAT = ROOT / "data/compat"
STORE_ID = re.compile(r"^9[A-Z0-9]{11}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENTRY_KEYS = {"title", "status", "notes", "tested", "runtime", "prefix", "dxvk", "vkd3d", "env",
              "dll_overrides", "launch", "winetricks", "registry"}
TESTED_KEYS = {"date", "launcher", "winegdk", "gpu", "distro"}
LAUNCH_KEYS = {"executable", "args", "eac_bypass", "working_dir"}


def launcher():
    spec = importlib.util.spec_from_file_location("xodus_launcher", ROOT / "launcher/xodus_launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def catalog(offline):
    mod = launcher()
    cfg = mod.load_config()
    if offline:
        cache = mod.CACHE_DIR / f"catalog-{cfg['market']}-{cfg['language']}.json"
        return json.loads(cache.read_text())
    return mod.load_catalog(cfg, progress=lambda d, t: print(f"\rfetching catalog {d}/{t}", end="",
                                                             file=sys.stderr))


def write_index(games):
    index = {g["id"]: g["title"] for g in games}
    (COMPAT / "catalog-index.json").write_text(
        json.dumps(dict(sorted(index.items())), indent=2, ensure_ascii=False) + "\n")
    return index


def check_entry(sid, e, errors):
    where = f"titles[{sid}]"
    if not STORE_ID.match(sid):
        errors.append(f"{where}: not a Store ID")
    for k in set(e) - ENTRY_KEYS:
        errors.append(f"{where}: unknown field {k!r}")
    if not e.get("title"):
        errors.append(f"{where}: missing title")
    statuses = launcher().COMPAT_STATUSES
    if e.get("status", "untested") not in statuses:
        errors.append(f"{where}: status must be one of {', '.join(statuses)}")
    t = e.get("tested") or {}
    for k in set(t) - TESTED_KEYS:
        errors.append(f"{where}.tested: unknown field {k!r}")
    if t.get("date") and not DATE.match(str(t["date"])):
        errors.append(f"{where}.tested.date: use YYYY-MM-DD")
    if e.get("status", "untested") != "untested" and not t.get("date"):
        errors.append(f"{where}: a tested status needs tested.date")
    if e.get("prefix", "shared") not in ("shared", "per-game"):
        errors.append(f"{where}.prefix: shared or per-game")
    for k in ("env", "dll_overrides"):
        if not isinstance(e.get(k, {}), dict) or \
                not all(isinstance(v, str) for v in e.get(k, {}).values()):
            errors.append(f"{where}.{k}: must be an object of strings")
    for k in set(e.get("launch") or {}) - LAUNCH_KEYS:
        errors.append(f"{where}.launch: unknown field {k!r}")
    if not isinstance(e.get("winetricks", []), list):
        errors.append(f"{where}.winetricks: must be a list of verbs")
    for r in e.get("registry") or []:
        if not isinstance(r, dict) or not r.get("key"):
            errors.append(f"{where}.registry: every entry needs a key")
    rt = e.get("runtime") or {}
    if not isinstance(rt.get("wine", ""), str) or not isinstance(rt.get("min_winegdk", ""), str):
        errors.append(f"{where}.runtime: wine and min_winegdk are strings")


def check(db, index):
    errors = []
    if db.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    for k in ("generated", "updated"):
        if not DATE.match(str(db.get(k, ""))):
            errors.append(f"{k}: use YYYY-MM-DD")
    for k in set(db.get("defaults") or {}) - (ENTRY_KEYS - {"title", "notes", "tested"}):
        errors.append(f"defaults: unknown field {k!r}")
    for sid, e in (db.get("titles") or {}).items():
        check_entry(sid, e, errors)
        if index and sid not in index:
            print(f"warning: {sid} ({e.get('title')}) is not in catalog-index.json")
    newest = max((str((e.get("tested") or {}).get("date", "")) for e in db["titles"].values()), default="")
    if newest > str(db.get("updated", "")):
        errors.append(f"updated ({db.get('updated')}) is older than the newest tested.date ({newest})")
    return errors


def coverage(db, index):
    by_status = {}
    for sid, e in db["titles"].items():
        by_status[e.get("status", "untested")] = by_status.get(e.get("status", "untested"), 0) + 1
    covered = sum(1 for sid in db["titles"] if sid in index) if index else len(db["titles"])
    total = len(index) if index else "?"
    print(f"{covered} of {total} catalog titles have an entry: " +
          ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())))


def main(argv):
    db = json.loads((COMPAT / "titles.json").read_text())
    index_file = COMPAT / "catalog-index.json"
    if "--check" in argv:
        index = json.loads(index_file.read_text()) if index_file.exists() else {}
        errors = check(db, index)
        for e in errors:
            print("error:", e)
        coverage(db, index)
        return 1 if errors else 0
    index = write_index(catalog("--offline" in argv))
    print(f"\nwrote {index_file} ({len(index)} titles)", file=sys.stderr)
    coverage(db, index)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
