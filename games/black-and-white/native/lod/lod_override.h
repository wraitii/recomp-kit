#ifndef BW_LOD_OVERRIDE_H
#define BW_LOD_OVERRIDE_H
/* Included by the generated dispatch table and this replacement's source.
 * Defining FN_00803c00 routes every caller of LH3DIsland::Create through
 * bw_island_create; fn_00803c00 stays the translated original. */
#include "x86.h"

void bw_island_create(X86 *c);

#define FN_00803c00 bw_island_create
#endif
