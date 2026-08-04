#pragma once

#include <core/animation/animate_value/animate_value.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace stackchan_motion {

struct NativeWritePayload {
    int axis_id;
    int position;
    uint16_t time_ms;
    uint16_t speed;

    bool operator==(const NativeWritePayload& other) const {
        return axis_id == other.axis_id &&
            position == other.position &&
            time_ms == other.time_ms && speed == other.speed;
    }
};

enum class NativeWriteDecision : uint8_t {
    kSend,
    kSuppress,
};

// Suppression is deliberately downstream of interpolation and native
// rounding. It never predicts servo state: only an exact duplicate of the
// latest successful, ACK-confirmed payload for the same request/epoch may be
// skipped. The two fixed entries correspond to StackChan's yaw and pitch
// axes; an unexpected third axis fails open and is always sent.
class NativeWriteSuppressor {
public:
    bool enabled() const { return enabled_; }

    void SetEnabled(bool enabled) {
        if (enabled_ == enabled) {
            return;
        }
        enabled_ = enabled;
        InvalidateAll();
    }

    NativeWriteDecision Decide(
        const NativeWritePayload& payload,
        uint64_t request_token,
        uint32_t epoch,
        bool terminal,
        bool force) const {
        if (!enabled_ || terminal || force) {
            return NativeWriteDecision::kSend;
        }
        const Entry* entry = FindEntry(payload.axis_id);
        if (entry == nullptr || !entry->has_success ||
            !entry->last_attempt_succeeded ||
            entry->request_token != request_token || entry->epoch != epoch ||
            !(entry->payload == payload)) {
            return NativeWriteDecision::kSend;
        }
        return NativeWriteDecision::kSuppress;
    }

    void RecordAttempt(
        const NativeWritePayload& payload,
        uint64_t request_token,
        uint32_t epoch,
        bool ack_succeeded) {
        Entry* entry = FindOrCreateEntry(payload.axis_id);
        if (entry == nullptr) {
            return;
        }
        entry->last_attempt_succeeded = ack_succeeded;
        if (!ack_succeeded) {
            return;
        }
        entry->payload = payload;
        entry->request_token = request_token;
        entry->epoch = epoch;
        entry->has_success = true;
    }

    void InvalidateAxis(int axis_id) {
        Entry* entry = FindEntry(axis_id);
        if (entry != nullptr) {
            *entry = Entry{};
        }
    }

    void InvalidateAll() {
        entries_ = {};
    }

private:
    struct Entry {
        NativeWritePayload payload{};
        uint64_t request_token = 0;
        uint32_t epoch = 0;
        int axis_id = 0;
        bool occupied = false;
        bool has_success = false;
        bool last_attempt_succeeded = false;
    };

    Entry* FindEntry(int axis_id) {
        for (auto& entry : entries_) {
            if (entry.occupied && entry.axis_id == axis_id) {
                return &entry;
            }
        }
        return nullptr;
    }

    const Entry* FindEntry(int axis_id) const {
        for (const auto& entry : entries_) {
            if (entry.occupied && entry.axis_id == axis_id) {
                return &entry;
            }
        }
        return nullptr;
    }

    Entry* FindOrCreateEntry(int axis_id) {
        if (Entry* existing = FindEntry(axis_id); existing != nullptr) {
            return existing;
        }
        for (auto& entry : entries_) {
            if (!entry.occupied) {
                entry.occupied = true;
                entry.axis_id = axis_id;
                return &entry;
            }
        }
        return nullptr;
    }

    bool enabled_ = false;
    std::array<Entry, 2> entries_{};
};

struct NativeWriteTelemetryEvent {
    uint64_t mono_us;
    uint32_t dt_us;
    int axis_id;
    uint64_t request_token;
    int position;
    uint16_t time_ms;
    uint16_t speed;
    uint32_t write_duration_us;
    bool terminal;
    bool force;
    bool eligible;
    bool suppressed;
    bool attempted;
    bool ack_ok;
    bool moving;
};

// Fixed-capacity, allocation-free storage. Diagnostics are explicitly
// enabled for a bounded stopped-motion experiment and disabled before
// readout, so a full buffer is evidence of an invalid experiment rather than
// a reason to overwrite earlier timing data.
template <std::size_t Capacity>
class NativeWriteTelemetryBuffer {
public:
    static_assert(Capacity > 0, "telemetry capacity must be positive");

    bool enabled() const { return enabled_; }

    void SetEnabled(bool enabled) {
        if (enabled_ == enabled) {
            return;
        }
        if (enabled) {
            Clear();
        }
        enabled_ = enabled;
    }

    void Clear() {
        size_ = 0;
        overflow_count_ = 0;
    }

    void Push(const NativeWriteTelemetryEvent& event) {
        if (!enabled_) {
            return;
        }
        if (size_ == Capacity) {
            ++overflow_count_;
            return;
        }
        events_[size_++] = event;
    }

    std::size_t size() const { return size_; }
    uint32_t overflow_count() const { return overflow_count_; }
    const NativeWriteTelemetryEvent& At(std::size_t index) const {
        return events_[index];
    }

private:
    bool enabled_ = false;
    std::array<NativeWriteTelemetryEvent, Capacity> events_{};
    std::size_t size_ = 0;
    uint32_t overflow_count_ = 0;
};

template <typename WriteFrame>
inline bool WriteHeadSpringFrameIfFresh(
    uint64_t snapshot_token,
    uint64_t live_token,
    WriteFrame&& write_frame) {
    if (snapshot_token != live_token) {
        return false;
    }
    write_frame();
    return true;
}

// The token reader owns the short motion-state critical section. A matching
// token reserves at most this one frame under the caller's bus lock. The
// reader must return before write_frame starts so a blocking servo write
// cannot delay a newer target from being accepted. A retarget after the
// reservation is handled by the next freshness gate and the caller's
// post-write token guard.
template <
    typename ReadLiveToken,
    typename WriteFrame,
    typename = std::enable_if_t<
        std::is_invocable_v<ReadLiveToken&>>>
inline bool WriteHeadSpringFrameIfFresh(
    uint64_t snapshot_token,
    ReadLiveToken&& read_live_token,
    WriteFrame&& write_frame) {
    const uint64_t live_token = read_live_token();
    return WriteHeadSpringFrameIfFresh(
        snapshot_token, live_token,
        static_cast<WriteFrame&&>(write_frame));
}

inline int RoundedNativePosition(
    float degrees,
    int zero_raw,
    int minimum_raw,
    int maximum_raw) {
    const long rounded =
        zero_raw + std::lround(degrees * 16.0f / 5.0f);
    return std::clamp(
        static_cast<int>(rounded),
        minimum_raw,
        maximum_raw);
}

inline uint32_t EnsurePositionRecoveryDurationMs(
    uint32_t requested_duration_ms,
    bool position_unknown,
    uint32_t recovery_minimum_ms) {
    if (!position_unknown) {
        return requested_duration_ms;
    }
    return std::max(requested_duration_ms, recovery_minimum_ms);
}

inline bool ShouldClearFinalNativeWrite(
    bool final_native_write,
    bool write_succeeded) {
    return final_native_write && write_succeeded;
}

inline float SelectSpringFrameDegrees(
    float interpolated_degrees,
    int target_degrees,
    bool final_native_write) {
    return final_native_write
        ? static_cast<float>(target_degrees)
        : interpolated_degrees;
}

inline void AdvanceAxisSpring(
    smooth_ui_toolkit::AnimateValue& axis_anim,
    bool& snap_on_rest,
    float dt_s,
    int& new_current_deg,
    bool& new_moving) {
    if (!new_moving) {
        return;
    }

    axis_anim.updateWithDelta(dt_s);
    new_current_deg = static_cast<int>(axis_anim.directValue());
    if (axis_anim.done()) {
        new_moving = false;
        if (snap_on_rest) {
            new_current_deg = static_cast<int>(axis_anim.end);
        }
    }
}

inline void StartOrRetargetAxisSpring(
    smooth_ui_toolkit::AnimateValue& axis_anim,
    bool& snap_on_rest,
    int current_deg,
    int target_deg,
    bool moving,
    bool was_spring_moving,
    const smooth_ui_toolkit::SpringOptions_t& spring_options) {
    axis_anim.springOptions() = spring_options;

    if (!moving) {
        axis_anim.teleport(static_cast<float>(current_deg));
        axis_anim.updateWithDelta(0.0f);
        // A spring can be fractional even when its reported integer pose
        // already equals the new target. Keep one exact native write pending,
        // including a prior final write that has not yet succeeded.
        snap_on_rest = snap_on_rest || was_spring_moving;
        return;
    }

    if (!was_spring_moving) {
        axis_anim.teleport(static_cast<float>(current_deg));
        // Spring::init() leaves its cached current velocity untouched until
        // the next generator step. Advance by zero explicit time so a move
        // restarted after an external stop begins with the configured
        // zero velocity instead of inheriting stale momentum.
        axis_anim.updateWithDelta(0.0f);
    }
    // Host interpolation advances with updateWithDelta(). For an existing
    // spring, directValue() preserves its fractional position and retarget()
    // carries its current velocity forward without a wall-clock update.
    axis_anim.retarget(
        axis_anim.directValue(), static_cast<float>(target_deg));
    snap_on_rest = true;
}

}  // namespace stackchan_motion
