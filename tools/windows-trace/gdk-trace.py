#!/usr/bin/env python3
"""Attach Frida to a running GDK game on Windows and log its xgameruntime calls.

usage: python gdk-trace.py <pid | process-name> [out.jsonl]
Run on the Windows machine (or with frida-server reachable). Each line of the
output file is one event: query (QueryApiImpl), iface (interface obtained),
call (virtual method call with HRESULT), init.
"""
import json, os, sys, time
import frida

target = sys.argv[1]
out_path = sys.argv[2] if len(sys.argv) > 2 else "gdk-trace.jsonl"
here = os.path.dirname(os.path.abspath(__file__))
session = frida.attach(int(target) if target.isdigit() else target)
script = session.create_script(open(os.path.join(here, "gdk-trace.js")).read())
out = open(out_path, "a")

def on_message(msg, data):
    if msg["type"] == "send":
        ev = msg["payload"]; ev["t"] = time.time()
        out.write(json.dumps(ev) + "\n"); out.flush()
        if ev["ev"] in ("query", "init", "iface", "ready", "wait"):
            print(ev)
    else:
        print(msg)

script.on("message", on_message)
script.load()
names_file = os.path.join(here, "gdk-guids.json")
if os.path.exists(names_file):
    script.exports_sync.set_names(json.load(open(names_file)))
vt_file = os.path.join(here, "gdk-vtables.json")
if os.path.exists(vt_file):
    script.exports_sync.set_vtables(json.load(open(vt_file)))
script.exports_sync.start()
print(f"tracing {target}; writing {out_path}; Ctrl+C to stop")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
session.detach()
