// Bounded D3D8 guest COM bridge. Guest layouts stay 32-bit; host renderer
// pointers are owned here. Unimplemented slots fail by name through the import
// dispatcher. Descriptors and COM lifetime are tested separately from GPU work.
#include "com.h"
#include "dx.h"
#include "../runtime/guest.h"
#include "../runtime/memory.h"
#include "../runtime/display_seam.h"
#include "../runtime/win32.h"
#include <unordered_set>
#include <cstdio>
#include <cstdlib>

#include <string.h>
#include <vector>
#include <string>

#ifdef RECOMP_D3D8_WGPU
#include "d3d8_abi.h"
#endif

namespace {

// ---------------------------------------------------------------------------
// Win32 / D3D8 constants (guest values, not host).
// ---------------------------------------------------------------------------
constexpr uint32_t D8_OK = 0;
constexpr uint32_t D8_ERR_INVALIDCALL = 0x8876086Cu;
constexpr uint32_t D8_ERR_NOTAVAILABLE = 0x8876086Au;
constexpr uint32_t D8_DEVTYPE_HAL = 1;

// Depth formats the bridge accepts at create time even though it has no depth
// storage yet, so the guest's D3DPRESENT_PARAMETERS is not rejected outright.
// Using one (ZENABLE/ZWRITEENABLE on, or a deep Clear) fails by name below.
constexpr uint32_t D8FMT_D16 = 80;
constexpr uint32_t D8FMT_D24S8 = 75;
constexpr uint32_t D8FMT_D24X8 = 77;
constexpr uint32_t D8FMT_D32 = 71;

constexpr uint32_t D8_CLEAR_ZBUFFER = 0x2;
constexpr uint32_t D8_CLEAR_STENCIL = 0x4;
constexpr uint32_t D8_RS_ZENABLE = 7;
constexpr uint32_t D8_RS_ZWRITEENABLE = 14;

// D3D8's GetAvailableTextureMem reports free texture memory, which on the
// modeled unified-memory machine is the runtime's deterministic available
// physical memory (KERNEL32!GlobalMemoryStatus dwAvailPhys, 384 MB). The
// backend exposes no VRAM budget, so this is the modeled figure, not a live
// wgpu query.
constexpr uint32_t D8_AVAILABLE_TEXTURE_MEM = 384u * 1024u * 1024u;

constexpr uint32_t D8FMT_X8R8G8B8 = 0x16;

#define D8_IID(a, b, c, d0, d1, d2, d3, d4, d5, d6, d7)                                            \
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

static const uint8_t IID_IDirect3D8_[16] =
    D8_IID(0x1dd9e8da, 0x1c77, 0x4d40, 0xb0, 0xcf, 0x98, 0xfe, 0xfd, 0xff, 0x95, 0x12);
static const uint8_t IID_IDirect3DDevice8_[16] =
    D8_IID(0x7385e5df, 0x8fe8, 0x41d5, 0x86, 0xb6, 0xd7, 0xb4, 0x85, 0x47, 0xb6, 0xcf);
static const uint8_t IID_IDirect3DSurface8_[16] =
    D8_IID(0xb96eebca, 0xb326, 0x4ea5, 0x88, 0x2f, 0x2f, 0xf5, 0xba, 0xe0, 0x21, 0xdd);

// ---------------------------------------------------------------------------
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
        LOGW("d3d8: adapter %s vendor=0x%04x device=0x%04x max_texture=%u", c.info.name,
             c.info.vendor_id, c.info.device_id, c.info.max_texture_dimension_2d);
    } else {
        LOGW("d3d8: adapter query failed: %s", err.message);
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

#ifdef RECOMP_D3D8_WGPU
static D3d8Device *host_device(ComObj *o) {
    return static_cast<D3d8Device *>(o->d3d8_device);
}
#endif

// Objects have stable host addresses until com_reset. Reset must release GPU
// handles itself: com_reset discards objects without running their destructors.
std::unordered_set<ComObj *> live_devices;

#ifdef RECOMP_D3D8_WGPU
uint32_t host_result(X86 *c, int32_t status, const D3d8Error &err) {
    if (status == D3D8_OK)
        return D8_OK;
    fprintf(stderr, "d3d8: %s\n", err.message);
    fflush(stderr);
    if (status == D3D8_UNSUPPORTED || status == D3D8_NOT_IMPLEMENTED)
        imports_unsupported(c);
    return status == D3D8_INVALID_ARGUMENT ? D8_ERR_INVALIDCALL : D8_ERR_NOTAVAILABLE;
}
#endif

// ---------------------------------------------------------------------------
// D3DCAPS8 describes the bridge, not wgpu's potential capabilities. Textures,
// lighting, vertex buffers and draws remain unsupported in this integration.
// ---------------------------------------------------------------------------
void write_caps(uint32_t addr) {
    memset(gm_ptr(addr), 0, 212);
    wr32(addr, D8_DEVTYPE_HAL);
    wr32(addr + 12, 0x00080000u); // D3DCAPS2_CANRENDERWINDOWED
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
        result.push_back({dw, dh, 0, D8FMT_X8R8G8B8});
    static const uint32_t kStandard[][2] = {
        {640, 480}, {800, 600}, {1024, 768}, {1280, 1024}, {1600, 1200},
    };
    for (const auto &m : kStandard)
        if (m[0] != dw || m[1] != dh)
            result.push_back({m[0], m[1], 0, D8FMT_X8R8G8B8});
    return result;
}

bool adapter_type(X86 *c) {
    return arg(c, 1) == 0 && arg(c, 2) == D8_DEVTYPE_HAL && ensure_adapter();
}
bool color_format(uint32_t format) {
    return format == 21 || format == 22;
}

void write_display_mode(uint32_t addr, const DisplayMode &m) {
    if (!addr || !gm_valid(addr, 16))
        return;
    wr32(addr + 0, m.w);
    wr32(addr + 4, m.h);
    wr32(addr + 8, m.refresh);
    wr32(addr + 12, m.format);
}

// Depth storage is not implemented. An operation that would need it must stop
// with a named diagnostic rather than pretend depth testing/writing happened.
[[noreturn]] void depth_unsupported(X86 *c, const char *what) {
    fprintf(stderr, "d3d8: %s needs depth storage, which is not implemented\n", what);
    fflush(stderr);
    imports_unsupported(c);
    abort();
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
    strncpy((char *)gm_ptr(out + 512), adapter_cache().info.name, 511);
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
    write_display_mode(out, {w, h, 0, D8FMT_X8R8G8B8});
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
    com_ret(c, adapter_type(c) && arg(c, 3) == 22 && arg(c, 4) == 1 && arg(c, 5) == 1 &&
                       color_format(arg(c, 6))
                   ? D8_OK
                   : D8_ERR_NOTAVAILABLE);
}
void D8_CheckDeviceMultiSampleType(X86 *c) {
    com_ret(c, adapter_type(c) && color_format(arg(c, 3)) && arg(c, 4) && !arg(c, 5)
                   ? D8_OK
                   : D8_ERR_NOTAVAILABLE);
}
void D8_CheckDepthStencilMatch(X86 *c) {
    com_ret(c, D8_ERR_NOTAVAILABLE);
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
    // DISCARD (1) and COPY_VSYNC (4) are the swap effects the host can honour
    // by presenting a completed frame; reject any other semantics instead of
    // silently dropping them. Windowed (0) and fullscreen (1) present through
    // the same host target, so the flag is not a constraint. An autodepth
    // request is accepted only for a depth format the bridge can describe;
    // the host backend does not yet own a depth buffer, so its surface calls
    // still fail by name rather than returning a fabricated handle.
    uint32_t depth = rd32(pp + 32), depth_format = rd32(pp + 36);
    if (rd32(pp + 12) > 1 || rd32(pp + 16) || (swap != 1 && swap != 4) ||
        (depth && depth_format != D8FMT_D32 && depth_format != D8FMT_D24S8 &&
         depth_format != D8FMT_D24X8 && depth_format != D8FMT_D16) ||
        rd32(pp + 40) || rd32(pp + 44) || rd32(pp + 48)) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
    // TODO(depth): depth storage is not implemented. The create call is not
    // refused because the guest asks for it unconditionally, but any state or
    // clear that would need it fails by name in Dev_SetRenderState/Dev_Clear.
    if (depth)
        LOGW("d3d8: autodepth requested (fmt 0x%x); depth storage is not implemented yet",
             depth_format);
    LOGV("d3d8: CreateDevice %ux%u fmt=0x%x swap=%u windowed=%u accepted", w, h, format, swap,
         windowed);
    if (!adapter_type(c)) {
        com_ret(c, D8_ERR_NOTAVAILABLE);
        return;
    }
#ifdef RECOMP_D3D8_WGPU
    ComObj *factory = d8_this(c);
    ComObj *dev = com_new(K_D3D8DEVICE);
    live_devices.insert(dev);
    dev->d3d8_factory = factory->id;
    com_addref(factory);
    dev->d3d8_width = w;
    dev->d3d8_height = h;
    dev->d3d8_format = format;
    D3d8Error err{};
    dev->d3d8_device = d3d8_device_create(w, h, format, &err);
    if (!dev->d3d8_device) {
        com_release(dev);
        com_ret(c, host_result(c, err.status ? err.status : D3D8_BACKEND, err));
        return;
    }
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

void Dev_TestCooperativeLevel(X86 *c) {
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
        if (arg(c, 3) & (D8_CLEAR_ZBUFFER | D8_CLEAR_STENCIL))
            depth_unsupported(c, "IDirect3DDevice8::Clear with ZBUFFER/STENCIL");
        D3d8Error err{};
        int32_t status = d3d8_device_clear(host_device(dev), arg(c, 1), arg(c, 3), arg(c, 4), &err);
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
void Dev_SetRenderState(X86 *c) {
#ifdef RECOMP_D3D8_WGPU
    ComObj *dev = d8_dev(c);
    if (dev && dev->d3d8_device) {
        // Depth-dependent states cannot be honoured without depth storage.
        // Any nonzero ZENABLE/ZWRITEENABLE would silently mis-render, so it
        // stops by name here. (The D3D8 default is ZENABLE=TRUE; a game that
        // relies on depth must wait for the depth implementation.)
        if ((arg(c, 1) == D8_RS_ZENABLE || arg(c, 1) == D8_RS_ZWRITEENABLE) && arg(c, 2))
            depth_unsupported(c, "IDirect3DDevice8::SetRenderState(ZENABLE/ZWRITEENABLE on)");
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
        std::vector<uint8_t> rgba(size_t(bytes), 0);
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
        std::vector<uint32_t> argb(size_t(bytes / 4));
        for (size_t i = 0; i < argb.size(); ++i) {
            const uint8_t *p = rgba.data() + i * 4;
            argb[i] =
                (uint32_t(p[3]) << 24) | (uint32_t(p[0]) << 16) | (uint32_t(p[1]) << 8) | p[2];
        }
        host_display_present_window(argb.data(), int(dev->d3d8_width), int(dev->d3d8_height));
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
    ComObj *surface = com_get(dev->d3d8_backbuffer);
    if (surface) {
        com_addref(surface);
    } else {
        surface = com_new(K_D3D8SURFACE);
        surface->d3d8_owner = dev->id;
        // Only external surface references retain the device. A weak cache
        // avoids a device <-> implicit surface reference cycle.
        com_addref(dev);
        dev->d3d8_backbuffer = surface->id;
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

// IDirect3DSurface8 derives directly from IUnknown, not IDirect3DResource8.
// Its descriptor is eight guest DWORDs; in particular Size precedes the
// multisample field (unlike D3D9). This view owns no pixel allocation: its
// storage remains the device's real host backbuffer.
void Surface_GetDesc(X86 *c) {
    ComObj *surface = com_this_arg(c, IF_D3D8SURFACE8);
    ComObj *dev = surface ? com_get(surface->d3d8_owner) : nullptr;
    uint32_t out = arg(c, 1);
    if (!dev || !out || !gm_valid(out, 32)) {
        com_ret(c, D8_ERR_INVALIDCALL);
        return;
    }
    wr32(out + 0, dev->d3d8_format);
    wr32(out + 4, 1);  // D3DRTYPE_SURFACE
    wr32(out + 8, 1);  // D3DUSAGE_RENDERTARGET
    wr32(out + 12, 0); // D3DPOOL_DEFAULT
    wr32(out + 16, dev->d3d8_width * dev->d3d8_height * 4);
    wr32(out + 20, 0); // D3DMULTISAMPLE_NONE
    wr32(out + 24, dev->d3d8_width);
    wr32(out + 28, dev->d3d8_height);
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

void surface_destroy(ComObj *surface) {
    ComObj *dev = com_get(surface->d3d8_owner);
    if (dev) {
        if (dev->d3d8_backbuffer == surface->id)
            dev->d3d8_backbuffer = 0;
        com_release(dev);
    }
    surface->d3d8_owner = 0;
}

// ---------------------------------------------------------------------------
// Vtables, in interface order. A guest dispatches by slot index, so the order
// is the ABI and may not be rearranged.
// ---------------------------------------------------------------------------
// --- IDirect3DSurface8 table ---
static const ComMethod g_surface8[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"GetDevice", 2, Surface_GetDevice},
    {"SetPrivateData", 5, imports_unsupported},
    {"GetPrivateData", 4, imports_unsupported},
    {"FreePrivateData", 2, imports_unsupported},
    {"GetContainer", 3, imports_unsupported},
    {"GetDesc", 2, Surface_GetDesc},
    {"LockRect", 4, imports_unsupported},
    {"UnlockRect", 1, imports_unsupported},
};

// --- IDirect3D8 table ---
static const ComMethod g_d3d8[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"RegisterSoftwareDevice", 2, D8_RegisterSoftwareDevice},
    {"GetAdapterCount", 1, D8_GetAdapterCount},
    {"GetAdapterIdentifier", 4, D8_GetAdapterIdentifier},
    {"GetAdapterModeCount", 2, D8_GetAdapterModeCount},
    {"EnumAdapterModes", 4, D8_EnumAdapterModes},
    {"GetAdapterDisplayMode", 3, D8_GetAdapterDisplayMode},
    {"CheckDeviceType", 6, D8_CheckDeviceType},
    {"CheckDeviceFormat", 7, D8_CheckDeviceFormat},
    {"CheckDeviceMultiSampleType", 6, D8_CheckDeviceMultiSampleType},
    {"CheckDepthStencilMatch", 6, D8_CheckDepthStencilMatch},
    {"GetDeviceCaps", 4, D8_GetDeviceCaps},
    {"GetAdapterMonitor", 2, D8_GetAdapterMonitor},
    {"CreateDevice", 7, D8_CreateDevice},
};

// --- IDirect3DDevice8 table ---
static const ComMethod g_device8[] = {
    {"QueryInterface", 3, com_QueryInterface},
    {"AddRef", 1, com_AddRef},
    {"Release", 1, com_Release},
    {"TestCooperativeLevel", 1, Dev_TestCooperativeLevel},
    {"GetAvailableTextureMem", 1, Dev_GetAvailableTextureMem},
    {"ResourceManagerDiscardBytes", 2, imports_unsupported},
    {"GetDirect3D", 2, Dev_GetDirect3D},
    {"GetDeviceCaps", 2, Dev_GetDeviceCaps},
    {"GetDisplayMode", 2, imports_unsupported},
    {"GetCreationParameters", 2, imports_unsupported},
    {"SetCursorProperties", 4, imports_unsupported},
    {"SetCursorPosition", 4, imports_unsupported},
    {"ShowCursor", 2, imports_unsupported},
    {"CreateAdditionalSwapChain", 3, imports_unsupported},
    {"Reset", 2, imports_unsupported},
    {"Present", 5, Dev_Present},
    {"GetBackBuffer", 4, Dev_GetBackBuffer},
    {"GetRasterStatus", 2, imports_unsupported},
    {"SetGammaRamp", 3, imports_unsupported},
    {"GetGammaRamp", 2, imports_unsupported},
    {"CreateTexture", 8, imports_unsupported},
    {"CreateVolumeTexture", 9, imports_unsupported},
    {"CreateCubeTexture", 7, imports_unsupported},
    {"CreateVertexBuffer", 6, imports_unsupported},
    {"CreateIndexBuffer", 6, imports_unsupported},
    {"CreateRenderTarget", 7, imports_unsupported},
    {"CreateDepthStencilSurface", 6, imports_unsupported},
    {"CreateImageSurface", 5, imports_unsupported},
    {"CopyRects", 6, imports_unsupported},
    {"UpdateTexture", 3, imports_unsupported},
    {"GetFrontBuffer", 2, imports_unsupported},
    {"SetRenderTarget", 3, imports_unsupported},
    {"GetRenderTarget", 2, imports_unsupported},
    {"GetDepthStencilSurface", 2, imports_unsupported},
    {"BeginScene", 1, Dev_BeginScene},
    {"EndScene", 1, Dev_EndScene},
    {"Clear", 7, Dev_Clear},
    {"SetTransform", 3, Dev_SetTransform},
    {"GetTransform", 3, imports_unsupported},
    {"MultiplyTransform", 3, imports_unsupported},
    {"SetViewport", 2, Dev_SetViewport},
    {"GetViewport", 2, imports_unsupported},
    {"SetMaterial", 2, imports_unsupported},
    {"GetMaterial", 2, imports_unsupported},
    {"SetLight", 3, imports_unsupported},
    {"GetLight", 3, imports_unsupported},
    {"LightEnable", 3, imports_unsupported},
    {"GetLightEnable", 3, imports_unsupported},
    {"SetClipPlane", 3, imports_unsupported},
    {"GetClipPlane", 3, imports_unsupported},
    {"SetRenderState", 3, Dev_SetRenderState},
    {"GetRenderState", 3, imports_unsupported},
    {"BeginStateBlock", 1, imports_unsupported},
    {"EndStateBlock", 2, imports_unsupported},
    {"ApplyStateBlock", 2, imports_unsupported},
    {"CaptureStateBlock", 2, imports_unsupported},
    {"DeleteStateBlock", 2, imports_unsupported},
    {"CreateStateBlock", 3, imports_unsupported},
    {"SetClipStatus", 2, imports_unsupported},
    {"GetClipStatus", 2, imports_unsupported},
    {"GetTexture", 3, imports_unsupported},
    {"SetTexture", 3, imports_unsupported},
    {"GetTextureStageState", 4, imports_unsupported},
    {"SetTextureStageState", 4, Dev_SetTextureStageState},
    {"ValidateDevice", 2, imports_unsupported},
    {"GetInfo", 4, imports_unsupported},
    {"SetPaletteEntries", 3, imports_unsupported},
    {"GetPaletteEntries", 3, imports_unsupported},
    {"SetCurrentTexturePalette", 2, imports_unsupported},
    {"GetCurrentTexturePalette", 2, imports_unsupported},
    // TODO(draw): when a draw path is implemented it must call the backend's
    // state check and fail loudly on any render/texture-stage state the
    // renderer does not honour, rather than drawing with it dropped.
    {"DrawPrimitive", 4, imports_unsupported},
    {"DrawIndexedPrimitive", 6, imports_unsupported},
    {"DrawPrimitiveUP", 5, imports_unsupported},
    {"DrawIndexedPrimitiveUP", 9, imports_unsupported},
    {"ProcessVertices", 6, imports_unsupported},
    {"CreateVertexShader", 5, imports_unsupported},
    {"SetVertexShader", 2, imports_unsupported},
    {"GetVertexShader", 2, imports_unsupported},
    {"DeleteVertexShader", 2, imports_unsupported},
    {"SetVertexShaderConstant", 4, imports_unsupported},
    {"GetVertexShaderConstant", 4, imports_unsupported},
    {"GetVertexShaderDeclaration", 4, imports_unsupported},
    {"GetVertexShaderFunction", 4, imports_unsupported},
    {"SetStreamSource", 4, imports_unsupported},
    {"GetStreamSource", 4, imports_unsupported},
    {"SetIndices", 3, imports_unsupported},
    {"GetIndices", 3, imports_unsupported},
    {"CreatePixelShader", 3, imports_unsupported},
    {"SetPixelShader", 2, imports_unsupported},
    {"GetPixelShader", 2, imports_unsupported},
    {"DeletePixelShader", 2, imports_unsupported},
    {"SetPixelShaderConstant", 4, imports_unsupported},
    {"GetPixelShaderConstant", 4, imports_unsupported},
    {"GetPixelShaderFunction", 4, imports_unsupported},
    {"DrawRectPatch", 4, imports_unsupported},
    {"DrawTriPatch", 4, imports_unsupported},
    {"DeletePatch", 2, imports_unsupported},
};

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
};

void device_destroy(ComObj *o) {
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
    com_bind(IF_D3D8, K_D3D8);
    com_bind(IF_D3D8DEVICE, K_D3D8DEVICE);
    com_bind(IF_D3D8SURFACE8, K_D3D8SURFACE);
    com_register_iid(IF_D3D8, IID_IDirect3D8_);
    com_register_iid(IF_D3D8DEVICE, IID_IDirect3DDevice8_);
    com_register_iid(IF_D3D8SURFACE8, IID_IDirect3DSurface8_);
    com_set_destructor(K_D3D8SURFACE, surface_destroy);
    com_set_destructor(K_D3D8DEVICE, device_destroy);
    imports_register(g_d3d8_exports, std::size(g_d3d8_exports));
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
    adapter_cache() = {};
}
