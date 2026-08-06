#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <future>
#include <map>
#include <mutex>
#include <vector>

#include "core/hal/hal.hpp"
#include "head_spring_motion.h"

namespace {

smooth_ui_toolkit::SpringOptions_t CriticalSpringOptions() {
    smooth_ui_toolkit::SpringOptions_t options;
    options.stiffness = 100.0f;
    options.damping = 20.0f;
    options.mass = 1.0f;
    options.velocity = 0.0f;
    options.restDelta = 0.001f;
    options.restSpeed = 0.001f;
    options.duration = 0.0f;
    options.bounce = 0.0f;
    options.visualDuration = 0.0f;
    return options;
}

smooth_ui_toolkit::SpringOptions_t SaturatedSmallMoveSpringOptions() {
    smooth_ui_toolkit::SpringOptions_t options;
    options.stiffness = 650.0f;
    options.damping = 2.0f * std::sqrt(650.0f);
    options.mass = 1.0f;
    options.velocity = 0.0f;
    options.restDelta = 0.5f;
    options.restSpeed = 0.5f;
    options.duration = 0.0f;
    options.bounce = 0.0f;
    options.visualDuration = 0.0f;
    return options;
}

struct NativeFrame {
    int at_ms;
    stackchan_motion::NativeWritePayload payload;
    uint64_t token;
    bool terminal;

    bool operator==(const NativeFrame& other) const {
        return at_ms == other.at_ms && payload == other.payload &&
            token == other.token && terminal == other.terminal;
    }
};

int YawRaw(float degrees) {
    return stackchan_motion::RoundedNativePosition(
        degrees, 460, 0, 1000);
}

std::vector<NativeFrame> GenerateSmallMoveFrames(int target_deg) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = SaturatedSmallMoveSpringOptions();
    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, target_deg, true, false, options);

    std::vector<NativeFrame> frames;
    bool moving = true;
    for (int tick = 1; tick <= 100 && moving; ++tick) {
        int current = 0;
        stackchan_motion::AdvanceAxisSpring(
            animation, snap_on_rest, 0.020f, current, moving);
        frames.push_back({
            tick * 20,
            {1, YawRaw(animation.directValue()), 30, 0},
            1,
            false,
        });
    }
    frames.push_back({
        frames.back().at_ms + 20,
        {1, YawRaw(static_cast<float>(target_deg)), 30, 0},
        1,
        true,
    });
    return frames;
}

std::vector<NativeFrame> GenerateSmallRetargetFrames(
    int initial_target,
    int new_target) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = SaturatedSmallMoveSpringOptions();
    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, initial_target, true, false, options);

    std::vector<NativeFrame> frames;
    bool moving = true;
    for (int tick = 1; tick <= 6; ++tick) {
        int current = 0;
        stackchan_motion::AdvanceAxisSpring(
            animation, snap_on_rest, 0.020f, current, moving);
        frames.push_back({
            tick * 20,
            {1, YawRaw(animation.directValue()), 30, 0},
            1,
            false,
        });
    }
    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest,
        static_cast<int>(animation.directValue()), new_target,
        true, true, options);
    moving = true;
    for (int tick = 7; tick <= 100 && moving; ++tick) {
        int current = static_cast<int>(animation.directValue());
        stackchan_motion::AdvanceAxisSpring(
            animation, snap_on_rest, 0.020f, current, moving);
        frames.push_back({
            tick * 20,
            {1, YawRaw(animation.directValue()), 30, 0},
            2,
            false,
        });
    }
    frames.push_back({
        frames.back().at_ms + 20,
        {1, YawRaw(static_cast<float>(new_target)), 30, 0},
        2,
        true,
    });
    return frames;
}

struct NativeTrace {
    std::vector<NativeFrame> sent;
    std::map<int, int> first_raw_ms;
    int suppressed = 0;
    int maximum_gap_ms = 0;
};

NativeTrace RunNativeTrace(
    bool suppression_enabled,
    const std::vector<NativeFrame>& frames) {
    stackchan_motion::NativeWriteSuppressor suppressor;
    suppressor.SetEnabled(suppression_enabled);
    NativeTrace trace;
    int last_sent_ms = 0;
    for (const auto& frame : frames) {
        const auto decision = suppressor.Decide(
            frame.payload, frame.token, 1, frame.terminal, false);
        if (decision == stackchan_motion::NativeWriteDecision::kSuppress) {
            ++trace.suppressed;
            continue;
        }
        if (last_sent_ms != 0) {
            trace.maximum_gap_ms = std::max(
                trace.maximum_gap_ms, frame.at_ms - last_sent_ms);
        }
        last_sent_ms = frame.at_ms;
        trace.sent.push_back(frame);
        trace.first_raw_ms.emplace(frame.payload.position, frame.at_ms);
        suppressor.RecordAttempt(frame.payload, frame.token, 1, true);
    }
    return trace;
}

class HeadSpringMotionTest : public testing::Test {
protected:
    void SetUp() override {
        smooth_ui_toolkit::ui_hal::on_get_tick([] { return 0; });
    }
};

TEST_F(HeadSpringMotionTest,
       UnknownPositionGetsEnoughTimeToResynchronizePhysicalPose) {
    EXPECT_EQ(
        stackchan_motion::EnsurePositionRecoveryDurationMs(
            20, false, 600),
        20u);
    EXPECT_EQ(
        stackchan_motion::EnsurePositionRecoveryDurationMs(
            20, true, 600),
        600u);
    EXPECT_EQ(
        stackchan_motion::EnsurePositionRecoveryDurationMs(
            1500, true, 600),
        1500u);
}

TEST_F(HeadSpringMotionTest, FreshSnapshotWritesOneBusFrame) {
    int writes = 0;

    const bool wrote = stackchan_motion::WriteHeadSpringFrameIfFresh(
        7, 7, [&writes] { ++writes; });

    EXPECT_TRUE(wrote);
    EXPECT_EQ(writes, 1);
}

TEST_F(HeadSpringMotionTest, RetargetedSnapshotWritesNoStaleBusFrame) {
    uint64_t live_request_token = 7;
    const uint64_t old_tick_snapshot_token = live_request_token;
    live_request_token = 8;
    int old_tick_writes = 0;

    const bool wrote = stackchan_motion::WriteHeadSpringFrameIfFresh(
        old_tick_snapshot_token,
        live_request_token,
        [&old_tick_writes] { ++old_tick_writes; });

    EXPECT_FALSE(wrote);
    EXPECT_EQ(old_tick_writes, 0);
}

TEST_F(HeadSpringMotionTest, RepeatedRetargetsNeverWriteSupersededFrames) {
    int stale_writes = 0;

    for (uint64_t token = 1; token <= 10000; ++token) {
        const bool wrote = stackchan_motion::WriteHeadSpringFrameIfFresh(
            token, token + 1, [&stale_writes] { ++stale_writes; });
        ASSERT_FALSE(wrote);
    }

    EXPECT_EQ(stale_writes, 0);
}

TEST_F(HeadSpringMotionTest, SlowBusWriteDoesNotBlockLatestTargetAcceptance) {
    std::mutex motion_mutex;
    uint64_t live_request_token = 7;
    std::promise<void> write_started;
    std::promise<void> release_write;
    auto release_write_future = release_write.get_future();

    auto write_future = std::async(std::launch::async, [&] {
        return stackchan_motion::WriteHeadSpringFrameIfFresh(
            7,
            [&] {
                std::lock_guard<std::mutex> lock(motion_mutex);
                return live_request_token;
            },
            [&] {
                write_started.set_value();
                release_write_future.wait();
            });
    });
    write_started.get_future().wait();

    const bool accepted_latest_target = motion_mutex.try_lock();
    if (accepted_latest_target) {
        live_request_token = 8;
        motion_mutex.unlock();
    }

    EXPECT_TRUE(accepted_latest_target);
    release_write.set_value();
    EXPECT_TRUE(write_future.get());
}

TEST_F(HeadSpringMotionTest,
       RetargetAfterReservationAllowsAtMostOneOldFrameThenLatest) {
    uint64_t live_request_token = 7;
    std::vector<uint64_t> written_tokens;

    const bool reserved_old_frame =
        stackchan_motion::WriteHeadSpringFrameIfFresh(
            7,
            [&] { return live_request_token; },
            [&] {
                // StartMove lands after the freshness gate reserved this
                // frame but before its first physical side effect.
                live_request_token = 8;
                written_tokens.push_back(7);
            });
    const bool repeated_old_frame =
        stackchan_motion::WriteHeadSpringFrameIfFresh(
            7, live_request_token,
            [&] { written_tokens.push_back(7); });
    const bool latest_frame =
        stackchan_motion::WriteHeadSpringFrameIfFresh(
            8, live_request_token,
            [&] { written_tokens.push_back(8); });

    EXPECT_TRUE(reserved_old_frame);
    EXPECT_FALSE(repeated_old_frame);
    EXPECT_TRUE(latest_frame);
    EXPECT_EQ(written_tokens, (std::vector<uint64_t>{7, 8}));
}

TEST_F(HeadSpringMotionTest, TorqueOffIsBusOrderedAfterReservedFrame) {
    enum class BusEvent { kWritePosition, kDisableTorque };

    std::mutex motion_mutex;
    std::mutex bus_mutex;
    std::mutex events_mutex;
    uint64_t live_request_token = 7;
    std::vector<BusEvent> bus_events;
    std::promise<void> frame_reserved;
    std::promise<void> invalidation_published;
    auto invalidation_published_future =
        invalidation_published.get_future();

    auto tick_future = std::async(std::launch::async, [&] {
        std::lock_guard<std::mutex> bus_lock(bus_mutex);
        return stackchan_motion::WriteHeadSpringFrameIfFresh(
            7,
            [&] {
                std::lock_guard<std::mutex> motion_lock(motion_mutex);
                frame_reserved.set_value();
                return live_request_token;
            },
            [&] {
                invalidation_published_future.wait();
                std::lock_guard<std::mutex> events_lock(events_mutex);
                bus_events.push_back(BusEvent::kWritePosition);
            });
    });

    auto torque_off_future = std::async(std::launch::async, [&] {
        frame_reserved.get_future().wait();
        {
            std::lock_guard<std::mutex> motion_lock(motion_mutex);
            live_request_token = 8;
        }
        invalidation_published.set_value();
        std::lock_guard<std::mutex> bus_lock(bus_mutex);
        std::lock_guard<std::mutex> events_lock(events_mutex);
        bus_events.push_back(BusEvent::kDisableTorque);
    });

    EXPECT_TRUE(tick_future.get());
    torque_off_future.get();

    int writes_after_torque_off = 0;
    const bool stale_frame_after_torque_off =
        stackchan_motion::WriteHeadSpringFrameIfFresh(
            7, live_request_token,
            [&] { ++writes_after_torque_off; });

    EXPECT_EQ(bus_events,
              (std::vector<BusEvent>{BusEvent::kWritePosition,
                                     BusEvent::kDisableTorque}));
    EXPECT_FALSE(stale_frame_after_torque_off);
    EXPECT_EQ(writes_after_torque_off, 0);
}

TEST_F(HeadSpringMotionTest,
       RetargetsMovingSpringWithoutPositionOrVelocityReset) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = CriticalSpringOptions();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, 30, true, false, options);
    animation.updateWithDelta(0.1f);
    const float before_retarget = animation.directValue();

    ASSERT_GT(before_retarget, 0.0f);
    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, static_cast<int>(before_retarget),
        60, true, true, options);

    EXPECT_FLOAT_EQ(animation.directValue(), before_retarget);
    EXPECT_GT(std::abs(animation.springOptions().velocity), 1.0f);
    EXPECT_TRUE(snap_on_rest);

    animation.updateWithDelta(0.01f);
    EXPECT_GT(animation.directValue(), before_retarget);
}

TEST_F(HeadSpringMotionTest, DoesNotMixWallClockIntoExplicitDeltaRetarget) {
    uint32_t tick_ms = 0;
    smooth_ui_toolkit::ui_hal::on_get_tick([&tick_ms] { return tick_ms; });
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = CriticalSpringOptions();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, 30, true, false, options);
    animation.updateWithDelta(0.1f);
    const float before_retarget = animation.directValue();

    tick_ms = 500;
    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, static_cast<int>(before_retarget),
        60, true, true, options);
    smooth_ui_toolkit::ui_hal::on_get_tick([] { return 0; });

    EXPECT_FLOAT_EQ(animation.directValue(), before_retarget);
}

TEST_F(HeadSpringMotionTest, PreservesMomentumAcrossTargetReversal) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = CriticalSpringOptions();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, 30, true, false, options);
    animation.updateWithDelta(0.1f);
    const float before_reversal = animation.directValue();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, static_cast<int>(before_reversal),
        -30, true, true, options);
    EXPECT_FLOAT_EQ(animation.directValue(), before_reversal);

    animation.updateWithDelta(0.001f);
    EXPECT_GT(animation.directValue(), before_reversal);

    for (int step = 0; step < 1000 && !animation.done(); ++step) {
        animation.updateWithDelta(0.01f);
    }
    EXPECT_TRUE(animation.done());
    EXPECT_NEAR(animation.directValue(), -30.0f, options.restDelta);
}

TEST_F(HeadSpringMotionTest, StartsAndStopsFromConfirmedIntegerPose) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = CriticalSpringOptions();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 10, 20, true, false, options);
    EXPECT_FLOAT_EQ(animation.directValue(), 10.0f);
    EXPECT_FLOAT_EQ(animation.springOptions().velocity, 0.0f);
    EXPECT_TRUE(snap_on_rest);

    animation.updateWithDelta(0.1f);
    ASSERT_GT(animation.directValue(), 10.0f);

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 12, 12, false, true, options);
    EXPECT_FLOAT_EQ(animation.directValue(), 12.0f);
    EXPECT_FLOAT_EQ(animation.springOptions().velocity, 0.0f);
    EXPECT_TRUE(snap_on_rest);
}

TEST_F(HeadSpringMotionTest,
       RetargetingFractionalSpringToTruncatedCurrentRequestsFinalWrite) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = CriticalSpringOptions();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, 30, true, false, options);
    animation.updateWithDelta(0.1f);
    const float fractional_current = animation.directValue();
    const int truncated_current = static_cast<int>(fractional_current);

    ASSERT_NE(
        fractional_current,
        static_cast<float>(truncated_current));
    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest,
        truncated_current, truncated_current, false, true, options);

    EXPECT_FLOAT_EQ(
        animation.directValue(),
        static_cast<float>(truncated_current));
    EXPECT_TRUE(snap_on_rest);
}

TEST_F(HeadSpringMotionTest,
       SameTargetRedispatchPreservesFailedFinalNativeWrite) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = true;
    const auto options = CriticalSpringOptions();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 12, 12, false, false, options);

    EXPECT_TRUE(snap_on_rest);
}

TEST_F(HeadSpringMotionTest,
       CompletedSpringKeepsExactTerminalFramePending) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = CriticalSpringOptions();
    int current_deg = 0;
    bool moving = true;

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, 30, true, false, options);
    for (int step = 0; step < 1000 && moving; ++step) {
        stackchan_motion::AdvanceAxisSpring(
            animation, snap_on_rest, 0.01f, current_deg, moving);
    }

    ASSERT_FALSE(moving);
    EXPECT_EQ(current_deg, 30);
    EXPECT_TRUE(snap_on_rest);
}

TEST_F(HeadSpringMotionTest, PendingTerminalFrameUsesExactIntegerTarget) {
    EXPECT_FLOAT_EQ(
        stackchan_motion::SelectSpringFrameDegrees(31.4f, 33, true),
        33.0f);
    EXPECT_FLOAT_EQ(
        stackchan_motion::SelectSpringFrameDegrees(31.4f, 33, false),
        31.4f);
}

TEST_F(HeadSpringMotionTest, ClearsStaleVelocityWhenStartingAfterExternalStop) {
    smooth_ui_toolkit::AnimateValue animation;
    bool snap_on_rest = false;
    const auto options = CriticalSpringOptions();

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, 0, 30, true, false, options);
    animation.updateWithDelta(0.1f);
    const int externally_confirmed_pose =
        static_cast<int>(animation.directValue());

    stackchan_motion::StartOrRetargetAxisSpring(
        animation, snap_on_rest, externally_confirmed_pose,
        -30, true, false, options);

    EXPECT_FLOAT_EQ(
        animation.directValue(),
        static_cast<float>(externally_confirmed_pose));
    EXPECT_FLOAT_EQ(animation.springOptions().velocity, 0.0f);

    animation.updateWithDelta(0.001f);
    EXPECT_LT(
        animation.directValue(),
        static_cast<float>(externally_confirmed_pose));
}

TEST_F(HeadSpringMotionTest, PreservesNativeServoStepsWithinOneDegree) {
    const int lower = stackchan_motion::RoundedNativePosition(
        10.1f, 460, 0, 1000);
    const int upper = stackchan_motion::RoundedNativePosition(
        10.9f, 460, 0, 1000);

    EXPECT_EQ(lower, 492);
    EXPECT_EQ(upper, 495);
    EXPECT_NE(lower, upper);
}

TEST_F(HeadSpringMotionTest, RoundsNegativeOffsetsSymmetrically) {
    const int positive = stackchan_motion::RoundedNativePosition(
        0.2f, 460, 0, 1000);
    const int negative = stackchan_motion::RoundedNativePosition(
        -0.2f, 460, 0, 1000);

    EXPECT_EQ(positive - 460, 1);
    EXPECT_EQ(460 - negative, 1);
}

TEST_F(HeadSpringMotionTest, RoundsExactHalfNativeStepAwayFromZero) {
    const int positive = stackchan_motion::RoundedNativePosition(
        0.15625f, 460, 0, 1000);
    const int negative = stackchan_motion::RoundedNativePosition(
        -0.15625f, 460, 0, 1000);

    EXPECT_EQ(positive, 461);
    EXPECT_EQ(negative, 459);
}

TEST_F(HeadSpringMotionTest, ClampsPitchToEstablishedRawSafetyRange) {
    EXPECT_EQ(
        stackchan_motion::RoundedNativePosition(-100.0f, 620, 620, 901),
        620);
    EXPECT_EQ(
        stackchan_motion::RoundedNativePosition(88.0f, 620, 620, 901),
        901);
    EXPECT_EQ(
        stackchan_motion::RoundedNativePosition(100.0f, 620, 620, 901),
        901);
}

TEST_F(HeadSpringMotionTest, NativePositionIsMonotonicAcrossSpringRange) {
    int previous = stackchan_motion::RoundedNativePosition(
        -90.0f, 460, 0, 1000);
    for (int step = -899; step <= 900; ++step) {
        const float degrees = static_cast<float>(step) / 10.0f;
        const int current = stackchan_motion::RoundedNativePosition(
            degrees, 460, 0, 1000);
        EXPECT_GE(current, previous);
        previous = current;
    }
}

TEST_F(HeadSpringMotionTest, ClearsFinalNativeWriteOnlyAfterSuccessfulWrite) {
    EXPECT_FALSE(
        stackchan_motion::ShouldClearFinalNativeWrite(true, false));
    EXPECT_TRUE(
        stackchan_motion::ShouldClearFinalNativeWrite(true, true));
    EXPECT_FALSE(
        stackchan_motion::ShouldClearFinalNativeWrite(false, true));
}

TEST_F(HeadSpringMotionTest,
       DuplicateNativeWriteSuppressionDefaultsToLegacyWrites) {
    stackchan_motion::NativeWriteSuppressor suppressor;
    const stackchan_motion::NativeWritePayload payload{1, 463, 30, 0};

    EXPECT_FALSE(suppressor.enabled());
    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    suppressor.RecordAttempt(payload, 7, 11, true);
    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
}

TEST_F(HeadSpringMotionTest,
       SuppressesOnlyExactDuplicateOfSuccessfulNativeWrite) {
    stackchan_motion::NativeWriteSuppressor suppressor;
    suppressor.SetEnabled(true);
    const stackchan_motion::NativeWritePayload payload{1, 463, 30, 0};

    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    suppressor.RecordAttempt(payload, 7, 11, true);
    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSuppress);

    EXPECT_EQ(
        suppressor.Decide({1, 464, 30, 0}, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    EXPECT_EQ(
        suppressor.Decide({1, 463, 31, 0}, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    EXPECT_EQ(
        suppressor.Decide({1, 463, 30, 1}, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
}

TEST_F(HeadSpringMotionTest,
       TerminalForceAndFailedAttemptsAreNeverSuppressed) {
    stackchan_motion::NativeWriteSuppressor suppressor;
    suppressor.SetEnabled(true);
    const stackchan_motion::NativeWritePayload payload{1, 463, 30, 0};
    suppressor.RecordAttempt(payload, 7, 11, true);

    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, true, false),
        stackchan_motion::NativeWriteDecision::kSend);
    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, false, true),
        stackchan_motion::NativeWriteDecision::kSend);

    suppressor.RecordAttempt(payload, 7, 11, false);
    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    suppressor.RecordAttempt(payload, 7, 11, true);
    EXPECT_EQ(
        suppressor.Decide(payload, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSuppress);
}

TEST_F(HeadSpringMotionTest,
       TokenEpochAndAxisBoundariesForceFirstNativeWrite) {
    stackchan_motion::NativeWriteSuppressor suppressor;
    suppressor.SetEnabled(true);
    const stackchan_motion::NativeWritePayload yaw{1, 463, 30, 0};
    const stackchan_motion::NativeWritePayload pitch{2, 463, 30, 0};
    suppressor.RecordAttempt(yaw, 7, 11, true);

    EXPECT_EQ(
        suppressor.Decide(yaw, 8, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    EXPECT_EQ(
        suppressor.Decide(yaw, 7, 12, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    EXPECT_EQ(
        suppressor.Decide(pitch, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
}

TEST_F(HeadSpringMotionTest, ModeSwitchAndAxisInvalidationClearNativeCache) {
    stackchan_motion::NativeWriteSuppressor suppressor;
    suppressor.SetEnabled(true);
    const stackchan_motion::NativeWritePayload yaw{1, 463, 30, 0};
    const stackchan_motion::NativeWritePayload pitch{2, 620, 30, 0};
    suppressor.RecordAttempt(yaw, 7, 11, true);
    suppressor.RecordAttempt(pitch, 8, 11, true);

    suppressor.InvalidateAxis(1);
    EXPECT_EQ(
        suppressor.Decide(yaw, 7, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
    EXPECT_EQ(
        suppressor.Decide(pitch, 8, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSuppress);

    suppressor.SetEnabled(false);
    suppressor.SetEnabled(true);
    EXPECT_EQ(
        suppressor.Decide(pitch, 8, 11, false, false),
        stackchan_motion::NativeWriteDecision::kSend);
}

TEST_F(HeadSpringMotionTest,
       DuplicateSuppressionMeetsSmallMoveAndRetargetAdoptionGates) {
    struct Scenario {
        std::vector<NativeFrame> frames;
        std::size_t expected_candidate_nonterminal_writes;
        int expected_terminal_ms;
    };
    const std::vector<Scenario> scenarios{
        {GenerateSmallMoveFrames(1), 4, 260},
        {GenerateSmallMoveFrames(2), 6, 280},
        {GenerateSmallRetargetFrames(1, -1), 10, 400},
        {GenerateSmallRetargetFrames(2, 1), 8, 360},
    };

    for (const auto& scenario : scenarios) {
        const auto legacy = RunNativeTrace(false, scenario.frames);
        const auto candidate = RunNativeTrace(true, scenario.frames);

        ASSERT_FALSE(scenario.frames.empty());
        EXPECT_EQ(legacy.sent, scenario.frames);
        ASSERT_FALSE(candidate.sent.empty());
        EXPECT_EQ(candidate.sent.size() - 1,
                  scenario.expected_candidate_nonterminal_writes);
        EXPECT_EQ(candidate.first_raw_ms, legacy.first_raw_ms);
        EXPECT_TRUE(candidate.sent.back().terminal);
        EXPECT_EQ(candidate.sent.back().at_ms, scenario.expected_terminal_ms);
        EXPECT_EQ(candidate.sent.back().payload.position,
                  legacy.sent.back().payload.position);
        EXPECT_LE(candidate.maximum_gap_ms, 250);
    }

    const auto one_degree = RunNativeTrace(true, scenarios[0].frames);
    const auto two_degree = RunNativeTrace(true, scenarios[1].frames);
    EXPECT_GE(
        static_cast<double>(one_degree.suppressed) /
            static_cast<double>(scenarios[0].frames.size() - 1),
        0.60);
    EXPECT_GE(
        static_cast<double>(two_degree.suppressed) /
            static_cast<double>(scenarios[1].frames.size() - 1),
        0.50);
}

TEST_F(HeadSpringMotionTest, NativeWriteTelemetryDefaultsOffAndClearsOnEnable) {
    stackchan_motion::NativeWriteTelemetryBuffer<4> telemetry;
    const stackchan_motion::NativeWriteTelemetryEvent event{
        1000, 20000, 1, 7, 463, 30, 0, 80,
        false, false, true, false, true, true, true};

    telemetry.Push(event);
    EXPECT_FALSE(telemetry.enabled());
    EXPECT_EQ(telemetry.size(), 0u);

    telemetry.SetEnabled(true);
    telemetry.Push(event);
    ASSERT_EQ(telemetry.size(), 1u);
    EXPECT_EQ(telemetry.At(0).position, 463);

    telemetry.SetEnabled(false);
    EXPECT_EQ(telemetry.size(), 1u);
    telemetry.SetEnabled(true);
    EXPECT_EQ(telemetry.size(), 0u);
    EXPECT_EQ(telemetry.overflow_count(), 0u);
}

TEST_F(HeadSpringMotionTest, NativeWriteTelemetryIsFixedCapacityAndCountsDrops) {
    stackchan_motion::NativeWriteTelemetryBuffer<2> telemetry;
    telemetry.SetEnabled(true);
    stackchan_motion::NativeWriteTelemetryEvent first{
        1000, 20000, 1, 7, 463, 30, 0, 80,
        false, false, true, false, true, true, true};
    auto second = first;
    second.mono_us = 2000;
    second.position = 464;
    auto dropped = second;
    dropped.mono_us = 3000;
    dropped.position = 465;

    telemetry.Push(first);
    telemetry.Push(second);
    telemetry.Push(dropped);

    ASSERT_EQ(telemetry.size(), 2u);
    EXPECT_EQ(telemetry.At(0).position, 463);
    EXPECT_EQ(telemetry.At(1).position, 464);
    EXPECT_EQ(telemetry.overflow_count(), 1u);
}

}  // namespace
