cmake_minimum_required(VERSION 3.16)

project(stackchan_firmware_host_tests LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

include(FetchContent)

FetchContent_Declare(
    googletest
    GIT_REPOSITORY https://github.com/google/googletest.git
    GIT_TAG v1.17.0
)
FetchContent_MakeAvailable(googletest)

enable_testing()

find_package(Python3 REQUIRED COMPONENTS Interpreter)

add_test(
    NAME release_script_test
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_release.py
)

add_executable(mdns_candidate_extract_test
    test_mdns_candidate_extract.cc
    ../main/protocols/mdns_candidate_extract.cc
)

target_compile_definitions(mdns_candidate_extract_test
    PRIVATE STACKCHAN_MDNS_HOST_TEST
)

target_include_directories(mdns_candidate_extract_test
    PRIVATE
        stubs
        ../main/protocols
)

target_link_libraries(mdns_candidate_extract_test
    PRIVATE GTest::gtest_main
)

add_executable(listening_profile_test
    test_listening_profile.cc
    ../main/listening_profile.cc
)

target_include_directories(listening_profile_test
    PRIVATE
        ../main
)

target_link_libraries(listening_profile_test
    PRIVATE GTest::gtest_main
)

add_executable(camera_stream_protocol_test
    test_camera_stream_protocol.cc
)

target_include_directories(camera_stream_protocol_test
    PRIVATE
        ../main
        ../patches
)

target_link_libraries(camera_stream_protocol_test
    PRIVATE GTest::gtest_main
)

foreach(stackchan_enabled IN ITEMS 0 1)
    add_executable(camera_stream_capability_${stackchan_enabled}_test
        test_camera_stream_capability.cc
    )
    target_compile_definitions(camera_stream_capability_${stackchan_enabled}_test
        PRIVATE
            CONFIG_BOARD_TYPE_STACKCHAN=${stackchan_enabled}
            EXPECTED_STACKCHAN_CAMERA_STREAM=${stackchan_enabled}
    )
    target_include_directories(camera_stream_capability_${stackchan_enabled}_test
        PRIVATE ../main
    )
    target_link_libraries(camera_stream_capability_${stackchan_enabled}_test
        PRIVATE GTest::gtest_main
    )
endforeach()

add_executable(camera_datagram_protocol_test
    test_camera_datagram_protocol.cc
)

target_include_directories(camera_datagram_protocol_test
    PRIVATE
        ../main
)

target_link_libraries(camera_datagram_protocol_test
    PRIVATE GTest::gtest_main
)

add_executable(head_spring_motion_test
    test_head_spring_motion.cc
    ../components/smooth_ui_toolkit/src/core/animation/animate/animate.cpp
    ../components/smooth_ui_toolkit/src/core/animation/animate_value/animate_value.cpp
    ../components/smooth_ui_toolkit/src/core/animation/generators/easing/easing.cpp
    ../components/smooth_ui_toolkit/src/core/animation/generators/spring/spring.cpp
    ../components/smooth_ui_toolkit/src/core/easing/ease.cpp
    ../components/smooth_ui_toolkit/src/core/hal/hal.cpp
)

target_include_directories(head_spring_motion_test
    PRIVATE
        ../components/smooth_ui_toolkit/src
        ../main/boards/stackchan
)

target_link_libraries(head_spring_motion_test
    PRIVATE GTest::gtest_main
)

add_executable(mcp_message_dispatch_test
    test_mcp_message_dispatch.cc
)

target_include_directories(mcp_message_dispatch_test
    PRIVATE
        ../main
)

target_link_libraries(mcp_message_dispatch_test
    PRIVATE GTest::gtest_main
)

add_executable(stackchan_auto_sleep_control_test
    test_stackchan_auto_sleep_control.cc
)

target_include_directories(stackchan_auto_sleep_control_test
    PRIVATE
        ../main/boards/stackchan
)

target_link_libraries(stackchan_auto_sleep_control_test
    PRIVATE GTest::gtest_main
)

add_executable(power_save_timer_test
    test_power_save_timer.cc
    ../main/boards/common/power_save_timer.cc
)

target_include_directories(power_save_timer_test
    PRIVATE
        power_save_timer_stubs
        ../main/boards/common
        ../main/boards/stackchan
)

target_link_libraries(power_save_timer_test
    PRIVATE GTest::gtest_main
)

include(GoogleTest)
gtest_discover_tests(mdns_candidate_extract_test)
gtest_discover_tests(listening_profile_test)
gtest_discover_tests(camera_stream_protocol_test)
gtest_discover_tests(camera_stream_capability_0_test
    TEST_PREFIX "camera_stream_capability.disabled."
)
gtest_discover_tests(camera_stream_capability_1_test
    TEST_PREFIX "camera_stream_capability.enabled."
)
gtest_discover_tests(camera_datagram_protocol_test)
gtest_discover_tests(head_spring_motion_test)
gtest_discover_tests(mcp_message_dispatch_test)
gtest_discover_tests(stackchan_auto_sleep_control_test
    TEST_PREFIX "stackchan_auto_sleep_control."
)
gtest_discover_tests(power_save_timer_test
    TEST_PREFIX "power_save_timer."
)
