#!/usr/bin/env python3
"""Keep data/compat in step with the PC Game Pass catalog.

    scripts/compat-seed.py            write data/compat/catalog-index.json ({StoreId: title} for
                                      every title) from the launcher's catalog cache, fetching the
                                      catalog the way the launcher does when the cache is stale
    scripts/compat-seed.py --offline  use the cache whatever its age (no network)
    scripts/compat-seed.py --check    validate data/compat/titles.json (schema, statuses, ids,
                                      launcher / anticheat fields) and data/compat/launchers.json

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
              "dll_overrides", "launch", "winetricks", "registry", "launcher", "launcher_game_id",
              "anticheat"}
TESTED_KEYS = {"date", "launcher", "winegdk", "gpu", "distro"}
LAUNCH_KEYS = {"executable", "args", "eac_bypass", "working_dir"}
ANTICHEAT_KEYS = {"kind", "linux_module", "bypass", "online"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def launcher():
    return _load("xodus_launcher", ROOT / "launcher/xodus_launcher.py")


def thirdparty():
    return _load("thirdparty", ROOT / "launcher/thirdparty.py")


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


def check_entry(sid, e, errors, launcher_ids, tp):
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
    if "launcher" in e:
        if not isinstance(e["launcher"], str) or e["launcher"] not in launcher_ids:
            errors.append(f"{where}.launcher: must be a launcher id from launchers.json "
                          f"({', '.join(launcher_ids) or 'none'})")
    if "launcher_game_id" in e:
        if e["launcher_game_id"] is not None and not isinstance(e["launcher_game_id"], str):
            errors.append(f"{where}.launcher_game_id: a string or null")
        if "launcher" not in e:
            errors.append(f"{where}.launcher_game_id: needs a launcher")
    ac = e.get("anticheat")
    if ac is not None:
        if not isinstance(ac, dict):
            errors.append(f"{where}.anticheat: must be an object")
        else:
            for k in set(ac) - ANTICHEAT_KEYS:
                errors.append(f"{where}.anticheat: unknown field {k!r}")
            if ac.get("kind") not in tp.ANTICHEAT_KINDS:
                errors.append(f"{where}.anticheat.kind: one of {', '.join(tp.ANTICHEAT_KINDS)}")
            if "linux_module" in ac and not isinstance(ac["linux_module"], bool):
                errors.append(f"{where}.anticheat.linux_module: true or false")
            if ac.get("bypass") is not None and not isinstance(ac["bypass"], str):
                errors.append(f"{where}.anticheat.bypass: a relative exe path or null")
            if "online" in ac and ac["online"] not in tp.ONLINE_STATES:
                errors.append(f"{where}.anticheat.online: one of {', '.join(tp.ONLINE_STATES)}")


def check(db, index, launchers):
    errors = []
    tp = thirdparty()
    if db.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    for k in ("generated", "updated"):
        if not DATE.match(str(db.get(k, ""))):
            errors.append(f"{k}: use YYYY-MM-DD")
    for k in set(db.get("defaults") or {}) - (ENTRY_KEYS - {"title", "notes", "tested", "launcher",
                                                            "launcher_game_id", "anticheat"}):
        errors.append(f"defaults: unknown field {k!r}")
    launcher_ids = sorted((launchers or {}).get("launchers") or {})
    for sid, e in (db.get("titles") or {}).items():
        check_entry(sid, e, errors, launcher_ids, tp)
        if index and sid not in index:
            print(f"warning: {sid} ({e.get('title')}) is not in catalog-index.json")
    newest = max((str((e.get("tested") or {}).get("date", "")) for e in db["titles"].values()), default="")
    if newest > str(db.get("updated", "")):
        errors.append(f"updated ({db.get('updated')}) is older than the newest tested.date ({newest})")
    errors += tp.validate_launchers_db(launchers)
    return errors


def coverage(db, index):
    by_status = {}
    for sid, e in db["titles"].items():
        by_status[e.get("status", "untested")] = by_status.get(e.get("status", "untested"), 0) + 1
    covered = sum(1 for sid in db["titles"] if sid in index) if index else len(db["titles"])
    total = len(index) if index else "?"
    print(f"{covered} of {total} catalog titles have an entry: " +
          ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())))
    via = sum(1 for e in db["titles"].values() if e.get("launcher"))
    if via:
        print(f"{via} of them run through a third-party launcher")


def main(argv):
    db = json.loads((COMPAT / "titles.json").read_text())
    index_file = COMPAT / "catalog-index.json"
    if "--check" in argv:
        index = json.loads(index_file.read_text()) if index_file.exists() else {}
        launchers = json.loads((COMPAT / "launchers.json").read_text())
        errors = check(db, index, launchers)
        for e in errors:
            print("error:", e)
        coverage(db, index)
        print(f"launchers.json: {len(launchers.get('launchers') or {})} recipes "
              + ("OK" if not errors else "checked"))
        return 1 if errors else 0
    index = write_index(catalog("--offline" in argv))
    print(f"\nwrote {index_file} ({len(index)} titles)", file=sys.stderr)
    coverage(db, index)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
