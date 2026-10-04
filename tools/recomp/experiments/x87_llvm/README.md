# LLVM x87 stack-to-SSA passes

An opt-in LLVM **22** native-host experiment. Production C translation is unchanged.
LLVM IR remains the optimization representation throughout:

```text
verified decoded instructions → semantic LLVM CFG
  → recomp-x87-analyze → recomp-x87-ssa → recomp-x87-materialize
  → link runtime helper bitcode → always-inline + default<O2> → native code
```

`recomp-x87-stack` is shorthand for the three compiler passes. Run through the
build wrapper, which uses CMake and one LLVM installation for Clang, opt, linking
and the plugin:

```sh
LLVM_CONFIG=/path/to/llvm-config python tools/build.py --x87-llvm-experiment
# Optional game-owned complete-function evidence and harness profile:
python tools/build.py --x87-llvm-experiment --x87-llvm-function /path/to/profile.json
```

Discovery uses PATH or the standard ARM Homebrew installation when LLVM_CONFIG
is unset. The default synthetic tests require no game files. A profile supplies
`exe` (relative to the profile), `sha256`, `entry` (hex string), `size`,
`function_sha256`, and `fixture` (relative C harness header). It must name a
contiguous function extent supported by inspected disassembly and CFG evidence.
The frontend checks executable and function hashes and complete byte decoding;
it does not guess boundaries. Game addresses, constants and scenarios stay in
the game repository. Generated listings, LLVM, C and binaries stay in build/.

## Pass contracts

### 1. Machine-operation and CFG emission

`run.py` retains the original straight-line arithmetic fixtures. `function.py`
decodes the verified function bytes through the production decoder and emits
basic blocks, conditional/unconditional branches and explicit guest operations.
It supports float/double memory FLD/FCOMP, FCHS, FNSTSW AX, TEST AH/immediate,
MOV reg32/immediate, XOR EAX/EAX, JZ/JNZ (including JE/JNE), JMP and plain RET.
Unsupported instructions, operands, external branch targets and extent
fallthrough fail. This intentionally small set covers the first real function.
The C reference uses the production instruction emitter with all flags live.

**Preconditions:** correct entry/extent and decoded instructions; mapped ordinary
guest memory separate from X86/runtime storage. No interior faults or host FP
traps. **Invariant:** each emitted instruction performs the same ordered state
transition as the current C runtime and reaches the same successor. Register and
address operations retain i32 widths; RET reads EIP and adjusts guest ESP before
calling the existing return dispatcher. TEST/XOR update the defined flags, leaving
AF as the runtime does. FCOMP uses `fcom`, not an inferred source-level comparison.
FCHS is fneg plus fset, without introducing a rounding operation.

No fast-math, overflow flags, inbounds GEPs or guessed alias annotations are used.
Existing arithmetic fixtures retain each `rk_round` call and ordinary double
operation. No numeric identities or precision changes are introduced.

### 2. Stack-shape analysis (`recomp-x87-analyze`)

`X87Analysis` validates the complete function before mutation: void(ptr) ABI,
closed helper declarations with exact signatures, allowed instructions, no
relaxation flags, and a reachable acyclic CFG. It propagates local depth `d` and
touched-position mask `M` in topological order. Every incoming edge at a join
must have identical `(d,M)`. Cycles, unreachable blocks, incoming x87 value
consumption, depth outside 0..8, and incompatible joins are rejected.

**Preconditions:** private helper ABI, TOP in 0..7, stable CW, no implicit x87
observers/mutators. **Invariant:** `(d,M)` is the same on every path to a block;
all reads/sets use locally defined values. Analysis does not mutate IR. Equal
masks are conservative: a path that leaves a slot untouched cannot join a path
that overwrites it, even if later code would overwrite it on both paths. We do
not synthesize incoming tag/exact-integer metadata PHIs in this milestone.

### 3. Value flow and PHIs (`recomp-x87-ssa`)

Let T be entry TOP and P(k)=(T-k-1)&7. Keep last-written value L[k] for each touched
position, including popped values. The invariant is baseline TOP=(T-d)&7 and
live ST(i)=L[d-1-i]. Popped L[k] still equals that physical slot's contents.
Untouched slots retain their entry contents and metadata.

**Preconditions:** successful shape analysis; helper implementations are linked
only after lifting. **Preservation argument:** push writes L[d] and increments d;
read substitutes L[d-1-i]; set updates that value; pop decrements d without erasing
L. At a single-predecessor block, inherit L. At a join, a double LLVM PHI for each
touched position selects the predecessor's L, including dead stack contents.
Compatible depths make ST indexing identical on every incoming edge. Induction
over the topological order extends the straight-line invariant to every path.

Numeric, memory, register and branch operations remain in place with unchanged
operands after substitution. The pass adds `rk_snapshot` state descriptions at
explicit observers and exits, consumes `recomp.x87.region`, and produces
`recomp.x87.ssa`. The intermediate `ssa.ll` exposes the PHIs and snapshots.

### 4. State materialization (`recomp-x87-materialize`)

**Preconditions:** snapshots produced by pass 3. For each touched k, write L[k]
to P(k), zero its integer bits/exact marker, and classify it live iff k<d; set
TOP=(T-d)&7. Untouched fields stay intact. Each touched slot was assigned by
push/set, which clears integer metadata; pop clears its tag/exact marker without
erasing its contents. Thus these writes reconstruct complete current-runtime
x87 state, not merely the live stack. The pass consumes the SSA attribute.

**Preservation invariant:** complete CPU state matches at each snapshot and final
return; between snapshots the deferred state is represented by `(T,d,M,L)`.
Integer registers/flags, x87 status and guest memory are updated in place by
unchanged operations. The observation policy is explicit:

- `rk_fnstsw`: materialize before reading status/TOP and writing AX. Its harness
  hook `rk_observe` captures complete CPU state before the read. It may not mutate
  guest state. Conservative full materialization also makes this a regression
  for future read-only observers, although FNSTSW itself only needs status/TOP.
- `rk_observe`: full materialization before a read-only observer; SSA remains
  valid afterwards. Unknown calls and mutating observers are rejected.
- `rk_ret`: materialize before popping the guest return address and invoking the
  return dispatcher. No deferred use follows it; it must immediately precede
  LLVM return. A bare LLVM return also materializes (fragment fixtures).

Guest-memory reads/writes are **not** fault checkpoints. Callbacks, signal
handlers, traps, dirty/watch hooks and arbitrary interior fault observations are
excluded. Guest input/output aliasing with each other is allowed. The harness
only accepts one ordinary caller continuation; callback/tail/unknown dispatch
aborts. This is a stub-dependent return environment, not live game execution.

These are preservation arguments for the bounded transformation, **not a
machine-checked proof of its implementation, LLVM or original x87**. Arithmetic
remains the double-backed runtime's approximation. Enabling this in production
would require a stronger fault/observer contract and more evidence.

## Validation and artifacts

The existing local-value C harness compares complete CPU state and scratch memory
for baseline C, raw LLVM and lifted LLVM. Its fourth variant is local-value C for
the original four fragments, a repeated baseline for the new CFG regression and
complete-function replay. No empty register contents are normalized away.

Five synthetic fixtures run 24,576 inputs each: stored/live dot products,
aliasing stores/reloads, eight-slot wraparound, and a diamond with separate PHIs
for live and popped slots, an observer and an aliasing output store. The optional
profile reuses this harness with game-owned setup, boundary checks and timings.
Thirteen compiled rejection cases cover incoming dependencies, invalid registers,
unknown calls, direct memory, numeric relaxations, cycles, unreachable blocks,
and unequal depth/touched masks at joins. Separate-stage postconditions, LLVM
verification and idempotence are checked. Python tests check frontend scope and
preserved machine operations; these are regression evidence, not an x86 oracle.

Artifacts under `build/x87-llvm-experiment/`:

- `input.ll`, `ssa.ll`, `lifted.ll`: emission, PHIs/snapshots, materialization.
- `helpers.bc`, `linked.bc`, `optimized.ll`: lowering and ordinary LLVM passes.
- `llvm.s`, `llvm.o`, `baseline.c`, `results.txt`: native code and synthetic results.
- Optional `function/{decoded.txt,baseline.c,fixtures.h}` and `function-results.txt`.

Timings use nine rotating trials of a million calls, process CPU time, PC=00/10,
nearest rounding. They include call/reset/loop and observer-hook overhead. A
small function's improvement does not establish a game-level speedup, original
x86 equivalence, or a reason to expand the production backend yet.
