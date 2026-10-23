"""Durable, idempotent status store shared by receiver and Dashboard."""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from common_io import atomic_write_json as atomic_json, now_iso


FFPROBE = Path(r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\AI_Subtitle_Backend\backend\ffmpeg\win_x64\ffprobe.exe")


def valid_output(path_value: str) -> bool:
    try:
        path = Path(path_value)
        if not path.is_file() or path.stat().st_size < 1024 or not FFPROBE.is_file():
            return False
        result = subprocess.run(
            [str(FFPROBE), "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_type,duration", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") if isinstance(payload, dict) else []
        return bool(streams and streams[0].get("codec_type") == "video" and float(streams[0].get("duration") or 0) > 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


class PipelineStatusStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["schemaVersion"] = 3
        payload.setdefault("sequence", 0)
        payload.setdefault("projects", {})
        payload.setdefault("batches", {})
        payload.setdefault("archivedProjects", {})
        payload.setdefault("events", [])
        payload.setdefault("seenEventIds", [])
        payload.setdefault("reconciledAt", "")
        payload.setdefault("reconciliationState", "unknown")
        payload.setdefault("reconciliationMessage", "等待接收器对账")
        return payload

    def _save(self) -> None:
        self.data["updatedAt"] = now_iso()
        atomic_json(self.path, self.data)

    def reset(self, reason: str = "dashboard_test_reset") -> dict[str, Any]:
        """Start a new empty status generation after records were archived."""
        with self.lock:
            self.data = {
                "schemaVersion": 4,
                "sequence": 0,
                "projects": {},
                "batches": {},
                "archivedProjects": {},
                "events": [],
                "seenEventIds": [],
                "reconciledAt": now_iso(),
                "reconciliationState": "healthy",
                "reconciliationMessage": f"测试记录已重置：{reason}",
            }
            self._save()
            return dict(self.data)

    def register_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = str(task.get("taskId") or "").strip()
        if not task_id:
            raise ValueError("taskId is required")
        with self.lock:
            archived = self.data.get("archivedProjects") or {}
            if task_id in archived:
                return dict(archived[task_id])
            project = self.data["projects"].setdefault(task_id, {})
            expected = max(0, int(task.get("expectedCount") or len(task.get("urls") or [])))
            videos = project.get("videos") if isinstance(project.get("videos"), dict) else {}
            seeded_failures = 0
            for failure in task.get("failureItems") or []:
                if not isinstance(failure, dict) or not str(failure.get("url") or ""):
                    continue
                url = str(failure["url"])
                videos.setdefault(url, {"url": url, "stage": "failed", "status": "failed",
                                               "error": str(failure.get("reason") or "处理失败"), "updatedAt": now_iso()})
                seeded_failures += 1
            existing_completed = max(int(project.get("completed") or 0), int(task.get("completed") or 0))
            existing_failed = max(int(project.get("failed") or 0), int(task.get("failed") or 0))
            baseline_completed = max(int(project.get("baselineCompleted") or 0), int(task.get("completed") or 0))
            baseline_failed = max(int(project.get("baselineFailed") or 0), max(0, int(task.get("failed") or 0) - seeded_failures))
            incoming_status = str(task.get("status") or "")
            authoritative = {"running", "failed", "completed", "blocked_missing_links", "paused"}
            selected_status = incoming_status if incoming_status in authoritative else str(project.get("status") or incoming_status or "waiting")
            selected_text = str(task.get("stateText") or "") if incoming_status in authoritative else str(project.get("stateText") or task.get("stateText") or "等待处理")
            project.update({
                "taskId": task_id,
                "batchId": str(task.get("batchId") or ""),
                "runGroupId": str(task.get("runGroupId") or project.get("runGroupId") or ""),
                "batchKind": str(task.get("batchKind") or project.get("batchKind") or "formal"),
                "selectedProjects": list(task.get("selectedProjects") or project.get("selectedProjects") or []),
                "batchSequence": int(task.get("batchSequence") or 1),
                "batchSize": int(task.get("batchSize") or 1),
                "date": str(task.get("date") or ""),
                "project": str(task.get("project") or ""),
                "material": str(task.get("material") or ""),
                "expectedCount": expected,
                "completed": existing_completed,
                "failed": existing_failed,
                "baselineCompleted": baseline_completed,
                "baselineFailed": baseline_failed,
                "remaining": max(0, expected - existing_completed),
                "currentTask": str(project.get("currentTask") or ""),
                "error": str(project.get("error") or ""),
                "status": selected_status,
                "stateText": selected_text,
                "pipelineStatus": str(task.get("pipelineStatus") or project.get("pipelineStatus") or ""),
                "pipelineStateText": str(task.get("pipelineStateText") or project.get("pipelineStateText") or ""),
                "pipelineUpdatedAt": str(task.get("pipelineUpdatedAt") or project.get("pipelineUpdatedAt") or ""),
                "currentPhase": str(task.get("currentPhase") or project.get("currentPhase") or ""),
                "attemptId": str(task.get("attemptId") or project.get("attemptId") or ""),
                "manifestId": str(task.get("manifestId") or project.get("manifestId") or ""),
                "videos": videos,
                "updatedAt": now_iso(),
            })
            self._rebuild_batch(project["batchId"])
            self._save()
            return dict(project)

    def apply(self, event: dict[str, Any]) -> dict[str, Any]:
        event_id = str(event.get("eventId") or "").strip()
        task_id = str(event.get("taskId") or "").strip()
        if not event_id or not task_id:
            raise ValueError("eventId and taskId are required")
        with self.lock:
            seen = set(self.data.get("seenEventIds") or [])
            if event_id in seen:
                return {"duplicate": True, "sequence": self.data["sequence"]}
            if task_id in (self.data.get("archivedProjects") or {}):
                self.data["sequence"] = int(self.data.get("sequence") or 0) + 1
                recorded = dict(event)
                recorded.update({
                    "sequence": self.data["sequence"],
                    "ignored": True,
                    "ignoreReason": "task_archived",
                    "receivedAt": now_iso(),
                })
                events = list(self.data.get("events") or [])
                events.append(recorded)
                self.data["events"] = events[-4000:]
                self.data["seenEventIds"] = (list(self.data.get("seenEventIds") or []) + [event_id])[-10000:]
                self._save()
                return {
                    "duplicate": False,
                    "ignored": True,
                    "sequence": self.data["sequence"],
                    "event": recorded,
                }
            if task_id not in self.data["projects"]:
                self.register_task(event)
            project = self.data["projects"][task_id]
            url = str(event.get("url") or "")
            stage = str(event.get("stage") or "waiting")
            status = str(event.get("status") or ("failed" if stage == "failed" else "running"))
            output_path = str(event.get("outputPath") or "")
            error = str(event.get("error") or event.get("message") or "") if status == "failed" else ""
            if stage == "complete":
                if not valid_output(output_path):
                    stage, status = "failed", "failed"
                    error = f"最终文件不存在或无效：{output_path or '未提供路径'}"
                else:
                    status = "completed"
            if url:
                video = project["videos"].setdefault(url, {"url": url})
                video.update({
                    "stage": stage, "status": status, "progress": event.get("progress"),
                    "message": str(event.get("message") or ""), "error": error,
                    "outputPath": output_path or str(video.get("outputPath") or ""), "updatedAt": now_iso(),
                })
            self.data["sequence"] = int(self.data.get("sequence") or 0) + 1
            recorded = dict(event)
            recorded["sequence"] = self.data["sequence"]
            recorded["stage"] = stage
            recorded["status"] = status
            recorded["error"] = error
            recorded["receivedAt"] = now_iso()
            events = list(self.data.get("events") or [])
            events.append(recorded)
            self.data["events"] = events[-4000:]
            self.data["seenEventIds"] = (list(self.data.get("seenEventIds") or []) + [event_id])[-10000:]
            self._refresh_project(project)
            self._rebuild_batch(str(project.get("batchId") or ""))
            self._save()
            return {"duplicate": False, "sequence": self.data["sequence"], "event": recorded}

    def _refresh_project(self, project: dict[str, Any]) -> None:
        videos = list((project.get("videos") or {}).values())
        completed = int(project.get("baselineCompleted") or 0) + sum(1 for item in videos if item.get("status") == "completed")
        failed = int(project.get("baselineFailed") or 0) + sum(1 for item in videos if item.get("status") == "failed")
        expected = max(int(project.get("expectedCount") or 0), len(videos))
        active = next((item for item in videos if item.get("status") == "running"), None)
        waiting = next((item for item in videos if item.get("status") == "waiting"), None)
        current = active or waiting or {}
        if completed == expected and expected:
            state, text = "completed", f"已完成 {completed} / {expected}"
        elif active:
            state, text = "running", f"{active.get('stage')}：{completed} / {expected}"
        elif failed:
            state, text = "failed", f"失败 {failed}，已完成 {completed} / {expected}"
        else:
            state, text = "waiting", f"等待处理：已完成 {completed} / {expected}"
        project.update({
            "completed": completed, "failed": failed, "remaining": max(0, expected - completed),
            "currentTask": str(current.get("url") or ""),
            "error": str(next((item.get("error") for item in videos if item.get("error")), "")),
            "status": state, "stateText": text, "updatedAt": now_iso(),
        })

    def _rebuild_batch(self, batch_id: str) -> None:
        if not batch_id:
            return
        projects = [item for item in self.data["projects"].values() if str(item.get("batchId") or "") == batch_id]
        if not projects:
            self.data["batches"].pop(batch_id, None)
            return
        expected = sum(int(item.get("expectedCount") or 0) for item in projects)
        completed = sum(int(item.get("completed") or 0) for item in projects)
        failed = sum(int(item.get("failed") or 0) for item in projects)
        current = next((item for item in projects if item.get("currentTask")), {})
        self.data["batches"][batch_id] = {
            "batchId": batch_id,
            "runGroupId": str(next((item.get("runGroupId") for item in projects if item.get("runGroupId")), "")),
            "batchKind": str(next((item.get("batchKind") for item in projects if item.get("batchKind")), "formal")),
            "selectedProjects": sorted({project for item in projects for project in (item.get("selectedProjects") or [])}),
            "batchSize": max(int(item.get("batchSize") or 1) for item in projects),
            "projectCount": len(projects), "total": expected, "completed": completed, "failed": failed,
            "remaining": max(0, expected - completed), "currentTask": str(current.get("currentTask") or ""),
            "error": str(next((item.get("error") for item in projects if item.get("error")), "")),
            "updatedAt": now_iso(),
        }

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: str = "cancelled",
        task: dict[str, Any] | None = None,
        archived_status: str = "cancelled",
    ) -> dict[str, Any]:
        """Archive one task without touching its media or event history."""
        task_id = str(task_id or "").strip()
        if not task_id:
            raise ValueError("taskId is required")
        with self.lock:
            archived_projects = self.data.setdefault("archivedProjects", {})
            existing = archived_projects.get(task_id)
            if isinstance(existing, dict):
                return {"archived": True, "duplicate": True, "project": dict(existing)}
            active = self.data.setdefault("projects", {}).pop(task_id, None)
            source = dict(active) if isinstance(active, dict) else dict(task or {})
            source.update({
                "taskId": task_id,
                "status": archived_status,
                "archiveReason": str(reason or archived_status),
                "archivedAt": now_iso(),
                "cancelledAt": str((task or {}).get("cancelledAt") or "") if archived_status == "cancelled" else "",
                "updatedAt": now_iso(),
            })
            archived_projects[task_id] = source
            batch_id = str(source.get("batchId") or "")
            if batch_id:
                self._rebuild_batch(batch_id)
            self._save()
            return {"archived": True, "duplicate": False, "project": dict(source)}

    def reconcile(self, task_records: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply authoritative cancellation/merge records from task folders."""
        cancelled = 0
        merged = 0
        with self.lock:
            try:
                previous_state = str(self.data.get("reconciliationState") or "")
                previous_reconciled_at = str(self.data.get("reconciledAt") or "")
                changed_active = False
                for raw in task_records:
                    if not isinstance(raw, dict):
                        continue
                    task_id = str(raw.get("taskId") or "").strip()
                    if not task_id:
                        continue
                    status = str(raw.get("status") or "").strip().lower()
                    merged_into = str(raw.get("mergedIntoTaskId") or raw.get("replenishForTaskId") or "").strip()
                    if status == "cancelled":
                        result = self.cancel_task(
                            task_id,
                            reason=str(raw.get("cancelReason") or "cancelled"),
                            task=raw,
                        )
                        cancelled += 0 if result.get("duplicate") else 1
                    elif status == "merged_into_parent" or (merged_into and status.startswith("merged")):
                        result = self.cancel_task(
                            task_id,
                            reason=f"merged_into_parent:{merged_into}",
                            task=raw,
                            archived_status="merged_into_parent",
                        )
                        merged += 0 if result.get("duplicate") else 1
                    elif task_id in self.data.get("projects", {}):
                        project = self.data["projects"][task_id]
                        authority = str(raw.get("_authoritySource") or "task_file")
                        if authority == "task_file":
                            # Durable identity comes only from the original
                            # parent task record, never from a replenishment
                            # progress snapshot.
                            for key in (
                                "batchId", "runGroupId", "batchKind", "selectedProjects", "batchSequence", "batchSize", "date",
                                "project", "material", "expectedCount",
                                "productionDate", "contractVersion", "languagePlan",
                            ):
                                if raw.get(key) not in (None, "") and project.get(key) != raw.get(key):
                                    project[key] = raw.get(key)
                                    changed_active = True
                        elif authority == "workbench_project_status":
                            expected = max(0, int(project.get("expectedCount") or raw.get("expectedCount") or 0))
                            completed = min(expected, max(0, int(raw.get("completed") or 0))) if expected else 0
                            failed = max(0, int(raw.get("failed") or 0))
                            progress_status = str(raw.get("status") or "")
                            if expected and completed >= expected:
                                progress_status = "completed"
                                failed = 0
                            progress = {
                                "completed": completed,
                                "failed": failed,
                                "remaining": max(0, expected - completed),
                                "status": progress_status or str(project.get("status") or "waiting"),
                                "stateText": str(raw.get("stateText") or project.get("stateText") or ""),
                                "currentTask": str(raw.get("currentTask") or ""),
                                "error": "" if progress_status == "completed" else str(raw.get("error") or ""),
                                "gapActive": bool(raw.get("gapActive")) and progress_status != "completed",
                                "terminal": bool(raw.get("terminal")) or progress_status in {"completed", "blocked_missing_links"},
                                "notStarted": bool(raw.get("notStarted")),
                                "gapRevision": int(raw.get("gapRevision") or 0),
                                # These are production stages authored by the
                                # workbench.  Keep them independent from the
                                # Step 1/2 download counters above.
                                "pipelineStatus": str(raw.get("pipelineStatus") or project.get("pipelineStatus") or ""),
                                "pipelineStateText": str(raw.get("pipelineStateText") or project.get("pipelineStateText") or ""),
                                "pipelineUpdatedAt": str(raw.get("pipelineUpdatedAt") or project.get("pipelineUpdatedAt") or ""),
                                "currentPhase": str(raw.get("currentPhase") or project.get("currentPhase") or ""),
                                "attemptId": str(raw.get("attemptId") or project.get("attemptId") or ""),
                                "manifestId": str(raw.get("manifestId") or project.get("manifestId") or ""),
                                "runGroupId": str(raw.get("runGroupId") or project.get("runGroupId") or ""),
                                "batchKind": str(raw.get("batchKind") or project.get("batchKind") or "formal"),
                                "selectedProjects": list(raw.get("selectedProjects") or project.get("selectedProjects") or []),
                            }
                            for key, value in progress.items():
                                if project.get(key) != value:
                                    project[key] = value
                                    changed_active = True
                            project["updatedAt"] = str(raw.get("updatedAt") or now_iso())
                            self._rebuild_batch(str(project.get("batchId") or ""))
                if cancelled or merged or changed_active or previous_state != "healthy" or not previous_reconciled_at:
                    self.data["schemaVersion"] = 4
                    self.data["reconciledAt"] = now_iso()
                    self.data["reconciliationState"] = "healthy"
                    self.data["reconciliationMessage"] = (
                        f"对账完成：迁入撤回历史 {cancelled} 项，合并子任务 {merged} 项"
                    )
                    self._save()
                return {
                    "state": "healthy",
                    "reconciledAt": str(self.data.get("reconciledAt") or ""),
                    "cancelled": cancelled,
                    "merged": merged,
                    "sequence": int(self.data.get("sequence") or 0),
                }
            except Exception as exc:
                self.data["reconciledAt"] = now_iso()
                self.data["reconciliationState"] = "failed"
                self.data["reconciliationMessage"] = str(exc)
                self._save()
                raise

    def mark_reconciliation_failed(self, message: str) -> None:
        with self.lock:
            self.data["schemaVersion"] = 3
            self.data["reconciledAt"] = now_iso()
            self.data["reconciliationState"] = "failed"
            self.data["reconciliationMessage"] = str(message or "接收器对账失败")
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            payload = json.loads(json.dumps(self.data, ensure_ascii=False))
            payload["schemaVersion"] = 3
            payload["servedAt"] = now_iso()
            return payload

    def summary(self) -> dict[str, Any]:
        """Small Dashboard payload: never make a phone poll 4,000 events."""
        with self.lock:
            projects = []
            for item in (self.data.get("projects") or {}).values():
                if not isinstance(item, dict):
                    continue
                projects.append({key: item.get(key) for key in (
                    "taskId", "batchId", "runGroupId", "batchKind", "selectedProjects", "batchSequence", "batchSize", "date", "productionDate",
                    "project", "material", "expectedCount", "completed", "failed", "remaining",
                    "status", "stateText", "pipelineStatus", "pipelineStateText", "pipelineUpdatedAt",
                    "currentPhase", "attemptId", "manifestId", "error", "updatedAt", "gapActive",
                )})
            return {
                "schemaVersion": 3,
                "sequence": int(self.data.get("sequence") or 0),
                "servedAt": now_iso(),
                "updatedAt": str(self.data.get("updatedAt") or ""),
                "reconciledAt": str(self.data.get("reconciledAt") or ""),
                "reconciliationState": str(self.data.get("reconciliationState") or "unknown"),
                "reconciliationMessage": str(self.data.get("reconciliationMessage") or ""),
                "batches": json.loads(json.dumps(self.data.get("batches") or {}, ensure_ascii=False)),
                "archivedProjects": json.loads(json.dumps(self.data.get("archivedProjects") or {}, ensure_ascii=False)),
                "projects": projects,
            }

    def events_after(self, sequence: int) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in self.data.get("events") or [] if int(item.get("sequence") or 0) > sequence]
