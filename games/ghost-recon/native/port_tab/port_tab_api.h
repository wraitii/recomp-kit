#ifndef GHOST_RECON_PORT_TAB_API_H
#define GHOST_RECON_PORT_TAB_API_H
#include <stdint.h>

/* DIVERGENCE(original): the live FXAA mode the Port tab's checkbox drives.
 * Seeded from RECOMP_FXAA (or the persisted choice) and read by scene_post at
 * the scene boundary. 0 is off; 1 filters; other values are passed through so
 * the renderer keeps its existing unsupported-mode diagnostic. The mode is a
 * plain word written and read under the guest scheduler baton (the options
 * callbacks and the scene boundary both run under it); it is not atomic and
 * must not be read from a native thread. */
uint32_t gr_port_fxaa_mode(void);

#endif
