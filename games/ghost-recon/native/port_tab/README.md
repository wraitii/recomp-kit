# Port tab + live FXAA

`DIVERGENCE(original)`. This mod adds a **Port** options tab with a single
**FXAA** checkbox and lets the checkbox filter the finished 3D scene at runtime.

The installed `Data/Shell/IKE.RES` is read only; it is never modified. The
loader hook rewrites the bytes in memory and writes a **generated** copy to the
host temp directory, then hands the original loader a synthetic guest path
aliased to that copy.

## Files

| File | Purpose |
| --- | --- |
| `port_tab.c` | Native hooks: resource loader, options dispatch/show/accept/cancel, live mode, persistence. |
| `port_tab.h` | `FN_*` redirects for the generated dispatch table. |
| `port_tab_api.h` | Cross-module `gr_port_fxaa_mode()`; consumed by `scene_post`. |
| `port_tab_logic.c/.h` | Dependency-free guest-memory helpers (path match, widget name, child lookup). |
| `port_tab_rewrite.c/.h` | Pure, pinned rewrite of `IKE.RES` into a new heap buffer. |

## Original addresses and evidence

| Address | Role | Evidence |
| --- | --- | --- |
| `00650490` | `UIRoot_LoadResourceFromFile` | Hook target; decompilation shows `FUN_004af420` -> `006e9b4a` -> `006f63ec`/`006f63bb` -> `006fff30` -> `00703c93`, which calls `CreateFileA`. The runtime answers that with `win32_host_path_op(..., WIN32_FILE_READ)`, where the alias is consulted first. |
| `006a7fe0` | `OptionsPage_DispatchEvent` | event id `0xb6`; source widget `**(int**)(event+0x24)` at `006a832d`; receiver is the complete page `+8` (uses `in_ECX-8`). |
| `006a7990` | `OptionsPage_SetVisibleAndInitialize` | vtable `00863468` slot `54` (`00863540`); hides the six original panels of `FILL0`. |
| `006ac670` | Options ACCEPT | vtable `00863468` slot `0x10c` (`00863574`). |
| `006ac5f0` | Options CANCEL | vtable `00863468` slot `0x110` (`00863578`). When `page[0x13c]+0x10` bit 0 is set it backs out of the Input sub-panel and does **not** close Options. |
| `006aa3a0` / `0068a2f0` | Toggle initialization | Original helper sends event `0xba` with `(3, bool)`; button handler writes widget `+0x14` to 2 (checked) or 0 (off). The native synchronization mirrors that exact branch. `0068a0f0(4, bool)` controls hover, not checked state. |
| `00650c10` | type-`0x01` container ctor | zeroes `byte[+0xea]`; `00652400` deserializes that byte after the children. |

## Rewrite contract

* Pinned variant: GOG 1.4 `Data/Shell/IKE.RES`, 280118 bytes,
  SHA-256 `ec6d7456fe96dca917eae73fb59161ab2f8995d10edd1bf34d27f68b7973debd`.
  Any other size or digest fails closed. Only after this check does the rewrite
  use the verified record offsets listed in `port_tab_rewrite.c`. It clones
  records from the installed input; no copied game records are embedded.
* Adds `TAB_PORT` (type `0x13`), `PORT` (type `0x01`) and `PORT_FXAA`
  (type `0x0f`, cloned from MIPMAP); the options page child count goes 24 -> 26 and the container
  footer is 0.
* Original six tabs are retimed to a 90 px pitch and 88 px width so seven fit
  in the 640 px panel: `[5+90i, 93+90i)`. SOUND's mirrored right-edge
  flags and texture coordinates move to PORT; SOUND adopts GRAPHICS' middle style.

## Behavior and switches

* `RECOMP_PORT_TAB=0` disables the rewrite entirely; the original resource is
  used and every menu-side FXAA change is skipped. Menu-side changes are also
  gated on the live page actually containing a `PORT` child, so a digest or
  parse failure cannot change the original RESET behavior.
* `RECOMP_FXAA` seeds the mode. `0` is off, `1` filters, any other value is
  passed to the renderer, which keeps its existing unsupported-mode diagnostic.
* The persisted choice lives at `$HOME/.ghostrecon_recomp_port_tab.cfg` (or
  `%USERPROFILE%`). `RECOMP_PORT_SETTINGS` overrides the path (used by the
  tests). Only 0/1 preferences are written; an unsupported `RECOMP_FXAA` mode
  is a renderer diagnostic and is never persisted. Writes go to an exclusively
  created sibling temp and are renamed into place. On Windows, where `rename`
  cannot replace an existing file, a later save reports and keeps the previous
  choice rather than deleting it.
* Accept persists the live value; Cancel reverts to the snapshot taken on the
  first open (a repeated show while already open does not resnapshot); RESET
  forces 0. The dispatch hook does not persist or revert: the confirmed
  Accept/Cancel callbacks own the transaction.
* `gr_port_fxaa_mode()` is a plain word. The options callbacks and the scene
  boundary both run under the guest scheduler baton, so no host thread may read
  it. `scene_post` reads it at the `0047c160`/`SCENE_DRAW_AFTER_EFFECTS`
  boundary.

## Execution evidence

A headless startup (`build/recomp/pop_headless`, `RECOMP_MAX_FRAMES=180`, no frame
output) with `RECOMP_TRACE_FILES=1` shows the original `GetFileAttributes` on
`C:\Ghost Recon\data\Shell\IKE.RES` (size 280118) and then
`CreateFileA("C:\Windows\Temp\recomp_port_tab.res")` resolving to the host
temp file, with no `port_tab:` fallback diagnostic; the temp file is gone
after the loader returns. That establishes the alias read and a resource parse
on the real path. The user has also confirmed the corrected tab styling and MIPMAP-based FXAA
control work in-game. The cleanup produces a byte-identical resource payload;
the hook harness covers Cancel/Accept and initialization separately.

## Known gaps

* The footer byte at container `+0xea` is deserialized; not every later use has
  been proven dead, so 0 follows the constructor rather than an assertion.
* The alias is removed immediately after the loader returns, on the assumption
  that the loader reads the stream synchronously. A headless run is the
  execution check for that assumption; see `docs/engine-info.md` tooling and
  `recomp-kit/host/headless_main.cpp`.
* PORT uses SOUND's 600x320 panel; the MIPMAP toggle is placed at (24,24),
  retaining its 14x14 size, five texture states, and label font/alignment.
* This is an intentional gameplay/visual divergence, retained behind
  `RECOMP_PORT_TAB=0` and the `FN_00650490` override.
