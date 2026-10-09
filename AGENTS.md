# Working on recomp-kit

The kit is the translator, guest runtime, DirectX/Win32 shims and native hosts.
Games live in their own repositories and pull the kit in as a submodule; their
installations, analysis and builds stay in ignored `original/`, `analysis/` and
`build/`. `docs/code-guide.md` maps behavior to source; `docs/architecture.md`
explains guest memory, threads and frames.

## Ground rules

- Do not write tests unless asked. Check translator changes with the diff harness below.
- Avoid comments; code should read clearly without them.
- Before adding code or documentation, check whether it can be shorter or folded
  into an existing section.
- The repository has legacy; existing patterns are not authoritative.

## Invariants

- Guest addresses are 32-bit values accessed through the memory helpers, never
  host pointers. Guest memory changes only on the scheduler baton holder; the
  presenter only sees sealed frames.
- Nothing in `runtime/`, `dx/`, `host/`, `platform/` or `mods/` names a game.
  Game addresses go in `game.toml` (`[hooks]`, `[translate]`) and code reaches
  them through generated `RECOMP_HOOK_*` macros.
- Operating-system calls go through `platform/os.h`; platform `#ifdef`s live only
  in `os_posix.cpp` and `os_win32.cpp`.
- Never edit generated code under `build/recomp/gen/`. Fix the translator and
  rebuild with `--regenerate`.
- Unknown imports, COM methods and unsupported state fail with a named
  diagnostic rather than a silent stub.
- Build only through `tools/build.py`. Format native code with
  `tools/format.py --write`; leave `third_party/` as vendored.
- Never commit game files, generated code, binaries or logs.

## Diff harness

`tools/recomp/diff/run.py --game-dir <game>` runs translated functions from the
last build and the original bytes under Unicorn on the same random registers,
arguments, scratch memory and uninitialized globals, then compares registers,
flags, x87 state and writable memory. Inputs where the original faults, calls an
import or runs too long are discarded. Select functions with `--func ADDR`,
`--sample N`, `--all`, or `--changed` (generated C differs from the previous run).

For a translator change: run once before (it records generated bodies), change
and `--regenerate`, then run `--changed`. `--fpu-cw` fixes the initial x87
control word; divergences only under `007f` usually come from binary32 x87
arithmetic. The report is in `<build>/recomp/diff/report.json`.
