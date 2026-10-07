# Function corpus tools

Game-owned real-function corpora pair original instruction provenance with
deterministic inputs and explicit comparison contracts, optionally including
typed native C references. The kit
owns their build, comparison, timing and report machinery. It never owns game
addresses or reconstructed game logic.

```sh
python tools/build.py --game-dir /absolute/game --function-corpus /absolute/game/benchmarks/functions/corpus.json
# Correctness and size only:
python tools/build.py --game-dir /absolute/game --function-corpus /absolute/manifest.json --corpus-trial-ms 0
```

Timing is budgeted, not counted: `--corpus-trial-ms N` (default 10) gives each
eager trial N ms. For every row the harness first doubles a call count, untimed,
until one eager batch (the slowest translated variant) reaches the budget, then
uses that one count for every variant, the native kernel and every rotating trial
of the row, so ratios within a row stay paired. The calibrated count is printed
(`CALLS row n`) and recorded per row, with the budget, in `report.json`, `.csv`
and `.md`. Rows are calibrated just before their own trials, so a thermally
throttled machine affects each row's calibration instead of skewing late rows.

`--corpus-asan` builds the corpus with AddressSanitizer for correctness runs; it
requires `--corpus-trial-ms 0`, and the report notes that code sizes are instrumented.

Outputs stay under the game's ignored `build/function-corpus/`. A manifest uses
`contract: "mapped-native-corpus-v1"` or `"mapped-comparison-corpus-v2"`, a
fixture `header`, fixture `sources` and `functions` rows containing `address`,
`name`, `instructions_sha256` and unique nonnegative `fixture_id`. Sources and
header resolve inside the manifest directory. The kit verifies the executable
and the instruction-span hashes, decodes the public code-map boundaries and
refuses rewrites/outward transfers.

`mapped-native-corpus-v1` is unchanged: every row has a `kernel`, and an
optional per-row `callees` list names other reviewed corpus rows. Only those
decoded direct calls are permitted, each invokes the callee in the same
translation mode in a separate translation unit, and no callee is stubbed.
Indirect calls, tail transfers, SEH, boundary declarations and undeclared
dispatch fail.

`mapped-comparison-corpus-v2` preserves every native-reference row and adds
explicit `comparison: "translation-only"` rows that have no native kernel or
adapter. Translation-only rows omit adapter/kernel results from generated
tables, timing, JSON, CSV and Markdown; the runner never fabricates native
results. The v1 `callees` contract still applies. A row may additionally declare
modeled boundaries, which are validated exactly against the decoded calls:

- `memory_ranges`: a nonempty list of `[start, size]` integer pairs. The harness
  snapshots those guest ranges in declaration order and compares every byte
  against eager C. Omitted, it defaults to the header's
  `CORPUS_SCRATCH`/`CORPUS_SCRATCH_SIZE` window, which is not enlarged.
- `boundary_stubs`: a map of original direct-call target `address` to fixture
  function symbol. The stub and `callees` declarations must exactly partition
  the decoded direct calls and be disjoint.
- `indirect_calls`: the exact original CALL instruction site addresses.
- `indirect_targets`: a map of guest fixture target token to fixture symbol, the
  explicit whitelist for those sites.

A row with `boundary_stubs` or `indirect_calls` is a boundary row. Generated
per-mode wrappers call `corpus_boundary(c, target)` before every declared direct
callee or fixture stub. Each indirect site is rewritten from the runtime
`recomp_call`/`CALLIND` path to one generated mode dispatch that calls
`corpus_boundary` and then the whitelisted fixture symbol; an unknown target
aborts with a named diagnostic. `recomp_jump` and external tail transfers stay
rejected. Stub target addresses mark modeled boundaries and are explicitly
reported; real callees retain independently byte-verified translated bodies.

The game fixture header declares its arena, image base, ordinary return sentinel
and benchmark input. It supplies `corpus_setup`, entry reset, observable-result
comparison, post-timing sanity checks and `corpus_native_trial` for direct
typed-API timing. Native adapters export `native_ADDRESS(X86 *)`; kernels use
ordinary host types and pointers, compiled separately with no LTO. See a game's
corpus README for its exact contract.

### Boundary hooks (v2)

A v2 fixture header may define `CORPUS_BOUNDARY_HOOKS` and provide:

```c
void corpus_boundary(X86 *c, uint32_t target);
void corpus_movs_site(X86 *c, int rep, uint32_t site);
void corpus_variant_begin(unsigned fixture, unsigned mode, int checking);
void corpus_variant_end(unsigned fixture);
void corpus_validation_end(unsigned fixture, unsigned checks);
```

The harness calls `corpus_variant_begin` **before** `corpus_setup` (setup calls
its own `corpus_boundary_case_begin` and expects the active mode), runs the
variant, then calls `corpus_variant_end`. The same bracket wraps each timed
workload with `checking = 0`. `corpus_validation_end` runs once per row after
its checks. A mapped `movsd`/`rep_movsd` in a v2 translation is rewritten to
`corpus_movs_site(c, rep, site)` using the decoded instruction EIP (from the
instruction comment; for IR SSA, from the emitter's `B<index>` block, which is
an index into `fir.insns`). The hook runs the selected runtime move helper exactly once; its
instrumentation must not alter guest state. Any helper call that cannot be
attributed to a site fails generation rather than silently skipping coverage.

`corpus_validation_end` may print one `COVERAGE <fixture_id> <json>` line whose
payload is an object of nonnegative integer counts. Native-reference rows may
omit it; every translation-only row must emit exactly one. The runner rejects
missing, duplicate, unknown-fixture, non-object and negative/non-integer
records and records the object as row coverage. Coverage is game evidence and
is reported alongside, not instead of, the byte comparisons.

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
commands.

## Consolidated regression tools

The former `experiments/x87_locals` directory and its build flag have been
replaced by this suite. The LLVM comparison, sweep and x87 stack-to-SSA
experiments were removed once none of them fed production; their history is in Git.

- `--corpus-fragments`: [synthetic x87 fixtures](fragments/README.md), preserving
  historical state/rounding regressions and explicitly weaker diagnostic modes.
- `--cpu-locals-checks`: full-state integer/x87 synthetic checks, sharing the
  migrated fragment harness, including null-check fallback and mutating callees.

Use the game's build wrapper for every native compilation. Do not invoke
compilers directly or retain private bytes/generated code in Git.

`--corpus-ir-ssa` tries the experimental integer p-code SSA emitter in the
combined variant, preserving the same full-state checks against eager C.
Unsupported functions use the existing emitter; each row's `ir_ssa` records
whether emission succeeded and the fallback reason. Audited x87 instructions
use byte-backed effects lowered by the scalar x87 tracker; raw floating
p-code, unsupported x87 forms, locked operations and unbound calls retain fallback.
Declared reviewed direct callees are bound explicitly and use the same mode.
The policy (default scalar x87 and local CPU state, the production policy) is
recorded in JSON and Markdown.
This mode runs separately from decoded-dataflow experiments and does not enable
production IR emission. See [IR limitations](../../../docs/ir.md).

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

The default `--corpus-ir-ssa-x87 scalar` uses scalar x87 stack/environment state
with exact outgoing residues; `scalar-strict` retains pre-load observations and
general arithmetic. The default `--corpus-ir-ssa-state locals` separately defers
ordinary read GPR/flag snapshots, retaining diagnostics and complete
store/call/exit state; `strict` keeps them. The default policies keep a strict
path under `RECOMP_NULL_CHECKS=1`, and the report records the selection. See [the IR contracts](../../../docs/ir.md#scalar-x87-and-local-cpu-state).

### Experimental SSA ceiling column

`--corpus-ir-ssa-ceiling A,C,D,E|all` (requires `--corpus-ir-ssa`, scalar x87 and
locals state) adds an unproven, corpus-only variant between `combined` and
`native`; see [the IR contract](../../../docs/ir.md#ssa-ceiling-experiment-corpus-only-unproven).
Its harness records start with `CEILING row checked skipped observation memory eax st0 boundary
first_input reason` and `CEILING_BENCH row valid`. The default mode order, generated
tables and existing variants are unchanged when the option is omitted. Fixtures
may read `corpus_relaxed_boundaries`/`corpus_relaxed_boundary_mismatches` (defined
by the harness) in their boundary hooks to count rather than abort for that variant.

