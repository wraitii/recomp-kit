# recomp-kit

A static recompilation kit: 32-bit x86 Windows games become native
applications, with no JIT and no emulator at run time. macOS is the main
target and Linux is supported; Windows builds work too, and iOS and Android
builds exist. You supply your own copy of each game.

## Games

Each game lives in `games/<id>/` with its own setup, build and status notes in
`games/<id>/README.md`. Games are kept in their own repositories, each with this
kit as a submodule.

| Game | Status | Version | Notes |
|---|---|---|---|
| Black & White | Playable | CD with the 1.42 fan patch | [games/black-and-white](games/black-and-white/README.md) |
| Tom Clancy's Ghost Recon | Playable | GOG 1.4 | [games/ghost-recon](games/ghost-recon/README.md) |
| Railroad Tycoon 3 | Bring-up | GOG | [games/railroad-tycoon-3](games/railroad-tycoon-3/README.md) |
| Civilization IV: Beyond the Sword | Playable | GOG | [games/civ4-bts](games/civ4-bts/README.md) |
| 4x4 Evolution 2 | Playable | PC | [games/4x4-evo2](games/4x4-evo2/README.md) |

## Desktop library

The desktop library imports installed folders and ZIPs with exact version checks,
and offers an assisted drop folder for manual preparation. Other installers and
disc images are reference-only; the library never runs them. ZIP imports reject
unsafe paths, links and special files and enforce entry and expanded-size limits.
Black & White needs an installed folder with the fan patch.

## Building from source

From a game's repository root (the one containing `recomp-kit/`), with the game's
`tools/build.py` wrapper:

```sh
python3 -m venv tools/.venv && tools/.venv/bin/pip install -r recomp-kit/requirements-dev.txt
RECOMP_BUILD_ROOT=$PWD/build tools/.venv/bin/python recomp-kit/tools/setup.py \
  --game <id> --install "/path/to/installed game"
tools/.venv/bin/python tools/build.py --regenerate
```

Setup checks the executable against `game.toml` and links your installation;
no game files are copied or changed. A game's `README.md` gives its exact commands
and notes. Without a game repository, `tools/build.py --game <id>` in this kit
builds into `build/<id>/`. `tools/build.py --target library` builds the desktop
library. Prerequisites, other targets and adding a game are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## How it works

Translation reads the game's code map (function spans from Ghidra, no
instruction bytes) and the executable, lifts each function with SLEIGH and
emits SSA-based C ([docs/ir.md](docs/ir.md)). The runtime provides guest memory,
threads and the Win32 and DirectX surface the game expects
([docs/architecture.md](docs/architecture.md)), and native hosts draw through
Metal or Vulkan. [docs/code-guide.md](docs/code-guide.md) maps behavior to
source.

| Directory | What it holds |
|---|---|
| `runtime/` | x86 semantics (`x86.h`), guest memory, PE loader, scheduler, kernel32/user32 shims |
| `dx/` | DirectX and media shims: DirectDraw through Direct3D 11, DirectSound/WinMM, DirectInput/XInput, Miles, QMixer, FMOD, Bink, DirectShow, Media Foundation |
| `host/` | SDL3 host, Metal/Vulkan/fake GPU backends, audio mixer, presentation |
| `platform/` | `os.h`, the only place that talks to the operating system |
| `mods/` | the mod foundation (Lua 5.4), loader and lifecycle services |
| `games/<id>/` | one game's `game.toml`, code map and native replacements |
| `desktop/`, `web/` | the desktop library, browser launcher and player |
| `tools/` | translator, build and test scripts |
| `third_party/` | vendored Lua, TinySoundFont, minimp3, stb_truetype, volk, Vulkan headers |
