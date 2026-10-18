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

On macOS, an installed folder contains the game app, a native Configure app,
`original/`, and `.recomp/installation.json`. The manifest keeps executable and
data-root paths relative to that folder, so the app resolves them from its own
location, including beneath a checkout. Resolution order is command-line override,
`RECOMP_EXE`, installed manifest, checkout detection, then a saved executable path.
An invalid installed manifest reports an error instead of selecting another copy.
Configure reads its target from the manifest and shares the game's profile and
installation lock. Profiles use the manifest identity under the platform user-data
directory, so renaming the game folder does not change the profile key.

Publication stages both apps on the destination volume, retains backups, and
replaces the manifest last. Rebuilds use and preserve the installed `original/`;
importing another copy requires a new destination. Validated retries retain build
outputs, and dependency caches remain reusable between jobs. The registry changes
after publication. Use **Locate game folder** to attach a moved installation
without rebuilding. Keep both apps, `original/` and `.recomp/` together. macOS is
the library's installation target for this pass; Windows/Linux installed layouts,
direct launches after a move, interruption recovery and Gatekeeper prompts remain
unverified. Copying and renaming the built game bundle preserves its signature and
executable permission; that check does not establish launch behavior.

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
library. On arm64 macOS, add `--package-library --library-python-root /path/to/python`
with a standalone Python installation to bundle the source kit, conversion tools,
CMake, Ninja, SDL and FFmpeg development files, and the Rust renderer. The
`Desktop library` workflow builds and relocates this package without game files;
its ad hoc signed artifact targets macOS 14 and still needs Apple Command Line
Tools to compile games locally. Developer ID signing and notarization are not yet
part of that workflow. Prerequisites, other targets and adding a game are in
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
