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
directory; the installation link preserves that layout. For a desktop library
import, select the complete Civ4 content root containing `Beyond the Sword/` and
the base-game assets. The library preserves these relationships under `original/`
and verifies each auxiliary DLL. The pinned `msxml3.dll` must be supplied manually;
its stock-install provenance is unknown, and Wine is not a launch dependency.
This nested installation flow still needs end-to-end verification. Prerequisites are in
[CONTRIBUTING](../../CONTRIBUTING.md#prerequisites). Later builds need only
`tools/.venv/bin/python tools/build.py`; add `--regenerate` after changing a
translation input.

## Code map

The main executable's map is exported from Ghidra with `ExportCodeMap.java`, which
takes the destination, image base and executable SHA-256 and is shared with the
auxiliary modules.
The Ghidra project is `ghidra/Civ4BTS.gpr`, imported with automatic analysis
disabled; decline the GUI's analysis prompt to keep that state. Add the shared `../ghidra_scripts/` folder to Script Manager, enable the ghidraLlm API,
and verify the program with `./tools/ghidra_api /program/info`. The default
server is port 8166; `GHIDRA_API_BASE` selects another. After deliberate analysis
has produced instruction boundaries:

```sh
./tools/ghidra_api /script/run '{"name":"ExportCodeMap.java","args":["/ABS/PATH/analysis/code-map-export","0x00400000","90dbdbdcbd6c631c9d575881d0241d9a2012b1a5957bacbe68212745da22174e"]}'
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

### CvGameCoreDLL

`CvGameCoreDLL.dll` is mapped at guest base `0x10500000` (file base `0x10000000`).
Its Ghidra map is seeded from evidence, then exported and packed:

1. Import `Assets/CvGameCoreDLL.dll` into `ghidra/Civ4BTS.gpr` with automatic
   analysis off. Make it the active program.
2. Write `slots.tsv` from the PE file's type-3 base relocations (`rva` column,
   one line per slot). `RebaseAuxModule.java` moves the image to the guest base
   and relocates exactly these slots once, so Ghidra's own relocation pass is not used.
3. `RebaseAuxModule.java` with the SHA-256, `0x10000000`, `0x10500000` and `slots.tsv`.
   It turns every analyzer off first.
4. Seed functions with `SeedAuxEntries.java`: observed targets (`site target kind
   count desc` rows, one per line, `-` for none), then `audit`, then `apply`.
   It refuses to run while any analyzer is on and follows direct calls from
   what it creates.
5. For pointers found in `.rdata` that name seeded code, write a `slot target`
   TSV and run `LinkDataSlots.java` on it, so the code map records them as data
   entries.
6. Export with `ExportCodeMap.java` (`0x10500000`, the file's SHA-256), then pack:

```sh
tools/.venv/bin/python recomp-kit/tools/recomp/code_map.py --pack analysis/cvgamecore-export \
  --exe "Civilization IV Complete/Civ4/Beyond the Sword/Assets/CvGameCoreDLL.dll" \
  --out recomp-kit/games/civ4-bts/metadata-cvgamecore --base 0x10500000
tools/.venv/bin/python tools/build.py --regenerate cvgamecore --target app \
  --allow-unmodelled "<reason>"
```

The translator also runs a bootstrap sweep of the image for every module (`discover`
in `[translate]`, on by default). Sweep bodies outside the Ghidra map are provisional:
they are emitted as decoded regions, and a provisional jump into mapped code goes
through the runtime dispatch rather than a trap. Those builds need `--allow-unmodelled`
with a reason.
