/* native_seam.h - services a game's native overrides may call.
 *
 * A game's [translate] overrides header is compiled into the translation as C,
 * so everything here has C linkage and C types. None of it names a game.
 */
#pragma once
#include <stddef.h>
#include <stdint.h>
/* Same typedef as x86.h; repeating it is valid in C11 and C++. */
typedef struct X86 X86;
#ifdef __cplusplus
extern "C" {
#endif

/* The size, in points, of the display the program's window is shown on - the
 * area a fullscreen window fills. Non-zero with both set, or zero
 * when the host has no display to report. Safe from any guest thread. */
int host_display_screen_size(int *w, int *h);

/* The host path a guest file would be written to in the writable overlay tier
 * (the player's profile). Zero, with nothing written, when there is no such
 * tier: a native override must never write into the game's own directory. */
int recomp_writable_path(const char *guest_path, char *out, size_t out_len);

/* The host path a guest file is read from, through every overlay tier and
 * then the game directory. Zero when the path cannot be resolved. */
int recomp_readable_path(const char *guest_path, char *out, size_t out_len);

/* Offer a DirectDraw display mode, as the host does for its own modes. */
int ddraw_add_mode(int w, int h, int bpp);

/* The guest has finished drawing its 3D scene and is about to draw its overlay.
 * Mode 0 only marks the boundary (a draw trace prints it); mode 1 filters the
 * scene with FXAA first, on the host, so overlay draws stay unfiltered. Needs
 * the wgpu renderer: any non-zero mode without it, or with an unusable device
 * state, stops with a diagnostic. Returns zero on success. Requires the guest
 * scheduler baton. */
int d3d8_scene_boundary(X86 *c, uint32_t mode);

/* Optional game adapter for absolute touch placement. Coordinates and canvas
 * size are logical game pixels after the compositor's mapping. Called only
 * with the guest scheduler baton. Return non-zero only after placing a live
 * cursor; zero retains the host's existing fallback. Physical mouse motion
 * does not call this adapter. */
int recomp_pointer_place(int32_t x, int32_t y, int32_t width, int32_t height);

/* Discard already sampled X/Y motion for a DirectInput mouse after absolute
 * placement. The argument is its guest interface address. Buttons, wheel and
 * keyboard input are retained. Requires the guest scheduler baton. */
void dinput_discard_mouse_motion(uint32_t device);

#ifdef __cplusplus
}
#endif
