#include "riff.h"
#include "../runtime/memory.h"

namespace {

constexpr uint32_t kRiff = 0x46464952, kWave = 0x45564157;
constexpr uint32_t kFmt = 0x20746d66, kData = 0x61746164, kFact = 0x74636166;

uint32_t rd32le(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

uint16_t rd16le(const uint8_t *p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

} // namespace

bool riff_parse_wave_host(const uint8_t *bytes, size_t size, WavImage *out) {
    if (!bytes || !out || size < 12 || size > UINT32_MAX)
        return false;
    uint32_t end = (uint32_t)size;
    if (rd32le(bytes) != kRiff || rd32le(bytes + 8) != kWave)
        return false;
    WavImage w{};
    uint32_t at = 12;
    bool have_fmt = false;
    while (at <= end && end - at >= 8) {
        uint32_t id = rd32le(bytes + at), chunk = rd32le(bytes + at + 4);
        uint32_t data = at + 8, available = end - data;
        if (id == kFmt) {
            if (chunk < 16 || chunk > available)
                return false;
            uint16_t format = rd16le(bytes + data);
            uint16_t channels = rd16le(bytes + data + 2);
            uint32_t rate = rd32le(bytes + data + 4);
            uint16_t block = rd16le(bytes + data + 12);
            uint16_t bits = rd16le(bytes + data + 14);
            if (!format || (channels != 1 && channels != 2) || !rate || rate > INT32_MAX)
                return false;
            if (format == 1 && bits != 8 && bits != 16)
                return false;
            w.format = format;
            w.channels = channels;
            w.rate = rate;
            w.block = block;
            w.bits = bits;
            have_fmt = true;
        } else if (id == kFact) {
            if (chunk < 4 || chunk > available)
                return false;
            w.frames = rd32le(bytes + data);
        } else if (id == kData && have_fmt) {
            uint32_t n = chunk > available ? available : chunk;
            if (!n)
                return false;
            w.bytes = n;
            w.data = bytes + data;
            *out = w;
            return true;
        }
        uint64_t padded = (uint64_t)chunk + (chunk & 1u);
        if (padded > available)
            return false;
        at = data + (uint32_t)padded;
    }
    return false;
}

bool riff_parse_wave(uint32_t p, uint32_t bytes, RiffWave *out) {
    if (!out || bytes < 12 || !gm_valid(p, bytes))
        return false;
    WavImage w{};
    if (!riff_parse_wave_host(g_mem + p, bytes, &w) || w.format != 1)
        return false;
    out->pcm = p + (uint32_t)(w.data - (g_mem + p));
    out->pcm_bytes = w.bytes;
    out->rate = w.rate;
    out->channels = w.channels;
    out->bits = w.bits;
    return true;
}
