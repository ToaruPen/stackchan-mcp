#include "camera_datagram_protocol.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>
#include <vector>

namespace {

std::string Hex(std::string_view bytes) {
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (const unsigned char byte : bytes) {
        out << std::setw(2) << static_cast<unsigned int>(byte);
    }
    return out.str();
}

uint16_t ReadBigEndian16(std::string_view bytes, size_t offset) {
    return (static_cast<uint16_t>(static_cast<uint8_t>(bytes[offset])) << 8U) |
           static_cast<uint8_t>(bytes[offset + 1]);
}

uint32_t ReadBigEndian32(std::string_view bytes, size_t offset) {
    return (static_cast<uint32_t>(static_cast<uint8_t>(bytes[offset])) << 24U) |
           (static_cast<uint32_t>(static_cast<uint8_t>(bytes[offset + 1])) << 16U) |
           (static_cast<uint32_t>(static_cast<uint8_t>(bytes[offset + 2])) << 8U) |
           static_cast<uint8_t>(bytes[offset + 3]);
}

uint32_t Crc32(std::string_view bytes) {
    uint32_t crc = 0xFFFFFFFFU;
    for (const unsigned char byte : bytes) {
        crc ^= byte;
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1U) ^ ((crc & 1U) ? 0xEDB88320U : 0U);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

CameraDatagramToken GoldenToken() {
    CameraDatagramToken token{};
    for (size_t index = 0; index < token.size(); ++index) {
        token[index] = static_cast<uint8_t>(index);
    }
    return token;
}

}  // namespace

TEST(CameraDatagramProtocolTest, MatchesPythonGoldenVectors) {
    const auto token = GoldenToken();
    const std::array<uint8_t, 3> frame{'a', 'b', 'c'};

    EXPECT_EQ(
        Hex(BuildCameraDatagramHello(token)),
        "534355310103000102030405060708090a0b0c0d0e0f"
    );

    auto datagrams = BuildCameraFrameDatagrams(
        token,
        7,
        frame.data(),
        frame.size()
    );
    ASSERT_EQ(datagrams.size(), 1U);
    EXPECT_EQ(
        Hex(datagrams.front()),
        "534355310101"
        "000102030405060708090a0b0c0d0e0f"
        "000000070000000100000003352441c2"
        "616263"
    );
}

TEST(CameraDatagramProtocolTest, ParsesOnlyExactTokensAndBoundedCredits) {
    const auto token = GoldenToken();
    const auto parsed = ParseCameraDatagramTokenHex(
        "000102030405060708090a0b0c0d0e0f"
    );
    ASSERT_TRUE(parsed.has_value());
    EXPECT_EQ(*parsed, token);
    EXPECT_FALSE(ParseCameraDatagramTokenHex("0001").has_value());
    EXPECT_FALSE(ParseCameraDatagramTokenHex(
        "000102030405060708090a0b0c0d0e0g"
    ).has_value());

    std::string credit = BuildCameraDatagramCredit(token, 4);
    ASSERT_FALSE(credit.empty());
    EXPECT_EQ(
        Hex(credit),
        "534355310102000102030405060708090a0b0c0d0e0f04"
    );
    EXPECT_EQ(ParseCameraDatagramCredit(credit, token), 4);

    auto wrong_token = token;
    wrong_token[0] ^= 1U;
    EXPECT_FALSE(ParseCameraDatagramCredit(credit, wrong_token).has_value());
    credit.back() = 0;
    EXPECT_FALSE(ParseCameraDatagramCredit(credit, token).has_value());
    credit.back() = 5;
    EXPECT_FALSE(ParseCameraDatagramCredit(credit, token).has_value());
    EXPECT_TRUE(BuildCameraDatagramCredit(token, 0).empty());
    EXPECT_TRUE(BuildCameraDatagramCredit(token, 5).empty());
}

TEST(CameraDatagramProtocolTest, PrefersAdvertisedDirectHostOverWebsocketHost) {
    EXPECT_EQ(
        SelectCameraDatagramHost(
            "192.0.2.10",
            "wss://public-tunnel.example/camera"
        ),
        "192.0.2.10"
    );
    EXPECT_EQ(
        SelectCameraDatagramHost(
            "",
            "wss://public-tunnel.example/camera"
        ),
        "public-tunnel.example"
    );
    EXPECT_FALSE(
        SelectCameraDatagramHost(
            "bad host/path",
            "wss://public-tunnel.example/camera"
        ).has_value()
    );
}

TEST(CameraDatagramProtocolTest, RetriesOnlyTheSmallSessionHello) {
    const auto token = GoldenToken();
    int sends = 0;
    int pauses = 0;

    EXPECT_TRUE(SendCameraDatagramHelloBurst(
        token,
        [&](const std::string& hello) {
            ++sends;
            return static_cast<int>(hello.size());
        },
        [&]() { ++pauses; }
    ));
    EXPECT_EQ(sends, 3);
    EXPECT_EQ(pauses, 2);
}

TEST(CameraDatagramProtocolTest, SplitsLargeFramesIntoBoundedSelfDescribingChunks) {
    const auto token = GoldenToken();
    std::vector<uint8_t> frame(3000);
    for (size_t index = 0; index < frame.size(); ++index) {
        frame[index] = static_cast<uint8_t>(index & 0xFFU);
    }

    auto datagrams = BuildCameraFrameDatagrams(
        token,
        42,
        frame.data(),
        frame.size(),
        1200
    );
    ASSERT_EQ(datagrams.size(), 3U);

    std::string reconstructed;
    for (size_t index = 0; index < datagrams.size(); ++index) {
        const auto& datagram = datagrams[index];
        EXPECT_LE(datagram.size(), 1200U);
        EXPECT_EQ(datagram.substr(0, 4), "SCU1");
        EXPECT_EQ(static_cast<uint8_t>(datagram[4]), 1U);
        EXPECT_EQ(static_cast<uint8_t>(datagram[5]), 1U);
        EXPECT_EQ(ReadBigEndian32(datagram, 22), 42U);
        EXPECT_EQ(ReadBigEndian16(datagram, 26), index);
        EXPECT_EQ(ReadBigEndian16(datagram, 28), datagrams.size());
        EXPECT_EQ(ReadBigEndian32(datagram, 30), frame.size());
        EXPECT_EQ(ReadBigEndian32(datagram, 34), Crc32(
            std::string_view(
                reinterpret_cast<const char*>(frame.data()),
                frame.size()
            )
        ));
        reconstructed.append(datagram.substr(38));
    }
    EXPECT_EQ(
        reconstructed,
        std::string(
            reinterpret_cast<const char*>(frame.data()),
            frame.size()
        )
    );

    datagrams.front().back() ^= 1;
    std::string corrupted;
    for (const auto& datagram : datagrams) {
        corrupted.append(datagram.substr(38));
    }
    EXPECT_NE(ReadBigEndian32(datagrams.front(), 34), Crc32(corrupted));
}

TEST(CameraDatagramProtocolTest, RejectsInvalidFrameAndDatagramBounds) {
    const auto token = GoldenToken();
    const uint8_t byte = 1;
    EXPECT_TRUE(BuildCameraFrameDatagrams(token, 1, nullptr, 1).empty());
    EXPECT_TRUE(BuildCameraFrameDatagrams(token, 1, &byte, 0).empty());
    EXPECT_TRUE(BuildCameraFrameDatagrams(token, 1, &byte, 1, 38).empty());
    EXPECT_TRUE(BuildCameraFrameDatagrams(token, 1, &byte, 1, 1201).empty());
}

TEST(CameraDatagramProtocolTest, ExtractsGatewayHostFromWebsocketUrl) {
    EXPECT_EQ(
        ExtractCameraDatagramHost("ws://192.0.2.10:18765/"),
        std::optional<std::string>("192.0.2.10")
    );
    EXPECT_EQ(
        ExtractCameraDatagramHost("wss://gateway.example.test/path"),
        std::optional<std::string>("gateway.example.test")
    );
    EXPECT_EQ(
        ExtractCameraDatagramHost("ws://[2001:db8::1]:8765/"),
        std::optional<std::string>("2001:db8::1")
    );
    EXPECT_FALSE(ExtractCameraDatagramHost("http://192.0.2.10/").has_value());
    EXPECT_FALSE(ExtractCameraDatagramHost("ws:///missing-host").has_value());
}
