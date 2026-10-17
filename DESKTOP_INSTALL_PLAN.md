# Desktop library installation task plan

Temporary implementation plan. Work through the steps in order, record completion
here, and delete this file as the final step. Creating this plan does not start
implementation. Plan review alone does not complete implementation checkboxes.

The library helps users install their own games as native applications. Users point
at the files they already have, understand any required preparation, and receive a
complete game folder. Launching the game and its sidecars must work after that folder
moves or is renamed, without the library, its registry, the source installation, the
kit checkout, Python, or build tools.

## Scope and non-goals

- Current milestone: macOS installed folders, ZIPs, assisted preparation, movable
  game folders and interruption recovery. Additional installer, archive and disc
  adapters in Step 4 follow individually against real inputs. Windows and Linux
  packaging follow the macOS validation pass.
- Out of scope: downloading game content, bundling game data, executing installers
  (including under Wine), bypassing copy protection or DRM, patching a user's files in
  place, and making Wine a dependency of the library or the finished app.
- Support level is best-effort. Sources the library cannot extract are handled by
  assisted mode (Step 6), not by running their installers.
- Existing development installations may be recreated; legacy library records and
  installed layouts are outside the new contract.

## Decisions

- **External tools:** evaluate innoextract (zlib-style licence), unshield (MIT) and
  libarchive (BSD-2-Clause) as their adapters are implemented; none ship in the current
  milestone. Keep each selected upstream notice in the bundle. Check each
  shipped file against libarchive's per-file exceptions (Apache-2.0, BSD-4-clause,
  public domain). unshield is preferred as a linked library if its C API proves usable
  in Step 4; libarchive stays only if it covers the image reading we need.
- **Platform:** macOS only for this pass.
- **Black & White:** the installed-folder route is supported. Installer and disc-image
  extraction for Black & White is handled by assisted mode, not by a dedicated
  Black & White flow. Standalone extraction utilities are deferred; they do not ship
  in this pass.
- **Assisted mode:** available for every source, and offered whenever automatic
  preparation cannot finish. It is also selectable up front. Requirements are in
  Step 6.
- **Source identity:** each source variant is its own candidate, identified by payload
  hashes, never by file name. The same game can appear as several variants (stock disc,
  repack, patched folder, different language).

## Assisted mode requirements

These are the requirements that Step 6 implements. They are the baseline for review.

- **Entry:** the user can choose assisted mode at source selection, and the library
  offers it when a required file or tool is missing. It never starts a build while
  required content is absent.
- **Drop folder:** created inside the job's staging area and shown with Open and Copy
  path actions. It is never the user's source and never a folder the library reads
  outside the job.
- **Checklist:** a plain-text file in the drop folder lists each required item with its
  name, purpose, accepted forms, and where to find it. The text is stable and readable
  by a person or their own agent. The library does not depend on any agent.
- **Live feedback:** the library watches the drop folder and shows each item as missing,
  waiting for the file to settle, checking, verified, wrong version, or unreadable.
  It validates a file only after its size and modification time have been stable for
  a short interval, so partial copies are not reported as failures.
- **Continue:** enabled only when every required item is verified. The build starts
  only after the user chooses Continue. Optional items never block Continue.
- **Validation parity:** the same validators as the automatic path run on drop-folder
  content. Assisted mode must not weaken identity, hash or containment checks.
- **Result:** OK, or FAIL with structured errors per item and a recovery action each
  (Retry check, Open drop folder, Cancel job). A failed check never starts the build.
- **Persistence:** the waiting state and drop-folder identity live in the journal. The
  job survives quitting and restarting the library. On resume, every item is
  revalidated; earlier results are not trusted.
- **Isolation:** the library never writes into the user's source, never executes any
  file from the drop folder, and applies size and count budgets and containment checks
  to everything in it.
- **Cancel:** removes the drop folder only if the job created it, and only after
  confirmation. Cancelling never touches the user's source.
- **Feedback restraint:** status announcements happen on meaningful changes (an item
  becomes verified or fails, and the final result), not on every file event.

## Design constraints

- Keep original user sources untouched. All extraction, assembly and preparation happen
  in staging owned by the installation job.
- Keep game-specific knowledge in `games/<id>/game.toml` and explicit game helpers.
  Generic library and host code must not name games.
- Recognition and translation support are separate: identifying a stock or older
  release does not make its executable compatible with the translated build.
- Keep exact executable and auxiliary-module validation. Never substitute a guessed
  version or silently patch a user's files.
- Persisted paths inside an installed folder are relative to the game folder. Absolute
  paths appear only in the library registry and transient job state.
- Repository rules: no tests unless asked; build through `tools/build.py`, including the
  windowed app when affected; obtain permission before running any app.

## Implementation baseline before this work

Reviewed against the checkout before this plan's implementation. These are starting
points, not completed steps.

- `tools/library.py` uses first-match, case-sensitive folder discovery and extension-based
  ZIP/Inno extraction. `prerequisites()` requires the user to install `innoextract` for
  GOG installers; the kit ships no extraction tools today.
- It builds in the destination, publishes only `app/`, and launches with an absolute
  `--exe` path.
- Its records are `library.json`, `import-source.json`, `imported.json`, `work.json`,
  `installing.json`, `ready.json`, and registry `location.json`.
- `runtime/layout.cpp` can classify an installed app under a checkout as a developer run.
  `host/game_path.cpp` has no installed-manifest resolution; desktop resolution is CLI
  flag, environment, checkout, then saved path. A supplied data root is a separate
  mobile-only override.
- Existing archive containment checks, app rollback and binary validation should be
  retained and extended.

## Step 1 Establish the contracts

- [x] Review `tools/library.py`, `tools/game_config.py`, `tools/package_desktop.py`,
  `tools/recomp/finish_bundle.py`, `desktop/library/`, `web/library/`, and the host
  launcher, game-path and layout code.
- [x] Write the schema note below before code changes. Durable records carry
  a schema version; readers reject unknown major versions without
  rewriting them.
- [x] Define `installation.json`, including profile, build, paths, fingerprints,
  sidecars and owned artifacts, with no embedded commands and containment checks.
- [x] Define payload and binary fingerprints.

Completion: source, job, installed-folder and checklist contracts agree in this file,
and the new contract does not promise compatibility with existing development installs.

### Step 1 schema

Durable records use `schema: {major, minor}`; internal UI events need no independent
version contract. Readers accept unknown
minor versions only when they add optional fields without changing required semantics;
they reject an unknown major without rewriting the record. Source
inspection returns a versioned source kind and payload identity, diagnostics, and
candidates. Each candidate records its content root, executable and data layout,
fingerprints, evidence, preparation IDs, and one status: `supported`,
`preparation_required`, `recognized_unsupported`, `incomplete`, or `unknown`.
Preparation records use stable IDs and state their validated inputs and outputs; their
actions are code, never shell commands from game data.

Structured errors contain a stable code, stage, affected relative path when applicable,
short message, technical details, and recovery actions. A versioned job journal records
job ID, game, source identity/record, recipe identity, mode (`automatic` or `assisted`),
stage, validated inputs/outputs and errors. Assisted journals also persist the owned
drop-folder identity and checklist item identities. Settled verification results are
recomputed on resume; only an explicit Continue after all required items verify advances
to building. Stages are `inspecting`, `discovering`,
`preparing`, `extracting`, `copying`, `validating`, `building`, `publishing`,
`cleaning`, `waiting_for_input`, `cancelling_assisted`, `complete`, `failed`, and
`cancelled`. The checklist
record declares required and optional items with stable IDs, purpose, accepted forms,
and acquisition guidance. Assisted item states are `missing`, `settling`, `checking`,
`verified`, `wrong_version`, or `unreadable`; automatic and assisted modes share the
same validators.

The versioned `installation.json` is the installed-folder contract. It records game ID;
a stable profile ID stored in the manifest and used as the key beneath the OS user-data
root, plus an optional relative profile-root suffix; recipe and build identities, kit
revision and a digest of local build inputs; relative game content/data roots, executable
path, and native launch paths for the game and sidecars; executable and auxiliary-module
paths with `{size, sha256}` fingerprints; declared launch roles; sidecars that refer only
to those roles; and owned relative artifacts. It contains no commands. The default
profile ID preserves the existing app-name user-data key. Every installed-folder path
must be relative, normalized and contained after symlink resolution; profile suffixes
must be relative and contained under the selected platform profile directory. The
registry stores the absolute installed-folder location and references the installation
manifest. Job paths may be absolute transiently. Cleanup paths must be explicitly owned
by the journal and proven inside job staging or the journaled publication backup area
under `.recomp/` before use.

Payload identity is a deterministic digest over sorted content-layout entries (normalized
relative path, size and SHA-256), excluding the selected outer folder or archive filename.
It includes every file read for preparation, validation or the build. Executable and
auxiliary-module fingerprints always include exact size and SHA-256. Legacy
`library.json`, `location.json`, `import-source.json`,
`imported.json`, `ready.json`, `work.json` and `installing.json` are not inputs to the
new registry, source, installed-facts or journal contracts. Existing development
installations can be recreated; publication rollback remains required for updates made
under the new contract.

## Step 2 Describe game knowledge

- [x] Extend existing `game.toml` sections instead of adding parallel configuration.
  Validate new fields centrally in `tools/game_config.py`, with errors naming the game
  and key.
- [x] Allow discovery hints: install names, store IDs, candidate executable names, known
  version fingerprints, essential files, and data needed outside the executable's
  directory. Config covers names, executable candidates, fingerprints, contained
  executable/data roots, and required files/directories. Installer and media layout
  recognition is deferred to the evidence-backed adapters in Step 4.
- [x] Describe each known version as supported, preparation required, recognised but
  unsupported, or incomplete. Unidentified versions are unknown; the closest candidate
  is shown only as a hint.
- [x] Describe preparation with a stable identifier, applicability conditions,
  user-facing explanation, required inputs or tools, and validated outputs. Keep complex
  actions in explicit helpers, not shell strings in TOML.
- [x] Describe the assisted-mode checklist per game: required and optional items, their
  identities, and how each is obtained. This feeds Step 6.
- [x] Black & White: record the evidence-backed stock 1.2 executable identity and the
  1.42 supported target. The installed-folder route is supported; installer and disc
  media stay assisted. Exact patch order and content deltas remain unverified, so guidance
  stays manual and makes no sufficiency claim (Step 6 and Step 9).
- [x] Civilization IV: the recipe-pinned `msxml3.dll` is read and hash-checked by the
  loader. A matching copy exists in the inspected sibling game folder, but its
  provenance is unknown. Winetricks is listed only as a manual acquisition route; do not
  claim stock-install provenance or a Wine runtime requirement. Evidence: `runtime/loader.cpp`,
  `map_aux_module` (around line 453) and the unconditional auxiliary mapping loop (around
  line 1042).
- [x] Civilization IV: capture the parent-directory data requirements, including base
  game assets and fonts, so importing Beyond the Sword produces a complete layout.

Completion: game-specific guidance is backed by known identities and requirements;
recognition never bypasses supported-binary checks.

Validation for this metadata pass: all six game configs loaded with
the project Python environment, and `git diff --check` passed. The shared native
discovery changes were validated by the windowed app build; no app was run.

## Step 3 Build source inspection and discovery

- [x] Accept folders, executables, installers, archives and disc images through one
  backend entry point. Identify PE files and bounded archive, cabinet, Inno and disc
  signatures by bytes; extensions are hints only. Safe archive listing and extraction
  remain Step 4 work.
- [x] Share native Steam, GOG-folder, common-location and Wine search roots through a
  declarative data file. Windows registry lookup stays native; Python's Windows and Linux
  discovery-provider completeness remains unverified.
- [x] Search selected folders deterministically: case-insensitive matching,
  deduplication, bounded depth and work, and cancellation. Never follow a symlink out of
  the selected root. Report inaccessible locations and search limits without discarding
  valid candidates elsewhere.
- [x] Collect and validate candidates before selecting one. Report executables, auxiliary
  modules, essential data, preparation requirements and useful rejection reasons. Let
  the caller choose by stable candidate reference when several viable copies remain.
- [x] Selecting an original executable inspects its containing folder. Installer and
  media selections report bounded sibling payload names, sizes and extension hints;
  payload listing and assembly remain Step 4 work.

Completion: a valid copy cannot be hidden by an earlier unsupported executable.

Step 3 integration ownership: Step 4 owns safe archive/media listing, extraction and
verified source-layout assembly. ZIP and Inno signatures were adapter-gated at Step 3.
Candidate IDs bind the canonical content root, actual
executable-relative path and executable digest; full payload identity belongs to Step 5.
Step 7 owns nested and case-preserving installed layouts. The CLI backend is ready; Step 8
owns desktop UI wiring and dispatch. Step 9 owns platform verification. Python discovery
was exercised only on macOS; Windows and Linux provider completeness remains unverified.

Validation for Step 3: six game configs loaded; the real Ghost Recon folder and direct
executable produced the same supported candidate ID. Local Black & White 1.2 was
recognized as requiring `manual_1_42`, and 1.42 as supported. A real-host discovery scan
hit its 60,000-entry cap with no candidate in those configured locations and correctly
remained incomplete, without auto-selection. Python's Windows and Linux provider
completeness remains unverified; the macOS path was exercised. The native windowed app
build passed for the shared launcher and platform changes; no app was run.

## Step 4 Add extraction and disc-image adapters

Progress: the Python library now inspects and extracts top-level ZIP archives with
bounded entry and byte counts, path-collision checks, and rejection of links and
special files. Candidate IDs include the archive digest and member-relative game
layout; installation re-extracts into temporary storage and validates the selected
candidate before copying it. Nested archives, other archive formats, media, and
installer adapters remain unimplemented.

Validation: Python syntax compilation, game-config loading and diff review passed.
No app or build was run; importing a real ZIP remains unverified.

- [ ] Separate probing, listing, extraction and game-layout assembly. Select an adapter
  from evidence, never by treating every `.exe` as a GOG installer. Record verified
  installer/media layouts and assembly rules in the existing game setup configuration
  alongside each adapter.
- [ ] Keep ZIP support. Add a common archive backend using libarchive for the formats
  it covers. Document the shipped capabilities in the README.
- [ ] Use innoextract for supported Inno/GOG installers. Detect missing multipart
  payloads and any RAR dependency before extraction starts.
- [ ] Add InstallShield cabinet support through unshield. Evaluate the linked C API
  against a subprocess before choosing; the linked route needs its MIT notice shipped.
  Recognise cabinet media separately from arbitrary `setup.exe` files. Apply
  game-specific assembly rules only where verified. Never execute an installer as a
  fallback.
- [ ] Add disc-image adapters for `.iso` and `.bin`/`.cue`. Plain ISO 9660 is read
  through libarchive, whose ISO reader is partial (Rock Ridge only partly supported,
  and it cannot read all images). Raw 2352-byte sector tracks need a reader that strips
  sync and header bytes before the ISO layer. `.cue` sheets need a parser of their own.
  Verify each variant against real dumps, and report unsupported variants (for example,
  unexpected sector sizes or multiple tracks) with a named reason.
- [ ] Rediscover content after extraction, so archives containing installers or further
  archives are supported. Limit nesting depth, file count, bytes and total work.
- [ ] Apply containment, link and filename-collision policies in every adapter. Prevent
  traversal and writes through links during extraction itself, including by external
  tools; post-extraction checks cannot undo an escaped write. Define handling for
  case and Unicode collisions, absolute paths, special files and duplicate entries.
- [ ] Check only the tools the chosen source needs. Give precise recovery instructions
  for missing optional tools. Verify each adapter against a real or evidence-backed
  sample before claiming coverage.

Completion: supported inputs converge on one validated installation candidate, and
unsupported inputs produce an actionable explanation or route to assisted mode.

## Step 5 Make jobs journaled and recoverable

Progress: the Python backend now stages folder and ZIP installs under job-owned
directories, fingerprints bounded source and recipe inputs, journals retries, and
uses installed facts as the publication commit record. It now keeps bounded attempt
logs, preflights additional volume space, keeps temporary files under the job, and
monitors staged build growth with a best-effort ceiling. Validated retries retain
build outputs; dependency caches use their normal toolchain locations. The monitor
can overshoot between scans, and shared toolchain caches are not counted.
Strict per-job filesystem quotas are outside this milestone. Step 9
will validate the current monitor and exercise interruption recovery, UI actions and
cleanup failure end to end. Windows process and lock guarantees remain unverified.

Validation: Python and JavaScript syntax checks, game-config loading, a real Ghost
Recon recipe fingerprint, diff checks, the library build and the windowed app target
passed. The app was not launched; the space monitor and recovery flow have not been
exercised end to end.

- [ ] Journal each completed stage with its validated inputs and outputs. Resume only
  when source identity and recipe still match; otherwise explain what must be repeated.
- [ ] Revalidate copied and prepared executable and auxiliary-module hashes before
  building and before publication. Fingerprint all effective build inputs, including
  local helper changes, not just a kit revision or source path.
- [ ] Return structured errors with code, stage, affected path, concise explanation,
  technical details and recovery actions. Cover wrong versions, missing data or
  volumes, unsupported formats, permissions, space and failed tools. Keep hashes and
  process output in expandable details.
- [x] Offer recovery actions: Choose another copy, Locate missing file, Change
  destination, Recheck preparation, Retry failed stage, and Open log.
- [ ] Budget simultaneous extraction, copying, build outputs, dependency builds and
  retained backups. Enforce limits during extraction, not only in preflight.
- [ ] Give cancellation a bounded process-tree shutdown with escalation. Lock the
  destination across library instances and storage roots, so recovery cannot clean a
  live job. Reject source, destination and staging overlap before any write or cleanup.
  Delete only paths proven to belong to the job, never data inferred from a malformed
  record.
- [ ] Keep per-attempt logs under `.recomp/logs/`, rotated by count and size. Distinguish
  user cancellation from failure. Treat cleanup failure after successful publication as a
  warning, with Play still available.

Completion: interruption leaves either the previous usable app or an explicitly
recoverable installation, and each next action states what it will do.

## Step 6 Implement assisted mode

Depends on Steps 1, 2 and 5. Implements the requirements in "Assisted mode requirements".

- [x] Add the waiting-for-input stage to the job model and journal, and persist the
  drop-folder identity with it.
- [x] Generate the drop folder and the plain-text checklist from the game's checklist
  definition (Step 2). Include the exact path and the expected layout.
- [ ] Preserve the Black & White installed-folder route and assisted media route. Keep
  patch guidance manual and limited to the pinned 1.42 target; do not claim a verified
  patch order, content deltas or sufficiency. Retain the limitation if evidence is absent.
- [x] Implement the folder watcher with settle detection, per-item states, and
  validation through the same validators as the automatic path.
- [x] Gate the build on Continue, which is enabled only when all required items verify.
  Run every preparation step that does not need the drop folder before the gate.
- [x] Implement FAIL results with per-item structured errors and recovery actions.
- [x] Implement resume-after-restart with full revalidation, and cancel with ownership
  checks.
- [ ] Enforce the isolation rules: no writes to the user's source, no execution of drop
  folder content, and size, count and containment budgets.

Completion: a user can supply content by hand, see exactly what is missing, and reach
OK or a clear FAIL without the library ever starting a build on unverified input.
Backend and desktop UI changes compile; runtime folder-watcher, restart, and cancellation
exercises remain for Step 9. Strict filesystem quotas are outside this milestone; the
current build-output ceiling is best-effort and does not cover global compiler caches.

## Step 7 Publish a movable game folder and sidecars

- [x] Use this macOS layout:

  ```text
  Game Name/
    Game Name.app
    Configure Game Name.app
    original/
    .recomp/
      installation.json
      logs/
  ```

- [x] Preserve game-relative directory relationships under `original/`, recording the
  executable's relative location, including nested expansion folders.
- [ ] Keep Windows and Linux binaries with their resources and libraries. Package
  generation and installed path resolution must agree on each platform. Audit transitive
  native dependencies, auxiliary translations and bundled mods for checkout or build
  paths. Folder portability does not imply cross-platform binary compatibility.
- [ ] On macOS, verify that a copied or moved folder still launches: quarantine
  attributes on downloaded bundles, the ad-hoc signature after a move, and Gatekeeper
  prompts for the app and the Configure app. Finish bundle changes before signing; verify
  signatures and executable permissions.
- [x] Resolve the manifest from the app's own location, independently of the working
  directory. On macOS, the game folder is the parent of the outer `.app`, so the manifest
  is `<game folder>/.recomp/installation.json`. Precedence, documented in the README:
  explicit CLI flag, environment override, installed manifest, checkout detection, then
  developer defaults. An installed manifest outranks checkout detection even beneath a
  repository. Record overrides in the launch log.
- [x] Invalid installed data is reported with the failing path and check. Never silently
  select another copy. Library Play and direct launches use the same resolution code.
- [x] Make Configure a small native launcher for the main app's `--configure` mode.
  Describe sidecars declaratively, resolve their targets relative to the game folder, and
  share the game's profile identity.
- [x] Keep platform profile locations as the default. Profile identity comes from the
  manifest, so moving the folder on the same machine keeps access to its profile.
- [x] Publish the app, sidecars and manifest as one recoverable update, preserving game
  data, mods and profiles. Stage on the destination filesystem even when building on
  another volume. Sequence:
  1. build and verify the complete new layout in `.recomp/staging-<job>/`;
  2. move the existing app and sidecars to `.recomp/backup-<job>/`;
  3. move the verified new app and sidecars into place;
  4. atomically replace `installation.json` last. This is the commit point. Before it,
     rollback moves the backup back; after it, only cleanup remains.
  On startup, recovery inspects which step completed and reports the result. Coordinate
  with running game and Configure processes using a lock that records owner PID and
  process start time, so stale locks are detectable.
- [x] Update library registration only after the commit point. The library may locate
  and adopt folders already produced under the new contract without rebuilding them.
- [x] Make library registration a convenience. Offer Locate game folder to repair stale
  records or adopt an existing installation without rebuilding it.

Completion: direct game and Configure launches resolve from the published folder and
library reattachment is optional for play. Moves, rename behavior, Gatekeeper prompts,
Windows/Linux packaging, and end-to-end recovery remain unverified in Step 9. Bundle
construction checks executable permissions, signatures and native dependency paths for
checkout/build references; this does not establish runtime portability.

## Step 8 Build the user flow

- [x] Replace the prerequisite-first flow with Your copy, Review, and Install. Start
  automatic discovery while manual selection is available immediately.
- [x] Offer detected copies, drag and drop, Choose folder and Choose file, with native
  picker differences kept behind the same discovery flow.
- [x] Show what was found: game, version and status, source location, required
  preparation, and destination. Show successful tool checks compactly.
- [x] Show extra preparation only when it applies, with a supported automated action or
  clear manual instructions followed by reinspection. Offer assisted mode here, and
  preserve the user's choices when going back.
- [x] Preview the final game folder, explain that sources are preserved, and show the
  estimated peak and available space per affected volume before Install.
- [x] Make destination selection explicit: a parent directory versus the final game
  folder. Show the exact resulting path before confirmation.
- [x] Provide keyboard access, sensible focus changes and restrained accessible status
  announcements, as set out in the assisted-mode requirements.

Completion: users understand their copy's status and their options before any build
tool is involved. Library and windowed app builds, Python/JavaScript syntax, game
configuration and literal checks, formatting, scoped diff checks, and the built-app
dependency audit passed. No app launches or tests were run.

## Step 9 Validate the completed flow

- [x] Review diffs, configuration validation, path resolution and recovery logic. Build
  only affected library and windowed game targets through the build tool. Metadata-only
  config and documentation changes need config loading and diff review; regenerate and
  rebuild when translation inputs or code actually change.
- [ ] Prepare fixtures outside the repository and out of git: a stock GhostRecon folder, a
  GOG multipart installer set, a supported InstallShield set, an ISO, a raw `.bin`/`.cue`
  pair, a ZIP, a nested archive containing an installer, and two candidate copies under
  one search root.
- [ ] With user permission before any app runs, exercise installed folders, direct
  executable selection, GOG multipart inputs, InstallShield media, disc images, archives,
  nested inputs, multiple candidates and assisted mode end to end.
- [ ] Check Black & White version guidance and Civ IV preparation and parent data against
  actual supported inputs. Verify the Black & White official/fan patch sequence and
  content deltas if evidence permits; otherwise preserve the manual-only limitation.
  Claim adapter coverage only with evidence.
- [ ] Exercise missing files and tools, unsupported versions, low space, cancellation,
  restart recovery during assisted mode, failed updates, interruption at each
  publication step, and cleanup failure, without risking user sources.
- [ ] Move and rename a finished game folder. Launch the game and Configure directly
  without library involvement, then reattach it. Make the old source and build paths
  unavailable, clear path overrides, and launch from an unrelated working directory.
  Include installation beneath a checkout, paths with spaces and non-ASCII characters,
  and a second move after reattachment. Check every affected desktop layout and record
  unavailable platforms as unverified.

Completion: the recorded checks support the shipped capabilities and the portability
contract; unresolved limitations stay visible rather than being approximated.

Review checkpoint (2026-10-17): Python/JavaScript syntax, all six game configurations,
game-literal checks, scoped diff review and the built macOS app's native dependency
audit passed. The library rebuilt successfully. The windowed build passed during
Steps 7–8; this pass changed only Python, UI and documentation. A fresh windowed
build attempt was cancelled while waiting for another build's lock, before it began
building. Read-only inspection recognized the existing Ghost Recon folder as
supported with a complete scan and no diagnostics. A temporary copy of the game app
retained its signature and executable permission after renaming its containing folder
to a path with spaces and non-ASCII characters. The bundle had no quarantine attribute;
no application was launched. This does not verify Configure launches, Gatekeeper,
profiles after relocation, reattachment or interruption recovery. Source fixtures and
end-to-end exercises remain pending; Black & White patch sequencing and actual Civ IV
imports remain unverified. Review corrected the adopted-folder display and stopped
post-commit cleanup recovery from requiring mutable game data to remain byte-identical.

## Step 10 Document, commit and delete this plan

- [x] Fold only durable configuration and packaging guidance into existing docs,
  including the shipped tool notices. Keep game-specific evidence with that game's
  configuration and helpers.
- [x] Commit kit changes in the kit. Update the parent gitlink in a separate commit.
  Keep assets, binaries and local logs ignored.
- [ ] Delete `DESKTOP_INSTALL_PLAN.md` once implementation, validation and durable
  documentation are complete. Do not delete it merely because work is paused.

The README records the macOS installed layout, resolution order, profile identity,
publication and reattachment contract, and verification limits. Game-specific setup
guidance stays in the Black & White and Civilization IV READMEs. No extraction-tool
binaries were added; their bundling and notices remain part of unfinished Step 4.
Keep this plan for the holistic review while earlier implementation and runtime
validation items remain open.

Cleanup review (2026-10-17): fixed assisted Continue, recovery actions and nested
layout validation; interrupted assisted builds return to preparation for fresh
validation and explicit Continue. Rebuilds require the installed original copy.
Space estimates no longer hash payloads, validated retries retain build outputs,
and game recipe identities exclude library UI inputs. Native launchers share path
checks; runtime launch validation no longer depends on installer ownership records.
Python/JavaScript syntax, all game configs, game-literal and formatting checks,
diff review, and library/windowed app builds passed. No tests or apps were run;
the end-to-end exercises in Step 9 remain pending.

## Open questions

- unshield: linked library or subprocess (Step 4 evaluation).
- libarchive: whether it is needed beyond ZIP and plain ISO, once disc-image adapters
  exist.
- Black & White: official/fan patch order and content deltas; Step 6 keeps preparation
  manual, and Step 9 verifies evidence or retains the limitation (Evidence section).
- Black & White repack: identify the `ArC\x01` payload format, then support it or decline
  it with a named reason.
- Windows and Linux packaging: a follow-up pass after macOS.

## Evidence

Evidence gathered from the user's own files. These are not in the repository and are
recorded only as facts about the sources.

The external references below support the signature probes; they make no claims about
uninspected installer payloads.

- Bounded Inno loader signatures were checked against
  [innoextract's loader offsets](https://raw.githubusercontent.com/dscharrer/innoextract/master/src/loader/offsets.cpp);
  InstallShield Cabinet identification uses the `ISc(` signature in
  [unshield's cabinet header](https://raw.githubusercontent.com/twogood/unshield/master/lib/cabfile.h).

- **4x4 Evolution 2 disc:** the `.cue` declares one track, `MODE1/2352`. The `.bin` is
  227,957 sectors with no remainder. Sector 0 has the Mode 1 sync pattern and header.
  Sector 16 carries `CD001` at user-data offset 0, so user data is bytes 16..2063 of each
  sector. Stripping that gives a 466,855,936-byte ISO, which the system `bsdtar`
  (libarchive) lists in full: 29 entries matching the already-extracted folder. The disc
  is an InstallShield installer (`Setup.exe`, `_sys1.cab`, `data1.cab`, `_user1.cab`), so
  the cabinets are the unshield path and the installer is never executed.
- **Black & White fan patch:** `BWFanPatchInstaller.exe` is an unofficial community patch
  (bwfiles.com, April 2019 readme, "UNOFFICIAL PATCH"), not a Lionhead or EA release. It is
  an NSIS installer whose payload overlays `runblack.exe` (8,663,040 bytes, SHA-256
  `3f58bc02…d48aba`), `d3dim.dll`, `d3dim700.dll`, `LHLogR.dll`, `LHMultiplayerR.dll`,
  `Setup.exe`, and the `Data/`, `Plug Ins/` and `Scripts/` folders. Its readme refers to
  official patch notes v1.10 and v1.20, so it layers on earlier patches.
- **Black & White official patches:** `Black_White_Patch_v1.100.exe` and
  `black_white_patch_v1_20.exe` sit beside the fan patch. Their order relative to 1.42 is
  unverified.
- **Black & White crack folder:** `BNW/crack/runblack.exe` (SHA-256 `86a30b08…f8f03cf`,
  8,500,623 bytes) is a different executable from the fan patch's `runblack.exe`. It is
  not a supported candidate.
- **Black & White repack ISO:** `Black and White Complete Collection.iso` (1.2 GB) has an
  `autorun.inf` that launches `setup.exe`, plus `elamigos-*.bin` payloads, one per
  language. Their header is `ArC\x01`, a format no current adapter is known to read.
  Its `setup.exe` is never run.
- **Black & White variants:** the user holds several ISO and patch variants. The French
  disc is the one that contained the wanted files.
