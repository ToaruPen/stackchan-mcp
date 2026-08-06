#pragma once

#include <string>

namespace stackchan_power {

inline constexpr int kAutoSleepAfterSeconds = 60;
inline constexpr int kAutoShutdownAfterSeconds = 300;

enum class AutoSleepPersistenceStatus {
    kOk,
    kMissing,
    kError,
};

struct AutoSleepPersistenceRead {
    AutoSleepPersistenceStatus status = AutoSleepPersistenceStatus::kError;
    bool enabled = true;
    std::string error;
};

struct AutoSleepPersistenceWrite {
    bool ok = false;
    std::string error;
};

struct AutoSleepControlHooks {
    void* context = nullptr;
    AutoSleepPersistenceRead (*read)(void* context) = nullptr;
    AutoSleepPersistenceWrite (*write)(void* context, bool enabled) = nullptr;
    void (*set_timer_enabled)(void* context, bool enabled) = nullptr;
    void (*wake_up)(void* context) = nullptr;
};

struct AutoSleepReadResult {
    bool ok = false;
    bool enabled = true;
    std::string error;
};

struct AutoSleepSetResult {
    bool ok = false;
    bool previous_enabled = true;
    bool enabled = true;
    std::string error;
};

inline AutoSleepReadResult GetAutoSleepPolicy(
    const AutoSleepControlHooks& hooks) {
    const AutoSleepPersistenceRead persisted = hooks.read(hooks.context);
    if (persisted.status == AutoSleepPersistenceStatus::kError) {
        return AutoSleepReadResult{
            .ok = false,
            .error = persisted.error,
        };
    }
    return AutoSleepReadResult{
        .ok = true,
        .enabled = persisted.status == AutoSleepPersistenceStatus::kMissing
                       ? true
                       : persisted.enabled,
    };
}

inline AutoSleepSetResult SetAutoSleepPolicy(
    const AutoSleepControlHooks& hooks, bool enabled) {
    const AutoSleepReadResult current = GetAutoSleepPolicy(hooks);
    if (!current.ok) {
        return AutoSleepSetResult{
            .ok = false,
            .error = current.error,
        };
    }

    if (current.enabled != enabled) {
        const AutoSleepPersistenceWrite persisted =
            hooks.write(hooks.context, enabled);
        if (!persisted.ok) {
            return AutoSleepSetResult{
                .ok = false,
                .previous_enabled = current.enabled,
                .enabled = current.enabled,
                .error = persisted.error,
            };
        }
    }

    if (enabled) {
        // SetEnabled(true) is intentionally idempotent, so cycle through the
        // disabled state to restart the countdown even for true -> true.
        hooks.set_timer_enabled(hooks.context, false);
        hooks.set_timer_enabled(hooks.context, true);
    } else {
        hooks.set_timer_enabled(hooks.context, false);
        // Also wake explicitly for false -> false. This keeps the display and
        // the persisted policy synchronized even if the timer was already off.
        hooks.wake_up(hooks.context);
    }

    return AutoSleepSetResult{
        .ok = true,
        .previous_enabled = current.enabled,
        .enabled = enabled,
    };
}

}  // namespace stackchan_power
