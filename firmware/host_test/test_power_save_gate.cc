#include "boards/stackchan/power_save_gate.h"
#include "boards/stackchan/stackchan_auto_sleep_control.h"

#include <gtest/gtest.h>

namespace {

using stackchan::power::PowerSaveGate;

// The board starts PowerSaveTimer enabled before the first battery poll.
constexpr bool kTimerEnabledAtBoot = true;

TEST(PowerSaveGateTest, BootOnExternalPowerDisablesTimer) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = true;
    ASSERT_TRUE(gate.Update(/*discharging=*/false,
                            /*external_power_auto_off=*/false, &enable));
    EXPECT_FALSE(enable);
    EXPECT_FALSE(gate.Update(false, false, &enable));
}

TEST(PowerSaveGateTest, BootOnBatteryKeepsTimer) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = false;
    EXPECT_FALSE(gate.Update(/*discharging=*/true, false, &enable));
    EXPECT_TRUE(gate.applied());
}

TEST(PowerSaveGateTest, FollowsPowerSourceTransitions) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = false;
    ASSERT_TRUE(gate.Update(false, false, &enable));  // USB at boot
    EXPECT_FALSE(enable);
    ASSERT_TRUE(gate.Update(true, false, &enable));  // unplugged
    EXPECT_TRUE(enable);
    ASSERT_TRUE(gate.Update(false, false, &enable));  // plugged back in
    EXPECT_FALSE(enable);
}

TEST(PowerSaveGateTest, OptInKeepsTimerOnExternalPower) {
    PowerSaveGate gate(kTimerEnabledAtBoot);
    bool enable = false;
    EXPECT_FALSE(gate.Update(false, /*external_power_auto_off=*/true, &enable));
    EXPECT_TRUE(gate.applied());
    // Turning the opt-in off while on USB stops the timer.
    ASSERT_TRUE(gate.Update(false, false, &enable));
    EXPECT_FALSE(enable);
    // Turning it back on restarts it.
    ASSERT_TRUE(gate.Update(false, true, &enable));
    EXPECT_TRUE(enable);
}

TEST(PowerSaveGateTest, GlobalDisableOverridesBothPowerSourcesAndExternalOptIn) {
    for (bool discharging : {false, true}) {
        for (bool external_auto_off : {false, true}) {
            PowerSaveGate gate(true);
            bool enable = true;
            ASSERT_TRUE(gate.Update(discharging, external_auto_off, &enable,
                                    /*auto_sleep_enabled=*/false));
            EXPECT_FALSE(enable);
            EXPECT_FALSE(gate.Update(discharging, external_auto_off, &enable,
                                     /*auto_sleep_enabled=*/false));
        }
    }
}

struct CombinedSleepPolicy {
    bool persisted_enabled = true;
    bool auto_sleep_enabled = true;
    bool discharging = false;
    bool external_auto_off = false;
    PowerSaveGate gate{false};
    int timer_starts = 0;

    stackchan_power::AutoSleepControlHooks Hooks() {
        return {
            .context = this,
            .read = [](void* context) {
                auto* self = static_cast<CombinedSleepPolicy*>(context);
                return stackchan_power::AutoSleepPersistenceRead{
                    .status = stackchan_power::AutoSleepPersistenceStatus::kOk,
                    .enabled = self->persisted_enabled,
                };
            },
            .write = [](void* context, bool enabled) {
                static_cast<CombinedSleepPolicy*>(context)->persisted_enabled = enabled;
                return stackchan_power::AutoSleepPersistenceWrite{.ok = true};
            },
            .set_timer_enabled = [](void* context, bool enabled) {
                auto* self = static_cast<CombinedSleepPolicy*>(context);
                self->auto_sleep_enabled = enabled;
                self->Apply();
            },
            .wake_up = [](void*) {},
        };
    }

    void Apply() {
        bool enable = false;
        if (gate.Update(discharging, external_auto_off, &enable,
                        auto_sleep_enabled) && enable) {
            ++timer_starts;
        }
    }
};

TEST(PowerSaveGateTest, EnablingGlobalSleepOnUsbRequiresExternalPowerOptIn) {
    CombinedSleepPolicy policy;
    ASSERT_TRUE(stackchan_power::SetAutoSleepPolicy(policy.Hooks(), true).ok);
    EXPECT_FALSE(policy.gate.applied());
    EXPECT_EQ(policy.timer_starts, 0);

    policy.external_auto_off = true;
    policy.Apply();
    EXPECT_TRUE(policy.gate.applied());
    EXPECT_EQ(policy.timer_starts, 1);
}

TEST(PowerSaveGateTest, DisabledGlobalSleepSurvivesPowerChangesAndOptIn) {
    CombinedSleepPolicy policy;
    ASSERT_TRUE(stackchan_power::SetAutoSleepPolicy(policy.Hooks(), false).ok);
    policy.discharging = true;
    policy.Apply();
    EXPECT_FALSE(policy.gate.applied());
    policy.discharging = false;
    policy.external_auto_off = true;
    policy.Apply();
    EXPECT_FALSE(policy.gate.applied());
    EXPECT_EQ(policy.timer_starts, 0);
}

TEST(PowerSaveGateTest, ReenablingOnBatteryRestartsCountdown) {
    CombinedSleepPolicy policy;
    policy.discharging = true;
    policy.Apply();
    EXPECT_EQ(policy.timer_starts, 1);
    ASSERT_TRUE(stackchan_power::SetAutoSleepPolicy(policy.Hooks(), true).ok);
    EXPECT_TRUE(policy.gate.applied());
    EXPECT_EQ(policy.timer_starts, 2);
    ASSERT_TRUE(stackchan_power::SetAutoSleepPolicy(policy.Hooks(), false).ok);
    EXPECT_FALSE(policy.gate.applied());
    ASSERT_TRUE(stackchan_power::SetAutoSleepPolicy(policy.Hooks(), true).ok);
    EXPECT_TRUE(policy.gate.applied());
    EXPECT_EQ(policy.timer_starts, 3);
}

}  // namespace
