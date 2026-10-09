#include "port_tab_rewrite.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------------- */
/* Little-endian scalar access and a growable output buffer.                  */

typedef struct {
  uint8_t *p;
  size_t len, cap;
} Buf;

static uint32_t rdu32(const uint8_t *p, size_t o) {
  return (uint32_t)p[o] | ((uint32_t)p[o + 1] << 8) |
         ((uint32_t)p[o + 2] << 16) | ((uint32_t)p[o + 3] << 24);
}
static uint16_t rdu16(const uint8_t *p, size_t o) {
  return (uint16_t)((uint32_t)p[o] | ((uint32_t)p[o + 1] << 8));
}
static void wru32(uint8_t *p, size_t o, uint32_t v) {
  p[o] = (uint8_t)v;
  p[o + 1] = (uint8_t)(v >> 8);
  p[o + 2] = (uint8_t)(v >> 16);
  p[o + 3] = (uint8_t)(v >> 24);
}
static int fail(char *err, size_t elen, const char *msg) {
  if (err && elen)
    snprintf(err, elen, "%s", msg);
  return -1;
}

static void buf_free(Buf *b) {
  free(b->p);
  b->p = NULL;
  b->len = b->cap = 0;
}
static int buf_put(Buf *b, const void *p, size_t n) {
  if (b->len + n > b->cap) {
    size_t cap = b->cap ? b->cap : 1024;
    while (cap < b->len + n)
      cap *= 2;
    uint8_t *q = (uint8_t *)realloc(b->p, cap);
    if (!q)
      return -1;
    b->p = q;
    b->cap = cap;
  }
  if (n)
    memcpy(b->p + b->len, p, n);
  b->len += n;
  return 0;
}
static int put_u8(Buf *b, uint8_t v) { return buf_put(b, &v, 1); }
static int put_u32(Buf *b, uint32_t v) {
  uint8_t t[4] = {(uint8_t)v, (uint8_t)(v >> 8), (uint8_t)(v >> 16),
                  (uint8_t)(v >> 24)};
  return buf_put(b, t, 4);
}

/* ------------------------------------------------------------------------- */
/* SHA-256, so the whole input is pinned, not just a few anchor tokens.       */

typedef struct {
  uint32_t h[8];
  uint64_t bits;
  uint8_t block[64];
  size_t used;
} Sha256;

static const uint32_t K256[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu,
    0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u,
    0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u,
    0xc19bf174u, 0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau, 0x983e5152u,
    0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu,
    0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u,
    0xd6990624u, 0xf40e3585u, 0x106aa070u, 0x19a4c116u, 0x1e376c08u,
    0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu,
    0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u};

static uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static void sha_block(Sha256 *s, const uint8_t *p) {
  uint32_t w[64];
  for (int i = 0; i < 16; i++)
    w[i] = (uint32_t)p[i * 4] << 24 | (uint32_t)p[i * 4 + 1] << 16 |
           (uint32_t)p[i * 4 + 2] << 8 | (uint32_t)p[i * 4 + 3];
  for (int i = 16; i < 64; i++) {
    uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
    uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3];
  uint32_t e = s->h[4], f = s->h[5], g = s->h[6], h = s->h[7];
  for (int i = 0; i < 64; i++) {
    uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
    uint32_t ch = (e & f) ^ (~e & g);
    uint32_t t1 = h + S1 + ch + K256[i] + w[i];
    uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
    uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
    uint32_t t2 = S0 + maj;
    h = g;
    g = f;
    f = e;
    e = d + t1;
    d = c;
    c = b;
    b = a;
    a = t1 + t2;
  }
  s->h[0] += a;
  s->h[1] += b;
  s->h[2] += c;
  s->h[3] += d;
  s->h[4] += e;
  s->h[5] += f;
  s->h[6] += g;
  s->h[7] += h;
}

static void sha_init(Sha256 *s) {
  static const uint32_t iv[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u,
                                 0xa54ff53au, 0x510e527fu, 0x9b05688cu,
                                 0x1f83d9abu, 0x5be0cd19u};
  memcpy(s->h, iv, sizeof iv);
  s->bits = 0;
  s->used = 0;
}
static void sha_update(Sha256 *s, const uint8_t *p, size_t n) {
  s->bits += (uint64_t)n * 8;
  while (n) {
    size_t take = 64 - s->used;
    if (take > n)
      take = n;
    memcpy(s->block + s->used, p, take);
    s->used += take;
    p += take;
    n -= take;
    if (s->used == 64) {
      sha_block(s, s->block);
      s->used = 0;
    }
  }
}
static void sha_final(Sha256 *s, uint8_t out[32]) {
  uint64_t bits = s->bits;
  uint8_t pad = 0x80;
  sha_update(s, &pad, 1);
  uint8_t zero = 0;
  while (s->used != 56)
    sha_update(s, &zero, 1);
  uint8_t len[8];
  for (int i = 0; i < 8; i++)
    len[i] = (uint8_t)(bits >> (56 - i * 8));
  sha_update(s, len, 8);
  for (int i = 0; i < 8; i++) {
    out[i * 4] = (uint8_t)(s->h[i] >> 24);
    out[i * 4 + 1] = (uint8_t)(s->h[i] >> 16);
    out[i * 4 + 2] = (uint8_t)(s->h[i] >> 8);
    out[i * 4 + 3] = (uint8_t)s->h[i];
  }
}

/* ------------------------------------------------------------------------- */
typedef struct {
  size_t name_off;
  size_t name_len;
  size_t base_end;
} Base;

/* base FUN_00653dd0: 12 bytes, name, 36 bytes, u16 + optional label, two
 * arrays, two bytes. Returns 0 on success. */
static int parse_base(const uint8_t *in, size_t n, size_t type_byte, Base *b,
                      char *err, size_t elen) {
  size_t o = type_byte + 1;
  if (o + 12 > n)
    return fail(err, elen, "base truncated (header)");
  o += 12;
  if (o + 4 > n)
    return fail(err, elen, "base truncated (name length)");
  b->name_off = o;
  b->name_len = rdu32(in, o);
  o += 4;
  if (o + b->name_len > n)
    return fail(err, elen, "base truncated (name)");
  o += b->name_len;
  if (o + 36 > n)
    return fail(err, elen, "base truncated (fields)");
  o += 36;
  if (o + 2 > n)
    return fail(err, elen, "base truncated (a6)");
  if (rdu16(in, o) == 0xffff) {
    o += 2;
    if (o + 4 > n)
      return fail(err, elen, "base truncated (label length)");
    uint32_t ll = rdu32(in, o);
    o += 4;
    if (o + ll > n)
      return fail(err, elen, "base truncated (label)");
    o += ll;
  } else {
    o += 2;
  }
  if (o + 4 > n)
    return fail(err, elen, "base truncated (array1)");
  uint32_t c1 = rdu32(in, o);
  o += 4;
  if (o + (size_t)c1 * 16 > n)
    return fail(err, elen, "base truncated (array1 data)");
  o += (size_t)c1 * 16;
  if (o + 4 > n)
    return fail(err, elen, "base truncated (array2)");
  uint32_t c2 = rdu32(in, o);
  o += 4;
  if (o + (size_t)c2 * 4 > n)
    return fail(err, elen, "base truncated (array2 data)");
  o += (size_t)c2 * 4;
  if (o + 2 > n)
    return fail(err, elen, "base truncated (tail)");
  o += 2;
  b->base_end = o;
  return 0;
}

/* File offset of the base field +0x2c (x). The 36-byte block after the name is
 * +0x28,@0 +0x2c,@4 +0x30,@8 +0x3c,@12 +0x40,@16 +0x5c,@20. Disassembly of
 * FUN_00653dd0 fixes the order; the page reads x=0/w=640/h=480 and each tab
 * x=5+98k with w=96, so +0x2c is x and +0x3c is width. */
static size_t base_field_2c(const Base *b) {
  return b->name_off + 4 + b->name_len + 4;
}
static size_t base_field_3c(const Base *b) { return base_field_2c(b) + 8; }

/* ------------------------------------------------------------------------- */
/* Rebuild a leaf (types 0x13/0x0f, FUN_0068a770) with a new inline name and  */
/* a new inline c8 label and geometry.                                       */

static int rebuild_leaf(const uint8_t *in, size_t n, size_t span_start,
                        size_t span_end, const char *name, const char *label,
                        uint32_t x, uint32_t y, uint32_t w, Buf *out, char *err,
                        size_t elen) {
  if (span_start < 17 || span_start + 1 > span_end || span_end > n)
    return fail(err, elen, "leaf span out of range");
  Base b;
  if (parse_base(in, n, span_start, &b, err, elen))
    return -1;
  size_t c8_off = b.base_end + 2;
  if (c8_off + 4 > span_end)
    return fail(err, elen, "leaf c8 out of range");
  if (rdu32(in, c8_off) == 0xffffffffu)
    return fail(err, elen, "template c8 is already inline");
  size_t after_name = b.name_off + 4 + b.name_len;
  if (after_name > c8_off)
    return fail(err, elen, "leaf name overlaps c8");

  if (buf_put(out, in + span_start, b.name_off - span_start) ||
      put_u32(out, (uint32_t)strlen(name)) ||
      buf_put(out, name, strlen(name)) ||
      buf_put(out, in + after_name, c8_off - after_name) ||
      put_u32(out, 0xffffffffu) || put_u32(out, (uint32_t)strlen(label)) ||
      buf_put(out, label, strlen(label)) ||
      buf_put(out, in + c8_off + 4, span_end - (c8_off + 4)))
    return fail(err, elen, "out of memory (leaf)");

  Base nb;
  if (parse_base(out->p, out->len, 0, &nb, err, elen))
    return -1;
  wru32(out->p, base_field_2c(&nb), x);
  wru32(out->p, base_field_2c(&nb) + 4, y);
  wru32(out->p, base_field_3c(&nb), w);
  return 0;
}

/* Build a type-0x01 panel from the SOUND panel's base and container header,
 * with a new PORT name, one child and the container footer. The container
 * deserializer FUN_00652400 reads one byte after the children into `+0xea`,
 * and the type-0x01 constructor FUN_00650c10 sets `byte[this+0xea]=0`, so 0 is
 * the value a freshly constructed container holds. Whether every later use of
 * `+0xea` is dead has not been proven, so this follows the constructor rather
 * than asserting the field is unread. */
#define GR_PANEL_FOOTER 0x00u
static int build_panel(const uint8_t *in, size_t n, size_t sound_type_byte,
                       const uint8_t *child, size_t child_len, Buf *out,
                       char *err, size_t elen) {
  if (sound_type_byte < 17 || in[sound_type_byte] != 0x01)
    return fail(err, elen, "panel template is not a container");
  Base b;
  if (parse_base(in, n, sound_type_byte, &b, err, elen))
    return -1;
  size_t count_off = b.base_end + 8;
  if (count_off + 4 > n)
    return fail(err, elen, "panel header out of range");
  size_t after_name = b.name_off + 4 + b.name_len;
  if (after_name > count_off)
    return fail(err, elen, "panel name overlaps count");

  if (buf_put(out, in + sound_type_byte, b.name_off - sound_type_byte) ||
      put_u32(out, 4) || buf_put(out, "PORT", 4) ||
      buf_put(out, in + after_name, count_off - after_name) ||
      put_u32(out, 1) || buf_put(out, child, child_len) ||
      put_u8(out, GR_PANEL_FOOTER))
    return fail(err, elen, "out of memory (panel)");
  return 0;
}

/* ------------------------------------------------------------------------- */

/* Seven tabs must not overlap: the original six run x=5..495 with w=96 on a
 * 98 px pitch. Seven use a 90 px pitch and an 88 px width, so tab i spans
 * [5+90i, 93+90i) and the last ends at 633 <= 640. */
#define GR_TAB_COUNT 7
#define GR_TAB_X0 5
#define GR_TAB_Y 55
#define GR_TAB_DX 90
#define GR_TAB_W 88

/* Verified record boundaries in the SHA-256-pinned GOG 1.4 resource.
 * These are file offsets, not guest addresses. Never apply them before the
 * whole-input identity check. No game asset bytes are embedded here. */
static const size_t k_tabs[] = {0x3076c, 0x3089d, 0x309cb,
                                0x30aff, 0x30c30, 0x30d5d};
enum {
  OPTIONS_COUNT = 0x30558,
  TAB_MIDDLE = 0x30aff,
  TAB_INSERT = 0x30c30,
  TAB_LAST = 0x30d5d,
  TAB_END = 0x30e8b,
  GRAPHICS_PANEL = 0x3419c,
  SOUND_PANEL = 0x39cc8,
  MIPMAP_BUTTON = 0x3719a,
  MIPMAP_END = 0x37289
};

int gr_port_tab_rewrite(const uint8_t *in, size_t in_len, uint8_t **out,
                        size_t *out_len, char *err, size_t err_len) {
  if (!in || !out || !out_len)
    return fail(err, err_len, "invalid argument");
  *out = NULL;
  *out_len = 0;
  if (in_len != GR_PORT_TAB_IKE_SIZE)
    return fail(err, err_len, "IKE.RES size is not the pinned variant");

  /* Whole-input identity, not just anchor tokens. */
  {
    static const uint8_t pinned[32] = {
        0xec, 0x6d, 0x74, 0x56, 0xfe, 0x96, 0xdc, 0xa9, 0x17, 0xea, 0xe7,
        0x3f, 0xb5, 0x91, 0x61, 0xab, 0x2f, 0x89, 0x95, 0xd1, 0x0e, 0xdd,
        0x1b, 0xf3, 0x4d, 0x27, 0xf6, 0x8b, 0x79, 0x73, 0xde, 0xbd};
    Sha256 s;
    uint8_t got[32];
    sha_init(&s);
    sha_update(&s, in, in_len);
    sha_final(&s, got);
    if (memcmp(got, pinned, 32) != 0)
      return fail(err, err_len, "IKE.RES digest is not the pinned variant");
  }

  /* Work on a mutable full copy so the existing tabs and the page count can
   * be patched in place before the two insertions. */
  uint8_t *work = (uint8_t *)malloc(in_len);
  if (!work)
    return fail(err, err_len, "out of memory (input copy)");
  memcpy(work, in, in_len);

  int rc = -1;
  Buf newtab = {0}, newcb = {0}, newpanel = {0}, res = {0};
  /* SOUND owns the mirrored right edge (flag 0x100000 and different UVs).
   * Transfer that style to PORT, then give SOUND GRAPHICS' middle style.
   * Keep SOUND's name and localized button label intact. */
  Base middle, last;
  if (parse_base(in, in_len, TAB_MIDDLE, &middle, err, err_len) ||
      parse_base(in, in_len, TAB_LAST, &last, err, err_len))
    goto done;
  size_t middle_fields = middle.name_off + 4 + middle.name_len;
  size_t last_fields = last.name_off + 4 + last.name_len;
  if (middle.base_end - middle_fields != last.base_end - last_fields) {
    fail(err, err_len, "tab style span mismatch");
    goto done;
  }
  memcpy(work + TAB_LAST + 1, in + TAB_MIDDLE + 1, 12);
  memcpy(work + last_fields, in + middle_fields,
         middle.base_end - middle_fields);
  for (int i = 0; i < GR_TAB_COUNT - 1; i++) {
    Base tb;
    if (parse_base(work, in_len, k_tabs[i], &tb, err, err_len))
      goto done;
    wru32(work, base_field_2c(&tb), (uint32_t)(GR_TAB_X0 + i * GR_TAB_DX));
    wru32(work, base_field_3c(&tb), GR_TAB_W);
  }
  if (rebuild_leaf(in, in_len, TAB_LAST, TAB_END, "TAB_PORT", "Port",
                   (uint32_t)(GR_TAB_X0 + 6 * GR_TAB_DX), GR_TAB_Y, GR_TAB_W,
                   &newtab, err, err_len))
    goto done;
  /* Keep the MIPMAP button's 14px size and texture states, but move it
   * to the upper left of the PORT panel. */
  if (rebuild_leaf(in, in_len, MIPMAP_BUTTON, MIPMAP_END, "PORT_FXAA", "FXAA",
                   24, 24, 14, &newcb, err, err_len))
    goto done;
  if (build_panel(in, in_len, SOUND_PANEL, newcb.p, newcb.len, &newpanel, err,
                  err_len))
    goto done;

  wru32(work, OPTIONS_COUNT, rdu32(work, OPTIONS_COUNT) + 2);

  if (buf_put(&res, work, TAB_INSERT) || buf_put(&res, newtab.p, newtab.len) ||
      buf_put(&res, work + TAB_INSERT, GRAPHICS_PANEL - TAB_INSERT) ||
      buf_put(&res, newpanel.p, newpanel.len) ||
      buf_put(&res, work + GRAPHICS_PANEL, in_len - GRAPHICS_PANEL)) {
    fail(err, err_len, "out of memory (assemble)");
    goto done;
  }
  *out = res.p;
  *out_len = res.len;
  res.p = NULL;
  rc = 0;
done:
  free(work);
  buf_free(&newtab);
  buf_free(&newcb);
  buf_free(&newpanel);
  buf_free(&res);
  return rc;
}
