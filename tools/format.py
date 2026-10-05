#!/usr/bin/env python3
"""Format handwritten native code without touching vendored or generated sources."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def source_files():
    """List only first-party native source roots, independent of private build inputs."""
    suffixes = {".c", ".cpp", ".h", ".hpp", ".m", ".mm"}
    return sorted(path for directory in ("runtime", "dx", "host", "platform", "mods", "games", "tests")
                  for path in (ROOT / directory).rglob("*")
                  if path.is_file() and path.suffix in suffixes)


def main():
    """Check formatting by default; --write explicitly applies the checked-in style."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--paths", nargs="+", type=Path, help="Explicit native files/directories to format")
    args = parser.parse_args()
    binary = Path(sys.executable).parent / "clang-format"
    if not binary.is_file():
        binary = shutil.which("clang-format")
    if not binary:
        parser.error("Install requirements-dev.txt in the Python environment running this command")
    files = source_files()
    if args.paths:
        files = sorted({p.resolve() for requested in args.paths
                        for p in (requested.rglob('*') if requested.is_dir() else [requested])
                        if p.is_file() and p.suffix in {'.c', '.cpp', '.h', '.hpp', '.m', '.mm'}})
        if not files:
            parser.error("No native source files in --paths")
    flags = ["-i"] if args.write else ["--dry-run", "--Werror"]
    for offset in range(0, len(files), 50):
        subprocess.run([str(binary), *flags, *map(str, files[offset:offset + 50])],
                       cwd=ROOT, check=True)
    print(f"{'Formatted' if args.write else 'Checked'} {len(files)} handwritten source files")


if __name__ == "__main__":
    main()
