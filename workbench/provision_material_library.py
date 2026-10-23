"""Provision verified AI-material folders as non-destructive stitcher projects.

Run after adding a new archive to E:\\AI素材库.  Existing project entries are
kept; this script only refreshes the named, material-specific automation
projects below and writes one timestamped backup before the first change.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(r"E:\AI素材库\素材1")
PROJECT_FILE = Path(r"E:\Backup\Documents\ai-video-workbench-suite\video-stitcher\.board_video_projects.json")

MATERIALS = {
    "净白板": ("净白板", "white_panel", True),
    "碳岩板": ("石纹", "carbon_crystal", True),
    "金属板": ("金属板", "metal_panel", True),
    "木饰面": ("木纹", "wood_veneer", True),
    "镜面板": ("", "mirror_panel", False),
    "PU石": ("", "pu_stone", False),
    "PU软石": ("", "flexible_pu_stone", False),
    "塑木地板": ("", "plastic_wood_flooring", False),
    "晶岩墙板": ("石纹", "crystal_rock_wall", True),
}

PROJECT_MATERIALS = {
    "ProjectB": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "ProjectA": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "ProjectC": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "ProjectD": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "SPC地板": ("塑木地板",),
    "SPC墙板": ("晶岩墙板",),
}

BRANDS = {"ProjectB": "ExampleBrandB", "ProjectA": "ExampleBrandC", "ProjectC": "ExampleBrandC", "ProjectD": "ExampleBrandC", "SPC地板": "ExampleBrandC", "SPC墙板": "ExampleBrandC"}


def audio_root_for(project: str, material: str) -> str:
    if project == "ProjectB":
        return str(Path(r"E:\音频\ProjectB音频") / material)
    if project == "SPC地板":
        return str(Path(r"E:\音频\SPC地板音频") / material)
    if project == "SPC墙板":
        return str(Path(r"E:\音频\SPC墙板音频") / material)
    return str(Path(rf"E:\音频\{project}音频"))


def main() -> None:
    if not ROOT.is_dir():
        raise SystemExit(f"AI 素材库不存在：{ROOT}")
    payload = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    original_payload = copy.deepcopy(payload)
    projects = payload.setdefault("projects", {})
    template = copy.deepcopy(projects.get("默认项目") or next(iter(projects.values()), {}))
    if not isinstance(template, dict):
        raise SystemExit("拼接项目配置中没有可用模板")
    changed, added = False, []
    for project, materials in PROJECT_MATERIALS.items():
        for material in materials:
            ai_name, cover_material, use_ai = MATERIALS[material]
            name = f"{project}{material}"
            data = projects.get(name)
            created = False
            if not isinstance(data, dict):
                same_project = next(
                    (value for key, value in projects.items() if str(key).startswith(project) and isinstance(value, dict)),
                    template,
                )
                data = copy.deepcopy(same_project)
                projects[name] = data
                created = True
                added.append(name)
                changed = True

            if use_ai:
                ai_folder = ROOT / ai_name
                if not ai_folder.is_dir():
                    raise SystemExit(f"素材目录缺失：{ai_folder}")
                if not data.get("ai_folder"):
                    data["ai_folder"] = str(ai_folder)
                    changed = True
                if data.get("use_ai") is not True:
                    data["use_ai"] = True
                    changed = True
            else:
                if data.get("use_ai") is not False or data.get("ai_folder") or data.get("old_folder"):
                    changed = True
                data["use_ai"] = False
                data["ai_folder"] = ""
                data["old_folder"] = ""
                picker_dirs = data.get("picker_dirs") if isinstance(data.get("picker_dirs"), dict) else {}
                picker_dirs["old"] = ""
                data["picker_dirs"] = picker_dirs

            brand = BRANDS[project]
            for key, value in {
                "prefix": name,
                "cover_material": cover_material,
                "logo_brand": brand,
                "logo_path": str(PROJECT_FILE.parent / ("ExampleBrandB-transparent.png" if brand == "ExampleBrandB" else "ExampleBrandC-transparent.png")),
                "audio_auto_latest": True,
            }.items():
                if data.get(key) != value:
                    data[key] = value
                    changed = True
            if created or not str(data.get("audio_material_root") or "").strip() or (
                project == "ProjectB" and material in {"镜面板", "PU石", "PU软石"}
                and Path(str(data.get("audio_material_root") or "")).name != material
            ):
                data["audio_material_root"] = audio_root_for(project, material)
                changed = True
            data.setdefault("language_tasks", [])

    if not changed:
        print(json.dumps({"backup": "", "projects": [], "changed": False}, ensure_ascii=False))
        return
    backup = PROJECT_FILE.with_name(f"{PROJECT_FILE.stem}.before-material-map-{datetime.now():%Y%m%d-%H%M%S}.json")
    backup.write_text(json.dumps(original_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["version"] = max(int(payload.get("version") or 0), 11)
    PROJECT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"backup": str(backup), "projects": added, "changed": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
