cmake_minimum_required(VERSION 3.16)
project(stackchan_avatar_preview LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(LV_BUILD_CONF_PATH "${CMAKE_CURRENT_SOURCE_DIR}/lv_conf.h" CACHE PATH "" FORCE)
set(CONFIG_LV_BUILD_DEMOS OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(CONFIG_LV_USE_THORVG_INTERNAL OFF CACHE BOOL "" FORCE)

if(LVGL_SOURCE_DIR)
    add_subdirectory("${LVGL_SOURCE_DIR}" lvgl)
else()
    include(FetchContent)
    FetchContent_Declare(lvgl
        GIT_REPOSITORY https://github.com/lvgl/lvgl.git
        GIT_TAG 85aa60d18b3d5e5588d7b247abf90198f07c8a63
    )
    FetchContent_MakeAvailable(lvgl)
endif()

add_executable(avatar_preview
    main.cc
    ../../main/boards/stackchan/official_avatar.cc
)
target_include_directories(avatar_preview PRIVATE ../../main/boards/stackchan ../../main)
target_link_libraries(avatar_preview PRIVATE lvgl::lvgl)
enable_testing()
add_test(NAME official_avatar_render COMMAND avatar_preview)
