// Guest COM -> real GPU regression for render-target texture sampling.
// Distinct texture and surface identities must refer to the same GPU image.
#include "../dx.h"
#include "../../runtime/memory.h"
#include "guest_abi.h"
#include "d3d8_abi.h"

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
    uint32_t scratch = heap_alloc(0x1000, true, 16);
    cpu_reset();
    ComObj *dev = com_new(K_D3D8DEVICE);
    dev->d3d8_width = dev->d3d8_height = 16;
    dev->d3d8_format = 21;
    D3d8Error err{};
    auto *gpu = d3d8_device_create(16, 16, 21, 0, &err);
    if (!gpu) {
        fprintf(stderr, "GPU creation failed: %s\n", err.message);
        return 1;
    }
    dev->d3d8_device = gpu;
    uint32_t device = com_view(dev, IF_D3D8DEVICE);
    check(call_method(device, 16, {0, 0, scratch}) == 0, "GetBackBuffer");
    uint32_t backbuffer = rd32(scratch);
    check(call_method(device, 20, {16, 16, 1, 1, 21, 0, scratch}) == 0, "CreateTexture RT");
    uint32_t texture = rd32(scratch);
    check(call_method(texture, 15, {0, scratch}) == 0, "GetSurfaceLevel");
    uint32_t surface = rd32(scratch);
    check(com_this(texture, IF_D3D8TEXTURE8)->id != com_this(surface, IF_D3D8SURFACE8)->id,
          "fixture uses distinct COM identities");
    // Bind the texture before promoting its surface to a render target.
    check(call_method(device, 61, {0, texture}) == 0, "bind texture");
    check(call_method(device, 31, {surface, 0}) == 0, "bind render surface");
    check(call_method(device, 36, {0, 0, 1, 0xffff00ff, 0x3f800000, 0}) == 0, "clear RT magenta");
    check(call_method(device, 31, {backbuffer, 0}) == 0, "restore backbuffer");
    check(call_method(device, 50, {137, 0}) == 0, "disable lighting");
    check(call_method(device, 50, {7, 0}) == 0, "disable depth");
    check(call_method(device, 50, {22, 1}) == 0, "disable cull");
    check(call_method(device, 76, {0x142}) == 0, "set textured FVF");
    // Synthetic ps.1.1: TEX t0; MOV r0,t0. No original game assets.
    const uint32_t shader[] = {0xffff0101, 66, 0xb00f0000, 1, 0x800f0000, 0xb0e40000, 0xffff};
    for (uint32_t i = 0; i < sizeof(shader) / sizeof(shader[0]); ++i)
        wr32(scratch + 64 + i * 4, shader[i]);
    check(call_method(device, 87, {scratch + 64, scratch}) == 0, "create sampling PS");
    uint32_t ps = rd32(scratch);
    check(call_method(device, 88, {ps}) == 0, "bind sampling PS");
    ComObj *vb = com_new(K_D3D8VERTEXBUFFER);
    vb->pixels = heap_alloc(72, true, 16);
    vb->pixels_bytes = 72;
    const float xy[3][2] = {{-1, -1}, {3, -1}, {-1, 3}};
    for (uint32_t i = 0; i < 3; ++i) {
        float vertex[6] = {xy[i][0], xy[i][1], 0.5f, 0, 0.5f, 0.5f};
        memcpy(gm_ptr(vb->pixels + i * 24), vertex, sizeof(vertex));
        wr32(vb->pixels + i * 24 + 12, 0xffffffff);
    }
    dev->d3d8_stream_vb = vb->id;
    dev->d3d8_stream_stride = 24;
    check(call_method(device, 34) == 0, "BeginScene");
    check(call_method(device, 70, {4, 0, 1}) == 0, "sample rendered texture through COM");
    check(call_method(device, 35) == 0, "EndScene");
    uint8_t pixels[16 * 16 * 4];
    uint32_t size = 0;
    check(d3d8_device_read_pixels(gpu, pixels, sizeof(pixels), &size, &err) == 0,
          "read GPU backbuffer");
    const uint8_t magenta[] = {255, 0, 255, 255};
    check(memcmp(pixels + (8 * 16 + 8) * 4, magenta, 4) == 0,
          "sample GPU-rendered magenta instead of stale CPU black");
    check(call_method(surface, 9, {scratch, 0, 0x10}) == 0, "lock rendered surface readonly");
    check(rd32(rd32(scratch + 4)) == 0xffff00ff, "surface readback uses the same GPU identity");
    call_method(surface, 10);
    call_method(device, 61, {0, 0});
    call_method(device, 90, {ps});
    dev->d3d8_stream_vb = 0;
    com_release(vb);
    call_method(surface, 2);
    call_method(texture, 2);
    call_method(backbuffer, 2);
    call_method(device, 2);
    heap_free(scratch);
    fprintf(stderr, "d3d8 render: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures ? 1 : 0;
}
