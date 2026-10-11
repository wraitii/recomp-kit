# recomp-kit

A static recompilation kit: 32-bit x86 Windows games become native
applications, with no JIT and no emulator at run time. macOS is the main
target and Linux is supported; Windows builds work too, and iOS and Android
builds exist. You supply your own copy of each game.

## Games

| Game | Status | Version |
|---|---|---|
| Black & White | Playable | CD with the 1.42 fan patch |
| Tom Clancy's Ghost Recon | Playable | GOG 1.4 |
| Railroad Tycoon 3 | Playable | GOG |

## Desktop library

Recomp Library is a desktop app, coming soon, that turns your copy into a native
game. Point it at an installed game folder or a GOG offline installer; it
checks the version, builds the port and keeps it ready to play. Black & White
is not sold digitally, so it needs an installed folder with the fan patch.

## Building from source

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python tools/setup.py --game ghost-recon --install "/path/to/Ghost Recon"
.venv/bin/python tools/build.py --game ghost-recon
```

Setup checks the executable against `game.toml` and links your installation;
no game files are copied or changed. `tools/build.py --target library` builds
the desktop library. Prerequisites, other targets and adding a game are in
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
| `dx/` | DirectDraw, Direct3D, DirectSound, DirectInput, QMixer shims |
| `host/` | SDL3 host, Metal/Vulkan/fake GPU backends, audio mixer, presentation |
| `platform/` | `os.h`, the only place that talks to the operating system |
| `mods/` | the mod foundation (Lua 5.4), loader and lifecycle services |
| `games/<id>/` | one game's `game.toml`, code map and native replacements |
| `desktop/`, `web/` | the desktop library, browser launcher and player |
| `tools/` | translator, build and test scripts |
| `third_party/` | vendored Lua, TinySoundFont, minimp3, stb_truetype, volk, Vulkan headers |
