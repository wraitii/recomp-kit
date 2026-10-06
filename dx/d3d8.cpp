// Bounded D3D8 guest COM bridge. Guest layouts stay 32-bit; host renderer
// pointers are owned here. Unimplemented slots fail by name through the import
// dispatcher. Descriptors and COM lifetime are tested separately from GPU work.
#include "com.h"
#include "dx.h"
#include "d3d8_fpu.h"
#include "frame_dump.h"
#include "../runtime/guest.h"
#include "../runtime/memory.h"
#include "../runtime/native_seam.h"
#include "../runtime/display_seam.h"
#include "../runtime/win32.h"
#include "../platform/os.h"
#include <unordered_set>
#include <cstdio>
#include <cstdlib>

#include <string.h>
#include <algorithm>
#include <vector>
#include <string>
#include <new>

#ifdef RECOMP_D3D8_RESOURCES
#include "d3d8_abi.h"
#endif

namespace {

// ---------------------------------------------------------------------------
// Win32 / D3D8 constants (guest values, not host).
// ---------------------------------------------------------------------------
constexpr uint32_t D8_OK = 0;
constexpr uint32_t D8_ERR_INVALIDCALL = 0x8876086Cu;
constexpr uint32_t D8_ERR_NOTAVAILABLE = 0x8876086Au;

// D3D8 enum/flag values come from the pinned Wine headers
// (third_party/wine-d3d8), resolved by tools/gen_d3d8_constants.py from
// dx/d3d8_constants.json under the headers' own names.
#include "d3d8_constants.inc"

// D3DSWAPEFFECT. The host owns presentation: a single offscreen target is
// blitted to the window, so DISCARD, FLIP and COPY_VSYNC all mean
//
//   "render into the target, then present the completed frame".
//
// FLIP is what the original asks for in fullscreen (with Windowed == FALSE).
// DIVERGENCE(original): real FLIP rotates back-buffer ownership; the bridge's
// presentation seam coalesces the effects.
//
// D3DPRESENT_INTERVAL_*. Default (0) and IMMEDIATE both mean "do not throttle
// this Present against a refresh count"; the host present is not vsync-bound,
// so either is serviceable. The ONE..FOUR vblank counts are not modeled.

// D3D8's GetAvailableTextureMem reports free texture memory, which on the
// modeled unified-memory machine is the runtime's deterministic available
// physical memory (KERNEL32!GlobalMemoryStatus dwAvailPhys, 384 MB). The
// backend exposes no VRAM budget, so this is the modeled figure, not a live
// wgpu query.
constexpr uint32_t D8_AVAILABLE_TEXTURE_MEM = 384u * 1024u * 1024u;

// Bytes per pixel for the uncompressed D3D8 formats the bridge can hold in
// system memory. 0 marks a format the shim does not represent (compressed DXT
// blocks, depth, mixed signed formats, and the palettised P8/A8P8 formats: no
// texture palette path exists, so advertising them would let a create succeed
// whose SetPaletteEntries then fails). The value is the true
// D3D8 texel pitch unit: a R5G6B5 level of width w has pitch 2*w, and the
// bytes the guest reads and writes through LockRect are in that native
// layout. The host renderer samples the 32-bit ARGB formats and the packed
// 16-bit R5G6B5/A1R5G5B5/A4R4G4B4 and signed V8U8 formats; a level in
// any other format is
// still an honest CPU texture and is refused by name when something tries to
// give it to the device.
//
// Format numbers follow the pinned Wine D3D8 headers
// (graphics/d3d8-wgpu/reference/wine/d3d8types.h).
uint32_t d8_format_bytes(uint32_t fmt) {
#ifdef RECOMP_D3D8_RESOURCES
    return d3d8_format_bytes(fmt);
#else
    switch (fmt) {
    case D3DFMT_A8R8G8B8:
    case D3DFMT_X8R8G8B8:
        return 4;
    case D3DFMT_R5G6B5:
    case 24: // X1R5G5B5
    case 25: // A1R5G5B5
    case 26: // A4R4G4B4
    case 29: // A8R3G3B2
    case 30: // X4R4G4B4
    case 60: // V8U8: signed U then V bytes
    case 51: // A8L8
        return 2;
    case 27: // R3G3B2
    case 28: // A8
    case 50: // L8
    case 52: // A4L4
        return 1;
    case 31: // A2B10G10R10
    case 32: // A8B8G8R8
    case 33: // X8B8G8R8
    case 34: // G16R16
    case 35: // A2R10G10B10
        return 4;
    case 36: // A16B16G16R16
        return 8;
    default:
        return 0;
    }
#endif
}

// Bytes per 4x4 block for the S3TC/BC formats the Rust renderer decodes at
// upload (graphics/d3d8-wgpu/src/d3d8/format.rs). 0 marks a non-block format.
// The guest's compressed bytes are authoritative in CPU storage; a level's
// pitch and size are block-based (see d3d8_texture_level_layout).
uint32_t d8_block_bytes(uint32_t fmt) {
    return fmt == D3DFMT_DXT1 ? 8u : (fmt == D3DFMT_DXT3 || fmt == D3DFMT_DXT5 ? 16u : 0u);
}

// Adapter facts. d3d8_adapter_info builds a wgpu context, so query it once.
// ---------------------------------------------------------------------------
struct AdapterCache {
    bool queried = false;
    bool valid = false;
#ifdef RECOMP_D3D8_WGPU
    D3d8AdapterInfo info{};
#endif
};
AdapterCache &adapter_cache() {
    static AdapterCache c;
    return c;
}

bool ensure_adapter() {
    AdapterCache &c = adapter_cache();
    if (c.queried)
        return c.valid;
    c.queried = true;
#ifdef RECOMP_D3D8_WGPU
    D3d8Error err{};
    if (d3d8_abi_version() != D3D8_ABI_VERSION) {
        fprintf(stderr, "d3d8: incompatible host renderer ABI\n");
        fflush(stderr);
        return false;
    }
    if (d3d8_adapter_info(&c.info, &err) == 0) {
        c.valid = true;
        LOGW("d3d8: adapter %s vendor=0x%04x device=0x%04x max_texture=%u",
             reinterpret_cast<const char *>(c.info.name), c.info.vendor_id, c.info.device_id,
             c.info.max_texture_dimension_2d);
    } else {
        LOGW("d3d8: adapter query failed: %s", reinterpret_cast<const char *>(err.message));
    }
#else
    LOGW("d3d8: built without the Rust wgpu renderer (RECOMP_D3D8_WGPU)");
#endif
    return c.valid;
}

ComObj *d8_this(X86 *c) {
    return com_this_arg(c, IF_D3D8);
}
ComObj *d8_dev(X86 *c) {
    return com_this_arg(c, IF_D3D8DEVICE);
}
ComObj *d8_tex(X86 *c) {
    return com_this_arg(c, IF_D3D8TEXTURE8);
}

#ifdef RECOMP_D3D8_WGPU
static D3d8Device *host_device(ComObj *o) {
    return static_cast<D3d8Device *>(o->d3d8_device);
}
#endif

// Objects have stable host addresses until com_reset. Reset must release GPU
// handles itself: com_reset discards objects without running their destructors.
std::unordered_set<ComObj *> live_devices;

std::unordered_set<ComObj *> live_resources;

uint8_t *storage_data(ComObj *o) {
#ifdef RECOMP_D3D8_RESOURCES
    return d3d8_storage_data(static_cast<D3d8Storage *>(o->d3d8_storage));
#else
    return o->blob.empty() ? nullptr : o->blob.data();
#endif
}
bool storage_alloc(ComObj *o, uint32_t bytes) {
    if (!bytes)
        return false;
#ifdef RECOMP_D3D8_RESOURCES
    D3d8Error err{};
    o->d3d8_storage = d3d8_storage_create(bytes, &err);
    if (!o->d3d8_storage)
        return false;
#else
    try {
        o->blob.assign(bytes, 0);
    } catch (const std::bad_alloc &) {
        return false;
    }
#endif
    o->pixels_bytes = bytes;
    live_resources.insert(o);
    return true;
}
void storage_destroy(ComObj *o) {
#ifdef RECOMP_D3D8_RESOURCES
    d3d8_storage_destroy(static_cast<D3d8Storage *>(o->d3d8_storage));
    o->d3d8_storage = nullptr;
#else
    o->blob.clear();
#endif
    o->pixels_bytes = 0;
    live_resources.erase(o);
}

#ifdef RECOMP_D3D8_WGPU
uint32_t host_result(X86 *c, int32_t status, const D3d8Error &err) {
    if (status == D3D8_STATUS_OK)
        return D8_OK;
    fprintf(stderr, "d3d8: %s\n", reinterpret_cast<const char *>(err.message));
    fflush(stderr);
    if (status == D3D8_STATUS_UNSUPPORTED || status == D3D8_STATUS_NOT_IMPLEMENTED)
        imports_unsupported(c);
    return status == D3D8_STATUS_INVALID_ARGUMENT ? D8_ERR_INVALIDCALL
           : status == D3D8_STATUS_OUT_OF_MEMORY  ? E_OUTOFMEMORY
                                                  : D8_ERR_NOTAVAILABLE;
}
#endif

// ---------------------------------------------------------------------------
// D3DCAPS8 describes this bounded renderer, not wgpu's potential capabilities.
//
// The values mirror a period GeForce3/4-class DX8.1 HAL where the bridge has a
// path for the feature, and are deliberately conservative where the shim has
// no equivalent. A bit a period HAL would report but this shim does not is
// called out with a comment so the omission is a decision, not an accident.
// The feature a bit gates is checked at draw time by the bridge and fails by
// name when unimplemented, which is how the remaining gaps are found.
// ---------------------------------------------------------------------------
void write_caps(uint32_t addr) {
    memset(gm_ptr(addr), 0, 212);
    wr32(addr, D3DDEVTYPE_HAL);
    // Caps (+0x08) stays zero: the only D3D8 bit is D3DCAPS_READ_SCANLINE
    // (0x20000), i.e. IDirect3DDevice8::GetRasterStatus, which the bridge does
    // not implement. (Caps3 +0x10 also stays zero: ALPHA_FULLSCREEN_FLIP_OR_
    // DISCARD is about a real fullscreen flip chain, which the host present
    // seam does not have.)
    //
    // Caps2 (+0x0c). Windowed rendering is the normal host path; the arena owns
    // all resources so managed textures are manageable; every texture lock is
    // serviced from CPU storage, so dynamic textures are honest. Not advertised:
    // D3DCAPS2_FULLSCREENGAMMA / CANCALIBRATEGAMMA (no SetGammaRamp path) and
    // D3DCAPS2_NO2DDURING3DSCENE (inert: the host always composites 2D).
    wr32(addr + 0x0c,
         D3DCAPS2_CANRENDERWINDOWED | D3DCAPS2_CANMANAGERESOURCE | D3DCAPS2_DYNAMICTEXTURES);
    // PresentationIntervals (+0x14). The host present is not throttled against
    // a refresh count, so only IMMEDIATE is truthful. The ONE..FOUR vblank
    // intervals are not modelled and are left out.
    wr32(addr + 0x14, D3DPRESENT_INTERVAL_IMMEDIATE);
    // CursorCaps (+0x18) stays zero: SetCursorProperties/ShowCursor are not
    // implemented, so a hardware-color cursor must not be advertised (a period
    // HAL reported D3DCURSORCAPS_COLOR).
    //
    // DevCaps (+0x1c). HWTRANSFORMANDLIGHT is load-bearing: a guest that gates
    // its configurable T&L path on this bit otherwise forces the software
    // fallback. The rest describe a DX8 HAL whose vertices and textures live in
    // system memory (the bridge's CPU storage) and that rasterizes and draws
    // primitives itself. Not advertised: EXECUTEVIDEOMEMORY /
    // TLVERTEXVIDEOMEMORY (no separate video memory), CANRENDERAFTERFLIP /
    // SEPARATETEXTUREMEMORIES / CANBLTSYSTONONLOCAL / PUREDEVICE (no flip
    // chain, one texture memory, no non-local blit, not pure) and the
    // RT/N-patch bits (no tessellation). The guest-side evidence and addresses
    // live in the game repository's docs/engine-info.md.
    //
    // DIVERGENCE(original): the reference adapter reported its full DevCaps.
    wr32(addr + 0x1c,
         0x00000010u |     // D3DDEVCAPS_EXECUTESYSTEMMEMORY
             0x00000040u | // D3DDEVCAPS_TLVERTEXSYSTEMMEMORY
             0x00000100u | // D3DDEVCAPS_TEXTURESYSTEMMEMORY
             0x00000200u | // D3DDEVCAPS_TEXTUREVIDEOMEMORY
             0x00000400u | // D3DDEVCAPS_DRAWPRIMTLVERTEX
             0x00001000u | // D3DDEVCAPS_TEXTURENONLOCALVIDMEM
             0x00002000u | // D3DDEVCAPS_DRAWPRIMITIVES2
             0x00008000u | // D3DDEVCAPS_DRAWPRIMITIVES2EX
             0x00010000u | // D3DDEVCAPS_HWTRANSFORMANDLIGHT
             0x00080000u); // D3DDEVCAPS_HWRASTERIZATION
    // PrimitiveMiscCaps (+0x20). Z-write masking, all three cull modes, the
    // per-channel color-write mask and blend ops are honoured by the draw
    // pipeline. CLIPTLVERTS matches the pre-transformed (XYZRHW) path, whose
    // clipping is only accepted when it is provably unobservable. Not
    // advertised: LINEPATTERNREP (no patterned lines), CLIPPLANESCALEDPOINTS,
    // TSSARGTEMP (no temp register) and NULLREFERENCE/CULLNONE-only quirks.
    wr32(addr + 0x20,
         0x00000002u |     // D3DPMISCCAPS_MASKZ
             0x00000010u | // D3DPMISCCAPS_CULLNONE
             0x00000020u | // D3DPMISCCAPS_CULLCW
             0x00000040u | // D3DPMISCCAPS_CULLCCW
             0x00000080u | // D3DPMISCCAPS_COLORWRITEENABLE
             0x00000200u | // D3DPMISCCAPS_CLIPTLVERTS
             0x00000800u); // D3DPMISCCAPS_BLENDOP
    // RasterCaps (+0x24). The bridge honours depth test, vertex and table fog,
    // D3DRS_RANGEFOGENABLE, LOD bias, ZBIAS, anisotropic filtering (at
    // MaxAnisotropy = 1, i.e. linear) and perspective-correct interpolation.
    // DITHER is stored but is a documented no-op (the host target is 32-bit),
    // so it is advertised although it changes no pixels. Not advertised:
    // PAT/ANTIALIASEDGES (no patterned or antialiased lines),
    // ZBUFFERLESSHSR, WFOG/ZFOG (ordinary w/eye-space depth is used),
    // STRETCHBLTMULTISAMPLE.
    wr32(addr + 0x24,
         0x00000001u |     // D3DPRASTERCAPS_DITHER
             0x00000010u | // D3DPRASTERCAPS_ZTEST
             0x00000080u | // D3DPRASTERCAPS_FOGVERTEX
             0x00000100u | // D3DPRASTERCAPS_FOGTABLE
             0x00002000u | // D3DPRASTERCAPS_MIPMAPLODBIAS
             0x00004000u | // D3DPRASTERCAPS_ZBIAS
             0x00010000u | // D3DPRASTERCAPS_FOGRANGE
             0x00020000u | // D3DPRASTERCAPS_ANISOTROPY
             0x00400000u); // D3DPRASTERCAPS_COLORPERSPECTIVE
    // ZCmpCaps (+0x28), SrcBlendCaps (+0x2c), DestBlendCaps (+0x30). The
    // fixed-function shader maps every D3DCMPFUNC and every D3DBLEND factor
    // except BOTHSRCALPHA/BOTHINVSRCALPHA, which wgpu cannot express; those
    // two bits are omitted (a period HAL reported them).
    wr32(addr + 0x28, 0x000000ffu);
    wr32(addr + 0x2c, 0x000007ffu);
    wr32(addr + 0x30, 0x000007ffu);
    // AlphaCmpCaps (+0x34). A guest can gate its `D3DRS_ALPHATESTENABLE`
    // setup on the compare function it intends to use; a zero field silently
    // renders alpha-tested geometry opaque even when the bridge would honour
    // the state. The fixed-function shader implements every `D3DCMPFUNC`
    // (`NEVER` .. `ALWAYS`), so advertise all eight bits. This matches the D3D9
    // bridge's `AlphaCmpCaps`.
    //
    // DIVERGENCE(original): the reference adapter reported its hardware's
    // compare-function set; the bridge supports the full D3D8 enum.
    wr32(addr + 0x34, 0x000000ffu);
    // ShadeCaps (+0x38). Gouraud RGB and specular shading, per-vertex alpha and
    // fog, are all implemented; flat/Phong shading stay refusals. Not
    // advertised: COLORFLATRGB / SPECULARFLATRGB / ALPHAFLATBLEND (flat is a
    // named refusal) and the per-pixel *PHONGRGB forms.
    wr32(addr + 0x38, 0x00084208u);
    // TextureCaps (+0x3c). Perspective-correct 2D textures with alpha, mipmaps
    // and projected (COUNT3|PROJECTED) coordinates are supported; wgpu handles
    // non-power-of-two sizes. Not advertised: POW2/SQUAREONLY (wgpu does not
    // require them, so a HAL would leave them clear), ALPHAPALETTE /
    // MIPVOLUMEMAP / VOLUMEMAP / CUBEMAP / MIPCUBEMAP and their POW2 variants
    // (no cube/volume textures), NONPOW2CONDITIONAL and
    // TEXREPEATNOTSCALEDBYSIZE (no such restriction).
    wr32(addr + 0x3c, D3DPTEXTURECAPS_PERSPECTIVE | D3DPTEXTURECAPS_ALPHA |
                          D3DPTEXTURECAPS_PROJECTED | D3DPTEXTURECAPS_MIPMAP);
    // TextureFilterCaps (+0x40). Point, linear and anisotropic (linear at
    // MaxAnisotropy = 1), with point/linear mip filters. Cubic stays a named
    // refusal. Cube/volume filter caps (+0x44/+0x48) stay zero: no cube or
    // volume textures.
    wr32(addr + 0x40, D3DPTFILTERCAPS_MINFPOINT | D3DPTFILTERCAPS_MINFLINEAR |
                          D3DPTFILTERCAPS_MINFANISOTROPIC | D3DPTFILTERCAPS_MIPFPOINT |
                          D3DPTFILTERCAPS_MIPFLINEAR | D3DPTFILTERCAPS_MAGFPOINT |
                          D3DPTFILTERCAPS_MAGFLINEAR | D3DPTFILTERCAPS_MAGFANISOTROPIC);
    // TextureAddressCaps (+0x4c). Wrap, mirror, clamp, border and independent
    // U/V are honoured. MIRRORONCE has no wgpu equivalent and is omitted.
    // VolumeTextureAddressCaps (+0x50) stays zero (no volume textures).
    wr32(addr + 0x4c, D3DPTADDRESSCAPS_WRAP | D3DPTADDRESSCAPS_MIRROR | D3DPTADDRESSCAPS_CLAMP |
                          D3DPTADDRESSCAPS_BORDER | D3DPTADDRESSCAPS_INDEPENDENTUV);
    // LineCaps (+0x54) stays zero: DrawPrimitive accepts only POINTLIST and
    // TRIANGLELIST, so textured/z-tested/blended/alpha-tested/fogged lines are
    // not offered.
    // MaxTextureWidth/MaxTextureHeight (D3DCAPS8 +0x58/+0x5c). A game that
    // sizes textures against these (Ghost Recon's 0x004eac20 halves an image
    // until it fits) collapses every texture to 1x1 when they read 0. 2048 is
    // inside wgpu's default max_texture_dimension_2d (8192) on every backend.
    //
    // DIVERGENCE(original): the reference adapter reported its hardware's limit.
    wr32(addr + 0x58, 2048);
    wr32(addr + 0x5c, 2048);
    // MaxVolumeExtent (+0x60) stays zero: no volume textures.
    // MaxTextureRepeat / MaxTextureAspectRatio (+0x64/+0x68): 0 means "no
    // limit" in D3D8 and matches the sampler, which wraps and filters freely.
    // MaxAnisotropy (+0x6c): the bridge maps D3DTEXF_ANISOTROPIC to linear
    // filtering, so the only honest maximum is 1.
    wr32(addr + 0x6c, 1);
    // MaxVertexW, guard band and ExtentsAdjust (+0x70..+0x84) stay zero. There
    // is no w-buffer and wgpu clips at the view volume, so a guard band would
    // be a promise the host does not keep.
    // StencilCaps (+0x88) stays zero: D3DRS_STENCILENABLE is a named refusal,
    // so no stencil operation is offered (the D3D7 bridge does the same).
    // FVFCaps (+0x8c). D3DFVFCAPS_TEXCOORDCOUNTMASK is the number of texture
    // coordinate sets the FVF decoder carries; it handles two. PSIZE (point
    // size) is not offered, so its bit stays clear.
    wr32(addr + 0x8c, 2); // D3DFVFCAPS_TEXCOORDCOUNTMASK field: two sets
    // TextureOpCaps (+0x90). The fixed-function path resolves the core
    // modulate/add/blend ops; the value matches the D3D9 bridge's declared
    // capability. A guest can read D3DTEXOPCAPS_DOTPRODUCT3 (0x00800000) as a
    // stand-in for "a real 3D adapter" when deciding whether compressed
    // (S3TC/DXT) textures are usable, so leaving this zero silently disables
    // them even though CheckDeviceFormat accepts the DXT formats. Advertising
    // the bit is what original hardware reported.
    wr32(addr + 0x90,
         D3DTEXOPCAPS_DISABLE | D3DTEXOPCAPS_SELECTARG1 | D3DTEXOPCAPS_SELECTARG2 |
             D3DTEXOPCAPS_MODULATE | D3DTEXOPCAPS_MODULATE2X | D3DTEXOPCAPS_MODULATE4X |
             D3DTEXOPCAPS_ADD | D3DTEXOPCAPS_ADDSIGNED | D3DTEXOPCAPS_ADDSIGNED2X |
             D3DTEXOPCAPS_SUBTRACT | D3DTEXOPCAPS_ADDSMOOTH | D3DTEXOPCAPS_BLENDDIFFUSEALPHA |
             D3DTEXOPCAPS_BLENDTEXTUREALPHA | D3DTEXOPCAPS_BLENDFACTORALPHA |
             D3DTEXOPCAPS_BLENDTEXTUREALPHAPM | D3DTEXOPCAPS_BLENDCURRENTALPHA |
             D3DTEXOPCAPS_MODULATEALPHA_ADDCOLOR | D3DTEXOPCAPS_MODULATECOLOR_ADDALPHA |
             D3DTEXOPCAPS_MODULATEINVALPHA_ADDCOLOR | D3DTEXOPCAPS_MODULATEINVCOLOR_ADDALPHA |
             D3DTEXOPCAPS_BUMPENVMAP | D3DTEXOPCAPS_BUMPENVMAPLUMINANCE | D3DTEXOPCAPS_DOTPRODUCT3 |
             D3DTEXOPCAPS_MULTIPLYADD | D3DTEXOPCAPS_LERP);
    // MaxTextureBlendStages/MaxSimultaneousTextures (+0x94/+0x98). Guest
    // 0x007c2160 clamps both to 2 and only sets up (and later binds) that many
    // texture stages; at 0 the game renders everything untextured, and at 1 it
    // takes its single-stage path and never binds the base texture the world
    // draws keep in stage 1 (0x007c2c70). The bridge composites two stages.
    //
    // DIVERGENCE(original): the reference adapter reported its hardware's count.
    wr32(addr + 0x94, 2);
#ifdef RECOMP_D3D8_WGPU
    wr32(addr + 0x98, 4); // ps.1.1 has four independent samplers
#else
    wr32(addr + 0x98, 2);
#endif
    // VertexProcessingCaps (+0x9c). Texture-coordinate generation,
    // COLOR1/COLOR2 material sources, directional/point/spot lights, the local
    // viewer and the no-UBYTE4 rule are implemented. Not advertised: TWEENING
    // (no vertex blending).
    wr32(addr + 0x9c, D3DVTXPCAPS_TEXGEN | D3DVTXPCAPS_MATERIALSOURCE7 |
                          D3DVTXPCAPS_DIRECTIONALLIGHTS | D3DVTXPCAPS_POSITIONALLIGHTS |
                          D3DVTXPCAPS_LOCALVIEWER | D3DVTXPCAPS_NO_VSDT_UBYTE4);
    // MaxActiveLights (+0xa0) matches the bridge's eight-light state;
    // MaxUserClipPlanes (+0xa4), MaxVertexBlendMatrices (+0xa8) and
    // MaxVertexBlendMatrixIndex (+0xac) stay zero because clip planes and
    // vertex blending are named refusals.
    wr32(addr + 0xa0, 8);
    // MaxPointSize (+0xb0) stays zero: POINTLIST draws at wgpu's fixed
    // one-pixel point size, which is D3D8's default 1.0 and the only size the
    // bridge honours (see `d3d8-wgpu::Device::draw_primitive` and
    // `DeviceState::validate_point_draw`), so no larger size is offered.
    // MaxPrimitiveCount (+0xb4) is the DX8 HAL-scale batch limit; a draw is not
    // otherwise bounded by the bridge. MaxVertexIndex (+0xb8) is the 16-bit
    // vertex-index ceiling; MaxStreams (+0xbc) is one because SetStreamSource
    // accepts only stream 0. MaxStreamStride (+0xc0) is the DX8 HAL maximum.
    wr32(addr + 0xb4, 0x00555555u);
    wr32(addr + 0xb8, 0x0000ffffu);
    wr32(addr + 0xbc, 1);
    wr32(addr + 0xc0, 508);
#ifdef RECOMP_D3D8_WGPU
    wr32(addr + 0xc4, 0xfffe0101u); // vs.1.1
    wr32(addr + 0xc8, 96);
    wr32(addr + 0xcc, 0xffff0101u); // ps.1.1
    wr32(addr + 0xd0, 0x3f800000u); // MaxPixelShaderValue = 1.0
#endif
}

// The DirectDraw table holds the front end's 8/16-bit modes, so filtering it
// for 32 bits yields nothing. The D3D8 backend can create and present any
// 32-bit target, so advertise the virtual desktop plus the standard 4:3 steps;
// 0 is the default refresh. The game picks its own fullscreen size and only
// needs a non-empty mode list to consider the adapter compatible.
//
// DIVERGENCE(original): the reference adapter enumerated the real hardware's
// modes. These host-presentable sizes are synthesized; the host seam does not
// enumerate D3D8 display modes, and the desktop entry comes from the virtual
// display mode rather than an adapter query.
struct DisplayMode {
    uint32_t w, h, refresh, format;
};
std::vector<DisplayMode> display_modes() {
    std::vector<DisplayMode> result;
    uint32_t dw = 0, dh = 0, dbpp = 0;
    win32_display_mode(&dw, &dh, &dbpp);
    if (dw && dh)
        result.push_back({dw, dh, 0, D3DFMT_X8R8G8B8});
    static const uint32_t kStandard[][2] = {
        {640, 480},
        {800, 600},
        {1024, 768},
        {1280, 1024},
        {1600, 1200},
        // Experimental extras: 4:3 above 1600x1200, and 1512x982 (this
        // MacBook's 3024x1964 panel at 1x points, ~1.54:1).
        {1440, 1080},
        {1920, 1440},
        {1512, 982},
    };
    for (const auto &m : kStandard)
        if (m[0] != dw || m[1] != dh)
            result.push_back({m[0], m[1], 0, D3DFMT_X8R8G8B8});
    return result;
}

bool adapter_type(X86 *c) {
    return arg(c, 1) == 0 && arg(c, 2) == D3DDEVTYPE_HAL && ensure_adapter();
}
bool color_format(uint32_t format) {
    return format == D3DFMT_A8R8G8B8 || format == D3DFMT_X8R8G8B8;
}

// True when a raw D3DFORMAT is one of the four depth/stencil formats the
// wgpu backend maps to an honest format (see graphics/d3d8-wgpu format.rs).
bool d8_depth_format(uint32_t fmt) {
    return fmt == D3DFMT_D16 || fmt == D3DFMT_D24S8 || fmt == D3DFMT_D24X8 || fmt == D3DFMT_D32;
}

// True when CheckDeviceFormat should answer D3D_OK for a usage/rtype/format
// triple. Kept separate so the texture tests can exercise the policy without
// a device. Depth-stencil use is backed for the four mapped depth formats; the
// CPU formats and GPU sampling support are separate capabilities.
bool check_device_format_ok(uint32_t usage, uint32_t rtype, uint32_t fmt) {
    if (usage & D3DUSAGE_DEPTHSTENCIL)
        return rtype == D3DRTYPE_SURFACE && d8_depth_format(fmt);
    if (usage & D3DUSAGE_RENDERTARGET) {
        // A render target is either a standalone surface or a level-0
        // DEFAULT-pool texture; the bridge keeps the latter CPU-backed and
        // binds it through SetRenderTarget. Both are limited to the color
        // formats the offscreen target can hold.
        return (rtype == D3DRTYPE_SURFACE || rtype == D3DRTYPE_TEXTURE) && color_format(fmt);
    }
    if (rtype == D3DRTYPE_TEXTURE)
        return d8_format_bytes(fmt) != 0 || d8_block_bytes(fmt) != 0;
    return false;
}

void write_display_mode(uint32_t addr, const DisplayMode &m) {
    if (!addr || !gm_valid(addr, 16))
        return;
    wr32(addr + 0, m.w);
    wr32(addr + 4, m.h);
    wr32(addr + 8, m.refresh);
    wr32(addr + 12, m.format);
}

// ---------------------------------------------------------------------------
// IDirect3D8
// ---------------------------------------------------------------------------
void D8_RegisterSoftwareDevice(X86 *c) {
    (void)c;
    log_once("d3d8.factory.RegisterSoftwareDevice", "d3d8: RegisterSoftwareDevice ignored");
    com_ret(c, D8_ERR_NOTAVAILABLE);
}
void D8_GetAdapterCount(X86 *c) {
    com_ret(c, ensure_adapter() ? 1 : 0);
}
void D8_GetAdapterIdentifier(X86 *c) {
    uint32_t out = arg(c, 3);
    if (arg(c, 1) != 0 || !out || !gm_valid(out, 1068)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!ensure_adapter()) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    memset(gm_ptr(out), 0, 1068);
#ifdef RECOMP_D3D8_WGPU
    strncpy((char *)gm_ptr(out), "wgpu", 511);
    strncpy((char *)gm_ptr(out + 512), reinterpret_cast<const char *>(adapter_cache().info.name),
            511);
    wr32(out + 1032, adapter_cache().info.vendor_id);
    wr32(out + 1036, adapter_cache().info.device_id);
#endif
    com_ret(c, D8_OK);
}
void D8_GetAdapterModeCount(X86 *c) {
    uint32_t count = arg(c, 1) == 0 && ensure_adapter() ? uint32_t(display_modes().size()) : 0;
    LOGV("d3d8: GetAdapterModeCount(adapter %u) -> %u", arg(c, 1), count);
    com_ret(c, count);
}
void D8_EnumAdapterModes(X86 *c) {
    auto modes = display_modes();
    uint32_t mode = arg(c, 2), out = arg(c, 3);
    if (arg(c, 1) != 0 || mode >= modes.size() || !out || !gm_valid(out, 16)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!ensure_adapter()) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    write_display_mode(out, modes[mode]);
    LOGV("d3d8: EnumAdapterModes(adapter %u, mode %u) -> %ux%u fmt=0x%x", arg(c, 1), mode,
         modes[mode].w, modes[mode].h, modes[mode].format);
    com_ret(c, D8_OK);
}
void D8_GetAdapterDisplayMode(X86 *c) {
    uint32_t out = arg(c, 2), w, h, bpp;
    if (arg(c, 1) != 0 || !out || !gm_valid(out, 16)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!ensure_adapter()) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    win32_display_mode(&w, &h, &bpp);
    if (bpp != 32) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    write_display_mode(out, {w, h, 0, D3DFMT_X8R8G8B8});
    com_ret(c, D8_OK);
}
void D8_CheckDeviceType(X86 *c) {
    // The game probes the fullscreen form (Windowed == 0) of its default
    // mode. The host backend renders the requested backbuffer and blits the
    // completed frame to the window, so windowed and fullscreen requests are
    // equally serviceable; only format compatibility is a real constraint.
    uint32_t ok =
        adapter_type(c) && arg(c, 3) == 22 && color_format(arg(c, 4)) ? D8_OK : D8_ERR_NOTAVAILABLE;
    LOGV("d3d8: CheckDeviceType(adapter %u, type %u, display 0x%x, back 0x%x, windowed %u) -> 0x%x",
         arg(c, 1), arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5), ok);
    com_ret(c, ok);
}
void D8_CheckDeviceFormat(X86 *c) {
    // (Adapter, DeviceType, AdapterFormat, Usage, RType, CheckFormat). The
    // adapter format is the host's presentation format, so only X8R8G8B8 is
    // backed. A render-target or depth-stencil use needs host storage: only
    // the 32-bit ARGB color formats can be a render target, and depth is not
    // implemented. A plain texture use is answerable for any format the shim
    // can hold in system memory (a SYSTEMMEM texture is CPU bytes); the host
    // refuses to sample a non-ARGB format by name if one is ever bound.
    uint32_t usage = arg(c, 4), rtype = arg(c, 5), fmt = arg(c, 6);
    bool ok = false;
    if (adapter_type(c) && arg(c, 3) == D3DFMT_X8R8G8B8) {
        if (check_device_format_ok(usage, rtype, fmt))
            ok = true;
    }
    LOGV("d3d8: CheckDeviceFormat(adapter %u, type %u, adapterfmt 0x%x, usage 0x%x, rtype %u, "
         "fmt 0x%x) -> %s",
         arg(c, 1), arg(c, 2), arg(c, 3), usage, rtype, fmt, ok ? "OK" : "NOTAVAILABLE");
    com_ret(c, ok ? D8_OK : D8_ERR_NOTAVAILABLE);
}
void D8_CheckDeviceMultiSampleType(X86 *c) {
    com_ret(c, adapter_type(c) && color_format(arg(c, 3)) && arg(c, 4) && !arg(c, 5)
                   ? D8_OK
                   : D8_ERR_NOTAVAILABLE);
}
// (Adapter, DeviceType, AdapterFormat, RenderTargetFormat, DepthStencilFormat).
// Every depth format the backend maps is compatible with the 32-bit ARGB
// render targets this bridge advertises; anything else is NOTAVAILABLE.
void D8_CheckDepthStencilMatch(X86 *c) {
    bool ok = adapter_type(c) && color_format(arg(c, 3)) && color_format(arg(c, 4)) &&
              d8_depth_format(arg(c, 5));
    com_ret(c, ok ? D8_OK : D8_ERR_NOTAVAILABLE);
}
void D8_GetDeviceCaps(X86 *c) {
    uint32_t out = arg(c, 3);
    if (!out || !gm_valid(out, 212)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!adapter_type(c)) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    write_caps(out);
    LOGV("d3d8: GetDeviceCaps(adapter %u, type %u) devcaps=0x%x caps2=0x%x", arg(c, 1), arg(c, 2),
         rd32(out + 28), rd32(out + 12));
    com_ret(c, D8_OK);
}
void D8_GetAdapterMonitor(X86 *c) {
    // USER32's virtual desktop exposes one monitor, with guest handle 1.
    com_ret(c, arg(c, 1) == 0 && ensure_adapter() ? 1 : 0);
}

// Internal surface references preserve storage/identity without retaining the
// device in return. An external AddRef (including GetRenderTarget) restores
// device ownership; its final Release drops it, avoiding a binding cycle.
void surface_refs_changed(ComObj *surface) {
    const bool external = surface->refs > surface->internal_refs;
    ComObj *dev = com_get(surface->d3d8_owner);
    if (!dev || external == surface->d3d8_owner_retained)
        return;
    surface->d3d8_owner_retained = external;
    if (external)
        com_addref(dev);
    else
        com_release(dev);
}

// Drop binding refs after clearing ids: release may recursively destroy a
// resource or its device. Caller keeps the device alive across normal switches.
void device_unbind_targets(ComObj *dev) {
    const uint32_t color = dev->d3d8_target, depth = dev->d3d8_target_depth;
    dev->d3d8_target = dev->d3d8_target_depth = 0;
    if (ComObj *o = com_get(color))
        com_release_internal(o);
    if (ComObj *o = com_get(depth))
        com_release_internal(o);
}

// A device-owned implicit surface. Real D3D8 keeps the implicit swap-chain
// color and depth surfaces alive until the device is reset or released, so the
// device holds one internal reference and the guest's Get* result is an
// additional AddRef. That reference is what surface_refs_changed turns into
// device retention; internal ownership does not retain the device, so there is
// no device <-> surface cycle.
ComObj *device_implicit_surface(ComObj *dev, uint32_t w, uint32_t h, uint32_t format, bool depth) {
    ComObj *surface = com_new(K_D3D8SURFACE);
    if (!surface)
        return nullptr;
    surface->d3d8_owner = dev->id;
    surface->width = w;
    surface->height = h;
    surface->rmask = format;
    surface->d3d8_depth = depth;
    surface->d3d8_usage = depth ? D3DUSAGE_DEPTHSTENCIL : D3DUSAGE_RENDERTARGET;
    com_internalize(surface); // the device's ownership reference
    return surface;
}

// The implicit color surface, made on demand and kept via the device's weak
// cache. Returns a new guest reference.
ComObj *device_backbuffer(ComObj *dev) {
    ComObj *surface = com_get(dev->d3d8_backbuffer);
    if (surface) {
        com_addref(surface);
        return surface;
    }
    surface =
        device_implicit_surface(dev, dev->d3d8_width, dev->d3d8_height, dev->d3d8_format, false);
    if (!surface)
        return nullptr;
    dev->d3d8_backbuffer = surface->id;
    com_addref(surface); // the guest's returned reference
    return surface;
}

// The implicit autodepth surface, made on demand. The actual depth bytes live
// in the Rust target; this guest object is the handle GetDepthStencilSurface
// returns and SetRenderTarget accepts. Returns a new guest reference.
ComObj *device_depthbuffer(ComObj *dev) {
    ComObj *surface = com_get(dev->d3d8_depthbuffer);
    if (surface) {
        com_addref(surface);
        return surface;
    }
    surface = device_implicit_surface(dev, dev->d3d8_width, dev->d3d8_height,
                                      dev->d3d8_depth_format, true);
    if (!surface)
        return nullptr;
    dev->d3d8_depthbuffer = surface->id;
    com_addref(surface);
    return surface;
}

// The initial bridge supports a single explicit-size windowed color target.
// Reject unsupported presentation semantics before allocating host resources.
void D8_CreateDevice(X86 *c) {
    uint32_t pp = arg(c, 5), out = arg(c, 6);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    if (!pp || !gm_valid(pp, 52) || !d8_this(c)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t w = rd32(pp), h = rd32(pp + 4), format = rd32(pp + 8);
    uint32_t swap = rd32(pp + 20), windowed = rd32(pp + 28);
    LOGV("d3d8: CreateDevice params %ux%u fmt=0x%x count=%u ms=%u swap=%u hwnd=0x%x windowed=%u "
         "autodepth=%u autofmt=0x%x flags=0x%x refresh=%u interval=%u",
         w, h, format, rd32(pp + 12), rd32(pp + 16), swap, rd32(pp + 24), windowed, rd32(pp + 32),
         rd32(pp + 36), rd32(pp + 40), rd32(pp + 44), rd32(pp + 48));
    if (!w || !h || uint64_t(w) * h * 4 > UINT32_MAX || !color_format(format)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    // DISCARD (1), FLIP (2) and COPY_VSYNC (4) are the swap effects the host
    // can honour by presenting a completed frame; reject any other semantics
    // instead of silently dropping them. FLIP is fullscreen-only in real D3D8,
    // so gate it on Windowed == FALSE. Windowed and fullscreen themselves
    // present through the same host target, so the flag is otherwise not a
    // constraint. An autodepth request is accepted only for a depth format the
    // backend maps; the depth attachment is owned by the Rust target and the
    // guest surface is its handle. The presentation interval accepts Default
    // and IMMEDIATE, the ways RT3 asks for an unthrottled Present.
    uint32_t depth = rd32(pp + 32), depth_format = rd32(pp + 36), interval = rd32(pp + 48);
    bool swap_ok = swap == D3DSWAPEFFECT_DISCARD || swap == D3DSWAPEFFECT_COPY_VSYNC ||
                   (swap == D3DSWAPEFFECT_FLIP && !windowed);
    bool interval_ok = interval == 0 || interval == D3DPRESENT_INTERVAL_IMMEDIATE;
    if (rd32(pp + 12) > 1 || rd32(pp + 16) || !swap_ok ||
        (depth && !d8_depth_format(depth_format)) || rd32(pp + 40) || rd32(pp + 44) ||
        !interval_ok) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    LOGV("d3d8: CreateDevice %ux%u fmt=0x%x swap=%u windowed=%u autodepth=%u autofmt=0x%x", w, h,
         format, swap, windowed, depth, depth_format);
    if (!adapter_type(c)) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    uint16_t previous_cw = c->fpu_cw;
    uint32_t behavior_flags = arg(c, 4);
    d3d8_setup_guest_fpu(c, behavior_flags);
    LOGW("d3d8: CreateDevice behavior=0x%x guest FPU CW %04x -> %04x", behavior_flags, previous_cw,
         c->fpu_cw);
#ifdef RECOMP_D3D8_WGPU
    ComObj *factory = d8_this(c);
    ComObj *dev = com_new(K_D3D8DEVICE);
    live_devices.insert(dev);
    dev->d3d8_factory = factory->id;
    com_addref(factory);
    dev->d3d8_width = w;
    dev->d3d8_height = h;
    dev->d3d8_format = format;
    dev->d3d8_depth_format = depth ? depth_format : 0;
    D3d8Error err{};
    dev->d3d8_device = d3d8_device_create(w, h, format, depth ? depth_format : 0, &err);
    if (!dev->d3d8_device) {
        com_release(dev);
        com_ret(c, host_result(c, err.status ? err.status : D3D8_STATUS_BACKEND, err));
        return;
    }
    // The default depth binding is logical until a guest asks for a handle.
    // Creating an externally counted handle here would retain the device forever.
    uint32_t view = com_view(dev, IF_D3D8DEVICE);
    if (!view) {
        com_release(dev);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, view);
    com_ret(c, D8_OK);
#else
    com_ret(c, D8_ERR_NOTAVAILABLE);
#endif
}

// ---------------------------------------------------------------------------
// IDirect3DDevice8
// ---------------------------------------------------------------------------

// Drop the device's references to the implicit backbuffer and autodepth
// surfaces so Reset or destruction can retire them. Any guest reference keeps
// the old object alive; the weak cache is cleared so a future Get* builds a
// fresh surface against the new target.
void device_discard_implicit_surfaces(ComObj *dev) {
    device_unbind_targets(dev);
    ComObj *backbuffer = com_get(dev->d3d8_backbuffer);
    ComObj *depthbuffer = com_get(dev->d3d8_depthbuffer);
    dev->d3d8_backbuffer = 0;
    dev->d3d8_depthbuffer = 0;
    if (backbuffer)
        com_release_internal(backbuffer);
    if (depthbuffer)
        com_release_internal(depthbuffer);
    dev->d3d8_depth_detached = false;
}

// (this, pPresentationParameters). D3D8 Reset recreates the implicit swap
// chain and resets device state. The guest 52-byte D3DPRESENT_PARAMETERS is
// copied into the plain ABI struct; validation and the target/state rebuild
// live in Rust. Here we only retire the two guest implicit-surface handles so
// they are lazily recreated against the new target.
void Dev_Reset(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t pp = arg(c, 1);
    if (!dev || !dev->d3d8_device || !pp || !gm_valid(pp, 52)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    D3d8PresentParams params{};
    params.back_buffer_width = rd32(pp);
    params.back_buffer_height = rd32(pp + 4);
    params.back_buffer_format = rd32(pp + 8);
    params.back_buffer_count = rd32(pp + 12);
    params.multisample_type = rd32(pp + 16);
    params.swap_effect = rd32(pp + 20);
    params.device_window = rd32(pp + 24);
    params.windowed = rd32(pp + 28);
    params.enable_auto_depth_stencil = rd32(pp + 32);
    params.auto_depth_stencil_format = rd32(pp + 36);
    params.flags = rd32(pp + 40);
    params.fullscreen_refresh_rate = rd32(pp + 44);
    params.fullscreen_presentation_interval = rd32(pp + 48);
    LOGV("d3d8: Reset params %ux%u fmt=0x%x count=%u ms=%u swap=%u hwnd=0x%x windowed=%u "
         "autodepth=%u autofmt=0x%x flags=0x%x refresh=%u interval=%u",
         params.back_buffer_width, params.back_buffer_height, params.back_buffer_format,
         params.back_buffer_count, params.multisample_type, params.swap_effect,
         params.device_window, params.windowed, params.enable_auto_depth_stencil,
         params.auto_depth_stencil_format, params.flags, params.fullscreen_refresh_rate,
         params.fullscreen_presentation_interval);
    D3d8Error err{};
    int32_t status = d3d8_device_reset(host_device(dev), &params, &err);
    if (status) {
        com_ret(c, host_result(c, status, err));
        return;
    }
    dev->d3d8_width = params.back_buffer_width;
    dev->d3d8_height = params.back_buffer_height;
    dev->d3d8_format = params.back_buffer_format;
    dev->d3d8_depth_format =
        params.enable_auto_depth_stencil ? params.auto_depth_stencil_format : 0;
    device_discard_implicit_surfaces(dev);
    dev->d3d8_fvf = 0;
    if (dev->d3d8_constants) {
        dev->d3d8_constants->pixel_shader = 0;
        memset(dev->d3d8_constants->vconst, 0, sizeof(dev->d3d8_constants->vconst));
        memset(dev->d3d8_constants->pconst, 0, sizeof(dev->d3d8_constants->pconst));
    }
    com_ret(c, D8_OK);
#else
    com_ret(c, D8_ERR_NOTAVAILABLE);
#endif
}

void Dev_TestCooperativeLevel(X86 *c) {
    com_ret(c, D8_OK);
}
// (this, DWORD *pNumPasses). The engine's DoTextureTests (guest 0x005489f0)
// probes the current texture/blend state and only keeps its texture path when
// the probe reports S_OK and exactly one pass. The host composites the two
// texture stages in a single pass, so every state the bridge accepts is valid
// with one pass; reporting a pass count of 1 is the modelled answer, not a
// driver query. Rejecting a state here would make the engine drop a mode it
// later needs.
void Dev_ValidateDevice(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 1);
    com_ret(c, D8_OK);
}
// The renderer stores this as its texture-memory budget (Ghidra 0x007c70b0
// reads IDirect3DDevice8 slot 4 into renderer +4/+8). Return the modeled
// available texture memory rather than a fabricated GPU size.
void Dev_GetAvailableTextureMem(X86 *c) {
    com_ret(c, D8_AVAILABLE_TEXTURE_MEM);
}
void Dev_GetDirect3D(X86 *c) {
    ComObj *dev = d8_dev(c);
    ComObj *factory = dev ? com_get(dev->d3d8_factory) : nullptr;
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    if (!factory) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(factory, IF_D3D8);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(factory);
    wr32(out, view);
    com_ret(c, D8_OK);
}
void Dev_GetDeviceCaps(X86 *c) {
    uint32_t out = arg(c, 1);
    if (!d8_dev(c) || !out || !gm_valid(out, 212)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    write_caps(out);
    com_ret(c, D8_OK);
}
void Dev_BeginScene(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        D3d8Error err{};
        int32_t status = d3d8_device_begin_scene(host_device(dev), &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_EndScene(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        D3d8Error err{};
        int32_t status = d3d8_device_end_scene(host_device(dev), &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_Clear(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        // (this, Count, pRects, Flags, Color, Z, Stencil). The Rust backend
        // applies exactly the color/depth/stencil bits Flags asks for; a
        // request the target cannot satisfy is a named backend error.
        float z;
        uint32_t zbits = arg(c, 5);
        memcpy(&z, &zbits, 4);
        LOGV("d3d8: Clear rects=%u flags=0x%x color=0x%08x z=%g stencil=%u", arg(c, 1), arg(c, 3),
             arg(c, 4), z, arg(c, 6));
        D3d8Error err{};
        int32_t status = d3d8_device_clear(host_device(dev), arg(c, 1), arg(c, 3), arg(c, 4), z,
                                           arg(c, 6), &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_SetTransform(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t matrix = arg(c, 2);
    if (dev && dev->d3d8_device && matrix && gm_valid(matrix, 64)) {
        D3d8Matrix value;
        memcpy(&value, gm_ptr(matrix), sizeof value); // Guest may be unaligned.
        D3d8Error err{};
        int32_t status = d3d8_device_set_transform(host_device(dev), arg(c, 1), &value, &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_SetViewport(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t vp = arg(c, 1);
    if (dev && dev->d3d8_device && vp && gm_valid(vp, 24)) {
        float min_z, max_z;
        memcpy(&min_z, gm_ptr(vp + 16), 4);
        memcpy(&max_z, gm_ptr(vp + 20), 4);
        D3d8Error err{};
        int32_t status = d3d8_device_set_viewport(host_device(dev), rd32(vp), rd32(vp + 4),
                                                  rd32(vp + 8), rd32(vp + 12), min_z, max_z, &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
// (this, D3DVIEWPORT8 *pViewport). D3D8 returns the viewport currently set,
// not a query of the host rasterizer, so an out-of-range SetViewport value is
// still reported back verbatim.
void Dev_GetViewport(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t out = arg(c, 1);
    if (dev && dev->d3d8_device && out && gm_valid(out, 24)) {
        D3d8Viewport vp{};
        D3d8Error err{};
        int32_t status = d3d8_device_get_viewport(host_device(dev), &vp, &err);
        if (status == D3D8_STATUS_OK) {
            wr32(out + 0, vp.x);
            wr32(out + 4, vp.y);
            wr32(out + 8, vp.width);
            wr32(out + 12, vp.height);
            memcpy(gm_ptr(out + 16), &vp.min_z, 4);
            memcpy(gm_ptr(out + 20), &vp.max_z, 4);
        }
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_SetMaterial(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t p = arg(c, 1);
    if (dev && dev->d3d8_device && p && gm_valid(p, sizeof(D3d8Material))) {
        D3d8Material material;
        memcpy(&material, gm_ptr(p), sizeof material);
        D3d8Error err{};
        int32_t status = d3d8_device_set_material(host_device(dev), &material, &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_GetMaterial(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t p = arg(c, 1);
    if (dev && dev->d3d8_device && p && gm_valid(p, sizeof(D3d8Material))) {
        D3d8Material material{};
        D3d8Error err{};
        int32_t status = d3d8_device_get_material(host_device(dev), &material, &err);
        if (status == D3D8_STATUS_OK)
            memcpy(gm_ptr(p), &material, sizeof material);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
// (this, Index, pLight). D3D8 accepts eight active lights; an out-of-range
// index is INVALIDCALL from the backend, not a fail-loud unsupported call.
void Dev_SetLight(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t p = arg(c, 2);
    if (dev && dev->d3d8_device && p && gm_valid(p, sizeof(D3d8Light))) {
        D3d8Light light;
        memcpy(&light, gm_ptr(p), sizeof light);
        D3d8Error err{};
        int32_t status = d3d8_device_set_light(host_device(dev), arg(c, 1), &light, &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_GetLight(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t p = arg(c, 2);
    if (dev && dev->d3d8_device && p && gm_valid(p, sizeof(D3d8Light))) {
        D3d8Light light{};
        D3d8Error err{};
        int32_t status = d3d8_device_get_light(host_device(dev), arg(c, 1), &light, &err);
        if (status == D3D8_STATUS_OK)
            memcpy(gm_ptr(p), &light, sizeof light);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_LightEnable(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        D3d8Error err{};
        int32_t status = d3d8_device_light_enable(host_device(dev), arg(c, 1), arg(c, 2), &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_GetLightEnable(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    uint32_t out = arg(c, 2);
    if (dev && dev->d3d8_device && out && gm_valid(out, 4)) {
        uint32_t enabled = 0;
        D3d8Error err{};
        int32_t status = d3d8_device_get_light_enable(host_device(dev), arg(c, 1), &enabled, &err);
        if (status == D3D8_STATUS_OK)
            wr32(out, enabled);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_SetRenderState(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        D3d8Error err{};
        int32_t status = d3d8_device_set_render_state(host_device(dev), arg(c, 1), arg(c, 2), &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_SetTextureStageState(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        D3d8Error err{};
        int32_t status = d3d8_device_set_texture_stage_state(host_device(dev), arg(c, 1), arg(c, 2),
                                                             arg(c, 3), &err);
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
// (this, Stage, pTexture). The device holds a reference to the bound texture
// and drops the replaced one; the rendered sampling happens at draw time via
// d8_sync_texture, so a LockRect after SetTexture is observed.
void Dev_SetTexture(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t stage = arg(c, 1), view = arg(c, 2);
    if (!dev || stage >= 8) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *tex = view ? com_this(view, IF_D3D8TEXTURE8) : nullptr;
    if (view && (!tex || tex->kind != K_D3D8TEXTURE)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *old = com_get(dev->d3d8_bound_texture[stage]);
    if (tex)
        com_addref(tex);
    dev->d3d8_bound_texture[stage] = tex ? tex->id : 0;
    if (old && old != tex)
        com_release(old);
    com_ret(c, D8_OK);
}
// (this, Stage, ppTexture). D3D8 returns the bound texture with a new
// reference; a NULL binding writes NULL and reports D3D_OK.
void Dev_GetTexture(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t stage = arg(c, 1), out = arg(c, 2);
    if (!dev || stage >= 8 || !out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    ComObj *tex = com_get(dev->d3d8_bound_texture[stage]);
    if (tex) {
        com_addref(tex);
        uint32_t tex_view = com_view(tex, IF_D3D8TEXTURE8);
        if (!tex_view) {
            com_release(tex);
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
        wr32(out, tex_view);
    }
    com_ret(c, D8_OK);
}
// Set once the native-texture handoff is unavailable, so a host that cannot take it
// does not pay a GPU copy and wait on every frame before falling back.
static bool g_native_handoff_failed = false;
void Dev_Present(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        if (arg(c, 1) || arg(c, 2) || arg(c, 3) || arg(c, 4)) {
            fprintf(stderr, "d3d8: Present rectangles/window override/dirty regions unsupported\n");
            fflush(stderr);
            imports_unsupported(c);
        }
        D3d8Error err{};
        int32_t status = d3d8_device_present(host_device(dev), &err);
        if (status) {
            com_ret(c, host_result(c, status, err));
            return;
        }
        uint64_t bytes = uint64_t(dev->d3d8_width) * dev->d3d8_height * 4;
        if (!bytes || bytes > UINT32_MAX) {
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
        // Preferred: hand the backbuffer to the presenter as a GPU texture, with
        // no CPU pixels. Frame dumps need the pixels, and a host without a shared
        // Metal device declines, so both take the readback below.
        if (!dx_dump_enabled() && !g_native_handoff_failed) {
            void *native = nullptr;
            uint32_t *busy = nullptr;
            uint32_t w = 0, h = 0;
            if (d3d8_device_present_handoff(host_device(dev), &native, &busy, &w, &h, &err) == 0) {
                if (host_display_present_native_texture(native, int(w), int(h), busy)) {
                    com_ret(c, D8_OK);
                    return;
                }
                // Declined (different device or no GPU presenter): not retried.
                __atomic_store_n(busy, 0u, __ATOMIC_RELEASE);
            }
            g_native_handoff_failed = true;
            fprintf(stderr, "d3d8: native present handoff unavailable (%s); using CPU readback\n",
                    err.message);
            fflush(stderr);
        }
        // Rust copies mapped rows straight into reused caller storage. Dump raw
        // readback before the host's presentation-only opaque-alpha adjustment.
        static std::vector<uint8_t> rgba;
        if (rgba.size() != size_t(bytes))
            rgba.resize(size_t(bytes), 0);
        uint32_t got = 0;
        status =
            d3d8_device_read_pixels(host_device(dev), rgba.data(), uint32_t(bytes), &got, &err);
        if (status) {
            com_ret(c, host_result(c, status, err));
            return;
        }
        if (got != bytes) {
            com_ret(c, D8_ERR_NOTAVAILABLE);
            return;
        }
        dx_dump_frame_rgba(rgba.data(), dev->d3d8_width, dev->d3d8_height);
        host_display_present_window_rgba(rgba.data(), int(dev->d3d8_width), int(dev->d3d8_height));
        com_ret(c, D8_OK);
        return;
    }
#endif
    com_ret(c, D8_ERR_INVALIDCALL);
}
void Dev_GetBackBuffer(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t out = arg(c, 3);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    // The host backend has one non-stereo, non-multisampled backbuffer.
    if (!dev || arg(c, 1) != 0 || arg(c, 2) != 0 || !dev->d3d8_width || !dev->d3d8_height ||
        (dev->d3d8_format != 21 && dev->d3d8_format != 22) ||
        uint64_t(dev->d3d8_width) * dev->d3d8_height * 4 > UINT32_MAX) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *surface = device_backbuffer(dev);
    if (!surface) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    uint32_t view = com_view(surface, IF_D3D8SURFACE8);
    if (!view) {
        com_release(surface);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, view);
    com_ret(c, D8_OK);
}

// Returns the currently bound color identity, with a real external reference.
void Dev_GetRenderTarget(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    // A bound color surface keeps its internal binding reference; the guest's
    // returned handle is one more. With no explicit target the implicit
    // backbuffer supplies the reference.
    ComObj *surface;
    if (dev->d3d8_target) {
        surface = com_get(dev->d3d8_target);
        if (surface)
            com_addref(surface);
    } else {
        surface = device_backbuffer(dev);
    }
    if (!surface) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    uint32_t view = com_view(surface, IF_D3D8SURFACE8);
    if (!view) {
        com_release(surface);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, view);
    com_ret(c, D8_OK);
}

// (this, ppDepthStencilSurface). Returns the implicit autodepth surface when
// the device was created with EnableAutoDepthStencil; without one this is the
// same INVALIDCALL real D3D8 returns.
void Dev_GetDepthStencilSurface(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!dev->d3d8_depth_format || dev->d3d8_depth_detached) {
        com_ret(c, 0x88760866u);
        return;
    }
    ComObj *surface =
        dev->d3d8_target_depth ? com_get(dev->d3d8_target_depth) : device_depthbuffer(dev);
    if (dev->d3d8_target_depth && surface)
        com_addref(surface);
    if (!surface) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    uint32_t view = com_view(surface, IF_D3D8SURFACE8);
    if (!view) {
        com_release(surface);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, view);
    com_ret(c, D8_OK);
}

// Bounded boundary evidence. Read raw stack bytes without guest accessors so
// a corrupt stack cannot fault during diagnostics. Optimized x86 uses EBP as
// a general register: these words are candidates, never a claimed call chain.
void diagnose_render_target(X86 *c, ComObj *dev, uint32_t rt_arg, ComObj *rt, uint32_t ds_arg,
                            ComObj *ds) {
    fprintf(stderr,
            "d3d8: SetRenderTarget device=%u backbuffer=%u depthbuffer=%u "
            "EIP=%08x ESP=%08x EBP=%08x\n",
            dev ? dev->id : 0, dev ? dev->d3d8_backbuffer : 0, dev ? dev->d3d8_depthbuffer : 0,
            c->eip, c->r[R_ESP], c->r[R_EBP]);
    auto surface = [](const char *label, uint32_t pointer, ComObj *o) {
        if (!o) {
            fprintf(stderr, "  %s ptr=%08x %s\n", label, pointer, pointer ? "unresolved" : "null");
            return;
        }
        ComObj *owner = com_get(o->d3d8_owner);
        const bool implicit = owner && owner->d3d8_backbuffer == o->id;
        fprintf(stderr,
                "  %s ptr=%08x id=%u owner=%u texture=%u level=%u "
                "size=%ux%u format=0x%x usage=0x%x pool=%u depth=%u implicit=%u\n",
                label, pointer, o->id, o->d3d8_owner, o->d3d8_texture, o->d3d8_level,
                implicit ? owner->d3d8_width : o->width, implicit ? owner->d3d8_height : o->height,
                implicit ? owner->d3d8_format : o->rmask,
                implicit ? D3DUSAGE_RENDERTARGET : o->d3d8_usage, o->d3d8_pool,
                unsigned(o->d3d8_depth), unsigned(implicit));
    };
    surface("backbuffer", 0, dev ? com_get(dev->d3d8_backbuffer) : nullptr);
    surface("target", rt_arg, rt);
    surface("depth", ds_arg, ds);
    fprintf(stderr, "  raw stack (word 0 is COM return address; not an unwound call chain):\n");
    for (unsigned i = 0; i < 48; ++i) {
        const uint64_t at = uint64_t(c->r[R_ESP]) + i * 4;
        if (!g_mem || at < GUEST_NULL_LIMIT || at + 4 > GUEST_SIZE) {
            fprintf(stderr, "    +%03x <unreadable>\n", i * 4);
            break;
        }
        uint32_t word;
        memcpy(&word, g_mem + at, sizeof word);
        fprintf(stderr, "    +%03x %08x\n", i * 4, word);
    }
    fflush(stderr);
}

// D3D8: NULL color keeps current color; NULL depth detaches it. Binding
// nonnull color resets the viewport. Only this device's implicit surfaces and
// level-0 DEFAULT-pool RT textures are represented; other cases stop by name.
void Dev_SetRenderTarget(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t rt_arg = arg(c, 1), ds_arg = arg(c, 2);
    ComObj *rt = rt_arg ? com_this(rt_arg, IF_D3D8SURFACE8) : nullptr;
    ComObj *ds = ds_arg ? com_this(ds_arg, IF_D3D8SURFACE8) : nullptr;
    bool rejected = !dev || (rt_arg && (!rt || rt->d3d8_depth || rt->d3d8_owner != dev->id)) ||
                    (ds_arg && (!ds || !ds->d3d8_depth || ds->d3d8_owner != dev->id));
    if (dev && rt && rt->id != dev->d3d8_backbuffer &&
        (!(rt->d3d8_usage & D3DUSAGE_RENDERTARGET) || rt->d3d8_pool != D3DPOOL_DEFAULT ||
         rt->d3d8_level != 0 || !rt->pixels_bytes || rt->lock_count))
        rejected = true;
    if (rejected) {
        diagnose_render_target(c, dev, rt_arg, rt, ds_arg, ds);
        fprintf(stderr,
                "d3d8: SetRenderTarget unsupported surface identity/owner/usage/level/lock\n");
        fflush(stderr);
        imports_unsupported(c);
    }
    if (!rt_arg)
        rt = com_get(dev->d3d8_target ? dev->d3d8_target : dev->d3d8_backbuffer);
    const bool texture = rt && rt->id != dev->d3d8_backbuffer;
    const uint32_t width = texture ? rt->width : dev->d3d8_width;
    const uint32_t height = texture ? rt->height : dev->d3d8_height;
    if (ds && (ds->width < width || ds->height < height)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
#ifdef RECOMP_D3D8_WGPU
    // DIVERGENCE(original): `ds` is validated for owner, size and format, but
    // the renderer attaches the device's shared implicit autodepth rather than
    // the standalone surface's own storage, so depth contents are shared
    // between render targets.
    D3d8Error err{};
    int32_t status = d3d8_device_set_render_target(
        host_device(dev), texture ? rt->id : 0, texture ? rt->d3d8_level : 0,
        texture ? rt->d3d8_content_generation : 0, texture ? rt->rmask : dev->d3d8_format, width,
        height, texture ? storage_data(rt) : nullptr, texture ? rt->pixels_bytes : 0, ds != nullptr,
        rt_arg != 0, &err);
    if (status) {
        com_ret(c, host_result(c, status, err));
        return;
    }
#else
    // State-only fixtures exercise identity and ownership without claiming GPU execution.
#endif
    // Retain new bindings before releasing old ones, including rebinding self.
    if (rt)
        com_retain_internal(rt);
    if (ds)
        com_retain_internal(ds);
    device_unbind_targets(dev);
    dev->d3d8_target = rt ? rt->id : 0;
    dev->d3d8_target_depth = ds ? ds->id : 0;
    dev->d3d8_depth_detached = !ds;
    if (texture)
        rt->d3d8_gpu_target = true;
    com_ret(c, D8_OK);
}

// CreateTexture has several D3DERR_INVALIDCALL paths and the guest rarely
// inspects which one fired. Print the whole descriptor on stderr so a run can
// attribute the rejection without guessing from the HRESULT.
void diagnose_create_texture(const char *reason, ComObj *dev, uint32_t w, uint32_t h,
                             uint32_t levels, uint32_t usage, uint32_t format, uint32_t pool) {
    fprintf(stderr,
            "d3d8: CreateTexture failed: %s (device=%u %ux%u levels=%u usage=0x%x format=0x%x "
            "pool=%u)\n",
            reason, dev ? dev->id : 0, w, h, levels, usage, format, pool);
    fflush(stderr);
}

// (this, Width, Height, Levels, Usage, Format, Pool, ppTexture). Every level
// is a real, separately sized CPU surface. D3DUSAGE_RENDERTARGET is accepted and
// kept CPU-backed (see the DIVERGENCE below); D3DUSAGE_DEPTHSTENCIL still stops
// by name because no depth texture path exists.
void Dev_CreateTexture(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t w = arg(c, 1), h = arg(c, 2), levels = arg(c, 3);
    uint32_t usage = arg(c, 4), format = arg(c, 5), pool = arg(c, 6), out = arg(c, 7);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    uint32_t bpp = d8_format_bytes(format);
    uint32_t block = d8_block_bytes(format);
    if (!dev || !w || !h || w > 16384 || h > 16384) {
        diagnose_create_texture("invalid device or dimensions", dev, w, h, levels, usage, format,
                                pool);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!bpp && !block) {
        diagnose_create_texture("format is not representable", dev, w, h, levels, usage, format,
                                pool);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (usage & D3DUSAGE_DEPTHSTENCIL) {
        diagnose_create_texture(
            "D3DUSAGE_DEPTHSTENCIL textures are not implemented; use CreateDepthStencilSurface",
            dev, w, h, levels, usage, format, pool);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
#ifdef RECOMP_D3D8_RESOURCES
    D3d8LevelLayout first{};
    D3d8Error layout_err{};
    if (d3d8_texture_level_layout(w, h, levels, 0, format, &first, &layout_err) != D3D8_STATUS_OK) {
        diagnose_create_texture(reinterpret_cast<const char *>(layout_err.message), dev, w, h,
                                levels, usage, format, pool);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    levels = first.levels;
#else
    if (!levels) {
        levels = 1;
        for (uint32_t d = (w > h ? w : h); d > 1; d >>= 1)
            ++levels;
    }
    if (levels > 16)
        levels = 16;
#endif
    // CPU storage stages guest locks/uploads; SetRenderTarget promotes a
    // supported level to lifetime-owned GPU storage on first binding.
    ComObj *tex = com_new(K_D3D8TEXTURE);
    if (!tex) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    tex->d3d8_owner = dev->id;
    com_addref(dev);
    tex->rmask = format;
    tex->d3d8_usage = usage;
    tex->d3d8_pool = pool;
    tex->d3d8_level_count = levels;
    tex->d3d8_lod = 0;
    for (uint32_t l = 0; l < levels; ++l) {
        uint32_t lw = w >> l, lh = h >> l;
        if (!lw)
            lw = 1;
        if (!lh)
            lh = 1;
        uint64_t bytes = uint64_t(lw) * bpp * lh;
#ifdef RECOMP_D3D8_RESOURCES
        D3d8LevelLayout layout{};
        if (d3d8_texture_level_layout(w, h, levels, l, format, &layout, &layout_err) !=
            D3D8_STATUS_OK) {
            diagnose_create_texture(reinterpret_cast<const char *>(layout_err.message), dev, w, h,
                                    levels, usage, format, pool);
            com_release(tex);
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
        lw = layout.width;
        lh = layout.height;
        bytes = layout.size;
#endif
        ComObj *level = com_new(K_D3D8SURFACE);
        if (!level || bytes > GUEST_SIZE || !storage_alloc(level, (uint32_t)bytes)) {
            if (level)
                com_release(level);
            com_release(tex);
            fprintf(stderr,
                    "d3d8: CreateTexture failed: CPU storage (device=%u level=%u bytes=%llu)\n",
                    dev->id, l, (unsigned long long)bytes);
            fflush(stderr);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        level->d3d8_owner = dev->id;
        level->d3d8_owner_retained = true;
        com_addref(dev);
        level->d3d8_texture = tex->id;
        level->d3d8_level = l;
        level->rmask = format;
        level->d3d8_usage = usage;
        level->d3d8_pool = pool;
        level->width = lw;
        level->height = lh;
#ifdef RECOMP_D3D8_RESOURCES
        // A block format reports a block-row pitch; its bytes-per-texel is
        // meaningless and d8_lock_offset uses the block path below.
        level->bpp = block ? 0 : bpp * 8;
        level->pitch = layout.pitch;
#else
        level->bpp = bpp * 8;
        level->pitch = lw * bpp;
#endif
        // The initial reference is the texture's ownership of the level.
        tex->d3d8_levels.push_back(level->id);
        com_internalize(level);
    }
    uint32_t view = com_view(tex, IF_D3D8TEXTURE8);
    if (!view) {
        com_release(tex);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    LOGV("d3d8: CreateTexture %ux%u levels=%u fmt=0x%x pool=%u -> %08x into %08x", w, h, levels,
         format, pool, view, out);
    wr32(out, view);
    com_ret(c, D8_OK);
}

// (this, Width, Height, Format, MultiSampleType, ppSurface). A standalone
// depth-stencil surface the guest can bind with SetRenderTarget. D3D8 accepts
// D16, D24X8, D24S8 and D32. The object stores the descriptor and is retired
// with the device; the depth bytes stay in the Rust target.
//
// DIVERGENCE(original): the renderer's depth attachment is the device's shared
// implicit autodepth, not this surface's own storage (see Dev_SetRenderTarget),
// so depth contents are shared across render targets rather than independent.
void Dev_CreateDepthStencilSurface(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t w = arg(c, 1), h = arg(c, 2), format = arg(c, 3), ms = arg(c, 4), out = arg(c, 5);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    if (!dev || !w || !h || w > 16384 || h > 16384 || ms != 0 || !d8_depth_format(format)) {
        fprintf(stderr,
                "d3d8: CreateDepthStencilSurface rejected (device=%u %ux%u format=0x%x ms=%u)\n",
                dev ? dev->id : 0, w, h, format, ms);
        fflush(stderr);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *surface = com_new(K_D3D8SURFACE);
    if (!surface) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    // A returned resource holds its own reference and retains the device, the
    // same lifetime rule as a texture level.
    surface->d3d8_owner = dev->id;
    com_addref(dev);
    surface->d3d8_owner_retained = true;
    surface->d3d8_depth = true;
    surface->d3d8_usage = D3DUSAGE_DEPTHSTENCIL;
    surface->rmask = format;
    surface->width = w;
    surface->height = h;
    uint32_t view = com_view(surface, IF_D3D8SURFACE8);
    if (!view) {
        com_release(surface);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    LOGV("d3d8: CreateDepthStencilSurface %ux%u fmt=0x%x -> %08x into %08x", w, h, format, view,
         out);
    wr32(out, view);
    com_ret(c, D8_OK);
}

// Once a level has been GPU-rendered, its CPU blob is a staging copy only.
// Synchronize before any CPU read (locks or UpdateTexture source), preserving
// guest byte order through the ABI. Generation stays unchanged on readback.
void sync_rendered_level(ComObj *level) {
#ifdef RECOMP_D3D8_WGPU
    if (!level || !level->d3d8_gpu_target || level->lock_count)
        return;
    ComObj *dev = com_get(level->d3d8_owner);
    if (!dev || !dev->d3d8_device)
        return;
    D3d8Error err{};
    int32_t status = d3d8_device_read_texture(host_device(dev), level->id, level->d3d8_level,
                                              level->d3d8_content_generation, storage_data(level),
                                              level->pixels_bytes, &err);
    if (status) {
        fprintf(stderr, "d3d8: render-target CPU readback failed: %s\n",
                reinterpret_cast<const char *>(err.message));
        fflush(stderr);
        imports_unsupported(nullptr);
    }
#endif
}

// (this, pSourceTexture, pDestinationTexture). D3D8 requires equal formats and
// equal level counts and dimensions, with the source in D3DPOOL_SYSTEMMEM and
// the destination in D3DPOOL_DEFAULT. Any mismatch is a caller error and must
// not leave a partial update, so every level is checked before anything is
// copied. Both textures here are CPU-backed; the copy is the system-memory
// texture content the guest will later lock or hand to the device.
void Dev_UpdateTexture(X86 *c) {
    ComObj *src = com_this(arg(c, 1), IF_D3D8TEXTURE8);
    ComObj *dst = com_this(arg(c, 2), IF_D3D8TEXTURE8);
    if (!src || !dst || src->kind != K_D3D8TEXTURE || dst->kind != K_D3D8TEXTURE || src == dst) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (src->rmask != dst->rmask || src->d3d8_levels.size() != dst->d3d8_levels.size() ||
        src->d3d8_pool != D3DPOOL_SYSTEMMEM || dst->d3d8_pool != D3DPOOL_DEFAULT) {
        LOGW("d3d8: UpdateTexture needs equal formats and level counts, SYSTEMMEM source and "
             "DEFAULT destination (fmt 0x%x/0x%x, pools %u/%u)",
             src->rmask, dst->rmask, src->d3d8_pool, dst->d3d8_pool);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    for (size_t l = 0; l < src->d3d8_levels.size(); ++l) {
        ComObj *s = com_get(src->d3d8_levels[l]);
        ComObj *d = com_get(dst->d3d8_levels[l]);
        if (!s || !d || s->width != d->width || s->height != d->height ||
            s->pixels_bytes != d->pixels_bytes || !storage_data(s) || !storage_data(d)) {
            LOGW("d3d8: UpdateTexture level %zu dimensions differ", l);
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
    }
    for (size_t l = 0; l < src->d3d8_levels.size(); ++l) {
        ComObj *s = com_get(src->d3d8_levels[l]);
        ComObj *d = com_get(dst->d3d8_levels[l]);
#ifdef RECOMP_D3D8_RESOURCES
        D3d8Error err{};
        if (d3d8_storage_copy(static_cast<D3d8Storage *>(d->d3d8_storage),
                              static_cast<D3d8Storage *>(s->d3d8_storage),
                              &err) != D3D8_STATUS_OK) {
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
#else
        sync_rendered_level(s);
        memcpy(storage_data(d), storage_data(s), s->pixels_bytes);
#endif
        // The destination texture's sampled content changed.
        ++d->d3d8_content_generation;
    }
    com_ret(c, D8_OK);
}
// LockRect hands the guest a heap copy; UnlockRect copies it back and frees
// it. The guest pointer is never stored in a host field beyond the lock.
static uint32_t d8_stage_lock(ComObj *o) {
    if (!o || !o->pixels_bytes)
        return 0;
    if (o->lock_count == 0)
        sync_rendered_level(o);
    if (o->lock_count++ == 0) {
        o->pixels = heap_alloc(o->pixels_bytes, false, 16);
        if (!o->pixels) {
            o->lock_count = 0;
            return 0;
        }
        memcpy(gm_ptr(o->pixels), storage_data(o), o->pixels_bytes);
    }
    return o->pixels;
}
static void d8_stage_unlock(ComObj *o) {
    if (!o || o->lock_count <= 0)
        return;
    if (--o->lock_count == 0 && o->pixels) {
        memcpy(storage_data(o), gm_ptr(o->pixels), o->pixels_bytes);
        heap_free(o->pixels);
        o->pixels = 0;
        // The level's bytes now differ from any resident GPU copy. A nested
        // lock (count > 1) writes through the same staged block and is still
        // open, so the draw path is told `dirty` instead of trusting this.
        ++o->d3d8_content_generation;
    }
}

// Validate a D3D8 lock rectangle against a level and return the byte offset of
// its top-left corner. A null rect locks the whole level. An empty or
// out-of-bounds rect is invalid; LockRect reports that as INVALIDCALL rather
// than handing out a pointer past the allocation.
bool d8_lock_offset(const ComObj *level, uint32_t rect, uint32_t *offset) {
    *offset = 0;
    if (!rect)
        return true;
    if (!gm_valid(rect, 16))
        return false;
    int32_t left = (int32_t)rd32(rect);
    int32_t top = (int32_t)rd32(rect + 4);
    int32_t right = (int32_t)rd32(rect + 8);
    int32_t bottom = (int32_t)rd32(rect + 12);
    if (left < 0 || top < 0 || right > (int32_t)level->width || bottom > (int32_t)level->height ||
        right <= left || bottom <= top)
        return false;
    // Block formats are addressed in 4x4 blocks: the top-left of the rect's
    // block is the byte offset. D3D8 requires such a rect to be block-aligned;
    // a non-aligned lock would let the guest write into the previous block, so
    // reject it rather than returning a wrong pointer.
    if (uint32_t bb = d8_block_bytes(level->rmask)) {
        if ((left & 3) || (top & 3))
            return false;
        *offset = (uint32_t)(top / 4) * level->pitch + (uint32_t)(left / 4) * bb;
    } else {
        *offset = (uint32_t)top * level->pitch + (uint32_t)left * (level->bpp / 8);
    }
    return true;
}

// A texture level is one K_D3D8SURFACE; the implicit backbuffer has no blob
// and leaves d3d8_texture zero. The level's own bytes back its lock.
void Surface_GetDesc(X86 *c) {
    ComObj *surface = com_this_arg(c, IF_D3D8SURFACE8);
    uint32_t out = arg(c, 1);
    if (!surface || !out || !gm_valid(out, 32)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t format, usage, pool, size, width, height;
    if (surface->d3d8_texture) {
        format = surface->rmask;
        usage = surface->d3d8_usage;
        pool = surface->d3d8_pool;
        width = surface->width;
        height = surface->height;
        // pixels_bytes already carries the block-aware size for a compressed
        // level; pitch * height would over-report it.
        size = surface->pixels_bytes;
    } else if (surface->d3d8_depth) {
        // Autodepth handle. Its bytes live in the Rust target; the descriptor
        // reports the D3D8 depth size (D16 is 2 bytes/texel, the rest 4).
        format = surface->rmask;
        usage = D3DUSAGE_DEPTHSTENCIL;
        pool = 0; // D3DPOOL_DEFAULT
        width = surface->width;
        height = surface->height;
        size = width * height * (surface->rmask == D3DFMT_D16 ? 2u : 4u);
    } else {
        ComObj *dev = com_get(surface->d3d8_owner);
        if (!dev) {
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
        format = dev->d3d8_format;
        usage = D3DUSAGE_RENDERTARGET;
        pool = 0; // D3DPOOL_DEFAULT
        width = dev->d3d8_width;
        height = dev->d3d8_height;
        size = width * height * 4;
    }
    wr32(out + 0, format);
    wr32(out + 4, D3DRTYPE_SURFACE);
    wr32(out + 8, usage);
    wr32(out + 12, pool);
    wr32(out + 16, size);
    wr32(out + 20, 0); // D3DMULTISAMPLE_NONE
    wr32(out + 24, width);
    wr32(out + 28, height);
    com_ret(c, D8_OK);
}

void Surface_GetDevice(X86 *c) {
    ComObj *surface = com_this_arg(c, IF_D3D8SURFACE8);
    ComObj *dev = surface ? com_get(surface->d3d8_owner) : nullptr;
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(dev, IF_D3D8DEVICE);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(dev);
    wr32(out, view);
    com_ret(c, D8_OK);
}

// A texture level can be locked through its surface view; the implicit
// backbuffer cannot (its bytes belong to the host render target, not the
// shim), so that stops by name rather than returning a pointer into host
// memory.
void Surface_LockRect(X86 *c) {
    ComObj *surface = com_this_arg(c, IF_D3D8SURFACE8);
    uint32_t out = arg(c, 1);
    if (!surface || !out || !gm_valid(out, 8)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!surface->d3d8_texture) {
        fprintf(stderr, "d3d8: IDirect3DSurface8::LockRect on a host render target is not "
                        "implemented\n");
        fflush(stderr);
        imports_unsupported(c);
    }
    // (this, pLockedRect, pRect, Flags).
    uint32_t offset;
    if (!d8_lock_offset(surface, arg(c, 2), &offset)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t base = d8_stage_lock(surface);
    wr32(out, surface->pitch);
    wr32(out + 4, base ? base + offset : 0);
    com_ret(c, base ? D8_OK : E_OUTOFMEMORY);
}
void Surface_UnlockRect(X86 *c) {
    d8_stage_unlock(com_this_arg(c, IF_D3D8SURFACE8));
    com_ret(c, D8_OK);
}

// ---------------------------------------------------------------------------
// IDirect3DTexture8: a 2D texture and its mip levels. The levels are real,
// separately sized CPU surfaces; LockRect stages through the guest heap. No
// host texture is created here, so a SYSTEMMEM (or any pool) texture is
// backed by the bytes the guest writes, not by a hidden GPU copy.
// ---------------------------------------------------------------------------
static ComObj *texture_level(ComObj *tex, uint32_t level) {
    if (!tex || tex->kind != K_D3D8TEXTURE || level >= tex->d3d8_levels.size())
        return nullptr;
    return com_get(tex->d3d8_levels[level]);
}

void Tex_GetDevice(X86 *c) {
    ComObj *tex = d8_tex(c);
    ComObj *dev = tex ? com_get(tex->d3d8_owner) : nullptr;
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(dev, IF_D3D8DEVICE);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(dev);
    wr32(out, view);
    com_ret(c, D8_OK);
}
void Tex_SetPriority(X86 *c) {
    ComObj *tex = d8_tex(c);
    uint32_t old = tex ? tex->d3d8_priority : 0;
    if (tex)
        tex->d3d8_priority = arg(c, 1);
    com_ret(c, old);
}
void Tex_GetPriority(X86 *c) {
    ComObj *tex = d8_tex(c);
    com_ret(c, tex ? tex->d3d8_priority : 0);
}
void Tex_PreLoad(X86 *c) {
    // No host copy exists until a draw binds the texture, so there is nothing
    // to preload. The call is harmless and returns success as the real API.
    com_ret(c, D8_OK);
}
void Tex_GetType(X86 *c) {
    com_ret(c, D3DRTYPE_TEXTURE);
}
void Tex_SetLOD(X86 *c) {
    ComObj *tex = d8_tex(c);
    uint32_t old = tex ? tex->d3d8_lod : 0;
    if (tex)
        tex->d3d8_lod = arg(c, 1);
    com_ret(c, old);
}
void Tex_GetLOD(X86 *c) {
    ComObj *tex = d8_tex(c);
    com_ret(c, tex ? tex->d3d8_lod : 0);
}
void Tex_GetLevelCount(X86 *c) {
    ComObj *tex = d8_tex(c);
    com_ret(c, tex ? tex->d3d8_level_count : 0);
}
void Tex_GetLevelDesc(X86 *c) {
    ComObj *tex = d8_tex(c);
    ComObj *level = texture_level(tex, arg(c, 1));
    uint32_t out = arg(c, 2);
    if (!level || !out || !gm_valid(out, 32)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out + 0, level->rmask);
    wr32(out + 4, D3DRTYPE_SURFACE);
    wr32(out + 8, level->d3d8_usage);
    wr32(out + 12, level->d3d8_pool);
    wr32(out + 16, level->pixels_bytes);
    wr32(out + 20, 0); // D3DMULTISAMPLE_NONE
    wr32(out + 24, level->width);
    wr32(out + 28, level->height);
    com_ret(c, D8_OK);
}
void Tex_GetSurfaceLevel(X86 *c) {
    ComObj *tex = d8_tex(c);
    ComObj *level = texture_level(tex, arg(c, 1));
    uint32_t out = arg(c, 2);
    if (!level || !out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    com_addref(level);
    uint32_t view = com_view(level, IF_D3D8SURFACE8);
    if (!view) {
        com_release(level);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, view);
    com_ret(c, D8_OK);
}
void Tex_LockRect(X86 *c) {
    ComObj *level = texture_level(d8_tex(c), arg(c, 1));
    uint32_t out = arg(c, 2);
    if (!level || !out || !gm_valid(out, 8)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    // (this, Level, pLockedRect, pRect, Flags).
    uint32_t offset;
    if (!d8_lock_offset(level, arg(c, 3), &offset)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t base = d8_stage_lock(level);
    wr32(out, level->pitch);
    wr32(out + 4, base ? base + offset : 0);
    com_ret(c, base ? D8_OK : E_OUTOFMEMORY);
}
void Tex_UnlockRect(X86 *c) {
    d8_stage_unlock(texture_level(d8_tex(c), arg(c, 1)));
    com_ret(c, D8_OK);
}
void Tex_AddDirtyRect(X86 *c) {
    // A hint that a rectangle changed. Nothing is uploaded yet, so there is
    // no GPU copy to mark; the level bytes are already the current contents.
    (void)c;
    com_ret(c, D8_OK);
}

void texture_destroy(ComObj *tex) {
    for (uint32_t sid : tex->d3d8_levels) {
        ComObj *level = com_get(sid);
        if (level)
            com_release_internal(level); // the texture's own level reference
    }
    tex->d3d8_levels.clear();
    if (ComObj *dev = com_get(tex->d3d8_owner))
        com_release(dev);
    tex->d3d8_owner = 0;
}

void surface_destroy(ComObj *surface) {
#ifdef RECOMP_D3D8_WGPU
    if (ComObj *owner = com_get(surface->d3d8_owner))
        if (owner->d3d8_device && surface->pixels_bytes)
            d3d8_device_release_texture(host_device(owner), surface->id);
#endif
    if (surface->pixels)
        heap_free(surface->pixels);
    surface->pixels = 0;
    storage_destroy(surface);
    ComObj *dev = com_get(surface->d3d8_owner);
    if (dev) {
        if (dev->d3d8_backbuffer == surface->id)
            dev->d3d8_backbuffer = 0;
        if (dev->d3d8_depthbuffer == surface->id)
            dev->d3d8_depthbuffer = 0;
        if (surface->d3d8_owner_retained)
            com_release(dev);
    }
    surface->d3d8_owner_retained = false;
    surface->d3d8_owner = 0;
    surface->d3d8_texture = 0;
}

// ---------------------------------------------------------------------------
// IDirect3DVertexBuffer8 / IDirect3DIndexBuffer8. Storage is real host memory
// (Rust storage when configured); Lock stages it into the guest heap and
// Unlock copies it back, so the
// bytes the guest writes survive for the draw path to consume. A buffer is
// bound weakly by the device; the guest's reference is the only owner.
// ---------------------------------------------------------------------------
bool d8_buffer_alloc(ComObj *o, uint32_t bytes) {
    if (!bytes)
        return false;
    if (!storage_alloc(o, bytes))
        return false;
    o->pixels = 0;
    return true;
}

ComObj *d8_buffer(X86 *c) {
    ComObj *o = com_this(arg(c, 0));
    if (!o || (o->kind != K_D3D8VERTEXBUFFER && o->kind != K_D3D8INDEXBUFFER))
        return nullptr;
    return o;
}

void Buffer_GetDevice(X86 *c) {
    ComObj *o = d8_buffer(c);
    ComObj *dev = o ? com_get(o->d3d8_owner) : nullptr;
    uint32_t out = arg(c, 1);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(dev, IF_D3D8DEVICE);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(dev);
    wr32(out, view);
    com_ret(c, D8_OK);
}

// (this, OffsetToLock, SizeToLock, ppbData, Flags). A zero SizeToLock means
// "to the end of the buffer", as in D3D8. The whole blob is staged around the
// lock, so the pointer the guest receives is the staged block plus the offset.
void Buffer_Lock(X86 *c) {
    ComObj *o = d8_buffer(c);
    uint32_t offset = arg(c, 1), out = arg(c, 3);
    if (!o || !o->pixels_bytes || !out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    // The engine's RenderingContext::LockVB (00545b50) passes the caller's end
    // vertex as SizeToLock: WorldModule::LockVb (00523cf0) calls it with
    // (start, start + count), and LockVB scales both by the 24-byte stride. The
    // declared size can therefore run past the buffer while the bytes actually
    // written (count) stay inside it; the original D3D8 accepts this. Rejecting
    // it stalls the vertex buffer and the next draw reads stale (zero) vertices,
    // which is what flickered the UI. SizeToLock (argument 2) is not used beyond
    // validation - the whole blob is staged - so only the offset must be in range.
    if (offset > o->pixels_bytes) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t base = d8_stage_lock(o);
    wr32(out, base ? base + offset : 0);
    com_ret(c, base ? D8_OK : E_OUTOFMEMORY);
}

void Buffer_Unlock(X86 *c) {
    d8_stage_unlock(d8_buffer(c));
    com_ret(c, D8_OK);
}
void Buffer_SetPriority(X86 *c) {
    ComObj *o = d8_buffer(c);
    if (o)
        o->d3d8_priority = arg(c, 1);
    com_ret(c, o ? o->d3d8_priority : 0);
}
void Buffer_GetPriority(X86 *c) {
    ComObj *o = d8_buffer(c);
    com_ret(c, o ? o->d3d8_priority : 0);
}
void Buffer_PreLoad(X86 *c) {
    (void)c;
    com_ret(c, D8_OK);
}
void Buffer_GetType(X86 *c) {
    ComObj *o = d8_buffer(c);
    uint32_t type = 0;
    if (o)
        type = o->kind == K_D3D8VERTEXBUFFER ? D3DRTYPE_VERTEXBUFFER : D3DRTYPE_INDEXBUFFER;
    com_ret(c, type);
}
// D3DVERTEXBUFFER_DESC is 24 bytes, D3DINDEXBUFFER_DESC is 20. A vertex
// buffer's Format is always D3DFMT_VERTEXDATA; an index buffer reports the
// format it was created with.
void Buffer_GetDesc(X86 *c) {
    ComObj *o = d8_buffer(c);
    uint32_t out = arg(c, 1);
    if (!o || !out) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    uint32_t size = o->pixels_bytes;
    if (o->kind == K_D3D8VERTEXBUFFER) {
        if (!gm_valid(out, 24)) {
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
        wr32(out + 0, D3DFMT_VERTEXDATA);
        wr32(out + 4, D3DRTYPE_VERTEXBUFFER);
        wr32(out + 8, o->d3d8_buffer_usage);
        wr32(out + 12, o->d3d8_buffer_pool);
        wr32(out + 16, size);
        wr32(out + 20, o->d3d8_buffer_fvf);
    } else {
        if (!gm_valid(out, 20)) {
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
        wr32(out + 0, o->d3d8_buffer_format);
        wr32(out + 4, D3DRTYPE_INDEXBUFFER);
        wr32(out + 8, o->d3d8_buffer_usage);
        wr32(out + 12, o->d3d8_buffer_pool);
        wr32(out + 16, size);
    }
    com_ret(c, D8_OK);
}

void buffer_destroy(ComObj *o) {
    if (o->pixels)
        heap_free(o->pixels);
    o->pixels = 0;
    storage_destroy(o);
    // The owning device binds buffers weakly; drop any binding that points here
    // so a later draw does not resolve a dead object id.
    if (ComObj *dev = com_get(o->d3d8_owner)) {
        if (dev->d3d8_stream_vb == o->id) {
            dev->d3d8_stream_vb = 0;
            dev->d3d8_stream_stride = 0;
        }
        if (dev->d3d8_indices == o->id)
            dev->d3d8_indices = 0;
        com_release(dev);
    }
    o->d3d8_owner = 0;
}

// (this, Length, Usage, FVF, Pool, ppVertexBuffer)
void Dev_CreateVertexBuffer(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t bytes = arg(c, 1), out = arg(c, 5);
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    if (!dev || !out || !gm_valid(out, 4) || !bytes) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *vb = com_new(K_D3D8VERTEXBUFFER);
    if (!vb || !d8_buffer_alloc(vb, bytes)) {
        if (vb)
            com_release(vb);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    vb->d3d8_owner = dev->id;
    com_addref(dev);
    vb->d3d8_buffer_usage = arg(c, 2);
    vb->d3d8_buffer_fvf = arg(c, 3);
    vb->d3d8_buffer_pool = arg(c, 4);
    uint32_t view = com_view(vb, IF_D3D8VERTEXBUFFER8);
    if (!view) {
        com_release(vb);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    LOGV("d3d8: CreateVertexBuffer %u bytes usage=0x%x fvf=0x%x pool=%u -> %08x", bytes, arg(c, 2),
         arg(c, 3), arg(c, 4), view);
    wr32(out, view);
    com_ret(c, D8_OK);
}

// (this, Length, Usage, Format, Pool, ppIndexBuffer)
void Dev_CreateIndexBuffer(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t bytes = arg(c, 1), out = arg(c, 5), format = arg(c, 3);
    if (out && gm_valid(out, 4))
        wr32(out, 0);
    if (!dev || !out || !gm_valid(out, 4) || !bytes ||
        (format != D3DFMT_INDEX16 && format != D3DFMT_INDEX32)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *ib = com_new(K_D3D8INDEXBUFFER);
    if (!ib || !d8_buffer_alloc(ib, bytes)) {
        if (ib)
            com_release(ib);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    ib->d3d8_owner = dev->id;
    com_addref(dev);
    ib->d3d8_buffer_usage = arg(c, 2);
    ib->d3d8_buffer_format = format;
    ib->d3d8_buffer_pool = arg(c, 4);
    uint32_t view = com_view(ib, IF_D3D8INDEXBUFFER8);
    if (!view) {
        com_release(ib);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, view);
    com_ret(c, D8_OK);
}

// (this, StreamNumber, pStreamData, Stride). This renderer currently supports one vertex stream and
// has no per-stream offset (that arrived in D3D9); any other stream is
// INVALIDCALL rather than a silent drop.
void Dev_SetStreamSource(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t stream = arg(c, 1);
    ComObj *vb = com_this(arg(c, 2));
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (stream != 0) {
        LOGW("d3d8: SetStreamSource stream %u is not implemented (this renderer supports one "
             "stream)",
             stream);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!vb) {
        dev->d3d8_stream_vb = 0;
        dev->d3d8_stream_stride = 0;
    } else if (vb->kind == K_D3D8VERTEXBUFFER) {
        dev->d3d8_stream_vb = vb->id;
        dev->d3d8_stream_stride = arg(c, 3);
    } else {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    com_ret(c, D8_OK);
}

// (this, pIndexData, BaseVertexIndex)
void Dev_SetIndices(X86 *c) {
    ComObj *dev = d8_dev(c);
    ComObj *ib = com_this(arg(c, 1));
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!ib) {
        dev->d3d8_indices = 0;
        dev->d3d8_base_vertex = 0;
    } else if (ib->kind == K_D3D8INDEXBUFFER) {
        dev->d3d8_indices = ib->id;
        dev->d3d8_base_vertex = arg(c, 2);
    } else {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    com_ret(c, D8_OK);
}

D3d8DeviceState *d8_constants(ComObj *dev);

// (this, Handle). Wine uses handles above VS_HIGHESTFIXEDFXF (0xf0000000),
// independently of the 0xfffe0101 vertex bytecode version token.
void Dev_SetVertexShader(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t handle = arg(c, 1);
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    auto *state = d8_constants(dev);
    auto it = state->shaders.find(handle);
    if (handle > 0xf0000000u && (it == state->shaders.end() || it->second.pixel)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
#ifdef RECOMP_D3D8_WGPU
    D3d8Error err{};
    int32_t status =
        d3d8_device_shader_action(host_device(dev), handle > 0xf0000000u ? handle : 0, 0, 0, &err);
    if (status) {
        com_ret(c, host_result(c, status, err));
        return;
    }

#endif
    dev->d3d8_fvf = handle;
    com_ret(c, D8_OK);
}
void Dev_GetVertexShader(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, dev->d3d8_fvf);
    com_ret(c, D8_OK);
}

// ---------------------------------------------------------------------------
// Programmable shader constants. D3D8 keeps float4 register banks on the
// device; Set/Get copy the 32-bit float bit patterns verbatim and never
// convert or interpret them. The constants have no effect on the
// fixed-function pipeline. Programmable draws snapshot these banks through
// the host ABI. A
// guest may set constants while no programmable shader is bound, and the
// water path does, so failing the call would abort it. The banks live on the
// device object, behind a shared_ptr, so Set and Get round-trip across calls.
// ---------------------------------------------------------------------------
constexpr uint32_t D8_VERTEX_CONSTANT_REGISTERS = 96;
constexpr uint32_t D8_PIXEL_CONSTANT_REGISTERS = 8;

// Lazily allocates the per-device constant banks. Only a K_D3D8DEVICE touches
// them; every other object carries just the shared_ptr.
D3d8DeviceState *d8_constants(ComObj *dev) {
    if (!dev->d3d8_constants)
        dev->d3d8_constants = std::make_shared<D3d8DeviceState>();
    return dev->d3d8_constants.get();
}

// True when Set/Get may touch [reg, reg + count) and read/write the guest
// buffer at `data`. A zero count copies nothing and touches no register, so it
// succeeds even with a null pointer. count <= limit <= 96, so count * 16
// cannot overflow.
bool d8_constant_range(uint32_t reg, uint32_t count, uint32_t limit, uint32_t data) {
    if (count == 0)
        return true;
    if (reg >= limit || count > limit - reg)
        return false;
    return data && gm_valid(data, count * 16u);
}

// Copies `count` float4 registers between the guest buffer at `data` and the
// device's 32-bit bank. `to_guest` selects Get (true) or Set (false).
void d8_copy_registers(uint32_t (*bank)[4], uint32_t reg, uint32_t data, uint32_t count,
                       bool to_guest) {
    for (uint32_t i = 0; i < count; ++i)
        for (uint32_t k = 0; k < 4; ++k) {
            uint32_t off = data + (i * 4 + k) * 4;
            if (to_guest)
                wr32(off, bank[reg + i][k]);
            else
                bank[reg + i][k] = rd32(off);
        }
}

// (this, Register, pConstantData, ConstantCount). A range or pointer violation
// is an ordinary D3D error, not an unsupported-import abort.
void Dev_SetVertexShaderConstant(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t reg = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
    if (!dev || !d8_constant_range(reg, count, D8_VERTEX_CONSTANT_REGISTERS, data)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (count)
        d8_copy_registers(d8_constants(dev)->vconst, reg, data, count, false);
#ifdef RECOMP_D3D8_WGPU
    if (count && dev->d3d8_device) {
        D3d8Error err{};
        int32_t status = d3d8_device_shader_constants(
            host_device(dev), 0, reg, &d8_constants(dev)->vconst[reg][0], count, &err);
        if (status) {
            com_ret(c, host_result(c, status, err));
            return;
        }
    }
#endif
    com_ret(c, D8_OK);
}
void Dev_GetVertexShaderConstant(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t reg = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
    if (!dev || !d8_constant_range(reg, count, D8_VERTEX_CONSTANT_REGISTERS, data)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (count)
        d8_copy_registers(d8_constants(dev)->vconst, reg, data, count, true);
    com_ret(c, D8_OK);
}
void Dev_SetPixelShaderConstant(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t reg = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
    if (!dev || !d8_constant_range(reg, count, D8_PIXEL_CONSTANT_REGISTERS, data)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (count)
        d8_copy_registers(d8_constants(dev)->pconst, reg, data, count, false);
#ifdef RECOMP_D3D8_WGPU
    if (count && dev->d3d8_device) {
        D3d8Error err{};
        int32_t status = d3d8_device_shader_constants(
            host_device(dev), 1, reg, &d8_constants(dev)->pconst[reg][0], count, &err);
        if (status) {
            com_ret(c, host_result(c, status, err));
            return;
        }
    }
#endif
    com_ret(c, D8_OK);
}
void Dev_GetPixelShaderConstant(X86 *c) {
    ComObj *dev = d8_dev(c);
    uint32_t reg = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
    if (!dev || !d8_constant_range(reg, count, D8_PIXEL_CONSTANT_REGISTERS, data)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (count)
        d8_copy_registers(d8_constants(dev)->pconst, reg, data, count, true);
    com_ret(c, D8_OK);
}

// Read bounded guest token streams at token boundaries. DEF immediates and
// declaration constants may contain END bits; never scan them as opcodes.
// Creation copies all bytes before the guest assembler buffer can be freed.
bool d8_shader_words(uint32_t addr, bool declaration, std::vector<uint32_t> &out) {
    if (!addr)
        return false;
    for (uint32_t n = 0; n < 4096;) {
        if (!gm_valid(addr, 4))
            return false;
        uint32_t token = rd32(addr), extra = 0;
        if (declaration) {
            if (token == 0xffffffffu) {
                out.push_back(token);
                return true;
            }
            if ((token >> 29) == 4)
                extra = ((token >> 25) & 15) * 4;
            else if ((token >> 29) == 5)
                extra = (token >> 24) & 31;
        } else if (n) {
            uint32_t op = token & 0xffff;
            if (op == 0xffff) {
                out.push_back(token);
                return true;
            }
            if (op == 0xfffe)
                extra = (token >> 16) & 0x7fff;
            else if (op == 0)
                extra = 0;
            else if (op == 81)
                extra = 5;
            else if (op == 1 || (op >= 6 && op <= 7) || (op >= 14 && op <= 16) || op == 19 ||
                     op == 78 || op == 79)
                extra = 2;
            else if (op == 4 || op == 18 || op == 80)
                extra = 4;
            else if (op >= 64 && op <= 66)
                extra = 1;
            else if (op == 2 || op == 3 || op == 5 || (op >= 8 && op <= 13) || op == 17 ||
                     (op >= 20 && op <= 24) || (op >= 67 && op <= 70))
                extra = 3;
            else {
                fprintf(stderr, "d3d8: unsupported shader opcode %u\n", op);
                return false;
            }
        }
        if (extra >= 4096 - n || !gm_valid(addr, (extra + 1) * 4))
            return false;
        for (uint32_t i = 0; i <= extra; ++i)
            out.push_back(rd32(addr + i * 4));
        n += extra + 1;
        if (addr > UINT32_MAX - (extra + 1) * 4)
            return false;
        addr += (extra + 1) * 4;
    }
    return false;
}

// Create a device-owned program after copying and validating its token streams.
// Only the opaque numeric handle crosses into guest fields.
void d8_create_shader(X86 *c, bool pixel) {
    auto *dev = d8_dev(c);
    uint32_t output = arg(c, pixel ? 2 : 3);
    if (!dev || !output || !gm_valid(output, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(output, 0);
    D3d8Shader shader;
    shader.pixel = pixel;
    if ((!pixel && (arg(c, 4) & ~0x10u)) ||
        (!pixel && !d8_shader_words(arg(c, 1), true, shader.declaration)) ||
        ((pixel || arg(c, 2)) && !d8_shader_words(arg(c, pixel ? 1 : 2), false, shader.function))) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
#ifdef RECOMP_D3D8_WGPU
    auto *state = d8_constants(dev);
    uint32_t handle = state->next_shader;
    if (handle == UINT32_MAX) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    D3d8Error err{};
    int32_t status =
        d3d8_device_create_shader(host_device(dev), handle, pixel, shader.declaration.data(),
                                  uint32_t(shader.declaration.size()), shader.function.data(),
                                  uint32_t(shader.function.size()), &err);
    if (status) {
        com_ret(c, host_result(c, status, err));
        return;
    }
    ++state->next_shader;
    if (recomp_env("D3D8_TRACE_SHADERS"))
        fprintf(
            stderr, "d3d8: Create%sShader handle=0x%08x version=0x%08x words=%zu declaration=%zu\n",
            pixel ? "Pixel" : "Vertex", handle, shader.function.empty() ? 0 : shader.function[0],
            shader.function.size(), shader.declaration.size());
    state->shaders.emplace(handle, std::move(shader));
    wr32(output, handle);
    com_ret(c, D8_OK);
#else
    com_ret(c, D8_ERR_NOTAVAILABLE);
#endif
}
void Dev_CreateVertexShader(X86 *c) {
    d8_create_shader(c, false);
}
void Dev_CreatePixelShader(X86 *c) {
    d8_create_shader(c, true);
}

void Dev_SetPixelShader(X86 *c) {
    auto *dev = d8_dev(c);
    uint32_t handle = arg(c, 1);
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    auto *state = d8_constants(dev);
    auto it = state->shaders.find(handle);
    if (handle && (it == state->shaders.end() || !it->second.pixel)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
#ifdef RECOMP_D3D8_WGPU
    D3d8Error err{};
    int32_t status = d3d8_device_shader_action(host_device(dev), handle, 1, 0, &err);
    if (status) {
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    state->pixel_shader = handle;
    com_ret(c, D8_OK);
}
void Dev_GetPixelShader(X86 *c) {
    auto *dev = d8_dev(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out, d8_constants(dev)->pixel_shader);
    com_ret(c, D8_OK);
}
// Deleting a bound program clears that stage; queued host draws retain it.
void d8_delete_shader(X86 *c, bool pixel) {
    auto *dev = d8_dev(c);
    uint32_t handle = arg(c, 1);
    if (!dev) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    auto *state = d8_constants(dev);
    auto it = state->shaders.find(handle);
    if (it == state->shaders.end() || it->second.pixel != pixel) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
#ifdef RECOMP_D3D8_WGPU
    D3d8Error err{};
    int32_t status = d3d8_device_shader_action(host_device(dev), handle, pixel, 1, &err);
    if (status) {
        com_ret(c, host_result(c, status, err));
        return;
    }
#endif
    if (pixel && state->pixel_shader == handle)
        state->pixel_shader = 0;
    if (!pixel && dev->d3d8_fvf == handle)
        dev->d3d8_fvf = 0;
    state->shaders.erase(it);
    com_ret(c, D8_OK);
}
void Dev_DeleteVertexShader(X86 *c) {
    d8_delete_shader(c, false);
}
void Dev_DeletePixelShader(X86 *c) {
    d8_delete_shader(c, true);
}

// D3D8 query convention: null data asks for size; a short buffer reports
// D3DERR_MOREDATA and required bytes without a partial copy (Wine device.c).
void d8_get_shader(X86 *c, bool pixel, bool declaration) {
    auto *dev = d8_dev(c);
    uint32_t data = arg(c, 2), size = arg(c, 3);
    if (!dev || !size || !gm_valid(size, 4)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    auto *state = d8_constants(dev);
    auto it = state->shaders.find(arg(c, 1));
    if (it == state->shaders.end() || it->second.pixel != pixel) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    const auto &words = declaration ? it->second.declaration : it->second.function;
    uint32_t required = uint32_t(words.size() * 4), available = rd32(size);
    wr32(size, required);
    if (!data) {
        com_ret(c, D8_OK);
        return;
    }
    if (available < required) {
        com_ret(c, 0x88760867u);
        return;
    }
    if (!gm_valid(data, required)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    for (size_t i = 0; i < words.size(); ++i)
        wr32(data + uint32_t(i * 4), words[i]);
    com_ret(c, D8_OK);
}
void Dev_GetVertexShaderDeclaration(X86 *c) {
    d8_get_shader(c, false, true);
}
void Dev_GetVertexShaderFunction(X86 *c) {
    d8_get_shader(c, false, false);
}
void Dev_GetPixelShaderFunction(X86 *c) {
    d8_get_shader(c, true, false);
}

// The bytes to draw from right now: the guest heap while the buffer is locked,
// otherwise the host copy the last Unlock wrote back.
const uint8_t *d8_buffer_bytes(ComObj *o) {
    if (!o || !o->pixels_bytes)
        return nullptr;
    if (o->pixels)
        return gm_ptr(o->pixels);
    return storage_data(o);
}

// (this, SrcStartIndex, DestIndex, VertexCount, pDestBuffer, Flags). Fixed-
// function vertex processing into a software-processed destination buffer. The
// Rust renderer owns the transform/lighting/viewport math and the FVF layout;
// this bridge stages the source stream and the destination buffer bytes.
//
// A failure here is a capability report the engine handles: when ProcessVertices
// fails, ResetAndUploadRenderStateBlock takes its own fallback path. It is
// therefore returned as an ordinary D3D error, not an unsupported-import abort,
// so the engine decides what to do.
void Dev_ProcessVertices(X86 *c) {
    ComObj *dev = d8_dev(c);
    ComObj *dest = com_this(arg(c, 4));
    uint32_t src_start = arg(c, 1), dest_index = arg(c, 2), count = arg(c, 3), flags = arg(c, 5);
    ComObj *vb = dev ? com_get(dev->d3d8_stream_vb) : nullptr;
    if (!dev || !dest || dest->kind != K_D3D8VERTEXBUFFER || !vb || !count) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    // D3DPV_DONOTCOPYDATA (1) asks the processor to skip unchanged vertex
    // data; the fixed-function path rewrites every component, so accepting it
    // changes nothing. No other flag is defined in D3D8.
    if (flags & ~1u) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
#ifdef RECOMP_D3D8_WGPU
    if (!dev->d3d8_device) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    const uint8_t *src = d8_buffer_bytes(vb);
    uint8_t *dst = storage_data(dest);
    if (!src || !dst || !vb->pixels_bytes || !dest->pixels_bytes) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    D3d8Error err{};
    int32_t status = d3d8_device_process_vertices(
        host_device(dev), src, vb->pixels_bytes, dev->d3d8_fvf, dev->d3d8_stream_stride, dst,
        dest->pixels_bytes, dest->d3d8_buffer_fvf, src_start, dest_index, count, &err);
    if (status != D3D8_STATUS_OK) {
        fprintf(stderr, "d3d8: ProcessVertices: %s\n", reinterpret_cast<const char *>(err.message));
        fflush(stderr);
        // The engine's fallback replaces a zero return, so a named capability
        // failure is D3DERR_NOTAVAILABLE rather than an abort. A malformed call
        // is still the caller's error.
        com_ret(c,
                status == D3D8_STATUS_INVALID_ARGUMENT ? D8_ERR_INVALIDCALL : D8_ERR_NOTAVAILABLE);
        return;
    }
    com_ret(c, D8_OK);
#else
    (void)src_start;
    (void)dest_index;
    com_ret(c, D8_ERR_NOTAVAILABLE);
#endif
}

// (this, pSourceSurface, pSourceRectsArray, cRects, pDestinationSurface,
// pDestPointsArray). D3D8's rectangle blit between two surfaces that must have
// the same format. The shim's surfaces are CPU storage, so this copies the
// requested rows directly; a surface with an open lock contributes the staged
// guest heap bytes, exactly as a draw or a read would see them. A mismatched
// format is a caller error: the engine's DoTextureTests helper falls back to a
// manual conversion when this returns failure, so it must fail rather than
// guess. A null destination-points array copies each rect to its own corner.
void Dev_CopyRects(X86 *c) {
    ComObj *dev = d8_dev(c);
    ComObj *src = com_this(arg(c, 1), IF_D3D8SURFACE8);
    ComObj *dst = com_this(arg(c, 4), IF_D3D8SURFACE8);
    uint32_t rects = arg(c, 2), count = arg(c, 3), points = arg(c, 5);
    if (!dev || !src || !dst || !count || src->rmask != dst->rmask || src->d3d8_depth ||
        dst->d3d8_depth) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!gm_valid(rects, count * 16) || (points && !gm_valid(points, count * 8))) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    if (!storage_data(src) || !storage_data(dst)) {
        fprintf(stderr, "d3d8: CopyRects on a host render target is not implemented\n");
        fflush(stderr);
        imports_unsupported(c);
        return;
    }
    sync_rendered_level(src);
    sync_rendered_level(dst);
    const uint8_t *sbase = d8_buffer_bytes(src);
    uint8_t *dbase = const_cast<uint8_t *>(d8_buffer_bytes(dst));
    if (!sbase || !dbase) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    const uint32_t block = d8_block_bytes(src->rmask);
    const uint32_t texel = block ? block : src->bpp / 8;
    for (uint32_t i = 0; i < count; ++i) {
        int32_t left = (int32_t)rd32(rects + i * 16);
        int32_t top = (int32_t)rd32(rects + i * 16 + 4);
        int32_t right = (int32_t)rd32(rects + i * 16 + 8);
        int32_t bottom = (int32_t)rd32(rects + i * 16 + 12);
        int32_t dx = left, dy = top;
        if (points) {
            dx = (int32_t)rd32(points + i * 8);
            dy = (int32_t)rd32(points + i * 8 + 4);
        }
        if (left < 0 || top < 0 || right > (int32_t)src->width || bottom > (int32_t)src->height ||
            right <= left || bottom <= top || dx < 0 || dy < 0 ||
            dx + (right - left) > (int32_t)dst->width ||
            dy + (bottom - top) > (int32_t)dst->height) {
            com_ret(c, D8_ERR_INVALIDCALL);
            return;
        }
        if (block) {
            // Block formats address 4x4 blocks; D3D8 requires a block-aligned
            // rect. The block row pitch is the level pitch.
            if ((left & 3) || (top & 3) || (dx & 3) || (dy & 3) || ((right - left) & 3) ||
                ((bottom - top) & 3)) {
                com_ret(c, D8_ERR_INVALIDCALL);
                return;
            }
            const uint32_t row = uint32_t(right - left) / 4 * block;
            const uint32_t rows = uint32_t(bottom - top) / 4;
            const uint8_t *s =
                sbase + (uint32_t)(top / 4) * src->pitch + (uint32_t)(left / 4) * block;
            uint8_t *d = dbase + (uint32_t)(dy / 4) * dst->pitch + (uint32_t)(dx / 4) * block;
            for (uint32_t y = 0; y < rows; ++y)
                memcpy(d + (size_t)y * dst->pitch, s + (size_t)y * src->pitch, row);
        } else {
            const uint32_t row = uint32_t(right - left) * texel;
            const uint32_t rows = uint32_t(bottom - top);
            const uint8_t *s = sbase + (size_t)top * src->pitch + (size_t)left * texel;
            uint8_t *d = dbase + (size_t)dy * dst->pitch + (size_t)dx * texel;
            for (uint32_t y = 0; y < rows; ++y)
                memcpy(d + (size_t)y * dst->pitch, s + (size_t)y * src->pitch, row);
        }
    }
    // The destination content changed; any resident GPU copy is now stale.
    ++dst->d3d8_content_generation;
    com_ret(c, D8_OK);
}

#ifdef RECOMP_D3D8_WGPU
// Hand the bound stage-0 texture's level-0 CPU bytes to the renderer. Called
// immediately before each draw so a level locked after SetTexture samples its
// newest content. A null binding sends an empty block; the renderer keeps a
// white fallback bound there, but an unbound stage does not simply sample
// white. Wine's is_invalid_op (dlls/wined3d/utils.c) rewrites a stage op that
// reads TEXTURE with no texture to SELECTARG1(CURRENT), and
// DeviceState::resolve_texture_stage applies the same rule, so the stage passes
// CURRENT (the vertex diffuse at stage 0) through instead of the white texel.
//
// Returns false when the renderer rejected the bind; the diagnostic has already
// been reported through host_result() and the caller must not draw. This
// prevents a rejected bind from silently falling back to the white texture.
bool d8_sync_texture(X86 *c, ComObj *dev, uint32_t stage) {
    if (!dev || !dev->d3d8_device || stage >= 8)
        return true;
    ComObj *tex = com_get(dev->d3d8_bound_texture[stage]);
    D3d8Error err{};
    int32_t status;
    if (tex && !tex->d3d8_levels.empty()) {
        // Hand the renderer the whole mip chain (base level first). Each level
        // carries its own content generation so re-locking level N re-uploads
        // only N; `dirty` covers a lock still open at draw time.
        const uint32_t count = (uint32_t)tex->d3d8_levels.size();
        std::vector<D3d8TextureLevel> levels(count);
        for (uint32_t l = 0; l < count; ++l) {
            ComObj *level = com_get(tex->d3d8_levels[l]);
            if (!level)
                continue;
            const uint8_t *data = d8_buffer_bytes(level);
            D3d8TextureLevel &out = levels[l];
            out.level = level->d3d8_level;
            out.width = level->width;
            out.height = level->height;
            out.dirty = level->lock_count > 0 ? 1u : 0u;
            out.generation = level->d3d8_content_generation;
            out.data = data;
            out.bytes = data ? level->pixels_bytes : 0;
        }
        status = d3d8_device_set_texture(host_device(dev), stage, tex->id, tex->rmask, count,
                                         levels.data(), &err);
    } else {
        status = d3d8_device_set_texture(host_device(dev), stage, 0, 0, 0, nullptr, &err);
    }
    if (status != D3D8_STATUS_OK) {
        com_ret(c, host_result(c, status, err));
        return false;
    }
    return true;
}
#endif

// (this, PrimitiveType, StartVertex, PrimitiveCount)
void Dev_DrawPrimitive(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (!dev || !dev->d3d8_device) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *vb = com_get(dev->d3d8_stream_vb);
    const uint8_t *bytes = d8_buffer_bytes(vb);
    if (!bytes || !vb->pixels_bytes || !dev->d3d8_stream_stride) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    // The bridge composites two stages, so both bindings must be current.
    for (uint32_t stage = 0; stage < (d8_constants(dev)->pixel_shader ? 4u : 2u); ++stage)
        if (!d8_sync_texture(c, dev, stage))
            return;
    D3d8Error err{};
    int32_t status = d3d8_device_draw_primitive(host_device(dev), arg(c, 1), dev->d3d8_fvf, bytes,
                                                (uint32_t)vb->pixels_bytes, dev->d3d8_stream_stride,
                                                arg(c, 2), arg(c, 3), &err);
    com_ret(c, host_result(c, status, err));
#else
    com_ret(c, D8_ERR_INVALIDCALL);
#endif
}

// (this, PrimitiveType, MinIndex, NumVertices, StartIndex, PrimitiveCount). The
// Rust owns indexed range validation and triangle-list expansion. Guest COM
// identities and memory staging remain here.
void Dev_DrawIndexedPrimitive(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (!dev || !dev->d3d8_device) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    ComObj *vb = com_get(dev->d3d8_stream_vb);
    ComObj *ib = com_get(dev->d3d8_indices);
    uint32_t topology = arg(c, 1), min_index = arg(c, 2), num_vertices = arg(c, 3);
    uint32_t start_index = arg(c, 4), prim_count = arg(c, 5);
    const uint8_t *vbytes = d8_buffer_bytes(vb);
    const uint8_t *ibytes = d8_buffer_bytes(ib);
    uint32_t stride = dev->d3d8_stream_stride;
    // Wine's d3d8_device_DrawIndexedPrimitive returns D3D_OK immediately when
    // no index buffer is bound and submits no draw (dlls/d3d8/device.c at Wine
    // commit 455e3509b98a6919fd4ad1def4803e08c41c03b2). NumVertices and
    // MinIndex are draw hints there: only NumVertices is passed to the sysmem
    // vertex-buffer upload, and neither bounds which vertices the draw reads.
    if (!ib || !ibytes) {
        com_ret(c, D8_OK);
        return;
    }
    // A zero primitive count is a D3D_OK no-op: Wine computes an index count of
    // zero and emits a zero-length draw. The hint `num_vertices` may be zero.
    if (!prim_count) {
        com_ret(c, D8_OK);
        return;
    }
    if (!vbytes || !stride) {
        // Name which precondition failed; a bare INVALIDCALL hides whether the
        // guest drew with no stream bound or with a zero stride. Wine only
        // requires the index buffer and defers a missing stream to wined3d;
        // this renderer needs real vertex bytes, so it stays a named rejection.
        // DIVERGENCE(original): the original driver's result for an unbound
        // stream is not evidenced, so it is not guessed at here.
        LOGW("d3d8: DrawIndexedPrimitive rejected: vb=%s stride=%u prim_count=%u topology=%u "
             "fvf=%08x",
             vbytes ? "bound" : "none", stride, prim_count, topology, dev->d3d8_fvf);
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    // The bridge composites two stages, so both bindings must be current.
    for (uint32_t stage = 0; stage < (d8_constants(dev)->pixel_shader ? 4u : 2u); ++stage)
        if (!d8_sync_texture(c, dev, stage))
            return;
    D3d8Error err{};
    int32_t status = d3d8_device_draw_indexed_primitive(
        host_device(dev), topology, dev->d3d8_fvf, vbytes, vb->pixels_bytes, stride, ibytes,
        ib->pixels_bytes, ib->d3d8_buffer_format, dev->d3d8_base_vertex, min_index, num_vertices,
        start_index, prim_count, &err);
    com_ret(c, host_result(c, status, err));
#else
    com_ret(c, D8_ERR_INVALIDCALL);
#endif
}

// ---------------------------------------------------------------------------
// Vtables, in interface order. A guest dispatches by slot index, so the order
// is the ABI and may not be rearranged.
// ---------------------------------------------------------------------------
#include "d3d8_interfaces.inc"

// The DLL's one export. Not a COM method: the guest calls it directly.
void d3d8_Direct3DCreate8(X86 *c) {
    ComObj *o = com_new(K_D3D8);
    uint32_t view = o ? com_view(o, IF_D3D8) : 0;
    if (!view) {
        if (o)
            com_release(o);
        set_eax(c, 0);
        return;
    }
    LOGW("d3d8: Direct3DCreate8(SDK %u) -> %08x", arg(c, 0), view);
    set_eax(c, view);
}

static const ImportShim g_d3d8_exports[] = {
    {"d3d8.dll", "Direct3DCreate8", 1, d3d8_Direct3DCreate8},
    // Railroad Tycoon 3 loads its shipping d8tx.dll wrapper, whose only export
    // forwards to Direct3DCreate8. Treat it as the d3d8.dll entry point.
    {"d8tx.dll", "Direct3DCreate8", 1, d3d8_Direct3DCreate8},
};

void device_destroy(ComObj *o) {
    device_discard_implicit_surfaces(o);
#ifdef RECOMP_D3D8_WGPU
    if (o->d3d8_device) {
        d3d8_device_destroy(static_cast<D3d8Device *>(o->d3d8_device));
        o->d3d8_device = nullptr;
    }
#endif
    live_devices.erase(o);
    if (ComObj *factory = com_get(o->d3d8_factory))
        com_release(factory);
    o->d3d8_factory = 0;
}

} // namespace

void d3d8_register() {
    static bool done = false;
    if (done)
        return;
    done = true;

    com_define(IF_D3D8, "d3d8.dll", "IDirect3D8", g_d3d8, std::size(g_d3d8));
    com_define(IF_D3D8DEVICE, "d3d8.dll", "IDirect3DDevice8", g_device8, std::size(g_device8));
    com_define(IF_D3D8SURFACE8, "d3d8.dll", "IDirect3DSurface8", g_surface8, std::size(g_surface8));
    com_define(IF_D3D8TEXTURE8, "d3d8.dll", "IDirect3DTexture8", g_texture8, std::size(g_texture8));
    com_define(IF_D3D8VERTEXBUFFER8, "d3d8.dll", "IDirect3DVertexBuffer8", g_vertexbuffer8,
               std::size(g_vertexbuffer8));
    com_define(IF_D3D8INDEXBUFFER8, "d3d8.dll", "IDirect3DIndexBuffer8", g_indexbuffer8,
               std::size(g_indexbuffer8));
    com_bind(IF_D3D8, K_D3D8);
    com_bind(IF_D3D8DEVICE, K_D3D8DEVICE);
    com_bind(IF_D3D8SURFACE8, K_D3D8SURFACE);
    com_bind(IF_D3D8TEXTURE8, K_D3D8TEXTURE);
    com_bind(IF_D3D8VERTEXBUFFER8, K_D3D8VERTEXBUFFER);
    com_bind(IF_D3D8INDEXBUFFER8, K_D3D8INDEXBUFFER);
    com_register_iid(IF_D3D8, IID_IDirect3D8_);
    com_register_iid(IF_D3D8DEVICE, IID_IDirect3DDevice8_);
    com_register_iid(IF_D3D8SURFACE8, IID_IDirect3DSurface8_);
    com_register_iid(IF_D3D8TEXTURE8, IID_IDirect3DTexture8_);
    com_register_iid(IF_D3D8VERTEXBUFFER8, IID_IDirect3DVertexBuffer8_);
    com_register_iid(IF_D3D8INDEXBUFFER8, IID_IDirect3DIndexBuffer8_);
    com_set_destructor(K_D3D8SURFACE, surface_destroy);
    com_set_ref_hook(K_D3D8SURFACE, surface_refs_changed);
    com_set_destructor(K_D3D8TEXTURE, texture_destroy);
    com_set_destructor(K_D3D8VERTEXBUFFER, buffer_destroy);
    com_set_destructor(K_D3D8INDEXBUFFER, buffer_destroy);
    com_set_destructor(K_D3D8DEVICE, device_destroy);
    imports_register(g_d3d8_exports, std::size(g_d3d8_exports));
}

// Native-override seam (runtime/native_seam.h): the guest reached the point
// between its 3D scene and its overlay. Applies the optional scene
// post-process to the live device; without the wgpu renderer there is nothing
// to apply and a request for one fails by name.
extern "C" int d3d8_scene_boundary(X86 *c, uint32_t mode) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = live_devices.empty() ? nullptr : *live_devices.begin();
    if (dev && dev->d3d8_device) {
        D3d8Error err{};
        // RECOMP_D3D8_DUMP_SCENE=N saves the backbuffer before and after every
        // Nth boundary that runs a post-process, plus an amplified difference
        // image, into RECOMP_DUMP_FRAME_DIR. Diagnostic only.
        static const uint32_t dump_every = [] {
            const char *v = getenv("RECOMP_D3D8_DUMP_SCENE");
            return v ? uint32_t(strtoul(v, nullptr, 10)) : 0u;
        }();
        static uint32_t boundary_serial = 0;
        const bool dump =
            mode && dump_every && dx_dump_enabled() && (boundary_serial++ % dump_every) == 0;
        const size_t pixels = size_t(dev->d3d8_width) * dev->d3d8_height;
        std::vector<uint8_t> before;
        if (dump) {
            before.resize(pixels * 4);
            uint32_t got = 0;
            if (d3d8_device_read_pixels(host_device(dev), before.data(), uint32_t(before.size()),
                                        &got, &err) ||
                got != before.size())
                before.clear();
        }
        int32_t status = d3d8_device_scene_boundary(host_device(dev), mode, &err);
        if (status == D3D8_STATUS_OK && !before.empty()) {
            std::vector<uint8_t> after(pixels * 4), diff(pixels * 4, 255);
            uint32_t got = 0;
            if (!d3d8_device_read_pixels(host_device(dev), after.data(), uint32_t(after.size()),
                                         &got, &err) &&
                got == after.size()) {
                size_t changed = 0;
                for (size_t i = 0; i < pixels; ++i) {
                    int d = 0;
                    for (int k = 0; k < 3; ++k)
                        d = std::max(d, std::abs(int(before[i * 4 + k]) - int(after[i * 4 + k])));
                    changed += d != 0;
                    uint8_t v = uint8_t(std::min(255, d * 8));
                    diff[i * 4] = diff[i * 4 + 1] = diff[i * 4 + 2] = v;
                }
                char name[48];
                static uint32_t dumped = 0;
                snprintf(name, sizeof name, "scene_%04u_before", dumped);
                dx_dump_named_rgba(name, before.data(), dev->d3d8_width, dev->d3d8_height);
                snprintf(name, sizeof name, "scene_%04u_after", dumped);
                dx_dump_named_rgba(name, after.data(), dev->d3d8_width, dev->d3d8_height);
                snprintf(name, sizeof name, "scene_%04u_diff", dumped);
                dx_dump_named_rgba(name, diff.data(), dev->d3d8_width, dev->d3d8_height);
                fprintf(stderr, "d3d8: scene dump %u: %zu of %zu pixels changed\n", dumped++,
                        changed, pixels);
            }
        }
        return host_result(c, status, err) == D8_OK ? 0 : -1;
    }
    if (mode == 0)
        return 0;
    fprintf(stderr, "d3d8: scene post-process %u requested with no live device\n", mode);
#else
    if (mode == 0)
        return 0;
    fprintf(stderr, "d3d8: scene post-process %u requires the wgpu renderer\n", mode);
#endif
    fflush(stderr);
    imports_unsupported(c);
    return -1;
}

void d3d8_reset() {
#ifdef RECOMP_D3D8_WGPU
    for (ComObj *dev : live_devices) {
        if (dev->d3d8_device)
            d3d8_device_destroy(static_cast<D3d8Device *>(dev->d3d8_device));
        dev->d3d8_device = nullptr;
    }
#endif
    live_devices.clear();
    // Guest allocations were discarded by mem_init; release only host storage.
    while (!live_resources.empty())
        storage_destroy(*live_resources.begin());
    adapter_cache() = {};
}
