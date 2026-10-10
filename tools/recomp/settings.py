"""Everything that steers one translation, read once from game.toml [translate] and the command line."""
from pathlib import Path

from program import TranslateError


class Settings(object):
    def __init__(self, cfg, allow_unmodelled=None, jobs=1, module=None):
        translate = cfg["translate"]
        if not cfg.get("code_map_path"):
            raise TranslateError("game.toml [translate] code_map is required")
        self.cfg = cfg
        self.curated = {**{"globals." + name: entry for name, entry in cfg["globals"].items()},
                        **cfg["curated"]}
        self.game_dir = Path(cfg["dir"])
        self.exe = cfg["developer_exe_path"]
        self.code_map = Path(cfg["code_map_path"])
        self.module = next((m for m in cfg["aux_modules"] if m["key"] == module), None)
        if module and self.module is None:
            raise TranslateError("game.toml has no [modules.aux.%s]" % module)
        self.base = cfg["game"]["image_base"]
        self.sha256 = cfg["game"]["sha256"]
        if self.module:
            self.exe = self.module["path"]
            self.code_map = self.module["code_map_path"]
            self.base = self.module["base"]
            self.sha256 = self.module["sha256"]
            self.curated = {}
        self.alternate_entries = frozenset(int(a) for a in translate["alternate_entries"])
        self.configured_entries = (frozenset(self.module["entry_points"]) if self.module else
                                   self.alternate_entries | frozenset(int(a) for a in translate.get("entry_points", ())))
        if self.module:
            self.alternate_entries = frozenset()
        native = translate.get("native")
        self.native_header = native.get("header") if isinstance(native, dict) else None
        if self.module:
            self.native_header = None
        self.call_contracts = translate["call_contracts"]
        self.checked_returns = frozenset(translate["checked_returns"]) if not self.module else frozenset()
        self.allow_unmodelled = allow_unmodelled
        self.jobs = jobs
        relaxed = translate["fault_state"] == "relaxed"
        self.emit = {
            "x87_scalar_strict": not relaxed,
            "local_state": relaxed,
            "lazy_flags": relaxed,
            "msvc_convention": translate["msvc_x87_convention"],
            "x87_cw_clone": translate["x87_cw_clone"],
            "indirect_call_symbol": "recomp_call",
        }
