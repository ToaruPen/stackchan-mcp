#include <gtest/gtest.h>
#include "boards/stackchan/avatar_activity_policy.h"

using stackchan::AvatarActivityPolicy;

TEST(AvatarActivityPolicy, StartupAndSystemScreensRemainVisible) {
    AvatarActivityPolicy policy;
    EXPECT_FALSE(policy.visible());
    for (auto state : {kDeviceStateStarting, kDeviceStateWifiConfiguring,
                       kDeviceStateActivating, kDeviceStateUpgrading,
                       kDeviceStateAudioTesting, kDeviceStateFatalError}) {
        policy.OnDeviceState(state);
        EXPECT_FALSE(policy.visible());
        EXPECT_FALSE(policy.OnActivity(AvatarActivity::Thinking, 100));
    }
    policy.OnDeviceState(kDeviceStateIdle);
    EXPECT_TRUE(policy.visible());
    EXPECT_STREQ(policy.face(), "idle");
}

TEST(AvatarActivityPolicy, ConversationShowsListeningThinkingSpeakingThenIdle) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateListening);
    EXPECT_TRUE(policy.visible());
    EXPECT_STREQ(policy.face(), "listening");
    policy.OnDeviceState(kDeviceStateIdle);
    EXPECT_STREQ(policy.face(), "idle");
    EXPECT_TRUE(policy.OnActivity(AvatarActivity::Thinking, 10));
    EXPECT_STREQ(policy.face(), "thinking");
    policy.OnDeviceState(kDeviceStateSpeaking);
    EXPECT_STREQ(policy.face(), "speaking");
    policy.OnDeviceState(kDeviceStateIdle);
    EXPECT_STREQ(policy.face(), "idle");
}

TEST(AvatarActivityPolicy, StoppingCaptureDoesNotPretendToThink) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateListening);
    policy.OnDeviceState(kDeviceStateIdle);
    EXPECT_EQ(policy.activity(), AvatarActivity::Idle);
    EXPECT_FALSE(policy.ExpireThinking(60000));
}

TEST(AvatarActivityPolicy, LateCompletionCannotOverrideNewListeningOrSpeaking) {
    AvatarActivityPolicy policy;
    for (auto state : {kDeviceStateListening, kDeviceStateSpeaking}) {
        policy.OnDeviceState(state);
        auto activity = policy.activity();
        EXPECT_FALSE(policy.OnActivity(AvatarActivity::Idle, 100));
        EXPECT_FALSE(policy.OnActivity(AvatarActivity::Thinking, 100));
        EXPECT_EQ(policy.activity(), activity);
    }
}

TEST(AvatarActivityPolicy, ThinkingExpiresAfterThirtySeconds) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateIdle);
    ASSERT_TRUE(policy.OnActivity(AvatarActivity::Thinking, 10));
    EXPECT_FALSE(policy.ExpireThinking(30009));
    EXPECT_TRUE(policy.ExpireThinking(30010));
    EXPECT_STREQ(policy.face(), "idle");
    EXPECT_FALSE(policy.ExpireThinking(60010));
}

TEST(AvatarActivityPolicy, ExplicitOffSurvivesActivityAndSystemTransitions) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateIdle);
    policy.SetEnabled(false);
    EXPECT_TRUE(policy.OnActivity(AvatarActivity::Thinking, 10));
    EXPECT_FALSE(policy.visible());
    policy.OnDeviceState(kDeviceStateSpeaking);
    EXPECT_FALSE(policy.visible());
    policy.OnDeviceState(kDeviceStateUpgrading);
    policy.OnDeviceState(kDeviceStateIdle);
    EXPECT_FALSE(policy.visible());
    policy.SetEnabled(true);
    EXPECT_TRUE(policy.visible());
}

TEST(AvatarActivityPolicy, ExplicitShowCannotCoverSystemScreen) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateWifiConfiguring);
    policy.SetEnabled(false);
    policy.SetEnabled(true);
    EXPECT_FALSE(policy.visible());
    policy.OnDeviceState(kDeviceStateIdle);
    EXPECT_TRUE(policy.visible());
}

TEST(AvatarActivityPolicy, SameIdleResetClearsThinkingAfterNetworkError) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateIdle);
    policy.OnActivity(AvatarActivity::Thinking, 10);
    policy.OnDeviceState(kDeviceStateIdle);
    EXPECT_STREQ(policy.face(), "idle");
    EXPECT_FALSE(policy.ExpireThinking(40000));
}

TEST(AvatarActivityPolicy, InterruptionCancelsOldThinkingTimeout) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateIdle);
    policy.OnActivity(AvatarActivity::Thinking, 10);
    policy.OnDeviceState(kDeviceStateListening);
    EXPECT_FALSE(policy.ExpireThinking(40000));
    EXPECT_STREQ(policy.face(), "listening");
}

TEST(AvatarActivityPolicy, ExplicitIdleResumesCurrentActivityWithoutSuppressingOtherEmotions) {
    AvatarActivityPolicy policy;
    policy.OnDeviceState(kDeviceStateIdle);
    policy.OnActivity(AvatarActivity::Thinking, 0);
    EXPECT_STREQ(policy.ResolveExplicitFace("idle"), "thinking");
    EXPECT_STREQ(policy.ResolveExplicitFace("happy"), "happy");
    policy.OnDeviceState(kDeviceStateSpeaking);
    EXPECT_STREQ(policy.ResolveExplicitFace("idle"), "speaking");
    EXPECT_STREQ(policy.ResolveExplicitFace("sad"), "sad");
    policy.OnDeviceState(kDeviceStateListening);
    EXPECT_STREQ(policy.ResolveExplicitFace("idle"), "listening");
}
