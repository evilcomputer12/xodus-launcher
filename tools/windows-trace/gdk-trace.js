// Frida script: trace Xbox GDK runtime calls made by a real game on Windows.
//
// Hooks xgameruntime.dll's QueryApiImpl (class/interface requested, result) and
// then every virtual method of each interface the game obtains, logging slot,
// a few integer arguments and the HRESULT. Run from the host with:
//   python gdk-trace.py <pid-or-name>
// Output is a JSON-lines call log (one object per call) for comparison with WineGDK.
'use strict';

const MAX_SLOTS = 64;          // virtual methods to hook per interface
const hooked = new Set();      // vtable addresses already hooked
const names = {};              // GUID -> name, filled from gdk-guids.json if present
const vtables = {};            // IID -> [method names in vtable order], from gdk-vtables.json

function guidToString(p) {
  const d1 = p.readU32(), d2 = p.add(4).readU16(), d3 = p.add(6).readU16();
  const b = p.add(8).readByteArray(8);
  const hex = Array.from(new Uint8Array(b)).map(x => x.toString(16).padStart(2, '0')).join('');
  return `${d1.toString(16).padStart(8, '0')}-${d2.toString(16).padStart(4, '0')}-${d3.toString(16).padStart(4, '0')}-${hex.slice(0, 4)}-${hex.slice(4)}`;
}
function name(guid) { return names[guid] || guid; }
function hr(v) { const u = v.toInt32() >>> 0; return (u >= 0x80000000 ? '0x' + u.toString(16) : u === 0 ? 'S_OK' : '0x' + u.toString(16)); }

function hookInterface(ifacePtr, cls, iid) {
  const vtbl = ifacePtr.readPointer();
  if (hooked.has(vtbl.toString())) return;
  hooked.add(vtbl.toString());
  const mod = Process.findModuleByAddress(vtbl);
  send({ ev: 'iface', cls: name(cls), iid: name(iid), vtbl: vtbl.toString(), module: mod ? mod.name : null });
  for (let slot = 3; slot < MAX_SLOTS; slot++) {            // skip IUnknown
    const fn = vtbl.add(slot * Process.pointerSize).readPointer();
    const fmod = Process.findModuleByAddress(fn);
    if (!fmod || fmod.name.toLowerCase() !== 'xgameruntime.dll') break;   // end of vtable
    try {
      Interceptor.attach(fn, {
        onEnter(args) {
          this.t0 = Date.now();
          const t = vtables[iid]; const mname = t && t.methods[slot] ? t.methods[slot] : null;
          this.info = { ev: 'call', cls: name(cls), slot, method: mname, fn: fn.sub(fmod.base).toString(),
                        a1: args[1].toString(), a2: args[2].toString(), a3: args[3].toString() };
        },
        onLeave(ret) {
          this.info.ret = hr(ret); this.info.ms = Date.now() - this.t0;
          send(this.info);
        },
      });
    } catch (e) { /* non-hookable thunk */ }
  }
}

function main() {
  const m = Process.findModuleByName('xgameruntime.dll');
  if (!m) { send({ ev: 'wait', msg: 'xgameruntime.dll not loaded yet' }); return false; }
  const q = m.findExportByName('QueryApiImpl');
  const init = m.findExportByName('InitializeApiImplEx2') || m.findExportByName('InitializeApiImpl');
  if (init) Interceptor.attach(init, {
    onEnter(args) { this.a = [args[0].toInt32(), args[1].toInt32(), args[2].toInt32()]; },
    onLeave(ret) { send({ ev: 'init', gdkVer: this.a[0], gsVer: this.a[1], mode: this.a[2], ret: hr(ret) }); },
  });
  Interceptor.attach(q, {
    onEnter(args) { this.cls = guidToString(args[0]); this.iid = guidToString(args[1]); this.out = args[2]; },
    onLeave(ret) {
      send({ ev: 'query', cls: name(this.cls), iid: name(this.iid), ret: hr(ret) });
      if (ret.toInt32() === 0 && !this.out.isNull()) {
        const iface = this.out.readPointer();
        if (!iface.isNull()) hookInterface(iface, this.cls, this.iid);
      }
    },
  });
  send({ ev: 'ready', base: m.base.toString(), size: m.size });
  return true;
}

rpc.exports = {
  setNames(map) { Object.assign(names, map); },
  setVtables(map) { Object.assign(vtables, map); },
  start() {
    if (main()) return true;
    // the DLL is loaded a bit after process start: watch LoadLibrary
    const hook = Interceptor.attach(Process.getModuleByName('kernelbase.dll').getExportByName('LoadLibraryExW'), {
      onLeave() { if (!Process.findModuleByName('xgameruntime.dll')) return; hook.detach(); main(); },
    });
    return false;
  },
};
