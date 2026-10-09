#ifndef GHOST_RECON_PORT_TAB_REWRITE_H
#define GHOST_RECON_PORT_TAB_REWRITE_H
/* Pure, dependency-free rewriter for the pinned installed Data/Shell/IKE.RES.
 *
 * DIVERGENCE(original): adds a Port tab and panel without touching the asset on
 * disk. The input bytes are the user's installed resource; the output is a new
 * heap buffer with a TAB_PORT tab, a PORT panel and a PORT_FXAA checkbox. The
 * pinned variant is the GOG 1.4 Shell/IKE.RES (280118 bytes). Every anchor is
 * re-derived by exact name token and its type byte is checked, so a different
 * resource fails closed with a diagnostic instead of being patched blind.
 */
#include <stddef.h>
#include <stdint.h>

/* Rewrites `in` into a malloc'd `*out` (caller frees). Returns 0 on success,
 * or a negative code on any pin/parse failure, writing a diagnostic to `err`.
 */
int gr_port_tab_rewrite(const uint8_t *in, size_t in_len, uint8_t **out,
                        size_t *out_len, char *err, size_t err_len);

/* The pinned input size, so callers can reject other variants before parsing.
 */
#define GR_PORT_TAB_IKE_SIZE 280118u

#endif
