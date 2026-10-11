# Call contracts and static x87 mode analysis

## Current implementation

RT3 enables automatic private leaf contracts. Candidates need at least two
direct call sites and no more than 64 original instructions per site, then pass
closed single-entry, replacement/SEH and body preparation checks. Functions with
calls remain excluded. The reviewed `0042c1b0`/`0042ce00` to `005a10d0` pilot
remains explicitly selectable when automatic discovery is disabled.
Checked returns preserve the guest CALL stack write, actual RET
address load, stack adjustment and state publication. Expected returns bypass
continuation lookup; unexpected returns publish the fuller state and use general
return handling. Ordinary entries remain available for other callers.

Integer transfer plans combine callee effects with continuation liveness,
including values escaping through the caller's return and state read by later
calls even when it already resides in `X86`. Untracked GPRs and flags remain
observable on return; private layouts retain their callee outputs. Before
emission each direct call and tail is planned as a finalized, immutable
invocation record: form (ordinary, checked, private or tail), production macro
or literal custom binding, expected continuation, preparation guard and bound
shared layout, with a private invocation carrying its checked fallback as a
record. Emission renders from those records and chunked declarations derive
from them, so emitted C is never rewritten and prototypes are never
rediscovered from generated text; decoded bodies record their own bound
targets. `contract-report.json` reports the selected edges. Fallback flag
publication retains proven lazy recipes. Private preparation checks hooks,
profiling, frame watching, checkpoint countdown and replacement selection. It decrements once on success;
the ordinary entry owns polling on rejection. Replacement identity is resolved
beside the immutable dispatch tables without indexed table loads.

The shared preparation pass selects arithmetic conditions and state-transfer
requirements before lowering. Ordinary x87 entries retain guarded D3D8
arithmetic within the existing operation, density and activation budgets,
including calls whose private preparation was rejected. Non-x87 private entries
need no CW guard. Private x87 entries require a CW-preserving leaf and combine
D3D8 arithmetic with reduced state publication; no transfer-only private x87
body is emitted. Strict x87 and disabled specialization retain general entries.
Hooks use general arithmetic. Instruction-local proven PC/RC bits can still
fold general recipes, leaving unknown bits dynamic.

The current interface transfers values through `X86`. Future specialization
will pass actual arguments and results directly for integer and x87 functions;
arithmetic conditions remain separate from that interface. Private preparation
checks precision/rounding before decrementing the checkpoint countdown. Guest
scratch accesses and faults remain intact.
Each callee has one private body publishing the union of its callers' required
outputs. Callers retain their own continuation liveness and reloads.
`emit_c.prepare` computes SSA, publication and fallback requirements before C
emission. `contract_variants.SpecializationPlan` merges caller requirements into
shared layouts before any body is emitted. Prepared graphs pass between workers
through temporary files with SSA value references encoded by ID; emission uses
those graphs without repeating preparation. Private entry eligibility and
full-output checked-body aliases are settled in preparation. `x87_plan.py`
selects clones, binary32 runs, carry shapes and activation budgets ahead of
emission; emission verifies that its tracker matches the plan. Call sites carry
an immutable transport-independent `CallState`: semantic callee inputs,
continuation outputs, proven kept fields, resident fields and an explicit opaque
marker for state outside the integer projection. `PreparedFunction.finalize`
binds shared layouts without mutating call plans and report rows derive from the
finalized plan. A layout that omits a required output fails during finalize.

`tools/recomp/x87_audit.py` selects propagation, writer or real-call context
analysis with `--game` or `--game-dir`. Its shared library consumes lifted
instruction CFGs independently of production SSA acceptance. Production and
offline analysis share named scan records, internal entry classification and CW
entry scope through `program.py`. Byte-checked assumptions,
CFG graphs and bit provenance are reusable analysis interfaces.
Hypothetical roots, broad discovery reports and candidate context summaries stay
offline. Context preservation and constant mode are separate results.

`propagation --facts-out` emits `x87-compiler-facts-v1` for the selected production
policy. The compiler validates the executable, producer sources, code map,
configuration, native sources, assumption dependencies, entry scope, original
instruction sites and replacement boundaries before consuming it. Exploratory
contracts and old candidate summaries cannot produce compiler facts. Unknown
conditions retain guarded specialization. Hooks retain general arithmetic.
Facts can remove arithmetic duplication and private call-site CW checks without
establishing unrelated private-entry conditions. Original CW writes/restores and
other x87 state remain translated. Return summaries remain distinct from known
entry and instruction modes.

Use `--build-root` to select the installation/build tree. Detailed reports and
trusted local scan pickles remain ignored; only selected compiler facts ship.

## Objective

Make relevant x87 control-word facts explicit analysis metadata. Propagate known
bits and symbolic entry values through instructions and calls, then generate
known-mode private bodies where the incoming precision/rounding bits are proven.
A preservation summary supports propagation even when a helper temporarily
changes the control word and restores it before returning.

Control-word facts are independent of x87 values, exact-integer shadows, tags,
TOP and status. A mode proof does not justify removing their publication or
reloads. Full control-word state remains observable even when only some bits
are needed for arithmetic specialization.

## Worked example: `005a10d0`

The original function contains no control-word setter or call. It inherits its
caller's mode and preserves it. Its fast arithmetic requires
`(CW & 0x0f00) == 0`; the function alone supplies no constant incoming value.

RT3's CRT initializer at `005a63b8` calls
`__controlfp(0x10000, 0x30000)`. Those CRT constants map to raw x87 precision
mask `0x0300` and value `0x0200`: double precision, with rounding unchanged.
Later, device creation at `00549991` uses flags `0x20` or `0x40`, both omitting
`FPU_PRESERVE` (`0x2`). That creation precedes the engine frame loop. The runtime's
D3D8 boundary models its effect as `(CW & ~0x0f3f) | 0x003f`, establishing the
precision/rounding bits needed by the specialization while preserving other bits.

The full device body joins success and retry/error paths at `00549aed`, losing
the success-edge constant there. A separate success-continuation view exposes
subsequent losses without excluding failures from ordinary analysis.
`showErrorDialog` at `0051e980` exits on its first invocation but returns when
its global error-handling flag is already set, so it is not unconditionally
no-return. The pilot calls are downstream of setup, but their mode still
requires proofs through subsequent calls and control flow. Other invocations
of the general entry may have different modes.

Temporary changes need return relations rather than only a may-write flag.
For example, raw instructions in `_fabs` at `005a152e` save the incoming word,
select `0x133f`, and restore the saved word on ordinary paths through `__ctrlfp`
at `005a9070`. That helper writes the word and returns its previous value;
Ghidra's no-argument reader prototype is insufficient evidence. Restore coverage
on error paths remains unproven. The incoming fact can survive only exits for
which restoration is established; calls made during the temporary mode receive
that temporary fact.

## Implementation plan

1. **Define facts and effects.** Add plain, serializable metadata for known
   control-word bits, symbolic incoming values and possible changes. Keep it
   separate from integer field contracts. Describe local setters, resets,
   environment save/restore and audited native boundaries from instruction IR
   and runtime semantics. Unknown operations contribute conservative effects.

2. **Prove return relations.** Start with functions containing no setters and
   trustworthy call closures. Propagate effects through the call graph with
   conservative convergence handling. Then recognize save/change/restore
   sequences where dataflow and memory provenance prove the saved value reaches
   every relevant exit. Unproved aliases, restores, callbacks, indirect calls
   and unwind paths invalidate the relation. Preserve the guest operations.

3. **Propagate mode facts to call sites.** Use a forward CFG analysis with known
   bits and symbolic values; joins retain only facts common to incoming paths.
   Apply callee return relations and audited boundary effects. Track the actual
   mode at each nested call, not merely whether the enclosing function restores
   its entry mode. Hooks and replacement dispatch require matching guarded
   paths or conservative facts.

4. **Report the RT3 proof.** Trace device setup through the frame path to the two
   pilot callers. Extend `contract-report.json` with incoming mode facts,
   provenance, return relations and the boundary that lost each proof. Keep
   ordinary paths where the analysis cannot establish the required bits. If
   static propagation is insufficient, evaluate one enclosing mode guard over
   a proven region before adding repeated per-edge guards.

5. **Emit a known-mode private body.** Generate it from the same instruction IR,
   using the proven precision/rounding bits to remove the mode check and general
   arithmetic branch. Retain the ordinary entry, observation policies, actual
   guest RET check, scratch accesses and faults. Use the same conservative admission and shared layouts for automatic leaf
   discovery; measure linked growth separately from generated source size.

6. **Validate and measure.** Compare against original bytes and ordinary entries
   with the diff harness and existing rounding fixtures. Cover mode changes,
   restores, branch joins, ordinary fallback and unexpected RETs. Report discarded
   inputs and inherited discrepancies separately. Regenerate and rebuild through
   the game wrapper with `--target app`; inspect linked assembly for removed
   checks and fallback code, CPU-field transfers, frames and spills. Workload
   capture belongs to the user. Use host sampling with `RECOMP_PROFILE` unset
   or `0`, since the built-in guest profiler selects ordinary fallback. Expand
   only after correctness checks and measured benefit.

This work precedes broader x87 transfer contracts and the register ABI. Those
require separate proofs for values, shadows, tags, TOP, status and observable
residue; removing scratch memory requires additional alias and observability
proofs.

A custom direct-call ABI can build on the shared input/output contracts: pass
proven values as C arguments and results, keeping guest CALL/RET stack effects
and an explicit publication path for rejected preparation and unexpected RETs.
ABI layout and specialization selection belong outside the instruction proofs.
