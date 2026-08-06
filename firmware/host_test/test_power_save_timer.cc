#include <gtest/gtest.h>

#include "application.h"
#include "esp_timer.h"
#include "power_save_timer.h"
#include "settings.h"
#include "stackchan_auto_sleep_control.h"

namespace {

class PowerSaveTimerTest : public testing::Test {
protected:
    void SetUp() override {
        Settings::sleep_mode = true;
        Application::GetInstance().can_enter_sleep_mode = true;
    }

    FakeEspTimer* LastTimer() {
        EXPECT_FALSE(fake_esp_timer::timers.empty());
        return fake_esp_timer::timers.back();
    }
};

TEST_F(PowerSaveTimerTest, ExposesInitialEffectiveStateWithoutMutation) {
    PowerSaveTimer timer(-1, 60, 300);

    EXPECT_FALSE(timer.IsEnabled());
    EXPECT_FALSE(timer.IsInSleepMode());
}

TEST_F(PowerSaveTimerTest, EntersSleepAt60AndRequestsShutdownAt300) {
    int entered_sleep = 0;
    int shutdown_requests = 0;
    PowerSaveTimer timer(-1, 60, 300);
    timer.OnEnterSleepMode([&]() { ++entered_sleep; });
    timer.OnShutdownRequest([&]() { ++shutdown_requests; });
    timer.SetEnabled(true);

    fake_esp_timer::Fire(LastTimer(), 59);
    EXPECT_FALSE(timer.IsInSleepMode());
    EXPECT_EQ(entered_sleep, 0);

    fake_esp_timer::Fire(LastTimer());
    EXPECT_TRUE(timer.IsInSleepMode());
    EXPECT_EQ(entered_sleep, 1);

    fake_esp_timer::Fire(LastTimer(), 239);
    EXPECT_EQ(shutdown_requests, 0);
    fake_esp_timer::Fire(LastTimer());
    EXPECT_EQ(shutdown_requests, 1);
}

TEST_F(PowerSaveTimerTest, DisableWakesDisplayAndCancelsShutdown) {
    int exited_sleep = 0;
    int shutdown_requests = 0;
    PowerSaveTimer timer(-1, 60, 300);
    timer.OnExitSleepMode([&]() { ++exited_sleep; });
    timer.OnShutdownRequest([&]() { ++shutdown_requests; });
    timer.SetEnabled(true);
    fake_esp_timer::Fire(LastTimer(), 60);

    timer.SetEnabled(false);

    EXPECT_FALSE(timer.IsEnabled());
    EXPECT_FALSE(timer.IsInSleepMode());
    EXPECT_EQ(exited_sleep, 1);
    fake_esp_timer::Fire(LastTimer(), 300);
    EXPECT_EQ(shutdown_requests, 0);
}

TEST_F(PowerSaveTimerTest, PersistedFalseBlocksBatteryHookReenable) {
    PowerSaveTimer timer(-1, 60, 300);
    Settings::sleep_mode = false;

    timer.SetEnabled(true);

    EXPECT_FALSE(timer.IsEnabled());
}

TEST_F(PowerSaveTimerTest, PersistedTrueAllowsBatteryHookReenable) {
    PowerSaveTimer timer(-1, 60, 300);
    timer.SetEnabled(true);
    timer.SetEnabled(false);
    Settings::sleep_mode = true;

    timer.SetEnabled(true);

    EXPECT_TRUE(timer.IsEnabled());
}

TEST_F(PowerSaveTimerTest, SameTruePolicyRestartsAProgressedCountdown) {
    struct Platform {
        PowerSaveTimer* timer;
        bool persisted_enabled = true;

        stackchan_power::AutoSleepControlHooks Hooks() {
            return {
                .context = this,
                .read = [](void* context) {
                    return stackchan_power::AutoSleepPersistenceRead{
                        .status =
                            stackchan_power::AutoSleepPersistenceStatus::kOk,
                        .enabled = static_cast<Platform*>(context)
                                       ->persisted_enabled,
                    };
                },
                .write = [](void* context, bool enabled) {
                    static_cast<Platform*>(context)->persisted_enabled =
                        enabled;
                    Settings::sleep_mode = enabled;
                    return stackchan_power::AutoSleepPersistenceWrite{
                        .ok = true,
                    };
                },
                .set_timer_enabled = [](void* context, bool enabled) {
                    static_cast<Platform*>(context)->timer->SetEnabled(enabled);
                },
                .wake_up = [](void* context) {
                    static_cast<Platform*>(context)->timer->WakeUp();
                },
            };
        }
    };

    PowerSaveTimer timer(-1, 60, 300);
    Platform platform{.timer = &timer};
    timer.SetEnabled(true);
    fake_esp_timer::Fire(LastTimer(), 59);

    const auto result =
        stackchan_power::SetAutoSleepPolicy(platform.Hooks(), true);
    fake_esp_timer::Fire(LastTimer());

    ASSERT_TRUE(result.ok);
    EXPECT_TRUE(result.previous_enabled);
    EXPECT_FALSE(timer.IsInSleepMode());
    fake_esp_timer::Fire(LastTimer(), 58);
    EXPECT_FALSE(timer.IsInSleepMode());
    fake_esp_timer::Fire(LastTimer());
    EXPECT_TRUE(timer.IsInSleepMode());
}

}  // namespace
