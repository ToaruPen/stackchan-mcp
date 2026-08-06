#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "stackchan_auto_sleep_control.h"

namespace {

using stackchan_power::AutoSleepControlHooks;
using stackchan_power::AutoSleepPersistenceRead;
using stackchan_power::AutoSleepPersistenceStatus;

struct FakeAutoSleepPlatform {
    AutoSleepPersistenceStatus read_status =
        AutoSleepPersistenceStatus::kOk;
    bool persisted_enabled = true;
    bool write_ok = true;
    std::string error = "nvs I/O failure";
    std::vector<std::string> events;

    AutoSleepControlHooks Hooks() {
        return AutoSleepControlHooks{
            .context = this,
            .read = [](void* context) {
                auto* self = static_cast<FakeAutoSleepPlatform*>(context);
                self->events.push_back("read");
                return AutoSleepPersistenceRead{
                    .status = self->read_status,
                    .enabled = self->persisted_enabled,
                    .error = self->read_status ==
                                     AutoSleepPersistenceStatus::kError
                                 ? self->error
                                 : std::string(),
                };
            },
            .write = [](void* context, bool enabled) {
                auto* self = static_cast<FakeAutoSleepPlatform*>(context);
                self->events.push_back(enabled ? "write:true" : "write:false");
                if (!self->write_ok) {
                    return stackchan_power::AutoSleepPersistenceWrite{
                        .ok = false,
                        .error = self->error,
                    };
                }
                self->persisted_enabled = enabled;
                return stackchan_power::AutoSleepPersistenceWrite{
                    .ok = true,
                };
            },
            .set_timer_enabled = [](void* context, bool enabled) {
                auto* self = static_cast<FakeAutoSleepPlatform*>(context);
                self->events.push_back(enabled ? "timer:true" : "timer:false");
            },
            .wake_up = [](void* context) {
                static_cast<FakeAutoSleepPlatform*>(context)
                    ->events.push_back("wake");
            },
        };
    }
};

TEST(StackChanAutoSleepControlTest, UsesExactStackChanThresholds) {
    EXPECT_EQ(stackchan_power::kAutoSleepAfterSeconds, 60);
    EXPECT_EQ(stackchan_power::kAutoShutdownAfterSeconds, 300);
}

TEST(StackChanAutoSleepControlTest, GetterTreatsMissingKeyAsEnabledWithoutApplying) {
    FakeAutoSleepPlatform platform;
    platform.read_status = AutoSleepPersistenceStatus::kMissing;

    const auto result = stackchan_power::GetAutoSleepPolicy(platform.Hooks());

    ASSERT_TRUE(result.ok);
    EXPECT_TRUE(result.enabled);
    EXPECT_EQ(platform.events, std::vector<std::string>({"read"}));
}

TEST(StackChanAutoSleepControlTest, GetterReportsIoErrorWithoutApplying) {
    FakeAutoSleepPlatform platform;
    platform.read_status = AutoSleepPersistenceStatus::kError;

    const auto result = stackchan_power::GetAutoSleepPolicy(platform.Hooks());

    EXPECT_FALSE(result.ok);
    EXPECT_EQ(result.error, "nvs I/O failure");
    EXPECT_EQ(platform.events, std::vector<std::string>({"read"}));
}

TEST(StackChanAutoSleepControlTest, DisablePersistsBeforeStoppingAndWaking) {
    FakeAutoSleepPlatform platform;
    platform.persisted_enabled = true;

    const auto result =
        stackchan_power::SetAutoSleepPolicy(platform.Hooks(), false);

    ASSERT_TRUE(result.ok);
    EXPECT_TRUE(result.previous_enabled);
    EXPECT_FALSE(result.enabled);
    EXPECT_EQ(platform.events,
              std::vector<std::string>(
                  {"read", "write:false", "timer:false", "wake"}));
}

TEST(StackChanAutoSleepControlTest, SameFalseStillSynchronizesAndWakes) {
    FakeAutoSleepPlatform platform;
    platform.persisted_enabled = false;

    const auto result =
        stackchan_power::SetAutoSleepPolicy(platform.Hooks(), false);

    ASSERT_TRUE(result.ok);
    EXPECT_FALSE(result.previous_enabled);
    EXPECT_EQ(platform.events,
              std::vector<std::string>({"read", "timer:false", "wake"}));
}

TEST(StackChanAutoSleepControlTest, EnableAlwaysRestartsCountdownFromZero) {
    FakeAutoSleepPlatform platform;
    platform.persisted_enabled = false;

    const auto result =
        stackchan_power::SetAutoSleepPolicy(platform.Hooks(), true);

    ASSERT_TRUE(result.ok);
    EXPECT_FALSE(result.previous_enabled);
    EXPECT_TRUE(result.enabled);
    EXPECT_EQ(platform.events,
              std::vector<std::string>(
                  {"read", "write:true", "timer:false", "timer:true"}));
}

TEST(StackChanAutoSleepControlTest, SameTrueStillRestartsCountdownFromZero) {
    FakeAutoSleepPlatform platform;
    platform.persisted_enabled = true;

    const auto result =
        stackchan_power::SetAutoSleepPolicy(platform.Hooks(), true);

    ASSERT_TRUE(result.ok);
    EXPECT_TRUE(result.previous_enabled);
    EXPECT_EQ(platform.events,
              std::vector<std::string>({"read", "timer:false", "timer:true"}));
}

TEST(StackChanAutoSleepControlTest, ReadFailureNeverWritesOrApplies) {
    FakeAutoSleepPlatform platform;
    platform.read_status = AutoSleepPersistenceStatus::kError;

    const auto result =
        stackchan_power::SetAutoSleepPolicy(platform.Hooks(), false);

    EXPECT_FALSE(result.ok);
    EXPECT_EQ(platform.events, std::vector<std::string>({"read"}));
}

TEST(StackChanAutoSleepControlTest, WriteFailureNeverAppliesRuntimeState) {
    FakeAutoSleepPlatform platform;
    platform.persisted_enabled = true;
    platform.write_ok = false;

    const auto result =
        stackchan_power::SetAutoSleepPolicy(platform.Hooks(), false);

    EXPECT_FALSE(result.ok);
    EXPECT_EQ(platform.events,
              std::vector<std::string>({"read", "write:false"}));
}

}  // namespace
