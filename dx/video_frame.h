// Decoder-independent, limited-range BT.601 planar YUV row conversion.
#pragma once
#include <stdint.h>

enum VideoSurfaceType : uint32_t {
    VIDEO_XRGB8888 = 3,
    VIDEO_RGB555 = 9,
    VIDEO_RGB565 = 10,
    VIDEO_BGR24 = 20, // D3DFMT_R8G8B8: three bytes per pixel, blue first
};

// chroma_shift is 1 for YUV420P or 2 for YUV410P. The caller selects
// the chroma row for y >> chroma_shift and supplies width * bpp output bytes.
// The caller supplies width * bytes_per_pixel output bytes, where
// bytes_per_pixel is 4 for VIDEO_XRGB8888, 3 for VIDEO_BGR24 and 2 otherwise.
void video_frame_convert_row(uint8_t *dest, const uint8_t *y, const uint8_t *u, const uint8_t *v,
                             uint32_t width, VideoSurfaceType type, unsigned chroma_shift = 1);
