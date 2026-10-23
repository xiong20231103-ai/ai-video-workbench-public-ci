import tempfile
import unittest
from pathlib import Path

from ai_subtitle_worker import verify_processed_output


class _OCR:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def detect_stable_subtitle_band(self, path, fps, progress, minimum_hits=None, scan_mode="subtitle",
                                    required_consecutive=2, minimum_line_width_ratio=0.05):
        self.calls.append((path, fps, minimum_hits, scan_mode, required_consecutive, minimum_line_width_ratio))
        if self.error:
            raise self.error
        return self.result


class _CV2:
    CAP_PROP_POS_FRAMES = 1


class SubtitleVerificationTests(unittest.TestCase):
    def _verify(self, result=None, error=None):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "result.mp4"
            output.write_bytes(b"video")
            ocr = _OCR(result=result, error=error)
            value = verify_processed_output(
                output,
                {"ocrService": ocr, "cv2": _CV2()},
                1.5,
                "request-1",
                [[1200, 1320, 0, 1080]],
            )
            return value, ocr

    def test_clean_output_requires_three_consecutive_wide_line_verification(self):
        value, ocr = self._verify({
            "band": None,
            "bands": [],
            "diagnostics": {"frames_read": 24, "ocr_boxes": 0},
        })
        self.assertEqual(value["verificationStatus"], "verified_clean")
        self.assertEqual(ocr.calls[0][1], 2.0)
        self.assertEqual(ocr.calls[0][2], 3)
        self.assertEqual(ocr.calls[0][4], 3)
        self.assertEqual(ocr.calls[0][5], 0.30)
        self.assertEqual(ocr.calls[1][3], "global")

    def test_stable_residue_is_a_hard_failure(self):
        value, _ocr = self._verify({
            "band": [1200, 1320, 0, 1080],
            "bands": [[1200, 1320, 0, 1080]],
            "diagnostics": {"frames_read": 24, "ocr_boxes": 8, "candidate_frame_indices": []},
        })
        self.assertEqual(value["verificationStatus"], "residual_detected")
        self.assertEqual(value["residualBands"], [[1200, 1320, 0, 1080]])

    def test_isolated_text_is_only_a_warning(self):
        value, _ocr = self._verify({
            "band": None,
            "bands": [],
            "candidateBands": [[1180, 1340, 0, 1080]],
            "diagnostics": {"frames_read": 24, "ocr_boxes": 1},
        })
        self.assertEqual(value["verificationStatus"], "warning")
        self.assertEqual(value["candidateBands"], [[1180, 1340, 0, 1080]])

    def test_ocr_failure_blocks_downstream(self):
        value, _ocr = self._verify(error=RuntimeError("ocr offline"))
        self.assertEqual(value["verificationStatus"], "verification_failed")
        self.assertIn("ocr offline", value["message"])


if __name__ == "__main__":
    unittest.main()
