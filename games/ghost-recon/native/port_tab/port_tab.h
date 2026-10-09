#ifndef GHOST_RECON_PORT_TAB_H
#define GHOST_RECON_PORT_TAB_H
/* Included by the generated dispatch table and this module's source. */
#include "port_tab_api.h"
#include "x86.h"

/* Resource loader hook: when the game opens Data/Shell/IKE.RES, read the
 * installed asset, generate the augmented bytes and hand the original loader a
 * synthetic guest path aliased to a host temp file. Any failure falls back to
 * the original file with a named diagnostic. */
void gr_ui_load_resource(X86 *c);
/* Options page dispatch: live FXAA toggle, Accept/Cancel/Reset semantics. */
void gr_options_dispatch(X86 *c);
/* Options page show/hide: snapshot on open, keep PORT hidden until selected. */
void gr_options_set_visible(X86 *c);
/* Options page accept/cancel, so keyboard paths that bypass the dispatch also
 * persist or revert the toggle. */
void gr_options_accept(X86 *c);
void gr_options_cancel(X86 *c);

#define FN_00650490 gr_ui_load_resource
#define FN_006a7fe0 gr_options_dispatch
#define FN_006a7990 gr_options_set_visible
#define FN_006ac670 gr_options_accept
#define FN_006ac5f0 gr_options_cancel
#endif
