"""Tailscale-friendly task receiver.

It only stores confirmed JSON tasks in the workbench inbox.  It never starts
TikHub or any downstream production client.
"""
from __future__ import annotations

import html
import hashlib
import hmac
import json
import ctypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
import uvicorn

from common_io import pid_is_alive as _pid_is_alive
from task_inbox import (
    APP_DIR,
    CANCELLED_DIR,
    IMPORTED_DIR,
    INBOX_DIR,
    atomic_write_json,
    _json_file_lock,
    cancel_imported_task,
    cancel_pending_task,
    create_task,
    list_pending_tasks,
    load_project_download_statuses,
    load_templates,
    request_pending_start,
    submit_task_batch,
    submit_task,
    ensure_pipeline_profile,
    validate_batch_voice_intent,
    validate_task,
)
from pipeline_status_bus import PipelineStatusStore
from examplebrand_contract import ExampleBrand_BATCH_KIND, validate_examplebrand_batch_payload
from task_contract import (
    CONTRACT_VERSION,
    FORMAL_BATCH_TOTAL,
    PROJECT_EXPECTED_TOTALS,
    assign_batch_contracts,
    normalize_selected_projects,
    validate_formal_submission,
)


CONFIG_FILE = APP_DIR / "task_receiver_config.json"
DEFAULT_CONFIG = {
    "listenHost": "0.0.0.0",
    "listenPort": 8766,
    "accessToken": "",
    "machineName": "任务接收器",
}


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.is_file():
            config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        else:
            # Blank token is intentional for the same-computer test.  Before
            # accepting a remote sender, set one in this local-only file.
            atomic_write_json(CONFIG_FILE, config)
    except (OSError, ValueError):
        pass
    return config


CONFIG = load_config()
app = FastAPI(title="AI视频生产线任务接收器", docs_url=None, redoc_url=None)
PIPELINE_STATUS_FILE = APP_DIR / "handoff" / "status" / "pipeline_snapshot.json"
PIPELINE_STORE = PipelineStatusStore(PIPELINE_STATUS_FILE)
PIPELINE_OUTBOX_DIR = APP_DIR / "handoff" / "status" / "outbox"
TEST_RESET_BACKUP_DIR = APP_DIR / "handoff" / "test_reset_backups"
SUBMISSION_RECEIPT_DIR = APP_DIR / "handoff" / "submissions"
SUBMISSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _test_record_files() -> list[Path]:
    """Return record files only; never media, settings, libraries or calibration."""
    candidates: list[Path] = []
    for directory in (INBOX_DIR, IMPORTED_DIR, CANCELLED_DIR):
        candidates.extend(directory.glob("*.json"))
    for relative in (
        "handoff/download/*.json",
        "handoff/download/checkpoints/*.json",
        "handoff/status/*.json",
        "handoff/status/outbox/*.json",
        "handoff/capcut/*.json",
        "handoff/control/*.json",
        "handoff/upload/*.json",
        "handoff/upload/*.pid",
        "交接记录/*/*.json",
    ):
        candidates.extend(APP_DIR.glob(relative))
    candidates.extend((
        APP_DIR / "handoff" / "current_pipeline_context.json",
        APP_DIR / "ops_pipeline_status.json",
        APP_DIR.parent / "voice-client" / "data" / "last_batch_handoff.json",
        APP_DIR.parent / "voice-client" / "data" / "workbench_handoff_state.json",
    ))
    try:
        settings = json.loads((APP_DIR / "workbench_settings.json").read_text(encoding="utf-8"))
        client = Path(str(settings.get("capcut_client") or ""))
        if client.is_file():
            candidates.append(client.parent / "data" / "capcut-tasks.json")
    except (OSError, ValueError):
        pass
    unique: dict[str, Path] = {}
    for path in candidates:
        if path.is_file():
            unique[str(path.resolve()).lower()] = path.resolve()
    return list(unique.values())


def archive_and_reset_test_records() -> dict[str, Any]:
    """Archive every runtime record and initialize one empty test generation."""
    reset_id = datetime.now().astimezone().strftime("reset-%Y%m%d-%H%M%S")
    backup_root = (TEST_RESET_BACKUP_DIR / reset_id).resolve()
    backup_root.mkdir(parents=True, exist_ok=False)
    moved: list[str] = []
    suite_root = APP_DIR.parent.resolve()
    for source in _test_record_files():
        resolved = source.resolve()
        try:
            relative = resolved.relative_to(suite_root)
        except ValueError:
            # The configured CapCut client is a separate repository.  Keep its
            # queue under a clearly named subtree in the same reset backup.
            relative = Path("external-capcut") / resolved.name
        destination = backup_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"测试记录备份目标已存在：{destination}")
        shutil.move(str(resolved), str(destination))
        moved.append(str(relative))

    empty_status = {
        "schemaVersion": 1,
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": {},
    }
    atomic_write_json(APP_DIR / "handoff" / "download" / "project_status.json", empty_status)
    PIPELINE_STORE.reset(reset_id)

    # Keep the deployed client ready to accept a new manifest after the reset.
    try:
        settings = json.loads((APP_DIR / "workbench_settings.json").read_text(encoding="utf-8"))
        client = Path(str(settings.get("capcut_client") or ""))
        if client.is_file():
            queue_file = client.parent / "data" / "capcut-tasks.json"
            queue_file.parent.mkdir(parents=True, exist_ok=True)
            queue_file.write_text("[]\n", encoding="utf-8")
    except (OSError, ValueError):
        pass
    return {
        "ok": True,
        "resetId": reset_id,
        "archivedRecordCount": len(moved),
        "mediaDeleted": False,
        "message": f"已备份并清空 {len(moved)} 个测试记录；媒体、配置与校准均保留",
    }


def _reconciliation_task_records() -> list[dict[str, Any]]:
    """Read task-file authority without changing or deleting any task/media."""
    records: list[dict[str, Any]] = []
    for directory in (INBOX_DIR, IMPORTED_DIR, CANCELLED_DIR):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["_authoritySource"] = "task_file"
                records.append(payload)
    # The task files are authoritative for identity/batch/date, while this
    # status file is authoritative for actual step 1/2 completion (including
    # a replenishment merged back into its parent).  Keeping these roles
    # separate prevents a one-item replenishment batch from replacing the
    # parent's original 9-item task-box identity.
    status_file = APP_DIR / "handoff" / "download" / "project_status.json"
    try:
        status_payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status_payload = {}
    status_items = status_payload.get("items") if isinstance(status_payload, dict) else {}
    if isinstance(status_items, dict):
        for task_id, item in status_items.items():
            if not isinstance(item, dict):
                continue
            record = dict(item)
            record["taskId"] = str(record.get("taskId") or task_id)
            record["_authoritySource"] = "workbench_project_status"
            records.append(record)
    return records


def reconcile_pipeline_store() -> dict[str, Any]:
    """Make cancelled/merged task files authoritative for the status bus."""
    try:
        return PIPELINE_STORE.reconcile(_reconciliation_task_records())
    except Exception as exc:
        PIPELINE_STORE.mark_reconciliation_failed(str(exc))
        return {"state": "failed", "message": str(exc)}


def replay_pipeline_outbox() -> int:
    """Recover bridge events that were written while the receiver was offline."""
    delivered = 0
    for path in sorted(PIPELINE_OUTBOX_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pending = payload.get("pending") if isinstance(payload, dict) else None
        if not isinstance(pending, list):
            continue
        remaining = []
        for event in pending:
            try:
                PIPELINE_STORE.apply(event)
                delivered += 1
            except (OSError, ValueError):
                remaining.append(event)
        if len(remaining) != len(pending):
            payload["pending"] = remaining
            atomic_write_json(path, payload)
    return delivered
# A page-only capability protects the small management action without asking the
# phone user to type the shared sender secret.  It expires whenever the receiver
# restarts, and it permits cancelling pending records only.
PAGE_ACTION_SECRET = secrets.token_bytes(32)
WORKBENCH_WINDOW_TITLE = "TikHub AI 视频工作台"
WORKBENCH_INSTANCE_FILE = APP_DIR / "handoff" / "control" / "workbench.pid"
WORKBENCH_LAUNCH_FILE = APP_DIR / "handoff" / "control" / "workbench.launching"
WORKBENCH_READY_FILE = APP_DIR / "handoff" / "control" / "workbench.ready.json"
WORKBENCH_START_LOG_FILE = APP_DIR / "handoff" / "control" / "workbench_start.log"
WORKBENCH_START_LOCK = threading.Lock()


class WorkbenchStartupError(OSError):
    """A structured startup failure safe to return to the local Dashboard."""

    def __init__(self, code: str, message: str, *, log_file: Path | None = None):
        super().__init__(message)
        self.code = code
        self.log_file = log_file


def _is_loopback(request: Request) -> bool:
    return (request.client.host if request.client else "") in {"127.0.0.1", "::1"}


def _require_sender_permission(request: Request) -> None:
    if _is_loopback(request):
        return
    token = str(CONFIG.get("accessToken") or "")
    supplied = request.headers.get("X-Task-Token", "")
    if not token or not secrets.compare_digest(token, supplied):
        raise HTTPException(status_code=403, detail="任务接收器拒绝远程写入：请配置访问密钥。")


def _page_capability(action: str, task_id: str) -> str:
    message = f"{action}:{task_id}".encode("utf-8")
    return hmac.new(PAGE_ACTION_SECRET, message, "sha256").hexdigest()


def _require_page_capability(action: str, task_id: str, capability: str) -> None:
    expected = _page_capability(action, task_id)
    if not capability or not hmac.compare_digest(expected, capability):
        raise HTTPException(status_code=403, detail="撤销页面已失效，请刷新后重试。")


def _workbench_window_pid() -> int:
    try:
        handle = ctypes.windll.user32.FindWindowW(None, WORKBENCH_WINDOW_TITLE)
        if not handle:
            return 0
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return int(pid.value)
    except (AttributeError, OSError):
        return 0


def _registered_workbench_pid() -> int:
    try:
        pid = int(WORKBENCH_INSTANCE_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0
    if _pid_is_alive(pid):
        return pid
    try:
        WORKBENCH_INSTANCE_FILE.unlink()
    except OSError:
        pass
    return 0


def _remove_file_quietly(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _read_workbench_ready() -> dict[str, Any]:
    """Return only a fresh readiness record owned by a live workbench process."""
    try:
        payload = json.loads(WORKBENCH_READY_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        _remove_file_quietly(WORKBENCH_READY_FILE)
        return {}
    try:
        pid = int(payload.get("pid") or 0)
        heartbeat = float(payload.get("heartbeatEpoch") or 0)
    except (TypeError, ValueError):
        pid, heartbeat = 0, 0.0
    if (
        str(payload.get("state") or "") != "ready"
        or not _pid_is_alive(pid)
        or heartbeat <= 0
        or time.time() - heartbeat > 20
    ):
        _remove_file_quietly(WORKBENCH_READY_FILE)
        return {}
    payload["pid"] = pid
    return payload


def _clear_stale_workbench_registration() -> None:
    """A PID alone is not proof of a usable workbench; clear stale records."""
    registered = _registered_workbench_pid()
    if not registered:
        return
    ready = _read_workbench_ready()
    window_pid = _workbench_window_pid()
    if (ready and int(ready.get("pid") or 0) == registered) or (
        window_pid == registered and _pid_is_alive(window_pid)
    ):
        return
    _remove_file_quietly(WORKBENCH_INSTANCE_FILE)
    _remove_file_quietly(WORKBENCH_READY_FILE)


def _existing_workbench() -> tuple[int, bool]:
    _clear_stale_workbench_registration()
    ready = _read_workbench_ready()
    if ready:
        return int(ready["pid"]), True
    window_pid = _workbench_window_pid()
    if window_pid and _pid_is_alive(window_pid):
        return window_pid, False
    return 0, False


def _workbench_runtime_candidates() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA") or "")
    return [
        Path(r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\.cuda_env\Scripts\python.exe"),
        local / "Programs" / "Python" / "Python313" / "python.exe",
        local / "Programs" / "Python" / "Python313" / "pythonw.exe",
        local / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe",
        local / "hermes" / "hermes-agent" / "venv" / "Scripts" / "pythonw.exe",
    ]


def _resolve_workbench_runtime() -> Path:
    for runtime in _workbench_runtime_candidates():
        if runtime.is_file():
            return runtime
    raise WorkbenchStartupError(
        "runtime_not_found",
        "未找到工作台 Python 运行时（CUDA Python、Python 3.13、Hermes 均不可用）。",
    )


def _write_workbench_start_log(message: str) -> Path:
    WORKBENCH_START_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with WORKBENCH_START_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass
    return WORKBENCH_START_LOG_FILE


def _claim_workbench_launch() -> bool:
    """Use a durable short-lived claim so receiver restarts cannot double-launch."""
    WORKBENCH_LAUNCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            descriptor = os.open(
                str(WORKBENCH_LAUNCH_FILE),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            try:
                age = time.time() - WORKBENCH_LAUNCH_FILE.stat().st_mtime
            except OSError:
                age = 0
            if age < 30:
                return False
            try:
                WORKBENCH_LAUNCH_FILE.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{os.getpid()}\n{time.time():.6f}\n")
        return True
    return False


def _release_workbench_launch_claim() -> None:
    try:
        WORKBENCH_LAUNCH_FILE.unlink()
    except FileNotFoundError:
        pass


def ensure_workbench_running() -> dict[str, Any]:
    """Directly launch the chosen runtime and wait for a workbench heartbeat."""
    with WORKBENCH_START_LOCK:
        pid, ready = _existing_workbench()
        if pid:
            _release_workbench_launch_claim()
            return {
                "alreadyRunning": True,
                "started": False,
                "ready": ready,
                "pid": pid,
                "message": "工作台已运行，继续使用现有实例。" if ready else "工作台窗口已运行，正在等待就绪心跳。",
            }
        if not _claim_workbench_launch():
            return {
                "alreadyRunning": False,
                "started": False,
                "startPending": True,
                "ready": False,
                "pid": 0,
                "message": "工作台启动请求已发出，正在等待就绪状态。",
            }
        script = APP_DIR / "workbench.py"
        if not script.is_file():
            _release_workbench_launch_claim()
            raise WorkbenchStartupError("script_not_found", f"找不到工作台脚本：{script}")
        runtime = _resolve_workbench_runtime()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen([str(runtime), str(script)], cwd=str(APP_DIR), creationflags=flags)
        except OSError as exc:
            _release_workbench_launch_claim()
            log = _write_workbench_start_log(f"launch_failed runtime={runtime}: {exc}")
            raise WorkbenchStartupError("launch_failed", f"无法启动工作台运行时：{exc}", log_file=log) from exc
        process_pid = int(getattr(process, "pid", 0) or 0)
        for _ in range(80):
            time.sleep(0.25)
            ready_payload = _read_workbench_ready()
            if ready_payload:
                _release_workbench_launch_claim()
                return {
                    "alreadyRunning": False,
                    "started": True,
                    "ready": True,
                    "pid": int(ready_payload["pid"]),
                    "sessionId": str(ready_payload.get("sessionId") or ""),
                    "message": "工作台已启动并确认就绪。",
                }
            if process.poll() is not None:
                _release_workbench_launch_claim()
                log = _write_workbench_start_log(
                    f"process_exited pid={process_pid} exit={process.returncode} runtime={runtime}"
                )
                raise WorkbenchStartupError(
                    "process_exited",
                    f"工作台启动后立即退出（退出码 {process.returncode}）。",
                    log_file=log,
                )
        _release_workbench_launch_claim()
        log = _write_workbench_start_log(f"ready_timeout pid={process_pid} runtime={runtime}")
        raise WorkbenchStartupError(
            "ready_timeout",
            "工作台在 20 秒内未写入就绪心跳。",
            log_file=log,
        )


def _start_workbench_if_needed() -> bool:
    """Launch the local workbench once; it will claim the queued first task."""
    return bool(ensure_workbench_running().get("started"))


LOGIN_STARTUP = False


AUTO_SEQUENCE_MODES = {"auto_start_first", "auto_start_sequential"}


def _has_auto_start_task() -> bool:
    return any(
        task.get("startMode") in AUTO_SEQUENCE_MODES and int(task.get("batchSequence") or 1) == 1
        for _, task in list_pending_tasks(INBOX_DIR)
    )


def _imported_tasks() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in IMPORTED_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and str(record.get("status") or "").lower() not in {
            "cancelled", "canceled", "merged_into_parent",
        }:
            records.append(record)
    return records


def read_tasks() -> list[dict[str, Any]]:
    pending = [dict(task) for _, task in list_pending_tasks(INBOX_DIR)]
    return pending + _imported_tasks()


STATUS_LABELS = {
    "pending_confirmation": "待确认",
    "imported_waiting_start": "已导入，等待开始",
    "waiting_download": "等待下载",
    "downloading": "正在下载",
    "subtitle": "正在去字幕",
    "waiting_voice": "等待语音",
    "stitching": "正在拼接",
    "capcut": "CapCut 剪辑中",
    "uploading": "正在上传",
    "uploaded": "上传完成",
    "failed": "失败",
}

VOICE_PLAN_LABELS = {
    "create_new": "新建语音",
    "reuse_existing": "复用已有语音",
}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "machine": CONFIG.get("machineName"),
        "pending": len(list_pending_tasks(INBOX_DIR)),
        "contractVersion": CONTRACT_VERSION,
        "formalBatchTotal": FORMAL_BATCH_TOTAL,
        "projectTotals": dict(PROJECT_EXPECTED_TOTALS),
    }


@app.on_event("startup")
def launch_workbench_for_existing_auto_task() -> None:
    """Open the workbench on login, or recover a pending auto-start request."""
    try:
        replay_pipeline_outbox()
        for task in read_tasks():
            PIPELINE_STORE.register_task(task)
        # Reconcile only after task-file identities have been registered.
        # Workbench progress then updates counts without being allowed to
        # replace batchId/batchSize with a one-item replenishment batch.
        reconcile_pipeline_store()
    except (OSError, FileNotFoundError):
        # The pending task remains visible and can be started after the local
        # launcher is repaired; never discard it merely because launch failed.
        pass
    try:
        if LOGIN_STARTUP or _has_auto_start_task():
            _start_workbench_if_needed()
    except OSError as exc:
        _write_workbench_start_log(f"startup_failed login={LOGIN_STARTUP}: {exc}")
        # Keep all task/checkpoint records; the receiver remains available.


@app.get("/api/tasks")
def tasks():
    return {"items": read_tasks()}


@app.get("/api/templates")
def templates():
    """Return the non-secret task presets used by every submitter."""
    items = []
    for template in load_templates().get("templates", []):
        if not isinstance(template, dict):
            continue
        items.append({
            "id": template.get("id"),
            "label": template.get("label"),
            "project": template.get("project"),
            "material": template.get("material"),
            "materials": template.get("materials") or [],
            "expectedCount": template.get("expectedCount"),
        })
    return {"items": items}


def _submit_dashboard_task(task: dict[str, Any], *, start: bool) -> dict[str, Any]:
    """Store one web-originated task with the same batch shape as desktop."""
    task = dict(task)
    task_id = str(task["taskId"])
    task["batchId"] = f"web-{task_id}"
    task["batchSequence"] = 1
    task["batchSize"] = 1
    task["startMode"] = "auto_start_first" if start else "pending_confirmation"
    destination = submit_task(task, INBOX_DIR)
    PIPELINE_STORE.register_task(task)
    launch_requested = False
    if start:
        launch_requested = _start_workbench_if_needed()
    return {
        "ok": True,
        "taskId": task_id,
        "batchId": task["batchId"],
        "file": destination.name,
        "workbenchLaunchRequested": launch_requested,
    }


@app.post("/api/tasks/create")
async def create_dashboard_task(request: Request):
    """Create one normal project task from Dashboard without exposing a token."""
    _require_sender_permission(request)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("任务不是有效 JSON")
        task = create_task(
            str(payload.get("templateId") or ""),
            payload.get("urls") or [],
            date_label=str(payload.get("date") or "").strip(),
            material=str(payload.get("material") or "").strip() or None,
            audio_batch=str(payload.get("audioBatch") or "").strip() or None,
            voice_plan=str(payload.get("voicePlan") or "").strip() or None,
            cover_template_mode=str(payload.get("coverTemplateMode") or "").strip() or None,
            source="dashboard_web",
        )
        expected = payload.get("expectedCount")
        if expected not in (None, ""):
            task["expectedCount"] = max(1, int(expected))
        return _submit_dashboard_task(task, start=bool(payload.get("startNow")))
    except FileExistsError:
        raise HTTPException(status_code=409, detail="重复任务未写入")
    except (TypeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _admit_replenishment(task_id: str, payload: dict[str, Any]):
    record = (load_project_download_statuses().get("items") or {}).get(task_id)
    if not isinstance(record, dict):
        raise ValueError("找不到原项目状态，不能补链")
    if str(record.get("status") or "") == "running":
        raise ValueError("该项目正在补链处理中，请等待本次结果后再提交")
    expected = max(1, int(record.get("expectedCount") or 0))
    completed = max(0, int(record.get("completed") or 0))
    stored_missing = record.get("missingCount")
    missing = max(0, int(stored_missing)) if stored_missing not in (None, "") else max(0, expected - completed)
    if completed > expected or missing > expected - completed:
        raise ValueError("原项目缺口与完成数量不一致，需要人工核对")
    template_id = next(
        (
            str(item.get("id")) for item in load_templates().get("templates", [])
            if isinstance(item, dict) and str(item.get("project") or "") == str(record.get("project") or "")
        ),
        "",
    )
    if not template_id:
        raise ValueError("原项目没有可用的投递模板")
    task = create_task(
        template_id,
        payload.get("urls") or [],
        date_label=str(record.get("date") or "").strip(),
        material=str(record.get("material") or "").strip() or None,
        audio_batch=str(record.get("audioBatch") or "").strip() or None,
        voice_plan=str(record.get("voicePlan") or "").strip() or None,
        cover_template_mode=str(record.get("coverTemplateMode") or "classic_a").strip() or None,
        source="dashboard_replenishment",
    )
    urls = task.get("urls") or []
    count = len(urls)
    # Admission is serialized across receiver processes. Persisted child records,
    # not an in-memory flag, prevent duplicate or overlapping transport batches.
    for directory in (INBOX_DIR, IMPORTED_DIR):
        for index, path in enumerate(directory.glob("*.json")):
            if index >= 20000:
                raise ValueError("补链记录过多，需要人工核对")
            previous = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(previous, dict) or previous.get("replenishForTaskId") != task_id:
                continue
            if previous.get("status") in {"cancelled", "canceled"}:
                continue
            previous_urls = set(previous.get("urls") or [])
            if set(urls) == previous_urls:
                return {"ok": True, "duplicate": True, "taskId": previous["taskId"],
                        "batchId": previous.get("batchId", ""), "file": path.name,
                        "workbenchLaunchRequested": False}
            if previous_urls.intersection(urls):
                raise ValueError("链接与已接收的补链任务重叠，请查询原任务，不重复提交")
            terminal = (record.get("lastReplenishmentTaskId") == previous.get("taskId")
                        and record.get("status") in {"blocked_missing_links", "completed"})
            if previous.get("status") != "merged_into_parent" and not terminal:
                raise ValueError("原项目已有待处理补链，请等待本次结果后再提交")
    if missing <= 0:
        raise ValueError("该项目当前没有缺口")
    if not 1 <= count <= missing:
        raise ValueError(f"本次可补 1 至 {missing} 条新链接；当前识别到 {count} 条。")
    task.update({
        "expectedCount": count,
        "runGroupId": str(record.get("runGroupId") or ""),
        "replenishmentParentBatchId": str(record.get("batchId") or ""),
        "isReplenishment": True,
        "replenishForTaskId": task_id,
        "canonicalTaskId": task_id,
        "taskRole": "step12_replenishment",
        "replenishmentBaseExpected": expected,
        "replenishmentCompletedBefore": completed,
        "replenishmentMissingBefore": missing,
    })
    return _submit_dashboard_task(task, start=bool(payload.get("startNow", True)))


@app.post("/api/tasks/replenish/{task_id}")
async def replenish_dashboard_task(task_id: str, request: Request):
    """Submit one nonempty, bounded portion of a blocked project gap."""
    _require_sender_permission(request)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("补链不是有效 JSON")
        with _json_file_lock(INBOX_DIR.parent / "replenishment-admission.json"):
            return _admit_replenishment(task_id, payload)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="重复补链任务未写入")
    except (TypeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/pipeline/snapshot")
def pipeline_snapshot():
    """Restart-safe full state used after a receiver/Dashboard reconnect."""
    reconcile_pipeline_store()
    return PIPELINE_STORE.snapshot()


@app.get("/api/pipeline/summary")
def pipeline_summary():
    """Compact active status for Dashboard polling; history remains replayable."""
    reconcile_pipeline_store()
    return PIPELINE_STORE.summary()


@app.get("/api/pipeline/events")
def pipeline_events(after: int = 0):
    """Replay events after a durable sequence number without duplicates."""
    return {"items": PIPELINE_STORE.events_after(max(0, int(after))), "sequence": PIPELINE_STORE.snapshot()["sequence"]}


@app.post("/api/pipeline/events")
async def receive_pipeline_event(request: Request):
    _require_sender_permission(request)
    try:
        event = await request.json()
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        result = PIPELINE_STORE.apply(event)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/testing/reset")
async def reset_testing_records(request: Request):
    """Trusted Dashboard-only reset for repeatable end-to-end testing."""
    _require_sender_permission(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="重置确认无效") from exc
    if not isinstance(payload, dict) or payload.get("confirm") != "RESET_TEST_RECORDS":
        raise HTTPException(status_code=400, detail="必须明确确认重置全部测试记录")
    try:
        return archive_and_reset_test_records()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"测试记录重置失败：{exc}") from exc


@app.post("/api/tasks")
async def receive_task(request: Request):
    _require_sender_permission(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="任务不是有效 JSON") from exc
    payload = ensure_pipeline_profile(dict(payload))
    valid, error = validate_task(payload)
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    payload = dict(payload)
    payload["source"] = "task_receiver"
    try:
        destination = submit_task(payload, INBOX_DIR)
    except FileExistsError:
        return {"ok": True, "duplicate": True, "taskId": payload["taskId"]}
    PIPELINE_STORE.register_task(payload)
    return {"ok": True, "taskId": payload["taskId"], "file": destination.name}


def _prepare_batch_payload(payload: Any) -> tuple[list[dict[str, Any]], str, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise HTTPException(status_code=400, detail="任务批次缺少 tasks 列表")
    tasks = [dict(item) for item in payload["tasks"] if isinstance(item, dict)]
    if len(tasks) != len(payload["tasks"]) or not tasks:
        raise HTTPException(status_code=400, detail="任务批次包含无效任务")
    batch_id = str(payload.get("batchId") or "").strip()
    if not batch_id:
        raise HTTPException(status_code=400, detail="任务批次缺少 batchId")
    run_group_id = str(payload.get("runGroupId") or "").strip()
    if run_group_id and not SUBMISSION_ID_PATTERN.fullmatch(run_group_id):
        raise HTTPException(status_code=400, detail="runGroupId格式无效")
    start_mode = str(payload.get("startMode") or "pending_confirmation")
    if start_mode not in {"pending_confirmation", *AUTO_SEQUENCE_MODES}:
        raise HTTPException(status_code=400, detail="任务批次启动方式无效")
    # ExampleBrand is an independent 21-output task box. Never run the formal 79-item
    # language expansion or totals against it.
    if str(payload.get("batchKind") or "") == ExampleBrand_BATCH_KIND:
        examplebrand_payload = dict(payload)
        examplebrand_payload["tasks"] = tasks
        valid_examplebrand, examplebrand_error = validate_examplebrand_batch_payload(examplebrand_payload)
        if not valid_examplebrand:
            raise HTTPException(status_code=400, detail=examplebrand_error)
        for sequence, item in enumerate(tasks, start=1):
            ensure_pipeline_profile(item)
            if str(item.get("batchId") or "") != batch_id or int(item.get("batchSequence") or 0) != sequence:
                raise HTTPException(status_code=400, detail="ExampleBrand任务批次身份或顺序不一致")
            if int(item.get("batchSize") or 0) != len(tasks):
                raise HTTPException(status_code=400, detail="ExampleBrand任务批次数量不一致")
            item["source"] = "task_receiver"
            item["startMode"] = start_mode
            item["runGroupId"] = run_group_id
            item["batchKind"] = ExampleBrand_BATCH_KIND
            item["selectedProjects"] = ["ExampleBrand"]
        return tasks, batch_id, start_mode
    voice_intent_valid, voice_intent_error = validate_batch_voice_intent(payload, tasks)
    if not voice_intent_valid:
        raise HTTPException(status_code=400, detail=voice_intent_error)
    # The material rows may change every day, but a formal production task box
    # uses the maintained formal total. Rebuild the explicit per-material language
    # contract server-side so an older submitter cannot accidentally replace a
    # row count with a project-level total (for example 5 -> 13 for ProjectB).
    tasks = assign_batch_contracts(tasks)
    # V13 batch submissions may omit the client-side profile.  Rebuild it
    # after the server-owned language contract is attached so every persisted
    # row has the same downstream voice/stitch/CapCut identity.
    for item in tasks:
        ensure_pipeline_profile(item)
    is_replenishment = any(bool(item.get("isReplenishment")) for item in tasks)
    selected_projects = normalize_selected_projects(payload.get("selectedProjects"))
    if not is_replenishment:
        if selected_projects and payload.get("projectExpectedCount") not in (None, ""):
            try:
                claimed_total = int(payload.get("projectExpectedCount"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="projectExpectedCount必须是整数") from exc
            expected_scope_total = sum(PROJECT_EXPECTED_TOTALS.get(project, 0) for project in selected_projects)
            if claimed_total != expected_scope_total:
                raise HTTPException(
                    status_code=400,
                    detail=f"所选项目固定计划总数为 {expected_scope_total}，不能提交 {claimed_total}",
                )
        valid_batch, batch_error = validate_formal_submission(tasks, selected_projects or None)
        if not valid_batch:
            raise HTTPException(status_code=400, detail=batch_error)
    for sequence, item in enumerate(tasks, start=1):
        if str(item.get("batchId") or "") != batch_id:
            raise HTTPException(status_code=400, detail="任务批次编号不一致")
        if int(item.get("batchSequence") or 0) != sequence:
            raise HTTPException(status_code=400, detail="任务批次顺序不一致")
        if int(item.get("batchSize") or 0) != len(tasks):
            raise HTTPException(status_code=400, detail="任务批次数量不一致")
        valid, error = validate_task(item)
        if not valid:
            raise HTTPException(status_code=400, detail=error)
        item["source"] = "task_receiver"
        item["startMode"] = start_mode
        item["runGroupId"] = run_group_id
        item["batchKind"] = "formal"
        item["selectedProjects"] = list(selected_projects or PROJECT_EXPECTED_TOTALS.keys())
    return tasks, batch_id, start_mode


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("payloadHash", None)
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _submission_receipt_path(submission_id: str) -> Path:
    if not SUBMISSION_ID_PATTERN.fullmatch(submission_id):
        raise HTTPException(status_code=400, detail="clientSubmissionId 格式无效")
    return SUBMISSION_RECEIPT_DIR / f"{submission_id}.json"


@app.post("/api/batches/validate")
async def validate_batch_request(request: Request):
    """Validate and expand a task box without writing queue or status files."""
    _require_sender_permission(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="任务批次不是有效 JSON") from exc
    tasks, batch_id, start_mode = _prepare_batch_payload(payload)
    if str(payload.get("batchKind") or "") == ExampleBrand_BATCH_KIND:
        total_expected = sum(int(item.get("expectedCount") or 0) for item in tasks)
        return {
            "ok": True,
            "batchId": batch_id,
            "batchKind": ExampleBrand_BATCH_KIND,
            "startMode": start_mode,
            "examplebrandSummary": payload.get("examplebrandSummary") or {},
            "projectTotals": {"ExampleBrand": total_expected},
            "totalExpected": total_expected,
            "items": [
                {
                    "taskId": item.get("taskId"),
                    "project": "ExampleBrand",
                    "material": item.get("material"),
                    "expectedCount": item.get("expectedCount"),
                    "languagePlan": item.get("languagePlan") or [],
                    "outputPlan": item.get("outputPlan") or [],
                }
                for item in tasks
            ],
        }
    selected_projects = list(normalize_selected_projects(payload.get("selectedProjects")) or PROJECT_EXPECTED_TOTALS.keys())
    totals = {project: 0 for project in selected_projects}
    for item in tasks:
        project = str(item.get("project") or "")
        if project in totals:
            totals[project] += int(item.get("expectedCount") or 0)
    return {
        "ok": True,
        "batchId": batch_id,
        "batchKind": "formal",
        "runGroupId": str(payload.get("runGroupId") or ""),
        "selectedProjects": selected_projects,
        "startMode": start_mode,
        "projectTotals": totals,
        "totalExpected": sum(totals.values()),
        "items": [
            {
                "taskId": item.get("taskId"),
                "project": item.get("project"),
                "material": item.get("material"),
                "expectedCount": item.get("expectedCount"),
                "languagePlan": item.get("languagePlan") or [],
            }
            for item in tasks
        ],
    }


@app.post("/api/batches")
async def receive_batch(request: Request):
    """Receive one idempotent task box after a complete server-side audit."""
    _require_sender_permission(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="任务批次不是有效 JSON") from exc
    tasks, batch_id, start_mode = _prepare_batch_payload(payload)
    submission_id = str(payload.get("clientSubmissionId") or batch_id).strip()
    receipt_path = _submission_receipt_path(submission_id)
    calculated_hash = _payload_hash(payload)
    requested_hash = str(payload.get("payloadHash") or calculated_hash).strip().lower()
    if requested_hash != calculated_hash:
        raise HTTPException(status_code=400, detail="任务箱内容哈希不一致，请重新对账后提交")
    try:
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        existing = None
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"无法读取已有提交回执：{exc}") from exc
    if isinstance(existing, dict):
        if str(existing.get("payloadHash") or "").lower() != calculated_hash:
            raise HTTPException(status_code=409, detail="同一提交编号对应的任务内容已经变化")
        return {**existing, "ok": True, "duplicate": True}
    try:
        files = submit_task_batch(tasks, INBOX_DIR)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for item in tasks:
        PIPELINE_STORE.register_task(item)
    launch_requested = False
    launch_error = ""
    if start_mode in AUTO_SEQUENCE_MODES:
        try:
            launch_requested = _start_workbench_if_needed()
        except (OSError, FileNotFoundError) as exc:
            launch_error = str(exc)
    receipt = {
        "ok": True,
        "schemaVersion": 1,
        "receivedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batchId": batch_id,
        "batchKind": str(payload.get("batchKind") or "formal"),
        "runGroupId": str(payload.get("runGroupId") or ""),
        "selectedProjects": list(normalize_selected_projects(payload.get("selectedProjects"))),
        "clientSubmissionId": submission_id,
        "payloadHash": calculated_hash,
        "count": len(files),
        "taskIds": [item["taskId"] for item in tasks],
        "workbenchLaunchRequested": launch_requested,
        "workbenchLaunchError": launch_error,
    }
    atomic_write_json(receipt_path, receipt)
    return receipt


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request, cap: str = ""):
    """Cancel a pending or imported-but-not-started task without deleting assets."""
    if cap:
        _require_page_capability("cancel", task_id, cap)
    else:
        _require_sender_permission(request)
    try:
        task = cancel_pending_task(task_id)
    except FileNotFoundError as exc:
        try:
            task = cancel_imported_task(task_id)
        except FileNotFoundError as imported_exc:
            archived = (PIPELINE_STORE.snapshot().get("archivedProjects") or {}).get(task_id)
            if isinstance(archived, dict) and str(archived.get("status") or "") in {
                "cancelled", "canceled", "merged_into_parent",
            }:
                task = dict(archived)
                task["status"] = str(task.get("status") or "cancelled")
            else:
                raise HTTPException(status_code=404, detail=str(imported_exc)) from imported_exc
        except ValueError as imported_exc:
            raise HTTPException(status_code=409, detail=str(imported_exc)) from imported_exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    synchronized = True
    sync_error = ""
    try:
        sync_result = PIPELINE_STORE.cancel_task(
            task_id,
            reason=str(task.get("cancelReason") or "cancelled"),
            task=task,
        )
        reconciliation = reconcile_pipeline_store()
        synchronized = reconciliation.get("state") == "healthy"
    except Exception as exc:
        synchronized = False
        sync_error = str(exc)
        PIPELINE_STORE.mark_reconciliation_failed(sync_error)
        sync_result = {"archived": False}
    snapshot = PIPELINE_STORE.snapshot()
    return {
        "ok": True,
        "taskId": task_id,
        "status": task["status"],
        "pipelineSynchronized": synchronized,
        "pipelineSyncError": sync_error,
        "pipelineArchived": bool(sync_result.get("archived")),
        "sequence": int(snapshot.get("sequence") or 0),
        "reconciliationState": str(snapshot.get("reconciliationState") or "unknown"),
    }


@app.post("/api/workbench/ensure-running")
def ensure_workbench_endpoint(request: Request):
    """Single receiver-owned entry point for all workbench launch requests."""
    _require_sender_permission(request)
    try:
        result = ensure_workbench_running()
    except WorkbenchStartupError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": exc.code,
                "message": str(exc),
                "logFile": str(exc.log_file) if exc.log_file else "",
            },
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"code": "os_error", "message": f"工作台启动失败：{exc}"}) from exc
    except Exception as exc:
        log = _write_workbench_start_log(f"unexpected_error: {exc}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail={"code": "unexpected_error", "message": str(exc), "logFile": str(log)},
        ) from exc
    return {"ok": True, **result}


@app.post("/api/tasks/{task_id}/start")
def start_task(task_id: str, request: Request, cap: str = ""):
    """Persist an explicit start request; the workbench claims it safely."""
    if cap:
        _require_page_capability("start", task_id, cap)
    else:
        _require_sender_permission(request)
    try:
        task = request_pending_start(task_id)
        launch_requested = _start_workbench_if_needed()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "taskId": task_id,
        "startMode": task["startMode"],
        "workbenchLaunchRequested": launch_requested,
    }


@app.get("/", response_class=HTMLResponse)
def page():
    cards = []
    for task in read_tasks():
        status = str(task.get("status") or "")
        status_label = STATUS_LABELS.get(status, status or "未知")
        if status == "pending_confirmation" and task.get("startMode") in AUTO_SEQUENCE_MODES:
            status_label = "等待顺序自动执行"
        task_id = str(task.get("taskId") or "")
        batch_label = ""
        if task.get("batchId"):
            batch_label = f"第 {task.get('batchSequence') or '?'} / {task.get('batchSize') or '?'} 项"
        action = ""
        if status in {"pending_confirmation", "imported_waiting_start"} and task_id:
            cancel_capability = _page_capability("cancel", task_id)
            start_capability = _page_capability("start", task_id)
            action_label = "撤回尚未开始任务" if status == "imported_waiting_start" else "撤销此待确认任务"
            start_action = ""
            if status == "pending_confirmation" and task.get("startMode") not in AUTO_SEQUENCE_MODES:
                start_action = (
                    f'<button class="start" data-url="/api/tasks/{html.escape(task_id, quote=True)}/start?cap={start_capability}" '
                    f'onclick="startTask(this)">启动首项</button>'
                )
            action = (
                '<div class="task-actions">'
                f'{start_action}'
                f'<button class="cancel" data-url="/api/tasks/{html.escape(task_id, quote=True)}/cancel?cap={cancel_capability}" '
                f'onclick="cancelTask(this)">{action_label}</button>'
                '</div>'
            )
        cards.append(
            '<article class="task">'
            f'<div class="task-top"><strong>{html.escape(str(task.get("project") or "未分类"))}</strong>'
            f'<span>{html.escape(status_label)}</span></div>'
            '<dl>'
            f'<div><dt>生产日期</dt><dd>{html.escape(str(task.get("date") or "--"))}</dd></div>'
            f'<div><dt>材料</dt><dd>{html.escape(str(task.get("material") or "--"))}</dd></div>'
            f'<div><dt>音频批次</dt><dd>{html.escape(str(task.get("audioBatch") or "--"))}</dd></div>'
            f'<div><dt>语音处理</dt><dd>{html.escape(VOICE_PLAN_LABELS.get(str(task.get("voicePlan") or ""), "未指定"))}</dd></div>'
            f'<div><dt>链接数</dt><dd>{len(task.get("urls") or [])} 条</dd></div>'
            f'<div><dt>任务框顺序</dt><dd>{html.escape(batch_label or "单条任务")}</dd></div>'
            f'<div><dt>启动方式</dt><dd>{"顺序自动执行" if task.get("startMode") in AUTO_SEQUENCE_MODES else "等待确认"}</dd></div>'
            f'</dl>{action}</article>'
        )
    task_list = "".join(cards) or '<p class="empty">暂时没有任务</p>'
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>AI视频生产线 · 任务接收器</title><style>
        body{{font-family:'Microsoft YaHei UI',Arial;background:#f5f7fa;color:#172033;margin:0;padding:22px}}
        main{{max-width:960px;margin:auto;background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:24px}}
        h1{{margin:0;font-size:23px}}p{{color:#667085}}.tag{{color:#0f766e;background:#ecfdf5;padding:4px 8px;border-radius:99px}}.empty{{text-align:center;color:#98a2b3;padding:28px}}
        .tasks{{display:grid;gap:12px;margin-top:18px}}.task{{border:1px solid #e5e7eb;border-radius:12px;padding:14px;background:#fff}}.task-top{{display:flex;align-items:center;justify-content:space-between;gap:10px}}.task-top strong{{font-size:17px}}.task-top span{{font-size:13px;color:#2563eb;background:#eff6ff;padding:5px 8px;border-radius:99px;white-space:nowrap}}dl{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:14px 0 0}}dt{{font-size:12px;color:#667085}}dd{{margin:4px 0 0;font-weight:600;word-break:break-word}}.task-actions{{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}}button{{border-radius:8px;padding:8px 12px;font:inherit;font-size:13px}}button.start{{border:1px solid #bfdbfe;background:#2563eb;color:#fff}}button.cancel{{border:1px solid #fecaca;background:#fff;color:#b91c1c}}button:active{{filter:brightness(.96)}}@media(max-width:620px){{body{{padding:14px}}main{{padding:18px}}h1{{font-size:20px}}dl{{grid-template-columns:repeat(2,minmax(0,1fr))}}.task-actions button{{flex:1;padding:10px}}}}
        </style><main><h1>AI视频生产线 · 任务接收器</h1><p><span class="tag">查看与撤销</span> {html.escape(str(CONFIG.get('machineName')))} · 仅“待确认”任务可以撤销；撤销只移动任务记录，不删除视频、音频或素材。</p>
        <section class="tasks">{task_list}</section></main><script>async function runTaskAction(button,question,pending,failed){{if(!confirm(question))return;button.disabled=true;button.textContent=pending;try{{const r=await fetch(button.dataset.url,{{method:'POST'}});const d=await r.json();if(!r.ok)throw new Error(d.detail||failed);location.reload()}}catch(e){{alert(e.message||failed);button.disabled=false;button.textContent=button.classList.contains('start')?'启动首项':'撤回任务'}}}}function cancelTask(button){{return runTaskAction(button,'确认撤回此尚未开始任务吗？不会删除任何视频、音频或素材。','正在撤回…','撤回失败')}}function startTask(button){{return runTaskAction(button,'确认启动此批次首项吗？工作台将按既有顺序处理。','正在请求启动…','启动失败')}}</script></html>"""
    )


if __name__ == "__main__":
    LOGIN_STARTUP = "--login-startup" in sys.argv[1:]
    uvicorn.run(app, host=str(CONFIG.get("listenHost") or "0.0.0.0"), port=int(CONFIG.get("listenPort") or 8766))
