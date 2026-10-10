# State contract cleanup

Keep checked returns enabled and set `state_contract_callers = []` while the
private integer-state path remains experimental. Preserve the two reviewed
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
4. **Validated:** regenerate and validate against original bytes and ordinary entries. Temporarily
   re-enable the same callers to exercise private variants and unexpected RETs;
   inspect emitted host assembly, then leave the checked-return-only app built.

Use the function diff harness and existing rounding comparisons. Record discarded
inputs and inherited discrepancies separately from new failures. Build through
the game wrapper with `--target app`; workload capture belongs to the user.

After this cleanup, fuse eligibility/checkpoint policy and evaluate the same two
callers before expansion. A profile gain is still required; this refactor alone
does not establish one.
