// Guest DirectDraw mip locks -> D3D7 SetTexture/Draw -> actual GPU filtering.
#include "../dx.h"
#include "../../runtime/memory.h"
#include "guest_abi.h"
#include "d3d8_abi.h"
#include <cmath>

static void check(bool value, const char *message) {
    ++g_checks;
    if (!value) {
        ++g_failures;
        fprintf(stderr, "FAIL: %s\n", message);
    }
}

int main() {
    mem_init();
    imports_init();
    dx_register_shims();
    g_stack_top = STACK_TOP - 0x1000;
    cpu_reset();
    uint32_t scratch = heap_alloc(4096, true, 16), out = scratch + 256;
    const uint8_t dd7[16] = {0xc0, 0x5e, 0xe6, 0x15, 0x9c, 0x3b, 0xd2, 0x11,
                             0xb9, 0x2f, 0,    0x60, 0x97, 0x97, 0xea, 0x5b};
    const uint8_t d3d7[16] = {0x77, 0x9e, 4, 0xf5, 0x61, 0x48, 0xd2, 0x11,
                              0xa4, 7,    0, 0xa0, 0xc9, 6,    0x29, 0xa8};
    const uint8_t hal[16] = {0xe0, 0x3d, 0xe6, 0x84, 0xaa, 0x46, 0xcf, 0x11,
                             0x81, 0x6f, 0,    0,    0xc0, 0x20, 0x15, 0x6e};
    memcpy(gm_ptr(scratch), dd7, 16);
    check(call_shim(tramp("DDRAW.dll", "DirectDrawCreateEx"), {0, out, scratch, 0}) == DD_OK,
          "DirectDraw7");
    uint32_t dd = rd32(out);
    memcpy(gm_ptr(scratch), d3d7, 16);
    check(call_method(dd, 0, {scratch, out}) == S_OK, "Direct3D7");
    uint32_t d3d = rd32(out), desc = scratch + 512;
    auto make = [&](uint32_t size, uint32_t caps) {
        gm_zero(desc, DDSD2_SIZE);
        wr32(desc, DDSD2_SIZE);
        wr32(desc + DDSD_OFF_dwFlags, DDSD_CAPS | DDSD_WIDTH | DDSD_HEIGHT | DDSD_PIXELFORMAT);
        wr32(desc + DDSD_OFF_dwWidth, size);
        wr32(desc + DDSD_OFF_dwHeight, size);
        wr32(desc + DDSD_OFF_ddsCaps, caps);
        uint32_t pf = desc + DDSD_OFF_ddpfPixelFormat;
        wr32(pf, DDPF_SIZE);
        wr32(pf + DDPF_OFF_dwFlags, DDPF_RGB | DDPF_ALPHAPIXELS);
        wr32(pf + DDPF_OFF_dwRGBBitCount, 32);
        wr32(pf + DDPF_OFF_dwRBitMask, 0xff0000);
        wr32(pf + DDPF_OFF_dwGBitMask, 0xff00);
        wr32(pf + DDPF_OFF_dwBBitMask, 0xff);
        wr32(pf + DDPF_OFF_dwRGBAlphaBitMask, 0xff000000);
        check(call_method(dd, 6, {desc, out, 0}) == DD_OK, "CreateSurface");
        return rd32(out);
    };
    uint32_t target = make(16, DDSCAPS_3DDEVICE | DDSCAPS_OFFSCREENPLAIN | DDSCAPS_SYSTEMMEMORY);
    memcpy(gm_ptr(scratch), hal, 16);
    check(call_method(d3d, 4, {scratch, target, out}) == DD_OK, "CreateDevice");
    uint32_t dev = rd32(out);
    check(call_method(dev, 3, {desc}) == DD_OK, "GetCaps");
    check((rd32(desc + D3DDD7_OFF_dpcTriCaps + D3DPC_OFF_dwTextureCaps) & D3DPTEXTURECAPS_MIPMAP) !=
              0,
          "advertise mipmaps");
    check((rd32(desc + D3DDD7_OFF_dpcTriCaps + D3DPC_OFF_dwTextureFilterCaps) &
           D3DPTFILTERCAPS_MIPFLINEAR) != 0,
          "advertise linear mip filtering");

    uint32_t texture =
        make(8, DDSCAPS_TEXTURE | DDSCAPS_MIPMAP | DDSCAPS_COMPLEX | DDSCAPS_SYSTEMMEMORY);
    auto fill = [&](uint32_t surface, uint32_t color) {
        wr32(desc, DDSD2_SIZE);
        check(call_method(surface, 25, {0, desc, DDLOCK_WAIT, 0}) == DD_OK, "Lock mip");
        uint32_t pixels = rd32(desc + DDSD_OFF_lpSurface), pitch = rd32(desc + DDSD_OFF_lPitch);
        for (uint32_t y = 0; y < rd32(desc + DDSD_OFF_dwHeight); ++y)
            for (uint32_t x = 0; x < rd32(desc + DDSD_OFF_dwWidth); ++x)
                wr32(pixels + y * pitch + x * 4, color);
        check(call_method(surface, 32, {0}) == DD_OK, "Unlock mip");
    };
    uint32_t levels[4] = {texture, 0, 0, 0};
    uint32_t colors[4] = {0xffff0000, 0xff00ff00, 0xff0000ff, 0xffffffff};
    uint32_t caps = scratch + 768;
    gm_zero(caps, 16);
    wr32(caps, DDSCAPS_TEXTURE | DDSCAPS_MIPMAP);
    for (uint32_t i = 0; i < 4; ++i) {
        if (i) {
            check(call_method(levels[i - 1], 12, {caps, out}) == DD_OK, "Get next mip");
            levels[i] = rd32(out);
        }
        fill(levels[i], colors[i]);
    }
    check(call_method(dev, 20, {7, 0}) == DD_OK, "disable depth");
    check(call_method(dev, 20, {22, 1}) == DD_OK, "disable culling");
    check(call_method(dev, 20, {137, 0}) == DD_OK, "disable lighting");
    check(call_method(dev, 37, {0, 1, 2}) == DD_OK, "SELECTARG1 color");
    check(call_method(dev, 37, {0, 2, 2}) == DD_OK, "color arg texture");
    check(call_method(dev, 37, {0, 16, 2}) == DD_OK, "linear magnification");
    check(call_method(dev, 37, {0, 17, 2}) == DD_OK, "linear minification");
    check(call_method(dev, 35, {0, texture}) == DD_OK, "bind root mip chain");
    uint32_t vertices = scratch + 1024;
    const float xy[3][2] = {{-.5f, -.5f}, {31.5f, -.5f}, {-.5f, 31.5f}};
    for (uint32_t i = 0; i < 3; ++i) {
        float v[8] = {xy[i][0], xy[i][1], .5f, 1.f, 0, 0, i == 1 ? 8.f : 0.f, i == 2 ? 8.f : 0.f};
        memcpy(gm_ptr(vertices + i * 32), v, 32);
        wr32(vertices + i * 32 + 16, 0xffffffff);
    }
    auto draw = [&](uint32_t mip_filter, float bias, int r, int g, int b) {
        uint32_t bias_bits;
        memcpy(&bias_bits, &bias, 4);
        check(call_method(dev, 37, {0, 18, mip_filter}) == DD_OK, "set mip filter");
        check(call_method(dev, 37, {0, 19, bias_bits}) == DD_OK, "set LOD bias");
        check(call_method(dev, 10, {0, 0, 1, 0, 0x3f800000, 0}) == DD_OK, "Clear");
        check(call_method(dev, 5) == DD_OK, "BeginScene");
        check(call_method(dev, 25, {4, 0x1c4, vertices, 3, 0}) == DD_OK, "DrawPrimitive");
        check(call_method(dev, 6) == DD_OK, "EndScene");
        uint8_t pixels[16 * 16 * 4];
        uint32_t size = 0;
        D3d8Error err{};
        check(d3d8_device_read_pixels((D3d8Device *)com_this(dev)->d3d7_host, pixels,
                                      sizeof(pixels), &size, &err) == 0,
              "GPU readback");
        uint8_t *p = pixels + (8 * 16 + 8) * 4;
        if (std::abs(int(p[0]) - r) > 2 || std::abs(int(p[1]) - g) > 2 ||
            std::abs(int(p[2]) - b) > 2)
            fprintf(stderr, "filter=%u bias=%g: RGB=%u,%u,%u expected=%d,%d,%d\n", mip_filter, bias,
                    p[0], p[1], p[2], r, g, b);
        check(std::abs(int(p[0]) - r) <= 2 && std::abs(int(p[1]) - g) <= 2 &&
                  std::abs(int(p[2]) - b) <= 2,
              "selected/interpolated mip color");
    };
    draw(1, 0, 0, 255, 0);     // point mip, derivative selects level 1
    draw(2, .5f, 0, 128, 128); // trilinear between levels 1 and 2
    draw(0, 0, 255, 0, 0);     // NONE must keep level 0
    fill(levels[1], 0xffffff00);
    draw(1, 0, 255, 255, 0); // child update reaches GPU without rebinding root
    for (uint32_t i = 1; i < 4; ++i)
        call_method(levels[i], 2);
    call_method(dev, 2);
    call_method(texture, 2);
    call_method(target, 2);
    call_method(d3d, 2);
    call_method(dd, 2);
    dx_reset();
    mem_shutdown();
    printf("%d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
