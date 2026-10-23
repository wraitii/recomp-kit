#ifndef EVO2_FRAME_PACING_H
#define EVO2_FRAME_PACING_H
#include "x86.h"
#ifdef __cplusplus
extern "C" {
#endif
void evo2_complete_frame(X86 *c);
void evo2_load_input_bindings(X86 *c);
void evo2_force_startup_graphics(X86 *c);
#ifdef __cplusplus
}
#endif
#define FN_00495d10 evo2_complete_frame
#define FN_004823d0 evo2_load_input_bindings
#define FN_00491d00 evo2_force_startup_graphics
#endif
