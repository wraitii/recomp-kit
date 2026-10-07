# d3d8-wgpu

Isolated Rust + wgpu prototype of a **D3D8-semantics renderer**. It exists to
answer one question independently of the game: can a small, reusable D3D8 host
layer (state, resources, fixed-function emulation,
rendering) be built on wgpu — and specifically on Metal on ARM64 macOS, where
the DXVK/MoltenVK experiment failed?

The crate exports a host-only C ABI used by recomp-kit. It contains no guest CPU
or COM types and no game addresses. C++ owns guest dispatch, interface identity,
reference counts and guest staging allocations; Rust owns CPU resource bytes,
mip layout calculations, indexed-draw preparation, render state and GPU work.
The native resource tests link the same Rust storage without creating a GPU.

- Lives in recomp-kit and is built by `dx/CMakeLists.txt` when `RECOMP_D3D8_WGPU` is on
  (or a game ships its own `graphics/d3d8-wgpu`). Game-specific usage inventories and
  execution evidence belong in the game repository, alongside its README and engine notes.
- Some draw paths cover only what a game has needed so far (for example the lit
  `0x112`/`0x152` FVFs); anything else fails by name rather than approximating.
- `reference/` is gitignored local material (Wine's D3D8 headers); the pinned copy the
  generators use is `third_party/wine-d3d8/`.

## Status

The bridge uses opaque Rust storage for texture levels and vertex/index buffers.
Compact triangle lists queue GPU indices, including programmable shaders with
stream-0 declaration layouts and padded strides. Fixed-function lit lists pack
only distinct referenced vertices for GPU lighting or CPU fallback and remap indices;
unused sparse gaps are never evaluated. Checked arithmetic validates actual
indices and base/start offsets rather than upload hints. Sparse unlit spans,
strips/fans and diagnostic draws retain expansion; `RECOMP_D3D8_EXPAND_INDICES=1`
forces it for comparisons. Queued vertex/index bytes and constants are immutable
snapshots. Shaders and supported render pipelines are cached per device.

ABI version 8 (including programmable shaders) is generated from Rust with cbindgen 0.29.4. COM IIDs, slots and
arities are generated from the pinned Wine header with a reviewed handler map.
Normal CMake builds regenerate the header and COM tables under
`build/d3d8-generated/`; generated code is not tracked. Install the header generator with
`cargo install cbindgen --version 0.29.4 --locked --root build/tools`.

The Metal headless probe passed clear/readback, transformed triangles and depth
occlusion on Apple M1 Max. Startup replay produced one 640x480 PNG, then failed
loudly at `IDirect3DDevice8::Reset`. This is bounded execution evidence, not game
playability or original-D3D8 equivalence. The current validation and exact kit
revision are recorded in the game repository's README.

## Layout

```text
src/d3d8/            reusable D3D8 layer (no windowing, no game types)
  enums.rs           D3DFORMAT / FVF / render-state / texture-stage / transform types
  math.rs            D3D row-major matrices and vectors
  format.rs          D3DFORMAT <-> wgpu::TextureFormat
  state.rs           render state, texture-stage state, transforms, viewport
  resource.rs        CPU storage, mip layouts, borrowed vertices, indexed preparation
  fixed_function.rs  FVF -> vertex layout and fixed-function WGSL
  device.rs          Clear / Draw* / Present / resource creation
src/backend/         wgpu instance / adapter / device / surface / readback
src/abi.rs           C ABI (version/status/diagnostics, adapter query, device ops)
src/probe/           shared probe harness and assertions
src/bin/             probe-headless (offscreen readback), probe-windowed (present)
```

## Probe milestones

1. Device/backend init on the real adapter; record backend, adapter, limits.
2. Exact full-target clear + readback.
3. Fixed-function FVF triangle + center-pixel color readback.
4. Present (windowed minimum; headless readback also supported).
5. Textures, then depth/stencil (`D3DFMT_D24S8` as a probe target).

Unsupported operations must fail with a named diagnostic, never silently succeed.

## Implementation handoff

- [Pinned D3D8 headers](../../third_party/wine-d3d8/README.md): unchanged Wine API references.

Shader-model 1.1 token translation and declarations are implemented alongside
the fixed-function stages. See [shader support and validation](SHADERS.md) for
the supported instruction slice and remaining fidelity gaps.

From the repository root:

```sh
python3 tools/test.py --d3d8-wgpu test
python3 tools/test.py --d3d8-wgpu probe
```

The probes exit 0 only when every requested check passed, 1 for a pixel/report
failure, or 2 for unavailable/unsupported operations. A missing requested check
or an unsupported result cannot count as a pass. Cargo outputs stay under the
ignored build tree.

Draw diagnostics: `RECOMP_D3D8_TRACE_DRAWS=<n>` prints up to `n` draws with
both stage bindings (texture id or unbound), `RECOMP_D3D8_TRACE_DRAWS_START=<n>`
skips that many draws first, and `RECOMP_D3D8_TRACE_STAGE1=1` restricts the trace
to draws with a stage-1 texture bound.

## Supported slice and remaining work

Full-target color/depth/stencil clears, material/light storage, render/texture-stage
state, world/view/projection transforms, viewport, scene boundaries, and unlit
triangle-list draws with FVF 0x42/0x142/0x242 are supported. `D3DPT_POINTLIST`
is also supported through wgpu's fixed one-pixel `PointList`, but only in D3D8's
default point state: a non-default `D3DRS_POINTSIZE`, `D3DRS_POINTSCALEENABLE`
or `D3DRS_POINTSPRITEENABLE` fails by name. FVFs 0x112
(XYZ/NORMAL/TEX1) and 0x152 (XYZ/NORMAL/DIFFUSE/TEX1) use GPU
diffuse/ambient/emissive vertex lighting (directional/point/spot), material
sources and inverse-transpose normals, with floating diffuse passed to the
texture/fog/alpha/depth raster path; 0x112 has no COLOR1, so the material
supplies the diffuse. Both indexed and nonindexed draws use this path,
including fixed-function vertices paired with pixel shaders. Immutable per-draw
uniforms retain light/material state and precompute light transforms and cone
cosines. `RECOMP_D3D8_CPU_LIGHTING=1` forces the CPU reference. Unsafe positional
attenuation, nonfinite state/positional inputs and `ProcessVertices` retain CPU
processing and its diagnostics. Specular draws remain unsupported.

`DIVERGENCE(original):` GPU rounding, normalization and `pow` can differ from
original D3D8 hardware and the CPU evaluator. Metal synthetic comparisons allow
at most two 8-bit channel levels per pixel; they cover both FVFs, both index
widths, sparse/repeated indices, nonzero offsets, directional/point/spot/mixed
lights, material sources, normal normalization, lighting-disabled output,
textures, pixel shaders, fog, alpha testing and queued snapshots. No original
D3D8 visual equivalence or gameplay performance gain is established.

D16/D24X8/D24S8/D32 map
onto wgpu depth formats; the backend-defined D24 precision remains a fidelity
caveat. Draws reject unsupported reached state by name.

CPU textures keep native texel layouts and stage locks through guest memory.
GPU sampling and indexed triangle lists use immutable draw snapshots, with
expansion available for other supported topologies and diagnostic comparisons.
Resource pool/usage,
LOD/priority, COM identity and binding bookkeeping still live in the bridge.
The optional no-Rust kit profile retains CPU storage in C++ and advertises no
renderer. Headless present completes the offscreen frame; the guest bridge
reads it back and presents through the host display seam.

Specular lighting, other FVFs, line and triangle-strip/fan topologies and resource lock
flags remain incomplete or unsupported. The light-index range is currently bounded to eight slots, an
implementation limit that must not be confused with D3D8's active-light capacity.
Guest COM execution, GPU probes and original-D3D8 comparisons are different
levels of evidence; the latter remain outstanding. Half-pixel raster coverage,
numerical precision and repeated-frame game behavior remain open.

### Render-target textures

Level-0 DEFAULT-pool A8R8G8B8/X8R8G8B8 color surfaces can be bound with the
implicit depth surface or with depth detached. Setting a color surface resets
the viewport; depth-only changes keep it. A padded color attachment permits a
smaller color target to share the original larger depth surface; viewport-limited
clears/draws preserve depth outside that region. GPU copies publish the logical
color region after each write, including target switches within an open scene.
The implicit backbuffer remains the presentation/readback source.

Rendered levels are owned until their surface is destroyed, independently of
the CPU upload LRU. Sampling unchanged CPU generations reuses GPU-written pixels;
CPU generation changes upload new bytes. CPU staging reads synchronize from GPU
only when its generation is current. The bridge keys storage by level-surface
identity so releasing a parent texture cannot invalidate a still-bound surface.
Other color formats, mip levels and nonimplicit depth surfaces are unsupported;
read/write texture feedback fails by name.

The headless probe's `render texture/depth/restoration/readback` check covers
rendering, same-scene restoration, shared-depth preservation, sampling after
upload-cache eviction, CPU writes/readback generation ordering and depth detach.
This is GPU/API-model validation; original D3D8 differential equivalence remains
unmeasured.
