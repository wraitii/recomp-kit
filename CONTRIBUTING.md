# Contributing

## Prerequisites

- Python 3.9+ with a virtualenv holding `requirements-dev.txt` (CMake and Ninja
  come from it). On Windows use `.venv/Scripts/python.exe` below.
- macOS: Xcode Command Line Tools. Linux: clang, lld, make and the SDL headers
  listed in `.github/workflows/checks.yml`. Windows: LLVM clang in a Visual
  Studio developer prompt.
- [Ghidra 12.1.3](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.3_build)
  and a matching JDK, unless the game ships a code map (`[translate] code_map`).
- Your own installation of the game. `game.toml` pins the executable's SHA-256.

## Prepare a game

```sh
.venv/bin/python tools/setup.py --game <id> --install "/path/to/installed game"
```

Setup checks the executable hash and links the installation at
`<build root>/original`. Translation needs the game's Ghidra code map.

## Build

```sh
.venv/bin/python tools/build.py --game <id>               # app
.venv/bin/python tools/build.py --game <id> --regenerate  # after translator or input changes
.venv/bin/python tools/build.py --stub                    # hosts without a game
```

Other targets: `smoke` (offscreen scripted host), `headless`, `plugins`, `ios`,
`android`. `--preset` and `--config Debug` select the CMake configuration, whose
tree lives in `<build root>/cmake/<preset>`. `RECOMP_PROFILE_DIR` selects a
separate player profile. `-DRECOMP_VIDEO=OFF` at configure time builds without
FFmpeg.

## Add a game

Create `games/<id>/game.toml` modelled on `games/stub/`. To ship a code map,
export a `recomp-code-map-v3-export` directory from Ghidra (the game's own export
script; spans, jump tables, interior entries and non-returning calls) and pack it:

```sh
.venv/bin/python tools/recomp/code_map.py --pack EXPORT_DIR --exe GAME.EXE --out games/<id>/metadata
```

The packed map keeps addresses only: function spans, the instruction lengths a
linear decode would get wrong, jump tables, interior entries and non-returning
calls. A gap in it is fixed in Ghidra, then re-exported, repacked and
regenerated; translation never guesses entry points.
