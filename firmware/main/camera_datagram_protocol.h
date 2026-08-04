#ifndef CAMERA_DATAGRAM_PROTOCOL_H
#define CAMERA_DATAGRAM_PROTOCOL_H

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

inline constexpr std::string_view kCameraDatagramMagic = "SCU1";
inline constexpr uint8_t kCameraDatagramVersion = 1;
inline constexpr uint8_t kCameraDatagramFrameKind = 1;
inline constexpr uint8_t kCameraDatagramCreditKind = 2;
inline constexpr uint8_t kCameraDatagramHelloKind = 3;
inline constexpr size_t kCameraDatagramTokenBytes = 16;
inline constexpr size_t kCameraDatagramFrameHeaderBytes = 38;
inline constexpr size_t kCameraDatagramMaxBytes = 1200;
inline constexpr size_t kCameraDatagramMaxFrameBytes = 5U * 1024U * 1024U;

using CameraDatagramToken = std::array<uint8_t, kCameraDatagramTokenBytes>;

namespace camera_datagram_protocol {

inline void AppendBigEndian16(std::string& output, uint16_t value) {
    output.push_back(static_cast<char>((value >> 8U) & 0xFFU));
    output.push_back(static_cast<char>(value & 0xFFU));
}

inline void AppendBigEndian32(std::string& output, uint32_t value) {
    output.push_back(static_cast<char>((value >> 24U) & 0xFFU));
    output.push_back(static_cast<char>((value >> 16U) & 0xFFU));
    output.push_back(static_cast<char>((value >> 8U) & 0xFFU));
    output.push_back(static_cast<char>(value & 0xFFU));
}

inline uint32_t Crc32(const uint8_t* data, size_t size) {
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t index = 0; index < size; ++index) {
        crc ^= data[index];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1U) ^ ((crc & 1U) ? 0xEDB88320U : 0U);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

inline int HexDigit(char value) {
    if (value >= '0' && value <= '9') {
        return value - '0';
    }
    if (value >= 'a' && value <= 'f') {
        return value - 'a' + 10;
    }
    if (value >= 'A' && value <= 'F') {
        return value - 'A' + 10;
    }
    return -1;
}

inline void AppendPrefix(
    std::string& output,
    uint8_t kind,
    const CameraDatagramToken& token
) {
    output.append(kCameraDatagramMagic);
    output.push_back(static_cast<char>(kCameraDatagramVersion));
    output.push_back(static_cast<char>(kind));
    output.append(
        reinterpret_cast<const char*>(token.data()),
        token.size()
    );
}

inline bool HasPrefix(std::string_view datagram, uint8_t kind) {
    return datagram.size() >= 6 &&
           datagram.substr(0, kCameraDatagramMagic.size()) ==
               kCameraDatagramMagic &&
           static_cast<uint8_t>(datagram[4]) == kCameraDatagramVersion &&
           static_cast<uint8_t>(datagram[5]) == kind;
}

}  // namespace camera_datagram_protocol

inline std::optional<CameraDatagramToken> ParseCameraDatagramTokenHex(
    std::string_view hex
) {
    if (hex.size() != kCameraDatagramTokenBytes * 2U) {
        return std::nullopt;
    }
    CameraDatagramToken token{};
    for (size_t index = 0; index < token.size(); ++index) {
        const int high = camera_datagram_protocol::HexDigit(hex[index * 2U]);
        const int low = camera_datagram_protocol::HexDigit(hex[index * 2U + 1U]);
        if (high < 0 || low < 0) {
            return std::nullopt;
        }
        token[index] = static_cast<uint8_t>((high << 4U) | low);
    }
    return token;
}

inline std::string BuildCameraDatagramHello(
    const CameraDatagramToken& token
) {
    std::string datagram;
    datagram.reserve(6U + token.size());
    camera_datagram_protocol::AppendPrefix(
        datagram,
        kCameraDatagramHelloKind,
        token
    );
    return datagram;
}

template <typename Sender, typename Pause>
bool SendCameraDatagramHelloBurst(
    const CameraDatagramToken& token,
    Sender&& sender,
    Pause&& pause
) {
    constexpr int kHelloAttempts = 3;
    const std::string hello = BuildCameraDatagramHello(token);
    for (int attempt = 0; attempt < kHelloAttempts; ++attempt) {
        if (sender(hello) != static_cast<int>(hello.size())) {
            return false;
        }
        if (attempt + 1 < kHelloAttempts) {
            pause();
        }
    }
    return true;
}

inline std::string BuildCameraDatagramCredit(
    const CameraDatagramToken& token,
    uint8_t credits
) {
    if (credits < 1U || credits > 4U) {
        return {};
    }
    std::string datagram;
    datagram.reserve(7U + token.size());
    camera_datagram_protocol::AppendPrefix(
        datagram,
        kCameraDatagramCreditKind,
        token
    );
    datagram.push_back(static_cast<char>(credits));
    return datagram;
}

inline std::optional<uint8_t> ParseCameraDatagramCredit(
    std::string_view datagram,
    const CameraDatagramToken& expected_token
) {
    constexpr size_t kCreditBytes = 7U + kCameraDatagramTokenBytes;
    if (datagram.size() != kCreditBytes ||
        !camera_datagram_protocol::HasPrefix(
            datagram,
            kCameraDatagramCreditKind
        ) ||
        !std::equal(
            expected_token.begin(),
            expected_token.end(),
            reinterpret_cast<const uint8_t*>(datagram.data() + 6)
        )) {
        return std::nullopt;
    }
    const auto credits = static_cast<uint8_t>(datagram.back());
    if (credits < 1U || credits > 4U) {
        return std::nullopt;
    }
    return credits;
}

inline std::vector<std::string> BuildCameraFrameDatagrams(
    const CameraDatagramToken& token,
    uint32_t sequence,
    const uint8_t* frame,
    size_t frame_size,
    size_t max_datagram_bytes = kCameraDatagramMaxBytes
) {
    if (frame == nullptr || frame_size == 0U ||
        frame_size > kCameraDatagramMaxFrameBytes ||
        max_datagram_bytes <= kCameraDatagramFrameHeaderBytes ||
        max_datagram_bytes > kCameraDatagramMaxBytes) {
        return {};
    }
    const size_t payload_bytes =
        max_datagram_bytes - kCameraDatagramFrameHeaderBytes;
    const size_t chunk_count =
        (frame_size + payload_bytes - 1U) / payload_bytes;
    if (chunk_count == 0U ||
        chunk_count > std::numeric_limits<uint16_t>::max()) {
        return {};
    }

    const uint32_t frame_crc32 =
        camera_datagram_protocol::Crc32(frame, frame_size);
    std::vector<std::string> datagrams;
    datagrams.reserve(chunk_count);
    for (size_t chunk_index = 0; chunk_index < chunk_count; ++chunk_index) {
        const size_t offset = chunk_index * payload_bytes;
        const size_t size = std::min(payload_bytes, frame_size - offset);
        std::string datagram;
        datagram.reserve(kCameraDatagramFrameHeaderBytes + size);
        camera_datagram_protocol::AppendPrefix(
            datagram,
            kCameraDatagramFrameKind,
            token
        );
        camera_datagram_protocol::AppendBigEndian32(datagram, sequence);
        camera_datagram_protocol::AppendBigEndian16(
            datagram,
            static_cast<uint16_t>(chunk_index)
        );
        camera_datagram_protocol::AppendBigEndian16(
            datagram,
            static_cast<uint16_t>(chunk_count)
        );
        camera_datagram_protocol::AppendBigEndian32(
            datagram,
            static_cast<uint32_t>(frame_size)
        );
        camera_datagram_protocol::AppendBigEndian32(datagram, frame_crc32);
        datagram.append(
            reinterpret_cast<const char*>(frame + offset),
            size
        );
        datagrams.push_back(std::move(datagram));
    }
    return datagrams;
}

inline std::optional<std::string> ExtractCameraDatagramHost(
    std::string_view websocket_url
) {
    constexpr std::string_view kWsScheme = "ws://";
    constexpr std::string_view kWssScheme = "wss://";
    size_t authority_start = 0;
    if (websocket_url.substr(0, kWsScheme.size()) == kWsScheme) {
        authority_start = kWsScheme.size();
    } else if (websocket_url.substr(0, kWssScheme.size()) == kWssScheme) {
        authority_start = kWssScheme.size();
    } else {
        return std::nullopt;
    }
    const size_t authority_end = websocket_url.find_first_of(
        "/?#",
        authority_start
    );
    const std::string_view authority = websocket_url.substr(
        authority_start,
        authority_end == std::string_view::npos
            ? std::string_view::npos
            : authority_end - authority_start
    );
    if (authority.empty() || authority.find('@') != std::string_view::npos) {
        return std::nullopt;
    }
    if (authority.front() == '[') {
        const size_t bracket = authority.find(']');
        if (bracket == std::string_view::npos || bracket == 1U ||
            (bracket + 1U < authority.size() && authority[bracket + 1U] != ':')) {
            return std::nullopt;
        }
        return std::string(authority.substr(1U, bracket - 1U));
    }
    const size_t colon = authority.find(':');
    const std::string_view host = authority.substr(0, colon);
    if (host.empty() ||
        (colon != std::string_view::npos &&
         authority.find(':', colon + 1U) != std::string_view::npos)) {
        return std::nullopt;
    }
    return std::string(host);
}

inline std::optional<std::string> SelectCameraDatagramHost(
    std::string_view advertised_host,
    std::string_view websocket_url
) {
    if (advertised_host.empty()) {
        return ExtractCameraDatagramHost(websocket_url);
    }
    if (advertised_host.size() > 253U) {
        return std::nullopt;
    }
    for (const unsigned char ch : advertised_host) {
        if (ch <= 0x20U || ch >= 0x7FU || ch == '/' || ch == '?' ||
            ch == '#' || ch == '@' || ch == '[' || ch == ']') {
            return std::nullopt;
        }
    }
    return std::string(advertised_host);
}

#endif  // CAMERA_DATAGRAM_PROTOCOL_H
