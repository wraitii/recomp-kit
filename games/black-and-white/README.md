# Black & White

Black & White (2001), using the 1.42 fan patch `runblack.exe` as its reference.
Bring an installed copy to `build/original` with `tools/setup.py --game black-and-white --install <directory>`.

The game profile includes packed function-boundary metadata and native replacements for terrain cell construction/rendering and LOD settings. The translated originals remain available for comparison. `render.d3d8_wgpu` selects the D3D8/wgpu renderer used by the game's Direct3D 7 path.
