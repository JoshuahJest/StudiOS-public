#include <assert.h>
#include <math.h>

#include "CascadeMath.h"

int main(void) {
    assert(fabsf(cascade_scale_for_depth(0.0f) - 1.0f) < 0.001f);
    assert(cascade_scale_for_depth(1.0f) < cascade_scale_for_depth(0.25f));
    assert(fabsf(cascade_alpha_for_depth(-1.0f) - 1.0f) < 0.001f);
    assert(fabsf(cascade_alpha_for_depth(2.0f) - 0.12f) < 0.001f);
    return 0;
}
