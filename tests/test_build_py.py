"""tools/build.py argument handling and CMake invocation, without CMake or game files."""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("build_py", Path(__file__).parents[1] / "tools/build.py")
build_py = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_py)


class BuildPyTests(unittest.TestCase):
    def test_default_preset_follows_the_operating_system(self):
        self.assertEqual(build_py.default_preset("Darwin"), "macos")
        self.assertEqual(build_py.default_preset("Linux"), "linux")
        self.assertEqual(build_py.default_preset("Windows"), "windows")

    def test_debug_config_selects_the_debug_preset(self):
        self.assertEqual(build_py.preset_name("macos", "Release"), "macos")
        self.assertEqual(build_py.preset_name("linux", "Debug"), "linux-debug")

    def test_archive_path_per_platform(self):
        root = Path("/r/build")
        self.assertEqual(build_py.archive_path(root, "Darwin"), root / "cmake/macos/lib/librecomp_gen.a")
        self.assertEqual(build_py.archive_path(root, "Windows"), root / "cmake/windows/lib/recomp_gen.lib")
        self.assertEqual(build_py.archive_path(root, "Darwin", "macos-debug"),
                         root / "cmake/macos-debug/lib/librecomp_gen.a")

    def test_desktop_hosts_are_allowed_on_linux_and_windows(self):
        for system, preset in (("Linux", "linux"), ("Windows", "windows")):
            for target in ("app", "smoke", "headless", "fixture"):
                for extra in ([], ["--regenerate"]):
                    with self.subTest(system=system, target=target, extra=extra):
                        args, _ = build_py.parse_args(["--target", target] + extra, system=system)
                        self.assertEqual(args.preset, preset)
                        self.assertEqual(args.regenerate, bool(extra))

    def test_web_target_uses_the_web_presets(self):
        self.assertEqual(build_py.preset_name("macos", "Release", target="web"), "web")
        self.assertEqual(build_py.preset_name("linux", "Release", stub=True, target="web"), "web-stub")
        with patch.dict(os.environ, {"EMSDK": "/emsdk"}):
            args, _ = build_py.parse_args(["--target", "web"], system="Linux")
        self.assertEqual(args.target, "web")
        with patch.dict(os.environ, {"EMSDK": ""}), self.assertRaises(SystemExit):
            build_py.parse_args(["--target", "web"], system="Darwin")

    def test_jobs_must_be_positive(self):
        with self.assertRaises(SystemExit):
            build_py.parse_args(["--jobs", "0"], system="Darwin")

    def test_build_passes_targets_and_parallelism_to_cmake(self):
        with patch.object(build_py.subprocess, "run") as run:
            build_py.build("macos", ["pop_smoke"], 6)
        command = run.call_args[0][0]
        self.assertIn("--build", command)
        self.assertEqual(command[command.index("--preset") + 1], "macos")
        self.assertEqual(command[command.index("--parallel") + 1], "6")
        self.assertEqual(command[command.index("--target") + 1:], ["pop_smoke"])

    def test_game_dir_defaults_to_the_stub_and_is_validated(self):
        args, _ = build_py.parse_args([], system="Darwin")
        self.assertEqual(args.game_dir, build_py.ROOT / "games/stub")
        self.assertEqual(args.build_root, build_py.ROOT / "build")
        with self.assertRaises(SystemExit):
            build_py.parse_args(["--game-dir", "/no/such/game"], system="Darwin")
        with self.assertRaises(SystemExit):
            build_py.parse_args(["--game-dir", "games/stub"], system="Darwin")  # relative
        with self.assertRaises(SystemExit):
            build_py.parse_args(["--target", "plugins"], system="Darwin")  # the stub has no mods

    def test_build_root_follows_an_external_game_dir(self):
        self.assertEqual(build_py.build_root_for(build_py.ROOT / "games/stub"), build_py.ROOT / "build")
        self.assertEqual(build_py.build_root_for(Path("/tmp/populous-recomp")), Path("/tmp/populous-recomp/build"))
        self.assertEqual(build_py.build_dir_for(Path("/tmp/pr/build"), "macos"), Path("/tmp/pr/build/cmake/macos"))

    def test_configure_passes_the_build_dir_and_the_cache_paths(self):
        with patch.object(build_py.subprocess, "run") as run:
            build_py.configure("macos", build_py.game_defines(Path("/g"), Path("/g/build")),
                               build_dir=Path("/g/build/cmake/macos"))
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("-B") + 1], str(Path("/g/build/cmake/macos")))
        self.assertIn("-DRECOMP_GAME_DIR=/g", command)
        self.assertIn("-DPOP_BUILD_ROOT=/g/build", command)
        with patch.object(build_py.subprocess, "run") as run:
            build_py.build("macos", ["pop_smoke"], 2, build_dir=Path("/g/build/cmake/macos"))
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--build") + 1], str(Path("/g/build/cmake/macos")))
        self.assertEqual(command[command.index("--config") + 1], "Release")  # Xcode is multi-config

    def test_stub_selects_the_stub_preset_and_rejects_debug(self):
        args, _ = build_py.parse_args(["--stub"], system="Linux")
        self.assertEqual(build_py.preset_name(args.preset, args.config, stub=args.stub), "linux-stub")
        with self.assertRaises(SystemExit):
            build_py.parse_args(["--stub", "--config", "Debug"], system="Linux")

    def test_ios_target_uses_the_ios_preset_and_needs_macos(self):
        args, _ = build_py.parse_args(["--target", "ios", "--team", "T"], system="Darwin")
        self.assertEqual(build_py.preset_name(args.preset, args.config, stub=False, target=args.target), "ios")
        with self.assertRaises(SystemExit):
            build_py.parse_args(["--target", "ios", "--team", "T"], system="Linux")
        with self.assertRaises(SystemExit):
            build_py.parse_args(["--target", "ios"], system="Darwin")  # no team

    def test_run_translator_translates_each_auxiliary_module_into_its_own_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch.object(build_py.subprocess, "run") as run:
            build_py.run_translator(tmp, "/g", "/b", None, ["dfx"])
            self.assertEqual(run.call_count, 2)
            main_cmd, aux_cmd = run.call_args_list[0][0][0], run.call_args_list[1][0][0]
            self.assertNotIn("--module", main_cmd)
            self.assertEqual(aux_cmd[aux_cmd.index("--module") + 1], "dfx")
            self.assertEqual(aux_cmd[aux_cmd.index("--out") + 1], str(Path(tmp) / "aux-dfx"))
            self.assertTrue((Path(tmp) / "aux-dfx").is_dir())
            self.assertIn("translate-dfx-report.json", aux_cmd[aux_cmd.index("--report") + 1])

    def test_sync_tree_rewrites_only_changed_files(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "src", Path(tmp) / "dst"
            (src / "sub").mkdir(parents=True)
            (dst / "sub").mkdir(parents=True)
            (src / "keep.c").write_text("same")
            (src / "sub/change.c").write_text("new")
            (dst / "keep.c").write_text("same")
            (dst / "sub/change.c").write_text("old")
            (dst / "stale.c").write_text("gone")
            time.sleep(0.01)
            before = (dst / "keep.c").stat().st_mtime_ns
            build_py.sync_tree(src, dst)
            self.assertEqual((dst / "keep.c").stat().st_mtime_ns, before,
                             "an identical file keeps its mtime so ninja skips it")
            self.assertEqual((dst / "sub/change.c").read_text(), "new")
            self.assertFalse((dst / "stale.c").exists(), "a dropped file is removed")

    def test_translation_fingerprint_tracks_listings_and_args(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            listings = game / "listings"
            (listings / "functions").mkdir(parents=True)
            (game / "globals.toml").write_text("")
            (listings / "functions.tsv").write_text("address\tname\tbytes\n00401000\tF\t1\n")
            (listings / "functions" / "00401000.asm").write_text("00401000  RET\n")
            cfg = {"game": {"sha256": "abc"}, "translate": {}, "listings_path": listings,
                   "aux_modules": []}
            base = build_py.translation_fingerprint(game, cfg, {})
            self.assertEqual(base, build_py.translation_fingerprint(game, cfg, {}))
            self.assertNotEqual(base, build_py.translation_fingerprint(game, cfg, {"forget": "00401000"}))
            os.utime(listings / "functions" / "00401000.asm", (1, 1))
            self.assertNotEqual(base, build_py.translation_fingerprint(game, cfg, {}),
                                "a changed listing invalidates the stamp")

    def test_fingerprint_tracks_discovered_file_contents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            discovered = game / "discovered.txt"
            discovered.write_text("00401000\n")
            cfg = {"game": {"sha256": "abc"}, "translate": {},
                   "listings_path": game, "aux_modules": []}
            args = {"discovered": discovered}
            before = build_py.translation_fingerprint(game, cfg, args)
            discovered.write_text("00401000\n00402000\n")
            self.assertNotEqual(before, build_py.translation_fingerprint(game, cfg, args))
            discovered.unlink()
            with self.assertRaises(FileNotFoundError):
                build_py.translation_fingerprint(game, cfg, args)

    def test_fingerprint_tracks_code_map_contents_without_listing_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            metadata = game / "metadata"
            metadata.mkdir()
            for name in ("metadata.txt", "functions.tsv", "instruction_map.tsv"):
                (metadata / name).write_text("initial")
            cfg = {"game": {"sha256": "abc"}, "translate": {}, "code_map_path": metadata,
                   "listings_path": game / "listings", "aux_modules": []}
            before = build_py.translation_fingerprint(game, cfg, {})
            (metadata / "instruction_map.tsv").write_text("new boundaries")
            self.assertNotEqual(before, build_py.translation_fingerprint(game, cfg, {}))

    def test_fingerprint_tracks_auxiliary_listings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            aux = game / "aux"
            (aux / "functions").mkdir(parents=True)
            cfg = {"game": {"sha256": "abc"}, "translate": {}, "listings_path": game,
                   "aux_modules": [{"key": "dll", "listings_path": aux}]}
            before = build_py.translation_fingerprint(game, cfg, {})
            listing = aux / "functions/00401000.asm"
            listing.write_text("RET\n")
            added = build_py.translation_fingerprint(game, cfg, {})
            self.assertNotEqual(before, added)
            listing.write_text("NOP\nRET\n")
            edited = build_py.translation_fingerprint(game, cfg, {})
            self.assertNotEqual(added, edited)
            (aux / "functions.tsv").write_text("00401000\tEntry\n")
            self.assertNotEqual(edited, build_py.translation_fingerprint(game, cfg, {}))
            listing.unlink()
            self.assertNotEqual(edited, build_py.translation_fingerprint(game, cfg, {}))

    def test_pick_device_prefers_the_single_paired_ipad(self):
        devices = [
            {"identifier": "A", "hardwareProperties": {"productType": "iPhone16,1"},
             "connectionProperties": {"pairingState": "paired"}},
            {"identifier": "B", "hardwareProperties": {"productType": "iPad16,3"},
             "connectionProperties": {"pairingState": "paired"}},
        ]
        self.assertEqual(build_py.pick_device(devices), "B")
        with self.assertRaises(SystemExit):
            build_py.pick_device(devices + [{"identifier": "C", "hardwareProperties": {"productType": "iPad14,1"},
                                             "connectionProperties": {"pairingState": "paired"}}])
        with self.assertRaises(SystemExit):
            build_py.pick_device([])


if __name__ == "__main__":
    unittest.main()


def test_function_corpus_is_isolated_and_does_not_regenerate_game():
    import pytest
    args, _ = build_py.parse_args(['--function-corpus', 'manifest.json', '--corpus-trial-ms', '0'], system='Darwin')
    assert args.function_corpus == Path('manifest.json')
    assert args.corpus_trial_ms == 0
    for extra in (['--regenerate'], ['--stub'], ['--corpus-fragments'],
                  ['--cpu-locals-checks']):
        with pytest.raises(SystemExit):
            build_py.parse_args(['--function-corpus', 'manifest.json', *extra], system='Darwin')


def test_ir_ssa_corpus_mode_requires_isolation():
    import pytest
    args, _ = build_py.parse_args(['--function-corpus', 'manifest.json', '--corpus-ir-ssa'], system='Darwin')
    assert args.corpus_ir_ssa
    with pytest.raises(SystemExit):
        build_py.parse_args(['--corpus-ir-ssa'], system='Darwin')


def test_ir_ssa_native_checks_require_isolation():
    import pytest
    args, _ = build_py.parse_args(['--ir-ssa-checks'], system='Darwin')
    assert args.ir_ssa_checks
    for extra in (['--regenerate'], ['--stub'], ['--cpu-locals-checks'],
                  ['--function-corpus', 'manifest.json']):
        with pytest.raises(SystemExit):
            build_py.parse_args(['--ir-ssa-checks', *extra], system='Darwin')


def test_retired_dataflow_experiment_options_are_rejected():
    import pytest
    for option in (['--corpus-x87-dataflow'], ['--corpus-stack-forwarding'],
                   ['--corpus-decoded-dataflow'], ['--x87-dataflow-checks'],
                   ['--decoded-dataflow-checks']):
        with pytest.raises(SystemExit):
            build_py.parse_args(['--function-corpus', 'manifest.json', *option], system='Darwin')


def test_corpus_asan_is_a_correctness_build_of_the_function_corpus():
    import pytest
    args, _ = build_py.parse_args(['--function-corpus', 'manifest.json', '--corpus-asan',
                                   '--corpus-trial-ms', '0'], system='Darwin')
    assert args.corpus_asan
    with pytest.raises(SystemExit):
        build_py.parse_args(['--function-corpus', 'manifest.json', '--corpus-asan'], system='Darwin')
    with pytest.raises(SystemExit):
        build_py.parse_args(['--corpus-asan', '--corpus-trial-ms', '0'], system='Darwin')


def test_retired_llvm_experiment_options_are_rejected():
    import pytest
    for option in (['--corpus-llvm', 'm.json'], ['--corpus-llvm-sweep', 'm.json'],
                   ['--x87-llvm-experiment'], ['--x87-llvm-function', 'f.json']):
        with pytest.raises(SystemExit):
            build_py.parse_args(option, system='Darwin')


def test_corpus_fault_state_and_convention_require_ir_ssa():
    import pytest
    args, _ = build_py.parse_args(['--function-corpus', 'manifest.json', '--corpus-ir-ssa',
                                   '--corpus-fault-state', 'exact',
                                   '--corpus-msvc-x87-convention', 'off'], system='Darwin')
    assert args.corpus_fault_state == 'exact'
    assert args.corpus_msvc_x87_convention is False
    args, _ = build_py.parse_args(['--function-corpus', 'manifest.json', '--corpus-ir-ssa'], system='Darwin')
    assert args.corpus_fault_state is None and args.corpus_msvc_x87_convention is None
    for flags in (['--corpus-fault-state', 'relaxed'], ['--corpus-msvc-x87-convention', 'on']):
        with pytest.raises(SystemExit):
            build_py.parse_args(['--function-corpus', 'manifest.json', *flags], system='Darwin')
    for retired in (['--corpus-ir-ssa-x87', 'scalar'], ['--corpus-ir-ssa-state', 'locals'],
                    ['--corpus-ir-ssa-lazy-nan', 'on'], ['--corpus-fault-state', 'bogus']):
        with pytest.raises(SystemExit):
            build_py.parse_args(['--function-corpus', 'manifest.json', '--corpus-ir-ssa', *retired],
                                system='Darwin')
