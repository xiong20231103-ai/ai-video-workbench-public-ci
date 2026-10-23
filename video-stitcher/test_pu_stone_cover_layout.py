import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "board_video_stitcher.py"
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("pu_stone_cover_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

from localized_cover_data import LANGUAGES, localized_copy


class PuStoneCoverLayoutTests(unittest.TestCase):
    def test_pu_stone_keeps_complete_localized_title(self):
        for language_code in LANGUAGES:
            copy = localized_copy("pu_stone", language_code)
            _category, title = MODULE._split_cover_heading(
                {
                    "language_code": language_code,
                    "material_id": "pu_stone",
                    "title": copy["title"],
                }
            )
            self.assertEqual(copy["title"], title)

    def test_all_localized_pu_stone_covers_render(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "base.png"
            logo = root / "logo.png"
            Image.new("RGB", (1080, 1920), "#30343b").save(base)
            Image.new("RGBA", (300, 90), (255, 255, 255, 220)).save(logo)
            for language_code in LANGUAGES:
                copy = localized_copy("pu_stone", language_code)
                output = root / f"pu-stone-{language_code}.png"
                MODULE.render_cover_frame(
                    base,
                    logo,
                    {
                        "language_code": language_code,
                        "material_id": "pu_stone",
                        "title": copy["title"],
                        "subtitle": copy["subtitle"],
                        "vignette_enabled": False,
                        "effect_strength": "medium",
                    },
                    output,
                )
                self.assertTrue(output.is_file(), language_code)
                self.assertGreater(output.stat().st_size, 10_000, language_code)


if __name__ == "__main__":
    unittest.main()
