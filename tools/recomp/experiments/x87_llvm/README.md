# LLVM x87 stack-to-SSA pass

An opt-in, native-host experiment using LLVM IR as the only optimization IR:

```text
instruction listing → semantic LLVM calls → recomp-x87-stack
                    → link runtime helper bitcode → always-inline + default<O2>
                    → native object
```

Run with LLVM **22** development tools installed:

```sh
LLVM_CONFIG=/path/to/llvm-config python tools/build.py --x87-llvm-experiment
```

The game repository's wrapper works too. `llvm-config` is discovered from PATH
or the standard ARM Homebrew LLVM location when `LLVM_CONFIG` is unset. The
plugin, optimizer, linker and Clang all come from that one installation. CMake
builds everything; no game files are needed and the game build is unaffected.

## Representation and passes

`run.py` emits LLVM directly from the production instruction parser. `rk_push`,
`rk_read`, `rk_set` and `rk_pop` are ordinary LLVM declarations with a private
semantic contract. Arithmetic is ordinary double `fadd`/`fsub`/`fmul`, with an
explicit `rk_round` after each arithmetic instruction. Guest addresses remain
wrapping i32. No fast-math, overflow flags, inbounds GEPs or guessed alias
annotations are emitted.

`stack_pass.cpp` is a real new-pass-manager plugin, invoked by
`opt -passes=recomp-x87-stack -verify-each`. It validates a single-block region,
then replaces logical x87 accesses with LLVM SSA values and emits final-state
materialization. It rejects unknown calls, direct memory instructions, CFGs,
incoming stack dependencies, overflow beyond eight local slots and arithmetic
relaxations. The pass consumes its region attribute, making reapplication an
identity. It must run before helper definitions are linked.

`helpers.c` keeps the native X86 struct layout and the existing numeric semantics
in one place. Its bitcode is linked after lifting. LLVM's always-inliner and O2
pipeline then perform ordinary expression simplification and dead-store cleanup.
There is no additional general-purpose IR and no new floating-point model.

## Contract and proof argument

The pass preserves the **current runtime's complete final state on normal exit**
under these preconditions:

- The private helper declarations have exactly the supplied semantics. Guest
  memory is mapped ordinary storage, separate from the X86 object and runtime
  globals. Guest input/output aliasing with each other is permitted.
- Entry TOP is in 0..7. No operation consumes a value below the region's local
  stack, and its maximum depth is eight. Incoming physical contents may be
  arbitrary; overwritten slots and untouched slots remain distinguishable.
- No interior observer, callback, fault, signal handler or host floating-point
  trap observes CPU state. The host numeric environment is the same as the
  baseline's ordinary C arithmetic. CW is stable; this subset cannot change it.

Let T be entry TOP, d the current local depth, and L[k] the last value written
at depth k+1. Physical slot k is P(k)=(T-k-1)&7. For k=0..7 these are distinct.
Maintain the invariant that baseline TOP=(T-d)&7, live ST(i)=L[d-1-i], and
L[k] also records the baseline's retained value in any popped, touched slot.

1. Push stores its unchanged value in L[d] and increases d. That is exactly
   predecrementing TOP and writing P(d) in the baseline.
2. Read ST(i) substitutes L[d-1-i]. Arithmetic and `rk_round` remain in place
   with identical operands and ordering, including the status-word side effect.
3. Set ST(i) updates L[d-1-i]. Pop decreases d without destroying L, matching
   retained physical contents in the runtime.
4. At return, write each touched L[k] to P(k), zero its integer bits/exact marker,
   mark it live iff k<d, and classify live values using the unchanged helper.
   A touched slot's last assignment was push/set, which zeroes those integer
   fields; a pop only clears exact and marks empty. Untouched slots stay intact.
5. Loads, stores, register reads, arithmetic and rounding calls retain their
   order. They cannot observe deferred stack fields under the preconditions.
   Therefore final memory, status and all other CPU fields agree as well.

This is an inductive proof argument for the rewrite, **not a machine-checked
proof of the implementation, LLVM optimizer or original x87 semantics**. No
floating-point algebraic identities or precision relaxations are required.
The double-backed runtime's existing approximations remain. Interior fault
state is deliberately outside this contract; enabling this in the game would
need a stronger observation contract or an explicitly accepted relaxation.

## Checks and artifacts

The existing C experiment harness is reused, with four variants compiled by the
same Clang: baseline C, raw LLVM, lifted LLVM, local-value C. All compare complete
CPU state and scratch memory; none normalizes popped contents. Four synthetic
fixtures cover stored/live dot products, aliasing stores/reloads, and full
8-slot stack wraparound. Each runs 24,576 inputs over TOP/PC/RC combinations,
exceptional floats and ordinary finite values. These are regression evidence,
not an original-x86 oracle. The nine rejection fixtures and pass idempotence are
also checked through the compiled plugin. Portable Python tests check frontend
scope and preservation of arithmetic operations.

Artifacts under `build/x87-llvm-experiment/`:

- `input.ll`: explicit machine operations, both raw and transformable variants.
- `lifted.ll`: inspect this to review the stack-to-SSA transformation.
- `helpers.bc`, `linked.bc`, `optimized.ll`: runtime lowering and standard passes.
- `llvm.s`, `llvm.o`, `baseline.c`, `results.txt`, and the test executable.

Timings use nine rotating trials of a million calls, process CPU time, PC=00/10,
nearest rounding and ordinary finite inputs. They include loop/call/TOP-reset
costs and do not predict whole-game performance. No full game function or CFG
is translated by this experiment yet.

LLVM's [pass plugin documentation](https://llvm.org/docs/WritingAnLLVMNewPMPass.html)
describes the plugin interface used here.
