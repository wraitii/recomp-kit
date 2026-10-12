# Translator and instruction IR

Translation reads the Ghidra code map and the executable, lifts each mapped
function with SLEIGH, builds an integer SSA with scalar x87 tracking and emits C.
Game measurements and the optimization history live in the game repository.

```
code map v3 + PE -> program.py + frontend.py -> ir/lift.py -> ir/normalize.py -> ir/ssa.py
  -> prepare.prepare + x87_plan -> contract_variants.SpecializationPlan
  -> final call binding -> emit_c.emit_prepared -> output.py (chunks, funcs.h, table.c, symbols.json, report)
```

`tools/recomp/driver.py` is the command (`--game DIR --out DIR`, `--report`,
`--jobs`, `--allow-unmodelled REASON`); `build.py` runs it. `settings.py` turns
`game.toml [translate]` and the command line into one `Settings` object that is
passed to the program model, the workers and the corpus runner.

## Program model

`decode.py` owns byte-backed instruction records and decoding. `x86_frontend.py`
owns operand identities, declared function fields, CFG successors, return-jump
proofs and SEH analysis; the eager emitter inherits that analysis. `frontend.py`
assembles raw `FunctionIR`, and `ir/normalize.py` produces normalized IR while
retaining original bytes and decode evidence. These frontend modules do not
import either emitter. `ir/guest.py` describes register widths, aliases, roles and
guest linkage; C access expressions belong to the backend. x86 CALL pushes the
instruction continuation, and RET pops it and applies guest stack adjustment,
independently of private host transport.
`Program.body` attaches the same validated records to exploratory lifted IR;
the driver shares its image and decode cache with the program model. SLEIGH
still validates instruction lengths and effects independently.
Decode records contain structural operands without listing text. Exploratory
lifting uses `decode_span_record`; the production listing path formats those
records separately and retains its operand-width restrictions.

The code map (`recomp-code-map-v3`, `code_map.py`) is the only authority on code:
function spans, jump tables, interior entries (`branch`, `data`, `table`) and
non-returning functions/calls, addresses only. A missing or incomplete map is a
named error; unknown control flow fails with a diagnostic telling you to fix
Ghidra and re-export, and nothing guesses entry points. `code_map.py --pack`
reads a Ghidra export plus the private PE and keeps only spans, lengths where a
linear SLEIGH decode would disagree with Ghidra, and the tables.

`program.py` loads the PE (sections, relocations, imports), the map, and
`game.toml` `alternate_entries`/`entry_points`, and answers which functions,
entries, tables and noreturn callees exist. Internal-only switch entries,
outside references and call returns are derived from it. Pushed continuations
(`PUSH X; RET`) are not modelled, and computed jumps without a table are dynamic
dispatch (`recomp_jump`).

## Lifting

`lift.py` uses pinned `pypcode` SLEIGH semantics for 32-bit x86. Operations have
an opcode and varnodes `(space, offset, size)`; memory, register byte lanes and
arithmetic flags stay explicit. Boundaries come from the code map. Folded WAIT
prefixes lift in order, same-operand identities such as `XOR EBX,EBX` fold, and
consumed carry dependencies such as `SBB EAX,EAX` do not. SLEIGH omits AF; the
emitter supplies it for ADD/SUB/INC/DEC/SBB/ADC/NEG/CMP.
Register-name diagnostics and contract cell maps are cached by SLEIGH language;
the contract field projection itself remains the x86 runtime's GPR and flag set.

Raw SLEIGH is not faithful to the runtime's x87 (FIST/FISTP, FRNDINT, FPREM,
FCOM/FNCLEX status, FXAM, tags). `x87.py` corrects these by lowering operands,
recovered from `Insn.raw`, to ordered `X87_MEM`/`X87_REG` effects that call the
audited runtime helpers (`fx87`, `fdivz`, `fset`, exact-FILD metadata, FIST and
FRNDINT rounding, status merging, FXAM, partial FPREM). They share the memory
token with guest accesses. FS/GS and 16-bit addressing, FCOMI/FCMOV,
transcendentals and environment operations stay named fallbacks.

`cfg.py` holds `FunctionIR` (instructions, successors, table sites, external
exits, noreturn calls, SEH effects, entries) and the call-graph SCC order.
`normalize.py` turns that lifted model into corrected operations for SSA,
including audited x87/string lowering, explicit absolute-memory effects, SEH
and noreturn operations, and bound external-exit stubs. `emit_c.py` keeps the
C emission and preparation stages.

## SSA and C emission

`ssa.py` builds SSA over integer p-code on the instruction CFG. Registers and
instruction-local storage use byte lanes, preserving AL/AH/AX/EAX aliasing; each
instruction is a block, joins have phis, and LOAD, STORE and x87 effects thread
one memory token with no forwarding or store removal. Raw FLOAT operations,
unbound calls, user operations and intra-instruction control flow are rejected
with a named reason. `simplify.py` canonicalizes (trivial phis, copy/extension
propagation, constant folding) and removes dead values; potentially faulting
loads, stores, branches, returns, division and unknown operations are roots.
`coalesce.py` carries whole registers through phis; `publication.py` keeps
must-facts about published CPU fields (a field is known published only if every
predecessor published it; after a call or helper, a register or EIP/FS field
equals its reload, flags stay unknown).

`prepare.prepare` computes publication, fallback, flag and arithmetic requirements
without emitting a body. Final binding attaches call values and `lowering.py`
boundary operations before selecting CPU-field transport. `x87_plan.py` selects
clones and validates tracker/carry shapes and activation budgets.
`emit_c.emit_prepared` prints the finalized boundary operations through
`c_boundaries.py` and lowers integer values to
unsigned, width-masked C with staged parallel phi
copies, sign-bias comparisons and saturating shift counts. Dword DIV/IDIV are
checked `div32`/`idiv32` effects that reach the runtime error seam with the
original address. Memory arithmetic, logic and shifts, including absolute
(`ram`) operands, capture one read and emit flags after the STORE; `MOVSD`/`REP MOVSD` call runtime helpers in access-then-advance
order. Direct calls publish the CPU, call `entry_ADDR` (`CALL_FN`) and reload
each register whole, each flag and the memory token, keeping only used reloads;
a field whose four lanes are one value's bytes is stored whole; indirect calls go through
`recomp_call`. Jump tables and tail transfers inside or outside the body,
alternate entries (`body_X(c, entry)` plus `fn_E` wrappers), noreturn calls and
SEH frame effects (enter/adopt/leave/orphan, ordered, with full state
published) are emitted; after `setjmp` returns nonzero only `c` is used.

`production.py` runs one function through SSA with a 16384-instruction budget.
Bodies SSA cannot emit stay on `decoded.py`, the plain eager emitter (Capstone
decoding, full-state helpers) fed the mapped boundaries: about 65 bodies,
named in `translate-report.json` (INT, RDTSC, ROL/RCR, SHLD/SHRD, MMX,
FNSAVE/FNSTENV, guest continuations, over-budget bodies). `decoded.py` also emits the wrappers of internal-switch
entries. Unmodelled instructions become `recomp_unmodelled(c, addr)` traps only
under `--allow-unmodelled`; otherwise translation fails.

The decoded emitter handles `FEMMS` (`0f 0e`) by emptying the x87 tags and
clearing TOP, preserving the control word and other status fields. AMD's Athlon
optimization guide (22007K, section 9) specifies this stack-pointer reset;
Unicorn's Athlon model leaves TOP unchanged, so it is an incomplete oracle for
that field. Undefined data-register contents retain deterministic runtime values
and exact-value shadows. `EMMS` retains the Intel tag-only behavior.

`output.py` writes address-bucketed body chunks (2 MiB budget), entry thunks,
`funcs.h`, `table.c`, `symbols.json` and the report (counts, entry kinds, SSA
versus decoded reasons, unmodelled, seconds). Workers lift and emit one function
at a time from the image bytes; dispatch checks, call returns and entry sets
come from the program model, never from generated C.

`operations.py` is the single intrinsic description of IR operations: ordering,
memory direction, possible faults, observation, clobbering and termination.
`simplify`, `publication`, `transfer_plan`, `ssa`, `x87_carry`, `x87_plan` and
the emitter derive from it. Consumer policy stays explicit and separate:
relaxable observation (`publication.RELAXABLE_ACCESS`), lazy-flag eager
events, x87 reset/flush and liveness roots are derived from the table plus
named boundaries, not duplicated literals. A missing description raises a named
diagnostic and the body takes the decoded fallback. The C printer separately
validates which described operations it can render; that capability check is
not an effect classification. `decoded.py` keeps its own
mnemonic-level acceptance and per-mnemonic flag lowering; that raw path is
intentionally separate and does not consume the p-code operation table.

## State publication

SSA bodies keep state in host locals between observation points. Two kinds of
relaxation are kept apart. A *representation* change computes everything the
eager emitter would publish and publishes it at the same points (scalar x87,
deferred GPR/flag publication, carried x87 state, lazy flags, lazy NaN); it
needs only "no observer between", and eager full-state comparison checks it. An
*obligation* change omits state no admitted observer reads (call contracts,
popped x87 residue) and needs analysis evidence. Observation points are outside
code (imports, unknown calls, hooks, replacements), guest-visible memory,
flag/x87/SEH observers, scheduler checkpoints at imports, faults, and active
hooks or store watches. Unknown means conservative; accesses and faults are
never removed and their order is kept.

### Scalar x87

`x87_scalar.py` replaces physical push/pop/copy updates with scalars indexed
from the entry TOP, tracking all eight residues, tags and exact-integer shadows.
CW and SW are scalar locals passed to always-inlined `_sw` helper forms,
including FIST/FISTP, which read the scalar value and exact-integer shadow. Seams
(division, calls, opaque recipes, returns) materialize the required state.
`x87_carry.py` carries unpublished state and cached clean values across internal
CFG edges. Dirty parts union at joins; clean parts survive only when every
predecessor caches them. Cached parts do not require state publication. Each block
has a conservative fixed-point shape, predecessors write canonical function-scope
slots, successors copy them, and planning raises `SSAError` (whole-function
decoded fallback) on shape drift. External entries with internal backedges reset
the tracker so a first call never reads carried locals before initialization.
Carry needs the MSVC convention.

`x87_transition.py` owns validated instruction recipes, width-typed expressions,
CW/SW operands, helpers and ordered activation/reset/publication operations.
Scalar and eager execution share operand directions, pop counts, arithmetic and
unary recipes. The decoded eager policy retains its broader legacy instruction
acceptance; SSA admission stays byte-validated in `x87.py`. `x87_scalar.py`
interprets recipes into tracker changes and transition records, never C text.
`x87_carry.py` interprets those same transitions with real typed operands and
symbolic carry values. `x87_plan.py` stores each clone's block/event/edge
operations once; emission prints them through `c_x87.py` without replaying a
mutable tracker. Shape coverage, activation counts, operand roles and event
coverage remain checked before or during printing.

Control-word uses are dynamic, guarded constants, proven constants or partially
known operands with an explicit hook-dependent selection. Folding happens while
constructing operands; the C printer renders them without regex substitution,
helper-name replacement or leftover-text checks. Proof scope/provenance remains
attached to folded operands and proves only CW bits, never slot or status state.

Under PC=00, proven-binary32 operations use native float arithmetic plus the
runtime's NaN/status normalization once a linear run has two arithmetic effects.
A body with at least four PC/RC-sensitive operations in one tracker window is
emitted through the same body pass as private specialization (`x87_cw_clone`),
with D3D8 and general arithmetic: each activation checks hooks and tests
`(cw & 0xf00) == 0`, otherwise jumping to the same activation in the general clone; past the guard the
D3D8 body folds PC/RC to 0. Private state transfers are an additional capability
of that pass. Multi-window ordinary bodies are not duplicated. Independently, proved
instruction-local CW bits can fold ordinary scalar recipes without cloning;
partial facts leave the other PC/RC bits dynamic. Calls retain a hook guard,
and configured contextual entry assumptions are not emission facts.

Lazy NaN keeps basic-arithmetic results in full precision and folds
(`isnan`/indefinite) where the value stops flowing into NaN-propagating
arithmetic: stores, comparisons, FCHS/FABS, FNSTSW, FSTCW/FLDCW, opaque
fallbacks and any flush. IE is sticky, so only when the fold is computed
differs. The fast CW clone stores proven-binary32 values with canonical NaNs
without repeating the general float conversion and NaN quieting. It is off
under `fault_state = "exact"`.

### Lazy flags

`X86` carries a descriptor (`cc_op`, `cc_size`, `cc_mask`, `cc_a`, `cc_b`,
`cc_res`, `cc_carry`) for the last ADD/ADC/SUB/SBB/CMP/logic/INC/DEC;
`cc_carry` uses the former padding byte without changing the CPU layout.
`cc_op == X86_CC_NONE` means
the six flag fields are current. `x86_cc_settle` writes them; `x86_get_eflags`,
`x86_set_eflags`, `x86_sahf`, `recomp_comis`, the interpreter and `recomp_call`
settle first. `ir/flag_region.py` walks from the entry or a post-call point to
the next CALL/CALLIND/RET: with no flag read and no flag write the settle is
removed; with no read and all six flags written on every path it becomes
`x86_cc_drop`. After a call, the flags read, reloaded from the fields or not
written on every path form a mask: `x86_cc_settle_mask` (region without
writes) and `x86_cc_settle_or_drop` materialise only a descriptor whose
`cc_mask` meets it, and otherwise keep or drop it. A full settle stays for
paths leaving the body without
a call or return, for calls that can continue elsewhere (SEH adoption, noreturn,
setjmp), and where hooks may observe (entry thunks and `recomp_jump` settle
before a hook). Decoded bodies use an explicit per-mnemonic flag-effect table.

`ir/cc_carry.py` carries recipes through resolved CFG edges and merges equal-width
recipes with payload phis, including differing producer kinds. Only flags covered
by every predecessor's current recipe are deferred; other flags stay eager.
Entry edges, missing recipes, incompatible widths and unresolved cycles retain
eager publication. Deferred fields are excluded from publication liveness, while
their operands, incoming carry and result remain live.

### Call contracts

`call_contracts.py` summarizes every body (decoded included) over the eight GPRs
and six flags: `reads` is a backward may-liveness of entry values (through
direct callees), `kills` a forward must-definite over all return paths (a GPR
only if all four byte lanes are written), `writes` the fields any reachable
instruction or callee may write. A save/restore stays in `reads`. SLEIGH omits
AF for ADD/ADC/SUB/SBB/CMP/INC/DEC, which both emitters define, so the summary
adds it; any instruction outside a flag-free mnemonic list may write every flag.
Recursive components start empty and fall back to the conservative contract if
unconverged. At a direct CALL the emitter drops field `F` only when
`F not in reads and F in kills`, and keeps it when `F` is in neither `reads`
nor `writes`: no publication, and the SSA value continues past the call as
`CALL_KEEP`, known in the CPU afterwards only if it was before. Flags are kept
only all six together, so a pending descriptor passes through the callee
untouched; bodies with SEH effects keep nothing. ESP and EBP are never dropped
or kept. Indirect calls, unbound targets, failed lifts, SEH and alternate-entry
bodies and native replacements read and write everything and kill nothing.
Because any function can be hooked and a hook may rewrite the CPU, dropped and
kept fields are still published behind `recomp_hooks_ever`, and `CALL_KEEP`
rereads the field (after a settle, for flags) on that path. The flag is tested
on both sides of every running call, so it rises only before guest entry: the
loader arms it for any mod with a plugin or script, and a hook installed after
entry without it is refused (`POP_E_STATE`).

`checked_returns` admits reviewed, unreplaced single-entry SSA bodies with closed
CFGs and no SEH. Their private direct-call entries take the continuation decoded
from the original CALL bytes. RET still loads the guest return address, adjusts
ESP and publishes the same CPU/x87 state; a matching continuation skips
`recomp_return`, while a mismatch uses it unchanged. Ordinary entries remain for
indirect calls and callbacks. Checked entry thunks retain scheduler checkpoints,
frame checks, profiling and hooks; a changed base dispatch selects the ordinary
replacement. Auxiliary modules currently retain ordinary entries. An empty list
restores ordinary calls when automatic leaf discovery is also disabled.

`private_leaf_contracts` automatically admits closed, unreplaced returning
leaves after private and checked preparation. A reuse filter requires at
least two direct call sites and at most 64 original instructions per site.
Eligible closed callers use those targets; failed caller preparation remains decoded.
`state_contract_callers` additionally selects explicit caller addresses. Direct edges into
checked leaf bodies get private entries specialized for the required GPR/flag
outputs. A callee has one private body with the union of its callers' output
masks. Frozen `PrivateLayout(inputs, outputs, resident_opaque)` records order
fields by guest contract identity; inputs cover callee reads and linkage fields,
while x87, EIP, FS, DF and runtime state remain resident.
`contract_variants.SpecializationPlan` unions outputs across all callers.
Preparation retains `ProvisionalBindings`; a distinct final-binding phase attaches
`FinalBindings` with the shared layouts and ordered per-site `CallValues`.
`CallState` remains field requirements; `call_values.py` binds layout inputs to
caller SSA values, outputs to call results and preserved fields to before/after
continuation values. Missing SSA lanes explicitly remain resident. Full integer
fallback inputs/results and resident opaque state are separate from the private
layout; unused shared outputs still have result bindings. Validation checks
layout order and coverage before transport selection.

`lowering.py` finalizes publication operands and flag recipes, invocation,
eligibility/hook guards, reload/preservation, post-call settlement, checked
returns and parallel edge copies. Private calls carry their inputs and outputs
through the CPU fields (`CPUFieldTransport`); `PrivateLayout` still binds the
shared ordered fields and `call_values.py` supplies the fallback inputs/results
that transport validation requires. Guest CALL pushes and RET pops remain SSA
memory effects; entry thunks retain eligibility, checkpoints and hooks
independently of transport. `c_boundaries.py` prints these decisions;
`c_symbols.py` owns invocation rendering and declaration discovery. No semantic
module renders invocations or owns C prototypes. The x87 tracker uses the
structured transition plan described above.

C emission stores integer SSA values of up to four bytes in `uint32_t` and
explicitly widens qword results and `SUBPIECE` shifts. Guest accesses cache the
arena base, refreshed after calls, string/division helpers and SEH seams; builds
with store hooks or null checks retain the original accessors. Guest writes and
state publications remain ordered. Ordinary continuations use a small inline
return check with general dispatch shared in a cold helper per chunk. Contract
fallbacks and exceptional float stores are marked unlikely. Ordered x87
comparisons update status bits without branches; unordered handling stays
inline to avoid call-related spills and retains signalling-NaN behavior.

Emission requires those final decisions
and checks them against both the caller requests and private callee records.
Final binding hands fresh graphs directly to emission in the same worker, avoiding
another SSA serialization round trip.
Prepared SSA graphs pass between workers in temporary files through
`prepared_store.py`, encoding value references by ID. Each body is emitted once;
full-output integer private entries alias the checked body.
The report distinguishes each edge's required outputs from its entry mask.

`body.py` defines frozen emission policies, preparation requests and
`BodyVariant(entry, returns, arithmetic)` records. Supported forms are ordinary,
multiple entry, checked and private; only private bodies admit a whole-body
`Guarded(CW_FAST, entry)` arithmetic choice. Ordinary activation choices and typed
instruction proofs remain in the x87 plan. `c_symbols.py` owns backend spellings
and shared entry signatures. The corpus's `emit_c.emit` wrapper adapts keyword
options to typed requests; unused production emission wrappers were removed.
`phases.py` names scan, analysis, selection, preparation and emission payloads;
`selection.py` centralizes eligibility without changing the selection budgets.

`artifacts.py` snapshots lifted and normalized functions as immutable byte
payloads in the translation's temporary store. Keys include guest language,
source bytes, boundaries, CFG, exits, noreturn/SEH effects, entries and normalized
tail targets as applicable. Normalization is independent of emission policy;
SSA, publications and arithmetic plans remain variant-local. Every cache read
materializes a fresh graph, including reads in different workers. Temporary
snapshots are retained for repeated normalized preparation only; one-use
normalized graphs pass directly to SSA. Snapshots never survive a translation
run, so changed frontend code cannot reuse
an older producer. `frontend_work` reports build/reuse and instruction counts in
the contract report and optional driver report; emission never rebuilds SSA.

SSA dependencies include all state escaping through the caller's own
RET and full snapshots on observation paths. Input contracts use the same
byte-derived, corrected instruction IR as those leaf bodies. Unexpected guest
returns publish every changed field before ordinary return handling. Hooks,
profiling, frame watches, due scheduler checkpoints and replacements select a
fully published ordinary checked entry. Exact fault-state mode and auxiliary
modules retain ordinary entries. Guest accesses and the complete existing x87
materialization policy remain in place; integer contracts do not cover x87 fields.

`transfer_plan.py` owns call and return publications, hook guards, ordinary
fallbacks, flag recipes, continuation outputs and liveness roots. `publication.analyze`
exposes pre-event facts as well as required stores; ordinary fallbacks retain
only unproven integer fields, while flags remain explicit because a published
recipe can leave their physical CPU fields stale. Fallbacks and unexpected RETs
have separate flag publication plans, preserving proven lazy descriptors.
Private variant requests drive both body declarations and variant generation;
discarded emissions do not register variants or diagnostic decisions.
`contract-report.json` records selected callers, private output masks, call-site
publication decisions and SSA fallback reasons in the generated directory.

Private entry preparation combines eligibility with the checkpoint countdown.
A successful preparation decrements a nonzero countdown once; the private
thunk then tail-calls its body. A rejection leaves the countdown untouched and
the fully published ordinary entry owns the checkpoint, including due polling.
Hooks, profiling, frame watches and replacements retain that ordinary path.

`x87_cw.py` owns the control-word lattice, named masks and per-activation
arithmetic choices; `x87_propagation.py` owns `Prepared` bodies, return
relations and entry joins; `x87_report.py` owns the byte-backed writer/exposure
inventory and the driver's mode report (`x87_mode.py` re-exports them).
`ModeCondition(mask, value)` is the canonical concrete condition. Known bits,
symbolic entry bits, return preservation, configured premises, entry
verification and runtime guards stay distinct: a condition constrains only the
control word and proves nothing about x87 values, tags, shadows, TOP, status or
exact-integer shadows. Arithmetic is `Dynamic`, `Guarded(condition, boundary)`
or `Proven(condition, proof)`. `Guarded(entry)` keeps a fast-only private body
behind private entry preparation; `Guarded(activation)` keeps the in-body
fast/general clone pair; both retain the external ordinary fallback. `Proven`
keeps instruction proof scope and provenance and is never rendered as a runtime
guard. Private-entry guards are not static proofs. The plan's choice determines
clone routing; the scalar's site choice combines that route with the retained
instruction fact and is the single input to CW recipe folding. Private-call
preparation retains its proof record, and configured entry verification retains
the contract's scope and evidence. Instruction facts cross the
compiler-facing emission boundary as typed `InstructionFact` records matched to
their function and site; a bare `ModeCondition` is not a proof and is rejected
there (legacy tuples remain accepted only by analysis/verification APIs).

### Policies

| `[translate]` key | Default | Effect |
| --- | --- | --- |
| `fault_state` | `"relaxed"` | `"exact"`: strict x87 (publish before loads and stores, general arithmetic recipes), every pre-access GPR/flag snapshot, no lazy flags or NaN. Relaxed defers GPR/flag publication at loads and stores, keeping EIP/ESP/EBP |
| `msvc_x87_convention` | `true` | Every register above TOP is tagged empty, so popped residue is not published at calls and returns. Flags stay exact: CRT helpers return and consume flags. FINCSTP/FDECSTP bodies keep the exact flush. `false` publishes complete x87 state |
| `x87_cw_clone` | `true` | `false` emits only the general body |
| `call_contracts` | `true` | `false` restores full publication at calls |
| `checked_returns` | `[]` | Addresses given private direct-call entries with checked guest continuations |
| `private_leaf_contracts` | `false` | Automatically discover reusable private leaf contracts |
| `state_contract_callers` | `[]` | Caller addresses using specialized integer-state entries into checked leaf bodies |

A `RECOMP_NULL_CHECKS=1` build exposes CPU state to fault dispatch: translate it
with `fault_state = "exact"` and `msvc_x87_convention = false`.

DIVERGENCE(original) tags in the emitter name the accepted differences:
`[ssa-x87-scalar]` interior faults and store-watch callbacks may see the
preceding published x87 state; `[ssa-state-locals]` and `[ssa-call-contracts]`
let a fault or SEH context raised before a dropped field is overwritten, inside
a callee that keeps the field, or between an elided settle and its flag read,
see stale fields;
`[ssa-x87-binary32]` the binary32 exponent-range policy; `[ssa-x87-convention]`
popped residue unpublished at calls and returns. Interior fault and SEH
equivalence is unverified.

## Validation

- `tools/recomp/diff/run.py` runs translated functions and the original bytes
  under Unicorn on the same inputs and compares registers, flags, x87 state and
  memory (see the kit `AGENTS.md`). The oracle has known x87 quirks: FCOM/FCOMP
  on a QNaN omits IE, NaN payloads, invalid 80-bit encodings and RDTSC differ.
- The function corpus ([corpus README](../tools/recomp/corpus/README.md)) compares
  the eager and SSA variants (and an optional native kernel) on real game bytes;
  the SSA variant must lower every row.
- Report counts are frontend coverage, not execution coverage or equivalence.
- For x87 transition changes, compare eager and scalar output separately, then
  compare stateful sequences, carry joins, activation/reset/publication and
  dynamic/guarded/proven/partial CW operands with hook fallback. Roundtrip
  prepared stores and verify that external SSA values and shared CW proofs
  retain identity. Compare regenerated C and specialization decisions as well
  as refreshed offline propagation and proof contents. Recipe/text equivalence
  does not establish native floating-point or fault equivalence; a paired
  `--changed` run with identical bodies executes no new inputs.

### Rejected experiments

A private argument/result ABI and an LLVM emitter were built, compared against
the shipped C CPU-field path and removed. Neither demonstrated a performance
benefit. The private ABI grew 169,027,975 bytes of generated C to 184,028,662
and linked app text by 1,386,576 bytes over 8,133 call sites. The LLVM emitter,
tested on 14 selected functions, agreed on the recorded inputs but increased
linked text and bridge traffic while C already benefits from Clang's optimizer.
The current contract is C with CPU-field transport.

### Selected regeneration

`tools/build.py --regen-function HEX_ADDR` (repeatable) re-emits selected
functions using the whole-image preparation saved by a full regeneration.
Unchanged generated files retain timestamps, so Ninja recompiles affected
C chunks and relinks the requested target. Address buckets can include
neighboring functions; this is chunk compilation, not an isolated object per
function.

The ignored `recomp/emission-cache` (`recomp-emission-cache-v2`) validates
preparation sources, executable, map, policy, facts, native evidence and the
generated tree. It stores pre-finalization bodies, including private bodies, so
selected regeneration re-runs `lowering.py`, `c_boundaries.py`, `call_values.py`
and the printers from the cached SSA. Those modules are excluded from the
preparation identity; `prepare.py`, `c_symbols.py` and the frontend remain in it.
Contracts, selection, shared layouts and entry tables remain those of the full
generation, so changes to them require a full regeneration. Selected generation
records its scope and the preparation/finalization source revisions in
`partial-generations.json` and clears the whole-image translation stamp. A later
full `--regenerate` therefore cannot mistake a partially updated tree for a
fully current translation. Auxiliary modules currently require full generation.
