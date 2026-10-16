# Civilization IV: Beyond the Sword

Native port of the installed GOG **Civilization IV: Beyond the Sword**
(`Civ4BeyondSword.exe`). This directory is the game's configuration root
(`--game civ4-bts`): `game.toml` pins the executable, `metadata*/` hold the code
maps for the executable and its auxiliary DLLs, and `native/` holds replacements.
`mods/core` is the game's core mod content.

The installation also includes `CvGameCoreDLL` source. It is supporting research
evidence only; verify behavior against the matching binaries.

## Setup and build

From the game repository root (the one containing `recomp-kit/`):

```sh
git submodule update --init --recursive recomp-kit
python3 -m venv tools/.venv
tools/.venv/bin/python -m pip install -r recomp-kit/requirements-dev.txt
RECOMP_BUILD_ROOT=$PWD/build tools/.venv/bin/python recomp-kit/tools/setup.py \
  --game civ4-bts --install "$PWD/Civilization IV Complete/Civ4/Beyond the Sword"
tools/.venv/bin/python tools/build.py --regenerate
```

Setup checks the executable against `game.toml` and links the installation at
`build/original`. Beyond the Sword also uses base Civ4 data from its parent
directory; the installation link preserves that layout, but packaging will need
to account for it. Prerequisites are in
[CONTRIBUTING](../../CONTRIBUTING.md#prerequisites). Later builds need only
`tools/.venv/bin/python tools/build.py`; add `--regenerate` after changing a
translation input.

## Code map

The main executable's map is exported from Ghidra with `Civ4ExportCodeMap.java`.
The Ghidra project is `ghidra/Civ4BTS.gpr`, imported with automatic analysis
disabled; decline the GUI's analysis prompt to keep that state. Add the shared `../ghidra_scripts/` folder to Script Manager, enable the ghidraLlm API,
and verify the program with `./tools/ghidra_api /program/info`. The default
server is port 8166; `GHIDRA_API_BASE` selects another. After deliberate analysis
has produced instruction boundaries:

```sh
./tools/ghidra_api /script/run '{"name":"Civ4ExportCodeMap.java","args":["/ABS/PATH/analysis/code-map-export"]}'
tools/.venv/bin/python recomp-kit/tools/recomp/code_map.py --pack analysis/code-map-export \
  --exe build/original/Civ4BeyondSword.exe --out recomp-kit/games/civ4-bts/metadata
tools/.venv/bin/python tools/build.py --regenerate
```

The exporters validate the executable hash, architecture and image base and
refuse an existing output directory.

### Auxiliary modules

`msxml3.dll` is BBT-optimized and keeps its constant data in `.text`, so its map
needs evidence from Microsoft's public PDB (GUID `08737D37-4241-46DD-9FD1-4F04B2CA28C9`,
age 2), kept at `analysis/pdb/msxml3.pdb`:

```sh
tools/.venv/bin/python recomp-kit/tools/recomp/pdb_evidence.py --exe build/original/msxml3.dll \
  --pdb analysis/pdb/msxml3.pdb --pdbutil "$(brew --prefix llvm)/bin/llvm-pdbutil" \
  --out analysis/msxml3-pdb-evidence.json
tools/.venv/bin/python recomp-kit/tools/recomp/bootstrap_map.py --exe build/original/msxml3.dll \
  --base 0x10a00000 --evidence analysis/msxml3-pdb-evidence.json \
  --out recomp-kit/games/civ4-bts/metadata-msxml3 --report analysis/bootstrap-msxml3-report.json
tools/.venv/bin/python recomp-kit/tools/recomp/bootstrap_map.py --exe build/original/zlib1.dll \
  --base 0x10c00000 --evidence analysis/zlib1-data-evidence.json \
  --out recomp-kit/games/civ4-bts/metadata-zlib1 --report analysis/bootstrap-zlib1-report.json
```

`zlib1.dll` embeds strings and tables in `.text`; its data ranges were checked by
hex dump and kept in `analysis/zlib1-data-evidence.json`.
