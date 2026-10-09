#!/usr/bin/env python3
"""Fail when a literal identifying one of the kit's games appears in kit code on a non-comment line.

The runtime, shims, hosts, platform and mod layers must not know which game
they are building. Each games/<id>/game.toml supplies its identifying strings."""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import game_config  # noqa: E402

DIRECTORIES = ("runtime", "dx", "host", "platform", "mods")
SUFFIXES = {".c", ".cpp", ".h", ".hpp", ".mm", ".in"}
IDENTITY_KEYS = ("name", "app_name", "bundle_id", "executable", "guest_root")


def tokens():
    found = set()
    for toml in sorted((ROOT / "games").glob("*/game.toml")):
        if toml.parent.name == "stub":
            continue
        game = game_config.tomllib.loads(toml.read_text())["game"]
        found.update(str(game[key]) for key in IDENTITY_KEYS if game.get(key))
    return sorted(found)


def code_lines(text):
    """Yield (line number, code) with block and line comments removed."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    for number, line in enumerate(text.splitlines(), 1):
        yield number, line.split("//", 1)[0]


def findings():
    literals = tokens()
    for directory in DIRECTORIES:
        for path in sorted((ROOT / directory).rglob("*")):
            relative = path.relative_to(ROOT)
            if path.suffix not in SUFFIXES or "tests" in relative.parts:
                continue
            for number, code in code_lines(path.read_text(errors="replace")):
                for token in literals:
                    if token in code:
                        yield "%s:%d: %s" % (relative.as_posix(), number, token)


def main():
    found = list(findings())
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
