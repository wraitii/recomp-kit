# Railroad Tycoon 3

The GOG `RT3.exe` identified by [game.toml](game.toml) is the behavioral reference. The packed [code map](metadata/) contains 6,035 functions across 6,762 spans and 506,876 instruction boundaries, without executable bytes, instruction text or analysis names. The private executable supplies those bytes when the kit decodes listings into the build root.

Ghidra research, export scripts and engine notes remain in the [Railroad Tycoon 3 workbench](../../../). To refresh the map, export `recomp-code-map-v1` from the matching executable into a fresh directory, then run:

```sh
python tools/recomp/code_map.py --pack /path/to/v1-export --exe /path/to/RT3.exe --out games/railroad-tycoon-3/metadata
```

The map pins the executable SHA-256 and preserves disconnected function bodies and exceptional instruction boundaries. Check the generated listings against the previous cache before replacing the committed map.
