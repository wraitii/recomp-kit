#pragma once
#include <stdint.h>
#include <stddef.h>

struct RiffWave {
    uint32_t pcm;
    uint32_t pcm_bytes;
    uint32_t rate;
    uint16_t channels;
    uint16_t bits;
};

struct WavImage {
    const uint8_t *data;
    uint32_t bytes;
    uint32_t rate;
    uint16_t format;
    uint16_t channels;
    uint16_t bits;
    uint16_t block;
    uint32_t frames;
};

bool riff_parse_wave(uint32_t guest_ptr, uint32_t bytes, RiffWave *out);
bool riff_parse_wave_host(const uint8_t *bytes, size_t size, WavImage *out);
