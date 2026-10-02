# Wine D3D8 API reference

Unmodified `include/d3d8.h` from Wine commit
`db11d0fe6a169c457e23d007e20404643d067aa8` (Wine 11.0), used as data by
`tools/gen_com_interfaces.py` to recover COM method names, order, argument
counts and interface IIDs. It is not compiled into the renderer. The source
is https://github.com/wine-mirror/wine/blob/db11d0fe6a169c457e23d007e20404643d067aa8/include/d3d8.h.
The original LGPL notice is retained; the licence is in `COPYING.LIB`.

This is the same pinned header as the Ghost Recon renderer's API inventory.
Generated C++ includes stay in the build tree. Support choices come from
`dx/d3d8_bindings.json`, not from the mere presence of an API declaration.
