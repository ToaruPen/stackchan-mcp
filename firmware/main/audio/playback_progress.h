#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>

namespace stackchan::audio {

// Owned by the audio queue mutex. Empty queues alone miss popped work and the
// PCM still buffered by I2S. The deadline estimates that tail, not DAC completion.
struct PlaybackProgress {
    bool decoding = false;
    bool outputting = false;
    uint64_t submitted_until_ms = 0;

    void Submit(size_t samples, uint32_t sample_rate, uint64_t now_ms) {
        const uint64_t duration_ms = (static_cast<uint64_t>(samples) * 1000 + sample_rate - 1) / sample_rate;
        submitted_until_ms = std::max(now_ms, submitted_until_ms) + duration_ms;
    }

    bool IsIdle(bool decode_queue_empty, bool playback_queue_empty, uint64_t now_ms) const {
        return decode_queue_empty && playback_queue_empty && !decoding && !outputting &&
               now_ms >= submitted_until_ms;
    }
};

}  // namespace stackchan::audio
