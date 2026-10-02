#pragma once
#include "../runtime/x86.h"

// D3D8 CreateDevice changes the calling thread's x87 state unless
// D3DCREATE_FPU_PRESERVE is set. Model that in the guest, independent of the
// host CPU. Wine dlls/d3d8/device.c::setup_fpu/device_init provides the
// D3D8-specific evidence: single precision, nearest, masked exceptions;
// preserve the other CW bits. This occurs before backend device creation,
// so a subsequent backend failure does not restore the old control word.
static inline void d3d8_setup_guest_fpu(X86 *c, uint32_t behavior_flags) {
    constexpr uint32_t FPU_PRESERVE = 0x2;
    if (!(behavior_flags & FPU_PRESERVE))
        x87_set_cw(c, (uint16_t)((c->fpu_cw & ~0x0f3fu) | 0x003fu));
}
