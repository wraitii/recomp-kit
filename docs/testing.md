# Testing

| Command | Checks | Game needed |
| --- | --- | --- |
| `tools/test.py --native` | Native CTest suites (runtime, DirectX, Metal, controls) | `game`-labelled suites only |
| `tools/test.py --compile-only` | Native test binaries build | No |
| `tools/test.py --mods` | Mod loader, hooks and replay against the translated archive | Yes |
| `tools/test.py --d3d8-wgpu test\|probe` | Rust D3D8 renderer tests or offscreen probe | Yes, plus a GPU |
| `tools/format.py` | Native code formatting | No |

CTest labels: `nogame`, `game`, `gpu`, `device`, `mods`. Run a subset with
`ctest --test-dir <build root>/cmake/macos -L nogame` or `-R dx_tests`.

`d3d8_surface_tests` (`nogame`) exercises the backbuffer descriptor, texture
levels and staging, vtable calling convention, identity, bounds and
device/factory ownership through guest COM dispatch. Its devices are
constructed state fixtures; it does not create a GPU or demonstrate game
rendering. It uses Rust CPU storage and mip layouts through CXX, including
live locked resources across generation reset.
`d3d7_mipmap_tests` (`gpu`) follows DirectDraw implicit mip attachments and
per-level locks through D3D7 texture binding and real GPU readback. It checks
capability reporting, point/linear mip filtering, fractional LOD bias,
MIPFILTER=NONE and updates to a child without rebinding the root. `dx_tests`
covers rectangular mip dimensions/counts, attachment traversal, independent
storage, one-level requests and partial allocation rollback without a GPU.
`--unsupported-lock` and
`--unsupported-texture` are child probes expected to abort with named
diagnostics. When running `runtime_tests --startup-contracts` directly, set
`RECOMP_PYTHON` to the absolute Python interpreter containing `pefile`.

`tools/recomp/diff/run.py` compares translated game functions with Unicorn, and
with `--mode paired` compares the production specialization-enabled and forced
ordinary variants on each shared input; see `AGENTS.md`. The function corpus
(`tools/build.py --function-corpus`) compares the
eager and SSA translations of real game functions; see its
[README](../tools/recomp/corpus/README.md).

Diff reports include separate ordinary/original, specialized/original and
ordinary/specialized findings, with the first failing input and iteration.
Every run keeps its linked harness beside `report.json` and `run.json` in a new
directory; `--no-build` copies the prebuilt library under the build lock.
`run.json` records executable, harness, config, code-map and fact hashes, the
translation fingerprint, selected functions and execution limits. Baseline
snapshots are read and written only through explicit paths; existing report or
baseline destinations cannot be overwritten. Writable image sections,
BSS/scratch/TEB fills and the full zeroed stack are reset before every execution.

## Media probes

`RECOMP_TEST_AUDIO=/path/to/track.ogg` and `RECOMP_TEST_AVI=/path/to/movie.avi`
exercise the Vorbis and AVI/Indeo decoders in the native suites.

The diff harness reads `backends.json` when present, including LLVM body
hashes and C bridges in change selection. `--record-inputs DIR` saves every
input, including discarded inputs and complete scratch/BSS/TEB fills, in a
fresh directory. `--replay-inputs DIR` checks executable, selected functions
and input settings before replay. Original memory and all native memories are
reset independently for each mode.

Use `--backend-name NAME --compare-backend NAME=HARNESS` (repeatable) to compare
saved CPU-field C, value C and LLVM harnesses on one input set. `--mode paired`
compares ordinary/private modes within every backend. `pair` contains those
within-backend discrepancies; `native_pair` contains discrepancies against the
primary backend by mode; `oracle` remains separate for every backend/mode.
`--fail-on pair` includes native pairs while retaining the non-clean oracle
baseline. Library hashes and backend provenance are recorded in `run.json`.
A saved library's coverage must be supplied with its build evidence; its name
alone does not prove a private route was executed. Existing x87 slot/status,
fault-discard and gameplay coverage limits still apply.
