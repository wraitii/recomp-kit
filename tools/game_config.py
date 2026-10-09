"""Load a game directory: games/<id>/game.toml, with an optional curated globals file.

This module is the only place that knows the schema. Python 3.9 has no
tomllib, so the pinned tomli is the fallback."""

import os
from pathlib import Path
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

KIT = Path(__file__).resolve().parents[1]
REQUIRED_GAME_KEYS = ("id", "name", "app_name", "bundle_id", "executable", "sha256",
                      "image_base", "entry_point", "guest_root")

HEAP_BASE_DEFAULT = 0x01000000

# The settings page's rows, in mods/display_settings.h DisplayRow order; the
# controls' seven rows are one entry. [settings] rows names the ones a game
# shows. Without the key a game shows every row. "keypad" is the pre-touch-
# controls spelling of "controls"; SETTINGS_ROW_ALIASES keeps it loading.
SETTINGS_ROWS = ("rendering", "ui_scale", "wide_view", "window", "resolution", "frame_limit",
                 "performance_overlay", "textures", "filtering", "controls")
SETTINGS_ROW_ALIASES = {"keypad": "controls"}

# [controls] default_layout and the on-screen layout picker's choices.
CONTROLS_LAYOUTS = ("pad", "keys", "pad+keys", "hidden")
# [controls.mapped] left_stick/right_stick/dpad modes.
STICK_MODES = ("cursor", "arrows", "horizontal_arrows", "wasd", "scroll", "wheel", "none")
# [controls.native] buttons: the pad button names a physical button maps to.
PAD_BUTTONS = ("cross", "circle", "square", "triangle", "l1", "r1", "l2", "r2", "l3", "r3",
               "select", "start", "ps")
MAPPED_DEFAULTS = {"left_stick": "arrows", "right_stick": "cursor", "dpad": "arrows",
                   "cursor_speed": 900, "cross": "mouse_left", "circle": "mouse_right",
                   "square": "key:Space", "triangle": "key:Tab", "l1": "key:PageUp",
                   "r1": "key:PageDown", "l2": "mouse_middle", "r2": "key:LShift",
                   "start": "key:Escape", "select": "key:F10", "l3": "none", "r3": "none",
                   "ps": "action:settings"}
# [controls.native] axes: the six DirectInput/XInput axis names a physical
# axis maps to.
NATIVE_AXES = ("x", "y", "z", "rx", "ry", "rz")
# Mirrors the name column of host/controls/layout.cpp's kScancodes table (its
# scancode_from_name); keep both lists in sync.
KEY_NAMES = (
    "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P", "Q", "R", "S",
    "T", "U", "V", "W", "X", "Y", "Z", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "Return",
    "Escape", "Backspace", "Tab", "Space", "Minus", "Equals", "LeftBracket", "RightBracket",
    "Backslash", "Semicolon", "Apostrophe", "Grave", "Comma", "Period", "Slash", "F1", "F2", "F3",
    "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "Insert", "Home", "PageUp", "Delete",
    "End", "PageDown", "Right", "Left", "Down", "Up", "LCtrl", "LShift", "LAlt",
)
# key:<name>, mouse_left/right/middle, wheel_up/down, action:<name> or none.
BUTTON_TARGET_RE = re.compile(
    r"^(key:[A-Za-z0-9]+|mouse_(left|right|middle)|wheel_(up|down)|"
    r"action:(settings|system_keyboard|edit_layout)|none)$")
HEAP_END = 0x0e000000        # default heap end; reserved low runtime regions start here
GUEST_SIZE_DEFAULT = 0x10000000   # runtime/x86.h GUEST_SIZE: the arena, 256 MB unless a module needs more

# The production translation profile: every optimization the kit has. load()
# fills these when a game omits them. fault_state = "exact" gives up the
# relaxed SSA scalar/state policies.
TRANSLATE_DEFAULTS = {
    "fault_state": "relaxed",
    "msvc_x87_convention": True,
    "call_contracts": True,
    "x87_cw_clone": True,
}

def add_game_args(parser):
    """--game <id> for a kit game under games/, or --game-dir for any directory holding game.toml."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--game", help="A game under the kit's games/ directory")
    group.add_argument("--game-dir", type=Path, help="The directory holding game.toml")
    parser.add_argument("--build-root", type=Path, default=None,
                        help="Where outputs, the installation link live")


def resolve_game_args(args, default="stub"):
    """Fill args.game_dir and args.build_root, exporting the build root to child tools."""
    if args.game_dir is None:
        if not (args.game or default):
            raise SystemExit("Pass --game <id> or --game-dir <directory>")
        args.game_dir = KIT / "games" / (args.game or default)
    args.game_dir = args.game_dir.resolve()
    if args.build_root is not None:
        os.environ["RECOMP_BUILD_ROOT"] = str(args.build_root.resolve())
    args.build_root = build_root_for(args.game_dir)
    return args


def build_root_for(game_dir):
    """RECOMP_BUILD_ROOT when set; else beside a game outside the kit, or build/<id> for a kit game."""
    if os.environ.get("RECOMP_BUILD_ROOT"):
        return Path(os.environ["RECOMP_BUILD_ROOT"])
    game_dir = Path(game_dir).resolve()
    try:
        game_dir.relative_to(KIT)
    except ValueError:
        return game_dir / "build"
    return KIT / "build" if game_dir == KIT / "games/stub" else KIT / "build" / game_dir.name


def windows_version(value):
    """Decode major.minor[.build]; keep the historical 9x default and 6.1 SP1."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("[game] windows_version must be major.minor[.build]")
    parts = [int(part) for part in value.split(".")]
    major, minor = parts[:2]
    build = parts[2] if len(parts) == 3 else {(4, 10): 2222, (6, 1): 7601}.get((major, minor), 0)
    if major > 255 or minor > 255 or build > 32767:
        raise ValueError("[game] windows_version requires byte-sized major/minor and a 15-bit build")
    return major, minor, build, 2 if major >= 5 else 1


def validate_heap_base(value, heap_end=HEAP_END):
    """The heap arena start: page aligned, above the image base, below the arena end."""
    if value % 0x1000 or not (0x00400000 < value < heap_end):
        raise ValueError("[game] heap_base %#x must be page aligned and between 0x00400000 and %#x" % (value, heap_end))
    return value


def load_controls(controls, touch, source):
    """Validate [controls], merging [controls.mapped]/[controls.native] over their
    defaults. `touch` is the already-defaulted [touch] table: its `keypad` knob
    only sets `default_layout` when the game has not named one itself."""
    controls = dict(controls)
    if "default_layout" not in controls:
        controls["default_layout"] = {"auto": "keys", "hidden": "hidden"}[touch["keypad"]]
    if controls["default_layout"] not in CONTROLS_LAYOUTS:
        raise ValueError("%s: [controls] default_layout must be one of %s, not %r"
                         % (source, ", ".join(CONTROLS_LAYOUTS), controls["default_layout"]))
    pad = controls.setdefault("pad", "mapped")
    if pad not in ("native", "mapped", "off"):
        raise ValueError('%s: [controls] pad must be "native", "mapped" or "off", not %r' % (source, pad))

    mapped = dict(MAPPED_DEFAULTS)
    mapped.update(controls.get("mapped", {}))
    unknown = sorted(k for k in mapped if k not in MAPPED_DEFAULTS)
    if unknown:
        raise ValueError("%s: [controls.mapped] may name only %s, not %s"
                         % (source, ", ".join(sorted(MAPPED_DEFAULTS)), ", ".join(unknown)))
    for key, value in mapped.items():
        if key == "cursor_speed":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("%s: [controls.mapped] cursor_speed must be a positive integer, not %r"
                                 % (source, value))
        elif key in ("left_stick", "right_stick"):
            if value not in STICK_MODES:
                raise ValueError("%s: [controls.mapped] %s must be one of %s, not %r"
                                 % (source, key, ", ".join(STICK_MODES), value))
        elif key == "dpad":
            if value not in ("arrows", "wasd", "none"):
                raise ValueError('%s: [controls.mapped] dpad must be "arrows", "wasd" or "none", not %r'
                                 % (source, value))
        elif not isinstance(value, str) or not BUTTON_TARGET_RE.match(value):
            raise ValueError("%s: [controls.mapped] %s is not a valid target: %r" % (source, key, value))
        elif value.startswith("key:") and value[len("key:"):] not in KEY_NAMES:
            raise ValueError("%s: [controls.mapped] %s names no key: %r" % (source, key, value))
    controls["mapped"] = mapped

    native = dict(controls.get("native", {}))
    unknown_native = sorted(k for k in native if k not in ("xinput", "dinput", "axes", "buttons"))
    if unknown_native:
        raise ValueError("%s: [controls.native] may name only xinput, dinput, axes, buttons, not %s"
                         % (source, ", ".join(unknown_native)))
    native.setdefault("xinput", True)
    native.setdefault("dinput", True)
    if not isinstance(native["xinput"], bool) or not isinstance(native["dinput"], bool):
        raise ValueError("%s: [controls.native] xinput and dinput must be booleans" % source)
    axes = native.setdefault("axes", ["x", "y", "z", "rz", "rx", "ry"])
    if not isinstance(axes, list) or sorted(axes) != sorted(NATIVE_AXES):
        raise ValueError("%s: [controls.native] axes must list all six of %s exactly once, not %r"
                         % (source, ", ".join(NATIVE_AXES), axes))
    buttons = native.setdefault(
        "buttons", ["square", "cross", "circle", "triangle", "l1", "r1", "l2", "r2", "select",
                   "start", "l3", "r3", "ps"])
    if not isinstance(buttons, list) or sorted(buttons) != sorted(PAD_BUTTONS):
        raise ValueError("%s: [controls.native] buttons must list all thirteen of %s exactly once, not %r"
                         % (source, ", ".join(PAD_BUTTONS), buttons))
    controls["native"] = native
    return controls


def load(game_dir, build_root=None):
    """Return the parsed config with `globals` merged in and `dir`/`source` recorded."""
    game_dir = Path(game_dir)
    build_root = Path(build_root) if build_root is not None else build_root_for(game_dir)
    source = game_dir / "game.toml"
    with source.open("rb") as fh:
        cfg = tomllib.load(fh)
    game = cfg.get("game", {})
    missing = [key for key in REQUIRED_GAME_KEYS if key not in game]
    if missing:
        raise ValueError("%s: missing [game] keys: %s" % (source, ", ".join(missing)))
    game["heap_end"] = int(game.get("heap_end", HEAP_END))
    game["heap_base"] = validate_heap_base(int(game.get("heap_base", HEAP_BASE_DEFAULT)),
                                           game["heap_end"])
    windows_version(game.setdefault("windows_version", "4.10"))
    # Fail loudly rather than returning 0 from an import whose stdcall arity is
    # unknown: the un-popped arguments otherwise drift the guest stack.
    # Store hooks (the guest watchpoint and DirectDraw dirty tracking) cost a
    # test and an aliasing reload on every guest store. Off, RECOMP_WATCH is
    # unavailable and a DirectDraw Unlock compares the whole surface.
    if not isinstance(game.setdefault("store_hooks", True), bool):
        raise ValueError("%s: [game] store_hooks must be a boolean" % source)
    strict_imports = game.setdefault("strict_imports", False)
    if not isinstance(strict_imports, bool):
        raise ValueError("%s: [game] strict_imports must be a boolean" % source)
    translate = cfg.setdefault("translate", {})
    # "relaxed" keeps CPU and x87 state in host locals between observation
    # points, so an interior fault may see stale scratch state (DIVERGENCE
    # tags cpu-locals, ssa-x87-scalar, ssa-state-locals); "exact" publishes it
    # at every instruction. Null-check builds always use the exact form.
    if translate.setdefault("fault_state", TRANSLATE_DEFAULTS["fault_state"]) not in ("relaxed", "exact"):
        raise ValueError('%s: [translate] fault_state must be "relaxed" or "exact"' % source)
    # Calls and returns follow the MSVC x87 stack convention: popped x87
    # residue is dead there (DIVERGENCE ssa-x87-convention). Arithmetic flags
    # stay published either way. False restores conservative publication.
    if not isinstance(translate.setdefault("msvc_x87_convention", TRANSLATE_DEFAULTS["msvc_x87_convention"]), bool):
        raise ValueError("%s: [translate] msvc_x87_convention must be a boolean" % source)
    # Per-function reads/kills contracts let a direct CALL omit publication of
    # a field the callee neither reads nor preserves (DIVERGENCE
    # ssa-call-contracts). False publishes every field at every call.
    if not isinstance(translate.setdefault("call_contracts", TRANSLATE_DEFAULTS["call_contracts"]), bool):
        raise ValueError("%s: [translate] call_contracts must be a boolean" % source)
    # FP-heavy SSA bodies get a fast clone specialised for PC = RC = 0 that
    # falls back to the general body when the guest CW differs. Exact; false
    # emits the general body alone.
    if not isinstance(translate.setdefault("x87_cw_clone", TRANSLATE_DEFAULTS["x87_cw_clone"]), bool):
        raise ValueError("%s: [translate] x87_cw_clone must be a boolean" % source)
    alts = translate.setdefault("alternate_entries", [])
    if not isinstance(alts, list) or not all(type(v) is int for v in alts):
        raise ValueError("%s: [translate] alternate_entries must be a list of addresses" % source)
    tracks = cfg.setdefault("media", {}).setdefault("cd_tracks", [])
    if not isinstance(tracks, list) or not all(isinstance(v, str) for v in tracks):
        raise ValueError("%s: [media] cd_tracks must be a list of strings" % source)
    cfg.setdefault("hooks", {})
    render = cfg.setdefault("render", {})
    if not isinstance(render.setdefault("d3d8_wgpu", False), bool):
        raise ValueError("%s: [render] d3d8_wgpu must be a boolean" % source)
    input_config = cfg.setdefault("input", {})
    relative_capture = input_config.setdefault("relative_mouse_capture", True)
    if not isinstance(relative_capture, bool):
        raise ValueError("%s: [input] relative_mouse_capture must be a boolean" % source)
    cfg.setdefault("bundle", {}).setdefault("exclude", [])
    touch = cfg.setdefault("touch", {})
    touch.setdefault("keypad", "auto")
    if touch["keypad"] not in ("auto", "hidden"):
        raise ValueError('%s: [touch] keypad must be "auto" or "hidden", not %r' % (source, touch["keypad"]))
    cfg["controls"] = load_controls(cfg.get("controls", {}), touch, source)
    settings = cfg.setdefault("settings", {})
    rows = settings.setdefault("rows", list(SETTINGS_ROWS))
    if not isinstance(rows, list):
        raise ValueError("%s: [settings] rows must be a list, not %r" % (source, rows))
    rows = [SETTINGS_ROW_ALIASES.get(row, row) for row in rows]
    unknown = [row for row in rows if row not in SETTINGS_ROWS]
    if unknown:
        raise ValueError("%s: [settings] rows may name only %s, not %s"
                         % (source, ", ".join(SETTINGS_ROWS), ", ".join(map(repr, unknown))))
    settings["rows"] = rows
    launcher = cfg.setdefault("launcher", {})
    launcher.setdefault("title", game["name"])
    launcher.setdefault("store", "")
    for key in ("install_names", "gog_ids", "steam_ids"):
        value = launcher.setdefault(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("%s: [launcher] %s must be a list of strings" % (source, key))
    for key in ("title", "store"):
        if not isinstance(launcher[key], str):
            raise ValueError("%s: [launcher] %s must be a string" % (source, key))
    min_free = launcher.setdefault("min_free_mb", 0)
    if not isinstance(min_free, int) or min_free < 0:
        raise ValueError("%s: [launcher] min_free_mb must be a non-negative integer" % source)
    setup_dirs = cfg.setdefault("setup", {}).setdefault("required_dirs", [])
    if not isinstance(setup_dirs, list):
        raise ValueError("%s: [setup] required_dirs must be a list" % source)
    curated = {}
    globals_path = game_dir / translate.get("globals", "globals.toml")
    if globals_path.is_file():
        with globals_path.open("rb") as fh:
            curated = tomllib.load(fh)
    cfg["globals"] = {**curated.get("globals", {}), **cfg.get("globals", {})}
    cfg["curated"] = {key: value for key, value in curated.items() if key != "globals"}
    cfg["dir"] = game_dir
    cfg["source"] = str(source)
    cfg["build_root"] = build_root
    exe = game.get("developer_exe")
    cfg["developer_exe_path"] = ((game_dir / exe) if exe else build_root / "original" / game["executable"]).resolve()
    code_map = translate.get("code_map")
    if code_map is not None and (not isinstance(code_map, str) or not code_map):
        raise ValueError("%s: [translate] code_map must be a non-empty path" % source)
    cfg["code_map_path"] = (game_dir / code_map).resolve() if code_map else None
    # [translate] overrides: a header the generated sources include before they
    # define FN_<addr>, so a game can replace one translated function with a
    # native one (output.py's RECOMP_OVERRIDE_HEADER). Absent by default,
    # and required to exist when named: a path that silently does not resolve
    # would leave the build looking replaced while running the original.
    overrides = translate.get("overrides")
    cfg["overrides_header"] = None
    if overrides is not None:
        path = (game_dir / overrides).resolve()
        if not path.is_file():
            raise ValueError("%s: [translate] overrides names no file: %s" % (source, path))
        cfg["overrides_header"] = path
    validate_guest_layout(cfg, source)
    return cfg


def validate_guest_layout(cfg, source):
    game = cfg["game"]
    guest_size = int(game.setdefault("guest_size", GUEST_SIZE_DEFAULT))
    if guest_size % 0x1000 or guest_size < GUEST_SIZE_DEFAULT or guest_size > 0xfffff000:
        raise ValueError("%s: [game] guest_size %#x must be page aligned and between %#x and 0xfffff000"
                         % (source, guest_size, GUEST_SIZE_DEFAULT))
    game["guest_size"] = guest_size
    heap_base, heap_end = game["heap_base"], game["heap_end"]
    if heap_end % 0x1000 or heap_end > guest_size:
        raise ValueError("%s: [game] heap_end must be page aligned and fit inside guest_size" % source)
    # The mod heap, stack, callback sentinel, TEB and imports retain their
    # fixed low addresses. A larger heap must lie wholly above this band.
    if heap_base < GUEST_SIZE_DEFAULT and heap_end > HEAP_END:
        raise ValueError("%s: [game] heap overlaps reserved runtime memory [%#x, %#x)"
                         % (source, HEAP_END, GUEST_SIZE_DEFAULT))
