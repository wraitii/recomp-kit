# x87 local-value experiment

Run through the build wrapper (a game repository's wrapper works too):

```sh
python tools/build.py --x87-locals-experiment
```

No game files are needed. Uses the host CMake compiler at `-O2
-ffp-contract=off`, without LTO or fast-math. Outputs are in the selected game's
`build/x87-locals-experiment/` (the kit's build root for its stub game).
This is an opt-in research tool; the production translator does not import it.
The experiment is currently intended for Clang/GCC host builds, not cross builds.

`run.py` parses synthetic instruction listings through the production parser.
The baseline directly calls `Translator.emit_x87`. Three other emitters track
stack slots using C temporaries and defer final stack/tag materialization:

- **full:** retains every arithmetic `fx87` call, store conversion and final
  state field, including values and integer metadata in popped slots.
- **live:** also retains arithmetic semantics, but discards the values and
  integer bits in popped slots. This is a deliberately weaker contract.
- **relaxed:** uses the live contract and also omits `fx87`, using ordinary
  double arithmetic. This drops precision-control rounding, NaN canonicalization
  and invalid-status updates. Differences are counted, not accepted as passes.

There are three fixtures: a dot product plus offset stored to binary32, the same
calculation with an x87 result left live, and stores followed by potentially
aliasing reloads. Only self-contained straight-line FLD, FMUL, FADD, FSUB,
implicit FADDP and FSTP with ordinary binary32 memory operands are supported.
Other instructions, incoming-stack dependencies and local stack overflow fail.
No register arguments, types, function boundaries or control flow are inferred.

The C harness tests 24,576 inputs per fixture, with all eight entry TOPs and
all sixteen PC/RC bit combinations (including reserved PC=01, for runtime
regression only). Inputs include signed zero, subnormals, infinities, NaNs,
arbitrary binary32 bits and ordinary finite values. Destinations include aliases
and unaligned addresses. Each comparison checks the entire CPU struct and
256-byte scratch region, normalizing empty register contents only for live and
relaxed. The CPU object must be outside guest memory. Dirty tracking is inactive.

**These are normal-exit comparisons against the existing double-backed runtime.**
There is no original-x86 oracle, fault replay, callback or intermediate-state
observer. In particular, deferred state is not recoverable at an interior fault.
Existing precision, rounding and exception limitations remain. Passing here does
not authorize production use. Synthetic listing addresses are identifiers, not
encoded instruction boundaries or original executable bytes.

Timings use process CPU time, nine rotating-order trials of one million calls
per variant, finite inputs, PC=00/10 and RC=nearest. Functions are compiled
separately from the harness, preventing constant folding of the workload across
calls. TOP is restored each iteration, including for the live-result fixture.
Results include the call, restoration and loop overhead. These short hot fragments
do not represent cache behavior, call boundaries or whole-game performance.

Artifacts: `generated.c`, `results.txt`, `assembly-counts.json`, executable and
CMake build products. Clang's `-save-temps=obj` also retains the optimized `.s`
and LLVM `.bc` actually used by the build. Assembly counts include cold paths;
they are static counts, not executed instruction counts. No direct LLVM backend
is implemented or benchmarked by this probe.
