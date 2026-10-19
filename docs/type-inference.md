# Type inference

`tools/recomp/typeinf/` infers object shapes from original bytes: struct fields,
pointer nesting, vtable holders and interface labels. Its outputs are research
facts for Ghidra and, once a site can be bounded, for translator call facts.
Ghidra annotations are never an input.

## Whole-image dispatch bounds

Run from the kit root with the project's Python environment:

```sh
PYTHONPATH=tools:tools/recomp python -m typeinf.nominal \
    --game <id> --build-root <build> --structure-only --observe <edges.tsv> \
    --out <report.json> --targets <targets.tsv> --basis <basis.tsv>
```

`nominal.py` combines the existing dispatch proofs with `structure.py` bounds.
The basis file distinguishes `proof`, `nominal`, `structure` and
`structure+observed`. Outputs are candidates; installation into the game's
configured `indirect_targets` file is a separate step. The build fingerprints
that file. Per-function facts and preservation contracts are cached under
`<build>/typeinf`, keyed by executable, configuration and analysis sources.

Structural candidates are tables installed by constant vptr stores anywhere in
the image. A receiver requires its called slots and requirements propagated up
from direct callees. An observed target anchors compatible table prefixes from
slot zero through the called slot: known `RET n` cleanup must agree throughout
the prefix. Unknown cleanup and nonreturning methods impose no incompatibility;
nonreturning targets remain in the bound. An unlifted candidate leaves the site
open. Observed edges constrain compatibility; they are not an exhaustive target
list. Bounds excluding an observed target are rejected and reported.
Straight-line virtual JMP forwarders use the same receiver analysis and emit
`jump` bounds.
Their SSA view ends at the transfer without pushing a return address or
inferring cleanup or preservation. CW analysis removes conservative internal
successors only after byte-checked bounds identify function-entry targets.

This relies on the C++ assumptions that every runtime table is visibly installed
and overrides preserve argument cleanup across an interface. Register usage,
field extents and apparent return width do not establish incompatibility.
Requirements on an observed method's other arguments or result are not imposed
on competing implementations.

`alias.py` merges receiver types only with value-identity evidence. It follows
preserved registers and memory SSA through disjoint stores, private stack pushes
and callees proved to write only private stack memory. Receiver and vptr values
must both match. Combined call requirements are directional: an earlier call
must dominate the site borrowing its requirements. Unknown stores, callbacks,
dynamic control flow and open exception flow remain barriers. ABI/frame
isolation assumptions permit treating outgoing stack storage as private;
`--no-abi` disables these assumptions. `--explain-function HEX` reports identities,
proofs and barriers; `--explain-site HEX` explains a bound.

`--scope HEX --scope-depth N` limits fact collection for iteration. Scoped
structural bounds are reported but never emitted. Callers and separate reads of
a global are not pooled merely because their layouts or source address match.

Without `--structure-only`, `globals.py` and the nominal writer solver also run.
Configured allocator entries require byte-backed evidence. The global census
rejects unexplained address uses and nearby escaped/indexed addresses, with
`--escape-window` defaulting to `0x1000`. This window is a heuristic, not proof
against arbitrary indirect writes to a cell; reducing it weakens the premise.
Global writer inference remains experimental. Structural-only bounds do not
depend on this census.

## Shape research solver

```
tools/recomp/typeinf/run.py --game <id> --root ADDR [--root ...] [--vtable TABLE]
    [--depth 3] [--observe observe.txt] [--explain ADDR] [--no-abi] --out report.json
```

Seeds are the call closure of the roots: direct calls, observed indirect targets,
and the slot methods of an observed site's unique candidate table. `--explain`
prints every node of a function with a readable value description.
`--vtable` seeds every word of a table up to the next non-code word, which can run
into a neighbouring table.

## Model

- **Nodes** are `(function, SSA value)`, lifted by the kit's SSA. A **view** is a node
  plus an offset. Accesses and call operands are resolved through PHI leaves and
  preserved post-call reloads, so one object held in a register across calls is one
  node. Repeated loads of one field are distinct nodes linked by the same-field rule.
- **Fields** are `(node, offset)` with kinds `vtable`, `pointer`, `scalar`,
  `scalar_write`, `unknown`, `null`, `stack`, the access sizes, any vtable addresses
  written, and the pointee views of stored or loaded values. Frame-rooted accesses
  are excluded.
- **Requirements** `(requirer view, provider view)` say the requirer has every
  provider field, shifted by the view offsets. Pointer fields pair their pointees; a
  field with no pointee of its own references the provider's pointees.
- **Labels** are nominal interface names from D3D/D3DX import signatures: returned
  interfaces, `I*` arguments and `I**` out-parameters.

## Edges

- Calls: each caller pre-call GPR view requires the callee entry view. Post-call
  values require the callee's own exit views. An exit that returns an input
  (`return this`, a preserved register) links the caller's post value to its own
  pre-call value both ways instead, so callers never pool through a shared helper.
- Observed indirect targets are linked like direct calls, using the real pre-call
  registers.
- At an observed site `call [vptr+d]`, each aligned non-code word holding the target,
  minus `d`, is a candidate table whose words up to `d` are code. With exactly one
  candidate, the receiver's ECX requires the `this` view of every slot method up to
  `d`, and the vptr holder field records the table. Several candidates are only
  reported.
- Values read from or written to one field of one node are linked both ways.

## Registers and frames

Stack offsets relative to entry ESP are solved with a weighted union-find over the
entry, PHIs, post-call ESP (`before + 4 + callee RET n`), preserved EBP and the
balance at each return. A callee's `RET n` counts only when its own frame balances,
which rejects alloca-style helpers. Preservation is a least fixpoint over callers: a
register is preserved when every return yields its entry value, through preserved
callee reloads or a restore whose nearest store on every memory path is the entry
value. Registers a body never touches are preserved when all its callees preserve
them. Tail and import-tail exits preserve nothing.

Assumptions, both reported per run:

- callees do not write their caller's saved-register slots;
- indirect callees preserve EBX, ESI, EDI and EBP (`--no-abi` disables it).

An indirect call with no known cleanup leaves later ESP offsets unknown. Its
continuation loses restores and frame offsets; the call's inputs are unaffected.

## Conflicts

After the fixpoint, a field holding a vtable with a pointer or a written scalar, or
a pointer with a written scalar, is a conflict, as is a partially overlapping access.
Each kind and label carries its origin node and the edge that brought it.

## Gaps

- COM method typing: an interface-labelled receiver gives the method, its stdcall
  cleanup and its out-parameters. That fixes frames after COM calls and is the same
  fact the translator's call contracts lack.
- Stack locals as nodes, so out-parameters reach their reader.
- Bounding an indirect site needs the receiver's dynamic class: constructor writes
  flowing downward, and a closed census of every store to the holder field across
  the image. Non-virtual methods tie layout, not dynamic class.
- Scale: SSA is held for every seed and the solver slows past a few hundred
  functions. Whole images need per-function summaries built callees-first.
- Observation runs that log ECX and the vptr at each indirect call would give exact
  tables and same-object evidence instead of candidates.
