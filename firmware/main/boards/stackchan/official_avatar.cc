/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 * SPDX-License-Identifier: MIT
 *
 * Adapted from m5stack/StackChan, commit
 * eff0d6b5109663faec8f1d98848b4b0e5246bbfc, default.cpp, eyes.cpp and mouth.cpp.
 * See official_avatar.LICENSE for the upstream license.
 */
#include "official_avatar.h"

#include <algorithm>
#include <array>

namespace stackchan {
namespace {

struct Expression {
    int eye_size;
    int eye_weight;
    int eye_rotation;
    int gaze_x;
    int gaze_y;
};

// The first six slots match the existing set_avatar face names.
constexpr std::array<Expression, 8> kExpressions{{
    {20, 100, 0, 0, 0},       // idle: Neutral
    {20, 72, 1550, 0, 0},     // happy: Happy
    {20, 75, 0, 12, -8},      // thinking: Doubt, looking up
    {20, 70, -400, 0, 0},     // sad: Sad
    {32, 100, 0, 0, 0},       // surprised
    {20, 72, 1550, 0, 4},     // embarrassed: Happy, looking down
    {26, 100, 0, 0, -3},      // listening
    {20, 72, 1550, 0, 0},     // speaking: Happy, mouth animated separately
}};
constexpr std::array<int, 3> kEyeOpenness{100, 50, 0};
constexpr std::array<int, 5> kMouthWeight{0, 45, 100, 30, 70};

lv_obj_t* CreatePart(lv_obj_t* parent, lv_color_t color) {
    auto* part = lv_obj_create(parent);
    lv_obj_remove_style_all(part);
    lv_obj_set_style_bg_color(part, color, 0);
    lv_obj_set_style_bg_opa(part, LV_OPA_COVER, 0);
    lv_obj_remove_flag(part, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(part, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_align(part, LV_ALIGN_CENTER);
    return part;
}

}  // namespace

bool OfficialAvatar::EnsureCreated(lv_obj_t* parent) {
    if (root_ != nullptr) return true;
    if (parent == nullptr) return false;

    root_ = CreatePart(parent, lv_color_black());
    lv_obj_set_size(root_, 320, 240);
    for (auto& eye : eyes_) {
        eye.container = CreatePart(root_, lv_color_black());
        lv_obj_set_style_bg_opa(eye.container, LV_OPA_TRANSP, 0);
        lv_obj_set_size(eye.container, 32, 32);
        lv_obj_set_style_transform_pivot_x(eye.container, 16, 0);
        lv_obj_set_style_transform_pivot_y(eye.container, 16, 0);
        eye.pupil = CreatePart(eye.container, lv_color_white());
        lv_obj_set_style_radius(eye.pupil, LV_RADIUS_CIRCLE, 0);
        eye.eyelid = CreatePart(eye.container, lv_color_black());
    }
    mouth_ = CreatePart(root_, lv_color_white());
    lv_obj_set_pos(mouth_, 0, 26);
    Render(0, 0, 0);
    return true;
}

void OfficialAvatar::Render(int face_index, int eyes_index, int mouth_index, int mouth_opening) {
    if (root_ == nullptr) return;
    const auto& expression = kExpressions[std::clamp(face_index, 0, 7)];
    const int openness = kEyeOpenness[std::clamp(eyes_index, 0, 2)];
    const int weight = expression.eye_weight * openness / 100;
    for (int i = 0; i < 2; ++i) {
        auto& eye = eyes_[i];
        lv_obj_set_pos(eye.container, (i == 0 ? -70 : 70) + expression.gaze_x,
                       -16 + expression.gaze_y);
        lv_obj_set_style_transform_rotation(eye.container,
                                            i == 0 ? expression.eye_rotation : -expression.eye_rotation, 0);
        lv_obj_set_size(eye.pupil, expression.eye_size, expression.eye_size);
        lv_obj_set_size(eye.eyelid, expression.eye_size, expression.eye_size);
        lv_obj_set_y(eye.eyelid, -weight * expression.eye_size / 100);
    }

    const int mouth_weight = mouth_opening < 0
        ? kMouthWeight[std::clamp(mouth_index, 0, 4)] : std::clamp(mouth_opening, 0, 100);
    lv_obj_set_size(mouth_, 90 - 30 * mouth_weight / 100, 6 + 44 * mouth_weight / 100);
    lv_obj_set_style_radius(mouth_, 16 * mouth_weight / 100, 0);
}

void OfficialAvatar::SetHidden(bool hidden) {
    if (root_ == nullptr) return;
    if (hidden) {
        lv_obj_add_flag(root_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_remove_flag(root_, LV_OBJ_FLAG_HIDDEN);
    }
}

}  // namespace stackchan
