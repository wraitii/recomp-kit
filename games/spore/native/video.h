#pragma once
#include "x86.h"

#ifdef __cplusplus
extern "C" {
#endif
void spore_video_convert_row(X86 *c);
#ifdef __cplusplus
}
#endif

#define FN_00fce6c0 spore_video_convert_row
