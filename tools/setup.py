#!/usr/bin/env python3
"""Prepare private translation inputs from a contributor's own game installation.

    tools/setup.py --game <id> --install /path/to/the/installed/game

Creates <build root>/original, a symlink to the installation, after checking the
executable against game.toml. No game files are downloaded or changed.
"""

import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import game_config  # noqa: E402


def validate_game(directory, executable, sha256, required_dirs=()):
    """Require the supported executable and game-data directories before creating a link."""
    directory = directory.expanduser().resolve()
    image = directory / executable
    if not image.is_file():
        raise ValueError(f"{executable} was not found in {directory}")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    if digest != sha256:
        raise ValueError(f"Unsupported {executable}: SHA-256 {digest}; expected {sha256}")
    names = {entry.name.lower() for entry in directory.iterdir() if entry.is_dir()}
    for required in required_dirs:
        if required.lower() not in names:
            raise ValueError(f"Game installation is missing {required}/")
    return directory


def link_game(directory, destination):
    """Reuse the same installation link; refuse to replace another installation or directory."""
    directory = directory.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        if destination.resolve() != directory:
            raise ValueError(f"{destination} already points elsewhere; move it aside explicitly")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(directory, target_is_directory=True)


def main():
    """Validate local prerequisites, preserve existing inputs, and link the game."""
    parser = argparse.ArgumentParser(description=__doc__)
    game_config.add_game_args(parser)
    parser.add_argument("--install", type=Path, required=True, help="Your installed game directory")
    args = game_config.resolve_game_args(parser.parse_args(), default=None)
    try:
        cfg = game_config.load(args.game_dir, args.build_root)
        if cfg["code_map_path"] is None:
            raise ValueError("%s has no [translate] code_map; translation needs the game's Ghidra code map" % args.game_dir)
        setup = cfg.get("setup", {})
        directory = validate_game(args.install, cfg["game"]["executable"], cfg["game"]["sha256"],
                                  setup.get("required_dirs", ()))
        cfg["developer_exe_path"].parent.parent.mkdir(parents=True, exist_ok=True)
        link_game(directory, cfg["developer_exe_path"].parent)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Setup failed: {error}\n")
    game = "--game %s" % args.game if args.game else "--game-dir %s" % args.game_dir
    print("Game inputs ready. Next: tools/build.py %s --regenerate" % game)


if __name__ == "__main__":
    main()
