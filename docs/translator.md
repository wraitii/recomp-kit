# Rust translator

`translator/` is a Cargo workspace that translates a game image from its code map
into C. It replaces the Python engine described in [the IR](ir.md), which is
still the default. `tools/build.py --engine rust` (or `[translate] engine =
"rust"`) selects it. The concepts in that document (code map authority,
publication, lazy flags, call contracts, scalar x87, private contracts) carry
over. Module names do not, and the generated-code contract is the one in
[architecture](architecture.md). Each crate's `//!` doc states its own
responsibility.

## Pipeline

| Stage | Crate | Produces |
| --- | --- | --- |
| Settings | `rk-core` | `Settings` from `recomp-translate-input-v1` |
| Load | `rk-image` | `Program`: relocated PE, code-map functions, spans, tables, classified entries |
| Discover | `rk-discover` | Provisional functions and entries the map misses, merged into `Program` |
| Lift | `rk-frontend` (`rk-sleigh`, `rk-decode`) | One `FunctionIr` per function: p-code, CFG, tables, SEH, noreturn |
| Facts | `recomp-translate` (`facts.rs`) | Analysis facts checked against the lifted code: call and jump bounds, site and import effects |
| Global | `rk-global` | Call contracts, x87 control-word modes, private-contract targets |
| Plan | `rk-ir` → `rk-ssa` → `rk-plan` (+ `rk-x87`) | `PlannedFunction`: prepared SSA, publications, flag plan, x87 plan |
| Emit | `rk-emit` | C bodies and wrappers, or a trap body with a reason |
| Write | `rk-output` | The generated tree |

`rk-core` is the shared vocabulary. `rk-ir` is the lowering and the operation
set that SSA, planning and emission share. `rk-analysis` is a separate offline
binary that produces the facts the Facts stage reads (see below).

Plan and emit run in parallel per function, in two passes. The first pass emits
every function that neither calls a private-contract target nor is one. It
collects the field masks those calls need, `rk_global::specialize` merges them
into variant layouts, and the second pass emits the remaining functions and
their private variants. Bodies are sorted by address afterwards, so output does
not depend on scheduling. Unordered hash maps are banned by `clippy.toml`.

A function that cannot be lifted, planned or emitted becomes a
`recomp_unmodelled` trap body, and the report records it. Inside an emitted body,
unsupported instructions become named runtime traps. Errors in settings, inputs,
facts, global analysis or a private variant stop the translation.

## Inputs

`tools/build.py` resolves `game.toml` and writes `recomp/translate-input.json`
in the build root: `format`, `dir`, `exe`, `code_map`, `image_base`, `sha256`,
and the resolved `translate`, `globals`, `curated` and `aux_modules` tables.
`rk-core::settings` interprets them. `--module KEY` selects an auxiliary image
and drops the main-only options. The translator also reads:

- the executable and its `recomp-code-map-v3` directory. Both must match the
  configured SHA-256 and image base.
- `[translate] analysis_facts`, a `recomp-analysis-facts-v2` JSON produced by
  `rk-analysis`. The legacy alternative is `indirect_targets` (a
  `recomp-indirect-targets-v1` TSV) plus `call_effects`, and the two sources
  cannot be mixed. Every site is checked against the lifted code. Call and jump
  bounds feed discovery and global analysis, and effects replace the
  conservative contract at their sites.
- `[translate] discovery_evidence`, a TSV of extra discovery seeds.
- `[translate.native] header` and its includes, scanned for `FN_<addr>`
  replacements. Those functions are treated as opaque.

## Command and outputs

```sh
recomp-translate --input FILE --out DIR [--module KEY] [--report FILE] [--jobs N]
                 [--x87-mode auto|dynamic|fast] [--dump STAGE ...] [--only ADDR ...]
                 [--discovery-report FILE]
```

`build.py` builds the binary into `recomp/cargo`. It translates each image into a
staging directory (`aux-KEY/` for auxiliary images) and syncs the tree into
`recomp/gen`. The output tree contains `chunk_*.c` bodies (64 KiB address
buckets under a 4 MiB source budget), `chunk_entries_*.c` entry thunks,
`funcs.h`, `body.h`, `table.c`, `call_facts.c` and `symbols.json`. The report,
`recomp/translate[-KEY]-report.json`, gives counts, entry kinds, global-analysis
statistics, fact bounds and effects consumed, trap and fallback reasons, slow
operations, discovery traps, diagnostics and phase timings. `--discovery-report`
writes the discovered code map, instruction lengths and every rejected function
or entry with its pass and reason. `--dump program|fir|ssa|prepared|global|bodies|all`
prints intermediate stages to stdout, and `--only` restricts the dump to a set
of functions. The `ssa` and `prepared` dumps build SSA outside `rk-plan` and are
diagnostic only.

There is no emission cache or `--regen-function`. `build.py` fingerprints each
image from the input JSON, the translator binary, the code-map files and
executable, the fact, evidence and native-header files, and these environment
switches:

| Variable | Effect |
| --- | --- |
| `RK_NO_PRIVATE` | No private-contract targets |
| `RK_NO_DESCRIPTORS` | Publish flags eagerly instead of through lazy descriptors |
| `RK_NO_ROOTS` | Skip root coalescing in `rk-ssa` |
| `RK_X87_DYNAMIC` | Force dynamic x87 control-word clones, like `--x87-mode dynamic` |

## Analysis facts

`tools/build.py --produce-analysis-facts PATH` builds `rk-analysis` and runs it
on `recomp/analysis-input.json`, which is the translation input plus the
game's `[analysis]` table. That table names the method and effect catalogs,
observation TSVs (`--analysis-observation` adds more), effect families,
per-site overrides and the assumptions the analysis may make. The producer
reruns discovery and lifting, bounds indirect calls and jumps from tables,
SSA site shapes and stack-cleanup proofs, and joins them with the catalogs and
observations. It writes fresh facts that record their input digests, evidence
and assumptions, and it validates them with the same `rk_core::facts` parser
the translator uses. `--analysis-report` writes the selection report;
`--analysis-compare OLD` compares against older facts in that report without
merging them. The Python engine reads the same file through
`tools/recomp/ir/analysis_facts.py`.
