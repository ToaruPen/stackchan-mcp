#include "official_avatar.h"
#include "audio/playback_envelope.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace {

constexpr int kWidth = 320;
constexpr int kHeight = 240;
alignas(4) std::array<uint16_t, kWidth * kHeight> buffer{};
std::array<uint16_t, kWidth * kHeight> frame{};

void Require(bool condition, const char* message) {
    if (!condition) {
        std::fprintf(stderr, "%s\n", message);
        std::exit(1);
    }
}

void Flush(lv_display_t* display, const lv_area_t* area, uint8_t* pixels) {
    const auto* source = reinterpret_cast<uint16_t*>(pixels);
    for (int y = area->y1; y <= area->y2; ++y) {
        for (int x = area->x1; x <= area->x2; ++x) {
            frame[y * kWidth + x] = *source++;
        }
    }
    lv_display_flush_ready(display);
}

std::vector<uint16_t> Region(int x, int y, int width, int height) {
    std::vector<uint16_t> result;
    for (int row = y; row < y + height; ++row) {
        for (int col = x; col < x + width; ++col) {
            result.push_back(frame[row * kWidth + col]);
        }
    }
    return result;
}

int LitPixels(const std::vector<uint16_t>& region) {
    int count = 0;
    for (auto pixel : region) count += pixel != 0;
    return count;
}

void Save(const std::filesystem::path& directory, const char* name) {
    if (directory.empty()) return;
    std::filesystem::create_directories(directory);
    std::ofstream output(directory / (std::string(name) + ".ppm"), std::ios::binary);
    output << "P6\n320 240\n255\n";
    for (uint16_t pixel : frame) {
        const char rgb[] = {
            static_cast<char>(((pixel >> 11) & 31) * 255 / 31),
            static_cast<char>(((pixel >> 5) & 63) * 255 / 63),
            static_cast<char>((pixel & 31) * 255 / 31),
        };
        output.write(rgb, sizeof(rgb));
    }
}

}  // namespace

int main(int argc, char** argv) {
    const std::filesystem::path output = argc > 1 ? argv[1] : "";
    lv_init();
    auto* display = lv_display_create(kWidth, kHeight);
    lv_display_set_color_format(display, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(display, buffer.data(), nullptr, sizeof(buffer), LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_flush_cb(display, Flush);
    lv_obj_set_style_bg_color(lv_screen_active(), lv_color_hex(0x102030), 0);

    stackchan::OfficialAvatar avatar;
    Require(!avatar.EnsureCreated(nullptr), "Avatar must wait for the screen");
    Require(avatar.EnsureCreated(lv_screen_active()), "Avatar must initialize without image assets");
    auto* root = avatar.root();
    Require(avatar.EnsureCreated(lv_screen_active()) && avatar.root() == root,
            "Repeated initialization must preserve the avatar object");

    auto render = [&](int face, int eyes, int mouth, const char* name) {
        avatar.Render(face, eyes, mouth);
        lv_obj_update_layout(avatar.root());
        lv_refr_now(display);
        Save(output, name);
    };
    render(0, 0, 0, "idle");
    const auto idle_eyes = Region(60, 65, 200, 55);
    const auto closed_mouth = Region(100, 120, 120, 60);
    Require(LitPixels(idle_eyes) > 300, "Idle must display both eyes");
    Require(LitPixels(closed_mouth) > 300, "Idle must display the mouth");

    render(6, 0, 0, "listening");
    Require(Region(60, 65, 200, 55) != idle_eyes, "Listening must look different from idle");
    render(2, 0, 0, "thinking");
    Require(Region(60, 65, 200, 55) != idle_eyes, "Thinking must look different from idle");
    render(7, 0, 2, "speaking");
    const auto speaking_eyes = Region(60, 65, 200, 55);
    const auto open_mouth = Region(100, 130, 120, 65);
    render(7, 2, 2, "speaking_blink");
    Require(LitPixels(Region(60, 65, 200, 55)) == 0, "Blink must close both eyes");
    Require(Region(100, 130, 120, 65) == open_mouth, "Blink must preserve the speaking mouth");
    render(7, 0, 0, "speaking_closed");
    Require(Region(60, 65, 200, 55) == speaking_eyes, "Mouth movement must preserve the expression");
    Require(Region(100, 130, 120, 65) != open_mouth, "Speech must move the mouth");

    stackchan::audio::PlaybackEnvelope envelope;
    std::array<int16_t, 640> pcm{};
    const std::array<int16_t, 5> amplitudes{0, 1200, 3000, 6000, 0};
    std::array<int, 5> mouth_areas{};
    for (int i = 0; i < 75; ++i) {
        const int phase = i / 15;
        for (size_t j = 0; j < pcm.size(); ++j) {
            pcm[j] = (j % 2 ? -1 : 1) * amplitudes[phase];
        }
        const auto level = stackchan::audio::MeasurePlaybackLevel(pcm.data(), pcm.size());
        const uint32_t now = i * stackchan::audio::PlaybackEnvelope::kTickMs;
        const int opening = envelope.Advance(level, now, now);
        avatar.Render(7, 0, 0, opening);
        lv_obj_update_layout(avatar.root());
        lv_refr_now(display);
        Require(Region(60, 65, 200, 55) == speaking_eyes,
                "PCM-driven lip movement must preserve the eyes");
        char name[32];
        std::snprintf(name, sizeof(name), "lip_pcm_%03d", i);
        Save(output, name);
        if (i % 15 == 14) mouth_areas[phase] = LitPixels(Region(100, 120, 120, 65));
    }
    Require(mouth_areas[0] < mouth_areas[1] && mouth_areas[1] < mouth_areas[2] &&
            mouth_areas[2] < mouth_areas[3], "Louder PCM must visibly open the mouth further");
    Require(mouth_areas[4] == mouth_areas[0], "Silent PCM must return to the closed mouth");

    render(3, 0, 0, "sad");
    render(4, 0, 0, "surprised");
    avatar.SetHidden(true);
    lv_refr_now(display);
    Require(frame[120 * kWidth + 160] != 0, "Hidden avatar must reveal the system UI");
    avatar.SetHidden(false);
    render(0, 0, 0, "idle_restored");
    Require(Region(60, 65, 200, 55) == idle_eyes, "Resume must restore the face");

    lv_display_delete(display);
    lv_deinit();
    std::puts("Official avatar render checks passed (LVGL framebuffer)");
}
