"""Behaviour tests for the CCTV Lite video path. No window, no NVR."""

from __future__ import annotations

import threading
import unittest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from cctv_lite.player import (
    classify_rtp_caps,
    fit_even,
    gst_set_state_released,
    is_setup_failure,
    next_mode,
    select_mode,
)

Gst.init(None)


class PathSelection(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(select_mode(True, True), "gpu")
        self.assertEqual(select_mode(False, True), "cpu")
        self.assertEqual(select_mode(True, False), "cpu")

    def test_fallback_order(self):
        self.assertEqual(next_mode("gpu"), "cpu")
        self.assertIsNone(next_mode("cpu"))

    def test_setup_failure_is_narrow(self):
        self.assertTrue(is_setup_failure("vah264dec", "not-negotiated"))
        self.assertTrue(is_setup_failure("vapostproc", "Could not initialize VA display"))
        self.assertFalse(
            is_setup_failure("vah264dec", "Internal data stream error")
        )
        self.assertFalse(
            is_setup_failure("rtspsrc", "Could not read from resource")
        )

    def test_fit_keeps_aspect_and_even_sizes(self):
        self.assertEqual(fit_even(1920, 1080, 640, 360, False), (640, 360))
        self.assertEqual(fit_even(2560, 1440, 800, 600, False), (800, 450))
        self.assertEqual(fit_even(640, 360, 1920, 1080, False), (640, 360))
        self.assertEqual(fit_even(640, 360, 1920, 1080, True), (1920, 1080))
        width, height = fit_even(1920, 1080, 100, 100, False)
        self.assertEqual((width % 2, height % 2), (0, 0))
        self.assertLessEqual(width, 100)
        self.assertLessEqual(height, 100)

    def test_rtp_caps(self):
        h264 = Gst.Caps.from_string(
            "application/x-rtp,media=(string)video,encoding-name=(string)H264"
        )
        h265 = Gst.Caps.from_string(
            "application/x-rtp,media=(string)video,encoding-name=(string)HEVC"
        )
        audio = Gst.Caps.from_string(
            "application/x-rtp,media=(string)audio,encoding-name=(string)PCMU"
        )
        self.assertEqual(classify_rtp_caps(h264), ("video", "H264"))
        self.assertEqual(classify_rtp_caps(h265), ("video", "H265"))
        self.assertEqual(classify_rtp_caps(audio), ("audio", "PCMU"))
        self.assertEqual(classify_rtp_caps(None), ("", ""))


class NullTeardown(unittest.TestCase):
    def test_blocking_null_releases_the_gil(self):
        """A worker must finish set_state(NULL) while the main thread runs."""
        errors = []

        def once():
            pipe = Gst.parse_launch("videotestsrc num-buffers=5 ! fakesink")
            pipe.set_state(Gst.State.PLAYING)
            done = threading.Event()

            def worker():
                try:
                    gst_set_state_released(pipe, Gst.State.NULL)
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(done.wait(3), "NULL did not return")
            thread.join(1)
            self.assertFalse(thread.is_alive())

        for _ in range(8):
            once()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
