# recomp-kit

A static recompilation kit: 32-bit x86 Windows games become native
applications for macOS, iOS, Android, Linux and Windows, with no JIT and no
emulator at run time. Supported games live in `games/<id>/`; you supply your own
copy of each game.

## Layout

| Directory | What it holds |
|---|---|
| `runtime/` | x86 semantics (`x86.h`), guest memory, PE loader, scheduler, kernel32/user32 shims |
| `dx/` | DirectDraw, Direct3D 2, DirectSound, DirectInput, QMixer shims |
| `host/` | SDL3 host, Metal/Vulkan/fake GPU backends, audio mixer, presentation |
| `platform/` | `os.h`, the only place that talks to the operating system |
| `mods/` | the mod foundation (Lua 5.4), loader and lifecycle services |
| `games/<id>/` | one game's `game.toml`, code map and native replacements |
| `games/stub/` | a game that does not exist: the values game-free builds and CI configure with |
| `tools/` | translator, build and test scripts |
| `third_party/` | vendored Lua, TinySoundFont, minimp3, stb_truetype, volk, Vulkan headers |

## Games

A game directory holds only what is the game's:

```
games/<id>/
  game.toml       identity, translator inputs, hooks and host settings (see games/stub/game.toml)
  metadata/       the code map: function spans, no instruction bytes
  native/         optional native replacements for translated functions
```

Every kit tool takes `--game <id>`, or `--game-dir <path>` for a game kept
elsewhere. Outputs go to `build/<id>/` for a kit game and `<game-dir>/build`
otherwise; `--build-root` or `RECOMP_BUILD_ROOT` overrides both. The build root
also holds `original/`, the link to your installation that `tools/setup.py`
creates.

Nothing under `runtime/`, `dx/`, `host/` or `platform/` may name a game;
`tools/check_game_literals.py` enforces that. Game-specific documents live
with their game.

Translation ([docs/ir.md](docs/ir.md)) reads the game's Ghidra code map and the
executable, lifts each function with SLEIGH and emits SSA-based C; a small set of
unsupported shapes falls back to a plain eager emitter, named in the translation
report. `tools/recomp/diff/run.py` and the function corpus (`--function-corpus`)
check translated functions against the original bytes and against each other.
