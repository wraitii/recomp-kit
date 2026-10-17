# Black & White

Portable static recompilation of the PC **Black & White (2001)** and its patch
content, with selective native replacements. The behavioral reference is
`runblack.exe` from the 1.42 fan patch (32-bit x86, DirectDraw/Direct3D 7,
DirectInput, Winsock, Lionhead support libraries). The stock 1.2 executable is
supporting evidence only. This directory is the game's configuration root
(`--game black-and-white`).

The game profile includes packed function-boundary metadata and native
replacements for terrain cell construction/rendering and LOD settings. The
translated originals remain available for comparison. `render.d3d8_wgpu` selects
the D3D8/wgpu renderer used by the game's Direct3D 7 path.

## Setup and build

From the game repository root (the one containing `recomp-kit/`):

```sh
git submodule update --init --recursive recomp-kit
python3 -m venv tools/.venv
tools/.venv/bin/python -m pip install -r recomp-kit/requirements-dev.txt
RECOMP_BUILD_ROOT=$PWD/build tools/.venv/bin/python recomp-kit/tools/setup.py \
  --game black-and-white --install <directory>
tools/.venv/bin/python tools/build.py --regenerate --target app
```

Setup links the installed game at `build/original`; it changes no game files.
The desktop library accepts a supported 1.42 installed folder. Installer and disc
sources use assisted preparation; patch order and content deltas remain unverified,
so the library gives manual guidance and rechecks the resulting files.
Prerequisites are in [CONTRIBUTING](../../CONTRIBUTING.md#prerequisites). The
translator needs `tools/.venv`; the system Python lacks `pefile`.

Targets: `--target app` builds `build/BlackWhiteRecomp.app`; `--target headless`
builds the smoke binary `build/recomp/pop_headless`. Runtime and CMake edits need
only the `--target` build; add `--regenerate` only when a translation input
changed (listings, the game profile, code map or translator sources).

The executable and auxiliary modules decode from their packed maps.
A full regeneration translates all modules with no failures expected; per-module
counts land in `build/recomp/translate-*-report.json` (`functions_ok ==
functions_total`, `failures` and `unmodelled` empty).

Run paths, diagnostics, profiling flags and the game's status are in the game
repository's `docs/recompilation.md`.
