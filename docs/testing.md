# Testing

| Command | Checks | Game needed |
| --- | --- | --- |
| `tools/test.py --native` | Native CTest suites (runtime, DirectX, Metal, controls) | `game`-labelled suites only |
| `tools/test.py --compile-only` | Native test binaries build | No |
| `tools/test.py --mods` | Mod loader, hooks and replay against the translated archive | Yes |
| `tools/test.py --d3d8-wgpu test\|probe` | Rust D3D8 renderer tests or offscreen probe | Yes, plus a GPU |
| `tools/format.py` | Native code formatting | No |

CTest labels: `nogame`, `game`, `gpu`, `device`, `mods`. Run a subset with
`ctest --test-dir <build root>/cmake/macos -L nogame` or `-R dx_tests`.

## Translator checks

`tools/recomp/diff/run.py` compares translated game functions with Unicorn; see
`AGENTS.md`. The function corpus (`tools/build.py --function-corpus`) compares the
eager and SSA translations of real game functions; see its
[README](../tools/recomp/corpus/README.md).

## Media probes

`RECOMP_TEST_AUDIO=/path/to/track.ogg` and `RECOMP_TEST_AVI=/path/to/movie.avi`
exercise the Vorbis and AVI/Indeo decoders in the native suites.
