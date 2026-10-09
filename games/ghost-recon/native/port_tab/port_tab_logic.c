#include "port_tab_logic.h"

#include <string.h>

uint32_t gr_rd32(const uint8_t *mem, uint32_t addr) {
  const uint8_t *p = mem + addr;
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

int gr_path_is_ike_res(const char *guest_path) {
  if (!guest_path)
    return 0;
  size_t n = strlen(guest_path);
  if (n < 7)
    return 0;
  const char *t = guest_path + n - 7;
  if (t > guest_path && t[-1] != '\\' && t[-1] != '/' && t[-1] != ':')
    return 0;
  static const char k[] = "ike.res";
  for (int i = 0; i < 7; i++) {
    char c = t[i];
    if (c >= 'A' && c <= 'Z')
      c = (char)(c - 'A' + 'a');
    if (c != k[i])
      return 0;
  }
  return 1;
}

int gr_wide_eq(const uint16_t *s, const char *ascii) {
  if (!s)
    return 0;
  for (; *ascii; s++, ascii++)
    if (*s != (uint16_t)(unsigned char)*ascii)
      return 0;
  return *s == 0;
}

const uint16_t *gr_widget_name(const uint8_t *mem, uint32_t widget) {
  if (!widget)
    return NULL;
  uint32_t ctrl = gr_rd32(mem, widget + 0x24);
  if (!ctrl)
    return NULL;
  uint32_t buf = gr_rd32(mem, ctrl + 4);
  return buf ? (const uint16_t *)(mem + buf) : NULL;
}

uint32_t gr_child_by_name(const uint8_t *mem, uint32_t container,
                          const char *ascii) {
  if (!container)
    return 0;
  uint32_t vec = gr_rd32(mem, container + 0xac);
  uint32_t count = gr_rd32(mem, container + 0xb4);
  if (!vec || count > 0x10000)
    return 0;
  for (uint32_t i = 0; i < count; i++) {
    uint32_t ch = gr_rd32(mem, vec + i * 4);
    if (ch && gr_wide_eq(gr_widget_name(mem, ch), ascii))
      return ch;
  }
  return 0;
}
