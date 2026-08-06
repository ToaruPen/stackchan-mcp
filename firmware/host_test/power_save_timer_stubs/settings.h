#pragma once

#include <string>

class Settings {
public:
    inline static bool sleep_mode = true;

    Settings(const std::string&, bool) {}

    bool GetBool(const std::string&, bool) const {
        return sleep_mode;
    }
};
