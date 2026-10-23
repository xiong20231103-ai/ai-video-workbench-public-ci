from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from common_io import atomic_write_json, now_iso


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve()
TEMPLATE_FILE = (
    APP_DIR / "project_task_templates.json"
    if (APP_DIR / "project_task_templates.json").is_file()
    else BUNDLE_DIR / "project_task_templates.json"
)
INBOX_DIR = APP_DIR / "handoff" / "task_inbox" / "pending"
IMPORTED_DIR = APP_DIR / "handoff" / "task_inbox" / "imported"
CANCELLED_DIR = APP_DIR / "handoff" / "task_inbox" / "cancelled"
ExampleBrand_CONTROL_DIR = APP_DIR / "handoff" / "control" / "examplebrand_start_intents"
CURRENT_CONTEXT_FILE = APP_DIR / "handoff" / "current_pipeline_context.json"
DOWNLOAD_PROJECT_STATUS_FILE = APP_DIR / "handoff" / "download" / "project_status.json"
PIPELINE_PATHS_FILE = APP_DIR / "pipeline_paths.json"
SCHEMA_VERSION = 1
RUN_GROUP_SCHEMA_VERSION = 1
RUN_GROUP_SCHEMA_VERSION = 1
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
WINDOWS_SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


MATERIALS_BY_PROJECT = {
    "ProjectA": ["碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"],
    "ProjectB": ["碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"],
    "ProjectC": ["碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"],
    "ProjectD": ["碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"],
    "SPC地板": ["塑木地板"],
    "SPC墙板": ["晶岩墙板"],
    "ExampleBrand": ["清淤泵", "潜污泵", "离心泵", "轴流泵", "粉碎格栅", "一体化预制泵站", "大型泵站", "MaterialPump"],
}

DEFAULT_EXPECTED_COUNTS = {
    "ProjectA": 24,
    "ProjectB": 13,
    "ProjectC": 8,
    "ProjectD": 26,
    "SPC地板": 4,
    "SPC墙板": 4,
}

ENTITY_AUDIO_FOLDERS = {
    "碳岩板": "石纹", "净白板": "净白板", "金属板": "金属", "镜面板": "镜面",
    "木饰面": "木饰面", "PU石": "PU石", "PU软石": "PU软石",
}

DEFAULT_PIPELINE_PATHS = {
    "schemaVersion": 1,
    "audioRoots": {
        "ProjectA": r"E:\音频\ProjectA音频",
        "ProjectC": r"E:\音频\ProjectC音频",
        "ProjectD": r"E:\音频\ProjectD音频",
        "SPC地板": r"E:\音频\spc音频\地板",
        "SPC墙板": r"E:\音频\spc音频\墙板",
        "ProjectB": r"E:\音频\ProjectB音频",
        "ExampleBrand": r"E:\音频\ExampleBrand音频",
    },
}


DEFAULT_TEMPLATES = {
    "schemaVersion": 1,
    "templateVersion": "2026.07.20",
    "templates": [
        {
            "id": "cloud-carbon",
            "label": "ProjectA · 碳岩板",
            "project": "ProjectA",
            "material": "碳岩板",
            "download_path_source": "subject_dir",
            "subtitle_mode": "AI 去除 STTN",
            "subtitle_enabled": True,
            "download_workers": 2,
            "expectedCount": 24,
        },
        {
            "id": "physical-default",
            "label": "ProjectB · 净白板",
            "project": "ProjectB",
            "material": "净白板",
            "download_path_source": "tikhub_downloads",
            "subtitle_mode": "AI 去除 STTN",
            "subtitle_enabled": True,
            "download_workers": 2,
            "expectedCount": 13,
        },
        {
            "id": "huang-default",
            "label": "ProjectC · 碳岩板",
            "project": "ProjectC",
            "material": "碳岩板",
            "download_path_source": "tikhub_downloads",
            "subtitle_mode": "AI 去除 STTN",
            "subtitle_enabled": True,
            "download_workers": 2,
            "expectedCount": 8,
        },
        {
            "id": "real-huang-default",
            "label": "ProjectD · 碳岩板",
            "project": "ProjectD",
            "material": "碳岩板",
            "materials": ["碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"],
            "download_path_source": "tikhub_downloads",
            "subtitle_mode": "AI 去除 STTN",
            "subtitle_enabled": True,
            "download_workers": 2,
            "expectedCount": 26,
        },
        {
            "id": "spc-floor",
            "label": "SPC地板 · 塑木地板",
            "project": "SPC地板",
            "material": "塑木地板",
            "download_path_source": "tikhub_downloads",
            "subtitle_mode": "AI 去除 STTN",
            "subtitle_enabled": True,
            "download_workers": 2,
            "expectedCount": 4,
        },
        {
            "id": "spc-wall",
            "label": "SPC墙板 · 晶岩墙板",
            "project": "SPC墙板",
            "material": "晶岩墙板",
            "download_path_source": "tikhub_downloads",
            "subtitle_mode": "AI 去除 STTN",
            "subtitle_enabled": True,
            "download_workers": 2,
            "expectedCount": 4,
        },
    ],
}


@contextlib.contextmanager
def _json_file_lock(path: Path):
    """Cross-process lock for read/merge/write status registries.

    Windows releases the byte-range lock automatically if a worker exits, so a
    crashed process cannot leave the dashboard permanently locked.  POSIX
    fallback keeps local tests and development environments equivalent.
    """
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                handle.write(b"0")
                handle.flush()
            except OSError:
                pass
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:
                pass
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except ImportError:
                    pass
        finally:
            handle.close()


def _load_project_download_statuses_unlocked(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    records = payload.get("items")
    return {
        "schemaVersion": 1,
        "updatedAt": str(payload.get("updatedAt") or ""),
        "items": records if isinstance(records, dict) else {},
    }


def load_project_download_statuses(path: Path = DOWNLOAD_PROJECT_STATUS_FILE) -> dict[str, Any]:
    """Read the durable project-level Step 1/2 status registry.

    This registry deliberately lives outside the visible Tk table.  It lets the
    company submitter and OPS Dashboard see every project in a submitted queue,
    even after the workbench moves on to another project or restarts.
    """
    return _load_project_download_statuses_unlocked(path)


def save_project_download_status(record: dict[str, Any], path: Path = DOWNLOAD_PROJECT_STATUS_FILE) -> dict[str, Any]:
    task_id = str(record.get("taskId") or "").strip()
    if not task_id:
        raise ValueError("项目下载状态缺少 taskId")
    with _json_file_lock(path):
        payload = _load_project_download_statuses_unlocked(path)
        payload["items"][task_id] = dict(record)
        payload["updatedAt"] = now_iso()
        atomic_write_json(path, payload)
        return payload["items"][task_id]


def remove_project_download_status(task_id: str, path: Path = DOWNLOAD_PROJECT_STATUS_FILE) -> bool:
    """Remove an unstarted project's display card after it is revoked."""
    with _json_file_lock(path):
        payload = _load_project_download_statuses_unlocked(path)
        if str(task_id or "") not in payload["items"]:
            return False
        del payload["items"][str(task_id)]
        payload["updatedAt"] = now_iso()
        atomic_write_json(path, payload)
        return True


def load_templates(path: Path = TEMPLATE_FILE) -> dict[str, Any]:
    if not path.is_file():
        return json.loads(json.dumps(DEFAULT_TEMPLATES, ensure_ascii=False))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_TEMPLATES, ensure_ascii=False))
    if not isinstance(payload, dict) or not isinstance(payload.get("templates"), list):
        return json.loads(json.dumps(DEFAULT_TEMPLATES, ensure_ascii=False))
    return payload


def load_pipeline_paths(path: Path = PIPELINE_PATHS_FILE) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_PIPELINE_PATHS, ensure_ascii=False))
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("audioRoots"), dict):
                config["audioRoots"].update(payload["audioRoots"])
        else:
            atomic_write_json(path, config)
    except (OSError, ValueError):
        pass
    return config


def resolve_task_audio_directory(task: dict[str, Any], paths: dict[str, Any] | None = None) -> Path | None:
    """Resolve a logical audio batch on the machine that will actually run it."""
    profile = task.get("pipelineProfile") if isinstance(task, dict) else {}
    voice = profile.get("3_voice") if isinstance(profile, dict) else {}
    if not isinstance(voice, dict) or not voice:
        # Compatibility for rows imported by the V13 batch endpoint before
        # server-side profile normalization was added.
        try:
            normalized = ensure_pipeline_profile(task)
        except (TypeError, ValueError):
            normalized = task
        profile = normalized.get("pipelineProfile") if isinstance(normalized, dict) else {}
        voice = profile.get("3_voice") if isinstance(profile, dict) else {}
    if not isinstance(voice, dict) or not voice:
        return None
    # Compatibility for old task records created before local path mapping.
    legacy = str(voice.get("audioDirectoryRule") or "").strip()
    if legacy and (":" in legacy or legacy.startswith("\\\\")):
        return Path(legacy)
    project = str(task.get("project") or "")
    material = str(task.get("material") or "")
    audio_batch = str(voice.get("audioBatch") or task.get("audioBatch") or "").strip()
    roots = (paths or load_pipeline_paths()).get("audioRoots", {})
    root = str(roots.get(project) or "").strip()
    if not root or not audio_batch:
        return None
    if project == "ProjectB":
        return Path(root) / ENTITY_AUDIO_FOLDERS.get(material, material) / audio_batch
    return Path(root) / audio_batch


def normalize_urls(text_or_urls: str | list[str]) -> list[str]:
    text = "\n".join(text_or_urls) if isinstance(text_or_urls, list) else str(text_or_urls or "")
    return list(dict.fromkeys(match.rstrip(".,;，。；") for match in URL_PATTERN.findall(text)))


def canonical_task_id(task: dict[str, Any] | None) -> str:
    """Return the one production identity shared by Steps 1 through 6."""
    if not isinstance(task, dict):
        return ""
    parent = str(task.get("replenishForTaskId") or "").strip()
    return parent or str(task.get("canonicalTaskId") or task.get("taskId") or "").strip()


def new_run_group_id(prefix: str = "run") -> str:
    """Create a stable id for one user-selected combination of child batches."""
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", str(prefix or "run")).strip("-") or "run"
    return f"{safe_prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:10]}"


def selected_projects_from_tasks(tasks: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for task in tasks or []:
        project = str(task.get("project") or "").strip()
        if project and project not in result:
            result.append(project)
    return result


def validate_batch_voice_intent(
    payload: dict[str, Any], tasks: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Reject task rows that contradict the submitter's batch voice choice.

    V12 and older payloads did not carry a batch-level intent, so they retain
    their existing compatibility path. V13 and newer payloads are strict: the
    receiver must never silently accept a stale saved row from another voice
    batch.
    """
    voice_plan_intent = str(payload.get("voicePlanIntent") or "").strip()
    if not voice_plan_intent:
        return True, ""
    if voice_plan_intent not in {"create_new", "reuse_existing"}:
        return False, "批次语音处理意图无效"

    inconsistent: list[str] = []
    for item in tasks:
        project = str(item.get("project") or "").strip()
        material = str(item.get("material") or "").strip()
        expected_voice_plan = "create_new" if project == "ProjectB" else voice_plan_intent
        actual_voice_plan = str(item.get("voicePlan") or "").strip()
        if actual_voice_plan != expected_voice_plan:
            inconsistent.append(f"{project}/{material}={actual_voice_plan or 'missing'}")
            continue
        if expected_voice_plan == "create_new":
            production_date = str(
                item.get("productionDate") or item.get("date") or ""
            ).strip()
            if str(item.get("audioBatch") or "").strip() != production_date:
                inconsistent.append(
                    f"{project}/{material}=audioBatch:{item.get('audioBatch')}"
                )
    if inconsistent:
        return False, "批次语音处理与递交器选择不一致：" + "；".join(inconsistent)
    return True, ""


def load_task_record(task_id: str, *directories: Path) -> dict[str, Any] | None:
    """Load an audited inbox task without changing its durable state."""
    clean_id = str(task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", clean_id):
        return None
    search = directories or (IMPORTED_DIR, INBOX_DIR, CANCELLED_DIR)
    for directory in search:
        path = Path(directory) / f"{clean_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        valid, _ = validate_task(payload)
        if valid:
            return payload
    return None


def save_current_context(payload: dict[str, Any], path: Path = CURRENT_CONTEXT_FILE) -> None:
    valid, error = validate_task(payload)
    if not valid:
        raise ValueError(error)
    atomic_write_json(path, dict(payload))


def mark_replenishment_merged(
    child_task_id: str,
    parent_task_id: str,
    imported_dir: Path = IMPORTED_DIR,
) -> None:
    """Keep a non-production audit record after a Step 1/2 gap is closed."""
    path = Path(imported_dir) / f"{str(child_task_id or '').strip()}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    payload["status"] = "merged_into_parent"
    payload["canonicalTaskId"] = str(parent_task_id or "").strip()
    payload["mergedAt"] = now_iso()
    atomic_write_json(path, payload)


def batch_download_directory(base_directory: str | Path, task: dict[str, Any]) -> Path:
    """Create a new-only, non-destructive Step 1 location for a tagged batch."""
    def segment(value: Any) -> str:
        return WINDOWS_SAFE_NAME.sub("_", str(value or "未分类").strip()) or "未分类"
    return Path(base_directory) / "任务批次" / segment(task.get("date")) / segment(task.get("project")) / segment(task.get("material"))


def template_by_id(template_id: str, path: Path = TEMPLATE_FILE) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = load_templates(path)
    for template in catalog["templates"]:
        if str(template.get("id")) == template_id:
            return catalog, dict(template)
    raise ValueError(f"找不到任务模板：{template_id}")


def create_task(
    template_id: str,
    urls: str | list[str],
    *,
    date_label: str,
    material: str | None = None,
    audio_batch: str | None = None,
    voice_plan: str | None = None,
    cover_template_mode: str | None = None,
    template_path: Path = TEMPLATE_FILE,
    source: str = "company_submitter",
    run_group_id: str | None = None,
) -> dict[str, Any]:
    catalog, template = template_by_id(template_id, template_path)
    clean_urls = normalize_urls(urls)
    if not clean_urls:
        raise ValueError("没有可用的视频链接")
    selected_material = str(material or template["material"]).strip()
    allowed = template.get("materials") or MATERIALS_BY_PROJECT.get(str(template["project"]), [template["material"]])
    if selected_material not in allowed:
        raise ValueError(f"材料“{selected_material}”不适用于项目“{template['project']}”")
    project = str(template["project"])
    try:
        expected_count = max(1, int(template.get("expectedCount") or DEFAULT_EXPECTED_COUNTS.get(project) or len(clean_urls)))
    except (TypeError, ValueError):
        expected_count = len(clean_urls)
    selected_audio_batch = str(audio_batch or date_label).strip()
    selected_voice_plan = str(voice_plan or "").strip()
    if selected_voice_plan not in {"create_new", "reuse_existing"}:
        selected_voice_plan = "create_new" if project == "ProjectB" else "reuse_existing"
    if project == "ProjectB":
        selected_audio_batch = str(date_label).strip()
        selected_voice_plan = "create_new"
    selected_cover_mode = str(cover_template_mode or "auto_balanced").strip()
    if selected_cover_mode not in {"auto_balanced", "classic_a", "center_card_b", "editorial_c", "bottom_band_d", "auto_balanced_v2", "cover_a_v2", "cover_b_v2", "cover_c_v2", "cover_d_v2", "champagne_gold_a_v2", "champagne_gold_e_v2"}:
        raise ValueError("封面模板模式无效")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "runGroupId": str(run_group_id or "").strip(),
        "taskId": f"inbox-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "status": "pending_confirmation",
        "createdAt": now_iso(),
        "date": str(date_label).strip(),
        # Production date is immutable once a remote task is created.  The
        # execution date may cross midnight, but downstream paths must not.
        "productionDate": str(date_label).strip(),
        "audioBatch": selected_audio_batch,
        "voicePlan": selected_voice_plan,
        "templateId": template["id"],
        "templateVersion": str(catalog.get("templateVersion") or ""),
        "project": template["project"],
        "material": selected_material,
        "urls": clean_urls,
        "expectedCount": expected_count,
        # Stable four-template covers are opt-in for newly created tasks.
        # Legacy tasks without this field intentionally render template A.
        "coverTemplateMode": selected_cover_mode,
        "download_path_source": template.get("download_path_source", "tikhub_downloads"),
        "subtitle_mode": template.get("subtitle_mode", "AI 去除 STTN"),
        "subtitle_enabled": bool(template.get("subtitle_enabled", True)),
        "download_workers": int(template.get("download_workers", 2)),
        "pipelineProfile": build_pipeline_profile(
            project, selected_material, str(date_label).strip(), selected_audio_batch, selected_voice_plan
        ),
        "source": source,
    }


def build_pipeline_profile(
    project: str,
    material: str,
    date_label: str,
    audio_batch: str | None = None,
    voice_plan: str | None = None,
) -> dict[str, Any]:
    """The shared, reviewable settings that every downstream stage must use.

    Paths are rules, never a claim that an audio/video already exists.  Each
    existing client validates its own input before it starts work.
    """
    if project == "ExampleBrand":
        return {
            "kind": "examplebrand-natural-flow",
            "1_download": {"requiresConfirmation": True},
            "2_subtitle": {"mode": "AI 去除 STTN", "enabled": True},
            "3_voice": {"project": "ExampleBrand", "material": material, "audioBatch": audio_batch, "audioLocation": f"ExampleBrand / {audio_batch}", "useProjectLanguagePlan": True, "voicePlan": "create_new", "requiresNewGeneration": True, "voiceMap": {"en": "chand", "es": "Farid Dieck"}},
            "4_stitch": {"project": "ExampleBrand", "material": material, "logo": "ExampleBrand", "requiresExamplebrandAssets": True, "outputDirectoryRule": rf"E:\\生成视频\\{date_label}\\ExampleBrand"},
            "5_capcut": {"category": "ExampleBrand", "subtitleStyle": "CompanyStandard", "export": "2K", "outputDirectoryRule": rf"E:\\{date_label}\\ExampleBrand"},
            "6_upload": {"provider": "百度网盘", "remoteDirectoryRule": f"AI视频生产线/{date_label}/ExampleBrand", "onlyFinalMp4": True},
            "cleanup": {"sourceVideos": "recycle_after_uploaded", "stitchIntermediates": "recycle_after_uploaded"},
            "ops": {"dashboard": "AI视频生产线-OPS", "trackStatus": True, "independentBatch": True},
        }
    final_folder = "SPC" if project.startswith("SPC") else project
    audio_batch = str(audio_batch or date_label).strip()
    voice_plan = str(voice_plan or "").strip()
    if voice_plan not in {"create_new", "reuse_existing"}:
        voice_plan = "create_new" if project == "ProjectB" else "reuse_existing"
    logo = "ExampleBrandB" if project == "ProjectB" else "ExampleBrandC"
    if project == "ProjectB":
        audio_batch = date_label
        voice_plan = "create_new"
        audio_location = f"ProjectB / {ENTITY_AUDIO_FOLDERS.get(material, material)} / {audio_batch}"
        stitch_project = f"ProjectB{material}"
    elif project == "ProjectA":
        audio_location, stitch_project = f"ProjectA / {audio_batch}", "ProjectA"
    elif project in {"ProjectC", "ProjectD"}:
        audio_location, stitch_project = f"{project} / {audio_batch}", project
    elif project == "SPC地板":
        audio_location, stitch_project = f"SPC地板 / {audio_batch}", "spc地板"
    else:
        audio_location, stitch_project = f"SPC墙板 / {audio_batch}", "SPC墙板"
    return {
        "1_download": {"requiresConfirmation": True},
        "2_subtitle": {"mode": "AI 去除 STTN", "enabled": True},
        "3_voice": {
            "project": project, "material": material, "audioBatch": audio_batch,
            "audioLocation": audio_location, "useProjectLanguagePlan": True,
            "cycle": "daily" if project == "ProjectB" else "weekly",
            "voicePlan": voice_plan,
            "reuseExistingBatch": voice_plan == "reuse_existing",
            "requiresNewGeneration": voice_plan == "create_new",
        },
        "4_stitch": {"project": stitch_project, "material": material, "logo": logo, "outputDirectoryRule": rf"E:\生成视频\{date_label}\{final_folder}"},
        "5_capcut": {"category": project, "subtitleStyle": "CompanyStandard", "export": "2K", "outputDirectoryRule": rf"E:\{date_label}\{final_folder}"},
        "6_upload": {"provider": "百度网盘", "remoteDirectoryRule": f"AI视频生产线/{date_label}/{final_folder}", "onlyFinalMp4": True},
        "cleanup": {
            "sourceVideos": "recycle_after_uploaded", "stitchIntermediates": "recycle_after_uploaded",
            "capcutDrafts": "cleanup_after_export", "capcutSampleLibraries": "keep",
            "audio": "recycle_after_batch_uploaded" if project == "ProjectB" else "keep_until_weekly_batch_replaced",
            "audioBatch": audio_batch,
        },
        "ops": {"dashboard": "AI视频生产线-OPS", "trackStatus": True},
    }


def ensure_pipeline_profile(task: dict[str, Any]) -> dict[str, Any]:
    """Materialize the canonical downstream profile for a received task.

    Batch submissions can arrive from older V13 submitters that only carry
    top-level ``voicePlan`` and ``audioBatch`` fields.  Never infer a missing
    profile as reuse-existing audio: rebuild it from the task contract before
    the row is persisted.  Extension fields from an existing profile remain
    available, while canonical phase identity and paths always win.
    """
    if not isinstance(task, dict):
        return task
    project = str(task.get("project") or "").strip()
    material = str(task.get("material") or "").strip()
    if not project or not material:
        return task
    production_date = str(task.get("productionDate") or task.get("date") or "").strip()
    existing_profile = task.get("pipelineProfile")
    existing_voice = (
        existing_profile.get("3_voice")
        if isinstance(existing_profile, dict) and isinstance(existing_profile.get("3_voice"), dict)
        else {}
    )
    audio_batch = str(
        task.get("audioBatch") or existing_voice.get("audioBatch") or production_date
    ).strip()
    voice_plan = str(
        task.get("voicePlan") or existing_voice.get("voicePlan") or ""
    ).strip()
    canonical = build_pipeline_profile(
        project, material, production_date, audio_batch, voice_plan
    )
    if isinstance(existing_profile, dict):
        for key, value in existing_profile.items():
            if key not in canonical:
                canonical[key] = value
            elif isinstance(value, dict) and isinstance(canonical.get(key), dict):
                merged = dict(value)
                merged.update(canonical[key])
                canonical[key] = merged
    task["pipelineProfile"] = canonical
    voice = canonical.get("3_voice") if isinstance(canonical, dict) else {}
    if isinstance(voice, dict):
        task["audioBatch"] = str(voice.get("audioBatch") or audio_batch)
        task["voicePlan"] = str(voice.get("voicePlan") or voice_plan)
    return task


def validate_task(payload: dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "任务不是 JSON 对象"
    if str(payload.get("batchKind") or "").strip() == "examplebrand-natural-flow":
        required = ("taskId", "batchId", "date", "project", "material", "urls")
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            return False, "ExampleBrand任务缺少字段：" + "、".join(missing)
        if str(payload.get("project") or "").strip() != "ExampleBrand":
            return False, "ExampleBrand批次项目身份无效"
        if str(payload.get("material") or "").strip() not in MATERIALS_BY_PROJECT["ExampleBrand"]:
            return False, "ExampleBrand产品不受支持"
        run_group = str(payload.get("runGroupId") or "").strip()
        if run_group and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", run_group):
            return False, "runGroupId格式无效"
        if payload.get("selectedProjects") is not None and not isinstance(payload.get("selectedProjects"), list):
            return False, "selectedProjects必须是数组"
        urls = normalize_urls(payload.get("urls") or [])
        if len(urls) != len(payload.get("urls") or []):
            return False, "ExampleBrand链接格式不正确"
        return True, ""
    required = ("taskId", "date", "templateId", "project", "material", "urls")
    missing = [key for key in required if not str(payload.get(key) or "").strip()]
    if missing:
        return False, "缺少字段：" + "、".join(missing)
    run_group = str(payload.get("runGroupId") or "").strip()
    if run_group and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}", run_group):
        return False, "runGroupId格式无效"
    if payload.get("selectedProjects") is not None and not isinstance(payload.get("selectedProjects"), list):
        return False, "selectedProjects必须是数组"
    project = str(payload.get("project") or "").strip()
    cover_mode = str(payload.get("coverTemplateMode") or "classic_a").strip()
    if cover_mode not in {"auto_balanced", "classic_a", "center_card_b", "editorial_c", "bottom_band_d", "auto_balanced_v2", "cover_a_v2", "cover_b_v2", "cover_c_v2", "cover_d_v2", "champagne_gold_a_v2", "champagne_gold_e_v2"}:
        return False, "封面模板模式无效"
    language = str(payload.get("language") or "").strip().lower()
    korean_requested = language in {"ko", "korean", "韩语"}
    plan = payload.get("languagePlan")
    if isinstance(plan, list):
        korean_requested = korean_requested or any(
            isinstance(item, dict)
            and (str(item.get("code") or "").strip().lower() in {"ko", "korean"}
                 or str(item.get("name") or "").strip() == "韩语")
            for item in plan
        )
    if korean_requested and project != "ProjectD":
        return False, "韩语任务只允许用于ProjectD项目"
    urls = normalize_urls(payload.get("urls") or [])
    if len(urls) != len(payload.get("urls") or []):
        return False, "链接格式不正确"
    if payload.get("isReplenishment"):
        parent_id = str(payload.get("replenishForTaskId") or "").strip()
        if not parent_id:
            return False, "补链任务缺少原项目任务编号"
        canonical_id = str(payload.get("canonicalTaskId") or parent_id).strip()
        if canonical_id != parent_id:
            return False, "补链任务的父项目身份不一致"
        if str(payload.get("taskRole") or "step12_replenishment") != "step12_replenishment":
            return False, "补链任务只能用于第 1/2 步"
    return True, ""


def submit_task(payload: dict[str, Any], inbox_dir: Path = INBOX_DIR) -> Path:
    payload = ensure_pipeline_profile(dict(payload))
    valid, error = validate_task(payload)
    if not valid:
        raise ValueError(error)
    destination = inbox_dir / f"{payload['taskId']}.json"
    if destination.exists():
        raise FileExistsError("同一任务已投递")
    atomic_write_json(destination, payload)
    return destination


def submit_task_batch(payloads: list[dict[str, Any]], inbox_dir: Path = INBOX_DIR) -> list[Path]:
    """Store one reviewed submission batch after validating every task first.

    A failed validation never writes a partial batch.  If the filesystem fails
    while writing, only files created by this call are removed; existing task
    records are never touched.
    """
    if not isinstance(payloads, list) or not payloads:
        raise ValueError("任务框为空")
    prepared: list[tuple[Path, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    for payload in payloads:
        payload = ensure_pipeline_profile(dict(payload))
        valid, error = validate_task(payload)
        if not valid:
            raise ValueError(error)
        task_id = str(payload["taskId"])
        if task_id in seen_ids:
            raise ValueError("任务框存在重复任务编号")
        seen_ids.add(task_id)
        destination = inbox_dir / f"{task_id}.json"
        if destination.exists():
            raise FileExistsError(f"同一任务已投递：{task_id}")
        prepared.append((destination, dict(payload)))

    created: list[Path] = []
    try:
        for destination, payload in prepared:
            atomic_write_json(destination, payload)
            created.append(destination)
    except OSError:
        for destination in created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return created


def list_pending_tasks(inbox_dir: Path = INBOX_DIR) -> list[tuple[Path, dict[str, Any]]]:
    items: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(inbox_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        valid, _ = validate_task(payload)
        if valid and payload.get("status") == "pending_confirmation":
            # Older receiver builds persisted ``auto_start_first`` without the
            # ExampleBrand intent consumed by the workbench idle gate.  Repair that
            # durable handoff while listing the real inbox, but never mutate
            # custom test directories or ordinary formal tasks.
            if (
                inbox_dir == INBOX_DIR
                and str(payload.get("batchKind") or "").strip() == "examplebrand-natural-flow"
                and str(payload.get("startMode") or "").strip() in {"auto_start_first", "auto_start_sequential"}
            ):
                try:
                    ensure_examplebrand_start_intent(payload, requested_by="inbox_migration")
                except (OSError, ValueError):
                    # Keep the task visible; the workbench will surface the
                    # identity conflict instead of silently dropping it.
                    pass
            items.append((path, payload))
    # Keep a submitted task box together and preserve its explicit order.
    # Older one-off tasks have no batch id and are listed afterwards by file time.
    items.sort(key=lambda item: int(item[1].get("batchSequence") or 0))
    items.sort(
        key=lambda item: str(item[1].get("batchId") or item[1].get("createdAt") or ""),
        reverse=True,
    )
    return items


def ensure_examplebrand_start_intent(
    task: dict[str, Any], *, requested_by: str = "receiver_start", write: bool = True
) -> Path | None:
    """Create or validate the idle-gate intent for an explicit ExampleBrand start.

    ``startMode=auto_start_*`` is itself the durable operator decision.  This
    helper repairs tasks written by pre-intent receiver versions and is
    idempotent across receiver/workbench polling and restarts.
    """
    if str(task.get("batchKind") or "").strip() != "examplebrand-natural-flow":
        return None
    if str(task.get("startMode") or "").strip() not in {"auto_start_first", "auto_start_sequential"}:
        return None
    task_id_value = str(task.get("taskId") or "").strip()
    if not task_id_value:
        raise ValueError("ExampleBrand启动意图缺少taskId")
    canonical_id = str(task.get("canonicalTaskId") or task_id_value).strip()
    run_group = str(task.get("runGroupId") or "").strip()
    batch_id = str(task.get("batchId") or "").strip()
    intent_path = ExampleBrand_CONTROL_DIR / f"{task_id_value}.json"
    existing: dict[str, Any] = {}
    try:
        existing_payload = json.loads(intent_path.read_text(encoding="utf-8"))
        if isinstance(existing_payload, dict):
            existing = existing_payload
    except (OSError, ValueError):
        existing = {}
    identity = {
        "taskId": task_id_value,
        "canonicalTaskId": canonical_id,
        "runGroupId": run_group,
        "batchId": batch_id,
    }
    if existing:
        existing_identity = {key: str(existing.get(key) or "").strip() for key in identity}
        if existing_identity != identity:
            raise ValueError("ExampleBrand启动意图身份冲突，未启动任务")
    requested_at = str(task.get("startRequestedAt") or now_iso())
    intent = dict(existing)
    intent.update({
        "schemaVersion": 1,
        **identity,
        "batchKind": "examplebrand-natural-flow",
        "startPolicy": "auto_when_idle",
        "enabled": True,
        "requestedAt": requested_at,
        "requestedBy": requested_by,
        "source": "receiver_start",
    })
    intent["eligibleAfter"] = requested_at
    intent.setdefault("createdAt", requested_at)
    if write:
        atomic_write_json(intent_path, intent)
    return intent_path


def cancel_pending_task(
    task_id: str,
    *,
    inbox_dir: Path = INBOX_DIR,
    cancelled_dir: Path = CANCELLED_DIR,
    reason: str = "cancelled_from_receiver",
) -> dict[str, Any]:
    """Cancel a task only while it is still waiting for workbench confirmation.

    This is deliberately a record move, never a deletion of business files.
    Imported or running tasks have already entered the production chain and must
    be handled by the workbench's stop/recovery flow instead.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(task_id or "")):
        raise ValueError("无效的任务编号")
    source = inbox_dir / f"{task_id}.json"
    if not source.is_file():
        raise FileNotFoundError("任务不存在，或已被工作台导入")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("任务记录无法读取") from exc
    valid, error = validate_task(payload)
    if not valid:
        raise ValueError(error)
    if payload.get("status") != "pending_confirmation":
        raise ValueError("该任务已经开始处理，不能从接收器撤销")

    cancelled = dict(payload)
    cancelled["status"] = "cancelled"
    cancelled["cancelledAt"] = now_iso()
    cancelled["cancelReason"] = reason
    destination = cancelled_dir / source.name
    if destination.exists():
        destination = cancelled_dir / f"{task_id}-{uuid.uuid4().hex[:6]}.json"
    atomic_write_json(destination, cancelled)
    source.unlink()
    return cancelled


def request_pending_start(
    task_id: str,
    *,
    inbox_dir: Path = INBOX_DIR,
    requested_by: str = "receiver_page",
) -> dict[str, Any]:
    """Mark one not-yet-imported remote task for safe sequential start.

    The workbench remains the only process that imports and runs a task.  This
    helper merely changes the durable request in the inbox, so a receiver or
    workbench restart cannot lose the operator's explicit start decision.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(task_id or "")):
        raise ValueError("无效的任务编号")
    source = inbox_dir / f"{task_id}.json"
    if not source.is_file():
        raise FileNotFoundError("任务不存在，或已经被工作台导入")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("任务记录无法读取") from exc
    valid, error = validate_task(payload)
    if not valid:
        raise ValueError(error)
    if payload.get("status") != "pending_confirmation":
        raise ValueError("该任务已经开始处理，不能再次启动")

    updated = dict(payload)
    # The workbench recognises this existing mode and still enforces batch
    # ordering, including a predecessor blocked by missing links.
    updated["startMode"] = "auto_start_first"
    updated["startRequestedAt"] = now_iso()
    updated["startRequestedBy"] = requested_by

    # The workbench has a task-scoped idle gate.  Keep the durable receiver
    # mode and that gate's intent in sync for ExampleBrand, including legacy tasks
    # that were already switched to auto_start_first by an older build.
    ensure_examplebrand_start_intent(updated, requested_by=requested_by, write=False)
    atomic_write_json(source, updated)
    ensure_examplebrand_start_intent(updated, requested_by=requested_by)
    return updated


def cancel_imported_task(
    task_id: str,
    *,
    imported_dir: Path = IMPORTED_DIR,
    cancelled_dir: Path = CANCELLED_DIR,
    current_context_file: Path = CURRENT_CONTEXT_FILE,
    project_status_file: Path = DOWNLOAD_PROJECT_STATUS_FILE,
    reason: str = "revoked_before_download_from_receiver",
) -> dict[str, Any]:
    """Revoke an imported task only before the workbench starts Step 1.

    This preserves an audit record and never touches downloaded videos, audio,
    or other business files.  Running tasks must still use the workbench stop
    and recovery flow instead.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(task_id or "")):
        raise ValueError("无效的任务编号")
    source = imported_dir / f"{task_id}.json"
    if not source.is_file():
        raise FileNotFoundError("任务不存在，或已经开始处理")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("任务记录无法读取") from exc
    valid, error = validate_task(payload)
    if not valid:
        raise ValueError(error)
    if payload.get("status") != "imported_waiting_start":
        raise ValueError("该任务已经开始处理，不能从接收器撤回")

    cancelled = dict(payload)
    cancelled["status"] = "cancelled"
    cancelled["cancelledAt"] = now_iso()
    cancelled["cancelReason"] = reason
    destination = cancelled_dir / source.name
    if destination.exists():
        destination = cancelled_dir / f"{task_id}-{uuid.uuid4().hex[:6]}.json"
    atomic_write_json(destination, cancelled)
    source.unlink()

    try:
        current = json.loads(current_context_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if isinstance(current, dict) and str(current.get("taskId") or "") == task_id:
        atomic_write_json(current_context_file, cancelled)
    remove_project_download_status(task_id, project_status_file)
    return cancelled


def mark_imported(
    path: Path,
    payload: dict[str, Any],
    imported_dir: Path = IMPORTED_DIR,
    current_context_file: Path = CURRENT_CONTEXT_FILE,
) -> Path:
    payload = dict(payload)
    payload["status"] = "imported_waiting_start"
    payload["importedAt"] = now_iso()
    destination = imported_dir / path.name
    atomic_write_json(destination, payload)
    atomic_write_json(current_context_file, payload)
    path.unlink(missing_ok=True)
    return destination


def load_current_context(path: Path = CURRENT_CONTEXT_FILE) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    valid, _ = validate_task(payload)
    return payload if valid else None
