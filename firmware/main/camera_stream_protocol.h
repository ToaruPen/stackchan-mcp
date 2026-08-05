#ifndef CAMERA_STREAM_PROTOCOL_H
#define CAMERA_STREAM_PROTOCOL_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <condition_variable>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

struct CameraStreamMetadata {
    uint32_t sequence = 0;
    uint64_t captured_at_ms = 0;
    uint64_t encoded_at_ms = 0;
    uint16_t width = 0;
    uint16_t height = 0;
    uint8_t quality = 0;
    std::string device_id;
};

struct CameraStreamDimensions {
    uint16_t width = 0;
    uint16_t height = 0;
};

inline CameraStreamDimensions SelectCameraStreamDimensions(
    uint16_t frame_width,
    uint16_t frame_height,
    uint16_t sensor_width,
    uint16_t sensor_height,
    bool frame_dimensions_are_rotated) {
    return frame_dimensions_are_rotated
        ? CameraStreamDimensions{sensor_width, sensor_height}
        : CameraStreamDimensions{frame_width, frame_height};
}

namespace camera_stream_protocol {

inline std::string EscapeJsonString(const std::string& value) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string escaped;
    escaped.reserve(value.size());
    for (const auto byte : value) {
        const auto ch = static_cast<unsigned char>(byte);
        switch (ch) {
            case '"':
                escaped += "\\\"";
                break;
            case '\\':
                escaped += "\\\\";
                break;
            case '\b':
                escaped += "\\b";
                break;
            case '\f':
                escaped += "\\f";
                break;
            case '\n':
                escaped += "\\n";
                break;
            case '\r':
                escaped += "\\r";
                break;
            case '\t':
                escaped += "\\t";
                break;
            default:
                if (ch < 0x20) {
                    escaped += "\\u00";
                    escaped.push_back(kHex[(ch >> 4) & 0x0f]);
                    escaped.push_back(kHex[ch & 0x0f]);
                } else {
                    escaped.push_back(static_cast<char>(ch));
                }
                break;
        }
    }
    return escaped;
}

}  // namespace camera_stream_protocol

enum class CameraStreamCapturedFrameAction {
    kDiscard,
    kDeliver,
};

enum class CameraStreamSendAction {
    kReject,
    kSendDatagram,
};

enum class CameraMediaTextAction {
    kReject,
    kConfigureDatagram,
};

inline CameraStreamSendAction SelectCameraStreamSendAction(
    bool datagram_session_ready
) {
    return datagram_session_ready
        ? CameraStreamSendAction::kSendDatagram
        : CameraStreamSendAction::kReject;
}

inline CameraMediaTextAction SelectCameraMediaTextAction(
    std::string_view type,
    int version,
    int port,
    int max_datagram_bytes,
    bool token_valid
) {
    return type == "camera_datagram_config" && version == 1 &&
            port >= 1 && port <= 65535 &&
            max_datagram_bytes > 38 && max_datagram_bytes <= 1200 &&
            token_valid
        ? CameraMediaTextAction::kConfigureDatagram
        : CameraMediaTextAction::kReject;
}

template <typename Sender>
bool SendCameraDatagramsOnce(
    const std::vector<std::string>& datagrams,
    Sender&& sender
) {
    if (datagrams.empty()) {
        return false;
    }
    for (const auto& datagram : datagrams) {
        if (sender(datagram) != static_cast<int>(datagram.size())) {
            return false;
        }
    }
    return true;
}

struct CameraMediaDisconnectActions {
    bool notify_session_closed = false;
    bool reconnect = false;
};

inline CameraMediaDisconnectActions SelectCameraMediaDisconnectActions(
    bool reconnect_armed
) {
    return CameraMediaDisconnectActions{
        .notify_session_closed = reconnect_armed,
        .reconnect = reconnect_armed,
    };
}

enum class CameraStreamWorkerStage : uint8_t {
    kIdle,
    kWaiting,
    kCaptureLock,
    kDequeue,
    kEncode,
    kRequeue,
    kPublish,
};

inline const char* CameraStreamWorkerStageName(CameraStreamWorkerStage stage) {
    switch (stage) {
        case CameraStreamWorkerStage::kIdle:
            return "idle";
        case CameraStreamWorkerStage::kWaiting:
            return "waiting";
        case CameraStreamWorkerStage::kCaptureLock:
            return "capture-lock";
        case CameraStreamWorkerStage::kDequeue:
            return "dequeue";
        case CameraStreamWorkerStage::kEncode:
            return "encode";
        case CameraStreamWorkerStage::kRequeue:
            return "requeue";
        case CameraStreamWorkerStage::kPublish:
            return "publish";
    }
    return "unknown";
}

inline CameraStreamCapturedFrameAction SelectCameraStreamCapturedFrameAction(
    bool delivery_credit_claimed
) {
    return delivery_credit_claimed
        ? CameraStreamCapturedFrameAction::kDeliver
        : CameraStreamCapturedFrameAction::kDiscard;
}

inline std::vector<uint8_t> BuildCameraStreamPacket(
    const CameraStreamMetadata& metadata,
    const uint8_t* jpeg,
    size_t jpeg_size
) {
    if (jpeg == nullptr || jpeg_size < 4 ||
        jpeg[0] != 0xff || jpeg[1] != 0xd8 ||
        jpeg[jpeg_size - 2] != 0xff || jpeg[jpeg_size - 1] != 0xd9 ||
        metadata.width == 0 || metadata.height == 0 ||
        metadata.quality == 0 || metadata.quality > 100 ||
        metadata.encoded_at_ms < metadata.captured_at_ms ||
        metadata.device_id.empty()) {
        return {};
    }

    const std::string header =
        "{\"frameId\":\"" + std::to_string(metadata.sequence) +
        "\",\"deviceId\":\"" +
        camera_stream_protocol::EscapeJsonString(metadata.device_id) +
        "\",\"mimeType\":\"image/jpeg\"" +
        ",\"width\":" + std::to_string(metadata.width) +
        ",\"height\":" + std::to_string(metadata.height) +
        ",\"byteLength\":" + std::to_string(jpeg_size) +
        ",\"transport\":\"binary\"" +
        ",\"seq\":" + std::to_string(metadata.sequence) +
        ",\"captureTimestampMs\":" + std::to_string(metadata.captured_at_ms) +
        ",\"deviceEncodedAtMs\":" + std::to_string(metadata.encoded_at_ms) +
        ",\"quality\":" + std::to_string(metadata.quality) + "}";

    if (header.size() > std::numeric_limits<uint16_t>::max() ||
        jpeg_size > std::numeric_limits<size_t>::max() - 8 - header.size()) {
        return {};
    }

    std::vector<uint8_t> packet;
    packet.reserve(8 + header.size() + jpeg_size);
    packet.insert(packet.end(), {'S', 'C', 'L', '1', 1, 0});
    packet.push_back(static_cast<uint8_t>((header.size() >> 8) & 0xff));
    packet.push_back(static_cast<uint8_t>(header.size() & 0xff));
    packet.insert(packet.end(), header.begin(), header.end());
    packet.insert(packet.end(), jpeg, jpeg + jpeg_size);
    return packet;
}

struct CameraStreamPacket {
    uint32_t sequence = 0;
    std::vector<uint8_t> bytes;
};

class LatestCameraPacketSlot {
public:
    bool Publish(CameraStreamPacket packet) {
        std::lock_guard<std::mutex> lock(mutex_);
        const bool replaced = packet_.has_value();
        if (replaced) {
            ++replaced_packets_;
        }
        packet_ = std::move(packet);
        return replaced;
    }

    template <typename OnReplaced>
    bool Publish(CameraStreamPacket packet, OnReplaced&& on_replaced) {
        const bool replaced = Publish(std::move(packet));
        if (replaced) {
            std::forward<OnReplaced>(on_replaced)();
        }
        return replaced;
    }

    std::optional<CameraStreamPacket> Take() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!packet_.has_value()) {
            return std::nullopt;
        }
        auto packet = std::move(packet_);
        packet_.reset();
        return packet;
    }

    bool HasPacket() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return packet_.has_value();
    }

    void Clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        packet_.reset();
    }

    uint32_t replaced_packets() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return replaced_packets_;
    }

private:
    mutable std::mutex mutex_;
    std::optional<CameraStreamPacket> packet_;
    uint32_t replaced_packets_ = 0;
};

class CameraPacketSendLane {
public:
    using Sender = std::function<bool(const CameraStreamPacket&)>;
    using Refund = std::function<void()>;

    CameraPacketSendLane(Sender sender, Refund refund)
        : sender_(std::move(sender)), refund_(std::move(refund)), worker_([this]() {
              Run();
          }) {}

    ~CameraPacketSendLane() {
        {
            std::lock_guard<std::mutex> lock(wait_mutex_);
            stopping_ = true;
        }
        condition_.notify_all();
        if (worker_.joinable()) {
            worker_.join();
        }
        while (slot_.Take().has_value()) {
            refund_();
        }
    }

    CameraPacketSendLane(const CameraPacketSendLane&) = delete;
    CameraPacketSendLane& operator=(const CameraPacketSendLane&) = delete;

    void Publish(CameraStreamPacket packet) {
        std::lock_guard<std::mutex> lock(wait_mutex_);
        if (stopping_) {
            refund_();
            return;
        }
        slot_.Publish(std::move(packet), refund_);
        condition_.notify_one();
    }

    void Quiesce() {
        std::unique_lock<std::mutex> lock(wait_mutex_);
        while (slot_.Take().has_value()) {
            refund_();
        }
        condition_.wait(lock, [this]() { return !sending_; });
    }

private:
    void Run() {
        while (true) {
            std::unique_lock<std::mutex> lock(wait_mutex_);
            condition_.wait(lock, [this]() {
                return stopping_ || slot_.HasPacket();
            });
            if (stopping_) {
                return;
            }
            auto packet = slot_.Take();
            if (!packet.has_value()) {
                continue;
            }
            sending_ = true;
            lock.unlock();
            sender_(*packet);
            lock.lock();
            sending_ = false;
            condition_.notify_all();
        }
    }

    Sender sender_;
    Refund refund_;
    LatestCameraPacketSlot slot_;
    std::mutex wait_mutex_;
    std::condition_variable condition_;
    bool sending_ = false;
    bool stopping_ = false;
    std::thread worker_;
};

class CameraPacketSendLaneOwner {
public:
    using Sender = CameraPacketSendLane::Sender;
    using Refund = CameraPacketSendLane::Refund;

    CameraPacketSendLaneOwner(Sender sender, Refund refund)
        : refund_(std::move(refund)),
          lane_(std::make_unique<CameraPacketSendLane>(
              std::move(sender),
              refund_
          )) {}

    ~CameraPacketSendLaneOwner() {
        Reset();
    }

    CameraPacketSendLaneOwner(const CameraPacketSendLaneOwner&) = delete;
    CameraPacketSendLaneOwner& operator=(const CameraPacketSendLaneOwner&) = delete;

    bool Publish(CameraStreamPacket packet) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!lane_) {
            refund_();
            return false;
        }
        lane_->Publish(std::move(packet));
        return true;
    }

    void Reset() {
        std::unique_ptr<CameraPacketSendLane> lane;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            lane = std::move(lane_);
        }
        lane.reset();
    }

    void Quiesce() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (lane_) {
            lane_->Quiesce();
        }
    }

    bool active() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return lane_ != nullptr;
    }

private:
    mutable std::mutex mutex_;
    Refund refund_;
    std::unique_ptr<CameraPacketSendLane> lane_;
};

#endif  // CAMERA_STREAM_PROTOCOL_H
