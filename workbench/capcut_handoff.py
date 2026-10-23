from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from common_io import atomic_write_json, pid_is_alive, process_image_name


CATEGORIES = ("ProjectA", "ProjectB", "ProjectC", "ProjectD", "SPC地板", "SPC墙板", "ExampleBrand")
CATEGORY_ORDER = {name: index for index, name in enumerate(CATEGORIES)}
LANGUAGE_NAMES = {
    "en": "英语",
    "es": "西班牙语",
    "de": "德语",
    "id": "印尼语",
    "fr": "法语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "ru": "俄语",
    "tr": "土耳其语",
    "ko": "韩语",
}
MATERIAL_NAMES = (
    "净白板", "碳岩板", "金属板", "镜面板", "木饰面",
    "PU石", "PU软石", "塑木地板", "晶岩墙板",
    "清淤泵", "潜污泵", "离心泵", "轴流泵", "粉碎格栅", "一体化预制泵站", "大型泵站", "MaterialPump",
)
RUNNABLE_STATUSES = {"waiting_capcut", "waiting_client", "failed", "stopped"}
REMOTE_PIPELINE_ID = re.compile(r"(?:^|[_-])remote-(inbox-\d+-[a-z0-9]+)(?:[_-]|$)", re.IGNORECASE)
PRODUCTION_TARGETS = {
    "ProjectA": 24,
    "ProjectB": 13,
    "ProjectC": 8,
    "ProjectD": 26,
    "SPC地板": 4,
    "SPC墙板": 4,
    "ExampleBrand": 21,
}
STATUS_LABELS = {
    "waiting_capcut": "等待第五步",
    "validation_failed": "校验失败",
    "waiting_client": "等待 CapCut",
    "running": "正在处理",
    "completed": "已完成",
    "failed": "失败",
    "stopped": "已停止",
    "manual_review": "待人工确认",
}
CAPCUT_STATE_STATUS = {
    "Waiting": "waiting_client",
    "Completed": "completed",
    "Failed": "failed",
}
DATE_PATTERN = re.compile(r"^\d{1,2}\.\d{1,2}$")
INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LEGACY_INVALID_JSON_BACKSLASH = re.compile(r'(?<!\\)\\(?!["\\/bfnrtu])')


def load_json_compat(path: Path) -> dict:
    """Read legacy handoff JSON without losing Windows-path records.

    Some pre-v3 files accidentally persisted a single Windows path separator
    in a JSON string (for example ``E:\\generated``). Strict parsers reject
    those files and caused the active fifth-step ledger to vanish. Repair only
    invalid JSON escape starters in memory; the next atomic save normalizes the
    file without touching any media paths.
    """
    raw = Path(path).read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = json.loads(LEGACY_INVALID_JSON_BACKSLASH.sub(r"\\\\", raw))
    return value if isinstance(value, dict) else {}


CAPCUT_CLIENT_IMAGE = "capcutbatchclient.exe"


def _pid_is_alive(value) -> bool:
    """Whether a status file's CapCut client is still running.

    Never probe with ``os.kill(pid, 0)``: on Windows it terminates the target,
    so the 90 s stale check killed the client it was checking, or after PID
    reuse an unrelated program.  A PID now held by another program is not the
    client; an unreadable image name keeps the live answer (conservative).
    """
    try:
        pid = int(value or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not pid_is_alive(pid):
        return False
    image = process_image_name(pid)
    return not image or Path(image).name.lower() == CAPCUT_CLIENT_IMAGE


def _status_is_stale(payload: dict, max_age_seconds: int = 90) -> bool:
    """A running handoff without a live client/heartbeat is recoverable.

    This is intentionally conservative: a fresh heartbeat wins, and a PID is
    only trusted while it still identifies a live process.  Legacy manifests
    have neither field, so they become recoverable instead of blocking a new
    task forever.
    """
    if not bool(payload.get("running")):
        return False
    heartbeat = str(payload.get("heartbeatAt") or payload.get("updatedAt") or "")
    try:
        age = (datetime.now().astimezone() - datetime.fromisoformat(heartbeat)).total_seconds()
    except (TypeError, ValueError):
        age = max_age_seconds + 1
    return age > max_age_seconds and not _pid_is_alive(payload.get("clientPid"))


def safe_name(value: str) -> str:
    cleaned = INVALID_FILE_CHARS.sub("_", str(value or "").strip()).strip(" .")
    return cleaned or "未命名"


def subtitle_placement_metadata(source_paths: list[str]) -> dict:
    """Resolve only task-registered subtitle verification sidecars."""
    records = []
    for value in source_paths or []:
        path = Path(str(value or ""))
        sidecar = path.with_suffix(path.suffix + ".subtitle-verification.json")
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or Path(str(payload.get("outputPath") or "")).resolve() != path.resolve():
            continue
        records.append(payload)
    if not records:
        return {"captionPlacement": "company_default", "subtitleTreatment": "unknown", "sourceSubtitleBands": []}
    if any(str(item.get("subtitleTreatment") or "") == "blur_fallback" for item in records):
        return {"captionPlacement": "company_default", "subtitleTreatment": "blur_fallback", "sourceSubtitleBands": []}
    record = next((item for item in records if item.get("originalSubtitleBands")), records[0])
    bands = record.get("originalSubtitleBands") if isinstance(record.get("originalSubtitleBands"), list) else []
    height = int(record.get("sourceVideoHeight") or 0)
    target_y = None
    if bands and height > 0:
        valid = [area for area in bands if isinstance(area, list) and len(area) >= 2]
        if valid:
            center = sum((float(area[0]) + float(area[1])) / 2.0 for area in valid) / len(valid)
            target_y = max(-0.82, min(0.75, 1.0 - 2.0 * center / height))
    return {
        "captionPlacement": "original_band" if target_y is not None else "company_default",
        "subtitleTreatment": str(record.get("subtitleTreatment") or "sttn_clean"),
        "sourceSubtitleBands": bands,
        "sourceVideoHeight": height,
        "captionTargetY": target_y,
    }


def infer_date(output_path: Path, created_at: str = "", production_date: str = "") -> str:
    preferred = str(production_date or "").strip()
    if DATE_PATTERN.match(preferred):
        return preferred
    for parent in output_path.parents:
        if DATE_PATTERN.match(parent.name):
            return parent.name
    try:
        parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = datetime.now()
    return f"{parsed.month}.{parsed.day}"


def output_category(project: str) -> str:
    return "SPC" if str(project).startswith("SPC") else project


def production_target(project: str) -> int:
    """The pipeline contract is per project, never inferred from a date scan."""
    value = str(project or "").strip()
    for name, count in PRODUCTION_TARGETS.items():
        if value == name or value.startswith(name):
            return count
    return 0


def remote_pipeline_id(*values: str) -> str:
    for value in values:
        match = REMOTE_PIPELINE_ID.search(str(value or ""))
        if match:
            return match.group(1)
    return ""


def infer_project(record: dict, record_path: Path | None = None, project_hint: str = "") -> str:
    project = str(record.get("project") or project_hint or "").strip()
    if project in CATEGORIES:
        return project
    if project.startswith("ProjectB"):
        return "ProjectB"
    if project.upper().startswith("SPC地板"):
        return "SPC地板"
    if project.upper().startswith("SPC墙板"):
        return "SPC墙板"
    material = str((record.get("cover") or {}).get("material_label") or record.get("board") or "")
    if "塑木地板" in material:
        return "SPC地板"
    if "晶岩墙板" in material:
        return "SPC墙板"
    if record_path:
        for parent in record_path.parents:
            if parent.name in CATEGORIES:
                return parent.name
            if parent.name == "SPC":
                break
    return ""


def last_number(value: str, default: str = "001") -> str:
    matches = [part for part in str(value or "").split("_") if part.isdigit()]
    return matches[-1] if matches else default


class CapCutHandoffStore:
    def __init__(self, root: Path, final_output_root: Path):
        self.root = Path(root)
        self.final_output_root = Path(final_output_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def date_file(self, date_value: str) -> Path:
        return self.root / safe_name(date_value) / "第四步到第五步.json"

    def load_date(self, date_value: str) -> dict:
        path = self.date_file(date_value)
        try:
            payload = load_json_compat(path)
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["schemaVersion"] = max(3, int(payload.get("schemaVersion") or 1))
        payload.setdefault("date", date_value)
        payload.setdefault("tasks", [])
        payload.setdefault("archivedAttempts", [])
        # A release-only failure is maintenance evidence, not permission to
        # re-edit a QC-proved output. Keep the original error for audit.
        from resume_plan import released_final_is_verified
        for row in payload["tasks"]:
            if (str(row.get("status") or "").lower() in {"failed", "stopped"}
                    and released_final_is_verified(row)):
                row["maintenanceStatus"] = row["status"]
                row["maintenanceError"] = row.get("error", "")
                row["status"] = "completed"
                row["selected"] = False
        return payload

    def save_date(self, date_value: str, payload: dict) -> None:
        path = self.date_file(date_value)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload["schemaVersion"] = 3
        payload["date"] = date_value
        payload["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        # A shared "第四步到第五步.tmp" let two writers clobber each other and
        # failed outright on a short-lived reader handle (WinError 5,
        # 2026-10-07 18:28).  Same bytes, unique temp file, retried replace.
        atomic_write_json(path, payload)

    def requeue_invalid_completed_task(self, date_value: str, task_id: str, reason: str,
                                       quarantined_output: str = "") -> bool:
        """Archive one proven-invalid final artifact and make only that row runnable.

        This is intentionally not a generic "retry completed" shortcut.  The
        caller must have established an identity mismatch (for example a
        wall-board task opened a floor-board draft).  The original row and its
        output path remain in ``archivedAttempts`` for diagnosis; no media is
        removed by this ledger operation.
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        payload = self.load_date(date_value)
        task = next((item for item in payload.get("tasks") or []
                     if str(item.get("taskId") or "") == task_id), None)
        if not isinstance(task, dict):
            return False
        archive = dict(task)
        archive.update({
            "archivedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "archiveReason": str(reason or "invalid_final_artifact"),
            "quarantinedOutput": str(quarantined_output or ""),
        })
        payload.setdefault("archivedAttempts", []).append(archive)
        task.update({
            "status": "waiting_capcut",
            "selected": True,
            "error": "",
            "currentPhase": "Waiting",
            "phaseText": "",
            "heartbeatAt": "",
            "clientPid": 0,
            "activeManifestId": "",
            "activeAttemptId": "",
            "statusFile": "",
            # This marker is consumed by the Dashboard state bus.  It makes
            # an explicit recovery contract win over a newer abandoned
            # CapCut status file without treating ordinary ledger rows as
            # stronger than a live client attempt.
            "contractResetAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        task["invalidOutput"] = {
            "reason": str(reason or "invalid_final_artifact"),
            "quarantinedPath": str(quarantined_output or ""),
            "recordedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.save_date(date_value, payload)
        return True

    def dates(self) -> list[str]:
        values = [
            folder.name
            for folder in self.root.iterdir()
            if folder.is_dir() and self.date_file(folder.name).is_file()
        ]
        return sorted(values, key=self._date_sort_key, reverse=True)

    def change_final_output_root(self, new_root: Path) -> int:
        self.final_output_root = Path(new_root).resolve()
        changed = 0
        for date_value in self.dates():
            payload = self.load_date(date_value)
            date_changed = False
            for task in payload["tasks"]:
                if task.get("status") == "completed":
                    continue
                category = output_category(str(task.get("project") or ""))
                if not category:
                    continue
                final_name = "_".join(
                    safe_name(part)
                    for part in (
                        task.get("project", ""),
                        task.get("material", ""),
                        task.get("language", ""),
                        str(task.get("number") or "001").zfill(3),
                        f"{max(1, int(task.get('sequence') or 1)):02d}",
                    )
                ) + ".mp4"
                new_path = self.final_output_root / date_value / category / final_name
                if str(task.get("finalOutputPath") or "") == str(new_path):
                    continue
                task["finalOutputPath"] = str(new_path)
                task.pop("handoffManifest", None)
                task.pop("statusFile", None)
                if task.get("status") == "waiting_client":
                    task["status"] = "waiting_capcut"
                self._revalidate(task)
                date_changed = True
                changed += 1
            if date_changed:
                self.save_date(date_value, payload)
        return changed

    @staticmethod
    def _date_sort_key(value: str):
        try:
            month, day = (int(part) for part in value.split(".", 1))
            return month, day
        except (TypeError, ValueError):
            return 0, 0

    def ingest_records(self, project: str, records: list[dict], record_path: Path | None = None) -> int:
        grouped: dict[str, list[dict]] = {}
        for record in records or []:
            if record.get("status") != "success":
                continue
            video_path = Path(str(record.get("output") or "")).expanduser()
            audio_path = Path(str(record.get("audio_path") or "")).expanduser()
            subtitle_sources = [
                str(Path(value).expanduser().resolve())
                for value in (record.get("subject_source_paths") or [])
                if str(value or "").strip()
            ]
            actual_project = infer_project(record, record_path, project)
            cover = record.get("cover") or {}
            material = str(cover.get("material_label") or record.get("board") or "").strip()
            language_code = str(record.get("language_code") or cover.get("language_code") or "").strip().lower()
            language = LANGUAGE_NAMES.get(language_code, str(record.get("language") or "").strip())
            date_value = infer_date(
                video_path,
                record.get("created_at", ""),
                record.get("productionDate") or record.get("date") or "",
            )
            voice_number = last_number(audio_path.stem)
            sequence_text = last_number(video_path.stem, voice_number)
            sequence = max(1, int(sequence_text))
            legacy_identity = "|".join(
                (date_value, actual_project, material, language_code, voice_number, str(video_path.resolve()))
            )
            legacy_task_id = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()[:24]
            # A retry may regenerate the same output pathname after the prior
            # task was already exported/uploaded.  Path-only identity would
            # overwrite that completed task and silently lose the new video.
            # The generation record plus its timestamp is stable across scans
            # yet distinct for a genuine second generation.
            generation_identity = "|".join((
                str(record_path.resolve()) if record_path else "",
                str(record.get("created_at") or ""),
                str(video_path.resolve()),
            ))
            generation_task_id = hashlib.sha256(
                ("generation|" + generation_identity).encode("utf-8")
            ).hexdigest()[:24]
            task_id = generation_task_id
            category = output_category(actual_project)
            explicit_remote_task_id = str(record.get("remotePipelineTaskId") or "").strip()
            remote_task_id = explicit_remote_task_id or remote_pipeline_id(
                str(video_path), str(audio_path)
            )
            remote_task_id_source = str(record.get("remotePipelineTaskIdSource") or "").strip()
            if not remote_task_id_source:
                remote_task_id_source = "record" if explicit_remote_task_id else (
                    "inferred_media" if remote_task_id else ""
                )
            final_name_parts = [
                safe_name(part)
                for part in (
                    actual_project,
                    material,
                    language,
                    voice_number.zfill(3),
                    f"{sequence:02d}",
                )
            ]
            subtitle_placement = subtitle_placement_metadata(subtitle_sources)
            # Remote Fish tasks deliberately retain a receiver id and a
            # source-copy suffix.  Two such tasks can share the legacy
            # language/number/sequence tuple, so the old final filename would
            # overwrite one export with the other.  Preserve the familiar
            # filename for ordinary batches, but make remote exports stable
            # and unique using their deterministic handoff task id.
            if "remote-inbox-" in audio_path.stem:
                final_name_parts.append(task_id[:8])
            explicit_output_name = safe_name(Path(str(record.get("outputName") or "")).name)
            final_name = explicit_output_name if actual_project == "ExampleBrand" and explicit_output_name.lower().endswith(".mp4") else "_".join(final_name_parts) + ".mp4"
            final_path = self.final_output_root / date_value / category / final_name
            status = "waiting_capcut"
            issues = []
            if actual_project not in CATEGORIES:
                issues.append("无法识别项目")
            if not material:
                issues.append("缺少材料")
            if not language:
                issues.append("缺少语言")
            if not video_path.is_file():
                issues.append("视频不存在")
            if not audio_path.is_file():
                issues.append("音频不存在")
            if bool(record.get("formalTaskIdentity")) and not self._is_structured_remote_task(record):
                issues.append("正式任务身份不完整")
            if final_path.exists():
                issues.append("最终成片已存在")
            if issues:
                status = "validation_failed"
            grouped.setdefault(date_value, []).append(
                {
                    "taskId": task_id,
                    "generationIdentity": generation_identity,
                    "remotePipelineTaskId": remote_task_id,
                    "remotePipelineTaskIdSource": remote_task_id_source,
                    "formalTaskIdentity": bool(record.get("formalTaskIdentity")),
                    "canonicalTaskId": str(record.get("canonicalTaskId") or "").strip(),
                    "batchId": str(record.get("batchId") or "").strip(),
                    "expectedTaskCount": (
                        int(record.get("expectedTaskCount") or record.get("materialExpectedCount") or 0)
                        if remote_task_id else 0
                    ),
                    "date": date_value,
                    "productionDate": date_value,
                    "project": actual_project,
                    "material": material,
                    "language": language,
                    "languageCode": language_code,
                    "number": voice_number,
                    "sequence": sequence,
                    "videoPath": str(video_path.resolve()),
                    "audioPath": str(audio_path.resolve()),
                    "originalSamplePath": str(record.get("originalSamplePath") or "").strip(),
                    "sampleTargetSeconds": float(record.get("sampleTargetSeconds") or 0),
                    "channelAccount": str(record.get("channelAccount") or "").strip(),
                    "outputIndex": int(record.get("outputIndex") or sequence),
                    "executionProfileVersion": str(record.get("executionProfileVersion") or "").strip(),
                    "runGroupId": str(record.get("runGroupId") or "").strip(),
                    "parentCanonicalTaskId": str(record.get("parentCanonicalTaskId") or "").strip(),
                    # Written by the stitcher only for files picked from the subject-video folder.
                    # AI libraries, logos and sample libraries are deliberately not included.
                    "subtitleSourcePaths": subtitle_sources,
                    **subtitle_placement,
                    "finalOutputPath": str(final_path.resolve()),
                    "status": status,
                    "error": "；".join(issues),
                    "selected": status in RUNNABLE_STATUSES,
                    "generationRecord": str(record_path.resolve()) if record_path else "",
                }
            )

        added = 0
        for date_value, incoming in grouped.items():
            payload = self.load_date(date_value)
            # A restart/recovery can scan the same generation record again.
            # The formal task that already has a handoff manifest is the
            # authoritative one; a recovered, not-yet-handed-off duplicate
            # must never turn one video/audio pair into two CapCut jobs.
            self._drop_recovery_duplicates(payload)
            existing = {task.get("taskId"): task for task in payload["tasks"]}
            archived = payload.get("archivedOverproduction") or []
            archived_ids = {str(item.get("taskId") or "") for item in archived if isinstance(item, dict)}
            archived_keys = {
                self._generation_key(item)
                for item in archived
                if isinstance(item, dict) and self._generation_key(item) is not None
            }
            for task in incoming:
                # An archived overproduction row remains traceable but must
                # never be resurrected by a later startup recovery scan.
                if task.get("taskId") in archived_ids or self._generation_key(task) in archived_keys:
                    continue
                current = existing.get(task["taskId"])
                if current is None:
                    # The pre-generationIdentity client already wrote formal
                    # handoff rows with a different id.  Match that exact
                    # generation before considering legacy pathname fallback,
                    # so a later recovery cannot append a second CapCut job.
                    current = self._find_same_generation(existing.values(), task)
                    if current is not None:
                        task["taskId"] = current["taskId"]
                if current is None:
                    # Two workbench instances used to be able to write the
                    # same pending output into separate generation records a
                    # second apart.  While neither copy has a manifest or a
                    # confirmed final product, they are one delivery, not two
                    # CapCut jobs.
                    current = self._find_same_pending_delivery(existing.values(), task)
                    if current is not None:
                        task["taskId"] = current["taskId"]
                if current is None:
                    # Migration for records written before generationIdentity
                    # existed.  Reuse the old path-based id only when its
                    # manifest was created after this generation; otherwise a
                    # completed older run must stay separate from the retry.
                    legacy_identity = "|".join((
                        date_value, task["project"], task["material"], task["languageCode"],
                        task["number"], str(Path(task["videoPath"]).resolve()),
                    ))
                    legacy_id = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()[:24]
                    legacy = existing.get(legacy_id)
                    if legacy and not legacy.get("generationIdentity") and not self._is_completed_prior_generation(legacy, task):
                        task["taskId"] = legacy_id
                        current = legacy
                if current:
                    # A recovered record is the current source of truth for
                    # path validation.  Preserve only a confirmed exported
                    # result; retaining a historical validation_failed state
                    # prevents a fixed rule from ever making a remote task
                    # runnable again.
                    preserved = {}
                    if current.get("status") == "completed":
                        preserved = {
                            key: current.get(key)
                            for key in ("status", "error", "selected", "statusFile", "handoffManifest")
                            if current.get(key) not in (None, "")
                        }
                    # A formal handoff is the task owner of record.  Reused
                    # audio can carry an older *explicit* remote id in both
                    # its filename and a stale generation record; neither may
                    # overwrite the current task after a restart scan.
                    if (
                        bool(current.get("formalTaskIdentity"))
                        and not bool(task.get("formalTaskIdentity"))
                    ):
                        for key in (
                            "remotePipelineTaskId",
                            "remotePipelineTaskIdSource",
                            "formalTaskIdentity",
                            "canonicalTaskId",
                            "batchId",
                            "expectedTaskCount",
                            "date",
                            "productionDate",
                        ):
                            if key in current:
                                task[key] = current.get(key)
                    elif (
                        str(task.get("remotePipelineTaskIdSource") or "") == "inferred_media"
                        and str(current.get("remotePipelineTaskId") or "").strip()
                        and str(current.get("remotePipelineTaskIdSource") or "record") != "inferred_media"
                    ):
                        for key in (
                            "remotePipelineTaskId",
                            "remotePipelineTaskIdSource",
                            "expectedTaskCount",
                            "productionDate",
                        ):
                            if key in current:
                                task[key] = current.get(key)
                    task.update(preserved)
                    current.update(task)
                    if current.get("status") != "completed":
                        self._revalidate(current)
                else:
                    payload["tasks"].append(task)
                    existing[task["taskId"]] = task
                    added += 1
            payload["tasks"].sort(
                key=lambda item: (
                    CATEGORY_ORDER.get(item.get("project"), 99),
                    item.get("material", ""),
                    item.get("language", ""),
                    int(item.get("sequence") or 1),
                )
            )
            self.save_date(date_value, payload)
        return added

    @staticmethod
    def _generation_key(task: dict) -> tuple[str, str, str] | None:
        """Stable identity for one stitch generation, not merely a pathname."""
        record = str(task.get("generationRecord") or "").strip()
        video = str(task.get("videoPath") or "").strip()
        audio = str(task.get("audioPath") or "").strip()
        if not (record and video and audio):
            return None
        return record, video, audio

    @classmethod
    def _drop_recovery_duplicates(cls, payload: dict) -> None:
        """Keep the formal handoff copy when recovery rediscovered it.

        This changes only duplicate *manifest records*.  It never touches a
        source video, audio file, draft, final product or upload record.
        """
        grouped: dict[tuple[str, str, str], list[dict]] = {}
        untouched: list[dict] = []
        for task in payload.get("tasks", []):
            key = cls._generation_key(task)
            if key is None:
                untouched.append(task)
            else:
                grouped.setdefault(key, []).append(task)

        kept = list(untouched)
        for candidates in grouped.values():
            # A formal handoff is stronger evidence than a fresh recovery;
            # retain confirmed completion next, then the oldest stable id.
            candidates.sort(
                key=lambda task: (
                    0 if task.get("handoffManifest") else 1,
                    0 if task.get("status") == "completed" else 1,
                    str(task.get("taskId") or ""),
                )
            )
            kept.append(candidates[0])
        payload["tasks"] = kept

        # Generation-record paths are intentionally distinct for genuine
        # retries.  Pending copies of the *same* remote video/audio delivery
        # are not retries, however, and must collapse before any formal
        # expected-count check runs.
        pending: dict[tuple[str, str, str], list[dict]] = {}
        preserved: list[dict] = []
        for task in payload["tasks"]:
            owner = str(task.get("remotePipelineTaskId") or "").strip()
            video = str(task.get("videoPath") or "").strip()
            audio = str(task.get("audioPath") or "").strip()
            if (
                owner
                and video
                and audio
                and not task.get("handoffManifest")
                and str(task.get("status") or "") != "completed"
            ):
                pending.setdefault((owner, video, audio), []).append(task)
            else:
                preserved.append(task)
        for candidates in pending.values():
            candidates.sort(key=lambda task: str(task.get("generationIdentity") or task.get("taskId") or ""))
            preserved.append(candidates[0])
        payload["tasks"] = preserved

    @classmethod
    def _find_same_generation(cls, tasks, incoming: dict) -> dict | None:
        key = cls._generation_key(incoming)
        if key is None:
            return None
        matches = [task for task in tasks if cls._generation_key(task) == key]
        if not matches:
            return None
        matches.sort(
            key=lambda task: (
                0 if task.get("handoffManifest") else 1,
                0 if task.get("status") == "completed" else 1,
                str(task.get("taskId") or ""),
            )
        )
        return matches[0]

    @staticmethod
    def _find_same_pending_delivery(tasks, incoming: dict) -> dict | None:
        owner = str(incoming.get("remotePipelineTaskId") or "").strip()
        video = str(incoming.get("videoPath") or "").strip()
        audio = str(incoming.get("audioPath") or "").strip()
        if not (owner and video and audio):
            return None
        matches = [
            task
            for task in tasks
            if str(task.get("remotePipelineTaskId") or "").strip() == owner
            and str(task.get("videoPath") or "").strip() == video
            and str(task.get("audioPath") or "").strip() == audio
            and not task.get("handoffManifest")
            and str(task.get("status") or "") != "completed"
        ]
        if not matches:
            return None
        matches.sort(key=lambda task: str(task.get("generationIdentity") or task.get("taskId") or ""))
        return matches[0]

    @staticmethod
    def _is_completed_prior_generation(existing: dict, incoming: dict) -> bool:
        """Whether an old completed manifest predates this regenerated video."""
        if existing.get("status") != "completed":
            return False
        created = str(incoming.get("generationIdentity") or "").split("|", 2)
        if len(created) < 2 or not created[1]:
            return False
        try:
            incoming_at = datetime.fromisoformat(created[1])
            manifest_path = Path(str(existing.get("handoffManifest") or ""))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_at = datetime.fromisoformat(str(manifest.get("createdAt") or ""))
            return manifest_at < incoming_at
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def recover_generation_records(self, roots: list[Path]) -> int:
        added = 0
        seen: set[Path] = set()
        referenced_videos: set[Path] = set()
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("generation_records_*.json"), key=lambda item: item.name):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    records = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(records, list):
                    referenced_videos.update(
                        Path(str(record.get("output"))).resolve()
                        for record in records
                        if isinstance(record, dict) and record.get("output")
                    )
                    added += self.ingest_records("", records, path)
            for video in root.glob("*.mp4"):
                if video.resolve() not in referenced_videos:
                    added += self._ingest_manual_video(video)
        return added

    def archive_excess_pending_remote_tasks(
        self, date_value: str, remote_task_id: str, expected_count: int
    ) -> int:
        """Archive an unmistakable over-generation without touching media.

        This is deliberately narrow: it only acts when every row for an owner
        is still a manifest-free waiting task.  Any exported/running/failed
        row is evidence that human review is required instead.
        """
        expected = int(expected_count or 0)
        owner = str(remote_task_id or "").strip()
        if not (date_value and owner and expected > 0):
            return 0
        payload = self.load_date(date_value)
        # Preserve duplicate manifest rows for audit before collapsing them.
        # They are not media files and this never removes a source, draft or
        # final product.
        before = list(payload.get("tasks") or [])
        self._drop_recovery_duplicates(payload)
        retained_ids = {str(item.get("taskId") or "") for item in payload.get("tasks") or []}
        duplicate_rows = [
            item for item in before
            if str(item.get("taskId") or "") not in retained_ids
            and str(item.get("remotePipelineTaskId") or "").strip() == owner
        ]
        if duplicate_rows:
            now = datetime.now().isoformat(timespec="seconds")
            payload.setdefault("archivedDuplicateDeliveries", []).extend(
                {
                    **item,
                    "archivedAt": now,
                    "archiveReason": "同一远程视频/音频被不同生成记录重复交接，不进入第 5 步。",
                }
                for item in duplicate_rows
            )
        owned = [
            item for item in payload.get("tasks") or []
            if str(item.get("remotePipelineTaskId") or "").strip() == owner
        ]
        if len(owned) <= expected:
            if duplicate_rows:
                self.save_date(date_value, payload)
            return 0
        # A retry after midnight used to let the stitcher's legacy
        # ``datetime.now()`` output rule override the immutable production
        # date.  That produced a second generation for the same formal output
        # slots (for example sequence 1/2 under 7.29 for a 7.28 task).  When
        # the rows form exactly the expected set of formal slots, keep one
        # authoritative row per slot and archive only the duplicates.
        #
        # This is intentionally based on the formal language/voice/sequence
        # identity, not filename similarity.  A completed row wins; otherwise
        # the row physically stored under the production date wins, followed
        # by an existing formal handoff/checkpoint.
        slot_groups: dict[tuple[str, str, int], list[dict]] = {}
        for item in owned:
            try:
                sequence = int(item.get("sequence") or 0)
            except (TypeError, ValueError):
                sequence = 0
            slot = (
                str(item.get("languageCode") or item.get("language") or "").strip().casefold(),
                str(item.get("number") or "").strip(),
                sequence,
            )
            slot_groups.setdefault(slot, []).append(item)
        if len(slot_groups) == expected and all(slot != ("", "", 0) for slot in slot_groups):
            production_part = str(date_value).strip().casefold()

            def candidate_rank(item: dict):
                video_parts = {
                    str(part).strip().casefold()
                    for part in Path(str(item.get("videoPath") or "")).parts
                }
                return (
                    0 if str(item.get("status") or "") == "completed" else 1,
                    0 if production_part in video_parts else 1,
                    0 if item.get("handoffManifest") or item.get("statusFile") else 1,
                    0 if str(item.get("status") or "") == "waiting_client" else 1,
                    str(item.get("generationIdentity") or item.get("taskId") or ""),
                )

            keep_ids = set()
            archived = []
            for candidates in slot_groups.values():
                ordered = sorted(candidates, key=candidate_rank)
                keep_ids.add(str(ordered[0].get("taskId") or ""))
                archived.extend(ordered[1:])
            if archived:
                now = datetime.now().isoformat(timespec="seconds")
                history = payload.setdefault("archivedOverproduction", [])
                history.extend(
                    {
                        **item,
                        "archivedAt": now,
                        "archiveReason": (
                            f"正式任务目标为 {expected} 条；跨日或重复拼接产生了同一正式输出序号，"
                            f"该记录不进入第 5 步，生产日期仍为 {date_value}。"
                        ),
                    }
                    for item in archived
                )
                payload["tasks"] = [
                    item
                    for item in payload.get("tasks") or []
                    if str(item.get("taskId") or "") in keep_ids
                    or str(item.get("remotePipelineTaskId") or "").strip() != owner
                ]
                self.save_date(date_value, payload)
                return len(archived)
        # A completed formal set is stronger evidence than older pending
        # recovery rows from a different generation record.  Only migrate the
        # precise "N completed + stale waiting rows" shape; every ambiguous
        # mixture remains untouched for human review.
        completed = [item for item in owned if str(item.get("status") or "") == "completed"]
        if completed:
            if len(completed) != expected:
                return 0
            keep_ids = {str(item.get("taskId") or "") for item in completed}
            archived = [item for item in owned if str(item.get("taskId") or "") not in keep_ids]
            if any(str(item.get("status") or "") not in {"waiting_capcut", "waiting_client"} for item in archived):
                return 0
        else:
            if any(
                str(item.get("status") or "") != "waiting_capcut"
                or item.get("handoffManifest")
                or item.get("statusFile")
                for item in owned
            ):
                return 0
            ordered = sorted(
                owned,
                key=lambda item: (
                    int(item.get("sequence") or 0),
                    str(item.get("videoPath") or ""),
                    str(item.get("taskId") or ""),
                ),
            )
            keep_ids = {str(item.get("taskId") or "") for item in ordered[:expected]}
            archived = [item for item in owned if str(item.get("taskId") or "") not in keep_ids]
        if not archived:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        history = payload.setdefault("archivedOverproduction", [])
        history.extend(
            {
                **item,
                "archivedAt": now,
                "archiveReason": f"正式任务目标为 {expected} 条；该次拼接产生了 {len(owned)} 条，超额记录不进入第 5 步。",
            }
            for item in archived
        )
        payload["tasks"] = [
            item
            for item in payload.get("tasks") or []
            if str(item.get("taskId") or "") in keep_ids
            or str(item.get("remotePipelineTaskId") or "").strip() != owner
        ]
        self.save_date(date_value, payload)
        return len(archived)

    def _ingest_manual_video(self, video: Path) -> int:
        date_value = infer_date(video)
        project = next((name for name in CATEGORIES if name in video.stem), "")
        if not project:
            project = next((name for name in CATEGORIES if name in str(video.parent)), "")
        material = next((name for name in MATERIAL_NAMES if name in video.stem), "")
        language = next((name for name in LANGUAGE_NAMES.values() if name in video.stem), "")
        identity = "manual|" + str(video.resolve())
        task_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        payload = self.load_date(date_value)
        if any(task.get("taskId") == task_id for task in payload["tasks"]):
            return 0
        payload["tasks"].append(
            {
                "taskId": task_id,
                "date": date_value,
                "project": project,
                "material": material,
                "language": language,
                "languageCode": "",
                "number": last_number(video.stem),
                "sequence": 1,
                "videoPath": str(video.resolve()),
                "audioPath": "",
                "finalOutputPath": "",
                "status": "manual_review",
                "error": "旧视频没有第四步交接记录，请人工确认项目、材料、语言、音频和输出文件。",
                "selected": False,
                "generationRecord": "",
            }
        )
        self.save_date(date_value, payload)
        return 1

    def update_task(self, date_value: str, task_id: str, changes: dict) -> None:
        payload = self.load_date(date_value)
        for task in payload["tasks"]:
            if task.get("taskId") == task_id:
                task.update(changes)
                self._revalidate(task)
                break
        self.save_date(date_value, payload)

    def set_selected(self, date_value: str, task_ids: set[str], selected: bool) -> None:
        payload = self.load_date(date_value)
        for task in payload["tasks"]:
            if task.get("taskId") in task_ids and task.get("status") != "completed":
                task["selected"] = selected
        self.save_date(date_value, payload)

    def reassign_remote_owner(self, date_value: str, old_owner: str, new_owner: str) -> int:
        """Move durable task attribution without recreating media or tasks."""
        old_owner = str(old_owner or "").strip()
        new_owner = str(new_owner or "").strip()
        if not old_owner or not new_owner or old_owner == new_owner:
            return 0
        payload = self.load_date(date_value)
        changed = 0
        for task in payload.get("tasks") or []:
            if str(task.get("remotePipelineTaskId") or "").strip() != old_owner:
                continue
            task["remotePipelineTaskId"] = new_owner
            # Replenishment changes ownership only.  Its parent material count
            # is already durable and must never be replaced by a project total.
            changed += 1
        if changed:
            self.save_date(date_value, payload)
        return changed

    def _revalidate(self, task: dict) -> None:
        # A completed task may have its local source and final output removed
        # only after the cloud upload has been verified.  Revalidating those
        # disposable local paths later turns a genuine completed/uploaded item
        # into the misleading "视频不存在" failure shown by the Dashboard.
        # Completed is a terminal handoff result; upload owns any later cleanup.
        if str(task.get("status") or "").lower() == "completed":
            return
        issues = []
        if task.get("project") not in CATEGORIES:
            issues.append("无法识别项目")
        if not str(task.get("material") or "").strip():
            issues.append("缺少材料")
        if not str(task.get("language") or "").strip():
            issues.append("缺少语言")
        if not Path(str(task.get("videoPath") or "")).is_file():
            issues.append("视频不存在")
        if not Path(str(task.get("audioPath") or "")).is_file():
            issues.append("音频不存在")
        elif self._is_structured_remote_task(task):
            # Formal Fish handoffs carry their immutable task identity in the
            # manifest. The MP3 suffix (for example ``碳岩板-俄语-4``) is a
            # copy-library id, not the CapCut voice number, so never infer the
            # task number from this filename.
            pass
        elif bool(task.get("formalTaskIdentity")):
            # Do not silently fall back to filename heuristics when a record
            # claims to be formal but has lost its identity fields.
            issues.append("正式任务身份不完整")
        # Legacy local/remote-inbox rows still use the old filename-number
        # contract. Keep that guard for those rows only.
        elif (
            "remote-inbox-" not in Path(str(task.get("audioPath"))).stem
            and last_number(Path(str(task.get("audioPath"))).stem, "") != str(task.get("number") or "")
        ):
            issues.append("音频编号与任务编号不一致")
        output = Path(str(task.get("finalOutputPath") or ""))
        if output.is_file() and task.get("status") != "completed":
            issues.append("最终成片已存在")
        task["error"] = "；".join(issues)
        if issues:
            task["status"] = "validation_failed"
            task["selected"] = False
        elif task.get("status") in {"validation_failed", "manual_review"}:
            task["status"] = "waiting_capcut"
            task["selected"] = True

    @staticmethod
    def _is_structured_remote_task(task: dict) -> bool:
        """Return true only for a formal handoff with complete immutable identity."""
        if not bool(task.get("formalTaskIdentity")):
            return False
        required = (
            "canonicalTaskId",
            "batchId",
            "remotePipelineTaskId",
            "productionDate",
        )
        return all(str(task.get(key) or "").strip() for key in required)

    def create_manifest(
        self,
        date_value: str,
        task_ids: set[str],
        project_task_ids: set[str] | None = None,
        completed_task_ids: set[str] | None = None,
    ) -> Path:
        payload = self.load_date(date_value)
        selected = []
        for task in payload["tasks"]:
            if task.get("taskId") not in task_ids:
                continue
            self._revalidate(task)
            if task.get("status") not in RUNNABLE_STATUSES:
                continue
            selected.append(dict(task))
        self.save_date(date_value, payload)
        if not selected:
            raise ValueError("没有通过校验的已勾选任务。")
        # Every manifest must carry the immutable project contract, including
        # partial retry manifests.  GUI/manual entry points historically called
        # this method without the optional identity sets, producing a 19-item
        # manifest for a 26-item project.  Infer the contract before writing.
        project_task_ids = {str(item or "").strip() for item in (project_task_ids or set()) if str(item or "").strip()}
        completed_task_ids = {str(item or "").strip() for item in (completed_task_ids or set()) if str(item or "").strip()}
        selected_task_ids = {str(item.get("taskId") or "").strip() for item in selected if str(item.get("taskId") or "").strip()}
        owner = next(iter({str(task.get("remotePipelineTaskId") or "").strip() for task in selected}), "")
        expected = next(iter({int(task.get("expectedTaskCount") or 0) for task in selected}), 0)
        if not project_task_ids:
            # Legacy/local records may not have a formal receiver owner or
            # project target. They are complete manifests by construction and
            # retain the historical selected-task behavior. A declared target
            # count, however, must be reconstructed from the full persisted
            # project before a partial retry can be written.
            if expected <= 0:
                project_task_ids = set(selected_task_ids)
            else:
                candidates = [
                    task for task in payload["tasks"]
                    if str(task.get("remotePipelineTaskId") or "").strip() == owner
                    and int(task.get("expectedTaskCount") or 0) == expected
                    and str(task.get("taskId") or "").strip()
                ]
                inferred = {
                    str(task.get("taskId") or "").strip()
                    for task in candidates
                    if str(task.get("taskId") or "").strip()
                }
                if len(candidates) != expected or len(inferred) != expected:
                    raise ValueError(
                        "CAPCUT_PARTIAL_RETRY_CONTRACT_MISSING: 无法从正式任务表补全唯一项目任务身份。"
                    )
                project_task_ids = inferred
        if not completed_task_ids:
            completed_task_ids = {
                str(task.get("taskId") or "").strip()
                for task in payload["tasks"]
                if str(task.get("remotePipelineTaskId") or "").strip() == owner
                and str(task.get("taskId") or "").strip() in project_task_ids
                and str(task.get("status") or "").lower() == "completed"
            }
        if project_task_ids:
            if not completed_task_ids.issubset(project_task_ids):
                raise ValueError("已完成任务不属于当前正式项目，已阻止创建 CapCut 补跑清单。")
            if not selected_task_ids.issubset(project_task_ids):
                raise ValueError("补跑任务不属于当前正式项目，已阻止创建 CapCut 补跑清单。")
            if completed_task_ids & selected_task_ids:
                raise ValueError("已完成任务被重复加入 CapCut 补跑清单，已阻止创建。")
            if completed_task_ids | selected_task_ids != project_task_ids:
                raise ValueError(
                    "CAPCUT_PARTIAL_RETRY_CONTRACT_MISSING: 已完成任务与本次补跑任务未覆盖完整正式项目。"
                )
        owners = {str(task.get("remotePipelineTaskId") or "").strip() for task in selected}
        expected_counts = {int(task.get("expectedTaskCount") or 0) for task in selected}
        if len(owners) > 1:
            raise ValueError("一个 CapCut 清单不能混合多个远程项目。")
        if len(expected_counts) > 1:
            raise ValueError("一个 CapCut 清单不能混合多个正式目标数量。")
        output_paths = [str(Path(task["finalOutputPath"]).resolve()).casefold() for task in selected]
        if len(output_paths) != len(set(output_paths)):
            raise ValueError("已勾选任务存在重复的最终输出路径。")
        audio_numbers = {}
        for task in selected:
            audio_key = str(Path(task["audioPath"]).resolve()).casefold()
            known_number = audio_numbers.setdefault(audio_key, str(task.get("number") or ""))
            if known_number != str(task.get("number") or ""):
                raise ValueError("同一个音频被分配给了不同任务编号。")

        handoff_dir = self.root.parent / "handoff" / "capcut"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_id = f"{date_value}-{stamp}"
        manifest_id = f"capcut-{uuid.uuid4().hex}"
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        manifest = handoff_dir / f"{safe_name(date_value)}_启动批次_{stamp}.json"
        status_file = manifest.with_suffix(".status.json")
        content = {
            "schemaVersion": 2,
            # Cleanup v2 is opt-in at manifest creation time. Manifests made
            # before this field existed remain historical evidence and are
            # never swept automatically.
            "cleanupPolicyVersion": 2,
            "batchId": batch_id,
            "manifestId": manifest_id,
            "attemptId": attempt_id,
            "date": date_value,
            "productionDate": date_value,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "statusFile": str(status_file.resolve()),
            "remotePipelineTaskId": next(iter(owners), ""),
            "expectedTaskCount": next(iter(expected_counts), 0),
            "projectExpectedCount": next(iter(expected_counts), 0),
            "attemptTaskCount": len(selected),
            "projectTaskIds": sorted(project_task_ids),
            "completedTaskIds": sorted(completed_task_ids),
            "tasks": selected,
        }
        manifest.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        for task in payload["tasks"]:
            if task.get("taskId") in {item["taskId"] for item in selected}:
                task["status"] = "waiting_client"
                task["handoffManifest"] = str(manifest.resolve())
                task["statusFile"] = str(status_file.resolve())
                task["activeManifestId"] = manifest_id
                task["activeAttemptId"] = attempt_id
                task["currentPhase"] = "waiting_client"
                task["heartbeatAt"] = ""
        self.save_date(date_value, payload)
        return manifest

    def create_release_manifest(self, date_value: str, task_ids: set[str]) -> Path:
        """Create a lease-repair-only manifest for verified outputs.

        These rows already have a quality-passed final file.  The client must
        only reacquire/release the exact draft lease; importing media, opening
        CapCut, exporting, or replacing the output is forbidden.
        """
        payload = self.load_date(date_value)
        wanted = {str(value or "").strip() for value in task_ids if str(value or "").strip()}
        selected = []
        for task in payload.get("tasks") or []:
            if str(task.get("taskId") or "").strip() not in wanted:
                continue
            production = str(task.get("productionStatus") or "").lower()
            release = str(task.get("draftReleaseStatus") or "").lower()
            cleanup = str(task.get("draftCleanupStatus") or "").lower()
            quality = str(task.get("qualityStatus") or "").lower()
            output = Path(str(task.get("finalOutputPath") or ""))
            if str(task.get("status") or "").lower() != "completed":
                continue
            if production != "completed" or release not in {"pending", "release_pending", "failed"}:
                continue
            # A release-only manifest is allowed to touch a task only after
            # the final output has passed the same immutable quality gate as
            # normal upload.  Merely having a non-empty MP4 is not evidence
            # that production succeeded, and must never be enough to reopen
            # a CapCut lease.
            if quality != "passed":
                continue
            if cleanup not in {"pending", "release_pending", "failed", ""}:
                continue
            if not output.is_file() or output.stat().st_size <= 0:
                continue
            item = dict(task)
            item["releaseOnly"] = True
            selected.append(item)
        if not selected:
            raise ValueError("没有可修复草稿租约的已质检成片。")

        handoff_dir = self.root.parent / "handoff" / "capcut"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_id = f"{date_value}-release-{stamp}"
        manifest_id = f"capcut-release-{uuid.uuid4().hex}"
        attempt_id = f"attempt-release-{uuid.uuid4().hex}"
        manifest = handoff_dir / f"{safe_name(date_value)}_草稿恢复_{stamp}.json"
        status_file = manifest.with_suffix(".status.json")
        content = {
            "schemaVersion": 2,
            "releaseOnly": True,
            "cleanupPolicyVersion": 2,
            "batchId": batch_id,
            "manifestId": manifest_id,
            "attemptId": attempt_id,
            "date": date_value,
            "productionDate": date_value,
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "statusFile": str(status_file.resolve()),
            "remotePipelineTaskId": str(selected[0].get("remotePipelineTaskId") or ""),
            "expectedTaskCount": len(selected),
            "projectExpectedCount": len(selected),
            "attemptTaskCount": len(selected),
            "projectTaskIds": [str(item.get("taskId") or "") for item in selected],
            "completedTaskIds": [],
            "tasks": selected,
        }
        manifest.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        selected_ids = {str(item.get("taskId") or "") for item in selected}
        for task in payload.get("tasks") or []:
            if str(task.get("taskId") or "") not in selected_ids:
                continue
            task["status"] = "waiting_client"
            task["selected"] = False
            task["handoffManifest"] = str(manifest.resolve())
            task["statusFile"] = str(status_file.resolve())
            task["activeManifestId"] = manifest_id
            task["activeAttemptId"] = attempt_id
            task["currentPhase"] = "waiting_client"
            task["heartbeatAt"] = ""
        self.save_date(date_value, payload)
        return manifest

    def sync_statuses(self) -> int:
        changed = 0
        for date_value in self.dates():
            payload = self.load_date(date_value)
            by_id = {task.get("taskId"): task for task in payload["tasks"]}
            status_files = {
                Path(task["statusFile"])
                for task in payload["tasks"]
                if task.get("statusFile")
            }
            for path in status_files:
                try:
                    status = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                running = bool(status.get("running"))
                stale = _status_is_stale(status) if isinstance(status, dict) else False
                for item in status.get("tasks") or []:
                    task = by_id.get(item.get("taskId"))
                    if not task:
                        continue
                    # A task may be requeued after a failed client batch.
                    # Never let an older manifest overwrite that new attempt:
                    # only the task explicitly bound to this status file owns
                    # the update.
                    bound_status_file = str(task.get("statusFile") or "").strip()
                    if not bound_status_file or Path(bound_status_file).resolve() != path.resolve():
                        continue
                    active_manifest = str(task.get("activeManifestId") or "").strip()
                    active_attempt = str(task.get("activeAttemptId") or "").strip()
                    imported_manifest = str(item.get("manifestId") or status.get("manifestId") or "").strip()
                    imported_attempt = str(item.get("attemptId") or status.get("attemptId") or "").strip()
                    if active_manifest and imported_manifest and active_manifest != imported_manifest:
                        continue
                    if active_attempt and imported_attempt and active_attempt != imported_attempt:
                        continue
                    raw_state = str(item.get("state") or "")
                    # The client can retain a diagnostic from a manually
                    # stopped *previous* attempt after Retry Failed has put
                    # the task back in its Waiting state.  State is the
                    # authoritative queue contract: a Waiting task is
                    # runnable and must never be converted back to stopped
                    # merely because its old error text mentions "停止".
                    new_status = CAPCUT_STATE_STATUS.get(raw_state, "running" if running else task.get("status"))
                    if stale and new_status == "running":
                        new_status = "waiting_capcut"
                    imported_error = str(item.get("error") or "")
                    if raw_state == "Waiting" and "停止" in imported_error:
                        imported_error = ""
                    if stale:
                        imported_error = "CapCut 客户端心跳已过期；该条可安全继续。"
                        archives = payload.setdefault("archivedAttempts", [])
                        marker = (str(status.get("attemptId") or ""), str(task.get("taskId") or ""))
                        if not any((str(row.get("attemptId") or ""), str(row.get("taskId") or "")) == marker for row in archives if isinstance(row, dict)):
                            archives.append({
                                "attemptId": str(status.get("attemptId") or ""),
                                "manifestId": str(status.get("manifestId") or ""),
                                "taskId": str(task.get("taskId") or ""),
                                "archivedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "reason": "stale_heartbeat",
                            })
                    before = (
                        task.get("status"), task.get("error"), task.get("currentPhase"),
                        task.get("phaseText"), task.get("heartbeatAt"), task.get("clientPid"),
                        task.get("activeManifestId"), task.get("activeAttemptId"),
                        task.get("productionStatus"), task.get("draftReleaseStatus"),
                        task.get("draftCleanupStatus"), task.get("draftCleanupWarning"),
                        task.get("qualityStatus"), task.get("qualityReportPath"),
                        task.get("qualityCheckedAt"), task.get("qualityWarnings"),
                    )
                    task["currentPhase"] = raw_state or new_status
                    task["phaseText"] = str(item.get("stateText") or "")
                    task["heartbeatAt"] = str(status.get("heartbeatAt") or status.get("updatedAt") or "")
                    task["clientPid"] = int(status.get("clientPid") or 0)
                    task["activeManifestId"] = imported_manifest or active_manifest
                    task["activeAttemptId"] = imported_attempt or active_attempt
                    task["status"] = new_status
                    task["error"] = imported_error
                    # Production completion and CapCut draft housekeeping are
                    # independent facts.  A verified output whose draft root
                    # still needs release remains completed for upload, while
                    # the queue stays paused until the lease is repaired.
                    if "productionStatus" in item:
                        task["productionStatus"] = str(item.get("productionStatus") or "")
                    if "draftReleaseStatus" in item:
                        task["draftReleaseStatus"] = str(item.get("draftReleaseStatus") or "")
                    if "draftCleanupStatus" in item:
                        task["draftCleanupStatus"] = str(item.get("draftCleanupStatus") or "")
                    if "draftCleanupWarning" in item:
                        task["draftCleanupWarning"] = str(item.get("draftCleanupWarning") or "")
                    # Quality evidence is part of the immutable production
                    # result and must survive status normalization.  Without
                    # these fields the recovery selector sees a completed
                    # output, while create_release_manifest rejects the same
                    # row, leaving Safe Continue with no launchable manifest.
                    # A completed output keeps its recorded quality result
                    # when a later attempt (e.g. draft release only) reports
                    # none; on 2026-10-10 an empty value wiped "passed" and
                    # deadlocked the draft release.
                    keep_quality = (
                        not str(item.get("qualityStatus") or "").strip()
                        and bool(str(task.get("qualityStatus") or "").strip())
                        and str(task.get("productionStatus") or "").lower() == "completed"
                    )
                    if not keep_quality:
                        if "qualityStatus" in item:
                            task["qualityStatus"] = str(item.get("qualityStatus") or "")
                        if "qualityReportPath" in item:
                            task["qualityReportPath"] = str(item.get("qualityReportPath") or "")
                        if "qualityCheckedAt" in item:
                            task["qualityCheckedAt"] = str(item.get("qualityCheckedAt") or "")
                        if "qualityWarnings" in item:
                            task["qualityWarnings"] = list(item.get("qualityWarnings") or [])
                    if (str(task.get("productionStatus") or "").lower() == "completed" and
                            str(task.get("draftReleaseStatus") or "").lower() in {
                                "pending", "release_pending", "failed"
                            }):
                        # Do not let a post-production cleanup diagnostic turn
                        # a valid final video into a failed CapCut row.
                        task["status"] = "completed"
                        task["error"] = ""
                    task["selected"] = new_status in {"failed", "stopped"}
                    after = (
                        task.get("status"), task.get("error"), task.get("currentPhase"),
                        task.get("phaseText"), task.get("heartbeatAt"), task.get("clientPid"),
                        task.get("activeManifestId"), task.get("activeAttemptId"),
                        task.get("productionStatus"), task.get("draftReleaseStatus"),
                        task.get("draftCleanupStatus"), task.get("draftCleanupWarning"),
                        task.get("qualityStatus"), task.get("qualityReportPath"),
                        task.get("qualityCheckedAt"), task.get("qualityWarnings"),
                    )
                    if before != after:
                        changed += 1
            if changed:
                self.save_date(date_value, payload)
        return changed


def launch_capcut_client(executable: Path, manifest: Path) -> subprocess.Popen:
    executable = Path(executable)
    if not executable.is_file():
        raise FileNotFoundError(f"找不到 CapCut 客户端：{executable}")
    return subprocess.Popen(
        [str(executable), "--handoff", str(Path(manifest).resolve()), "--auto-start"],
        cwd=str(executable.parent),
    )
