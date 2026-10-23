"""Stage the optional General MIDI bank and remove stale copies when disabled."""

import argparse
from pathlib import Path
import shutil

import game_config

ROOT = Path(__file__).resolve().parents[1]


def copy_soundfont(cfg, dest):
    dest = Path(dest)
    bank = ROOT / "third_party/soundfonts/generaluser-gs"
    files = []
    for source, name in (("GeneralUser-GS.sf2", "general-midi.sf2"),
                         ("LICENSE", "general-midi-LICENSE.txt")):
        target = dest / name
        if cfg["audio"]["midi"]:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bank / source, target)
            files.append(target)
        else:
            target.unlink(missing_ok=True)
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args()
    copy_soundfont(game_config.load(args.game_dir), args.dest)
