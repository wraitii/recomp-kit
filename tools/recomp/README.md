# Translator and low-level tools

The contributor entry points are [setup.py](../setup.py), [build.py](../build.py)
and [test.py](../test.py). They check prerequisites and select the commands below.
Use those wrappers for an ordinary first build.

| Tool | Responsibility |
| --- | --- |
| `translate.py` | Decode instruction listings, resolve dispatch entries and emit C plus symbols |
| `runtime/x86.h` | Register/flag/x87 state and instruction semantics used by generated code |
| `symbols/` | Reviewed metadata for stable hooks and guest globals |
| `buildlock.sh`, `buildlock.py` | The shared process lock over build/recomp, as a shell entry and a Python module |
| `build_core.py`, `finish_bundle.py`, `snapshot_gen.py` | Install core mods reproducibly, finish the app bundle, copy one consistent generation of gen/ |
| `texture_pack.py`, `package_texture_pack.py` | Prepare and package optional local replacement textures |
| `presentation_smoke.py`, `performance_run.py` | Bounded presentation measurements and diagnostics |
| `tests/` | Differential instruction tests and tooling regressions |

Generated functions preserve addresses, symbol names and instruction comments.
They are a mechanical translation, so register operations are expected. Add
meaningful names in reviewed symbol metadata and comments in the translator;
do not hand-edit generated chunks or publish them.

A normal translation needs the exact executable and listings prepared by setup.
`tools/build.py` publishes the generated directory and `symbols.json` by rename
under the lock, then CMake rebuilds `librecomp_gen.a` from it; a reader under the
same lock never sees half a generation.

Game-specific smoke scripts and original-game comparisons belong in the game
repository. See [Testing](../../docs/testing.md) for the kit's generic suites.
