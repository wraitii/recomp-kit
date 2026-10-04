// Pixel exchange for GDI and common controls. Images own top-down BGRA pixels;
// handles and all bitmap storage exposed to guest code remain 32-bit values.
#pragma once
#include <cstdint>
#include <vector>
struct GdiImage {
    int32_t width = 0, height = 0;
    std::vector<uint32_t> pixels;
};
bool gdi_read_bitmap(uint32_t bitmap, GdiImage *image, bool preserve_alpha = false);
uint32_t gdi_image_bitmap(const GdiImage &image);
uint32_t gdi_image_mask(const GdiImage &image);
void gdi_delete_bitmap(uint32_t bitmap);
bool gdi_draw_image(uint32_t dc, const GdiImage &image, int32_t x, int32_t y, int32_t width,
                    int32_t height);
// Decode a Windows DIB resource. BMP files may supply a pixel offset
// relative to the DIB header; zero selects packed resource layout.
bool gdi_decode_image(uint32_t dib, uint32_t bytes, GdiImage *image, uint32_t pixel_offset = 0);
// Create a bitmap that keeps a raw DIB's own bit depth, palette, bitfields and
// row order, as LoadImage(LR_CREATEDIBSECTION)/CreateDIBSection do on Windows.
// gdi_image_bitmap always yields 32-bpp; this keeps a 24-bpp source at 24-bpp.
uint32_t gdi_create_dib_from_memory(uint32_t dib, uint32_t bytes, uint32_t pixel_offset = 0);
uint32_t gdi_create_icon(const GdiImage &image);
bool gdi_read_icon(uint32_t icon, GdiImage *image);
bool gdi_delete_icon(uint32_t icon);

// Dotted XOR focus border in the selected DIB or window surface; applying it twice restores pixels.
bool gdi_focus_rect(uint32_t dc, int32_t left, int32_t top, int32_t right, int32_t bottom);
