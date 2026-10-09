#ifndef GHOST_RECON_PROJECTION_CULL_H
#define GHOST_RECON_PROJECTION_CULL_H
/* Included by the generated dispatch table and this replacement's source.
 * Stable entry thunks keep redirects out of translated callers, so changing
 * this header does not recompile the translated bodies. */
#include "x86.h"

void gr_projection_cull(X86 *c);

int gr_projection_use_original(void);
void gr_projection_build(X86 *c);

#define FN_0081ae30 gr_projection_cull
#define FN_0081aa00 gr_projection_build
#endif
