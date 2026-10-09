/* Optional LOD/draw-distance override for Black & White.
 *
 * Wraps LH3DIsland::Create (0x00803c00): the translated original runs
 * unchanged, then this re-asserts a small set of float globals the engine
 * reads when choosing object and terrain level of detail. The port ships
 * raised object/terrain detail by default; `RECOMP_LOD_OFF=1` restores the
 * original preset and the same-binary A/B against fn_00803c00.
 *
 * The addresses follow docs/graphics-settings.md:
 *   - LH3DObject_DrawLOD (0x00815a70) / DrawLODFade (0x00816ad0) scale each
 *     object LOD band threshold by
 *       (radius+1) * g_ObjectLODScale(0x00c37ea0) * g_LevelOfDetail(0x00c3813c)
 *     and clamp the result to g_ObjectLODMax(0x00c37ec8, initial 5.0). The
 *     clamp is what caps far-object detail for large objects, so raising the
 *     max is the primary object knob.
 *   - LH3DIsland::Create seeds three 9-dword terrain LOD band groups at
 *     0x00e9c4b8/0x00e9c4e0/0x00e9c508, each with a far bound at +0x1c and
 *     thresholds at +0x20/+0x24; LH3DIsland_ComputeCellLOD (0x00877210) then
 *     classifies cells against them. The original rewrites these on every
 *     island load, so scaling after it runs does not compound.
 *
 * Defaults: LEVEL=3.0, MAX=50.0, TERRAIN=x3.0 (the values settled on for the
 * port). Each can be overridden per run:
 *   RECOMP_LOD_OFF=1        restore the original preset; ignores the others
 *   RECOMP_LOD_LEVEL=<f>    absolute g_LevelOfDetail (default 3.0)
 *   RECOMP_LOD_MAX=<f>      absolute g_ObjectLODMax (default 50.0)
 *   RECOMP_LOD_TERRAIN=<f>  terrain band multiplier (default 3.0; 1.0 = none)
 */
#include "lod_override.h"

#include <stdio.h>
#include <stdlib.h>

/* Retained translated original; bypasses the FN_00803c00 override. */
void fn_00803c00(X86 *c);

enum {
  G_LEVEL_OF_DETAIL = 0x00c3813cu, /* float, preset/options LOD scale */
  G_OBJECT_LOD_MAX = 0x00c37ec8u,  /* float, object band multiplier clamp */
};

/* Terrain band groups and the distance fields inside each. */
static const uint32_t kTerrainGroups[] = {0x00e9c4b8u, 0x00e9c4e0u, 0x00e9c508u};
static const uint32_t kTerrainDistanceOffsets[] = {0x1cu, 0x20u, 0x24u};

enum { DEFAULT_LEVEL = 3, DEFAULT_MAX = 50, DEFAULT_TERRAIN = 3 };

static int lod_off(void) {
  const char *v = getenv("RECOMP_LOD_OFF");
  return v && v[0] == '1' && v[1] == '\0';
}

/* Parse a whole-string float. Returns 0 and leaves *out untouched when the
 * variable is absent or not a number. */
static int env_float(const char *name, float *out) {
  const char *v = getenv(name);
  if (!v || !*v)
    return 0;
  char *end = NULL;
  const double d = strtod(v, &end);
  if (end == v || *end || d != d) {
    fprintf(stderr, "recomp: %s=%s is not a number; ignored\n", name, v);
    return 0;
  }
  *out = (float)d;
  return 1;
}

// @port 0x00803c00 0% rendering,verify,divergence
// Ghidra 0x00803c00 LH3DIsland::Create. The translated original still builds
// the island and seeds the LOD bands; this wrapper only re-asserts the
// distance globals afterward.
// DIVERGENCE(original): object and terrain detail are drawn farther than the
// shipped preset. This is an intentional, user-requested player-facing tuning
// option, not a reconstruction of original behavior; RECOMP_LOD_OFF=1 disables
// it and restores the preset exactly.
void bw_island_create(X86 *c) {
  fn_00803c00(c);
  if (lod_off())
    return;

  float level = (float)DEFAULT_LEVEL;
  float max = (float)DEFAULT_MAX;
  float terrain = (float)DEFAULT_TERRAIN;
  env_float("RECOMP_LOD_LEVEL", &level);
  env_float("RECOMP_LOD_MAX", &max);
  env_float("RECOMP_LOD_TERRAIN", &terrain);

  wrf32(G_LEVEL_OF_DETAIL, level);
  wrf32(G_OBJECT_LOD_MAX, max);
  if (terrain > 0.0f && terrain != 1.0f) {
    for (size_t g = 0; g < sizeof(kTerrainGroups) / sizeof(kTerrainGroups[0]); ++g) {
      for (size_t i = 0;
           i < sizeof(kTerrainDistanceOffsets) / sizeof(kTerrainDistanceOffsets[0]); ++i) {
        const uint32_t addr = kTerrainGroups[g] + kTerrainDistanceOffsets[i];
        wrf32(addr, rdf32(addr) * terrain);
      }
    }
  }

  static int reported = 0;
  if (!reported) {
    reported = 1;
    fprintf(stderr, "[recomp] lod: level=%.3f max=%.3f terrain=x%.3f\n",
            (double)level, (double)max, (double)terrain);
  }
}
