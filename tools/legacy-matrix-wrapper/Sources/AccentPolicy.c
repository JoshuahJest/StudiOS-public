#include "AccentPolicy.h"

double accent_spawn_probability(double frequency_seconds, double fps) {
    if (frequency_seconds < 0.5) frequency_seconds = 0.5;
    if (fps < 1.0) fps = 1.0;
    return 1.0 / (frequency_seconds * fps);
}

double adaptive_frame_interval(int on_ac_power, int low_power_mode, int thermal_ok) {
    return (on_ac_power && !low_power_mode && thermal_ok) ? (1.0 / 60.0) : (1.0 / 30.0);
}
