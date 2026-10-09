# GDK call tracer (Windows)

Attach to a real Xbox Game Pass title running on Windows and log every call it makes into
Microsoft's `xgameruntime.dll`: which runtime classes it asks for (`QueryApiImpl`), every
virtual method call with its arguments and HRESULT, and timing. The output is JSON lines that
can be compared with WineGDK's `trace+gdkc` log to find where the open-source runtime answers
differently.

Requirements on the Windows machine: Python 3 and `pip install frida-tools` (Frida 17 API).

```
python gdk-trace.py <pid or exe name> trace.jsonl
```

`gdk-guids.json` maps class/interface GUIDs to names and `gdk-vtables.json` maps interface
GUIDs to method names in vtable order; both are generated from the GDK interface definitions
(IDL files) in the xodus wine fork.
