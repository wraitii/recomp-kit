# Wine D3D9/D3DX9 API reference

Unmodified headers from Wine commit
`db11d0fe6a169c457e23d007e20404643d067aa8` (Wine 11.0), used as data by
generators. They are not compiled into the renderer. Sources:
https://github.com/wine-mirror/wine/blob/db11d0fe6a169c457e23d007e20404643d067aa8/include/
The original LGPL notice is retained; the licence is in `COPYING.LIB`.
`SHA256SUMS` records the exact bytes.

- `d3d9.h`: `tools/gen_com_interfaces.py` recovers COM method names, order,
  argument counts and interface IIDs for the D3D9 factory, device, resources,
  shaders, state blocks, volumes and queries.
- `d3dx9effect.h`: the same generator, run with `--sdk-version 26`, produces the
  version-26 `ID3DXEffect`/`ID3DXEffectPool` tables that the `d3dx9_26.dll`
  consumers use. `D3DX_SDK_VERSION` GUID and method branches are resolved by the
  `--sdk-version` selection, so the version-33 definitions stay separate.
- `d3dx9core.h`, `d3dx9shader.h`, `d3dx9tex.h`, `d3dx9math.h`: declaration input
  for `tools/gen_d3dx9_imports.py`, which derives the exact stdcall arity of the
  `d3dx9_33.dll` imports. `d3dx9.h` is the aggregating include.
- `d3d9types.h`: `tools/gen_d3d8_constants.py` resolves the names listed in
  `dx/d3d9_constants.json` into generated `constexpr` values.

The `d3d9types.h` revision is the Wine 11.0 pin (53443 bytes). The game
checkout's `../reference/wine/include/d3d9types.h` at reference HEAD
`a15ee2cc...` is 53256 bytes and only drops the `D3DCLIPPLANE0..5` defines; the
pinned bytes are vendored so every generator reads the same source.

Generated C++ includes stay in the build tree. Support choices come from
`dx/d3d9_bindings.json`, `dx/d3dx9_bindings.json`, `dx/d3dx9_imports.json` and
`dx/d3d9.cpp`, not from the mere presence of an API declaration or constant.
