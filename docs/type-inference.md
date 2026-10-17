# Type inference

`tools/recomp/typeinf/` infers object shapes from original bytes: struct fields,
pointer nesting, vtable holders and interface labels. Its outputs are research
facts for Ghidra and, once a site can be bounded, for translator call facts.
Ghidra annotations are never an input.

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
