import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("board_video_stitcher.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("board_video_stitcher_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class SourceManifestTests(unittest.TestCase):
    def _app(self, folder, whitelist, required=True):
        app = MODULE.BoardVideoStitcherApp.__new__(MODULE.BoardVideoStitcherApp)
        app.segments = []
        app.validated_timing = {}
        app.ai_folder_var = _Var(str(folder))
        app.old_folder_var = _Var(str(folder))
        app.use_ai_var = _Var(False)
        app.segment_file_whitelists = {
            MODULE.STANDARD_SEGMENT_NAMES[1]: tuple(str(path) for path in whitelist)
        }
        app.segment_file_whitelist_required = required
        return app

    def test_generation_segment_uses_only_explicit_processed_file(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            raw = folder / "clip.mp4"
            trimmed = folder / "clip_leadtrim2s.mp4"
            processed = folder / "clip_leadtrim2s_ai_sttn.mp4"
            for path in (raw, trimmed, processed):
                path.write_bytes(b"video")

            app = self._app(folder, [processed])
            segments = app.load_segments_for_board("material")
            old = next(seg for seg in segments if seg.name == MODULE.STANDARD_SEGMENT_NAMES[1])

            self.assertEqual([processed.resolve()], [path.resolve() for path in old.files()])

    def test_verified_no_subtitle_raw_file_is_allowed_when_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            raw = folder / "verified_no_subtitle.mp4"
            raw.write_bytes(b"video")

            app = self._app(folder, [raw])
            segments = app.load_segments_for_board("material")
            old = next(seg for seg in segments if seg.name == MODULE.STANDARD_SEGMENT_NAMES[1])

            self.assertEqual([raw.resolve()], [path.resolve() for path in old.files()])

    def test_remote_generation_never_falls_back_when_manifest_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "raw.mp4").write_bytes(b"video")
            app = self._app(folder, [], required=True)

            with self.assertRaisesRegex(ValueError, "STITCH_SOURCE_MANIFEST_MISSING"):
                app.load_segments_for_board("material")


if __name__ == "__main__":
    unittest.main()
