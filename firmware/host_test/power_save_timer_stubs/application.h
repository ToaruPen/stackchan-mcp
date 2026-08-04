#pragma once

class FakeAudioService {
public:
    bool IsWakeWordRunning() const {
        return wake_word_running;
    }

    void EnableWakeWordDetection(bool enabled) {
        wake_word_running = enabled;
    }

    bool wake_word_running = false;
};

class FakeAudioCodec {
public:
    void EnableInput(bool enabled) {
        input_enabled = enabled;
    }

    bool input_enabled = true;
};

class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }

    bool CanEnterSleepMode() const {
        return can_enter_sleep_mode;
    }

    FakeAudioService& GetAudioService() {
        return audio_service;
    }

    bool can_enter_sleep_mode = true;
    FakeAudioService audio_service;
};

class Board {
public:
    static Board& GetInstance() {
        static Board instance;
        return instance;
    }

    FakeAudioCodec* GetAudioCodec() {
        return &audio_codec;
    }

    FakeAudioCodec audio_codec;
};

inline void vTaskDelay(int) {}

#define pdMS_TO_TICKS(milliseconds) (milliseconds)
