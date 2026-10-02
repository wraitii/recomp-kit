// Guest ABI contract for the implicit D3D8 backbuffer. Devices here are
// constructed state fixtures, not host renderer creation or execution evidence.
#include "../dx.h"
#include "../../runtime/memory.h"
#include "guest_abi.h"

static void check(bool value, const char *message) {
    ++g_checks;
    if (!value) {
        ++g_failures;
        fprintf(stderr, "FAIL: %s\n", message);
    }
}

static void test_backbuffer(uint32_t format) {
    cpu_reset();
    ComObj *factory = com_new(K_D3D8);
    uint32_t factory_view = com_view(factory, IF_D3D8), factory_id = factory->id;
    ComObj *dev = com_new(K_D3D8DEVICE);
    dev->d3d8_factory = factory_id;
    com_addref(factory);
    dev->d3d8_width = 319;
    dev->d3d8_height = 241;
    dev->d3d8_format = format;
    uint32_t device = com_view(dev, IF_D3D8DEVICE), device_id = dev->id;
    check(call_method(device, 6, {sc(20)}) == 0 && rd32(sc(20)) == factory_view &&
              factory->refs == 3,
          "GetDirect3D returns and retains the factory");
    call_method(factory_view, 2);
    check(call_method(factory_view, 2) == 1, "device retains released factory");
    check(call_method(device, 16, {0, 0, sc(0)}) == 0, "GetBackBuffer succeeds");
    uint32_t surface = rd32(sc(0));
    check(surface != 0, "GetBackBuffer writes a real interface");
    if (!surface)
        return;
    ComObj *obj = com_this(surface, IF_D3D8SURFACE8);
    check(obj && obj->kind == K_D3D8SURFACE, "surface view has its own COM identity");
    check(dev->refs == 2, "external surface retains device");
    check(call_method(device, 16, {0, 0, sc(4)}) == 0 && rd32(sc(4)) == surface,
          "repeated GetBackBuffer preserves identity and adds a reference");
    check(obj->refs == 2, "two GetBackBuffer calls hold two surface references");

    // Every slot carries the real D3D8 arity, including unsupported methods.
    const uint8_t arities[] = {3, 1, 1, 2, 5, 4, 2, 3, 2, 4, 1};
    for (uint32_t slot = 0; slot < sizeof(arities); ++slot)
        check(imports_argc(rd32(rd32(surface) + 4 * slot)) == arities[slot],
              "surface vtable slot arity");
    memset(gm_ptr(sc(64)), 0xa5, 40);
    check(call_method(surface, 8, {sc(68)}) == 0, "GetDesc uses slot 8");
    const uint32_t expected[] = {format, 1, 1, 0, 319 * 241 * 4, 0, 319, 241};
    for (uint32_t i = 0; i < 8; ++i)
        check(rd32(sc(68 + 4 * i)) == expected[i], "guest D3DSURFACE_DESC field");
    check(rd32(sc(64)) == 0xa5a5a5a5 && rd32(sc(100)) == 0xa5a5a5a5,
          "GetDesc writes exactly 32 bytes");
    check(call_method(surface, 8, {0}) == 0x8876086c, "GetDesc rejects null output");
    check(call_method(surface, 8, {GUEST_SIZE - 16}) == 0x8876086c,
          "GetDesc rejects truncated output");

    const uint8_t surface_iid[] = {0xca, 0xeb, 0x6e, 0xb9, 0x26, 0xb3, 0xa5, 0x4e,
                                   0x88, 0x2f, 0x2f, 0xf5, 0xba, 0xe0, 0x21, 0xdd};
    const uint8_t unknown_iid[] = {0, 0, 0, 0, 0, 0, 0, 0, 0xc0, 0, 0, 0, 0, 0, 0, 0x46};
    memcpy(gm_ptr(sc(128)), surface_iid, 16);
    memcpy(gm_ptr(sc(144)), unknown_iid, 16);
    for (uint32_t iid : {sc(128), sc(144)}) {
        check(call_method(surface, 0, {iid, sc(8)}) == 0 && rd32(sc(8)) == surface,
              "QueryInterface retains canonical surface identity");
        check(call_method(surface, 2) == 2, "release QueryInterface reference");
    }
    check(call_method(surface, 3, {sc(12)}) == 0 && rd32(sc(12)) == device,
          "GetDevice returns owning device");
    check(dev->refs == 3, "GetDevice adds a reference");
    check(call_method(device, 2) == 2, "release GetDevice reference");
    check(call_method(surface, 3, {0}) == 0x8876086c && dev->refs == 2,
          "failed GetDevice does not retain device");

    wr32(sc(16), 0xfeedface);
    check(call_method(device, 16, {1, 0, sc(16)}) == 0x8876086c && rd32(sc(16)) == 0,
          "reject nonexistent backbuffer index and clear output");
    check(call_method(device, 16, {0, 1, sc(16)}) == 0x8876086c, "reject stereo backbuffer type");
    check(call_method(device, 16, {0, 0, 0}) == 0x8876086c, "reject null backbuffer output");
    check(call_method(device, 16, {0, 0, GUEST_SIZE - 2}) == 0x8876086c,
          "reject truncated backbuffer output");
    check(obj->refs == 2 && dev->refs == 2, "invalid calls leak no references");

    uint32_t surface_id = obj->id;
    check(call_method(surface, 2) == 1, "release first surface reference");
    check(call_method(surface, 2) == 0, "release final surface reference");
    check(!com_get(surface_id) && dev->refs == 1 && !dev->d3d8_backbuffer,
          "surface destruction clears weak cache and releases device");
    check(call_method(device, 16, {0, 0, sc(0)}) == 0,
          "backbuffer remains available after external references are released");
    surface = rd32(sc(0));
    check(call_method(device, 2) == 1, "surface keeps device alive after caller releases it");
    check(call_method(surface, 8, {sc(68)}) == 0, "retained surface descriptor remains valid");
    check(call_method(surface, 2) == 0 && !com_get(device_id),
          "last surface release destroys otherwise unreferenced device");
    check(!com_get(factory_id), "device destruction releases factory");
}

int main(int argc, char **argv) {
    mem_init();
    imports_init();
    dx_register_shims();
    g_stack_top = STACK_TOP - 0x1000;
    g_scratch = heap_alloc(0x400, true, 16);
    if (!g_scratch)
        return 1;
    if (argc == 2 && strcmp(argv[1], "--unsupported-lock") == 0) {
        cpu_reset();
        ComObj *surface = com_new(K_D3D8SURFACE);
        call_method(com_view(surface, IF_D3D8SURFACE8), 9, {sc(0), 0, 0});
        return 1; // The unsupported call must abort, even with RECOMP_LOG=0.
    }
    if (argc == 2 && strcmp(argv[1], "--unsupported-texture") == 0) {
        cpu_reset();
        ComObj *dev = com_new(K_D3D8DEVICE);
        call_method(com_view(dev, IF_D3D8DEVICE), 20, {16, 16, 1, 0, 21, 0, sc(0)});
        return 1;
    }
    cpu_reset();
    uint32_t factory = call_shim(tramp("d3d8.dll", "Direct3DCreate8"), {220});
    check(factory && call_method(factory, 4) == 0, "no-Rust factory advertises no adapter");
    uint32_t pp = sc(192);
    wr32(pp, 319);
    wr32(pp + 4, 241);
    wr32(pp + 8, 22);
    wr32(pp + 12, 1);
    wr32(pp + 20, 1);
    wr32(pp + 28, 1);
    wr32(sc(0), 0xfeedface);
    check(call_method(factory, 15, {0, 1, 0, 0x20, pp, sc(0)}) == 0x8876086a && rd32(sc(0)) == 0,
          "no-Rust CreateDevice fails with cleared output and correct stack cleanup");
    call_method(factory, 2);
    test_backbuffer(21); // A8R8G8B8
    test_backbuffer(22); // X8R8G8B8
    // Reset follows the runtime's generation order: old guest heap first,
    // then module state and COM vtables. No stale weak cache may survive.
    mem_init();
    imports_init();
    dx_reset();
    g_scratch = heap_alloc(0x400, true, 16);
    test_backbuffer(22);
    printf("d3d8 surface: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
