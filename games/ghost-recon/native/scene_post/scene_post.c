#include "scene_post.h"
#include "../port_tab/port_tab_api.h"
#include "native_seam.h"
#include "x86.h"
#include <stdio.h>
#include <stdlib.h>

void fn_0047c160(
    X86 *c); /* Retained mechanical original, bypasses FN override. */

/* Return address of the vtable +0x1c call at 0x0045f610 in FUN_0045f350 (the
 * scene draw): CALL dword ptr [EDX+0x1c], three bytes. The same function is
 * also called directly at 0x0048cba3 and from vtable 0x00857354 slot 7. */
#define SCENE_DRAW_AFTER_EFFECTS 0x0045f613u

/* DIVERGENCE(original): optional host post-process of the finished 3D scene.
 * FUN_0045f350 draws the world, sorted translucents and effects, calls this
 * function (0x0047c160, screen-space overlay quads) through
 * [DAT_008d5e18]+0x1c, and only then starts the HUD (radar, health and ammo
 * panels, crosshair via 0x00490070/0x00492390/0x00493fc0/0x004912f0/
 * 0x0049b530). A scene dump showed the HUD panels were drawn before the
 * later Ui_DrawThunk (0x0064f540), so that is too late to be a boundary.
 * The live mode comes from the Port tab's FXAA checkbox (gr_port_fxaa_mode),
 * seeded by RECOMP_FXAA. 1 filters the backbuffer right after this call
 * returns; 0 only marks the boundary in a RECOMP_D3D8_TRACE_DRAWS trace and
 * leaves behavior unchanged; any other value is passed to the renderer, which
 * stops with its existing diagnostic. Other callers run the original. */
void gr_scene_post(X86 *c) {
  uint32_t ret = rd32(c->r[R_ESP]);
  fn_0047c160(c);
  if (ret == SCENE_DRAW_AFTER_EFFECTS)
    d3d8_scene_boundary(c, gr_port_fxaa_mode());
}
