#include "x86.h"

#define STATE_MANAGER_OWNER 0x0cu
#define OWNER_RENDER_STATES 0x120u
#define OWNER_STAGE_STATES 0x520u
#define OWNER_SAMPLER_STATES 0xd60u
#define OWNER_TEXTURES 0x10a0u
#define OWNER_VERTEX_SHADER 0x10e0u
#define OWNER_PIXEL_SHADER 0x10e8u
#define OWNER_CONSTANTS 0x10f0u
#define OWNER_DEVICE 0x10f8u
#define SAMPLER_STATE_SLOTS 0x00bb0248u
#define VERTEX_SHADER_RESET_STAGES 0x00bb01d0u
#define D3DERR_INVALIDCALL 0x8876086cu

static uint32_t device_call(X86 *c, uint32_t owner, uint32_t offset, uint32_t a0, uint32_t a1,
                            uint32_t a2, int extra) {
    uint32_t device = rd32(owner + OWNER_DEVICE);
    uint32_t args[4] = {device, a0, a1, a2};
    return recomp_guest_call(c, rd32(rd32(device) + offset), args, 1 + extra);
}

static uint32_t set_transform(X86 *c, const uint32_t *a) {
    uint32_t device = rd32(a[0] + 0x10u);
    uint32_t args[3] = {device, a[1], a[2]};
    return recomp_guest_call(c, rd32(rd32(device) + 0xb0u), args, 3);
}

static uint32_t set_render_state(X86 *c, const uint32_t *a) {
    uint32_t owner = rd32(a[0] + STATE_MANAGER_OWNER);
    uint32_t slot = owner + OWNER_RENDER_STATES + a[1] * 4u;
    if (rd32(slot) != a[2]) {
        device_call(c, owner, 0xe4u, a[1], a[2], 0, 2);
        wr32(slot, a[2]);
    }
    return 0;
}

static uint32_t set_texture(X86 *c, const uint32_t *a) {
    uint32_t owner = rd32(a[0] + STATE_MANAGER_OWNER);
    uint32_t slot = owner + OWNER_TEXTURES + a[1] * 4u;
    if (rd32(slot) != a[2]) {
        wr32(slot, a[2]);
        device_call(c, owner, 0x104u, a[1], a[2], 0, 2);
    }
    if ((int32_t)a[1] > (int32_t)rd32(a[0] + 4u))
        wr32(a[0] + 4u, a[1]);
    return 0;
}

static uint32_t set_texture_stage_state(X86 *c, const uint32_t *a) {
    uint32_t owner = rd32(a[0] + STATE_MANAGER_OWNER);
    uint32_t slot = owner + OWNER_STAGE_STATES + (a[1] * 33u + a[2]) * 4u;
    if (rd32(slot) != a[3]) {
        device_call(c, owner, 0x10cu, a[1], a[2], a[3], 3);
        wr32(slot, a[3]);
    }
    return 0;
}

static uint32_t set_sampler_state(X86 *c, const uint32_t *a) {
    uint32_t owner = rd32(a[0] + STATE_MANAGER_OWNER);
    uint32_t index = a[1] * 13u + rd32(SAMPLER_STATE_SLOTS + a[2] * 4u);
    uint32_t slot = owner + OWNER_SAMPLER_STATES + index * 4u;
    if (rd32(slot) != a[3]) {
        wr32(slot, a[3]);
        device_call(c, owner, 0x114u, a[1], a[2], a[3], 3);
    }
    return 0;
}

static uint32_t set_vertex_shader(X86 *c, const uint32_t *a) {
    uint32_t owner = rd32(a[0] + STATE_MANAGER_OWNER);
    if (rd32(owner + OWNER_VERTEX_SHADER) != a[1]) {
        wr32(owner + OWNER_VERTEX_SHADER, a[1]);
        device_call(c, owner, 0x170u, a[1], 0, 0, 1);
    }
    if (!a[1])
        return 0;
    for (uint32_t stage = 0, row = 0; stage < rd32(VERTEX_SHADER_RESET_STAGES);
         ++stage, row += 0x84u) {
        owner = rd32(a[0] + STATE_MANAGER_OWNER);
        if (rd32(owner + row + 0x54cu) != stage) {
            device_call(c, owner, 0x10cu, stage, 11, stage, 3);
            wr32(owner + row + 0x54cu, stage);
        }
        owner = rd32(a[0] + STATE_MANAGER_OWNER);
        if (rd32(owner + row + 0x580u) != 0) {
            device_call(c, owner, 0x10cu, stage, 24, 0, 3);
            wr32(owner + row + 0x580u, 0);
        }
    }
    return 0;
}

static uint32_t set_pixel_shader(X86 *c, const uint32_t *a) {
    uint32_t owner = rd32(a[0] + STATE_MANAGER_OWNER);
    if (rd32(owner + OWNER_PIXEL_SHADER) != a[1]) {
        wr32(owner + OWNER_PIXEL_SHADER, a[1]);
        device_call(c, owner, 0x1acu, a[1], 0, 0, 1);
    }
    return 0;
}

static uint32_t shadow_constants(const uint32_t *a, uint32_t data, uint32_t capacity, uint32_t low,
                                 uint32_t high) {
    uint32_t shadow = rd32(rd32(a[0] + STATE_MANAGER_OWNER) + OWNER_CONSTANTS);
    uint32_t reg = a[1], source = a[2], end = a[1] + a[3];
    if (end > rd32(shadow + capacity))
        return D3DERR_INVALIDCALL;
    uint32_t target = rd32(shadow + data) + reg * 16u;
    for (uint32_t words = (a[3] << 4) >> 2; words; --words, source += 4, target += 4)
        wr32(target, rd32(source));
    if (rd32(shadow + low) > reg)
        wr32(shadow + low, reg);
    if (rd32(shadow + high) < end)
        wr32(shadow + high, end);
    return 0;
}

static uint32_t set_vertex_shader_constant_f(X86 *c, const uint32_t *a) {
    (void)c;
    return shadow_constants(a, 0x08u, 0x28u, 0x18u, 0x1cu);
}

static uint32_t set_pixel_shader_constant_f(X86 *c, const uint32_t *a) {
    (void)c;
    return shadow_constants(a, 0x10u, 0x2cu, 0x20u, 0x24u);
}

__attribute__((constructor)) static void register_effect_state_manager(void) {
    recomp_register_host_call(0x009cbeb0u, 3, set_transform);
    recomp_register_host_call(0x009cc000u, 3, set_render_state);
    recomp_register_host_call(0x009cc040u, 3, set_texture);
    recomp_register_host_call(0x009cc090u, 4, set_texture_stage_state);
    recomp_register_host_call(0x009cc0e0u, 4, set_sampler_state);
    recomp_register_host_call(0x009cc190u, 2, set_vertex_shader);
    recomp_register_host_call(0x009cc310u, 4, set_vertex_shader_constant_f);
    recomp_register_host_call(0x009cc240u, 2, set_pixel_shader);
    recomp_register_host_call(0x009cc3d0u, 4, set_pixel_shader_constant_f);
}
