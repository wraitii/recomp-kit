# Instruction IR and calling-convention analysis

`tools/recomp/ir/` is an experimental frontend over the same original image
bytes and instruction boundaries used by the production translator. It currently
provides lifting, a whole-image calling-convention census, integer SSA and an
opt-in C emitter for the mapped corpus and admitted production functions.
Production discovery and fallback C, and experimental LLVM emission, use the
existing decoded-instruction frontend. Census coverage is analysis evidence,
not execution, equivalence or a measured performance gain.

## Lifting

`lift.py` uses pinned `pypcode` SLEIGH semantics for 32-bit x86. Each operation
has an opcode, optional output and input varnodes `(space, offset, size)`.
Memory, register byte lanes and arithmetic flags remain explicit. Instruction
boundaries come from the translator's resolved functions; no alternate decoder
boundaries or guessed game entry points are introduced. Folded WAIT prefixes
are lifted in instruction order.

The frontend folds same-operand identities such as `XOR EBX,EBX`, while keeping
consumed carry dependencies such as `SBB EAX,EAX`. It normalizes the physical
x87 register shuffles into pre-instruction `stin(i)` and post-instruction
`stout(i)` slots plus a signed stack-depth change. Diagnostic FPU pointer and
opcode writes are omitted under the documented diagnostic-snapshot tradeoff.
These omissions must remain explicit if a future consumer observes that state.

Raw SLEIGH is not yet a faithful replacement for the kit's x87 runtime semantics.
FIST/FISTP and FRNDINT rounding, FPREM quotient truncation, FCOM/FNCLEX status
merging, FXAM classification and tags require correction before code generation
uses them directly. `x87.py` now corrects these shapes for code generation by
lowering byte-backed operands to ordered effects using the audited runtime
helpers; raw lifting/census still retain the gaps. Slot-depth normalization
alone does not establish floating-point
value or exception equivalence.

SLEIGH also omits arithmetic AF definitions. The experimental C consumer supplies
AF for its supported ADD/SUB/INC/DEC/SBB/ADC, NEG and CMP shapes, including folded
same-operand arithmetic, capturing operands before a destination changes. INC
and DEC retain CF. Additional integer corrections are described below.
Raw lifting and census summaries still have this gap;
their AF preservation facts are not code-generation proofs.

## Summaries

`summary.py` performs forward abstract value tracking and backward strong
liveness over the translator's CFG, including its resolved jump tables.
Summaries record register/flag inputs, preserved GPRs, stack argument extent,
stack purge, x87 input depth and depth change, and known exit register/stack
values. Liveness distinguishes register byte lanes internally; exported register
inputs currently aggregate those lanes into register names.

Forward values describe constants, entry registers plus offsets, absolute
memory loads used to identify imports, and aligned stack bases. An aligned
base is `(entry ESP + anchor) & -alignment`, with an independent offset. It
never becomes a guessed entry-relative offset. Push/pop traffic below that
base remains traceable, including a saved frame pointer that contains the
pre-alignment ESP. `MOV ESP,EBP` or LEAVE restores the original coordinate
system. Stores invalidate tracked slots on another base whenever their possible
byte ranges overlap. A return that still depends on an aligned ESP fails exact
purge analysis.

Direct callees are analyzed before callers. Recursive strongly connected
components iterate to a bounded fixed point. Failed callee summaries are not
used as reliable facts; agreeing syntactic RET operands can still supply their
cleanup. Unknown tail targets retain unknown purge and x87 effects rather than
inventing zero effects. Failed known functions and missing function targets have
separate diagnostics. RET's implicit return-address fetch is excluded from the
explicit return-address-read diagnostic; an ordinary load or pop of that address
still counts.

Stack-slot liveness prevents saved registers and `push ecx` local reservations
from automatically becoming inputs. It tracks known exit values from helpers
that construct their caller's frame as well as ordinary frame-pointer prologues.

## Import metadata and assumptions

`imports.py` reads cleanup metadata from literal `ImportShim` arrays in the kit's
runtime and DirectX sources, including their supported single-DLL macros. Keys
include both the lowercase DLL and exact export name. Conflicts or unsupported
expressions stay unknown. The translator retains the DLL for each named IAT
slot and rebases its address alongside the import name.

A numeric `argc_stdcall` specifies both cleanup bytes and a conservative stack
argument extent. `ARGC_CDECL` specifies zero cleanup, not an argument count;
argument pushes remain call-site evidence. This reader does not recover return
types, x87 results, register inputs, callbacks, or runtime registration overrides.
It is a census input, not sufficient authorization for an optimized call ABI.

Unknown calls assume the standard Win32 preserved-register set. Cleanup without
metadata is inferred from pushes and immediate caller cleanup. A bounded
straight-line lookahead also recognizes trailing import arguments pushed before
a getter call: when the next literal IAT call consumes exactly the earlier and
later push group, the getter is inferred to pop zero. Branches, other stack
updates and unrelated calls stop this inference. An unresolved
float return is inferred when the next x87 instruction consumes it. Unknown
calls do not add register inputs, so a future emitter must still pass current
register values at opaque boundaries. Saved-register slots are assumed immune
to opaque callee/pointer stores; escaped local stack storage is conservatively
live. These assumptions and unknown effects limit the meaning of `ok` and
`standard`: neither is a proof that all guest state can be discarded at a call.

## Census and validation

Run from a kit checkout, supplying the game repository and private image:

```sh
/path/to/game/tools/.venv/bin/python tools/recomp/translate.py \
  --game /path/to/game --out /tmp/ir-gen \
  --ir-census /path/to/game/analysis/ir-census/census.json
```

The JSON includes per-function summaries, overlapping reason counters, and
mutually exclusive categories: failed, standard, nonstandard with entry EBP
input, and other nonstandard. Entry EBP inputs include unwind funclets that use
a parent's frame. That input trait alone does not establish unwinder-only
reachability; the kit does not classify them by game-specific address ranges.
The report also records how many named IAT slots have known cleanup metadata.

Synthetic tests in `tools/recomp/tests/test_ir_summary.py` exercise actual
instruction bytes through SLEIGH, including aligned frames, partial aliasing,
frame-building helpers, recursion, imports and tail fallback. They run in the
portable test suite. The driver relocation tests check both IAT names and DLLs.
Whole-image census runs test analysis coverage, without executing the game.

## SSA and C emission

`ssa.py` builds SSA over reachable integer p-code using the supplied instruction
CFG. Registers and instruction-local unique storage use byte lanes, preserving
AL/AH/AX/EAX aliases and overlapping unique reads/writes. Each instruction starts
as a block; joins have phi values, including a virtual entry predecessor for
backedges to the function entry. An optional register-group pass carries complete
registers through entry values and phis; byte lanes remain the aliasing interface
for partial writes and snapshots. Trivial phis are simplified. LOAD and STORE
thread an explicit memory token; no inter-instruction forwarding or store removal
occurs. Corrected x87 effects thread that same token. FPU state remains resident
in the CPU object at observations; optional straight-line caches are described
below. Raw FLOAT operations, unbound calls, user operations, indirect transfers and
intra-instruction control flow remain rejected. Bound direct calls publish
tracked state, run the declared callee, and reload all tracked lanes and flags;
no calling-convention summary permits dropping state.

`emit_c.py` lowers integer values to unsigned, width-masked C, with staged
parallel phi copies on edges. Comparisons use sign-bit bias and p-code shifts
guard counts outside the value width, avoiding signed overflow and oversized C
shifts. Register SHL/SHR retain masked counts, zero-count flags and AF, and use
the existing runtime's deterministic OF recipe for nonzero counts (OF is
architecturally undefined for counts greater than one). Unsigned dword DIV
lowers to an explicit `DIV32` effect using checked `div32`, with CPU publication
and packed quotient/remainder results. Division by zero and quotient overflow
reach the existing error seam with the original instruction address, rather
than unchecked C division. Signed dword IDIV uses the same ordered model with checked `idiv32`; narrow
divisions remain unsupported. CDQ, IMUL and all SETcc conditions are admitted.
Register NEG restores AF; register SAR preserves zero-count flags and uses the
existing runtime OF recipe for nonzero counts. P-code arithmetic shifts use
unsigned sign-bit bias with explicit saturation, without C signed overflow or
x86 count remasking. Memory NEG/SAR remain named fallbacks.
MOVSX/MOVZX, register XCHG, NOT, LEAVE, register-destination ADC and ordinary
memory-source arithmetic are also admitted. Sign extension uses unsigned
sign-bit bias and width masks, without signed-overflow assumptions. Memory
ADD/SUB/INC/DEC use an instruction-local correction: SLEIGH's repeated LOADs
of the same RMW operand become copies of one captured read/result, and flag
assignments occur after the guest STORE, matching the existing emitter's fault
and watch snapshots. This is not general memory forwarding. Other RMW shapes
and implicit-lock memory XCHG retain named fallback diagnostics.
Guest accesses use the runtime's ordered read/write helpers and publish
known CPU fields before each access and on return. A conservative publication
analysis omits field stores only when their values are already published on
every incoming path. It also uses wider register phis, width-aware
canonicalization and dead-value elimination, without summary-driven calls.
The mapped corpus binds reviewed callees directly; the production adapter below
uses existing entry dispatch and rejects unsupported host-frame contracts.
State publication
alone does not establish interior fault equivalence with the existing emitter;
instruction-level update order still requires differential fault checks.

Run through the game build wrapper, using its Python environment:

```sh
/path/to/game/tools/.venv/bin/python tools/build.py --game-dir /path/to/game \
  --function-corpus MANIFEST --corpus-ir-ssa --corpus-calls 0
```

The flag replaces the combined variant only when the entire function is
supported. Otherwise the existing emitter supplies that whole function. JSON
records an `ir_ssa` emitted/fallback result and reason per function; Markdown
reports the emitted count. This mode runs separately from decoded-dataflow
experiments. The eager variant remains the full CPU/scratch-state oracle.

`test_ir_ssa.py` exercises actual instruction bytes, partial registers, wrapping
arithmetic/AF, loop backedges to entry, memory alias order, overlapping uniques,
instruction-local temporary lifetimes and conservative rejection. Its small
SSA interpreter checks independently expected results. Native corpus checks
exercise the admitted real functions. `tools/build.py --ir-ssa-checks` builds
byte-backed synthetic fixtures against eager C, comparing every CPU field
and 2 KiB of scratch for 24576 inputs per fixture. They cover register aliases,
loops, memory aliases, INC/DEC and carry-dependent subtraction, variable and
immediate shifts, and unsigned division. A mocked error handler records the
complete CPU and fault address, then returns with changed EAX/EDX; zero-divisor
and overflow cases check both the snapshot and continuation. A read-only native
watch callback compares complete CPU snapshots, addresses, widths and values at
every store, including division-handler stores. Branch joins, loop publication
and partial-word updates have explicit fixtures. Eighteen x87 fixtures exercise
all TOP/PC/RC combinations, randomized status and exact-integer metadata, special
and finite memory inputs, 32/64/80-bit floating memory, integer conversions,
register directions, remainder, comparison, classification and control words.
Float stores retain their actual `wrf*` accessors; the watch callback observes
integer stores rather than inventing watch calls for float stores. This does not
validate real guest SEH or handlers that mutate other state. Original-x86 and
interior memory-fault differential checks remain to be added.

## Corrected x87 effects

`Insn.raw` retains private original bytes for operand validation. `x87.py` decodes
only those bytes, at the supplied boundary, to recover memory width, address and
register operand direction. A folded WAIT prefix is accepted in instruction
order. Memory address expressions become width-masked integer p-code. FS/GS and
16-bit address forms are rejected; they need an explicit segment model.

`X87_MEM` and `X87_REG` are first-class ordered effects, with validated structured
operand descriptors rather than C snippets. Their shared token keeps x87 effects
and guest accesses in instruction order. CPU publication occurs before an x87
memory effect; FPU state is already resident in `c`. Register operations call the
runtime helpers without publishing unrelated integer fields. FNSTSW AX returns
an integer SSA value and updates AX through the existing partial-register model.

This path uses the same `fx87` precision/NaN rules, `fdivz` exceptions, `fset`,
push/pop/copy tags, exact FILD metadata, FIST/FRNDINT rounding, FCOM/FNCLEX status
merging, FXAM classification and partial FPREM behavior as eager C. It inherits
the documented binary64 x87 representation; it does not add an approximation.
It does not emit raw SLEIGH FLOAT arithmetic or silently discard popped residue.
Unsupported transcendental/environment operations, FCOMI/FCMOV, locked integer
operations and unbound calls retain whole-function fallback. General floating
SSA across CFG joins remains future work; the bounded value and publication
passes below keep the same full-state observation contract.

## Direct calls and absolute memory

The emitter requires an explicit `call_symbols` map of 32-bit guest targets to
C identifiers. The mapped corpus supplies only independently reviewed declared
callees, each translated in the same mode. A call requires its exact mapped
fallthrough; indirect, unbound and missing-continuation calls fail closed.
The guest return-address store remains ordered and observable. Callees own
ESP cleanup and EIP restoration. All tracked register lanes and flags are
reloaded afterward, and the x87 cache is invalidated. Resumable mode checks
EIP before continuing. Corpus bindings do not provide production dispatch,
hooks, SEH, imports or a summary-based call ABI. The production adapter binds
ordinary direct calls through stable entry thunks.

SLEIGH can encode an absolute memory operand as a `ram` varnode rather than an
explicit LOAD/STORE. Codegen normalizes source operands to one captured read
shared by flag/result consumers, and pure destinations to explicit stores.
Single-operation NOT is supported; absolute destination RMW with flag writes
is rejected pending ordered-store corrections. Raw census input is unchanged.
The SSA builder rejects unnormalized data-position `ram`, preventing addresses
from being silently used as loaded values.

## Bounded x87 values and publication

`x87_values.py` tracks three logical stack values in double locals. The `values`
mode substitutes those locals for repeated `ST` loads while retaining all
physical helpers. The `region` mode additionally defers arithmetic `fset`
bookkeeping until an observation. It keeps TOP and structural push/pop/copy
operations eager. Dirty values, zeroed exact-integer metadata and tags are
materialized before guest accesses, opaque calls, division error seams, exits
and control-flow boundaries. A popped dirty value is written before the pop,
preserving physical residue. Window eviction flushes; joins and backedges reset
only after incoming edges have flushed. Register copies and exchanges flush
slots their helpers read. Status updates and PC/RC behavior still use the
existing runtime helpers; there is no memory forwarding or fast math.

The emitter exposes `x87_values=True` and `x87_region=True` as mutually exclusive
comparison options; `optimize=False` disables both. The corpus CLI selects
`--corpus-ir-ssa-x87 effects|values|region` with `--corpus-ir-ssa` and records the
mode in its report. Native checks compare the three SSA modes with eager C,
including dirty popped residue, ST7 wraparound, outside-window exchanges,
integer store observations, branch joins, loops and calls. Real interior
memory-fault/SEH equivalence remains unverified.

## Scalar x87 and local CPU state

The opt-in corpus mode `--corpus-ir-ssa-x87 scalar` replaces physical x87
push/pop/copy updates with scalar values indexed relative to entry TOP. It tracks
all eight physical residues, tags and exact-integer shadows, including wrapped
copies and popped contents. CW/SW helpers use a private nonescaping environment.
All guest accesses remain ordered. Stores, CFG edges, division seams, calls and
returns materialize the required FPU state; opaque recipes materialize and
invalidate the tracker. Scalar values can survive a read-only access, but not
joins or opaque calls. No floating SSA phis or memory forwarding are introduced.

Under PC=00, operations on proven binary32 operands can use one native float
addition/subtraction/multiplication plus the runtime's NaN/status normalization.
Other precision settings and unproven operands use the existing double helpers.
Division is unchanged. PC=00 alone never proves an incoming operand's width.
Specialization requires at least two arithmetic effects in a linear run before
an observation boundary, avoiding selector overhead for isolated operations.
`scalar-strict` keeps pre-load publication and the general arithmetic recipes.

`--corpus-ir-ssa-state locals` separately defers GPR/flag publication at ordinary
integer and x87 reads, retaining EIP/ESP/EBP for diagnostics. Required store,
division, call and return snapshots remain complete. The must-analysis does not
claim skipped fields were published; this lets dead-value elimination remove
intermediate flags overwritten before a real observer. `strict` remains the
default state policy, independently of the chosen x87 mode.

DIVERGENCE(original): [ssa-x87-scalar] ordinary interior load faults may expose
preceding published x87 state. [ssa-state-locals] similarly defers GPR/flag state
except EIP/ESP/EBP. [ssa-x87-binary32] uses the existing documented binary32
exponent-range policy. These apply only to explicitly selected performance
modes; accesses and faults are not removed. `RECOMP_NULL_CHECKS=1` selects strict
CPU and x87 publication and general arithmetic for guest exception dispatch.
Real interior fault/SEH equivalence remains unverified.

The native suite compares eager C with effects, values, region, scalar,
scalar-strict and scalar/local-state for 142 byte-backed fixtures × 24576 inputs
in both ordinary and null-check builds. Complete outgoing CPU/scratch state and
integer-store snapshots remain exact; no residue is normalized away. Dedicated
fixtures cover full stack wraparound, exact qword copies, reversed arithmetic,
status after float stores, CW changes, and deferred flags before watched stores.
The null-check build exercises its conservative compiled path on mapped inputs;
it does not inject actual null faults or validate guest SEH.

## Reducing SSA and emitted C

The builder prioritizes an explicit state graph over compact output. Every
listed instruction is a block, every referenced register byte gets an entry
phi, and wide reads/writes initially pack/extract bytes. Later passes coalesce
register groups and omit redundant CPU field stores at memory effects.

`simplify.py` runs reusable SSA passes before C emission. `canonicalize` iterates
trivial-phi simplification, equal-width COPY/ZEXT propagation, width-masked
constant arithmetic and shifts, BYTE-of-PACK selection, and PACK-of-BYTE
reassembly of a complete source. Narrow copies and partial reassembly stay
explicit. It does not use host signed arithmetic or infer undefined flags.
`live_values` traces the resulting dependencies from every LOAD, STORE, DIV32,
terminator, effect snapshot and return state. Unknown operations also remain
roots so the emitter must diagnose them. When given a publication plan, snapshot
roots include only the fields that need assignments; already-published values
remain observable in the CPU without redundant SSA computations. `simplify` removes dead or aliased
operations and phis from block lists; stable IDs and the value table remain
available for diagnostics. No memory effects, snapshots or publication barriers
are removed, and no memory forwarding or guest-store elimination occurs. The C
consumer declares and initializes only reachable values. `emit(...,
optimize=False)` retains the raw SSA emission for pass comparisons; the
production decoded emitter remains separate and unchanged.

`coalesce.py` introduces wider inputs and phis for complete runtime register
groups before lane-phi simplification. BYTE operations bridge the wider values
to existing p-code lanes; predecessor PACK operations build parallel edge copies.
Canonicalization removes complete reassembly/extraction pairs, so full-register
loop updates no longer require four live byte phis. Partial writes retain the
unwritten lanes explicitly. Groups lacking any lane stay in the existing byte
representation. This is not a new call ABI or permission to discard upper bits.

`publication.py` computes must-facts for whole CPU fields. A block entry field
is known only when every predecessor has published its exit value; the virtual
entry contributes the initial CPU state. Facts are relative to block entry/exit
values rather than raw phi IDs, so a backedge cannot confuse an older published
phi with its next iteration. Each access and return compares the required state
against those facts. Uncertainty retains the assignment. DIV32 invalidates all
facts because the error seam can return through a handler. IDIV32 and bound
CALL effects also invalidate those facts. On normal continuation
LOAD/STORE accessors do not mutate CPU state: watch/dirty observers are read-only,
and an armed null fault either transfers control through SEH or terminates.
Any future returning CPU-mutating observer needs explicit invalidation and an SSA
effect model before admission. The mock division handler still changes only
EAX/EDX; arbitrary handler changes are not modeled.

The C consumer can disable these passes independently with
`publish_changed=False` and `wide_registers=False` for runtime comparisons.

The byte-backed SSA tests compare interpreter results before and after the
passes, exercise loop-carried parallel copies and overlapping uniques, and
check pass idempotence, narrow COPY widths, constant shift boundaries,
unused faulting loads, pre-load state and unknown-operation diagnostics.
Additional interpreter checks validate the complete expected CPU at every
planned observation, including omitted assignments, branches, entry backedges,
partial-register joins and wide loop phis. Native watchers independently compare
executed store snapshots against eager C.

Before these passes, the game's integer string-comparison function illustrates the cost: 54 original
instructions generate 3389 C lines, with 1109 value declarations, 382 staged
phi-copy temporaries and 192 CPU-field publication statements. Its graph has
189 PACK and 380 BYTE operations. These are source/graph counts, not executed
instruction counts or proof of a speedup from any proposed change.

Further reduction should proceed in this order:

1. Extend width-aware canonicalization where measured output justifies it.
   Retain potentially faulting loads, stores, branches, returns, division
   effects and every required CPU snapshot; outgoing flags and memory residue
   stay live. Pure value numbering needs dominance and loop-aware reasoning.
2. Form actual basic blocks and construct phis only at joins for live inputs.
   Keep the virtual entry predecessor and parallel copies for loop edges. This
   removes instruction-by-instruction gotos and most transient state versions.
3. Extend the current register-group coalescing where native measurements
   justify it. Partial writes currently keep explicit byte operations; consider
   wider insert/extract operations while retaining byte-range alias analysis.
4. Extend publication must-facts only with explicit helper/observer contracts.
   Preserve required fault snapshots; reducing the number of observation
   barriers needs separate evidence.

After canonicalization, emit short expressions for single-use values and retain
typed temporaries for reused values and ordered effects. Measure C source size,
generation time, cold compiler cost, native text and execution separately.
Basic-block formation and expression inlining remain unimplemented. Source
reduction by itself does not establish faster or smaller native code; the game's
corpus documentation records those measurements separately.

## Production selection

Select SSA in the game's `game.toml`:

```toml
[translate]
ir_ssa = true
ir_ssa_x87 = "scalar"  # effects (default), values, region, scalar, scalar-strict
ir_ssa_state = "locals"  # strict (default), locals
```

Regenerate through `tools/build.py --regenerate`. Setting `ir_ssa = false`
restores decoded emission. Discovery, entry ownership and decoded dispatch
validation still run first. `ir/production.py` then replaces supported final
bodies while keeping stable entry thunks and the existing raw/base/hooked
tables. SSA direct calls use `CALL_FN`, publishing and reloading required state;
there is no summary-driven calling-convention optimization. Ordinary decoded
callees and SSA callees can be mixed, including replacement/hook selection.

Alternate-entry bodies, SEH frames/helpers/restores, pushed continuations,
nonreturning control flow, audited instruction/operand/visual-clock rewrites,
division error seams and auxiliary modules keep whole-function decoded C.
Indirect calls, jump tables, external tail transfers and unsupported instructions
also fall back through named SSA/lift diagnostics. A 2048-instruction budget and
Python graph recursion limit retain decoded C for expensive constructions.
Original interior fault/SEH equivalence remains unverified; null-check builds
compile conservative publication inside admitted functions.

The translation JSON report includes an `ir_ssa` object with emitted/fallback
counts and percentages, policy names, aggregated fallback reasons and a
per-function map. Its denominator is the final emitted function bodies,
including recovered bodies, with alternate entry wrappers counted only under
their owning body. These are automatically generated frontend coverage metrics,
not a reconstruction census, execution coverage or equivalence evidence.

`test_ir_production.py` exercises final-driver selection/reporting, mixed
SSA/decoded direct calls through thunk declarations and conservative exclusions
over actual synthetic instruction bytes. The native SSA comparison suite checks
body semantics; production entry-dispatch tests check replacement/hook/profile
policy. A real replay is still needed before performance capture.

## Remaining code-generation work

Extend SSA beyond the admitted integer and corrected x87 effects, preserving
guest widths, wrapping arithmetic, consumed flags, memory effects and faults. Treat unknown or partial
summaries as opaque boundaries. Summary-driven direct calls need explicit state
publication rules for hooks, imports, SEH, scheduler/runtime observers, and the
comparison path. Correct remaining raw x87 gaps before emitting scalar floating
operations directly rather than using the audited effects.

Continue with the game's existing function corpus and full CPU/memory comparison
checks. Keep the production C emitter available and measure correctness,
compile cost and execution separately. Any broader fidelity tradeoff requires
an explicit agreement; this frontend does not change the game's optimization
contract.
