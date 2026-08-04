#pragma once
#include "sdkconfig.h"

#include <atomic>
#include <lvgl.h>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "camera.h"
#include "jpg/image_to_jpeg.h"
#include "esp_video_init.h"

class EspVideoStreamJpegEncoder;

struct JpegChunk {
    uint8_t* data;
    size_t len;
};

class EspVideo : public Camera {
private:
    struct FrameBuffer {
        uint8_t *data = nullptr;
        size_t len = 0;
        uint16_t width = 0;
        uint16_t height = 0;
        v4l2_pix_fmt_t format = 0;
    } frame_;
    v4l2_pix_fmt_t sensor_format_ = 0;
#ifdef CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    uint16_t sensor_width_ = 0;
    uint16_t sensor_height_ = 0;
#endif  // CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    int video_fd_ = -1;
    std::atomic<bool> streaming_on_{false};
    struct MmapBuffer { void *start = nullptr; size_t length = 0; };
    std::vector<MmapBuffer> mmap_buffers_;
    std::string explain_url_;
    std::string explain_token_;
    std::thread encoder_thread_;
    std::thread camera_stream_thread_;
    std::mutex capture_mutex_;
    std::unique_ptr<EspVideoStreamJpegEncoder> camera_stream_encoder_;
    Camera::StreamFrameSink camera_stream_sink_;
    std::atomic<bool> camera_stream_running_{false};
    std::atomic<uint32_t> camera_stream_credits_{0};
    std::atomic<uint32_t> camera_stream_sequence_{0};
    std::atomic<uint32_t> camera_stream_frames_{0};
    std::atomic<uint32_t> camera_stream_encode_failures_{0};
    std::atomic<int> camera_stream_fps_{0};
    std::atomic<int> camera_stream_quality_{0};
    std::atomic<CameraStreamWorkerStage> camera_stream_stage_{
        CameraStreamWorkerStage::kIdle
    };
    bool ClaimStreamCredit();
    void RestoreStreamCredit();
    void CameraStreamLoop();

public:
    EspVideo(const esp_video_init_config_t& config);
    ~EspVideo();

    virtual void SetExplainUrl(const std::string& url, const std::string& token);
    virtual bool Capture();
    // 翻转控制函数
    virtual bool SetHMirror(bool enabled) override;
    virtual bool SetVFlip(bool enabled) override;
    virtual std::string Explain(const std::string& question);
    bool StartStream(int fps, int quality, StreamFrameSink sink) override;
    void StopStream() override;
    void GrantStreamCredits(uint32_t credits) override;
    std::string GetStreamStatus() const override;
};
