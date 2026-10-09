# Instruction IR, analysis and observable contracts

`tools/recomp/ir/` is a frontend over the same original image bytes and
instruction boundaries the production translator uses. It provides lifting, a
whole-image calling-convention census, observable-contract analysis, integer SSA
with scalar x87 tracking, and a C emitter for the mapped function corpus and
admitted production functions. Discovery, entry ownership and fallback C stay with the decoded
instruction frontend. Census coverage is analysis evidence, not execution,
equivalence or a performance claim. Game measurements and the optimization plan
live in the game repository's working doc (`docs/translation-optimization.md`).

## Lifting

`lift.py` uses pinned `pypcode` SLEIGH semantics for 32-bit x86. Each operation
has an opcode, optional output and input varnodes `(space, offset, size)`;
memory, register byte lanes and arithmetic flags stay explicit. Boundaries come
from the translator's resolved functions; no alternate decoder boundaries or
guessed entry points are introduced. Folded WAIT prefixes lift in order.

`cfg.py` owns `FunctionIR`, byte-backed lifting through the production resolver,
the fixture-only default successors and iterative call-graph SCC traversal.
Calling-convention inference, observable-contract analysis and emission share
this representation. Compatibility exports remain in `summary.py`/`census.py`;
new consumers import the CFG directly. Analyses must not discover alternate
instruction boundaries or derive evidence from an already optimized emission.

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
funclet.

## Observable region contracts

Region analysis bounds observability around connected code, using the shared
CFG and original semantics. The analysis foundation below inventories effects
and demands; private region emission remains future work.

An **observable contract** describes what an outside continuation can distinguish
about an execution: its boundary events, state visible at those events, possible
responses, and exit behavior. It belongs to a region **and its environment**,
not just to a function signature. The same function can have a narrow private
contract inside a reviewed caller and a complete guest-state contract at its
public entry. Changing the observer configuration changes the contract.

For admitted inputs and the same external responses, the original and optimized
region must produce matching projected event traces and matching state at each
exit. Internal register assignments, physical x87 stack manipulations and
private spills are absent from that projection only when no admitted observer
can distinguish them. Dead representation is removable; arithmetic and control
that influence an observation remain consequential. Timing imports retain their
positions and dependencies; matching recorded responses is a validation method,
not permission to replace live clocks or change scheduling policy.

### Contract contents

A complete region contract needs the following facts. The current inventory
implements machine masks, may-effects and observer boundaries; admitted domains,
alias/escape proofs and reconstruction plans remain future work. Every narrowed field needs instruction/runtime evidence; unknown means
conservative, not empty.

| Field | Required information |
| --- | --- |
| Identity and scope | Original byte identity, entries, included CFG/callees, all normal/abnormal exits, and observation policy. External entry at an internal address keeps its public thunk. |
| Admitted domain | Pointer validity and lifetimes, possible overlap, x87 environment/depth, target sets, hook/replacement configuration, threading and fault assumptions. Guarded facts must be checked before any irreversible effect. |
| Entry dependencies | Register byte lanes, individual flags, logical x87 values/environment and guest memory read before definition, including address/control dependencies and transitive callee reads. |
| Memory effects | Symbolic guest ranges or reachable objects, reads, may/must writes, escaping addresses, alias relations and unchanged memory. A may-write set bounds damage; it does not permit arbitrary contents inside it. |
| Boundary events | Site and target, arguments/state and memory the observer can read, writes/clobbers it can perform, callback/reentry/yield behavior, and required ordering. Imports and unknown calls start as complete guest-state boundaries. |
| Exit obligations | Per-exit live machine state, memory/results, stack cleanup, continuation identity, exceptions, termination and divergence. Preservation of an input can be an obligation even if the region never computes with it. |
| Reconstruction | A mapping from private values to required guest state at every actual side exit, including intermediate fault/SEH state where supported. Resuming an original continuation must not duplicate completed effects. |
| Evidence and status | Byte-backed analysis, runtime observer review, unresolved facts, guards, differential cases and covered paths. Distinguish conservative, candidate, guarded and validated facts; testing alone is not a universal proof. |

Boundary contracts are bidirectional. Before an external call, commit memory and
state it can read. After it returns, invalidate/reload everything it may change,
including memory reachable through aliases or callbacks. Unknown readers/writers
may touch arbitrary mapped guest memory; a small fixture snapshot is not a bound
on their production footprint. If it may not return, model that exit. A read-only
observer still makes values observable; its required state must be published.

A private representation may use native locals for proven non-escaping spills,
or resolve guest addresses to borrowed host pointers for a bounded lifetime.
Guest-visible pointers and object layouts remain 32-bit. Copying guest objects
into native aggregates additionally requires alias, escape, lifetime and
intermediate-observer evidence, plus correct writeback/reconstruction; it is not
implied by a narrow CPU mask. Such representations are future consumers of the
contract, not part of the inventory tool.

At an unknown guest continuation, keep the production guest state, with only
already documented divergences. A narrower exit mask requires analysis of the
actual continuation's reads before overwrite, including later calls and status
observers. A conventional EAX/ST0 return signature is insufficient. Avoid
circular proofs: establish dependency facts from original semantics, not from an
emission that already discarded the state being classified. Separate inputs
needed for computation from untouched inputs that must survive for a later
observer: both constrain a private implementation, but only the former need
participate in its arithmetic.

### What creates an observation point

- **Outside code:** fallback bodies, unresolved direct/indirect calls, imports,
  callbacks, hooks and native replacements. A reviewed call included in the
  region is an internal edge; a call is not inherently an observation point.
- **Guest-visible memory:** commit before a reader can execute. Until escape and
  alias proofs exist, retain guest accesses and their order. Private stack slots
  can become locals only if no callback, unwind path or escaping pointer exposes
  them. Allocation/address identity and shared-object changes are also effects.
- **Machine-state observers:** flag consumers, x87 status/control/environment
  instructions, SEH contexts, and resumable returns. These can be represented
  as ordinary SSA data inside a region; at outside boundaries they need guest
  state or an explicit private interface.
- **Scheduling and services:** imports are scheduler checkpoints in the current
  runtime, even when the individual shim appears read-only. Other guest threads
  can observe memory after a yield. Preserve checkpoints and reload shared state
  after external execution; one execution baton is not whole-region immutability.
- **Faults and exceptional exits:** an access or divide can terminate or expose
  intermediate effects/state to a handler. Normal-return equivalence is weaker
  than this contract. First production regions need supported reconstruction or
  conservative seams; mapped-input corpus tests do not establish fault fidelity.
- **Instrumentation:** active entry hooks observe CPU/stack and can change control.
  Store-watch/dirty observers see accesses. A private path must preserve these
  events or use the conservative path under a proven configuration guard.
  Decide explicitly which profiling/diagnostic events are retained; do not
  silently turn a source-level instrumentation boundary into an internal edge.

Retain original order for external calls, RNG steps, guest writes and potentially
faulting accesses initially. Later transformations may relax order only with
specific independence and fault/observer evidence. Equality of final memory
alone misses a write followed by an external read and then an overwrite.

### Representation versus obligations

Two kinds of relaxation are easy to conflate. A *representation* change keeps
state in host locals between observation points but still computes everything
the eager emitter would publish, and publishes it at the same points. Scalar
x87 tracking, deferred GPR/flag publication at accesses and carried x87 state
across internal CFG edges (`x87_carry.py`) are of this kind. They need no
contract facts, only the statement that the skipped points have no observer,
and full-state eager comparison checks them.

An *obligation* change omits computing state because no admitted observer at a
region exit or boundary reads it. Examples are sticky status bits, flags or x87
residue at an internal callee's return, a private stack slot's guest store, or
values passed through a private call interface. That requires the contract
analysis described here and the region comparison column. A larger
representation region does not imply narrower obligations at its exits:
carried x87 state is still exact at every call and return.

### Composition

Compute conservative effects from original lifted instructions and reviewed
runtime observers. Propagate known callee contracts bottom-up, and propagate
which produced state callers/continuations can observe backward from the
region's external edges. Iterate recursive components conservatively. Unknown
targets and incomplete summaries retain complete boundaries. Enlarging a region
removes an internal publication seam, not the underlying value dependency.

### Observable-contract foundation

`ir/contracts/` is an analysis-only layer. It does not change production
publication, dispatch, hooks, x87 policy or the corpus comparison contract.

| Module | Responsibility |
| --- | --- |
| `model.py` | Immutable CPU/memory masks, may-effects, bidirectional observer contracts, instruction nodes and function facts. Unknown is universal, distinct from empty. Masks can express all state except definitely overwritten lanes. |
| `dataflow.py` | Backward CPU demands over CFG joins/loops, reachable may-effects and transitive call effects. Recursive components converge by monotone union; missing callees contribute universal effects. Cycles conservatively may not return. |
| `lifted.py` | Original lifted instructions to conservative facts. Calls stay outside boundaries even when their callees are known. Loads/stores retain fault observers; raw x87/opaque effects contribute universal state/memory effects. |
| `report.py` | Deterministic inventory and per-node demands with explicit limitations. Every report sets `optimization_authorized: false`. |

An observer records both reads before outside execution and possible response
clobbers/events. A possible write is not a definite overwrite and cannot kill
backward demand. The default observer can read/write all CPU and memory, yield,
callback, fault or fail to return; no Win32 convention is imported as evidence.
Node definitions concern normal continuation only. Fault reconstruction and
memory liveness need separate analyses before private regions can be emitted.

Raw SLEIGH lacks complete arithmetic flags and x87 semantics. Arithmetic flag
writes get unknown CPU-write effects and no definite definitions. x87 and other
unmodeled operations retain universal effects. Integer register reads are a
syntactic inventory, not precise semantic inputs. Public exits remain complete.
Effect sets report possibilities; the original CFG retains sequencing, and an
effect union never authorizes reordering or removal of events.

Inventory a game's byte-pinned corpus without compiling or timing:

```sh
/path/to/game/tools/.venv/bin/python tools/recomp/analyze_contracts.py \
  --game-dir /path/to/game --manifest /path/to/game/benchmarks/functions/corpus.json \
  --out /path/to/game/build/function-contracts/report.json
```

The tool verifies executable, map and instruction hashes, rejects configured
instruction rewrites, and lifts original bytes with production CFG successors.
Reports stay in the game's ignored build tree. Fixture stubs, native comparison
masks and legacy convention summaries never narrow production boundaries.
`test_ir_contracts.py` covers the mask lattice, byte lanes, joins/loops,
alternative entries, observer clobbers, unresolved/recursive/deep call graphs,
byte-backed effect inventories and input-provenance failures.

Extend the corrected instruction-effect adapter and memory/escape analysis next;
then introduce explicit observer policies and a validated reconstruction plan.
Keep diagnostic inventories separate from any future codegen admission type.
Neither a successful report nor a narrowed test mask establishes a safe private
ABI. Full-state production/corpus checks remain the comparison baseline.

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
- No per-callee summary permits dropping register or flag state at a call.
  `msvc_x87_convention` only elides popped x87 residue. The corpus binds
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
exact-integer shadows. CW and SW live in two scalar locals: helpers take CW by
value and SW through always-inlined `_sw` forms (`fx87_sw`, `fdivz_sw`,
`fcom_sw`, `fto_float_cw`, ...), so neither escapes and clang keeps both in
registers rather than reloading stack slots around every operation.
Division seams, calls, opaque recipes and returns materialize required FPU
state; opaque recipes then invalidate the tracker. In performance mode
unpublished state also survives internal CFG edges: `x87_carry.py` computes a
fixed-point join shape per block, each predecessor normalizes the runtime TOP
and writes function-scope canonical slot variables, and the successor copies
them into fresh locals. Only the parts that still need publishing are carried;
under the MSVC convention a popped register contributes just its empty tag.
The shape is conservative at joins (union of parts and dirty flags, `narrow`
intersected, `base`/`low` minimised and `high` maximised, with an inactive
predecessor counted as published at its TOP); TOP is republished when the
predecessors' published TOPs disagree, and a window of eight or more slots
falls back to the per-edge flush. Emission checks every carried edge against
the planned shape and raises `SSAError` (whole-function fallback) on drift.
Carry requires the MSVC convention: exact-flush bodies and strict mode keep the
per-edge flush. Guest loads and stores do not
observe x87 state in the kit runtime (the watchpoint and dirty tracking read
only address and value), so performance mode publishes none there, as the
decoded `x87_locals` pass already does; strict mode publishes before every
access. Under PC=00, operations on
proven-binary32 operands use native float add/sub/mul plus the runtime's
NaN/status normalization when a linear run has at least two arithmetic effects;
division and unproven operands use the double helpers.

A body with at least four PC/RC-sensitive x87 operations (arithmetic, FSQRT,
FRNDINT, FST m), one per eight instructions, and a single tracker window
(one guarded activation: no calls or opaque x87 recipes resetting it) is
emitted twice in one C
function under `[translate] x87_cw_clone` (default on). The fast clone runs
first; each tracker activation - the only point a tracker window reads the
guest CW, since FLDCW and calls reset the tracker - tests
`(cw & 0xf00) == 0` (PC = 24-bit, RC = nearest: the D3D8 control word
`0x007f`) and otherwise jumps to the same activation in the general clone.
Both clones replay identical tracker transitions, share the integer SSA and
carry locals, and at an activation all x87 state is published, so the jump
needs no state transfer. Past the guard the fast clone folds every CW use to
the constant 0 (helpers read only PC/RC), removing the precision selects and
rounding tests. It is exact and carries no divergence tag; emission raises
`SSAError` if the clones' activation counts differ. Corpus effect at CW
`0x007f`: single-window matrix/quaternion rows 5-27% faster for ~20-50%
more code. Multi-window bodies are not cloned: the clones share every C
local, so values live at each guard stay live into the general clone, and
RayTestTriangles (13 guards) spilled more and ran ~3% slower.

Lazy NaN checks are a representation change on top of that removal: a
basic-arithmetic result stays in full precision with no NaN branch, and the
`isnan`/indefinite fold happens where the value stops flowing into more
NaN-propagating arithmetic. Folding is per `Slot` and block-local; every
non-linear internal edge folds and canonicalises before the carried value is
snapshotted (so `CarryShape` is unchanged), and `flush()` folds before any
`c->st`/`c->fpu_sw` publication. `FCHS`/`FABS`, the comparisons, `FST`/`FSTP`,
`FNSTSW`, `FSTCW`/`FLDCW` and the opaque fallbacks are sinks; `FCLEX`
canonicalises without raising IE, then clears the status as eager does; pure
moves (`FXCH`, `FLD`/`FST ST(i)`) carry the pending flag. Loaded NaN payloads
are never marked pending, so they pass through untouched. Host `+ - * /` and
`sqrt` propagate NaN, and IE is sticky, so the sole observable difference from
an eager per-op check is when the fold is computed, never its result. The
relaxation is disabled for strict x87, the exact flush and
`optimize=False`, where emission is byte-identical to the eager helpers.

Lazy arithmetic flags are a second representation change on the same seams.
`X86` carries a descriptor (`cc_op`, `cc_size`, `cc_mask`, `cc_a`, `cc_b`,
`cc_res`) for the last recognised ADD/SUB/CMP/logic/INC/DEC instead of
materialising the six flag fields. `cc_op` is `X86_CC_NONE` when the fields are
current; otherwise `cc_op` names the operation, `cc_size` is 1/2/4, `cc_mask`
names the fields the operation defines (logic preserves AF, INC/DEC preserve
CF), and `cc_a`/`cc_b`/`cc_res` are the masked operands and the wrapped result.
`x86_cc_settle` writes the fields and clears the descriptor;
`x86_get_eflags`, `x86_set_eflags`, `x86_sahf` and `recomp_comis` settle or drop
it first, and the interpreter and `recomp_call` settle at entry and after every
call. An SSA body publishes a descriptor at a call or return seam only while
the seam's required flag values still resolve to that producer's result.

Settle sites are elided per site under `[translate] call_contracts` (default
on) by a region analysis (`ir/flag_region.py`). From the function entry or the
instruction after a call, the region walks in-body control flow to the next
CALL/CALLIND/RET. If it reads no flag before writing it and writes no flag
field, the descriptor passes through untouched and the settle is removed. If it
reads no flag before writing it and writes all six flags on every path to a
region boundary, the descriptor can be cleared without materialising it, and
the settle becomes `x86_cc_drop` (a single `cc_op = NONE` store; the dead
payload is canonicalized only by the comparison harness and the poison build).
Otherwise the
settle stays. Decoded bodies use an explicit per-mnemonic flag-effect table
(helper-backed DIV/IDIV and string compares name their writes; unknown forms
fail closed); SSA bodies apply the same classification over their p-code ops.
When the `locals` policy caches a flag, its C-local is initialised after the
entry settle and reloaded after the post-call settle, so a cached value never
predates the descriptor whose fields it copies.
An SSA body reloads every tracked flag from the callee directly after a call.
Only a call whose arithmetic-flag reload is actually needed is forced to
settle: that is the call the reload belongs to, and a reload is needed when a
later observation reads it, when a `RET` publishes it, or when a following
call's publication snapshot roots it (a callee that reads or preserves
flags).  A dead reload is still emitted, but it forces no settle, so its
shadowed value is never observed.
A call whose return can continue somewhere other than its fallthrough - a
resumable-stack diversion, an SEH frame adoption, a noreturn callee or setjmp -
keeps the settle, as does any region with a path that leaves the body without
reaching CALL/CALLIND/RET (a tail jump or trap). A mod hook callback observes the full register file, so the
generated entry thunks and `recomp_jump` settle before dispatching a hook.

Like lazy NaN this is exact - the descriptor materialises to the same fields
eager emission writes - so it carries no DIVERGENCE tag of its own. The
representation only pays off when a consumer can skip the settle, which needs
call summaries proving the callee does not observe the flags; the
cross-function call contracts below provide those summaries.

### Cross-function call contracts

`call_contracts.py` summarizes each final body - decoded fallbacks included -
over the runtime CPU fields: the eight GPRs and the six arithmetic flags. Its
`reads` set is a backward may-liveness of entry values (transitively through
direct callees); its `kills` set is a forward must-definite over every path to
a return, with a GPR killed only when all four byte lanes are written. A
save/restore (`push ebx`/`pop ebx`) reads the register, so it stays in `reads`
and is never dropped. Recursive components start from empty `reads` and
`kills`: the least fixed point is sound for the may-analysis and conservative
for the must-analysis; an
unconverged component falls back to `reads=all, kills=none` rather than publish
an under-approximation.

At a direct CALL in an SSA body, `emit` may drop publication of field `F` when
`F not in callee.reads and F in callee.kills`, shrinking the lazy-flag
descriptor's mask (or omitting it) and clearing a stale descriptor when flags
are dropped. A field the callee *preserves* is never dropped even when this
body does not read it back, because its CPU value can still flow out to this
body's caller. ESP and EBP are never dropped, since the stable entry thunk and
frame diagnostics read them. Indirect calls, CALLOTHER, unbound targets, failed
lifts, SEH and alternate-entry bodies, audited instruction rewrites and
configured native replacements are `reads=all, kills=none`. A mod hook can be
installed on any function at runtime and its callback sees the full register
file, so every contract call site still publishes its dropped fields behind
`recomp_hooks_ever`, a flag the installer sets once and never clears. Call contracts are
disabled under `resumable_stacks`, where a callee may divert EIP to a
continuation the call's contract does not cover. The `[translate]`
`call_contracts` key (default on) disables the optimization and restores full
publication. `RECOMP_CONTRACT_POISON=1` overwrites every dropped killed field
with distinctive garbage before the call (after materialising a pending
lazy-flags descriptor, which still carries the flags the call publishes), so a
wrong summary fails the full-state comparison loudly; it is a validation build
only.

DIVERGENCE(original): [ssa-call-contracts] a fault or SEH context raised inside
the callee before it overwrites a dropped field shows the caller's stale value
rather than the published one, as with [ssa-state-locals]. The same applies at
an elided settle site: a fault or SEH context raised between a `x86_cc_drop`
and the writes that kill all six flags, or between a removed settle and a later
flag read, observes the stale `eflags_*` fields rather than the pending
descriptor's values. Every ordinary flag read is preceded by a settle on its
path, so guest reads see the descriptor's values. Runtime hook installation from a
host thread is applied at a scheduler checkpoint; a hook that lands between a
call site's `recomp_hooks_ever` test and its dispatch sees the dropped fields
unpublished for that one call. The translation report records the
summarized-body, call-site, skipped-field and per-site elision counts. The
decoded `decoded_settles` and the SSA `ir_ssa.ssa_settles` counters break the
entry and post-call decisions down by `remove`/`drop`/`settle`.

The `locals` state policy defers GPR/flag publication at guest loads and
stores (integer and x87), keeping EIP/ESP/EBP for diagnostics; no runtime
observer reads other CPU fields there. Division, string-helper, call and return
snapshots stay complete. The must-analysis never claims a skipped field was
published, so dead-value elimination can drop intermediate flags overwritten
before a real observer.

The `msvc_x87_convention` policy assumes the MSVC x87 stack invariant:
every register above TOP is tagged empty. Arithmetic flags stay published at
calls and returns. CRT assembly helpers can return flags or consume entry flags;
a compiler ABI is insufficient evidence that those values are dead. Dropping
boundary flags requires actual call summaries, including decoded fallback bodies.
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

| `[translate]` key | Values | `emit` parameters |
| --- | --- | --- |
| `fault_state` | `"relaxed"` (default), `"exact"` | relaxed: `x87_scalar_strict=False, local_state=True`; exact: strict x87 (publish before loads and stores, general arithmetic recipes) and every pre-access GPR/flag snapshot. The decoded fallback's `x87_locals`/`cpu_locals` follow the same key |
| `msvc_x87_convention` | `true` (default), `false` | `msvc_convention`; false publishes complete x87 state at calls and returns. Flags stay exact in both modes |
| `x87_cw_clone` | `true` (default), `false` | `x87_cw_clone`; false emits only the general body (no PC = RC = 0 fast clone) |

Lazy NaN/IE checks (`lazy_nan=True`) are exact and always on in production; the
`emit` parameter remains so the synthetic checks can compare against the eager
`fx87`/`fx87_exact` emission. A `RECOMP_NULL_CHECKS=1` build exposes CPU state
to fault dispatch, so translate it with `fault_state = "exact"` and
`msvc_x87_convention = false`. `emit()` defaults to the production policy except `lazy_nan`, which is
opt-in at that layer.

DIVERGENCE(original): [ssa-x87-scalar] interior access faults and store watch
callbacks may expose the preceding published x87 state, and internal CFG edges
carry the scalar representation instead of publishing it. [ssa-state-locals] likewise defers GPR/flag
state except EIP/ESP/EBP at loads and stores. [ssa-x87-binary32] uses the documented binary32
exponent-range policy. [ssa-x87-convention] leaves popped x87 residue unpublished at calls and returns.
The former [ssa-flags-convention] assumption was rejected after a CRT flag ABI
caused an in-game regression.
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

### Validation obligations

Keep the current full-state column as an oracle. Add a separate region column
that compares at actual external cuts: ordered boundary events, declared
CPU/memory observations, injected external responses and all admitted exit
obligations. Internal calls should remain available as diagnostic checkpoints,
without requiring an artificial physical register file there. Test aliasing,
NaNs/signed zero/subnormals, PC/RC/TOP, multiple and early exits, boundary clobbers,
hooks, reentry and failures according to the claimed domain. Vary state declared
irrelevant while holding dependencies fixed to detect missed dependencies; this
is additional evidence, not a replacement for conservative analysis.

### Existing checks

- `tools/recomp/diff/run.py` compares translated game functions with the
  original bytes under Unicorn.
- The function corpus (`tools/recomp/corpus/README.md`) compares each variant with
  eager C on real game bytes:

  ```sh
  /path/to/game/tools/.venv/bin/python tools/build.py --game-dir /path/to/game \
    --function-corpus MANIFEST --corpus-ir-ssa --corpus-trial-ms 0
  ```

  `--corpus-ir-ssa` replaces the combined variant only when the whole function is
  supported; otherwise the decoded emitter supplies it, and JSON records an
  `ir_ssa` emitted/fallback result per function. The eager variant is the
  full-state oracle.

## Production selection

```toml
[translate]
ir_ssa = true                # false: decoded C only
fault_state = "relaxed"      # "exact": publish state at every instruction
msvc_x87_convention = true   # false: conservative call/return publication
x87_cw_clone = true          # false: no fast PC=RC=0 x87 clone
```

These are the defaults. Earlier keys (`x87_locals`, `cpu_locals`, `ir_ssa_x87`,
`ir_ssa_state`, `ir_ssa_msvc_convention`, `ir_ssa_x87_lazy_nan`) are rejected
with the name of their replacement.

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

