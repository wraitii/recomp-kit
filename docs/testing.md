# Testing

Run checks appropriate to your change. Every suite's output belongs under ignored
`build/`; requested tests must report failure rather than silently skip prerequisites.

| Command | What it checks | Needs game files? |
| --- | --- | --- |
| `tools/test.py` | Setup failures, synthetic texture processing and tooling regressions | No |
| `tools/format.py` | Consistent formatting of handwritten native code | No |
| `tools/test.py --compile-only` | Every native test binary this platform has compiles (macOS, Linux, Windows) | No |
| `tools/test.py --native` | Portable suites everywhere; runtime, adapters, offscreen Metal and UI tests on macOS | Partly: `game`-labeled suites need the image |
| `tools/test.py --mods` | Game-backed mod loader, hook and replay contracts | Yes, plus translated archive |

Native suites are CTest entries with labels: `nogame` runs everywhere and in CI,
`game` needs your installation, `gpu` needs a Metal device, `device` needs a
real Metal and audio device (the offline audio render is not what a hosted CI
runner produces), and `mods` needs the translated archive. Run one directly
with `.venv/bin/ctest --test-dir build/cmake/macos -L nogame` or `-R dx_tests`.
Two suites cover the on-screen controls, both `nogame`: `controls_tests` for the
layout model, geometry, routing, virtual pad, mapped binding, raster primitives
and editor (including the old keypad geometry as a regression oracle), and
`pad_tests` for the DirectInput joystick and the three XInput DLLs, which lives
beside `dx_tests` because it defines the `host_pad_*` callbacks strongly.
Every game-backed `tools/test.py` mode takes `--game-dir /abs/path/to/<game>`; without it
the kit's stub game is used and the `game`-labelled suites report a skip.

`d3d8_surface_tests` (`nogame`) exercises the backbuffer descriptor, texture
levels and staging, vtable calling convention, identity, bounds and
device/factory ownership through guest COM dispatch. Its devices are
constructed state fixtures; it does not create a GPU or demonstrate game
rendering. When a game provides the optional D3D8 Rust crate, this same suite
uses its real CPU storage and mip-layout ABI; standalone kit profiles exercise
the C++ fallback. Live locked resources are also exercised across generation
reset. `d3d8_abi_cpp_check` and a C compilation fixture check the generated
header's sizes and offsets when the Rust crate is present. `--unsupported-lock` (a lock on the host render target) and
`--unsupported-texture` (CreateVolumeTexture) are child probes expected to
abort with named diagnostics, including with `RECOMP_LOG=0`. When running
`runtime_tests --startup-contracts`
directly, set `RECOMP_PYTHON` to the absolute Python interpreter containing
`pefile`; CTest normally supplies that setting.

Invoke these with `.venv/bin/python`. The native tests need a macOS Metal device;
CI compiles them but does not claim GPU or original-game execution. The mod suite
runs against the selected game's translated archive.

For a Windows cross-build, set `LLVM_MINGW_ROOT`, then run
`tools/build.py --preset windows-cross --stub --target app` and
`tools/test.py --preset windows-cross-stub --compile-only`. CI verifies the
Indeo 5/AVI and Vorbis/Ogg components and uses Wine to load `dx_tests.exe` with
only its three copied FFmpeg DLLs. `RECOMP_TEST_AUDIO=/path/to/track.ogg` exercises
the CD-music decoder and requires nonzero PCM; CI generates its own sine tone.
`RECOMP_TEST_AVI=/path/to/movie.avi` exercises the guest AVIFile/Indeo imports,
requiring changing nonblack frames. Movie probes use private local inputs,
never uploaded. These headless probes do not open an audio device or game window.

For instruction-translation changes, use the original differential harness:

`tools/build.py --game-dir <game> --target dispatch-tests` builds the synthetic
entry-boundary suites (with and without hooks) and the real generated-table
profiling suite. Run them with `ctest --test-dir <build dir> -R
'entry_dispatch|profile_tests'`. The synthetic suite checks native selection,
raw-original access, nested/tail calls, hook delegation/removal and frame
diagnostics. It needs no original game behaviour; the profile suite needs a
generated image and exercises real direct/indirect dispatch and unwind cleanup.

```sh
.venv/bin/python tools/recomp/tests/test_translate.py --help
.venv/bin/python tools/recomp/tests/test_translate.py
```

Three translator suites need no game and run in `tools/test.py`:
`test_translate_insns.py` runs synthetic listings of individual instruction
forms through the translator, compiles them with the test harness and
compares registers, flags and memory with Unicorn; `test_jumptables.py`
decodes the jump-table shapes on synthetic functions over a fake image;
`test_translate_driver.py` covers driver rules such as what a withdrawn block
leaves behind. Add a case there first when the translator meets an
instruction or table shape it does not handle.

`tools/recomp/tests/test_cpu_c_locals.py` also runs in the portable suite. Native
CPU/x87 local-value checks run through `tools/build.py --cpu-locals-checks`.
They use the production driver and compare full CPU/scratch memory for eager,
CPU-local, CPU+x87-local and x87-local variants in ordinary and null-check builds.
Opaque callees record entry state and mutate cached fields; the null-check build
also checks CPU state at an injected access failure. These are synthetic mapped
tests against the current runtime, not original-x86, real hooks, real guest SEH
or gameplay evidence. No benchmarks or profile captures run in this mode.

Real game-function corpora run through `tools/build.py --function-corpus MANIFEST`.
Use `--corpus-trial-ms 0` for correctness and code sizes without timing. They compare
four translated C modes in full CPU/scratch state, then compare a reviewed typed
native reference using explicit game-owned observations. Timing reports separate
guest-state adapters from direct typed native kernels. See
[the corpus tools](../tools/recomp/corpus/README.md) for provenance, measurement
contracts and the consolidated fragment modes. Game assembly and outputs
remain private; these checks do not establish original-x86 equivalence.

`tools/build.py --ir-ssa-checks` compares the experimental integer SSA C emitter
with eager C using 163 byte-backed synthetic fixtures, 24576 inputs each,
and complete CPU/2 KiB scratch comparisons. It covers partial registers, loops,
memory aliases, INC/DEC/SBB/ADC, memory RMW snapshots, extensions, masked shifts,
checked signed/unsigned division, multiplication, all SETcc conditions, absolute
memory accesses and declared calls with both mock and byte-translated callees. Explicit indirect
call fixtures cover register and ESP-relative targets, live x87 state, and normal
or diverted resumable continuation. Dword string moves cover zero count, both
DF directions and overlapping copies with store observations.
Raw (`optimize=False`), scalar with strict state, scalar-strict and the
production scalar/local-state policy are compared in ordinary and null-check builds. Scalar/local-state retains
complete outgoing state while deferring load and store observations: the scalar column
compares store snapshots without x87 stack, tag, TOP and status fields, which scalar x87
does not publish at guest accesses, and the scalar/local-state column also without GPRs
other than ESP/EBP and the arithmetic flags. Null-check builds select strict publication. x87 checks cover all TOP,
PC and RC combinations, exact integer metadata, special and finite inputs,
80-bit memory, register directions, status/rounding/remainder and classification.
Native read-only store observers additionally compare complete CPU snapshots,
addresses, widths and values, with explicit branch-join, loop-backedge and
partial-word-update fixtures. Carried x87 fixtures cover forward joins at
different depths, mixed dirty parts, FXCH and live values on loop backedges,
a Cull-style FCOMP/FNSTSW branch, popped-tag publication, an unbalanced loop
that must fall back and a join with a path that went through a call.
Zero-divisor and overflow fixtures use a mock returning error handler that
records CPU/fault address and changes EAX/EDX, EBX/ESI and arithmetic flags. These are current-runtime checks,
not original-x86, actual guest SEH or interior memory-fault equivalence.

The differential harness compares translated routines with original instructions
under Unicorn. Unicorn is a development tool, not part of the playable app.

Game-specific gameplay and original-binary comparisons are maintained by each
game's repository. Report that evidence alongside the game and build
configuration; kit compilation and synthetic fixtures alone do not establish
original-game equivalence or playable performance.

### D3D8 interface generation

`tools/gen_com_interfaces.py` emits IID bytes and full vtables from a pinned Wine
D3D8 header plus `dx/d3d8_bindings.json`, whose only API choices are handlers.
Unmapped methods become named unsupported calls. Use `--reference third_party/wine-d3d8/d3d8.h
--bindings dx/d3d8_bindings.json --output build/d3d8-generated/d3d8_interfaces.inc`; `--check`
verifies drift without writing. CMake generates the include into the build
tree automatically; it records the source hash. No generated code is tracked.
`tools/gen_d3d8_constants.py` likewise resolves the names in `dx/d3d8_constants.json`
from `d3d8types.h`/`d3d8caps.h` into `build/d3d8-generated/d3d8_constants.inc`; an
unknown name fails generation, and a constant the Rust ABI header also defines is
`static_assert`ed equal to the Wine value.
A game repository can check this boundary and its cbindgen-generated host ABI
with its own code-generation wrapper. Generator regressions run in the portable suite.

When the Rust renderer is enabled, `d3d8_render_tests` (`gpu`) calls the
production D3D8 bridge through guest COM, clears a texture's level surface,
and samples the parent texture with a synthetic pixel shader. Metal readback
must show the rendered color rather than stale CPU staging bytes; a readonly
surface lock checks the corresponding guest readback identity.

The Rust D3D8 renderer tests and offscreen probe can be run through
`tools/test.py --game-dir /abs/game --d3d8-wgpu test` and `--d3d8-wgpu probe`.
They require graphics adapter access; an unavailable adapter fails the requested
suite. Shader tests include WGSL validation and Metal readback for constants
and dependent bump sampling; see [shader coverage](../graphics/d3d8-wgpu/SHADERS.md).
