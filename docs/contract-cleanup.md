# State contract cleanup

Keep checked returns enabled. The private integer-state path remains experimental;
the same two reviewed callers are re-enabled for workload measurement. Preserve those
callers as the validation scope; do not expand to x87 contracts or change guest
memory accesses.

1. **Implemented:** return private variant requests as structured emission data. Remove discovery
   through generated C text, including requests from discarded SSA emissions.
2. **Implemented:** expose publication analysis facts before each observation. Build explicit
   call plans containing fast-path publication, hook guards, ordinary fallback
   publication and continuation outputs. Root only values those paths require.
3. **Implemented:** plan lazy-flag publication separately for ordinary fallbacks and unexpected
   returns. Preserve descriptors where proven so fallback-only arithmetic does
   not become unconditional work. Keep eager flags where no recipe is proven.
4. **Validated:** regenerate and validate against original bytes and ordinary entries.
   Exercise private variants and unexpected RETs, inspect emitted host assembly
   and rebuild the windowed app. Keep the same two callers as the measurement scope.

Use the function diff harness and existing rounding comparisons. Record discarded
inputs and inherited discrepancies separately from new failures. Build through
the game wrapper with `--target app`; workload capture belongs to the user.

Follow-up cleanups consolidate complete transfer plans, use structured private
declaration dependencies and emit `contract-report.json`. Eligibility and
checkpoint preparation are fused; successful private entries decrement once
and tail-call the leaf, while rejected entries leave polling to the ordinary
path. Ordinary generated bodies remain unchanged with the pilot disabled.

Evaluate the same two callers before expansion. Use host sampling with
`RECOMP_PROFILE` unset or `0`: the built-in guest profiler selects ordinary
fallbacks. A profile gain is still required; this refactor alone does not
establish one.
