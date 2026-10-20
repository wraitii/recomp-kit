#pragma once
#include "../runtime/display_seam.h"
#include "pop_mod_api.h"
#include <string>
enum DisplayRow { DISPLAY_WINDOW, DISPLAY_OVERLAY, DISPLAY_ROW_COUNT };
// game.toml [settings] rows sets one bit per row in RECOMP_SETTINGS_ROWS, in
// the order above; the controls rows share the bit after the display rows.
constexpr int DISPLAY_CONTROLS_BIT = DISPLAY_ROW_COUNT;
bool mods_settings_row_listed(int bit);
#ifdef POPM_TESTING
// The kit's tests see every row whatever the game lists; one that checks the
// list sets its own mask (bits as RECOMP_SETTINGS_ROWS).
void mods_settings_rows_for_test(unsigned mask);
#endif
void mods_display_init();
void mods_display_live_defaults();
void mods_display_default_overlay(int mode);
void mods_display_reset();
bool mods_display_row_applies(DisplayRow row);
int mods_display_value(DisplayRow row);
PopModStatus mods_display_set(DisplayRow row, int value);
PopModStatus mods_display_nudge(DisplayRow row, int delta);
std::string mods_display_line(DisplayRow row);
