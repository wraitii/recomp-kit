#ifndef CIV4_NATIVE_H
#define CIV4_NATIVE_H
#include "x86.h"
#ifdef __cplusplus
extern "C" {
#endif
void civ4_select_capture_device(X86 *c);
void civ4_enable_voice(X86 *c);
void civ4_enable_capture(X86 *c);
#ifdef __cplusplus
}
#endif
#define FN_008ebb20 civ4_select_capture_device
#define FN_008e1fa0 civ4_enable_voice
#define FN_008e2010 civ4_enable_capture
#endif
