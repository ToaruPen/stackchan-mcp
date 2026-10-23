#pragma once

#include <cstdint>
#include <cstring>
#include "avatar_activity.h"
#include "device_state.h"

namespace stackchan {

class AvatarActivityPolicy {
public:
    static constexpr int64_t kThinkingTimeoutMs = 30000;

    void OnDeviceState(DeviceState state) {
        device_state_ = state;
        thinking_since_ms_ = -1;
        switch (state) {
            case kDeviceStateListening: activity_ = AvatarActivity::Listening; break;
            case kDeviceStateSpeaking: activity_ = AvatarActivity::Speaking; break;
            default: activity_ = AvatarActivity::Idle; break;
        }
    }

    bool OnActivity(AvatarActivity activity, int64_t now_ms) {
        // Gateway completions can arrive after the next local interaction.
        if (device_state_ != kDeviceStateIdle ||
            (activity != AvatarActivity::Idle && activity != AvatarActivity::Thinking)) {
            return false;
        }
        activity_ = activity;
        thinking_since_ms_ = activity == AvatarActivity::Thinking ? now_ms : -1;
        return true;
    }

    bool ExpireThinking(int64_t now_ms) {
        if (thinking_since_ms_ < 0 || now_ms - thinking_since_ms_ < kThinkingTimeoutMs) {
            return false;
        }
        activity_ = AvatarActivity::Idle;
        thinking_since_ms_ = -1;
        return true;
    }

    void SetEnabled(bool enabled) { enabled_ = enabled; }
    bool enabled() const { return enabled_; }
    bool visible() const {
        return enabled_ && (device_state_ == kDeviceStateIdle ||
            device_state_ == kDeviceStateConnecting ||
            device_state_ == kDeviceStateListening ||
            device_state_ == kDeviceStateSpeaking);
    }
    AvatarActivity activity() const { return activity_; }
    const char* ResolveExplicitFace(const char* requested) const {
        return std::strcmp(requested, "idle") == 0 ? face() : requested;
    }
    const char* face() const {
        switch (activity_) {
            case AvatarActivity::Listening: return "listening";
            case AvatarActivity::Thinking: return "thinking";
            case AvatarActivity::Speaking: return "speaking";
            default: return "idle";
        }
    }

private:
    DeviceState device_state_ = kDeviceStateUnknown;
    AvatarActivity activity_ = AvatarActivity::Idle;
    int64_t thinking_since_ms_ = -1;
    bool enabled_ = true;
};

}  // namespace stackchan
