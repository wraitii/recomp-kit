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

`d3d8_surface_tests` (`nogame`) exercises the backbuffer descriptor, texture
levels and staging, vtable calling convention, identity, bounds and
device/factory ownership through guest COM dispatch. Its devices are
constructed state fixtures; it does not create a GPU or demonstrate game
rendering. When a game provides the optional D3D8 Rust crate, this same suite
uses its real CPU storage and mip-layout ABI; standalone kit profiles exercise
the C++ fallback. Live locked resources are also exercised across generation
reset.
`d3d7_mipmap_tests` (`gpu`) follows DirectDraw implicit mip attachments and
per-level locks through D3D7 texture binding and real GPU readback. It checks
capability reporting, point/linear mip filtering, fractional LOD bias,
MIPFILTER=NONE and updates to a child without rebinding the root. `dx_tests`
covers rectangular mip dimensions/counts, attachment traversal, independent
storage, one-level requests and partial allocation rollback without a GPU.
`d3d8_abi_cpp_check` and a C compilation fixture check the generated header's
sizes and offsets when the Rust crate is present. `--unsupported-lock` and
`--unsupported-texture` are child probes expected to abort with named
diagnostics. When running `runtime_tests --startup-contracts` directly, set
`RECOMP_PYTHON` to the absolute Python interpreter containing `pefile`.

`tools/recomp/diff/run.py` compares translated game functions with Unicorn; see
`AGENTS.md`. The function corpus (`tools/build.py --function-corpus`) compares the
eager and SSA translations of real game functions; see its
[README](../tools/recomp/corpus/README.md).

## Media probes

`RECOMP_TEST_AUDIO=/path/to/track.ogg` and `RECOMP_TEST_AVI=/path/to/movie.avi`
exercise the Vorbis and AVI/Indeo decoders in the native suites.
