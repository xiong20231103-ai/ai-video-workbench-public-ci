import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageFont


MODULE_PATH = Path(__file__).with_name("board_video_stitcher.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("korean_cover_font_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class KoreanCoverFontTests(unittest.TestCase):
    def test_korean_uses_malgun_gothic_for_all_cover_text(self):
        config = {"language_code": "ko"}
        self.assertEqual(MODULE.MALGUN_GOTHIC_BOLD_FONT, MODULE._cover_font_path(config))
        self.assertEqual(MODULE.MALGUN_GOTHIC_FONT, MODULE._cover_font_path(config, subtitle=True))

    def test_selected_font_contains_distinct_hangul_glyphs(self):
        font = ImageFont.truetype(str(MODULE.MALGUN_GOTHIC_BOLD_FONT), 96)
        glyphs = {bytes(font.getmask(char)) for char in "\uce74\ubcf8\ud06c\ub9ac\uc2a4\ud0c8\ud328\ub110"}
        self.assertGreaterEqual(len(glyphs), 6)

    def test_korean_cover_renders_real_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "base.png"
            logo = root / "logo.png"
            output = root / "cover.png"
            Image.new("RGB", (1080, 1920), "#303030").save(base)
            Image.new("RGBA", (240, 80), (255, 255, 255, 180)).save(logo)
            MODULE.render_cover_frame(
                base,
                logo,
                {
                    "language_code": "ko",
                    "material_id": "carbon_crystal",
                    "title": "\uce74\ubcf8 \ud06c\ub9ac\uc2a4\ud0c8 \ud328\ub110",
                    "vignette_enabled": False,
                    "effect_strength": "medium",
                },
                output,
            )
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
