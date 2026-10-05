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
An optional per-row `callees` list names other reviewed corpus rows: only those
decoded direct calls are permitted, and each invokes the callee in the same
translation mode, in a separate translation unit. Callee rows retain their own
byte hashes, fixtures and native references. Only mapped CALL continuations and
the fixture's return sentinel are accepted as returns; indirect calls, tail
transfers, SEH and undeclared dispatch still fail. No callee is stubbed.

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

`--corpus-x87-dataflow` enables decoded x87 dataflow only in x87-local and
combined modes. The default is off; `report.json` records `x87_dataflow` and
Markdown identifies the selected setting. This does not change the game
configuration. A whole-image experiment can set `[translate].x87_dataflow = true`
with `x87_locals = true` and regenerate, but needs its own build and replay
validation; corpus results alone do not establish whole-image correctness.

Reports include linked native spans/instruction counts, separate native adapter
and kernel sizes, all timing trials, compile commands, provenance hashes and
generation/build wall times. Function text spans exclude separately compiled
callee bodies; call-path timings include the actual executed callees. Native
kernel spans likewise exclude out-of-line native callees. Timing failures and
incomplete checks fail the run;
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

`--corpus-stack-forwarding` additionally enables bounded binary32 guest-stack
forwarding and requires `--corpus-x87-dataflow`. The separate setting is
`[translate].x87_stack_forwarding` (default false, requires `x87_dataflow`).
Within an admitted decoded region, up to four stores can supply later FLD
reloads along a single basic block, at most 32 instructions away. Guest stores
and their `fto_float` conversion remain; possible writes, ESP mutations,
branches, calls and observers kill the proof. Nothing is forwarded across a
join or call. `report.json` records both experimental settings.

`--x87-dataflow-checks` also exercises forwarding in its x87 variants, including
rounded reloads, partial aliases, a status/plane loop and a mutating call.

`--corpus-decoded-dataflow` requires `--corpus-x87-dataflow` and enables
`[translate].decoded_dataflow` only in the combined variant. The whole-image
setting defaults false and requires `cpu_locals` plus `x87_dataflow`. A bounded
2048-instruction decoded CFG analysis tracks definitions and consumed flags
through loads and audited x87 instructions. Unknown instructions, integer
stores with observer callbacks, calls and external exits require every flag.
Logical instructions preserve runtime AF. Branch-heavy bodies can carry all
six audited GPRs plus written flags; opaque edges publish and refresh locals.
No outgoing CPU residue or guest stores are discarded, and no callee ABI is
assumed. `RECOMP_NULL_CHECKS` uses eager flag recipes and CPU lvalues.

`--decoded-dataflow-checks` runs the full-state synthetic fixtures with the
combined variant enabling decoded flags and forwarding. Both ordinary and
null-check binaries run, including aliases, opaque region exits and mutating
callees. The corpus eager variant emits every flag, providing a stronger oracle
than the older conventionally optimized eager variant. Current-runtime matches
still do not establish original-x86 equivalence.
