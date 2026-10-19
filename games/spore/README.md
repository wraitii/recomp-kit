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
tools/.venv/bin/python tools/build.py --regenerate
```

Setup checks the pinned executable against `game.toml`, validates the data
directories and links the installation as `build/original`. It refuses to
continue until `[translate] code_map` names a packed map.

## Blockers

1. Establish how the game reads its install and data paths, and whether the
   registry values need host handling.

## steam_api.dll

The GOG build ships a 15 KB `steam_api.dll` that is not a Steam client. Its
`SteamAPI_Init` always succeeds, `SteamAPI_Shutdown` does nothing, and
`SteamApps` returns an object whose ownership check accepts the game's app id.
`runtime/steam_api.cpp` is a deliberately marked mock of those three answers;
it logs a warning when `SteamAPI_Init` is reached and leaves any other
`steam_api` import a strict named failure.

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
