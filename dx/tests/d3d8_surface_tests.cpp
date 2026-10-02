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

// Creates a device object with the fields CreateTexture reads. No renderer is
// involved: a system-memory texture is CPU bytes.
static ComObj *make_test_device(uint32_t w, uint32_t h, uint32_t format) {
    ComObj *dev = com_new(K_D3D8DEVICE);
    dev->d3d8_width = w;
    dev->d3d8_height = h;
    dev->d3d8_format = format;
    return dev;
}

static void test_texture() {
    cpu_reset();
    ComObj *dev = make_test_device(8, 4, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);

    // The startup call: 1x1, 1 level, R5G6B5, SYSTEMMEM.
    wr32(sc(0), 0xfeedface);
    check(call_method(device, 20, {1, 1, 1, 0, 23, 2, sc(0)}) == 0, "CreateTexture succeeds");
    uint32_t tex = rd32(sc(0));
    check(tex != 0, "CreateTexture writes a real interface");
    ComObj *t = com_this(tex, IF_D3D8TEXTURE8);
    check(t && t->kind == K_D3D8TEXTURE, "texture view has its own COM identity");
    if (!t)
        return;
    check(t->d3d8_level_count == 1, "one level requested is one level created");

    // Every slot carries the real D3D8 arity, including unsupported methods.
    const uint8_t arities[] = {3, 1, 1, 2, 5, 4, 2, 2, 1, 1, 1, 2, 1, 1, 3, 3, 5, 2, 2};
    for (uint32_t slot = 0; slot < sizeof(arities); ++slot)
        check(imports_argc(rd32(rd32(tex) + 4 * slot)) == arities[slot],
              "texture vtable slot arity");

    check(call_method(tex, 10, {}) == 3, "GetType reports D3DRTYPE_TEXTURE");
    check(call_method(tex, 13, {}) == 1, "GetLevelCount is one");
    check(call_method(tex, 11, {4}) == 0 && call_method(tex, 12, {}) == 4,
          "SetLOD returns the old LOD and GetLOD reads the new one");

    check(call_method(tex, 14, {0, sc(32)}) == 0, "GetLevelDesc succeeds");
    check(rd32(sc(32)) == 23 && rd32(sc(36)) == 1 && rd32(sc(44)) == 2 && rd32(sc(56)) == 1 &&
              rd32(sc(60)) == 1,
          "level 0 descriptor is 1x1 R5G6B5 with pitch 2");
    check(call_method(tex, 14, {1, sc(32)}) == 0x8876086c, "out-of-range level is rejected");
    check(call_method(tex, 14, {0, 0}) == 0x8876086c, "null level descriptor is rejected");

    // LockRect returns a guest heap pointer in the format's own layout.
    check(call_method(tex, 16, {0, sc(8), 0, 0}) == 0, "LockRect succeeds");
    uint32_t pitch = rd32(sc(8)), ptr = rd32(sc(12));
    check(pitch == 2 && ptr != 0, "LockRect returns pitch 2 and a guest pointer");
    wr16(ptr, 0xf81f);
    check(call_method(tex, 17, {0}) == 0, "UnlockRect succeeds");
    check(call_method(tex, 16, {0, sc(8), 0, 0}) == 0 && rd16(rd32(sc(12))) == 0xf81f,
          "unlocked texel bytes survive to the next lock");
    call_method(tex, 17, {0});

    // A level surface shares the texture's staging.
    check(call_method(tex, 15, {0, sc(16)}) == 0 && rd32(sc(16)) != 0,
          "GetSurfaceLevel returns a surface view");
    uint32_t surf = rd32(sc(16));
    check(call_method(surf, 8, {sc(64)}) == 0 && rd32(sc(64)) == 23 && rd32(sc(88)) == 1 &&
              rd32(sc(92)) == 1,
          "level surface descriptor matches the level");
    check(call_method(surf, 9, {sc(8), 0, 0}) == 0 && rd32(sc(8)) == 2 &&
              rd16(rd32(sc(12))) == 0xf81f,
          "surface LockRect shares the level bytes");
    call_method(surf, 10);
    call_method(surf, 2);
    check(call_method(tex, 3, {sc(20)}) == 0 && rd32(sc(20)) == device,
          "texture GetDevice returns the owning device");
    call_method(device, 2); // release the GetDevice reference
    check(call_method(tex, 2) == 0, "last texture reference destroys it");
    check(!t->alive, "texture object is gone");

    // Levels = 0 asks for the full chain, halving each side to 1x1.
    cpu_reset();
    ComObj *dev2 = make_test_device(8, 4, 22);
    uint32_t device2 = com_view(dev2, IF_D3D8DEVICE);
    check(call_method(device2, 20, {8, 4, 0, 0, 21, 2, sc(0)}) == 0,
          "CreateTexture with levels=0 succeeds");
    uint32_t tex2 = rd32(sc(0));
    ComObj *t2 = com_this(tex2, IF_D3D8TEXTURE8);
    check(t2 && t2->d3d8_level_count == 4, "8x4 generates four levels");
    check(call_method(tex2, 14, {1, sc(32)}) == 0 && rd32(sc(56)) == 4 && rd32(sc(60)) == 2,
          "level 1 is 4x2");
    check(call_method(tex2, 14, {3, sc(32)}) == 0 && rd32(sc(56)) == 1 && rd32(sc(60)) == 1,
          "level 3 is 1x1");

    // A sub-rect lock points at the rect's top-left within the level's own
    // pitch; an empty or out-of-bounds rect is refused.
    uint32_t rect = sc(96);
    wr32(rect, 1);
    wr32(rect + 4, 1);
    wr32(rect + 8, 2);
    wr32(rect + 12, 2);
    check(call_method(tex2, 16, {1, sc(8), rect, 0}) == 0, "sub-rect lock succeeds");
    uint32_t sub_ptr = rd32(sc(12));
    check(rd32(sc(8)) == 16, "level 1 pitch is 16");
    check(call_method(tex2, 16, {1, sc(8), 0, 0}) == 0, "full lock for the base pointer");
    check(rd32(sc(12)) == sub_ptr - (16 + 4), "sub-rect pointer is base + top*pitch + left*bpp");
    call_method(tex2, 17, {1});
    call_method(tex2, 17, {1});
    wr32(rect, 0);
    wr32(rect + 4, 0);
    wr32(rect + 8, 5);
    wr32(rect + 12, 2);
    check(call_method(tex2, 16, {1, sc(8), rect, 0}) == 0x8876086c,
          "a rect past the level's right edge is rejected");
    wr32(rect + 8, 4);
    check(call_method(tex2, 16, {1, sc(8), rect, 0}) == 0, "the exact-bound rect is accepted");
    call_method(tex2, 17, {1});
    wr32(rect + 8, 0);
    check(call_method(tex2, 16, {1, sc(8), rect, 0}) == 0x8876086c, "an empty rect is rejected");

    // The level surface uses the same staging block and offset.
    check(call_method(tex2, 15, {1, sc(16)}) == 0, "GetSurfaceLevel on level 1");
    uint32_t surf2 = rd32(sc(16));
    check(call_method(surf2, 9, {sc(8), 0, 0}) == 0, "surface full lock");
    uint32_t surf_base = rd32(sc(12));
    call_method(surf2, 10);
    wr32(rect, 1);
    wr32(rect + 4, 1);
    wr32(rect + 8, 2);
    wr32(rect + 12, 2);
    check(call_method(tex2, 16, {1, sc(8), rect, 0}) == 0, "texture sub-rect lock");
    check(rd32(sc(12)) == surf_base + 16 + 4, "surface and texture share level staging");
    call_method(tex2, 17, {1});
    call_method(surf2, 2);
    call_method(tex2, 2);

    // UpdateTexture copies every level between a SYSTEMMEM source and a
    // DEFAULT destination of the same format and dimensions; every contract
    // violation is rejected without a partial copy.
    check(call_method(device2, 20, {1, 1, 1, 0, 23, 2, sc(0)}) == 0, "CreateTexture source");
    uint32_t src_tex = rd32(sc(0));
    check(call_method(device2, 20, {1, 1, 1, 0, 23, 0, sc(0)}) == 0, "CreateTexture destination");
    uint32_t dst_tex = rd32(sc(0));
    check(call_method(src_tex, 16, {0, sc(8), 0, 0}) == 0, "lock source");
    wr16(rd32(sc(12)), 0x07e0);
    call_method(src_tex, 17, {0});
    check(call_method(device2, 29, {src_tex, dst_tex}) == 0, "UpdateTexture succeeds");
    check(call_method(dst_tex, 16, {0, sc(8), 0, 0}) == 0 && rd16(rd32(sc(12))) == 0x07e0,
          "UpdateTexture copied the level bytes");
    call_method(dst_tex, 17, {0});
    check(call_method(device2, 29, {src_tex, src_tex}) == 0x8876086c,
          "UpdateTexture rejects source == destination");

    // A format mismatch is refused.
    check(call_method(device2, 20, {1, 1, 1, 0, 21, 0, sc(0)}) == 0,
          "CreateTexture wrong-format destination");
    uint32_t fmt_dst = rd32(sc(0));
    check(call_method(device2, 29, {src_tex, fmt_dst}) == 0x8876086c,
          "UpdateTexture rejects a format mismatch");
    call_method(fmt_dst, 2);
    // A DEFAULT-pool source is refused.
    check(call_method(device2, 20, {1, 1, 1, 0, 23, 0, sc(0)}) == 0,
          "CreateTexture DEFAULT source");
    uint32_t def_src = rd32(sc(0));
    check(call_method(device2, 29, {def_src, dst_tex}) == 0x8876086c,
          "UpdateTexture rejects a DEFAULT source");
    call_method(def_src, 2);
    // Mismatched dimensions are refused before anything is copied.
    check(call_method(device2, 20, {2, 1, 1, 0, 23, 2, sc(0)}) == 0, "CreateTexture wide source");
    uint32_t wide_src = rd32(sc(0));
    check(call_method(device2, 29, {wide_src, dst_tex}) == 0x8876086c,
          "UpdateTexture rejects mismatched dimensions");
    check(call_method(dst_tex, 16, {0, sc(8), 0, 0}) == 0 && rd16(rd32(sc(12))) == 0x07e0,
          "a rejected UpdateTexture leaves the destination unchanged");
    call_method(dst_tex, 17, {0});
    // Mismatched level counts are refused.
    check(call_method(device2, 20, {2, 1, 0, 0, 23, 2, sc(0)}) == 0,
          "CreateTexture two-level source");
    uint32_t two_src = rd32(sc(0));
    check(call_method(device2, 29, {two_src, dst_tex}) == 0x8876086c,
          "UpdateTexture rejects mismatched level counts");
    call_method(two_src, 2);
    call_method(wide_src, 2);
    call_method(src_tex, 2);
    call_method(dst_tex, 2);

    // Releasing a texture whose level is still locked must free the staging.
    check(call_method(device2, 20, {2, 2, 1, 0, 23, 2, sc(0)}) == 0, "CreateTexture lock-destroy");
    uint32_t locked_tex = rd32(sc(0));
    check(call_method(locked_tex, 16, {0, sc(8), 0, 0}) == 0, "lock the level");
    uint32_t staged = rd32(sc(12));
    check(heap_size(staged) != 0xffffffff, "staging block is live while locked");
    check(call_method(locked_tex, 2) == 0, "release the locked texture");
    check(heap_size(staged) == 0xffffffff, "destroying a locked level frees its staging block");

    // An unrepresentable format fails without fabricating a texture.
    wr32(sc(0), 0xfeedface);
    check(call_method(device2, 20, {4, 4, 1, 0, 0x31545844, 2, sc(0)}) == 0x8876086c,
          "an unrepresentable texture format is rejected");
    check(rd32(sc(0)) == 0, "failed CreateTexture clears the output pointer");
    call_method(device2, 2);
    check(dev2->refs == 0, "all texture references release the device");
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
        // CreateVolumeTexture is still unsupported; the probe must abort by name.
        call_method(com_view(dev, IF_D3D8DEVICE), 21, {4, 4, 4, 1, 0, 21, 2, sc(0)});
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
    test_texture();
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
