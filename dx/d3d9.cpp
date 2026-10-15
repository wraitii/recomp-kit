#include "com.h"
#include "dx.h"
#include "../runtime/guest.h"
#include "../runtime/memory.h"
#include "host_api.h"
#include "d3d9_pipeline.h"
#include "d3d9_raster.h"
#include "d3d9_shader.h"
#include "host_d9.h"
#include "frame_dump.h"
#include "d3d9_constants.inc"
#include "d3d_bridge_helpers.h"
#include "d3dx9shader_shim.h"
#include "d3dx9effect_shim.h"
#include "../runtime/win32.h"
#include "../platform/os.h"
#include "../runtime/display_seam.h"
#ifdef RECOMP_D3D9_WGPU
constexpr bool kD3d9WgpuEnabled = true;
#else
constexpr bool kD3d9WgpuEnabled = false;
#endif
#include <unordered_map>
#include <map>
#include <set>

#include <string.h>
#include <iterator>
#include <algorithm>
#include <initializer_list>
#include <cstdlib>

#define MAKE_D3DHRESULT(code) (0x88760000u | (uint32_t)(code))
static const uint32_t D3D_OK9 = 0u;
static const uint32_t D3DERR_NOTAVAILABLE = MAKE_D3DHRESULT(2154);
static const uint32_t D3DERR_INVALIDCALL = MAKE_D3DHRESULT(2156);
static const uint32_t D3DERR_NOTFOUND = MAKE_D3DHRESULT(2150);
static const uint32_t D3DERR_MOREDATA = MAKE_D3DHRESULT(2151);
static const uint32_t D3DERR_DEVICELOST = MAKE_D3DHRESULT(2152);
static const uint32_t D3DERR_DEVICENOTRESET = MAKE_D3DHRESULT(2153);
static const uint32_t D3DOK_NOAUTOGEN = 0x08760000u | 2159u;

static uint32_t bridge_result(X86 *c, const recomp::d3d::Status &status) {
    if (status.code == recomp::d3d::StatusCode::Ok)
        return D3D_OK9;
    std::string message(status.message.data(), status.message.size());
    LOGW("d3d9: %s", message.c_str());
    if (status.code == recomp::d3d::StatusCode::Unsupported ||
        status.code == recomp::d3d::StatusCode::NotImplemented)
        imports_unsupported(c);
    if (status.code == recomp::d3d::StatusCode::InvalidArgument)
        return D3DERR_INVALIDCALL;
    if (status.code == recomp::d3d::StatusCode::InvalidData)
        return MAKE_D3DHRESULT(2905);
    if (status.code == recomp::d3d::StatusCode::OutOfMemory)
        return E_OUTOFMEMORY;
    if (status.code == recomp::d3d::StatusCode::WasStillDrawing)
        return MAKE_D3DHRESULT(540);
    if (status.code == recomp::d3d::StatusCode::DeviceLost)
        return D3DERR_DEVICELOST;
    if (status.code == recomp::d3d::StatusCode::DeviceNotReset)
        return D3DERR_DEVICENOTRESET;
    if (status.code == recomp::d3d::StatusCode::NoAutogen)
        return D3DOK_NOAUTOGEN;
    if (status.code == recomp::d3d::StatusCode::NotAvailable)
        return D3DERR_NOTAVAILABLE;
    return D3DERR_NOTAVAILABLE;
}

static void d3d9_setup_guest_fpu(X86 *c, uint32_t behavior_flags) {
    if (!(behavior_flags & 0x2u))
        x87_set_cw(c, (uint16_t)((c->fpu_cw & ~0x0f3fu) | 0x003fu));
}

// Defined with the resources further down.
struct ComObj;
static ComObj *device_backbuffer(ComObj *dev);
static ComObj *device_depthbuffer(ComObj *dev);
static bool gpu_on();
static HostD9Target gpu_target(ComObj *dev);
static void target_viewport(ComObj *dev, const HostD9Target &t, int32_t vp[4]);
static void gpu_target_drawn(ComObj *dev, bool color, bool depth);
static void gpu_sync(ComObj *o);
static void device_discard_implicit_surfaces(ComObj *dev);
static bool implicit_surface_externally_referenced(uint32_t field);
static void create_texture_wgpu(X86 *c, recomp::d3d::D3d9TextureKind kind);
static void create_surface_wgpu(X86 *c, uint32_t out_index, uint32_t usage);
static ComObj *com_level_view(ComObj *tex, uint32_t face, uint32_t level, bool volume);
static void level_lock_wgpu(X86 *c, ComObj *o, uint32_t face, uint32_t level, bool volume,
                            uint32_t rect_addr, uint32_t box_addr, uint32_t flags, uint32_t out,
                            bool box_output);
static void level_unlock_wgpu(X86 *c, ComObj *o, uint32_t face, uint32_t level);
static ComObj *level_owner(ComObj *o, uint32_t *face, uint32_t *level, bool *volume);

// One scratch block for structures a method fills in for the guest.
static uint32_t g_scratch = 0, g_scratch_size = 0;
static uint32_t scratch9(uint32_t n) {
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

static ComObj *this_d3d9(X86 *c) {
    ComObj *o = com_this_arg(c);
    return (o && o->kind == K_D3D9) ? o : nullptr;
}
static ComObj *this_device9(X86 *c) {
    ComObj *o = com_this_arg(c);
    return (o && o->kind == K_D3D9DEVICE) ? o : nullptr;
}

static bool d3d9_flush_resource_owner(X86 *c, ComObj *resource) {
    ComObj *device = resource ? com_get(resource->dev_d3d) : nullptr;
    if (!device || !device->d3d9_device)
        return true;
    recomp::d3d::Status status = device->d3d9_device->flush();
    if (status.code == recomp::d3d::StatusCode::Ok)
        return true;
    com_ret(c, bridge_result(c, status));
    return false;
}

static uint32_t g_d3d9_display_owner = 0;

extern "C" bool d3d9_display_mode(uint32_t *w, uint32_t *h, uint32_t *bpp) {
    ComObj *dev = g_d3d9_display_owner ? com_get(g_d3d9_display_owner) : nullptr;
    if (!dev || !dev->d3d9_device)
        return false;
    recomp::d3d::D3d9PresentParams present = dev->d3d9_device->effective_present();
    if (present.windowed != 0 || present.width == 0 || present.height == 0)
        return false;
    if (w)
        *w = present.width;
    if (h)
        *h = present.height;
    if (bpp)
        *bpp = 32;
    return true;
}

static void d3d9_clear_virtual_display(ComObj *dev) {
    if (!dev || g_d3d9_display_owner != dev->id)
        return;
    g_d3d9_display_owner = 0;
    uint32_t w = 0, h = 0, bpp = 0;
    win32_display_mode(&w, &h, &bpp);
    host_set_display_mode((int)w, (int)h, (int)bpp);
}

static void d3d9_sync_virtual_display(X86 *c, ComObj *dev, uint32_t hwnd) {
    if (!dev || !dev->d3d9_device)
        return;
    recomp::d3d::D3d9PresentParams present = dev->d3d9_device->effective_present();
    if (present.windowed != 0) {
        d3d9_clear_virtual_display(dev);
        if (hwnd)
            win32_uncover_display(c, hwnd);
        return;
    }
    g_d3d9_display_owner = dev->id;
    host_set_display_mode((int)present.width, (int)present.height, 32);
    if (hwnd)
        win32_cover_display(c, hwnd, present.width, present.height);
}

static bool read_guest_rect(uint32_t addr, recomp::d3d::D3d9Rect *out) {
    if (!addr || !gm_valid(addr, 16))
        return false;
    out->left = (int32_t)rd32(addr);
    out->top = (int32_t)rd32(addr + 4);
    out->right = (int32_t)rd32(addr + 8);
    out->bottom = (int32_t)rd32(addr + 12);
    return true;
}

static uint32_t d3d9_target_id(ComObj *s) {
    if (!s)
        return 0;
    if (s->d3d9_implicit)
        return s->d3d9_target_depth ? 0xffffffffu : 0u;
    return s->id;
}

static void return_surface_handle(X86 *c, ComObj *s, uint32_t out) {
    if (!s) {
        com_ret(c, D3DERR_NOTFOUND);
        return;
    }
    com_addref(s);
    uint32_t view_ = com_view(s, IF_D3DSURFACE9);
    if (!view_) {
        com_release(s);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}

static void d3d9_ref_add(uint32_t id) {
    if (ComObj *o = com_get(id))
        com_retain_internal(o);
}
static void d3d9_ref_del(uint32_t id) {
    if (ComObj *o = com_get(id))
        com_release_internal(o);
}
static std::array<uint32_t, 38> d3d9_binding_ids(ComObj *dev,
                                                 const recomp::d3d::D3d9Bindings &bindings) {
    std::array<uint32_t, 38> out{};
    std::copy(bindings.textures.begin(), bindings.textures.end(), out.begin());
    std::copy(bindings.streams.begin(), bindings.streams.end(), out.begin() + 16);
    out[32] = bindings.indices;
    auto resolve = [&](const std::unordered_map<uint32_t, uint32_t> &map, uint32_t handle) {
        if (!handle)
            return 0u;
        auto it = map.find(handle);
        return it == map.end() ? 0u : it->second;
    };
    out[33] = resolve(dev->d3d9_device->shader_views, bindings.vertex_shader);
    out[34] = resolve(dev->d3d9_device->shader_views, bindings.pixel_shader);
    out[35] = resolve(dev->d3d9_device->decl_views, bindings.declaration);
    return out;
}
static std::set<uint32_t> d3d9_binding_set(ComObj *dev, const recomp::d3d::D3d9Bindings &bindings) {
    std::set<uint32_t> out;
    for (uint32_t id : d3d9_binding_ids(dev, bindings))
        if (id)
            out.insert(id);
    return out;
}
static void d3d9_reconcile(std::set<uint32_t> &current, const std::set<uint32_t> &desired) {
    for (uint32_t id : current)
        if (!desired.count(id))
            d3d9_ref_del(id);
    for (uint32_t id : desired)
        if (!current.count(id))
            d3d9_ref_add(id);
    current = desired;
}
static void d3d9_reconcile_bindings(std::array<uint32_t, 38> &snapshot,
                                    const std::array<uint32_t, 38> &ids) {
    for (size_t i = 0; i < ids.size(); ++i)
        if (snapshot[i] != ids[i] && ids[i])
            d3d9_ref_add(ids[i]);
    for (size_t i = 0; i < ids.size(); ++i)
        if (snapshot[i] != ids[i] && snapshot[i])
            d3d9_ref_del(snapshot[i]);
    snapshot = ids;
}
static void d3d9_refresh_bindings(ComObj *dev) {
    if (!dev || !dev->d3d9_device)
        return;
    recomp::d3d::D3d9BindingsResult result = dev->d3d9_device->current_bindings();
    if (result.status.code != recomp::d3d::StatusCode::Ok)
        return;
    auto *host = dev->d3d9_device.get();
    auto live = d3d9_binding_ids(dev, result.live);
    size_t slot = 36;
    for (const auto &target : {host->render_target(), host->depth_stencil()}) {
        if (target.status.code == recomp::d3d::StatusCode::Ok && target.value &&
            target.value != 0xffffffffu)
            live[slot] = target.value;
        ++slot;
    }
    auto recording = result.recording_active ? d3d9_binding_ids(dev, result.recording)
                                             : std::array<uint32_t, 38>{};
    d3d9_reconcile_bindings(host->live_bindings, live);
    d3d9_reconcile_bindings(host->recording_bindings, recording);
}
static void d3d9_bind_slot(ComObj *dev, size_t slot, uint32_t id) {
    D3d9HostDevice *host = dev->d3d9_device.get();
    if (host->recording_block) {
        d3d9_refresh_bindings(dev);
        return;
    }
    uint32_t &current = host->live_bindings[slot];
    if (current == id)
        return;
    if (id)
        d3d9_ref_add(id);
    if (current)
        d3d9_ref_del(current);
    current = id;
}
static void d3d9_refresh_block_bindings(ComObj *dev, uint32_t handle) {
    if (!dev || !dev->d3d9_device)
        return;
    recomp::d3d::D3d9BlockBindingsResult result = dev->d3d9_device->state_block_bindings(handle);
    std::set<uint32_t> &refs = dev->d3d9_device->block_refs[handle];
    if (result.status.code == recomp::d3d::StatusCode::Ok)
        d3d9_reconcile(refs, d3d9_binding_set(dev, result.bindings));
}
static void d3d9_drop_block_refs(ComObj *dev, uint32_t handle) {
    if (!dev || !dev->d3d9_device)
        return;
    auto it = dev->d3d9_device->block_refs.find(handle);
    if (it == dev->d3d9_device->block_refs.end())
        return;
    for (uint32_t id : it->second)
        d3d9_ref_del(id);
    dev->d3d9_device->block_refs.erase(it);
}
static void d3d9_drop_all_refs(ComObj *dev) {
    if (!dev || !dev->d3d9_device)
        return;
    D3d9HostDevice *host = dev->d3d9_device.get();
    for (uint32_t id : host->live_bindings)
        if (id)
            d3d9_ref_del(id);
    for (uint32_t id : host->recording_bindings)
        if (id)
            d3d9_ref_del(id);
    host->live_bindings.fill(0);
    host->recording_bindings.fill(0);
    for (auto &entry : host->block_refs)
        for (uint32_t id : entry.second)
            d3d9_ref_del(id);
    host->block_refs.clear();
}
static ComObj *d3d9_make_object(ComObj *dev, ComKind kind, ComIface iface, uint32_t handle,
                                uint32_t out) {
    ComObj *o = com_new(kind);
    if (!o)
        return nullptr;
    o->dev_d3d = dev->id;
    o->d3d9_state_handle = handle;
    com_retain_internal(dev);
    o->d3d9_owner_retained = true;
    uint32_t view = com_view(o, iface);
    if (!view) {
        com_release(o);
        return nullptr;
    }
    com_out_ptr(out, view);
    return o;
}

// ---------------------------------------------------------------------------
// IDirect3D9
// ---------------------------------------------------------------------------
// One adapter, the host window's display. A game that enumerates further gets
// an honest "there is only one".
void D9_RegisterSoftwareDevice(X86 *c) {
    com_ret(c, D3DERR_NOTAVAILABLE);
}
void D9_GetAdapterCount(X86 *c) {
    set_eax(c, 1);
}

// D3DADAPTER_IDENTIFIER9: two 512-byte strings, a 32-byte device name, the
// driver version, four ids, the identifier GUID and the WHQL level.
void D9_GetAdapterIdentifier(X86 *c) {
    uint32_t out = arg(c, 3);
    if (!out || (kD3d9WgpuEnabled && (!this_d3d9(c) || arg(c, 1) != 0 || !gm_valid(out, 1100)))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        recomp::d3d::D3d9AdapterIdentifierResult result =
            recomp::d3d::d3d9_adapter_identifier(arg(c, 1));
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        memset(gm_ptr(out), 0, 1100);
        std::string description(result.description.data(), result.description.size());
        gm_put_str(out, "wgpu", 512);
        gm_put_str(out + 512, description.c_str(), 512);
        gm_put_str(out + 1024, "\\\\.\\DISPLAY1", 32);
        wr32(out + 1056, result.driver_version_low);
        wr32(out + 1060, result.driver_version_high);
        wr32(out + 1064, result.vendor_id);
        wr32(out + 1068, result.device_id);
        wr32(out + 1072, result.sub_sys_id);
        wr32(out + 1076, result.revision);
        wr32(out + 1096, result.whql_level);
        com_ret(c, D3D_OK9);
        return;
    }
    memset(gm_ptr(out), 0, 1100);
    // Games of this era pick their feature set by vendor and device id, and
    // turn off whatever they do not recognise (Most Wanted offers shadows
    // only on NVIDIA and ATI). The renderer covers what a GeForce 7800 GTX
    // offered, hardware shadow maps included, so that is the card it names.
    gm_put_str(out, "nv4_disp.dll", 512);
    gm_put_str(out + 512, "NVIDIA GeForce 7800 GTX", 512);
    gm_put_str(out + 1024, "\\\\.\\DISPLAY1", 32);
    // Driver[512], Description[512], DeviceName[32], then:
    wr32(out + 1056, 0x000a249b); // DriverVersion 6.14.10.9371 (low, high)
    wr32(out + 1060, 0x0006000e);
    wr32(out + 1064, 0x10de); // VendorId
    wr32(out + 1068, 0x0091); // DeviceId
    wr32(out + 1076, 0xa1);   // Revision
    wr32(out + 1096, 1);      // WHQLLevel: certified
    com_ret(c, D3D_OK9);
}

static uint32_t d3d9_display_format(uint32_t bpp) {
    return bpp == 32 ? 22u : bpp == 16 ? 23u : 0u;
}
static recomp::d3d::D3d9DisplayMode d3d9_current_display_mode() {
    uint32_t w = 0, h = 0, bpp = 0;
    win32_display_mode(&w, &h, &bpp);
    recomp::d3d::D3d9DisplayMode mode{};
    mode.width = w;
    mode.height = h;
    mode.refresh_rate = 0;
    mode.format = d3d9_display_format(bpp);
    return mode;
}
static uint32_t desktop_format9() {
    return d3d9_current_display_mode().format;
}

static void add_display_mode(std::vector<recomp::d3d::D3d9DisplayMode> &modes, uint32_t w,
                             uint32_t h) {
    if (!w || !h)
        return;
    for (const auto &m : modes)
        if (m.width == w && m.height == h)
            return;
    recomp::d3d::D3d9DisplayMode mode{};
    mode.width = w;
    mode.height = h;
    mode.refresh_rate = 0;
    mode.format = d3d9_display_format(32);
    modes.push_back(mode);
}

static std::vector<recomp::d3d::D3d9DisplayMode> d3d9_display_modes() {
    std::vector<recomp::d3d::D3d9DisplayMode> modes;
    static const uint32_t kStandard[][2] = {
        {640, 480},   {800, 600},   {1024, 768},  {1152, 864},  {1280, 720},  {1280, 800},
        {1280, 960},  {1280, 1024}, {1366, 768},  {1400, 1050}, {1440, 900},  {1600, 900},
        {1600, 1200}, {1680, 1050}, {1920, 1080}, {1920, 1200}, {1920, 1440}, {2048, 1536},
        {2560, 1440}, {2560, 1600}, {2560, 1920},
    };
    for (const auto &m : kStandard)
        add_display_mode(modes, m[0], m[1]);
    int32_t host_w = 0, host_h = 0;
    if (host_display_screen(&host_w, &host_h) && host_w > 0 && host_h > 0)
        add_display_mode(modes, (uint32_t)host_w, (uint32_t)host_h);
    return modes;
}

static recomp::d3d::D3d9DisplayMode
d3d9_device_display_mode(const recomp::d3d::D3d9PresentParams &present) {
    if (present.windowed != 0 || !present.width || !present.height)
        return d3d9_current_display_mode();
    recomp::d3d::D3d9DisplayMode mode{};
    mode.width = present.width;
    mode.height = present.height;
    mode.refresh_rate = present.fullscreen_refresh_rate;
    mode.format = d3d9_display_format(32);
    return mode;
}

static void put_mode(uint32_t p, const recomp::d3d::D3d9DisplayMode &mode) {
    if (!p)
        return;
    wr32(p + 0, mode.width);
    wr32(p + 4, mode.height);
    wr32(p + 8, mode.refresh_rate);
    wr32(p + 12, mode.format);
}

void D9_GetAdapterModeCount(X86 *c) {
    if (kD3d9WgpuEnabled) {
        if (!this_d3d9(c) || arg(c, 1) != 0 || arg(c, 2) != d3d9_display_format(32)) {
            set_eax(c, 0);
            return;
        }
        set_eax(c, (uint32_t)d3d9_display_modes().size());
        return;
    }
    set_eax(c, 1);
}

static void put_display_mode(uint32_t p) {
    if (!p)
        return;
    if (!kD3d9WgpuEnabled) {
        wr32(p + 0, 1280);
        wr32(p + 4, 960);
        wr32(p + 8, 60);
        wr32(p + 12, 22);
        return;
    }
    put_mode(p, d3d9_current_display_mode());
}
void D9_EnumAdapterModes(X86 *c) {
    if (kD3d9WgpuEnabled) {
        ComObj *d3d = this_d3d9(c);
        uint32_t out = arg(c, 4);
        uint32_t format = d3d9_display_format(32);
        if (!d3d || arg(c, 1) != 0 || !out || !gm_valid(out, 16) || arg(c, 2) != format) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        std::vector<recomp::d3d::D3d9DisplayMode> modes = d3d9_display_modes();
        uint32_t mode = arg(c, 3);
        if (mode >= modes.size()) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        put_mode(out, modes[mode]);
        com_ret(c, D3D_OK9);
        return;
    }
    put_display_mode(arg(c, 4));
    com_ret(c, D3D_OK9);
}
void D9_GetAdapterDisplayMode(X86 *c) {
    if (kD3d9WgpuEnabled) {
        ComObj *d3d = this_d3d9(c);
        uint32_t out = arg(c, 2);
        if (!d3d || arg(c, 1) != 0 || !out || !gm_valid(out, 16)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (desktop_format9() == 0) {
            com_ret(c, D3DERR_NOTAVAILABLE);
            return;
        }
        put_display_mode(out);
        com_ret(c, D3D_OK9);
        return;
    }
    put_display_mode(arg(c, 2));
    com_ret(c, D3D_OK9);
}
void D9_CheckDeviceType(X86 *c) {
    if (!kD3d9WgpuEnabled) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!this_d3d9(c)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9CheckDeviceTypeRequest request{};
    request.adapter = arg(c, 1);
    request.device_type = arg(c, 2);
    request.adapter_format = arg(c, 3);
    request.backbuffer_format = arg(c, 4);
    request.windowed = arg(c, 5);
    com_ret(c, bridge_result(c, recomp::d3d::d3d9_check_device_type(request)));
}
void D9_CheckDeviceFormat(X86 *c) {
    if (!kD3d9WgpuEnabled) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!this_d3d9(c)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9CheckDeviceFormatRequest request{};
    request.adapter = arg(c, 1);
    request.device_type = arg(c, 2);
    request.adapter_format = arg(c, 3);
    request.usage = arg(c, 4);
    request.resource_type = arg(c, 5);
    request.check_format = arg(c, 6);
    com_ret(c, bridge_result(c, recomp::d3d::d3d9_check_device_format(request)));
}
// D3DMULTISAMPLE_TYPE and quality as a sample count: 2x and 4x, which every
// Apple GPU renders. NONMASKABLE (1) offers them as quality levels 0 and 1.
static uint32_t multisample_count(uint32_t type, uint32_t quality) {
    if (type == 1)
        return quality == 0 ? 2 : 4;
    if (type >= 2)
        return type >= 4 ? 4 : 2;
    return 0;
}
// (this, Adapter, DeviceType, SurfaceFormat, Windowed, MultiSampleType,
// pQualityLevels)
void D9_CheckDeviceMultiSampleType(X86 *c) {
    if (kD3d9WgpuEnabled) {
        uint32_t quality = arg(c, 6);
        if (!this_d3d9(c) || (quality && !gm_valid(quality, 4))) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9CheckMultiSampleRequest request{};
        request.adapter = arg(c, 1);
        request.device_type = arg(c, 2);
        request.surface_format = arg(c, 3);
        request.windowed = arg(c, 4);
        request.multisample_type = arg(c, 5);
        recomp::d3d::ValueResult result = recomp::d3d::d3d9_check_device_multisample_type(request);
        if (result.status.code == recomp::d3d::StatusCode::Ok && quality)
            wr32(quality, result.value);
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    uint32_t type = arg(c, 5), quality = arg(c, 6);
    uint32_t levels = type == 0 ? 1 : type == 1 ? 2 : (type == 2 || type == 4) ? 1 : 0;
    if (!levels) {
        com_ret(c, 0x8876086au); // D3DERR_NOTAVAILABLE
        return;
    }
    if (quality && gm_valid(quality, 4))
        wr32(quality, levels);
    com_ret(c, D3D_OK9);
}
void D9_CheckDepthStencilMatch(X86 *c) {
    if (!kD3d9WgpuEnabled) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!this_d3d9(c)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9CheckDepthStencilRequest request{};
    request.adapter = arg(c, 1);
    request.device_type = arg(c, 2);
    request.adapter_format = arg(c, 3);
    request.render_target_format = arg(c, 4);
    request.depth_stencil_format = arg(c, 5);
    com_ret(c, bridge_result(c, recomp::d3d::d3d9_check_depth_stencil_match(request)));
}
void D9_CheckDeviceFormatConversion(X86 *c) {
    if (!kD3d9WgpuEnabled) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!this_d3d9(c)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9CheckFormatConversionRequest request{};
    request.adapter = arg(c, 1);
    request.device_type = arg(c, 2);
    request.source_format = arg(c, 3);
    request.destination_format = arg(c, 4);
    com_ret(c, bridge_result(c, recomp::d3d::d3d9_check_device_format_conversion(request)));
}

// D3DCAPS9 (304 bytes), as a GeForce 7800 GTX reports it for what the
// renderer implements: shader model 3.0, trilinear and 16x anisotropic
// filtering, the usual blend, compare and stencil sets, non-power-of-two
// textures, four render targets.
static void put_caps9(uint32_t p) {
    if (!p)
        return;
    memset(gm_ptr(p), 0, 304);
    auto f = [&](uint32_t off, float v) { wrf32(p + off, v); };
    wr32(p + 0, D3DDEVTYPE_HAL);
    wr32(p + 8, 0x00020000u);  // Caps: READ_SCANLINE
    wr32(p + 12, 0x00020000u); // Caps2: FULLSCREENGAMMA
    wr32(
        p + 16,
        0x000003a0u); // Caps3: ALPHA_FULLSCREEN_FLIP_OR_DISCARD, COPY_TO_VIDMEM, COPY_TO_SYSTEMMEM, LINEAR_TO_SRGB
    wr32(p + 20, 0x8000000fu); // PresentationIntervals: IMMEDIATE, ONE..FOUR
    wr32(p + 28, 0x001bbef0u); // DevCaps: HWTRANSFORMANDLIGHT, PUREDEVICE, HWRASTERIZATION, ...
    wr32(
        p + 32,
        0x0003ccf2u); // PrimitiveMiscCaps: masks, cull modes, blend op, separate alpha, independent write masks
    wr32(
        p + 36,
        0x0f732191u); // RasterCaps: dither, zbias, fog, anisotropy, scissor, slope-scale depth bias
    wr32(p + 40, 0x000000ffu); // ZCmpCaps: all
    wr32(p + 44, 0x00003fffu); // SrcBlendCaps: all, BLENDFACTOR
    wr32(p + 48, 0x00003fffu); // DestBlendCaps
    wr32(p + 52, 0x000000ffu); // AlphaCmpCaps
    wr32(p + 56, 0x00084208u); // ShadeCaps
    wr32(
        p + 60,
        0x0001ec45u); // TextureCaps: PERSPECTIVE, ALPHA, CUBEMAP, MIPMAP, MIPCUBEMAP, PROJECTED, VOLUMEMAP, ...
    wr32(
        p + 64,
        0x03030700u); // TextureFilterCaps: MIN point/linear/aniso, MIP point/linear, MAG point/linear
    wr32(p + 68, 0x03030300u);  // CubeTextureFilterCaps
    wr32(p + 72, 0x03030300u);  // VolumeTextureFilterCaps
    wr32(p + 76, 0x0000003fu);  // TextureAddressCaps
    wr32(p + 80, 0x0000003fu);  // VolumeTextureAddressCaps
    wr32(p + 84, 0x0000001fu);  // LineCaps
    wr32(p + 88, 4096);         // MaxTextureWidth
    wr32(p + 92, 4096);         // MaxTextureHeight
    wr32(p + 96, 512);          // MaxVolumeExtent
    wr32(p + 100, 8192);        // MaxTextureRepeat
    wr32(p + 104, 8192);        // MaxTextureAspectRatio
    wr32(p + 108, 16);          // MaxAnisotropy
    f(112, 1e10f);              // MaxVertexW
    f(116, -1e8f);              // GuardBandLeft
    f(120, -1e8f);              // GuardBandTop
    f(124, 1e8f);               // GuardBandRight
    f(128, 1e8f);               // GuardBandBottom
    wr32(p + 136, 0x000001ffu); // StencilCaps: all ops, TWOSIDED
    wr32(p + 140, 0x00080008u); // FVFCaps: 8 texture coordinates, PSIZE
    wr32(p + 144, 0x03feffffu); // TextureOpCaps
    wr32(p + 148, 8);           // MaxTextureBlendStages
    wr32(p + 152, 8);           // MaxSimultaneousTextures
    wr32(p + 156, 0x0000007bu); // VertexProcessingCaps
    wr32(p + 160, 8);           // MaxActiveLights
    wr32(p + 164, 6);           // MaxUserClipPlanes
    wr32(p + 168, 4);           // MaxVertexBlendMatrices
    f(176, 8192.0f);            // MaxPointSize
    wr32(p + 180, 1048575);     // MaxPrimitiveCount
    wr32(p + 184, 1048575);     // MaxVertexIndex
    wr32(p + 188, 16);          // MaxStreams
    wr32(p + 192, 255);         // MaxStreamStride
    wr32(p + 196, 0xfffe0300u); // VertexShaderVersion vs_3_0
    wr32(p + 200, 256);         // MaxVertexShaderConst
    wr32(p + 204, 0xffff0300u); // PixelShaderVersion ps_3_0
    f(208, 3.4e38f);            // PixelShaderMaxValue
    wr32(
        p + 212,
        0x00000051u); // DevCaps2: STREAMOFFSET, CAN_STRETCHRECT_FROM_TEXTURES, VERTEXELEMENTSCANSHARESTREAMOFFSET
    wr32(p + 232, 1);           // NumberOfAdaptersInGroup
    wr32(p + 236, 0x0000030fu); // DeclTypes
    wr32(p + 240, 4);           // NumSimultaneousRTs
    wr32(p + 244, 0x03000300u); // StretchRectFilterCaps: point, linear
    wr32(p + 248, 1);           // VS20Caps.Caps: PREDICATION
    wr32(p + 252, 24);          // VS20Caps.DynamicFlowControlDepth
    wr32(p + 256, 32);          // VS20Caps.NumTemps
    wr32(p + 260, 4);           // VS20Caps.StaticFlowControlDepth
    wr32(p + 264, 0x1f);        // PS20Caps.Caps
    wr32(p + 268, 24);          // PS20Caps.DynamicFlowControlDepth
    wr32(p + 272, 32);          // PS20Caps.NumTemps
    wr32(p + 276, 4);           // PS20Caps.StaticFlowControlDepth
    wr32(p + 280, 512);         // PS20Caps.NumInstructionSlots
    wr32(p + 284, 0x00000300u); // VertexTextureFilterCaps
    wr32(p + 288, 0xffffffffu); // MaxVShaderInstructionsExecuted
    wr32(p + 292, 0xffffffffu); // MaxPShaderInstructionsExecuted
    wr32(p + 296, 32768);       // MaxVertexShader30InstructionSlots
    wr32(p + 300, 32768);       // MaxPixelShader30InstructionSlots
}
void D9_GetDeviceCaps(X86 *c) {
    if (kD3d9WgpuEnabled) {
        uint32_t out = arg(c, 3);
        if (!this_d3d9(c) || arg(c, 1) != 0 || arg(c, 2) < D3DDEVTYPE_HAL || arg(c, 2) > 3 ||
            !out || !gm_valid(out, 304)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (arg(c, 2) != D3DDEVTYPE_HAL) {
            com_ret(c, D3DERR_NOTAVAILABLE);
            return;
        }
        recomp::d3d::D3d9CapsResult result = recomp::d3d::d3d9_adapter_caps(arg(c, 1));
        if (result.status.code != recomp::d3d::StatusCode::Ok || result.caps.size() != 304) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        memcpy(gm_ptr(out), result.caps.data(), 304);
        com_ret(c, D3D_OK9);
        return;
    }
    put_caps9(arg(c, 3));
    com_ret(c, D3D_OK9);
}
void D9_GetAdapterMonitor(X86 *c) {
    if (kD3d9WgpuEnabled && (!this_d3d9(c) || arg(c, 1) != 0)) {
        set_eax(c, 0);
        return;
    }
    set_eax(c, 1); // a non-null HMONITOR
}

void D9_CreateDevice(X86 *c) {
    ComObj *d3d = this_d3d9(c);
    uint32_t present = arg(c, 5), out = arg(c, 6);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled)
        com_out_ptr(out, 0);
    if (!d3d) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!present || !gm_valid(present, 56)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9DeviceRequest request{};
        request.adapter = arg(c, 1);
        request.device_type = arg(c, 2);
        request.behavior_flags = arg(c, 4);
        request.present.width = rd32(present + 0);
        request.present.height = rd32(present + 4);
        request.present.format = rd32(present + 8);
        request.present.count = rd32(present + 12);
        request.present.multisample_type = rd32(present + 16);
        request.present.multisample_quality = rd32(present + 20);
        request.present.swap_effect = rd32(present + 24);
        request.present.windowed = rd32(present + 32);
        request.present.enable_auto_depth_stencil = rd32(present + 36);
        request.present.auto_depth_stencil_format = rd32(present + 40);
        request.present.flags = rd32(present + 44);
        request.present.fullscreen_refresh_rate = rd32(present + 48);
        request.present.presentation_interval = rd32(present + 52);
        uint32_t focus_window = arg(c, 3);
        uint32_t device_window = rd32(present + 28);
        if (!device_window)
            device_window = focus_window;
        if (focus_window && !host_window_rect(focus_window, nullptr, nullptr, nullptr, nullptr)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (request.present.windowed) {
            int32_t w = 0, h = 0;
            if (!device_window || !host_window_rect(device_window, nullptr, nullptr, &w, &h) ||
                w <= 0 || h <= 0) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
            if (!request.present.width)
                request.present.width = (uint32_t)w;
            if (!request.present.height)
                request.present.height = (uint32_t)h;
        } else if (!focus_window || !win32_top_level(focus_window) || !device_window ||
                   !win32_top_level(device_window)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        request.display_mode = d3d9_device_display_mode(request.present);
        request.thread_id = guest_current_thread_id();
        ComObj *dev = com_new(K_D3D9DEVICE);
        if (!dev) {
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        dev->dev_d3d = d3d->id;
        com_addref(d3d);
        dev->d3d9_focus_window = focus_window;
        dev->hwnd = device_window;
        dev->d3d9_device = std::make_shared<D3d9HostDevice>();
        uint16_t previous_cw = c->fpu_cw;
        d3d9_setup_guest_fpu(c, request.behavior_flags);
        LOGW("d3d9: CreateDevice windowed=%u %ux%u fmt=%u count=%u msaa=%u/%u swap=%u flags=0x%x "
             "refresh=%u interval=0x%x behavior=0x%x desktop=%ux%u fmt=%u FPU CW %04x -> %04x",
             request.present.windowed, request.present.width, request.present.height,
             request.present.format, request.present.count, request.present.multisample_type,
             request.present.multisample_quality, request.present.swap_effect,
             request.present.flags, request.present.fullscreen_refresh_rate,
             request.present.presentation_interval, request.behavior_flags,
             request.display_mode.width, request.display_mode.height, request.display_mode.format,
             previous_cw, c->fpu_cw);
        recomp::d3d::D3d9CreateResult result =
            recomp::d3d::d3d9_create_device(*dev->d3d9_device->value, request);
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_release(dev);
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        uint32_t view = com_view(dev, IF_D3DDEVICE9);
        if (!view) {
            com_release(dev);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        wr32(present + 0, result.present.width);
        wr32(present + 4, result.present.height);
        wr32(present + 8, result.present.format);
        wr32(present + 12, result.present.count);
        wr32(present + 16, result.present.multisample_type);
        wr32(present + 20, result.present.multisample_quality);
        wr32(present + 24, result.present.swap_effect);
        wr32(present + 36, result.present.enable_auto_depth_stencil);
        wr32(present + 40, result.present.auto_depth_stencil_format);
        wr32(present + 44, result.present.flags);
        wr32(present + 48, result.present.fullscreen_refresh_rate);
        wr32(present + 52, result.present.presentation_interval);
        dev->width = result.present.width;
        dev->height = result.present.height;
        dev->bpp = 32;
        d3d9_sync_virtual_display(c, dev, dev->hwnd);
        com_out_ptr(out, view);
        com_ret(c, D3D_OK9);
        return;
    }
    com_out_ptr(out, 0);
    ComObj *dev = com_new(K_D3D9DEVICE);
    if (!dev) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    dev->dev_d3d = d3d->id;
    // D3DPRESENT_PARAMETERS: back buffer size and format first, the device
    // window at 28 and Windowed at 32.
    if (present) {
        dev->width = rd32(present + 0);
        dev->height = rd32(present + 4);
        dev->bpp = 32;
        dev->hwnd = rd32(present + 28);
        dev->samples = multisample_count(rd32(present + 16), rd32(present + 20));
    }
    uint32_t view = com_view(dev, IF_D3DDEVICE9);
    if (!view) {
        com_release(dev);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    {
        ComObj *bb = device_backbuffer(dev);
        D9Pipeline &pl = d9_pipeline(dev->id);
        pl.color_target[0] = bb ? bb->id : 0;
        pl.scissor[2] = (int32_t)dev->width;
        pl.scissor[3] = (int32_t)dev->height;
        if (present && rd32(present + 36)) {
            device_depthbuffer(dev);
            pl.rs[7] = 1; // ZENABLE defaults on with an automatic depth buffer
            pl.states_changed();
        }
    }
    LOGW("d3d9: CreateDevice %ux%u, window %08x, %s renderer", dev->width, dev->height, dev->hwnd,
         host_d9_active() ? "GPU" : "CPU");
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

static ComObj *device_backbuffer(ComObj *dev);
static ComObj *device_depthbuffer(ComObj *dev);

// (this, pRect)
void Dev_SetScissorRect(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t r = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !r || !gm_valid(r, 16)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->set_scissor(
                                        (int32_t)rd32(r), (int32_t)rd32(r + 4),
                                        (int32_t)rd32(r + 8), (int32_t)rd32(r + 12))));
        return;
    }
    if (dev && r)
        for (int i = 0; i < 4; ++i)
            d9_pipeline(dev->id).scissor[i] = (int32_t)rd32(r + 4u * (uint32_t)i);
    com_ret(c, dev && r ? D3D_OK9 : D3DERR_INVALIDCALL);
}

static ComObj *device_backbuffer(ComObj *dev);
static ComObj *device_depthbuffer(ComObj *dev);
static void device_forget_buffers(ComObj *dev);

static void d3d9_reset_transaction(ComObj *dev, const recomp::d3d::D3d9ResetRequest &request,
                                   recomp::d3d::D3d9ResetResult &result) {
    result = dev->d3d9_device->reset(request);
    if (result.status.code != recomp::d3d::StatusCode::Ok)
        return;
    recomp::d3d::ValuesResult ids = dev->d3d9_device->registered_resource_ids();
    if (ids.status.code != recomp::d3d::StatusCode::Ok) {
        dev->d3d9_device->abort_reset();
        result.status = ids.status;
        return;
    }
    for (uint32_t id : ids.values) {
        ComObj *o = com_get(id);
        if (!o || !o->d3d9_resource || o->d3d9_parent || o->d3d9_implicit ||
            o->dev_d3d != dev->id) {
            dev->d3d9_device->abort_reset();
            LOGW("d3d9: reset holder %u is not a root resource of this device", id);
            result.status.code = recomp::d3d::StatusCode::InvalidArgument;
            return;
        }
    }
    for (uint32_t id : ids.values) {
        ComObj *o = com_get(id);
        recomp::d3d::Status prepared =
            dev->d3d9_device->prepare_resource_reset(o->id, *o->d3d9_resource);
        if (prepared.code != recomp::d3d::StatusCode::Ok) {
            dev->d3d9_device->abort_reset();
            result.status = prepared;
            return;
        }
    }
    recomp::d3d::Status ready = dev->d3d9_device->reset_ready();
    if (ready.code != recomp::d3d::StatusCode::Ok) {
        dev->d3d9_device->abort_reset();
        result.status = ready;
        return;
    }
    for (uint32_t id : ids.values)
        dev->d3d9_device->commit_resource_reset(id, *com_get(id)->d3d9_resource);
    result = dev->d3d9_device->finish_reset();
    d3d9_refresh_bindings(dev);
}

void Dev_Reset(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t present = arg(c, 1);
    if (!dev || !present) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev->d3d9_device || !gm_valid(present, 56)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9ResetRequest request{};
        request.present.width = rd32(present + 0);
        request.present.height = rd32(present + 4);
        request.present.format = rd32(present + 8);
        request.present.count = rd32(present + 12);
        request.present.multisample_type = rd32(present + 16);
        request.present.multisample_quality = rd32(present + 20);
        request.present.swap_effect = rd32(present + 24);
        request.present.windowed = rd32(present + 32);
        request.present.enable_auto_depth_stencil = rd32(present + 36);
        request.present.auto_depth_stencil_format = rd32(present + 40);
        request.present.flags = rd32(present + 44);
        request.present.fullscreen_refresh_rate = rd32(present + 48);
        request.present.presentation_interval = rd32(present + 52);
        uint32_t focus_window = dev->d3d9_focus_window;
        uint32_t device_window = rd32(present + 28);
        if (!device_window)
            device_window = focus_window;
        bool windowed = request.present.windowed != 0;
        request.window_valid = false;
        if (windowed) {
            int32_t w = 0, h = 0;
            if (device_window && host_window_rect(device_window, nullptr, nullptr, &w, &h) &&
                w > 0 && h > 0) {
                if (!request.present.width)
                    request.present.width = (uint32_t)w;
                if (!request.present.height)
                    request.present.height = (uint32_t)h;
                request.window_valid = true;
            }
        } else {
            request.window_valid = focus_window && win32_top_level(focus_window) && device_window &&
                                   win32_top_level(device_window);
        }
        request.implicit_external_refs =
            implicit_surface_externally_referenced(dev->d3d9_backbuffer) ||
            implicit_surface_externally_referenced(dev->d3d9_depthbuffer);
        request.display_mode = d3d9_device_display_mode(request.present);
        request.thread_id = guest_current_thread_id();
        recomp::d3d::D3d9ResetResult result{};
        d3d9_reset_transaction(dev, request, result);
        wr32(present + 0, result.present.width);
        wr32(present + 4, result.present.height);
        wr32(present + 8, result.present.format);
        wr32(present + 12, result.present.count);
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        if (dev->hwnd != device_window)
            win32_uncover_display(c, dev->hwnd);
        dev->hwnd = device_window;
        device_discard_implicit_surfaces(dev);
        d3d9_sync_virtual_display(c, dev, dev->hwnd);
        LOGW("d3d9: Reset to %ux%u windowed=%u", request.present.width, request.present.height,
             request.present.windowed);
        com_ret(c, D3D_OK9);
        return;
    }
    dev->width = rd32(present + 0);
    dev->height = rd32(present + 4);
    dev->samples = multisample_count(rd32(present + 16), rd32(present + 20));
    device_forget_buffers(dev);
    ComObj *bb = device_backbuffer(dev);
    dev->render_target = bb ? bb->id : 0;
    D9Pipeline &pl = d9_pipeline(dev->id);
    pl.color_target[0] = dev->render_target;
    for (int i = 1; i < 4; ++i)
        pl.color_target[i] = 0;
    pl.viewport_set = false;
    pl.scissor[0] = pl.scissor[1] = 0;
    pl.scissor[2] = (int32_t)dev->width;
    pl.scissor[3] = (int32_t)dev->height;
    if (rd32(present + 36)) { // EnableAutoDepthStencil
        device_depthbuffer(dev);
        pl.rs[7] = 1;
        pl.states_changed();
    } else
        dev->zbuffer_obj = 0;
    LOGW("d3d9: Reset to %ux%u", dev->width, dev->height);
    com_ret(c, D3D_OK9);
}

void Dev_TestCooperativeLevel(X86 *c) {
    ComObj *dev = this_device9(c);
    if (!dev || (kD3d9WgpuEnabled && !dev->d3d9_device)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        recomp::d3d::ValueResult result =
            dev->d3d9_device->test_cooperative_level(guest_current_thread_id());
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        if (result.value == 1) {
            com_ret(c, D3DERR_DEVICELOST);
            return;
        }
        if (result.value == 2) {
            com_ret(c, D3DERR_DEVICENOTRESET);
            return;
        }
        com_ret(c, D3D_OK9);
        return;
    }
    com_ret(c, D3D_OK9);
}
void Dev_GetAvailableTextureMem(X86 *c) {
    if (kD3d9WgpuEnabled) {
        ComObj *dev = this_device9(c);
        if (!dev || !dev->d3d9_device) {
            imports_unsupported(c);
            return;
        }
        recomp::d3d::ValueResult result = dev->d3d9_device->available_texture_mem();
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            std::string message(result.status.message.data(), result.status.message.size());
            LOGW("d3d9: GetAvailableTextureMem: %s", message.c_str());
            imports_unsupported(c);
            return;
        }
        set_eax(c, result.value);
        return;
    }
    set_eax(c, 256u * 1024u * 1024u);
}
void Dev_GetNumberOfSwapChains(X86 *c) {
    ComObj *dev = this_device9(c);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        set_eax(c, dev->d3d9_device->number_of_swap_chains().value);
        return;
    }
    set_eax(c, 1);
}
// (this, iSwapChain, pMode)
void Dev_GetDisplayMode(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!dev || !out || !gm_valid(out, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (arg(c, 1) != 0 || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9DisplayMode mode = d3d9_current_display_mode();
        recomp::d3d::D3d9DisplayModeRequest request{};
        request.swap_chain = arg(c, 1);
        request.width = mode.width;
        request.height = mode.height;
        request.refresh_rate = mode.refresh_rate;
        request.format = mode.format;
        recomp::d3d::D3d9DisplayModeResult result = dev->d3d9_device->display_mode(request);
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        wr32(out + 0, result.width);
        wr32(out + 4, result.height);
        wr32(out + 8, result.refresh_rate);
        wr32(out + 12, result.format);
        com_ret(c, D3D_OK9);
        return;
    }
    put_display_mode(out);
    com_ret(c, D3D_OK9);
}
void Dev_GetDirect3D(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *d3d = dev ? com_get(dev->dev_d3d) : nullptr;
    uint32_t out = arg(c, 1);
    if (!d3d || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(d3d, IF_D3D9);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(d3d);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}
void Dev_GetCreationParameters(X86 *c) {
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9CreationParametersResult result =
        recomp::d3d::d3d9_creation_parameters(*dev->d3d9_device->value);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    wr32(out + 0, result.parameters.adapter);
    wr32(out + 4, result.parameters.device_type);
    wr32(out + 8, dev->d3d9_focus_window);
    wr32(out + 12, result.parameters.behavior_flags);
    com_ret(c, D3D_OK9);
}
void Dev_GetDeviceCaps(X86 *c) {
    uint32_t out = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        ComObj *dev = this_device9(c);
        if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 304)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9CapsResult result = dev->d3d9_device->caps();
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        if (result.caps.size() >= 304)
            memcpy(gm_ptr(out), result.caps.data(), 304);
        com_ret(c, D3D_OK9);
        return;
    }
    put_caps9(out);
    com_ret(c, D3D_OK9);
}
void Dev_EvictManagedResources(X86 *c) {
    com_ret(c, this_device9(c) ? D3D_OK9 : D3DERR_INVALIDCALL);
}
void Dev_BeginScene(X86 *c) {
    ComObj *dev = this_device9(c);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->begin_scene()));
        return;
    }
    if (dev)
        dev->in_scene = true;
    com_ret(c, dev ? D3D_OK9 : D3DERR_INVALIDCALL);
}
void Dev_EndScene(X86 *c) {
    ComObj *dev = this_device9(c);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->end_scene()));
        return;
    }
    if (dev)
        dev->in_scene = false;
    com_ret(c, dev ? D3D_OK9 : D3DERR_INVALIDCALL);
}
// Defined with the resources below.
static ComObj *device_backbuffer(ComObj *dev);
static uint8_t *resource_bytes(ComObj *o);

// (this, Count, pRects, Flags, Color, Z, Stencil). Only the colour target is
// real so far: a 32-bit surface is filled with the colour, over the listed
// D3DRECTs or the whole surface.
void Dev_Clear(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t count = arg(c, 1), rects = arg(c, 2), flags = arg(c, 3), color = arg(c, 4);
    if (!dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if ((!count && rects) ||
            (count && (count > 0x0fffffffu || !rects || !gm_fits(rects, count * 16u)))) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        std::vector<recomp::d3d::D3d9Rect> list;
        list.reserve(count);
        for (uint32_t i = 0; i < count; ++i) {
            recomp::d3d::D3d9Rect r;
            uint32_t a = rects + i * 16u;
            r.left = (int32_t)rd32(a);
            r.top = (int32_t)rd32(a + 4);
            r.right = (int32_t)rd32(a + 8);
            r.bottom = (int32_t)rd32(a + 12);
            list.push_back(r);
        }
        float z;
        uint32_t zbits = arg(c, 5);
        memcpy(&z, &zbits, 4);
        rust::Slice<const recomp::d3d::D3d9Rect> rect_slice(list.empty() ? nullptr : list.data(),
                                                            list.size());
        com_ret(c,
                bridge_result(c, dev->d3d9_device->clear(flags, color, z, arg(c, 6), rect_slice)));
        return;
    }
    if (gpu_on()) {
        HostD9Target target = gpu_target(dev);
        int32_t vp[4];
        target_viewport(dev, target, vp);
        std::vector<int32_t> rs;
        if (count && rects && gm_fits(rects, count * 16u))
            for (uint32_t i = 0; i < count * 4; ++i)
                rs.push_back((int32_t)rd32(rects + 4 * i));
        float z;
        uint32_t zbits = arg(c, 5);
        memcpy(&z, &zbits, 4);
        host_d9_clear(&target, vp, (uint32_t)(rs.size() / 4), rs.empty() ? nullptr : rs.data(),
                      flags, color, z, arg(c, 6));
        gpu_target_drawn(dev, flags & 1u, flags & 6u);
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *s = com_get(dev->render_target);
    if (!s)
        s = device_backbuffer(dev);
    static uint32_t g_clears = 0;
    if (++g_clears <= 24)
        LOGW("d3d9: clear %u: flags %x colour %08x rects %u, target %u (%ux%u, %u bpp)%s", g_clears,
             flags, color, count, s ? s->id : 0, s ? s->width : 0, s ? s->height : 0,
             s ? s->bpp : 0, (s && s->id == dev->palette_obj) ? ", the back buffer" : "");
    uint8_t *bytes = resource_bytes(s);
    if ((flags & 1u) && bytes && s->bpp == 32) {
        uint32_t n = (count && rects) ? count : 1;
        for (uint32_t r = 0; r < n; ++r) {
            int32_t x1 = 0, y1 = 0, x2 = (int32_t)s->width, y2 = (int32_t)s->height;
            if (count && rects) {
                uint32_t q = rects + r * 16u;
                x1 = std::max(x1, (int32_t)rd32(q));
                y1 = std::max(y1, (int32_t)rd32(q + 4));
                x2 = std::min(x2, (int32_t)rd32(q + 8));
                y2 = std::min(y2, (int32_t)rd32(q + 12));
            }
            for (int32_t y = y1; y < y2; ++y) {
                uint8_t *row = bytes + (size_t)y * s->pitch;
                for (int32_t x = x1; x < x2; ++x)
                    memcpy(row + 4 * x, &color, 4);
            }
        }
        d9_raster_invalidate(s->id);
    }
    com_ret(c, D3D_OK9);
}

// (this, pSourceRect, pDestRect, hDestWindowOverride, pDirtyRegion). The back
// buffer goes to the host as a 32-bit frame: the first thing this module puts
// on screen.
void Dev_Present(X86 *c) {
    ComObj *dev = this_device9(c);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (arg(c, 1) || arg(c, 2) || arg(c, 3) || arg(c, 4)) {
            imports_unsupported(c);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        bool presented = false;
        if (!dx_dump_enabled() && dev->d3d9_device->native_present_available) {
            auto result = dev->d3d9_device->present_handoff();
            if (result.status.code == recomp::d3d::StatusCode::Ok) {
                presented = host_display_present_native_texture(
                    result.handoff.texture, (int)result.handoff.width, (int)result.handoff.height,
                    result.handoff.busy, result.handoff.ready_event, result.handoff.ready_value);
                if (!presented) {
                    __atomic_store_n(result.handoff.busy, 0u, __ATOMIC_RELEASE);
                    dev->d3d9_device->native_present_available = false;
                    LOGW("d3d9: presenter declined native texture handoff; using RGBA readback");
                }
            } else if (result.status.code != recomp::d3d::StatusCode::Unsupported) {
                com_ret(c, bridge_result(c, result.status));
                return;
            } else {
                dev->d3d9_device->native_present_available = false;
                std::string message(result.status.message.data(), result.status.message.size());
                LOGW("d3d9: %s; using RGBA readback", message.c_str());
            }
        }
        if (!presented) {
            recomp::d3d::D3d9FrameResult result = dev->d3d9_device->present();
            if (result.status.code != recomp::d3d::StatusCode::Ok) {
                com_ret(c, bridge_result(c, result.status));
                return;
            }
            if (!result.bytes.empty()) {
                dx_dump_frame_rgba(result.bytes.data(), result.width, result.height);
                host_display_present_window_rgba(result.bytes.data(), (int)result.width,
                                                 (int)result.height);
            }
        }
        recomp::d3d::D3d9PresentParams effective = dev->d3d9_device->effective_present();
        if (effective.presentation_interval != 0x80000000u) {
            const double delay = host_present_refresh_delay(1);
            if (delay > 0)
                guest_sleep_ms((uint32_t)(delay * 1000.0));
        }
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *bb = dev ? device_backbuffer(dev) : nullptr;
    if (bb && gpu_on()) {
        gpu_sync(bb);
        ddraw_external_present_begin();
        host_present(nullptr, (int)bb->width, (int)bb->height, 32, nullptr, (int)bb->pitch);
        host_d9_present(bb->id, bb->width, bb->height);
        ddraw_external_present_end();
        com_ret(c, D3D_OK9);
        return;
    }
    uint8_t *bbytes = resource_bytes(bb);
    if (bb && bbytes) {
        uint32_t lit = 0, n = bb->width * bb->height;
        for (uint32_t i = 0; i < n; i += 97) {
            uint32_t v;
            memcpy(&v, bbytes + (size_t)i * 4, 4);
            if (v & 0x00ffffffu)
                ++lit;
        }
        LOGW("d3d9: present back buffer %u (%ux%u, %u bpp), %u of %u sampled pixels lit, target "
             "now %u",
             bb->id, bb->width, bb->height, bb->bpp, lit, (n + 96) / 97, dev->render_target);
    }
    if (bb && bbytes && bb->bpp == 32) {
        ddraw_external_present_begin();
        host_present(bbytes, (int)bb->width, (int)bb->height, 32, nullptr, (int)bb->pitch);
        ddraw_external_present_end();
    }
    com_ret(c, D3D_OK9);
}

// ---------------------------------------------------------------------------
// Device resources
//
// A game creates textures, buffers and surfaces before it draws anything, and
// then writes its geometry and its images into them. None of that reaches a
// GPU yet, but the storage is real guest memory, so a Lock hands back a
// pointer the game can fill and the contents survive to be drawn later.
// ---------------------------------------------------------------------------
// A resource's contents live in host memory, as a driver's would: the guest
// heap is the game's, and a texture set would exhaust it. The guest sees the
// bytes only while the resource is locked, through a copy made on the first
// lock and written back on the last unlock.
static uint32_t resource_alloc(ComObj *o, uint32_t bytes) {
    if (!bytes)
        return 0;
    o->blob.assign(bytes, 0);
    o->pixels = 0;
    o->pixels_bytes = bytes;
    return 1;
}

// The bytes to read or write right now: the guest copy while locked.
static uint8_t *resource_bytes(ComObj *o) {
    if (!o)
        return nullptr;
    if (o->pixels)
        return gm_ptr(o->pixels);
    return o->blob.empty() ? nullptr : o->blob.data();
}

static void gpu_before_lock(ComObj *s);
static void gpu_after_unlock(ComObj *o, uint32_t lo, uint32_t hi);
static void gpu_forget(ComObj *o);

static uint32_t stage_lock(ComObj *o) {
    if (!o || o->blob.empty())
        return 0;
    if (o->lock_count == 0)
        gpu_before_lock(o);
    if (o->lock_count++ == 0) {
        o->pixels = heap_alloc((uint32_t)o->blob.size(), false, 16);
        if (!o->pixels) {
            o->lock_count = 0;
            return 0;
        }
        memcpy(gm_ptr(o->pixels), o->blob.data(), o->blob.size());
    }
    return o->pixels;
}

static void stage_unlock(ComObj *o) {
    if (!o || o->lock_count <= 0)
        return;
    if (--o->lock_count == 0 && o->pixels) {
        memcpy(o->blob.data(), gm_ptr(o->pixels), o->blob.size());
        heap_free(o->pixels);
        o->pixels = 0;
        d9_raster_invalidate(o->id);
        gpu_after_unlock(o, 0, (uint32_t)o->blob.size());
    }
}

static void d3d9_release_staging(D3d9HostResource *h) {
    if (!h)
        return;
    for (auto &entry : h->staging)
        if (entry.second.addr)
            heap_free(entry.second.addr);
    h->staging.clear();
}

static void d3d9_resource_destroy(ComObj *o) {
    if (kD3d9WgpuEnabled) {
        if (o->d3d9_resource && o->dev_d3d) {
            if (ComObj *dev = com_get(o->dev_d3d)) {
                if (dev->d3d9_device)
                    dev->d3d9_device->unregister_resource(o->id);
            }
        }
        if (!o->d3d9_parent && o->d3d9_resource)
            d3d9_release_staging(o->d3d9_resource.get());
        o->d3d9_resource.reset();
        if (o->d3d9_parent_retained) {
            o->d3d9_parent_retained = false;
            if (ComObj *parent = com_get(o->d3d9_parent))
                com_release_internal(parent);
        }
        o->d3d9_parent = 0;
        if (o->d3d9_owner_retained) {
            o->d3d9_owner_retained = false;
            if (ComObj *dev = com_get(o->dev_d3d))
                com_release_internal(dev);
        }
        o->dev_d3d = 0;
        return;
    }
    if (o->pixels)
        heap_free(o->pixels);
    o->pixels = 0;
    o->lock_count = 0;
    o->pixels_bytes = 0;
    std::vector<uint8_t>().swap(o->blob);
    d9_raster_invalidate(o->id);
    gpu_forget(o);
}

// Bytes per pixel for the formats this game creates. D3DFMT_A8R8G8B8 is 21,
// X8R8G8B8 22, R5G6B5 23, A8 28, D24S8 75, and the DXT block formats carry
// their four characters.
static uint32_t format_bytes(uint32_t fmt) {
    switch (fmt) {
    case 27:
    case 28:
    case 41:
    case 50:
    case 52: // R3G3B2, A8, P8, L8, A4L4
        return 1;
    case 23:
    case 24:
    case 25:
    case 26:
    case 29:
    case 30:
    case 40:
    case 51:
    case 60:
    case 61:
    case 70:
    case 73:
    case 80:
    case 81:
    case 111:
        return 2;
    case 36:
    case 113: // A16B16G16R16, A16B16G16R16F
        return 8;
    case 116: // A32B32G32R32F
        return 16;
    default:
        return 4;
    }
}

// The DXT formats, stored as blocks of 4x4 pixels.
static uint32_t dxt_block_bytes(uint32_t fmt) {
    switch (fmt) {
    case D3DFMT_DXT1:
        return 8;
    case D3DFMT_DXT2:
    case D3DFMT_DXT3:
    case D3DFMT_DXT4:
    case D3DFMT_DXT5:
        return 16;
    default:
        return 0;
    }
}

static ComObj *make_surface(ComObj *dev, uint32_t w, uint32_t h, uint32_t fmt) {
    ComObj *s = com_new(K_D3D9SURFACE);
    if (!s)
        return nullptr;
    s->dev_d3d = dev ? dev->id : 0;
    s->width = w ? w : 1;
    s->height = h ? h : 1;
    s->rmask = fmt; // the D3DFORMAT, which a Direct3D 9 surface has no other field for
    if (uint32_t block = dxt_block_bytes(fmt)) {
        // A compressed surface is rows of blocks; Pitch is the bytes in one.
        uint32_t bw = (s->width + 3) / 4, bh = (s->height + 3) / 4;
        s->bpp = 0;
        s->pitch = bw * block;
        resource_alloc(s, s->pitch * bh);
    } else {
        s->bpp = format_bytes(fmt) * 8;
        s->pitch = s->width * format_bytes(fmt);
        resource_alloc(s, s->pitch * s->height);
    }
    return s;
}

// ---- What the CPU renderer needs to know about surfaces and textures --------
bool d9_surface_info(uint32_t surface_id, D9SurfaceInfo *out) {
    ComObj *s = com_get(surface_id);
    if (!s || s->kind != K_D3D9SURFACE)
        return false;
    out->data = resource_bytes(s);
    out->size = s->blob.size();
    out->width = s->width;
    out->height = s->height;
    out->pitch = s->pitch;
    out->format = s->rmask;
    return true;
}

uint32_t d9_texture_level0(uint32_t texture_id) {
    ComObj *t = com_get(texture_id);
    if (!t)
        return 0;
    if (t->kind == K_D3D9SURFACE)
        return t->id;
    if (t->kind != K_D3D9TEXTURE)
        return 0;
    if (t->caps) // a cube: face zero, level zero, once it exists
        return t->surfaces.empty() ? 0 : t->surfaces[0];
    return t->back_obj;
}

// The surface behind a 2D texture's level, or null.
static ComObj *texture_level(ComObj *tex, uint32_t level) {
    if (!tex || tex->kind != K_D3D9TEXTURE || tex->caps)
        return nullptr;
    if (level < tex->surfaces.size())
        return com_get(tex->surfaces[level]);
    return level == 0 ? com_get(tex->back_obj) : nullptr;
}

// ---- The GPU's copies ------------------------------------------------------
// With a GPU renderer running, every texture, surface and buffer has a mirror
// on the host under the object's id; a surface that belongs to a texture is a
// slot of its container's mirror. The shim's bytes stay the reference for what
// the game wrote, the GPU's for what was drawn: a slot the game unlocked is
// uploaded before the next draw that reads it, and a slot the GPU drew into is
// read back before the game locks it.
static const uint32_t USAGE_RENDERTARGET = 1, USAGE_DEPTHSTENCIL = 2;

struct GpuMirror {
    bool defined = false;
    std::vector<uint8_t> cpu_dirty;      // by slot
    std::vector<uint8_t> gpu_newer;      // by slot
    uint32_t dirty_lo = 0, dirty_hi = 0; // buffers: the changed byte range
};
static std::unordered_map<uint32_t, GpuMirror> &gpu_mirrors() {
    static auto *m = new std::unordered_map<uint32_t, GpuMirror>();
    return *m;
}
// D3DUSAGE of every texture and surface, by id.
static std::unordered_map<uint32_t, uint32_t> &resource_usage() {
    static auto *m = new std::unordered_map<uint32_t, uint32_t>();
    return *m;
}
static bool gpu_on() {
    return host_d9_active() != 0;
}
static bool is_depth_format(uint32_t fmt) {
    return (fmt >= 70 && fmt <= 82) || fmt == 0x5a544e49u /* INTZ */ ||
           fmt == 0x34324644u /* DF24 */ || fmt == 0x36314644u /* DF16 */;
}

// A surface's place in its mirror: the container's id and the slot, or the
// surface's own id and slot zero.
static uint32_t mirror_slot(ComObj *s, uint32_t *slot) {
    *slot = 0;
    if (!s)
        return 0;
    if (s->kind == K_D3D9SURFACE && s->front_obj) {
        ComObj *tex = com_get(s->front_obj);
        if (tex && tex->kind == K_D3D9TEXTURE) {
            for (size_t i = 0; i < tex->surfaces.size(); ++i)
                if (tex->surfaces[i] == s->id) {
                    *slot = (uint32_t)i;
                    return tex->id;
                }
        }
    }
    return s->id;
}
static HostD9Surface surface_ref(ComObj *s) {
    HostD9Surface r = {0, 0, 0};
    if (!s)
        return r;
    uint32_t slot;
    r.id = mirror_slot(s, &slot);
    ComObj *owner = com_get(r.id);
    if (owner && owner->kind == K_D3D9TEXTURE && owner->caps) {
        r.face = slot % 6;
        r.level = slot / 6;
    } else {
        r.level = slot;
    }
    return r;
}
static uint32_t slot_count(ComObj *o) {
    if (o->kind == K_D3D9TEXTURE)
        return (uint32_t)std::max<size_t>(o->surfaces.size(), 1);
    return 1;
}
static ComObj *slot_surface(ComObj *o, uint32_t slot) {
    if (o->kind != K_D3D9TEXTURE)
        return o;
    return slot < o->surfaces.size() ? com_get(o->surfaces[slot]) : nullptr;
}

// Creates the mirror of a texture or stand-alone surface on first use.
static GpuMirror &gpu_define(ComObj *o) {
    GpuMirror &m = gpu_mirrors()[o->id];
    if (m.defined)
        return m;
    HostD9TextureDesc d{};
    d.id = o->id;
    d.format = o->rmask ? o->rmask : 22;
    d.width = o->width ? o->width : 1;
    d.height = o->height ? o->height : 1;
    uint32_t usage = resource_usage()[o->id];
    if (is_depth_format(d.format))
        usage |= USAGE_DEPTHSTENCIL;
    d.usage = (usage & USAGE_RENDERTARGET ? HOST_D9_USAGE_RENDERTARGET : 0) |
              (usage & USAGE_DEPTHSTENCIL ? HOST_D9_USAGE_DEPTH : 0);
    d.samples = o->kind == K_D3D9SURFACE ? o->samples : 0;
    if (o->kind == K_D3D9TEXTURE && o->caps) {
        d.kind = HOST_D9_TEX_CUBE;
        d.levels = o->pal_flags ? o->pal_flags : 1;
    } else {
        d.kind = HOST_D9_TEX_2D;
        d.levels = o->kind == K_D3D9TEXTURE ? slot_count(o) : 1;
    }
    host_d9_texture_define(&d);
    m.defined = true;
    uint32_t n = o->kind == K_D3D9TEXTURE && o->caps ? 6 * d.levels : slot_count(o);
    // A target's first contents are whatever is drawn; a texture's are what
    // the game wrote, so every slot starts out needing an upload.
    m.cpu_dirty.assign(n, d.usage ? 0 : 1);
    m.gpu_newer.assign(n, 0);
    return m;
}

// Uploads every slot the game changed since the GPU last saw it.
static void gpu_sync(ComObj *o) {
    if (!o)
        return;
    GpuMirror &m = gpu_define(o);
    for (uint32_t slot = 0; slot < m.cpu_dirty.size(); ++slot) {
        if (!m.cpu_dirty[slot])
            continue;
        m.cpu_dirty[slot] = 0;
        ComObj *s = slot_surface(o, slot);
        if (!s || s->blob.empty())
            continue;
        HostD9Surface r = surface_ref(s);
        host_d9_texture_upload(o->id, r.face, r.level, resource_bytes(s), s->pitch);
    }
}

// The surface is about to be read or written by the game.
static void gpu_before_lock(ComObj *s) {
    if (!s || s->kind != K_D3D9SURFACE)
        return;
    uint32_t slot;
    uint32_t id = mirror_slot(s, &slot);
    auto it = gpu_mirrors().find(id);
    if (it == gpu_mirrors().end() || slot >= it->second.gpu_newer.size() ||
        !it->second.gpu_newer[slot])
        return;
    it->second.gpu_newer[slot] = 0;
    if (s->bpp == 32 && !s->blob.empty()) {
        HostD9Surface r = surface_ref(s);
        host_d9_texture_read(id, r.face, r.level, s->blob.data(), s->pitch);
    }
}
// The game wrote the resource.
static void gpu_after_unlock(ComObj *o, uint32_t lo, uint32_t hi) {
    if (!o)
        return;
    if (o->kind == K_D3D9VB || o->kind == K_D3D9IB) {
        GpuMirror &m = gpu_mirrors()[o->id];
        if (m.dirty_hi <= m.dirty_lo) {
            m.dirty_lo = lo;
            m.dirty_hi = hi;
        } else {
            m.dirty_lo = std::min(m.dirty_lo, lo);
            m.dirty_hi = std::max(m.dirty_hi, hi);
        }
        m.defined = true;
        return;
    }
    uint32_t slot;
    uint32_t id = mirror_slot(o, &slot);
    auto it = gpu_mirrors().find(id);
    if (it == gpu_mirrors().end())
        return; // not mirrored yet: its first use uploads everything
    if (slot < it->second.cpu_dirty.size())
        it->second.cpu_dirty[slot] = 1;
}
static void gpu_drawn_into(ComObj *s) {
    if (!s)
        return;
    uint32_t slot;
    uint32_t id = mirror_slot(s, &slot);
    ComObj *owner = com_get(id);
    if (!owner)
        return;
    GpuMirror &m = gpu_define(owner);
    if (slot < m.gpu_newer.size()) {
        m.gpu_newer[slot] = 1;
        m.cpu_dirty[slot] = 0;
    }
}
static void gpu_sync_buffer(ComObj *b) {
    if (!b)
        return;
    GpuMirror &m = gpu_mirrors()[b->id];
    if (!m.defined) {
        m.defined = true;
        m.dirty_lo = 0;
        m.dirty_hi = (uint32_t)b->blob.size();
    }
    if (m.dirty_hi > m.dirty_lo && !b->blob.empty()) {
        uint32_t hi = std::min<uint32_t>(m.dirty_hi, (uint32_t)b->blob.size());
        host_d9_buffer_upload(b->id, (uint32_t)b->blob.size(), m.dirty_lo,
                              b->blob.data() + m.dirty_lo, hi - m.dirty_lo);
    }
    m.dirty_lo = m.dirty_hi = 0;
}
static void gpu_forget(ComObj *o) {
    auto it = gpu_mirrors().find(o->id);
    if (it != gpu_mirrors().end()) {
        if (o->kind == K_D3D9VB || o->kind == K_D3D9IB)
            host_d9_buffer_drop(o->id);
        else if (it->second.defined)
            host_d9_texture_drop(o->id);
        gpu_mirrors().erase(it);
    }
    resource_usage().erase(o->id);
}

static void d3d9_register_wgpu_resource(ComObj *dev, ComObj *o) {
    if (!dev || !dev->d3d9_device || !o || !o->d3d9_resource)
        return;
    recomp::d3d::Status status = dev->d3d9_device->register_resource(o->id, *o->d3d9_resource);
    if (status.code != recomp::d3d::StatusCode::Ok) {
        std::string message(status.message.data(), status.message.size());
        LOGW("d3d9: register resource %u failed: %s", o->id, message.c_str());
    }
}

static void create_buffer_wgpu(X86 *c, recomp::d3d::D3d9BufferKind kind) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 5);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_out_ptr(out, 0);
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    bool vertex = kind == recomp::d3d::D3d9BufferKind::Vertex;
    recomp::d3d::D3d9BufferRequest request{};
    request.kind = kind;
    request.size = arg(c, 1);
    request.usage = arg(c, 2);
    request.pool = arg(c, 4);
    request.fvf = vertex ? arg(c, 3) : 0;
    request.format = vertex ? 100 : arg(c, 3);
    request.shared_handle = arg(c, 6) != 0;
    auto resource = std::make_shared<D3d9HostResource>();
    recomp::d3d::Status status = resource->create_buffer(*dev->d3d9_device, request);
    if (status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, status));
        return;
    }
    ComKind resource_kind = vertex ? K_D3D9VB : K_D3D9IB;
    ComIface resource_iface = vertex ? IF_D3DVERTEXBUFFER9 : IF_D3DINDEXBUFFER9;
    ComObj *buffer = com_new(resource_kind);
    if (!buffer) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    buffer->d3d9_resource = std::move(resource);
    buffer->dev_d3d = dev->id;
    com_retain_internal(dev);
    buffer->d3d9_owner_retained = true;
    d3d9_register_wgpu_resource(dev, buffer);
    uint32_t view = com_view(buffer, resource_iface);
    if (!view) {
        com_release(buffer);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

static ComObj *level_owner(ComObj *o, uint32_t *face, uint32_t *level, bool *volume) {
    *face = 0;
    *level = 0;
    *volume = false;
    if (!o)
        return nullptr;
    if (o->d3d9_parent) {
        *face = o->d3d9_face;
        *level = o->d3d9_level;
        *volume = o->d3d9_volume;
        return com_get(o->d3d9_parent);
    }
    return o;
}

static ComObj *com_level_view(ComObj *tex, uint32_t face, uint32_t level, bool volume) {
    if (!tex || !tex->d3d9_resource)
        return nullptr;
    uint32_t slot;
    if (volume)
        slot = level;
    else if (tex->kind == K_D3D9CUBETEXTURE)
        slot = face + 6u * level;
    else
        slot = level;
    if (tex->d3d9_levels.size() <= slot)
        tex->d3d9_levels.resize(slot + 1, 0);
    ComObj *s = com_get(tex->d3d9_levels[slot]);
    if (s) {
        com_addref(s);
        return s;
    }
    s = com_new(volume ? K_D3D9VOLUME : K_D3D9SURFACE);
    if (!s)
        return nullptr;
    s->d3d9_resource = tex->d3d9_resource;
    s->d3d9_parent = tex->id;
    s->d3d9_face = face;
    s->d3d9_level = level;
    s->d3d9_volume = volume;
    s->dev_d3d = tex->dev_d3d;
    if (ComObj *dev = com_get(tex->dev_d3d)) {
        com_retain_internal(dev);
        s->d3d9_owner_retained = true;
    }
    com_retain_internal(tex);
    s->d3d9_parent_retained = true;
    tex->d3d9_levels[slot] = s->id;
    if (kD3d9WgpuEnabled && s->dev_d3d) {
        if (ComObj *dev = com_get(s->dev_d3d)) {
            if (dev->d3d9_device) {
                recomp::d3d::Status status = dev->d3d9_device->register_resource_view(
                    s->id, *s->d3d9_resource, face, level, volume);
                if (!d3d_bridge_ok(status)) {
                    tex->d3d9_levels[slot] = 0;
                    com_release(s);
                    return nullptr;
                }
            }
        }
    }
    return s;
}

static void create_texture_wgpu(X86 *c, recomp::d3d::D3d9TextureKind kind) {
    ComObj *dev = this_device9(c);
    recomp::d3d::D3d9TextureRequest request{};
    request.kind = kind;
    uint32_t out = 0;
    ComIface iface = IF_D3DTEXTURE9;
    ComKind com_kind = K_D3D9TEXTURE;
    switch (kind) {
    case recomp::d3d::D3d9TextureKind::Texture2D:
        request.width = arg(c, 1);
        request.height = arg(c, 2);
        request.levels = arg(c, 3);
        request.usage = arg(c, 4);
        request.format = arg(c, 5);
        request.pool = arg(c, 6);
        out = arg(c, 7);
        request.shared_handle = arg(c, 8) != 0;
        break;
    case recomp::d3d::D3d9TextureKind::Cube:
        request.width = request.height = arg(c, 1);
        request.levels = arg(c, 2);
        request.usage = arg(c, 3);
        request.format = arg(c, 4);
        request.pool = arg(c, 5);
        out = arg(c, 6);
        request.shared_handle = arg(c, 7) != 0;
        iface = IF_D3DCUBETEXTURE9;
        com_kind = K_D3D9CUBETEXTURE;
        break;
    case recomp::d3d::D3d9TextureKind::Volume:
        request.width = arg(c, 1);
        request.height = arg(c, 2);
        request.depth = arg(c, 3);
        request.levels = arg(c, 4);
        request.usage = arg(c, 5);
        request.format = arg(c, 6);
        request.pool = arg(c, 7);
        out = arg(c, 8);
        request.shared_handle = arg(c, 9) != 0;
        iface = IF_D3DVOLUMETEXTURE9;
        com_kind = K_D3D9VOLUMETEXTURE;
        break;
    }
    request.depth = request.depth ? request.depth : 1;
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_out_ptr(out, 0);
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    auto resource = std::make_shared<D3d9HostResource>();
    recomp::d3d::D3d9TextureCreateResult result =
        resource->create_texture(*dev->d3d9_device, request);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    ComObj *tex = com_new(com_kind);
    if (!tex) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    tex->d3d9_resource = std::move(resource);
    tex->dev_d3d = dev->id;
    com_retain_internal(dev);
    tex->d3d9_owner_retained = true;
    d3d9_register_wgpu_resource(dev, tex);
    uint32_t view = com_view(tex, iface);
    if (!view) {
        com_release(tex);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

static void create_surface_wgpu(X86 *c, uint32_t out_index, uint32_t usage) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, out_index);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_out_ptr(out, 0);
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9SurfaceRequest request{};
    request.width = arg(c, 1);
    request.height = arg(c, 2);
    request.format = arg(c, 3);
    if (out_index == 5) {
        request.pool = arg(c, 4);
        request.usage = 0;
        request.shared_handle = arg(c, 6) != 0;
    } else {
        request.pool = 0;
        request.usage = usage;
        request.multisample_type = arg(c, 4);
        request.multisample_quality = arg(c, 5);
        request.lockable = usage == USAGE_RENDERTARGET && arg(c, 6) != 0;
        request.discard = usage == USAGE_DEPTHSTENCIL && arg(c, 6) != 0;
        request.shared_handle = arg(c, 8) != 0;
    }
    auto resource = std::make_shared<D3d9HostResource>();
    recomp::d3d::Status status = resource->create_surface(*dev->d3d9_device, request);
    if (status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, status));
        return;
    }
    ComObj *s = com_new(K_D3D9SURFACE);
    if (!s) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    s->d3d9_resource = std::move(resource);
    s->dev_d3d = dev->id;
    com_retain_internal(dev);
    s->d3d9_owner_retained = true;
    d3d9_register_wgpu_resource(dev, s);
    uint32_t view = com_view(s, IF_D3DSURFACE9);
    if (!view) {
        com_release(s);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

static bool d3dx_srcinfo_ok(uint32_t addr) {
    return addr == 0 || gm_valid(addr, 28);
}

static bool d3dx_palette_ok(uint32_t addr) {
    return addr == 0 || gm_valid(addr, 1024);
}

static bool d3dx_read_file(const std::string &path, std::vector<uint8_t> &bytes) {
    if (path.empty())
        return false;
    int fd = os_fd_open(path.c_str(), OS_O_RDONLY);
    if (fd < 0)
        return false;
    OsStat st{};
    if (os_fd_stat(fd, &st) != 0 || !st.is_regular || !st.size || st.size > INT32_MAX) {
        os_fd_close(fd);
        return false;
    }
    bytes.resize((size_t)st.size);
    size_t got = 0;
    while (got < bytes.size()) {
        int64_t n = os_fd_read(fd, bytes.data() + got, bytes.size() - got);
        if (n <= 0)
            break;
        got += (size_t)n;
    }
    os_fd_close(fd);
    return got == bytes.size();
}

static bool d3dx_write_file(const std::string &path, const uint8_t *data, size_t size) {
    if (path.empty())
        return false;
    int fd = os_fd_open(path.c_str(), OS_O_WRONLY | OS_O_CREAT | OS_O_TRUNC);
    if (fd < 0)
        return false;
    size_t put = 0;
    while (put < size) {
        int64_t n = os_fd_write(fd, data + put, size - put);
        if (n <= 0)
            break;
        put += (size_t)n;
    }
    os_fd_close(fd);
    return put == size;
}

static void d3dx_write_info(uint32_t addr, const recomp::d3d::D3dxImageInfo &info) {
    if (!addr || !gm_valid(addr, 28))
        return;
    wr32(addr + 0, info.width);
    wr32(addr + 4, info.height);
    wr32(addr + 8, info.depth);
    wr32(addr + 12, info.mip_levels);
    wr32(addr + 16, info.format);
    wr32(addr + 20, info.resource_type);
    wr32(addr + 24, info.image_file_format);
}

static ComKind d3dx_com_kind(recomp::d3d::D3d9TextureKind kind) {
    switch (kind) {
    case recomp::d3d::D3d9TextureKind::Cube:
        return K_D3D9CUBETEXTURE;
    case recomp::d3d::D3d9TextureKind::Volume:
        return K_D3D9VOLUMETEXTURE;
    default:
        return K_D3D9TEXTURE;
    }
}

static ComIface d3dx_iface(recomp::d3d::D3d9TextureKind kind) {
    switch (kind) {
    case recomp::d3d::D3d9TextureKind::Cube:
        return IF_D3DCUBETEXTURE9;
    case recomp::d3d::D3d9TextureKind::Volume:
        return IF_D3DVOLUMETEXTURE9;
    default:
        return IF_D3DTEXTURE9;
    }
}

static void d3dx_finish_create(X86 *c, ComObj *dev,
                               const std::shared_ptr<D3d9HostResource> &resource,
                               const recomp::d3d::D3dxCreateResult &result,
                               recomp::d3d::D3d9TextureKind kind, uint32_t out_addr,
                               uint32_t srcinfo_addr) {
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    d3dx_write_info(srcinfo_addr, result.info);
    ComObj *tex = com_new(d3dx_com_kind(kind));
    if (!tex) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    tex->d3d9_resource = resource;
    tex->dev_d3d = dev->id;
    com_retain_internal(dev);
    tex->d3d9_owner_retained = true;
    d3d9_register_wgpu_resource(dev, tex);
    uint32_t view = com_view(tex, d3dx_iface(kind));
    if (!view) {
        com_release(tex);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out_addr, view);
    com_ret(c, D3D_OK9);
}

static void d3dx_create_from_file_ex(X86 *c, recomp::d3d::D3d9TextureKind kind) {
    ComObj *dev = this_device9(c);
    uint32_t file_addr = arg(c, 1);
    recomp::d3d::D3dxCreateRequest request{};
    request.kind = kind;
    uint32_t out_addr = 0, srcinfo_addr = 0, palette_addr = 0;
    switch (kind) {
    case recomp::d3d::D3d9TextureKind::Texture2D:
        request.width = arg(c, 2);
        request.height = arg(c, 3);
        request.levels = arg(c, 4);
        request.usage = arg(c, 5);
        request.format = arg(c, 6);
        request.pool = arg(c, 7);
        request.filter = arg(c, 8);
        request.mip_filter = arg(c, 9);
        request.color_key = arg(c, 10);
        srcinfo_addr = arg(c, 11);
        palette_addr = arg(c, 12);
        out_addr = arg(c, 13);
        request.depth = 1;
        break;
    case recomp::d3d::D3d9TextureKind::Cube:
        request.width = request.height = arg(c, 2);
        request.levels = arg(c, 3);
        request.usage = arg(c, 4);
        request.format = arg(c, 5);
        request.pool = arg(c, 6);
        request.filter = arg(c, 7);
        request.mip_filter = arg(c, 8);
        request.color_key = arg(c, 9);
        srcinfo_addr = arg(c, 10);
        palette_addr = arg(c, 11);
        out_addr = arg(c, 12);
        request.depth = 1;
        break;
    case recomp::d3d::D3d9TextureKind::Volume:
        request.width = arg(c, 2);
        request.height = arg(c, 3);
        request.depth = arg(c, 4);
        request.levels = arg(c, 5);
        request.usage = arg(c, 6);
        request.format = arg(c, 7);
        request.pool = arg(c, 8);
        request.filter = arg(c, 9);
        request.mip_filter = arg(c, 10);
        request.color_key = arg(c, 11);
        srcinfo_addr = arg(c, 12);
        palette_addr = arg(c, 13);
        out_addr = arg(c, 14);
        break;
    default:
        break;
    }
    if (!dev || !dev->d3d9_device || !out_addr || !gm_valid(out_addr, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!d3dx_srcinfo_ok(srcinfo_addr) || !d3dx_palette_ok(palette_addr)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (palette_addr) {
        imports_unsupported(c);
        return;
    }
    com_out_ptr(out_addr, 0);
    if (!file_addr || !gm_valid(file_addr, 1)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    std::string path = win32_host_path_op(gm_str(file_addr, 1024), WIN32_FILE_READ);
    std::vector<uint8_t> bytes;
    if (!d3dx_read_file(path, bytes)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    auto resource = std::make_shared<D3d9HostResource>();
    recomp::d3d::D3dxCreateResult result = resource->create_d3dx_texture(
        *dev->d3d9_device, request, rust::Slice<const uint8_t>(bytes.data(), bytes.size()));
    d3dx_finish_create(c, dev, resource, result, kind, out_addr, srcinfo_addr);
}

static void d3dx_create_from_memory(X86 *c, recomp::d3d::D3d9TextureKind kind) {
    ComObj *dev = this_device9(c);
    uint32_t data_addr = arg(c, 1), size = arg(c, 2), out_addr = arg(c, 3);
    if (!dev || !dev->d3d9_device || !out_addr || !gm_valid(out_addr, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_out_ptr(out_addr, 0);
    if (!data_addr || !size || !gm_valid(data_addr, size)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxCreateRequest request{};
    request.kind = kind;
    request.width = 0xFFFFFFFFu;
    request.height = 0xFFFFFFFFu;
    request.depth = 0xFFFFFFFFu;
    request.levels = 0xFFFFFFFFu;
    request.usage = 0;
    request.format = 0;
    request.pool = 1;
    request.filter = 0xFFFFFFFFu;
    request.mip_filter = 0xFFFFFFFFu;
    request.color_key = 0;
    auto resource = std::make_shared<D3d9HostResource>();
    recomp::d3d::D3dxCreateResult result = resource->create_d3dx_texture(
        *dev->d3d9_device, request,
        rust::Slice<const uint8_t>((const uint8_t *)gm_ptr(data_addr), size));
    d3dx_finish_create(c, dev, resource, result, kind, out_addr, 0);
}

void X_D3DXGetImageInfoFromFileInMemory(X86 *c) {
    uint32_t data_addr = arg(c, 0), size = arg(c, 1), info_addr = arg(c, 2);
    if (!data_addr || !size || !gm_valid(data_addr, size) || !d3dx_srcinfo_ok(info_addr)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxImageInfoResult result =
        d3dx_image_info(rust::Slice<const uint8_t>((const uint8_t *)gm_ptr(data_addr), size));
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    d3dx_write_info(info_addr, result.info);
    com_ret(c, D3D_OK9);
}

void X_D3DXCreateTextureFromFileExA(X86 *c) {
    d3dx_create_from_file_ex(c, recomp::d3d::D3d9TextureKind::Texture2D);
}
void X_D3DXCreateCubeTextureFromFileExA(X86 *c) {
    d3dx_create_from_file_ex(c, recomp::d3d::D3d9TextureKind::Cube);
}
void X_D3DXCreateVolumeTextureFromFileExA(X86 *c) {
    d3dx_create_from_file_ex(c, recomp::d3d::D3d9TextureKind::Volume);
}
void X_D3DXCreateTextureFromFileInMemory(X86 *c) {
    d3dx_create_from_memory(c, recomp::d3d::D3d9TextureKind::Texture2D);
}
void X_D3DXCreateCubeTextureFromFileInMemory(X86 *c) {
    d3dx_create_from_memory(c, recomp::d3d::D3d9TextureKind::Cube);
}
void X_D3DXCreateVolumeTextureFromFileInMemory(X86 *c) {
    d3dx_create_from_memory(c, recomp::d3d::D3d9TextureKind::Volume);
}

static bool d3dx_rect(uint32_t addr, recomp::d3d::D3d9Rect &rect, bool &present) {
    present = false;
    rect.left = rect.top = rect.right = rect.bottom = 0;
    if (!addr)
        return true;
    if (!gm_valid(addr, 16))
        return false;
    present = true;
    rect.left = (int32_t)rd32(addr + 0);
    rect.top = (int32_t)rd32(addr + 4);
    rect.right = (int32_t)rd32(addr + 8);
    rect.bottom = (int32_t)rd32(addr + 12);
    return true;
}

static ComObj *d3dx_device_of(ComObj *node) {
    if (!node)
        return nullptr;
    ComObj *dev = com_get(node->dev_d3d);
    if (!dev || !dev->d3d9_device)
        return nullptr;
    return dev;
}

void X_D3DXLoadSurfaceFromSurface(X86 *c) {
    ComObj *dst = com_this(arg(c, 0));
    ComObj *src = com_this(arg(c, 3));
    if (!dst || !src || dst->kind != K_D3D9SURFACE || src->kind != K_D3D9SURFACE) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!d3dx_palette_ok(arg(c, 1)) || !d3dx_palette_ok(arg(c, 4))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9Rect drect{}, srect{};
    bool dpresent = false, spresent = false;
    if (!d3dx_rect(arg(c, 2), drect, dpresent) || !d3dx_rect(arg(c, 5), srect, spresent)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t df = 0, dl = 0, sf = 0, sl = 0;
    bool dv = false, sv = false;
    ComObj *downer = level_owner(dst, &df, &dl, &dv);
    ComObj *sowner = level_owner(src, &sf, &sl, &sv);
    ComObj *dev = d3dx_device_of(downer);
    if (!downer || !sowner || !downer->d3d9_resource || !sowner->d3d9_resource || !dev) {
        imports_unsupported(c);
        return;
    }
    if (downer->dev_d3d != sowner->dev_d3d || dv || sv) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t filter = arg(c, 6), color_key = arg(c, 7);
    recomp::d3d::D3dxSurfaceReadResult read = dev->d3d9_device->d3dx_surface_read(
        *sowner->d3d9_resource, sf, sl, srect, spresent, color_key);
    if (read.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, read.status));
        return;
    }
    recomp::d3d::Status status = dev->d3d9_device->d3dx_surface_store(
        *downer->d3d9_resource, df, dl, drect, dpresent, *read.snapshot, filter);
    com_ret(c, bridge_result(c, status));
}

void X_D3DXSaveSurfaceToFileA(X86 *c) {
    uint32_t file_addr = arg(c, 0), format = arg(c, 1);
    ComObj *src = com_this(arg(c, 2));
    if (!src || src->kind != K_D3D9SURFACE) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!d3dx_palette_ok(arg(c, 3))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9Rect rect{};
    bool present = false;
    if (!d3dx_rect(arg(c, 4), rect, present)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t face = 0, level = 0;
    bool volume = false;
    ComObj *owner = level_owner(src, &face, &level, &volume);
    ComObj *dev = d3dx_device_of(owner);
    if (!owner || !owner->d3d9_resource || !dev || !file_addr || !gm_valid(file_addr, 1) ||
        volume) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxBytesResult result = dev->d3d9_device->d3dx_save_surface(
        *owner->d3d9_resource, face, level, rect, present, format);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    std::string path = win32_host_path_op(gm_str(file_addr, 1024), WIN32_FILE_WRITE);
    if (!d3dx_write_file(path, result.bytes.data(), result.bytes.size())) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_ret(c, D3D_OK9);
}

void X_D3DXSaveTextureToFileA(X86 *c) {
    uint32_t file_addr = arg(c, 0), format = arg(c, 1);
    ComObj *src = com_this(arg(c, 2));
    if (!src || (src->kind != K_D3D9TEXTURE && src->kind != K_D3D9CUBETEXTURE &&
                 src->kind != K_D3D9VOLUMETEXTURE)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!d3dx_palette_ok(arg(c, 3))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t face = 0, level = 0;
    bool volume = false;
    ComObj *owner = level_owner(src, &face, &level, &volume);
    ComObj *dev = d3dx_device_of(owner);
    if (!owner || !owner->d3d9_resource || !dev || !file_addr || !gm_valid(file_addr, 1)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxBytesResult result =
        dev->d3d9_device->d3dx_save_texture(*owner->d3d9_resource, format);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    std::string path = win32_host_path_op(gm_str(file_addr, 1024), WIN32_FILE_WRITE);
    if (!d3dx_write_file(path, result.bytes.data(), result.bytes.size())) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_ret(c, D3D_OK9);
}

static float d3dx_rf(uint32_t addr) {
    uint32_t bits = rd32(addr);
    float value;
    memcpy(&value, &bits, 4);
    return value;
}
static void d3dx_wf(uint32_t addr, float value) {
    uint32_t bits;
    memcpy(&bits, &value, 4);
    wr32(addr, bits);
}

static void d3d9_stage_shader(uint32_t addr, std::vector<uint32_t> &out);

static bool d3dx_stage_shader(uint32_t addr, std::vector<uint32_t> &out) {
    d3d9_stage_shader(addr, out);
    return !out.empty();
}

static ComObj *d3dx_ctab_this(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || o->kind != K_D3DXCONSTANTTABLE || !o->d3dx_ctab)
        return nullptr;
    return o;
}

static int d3dx_ctab_node_of(ComObj *o, uint32_t handle) {
    if (!o->d3dx_ctab_handles || handle < o->d3dx_ctab_handles)
        return -1;
    uint32_t offset = handle - o->d3dx_ctab_handles;
    if (offset % 4 || offset / 4 >= o->d3dx_ctab_nodes)
        return -1;
    return (int)(offset / 4);
}

static int d3dx_ctab_resolve(ComObj *o, uint32_t handle) {
    if (!handle)
        return -1;
    int node = d3dx_ctab_node_of(o, handle);
    if (node >= 0)
        return node;
    if (!gm_valid(handle, 1))
        return -1;
    std::string name = gm_str(handle, 16384);
    if (name.empty() || !gm_valid(handle, name.size() + 1) || rd8(handle + name.size()) != 0)
        return -1;
    return recomp::d3d::d3dx_constant_table_find(
        *o->d3dx_ctab->value, 0xffffffffu,
        rust::Slice<const uint8_t>((const uint8_t *)name.data(), name.size()));
}

static uint32_t d3dx_ctab_handle(ComObj *o, int node) {
    if (node < 0 || (uint32_t)node >= o->d3dx_ctab_nodes || !o->d3dx_ctab_handles)
        return 0;
    return o->d3dx_ctab_handles + (uint32_t)node * 4;
}

static bool d3dx_ctab_desc(ComObj *o, int node, recomp::d3d::D3dxConstantDesc &desc) {
    if (node < 0)
        return false;
    recomp::d3d::D3dxConstantDescResult result =
        recomp::d3d::d3dx_constant_table_desc(*o->d3dx_ctab->value, (uint32_t)node);
    if (result.status.code != recomp::d3d::StatusCode::Ok)
        return false;
    desc = result.desc;
    return true;
}

static void d3dx_ctab_apply_to(X86 *c, uint32_t device_addr, uint32_t handle, uint32_t setter,
                               const uint8_t *data, size_t size) {
    ComObj *o = d3dx_ctab_this(c);
    int node = o ? d3dx_ctab_resolve(o, handle) : -1;
    if (node < 0) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *dev = com_this(device_addr);
    if (!dev || dev->kind != K_D3D9DEVICE || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = recomp::d3d::d3dx_constant_table_apply(
        *dev->d3d9_device->value, *o->d3dx_ctab->value, (uint32_t)node, setter,
        rust::Slice<const uint8_t>(data, size));
    com_ret(c, status.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, status));
}

static bool d3dx_ctab_input_size(X86 *c, uint32_t handle, uint32_t setter, uint64_t requested,
                                 size_t &size) {
    ComObj *o = d3dx_ctab_this(c);
    int node = o ? d3dx_ctab_resolve(o, handle) : -1;
    if (node < 0) {
        com_ret(c, D3DERR_INVALIDCALL);
        return false;
    }
    size = (size_t)recomp::d3d::d3dx_constant_table_input_size(*o->d3dx_ctab->value, (uint32_t)node,
                                                               setter, requested);
    return true;
}

static void d3dx_ctab_apply_guest(X86 *c, uint32_t device_addr, uint32_t handle, uint32_t setter,
                                  uint32_t data_addr, uint32_t count, uint32_t element) {
    size_t size = 0;
    if (!d3dx_ctab_input_size(c, handle, setter, (uint64_t)count * element, size))
        return;
    if (!data_addr || !gm_fits(data_addr, size)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    const uint8_t *data = (const uint8_t *)gm_ptr(data_addr);
    d3dx_ctab_apply_to(c, device_addr, handle, setter, data, (size_t)size);
}

static void d3dx_ctab_pointer_array(X86 *c, uint32_t setter, uint32_t element) {
    uint32_t device_addr = arg(c, 1), handle = arg(c, 2), values_addr = arg(c, 3),
             count = arg(c, 4);
    size_t size = 0;
    if (!d3dx_ctab_input_size(c, handle, setter, (uint64_t)count * element, size))
        return;
    count = (uint32_t)(size / element);
    std::vector<uint8_t> gathered;
    if (!values_addr || !gm_fits_n(values_addr, count, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (count) {
        gathered.reserve((size_t)count * element);
        for (uint32_t i = 0; i < count; ++i) {
            uint32_t pointer = rd32(values_addr + i * 4);
            if (!pointer || !gm_valid(pointer, element)) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
            const uint8_t *bytes = (const uint8_t *)gm_ptr(pointer);
            gathered.insert(gathered.end(), bytes, bytes + element);
        }
    }
    d3dx_ctab_apply_to(c, device_addr, handle, setter, gathered.data(), gathered.size());
}

void D3DXBuffer_GetBufferPointer(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    set_eax(c, o ? o->d3dx_ctab_buffer : 0);
}

void D3DXBuffer_GetBufferSize(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    set_eax(c, o ? o->d3dx_ctab_size : 0);
}

void D3DXConstantTable_GetDesc(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    uint32_t desc = arg(c, 1);
    if (!o || !desc || !gm_valid(desc, 12)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxConstantTableDesc info =
        recomp::d3d::d3dx_constant_table_info(*o->d3dx_ctab->value);
    wr32(desc + 0, o->d3dx_ctab_buffer + info.creator_offset);
    wr32(desc + 4, info.version);
    wr32(desc + 8, info.constants);
    com_ret(c, D3D_OK9);
}

void D3DXConstantTable_GetConstantDesc(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    uint32_t handle = arg(c, 1), desc_addr = arg(c, 2), count_addr = arg(c, 3);
    int node = o ? d3dx_ctab_resolve(o, handle) : -1;
    recomp::d3d::D3dxConstantDesc desc;
    if (node < 0 || !d3dx_ctab_desc(o, node, desc)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if ((count_addr && !gm_valid(count_addr, 4)) || (desc_addr && !gm_valid(desc_addr, 48))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (desc_addr) {
        wr32(desc_addr + 0, o->d3dx_ctab_buffer + desc.name_offset);
        wr32(desc_addr + 4, desc.register_set);
        wr32(desc_addr + 8, desc.register_index);
        wr32(desc_addr + 12, desc.register_count);
        wr32(desc_addr + 16, desc.class_);
        wr32(desc_addr + 20, desc.type_);
        wr32(desc_addr + 24, desc.rows);
        wr32(desc_addr + 28, desc.columns);
        wr32(desc_addr + 32, desc.elements);
        wr32(desc_addr + 36, desc.struct_members);
        wr32(desc_addr + 40, desc.bytes);
        wr32(desc_addr + 44,
             desc.default_offset == 0xffffffffu ? 0 : o->d3dx_ctab_buffer + desc.default_offset);
    }
    if (count_addr)
        wr32(count_addr, 1);
    com_ret(c, D3D_OK9);
}

void D3DXConstantTable_GetSamplerIndex(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    int node = o ? d3dx_ctab_resolve(o, arg(c, 1)) : -1;
    set_eax(c, node < 0 ? 0xffffffffu
                        : recomp::d3d::d3dx_constant_table_sampler(*o->d3dx_ctab->value,
                                                                   (uint32_t)node));
}

void D3DXConstantTable_GetConstant(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    uint32_t handle = arg(c, 1);
    int parent = o && handle ? d3dx_ctab_resolve(o, handle) : -1;
    if (!o || (handle && parent < 0)) {
        set_eax(c, 0);
        return;
    }
    int node = recomp::d3d::d3dx_constant_table_constant(
        *o->d3dx_ctab->value, handle ? (uint32_t)parent : 0xffffffffu, arg(c, 2));
    set_eax(c, d3dx_ctab_handle(o, node));
}

void D3DXConstantTable_GetConstantByName(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    uint32_t handle = arg(c, 1), name_addr = arg(c, 2);
    if (!o || !name_addr || !gm_valid(name_addr, 1)) {
        set_eax(c, 0);
        return;
    }
    int parent = handle ? d3dx_ctab_resolve(o, handle) : -1;
    if (handle && parent < 0) {
        set_eax(c, 0);
        return;
    }
    uint32_t parent_arg = handle ? (uint32_t)parent : 0xffffffffu;
    std::string name = gm_str(name_addr, 16384);
    if (!gm_valid(name_addr, name.size() + 1) || rd8(name_addr + name.size()) != 0) {
        set_eax(c, 0);
        return;
    }
    int node = recomp::d3d::d3dx_constant_table_find(
        *o->d3dx_ctab->value, parent_arg,
        rust::Slice<const uint8_t>((const uint8_t *)name.data(), name.size()));
    set_eax(c, d3dx_ctab_handle(o, node));
}

void D3DXConstantTable_GetConstantElement(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    int node = o ? d3dx_ctab_resolve(o, arg(c, 1)) : -1;
    int element = node < 0 ? -1
                           : recomp::d3d::d3dx_constant_table_element(*o->d3dx_ctab->value,
                                                                      (uint32_t)node, arg(c, 2));
    set_eax(c, o ? d3dx_ctab_handle(o, element) : 0);
}

void D3DXConstantTable_SetDefaults(X86 *c) {
    ComObj *o = d3dx_ctab_this(c);
    ComObj *dev = com_this(arg(c, 1));
    if (!o || !dev || dev->kind != K_D3D9DEVICE || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status =
        recomp::d3d::d3dx_constant_table_defaults(*dev->d3d9_device->value, *o->d3dx_ctab->value);
    com_ret(c, status.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, status));
}

void D3DXConstantTable_SetValue(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetValue, arg(c, 3), arg(c, 4), 1);
}

void D3DXConstantTable_SetBool(X86 *c) {
    uint32_t value = arg(c, 3);
    d3dx_ctab_apply_to(c, arg(c, 1), arg(c, 2), kD3dxSetBool, (const uint8_t *)&value, 4);
}

void D3DXConstantTable_SetBoolArray(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetBoolArray, arg(c, 3), arg(c, 4), 4);
}

void D3DXConstantTable_SetInt(X86 *c) {
    uint32_t value = arg(c, 3);
    d3dx_ctab_apply_to(c, arg(c, 1), arg(c, 2), kD3dxSetInt, (const uint8_t *)&value, 4);
}

void D3DXConstantTable_SetIntArray(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetIntArray, arg(c, 3), arg(c, 4), 4);
}

void D3DXConstantTable_SetFloat(X86 *c) {
    uint32_t value = arg(c, 3);
    d3dx_ctab_apply_to(c, arg(c, 1), arg(c, 2), kD3dxSetFloat, (const uint8_t *)&value, 4);
}

void D3DXConstantTable_SetFloatArray(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetFloatArray, arg(c, 3), arg(c, 4), 4);
}

void D3DXConstantTable_SetVector(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetVector, arg(c, 3), 1, 16);
}

void D3DXConstantTable_SetVectorArray(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetVectorArray, arg(c, 3), arg(c, 4), 16);
}

void D3DXConstantTable_SetMatrix(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetMatrix, arg(c, 3), 1, 64);
}

void D3DXConstantTable_SetMatrixArray(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetMatrixArray, arg(c, 3), arg(c, 4), 64);
}

void D3DXConstantTable_SetMatrixPointerArray(X86 *c) {
    d3dx_ctab_pointer_array(c, kD3dxSetMatrixPointerArray, 64);
}

void D3DXConstantTable_SetMatrixTranspose(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetMatrixTranspose, arg(c, 3), 1, 64);
}

void D3DXConstantTable_SetMatrixTransposeArray(X86 *c) {
    d3dx_ctab_apply_guest(c, arg(c, 1), arg(c, 2), kD3dxSetMatrixTransposeArray, arg(c, 3),
                          arg(c, 4), 64);
}

void D3DXConstantTable_SetMatrixTransposePointerArray(X86 *c) {
    d3dx_ctab_pointer_array(c, kD3dxSetMatrixTransposePointerArray, 64);
}

void d3dx_ctab_destroy(ComObj *o) {
    if (o->d3dx_ctab_buffer) {
        heap_free(o->d3dx_ctab_buffer);
        o->d3dx_ctab_buffer = 0;
    }
    if (o->d3dx_ctab_handles) {
        heap_free(o->d3dx_ctab_handles);
        o->d3dx_ctab_handles = 0;
    }
    o->d3dx_ctab.reset();
}

void X_D3DXGetShaderVersion(X86 *c) {
    uint32_t addr = arg(c, 0);
    uint32_t version = addr && gm_valid(addr, 4) ? rd32(addr) : 0;
    set_eax(c, recomp::d3d::d3dx_shader_version(rust::Slice<const uint32_t>(&version, 1)));
}

void X_D3DXGetShaderInputSemantics(X86 *c) {
    uint32_t code_addr = arg(c, 0), semantics_addr = arg(c, 1), count_addr = arg(c, 2);
    if (!code_addr || !gm_valid(code_addr, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    std::vector<uint32_t> code;
    if (!d3dx_stage_shader(code_addr, code)) {
        com_ret(c, MAKE_D3DHRESULT(2905));
        return;
    }
    recomp::d3d::D3dxSemanticsResult result = recomp::d3d::d3dx_shader_input_semantics(
        rust::Slice<const uint32_t>(code.data(), code.size()));
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    uint32_t count = (uint32_t)result.semantics.size();
    if ((count_addr && !gm_valid(count_addr, 4)) ||
        (semantics_addr && !gm_fits_n(semantics_addr, count, 8))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (count_addr)
        wr32(count_addr, count);
    if (semantics_addr && count) {
        for (uint32_t i = 0; i < count; ++i) {
            wr32(semantics_addr + i * 8, result.semantics[i].usage);
            wr32(semantics_addr + i * 8 + 4, result.semantics[i].usage_index);
        }
    }
    com_ret(c, D3D_OK9);
}

void X_D3DXGetShaderConstantTable(X86 *c) {
    uint32_t code_addr = arg(c, 0), out_addr = arg(c, 1);
    if (!out_addr || !gm_valid(out_addr, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    wr32(out_addr, 0);
    if (!code_addr || !gm_valid(code_addr, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    std::vector<uint32_t> code;
    if (!d3dx_stage_shader(code_addr, code)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    auto host = std::make_shared<D3dxConstantTableHost>();
    recomp::d3d::D3dxConstantTableResult result = recomp::d3d::d3dx_constant_table_create(
        *host->value, rust::Slice<const uint32_t>(code.data(), code.size()));
    if (result.invalid_bytecode) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    uint32_t buffer = 0;
    if (!result.buffer.empty()) {
        buffer = heap_alloc((uint32_t)result.buffer.size(), true, 16);
        if (!buffer) {
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        memcpy(gm_ptr(buffer), result.buffer.data(), result.buffer.size());
    }
    uint32_t handles = 0;
    if (result.node_count) {
        handles = heap_alloc(result.node_count * 4, true, 16);
        if (!handles) {
            heap_free(buffer);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
    }
    ComObj *o = com_new(K_D3DXCONSTANTTABLE);
    if (!o) {
        heap_free(buffer);
        heap_free(handles);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    o->d3dx_ctab = host;
    o->d3dx_ctab_buffer = buffer;
    o->d3dx_ctab_size = (uint32_t)result.buffer.size();
    o->d3dx_ctab_handles = handles;
    o->d3dx_ctab_nodes = result.node_count;
    uint32_t view = com_view(o, IF_D3DXCONSTANTTABLE);
    if (!view) {
        com_release(o);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out_addr, view);
    com_ret(c, D3D_OK9);
}

static std::map<std::string, uint32_t> &d3dx_profile_strings() {
    static std::map<std::string, uint32_t> cache;
    return cache;
}

void d3dx_shader_helpers_reset() {
    d3dx_profile_strings().clear();
}

static ComObj *efx_this(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || o->kind != K_D3DXEFFECT || !o->d3dx_effect)
        return nullptr;
    return o;
}

static D3dxEffectHost *efx_host(ComObj *o) {
    return o && o->d3dx_effect ? o->d3dx_effect.get() : nullptr;
}

static ComObj *efx_device(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx)
        return nullptr;
    ComObj *dev = com_get(fx->d3dx_effect->device_id);
    if (!dev || dev->kind != K_D3D9DEVICE || !dev->d3d9_device)
        return nullptr;
    return dev;
}

static void efx_status(X86 *c, recomp::d3d::Status result) {
    com_ret(c, result.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, result));
}

static uint32_t efx_guest_string(D3dxEffectHost *host, const std::vector<uint8_t> &bytes) {
    std::string key((const char *)bytes.data(), bytes.size());
    auto it = host->strings.find(key);
    if (it != host->strings.end())
        return it->second;
    uint32_t addr = heap_alloc((uint32_t)bytes.size() + 1, true, 16);
    if (!addr)
        return 0;
    if (!bytes.empty())
        memcpy(gm_ptr(addr), bytes.data(), bytes.size());
    ((uint8_t *)gm_ptr(addr))[bytes.size()] = 0;
    host->strings.emplace(std::move(key), addr);
    return addr;
}

static bool efx_texture_kind(ComKind kind) {
    return kind == K_D3D9TEXTURE || kind == K_D3D9CUBETEXTURE || kind == K_D3D9VOLUMETEXTURE;
}

static const uint32_t EFX_KNOWN_CREATION_FLAGS = 0x1fbffu;

static uint32_t efx_wrap_handle(ComObj *fx, uint32_t token) {
    if (!token)
        return 0;
    D3dxEffectHost *host = efx_host(fx);
    auto it = host->handle_tokens.find(token);
    if (it != host->handle_tokens.end())
        return it->second;
    uint32_t addr = heap_alloc(4, false, 16);
    if (!addr)
        return 0;
    wr32(addr, token);
    host->handle_tokens.emplace(token, addr);
    host->handle_reverse.emplace(addr, token);
    return addr;
}

static uint32_t efx_resolve_handle(ComObj *fx, uint32_t guest_handle, uint32_t kind) {
    if (!guest_handle)
        return 0;
    D3dxEffectHost *host = efx_host(fx);
    auto it = host->handle_reverse.find(guest_handle);
    if (it != host->handle_reverse.end())
        return it->second;
    std::string text = gm_str(guest_handle, 16384);
    if (text.empty())
        return 0;
    rust::Slice<const uint8_t> name((const uint8_t *)text.data(), text.size());
    if (kind <= 2) {
        uint32_t token = recomp::d3d::d3dx_effect_find(*host->value, 0, name, kind);
        if (token)
            return token;
    }
    for (uint32_t candidate : {0u, 1u, 2u}) {
        uint32_t token = recomp::d3d::d3dx_effect_find(*host->value, 0, name, candidate);
        if (token)
            return token;
    }
    return 0;
}

static void efx_free_handles(D3dxEffectHost *host) {
    for (auto &entry : host->handle_tokens)
        heap_free(entry.second);
    host->handle_tokens.clear();
    host->handle_reverse.clear();
}

static void efx_reconcile_refs(std::set<uint32_t> &current, const std::set<uint32_t> &desired) {
    for (uint32_t id : current)
        if (!desired.count(id))
            if (ComObj *o = com_get(id))
                com_release(o);
    for (uint32_t id : desired)
        if (!current.count(id))
            if (ComObj *o = com_get(id))
                com_addref(o);
    current = desired;
}

static void efx_refresh_textures(ComObj *fx) {
    D3dxEffectHost *host = efx_host(fx);
    if (!host)
        return;
    std::set<uint32_t> desired;
    for (uint32_t id : recomp::d3d::d3dx_effect_texture_refs(*host->value)) {
        if (!id)
            continue;
        ComObj *o = com_get(id);
        if (o && efx_texture_kind(o->kind) && o->dev_d3d == host->device_id)
            desired.insert(id);
    }
    efx_reconcile_refs(host->texture_refs, desired);
}

static void efx_refresh_pool_textures(ComObj *pool) {
    if (!pool || !pool->d3dx_effect_pool)
        return;
    std::set<uint32_t> desired;
    for (uint32_t id : recomp::d3d::d3dx_effect_pool_texture_refs(*pool->d3dx_effect_pool->value)) {
        if (!id)
            continue;
        ComObj *o = com_get(id);
        if (o && efx_texture_kind(o->kind))
            desired.insert(id);
    }
    efx_reconcile_refs(pool->d3dx_effect_pool->texture_refs, desired);
}

static void efx_refresh_saved(ComObj *dev, ComObj *fx) {
    D3dxEffectHost *host = efx_host(fx);
    if (!host || !dev || !dev->d3d9_device)
        return;
    recomp::d3d::D3d9BindingsResult result = recomp::d3d::d3dx_effect_saved_bindings(*host->value);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_reconcile_refs(host->saved_refs, {});
        return;
    }
    efx_reconcile_refs(host->saved_refs, d3d9_binding_set(dev, result.live));
}

static void efx_drop_saved(ComObj *fx) {
    D3dxEffectHost *host = efx_host(fx);
    if (host)
        efx_reconcile_refs(host->saved_refs, {});
}

static recomp::d3d::Status efx_publish_shaders(ComObj *dev, ComObj *fx) {
    D3dxEffectHost *host = efx_host(fx);
    recomp::d3d::D3dxEffectShadersResult result =
        recomp::d3d::d3dx_effect_prepare_shaders(*host->value, *dev->d3d9_device->value);
    if (result.status.code != recomp::d3d::StatusCode::Ok)
        return result.status;
    std::set<uint32_t> added;
    auto publish = [&](uint32_t handle, bool pixel) -> bool {
        if (!handle || dev->d3d9_device->shader_views.count(handle))
            return true;
        ComObj *o = com_new(pixel ? K_D3D9PS : K_D3D9VS);
        if (!o)
            return false;
        o->dev_d3d = dev->id;
        o->d3d9_state_handle = handle;
        com_internalize(o);
        dev->d3d9_device->shader_views[handle] = o->id;
        host->published_shaders.insert(o->id);
        added.insert(o->id);
        return true;
    };
    bool published = true;
    for (uint32_t handle : result.vertex)
        if (!publish(handle, false)) {
            published = false;
            break;
        }
    if (published)
        for (uint32_t handle : result.pixel)
            if (!publish(handle, true)) {
                published = false;
                break;
            }
    if (!published) {
        for (uint32_t handle : result.vertex)
            if (!dev->d3d9_device->shader_views.count(handle))
                dev->d3d9_device->destroy_shader(false, handle);
        for (uint32_t handle : result.pixel)
            if (!dev->d3d9_device->shader_views.count(handle))
                dev->d3d9_device->destroy_shader(true, handle);
        for (uint32_t id : added)
            if (ComObj *o = com_get(id)) {
                if (o->d3d9_state_handle)
                    dev->d3d9_device->shader_views.erase(o->d3d9_state_handle);
                host->published_shaders.erase(id);
                com_release_internal(o);
            }
        return recomp::d3d::Status{recomp::d3d::StatusCode::OutOfMemory,
                                   rust::String("ID3DXEffect: shader view allocation failed")};
    }
    return recomp::d3d::Status{recomp::d3d::StatusCode::Ok, rust::String()};
}

static const uint32_t EFX_MANAGER_ADDREF = 1 * 4;
static const uint32_t EFX_MANAGER_RELEASE = 2 * 4;
static const uint32_t EFX_MANAGER_SET_TRANSFORM = 3 * 4;
static const uint32_t EFX_MANAGER_SET_RENDER_STATE = 7 * 4;
static const uint32_t EFX_MANAGER_SET_TEXTURE = 8 * 4;
static const uint32_t EFX_MANAGER_SET_TEXTURE_STAGE_STATE = 9 * 4;
static const uint32_t EFX_MANAGER_SET_SAMPLER_STATE = 10 * 4;
static const uint32_t EFX_MANAGER_SET_VERTEX_SHADER = 13 * 4;
static const uint32_t EFX_MANAGER_SET_VERTEX_SHADER_CONSTANT_F = 14 * 4;
static const uint32_t EFX_MANAGER_SET_PIXEL_SHADER = 17 * 4;
static const uint32_t EFX_MANAGER_SET_PIXEL_SHADER_CONSTANT_F = 18 * 4;

static bool efx_manager_valid(uint32_t manager) {
    if (!manager || !gm_valid(manager, 4))
        return false;
    uint32_t vtbl = rd32(manager);
    return vtbl && gm_valid(vtbl, 21 * 4);
}

static uint32_t efx_manager_method(uint32_t manager, uint32_t offset, const char *name) {
    uint32_t vtbl = efx_manager_valid(manager) ? rd32(manager) : 0;
    if (!vtbl || !gm_valid(vtbl + offset, 4)) {
        LOGW("d3d9: ID3DXEffectStateManager::%s vtable slot is not readable", name);
        return 0;
    }
    uint32_t fn = rd32(vtbl + offset);
    if (!fn || !gm_valid(fn, 1)) {
        LOGW("d3d9: ID3DXEffectStateManager::%s target is not a readable guest address", name);
        return 0;
    }
    return fn;
}

void d3dx_effect_destroy(ComObj *o) {
    if (!o->d3dx_effect)
        return;
    D3dxEffectHost *host = o->d3dx_effect.get();
    for (auto &entry : host->strings)
        heap_free(entry.second);
    host->strings.clear();
    for (auto &entry : host->pass_shaders) {
        if (entry.second.first)
            heap_free(entry.second.first);
        if (entry.second.second)
            heap_free(entry.second.second);
    }
    host->pass_shaders.clear();
    if (host->dispatch_scratch)
        heap_free(host->dispatch_scratch);
    host->dispatch_scratch = 0;
    host->dispatch_scratch_size = 0;
    efx_free_handles(host);
    efx_reconcile_refs(host->texture_refs, {});
    efx_reconcile_refs(host->saved_refs, {});
    if (host->state_manager) {
        X86 *c = guest_current_context();
        if (!c) {
            LOGW("d3d9: ID3DXEffectStateManager::Release requires a guest context");
            std::abort();
        } else {
            uint32_t release =
                efx_manager_method(host->state_manager, EFX_MANAGER_RELEASE, "Release");
            if (!release) {
                LOGW("d3d9: ID3DXEffectStateManager::Release unavailable during effect destroy");
                std::abort();
            } else
                guest_call(c, release, host->state_manager);
        }
        host->state_manager = 0;
    }
    ComObj *pool = com_get(host->pool_obj);
    ComObj *dev = com_get(host->device_id);
    if (dev && dev->d3d9_device) {
        recomp::d3d::d3dx_effect_release(*host->value);
        recomp::d3d::d3dx_effect_shutdown(*host->value, *dev->d3d9_device->value);
    }
    for (uint32_t id : host->published_shaders)
        if (ComObj *shader = com_get(id))
            com_release_internal(shader);
    host->published_shaders.clear();
    if (pool && pool->d3dx_effect_pool)
        efx_refresh_pool_textures(pool);
    if (pool)
        com_release(pool);
    if (dev)
        com_release(dev);
    o->d3dx_effect.reset();
}

void d3dx_effect_pool_destroy(ComObj *o) {
    if (!o || !o->d3dx_effect_pool)
        return;
    efx_reconcile_refs(o->d3dx_effect_pool->texture_refs, {});
    o->d3dx_effect_pool.reset();
}

void X_D3DX33CreateEffectPool(X86 *c) {
    uint32_t out = arg(c, 0);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    auto host = std::make_shared<D3dxEffectPoolHost>();
    ComObj *o = com_new(K_D3DXEFFECTPOOL);
    if (!o) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    o->d3dx_effect_pool = host;
    uint32_t view = com_view(o, IF_D3DXEFFECTPOOL33);
    if (!view) {
        com_release(o);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void X_D3DXCreateEffectFromFileA(X86 *c) {
    uint32_t device_addr = arg(c, 0), file = arg(c, 1), flags = arg(c, 4);
    uint32_t pool_addr = arg(c, 5), out = arg(c, 6), errors = arg(c, 7);
    if (errors && gm_valid(errors, 4))
        wr32(errors, 0);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    ComObj *dev = com_this(device_addr);
    if (!dev || dev->kind != K_D3D9DEVICE || !dev->d3d9_device || !file) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *pool = pool_addr ? com_this(pool_addr) : nullptr;
    if (pool_addr && (!pool || pool->kind != K_D3DXEFFECTPOOL || !pool->d3dx_effect_pool)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (flags & ~EFX_KNOWN_CREATION_FLAGS) {
        com_ret(c, bridge_result(
                       c, recomp::d3d::Status{recomp::d3d::StatusCode::Unsupported,
                                              rust::String("D3DXCreateEffectFromFileA: "
                                                           "unknown effect creation flags")}));
        return;
    }
    std::string path = win32_host_path_op(gm_str(file, 1024), WIN32_FILE_READ);
    std::vector<uint8_t> bytes;
    if (!d3dx_read_file(path, bytes)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    auto host = std::make_shared<D3dxEffectHost>();
    host->device_id = dev->id;
    recomp::d3d::D3dxEffectCreateResult result = recomp::d3d::d3dx_effect_create(
        *host->value, *dev->d3d9_device->value,
        rust::Slice<const uint8_t>(bytes.data(), bytes.size()), flags);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    com_addref(dev);
    if (pool) {
        recomp::d3d::Status join = recomp::d3d::d3dx_effect_join_pool(
            *host->value, *pool->d3dx_effect_pool->value, dev->id);
        if (join.code != recomp::d3d::StatusCode::Ok) {
            recomp::d3d::d3dx_effect_release(*host->value);
            com_release(dev);
            com_ret(c, bridge_result(c, join));
            return;
        }
        com_addref(pool);
        host->pool_obj = pool->id;
    }
    ComObj *o = com_new(K_D3DXEFFECT);
    if (!o) {
        recomp::d3d::d3dx_effect_release(*host->value);
        if (pool)
            com_release(pool);
        com_release(dev);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    o->d3dx_effect = host;
    uint32_t view = com_view(o, IF_D3DXEFFECT33);
    if (!view) {
        com_release(o);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    efx_refresh_textures(o);
    if (pool)
        efx_refresh_pool_textures(pool);
    recomp::d3d::Status publish = efx_publish_shaders(dev, o);
    if (publish.code != recomp::d3d::StatusCode::Ok) {
        com_release(o);
        com_ret(c, bridge_result(c, publish));
        return;
    }
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void Efx_GetDesc(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t desc = arg(c, 1);
    if (!fx || !desc || !gm_valid(desc, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxEffectDescResult result = recomp::d3d::d3dx_effect_desc(*efx_host(fx)->value);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    wr32(desc + 0, 0);
    wr32(desc + 4, result.desc.parameters);
    wr32(desc + 8, result.desc.techniques);
    wr32(desc + 12, result.desc.functions);
    com_ret(c, D3D_OK9);
}

void Efx_GetParameterDesc(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t desc = arg(c, 2);
    if (!fx || !desc || !gm_valid(desc, 44)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxEffectParamDescResult result = recomp::d3d::d3dx_effect_param_desc(
        *efx_host(fx)->value, efx_resolve_handle(fx, arg(c, 1), 0));
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    std::vector<uint8_t> name(result.name.begin(), result.name.end());
    std::vector<uint8_t> semantic(result.semantic.begin(), result.semantic.end());
    uint32_t name_addr = name.empty() ? 0 : efx_guest_string(efx_host(fx), name);
    if (!name.empty() && !name_addr) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    uint32_t semantic_addr = semantic.empty() ? 0 : efx_guest_string(efx_host(fx), semantic);
    if (!semantic.empty() && !semantic_addr) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(desc + 0, name_addr);
    wr32(desc + 4, semantic_addr);
    wr32(desc + 8, result.desc.class_);
    wr32(desc + 12, result.desc.type_);
    wr32(desc + 16, result.desc.rows);
    wr32(desc + 20, result.desc.columns);
    wr32(desc + 24, result.desc.elements);
    wr32(desc + 28, result.desc.annotations);
    wr32(desc + 32, result.desc.struct_members);
    wr32(desc + 36, result.desc.flags);
    wr32(desc + 40, result.desc.bytes);
    com_ret(c, D3D_OK9);
}

void Efx_GetTechniqueDesc(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t desc = arg(c, 2);
    if (!fx || !desc || !gm_valid(desc, 12)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxEffectTechniqueDescResult result = recomp::d3d::d3dx_effect_technique_desc(
        *efx_host(fx)->value, efx_resolve_handle(fx, arg(c, 1), 1));
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    std::vector<uint8_t> name(result.name.begin(), result.name.end());
    uint32_t name_addr = name.empty() ? 0 : efx_guest_string(efx_host(fx), name);
    if (!name.empty() && !name_addr) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(desc + 0, name_addr);
    wr32(desc + 4, result.desc.passes);
    wr32(desc + 8, result.desc.annotations);
    com_ret(c, D3D_OK9);
}

void Efx_GetPassDesc(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t desc = arg(c, 2);
    if (!fx || !desc || !gm_valid(desc, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D3dxEffectHost *host = efx_host(fx);
    uint32_t pass = efx_resolve_handle(fx, arg(c, 1), 2);
    recomp::d3d::D3dxEffectPassDescResult result =
        recomp::d3d::d3dx_effect_pass_desc(*host->value, pass);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    std::vector<uint8_t> name(result.name.begin(), result.name.end());
    uint32_t name_addr = name.empty() ? 0 : efx_guest_string(host, name);
    if (!name.empty() && !name_addr) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(desc + 0, name_addr);
    wr32(desc + 4, result.desc.annotations);
    auto it = host->pass_shaders.find(pass);
    if (it == host->pass_shaders.end()) {
        auto copy_shader = [](const rust::Vec<uint8_t> &bytes) -> uint32_t {
            if (bytes.empty())
                return 0;
            uint32_t addr = heap_alloc((uint32_t)bytes.size(), false, 16);
            if (addr)
                memcpy(gm_ptr(addr), bytes.data(), bytes.size());
            return addr;
        };
        uint32_t vertex = copy_shader(result.desc.vertex_shader);
        uint32_t pixel = copy_shader(result.desc.pixel_shader);
        if ((!result.desc.vertex_shader.empty() && !vertex) ||
            (!result.desc.pixel_shader.empty() && !pixel)) {
            if (vertex)
                heap_free(vertex);
            if (pixel)
                heap_free(pixel);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        it = host->pass_shaders.emplace(pass, std::make_pair(vertex, pixel)).first;
    }
    wr32(desc + 8, it->second.first);
    wr32(desc + 12, it->second.second);
    com_ret(c, D3D_OK9);
}

void Efx_GetParameter(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        set_eax(c, 0);
        return;
    }
    uint32_t parent = efx_resolve_handle(fx, arg(c, 1), 0);
    set_eax(c, efx_wrap_handle(
                   fx, recomp::d3d::d3dx_effect_enum(*efx_host(fx)->value, parent, arg(c, 2), 0)));
}

void Efx_GetParameterElement(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        set_eax(c, 0);
        return;
    }
    uint32_t parent = efx_resolve_handle(fx, arg(c, 1), 0);
    set_eax(c, efx_wrap_handle(
                   fx, recomp::d3d::d3dx_effect_enum(*efx_host(fx)->value, parent, arg(c, 2), 4)));
}

void Efx_GetParameterByName(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t name = arg(c, 2);
    if (!fx || !name) {
        set_eax(c, 0);
        return;
    }
    uint32_t parent = efx_resolve_handle(fx, arg(c, 1), 0);
    std::string text = gm_str(name, 16384);
    set_eax(c,
            efx_wrap_handle(
                fx, recomp::d3d::d3dx_effect_find(
                        *efx_host(fx)->value, parent,
                        rust::Slice<const uint8_t>((const uint8_t *)text.data(), text.size()), 0)));
}

void Efx_GetParameterBySemantic(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t semantic = arg(c, 2);
    set_eax(c, 0);
    if (!fx || !semantic)
        return;
    std::string want = gm_str(semantic, 16384);
    D3dxEffectHost *host = efx_host(fx);
    uint32_t parent = efx_resolve_handle(fx, arg(c, 1), 0);
    for (uint32_t index = 0;; ++index) {
        uint32_t handle = recomp::d3d::d3dx_effect_enum(*host->value, parent, index, 0);
        if (!handle)
            break;
        recomp::d3d::D3dxBytesResult name =
            recomp::d3d::d3dx_effect_param_semantic(*host->value, handle);
        std::string text(name.bytes.begin(), name.bytes.end());
        if (text == want) {
            set_eax(c, efx_wrap_handle(fx, handle));
            return;
        }
    }
}

void Efx_GetTechnique(X86 *c) {
    ComObj *fx = efx_this(c);
    set_eax(c, fx ? efx_wrap_handle(
                        fx, recomp::d3d::d3dx_effect_enum(*efx_host(fx)->value, 0, arg(c, 1), 1))
                  : 0);
}

void Efx_GetTechniqueByName(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t name = arg(c, 1);
    if (!fx || !name) {
        set_eax(c, 0);
        return;
    }
    std::string text = gm_str(name, 16384);
    set_eax(c,
            efx_wrap_handle(
                fx, recomp::d3d::d3dx_effect_find(
                        *efx_host(fx)->value, 0,
                        rust::Slice<const uint8_t>((const uint8_t *)text.data(), text.size()), 1)));
}

void Efx_GetPass(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        set_eax(c, 0);
        return;
    }
    uint32_t technique = efx_resolve_handle(fx, arg(c, 1), 1);
    set_eax(c, efx_wrap_handle(fx, recomp::d3d::d3dx_effect_enum(*efx_host(fx)->value, technique,
                                                                 arg(c, 2), 2)));
}

void Efx_GetPassByName(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t name = arg(c, 2);
    if (!fx || !name) {
        set_eax(c, 0);
        return;
    }
    uint32_t technique = efx_resolve_handle(fx, arg(c, 1), 1);
    std::string text = gm_str(name, 16384);
    set_eax(c,
            efx_wrap_handle(
                fx, recomp::d3d::d3dx_effect_find(
                        *efx_host(fx)->value, technique,
                        rust::Slice<const uint8_t>((const uint8_t *)text.data(), text.size()), 2)));
}

void Efx_GetAnnotation(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        set_eax(c, 0);
        return;
    }
    uint32_t object = efx_resolve_handle(fx, arg(c, 1), 3);
    set_eax(c, efx_wrap_handle(
                   fx, recomp::d3d::d3dx_effect_enum(*efx_host(fx)->value, object, arg(c, 2), 3)));
}

void Efx_GetAnnotationByName(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t name = arg(c, 2);
    if (!fx || !name) {
        set_eax(c, 0);
        return;
    }
    uint32_t object = efx_resolve_handle(fx, arg(c, 1), 3);
    std::string text = gm_str(name, 16384);
    set_eax(c,
            efx_wrap_handle(
                fx, recomp::d3d::d3dx_effect_find(
                        *efx_host(fx)->value, object,
                        rust::Slice<const uint8_t>((const uint8_t *)text.data(), text.size()), 3)));
}

static void efx_apply(X86 *c, uint32_t setter, const uint8_t *data, size_t size) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t handle = efx_resolve_handle(fx, arg(c, 1), 0);
    efx_status(c, recomp::d3d::d3dx_effect_set(*efx_host(fx)->value, handle, setter,
                                               rust::Slice<const uint8_t>(data, size)));
}

static void efx_set_array_n(X86 *c, uint32_t setter, uint32_t element, uint32_t count) {
    uint32_t addr = arg(c, 2);
    if (!addr || !count || !gm_fits_n(addr, count, element)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    efx_apply(c, setter, (const uint8_t *)gm_ptr(addr), (size_t)count * element);
}

static void efx_set_array(X86 *c, uint32_t setter, uint32_t element) {
    efx_set_array_n(c, setter, element, arg(c, 3));
}

void Efx_SetValue(X86 *c) {
    uint32_t addr = arg(c, 2), size = arg(c, 3);
    if (size && (!addr || !gm_valid(addr, size))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    efx_apply(c, 0, size ? (const uint8_t *)gm_ptr(addr) : nullptr, size);
}

void Efx_SetBool(X86 *c) {
    uint32_t value = arg(c, 2) ? 1 : 0;
    efx_apply(c, 1, (const uint8_t *)&value, 4);
}
void Efx_SetBoolArray(X86 *c) {
    efx_set_array(c, 2, 4);
}
void Efx_SetInt(X86 *c) {
    uint32_t value = arg(c, 2);
    efx_apply(c, 3, (const uint8_t *)&value, 4);
}
void Efx_SetIntArray(X86 *c) {
    efx_set_array(c, 4, 4);
}
void Efx_SetFloat(X86 *c) {
    uint32_t value = arg(c, 2);
    efx_apply(c, 5, (const uint8_t *)&value, 4);
}
void Efx_SetFloatArray(X86 *c) {
    efx_set_array(c, 6, 4);
}
void Efx_SetVector(X86 *c) {
    efx_set_array_n(c, 7, 16, 1);
}
void Efx_SetVectorArray(X86 *c) {
    efx_set_array(c, 8, 16);
}
void Efx_SetMatrix(X86 *c) {
    efx_set_array_n(c, 9, 64, 1);
}
void Efx_SetMatrixArray(X86 *c) {
    efx_set_array(c, 10, 64);
}
void Efx_SetMatrixTranspose(X86 *c) {
    efx_set_array_n(c, 12, 64, 1);
}
void Efx_SetMatrixTransposeArray(X86 *c) {
    efx_set_array(c, 13, 64);
}

static void efx_set_pointer_array(X86 *c, uint32_t setter) {
    uint32_t addr = arg(c, 2), count = arg(c, 3);
    if (!addr || !count || !gm_fits_n(addr, count, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    std::vector<uint8_t> gathered;
    gathered.reserve((size_t)count * 64);
    for (uint32_t i = 0; i < count; ++i) {
        uint32_t pointer = rd32(addr + i * 4);
        if (!pointer || !gm_valid(pointer, 64)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        const uint8_t *bytes = (const uint8_t *)gm_ptr(pointer);
        gathered.insert(gathered.end(), bytes, bytes + 64);
    }
    efx_apply(c, setter, gathered.data(), gathered.size());
}

void Efx_SetMatrixPointerArray(X86 *c) {
    efx_set_pointer_array(c, 11);
}
void Efx_SetMatrixTransposePointerArray(X86 *c) {
    efx_set_pointer_array(c, 14);
}

static void efx_get(X86 *c, uint32_t kind, uint64_t limit) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t addr = arg(c, 2);
    uint32_t handle = efx_resolve_handle(fx, arg(c, 1), 0);
    recomp::d3d::D3dxBytesResult result =
        recomp::d3d::d3dx_effect_get(*efx_host(fx)->value, handle, kind);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    size_t bytes = std::min<size_t>(result.bytes.size(), (size_t)limit);
    if (bytes && (!addr || !gm_valid(addr, (uint32_t)bytes))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (bytes)
        memcpy(gm_ptr(addr), result.bytes.data(), bytes);
    com_ret(c, D3D_OK9);
}

static uint64_t efx_array_bytes(uint32_t count, uint32_t element) {
    return (uint64_t)count * element;
}

void Efx_GetValue(X86 *c) {
    efx_get(c, 3, arg(c, 3));
}
void Efx_GetBool(X86 *c) {
    efx_get(c, 2, 4);
}
void Efx_GetInt(X86 *c) {
    efx_get(c, 1, 4);
}
void Efx_GetFloat(X86 *c) {
    efx_get(c, 0, 4);
}
void Efx_GetBoolArray(X86 *c) {
    efx_get(c, 6, efx_array_bytes(arg(c, 3), 4));
}
void Efx_GetIntArray(X86 *c) {
    efx_get(c, 5, efx_array_bytes(arg(c, 3), 4));
}
void Efx_GetFloatArray(X86 *c) {
    efx_get(c, 4, efx_array_bytes(arg(c, 3), 4));
}
void Efx_GetVector(X86 *c) {
    efx_get(c, 7, 16);
}
void Efx_GetVectorArray(X86 *c) {
    efx_get(c, 8, efx_array_bytes(arg(c, 3), 16));
}
void Efx_GetMatrix(X86 *c) {
    efx_get(c, 9, 64);
}
void Efx_GetMatrixArray(X86 *c) {
    efx_get(c, 10, efx_array_bytes(arg(c, 3), 64));
}
void Efx_GetMatrixTranspose(X86 *c) {
    efx_get(c, 11, 64);
}
void Efx_GetMatrixTransposeArray(X86 *c) {
    efx_get(c, 12, efx_array_bytes(arg(c, 3), 64));
}

void Efx_GetString(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t out = arg(c, 2);
    if (!fx || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3dxBytesResult result = recomp::d3d::d3dx_effect_get(
        *efx_host(fx)->value, efx_resolve_handle(fx, arg(c, 1), 0), 13);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    std::vector<uint8_t> bytes(result.bytes.begin(), result.bytes.end());
    uint32_t addr = bytes.empty() ? 0 : efx_guest_string(efx_host(fx), bytes);
    if (!bytes.empty() && !addr) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, addr);
    com_ret(c, D3D_OK9);
}

void Efx_SetTexture(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t handle = efx_resolve_handle(fx, arg(c, 1), 0);
    uint32_t texture = arg(c, 2);
    ComObj *tex = texture ? com_this(texture) : nullptr;
    if (texture && !tex) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (tex && (!efx_texture_kind(tex->kind) || tex->dev_d3d != fx->d3dx_effect->device_id)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult previous =
        recomp::d3d::d3dx_effect_texture(*efx_host(fx)->value, handle);
    recomp::d3d::Status result =
        recomp::d3d::d3dx_effect_set_texture(*efx_host(fx)->value, handle, tex ? tex->id : 0);
    if (result.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result);
        return;
    }
    if (previous.status.code == recomp::d3d::StatusCode::Ok &&
        previous.value == (tex ? tex->id : 0)) {
        com_ret(c, D3D_OK9);
        return;
    }
    efx_refresh_textures(fx);
    if (ComObj *pool = com_get(efx_host(fx)->pool_obj))
        efx_refresh_pool_textures(pool);
    com_ret(c, D3D_OK9);
}

void Efx_GetTexture(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t out = arg(c, 2);
    if (!fx || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t handle = efx_resolve_handle(fx, arg(c, 1), 0);
    recomp::d3d::ValueResult result =
        recomp::d3d::d3dx_effect_texture(*efx_host(fx)->value, handle);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    ComObj *tex = com_get(result.value);
    if (!tex || !efx_texture_kind(tex->kind) || tex->dev_d3d != fx->d3dx_effect->device_id) {
        com_out_ptr(out, 0);
        com_ret(c, D3D_OK9);
        return;
    }
    uint32_t view = com_view(tex, IF_D3DBASETEXTURE9);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(tex);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void Efx_GetPool(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t out = arg(c, 1);
    if (!fx || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *pool = com_get(efx_host(fx)->pool_obj);
    if (!pool) {
        com_out_ptr(out, 0);
        com_ret(c, D3D_OK9);
        return;
    }
    uint32_t view = com_view(pool, IF_D3DXEFFECTPOOL33);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(pool);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void Efx_SetTechnique(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    efx_status(c, recomp::d3d::d3dx_effect_set_technique(*efx_host(fx)->value,
                                                         efx_resolve_handle(fx, arg(c, 1), 1)));
}

void Efx_GetCurrentTechnique(X86 *c) {
    ComObj *fx = efx_this(c);
    set_eax(c, fx ? efx_wrap_handle(
                        fx, recomp::d3d::d3dx_effect_current_technique(*efx_host(fx)->value))
                  : 0);
}

void Efx_ValidateTechnique(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    efx_status(c, recomp::d3d::d3dx_effect_validate_technique(
                      *efx_host(fx)->value, efx_resolve_handle(fx, arg(c, 1), 1)));
}

void Efx_FindNextValidTechnique(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t out = arg(c, 2);
    if (!fx || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t next = recomp::d3d::d3dx_effect_find_next_valid(*efx_host(fx)->value,
                                                             efx_resolve_handle(fx, arg(c, 1), 1));
    uint32_t wrapped = efx_wrap_handle(fx, next);
    if (next && !wrapped) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    wr32(out, wrapped);
    com_ret(c, next ? D3D_OK9 : 1u);
}

void Efx_IsParameterUsed(X86 *c) {
    ComObj *fx = efx_this(c);
    set_eax(c, fx ? recomp::d3d::d3dx_effect_is_parameter_used(*efx_host(fx)->value,
                                                               efx_resolve_handle(fx, arg(c, 1), 0),
                                                               efx_resolve_handle(fx, arg(c, 2), 1))
                  : 0);
}

static bool efx_hr_failed(uint32_t hr) {
    return (int32_t)hr < 0;
}

static uint32_t efx_dispatch_commands(X86 *c, ComObj *fx, ComObj *dev,
                                      const rust::Vec<recomp::d3d::D3dxEffectCommand> &commands,
                                      const rust::Vec<float> &values) {
    D3dxEffectHost *host = efx_host(fx);
    if (!host || !dev || !dev->d3d9_device)
        return D3DERR_INVALIDCALL;
    const size_t command_count = commands.size();
    if (command_count == 0)
        return D3D_OK9;
    uint32_t manager = host->state_manager;
    if (!efx_manager_valid(manager)) {
        LOGW("d3d9: ID3DXEffect needs a valid ID3DXEffectStateManager for %u commands",
             (unsigned)command_count);
        return D3DERR_INVALIDCALL;
    }
    const recomp::d3d::D3dxEffectCommand *cmd_list = commands.data();
    uint32_t resolved_inline[128];
    std::vector<uint32_t> resolved_heap;
    uint32_t *resolved = resolved_inline;
    if (command_count > std::size(resolved_inline)) {
        resolved_heap.resize(command_count);
        resolved = resolved_heap.data();
    }
    std::fill(resolved, resolved + command_count, 0u);
    uint32_t pinned_inline[32];
    std::vector<uint32_t> pinned_heap;
    size_t pinned_count = 0;
    auto pinned_at = [&](size_t i) {
        return i < std::size(pinned_inline) ? pinned_inline[i] : pinned_heap[i - std::size(pinned_inline)];
    };
    auto pin = [&](uint32_t id) {
        for (size_t i = 0; i < pinned_count; ++i)
            if (pinned_at(i) == id)
                return false;
        if (pinned_count < std::size(pinned_inline))
            pinned_inline[pinned_count] = id;
        else
            pinned_heap.push_back(id);
        ++pinned_count;
        return true;
    };
    bool stale = false;
    bool oom = false;
    for (size_t i = 0; i < command_count && !stale && !oom; ++i) {
        const recomp::d3d::D3dxEffectCommand &cmd = cmd_list[i];
        if (cmd.kind == recomp::d3d::D3dxEffectCommandKind::SetTexture) {
            if (!cmd.value)
                continue;
            ComObj *o = com_get(cmd.value);
            if (!o || !efx_texture_kind(o->kind) || o->dev_d3d != host->device_id) {
                LOGW("d3d9: ID3DXEffectStateManager::SetTexture stale texture %u", cmd.value);
                stale = true;
                break;
            }
            resolved[i] = com_view(o, IF_D3DBASETEXTURE9);
            if (!resolved[i]) {
                oom = true;
                break;
            }
            if (pin(cmd.value))
                com_addref(o);
        } else if (cmd.kind == recomp::d3d::D3dxEffectCommandKind::SetVertexShader ||
                   cmd.kind == recomp::d3d::D3dxEffectCommandKind::SetPixelShader) {
            if (!cmd.value)
                continue;
            bool vertex = cmd.kind == recomp::d3d::D3dxEffectCommandKind::SetVertexShader;
            auto it = dev->d3d9_device->shader_views.find(cmd.value);
            ComObj *o = it == dev->d3d9_device->shader_views.end() ? nullptr : com_get(it->second);
            if (!o || o->kind != (vertex ? K_D3D9VS : K_D3D9PS) || o->dev_d3d != dev->id) {
                LOGW("d3d9: ID3DXEffectStateManager::%s stale shader handle %u",
                     vertex ? "SetVertexShader" : "SetPixelShader", cmd.value);
                stale = true;
                break;
            }
            resolved[i] = com_view(o, vertex ? IF_D3DVERTEXSHADER9 : IF_D3DPIXELSHADER9);
            if (!resolved[i]) {
                oom = true;
                break;
            }
            if (pin(it->second))
                com_addref(o);
        }
    }

    auto unpin = [&]() {
        for (size_t i = 0; i < pinned_count; ++i)
            if (ComObj *o = com_get(pinned_at(i)))
                com_release(o);
    };

    if (stale || oom) {
        unpin();
        return oom ? E_OUTOFMEMORY : D3DERR_INVALIDCALL;
    }

    auto temporary = [&](uint32_t bytes) -> uint32_t {
        if (!bytes)
            return 0;
        if (bytes > host->dispatch_scratch_size) {
            if (host->dispatch_scratch)
                heap_free(host->dispatch_scratch);
            host->dispatch_scratch = heap_alloc(bytes, false, 16);
            host->dispatch_scratch_size = host->dispatch_scratch ? bytes : 0;
        }
        return host->dispatch_scratch;
    };

    struct ManagerMethod {
        uint32_t fn = 0;
        RecompHostCall host = nullptr;
        bool resolved = false;
    };
    ManagerMethod methods[EFX_MANAGER_SET_PIXEL_SHADER_CONSTANT_F / 4 + 1];
    auto method = [&](uint32_t offset, const char *name, uint32_t nargs) -> const ManagerMethod & {
        ManagerMethod &m = methods[offset / 4];
        if (!m.resolved) {
            m.fn = efx_manager_method(manager, offset, name);
            m.host = m.fn ? recomp_host_call(m.fn, nargs) : nullptr;
            m.resolved = true;
        }
        return m;
    };
    auto invoke = [&](uint32_t offset, const char *name,
                      std::initializer_list<uint32_t> args) -> uint32_t {
        const ManagerMethod &m = method(offset, name, (uint32_t)args.size());
        if (!m.fn) {
            stale = true;
            return D3D_OK9;
        }
        if (m.host)
            return m.host(c, args.begin());
        return guest_call(c, m.fn, args.begin(), (int)args.size());
    };
    const float *value_data = values.data();
    const size_t value_total = values.size();

    uint32_t failure = D3D_OK9;
    for (size_t i = 0; i < command_count && !oom; ++i) {
        const recomp::d3d::D3dxEffectCommand &cmd = cmd_list[i];
        uint32_t result = D3D_OK9;
        switch (cmd.kind) {
        case recomp::d3d::D3dxEffectCommandKind::SetRenderState:
            result = invoke(EFX_MANAGER_SET_RENDER_STATE, "SetRenderState",
                            {manager, cmd.state, cmd.value});
            break;
        case recomp::d3d::D3dxEffectCommandKind::SetTextureStageState:
            result = invoke(EFX_MANAGER_SET_TEXTURE_STAGE_STATE, "SetTextureStageState",
                            {manager, cmd.index, cmd.state, cmd.value});
            break;
        case recomp::d3d::D3dxEffectCommandKind::SetSamplerState:
            result = invoke(EFX_MANAGER_SET_SAMPLER_STATE, "SetSamplerState",
                            {manager, cmd.index, cmd.state, cmd.value});
            break;
        case recomp::d3d::D3dxEffectCommandKind::SetTransform: {
            if (cmd.count != 16 || (size_t)cmd.offset + 16 > value_total) {
                LOGW("d3d9: ID3DXEffectStateManager::SetTransform has an invalid matrix span");
                stale = true;
                break;
            }
            uint32_t buf = temporary(64);
            if (!buf) {
                oom = true;
                break;
            }
            memcpy(gm_ptr(buf), value_data + cmd.offset, 64);
            result = invoke(EFX_MANAGER_SET_TRANSFORM, "SetTransform", {manager, cmd.state, buf});
            break;
        }
        case recomp::d3d::D3dxEffectCommandKind::SetTexture:
            result = invoke(EFX_MANAGER_SET_TEXTURE, "SetTexture", {manager, cmd.index, resolved[i]});
            break;
        case recomp::d3d::D3dxEffectCommandKind::SetVertexShader:
            result = invoke(EFX_MANAGER_SET_VERTEX_SHADER, "SetVertexShader", {manager, resolved[i]});
            break;
        case recomp::d3d::D3dxEffectCommandKind::SetPixelShader:
            result = invoke(EFX_MANAGER_SET_PIXEL_SHADER, "SetPixelShader", {manager, resolved[i]});
            break;
        case recomp::d3d::D3dxEffectCommandKind::SetVertexShaderConstantF:
        case recomp::d3d::D3dxEffectCommandKind::SetPixelShaderConstantF: {
            bool vertex = cmd.kind == recomp::d3d::D3dxEffectCommandKind::SetVertexShaderConstantF;
            const char *name = vertex ? "SetVertexShaderConstantF" : "SetPixelShaderConstantF";
            if (cmd.count % 4 != 0 || (size_t)cmd.offset + cmd.count > value_total ||
                cmd.count > UINT32_MAX / 4) {
                LOGW("d3d9: ID3DXEffectStateManager::%s has an invalid constant span", name);
                stale = true;
                break;
            }
            uint32_t bytes = cmd.count * 4;
            uint32_t buf = temporary(bytes);
            if (bytes && !buf) {
                oom = true;
                break;
            }
            if (bytes)
                memcpy(gm_ptr(buf), value_data + cmd.offset, bytes);
            result = invoke(vertex ? EFX_MANAGER_SET_VERTEX_SHADER_CONSTANT_F
                                   : EFX_MANAGER_SET_PIXEL_SHADER_CONSTANT_F,
                            name, {manager, cmd.index, buf, cmd.count / 4});
            break;
        }
        default:
            LOGW("d3d9: ID3DXEffectStateManager command %d is unsupported", (int)cmd.kind);
            stale = true;
            break;
        }
        if (stale || oom)
            break;
        if (efx_hr_failed(result))
            failure = result;
    }

    unpin();
    if (oom)
        return E_OUTOFMEMORY;
    if (stale)
        return D3DERR_INVALIDCALL;
    return failure;
}

static uint32_t efx_run(X86 *c, ComObj *fx, ComObj *dev,
                        recomp::d3d::D3dxEffectRunResult &result) {
    D3dxEffectHost *host = efx_host(fx);
    uint32_t hr;
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        if (result.reserved)
            recomp::d3d::d3dx_effect_batch_finish(*host->value, false);
        efx_refresh_textures(fx);
        if (ComObj *pool = com_get(host->pool_obj))
            efx_refresh_pool_textures(pool);
        d3d9_refresh_bindings(dev);
        hr = bridge_result(c, result.status);
    } else {
        hr = efx_dispatch_commands(c, fx, dev, result.commands, result.values);
        if (result.reserved)
            recomp::d3d::d3dx_effect_batch_finish(*host->value, !efx_hr_failed(hr));
        else
            d3d9_refresh_bindings(dev);
    }
    recomp::d3d::d3dx_effect_recycle(*host->value, std::move(result.commands),
                                     std::move(result.values));
    return hr;
}

void Efx_Begin(X86 *c) {
    ComObj *fx = efx_this(c);
    ComObj *dev = efx_device(c);
    uint32_t passes = arg(c, 1);
    if (!fx || !dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (passes && !gm_valid(passes, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result =
        recomp::d3d::d3dx_effect_begin(*efx_host(fx)->value, *dev->d3d9_device->value, arg(c, 2));
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        efx_status(c, result.status);
        return;
    }
    if (passes)
        wr32(passes, result.value);
    D3dxEffectHost *host = efx_host(fx);
    host->restores_state = !(arg(c, 2) & 1) && !host->state_manager;
    efx_refresh_saved(dev, fx);
    com_ret(c, D3D_OK9);
}

void Efx_BeginPass(X86 *c) {
    ComObj *fx = efx_this(c);
    ComObj *dev = efx_device(c);
    if (!fx || !dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_addref(fx);
    com_addref(dev);
    recomp::d3d::D3dxEffectRunResult result = recomp::d3d::d3dx_effect_begin_pass(
        *efx_host(fx)->value, *dev->d3d9_device->value, arg(c, 1));
    uint32_t hr = efx_run(c, fx, dev, result);
    com_release(dev);
    com_release(fx);
    com_ret(c, hr);
}

void Efx_CommitChanges(X86 *c) {
    ComObj *fx = efx_this(c);
    ComObj *dev = efx_device(c);
    if (!fx || !dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_addref(fx);
    com_addref(dev);
    recomp::d3d::D3dxEffectRunResult result =
        recomp::d3d::d3dx_effect_commit(*efx_host(fx)->value, *dev->d3d9_device->value);
    uint32_t hr = efx_run(c, fx, dev, result);
    com_release(dev);
    com_release(fx);
    com_ret(c, hr);
}

void Efx_EndPass(X86 *c) {
    ComObj *fx = efx_this(c);
    if (!fx) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    efx_status(c, recomp::d3d::d3dx_effect_end_pass(*efx_host(fx)->value));
}

void Efx_End(X86 *c) {
    ComObj *fx = efx_this(c);
    ComObj *dev = efx_device(c);
    if (!fx || !dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D3dxEffectHost *host = efx_host(fx);
    recomp::d3d::Status result =
        recomp::d3d::d3dx_effect_end(*host->value, *dev->d3d9_device->value);
    if (host->restores_state)
        d3d9_refresh_bindings(dev);
    host->restores_state = false;
    if (result.code == recomp::d3d::StatusCode::Ok)
        efx_drop_saved(fx);
    efx_status(c, result);
}

void Efx_GetDevice(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t out = arg(c, 1);
    ComObj *dev = fx ? com_get(fx->d3dx_effect->device_id) : nullptr;
    if (!fx || !dev || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(dev, IF_D3DDEVICE9);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(dev);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void Efx_OnLostDevice(X86 *c) {
    ComObj *fx = efx_this(c);
    ComObj *dev = efx_device(c);
    if (!fx || !dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status result =
        recomp::d3d::d3dx_effect_on_lost(*efx_host(fx)->value, *dev->d3d9_device->value);
    efx_drop_saved(fx);
    efx_refresh_textures(fx);
    if (ComObj *pool = com_get(efx_host(fx)->pool_obj))
        efx_refresh_pool_textures(pool);
    d3d9_refresh_bindings(dev);
    efx_status(c, result);
}

void Efx_OnResetDevice(X86 *c) {
    ComObj *fx = efx_this(c);
    ComObj *dev = efx_device(c);
    if (!fx || !dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status result =
        recomp::d3d::d3dx_effect_on_reset(*efx_host(fx)->value, *dev->d3d9_device->value);
    efx_refresh_textures(fx);
    if (ComObj *pool = com_get(efx_host(fx)->pool_obj))
        efx_refresh_pool_textures(pool);
    efx_status(c, result);
}

void Efx_SetStateManager(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t manager = arg(c, 1);
    if (!fx) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D3dxEffectHost *host = efx_host(fx);
    if (recomp::d3d::d3dx_effect_batch_active(*host->value)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (manager && !efx_manager_valid(manager)) {
        LOGW("d3d9: ID3DXEffectStateManager manager %#x is not a valid interface", manager);
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t addref = manager ? efx_manager_method(manager, EFX_MANAGER_ADDREF, "AddRef") : 0;
    uint32_t manager_release =
        manager ? efx_manager_method(manager, EFX_MANAGER_RELEASE, "Release") : 0;
    uint32_t old = host->state_manager;
    uint32_t release = old ? efx_manager_method(old, EFX_MANAGER_RELEASE, "Release") : 0;
    if ((manager && (!addref || !manager_release)) || (old && !release)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status set =
        recomp::d3d::d3dx_effect_set_state_manager(*host->value, manager != 0);
    if (set.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, set));
        return;
    }
    com_addref(fx);
    if (manager)
        guest_call(c, addref, manager);
    host->state_manager = manager;
    if (old)
        guest_call(c, release, old);
    recomp::d3d::d3dx_effect_batch_finish(*host->value, true);
    if (manager)
        efx_drop_saved(fx);
    com_release(fx);
    com_ret(c, D3D_OK9);
}

void Efx_GetStateManager(X86 *c) {
    ComObj *fx = efx_this(c);
    uint32_t out = arg(c, 1);
    if (!fx || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D3dxEffectHost *host = efx_host(fx);
    uint32_t manager = host->state_manager;
    if (manager) {
        uint32_t addref = efx_manager_method(manager, EFX_MANAGER_ADDREF, "AddRef");
        if (!addref) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_addref(fx);
        guest_call(c, addref, manager);
        wr32(out, manager);
        com_release(fx);
    } else {
        wr32(out, 0);
    }
    com_ret(c, D3D_OK9);
}

static uint32_t d3dx_profile_string(const std::string &profile) {
    if (profile.empty())
        return 0;
    auto &cache = d3dx_profile_strings();
    auto it = cache.find(profile);
    if (it != cache.end())
        return it->second;
    uint32_t addr = heap_alloc((uint32_t)profile.size() + 1, true, 16);
    if (!addr)
        return 0;
    gm_put_str(addr, profile.c_str(), (uint32_t)profile.size() + 1);
    cache[profile] = addr;
    return addr;
}

static void d3dx_shader_profile(X86 *c, bool vertex) {
    ComObj *dev = com_this(arg(c, 0));
    if (!dev || dev->kind != K_D3D9DEVICE || !dev->d3d9_device) {
        set_eax(c, 0);
        return;
    }
    recomp::d3d::D3dxProfilesResult result =
        recomp::d3d::d3dx_shader_profiles(*dev->d3d9_device->value);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        set_eax(c, 0);
        return;
    }
    std::string profile = vertex ? std::string(result.vertex) : std::string(result.pixel);
    set_eax(c, d3dx_profile_string(profile));
}

void X_D3DXGetVertexShaderProfile(X86 *c) {
    d3dx_shader_profile(c, true);
}

void X_D3DXGetPixelShaderProfile(X86 *c) {
    d3dx_shader_profile(c, false);
}

static bool d3dx_matrix_read(uint32_t addr, float out[16]) {
    if (!addr || !gm_valid(addr, 64))
        return false;
    for (int i = 0; i < 16; ++i)
        out[i] = d3dx_rf(addr + (uint32_t)i * 4);
    return true;
}

static void d3dx_matrix_write(uint32_t addr, const rust::Vec<float> &values) {
    for (size_t i = 0; i < values.size() && i < 16; ++i)
        d3dx_wf(addr + (uint32_t)i * 4, values[i]);
}

void X_D3DX33MatrixMultiply(X86 *c) {
    uint32_t out = arg(c, 0), a = arg(c, 1), b = arg(c, 2);
    float ma[16], mb[16];
    if (!d3dx_matrix_read(a, ma) || !d3dx_matrix_read(b, mb) || (!out || !gm_valid(out, 64))) {
        set_eax(c, 0);
        return;
    }
    recomp::d3d::D3dxMatrixResult result = recomp::d3d::d3dx_matrix_multiply(
        rust::Slice<const float>(ma, 16), rust::Slice<const float>(mb, 16));
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        set_eax(c, 0);
        return;
    }
    d3dx_matrix_write(out, result.values);
    set_eax(c, out);
}

void X_D3DX33MatrixTranspose(X86 *c) {
    uint32_t out = arg(c, 0), in = arg(c, 1);
    float m[16];
    if (!d3dx_matrix_read(in, m) || (!out || !gm_valid(out, 64))) {
        set_eax(c, 0);
        return;
    }
    recomp::d3d::D3dxMatrixResult result =
        recomp::d3d::d3dx_matrix_transpose(rust::Slice<const float>(m, 16));
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        set_eax(c, 0);
        return;
    }
    d3dx_matrix_write(out, result.values);
    set_eax(c, out);
}

void X_D3DX33MatrixInverse(X86 *c) {
    uint32_t out = arg(c, 0), pdet = arg(c, 1), in = arg(c, 2);
    float m[16];
    if (!d3dx_matrix_read(in, m) || (!out || !gm_valid(out, 64)) || (pdet && !gm_valid(pdet, 4))) {
        set_eax(c, 0);
        return;
    }
    recomp::d3d::D3dxMatrixInverseResult result =
        recomp::d3d::d3dx_matrix_inverse(rust::Slice<const float>(m, 16));
    if (result.status.code != recomp::d3d::StatusCode::Ok || !result.valid) {
        set_eax(c, 0);
        return;
    }
    if (pdet)
        d3dx_wf(pdet, result.determinant);
    d3dx_matrix_write(out, result.values);
    set_eax(c, out);
}

static void level_lock_wgpu(X86 *c, ComObj *o, uint32_t face, uint32_t level, bool volume,
                            uint32_t rect_addr, uint32_t box_addr, uint32_t flags, uint32_t out,
                            bool box_output) {
    uint32_t owner_face = 0, owner_level = 0;
    bool owner_volume = false;
    ComObj *owner = level_owner(o, &owner_face, &owner_level, &owner_volume);
    if (owner && owner != o) {
        face = owner_face;
        level = owner_level;
        volume = owner_volume;
    }
    if (!owner || !owner->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    if (!out || !gm_valid(out, box_output ? 12 : 8) || (rect_addr && !gm_valid(rect_addr, 16)) ||
        (box_addr && !gm_valid(box_addr, 24))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9LevelLockRequest request{};
    request.face = face;
    request.level = level;
    request.volume = volume;
    request.full_region = volume ? box_addr == 0 : rect_addr == 0;
    request.flags = flags;
    if (box_addr) {
        request.volume_box.left = rd32(box_addr + 0);
        request.volume_box.top = rd32(box_addr + 4);
        request.volume_box.right = rd32(box_addr + 8);
        request.volume_box.bottom = rd32(box_addr + 12);
        request.volume_box.front = rd32(box_addr + 16);
        request.volume_box.back = rd32(box_addr + 20);
    }
    if (rect_addr) {
        request.rect.left = (int32_t)rd32(rect_addr + 0);
        request.rect.top = (int32_t)rd32(rect_addr + 4);
        request.rect.right = (int32_t)rd32(rect_addr + 8);
        request.rect.bottom = (int32_t)rd32(rect_addr + 12);
    }
    D3d9HostResource *h = owner->d3d9_resource.get();
    if (!d3d9_flush_resource_owner(c, owner))
        return;
    uint64_t key = ((uint64_t)face << 32) | level;
    D3d9Staging &staging = h->staging[key];
    bool fresh = staging.addr == 0;
    recomp::d3d::D3d9LevelLockResult prepared = h->level_lock_prepare(request);
    if (prepared.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, prepared.status));
        return;
    }
    if (fresh) {
        if (prepared.total == 0) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        uint32_t stage = heap_alloc(prepared.total, false, 16);
        if (!stage) {
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        rust::Slice<const uint8_t> bytes = h->level_bytes(face, level);
        if (bytes.size() != prepared.total) {
            heap_free(stage);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        memcpy(gm_ptr(stage), bytes.data(), prepared.total);
        staging.addr = stage;
        staging.size = prepared.total;
    }
    recomp::d3d::D3d9LevelLockResult committed = h->level_lock_commit(request);
    if (committed.status.code != recomp::d3d::StatusCode::Ok) {
        if (fresh) {
            heap_free(staging.addr);
            staging.addr = 0;
            staging.size = 0;
        }
        com_ret(c, bridge_result(c, committed.status));
        return;
    }
    if (box_output) {
        wr32(out + 0, prepared.pitch);
        wr32(out + 4, prepared.slice_pitch);
        wr32(out + 8, staging.addr + prepared.offset);
    } else {
        wr32(out + 0, prepared.pitch);
        wr32(out + 4, staging.addr + prepared.offset);
    }
    com_ret(c, D3D_OK9);
}

static void level_unlock_wgpu(X86 *c, ComObj *o, uint32_t face, uint32_t level) {
    uint32_t owner_face = 0, owner_level = 0;
    bool owner_volume = false;
    ComObj *owner = level_owner(o, &owner_face, &owner_level, &owner_volume);
    if (owner && owner != o) {
        face = owner_face;
        level = owner_level;
    }
    if (!owner || !owner->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    D3d9HostResource *h = owner->d3d9_resource.get();
    uint64_t key = ((uint64_t)face << 32) | level;
    auto it = h->staging.find(key);
    if (it == h->staging.end() || !it->second.addr) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    rust::Slice<const uint8_t> bytes(gm_ptr(it->second.addr), it->second.size);
    if (!d3d9_flush_resource_owner(c, owner))
        return;
    recomp::d3d::D3d9BufferUnlockResult result = h->level_unlock(face, level, bytes);
    if (result.closed) {
        heap_free(it->second.addr);
        h->staging.erase(it);
    }
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    com_ret(c, D3D_OK9);
}

// (this, Length, Usage, FVF, Pool, ppVertexBuffer, pSharedHandle)
void Dev_CreateVertexBuffer(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t bytes = arg(c, 1), out = arg(c, 5);
    if (kD3d9WgpuEnabled) {
        create_buffer_wgpu(c, recomp::d3d::D3d9BufferKind::Vertex);
        return;
    }
    if (!dev || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *vb = com_new(K_D3D9VB);
    if (!vb || !resource_alloc(vb, bytes)) {
        if (vb)
            com_release(vb);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    vb->dev_d3d = dev->id;
    vb->buf_bytes = bytes;
    uint32_t view_ = com_view(vb, IF_D3DVERTEXBUFFER9);
    if (!view_) {
        com_release(vb);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}

// (this, Length, Usage, Format, Pool, ppIndexBuffer, pSharedHandle)
void Dev_CreateIndexBuffer(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t bytes = arg(c, 1), out = arg(c, 5);
    if (kD3d9WgpuEnabled) {
        create_buffer_wgpu(c, recomp::d3d::D3d9BufferKind::Index);
        return;
    }
    if (!dev || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *ib = com_new(K_D3D9IB);
    if (!ib || !resource_alloc(ib, bytes)) {
        if (ib)
            com_release(ib);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    ib->dev_d3d = dev->id;
    ib->buf_bytes = bytes;
    ib->rmask = arg(c, 3) == D3DFMT_INDEX32 ? 4 : 2;
    uint32_t view_ = com_view(ib, IF_D3DINDEXBUFFER9);
    if (!view_) {
        com_release(ib);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}

// (this, Width, Height, Levels, Usage, Format, Pool, ppTexture, pSharedHandle)
void Dev_CreateTexture(X86 *c) {
    if (kD3d9WgpuEnabled) {
        create_texture_wgpu(c, recomp::d3d::D3d9TextureKind::Texture2D);
        return;
    }
    ComObj *dev = this_device9(c);
    uint32_t w = arg(c, 1), h = arg(c, 2), fmt = arg(c, 5), out = arg(c, 7);
    if (!dev || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *tex = com_new(K_D3D9TEXTURE);
    if (!tex) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    tex->dev_d3d = dev->id;
    tex->width = w;
    tex->height = h;
    tex->bpp = format_bytes(fmt) * 8;
    // Every level gets its own surface. Levels = 0 asks for the full chain;
    // a game uploads each level with its own LockRect.
    uint32_t levels = arg(c, 3);
    if (!levels) {
        levels = 1;
        for (uint32_t d = std::max(w, h); d > 1; d >>= 1)
            ++levels;
    }
    tex->rmask = fmt;
    uint32_t usage = arg(c, 4);
    resource_usage()[tex->id] = usage & (USAGE_RENDERTARGET | USAGE_DEPTHSTENCIL);
    for (uint32_t l = 0; l < levels && l < 16; ++l) {
        ComObj *s = make_surface(dev, std::max(w >> l, 1u), std::max(h >> l, 1u), fmt);
        if (!s)
            break;
        s->front_obj = tex->id;
        tex->surfaces.push_back(s->id);
    }
    tex->back_obj = tex->surfaces.empty() ? 0 : tex->surfaces[0];
    uint32_t view_ = com_view(tex, IF_D3DTEXTURE9);
    if (!view_) {
        com_release(tex);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}

// (this, EdgeLength, Levels, Usage, Format, Pool, ppCubeTexture, pSharedHandle)
void Dev_CreateCubeTexture(X86 *c) {
    if (kD3d9WgpuEnabled) {
        create_texture_wgpu(c, recomp::d3d::D3d9TextureKind::Cube);
        return;
    }
    ComObj *dev = this_device9(c);
    uint32_t edge = arg(c, 1), fmt = arg(c, 4), out = arg(c, 6);
    if (!dev || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *tex = com_new(K_D3D9TEXTURE);
    if (!tex) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    tex->dev_d3d = dev->id;
    tex->width = tex->height = edge;
    tex->bpp = format_bytes(fmt) * 8;
    tex->rmask = fmt;
    tex->caps = 1;                              // a cube map
    tex->pal_flags = arg(c, 2) ? arg(c, 2) : 1; // levels, reusing a field cubes do not use
    resource_usage()[tex->id] = arg(c, 3) & (USAGE_RENDERTARGET | USAGE_DEPTHSTENCIL);
    uint32_t view = com_view(tex, IF_D3DCUBETEXTURE9);
    if (!view) {
        com_release(tex);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    LOGV("d3d9: CreateCubeTexture %u -> %08x into %08x", edge, view, out);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void Dev_CreateVolumeTexture(X86 *c) {
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    create_texture_wgpu(c, recomp::d3d::D3d9TextureKind::Volume);
}

static void create_surface_method(X86 *c, uint32_t out_index, uint32_t fmt_index, uint32_t usage) {
    if (kD3d9WgpuEnabled) {
        create_surface_wgpu(c, out_index, usage);
        return;
    }
    ComObj *dev = this_device9(c);
    uint32_t w = arg(c, 1), h = arg(c, 2), fmt = arg(c, fmt_index), out = arg(c, out_index);
    if (!dev || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *s = make_surface(dev, w, h, fmt);
    if (!s) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    // CreateRenderTarget and CreateDepthStencilSurface: MultiSample, MultisampleQuality.
    if (out_index == 7)
        s->samples = multisample_count(arg(c, 4), arg(c, 5));
    resource_usage()[s->id] = usage;
    uint32_t view_ = com_view(s, IF_D3DSURFACE9);
    if (!view_) {
        com_release(s);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}
// (this, Width, Height, Format, MultiSample, MultisampleQuality, Discard,
//  ppSurface, pSharedHandle)
void Dev_CreateDepthStencilSurface(X86 *c) {
    create_surface_method(c, 7, 3, USAGE_DEPTHSTENCIL);
}
// (this, Width, Height, Format, MultiSample, MultisampleQuality, Lockable,
//  ppSurface, pSharedHandle)
void Dev_CreateRenderTarget(X86 *c) {
    create_surface_method(c, 7, 3, USAGE_RENDERTARGET);
}
// (this, Width, Height, Format, Pool, ppSurface, pSharedHandle)
void Dev_CreateOffscreenPlainSurface(X86 *c) {
    create_surface_method(c, 5, 3, 0);
}

// The device's own back buffer and depth buffer, made on first ask and kept.
// The back buffer lives in its own field. It used to share render_target,
// which SetRenderTarget overwrites, so after the game switched targets the
// "back buffer" was whatever it had last drawn into.
static ComObj *device_backbuffer(ComObj *dev) {
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device)
            return nullptr;
        ComObj *s = com_get(dev->d3d9_backbuffer);
        if (!s) {
            recomp::d3d::D3d9SurfaceDescriptorResult desc =
                dev->d3d9_device->backbuffer_descriptor();
            if (desc.status.code != recomp::d3d::StatusCode::Ok)
                return nullptr;
            s = com_new(K_D3D9SURFACE);
            if (!s)
                return nullptr;
            s->d3d9_implicit = true;
            s->d3d9_resource = std::make_shared<D3d9HostResource>();
            s->d3d9_target_depth = false;
            s->dev_d3d = dev->id;
            com_internalize(s);
            dev->d3d9_backbuffer = s->id;
            if (dev->d3d9_device)
                dev->d3d9_device->register_implicit_targets(dev->d3d9_backbuffer,
                                                            dev->d3d9_depthbuffer);
        }
        return s;
    }
    ComObj *s = com_get(dev->palette_obj);
    if (!s) {
        s = make_surface(dev, dev->width ? dev->width : 640, dev->height ? dev->height : 480, 22);
        dev->palette_obj = s ? s->id : 0;
        if (s)
            s->samples = dev->samples;
        if (s)
            resource_usage()[s->id] = USAGE_RENDERTARGET;
        if (s && !com_get(dev->render_target))
            dev->render_target = s->id;
    }
    return s;
}

// (this, pSourceSurface, pSourceRect, pDestSurface, pDestRect, Filter): a
// nearest-neighbour copy between 32-bit surfaces, rectangles honoured. This is
// how a game moves an off-screen target onto the back buffer.
void Dev_StretchRect(X86 *c) {
    ComObj *src = com_this(arg(c, 1)), *dst = com_this(arg(c, 3));
    if (!src || !dst || src->kind != K_D3D9SURFACE || dst->kind != K_D3D9SURFACE) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        ComObj *dev = this_device9(c);
        if (!dev || !dev->d3d9_device || src->dev_d3d != dev->id || dst->dev_d3d != dev->id) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9Rect sr{}, dr{};
        bool have_src = arg(c, 2) != 0;
        bool have_dst = arg(c, 4) != 0;
        if ((have_src && !read_guest_rect(arg(c, 2), &sr)) ||
            (have_dst && !read_guest_rect(arg(c, 4), &dr))) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        rust::Slice<const recomp::d3d::D3d9Rect> srs(have_src ? &sr : nullptr, have_src ? 1 : 0);
        rust::Slice<const recomp::d3d::D3d9Rect> drs(have_dst ? &dr : nullptr, have_dst ? 1 : 0);
        com_ret(c, bridge_result(c, dev->d3d9_device->stretch_rect(d3d9_target_id(src), srs,
                                                                   d3d9_target_id(dst), drs,
                                                                   arg(c, 5))));
        return;
    }
    if (gpu_on()) {
        uint32_t slot;
        ComObj *sowner = com_get(mirror_slot(src, &slot));
        ComObj *downer = com_get(mirror_slot(dst, &slot));
        if (sowner)
            gpu_sync(sowner);
        if (downer)
            gpu_sync(downer);
        int32_t sr[4] = {0, 0, (int32_t)src->width, (int32_t)src->height};
        int32_t dr[4] = {0, 0, (int32_t)dst->width, (int32_t)dst->height};
        if (arg(c, 2))
            for (int i = 0; i < 4; ++i)
                sr[i] = (int32_t)rd32(arg(c, 2) + 4u * (uint32_t)i);
        if (arg(c, 4))
            for (int i = 0; i < 4; ++i)
                dr[i] = (int32_t)rd32(arg(c, 4) + 4u * (uint32_t)i);
        host_d9_stretch(surface_ref(src), sr, surface_ref(dst), dr, arg(c, 5));
        gpu_drawn_into(dst);
        com_ret(c, D3D_OK9);
        return;
    }
    uint8_t *sbytes = resource_bytes(src), *dbytes = resource_bytes(dst);
    if (!sbytes || !dbytes || src->bpp != 32 || dst->bpp != 32) {
        com_ret(c, D3D_OK9);
        return;
    }
    auto rect = [](uint32_t p, ComObj *s, int32_t r[4]) {
        r[0] = 0;
        r[1] = 0;
        r[2] = (int32_t)s->width;
        r[3] = (int32_t)s->height;
        if (p)
            for (int i = 0; i < 4; ++i)
                r[i] = (int32_t)rd32(p + 4u * (uint32_t)i);
        r[0] = std::max(r[0], 0);
        r[1] = std::max(r[1], 0);
        r[2] = std::min(r[2], (int32_t)s->width);
        r[3] = std::min(r[3], (int32_t)s->height);
    };
    ComObj *dev_ = com_get(dst->dev_d3d);
    static uint32_t g_stretches = 0;
    if (++g_stretches <= 24)
        LOGW("d3d9: StretchRect %u (%ux%u, %u bpp) -> %u (%ux%u, %u bpp)%s", src->id, src->width,
             src->height, src->bpp, dst->id, dst->width, dst->height, dst->bpp,
             (dev_ && dst->id == dev_->palette_obj) ? ", onto the back buffer" : "");
    int32_t sr[4], dr[4];
    rect(arg(c, 2), src, sr);
    rect(arg(c, 4), dst, dr);
    int32_t sw = sr[2] - sr[0], sh = sr[3] - sr[1], dw = dr[2] - dr[0], dh = dr[3] - dr[1];
    if (sw <= 0 || sh <= 0 || dw <= 0 || dh <= 0) {
        com_ret(c, D3D_OK9);
        return;
    }
    for (int32_t y = 0; y < dh; ++y) {
        int32_t sy = sr[1] + y * sh / dh;
        uint8_t *drow = dbytes + (size_t)(dr[1] + y) * dst->pitch;
        const uint8_t *srow = sbytes + (size_t)sy * src->pitch;
        for (int32_t x = 0; x < dw; ++x)
            memcpy(drow + 4 * (dr[0] + x), srow + 4 * (sr[0] + x * sw / dw), 4);
    }
    d9_raster_invalidate(dst->id);
    com_ret(c, D3D_OK9);
}

void Dev_UpdateSurface(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *src = com_this(arg(c, 1)), *dst = com_this(arg(c, 3));
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !src || !dst || src->kind != K_D3D9SURFACE ||
            dst->kind != K_D3D9SURFACE || src->dev_d3d != dev->id || dst->dev_d3d != dev->id ||
            !src->d3d9_resource || !dst->d3d9_resource ||
            src->d3d9_resource.get() == dst->d3d9_resource.get()) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9Rect sr{};
        bool have_src = arg(c, 2) != 0;
        if (have_src && !read_guest_rect(arg(c, 2), &sr)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        int32_t dx = 0, dy = 0;
        if (arg(c, 4)) {
            if (!gm_valid(arg(c, 4), 8)) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
            dx = (int32_t)rd32(arg(c, 4));
            dy = (int32_t)rd32(arg(c, 4) + 4);
        }
        rust::Slice<const recomp::d3d::D3d9Rect> sr_slice(have_src ? &sr : nullptr,
                                                          have_src ? 1 : 0);
        com_ret(
            c, bridge_result(c, dev->d3d9_device->update_surface(
                                    *src->d3d9_resource, src->d3d9_face, src->d3d9_level, sr_slice,
                                    *dst->d3d9_resource, dst->d3d9_face, dst->d3d9_level, dx, dy)));
        return;
    }
    imports_unsupported(c);
    com_ret(c, D3DERR_INVALIDCALL);
}

void Dev_UpdateTexture(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *src = com_this(arg(c, 1)), *dst = com_this(arg(c, 2));
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !src || !dst || !src->d3d9_resource ||
            !dst->d3d9_resource || src->dev_d3d != dev->id || dst->dev_d3d != dev->id ||
            src->d3d9_resource.get() == dst->d3d9_resource.get()) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->update_texture(*src->d3d9_resource,
                                                                     *dst->d3d9_resource)));
        return;
    }
    imports_unsupported(c);
    com_ret(c, D3DERR_INVALIDCALL);
}

void Dev_GetRenderTargetData(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *src = com_this(arg(c, 1)), *dst = com_this(arg(c, 2));
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !src || !dst || src->kind != K_D3D9SURFACE ||
            dst->kind != K_D3D9SURFACE || !dst->d3d9_resource || src->dev_d3d != dev->id ||
            dst->dev_d3d != dev->id) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->get_render_target_data(
                                        d3d9_target_id(src), *dst->d3d9_resource, dst->d3d9_face,
                                        dst->d3d9_level)));
        return;
    }
    imports_unsupported(c);
    com_ret(c, D3DERR_INVALIDCALL);
}

void Dev_ColorFill(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *s = com_this(arg(c, 1));
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !s || s->kind != K_D3D9SURFACE || s->dev_d3d != dev->id) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9Rect r{};
        bool have = arg(c, 2) != 0;
        if (have && !read_guest_rect(arg(c, 2), &r)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        rust::Slice<const recomp::d3d::D3d9Rect> slice(have ? &r : nullptr, have ? 1 : 0);
        com_ret(
            c, bridge_result(c, dev->d3d9_device->color_fill(d3d9_target_id(s), slice, arg(c, 3))));
        return;
    }
    imports_unsupported(c);
    com_ret(c, D3DERR_INVALIDCALL);
}
static ComObj *device_depthbuffer(ComObj *dev) {
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device)
            return nullptr;
        ComObj *s = com_get(dev->d3d9_depthbuffer);
        if (!s) {
            recomp::d3d::D3d9SurfaceDescriptorResult desc =
                dev->d3d9_device->depthbuffer_descriptor();
            if (desc.status.code != recomp::d3d::StatusCode::Ok)
                return nullptr;
            s = com_new(K_D3D9SURFACE);
            if (!s)
                return nullptr;
            s->d3d9_implicit = true;
            s->d3d9_resource = std::make_shared<D3d9HostResource>();
            s->d3d9_target_depth = true;
            s->dev_d3d = dev->id;
            com_internalize(s);
            dev->d3d9_depthbuffer = s->id;
            if (dev->d3d9_device)
                dev->d3d9_device->register_implicit_targets(dev->d3d9_backbuffer,
                                                            dev->d3d9_depthbuffer);
        }
        return s;
    }
    ComObj *s = com_get(dev->zbuffer_obj);
    if (!s) {
        s = make_surface(dev, dev->width ? dev->width : 640, dev->height ? dev->height : 480, 75);
        dev->zbuffer_obj = s ? s->id : 0;
        if (s)
            s->samples = dev->samples;
        if (s)
            resource_usage()[s->id] = USAGE_DEPTHSTENCIL;
    }
    return s;
}

static bool implicit_surface_externally_referenced(uint32_t field) {
    ComObj *s = com_get(field);
    return s && s->refs > s->internal_refs;
}

static void device_discard_implicit_surfaces(ComObj *dev) {
    for (uint32_t *field : {&dev->d3d9_backbuffer, &dev->d3d9_depthbuffer}) {
        ComObj *s = com_get(*field);
        *field = 0;
        if (s)
            com_release_internal(s);
    }
    dev->render_target = 0;
    if (kD3d9WgpuEnabled && dev->d3d9_device)
        dev->d3d9_device->register_implicit_targets(0, 0);
}

// Drops the device's own back and depth buffers; the game's references to them
// keep the objects alive, and the device makes new ones on next use.
static void device_forget_buffers(ComObj *dev) {
    for (uint32_t *field : {&dev->palette_obj, &dev->zbuffer_obj}) {
        ComObj *s = com_get(*field);
        if (s)
            com_release(s);
        *field = 0;
    }
}

// (this, iSwapChain, iBackBuffer, Type, ppBackBuffer)
void Dev_GetBackBuffer(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 4);
    if (kD3d9WgpuEnabled &&
        (!dev || arg(c, 1) || arg(c, 2) || arg(c, 3) || !out || !gm_valid(out, 4))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *s = dev ? device_backbuffer(dev) : nullptr;
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_addref(s);
    uint32_t view_ = com_view(s, IF_D3DSURFACE9);
    if (!view_) {
        com_release(s);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}

void Dev_GetDepthStencilSurface(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_out_ptr(out, 0);
        recomp::d3d::ValueResult result = dev->d3d9_device->depth_stencil();
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        if (result.value == 0) {
            com_ret(c, D3DERR_NOTFOUND);
            return;
        }
        ComObj *s = result.value == 0xffffffffu ? device_depthbuffer(dev) : com_get(result.value);
        return_surface_handle(c, s, out);
        return;
    }
    ComObj *s = dev ? device_depthbuffer(dev) : nullptr;
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    return_surface_handle(c, s, out);
}

// (this, iSwapChain, ppRenderTarget). The surface bound as target 0, or the
// implicit back buffer when none was set; the returned handle is a new
// external reference, like GetBackBuffer's.
void Dev_GetRenderTarget(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || arg(c, 1) != 0 || !out || !gm_valid(out, 4)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_out_ptr(out, 0);
        recomp::d3d::ValueResult result = dev->d3d9_device->render_target();
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        ComObj *s = result.value == 0 ? device_backbuffer(dev) : com_get(result.value);
        return_surface_handle(c, s, out);
        return;
    }
    if (!dev || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_out_ptr(out, 0);
    ComObj *s = dev->render_target ? com_get(dev->render_target) : nullptr;
    if (!s)
        s = device_backbuffer(dev);
    return_surface_handle(c, s, out);
}

void Dev_SetRenderTarget(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *s = com_this(arg(c, 2));
    uint32_t index = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || (arg(c, 2) && (!s || s->kind != K_D3D9SURFACE)) ||
            (s && s->dev_d3d != dev->id) || (index == 0 && !s)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        uint32_t face = 0, level = 0;
        bool volume = false;
        uint32_t id = 0;
        if (s) {
            id = d3d9_target_id(s);
            face = s->d3d9_face;
            level = s->d3d9_level;
            volume = s->d3d9_volume;
        }
        recomp::d3d::Status status =
            dev->d3d9_device->set_render_target(index, id, face, level, volume);
        if (d3d_bridge_ok(status))
            d3d9_refresh_bindings(dev);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (!dev || index > 3) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (index == 0 && !(s && s->kind == K_D3D9SURFACE)) {
        com_ret(c, D3DERR_INVALIDCALL); // target 0 cannot be unset
        return;
    }
    D9Pipeline &pl = d9_pipeline(dev->id);
    pl.color_target[index] = (s && s->kind == K_D3D9SURFACE) ? s->id : 0;
    if (index == 0) {
        dev->render_target = s->id;
        // Setting render target 0 resets the viewport and scissor to the target.
        pl.viewport_set = false;
        pl.scissor[0] = pl.scissor[1] = 0;
        pl.scissor[2] = (int32_t)s->width;
        pl.scissor[3] = (int32_t)s->height;
    }
    com_ret(c, D3D_OK9);
}

void Dev_SetDepthStencilSurface(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *s = com_this(arg(c, 1));
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || (arg(c, 1) && (!s || s->kind != K_D3D9SURFACE)) ||
            (s && s->dev_d3d != dev->id)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        uint32_t face = 0, level = 0;
        bool volume = false;
        uint32_t id = 0;
        if (s) {
            if (s->d3d9_implicit && s->d3d9_target_depth) {
                id = 0xffffffffu;
            } else {
                id = s->id;
                face = s->d3d9_face;
                level = s->d3d9_level;
                volume = s->d3d9_volume;
            }
        }
        recomp::d3d::Status status = dev->d3d9_device->set_depth_stencil(id, face, level, volume);
        if (d3d_bridge_ok(status))
            d3d9_refresh_bindings(dev);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (dev)
        dev->zbuffer_obj = (s && s->kind == K_D3D9SURFACE) ? s->id : 0;
    com_ret(c, D3D_OK9);
}

// (this, pVertexElements, ppDecl): the element array is kept verbatim, since
// only the eventual renderer can interpret it.
void Dev_CreateVertexDeclaration(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t elems = arg(c, 1), out = arg(c, 2);
    if (!dev || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev->d3d9_device || !gm_valid(out, 4)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        std::vector<recomp::d3d::D3d9VertexElement> elements;
        for (uint32_t i = 0; i < 65 && elems; ++i) {
            uint32_t e = elems + i * 8u;
            if (!gm_valid(e, 8)) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
            recomp::d3d::D3d9VertexElement element{};
            element.stream = rd16(e);
            element.offset = rd16(e + 2);
            element.element_type = gm_ptr(e)[4];
            element.method = gm_ptr(e)[5];
            element.usage = gm_ptr(e)[6];
            element.usage_index = gm_ptr(e)[7];
            elements.push_back(element);
            if (element.stream == 0xff)
                break;
        }
        recomp::d3d::ValueResult result = dev->d3d9_device->create_vertex_declaration(
            rust::Slice<const recomp::d3d::D3d9VertexElement>(elements.data(), elements.size()));
        if (!d3d_bridge_ok(result.status)) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        ComObj *d = d3d9_make_object(dev, K_D3D9DECL, IF_D3DVERTEXDECL9, result.value, out);
        if (!d) {
            dev->d3d9_device->destroy_declaration(result.value);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        dev->d3d9_device->decl_views[result.value] = d->id;
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *d = com_new(K_D3D9DECL);
    if (!d) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    d->dev_d3d = dev->id;
    // D3DVERTEXELEMENT9 is 8 bytes and the array ends with D3DDECL_END, whose
    // Stream is 0xff. Copy until that, so the declaration survives the call.
    for (uint32_t i = 0; i < 65 && elems; ++i) {
        uint32_t e = elems + i * 8u;
        uint16_t stream = rd16(e);
        for (int b = 0; b < 8; ++b)
            d->blob.push_back(gm_ptr(e)[b]);
        if (stream == 0xff)
            break;
    }
    uint32_t view_ = com_view(d, IF_D3DVERTEXDECL9);
    if (!view_) {
        com_release(d);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}

void Dev_SetVertexDeclaration(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *d = com_this(arg(c, 1));
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || (arg(c, 1) && (!d || d->kind != K_D3D9DECL)) ||
            (d && d->dev_d3d != dev->id)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Status status =
            dev->d3d9_device->set_vertex_declaration(d ? d->d3d9_state_handle : 0);
        if (d3d_bridge_ok(status))
            d3d9_refresh_bindings(dev);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (dev) {
        dev->current_viewport = (d && d->kind == K_D3D9DECL) ? d->id : 0;
        d9_pipeline(dev->id).declaration = dev->current_viewport;
    }
    com_ret(c, D3D_OK9);
}

void Dev_CreateQuery(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t type = arg(c, 1), out = arg(c, 2);
    if (!dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    bool known = type == 4 || type == 5 || type == 6 || (type >= 8 && type <= 19);
    if (!known) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::ValueResult result = dev->d3d9_device->create_query(type);
        if (!out) {
            if (result.status.code == recomp::d3d::StatusCode::Ok && result.value) {
                dev->d3d9_device->destroy_query(result.value);
                com_ret(c, D3D_OK9);
            } else {
                com_ret(c, D3DERR_NOTAVAILABLE);
            }
            return;
        }
        if (!gm_valid(out, 4)) {
            if (result.status.code == recomp::d3d::StatusCode::Ok && result.value)
                dev->d3d9_device->destroy_query(result.value);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        ComObj *q = com_new(K_D3D9QUERY);
        if (!q) {
            dev->d3d9_device->destroy_query(result.value);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        q->dev_d3d = dev->id;
        q->d3d9_query = result.value;
        com_retain_internal(dev);
        q->d3d9_owner_retained = true;
        uint32_t view_ = com_view(q, IF_D3DQUERY9);
        if (!view_) {
            com_release(q);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        com_out_ptr(out, view_);
        com_ret(c, D3D_OK9);
        return;
    }
    bool creatable = type == D3DQUERYTYPE_OCCLUSION && gpu_on();
    if (!out) {
        com_ret(c, creatable ? D3D_OK9 : D3DERR_NOTAVAILABLE);
        return;
    }
    if (!creatable) {
        LOGW("d3d9: CreateQuery unsupported for query type %u", type);
        imports_unsupported(c);
    }
    ComObj *q = com_new(K_D3D9QUERY);
    if (!q) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    q->dev_d3d = dev ? dev->id : 0;
    q->dev_type = type;
    uint32_t view_ = com_view(q, IF_D3DQUERY9);
    if (!view_) {
        com_release(q);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}

// (this, StreamNumber, pStreamData, OffsetInBytes, Stride)
void Dev_SetStreamSource(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *vb = com_this(arg(c, 2));
    uint32_t stream = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || stream >= 16 ||
            (arg(c, 2) && (!vb || vb->kind != K_D3D9VB)) || (vb && vb->dev_d3d != dev->id)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Status status =
            dev->d3d9_device->set_stream_source(stream, vb ? vb->id : 0, arg(c, 3), arg(c, 4));
        if (d3d_bridge_ok(status))
            d3d9_bind_slot(dev, 16 + stream, vb ? vb->id : 0);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (!dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (stream >= 16 || (arg(c, 2) && (!vb || vb->kind != K_D3D9VB))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (stream >= 8) {
        LOGW("d3d9: SetStreamSource unsupported for stream %u", stream);
        imports_unsupported(c);
    }
    D9Pipeline::Stream &st = d9_pipeline(dev->id).stream[stream];
    st.vb = (vb && vb->kind == K_D3D9VB) ? vb->id : 0;
    st.offset = arg(c, 3);
    st.stride = arg(c, 4);
    if (stream == 0) {
        dev->back_obj = st.vb;
        dev->lock_off = arg(c, 3);
        dev->rate = arg(c, 4); // the stride, reusing a spare field
    }
    com_ret(c, D3D_OK9);
}

void Dev_SetIndices(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *ib = com_this(arg(c, 1));
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || (arg(c, 1) && (!ib || ib->kind != K_D3D9IB)) ||
            (ib && ib->dev_d3d != dev->id)) {
            log_once(("d3d9.SetIndices:" + std::to_string(arg(c, 1))).c_str(),
                     "d3d9: SetIndices rejected device=%08x id=%08x buffer=%08x id=%08x kind=%u "
                     "owner=%08x",
                     arg(c, 0), dev ? dev->id : 0, arg(c, 1), ib ? ib->id : 0,
                     ib ? static_cast<unsigned>(ib->kind) : 0, ib ? ib->dev_d3d : 0);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Status status = dev->d3d9_device->set_indices(ib ? ib->id : 0);
        if (d3d_bridge_ok(status))
            d3d9_bind_slot(dev, 32, ib ? ib->id : 0);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (dev) {
        dev->front_obj = (ib && ib->kind == K_D3D9IB) ? ib->id : 0;
        d9_pipeline(dev->id).index_buffer = dev->front_obj;
    }
    com_ret(c, D3D_OK9);
}

// (this, Stage, pTexture): the texture behind a sampler, for the renderer.
void Dev_SetTexture(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *tex = com_this(arg(c, 2));
    uint32_t stage = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (stage >= 16) {
            if (stage >= D3DDMAPSAMPLER && stage <= D3DVERTEXTEXTURESAMPLER3)
                imports_unsupported(c);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (arg(c, 2)) {
            if (!tex || tex->dev_d3d != dev->id ||
                (tex->kind != K_D3D9TEXTURE && tex->kind != K_D3D9CUBETEXTURE &&
                 tex->kind != K_D3D9VOLUMETEXTURE)) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
        }
        recomp::d3d::Status status = dev->d3d9_device->set_texture(stage, tex ? tex->id : 0);
        if (d3d_bridge_ok(status))
            d3d9_bind_slot(dev, stage, tex ? tex->id : 0);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (!dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (stage >= 16) {
        if (stage >= D3DDMAPSAMPLER && stage <= D3DVERTEXTEXTURESAMPLER3) {
            LOGW("d3d9: SetTexture unsupported for sampler %u", stage);
            imports_unsupported(c);
        }
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    dev->texture_handle = tex ? tex->id : 0;
    d9_pipeline(dev->id).sampler_tex[stage] = tex ? tex->id : 0;
    com_ret(c, D3D_OK9);
}

static uint32_t g_draws = 0;

static const char *prim_name(uint32_t type) {
    switch (type) {
    case 1:
        return "point list";
    case 2:
        return "line list";
    case 3:
        return "line strip";
    case 4:
        return "triangle list";
    case 5:
        return "triangle strip";
    case 6:
        return "triangle fan";
    default:
        return "unknown primitive";
    }
}

// The first few draws, then (with verbose logging) every 256th: a race makes
// thousands a frame, and writing even one in 256 cost a percent of the frame.
static bool draw_worth_reporting() {
    ++g_draws;
    return g_draws <= 8 || ((g_draws % 256) == 0 && log_level() >= 2);
}

static uint64_t primitive_vertices(uint32_t prim, uint32_t count) {
    uint64_t n = count;
    switch (prim) {
    case 1:
        return n;
    case 2:
        return n * 2;
    case 3:
        return n + 1;
    case 4:
        return n * 3;
    case 5:
    case 6:
        return n + 2;
    default:
        return 0;
    }
}

static uint32_t inline_bytes(uint64_t count, uint32_t stride) {
    if (!stride || count > UINT32_MAX / stride)
        return 0;
    return static_cast<uint32_t>(count) * stride;
}

// The render target and depth buffer a draw or clear goes to, mirrored.
static HostD9Target gpu_target(ComObj *dev) {
    HostD9Target t{};
    D9Pipeline &pl = d9_pipeline(dev->id);
    for (int i = 0; i < 4; ++i) {
        ComObj *s = com_get(pl.color_target[i]);
        if (i == 0 && !s)
            s = com_get(dev->render_target) ? com_get(dev->render_target) : device_backbuffer(dev);
        if (!s)
            continue;
        uint32_t slot;
        ComObj *owner = com_get(mirror_slot(s, &slot));
        if (owner)
            gpu_sync(owner);
        t.color[i] = surface_ref(s);
    }
    if (ComObj *z = com_get(dev->zbuffer_obj)) {
        uint32_t slot;
        ComObj *owner = com_get(mirror_slot(z, &slot));
        if (owner)
            gpu_sync(owner);
        t.depth = surface_ref(z);
    }
    return t;
}
static void gpu_target_drawn(ComObj *dev, bool color, bool depth) {
    D9Pipeline &pl = d9_pipeline(dev->id);
    if (color)
        for (int i = 0; i < 4; ++i) {
            ComObj *s = com_get(pl.color_target[i]);
            if (i == 0 && !s)
                s = com_get(dev->render_target);
            gpu_drawn_into(s);
        }
    if (depth)
        gpu_drawn_into(com_get(dev->zbuffer_obj));
}
static void target_viewport(ComObj *dev, const HostD9Target &t, int32_t vp[4]) {
    D9Pipeline &pl = d9_pipeline(dev->id);
    if (pl.viewport_set) {
        for (int i = 0; i < 4; ++i)
            vp[i] = (int32_t)pl.viewport[i];
        return;
    }
    ComObj *rt =
        com_get(pl.color_target[0]) ? com_get(pl.color_target[0]) : com_get(dev->render_target);
    (void)t;
    vp[0] = vp[1] = 0;
    vp[2] = rt ? (int32_t)rt->width : (int32_t)dev->width;
    vp[3] = rt ? (int32_t)rt->height : (int32_t)dev->height;
}

// One draw, handed to the GPU renderer with everything the device has bound.
static void gpu_draw(X86 *c, ComObj *dev, HostD9Draw &d) {
    D9Pipeline &pl = d9_pipeline(dev->id);
    if (pl.vs.empty() || pl.ps.empty()) {
        LOGW("d3d9: GPU draw unsupported: no vertex/pixel shader bound (primitive %u)",
             d.primitive);
        imports_unsupported(c);
    }
    d.target = gpu_target(dev);
    target_viewport(dev, d.target, d.viewport);
    d.depth_range[0] = pl.viewport_z[0];
    d.depth_range[1] = pl.viewport_z[1];
    for (int i = 0; i < 4; ++i)
        d.scissor[i] = pl.scissor[i];
    if (!pl.vs_key)
        pl.vs_key = d9sh::code_key(pl.vs.data(), pl.vs.size());
    if (!pl.ps_key)
        pl.ps_key = d9sh::code_key(pl.ps.data(), pl.ps.size());
    const d9sh::Program &vs = d9sh::program_for_key(pl.vs_key, pl.vs.data(), pl.vs.size());
    const d9sh::Program &ps = d9sh::program_for_key(pl.ps_key, pl.ps.data(), pl.ps.size());
    d.vs_key = pl.vs_key;
    d.ps_key = pl.ps_key;
    d.vs = pl.vs.data();
    d.vs_size = (uint32_t)pl.vs.size();
    d.ps = pl.ps.data();
    d.ps_size = (uint32_t)pl.ps.size();
    d.vconst = &pl.vconst[0][0];
    d.vconst_count = std::min<uint32_t>(vs.max_const, 256);
    d.pconst = &pl.pconst[0][0];
    d.pconst_count = std::min<uint32_t>(ps.max_const, 32);
    ComObj *decl = com_get(pl.declaration);
    if (!decl || decl->kind != K_D3D9DECL) {
        LOGW("d3d9: GPU draw unsupported: no vertex declaration bound (primitive %u)", d.primitive);
        imports_unsupported(c);
    }
    d.decl = decl->blob.data();
    d.decl_size = (uint32_t)decl->blob.size();
    d.decl_id = decl->id;
    for (int i = 0; i < 8; ++i) {
        if (d.stream[i].stride && !d.stream[i].buffer)
            continue; // the draw's own inline vertices
        ComObj *vb = com_get(pl.stream[i].vb);
        if (!vb)
            continue;
        gpu_sync_buffer(vb);
        d.stream[i].buffer = vb->id;
        d.stream[i].offset = pl.stream[i].offset;
        d.stream[i].stride = pl.stream[i].stride;
    }
    for (int s = 0; s < 16; ++s) {
        ComObj *tex = com_get(pl.sampler_tex[s]);
        if (!tex || (tex->kind != K_D3D9TEXTURE && tex->kind != K_D3D9SURFACE))
            continue;
        gpu_sync(tex);
        d.sampler_texture[s] = tex->id;
    }
    d.sampler_state = &pl.sampler_state[0][0];
    d.render_state = pl.rs;
    d.render_state_set = (const uint8_t *)pl.rs_set;
    d.state_version = pl.state_version;
    for (int st = 0; st < 8; ++st)
        if (pl.tss[st][24] & 0x100) // D3DTSS_TEXTURETRANSFORMFLAGS, D3DTTFF_PROJECTED
            d.projected_mask |= 1u << st;
    d.label = pl.label;
    host_d9_draw(&d);
    bool color_write = !pl.rs_set[168] || (pl.rs[168] & 0xf) != 0;
    bool z_write = (!pl.rs_set[14] || pl.rs[14]) && (!pl.rs_set[7] || pl.rs[7]);
    gpu_target_drawn(dev, color_write, z_write);
}

// Hands one draw to the CPU renderer with what the device has bound.
static void raster(X86 *c, const D9DrawCall &call) {
    ComObj *dev = this_device9(c);
    if (!dev)
        return;
    ComObj *target = com_get(dev->render_target);
    if (!target)
        target = device_backbuffer(dev);
    ComObj *decl = com_get(dev->current_viewport); // SetVertexDeclaration keeps it here
    static const std::vector<uint8_t> none;
    d9_raster_draw(dev, target, (decl && decl->kind == K_D3D9DECL) ? decl->blob : none, call);
}

// (this, PrimitiveType, StartVertex, PrimitiveCount)
void Dev_DrawPrimitive(X86 *c) {
    if (draw_worth_reporting())
        LOGW("d3d9: draw %u: %s, %u primitives from vertex %u, from the bound stream", g_draws,
             prim_name(arg(c, 1)), arg(c, 3), arg(c, 2));
    ComObj *dev = this_device9(c);
    uint32_t prim_count = arg(c, 3);
    if (prim_count == 0) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Status status =
            dev->d3d9_device->draw_primitive(arg(c, 1), arg(c, 2), prim_count);
        if (status.code == recomp::d3d::StatusCode::Ok)
            d3d9_refresh_bindings(dev);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (dev && gpu_on()) {
        bool have_stream = false;
        for (int i = 0; i < 8; ++i)
            if (d9_pipeline(dev->id).stream[i].vb)
                have_stream = true;
        if (!have_stream) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        HostD9Draw d{};
        d.primitive = arg(c, 1);
        d.primitive_count = prim_count;
        d.start = arg(c, 2);
        gpu_draw(c, dev, d);
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *vb = dev ? com_get(dev->back_obj) : nullptr;
    if (!vb || !resource_bytes(vb) || dev->lock_off >= vb->blob.size()) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D9DrawCall call;
    call.prim = arg(c, 1);
    call.prim_count = prim_count;
    call.vertex_data = resource_bytes(vb) + dev->lock_off;
    call.vertex_bytes = vb->blob.size() - dev->lock_off;
    call.stride = dev->rate;
    call.first = arg(c, 2);
    raster(c, call);
    com_ret(c, D3D_OK9);
}

// (this, Type, BaseVertexIndex, MinVertexIndex, NumVertices, startIndex, primCount)
void Dev_DrawIndexedPrimitive(X86 *c) {
    if (draw_worth_reporting())
        LOGW("d3d9: draw %u: %s, %u primitives, %u vertices from index %u, indexed", g_draws,
             prim_name(arg(c, 1)), arg(c, 6), arg(c, 4), arg(c, 5));
    ComObj *dev = this_device9(c);
    uint32_t prim_count = arg(c, 6);
    if (prim_count == 0) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Status status = dev->d3d9_device->draw_indexed_primitive(
            arg(c, 1), (int32_t)arg(c, 2), arg(c, 3), arg(c, 4), arg(c, 5), prim_count);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (dev && gpu_on()) {
        ComObj *ibo = com_get(d9_pipeline(dev->id).index_buffer);
        if (!ibo) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        gpu_sync_buffer(ibo);
        HostD9Draw d{};
        d.primitive = arg(c, 1);
        d.primitive_count = prim_count;
        d.start = arg(c, 5);
        d.base_vertex = (int32_t)arg(c, 2);
        d.index_buffer = ibo->id;
        d.index_size = ibo->rmask ? ibo->rmask : 2;
        gpu_draw(c, dev, d);
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *vb = dev ? com_get(dev->back_obj) : nullptr;
    ComObj *ib = dev ? com_get(dev->front_obj) : nullptr;
    if (!vb || !resource_bytes(vb) || !ib || !resource_bytes(ib) ||
        dev->lock_off >= vb->blob.size()) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D9DrawCall call;
    call.prim = arg(c, 1);
    call.prim_count = prim_count;
    call.vertex_data = resource_bytes(vb) + dev->lock_off;
    call.vertex_bytes = vb->blob.size() - dev->lock_off;
    call.stride = dev->rate;
    call.index_data = resource_bytes(ib);
    call.index_bytes = ib->blob.size();
    call.index_size = ib->rmask ? ib->rmask : 2;
    call.base_vertex = (int32_t)arg(c, 2);
    call.first = arg(c, 5);
    raster(c, call);
    com_ret(c, D3D_OK9);
}

// (this, PrimitiveType, PrimitiveCount, pVertexStreamZeroData, Stride): the
// vertices travel with the call rather than in a buffer, which is how this
// game draws.
// What a rasterizer will need from a draw, spelled out for the first few: the
// bound vertex declaration decoded, the raw vertices as floats, the bound
// texture, and the surface being drawn into.
static void describe_draw_inputs(X86 *c, uint32_t vertices, uint32_t stride, uint32_t nverts) {
    ComObj *dev = this_device9(c);
    if (!dev)
        return;
    ComObj *decl = com_get(dev->current_viewport); // SetVertexDeclaration keeps it here
    if (decl && decl->kind == K_D3D9DECL) {
        static const char *types[] = {"FLOAT1",    "FLOAT2",   "FLOAT3",   "FLOAT4",  "D3DCOLOR",
                                      "UBYTE4",    "SHORT2",   "SHORT4",   "UBYTE4N", "SHORT2N",
                                      "SHORT4N",   "USHORT2N", "USHORT4N", "UDEC3",   "DEC3N",
                                      "FLOAT16_2", "FLOAT16_4"};
        static const char *usages[] = {
            "POSITION", "BLENDWEIGHT", "BLENDINDICES", "NORMAL", "PSIZE", "TEXCOORD", "TANGENT",
            "BINORMAL", "TESSFACTOR",  "POSITIONT",    "COLOR",  "FOG",   "DEPTH",    "SAMPLE"};
        char line[512];
        size_t used = 0;
        for (size_t i = 0; i + 8 <= decl->blob.size(); i += 8) {
            uint16_t stream = (uint16_t)(decl->blob[i] | (decl->blob[i + 1] << 8));
            if (stream == 0xff)
                break;
            uint16_t off = (uint16_t)(decl->blob[i + 2] | (decl->blob[i + 3] << 8));
            uint8_t type = decl->blob[i + 4], usage = decl->blob[i + 6], index = decl->blob[i + 7];
            used += (size_t)snprintf(line + used, sizeof line - used, " %s%u@%u:%s",
                                     usage < 14 ? usages[usage] : "?", index, off,
                                     type < 17 ? types[type] : "?");
            if (used >= sizeof line)
                break;
        }
        LOGW("d3d9:   declaration %u:%s", decl->id, line);
    } else {
        LOGW("d3d9:   no vertex declaration bound (FVF path)");
    }
    for (uint32_t v = 0; v < nverts && v < 4; ++v) {
        uint32_t base = vertices + v * stride;
        char line[256]{};
        size_t used = 0;
        for (uint32_t o = 0; o + 4 <= stride && o < 40; o += 4)
            used += (size_t)snprintf(
                line + used, sizeof line - used, " %g", (double)[&] {
                    float f;
                    uint32_t bits = rd32(base + o);
                    memcpy(&f, &bits, 4);
                    return f;
                }());
        LOGW("d3d9:   vertex %u:%s", v, line);
    }
    ComObj *tex = com_get(dev->texture_handle);
    ComObj *rt = com_get(dev->render_target);
    LOGW("d3d9:   texture %u (%ux%u), target %u (%ux%u)%s", tex ? tex->id : 0, tex ? tex->width : 0,
         tex ? tex->height : 0, rt ? rt->id : 0, rt ? rt->width : 0, rt ? rt->height : 0,
         (rt && rt->id == dev->palette_obj) ? ", the back buffer" : "");
}

void Dev_DrawPrimitiveUP(X86 *c) {
    if (draw_worth_reporting()) {
        LOGW("d3d9: draw %u: %s, %u primitives, %u-byte vertices inline at %08x", g_draws,
             prim_name(arg(c, 1)), arg(c, 2), arg(c, 4), arg(c, 3));
    }
    ComObj *dev = this_device9(c);
    uint32_t prim_count = arg(c, 2);
    if (prim_count == 0) {
        com_ret(c, D3D_OK9);
        return;
    }
    uint32_t bytes = inline_bytes(primitive_vertices(arg(c, 1), prim_count), arg(c, 4));
    if (!dev || !bytes || !arg(c, 3) || !gm_valid(arg(c, 3), bytes)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!kD3d9WgpuEnabled && g_draws <= 8)
        describe_draw_inputs(c, arg(c, 3), arg(c, 4),
                             static_cast<uint32_t>(primitive_vertices(arg(c, 1), prim_count)));
    if (kD3d9WgpuEnabled) {
        if (!dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Status status = dev->d3d9_device->draw_primitive_up(
            arg(c, 1), prim_count, rust::Slice<const uint8_t>(gm_ptr(arg(c, 3)), bytes), arg(c, 4));
        d3d9_refresh_bindings(dev);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (gpu_on()) {
        HostD9Draw d{};
        d.primitive = arg(c, 1);
        d.primitive_count = prim_count;
        d.stream[0].stride = arg(c, 4);
        d.inline_vertices = gm_ptr(arg(c, 3));
        d.inline_bytes = bytes;
        gpu_draw(c, dev, d);
        com_ret(c, D3D_OK9);
        return;
    }
    D9DrawCall call;
    call.prim = arg(c, 1);
    call.prim_count = prim_count;
    call.vertices = arg(c, 3);
    call.stride = arg(c, 4);
    raster(c, call);
    com_ret(c, D3D_OK9);
}

// (this, PrimitiveType, MinVertexIndex, NumVertices, PrimitiveCount,
//  pIndexData, IndexDataFormat, pVertexStreamZeroData, Stride)
void Dev_DrawIndexedPrimitiveUP(X86 *c) {
    if (draw_worth_reporting())
        LOGW("d3d9: draw %u: %s, %u primitives, %u vertices of %u bytes inline at %08x, "
             "indices at %08x",
             g_draws, prim_name(arg(c, 1)), arg(c, 4), arg(c, 3), arg(c, 8), arg(c, 7), arg(c, 5));
    ComObj *dev = this_device9(c);
    uint32_t prim_count = arg(c, 4);
    if (prim_count == 0) {
        com_ret(c, D3D_OK9);
        return;
    }
    uint32_t isize = arg(c, 6) == D3DFMT_INDEX32 ? 4 : 2;
    uint32_t ibytes = inline_bytes(primitive_vertices(arg(c, 1), prim_count), isize);
    uint32_t vbytes = inline_bytes(static_cast<uint64_t>(arg(c, 2)) + arg(c, 3), arg(c, 8));
    if (!dev || (arg(c, 6) != D3DFMT_INDEX16 && arg(c, 6) != D3DFMT_INDEX32) || !ibytes ||
        !vbytes || !arg(c, 5) || !arg(c, 7) || !gm_valid(arg(c, 5), ibytes) ||
        !gm_valid(arg(c, 7), vbytes)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Status status = dev->d3d9_device->draw_indexed_primitive_up(
            arg(c, 1), arg(c, 2), arg(c, 3), prim_count,
            rust::Slice<const uint8_t>(gm_ptr(arg(c, 5)), ibytes), arg(c, 6),
            rust::Slice<const uint8_t>(gm_ptr(arg(c, 7)), vbytes), arg(c, 8));
        d3d9_refresh_bindings(dev);
        com_ret(c, bridge_result(c, status));
        return;
    }
    if (gpu_on()) {
        HostD9Draw d{};
        d.primitive = arg(c, 1);
        d.primitive_count = prim_count;
        d.stream[0].stride = arg(c, 8);
        d.inline_vertices = gm_ptr(arg(c, 7));
        d.inline_bytes = vbytes;
        d.inline_indices = gm_ptr(arg(c, 5));
        d.inline_index_bytes = ibytes;
        d.index_size = isize;
        gpu_draw(c, dev, d);
        com_ret(c, D3D_OK9);
        return;
    }
    D9DrawCall call;
    call.prim = arg(c, 1);
    call.prim_count = prim_count;
    call.vertices = arg(c, 7);
    call.stride = arg(c, 8);
    call.indices = arg(c, 5);
    call.index_size = arg(c, 6) == D3DFMT_INDEX32 ? 4 : 2;
    raster(c, call);
    com_ret(c, D3D_OK9);
}

// ---------------------------------------------------------------------------
// The resource interfaces themselves
// ---------------------------------------------------------------------------

static void resource_type_wgpu(X86 *c, ComObj *o) {
    if (!o || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::ValueResult result = o->d3d9_resource->resource_type();
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    com_ret(c, result.value);
}

void Res_GetType(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (kD3d9WgpuEnabled) {
        if (o && o->d3d9_implicit) {
            com_ret(c, 1);
            return;
        }
        resource_type_wgpu(c, o);
        return;
    }
    uint32_t type = 0;
    if (o && o->kind == K_D3D9SURFACE)
        type = 1;
    else if (o && o->kind == K_D3D9TEXTURE)
        type = o->caps ? 5 : 3;
    else if (o && o->kind == K_D3D9VB)
        type = 6;
    else if (o && o->kind == K_D3D9IB)
        type = 7;
    com_ret(c, type);
}

void Buf_GetType(X86 *c) {
    Res_GetType(c);
}

static const uint8_t kIidUnknown9[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0xc0, 0, 0, 0, 0, 0, 0, 0x46};

static void resource_container(X86 *c) {
    ComObj *s = com_this_arg(c);
    uint32_t riid = arg(c, 1), out = arg(c, 2);
    if (!s || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_out_ptr(out, 0);
    ComObj *target = s->d3d9_parent ? com_get(s->d3d9_parent) : com_get(s->dev_d3d);
    if (!target) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!riid || !gm_valid(riid, 16)) {
        com_ret(c, E_POINTER);
        return;
    }
    ComIface want = com_iface_for_iid(riid);
    if (want == IF_NONE) {
        if (memcmp(gm_ptr(riid), kIidUnknown9, 16) != 0) {
            com_ret(c, E_NOINTERFACE);
            return;
        }
        if (!target->identity) {
            com_ret(c, E_NOINTERFACE);
            return;
        }
        com_addref(target);
        com_out_ptr(out, target->identity);
        com_ret(c, D3D_OK9);
        return;
    }
    if (!com_iface_binds(want, target->kind)) {
        com_ret(c, E_NOINTERFACE);
        return;
    }
    uint32_t view = com_view(target, want);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(target);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

// (this, riid, ppContainer): the texture a level or face belongs to, or the
// device when the surface stands alone.
void Surf_GetContainer(X86 *c) {
    if (kD3d9WgpuEnabled) {
        resource_container(c);
        return;
    }
    ComObj *s = com_this_arg(c);
    uint32_t out = arg(c, 2);
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *parent = com_get(s->front_obj);
    if (parent) {
        com_addref(parent);
        com_out_ptr(out, com_view(parent, parent->caps ? IF_D3DCUBETEXTURE9 : IF_D3DTEXTURE9));
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *dev = com_get(s->dev_d3d);
    if (!dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_addref(dev);
    com_out_ptr(out, com_view(dev, IF_D3DDEVICE9));
    com_ret(c, D3D_OK9);
}

void Res_GetDevice(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t out = arg(c, 1);
    if (!o || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled && !o->d3d9_resource && !o->d3d9_implicit && !o->dev_d3d) {
        imports_unsupported(c);
        return;
    }
    ComObj *dev = com_get(o->dev_d3d);
    if (!dev || (kD3d9WgpuEnabled && !dev->d3d9_device)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(dev, IF_D3DDEVICE9);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(dev);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void Buf_GetDevice(X86 *c) {
    Res_GetDevice(c);
}

static uint32_t private_view_key(ComObj *o) {
    if (!o || !o->d3d9_parent)
        return 0;
    uint32_t key = 0x80000000u | ((o->d3d9_face & 0x1fu) << 8) | (o->d3d9_level & 0xffu);
    if (o->d3d9_volume)
        key |= 0x40000000u;
    return key;
}

static void resource_set_private_data(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t guid = arg(c, 1), data = arg(c, 2), size = arg(c, 3), flags = arg(c, 4);
    if (!o || !o->d3d9_resource || !guid || !gm_valid(guid, 16) ||
        (size && (!data || !gm_valid(data, size))) || (flags & ~1u)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (flags) {
        LOGW("d3d9: IUnknown private data is not implemented");
        imports_unsupported(c);
        return;
    }
    rust::Slice<const uint8_t> g(gm_ptr(guid), 16);
    rust::Slice<const uint8_t> d(data ? gm_ptr(data) : nullptr, size);
    recomp::d3d::Status status =
        o->d3d9_resource->set_private_data(private_view_key(o), g, d, flags);
    com_ret(c, status.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, status));
}

static void resource_get_private_data(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t guid = arg(c, 1), data = arg(c, 2), size_addr = arg(c, 3);
    if (!o || !o->d3d9_resource || !guid || !gm_valid(guid, 16) || !size_addr ||
        !gm_valid(size_addr, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t have = rd32(size_addr);
    if (data && have && !gm_valid(data, have)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    rust::Slice<const uint8_t> g(gm_ptr(guid), 16);
    recomp::d3d::D3d9PrivateDataResult result =
        o->d3d9_resource->get_private_data(private_view_key(o), g);
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    if (!result.found) {
        com_ret(c, D3DERR_NOTFOUND);
        return;
    }
    uint32_t need = (uint32_t)result.data.size();
    if (!data) {
        wr32(size_addr, need);
        com_ret(c, D3D_OK9);
        return;
    }
    if (have < need) {
        wr32(size_addr, need);
        com_ret(c, D3DERR_MOREDATA);
        return;
    }
    if (need) {
        if (!gm_valid(data, need)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        memcpy(gm_ptr(data), result.data.data(), need);
    }
    wr32(size_addr, need);
    com_ret(c, D3D_OK9);
}

static void resource_free_private_data(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t guid = arg(c, 1);
    if (!o || !o->d3d9_resource || !guid || !gm_valid(guid, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    rust::Slice<const uint8_t> g(gm_ptr(guid), 16);
    recomp::d3d::Status status = o->d3d9_resource->free_private_data(private_view_key(o), g);
    if (status.code == recomp::d3d::StatusCode::Ok)
        com_ret(c, D3D_OK9);
    else if (status.code == recomp::d3d::StatusCode::InvalidArgument)
        com_ret(c, D3DERR_NOTFOUND);
    else
        com_ret(c, bridge_result(c, status));
}

static void resource_set_priority(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::ValueResult result = o->d3d9_resource->set_priority(arg(c, 1));
    com_ret(c, result.status.code == recomp::d3d::StatusCode::Ok ? result.value
                                                                 : bridge_result(c, result.status));
}

static void resource_get_priority(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::ValueResult result = o->d3d9_resource->priority();
    com_ret(c, result.status.code == recomp::d3d::StatusCode::Ok ? result.value
                                                                 : bridge_result(c, result.status));
}

static void resource_set_lod(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::ValueResult result = o->d3d9_resource->set_lod(arg(c, 1));
    com_ret(c, result.status.code == recomp::d3d::StatusCode::Ok ? result.value
                                                                 : bridge_result(c, result.status));
}

static void resource_get_lod(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::ValueResult result = o->d3d9_resource->lod();
    com_ret(c, result.status.code == recomp::d3d::StatusCode::Ok ? result.value
                                                                 : bridge_result(c, result.status));
}

static void resource_preload(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    if (!d3d9_flush_resource_owner(c, o))
        return;
    recomp::d3d::Status status = o->d3d9_resource->preload();
    if (status.code != recomp::d3d::StatusCode::Ok) {
        std::string message(status.message.data(), status.message.size());
        LOGW("d3d9: PreLoad: %s", message.c_str());
        com_ret(c, bridge_result(c, status));
        return;
    }
    com_ret(c, D3D_OK9);
}

// Lock hands the guest the storage this resource was created with. Unlock is
// a no-op: there is nothing to upload to yet.
static void buffer_lock(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t offset = arg(c, 1), out = arg(c, 3);
    if (!o || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t staged = offset < o->blob.size() ? stage_lock(o) : 0;
    com_out_ptr(out, staged ? staged + offset : 0);
    com_ret(c, staged ? D3D_OK9 : E_OUTOFMEMORY);
}

static void buffer_lock_wgpu(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t offset = arg(c, 1), size = arg(c, 2), out = arg(c, 3), flags = arg(c, 4);
    if (!o || (o->kind != K_D3D9VB && o->kind != K_D3D9IB) || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9BufferLockRequest request{};
    request.offset = offset;
    request.size = size;
    request.flags = flags;
    recomp::d3d::D3d9BufferLockResult prepared = o->d3d9_resource->lock_prepare(request);
    if (prepared.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, prepared.status));
        return;
    }
    D3d9Staging &staging = o->d3d9_resource->staging[0];
    bool fresh = staging.addr == 0;
    if (fresh) {
        if (prepared.total == 0) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        uint32_t stage = heap_alloc(prepared.total, false, 16);
        if (!stage) {
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        rust::Slice<const uint8_t> bytes = o->d3d9_resource->bytes();
        if (bytes.size() != prepared.total) {
            heap_free(stage);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        memcpy(gm_ptr(stage), bytes.data(), prepared.total);
        staging.addr = stage;
        staging.size = prepared.total;
    }
    recomp::d3d::D3d9BufferLockResult committed = o->d3d9_resource->lock_commit(request);
    if (committed.status.code != recomp::d3d::StatusCode::Ok) {
        if (fresh) {
            heap_free(staging.addr);
            staging.addr = 0;
            staging.size = 0;
        }
        com_ret(c, bridge_result(c, committed.status));
        return;
    }
    wr32(out, staging.addr + committed.offset);
    com_ret(c, D3D_OK9);
}

static void buffer_unlock_wgpu(X86 *c) {
    ComObj *o = com_this_arg(c);
    if (!o || (o->kind != K_D3D9VB && o->kind != K_D3D9IB) || !o->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    auto it = o->d3d9_resource->staging.find(0);
    if (it == o->d3d9_resource->staging.end() || !it->second.addr) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    rust::Slice<const uint8_t> bytes(gm_ptr(it->second.addr), it->second.size);
    recomp::d3d::D3d9BufferUnlockResult result = o->d3d9_resource->unlock(bytes);
    if (result.closed) {
        heap_free(it->second.addr);
        o->d3d9_resource->staging.erase(it);
    }
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    com_ret(c, D3D_OK9);
}

void VB_Lock(X86 *c) {
    if (kD3d9WgpuEnabled) {
        buffer_lock_wgpu(c);
        return;
    }
    buffer_lock(c);
}
void IB_Lock(X86 *c) {
    if (kD3d9WgpuEnabled) {
        buffer_lock_wgpu(c);
        return;
    }
    buffer_lock(c);
}
void Buf_Unlock(X86 *c) {
    if (kD3d9WgpuEnabled) {
        buffer_unlock_wgpu(c);
        return;
    }
    stage_unlock(com_this_arg(c));
    com_ret(c, D3D_OK9);
}
// D3DVERTEXBUFFER_DESC / D3DINDEXBUFFER_DESC: Format, Type, Usage, Pool, Size.
void Buf_GetDesc(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t d = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!o || (o->kind != K_D3D9VB && o->kind != K_D3D9IB) || !o->d3d9_resource) {
            imports_unsupported(c);
            return;
        }
        uint32_t desc_size = o->kind == K_D3D9VB ? 24 : 20;
        if (!d || !gm_valid(d, desc_size)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9BufferDescriptorResult result = o->d3d9_resource->descriptor();
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        memset(gm_ptr(d), 0, desc_size);
        wr32(d + 0, result.descriptor.format);
        wr32(d + 4, result.descriptor.resource_type);
        wr32(d + 8, result.descriptor.usage);
        wr32(d + 12, result.descriptor.pool);
        wr32(d + 16, result.descriptor.size);
        if (o->kind == K_D3D9VB)
            wr32(d + 20, result.descriptor.fvf);
        com_ret(c, D3D_OK9);
        return;
    }
    if (!o || !d) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    memset(gm_ptr(d), 0, 20);
    wr32(d + 16, o->buf_bytes);
    com_ret(c, D3D_OK9);
}

// D3DLOCKED_RECT is { INT Pitch; void *pBits; }.
void Surf_LockRect(X86 *c) {
    if (kD3d9WgpuEnabled) {
        ComObj *o = com_this_arg(c);
        if (o && o->d3d9_implicit) {
            imports_unsupported(c);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        level_lock_wgpu(c, o, 0, 0, false, arg(c, 2), 0, arg(c, 3), arg(c, 1), false);
        return;
    }
    ComObj *s = com_this_arg(c);
    uint32_t out = arg(c, 1);
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t staged = stage_lock(s);
    wr32(out, s->pitch);
    wr32(out + 4, staged);
    com_ret(c, staged ? D3D_OK9 : E_OUTOFMEMORY);
}
void Surf_UnlockRect(X86 *c) {
    if (kD3d9WgpuEnabled) {
        ComObj *o = com_this_arg(c);
        if (o && o->d3d9_implicit) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        level_unlock_wgpu(c, o, 0, 0);
        return;
    }
    stage_unlock(com_this_arg(c));
    com_ret(c, D3D_OK9);
}
// D3DSURFACE_DESC: Format, Type, Usage, Pool, MultiSampleType, Quality,
// Width, Height.
void Surf_GetDesc(X86 *c) {
    ComObj *s = com_this_arg(c);
    uint32_t d = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (s && s->d3d9_implicit) {
            ComObj *dev = com_get(s->dev_d3d);
            if (!dev || !dev->d3d9_device || !d || !gm_valid(d, 32)) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
            recomp::d3d::D3d9SurfaceDescriptorResult result =
                s->d3d9_target_depth ? dev->d3d9_device->depthbuffer_descriptor()
                                     : dev->d3d9_device->backbuffer_descriptor();
            if (result.status.code != recomp::d3d::StatusCode::Ok) {
                com_ret(c, bridge_result(c, result.status));
                return;
            }
            memset(gm_ptr(d), 0, 32);
            wr32(d + 0, result.descriptor.format);
            wr32(d + 4, result.descriptor.resource_type);
            wr32(d + 8, result.descriptor.usage);
            wr32(d + 12, result.descriptor.pool);
            wr32(d + 24, result.descriptor.width);
            wr32(d + 28, result.descriptor.height);
            com_ret(c, D3D_OK9);
            return;
        }
        if (!s || !s->d3d9_resource || !d || !gm_valid(d, 32)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (s->d3d9_parent) {
            ComObj *parent = com_get(s->d3d9_parent);
            if (!parent || !parent->d3d9_resource) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
            recomp::d3d::D3d9TextureDescriptorResult td =
                parent->d3d9_resource->texture_descriptor();
            recomp::d3d::D3d9LevelDescriptorResult ld =
                parent->d3d9_resource->texture_level_descriptor(s->d3d9_face, s->d3d9_level);
            if (td.status.code != recomp::d3d::StatusCode::Ok) {
                com_ret(c, bridge_result(c, td.status));
                return;
            }
            if (ld.status.code != recomp::d3d::StatusCode::Ok) {
                com_ret(c, bridge_result(c, ld.status));
                return;
            }
            memset(gm_ptr(d), 0, 32);
            wr32(d + 0, ld.descriptor.format);
            wr32(d + 4, td.descriptor.resource_type);
            wr32(d + 8, td.descriptor.usage);
            wr32(d + 12, td.descriptor.pool);
            wr32(d + 24, ld.descriptor.width);
            wr32(d + 28, ld.descriptor.height);
            com_ret(c, D3D_OK9);
            return;
        }
        recomp::d3d::D3d9SurfaceDescriptorResult result = s->d3d9_resource->surface_descriptor();
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        memset(gm_ptr(d), 0, 32);
        wr32(d + 0, result.descriptor.format);
        wr32(d + 4, result.descriptor.resource_type);
        wr32(d + 8, result.descriptor.usage);
        wr32(d + 12, result.descriptor.pool);
        wr32(d + 16, result.descriptor.multisample_type);
        wr32(d + 20, result.descriptor.multisample_quality);
        wr32(d + 24, result.descriptor.width);
        wr32(d + 28, result.descriptor.height);
        com_ret(c, D3D_OK9);
        return;
    }
    if (!s || !d) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    memset(gm_ptr(d), 0, 32);
    wr32(d + 0, s->rmask ? s->rmask : 22);
    wr32(d + 24, s->width);
    wr32(d + 28, s->height);
    com_ret(c, D3D_OK9);
}

// A texture level is the surface made with the texture.
// A cube face, made on first ask and kept, so the game gets the same surface
// back every time. Returning success without writing the pointer - which is
// what the stub did - had the game store nothing and throw the cube away.
// The surface for one face and level of a cube, made on first ask and kept on
// the texture, indexed face + 6 * level.
static ComObj *cube_face(ComObj *tex, uint32_t face, uint32_t level) {
    if (!tex || tex->kind != K_D3D9TEXTURE || !tex->caps || face > 5 || level > 15)
        return nullptr;
    size_t slot = face + 6u * level;
    if (tex->surfaces.size() <= slot)
        tex->surfaces.resize(slot + 1, 0);
    ComObj *s = com_get(tex->surfaces[slot]);
    if (!s) {
        uint32_t edge = std::max(tex->width >> level, 1u);
        s = make_surface(com_get(tex->dev_d3d), edge, edge, tex->rmask ? tex->rmask : 22);
        if (!s)
            return nullptr;
        s->front_obj = tex->id;
        tex->surfaces[slot] = s->id;
    }
    return s;
}

// (this, FaceType, Level, ppCubeMapSurface). A face is a view into the
// texture, so handing one out references the texture too: the game takes
// face zero, releases the cube, and goes on using it for the other five.
void Tex_GetCubeMapSurface(X86 *c) {
    if (kD3d9WgpuEnabled) {
        ComObj *tex = com_this_arg(c);
        uint32_t out = arg(c, 3);
        if (!out || !gm_valid(out, 4)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        ComObj *s = com_level_view(tex, arg(c, 1), arg(c, 2), false);
        if (!s) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        uint32_t view = com_view(s, IF_D3DSURFACE9);
        if (!view) {
            com_release(s);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        com_out_ptr(out, view);
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *tex = com_this_arg(c);
    uint32_t out = arg(c, 3);
    ComObj *s = cube_face(tex, arg(c, 1), arg(c, 2));
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(s, IF_D3DSURFACE9);
    if (!view) {
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_addref(tex);
    com_addref(s);
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

// Returns a count, not an HRESULT. The stub returned zero, which told the
// game its textures had no levels at all.
// (this, FaceType, Level, pLockedRect, pRect, Flags)
void Tex_LockRectCube(X86 *c) {
    if (kD3d9WgpuEnabled) {
        level_lock_wgpu(c, com_this_arg(c), arg(c, 1), arg(c, 2), false, arg(c, 4), 0, arg(c, 5),
                        arg(c, 3), false);
        return;
    }
    ComObj *s = cube_face(com_this_arg(c), arg(c, 1), arg(c, 2));
    uint32_t out = arg(c, 3);
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t staged = stage_lock(s);
    wr32(out, s->pitch);
    wr32(out + 4, staged);
    com_ret(c, staged ? D3D_OK9 : E_OUTOFMEMORY);
}
// (this, FaceType, Level)
void Tex_UnlockRectCube(X86 *c) {
    if (kD3d9WgpuEnabled) {
        level_unlock_wgpu(c, com_this_arg(c), arg(c, 1), arg(c, 2));
        return;
    }
    stage_unlock(cube_face(com_this_arg(c), arg(c, 1), arg(c, 2)));
    com_ret(c, D3D_OK9);
}

void Tex_GetLevelCount(X86 *c) {
    ComObj *tex = com_this_arg(c);
    if (kD3d9WgpuEnabled) {
        if (!tex || !tex->d3d9_resource) {
            imports_unsupported(c);
            return;
        }
        recomp::d3d::D3d9TextureDescriptorResult result = tex->d3d9_resource->texture_descriptor();
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        com_ret(c, result.descriptor.levels);
        return;
    }
    set_eax(c, (tex && !tex->caps && !tex->surfaces.empty()) ? (uint32_t)tex->surfaces.size() : 1);
}

// (this, Level, pDesc): the same D3DSURFACE_DESC a surface describes itself
// with, for the level the texture was created at.
void Tex_GetLevelDesc(X86 *c) {
    ComObj *tex = com_this_arg(c);
    uint32_t d = arg(c, 2);
    if (kD3d9WgpuEnabled) {
        if (!tex || !tex->d3d9_resource || !d || !gm_valid(d, 32)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::D3d9TextureDescriptorResult td = tex->d3d9_resource->texture_descriptor();
        recomp::d3d::D3d9LevelDescriptorResult ld =
            tex->d3d9_resource->texture_level_descriptor(0, arg(c, 1));
        if (td.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, td.status));
            return;
        }
        if (ld.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, ld.status));
            return;
        }
        memset(gm_ptr(d), 0, 32);
        wr32(d + 0, ld.descriptor.format);
        wr32(d + 4, td.descriptor.resource_type);
        wr32(d + 8, td.descriptor.usage);
        wr32(d + 12, td.descriptor.pool);
        wr32(d + 24, ld.descriptor.width);
        wr32(d + 28, ld.descriptor.height);
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *s = texture_level(tex, arg(c, 1));
    if (!tex || !d) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    memset(gm_ptr(d), 0, 32);
    wr32(d + 0, tex->rmask ? tex->rmask : 22);
    wr32(d + 24, s ? s->width : tex->width);
    wr32(d + 28, s ? s->height : tex->height);
    com_ret(c, D3D_OK9);
}

void Tex_GetSurfaceLevel(X86 *c) {
    ComObj *tex = com_this_arg(c);
    uint32_t out = arg(c, 2);
    if (kD3d9WgpuEnabled) {
        if (!out || !gm_valid(out, 4)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        ComObj *s = com_level_view(tex, 0, arg(c, 1), false);
        if (!s) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        uint32_t view = com_view(s, IF_D3DSURFACE9);
        if (!view) {
            com_release(s);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        com_out_ptr(out, view);
        com_ret(c, D3D_OK9);
        return;
    }
    ComObj *s = texture_level(tex, arg(c, 1));
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    // A level is a view into the texture and references it, the same way a
    // cube face does.
    s->front_obj = tex->id;
    com_addref(tex);
    com_addref(s);
    uint32_t view_ = com_view(s, IF_D3DSURFACE9);
    if (!view_) {
        com_release(s);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view_);
    com_ret(c, D3D_OK9);
}
void Tex_LockRect(X86 *c) {
    if (kD3d9WgpuEnabled) {
        level_lock_wgpu(c, com_this_arg(c), 0, arg(c, 1), false, arg(c, 3), 0, arg(c, 4), arg(c, 2),
                        false);
        return;
    }
    ComObj *s = texture_level(com_this_arg(c), arg(c, 1));
    uint32_t out = arg(c, 2);
    if (!s || !out) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t staged = stage_lock(s);
    wr32(out, s->pitch);
    wr32(out + 4, staged);
    com_ret(c, staged ? D3D_OK9 : E_OUTOFMEMORY);
}
void Tex_UnlockRect(X86 *c) {
    if (kD3d9WgpuEnabled) {
        level_unlock_wgpu(c, com_this_arg(c), 0, arg(c, 1));
        return;
    }
    stage_unlock(texture_level(com_this_arg(c), arg(c, 1)));
    com_ret(c, D3D_OK9);
}

static void autogen_set_filter(X86 *c) {
    ComObj *tex = com_this_arg(c);
    if (!tex || !tex->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::Status status = tex->d3d9_resource->set_autogen_filter(arg(c, 1));
    com_ret(c, status.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, status));
}

static void autogen_get_filter(X86 *c) {
    ComObj *tex = com_this_arg(c);
    if (!tex || !tex->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::ValueResult result = tex->d3d9_resource->autogen_filter();
    com_ret(c, result.status.code == recomp::d3d::StatusCode::Ok ? result.value
                                                                 : bridge_result(c, result.status));
}

static void autogen_generate(X86 *c) {
    ComObj *tex = com_this_arg(c);
    if (!tex || !tex->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    if (!d3d9_flush_resource_owner(c, tex))
        return;
    recomp::d3d::Status status = tex->d3d9_resource->generate_mips();
    if (status.code != recomp::d3d::StatusCode::Ok) {
        std::string message(status.message.data(), status.message.size());
        LOGW("d3d9: GenerateMipSubLevels: %s", message.c_str());
    }
}

static bool read_dirty_rect(uint32_t rect, recomp::d3d::D3d9LockRect *out) {
    if (!rect)
        return true;
    if (!gm_valid(rect, 16))
        return false;
    out->left = (int32_t)rd32(rect + 0);
    out->top = (int32_t)rd32(rect + 4);
    out->right = (int32_t)rd32(rect + 8);
    out->bottom = (int32_t)rd32(rect + 12);
    return true;
}

static bool read_dirty_box(uint32_t box, recomp::d3d::D3d9LockBox *out) {
    if (!box)
        return true;
    if (!gm_valid(box, 24))
        return false;
    out->left = rd32(box + 0);
    out->top = rd32(box + 4);
    out->right = rd32(box + 8);
    out->bottom = rd32(box + 12);
    out->front = rd32(box + 16);
    out->back = rd32(box + 20);
    return true;
}

void Tex_AddDirtyRect(X86 *c) {
    ComObj *tex = com_this_arg(c);
    recomp::d3d::D3d9LockRect rect{};
    if (!tex || !tex->d3d9_resource || !read_dirty_rect(arg(c, 1), &rect)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = tex->d3d9_resource->add_dirty_rect(0, 0, arg(c, 1) == 0, rect);
    com_ret(c, status.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, status));
}

void Tex_AddDirtyRectCube(X86 *c) {
    ComObj *tex = com_this_arg(c);
    recomp::d3d::D3d9LockRect rect{};
    if (!tex || !tex->d3d9_resource || !read_dirty_rect(arg(c, 2), &rect)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status =
        tex->d3d9_resource->add_dirty_rect(arg(c, 1), 0, arg(c, 2) == 0, rect);
    com_ret(c, status.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, status));
}

void VolTex_GetLevelCount(X86 *c) {
    ComObj *tex = com_this_arg(c);
    if (!tex || !tex->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::D3d9TextureDescriptorResult result = tex->d3d9_resource->texture_descriptor();
    if (result.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    com_ret(c, result.descriptor.levels);
}

void VolTex_GetLevelDesc(X86 *c) {
    ComObj *tex = com_this_arg(c);
    uint32_t d = arg(c, 2);
    if (!tex || !tex->d3d9_resource || !d || !gm_valid(d, 28)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9TextureDescriptorResult td = tex->d3d9_resource->texture_descriptor();
    recomp::d3d::D3d9LevelDescriptorResult ld =
        tex->d3d9_resource->texture_volume_descriptor(arg(c, 1));
    if (td.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, td.status));
        return;
    }
    if (ld.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, ld.status));
        return;
    }
    memset(gm_ptr(d), 0, 28);
    wr32(d + 0, ld.descriptor.format);
    wr32(d + 4, td.descriptor.resource_type);
    wr32(d + 8, td.descriptor.usage);
    wr32(d + 12, td.descriptor.pool);
    wr32(d + 16, ld.descriptor.width);
    wr32(d + 20, ld.descriptor.height);
    wr32(d + 24, ld.descriptor.depth);
    com_ret(c, D3D_OK9);
}

void VolTex_GetVolumeLevel(X86 *c) {
    ComObj *tex = com_this_arg(c);
    uint32_t out = arg(c, 2);
    if (!out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *v = com_level_view(tex, 0, arg(c, 1), true);
    if (!v) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t view = com_view(v, IF_D3DVOLUME9);
    if (!view) {
        com_release(v);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    com_out_ptr(out, view);
    com_ret(c, D3D_OK9);
}

void VolTex_LockBox(X86 *c) {
    level_lock_wgpu(c, com_this_arg(c), 0, arg(c, 1), true, 0, arg(c, 3), arg(c, 4), arg(c, 2),
                    true);
}

void VolTex_UnlockBox(X86 *c) {
    level_unlock_wgpu(c, com_this_arg(c), 0, arg(c, 1));
}

void VolTex_AddDirtyBox(X86 *c) {
    ComObj *tex = com_this_arg(c);
    recomp::d3d::D3d9LockBox box{};
    if (!tex || !tex->d3d9_resource || !read_dirty_box(arg(c, 1), &box)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = tex->d3d9_resource->add_dirty_box(0, arg(c, 1) == 0, box);
    com_ret(c, status.code == recomp::d3d::StatusCode::Ok ? D3D_OK9 : bridge_result(c, status));
}

void Vol_GetContainer(X86 *c) {
    if (kD3d9WgpuEnabled) {
        resource_container(c);
        return;
    }
    com_ret(c, D3DERR_INVALIDCALL);
}

void Vol_GetDesc(X86 *c) {
    ComObj *v = com_this_arg(c);
    uint32_t d = arg(c, 1);
    if (!v || !v->d3d9_resource || !d || !gm_valid(d, 28)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t face = 0, level = 0;
    bool volume = false;
    ComObj *owner = level_owner(v, &face, &level, &volume);
    if (!owner || !owner->d3d9_resource) {
        imports_unsupported(c);
        return;
    }
    recomp::d3d::D3d9TextureDescriptorResult td = owner->d3d9_resource->texture_descriptor();
    recomp::d3d::D3d9LevelDescriptorResult ld =
        owner->d3d9_resource->texture_volume_descriptor(level);
    if (td.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, td.status));
        return;
    }
    if (ld.status.code != recomp::d3d::StatusCode::Ok) {
        com_ret(c, bridge_result(c, ld.status));
        return;
    }
    memset(gm_ptr(d), 0, 28);
    wr32(d + 0, ld.descriptor.format);
    wr32(d + 4, td.descriptor.resource_type);
    wr32(d + 8, td.descriptor.usage);
    wr32(d + 12, td.descriptor.pool);
    wr32(d + 16, ld.descriptor.width);
    wr32(d + 20, ld.descriptor.height);
    wr32(d + 24, ld.descriptor.depth);
    com_ret(c, D3D_OK9);
}

void Vol_LockBox(X86 *c) {
    level_lock_wgpu(c, com_this_arg(c), 0, 0, true, 0, arg(c, 2), arg(c, 3), arg(c, 1), true);
}

void Vol_UnlockBox(X86 *c) {
    level_unlock_wgpu(c, com_this_arg(c), 0, 0);
}

static const uint32_t S_FALSE9 = 1;
static std::unordered_map<uint32_t, uint32_t> &query_state() { // id -> 1 begun, 2 ended
    static auto *m = new std::unordered_map<uint32_t, uint32_t>();
    return *m;
}
static void query_destroy(ComObj *o) {
    if (kD3d9WgpuEnabled && o->d3d9_query) {
        if (ComObj *dev = com_get(o->dev_d3d)) {
            if (dev->d3d9_device)
                dev->d3d9_device->destroy_query(o->d3d9_query);
        }
        o->d3d9_query = 0;
    }
    if (o->d3d9_owner_retained) {
        o->d3d9_owner_retained = false;
        if (ComObj *dev = com_get(o->dev_d3d))
            com_release_internal(dev);
    }
    o->dev_d3d = 0;
    if (query_state().erase(o->id))
        host_d9_query_drop(o->id);
}
void Query_GetType9(X86 *c) {
    ComObj *q = com_this_arg(c);
    if (kD3d9WgpuEnabled) {
        ComObj *dev = q ? com_get(q->dev_d3d) : nullptr;
        if (!q || !q->d3d9_query || !dev || !dev->d3d9_device) {
            imports_unsupported(c);
            return;
        }
        recomp::d3d::ValueResult result = dev->d3d9_device->query_type(q->d3d9_query);
        if (!d3d_bridge_ok(result.status)) {
            bridge_result(c, result.status);
            return;
        }
        set_eax(c, result.value);
        return;
    }
    set_eax(c, q ? q->dev_type : 0);
}
void Query_GetDataSize(X86 *c) {
    ComObj *q = com_this_arg(c);
    uint32_t type = q ? q->dev_type : 0;
    if (kD3d9WgpuEnabled) {
        ComObj *dev = q ? com_get(q->dev_d3d) : nullptr;
        if (!q || !q->d3d9_query || !dev || !dev->d3d9_device) {
            imports_unsupported(c);
            return;
        }
        recomp::d3d::ValueResult kind = dev->d3d9_device->query_type(q->d3d9_query);
        if (!d3d_bridge_ok(kind.status)) {
            bridge_result(c, kind.status);
            return;
        }
        recomp::d3d::ValueResult result = recomp::d3d::d3d9_query_data_size(kind.value);
        if (!d3d_bridge_ok(result.status)) {
            bridge_result(c, result.status);
            return;
        }
        set_eax(c, result.value);
        return;
    }
    set_eax(c, (type == D3DQUERYTYPE_OCCLUSION || type == D3DQUERYTYPE_EVENT) ? 4 : 0);
}
// (this, dwIssueFlags): D3DISSUE_END 1, D3DISSUE_BEGIN 2.
void Query_Issue(X86 *c) {
    ComObj *q = com_this_arg(c);
    uint32_t flags = arg(c, 1);
    if (!q || (flags != D3DISSUE_BEGIN && flags != D3DISSUE_END)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!q->d3d9_query) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        ComObj *dev = com_get(q->dev_d3d);
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->query_issue(q->d3d9_query, flags)));
        return;
    }
    if (q->dev_type == D3DQUERYTYPE_OCCLUSION && gpu_on()) {
        if (flags == D3DISSUE_BEGIN) {
            host_d9_query_begin(q->id);
            query_state()[q->id] = 1;
        } else {
            host_d9_query_end(q->id);
            query_state()[q->id] = 2;
        }
        com_ret(c, D3D_OK9);
        return;
    }
    LOGW("d3d9: IDirect3DQuery9::Issue unsupported for query type %u", q->dev_type);
    imports_unsupported(c);
}
// (this, pData, dwSize, dwGetDataFlags)
void Query_GetData(X86 *c) {
    ComObj *q = com_this_arg(c);
    uint32_t data = arg(c, 1), size = arg(c, 2), flags = arg(c, 3);
    if (!q || (size != 0 && (size != 4 || !data || !gm_valid(data, size))) ||
        (flags & ~D3DGETDATA_FLUSH)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!q->d3d9_query) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (size) {
            if (!data || !gm_valid(data, size)) {
                com_ret(c, D3DERR_INVALIDCALL);
                return;
            }
        } else if (size != 0) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        ComObj *dev = com_get(q->dev_d3d);
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        uint8_t staged[4]{};
        rust::Slice<uint8_t> out(size ? staged : nullptr, size);
        recomp::d3d::ValueResult result =
            dev->d3d9_device->query_get_data(q->d3d9_query, out, size, flags);
        if (result.status.code != recomp::d3d::StatusCode::Ok) {
            com_ret(c, bridge_result(c, result.status));
            return;
        }
        if (!result.value) {
            com_ret(c, S_FALSE9);
            return;
        }
        if (size)
            memcpy(gm_ptr(data), staged, size);
        com_ret(c, D3D_OK9);
        return;
    }
    if (flags & D3DGETDATA_FLUSH) {
        LOGW("d3d9: IDirect3DQuery9::GetData D3DGETDATA_FLUSH unsupported");
        imports_unsupported(c);
    }
    if (q->dev_type != D3DQUERYTYPE_OCCLUSION || !gpu_on()) {
        LOGW("d3d9: IDirect3DQuery9::GetData unsupported for query type %u", q->dev_type);
        imports_unsupported(c);
    }
    auto it = query_state().find(q->id);
    if (it == query_state().end() || it->second != 2) {
        com_ret(c, S_FALSE9);
        return;
    }
    uint32_t count = 0;
    int r = host_d9_query_result(q->id, &count);
    if (r == 0) {
        com_ret(c, S_FALSE9);
        return;
    }
    if (r < 0) {
        LOGW("d3d9: IDirect3DQuery9::GetData host error");
        imports_unsupported(c);
    }
    if (size)
        wr32(data, count);
    com_ret(c, D3D_OK9);
}

// The pipeline record, and the device setters that write it
// ---------------------------------------------------------------------------
D9Pipeline &d9_pipeline(uint32_t device_id) {
    // Records are never erased and map nodes do not move, so the last one
    // asked for can be handed out again without a lookup.
    static auto *records = new std::map<uint32_t, D9Pipeline>();
    static thread_local uint32_t last_id = 0;
    static thread_local D9Pipeline *last = nullptr;
    if (last && last_id == device_id)
        return *last;
    last = &(*records)[device_id];
    last_id = device_id;
    return *last;
}

// (this, State, Value)
// (this, pViewport): D3DVIEWPORT9 is X, Y, Width, Height, MinZ, MaxZ.
void Dev_SetViewport(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t v = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !v || !gm_valid(v, 24)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::Viewport viewport{};
        viewport.x = rd32(v);
        viewport.y = rd32(v + 4);
        viewport.width = rd32(v + 8);
        viewport.height = rd32(v + 12);
        memcpy(&viewport.min_z, gm_ptr(v + 16), 4);
        memcpy(&viewport.max_z, gm_ptr(v + 20), 4);
        com_ret(c, bridge_result(c, dev->d3d9_device->set_viewport(viewport)));
        return;
    }
    if (dev && v) {
        D9Pipeline &p = d9_pipeline(dev->id);
        for (int i = 0; i < 4; ++i)
            p.viewport[i] = rd32(v + 4u * (uint32_t)i);
        uint32_t b0 = rd32(v + 16), b1 = rd32(v + 20);
        memcpy(&p.viewport_z[0], &b0, 4);
        memcpy(&p.viewport_z[1], &b1, 4);
        p.viewport_set = true;
    }
    com_ret(c, D3D_OK9);
}
void Dev_GetViewport(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t v = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !v || !gm_valid(v, 24)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        recomp::d3d::ViewportResult result = dev->d3d9_device->viewport();
        if (d3d_bridge_ok(result.status)) {
            wr32(v, result.viewport.x);
            wr32(v + 4, result.viewport.y);
            wr32(v + 8, result.viewport.width);
            wr32(v + 12, result.viewport.height);
            memcpy(gm_ptr(v + 16), &result.viewport.min_z, 4);
            memcpy(gm_ptr(v + 20), &result.viewport.max_z, 4);
        }
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    if (dev && v) {
        D9Pipeline &p = d9_pipeline(dev->id);
        ComObj *rt = com_get(dev->render_target);
        uint32_t whole[4] = {0, 0, rt ? rt->width : 640, rt ? rt->height : 480};
        for (int i = 0; i < 4; ++i)
            wr32(v + 4u * (uint32_t)i, p.viewport_set ? p.viewport[i] : whole[i]);
        float z0 = p.viewport_set ? p.viewport_z[0] : 0.0f,
              z1 = p.viewport_set ? p.viewport_z[1] : 1.0f;
        uint32_t b0, b1;
        memcpy(&b0, &z0, 4);
        memcpy(&b1, &z1, 4);
        wr32(v + 16, b0);
        wr32(v + 20, b1);
    }
    com_ret(c, D3D_OK9);
}

// (this, State, pMatrix): world, view, projection and texture transforms,
// kept for the fixed-function path.
void Dev_SetTransform(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t state = arg(c, 1), m = arg(c, 2);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device || !m || !gm_valid(m, 64)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        float matrix[16];
        memcpy(matrix, gm_ptr(m), 64);
        com_ret(c, bridge_result(c, dev->d3d9_device->set_transform(
                                        state, rust::Slice<const float>(matrix, 16))));
        return;
    }
    if (dev && m) {
        uint32_t slot =
            state < 256 ? state : (state >= 256 && state < 260 ? state - 256 + 24 : 0xffffffffu);
        if (slot < 32)
            for (int i = 0; i < 16; ++i) {
                uint32_t b = rd32(m + 4u * (uint32_t)i);
                memcpy(&d9_pipeline(dev->id).transform[slot][i], &b, 4);
            }
    }
    com_ret(c, D3D_OK9);
}

// (this, State, pMatrix): multiply the current transform by the supplied
// matrix, current = current * supplied, in D3D9's row-major layout.
void Dev_MultiplyTransform(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t state = arg(c, 1), m = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !m || !gm_valid(m, 64)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    float matrix[16];
    memcpy(matrix, gm_ptr(m), 64);
    com_ret(c, bridge_result(c, dev->d3d9_device->multiply_transform(
                                    state, rust::Slice<const float>(matrix, 16))));
}

// (this, Stage, Type, Value)
void Dev_SetTextureStageState(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t stage = arg(c, 1), type = arg(c, 2);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(
            c, bridge_result(c, dev->d3d9_device->set_texture_stage_state(stage, type, arg(c, 3))));
        return;
    }
    if (dev && stage < 8 && type < 33)
        d9_pipeline(dev->id).tss[stage][type] = arg(c, 3);
    com_ret(c, D3D_OK9);
}

void Dev_SetRenderState(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t state = arg(c, 1);
    if (kD3d9WgpuEnabled) {
        if (!dev || !dev->d3d9_device) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->set_render_state(state, arg(c, 2))));
        return;
    }
    if (!dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (state < D3DRS_ZENABLE || state > D3DRS_BLENDOPALPHA) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D9Pipeline &p = d9_pipeline(dev->id);
    if (p.rs[state] != arg(c, 2) || !p.rs_set[state]) {
        p.rs[state] = arg(c, 2);
        p.rs_set[state] = true;
        p.states_changed();
    }
    com_ret(c, D3D_OK9);
}

// (this, Sampler, Type, Value)
void Dev_SetSamplerState(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t sampler = arg(c, 1), type = arg(c, 2);
    if (!dev) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (kD3d9WgpuEnabled) {
        if (!dev->d3d9_device || sampler >= 16) {
            if (sampler >= D3DDMAPSAMPLER && sampler <= D3DVERTEXTEXTURESAMPLER3)
                imports_unsupported(c);
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        com_ret(c, bridge_result(c, dev->d3d9_device->set_sampler_state(sampler, type, arg(c, 3))));
        return;
    }
    if (sampler >= 16) {
        if (sampler >= D3DDMAPSAMPLER && sampler <= D3DVERTEXTEXTURESAMPLER3) {
            LOGW("d3d9: SetSamplerState unsupported for sampler %u", sampler);
            imports_unsupported(c);
        }
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (type < D3DSAMP_ADDRESSU || type > D3DSAMP_DMAPOFFSET) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    D9Pipeline &p = d9_pipeline(dev->id);
    if (p.sampler_state[sampler][type] != arg(c, 3)) {
        p.sampler_state[sampler][type] = arg(c, 3);
        p.states_changed();
    }
    com_ret(c, D3D_OK9);
}

static bool write_registers(float (*table)[4], uint32_t limit, uint32_t start, uint32_t data,
                            uint32_t count) {
    if (count == 0)
        return true;
    if (start > limit || count > limit - start)
        return false;
    if (!data || !gm_valid(data, count * 16))
        return false;
    for (uint32_t i = 0; i < count; ++i)
        for (uint32_t k = 0; k < 4; ++k) {
            uint32_t bits = rd32(data + i * 16 + k * 4);
            memcpy(&table[start + i][k], &bits, 4);
        }
    return true;
}

// (this, StartRegister, pConstantData, Vector4fCount)
void Dev_SetVertexShaderConstantF(X86 *c) {
    ComObj *dev = this_device9(c);
    if (kD3d9WgpuEnabled) {
        uint32_t start = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
        if (!dev || !dev->d3d9_device || count > 256 || start > 256 - count) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (!count) {
            com_ret(c, D3D_OK9);
            return;
        }
        if (!data || !gm_valid(data, count * 16)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        std::vector<float> values(count * 4);
        memcpy(values.data(), gm_ptr(data), count * 16);
        com_ret(c, bridge_result(c, dev->d3d9_device->set_shader_constants_f(
                                        false, start,
                                        rust::Slice<const float>(values.data(), values.size()))));
        return;
    }
    if (!dev ||
        !write_registers(d9_pipeline(dev->id).vconst, 256, arg(c, 1), arg(c, 2), arg(c, 3))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_ret(c, D3D_OK9);
}
void Dev_SetPixelShaderConstantF(X86 *c) {
    ComObj *dev = this_device9(c);
    if (kD3d9WgpuEnabled) {
        uint32_t start = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
        if (!dev || !dev->d3d9_device || count > 32 || start > 32 - count) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        if (!count) {
            com_ret(c, D3D_OK9);
            return;
        }
        if (!data || !gm_valid(data, count * 16)) {
            com_ret(c, D3DERR_INVALIDCALL);
            return;
        }
        std::vector<float> values(count * 4);
        memcpy(values.data(), gm_ptr(data), count * 16);
        com_ret(c, bridge_result(c, dev->d3d9_device->set_shader_constants_f(
                                        true, start,
                                        rust::Slice<const float>(values.data(), values.size()))));
        return;
    }
    if (!dev ||
        !write_registers(d9_pipeline(dev->id).pconst, 32, arg(c, 1), arg(c, 2), arg(c, 3))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_ret(c, D3D_OK9);
}

// The DLL's one export. Not a COM method: the guest calls it directly.
void d3d9_Direct3DCreate9(X86 *c) {
    ComObj *o = com_new(K_D3D9);
    uint32_t view = o ? com_view(o, IF_D3D9) : 0;
    if (!view) {
        if (o)
            com_release(o);
        set_eax(c, 0);
        return;
    }
    LOGW("d3d9: Direct3DCreate9(SDK %u) -> %08x", arg(c, 0), view);
    set_eax(c, view);
}

static const ImportShim g_d3d9_exports[] = {
    {"d3d9.dll", "Direct3DCreate9", 1, d3d9_Direct3DCreate9},
};

void d3d9_reset() {
    g_scratch = 0;
    g_scratch_size = 0;
}

static bool d3d9_write_view(uint32_t out, uint32_t id, ComIface iface) {
    ComObj *o = com_get(id);
    if (!o)
        return false;
    com_addref(o);
    uint32_t view = com_view(o, iface);
    if (!view) {
        com_release(o);
        return false;
    }
    com_out_ptr(out, view);
    return true;
}

static void d3d9_stage_shader(uint32_t addr, std::vector<uint32_t> &out) {
    if (!addr)
        return;
    for (uint32_t i = 0; i < 4096; ++i) {
        if (addr > UINT32_MAX - i * 4u)
            break;
        uint32_t a = addr + i * 4u;
        if (!gm_valid(a, 4))
            break;
        out.push_back(rd32(a));
    }
}

static uint32_t d3d9_shader_id(ComObj *dev, uint32_t handle, bool pixel) {
    if (!dev || !dev->d3d9_device || !handle)
        return 0;
    auto &map = dev->d3d9_device->shader_views;
    auto it = map.find(handle);
    return it == map.end() ? 0 : it->second;
}

void Dev_GetRenderState(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->render_state(arg(c, 1));
    if (d3d_bridge_ok(result.status))
        wr32(out, result.value);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetTextureStageState(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 3);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->texture_stage_state(arg(c, 1), arg(c, 2));
    if (d3d_bridge_ok(result.status))
        wr32(out, result.value);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetSamplerState(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 3);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->sampler_state(arg(c, 1), arg(c, 2));
    if (d3d_bridge_ok(result.status))
        wr32(out, result.value);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetTransform(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || (!out || !gm_valid(out, 64))) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::FloatValuesResult result = dev->d3d9_device->transform(arg(c, 1));
    if (d3d_bridge_ok(result.status) && result.values.size() == 16)
        for (size_t i = 0; i < 16; ++i)
            memcpy(gm_ptr(out + (uint32_t)i * 4), &result.values[i], 4);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetScissorRect(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9ScissorResult result = dev->d3d9_device->scissor();
    if (d3d_bridge_ok(result.status)) {
        wr32(out, (uint32_t)result.left);
        wr32(out + 4, (uint32_t)result.top);
        wr32(out + 8, (uint32_t)result.right);
        wr32(out + 12, (uint32_t)result.bottom);
    }
    com_ret(c, bridge_result(c, result.status));
}
void Dev_SetMaterial(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t p = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !p || !gm_valid(p, 68)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    float values[17];
    memcpy(values, gm_ptr(p), 68);
    com_ret(c,
            bridge_result(c, dev->d3d9_device->set_material(rust::Slice<const float>(values, 17))));
}
void Dev_GetMaterial(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t p = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !p || !gm_valid(p, 68)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::FloatValuesResult result = dev->d3d9_device->material();
    if (d3d_bridge_ok(result.status) && result.values.size() == 17)
        memcpy(gm_ptr(p), result.values.data(), 68);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_SetLight(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t p = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !p || !gm_valid(p, 104)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t type = rd32(p);
    float values[25];
    memcpy(values, gm_ptr(p + 4), 100);
    com_ret(c, bridge_result(c, dev->d3d9_device->set_light(arg(c, 1), type,
                                                            rust::Slice<const float>(values, 25))));
}
void Dev_GetLight(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t p = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !p || !gm_valid(p, 104)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9LightResult result = dev->d3d9_device->light(arg(c, 1));
    if (d3d_bridge_ok(result.status) && result.values.size() == 25) {
        wr32(p, result.light_type);
        memcpy(gm_ptr(p + 4), result.values.data(), 100);
    }
    com_ret(c, bridge_result(c, result.status));
}
void Dev_LightEnable(X86 *c) {
    ComObj *dev = this_device9(c);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_ret(c, bridge_result(c, dev->d3d9_device->set_light_enable(arg(c, 1), arg(c, 2) != 0)));
}
void Dev_GetLightEnable(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->light_enable(arg(c, 1));
    if (d3d_bridge_ok(result.status))
        wr32(out, result.value);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_SetClipPlane(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t p = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !p || !gm_valid(p, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    float values[4];
    memcpy(values, gm_ptr(p), 16);
    com_ret(c, bridge_result(c, dev->d3d9_device->set_clip_plane(
                                    arg(c, 1), rust::Slice<const float>(values, 4))));
}
void Dev_GetClipPlane(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t p = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !p || !gm_valid(p, 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::FloatValuesResult result = dev->d3d9_device->clip_plane(arg(c, 1));
    if (d3d_bridge_ok(result.status) && result.values.size() == 4)
        memcpy(gm_ptr(p), result.values.data(), 16);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetTexture(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->texture(arg(c, 1));
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    if (!result.value || !d3d9_write_view(out, result.value, IF_D3DBASETEXTURE9))
        wr32(out, 0);
    com_ret(c, D3D_OK9);
}
void Dev_GetStreamSource(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2), offset = arg(c, 3), stride = arg(c, 4);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4) || !offset || !gm_valid(offset, 4) ||
        !stride || !gm_valid(stride, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9StreamResult result = dev->d3d9_device->stream_source(arg(c, 1));
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    if (!result.buffer || !d3d9_write_view(out, result.buffer, IF_D3DVERTEXBUFFER9))
        wr32(out, 0);
    wr32(offset, result.offset);
    wr32(stride, result.stride);
    com_ret(c, D3D_OK9);
}
void Dev_SetStreamSourceFreq(X86 *c) {
    ComObj *dev = this_device9(c);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    com_ret(c, bridge_result(c, dev->d3d9_device->set_stream_freq(arg(c, 1), arg(c, 2))));
}
void Dev_GetStreamSourceFreq(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->stream_freq(arg(c, 1));
    if (d3d_bridge_ok(result.status))
        wr32(out, result.value);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetIndices(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->indices();
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    if (!result.value || !d3d9_write_view(out, result.value, IF_D3DINDEXBUFFER9))
        wr32(out, 0);
    com_ret(c, D3D_OK9);
}
void Dev_GetFVF(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->fvf();
    if (d3d_bridge_ok(result.status))
        wr32(out, result.value);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_SetFVF(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t fvf = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!fvf) {
        com_ret(c, D3D_OK9);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->create_fvf_declaration(fvf);
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    if (dev->d3d9_device->decl_views.find(result.value) == dev->d3d9_device->decl_views.end()) {
        ComObj *d = com_new(K_D3D9DECL);
        if (!d) {
            dev->d3d9_device->destroy_declaration(result.value);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        d->dev_d3d = dev->id;
        d->d3d9_state_handle = result.value;
        com_internalize(d);
        uint32_t view = com_view(d, IF_D3DVERTEXDECL9);
        if (!view) {
            com_release_internal(d);
            com_ret(c, E_OUTOFMEMORY);
            return;
        }
        dev->d3d9_device->decl_views[result.value] = d->id;
        dev->d3d9_device->fvf_decls.insert(d->id);
    }
    recomp::d3d::Status status = dev->d3d9_device->set_fvf_binding(result.value, fvf);
    if (d3d_bridge_ok(status))
        d3d9_refresh_bindings(dev);
    com_ret(c, bridge_result(c, status));
}
void Dev_GetVertexDeclaration(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->vertex_declaration();
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    uint32_t id = 0;
    if (result.value) {
        auto it = dev->d3d9_device->decl_views.find(result.value);
        if (it != dev->d3d9_device->decl_views.end())
            id = it->second;
    }
    if (!id || !d3d9_write_view(out, id, IF_D3DVERTEXDECL9))
        wr32(out, 0);
    com_ret(c, D3D_OK9);
}
void Dev_CreateVertexShader(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    std::vector<uint32_t> code;
    d3d9_stage_shader(arg(c, 1), code);
    recomp::d3d::ValueResult result = dev->d3d9_device->create_shader(
        false, rust::Slice<const uint32_t>(code.data(), code.size()));
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    ComObj *o = d3d9_make_object(dev, K_D3D9VS, IF_D3DVERTEXSHADER9, result.value, out);
    if (!o) {
        dev->d3d9_device->destroy_shader(false, result.value);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    dev->d3d9_device->shader_views[result.value] = o->id;
    com_ret(c, D3D_OK9);
}
void Dev_SetVertexShader(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *s = com_this(arg(c, 1));
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || (arg(c, 1) && (!s || s->kind != K_D3D9VS)) ||
        (s && s->dev_d3d != dev->id)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = dev->d3d9_device->set_shader(false, s ? s->d3d9_state_handle : 0);
    if (d3d_bridge_ok(status))
        d3d9_refresh_bindings(dev);
    com_ret(c, bridge_result(c, status));
}
void Dev_GetVertexShader(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->shader(false);
    uint32_t id = d3d9_shader_id(dev, d3d_bridge_ok(result.status) ? result.value : 0, false);
    if (!id || !d3d9_write_view(out, id, IF_D3DVERTEXSHADER9))
        wr32(out, 0);
    com_ret(c, D3D_OK9);
}
void Dev_CreatePixelShader(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    std::vector<uint32_t> code;
    d3d9_stage_shader(arg(c, 1), code);
    recomp::d3d::ValueResult result = dev->d3d9_device->create_shader(
        true, rust::Slice<const uint32_t>(code.data(), code.size()));
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    ComObj *o = d3d9_make_object(dev, K_D3D9PS, IF_D3DPIXELSHADER9, result.value, out);
    if (!o) {
        dev->d3d9_device->destroy_shader(true, result.value);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    dev->d3d9_device->shader_views[result.value] = o->id;
    com_ret(c, D3D_OK9);
}
void Dev_SetPixelShader(X86 *c) {
    ComObj *dev = this_device9(c);
    ComObj *s = com_this(arg(c, 1));
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || (arg(c, 1) && (!s || s->kind != K_D3D9PS)) ||
        (s && s->dev_d3d != dev->id)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = dev->d3d9_device->set_shader(true, s ? s->d3d9_state_handle : 0);
    if (d3d_bridge_ok(status))
        d3d9_refresh_bindings(dev);
    com_ret(c, bridge_result(c, status));
}
void Dev_GetPixelShader(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValueResult result = dev->d3d9_device->shader(true);
    uint32_t id = d3d9_shader_id(dev, d3d_bridge_ok(result.status) ? result.value : 0, true);
    if (!id || !d3d9_write_view(out, id, IF_D3DPIXELSHADER9))
        wr32(out, 0);
    com_ret(c, D3D_OK9);
}
static void d3d9_get_function(X86 *c, bool pixel) {
    ComObj *o = com_this_arg(c);
    uint32_t data = arg(c, 1), size = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!o || o->kind != (pixel ? K_D3D9PS : K_D3D9VS) || !size || !gm_valid(size, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *dev = com_get(o->dev_d3d);
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::ValuesResult result =
        dev->d3d9_device->shader_function(pixel, o->d3d9_state_handle);
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    uint32_t required = (uint32_t)result.values.size() * 4;
    uint32_t available = rd32(size);
    wr32(size, required);
    if (!data) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (available < required || !gm_valid(data, required)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    for (size_t i = 0; i < result.values.size(); ++i)
        wr32(data + (uint32_t)i * 4, result.values[i]);
    com_ret(c, D3D_OK9);
}
void VS_GetFunction(X86 *c) {
    d3d9_get_function(c, false);
}
void PS_GetFunction(X86 *c) {
    d3d9_get_function(c, true);
}
void Decl_GetDeclaration(X86 *c) {
    ComObj *o = com_this_arg(c);
    uint32_t data = arg(c, 1), count = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!o || o->kind != K_D3D9DECL || !count || !gm_valid(count, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *dev = com_get(o->dev_d3d);
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::D3d9DeclarationResult result =
        dev->d3d9_device->declaration_elements(o->d3d9_state_handle);
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    uint32_t required = (uint32_t)result.elements.size();
    uint32_t available = rd32(count);
    wr32(count, required);
    if (!data) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (available < required || !gm_valid(data, required * 8)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    for (uint32_t i = 0; i < required; ++i) {
        const recomp::d3d::D3d9VertexElement &e = result.elements[i];
        uint32_t at = data + i * 8;
        wr16(at, e.stream);
        wr16(at + 2, e.offset);
        gm_ptr(at)[4] = e.element_type;
        gm_ptr(at)[5] = e.method;
        gm_ptr(at)[6] = e.usage;
        gm_ptr(at)[7] = e.usage_index;
    }
    com_ret(c, D3D_OK9);
}
static void d3d9_constant_i(X86 *c, bool pixel, bool set) {
    ComObj *dev = this_device9(c);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t start = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
    if (count > 16 || start > 16 - count) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!count) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!data || !gm_valid(data, count * 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (set) {
        std::vector<int32_t> values(count * 4);
        memcpy(values.data(), gm_ptr(data), count * 16);
        com_ret(c, bridge_result(c, dev->d3d9_device->set_shader_constants_i(
                                        pixel, start,
                                        rust::Slice<const int32_t>(values.data(), values.size()))));
        return;
    }
    recomp::d3d::ValuesResult result = dev->d3d9_device->shader_constants_i(pixel, start, count);
    if (d3d_bridge_ok(result.status) && result.values.size() == (size_t)count * 4)
        for (uint32_t i = 0; i < count * 4; ++i)
            wr32(data + i * 4, result.values[i]);
    com_ret(c, bridge_result(c, result.status));
}
static void d3d9_constant_b(X86 *c, bool pixel, bool set) {
    ComObj *dev = this_device9(c);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    uint32_t start = arg(c, 1), data = arg(c, 2), count = arg(c, 3);
    if (count > 16 || start > 16 - count) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!count) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!data || !gm_valid(data, count * 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (set) {
        std::vector<uint32_t> values(count);
        for (uint32_t i = 0; i < count; ++i)
            values[i] = rd32(data + i * 4);
        com_ret(c,
                bridge_result(c, dev->d3d9_device->set_shader_constants_b(
                                     pixel, start,
                                     rust::Slice<const uint32_t>(values.data(), values.size()))));
        return;
    }
    recomp::d3d::ValuesResult result = dev->d3d9_device->shader_constants_b(pixel, start, count);
    if (d3d_bridge_ok(result.status) && result.values.size() == count)
        for (uint32_t i = 0; i < count; ++i)
            wr32(data + i * 4, result.values[i]);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetVertexShaderConstantF(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t data = arg(c, 2), count = arg(c, 3);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || count > 256 || arg(c, 1) > 256 - count) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!count) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!data || !gm_valid(data, count * 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::FloatValuesResult result =
        dev->d3d9_device->shader_constants_f(false, arg(c, 1), count);
    if (d3d_bridge_ok(result.status) && result.values.size() == (size_t)count * 4)
        memcpy(gm_ptr(data), result.values.data(), count * 16);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_GetPixelShaderConstantF(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t data = arg(c, 2), count = arg(c, 3);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || count > 32 || arg(c, 1) > 32 - count) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    if (!count) {
        com_ret(c, D3D_OK9);
        return;
    }
    if (!data || !gm_valid(data, count * 16)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::FloatValuesResult result =
        dev->d3d9_device->shader_constants_f(true, arg(c, 1), count);
    if (d3d_bridge_ok(result.status) && result.values.size() == (size_t)count * 4)
        memcpy(gm_ptr(data), result.values.data(), count * 16);
    com_ret(c, bridge_result(c, result.status));
}
void Dev_SetVertexShaderConstantI(X86 *c) {
    d3d9_constant_i(c, false, true);
}
void Dev_GetVertexShaderConstantI(X86 *c) {
    d3d9_constant_i(c, false, false);
}
void Dev_SetPixelShaderConstantI(X86 *c) {
    d3d9_constant_i(c, true, true);
}
void Dev_GetPixelShaderConstantI(X86 *c) {
    d3d9_constant_i(c, true, false);
}
void Dev_SetVertexShaderConstantB(X86 *c) {
    d3d9_constant_b(c, false, true);
}
void Dev_GetVertexShaderConstantB(X86 *c) {
    d3d9_constant_b(c, false, false);
}
void Dev_SetPixelShaderConstantB(X86 *c) {
    d3d9_constant_b(c, true, true);
}
void Dev_GetPixelShaderConstantB(X86 *c) {
    d3d9_constant_b(c, true, false);
}
void Dev_CreateStateBlock(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 2);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    recomp::d3d::ValueResult result = dev->d3d9_device->create_state_block(arg(c, 1));
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    ComObj *b = d3d9_make_object(dev, K_D3D9STATEBLOCK, IF_D3DSTATEBLOCK9, result.value, out);
    if (!b) {
        dev->d3d9_device->destroy_state_block(result.value);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    dev->d3d9_device->block_views[result.value] = b->id;
    d3d9_refresh_block_bindings(dev, result.value);
    com_ret(c, D3D_OK9);
}
void Dev_BeginStateBlock(X86 *c) {
    ComObj *dev = this_device9(c);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = dev->d3d9_device->begin_state_block();
    if (d3d_bridge_ok(status))
        dev->d3d9_device->recording_block = true;
    com_ret(c, bridge_result(c, status));
}
void Dev_EndStateBlock(X86 *c) {
    ComObj *dev = this_device9(c);
    uint32_t out = arg(c, 1);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!dev || !dev->d3d9_device || !out || !gm_valid(out, 4)) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    wr32(out, 0);
    recomp::d3d::ValueResult result = dev->d3d9_device->end_state_block();
    if (d3d_bridge_ok(result.status))
        dev->d3d9_device->recording_block = false;
    if (!d3d_bridge_ok(result.status)) {
        com_ret(c, bridge_result(c, result.status));
        return;
    }
    ComObj *b = d3d9_make_object(dev, K_D3D9STATEBLOCK, IF_D3DSTATEBLOCK9, result.value, out);
    if (!b) {
        d3d9_refresh_bindings(dev);
        dev->d3d9_device->destroy_state_block(result.value);
        com_ret(c, E_OUTOFMEMORY);
        return;
    }
    dev->d3d9_device->block_views[result.value] = b->id;
    d3d9_refresh_block_bindings(dev, result.value);
    d3d9_refresh_bindings(dev);
    com_ret(c, D3D_OK9);
}
void SB_Capture(X86 *c) {
    ComObj *b = com_this_arg(c);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!b || b->kind != K_D3D9STATEBLOCK) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *dev = com_get(b->dev_d3d);
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = dev->d3d9_device->capture_state_block(b->d3d9_state_handle);
    if (d3d_bridge_ok(status))
        d3d9_refresh_block_bindings(dev, b->d3d9_state_handle);
    com_ret(c, bridge_result(c, status));
}
void SB_Apply(X86 *c) {
    ComObj *b = com_this_arg(c);
    if (!kD3d9WgpuEnabled) {
        imports_unsupported(c);
        return;
    }
    if (!b || b->kind != K_D3D9STATEBLOCK) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    ComObj *dev = com_get(b->dev_d3d);
    if (!dev || !dev->d3d9_device) {
        com_ret(c, D3DERR_INVALIDCALL);
        return;
    }
    recomp::d3d::Status status = dev->d3d9_device->apply_state_block(b->d3d9_state_handle);
    if (d3d_bridge_ok(status))
        d3d9_refresh_bindings(dev);
    com_ret(c, bridge_result(c, status));
}

#include "d3d9_interfaces.inc"

static void d3d9_drop_fvf_cache(ComObj *dev) {
    if (!dev || !dev->d3d9_device)
        return;
    for (uint32_t id : dev->d3d9_device->fvf_decls) {
        if (ComObj *o = com_get(id))
            com_release_internal(o);
    }
    dev->d3d9_device->fvf_decls.clear();
}

static void d3d9_device_destroy(ComObj *o) {
    device_discard_implicit_surfaces(o);
    d3d9_clear_virtual_display(o);
    if (o->d3d9_device) {
        if (X86 *c = guest_current_context())
            win32_uncover_display(c, o->hwnd);
        d3d9_drop_all_refs(o);
        d3d9_drop_fvf_cache(o);
        o->d3d9_device.reset();
        if (ComObj *factory = com_get(o->dev_d3d))
            com_release(factory);
    }
    o->dev_d3d = 0;
    o->d3d9_focus_window = 0;
}

static void d3d9_child_refs_changed(ComObj *o) {
    if (!kD3d9WgpuEnabled || !o || !o->dev_d3d)
        return;
    ComObj *dev = com_get(o->dev_d3d);
    if (!dev || !dev->d3d9_device)
        return;
    const bool external = o->refs > o->internal_refs;
    if (external == o->d3d9_owner_retained)
        return;
    o->d3d9_owner_retained = external;
    if (external)
        com_retain_internal(dev);
    else
        com_release_internal(dev);
}

static void d3d9_pipeline_object_destroy(ComObj *o) {
    if (!o)
        return;
    if (kD3d9WgpuEnabled) {
        ComObj *dev = com_get(o->dev_d3d);
        if (dev && dev->d3d9_device) {
            D3d9HostDevice *host = dev->d3d9_device.get();
            switch (o->kind) {
            case K_D3D9VS:
                host->shader_views.erase(o->d3d9_state_handle);
                host->destroy_shader(false, o->d3d9_state_handle);
                break;
            case K_D3D9PS:
                host->shader_views.erase(o->d3d9_state_handle);
                host->destroy_shader(true, o->d3d9_state_handle);
                break;
            case K_D3D9DECL:
                host->decl_views.erase(o->d3d9_state_handle);
                host->fvf_decls.erase(o->id);
                host->destroy_declaration(o->d3d9_state_handle);
                break;
            case K_D3D9STATEBLOCK:
                d3d9_drop_block_refs(dev, o->d3d9_state_handle);
                host->block_views.erase(o->d3d9_state_handle);
                host->destroy_state_block(o->d3d9_state_handle);
                break;
            default:
                break;
            }
        }
        if (o->d3d9_owner_retained) {
            o->d3d9_owner_retained = false;
            if (dev)
                com_release_internal(dev);
        }
        o->dev_d3d = 0;
        o->d3d9_state_handle = 0;
        return;
    }
    o->d3d9_state_handle = 0;
}

void d3d9_register() {
    static bool done = false;
    if (done)
        return;
    done = true;

    com_define(IF_D3D9, "d3d9.dll", "IDirect3D9", g_d3d9, std::size(g_d3d9));
    com_define(IF_D3DDEVICE9, "d3d9.dll", "IDirect3DDevice9", g_device9, std::size(g_device9));
    com_define(IF_D3DRESOURCE9, "d3d9.dll", "IDirect3DResource9", g_resource9,
               std::size(g_resource9));
    com_bind(IF_D3D9, K_D3D9);
    com_bind(IF_D3DDEVICE9, K_D3D9DEVICE);
    com_bind(IF_D3DRESOURCE9, K_D3D9VB);
    com_bind(IF_D3DRESOURCE9, K_D3D9IB);
    com_register_iid(IF_D3D9, IID_IDirect3D9_);
    com_register_iid(IF_D3DDEVICE9, IID_IDirect3DDevice9_);
    com_register_iid(IF_D3DRESOURCE9, IID_IDirect3DResource9_);
    com_define(IF_D3DTEXTURE9, "d3d9.dll", "IDirect3DTexture9", g_texture9, std::size(g_texture9));
    com_define(IF_D3DCUBETEXTURE9, "d3d9.dll", "IDirect3DCubeTexture9", g_cubetexture9,
               std::size(g_cubetexture9));
    com_define(IF_D3DSURFACE9, "d3d9.dll", "IDirect3DSurface9", g_surface9, std::size(g_surface9));
    com_define(IF_D3DVERTEXBUFFER9, "d3d9.dll", "IDirect3DVertexBuffer9", g_vb9, std::size(g_vb9));
    com_define(IF_D3DINDEXBUFFER9, "d3d9.dll", "IDirect3DIndexBuffer9", g_ib9, std::size(g_ib9));
    com_define(IF_D3DVERTEXDECL9, "d3d9.dll", "IDirect3DVertexDeclaration9", g_decl9,
               std::size(g_decl9));
    com_define(IF_D3DQUERY9, "d3d9.dll", "IDirect3DQuery9", g_query9, std::size(g_query9));
    com_define(IF_D3DBASETEXTURE9, "d3d9.dll", "IDirect3DBaseTexture9", g_basetexture9,
               std::size(g_basetexture9));
    com_define(IF_D3DVOLUME9, "d3d9.dll", "IDirect3DVolume9", g_volume9, std::size(g_volume9));
    com_define(IF_D3DVOLUMETEXTURE9, "d3d9.dll", "IDirect3DVolumeTexture9", g_volumetexture9,
               std::size(g_volumetexture9));
    com_define(IF_D3DVERTEXSHADER9, "d3d9.dll", "IDirect3DVertexShader9", g_vertexshader9,
               std::size(g_vertexshader9));
    com_define(IF_D3DPIXELSHADER9, "d3d9.dll", "IDirect3DPixelShader9", g_pixelshader9,
               std::size(g_pixelshader9));
    com_define(IF_D3DSTATEBLOCK9, "d3d9.dll", "IDirect3DStateBlock9", g_stateblock9,
               std::size(g_stateblock9));
    com_register_iid(IF_D3DTEXTURE9, IID_IDirect3DTexture9_);
    com_register_iid(IF_D3DCUBETEXTURE9, IID_IDirect3DCubeTexture9_);
    com_register_iid(IF_D3DSURFACE9, IID_IDirect3DSurface9_);
    com_register_iid(IF_D3DVERTEXBUFFER9, IID_IDirect3DVertexBuffer9_);
    com_register_iid(IF_D3DINDEXBUFFER9, IID_IDirect3DIndexBuffer9_);
    com_register_iid(IF_D3DVERTEXDECL9, IID_IDirect3DVertexDeclaration9_);
    com_register_iid(IF_D3DQUERY9, IID_IDirect3DQuery9_);
    com_register_iid(IF_D3DBASETEXTURE9, IID_IDirect3DBaseTexture9_);
    com_register_iid(IF_D3DVOLUME9, IID_IDirect3DVolume9_);
    com_register_iid(IF_D3DVOLUMETEXTURE9, IID_IDirect3DVolumeTexture9_);
    com_register_iid(IF_D3DVERTEXSHADER9, IID_IDirect3DVertexShader9_);
    com_register_iid(IF_D3DPIXELSHADER9, IID_IDirect3DPixelShader9_);
    com_register_iid(IF_D3DSTATEBLOCK9, IID_IDirect3DStateBlock9_);
    com_bind(IF_D3DTEXTURE9, K_D3D9TEXTURE);
    com_bind(IF_D3DCUBETEXTURE9, K_D3D9CUBETEXTURE);
    if (!kD3d9WgpuEnabled)
        com_bind(IF_D3DCUBETEXTURE9, K_D3D9TEXTURE);
    com_bind(IF_D3DVOLUMETEXTURE9, K_D3D9VOLUMETEXTURE);
    com_bind(IF_D3DVOLUME9, K_D3D9VOLUME);
    com_bind(IF_D3DBASETEXTURE9, K_D3D9TEXTURE);
    com_bind(IF_D3DBASETEXTURE9, K_D3D9CUBETEXTURE);
    com_bind(IF_D3DBASETEXTURE9, K_D3D9VOLUMETEXTURE);
    com_bind(IF_D3DSURFACE9, K_D3D9SURFACE);
    com_bind(IF_D3DRESOURCE9, K_D3D9TEXTURE);
    com_bind(IF_D3DRESOURCE9, K_D3D9CUBETEXTURE);
    com_bind(IF_D3DRESOURCE9, K_D3D9VOLUMETEXTURE);
    com_bind(IF_D3DRESOURCE9, K_D3D9SURFACE);
    com_bind(IF_D3DVERTEXBUFFER9, K_D3D9VB);
    com_bind(IF_D3DINDEXBUFFER9, K_D3D9IB);
    com_bind(IF_D3DVERTEXDECL9, K_D3D9DECL);
    com_bind(IF_D3DQUERY9, K_D3D9QUERY);
    com_bind(IF_D3DVERTEXSHADER9, K_D3D9VS);
    com_bind(IF_D3DPIXELSHADER9, K_D3D9PS);
    com_bind(IF_D3DSTATEBLOCK9, K_D3D9STATEBLOCK);
    for (ComKind k : {K_D3D9TEXTURE, K_D3D9CUBETEXTURE, K_D3D9VOLUMETEXTURE, K_D3D9VOLUME,
                      K_D3D9SURFACE, K_D3D9VB, K_D3D9IB})
        com_set_destructor(k, d3d9_resource_destroy);
    for (ComKind k : {K_D3D9DECL, K_D3D9VS, K_D3D9PS, K_D3D9STATEBLOCK})
        com_set_destructor(k, d3d9_pipeline_object_destroy);
    for (ComKind k : {K_D3D9SURFACE, K_D3D9TEXTURE, K_D3D9CUBETEXTURE, K_D3D9VOLUMETEXTURE,
                      K_D3D9VB, K_D3D9IB, K_D3D9DECL, K_D3D9VS, K_D3D9PS, K_D3D9STATEBLOCK})
        com_set_ref_hook(k, d3d9_child_refs_changed);
    com_set_destructor(K_D3D9QUERY, query_destroy);
    com_set_destructor(K_D3D9DEVICE, d3d9_device_destroy);
    imports_register(g_d3d9_exports, std::size(g_d3d9_exports));
}
