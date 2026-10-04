# Translator C/LLVM comparison

`tools/build.py --llvm-compare /absolute/path/codegen.json` is an opt-in,
**build-only** path through `translate.py`. It does not modify production chunks,
dispatch tables, overrides, or the application's backend. Requires LLVM 22 plus
an existing native Release C translation and its CMake `compile_commands.json`.
Configure/build the normal C target first (`tools/build.py --regenerate --target
gen --game-dir /absolute/game`); subsequent comparisons do not regenerate it.
The game wrapper supplies `--game-dir` when available.

Manifest (paths relative to the manifest):

```json
{
  "contract": "mapped-normal-exit-v1",
  "functions": ["leaf-profile.json"]
}
```

Each function profile uses the existing
[x87 LLVM profile fields](../experiments/x87_llvm/README.md), plus
`codegen_fixture`: a game-owned header using the shared harness with four
functions `compare_c_native`, `compare_c_llvm`, `compare_raw`, `compare_lifted`.
Its mode table labels should distinguish the production compiler, LLVM22 C,
raw semantic LLVM and lifted LLVM. CMake defines `RK_CODEGEN_TEST`,
`RK_DIRECT_TEST`, and `RK_FUNCTION_TEST`; the header can reuse an experiment
fixture using these conditionals. Numeric inputs/addresses remain game-owned.

`synchronize_cfg: true` opts into bounded loops and ordinary synchronous calls.
List every direct callee as a hex string in `call_targets`; the fixture must define
`void rk_fixture_call(X86 *, uint32_t target)` and reject unexpected targets.
Both C and LLVM call the same generated opaque `entry_ADDR` thunk, then the same
harness dispatcher. There is no callee implementation or game dispatch change.
The fixture can observe and mutate complete state; compare these boundary events
as well as final state. Unknown, intrinsic and resumable continuations fail.
`FIXTURE_SCRATCH_SIZE` widens the default 256-byte compared window at `0x10000`;
all fixture writes must fit. Optional `FIXTURE_AFTER_STATE(mode,c)`,
`FIXTURE_BENCH_SETUP(c)` and `FIXTURE_BENCH_CHECK(c)` support call-boundary checks,
untimed geometry setup and a function-specific post-timing sanity check.

## Emission and preservation

1. Load the game configuration and PE through the normal translator entry point.
   Verify executable/function hashes, the listing extent and every decoded
   instruction/operand against the bytes. Normalize only spelling aliases.
2. Both emitters receive that same validated `Function.insns`. C goes through
   `Translator.prepare` and `Translator.translate` with default flag liveness,
   not an instruction-only loop or eager-flags approximation. LLVM reuses the
   bounded semantic emitter and existing stack/SSA/effect/materialization passes.
3. Refuse unsupported instructions, noncontiguous extents, configured
   instruction rewrites, intrinsics, SEH, pushed continuations and jump tables.
   The compiled LLVM analysis refuses incoming stack operands and incompatible
   joins. Cycles and calls are rejected by default; synchronized mode requires
   zero local depth at each loop cut/call, full predecessor snapshots, and a fresh
   state model after the boundary. There is no fallback labeled as successful LLVM.
4. The build wrapper requires the resulting C body to match exactly one existing
   production chunk and its copied `x86.h` to match the canonical runtime. This
   detects unsupported production wrapper/alternate-entry differences or stale
   source/header state. Only exported symbol names change during compilation.

**Precondition:** the manifest explicitly selects `mapped-normal-exit-v1`:
ordinary mapped guest memory separate from CPU/runtime storage, stable CW between
declared calls, valid
entry TOP, no interior faults, asynchronous observers or mutating hooks, and the
harness's ordinary return continuation. Memory stores, including stack writes,
are supported with store/watch hooks disabled; partial writes and interior faults
remain excluded. A declared synchronous callee may mutate state and return a new
valid TOP/CW; subsequent incoming x87 consumption is still rejected. Entry profiling,
frame-watch and override dispatch live outside the measured `fn_ADDR` bodies.
The return dispatcher implementation is compiled, but its external decisions
are supplied by the existing harness (unexpected returns abort).

**Preservation invariant:** normal C wrappers and semantic LLVM implement the
same verified instruction CFG. Existing pass invariants preserve every final
CPU field and guest byte, including popped x87 contents. All arithmetic rounding
calls remain. Full-state snapshots remain before return dispatch; intermediate
materialization follows the effect pass's explicit contract. There is no caller
liveness assumption, numerical relaxation, or production fault-equivalence claim.

## Build policy and measurements

The exact selected chunk command comes from the production compile database.
Source/output arguments are replaced; the remaining order, definitions, include
paths, optimization and native architecture are retained. Unsupported compiler
flags, response files, cross/universal builds, non-Clang compilers or non-O2
configurations fail loudly. This is intentionally narrower than the normal build.
Missing/stale production artifacts require a normal rebuild; the comparison does
not repair or overwrite them. Do not combine this mode with `--regenerate`, other
experiments, translation overrides or nondefault target/preset/config settings.

Four variants share one replay executable and the existing inputs/checks:

| Variant | Input | Compiler/settings |
| --- | --- | --- |
| `production_c` | Exact production C body | Production compiler and recorded flags |
| `llvm22_c` | Same C body | LLVM22 Clang, same flags |
| `llvm_raw` | Semantic LLVM without lifting | LLVM22 helpers/O2/backend |
| `llvm_lifted` | Semantic LLVM with existing passes | Same LLVM22 pipeline |

Production FP flags are **not changed** to the earlier experiment's
`-ffp-contract=off`. Semantic LLVM has no fast-math/contraction flags, and every
rounding point remains explicit. Inspect disassembly and full-state replay before
claiming the policies yield the same result; a mismatch is a failure, not grounds
to mask fields. Helper bitcode uses the same production flags with a final `-O1`
for helper preparation, then always-inline/O2, as in the original experiment.
Codegen and replay use the recorded production O2 setting.

All compilation runs through the comparison CMake project, under the build
wrapper's lock. The output is `<game build>/llvm-compare/`, separate from `recomp/gen`:

- `translation.json`: contract, identities, listing/body provenance, dispatch off.
- `build-settings.json`: original compile commands, flags/compiler versions,
  compile database hash and measurement contract.
- `<address>/baseline.c`, `body.txt`, `input.ll`: translator output;
  `callees.c`, `call-dispatch.h`: shared test call boundary.
- `ssa.ll`, `effects.ll`, `lifted.ll`, `optimized.ll`: inspectable LLVM stages.
  Synchronized functions also check analysis non-mutation and pass idempotence.
- `c_native.s`, `c_llvm.s`, `llvm.s`, `disassembly.txt`: assembly and linked code.
- `codegen.json`: per-function native spans/instruction counts (including
  alignment and cold/dispatcher paths), ARM64 fused-operation counts.
- `results.txt`: complete-state replay and nine rotating million-call timing trials.
- `time-*.json`: command, elapsed wall time and exit status for individual
  compiles/passes. LLVM timings include both raw/lifted variants and helper
  definitions; C timings cover one object each. Concurrent build scheduling,
  process startup and caches affect these samples. Plugin compilation, assembly
  copies and executable linking are not in the per-leaf totals.

The comparison is evidence about translated body code generation and the current
double-backed runtime. It does not establish original-x86, fault/SEH, hook/entry
cost, or game-level behavior/performance equivalence, and enables no gameplay path.
