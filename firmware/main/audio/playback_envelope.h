#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace stackchan::audio {

// Measure the decoded PCM handed to the speaker, before any codec volume scaling.
inline uint8_t MeasurePlaybackLevel(const int16_t* samples, size_t count, bool muted = false) {
    if (muted || count == 0) return 0;
    uint64_t energy = 0;
    for (size_t i = 0; i < count; ++i) {
        const int64_t sample = samples[i];
        energy += static_cast<uint64_t>(sample * sample);
    }
    const float rms = std::sqrt(static_cast<float>(energy) / count);
    constexpr float kNoiseFloor = 120.0f;
    constexpr float kFullOpening = 6000.0f;
    return static_cast<uint8_t>(std::clamp((rms - kNoiseFloor) * 100.0f /
                                        (kFullOpening - kNoiseFloor), 0.0f, 100.0f));
}

// One animation owner advances this at 40 ms. Audio callbacks only publish samples.
class PlaybackEnvelope {
public:
    static constexpr uint32_t kTickMs = 40;
    static constexpr uint32_t kStaleMs = 180;

    int Advance(uint8_t level, uint32_t sample_ms, uint32_t now_ms) {
        if (static_cast<uint32_t>(now_ms - sample_ms) >= kStaleMs) return Reset();
        const int target = std::min<int>(level, 100);
        if (target > opening_) {
            opening_ += ((target - opening_) * 3 + 3) / 4;
        } else {
            opening_ -= (opening_ - target + 1) / 2;
        }
        if (target == 0 && opening_ <= 4) opening_ = 0;
        return opening_;
    }

    int Reset() { return opening_ = 0; }
    int opening() const { return opening_; }

private:
    int opening_ = 0;
};

}  // namespace stackchan::audio
