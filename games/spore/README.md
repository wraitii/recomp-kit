# Spore (2008)

Target: the GOG SPORE Collection build. The game directory is
`recomp-kit/games/spore/` (`--game spore`).

## Status

The executable is pinned to `SporebinEP1/SporeApp.exe` (Galactic Adventures
3.1.0.22), the executable GOG launches for the collection. It loads the base
`Data/` content together with `DataEP1/`; `SporeBin/SporeApp.exe` is the base-game
fallback and is not the target. `metadata/` holds the packed code map for
`SporebinEP1/SporeApp.exe`.

## Installed layout

```
Spore.exe
SporebinEP1/SporeApp.exe
SporeBin/SporeApp.exe
Data/
DataEP1/
bp1content/
```

`game.toml` requires `Data`, `DataEP1` and `bp1content`.

## Setup and build

From the game repository root:

```sh
git submodule update --init --recursive recomp-kit
python3 -m venv tools/.venv
tools/.venv/bin/python -m pip install -r recomp-kit/requirements-dev.txt
RECOMP_BUILD_ROOT=$PWD/build tools/.venv/bin/python recomp-kit/tools/setup.py \
  --game spore --install "$PWD/SPORE"
tools/.venv/bin/python tools/build.py --regenerate \
  --allow-unmodelled "Spore bring-up: unsupported instructions trap at their own address"
```

The allowance covers instructions the recovered boundaries cannot model; each
one traps at its own address if reached.

Setup checks the pinned executable against `game.toml`, validates the data
directories and links the installation as `build/original`. It refuses to
continue until `[translate] code_map` names a packed map.

## Installation registry

`game.toml` supplies the installation values from the GOG Collection's
`goggame-1948823323.script`; saved registry values override these defaults.
The guest root maps to `SporebinEP1`, with the content directories alongside it.
Startup `0x007e7db0` reads `spore\DataDir` through `0x006ab870`; without it,
the package path resolves to the executable directory. The separate config-path
fallback finds `../Data/Config`, but cannot supply packaged default preferences
or option definitions, leading to the `setOption` error at `0x007c8eee`.

## steam_api.dll

The GOG build ships a 15 KB `steam_api.dll` that is not a Steam client. Its
`SteamAPI_Init` always succeeds, `SteamAPI_Shutdown` does nothing, and
`SteamApps` returns an object whose ownership check accepts the game's app id.
`runtime/steam_api.cpp` is a deliberately marked mock of those three answers;
it logs a warning when `SteamAPI_Init` is reached and leaves any other
`steam_api` import a strict named failure.

## goggame-1948823323.dll

The GOG build loads this helper from `SporebinEP1`; it exports no functions and
is mapped as an auxiliary image for its `DllMain`. `metadata-goggame/` is a
bootstrap code map. Auxiliary mappings may push the heap start up, so this DLL
owns `0x10000000`..`0x1003a000` and the effective heap base becomes
`0x1003a000`.

## D3DX shader compilation

The `D3DXCompileShader` IAT at `0x011e2014` is called at `0x006fb5df`,
`0x006fec68`, `0x00713d5e`, `0x00713df8`, `0x011f6cd4` and `0x011f7aeb`.
Each supplies entry point `main`, null macros/includes/constant-table output,
and its profile from the guest; flags are `0x12`/`0x32`, `0x2` and `0`.
The native compiler is vkd3d-shader 2.1. `DIVERGENCE(original):` it emits
full-precision bytecode when `D3DXSHADER_PARTIALPRECISION` is set, which the
Direct3D 9 rules permit; `SKIPVALIDATION` still passes through vkd3d's HLSL
front end, so malformed source may fail compilation.

## MSVCR90.dll

The executable imports 159 entries from the VS2008 C runtime. The PE entry is
`0x011e11e1` (`__security_init_cookie` `0x011e1588`, then
`WinMainCRTStartup` `0x011e0f21`). Startup calls `_initterm_e`
(`0x13eb09c`..`0x13eb0ac`), whose first callback `0x011e1100` performs
`__set_app_type`, `_encode_pointer`, `__p__fmode`, `__p__commode`,
`_adjust_fdiv`, `__setusermatherr` and `_configthreadlocale`; then `_initterm`
(`0x13cc80c`..`0x13eb098`) runs 31,266 C++ initializers; `_acmdln`
(`0x13cc324`) is read as a data import, and `_ismbblead` runs per command-line
byte before `WinMain` (`0xf489e0`).

`runtime/msvcr90.cpp` implements that startup set, the guest-heap `malloc`
family, strings and character classes. The table is registered strict, so any
other MSVCR90 name still stops the run with a named diagnostic instead of
returning 0. Known gaps: stdio (`__iob_func`, `printf` family), double-returning
math and conversion (`atof`, `strtod`, `floor`), C++ exception handling
(`__CxxFrameHandler3`, `_except_handler3`, `_except_handler4_common`,
`_CxxThrowException`), locale, and time/stat conversion.
