/*
 * Local StackChan integration bound for esp_video 1.3.1.
 *
 * The upstream VIDIOC_DQBUF path waits forever. Camera stream shutdown joins
 * the producer before replying, so dequeue must wake within a bounded time.
 */
#ifndef ESP_VIDEO_DQBUF_TIMEOUT_H
#define ESP_VIDEO_DQBUF_TIMEOUT_H

#define ESP_VIDEO_DQBUF_TIMEOUT_MS 250U

#if defined(CONFIG_BOARD_TYPE_STACKCHAN) && CONFIG_BOARD_TYPE_STACKCHAN
#define ESP_VIDEO_DQBUF_WAIT_TICKS pdMS_TO_TICKS(ESP_VIDEO_DQBUF_TIMEOUT_MS)
#else
#define ESP_VIDEO_DQBUF_WAIT_TICKS portMAX_DELAY
#endif

#endif  // ESP_VIDEO_DQBUF_TIMEOUT_H
