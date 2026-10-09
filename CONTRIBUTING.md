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

Every kit tool takes `--game-dir /abs/path/to/<game>`; a game repository's
`tools/*.py` wrappers pass it for you.

```sh
.venv/bin/python tools/setup.py --game-dir /abs/<game> \
  --install "/path/to/installed game" \
  --ghidra-home /path/to/ghidra --java-home /path/to/jdk
```

Setup checks the executable hash, links the installation at `developer_exe`,
fetches the metadata `[setup]` names and exports translation inputs into
`analysis/`. `--link-only` skips the Ghidra export. `GHIDRA_HOME` and
`JAVA_HOME` work instead of the flags.

## Build

```sh
.venv/bin/python tools/build.py --game-dir /abs/<game>               # app
.venv/bin/python tools/build.py --game-dir /abs/<game> --regenerate  # after translator or input changes
.venv/bin/python tools/build.py --stub                               # hosts without a game
```

Other targets: `smoke` (offscreen scripted host), `headless`, `plugins`, `ios`,
`android`. `--preset` and `--config Debug` select the CMake configuration, whose
tree lives in `<build root>/cmake/<preset>`. `RECOMP_PROFILE_DIR` selects a
separate player profile. `-DRECOMP_VIDEO=OFF` at configure time builds without
FFmpeg.

## Add a game

Create a repository with `game.toml` and `globals.toml` modelled on
`games/stub/`, add the kit as a submodule at `kit/`, and build with
`kit/tools/build.py --game-dir "$PWD"`.
