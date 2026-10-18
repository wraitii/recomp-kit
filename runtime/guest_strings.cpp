#include "runtime/guest.h"

#include <algorithm>
#include <cstring>
#include <string>

std::string gm_str(uint32_t a, size_t max_len) {
    if (a == 0 || a >= GUEST_SIZE)
        return std::string();
    const size_t available = std::min(max_len, (size_t)(GUEST_SIZE - a));
    if (!available)
        return std::string();
    const char *begin = (const char *)(g_mem + a);
    const char *end = (const char *)memchr(begin, 0, available);
    return std::string(begin, end ? (size_t)(end - begin) : available);
}

uint32_t gm_put_str(uint32_t a, const char *s, uint32_t cap) {
    if (!a || !cap)
        return 0;
    uint32_t n = 0;
    while (s[n] && n + 1 < cap) {
        g_mem[a + n] = (uint8_t)s[n];
        ++n;
    }
    g_mem[a + n] = 0;
    return n;
}

std::string gm_wstr(uint32_t a, size_t max_chars) {
    std::string out;
    if (!a || !gm_valid(a, 2))
        return out;
    // Clamp before address arithmetic, including the lookahead for a low surrogate.
    max_chars = std::min(max_chars, (size_t)(GUEST_SIZE - a) / 2);
    for (size_t i = 0; i < max_chars; ++i) {
        uint32_t u = rd16(a + 2 * (uint32_t)i);
        if (!u)
            break;
        if (u >= 0xd800 && u < 0xdc00 && i + 1 < max_chars) {
            uint32_t lo = rd16(a + 2 * (uint32_t)(i + 1));
            if (lo >= 0xdc00 && lo < 0xe000) {
                u = 0x10000 + ((u - 0xd800) << 10) + (lo - 0xdc00);
                ++i;
            }
        }
        if (u >= 0xd800 && u < 0xe000)
            u = 0xfffd;
        if (u < 0x80) {
            out += (char)u;
        } else if (u < 0x800) {
            out += (char)(0xc0 | (u >> 6));
            out += (char)(0x80 | (u & 0x3f));
        } else if (u < 0x10000) {
            out += (char)(0xe0 | (u >> 12));
            out += (char)(0x80 | ((u >> 6) & 0x3f));
            out += (char)(0x80 | (u & 0x3f));
        } else {
            out += (char)(0xf0 | (u >> 18));
            out += (char)(0x80 | ((u >> 12) & 0x3f));
            out += (char)(0x80 | ((u >> 6) & 0x3f));
            out += (char)(0x80 | (u & 0x3f));
        }
    }
    return out;
}

uint32_t gm_put_wstr(uint32_t a, const std::string &s, uint32_t cap) {
    if (!a || !cap || !gm_valid(a, 2))
        return 0;
    cap = std::min(cap, (GUEST_SIZE - a) / 2);
    uint32_t n = 0;
    size_t i = 0;
    while (i < s.size() && n + 1 < cap) {
        unsigned char b = (unsigned char)s[i];
        if (!b)
            break;
        uint32_t u = 0xfffd, minimum = 0;
        size_t len = 1;
        if (b < 0x80) {
            u = b;
        } else if (b >= 0xc2 && b <= 0xdf) {
            u = b & 0x1f;
            minimum = 0x80;
            len = 2;
        } else if (b >= 0xe0 && b <= 0xef) {
            u = b & 0x0f;
            minimum = 0x800;
            len = 3;
        } else if (b >= 0xf0 && b <= 0xf4) {
            u = b & 0x07;
            minimum = 0x10000;
            len = 4;
        }
        bool valid = len <= s.size() - i;
        for (size_t k = 1; valid && k < len; ++k) {
            unsigned char next = (unsigned char)s[i + k];
            valid = (next & 0xc0) == 0x80;
            u = (u << 6) | (next & 0x3f);
        }
        if (!valid || u < minimum || u > 0x10ffff || (u >= 0xd800 && u < 0xe000)) {
            // Consume one invalid byte at a time; never read beyond the source.
            u = 0xfffd;
            len = 1;
        }
        i += len;
        if (u >= 0x10000) {
            if (n + 2 >= cap)
                break;
            wr16(a + 2 * n++, (uint16_t)(0xd800 + ((u - 0x10000) >> 10)));
            wr16(a + 2 * n++, (uint16_t)(0xdc00 + ((u - 0x10000) & 0x3ff)));
        } else {
            wr16(a + 2 * n++, (uint16_t)u);
        }
    }
    wr16(a + 2 * n, 0);
    return n;
}
