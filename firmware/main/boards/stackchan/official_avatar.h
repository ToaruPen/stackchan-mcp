/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <lvgl.h>

namespace stackchan {

// LVGL calls require the display lock. Geometry follows M5Stack's DefaultAvatar.
class OfficialAvatar {
public:
    bool EnsureCreated(lv_obj_t* parent);
    void Render(int face_index, int eyes_index, int mouth_index, int mouth_opening = -1);
    void SetHidden(bool hidden);
    lv_obj_t* root() const { return root_; }

private:
    struct Eye {
        lv_obj_t* container = nullptr;
        lv_obj_t* pupil = nullptr;
        lv_obj_t* eyelid = nullptr;
    };

    lv_obj_t* root_ = nullptr;
    Eye eyes_[2];
    lv_obj_t* mouth_ = nullptr;
};

}  // namespace stackchan
