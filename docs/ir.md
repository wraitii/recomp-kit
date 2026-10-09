# Translator and instruction IR

Translation reads the Ghidra code map and the executable, lifts each mapped
function with SLEIGH, builds an integer SSA with scalar x87 tracking and emits C.
Game measurements and the optimization history live in the game repository.

```
code map v3 + PE -> program.py -> ir/lift.py (+ x87.py) -> ir/cfg.py -> ir/ssa.py
  -> ir/emit_c.py -> output.py (chunks, funcs.h, table.c, symbols.json, report)
```

`tools/recomp/driver.py` is the command (`--game DIR --out DIR`, `--report`,
`--jobs`, `--allow-unmodelled REASON`); `build.py` runs it. `settings.py` turns
`game.toml [translate]` and the command line into one `Settings` object that is
passed to the program model, the workers and the corpus runner.

## Program model

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

Raw SLEIGH is not faithful to the runtime's x87 (FIST/FISTP, FRNDINT, FPREM,
FCOM/FNCLEX status, FXAM, tags). `x87.py` corrects these by lowering operands,
recovered from `Insn.raw`, to ordered `X87_MEM`/`X87_REG` effects that call the
audited runtime helpers (`fx87`, `fdivz`, `fset`, exact-FILD metadata, FIST and
FRNDINT rounding, status merging, FXAM, partial FPREM). They share the memory
token with guest accesses. FS/GS and 16-bit addressing, FCOMI/FCMOV,
transcendentals and environment operations stay named fallbacks.

`cfg.py` holds `FunctionIR` (instructions, successors, table sites, external
exits, noreturn calls, SEH effects, entries) and the call-graph SCC order.

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
predecessor published it).

`emit_c.py` lowers values to unsigned, width-masked C with staged parallel phi
copies, sign-bias comparisons and saturating shift counts. Dword DIV/IDIV are
checked `div32`/`idiv32` effects that reach the runtime error seam with the
original address. Memory ADD/SUB/INC/DEC capture one read and emit flags after
the STORE; `MOVSD`/`REP MOVSD` call runtime helpers in access-then-advance
order. Direct calls publish the CPU, call `entry_ADDR` (`CALL_FN`) and reload
every tracked lane, flag and the memory token; indirect calls go through
`recomp_call`. Jump tables and tail transfers inside or outside the body,
alternate entries (`body_X(c, entry)` plus `fn_E` wrappers), noreturn calls and
SEH frame effects (enter/adopt/leave/orphan, ordered, with full state
published) are emitted; after `setjmp` returns nonzero only `c` is used.

`production.py` runs one function through SSA with a 16384-instruction budget.
Bodies SSA cannot emit stay on `decoded.py`, the plain eager emitter (Capstone
decoding, full-state helpers) fed the mapped boundaries: about 140 bodies,
named in `translate-report.json` (direct-ram read-modify-write, INT, RDTSC,
SHLD/SHRD, some register shifts, FNSAVE/FNSTENV, guest continuations,
over-budget bodies). `decoded.py` also emits the wrappers of internal-switch
entries. Unmodelled instructions become `recomp_unmodelled(c, addr)` traps only
under `--allow-unmodelled`; otherwise translation fails.

`output.py` writes address-bucketed body chunks (2 MiB budget), entry thunks,
`funcs.h`, `table.c`, `symbols.json` and the report (counts, entry kinds, SSA
versus decoded reasons, unmodelled, seconds). Workers lift and emit one function
at a time from the image bytes; dispatch checks, call returns and entry sets
come from the program model, never from generated C.

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
CW and SW are scalar locals passed to always-inlined `_sw` helper forms. Seams
(division, calls, opaque recipes, returns) materialize the required state.
`x87_carry.py` carries unpublished state across internal CFG edges: each block
has a conservative fixed-point shape, predecessors write canonical function-scope
slots, successors copy them, and emission raises `SSAError` (whole-function
decoded fallback) on drift. Carry needs the MSVC convention.

Under PC=00, proven-binary32 operations use native float arithmetic plus the
runtime's NaN/status normalization once a linear run has two arithmetic effects.
A body with at least four PC/RC-sensitive operations in one tracker window is
emitted twice (`x87_cw_clone`): each activation tests `(cw & 0xf00) == 0` and
otherwise jumps to the same activation in the general clone; past the guard the
fast clone folds CW to 0. It is exact. Multi-window bodies are not cloned.

Lazy NaN keeps basic-arithmetic results in full precision and folds
(`isnan`/indefinite) where the value stops flowing into NaN-propagating
arithmetic: stores, comparisons, FCHS/FABS, FNSTSW, FSTCW/FLDCW, opaque
fallbacks and any flush. IE is sticky, so only when the fold is computed
differs. It is off under `fault_state = "exact"`.

### Lazy flags

`X86` carries a descriptor (`cc_op`, `cc_size`, `cc_mask`, `cc_a`, `cc_b`,
`cc_res`) for the last ADD/SUB/CMP/logic/INC/DEC; `cc_op == X86_CC_NONE` means
the six flag fields are current. `x86_cc_settle` writes them; `x86_get_eflags`,
`x86_set_eflags`, `x86_sahf`, `recomp_comis`, the interpreter and `recomp_call`
settle first. `ir/flag_region.py` walks from the entry or a post-call point to
the next CALL/CALLIND/RET: with no flag read and no flag write the settle is
removed; with no read and all six flags written on every path it becomes
`x86_cc_drop`; otherwise it stays, as it does for paths leaving the body without
a call or return, for calls that can continue elsewhere (SEH adoption, noreturn,
setjmp), and where hooks may observe (entry thunks and `recomp_jump` settle
before a hook). Decoded bodies use an explicit per-mnemonic flag-effect table.

### Call contracts

`call_contracts.py` summarizes every body (decoded included) over the eight GPRs
and six flags: `reads` is a backward may-liveness of entry values (through
direct callees), `kills` a forward must-definite over all return paths (a GPR
only if all four byte lanes are written). A save/restore stays in `reads`.
Recursive components start empty and fall back to `reads=all, kills=none` if
unconverged. At a direct CALL the emitter drops field `F` only when
`F not in reads and F in kills`; ESP and EBP are never dropped. Indirect calls,
unbound targets, failed lifts, SEH and alternate-entry bodies and native
replacements are `reads=all, kills=none`. Because any function can be hooked,
dropped fields are still published behind `recomp_hooks_ever`.

### Policies

| `[translate]` key | Default | Effect |
| --- | --- | --- |
| `fault_state` | `"relaxed"` | `"exact"`: strict x87 (publish before loads and stores, general arithmetic recipes), every pre-access GPR/flag snapshot, no lazy flags or NaN. Relaxed defers GPR/flag publication at loads and stores, keeping EIP/ESP/EBP |
| `msvc_x87_convention` | `true` | Every register above TOP is tagged empty, so popped residue is not published at calls and returns. Flags stay exact: CRT helpers return and consume flags. FINCSTP/FDECSTP bodies keep the exact flush. `false` publishes complete x87 state |
| `x87_cw_clone` | `true` | `false` emits only the general body |
| `call_contracts` | `true` | `false` restores full publication at calls |

A `RECOMP_NULL_CHECKS=1` build exposes CPU state to fault dispatch: translate it
with `fault_state = "exact"` and `msvc_x87_convention = false`.

DIVERGENCE(original) tags in the emitter name the accepted differences:
`[ssa-x87-scalar]` interior faults and store-watch callbacks may see the
preceding published x87 state; `[ssa-state-locals]` and `[ssa-call-contracts]`
let a fault or SEH context raised before a dropped field is overwritten, or
between an elided settle and its flag read, see stale fields;
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
