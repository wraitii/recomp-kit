# Call contracts and static x87 mode analysis

## Current implementation

The RT3 pilot specializes direct calls from `0042c1b0` and `0042ce00` to
`005a10d0`. Checked returns preserve the guest CALL stack write, actual RET
address load, stack adjustment and state publication. Expected returns bypass
continuation lookup; unexpected returns publish the fuller state and use general
return handling. Ordinary entries remain available for other callers.

Integer transfer plans combine callee effects with continuation liveness,
including values escaping through the caller's return. Structured emission data
owns private variants and declaration dependencies; `contract-report.json`
reports the selected edges. Fallback flag publication retains proven lazy
recipes. Private preparation checks hooks, profiling, frame watching,
checkpoint countdown and replacement selection. It decrements once on success;
the ordinary entry owns polling on rejection. Replacement identity is resolved
beside the immutable dispatch tables without indexed table loads.

The body still transfers x87 state through `X86`. Its internal arithmetic
specialization checks the precision/rounding bits; the general branch handles
other modes. Guest scratch accesses and faults remain intact.

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

The pilot calls are downstream of this setup, but the mode reaching them still
requires a proof through subsequent calls and control flow. Other invocations
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
   guest RET check, scratch accesses and faults. Limit initial emission to the
   reviewed pilot edges and measure code growth before broader specialization.

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
