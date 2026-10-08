// script.h - timed input and render checks for the headless smoke host.
//
// Scripts use ordinary input and observable host metrics. Game-addressed
// camera, entity, world-projection and simulation-dump directives are rejected
// by the parser because those behaviors belong in a game's own test harness.
//
//   wait 500
//   move 320 140
//   moveby -20 10
//   click left 320 140
//   tap 320 140
//   button right down
//   key ESCAPE down
//   pad cross down
//   dump menu
//   dumpc composed
//   probe 320 240 255 0 0 8
//   await picture>0.98 for 2000 within 60000
//   expect textures>0
//   quit
//
// An await hold uses presented frames, while its timeout uses script time.
// This lets a visual condition survive transitions without hanging when the
// renderer stalls. If the clock is pinned and no frame presents, the hold does
// not advance; the independent script-time timeout still bounds the wait.

#pragma once
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

enum HostScriptOp {
    HOST_SCRIPT_WAIT = 0,
    HOST_SCRIPT_MOVE,
    HOST_SCRIPT_MOVEBY,
    HOST_SCRIPT_CLICK,
    HOST_SCRIPT_TAP,
    HOST_SCRIPT_TAP_DRAWABLE,
    HOST_SCRIPT_BUTTON, // press or release a mouse button where the pointer is
    HOST_SCRIPT_KEY,
    HOST_SCRIPT_DUMP,
    HOST_SCRIPT_EXPECT,
    HOST_SCRIPT_PEEK,
    HOST_SCRIPT_AWAIT,
    HOST_SCRIPT_QUIT,
    HOST_SCRIPT_READFILE,
    // The display verbs. Their executors are the smoke host's; a host that has
    // none of them ignores the step rather than failing to build.
    HOST_SCRIPT_GUESTCLICK,
    HOST_SCRIPT_PROBE,
    HOST_SCRIPT_DUMPC,
    HOST_SCRIPT_DUMPAT,
    HOST_SCRIPT_FOCUS, // down: 1 the window gains focus, 0 it loses it
    HOST_SCRIPT_PAD,   // button: control index; x: button level or axis value
};

struct HostScriptStep {
    int op;
    uint32_t at_ms;   // when it runs, on the script clock
    int32_t x, y;     // move, click
    int32_t button;   // click: 0 left, 1 right, 2 middle
    uint32_t addr;    // peek: guest address
    uint32_t len;     // peek: bytes to read
    int32_t down;     // key: 1 down, 0 up
    uint8_t dik;      // key: the DirectInput scan code
    char name[64];    // key name, dump name, expect metric, or guest path
    char text[64];    // readfile: the substring the content must contain
    double threshold; // expect, await: the value to pass
    // `>=` rather than `>`. A turn is a counter and the natural way to wait
    // for one is "at least N"; writing counter>99 to mean counter>=100 is
    // an invitation to an off-by-one in a file nobody re-reads.
    bool at_least;
    uint32_t timeout_ms; // await: how long before giving up
    uint32_t hold_ms;    // await: how long the claim must stay true

    // Pixel checks carry colour and tolerance separately from their position.
    int32_t r, g, b;   // probe: the colour the pixel must be, 0..255
    int32_t tol;       // probe: how far off each channel may be
    uint32_t press_ms; // guestclick: press to release, on the guest clock
};

// Parses `text` into at most `max` steps. Returns the number parsed, or -1 with
// `error` filled in - which names the line and what was wrong with it, because
// a smoke run that fails to parse its own script should say so in one line.
int host_script_parse(const char *text, struct HostScriptStep *out, int max, char *error,
                      size_t error_len);

// The DirectInput scan code a name stands for, or 0. Only the keys a script has
// any reason to press.
uint8_t host_script_dik(const char *name);

#ifdef __cplusplus
}

// `for <ms>` as a number of presented frames, at a clock step of step_ms.
//
// Pure, and separate from the parser, so the conversion can be asserted
// exactly rather than inferred from a run. step_ms of 0 means nothing is
// pinned, and the nominal 50 ms frame step is used. This makes the conversion
// stable whether the clock is pinned or running from host time.
//
// Rounds UP and never returns 0 for a non-zero hold: a hold of one frame is
// the weakest useful claim, and rounding a short hold down to nothing would
// silently restore the defect this replaced.
uint32_t host_script_hold_frames(uint32_t hold_ms, uint32_t step_ms);

// The same conversion for an INPUT hold - a click held down, and later
// guestclick - with a floor of four presented frames.
//
// The floor is the point. A click is only seen if the guest polls the device
// while the button is down, and the guest polls once a frame; 120 ms is two
// and a half frames at a 50 ms step, so a click could be delivered and taken
// away between two polls and simply not happen. That is the pinned flake:
// two runs with byte-identical menu frames diverged at the first click, one
// loading the level and the other sitting at turn 0 with no textures. Four
// frames is short enough to stay a click and long enough that no single
// missed poll loses it.
uint32_t host_script_input_hold_frames(uint32_t hold_ms, uint32_t step_ms);

// ---------------------------------------------------------------------------
// Whether the scheduler's input drain has work it can finish.
//
// The scheduler drains whenever this says yes and asks again immediately
// afterwards, so a yes the drain cannot consume is a hot spin for as long as
// the condition lasts. The decision is here, out of the host, so a test can
// state each case rather than infer it from a run's CPU time.
// ---------------------------------------------------------------------------
struct HostScriptDrainState {
    int quit_requested;  // the script asked to quit; nothing more is due
    int ticking;         // another thread is already inside the tick
    int holding_button;  // a click is held: 1, else 0
    int hold_reached;    // and its release is owed now
    int guestclick_held; // the same, for a guestclick
    int guestclick_reached;
    int await_started; // an await is in progress and has not passed
    int script_started;
    int steps_left; // steps not yet run
    int step_due;   // and the next one is due
};

int host_script_drain_wanted(const struct HostScriptDrainState *s);

// Did the run get to the end of its script?
//
// A run that stopped early has not proved what its script says, however many
// expectations it happened to meet on the way: the ones it never reached
// cannot fail, so a truncated run reads as a cleaner one than a complete
// failure does. `abnormal_exit` is the host's own verdict on how the guest
// stopped; the step counts are how far the script got.
int host_script_run_unfinished(int abnormal_exit, int next_step, int step_count);

// Read a configured guest counter by its generic script metric name.
double host_script_counter_metric(const char *name, uint32_t (*guest_u32)(uint32_t));

#endif
