# Railroad Tycoon 3

The GOG `RT3.exe` identified by [game.toml](game.toml) is the behavioral reference. The packed [code map](metadata/) contains 6,039 functions across 6,760 spans and 510,070 instruction boundaries, without executable bytes, instruction text or analysis names. The private executable supplies those bytes during translation.

Ghidra research, export scripts and engine notes remain in the [Railroad Tycoon 3 workbench](../../../). To refresh the map, export `recomp-code-map-v3-export` from the matching executable into a fresh directory, then run:

```sh
python tools/recomp/code_map.py --pack /path/to/v3-export --exe /path/to/RT3.exe --out games/railroad-tycoon-3/metadata
```

The map pins the executable SHA-256 and preserves disconnected function bodies and exceptional instruction boundaries. It also records Ghidra jump targets, referenced interior entries and non-returning functions/calls. Verify every boundary repair against original bytes before exporting.

Unsupported vector instructions emit named traps at their original addresses; the previous 52 `skip_functions` entries are no longer needed. The kit now lowers `movups xmm3, xmmword ptr [ecx]` at the former first stop, `0x00581699`; `mulps xmm2, xmm3` at `0x005816a8` still traps. Remaining SSE/3DNow gaps still trap; CPUID feature advertisement is unchanged.

The checked-return pilot selects `0x005a10d0` (through `0x005a1144`), the x87
integer-conversion helper. Original bytes show a single-entry leaf with aligned
stack scratch, path-dependent ECX/flags and one RET; callers include `0x0042c1b0`
and `0x0042ce00`. Eligible SSA direct calls pass their decoded continuation and
skip general return lookup on a match. Stack writes, x87 effects and all existing
state publication remain translated. Decoded callers, indirect calls, callbacks,
hooks and profiling retain the ordinary entry. Set `checked_returns = []` under
`[translate]` and regenerate to disable the pilot. Savings across the conversion call's
state transfer are piloted separately below; scratch memory remains translated.

The integer-state pilot selects callers `0x0042c1b0` and `0x0042ce00` through
`state_contract_callers`. Their conversion edges require EAX, ESP and EBP on
normal continuation, with EDX, ECX and AF retained at sites where they remain
observable; EIP and all existing x87 state remain published. Private
entries omit other integer return stores and use byte-derived leaf input
contracts. Full changed state is recovered on an unexpected RET, and observation
boundaries retain the ordinary path. Set `state_contract_callers = []` and
regenerate to disable this stage independently. Expansion and x87 contracts
await correctness coverage and a workload profile showing a gain.

Validation compared all three private output sets and their unexpected-return
paths against the ordinary entry over 1,860 rounding cases each, with no new
differences. The ordinary entry still has the baseline's 96 original-byte
rounding discrepancies. Random-input diff coverage reached valid returns in
`0x005a10d0` and `0x0042c1b0`, but not `0x0042ce00`.
