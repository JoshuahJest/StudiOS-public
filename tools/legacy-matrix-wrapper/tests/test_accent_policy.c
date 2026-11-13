#include <assert.h>
#include <math.h>
#include "AccentPolicy.h"

int main(void) {
    assert(fabs(accent_spawn_probability(60.0, 30.0) - (1.0 / 1800.0)) < 0.000001);
    assert(fabs(adaptive_frame_interval(1, 0, 1) - (1.0 / 60.0)) < 0.000001);
    assert(fabs(adaptive_frame_interval(1, 1, 1) - (1.0 / 30.0)) < 0.000001);
    assert(fabs(adaptive_frame_interval(0, 0, 1) - (1.0 / 30.0)) < 0.000001);
    assert(fabs(adaptive_frame_interval(1, 0, 0) - (1.0 / 30.0)) < 0.000001);
    return 0;
}
