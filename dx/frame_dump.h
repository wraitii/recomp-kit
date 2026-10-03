// Optional host-only diagnostics. Never retain the caller's frame pointer.
#pragma once
#include <cstdint>

// RECOMP_DUMP_FRAME_DIR writes sequential RGB PNGs. Unset means no file work.
void dx_dump_frame_rgba(const uint8_t *rgba, uint32_t width, uint32_t height);

// True when RECOMP_DUMP_FRAME_DIR is set.
bool dx_dump_enabled();
// Writes <RECOMP_DUMP_FRAME_DIR>/<name>.png; a no-op when the directory is unset.
void dx_dump_named_rgba(const char *name, const uint8_t *rgba, uint32_t width, uint32_t height);
