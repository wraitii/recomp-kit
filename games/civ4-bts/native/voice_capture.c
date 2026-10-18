#include "civ4_native.h"
#include "platform/os.h"
#include <string.h>

/* DIVERGENCE(original): defer 0x008ebb20 device creation until the checkbox's
 * 0x008e1fa0 enable request; release on disable and gate 0x008e2010 capture.
 * Evidence: 0x008ec3c0 constructs capture unconditionally, 0x009f76d0 starts
 * its buffer, and 0x009f69e0 stops/releases it. Host capture remains missing. */
extern void fn_008ebb20(X86 *c);
extern void fn_008e1fa0(X86 *c);
extern void fn_008e2010(X86 *c);

enum {
    VOICE_SUBSYSTEM = 0x2da4,
    CAPTURE_DEVICE_INDEX = 0x76c,
    CAPTURE_DEVICE = 0x774,
    CAPTURE_VOLUME = 0x780,
    VOICE_ENABLED = 0x790,
    CAPTURE_TARGET = 0x794,
};

static int lazy_capture(void) {
    const char *value = recomp_env("CIV4_LAZY_CAPTURE");
    return !value || strcmp(value, "0") != 0;
}

static void return_void(X86 *c, uint32_t bytes) {
    c->eip = rd32(c->r[R_ESP]);
    c->r[R_ESP] += 4 + bytes;
    recomp_return(c);
}

void civ4_select_capture_device(X86 *c) {
    uint32_t voice = c->r[R_ECX];
    if (!lazy_capture() || rd8(voice + VOICE_ENABLED)) {
        fn_008ebb20(c);
        return;
    }
    wr32(voice + CAPTURE_DEVICE_INDEX, rd32(c->r[R_ESP] + 4));
    return_void(c, 4);
}

void civ4_enable_voice(X86 *c) {
    uint32_t manager = c->r[R_ECX];
    uint32_t voice = rd32(manager + VOICE_SUBSYSTEM);
    if (lazy_capture() && voice) {
        if (rd8(c->r[R_ESP] + 4)) {
            if (!rd32(voice + CAPTURE_DEVICE)) {
                uint32_t index = rd32(voice + CAPTURE_DEVICE_INDEX);
                uint32_t volume[2] = {rd32(voice + CAPTURE_VOLUME),
                                      rd32(voice + CAPTURE_VOLUME + 4)};
                uint8_t enabled = rd8(voice + VOICE_ENABLED);
                wr8(voice + VOICE_ENABLED, 1);
                c->r[R_ECX] = voice;
                recomp_guest_call(c, 0x008ebb20u, &index, 1);
                wr8(voice + VOICE_ENABLED, enabled);
                c->r[R_ECX] = voice;
                recomp_guest_call(c, 0x008eb370u, volume, 2);
            }
        } else {
            uint32_t capture = rd32(voice + CAPTURE_DEVICE);
            if (capture) {
                uint32_t args[2] = {capture, 1};
                if (recomp_guest_call(c, 0x009d5fa0u, args, 2))
                    recomp_guest_call(c, 0x009d5f90u, args, 2);
                recomp_guest_call(c, 0x009d5f70u, &capture, 1);
                wr32(voice + CAPTURE_DEVICE, 0);
                wr32(voice + CAPTURE_TARGET, 0xffffffffu);
            }
        }
        c->r[R_ECX] = manager;
    }
    fn_008e1fa0(c);
}

void civ4_enable_capture(X86 *c) {
    uint32_t voice = rd32(c->r[R_ECX] + VOICE_SUBSYSTEM);
    if (lazy_capture() && voice && !rd8(voice + VOICE_ENABLED)) {
        return_void(c, 8);
        return;
    }
    fn_008e2010(c);
}
