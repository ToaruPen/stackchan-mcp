#ifndef CAMERA_STREAM_CAPABILITY_H
#define CAMERA_STREAM_CAPABILITY_H

namespace camera_stream_protocol {

#if CONFIG_BOARD_TYPE_STACKCHAN
inline constexpr bool kCameraStreamEnabled = true;
#else
inline constexpr bool kCameraStreamEnabled = false;
#endif

}  // namespace camera_stream_protocol

#endif  // CAMERA_STREAM_CAPABILITY_H
