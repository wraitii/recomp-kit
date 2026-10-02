// Optional host-only diagnostics. Never retain the caller's frame pointer.
#pragma once
#include <cstdint>

// RECOMP_DUMP_FRAME_DIR writes sequential RGB PNGs. Unset means no file work.
void dx_dump_frame_rgba(const uint8_t *rgba, uint32_t width, uint32_t height);
