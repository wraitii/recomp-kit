// Guest ABI contract for the implicit D3D8 backbuffer. Devices here are
// constructed state fixtures, not host renderer creation or execution evidence.
#include "../dx.h"
#include "../d3d8_fpu.h"
#include "../../runtime/memory.h"
#include "guest_abi.h"

static void check(bool value, const char *message) {
    ++g_checks;
    if (!value) {
        ++g_failures;
        fprintf(stderr, "FAIL: %s\n", message);
    }
}

// Exercise the arithmetic behind a log/exponent-based dimension check using
// the same helpers as generated FLDLN2/FYL2X/FMUL and truncating FISTP. This
// tests a branch-sensitive result, not approximate agreement of FP values.
static int dimension_exponent(uint32_t size) {
    fpush(&g_cpu, 0.69314718055994530942);
    fpush(&g_cpu, (double)size);
    fset(&g_cpu, 1, fx87_exact(&g_cpu, ST(&g_cpu, 1) * log2(ST(&g_cpu, 0))));
    fdrop(&g_cpu);
    fset(&g_cpu, 0, fx87(&g_cpu, ST(&g_cpu, 0) * 1.4426950408889634));
    uint16_t cw = g_cpu.fpu_cw;
    x87_set_cw(&g_cpu, (uint16_t)(cw | 0x0c00u));
    int result = (int)fround_cw(&g_cpu, fpop(&g_cpu));
    x87_set_cw(&g_cpu, cw);
    return result;
}

static void test_device_fpu() {
    cpu_reset();
    x87_set_cw(&g_cpu, 0x027f);
    check(dimension_exponent(128) == 6, "binary64 intermediate reproduces below-integer exponent");
    for (uint32_t flags : {0x20u, 0x40u, 0x24u, 0x44u}) {
        cpu_reset();
        x87_set_cw(&g_cpu, 0x1f40);
        g_cpu.fpu_sw = 0x4120;
        fpush(&g_cpu, 1.25);
        uint16_t tag = g_cpu.fpu_tag;
        uint32_t top = g_cpu.fpu_top;
        d3d8_setup_guest_fpu(&g_cpu, flags);
        check(g_cpu.fpu_cw == 0x107f,
              "default device FPU masks exceptions, sets PC24/nearest and preserves other bits");
        check(g_cpu.fpu_sw == 0x4120 && g_cpu.fpu_tag == tag && g_cpu.fpu_top == top &&
                  ST(&g_cpu, 0) == 1.25,
              "device FPU setup preserves status and stack");
        fdrop(&g_cpu);
        for (int exponent = 0; exponent <= 13; ++exponent)
            check(dimension_exponent(1u << exponent) == exponent,
                  "power-of-two exponent survives default device precision and truncation");
        for (uint32_t size : {127u, 129u, 255u, 257u})
            check(ldexp(1.0, dimension_exponent(size)) != (double)size,
                  "nearby non-power dimension remains rejected");
        check(g_cpu.fpu_cw == 0x107f && g_cpu.fpu_top == 0,
              "dimension check restores CW and balances x87 stack");
    }
    for (uint16_t cw : {0x037f, 0x027f, 0x1c60}) {
        x87_set_cw(&g_cpu, cw);
        d3d8_setup_guest_fpu(&g_cpu, 0x26);
        check(g_cpu.fpu_cw == cw, "FPU_PRESERVE leaves guest CW unchanged");
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
    check(obj->refs == 3,
          "two GetBackBuffer calls hold two guest references plus device ownership");

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
        check(call_method(surface, 2) == 3, "release QueryInterface reference");
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
    check(obj->refs == 3 && dev->refs == 2, "invalid calls leak no references");

    uint32_t surface_id = obj->id;
    check(call_method(surface, 2) == 2, "release first surface reference");
    check(call_method(surface, 2) == 1 && com_get(surface_id) == obj,
          "device ownership keeps the surface alive after guest references are released");
    check(!obj->d3d8_owner_retained && dev->refs == 1 && dev->d3d8_backbuffer == surface_id,
          "internal-only surface does not retain the device and stays weakly cached");
    check(call_method(device, 16, {0, 0, sc(0)}) == 0 && rd32(sc(0)) == surface,
          "backbuffer remains available and is the device-owned surface");
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

    // Signed bump maps keep native U,V bytes in guest locks at every mip.
    check(call_method(device2, 20, {128, 128, 5, 0, 60, 1, sc(0)}) == 0,
          "CreateTexture V8U8 managed mip chain succeeds");
    uint32_t bump = rd32(sc(0));
    for (uint32_t level = 0; level < 5; ++level) {
        uint32_t width = 128 >> level;
        check(call_method(bump, 14, {level, sc(32)}) == 0 && rd32(sc(32)) == 60 &&
                  rd32(sc(44)) == 1 && rd32(sc(48)) == width * width * 2 && rd32(sc(56)) == width,
              "V8U8 descriptor preserves format and mip extent");
        check(call_method(bump, 16, {level, sc(8), 0, 0}) == 0 && rd32(sc(8)) == width * 2,
              "V8U8 LockRect uses two bytes per texel");
        wr16(rd32(sc(12)), 0x7f80);
        check(call_method(bump, 17, {level}) == 0, "unlock signed bump mip");
        check(call_method(bump, 16, {level, sc(8), 0, 0}) == 0 && rd16(rd32(sc(12))) == 0x7f80,
              "signed bump bytes survive unlock and relock");
        call_method(bump, 17, {level});
    }
    call_method(bump, 2);

    // An unrepresentable format fails without fabricating a texture. DXT1 is
    // representable now (compressed texture support), so use an unknown
    // FourCC here.
    wr32(sc(0), 0xfeedface);
    check(call_method(device2, 20, {4, 4, 1, 0, 0xfeedface, 2, sc(0)}) == 0x8876086c,
          "an unrepresentable texture format is rejected");
    check(rd32(sc(0)) == 0, "failed CreateTexture clears the output pointer");
    call_method(device2, 2);
    check(dev2->refs == 0, "all texture references release the device");
}

// RT texture CPU staging and descriptor contract; switching/lifetime is below.
static void test_render_target_texture() {
    cpu_reset();
    ComObj *dev = make_test_device(256, 256, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);

    check(call_method(device, 20, {256, 256, 1, 1, 22, 0, sc(0)}) == 0,
          "CreateTexture with D3DUSAGE_RENDERTARGET succeeds");
    uint32_t tex = rd32(sc(0));
    ComObj *t = tex ? com_this(tex, IF_D3D8TEXTURE8) : nullptr;
    check(t && t->d3d8_usage == 1 && t->d3d8_pool == 0,
          "render-target texture records usage and DEFAULT pool");
    if (!t)
        return;
    check(t->d3d8_level_count == 1, "render-target texture has one level");
    ComObj *level = com_get(t->d3d8_levels[0]);
    check(level && level->d3d8_usage == 1 && level->d3d8_texture == t->id,
          "level keeps the render-target usage and texture owner");
    check(call_method(tex, 14, {0, sc(32)}) == 0 && rd32(sc(32)) == 22 && rd32(sc(40)) == 1 &&
              rd32(sc(56)) == 256 && rd32(sc(60)) == 256,
          "level descriptor reports the render-target usage and size");

    // CPU storage and the normal staged lock path still work.
    check(call_method(tex, 16, {0, sc(8), 0, 0}) == 0, "render-target level LockRect succeeds");
    uint32_t pitch = rd32(sc(8)), ptr = rd32(sc(12));
    check(pitch == 256 * 4 && ptr != 0, "render-target level lock returns its pitch and bytes");
    wr32(ptr, 0x80112233);
    check(call_method(tex, 17, {0}) == 0, "render-target UnlockRect succeeds");
    check(call_method(tex, 16, {0, sc(8), 0, 0}) == 0 && rd32(rd32(sc(12))) == 0x80112233,
          "unlocked render-target bytes survive to the next lock");
    call_method(tex, 17, {0});

    // Sampling stays on the same bind path as a normal texture.
    check(call_method(device, 61, {0, tex}) == 0, "SetTexture accepts a render-target texture");
    check(dev->d3d8_bound_texture[0] == t->id, "render-target texture is bound on the device");
    check(call_method(tex, 15, {0, sc(16)}) == 0 && rd32(sc(16)) != 0,
          "GetSurfaceLevel returns the render-target level surface");
    call_method(rd32(sc(16)), 2);
    call_method(device, 61, {0, 0});

    // DEPTHSTENCIL is still refused without fabricating a texture.
    wr32(sc(0), 0xfeedface);
    check(call_method(device, 20, {256, 256, 1, 2, 22, 0, sc(0)}) == 0x8876086c,
          "CreateTexture D3DUSAGE_DEPTHSTENCIL is rejected");
    check(rd32(sc(0)) == 0, "failed depth-stencil CreateTexture clears the output");
    check(call_method(tex, 2) == 0, "release the render-target texture");
    call_method(device, 2);
}

// The autodepth handle path. Fixtures carry the depth fields CreateDevice
// would set; the actual depth bytes live in the Rust target and are exercised
// by the headless replay, not here.
static void test_depth() {
    cpu_reset();
    ComObj *dev = make_test_device(64, 48, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);

    // No autodepth: the request is refused and clears the output.
    wr32(sc(0), 0xfeedface);
    check(call_method(device, 33, {sc(0)}) == 0x88760866 && rd32(sc(0)) == 0,
          "GetDepthStencilSurface without autodepth is NOTFOUND");

    dev->d3d8_depth_format = 80; // D3DFMT_D16
    wr32(sc(0), 0xfeedface);
    check(call_method(device, 33, {sc(0)}) == 0, "GetDepthStencilSurface returns the depth handle");
    uint32_t depth = rd32(sc(0));
    check(depth != 0, "GetDepthStencilSurface writes a real interface");
    ComObj *d = depth ? com_this(depth, IF_D3D8SURFACE8) : nullptr;
    uint32_t depth_id = d ? d->id : 0;
    check(d && d->d3d8_depth, "depth surface is marked as depth");
    check(call_method(device, 33, {sc(4)}) == 0 && rd32(sc(4)) == depth,
          "repeated GetDepthStencilSurface preserves identity");
    check(d && d->refs == 3,
          "two GetDepthStencilSurface calls hold two guest references plus device ownership");
    call_method(depth, 2);

    // Descriptor: depth usage, D16 format, 2 bytes per texel.
    check(call_method(depth, 8, {sc(64)}) == 0, "depth GetDesc succeeds");
    check(rd32(sc(64)) == 80 && rd32(sc(72)) == 2 && rd32(sc(80)) == 64 * 48 * 2 &&
              rd32(sc(88)) == 64 && rd32(sc(92)) == 48,
          "depth descriptor format/usage/size/width/height");

    // SetRenderTarget accepts the implicit backbuffer and the depth handle.
    check(call_method(device, 16, {0, 0, sc(8)}) == 0, "GetBackBuffer for SetRenderTarget");
    uint32_t backbuffer = rd32(sc(8));
    check(call_method(device, 31, {backbuffer, depth}) == 0,
          "SetRenderTarget accepts backbuffer + depth");
    check(call_method(device, 31, {backbuffer, 0}) == 0,
          "SetRenderTarget accepts a NULL depth argument");
    check(call_method(device, 33, {sc(12)}) == 0x88760866 && rd32(sc(12)) == 0,
          "NULL depth detaches; GetDepthStencilSurface returns NOTFOUND");
    call_method(backbuffer, 2);
    check(call_method(depth, 2) == 1 && com_get(depth_id) == d,
          "device ownership keeps the depth surface alive after guest references are released");
    check(!d->d3d8_owner_retained && dev->refs == 1,
          "internal-only depth surface does not retain the device");
    call_method(device, 2);
    check(dev->refs == 0 && !com_get(depth_id),
          "device release destroys the device-owned depth surface");
}

// CreateDepthStencilSurface creates a standalone depth handle the guest can
// bind. It is a real D3D8 resource with its own lifetime; the renderer still
// uses the shared autodepth attachment (DIVERGENCE in dx/d3d8.cpp).
static void test_create_depth_stencil_surface() {
    cpu_reset();
    ComObj *dev = make_test_device(64, 48, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);

    // (this, Width, Height, Format, MultiSampleType, ppSurface). Slot 26.
    wr32(sc(0), 0xfeedface);
    check(call_method(device, 26, {32, 16, 77, 0, sc(0)}) == 0,
          "CreateDepthStencilSurface succeeds");
    uint32_t surface = rd32(sc(0));
    check(surface != 0, "CreateDepthStencilSurface writes a real interface");
    ComObj *depth = surface ? com_this(surface, IF_D3D8SURFACE8) : nullptr;
    uint32_t depth_id = depth ? depth->id : 0;
    check(depth && depth->d3d8_depth && depth->rmask == 77 && depth->width == 32 &&
              depth->height == 16,
          "standalone depth surface records its descriptor");
    check(depth && depth->d3d8_owner == dev->id && depth->d3d8_owner_retained && dev->refs == 2,
          "standalone depth surface retains its device");

    // Descriptor: D24X8, depth usage, 4 bytes per texel.
    check(call_method(surface, 8, {sc(64)}) == 0 && rd32(sc(64)) == 77 && rd32(sc(72)) == 2 &&
              rd32(sc(80)) == 32 * 16 * 4 && rd32(sc(88)) == 32 && rd32(sc(92)) == 16,
          "standalone depth descriptor");

    // It can bind as the depth argument of a render-target texture.
    check(call_method(device, 20, {32, 16, 1, 1, 22, 0, sc(8)}) == 0, "create RT texture");
    uint32_t texture = rd32(sc(8));
    check(call_method(texture, 15, {0, sc(12)}) == 0, "get RT level surface");
    uint32_t color = rd32(sc(12));
    check(call_method(device, 31, {color, surface}) == 0,
          "SetRenderTarget accepts a standalone depth surface");
    check(dev->d3d8_target_depth == depth_id, "binding records the standalone depth identity");
    call_method(color, 2);
    call_method(texture, 2);
    check(call_method(device, 31, {0, 0}) == 0, "detach the standalone depth");
    check(call_method(surface, 2) == 0, "releasing the standalone depth destroys it");
    check(!com_get(depth_id), "standalone depth is gone after its last reference");
    call_method(device, 2);
    check(dev->refs == 0, "device released after standalone depth");

    // Rejected cases: bad format, zero size and multisample.
    ComObj *dev2 = make_test_device(64, 48, 22);
    uint32_t device2 = com_view(dev2, IF_D3D8DEVICE);
    wr32(sc(0), 0xfeedface);
    check(call_method(device2, 26, {32, 16, 21, 0, sc(0)}) == 0x8876086c && rd32(sc(0)) == 0,
          "a color format is not a depth-stencil surface");
    check(call_method(device2, 26, {0, 16, 77, 0, sc(0)}) == 0x8876086c,
          "zero-width depth-stencil surface is rejected");
    check(call_method(device2, 26, {32, 16, 77, 2, sc(0)}) == 0x8876086c,
          "multisampled depth-stencil surface is rejected");
    call_method(device2, 2);
}

// The game's own sequence: GetRenderTarget/GetDepthStencilSurface, release the
// returned references, and later SetRenderTarget with the saved 32-bit
// pointers. Real D3D8 keeps the implicit surfaces alive because the device owns
// them; the bridge must not leave dangling handles.
static void test_implicit_surface_lifetime() {
    cpu_reset();
    ComObj *dev = make_test_device(64, 48, 22);
    dev->d3d8_depth_format = 77;
    uint32_t device = com_view(dev, IF_D3D8DEVICE);

    // __init_direct3d saves these two handles and releases its references.
    check(call_method(device, 32, {sc(0)}) == 0, "save the implicit render target");
    uint32_t saved_color = rd32(sc(0));
    check(call_method(device, 33, {sc(4)}) == 0, "save the implicit depth surface");
    uint32_t saved_depth = rd32(sc(4));
    check(call_method(saved_color, 2) == 1, "release the saved color reference");
    check(call_method(saved_depth, 2) == 1, "release the saved depth reference");

    // ensureLightingUpload then restores the saved pointers.
    check(call_method(device, 31, {saved_color, saved_depth}) == 0,
          "SetRenderTarget accepts the saved device-owned handles");
    check(dev->d3d8_target == com_this(saved_color, IF_D3D8SURFACE8)->id &&
              dev->d3d8_target_depth == com_this(saved_depth, IF_D3D8SURFACE8)->id,
          "restored handles resolve to the device-owned surfaces");
    check(dev->d3d8_backbuffer != 0 && dev->d3d8_depthbuffer != 0,
          "implicit surface caches were not cleared by the guest releases");
    check(call_method(device, 31, {0, 0}) == 0, "detach for teardown");
    call_method(device, 2);
    check(dev->refs == 0, "device release retires the implicit surfaces");
}

// Bound surface storage survives releasing the texture and temporary surface
// handle. Internal binding/parent refs must not keep the device alive in a cycle.
static void test_render_target_switching() {
    cpu_reset();
    ComObj *dev = make_test_device(64, 48, 22);
    dev->d3d8_depth_format = 71;
    uint32_t device = com_view(dev, IF_D3D8DEVICE);
    check(call_method(device, 32, {sc(0)}) == 0, "GetRenderTarget returns implicit color");
    uint32_t backbuffer = rd32(sc(0));
    check(call_method(device, 33, {sc(4)}) == 0, "GetDepthStencilSurface before texture pass");
    uint32_t depth = rd32(sc(4));
    check(call_method(device, 20, {16, 8, 1, 1, 21, 0, sc(8)}) == 0, "create small RT texture");
    uint32_t texture = rd32(sc(8));
    check(call_method(texture, 15, {0, sc(12)}) == 0, "get small RT surface");
    uint32_t surface = rd32(sc(12));
    ComObj *level = com_this(surface, IF_D3D8SURFACE8);
    const uint32_t level_id = level->id;
    check(call_method(device, 31, {surface, depth}) == 0, "bind texture with larger shared depth");
    check(dev->d3d8_target == level_id && dev->d3d8_target_depth == com_this(depth)->id,
          "binding records exact color/depth identities");
    call_method(surface, 2);
    call_method(texture, 2);
    check(com_get(level_id) && level->refs == 1 && level->internal_refs == 1,
          "binding keeps level alive after parent and external surface release");
    check(!level->d3d8_owner_retained, "internal-only level does not retain device in return");
    check(call_method(device, 32, {sc(16)}) == 0 && rd32(sc(16)) == surface,
          "GetRenderTarget retains the bound surface identity");
    check(level->d3d8_owner_retained, "external GetRenderTarget reference retains device");
    call_method(rd32(sc(16)), 2);
    check(call_method(device, 31, {0, 0}) == 0 && dev->d3d8_target == level_id,
          "NULL color retains the target; NULL depth detaches");
    check(call_method(device, 33, {sc(20)}) == 0x88760866 && !rd32(sc(20)),
          "detached depth is reported as NOTFOUND");
    check(call_method(device, 31, {backbuffer, depth}) == 0, "restore saved color/depth");
    check(!com_get(level_id), "restoring releases the last internal level reference");
    check(call_method(device, 32, {sc(24)}) == 0 && rd32(sc(24)) == backbuffer,
          "GetRenderTarget returns restored backbuffer");
    call_method(rd32(sc(24)), 2);
    call_method(backbuffer, 2);
    call_method(depth, 2);
    const uint32_t id = dev->id;
    call_method(device, 2);
    check(!com_get(id), "final device Release tears down internal bindings without a cycle");
}

// Vertex and index buffers: CPU-backed storage with a staged guest lock. No
// renderer is involved; the draw path that consumes the bytes is exercised by
// the headless replay.
static void test_buffers() {
    cpu_reset();
    ComObj *dev = make_test_device(64, 48, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);

    // CreateVertexBuffer: Length, Usage, FVF, Pool, ppVertexBuffer.
    check(call_method(device, 23, {96, 0x208, 0x142, 0, sc(0)}) == 0,
          "CreateVertexBuffer succeeds");
    uint32_t vb = rd32(sc(0));
    ComObj *v = vb ? com_this(vb, IF_D3D8VERTEXBUFFER8) : nullptr;
    check(v && v->kind == K_D3D8VERTEXBUFFER, "vertex buffer view has its own COM identity");
    if (!v)
        return;
    check(v->pixels_bytes == 96, "vertex buffer storage is the requested length");

    const uint8_t vb_arities[] = {3, 1, 1, 2, 5, 4, 2, 2, 1, 1, 1, 5, 1, 2};
    for (uint32_t slot = 0; slot < sizeof(vb_arities); ++slot)
        check(imports_argc(rd32(rd32(vb) + 4 * slot)) == vb_arities[slot],
              "vertex buffer vtable slot arity");

    check(call_method(vb, 10, {}) == 6, "vertex buffer GetType is D3DRTYPE_VERTEXBUFFER");
    check(call_method(vb, 13, {sc(32)}) == 0, "vertex buffer GetDesc succeeds");
    check(rd32(sc(32)) == 100 && rd32(sc(36)) == 6 && rd32(sc(40)) == 0x208 && rd32(sc(44)) == 0 &&
              rd32(sc(48)) == 96 && rd32(sc(52)) == 0x142,
          "D3DVERTEXBUFFER_DESC fields");

    // Lock writes into guest heap; Unlock copies back. Locking to the end with
    // size 0 and a sub-range both return the staged block plus the offset.
    check(call_method(vb, 11, {0, 0, sc(8), 0}) == 0, "vertex buffer Lock succeeds");
    uint32_t base = rd32(sc(8));
    check(base != 0 && heap_size(base) != 0xffffffff, "Lock returns live guest storage");
    wr32(base, 0x11223344);
    check(call_method(vb, 12, {}) == 0, "vertex buffer Unlock succeeds");
    check(call_method(vb, 11, {0, 0, sc(8), 0}) == 0 && rd32(rd32(sc(8))) == 0x11223344,
          "unlocked vertex bytes survive to the next lock");
    call_method(vb, 12, {});
    check(call_method(vb, 11, {12, 4, sc(8), 0}) == 0 && rd32(sc(8)) == base + 12,
          "a sub-range lock returns base + offset");
    call_method(vb, 12, {});
    // The engine's LockVB passes an end vertex as SizeToLock, so a size that
    // runs past the buffer is valid while the offset is inside it. Only an
    // offset past the end is rejected.
    check(call_method(vb, 11, {92, 8, sc(8), 0}) == 0 && rd32(sc(8)) == base + 92,
          "an over-large SizeToLock is accepted when the offset is in range");
    call_method(vb, 12, {});
    check(call_method(vb, 11, {97, 4, sc(8), 0}) == 0x8876086c,
          "an offset past the buffer end is rejected");
    check(call_method(vb, 11, {0, 0, 0, 0}) == 0x8876086c, "null Lock output is rejected");

    // Destroying a still-locked buffer must free its staging block.
    check(call_method(device, 23, {16, 8, 0x142, 0, sc(24)}) == 0,
          "CreateVertexBuffer for lock-destroy");
    uint32_t locked_vb = rd32(sc(24));
    check(call_method(locked_vb, 11, {0, 0, sc(8), 0}) == 0, "lock the buffer");
    uint32_t staged = rd32(sc(8));
    check(heap_size(staged) != 0xffffffff, "staging is live while locked");
    check(call_method(locked_vb, 2) == 0, "release the locked buffer");
    check(heap_size(staged) == 0xffffffff, "destroying a locked buffer frees its staging block");

    // CreateIndexBuffer: Length, Usage, Format, Pool, ppIndexBuffer.
    check(call_method(device, 24, {48, 8, 102, 0, sc(16)}) == 0, "CreateIndexBuffer succeeds");
    uint32_t ib = rd32(sc(16));
    ComObj *i = ib ? com_this(ib, IF_D3D8INDEXBUFFER8) : nullptr;
    check(i && i->kind == K_D3D8INDEXBUFFER, "index buffer view has its own COM identity");
    if (!i)
        return;
    check(call_method(ib, 10, {}) == 7, "index buffer GetType is D3DRTYPE_INDEXBUFFER");
    check(call_method(ib, 13, {sc(64)}) == 0 && rd32(sc(64)) == 102 && rd32(sc(68)) == 7 &&
              rd32(sc(72)) == 8 && rd32(sc(80)) == 48,
          "D3DINDEXBUFFER_DESC reports the index format and size");
    check(call_method(device, 24, {48, 0, 99, 0, sc(16)}) == 0x8876086c,
          "an unknown index format is rejected");

    // SetStreamSource/SetIndices bind weakly; releasing the buffer clears the
    // device binding so a later draw cannot resolve a dead id.
    check(call_method(device, 83, {0, vb, 24}) == 0, "SetStreamSource accepts stream 0");
    check(dev->d3d8_stream_vb == v->id && dev->d3d8_stream_stride == 24,
          "stream source is stored on the device");
    check(call_method(device, 83, {1, vb, 24}) == 0x8876086c, "a second stream is rejected");
    check(call_method(device, 85, {ib, 3}) == 0, "SetIndices stores the index buffer");
    check(dev->d3d8_indices == i->id && dev->d3d8_base_vertex == 3, "base vertex is stored");
    check(call_method(vb, 2) == 0, "release the vertex buffer destroys it");
    check(dev->d3d8_stream_vb == 0, "destroying the vertex buffer clears the stream binding");
    check(call_method(ib, 2) == 0, "release the index buffer destroys it");
    check(dev->d3d8_indices == 0, "destroying the index buffer clears the indices binding");
    check(call_method(device, 2) == 0, "device released after the buffers");
}

// Texture binding and reference lifetime. The renderer's sampling is exercised
// by the headless replay; this is the guest COM identity/lifetime contract.
static void test_bind_texture() {
    cpu_reset();
    ComObj *dev = make_test_device(8, 4, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);
    check(call_method(device, 20, {8, 4, 1, 0, 22, 2, sc(0)}) == 0, "texture A created");
    uint32_t a = rd32(sc(0));
    check(call_method(device, 20, {8, 4, 1, 0, 22, 2, sc(4)}) == 0, "texture B created");
    uint32_t b = rd32(sc(4));
    ComObj *ta = com_this(a, IF_D3D8TEXTURE8);
    ComObj *tb = com_this(b, IF_D3D8TEXTURE8);
    if (!ta || !tb)
        return;

    // SetTexture binds stage 0; GetTexture returns the same view with a new ref.
    check(call_method(device, 61, {0, a}) == 0, "SetTexture binds stage 0");
    check(dev->d3d8_bound_texture[0] == ta->id, "device stores the bound texture");
    check(call_method(device, 60, {0, sc(8)}) == 0 && rd32(sc(8)) == a,
          "GetTexture returns the bound view");
    call_method(rd32(sc(8)), 2);

    // Replacing the binding drops the device's reference to A. With the
    // caller's reference released below, A is destroyed.
    check(call_method(device, 61, {0, b}) == 0, "SetTexture replaces the binding");
    check(dev->d3d8_bound_texture[0] == tb->id, "device stores the replacement");
    check(call_method(a, 2) == 0, "replaced texture is released by the device");
    check(!com_get(ta->id), "replaced texture is destroyed");

    // A null binding releases and clears the slot, and GetTexture reports null.
    wr32(sc(8), 0xfeedface);
    check(call_method(device, 61, {0, 0}) == 0, "SetTexture(NULL) unbinds");
    check(dev->d3d8_bound_texture[0] == 0 && call_method(device, 60, {0, sc(8)}) == 0 &&
              rd32(sc(8)) == 0,
          "unbound stage is cleared and GetTexture returns null");
    check(call_method(b, 2) == 0, "unbound texture is released by the device");
    check(!com_get(tb->id), "unbound texture is destroyed");

    // Stage 8 is outside D3D8's eight stages; a non-texture view is invalid.
    check(call_method(device, 61, {8, 0}) == 0x8876086c, "stage 8 is rejected");
    check(call_method(device, 61, {0, a}) == 0x8876086c, "a dead texture view is rejected");
    call_method(device, 2);
    check(dev->refs == 0, "device released after the binding test");
}

// D3D8 stores programmable shader constants on the device and returns the
// same bits. The bridge must not round-trip them through float, and a range
// or guest-pointer violation is an ordinary D3DERR_INVALIDCALL error, not an
// unsupported-import abort: the water path writes a constant while no
// programmable shader is bound, which real D3D8 permits and which has no
// effect on fixed-function draws. Fixture devices only; no GPU work.
static void test_shader_constants() {
    cpu_reset();
    ComObj *dev = make_test_device(4, 4, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);
    const uint32_t set_vs = 79, get_vs = 80, set_ps = 91, get_ps = 92;
    const uint32_t in = sc(0x200), out = sc(0x300);

    // Bit patterns a float conversion would lose: quiet and signalling NaNs,
    // both infinities, negative zero, subnormals and ordinary values.
    const uint32_t bits[8][4] = {
        {0x7fc00000u, 0xffc00000u, 0x7f800000u, 0xff800000u},
        {0x80000000u, 0x00000001u, 0x007fffffu, 0x3f800000u},
        {0xdeadbeefu, 0xffffffffu, 0x12345678u, 0xcdcdcdcdu},
        {0x00000000u, 0x7f7fffffu, 0xff7fffffu, 0x40000000u},
        {0x00000001u, 0x80000001u, 0x00800000u, 0x80800000u},
        {0x3eaaaaabu, 0xbeaaaaabu, 0x4b000000u, 0xcb000000u},
        {0x7f000000u, 0xff000000u, 0x00ffffffu, 0x7fffffffu},
        {0x80000000u, 0x7fc00001u, 0xffc00001u, 0x00000000u},
    };
    for (uint32_t i = 0; i < 8; ++i)
        for (uint32_t k = 0; k < 4; ++k)
            wr32(in + (i * 4 + k) * 4, bits[i][k]);

    // The water path's call shape: one vertex constant. Set and Get are
    // separate dispatches, so the bank must survive between calls.
    check(call_method(device, set_vs, {7, in, 1}) == 0, "vertex constant register 7 is accepted");
    wr32(out, 0xfeedface);
    check(call_method(device, get_vs, {7, out, 1}) == 0, "GetVertexShaderConstant succeeds");
    for (uint32_t k = 0; k < 4; ++k)
        check(rd32(out + k * 4) == bits[0][k], "vertex constant bit-exact across calls");

    // The pixel bank is separate; writing it leaves the vertex bank alone.
    check(call_method(device, set_ps, {7, in, 1}) == 0, "pixel constant register 7 is accepted");
    wr32(out, 0xfeedface);
    check(call_method(device, get_ps, {7, out, 1}) == 0 && rd32(out) == bits[0][0],
          "pixel constant is bit-exact");
    check(call_method(device, get_vs, {7, out, 1}) == 0 && rd32(out) == bits[0][0],
          "a pixel write leaves the vertex bank alone");

    // A block fill and read-back of the whole vs.1.1 bank.
    check(call_method(device, set_vs, {0, in, 8}) == 0, "eight vertex constants are accepted");
    memset(gm_ptr(out), 0, 128);
    check(call_method(device, get_vs, {0, out, 8}) == 0, "GetVertexShaderConstant reads a block");
    for (uint32_t i = 0; i < 8; ++i)
        for (uint32_t k = 0; k < 4; ++k)
            check(rd32(out + (i * 4 + k) * 4) == bits[i][k], "vertex constant block bit-exact");

    // Bounds: 96 vertex and 8 pixel registers, addressed as [reg, reg + count).
    check(call_method(device, set_vs, {0, in, 96}) == 0, "the whole 96-register bank is writable");
    check(call_method(device, set_vs, {96, in, 1}) == 0x8876086c, "register 96 is rejected");
    check(call_method(device, set_vs, {95, in, 2}) == 0x8876086c,
          "a range past register 95 is rejected");
    check(call_method(device, set_vs, {0, in, 97}) == 0x8876086c, "97 registers are rejected");
    check(call_method(device, set_vs, {1, in, 96}) == 0x8876086c,
          "a shifted 96-register block is rejected");
    check(call_method(device, get_vs, {96, in, 1}) == 0x8876086c, "Get rejects register 96");
    check(call_method(device, get_vs, {0, in, 97}) == 0x8876086c, "Get rejects 97 registers");
    check(call_method(device, set_ps, {8, in, 1}) == 0x8876086c, "pixel register 8 is rejected");
    check(call_method(device, set_ps, {0, in, 9}) == 0x8876086c,
          "nine pixel registers are rejected");
    check(call_method(device, get_ps, {7, in, 2}) == 0x8876086c,
          "Get rejects a pixel range past 7");

    // Count 0 copies nothing and touches no register, so a null pointer is
    // harmless; a non-zero count needs a live guest buffer.
    check(call_method(device, set_vs, {0, 0, 0}) == 0, "a zero-count Set is a no-op");
    check(call_method(device, set_vs, {96, 0, 0}) == 0, "a zero-count Set ignores the register");
    check(call_method(device, get_ps, {0, 0, 0}) == 0, "a zero-count Get is a no-op");
    check(call_method(device, set_vs, {0, 0, 1}) == 0x8876086c, "a null Set pointer is rejected");
    check(call_method(device, get_vs, {0, 0, 1}) == 0x8876086c, "a null Get pointer is rejected");
    check(call_method(device, set_vs, {0, GUEST_SIZE - 8, 1}) == 0x8876086c,
          "a truncated Set pointer is rejected");
    check(call_method(device, get_ps, {7, GUEST_SIZE - 8, 1}) == 0x8876086c,
          "a truncated Get pointer is rejected");
    wr32(out, 0xfeedface);
    check(call_method(device, get_vs, {96, out, 1}) == 0x8876086c && rd32(out) == 0xfeedface,
          "a rejected Get writes no output");

    // Per-device state: a second device starts at zero.
    ComObj *dev2 = make_test_device(4, 4, 22);
    uint32_t device2 = com_view(dev2, IF_D3D8DEVICE);
    wr32(out, 0xfeedface);
    check(call_method(device2, get_vs, {7, out, 1}) == 0 && rd32(out) == 0,
          "a second device has its own zeroed constant bank");
    call_method(device2, 2);
    call_method(device, 2);
}

// Seed host fixture programs to test guest COM ownership and query semantics
// without a GPU. Real Create*Shader execution is covered by the game startup;
// token translation and rendering are covered by the Rust shader tests.
static void test_shader_lifecycle() {
    cpu_reset();
    ComObj *dev = make_test_device(4, 4, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE), out = sc(0x200), size = sc(0x300);
    dev->d3d8_constants = std::make_shared<D3d8DeviceState>();
    const uint32_t vs = 0xf0000001u, ps = 0xf0000002u;
    D3d8Shader vertex;
    vertex.declaration = {0x20000000, 0x40020000, 0xffffffff};
    vertex.function = {0xfffe0101, 1, 0xc00f0000, 0x90e40000, 0xffff};
    D3d8Shader pixel;
    pixel.pixel = true;
    pixel.function = {0xffff0101, 1, 0x800f0000, 0xa0e40000, 0xffff};
    dev->d3d8_constants->shaders.emplace(vs, vertex);
    dev->d3d8_constants->shaders.emplace(ps, pixel);
    check(call_method(device, 76, {vs}) == 0, "bind fixture vertex shader");
    check(call_method(device, 77, {out}) == 0 && rd32(out) == vs,
          "vertex shader handle round trip");
    check(call_method(device, 88, {ps}) == 0, "bind fixture pixel shader");
    check(call_method(device, 89, {out}) == 0 && rd32(out) == ps, "pixel shader handle round trip");
    check(call_method(device, 76, {ps}) == 0x8876086c, "pixel handle rejected as vertex shader");
    check(call_method(device, 88, {vs}) == 0x8876086c, "vertex handle rejected as pixel shader");
    check(call_method(device, 82, {vs, 0, size}) == 0 && rd32(size) == 20,
          "vertex bytecode size query");
    wr32(size, 4);
    wr32(out, 0xdeadbeef);
    check(call_method(device, 82, {vs, out, size}) == 0x88760867,
          "short bytecode query reports MOREDATA");
    check(rd32(size) == 20 && rd32(out) == 0xdeadbeef, "short query updates size without copying");
    check(call_method(device, 82, {vs, out, size}) == 0, "vertex bytecode copy succeeds");
    for (uint32_t i = 0; i < vertex.function.size(); ++i)
        check(rd32(out + i * 4) == vertex.function[i], "vertex bytecode copy preserves tokens");
    check(call_method(device, 81, {vs, 0, size}) == 0 && rd32(size) == 12,
          "vertex declaration size query");
    check(call_method(device, 93, {ps, 0, size}) == 0 && rd32(size) == 20,
          "pixel bytecode size query");
    check(call_method(device, 82, {ps, 0, size}) == 0x8876086c, "wrong shader type query rejected");
    ComObj *other = make_test_device(4, 4, 22);
    check(call_method(com_view(other, IF_D3D8DEVICE), 88, {ps}) == 0x8876086c,
          "shader handles belong to their device");
    check(call_method(device, 78, {vs}) == 0, "delete bound vertex shader");
    check(call_method(device, 77, {out}) == 0 && rd32(out) == 0, "vertex deletion clears binding");
    check(call_method(device, 90, {ps}) == 0, "delete bound pixel shader");
    check(call_method(device, 89, {out}) == 0 && rd32(out) == 0, "pixel deletion clears binding");
    check(call_method(device, 88, {ps}) == 0x8876086c, "deleted shader cannot be rebound");
    check(call_method(device, 90, {ps}) == 0x8876086c, "double shader deletion rejected");
    check(call_method(device, 76, {0x112}) == 0, "FVF restores fixed-function vertex stage");
    check(call_method(device, 77, {out}) == 0 && rd32(out) == 0x112, "FVF handle round trip");
    call_method(com_view(other, IF_D3D8DEVICE), 2);
    call_method(device, 2);
}

// D3DCAPS8 limits and the broader HAL surface. Ghost Recon's 0x004eac20
// halves every texture until it fits MaxTextureWidth/Height, so a zero there
// silently collapses all sampled textures to 1x1. The rest of the table pins
// the fields write_caps advertises so a change to the bridge's fixed-function
// slice has to update this contract deliberately.
static void test_caps() {
    cpu_reset();
    ComObj *dev = make_test_device(4, 4, 22);
    uint32_t device = com_view(dev, IF_D3D8DEVICE);
    uint32_t caps = sc(0x100);
    for (uint32_t i = 0; i < 212; i += 4)
        wr32(caps + i, 0xcdcdcdcd);
    check(call_method(device, 7, {caps}) == 0, "GetDeviceCaps succeeds");
    check(rd32(caps) == 1, "device type is D3DDEVTYPE_HAL");

    // Exact DWORDs. Every field the shim sets to a single value is pinned here.
    struct CapsField {
        const char *name;
        uint32_t off;
        uint32_t value;
    };
    static const CapsField fields[] = {
        {"Caps2", 0x0c, 0x30080000u},                 // windowed + managed + dynamic
        {"PresentationIntervals", 0x14, 0x80000000u}, // IMMEDIATE only
        {"ZCmpCaps", 0x28, 0x000000ffu},              // all eight compare funcs
        {"SrcBlendCaps", 0x2c, 0x000007ffu},          // no BOTH* variants
        {"DestBlendCaps", 0x30, 0x000007ffu},         // no BOTH* variants
        {"AlphaCmpCaps", 0x34, 0x000000ffu},          // all eight compare funcs
        {"ShadeCaps", 0x38, 0x00084208u},             // gouraud color/specular/alpha/fog
        {"TextureCaps", 0x3c, 0x00004405u},           // perspective/alpha/projected/mipmap
        {"TextureFilterCaps", 0x40, 0x07030700u},     // point/linear/aniso + mips
        {"TextureAddressCaps", 0x4c, 0x0000001fu},    // wrap/mirror/clamp/border/indep UV
        {"MaxTextureWidth", 0x58, 2048u},
        {"MaxTextureHeight", 0x5c, 2048u},
        {"MaxAnisotropy", 0x6c, 1u},          // anisotropic is linear
        {"FVFCaps", 0x8c, 2u},                // two texcoord sets
        {"TextureOpCaps", 0x90, 0x03feffffu}, // includes DOTPRODUCT3
        {"MaxTextureBlendStages", 0x94, 2u},
        {"MaxSimultaneousTextures", 0x98, 2u},
        {"VertexProcessingCaps", 0x9c, 0x000000bbu},
        {"MaxActiveLights", 0xa0, 8u},
        {"MaxPrimitiveCount", 0xb4, 0x00555555u},
        {"MaxVertexIndex", 0xb8, 0x0000ffffu},
        {"MaxStreams", 0xbc, 1u},
        {"MaxStreamStride", 0xc0, 508u},
        {"VertexShaderVersion", 0xc4, 0u},
        {"MaxVertexShaderConst", 0xc8, 0u},
        {"PixelShaderVersion", 0xcc, 0u},
        {"MaxPixelShaderValue", 0xd0, 0u},
        {"MaxUserClipPlanes", 0xa4, 0u},
        {"MaxVertexBlendMatrices", 0xa8, 0u},
        {"MaxVertexBlendMatrixIndex", 0xac, 0u},
    };
    for (const CapsField &field : fields)
        check(rd32(caps + field.off) == field.value, field.name);

    // Fields a period HAL reports but the shim must leave zero because the
    // bridge has no equivalent (or no source of the value at all).
    static const struct {
        const char *name;
        uint32_t off;
    } zeroes[] = {
        {"Caps", 0x08},
        {"Caps3", 0x10},
        {"CursorCaps", 0x18},
        {"CubeTextureFilterCaps", 0x44},
        {"VolumeTextureFilterCaps", 0x48},
        {"VolumeTextureAddressCaps", 0x50},
        {"LineCaps", 0x54},
        {"MaxVolumeExtent", 0x60},
        {"MaxTextureRepeat", 0x64},
        {"MaxTextureAspectRatio", 0x68},
        {"MaxVertexW", 0x70},
        {"GuardBandLeft", 0x74},
        {"GuardBandTop", 0x78},
        {"GuardBandRight", 0x7c},
        {"GuardBandBottom", 0x80},
        {"ExtentsAdjust", 0x84},
        {"StencilCaps", 0x88},
        {"MaxPointSize", 0xb0},
    };
    for (const auto &field : zeroes)
        check(rd32(caps + field.off) == 0, field.name);

    // DevCaps bits the draw pipeline can keep.
    const uint32_t devcaps = rd32(caps + 0x1c);
    check((devcaps & 0x00000010u) != 0, "DevCaps EXECUTESYSTEMMEMORY");
    check((devcaps & 0x00000040u) != 0, "DevCaps TLVERTEXSYSTEMMEMORY");
    check((devcaps & 0x00000100u) != 0, "DevCaps TEXTURESYSTEMMEMORY");
    check((devcaps & 0x00000200u) != 0, "DevCaps TEXTUREVIDEOMEMORY");
    check((devcaps & 0x00000400u) != 0, "DevCaps DRAWPRIMTLVERTEX");
    check((devcaps & 0x00001000u) != 0, "DevCaps TEXTURENONLOCALVIDMEM");
    check((devcaps & 0x00002000u) != 0, "DevCaps DRAWPRIMITIVES2");
    check((devcaps & 0x00008000u) != 0, "DevCaps DRAWPRIMITIVES2EX");
    check((devcaps & D3DDEVCAPS_HWTRANSFORMANDLIGHT) != 0,
          "DevCaps advertises hardware transform and lighting");
    check((devcaps & 0x00080000u) != 0, "DevCaps HWRASTERIZATION");
    check((devcaps & 0x00000020u) == 0, "DevCaps EXECUTEVIDEOMEMORY stays clear");
    check((devcaps & 0x00000080u) == 0, "DevCaps TLVERTEXVIDEOMEMORY stays clear");
    check((devcaps & 0x00000800u) == 0, "DevCaps CANRENDERAFTERFLIP stays clear");
    check((devcaps & 0x00004000u) == 0, "DevCaps SEPARATETEXTUREMEMORIES stays clear");
    check((devcaps & 0x00020000u) == 0, "DevCaps CANBLTSYSTONONLOCAL stays clear");
    check((devcaps & 0x00100000u) == 0, "DevCaps PUREDEVICE stays clear");

    // PrimitiveMiscCaps: cull modes, maskz, color-write enable, blend op.
    const uint32_t misc = rd32(caps + 0x20);
    check((misc & 0x00000002u) != 0, "PrimitiveMiscCaps MASKZ");
    check((misc & 0x00000010u) != 0, "PrimitiveMiscCaps CULLNONE");
    check((misc & 0x00000020u) != 0, "PrimitiveMiscCaps CULLCW");
    check((misc & 0x00000040u) != 0, "PrimitiveMiscCaps CULLCCW");
    check((misc & 0x00000080u) != 0, "PrimitiveMiscCaps COLORWRITEENABLE");
    check((misc & 0x00000200u) != 0, "PrimitiveMiscCaps CLIPTLVERTS");
    check((misc & 0x00000800u) != 0, "PrimitiveMiscCaps BLENDOP");
    check((misc & 0x00000400u) == 0, "PrimitiveMiscCaps TSSARGTEMP stays clear");

    // RasterCaps: depth test, both fog paths, LOD bias, ZBIAS, aniso, perspective.
    const uint32_t raster = rd32(caps + 0x24);
    check((raster & 0x00000001u) != 0, "RasterCaps DITHER");
    check((raster & 0x00000010u) != 0, "RasterCaps ZTEST");
    check((raster & 0x00000080u) != 0, "RasterCaps FOGVERTEX");
    check((raster & 0x00000100u) != 0, "RasterCaps FOGTABLE");
    check((raster & 0x00002000u) != 0, "RasterCaps MIPMAPLODBIAS");
    check((raster & 0x00004000u) != 0, "RasterCaps ZBIAS");
    check((raster & 0x00010000u) != 0, "RasterCaps FOGRANGE");
    check((raster & 0x00020000u) != 0, "RasterCaps ANISOTROPY");
    check((raster & 0x00400000u) != 0, "RasterCaps COLORPERSPECTIVE");
    check((raster & 0x00100000u) == 0, "RasterCaps WFOG stays clear");
    check((raster & 0x00200000u) == 0, "RasterCaps ZFOG stays clear");

    // Blend caps: BOTHSRCALPHA/BOTHINVSRCALPHA are not representable in wgpu.
    check((rd32(caps + 0x2c) & 0x00001800u) == 0, "SrcBlendCaps omits BOTH* variants");
    check((rd32(caps + 0x30) & 0x00001800u) == 0, "DestBlendCaps omits BOTH* variants");
    check((rd32(caps + 0x34) & 0x00000040u) != 0,
          "AlphaCmpCaps advertises D3DPCMPCAPS_GREATEREQUAL");

    // Texture caps and filters: no cube/volume, no cubic filter, no mirror-once.
    check((rd32(caps + 0x3c) & 0x00000800u) == 0, "TextureCaps omits CUBEMAP");
    check((rd32(caps + 0x3c) & 0x00002000u) == 0, "TextureCaps omits VOLUMEMAP");
    check((rd32(caps + 0x40) & 0x08000000u) == 0, "TextureFilterCaps omits cubic");
    check((rd32(caps + 0x4c) & 0x00000020u) == 0, "TextureAddressCaps omits MIRRORONCE");

    // Vertex processing: TEXGEN and all three light types, but no tweening.
    const uint32_t vpc = rd32(caps + 0x9c);
    check((vpc & 0x00000001u) != 0, "VertexProcessingCaps TEXGEN");
    check((vpc & 0x00000008u) != 0, "VertexProcessingCaps DIRECTIONALLIGHTS");
    check((vpc & 0x00000010u) != 0, "VertexProcessingCaps POSITIONALLIGHTS");
    check((vpc & 0x00000040u) == 0, "VertexProcessingCaps omits TWEENING");

    // TextureOpCaps must advertise at least DOTPRODUCT3: a guest may use it as
    // its compressed-texture capability gate, so a zero value disables DXT
    // textures even when CheckDeviceFormat accepts them.
    check((rd32(caps + 0x90) & 0x00800000u) != 0, "TextureOpCaps advertises DOTPRODUCT3");
    check(call_method(device, 7, {0}) == 0x8876086c, "null caps output is rejected");
    call_method(device, 2);
}

int main(int argc, char **argv) {
    mem_init();
    imports_init();
    dx_register_shims();
    g_stack_top = STACK_TOP - 0x1000;
    g_scratch = heap_alloc(0x1000, true, 16);
    if (!g_scratch)
        return 1;
    if (argc == 2 && strcmp(argv[1], "--unsupported-lock") == 0) {
        cpu_reset();
        ComObj *surface = com_new(K_D3D8SURFACE);
        call_method(com_view(surface, IF_D3D8SURFACE8), 9, {sc(0), 0, 0});
        return 1; // The unsupported call must abort, even with RECOMP_LOG=0.
    }
    if (argc == 2 && strcmp(argv[1], "--unsupported-rendertarget") == 0) {
        cpu_reset();
        ComObj *dev = make_test_device(4, 4, 22);
        // A depth surface is not a valid render target; the probe must abort
        // by name rather than silently redirecting the implicit target.
        ComObj *depth = com_new(K_D3D8SURFACE);
        depth->d3d8_owner = dev->id;
        depth->d3d8_depth = true;
        call_method(com_view(dev, IF_D3D8DEVICE), 31, {com_view(depth, IF_D3D8SURFACE8), 0});
        return 1;
    }
    if (argc == 2 && strcmp(argv[1], "--unsupported-rendertarget-texture") == 0) {
        cpu_reset();
        ComObj *dev = make_test_device(256, 256, 22);
        uint32_t device = com_view(dev, IF_D3D8DEVICE);
        // Matching owner does not make a non-RT texture bindable.
        check(call_method(device, 20, {256, 256, 1, 0, 22, 0, sc(0)}) == 0,
              "probe: create texture without RT usage");
        uint32_t tex = rd32(sc(0));
        check(call_method(tex, 15, {0, sc(4)}) == 0, "probe: GetSurfaceLevel");
        call_method(device, 31, {rd32(sc(4)), 0});
        return 1;
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
    test_device_fpu();
    test_backbuffer(21); // A8R8G8B8
    test_backbuffer(22); // X8R8G8B8
    test_texture();
    test_render_target_texture();
    test_bind_texture();
    test_shader_constants();
    test_shader_lifecycle();
    test_caps();
    test_depth();
    test_create_depth_stencil_surface();
    test_implicit_surface_lifetime();
    test_render_target_switching();
    test_buffers();
    // Live locked CPU resources must be retired when mem_init discards guest
    // allocations. Releasing old guest staging during reset would be invalid.
    ComObj *reset_device = make_test_device(4, 4, 22);
    uint32_t reset_view = com_view(reset_device, IF_D3D8DEVICE);
    check(call_method(reset_view, 23, {32, 0, 0x42, 0, sc(0)}) == 0,
          "create live Rust-backed buffer for generation reset");
    check(call_method(rd32(sc(0)), 11, {0, 0, sc(8), 0}) == 0,
          "lock live buffer before generation reset");
    check(call_method(reset_view, 20, {4, 4, 0, 0, 21, 2, sc(16)}) == 0,
          "create live mip storage for generation reset");
    // Reset follows the runtime's generation order: old guest heap first,
    // then module state and COM vtables. No stale weak cache may survive.
    mem_init();
    imports_init();
    dx_reset();
    g_scratch = heap_alloc(0x1000, true, 16);
    test_backbuffer(22);
    test_buffers();
    printf("d3d8 surface: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
