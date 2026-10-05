// audio3d.h - the 3D positional-audio model shared by the DirectSound3D and
// Miles 3D shims.
//
// Both APIs do the same thing: they pan a source against a listener and
// attenuate it with distance. DirectSound3D (dsound.cpp) and the Miles
// software provider (mss32.cpp) differ only in how their state is addressed
// and spelled, so they share the vector maths and Wine's stereo speaker mix
// here. A title that asks for either gets the same placement.
//
// The helper deliberately stops at "a source gain and an angle": distance,
// rolloff and the API's own volume units differ between the two and stay in
// the shim that owns them.
#pragma once
#include <math.h>
#include <stdint.h>

namespace audio3d {

static const float PI_F_ = 3.14159265358979323846f;

struct V3 {
    float x, y, z;
};

static inline float v3_dot(const V3 &a, const V3 &b) {
    return a.x * b.x + a.y * b.y + a.z * b.z;
}
static inline V3 v3_cross(const V3 &a, const V3 &b) {
    V3 c;
    c.x = a.y * b.z - a.z * b.y;
    c.y = a.z * b.x - a.x * b.z;
    c.z = a.x * b.y - a.y * b.x;
    return c;
}
static inline float v3_len(const V3 &a) {
    return sqrtf(v3_dot(a, a));
}
static float v3_angle(const V3 &a, const V3 &b) {
    float la = v3_len(a), lb = v3_len(b);
    if (!la || !lb)
        return 0.0f;
    float c = v3_dot(a, b) / (la * lb);
    if (c > 1.0f)
        c = 1.0f;
    if (c < -1.0f)
        c = -1.0f;
    // Wine's AngleBetweenVectorsRad computes the cosine in float and calls the
    // double acos(), rounding once on return. acosf(-1.0f) on macOS is one ulp
    // short of pi, which moves an exactly-sideways source a hair off the
    // speaker axis; the double call rounds to the nearest float and keeps the
    // reference's hard-side pan.
    return (float)acos((double)c);
}

// Wine's stereo speaker mix for a pan angle in radians, and the two gains
// before the source/distance volume is folded in.
static void stereo_gains(float angle, float *left, float *right) {
    const float half_pi = PI_F_ / 2.0f;
    float a;
    if (angle >= -half_pi && angle < half_pi) {
        a = (angle + half_pi) / PI_F_;
        *left = sqrtf(1.0f - a);
        *right = sqrtf(a);
        return;
    }
    if (angle < -half_pi)
        angle += 2.0f * PI_F_;
    a = (angle - half_pi) / PI_F_;
    if (a < 0.0f)
        a = 0.0f;
    if (a > 1.0f)
        a = 1.0f;
    *right = sqrtf(1.0f - a);
    *left = sqrtf(a);
}

} // namespace audio3d
