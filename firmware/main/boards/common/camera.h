#ifndef CAMERA_H
#define CAMERA_H

#include <cstddef>
#include <functional>
#include <string>

#include "camera_stream_protocol.h"

class Camera {
public:
    using StreamFrameSink = std::function<void(
        const CameraStreamMetadata& metadata,
        const uint8_t* jpeg,
        size_t jpeg_size
    )>;

    virtual ~Camera() = default;
    virtual void SetExplainUrl(const std::string& url, const std::string& token) = 0;
    virtual bool Capture() = 0;
    virtual bool SetHMirror(bool enabled) = 0;
    virtual bool SetVFlip(bool enabled) = 0;
    virtual bool SetSwapBytes(bool enabled) { return false; }  // Optional, default no-op
    virtual std::string Explain(const std::string& question) = 0;
    virtual bool StartStream(int fps, int quality, StreamFrameSink sink) {
        return false;
    }
    virtual void StopStream() {}
    virtual void GrantStreamCredits(uint32_t credits) {}
    virtual std::string GetStreamStatus() const {
        return R"({"running":false,"supported":false})";
    }
};

#endif // CAMERA_H
