#include "audio/playback_envelope.h"
#include "audio/playback_progress.h"

#include <gtest/gtest.h>
#include <array>
#include <limits>

using stackchan::audio::MeasurePlaybackLevel;
using stackchan::audio::PlaybackEnvelope;
using stackchan::audio::PlaybackProgress;

namespace {
uint8_t Level(int16_t amplitude) {
    std::array<int16_t, 640> pcm;
    pcm.fill(amplitude);
    return MeasurePlaybackLevel(pcm.data(), pcm.size());
}
}  // namespace

TEST(PlaybackLevel, SilenceAndNoiseDoNotOpenMouth) {
    EXPECT_EQ(MeasurePlaybackLevel(nullptr, 0), 0);
    EXPECT_EQ(Level(0), 0);
    EXPECT_EQ(Level(80), 0);
    EXPECT_EQ(Level(-80), 0);
}

TEST(PlaybackLevel, QuietNormalAndLoudSpeechHaveIncreasingOpenings) {
    EXPECT_GT(Level(1200), 0);
    EXPECT_GT(Level(3000), Level(1200));
    EXPECT_GT(Level(6000), Level(3000));
    EXPECT_EQ(Level(6000), 100);
    EXPECT_EQ(Level(3000), Level(-3000));
}

TEST(PlaybackLevel, MinimumSignedSampleDoesNotOverflow) {
    EXPECT_EQ(Level(std::numeric_limits<int16_t>::min()), 100);
    EXPECT_EQ(Level(std::numeric_limits<int16_t>::max()), 100);
}

TEST(PlaybackLevel, UsesRmsRatherThanSinglePeak) {
    std::array<int16_t, 100> pcm{};
    pcm[0] = 6000;
    const auto level = MeasurePlaybackLevel(pcm.data(), pcm.size());
    EXPECT_LT(level, Level(1200));
    EXPECT_GT(level, 0);
}

TEST(PlaybackLevel, MutedOutputIsSilent) {
    const int16_t pcm[] = {3000, -3000};
    EXPECT_EQ(MeasurePlaybackLevel(pcm, 2, true), 0);
}

TEST(PlaybackEnvelope, SilenceNeverAnimates) {
    PlaybackEnvelope envelope;
    for (uint32_t now = 0; now < 1000; now += 40) EXPECT_EQ(envelope.Advance(0, now, now), 0);
}

TEST(PlaybackEnvelope, AttackIsFasterThanReleaseAndSilenceCloses) {
    PlaybackEnvelope envelope;
    const int first = envelope.Advance(100, 1000, 1000);
    EXPECT_GT(first, 50);
    const int released = envelope.Advance(0, 1040, 1040);
    EXPECT_GT(released, 0);
    EXPECT_LT(first - released, first);
    for (uint32_t now = 1080; now <= 1240; now += 40) envelope.Advance(0, now, now);
    EXPECT_EQ(envelope.opening(), 0);
}

TEST(PlaybackEnvelope, MissingAudioClosesWithinStaleBound) {
    PlaybackEnvelope envelope;
    envelope.Advance(100, 1000, 1000);
    EXPECT_GT(envelope.Advance(100, 1000, 1179), 0);
    EXPECT_EQ(envelope.Advance(100, 1000, 1180), 0);
}

TEST(PlaybackEnvelope, TimestampWrapPreservesFreshnessAndTimeout) {
    PlaybackEnvelope envelope;
    constexpr uint32_t sample = UINT32_MAX - 80;
    EXPECT_GT(envelope.Advance(100, sample, 10), 0);
    EXPECT_EQ(envelope.Advance(100, sample, 100), 0);
}

TEST(PlaybackEnvelope, StopResetClosesImmediatelyAndNextSilenceStaysClosed) {
    PlaybackEnvelope envelope;
    envelope.Advance(100, 1000, 1000);
    EXPECT_EQ(envelope.Reset(), 0);
    EXPECT_EQ(envelope.Advance(0, 1100, 1100), 0);
}

TEST(PlaybackProgress, EmptyQueuesCannotFinishWhileDecoderOwnsLastPacket) {
    PlaybackProgress progress;
    progress.decoding = true;
    EXPECT_FALSE(progress.IsIdle(true, true, 1000));
    progress.decoding = false;
    EXPECT_FALSE(progress.IsIdle(true, false, 1000));
    EXPECT_FALSE(progress.IsIdle(false, true, 1000));
}

TEST(PlaybackProgress, PoppedOutputAndSubmittedTailDelayCompletion) {
    PlaybackProgress progress;
    progress.outputting = true;
    progress.Submit(960, 16000, 1000);
    EXPECT_FALSE(progress.IsIdle(true, true, 1000));
    progress.outputting = false;
    EXPECT_FALSE(progress.IsIdle(true, true, 1059));
    EXPECT_TRUE(progress.IsIdle(true, true, 1060));
}

TEST(PlaybackProgress, BackToBackBufferedWritesAccumulateTheirDuration) {
    PlaybackProgress progress;
    progress.Submit(960, 16000, 1000);
    progress.Submit(960, 16000, 1010);
    EXPECT_FALSE(progress.IsIdle(true, true, 1119));
    EXPECT_TRUE(progress.IsIdle(true, true, 1120));
}

TEST(PlaybackProgress, GapStartsDeadlineAtNewOutputNotPreviousUtterance) {
    PlaybackProgress progress;
    progress.Submit(960, 16000, 1000);
    progress.Submit(960, 16000, 2000);
    EXPECT_FALSE(progress.IsIdle(true, true, 2059));
    EXPECT_TRUE(progress.IsIdle(true, true, 2060));
}

TEST(PlaybackProgress, QueueClearDoesNotEraseInFlightOutput) {
    PlaybackProgress progress;
    progress.outputting = true;
    progress.Submit(960, 16000, 1000);
    EXPECT_FALSE(progress.IsIdle(true, true, 2000));
    progress.outputting = false;
    EXPECT_TRUE(progress.IsIdle(true, true, 2000));
}
