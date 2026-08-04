#include "camera_stream_capability.h"

#include <gtest/gtest.h>

TEST(CameraStreamCapabilityTest, MatchesTheBoardBuildFlag) {
    EXPECT_EQ(
        camera_stream_protocol::kCameraStreamEnabled,
        EXPECTED_STACKCHAN_CAMERA_STREAM != 0
    );
}
