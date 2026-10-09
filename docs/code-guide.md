# Code guide

Start from the user-visible behavior, then follow the entry point below. The
native code is grouped by responsibility; the original game functions are
translated locally into `build/recomp/gen/` and are never edited in place.

## Where to make a change

| Change | Start here | Major entry points |
| --- | --- | --- |
| Host display settings | [display_settings.cpp](../mods/display_settings.cpp), [settings_page.cpp](../mods/settings_page.cpp) | `mods_display_init`, `mods_display_set`, `apply_transition` |
| Mouse edges, coordinate mapping | [input_gate.cpp](../host/input_gate.cpp) | `take_layout`, `host_gate_pointer_event`, `host_gate_window_pointer` |
| Window, focus, quit (SDL3) | [sdl/main.cpp](../host/sdl/main.cpp) | `handle_event`, `apply_focus`, `apply_window_mode`, `pump`, `applicationShouldTerminate` |
| Frame lifetime and pacing | [present_thread.cpp](../host/present_thread.cpp) | `acquire`, `host_frame_seal`, `sweep` |
| World rendering, materials | [d3d_render.cpp](../host/d3d_render.cpp) | `host_d3d_expand`, `uploadTexture`, `drawSnapshot`, `d3d_fragment` |
| UI separation and final composition | [ui_layer.cpp](../host/ui_layer.cpp), [compositor.cpp](../host/compositor.cpp) | `ui_layer_extract`, `replay`, `compositor_compose` |
| Audio streaming or gaps | [audio/mixer.cpp](../host/audio/mixer.cpp) | `ensure_engine`, `host_audio_stream`, `host_audio_queue`, `host_audio_queued_bytes` |
| Sound evidence | [audio_capture.cpp](../host/audio_capture.cpp) | `host_capture_write`, `host_capture_stats` |
| Original graphics API behavior | [DirectX adapters](../dx/README.md) | `Surface_Lock`, `Surface_Unlock`, `Surface_Blt`, `Surface_Flip`, `d3d_upload_texture` |
| Imports, startup, memory | [Guest runtime](../runtime/README.md) | `loader_load`, `patch_iat`, `imports_dispatch`, `heap_realloc` |
| Mod lifecycle and hooks | [loader.cpp](../mods/loader.cpp), [hooks.cpp](../mods/hooks.cpp) | `mods_load_all`, `build_api`, `mods_hook_dispatch`, `mods_call_next` |
| Instruction translation | [translate.py](../tools/recomp/translate.py), [x86.h](../runtime/x86.h) | Instruction emitters and register/flag helpers |
| Observable region analysis | [IR contracts](ir.md#observable-contract-foundation), [cfg.py](../tools/recomp/ir/cfg.py) | Shared CFG; immutable observations/effects; conservative demand and call-graph solvers |
| Native replacement validation | [replay.cpp](../mods/native/replay.cpp), [page_track.cpp](../mods/native/page_track.cpp) | `load`, `run`, `translated`, `begin`, `end` |

## Follow display settings

The settings page calls `mods_display_nudge` and `mods_display_set`. The setter
validates choices and persists them through the shared settings store. Host-only
values publish immediately; render and projection changes pass through
`apply_transition` at the frame boundary. Game-owned menus can open the shared
settings page through the mod API.

## Read guest-facing code

`X86* c` is an emulated register *state*, not an executing emulator. `arg(c, i)`
reads an argument from the guest stack; `rd32` and `wr32` access the guest arena.
A hexadecimal function address identifies a translated dispatch entry. A native
pointer, COM handle, surface ID and guest address are distinct values even when
all happen to fit in an integer.

COM adapters validate their `this` object, decode guest parameters, invoke host
services, and return through `com_ret`. Win32 imports use their declared calling
convention in `imports_dispatch`. New callbacks should use `guest_call` and the
existing hook APIs instead of manufacturing host calls to guest addresses.

Named symbols are useful navigation hints, but some are incomplete. Keep address
provenance for a reviewed hook. Do not rename an unknown function to a guessed
gameplay meaning or replace a routine without a behavior comparison.

## Respect ownership boundaries

- Guest memory changes happen on the scheduler baton holder. The window host queues requests.
- The presenter receives immutable frame values and retained resources. Do not
  let it read mutable guest pointers after sealing.
- A surface revision is a content version. Preserve leased bytes before writes;
  changing palette colors can also change resolved texture content.
- Audio player operations follow the documented lock order in `audio/mixer.cpp`. Its
  completion callbacks and playback clock have different timing guarantees.
- Mod teardown revokes entry before unloading code. A callback still on a worker's
  stack must finish or unwind before that module's resources are reclaimed.

Major function comments describe these contracts where they are implemented.
Keep them synchronized when changing behavior. Prefer a focused regression in
that module's tests over copying a large gameplay scenario for a small helper.
See [Testing](testing.md) for the available suites and their limits.

Absolute host positions use the same compositor mapping as ordinary Win32 mouse
input. A game's integrated DirectInput cursor moves when the guest consumes those
messages or counts; the host does not inspect or mutate private cursor objects.
Relative capture is selected with `[input] relative_mouse_capture` (default `true`
to preserve existing no-hook capture behavior; set `false` for absolute mode).
