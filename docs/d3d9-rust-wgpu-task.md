# D3D9/D3DX migration to Rust on wgpu

Temporary task document, started 2026-10-11 at kit `199830e0`. Delete it once
M13 is done; move lasting API and build facts into the existing kit docs and
keep consumer evidence in the consumer's repository.

## Goal

Replace the legacy C++/CPU D3D9 and D3DX implementation with a reusable Rust
implementation on the shared wgpu facilities, behind a thin C++ guest ABI
layer. Civ4 is the first consumer and sets the initial scope (D1), but the
implementation follows the published API so later consumers extend it rather
than work around Civ4-shaped shortcuts.

Non-goals: hardening or repairing the legacy backend first (its code is
research input; port useful parts where D1 wants them), and promising full
D3D9/D3DX conformance.

Terms:

- **Legacy bytecode**: the D3D9 shader token stream (`vs_1_1` to `ps_3_0`)
  that games pass to `CreateVertexShader`/`CreatePixelShader`, optionally with
  a CTAB constant table. It is not the DXBC/DXIL format of D3D10 and later.
- **Effects**: the D3DX effect framework (`ID3DXEffect`). An `.fx` source file,
  or its compiled `.fxo` form, bundles shaders, parameters, annotations,
  techniques and passes that set render state, plus preshaders (CPU-side
  expressions evaluated on parameters). Civ4 renders through compiled effects.

## Architecture

Follow the D3D7/D3D8 CXX migration (kit commit `f90bba7`):

- **C++** keeps x86 marshalling, guest memory checks and staging, COM identity
  and reference counts, filesystem/include callbacks, and HRESULT mapping. It
  holds no duplicate API state.
- **Rust** owns API state, validation, resource storage and layouts, formats,
  shader/effect behavior, draw preparation and rendering. It is reached through
  generated CXX: opaque `rust::Box` holders plus typed descriptors and results.
  The D3D8 pattern is `Device`, `Device7State`, `Storage` and `Resource` in
  `graphics/d3d8-wgpu/src/bridge.rs`, held by the `D3dHost*` helpers in
  `dx/d3d_bridge_helpers.h`, with rendering in `bridge_render.rs`.
- Borrowed `rust::Slice` lives for one synchronous call; anything needed later
  is copied or retained by Rust. Guest addresses and x86 objects never cross.
  Queued work owns its bytes and resources; guest memory is touched only on the
  scheduler baton holder and the presenter receives sealed frames.
- D3D8 is an architectural template only. Its SM1.1 support is a partial slice
  (`graphics/d3d8-wgpu/SHADERS.md`); D3D9 must not inherit its limits, caps or
  defaults. D3D7/D3D8 behavior must be preserved.
- Reuse backend and presentation facilities; renaming the crate is not
  required.

Object ownership rules live in `docs/architecture.md` under "Threads and
ownership". Rust owns device, resource and pipeline state through CXX; C++ retains COM
identities and guest staging. Shader execution and D3DX objects remain M5–M9.

## Current status

- **Rust D3D9: M1–M4 done within the contracts below.** Device and resource
  state, descriptors, layouts, bytes, locks, dirty regions, priority/LOD and
  byte private data live in Rust. C++ retains COM identities/refcounts,
  factory/device/texture parents and per-subresource guest staging. Buffers,
  standalone surfaces and 2D/cube/volume textures now have Rust resource paths;
  texture level surfaces and volumes share the parent's storage and retain its
  identity without a COM reference cycle. Upload/readback use wgpu-owned
  resources and synchronous copies of borrowed data. M2 device/presentation
  is implemented within the M2 contract below: factory
  enumeration now resolves the virtual desktop with adapter validation, and
  Reset/Present/TestCooperativeLevel, implicit back/depth targets, GetBackBuffer/
  GetRenderTarget/GetDepthStencilSurface/GetDisplayMode/GetNumberOfSwapChains,
  adapter-fact caps and Check* passed the flag-on windowed build/signing.
  A unified Reset transaction stages new implicit targets and every live
  holder's GPU storage from committed CPU bytes, confirms readiness, and only
  then installs handles and moves the device. Ordinary Reset keeps the healthy
  wgpu context and rebuilds managed residency; physical loss stages a compatible
  fresh context with an isolated loss token and replaces the old one. Pipeline
  state, shader/declaration bytes and state blocks now live in Rust (M4).
  M5–M9 remain pending; state storage does not imply drawing support. Civ4's
  `[render] d3d9_wgpu = true` stays on during active migration.
- **ABI foundation: done.** Pinned Wine headers and IIDs
  (`third_party/wine-d3d9/`), generated D3D9 constants, version-specific D3DX
  tables, and the 25 `d3dx9_33` imports Civ4 uses (3 reuse existing matrix
  handlers, 22 fail by name). `D9_STUB`/`RES_STUB`/`FX_STUB` are gone; missing
  shader/declaration draws and query failures fail by name. Coverage lives in
  `dx/d3d9_bindings.json`, `dx/d3dx9_bindings.json`,
  `dx/d3dx9_33_bindings.json` and `dx/d3dx9_imports.json`. The authoritative
  D3D9 flag-on status map is `dx/d3d9_bindings.json`: `flag_on` marks each
  callable method as `rust` or `native`; every omitted method fails by name.
  The COM generator derives dispatch from those same records.
- The full M3 resource implementation passed supervisor review and a flag-on
  macOS windowed build/signing. The first compile exposed Rust/CXX integration
  errors; those were fixed and the resumed final build passed. Native/Rust
  formatting, generated COM/status staleness and whitespace checks passed.
  No tests, app, headless or GPU runs were performed.

Handoff: M4 is complete within its state-storage contract below; M5 is next.
Rust owns state, declaration/shader bytes and state-block recording/snapshots;
C++ retains COM identities from Rust binding ids without device/child cycles.
Relevant files: `graphics/d3d8-wgpu/src/{bridge.rs,d3d9.rs,d3d9/{shader.rs,state.rs}}`
and `dx/{d3d9.cpp,com.h,d3d_bridge_helpers.h,d3d9_bindings.json}`. Pi implementation
and independent review, supervisor corrections, native/Rust formatting, generated
dispatch staleness, repository boundaries and whitespace checks passed. The final
flag-on `tools/.venv/bin/python tools/build.py --target app` and signing passed
after correcting CXX declarations/imports, a Rust map-key type and the C++
shader identity map. No tests, app, headless or GPU runs; runtime behavior remains
unverified. Explicit deferrals are in M4 below. The Civ4 flag stays on.

M4 primary evidence: Microsoft `Reset`, `GetFunction`, `D3DSTATEBLOCKTYPE`, the
three "Saving ... States With a StateBlock" pages and the `Capture` per-entry
example; [SetLight](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-setlight),
[GetLight](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-getlight),
[LightEnable](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-lightenable),
[GetLightEnable](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-getlightenable),
[MultiplyTransform](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-multiplytransform),
[SetViewport](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-setviewport),
[D3DVIEWPORT9](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dviewport9),
[D3DTEXTURESTAGESTATETYPE](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dtexturestagestatetype),
[D3DSAMPLERSTATETYPE](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dsamplerstatetype),
[D3DTEXTUREOP](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dtextureop),
[texture blending](https://learn.microsoft.com/en-us/windows/win32/direct3d9/texture-blending),
`D3DTRANSFORMSTATETYPE` and `D3DMATERIAL9`/`D3DLIGHT9`; pinned Wine 11.0
`d3d9.h`/`d3d9types.h`/`d3d9caps.h`; Wine `dlls/d3d9/stateblock.c`,
`dlls/wined3d/stateblock.c` and `dlls/wined3d/shader_sm1.c` (pin
`db11d0fe6a169c457e23d007e20404643d067aa8`).

## Evidence rules

| Source | Answers | Does not answer |
| --- | --- | --- |
| Microsoft [D3D9](https://learn.microsoft.com/en-us/windows/win32/direct3d9/dx9-graphics-reference-d3d) / [D3DX9](https://learn.microsoft.com/en-us/windows/win32/direct3d9/dx9-graphics-reference-d3dx) reference | Semantics, errors, lifecycle | Kit scope |
| Pinned Wine headers | Declarations, layouts, slots, IIDs | Semantics |
| Current code, binding JSON | What is implemented and wired | Correctness, intended architecture, API scope |
| Original binaries (Ghidra, verified against bytes) | What a consumer calls and branches on | General API coverage, whether a path runs |
| Parsed consumer assets | Container structure, concrete inputs | Semantic support, which assets are used |
| Dependency docs and source | Claimed interfaces and restrictions | Fit with D1 without investigation |
| Approved runs and comparisons | The exact cases, settings and platforms run | Anything else |

Parsing is not support, a build is not correctness, and an unrun path is not
broken. Asset or import counts do not justify a dependency.

Buffer-lock evidence: Microsoft [vertex-buffer Lock](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3dvertexbuffer9-lock),
[index-buffer Lock](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3dindexbuffer9-lock),
[D3DLOCK](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dlock) and
[locking resources](https://learn.microsoft.com/en-us/windows/win32/direct3d9/locking-resources)
define byte ranges, matched outstanding lock counts, READONLY and the dynamic
usage requirement for DISCARD/NOOVERWRITE. NO_DIRTY_UPDATE is valid for buffers;
system-memory writeback does not imply a managed dirty-region update. DONOTWAIT
is a surface flag. Header declarations and flag values use the existing Wine
11.0 pin `db11d0fe6a169c457e23d007e20404643d067aa8` in
`third_party/wine-d3d9/{d3d9.h,d3d9types.h}`. No index-element alignment rule is
inferred from the buffer format.

## Decisions

D1, D2 and D4–D7 were agreed on 2026-10-12; D3 is open. Record later changes
here with rationale and rejected alternatives, and discuss substantive
tradeoffs in chat first.

**D1. Support contract.** Base D3D9 (no D3D9Ex), `d3dx9_33` (no `_26`),
vertex and pixel shader models 1.1–2.0. SM3 is deferred, not excluded: nothing
in the design should prevent it. The M3 format and resource contract below fixes the initial format set from
Civ4's assets and resource calls plus common D3D9 neighbours.
Platform: the native macOS build (`dx/CMakeLists.txt` rejects cross-compiling
and mismatched architectures); other targets are unverified. Acceptance
consumer: Civ4. Everything outside this contract fails by name.

Shaders always reach the device as legacy bytecode. A game either ships that
bytecode precompiled or produces it at runtime from HLSL or assembly text.
Running it (D2) and producing it (D3, D4) are separate decisions:

```
HLSL source --D3 compile--+
                          +--> legacy bytecode --D2 translate--> WGSL --> wgpu
asm text ----D4 assemble--+          ^
                                     +-- precompiled (shipped files, effects)
```

**M3 resource contract.** Vertex/index buffers, standalone render-target,
depth-stencil and offscreen surfaces, and 2D/cube/volume textures use
DEFAULT/MANAGED/SYSTEMMEM/SCRATCH where the published resource API permits the
pool/usage combination. SYSTEMMEM/SCRATCH have CPU storage; DEFAULT/MANAGED
have GPU storage. Buffers retain FVF and valid usage metadata; texture layouts
include all requested mips, cube faces and volume slices. Texture/surface/volume
locks are single per subresource; buffers permit matched outstanding locks.
CPU packed layouts are preserved on lock and readback; expanded GPU formats
use explicit packing/conversion. Packed color formats expanded to RGBA8 retain
round-trip packed storage; normalized sampling precision still needs M11 review.
Queued work owns its bytes and GPU handles.

| D1 format family | Formats |
| --- | --- |
| Color | R8G8B8, A8R8G8B8, X8R8G8B8, R5G6B5, X1R5G5B5, A1R5G5B5, A4R4G4B4, R3G3B2, A8, A8R3G3B2, X4R4G4B4, A2B10G10R10, A8B8G8R8, X8B8G8R8, G16R16, A2R10G10B10, A16B16G16R16 |
| Luminance | L8, A8L8, A4L4 |
| Block compression | DXT1–DXT5, retained compressed on GPU, including cube/volume layouts |
| Signed vectors | V8U8, Q8W8V8U8, V16U16, Q16W16V16U16 |
| Floating point | R16F, G16R16F, A16B16G16R16F, R32F, G32R32F, A32B32G32R32F |
| Depth/stencil | D16, D16_LOCKABLE, D24X8, D24S8, D32F_LOCKABLE |
| Buffers | VERTEXDATA, INDEX16, INDEX32 |

Format availability and render/filter usages follow requested wgpu features and
format capabilities. Autogen supports point/linear filtering where the format
allows it; only the top level is guest-visible. The documented unsupported-format
fallback creates one level; M2's `CheckDeviceFormat` must report
`D3DOK_NOAUTOGEN` for that case. Non-lockable depth has no raw readback promise;
lockable depth uses actual GPU readback. Integer-normalized D32 has no faithful
wgpu depth peer and fails by name; it is distinct from D32F_LOCKABLE.

Explicit deferrals: MSAA (M2/M6), additional swap chains (M2), device copy/draw/query methods (M6), palette/YUV/packed bump
and other unlisted formats,
shared handles, GDI `GetDC`/`ReleaseDC` and `D3DSPD_IUNKNOWN` private data.
The last three are Windows/COM interop, outside this initial storage contract.
D3DX resource loading/conversion remains M7. No silent stub implements these.

M3 evidence: pinned Wine 11.0 headers define descriptors and slot/field layouts;
Microsoft [D3DPOOL](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dpool),
[D3DUSAGE](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dusage),
[CreateTexture](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-createtexture)
and [automatic mip generation](https://learn.microsoft.com/en-us/windows/win32/direct3d9/automatic-generation-of-mipmaps)
define pool/usage and mip behavior. Implementation references are pinned local
`wgpu-types 27.0.1`, `wgpu-core 27.0.3` and `wgpu-hal 27.0.4` source:
`features.rs` (BC/BC_SLICED_3D/16-bit normalized formats),
`metal/adapter.rs:952–953` (BC3D feature), and `command/transfer.rs` (depth-copy
and row-alignment restrictions). D3D8 constructors retain their existing feature
requests; the D3D9 constructor requests its own supported features.

**M2 device and presentation contract.** One implicit swap chain (index 0) per
windowed HAL device. Rust validates `D3DPRESENT_PARAMETERS` for create and
reset: single back buffer, `D3DSWAPEFFECT_DISCARD`, default interval, no
multisample, no presentation flags; `D3DFMT_UNKNOWN` resolves from the desktop
format; zero dimensions resolve from the device/focus window before the call.
Rust owns the effective parameters and a shared loss/reset-required token
carried by the device context and every resource's GPU clone, so a failed Reset
stops further GPU submissions without being classified as physical loss or
consuming a replacement. `Reset` rejects any still-live explicit
`D3DPOOL_DEFAULT` resource and any outstanding external implicit-surface
reference (reported by C++ as a request flag) without invalidating or
resurrecting either; a failed Reset latches reset-required and is cleared only
after every holder is committed. `Reset` runs one transaction for both cases:
it stages new implicit targets and every live resource's GPU storage from
committed CPU bytes without touching the live holders, validates holder pools
against the registration, confirms readiness (staged set, capabilities, a queue
flush and candidate-loss polls before and after), then installs resource
handles and moves the device in infallible, allocation-free steps. Ordinary
Reset keeps the healthy wgpu device (D7) and clears reset-required after
install, so managed GPU residency is rebuilt from the system-memory copy;
physical loss stages a compatible fresh context and swaps to it, and the
replacement's callback captures an isolated loss token. Managed and
SYSTEMMEM/SCRATCH CPU bytes and metadata survive; while blocked, a create
request validates its original usage/pool/format and keeps its pool metadata
but allocates CPU storage only (no GPU submission), so explicit
`D3DPOOL_DEFAULT` objects remain registered and releaseable and block Reset
until the guest releases them; implicit targets are runtime-owned. A candidate
that is lost at readiness aborts with the old device and holders untouched; one
lost after readiness commits a coherent replacement and reports failure without
mixed old/new resources. Caps and
`Check*` report only the implemented subset; `Check*` return
`D3DERR_NOTAVAILABLE` for unsupported queried capability without aborting.
C++ owns COM identity/refcounts and the implicit-surface weak cache, with the
D3D8 ref-hook device-retention pattern and no cycle. Explicit deferrals:
fullscreen, additional swap chains and swap-chain COM, multiple back buffers,
COPY/FLIP swap effects, MSAA, non-default intervals/presentation flags,
Present rectangles/window overrides/dirty regions and implicit-surface locks.
These executing paths fail by name. Native GPU present handoff is an
optimization deferral; the owned CPU snapshot reaches the existing presenter.
`D3DUSAGE_QUERY_*` capability bits and `CheckDeviceFormatConversion` report
`D3DERR_NOTAVAILABLE` while copy/conversion is unimplemented; no identity copy
is claimed.

M2 primary evidence: Microsoft
[Reset](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-reset),
[Present](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-present),
[TestCooperativeLevel](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-testcooperativelevel),
[lost devices](https://learn.microsoft.com/en-us/windows/win32/direct3d9/lost-devices),
[D3DPRESENT_PARAMETERS](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dpresent-parameters),
[D3DCAPS9](https://learn.microsoft.com/en-us/windows/win32/direct3d9/d3dcaps9),
[CheckDeviceType](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-checkdevicetype),
[CheckDeviceFormat](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-checkdeviceformat),
[CheckDeviceMultiSampleType](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-checkdevicemultisampletype),
[CheckDepthStencilMatch](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-checkdepthstencilmatch),
[CheckDeviceFormatConversion](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-checkdeviceformatconversion),
[GetDeviceCaps](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-getdevicecaps),
[GetAdapterCount](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-getadaptercount),
[GetAdapterModeCount](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-getadaptermodecount),
[EnumAdapterModes](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-enumadaptermodes),
[GetAdapterDisplayMode](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-getadapterdisplaymode),
[GetAdapterIdentifier](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-getadapteridentifier),
[GetAdapterMonitor](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3d9-getadaptermonitor),
[GetDisplayMode](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-getdisplaymode),
[GetBackBuffer](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-getbackbuffer),
[GetRenderTarget](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-getrendertarget),
[GetDepthStencilSurface](https://learn.microsoft.com/en-us/windows/win32/api/d3d9/nf-d3d9-idirect3ddevice9-getdepthstencilsurface);
pinned Wine 11.0 `d3d9types.h`/`d3d9caps.h`/`d3d9.h` declarations; and wgpu 27.0.1
`Device::set_device_lost_callback` / `DeviceLostReason` behavior, `poll`
(`PollType::Poll`, `PollError` limited to `Timeout`/`WrongSubmissionIndex`) and
wgpu-core 27.0.3 `device/resource.rs` `lose()` (immediate callback) plus
`device/queue.rs` poll discovery. Thread identity uses the recomp runtime's
guest scalar `guest_current_thread_id()`.

**D2. Running legacy bytecode: extend the kit's Rust translator.** The D3D8
crate already translates SM1.1 to WGSL (`graphics/d3d8-wgpu/src/d3d8/shader.rs`).
Extend it to D1's models (adding `ps_1_4`, `vs_2_0`, `ps_2_0`), with the legacy
C++ decoder (`dx/d3d9_shader.cpp`) and Wine's `dlls/wined3d/shader_sm1.c` as
references. The same decoder serves bytecode reflection
(`D3DXGetShaderConstantTable`, `D3DXGetShaderInputSemantics`,
`D3DXGetShaderVersion`). Rejected: vkd3d-shader, which adds a C dependency
under LGPL and emits Vulkan-oriented SPIR-V whose acceptance by naga on Metal is
unverified.

**D3. Compiling HLSL to legacy bytecode: open.** Covers `D3DXCompileShader*`
and compiling `.fx` source. Normal Civ4 play loads compiled effects only (see
Civ4 evidence), so it may not be needed. It would be needed for source effects
dropped into `Shaders/FXO` (`D3DXCreateEffectFromFileA` compiles text input),
edited effects picked up by hot-loading, or the device-less effect compiler
path. Candidates: vkd3d-shader's HLSL compiler or a custom compiler. Until
decided, these entry points fail by name.

**D4. Assembling shader text: deferred.** `D3DXAssembleShader*` is reached only
through Gamebryo's shader-program files, which Civ4 does not ship. Fails by
name. Reference if needed later: Wine `dlls/d3dcompiler_43/asmparser.c`.

**D5. Effects runtime: kit-owned Rust.** No dependency fits: Wine's d3dx9
effect code is C, LGPL and built on Wine interfaces, so it is a reference only.
The runtime loads compiled effects, exposes parameters and annotations,
evaluates preshaders, shares parameters through pools, and applies techniques
and passes as device state with save/restore. The legacy `dx/d3dx9.cpp` is
research input only: it carries a CPU effect runtime with parameter, preshader
and render-state handling, whose behavior is not a correctness reference.
Source effects depend on D3. Technique choice and fallback are game behavior,
not kit policy.

**D6. Images: `ddsfile` and `image`.** Both MIT/Apache. DXT data stays
compressed on the GPU (wgpu `TEXTURE_COMPRESSION_BC`, available on desktop GPUs
and Apple Silicon). `bcdec_rs` is added only where the CPU must see
decompressed texels: locks, readback, `D3DXLoadSurfaceFromSurface` conversion
and saving. D3DX filter fidelity is settled per function in M7.

**D7. GPU device ownership: one context per D3D9 device.** A game uses D3D8 or
D3D9, not both, so sharing the D3D8 `GpuContext` gains nothing. Each D3D9
device owns its wgpu device and presentation and reuses the shared backend
code. Ordinary `Reset` recreates the swap chain and rebuilds managed GPU
residency from the system-memory CPU copy while keeping the wgpu device. A
physically lost wgpu device is the one exception: it cannot be retained, so
`Reset` acquires a fresh compatible context, restages targets and managed
storage, and atomically replaces the old one with an isolated loss token. The
two paths are distinct: D3D9 `D3DERR_DEVICENOTRESET` covers both, but only
physical loss consumes a replacement context.
Multiple devices and additional swap chains fail by name until a consumer needs
them.

## Milestones

The kit flag stays off by default until M13; Civ4 keeps it on during active
migration per the user's 2026-10-12 instruction. A milestone is done when its
"Done when" holds within D1; anything outside D1 fails by name. M2–M4 can
proceed in parallel once M1 exists.

- [x] **M1. D3D9 boundary.** Rust D3D9 device and resource objects behind CXX,
  following the D3D8 pattern. Done when: `D9_CreateDevice` with the flag on
  creates a Rust device holding its own wgpu context (D7); C++ holds only COM
  identity, refcounts and guest marshalling; the guest/Rust ownership of every
  D3D9 object type is written down.
- [x] **M2. Device and presentation.** Needs M1. Done when: creation and
  `Reset` validate presentation parameters; back buffers, `Present`, caps and
  `Check*` answers reflect what is actually implemented; reset and device loss
  recreate default-pool resources and keep managed ones. Implemented subset:
  the M2 contract's shared validator, Rust-owned implicit back/depth targets,
  `Reset`/`Present`/`TestCooperativeLevel`,
  `GetBackBuffer`/`GetRenderTarget`/`GetDepthStencilSurface`/`GetDisplayMode`/
  `GetNumberOfSwapChains`, adapter-fact caps and `Check*`, factory enumeration
  with adapter validation and the D3D9 ref-hook implicit-surface lifetime.
  Both Reset paths stage implicit targets and managed residency before
  committing; healthy Reset retains its context, physical loss replaces it.
  CPU pools, locks/staging, resource identities and metadata survive; explicit
  DEFAULT resources must be released and recreated by the guest. Done within
  the M2 contract and named deferrals above. Supervisor review and the flag-on
  windowed build/signing passed; runtime GPU behavior remains unverified.
- [x] **M3. Resources.** Needs M1. Done when: vertex and index buffers,
  surfaces, and 2D, cube and volume textures support D1 pools, usages, formats,
  pitches, mips and lock flags, with callable upload/readback and owned GPU
  submissions. Done within the M3 resource contract above: all creation,
  descriptor, level-view and lock APIs are guest-callable through CXX; Rust owns
  bytes, validation and residency; C++ owns COM identities, retained parents and
  guest staging. The listed interop, MSAA and later-milestone deferrals fail by
  name. The D1 format list is fixed above.
- [x] **M4. Pipeline state.** Needs M1. Done when: vertex declarations and FVF,
  shader objects with bytecode retrieval, float/int/bool constants, render,
  texture-stage and sampler state, state blocks and all getters are stored and
  validated in Rust. Done within the state-storage contract: callable declaration
  creation/FVF synthesis and cached identities, shader Create/Set/Get/GetFunction
  for VS1.1/2.0 and PS1.1–1.4/2.0, F/I/B constants, render/texture-stage/sampler
  setters/getters, transforms/MultiplyTransform, viewport/scissor, material/lights,
  clip planes, texture/stream/index bindings and ordinary stream frequency 1.
  ALL/PIXELSTATE/VERTEXSTATE and custom blocks retain per-entry snapshots;
  recording does not mutate live state. Rust validates enums, layouts, binding
  kind/pool and ranges; C++ checks guest memory and COM/device identity. Successful
  Reset clears bindings/default state at commit, preserves shader/declaration
  objects, and rejects live state blocks. GetFunction/GetDeclaration support size
  queries and reject short outputs. Caps report 16 streams/pixel sampler slots,
  eight fixed-function stages/textures, 256 vertex float constants, eight active
  lights and six clip planes; execution capability bits remain deferred.
  Named deferrals: SM3/translation (M5), target selection/draws/queries (M6),
  instancing and stream dividers above 1, vertex/displacement samplers, non-default
  declaration methods/tessellation, palettes, software VP, clip status and
  ValidateDevice. Pixel filter kernels/anisotropy remain M11; state storage
  accepts the pinned D3D filter enum and anisotropy 1–16. Viewport extents are
  target-bounded; finite depth values outside 0..1 remain stored, following
  pinned Wine device behavior. LightEnable beyond the eight-active contract
  returns INVALIDCALL. Supervisor review and the windowed build/signing passed;
  execution and runtime fidelity remain unverified.
- [ ] **M5. Shader translation.** Needs M4, D2. Done when: every shader in
  Civ4's compiled effects (`vs_1_1`, `vs_2_0`, `ps_1_1`, `ps_1_4`, `ps_2_0`)
  translates to WGSL that naga validates, and other versions fail by name at
  creation.
- [ ] **M6. Drawing.** Needs M2–M5. Done when: all draw calls, including
  user-pointer and indexed variants, render through fixed-function or
  translated shader stages from immutable state snapshots; clears, copies,
  readback, `Present` and event/occlusion queries work with correct `S_FALSE`
  and data-size behavior. No skipped draws or fabricated query results.
- [ ] **M7. D3DX textures.** Needs M3, D6. Done when: Civ4's texture
  imports (file and memory loaders, image info, `D3DXLoadSurfaceFromSurface`,
  save to file) handle DDS/DXT, TGA, PNG, JPG and BMP into D1 formats, with
  mips, cube faces, volume slices, color keys and filtering.
- [ ] **M8. D3DX shader helpers and math.** Needs M5. Done when: constant
  tables, input semantics, shader version and vertex/pixel profile queries
  (matching M2's caps) work, and matrix helpers handle aliasing and singular
  inputs. Compile and assemble keep failing by name per D3/D4.
- [ ] **M9. D3DX effects.** Needs M4, M8, D5. Done when: all 14 Civ4 compiled
  effects load, expose parameters, annotations and techniques, evaluate their
  preshaders, share pools, and apply passes through the Rust device state with
  `Begin`/`End` save/restore and lost/reset handling.
- [ ] **M10. First frame.** Needs the subset of M2–M9 Civ4's startup uses.
  Done when: a reviewed build with the flag on reaches Civ4's main menu in an
  approved run, with known gaps listed here.
- [ ] **M11. Raster breadth.** Needs M6. Done when: viewport and half-pixel
  offset, culling, alpha test, fog, blending including separate alpha, depth,
  stencil, scissor, color masks, depth bias, texture addressing, filtering, LOD
  and sRGB match D3D9 semantics, with precision limits documented and caps
  matching.
- [ ] **M12. Acceptance.** Needs M2–M11. Done when: approved Civ4 runs covering
  menus, a game map, leaderheads and effects-heavy scenes (water, terrain,
  bloom) match the legacy renderer or reference captures at the same settings,
  survive reset, and have recorded performance; D3D7/D3D8 consumers are
  unaffected.
- [ ] **M13. Switch.** Needs M12. Done when: the Rust path is the default,
  superseded C++ D3D9/D3DX code is removed, lasting facts are moved into kit
  docs, and this file is deleted.

## Working rules

Both `AGENTS.md` files apply. In particular: no tests, and no app, GPU or
headless runs without separate user approval, including for M10 and M12. Build
with `tools/.venv/bin/python tools/build.py --target app` (`--regenerate` when
translation inputs change) and format with the kit formatter. Verify Ghidra
identity before edits. Keep assets, dumps and logs ignored; commit only when
asked.

## Civ4 consumer evidence

Static only; nothing has been run. In the consumer's ignored `analysis/`:
`d3d9-inventory.json` (identity, imports, call sites), `fxo-inventory.json`
(parsed effects), `d3d9-resource-evidence.json` (DDS headers, masks and resource
calls) and Ghidra dumps. The resource scan found 4,031 loose DDS files across
BTS/base/Warlords asset roots and BTS mods: 1,326 DXT1, 1,957 DXT3, 476 DXT5,
and 272 uncompressed. Masks include RGB555 (`0x7c00/0x03e0/0x001f`), not RGB565.
No cube/volume DDS appeared in that scan; archives and runtime asset use are
unverified. `0x9b3e80` creates DEFAULT render-target textures;
`0x9a6c6e` creates a depth surface; `0x9b0d1a`/`0x9b30ea` create dynamic DEFAULT
buffers; `0x9c5be0`/`0x9c5c70`/`0x9c5ce0` request managed D3DX textures. Chosen
render-target/depth formats remain descriptor-sourced and unverified; the format
classifier `0x9ae2f0` proves recognition, not use.

- 25 `d3dx9_33` imports with 49 static call sites; `Direct3DCreate9` only, no
  Ex.
- All 14 compiled effects in `Shaders/FXO` parse: `vs_1_1`, `vs_2_0`,
  `ps_1_1`, `ps_1_4`, `ps_2_0`, preshaders in 11 of them, NULL and mixed
  stages.
- Effect loading: `0x456880` loads the shader library from `Shaders\FXO` and
  registers `Shaders/FX` only as a hot-load watch directory (`0x699a80`,
  `FindFirstChangeNotificationA`). Effects are created by `0x9c3520` →
  `0x9c4ae0`: with the renderer's device it calls `D3DXCreateEffectFromFileA`;
  without one it uses `D3DXCreateEffectCompilerFromFileA` (`0x9c4830`).
  Whether the device-less path or hot-loading runs in normal play is unverified.
- `Shaders/FX` ships sources for the 14 effects plus 6 without compiled
  versions (Decal, PeakHill, PerPixelLighting, Route, TreeShader,
  ComputeCiv4Lighting), and two `.hlsl` include files. No `.vsh`, `.psh` or
  Gamebryo shader-program files ship.
- Shipped images include DDS/DXT, TGA, PNG, JPG and BMP; which are used is
  unknown.
