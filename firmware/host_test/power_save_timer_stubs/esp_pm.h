#pragma once

#include "esp_timer.h"

struct esp_pm_config_t {
    int max_freq_mhz;
    int min_freq_mhz;
    bool light_sleep_enable;
};

inline esp_err_t esp_pm_configure(const esp_pm_config_t*) {
    return ESP_OK;
}
