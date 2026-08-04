#pragma once

#include <cstdint>
#include <stdexcept>
#include <vector>

using esp_err_t = int;
inline constexpr esp_err_t ESP_OK = 0;
inline constexpr int ESP_TIMER_TASK = 0;

struct FakeEspTimer;
using esp_timer_handle_t = FakeEspTimer*;

struct esp_timer_create_args_t {
    void (*callback)(void*);
    void* arg;
    int dispatch_method;
    const char* name;
    bool skip_unhandled_events;
};

struct FakeEspTimer {
    void (*callback)(void*) = nullptr;
    void* arg = nullptr;
    bool running = false;
};

namespace fake_esp_timer {
inline std::vector<FakeEspTimer*> timers;

inline void Fire(FakeEspTimer* timer, int count = 1) {
    for (int index = 0; index < count; ++index) {
        if (timer->running) {
            timer->callback(timer->arg);
        }
    }
}
}  // namespace fake_esp_timer

inline esp_err_t esp_timer_create(const esp_timer_create_args_t* args,
                                  esp_timer_handle_t* out) {
    auto* timer = new FakeEspTimer{
        .callback = args->callback,
        .arg = args->arg,
    };
    fake_esp_timer::timers.push_back(timer);
    *out = timer;
    return ESP_OK;
}

inline esp_err_t esp_timer_start_periodic(esp_timer_handle_t timer,
                                          std::uint64_t) {
    timer->running = true;
    return ESP_OK;
}

inline esp_err_t esp_timer_stop(esp_timer_handle_t timer) {
    timer->running = false;
    return ESP_OK;
}

inline esp_err_t esp_timer_delete(esp_timer_handle_t timer) {
    for (auto it = fake_esp_timer::timers.begin();
         it != fake_esp_timer::timers.end(); ++it) {
        if (*it == timer) {
            fake_esp_timer::timers.erase(it);
            break;
        }
    }
    delete timer;
    return ESP_OK;
}

#define ESP_ERROR_CHECK(expression)                                      \
    do {                                                                 \
        if ((expression) != ESP_OK) {                                    \
            throw std::runtime_error("ESP_ERROR_CHECK failed in test"); \
        }                                                                \
    } while (false)
