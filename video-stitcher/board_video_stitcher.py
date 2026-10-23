import csv
import hashlib
import json
import math
import os
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
import cover_templates_v2 as cover_v2
from tkinter import (
    BOTH,
    DISABLED,
    END,
    LEFT,
    NORMAL,
    RIGHT,
    X,
    BooleanVar,
    IntVar,
    StringVar,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
    simpledialog,
)
from tkinter import ttk

from localized_cover_data import (
    LANGUAGES,
    LANGUAGE_LABEL_TO_CODE,
    MATERIALS,
    MATERIAL_LABEL_TO_KEY,
    MATERIAL_SERIES,
    SERIES_LABEL_TO_KEY,
    localized_copy,
)




VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
SUPPORTED_EXTS = VIDEO_EXTS | IMAGE_EXTS
AUTOMATIC_OUTPUT_ROOT = Path(r"E:\生成视频")

AUDIO_LANGUAGE_ALIASES = {
    "en": ("英语", "英文", "english"),
    "es": ("西班牙语", "西语", "español", "spanish"),
    "de": ("德语", "德文", "german", "deutsch"),
    "id": ("印度尼西亚语", "印尼语", "indonesian", "bahasa indonesia"),
    "fr": ("法语", "法文", "french", "français"),
    "pt": ("葡萄牙语", "葡语", "portuguese", "português"),
    "it": ("意大利语", "意语", "italian", "italiano"),
    "ru": ("俄语", "俄文", "russian", "русский"),
    "tr": ("土耳其语", "土语", "turkish", "türkçe"),
    "ko": ("韩语", "韩文", "korean", "한국어"),
}

AUDIO_MATERIAL_ALIASES = (
    ("flexible_pu_stone", ("pu软石", "柔性pu石", "flexible pu stone")),
    ("plastic_wood_flooring", ("塑木地板", "塑木", "plastic wood flooring", "wpc flooring")),
    ("crystal_rock_wall", ("晶岩墙板", "晶岩板", "crystal rock wall")),
    ("carbon_crystal", ("碳岩板", "碳晶板", "碳岩", "碳晶", "carbon crystal")),
    ("white_panel", ("净白板", "白色装饰板", "white panel", "white decorative")),
    ("metal_panel", ("金属板", "metal panel")),
    ("mirror_panel", ("镜面板", "mirror panel")),
    ("wood_veneer", ("木饰面", "wood veneer")),
    ("pu_stone", ("pu石", "pu stone")),
)


def detect_audio_language_code(value):
    """Infer a supported cover language from an audio filename or task name."""
    normalized = str(value or "").casefold()
    for code, aliases in AUDIO_LANGUAGE_ALIASES.items():
        if any(alias.casefold() in normalized for alias in aliases):
            return code
    return ""


def detect_audio_material_id(value):
    """Infer a supported material from an audio filename or handoff name."""
    normalized = str(value or "").casefold()
    for material_id, aliases in AUDIO_MATERIAL_ALIASES:
        if any(alias.casefold() in normalized for alias in aliases):
            return material_id
    return ""


def default_logo_brand_for_project(project_name):
    normalized = str(project_name or "").strip().casefold()
    if normalized.startswith("ProjectB"):
        return "ExampleBrandB"
    if normalized.startswith(("ProjectA", "spc地板", "spc墙板", "ProjectC", "ProjectD")):
        return "ExampleBrandC"
    return ""


def output_category_for_project(project_name):
    normalized = str(project_name or "").strip().casefold()
    if normalized.startswith("ProjectB"):
        return "ProjectB"
    if normalized.startswith("spc"):
        return "SPC"
    if normalized.startswith("ProjectD"):
        return "ProjectD"
    if normalized.startswith("ProjectC"):
        return "ProjectC"
    if normalized.startswith("ProjectA"):
        return "ProjectA"
    return ""


def dated_output_dir_for_project(project_name, now=None):
    category = output_category_for_project(project_name)
    if not category:
        return None
    current = now or datetime.now()
    return AUTOMATIC_OUTPUT_ROOT / f"{current.month}.{current.day}" / category


def generation_count_profile_for_project(project_name):
    normalized = str(project_name or "").strip().casefold()
    if normalized.startswith("spc"):
        return {"en": 1, "de": 1, "it": 1, "ru": 1}
    # Saved stitcher projects include their material suffix (for example
    # “ProjectA碳岩板”).  Remote workbench tasks use the same project, so the
    # profile must cover both the base name and the material-specific entry.
    if normalized.startswith("ProjectA"):
        return {
            "es": 6, "de": 4, "en": 2, "id": 2, "fr": 2,
            "pt": 2, "it": 2, "ru": 2, "tr": 2,
        }
    if normalized.startswith("ProjectB"):
        return {"en": 13}
    if normalized.startswith("ProjectD"):
        return {"ko": 2, "es": 4, "pt": 2, "id": 2, "de": 4, "it": 6, "ru": 2, "en": 4}
    if normalized.startswith("ProjectC"):
        return {"it": 2, "en": 2, "de": 2, "ru": 2}
    return None


def default_generation_count(project_name, language_code, fallback=1):
    profile = generation_count_profile_for_project(project_name)
    if profile is None:
        return fallback
    return profile.get(str(language_code or "").strip(), 0)


DEFAULT_DURATION = 3.0
DEFAULT_AI_DURATION = 4.0
DEFAULT_END_RESERVE = 3.0
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30
DEFAULT_CRF = 18
PROJECT_VERSION = 11
STANDARD_SEGMENT_NAMES = ("01_AI素材", "02_老素材")
COVER_SETTINGS_FILE = ".board_cover_settings.json"
PROJECTS_SETTINGS_FILE = ".board_video_projects.json"
AI_USAGE_HISTORY_FILE = ".board_ai_usage_history.json"
IMPACT_FONT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "impact.ttf"
ARIAL_BLACK_FONT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "ariblk.ttf"
ARIAL_BOLD_FONT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arialbd.ttf"
MALGUN_GOTHIC_FONT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgun.ttf"
MALGUN_GOTHIC_BOLD_FONT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgunbd.ttf"
LOGO_BRANDS = {
    "ExampleBrandC": "ExampleBrandC-transparent.png",
    "ExampleBrandB": "ExampleBrandB-transparent.png",
}
COVER_CATEGORY_WORDS = {
    "de": "PANEEL", "en": "PANEL", "es": "PANEL", "pt": "PAINEL",
    "fr": "PANNEAU", "it": "PANNELLO", "ru": "ПАНЕЛЬ",
    "id": "PANEL", "tr": "PANEL", "ko": "패널",
}
COVER_STONE_WORDS = {
    "de": "STEIN", "en": "STONE", "es": "PIEDRA", "pt": "PEDRA", "fr": "PIERRE",
    "it": "PIETRA", "ru": "КАМЕНЬ", "id": "BATU", "tr": "TAŞ", "ko": "스톤",
}
COVER_FLOOR_WORDS = {
    "de": "BODEN", "en": "FLOORING", "es": "PISO", "pt": "PISO", "fr": "SOL",
    "it": "PAVIMENTO", "ru": "ПОКРЫТИЕ", "id": "LANTAI", "tr": "ZEMİN",
}

COVER_PRESETS = {
    "white_decorative": {
        "label": "白色装饰板",
        "aliases": ("白色装饰", "white decorative"),
        "title": "WHITE DECORATIVE BOARD",
        "lines": ("WHITE DECORATIVE", "BOARD"),
        "subtitle": "",
        "style": "white",
    },
    "carbon_crystal": {
        "label": "碳晶板",
        "aliases": ("碳晶", "碳岩", "carbon crystal"),
        "title": "CARBON CRYSTAL BOARD",
        "lines": ("CARBON CRYSTAL", "BOARD"),
        "subtitle": "",
        "style": "white",
    },
    "metal_panel": {
        "label": "金属板",
        "aliases": ("金属板", "metal panel"),
        "title": "METAL PANEL",
        "lines": ("METAL PANEL",),
        "subtitle": "",
        "style": "white_shadow",
    },
    "fluted_wall": {
        "label": "格栅墙板",
        "aliases": ("格栅", "fluted"),
        "title": "FLUTED WALL PANEL",
        "lines": ("FLUTED", "WALL PANEL"),
        "subtitle": "FASTER INSTALLATION",
        "style": "yellow",
    },
    "seamless_wall": {
        "label": "无缝墙板",
        "aliases": ("无缝", "seamless"),
        "title": "SEAMLESS WALL PANEL",
        "lines": ("SEAMLESS WALL", "PANEL"),
        "subtitle": "A CLEANER WALL WITHOUT\nVISIBLE GAPS",
        "style": "yellow",
    },
    "pu_stone": {
        "label": "PU石",
        "aliases": ("pu石", "pu stone", "仿石"),
        "title": "PU STONE",
        "lines": ("PU STONE",),
        "subtitle": "REAL STONE LOOK\nLIGHTWEIGHT, EASY TO INSTALL",
        "style": "yellow",
    },
}
PRESET_LABEL_TO_KEY = {item["label"]: key for key, item in COVER_PRESETS.items()}
PRESET_KEY_TO_LABEL = {key: item["label"] for key, item in COVER_PRESETS.items()}
COVER_PRESET_CHOICES = ("自动识别", *PRESET_LABEL_TO_KEY.keys(), "自定义")
COVER_STYLE_LABELS = {"white": "白字", "white_shadow": "白字黑描边", "yellow": "黄字黑描边"}
STYLE_LABEL_TO_KEY = {label: key for key, label in COVER_STYLE_LABELS.items()}
COVER_EFFECT_STRENGTHS = ("轻微", "中等", "强烈")
COVER_EFFECT_VALUES = {
    "轻微": {"vignette": 0.20, "shadow": 0.38, "glow": 0.16},
    "中等": {"vignette": 0.35, "shadow": 0.56, "glow": 0.24},
    "强烈": {"vignette": 0.52, "shadow": 0.72, "glow": 0.34},
}

COVER_TEMPLATE_IDS = ("classic_a", "center_card_b", "editorial_c", "bottom_band_d")
NEW_COVER_TEMPLATE_IDS = cover_v2.IDS
ALL_COVER_TEMPLATE_IDS = COVER_TEMPLATE_IDS + NEW_COVER_TEMPLATE_IDS
COVER_TEMPLATE_LABELS = {
    "auto_balanced": "自动均衡（稳定随机）",
    "classic_a": "模板A · 经典金色",
    "center_card_b": "模板B · 居中卡片",
    "editorial_c": "模板C · 杂志分栏",
    "bottom_band_d": "模板D · 底部信息带",
}
COVER_TEMPLATE_LABELS.update({key: name for key, name in cover_v2.NAMES.items()})
COVER_TEMPLATE_LABELS["auto_balanced_v2"] = "新版暖灰自动均衡（A–D）"
COVER_TEMPLATE_LABEL_TO_ID = {label: key for key, label in COVER_TEMPLATE_LABELS.items()}
COVER_TEMPLATE_NAMES = {
    "classic_a": "经典金色",
    "center_card_b": "居中卡片",
    "editorial_c": "杂志分栏",
    "bottom_band_d": "底部信息带",
}
COVER_TEMPLATE_NAMES.update(cover_v2.NAMES)


def stable_cover_template(identity, output_index, mode="auto_balanced", persisted_template=""):
    """Return a retry-stable, balanced cover template assignment.

    The output index rotates through a deterministic permutation of all four
    templates.  A persisted assignment always wins, while legacy records with
    no mode retain template A.
    """
    persisted = str(persisted_template or "").strip()
    if persisted in ALL_COVER_TEMPLATE_IDS:
        template_id = persisted
    else:
        selected_mode = str(mode or "").strip()
        if selected_mode in ALL_COVER_TEMPLATE_IDS:
            template_id = selected_mode
        elif selected_mode in {"auto_balanced", "auto_balanced_v2"}:
            seed = str(identity or "").strip()
            digest = hashlib.sha256(seed.encode("utf-8")).digest()
            order = list(cover_v2.ROTATION if selected_mode == "auto_balanced_v2" else COVER_TEMPLATE_IDS)
            # Fisher-Yates driven by the stable digest, not runtime randomness.
            for position in range(len(order) - 1, 0, -1):
                swap = digest[position] % (position + 1)
                order[position], order[swap] = order[swap], order[position]
            template_id = order[max(0, int(output_index or 0)) % len(order)]
        else:
            template_id = "classic_a"
    seed_summary = hashlib.sha256(
        f"{identity}|{max(0, int(output_index or 0))}".encode("utf-8")
    ).hexdigest()[:12]
    return template_id, seed_summary


def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def supported_files(folder):
    if not folder:
        return []
    path = Path(folder)
    if not path.exists():
        return []
    files = []
    for item in path.iterdir():
        if item.is_file() and item.suffix.lower() in SUPPORTED_EXTS:
            files.append(item)
    return sorted(files, key=lambda p: p.name.lower())


def natural_sort_key(value):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value))]


def all_regular_files(folder):
    if not folder:
        return []
    path = Path(folder)
    if not path.exists():
        return []
    return sorted([item for item in path.iterdir() if item.is_file()], key=lambda p: p.name.lower())


def combination_key(board_name, picks):
    parts = [board_name]
    for segment_name, file_path in picks:
        parts.append(f"{segment_name}::{Path(file_path).name}")
    return "||".join(parts)


def safe_name(value):
    return "".join(ch if ch not in '\\\\/:"*?<>|' else "_" for ch in value)


def infer_cover_preset(board_name):
    normalized = board_name.strip().lower()
    for key, preset in COVER_PRESETS.items():
        if any(alias.lower() in normalized for alias in preset["aliases"]):
            return key
    return "custom"


def default_cover_config(board_name):
    preset_key = infer_cover_preset(board_name)
    if preset_key in COVER_PRESETS:
        preset = COVER_PRESETS[preset_key]
        return {
            "enabled": True,
            "preset": preset_key,
            "title": preset["title"],
            "subtitle": preset["subtitle"],
            "style": preset["style"],
            "vignette_enabled": True,
            "effect_strength": "中等",
        }
    return {
        "enabled": True,
        "preset": "custom",
        "title": board_name.upper(),
        "subtitle": "",
        "style": "white_shadow",
        "vignette_enabled": True,
        "effect_strength": "中等",
    }


def validate_transparent_logo(path):
    logo_path = Path(path)
    if not logo_path.is_file() or logo_path.suffix.lower() != ".png":
        raise ValueError("请选择透明背景的 PNG Logo 文件。")
    try:
        with Image.open(logo_path) as image:
            if "A" not in image.getbands():
                raise ValueError("Logo PNG 没有透明通道，请使用抠图后的透明 PNG。")
            alpha = image.getchannel("A")
            minimum, maximum = alpha.getextrema()
            if minimum == maximum == 255:
                raise ValueError("Logo PNG 背景不透明，请使用抠图后的透明 PNG。")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Logo PNG 无法读取：{exc}") from exc
    return logo_path


def _cover_title_lines(config):
    title = str(config.get("title", "")).strip()
    if not title:
        return ()
    explicit = tuple(line.strip() for line in title.splitlines() if line.strip())
    if len(explicit) > 1:
        return explicit[:2]
    words = title.split()
    if len(words) == 1:
        word = words[0]
        if len(word) <= 18:
            return (word,)
        if "-" in word:
            parts = [part for part in word.split("-") if part]
            if len(parts) >= 2:
                midpoint = (len(parts) + 1) // 2
                return ("-".join(parts[:midpoint]) + "-", "-".join(parts[midpoint:]))
        midpoint = len(word) // 2
        return (word[:midpoint], word[midpoint:])
    if len(words) == 2 and len(title) <= 22:
        return (title,)
    midpoint = (len(words) + 1) // 2
    return (" ".join(words[:midpoint]), " ".join(words[midpoint:]))


def _split_cover_heading(config):
    title = str(config.get("title", "")).strip()
    language_code = str(config.get("language_code", "en"))
    material_id = str(config.get("material_id", ""))
    if material_id in {"pu_stone", "flexible_pu_stone"}:
        category = COVER_STONE_WORDS.get(language_code, "STONE")
    elif material_id == "plastic_wood_flooring":
        category = COVER_FLOOR_WORDS.get(language_code, "FLOORING")
    else:
        category = COVER_CATEGORY_WORDS.get(language_code, "PANEL")
    if not title:
        return category, ""
    # PU stone titles are deliberately short.  Keep the complete localized
    # product name in the main line instead of reducing e.g. "PU STONE" to
    # just "PU"; the small category label can repeat STONE without making the
    # primary title look empty.
    if material_id == "pu_stone":
        return category, title
    words = title.split()
    normalized_category = category.casefold()
    remaining = [word for word in words if word.strip("-–—").casefold() != normalized_category]
    if len(remaining) == len(words):
        common = {"panel", "paneel", "painel", "panneau", "pannello", "панель", "board", "papan"}
        remaining = [word for word in words if word.strip("-–—").casefold() not in common]
    material_title = " ".join(remaining).strip() or title
    return category, material_title
def _fit_font(draw, lines, max_size, max_width, stroke_width=0, font_path=None):
    font_path = str(font_path or (IMPACT_FONT if IMPACT_FONT.exists() else "arial.ttf"))
    size = max(int(max_size), 12)
    while size > 12:
        font = ImageFont.truetype(font_path, size)
        widths = [
            draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)[2]
            for line in lines
            if line
        ]
        if not widths or max(widths) <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(font_path, 12)


def _cover_font_path(config, subtitle=False):
    if config.get("language_code") == "ko":
        candidate = MALGUN_GOTHIC_FONT if subtitle else MALGUN_GOTHIC_BOLD_FONT
        if not candidate.is_file():
            raise ValueError(
                "韩语封面字体缺失：需要 Windows Malgun Gothic，已停止生成以避免韩文显示为方框。"
            )
        return candidate
    if config.get("language_code") == "ru":
        candidate = ARIAL_BOLD_FONT if subtitle else ARIAL_BLACK_FONT
        if candidate.exists():
            return candidate
    candidate = ARIAL_BOLD_FONT if subtitle else IMPACT_FONT
    return candidate if candidate.exists() else "arial.ttf"


def _apply_cover_vignette(canvas, darkness):
    if darkness <= 0:
        return canvas
    mask_width = max(96, min(360, canvas.width // 3))
    mask_height = max(160, min(640, canvas.height // 3))
    pixels = []
    for y in range(mask_height):
        ny = abs((y + 0.5) / mask_height * 2.0 - 1.0)
        for x in range(mask_width):
            nx = abs((x + 0.5) / mask_width * 2.0 - 1.0)
            radius = math.sqrt((nx / 1.04) ** 2 + (ny / 1.08) ** 2)
            amount = max(0.0, min(1.0, (radius - 0.40) / 0.62))
            amount = amount * amount * (3.0 - 2.0 * amount)
            pixels.append(round(255 * darkness * amount))
    mask = Image.new("L", (mask_width, mask_height))
    mask.putdata(pixels)
    mask = mask.resize(canvas.size, Image.Resampling.BICUBIC).filter(
        ImageFilter.GaussianBlur(max(2, canvas.width // 180))
    )
    shade = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
    shade.putalpha(mask)
    return Image.alpha_composite(canvas, shade)


def _apply_bottom_gradient(canvas, darkness):
    if darkness <= 0:
        return canvas
    mask_height = max(256, min(720, canvas.height // 2))
    values = []
    for y in range(mask_height):
        position = y / max(1, mask_height - 1)
        progress = max(0.0, min(1.0, (position - 0.42) / 0.58))
        smooth = progress * progress * (3.0 - 2.0 * progress)
        values.append(round(255 * darkness * smooth))
    mask = Image.new("L", (1, mask_height))
    mask.putdata(values)
    mask = mask.resize((canvas.width, canvas.height), Image.Resampling.BICUBIC)
    shade = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
    shade.putalpha(mask)
    return Image.alpha_composite(canvas, shade)


def _draw_hdr_text(canvas, xy, text, font, fill, spacing, stroke_width, strength, align="center"):
    width, height = canvas.size
    shadow_alpha = round(255 * strength["shadow"])
    glow_alpha = round(255 * strength["glow"])
    offset = max(2, int(width * 0.006))

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.multiline_text(
        (xy[0] + offset, xy[1] + offset), text, font=font, fill=(0, 0, 0, shadow_alpha),
        spacing=spacing, align=align, stroke_width=stroke_width + max(1, offset // 2),
        stroke_fill=(0, 0, 0, shadow_alpha),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, int(width * 0.004))))
    canvas.alpha_composite(shadow)

    glow_mask = Image.new("L", canvas.size, 0)
    glow_draw = ImageDraw.Draw(glow_mask)
    glow_draw.multiline_text(
        xy, text, font=font, fill=glow_alpha, spacing=spacing, align=align,
        stroke_width=max(1, stroke_width // 2), stroke_fill=glow_alpha,
    )
    glow_mask = glow_mask.filter(ImageFilter.GaussianBlur(max(3, int(width * 0.007))))
    glow = Image.new("RGBA", canvas.size, fill)
    glow.putalpha(glow_mask)
    canvas.alpha_composite(glow)

    draw = ImageDraw.Draw(canvas)
    draw.multiline_text(
        xy, text, font=font, fill=fill, spacing=spacing, align=align,
        stroke_width=stroke_width, stroke_fill="#090909",
    )

    text_mask = Image.new("L", canvas.size, 0)
    mask_draw = ImageDraw.Draw(text_mask)
    mask_draw.multiline_text(xy, text, font=font, fill=255, spacing=spacing, align=align)
    bbox = text_mask.getbbox()
    if bbox:
        highlight = Image.new("L", canvas.size, 0)
        highlight_pixels = highlight.load()
        mask_pixels = text_mask.load()
        top, bottom = bbox[1], max(bbox[1] + 1, bbox[3])
        highlight_depth = top + max(1, int((bottom - top) * 0.48))
        for y in range(top, min(bottom, highlight_depth)):
            fade = 1.0 - (y - top) / max(1, highlight_depth - top)
            alpha = round(115 * fade)
            for x in range(bbox[0], bbox[2]):
                if mask_pixels[x, y]:
                    highlight_pixels[x, y] = min(mask_pixels[x, y], alpha)
        sheen = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
        sheen.putalpha(highlight.filter(ImageFilter.GaussianBlur(max(1, width // 720))))
        canvas.alpha_composite(sheen)


def _cover_logo(canvas, logo_path, *, x, y, max_width_ratio=0.28, max_height_ratio=0.078):
    with Image.open(logo_path) as logo_source:
        logo = logo_source.convert("RGBA")
    width, height = canvas.size
    scale = min(
        max(1, int(width * max_width_ratio)) / max(1, logo.width),
        max(1, int(height * max_height_ratio)) / max(1, logo.height),
    )
    logo = logo.resize(
        (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
        Image.Resampling.LANCZOS,
    )
    px = int(x - logo.width / 2) if isinstance(x, float) else int(x)
    py = int(y)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_patch = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow_patch.putalpha(logo.getchannel("A").filter(ImageFilter.GaussianBlur(max(2, width // 270))))
    shadow.alpha_composite(shadow_patch, (px + max(2, width // 270), py + max(2, width // 270)))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(logo, (px, py))
    return px, py, logo.width, logo.height


def _centered_text_x(draw, text, font, canvas_width, spacing=4, stroke_width=0):
    bbox = draw.multiline_textbbox(
        (0, 0), text, font=font, spacing=spacing, align="center", stroke_width=stroke_width
    )
    return int((canvas_width - (bbox[2] - bbox[0])) / 2)


def _render_cover_variant(base_frame, logo_path, config, output_path, template_id):
    """Render templates B-D while preserving the existing template A path."""
    logo_path = validate_transparent_logo(logo_path)
    with Image.open(base_frame) as source:
        canvas = source.convert("RGBA")
    width, height = canvas.size
    effect_strength = str(config.get("effect_strength", "中等"))
    strength = COVER_EFFECT_VALUES.get(effect_strength, COVER_EFFECT_VALUES["中等"])
    if bool(config.get("vignette_enabled", True)):
        canvas = _apply_cover_vignette(canvas, strength["vignette"])
    canvas = _apply_bottom_gradient(canvas, min(0.78, strength["vignette"] + 0.32))
    draw = ImageDraw.Draw(canvas)
    category, material_title = _split_cover_heading(config)
    title_config = dict(config)
    title_config["title"] = material_title
    title_lines = tuple(line for line in _cover_title_lines(title_config) if line)
    if not title_lines:
        raise ValueError("封面主标题不能为空。")
    title_text = "\n".join(title_lines)
    subtitle = str(config.get("subtitle", "")).strip() if str(config.get("material_id", "")) == "pu_stone" else ""
    stroke_width = max(2, int(width * 0.0042))
    gold = "#E7BE78"
    white = "#FFFFFF"
    safe_left = int(width * 0.07)
    safe_right = int(width * 0.07)
    safe_width = width - safe_left - safe_right

    if template_id == "center_card_b":
        _cover_logo(canvas, logo_path, x=width / 2, y=int(height * 0.075), max_width_ratio=0.27)
        card_left, card_right = int(width * 0.075), int(width * 0.925)
        card_top, card_bottom = int(height * 0.50), int(height * (0.89 if subtitle else 0.84))
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        radius = max(18, int(width * 0.035))
        overlay_draw.rounded_rectangle(
            (card_left, card_top, card_right, card_bottom), radius=radius,
            fill=(8, 10, 14, 174), outline=(231, 190, 120, 220), width=max(2, width // 270),
        )
        canvas.alpha_composite(overlay)
        draw = ImageDraw.Draw(canvas)
        category_font = _fit_font(draw, (category,), width * 0.06, safe_width * 0.72, 1, _cover_font_path(config, subtitle=True))
        category_x = _centered_text_x(draw, category, category_font, width)
        category_y = card_top + int(height * 0.035)
        _draw_hdr_text(canvas, (category_x, category_y), category, category_font, white, 2, 1, strength, align="center")
        title_font = _fit_font(draw, title_lines, width * 0.135, safe_width * 0.76, stroke_width, _cover_font_path(config))
        spacing = max(3, int(title_font.size * 0.08))
        title_x = _centered_text_x(draw, title_text, title_font, width, spacing, stroke_width)
        title_y = category_y + int(category_font.size * 1.35)
        _draw_hdr_text(canvas, (title_x, title_y), title_text, title_font, gold, spacing, stroke_width, strength, align="center")
        if subtitle:
            subtitle_lines = tuple(line.strip() for line in subtitle.splitlines() if line.strip())[:2]
            subtitle_font = _fit_font(draw, subtitle_lines, width * 0.043, safe_width * 0.72, 1, _cover_font_path(config, subtitle=True))
            subtitle_text = "\n".join(subtitle_lines)
            subtitle_x = _centered_text_x(draw, subtitle_text, subtitle_font, width, int(subtitle_font.size * 0.18), 1)
            subtitle_y = card_bottom - int(height * 0.105)
            _draw_hdr_text(canvas, (subtitle_x, subtitle_y), subtitle_text, subtitle_font, white, max(3, int(subtitle_font.size * 0.18)), 1, strength, align="center")

    elif template_id == "editorial_c":
        _cover_logo(canvas, logo_path, x=safe_left, y=int(height * 0.07), max_width_ratio=0.26)
        panel_left, panel_top = int(width * 0.045), int(height * 0.48)
        panel_right, panel_bottom = int(width * 0.74), int(height * 0.93)
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom), radius=max(16, width // 45), fill=(5, 8, 12, 154)
        )
        line_x = panel_left + int(width * 0.035)
        overlay_draw.rounded_rectangle(
            (line_x, panel_top + int(height * 0.035), line_x + max(5, width // 135), panel_bottom - int(height * 0.035)),
            radius=max(2, width // 270), fill=(231, 190, 120, 245),
        )
        canvas.alpha_composite(overlay)
        draw = ImageDraw.Draw(canvas)
        text_left = line_x + int(width * 0.04)
        text_width = panel_right - text_left - int(width * 0.03)
        category_font = _fit_font(draw, (category,), width * 0.058, text_width, 1, _cover_font_path(config, subtitle=True))
        category_y = panel_top + int(height * 0.045)
        _draw_hdr_text(canvas, (text_left, category_y), category, category_font, white, 2, 1, strength, align="left")
        title_font = _fit_font(draw, title_lines, width * 0.125, text_width, stroke_width, _cover_font_path(config))
        spacing = max(3, int(title_font.size * 0.06))
        title_y = category_y + int(category_font.size * 1.5)
        _draw_hdr_text(canvas, (text_left, title_y), title_text, title_font, gold, spacing, stroke_width, strength, align="left")
        if subtitle:
            subtitle_lines = tuple(line.strip() for line in subtitle.splitlines() if line.strip())[:2]
            subtitle_font = _fit_font(draw, subtitle_lines, width * 0.039, text_width, 1, _cover_font_path(config, subtitle=True))
            subtitle_y = panel_bottom - int(height * 0.11)
            _draw_hdr_text(canvas, (text_left, subtitle_y), "\n".join(subtitle_lines), subtitle_font, white, max(3, int(subtitle_font.size * 0.18)), 1, strength, align="left")

    elif template_id == "bottom_band_d":
        logo_box = _cover_logo(canvas, logo_path, x=width * 0.73, y=int(height * 0.07), max_width_ratio=0.25)
        band_top = int(height * (0.60 if subtitle else 0.65))
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle((0, band_top, width, height), fill=(4, 7, 11, 188))
        overlay_draw.rectangle((0, band_top, width, band_top + max(5, height // 320)), fill=(231, 190, 120, 242))
        canvas.alpha_composite(overlay)
        draw = ImageDraw.Draw(canvas)
        pill_font = _fit_font(draw, (category,), width * 0.05, safe_width * 0.55, 1, _cover_font_path(config, subtitle=True))
        pill_box = draw.textbbox((0, 0), category, font=pill_font)
        pill_w = pill_box[2] - pill_box[0] + int(width * 0.075)
        pill_h = pill_box[3] - pill_box[1] + int(height * 0.022)
        pill_x, pill_y = safe_left, band_top + int(height * 0.04)
        draw.rounded_rectangle((pill_x, pill_y, pill_x + pill_w, pill_y + pill_h), radius=pill_h // 2, fill=gold)
        draw.text((pill_x + int(width * 0.035), pill_y + int(height * 0.009)), category, font=pill_font, fill="#111317")
        title_font = _fit_font(draw, title_lines, width * 0.13, safe_width, stroke_width, _cover_font_path(config))
        spacing = max(3, int(title_font.size * 0.06))
        title_y = pill_y + pill_h + int(height * 0.025)
        _draw_hdr_text(canvas, (safe_left, title_y), title_text, title_font, gold, spacing, stroke_width, strength, align="left")
        if subtitle:
            subtitle_lines = tuple(line.strip() for line in subtitle.splitlines() if line.strip())[:2]
            subtitle_font = _fit_font(draw, subtitle_lines, width * 0.04, safe_width, 1, _cover_font_path(config, subtitle=True))
            subtitle_y = height - int(height * 0.10)
            _draw_hdr_text(canvas, (safe_left, subtitle_y), "\n".join(subtitle_lines), subtitle_font, white, max(3, int(subtitle_font.size * 0.18)), 1, strength, align="left")
    else:
        raise ValueError(f"未知封面模板：{template_id}")

    canvas.convert("RGB").save(output_path, "PNG")


def render_cover_frame(base_frame, logo_path, config, output_path):
    template_id = str(config.get("cover_template_id") or "classic_a")
    if template_id in NEW_COVER_TEMPLATE_IDS:
        category, title = _split_cover_heading(config)
        return cover_v2.render(base_frame, logo_path, config, output_path, category, title)
    if template_id != "classic_a":
        return _render_cover_variant(base_frame, logo_path, config, output_path, template_id)
    logo_path = validate_transparent_logo(logo_path)
    with Image.open(base_frame) as source:
        canvas = source.convert("RGBA")
    width, height = canvas.size
    effect_strength = str(config.get("effect_strength", "中等"))
    strength = COVER_EFFECT_VALUES.get(effect_strength, COVER_EFFECT_VALUES["中等"])
    if bool(config.get("vignette_enabled", True)):
        canvas = _apply_cover_vignette(canvas, strength["vignette"])
    canvas = _apply_bottom_gradient(canvas, min(0.72, strength["vignette"] + 0.28))

    with Image.open(logo_path) as logo_source:
        logo = logo_source.convert("RGBA")
    safe_left = int(width * 0.07)
    safe_right = int(width * 0.07)
    safe_width = width - safe_left - safe_right
    max_logo_width = int(width * 0.30)
    max_logo_height = int(height * 0.085)
    scale = min(max_logo_width / logo.width, max_logo_height / logo.height)
    logo_size = (max(1, int(logo.width * scale)), max(1, int(logo.height * scale)))
    logo = logo.resize(logo_size, Image.Resampling.LANCZOS)
    logo_xy = (safe_left, int(height * 0.07))
    logo_shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_logo = logo.getchannel("A").filter(ImageFilter.GaussianBlur(max(2, width // 270)))
    shadow_logo = shadow_logo.point(lambda value: round(value * strength["shadow"] * 0.75))
    shadow_patch = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow_patch.putalpha(shadow_logo)
    logo_shadow.alpha_composite(shadow_patch, (logo_xy[0] + max(2, width // 270), logo_xy[1] + max(2, width // 270)))
    canvas.alpha_composite(logo_shadow)
    canvas.alpha_composite(logo, logo_xy)

    draw = ImageDraw.Draw(canvas)
    category, material_title = _split_cover_heading(config)
    title_config = dict(config)
    title_config["title"] = material_title
    title_lines = tuple(line for line in _cover_title_lines(title_config) if line)
    if not title_lines:
        raise ValueError("封面主标题不能为空。")
    stroke_width = max(2, int(width * 0.0045))
    category_font = _fit_font(
        draw, (category,), width * 0.078, safe_width, max(1, stroke_width // 2),
        _cover_font_path(config, subtitle=True),
    )
    category_y = int(height * 0.55)
    _draw_hdr_text(
        canvas, (safe_left, category_y), category, category_font, "#FFFFFF",
        2, max(1, stroke_width // 2), strength, align="left",
    )

    is_pu_stone = str(config.get("material_id", "")) == "pu_stone"
    title_max_size = width * (0.18 if is_pu_stone else 0.145)
    title_font = _fit_font(draw, title_lines, title_max_size, safe_width, stroke_width, _cover_font_path(config))
    line_spacing = max(2, int(title_font.size * 0.05))
    title_text = "\n".join(title_lines)
    title_box = draw.multiline_textbbox(
        (0, 0), title_text, font=title_font, spacing=line_spacing, align="left", stroke_width=stroke_width
    )
    title_height = title_box[3] - title_box[1]
    category_box = draw.textbbox((0, 0), category, font=category_font, stroke_width=max(1, stroke_width // 2))
    category_height = category_box[3] - category_box[1]
    title_xy = (safe_left, category_y + category_height + int(height * 0.012))
    _draw_hdr_text(canvas, title_xy, title_text, title_font, "#E7BE78", line_spacing, stroke_width, strength, align="left")

    subtitle = str(config.get("subtitle", "")).strip()
    if is_pu_stone and subtitle:
        divider_y = title_xy[1] + title_height + int(height * 0.025)
        divider_width = int(safe_width * 0.18)
        divider_height = max(4, int(height * 0.004))
        draw.rounded_rectangle(
            (safe_left, divider_y, safe_left + divider_width, divider_y + divider_height),
            radius=max(2, divider_height // 2),
            fill="#E7BE78",
        )
        subtitle_lines = tuple(line.strip() for line in subtitle.splitlines() if line.strip())[:2]
        if subtitle_lines:
            subtitle_font = _fit_font(
                draw,
                subtitle_lines,
                width * 0.052,
                safe_width,
                max(1, stroke_width // 3),
                _cover_font_path(config, subtitle=True),
            )
            subtitle_spacing = max(4, int(subtitle_font.size * 0.22))
            subtitle_text = "\n".join(subtitle_lines)
            subtitle_y = divider_y + divider_height + int(height * 0.018)
            subtitle_box = draw.multiline_textbbox(
                (safe_left, subtitle_y),
                subtitle_text,
                font=subtitle_font,
                spacing=subtitle_spacing,
                align="left",
                stroke_width=max(1, stroke_width // 3),
            )
            if subtitle_box[3] > height - int(height * 0.055):
                raise ValueError("PU石封面卖点文字超出安全区域，已停止生成。")
            _draw_hdr_text(
                canvas,
                (safe_left, subtitle_y),
                subtitle_text,
                subtitle_font,
                "#FFFFFF",
                subtitle_spacing,
                max(1, stroke_width // 3),
                strength,
                align="left",
            )

    canvas.convert("RGB").save(output_path, "PNG")


@dataclass
class Segment:
    name: str
    path: str
    duration: float
    count: int = 0
    warnings: list[str] = field(default_factory=list)
    file_whitelist: tuple[str, ...] = ()

    def files(self):
        files = supported_files(self.path)
        if not self.file_whitelist:
            return files
        allowed = {
            os.path.normcase(str(Path(value).resolve()))
            for value in self.file_whitelist
            if str(value).strip()
        }
        return [
            path for path in files
            if os.path.normcase(str(path.resolve())) in allowed
        ]


@dataclass
class LanguageTask:
    name: str
    audio_path: str
    duration: float
    count: int = 1
    error: str = ""
    note: str = ""
    language_code: str = "en"


class VideoComposer:
    def __init__(self, ffmpeg_path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=DEFAULT_FPS, crf=DEFAULT_CRF):
        self.ffmpeg_path = ffmpeg_path
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.crf = int(crf)
        self.process = None
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

    def _run(self, args):
        if self.cancel_requested:
            raise RuntimeError("任务已取消")
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
        )
        stdout, stderr = self.process.communicate()
        if self.cancel_requested:
            raise RuntimeError("任务已取消")
        if self.process.returncode != 0:
            tail = (stderr or stdout or "").strip().splitlines()[-12:]
            raise RuntimeError("\\n".join(tail) or "FFmpeg 执行失败")
        return stdout, stderr

    def _fit_filter(self):
        return (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"setsar=1,fps={self.fps},format=yuv420p"
        )

    def normalize_segment(self, source, duration, output, leading_frame_trim=0):
        source = str(source)
        output = str(output)
        ext = Path(source).suffix.lower()
        base = [self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error"]
        if ext in IMAGE_EXTS:
            args = base + [
                "-loop",
                "1",
                "-t",
                f"{duration:.3f}",
                "-i",
                source,
                "-vf",
                self._fit_filter(),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(self.crf),
                "-pix_fmt",
                "yuv420p",
                output,
            ]
        else:
            leading_frame_trim = max(0, int(leading_frame_trim or 0))
            trim_filter = ""
            if leading_frame_trim:
                # Non-AI source videos can retain subtitle pixels for their
                # first two frames even though the downstream cover only masks
                # frame zero.  Trim five frames before normalisation and cover
                # rendering; the original source file remains untouched.
                trim_filter = f"trim=start_frame={leading_frame_trim},setpts=PTS-STARTPTS,"
            args = base + [
                "-stream_loop",
                "-1",
                "-i",
                source,
                "-t",
                f"{duration:.3f}",
                "-vf",
                trim_filter + self._fit_filter(),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(self.crf),
                "-pix_fmt",
                "yuv420p",
                output,
            ]
        self._run(args)

    def concat_segments(self, inputs, output):
        args = [self.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error"]
        for item in inputs:
            args += ["-i", str(item)]
        labels = "".join(f"[{idx}:v]" for idx in range(len(inputs)))
        args += [
            "-filter_complex",
            f"{labels}concat=n={len(inputs)}:v=1:a=0[v]",
            "-map",
            "[v]",
            "-r",
            str(self.fps),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(self.crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
        self._run(args)

    def extract_first_frame(self, source, output):
        args = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(output),
        ]
        self._run(args)

    def apply_first_frame_cover(self, source, cover_image, output):
        args = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-i",
            str(cover_image),
            "-filter_complex",
            "[0:v][1:v]overlay=0:0:enable='eq(n,0)':eof_action=pass[v]",
            "-map",
            "[v]",
            "-an",
            "-r",
            str(self.fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(self.crf),
            "-pix_fmt",
            "yuv420p",
            str(output),
        ]
        self._run(args)

    def compose(self, picks, output_path, temp_dir, cover_config=None, leading_frame_trim=0):
        normalized = []
        for idx, pick in enumerate(picks):
            out = Path(temp_dir) / f"segment_{idx:03d}.mp4"
            self.normalize_segment(
                pick["file"], pick["duration"], out,
                leading_frame_trim=leading_frame_trim if idx == 0 else 0,
            )
            if idx == 0 and cover_config and cover_config.get("enabled"):
                base_frame = Path(temp_dir) / "cover_base.png"
                rendered_frame = Path(temp_dir) / "cover_rendered.png"
                covered_segment = Path(temp_dir) / "segment_000_covered.mp4"
                self.extract_first_frame(out, base_frame)
                render_cover_frame(base_frame, cover_config["logo_path"], cover_config, rendered_frame)
                self.apply_first_frame_cover(out, rendered_frame, covered_segment)
                out = covered_segment
            normalized.append(out)
        self.concat_segments(normalized, output_path)


class BoardVideoStitcherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AI 板材视频自动拼接工具")
        self.root.geometry("1536x1000")
        self.root.minsize(1180, 760)
        self.root.configure(bg="#F5F7FA")

        self.root_dir = StringVar()
        self.project_name_var = StringVar(value="默认项目")
        self.use_ai_var = BooleanVar(value=True)
        self.ai_folder_var = StringVar()
        self.old_folder_var = StringVar()
        self.output_dir = StringVar()
        self.board_var = StringVar()
        self.ffmpeg_path = StringVar(value=shutil.which("ffmpeg") or "")
        self.audio_folder_var = StringVar()
        self.audio_material_root_var = StringVar()
        self.audio_batch_var = StringVar()
        self.audio_auto_latest_var = BooleanVar(value=True)
        self.audio_batch_info_var = StringVar(value="尚未扫描音频批次")
        self.audio_batch_paths = {}
        self.audio_batch_refreshing = False
        self.audio_task_material_root = None
        self.reference_audio_var = StringVar()
        self.prefix_var = StringVar(value="video")
        self.count_var = IntVar(value=1)
        self.old_sequential_var = BooleanVar(value=False)
        self.old_sort_mode_var = StringVar(value="下载时间")
        self.language_name_edit_var = StringVar()
        self.language_count_edit_var = StringVar(value="1")
        self.language_note_edit_var = StringVar()
        self.language_code_edit_var = StringVar(value=LANGUAGES["en"])
        self.all_boards_var = BooleanVar(value=False)
        self.duration_edit_var = StringVar(value="")
        self.width_var = StringVar(value=str(DEFAULT_WIDTH))
        self.height_var = StringVar(value=str(DEFAULT_HEIGHT))
        self.fps_var = StringVar(value=str(DEFAULT_FPS))
        self.crf_var = StringVar(value=str(DEFAULT_CRF))
        self.ai_duration_var = StringVar(value=str(DEFAULT_AI_DURATION))
        self.end_reserve_var = StringVar(value=str(DEFAULT_END_RESERVE))
        self.cover_enabled_var = BooleanVar(value=True)
        self.cover_preset_var = StringVar(value="自动识别")
        self.cover_series_var = StringVar(value=MATERIAL_SERIES["seven"]["label"])
        self.cover_material_var = StringVar(value=MATERIALS["white_panel"])
        self.cover_language_var = StringVar(value=LANGUAGES["en"])
        self.cover_title_var = StringVar()
        self.cover_subtitle_var = StringVar()
        self.cover_style_var = StringVar(value=COVER_STYLE_LABELS["white_shadow"])
        self.cover_template_mode_var = StringVar(value=COVER_TEMPLATE_LABELS["auto_balanced"])
        self.cover_vignette_var = BooleanVar(value=True)
        self.cover_effect_strength_var = StringVar(value="中等")
        self.logo_brand_var = StringVar(value="ExampleBrandC")
        self.logo_path_var = StringVar(value=str(Path(__file__).resolve().parent / LOGO_BRANDS["ExampleBrandC"]))
        self.picker_dirs = {}

        self.segments = []
        # A remote workbench handoff may supply an exact list of completed
        # Step 1/2 outputs.  Empty means the normal manual-folder behaviour.
        self.segment_file_whitelists = {}
        # When True, the remote task manifest is authoritative.  Generation
        # must stop instead of silently falling back to every video found in
        # the folder (which may also contain raw and lead-trim intermediates).
        self.segment_file_whitelist_required = False
        self.language_tasks = []
        self.validated_language_tasks = []
        self.cover_configs = {}
        self.localized_cover_texts = {}
        self.active_cover_text_key = None
        self.active_cover_board = None
        self.cover_config_loading = False
        self.cover_save_job = None
        self.cover_preview_photo = None
        self.latest_cover_preview_path = None
        self.validated_cover_configs = {}
        self.cover_assignment_context = {}
        self.cover_template_assignments = {}
        self.cover_template_assignment_callback = None
        self.reference_audio_duration = None
        self.probed_audio_path = None
        self.validated_timing = {}
        self.last_records = []
        self.worker = None
        self.msg_queue = queue.Queue()
        self.composer = None
        self.controls = []
        self.cancel_button = None
        self.preview_open_button = None
        self.latest_preview_path = None
        self.projects = {}
        self.active_project_name = ""
        self.project_loading = False
        self.project_save_job = None
        self.status_var = StringVar(value="就绪")

        self._configure_styles()
        self._build_ui()
        self.load_projects()
        self.count_var.trace_add("write", lambda *_args: self.update_stats())
        self.old_sequential_var.trace_add("write", lambda *_args: self.update_stats())
        self.ai_duration_var.trace_add("write", lambda *_args: self.on_timing_changed())
        self.end_reserve_var.trace_add("write", lambda *_args: self.on_timing_changed())
        self.reference_audio_var.trace_add("write", lambda *_args: self.on_reference_audio_path_edited())
        for variable in (
            self.use_ai_var, self.ai_folder_var, self.old_folder_var, self.output_dir, self.audio_folder_var,
            self.audio_material_root_var, self.audio_batch_var, self.audio_auto_latest_var,
            self.ffmpeg_path, self.prefix_var, self.count_var, self.old_sequential_var,
            self.old_sort_mode_var, self.width_var, self.height_var, self.fps_var,
            self.crf_var, self.ai_duration_var, self.end_reserve_var,
        ):
            variable.trace_add("write", lambda *_args: self.schedule_project_save())
        for variable in (
            self.cover_enabled_var,
            self.cover_title_var,
            self.cover_subtitle_var,
            self.cover_style_var,
            self.cover_template_mode_var,
            self.cover_vignette_var,
            self.cover_effect_strength_var,
            self.logo_brand_var,
        ):
            variable.trace_add("write", lambda *_args: self.schedule_cover_settings_save())
        self._set_running(False)
        self.update_ai_mode_controls()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_messages()
        self._log("程序已启动。请选择 AI素材、老素材、多语言音频文件夹和输出目录。")
        if not self.ffmpeg_path.get():
            self._log("未在 PATH 中找到 FFmpeg，请点击“选择 ffmpeg.exe”。")

    def _configure_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.root.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#F5F7FA", foreground="#111827")
        style.configure("App.TFrame", background="#F5F7FA")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("Card.TLabel", background="#FFFFFF", foreground="#111827")
        style.configure(
            "CardTitle.TLabel",
            background="#FFFFFF",
            foreground="#111827",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        style.configure("Muted.TLabel", background="#FFFFFF", foreground="#6B7280", font=("Microsoft YaHei UI", 9))
        style.configure("StatValue.TLabel", background="#FFFFFF", foreground="#2563EB", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("SuccessStat.TLabel", background="#FFFFFF", foreground="#059669", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("PurpleStat.TLabel", background="#FFFFFF", foreground="#7C3AED", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure(
            "TEntry",
            fieldbackground="#FFFFFF",
            bordercolor="#D1D5DB",
            lightcolor="#D1D5DB",
            darkcolor="#D1D5DB",
            padding=(10, 5),
        )
        style.configure("TCombobox", fieldbackground="#FFFFFF", bordercolor="#D1D5DB", padding=(9, 7), arrowsize=15)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#FFFFFF")],
            selectbackground=[("readonly", "#FFFFFF")],
            selectforeground=[("readonly", "#111827")],
        )
        style.configure("TCheckbutton", background="#FFFFFF", foreground="#374151", padding=4)
        style.map("TCheckbutton", background=[("active", "#FFFFFF")])
        style.configure("TNotebook", background="#FFFFFF", borderwidth=0, tabmargins=0)
        style.configure(
            "TNotebook.Tab",
            background="#F3F4F6",
            foreground="#4B5563",
            padding=(16, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#EFF6FF"), ("active", "#E5E7EB")],
            foreground=[("selected", "#2563EB")],
        )
        style.configure(
            "Outline.TButton",
            background="#FFFFFF",
            foreground="#2563EB",
            bordercolor="#93B4F4",
            padding=(14, 6),
            relief="solid",
        )
        style.map(
            "Outline.TButton",
            background=[("active", "#EFF6FF"), ("disabled", "#F3F4F6")],
            foreground=[("disabled", "#9CA3AF")],
        )
        style.configure(
            "Neutral.TButton",
            background="#FFFFFF",
            foreground="#374151",
            bordercolor="#D1D5DB",
            padding=(14, 6),
            relief="solid",
        )
        style.map(
            "Neutral.TButton",
            background=[("active", "#F9FAFB"), ("disabled", "#F3F4F6")],
            foreground=[("disabled", "#9CA3AF")],
        )
        style.configure(
            "Primary.TButton",
            background="#2563EB",
            foreground="#FFFFFF",
            bordercolor="#2563EB",
            padding=(14, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#1D4ED8"), ("disabled", "#93C5FD")],
            foreground=[("disabled", "#FFFFFF")],
        )
        style.configure(
            "Preview.TButton",
            background="#EFF6FF",
            foreground="#2563EB",
            bordercolor="#60A5FA",
            padding=(14, 7),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "Preview.TButton",
            background=[("active", "#DBEAFE"), ("disabled", "#F3F4F6")],
            foreground=[("disabled", "#9CA3AF")],
        )
        style.configure(
            "Danger.TButton",
            background="#FFFFFF",
            foreground="#DC2626",
            bordercolor="#FCA5A5",
            padding=(14, 6),
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#FEF2F2"), ("disabled", "#F3F4F6")],
            foreground=[("disabled", "#9CA3AF")],
        )
        style.configure(
            "Cancel.TButton",
            background="#F3F4F6",
            foreground="#4B5563",
            bordercolor="#D1D5DB",
            padding=(14, 6),
        )
        style.configure(
            "Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground="#1F2937",
            rowheight=34,
            bordercolor="#E5E7EB",
            borderwidth=1,
        )
        style.map("Treeview", background=[("selected", "#E8F1FF")], foreground=[("selected", "#111827")])
        style.configure(
            "Treeview.Heading",
            background="#F8FAFC",
            foreground="#374151",
            font=("Microsoft YaHei UI", 10, "bold"),
            padding=(8, 9),
            relief="flat",
        )
        style.map("Treeview.Heading", background=[("active", "#F1F5F9")])
        style.configure(
            "Blue.Horizontal.TProgressbar",
            troughcolor="#E5E7EB",
            background="#2563EB",
            bordercolor="#E5E7EB",
            lightcolor="#2563EB",
            darkcolor="#2563EB",
        )

    def _build_ui(self):
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(18, 10))
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill=X, pady=(0, 8))
        ttk.Label(
            header,
            text="AI 板材视频自动拼接工具",
            background="#F5F7FA",
            foreground="#111827",
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(side=LEFT)
        ttk.Label(
            header,
            text="静音竖屏视频批量组合",
            background="#F5F7FA",
            foreground="#6B7280",
            font=("Microsoft YaHei UI", 9),
        ).pack(side=LEFT, padx=(12, 0), pady=(5, 0))

        main = ttk.Frame(outer, style="App.TFrame")
        main.pack(fill=BOTH, expand=True)
        main.columnconfigure(0, weight=2, uniform="main")
        main.columnconfigure(1, weight=1, uniform="main")
        main.rowconfigure(0, weight=1)

        left = ttk.Frame(main, style="App.TFrame")
        right_shell = ttk.Frame(main, style="App.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right_shell.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        right_shell.columnconfigure(0, weight=1)
        right_shell.rowconfigure(0, weight=1)

        right_canvas = tk.Canvas(right_shell, bg="#F5F7FA", highlightthickness=0, borderwidth=0)
        right_scrollbar = ttk.Scrollbar(right_shell, orient="vertical", command=right_canvas.yview)
        right_canvas.configure(yscrollcommand=right_scrollbar.set)
        right_canvas.grid(row=0, column=0, sticky="nsew")
        right_scrollbar.grid(row=0, column=1, sticky="ns")
        right = ttk.Frame(right_canvas, style="App.TFrame")
        right_window = right_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind(
            "<Configure>",
            lambda _event: right_canvas.configure(scrollregion=right_canvas.bbox("all")),
        )
        right_canvas.bind(
            "<Configure>",
            lambda event: right_canvas.itemconfigure(right_window, width=event.width),
        )
        right_canvas.bind("<Enter>", lambda _event: right_canvas.bind_all("<MouseWheel>", self._scroll_right_panel))
        right_canvas.bind("<Leave>", lambda _event: right_canvas.unbind_all("<MouseWheel>"))
        right.columnconfigure(0, weight=1)

        self.build_paths_card(left)
        self.build_segments_card(left)
        self.build_settings_card(right)
        self.build_stats_card(right)
        self.build_preview_card(right)
        self.build_actions_card(right)

        self.log_text = ttk.Treeview(
            outer,
            columns=("time", "message"),
            show="headings",
            height=1,
        )

        footer = ttk.Frame(outer, style="App.TFrame")
        footer.pack(fill=X, pady=(0, 8), before=main)
        ttk.Label(
            footer,
            textvariable=self.status_var,
            background="#F5F7FA",
            foreground="#4B5563",
            font=("Microsoft YaHei UI", 9),
        ).pack(side=LEFT)
        self.progress = ttk.Progressbar(
            footer,
            mode="determinate",
            style="Blue.Horizontal.TProgressbar",
            length=260,
        )
        self.progress.pack(side=RIGHT, padx=(10, 0))
        self.progress_label = ttk.Label(
            footer,
            text="空闲",
            background="#F5F7FA",
            foreground="#6B7280",
            font=("Microsoft YaHei UI", 9),
        )
        self.progress_label.pack(side=RIGHT)

    def _scroll_right_panel(self, event):
        widget = event.widget
        while widget is not None and not isinstance(widget, tk.Canvas):
            widget = getattr(widget, "master", None)
        if widget is not None:
            widget.yview_scroll(int(-event.delta / 120), "units")

    def _card(self, parent, title):
        card = tk.Frame(
            parent,
            bg="#FFFFFF",
            highlightthickness=1,
            highlightbackground="#E1E6ED",
            bd=0,
        )
        head = ttk.Frame(card, style="Card.TFrame")
        head.pack(fill=X, padx=16, pady=(10, 6))
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(side=LEFT)
        body = ttk.Frame(card, style="Card.TFrame")
        body.pack(fill=BOTH, expand=True, padx=16, pady=(0, 8))
        return card, body

    def build_paths_card(self, parent):
        card, body = self._card(parent, "路径设置")
        card.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(body, text="当前项目", style="Card.TLabel").grid(
            row=0, column=0, padx=(0, 14), pady=5, sticky="w"
        )
        self.project_combo = ttk.Combobox(body, textvariable=self.project_name_var, state="readonly")
        self.project_combo.grid(row=0, column=1, padx=(0, 12), pady=5, sticky="ew")
        self.project_combo.bind("<<ComboboxSelected>>", self.on_project_selected)
        project_buttons = ttk.Frame(body, style="Card.TFrame")
        project_buttons.grid(row=0, column=2, pady=5, sticky="e")
        self._button(project_buttons, "新建", self.create_project).pack(side=LEFT, padx=(0, 4))
        self._button(project_buttons, "重命名", self.rename_project).pack(side=LEFT, padx=4)
        self._button(project_buttons, "删除", self.delete_project, "Danger.TButton").pack(side=LEFT, padx=(4, 0))
        ttk.Label(body, text="素材模式", style="Card.TLabel").grid(row=1, column=0, padx=(0, 14), pady=5, sticky="w")
        self.use_ai_check = ttk.Checkbutton(
            body, text="使用 AI 素材（关闭后只生成老素材）", variable=self.use_ai_var,
            command=self.on_use_ai_changed,
        )
        self.use_ai_check.grid(row=1, column=1, columnspan=2, sticky="w", pady=5)
        self.controls.append(self.use_ai_check)
        self.ai_path_controls = self._path_row(body, "AI素材文件夹", self.ai_folder_var, self.choose_ai_folder, 2)
        self._path_row(body, "老素材文件夹", self.old_folder_var, self.choose_old_folder, 3)
        self._path_row(body, "输出目录", self.output_dir, self.choose_output_dir, 4)
        self._path_row(body, "材料音频根目录", self.audio_material_root_var, self.choose_audio_folder, 5)
        ttk.Label(body, text="每日音频批次", style="Card.TLabel").grid(row=6, column=0, padx=(0, 14), pady=5, sticky="w")
        audio_batch_row = ttk.Frame(body, style="Card.TFrame")
        audio_batch_row.grid(row=6, column=1, columnspan=2, pady=5, sticky="ew")
        self.audio_batch_combo = ttk.Combobox(audio_batch_row, textvariable=self.audio_batch_var, state="readonly", width=22)
        self.audio_batch_combo.pack(side=LEFT)
        self.audio_batch_combo.bind("<<ComboboxSelected>>", self.on_audio_batch_selected)
        self.controls.append(self.audio_batch_combo)
        self.audio_auto_latest_check = ttk.Checkbutton(
            audio_batch_row, text="自动使用最新批次", variable=self.audio_auto_latest_var,
            command=self.on_audio_auto_latest_changed,
        )
        self.audio_auto_latest_check.pack(side=LEFT, padx=(10, 6))
        self.controls.append(self.audio_auto_latest_check)
        self._button(audio_batch_row, "刷新批次", self.refresh_audio_batches_from_ui).pack(side=LEFT, padx=6)
        ttk.Label(audio_batch_row, textvariable=self.audio_batch_info_var, style="Muted.TLabel").pack(side=LEFT, padx=(8, 0))
        ttk.Label(body, text="当前音频目录", style="Card.TLabel").grid(row=7, column=0, padx=(0, 14), pady=5, sticky="w")
        current_audio_entry = ttk.Entry(body, textvariable=self.audio_folder_var, state="readonly")
        current_audio_entry.grid(row=7, column=1, columnspan=2, pady=5, sticky="ew")
        self._path_row(body, "FFmpeg", self.ffmpeg_path, self.choose_ffmpeg, 8)
    def build_board_card(self, parent):
        card, body = self._card(parent, "板材类型")
        card.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="当前板材", style="Card.TLabel").grid(
            row=0,
            column=0,
            padx=(0, 12),
            sticky="w",
        )
        self.board_combo = ttk.Combobox(body, textvariable=self.board_var, state="readonly")
        self.board_combo.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.board_combo.bind("<<ComboboxSelected>>", self.on_board_selected)
        self.controls.append(self.board_combo)
        self._button(body, "刷新", self.scan_boards).grid(row=0, column=2, padx=4)
        self._button(body, "创建", self.create_board).grid(row=0, column=3, padx=4)
        self._button(body, "删除", self.delete_board, "Danger.TButton").grid(row=0, column=4, padx=4)
        check = ttk.Checkbutton(body, text="所有板材各生成 N 条", variable=self.all_boards_var)
        check.grid(row=0, column=5, padx=(16, 0))
        self.controls.append(check)

    def build_segments_card(self, parent):
        card, body = self._card(parent, "素材与语言任务")
        card.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(3, weight=1)
        body.columnconfigure(0, weight=1)

        columns = ("order", "name", "duration", "count", "warning")
        self.segment_tree = ttk.Treeview(body, columns=columns, show="headings", height=2)
        for col, text, width in [
            ("order", "顺序", 58),
            ("name", "素材类型", 210),
            ("duration", "时长(s)", 110),
            ("count", "素材数", 80),
            ("warning", "提示", 260),
        ]:
            self.segment_tree.heading(col, text=text)
            self.segment_tree.column(col, width=width, anchor="center" if col in {"order", "duration", "count"} else "w")
        self.segment_tree.grid(row=0, column=0, sticky="ew")

        material_tools = ttk.Frame(body, style="Card.TFrame")
        material_tools.grid(row=1, column=0, sticky="ew", pady=(8, 10))
        self.material_mode_label = ttk.Label(material_tools, text="固定拼接：AI素材 → 老素材", style="Muted.TLabel")
        self.material_mode_label.pack(side=LEFT)
        sequence_check = ttk.Checkbutton(
            material_tools,
            text="老素材按文件名顺序使用",
            variable=self.old_sequential_var,
        )
        sequence_check.pack(side=RIGHT, padx=(10, 0))
        self.controls.append(sequence_check)
        self.old_sort_combo = ttk.Combobox(
            material_tools,
            textvariable=self.old_sort_mode_var,
            values=("下载时间", "文件名"),
            state="readonly",
            width=8,
        )
        self.old_sort_combo.pack(side=RIGHT, padx=(6, 0))
        self.controls.append(self.old_sort_combo)
        ttk.Label(material_tools, text="排序", style="Card.TLabel").pack(side=RIGHT, padx=(10, 0))
        self._button(material_tools, "重新扫描素材", self.scan_segments).pack(side=RIGHT)

        ttk.Label(body, text="多语言任务", style="CardTitle.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 6))
        language_columns = ("language", "language_code", "note", "audio", "audio_duration", "count", "body", "old", "status")
        self.language_tree = ttk.Treeview(body, columns=language_columns, show="headings", height=7)
        for col, text, width in [
            ("language", "语言/输出名", 80),
            ("language_code", "封面语言", 95),
            ("note", "国家/语言备注", 75),
            ("audio", "音频文件", 105),
            ("audio_duration", "音频(s)", 58),
            ("count", "生成数量", 58),
            ("body", "主体(s)", 58),
            ("old", "老素材(s)", 62),
            ("status", "状态", 95),
        ]:
            self.language_tree.heading(col, text=text)
            self.language_tree.column(
                col,
                width=width,
                anchor="center" if col in {"audio_duration", "count", "body", "old"} else "w",
            )
        language_scroll = ttk.Scrollbar(body, orient="vertical", command=self.language_tree.yview)
        self.language_tree.configure(yscrollcommand=language_scroll.set)
        self.language_tree.grid(row=3, column=0, sticky="nsew")
        language_scroll.grid(row=3, column=1, sticky="ns")
        self.language_tree.bind("<<TreeviewSelect>>", self.on_language_selected)

        language_tools = ttk.Frame(body, style="Card.TFrame")
        language_tools.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        editor_row = ttk.Frame(language_tools, style="Card.TFrame")
        editor_row.pack(fill=X)
        ttk.Label(editor_row, text="封面语言", style="Card.TLabel").pack(side=LEFT)
        self.language_code_combo = ttk.Combobox(
            editor_row, textvariable=self.language_code_edit_var,
            values=tuple(LANGUAGES.values()), state="readonly", width=18,
        )
        self.language_code_combo.pack(side=LEFT, padx=(6, 10))
        self.controls.append(self.language_code_combo)
        ttk.Label(editor_row, text="名称", style="Card.TLabel").pack(side=LEFT)
        name_entry = ttk.Entry(editor_row, textvariable=self.language_name_edit_var, width=11)
        name_entry.pack(side=LEFT, padx=(6, 10))
        self.controls.append(name_entry)
        detail_row = ttk.Frame(language_tools, style="Card.TFrame")
        detail_row.pack(fill=X, pady=(6, 0))
        ttk.Label(detail_row, text="备注", style="Card.TLabel").pack(side=LEFT)
        note_entry = ttk.Entry(detail_row, textvariable=self.language_note_edit_var, width=11)
        note_entry.pack(side=LEFT, padx=(6, 10))
        self.controls.append(note_entry)
        ttk.Label(detail_row, text="数量", style="Card.TLabel").pack(side=LEFT)
        count_entry = ttk.Entry(detail_row, textvariable=self.language_count_edit_var, width=5)
        count_entry.pack(side=LEFT, padx=(6, 10))
        self.controls.append(count_entry)
        self._button(detail_row, "应用", self.apply_language_edit).pack(side=LEFT)

        order_row = ttk.Frame(language_tools, style="Card.TFrame")
        order_row.pack(fill=X, pady=(6, 0))
        self._button(order_row, "上移", lambda: self.move_language_task(-1)).pack(side=LEFT, padx=(0, 6))
        self._button(order_row, "下移", lambda: self.move_language_task(1)).pack(side=LEFT)
        self._button(order_row, "添加音频", self.add_language_audio_files).pack(side=LEFT, padx=(12, 6))
        self._button(order_row, "添加空任务", self.add_empty_language_task).pack(side=LEFT, padx=(0, 6))
        self._button(order_row, "绑定音频", self.bind_audio_to_language_task).pack(side=LEFT, padx=(0, 6))
        self._button(order_row, "删除任务", self.delete_language_task, "Danger.TButton").pack(side=LEFT)
        self._button(order_row, "扫描音频", self.scan_language_tasks).pack(side=RIGHT)

    def build_settings_card(self, parent):
        card, body = self._card(parent, "生成与首帧设置")
        card.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        notebook = ttk.Notebook(body, width=290, height=445)
        notebook.pack(fill=X)

        generation_tab = ttk.Frame(notebook, style="Card.TFrame", padding=(2, 10, 2, 2))
        generation_tab.columnconfigure(0, weight=1)
        generation_tab.columnconfigure(1, weight=1)
        notebook.add(generation_tab, text="生成参数")
        self._field_box(generation_tab, "导出文件名", self.prefix_var, 0, 0)
        self._field_box(generation_tab, "默认每种语言数量", self.count_var, 0, 1)
        self._field_box(generation_tab, "宽度", self.width_var, 1, 0)
        self._field_box(generation_tab, "高度", self.height_var, 1, 1)
        self._field_box(generation_tab, "帧率", self.fps_var, 2, 0)
        self._field_box(generation_tab, "CRF", self.crf_var, 2, 1)
        self.ai_duration_entry = self._field_box(generation_tab, "AI 时长(s)", self.ai_duration_var, 3, 0)
        self._field_box(generation_tab, "结尾预留(s)", self.end_reserve_var, 3, 1)

        cover_tab = ttk.Frame(notebook, style="Card.TFrame", padding=(2, 8, 2, 2))
        cover_tab.columnconfigure(1, weight=1)
        notebook.add(cover_tab, text="首帧封面")
        cover_check = ttk.Checkbutton(cover_tab, text="自动生成视频第 1 帧封面", variable=self.cover_enabled_var)
        cover_check.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.controls.append(cover_check)

        ttk.Label(cover_tab, text="材料系列", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self.cover_series_combo = ttk.Combobox(
            cover_tab, textvariable=self.cover_series_var,
            values=tuple(item["label"] for item in MATERIAL_SERIES.values()), state="readonly", width=15,
        )
        self.cover_series_combo.grid(row=1, column=1, sticky="ew", padx=(8, 10), pady=3)
        self.cover_series_combo.bind("<<ComboboxSelected>>", self.on_cover_series_changed)
        self.controls.append(self.cover_series_combo)

        ttk.Label(cover_tab, text="当前材料", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=3)
        self.cover_material_combo = ttk.Combobox(cover_tab, textvariable=self.cover_material_var, state="readonly", width=15)
        self.cover_material_combo.grid(row=2, column=1, sticky="ew", padx=(8, 10), pady=3)
        self.cover_material_combo.bind("<<ComboboxSelected>>", self.on_cover_text_selector_changed)
        self.controls.append(self.cover_material_combo)

        ttk.Label(cover_tab, text="预览语言", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=3)
        self.cover_language_combo = ttk.Combobox(
            cover_tab, textvariable=self.cover_language_var,
            values=tuple(LANGUAGES.values()), state="readonly", width=15,
        )
        self.cover_language_combo.grid(row=3, column=1, sticky="ew", padx=(8, 10), pady=3)
        self.cover_language_combo.bind("<<ComboboxSelected>>", self.on_cover_text_selector_changed)
        self.controls.append(self.cover_language_combo)

        ttk.Label(cover_tab, text="封面模板", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=3)
        self.cover_template_mode_combo = ttk.Combobox(
            cover_tab,
            textvariable=self.cover_template_mode_var,
            values=tuple(COVER_TEMPLATE_LABELS.values()),
            state="readonly",
            width=22,
        )
        self.cover_template_mode_combo.grid(row=4, column=1, sticky="ew", padx=(8, 10), pady=3)
        self.controls.append(self.cover_template_mode_combo)

        ttk.Label(cover_tab, text="主标题", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=3)
        title_entry = ttk.Entry(cover_tab, textvariable=self.cover_title_var)
        title_entry.grid(row=5, column=1, sticky="ew", padx=(8, 10), pady=3)
        self.controls.append(title_entry)
        ttk.Label(cover_tab, text="四周暗角", style="Card.TLabel").grid(row=6, column=0, sticky="w", pady=3)
        vignette_check = ttk.Checkbutton(cover_tab, text="启用", variable=self.cover_vignette_var)
        vignette_check.grid(row=6, column=1, sticky="w", padx=(8, 10), pady=3)
        self.controls.append(vignette_check)
        ttk.Label(cover_tab, text="增强强度", style="Card.TLabel").grid(row=7, column=0, sticky="w", pady=3)
        self.cover_effect_strength_combo = ttk.Combobox(
            cover_tab, textvariable=self.cover_effect_strength_var,
            values=COVER_EFFECT_STRENGTHS, state="readonly", width=15,
        )
        self.cover_effect_strength_combo.grid(row=7, column=1, sticky="ew", padx=(8, 10), pady=3)
        self.controls.append(self.cover_effect_strength_combo)

        ttk.Label(cover_tab, text="Logo 品牌", style="Card.TLabel").grid(row=8, column=0, sticky="w", pady=3)
        self.logo_brand_combo = ttk.Combobox(
            cover_tab, textvariable=self.logo_brand_var, values=tuple(LOGO_BRANDS), state="readonly", width=15,
        )
        self.logo_brand_combo.grid(row=8, column=1, sticky="ew", padx=(8, 10), pady=3)
        self.logo_brand_combo.bind("<<ComboboxSelected>>", self.on_logo_brand_changed)
        self.controls.append(self.logo_brand_combo)

        self.cover_thumbnail_label = tk.Label(
            cover_tab, text="首帧\n预览", bg="#111827", fg="#9CA3AF",
            width=8, height=7, font=("Microsoft YaHei UI", 9),
        )
        self.cover_thumbnail_label.grid(row=0, column=2, rowspan=8, sticky="n", pady=(2, 0))
        self._button(cover_tab, "预览全部模板", self.preview_all_cover_templates, "Preview.TButton").grid(
            row=8, column=2, sticky="ew", pady=3,
        )
    def build_stats_card(self, parent):
        card, body = self._card(parent, "统计信息")
        card.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for col in range(3):
            body.columnconfigure(col, weight=1, uniform="stats")
        self.combo_value_label = self._stat_box(body, "理论组合数", "-", 0, "StatValue.TLabel")
        self.segment_count_label = self._stat_box(body, "素材类型", "0", 1, "SuccessStat.TLabel")
        self.material_count_label = self._stat_box(body, "当前素材数量", "0", 2, "PurpleStat.TLabel")
        self.timing_summary_label = ttk.Label(
            body,
            text="音频：未选择",
            style="Card.TLabel",
            wraplength=360,
        )
        self.timing_summary_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        self.stats_warning_label = ttk.Label(body, text="", style="Muted.TLabel", wraplength=360)
        self.stats_warning_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self.stats_label = self.stats_warning_label

    def _stat_box(self, parent, title, value, column, value_style):
        frame = tk.Frame(parent, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E5E7EB")
        frame.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0 if column == 0 else 5, 0 if column == 2 else 5),
        )
        title_label = ttk.Label(frame, text=title, style="Muted.TLabel")
        title_label.pack(pady=(10, 3))
        label = ttk.Label(frame, text=value, style=value_style)
        label.pack(pady=(0, 8))
        label.stat_title_label = title_label
        return label

    def build_preview_card(self, parent):
        card, body = self._card(parent, "预览")
        card.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        top = ttk.Frame(body, style="Card.TFrame")
        top.pack(fill=X, pady=(0, 9))
        ttk.Label(top, text="最近预览文件", style="Card.TLabel").pack(side=LEFT)
        self.preview_filename_label = ttk.Label(top, text="尚未生成", style="Muted.TLabel")
        self.preview_filename_label.pack(side=RIGHT)
        buttons = ttk.Frame(body, style="Card.TFrame")
        buttons.pack(fill=X)
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.preview_open_button = self._button(buttons, "打开预览", self.open_preview_video)
        self.preview_open_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self._button(
            buttons,
            "重新生成预览",
            self.preview_current_combo,
            "Neutral.TButton",
        ).grid(row=0, column=1, sticky="ew", padx=(5, 0))

    def build_actions_card(self, parent):
        card, body = self._card(parent, "操作")
        card.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        self._button(body, "素材体检", self.health_check, "Neutral.TButton").grid(
            row=0,
            column=0,
            sticky="ew",
            padx=(0, 5),
            pady=(0, 8),
        )
        self._button(body, "预览组合清单", self.preview_combinations, "Neutral.TButton").grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(5, 0),
            pady=(0, 8),
        )
        self._button(body, "预览当前组合", self.preview_current_combo, "Preview.TButton").grid(
            row=1,
            column=0,
            sticky="ew",
            padx=(0, 5),
            pady=(0, 8),
        )
        self._button(body, "开始生成", self.start_generate, "Primary.TButton").grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(5, 0),
            pady=(0, 8),
        )
        self._button(body, "保存模板", self.save_project, "Neutral.TButton").grid(
            row=2,
            column=0,
            sticky="ew",
            padx=(0, 5),
            pady=(0, 8),
        )
        self._button(body, "打开模板", self.open_project, "Neutral.TButton").grid(
            row=2,
            column=1,
            sticky="ew",
            padx=(5, 0),
            pady=(0, 8),
        )
        self._button(body, "导出上次记录", self.export_last_records, "Neutral.TButton").grid(
            row=3,
            column=0,
            sticky="ew",
            padx=(0, 5),
            pady=(0, 8),
        )
        self.cancel_button = self._button(body, "取消", self.cancel_generate, "Cancel.TButton")
        self.cancel_button.grid(row=3, column=1, sticky="ew", padx=(5, 0), pady=(0, 8))
        self._button(body, "清空 AI 使用记录", self.clear_ai_usage_history, "Neutral.TButton").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(0, 2)
        )

    def _button(self, parent, text, command, style="Outline.TButton"):
        button = ttk.Button(parent, text=text, command=command, style=style)
        self.controls.append(button)
        return button

    def _path_row(self, parent, label, var, command, row):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(
            row=row,
            column=0,
            padx=(0, 14),
            pady=5,
            sticky="w",
        )
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, padx=(0, 12), pady=5, sticky="ew")
        self.controls.append(entry)
        button_text = "选择 ffmpeg.exe" if label == "FFmpeg" else "选择"
        button = self._button(parent, button_text, command)
        button.grid(row=row, column=2, pady=5, sticky="ew")
        parent.columnconfigure(1, weight=1)
        return entry, button

    def _field_box(self, parent, label, var, row, column):
        box = tk.Frame(parent, bg="#FFFFFF", highlightthickness=1, highlightbackground="#E5E7EB")
        box.grid(
            row=row,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 6, 0 if column == 1 else 6),
            pady=3,
        )
        ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="w", padx=9, pady=(5, 2))
        entry = ttk.Entry(box, textvariable=var)
        entry.pack(fill=X, padx=7, pady=(0, 5))
        self.controls.append(entry)
        return entry

    def _log(self, message):
        t = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("", END, values=(t, message))
        children = self.log_text.get_children()
        if len(children) > 300:
            self.log_text.delete(children[0])
        self.status_var.set(message)

    def _queue_log(self, message):
        self.msg_queue.put(("log", message))

    def _poll_messages(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    current, total = payload
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = current
                    self.progress_label.config(text=f"{current}/{total}")
                    self.status_var.set(f"正在处理 {current}/{total}")
                elif kind == "done":
                    self._set_running(False)
                    self.progress_label.config(text=payload)
                    self.status_var.set(payload)
                    self._log(payload)
                elif kind == "records":
                    self.last_records = payload
                elif kind == "preview_done":
                    preview_path = Path(payload)
                    self.latest_preview_path = preview_path
                    self.preview_filename_label.config(text=preview_path.name)
                    self.progress["value"] = 1
                    self.progress_label.config(text="预览完成")
                    self.status_var.set("预览生成完成")
                    self._set_running(False)
                    self._log(f"预览生成成功：{preview_path}")
                    self.open_preview_video()
                elif kind == "preview_error":
                    self._set_running(False)
                    self.progress_label.config(text="预览失败")
                    self.status_var.set("预览生成失败")
                    self._log(f"预览生成失败：{payload}")
                    messagebox.showerror("预览失败", str(payload))
                elif kind == "cover_preview_done":
                    self.latest_cover_preview_path = Path(payload)
                    self.show_cover_thumbnail(self.latest_cover_preview_path)
                    self._set_running(False)
                    self.progress_label.config(text="首帧预览完成")
                    self.status_var.set("首帧封面预览完成")
                    self._log(f"首帧封面预览成功：{payload}")
                elif kind == "cover_preview_error":
                    self._set_running(False)
                    self.progress_label.config(text="首帧预览失败")
                    self.status_var.set("首帧封面预览失败")
                    self._log(f"首帧封面预览失败：{payload}")
                    messagebox.showerror("首帧预览失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(150, self._poll_messages)

    def _set_running(self, running):
        for control in self.controls:
            try:
                control.configure(state=DISABLED if running else NORMAL)
            except Exception:
                pass
        try:
            self.board_combo.configure(state=DISABLED if running else "readonly")
        except Exception:
            pass
        try:
            self.project_combo.configure(state=DISABLED if running else "readonly")
        except Exception:
            pass
        if self.cancel_button is not None:
            try:
                self.cancel_button.configure(state=NORMAL if running else DISABLED)
            except Exception:
                pass
        if self.preview_open_button is not None and not running:
            preview_exists = bool(self.latest_preview_path and Path(self.latest_preview_path).exists())
            self.preview_open_button.configure(state=NORMAL if preview_exists else DISABLED)
        if not running:
            for combo in (
                getattr(self, "cover_preset_combo", None),
                getattr(self, "cover_style_combo", None),
                getattr(self, "cover_effect_strength_combo", None),
                getattr(self, "old_sort_combo", None),
                getattr(self, "language_code_combo", None),
                getattr(self, "cover_series_combo", None),
                getattr(self, "cover_material_combo", None),
                getattr(self, "cover_language_combo", None),
                getattr(self, "logo_brand_combo", None),
            ):
                if combo is not None:
                    combo.configure(state="readonly")
            self.update_ai_mode_controls()

    def update_ai_mode_controls(self):
        enabled = bool(self.use_ai_var.get())
        for control in getattr(self, "ai_path_controls", ()):
            try:
                control.configure(state=NORMAL if enabled else DISABLED)
            except Exception:
                pass
        if hasattr(self, "ai_duration_entry"):
            self.ai_duration_entry.configure(state=NORMAL if enabled else DISABLED)
        if hasattr(self, "material_mode_label"):
            self.material_mode_label.configure(
                text="固定拼接：AI素材 → 老素材" if enabled else "当前模式：仅老素材"
            )

    def on_use_ai_changed(self):
        self.update_ai_mode_controls()
        self.refresh_direct_task()
        self.refresh_language_tasks()
        self.schedule_project_save()

    def settings_root(self):
        source_value = self.ai_folder_var.get().strip() if self.use_ai_var.get() else self.old_folder_var.get().strip()
        source_folder = Path(source_value) if source_value else None
        if source_folder and source_folder.is_dir():
            return source_folder.parent
        output = Path(self.output_dir.get().strip()) if self.output_dir.get().strip() else None
        return output if output and output.is_dir() else None

    def cover_settings_path(self):
        root = self.settings_root() or Path(".")
        return root / COVER_SETTINGS_FILE

    def load_cover_settings(self):
        self.cover_configs = {}
        path = self.cover_settings_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.cover_configs = data.get("boards", {}) if isinstance(data.get("boards", {}), dict) else {}
            loaded_texts = data.get("localized_texts", {})
            if isinstance(loaded_texts, dict):
                self.localized_cover_texts.update(loaded_texts)
            brand = str(data.get("logo_brand", ""))
            if brand not in LOGO_BRANDS:
                brand = "ExampleBrandB" if "baijax" in Path(str(data.get("logo_path", ""))).name.lower() else "ExampleBrandC"
            self.logo_brand_var.set(brand)
            self.logo_path_var.set(str(self.logo_path_for_brand()))
        except Exception as exc:
            self._log(f"封面设置读取失败：{exc}")

    def current_series_key(self):
        return SERIES_LABEL_TO_KEY.get(self.cover_series_var.get(), "seven")

    def current_material_id(self):
        material_id = MATERIAL_LABEL_TO_KEY.get(self.cover_material_var.get())
        allowed = MATERIAL_SERIES[self.current_series_key()]["materials"]
        return material_id if material_id in allowed else allowed[0]

    def apply_automatic_output_dir(self):
        desired = dated_output_dir_for_project(self.active_project_name)
        if desired is None:
            return False
        desired_text = str(desired)
        if self.output_dir.get().strip() == desired_text:
            return False
        self.output_dir.set(desired_text)
        self.picker_dirs["output"] = desired_text
        return True

    def apply_automatic_cover_identity(self, tasks=None):
        detected = {
            detect_audio_material_id(Path(task.audio_path).stem if task.audio_path else task.name)
            for task in (tasks or [])
        }
        detected.discard("")
        material_id = next(iter(detected)) if len(detected) == 1 else ""
        changed = False
        if material_id:
            series_key = next(
                (key for key, value in MATERIAL_SERIES.items() if material_id in value["materials"]),
                "",
            )
            if series_key:
                series_label = MATERIAL_SERIES[series_key]["label"]
                material_label = MATERIALS[material_id]
                if self.cover_series_var.get() != series_label:
                    self.cover_series_var.set(series_label)
                    changed = True
                self.refresh_cover_material_choices()
                if self.cover_material_var.get() != material_label:
                    self.cover_material_var.set(material_label)
                    changed = True

        logo_brand = default_logo_brand_for_project(self.active_project_name)
        if logo_brand and self.logo_brand_var.get() != logo_brand:
            self.logo_brand_var.set(logo_brand)
            self.logo_path_var.set(str(self.logo_path_for_brand()))
            changed = True
        return changed

    def current_cover_language_code(self):
        return LANGUAGE_LABEL_TO_CODE.get(self.cover_language_var.get(), "en")

    def cover_text_key(self, material_id=None, language_code=None):
        return f"{material_id or self.current_material_id()}|{language_code or self.current_cover_language_code()}"

    def refresh_cover_material_choices(self, choose_first=False):
        series = MATERIAL_SERIES[self.current_series_key()]
        labels = tuple(MATERIALS[item] for item in series["materials"])
        if hasattr(self, "cover_material_combo"):
            self.cover_material_combo["values"] = labels
        if choose_first or self.cover_material_var.get() not in labels:
            self.cover_material_var.set(labels[0])

    def commit_current_cover_config(self, board_name=None):
        if self.cover_config_loading:
            return
        board = board_name or self.active_cover_board
        if not board:
            return
        if self.active_cover_text_key:
            self.localized_cover_texts[self.active_cover_text_key] = {
                "title": self.cover_title_var.get().strip(),
                "subtitle": self.cover_subtitle_var.get().strip().replace(" / ", "\n"),
            }
        self.cover_configs[board] = {
            "enabled": bool(self.cover_enabled_var.get()),
            "preset": self.current_material_id(),
            "style": STYLE_LABEL_TO_KEY.get(self.cover_style_var.get(), "white_shadow"),
            "template_mode": COVER_TEMPLATE_LABEL_TO_ID.get(
                self.cover_template_mode_var.get(), "auto_balanced"
            ),
            "vignette_enabled": bool(self.cover_vignette_var.get()),
            "effect_strength": self.cover_effect_strength_var.get()
            if self.cover_effect_strength_var.get() in COVER_EFFECT_STRENGTHS else "中等",
        }

    def save_cover_settings(self):
        self.commit_current_cover_config()
        root = self.settings_root()
        if not root or not root.is_dir():
            return
        path = self.cover_settings_path()
        payload = {
            "version": 5,
            "logo_brand": self.logo_brand_var.get(),
            "logo_path": str(self.logo_path_for_brand()),
            "boards": self.cover_configs,
            "localized_texts": self.localized_cover_texts,
        }
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            self._log(f"封面设置保存失败：{exc}")

    def schedule_cover_settings_save(self):
        if self.cover_config_loading or not self.active_cover_board:
            return
        if self.cover_save_job is not None:
            self.root.after_cancel(self.cover_save_job)
        self.cover_save_job = self.root.after(500, self._save_scheduled_cover_settings)

    def _save_scheduled_cover_settings(self):
        self.cover_save_job = None
        self.save_cover_settings()
        self.schedule_project_save()

    def load_localized_cover_text_into_vars(self):
        material_id = self.current_material_id()
        language_code = self.current_cover_language_code()
        key = self.cover_text_key(material_id, language_code)
        text = dict(localized_copy(material_id, language_code))
        text.update(self.localized_cover_texts.get(key, {}))
        self.cover_config_loading = True
        try:
            self.cover_title_var.set(str(text.get("title", "")))
            self.cover_subtitle_var.set(str(text.get("subtitle", "")).replace("\n", " / "))
        finally:
            self.cover_config_loading = False
        self.active_cover_text_key = key

    def load_cover_config_into_vars(self, board_name):
        self.active_cover_board = board_name or None
        if not board_name:
            return
        config = dict(self.cover_configs.get(board_name) or default_cover_config(board_name))
        self.cover_configs[board_name] = config
        self.cover_config_loading = True
        try:
            self.cover_enabled_var.set(bool(config.get("enabled", True)))
            style_key = config.get("style", "white_shadow")
            self.cover_style_var.set(COVER_STYLE_LABELS.get(style_key, COVER_STYLE_LABELS["white_shadow"]))
            template_mode = str(config.get("template_mode") or "classic_a")
            if template_mode not in COVER_TEMPLATE_LABELS:
                template_mode = "classic_a"
            self.cover_template_mode_var.set(COVER_TEMPLATE_LABELS[template_mode])
            self.cover_vignette_var.set(bool(config.get("vignette_enabled", True)))
            strength = str(config.get("effect_strength", "中等"))
            self.cover_effect_strength_var.set(strength if strength in COVER_EFFECT_STRENGTHS else "中等")
        finally:
            self.cover_config_loading = False
        self.refresh_cover_material_choices()
        self.load_localized_cover_text_into_vars()

    def on_cover_series_changed(self, _event=None):
        self.commit_current_cover_config()
        self.refresh_cover_material_choices(choose_first=True)
        self.load_localized_cover_text_into_vars()
        self.save_cover_settings()

    def on_cover_text_selector_changed(self, _event=None):
        self.commit_current_cover_config()
        self.refresh_cover_material_choices()
        self.load_localized_cover_text_into_vars()
        self.schedule_cover_settings_save()

    def on_cover_preset_selected(self, _event=None):
        self.on_cover_text_selector_changed(_event)

    def cover_config_for_language(self, board_name, language_code):
        if board_name == self.active_cover_board:
            self.commit_current_cover_config(board_name)
        config = dict(self.cover_configs.get(board_name) or default_cover_config(board_name))
        material_id = self.current_material_id()
        text = dict(localized_copy(material_id, language_code))
        text.update(self.localized_cover_texts.get(self.cover_text_key(material_id, language_code), {}))
        config.update({
            "preset": material_id,
            "material_id": material_id,
            "material_label": MATERIALS[material_id],
            "material_series": self.current_series_key(),
            "language_code": language_code,
            "language_label": LANGUAGES.get(language_code, LANGUAGES["en"]),
            "title": str(text.get("title", "")).strip(),
            "subtitle": str(text.get("subtitle", "")).strip() if material_id == "pu_stone" else "",
            "logo_brand": self.logo_brand_var.get(),
            "logo_path": str(self.logo_path_for_brand()),
            "template_mode": COVER_TEMPLATE_LABEL_TO_ID.get(
                self.cover_template_mode_var.get(), str(config.get("template_mode") or "classic_a")
            ),
        })
        config.setdefault("vignette_enabled", True)
        config.setdefault("effect_strength", "中等")
        if config.get("enabled"):
            if not config["title"]:
                raise ValueError(f"{MATERIALS[material_id]} / {LANGUAGES.get(language_code, language_code)} 的封面主标题不能为空。")
            validate_transparent_logo(config["logo_path"])
        return config

    def cover_config_for_board(self, board_name):
        return self.cover_config_for_language(board_name, self.current_cover_language_code())
    def logo_path_for_brand(self):
        brand = self.logo_brand_var.get() if self.logo_brand_var.get() in LOGO_BRANDS else "ExampleBrandC"
        return Path(__file__).resolve().parent / LOGO_BRANDS[brand]

    def on_logo_brand_changed(self, _event=None):
        self.logo_path_var.set(str(self.logo_path_for_brand()))
        self.save_cover_settings()
        self.schedule_project_save()

    def on_board_selected(self, _event=None):
        self.commit_current_cover_config()
        self.scan_segments()
        self.load_cover_config_into_vars(self.board_var.get().strip())
        self.save_cover_settings()

    def material_hint(self):
        value = self.ai_folder_var.get().strip() if self.use_ai_var.get() else self.old_folder_var.get().strip()
        if not value:
            return "当前任务"
        folder = Path(value)
        generic_names = {"ai素材", "01_ai素材", "ai", "01_ai"}
        return folder.parent.name if folder.name.lower() in generic_names and folder.parent.name else folder.name

    def active_segment_names(self):
        return STANDARD_SEGMENT_NAMES if self.use_ai_var.get() else (STANDARD_SEGMENT_NAMES[1],)

    @staticmethod
    def find_segment(segments, name):
        return next((segment for segment in segments if segment.name == name), None)

    def refresh_direct_task(self):
        self.commit_current_cover_config()
        task_name = self.material_hint()
        self.board_var.set(task_name)
        source_value = self.ai_folder_var.get().strip() if self.use_ai_var.get() else self.old_folder_var.get().strip()
        self.root_dir.set(str(Path(source_value).parent) if source_value else "")
        self.scan_segments()
        self.load_cover_config_into_vars(task_name)
        self.save_cover_settings()
        self._log("已扫描 AI 素材和老素材文件夹。" if self.use_ai_var.get() else "已扫描老素材文件夹。")

    def projects_settings_path(self):
        return Path(__file__).resolve().parent / PROJECTS_SETTINGS_FILE

    def ai_usage_history_path(self):
        return Path(__file__).resolve().parent / AI_USAGE_HISTORY_FILE

    @staticmethod
    def normalized_media_path(path):
        return os.path.normcase(str(Path(path).resolve()))

    @staticmethod
    def _folder_sets(value):
        if not isinstance(value, dict):
            return {}
        return {
            str(folder): {str(item) for item in items if isinstance(item, str)}
            for folder, items in value.items() if isinstance(items, list)
        }

    def load_ai_usage_history(self):
        # "seen" survives round resets and "清空 AI 使用记录": it remembers every
        # AI file ever offered, so files added later are recognised as new.
        self._ai_seen_history = {}
        path = self.ai_usage_history_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            self._ai_seen_history = self._folder_sets(data.get("seen"))
            return self._folder_sets(data.get("folders"))
        except Exception as exc:
            self._log(f"AI 使用记录读取失败，将重新建立：{exc}")
            return {}

    def save_ai_usage_history(self, usage):
        path = self.ai_usage_history_path()
        seen = getattr(self, "_ai_seen_history", None)
        if seen is None:
            # Never loaded in this run (e.g. clearing from the UI): keep what is on disk.
            try:
                seen = self._folder_sets(json.loads(path.read_text(encoding="utf-8")).get("seen"))
            except Exception:
                seen = {}
        payload = {
            "version": 2,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "folders": {folder: sorted(used) for folder, used in usage.items() if used},
            "seen": {folder: sorted(items) for folder, items in seen.items() if items},
        }
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def clear_ai_usage_history(self):
        if not messagebox.askyesno("清空 AI 使用记录", "清空后，所有项目的 AI 素材会重新从第一轮开始随机。确定继续吗？"):
            return
        self.save_ai_usage_history({})
        self._log("已清空全项目 AI 使用记录。")
        messagebox.showinfo("完成", "AI 使用记录已清空。")

    def choose_global_ai_file(self, segment, files, usage, previous=None):
        folder_key = self.normalized_media_path(segment.path)
        used = usage.setdefault(folder_key, set())
        file_map = {self.normalized_media_path(path): path for path in files}
        used.intersection_update(file_map)
        seen_history = getattr(self, "_ai_seen_history", None)
        if seen_history is None:
            seen_history = self._ai_seen_history = {}
        seen = seen_history.get(folder_key)
        if seen is None:
            # First time this folder is tracked: everything already present is
            # the baseline, so only files the user adds later count as new.
            seen = seen_history[folder_key] = set(file_map)
        fresh = [path for key, path in file_map.items() if key not in seen]
        if fresh:
            remaining = len(fresh) - 1
            if previous and len(fresh) > 1:
                fresh = [path for path in fresh if str(path) != previous] or fresh
            selected = random.choice(fresh)
            key = self.normalized_media_path(selected)
            seen.add(key)
            used.add(key)
            self._queue_log(f"优先使用新加入的 AI 素材（本文件夹还剩 {remaining} 个新素材未用）：{selected.name}")
            return selected
        available = [path for key, path in file_map.items() if key not in used]
        if not available:
            used.clear()
            available = list(file_map.values())
            self._queue_log(f"AI 素材文件夹已完成一轮，开始新一轮随机：{segment.path}")
        if previous and len(available) > 1:
            without_previous = [path for path in available if str(path) != previous]
            if without_previous:
                available = without_previous
        selected = random.choice(available)
        used.add(self.normalized_media_path(selected))
        return selected

    def capture_project_state(self):
        return {
            "use_ai": bool(self.use_ai_var.get()),
            "ai_folder": self.ai_folder_var.get().strip(),
            "old_folder": self.old_folder_var.get().strip(),
            "output_dir": self.output_dir.get().strip(),
            "audio_folder": self.audio_folder_var.get().strip(),
            "audio_material_root": self.audio_material_root_var.get().strip(),
            "audio_batch": self.audio_batch_var.get().strip(),
            "audio_auto_latest": bool(self.audio_auto_latest_var.get()),
            "ffmpeg_path": self.ffmpeg_path.get().strip(),
            "prefix": self.prefix_var.get().strip(),
            "count": int(self.count_var.get() or 1),
            "old_sequential": bool(self.old_sequential_var.get()),
            "old_sort_mode": self.old_sort_mode_var.get(),
            "width": self.width_var.get(),
            "height": self.height_var.get(),
            "fps": self.fps_var.get(),
            "crf": self.crf_var.get(),
            "ai_duration": self.ai_duration_var.get(),
            "end_reserve": self.end_reserve_var.get(),
            "cover_series": self.current_series_key(),
            "cover_material": self.current_material_id(),
            "cover_template_mode": COVER_TEMPLATE_LABEL_TO_ID.get(
                self.cover_template_mode_var.get(), "auto_balanced"
            ),
            "localized_cover_texts": self.localized_cover_texts,
            "logo_brand": self.logo_brand_var.get(),
            "logo_path": str(self.logo_path_for_brand()),
            "picker_dirs": dict(self.picker_dirs),
            "cover_configs": self.cover_configs,
            "language_tasks": [
                {"name": task.name, "audio_path": task.audio_path, "duration": task.duration,
                 "count": task.count, "error": task.error, "note": task.note, "language_code": task.language_code}
                for task in self.language_tasks
            ],
            "generation_count_profile_version": 1,
        }

    def apply_project_state(self, data):
        self.project_loading = True
        try:
            self.segment_file_whitelists = {}
            self.segment_file_whitelist_required = False
            self.use_ai_var.set(bool(data.get("use_ai", True)))
            self.ai_folder_var.set(str(data.get("ai_folder", "")))
            self.old_folder_var.set(str(data.get("old_folder", "")))
            self.output_dir.set(str(data.get("output_dir", "")))
            self.apply_automatic_output_dir()
            self.configure_audio_project_state(data)
            self.ffmpeg_path.set(str(data.get("ffmpeg_path", self.ffmpeg_path.get())))
            self.prefix_var.set(str(data.get("prefix", "video")))
            self.count_var.set(max(0, int(data.get("count", 1))))
            self.old_sequential_var.set(bool(data.get("old_sequential", False)))
            self.old_sort_mode_var.set(str(data.get("old_sort_mode", "下载时间")))
            self.width_var.set(str(data.get("width", DEFAULT_WIDTH)))
            self.height_var.set(str(data.get("height", DEFAULT_HEIGHT)))
            self.fps_var.set(str(data.get("fps", DEFAULT_FPS)))
            self.crf_var.set(str(data.get("crf", DEFAULT_CRF)))
            self.ai_duration_var.set(str(data.get("ai_duration", DEFAULT_AI_DURATION)))
            self.end_reserve_var.set(str(data.get("end_reserve", DEFAULT_END_RESERVE)))
            saved_template_mode = str(data.get("cover_template_mode") or "classic_a")
            if saved_template_mode not in COVER_TEMPLATE_LABELS:
                saved_template_mode = "classic_a"
            self.cover_template_mode_var.set(COVER_TEMPLATE_LABELS[saved_template_mode])
            default_series = "spc" if "spc" in self.active_project_name.lower() else "seven"
            series_key = str(data.get("cover_series", default_series))
            if series_key not in MATERIAL_SERIES:
                series_key = default_series
            self.cover_series_var.set(MATERIAL_SERIES[series_key]["label"])
            allowed_materials = MATERIAL_SERIES[series_key]["materials"]
            material_id = str(data.get("cover_material", allowed_materials[0]))
            if material_id not in allowed_materials:
                material_id = allowed_materials[0]
            self.cover_material_var.set(MATERIALS[material_id])
            saved_texts = data.get("localized_cover_texts", {})
            self.localized_cover_texts = dict(saved_texts) if isinstance(saved_texts, dict) else {}
            saved_cover_configs = data.get("cover_configs", {})
            self.cover_configs = dict(saved_cover_configs) if isinstance(saved_cover_configs, dict) else {}
            brand = str(data.get("logo_brand", ""))
            if brand not in LOGO_BRANDS:
                brand = "ExampleBrandB" if "baijax" in Path(str(data.get("logo_path", ""))).name.lower() else "ExampleBrandC"
            self.logo_brand_var.set(brand)
            self.logo_path_var.set(str(self.logo_path_for_brand()))
            saved_picker_dirs = data.get("picker_dirs", {})
            self.picker_dirs = dict(saved_picker_dirs) if isinstance(saved_picker_dirs, dict) else {}
            self.active_cover_text_key = None
            self.language_tasks = []
            for item in data.get("language_tasks", []):
                if not isinstance(item, dict):
                    continue
                audio_path = str(item.get("audio_path", ""))
                self.language_tasks.append(LanguageTask(
                    name=str(item.get("name") or (Path(audio_path).stem if audio_path else "未命名任务")),
                    audio_path=audio_path,
                    duration=float(item.get("duration", 0) or 0),
                    count=max(0, int(item.get("count", 1))),
                    error=str(item.get("error", "")),
                    note=str(item.get("note", "")),
                    language_code=str(item.get("language_code", "en")) if str(item.get("language_code", "en")) in LANGUAGES else "en",
                ))
            self.refresh_audio_batches(scan_tasks=True, log_result=False)
        finally:
            self.project_loading = False
        self.active_cover_board = None
        if self.apply_automatic_cover_identity(self.language_tasks):
            self.save_cover_settings()
        self.refresh_direct_task()
        self.refresh_language_tasks(select_index=0 if self.language_tasks else None)

    def refresh_project_combo(self):
        names = list(self.projects)
        self.project_combo["values"] = names
        if self.active_project_name in names:
            self.project_name_var.set(self.active_project_name)

    def load_projects(self):
        path = self.projects_settings_path()
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                messagebox.showwarning("项目配置", f"项目配置读取失败，将建立默认项目：{exc}")
        projects = data.get("projects", {}) if isinstance(data, dict) else {}
        self.projects = projects if isinstance(projects, dict) else {}
        if not self.projects:
            self.projects = {"默认项目": self.capture_project_state()}
        for project_name, project_data in self.projects.items():
            if not isinstance(project_data, dict):
                continue
            logo_brand = default_logo_brand_for_project(project_name)
            if logo_brand:
                project_data["logo_brand"] = logo_brand
                project_data["logo_path"] = str(Path(__file__).resolve().parent / LOGO_BRANDS[logo_brand])
            output_dir = dated_output_dir_for_project(project_name)
            if output_dir is not None:
                project_data["output_dir"] = str(output_dir)
            try:
                profile_version = int(project_data.get("generation_count_profile_version", 0) or 0)
            except (TypeError, ValueError):
                profile_version = 0
            if profile_version < 1:
                tasks = project_data.get("language_tasks", [])
                if isinstance(tasks, list):
                    for task in tasks:
                        if not isinstance(task, dict):
                            continue
                        language_code = str(task.get("language_code", "")).strip()
                        if language_code not in LANGUAGES:
                            language_code = detect_audio_language_code(
                                task.get("audio_path", "") or task.get("name", "")
                            )
                        task["count"] = default_generation_count(
                            project_name,
                            language_code,
                            fallback=max(0, int(task.get("count", 1) or 0)),
                        )
                project_data["generation_count_profile_version"] = 1
        requested = str(data.get("active_project", "")) if isinstance(data, dict) else ""
        self.active_project_name = requested if requested in self.projects else next(iter(self.projects))
        self.refresh_project_combo()
        self.apply_project_state(self.projects[self.active_project_name])
        self.save_projects()

    def save_projects(self):
        if self.project_loading:
            return
        if self.active_project_name:
            self.projects[self.active_project_name] = self.capture_project_state()
        payload = {"version": 2, "active_project": self.active_project_name, "projects": self.projects}
        path = self.projects_settings_path()
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

    def schedule_project_save(self):
        if self.project_loading or not self.active_project_name:
            return
        if self.project_save_job is not None:
            self.root.after_cancel(self.project_save_job)
        self.project_save_job = self.root.after(500, self._save_scheduled_project)

    def _save_scheduled_project(self):
        self.project_save_job = None
        self.save_projects()

    def on_project_selected(self, _event=None):
        selected = self.project_name_var.get().strip()
        if not selected or selected == self.active_project_name or selected not in self.projects:
            return
        self.save_cover_settings()
        if self.active_project_name:
            self.projects[self.active_project_name] = self.capture_project_state()
        self.active_project_name = selected
        self.apply_project_state(self.projects[selected])
        self.save_projects()
        self._log(f"已切换项目：{selected}")

    def create_project(self):
        name = simpledialog.askstring("新建项目", "请输入项目名称：", parent=self.root)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showwarning("提示", "项目名称不能为空。")
            return
        if name in self.projects:
            messagebox.showwarning("提示", "这个项目名称已经存在。")
            return
        self.save_projects()
        self.projects[name] = {
            "ai_folder": "", "old_folder": "", "output_dir": "", "audio_folder": "",
            "audio_material_root": "", "audio_batch": "", "audio_auto_latest": True,
            "ffmpeg_path": self.ffmpeg_path.get().strip(), "prefix": name, "count": 1,
            "use_ai": True, "old_sequential": False, "old_sort_mode": "下载时间", "language_tasks": [],
            "logo_brand": "ExampleBrandC", "picker_dirs": {},
        }
        self.active_project_name = name
        self.refresh_project_combo()
        self.apply_project_state(self.projects[name])
        self.save_projects()
        self._log(f"已创建项目：{name}")

    def rename_project(self):
        old_name = self.active_project_name
        if not old_name:
            return
        name = simpledialog.askstring("重命名项目", "请输入新的项目名称：", initialvalue=old_name, parent=self.root)
        if name is None:
            return
        name = name.strip()
        if not name or name == old_name:
            return
        if name in self.projects:
            messagebox.showwarning("提示", "这个项目名称已经存在。")
            return
        self.projects[old_name] = self.capture_project_state()
        items = [(name if key == old_name else key, value) for key, value in self.projects.items()]
        self.projects = dict(items)
        self.active_project_name = name
        self.refresh_project_combo()
        self.save_projects()
        self._log(f"项目已重命名：{old_name} → {name}")

    def delete_project(self):
        name = self.active_project_name
        if not name or not messagebox.askyesno("删除项目", f"只删除项目“{name}”的配置，不会删除任何素材文件。确定继续吗？"):
            return
        self.projects.pop(name, None)
        if not self.projects:
            self.projects["默认项目"] = {"ffmpeg_path": self.ffmpeg_path.get().strip(), "prefix": "video"}
        self.active_project_name = next(iter(self.projects))
        self.refresh_project_combo()
        self.apply_project_state(self.projects[self.active_project_name])
        self.save_projects()
        self._log(f"已删除项目配置：{name}")

    def on_close(self):
        try:
            self.save_cover_settings()
            self.save_projects()
        finally:
            self.root.destroy()

    def picker_initial_dir(self, key, current_value="", is_file=False):
        remembered = str(self.picker_dirs.get(key, "")).strip()
        if remembered and Path(remembered).is_dir():
            return remembered
        current = str(current_value).strip()
        if current:
            path = Path(current)
            candidate = path.parent if is_file or path.is_file() else path
            if candidate.is_dir():
                return str(candidate)
        return str(Path.home())

    def remember_picker_dir(self, key, selected, is_file=False):
        path = Path(selected)
        folder = path.parent if is_file else path
        self.picker_dirs[key] = str(folder)
        self.schedule_project_save()

    @staticmethod
    def audio_files_in(folder):
        try:
            return sorted(
                [path for path in Path(folder).iterdir() if path.is_file() and path.suffix.lower() in AUDIO_EXTS],
                key=lambda path: natural_sort_key(path.name),
            )
        except OSError:
            return []

    @staticmethod
    def parse_audio_batch_date(name):
        match = re.fullmatch(
            r"(?:(\d{4})[._\-年])?(\d{1,2})[._\-月](\d{1,2})日?",
            str(name).strip(),
        )
        if not match:
            return None
        try:
            year = int(match.group(1) or datetime.now().year)
            return datetime(year, int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    def audio_batch_sort_key(self, folder):
        parsed = self.parse_audio_batch_date(folder.name)
        try:
            modified = folder.stat().st_mtime
        except OSError:
            modified = 0
        return (1 if parsed else 0, parsed.timestamp() if parsed else modified, natural_sort_key(folder.name))

    def discover_audio_batches(self, material_root):
        root = Path(material_root)
        if not root.is_dir():
            return []
        children = []
        try:
            for folder in root.iterdir():
                if folder.is_dir() and self.audio_files_in(folder):
                    children.append(folder)
        except OSError:
            return []
        children.sort(key=self.audio_batch_sort_key, reverse=True)
        batches = [(folder.name, folder) for folder in children]
        if self.audio_files_in(root):
            batches.append(("根目录音频", root))
        return batches

    def configure_audio_project_state(self, data):
        saved_folder = str(data.get("audio_folder", "")).strip()
        saved_root = str(data.get("audio_material_root", "")).strip()
        saved_batch = str(data.get("audio_batch", "")).strip()
        material_root = Path(saved_root) if saved_root else None
        if material_root is None and saved_folder:
            legacy = Path(saved_folder)
            if self.audio_files_in(legacy) and self.parse_audio_batch_date(legacy.name):
                material_root = legacy.parent
                saved_batch = saved_batch or legacy.name
            else:
                material_root = legacy
                if self.audio_files_in(legacy):
                    saved_batch = saved_batch or "根目录音频"
        self.audio_material_root_var.set(str(material_root) if material_root else "")
        self.audio_task_material_root = str(material_root) if material_root else None
        self.audio_batch_var.set(saved_batch)
        self.audio_auto_latest_var.set(bool(data.get("audio_auto_latest", True)))
        self.audio_folder_var.set(saved_folder)

    def refresh_audio_batches(self, scan_tasks=True, log_result=True):
        if self.audio_batch_refreshing:
            return False
        self.audio_batch_refreshing = True
        try:
            root_value = self.audio_material_root_var.get().strip()
            root = Path(root_value) if root_value else None
            batches = self.discover_audio_batches(root) if root else []
            self.audio_batch_paths = {name: path for name, path in batches}
            names = [name for name, _path in batches]
            self.audio_batch_combo.configure(values=names)
            if not names:
                self.audio_batch_var.set("")
                self.audio_folder_var.set("")
                self.audio_batch_info_var.set("未找到包含音频的批次")
                roots = [path for path in (self.audio_task_material_root, root_value) if path]
                self.language_tasks = self.retain_tasks_outside_audio_roots(roots)
                self.audio_task_material_root = root_value or None
                self.refresh_language_tasks(select_index=0 if self.language_tasks else None)
                if log_result:
                    self._log("材料音频根目录中没有找到可用音频批次。")
                return False
            current = self.audio_batch_var.get().strip()
            if self.audio_auto_latest_var.get() or current not in self.audio_batch_paths:
                current = names[0]
            selected_folder = self.audio_batch_paths[current]
            self.audio_batch_var.set(current)
            self.audio_folder_var.set(str(selected_folder))
            count = len(self.audio_files_in(selected_folder))
            self.audio_batch_info_var.set(f"{count} 个音频")
            if scan_tasks:
                self.scan_language_tasks(log_result=log_result)
            elif log_result:
                self._log(f"当前音频批次：{current}（{count} 个音频）")
            return True
        finally:
            self.audio_batch_refreshing = False

    def refresh_audio_batches_from_ui(self):
        self.refresh_audio_batches(scan_tasks=True, log_result=True)

    def on_audio_batch_selected(self, _event=None):
        if self.audio_batch_refreshing:
            return
        selected = self.audio_batch_var.get().strip()
        folder = self.audio_batch_paths.get(selected)
        if folder is None:
            return
        self.audio_auto_latest_var.set(False)
        self.audio_folder_var.set(str(folder))
        self.audio_batch_info_var.set(f"{len(self.audio_files_in(folder))} 个音频")
        self.scan_language_tasks(log_result=True)

    def on_audio_auto_latest_changed(self):
        self.refresh_audio_batches(scan_tasks=True, log_result=True)

    def retain_tasks_outside_audio_roots(self, roots):
        resolved_roots = []
        for value in roots:
            try:
                resolved_roots.append(Path(value).resolve())
            except OSError:
                continue

        def managed(task):
            if not task.audio_path:
                return False
            try:
                audio_path = Path(task.audio_path).resolve()
            except OSError:
                return False
            for root in resolved_roots:
                try:
                    audio_path.relative_to(root)
                    return True
                except ValueError:
                    continue
            return False

        return [task for task in self.language_tasks if not managed(task)]

    def choose_ai_folder(self):
        folder = filedialog.askdirectory(
            title="选择 AI 素材文件夹",
            initialdir=self.picker_initial_dir("ai", self.ai_folder_var.get()),
        )
        if not folder:
            return
        self.remember_picker_dir("ai", folder)
        self.save_cover_settings()
        self.ai_folder_var.set(folder)
        self.active_cover_board = None
        self.update_ai_mode_controls()
        self.load_cover_settings()
        self.refresh_direct_task()

    def choose_old_folder(self):
        folder = filedialog.askdirectory(
            title="选择老素材文件夹",
            initialdir=self.picker_initial_dir("old", self.old_folder_var.get()),
        )
        if not folder:
            return
        if not self.use_ai_var.get():
            self.save_cover_settings()
        self.remember_picker_dir("old", folder)
        self.old_folder_var.set(folder)
        if not self.use_ai_var.get():
            self.active_cover_board = None
            self.load_cover_settings()
        self.refresh_direct_task()

    def choose_root_dir(self):
        folder = filedialog.askdirectory(title="选择素材根目录")
        if folder:
            self.save_cover_settings()
            self.root_dir.set(folder)
            self.active_cover_board = None
            self.load_cover_settings()
            self.scan_boards()

    def choose_output_dir(self):
        folder = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=self.picker_initial_dir("output", self.output_dir.get()),
        )
        if folder:
            self.remember_picker_dir("output", folder)
            self.output_dir.set(folder)

    def choose_audio_folder(self):
        folder = filedialog.askdirectory(
            title="选择材料音频根目录（也可以选择某一天的音频文件夹）",
            initialdir=self.picker_initial_dir("audio", self.audio_material_root_var.get() or self.audio_folder_var.get()),
        )
        if not folder:
            return
        selected = Path(folder)
        selected_batch = ""
        if self.audio_files_in(selected) and self.parse_audio_batch_date(selected.name):
            selected_batch = selected.name
            selected = selected.parent
        elif self.audio_files_in(selected) and not self.discover_audio_batches(selected):
            selected_batch = "根目录音频"
        self.remember_picker_dir("audio", selected)
        self.audio_material_root_var.set(str(selected))
        self.audio_batch_var.set(selected_batch)
        self.refresh_audio_batches(scan_tasks=True, log_result=True)

    def scan_language_tasks(self, log_result=True):
        folder_value = self.audio_folder_var.get().strip()
        if not folder_value or not Path(folder_value).is_dir():
            if log_result:
                messagebox.showwarning("音频文件夹不可用", "请选择有效的多语言音频文件夹。")
            return
        folder = Path(folder_value)
        files = sorted(
            [item for item in folder.iterdir() if item.is_file() and item.suffix.lower() in AUDIO_EXTS],
            key=lambda item: natural_sort_key(item.name),
        )
        existing = {
            str(Path(task.audio_path).resolve()): task
            for task in self.language_tasks if task.audio_path
        }
        scanned_paths = {str(item.resolve()) for item in files}
        ordered_paths = [
            str(Path(task.audio_path).resolve())
            for task in self.language_tasks
            if task.audio_path and str(Path(task.audio_path).resolve()) in scanned_paths
        ]
        ordered_paths.extend(str(item.resolve()) for item in files if str(item.resolve()) not in ordered_paths)
        try:
            default_count = max(0, int(self.count_var.get()))
        except (ValueError, tk.TclError):
            default_count = 1
        # Official production projects have a durable language distribution.
        # Do not retain a stale count from a previous audio batch (remote
        # handoffs used to persist every language as 1), otherwise merely
        # opening the project on a later day produces the wrong total.
        project_profile = generation_count_profile_for_project(self.active_project_name)
        tasks = []
        for resolved in ordered_paths:
            audio = Path(resolved)
            old = existing.get(resolved)
            detected_language_code = detect_audio_language_code(audio.stem)
            language_code = detected_language_code or (old.language_code if old else "en")
            try:
                duration = self.probe_audio_duration(audio)
                error = ""
            except ValueError as exc:
                duration = 0.0
                error = str(exc)
            tasks.append(
                LanguageTask(
                    name=old.name if old else audio.stem,
                    audio_path=str(audio),
                    duration=duration,
                    count=(
                        project_profile.get(language_code, 0)
                        if project_profile is not None
                        else (old.count if old else default_generation_count(
                            self.active_project_name, language_code, default_count
                        ))
                    ),
                    error=error,
                    note=old.note if old else "",
                    language_code=language_code,
                )
            )
        material_root_value = self.audio_material_root_var.get().strip()
        managed_roots = [path for path in (self.audio_task_material_root, material_root_value) if path]
        retained = self.retain_tasks_outside_audio_roots(managed_roots)
        self.language_tasks = tasks + retained
        self.audio_task_material_root = material_root_value or None
        identity_changed = self.apply_automatic_cover_identity(tasks)
        self.refresh_language_tasks(select_index=0 if self.language_tasks else None)
        if identity_changed and not self.project_loading:
            self.load_localized_cover_text_into_vars()
            self.save_cover_settings()
            self.schedule_project_save()
        if log_result:
            self._log(f"已扫描 {len(tasks)} 个文件夹音频，保留 {len(retained)} 个手动任务。")

    def make_language_task(self, audio_path="", name=""):
        try:
            default_count = max(0, int(self.count_var.get()))
        except (ValueError, tk.TclError):
            default_count = 1
        audio_path = str(audio_path).strip()
        if audio_path:
            try:
                duration = self.probe_audio_duration(audio_path)
                error = ""
            except ValueError as exc:
                duration = 0.0
                error = str(exc)
            task_name = name or Path(audio_path).stem
            language_code = detect_audio_language_code(Path(audio_path).stem) or "en"
            default_count = default_generation_count(self.active_project_name, language_code, default_count)
        else:
            duration = 0.0
            error = "尚未绑定音频"
            task_name = name or f"语言任务{len(self.language_tasks) + 1}"
            language_code = detect_audio_language_code(task_name) or "en"
        return LanguageTask(task_name, audio_path, duration, default_count, error, "", language_code)

    def add_language_audio_files(self):
        paths = filedialog.askopenfilenames(
            title="添加一个或多个语言音频",
            initialdir=self.picker_initial_dir("audio_files", self.audio_folder_var.get()),
            filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        self.remember_picker_dir("audio_files", paths[0], is_file=True)
        existing = {str(Path(task.audio_path).resolve()) for task in self.language_tasks if task.audio_path}
        for path in paths:
            resolved = str(Path(path).resolve())
            if resolved not in existing:
                self.language_tasks.append(self.make_language_task(path))
                existing.add(resolved)
        self.apply_automatic_cover_identity(self.language_tasks)
        self.refresh_language_tasks(select_index=len(self.language_tasks) - 1)

    def add_empty_language_task(self):
        self.language_tasks.append(self.make_language_task())
        self.refresh_language_tasks(select_index=len(self.language_tasks) - 1)

    def bind_audio_to_language_task(self):
        index = self.selected_language_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一个语言任务。")
            return
        path = filedialog.askopenfilename(
            title="为当前任务绑定音频",
            initialdir=self.picker_initial_dir("audio_files", self.language_tasks[index].audio_path or self.audio_folder_var.get(), is_file=bool(self.language_tasks[index].audio_path)),
            filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.remember_picker_dir("audio_files", path, is_file=True)
        updated = self.make_language_task(path, self.language_tasks[index].name)
        updated.count = self.language_tasks[index].count
        updated.note = self.language_tasks[index].note
        updated.language_code = self.language_tasks[index].language_code
        self.language_tasks[index] = updated
        self.apply_automatic_cover_identity([updated])
        self.refresh_language_tasks(select_index=index)

    def delete_language_task(self):
        index = self.selected_language_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一个语言任务。")
            return
        self.language_tasks.pop(index)
        self.refresh_language_tasks(select_index=min(index, len(self.language_tasks) - 1) if self.language_tasks else None)

    def selected_language_index(self):
        selection = self.language_tree.selection() if hasattr(self, "language_tree") else ()
        if not selection:
            return None
        return self.language_tree.index(selection[0])

    def selected_language_task(self, require_enabled=False):
        index = self.selected_language_index()
        if index is not None and index < len(self.language_tasks):
            task = self.language_tasks[index]
            if not require_enabled or task.count > 0:
                return task
        return next((task for task in self.language_tasks if not require_enabled or task.count > 0), None)

    def on_language_selected(self, _event=None):
        task = self.selected_language_task()
        if not task:
            return
        self.language_name_edit_var.set(task.name)
        self.language_count_edit_var.set(str(task.count))
        self.language_note_edit_var.set(task.note)
        self.language_code_edit_var.set(LANGUAGES.get(task.language_code, LANGUAGES["en"]))
        self.cover_language_var.set(LANGUAGES.get(task.language_code, LANGUAGES["en"]))
        self.on_cover_text_selector_changed()
        self.refresh_segments(scan_counts=False)

    def apply_language_edit(self):
        index = self.selected_language_index()
        if index is None:
            messagebox.showinfo("提示", "请先选择一个语言任务。")
            return
        name = self.language_name_edit_var.get().strip()
        try:
            count = int(self.language_count_edit_var.get())
        except ValueError:
            count = -1
        if not name:
            messagebox.showwarning("设置有误", "语言/输出名不能为空。")
            return
        if count < 0:
            messagebox.showwarning("设置有误", "生成数量必须是大于或等于 0 的整数。")
            return
        self.language_tasks[index].name = name
        self.language_tasks[index].count = count
        self.language_tasks[index].note = self.language_note_edit_var.get().strip()
        self.language_tasks[index].language_code = LANGUAGE_LABEL_TO_CODE.get(self.language_code_edit_var.get(), "en")
        self.refresh_language_tasks(select_index=index)

    def move_language_task(self, direction):
        index = self.selected_language_index()
        if index is None:
            return
        target = index + direction
        if target < 0 or target >= len(self.language_tasks):
            return
        self.language_tasks[index], self.language_tasks[target] = self.language_tasks[target], self.language_tasks[index]
        self.refresh_language_tasks(select_index=target)

    def refresh_language_tasks(self, select_index=None):
        if not hasattr(self, "language_tree"):
            return
        if select_index is None:
            select_index = self.selected_language_index()
        for item in self.language_tree.get_children():
            self.language_tree.delete(item)
        for task in self.language_tasks:
            try:
                timing = self.calculate_timing(require_audio=False, task=task)
                body = f"{timing['body_duration']:.3f}"
                old = f"{timing['old_duration']:.3f}"
                status = "跳过" if task.count == 0 else "可生成"
            except ValueError as exc:
                body = "-"
                old = "-"
                status = task.error or str(exc)
            self.language_tree.insert(
                "",
                END,
                values=(task.name, LANGUAGES.get(task.language_code, LANGUAGES["en"]), task.note, Path(task.audio_path).name if task.audio_path else "未绑定", f"{task.duration:.3f}" if task.duration else "-", task.count, body, old, status),
            )
        if select_index is not None and 0 <= select_index < len(self.language_tasks):
            item = self.language_tree.get_children()[select_index]
            self.language_tree.selection_set(item)
            self.language_tree.focus(item)
            self.language_tree.see(item)
        self.update_stats()
        self.schedule_project_save()

    def validated_language_task_data(self):
        active = [task for task in self.language_tasks if task.count > 0]
        if not active:
            raise ValueError("音频文件夹中没有生成数量大于 0 的语言任务。")
        names = [task.name.strip().lower() for task in active]
        if len(names) != len(set(names)):
            raise ValueError("语言/输出名不能重复，请修改后再生成。")
        result = []
        for task in active:
            if not task.audio_path:
                raise ValueError(f"{task.name}: 尚未绑定音频。")
            if not Path(task.audio_path).is_file():
                raise ValueError(f"{task.name}: 音频文件不存在，请重新绑定。")
            if task.error:
                raise ValueError(f"{task.name}: {task.error}")
            timing = self.calculate_timing(require_audio=True, task=task)
            result.append({
                "name": task.name.strip(),
                "audio_path": task.audio_path,
                "note": task.note.strip(),
                "language_code": task.language_code if task.language_code in LANGUAGES else "en",
                "audio_duration": task.duration,
                "count": task.count,
                "timing": timing,
            })
        return result

    def completed_output_count_for_task(self, task):
        """Count completed outputs for a language task without redoing handoffs.

        A TikHub handoff may move a successfully generated source video on to
        the CapCut client.  In that case the source file is no longer a
        reliable resume signal, but the Step 4 CSV record remains local and
        authoritative.  Count both sources, using the larger count so a
        partial cleanup never causes duplicate videos on a resume.
        """
        output_dir = Path(self.output_dir.get().strip())
        if not output_dir.is_dir():
            return 0
        prefix = self.prefix_var.get().strip() or "video"
        output_prefix = safe_name(f"{prefix}_{safe_name(task['name'])}")
        file_count = sum(
            1 for item in output_dir.glob(f"{output_prefix}_*.mp4")
            if item.is_file() and item.stat().st_size > 0
        )
        recorded_outputs = set()
        task_name = str(task["name"]).strip()
        audio_path = str(Path(task["audio_path"]).resolve())
        handoff_match = re.search(r"(?:remote[-_])?inbox-\d{14}-[0-9a-f]{8}-\d{2}", task_name, re.IGNORECASE)
        handoff_identity = handoff_match.group(0).casefold() if handoff_match else ""
        for record_path in output_dir.glob("generation_records_*.csv"):
            try:
                with record_path.open("r", newline="", encoding="utf-8-sig") as handle:
                    reader = csv.reader(handle)
                    header = next(reader, [])
                    for values in reader:
                        if len(values) >= len(header):
                            row = dict(zip(header, values))
                            status = row.get("status", "")
                            record_name = str(row.get("language", "")).strip()
                            recorded_audio = str(row.get("audio_path", "")).strip()
                            output = str(row.get("output", "")).strip()
                        elif len(values) >= 9:
                            # Older releases omitted two CSV fields.  Their
                            # shifted rows keep language at index 2, output at
                            # index 7, and success state at index 8.
                            status = values[8]
                            record_name = str(values[2]).strip()
                            recorded_audio = str(values[4]).strip()
                            output = str(values[7]).strip()
                        else:
                            continue
                        if status != "success":
                            continue
                        record_match = re.search(
                            r"(?:remote[-_])?inbox-\d{14}-[0-9a-f]{8}-\d{2}",
                            record_name,
                            re.IGNORECASE,
                        )
                        record_identity = record_match.group(0).casefold() if record_match else ""
                        if handoff_identity:
                            if record_identity != handoff_identity:
                                continue
                        elif record_name != task_name:
                            continue
                        if not handoff_identity and recorded_audio and str(Path(recorded_audio).resolve()) != audio_path:
                            continue
                        if output:
                            recorded_outputs.add(output.casefold())
            except (OSError, UnicodeError, csv.Error) as exc:
                self._log(f"跳过无法读取的生成记录 {record_path.name}: {exc}")
        return max(file_count, len(recorded_outputs))

    def remaining_language_task_data(self, tasks):
        """Keep configured counts as daily targets and run only the remainder."""
        remaining = []
        completed_total = 0
        target_total = 0
        for task in tasks:
            target = max(0, int(task["count"]))
            completed = min(target, self.completed_output_count_for_task(task))
            left = target - completed
            target_total += target
            completed_total += completed
            if left:
                resumed = dict(task)
                resumed["count"] = left
                resumed["cover_output_start"] = completed
                remaining.append(resumed)
        self._log(f"第4步断点续跑：目标 {target_total} 条，已完成 {completed_total} 条，本次生成 {sum(item['count'] for item in remaining)} 条。")
        return remaining

    def resolve_cover_template(self, board, task, output_index, cover_config):
        """Resolve and persist the template for one logical output.

        The output index includes the completed-output offset so a partial
        retry addresses the same cover assignment instead of starting a new
        four-template cycle.
        """
        config = dict(cover_config or {})
        logical_index = max(0, int(task.get("cover_output_start", 0) or 0)) + max(0, int(output_index))
        language_code = str(task.get("language_code") or config.get("language_code") or "en")
        context = self.cover_assignment_context if isinstance(self.cover_assignment_context, dict) else {}
        identity_parts = [
            str(context.get("batchId") or ""),
            str(context.get("canonicalTaskId") or context.get("taskId") or ""),
            str(context.get("productionDate") or ""),
            str(context.get("stableTaskId") or context.get("taskId") or ""),
            str(board),
            language_code,
        ]
        identity = "|".join(identity_parts)
        assignment_key = f"{safe_name(str(board))}|{language_code}|{logical_index}"
        persisted = self.cover_template_assignments.get(assignment_key, {})
        if isinstance(persisted, str):
            persisted_template = persisted
        elif isinstance(persisted, dict):
            persisted_template = str(persisted.get("coverTemplateId") or "")
        else:
            persisted_template = ""
        mode = str(context.get("coverTemplateMode") or config.get("template_mode") or "classic_a")
        if mode in NEW_COVER_TEMPLATE_IDS or mode == "auto_balanced_v2":
            # Attempts and manifests do not own versioned cover allocations.
            identity = "|".join([
                str(context.get("canonicalTaskId") or context.get("stableTaskId") or context.get("taskId") or ""),
                str(context.get("productionDate") or ""), str(board), language_code,
            ])
        template_id, seed_summary = stable_cover_template(
            identity,
            logical_index,
            mode=mode,
            persisted_template=persisted_template,
        )
        assignment = {
            "coverTemplateId": template_id,
            "coverTemplateName": COVER_TEMPLATE_NAMES[template_id],
            "stableSeed": seed_summary,
            "logicalOutputIndex": logical_index,
            "board": str(board),
            "languageCode": language_code,
        }
        if template_id in NEW_COVER_TEMPLATE_IDS:
            assignment = cover_v2.persist_assignment(
                Path(__file__).resolve().parent.parent / "workbench" / "handoff" / "cover_templates_v2",
                identity, assignment_key, assignment,
            )
            template_id = assignment["coverTemplateId"]
            seed_summary = assignment["stableSeed"]
        else:
            self.cover_template_assignments[assignment_key] = assignment
            callback = self.cover_template_assignment_callback
            if callable(callback):
                callback(assignment_key, dict(assignment))
        config.update({
            "cover_template_id": template_id,
            "cover_template_name": COVER_TEMPLATE_NAMES[template_id],
            "cover_template_seed": seed_summary,
            "cover_template_assignment_key": assignment_key,
            "cover_template_output_index": logical_index,
        })
        return config

    def ffprobe_path(self):
        configured = Path(self.ffmpeg_path.get().strip()) if self.ffmpeg_path.get().strip() else None
        if configured and configured.is_file():
            sibling = configured.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            if sibling.is_file():
                return sibling
        discovered = shutil.which("ffprobe")
        return Path(discovered) if discovered else None

    def probe_audio_duration(self, audio_path):
        audio = Path(audio_path)
        if not audio.is_file():
            raise ValueError("音频文件不存在，请重新选择。")
        if audio.suffix.lower() not in AUDIO_EXTS:
            raise ValueError("音频格式不支持，请选择 MP3、WAV、M4A、AAC、FLAC 或 OGG。")
        ffprobe = self.ffprobe_path()
        if not ffprobe:
            raise ValueError("找不到 ffprobe.exe，请确认 FFmpeg 路径完整。")
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            completed = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=duration",
                    "-of",
                    "json",
                    str(audio),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                startupinfo=startupinfo,
            )
        except Exception as exc:
            raise ValueError(f"音频读取失败：{exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            raise ValueError(f"音频读取失败：{detail[-1] if detail else 'ffprobe 执行失败'}")
        try:
            data = json.loads(completed.stdout or "{}")
            candidates = []
            format_duration = data.get("format", {}).get("duration")
            if format_duration not in (None, "N/A"):
                candidates.append(float(format_duration))
            for stream in data.get("streams", []):
                value = stream.get("duration")
                if value not in (None, "N/A"):
                    candidates.append(float(value))
            duration = max(value for value in candidates if value > 0)
        except Exception as exc:
            raise ValueError("音频没有可识别的有效时长。") from exc
        return duration

    def set_reference_audio(self, audio_path, log_result=True):
        duration = self.probe_audio_duration(audio_path)
        self.reference_audio_var.set(str(Path(audio_path)))
        self.reference_audio_duration = duration
        self.probed_audio_path = str(Path(audio_path).resolve())
        self.on_timing_changed()
        if log_result:
            self._log(f"参考音频时长：{duration:.3f} 秒")

    def choose_reference_audio(self):
        file_path = filedialog.askopenfilename(
            title="选择参考音频",
            filetypes=[
                ("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"),
                ("All files", "*.*"),
            ],
        )
        if not file_path:
            return
        try:
            self.set_reference_audio(file_path)
        except ValueError as exc:
            messagebox.showwarning("参考音频不可用", str(exc))

    def ensure_reference_audio(self):
        audio_path = self.reference_audio_var.get().strip()
        if not audio_path:
            raise ValueError("请选择参考音频。")
        resolved = str(Path(audio_path).resolve())
        if self.reference_audio_duration is None or self.probed_audio_path != resolved:
            self.set_reference_audio(audio_path, log_result=False)
        return self.reference_audio_duration

    def on_reference_audio_path_edited(self):
        value = self.reference_audio_var.get().strip()
        if not value:
            self.reference_audio_duration = None
            self.probed_audio_path = None
        else:
            try:
                resolved = str(Path(value).resolve())
            except OSError:
                resolved = value
            if self.probed_audio_path != resolved:
                self.reference_audio_duration = None
        self.on_timing_changed()

    def calculate_timing(self, require_audio=True, task=None):
        try:
            end_reserve = float(self.end_reserve_var.get())
        except (ValueError, tk.TclError) as exc:
            raise ValueError("结尾预留必须填写有效数字。") from exc
        if self.use_ai_var.get():
            try:
                ai_duration = float(self.ai_duration_var.get())
            except (ValueError, tk.TclError) as exc:
                raise ValueError("AI 时长必须填写有效数字。") from exc
            if ai_duration <= 0:
                raise ValueError("AI 时长必须大于 0 秒。")
        else:
            ai_duration = 0.0
        if end_reserve < 0:
            raise ValueError("结尾预留不能小于 0 秒。")

        task = task or self.selected_language_task(require_enabled=False)
        if task is not None:
            audio_duration = float(task.duration)
            audio_path = task.audio_path
            if task.error:
                raise ValueError(task.error)
        elif self.reference_audio_duration is not None:
            audio_duration = float(self.reference_audio_duration)
            audio_path = self.reference_audio_var.get().strip()
        else:
            if require_audio:
                raise ValueError("请选择多语言音频文件夹并扫描音频。")
            return None
        if audio_duration <= ai_duration + end_reserve:
            minimum_label = "AI 时长与结尾预留之和" if self.use_ai_var.get() else "结尾预留"
            raise ValueError(f"音频只有 {audio_duration:.3f} 秒，必须大于{minimum_label} ({ai_duration + end_reserve:.3f} 秒)。")
        body_duration = audio_duration - end_reserve
        old_duration = body_duration - ai_duration
        return {
            "reference_audio": audio_path,
            "audio_duration": audio_duration,
            "ai_duration": ai_duration,
            "end_reserve": end_reserve,
            "body_duration": body_duration,
            "old_duration": old_duration,
        }

    def apply_timing_to_segments(self, require_audio=True, task=None):
        timing = self.calculate_timing(require_audio=require_audio, task=task)
        if timing is None:
            return None
        duration_by_name = {
            STANDARD_SEGMENT_NAMES[0]: timing["ai_duration"],
            STANDARD_SEGMENT_NAMES[1]: timing["old_duration"],
        }
        for segment in self.segments:
            if segment.name in duration_by_name:
                segment.duration = duration_by_name[segment.name]
        return timing
    def on_timing_changed(self):
        try:
            ai_duration = float(self.ai_duration_var.get())
            if ai_duration > 0:
                for segment in self.segments:
                    if segment.name == STANDARD_SEGMENT_NAMES[0]:
                        segment.duration = ai_duration
        except (ValueError, tk.TclError):
            pass
        try:
            self.apply_timing_to_segments(require_audio=False)
        except ValueError:
            pass
        if hasattr(self, "language_tree"):
            self.refresh_language_tasks()
        if hasattr(self, "segment_tree"):
            self.refresh_segments(scan_counts=False)
        elif hasattr(self, "timing_summary_label"):
            self.update_stats()

    def choose_ffmpeg(self):
        file_path = filedialog.askopenfilename(
            title="选择 ffmpeg.exe",
            initialdir=self.picker_initial_dir("ffmpeg", self.ffmpeg_path.get(), is_file=True),
            filetypes=[("FFmpeg", "ffmpeg.exe"), ("Executable", "*.exe"), ("All files", "*.*")],
        )
        if file_path:
            self.remember_picker_dir("ffmpeg", file_path, is_file=True)
            self.ffmpeg_path.set(file_path)
            self._log(f"已选择 FFmpeg：{file_path}")

    def preview_all_cover_templates(self):
        try:
            self.apply_automatic_output_dir()
            if not self.ffmpeg_path.get() or not Path(self.ffmpeg_path.get()).exists():
                raise ValueError("找不到 FFmpeg，请选择 ffmpeg.exe。")
            if not self.output_dir.get():
                raise ValueError("请选择输出目录。")
            self.ensure_current_board_loaded()
            board = self.board_var.get().strip()
            config = self.cover_config_for_board(board)
            if not config.get("enabled"):
                raise ValueError("请先开启自动首帧封面。")
            picks = self.build_combinations(board, self.segments, 1, {}, {})[0]
            output_dir = Path(self.output_dir.get()) / "_preview"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"cover_templates_{self.current_material_id()}_{self.current_cover_language_code()}_{now_stamp()}.png"
            self.save_cover_settings()
        except (ValueError, OSError) as exc:
            messagebox.showwarning("无法生成封面预览", str(exc))
            return
        self.progress_label.config(text="正在生成旧版与新版封面预览")
        self.status_var.set("正在生成全部 10 套封面预览...")
        self._set_running(True)
        self.worker = threading.Thread(
            target=self.generate_cover_template_montage_worker,
            args=(picks[0], config, output_path, self.ffmpeg_path.get()),
            daemon=True,
        )
        self.worker.start()

    def generate_cover_template_montage_worker(self, ai_pick, config, output_path, ffmpeg_path):
        temp_dir = Path(tempfile.mkdtemp(prefix="board_cover_templates_"))
        try:
            self.composer = VideoComposer(ffmpeg_path, 540, 960, 24, 28)
            normalized = temp_dir / "preview.mp4"
            base_frame = temp_dir / "base_frame.png"
            self.composer.normalize_segment(ai_pick["file"], min(float(ai_pick["duration"]), 0.25), normalized)
            self.composer.extract_first_frame(normalized, base_frame)
            images = []
            for template_id in ALL_COVER_TEMPLATE_IDS:
                rendered = temp_dir / f"{template_id}.png"
                item_config = dict(config)
                item_config["cover_template_id"] = template_id
                render_cover_frame(base_frame, item_config["logo_path"], item_config, rendered)
                with Image.open(rendered) as source:
                    images.append(source.convert("RGB"))
            cell_width, cell_height = images[0].size
            montage = Image.new("RGB", (cell_width * 4, cell_height * 3), "#E9E3D9")
            for index, image in enumerate(images):
                montage.paste(image, ((index % 4) * cell_width, (index // 4) * cell_height))
            montage.save(output_path, "PNG")
            self.msg_queue.put(("cover_preview_done", str(output_path)))
        except Exception as exc:
            self.msg_queue.put(("cover_preview_error", str(exc)))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.composer = None

    def preview_cover_frame(self):
        try:
            self.apply_automatic_output_dir()
            if not self.ffmpeg_path.get() or not Path(self.ffmpeg_path.get()).exists():
                raise ValueError("找不到 FFmpeg，请选择 ffmpeg.exe。")
            if not self.output_dir.get():
                raise ValueError("请选择输出目录。")
            self.ensure_current_board_loaded()
            board = self.board_var.get().strip()
            config = self.cover_config_for_board(board)
            if not config.get("enabled"):
                raise ValueError("请先开启自动首帧封面。")
            picks = self.build_combinations(board, self.segments, 1, {}, {})[0]
            output_dir = Path(self.output_dir.get()) / "_preview"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"cover_preview_{now_stamp()}.png"
            self.save_cover_settings()
        except (ValueError, OSError) as exc:
            messagebox.showwarning("无法生成首帧预览", str(exc))
            return

        self.progress_label.config(text="正在生成首帧预览")
        self.status_var.set("正在生成首帧封面预览...")
        self._set_running(True)
        self.worker = threading.Thread(
            target=self.generate_cover_preview_worker,
            args=(picks[0], config, output_path, self.ffmpeg_path.get()),
            daemon=True,
        )
        self.worker.start()

    def generate_cover_preview_worker(self, ai_pick, config, output_path, ffmpeg_path):
        temp_dir = tempfile.mkdtemp(prefix="board_cover_preview_")
        try:
            self.composer = VideoComposer(ffmpeg_path, 540, 960, 24, 28)
            normalized = Path(temp_dir) / "ai_preview.mp4"
            base_frame = Path(temp_dir) / "base_frame.png"
            self.composer.normalize_segment(ai_pick["file"], min(float(ai_pick["duration"]), 0.25), normalized)
            self.composer.extract_first_frame(normalized, base_frame)
            render_cover_frame(base_frame, config["logo_path"], config, output_path)
            self.msg_queue.put(("cover_preview_done", str(output_path)))
        except Exception as exc:
            self.msg_queue.put(("cover_preview_error", str(exc)))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.composer = None

    def show_cover_thumbnail(self, image_path):
        try:
            with Image.open(image_path) as source:
                preview = source.convert("RGB")
                preview.thumbnail((84, 149), Image.Resampling.LANCZOS)
            self.cover_preview_photo = ImageTk.PhotoImage(preview)
            self.cover_thumbnail_label.configure(image=self.cover_preview_photo, text="", width=84, height=149)
        except Exception as exc:
            self._log(f"首帧缩略图显示失败：{exc}")

    def scan_boards(self):
        self.commit_current_cover_config()
        root = Path(self.root_dir.get())
        if not root.exists():
            self.board_combo["values"] = []
            self.board_var.set("")
            self.segments = []
            self.active_cover_board = None
            self.refresh_segments()
            self._log("素材根目录不存在。")
            return
        boards = sorted([p.name for p in root.iterdir() if p.is_dir()], key=str.lower)
        self.board_combo["values"] = boards
        if boards and self.board_var.get() not in boards:
            self.board_var.set(boards[0])
        elif not boards:
            self.board_var.set("")
        self.scan_segments()
        self.load_cover_config_into_vars(self.board_var.get().strip())
        self._log(f"扫描到 {len(boards)} 个板材类型。")

    def create_board(self):
        root = Path(self.root_dir.get())
        if not root.exists():
            messagebox.showwarning("提示", "请先选择有效的素材根目录。")
            return
        name = simpledialog.askstring("创建板材类型", "请输入板材类型文件夹名称：")
        if not name:
            return
        target = root / name.strip()
        if target.exists():
            messagebox.showinfo("提示", "这个板材类型已经存在。")
            return
        target.mkdir(parents=True, exist_ok=True)
        for segment_name in self.active_segment_names():
            (target / segment_name).mkdir()
        self.board_var.set(target.name)
        self.scan_boards()
        self.save_cover_settings()

    def delete_board(self):
        board_path = self.current_board_path()
        if not board_path:
            return
        if not messagebox.askyesno("确认删除", f"确定删除板材类型文件夹？\\n{board_path}"):
            return
        shutil.rmtree(board_path)
        self.cover_configs.pop(self.board_var.get(), None)
        self.active_cover_board = None
        self.board_var.set("")
        self.scan_boards()
        self.save_cover_settings()

    def current_board_path(self):
        if not self.root_dir.get() or not self.board_var.get():
            return None
        path = Path(self.root_dir.get()) / self.board_var.get()
        return path if path.exists() else None

    def ensure_current_board_loaded(self):
        ai_value = self.ai_folder_var.get().strip()
        old_value = self.old_folder_var.get().strip()
        if self.use_ai_var.get() and (not ai_value or not Path(ai_value).is_dir()):
            raise ValueError("请选择有效的 AI 素材文件夹。")
        if not old_value or not Path(old_value).is_dir():
            raise ValueError("请选择有效的老素材文件夹。")
        source_folder = Path(ai_value) if self.use_ai_var.get() else Path(old_value)
        old_folder = Path(old_value)
        if not old_folder.is_dir():
            raise ValueError("请选择有效的老素材文件夹。")
        self.board_var.set(self.material_hint())
        if not self.segments:
            self.scan_segments()
        return source_folder.parent

    def scan_segments(self):
        previous_durations = {seg.name: seg.duration for seg in self.segments}
        try:
            ai_duration = float(self.ai_duration_var.get())
            if ai_duration <= 0:
                raise ValueError
        except (ValueError, tk.TclError):
            ai_duration = DEFAULT_AI_DURATION
        folders = {
            STANDARD_SEGMENT_NAMES[0]: self.ai_folder_var.get().strip(),
            STANDARD_SEGMENT_NAMES[1]: self.old_folder_var.get().strip(),
        }
        defaults = {
            STANDARD_SEGMENT_NAMES[0]: ai_duration,
            STANDARD_SEGMENT_NAMES[1]: previous_durations.get(STANDARD_SEGMENT_NAMES[1], DEFAULT_DURATION),
        }
        self.segments = [
            Segment(
                name,
                folders[name],
                previous_durations.get(name, defaults[name]),
                file_whitelist=tuple(self.segment_file_whitelists.get(name, ())),
            )
            for name in self.active_segment_names()
        ]
        try:
            self.apply_timing_to_segments(require_audio=False)
        except ValueError:
            pass
        self.refresh_segments(scan_counts=True)

    def selected_segment_index(self):
        selection = self.segment_tree.selection()
        if not selection:
            return None
        return self.segment_tree.index(selection[0])

    def on_segment_selected(self, _event):
        idx = self.selected_segment_index()
        if idx is not None:
            self.duration_edit_var.set(str(self.segments[idx].duration))

    def apply_duration(self):
        idx = self.selected_segment_index()
        if idx is None:
            return
        try:
            value = float(self.duration_edit_var.get())
            if value <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "时长必须是大于 0 的数字。")
            return
        self.segments[idx].duration = value
        self.refresh_segments(scan_counts=True)

    def refresh_segments(self, scan_counts=False):
        for item in self.segment_tree.get_children():
            self.segment_tree.delete(item)
        for idx, seg in enumerate(self.segments):
            if scan_counts:
                files = seg.files()
                usable = self.usable_files_for_segment(seg, idx, files)
                seg.count = len(usable)
                seg.warnings = self.segment_warnings(seg, idx, files, usable, all_regular_files(seg.path))
            if seg.name == STANDARD_SEGMENT_NAMES[0]:
                duration_text = f"{seg.duration:g}"
            else:
                try:
                    timing = self.calculate_timing(require_audio=False)
                    duration_text = f"自动 {timing['old_duration']:.3f}" if timing else "等待音频"
                except ValueError:
                    duration_text = "设置有误"
            display_name = "AI素材" if seg.name == STANDARD_SEGMENT_NAMES[0] else "老素材"
            self.segment_tree.insert(
                "",
                END,
                values=(idx + 1, display_name, duration_text, seg.count, "；".join(seg.warnings)),
            )
        self.update_stats()

    def usable_files_for_segment(self, seg, idx, files):
        return [p for p in files if p.suffix.lower() in VIDEO_EXTS]

    def segment_warnings(self, seg, idx, files, usable, all_files):
        warnings = []
        if not seg.path or not Path(seg.path).is_dir():
            return ["请选择有效文件夹"]
        unsupported = [p.name for p in all_files if p.suffix.lower() not in SUPPORTED_EXTS]
        if unsupported:
            warnings.append(f"不支持格式 {len(unsupported)} 个")
        images = [p.name for p in files if p.suffix.lower() in IMAGE_EXTS]
        if images:
            warnings.append(f"发现图片 {len(images)} 个，仅支持视频")
        if not usable:
            warnings.append("无可用视频素材")
        if len(usable) == 1:
            warnings.append("只有 1 个素材，重复率高")
        return warnings

    def update_stats(self):
        counts = [max(seg.count, 0) for seg in self.segments]
        segment_count = len(self.segments)
        material_count = sum(counts)
        total_text = "-"
        warning = ""
        total_requested = sum(max(task.count, 0) for task in self.language_tasks)
        active_languages = sum(1 for task in self.language_tasks if task.count > 0)
        if counts:
            if any(count == 0 for count in counts):
                total_text = "0"
                warning = "存在空素材文件夹，暂时无法生成。"
            else:
                total = math.prod(counts)
                if self.old_sequential_var.get():
                    old_segment = self.find_segment(self.segments, STANDARD_SEGMENT_NAMES[1])
                    old_count = old_segment.count if old_segment else 0
                    total_text = f"{old_count:,}"
                    if total_requested > old_count:
                        warning = f"顺序模式需要 {total_requested} 个老素材，目前只有 {old_count} 个，还缺 {total_requested - old_count} 个。"
                else:
                    total_text = f"{total:,}"
                    if total_requested >= total:
                        warning = "生成数量已达到或超过理论组合数，重复率会升高。"
                    elif total_requested >= total * 0.8:
                        warning = "生成数量接近理论组合数，建议补充素材。"
        self.combo_value_label.stat_title_label.config(
            text="顺序可生成数" if self.old_sequential_var.get() else "理论组合数"
        )
        self.combo_value_label.config(text=total_text)
        self.segment_count_label.config(text=str(segment_count))
        self.material_count_label.config(text=str(material_count))
        selected = self.selected_language_task(require_enabled=False)
        try:
            timing = self.calculate_timing(require_audio=False, task=selected) if selected else None
            if timing and selected:
                selected_label = f"{selected.name}（{selected.note}）" if selected.note else selected.name
                timing_text = (
                    f"语言 {active_languages} 种  |  总生成 {total_requested} 条  |  当前：{selected_label}\n"
                    f"音频 {timing['audio_duration']:.3f}s  |  主体 {timing['body_duration']:.3f}s  |  "
                    + (f"AI {timing['ai_duration']:.3f}s  |  " if self.use_ai_var.get() else "仅老素材  |  ")
                    + f"老素材 {timing['old_duration']:.3f}s"
                )
            else:
                timing_text = "请选择多语言音频文件夹；每个音频可单独设置生成数量。"
        except ValueError as exc:
            timing_text = f"当前语言时长设置：{exc}"
        self.timing_summary_label.config(text=timing_text)
        self.stats_warning_label.config(text=warning)
    def get_settings(self):
        try:
            count = int(self.count_var.get())
            width = int(self.width_var.get())
            height = int(self.height_var.get())
            fps = int(self.fps_var.get())
            crf = int(self.crf_var.get())
            if count < 0 or width <= 0 or height <= 0 or fps <= 0 or crf < 0:
                raise ValueError
        except Exception:
            raise ValueError("默认语言数量必须大于或等于 0，宽高、帧率和 CRF 必须填写有效数字。")
        return count, width, height, fps, crf

    def sort_old_files(self, paths, mode=None):
        selected_mode = mode or self.old_sort_mode_var.get()
        if selected_mode == "下载时间":
            def creation_key(path):
                try:
                    created = path.stat().st_ctime_ns
                except OSError:
                    created = 0
                return created, natural_sort_key(path.name)
            return sorted(paths, key=creation_key)
        return sorted(paths, key=lambda path: natural_sort_key(path.name))

    def selected_boards(self):
        return [self.material_hint()]

    def validate_before_generate(self):
        self.apply_automatic_output_dir()
        self.refresh_audio_batches(scan_tasks=True, log_result=False)
        default_count, width, height, fps, crf = self.get_settings()
        if not self.ffmpeg_path.get() or not Path(self.ffmpeg_path.get()).exists():
            raise ValueError("找不到 FFmpeg，请选择 ffmpeg.exe。")
        if not self.output_dir.get():
            raise ValueError("请选择输出目录。")
        Path(self.output_dir.get()).mkdir(parents=True, exist_ok=True)
        self.ensure_current_board_loaded()
        self.refresh_segments(scan_counts=True)
        configured_tasks = self.validated_language_task_data()
        tasks = self.remaining_language_task_data(configured_tasks)
        if not tasks:
            raise ValueError("第4步的目标成片均已有成功记录，本次无需重复生成。")
        self.validated_language_tasks = tasks
        self.validated_timing = dict(tasks[0]["timing"])
        self.apply_timing_to_segments(require_audio=True, task=next(task for task in self.language_tasks if task.count > 0))
        boards = self.selected_boards()
        segments = self.load_segments_for_board(boards[0], tasks[0]["timing"])
        for seg in segments:
            if seg.duration <= 0:
                raise ValueError("段落时长必须大于 0。")
        self.build_combinations(boards[0], segments, 1, {}, {})
        if self.old_sequential_var.get():
            old_segment = self.find_segment(segments, STANDARD_SEGMENT_NAMES[1])
            old_files = self.sort_old_files([path for path in old_segment.files() if path.suffix.lower() in VIDEO_EXTS])
            requested = sum(task["count"] for task in tasks)
            if requested > len(old_files):
                raise ValueError(
                    f"顺序模式共需要 {requested} 个老素材，目前只有 {len(old_files)} 个，还缺 {requested - len(old_files)} 个。"
                )
        self.validated_cover_configs = {
            boards[0]: {
                code: self.cover_config_for_language(boards[0], code)
                for code in {task["language_code"] for task in tasks}
            }
        }
        self.save_cover_settings()
        return default_count, width, height, fps, crf, boards, tasks

    def load_segments_for_board(self, _board_name, timing=None):
        duration_by_name = {seg.name: seg.duration for seg in self.segments}
        active_timing = timing or self.validated_timing
        if active_timing:
            duration_by_name.update({
                STANDARD_SEGMENT_NAMES[0]: active_timing["ai_duration"],
                STANDARD_SEGMENT_NAMES[1]: active_timing["old_duration"],
            })
        folders = {
            STANDARD_SEGMENT_NAMES[0]: self.ai_folder_var.get().strip(),
            STANDARD_SEGMENT_NAMES[1]: self.old_folder_var.get().strip(),
        }
        segments = []
        for name in self.active_segment_names():
            whitelist = tuple(self.segment_file_whitelists.get(name, ()))
            if (
                self.segment_file_whitelist_required
                and name == STANDARD_SEGMENT_NAMES[1]
                and not whitelist
            ):
                raise ValueError(
                    "STITCH_SOURCE_MANIFEST_MISSING: 当前远程任务没有正式第1/2步素材清单，"
                    "已阻止回退扫描原视频和中间文件。"
                )
            segments.append(
                Segment(
                    name,
                    folders[name],
                    duration_by_name.get(name, DEFAULT_DURATION),
                    file_whitelist=whitelist,
                )
            )
        return segments
    def health_check(self):
        try:
            self.ensure_current_board_loaded()
        except ValueError as exc:
            messagebox.showwarning("无法体检", str(exc))
            return
        self.refresh_segments(scan_counts=True)
        problems = []
        try:
            language_data = self.validated_language_task_data()
            if self.old_sequential_var.get():
                old_segment = self.find_segment(self.segments, STANDARD_SEGMENT_NAMES[1])
                old_count = len([path for path in old_segment.files() if path.suffix.lower() in VIDEO_EXTS]) if old_segment else 0
                requested = sum(task["count"] for task in language_data)
                if requested > old_count:
                    problems.append(f"顺序模式: 需要 {requested} 个老素材，目前只有 {old_count} 个，还缺 {requested - old_count} 个")
        except ValueError as exc:
            problems.append(f"多语言音频: {exc}")
        try:
            self.cover_config_for_board(self.board_var.get().strip())
        except ValueError as exc:
            problems.append(f"首帧封面: {exc}")
        for idx, seg in enumerate(self.segments):
            files = self.usable_files_for_segment(seg, idx, seg.files())
            problems.extend([f"{seg.name}: {item}" for item in seg.warnings])
            ffmpeg = self.ffmpeg_path.get()
            if ffmpeg and Path(ffmpeg).exists():
                for file_path in files:
                    ok, error = self.probe_file(ffmpeg, file_path)
                    if not ok:
                        problems.append(f"{seg.name}: 文件可能损坏：{file_path.name} - {error}")
        if problems:
            self.show_text_window("素材体检结果", "\\n".join(problems))
            self._log(f"素材体检完成，发现 {len(problems)} 个问题。")
        else:
            messagebox.showinfo("素材体检", "未发现明显问题。")
            self._log("素材体检完成，未发现明显问题。")

    def probe_file(self, ffmpeg, file_path):
        ext = file_path.suffix.lower()
        args = [ffmpeg, "-v", "error"]
        if ext in VIDEO_EXTS:
            args += ["-t", "1", "-i", str(file_path), "-f", "null", "-"]
        else:
            args += ["-i", str(file_path), "-frames:v", "1", "-f", "null", "-"]
        try:
            completed = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
        except Exception as exc:
            return False, str(exc)
        err = (completed.stderr or "").strip().splitlines()
        return completed.returncode == 0, err[-1] if err else ""

    def preview_combinations(self):
        try:
            _default, _width, _height, _fps, _crf, boards, tasks = self.validate_before_generate()
            history = self.load_history()
            lines = []
            last_by_segment = {}
            old_cursor = 0
            for board in boards:
                base_segments = self.load_segments_for_board(board, tasks[0]["timing"])
                sequential_old_files = None
                if self.old_sequential_var.get():
                    old_segment = self.find_segment(base_segments, STANDARD_SEGMENT_NAMES[1])
                    sequential_old_files = self.sort_old_files([path for path in old_segment.files() if path.suffix.lower() in VIDEO_EXTS])
                for task in tasks:
                    segments = self.load_segments_for_board(board, task["timing"])
                    combos = self.build_combinations(
                        board,
                        segments,
                        task["count"],
                        history,
                        last_by_segment,
                        sequential_old_files=sequential_old_files,
                        old_start_index=old_cursor,
                    )
                    for index, picks in enumerate(combos, 1):
                        names = " | ".join(f"{pick['segment']}={Path(pick['file']).name}" for pick in picks)
                        label = f"{task['name']}（{task['note']}）" if task.get("note") else task["name"]
                        label += f" [{LANGUAGES.get(task['language_code'], task['language_code'])}]"
                        lines.append(f"{label} #{index}: {names} | 老素材时长={task['timing']['old_duration']:.3f}s")
                    if sequential_old_files is not None:
                        old_cursor += task["count"]
        except ValueError as exc:
            messagebox.showwarning("提示", str(exc))
            return
        self.show_text_window("组合清单预览", "\n".join(lines) if lines else "没有可生成的组合。")
        self._log(f"已预览 {len(lines)} 条多语言组合。")
    def build_combinations(
        self,
        board_name,
        segments,
        count,
        history,
        last_by_segment,
        sequential_old_files=None,
        old_start_index=0,
        ai_usage=None,
    ):
        segment_by_name = {seg.name: seg for seg in segments}
        files_by_segment = []
        for segment_name in self.active_segment_names():
            seg = segment_by_name.get(segment_name)
            if not seg or not Path(seg.path).is_dir():
                raise ValueError(f"{board_name} / {segment_name} 文件夹不存在。")
            files = [path for path in seg.files() if path.suffix.lower() in VIDEO_EXTS]
            if not files:
                raise ValueError(f"{board_name} / {seg.name} 没有可用视频素材。")
            files_by_segment.append((seg, files))

        if sequential_old_files is not None:
            if old_start_index + count > len(sequential_old_files):
                raise ValueError("按顺序使用的老素材数量不足。")
            pair_by_name = {segment.name: (segment, files) for segment, files in files_by_segment}
            old_seg, _old_files = pair_by_name[STANDARD_SEGMENT_NAMES[1]]
            ai_pair = pair_by_name.get(STANDARD_SEGMENT_NAMES[0])
            combos = []
            for offset in range(count):
                old_file = sequential_old_files[old_start_index + offset]
                picks = []
                if ai_pair:
                    ai_seg, ai_files = ai_pair
                    previous = last_by_segment.get((board_name, ai_seg.name))
                    if ai_usage is not None:
                        ai_file = self.choose_global_ai_file(ai_seg, ai_files, ai_usage, previous)
                    else:
                        choices = [path for path in ai_files if str(path) != previous] if previous and len(ai_files) > 1 else ai_files
                        ai_file = random.choice(choices or ai_files)
                    picks.append({"segment": ai_seg.name, "file": str(ai_file), "duration": ai_seg.duration})
                    last_by_segment[(board_name, ai_seg.name)] = str(ai_file)
                picks.append({
                    "segment": old_seg.name,
                    "file": str(old_file),
                    "duration": old_seg.duration,
                    "sequence_index": old_start_index + offset + 1,
                })
                combos.append(picks)
                last_by_segment[(board_name, old_seg.name)] = str(old_file)
            return combos

        if ai_usage is not None and self.use_ai_var.get():
            combos = []
            for _offset in range(count):
                picks = []
                for seg, files in files_by_segment:
                    previous = last_by_segment.get((board_name, seg.name))
                    if seg.name == STANDARD_SEGMENT_NAMES[0]:
                        file_path = self.choose_global_ai_file(seg, files, ai_usage, previous)
                    else:
                        choices = [path for path in files if str(path) != previous] if previous and len(files) > 1 else files
                        file_path = random.choice(choices or files)
                    picks.append({"segment": seg.name, "file": str(file_path), "duration": seg.duration})
                    last_by_segment[(board_name, seg.name)] = str(file_path)
                combos.append(picks)
            return combos

        combos = []
        used_in_batch = set()
        attempts_limit = max(count * 80, 200)
        attempts = 0
        while len(combos) < count and attempts < attempts_limit:
            attempts += 1
            picks = []
            for seg, files in files_by_segment:
                previous = last_by_segment.get((board_name, seg.name))
                choices = files
                if previous and len(files) > 1:
                    choices = [path for path in files if str(path) != previous] or files
                file_path = random.choice(choices)
                picks.append({"segment": seg.name, "file": str(file_path), "duration": seg.duration})
            key = combination_key(board_name, [(pick["segment"], pick["file"]) for pick in picks])
            if key in used_in_batch:
                continue
            if key in history and attempts < attempts_limit // 2:
                continue
            combos.append(picks)
            used_in_batch.add(key)
            for pick in picks:
                last_by_segment[(board_name, pick["segment"])] = pick["file"]
        while len(combos) < count:
            picks = []
            for seg, files in files_by_segment:
                file_path = random.choice(files)
                picks.append({"segment": seg.name, "file": str(file_path), "duration": seg.duration})
            combos.append(picks)
        return combos
    def preview_current_combo(self):
        try:
            if not self.ffmpeg_path.get() or not Path(self.ffmpeg_path.get()).exists():
                raise ValueError("找不到 FFmpeg，请选择 ffmpeg.exe。")
            if not self.output_dir.get():
                raise ValueError("请选择输出目录。")
            output_dir = Path(self.output_dir.get())
            output_dir.mkdir(parents=True, exist_ok=True)
            _default, _width, _height, _fps, _crf, boards, tasks = self.validate_before_generate()
            board = boards[0]
            selected = self.selected_language_task(require_enabled=True)
            if not selected:
                raise ValueError("没有可预览的语言任务。")
            task = next((item for item in tasks if item["audio_path"] == selected.audio_path), tasks[0])
            segments = self.load_segments_for_board(board, task["timing"])
            history = self.load_history()
            sequential_old_files = None
            old_offset = 0
            if self.old_sequential_var.get():
                old_segment = self.find_segment(segments, STANDARD_SEGMENT_NAMES[1])
                sequential_old_files = self.sort_old_files(
                    [path for path in old_segment.files() if path.suffix.lower() in VIDEO_EXTS]
                )
                for item in tasks:
                    if item["audio_path"] == task["audio_path"]:
                        break
                    old_offset += item["count"]
            picks = self.build_combinations(
                board,
                segments,
                1,
                history,
                {},
                sequential_old_files=sequential_old_files,
                old_start_index=old_offset,
            )[0]
            cover_config = self.cover_config_for_language(board, task["language_code"])
            self.save_cover_settings()
        except (ValueError, OSError) as exc:
            messagebox.showwarning("无法生成预览", str(exc))
            return

        preview_dir = output_dir / "_preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = preview_dir / f"preview_{safe_name(task['name'])}_{now_stamp()}.mp4"
        self.progress["maximum"] = 1
        self.progress["value"] = 0
        self.progress_label.config(text="正在生成预览")
        self.status_var.set(f"正在生成 {task['name']} 低清预览...")
        self._set_running(True)
        self.worker = threading.Thread(
            target=self.generate_preview_video,
            args=(picks, preview_path, self.ffmpeg_path.get(), cover_config),
            daemon=True,
        )
        self.worker.start()
    def generate_preview_video(self, picks, preview_path, ffmpeg_path, cover_config):
        temp_dir = tempfile.mkdtemp(prefix="board_video_preview_")
        try:
            self.composer = VideoComposer(ffmpeg_path, 540, 960, 24, 28)
            self.composer.compose(picks, preview_path, temp_dir, cover_config)
            self.msg_queue.put(("preview_done", str(preview_path)))
        except Exception as exc:
            self.msg_queue.put(("preview_error", str(exc)))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.composer = None

    def open_preview_video(self):
        preview_path = Path(self.latest_preview_path) if self.latest_preview_path else None
        if not preview_path or not preview_path.exists():
            messagebox.showinfo("提示", "还没有可打开的预览视频。")
            return
        try:
            if os.name == "nt":
                os.startfile(str(preview_path))
            else:
                subprocess.Popen(["xdg-open", str(preview_path)])
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法打开预览视频：{exc}")

    def start_generate(self):
        try:
            _default, width, height, fps, crf, boards, tasks = self.validate_before_generate()
        except ValueError as exc:
            messagebox.showwarning("提示", str(exc))
            return
        total = sum(task["count"] for task in tasks) * len(boards)
        self.refresh_segments(scan_counts=True)
        self.progress["value"] = 0
        self.progress["maximum"] = total
        self._set_running(True)
        self.worker = threading.Thread(
            target=self.generate_worker,
            args=(
                width,
                height,
                fps,
                crf,
                boards,
                list(tasks),
                dict(self.validated_cover_configs),
                bool(self.old_sequential_var.get()),
                self.old_sort_mode_var.get(),
            ),
            daemon=True,
        )
        self.worker.start()
    def cancel_generate(self):
        if self.composer:
            self.composer.cancel()
            self._queue_log("正在取消任务...")

    def generate_worker(self, width, height, fps, crf, boards, tasks, cover_configs, old_sequential, old_sort_mode):
        records = []
        history = self.load_history()
        ai_usage = self.load_ai_usage_history() if self.use_ai_var.get() else {}
        last_by_segment = {}
        output_dir = Path(self.output_dir.get())
        prefix = self.prefix_var.get().strip() or "video"
        total = sum(task["count"] for task in tasks) * len(boards)
        current = 0
        try:
            self.composer = VideoComposer(self.ffmpeg_path.get(), width, height, fps, crf)
            for board in boards:
                if self.composer.cancel_requested:
                    break
                old_cursor = 0
                base_segments = self.load_segments_for_board(board, tasks[0]["timing"])
                sequential_old_files = None
                if old_sequential:
                    old_segment = self.find_segment(base_segments, STANDARD_SEGMENT_NAMES[1])
                    sequential_old_files = self.sort_old_files(
                        [path for path in old_segment.files() if path.suffix.lower() in VIDEO_EXTS],
                        mode=old_sort_mode,
                    )
                for task in tasks:
                    if self.composer.cancel_requested:
                        break
                    cover_config = cover_configs[board][task["language_code"]]
                    segments = self.load_segments_for_board(board, task["timing"])
                    try:
                        combos = self.build_combinations(
                            board,
                            segments,
                            task["count"],
                            history,
                            last_by_segment,
                            sequential_old_files=sequential_old_files,
                            old_start_index=old_cursor,
                            ai_usage=ai_usage if self.use_ai_var.get() else None,
                        )
                    except Exception as exc:
                        for index in range(task["count"]):
                            current += 1
                            records.append(self.record_for_failure(board, index + 1, [], str(exc), task))
                            self.msg_queue.put(("progress", (current, total)))
                        self._queue_log(f"{task['name']}: 无法生成组合 - {exc}")
                        if old_sequential:
                            old_cursor += task["count"]
                        continue
                    for output_index, picks in enumerate(combos):
                        current += 1
                        self.msg_queue.put(("progress", (current, total)))
                        output_prefix = f"{prefix}_{safe_name(task['name'])}"
                        output_path = self.next_output_path(output_dir, output_prefix, board)
                        assigned_cover_config = self.resolve_cover_template(
                            board, task, output_index, cover_config
                        )
                        self._queue_log(
                            f"开始生成：{output_path.name}；封面模板="
                            f"{assigned_cover_config['cover_template_name']}"
                            f"({assigned_cover_config['cover_template_id']})；"
                            f"稳定种子={assigned_cover_config['cover_template_seed']}"
                        )
                        record = self.record_for_start(board, output_path, picks, assigned_cover_config, task["timing"], task, old_sequential, old_sort_mode)
                        temp_dir = tempfile.mkdtemp(prefix="board_video_")
                        try:
                            leading_trim = 0 if self.use_ai_var.get() else 5
                            if leading_trim:
                                record["sourceLeadingFrameTrim"] = leading_trim
                                self._queue_log("Non-AI source: trimming first 5 frames before cover rendering")
                            self.composer.compose(
                                picks, output_path, temp_dir, assigned_cover_config,
                                leading_frame_trim=leading_trim,
                            )
                            key = combination_key(board, [(pick["segment"], pick["file"]) for pick in picks])
                            history[key] = {
                                "board": board,
                                "language": task["name"],
                                "created_at": datetime.now().isoformat(timespec="seconds"),
                                "items": [
                                    {
                                        "segment": pick["segment"],
                                        "file": Path(pick["file"]).name,
                                        "path": str(Path(pick["file"]).resolve()),
                                    }
                                    for pick in picks
                                ],
                            }
                            record["status"] = "success"
                            self._queue_log(f"生成成功：{output_path.name}")
                        except Exception as exc:
                            record["status"] = "failed"
                            record["error"] = str(exc)
                            self._queue_log(f"生成失败：{output_path.name} - {exc}")
                        finally:
                            records.append(record)
                            shutil.rmtree(temp_dir, ignore_errors=True)
                            self.msg_queue.put(("progress", (current, total)))
                    if old_sequential:
                        old_cursor += task["count"]
            self.save_history(history)
            if self.use_ai_var.get():
                self.save_ai_usage_history(ai_usage)
            self.write_records(records)
            self.msg_queue.put(("records", records))
            status = "任务已取消" if self.composer and self.composer.cancel_requested else "生成任务完成"
            self.msg_queue.put(("done", status))
        except Exception as exc:
            self.msg_queue.put(("done", f"任务异常结束：{exc}"))
        finally:
            self.composer = None
    def next_output_path(self, output_dir, prefix, _board_name=None):
        base_name = Path(prefix).stem.strip() or "video"
        base_name = safe_name(base_name)
        counter = 1
        while True:
            candidate = output_dir / f"{base_name}_{counter:04d}.mp4"
            if not candidate.exists():
                return candidate
            counter += 1

    def record_for_start(
        self,
        board,
        output_path,
        picks,
        cover_config=None,
        timing=None,
        language_task=None,
        old_sequential=False,
        old_sort_mode="下载时间",
    ):
        language_task = language_task or {}
        return {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "board": board,
            "language": language_task.get("name", ""),
            "language_note": language_task.get("note", ""),
            "language_code": language_task.get("language_code", "en"),
            "audio_path": language_task.get("audio_path", ""),
            "selection_mode": (
                "ai_global_unique_old_sequential" if self.use_ai_var.get() and old_sequential else
                "old_only_sequential" if old_sequential else
                "ai_global_unique_old_random" if self.use_ai_var.get() else "old_only_random"
            ),
            "old_sort_mode": old_sort_mode if old_sequential else "",
            "output": str(output_path),
            "status": "pending",
            "error": "",
            "items": [
                {
                    "segment": pick["segment"],
                    "file": pick["file"],
                    "duration": pick["duration"],
                    "sequence_index": pick.get("sequence_index"),
                }
                for pick in picks
            ],
            "cover": {
                "enabled": bool(cover_config and cover_config.get("enabled")),
                "preset": (cover_config or {}).get("preset", ""),
                "material_id": (cover_config or {}).get("material_id", ""),
                "material_label": (cover_config or {}).get("material_label", ""),
                "material_series": (cover_config or {}).get("material_series", ""),
                "language_code": (cover_config or {}).get("language_code", ""),
                "title": (cover_config or {}).get("title", ""),
                "subtitle": (cover_config or {}).get("subtitle", ""),
                "style": (cover_config or {}).get("style", ""),
                "logo_path": (cover_config or {}).get("logo_path", ""),
                "logo_brand": (cover_config or {}).get("logo_brand", ""),
                "vignette_enabled": bool((cover_config or {}).get("vignette_enabled", True)),
                "effect_strength": (cover_config or {}).get("effect_strength", "中等"),
                "template_mode": (cover_config or {}).get("template_mode", "classic_a"),
                "template_id": (cover_config or {}).get("cover_template_id", "classic_a"),
                "template_name": (cover_config or {}).get("cover_template_name", COVER_TEMPLATE_NAMES["classic_a"]),
                "stable_seed": (cover_config or {}).get("cover_template_seed", ""),
                "assignment_key": (cover_config or {}).get("cover_template_assignment_key", ""),
                "logical_output_index": (cover_config or {}).get("cover_template_output_index", 0),
            },
            "timing": dict(timing or {}),
        }

    def record_for_failure(self, board, index, picks, error, language_task=None):
        language_task = language_task or {}
        return {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "board": board,
            "language": language_task.get("name", ""),
            "language_note": language_task.get("note", ""),
            "language_code": language_task.get("language_code", "en"),
            "audio_path": language_task.get("audio_path", ""),
            "output": "",
            "status": "failed",
            "error": error,
            "items": picks,
            "index": index,
        }
    def history_path(self):
        return Path(self.output_dir.get() or ".") / ".board_video_history.json"

    def load_history(self):
        path = self.history_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        folders = {
            STANDARD_SEGMENT_NAMES[0]: Path(self.ai_folder_var.get().strip()) if self.ai_folder_var.get().strip() else None,
            STANDARD_SEGMENT_NAMES[1]: Path(self.old_folder_var.get().strip()),
        }
        cleaned = {}
        for key, item in data.items():
            exists = True
            for media in item.get("items", []):
                saved_path = media.get("path", "")
                if saved_path:
                    candidate = Path(saved_path)
                else:
                    folder = folders.get(media.get("segment", ""))
                    candidate = folder / media.get("file", "") if folder else Path("__missing__")
                if not candidate.is_file():
                    exists = False
                    break
            if exists:
                cleaned[key] = item
        return cleaned

    def save_history(self, history):
        path = self.history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_records(self, records):
        if not records:
            return
        base = Path(self.output_dir.get()) / f"generation_records_{now_stamp()}"
        json_path = base.with_suffix(".json")
        csv_path = base.with_suffix(".csv")
        json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["created_at", "board", "language", "language_code", "language_note", "audio_path", "selection_mode", "old_sort_mode", "output", "status", "error", "materials", "cover", "timing"])
            for record in records:
                writer.writerow(self.record_csv_row(record))
        self._queue_log(f"生成记录已保存：{json_path.name} / {csv_path.name}")

    def record_csv_row(self, record):
        materials = " | ".join(
            f"{item.get('segment')}={Path(item.get('file', '')).name}"
            + (f"[顺序{item.get('sequence_index')}]" if item.get("sequence_index") else "")
            for item in record.get("items", [])
        )
        cover = record.get("cover", {})
        cover_text = " | ".join(
            part
            for part in (
                f"preset={cover.get('preset', '')}" if cover else "",
                f"material={cover.get('material_label', '')}" if cover else "",
                f"series={cover.get('material_series', '')}" if cover else "",
                f"language_code={cover.get('language_code', '')}" if cover else "",
                f"title={cover.get('title', '')}" if cover else "",
                f"subtitle={cover.get('subtitle', '')}" if cover else "",
                f"logo_brand={cover.get('logo_brand', '')}" if cover else "",
                f"logo={cover.get('logo_path', '')}" if cover else "",
                f"vignette={cover.get('vignette_enabled', True)}" if cover else "",
                f"effect={cover.get('effect_strength', '中等')}" if cover else "",
                f"template_id={cover.get('template_id', 'classic_a')}" if cover else "",
                f"template_name={cover.get('template_name', '')}" if cover else "",
                f"stable_seed={cover.get('stable_seed', '')}" if cover else "",
                f"assignment_key={cover.get('assignment_key', '')}" if cover else "",
            )
            if part
        )
        timing = record.get("timing", {})
        timing_text = " | ".join(
            f"{key}={timing.get(key)}"
            for key in (
                "reference_audio",
                "audio_duration",
                "body_duration",
                "ai_duration",
                "old_duration",
                "end_reserve",
            )
            if timing.get(key) not in (None, "")
        )
        return [
            record.get("created_at", ""),
            record.get("board", ""),
            record.get("language", ""),
            record.get("language_note", ""),
            record.get("audio_path", ""),
            record.get("selection_mode", ""),
            record.get("old_sort_mode", ""),
            record.get("output", ""),
            record.get("status", ""),
            record.get("error", ""),
            materials,
            cover_text,
            timing_text,
        ]

    def export_last_records(self):
        if not self.last_records:
            messagebox.showinfo("提示", "还没有可导出的生成记录。")
            return
        file_path = filedialog.asksaveasfilename(
            title="导出生成记录",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("CSV", "*.csv")],
        )
        if not file_path:
            return
        path = Path(file_path)
        if path.suffix.lower() == ".csv":
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["created_at", "board", "language", "language_code", "language_note", "audio_path", "selection_mode", "old_sort_mode", "output", "status", "error", "materials", "cover", "timing"])
                for record in self.last_records:
                    writer.writerow(self.record_csv_row(record))
        else:
            path.write_text(json.dumps(self.last_records, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(f"已导出生成记录：{path}")

    def save_project(self):
        file_path = filedialog.asksaveasfilename(
            title="保存项目模板",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not file_path:
            return
        self.save_cover_settings()
        data = {
            "version": PROJECT_VERSION,
            "use_ai": bool(self.use_ai_var.get()),
            "root_dir": self.root_dir.get(),
            "ai_folder": self.ai_folder_var.get().strip(),
            "old_folder": self.old_folder_var.get().strip(),
            "audio_folder": self.audio_folder_var.get().strip(),
            "audio_material_root": self.audio_material_root_var.get().strip(),
            "audio_batch": self.audio_batch_var.get().strip(),
            "audio_auto_latest": bool(self.audio_auto_latest_var.get()),
            "old_sequential": bool(self.old_sequential_var.get()),
            "old_sort_mode": self.old_sort_mode_var.get(),
            "language_tasks": [
                {
                    "name": task.name,
                    "audio_path": task.audio_path,
                    "duration": task.duration,
                    "count": task.count,
                    "note": task.note,
                    "language_code": task.language_code,
                }
                for task in self.language_tasks
            ],
            "output_dir": self.output_dir.get(),
            "ffmpeg_path": self.ffmpeg_path.get(),
            "board": self.board_var.get(),
            "all_boards": self.all_boards_var.get(),
            "prefix": self.prefix_var.get(),
            "count": self.count_var.get(),
            "width": self.width_var.get(),
            "height": self.height_var.get(),
            "fps": self.fps_var.get(),
            "crf": self.crf_var.get(),
            "reference_audio": self.reference_audio_var.get().strip(),
            "reference_audio_duration": self.reference_audio_duration,
            "ai_duration": self.ai_duration_var.get(),
            "end_reserve": self.end_reserve_var.get(),
            "cover_series": self.current_series_key(),
            "cover_material": self.current_material_id(),
            "cover_template_mode": COVER_TEMPLATE_LABEL_TO_ID.get(
                self.cover_template_mode_var.get(), "auto_balanced"
            ),
            "segments": [
                {"name": seg.name, "path": seg.path, "duration": seg.duration}
                for seg in self.segments
            ],
            "cover_settings": {
                "version": 5,
                "logo_brand": self.logo_brand_var.get(),
                "logo_path": str(self.logo_path_for_brand()),
                "boards": self.cover_configs,
                "localized_texts": self.localized_cover_texts,
            },
        }
        Path(file_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(f"已保存模板：{file_path}")

    def open_project(self):
        file_path = filedialog.askopenfilename(
            title="打开项目模板",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not file_path:
            return
        try:
            data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("错误", f"模板读取失败：{exc}")
            return
        self.save_cover_settings()
        self.use_ai_var.set(bool(data.get("use_ai", True)))
        legacy_root = Path(data.get("root_dir", "")) if data.get("root_dir") else None
        legacy_board = str(data.get("board", "")).strip()
        legacy_base = legacy_root / legacy_board if legacy_root and legacy_board else None
        ai_folder = str(data.get("ai_folder", "")).strip()
        old_folder = str(data.get("old_folder", "")).strip()
        if not ai_folder and legacy_base:
            ai_folder = str(legacy_base / STANDARD_SEGMENT_NAMES[0])
        if not old_folder and legacy_base:
            old_folder = str(legacy_base / STANDARD_SEGMENT_NAMES[1])
        self.ai_folder_var.set(ai_folder)
        self.old_folder_var.set(old_folder)
        self.root_dir.set(str(Path(ai_folder).parent) if ai_folder else "")
        self.active_cover_board = None
        self.output_dir.set(data.get("output_dir", ""))
        self.ffmpeg_path.set(data.get("ffmpeg_path", self.ffmpeg_path.get()))
        self.prefix_var.set(data.get("prefix", "video"))
        self.count_var.set(int(data.get("count", 1)))
        self.width_var.set(str(data.get("width", DEFAULT_WIDTH)))
        self.height_var.set(str(data.get("height", DEFAULT_HEIGHT)))
        self.fps_var.set(str(data.get("fps", DEFAULT_FPS)))
        self.crf_var.set(str(data.get("crf", DEFAULT_CRF)))
        self.ai_duration_var.set(str(data.get("ai_duration", DEFAULT_AI_DURATION)))
        self.end_reserve_var.set(str(data.get("end_reserve", DEFAULT_END_RESERVE)))
        self.reference_audio_duration = None
        self.probed_audio_path = None
        self.reference_audio_var.set("")
        self.old_sequential_var.set(bool(data.get("old_sequential", False)))
        sort_mode = str(data.get("old_sort_mode", "下载时间"))
        self.old_sort_mode_var.set(sort_mode if sort_mode in {"下载时间", "文件名"} else "下载时间")
        saved_audio = str(data.get("reference_audio", "")).strip()
        audio_folder = str(data.get("audio_folder", "")).strip()
        if not audio_folder and saved_audio:
            audio_folder = str(Path(saved_audio).parent)
        audio_state = dict(data)
        audio_state["audio_folder"] = audio_folder
        self.configure_audio_project_state(audio_state)
        self.language_tasks = []
        saved_tasks = data.get("language_tasks", [])
        if isinstance(saved_tasks, list):
            for item in saved_tasks:
                if not isinstance(item, dict):
                    continue
                audio_path = str(item.get("audio_path", "")).strip()
                try:
                    if not audio_path:
                        raise ValueError("尚未绑定音频")
                    duration = self.probe_audio_duration(audio_path)
                    error = ""
                except ValueError as exc:
                    duration = float(item.get("duration", 0) or 0)
                    error = str(exc)
                self.language_tasks.append(
                    LanguageTask(
                        name=str(item.get("name", Path(audio_path).stem if audio_path else "未命名任务")).strip() or "未命名任务",
                        audio_path=audio_path,
                        duration=duration,
                        count=max(0, int(item.get("count", self.count_var.get()))),
                        error=error,
                        note=str(item.get("note", "")).strip(),
                        language_code=str(item.get("language_code", "en")) if str(item.get("language_code", "en")) in LANGUAGES else "en",
                    )
                )
        if not self.language_tasks and saved_audio:
            try:
                duration = self.probe_audio_duration(saved_audio)
                error = ""
            except ValueError as exc:
                duration = float(data.get("reference_audio_duration", 0) or 0)
                error = str(exc)
            self.language_tasks = [
                LanguageTask(Path(saved_audio).stem, saved_audio, duration, max(0, int(data.get("count", 1))), error)
            ]
        self.refresh_audio_batches(scan_tasks=True, log_result=False)
        self.all_boards_var.set(False)
        series_key = str(data.get("cover_series", "seven"))
        if series_key not in MATERIAL_SERIES:
            series_key = "seven"
        self.cover_series_var.set(MATERIAL_SERIES[series_key]["label"])
        allowed_materials = MATERIAL_SERIES[series_key]["materials"]
        material_id = str(data.get("cover_material", allowed_materials[0]))
        if material_id not in allowed_materials:
            material_id = allowed_materials[0]
        self.cover_material_var.set(MATERIALS[material_id])
        self.load_cover_settings()
        cover_settings = data.get("cover_settings", {})
        if isinstance(cover_settings, dict):
            if isinstance(cover_settings.get("boards"), dict):
                self.cover_configs.update(cover_settings["boards"])
            if isinstance(cover_settings.get("localized_texts"), dict):
                self.localized_cover_texts.update(cover_settings["localized_texts"])
            brand = str(cover_settings.get("logo_brand", ""))
            if brand not in LOGO_BRANDS:
                brand = "ExampleBrandB" if "baijax" in Path(str(cover_settings.get("logo_path", ""))).name.lower() else "ExampleBrandC"
            self.logo_brand_var.set(brand)
            self.logo_path_var.set(str(self.logo_path_for_brand()))
        self.update_ai_mode_controls()
        self.refresh_direct_task()
        self.refresh_language_tasks()
        self._log(f"已打开模板：{file_path}")

    def show_text_window(self, title, content):
        import tkinter as tk

        win = Toplevel(self.root)
        win.title(title)
        win.geometry("900x560")
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill=BOTH, expand=True)
        text = tk.Text(frame, wrap="word")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill="y")
        text.pack(fill=BOTH, expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")


def main():
    root = Tk()
    BoardVideoStitcherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
