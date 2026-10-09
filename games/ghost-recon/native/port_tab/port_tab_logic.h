#ifndef GHOST_RECON_PORT_TAB_LOGIC_H
#define GHOST_RECON_PORT_TAB_LOGIC_H
/* Dependency-free helpers over guest memory, so the confirmed wrapper bugs
 * (filename boundary, RSString double indirection, child lookup) are unit
 * tested without the runtime. `mem` is the guest arena base. */
#include <stddef.h>
#include <stdint.h>

uint32_t gr_rd32(const uint8_t *mem, uint32_t addr);
/* True when the guest path names IKE.RES, case-insensitively, at a path
 * boundary (so "fooike.res" does not match). */
int gr_path_is_ike_res(const char *guest_path);
int gr_wide_eq(const uint16_t *s, const char *ascii);
/* Widget name: the RSString at widget+0x20 holds a control block pointer at
 * +4 whose +4 is the wide buffer (FUN_0056c4e0's double indirection). */
const uint16_t *gr_widget_name(const uint8_t *mem, uint32_t widget);
uint32_t gr_child_by_name(const uint8_t *mem, uint32_t container,
                          const char *ascii);
#endif
