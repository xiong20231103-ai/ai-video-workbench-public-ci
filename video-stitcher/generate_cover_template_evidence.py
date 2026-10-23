"""Generate deterministic visual-regression contact sheets for cover templates."""

import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cover_evidence_stitcher", ROOT / "board_video_stitcher.py")
stitcher = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT))
spec.loader.exec_module(stitcher)


def make_sheet(base, logo, rows, output):
    thumb_size = (270, 480)
    header = 34
    sheet = Image.new("RGB", (thumb_size[0] * 4, (thumb_size[1] + header) * len(rows)), "#ECE9E2")
    draw = ImageDraw.Draw(sheet)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        for row_index, (material_id, language_code) in enumerate(rows):
            copy = stitcher.localized_copy(material_id, language_code)
            for column, template_id in enumerate(stitcher.COVER_TEMPLATE_IDS):
                rendered = temp / f"{row_index}-{column}.png"
                config = {
                    **stitcher.default_cover_config(stitcher.MATERIALS[material_id]),
                    **copy,
                    "material_id": material_id,
                    "material_label": stitcher.MATERIALS[material_id],
                    "language_code": language_code,
                    "cover_template_id": template_id,
                    "logo_path": str(logo),
                }
                stitcher.render_cover_frame(base, logo, config, rendered)
                with Image.open(rendered) as image:
                    image = image.convert("RGB")
                    image.thumbnail(thumb_size, Image.Resampling.LANCZOS)
                    x = column * thumb_size[0] + (thumb_size[0] - image.width) // 2
                    y = row_index * (thumb_size[1] + header) + header
                    sheet.paste(image, (x, y))
                label = f"{material_id} / {language_code} / {template_id}"
                draw.text((column * thumb_size[0] + 8, row_index * (thumb_size[1] + header) + 9), label, fill="#111111")
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, "PNG")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--set", choices=("focus", "materials"), default="focus")
    args = parser.parse_args()
    base = args.base
    if not base or not base.is_file():
        base = args.output.parent / "cover-template-evidence-base.png"
        canvas = Image.new("RGB", (540, 960), "#394851")
        draw = ImageDraw.Draw(canvas)
        for index in range(16):
            shade = 54 + index * 5
            draw.rectangle((index * 34, 0, index * 34 + 34, 960), fill=(shade, shade + 8, shade + 14))
        canvas.save(base)
    logo = ROOT / "ExampleBrandC-transparent.png"
    if args.set == "focus":
        rows = [("pu_stone", code) for code in ("de", "ru", "ko", "id", "it")]
    else:
        rows = [(material_id, "en") for material_id in stitcher.MATERIALS]
    make_sheet(base, logo, rows, args.output)


if __name__ == "__main__":
    main()
