#include "camera_stream_protocol.h"
#include "camera_datagram_protocol.h"
#include "esp_video_dqbuf_timeout.h"

#include <gtest/gtest.h>

#include <atomic>
#include <cstdint>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr uint8_t kJpeg[] = {0xff, 0xd8, 0x11, 0x22, 0xff, 0xd9};

TEST(CameraStreamProtocolTest, DiscardsCapturedFramesWithoutDeliveryCredit) {
    EXPECT_EQ(
        SelectCameraStreamCapturedFrameAction(false),
        CameraStreamCapturedFrameAction::kDiscard
    );
    EXPECT_EQ(
        SelectCameraStreamCapturedFrameAction(true),
        CameraStreamCapturedFrameAction::kDeliver
    );
}

TEST(CameraStreamProtocolTest, UsesSensorDimensionsForUnrotatedStreamInput) {
    const auto rotated =
        SelectCameraStreamDimensions(240, 320, 320, 240, true);
    EXPECT_EQ(rotated.width, 320);
    EXPECT_EQ(rotated.height, 240);

    const auto unrotated =
        SelectCameraStreamDimensions(320, 240, 0, 0, false);
    EXPECT_EQ(unrotated.width, 320);
    EXPECT_EQ(unrotated.height, 240);
}

TEST(CameraStreamProtocolTest, EscapesJsonRpcErrorMessageCharacters) {
    EXPECT_EQ(
        camera_stream_protocol::EscapeJsonString("bad \"message\"\\\n"),
        "bad \\\"message\\\"\\\\\\n"
    );
}

TEST(CameraStreamProtocolTest, NeverFallsBackToTheControlTransportForCameraMedia) {
    EXPECT_EQ(
        SelectCameraStreamSendAction(false),
        CameraStreamSendAction::kReject
    );
    EXPECT_EQ(
        SelectCameraStreamSendAction(true),
        CameraStreamSendAction::kSendDatagram
    );
}

TEST(CameraStreamProtocolTest, AcceptsOnlyValidDatagramConfigOnCameraWebSocket) {
    EXPECT_EQ(
        SelectCameraMediaTextAction(
            "camera_datagram_config",
            1,
            18765,
            1200,
            true
        ),
        CameraMediaTextAction::kConfigureDatagram
    );
    EXPECT_EQ(
        SelectCameraMediaTextAction("camera_stream_credit", 1, 18765, 1200, true),
        CameraMediaTextAction::kReject
    );
    EXPECT_EQ(
        SelectCameraMediaTextAction("camera_datagram_config", 2, 18765, 1200, true),
        CameraMediaTextAction::kReject
    );
    EXPECT_EQ(
        SelectCameraMediaTextAction("camera_datagram_config", 1, 0, 1200, true),
        CameraMediaTextAction::kReject
    );
    EXPECT_EQ(
        SelectCameraMediaTextAction("camera_datagram_config", 1, 18765, 1201, true),
        CameraMediaTextAction::kReject
    );
    EXPECT_EQ(
        SelectCameraMediaTextAction("camera_datagram_config", 1, 18765, 1200, false),
        CameraMediaTextAction::kReject
    );
}

TEST(CameraStreamProtocolTest, StopsDatagramSendAtFirstFailureWithoutTcpFallback) {
    CameraDatagramToken token{};
    std::vector<uint8_t> frame(3000, 0x5a);
    const auto datagrams = BuildCameraFrameDatagrams(
        token,
        9,
        frame.data(),
        frame.size()
    );
    ASSERT_EQ(datagrams.size(), 3U);
    int udp_sends = 0;
    int websocket_sends = 0;

    const bool sent = SendCameraDatagramsOnce(
        datagrams,
        [&](const std::string& datagram) {
            ++udp_sends;
            return udp_sends == 1
                ? static_cast<int>(datagram.size())
                : -1;
        }
    );

    EXPECT_FALSE(sent);
    EXPECT_EQ(udp_sends, 2);
    EXPECT_EQ(websocket_sends, 0);
}

TEST(CameraStreamProtocolTest, UnexpectedMediaDisconnectClosesSessionAndReconnects) {
    const auto intentional = SelectCameraMediaDisconnectActions(false);
    EXPECT_FALSE(intentional.notify_session_closed);
    EXPECT_FALSE(intentional.reconnect);

    const auto unexpected = SelectCameraMediaDisconnectActions(true);
    EXPECT_TRUE(unexpected.notify_session_closed);
    EXPECT_TRUE(unexpected.reconnect);
}

TEST(CameraStreamProtocolTest, BoundsDriverDequeueWaitBelowTheStopBudget) {
    EXPECT_EQ(ESP_VIDEO_DQBUF_TIMEOUT_MS, 250U);
    EXPECT_LT(ESP_VIDEO_DQBUF_TIMEOUT_MS, 5000U);
}

TEST(CameraStreamProtocolTest, NamesEveryStreamWorkerStageForStopDiagnostics) {
    EXPECT_STREQ(CameraStreamWorkerStageName(CameraStreamWorkerStage::kIdle), "idle");
    EXPECT_STREQ(CameraStreamWorkerStageName(CameraStreamWorkerStage::kWaiting), "waiting");
    EXPECT_STREQ(
        CameraStreamWorkerStageName(CameraStreamWorkerStage::kCaptureLock),
        "capture-lock"
    );
    EXPECT_STREQ(
        CameraStreamWorkerStageName(CameraStreamWorkerStage::kDequeue),
        "dequeue"
    );
    EXPECT_STREQ(CameraStreamWorkerStageName(CameraStreamWorkerStage::kEncode), "encode");
    EXPECT_STREQ(CameraStreamWorkerStageName(CameraStreamWorkerStage::kRequeue), "requeue");
    EXPECT_STREQ(CameraStreamWorkerStageName(CameraStreamWorkerStage::kPublish), "publish");
}

TEST(CameraStreamProtocolTest, BuildsScl1EnvelopeWithBoundedJsonHeader) {
    CameraStreamMetadata metadata{
        .sequence = 7,
        .captured_at_ms = 1000,
        .encoded_at_ms = 1012,
        .width = 320,
        .height = 240,
        .quality = 60,
        .device_id = "stackchan-test",
    };

    auto packet = BuildCameraStreamPacket(metadata, kJpeg, sizeof(kJpeg));

    ASSERT_GE(packet.size(), 8U + sizeof(kJpeg));
    EXPECT_EQ(std::string(packet.begin(), packet.begin() + 4), "SCL1");
    EXPECT_EQ(packet[4], 1);
    EXPECT_EQ(packet[5], 0);
    const size_t header_length =
        (static_cast<size_t>(packet[6]) << 8) | packet[7];
    ASSERT_GT(header_length, 0U);
    ASSERT_EQ(packet.size(), 8U + header_length + sizeof(kJpeg));

    const std::string header(
        packet.begin() + 8,
        packet.begin() + 8 + header_length
    );
    EXPECT_NE(header.find("\"frameId\":\"7\""), std::string::npos);
    EXPECT_NE(header.find("\"deviceId\":\"stackchan-test\""), std::string::npos);
    EXPECT_NE(header.find("\"mimeType\":\"image/jpeg\""), std::string::npos);
    EXPECT_NE(header.find("\"width\":320"), std::string::npos);
    EXPECT_NE(header.find("\"height\":240"), std::string::npos);
    EXPECT_NE(header.find("\"byteLength\":6"), std::string::npos);
    EXPECT_NE(header.find("\"transport\":\"binary\""), std::string::npos);
    EXPECT_NE(header.find("\"seq\":7"), std::string::npos);
    EXPECT_NE(header.find("\"captureTimestampMs\":1000"), std::string::npos);
    EXPECT_NE(header.find("\"deviceEncodedAtMs\":1012"), std::string::npos);
    EXPECT_NE(header.find("\"quality\":60"), std::string::npos);

    const std::vector<uint8_t> jpeg(
        packet.begin() + 8 + header_length,
        packet.end()
    );
    EXPECT_EQ(jpeg, std::vector<uint8_t>(std::begin(kJpeg), std::end(kJpeg)));
}

TEST(CameraStreamProtocolTest, RejectsInvalidMetadataOrJpeg) {
    CameraStreamMetadata valid{
        .sequence = 1,
        .captured_at_ms = 1000,
        .encoded_at_ms = 1001,
        .width = 320,
        .height = 240,
        .quality = 60,
        .device_id = "stackchan-test",
    };

    EXPECT_TRUE(BuildCameraStreamPacket(valid, nullptr, 0).empty());

    auto bad_quality = valid;
    bad_quality.quality = 101;
    EXPECT_TRUE(
        BuildCameraStreamPacket(bad_quality, kJpeg, sizeof(kJpeg)).empty()
    );

    auto bad_time = valid;
    bad_time.encoded_at_ms = 999;
    EXPECT_TRUE(
        BuildCameraStreamPacket(bad_time, kJpeg, sizeof(kJpeg)).empty()
    );
}

TEST(CameraStreamProtocolTest, LatestSlotReplacesOneUnsentPacket) {
    LatestCameraPacketSlot slot;
    CameraStreamPacket first{
        .sequence = 10,
        .bytes = {1, 2, 3},
    };
    CameraStreamPacket second{
        .sequence = 11,
        .bytes = {4, 5, 6},
    };

    EXPECT_FALSE(slot.Publish(std::move(first)));
    EXPECT_TRUE(slot.Publish(std::move(second)));
    EXPECT_EQ(slot.replaced_packets(), 1U);

    auto taken = slot.Take();
    ASSERT_TRUE(taken.has_value());
    EXPECT_EQ(taken->sequence, 11U);
    EXPECT_EQ(taken->bytes, (std::vector<uint8_t>{4, 5, 6}));
    EXPECT_FALSE(slot.Take().has_value());
}

TEST(CameraStreamProtocolTest, ReplacedPacketsRefundTheirStreamCredits) {
    LatestCameraPacketSlot slot;
    int available_credits = 4;

    for (uint32_t sequence = 1; sequence <= 6; ++sequence) {
        ASSERT_GT(available_credits, 0);
        --available_credits;
        slot.Publish(
            CameraStreamPacket{
                sequence,
                {static_cast<uint8_t>(sequence)},
            },
            [&available_credits]() {
                ++available_credits;
            }
        );
    }

    EXPECT_EQ(available_credits, 3);
    EXPECT_EQ(slot.replaced_packets(), 5U);
}

TEST(CameraStreamProtocolTest, DedicatedSendLaneNeverBlocksThePublishingThread) {
    std::mutex mutex;
    std::condition_variable condition;
    bool first_send_started = false;
    bool release_first_send = false;
    std::vector<uint32_t> sent_sequences;
    int refunded = 0;

    CameraPacketSendLane lane(
        [&](const CameraStreamPacket& packet) {
            std::unique_lock<std::mutex> lock(mutex);
            sent_sequences.push_back(packet.sequence);
            if (packet.sequence == 1) {
                first_send_started = true;
                condition.notify_all();
                condition.wait(lock, [&]() { return release_first_send; });
            }
            condition.notify_all();
            return true;
        },
        [&]() { ++refunded; }
    );

    lane.Publish(CameraStreamPacket{1, {1}});
    {
        std::unique_lock<std::mutex> lock(mutex);
        ASSERT_TRUE(condition.wait_for(
            lock,
            std::chrono::milliseconds(100),
            [&]() { return first_send_started; }
        ));
    }

    lane.Publish(CameraStreamPacket{2, {2}});
    lane.Publish(CameraStreamPacket{3, {3}});
    {
        std::lock_guard<std::mutex> lock(mutex);
        release_first_send = true;
    }
    condition.notify_all();

    {
        std::unique_lock<std::mutex> lock(mutex);
        ASSERT_TRUE(condition.wait_for(
            lock,
            std::chrono::milliseconds(100),
            [&]() { return sent_sequences.size() == 2; }
        ));
        EXPECT_EQ(sent_sequences, (std::vector<uint32_t>{1, 3}));
    }
    EXPECT_EQ(refunded, 1);
}

TEST(CameraStreamProtocolTest, FailedTransportSendConsumesItsCredit) {
    std::mutex mutex;
    std::condition_variable condition;
    bool send_finished = false;
    std::atomic<int> refunded{0};

    CameraPacketSendLane lane(
        [&](const CameraStreamPacket&) {
            std::lock_guard<std::mutex> lock(mutex);
            send_finished = true;
            condition.notify_all();
            return false;
        },
        [&]() { refunded.fetch_add(1); }
    );

    lane.Publish(CameraStreamPacket{1, {1}});
    {
        std::unique_lock<std::mutex> lock(mutex);
        ASSERT_TRUE(condition.wait_for(
            lock,
            std::chrono::milliseconds(100),
            [&]() { return send_finished; }
        ));
    }

    EXPECT_EQ(refunded.load(), 0);
}

TEST(CameraStreamProtocolTest, OwnerRejectsPublishWhileSendLaneIsBeingDestroyed) {
    std::mutex mutex;
    std::condition_variable condition;
    bool send_started = false;
    bool release_send = false;
    std::atomic<int> refunded{0};

    CameraPacketSendLaneOwner owner(
        [&](const CameraStreamPacket&) {
            std::unique_lock<std::mutex> lock(mutex);
            send_started = true;
            condition.notify_all();
            condition.wait(lock, [&]() { return release_send; });
            return true;
        },
        [&]() { refunded.fetch_add(1); }
    );

    EXPECT_TRUE(owner.Publish(CameraStreamPacket{1, {1}}));
    {
        std::unique_lock<std::mutex> lock(mutex);
        ASSERT_TRUE(condition.wait_for(
            lock,
            std::chrono::milliseconds(100),
            [&]() { return send_started; }
        ));
    }

    std::thread resetter([&]() { owner.Reset(); });
    while (owner.active()) {
        std::this_thread::yield();
    }

    EXPECT_FALSE(owner.Publish(CameraStreamPacket{2, {2}}));
    {
        std::lock_guard<std::mutex> lock(mutex);
        release_send = true;
    }
    condition.notify_all();
    resetter.join();

    EXPECT_EQ(refunded.load(), 1);
}

TEST(CameraStreamProtocolTest, QuiesceWaitsForInflightAndDiscardsQueuedPacket) {
    std::mutex mutex;
    std::condition_variable condition;
    bool send_started = false;
    bool release_send = false;
    bool quiesced = false;
    std::vector<uint32_t> sent_sequences;
    std::atomic<int> refunded{0};

    CameraPacketSendLaneOwner owner(
        [&](const CameraStreamPacket& packet) {
            std::unique_lock<std::mutex> lock(mutex);
            sent_sequences.push_back(packet.sequence);
            send_started = true;
            condition.notify_all();
            condition.wait(lock, [&]() { return release_send; });
            return true;
        },
        [&]() {
            refunded.fetch_add(1);
            condition.notify_all();
        }
    );

    EXPECT_TRUE(owner.Publish(CameraStreamPacket{40, {1}}));
    {
        std::unique_lock<std::mutex> lock(mutex);
        ASSERT_TRUE(condition.wait_for(
            lock,
            std::chrono::milliseconds(100),
            [&]() { return send_started; }
        ));
    }
    EXPECT_TRUE(owner.Publish(CameraStreamPacket{41, {2}}));

    std::thread quiescer([&]() {
        owner.Quiesce();
        std::lock_guard<std::mutex> lock(mutex);
        quiesced = true;
        condition.notify_all();
    });
    {
        std::unique_lock<std::mutex> lock(mutex);
        ASSERT_TRUE(condition.wait_for(
            lock,
            std::chrono::milliseconds(100),
            [&]() { return refunded.load() == 1; }
        ));
        EXPECT_FALSE(quiesced);
        release_send = true;
    }
    condition.notify_all();
    quiescer.join();

    EXPECT_TRUE(quiesced);
    EXPECT_EQ(sent_sequences, (std::vector<uint32_t>{40}));
    EXPECT_EQ(refunded.load(), 1);
}

TEST(CameraStreamProtocolTest, QuiescedSendLaneAcceptsLaterPackets) {
    std::mutex mutex;
    std::condition_variable condition;
    std::vector<uint32_t> sent_sequences;
    std::atomic<int> refunded{0};

    CameraPacketSendLaneOwner owner(
        [&](const CameraStreamPacket& packet) {
            std::lock_guard<std::mutex> lock(mutex);
            sent_sequences.push_back(packet.sequence);
            condition.notify_all();
            return true;
        },
        [&]() { refunded.fetch_add(1); }
    );

    owner.Quiesce();
    EXPECT_TRUE(owner.active());
    EXPECT_TRUE(owner.Publish(CameraStreamPacket{52, {1}}));
    {
        std::unique_lock<std::mutex> lock(mutex);
        ASSERT_TRUE(condition.wait_for(
            lock,
            std::chrono::milliseconds(100),
            [&]() { return sent_sequences == std::vector<uint32_t>{52}; }
        ));
    }
    EXPECT_EQ(refunded.load(), 0);
}

}  // namespace
