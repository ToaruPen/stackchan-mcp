import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import release  # noqa: E402


class ReleaseScriptTest(unittest.TestCase):
    def test_dequeue_timeout_patch_is_stackchan_only(self) -> None:
        self.assertTrue(
            release.should_apply_esp_video_dqbuf_timeout("stackchan", "esp32s3")
        )
        self.assertFalse(
            release.should_apply_esp_video_dqbuf_timeout("m5stack-core-s3", "esp32s3")
        )
        self.assertFalse(
            release.should_apply_esp_video_dqbuf_timeout("stackchan", "esp32")
        )


if __name__ == "__main__":
    unittest.main()
