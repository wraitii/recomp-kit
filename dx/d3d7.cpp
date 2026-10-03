// d3d7.cpp - Direct3D 7: the IDirect3D7 factory, the IDirect3DDevice7 state
// store and IDirect3DVertexBuffer7.
//
// This is the stage-2a front end. It gets the guest through OpenD3D (0x82cd80)
// and keeps a faithful record of everything the engine sets, because the
// engine caches every render and texture-stage state itself and reads it back
// through GetRenderState/GetTextureStageState (fn_0082c8f0 at 0x82c8f0 walks
// all 256 render states and all 8 stages x 256 stage states once at startup).
//
// Nothing here rasterizes. Anything that would need rendering -- draws, Clear
// on the 3D target, VB draws, state blocks, ProcessVertices -- stops the run
// with an abort that names the interface and method. The state methods are
// real; the unimplemented ones fail loudly rather than returning zero.
//
// The seam for a later stage: a D3D7 device holds a D3d7DeviceState and every
// Set* updates it. When the wgpu renderer is wired in, each Set* should call
// the corresponding Rust d3d8 ABI entry (d3d8_device_set_render_state,
// set_texture_stage_state, set_transform, set_viewport, set_material, set_light,
// light_enable, set_texture) after recording, translating D3D7 ids to D3D8 ids
// (D3D7 world/view/projection are 1/2/3, D3D8 uses 256/257/258). Draws and
// Clear stay aborted until the renderer and the 16bpp present boundary exist.
#include "com.h"
#include "ddraw.h"
#include "dxtypes.h"
#include "../runtime/guest.h"
#include "../runtime/memory.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <array>

namespace {

// ---------------------------------------------------------------- IIDs
// The device class GUIDs. Both are offered because both are real host devices:
// the HAL device delegates vertex processing to the engine (the engine submits
// XYZRHW vertices and never calls CreateVertexBuffer when its own HardwareTnL
// option is off), while the TnL device exposes D3DDEVCAPS_HWTRANSFORMANDLIGHT.
// GetCaps reports exactly the class that was created, so the engine's own
// detection (it compares deviceGUID against IID_IDirect3DTnLHalDevice) sees a
// consistent picture. The game's default config leaves HardwareTnL unset, so
// the default device on this host is the HAL one.
#define GUID_BYTES(a, b, c, d0, d1, d2, d3, d4, d5, d6, d7)                                        \
    {(uint8_t)((a) & 0xff),                                                                        \
     (uint8_t)(((a) >> 8) & 0xff),                                                                 \
     (uint8_t)(((a) >> 16) & 0xff),                                                                \
     (uint8_t)(((a) >> 24) & 0xff),                                                                \
     (uint8_t)((b) & 0xff),                                                                        \
     (uint8_t)(((b) >> 8) & 0xff),                                                                 \
     (uint8_t)((c) & 0xff),                                                                        \
     (uint8_t)(((c) >> 8) & 0xff),                                                                 \
     d0,                                                                                           \
     d1,                                                                                           \
     d2,                                                                                           \
     d3,                                                                                           \
     d4,                                                                                           \
     d5,                                                                                           \
     d6,                                                                                           \
     d7}

const uint8_t IID_IDirect3D7_[16] =
    GUID_BYTES(0xF5049E77, 0x4861, 0x11D2, 0xA4, 0x07, 0x00, 0xA0, 0xC9, 0x06, 0x29, 0xA8);
const uint8_t IID_IDirect3DHALDevice_[16] =
    GUID_BYTES(0x84E63DE0, 0x46AA, 0x11CF, 0x81, 0x6F, 0x00, 0x00, 0xC0, 0x20, 0x15, 0x6E);
const uint8_t IID_IDirect3DTnLHalDevice_[16] =
    GUID_BYTES(0xF5049E78, 0x4861, 0x11D2, 0xA4, 0x07, 0x00, 0xA0, 0xC9, 0x06, 0x29, 0xA8);
const uint8_t IID_IDirect3DVertexBuffer7_[16] =
    GUID_BYTES(0xF5049E7D, 0x4861, 0x11D2, 0xA4, 0x07, 0x00, 0xA0, 0xC9, 0x06, 0x29, 0xA8);

bool guid_eq(uint32_t guest_addr, const uint8_t *want) {
    return guest_addr && gm_valid(guest_addr, 16) && memcmp(gm_ptr(guest_addr), want, 16) == 0;
}

// ---------------------------------------------------------------- scratch
uint32_t g_scratch = 0, g_scratch_size = 0;
uint32_t scratch(uint32_t n) {
    if (g_scratch_size < n) {
        if (g_scratch)
            heap_free(g_scratch);
        g_scratch = heap_alloc(n, true, 16);
        g_scratch_size = g_scratch ? n : 0;
    }
    if (g_scratch)
        memset(gm_ptr(g_scratch), 0, g_scratch_size);
    return g_scratch;
}

// ---------------------------------------------------------------- loud aborts
// Every slot that would need rendering lands here, naming itself. Returning a
// success would let the engine believe work happened; returning an error would
// be swallowed by callers that do not check (the engine ignores Clear's and
// BeginScene's HRESULTs), so the only honest option is to stop.
[[noreturn]] void needs_render(const char *iface, const char *method) {
    LOGW("d3d7: %s::%s is not implemented (needs rendering); stopping", iface, method);
    fflush(stderr);
    abort();
}

// ---------------------------------------------------------------- accessors
ComObj *this_d3d7(X86 *c) {
    ComObj *o = com_this_arg(c);
    return (o && o->kind == K_DDRAW) ? o : nullptr;
}
ComObj *this_device7(X86 *c) {
    ComObj *o = com_this_arg(c);
    return (o && o->kind == K_D3D7DEVICE && o->d3d7) ? o : nullptr;
}
ComObj *this_vb7(X86 *c) {
    ComObj *o = com_this_arg(c);
    return (o && o->kind == K_D3D7VB) ? o : nullptr;
}

// ---------------------------------------------------------------- FVF stride
// Byte width of one vertex for the FVF layouts the engine uses. Zero means the
// layout is not decoded here and CreateVertexBuffer refuses it rather than
// guessing a size the engine would then Lock past.
uint32_t fvf_stride(uint32_t fvf) {
    uint32_t n = 0;
    switch (fvf & 0x00e) { // D3DFVF_POSITION_MASK
    case 0x002:            // XYZ
        n += 12;
        break;
    case 0x004: // XYZRHW
        n += 16;
        break;
    default:
        return 0; // XYZB* blend-weight layouts are not used by this game.
    }
    if (fvf & 0x010)
        n += 12; // NORMAL
    if (fvf & 0x020)
        n += 4; // PSIZE
    if (fvf & 0x040)
        n += 4; // DIFFUSE
    if (fvf & 0x080)
        n += 4; // SPECULAR
    uint32_t texcount = (fvf >> 8) & 0xf;
    for (uint32_t i = 0; i < texcount; ++i) {
        uint32_t size = (fvf >> (16 + i * 2)) & 0x3;
        n += size == 0 ? 8 : size * 4; // 0 means the SDK's default of 2 floats
    }
    return n;
}

// ---------------------------------------------------------------- defaults
// The documented D3D7 render-state defaults. The engine reads every one of
// these back at 0x82c8f0, so a value that is not recorded here is a value the
// engine's cache would hold as "unset" while the device held something else.
// Values are the Direct3D 7 SDK defaults; the ones the engine explicitly sets
// afterwards are listed only for completeness.
uint32_t render_state_default(uint32_t s) {
    switch (s) {
    case 7: // ZENABLE: D3DZB_FALSE
        return 0;
    case 8: // FILLMODE: SOLID
        return 3;
    case 9: // SHADEMODE: GOURAUD
        return 2;
    case 14: // ZWRITEENABLE: TRUE
        return 1;
    case 16: // LASTPIXEL: TRUE
        return 1;
    case 19: // SRCBLEND: ONE
        return 2;
    case 20: // DESTBLEND: ZERO
        return 1;
    case 22: // CULLMODE: CCW
        return 3;
    case 23: // ZFUNC: LESSEQUAL
        return 4;
    case 25: // ALPHAFUNC: ALWAYS
        return 8;
    case 34: // FOGCOLOR: 0
        return 0;
    case 35: // FOGTABLEMODE: NONE
        return 0;
    case 36: // FOGSTART: 0
        return 0;
    case 37: // FOGEND: 1.0
        return 0x3f800000u;
    case 38: // FOGDENSITY: 1.0
        return 0x3f800000u;
    case 52: // STENCILENABLE: FALSE
        return 0;
    case 56: // STENCILFUNC: ALWAYS
        return 8;
    case 58: // STENCILMASK: 0xffffffff
        return 0xffffffffu;
    case 59: // STENCILWRITEMASK: 0xffffffff
        return 0xffffffffu;
    case 60: // TEXTUREFACTOR: 0xffffffff
        return 0xffffffffu;
    case 136: // CLIPPING: TRUE
        return 1;
    case 137: // LIGHTING: TRUE
        return 1;
    case 139: // AMBIENT: 0
        return 0;
    case 140: // FOGVERTEXMODE: NONE
        return 0;
    case 141: // COLORVERTEX: TRUE
        return 1;
    case 142: // LOCALVIEWER: TRUE
        return 1;
    case 143: // NORMALIZENORMALS: FALSE
        return 0;
    case 145: // DIFFUSEMATERIALSOURCE: COLOR1
        return 0;
    case 146: // SPECULARMATERIALSOURCE: COLOR2
        return 1;
    case 147: // AMBIENTMATERIALSOURCE: MATERIAL
        return 2;
    case 148: // EMISSIVEMATERIALSOURCE: MATERIAL
        return 2;
    case 151: // VERTEXBLEND: DISABLE
        return 0;
    case 152: // CLIPPLANEENABLE: 0
        return 0;
    case 153: // SOFTWAREVERTEXPROCESSING: FALSE
        return 0;
    case 154: // POINTSIZE: 1.0
        return 0x3f800000u;
    case 158: // POINTSCALE_A: 1.0
        return 0x3f800000u;
    case 159: // POINTSCALE_B: 0
        return 0;
    case 160: // POINTSCALE_C: 0
        return 0;
    case 161: // MULTISAMPLEANTIALIAS: TRUE
        return 1;
    case 162: // MULTISAMPLEMASK: 0xffffffff
        return 0xffffffffu;
    case 164: // PATCHSEGMENTS: 1.0
        return 0x3f800000u;
    case 168: // COLORWRITEENABLE: RGB
        return 0x0000000fu;
    case 170: // TWEENFACTOR: 0
        return 0;
    case 171: // BLENDOP: ADD
        return 1;
    default:
        return 0;
    }
}

// The documented D3DTSS defaults. Stage zero blends texture with the diffuse
// current colour; every later stage is disabled. Texture coordinates are the
// SDK default (2 floats), addressing wraps.
uint32_t tss_default(uint32_t stage, uint32_t type) {
    switch (type) {
    case 1: // COLOROP
        return stage == 0 ? 4u /* MODULATE */ : 1u /* DISABLE */;
    case 2:       // COLORARG1
        return 2; // TEXTURE
    case 3:       // COLORARG2
        return 1; // CURRENT
    case 4:       // ALPHAOP
        return stage == 0 ? 2u /* SELECTARG1 */ : 1u /* DISABLE */;
    case 5:       // ALPHAARG1
        return 2; // TEXTURE
    case 6:       // ALPHAARG2
        return 1; // CURRENT
    case 11:      // TEXCOORDINDEX
        return 0;
    case 12:      // ADDRESS
    case 13:      // ADDRESSU
    case 14:      // ADDRESSV
        return 1; // WRAP
    case 16:      // MAGFILTER
    case 17:      // MINFILTER
        return 2; // LINEAR
    case 18:      // MIPFILTER
        return 0; // NONE
    default:
        return 0;
    }
}

void init_device_state(D3d7DeviceState &s) {
    for (uint32_t i = 0; i < 256; ++i)
        s.render_state[i] = render_state_default(i);
    for (uint32_t stage = 0; stage < 8; ++stage)
        for (uint32_t type = 0; type < 256; ++type)
            s.tss[stage][type] = tss_default(stage, type);
    for (uint32_t t = 0; t < 256; ++t) {
        memset(s.transform[t], 0, sizeof s.transform[t]);
        s.transform[t][0] = s.transform[t][5] = s.transform[t][10] = s.transform[t][15] = 1.0f;
        s.transform_set[t] = false;
    }
    memset(s.viewport, 0, sizeof s.viewport);
    s.viewport[5] = 1.0f; // dvMaxZ
    // Default material: opaque white diffuse, no ambient/specular/emissive.
    memset(s.material, 0, sizeof s.material);
    s.material[0] = s.material[1] = s.material[2] = s.material[3] = 1.0f;
    memset(s.light, 0, sizeof s.light);
    memset(s.light_enable, 0, sizeof s.light_enable);
    memset(s.texture, 0, sizeof s.texture);
    s.in_scene = false;
    s.d3d_obj = 0;
    s.render_target = 0;
    memset(s.device_guid, 0, sizeof s.device_guid);
    s.tnl = false;
}

// ---------------------------------------------------------------- device caps
// Fills a D3DDEVICEDESC7. "Host truth" means it follows from what the host
// really is; "chosen" means the value is a reasonable stand-in the renderer
// must be able to keep when the state is forwarded.
void fill_device_desc7(uint32_t addr, const uint8_t guid[16], bool tnl) {
    gm_zero(addr, D3DDEVICEDESC7_SIZE);
    // Host truth: the class decides the TnL bit; the arena holds system-memory
    // textures, hardware rasterizes. Chosen: the primitive and draw caps below
    // are the fixed-function set wgpu can keep.
    uint32_t caps = D3DDEVCAPS_TEXTURENONLOCALVIDMEM | D3DDEVCAPS_HWRASTERIZATION |
                    D3DDEVCAPS_DRAWPRIMITIVES2 | D3DDEVCAPS_DRAWPRIMTLVERTEX |
                    D3DDEVCAPS_TEXTUREVIDEOMEMORY | D3DDEVCAPS_TLVERTEXSYSTEMMEMORY;
    if (tnl)
        caps |= D3DDEVCAPS_HWTRANSFORMANDLIGHT;
    wr32(addr + D3DDD7_OFF_dwDevCaps, caps);
    wr32(addr + D3DDD7_OFF_dpcLineCaps + D3DPC_OFF_dwSize, D3DPRIMCAPS_SIZE);
    wr32(addr + D3DDD7_OFF_dpcTriCaps + D3DPC_OFF_dwSize, D3DPRIMCAPS_SIZE);
    // Host truth: the display mode is 16bpp and the z-buffer the engine asks
    // for is 16-bit, so both bit-depth masks advertise 16 only.
    wr32(addr + D3DDD7_OFF_dwDeviceRenderBitDepth, DDBD_16);
    wr32(addr + D3DDD7_OFF_dwDeviceZBufferBitDepth, DDBD_16);
    // Chosen: the engine creates 256x256 textures and DXT ones it makes itself.
    wr32(addr + D3DDD7_OFF_dwMinTextureWidth, 1);
    wr32(addr + D3DDD7_OFF_dwMinTextureHeight, 1);
    wr32(addr + D3DDD7_OFF_dwMaxTextureWidth, 2048);
    wr32(addr + D3DDD7_OFF_dwMaxTextureHeight, 2048);
    wr32(addr + D3DDD7_OFF_dwMaxTextureRepeat, 0);
    wr32(addr + D3DDD7_OFF_dwMaxTextureAspectRatio, 0);
    wr32(addr + D3DDD7_OFF_dwMaxAnisotropy, 1);
    wr32(addr + D3DDD7_OFF_dwStencilCaps, 0);
    wr32(addr + D3DDD7_OFF_dwFVFCaps, 0x100);
    // Chosen: the engine writes at most three texture stages; advertise the
    // D3D7 maximum so it is not refused, with two simultaneous textures.
    wr32(addr + D3DDD7_OFF_dwTextureOpCaps, 0x0000ffffu);
    wr16(addr + D3DDD7_OFF_wMaxTextureBlendStages, 8);
    wr16(addr + D3DDD7_OFF_wMaxSimultaneousTextures, 2);
    wr32(addr + D3DDD7_OFF_dwMaxActiveLights, 8);
    wr32(addr + D3DDD7_OFF_dvMaxVertexW, 0x41200000u); // 10.0f, chosen
    memcpy(gm_ptr(addr + D3DDD7_OFF_deviceGUID), guid, 16);
    wr16(addr + D3DDD7_OFF_wMaxUserClipPlanes, 0);
    wr16(addr + D3DDD7_OFF_wMaxVertexBlendMatrices, 0);
    wr32(addr + D3DDD7_OFF_dwVertexProcessingCaps, 0);
}

// ---------------------------------------------------------------- factory
void D3D7_EnumDevices(X86 *c) {
    ComObj *d3d = this_d3d7(c);
    uint32_t cb = arg(c, 1), ctx = arg(c, 2);
    if (!d3d || !cb) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    // One device, the HAL class: the guest's default path. The callback is the
    // D3D7 form (description, name, D3DDEVICEDESC7*, ctx).
    uint32_t desc7 = scratch(256 + D3DDEVICEDESC7_SIZE);
    if (!desc7) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    uint32_t desc_str = desc7;
    uint32_t name_str = desc7 + 128;
    gm_put_str(desc_str, "Primary Display Driver (HAL)", 128);
    gm_put_str(name_str, "display", 128);
    fill_device_desc7(desc7 + 256, IID_IDirect3DHALDevice_, false);
    guest_call(c, cb, desc_str, name_str, desc7 + 256, ctx);
    com_ret(c, D3D_OK_);
}

void D3D7_EnumZBufferFormats(X86 *c) {
    ComObj *d3d = this_d3d7(c);
    uint32_t guid = arg(c, 1), cb = arg(c, 2), ctx = arg(c, 3);
    if (!d3d || !cb)
        com_ret(c, DDERR_INVALIDPARAMS);
    else if (!(guid_eq(guid, IID_IDirect3DHALDevice_) || guid_eq(guid, IID_IDirect3DTnLHalDevice_)))
        com_ret(c, DDERR_NOTFOUND);
    else {
        // One DDPF_ZBUFFER entry, 16-bit. The display mode is 16bpp and the
        // callback at 0x82b370 records dwRGBBitCount into DAT_00c386d8 (as
        // 16 or not), then OpenD3D creates whichever format first succeeds.
        // A 16-bit z-buffer is therefore the format the original run used;
        // reporting a 24/32-bit one would set DAT_00c386d8=0 and change the
        // engine's depth-buffer reporting. The callback accepts any
        // DDPF_ZBUFFER entry, so the single justified entry is enough.
        uint32_t pf = scratch(DDPF_SIZE);
        if (!pf)
            com_ret(c, E_OUTOFMEMORY);
        else {
            wr32(pf + DDPF_OFF_dwSize, DDPF_SIZE);
            wr32(pf + DDPF_OFF_dwFlags, DDPF_ZBUFFER);
            wr32(pf + DDPF_OFF_dwFourCC, 0);
            wr32(pf + DDPF_OFF_dwRGBBitCount, 16);
            wr32(pf + 0x10, 0);      // dwStencilBitDepth (union with RBitMask)
            wr32(pf + 0x14, 0xffff); // dwZBitMask (union with GBitMask)
            wr32(pf + 0x18, 0);      // dwStencilBitMask
            wr32(pf + 0x1c, 0);
            guest_call(c, cb, pf, ctx);
            com_ret(c, D3D_OK_);
        }
    }
}

void D3D7_EvictManagedTextures(X86 *c) {
    // No host-side managed texture cache exists yet; there is nothing to drop.
    // When textures forward to wgpu this must evict the upload cache by
    // revision (d3d.cpp's EvictManagedTextures is the model).
    com_ret(c, this_d3d7(c) ? D3D_OK_ : DDERR_INVALIDOBJECT);
}

void D3D7_CreateVertexBuffer(X86 *c) {
    ComObj *d3d = this_d3d7(c);
    uint32_t desc = arg(c, 1), out = arg(c, 2), flags = arg(c, 3);
    (void)flags;
    if (!d3d || !desc || !out || !gm_valid(desc, D3DVERTEXBUFFERDESC_SIZE) || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    uint32_t fvf = rd32(desc + D3DVBD_OFF_dwFVF);
    uint32_t count = rd32(desc + D3DVBD_OFF_dwNumVertices);
    uint32_t stride = fvf_stride(fvf);
    if (!stride || !count || (uint64_t)stride * count > 0xffffffffu) {
        log_once("d3d7.vb.format", "d3d7: CreateVertexBuffer refuses FVF %08x (%u verts)", fvf,
                 count);
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    uint32_t bytes = stride * count;
    uint32_t data = heap_alloc(bytes, true, 16);
    if (!data) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    // No count*stride overflow: bytes is a 32-bit value that fits the arena
    // because heap_alloc refuses anything larger.
    ComObj *vb = com_new(K_D3D7VB);
    vb->pixels = data;
    vb->pixels_bytes = bytes;
    vb->owns_pixels = true;
    vb->vb_fvf = fvf;
    vb->vb_num_vertices = count;
    vb->vb_caps = rd32(desc + D3DVBD_OFF_dwCaps);
    com_out_ptr(out, com_view(vb, IF_D3DVERTEXBUFFER7));
    LOGV("d3d7: created a vertex buffer FVF %08x, %u vertices, %u bytes", fvf, count, bytes);
    com_ret(c, D3D_OK_);
}

void D3D7_CreateDevice(X86 *c) {
    ComObj *d3d = this_d3d7(c);
    uint32_t guid = arg(c, 1), surf = arg(c, 2), out = arg(c, 3);
    if (!d3d || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    com_out_ptr(out, 0);
    ComObj *target = surf ? com_this(surf) : nullptr;
    if (!target || target->kind != K_SURFACE) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    bool tnl = guid_eq(guid, IID_IDirect3DTnLHalDevice_);
    bool hal = guid_eq(guid, IID_IDirect3DHALDevice_);
    if (!tnl && !hal) {
        log_once("d3d7.device.guid", "d3d7: CreateDevice for an unrecognised device class");
        com_ret(c, DDERR_NOTFOUND);
        return;
    }
    ComObj *dev = com_new(K_D3D7DEVICE);
    dev->d3d7 = std::make_shared<D3d7DeviceState>();
    init_device_state(*dev->d3d7);
    dev->d3d7->d3d_obj = d3d->id;
    dev->d3d7->render_target = target->id;
    memcpy(dev->d3d7->device_guid, tnl ? IID_IDirect3DTnLHalDevice_ : IID_IDirect3DHALDevice_, 16);
    dev->d3d7->tnl = tnl;
    com_addref(target);
    com_out_ptr(out, com_view(dev, IF_D3DDEVICE7));
    LOGV("d3d7: created a %s device on the %ux%ux%u surface #%u", tnl ? "TnL" : "HAL",
         target->width, target->height, target->bpp, target->id);
    com_ret(c, D3D_OK_);
}

// ---------------------------------------------------------------- device
void Device7_GetCaps(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, D3DDEVICEDESC7_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    fill_device_desc7(out, dev->d3d7->device_guid, dev->d3d7->tnl);
    com_ret(c, D3D_OK_);
}

void Device7_EnumTextureFormats(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t cb = arg(c, 1), ctx = arg(c, 2);
    if (!dev || !cb) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    // The engine's callback at 0x85dc20 keeps the last 16-bit R5G6B5, the last
    // 16-bit A4R4G4B4 and the last 32-bit ARGB it sees, then creates every
    // surface from those globals. Report only the formats the host will really
    // decode when the renderer is wired in: R5G6B5, A4R4G4B4 and A8R8G8B8.
    // DXT1/DXT3 are deliberately absent: wgpu requests no TEXTURE_COMPRESSION_BC
    // feature, so a DXT texture could not be uploaded. The engine builds DXT
    // surfaces itself through DirectDraw CreateSurface, which this shim
    // accepts as guest bytes; only the D3D7 texture *format* list omits them,
    // so nothing here promises a compressed texture it cannot honor.
    struct Fmt {
        uint32_t flags, bits, r, g, b, a;
    };
    // Order is fixed and asserted by the tests: R5G6B5, A4R4G4B4, A8R8G8B8.
    const Fmt fmts[] = {
        {DDPF_RGB, 16, 0x7c00, 0x03e0, 0x001f, 0},                                   // R5G6B5
        {DDPF_RGB | DDPF_ALPHAPIXELS, 16, 0x0f00, 0x00f0, 0x000f, 0xf000},           // A4R4G4B4
        {DDPF_RGB | DDPF_ALPHAPIXELS, 32, 0xff0000, 0x00ff00, 0x0000ff, 0xff000000}, // A8R8G8B8
    };
    for (const Fmt &f : fmts) {
        uint32_t pf = scratch(DDPF_SIZE);
        if (!pf)
            break;
        wr32(pf + DDPF_OFF_dwSize, DDPF_SIZE);
        wr32(pf + DDPF_OFF_dwFlags, f.flags);
        wr32(pf + DDPF_OFF_dwFourCC, 0);
        wr32(pf + DDPF_OFF_dwRGBBitCount, f.bits);
        wr32(pf + DDPF_OFF_dwRBitMask, f.r);
        wr32(pf + DDPF_OFF_dwGBitMask, f.g);
        wr32(pf + DDPF_OFF_dwBBitMask, f.b);
        wr32(pf + DDPF_OFF_dwRGBAlphaBitMask, f.a);
        if (guest_call(c, cb, pf, ctx) != DDENUMRET_OK)
            break;
    }
    com_ret(c, D3D_OK_);
}

void Device7_BeginScene(X86 *c) {
    ComObj *dev = this_device7(c);
    if (!dev) {
        com_ret(c, DDERR_INVALIDOBJECT);
        return;
    }
    if (dev->d3d7->in_scene) {
        com_ret(c, D3DERR_SCENEINSCENE);
        return;
    }
    dev->d3d7->in_scene = true;
    com_ret(c, D3D_OK_);
}

void Device7_EndScene(X86 *c) {
    ComObj *dev = this_device7(c);
    if (!dev) {
        com_ret(c, DDERR_INVALIDOBJECT);
        return;
    }
    if (!dev->d3d7->in_scene) {
        com_ret(c, D3DERR_SCENENOTINSCENE);
        return;
    }
    dev->d3d7->in_scene = false;
    com_ret(c, D3D_OK_);
}

void Device7_GetDirect3D(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    ComObj *d3d = com_get(dev->d3d7->d3d_obj);
    if (!d3d) {
        com_out_ptr(out, 0);
        com_ret(c, DDERR_INVALIDOBJECT);
        return;
    }
    com_addref(d3d);
    com_out_ptr(out, com_view(d3d, IF_D3D7));
    com_ret(c, D3D_OK_);
}

void Device7_SetTransform(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t state = arg(c, 1), matrix = arg(c, 2);
    if (!dev || !state || state >= 256 || !matrix || !gm_valid(matrix, 64)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(dev->d3d7->transform[state], gm_ptr(matrix), 64);
    dev->d3d7->transform_set[state] = true;
    com_ret(c, D3D_OK_);
}

void Device7_GetTransform(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t state = arg(c, 1), out = arg(c, 2);
    if (!dev || !state || state >= 256 || !out || !gm_valid(out, 64)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(gm_ptr(out), dev->d3d7->transform[state], 64);
    com_ret(c, D3D_OK_);
}

void Device7_SetViewport(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t vp = arg(c, 1);
    if (!dev || !vp || !gm_valid(vp, D3DVIEWPORT7_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(dev->d3d7->viewport, gm_ptr(vp), sizeof dev->d3d7->viewport);
    com_ret(c, D3D_OK_);
}

void Device7_GetViewport(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, D3DVIEWPORT7_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(gm_ptr(out), dev->d3d7->viewport, sizeof dev->d3d7->viewport);
    com_ret(c, D3D_OK_);
}

void Device7_SetMaterial(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t m = arg(c, 1);
    if (!dev || !m || !gm_valid(m, D3DMATERIAL7_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(dev->d3d7->material, gm_ptr(m), sizeof dev->d3d7->material);
    com_ret(c, D3D_OK_);
}

void Device7_GetMaterial(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, D3DMATERIAL7_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(gm_ptr(out), dev->d3d7->material, sizeof dev->d3d7->material);
    com_ret(c, D3D_OK_);
}

void Device7_SetLight(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t idx = arg(c, 1), data = arg(c, 2);
    if (!dev || idx >= 8 || !data || !gm_valid(data, D3DLIGHT7_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(dev->d3d7->light, gm_ptr(data), sizeof dev->d3d7->light);
    com_ret(c, D3D_OK_);
}

void Device7_GetLight(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t idx = arg(c, 1), out = arg(c, 2);
    if (!dev || idx >= 8 || !out || !gm_valid(out, D3DLIGHT7_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    memcpy(gm_ptr(out), dev->d3d7->light, sizeof dev->d3d7->light);
    com_ret(c, D3D_OK_);
}

void Device7_SetRenderState(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t state = arg(c, 1), value = arg(c, 2);
    if (!dev || state >= 256) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    dev->d3d7->render_state[state] = value;
    com_ret(c, D3D_OK_);
}

void Device7_GetRenderState(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t state = arg(c, 1), out = arg(c, 2);
    if (!dev || state >= 256 || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    wr32(out, dev->d3d7->render_state[state]);
    com_ret(c, D3D_OK_);
}

void Device7_GetTextureStageState(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t stage = arg(c, 1), type = arg(c, 2), out = arg(c, 3);
    if (!dev || stage >= 8 || type >= 256 || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    wr32(out, dev->d3d7->tss[stage][type]);
    com_ret(c, D3D_OK_);
}

void Device7_SetTextureStageState(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t stage = arg(c, 1), type = arg(c, 2), value = arg(c, 3);
    // The engine sets type 0 twice at 0x82ccd2 (a type the D3DTSS enum does
    // not name). Refusing it would leave the engine's cache at -1 and reissue
    // the call forever, so every in-range type is stored; Get returns it.
    if (!dev || stage >= 8 || type >= 256) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    dev->d3d7->tss[stage][type] = value;
    com_ret(c, D3D_OK_);
}

void Device7_ValidateDevice(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    // The front end has no fixed-function validation to do yet, so it accepts
    // every combination. The engine probes blend modes with this; returning a
    // failing count would make it discard a mode it later needs.
    wr32(out, 1);
    com_ret(c, D3D_OK_);
}

void Device7_GetTexture(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t stage = arg(c, 1), out = arg(c, 2);
    if (!dev || stage >= 8 || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    ComObj *tex = com_get(dev->d3d7->texture[stage]);
    if (tex)
        com_addref(tex);
    wr32(out, tex ? com_view(tex, IF_DDSURFACE7) : 0);
    com_ret(c, D3D_OK_);
}

void Device7_SetTexture(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t stage = arg(c, 1), ptr = arg(c, 2);
    if (!dev || stage >= 8) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    ComObj *tex = ptr ? com_this(ptr) : nullptr;
    if (ptr && (!tex || tex->kind != K_SURFACE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    if (tex)
        com_addref(tex);
    if (ComObj *old = com_get(dev->d3d7->texture[stage]))
        com_release(old);
    dev->d3d7->texture[stage] = tex ? tex->id : 0;
    com_ret(c, D3D_OK_);
}

void Device7_LightEnable(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t idx = arg(c, 1), enable = arg(c, 2);
    if (!dev || idx >= 8) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    dev->d3d7->light_enable[idx] = enable ? 1 : 0;
    com_ret(c, D3D_OK_);
}

void Device7_GetLightEnable(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t idx = arg(c, 1), out = arg(c, 2);
    if (!dev || idx >= 8 || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    wr32(out, dev->d3d7->light_enable[idx]);
    com_ret(c, D3D_OK_);
}

void Device7_GetRenderTarget(X86 *c) {
    ComObj *dev = this_device7(c);
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    ComObj *s = com_get(dev->d3d7->render_target);
    if (s)
        com_addref(s);
    wr32(out, s ? com_view(s, IF_DDSURFACE7) : 0);
    com_ret(c, D3D_OK_);
}

// --- Loudly unimplemented device slots. Naming each one is the whole point:
// the abort's message is the diagnosis. ---
#define D3D7_ABORT(fn, method)                                                                     \
    void fn(X86 *) {                                                                               \
        needs_render("IDirect3DDevice7", method);                                                  \
    }

D3D7_ABORT(Device7_SetRenderTarget, "SetRenderTarget")
D3D7_ABORT(Device7_Clear, "Clear")
D3D7_ABORT(Device7_MultiplyTransform, "MultiplyTransform")
D3D7_ABORT(Device7_BeginStateBlock, "BeginStateBlock")
D3D7_ABORT(Device7_EndStateBlock, "EndStateBlock")
D3D7_ABORT(Device7_PreLoad, "PreLoad")
D3D7_ABORT(Device7_DrawPrimitive, "DrawPrimitive")
D3D7_ABORT(Device7_DrawIndexedPrimitive, "DrawIndexedPrimitive")
D3D7_ABORT(Device7_SetClipStatus, "SetClipStatus")
D3D7_ABORT(Device7_GetClipStatus, "GetClipStatus")
D3D7_ABORT(Device7_DrawPrimitiveStrided, "DrawPrimitiveStrided")
D3D7_ABORT(Device7_DrawIndexedPrimitiveStrided, "DrawIndexedPrimitiveStrided")
D3D7_ABORT(Device7_DrawPrimitiveVB, "DrawPrimitiveVB")
D3D7_ABORT(Device7_DrawIndexedPrimitiveVB, "DrawIndexedPrimitiveVB")
D3D7_ABORT(Device7_ComputeSphereVisibility, "ComputeSphereVisibility")
D3D7_ABORT(Device7_ApplyStateBlock, "ApplyStateBlock")
D3D7_ABORT(Device7_CaptureStateBlock, "CaptureStateBlock")
D3D7_ABORT(Device7_DeleteStateBlock, "DeleteStateBlock")
D3D7_ABORT(Device7_CreateStateBlock, "CreateStateBlock")
D3D7_ABORT(Device7_Load, "Load")
D3D7_ABORT(Device7_SetClipPlane, "SetClipPlane")
D3D7_ABORT(Device7_GetClipPlane, "GetClipPlane")
D3D7_ABORT(Device7_GetInfo, "GetInfo")

// ---------------------------------------------------------------- VB
void VB7_Lock(X86 *c) {
    ComObj *vb = this_vb7(c);
    uint32_t data_out = arg(c, 2), size_out = arg(c, 3);
    if (!vb) {
        com_ret(c, DDERR_INVALIDOBJECT);
        return;
    }
    if (data_out && !gm_valid(data_out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    if (size_out && !gm_valid(size_out, 4)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    if (data_out)
        wr32(data_out, vb->pixels);
    if (size_out)
        wr32(size_out, vb->pixels_bytes);
    ++vb->lock_count;
    com_ret(c, D3D_OK_);
}

void VB7_Unlock(X86 *c) {
    ComObj *vb = this_vb7(c);
    if (!vb) {
        com_ret(c, DDERR_INVALIDOBJECT);
        return;
    }
    if (vb->lock_count > 0)
        --vb->lock_count;
    com_ret(c, D3D_OK_);
}

void VB7_GetVertexBufferDesc(X86 *c) {
    ComObj *vb = this_vb7(c);
    uint32_t out = arg(c, 1);
    if (!vb || !out || !gm_valid(out, D3DVERTEXBUFFERDESC_SIZE)) {
        com_ret(c, DDERR_INVALIDPARAMS);
        return;
    }
    wr32(out + D3DVBD_OFF_dwSize, D3DVERTEXBUFFERDESC_SIZE);
    wr32(out + D3DVBD_OFF_dwCaps, vb->vb_caps);
    wr32(out + D3DVBD_OFF_dwFVF, vb->vb_fvf);
    wr32(out + D3DVBD_OFF_dwNumVertices, vb->vb_num_vertices);
    com_ret(c, D3D_OK_);
}

void VB7_Optimize(X86 *c) {
    // The guest buffer is already the only storage; there is no repack to do.
    com_ret(c, this_vb7(c) ? D3D_OK_ : DDERR_INVALIDOBJECT);
}

void VB7_ProcessVertices(X86 *) {
    needs_render("IDirect3DVertexBuffer7", "ProcessVertices");
}
void VB7_ProcessVerticesStrided(X86 *) {
    needs_render("IDirect3DVertexBuffer7", "ProcessVerticesStrided");
}

// ---------------------------------------------------------------- vtables
const ComMethod g_d3d7[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"EnumDevices", 3, D3D7_EnumDevices},
    {"CreateDevice", 4, D3D7_CreateDevice},
    {"CreateVertexBuffer", 4, D3D7_CreateVertexBuffer},
    {"EnumZBufferFormats", 4, D3D7_EnumZBufferFormats},
    {"EvictManagedTextures", 1, D3D7_EvictManagedTextures},
};

const ComMethod g_device7[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetCaps", 2, Device7_GetCaps},                                         // 0x0c
    {"EnumTextureFormats", 3, Device7_EnumTextureFormats},                   // 0x10
    {"BeginScene", 1, Device7_BeginScene},                                   // 0x14
    {"EndScene", 1, Device7_EndScene},                                       // 0x18
    {"GetDirect3D", 2, Device7_GetDirect3D},                                 // 0x1c
    {"SetRenderTarget", 3, Device7_SetRenderTarget},                         // 0x20
    {"GetRenderTarget", 2, Device7_GetRenderTarget},                         // 0x24
    {"Clear", 7, Device7_Clear},                                             // 0x28
    {"SetTransform", 3, Device7_SetTransform},                               // 0x2c
    {"GetTransform", 3, Device7_GetTransform},                               // 0x30
    {"SetViewport", 2, Device7_SetViewport},                                 // 0x34
    {"MultiplyTransform", 3, Device7_MultiplyTransform},                     // 0x38
    {"GetViewport", 2, Device7_GetViewport},                                 // 0x3c
    {"SetMaterial", 2, Device7_SetMaterial},                                 // 0x40
    {"GetMaterial", 2, Device7_GetMaterial},                                 // 0x44
    {"SetLight", 3, Device7_SetLight},                                       // 0x48
    {"GetLight", 3, Device7_GetLight},                                       // 0x4c
    {"SetRenderState", 3, Device7_SetRenderState},                           // 0x50
    {"GetRenderState", 3, Device7_GetRenderState},                           // 0x54
    {"BeginStateBlock", 1, Device7_BeginStateBlock},                         // 0x58
    {"EndStateBlock", 2, Device7_EndStateBlock},                             // 0x5c
    {"PreLoad", 2, Device7_PreLoad},                                         // 0x60
    {"DrawPrimitive", 6, Device7_DrawPrimitive},                             // 0x64
    {"DrawIndexedPrimitive", 8, Device7_DrawIndexedPrimitive},               // 0x68
    {"SetClipStatus", 2, Device7_SetClipStatus},                             // 0x6c
    {"GetClipStatus", 2, Device7_GetClipStatus},                             // 0x70
    {"DrawPrimitiveStrided", 6, Device7_DrawPrimitiveStrided},               // 0x74
    {"DrawIndexedPrimitiveStrided", 8, Device7_DrawIndexedPrimitiveStrided}, // 0x78
    {"DrawPrimitiveVB", 6, Device7_DrawPrimitiveVB},                         // 0x7c
    {"DrawIndexedPrimitiveVB", 8, Device7_DrawIndexedPrimitiveVB},           // 0x80
    {"ComputeSphereVisibility", 6, Device7_ComputeSphereVisibility},         // 0x84
    {"GetTexture", 3, Device7_GetTexture},                                   // 0x88
    {"SetTexture", 3, Device7_SetTexture},                                   // 0x8c
    {"GetTextureStageState", 4, Device7_GetTextureStageState},               // 0x90
    {"SetTextureStageState", 4, Device7_SetTextureStageState},               // 0x94
    {"ValidateDevice", 2, Device7_ValidateDevice},                           // 0x98
    {"ApplyStateBlock", 2, Device7_ApplyStateBlock},                         // 0x9c
    {"CaptureStateBlock", 2, Device7_CaptureStateBlock},                     // 0xa0
    {"DeleteStateBlock", 2, Device7_DeleteStateBlock},                       // 0xa4
    {"CreateStateBlock", 3, Device7_CreateStateBlock},                       // 0xa8
    {"Load", 6, Device7_Load},                                               // 0xac
    {"LightEnable", 3, Device7_LightEnable},                                 // 0xb0
    {"GetLightEnable", 3, Device7_GetLightEnable},                           // 0xb4
    {"SetClipPlane", 3, Device7_SetClipPlane},                               // 0xb8
    {"GetClipPlane", 3, Device7_GetClipPlane},                               // 0xbc
    {"GetInfo", 4, Device7_GetInfo},                                         // 0xc0
};

const ComMethod g_vb7[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"Lock", 4, VB7_Lock},                                     // 0x0c
    {"Unlock", 1, VB7_Unlock},                                 // 0x10
    {"ProcessVertices", 8, VB7_ProcessVertices},               // 0x14
    {"GetVertexBufferDesc", 2, VB7_GetVertexBufferDesc},       // 0x18
    {"Optimize", 3, VB7_Optimize},                             // 0x1c
    {"ProcessVerticesStrided", 8, VB7_ProcessVerticesStrided}, // 0x20
};

} // namespace

// Releasing a device also releases the render target and every bound texture
// it retained. Registered with com_set_destructor so com_destroy runs it.
void d3d7_device_destroy(ComObj *dev) {
    if (!dev->d3d7)
        return;
    for (uint32_t &id : dev->d3d7->texture) {
        if (id) {
            if (ComObj *t = com_get(id))
                com_release(t);
            id = 0;
        }
    }
    if (dev->d3d7->render_target) {
        if (ComObj *s = com_get(dev->d3d7->render_target))
            com_release(s);
        dev->d3d7->render_target = 0;
    }
}

void d3d7_vb_destroy(ComObj *vb) {
    if (vb->pixels && vb->owns_pixels) {
        heap_free(vb->pixels);
        vb->pixels = 0;
        vb->pixels_bytes = 0;
    }
}

void d3d7_reset() {
    g_scratch = 0;
    g_scratch_size = 0;
}

void d3d7_register() {
    static bool done = false;
    if (done)
        return;
    done = true;
    com_define(IF_D3D7, "DDRAW.dll", "IDirect3D7", g_d3d7, std::size(g_d3d7));
    com_define(IF_D3DDEVICE7, "DDRAW.dll", "IDirect3DDevice7", g_device7, std::size(g_device7));
    com_define(IF_D3DVERTEXBUFFER7, "DDRAW.dll", "IDirect3DVertexBuffer7", g_vb7, std::size(g_vb7));
    // IDirect3D7 is an interface on the DirectDraw object: one refcount and
    // one controlling IUnknown, exactly like IDirect3D3.
    com_bind(IF_D3D7, K_DDRAW);
    com_bind(IF_D3DDEVICE7, K_D3D7DEVICE);
    com_register_iid(IF_D3D7, IID_IDirect3D7_);
    com_set_destructor(K_D3D7DEVICE, d3d7_device_destroy);
    com_set_destructor(K_D3D7VB, d3d7_vb_destroy);
    // Register the VB's own IID too, so naming it in a log is not a hex dump.
    com_register_iid(IF_D3DVERTEXBUFFER7, IID_IDirect3DVertexBuffer7_);
}
