# Tom Clancy's Ghost Recon (2001)

A native port by static recompilation. You supply a **GOG 1.4** installation;
Desert Siege and Island Thunder use the same executable with their expansion
data. Ghidra is not needed: the build decodes your executable with the code map
in `metadata/`.

```sh
.venv/bin/python tools/setup.py --game ghost-recon --install "/path/to/Ghost Recon"
.venv/bin/python tools/build.py --game ghost-recon --regenerate
open build/ghost-recon/GhostReconRecomp.app
```

Ctrl+Option+M releases mouse capture. `RECOMP_FXAA=1` enables optional scene
antialiasing.

## Native replacements

| Source | Original addresses | Purpose / comparison switch |
| --- | --- | --- |
| `native/projection_cull` | `0081ae30`, `0081aa00` | Projection mesh culling and build; `RECOMP_PROJECTION_ORIGINAL=1` |
| `native/ray_triangles` | `00556840` | Collision triangle tests; `RECOMP_RAYTRI_ORIGINAL=1` |
| `native/scene_post` | `0047c160` | Scene boundary and optional FXAA before the HUD |
| `native/port_tab` | `00650490`, `006a7fe0` and others | Options **Port** tab with a live FXAA toggle; see its README |
