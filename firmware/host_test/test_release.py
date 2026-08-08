import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release  # noqa: E402


def _dqbuf_source(
    tick_declaration: str = "",
    wait_expression: str = "portMAX_DELAY",
    *,
    patched_include: bool = False,
) -> str:
    include = '#include "esp_video.h"\n'
    if patched_include:
        include += '#include "esp_video_dqbuf_timeout.h"\n'
    include += '#include "esp_video_vfs.h"'
    tick_line = f"    {tick_declaration}\n" if tick_declaration else ""
    return (
        f"{include}\n\n"
        "static esp_err_t esp_video_ioctl_dqbuf("
        "struct esp_video *video, struct v4l2_buffer *vbuf)\n"
        "{\n"
        "    esp_err_t ret;\n"
        f"{tick_line}"
        "    struct esp_video_buffer_info info;\n"
        "    struct esp_video_buffer_element *element;\n\n"
        f"    element = esp_video_recv_element(video, vbuf->type, {wait_expression});\n"
        "    return ESP_OK;\n"
        "}\n"
    )


class ReleaseScriptTest(unittest.TestCase):
    def test_dequeue_timeout_patch_is_stackchan_only(self) -> None:
        self.assertTrue(
            release.should_apply_esp_video_dqbuf_timeout("stackchan", "esp32s3")
        )
        self.assertTrue(
            release.should_apply_esp_video_dqbuf_timeout("stackchan", "esp32p4")
        )
        self.assertFalse(
            release.should_apply_esp_video_dqbuf_timeout("m5stack-core-s3", "esp32s3")
        )
        self.assertFalse(
            release.should_apply_esp_video_dqbuf_timeout("stackchan", "esp32")
        )


class EspVideoDqbufPatchTest(unittest.TestCase):
    def test_known_source_shapes_converge_to_patched_source(self) -> None:
        expected = _dqbuf_source(
            wait_expression="ESP_VIDEO_DQBUF_WAIT_TICKS",
            patched_include=True,
        )
        sources = {
            "direct-port-max-delay": _dqbuf_source(),
            "current-upstream-ticks": _dqbuf_source(
                "uint32_t ticks = portMAX_DELAY;",
                "ticks",
            ),
            "legacy-local-timeout": _dqbuf_source(
                "uint32_t ticks = pdMS_TO_TICKS(ESP_VIDEO_DQBUF_TIMEOUT_MS);",
                "ticks",
            ),
            "already-patched": expected,
        }

        for name, source in sources.items():
            with self.subTest(name=name):
                self.assertEqual(
                    release._patch_esp_video_dqbuf_source(source),
                    expected,
                )

    def test_patched_source_is_idempotent(self) -> None:
        source = _dqbuf_source(
            wait_expression="ESP_VIDEO_DQBUF_WAIT_TICKS",
            patched_include=True,
        )

        once = release._patch_esp_video_dqbuf_source(source)

        self.assertEqual(release._patch_esp_video_dqbuf_source(once), once)

    def test_unknown_declaration_is_rejected(self) -> None:
        source = _dqbuf_source(
            "const TickType_t ticks = portMAX_DELAY;",
            "ticks",
        )

        with self.assertRaisesRegex(RuntimeError, "dequeue declaration changed"):
            release._patch_esp_video_dqbuf_source(source)

    def test_mixed_known_shape_is_rejected(self) -> None:
        source = _dqbuf_source(
            "uint32_t ticks = portMAX_DELAY;",
            "portMAX_DELAY",
        )

        with self.assertRaisesRegex(RuntimeError, "dequeue source shape changed"):
            release._patch_esp_video_dqbuf_source(source)

    def test_multiple_dequeue_markers_are_rejected(self) -> None:
        source = _dqbuf_source(
            "uint32_t ticks = portMAX_DELAY;",
            "ticks",
        ).replace(
            "    return ESP_OK;",
            "    element = esp_video_recv_element("
            "video, vbuf->type, portMAX_DELAY);\n"
            "    return ESP_OK;",
        )

        with self.assertRaisesRegex(RuntimeError, "dequeue source shape changed"):
            release._patch_esp_video_dqbuf_source(source)

    def test_multiple_include_markers_are_rejected(self) -> None:
        source_include = '#include "esp_video.h"\n#include "esp_video_vfs.h"'
        patched_include = (
            '#include "esp_video.h"\n'
            '#include "esp_video_dqbuf_timeout.h"\n'
            '#include "esp_video_vfs.h"'
        )
        source = _dqbuf_source().replace(
            source_include,
            source_include + "\n" + patched_include,
        )

        with self.assertRaisesRegex(RuntimeError, "include context changed"):
            release._patch_esp_video_dqbuf_source(source)

    def test_partial_patched_source_is_rejected(self) -> None:
        source = _dqbuf_source(patched_include=True)

        with self.assertRaisesRegex(RuntimeError, "dequeue source shape changed"):
            release._patch_esp_video_dqbuf_source(source)

    def test_dequeue_marker_outside_function_is_rejected(self) -> None:
        source = _dqbuf_source() + (
            "\nstatic void unrelated_function("
            "struct esp_video *video, struct v4l2_buffer *vbuf)\n"
            "{\n"
            "    esp_video_recv_element(video, vbuf->type, portMAX_DELAY);\n"
            "}\n"
        )

        with self.assertRaisesRegex(RuntimeError, "dequeue source shape changed"):
            release._patch_esp_video_dqbuf_source(source)

    def test_unknown_extra_dequeue_is_rejected(self) -> None:
        source = _dqbuf_source().replace(
            "    return ESP_OK;",
            "    esp_video_recv_element(video, vbuf->type, UNKNOWN_WAIT);\n"
            "    return ESP_OK;",
        )

        with self.assertRaisesRegex(RuntimeError, "dequeue source shape changed"):
            release._patch_esp_video_dqbuf_source(source)


if __name__ == "__main__":
    unittest.main()
