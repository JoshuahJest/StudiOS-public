#include "CascadeMath.h"

static float clamp01(float value) {
    if (value < 0.0f) return 0.0f;
    if (value > 1.0f) return 1.0f;
    return value;
}

float cascade_scale_for_depth(float depth) {
    return 1.0f / (1.0f + clamp01(depth) * 0.7f);
}

float cascade_alpha_for_depth(float depth) {
    float normalized = clamp01(depth);
    float alpha = 1.0f - normalized * 0.88f;
    return alpha < 0.12f ? 0.12f : alpha;
}
