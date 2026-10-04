// dxt_decode.h - S3TC / DXT block decoding shared by the DirectDraw surface
// layer (a DXT source surface blitted into a normal texture) and the D3D7
// texture upload (a DXT surface bound as a texture).
//
// The guest's DXT bytes stay authoritative: they live in the surface's own
// guest-addressable storage and a guest Lock sees the raw blocks. This header
// is only the CPU decode used where a host needs linear RGBA: a Blt into a
// 16/32-bit destination, or the wgpu upload point.
//
// The engine creates DXT surfaces itself and never asks the D3D7 device for
// them (their CreateSurface is a DirectDraw one), so the Blt path is the one
// that actually feeds the tree/foliage textures. See
// docs/d3d7-inventory.md and docs/recompilation.md.
#pragma once
#include <stdint.h>
#include <string.h>

namespace dxdxt {

// The FourCCs this decoder understands. DXT2/DXT4 are the premultiplied-alpha
// cousins of DXT3/DXT5; the engine does not create them and premultiplication
// would change the stored colour, so they are deliberately not accepted here
// (the callers fail loudly instead of guessing).
static const uint32_t kDxt1 = 0x31545844u; // 'DXT1'
static const uint32_t kDxt3 = 0x33545844u; // 'DXT3'
static const uint32_t kDxt5 = 0x35545844u; // 'DXT5'

struct Rgba {
    uint8_t r, g, b, a;
};

inline bool is_dxt(uint32_t fourcc) {
    return fourcc == kDxt1 || fourcc == kDxt3 || fourcc == kDxt5;
}

// Bytes per 4x4 block: DXT1 is 8, the alpha-carrying formats are 16.
inline uint32_t block_bytes(uint32_t fourcc) {
    return fourcc == kDxt1 ? 8u : 16u;
}

// Rows of blocks for a height: ceil(height / 4).
inline uint32_t block_rows(uint32_t height) {
    return (height + 3u) / 4u;
}

// Bytes in one row of blocks: the effective pitch a guest Lock reports.
inline uint32_t pitch(uint32_t width, uint32_t fourcc) {
    return ((width + 3u) / 4u) * block_bytes(fourcc);
}

// Total linear size of the top level (no mip chain is created for these).
inline uint32_t linear_size(uint32_t width, uint32_t height, uint32_t fourcc) {
    return pitch(width, fourcc) * block_rows(height);
}

inline uint16_t rd16le(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

inline uint32_t rd32le(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

// 5:6:5 to 8:8:8 by bit replication, the same rule the D3D7 16bpp texture
// expansion and the Rust renderer's ColorFormat decoder use.
inline uint8_t expand5(uint32_t v) {
    return (uint8_t)((v << 3) | (v >> 2));
}
inline uint8_t expand6(uint32_t v) {
    return (uint8_t)((v << 2) | (v >> 4));
}

// The four-colour DXT palette. `always_four` is set for DXT3/5 (and not for
// DXT1's three-colour mode), matching the format's own rule.
inline void decode_colour_block(const uint8_t *p, bool always_four, Rgba out[16]) {
    const uint16_t c0 = rd16le(p), c1 = rd16le(p + 2);
    const uint8_t r0 = expand5((c0 >> 11) & 0x1f), g0 = expand6((c0 >> 5) & 0x3f),
                  b0 = expand5(c0 & 0x1f);
    const uint8_t r1 = expand5((c1 >> 11) & 0x1f), g1 = expand6((c1 >> 5) & 0x3f),
                  b1 = expand5(c1 & 0x1f);
    Rgba pal[4];
    pal[0] = Rgba{r0, g0, b0, 0xff};
    pal[1] = Rgba{r1, g1, b1, 0xff};
    if (always_four || c0 > c1) {
        pal[2] = Rgba{(uint8_t)((2 * r0 + r1) / 3), (uint8_t)((2 * g0 + g1) / 3),
                      (uint8_t)((2 * b0 + b1) / 3), 0xff};
        pal[3] = Rgba{(uint8_t)((r0 + 2 * r1) / 3), (uint8_t)((g0 + 2 * g1) / 3),
                      (uint8_t)((b0 + 2 * b1) / 3), 0xff};
    } else {
        pal[2] = Rgba{(uint8_t)((r0 + r1) / 2), (uint8_t)((g0 + g1) / 2), (uint8_t)((b0 + b1) / 2),
                      0xff};
        pal[3] = Rgba{0, 0, 0, 0}; // transparent in DXT1's three-colour mode
    }
    const uint32_t bits = rd32le(p + 4);
    for (int i = 0; i < 16; ++i)
        out[i] = pal[(bits >> (2 * i)) & 3u];
}

// 16 4-bit alpha values for DXT3.
inline void decode_alpha4(const uint8_t *p, Rgba out[16]) {
    for (int i = 0; i < 16; ++i) {
        const uint32_t a = (p[i >> 1] >> ((i & 1) * 4)) & 0xf;
        out[i].a = (uint8_t)((a << 4) | a);
    }
}

// DXT3: eight alpha bytes then a colour block that always uses four colours.
inline void decode_dxt3(const uint8_t *p, Rgba out[16]) {
    decode_alpha4(p, out);
    Rgba colour[16];
    decode_colour_block(p + 8, true, colour);
    for (int i = 0; i < 16; ++i) {
        out[i].r = colour[i].r;
        out[i].g = colour[i].g;
        out[i].b = colour[i].b;
    }
}

// DXT5: two 8-bit alpha endpoints then 48 bits of 3-bit indices, then the
// always-four-colour block.
inline void decode_dxt5(const uint8_t *p, Rgba out[16]) {
    const uint8_t a0 = p[0], a1 = p[1];
    uint64_t abits = 0;
    for (int i = 0; i < 6; ++i)
        abits |= (uint64_t)p[2 + i] << (8 * i);
    uint8_t alpha[8];
    if (a0 > a1) {
        alpha[0] = a0;
        alpha[1] = a1;
        for (int i = 1; i <= 6; ++i)
            alpha[i + 1] = (uint8_t)(((7 - i) * a0 + i * a1) / 7);
    } else {
        alpha[0] = a0;
        alpha[1] = a1;
        for (int i = 1; i <= 4; ++i)
            alpha[i + 1] = (uint8_t)(((5 - i) * a0 + i * a1) / 5);
        alpha[6] = 0x00;
        alpha[7] = 0xff;
    }
    Rgba colour[16];
    decode_colour_block(p + 8, true, colour);
    for (int i = 0; i < 16; ++i) {
        const uint32_t idx = (uint32_t)((abits >> (3 * i)) & 7u);
        out[i].r = colour[i].r;
        out[i].g = colour[i].g;
        out[i].b = colour[i].b;
        out[i].a = alpha[idx];
    }
}

// Decodes one 4x4 block. Returns false for a FourCC this decoder does not
// implement so callers can fail loudly rather than draw garbage.
inline bool decode_block(const uint8_t *p, uint32_t fourcc, Rgba out[16]) {
    switch (fourcc) {
    case kDxt1:
        decode_colour_block(p, false, out);
        return true;
    case kDxt3:
        decode_dxt3(p, out);
        return true;
    case kDxt5:
        decode_dxt5(p, out);
        return true;
    default:
        return false;
    }
}

// Samples one texel (x,y) of a DXT surface. `base` points at the first block
// and `row_pitch` is the block-row pitch. A null/unsupported FourCC writes the
// transparent black and returns false.
inline bool sample(const uint8_t *base, uint32_t row_pitch, uint32_t fourcc, uint32_t x, uint32_t y,
                   Rgba *out) {
    const uint32_t bx = x / 4u, by = y / 4u;
    const uint8_t *p = base + (size_t)by * row_pitch + (size_t)bx * block_bytes(fourcc);
    Rgba block[16];
    if (!decode_block(p, fourcc, block)) {
        *out = Rgba{0, 0, 0, 0};
        return false;
    }
    *out = block[(y & 3u) * 4u + (x & 3u)];
    return true;
}

} // namespace dxdxt
