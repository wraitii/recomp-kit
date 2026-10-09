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
creates, and the listings decoded from the code map.

Translator runtime substitutions are opt-in per image. If a game's CRT
`setjmp` or `longjmp` entry points are verified to match the runtime ABI, list
their guest addresses in `game.toml`:

```toml
[translate.intrinsics]
setjmp = 0x00401234
longjmp = 0x00405678
```

Omit this table when the image has no verified entry points. Earlier
translations implicitly treated `0x0055dafc` and `0x0055db78` as these routines;
games that relied on those substitutions must now declare the addresses
explicitly. Addresses must be distinct 32-bit guest addresses.

## Build a game on desktop

`tools/build.py --target app` selects the `macos`, `linux` or `windows`
CMake preset and builds `recomp_app`. `--regenerate` runs the Python
translator on all three platforms. The iOS packager runs on macOS,
including `--target ios --stub` builds. Prerequisites are in
[Contributing](CONTRIBUTING.md).

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python tools/setup.py --game <id> --install /path/to/the/installed/game
.venv/bin/python tools/build.py --game <id> --regenerate
open build/<id>/<AppName>.app
```

Games without a code map also need `--ghidra-home` for setup. `tools/build.py
--stub` links the hosts against a stub translation without game files.

## Dependencies

SDL3 is fetched at its pinned release and linked statically; Lua,
TinySoundFont, minimp3, stb_truetype, volk and Vulkan headers are vendored with
their upstream notices (see [NOTICE](NOTICE)). Video uses a pinned FFmpeg 7.1.1
built from source with a limited decoder set and linked dynamically; its
license and configure flags are in [the FFmpeg notice](third_party/ffmpeg/NOTICE.md).
`-DRECOMP_VIDEO=OFF` disables it.

For Android, set `ANDROID_NDK_HOME`, `ANDROID_HOME` and `JAVA_HOME` for
your NDK, SDK and Android Studio JBR, then run `tools/build.py --target android`
from the prepared game repository (`--stub` needs no translation).
The APK is `build/android/app/build/outputs/apk/debug/app-debug.apk`.
`--push-game` still stages and pushes the original game data separately;
without a ready device the default build skips install and launch.

## Run on an iPad

Requires Xcode with the iOS SDK, an Apple developer team signed in to Xcode,
a paired iPad with developer mode on, and a macOS build already regenerated.

```sh
export RECOMP_IOS_TEAM=<your team id>       # security find-identity -v -p codesigning
.venv/bin/python tools/build.py --target ios --console
```

The build stages the game directory into the app (see `[bundle].exclude` in
the game's `game.toml`), signs it, installs it with `devicectl` and streams
the console. Touch: tap = left click, long press then lift = right click, long
press then drag = wheel-button drag, a hold on a screen edge scrolls, drag =
left drag, two-finger drag pans, two-finger tap = right click between the two
fingers, three-finger tap = F10 (Options), four-finger tap toggles the system
keyboard. On-screen controls — a gamepad, a keyboard, or both — are drawn over
the game; see "On-screen controls" below.
RECOMP_* switches reach the device through `Documents/switches.txt` (NAME=VALUE
lines), copied in with `xcrun devicectl device copy to --domain-type
appDataContainer --domain-identifier <bundle id>`. `tools/ios_logs.py` pulls
the app's Documents (saves) back to the Mac.

## On-screen controls

Design: `docs/superpowers/specs/2026-09-17-touch-controls-design.md`.

**Playing.** Three layouts ship with every game — `pad` (a PlayStation-style
gamepad: two sticks, a dpad, ✕○□△, shoulders, triggers, start and select),
`keys` (the split keyboard: HIDE/KEYS tabs per half, Shift/Ctrl/Alt hold to
chord, tap to latch, double tap to lock) and `pad+keys` — plus a Hidden
slot. A small tab cycles between them, and the F10 page has the same list
along with the controls' size, their opacity, button haptics, whether the
pad stays on screen while a controller is connected, and "Edit controls".
A keyboard layout hides itself when a hardware keyboard is attached, and a
pad layout hides itself when a controller is connected; its tab stays so
you can bring it back.

A physical controller (DualSense, Xbox, MFi) is picked up over USB or
Bluetooth on desktop, iOS and Android, and drives the same pad. Game rumble
goes to the controller, or to the phone or tablet's motor when there is
none.

Phones may be held in portrait: the game sits at the top at full width and
the controls fill the space below it, so nothing covers the game. Tablets
stay landscape.

**Editing a layout.** "Edit controls" on the F10 page freezes input (the
game keeps running) and opens the editor. Drag to move a control, pinch to
resize it, and use the toolbar to add, delete, rebind, switch a stick
between fixed and floating, turn snapping off, switch or rename a layout,
or reset to the game's default. Done saves to
`<profile>/controls/<layout>.<form>.json`, where `<form>` is `tablet`,
`phone-landscape` or `phone-portrait`, so a phone in portrait and a tablet
keep separate layouts. The files are plain JSON and can be edited by hand.

**For a game repo.** Put starting layouts in the repo's `layouts/`
directory, named `<layout>.<form>.json` (or `<layout>.json` for every
form). `tools/build.py` and `tools/package_desktop.py` copy them into the
app; on Android they travel as APK assets and are unpacked on first run.
Anything not shipped falls back to the kit's built-ins. `game.toml`
configures the rest:

```toml
[controls]
default_layout = "pad"      # "pad" | "keys" | "pad+keys" | "hidden"
pad = "mapped"              # "mapped" (keys and mouse) | "native" | "off"

[controls.mapped]           # only the entries you want to change
left_stick = "arrows"       # "cursor" | "arrows" | "wasd" | "scroll" | "wheel" | "none"
cross = "mouse_left"        # "key:<name>" | "mouse_left|right|middle" | "wheel_up|down" | "action:<name>" | "none"
```

Use `pad = "native"` when the game reads a controller itself: the pad is
then served as a DirectInput joystick and through `xinput1_3`, `xinput1_4`
and `xinput9_1_0`, and the game's rumble comes back out. `[controls]`
replaces the old `[touch] keypad`, which is still accepted (`"auto"` →
`"keys"`, `"hidden"` → `"hidden"`).

## Run in a browser

Requires the Emscripten SDK (`source emsdk_env.sh`) and a macOS or Linux
build already regenerated, or `--stub`.

```sh
.venv/bin/python tools/build.py --target web
.venv/bin/python kit/tools/web_launcher.py --game-dir "$PWD" --out build/web-site \
    --web-build <game id>=build/web/recomp --serve 8000
```

`--target web` builds the `web` preset (WebGPU, pthreads, WasmFS) and writes
`build/web-site`: the launcher page with the game's build in `<game id>/`.
Serve it from localhost or over HTTPS with `Cross-Origin-Opener-Policy:
same-origin` and `Cross-Origin-Embedder-Policy: require-corp`; `--serve`
does that for local testing. The launcher imports the player's copy of the
game into the browser's private storage, and Play opens the game page, which
asks for a WebGPU device (tested in Chrome on macOS).
The game runs on a worker; the browser's main thread owns SDL, WebGPU and the
presenter. `RECOMP_*` switches go in the page's query string
(`<game id>/?RECOMP_D3D9_STATS=1`). Reading a render target back is not
supported in the browser.

## Other platforms and GPU APIs

Direct3D 9 renders on Metal (macOS, iOS), Vulkan (Linux, Windows, and macOS
through MoltenVK with `RECOMP_GPU_BACKEND=vulkan`) and WebGPU (the
web), all from one shader generator. Windows builds cross-compile on macOS or
Linux with llvm-mingw: set `LLVM_MINGW_ROOT` and pass
`--preset windows-cross` (or `windows-cross-stub`) to `tools/build.py`; the
executable lands in `build/windows/recomp/` (`build/windows-stub/recomp/` for
the stub). These presets enable FFmpeg movies
and file-backed music, building the three shared media DLLs with llvm-mingw and
copying them beside the executable. The build host needs a POSIX shell and GNU
make; MSYS2 is only required when building on Windows itself. Translated app
packages, including the DLLs and their notice, land in `build/windows/package/`.

## Check a change

```sh
.venv/bin/python tools/test.py                  # portable Python suites, on the stub game
.venv/bin/python tools/format.py                # handwritten native code style
.venv/bin/python tools/check_repo.py            # nothing private is tracked
.venv/bin/python tools/check_game_literals.py   # kit code names no game
.venv/bin/python tools/test.py --native         # native suites; game-labelled ones skip on the stub
.venv/bin/python tools/test.py --game-dir /abs/<game> --native   # the same against a real game
```

Nothing under `runtime/`, `dx/`, `host/` or `platform/` may name a game;
`tools/check_game_literals.py` enforces that. Game-specific documents live
with their game.

The experimental [instruction IR](docs/ir.md) lifts original x86 bytes with
SLEIGH and supports `translate.py --ir-census FILE` for calling-convention
analysis. Opt-in `[translate] ir_ssa = true` now selects supported production
function bodies after decoded boundary discovery, retaining decoded fallback
and the existing dispatch ABI. Translation reports record emitted/fallback
percentages and reasons. Integer SSA and an initial C emitter
with corrected ordered x87 effects can be checked in an isolated function corpus
with `--corpus-ir-ssa`; unsupported
functions retain the existing emitter, with per-function fallback reasons.
