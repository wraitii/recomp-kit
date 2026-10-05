# Function corpus tools

Game-owned real-function corpora pair original instruction provenance with typed
native C, deterministic inputs and explicit observable-result contracts. The kit
owns their build, comparison, timing and report machinery. It never owns game
addresses or reconstructed game logic.

```sh
python tools/build.py --game-dir /absolute/game --function-corpus /absolute/game/benchmarks/functions/corpus.json
# Correctness and size only:
python tools/build.py --game-dir /absolute/game --function-corpus /absolute/manifest.json --corpus-calls 0
```

Outputs stay under the game's ignored `build/function-corpus/`. A manifest uses
`contract: "mapped-native-corpus-v1"`, a fixture `header`, fixture `sources` and
`functions` rows containing `address`, `name`, `instructions_sha256`, `kernel`
and unique nonnegative `fixture_id`. Sources/header resolve inside the manifest
directory. The kit verifies the executable and the instruction-span hashes,
decodes the public code-map boundaries and refuses rewrites/outward transfers.

The game fixture header declares its arena, image base, scratch window, ordinary
return sentinel and benchmark input. It supplies `corpus_setup`, entry reset,
observable-result comparison, post-timing sanity checks and
`corpus_native_trial` for direct typed-API timing. Native adapters export
`native_ADDRESS(X86 *)`; kernels use ordinary host types and pointers, compiled
separately with no LTO. See a game's corpus README for its exact contract.

Four translated modes (eager, CPU locals, x87 locals, combined) share the same
instruction-derived CFG and flag liveness. All optimized modes must match eager
C in full CPU and scratch memory at normal exit. The native reference compares
only the game's declared observations, making its missing machine bookkeeping
explicit. These are current-runtime comparisons, not an original-x86 oracle.

Reports include linked native spans/instruction counts, separate native adapter
and kernel sizes, all timing trials, compile commands, provenance hashes and
generation/build wall times. Timing failures and incomplete checks fail the run;
no successful report is emitted for a failed corpus. `llvm-objdump` is required.
Static SP accesses currently recognize arm64 addressing. Compiler settings are
controlled O2/no contraction/no builtin substitution, not copied production
commands; optional LLVM comparisons below retain their stronger production
configuration checks.

## Consolidated regression and LLVM tools

The former `experiments/x87_locals` and `llvm_compare_native` directories and
their build flags have been replaced by this suite:

- `--corpus-fragments`: [synthetic x87 fixtures](fragments/README.md), preserving
  historical state/rounding regressions and explicitly weaker diagnostic modes.
- `--cpu-locals-checks`: full-state integer/x87 synthetic checks, sharing the
  migrated fragment harness, including null-check fallback and mutating callees.
- `--corpus-llvm MANIFEST`: [optional C/LLVM comparison backend](llvm/README.md),
  preserving byte verification, exact production compile settings, fixture
  checks and raw/lifted LLVM evidence. Outputs: `build/function-corpus-llvm/`.
- `--corpus-llvm-sweep MANIFEST`: optional full-census build/size coverage,
  outputting `build/function-corpus-sweep/`; unfixtureed functions are not run.

LLVM profile manifests use their existing `mapped-normal-exit-v1` schema; they
are not interchangeable with native-reference corpus manifests. Migrating a
function's input contract to LLVM remains explicit. No LLVM mode is activated
for gameplay or silently included in the native-reference report.

Use the game's build wrapper for every native compilation. Do not invoke
compilers directly or retain private bytes/generated code in Git.
