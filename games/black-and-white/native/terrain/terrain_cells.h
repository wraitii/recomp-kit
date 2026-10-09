#ifndef BW_TERRAIN_CELLS_H
#define BW_TERRAIN_CELLS_H
/* Included by the generated dispatch table and this replacement's source.
 * Stable entry thunks keep redirects out of translated callers. */
#include "x86.h"

void bw_build_cell_indices(X86 *c);
/* Shared with the native RenderCell. */
int bw_terrain_use_original(void);
int bw_cell_indices_native_ok(uint32_t mode);
void bw_build_cell_indices_impl(X86 *c, uint32_t cell, uint32_t mode);
void bw_render_cell(X86 *c);

#define FN_00875c60 bw_build_cell_indices
#define FN_00874aa0 bw_render_cell
#endif
