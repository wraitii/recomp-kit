# Wine D3D8 API reference

Unmodified `include/d3d8.h`, `d3d8types.h` and `d3d8caps.h` from Wine commit
`db11d0fe6a169c457e23d007e20404643d067aa8` (Wine 11.0), used as data by
generators. They are not compiled into the renderer. Sources:
https://github.com/wine-mirror/wine/blob/db11d0fe6a169c457e23d007e20404643d067aa8/include/
The original LGPL notice is retained; the licence is in `COPYING.LIB`.
`SHA256SUMS` records the exact bytes.

- `d3d8.h`: `tools/gen_com_interfaces.py` recovers COM method names, order,
  argument counts and interface IIDs.
- `d3d8types.h`, `d3d8caps.h`: `tools/gen_d3d8_constants.py` resolves the names
  listed in `dx/d3d8_constants.json` (enums, usage and capability flags) into
  generated `constexpr` values, so `dx/d3d8.cpp` uses the header's names instead
  of hand-copied numbers.

This is the same pinned header set as the Ghost Recon renderer's API inventory.
Generated C++ includes stay in the build tree. Support choices come from
`dx/d3d8_bindings.json` and `dx/d3d8.cpp`, not from the mere presence of an API
declaration or constant.
