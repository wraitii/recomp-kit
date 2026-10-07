# Instruction IR, SSA and calling-convention analysis

`tools/recomp/ir/` is a frontend over the same original image bytes and
instruction boundaries the production translator uses. It provides lifting, a
whole-image calling-convention census, integer SSA with scalar x87 tracking, and
a C emitter for the mapped function corpus and for admitted production
functions. Discovery, entry ownership and fallback C stay with the decoded
instruction frontend. Census coverage is analysis evidence, not execution,
equivalence or a performance claim. Game measurements and the optimization plan
live in the game repository's working doc (`docs/translation-optimization.md`).

## Lifting

`lift.py` uses pinned `pypcode` SLEIGH semantics for 32-bit x86. Each operation
has an opcode, optional output and input varnodes `(space, offset, size)`;
memory, register byte lanes and arithmetic flags stay explicit. Boundaries come
from the translator's resolved functions; no alternate decoder boundaries or
guessed entry points are introduced. Folded WAIT prefixes lift in order.

The frontend folds same-operand identities such as `XOR EBX,EBX` but keeps
consumed carry dependencies such as `SBB EAX,EAX`. It normalizes x87 register
shuffles into pre-instruction `stin(i)` and post-instruction `stout(i)` slots
plus a signed depth change. Diagnostic FPU pointer and opcode writes are omitted
under the documented diagnostic-snapshot tradeoff.

Raw SLEIGH is not a faithful replacement for the runtime's x87 semantics
(FIST/FISTP, FRNDINT, FPREM, FCOM/FNCLEX status, FXAM, tags). `x87.py` corrects
these for code generation by lowering byte-backed operands to ordered effects
that call the audited runtime helpers; raw lifting and the census keep the gaps.
SLEIGH also omits arithmetic AF; the C emitter supplies AF for its supported
ADD/SUB/INC/DEC/SBB/ADC/NEG/CMP shapes. Raw lifting and census AF facts are not
code-generation proofs.

## Summaries and census

`summary.py` does forward abstract value tracking and backward byte-lane
liveness over the translator's CFG, including resolved jump tables. A summary
records register/flag inputs, preserved GPRs, stack argument extent and purge,
x87 input depth and depth change, and known exit register/stack values. Forward
values are constants, entry registers plus offsets, import-slot loads and aligned
stack bases (`(entry ESP + anchor) & -alignment` with an independent offset,
never a guessed entry-relative offset). Direct callees are analyzed before
callers; recursive components iterate to a bounded fixed point. Failed callee
summaries are not reliable facts, and unknown tail targets keep unknown purge and
x87 effects.

`imports.py` reads cleanup metadata from literal `ImportShim` arrays in the
runtime and DirectX sources; conflicts or unsupported expressions stay unknown.
`argc_stdcall` gives cleanup bytes and a conservative argument extent;
`ARGC_CDECL` gives zero cleanup, not an argument count. Unknown calls assume the
standard Win32 preserved-register set, infer cleanup from pushes and caller
cleanup, and add no register inputs. These assumptions limit what `ok` and
`standard` mean: neither proves guest state can be discarded at a call, and the
reader is a census input, not authorization for an optimized call ABI.

Run the census from a kit checkout, supplying the game repository and image:

```sh
/path/to/game/tools/.venv/bin/python tools/recomp/translate.py \
  --game /path/to/game --out /tmp/ir-gen \
  --ir-census /path/to/game/analysis/ir-census/census.json
```

The JSON has per-function summaries, overlapping reason counters and exclusive
categories (failed, standard, nonstandard with entry EBP input, other
nonstandard). Entry EBP input alone does not classify a function as an unwind
funclet. `tools/recomp/tests/test_ir_summary.py` checks the analyses on real
instruction bytes.

## SSA and C emission

`ssa.py` builds SSA over reachable integer p-code on the instruction CFG.
Registers and instruction-local unique storage use byte lanes, preserving
AL/AH/AX/EAX aliasing. Each instruction is a block; joins have phis, with a
virtual entry predecessor for backedges to the entry. LOAD, STORE and corrected
x87 effects thread one memory token; there is no memory forwarding or store
removal. Raw FLOAT operations, unbound calls, user operations, unbound indirect
transfers and intra-instruction control flow are rejected (whole-function
fallback with a named reason).

`emit_c.py` lowers integer values to unsigned, width-masked C with staged
parallel phi copies. Comparisons and arithmetic shifts use sign-bit bias and
saturating counts, avoiding signed overflow and oversized shifts. Shifts keep
masked counts, zero-count flags, AF and the runtime's deterministic OF recipe.
Dword DIV/IDIV lower to checked `div32`/`idiv32` effects that reach the existing
error seam with the original address and reload all tracked state afterwards;
narrow division is unsupported. CDQ, IMUL, SETcc, MOVSX/MOVZX, XCHG, NOT, LEAVE,
ADC and CLD/STD are admitted. Memory ADD/SUB/INC/DEC use one captured read and
emit flags after the guest STORE, matching the decoded emitter's fault and watch
snapshots. Dword `MOVSD`/`REP MOVSD` call the runtime helpers in
access-then-advance order. Other RMW shapes, locked XCHG, SSE `MOVSD` and other
string widths stay named fallbacks.

Calls and boundaries:

- A direct `CALL` requires an explicit `call_symbols` map from 32-bit target to C
  symbol and its canonical fallthrough. It publishes the pre-call CPU, calls the
  symbol, and reloads every tracked lane, flag and the memory token; the callee
  owns ESP cleanup and EIP restoration. Unbound calls fail closed.
- `indirect_call_symbol` (production passes `recomp_call`) opts in to indirect
  calls: the target must be a readable 32-bit value with a canonical fallthrough.
  Without it they fail closed. `resumable_stacks` checks EIP before continuing.
- No per-callee summary permits dropping state at a call; only the
  `ir_ssa_msvc_convention` policy below does, uniformly. The corpus binds
  independently reviewed callees in the same mode; production dispatch goes
  through stable entry thunks (`CALL_FN`).
- SLEIGH's absolute `ram` memory operands are normalized to one captured read or
  an explicit store; absolute RMW with flag writes is rejected.

State publication alone does not establish interior-fault equivalence with the
decoded emitter. `emit(..., optimize=False)` is the raw SSA emission used for pass
comparisons; `publish_changed=False` and `wide_registers=False` disable those
passes individually.

### Corrected x87 effects

`Insn.raw` keeps the original bytes so `x87.py` can recover memory width, address
and register-operand direction (FS/GS and 16-bit addressing are rejected).
`X87_MEM` and `X87_REG` are ordered effects with structured operand descriptors;
they share the memory token with guest accesses. They use the eager runtime's
`fx87` precision and NaN rules, `fdivz`, `fset`, tags, exact-FILD metadata, FIST
and FRNDINT rounding, status merging, FXAM and partial FPREM; no approximation is
added. FCOMI/FCMOV, transcendental and environment operations stay fallbacks.

### Scalar x87 and local CPU state

Scalar x87 (`x87_scalar.py`) is the optimized lowering; the raw path keeps ordered
helpers. It replaces physical push/pop/copy updates with scalar values indexed
relative to the entry TOP, tracking all eight physical residues, tags and
exact-integer shadows. CW/SW helpers use a private non-escaping environment.
CFG edges, division seams, calls and returns materialize required FPU state;
opaque recipes materialize and invalidate the tracker. Guest loads and stores
do not observe x87 state in the kit runtime (the watchpoint and dirty tracking
read only address and value), so performance mode publishes none there, as the
decoded `x87_locals` pass already does; strict mode publishes before every
access. Values survive accesses but not joins or opaque calls. Under PC=00, operations on
proven-binary32 operands use native float add/sub/mul plus the runtime's
NaN/status normalization when a linear run has at least two arithmetic effects;
division and unproven operands use the double helpers.

The `locals` state policy defers GPR/flag publication at guest loads and
stores (integer and x87), keeping EIP/ESP/EBP for diagnostics; no runtime
observer reads other CPU fields there. Division, string-helper, call and return
snapshots stay complete. The must-analysis never claims a skipped field was
published, so dead-value elimination can drop intermediate flags overwritten
before a real observer.

The `ir_ssa_msvc_convention` policy assumes MSVC-compiled callers and callees.
Arithmetic flags (CF/PF/AF/ZF/SF/OF) are dead across `CALL` and `RET`, so they
are not published before a call or at return. Entry and post-call reads still
load the CPU, and DF, division and string-helper snapshots stay complete. For
x87 the assumed invariant is that every register above TOP is tagged empty.
Every runtime push and `fset` rewrites `st`, `st_bits` and `st_exact`, and
`st_bits` is read only while `st_exact` is set. A flush therefore writes no
value, bits or exact flag for popped registers, and leaves the tag of a
register pushed and popped within the region untouched (it is already empty).
It retags empty a register popped from the region-start stack, writes live
`st_bits` only when `st_exact` may be set, and writes TOP only when it moved.
The tag word, TOP, status and live registers stay exact, so FXAM, `FLD ST(i)`,
FXCH and environment stores are unaffected. Functions with FINCSTP/FDECSTP keep
the exact flush. `emit(..., facts=dict)` reports whether a body reads a flag at
entry or after a call, or kept the exact flush. Production sums these into the
report's `ir_ssa.convention_census`, a sanity census rather than a gate.

Policies and where they apply:

| Setting | Values | Meaning |
| --- | --- | --- |
| `ir_ssa_x87` | `scalar` (default), `scalar-strict` | strict publishes x87 state before loads and stores and uses general arithmetic recipes |
| `ir_ssa_state` | `locals` (default), `strict` | strict keeps every pre-access GPR/flag snapshot |
| `ir_ssa_msvc_convention` | `true` (default), `false` | false publishes flags and complete x87 state at calls and returns |

`RECOMP_NULL_CHECKS=1` builds always compile the strict forms of all three: `emit`
emits a strict/fast `#if` pair whenever any policy is relaxed. `emit()`
defaults to the production policy (`x87_scalar_strict=False, local_state=True,
msvc_convention=True`).

DIVERGENCE(original): [ssa-x87-scalar] interior access faults and store watch
callbacks may expose the preceding published x87 state. [ssa-state-locals] likewise defers GPR/flag
state except EIP/ESP/EBP at loads and stores. [ssa-x87-binary32] uses the documented binary32
exponent-range policy. [ssa-flags-convention] and [ssa-x87-convention] leave
arithmetic flags and popped x87 residue unpublished at calls and returns.
Accesses and faults are never removed. Interior
fault/SEH equivalence remains unverified.

### Reduction passes

`simplify.py` runs `canonicalize` (trivial phis, width-aware COPY/ZEXT
propagation, constant arithmetic and shifts, BYTE-of-PACK and PACK-of-BYTE
folding) and `live_values` dead-value elimination. Potentially faulting loads,
stores, branches, returns, division effects and unknown operations remain roots.
`coalesce.py` carries whole runtime registers through entry values and phis, and
`publication.py` computes must-facts for whole CPU fields: a field is known
published at block entry only if every predecessor published it, so uncertainty
keeps the assignment. DIV32, IDIV32 and bound CALL invalidate all facts. Watch
and dirty observers are read-only and an armed null fault transfers control or
terminates; a future returning, CPU-mutating observer needs an explicit SSA
effect model before admission.

## Validation

- `tools/recomp/tests/test_ir_*.py` (portable suite): byte-backed lifting, SSA
  interpreter checks, pass idempotence, publication plans, emitter admission and
  rejection, production selection.
- `tools/build.py --ir-ssa-checks`: the byte-backed synthetic fixtures in
  `ir/native_checks.py` run against eager C as five columns (eager, raw,
  scalar with strict state, scalar-strict, and the production scalar/locals with the MSVC convention, which
  compares with that convention's dead fields cleared), each in an ordinary and
  a null-check build, comparing every CPU field, 2 KiB of scratch and read-only
  store-watch snapshots over 24576 inputs per fixture, including all x87 TOP, PC
  and RC combinations. A mocked divide-error handler mutates other GPRs/flags and
  the checks require every tracked field to be reloaded. The null-check build runs
  its conservative path on mapped inputs; it does not inject null faults or
  validate guest SEH.
- The function corpus (`tools/recomp/corpus/README.md`) compares each variant with
  eager C on real game bytes:

  ```sh
  /path/to/game/tools/.venv/bin/python tools/build.py --game-dir /path/to/game \
    --function-corpus MANIFEST --corpus-ir-ssa --corpus-trial-ms 0
  ```

  `--corpus-ir-ssa` replaces the combined variant only when the whole function is
  supported; otherwise the decoded emitter supplies it, and JSON records an
  `ir_ssa` emitted/fallback result per function. The eager variant is the
  full-state oracle. Original-x86 and interior-fault differential checks are not
  part of any of these.

## Production selection

```toml
[translate]
ir_ssa = true
ir_ssa_x87 = "scalar"    # scalar (default) or scalar-strict
ir_ssa_state = "locals"  # locals (default) or strict
ir_ssa_msvc_convention = true  # false: conservative call/return publication
```

Regenerate with `tools/build.py --regenerate`; `ir_ssa = false` restores decoded
emission. Discovery, entry ownership and decoded dispatch validation run first;
`ir/production.py` then replaces supported final bodies while keeping stable entry
thunks and the raw/base/hooked tables. SSA and decoded callees can mix, including
replacement and hook selection.

Alternate-entry bodies, SEH frames/helpers/restores, pushed continuations,
nonreturning control flow, audited instruction/operand/visual-clock rewrites,
unsupported division shapes and auxiliary modules keep decoded C. Unbound
indirect calls, jump tables, external tail transfers and unsupported instructions
fall back with named SSA/lift diagnostics, and a 2048-instruction budget and graph
recursion limit keep decoded C for expensive bodies. The translation report's
`ir_ssa` object lists emitted/fallback counts, policy names and aggregated
fallback reasons per final body; these are frontend coverage metrics, not
execution coverage or equivalence evidence.

## SSA ceiling experiment (corpus-only, unproven)

`ir/ceiling.py` defines four aggressive relaxations that the function corpus can
apply as one extra "SSA ceiling" column to measure what they could buy before any
is proven. They are not part of the agreed performance-mode contract and never
reach `game.toml` or `ir/production.py`: the only entry is the private `_ceiling`
argument of `emit_c.emit`, a regression test checks production never passes it,
and every other variant is byte-identical when the option is absent. Select them
with `--corpus-ir-ssa --corpus-ir-ssa-ceiling A,C,D,E|all` (the game wrapper
spells it `--ir-ssa-ceiling`).

| Letter | Relaxation |
| --- | --- |
| A | No access snapshots at all, dropping EIP/ESP/EBP too (production `locals` already defers every other field at loads and stores) |
| C | x87 values only: no tags or exact shadows, at edges as well as calls and returns |
| D | No sticky exception bits; NaN canonicalised only at stores |
| E | Constant PC=00/RC=nearest; slots are plain C `float` |

C, D and E lower only the x87 forms the corpus contains; any other form raises a
named `SSAError`, and the runner keeps the ordinary scalar/locals body for that
function and records the reason. The ceiling column is judged on declared
observations, not full-state equality: native rows use the fixture's observable
contract; translation-only rows compare declared guest ranges, EAX, ST0 and
relaxed boundary snapshots. Mismatches are counted, never fatal. With D, sticky IE
is absent from a status word a guest reads through `FNSTSW AX`. The former B (flags
dead across calls and returns) is now production `ir_ssa_msvc_convention`. The
column is built on top of the production policy.
