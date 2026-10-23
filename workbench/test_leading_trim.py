from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from leading_trim import LEADING_TRIM_SECONDS, LeadingTrimError, trim_leading_cover


FFROOT = Path(r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\AI_Subtitle_Backend\backend\ffmpeg\win_x64")
FFMPEG = FFROOT / "ffmpeg.exe"
FFPROBE = FFROOT / "ffprobe.exe"


@unittest.skipUnless(FFMPEG.is_file() and FFPROBE.is_file(), "TikHub FFmpeg runtime unavailable")
class LeadingTrimIntegrationTests(unittest.TestCase):
    def _duration(self, path: Path) -> float:
        completed = subprocess.run(
            [str(FFPROBE), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        return float(json.loads(completed.stdout)["format"]["duration"])

    def _source(self, root: Path, duration: float = 4.0) -> Path:
        path = root / "source.mp4"
        completed = subprocess.run(
            [str(FFMPEG), "-y", "-f", "lavfi", "-i", f"color=c=red:s=1080x1920:r=30:d={duration}",
             "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
             "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18", "-c:a", "aac", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if completed.returncode:
            self.skipTest(f"NVENC test encoder unavailable: {completed.stderr[-200:]}")
        return path

    def test_trim_is_exact_reusable_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._source(root)
            source_before = source.stat().st_size
            result = trim_leading_cover(source, FFMPEG, FFPROBE)
            output = Path(result["outputPath"])
            self.assertTrue(output.is_file())
            self.assertEqual(source_before, source.stat().st_size)
            self.assertAlmostEqual(self._duration(source) - LEADING_TRIM_SECONDS, self._duration(output), delta=0.40)
            self.assertTrue(Path(str(output) + ".leading-trim.json").is_file())
            repeated = trim_leading_cover(source, FFMPEG, FFPROBE)
            self.assertTrue(repeated["reused"])
            self.assertEqual(result["outputPath"], repeated["outputPath"])

    def test_short_source_is_blocked_without_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._source(root, duration=2.5)
            with self.assertRaises(LeadingTrimError):
                trim_leading_cover(source, FFMPEG, FFPROBE)
            self.assertFalse((root / "source_leadtrim2s.mp4").exists())


if __name__ == "__main__":
    unittest.main()
