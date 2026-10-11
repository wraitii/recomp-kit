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
`[translate]`, also disable `private_leaf_contracts`, and regenerate to disable checked calls. Savings across the conversion call's
state transfer are piloted separately below; scratch memory remains translated.

The experimental integer-state pilot selects the reviewed callers
`0x0042c1b0` and `0x0042ce00` through
`state_contract_callers`. Their conversion edges require EAX, ESP and EBP on
normal continuation, with AF retained at sites where it remains
observable; EIP and all existing x87 state remain published. Private
entries omit other integer return stores and use byte-derived leaf input
contracts. Full changed state is recovered on an unexpected RET, and observation
boundaries retain the ordinary path. Set `state_contract_callers = []` and
disable `private_leaf_contracts` and regenerate to return to ordinary calls.
Automatic discovery extends these contracts to returning leaves with at least
two direct call sites and at most 64 original instructions per site, subject to
closed single-entry, replacement/SEH and emitter checks. One shared output layout
is emitted per callee; the report records automatic selections and rejections.
Non-leaf specialization and x87 value-transfer contracts remain future work.

Validation compared both private output sets and their unexpected-return
paths against the ordinary entry over 1,860 rounding cases each, with no new
differences. The ordinary entry still has the baseline's 96 original-byte
rounding discrepancies. Random-input diff coverage reached valid returns in
`0x005a10d0` and `0x0042c1b0`, including hook-enabled ordinary fallback, but not
`0x0042ce00`. Cleanup removes fallback parity reductions in the private leaf
assembly and reduces caller stack frames. Eligibility and checkpoint preparation
share one policy call; private entry thunks tail-call their bodies. Workload
measurement uses host sampling with `RECOMP_PROFILE` unset or `0`, since the
built-in guest profiler selects ordinary fallback. No workload gain is established.

Static control-word analysis proves the conversion leaf preserves its incoming
mode. Private preparation now requires `(CW & 0x0f00) == 0`, and each private
body emits only the fast arithmetic; other modes reuse the ordinary checked
entry. `x87_cw_clone = false` keeps x87 calls on general ordinary entries. In the linked
ARM64 diff harness, the two private bodies shrink from 4,680 to 1,120 bytes;
preparation grows by 12 bytes. The existing rounding fixture, routed through
preparation for private calls, matches the captured ordinary implementation in
9,300 cases across both output masks, fallback modes and unexpected returns,
including full x87 state and scratch memory. The ordinary original-byte fixture
retains its 96 inherited discrepancies; the random-input diff comparison passes
200 valid returns without discarded inputs.

The selected callers now share one private body with their union output mask
`0431` (`EAX`, `ESP`, `EBP`, `AF`), while keeping per-edge liveness and reloads.
Its linked ARM64 size is 564 bytes, removing the redundant 556-byte copy.
The shared layout passes 9,300 comparisons against the captured ordinary body,
including fallback modes, full x87 state, scratch memory and unexpected RETs.
Original-byte checks pass for the helper and one caller; the other caller is
unreached under random inputs. Ordinary entry and preparation policies are kept.

The code-map inventory has 58 functions containing 78 control-word writer sites.
Their direct-call descendants include 81 of 1,565 x87-using functions. This
narrows the exceptional-mode review; it does not establish a default at the
remaining entries. The generated contract report includes writer sites, exposure
closures, unknown boundaries and x87-only preservation coverage: 517 of 1,565
functions prove intrinsic precision/rounding preservation. Offline analysis with
selected boundary contracts raises that to 525; incoming constants remain separate. Local stack provenance
proves the original `__ftol` save/truncate/restore sequence; shared globals,
indirect calls and startup-to-frame propagation remain conservative.

The FEMMS regeneration changes 62 functions. Random-input original-byte checks
reach no comparable returns in those functions; instruction checks cover tag
clearing and control-word preservation. TOP follows AMD's documented reset,
where Unicorn's Athlon model differs; see [the IR guide](../../docs/ir.md).

Private continuation layouts retain modified registers absent from the caller's
SSA and values observed by later calls, even when no additional publication is
needed. The original-byte regression suite exercises eight cases, including
`005a76d8`'s EAX exponent result before CRT math calls and `0052ecd0`'s EDX
constructor result escaping through its caller. Run after rebuilding:

```sh
python tools/recomp/diff/run.py --game railroad-tycoon-3 --suite games/railroad-tycoon-3/validation/private-call-state.json
```

Selected production facts use import PC/RC preservation and the explicit,
verified cargo-entry premise in `game.toml`. `x87-boundaries.json` records the
byte-backed native/import boundaries; `x87-facts.json` covers 479 instruction
sites in 10 functions. It removes five ordinary arithmetic copies and six
private call-site CW checks while retaining other preparation and fallback
conditions. Exploratory roots and allocator/math assumptions remain offline.
Re-export facts with `tools/recomp/x87_audit.py propagation --facts-out` after
changing analysis dependencies; see [the contract guide](../../docs/contract-cleanup.md).
Future specialized calls will pass actual arguments/results instead of `X86*`.
