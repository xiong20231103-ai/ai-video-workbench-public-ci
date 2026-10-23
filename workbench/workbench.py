from __future__ import annotations

import contextlib
import copy
import csv
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from capcut_handoff import (
    CATEGORIES,
    RUNNABLE_STATUSES,
    STATUS_LABELS,
    CapCutHandoffStore,
    launch_capcut_client,
)
from common_io import atomic_write_json, pid_is_alive as _pid_is_alive
from embedded_stitcher import EmbeddedStitcherController
from examplebrand_production import (
    ExamplebrandProductionComposer,
    ExamplebrandProductionError,
    derived_execution_profile,
    validate_static_resources,
)
from baidu_upload import (
    CONFIG_FILE as BAIDU_CONFIG_FILE,
    STATE_FILE as BAIDU_STATE_FILE,
    BaiduNetdiskClient,
    SecretStore,
    UploadCoordinator,
    load_config as load_baidu_config,
    load_json as load_baidu_json,
    save_config as save_baidu_config,
)
from task_inbox import (
    IMPORTED_DIR as TASK_IMPORTED_DIR,
    batch_download_directory,
    canonical_task_id,
    list_pending_tasks,
    load_current_context,
    load_project_download_statuses,
    load_task_record,
    mark_replenishment_merged,
    mark_imported,
    remove_project_download_status,
    resolve_task_audio_directory,
    ensure_pipeline_profile,
    save_current_context,
    save_project_download_status,
)
from task_contract import expected_count as contract_expected_count, language_plan as contract_language_plan
from voice_guardrails import validate_voice_handoff
from voice_dependency_preflight import check_voice_dependencies, dependency_message
from recovery_control import (
    RecoveryControl,
    RecoveryControlError,
    capture_process_identity,
    observe_process_identity,
    resolve_recovery_identity,
)

if os.name == "nt":
    import msvcrt


APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "workbench_settings.json"
OPS_STATUS_FILE = APP_DIR / "ops_pipeline_status.json"
WORKBENCH_INSTANCE_FILE = APP_DIR / "handoff" / "control" / "workbench.pid"
WORKBENCH_LAUNCH_FILE = APP_DIR / "handoff" / "control" / "workbench.launching"
WORKBENCH_READY_FILE = APP_DIR / "handoff" / "control" / "workbench.ready.json"
WORKBENCH_LOCK_FILE = APP_DIR / "handoff" / "control" / "workbench.instance.lock"
WORKBENCH_SESSION_ID = uuid.uuid4().hex
ExampleBrand_CONTROL_DIR = APP_DIR / "handoff" / "control" / "examplebrand_start_intents"
ExampleBrand_ARTIFACT_ROOT = APP_DIR / "handoff" / "examplebrand_production"
_WORKBENCH_MUTEX_HANDLE = None
_WORKBENCH_FILE_LOCK_HANDLE = None
WORKBENCH_LOG_DIR = APP_DIR / "handoff" / "logs"
_WORKBENCH_LOG_LOCK = threading.Lock()
# Repetitive progress and status lines that made up 97% of the 2026-10-08 log
# (34k lines). Each is written at most once per window, keyed per pattern or,
# where the exact text matters, per text. Everything else is logged as-is.
LOG_THROTTLE_RULES = (
    (re.compile(r"^CUDA STTN subtitle removal attempt \d+$"), 60, True),
    (re.compile(r"^视频下载 \d+%$"), 30, False),
    (re.compile(r"GPU OCR sample \d+/\d+$"), 30, False),
    (re.compile(r"^Ignored cross-date CapCut attribution:"), 3600, True),
    (re.compile(r"^自动恢复已暂停："), 300, True),
)


def append_workbench_log(text, now=None):
    """Mirror one UI log line to today's file so it outlives the window.

    Logging must never interrupt production, so any file error is ignored.
    """
    now = now or datetime.now()
    line = f"{now.isoformat(timespec='seconds')} [pid {os.getpid()}] {text}\n"
    try:
        with _WORKBENCH_LOG_LOCK:
            WORKBENCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (WORKBENCH_LOG_DIR / f"workbench-{now:%Y%m%d}.log").open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass


def _acquire_windows_workbench_mutex() -> bool:
    """Use a kernel mutex so two Python runtimes cannot race the PID file."""
    global _WORKBENCH_MUTEX_HANDLE
    if os.name != "nt":
        return True
    if _WORKBENCH_MUTEX_HANDLE:
        return False
    digest = hashlib.sha256(str(APP_DIR).lower().encode("utf-8")).hexdigest()[:24]
    name = f"Local\\AIVideoWorkbench-{digest}"
    # ``ctypes.windll.kernel32.GetLastError()`` is unreliable after a foreign
    # call when two different embedded Python runtimes race at startup.  Keep
    # the thread-local Win32 error explicitly so ERROR_ALREADY_EXISTS cannot be
    # lost between CreateMutexW and the check below.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    last_error = ctypes.get_last_error()
    if not handle:
        return False
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _WORKBENCH_MUTEX_HANDLE = handle
    return True


def _release_windows_workbench_mutex() -> None:
    global _WORKBENCH_MUTEX_HANDLE
    if os.name == "nt" and _WORKBENCH_MUTEX_HANDLE:
        try:
            ctypes.windll.kernel32.CloseHandle(_WORKBENCH_MUTEX_HANDLE)
        finally:
            _WORKBENCH_MUTEX_HANDLE = None


def _acquire_workbench_file_lock() -> bool:
    """Hold a one-byte OS file lock for the full Workbench process lifetime."""
    global _WORKBENCH_FILE_LOCK_HANDLE
    if os.name != "nt":
        return True
    if _WORKBENCH_FILE_LOCK_HANDLE is not None:
        return False
    WORKBENCH_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = open(WORKBENCH_LOCK_FILE, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    _WORKBENCH_FILE_LOCK_HANDLE = handle
    return True


def _release_workbench_file_lock() -> None:
    global _WORKBENCH_FILE_LOCK_HANDLE
    handle = _WORKBENCH_FILE_LOCK_HANDLE
    if os.name == "nt" and handle is not None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()
            _WORKBENCH_FILE_LOCK_HANDLE = None


def _release_workbench_locks() -> None:
    _release_windows_workbench_mutex()
    _release_workbench_file_lock()


def acquire_workbench_instance() -> bool:
    """Acquire a process-owned PID file without inspecting unrelated processes."""
    if not _acquire_workbench_file_lock():
        return False
    if not _acquire_windows_workbench_mutex():
        _release_workbench_file_lock()
        return False
    WORKBENCH_INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            descriptor = os.open(
                str(WORKBENCH_INSTANCE_FILE),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            try:
                existing_pid = int(WORKBENCH_INSTANCE_FILE.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                existing_pid = 0
            if _pid_is_alive(existing_pid):
                _release_workbench_locks()
                return False
            try:
                WORKBENCH_INSTANCE_FILE.unlink()
            except OSError:
                _release_workbench_locks()
                return False
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            WORKBENCH_LAUNCH_FILE.unlink()
        except FileNotFoundError:
            pass
        return True
    _release_workbench_locks()
    return False


def release_workbench_instance() -> None:
    try:
        registered_pid = int(WORKBENCH_INSTANCE_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        _release_workbench_locks()
        return
    if registered_pid == os.getpid():
        try:
            WORKBENCH_INSTANCE_FILE.unlink()
        except FileNotFoundError:
            pass
    _release_workbench_locks()


def publish_workbench_ready() -> None:
    """Publish a local-only liveness record once Tk has finished constructing."""
    WORKBENCH_READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    payload = {
        "state": "ready",
        "pid": os.getpid(),
        "sessionId": WORKBENCH_SESSION_ID,
        "readyAt": now,
        "heartbeatAt": now,
        "heartbeatEpoch": time.time(),
    }
    temp = WORKBENCH_READY_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, WORKBENCH_READY_FILE)


def remove_workbench_ready() -> None:
    try:
        payload = json.loads(WORKBENCH_READY_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if int(payload.get("pid") or 0) != os.getpid() or str(payload.get("sessionId") or "") != WORKBENCH_SESSION_ID:
        return
    try:
        WORKBENCH_READY_FILE.unlink()
    except FileNotFoundError:
        pass
DOWNLOAD_STATE_FILE = APP_DIR / "handoff" / "download" / "download_batch_state.json"
DOWNLOAD_CHECKPOINT_DIR = APP_DIR / "handoff" / "download" / "checkpoints"
# One state/request/log file per prefetched project (plan item P2).
DOWNLOAD_PREFETCH_DIR = APP_DIR / "handoff" / "download" / "prefetch"
DOWNLOAD_BRIDGE_RUNTIME = Path(r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\.cuda_env\Scripts\python.exe")
SUBTITLE_MODE_IDS = {"AI 去除 STTN": "ai-sttn", "AI 去除 LaMa": "ai-lama", "高速模糊": "fast-blur", "原智能模糊": "blur"}
VOICE_CLIENT_DIR = APP_DIR / "tools" / "BatchVoiceClient-Portable"
VOICE_LAUNCH_STARTED = "started"
VOICE_LAUNCH_ALREADY_STARTING = "already_starting_same_task"
VOICE_LAUNCH_ALREADY_RUNNING = "already_running_same_task"
VOICE_LAUNCH_BLOCKED_OTHER_TASK = "blocked_other_task"
VOICE_LAUNCH_FAILED = "launch_failed"
VOICE_BRIDGE_START_TIMEOUT_SECONDS = 30
UPLOAD_SERVICE = APP_DIR / "upload_service.py"
CONTINUE_COMMAND_FILE = APP_DIR / "handoff" / "control" / "continue_current_project.json"
PIPELINE_STATUS_FILE = APP_DIR / "handoff" / "status" / "pipeline_snapshot.json"
AUTO_RECOVERY_STATE_FILE = APP_DIR / "handoff" / "control" / "automatic_recovery.json"
AUTO_RECOVERY_LOCK_FILE = APP_DIR / "handoff" / "control" / "automatic_recovery.lock"
AUTO_RECOVERY_SCHEMA_VERSION = 4
AUTO_RECOVERY_DELAY_SECONDS = 30
# Pipeline states in which a started remote project no longer holds the
# shared Step 3-5 resources (Fish, stitcher, CapCut): finished, or parked
# after Step 1/2 waiting for missing links.
QUEUE_RELEASED_PIPELINE_STATUSES = frozenset({"completed", "blocked_missing_links"})
# Pipeline states that still belong to Step 1/2.  Any other state means the
# project has moved past the download/AI-subtitle step.
STEP12_PIPELINE_STATUSES = frozenset({
    "", "waiting", "downloading", "step12_resuming", "step12_interrupted",
    "blocked_download_runtime", "blocked_step12_resume", "blocked_missing_links",
})
# A finished bridge process is reported a moment later; until then the
# project record can still say "running".
STEP12_REPORT_GRACE_SECONDS = 600
AUTO_RECOVERY_MAX_ATTEMPTS = 2
AUTO_RECOVERY_VOICE_ERRORS = {
    # Only errors with a completed bridge/launch identity are retryable.  Batch
    # aggregates and generic launch errors do not prove that retrying is safe.
    "VOICE_BRIDGE_START_TIMEOUT", "VOICE_PROCESS_EXITED_EARLY",
}
AUTO_RECOVERY_CAPCUT_ERRORS = {
    "CAPCUT_MAIN_WINDOW_TRANSITION_TIMEOUT", "CAPCUT_EDITOR_WINDOW_NOT_READY",
    "CAPCUT_EDITOR_REBIND_TIMEOUT", "CAPCUT_EDITOR_CONTEXT_AMBIGUOUS",
    "CAPCUT_FILE_DIALOG_NATIVE_NOT_FOUND", "CAPCUT_FILE_DIALOG_PROBE_TIMEOUT",
    "CAPCUT_FILE_DIALOG_FILENAME_FIELD_MISSING", "CAPCUT_IMPORT_COMMAND_UNCONFIRMED",
    "CAPCUT_IMPORT_WORKER_STALLED", "CAPCUT_EDITOR_TRANSITION_IN_PLACE_NOT_CONFIRMED",
    "CAPCUT_EDITOR_REPLACEMENT_WINDOW_NOT_FOUND", "CAPCUT_EDITOR_PROBE_TIMEOUT",
    # A video-only checkpoint with no unknown media is a safe lower-stage
    # fact: retrying the audio import will not duplicate the body video.
    "CAPCUT_DRAFT_AUDIO_MISSING",
}


def persist_formal_stitch_identity(record_path, records):
    """Atomically bind a stitch generation record to its current task.

    Reused audio intentionally keeps its original filename.  That filename may
    contain an older ``remote-inbox`` id, so it is evidence for the media file
    only and must never become the owner of a later production task.  Persist
    the formal task identity before CapCut ingestion so a startup recovery scan
    cannot reconstruct the rows with the old audio owner's id.
    """
    path = Path(record_path) if record_path else None
    if path is None or not path.is_file():
        raise ValueError("missing stitch generation record")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list) or len(payload) != len(records):
        raise ValueError("stitch generation record changed before identity binding")
    identity_keys = (
        "remotePipelineTaskId",
        "remotePipelineTaskIdSource",
        "formalTaskIdentity",
        "canonicalTaskId",
        "batchId",
        "productionDate",
        "expectedTaskCount",
    )
    updated = []
    for stored, formal in zip(payload, records):
        if not isinstance(stored, dict) or not isinstance(formal, dict):
            raise ValueError("invalid stitch generation record row")
        stored_output = str(stored.get("output") or "").strip()
        formal_output = str(formal.get("output") or "").strip()
        stored_audio = str(stored.get("audio_path") or "").strip()
        formal_audio = str(formal.get("audio_path") or "").strip()
        if stored_output != formal_output or stored_audio != formal_audio:
            raise ValueError("stitch generation record media identity changed")
        row = dict(stored)
        for key in identity_keys:
            if key in formal:
                row[key] = formal.get(key)
        updated.append(row)
    temporary = path.with_name(path.name + ".formal.tmp")
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return True


def continue_command_target_matches(command, task, snapshot_updated_at=""):
    """Validate that a Dashboard command still targets the canonical project."""
    if str(command.get("requestedBy") or "") != "dashboard":
        return True, ""
    required = ("commandId", "batchId", "taskId", "productionDate", "snapshotUpdatedAt")
    missing = [key for key in required if not str(command.get(key) or "").strip()]
    if missing:
        return False, f"安全继续命令缺少目标字段：{', '.join(missing)}"
    if not isinstance(task, dict) or not task:
        return False, "当前没有与安全继续命令匹配的活动任务"
    actual_task_id = canonical_task_id(task)
    actual_batch_id = str(task.get("batchId") or "").strip()
    actual_date = str(task.get("productionDate") or task.get("date") or "").strip()
    expected = (
        str(command.get("taskId") or "").strip(),
        str(command.get("batchId") or "").strip(),
        str(command.get("productionDate") or "").strip(),
    )
    actual = (actual_task_id, actual_batch_id, actual_date)
    is_replenishment_transport = (
        str(task.get("replenishForTaskId") or "").strip() == actual_task_id
        and str(task.get("taskId") or "").strip() != actual_task_id
    )
    identity_matches = (
        actual_task_id == expected[0]
        and actual_date == expected[2]
        and (actual_batch_id == expected[1] or is_replenishment_transport)
    )
    if not identity_matches:
        return False, (
            "安全继续目标已变化："
            f"命令={expected[0]}/{expected[1]}/{expected[2]}，"
            f"当前={actual[0]}/{actual[1]}/{actual[2]}"
        )
    command_run_group = str(command.get("runGroupId") or "").strip()
    if command_run_group and command_run_group != str(task.get("runGroupId") or "").strip():
        return False, (
            "安全继续运行组已变化："
            f"命令={command_run_group}，当前={str(task.get('runGroupId') or '').strip() or '未声明'}"
        )
    command_batch_kind = str(command.get("batchKind") or "").strip()
    if command_batch_kind and command_batch_kind != str(task.get("batchKind") or "formal").strip():
        return False, (
            "安全继续批次类型已变化："
            f"命令={command_batch_kind}，当前={str(task.get('batchKind') or 'formal').strip()}"
        )
    command_scope = command.get("selectedProjects")
    if isinstance(command_scope, list) and command_scope:
        current_scope = task.get("selectedProjects")
        if (
            not isinstance(current_scope, list)
            or sorted(str(item) for item in command_scope)
            != sorted(str(item) for item in current_scope)
        ):
            return False, "安全继续项目组合已变化，旧命令已阻止"
    # A newly started workbench publishes its ready/heartbeat state before it
    # can consume the Dashboard mailbox.  That legitimately refreshes the
    # receiver snapshot timestamp while the batch/task/date identity remains
    # unchanged.  Treat identity as the safety boundary; the recovery path
    # independently reconciles every step before starting a client.
    #
    # ``snapshotUpdatedAt`` remains useful audit metadata, but must not turn a
    # valid command into stale merely because this workbench has just started.
    return True, ""


def completed_download_checkpoint_count(task):
    """Count durable completed outputs for one transport task.

    A bridge can persist its checkpoint just before the Tk progress counters
    refresh. Replenishment reconciliation must trust that task-scoped proof
    instead of writing 0/N and permanently blocking the canonical parent.
    """
    task_id = str((task or {}).get("taskId") or "").strip()
    if not task_id:
        return 0
    path = DOWNLOAD_CHECKPOINT_DIR / f"{task_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return 0
    outputs = set()
    for item in (payload.get("items") or {}).values():
        if not isinstance(item, dict) or str(item.get("status") or "") != "completed":
            continue
        output_value = str(item.get("outputPath") or "").strip()
        if not output_value:
            continue
        output = Path(output_value)
        try:
            if not output.is_file() or output.stat().st_size < 1024:
                continue
            outputs.add(os.path.normcase(str(output.resolve())))
        except OSError:
            continue
    return len(outputs)


def interrupted_step12_resume_evidence(task, download_record, session_payload):
    """Return whether an incomplete Step 1/2 session is an interrupted run.

    A real link gap has fewer submitted URLs than the contract or a durable
    terminal result.  By contrast, closing the workbench while the bridge is
    still processing leaves the complete original URL set, an empty terminal
    result, and one or more pending/running table rows.  That state is safe to
    resume because ``tikhub_bridge.Checkpoint`` validates and skips completed
    outputs before processing the remaining URLs.
    """
    if not isinstance(task, dict) or not isinstance(download_record, dict):
        return False, "missing task or download record"
    if bool(task.get("isReplenishment")):
        return False, "replenishment tasks use the existing gap merge flow"
    task_id = canonical_task_id(task)
    task_urls = [str(url).strip() for url in (task.get("urls") or []) if str(url).strip()]
    try:
        expected = max(1, int(download_record.get("expectedCount") or task.get("expectedCount") or len(task_urls) or 1))
    except (TypeError, ValueError):
        expected = max(1, len(task_urls))
    if len(task_urls) != expected or len(set(task_urls)) != expected:
        return False, "the original request does not contain the full unique URL contract"
    if not isinstance(session_payload, dict):
        return False, "download session is missing"
    saved_task = session_payload.get("activeExternalTask") if isinstance(session_payload.get("activeExternalTask"), dict) else {}
    request = ((session_payload.get("context") or {}).get("request")
               if isinstance(session_payload.get("context"), dict) else {})
    if not isinstance(request, dict):
        request = {}
    # Older waiting-task snapshots only persisted ``pendingImport`` and the
    # table rows.  They still contain enough immutable evidence to start the
    # original task safely: the imported task owns the full unique URL set and
    # every saved row is keyed by exactly that set.  Treat that as a resumable
    # pre-start snapshot instead of inventing a zero-count missing-link gap.
    if not request:
        request = {
            "taskId": str(saved_task.get("taskId") or "").strip(),
            "urls": list(saved_task.get("urls") or []),
        }
    if canonical_task_id(saved_task) != task_id or str(request.get("taskId") or "").strip() != str(task.get("taskId") or "").strip():
        return False, "download session belongs to another task"
    request_urls = [str(url).strip() for url in (request.get("urls") or []) if str(url).strip()]
    if request_urls != task_urls:
        return False, "download session URL set does not match the original request"
    terminal_result = session_payload.get("lastBatchResult")
    if isinstance(terminal_result, dict) and terminal_result:
        return False, "download bridge already wrote a terminal batch result"
    rows = session_payload.get("tasks") if isinstance(session_payload.get("tasks"), list) else []
    row_map = {
        str(item.get("url") or "").strip(): item.get("values")
        for item in rows if isinstance(item, dict) and str(item.get("url") or "").strip()
    }
    if set(row_map) != set(task_urls):
        return False, "download session rows do not match the original request"
    unfinished = 0
    for url in task_urls:
        values = row_map.get(url)
        if not isinstance(values, list) or len(values) < 4:
            return False, "download session row is incomplete"
        download_state, subtitle_state, stage = map(str, values[1:4])
        if download_state == "失败" or subtitle_state == "失败" or stage == "处理失败":
            return False, "download bridge recorded a real failed item"
        if not (download_state == "已完成" and subtitle_state == "已完成"):
            unfinished += 1
    if not unfinished:
        return False, "download session has no interrupted items"
    return True, f"original request still contains all {expected} URLs; {unfinished} item(s) were interrupted"


def checkpoint_step12_resume_evidence(task, download_record, checkpoint_payload):
    """Build a safe Step 1/2 recovery plan without a live bridge session.

    The bridge session is transport state and can legitimately end after a
    connection reset.  The immutable task URL contract plus the task-scoped
    checkpoint are the durable source of truth.  Completed artifacts are
    retained only when their files still exist; failed, missing, and never
    started URLs become the next recovery queue.
    """
    if not isinstance(task, dict) or not isinstance(download_record, dict):
        return False, "missing task or download record", {}
    if bool(task.get("isReplenishment")):
        return False, "replenishment tasks use the existing gap merge flow", {}

    task_id = str(task.get("taskId") or "").strip()
    canonical_id = canonical_task_id(task)
    urls = [str(url).strip() for url in (task.get("urls") or []) if str(url).strip()]
    try:
        expected = max(1, int(download_record.get("expectedCount") or task.get("expectedCount") or len(urls) or 1))
    except (TypeError, ValueError):
        expected = max(1, len(urls))
    if not task_id or not canonical_id:
        return False, "task identity is incomplete", {}
    if len(urls) != expected or len(set(urls)) != expected:
        return False, "the original request does not contain the full unique URL contract", {}
    if not isinstance(checkpoint_payload, dict):
        return False, "task checkpoint is missing", {}
    checkpoint_task_id = str(checkpoint_payload.get("taskId") or "").strip()
    if checkpoint_task_id and checkpoint_task_id != task_id:
        return False, "task checkpoint belongs to another task", {}
    items = checkpoint_payload.get("items")
    if not isinstance(items, dict):
        return False, "task checkpoint items are missing", {}
    unknown_urls = sorted(set(str(url).strip() for url in items) - set(urls))
    if unknown_urls:
        return False, "task checkpoint contains URLs outside the immutable contract", {}

    completed_urls, pending_urls, invalid_completed_urls = [], [], []
    completed_outputs = []
    for url in urls:
        item = items.get(url) if isinstance(items.get(url), dict) else {}
        output_value = str(item.get("outputPath") or "").strip()
        output = Path(output_value) if output_value else None
        output_valid = False
        if str(item.get("status") or "") == "completed" and output is not None:
            try:
                output_valid = output.is_file() and output.stat().st_size >= 1024
            except OSError:
                output_valid = False
        if output_valid:
            completed_urls.append(url)
            completed_outputs.append(str(output.resolve()))
        else:
            pending_urls.append(url)
            if str(item.get("status") or "") == "completed":
                invalid_completed_urls.append(url)

    if not pending_urls:
        return False, "task checkpoint already contains every valid output", {}
    plan = {
        "taskId": task_id,
        "canonicalTaskId": canonical_id,
        "expectedCount": expected,
        "completedCount": len(completed_urls),
        "pendingCount": len(pending_urls),
        "completedUrls": completed_urls,
        "pendingUrls": pending_urls,
        "completedOutputs": completed_outputs,
        "invalidCompletedUrls": invalid_completed_urls,
    }
    return (
        True,
        f"checkpoint preserves {len(completed_urls)}/{expected} valid outputs; "
        f"rebuild the bridge session for {len(pending_urls)} remaining URL(s)",
        plan,
    )


def resolve_continue_command_task(command, current_task, status_items, task_loader=load_task_record):
    """Resolve a Dashboard recovery target without trusting a stale context.

    ``current_pipeline_context.json`` is a restart aid, not a queue lock.  A
    CapCut client can finish after the workbench which launched it has exited;
    in that case the durable project registry is already ``completed`` while
    the context still names the old project.  Permit a command-scoped switch
    only when that old project is durably complete and the requested task's
    immutable identity can be loaded and verified.
    """
    current = current_task if isinstance(current_task, dict) else {}
    requested_task_id = str(command.get("taskId") or "").strip()
    requested_batch_id = str(command.get("batchId") or "").strip()
    requested_date = str(command.get("productionDate") or "").strip()
    current_task_id = canonical_task_id(current)
    if not requested_task_id or requested_task_id == current_task_id:
        return current, ""

    records = status_items if isinstance(status_items, dict) else {}
    current_record = records.get(current_task_id) if current_task_id else None
    if current_task_id and (
        not isinstance(current_record, dict)
        or str(current_record.get("pipelineStatus") or "").strip() != "completed"
    ):
        return None, (
            "安全继续目标已变化，且工作台当前项目尚未完成："
            f"当前={current_task_id or '无'}，命令={requested_task_id}"
        )

    candidate = task_loader(requested_task_id)
    if not isinstance(candidate, dict):
        return None, f"安全继续目标任务不存在或无法读取：{requested_task_id}"
    actual = (
        canonical_task_id(candidate),
        str(candidate.get("batchId") or "").strip(),
        str(candidate.get("productionDate") or candidate.get("date") or "").strip(),
    )
    expected = (requested_task_id, requested_batch_id, requested_date)
    if actual != expected:
        return None, (
            "安全继续目标任务身份不一致："
            f"命令={expected[0]}/{expected[1]}/{expected[2]}，"
            f"任务记录={actual[0]}/{actual[1]}/{actual[2]}"
        )
    candidate_record = records.get(requested_task_id)
    if not isinstance(candidate_record, dict):
        return None, f"安全继续目标缺少第 1/2 步正式状态：{requested_task_id}"
    if str(candidate_record.get("pipelineStatus") or "").strip() == "completed":
        return None, "安全继续目标项目已经完成，无需再次恢复。"

    recovered = dict(candidate)
    recovered["canonicalTaskId"] = requested_task_id
    # The receiver's imported audit record intentionally remains immutable.
    # Only the active context reflects that Step 1/2 has already completed.
    recovered["status"] = "step12_completed"
    return recovered, ""


def _parse_iso_timestamp(value):
    """Return a comparable timestamp for persisted local control files."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def voice_terminal_predates_continue_command(handoff, command):
    """Whether a terminal Fish handoff belongs to an earlier recovery attempt.

    A cancelled handoff is durable on disk.  If a user clicked Dashboard
    "safe continue" *after* that terminal event, the old event is history and
    must never close the newer command merely because it has the same task ID.
    """
    if not isinstance(handoff, dict) or not isinstance(command, dict):
        return False
    terminal_at = next(
        (
            _parse_iso_timestamp(handoff.get(field))
            for field in ("cancelledAt", "canceledAt", "completedAt", "endedAt", "updatedAt")
            if _parse_iso_timestamp(handoff.get(field)) is not None
        ),
        None,
    )
    requested_at = next(
        (
            _parse_iso_timestamp(command.get(field))
            for field in ("requestedAt", "startedAt", "createdAt")
            if _parse_iso_timestamp(command.get(field)) is not None
        ),
        None,
    )
    return bool(terminal_at and requested_at and terminal_at < requested_at)


def _capcut_stopped_with_error(status, claimed_at):
    """Reason text when a terminal CapCut status stopped its task with an error.

    Applies only to a status written after the command was claimed whose
    current task was left unfinished with an error code.  Items the attempt
    completed earlier stay completed; the command itself still ended
    (2026-10-16 09:33: one item done, then CAPCUT_EDITOR_PROBE_TIMEOUT left
    the command reconciliation_required).  Returns "" otherwise.
    """
    if not isinstance(status, dict) or bool(status.get("running")):
        return ""
    written_at = _parse_iso_timestamp(status.get("updatedAt") or status.get("heartbeatAt"))
    claimed = _parse_iso_timestamp(claimed_at)
    if written_at is None or claimed is None or written_at.tzinfo is None or claimed.tzinfo is None:
        return ""
    if written_at < claimed:
        return ""
    current_id = str(status.get("currentTaskId") or "").strip()
    for item in status.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        if current_id and str(item.get("taskId") or "").strip() != current_id:
            continue
        state = str(item.get("state") or item.get("status") or "").strip().lower()
        if state in {"completed", "running"}:
            continue
        code = str(item.get("errorCode") or item.get("error_code") or "").strip()
        if code:
            return str(item.get("error") or "").strip() or f"CapCut 已停止：{code}"
    return ""


def batch_root_directory(value, fallback=""):
    """Recover the durable Step 1 root from a previously selected batch path."""
    raw = str(value or fallback or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    parts = path.parts
    try:
        index = parts.index("任务批次")
    except ValueError:
        return raw
    return str(Path(*parts[:index])) if index else raw


def default_capcut_client_path():
    candidates = (
        APP_DIR / "tools" / "CapCutBatchClient" / "CapCutBatchClient.exe",
        APP_DIR.parent / "capcut-auto-caption-client" / "bin" / "CapCutBatchClient.exe",
        APP_DIR.parents[2] / "capcut-auto-caption-client" / "bin" / "CapCutBatchClient.exe",
    )
    return str(next((path for path in candidates if path.is_file()), candidates[0]))


CAPCUT_DRAFT_PREFLIGHT_ERROR_CODES = {
    "CAPCUT_RECOVERY_DRAFT_BINDING_LOST",
    "CAPCUT_RECOVERY_DRAFT_IDENTITY_MISMATCH",
    "CAPCUT_UNEXPECTED_ACTIVE_DRAFT_BLOCKED",
    "CAPCUT_DRAFT_CHECKPOINT_ARCHIVE_NOT_FOUND",
    "CAPCUT_DRAFT_DIRECTORY_NOT_CREATED",
    "CAPCUT_ACTIVE_DRAFT_ROOT_PROBE_FAILED",
    "CAPCUT_SAMPLE_ASSIGNMENT_MISSING",
    "CAPCUT_SAMPLE_ASSIGNMENT_FILE_MISSING",
    "CAPCUT_SAMPLE_ASSIGNMENT_HASH_MISMATCH",
    "CAPCUT_SAMPLE_ASSIGNMENT_CONFLICT",
    "CAPCUT_VIDEO_CHECKPOINT_IDENTITY_MISMATCH",
    "CAPCUT_COMMITTED_SAMPLE_IDENTITY_MISMATCH",
    "CAPCUT_AUDIO_CHECKPOINT_IDENTITY_MISMATCH",
    "CAPCUT_AUDIO_CHECKPOINT_DUPLICATED",
    "CAPCUT_AUDIO_CHECKPOINT_OFFSET",
    "CAPCUT_AUDIO_CHECKPOINT_PROMOTION_FAILED",
    "CAPCUT_MEDIA_STAGE_REGRESSION_BLOCKED",
    "CAPCUT_COMMITTED_MEDIA_UNKNOWN_BLOCKED",
}


def capcut_failure_pipeline_status(item):
    """Describe a terminal client result without calling it a launch failure."""
    item = item if isinstance(item, dict) else {}
    error_code = str(
        item.get("errorCode") or item.get("error_code") or ""
    ).strip()
    detail = str(item.get("error") or "").strip()
    preflight = error_code in CAPCUT_DRAFT_PREFLIGHT_ERROR_CODES
    state = "blocked_capcut_preflight" if preflight else "blocked_capcut"
    text = (
        "客户端已启动，但草稿恢复预检被阻止"
        if preflight
        else "第 5 步 CapCut 需要处理，后续项目保持等待"
    )
    if error_code:
        text += f"：{error_code}"
    if detail:
        text += f"；{detail}"
    return state, text, error_code


DEFAULTS = {
    "tikhub_launcher": r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\Start_TikHub_AI.cmd",
    "tikhub_downloads": r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\downloads",
    "stitcher_launcher": r"C:\Users\ExampleUser\Documents\New project 2\AI板材视频拼接工具.pyw",
    "stitcher_projects": r"C:\Users\ExampleUser\Documents\New project 2\.board_video_projects.json",
    "subject_dir": r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\downloads",
    "ai_material_dir": "",
    "voice_client": str(VOICE_CLIENT_DIR / "BatchVoiceClient.exe"),
    "audio_dir": str(VOICE_CLIENT_DIR / "downloads"),
    "output_dir": "",
    "capcut_client": default_capcut_client_path(),
    "capcut_output_root": "E:\\",
    "download_workers": 3,
    # "0" turns off downloading the next project while this one is in Step 3-5.
    "prefetch_next_project": "1",
    "batch_name": datetime.now().strftime("批次_%Y%m%d_%H%M"),
}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
PROCESSED_SUFFIXES = ("_ai_sttn", "_ai_lama", "_blur", "_covered")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
URL_TRAILING_PUNCTUATION = ".,;，。；"
MATERIAL_NAMES = {
    "white_panel": "净白板",
    "carbon_crystal": "碳岩板",
    "metal_panel": "金属板",
    "mirror_panel": "镜面板",
    "wood_veneer": "木饰面",
    "pu_stone": "PU石",
    "flexible_pu_stone": "PU软石",
    "plastic_wood_flooring": "塑木地板",
    "crystal_rock_wall": "晶岩墙板",
}

# The remote task is always authoritative.  These entries are only used to
# create missing local stitcher presets; they never replace a task's audio,
# source-video or production-date handoff at runtime.
STITCH_MATERIAL_RULES = {
    "净白板": {"cover_material": "white_panel", "ai_folder": "净白板", "use_ai": True},
    "碳岩板": {"cover_material": "carbon_crystal", "ai_folder": "石纹", "use_ai": True},
    "金属板": {"cover_material": "metal_panel", "ai_folder": "金属板", "use_ai": True},
    "木饰面": {"cover_material": "wood_veneer", "ai_folder": "木纹", "use_ai": True},
    "镜面板": {"cover_material": "mirror_panel", "ai_folder": "", "use_ai": False},
    "PU石": {"cover_material": "pu_stone", "ai_folder": "", "use_ai": False},
    "PU软石": {"cover_material": "flexible_pu_stone", "ai_folder": "", "use_ai": False},
    "塑木地板": {"cover_material": "plastic_wood_flooring", "ai_folder": "", "use_ai": False},
    "晶岩墙板": {"cover_material": "crystal_rock_wall", "ai_folder": "石纹", "use_ai": True},
}

STITCH_PROJECT_MATERIALS = {
    "ProjectB": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "ProjectA": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "ProjectC": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "ProjectD": ("碳岩板", "净白板", "金属板", "镜面板", "木饰面", "PU石", "PU软石"),
    "SPC地板": ("塑木地板",),
    "SPC墙板": ("晶岩墙板",),
}

PROJECT_BRANDS = {"ProjectB": "ExampleBrandB", "ProjectA": "ExampleBrandC", "ProjectC": "ExampleBrandC", "ProjectD": "ExampleBrandC", "SPC地板": "ExampleBrandC", "SPC墙板": "ExampleBrandC"}


def default_material_audio_root(project, material):
    if project == "ProjectB":
        return str(Path(r"E:\音频\ProjectB音频") / material)
    if project == "SPC地板":
        return str(Path(r"E:\音频\SPC地板音频") / material)
    if project == "SPC墙板":
        return str(Path(r"E:\音频\SPC墙板音频") / material)
    return str(Path(rf"E:\音频\{project}音频"))


class Workbench(tk.Tk):
    BG, CARD, BORDER = "#F5F7FA", "#FFFFFF", "#E5E7EB"
    BLUE, BLUE_DARK = "#1677FF", "#0958D9"
    TEXT, MUTED = "#172033", "#667085"
    GREEN, ORANGE, RED, PURPLE = "#16A34A", "#D97706", "#DC2626", "#7C3AED"

    def __init__(self):
        super().__init__()
        self.title("TikHub AI 视频工作台")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        window_w = min(1900, max(1280, screen_w - 40))
        window_h = min(1000, max(760, screen_h - 90))
        self.geometry(f"{window_w}x{window_h}+10+10")
        self.minsize(1280, 760)
        self.configure(bg=self.BG)
        self.settings = self.load_settings()
        self.vars = {k: tk.StringVar(value=str(v)) for k, v in self.settings.items()}
        self.batch_root_sources = {
            key: batch_root_directory(self.settings.get(key), DEFAULTS.get(key))
            for key in ("tikhub_downloads", "subject_dir")
        }
        self.mode = tk.StringVar(value="AI 去除 STTN")
        self.auto_subtitle = tk.BooleanVar(value=True)
        self.preview_first = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="正在检查运行环境…")
        self.link_count_var = tk.StringVar(value="已识别 0 个")
        self.file_count_var = tk.StringVar(value="0 个视频")
        self.total_var = tk.StringVar(value="0")
        self.success_var = tk.StringVar(value="0")
        self.failed_var = tk.StringVar(value="0")
        self.progress_var = tk.StringVar(value="0%")
        self.logo_brand = tk.StringVar(value="无")
        self.product_info_var = tk.StringVar(value="尚未扫描成品目录")
        self.product_category_var = tk.StringVar(value="")
        self.product_catalog = {}
        self.product_category_paths = {}
        self.live_tasks = {}
        self.task_rows = {}
        self.last_batch_result = None
        self.last_download_runtime_failure = None
        self.download_task_errors = {}
        self.download_batch_context = {}
        self.last_stitch_result = None
        self.current_task_view = "live"
        self.download_process = None
        self.current_page = "download"
        self.stitcher_controller = None
        self.stitcher_load_error = None
        self.capcut_process = None
        self.capcut_manifest = None
        self.upload_process = None
        self.upload_status_var = tk.StringVar(value="正在检查百度网盘配置…")
        self.upload_detail_var = tk.StringVar(value="尚未授权")
        self.upload_account_var = tk.StringVar(value="--")
        self.upload_queue_var = tk.StringVar(value="0")
        self.upload_completed_var = tk.StringVar(value="0")
        self.upload_failed_var = tk.StringVar(value="0")
        self.voice_batch_context_var = tk.StringVar(value="未导入外部任务：按原有语音目录规则运行。")
        self.inbox_task_var = tk.StringVar(value="")
        self.inbox_info_var = tk.StringVar(value="外部任务收件箱正在检查…")
        self.inbox_tasks = {}
        self.active_external_task = load_current_context()
        self.capcut_store = CapCutHandoffStore(
            APP_DIR / "交接记录",
            Path(self.vars["capcut_output_root"].get().strip() or "E:\\"),
        )
        self.capcut_date_var = tk.StringVar()
        self.capcut_summary_var = tk.StringVar(value="尚未读取第四步任务")
        self.capcut_project_vars = {name: tk.BooleanVar(value=True) for name in CATEGORIES}
        self.capcut_status_vars = {
            "pending": tk.BooleanVar(value=True),
            "failed": tk.BooleanVar(value=True),
            "completed": tk.BooleanVar(value=False),
        }
        self.voice_handoff_handled = set()
        self.voice_auto_launch_task_ids = set()
        self.voice_batch_process = None
        self.voice_launch_identity = {}
        self.voice_launch_records = {}
        self.last_voice_launch_error = None
        self.stitch_auto_launch_task_ids = set()
        self.capcut_auto_launch_task_ids = set()
        self.capcut_launch_records = {}
        self.auto_recovery_var = tk.BooleanVar(value=False)
        self._auto_recovery_busy = False
        self.recovery_control = RecoveryControl(
            AUTO_RECOVERY_STATE_FILE,
            AUTO_RECOVERY_LOCK_FILE,
            CONTINUE_COMMAND_FILE,
        )
        try:
            recovery_state = self._read_auto_recovery_state()
            self.auto_recovery_var.set(
                not bool(recovery_state.get("manualPause")) and not bool(recovery_state.get("readError"))
            )
        except Exception:
            self.auto_recovery_var.set(False)
        # Remote submitters send an ordered task box.  The workbench claims one
        # pending item at a time, so a later project never competes with the
        # current CUDA subtitle job.
        self.auto_start_claimed_task_ids = set()
        self._load_voice_handoff_state()
        self.setup_styles()
        self.build_ui()
        self.restore_download_session()
        self.after(150, self.check_environment)
        self.after(1200, self.poll_voice_handoff)
        self.after(1800, self.poll_capcut_status)
        self.after(900, self.ensure_upload_service)
        self.after(2200, self.poll_upload_status)
        self.after(2600, self.poll_task_inbox)
        self.after(2800, self.poll_control_commands)
        self.after(2000, self.publish_ops_status)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_settings(self):
        data = dict(DEFAULTS)
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update({k: v for k, v in saved.items() if k in data})
        except (OSError, ValueError):
            pass
        if not str(data.get("audio_dir", "")).strip():
            data["audio_dir"] = str(VOICE_CLIENT_DIR / "downloads")
        return data

    def save_settings(self):
        payload = {k: v.get().strip() for k, v in self.vars.items()}
        SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_download_session(self):
        """Persist the visible Step 1/2 task table so a restart never hides failures."""
        payload = {
            "schemaVersion": 1,
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "context": self.download_batch_context,
            "activeExternalTask": self.active_external_task or {},
            "tasks": [
                {"url": url, "values": list(values)}
                for url, values in self.live_tasks.items()
            ],
            "lastBatchResult": self.last_batch_result or {},
            "downloadRuntimeFailure": self.last_download_runtime_failure or {},
            "taskErrors": self.download_task_errors,
            "stats": {
                "total": self.total_var.get(),
                "success": self.success_var.get(),
                "failed": self.failed_var.get(),
                "progress": self.progress_var.get(),
                "status": self.status_var.get(),
            },
        }
        try:
            DOWNLOAD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = DOWNLOAD_STATE_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, DOWNLOAD_STATE_FILE)
        except OSError as exc:
            self.write_log(f"保存下载批次状态失败：{exc}")

    def _expected_download_count(self) -> int:
        task = self.active_external_task or {}
        try:
            return max(1, int(task.get("expectedCount") or len(self.live_tasks) or 1))
        except (TypeError, ValueError):
            return max(1, len(self.live_tasks))

    @staticmethod
    def _pid_has_visible_window(pid: int) -> bool:
        """Return whether a Windows process still owns a visible top-level window."""
        if os.name != "nt" or pid <= 0:
            return False
        try:
            user32 = ctypes.windll.user32
            found = {"value": False}
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def visit(hwnd, _lparam):
                owner_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
                if int(owner_pid.value) == int(pid) and user32.IsWindowVisible(hwnd):
                    found["value"] = True
                    return False
                return True

            user32.EnumWindows(callback_type(visit), 0)
            return bool(found["value"])
        except (AttributeError, OSError):
            return False

    def _capcut_manifest_runtime(self, manifest_value):
        """Read only the ownership and status file declared by one manifest."""
        path = Path(str(manifest_value or "").strip())
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            return "", ""
        if not isinstance(payload, dict):
            return "", ""
        return (
            str(payload.get("remotePipelineTaskId") or "").strip(),
            str(payload.get("statusFile") or "").strip(),
        )

    def _retire_stale_capcut_clients(self, current_task_id):
        """Detach only terminal automatic-client processes from earlier tasks.

        The user-facing CapCut editor is never discovered or terminated here.
        A process is eligible only when Workbench launched it, its manifest is
        terminal, and it has no visible window.
        """
        current_task_id = str(current_task_id or "").strip()
        for owner_id, record in list(self.capcut_launch_records.items()):
            if owner_id == current_task_id:
                continue
            process = record.get("process")
            if process is None or process.poll() is not None:
                self.capcut_launch_records.pop(owner_id, None)
                self.capcut_auto_launch_task_ids.discard(owner_id)
                continue
            manifest_owner, status_file = self._capcut_manifest_runtime(record.get("manifestId"))
            terminal = not self._capcut_status_file_is_running(status_file)
            visible = self._pid_has_visible_window(int(record.get("pid") or 0))
            if not terminal or visible or (manifest_owner and manifest_owner != owner_id):
                continue
            try:
                process.terminate()
                process.wait(timeout=3)
                self.write_log(f"已结束上一项目的终态 CapCut 自动客户端：{owner_id}。")
            except (OSError, subprocess.SubprocessError):
                self.write_log(f"上一项目 CapCut 自动客户端仍在后台；已解除对当前任务的阻塞：{owner_id}。")
            self.capcut_launch_records.pop(owner_id, None)
            self.capcut_auto_launch_task_ids.discard(owner_id)
            if self.capcut_process is process:
                self.capcut_process = None
                self.capcut_manifest = None

    def _current_capcut_client_is_running(self, task_id) -> bool:
        """A CapCut client blocks recovery only when it belongs to this task."""
        task_id = str(task_id or "").strip()
        self._retire_stale_capcut_clients(task_id)
        record = self.capcut_launch_records.get(task_id) or {}
        process = record.get("process")
        if process is None or process.poll() is not None:
            self._release_capcut_launch_guard(task_id)
            return False
        owner_id, status_file = self._capcut_manifest_runtime(record.get("manifestId"))
        if owner_id != task_id or not self._capcut_status_file_is_running(status_file):
            self._release_capcut_launch_guard(task_id)
            return False
        return True

    def _wait_for_previous_capcut_client_exit(self, next_task_id, timeout_seconds=10.0):
        """Keep automatic Step 5 launches serial across remote projects.

        The calibrated client is intentionally a single shared installation.
        A terminal client normally closes itself, but the workbench must not
        race that close and spawn a second writer for the same task table.
        This routine only waits for clients that this workbench launched.  It
        never terminates a visible CapCut editor or an unknown user process.
        """
        next_task_id = str(next_task_id or "").strip()
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        for owner_id, record in list(self.capcut_launch_records.items()):
            if owner_id == next_task_id:
                continue
            process = record.get("process")
            if process is None or process.poll() is not None:
                self._release_capcut_launch_guard(owner_id)
                continue

            manifest_owner, status_file = self._capcut_manifest_runtime(record.get("manifestId"))
            terminal = not self._capcut_status_file_is_running(status_file)
            if manifest_owner and manifest_owner != owner_id:
                continue
            if not terminal:
                return False, f"previous CapCut client for {owner_id} is still processing"

            self.write_log(
                f"Waiting for terminal CapCut client {owner_id} (PID {record.get('pid')}) before starting {next_task_id}."
            )
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if process.poll() is None:
                return False, (
                    f"capcut_client_exit_timeout: terminal client {owner_id} "
                    f"(PID {record.get('pid')}) did not exit within {int(timeout_seconds)} seconds"
                )
            self._release_capcut_launch_guard(owner_id)
        return True, ""

    def _close_step12_replenishment(self) -> tuple[int, bool]:
        """Merge a finished gap-fill into its parent before any later step.

        The child task is a transport/audit record only.  Once the parent's
        source count is complete, all downstream work must run with the
        original task configuration and identity.
        """
        task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        parent_id = str(task.get("replenishForTaskId") or "").strip()
        if not parent_id:
            missing = max(0, self._expected_download_count() - self._integer(self.success_var.get()))
            return missing, True

        registry = load_project_download_statuses().get("items", {})
        parent_status = registry.get(parent_id) if isinstance(registry, dict) else None
        if not isinstance(parent_status, dict):
            detail = "补链已完成，但找不到原项目状态；已停在第 1/2 步，未启动后续流程。"
            self._set_remote_pipeline_status(parent_id, "blocked_reconcile", detail)
            self.write_log(detail)
            return 0, False
        expected = max(1, self._integer(parent_status.get("expectedCount"), 1))
        completed = max(0, self._integer(parent_status.get("completed"), 0))
        missing = max(0, self._integer(parent_status.get("missingCount"), expected - completed))
        if missing:
            # A completed transport can contribute fewer sources than the total
            # remaining gap. Retain its checkpoint/audit and release admission;
            # downstream remains blocked until the original parent is complete.
            child_id = str(task.get("taskId") or "").strip()
            if child_id:
                mark_replenishment_merged(child_id, parent_id)
            return missing, False
        if completed != expected:
            detail = f"补链对账异常：父项目计划 {expected} 条、有效成品 {completed} 条；已停止后续流程。"
            self._set_remote_pipeline_status(parent_id, "blocked_reconcile", detail)
            self.write_log(detail)
            return 0, False

        parent = load_task_record(parent_id)
        if not isinstance(parent, dict):
            detail = "补链数量已完整，但找不到原任务配置；已停在第 1/2 步，未启动配音。"
            self._set_remote_pipeline_status(parent_id, "blocked_reconcile", detail)
            self.write_log(detail)
            return 0, False

        child_id = str(task.get("taskId") or "").strip()
        restored = dict(parent)
        restored.update({
            "canonicalTaskId": parent_id,
            "status": "step12_completed",
            "step1DownloadDirectory": str(
                task.get("step1DownloadDirectory")
                or parent.get("step1DownloadDirectory")
                or self.vars["tikhub_downloads"].get()
            ),
            "replenishmentClosedByTaskId": child_id,
        })
        self.active_external_task = restored
        self.download_batch_context["activeExternalTask"] = restored
        self.total_var.set(str(expected))
        self.success_var.set(str(expected))
        self.failed_var.set("0")
        self.progress_var.set("100%")
        self.last_batch_result = {"total": expected, "succeeded": expected, "failed": 0}
        save_current_context(restored)
        # Closing the parent retires every transport batch that contributed
        # media, not just the last gap-fill click.  Otherwise an earlier
        # partial replenishment remains active and its valid checkpoint is
        # easy to lose on the next stage/restart.
        for contributor_id in self._task_checkpoint_ids(restored):
            if contributor_id != parent_id:
                mark_replenishment_merged(contributor_id, parent_id)
        self.save_download_session()
        self._set_remote_pipeline_status(parent_id, "voice_pending", "补链已闭环，正在检查第 3 步配音")
        self.write_log(f"第 1/2 步补链闭环完成：原项目 {parent_id} 已补齐 {expected}/{expected}，恢复父任务进入后续流程。")
        return 0, True

    def _advance_after_step12(self, task=None, force_voice_retry=False):
        """Advance one fully reconciled task to exactly its next incomplete step."""
        task = task or self.active_external_task or load_current_context()
        if not isinstance(task, dict):
            return False
        task_id = canonical_task_id(task)
        if not task_id:
            return False
        completed_handoff = self._completed_voice_handoff_for_task(task)
        if completed_handoff is not None:
            self._set_remote_pipeline_status(task_id, "voice_completed", "已验证当前项目配音，正在进入第 4 步")
            self._handle_voice_handoff(completed_handoff)
            return task_id in self.stitch_auto_launch_task_ids
        self._set_remote_pipeline_status(task_id, "voice_pending", "第 1/2 步已完整，正在准备第 3 步配音")
        self.last_voice_launch_error = None
        started = self._auto_launch_voice_after_download(force_retry=force_voice_retry)
        if started:
            return True
        if task_id in self.voice_auto_launch_task_ids:
            self._set_remote_pipeline_status(task_id, "voice_running", "第 3 步配音已在启动或运行")
            return True
        transition_error = self.last_voice_launch_error
        if (
            isinstance(transition_error, dict)
            and str(transition_error.get("taskId") or "") == task_id
        ):
            detail = str(transition_error.get("message") or "第 3 步配音准备失败")
            error_code = str(transition_error.get("errorCode") or "VOICE_PLAN_CREATE_FAILED")
            self._set_remote_pipeline_status(task_id, "blocked_voice", detail, error_code=error_code)
            self.status_var.set(detail)
            return False
        launch_state = self._read_local_json_object(self._voice_launch_state_file())
        identity = self._voice_task_identity(task)
        matching_launch = self._voice_identity_matches(launch_state, identity)
        detail = (
            str(launch_state.get("message") or "").strip()
            if matching_launch else ""
        ) or "第 1/2 步已完整，但第 3 步配音未能启动；任务和媒体已保留，可安全继续重试。"
        error_code = str(launch_state.get("errorCode") or "") if matching_launch else ""
        self._set_remote_pipeline_status(task_id, "blocked_voice", detail, error_code=error_code)
        self.status_var.set(detail)
        self.write_log(detail)
        return False

    def _download_failure_items(self):
        items = []
        for url, values in self.live_tasks.items():
            download_status = str(values[1] if len(values) > 1 else "")
            subtitle_status = str(values[2] if len(values) > 2 else "")
            stage = str(values[3] if len(values) > 3 else "")
            if download_status == "失败" or subtitle_status == "失败" or stage == "处理失败":
                reason = " / ".join(
                    part for part in (stage, download_status, subtitle_status)
                    if part and part not in {"失败", "处理失败"}
                )
                detail = self.download_task_errors.get(url) if isinstance(self.download_task_errors, dict) else None
                item = {"url": url, "reason": reason or "下载或 AI 去字幕失败"}
                if isinstance(detail, dict):
                    item.update({key: detail.get(key) for key in ("errorCode", "phase", "message") if detail.get(key)})
                items.append(item)
        return items

    def save_project_download_progress(self, phase="running"):
        """Persist one project card for remote Dashboard/submitter synchronisation."""
        task = self.active_external_task or {}
        task_id = str(task.get("taskId") or "").strip()
        if not task_id:
            return
        current_task = load_current_context()
        if (
            isinstance(current_task, dict)
            and str(current_task.get("taskId") or "") == task_id
            and current_task.get("status") == "cancelled"
        ):
            remove_project_download_status(task_id)
            return
        expected = self._expected_download_count()
        completed = self._integer(self.success_var.get())
        failures = self._download_failure_items()
        failed = max(self._integer(self.failed_var.get()), len(failures))
        report_task = task
        replenish_for = str(task.get("replenishForTaskId") or "").strip()
        if replenish_for:
            registry = load_project_download_statuses().get("items", {})
            parent = registry.get(replenish_for) if isinstance(registry, dict) else {}
            if not isinstance(parent, dict):
                parent = {}
            try:
                expected = max(1, int(task.get("replenishmentBaseExpected") or parent.get("expectedCount") or expected))
                completed_before = max(0, int(task.get("replenishmentCompletedBefore", parent.get("completed") or 0)))
            except (TypeError, ValueError):
                completed_before = 0
            contributed = max(completed, completed_download_checkpoint_count(task))
            completed = min(expected, completed_before + contributed)
            # The child contributes media/progress only.  The parent's task-box
            # identity is authoritative and must not be overwritten by the
            # one-item replenishment transport batch.
            report_task = {**task, **parent, "taskId": replenish_for}
        missing = max(0, expected - completed)
        registry = load_project_download_statuses().get("items", {})
        existing = registry.get(str(report_task.get("taskId") or task_id), {}) if isinstance(registry, dict) else {}
        pipeline_status = str(existing.get("pipelineStatus") or "").strip()
        if phase == "waiting":
            completed = completed_before if replenish_for else 0
            failed, failures, missing = 0, [], 0
            status, text = "waiting_download", "任务已收到，等待家用电脑确认开始下载"
            pipeline_status = "waiting"
        elif phase == "interrupted":
            # Closing the workbench is not a terminal download result.  Keep
            # the real completed count, but do not turn the remaining original
            # URLs into a replenishment gap on the next startup.
            missing = 0
            runtime_failure = self.last_download_runtime_failure or {}
            runtime_reason = str(runtime_failure.get("message") or "").strip()
            if runtime_reason:
                status, text = "blocked_download_runtime", (
                    f"下载与 AI 去字幕运行中断：{runtime_reason}；"
                    f"已完成 {completed} / {expected}，可安全继续"
                )
                pipeline_status = "blocked_download_runtime"
            else:
                status, text = "interrupted", f"下载与 AI 去字幕已中断：{completed} / {expected}，可安全继续"
                pipeline_status = "step12_interrupted"
        elif phase == "runtime_failed":
            # A bridge/process failure is not a missing-link contract.  The
            # original URL set remains complete and can be resumed in place.
            missing = 0
            failed = len(failures)
            failure = self.last_download_runtime_failure or {}
            reason = str(failure.get("message") or "下载桥异常退出")
            status, text = "blocked_download_runtime", f"下载与 AI 去字幕运行中断：{reason}；可安全继续"
            pipeline_status = "blocked_download_runtime"
        elif phase == "running":
            # Progress is not a deficit.  A missing-link decision is only
            # valid after the bridge has emitted its final result.
            missing = 0
            status, text = "running", f"正在下载与 AI 去字幕：{completed} / {expected}"
            if pipeline_status != "completed":
                pipeline_status = "downloading"
        elif missing:
            status, text = "blocked_missing_links", f"待补链：已完成 {completed} / {expected}，还差 {missing} 条"
            pipeline_status = "blocked_missing_links"
        else:
            status, text = "completed", f"下载与 AI 去字幕完成：{completed} / {expected}"
            if pipeline_status != "completed":
                pipeline_status = "step1_completed"
        gap_active = status == "blocked_missing_links"
        terminal = status in {"blocked_missing_links", "completed"}
        try:
            gap_revision = max(0, int(existing.get("gapRevision") or 0))
        except (TypeError, ValueError):
            gap_revision = 0
        if gap_active and (
            not bool(existing.get("gapActive"))
            or self._integer(existing.get("missingCount"), 0) != missing
        ):
            gap_revision += 1
        try:
            save_project_download_status({
                "taskId": str(report_task.get("taskId") or task_id),
                "batchId": str(report_task.get("batchId") or ""),
                "runGroupId": str(report_task.get("runGroupId") or ""),
                "batchKind": str(report_task.get("batchKind") or ("examplebrand-natural-flow" if str(report_task.get("project") or "") == "ExampleBrand" else "formal")),
                "selectedProjects": list(report_task.get("selectedProjects") or []),
                "batchSequence": report_task.get("batchSequence"),
                "batchSize": report_task.get("batchSize"),
                "date": str(report_task.get("date") or ""),
                "project": str(report_task.get("project") or ""),
                "material": str(report_task.get("material") or ""),
                "audioBatch": str(report_task.get("audioBatch") or ""),
                "voicePlan": str(report_task.get("voicePlan") or ""),
                "expectedCount": expected,
                "submittedCount": expected if replenish_for else len(self.live_tasks),
                "completed": completed,
                "failed": failed,
                "missingCount": missing,
                "terminal": terminal,
                "notStarted": status == "waiting_download",
                "gapActive": gap_active,
                "gapRevision": gap_revision,
                "replenishmentState": (
                    "completed" if replenish_for and terminal and not missing
                    else "partial" if replenish_for and terminal and missing
                    else "running" if replenish_for
                    else str(existing.get("replenishmentState") or "")
                ),
                "failureItems": failures,
                "runtimeFailure": self.last_download_runtime_failure or {},
                "status": status,
                "stateText": text,
                "pipelineStatus": pipeline_status,
                "pipelineStateText": (
                    text if phase in {"waiting", "running", "interrupted", "runtime_failed"}
                    else str(existing.get("pipelineStateText") or text)
                ),
                "lastReplenishmentTaskId": task_id if replenish_for else "",
                "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
        except (OSError, ValueError) as exc:
            self.write_log(f"保存项目缺口状态失败：{exc}")

    def restore_download_session(self):
        imported_task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        imported_id = str(imported_task.get("taskId") or "").strip()
        if imported_id and imported_task.get("status") == "cancelled":
            self.active_external_task = {}
            self.download_batch_context = {}
            self.last_batch_result = None
            self.tree.delete(*self.tree.get_children())
            self.live_tasks.clear()
            self.task_rows.clear()
            self.total_var.set("0"); self.success_var.set("0"); self.failed_var.set("0"); self.progress_var.set("0%")
            self.status_var.set("上一条远程任务已撤回，未恢复下载会话。")
            self.save_download_session()
            return
        try:
            payload = json.loads(DOWNLOAD_STATE_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            if imported_id and imported_task.get("status") == "imported_waiting_start":
                self._restore_imported_waiting_task(imported_task)
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            if imported_id and imported_task.get("status") == "imported_waiting_start":
                self._restore_imported_waiting_task(imported_task)
            return
        # A task that has been imported but not started is the authoritative
        # context.  Do not revive an earlier finished/failed table over it:
        # that made a 3-link replenishment look like the old 25-link batch.
        saved_task = payload.get("activeExternalTask") if isinstance(payload.get("activeExternalTask"), dict) else {}
        saved_id = str(saved_task.get("taskId") or "").strip()
        saved_stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        saved_result = payload.get("lastBatchResult") if isinstance(payload.get("lastBatchResult"), dict) else {}
        saved_started = bool(saved_result) or any(
            self._integer(saved_stats.get(key)) > 0 for key in ("success", "failed")
        ) or self._integer(saved_stats.get("progress")) > 0
        if imported_id and imported_task.get("status") == "imported_waiting_start" and (imported_id != saved_id or not saved_started):
            self._restore_imported_waiting_task(imported_task)
            return
        self.download_batch_context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        restored = 0
        self.tree.delete(*self.tree.get_children())
        self.live_tasks.clear(); self.task_rows.clear()
        for item in payload.get("tasks") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            values = item.get("values")
            if not url or not isinstance(values, list) or len(values) < 7:
                continue
            normalized = tuple(str(value) for value in values[:7])
            self.live_tasks[url] = normalized
            self.task_rows[url] = self.tree.insert("", "end", values=normalized)
            restored += 1
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        self.total_var.set(str(stats.get("total") or restored))
        self.success_var.set(str(stats.get("success") or "0"))
        self.failed_var.set(str(stats.get("failed") or "0"))
        self.progress_var.set(str(stats.get("progress") or "0%"))
        result = payload.get("lastBatchResult")
        self.last_batch_result = result if isinstance(result, dict) and result else None
        runtime_failure = payload.get("downloadRuntimeFailure")
        self.last_download_runtime_failure = runtime_failure if isinstance(runtime_failure, dict) and runtime_failure else None
        task_errors = payload.get("taskErrors")
        self.download_task_errors = task_errors if isinstance(task_errors, dict) else {}
        if restored:
            self.status_var.set(f"已恢复上次批次：{restored} 条任务；失败任务会一直保留，直到你新建批次。")
            self.write_log(f"已恢复下载批次状态：{restored} 条任务。")
            restored_incomplete = (
                self.last_batch_result is None
                and any(
                    not (str(values[1]) == "已完成" and str(values[2]) == "已完成")
                    for values in self.live_tasks.values() if len(values) >= 3
                )
            )
            self.save_project_download_progress("interrupted" if restored_incomplete else "finished")
            if self._integer(self.failed_var.get()) > 0 or self._expected_download_count() > self._integer(self.success_var.get()):
                # Keep restored failures visible in Dashboard and the log, but
                # never queue a modal dialog.  A new remote task may already
                # be downloading when this delayed callback would otherwise
                # interrupt it with an unrelated old-batch warning.
                self.write_log("上次批次存在失败：已同步到 Dashboard，不弹出旧批次提醒。")

    def _restore_imported_waiting_task(self, task):
        """Restore a received task without starting it or mixing it with an older batch."""
        urls = [str(url).strip() for url in (task.get("urls") or []) if str(url).strip()]
        source_key = str(task.get("download_path_source") or "tikhub_downloads")
        source_var = self.vars.get(source_key)
        source_root = self.batch_root_sources.get(source_key) or (source_var.get().strip() if source_var else "")
        configured_directory = str(task.get("step1DownloadDirectory") or "").strip()
        if configured_directory:
            batch_directory = Path(configured_directory)
        elif source_root:
            batch_directory = batch_download_directory(source_root, task)
        else:
            batch_directory = None
        if batch_directory:
            self.vars["tikhub_downloads"].set(str(batch_directory))
        if task.get("subtitle_mode") in self.mode_cards:
            self.select_mode(task["subtitle_mode"])
        self.auto_subtitle.set(bool(task.get("subtitle_enabled", True)))
        self.vars["download_workers"].set(str(self._integer(task.get("download_workers"), 2)))
        self.links.delete("1.0", "end")
        self.links.insert("1.0", "\n".join(urls))
        self.links.edit_modified(False)
        self.update_link_count()
        self.tree.delete(*self.tree.get_children())
        self.live_tasks.clear(); self.task_rows.clear()
        for url in urls:
            values = (url, "等待", "未开始", "等待下载", "0%", "…", "")
            self.live_tasks[url] = values
            self.task_rows[url] = self.tree.insert("", "end", values=values)
        self.download_batch_context = {
            "date": str(task.get("date") or ""),
            "project": str(task.get("project") or ""),
            "material": str(task.get("material") or ""),
            "pendingImport": True,
            "request": {
                "taskId": str(task.get("taskId") or ""),
                "urls": list(urls),
                "download_path": str(batch_directory or ""),
            },
        }
        self.last_batch_result = None
        self.last_download_runtime_failure = None
        self.download_task_errors = {}
        self.total_var.set(str(len(urls))); self.success_var.set("0"); self.failed_var.set("0"); self.progress_var.set("0%")
        descriptor = "补链任务" if task.get("isReplenishment") else "待开始任务"
        self.status_var.set(f"已恢复{descriptor}：{len(urls)} 条链接，尚未开始下载。")
        self.show_task_view("live")
        self.save_download_session()
        self.save_project_download_progress("waiting")
        self.write_log(self.status_var.get())

    def batch_completeness_report(self) -> tuple[bool, str]:
        """Compare persisted link results, voice tasks and CapCut handoff without guessing by filename."""
        context = self.download_batch_context if isinstance(self.download_batch_context, dict) else {}
        project = str(context.get("project") or (self.active_external_task or {}).get("project") or "").strip()
        material = str(context.get("material") or (self.active_external_task or {}).get("material") or "").strip()
        date_value = str(context.get("date") or (self.active_external_task or {}).get("date") or self.capcut_date_var.get() or "").strip()
        batch_label = f"批次：{date_value or '未标记日期'} / {project or '未标记项目'} / {material or '未标记材料'}"
        if self.download_process and self.download_process.poll() is None:
            return False, "\n".join((
                batch_label,
                "结果：第 1 步正在下载与 AI 去字幕，完成后再核对缺口。",
                "当前不会把尚未完成的任务误判为缺口。",
            ))
        if self.last_batch_result is None:
            return False, "\n".join((
                batch_label,
                "结果：任务已导入，尚未开始下载与 AI 去字幕。",
                "请完成第 1 步后再核对实际成品；此时不会提示补链接。",
            ))
        failed_links = [
            url for url, values in self.live_tasks.items()
            if len(values) >= 4 and (str(values[1]) == "失败" or str(values[2]) == "失败" or str(values[3]) == "处理失败")
        ]
        lines = [
            batch_label,
            f"下载与去字幕：成功 {self.success_var.get()}，失败 {len(failed_links)}，计划 {self._expected_download_count()} 条",
        ]
        problems = []
        missing_count = max(0, self._expected_download_count() - self._integer(self.success_var.get()))
        if missing_count:
            problems.append(f"本项目还缺 {missing_count} 条视频；补链后只重试缺失项，不新建批次。")
            lines.append(f"项目缺口：已完成 {self.success_var.get()} / 计划 {self._expected_download_count()}，还差 {missing_count} 条")
        if failed_links:
            problems.append(f"下载或去字幕失败 {len(failed_links)} 条，需要补链接后重试。")
            lines.append("失败链接：")
            lines.extend(f"- {url}" for url in failed_links[:8])
            if len(failed_links) > 8:
                lines.append(f"- 其余 {len(failed_links) - 8} 条见任务表")

        voice_items = []
        try:
            voice_payload = json.loads(self._voice_handoff_file().read_text(encoding="utf-8-sig"))
            for item in (voice_payload.get("completedItems") or []) + (voice_payload.get("failedItems") or []):
                if not isinstance(item, dict):
                    continue
                if project and str(item.get("project") or "") != project:
                    continue
                if material and str(item.get("material") or "") != material:
                    continue
                voice_items.append(item)
        except (OSError, ValueError, UnicodeError):
            pass

        if voice_items and date_value:
            handoff = self.capcut_store.load_date(date_value)
            capcut_tasks = [
                item for item in handoff.get("tasks") or []
                if (not project or str(item.get("project") or "") == project)
                and (not material or str(item.get("material") or "") == material)
            ]
            capcut_keys = {
                (str(item.get("language") or ""), str(item.get("number") or "").zfill(3))
                for item in capcut_tasks
            }
            missing = []
            voice_failed = 0
            for item in voice_items:
                language = str(item.get("language") or "")
                number = str(item.get("taskId") or item.get("id") or "").zfill(3)
                if not str(item.get("audioPath") or ""):
                    voice_failed += 1
                    continue
                if (language, number) not in capcut_keys:
                    missing.append(f"{language} / {number}")
            if voice_failed:
                problems.append(f"语音生成失败或缺文件 {voice_failed} 条。")
            if missing:
                problems.append(f"尚未进入拼接/CapCut 的语言任务 {len(missing)} 条。")
                lines.append("缺少语言视频：" + "、".join(missing[:12]))
            failed_capcut = [item for item in capcut_tasks if str(item.get("status") or "") in {"failed", "validation_failed", "manual_review", "stopped"}]
            if failed_capcut:
                problems.append(f"拼接或 CapCut 异常 {len(failed_capcut)} 条。")
            lines.append(f"语言核对：应有 {len(voice_items)} 条，已进入第五步 {len(capcut_tasks)} 条。")
        elif date_value:
            lines.append("语言核对：尚未找到本批语音交接记录，完成语音后可再次核对。")
        else:
            lines.append("语言核对：请通过任务投递器导入项目，系统才能精确关联项目、材料和语言。")

        if problems:
            lines.insert(1, "结果：发现缺口，建议先补齐再进入下一步。")
            lines.append("提醒：系统不会自动补猜材料、语言或视频；只提示明确缺口。")
        else:
            lines.insert(1, "结果：当前没有检测到明确缺口。")
        return bool(problems), "\n".join(lines)

    def show_batch_completeness(self):
        has_problems, report = self.batch_completeness_report()
        self.write_log(report.replace("\n", " | "))
        # A remotely submitted batch is intentionally only queued.  It is not
        # a gap and should not interrupt the home/remote operator with a modal
        # dialog before step 1 has actually run.
        if self.last_batch_result is None:
            self.status_var.set("任务已收到，等待在家用电脑开始第 1 步；完成后才会核对缺口。")
            return
        if self.download_process and self.download_process.poll() is None:
            self.status_var.set("第 1 步正在运行；完成后会自动记录结果，再核对缺口。")
            return
        if has_problems:
            messagebox.showwarning("批次缺口提醒", report)
        else:
            messagebox.showinfo("批次核对", report)

    @staticmethod
    def _integer(value, default=0):
        try:
            return int(float(str(value).strip().rstrip("%")))
        except (TypeError, ValueError):
            return default

    def _download_ops_status(self):
        running = bool(self.download_process and self.download_process.poll() is None)
        total = self._integer(self.total_var.get())
        completed = self._integer(self.success_var.get())
        failed = self._integer(self.failed_var.get())
        progress = self._integer(self.progress_var.get())
        current = None
        failure_items = []
        for values in self.live_tasks.values():
            stage = str(values[3] if len(values) > 3 else "")
            download_status = str(values[1] if len(values) > 1 else "")
            subtitle_status = str(values[2] if len(values) > 2 else "")
            if download_status == "失败" or subtitle_status == "失败" or stage == "处理失败":
                reason = " / ".join(
                    part for part in (stage, download_status, subtitle_status)
                    if part and part not in {"失败", "处理失败"}
                )
                failure_items.append({
                    "url": str(values[0] if values else ""),
                    "reason": reason or "下载或 AI 去字幕失败",
                })
            if stage not in {"等待下载", "等待拼接配置", "处理失败"}:
                current = {
                    "name": str(values[0]),
                    "stage": stage,
                    "progressText": str(values[4] if len(values) > 4 else ""),
                }
                break
        if running:
            state = "running"
            state_text = (current or {}).get("stage") or "下载与AI去字幕"
        elif self.last_batch_result:
            state = "failed" if failed else "completed"
            state_text = "部分失败" if failed else "下载与去字幕完成"
            progress = 100
        else:
            state = "idle"
            state_text = "等待下载任务"
        return {
            "state": state,
            "stateText": state_text,
            "total": total,
            "completed": completed,
            "failed": failed,
            "progress": progress,
            "current": current,
            "failureItems": failure_items,
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _voice_ops_status(self):
        active = self.active_external_task or load_current_context() or {}
        active_identity = self._voice_task_identity(active)
        expected_total = len(contract_language_plan(active)) if isinstance(active, dict) else 0
        launch = self._read_local_json_object(self._voice_launch_state_file())
        if (self._voice_identity_matches(launch, active_identity)
                and launch.get('launchState') == 'failed'
                and launch.get('errorCode') in {'VOICE_DEPENDENCIES_MISSING', 'VOICE_DEPENDENCY_CHECK_FAILED'}):
            return {'state': 'failed', 'stateText': str(launch.get('message') or '配音依赖检查失败'),
                    'total': expected_total, 'completed': 0, 'failed': 1, 'progress': 0,
                    'phase': 'dependency_preflight', 'errorCode': launch['errorCode'], **active_identity}
        path = self._voice_handoff_file()
        if not path.is_file():
            return {
                "state": "idle", "stateText": f"{active_identity.get('audioBatch') or '当前批次'}配音等待启动",
                "total": expected_total, "completed": 0, "failed": 0, "progress": 0,
                **active_identity,
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            return {
                "state": "failed", "stateText": "语音状态读取失败",
                "total": 0, "completed": 0, "failed": 1, "progress": 0,
            }
        if active_identity.get("canonicalTaskId") and not self._voice_payload_matches_identity(payload, active_identity):
            launch = self._read_local_json_object(self._voice_launch_state_file())
            launch_matches = self._voice_identity_matches(launch, active_identity)
            launch_state = str(launch.get("launchState") or "") if launch_matches else ""
            state = "running" if launch_state in {
                "preparing_plan", "process_started", "waiting_for_bridge", "fish_listening", "generating"
            } else "failed" if launch_state == "failed" else "idle"
            text = str(launch.get("message") or "").strip() if launch_matches else ""
            if not text:
                text = f"{active_identity.get('audioBatch') or '当前批次'}配音等待启动"
            return {
                "state": state,
                "stateText": text,
                "total": expected_total,
                "completed": 0,
                "failed": 1 if state == "failed" else 0,
                "progress": 0,
                "phase": launch_state,
                "errorCode": str(launch.get("errorCode") or "") if launch_matches else "",
                "nodePid": self._integer(launch.get("nodePid"), 0) if launch_matches else 0,
                **active_identity,
                "updatedAt": str(launch.get("updatedAt") or "") if launch_matches else "",
            }
        raw = str(payload.get("status") or "idle")
        state = {
            "running": "running",
            "completed": "completed",
            "completed_with_errors": "failed",
            "failed": "failed",
        }.get(raw, "idle")
        total = self._integer(payload.get("total"))
        completed = self._integer(payload.get("completed"))
        failed = self._integer(payload.get("failed"))
        finished = completed + failed
        login_required = bool(payload.get("loginRequired"))
        bridge = payload.get("bridge") if isinstance(payload.get("bridge"), dict) else {}
        phase = str(bridge.get("phase") or "")
        phase_text = {
            "preparing": "正在准备 Fish 页面",
            "locating_primary_voice_card": "正在定位右侧主音色卡",
            "waiting_for_switch_button": "正在等待悬停切换按钮",
            "activating_primary_voice_name": "正在点击主音色名称以显示切换按钮",
            "opening_voice_catalog": "正在打开完整音色库",
            "selecting_saved_voice": "正在进入已收藏音色",
            "selecting_voice_collection": "正在打开音频1收藏夹",
            "selecting_target_voice": "正在选择目标音色",
            "verifying_voice": "正在验证当前主音色",
            "writing_text": "正在填写配音文案",
            "generating": "Fish 正在生成语音",
            "downloading": "正在下载生成音频",
            "failed": "Fish 配音失败，可安全继续",
        }.get(phase, "")
        return {
            "state": state,
            "stateText": (
                "Fish Audio 登录已失效，等待在家用 Chrome 登录"
                if login_required else phase_text or {
                "running": "正在生成语音",
                "completed": "语音生成完成",
                "failed": "语音生成有失败",
                "idle": "等待语音任务",
                }[state]
            ),
            "batchId": str(payload.get("batchId") or ""),
            "sourceBatchId": str(payload.get("sourceBatchId") or active_identity.get("batchId") or ""),
            "runGroupId": str(payload.get("runGroupId") or active_identity.get("runGroupId") or ""),
            "canonicalTaskId": str(payload.get("canonicalTaskId") or active_identity.get("canonicalTaskId") or ""),
            "productionDate": str(payload.get("productionDate") or active_identity.get("productionDate") or ""),
            "audioBatch": str(payload.get("audioBatch") or active_identity.get("audioBatch") or ""),
            "total": total,
            "completed": completed,
            "failed": failed,
            "progress": int(finished / max(total, 1) * 100),
            "lastHeartbeat": payload.get("lastHeartbeat"),
            "phase": phase,
            "extensionVersion": str(bridge.get("extensionVersion") or ""),
            "expectedVoice": str(bridge.get("expectedVoice") or ""),
            "voiceEvidence": bridge.get("voiceEvidence"),
            "voiceNavigation": bridge.get("voiceNavigation"),
            "loginRequired": login_required,
            "updatedAt": payload.get("completedAt") or payload.get("startedAt"),
        }

    def _stitch_ops_status(self):
        controller = self.stitcher_controller
        if controller is not None and controller.is_running():
            app = controller.app
            current = self._integer(app.progress["value"])
            total = self._integer(app.progress["maximum"], 1)
            return {
                "state": "running",
                "stateText": str(app.status_var.get() or "正在AI素材拼接"),
                "total": total,
                "completed": current,
                "failed": 0,
                "progress": int(current / max(total, 1) * 100),
                "project": str(getattr(app, "active_project_name", "") or ""),
            }
        if self.last_stitch_result:
            return dict(self.last_stitch_result)
        return {
            "state": "idle", "stateText": "等待拼接任务",
            "total": 0, "completed": 0, "failed": 0, "progress": 0,
        }

    def _upload_ops_status(self):
        state = load_baidu_json(BAIDU_STATE_FILE, {})
        config = load_baidu_config()
        tasks = list((state.get("tasks") or {}).values()) if isinstance(state, dict) else []
        completed = sum(1 for item in tasks if item.get("status") == "uploaded")
        failed = sum(1 for item in tasks if item.get("status") == "failed")
        uploading = [item for item in tasks if item.get("status") == "uploading"]
        total = len(tasks)
        if not config.get("enabled"):
            status, text = "unconfigured", "百度网盘未配置"
        elif not state.get("authorized"):
            status, text = "idle", "等待百度网盘授权"
        elif uploading:
            status, text = "running", "正在上传最终成片"
        elif failed:
            status, text = "failed", f"{failed} 条上传失败"
        elif total and completed == total:
            status, text = "completed", f"{completed} 条已上传"
        else:
            status, text = "idle", "等待CapCut最终成片"
        current = uploading[0] if uploading else next(
            (item for item in tasks if item.get("status") == "failed"), {}
        )
        return {
            "state": status,
            "stateText": text,
            "total": total,
            "completed": completed,
            "failed": failed,
            "progress": round(
                sum(self._integer(item.get("progress")) for item in tasks) / max(total, 1)
            ),
            "current": {
                "stage": "上传中" if uploading else ("上传失败" if current else ""),
                "name": Path(str(current.get("localPath") or "")).name,
                "progressText": f"{self._integer(current.get('progress'))}%" if current else "",
            },
            "updatedAt": str(state.get("updatedAt") or ""),
            "account": str(state.get("account") or ""),
            "chunkSizeMB": self._integer(state.get("chunkSizeMB")),
        }

    def _capcut_ops_status(self):
        """Expose the durable client phase instead of a guessed percentage."""
        task = self.active_external_task or load_current_context() or {}
        owner = canonical_task_id(task)
        date_value = str(task.get("productionDate") or task.get("date") or "")
        if not owner or not date_value:
            return {"state": "idle", "phase": "", "updatedAt": ""}
        payload = self.capcut_store.load_date(date_value)
        rows = [item for item in payload.get("tasks") or []
                if str(item.get("remotePipelineTaskId") or "") == owner]
        active = next((item for item in rows if str(item.get("status") or "") == "running"), None)
        if active is None:
            active = next((item for item in rows if str(item.get("status") or "") in {"failed", "stopped", "validation_failed", "manual_review"}), None)
        if active is None:
            active = next((item for item in rows if str(item.get("status") or "") == "waiting_client"), None)
        expected = contract_expected_count(task)
        completed = sum(1 for item in rows if str(item.get("status") or "") == "completed")
        unique_ids = {str(item.get("taskId") or "") for item in rows}
        all_completed = bool(expected and len(rows) == expected and len(unique_ids) == expected and "" not in unique_ids and completed == expected)
        summary_state = "completed" if all_completed else str((active or {}).get("status") or ("reconciliation_required" if rows else "idle"))
        if active is None and rows:
            active = rows[-1]
        return {
            "state": summary_state,
            "primaryErrorCode": str((active or {}).get("errorCode") or (active or {}).get("error_code") or ""),
            "primaryError": str((active or {}).get("error") or ""),
            "phase": str((active or {}).get("currentPhase") or ""),
            "phaseText": str((active or {}).get("phaseText") or ""),
            "attemptId": str((active or {}).get("activeAttemptId") or ""),
            "manifestId": str((active or {}).get("activeManifestId") or ""),
            "heartbeatAt": str((active or {}).get("heartbeatAt") or ""),
            "completed": completed,
            "failed": sum(1 for item in rows if str(item.get("status") or "") in {"failed", "stopped", "validation_failed"}),
            "expected": contract_expected_count(task),
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def publish_ops_status(self):
        try:
            payload = {
                "schemaVersion": 3,
                "machine": "WorkBench",
                "updatedAt": datetime.now().astimezone().isoformat(),
                "stages": {
                    "download": self._download_ops_status(),
                    "voice": self._voice_ops_status(),
                    "stitch": self._stitch_ops_status(),
                    "capcut": self._capcut_ops_status(),
                    "upload": self._upload_ops_status(),
                },
            }
            temporary = OPS_STATUS_FILE.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(OPS_STATUS_FILE)
        except (OSError, ValueError, tk.TclError):
            pass
        finally:
            try:
                if self.winfo_exists():
                    self.after(2000, self.publish_ops_status)
            except tk.TclError:
                pass

    def setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.CARD, borderwidth=1, relief="solid", bordercolor="#E5EAF2")
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background=self.CARD, foreground=self.TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=self.CARD, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Section.TLabel", background=self.CARD, foreground=self.TEXT, font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Primary.TButton", background=self.BLUE, foreground="white", padding=(14, 10), borderwidth=0, font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", self.BLUE_DARK)])
        style.configure("Secondary.TButton", background="white", foreground=self.TEXT, padding=(11, 8), bordercolor=self.BORDER)
        style.map("Secondary.TButton", background=[("active", "#EFF6FF")], foreground=[("active", self.BLUE)])
        style.configure("Purple.TButton", background=self.PURPLE, foreground="white", padding=(14, 10), borderwidth=0, font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Purple.TButton", background=[("active", "#6847E8")])
        style.configure("Orange.TButton", background=self.ORANGE, foreground="white", padding=(14, 10), borderwidth=0, font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Orange.TButton", background=[("active", "#B45309")])
        style.configure("Danger.TButton", background="#DC2626", foreground="white", padding=(11, 8), borderwidth=0, font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Danger.TButton", background=[("active", "#B91C1C")])
        style.configure("Small.TButton", background="white", foreground=self.TEXT, padding=(7, 5), bordercolor=self.BORDER, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview", rowheight=32, background="white", fieldbackground="white", bordercolor=self.BORDER, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", background="#F8FAFC", foreground=self.MUTED, font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TEntry", padding=7, fieldbackground="white", bordercolor="#D9E0EA")
        style.configure("TCombobox", padding=6, fieldbackground="white")

    def card(self, parent, title, subtitle=""):
        outer = ttk.Frame(parent, style="Card.TFrame", padding=12)
        ttk.Label(outer, text=title, style="Section.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(outer, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(3, 12))
        return outer

    def build_ui(self):
        header = ttk.Frame(self, padding=(22, 12))
        header.pack(fill="x")
        title = ttk.Frame(header)
        title.pack(side="left")
        icon = tk.Label(title, text="▶", bg=self.BLUE, fg="white", width=3, height=1, font=("Microsoft YaHei UI", 16, "bold"))
        icon.pack(side="left", padx=(0, 12), ipady=6)
        title_text = ttk.Frame(title)
        title_text.pack(side="left")
        ttk.Label(title_text, text="TikHub AI 视频工作台", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_text, text="下载 / AI 去字幕 / 批量生成语音 / 拼接输出 / CapCut 成片 / 百度网盘", foreground=self.MUTED).pack(anchor="w")
        batch = ttk.Frame(header)
        batch.pack(side="left", padx=(55, 0))
        ttk.Label(batch, text="当前批次", foreground=self.MUTED).pack(side="left")
        ttk.Entry(batch, textvariable=self.vars["batch_name"], width=25).pack(side="left", padx=(8, 20))
        ttk.Label(batch, text="项目位置", foreground=self.MUTED).pack(side="left")
        location = ttk.Entry(batch, textvariable=self.vars["tikhub_downloads"], width=38)
        location.pack(side="left", padx=(8, 5))
        ttk.Button(batch, text="▣", style="Small.TButton", command=lambda: self.choose_dir("tikhub_downloads")).pack(side="left")
        tools = ttk.Frame(header)
        tools.pack(side="right")
        ttk.Button(tools, text="保存模板", style="Secondary.TButton", command=self.save_template).pack(side="left", padx=4)
        ttk.Button(tools, text="加载模板", style="Secondary.TButton", command=self.load_template).pack(side="left", padx=4)
        ttk.Button(
            tools,
            text="安全继续任务",
            style="Secondary.TButton",
            command=self.continue_current_project,
        ).pack(side="left", padx=4)
        ttk.Checkbutton(
            tools,
            text="自动恢复",
            variable=self.auto_recovery_var,
            command=self.set_auto_recovery_enabled,
        ).pack(side="left", padx=4)
        ttk.Button(tools, text="新建批次", style="Primary.TButton", command=self.new_batch).pack(side="left", padx=4)
        ttk.Button(tools, text="⚙", style="Small.TButton", command=self.check_environment).pack(side="left", padx=(10, 4))
        ttk.Button(tools, text="?", style="Small.TButton", command=self.show_help).pack(side="left", padx=4)

        flow = ttk.Frame(self, padding=(22, 6, 22, 14))
        flow.pack(fill="x")
        self.flow_cards = []
        steps = (
            ("1+2", "下载与 AI 去字幕", "下载视频素材 → AI 自动处理字幕", self.BLUE, self.show_download_page),
            ("3", "批量生成语音", "维护文案任务 → 批量生成并下载音频", self.ORANGE, self.show_voice_page),
            ("4", "拼接配置与输出", "配置视频、音频与资源 → 生成成品", self.PURPLE, self.show_stitch_page),
            ("5", "CapCut 字幕成片", "选择当天任务 → 自动字幕与最终导出", self.GREEN, self.show_capcut_page),
            ("6", "百度网盘上传", "成片稳定校验 → 自动分片上传", "#0EA5A4", self.show_upload_page),
        )
        for i, (mark, name, desc, color, command) in enumerate(steps):
            if i:
                ttk.Label(flow, text="······→", foreground="#A8B3C5").pack(side="left", padx=5)
            box = tk.Frame(flow, bg="white", highlightbackground="#CFE0FF" if i < 2 else self.BORDER, highlightthickness=1, padx=16, pady=10)
            box.pack(side="left", fill="x", expand=True)
            badge = tk.Label(box, text=mark, bg=color, fg="white", font=("Microsoft YaHei UI", 10, "bold"), width=3, height=1)
            badge.pack(side="left", padx=(0, 10), ipady=5)
            texts = tk.Frame(box, bg="white"); texts.pack(side="left")
            tk.Label(texts, text=name, bg="white", fg=color, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
            tk.Label(texts, text=desc, bg="white", fg=self.MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            self.flow_cards.append((box, badge, texts, color))
            for widget in (box, badge, texts, *texts.winfo_children()):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _event, fn=command: fn())

        self.page_container = ttk.Frame(self)
        self.page_container.pack(fill="both", expand=True, padx=22)
        self.page_container.rowconfigure(0, weight=1)
        self.page_container.columnconfigure(0, weight=1)
        self.download_page = ttk.Frame(self.page_container)
        self.voice_page = ttk.Frame(self.page_container)
        self.stitch_page = ttk.Frame(self.page_container)
        self.capcut_page = ttk.Frame(self.page_container)
        self.upload_page = ttk.Frame(self.page_container)
        self.download_page.grid(row=0, column=0, sticky="nsew")
        self.voice_page.grid(row=0, column=0, sticky="nsew")
        self.stitch_page.grid(row=0, column=0, sticky="nsew")
        self.capcut_page.grid(row=0, column=0, sticky="nsew")
        self.upload_page.grid(row=0, column=0, sticky="nsew")

        body = ttk.Panedwindow(self.download_page, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=440)
        center = ttk.Frame(body, width=1000)
        body.add(left, weight=2); body.add(center, weight=5)
        self.build_download(left)
        self.build_tasks(center)
        self.build_voice_page()
        self.build_stitch_placeholder()
        self.build_capcut_page()
        self.build_upload_page()
        self.build_log()
        status = ttk.Frame(self, padding=(22, 3, 22, 8))
        self.status_frame = status
        status.pack(fill="x")
        ttk.Label(status, text="●", foreground=self.GREEN).pack(side="left")
        ttk.Label(status, textvariable=self.status_var, foreground=self.MUTED).pack(side="left", padx=5)
        ttk.Label(status, text="v0.5 · 已接入百度网盘自动上传", foreground=self.MUTED).pack(side="right")
        self.show_download_page()

    def build_download(self, parent):
        card = self.card(parent, "☁  1 下载与 AI 去字幕", "支持 TikTok / 抖音等链接，批量下载并自动去字幕")
        card.pack(fill="both", expand=True, padx=(0, 6))
        link_header = ttk.Frame(card, style="Card.TFrame")
        link_header.pack(fill="x")
        ttk.Label(link_header, text="视频链接（每行一个链接）", style="Card.TLabel").pack(side="left")
        ttk.Label(link_header, textvariable=self.link_count_var, style="Muted.TLabel").pack(side="right")
        self.links = tk.Text(card, height=3, relief="solid", bd=1, font=("Microsoft YaHei UI", 9), wrap="word")
        self.links.pack(fill="x", pady=(6, 8))
        self.links.bind("<<Modified>>", self.on_links_modified)
        self.links.edit_modified(False)
        buttons = ttk.Frame(card, style="Card.TFrame")
        buttons.pack(fill="x")
        ttk.Button(buttons, text="粘贴", style="Small.TButton", command=self.paste_links).pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="清空", style="Small.TButton", command=self.clear_links).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(buttons, text="提取链接", style="Small.TButton", command=self.show_link_count).pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="从文件导入", style="Small.TButton", command=self.import_links).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Separator(card).pack(fill="x", pady=10)
        ttk.Label(card, text="字幕处理模式", style="Card.TLabel").pack(anchor="w")
        modes = ttk.Frame(card, style="Card.TFrame"); modes.pack(fill="x", pady=7)
        self.mode_cards = {}
        data = (("AI 去除 STTN", "智能识别并去除\nSRT/TT 字幕"), ("AI 去除 LaMa", "LaMa 修复画面\n更自然"), ("高速模糊", "快速模糊字幕\n速度更快"), ("原智能模糊", "兼容原有模式\n稳定处理"))
        for index, (name, desc) in enumerate(data):
            frame = tk.Frame(modes, bg="white", highlightbackground=self.BORDER, highlightthickness=1, padx=8, pady=8, cursor="hand2")
            frame.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 3, 0), pady=(0, 4))
            modes.columnconfigure(index, weight=1)
            label = tk.Label(frame, text=name, bg="white", fg=self.TEXT, font=("Microsoft YaHei UI", 9, "bold")); label.pack(anchor="w")
            detail = tk.Label(frame, text=desc, bg="white", fg=self.MUTED, justify="left", font=("Microsoft YaHei UI", 8)); detail.pack(anchor="w", pady=(4, 0))
            for widget in (frame, label, detail): widget.bind("<Button-1>", lambda _e, n=name: self.select_mode(n))
            self.mode_cards[name] = (frame, label, detail)
        self.select_mode(self.mode.get())
        ttk.Checkbutton(card, text="下载完成后自动处理字幕", variable=self.auto_subtitle).pack(anchor="w", pady=4)
        ttk.Checkbutton(card, text="先生成 8 秒预览", variable=self.preview_first).pack(anchor="w", pady=4)
        concurrency = ttk.Frame(card, style="Card.TFrame")
        concurrency.pack(fill="x", pady=(4, 0))
        ttk.Label(concurrency, text="并发下载数量", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(concurrency, from_=1, to=8, width=5, textvariable=self.vars["download_workers"]).pack(side="left", padx=8)
        ttk.Label(concurrency, text="AI 去字幕仍按顺序逐个处理", style="Muted.TLabel").pack(side="left")
        ttk.Separator(card).pack(fill="x", pady=10)
        ttk.Label(card, text="TikHub 下载目录", style="Card.TLabel").pack(anchor="w")
        self.path_row(card, "tikhub_downloads")
        self.download_button = ttk.Button(card, text="开始下载与去字幕", style="Primary.TButton", command=self.start_background_download)
        self.download_button.pack(fill="x", pady=(14, 6))
        secondary = ttk.Frame(card, style="Card.TFrame"); secondary.pack(fill="x")
        ttk.Button(secondary, text="打开原 TikHub", style="Secondary.TButton", command=self.launch_tikhub).pack(side="left", fill="x", expand=True)
        ttk.Button(secondary, text="扫描去字幕成品", style="Secondary.TButton", command=self.scan_subjects).pack(side="left", fill="x", expand=True, padx=(5, 0))
        ttk.Separator(card).pack(fill="x", pady=10)
        inbox_header = ttk.Frame(card, style="Card.TFrame")
        inbox_header.pack(fill="x")
        ttk.Label(inbox_header, text="第1步外部任务投递", style="Card.TLabel").pack(side="left")
        ttk.Label(inbox_header, textvariable=self.inbox_info_var, style="Muted.TLabel").pack(side="right")
        inbox_row = ttk.Frame(card, style="Card.TFrame")
        inbox_row.pack(fill="x", pady=(6, 2))
        self.inbox_task_box = ttk.Combobox(inbox_row, state="readonly", textvariable=self.inbox_task_var)
        self.inbox_task_box.pack(side="left", fill="x", expand=True)
        ttk.Button(inbox_row, text="扫描外部任务", style="Small.TButton", command=lambda: self.refresh_task_inbox(True)).pack(side="left", padx=(5, 0))
        ttk.Button(inbox_row, text="导入到第1步", style="Small.TButton", command=self.import_external_task).pack(side="left", padx=(5, 0))
        ttk.Label(card, text="导入只填充第1步链接和下载设置，不会自动开始下载。", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

    def build_tasks(self, parent):
        card = self.card(parent, "▣  2 任务与进度", "实时查看任务状态与处理进度")
        card.pack(fill="both", expand=True, padx=6)
        stats = ttk.Frame(card, style="Card.TFrame"); stats.pack(fill="x", pady=(0, 12))
        for i, (label, var, color) in enumerate((("总任务数", self.total_var, self.TEXT), ("成功", self.success_var, self.GREEN), ("失败", self.failed_var, self.RED), ("总进度", self.progress_var, self.BLUE))):
            box = tk.Frame(stats, bg="white", highlightbackground=self.BORDER, highlightthickness=1, padx=12, pady=8)
            box.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 5, 0)); stats.columnconfigure(i, weight=1)
            tk.Label(box, text=label, bg="white", fg=self.MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w")
            tk.Label(box, textvariable=var, bg="white", fg=color, font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", pady=(3, 0))
        tabs = ttk.Frame(card, style="Card.TFrame")
        tabs.pack(fill="x", pady=(0, 8))
        self.live_tab_button = ttk.Button(tabs, text="当前处理任务", style="Primary.TButton", command=lambda: self.show_task_view("live"))
        self.live_tab_button.pack(side="left")
        self.products_tab_button = ttk.Button(tabs, text="已完成成品", style="Secondary.TButton", command=lambda: self.show_task_view("products"))
        self.products_tab_button.pack(side="left", padx=5)
        ttk.Label(tabs, textvariable=self.product_info_var, style="Muted.TLabel").pack(side="right")

        self.task_view_container = ttk.Frame(card, style="Card.TFrame")
        self.task_view_container.pack(fill="both", expand=True)
        self.task_view_container.rowconfigure(0, weight=1)
        self.task_view_container.columnconfigure(0, weight=1)
        self.live_task_frame = ttk.Frame(self.task_view_container, style="Card.TFrame")
        self.product_task_frame = ttk.Frame(self.task_view_container, style="Card.TFrame")
        self.live_task_frame.grid(row=0, column=0, sticky="nsew")
        self.product_task_frame.grid(row=0, column=0, sticky="nsew")

        cols = ("name", "download", "subtitle", "stage", "progress", "action", "path")
        self.tree = ttk.Treeview(self.live_task_frame, columns=cols, show="headings", selectmode="extended")
        for col, text, width in (("name", "视频名称", 220), ("download", "下载状态", 92), ("subtitle", "字幕处理状态", 105), ("stage", "当前阶段", 115), ("progress", "进度", 70), ("action", "操作", 55), ("path", "", 0)):
            self.tree.heading(col, text=text); self.tree.column(col, width=width, anchor="w")
        self.tree.column("path", width=0, stretch=False)
        self.tree.pack(fill="both", expand=True)

        category_bar = ttk.Frame(self.product_task_frame, style="Card.TFrame")
        category_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(category_bar, text="材料分类", style="Card.TLabel").pack(side="left")
        self.product_category_combo = ttk.Combobox(category_bar, textvariable=self.product_category_var, state="readonly", width=28)
        self.product_category_combo.pack(side="left", padx=8)
        self.product_category_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_product_tree())
        ttk.Label(category_bar, text="选择分类后，只会将该文件夹用于拼接", style="Muted.TLabel").pack(side="left")

        product_cols = ("category", "name", "status", "subtitle", "stage", "modified", "path")
        self.product_tree = ttk.Treeview(self.product_task_frame, columns=product_cols, show="headings", selectmode="extended")
        for col, text, width in (("category", "分类", 120), ("name", "成品名称", 270), ("status", "文件状态", 85), ("subtitle", "字幕处理状态", 100), ("stage", "用途", 105), ("modified", "修改时间", 130), ("path", "", 0)):
            self.product_tree.heading(col, text=text); self.product_tree.column(col, width=width, anchor="w")
        self.product_tree.column("path", width=0, stretch=False)
        self.product_tree.pack(fill="both", expand=True)

        actions = ttk.Frame(card, style="Card.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="打开主体目录", style="Secondary.TButton", command=lambda: self.open_folder(self.vars["subject_dir"].get())).pack(side="left")
        ttk.Button(actions, text="扫描成品目录", style="Secondary.TButton", command=self.scan_subjects).pack(side="left", padx=5)
        ttk.Button(actions, text="批次核对 / 缺口提醒", style="Orange.TButton", command=self.show_batch_completeness).pack(side="left", padx=5)
        ttk.Button(actions, text="使用成品目录进入拼接", style="Primary.TButton", command=self.handoff).pack(side="right")
        self.show_task_view("live")

    def show_task_view(self, view):
        self.current_task_view = view
        if view == "products":
            self.product_task_frame.tkraise()
            self.live_tab_button.configure(style="Secondary.TButton")
            self.products_tab_button.configure(style="Primary.TButton")
        else:
            self.live_task_frame.tkraise()
            self.live_tab_button.configure(style="Primary.TButton")
            self.products_tab_button.configure(style="Secondary.TButton")

    def build_voice_page(self):
        page = ttk.Frame(self.voice_page, padding=(0, 8, 0, 8))
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=3)
        page.columnconfigure(1, weight=2)
        page.rowconfigure(0, weight=1)

        main = self.card(
            page,
            "♫  3 批量生成语音",
            "便携客户端已完整并入工作台，包含 Node.js、Playwright、Chromium、任务表和桌面程序。",
        )
        main.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        intro = (
            "工作方式\n\n"
            "1. 在任务管理中按项目维护多语言文案、声音和输出文件名。\n"
            "2. 在控制台设置目标语音网站和浏览器运行方式。\n"
            "3. 点击开始生成，由 Playwright 逐条操作网页并下载音频。\n"
            "4. 生成的音频目录会自动传给步骤 4 的视频拼接工具。"
        )
        ttk.Label(main, text=intro, style="Card.TLabel", justify="left", wraplength=720).pack(anchor="w", pady=(4, 18))

        warning = tk.Frame(main, bg="#FFF7ED", highlightbackground="#FED7AA", highlightthickness=1, padx=14, pady=12)
        warning.pack(fill="x", pady=(0, 18))
        tk.Label(warning, text="接入状态", bg="#FFF7ED", fg=self.ORANGE, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(
            warning,
            text="客户端框架可运行；目标语音网站的按钮和输入框尚未绑定，需在 siteAdapter.js 中完成网页适配后才能自动生成。",
            bg="#FFF7ED", fg="#9A3412", justify="left", wraplength=760, font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(5, 0))

        batch_context = tk.Frame(main, bg="#EFF6FF", highlightbackground="#BFDBFE", highlightthickness=1, padx=14, pady=10)
        batch_context.pack(fill="x", pady=(0, 16))
        tk.Label(batch_context, text="当前任务音频规则", bg="#EFF6FF", fg=self.BLUE, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(batch_context, textvariable=self.voice_batch_context_var, bg="#EFF6FF", fg="#1E3A5F", justify="left", wraplength=760, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 0))

        ttk.Label(main, text="语音输出目录（以步骤 4 的项目、材料和日期为准）", style="Card.TLabel").pack(anchor="w")
        output_row = ttk.Frame(main, style="Card.TFrame")
        output_row.pack(fill="x", pady=(6, 14))
        ttk.Entry(output_row, textvariable=self.vars["audio_dir"], state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="打开目录", style="Secondary.TButton", command=self.open_voice_downloads).pack(side="left", padx=(6, 0))

        ttk.Button(main, text="启动批量语音客户端", style="Orange.TButton", command=self.launch_voice_client).pack(fill="x", pady=(4, 8))
        actions = ttk.Frame(main, style="Card.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="打开任务表", style="Secondary.TButton", command=self.open_voice_tasks).pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="查看使用说明", style="Secondary.TButton", command=self.open_voice_docs).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(actions, text="刷新状态", style="Secondary.TButton", command=self.refresh_voice_info).pack(side="left", fill="x", expand=True)

        side = self.card(page, "运行信息", "检查便携运行环境和任务数据")
        side.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.voice_info_var = tk.StringVar(value="正在检查…")
        ttk.Label(side, textvariable=self.voice_info_var, style="Card.TLabel", justify="left", wraplength=460).pack(anchor="w", fill="x")
        ttk.Separator(side).pack(fill="x", pady=18)
        ttk.Label(side, text="并入位置", style="Card.TLabel").pack(anchor="w")
        ttk.Label(side, text=str(VOICE_CLIENT_DIR), style="Muted.TLabel", justify="left", wraplength=460).pack(anchor="w", pady=(5, 14))
        ttk.Label(side, text="任务字段", style="Card.TLabel").pack(anchor="w")
        ttk.Label(
            side,
            text="项目、语言、声音、文案、输出文件名、状态",
            style="Muted.TLabel", justify="left", wraplength=460,
        ).pack(anchor="w", pady=(5, 0))
        self.after(200, self.refresh_voice_info)

    def build_stitch_placeholder(self):
        self.stitch_loading = ttk.Frame(self.stitch_page, style="Card.TFrame", padding=24)
        self.stitch_loading.pack(fill="both", expand=True)
        ttk.Label(self.stitch_loading, text="AI 素材拼接完整工作区", style="Title.TLabel").pack(pady=(80, 10))
        ttk.Label(
            self.stitch_loading,
            text="首次进入时会加载原 AI 素材拼接工具的完整项目、语言任务、生成参数和首帧封面界面。",
            foreground=self.MUTED,
        ).pack()
        self.stitch_loading_label = ttk.Label(self.stitch_loading, text="点击顶部步骤 4 进入", foreground=self.BLUE)
        self.stitch_loading_label.pack(pady=18)
        ttk.Button(self.stitch_loading, text="进入拼接配置", style="Purple.TButton", command=self.show_stitch_page).pack()

    def build_capcut_page(self):
        page = ttk.Frame(self.capcut_page, padding=(0, 8, 0, 8))
        page.pack(fill="both", expand=True)
        filters = self.card(page, "✓  5 CapCut 自动字幕与最终成片", "第四步成功任务会按日期汇总；项目、材料、语言、视频和音频均来自精确交接记录。")
        filters.pack(fill="x")

        first = ttk.Frame(filters, style="Card.TFrame")
        first.pack(fill="x")
        ttk.Label(first, text="处理日期", style="Card.TLabel").pack(side="left")
        self.capcut_date_combo = ttk.Combobox(first, textvariable=self.capcut_date_var, state="readonly", width=12)
        self.capcut_date_combo.pack(side="left", padx=(8, 18))
        self.capcut_date_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_capcut_tasks(False))
        ttk.Label(first, text="项目", style="Card.TLabel").pack(side="left", padx=(0, 5))
        for name in CATEGORIES:
            ttk.Checkbutton(
                first,
                text=name,
                variable=self.capcut_project_vars[name],
                command=lambda: self.refresh_capcut_tasks(False),
            ).pack(side="left", padx=3)
        ttk.Label(first, text="状态", style="Card.TLabel").pack(side="left", padx=(18, 5))
        for key, label in (("pending", "等待处理"), ("failed", "上次失败"), ("completed", "已完成")):
            ttk.Checkbutton(
                first,
                text=label,
                variable=self.capcut_status_vars[key],
                command=lambda: self.refresh_capcut_tasks(False),
            ).pack(side="left", padx=3)
        ttk.Button(first, text="刷新并恢复记录", style="Secondary.TButton", command=self.refresh_capcut_tasks).pack(side="right")

        output_row = ttk.Frame(filters, style="Card.TFrame")
        output_row.pack(fill="x", pady=(10, 0))
        ttk.Label(output_row, text="最终成片根目录", style="Card.TLabel").pack(side="left")
        ttk.Entry(output_row, textvariable=self.vars["capcut_output_root"], state="readonly").pack(
            side="left", fill="x", expand=True, padx=(8, 6)
        )
        ttk.Button(
            output_row,
            text="选择导出目录",
            style="Secondary.TButton",
            command=self.choose_capcut_output_root,
        ).pack(side="left")
        ttk.Label(
            output_row,
            text="自动生成：根目录\\日期\\ProjectA、ProjectB、ProjectC、ProjectD或 SPC",
            style="Muted.TLabel",
        ).pack(side="left", padx=(10, 0))

        ttk.Label(filters, textvariable=self.capcut_summary_var, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

        table_card = self.card(page, "当天第五步任务", "点击最左侧勾选；红色异常任务不会交给 CapCut。")
        table_card.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("selected", "project", "material", "language", "video", "audio", "output", "status", "task_id")
        self.capcut_tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="browse")
        headings = {
            "selected": "选择",
            "project": "项目",
            "material": "材料",
            "language": "语言",
            "video": "第四步视频",
            "audio": "对应音频",
            "output": "最终输出",
            "status": "状态",
        }
        widths = {
            "selected": 55,
            "project": 85,
            "material": 100,
            "language": 95,
            "video": 210,
            "audio": 210,
            "output": 250,
            "status": 105,
        }
        for column, title in headings.items():
            self.capcut_tree.heading(column, text=title)
            self.capcut_tree.column(column, width=widths[column], anchor="w")
        self.capcut_tree.column("task_id", width=0, stretch=False)
        self.capcut_tree.tag_configure("error", foreground=self.RED)
        self.capcut_tree.tag_configure("completed", foreground=self.GREEN)
        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=self.capcut_tree.yview)
        self.capcut_tree.configure(yscrollcommand=scrollbar.set)
        self.capcut_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.capcut_tree.bind("<Button-1>", self.on_capcut_tree_click)

        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="全选当前显示", style="Secondary.TButton", command=lambda: self.select_capcut_visible(True)).pack(side="left")
        ttk.Button(actions, text="取消全选", style="Secondary.TButton", command=lambda: self.select_capcut_visible(False)).pack(side="left", padx=5)
        ttk.Button(actions, text="编辑选中任务", style="Secondary.TButton", command=self.edit_capcut_task).pack(side="left")
        ttk.Button(actions, text="查看视频", style="Secondary.TButton", command=lambda: self.open_capcut_item("videoPath")).pack(side="left", padx=(14, 5))
        ttk.Button(actions, text="播放音频", style="Secondary.TButton", command=lambda: self.open_capcut_item("audioPath")).pack(side="left")
        ttk.Button(actions, text="打开成片目录", style="Secondary.TButton", command=self.open_capcut_output).pack(side="right", padx=(5, 0))
        ttk.Button(actions, text="确认并启动 CapCut", style="Primary.TButton", command=self.start_capcut_handoff).pack(side="right")
        ttk.Button(actions, text="生成第五步任务", style="Secondary.TButton", command=self.prepare_capcut_handoff).pack(side="right", padx=(5, 5))

    def build_upload_page(self):
        page = ttk.Frame(self.upload_page, padding=(0, 8, 0, 8))
        page.pack(fill="both", expand=True)
        header = self.card(
            page,
            "☁  6 百度网盘最终成片上传",
            "只读取第五步已完成的最终 MP4；文件稳定且容器有效后才上传，失败只重试上传。",
        )
        header.pack(fill="x")
        stats = ttk.Frame(header, style="Card.TFrame")
        stats.pack(fill="x", pady=(0, 12))
        for index, (label, var, color) in enumerate(
            (
                ("百度账号", self.upload_account_var, self.BLUE),
                ("待处理", self.upload_queue_var, self.ORANGE),
                ("上传完成", self.upload_completed_var, self.GREEN),
                ("上传失败", self.upload_failed_var, self.RED),
            )
        ):
            box = tk.Frame(
                stats, bg="white", highlightbackground=self.BORDER,
                highlightthickness=1, padx=12, pady=8,
            )
            box.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0))
            stats.columnconfigure(index, weight=1)
            tk.Label(
                box, text=label, bg="white", fg=self.MUTED,
                font=("Microsoft YaHei UI", 8),
            ).pack(anchor="w")
            tk.Label(
                box, textvariable=var, bg="white", fg=color,
                font=("Microsoft YaHei UI", 14, "bold"),
            ).pack(anchor="w", pady=(3, 0))
        ttk.Label(header, textvariable=self.upload_status_var, style="Card.TLabel").pack(anchor="w")
        ttk.Label(header, textvariable=self.upload_detail_var, style="Muted.TLabel").pack(anchor="w", pady=(4, 10))
        action_row = ttk.Frame(header, style="Card.TFrame")
        action_row.pack(fill="x")
        ttk.Button(
            action_row, text="配置并授权百度网盘", style="Primary.TButton",
            command=self.configure_baidu_upload,
        ).pack(side="left")
        ttk.Button(
            action_row, text="启动上传服务", style="Secondary.TButton",
            command=lambda: self.ensure_upload_service(show_message=True),
        ).pack(side="left", padx=6)
        ttk.Button(
            action_row, text="打开上传状态目录", style="Secondary.TButton",
            command=self.open_upload_state_dir,
        ).pack(side="left")
        ttk.Button(
            action_row, text="确认已在公司下载（清理所选本地成片）", style="Danger.TButton",
            command=self.confirm_selected_uploaded_download,
        ).pack(side="left", padx=6)
        ttk.Label(
            action_row,
            text="网盘目录：我的应用数据 / AI视频生产线 / 日期 / ProjectA、ProjectB、ProjectC、ProjectD、SPC",
            style="Muted.TLabel",
        ).pack(side="right")

        table_card = self.card(page, "上传任务", "本地成片永久保留；上传完成后记录云端文件ID和大小。")
        table_card.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("date", "project", "file", "remote", "progress", "status", "error")
        self.upload_tree = ttk.Treeview(table_card, columns=columns, show="headings")
        for column, title, width in (
            ("date", "日期", 75),
            ("project", "项目", 90),
            ("file", "本地最终成片", 280),
            ("remote", "百度网盘目录", 330),
            ("progress", "进度", 70),
            ("status", "状态", 100),
            ("error", "错误", 300),
        ):
            self.upload_tree.heading(column, text=title)
            self.upload_tree.column(column, width=width, anchor="w")
        self.upload_tree.tag_configure("failed", foreground=self.RED)
        self.upload_tree.tag_configure("uploaded", foreground=self.GREEN)
        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=self.upload_tree.yview)
        self.upload_tree.configure(yscrollcommand=scrollbar.set)
        self.upload_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def show_download_page(self):
        self.current_page = "download"
        self.download_page.tkraise()
        if not self.log_frame.winfo_manager():
            self.log_frame.pack(fill="x", padx=22, pady=(12, 0), before=self.status_frame)
        self._set_flow_active(0)

    def show_voice_page(self):
        self.current_page = "voice"
        self.voice_page.tkraise()
        self.log_frame.pack_forget()
        self._set_flow_active(1)
        self.refresh_voice_info()

    def _task_checkpoint_ids(self, task):
        """Return every checkpoint that contributes media to one canonical task."""
        values = [
            canonical_task_id(task),
            task.get("taskId") if isinstance(task, dict) else "",
            task.get("replenishmentClosedByTaskId") if isinstance(task, dict) else "",
        ]
        parent_id = canonical_task_id(task)
        if parent_id:
            expected_identity = {
                "productionDate": str(task.get("productionDate") or task.get("date") or "").strip(),
                "project": str(task.get("project") or "").strip(),
                "material": str(task.get("material") or "").strip(),
            }
            try:
                records = sorted(TASK_IMPORTED_DIR.glob("*.json"), key=lambda path: path.name.casefold())
            except OSError:
                records = []
            for record_path in records:
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, UnicodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if str(record.get("replenishForTaskId") or "").strip() != parent_id:
                    continue
                if str(record.get("status") or "").strip().lower() in {"cancelled", "canceled"}:
                    continue
                record_canonical = str(record.get("canonicalTaskId") or parent_id).strip()
                if record_canonical != parent_id:
                    continue
                record_identity = {
                    "productionDate": str(record.get("productionDate") or record.get("date") or "").strip(),
                    "project": str(record.get("project") or "").strip(),
                    "material": str(record.get("material") or "").strip(),
                }
                if any(
                    expected_identity[key]
                    and record_identity[key]
                    and expected_identity[key] != record_identity[key]
                    for key in expected_identity
                ):
                    continue
                child_id = str(record.get("taskId") or record_path.stem).strip()
                if child_id:
                    values.append(child_id)
        return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))

    @staticmethod
    def _verified_subtitle_passthrough(item, output, checkpoint_id):
        """Accept a non-STTN output only when its task-scoped OCR proof is intact."""
        verification = item.get("verification")
        if not isinstance(verification, dict):
            return False, "下载记录缺少去字幕复检结果"
        verification_status = str(verification.get("verificationStatus") or "").strip().lower()
        if verification.get("verified") is not True or verification_status not in {"verified_clean", "warning"}:
            return False, f"去字幕复检未放行（{verification_status or 'unknown'}）"
        if verification.get("residualBands"):
            return False, "去字幕复检仍包含完整稳定字幕带"

        sidecar_value = str(item.get("subtitleVerificationPath") or "").strip()
        sidecar_path = Path(sidecar_value) if sidecar_value else Path(f"{output}.subtitle-verification.json")
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError) as exc:
            return False, f"无法读取任务绑定的去字幕复检证据：{exc}"

        try:
            sidecar_output = Path(str(sidecar.get("outputPath") or "").strip()).resolve()
        except (OSError, RuntimeError, ValueError):
            return False, "去字幕复检证据缺少有效输出路径"
        if os.path.normcase(str(sidecar_output)) != os.path.normcase(str(output.resolve())):
            return False, "去字幕复检证据与当前视频路径不一致"

        sidecar_task_id = str(sidecar.get("taskId") or "").strip()
        if sidecar_task_id != str(checkpoint_id or "").strip():
            return False, "去字幕复检证据与当前任务身份不一致"
        item_url = str(item.get("url") or "").strip()
        sidecar_url = str(sidecar.get("url") or "").strip()
        if item_url and sidecar_url != item_url:
            return False, "去字幕复检证据与当前视频链接不一致"

        sidecar_verification = sidecar.get("verification")
        if not isinstance(sidecar_verification, dict):
            return False, "去字幕复检证据缺少验证详情"
        sidecar_status = str(sidecar_verification.get("verificationStatus") or "").strip().lower()
        if sidecar_verification.get("verified") is not True or sidecar_status != verification_status:
            return False, "下载记录与去字幕复检证据的结论不一致"
        if sidecar_verification.get("residualBands"):
            return False, "去字幕复检证据仍包含完整稳定字幕带"

        treatment = str(sidecar.get("subtitleTreatment") or item.get("subtitleOutcome") or "").strip().lower()
        if treatment not in {"verified_no_subtitle", "minor_residual"}:
            return False, f"未经允许的非STTN处理结果：{treatment or 'unknown'}"
        return True, ""

    def _task_stitch_source_manifest(self, task):
        """Build the only source list that a remote Step 4 may consume."""
        entries, failures, seen = [], [], {}
        duplicate_sources = []
        missing_checkpoints = []
        for checkpoint_id in self._task_checkpoint_ids(task):
            path = DOWNLOAD_CHECKPOINT_DIR / f"{checkpoint_id}.json"
            try:
                checkpoint = json.loads(path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError as exc:
                # A full Dashboard replenishment can legitimately become the
                # only physical Step 1/2 checkpoint. Defer the missing parent
                # error until all merged contributors have been counted.
                missing_checkpoints.append((checkpoint_id, exc))
                continue
            except (OSError, ValueError, UnicodeError) as exc:
                failures.append(f"无法读取下载清单 {checkpoint_id}: {exc}")
                continue
            for item in (checkpoint.get("items") or {}).values():
                if not isinstance(item, dict) or str(item.get("status") or "") != "completed":
                    continue
                output = Path(str(item.get("outputPath") or "").strip())
                if not output.is_file() or output.stat().st_size < 1024:
                    failures.append(f"下载记录的视频不存在或无效：{output}")
                    continue
                outcome = str(item.get("subtitleOutcome") or "").strip()
                legacy_processed = output.stem.lower().endswith(
                    tuple(suffix.lower() for suffix in PROCESSED_SUFFIXES)
                )
                verified_passthrough = False
                verification_failure = ""
                if outcome not in {"processed", "verified_no_subtitle"} and not legacy_processed:
                    verified_passthrough, verification_failure = self._verified_subtitle_passthrough(
                        item, output, checkpoint_id
                    )
                if (
                    outcome not in {"processed", "verified_no_subtitle"}
                    and not legacy_processed
                    and not verified_passthrough
                ):
                    failures.append(
                        "视频缺少可验证的 AI 去字幕结果，不能把原文件当作第4步素材："
                        f"{output.name}（{verification_failure or '未通过任务绑定复检'}）"
                    )
                    continue
                resolved = str(output.resolve())
                key = os.path.normcase(resolved)
                if key in seen:
                    duplicate_sources.append({
                        "path": resolved,
                        "firstCheckpointId": seen[key]["checkpointId"],
                        "duplicateCheckpointId": checkpoint_id,
                        "url": str(item.get("url") or ""),
                    })
                    continue
                entry = {
                    "path": resolved,
                    "url": str(item.get("url") or ""),
                    "outcome": outcome or "processed",
                    "subtitleVerificationStatus": str(
                        (item.get("verification") or {}).get("verificationStatus") or ""
                    ).strip(),
                    "checkpointId": checkpoint_id,
                }
                seen[key] = entry
                entries.append(entry)
        entries.sort(key=lambda item: Path(item["path"]).name.casefold())
        expected = contract_expected_count(task)
        manifest_complete = bool(expected and len(entries) == expected)
        if missing_checkpoints and not manifest_complete:
            failures.extend(
                f"无法读取下载清单 {checkpoint_id}: {exc}"
                for checkpoint_id, exc in missing_checkpoints
            )
        if expected and len(entries) != expected:
            failures.append(f"任务素材清单为 {len(entries)}/{expected}，不能安全进入第4步。")
        if duplicate_sources:
            sample = duplicate_sources[0]
            failures.append(
                "任务补链含重复视频："
                f"{Path(sample['path']).name} 同时出现在 "
                f"{sample['firstCheckpointId']} 与 {sample['duplicateCheckpointId']}；"
                f"当前只有 {len(entries)}/{expected} 个不同视频，请补充 1 条新的链接。"
            )
        parents = {str(Path(item["path"]).parent.resolve()) for item in entries}
        if len(parents) > 1:
            failures.append("任务素材分散在多个目录；为避免混用，已阻止第4步。")
        return entries, failures, (Path(next(iter(parents))) if len(parents) == 1 else None)

    def show_stitch_page(self, focus_actions=False, target_project=None):
        self.current_page = "stitch"
        self.stitch_page.tkraise()
        self.log_frame.pack_forget()
        self._set_flow_active(2)
        controller = self._ensure_stitcher_embedded()
        if controller is None:
            return
        try:
            active_task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
            remote_sources = None
            if active_task and self._is_remote_auto_sequence_task(active_task):
                subject, count = None, 0
                remote_sources, source_failures, subject = self._task_stitch_source_manifest(active_task)
                if source_failures:
                    raise ValueError("\n".join(source_failures))
            else:
                subject, count = self._stage_selected_subjects(required=False)
            # Validate the task manifest before loading a saved GUI project.
            # Otherwise a stale project can display an unrelated AI/old-media
            # warning (for example 石纹/02 老素材) even though the real
            # problem is an incomplete replenishment manifest.
            if target_project:
                controller.select_project(target_project)
            if subject is not None:
                controller.apply_handoff(subject)
                self.vars["subject_dir"].set(str(subject))
                self.write_log(f"拼接页面已直接使用下载目录，当前包含 {count} 个去字幕成品；未创建交接文件夹。")
            if active_task and self._is_remote_auto_sequence_task(active_task):
                audio_dir = str(self.vars["audio_dir"].get() or "").strip()
                if audio_dir:
                    controller.apply_audio_handoff(
                        Path(audio_dir),
                        str(active_task.get("productionDate") or active_task.get("date") or ""),
                    )
                    self.write_log(f"第 4 步已绑定当前任务音频目录：{audio_dir}")
            controller.refresh_audio()
            if active_task and self._is_remote_auto_sequence_task(active_task):
                controller.apply_task_contract(active_task)
                sources = remote_sources or []
                controller.apply_handoff(subject, [item["path"] for item in sources])
                self.vars["subject_dir"].set(str(subject))
                self.write_log(
                    f"第4步已绑定任务素材清单：{len(sources)}/{contract_expected_count(active_task)} 条；"
                    "只会使用本任务已验证的文件，不扫描其他项目目录。"
                )
                self.write_log(
                    f"第 4 步已应用材料任务合同：计划 {contract_expected_count(active_task)} 条，"
                    "不会使用项目总数覆盖。"
                )
        except Exception as exc:
            # A remote automatic run must never block behind a desktop dialog.
            # Surface a structured recoverable state to the control plane instead.
            if active_task and self._is_remote_auto_sequence_task(active_task):
                error_code = str(getattr(exc, "error_code", "STITCH_SETUP_FAILED") or "STITCH_SETUP_FAILED")
                missing_languages = list(getattr(exc, "missing_languages", []) or [])
                message = f"第4步拼接配置未通过：{exc}；已停止，修复后可安全继续。"
                self._set_remote_pipeline_status(
                    canonical_task_id(active_task),
                    "blocked_stitch",
                    message,
                    error_code=error_code,
                    missing_languages=missing_languages,
                )
                self.status_var.set(message)
                self.write_log(f"第4步拼接合同验证失败 [{error_code}]：{exc}")
                return
            self.write_log(f"加载成品目录失败：{exc}")
            messagebox.showerror("加载成品目录失败", str(exc))
        if focus_actions:
            self.after(150, controller.focus_generation_actions)

    def show_capcut_page(self):
        self.current_page = "capcut"
        self.capcut_page.tkraise()
        self.log_frame.pack_forget()
        self._set_flow_active(3)
        self.refresh_capcut_tasks()

    def show_upload_page(self):
        self.current_page = "upload"
        self.upload_page.tkraise()
        self.log_frame.pack_forget()
        self._set_flow_active(4)
        self.refresh_upload_page()

    def _set_flow_active(self, active_index):
        for index, (box, badge, texts, color) in enumerate(self.flow_cards):
            active = index == active_index
            active_backgrounds = ("#EDF5FF", "#FFF7ED", "#F5F3FF", "#ECFDF5", "#ECFEFF")
            background = active_backgrounds[index] if active and index < len(active_backgrounds) else "white"
            box.configure(bg=background, highlightbackground=color if active else self.BORDER, highlightthickness=2 if active else 1)
            for child in texts.winfo_children():
                child.configure(bg=background)
            texts.configure(bg=background)

    def _stitcher_source_path(self):
        launcher = Path(self.vars["stitcher_launcher"].get().strip())
        candidates = (
            launcher.parent / "board_video_stitcher.py",
            launcher if launcher.suffix.lower() in {".py", ".pyw"} else Path(),
        )
        return next((path for path in candidates if path.is_file()), candidates[0])

    def _ensure_stitcher_embedded(self):
        if self.stitcher_controller is not None:
            return self.stitcher_controller
        if self.stitcher_load_error:
            return None
        self.stitch_loading_label.configure(text="正在加载完整 AI 素材拼接页面…")
        self.update_idletasks()
        try:
            controller = EmbeddedStitcherController(self.stitch_page, self._stitcher_source_path())
            controller.set_records_callback(self.on_stitch_records)
            self.stitcher_controller = controller
            self.stitch_loading.pack_forget()
            self.write_log("完整 AI 素材拼接页面已加载。")
            return controller
        except Exception as exc:
            self.stitcher_load_error = str(exc)
            self.stitch_loading_label.configure(text=f"加载失败：{exc}", foreground=self.RED)
            ttk.Button(
                self.stitch_loading,
                text="打开原 AI 拼接工具",
                style="Secondary.TButton",
                command=self.launch_stitcher,
            ).pack(pady=8)
            self.write_log(f"完整拼接页面加载失败：{exc}")
            return None

    def _stage_selected_subjects(self, required=True):
        category = self.product_category_var.get().strip()
        subject = self.product_category_paths.get(category)
        if subject is None or not subject.is_dir():
            if required:
                raise ValueError("请先扫描成品目录并选择一个材料分类。")
            return None, 0
        products = self.product_catalog.get(category, [])
        if required and not products:
            raise ValueError(f"分类“{category}”中没有去字幕成品。")
        return subject, len(products)

    def build_log(self):
        frame = self.card(self, "实时日志与操作")
        self.log_frame = frame
        frame.configure(padding=10)
        frame.pack(fill="x", padx=22, pady=(12, 0))
        content = ttk.Frame(frame, style="Card.TFrame"); content.pack(fill="x")
        log_wrap = ttk.Frame(content, style="Card.TFrame"); log_wrap.pack(side="left", fill="both", expand=True)
        self.log = tk.Text(log_wrap, height=3, bg="#FBFCFE", fg=self.TEXT, relief="flat", bd=0, font=("Consolas", 9))
        self.log.pack(fill="x")
        controls = ttk.Frame(content, style="Card.TFrame", padding=(14, 0, 0, 0)); controls.pack(side="right", fill="y")
        ttk.Label(controls, text="运行状态", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(controls, textvariable=self.status_var, style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(controls, text="停止任务", style="Small.TButton", command=self.stop_download).grid(row=1, column=0, pady=8, padx=(0, 5))
        ttk.Button(controls, text="清空日志", style="Small.TButton", command=self.clear_log).grid(row=1, column=1, pady=8, padx=5)
        ttk.Button(controls, text="导出日志", style="Small.TButton", command=self.export_log).grid(row=1, column=2, pady=8, padx=(5, 0))
        self.write_log("工作台已启动，正在检查两个工具。")

    def path_row(self, parent, key):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=(5, 2))
        ttk.Entry(row, textvariable=self.vars[key]).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3, command=lambda: self.choose_dir(key)).pack(side="left", padx=(5, 0))

    def choose_dir(self, key):
        current = self.vars[key].get().strip()
        folder = filedialog.askdirectory(initialdir=current if Path(current).is_dir() else None)
        if folder:
            self.vars[key].set(folder)
            self.save_settings()

    def paste_links(self):
        try:
            self.links.insert("end", self.clipboard_get())
        except tk.TclError:
            pass

    def clear_links(self):
        self.links.delete("1.0", "end")
        self.update_link_count()

    def on_links_modified(self, _event=None):
        if not self.links.edit_modified():
            return
        self.links.edit_modified(False)
        self.update_link_count()

    def update_link_count(self):
        self.link_count_var.set(f"已识别 {len(self.extract_links())} 个")

    def remove_completed_link(self, completed_url):
        target = completed_url.rstrip(URL_TRAILING_PUNCTUATION)
        if not target:
            return False
        original = self.links.get("1.0", "end-1c")
        changed = False
        remaining_lines = []
        for line in original.splitlines():
            matches = URL_PATTERN.findall(line)
            updated = line
            for match in matches:
                if match.rstrip(URL_TRAILING_PUNCTUATION) == target:
                    updated = updated.replace(match, "", 1)
                    changed = True
            if updated.strip(" \t" + URL_TRAILING_PUNCTUATION):
                remaining_lines.append(updated)
        if not changed:
            return False
        self.links.delete("1.0", "end")
        if remaining_lines:
            self.links.insert("1.0", "\n".join(remaining_lines))
        self.links.edit_modified(False)
        self.update_link_count()
        return True

    def import_links(self):
        path = filedialog.askopenfilename(title="导入链接文件", filetypes=(("文本文件", "*.txt"), ("所有文件", "*.*")))
        if not path:
            return
        try:
            self.links.insert("end", Path(path).read_text(encoding="utf-8"))
            self.show_link_count()
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("导入失败", str(exc))

    def show_link_count(self):
        count = len(self.extract_links())
        self.link_count_var.set(f"已识别 {count} 个")
        self.write_log(f"已提取 {count} 个有效链接。")
        self.status_var.set(f"已识别 {count} 个视频链接")

    def select_mode(self, name):
        self.mode.set(name)
        for mode_name, (frame, label, detail) in self.mode_cards.items():
            selected = mode_name == name
            bg = "#EDF5FF" if selected else "white"
            frame.configure(bg=bg, highlightbackground=self.BLUE if selected else self.BORDER, highlightthickness=2 if selected else 1)
            label.configure(bg=bg, fg=self.BLUE if selected else self.TEXT)
            detail.configure(bg=bg)

    def save_template(self):
        path = filedialog.asksaveasfilename(title="保存工作台模板", defaultextension=".json", filetypes=(("JSON 模板", "*.json"),))
        if not path:
            return
        payload = {k: v.get().strip() for k, v in self.vars.items()}
        payload.update({"subtitle_mode": self.mode.get(), "auto_subtitle": self.auto_subtitle.get(), "preview_first": self.preview_first.get(), "logo_brand": self.logo_brand.get()})
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_log(f"模板已保存：{path}")

    def load_template(self):
        path = filedialog.askopenfilename(title="加载工作台模板", filetypes=(("JSON 模板", "*.json"), ("所有文件", "*.*")))
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for key, variable in self.vars.items():
                if key in data: variable.set(str(data[key]))
            if data.get("subtitle_mode") in self.mode_cards: self.select_mode(data["subtitle_mode"])
            self.auto_subtitle.set(bool(data.get("auto_subtitle", True)))
            self.preview_first.set(bool(data.get("preview_first", False)))
            self.logo_brand.set(str(data.get("logo_brand", "无")))
            self.save_settings(); self.write_log(f"模板已加载：{path}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("加载失败", str(exc))

    def show_help(self):
        messagebox.showinfo(
            "使用帮助",
            "1. 粘贴视频链接并选择去字幕模式\n"
            "2. 下载完成后扫描去字幕成品\n"
            "3. 打开批量语音客户端，维护文案并生成音频\n"
            "4. 进入拼接工具，确认视频、音频、语言、Logo 和数量\n\n"
            "提示：批量语音客户端需要先完成目标网站适配。",
        )

    def stop_download(self):
        if not self.download_process or self.download_process.poll() is not None:
            return messagebox.showinfo("没有任务", "当前没有正在运行的后台下载任务。")
        if messagebox.askyesno("停止任务", "确定停止当前整批下载吗？已完成文件会保留。"):
            self.download_process.terminate()
            self.write_log("已请求停止当前后台任务。")

    def clear_log(self):
        if messagebox.askyesno("清空日志", "确定清空当前界面日志吗？"):
            self.log.delete("1.0", "end")

    def export_log(self):
        path = filedialog.asksaveasfilename(title="导出日志", defaultextension=".txt", filetypes=(("文本日志", "*.txt"),))
        if path:
            Path(path).write_text(self.log.get("1.0", "end-1c"), encoding="utf-8")

    def _log_is_throttled(self, text):
        """True when a known repetitive line was already logged in its window."""
        for index, (pattern, window_seconds, per_text) in enumerate(LOG_THROTTLE_RULES):
            if not pattern.search(text):
                continue
            key = (index, text if per_text else "")
            now = time.monotonic()
            last = self.__dict__.setdefault("_log_throttle", {}).get(key)
            if last is not None and now - last < window_seconds:
                return True
            self._log_throttle[key] = now
            return False
        return False

    def write_log(self, text):
        if self._log_is_throttled(str(text)):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{stamp}] {text}\n")
        self.log.see("end")
        append_workbench_log(text)

    def check_environment(self):
        checks = {
            "TikHub": Path(self.vars["tikhub_launcher"].get()).is_file(),
            "TikHub 下载目录": Path(self.vars["tikhub_downloads"].get()).is_dir(),
            "批量语音客户端": self._voice_client_path().is_file(),
            "AI 拼接工具": Path(self.vars["stitcher_launcher"].get()).is_file(),
        }
        missing = [name for name, ok in checks.items() if not ok]
        if missing:
            self.status_var.set("环境缺失：" + "、".join(missing))
            self.write_log("环境检查失败：" + "、".join(missing))
        else:
            self.status_var.set("系统就绪 · 三个工具均可用")
            self.write_log("环境检查通过：TikHub AI、批量语音客户端与拼接工具均已找到。")

    def _voice_client_path(self):
        configured = self.vars.get("voice_client")
        return Path(configured.get().strip()) if configured else VOICE_CLIENT_DIR / "BatchVoiceClient.exe"

    @staticmethod
    def _task_voice_profile(task):
        """Return the canonical voice profile, including legacy V13 rows.

        The batch receiver is authoritative for new rows, but a task already
        imported before that normalization may have only top-level
        ``voicePlan``/``audioBatch`` fields.  Materialize the same profile in
        memory so those rows cannot silently fall into the reuse-audio path.
        """
        if not isinstance(task, dict):
            return {}
        profile = task.get("pipelineProfile")
        voice = profile.get("3_voice") if isinstance(profile, dict) else None
        if isinstance(voice, dict) and voice:
            return voice
        try:
            normalized = ensure_pipeline_profile(task)
        except (TypeError, ValueError):
            normalized = task
        profile = normalized.get("pipelineProfile") if isinstance(normalized, dict) else {}
        voice = profile.get("3_voice") if isinstance(profile, dict) else {}
        return voice if isinstance(voice, dict) else {}

    @staticmethod
    def _voice_task_identity(task):
        task = task if isinstance(task, dict) else {}
        voice = Workbench._task_voice_profile(task)
        return {
            "runGroupId": str(task.get("runGroupId") or "").strip(),
            "batchId": str(task.get("batchId") or "").strip(),
            "canonicalTaskId": canonical_task_id(task),
            "manifestId": str(task.get("manifestId") or "").strip(),
            "productionDate": str(task.get("productionDate") or task.get("date") or "").strip(),
            "audioBatch": str(voice.get("audioBatch") or task.get("audioBatch") or "").strip(),
        }

    def _voice_batch_recovery_facts(self, task, record=None):
        """Build a verifiable identity for one multi-language Fish command.

        The workbench launches one Node/Fish producer for the language plan,
        while copy assignments are selected per language inside that run.  A
        fresh batch therefore has no text hash for the pending rows yet.  Do
        not borrow the completed Spanish assignment (or the parent task as a
        child); bind the recovery command to the immutable language plan and
        its actual voice mapping instead.
        """
        task = task if isinstance(task, dict) else {}
        record = record if isinstance(record, dict) else {}
        planned = [item for item in contract_language_plan(task) if isinstance(item, dict)]
        if not planned:
            return {}
        rules = {}
        try:
            rules_path = self._voice_client_path().parent / "data" / "language_voice_rules.csv"
            if rules_path.is_file():
                with rules_path.open("r", encoding="utf-8-sig", newline="") as handle:
                    rules = {
                        str(row.get("language") or "").strip(): str(row.get("voice") or "").strip()
                        for row in csv.DictReader(handle)
                        if str(row.get("language") or "").strip() and str(row.get("voice") or "").strip()
                    }
        except (OSError, UnicodeError, csv.Error):
            rules = {}
        language_rows = []
        for item in planned:
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or code).strip()
            language_rows.append({
                "code": code,
                "name": name,
                "audioCount": int(item.get("audioCount") or 1),
                "outputCount": int(item.get("outputCount") or 0),
                "voice": rules.get(name, ""),
            })
        if not all(row["code"] and row["name"] and row["voice"] for row in language_rows):
            return {}
        canonical = str(record.get("canonicalTaskId") or task.get("canonicalTaskId") or canonical_task_id(task)).strip()
        run_group = str(record.get("runGroupId") or task.get("runGroupId") or "").strip()
        batch = str(record.get("batchId") or task.get("batchId") or "").strip()
        production_date = str(task.get("productionDate") or task.get("date") or "").strip()
        material = str(record.get("material") or task.get("material") or "").strip()
        audio_batch = str(
            self._task_voice_profile(task).get("audioBatch")
            or task.get("audioBatch") or record.get("audioBatch") or ""
        ).strip()
        if not canonical or not run_group or not batch or not material or not audio_batch:
            return {}
        contract_payload = {
            "version": 1,
            "canonicalTaskId": canonical,
            "runGroupId": run_group,
            "batchId": batch,
            "productionDate": production_date,
            "project": str(task.get("project") or "").strip(),
            "material": material,
            "audioBatch": audio_batch,
            "languages": language_rows,
        }
        digest = hashlib.sha256(
            json.dumps(contract_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        language_set = ",".join(f"{row['code']}:{row['outputCount']}" for row in language_rows)
        return {
            "audioBatchContractId": f"voice-batch-v1|{digest}",
            "audioTaskId": f"voice-batch-v1|{canonical}|{audio_batch}|{digest[:16]}",
            "product": material,
            "material": material,
            "language": f"language-plan:{language_set}",
            "languageSet": language_set,
            "voiceContractId": f"voice-plan-v1|{digest}",
            "copyContractState": f"copy-plan-pending-v1|{digest}",
        }

    @staticmethod
    def _voice_identity_matches(left, right):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        left_task = str(left.get("canonicalTaskId") or "").strip()
        right_task = str(right.get("canonicalTaskId") or "").strip()
        if not left_task or left_task != right_task:
            return False
        for key in ("runGroupId", "batchId", "manifestId", "productionDate", "audioBatch"):
            left_value = str(left.get(key) or "").strip()
            right_value = str(right.get(key) or "").strip()
            if left_value and right_value and left_value != right_value:
                return False
        return True

    def _voice_launch_state_file(self):
        return self._voice_client_path().parent / "data" / "voice_launch_state.json"

    def _persist_voice_launch_state(self, state, identity, *, pid=0, error_code="", message="", **extra):
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        payload = {
            "version": 1,
            **(identity if isinstance(identity, dict) else {}),
            "launchState": str(state or ""),
            "nodePid": int(pid or 0),
            "errorCode": str(error_code or ""),
            "message": str(message or ""),
            "updatedAt": now,
            **extra,
        }
        path = self._voice_launch_state_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            self.write_log(f"保存语音启动状态失败：{exc}")
        return payload

    def _voice_launch_result_for_existing_process(self, task):
        process = self.voice_batch_process
        if not process:
            return ""
        if process.poll() is not None:
            self.voice_batch_process = None
            self.voice_launch_identity = {}
            return ""
        requested = self._voice_task_identity(task)
        owner = self.voice_launch_identity if isinstance(self.voice_launch_identity, dict) else {}
        if not self._voice_identity_matches(owner, requested):
            detail = (
                "其他任务的第 3 步配音仍在运行；当前任务保持等待，"
                "避免两个 Fish 批次争用同一浏览器。"
            )
            self._persist_voice_launch_state(
                "blocked_other_task", requested, pid=getattr(process, "pid", 0),
                error_code="VOICE_BUSY_OTHER_TASK", message=detail,
                ownerIdentity=owner,
            )
            self.write_log(detail)
            return VOICE_LAUNCH_BLOCKED_OTHER_TASK
        listener_pid = self._fish_bridge_listener_pid(5178)
        state = VOICE_LAUNCH_ALREADY_RUNNING if listener_pid else VOICE_LAUNCH_ALREADY_STARTING
        code = "VOICE_ALREADY_RUNNING_CURRENT_TASK" if listener_pid else "VOICE_ALREADY_STARTING_CURRENT_TASK"
        detail = "当前任务第 3 步配音正在运行" if listener_pid else "当前任务第 3 步配音正在启动"
        self._persist_voice_launch_state(
            "fish_listening" if listener_pid else "waiting_for_bridge",
            requested,
            pid=getattr(process, "pid", 0),
            error_code=code,
            message=detail,
            bridgePort=5178,
        )
        self.write_log(f"{detail}；忽略重复启动请求。")
        return state

    def refresh_voice_info(self):
        root = self._voice_client_path().parent
        task_file = root / "data" / "tasks.csv"
        active_audio_dir, rule_count = self._sync_voice_output_dirs()
        checks = (
            ("桌面客户端", self._voice_client_path().is_file()),
            ("Node.js 运行时", (root / "runtime" / "node.exe").is_file()),
            ("Chromium 浏览器", (root / ".browsers" / "chrome-win64" / "chrome.exe").is_file()),
            ("网页适配器", (root / "src" / "siteAdapter.js").is_file()),
        )
        task_count = 0
        if task_file.is_file():
            try:
                with task_file.open("r", encoding="utf-8-sig", newline="") as handle:
                    task_count = sum(1 for _row in csv.DictReader(handle))
            except (OSError, UnicodeError, csv.Error):
                task_count = 0
        lines = [f"{'✓' if ok else '✗'}  {name}" for name, ok in checks]
        dependencies = check_voice_dependencies(root)
        lines.insert(0, '✓  配音依赖可加载' if dependencies['ok'] else '✗  ' + dependency_message(dependencies))
        lines.extend((
            "",
            f"当前任务：{task_count} 条",
            f"步骤 4 目录规则：{rule_count} 条",
            f"当前项目今日目录：{active_audio_dir or '未配置'}",
        ))
        self.voice_info_var.set("\n".join(lines))
        self._refresh_voice_batch_context()

    def _refresh_voice_batch_context(self):
        task = self.active_external_task or load_current_context()
        if not task:
            self.voice_batch_context_var.set("未导入外部任务：按原有语音目录规则运行。")
            return
        voice = self._task_voice_profile(task)
        if not voice:
            self.voice_batch_context_var.set("当前任务没有音频批次记录，请人工确认语音目录。")
            return
        creates_voice = bool(voice.get("requiresNewGeneration")) or voice.get("voicePlan") == "create_new"
        cycle = "新建语音" if creates_voice else ("当天生成" if voice.get("cycle") == "daily" else "复用每周音频")
        self.voice_batch_context_var.set(
            f"生产日期：{task.get('date')}  ·  项目/材料：{task.get('project')} / {task.get('material')}\n"
            f"音频批次：{voice.get('audioBatch')}（{cycle}）\n"
            f"音频目录：{resolve_task_audio_directory(task) or '家里路径未配置'}"
        )

    def launch_voice_client(self, unattended=False):
        path = self._voice_client_path()
        task = self.active_external_task or load_current_context()
        identity = self._voice_task_identity(task)
        if not path.is_file():
            detail = f"找不到批量语音客户端：\n{path}"
            if unattended:
                self.status_var.set(f"无人值守语音未启动：{detail}")
                self.write_log(self.status_var.get())
                self._persist_voice_launch_state(
                    "failed", identity, error_code="VOICE_NODE_START_FAILED", message=detail,
                )
                return VOICE_LAUNCH_FAILED
            messagebox.showerror("无法启动", detail)
            return VOICE_LAUNCH_FAILED
        if unattended:
            existing_result = self._voice_launch_result_for_existing_process(task)
            if existing_result:
                return existing_result
        dependencies = check_voice_dependencies(path.parent)
        if not dependencies['ok']:
            detail = dependency_message(dependencies)
            self.last_voice_launch_error = {'errorCode': dependencies['code'], 'message': detail}
            self._persist_voice_launch_state('failed', identity, error_code=dependencies['code'], message=detail)
            task_id = canonical_task_id(task)
            if task_id:
                self._set_remote_pipeline_status(task_id, 'blocked_voice', detail, error_code=dependencies['code'])
            self.status_var.set(detail)
            self.voice_info_var.set(detail)
            self.write_log(detail)
            return VOICE_LAUNCH_FAILED
        active_audio_dir, rule_count = self._sync_voice_output_dirs()
        if rule_count == 0:
            detail = "第四步骤还没有可用的音频目录，请先在步骤 4 配置项目音频目录。"
            if unattended:
                self.status_var.set(f"无人值守语音未启动：{detail}")
                self.write_log(self.status_var.get())
                self._persist_voice_launch_state(
                    "failed", identity, error_code="VOICE_PLAN_CREATE_FAILED", message=detail,
                )
                return VOICE_LAUNCH_FAILED
            messagebox.showerror("目录未配置", detail)
            return VOICE_LAUNCH_FAILED
        voice = self._task_voice_profile(task)
        creates_voice = bool(voice) and (
            bool(voice.get("requiresNewGeneration")) or voice.get("voicePlan") == "create_new"
        )
        if isinstance(voice, dict) and voice.get("cycle") == "weekly" and active_audio_dir and not creates_voice:
            existing = list(Path(active_audio_dir).glob("*.mp3")) if Path(active_audio_dir).is_dir() else []
            if existing and messagebox.askyesno(
                "复用本周音频批次",
                f"音频批次 {voice.get('audioBatch')} 已有 {len(existing)} 个 MP3：\n{active_audio_dir}\n\n"
                "选择“是”：复用音频，不启动浏览器生成。\n"
                "选择“否”：仍启动语音客户端，供你补齐或重做。",
            ):
                self.status_var.set(f"已复用每周音频批次 {voice.get('audioBatch')} · {len(existing)} 个 MP3")
                self.write_log(self.status_var.get())
                return
        self.save_settings()
        if unattended:
            root = path.parent
            node = root / "runtime" / "node.exe"
            if not node.is_file():
                node = Path(r"C:\Program Files\nodejs\node.exe")
            runner = root / "src" / "runBatch.js"
            if not node.is_file() or not runner.is_file():
                detail = "找不到 Node.js 或 runBatch.js。"
                self.write_log(f"无人值守语音未启动：{detail}")
                self._persist_voice_launch_state(
                    "failed", identity, error_code="VOICE_NODE_START_FAILED", message=detail,
                )
                return VOICE_LAUNCH_FAILED
            project = str(task.get("project") or "").strip()
            material = str(task.get("material") or "").strip()
            if not project or not material:
                detail = "远程任务缺少项目名称或材料名称。"
                self.write_log(f"无人值守语音未启动：{detail}")
                self._persist_voice_launch_state(
                    "failed", identity, error_code="VOICE_PLAN_CREATE_FAILED", message=detail,
                )
                return VOICE_LAUNCH_FAILED
            task_id = canonical_task_id(task)
            bridge_ready, bridge_detail = self._prepare_fish_bridge_for_launch(task_id)
            if not bridge_ready:
                self.status_var.set(f"无人值守语音未启动：{bridge_detail}")
                self.write_log(self.status_var.get())
                self._persist_voice_launch_state(
                    "failed", identity, error_code="VOICE_BUSY_OTHER_TASK", message=bridge_detail,
                )
                return VOICE_LAUNCH_BLOCKED_OTHER_TASK
            self._persist_voice_launch_state(
                "preparing_plan", identity, message="正在生成当前任务的 Fish 配音计划",
                nodePath=str(node), audioOutputDir=str(active_audio_dir or ""), bridgePort=5178,
            )
            try:
                task_file, task_ids = self._write_remote_voice_task_plan(root, task)
            except (OSError, ValueError) as exc:
                detail = f"Remote voice plan was not created: {exc}"
                self.write_log(detail)
                self._persist_voice_launch_state(
                    "failed", identity, error_code="VOICE_PLAN_CREATE_FAILED", message=detail,
                )
                return VOICE_LAUNCH_FAILED
            env = os.environ.copy()
            env["RUN_PROJECTS"] = project
            env["RUN_TASK_IDS"] = "|".join(task_ids)
            env["TASK_FILE"] = str(task_file)
            env["RUN_GROUP_ID"] = identity.get("runGroupId", "")
            env["SOURCE_BATCH_ID"] = identity.get("batchId", "")
            env["CANONICAL_TASK_ID"] = identity.get("canonicalTaskId", "")
            env["MANIFEST_ID"] = identity.get("manifestId", "")
            env["PRODUCTION_DATE"] = identity.get("productionDate", "")
            env["AUDIO_BATCH"] = identity.get("audioBatch", "")
            if active_audio_dir:
                env["AUDIO_OUTPUT_DIR"] = str(active_audio_dir)
            try:
                self.voice_batch_process = subprocess.Popen(
                    [str(node), str(runner)],
                    cwd=str(root),
                    env=env,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                detail = f"无人值守语音启动失败：{exc}"
                self.write_log(detail)
                self.voice_batch_process = None
                self.voice_launch_identity = {}
                self._persist_voice_launch_state(
                    "failed", identity, error_code="VOICE_NODE_START_FAILED", message=detail,
                    taskPlanPath=str(task_file),
                )
                return VOICE_LAUNCH_FAILED
            self.voice_launch_identity = dict(identity)
            started_at = time.monotonic()
            self._persist_voice_launch_state(
                "process_started", identity, pid=int(self.voice_batch_process.pid or 0),
                message="Node进程已启动，正在等待Fish桥接服务",
                taskPlanPath=str(task_file), nodePath=str(node),
                audioOutputDir=str(active_audio_dir or ""), bridgePort=5178,
                startedAt=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            self._update_continue_bridge_state(
                task_id,
                "starting_fish",
                "5178 端口已释放，正在启动 Fish 桥接服务",
                pid=int(self.voice_batch_process.pid or 0),
            )
            active_label = voice.get("audioBatch") if isinstance(voice, dict) else "当前批次"
            self.status_var.set(f"无人值守语音已启动：{project} / {active_label}；Fish 页面将自动打开")
            self.write_log(self.status_var.get())
            self.after(
                1000,
                lambda value=task_id, requested=dict(identity), began=started_at:
                    self._confirm_voice_launch(value, requested, began),
            )
            return VOICE_LAUNCH_STARTED
        try:
            self.voice_batch_process = subprocess.Popen([str(path)], cwd=str(path.parent), shell=False)
        except OSError as exc:
            self.voice_batch_process = None
            messagebox.showerror("启动失败", str(exc))
            return VOICE_LAUNCH_FAILED
        action_label = "新建语音" if creates_voice else "补齐/重新生成语音"
        self.status_var.set(f"批量语音客户端已启动：{action_label}")
        current_task = self.active_external_task or load_current_context() or {}
        active_label = (
            voice.get("audioBatch") if isinstance(voice, dict) else
            str(current_task.get("productionDate") or current_task.get("date") or "当前批次")
        )
        self.write_log(f"已启动批量语音客户端：{action_label}；已同步 {rule_count} 条目录规则，当前音频批次为 {active_label}。")
        return VOICE_LAUNCH_STARTED

    def _voice_payload_matches_identity(self, payload, identity):
        if not isinstance(payload, dict) or not isinstance(identity, dict):
            return False
        task_id = str(identity.get("canonicalTaskId") or "").strip()
        if not self._voice_payload_matches_task(payload, task_id):
            return False
        for key in ("runGroupId", "sourceBatchId", "manifestId", "productionDate", "audioBatch"):
            identity_key = "batchId" if key == "sourceBatchId" else key
            expected = str(identity.get(identity_key) or "").strip()
            observed = str(payload.get(key) or "").strip()
            if expected and observed and expected != observed:
                return False
        return True

    def _fail_voice_launch_confirmation(self, task_id, identity, error_code, detail, *, exit_code=None):
        process = self.voice_batch_process
        pid = int(getattr(process, "pid", 0) or 0) if process else 0
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        self.voice_batch_process = None
        self.voice_launch_identity = {}
        self.voice_launch_records.pop(task_id, None)
        self._release_voice_launch_guard(task_id)
        self._persist_voice_launch_state(
            "failed", identity, pid=pid, error_code=error_code, message=detail,
            exitCode=exit_code, bridgePort=5178,
            failedAt=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._set_remote_pipeline_status(task_id, "blocked_voice", detail, error_code=error_code)
        self.status_var.set(detail)
        self.write_log(detail)
        self._finish_continue_command(task_id, "blocked", detail, phase="voice_launch_failed")
        return False

    def _confirm_voice_launch(self, task_id, identity, started_monotonic):
        active = self.active_external_task or load_current_context() or {}
        if canonical_task_id(active) != task_id:
            self.write_log("当前任务已变化；停止更新旧任务的语音启动界面状态。")
            return False
        process = self.voice_batch_process
        if not process:
            return self._fail_voice_launch_confirmation(
                task_id, identity, "VOICE_PROCESS_EXITED_EARLY",
                "第 3 步 Node 进程在 Fish 桥接启动前消失；任务和媒体已保留。",
            )
        return_code = process.poll()
        handoff = self._read_local_json_object(self._voice_handoff_file())
        matching_handoff = self._voice_payload_matches_identity(handoff, identity)
        handoff_status = str(handoff.get("status") or "").lower() if matching_handoff else ""
        listener_pid = self._fish_bridge_listener_pid(5178)
        if matching_handoff and handoff_status in {"running", "completed", "completed_with_errors", "failed"}:
            state = "fish_listening" if handoff_status == "running" else handoff_status
            self._persist_voice_launch_state(
                state, identity, pid=int(getattr(process, "pid", 0) or 0),
                message="Fish 桥接已确认，当前任务配音正在运行" if handoff_status == "running" else "Fish 已返回当前任务终态",
                bridgePort=5178, bridgePid=listener_pid,
                fishBatchId=str(handoff.get("batchId") or ""),
            )
            if handoff_status == "running":
                self._set_remote_pipeline_status(task_id, "voice_running", "第 3 步配音正在运行")
            return True
        if return_code is not None:
            return self._fail_voice_launch_confirmation(
                task_id, identity, "VOICE_PROCESS_EXITED_EARLY",
                f"第 3 步 Node 进程在 Fish 桥接确认前退出（退出码 {return_code}）；任务和媒体已保留。",
                exit_code=int(return_code),
            )
        elapsed = max(0.0, time.monotonic() - float(started_monotonic))
        if elapsed >= VOICE_BRIDGE_START_TIMEOUT_SECONDS:
            return self._fail_voice_launch_confirmation(
                task_id, identity, "VOICE_BRIDGE_START_TIMEOUT",
                "第 3 步 Node 已启动，但30秒内没有确认当前任务的Fish桥接；已结束本轮自有进程并解除锁。",
            )
        self._persist_voice_launch_state(
            "waiting_for_bridge", identity, pid=int(getattr(process, "pid", 0) or 0),
            message="Node进程已启动，正在等待当前任务Fish桥接确认",
            bridgePort=5178, observedListenerPid=listener_pid,
            elapsedSeconds=round(elapsed, 1),
        )
        self._set_remote_pipeline_status(task_id, "voice_starting", "第 3 步配音正在启动，等待Fish桥接确认")
        if self.winfo_exists():
            self.after(
                1000,
                lambda value=task_id, requested=dict(identity), began=started_monotonic:
                    self._confirm_voice_launch(value, requested, began),
            )
        return True

    def _write_remote_voice_task_plan(self, voice_root, task):
        """Build a material-specific language plan for one received task.

        The manual tasks.csv is intentionally never reused for remote work: it
        may describe the material from an earlier batch.  This plan follows the
        maintained language list and the copy library's project/material rules.
        """
        planned_languages = contract_language_plan(task)
        if not planned_languages:
            raise ValueError("remote task language plan is empty")
        task_id = str(task.get("taskId") or "").strip()
        project = str(task.get("project") or "").strip()
        material = str(task.get("material") or "").strip()
        if not task_id or not project or not material:
            raise ValueError("remote task is missing taskId, project or material")
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", task_id)
        folder = Path(voice_root) / "data" / "remote_task_plans"
        folder.mkdir(parents=True, exist_ok=True)
        plan = folder / f"{safe_id}.csv"
        rows = []
        for index, planned in enumerate(planned_languages, 1):
            rows.append({
                "id": f"remote-{safe_id}-{index:02d}",
                "project": project,
                "language": str(planned.get("name") or planned.get("code") or ""),
                "languageCode": str(planned.get("code") or ""),
                "material": material,
                "voice": "default",
                "text": "",
                "outputName": "",
                "status": "",
                "audioCount": int(planned.get("audioCount") or 1),
                "outputCount": int(planned.get("outputCount") or 0),
                "expectedCount": contract_expected_count(task),
                "canonicalTaskId": str(task.get("canonicalTaskId") or task_id),
                "runGroupId": str(task.get("runGroupId") or ""),
                "sourceBatchId": str(task.get("batchId") or ""),
                "manifestId": str(task.get("manifestId") or ""),
                "productionDate": str(task.get("productionDate") or task.get("date") or ""),
                "copyLibraryVersion": str(task.get("copyLibraryVersion") or ""),
                "audioBatch": str(
                    self._task_voice_profile(task).get("audioBatch")
                    or task.get("audioBatch") or ""
                ),
            })
        temporary = plan.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, plan)
        return plan, [row["id"] for row in rows]

    @staticmethod
    def _validate_reuse_audio_contract(task):
        """Resolve only the audio batch explicitly named by the task.

        Reusable audio filenames legitimately contain an older remote inbox
        id.  That media identity must not be treated as the owner of the new
        video task, and a missing declared batch must never silently fall back
        to the newest directory on disk.
        """
        audio_dir = resolve_task_audio_directory(task)
        if audio_dir is None:
            return None, "任务没有可解析的复用音频目录"
        audio_dir = Path(audio_dir)
        if not audio_dir.is_dir():
            return None, f"任务声明的音频批次目录不存在：{audio_dir}"

        project = str((task or {}).get("project") or "").strip()
        material = str((task or {}).get("material") or "").strip()
        planned = [item for item in contract_language_plan(task) if isinstance(item, dict)]
        expected = sum(max(1, int(item.get("audioCount") or 1)) for item in planned)
        candidates = [
            path for path in audio_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac"}
            and path.stat().st_size > 0
            and (not project or project in path.stem)
            and (not material or material in path.stem)
        ]
        if len(candidates) != expected:
            return None, (
                f"{audio_dir} 中当前项目有效音频应为 {expected} 条，实际 {len(candidates)} 条"
            )
        missing = []
        for item in planned:
            name = str(item.get("name") or item.get("code") or "").strip()
            code = str(item.get("code") or "").strip().lower()
            if not any(name in path.stem or f"_{code}_" in path.stem.lower() for path in candidates):
                missing.append(name or code)
        if missing:
            return None, f"缺少语言音频：{'、'.join(missing)}"
        return audio_dir, ""

    def _expected_remote_voice_task_count(self, task):
        """Return the durable language-plan size for one received task.

        A remote run must not move into stitching after a filtered/manual
        retry has produced only part of its language set.  The original plan
        is written before Fish starts, so it is the authoritative count.
        """
        task_id = str((task or {}).get("taskId") or "").strip()
        if not task_id:
            return 0
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", task_id)
        plan = self._voice_client_path().parent / "data" / "remote_task_plans" / f"{safe_id}.csv"
        try:
            with plan.open("r", encoding="utf-8-sig", newline="") as handle:
                return sum(1 for row in csv.DictReader(handle) if str(row.get("id") or "").strip())
        except (OSError, UnicodeError, csv.Error):
            return 0

    def open_voice_tasks(self):
        path = self._voice_client_path().parent / "data" / "tasks.csv"
        if path.is_file():
            os.startfile(str(path))
        else:
            messagebox.showwarning("任务表不存在", f"找不到任务表：\n{path}")

    def open_voice_docs(self):
        path = self._voice_client_path().parent / "桌面客户端说明.md"
        if path.is_file():
            os.startfile(str(path))
        else:
            messagebox.showwarning("说明不存在", f"找不到使用说明：\n{path}")

    def open_voice_downloads(self):
        active_audio_dir, rule_count = self._sync_voice_output_dirs()
        if rule_count == 0 or active_audio_dir is None:
            return messagebox.showwarning("目录未配置", "第四步骤当前项目没有可用的音频目录。")
        path = Path(active_audio_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return messagebox.showerror("目录不可用", str(exc))
        self.vars["audio_dir"].set(str(path))
        self.save_settings()
        os.startfile(str(path))

    def _sync_voice_output_dirs(self):
        """同步音频输出规则；外部任务优先使用其独立音频批次。"""
        config_path = Path(self.vars["stitcher_projects"].get().strip())
        output_file = self._voice_client_path().parent / "data" / "project_output_dirs.csv"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            return None, 0
        # Material presets were generalised for all project/material pairs.
        # Keep the voice-directory synchroniser on the renamed helper; the
        # old entity-only name no longer exists and previously made every
        # unattended Fish launch fail before its task plan was written.
        payload = self._ensure_material_stitch_projects(payload, config_path)

        projects = payload.get("projects", {})
        if not isinstance(projects, dict):
            return None, 0
        active_project = str(payload.get("active_project", "")).strip()
        task = self.active_external_task or load_current_context()
        production_date = str((task or {}).get("productionDate") or (task or {}).get("date") or "").strip()
        today_folder = production_date or f"{datetime.now().month}.{datetime.now().day}"
        task_voice = self._task_voice_profile(task)
        task_audio_dir = resolve_task_audio_directory(task) if task_voice else None
        rules = []
        active_audio_dir = None

        # ExampleBrand has no legacy saved stitcher project. Its voice output rule is
        # derived directly from the immutable receiver contract, while formal
        # projects continue through the existing project loop below.
        if str((task or {}).get("batchKind") or "") == "examplebrand-natural-flow" and task_audio_dir:
            rules.append({
                "project": "ExampleBrand",
                "material": str((task or {}).get("material") or ""),
                "baseDir": str(Path(task_audio_dir).parent),
                "folderName": "",
                "dateMode": "fixed",
                "dateValue": str(task_voice.get("audioBatch") or today_folder),
                "outputDir": "",
            })
            active_audio_dir = Path(task_audio_dir)

        for stitch_project, data in projects.items():
            if not isinstance(data, dict):
                continue
            audio_folder = str(data.get("audio_folder", "")).strip()
            explicit_root = str(data.get("audio_material_root", "")).strip()
            if not audio_folder and not explicit_root:
                continue

            material_root = Path(explicit_root or audio_folder)
            if not explicit_root and re.fullmatch(r"\d{1,2}\.\d{1,2}", material_root.name):
                material_root = material_root.parent

            project_name, material_name = self._voice_output_project_identity(stitch_project, data)

            is_external_match = bool(task_voice) and project_name == str(task.get("project") or "") and (
                not material_name or material_name == str(task.get("material") or "")
            )
            date_mode = "fixed" if is_external_match else "auto"
            date_value = str(task_voice.get("audioBatch") or "") if is_external_match else ""

            rules.append({
                "project": project_name,
                "material": material_name,
                "baseDir": str(material_root),
                "folderName": "",
                "dateMode": date_mode,
                "dateValue": date_value,
                "outputDir": "",
            })
            if is_external_match:
                active_audio_dir = task_audio_dir or material_root / date_value
            if str(stitch_project).strip() == active_project:
                active_audio_dir = active_audio_dir or material_root / today_folder

        if not rules:
            return None, 0
        try:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with output_file.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("project", "material", "baseDir", "folderName", "dateMode", "dateValue", "outputDir"),
                )
                writer.writeheader()
                writer.writerows(rules)
        except OSError as exc:
            self.write_log(f"同步步骤 4 音频目录失败：{exc}")
            return None, 0

        if active_audio_dir is None and task_audio_dir:
            active_audio_dir = task_audio_dir
        if active_audio_dir is None and rules:
            active_audio_dir = Path(rules[0]["baseDir"]) / today_folder
        self.vars["audio_dir"].set(str(active_audio_dir))
        return active_audio_dir, len(rules)

    def _voice_output_project_identity(self, stitch_project, data):
        """Return the task-level project/material identity for a stitcher entry.

        Stitcher projects are named for human use (for example ``ProjectA碳岩板``),
        while remote tasks store the two fields separately (``ProjectA`` and
        ``碳岩板``).  Keeping the combined name here makes the voice-client CSV
        miss the task's output rule and it can then fall back to whichever
        project happened to be selected in the UI.
        """
        configured_name = str(stitch_project).strip()
        configured_material = MATERIAL_NAMES.get(str(data.get("cover_material", "")).strip(), "")
        if configured_name.casefold().startswith("ProjectB"):
            return "ProjectB", configured_material

        # These project names conventionally append the material name.  Split
        # them before serialising the voice output rules so a task's
        # project/material key is exact and cannot fall back to another root.
        for project_name in ("SPC墙板", "SPC地板", "ProjectA", "ProjectD", "ProjectC"):
            if configured_name.casefold().startswith(project_name.casefold()):
                suffix = configured_name[len(project_name):].strip()
                return project_name, configured_material or suffix
        return configured_name, configured_material

    def _ensure_material_stitch_projects(self, payload, config_path):
        projects = payload.get("projects", {})
        if not isinstance(projects, dict):
            return payload
        changed = False
        template = copy.deepcopy(projects.get("默认项目") or next(iter(projects.values()), {}))
        if not isinstance(template, dict):
            return payload
        for project_name, materials in STITCH_PROJECT_MATERIALS.items():
            brand = PROJECT_BRANDS[project_name]
            for material_name in materials:
                rule = STITCH_MATERIAL_RULES[material_name]
                name = f"{project_name}{material_name}"
                data = projects.get(name)
                created = False
                if not isinstance(data, dict):
                    source = next(
                        (
                            value for key, value in projects.items()
                            if str(key).startswith(project_name) and isinstance(value, dict)
                        ),
                        template,
                    )
                    data = copy.deepcopy(source)
                    projects[name] = data
                    created = True
                    changed = True

                if rule["use_ai"]:
                    # A saved project can be copied from another material.  A
                    # non-empty path is therefore not proof that it is still
                    # the correct public AI library.  In particular, a wood
                    # veneer task must never inherit the carbon-crystal
                    # (石纹) folder just because that directory exists.
                    expected_ai_folder = Path(r"E:\AI素材库\素材1") / rule["ai_folder"]
                    configured_ai_folder = Path(str(data.get("ai_folder") or "").strip())
                    if configured_ai_folder.name != rule["ai_folder"]:
                        data["ai_folder"] = str(expected_ai_folder)
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
                    if picker_dirs.get("old"):
                        changed = True
                    picker_dirs["old"] = ""
                    data["picker_dirs"] = picker_dirs

                if str(data.get("cover_material") or "") != rule["cover_material"]:
                    data["cover_material"] = rule["cover_material"]
                    changed = True
                if str(data.get("logo_brand") or "") != brand:
                    data["logo_brand"] = brand
                    data["logo_path"] = str(
                        config_path.parent / ("ExampleBrandB-transparent.png" if brand == "ExampleBrandB" else "ExampleBrandC-transparent.png")
                    )
                    changed = True
                if str(data.get("prefix") or "") != name:
                    data["prefix"] = name
                    changed = True
                if created:
                    data["audio_folder"] = ""
                    data["audio_material_root"] = default_material_audio_root(project_name, material_name)
                    data["audio_batch"] = ""
                data.setdefault("audio_auto_latest", True)
                data.setdefault("language_tasks", [])
        if changed:
            try:
                temporary = config_path.with_suffix(config_path.suffix + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(config_path)
            except OSError as exc:
                self.write_log(f"补齐第四步骤ProjectB材料项目失败：{exc}")
        return payload

    def _voice_handoff_file(self):
        return self._voice_client_path().parent / "data" / "last_batch_handoff.json"

    def _voice_handoff_state_file(self):
        return self._voice_client_path().parent / "data" / "workbench_handoff_state.json"

    def _fish_bridge_runtime_file(self):
        return self._voice_client_path().parent / "data" / "fish_bridge_runtime.json"

    @staticmethod
    def _read_local_json_object(path):
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _fish_bridge_listener_pid(port=5178):
        """Return the PID listening on the local Fish bridge port, if any."""
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        pattern = re.compile(
            rf"^\s*TCP\s+(?:127\.0\.0\.1|\[::1\]):{int(port)}\s+\S+\s+LISTENING\s+(\d+)\s*$",
            re.IGNORECASE,
        )
        for line in (result.stdout or "").splitlines():
            match = pattern.match(line)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    return 0
        return 0

    @staticmethod
    def _fish_bridge_process_command(pid):
        pid = int(pid or 0)
        if pid <= 0:
            return ""
        command = (
            "$item = Get-CimInstance Win32_Process -Filter 'ProcessId="
            f"{pid}'; if ($null -ne $item) {{ $item.CommandLine }}"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return str(result.stdout or "").strip()

    @staticmethod
    def _is_our_fish_bridge_command(command):
        normalized = str(command or "").replace("/", "\\").lower()
        return "node" in normalized and "voice-client\\src\\runbatch.js" in normalized

    def _update_continue_bridge_state(self, task_id, state, message, pid=0):
        """Persist non-sensitive Fish bridge status for the Dashboard command."""
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        try:
            request = self._read_local_json_object(CONTINUE_COMMAND_FILE)
            if str(request.get("taskId") or "").strip() != task_id:
                return
            if str(request.get("state") or "") not in {"requested", "accepted", "recovering", "running"}:
                return
            request["bridgeState"] = str(state or "")
            request["bridgeMessage"] = str(message or "")
            request["bridgePort"] = 5178
            request["bridgeUpdatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
            if pid:
                request["bridgePid"] = int(pid)
            self._write_continue_command(request)
        except OSError as exc:
            self.write_log(f"保存 Fish 桥接状态失败：{exc}")

    def _mark_fish_bridge_runtime_cleaned(self, pid, reason):
        runtime_file = self._fish_bridge_runtime_file()
        payload = self._read_local_json_object(runtime_file)
        payload.update({
            "version": max(1, self._integer(payload.get("version"), 1)),
            "pid": int(pid or payload.get("pid") or 0),
            "port": 5178,
            "state": "cleaned",
            "cleanupReason": str(reason or ""),
            "cleanedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        try:
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            temp = runtime_file.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, runtime_file)
        except OSError as exc:
            self.write_log(f"保存 Fish 桥接清理状态失败：{exc}")

    def _terminate_confirmed_fish_bridge(self, pid, reason):
        """Stop only a command line proven to be this system's terminal Fish bridge."""
        pid = int(pid or 0)
        if pid <= 0:
            return False, "Fish 桥接进程 PID 无效"
        command = self._fish_bridge_process_command(pid)
        if not self._is_our_fish_bridge_command(command):
            return False, "5178 占用进程未被识别为本系统 Fish 桥接，已保留该进程"
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"无法结束已确认的 Fish 桥接 PID {pid}：{exc}"
        time.sleep(0.25)
        if self._fish_bridge_listener_pid(5178):
            return False, f"已请求结束 Fish 桥接 PID {pid}，但 5178 仍在占用"
        self._mark_fish_bridge_runtime_cleaned(pid, reason)
        return True, f"已清理旧 Fish 桥接 PID {pid}，5178 已释放"

    def _prepare_fish_bridge_for_launch(self, task_id):
        """Preflight 5178 without ever terminating an unrelated program."""
        listener_pid = self._fish_bridge_listener_pid(5178)
        if not listener_pid:
            self._update_continue_bridge_state(task_id, "port_released", "Fish 桥接端口 5178 空闲")
            return True, "Fish 桥接端口 5178 空闲"

        command = self._fish_bridge_process_command(listener_pid)
        if not self._is_our_fish_bridge_command(command):
            detail = f"5178 被外部程序占用（PID {listener_pid}）：{command[:240] or '无法读取命令行'}"
            self._update_continue_bridge_state(task_id, "blocked_external_port", detail, listener_pid)
            return False, detail

        runtime = self._read_local_json_object(self._fish_bridge_runtime_file())
        handoff = self._read_local_json_object(self._voice_handoff_file())
        runtime_pid = self._integer(runtime.get("pid"), 0)
        runtime_terminal = str(runtime.get("terminalStatus") or runtime.get("state") or "").lower()
        handoff_terminal = str(handoff.get("status") or "").lower()
        terminal_states = {"completed", "completed_with_errors", "failed", "cancelled", "canceled", "terminal", "cleaned"}
        confirmed_terminal = (
            (runtime_pid in {0, listener_pid} and runtime_terminal in terminal_states)
            or handoff_terminal in terminal_states
        )
        if not confirmed_terminal:
            detail = f"本系统 Fish 桥接 PID {listener_pid} 仍属于运行中的批次；为避免重复配音，暂不启动第二个批次"
            self._update_continue_bridge_state(task_id, "bridge_busy", detail, listener_pid)
            return False, detail

        self._update_continue_bridge_state(task_id, "cleaning_stale_bridge", f"正在清理旧 Fish 桥接 PID {listener_pid}", listener_pid)
        cleaned, detail = self._terminate_confirmed_fish_bridge(listener_pid, "所属批次已完成、失败或取消")
        if cleaned:
            self._update_continue_bridge_state(task_id, "port_released", detail)
        else:
            self._update_continue_bridge_state(task_id, "blocked_bridge_cleanup", detail, listener_pid)
        return cleaned, detail

    def _cleanup_terminal_fish_bridge(self, payload):
        """Best-effort cleanup after a terminal handoff, including after restart."""
        status = str((payload or {}).get("status") or "").lower()
        if status not in {"completed", "completed_with_errors", "failed", "cancelled", "canceled"}:
            return
        listener_pid = self._fish_bridge_listener_pid(5178)
        if not listener_pid:
            return
        command = self._fish_bridge_process_command(listener_pid)
        if not self._is_our_fish_bridge_command(command):
            return
        cleaned, detail = self._terminate_confirmed_fish_bridge(listener_pid, f"终态语音批次：{status}")
        if cleaned:
            self.write_log(detail)

    def _voice_payload_matches_task(self, payload, task_id):
        """Return whether a voice handoff belongs to the current canonical task.

        New handoffs include canonicalTaskIds.  The fallback keeps today\'s
        already-written handoff usable because its remote task id contains the
        canonical inbox id.
        """
        if not task_id:
            return False
        candidates = []
        for key in ("canonicalTaskId", "taskId"):
            value = str(payload.get(key) or "").strip()
            if value:
                candidates.append(value)
        for key in ("canonicalTaskIds", "taskIds"):
            values = payload.get(key)
            if isinstance(values, list):
                candidates.extend(str(value or "").strip() for value in values)
        for key in ("completedItems", "failedItems"):
            for item in payload.get(key) or []:
                if isinstance(item, dict):
                    candidates.extend(
                        str(item.get(field) or "").strip()
                        for field in ("canonicalTaskId", "taskId")
                    )
        return any(candidate == task_id or task_id in candidate for candidate in candidates if candidate)

    def _completed_voice_handoff_for_task(self, task):
        """Return a verified, task-scoped completed voice handoff for recovery.

        A Dashboard recovery must not open Fish again merely because the old
        handoff has already been marked handled.  Audio ownership, count and
        file existence are checked here before Step 4 is allowed to resume.
        """
        task_id = canonical_task_id(task)
        if not task_id:
            return None
        try:
            payload = json.loads(self._voice_handoff_file().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            return None
        if str(payload.get("status") or "") != "completed" or not self._voice_payload_matches_task(payload, task_id):
            return None
        project = str(task.get("project") or "").strip()
        material = str(task.get("material") or "").strip()
        owned = []
        for item in payload.get("completedItems") or []:
            if not isinstance(item, dict):
                continue
            item_task = str(item.get("canonicalTaskId") or item.get("taskId") or "").strip()
            if not (item_task == task_id or task_id in item_task):
                continue
            if str(item.get("project") or "").strip() != project or str(item.get("material") or "").strip() != material:
                continue
            audio_path = Path(str(item.get("audioPath") or "").strip())
            if not audio_path.is_file():
                return None
            owned.append(dict(item))
        expected = self._expected_remote_voice_task_count(task)
        if not owned or (expected and len(owned) != expected):
            return None
        return {
            **payload,
            "taskId": task_id,
            "canonicalTaskId": task_id,
            "taskIds": [task_id],
            "canonicalTaskIds": [task_id],
            "total": len(owned),
            "completed": len(owned),
            "failed": 0,
            "completedItems": owned,
            "failedItems": [],
        }

    def _release_voice_launch_guard(self, task_id):
        """Allow a failed task to be retried without allowing concurrent runs."""
        if task_id:
            self.voice_auto_launch_task_ids.discard(task_id)
        if self.voice_batch_process and self.voice_batch_process.poll() is not None:
            self.voice_batch_process = None
            self.voice_launch_identity = {}

    def _retire_voice_launch_record(self, task_id, batch_id):
        """Forget one terminal Fish launch before another recovery is allowed."""
        if task_id:
            launch = self.voice_launch_records.get(task_id)
            if isinstance(launch, dict) and str(launch.get("batchId") or "").strip() == str(batch_id or "").strip():
                self.voice_launch_records.pop(task_id, None)
        self._release_voice_launch_guard(task_id)
        self._remember_voice_handoff(batch_id)

    def _load_voice_handoff_state(self):
        try:
            payload = json.loads(self._voice_handoff_state_file().read_text(encoding="utf-8-sig"))
            batch_ids = payload.get("handledBatchIds", [])
            if isinstance(batch_ids, list):
                self.voice_handoff_handled.update(str(item).strip() for item in batch_ids if str(item).strip())
        except (OSError, ValueError, UnicodeError, AttributeError):
            pass

    def _remember_voice_handoff(self, batch_id):
        batch_id = str(batch_id).strip()
        if not batch_id:
            return
        self.voice_handoff_handled.add(batch_id)
        state_file = self._voice_handoff_state_file()
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            handled = sorted(self.voice_handoff_handled)[-50:]
            temporary = state_file.with_suffix(state_file.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"version": 1, "handledBatchIds": handled}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(state_file)
        except OSError as exc:
            self.write_log(f"保存语音交接状态失败：{exc}")

    def poll_voice_handoff(self):
        try:
            payload = json.loads(self._voice_handoff_file().read_text(encoding="utf-8-sig"))
            batch_id = str(payload.get("batchId", "")).strip()
            status = str(payload.get("status", "")).strip()
            terminal_status = status.lower() in {"completed", "completed_with_errors", "failed", "cancelled", "canceled"}
            if terminal_status and status.lower() in {"failed", "completed_with_errors"}:
                # Handoff consumption and Dashboard-command completion are
                # separate idempotent duties.  Reconcile even when this batch
                # is already present in handledBatchIds.
                self._reconcile_terminal_voice_command(payload)
            if batch_id and terminal_status and batch_id not in self.voice_handoff_handled:
                # This also covers a workbench restart: a terminal handoff must
                # never leave its own Node bridge holding 5178 indefinitely.
                self._cleanup_terminal_fish_bridge(payload)
            # A voice reset/cancel is a terminal event for the *current*
            # Dashboard recovery command.  Previously it was ignored here,
            # leaving that command in ``running`` forever even though the
            # Node/Fish process had stopped.  Do not restart Fish from this
            # observer: release the task-level guard and let the operator send
            # one fresh, idempotent safe-continue command after the reset.
            if batch_id and status.lower() in {"cancelled", "canceled"}:
                task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
                task_id = canonical_task_id(task)
                launch = self.voice_launch_records.get(task_id) if task_id else None
                command = self._read_local_json_object(CONTINUE_COMMAND_FILE)
                command_targets_task = bool(
                    task_id
                    and isinstance(command, dict)
                    and str(command.get("taskId") or "").strip() == task_id
                )
                # A cancelled handoff can be a deliberately reset *old* batch
                # for the same canonical project.  It must not block a later
                # Dashboard click.  Only a batch which this workbench session
                # actually launched and associated with its task-level record
                # is allowed to close the current continue command.
                is_current_launch = bool(
                    isinstance(launch, dict)
                    and str(launch.get("batchId") or "").strip() == batch_id
                )
                if batch_id in self.voice_handoff_handled:
                    return
                if command_targets_task and voice_terminal_predates_continue_command(payload, command):
                    # The command was created after this batch had already
                    # been cancelled.  Archive the old terminal event exactly
                    # once and leave the fresh command free to start Fish.
                    self._retire_voice_launch_record(task_id, batch_id)
                    self.write_log(
                        f"已归档早于当前安全继续命令的历史取消语音批次 {batch_id}；不会阻止新配音批次。"
                    )
                    return
                if task_id and is_current_launch:
                    # Retire before closing the command.  Without this, the
                    # next polling cycle sees the same cancelled handoff and
                    # repeatedly blocks every newly created continue command.
                    self._retire_voice_launch_record(task_id, batch_id)
                    detail = "第 3 步配音批次已取消或重置；未生成的音频保持等待，可重新点击安全继续。"
                    self._set_remote_pipeline_status(task_id, "voice_pending", detail)
                    self.status_var.set(detail)
                    self.write_log(detail)
                    self._finish_continue_command(task_id, "blocked", detail)
                elif (
                    task_id
                    and self._voice_payload_matches_task(payload, task_id)
                ):
                    self._remember_voice_handoff(batch_id)
                    self.write_log(
                        f"已忽略历史取消的语音批次 {batch_id}；它不属于当前工作台会话，不会阻止新的安全继续。"
                    )
                return
            if batch_id and status == "running":
                task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
                task_id = str(task.get("taskId") or "").strip()
                canonical_id = canonical_task_id(task)
                launch = self.voice_launch_records.get(canonical_id) if canonical_id else None
                if (
                    isinstance(launch, dict)
                    and not str(launch.get("batchId") or "").strip()
                    and self._voice_payload_matches_task(payload, canonical_id)
                ):
                    # The Node bridge writes its batch handoff just after
                    # Popen returns.  Bind that new batch ID here; never infer
                    # ownership from an older file merely sharing the project.
                    launch["batchId"] = batch_id
                    launch["bridgePid"] = self._integer(
                        self._read_local_json_object(self._fish_bridge_runtime_file()).get("pid"),
                        0,
                    )
                if canonical_id and self._voice_payload_matches_task(payload, canonical_id):
                    runtime = self._read_local_json_object(self._fish_bridge_runtime_file())
                    bridge = payload.get("bridge") if isinstance(payload.get("bridge"), dict) else {}
                    extension_version = str(bridge.get("extensionVersion") or "").strip()
                    extension_version_ok = bridge.get("extensionVersionOk")
                    if extension_version_ok is False:
                        required_version = str(bridge.get("requiredExtensionVersion") or "1.3.32").strip()
                        detail = (
                            f"Fish 插件版本 {extension_version or '未知'}，需要重新加载 {required_version}；"
                            "已保留配音任务，未派发任何音色。"
                        )
                        self._set_remote_pipeline_status(canonical_id, "voice_pending", detail)
                        self._update_continue_bridge_state(
                            canonical_id,
                            "fish_version_mismatch",
                            detail,
                            pid=self._integer(runtime.get("pid"), 0),
                        )
                        version_signature = (batch_id, extension_version, required_version)
                        if version_signature != getattr(self, "_last_fish_version_signature", None):
                            self._last_fish_version_signature = version_signature
                            self.status_var.set(detail)
                            self.write_log(detail)
                        return
                    self._update_continue_bridge_state(
                        canonical_id,
                        "fish_listening",
                        "Fish 桥接已监听 5178，正在等待或处理配音任务",
                        pid=self._integer(runtime.get("pid"), 0),
                    )
                total = self._integer(payload.get("total"), self._expected_remote_voice_task_count(task))
                completed = self._integer(payload.get("completed"), 0)
                failed = self._integer(payload.get("failed"), 0)
                current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
                language = str(current.get("language") or "").strip()
                detail = f"第 3 步配音：{completed}/{total}，失败 {failed}"
                if language:
                    detail += f"，当前 {language}"
                signature = (batch_id, completed, failed, language)
                if signature != getattr(self, "_last_voice_progress_signature", None):
                    self._last_voice_progress_signature = signature
                    self.status_var.set(detail)
                    self.write_log(detail)
                    if task_id:
                        self._set_remote_pipeline_status(task_id, "voice_running", detail)
            if batch_id and status in {"completed", "completed_with_errors", "failed"} and batch_id not in self.voice_handoff_handled:
                self._remember_voice_handoff(batch_id)
                self._handle_voice_handoff(payload)
        except (OSError, ValueError, UnicodeError, AttributeError):
            pass
        finally:
            if self.winfo_exists():
                self.after(1200, self.poll_voice_handoff)

    def _reconcile_terminal_voice_command(self, payload):
        """Close a matching in-flight continue command for a failed Fish batch."""
        task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        task_id = canonical_task_id(task)
        batch_id = str(payload.get("batchId") or "").strip()
        command = self._read_local_json_object(CONTINUE_COMMAND_FILE)
        if not task_id or not batch_id or not isinstance(command, dict):
            return False
        if str(command.get("taskId") or "").strip() != task_id:
            return False
        if str(command.get("state") or "") not in {"requested", "accepted", "recovering", "running"}:
            return False
        command_batch = str(command.get("batchId") or "").strip()
        if command_batch and command_batch != batch_id:
            return False
        if not self._voice_payload_matches_task(payload, task_id):
            return False
        if voice_terminal_predates_continue_command(payload, command):
            self._remember_voice_handoff(batch_id)
            return False
        completed = self._integer(payload.get("completed"), len(payload.get("completedItems") or []))
        total = self._integer(payload.get("total"), self._expected_remote_voice_task_count(task))
        failed = self._integer(payload.get("failed"), len(payload.get("failedItems") or []))
        errors = payload.get("failedItems") if isinstance(payload.get("failedItems"), list) else []
        first_error = ""
        first_error_code = str(payload.get("errorCode") or "").strip()
        if errors and isinstance(errors[0], dict):
            first_error = str(errors[0].get("error") or errors[0].get("message") or "").strip()
            first_error_code = str(errors[0].get("errorCode") or first_error_code).strip()
        first_error_code = first_error_code or "VOICE_BATCH_INCOMPLETE"
        detail = f"第 3 步配音失败：已成功 {completed}/{total or completed}，失败 {failed}；可安全继续重试缺失语音。"
        if first_error:
            detail += f" 原因：{first_error}"
        self._retire_voice_launch_record(task_id, batch_id)
        first_failed_item = errors[0] if errors and isinstance(errors[0], dict) else {}
        self._set_remote_pipeline_status(
            task_id, "blocked_voice", detail, error_code=first_error_code,
            child_task_id=str(first_failed_item.get("taskId") or "").strip(),
            language=str(first_failed_item.get("language") or "").strip(),
            failure_event_id=str(payload.get("failureEventId") or "").strip(),
            child_task_type="voice",
        )
        self.status_var.set(detail)
        self.write_log(detail)
        self._finish_continue_command(task_id, "blocked", detail)
        return True

    def _handle_voice_handoff(self, payload):
        task = self.active_external_task or {}
        unattended = self._is_remote_auto_sequence_task(task)
        task_id = canonical_task_id(task)
        belongs_to_active_task = self._voice_payload_matches_task(payload, task_id)
        if self.voice_batch_process and self.voice_batch_process.poll() is not None:
            self.voice_batch_process = None
            self.voice_launch_identity = {}
        if task_id and belongs_to_active_task:
            self.voice_launch_records.pop(task_id, None)
        if bool(payload.get("loginRequired")):
            detail = "Fish Audio 登录已失效，任务已保留，可登录后点击安全继续重试"
            if belongs_to_active_task:
                self._persist_voice_launch_state(
                    "failed", self._voice_task_identity(task),
                    error_code="VOICE_LOGIN_REQUIRED", message=detail,
                    fishBatchId=str(payload.get("batchId") or ""),
                )
                self._release_voice_launch_guard(task_id)
                self._set_remote_pipeline_status(task_id, "blocked_voice", detail, error_code="VOICE_LOGIN_REQUIRED")
                self._finish_continue_command(task_id, "blocked", detail)
            self.status_var.set(detail)
            self.write_log(self.status_var.get())
            return
        if not unattended:
            self.deiconify()
            self.lift()
            self.focus_force()
        items = payload.get("completedItems", [])
        if not isinstance(items, list) or not items:
            error = str(payload.get("error", "")).strip()
            detail = error or "本次没有生成可交接的音频。"
            failed_items = payload.get("failedItems") if isinstance(payload.get("failedItems"), list) else []
            first_failed = failed_items[0] if failed_items and isinstance(failed_items[0], dict) else {}
            first_failed_error = str(first_failed.get("error") or first_failed.get("message") or "").strip()
            if first_failed_error and first_failed_error not in detail:
                detail = f"{detail} 原因：{first_failed_error}"
            error_code = str(
                first_failed.get("errorCode")
                or payload.get("errorCode")
                or "VOICE_BATCH_INCOMPLETE"
            ).strip()
            self.write_log(f"步骤 3 批次未生成可交接音频。 {detail}")
            if belongs_to_active_task:
                self._persist_voice_launch_state(
                    "failed", self._voice_task_identity(task),
                    error_code=error_code, message=detail,
                    fishBatchId=str(payload.get("batchId") or ""),
                )
                self._release_voice_launch_guard(task_id)
                self._set_remote_pipeline_status(
                    task_id, "blocked_voice", f"第 3 步配音失败：{detail}",
                    error_code=error_code,
                    child_task_id=str(first_failed.get("taskId") or "").strip(),
                    language=str(first_failed.get("language") or "").strip(),
                    failure_event_id=str(payload.get("failureEventId") or "").strip(),
                    child_task_type="voice",
                )
                self.status_var.set(f"第 3 步配音失败，任务已保留；可点击安全继续重试。{detail}")
                self._finish_continue_command(task_id, "blocked", self.status_var.get())
            if not unattended:
                messagebox.showwarning("语音批次未完成", detail)
            return

        groups = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            project = str(item.get("project", "")).strip()
            material = str(item.get("material", "")).strip()
            audio_path = str(item.get("audioPath", "")).strip()
            key = (project, material)
            group = groups.setdefault(key, {
                "project": project, "material": material, "count": 0, "audio_dir": "",
            })
            group["count"] += 1
            if audio_path:
                group["audio_dir"] = str(Path(audio_path).parent)

        available = []
        for group in groups.values():
            if str(group.get("project") or "") == "ExampleBrand" and str(task.get("batchKind") or "") == "examplebrand-natural-flow":
                group["stitch_project"] = "ExampleBrand专属动态拼接"
            else:
                group["stitch_project"] = self._match_stitch_project(group["project"], group["material"])
            if group["stitch_project"]:
                available.append(group)
        if not available:
            detail = "语音已经生成，但没有找到对应的第四步骤项目。"
            if unattended and task_id:
                self._set_remote_pipeline_status(task_id, "blocked_stitch", detail)
                self.status_var.set(detail)
                self.write_log(detail)
            else:
                messagebox.showwarning("无法自动交接", detail)
            return

        if unattended:
            task = self.active_external_task or {}
            requested_project = str(task.get("project") or "").strip()
            requested_material = str(task.get("material") or "").strip()
            selected = next(
                (
                    group for group in available
                    if group["project"] == requested_project
                    and (not requested_material or group["material"] == requested_material)
                ),
                None,
            )
        else:
            selected = available[0] if len(available) == 1 else self._choose_voice_group(available)
        if selected is None:
            self.write_log("步骤 3 已完成；已保留多材料交接清单，用户暂未选择第四步骤项目。")
            return
        failed = int(payload.get("failed", 0) or 0)
        if unattended:
            task = self.active_external_task or {}
            expected = self._expected_remote_voice_task_count(task)
            if failed or (expected and selected["count"] < expected):
                detail = f"语音还未齐全：已成功 {selected['count']} / 计划 {expected or selected['count']}，失败 {failed}"
                self.status_var.set(detail + "。已停在第 3 步，不会自动进入拼接。")
                self.write_log(self.status_var.get())
                if task_id:
                    self._persist_voice_launch_state(
                        "failed", self._voice_task_identity(task),
                        error_code="VOICE_BATCH_INCOMPLETE", message=detail,
                        fishBatchId=str(payload.get("batchId") or ""),
                    )
                    self._release_voice_launch_guard(task_id)
                    self._set_remote_pipeline_status(task_id, "blocked_voice", detail, error_code="VOICE_BATCH_INCOMPLETE")
                    self._finish_continue_command(task_id, "blocked", detail)
                return
        self.write_log(
            f"步骤 3 已完成并交接：{selected['project']} / {selected['material'] or '通用'}，"
            f"{selected['count']} 个音频 → 第四步骤“{selected['stitch_project']}”。"
        )
        self.status_var.set(f"语音完成 · 正在进入 {selected['stitch_project']}")
        if unattended and task_id:
            self._persist_voice_launch_state(
                "completed", self._voice_task_identity(task),
                message="第 3 步配音已完成并通过当前任务身份验证",
                fishBatchId=str(payload.get("batchId") or ""),
                completedAt=str(payload.get("completedAt") or datetime.now().astimezone().isoformat(timespec="seconds")),
            )
            self._set_remote_pipeline_status(task_id, "voice_completed", "第 3 步配音完成，正在进入第 4 步")
        if selected.get("audio_dir"):
            self.vars["audio_dir"].set(str(selected["audio_dir"]))
        if str(task.get("batchKind") or "") != "examplebrand-natural-flow":
            self.show_stitch_page(not unattended, target_project=selected["stitch_project"])
        if unattended:
            task_id = str((self.active_external_task or {}).get("taskId") or "").strip()
            if task_id:
                # The embedded page has been constructed synchronously above.
                # Starting here avoids an orphaned Tk after() callback leaving
                # a completed remote voice handoff silently stuck at Step 3.
                self._start_remote_stitch(task_id)
        if failed:
            messagebox.showwarning("部分语音生成失败", f"已生成 {len(items)} 个，失败 {failed} 个；已加载成功音频。")

    def _start_remote_stitch(self, task_id):
        """Start the prepared Step 4 only for the active remote queue item."""
        task = self.active_external_task or {}
        if canonical_task_id(task) != task_id or task_id in self.stitch_auto_launch_task_ids:
            return
        if str(task.get("batchKind") or "") == "examplebrand-natural-flow":
            try:
                sources, failures, _subject = self._task_stitch_source_manifest(task)
                if failures:
                    raise ExamplebrandProductionError("ExampleBrand_ASSET_IDENTITY_MISMATCH", "\n".join(failures))
                audio_dir = Path(str(self.vars["audio_dir"].get() or "").strip())
                # Reload the task-bound replenishment module at the execution boundary so a long-lived workbench cannot keep a pre-fix single-child implementation in memory.
                import importlib
                import examplebrand_replenishment as examplebrand_replenishment_module
                examplebrand_replenishment_module = importlib.reload(examplebrand_replenishment_module)
                execution_task = examplebrand_replenishment_module.prepare(
                    task, sources, TASK_IMPORTED_DIR, DOWNLOAD_CHECKPOINT_DIR, ExampleBrand_ARTIFACT_ROOT)
                composer = ExamplebrandProductionComposer(execution_task, sources, audio_dir, ExampleBrand_ARTIFACT_ROOT)
                # Resource validation is synchronous and read-only. Do not start a
                # background FFmpeg run until every ExampleBrand-only gate has passed.
                composer.validate_resources()
                self.stitch_auto_launch_task_ids.add(task_id)
                self._set_remote_pipeline_status(task_id, "stitch_running", "第4步ExampleBrand动态裁剪与主体拼接正在运行")
                self.status_var.set("ExampleBrand第4步：正在按实际配音时长裁剪普通素材")

                def progress(done, total, output_id):
                    self.after(0, lambda: self.status_var.set(
                        f"ExampleBrand第4步：{done}/{total} · {str(output_id)[-10:]}"
                    ))

                def worker():
                    try:
                        records, record_path = composer.execute(progress=progress)
                        self.after(0, lambda: self.on_stitch_records("ExampleBrand", records, record_path))
                    except Exception as exc:
                        code = str(getattr(exc, "error_code", "ExampleBrand_CLIP_ANALYSIS_FAILED") or "ExampleBrand_CLIP_ANALYSIS_FAILED")
                        detail = f"ExampleBrand第4步已安全停止：{exc}"
                        self.after(0, lambda: self._finish_examplebrand_stitch_failure(task_id, code, detail))

                threading.Thread(target=worker, daemon=True, name=f"examplebrand-stitch-{task_id[-12:]}").start()
                return
            except Exception as exc:
                code = str(getattr(exc, "error_code", "ExampleBrand_ASSET_IDENTITY_MISMATCH") or "ExampleBrand_ASSET_IDENTITY_MISMATCH")
                self._finish_examplebrand_stitch_failure(task_id, code, f"ExampleBrand资源门禁未通过：{exc}")
                return
        controller = self.stitcher_controller
        if controller is None:
            message = "第 4 步拼接器不可用，未启动自动拼接"
            self.status_var.set(message)
            self.write_log(message)
            self._set_remote_pipeline_status(task_id, "blocked_stitch", message, error_code="STITCH_CLIENT_UNAVAILABLE")
            return
        try:
            started = controller.start_unattended()
            if started:
                self.stitch_auto_launch_task_ids.add(task_id)
                self._set_remote_pipeline_status(task_id, "stitch_running", "第 4 步AI素材拼接正在运行")
                self.status_var.set("Remote Step 4 started automatically; waiting for finished videos.")
                self.write_log(self.status_var.get())
            else:
                message = "第 4 步拼接器未启动工作线程"
                self.status_var.set(message)
                self.write_log(message)
                self._set_remote_pipeline_status(task_id, "blocked_stitch", message, error_code="STITCH_THREAD_NOT_STARTED")
        except Exception as exc:
            message = f"第 4 步自动拼接启动失败：{exc}"
            self.status_var.set(message)
            self.write_log(message)
            self._set_remote_pipeline_status(task_id, "blocked_stitch", message, error_code="STITCH_PROCESS_START_FAILED")

    def _finish_examplebrand_stitch_failure(self, task_id, error_code, detail):
        self.stitch_auto_launch_task_ids.discard(task_id)
        self.status_var.set(detail)
        self.write_log(f"ExampleBrand第4步失败 [{error_code}]：{detail}")
        self._set_remote_pipeline_status(task_id, "blocked_stitch", detail, error_code=error_code)
        self._finish_continue_command(task_id, "blocked", detail)

    def _match_stitch_project(self, project, material):
        config_path = Path(self.vars["stitcher_projects"].get().strip())
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            return None
        projects = payload.get("projects", {})
        if not isinstance(projects, dict):
            return None
        payload = self._ensure_material_stitch_projects(payload, config_path)
        exact_name = f"{str(project).strip()}{str(material).strip()}"
        data = projects.get(exact_name)
        if not isinstance(data, dict):
            return None
        configured_material = MATERIAL_NAMES.get(str(data.get("cover_material", "")).strip(), "")
        return exact_name if configured_material == str(material).strip() else None

    def _choose_voice_group(self, groups):
        dialog = tk.Toplevel(self)
        dialog.title("选择先进入第四步骤的材料")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("560x420")
        dialog.configure(bg=self.BG)
        result = {"value": None}
        ttk.Label(dialog, text="本次生成了多个项目或材料", style="Title.TLabel").pack(anchor="w", padx=24, pady=(22, 6))
        ttk.Label(dialog, text="请选择先进入第四步骤处理的一组；其他音频已保存在对应目录。", foreground=self.MUTED).pack(anchor="w", padx=24)
        choices = tk.Listbox(dialog, font=("Microsoft YaHei UI", 10), activestyle="none", relief="solid", bd=1)
        choices.pack(fill="both", expand=True, padx=24, pady=18)
        for group in groups:
            choices.insert("end", f"{group['project']} · {group['material'] or '通用'} · {group['count']} 个音频")
        choices.selection_set(0)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=24, pady=(0, 20))

        def confirm():
            selection = choices.curselection()
            if selection:
                result["value"] = groups[selection[0]]
                dialog.destroy()

        ttk.Button(buttons, text="稍后处理", style="Secondary.TButton", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="进入第四步骤", style="Purple.TButton", command=confirm).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        return result["value"]

    def launch_tikhub(self):
        path = Path(self.vars["tikhub_launcher"].get())
        if not path.is_file():
            return messagebox.showerror("无法启动", f"找不到 TikHub 启动文件：\n{path}")
        subprocess.Popen(["cmd.exe", "/c", "start", "", str(path)], cwd=str(path.parent), shell=False)
        self.write_log("已启动 TikHub AI。请在原工具中粘贴链接并开始下载。")

    def extract_links(self):
        text = self.links.get("1.0", "end").strip()
        found = URL_PATTERN.findall(text)
        return list(dict.fromkeys(url.rstrip(URL_TRAILING_PUNCTUATION) for url in found))

    @staticmethod
    def _auto_recovery_default_state():
        # A missing control file is deliberately paused.  Deploying or
        # restarting the Workbench must never start a production recovery until
        # the operator explicitly enables this session.
        return {
            "schemaVersion": AUTO_RECOVERY_SCHEMA_VERSION,
            "manualPause": True,
            "entries": {},
        }

    @contextlib.contextmanager
    def _auto_recovery_lock(self):
        """Serialize recovery claims across workbench/dashboard processes."""
        AUTO_RECOVERY_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = AUTO_RECOVERY_LOCK_FILE.open("a+b")
        try:
            if os.name == "nt" and handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield handle
        finally:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _read_auto_recovery_state_unlocked(self):
        try:
            payload = json.loads(AUTO_RECOVERY_STATE_FILE.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return self._auto_recovery_default_state()
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            # Never turn a damaged control file into a fresh budget.  Pause and
            # preserve the diagnostic so an operator can repair it explicitly.
            state = self._auto_recovery_default_state()
            state["readError"] = f"{type(exc).__name__}: {exc}"
            return state
        if not isinstance(payload, dict):
            state = self._auto_recovery_default_state()
            state["readError"] = "automatic recovery state is not an object"
            return state
        version = payload.get("schemaVersion", payload.get("version"))
        entries = payload.get("entries") if isinstance(payload.get("entries"), dict) else {}
        if version == 1:
            # v1 was batch-scoped and cannot be safely mapped to a language or
            # output task.  Keep it as audit data, but do not copy its count.
            return {
                "schemaVersion": AUTO_RECOVERY_SCHEMA_VERSION,
                "manualPause": True,
                "entries": {},
                "legacyEntries": entries,
                "migrationBlocked": bool(entries),
                "readError": "v1 recovery entries require explicit child-task migration",
            }
        if version == 2:
            # v2 already has per-entry records, but older producers sometimes
            # used the parent task as the child identity.  Keep valid entries
            # and pause only the entries that cannot be proven safe.
            blocked = {}
            valid = {}
            for key, entry in entries.items():
                if isinstance(entry, dict) and str(entry.get("childTaskId") or "").strip() and str(entry.get("step") or "").strip():
                    valid[key] = entry
                else:
                    blocked[key] = entry
            result = {
                "schemaVersion": AUTO_RECOVERY_SCHEMA_VERSION,
                "manualPause": True if blocked else bool(payload.get("manualPause", True)),
                "entries": valid,
            }
            if blocked:
                result["legacyEntries"] = blocked
                result["migrationBlocked"] = True
                result["readError"] = "v2 entries without verified childTaskId require migration"
            return result
        if version != AUTO_RECOVERY_SCHEMA_VERSION:
            state = self._auto_recovery_default_state()
            state["readError"] = f"unsupported recovery schema: {version!r}"
            return state
        return {
            "schemaVersion": AUTO_RECOVERY_SCHEMA_VERSION,
            "manualPause": bool(payload.get("manualPause", True)),
            "entries": entries,
            **({"legacyEntries": payload["legacyEntries"]} if isinstance(payload.get("legacyEntries"), dict) else {}),
            **({"migrationBlocked": True} if payload.get("migrationBlocked") else {}),
            **({"readError": str(payload["readError"])} if payload.get("readError") else {}),
        }

    def _read_auto_recovery_state(self):
        controller = self.__dict__.get("recovery_control")
        if controller is not None:
            return controller.status()
        try:
            with self._auto_recovery_lock():
                return self._read_auto_recovery_state_unlocked()
        except (OSError, ValueError, TypeError) as exc:
            state = self._auto_recovery_default_state()
            state["readError"] = f"lock/read failed: {type(exc).__name__}: {exc}"
            return state

    _atomic_write_json = staticmethod(atomic_write_json)

    def _write_auto_recovery_state(self, payload, *, locked=False):
        if not locked:
            with self._auto_recovery_lock():
                return self._write_auto_recovery_state(payload, locked=True)
        normalized = dict(payload)
        normalized["schemaVersion"] = AUTO_RECOVERY_SCHEMA_VERSION
        normalized.pop("version", None)
        self._atomic_write_json(AUTO_RECOVERY_STATE_FILE, normalized)

    def _write_continue_command(self, payload):
        """Use the same fsynced unique-temp writer for manual and auto commands."""
        self._atomic_write_json(CONTINUE_COMMAND_FILE, payload)

    def set_auto_recovery_enabled(self):
        from concurrent.futures import ThreadPoolExecutor
        enabled = bool(self.auto_recovery_var.get())
        if "_recovery_executor" not in self.__dict__:
            self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recovery-control")
            self._recovery_poll_future = None
        def save():
            if enabled and not self.recovery_control.acceptance_verified():
                raise RecoveryControlError("recovery_not_accepted", "自动恢复尚未通过真实验收")
            return self.recovery_control.set_session_paused(
                {"pid": os.getpid(), "sessionId": WORKBENCH_SESSION_ID}, not enabled)
        future = self._recovery_executor.submit(save)
        def finished():
            if not future.done():
                self.after(100, finished)
                return
            try:
                future.result()
                message = "自动安全恢复已启用" if enabled else "自动安全恢复已暂停"
            except Exception as exc:
                self.auto_recovery_var.set(False)
                message = f"自动恢复状态未改变：{exc}"
            self.status_var.set(message)
            self.write_log(message)
        self.after(100, finished)

    def _auto_recovery_candidate(self):
        task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        if not task:
            task = load_current_context()
        task_id = canonical_task_id(task)
        if not task_id:
            return None
        records = load_project_download_statuses().get("items", {})
        record = records.get(task_id) if isinstance(records, dict) else None
        if not isinstance(record, dict):
            return None
        pipeline = str(record.get("pipelineStatus") or "").strip().lower()
        from resume_plan import resolve_resume_plan, verify_final_row
        date_value = str(task.get("productionDate") or task.get("date") or "").strip()
        stored = self.capcut_store.load_date(date_value) if date_value else {"tasks": []}
        verified_plan = resolve_resume_plan(
            task, stored.get("tasks") or [], verify_final=verify_final_row
        )
        if verified_plan.get("decision") in {"blocked", "completed"}:
            return None
        if verified_plan.get("step") == "step5":
            record = {**record, **verified_plan["row"], "childTaskId": verified_plan["childTaskId"],
                      "canonicalTaskId": task_id, "sourceVideoHash": verified_plan["sourceVideoHash"]}
            pipeline = "blocked_capcut" if str(record.get("status") or "") == "failed" else "capcut_waiting"
        raw_error_code = str(record.get("errorCode") or "").strip()
        # Aggregate failures are never safe to retry by themselves.  A
        # producer must persist the structured leaf cause first; otherwise an
        # automatic retry could repeat an unknown operation indefinitely.
        aggregate_errors = {
            "VOICE_BATCH_INCOMPLETE", "VOICE_CLIENT_LAUNCH_FAILED",
            "VOICE_START_FAILED", "CAPCUT_RECOVERY_FAILED",
        }
        error_code = raw_error_code
        if raw_error_code in aggregate_errors:
            error_code = str(
                record.get("underlyingErrorCode")
                or record.get("primaryErrorCode")
                or record.get("rootErrorCode")
                or ""
            ).strip()
            if not error_code:
                return {
                    "task": task, "record": record, "taskId": task_id,
                    "step": "step3" if raw_error_code.startswith("VOICE_") else "step5",
                    "stableKey": f"unverified|{task_id}|{'step3' if raw_error_code.startswith('VOICE_') else 'step5'}",
                    "childTaskId": "", "runGroupId": str(task.get("runGroupId") or record.get("runGroupId") or "").strip(),
                    "batchId": str(task.get("batchId") or record.get("batchId") or "").strip(),
                    "identityVerified": False,
                    "identityError": "recovery_identity_unverified: aggregate error has no structured leaf cause",
                    "errorCode": raw_error_code,
                }
        if pipeline in {"blocked_voice", "voice_failed", "voice_login_required"}:
            step, allowed = "step3", AUTO_RECOVERY_VOICE_ERRORS
        elif pipeline.startswith("blocked_capcut") or pipeline in {"capcut_failed", "capcut_stopped"}:
            step, allowed = "step5", AUTO_RECOVERY_CAPCUT_ERRORS
        else:
            return None
        if error_code not in allowed:
            return None
        if step == "step3":
            child_task_id = str(record.get("voiceTaskId") or record.get("audioTaskId") or "").strip()
        else:
            # A Step 5 budget is per output video.  Never inherit a stale
            # Step 3 voice child or fall back to the parent batch task.
            child_task_id = str(
                record.get("capcutTaskId") or record.get("outputTaskId")
                or record.get("videoTaskId") or record.get("childTaskId")
                or task.get("childTaskId") or ""
            ).strip()
        language = str(record.get("language") or task.get("language") or "").strip()
        identity_facts = {
            "step": step,
            "runGroupId": record.get("runGroupId") or task.get("runGroupId"),
            "batchId": record.get("batchId") or task.get("batchId"),
            "canonicalTaskId": record.get("canonicalTaskId") or task.get("canonicalTaskId") or task_id,
            "audioTaskId": child_task_id if step == "step3" else "",
            "outputTaskId": child_task_id if step == "step5" else "",
            "product": record.get("product") or record.get("material") or task.get("material"),
            "material": record.get("material") or task.get("material"),
            "language": language,
            "copyTextHash": record.get("copyTextHash") or task.get("copyTextHash"),
            "voiceContractId": record.get("voiceContractId") or record.get("voiceId") or task.get("voiceContractId") or task.get("voiceId"),
            "sourceVideoHash": record.get("sourceVideoHash") or record.get("sourceHash") or task.get("sourceVideoHash"),
            "legacySourceIdentity": record.get("generationIdentity") or record.get("videoPath") or child_task_id,
            "outputIdentity": record.get("outputPath") or record.get("output") or child_task_id,
        }
        if step == "step5" and not identity_facts.get("sourceVideoHash"):
            # Older formal handoff rows did not persist a source hash, but
            # they did persist an immutable generation identity and the
            # exact source path.  Prefer a real file hash when available so
            # recovery is still bound to the actual media; fall back to the
            # producer generation identity only when the source is gone, and
            # never use a parent batch id as the media identity.
            source_path = str(record.get("videoPath") or task.get("videoPath") or "").strip()
            try:
                if source_path and Path(source_path).is_file():
                    digest = hashlib.sha256()
                    with Path(source_path).open("rb") as source_handle:
                        for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    identity_facts["sourceVideoHash"] = digest.hexdigest()
            except (OSError, PermissionError):
                pass
            if not identity_facts.get("sourceVideoHash"):
                identity_facts["sourceVideoHash"] = str(
                    record.get("generationIdentity") or task.get("generationIdentity") or ""
                ).strip()
        if step == "step3" and (
            not child_task_id
            or not identity_facts.get("copyTextHash")
            or not identity_facts.get("voiceContractId")
        ):
            # The producer is one language-plan command.  Its pending rows do
            # not have copy hashes until CopySelector chooses their own copy;
            # bind recovery to the verified aggregate plan instead of the
            # already completed sibling language.
            identity_facts.update(self._voice_batch_recovery_facts(task, record))
        identity = resolve_recovery_identity(task, identity_facts)
        identity_error = "" if identity.get("verified") else str(identity.get("error") or "recovery_identity_unverified")
        stable = str(identity.get("stableRecoveryKey") or f"unverified|{task_id}|{step}")
        resolved_child_task_id = str(identity.get("childTaskId") or child_task_id).strip()
        stable_parts = (
            str(record.get("runGroupId") or task.get("runGroupId") or "").strip(),
            str(record.get("batchId") or task.get("batchId") or "").strip(),
            resolved_child_task_id,
            step,
        )
        failed_at = str(
            record.get("pipelineUpdatedAt") or record.get("updatedAt")
            or record.get("failedAt") or ""
        ).strip()
        failed_at = str(
            record.get("failedAt") or record.get("pipelineUpdatedAt") or record.get("updatedAt") or ""
        ).strip()
        failure_event_id = str(record.get("failureEventId") or "").strip()
        if not failure_event_id:
            failure_event_id = hashlib.sha256(
                f"{stable}|{error_code}|{failed_at}".encode("utf-8", errors="replace")
            ).hexdigest()[:24]
        return {"task": task, "record": record, "taskId": task_id, "childTaskId": resolved_child_task_id,
                 "language": language, "step": step,
                 "stableKey": stable if resolved_child_task_id else f"unverified|{task_id}|{step}",
                 "errorCode": error_code, "rawErrorCode": raw_error_code,
                 "identityVerified": not bool(identity_error), "identityError": identity_error,
                 "failureEventId": failure_event_id, "fingerprint": f"{stable}|{failure_event_id}", "failedAt": failed_at,
                 "runGroupId": stable_parts[0], "batchId": stable_parts[1]}

    def _auto_recovery_resources_busy(self, task_id, ignore_command_id=""):
        command = self._read_local_json_object(CONTINUE_COMMAND_FILE)
        if (
            str(command.get("commandId") or "") != str(ignore_command_id or "")
            and str(command.get("state") or "").lower() in {"requested", "accepted", "recovering", "running", "executing"}
        ):
            return True
        if self.download_process and self.download_process.poll() is None:
            # Popen can outlive child bookkeeping after a worker crash.
            # Cross-check the OS PID before reserving the global recovery slot.
            if _pid_is_alive(int(getattr(self.download_process, "pid", 0) or 0)):
                return True
            self.download_process = None
        if self.stitcher_controller is not None and self.stitcher_controller.is_running():
            return True
        if self.voice_batch_process and self.voice_batch_process.poll() is None:
            if _pid_is_alive(int(getattr(self.voice_batch_process, "pid", 0) or 0)):
                return True
            self.voice_batch_process = None
        # Recovery uses a read-only probe.  The ExampleBrand startup gate also mutates
        # its idle clock and pipeline state, so it must not run from a 30s
        # recovery check.
        exact_process_names = {
            "capcut.exe", "jianyingpro.exe", "batchvoiceclient.exe",
            "sttn.exe", "ffmpeg.exe", "ffmpeg", "fish-audio-batch-bridge.exe",
        }
        exact_command_names = {"runbatch.js", "download.py", "download.exe"}
        try:
            import psutil
            # Match executable/process semantics, never arbitrary substrings
            # of a full path or command line.  A helper installed under a
            # directory such as ``E:\Backup\Downloads`` used to make every
            # recovery look busy because the old check searched the entire
            # joined string for ``download``.  Keep the gate conservative for
            # unreadable process metadata, but do not classify unrelated
            # applications merely by their parent folder name.
            for process in psutil.process_iter(["name", "exe", "cmdline"]):
                try:
                    name = str(process.info.get("name") or "").casefold().strip()
                    exe_name = Path(str(process.info.get("exe") or "")).name.casefold()
                    cmdline = [str(x).casefold().strip() for x in (process.info.get("cmdline") or [])]
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return True
                if name in exact_process_names or exe_name in exact_process_names:
                    return True
                # Only inspect command-line basenames/explicit script names.
                # Do not match a directory component such as ``Downloads``.
                command_basenames = {Path(arg.strip('"')).name for arg in cmdline if arg}
                if command_basenames & exact_command_names:
                    return True
                if any("fish-audio-batch-bridge" in Path(arg.strip('"')).name for arg in cmdline if arg):
                    return True
        except ImportError:
            # Some portable workbench runtimes do not ship psutil, while
            # tasklist may be denied by the host policy.  Use the native
            # Toolhelp process snapshot first so an unavailable tasklist does
            # not turn every recovery into a false "resources busy" result.
            try:
                if os.name == "nt":
                    import ctypes.wintypes as wintypes

                    class _ProcessEntry32W(ctypes.Structure):
                        _fields_ = [
                            ("dwSize", wintypes.DWORD),
                            ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.c_void_p),
                            ("th32ModuleID", wintypes.DWORD),
                            ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD),
                            ("szExeFile", wintypes.WCHAR * 260),
                        ]

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
                    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
                    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
                    kernel32.Process32FirstW.restype = wintypes.BOOL
                    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
                    kernel32.Process32NextW.restype = wintypes.BOOL
                    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
                    invalid = ctypes.c_void_p(-1).value
                    if snapshot in (0, invalid):
                        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
                    try:
                        entry = _ProcessEntry32W()
                        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
                        names = set()
                        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                            while True:
                                names.add(str(entry.szExeFile or "").casefold())
                                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                                    break
                        if names & exact_process_names:
                            return True
                        return False
                    finally:
                        kernel32.CloseHandle(snapshot)
                raise OSError("native process snapshot unavailable")
            except Exception:
                # Final fallback for non-Windows or hosts that deny both
                # native enumeration and tasklist remains conservative.
                try:
                    result = subprocess.run(
                        ["tasklist", "/FO", "CSV", "/NH"],
                        capture_output=True, text=True, timeout=2,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if result.returncode != 0:
                        return True
                    process_names = {
                        str(row.split(",", 2)[1]).strip('"').casefold()
                        for row in (result.stdout or "").splitlines()
                        if row.count(",") >= 1
                    }
                    return bool(process_names & exact_process_names)
                except Exception:
                    return True
        record = self.capcut_launch_records.get(str(task_id or "")) or {}
        process = record.get("process")
        return bool(process is not None and process.poll() is None)

    @staticmethod
    def _auto_recovery_request_from_entry(entry):
        """Rebuild the exact command after a client restart; never mint a new ID."""
        return {
            "version": 2,
            "commandId": str(entry.get("recoveryCommandId") or ""),
            "recoveryCommandId": str(entry.get("recoveryCommandId") or ""),
            "source": "automatic_recovery",
            "state": "requested",
            "phase": "automatic_recovery_redispatch",
            "taskId": str(entry.get("taskId") or ""),
            "childTaskId": str(entry.get("childTaskId") or ""),
            "stableRecoveryKey": str(entry.get("stableRecoveryKey") or ""),
            "step": str(entry.get("step") or ""),
            "runGroupId": str(entry.get("runGroupId") or ""),
            "batchId": str(entry.get("batchId") or ""),
            "automaticAttemptCount": int(entry.get("automaticAttemptCount") or 0),
            "failureEventId": str(entry.get("failureEventId") or ""),
            "errorCode": str(entry.get("lastErrorCode") or entry.get("primaryError") or ""),
            "requestedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _step12_recovery_observation(self, command):
        """Judge an executed Step 1/2 command by the download it resumed.

        Step 1/2 runs inside this workbench, so its receipt has no producer
        process or CapCut status file.  Without this check every such command
        became reconciliation_required within seconds and then held back every
        later Step 1/2 Safe Continue as a same-step blocker.
        """
        task_id = str(command.get("canonicalTaskId") or command.get("taskId") or "").strip()
        if not task_id:
            return None
        active = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        process = self.__dict__.get("download_process")
        if (process is not None and canonical_task_id(active) == task_id and process.poll() is None
                and _pid_is_alive(int(getattr(process, "pid", 0) or 0))):
            return {"status": "alive", "processObservation": "workbench_download", "pid": process.pid}
        record = load_project_download_statuses().get("items", {}).get(task_id)
        if not isinstance(record, dict):
            return None
        status = str(record.get("status") or "").strip().lower()
        pipeline = str(record.get("pipelineStatus") or "").strip().lower()
        text = str(record.get("stateText") or record.get("pipelineStateText") or "").strip()
        observation = {"processObservation": "download_record", "downloadStatus": status, "pipelineStatus": pipeline}
        if pipeline not in STEP12_PIPELINE_STATUSES:
            return {**observation, "status": "completed", "outcome": "step12_completed"}
        if "blocked_missing_links" in (pipeline, status):
            return {**observation, "status": "completed", "outcome": "missing_links",
                    "reason": text or "第 1/2 步已结束，等待补链"}
        if pipeline in {"step12_interrupted", "blocked_download_runtime", "blocked_step12_resume"}                 or status in {"interrupted", "blocked_download_runtime"}:
            return {**observation, "status": "failed", "outcome": "interrupted",
                    "reason": text or "第 1/2 步已中断，可再次安全继续"}
        if pipeline in {"downloading", "step12_resuming"} or status == "running":
            try:
                updated = datetime.fromisoformat(str(record.get("updatedAt") or ""))
                age = (datetime.now(timezone.utc) - updated).total_seconds()
            except (TypeError, ValueError):
                age = None
            if age is not None and age < STEP12_REPORT_GRACE_SECONDS:
                return {**observation, "status": "alive"}
            return {**observation, "status": "failed", "outcome": "interrupted",
                    "reason": "第 1/2 步下载会话已不在运行，可再次安全继续"}
        if status == "completed":
            return {**observation, "status": "completed", "outcome": "step12_completed"}
        return None

    def _close_step12_continue_mailbox(self, reconciled):
        """End the Dashboard command of a Step 1/2 resume that stopped.

        A resume that reaches Step 3 keeps its mailbox for the later steps.
        One that stopped at missing links or an interruption is otherwise left
        "recovering", which holds back every later Safe Continue request.
        """
        events = []
        for command in reconciled or []:
            if (not isinstance(command, dict) or command.get("ignored")
                    or command.get("step") != "step12"
                    or command.get("state") not in {"completed", "failed"}):
                continue
            receipt = command.get("executionReceipt") if isinstance(command.get("executionReceipt"), dict) else {}
            outcome = str(receipt.get("outcome") or "")
            if outcome not in {"missing_links", "interrupted"}:
                continue
            mailbox = self._read_local_json_object(CONTINUE_COMMAND_FILE)
            if (str(mailbox.get("commandId") or "") != str(command.get("commandId") or "")
                    or str(mailbox.get("state") or "") not in {"requested", "accepted", "recovering", "running"}):
                continue
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            reason = str(receipt.get("reason") or "")
            mailbox.update({"state": "blocked", "phase": f"step12_{outcome}", "message": reason,
                            "finishedAt": now, "lastHeartbeat": now})
            self._write_continue_command(mailbox)
            events.append(f"安全继续已结束：{reason}")
        return events

    def _live_capcut_launch_identity(self, command):
        """Identity of the CapCut client this workbench launched for the
        command's task, while that process is still running; else {}."""
        if str(command.get("step") or "") != "step5":
            return {}
        task_id = str(command.get("taskId") or command.get("canonicalTaskId") or "").strip()
        records = self.__dict__.get("capcut_launch_records")
        record = records.get(task_id) if isinstance(records, dict) and task_id else None
        if not isinstance(record, dict):
            return {}
        process = record.get("process")
        try:
            if process is None or process.poll() is not None:
                return {}
            pid = int(record.get("pid") or getattr(process, "pid", 0) or 0)
        except (OSError, TypeError, ValueError):
            return {}
        return capture_process_identity(pid, role="capcut-batch-client") if pid > 0 else {}

    def _live_voice_launch_identity(self, task_id):
        """Bind only the producer this Workbench launched for this exact task."""
        identity = self.__dict__.get("voice_launch_identity") or {}
        process = self.__dict__.get("voice_batch_process")
        owner = str(identity.get("canonicalTaskId") or identity.get("taskId") or "")
        if (not task_id or owner != task_id or process is None or process.poll() is not None):
            return {}
        return capture_process_identity(int(process.pid), role="fish-voice-producer")

    def _recovery_process_observation(self, command):
        """Return a fact-based observation for one pending command."""
        mailbox = self._read_local_json_object(CONTINUE_COMMAND_FILE)
        command_id = str(command.get("commandId") or "")
        mailbox_id = str(mailbox.get("commandId") or mailbox.get("recoveryCommandId") or "")
        mailbox_state = str(mailbox.get("state") or "").lower()
        if (command.get("step") == "step12"
                and str(command.get("state") or "") not in {"claimed", "dispatched", "accepted"}):
            observation = self._step12_recovery_observation(command)
            if observation:
                return observation
        if command.get("step") == "step3":
            live = self._live_voice_launch_identity(str(command.get("taskId") or command.get("canonicalTaskId") or ""))
            if live and observe_process_identity(live) == "matched_alive":
                return {"status": "alive", "processObservation": "matched_alive", "pid": live["pid"]}
            from resume_plan import adopt_completed_voice
            active = self.active_external_task if isinstance(self.active_external_task, dict) else {}
            adopted = adopt_completed_voice(active, command,
                self._read_local_json_object(self._fish_bridge_runtime_file()),
                self._read_local_json_object(self._voice_handoff_file()))
            if adopted:
                return {**adopted, "adoptedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            from fish_reconciliation import observe_failed_fish
            fish_observation = observe_failed_fish(
                command, mailbox,
                self._read_local_json_object(self._fish_bridge_runtime_file()),
                self._read_local_json_object(self._voice_handoff_file()),
            )
            if fish_observation.get("status") in {"failed", "completed"}:
                return fish_observation
        receipt = command.get("executionReceipt") if isinstance(command.get("executionReceipt"), dict) else {}
        identity = receipt.get("processIdentity") if isinstance(receipt.get("processIdentity"), dict) else {}
        if not identity:
            # Safe Continue often returns before the CapCut client exists
            # ("existing CapCut work is being observed"); the queue launches
            # it a moment later, so the receipt has no producer and the
            # command read as reconciliation_required for the whole run
            # (2026-10-11 15:52-16:13).  Use the client this workbench itself
            # launched for the task while it is still running.
            identity = self._live_capcut_launch_identity(command)
        # A live workbench is not evidence of a live Fish/CapCut producer.
        # Mailbox terminal strings are likewise transport, not artifact QC.
        process_status = observe_process_identity(identity)
        if process_status == "matched_alive":
            return {"status": "alive", "processObservation": process_status, "pid": identity.get("pid")}
        if process_status == "pid_reused":
            return {"status": "unknown", "processObservation": process_status, "reason": "PID已被其他进程复用"}
        if process_status == "unverifiable" and identity:
            return {"status": "unknown", "processObservation": process_status, "reason": "无法核验进程启动身份"}
        if (str(command.get('state') or '') in {'claimed', 'dispatched'}
                and not command.get('acceptedAt') and not receipt
                and (not mailbox_id or (mailbox_id == command_id and mailbox_state == 'requested'))):
            return {"status": "redispatch"}

        # A CapCut client can finish with a durable terminal status before
        # the recovery controller receives its execution receipt (for example
        # when the client closes itself after a guarded draft-reopen failure).
        # Treating that state as forever ``unknown`` strands the command in
        # reconciliation_required and blocks the next Safe Continue forever.
        # The status file is accepted only when all durable identities bound to
        # this command agree; it is never inferred from a stale process flag.
        status_file = str(
            receipt.get("statusFile")
            or (mailbox.get("boundStatusFile") if mailbox_id == command_id else "")
            or ""
        ).strip()
        if not status_file:
            # A client can finish before the controller receives the receipt.
            # In that race the command has no statusFile, but the immutable
            # date-scoped CapCut handoff row still records the task's status
            # file.  Resolve only an exact task/child identity; never choose
            # the newest arbitrary status file or infer success from time.
            command_task = str(
                command.get("taskId") or command.get("canonicalTaskId") or ""
            ).strip()
            command_child = str(command.get("childTaskId") or "").strip()
            if command_task:
                try:
                    for date_value in self.capcut_store.dates():
                        payload = self.capcut_store.load_date(str(date_value))
                        for row in (payload.get("tasks") or []) if isinstance(payload, dict) else []:
                            if not isinstance(row, dict):
                                continue
                            if str(row.get("remotePipelineTaskId") or "").strip() != command_task:
                                continue
                            if command_child and str(row.get("taskId") or "").strip() != command_child:
                                continue
                            candidate = str(row.get("statusFile") or "").strip()
                            if candidate:
                                status_file = candidate
                                break
                        if status_file:
                            break
                except Exception:
                    # Reconciliation must remain conservative if the handoff
                    # store cannot be read; the command stays blocked below.
                    status_file = ""
        if status_file:
            try:
                status_path = Path(status_file)
                status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError):
                status = None
            if isinstance(status, dict) and not bool(status.get("running")):
                command_task = str(
                    command.get("taskId")
                    or command.get("canonicalTaskId")
                    or ""
                ).strip()
                status_task = str(
                    status.get("remotePipelineTaskId")
                    or status.get("taskId")
                    or ""
                ).strip()
                command_manifest = str(
                    command.get("manifestId")
                    or mailbox.get("boundManifestId")
                    or ""
                ).strip()
                command_attempt = str(
                    command.get("attemptId")
                    or mailbox.get("boundAttemptId")
                    or ""
                ).strip()
                manifest_matches = not command_manifest or str(
                    status.get("manifestId") or ""
                ).strip() == command_manifest
                attempt_matches = not command_attempt or str(
                    status.get("attemptId") or ""
                ).strip() == command_attempt
                task_matches = not command_task or not status_task or status_task == command_task
                if task_matches and manifest_matches and attempt_matches:
                    task_states = {
                        str(item.get("state") or item.get("status") or "")
                        .strip().lower()
                        for item in status.get("tasks") or []
                        if isinstance(item, dict)
                    }
                    failed_states = {
                        "failed", "stopped", "validation_failed",
                        "manual_review", "completed_with_errors",
                    }
                    if int(status.get("failed") or 0) > 0 or task_states & failed_states:
                        return {
                            "status": "failed",
                            "processObservation": "exited",
                            "statusFile": status_file,
                            "reason": str(status.get("message") or "CapCut 终态包含失败任务"),
                            "manifestId": status.get("manifestId"),
                            "attemptId": status.get("attemptId"),
                        }
                    if task_states and task_states <= {"completed"}:
                        return {
                            "status": "completed",
                            "processObservation": "exited",
                            "statusFile": status_file,
                            "manifestId": status.get("manifestId"),
                            "attemptId": status.get("attemptId"),
                        }
                    stopped = _capcut_stopped_with_error(status, command.get("claimedAt"))
                    if stopped:
                        # The client gave up before producing anything (it
                        # could not acquire the task draft root, 2026-10-11
                        # 15:13) and left the task Waiting with an error code.
                        # Close the command as failed instead of stranding it
                        # as unknown, so the next Safe Continue is accepted.
                        return {
                            "status": "failed",
                            "processObservation": "exited",
                            "statusFile": status_file,
                            "reason": stopped,
                            "manifestId": status.get("manifestId"),
                            "attemptId": status.get("attemptId"),
                        }
        # A dead process with no terminal receipt is not safe to duplicate.
        return {"status": "unknown", "processObservation": process_status, "reason": "执行身份已退出且结果未确认"}

    def _poll_automatic_recovery_v4(self, enabled):
        """Drive the shared controller; never mutate budgets outside its lock."""
        events = []
        if "recovery_control" not in self.__dict__:
            self.recovery_control = RecoveryControl(
                AUTO_RECOVERY_STATE_FILE, AUTO_RECOVERY_LOCK_FILE, CONTINUE_COMMAND_FILE
            )
        if self._auto_recovery_busy:
            return events
        # Dashboard requests live in the independent recovery-requests queue.
        # Resolve their stable step/child identity before delivery so an old
        # unresolved command for another step cannot strand a valid request.
        try:
            self._enrich_pending_dashboard_recovery_requests()
        except Exception as exc:
            events.append(f"安全继续请求身份预检暂停：{type(exc).__name__}: {exc}")
        self.recovery_control.deliver_next_request()
        # Reconcile durable commands even while automatic recovery is paused;
        # pausing forbids new dispatches, but it must not leave an old command
        # permanently marked running.  This is read-only/terminal bookkeeping
        # unless a previously claimed command can be safely re-delivered.
        try:
            reconciliation = self.recovery_control.reconcile_pending_commands(
                self._recovery_process_observation,
                allow_redispatch=enabled,
                resource_guard=lambda command: self._auto_recovery_resources_busy("", str(command.get("commandId") or "")),
            )
            events.extend(self._close_step12_continue_mailbox(reconciliation))
            if any(item.get("status") == "reconciliation_required" for item in reconciliation if isinstance(item, dict)):
                events.append("自动恢复已暂停：存在待核对命令")
                return events
        except RecoveryControlError as exc:
            events.append(f"恢复命令对账暂停：{exc.code}: {exc}")
            return events
        if not enabled:
            return events
        candidate = self._auto_recovery_candidate()
        if not candidate:
            return events
        identity = {
            "verified": bool(candidate.get("identityVerified")),
            "error": candidate.get("identityError"),
            "stableRecoveryKey": candidate.get("stableKey"),
            "step": candidate.get("step"),
            "runGroupId": candidate.get("runGroupId"),
            "batchId": candidate.get("batchId"),
            "canonicalTaskId": candidate.get("task", {}).get("canonicalTaskId") or candidate.get("taskId"),
            "childTaskId": candidate.get("childTaskId"),
        }
        if not identity["verified"]:
            events.append("自动恢复已暂停：recovery_identity_unverified")
            events.append(str(candidate.get("identityError") or "recovery_identity_unverified"))
            return events
        failure = {
            "failureEventId": candidate.get("failureEventId"),
            "failureFingerprint": candidate.get("fingerprint"),
            "failedAt": candidate.get("failedAt"),
            "errorCode": candidate.get("errorCode"),
        }
        try:
            entry = self.recovery_control.observe_failure(identity, failure)
            state = self.recovery_control.status()
            latest = state.get("entries", {}).get(identity["stableRecoveryKey"], entry)
            if str(latest.get("commandState") or "") in {"claimed", "dispatched", "accepted", "executing", "running", "reconciling", "reconciliation_required"}:
                return events
            claim = self.recovery_control.claim_recovery(
                identity,
                "automatic",
                failure=failure,
                resource_guard=lambda: self._auto_recovery_resources_busy(candidate["taskId"]),
            )
            if claim.get("accepted"):
                command = claim.get("command") or {}
                events.append(f"自动恢复第{candidate['step']}：第{command.get('automaticAttemptCount', 0)}/2次，已派发")
                events.append(f"已派发统一恢复命令 {command.get('commandId')}：{candidate.get('childTaskId')}")
            elif claim.get("code") not in {"recovery_not_due", "recovery_paused", "recovery_budget_exhausted", "recovery_resources_busy"}:
                events.append(f"自动恢复暂缓：{claim.get('code')} {claim.get('message', '')}")
        except RecoveryControlError as exc:
            events.append(f"自动恢复控制器暂停：{exc.code}: {exc}")
        except Exception as exc:
            events.append(f"自动恢复控制器异常：{type(exc).__name__}: {exc}")
        return events

    def _enrich_pending_dashboard_recovery_requests(self):
        """Bind queued Dashboard transport requests to verified producer facts.

        Dashboard writes transport-only requests and deliberately does not
        overwrite the production mailbox.  Before the shared controller can
        safely deliver across a stale command from another step, this worker
        fills the immutable stable identity from the current task, project
        status and date-scoped CapCut handoff.  Ambiguous requests remain
        queued and are never guessed into a parent-level retry.
        """
        request_dir = AUTO_RECOVERY_STATE_FILE.parent / "recovery-requests"
        try:
            paths = list(request_dir.glob("*.json"))
        except OSError:
            return
        candidates = []
        for path in paths:
            try:
                request = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError, UnicodeError):
                continue
            if isinstance(request, dict) and str(request.get("state") or "").lower() == "requested":
                candidates.append((str(request.get("requestedAt") or ""), path, request))
        candidates.sort(key=lambda item: (item[0] or "9999", item[1].name))
        if not candidates:
            return

        status_items = load_project_download_statuses().get("items", {})
        for _, path, request in candidates:
            command_id = str(request.get("commandId") or "").strip()
            task_id = str(request.get("taskId") or "").strip()
            if not command_id or not task_id:
                continue
            if (str(request.get("step") or "").strip()
                    and str(request.get("stableRecoveryKey") or "").strip()):
                continue
            task = load_task_record(task_id)
            if not isinstance(task, dict) or canonical_task_id(task) != task_id:
                current = load_current_context()
                task = current if isinstance(current, dict) and canonical_task_id(current) == task_id else None
            if not isinstance(task, dict):
                continue
            expected_pairs = (
                ("batchId", request.get("batchId"), task.get("batchId")),
                ("runGroupId", request.get("runGroupId"), task.get("runGroupId")),
                ("productionDate", request.get("productionDate"), task.get("productionDate") or task.get("date")),
            )
            if any(expected and actual and str(expected).strip() != str(actual).strip()
                   for _, expected, actual in expected_pairs):
                continue
            live_record = status_items.get(task_id, {}) if isinstance(status_items, dict) else {}
            if not isinstance(live_record, dict):
                live_record = {}
            stale_producer_snapshot = False
            for field in ("manifestId", "attemptId"):
                requested_value = str(request.get(field) or "").strip()
                current_value = str(live_record.get(field) or "").strip()
                if requested_value and current_value and requested_value != current_value:
                    stale_producer_snapshot = True
                    break
            if stale_producer_snapshot:
                continue
            identity = self._continue_recovery_identity(task)
            if not identity.get("verified") or identity.get("completed"):
                continue
            patch = {
                "step": identity.get("step"),
                "stableRecoveryKey": identity.get("stableRecoveryKey"),
                "canonicalTaskId": identity.get("canonicalTaskId") or task_id,
                "childTaskId": identity.get("childTaskId") or "",
                "runGroupId": identity.get("runGroupId") or request.get("runGroupId") or "",
                "batchId": identity.get("batchId") or request.get("batchId") or "",
            }
            if not patch["step"] or not patch["stableRecoveryKey"]:
                continue
            self.recovery_control.amend_request_identity(command_id, patch, task_id=task_id)


    def poll_automatic_recovery(self):
        # A single worker owns recovery disk/lock/probe work. The Tk callback
        # only transfers immutable options and consumes completed messages.
        from concurrent.futures import ThreadPoolExecutor
        if "_recovery_executor" not in self.__dict__:
            self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recovery-control")
            self._recovery_poll_future = None
        future = self._recovery_poll_future
        if future is not None:
            if not future.done():
                return
            try:
                for message in future.result():
                    self.status_var.set(message)
                    self.write_log(message)
            except Exception as exc:
                self.write_log(f"恢复后台检查暂停：{type(exc).__name__}: {exc}")
            self._recovery_poll_future = None
        self._recovery_poll_future = self._recovery_executor.submit(
            self._poll_automatic_recovery_v4, bool(self.auto_recovery_var.get())
        )

    def poll_task_inbox(self):
        self.refresh_task_inbox(False)
        self.auto_start_next_external_task()
        try:
            self.poll_prefetch_next_project()
        except Exception as exc:
            # Prefetch is an optimisation; it must never stop the queue poll.
            self.write_log(f"预取下一项目暂停：{type(exc).__name__}: {exc}")
        self.poll_automatic_recovery()
        self.after(5000, self.poll_task_inbox)

    @staticmethod
    def _is_remote_auto_sequence_task(task):
        """Return whether a receiver task may be started without local clicks."""
        mode = str(task.get("startMode") or "")
        if mode in {"auto_start_first", "auto_start_sequential"}:
            return True
        if str(task.get("batchKind") or "") != "examplebrand-natural-flow":
            return False
        task_id = str(task.get("taskId") or "").strip()
        intent = ExampleBrand_CONTROL_DIR / f"{task_id}.json"
        try:
            payload = json.loads(intent.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        identity = (
            str(payload.get("runGroupId") or ""), str(payload.get("batchId") or ""),
            str(payload.get("canonicalTaskId") or payload.get("taskId") or ""),
        )
        expected = (
            str(task.get("runGroupId") or ""), str(task.get("batchId") or ""), canonical_task_id(task),
        )
        return identity == expected and str(payload.get("startPolicy") or "") == "auto_when_idle" and bool(payload.get("enabled"))

    def _examplebrand_global_idle(self, task, status_items):
        is_examplebrand = str(task.get("batchKind") or "") == "examplebrand-natural-flow"
        task_id = canonical_task_id(task)

        def not_idle():
            # The observation clock is deliberately reset by every busy sample.
            # Sixty seconds therefore means continuously idle, not merely sixty
            # seconds since the receiver created the start intent.
            self.__dict__.setdefault("_examplebrand_idle_since", {}).pop(task_id, None)
            return False

        if getattr(self, "download_process", None) and self.download_process.poll() is None:
            return not_idle()
        if getattr(self, "voice_batch_process", None) and self.voice_batch_process.poll() is None:
            return not_idle()
        controller = getattr(self, "stitcher_controller", None)
        if controller is not None and controller.is_running():
            return not_idle()
        if getattr(self, "capcut_process", None) and self.capcut_process.poll() is None:
            return not_idle()

        # A manual Safe Continue owns the same production lock.  A ExampleBrand auto
        # claim must never race an accepted or executing command.
        command = self._read_local_json_object(CONTINUE_COMMAND_FILE)
        if str(command.get("state") or "").lower() in {
            "requested", "accepted", "acknowledged", "executing", "running",
        }:
            return not_idle()

        # Also guard processes that survived a workbench restart and therefore
        # are no longer represented by the in-memory Popen fields.
        try:
            import psutil
            configured_capcut = Path(str(self.vars["capcut_client"].get() or "")).resolve()
            configured_capcut_text = str(configured_capcut).lower()
            for process in psutil.process_iter(["name", "exe", "cmdline"]):
                try:
                    cmdline = [str(part) for part in (process.info.get("cmdline") or [])]
                    process_name = str(process.info.get("name") or "").lower()
                    executable = str(process.info.get("exe") or "").strip()
                    if not executable and cmdline:
                        # On Windows psutil may not expose exe for a protected
                        # process; the first command-line token is still a
                        # useful identity when it is an absolute path.
                        first = cmdline[0].strip().strip('"')
                        if Path(first).suffix.lower() == ".exe":
                            executable = first
                    identity = " ".join([process_name] + cmdline).lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

                # A CapCutBatchClient from an isolated acceptance directory is
                # not a production lease.  The previous name-only check made
                # such a stale test process block every ExampleBrand job indefinitely.
                # Production CapCut clients are accepted only when their
                # executable resolves to the configured client path; unknown
                # paths remain conservative blockers when identity is hidden.
                if "capcutbatchclient" in identity:
                    executable_text = str(Path(executable).resolve()).lower() if executable else ""
                    if executable_text and executable_text != configured_capcut_text:
                        continue
                    if not executable_text and configured_capcut_text not in identity:
                        return not_idle()
                    return not_idle()

                if any(name in identity for name in (
                    "jianyingpro.exe", "capcut.exe",
                    "fish-audio-batch-bridge", "batchvoiceclient",
                )):
                    return not_idle()
        except ImportError:
            pass

        executable = Path(str(self.vars["capcut_client"].get() or ""))
        lease_path = executable.parent / "data" / "draft-root-lease.json"
        if lease_path.is_file():
            lease = self._read_local_json_object(lease_path)
            release_state = str(
                lease.get("ReleaseState") or lease.get("releaseState") or
                lease.get("State") or lease.get("state") or ""
            ).lower()
            if release_state not in {"released", "archived", "completed"}:
                return not_idle()
        current = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        current_id = canonical_task_id(current)
        if current_id and current_id != task_id:
            state = status_items.get(current_id) if isinstance(status_items, dict) else None
            if not isinstance(state, dict) or str(state.get("pipelineStatus") or "") not in QUEUE_RELEASED_PIPELINE_STATUSES:
                return not_idle()
        # The active-task check alone is not enough once a parked project is
        # resumed by its replenishment: the active task may then be the
        # finished replenishment child while the parent still runs Step 3-5.
        # Any other started project of the same task box that is neither
        # finished nor parked keeps the shared resources busy.
        batch_id = str(task.get("batchId") or "").strip()
        if batch_id and isinstance(status_items, dict):
            for other_id, record in status_items.items():
                if (not isinstance(record, dict) or other_id == task_id
                        or str(record.get("batchId") or "").strip() != batch_id):
                    continue
                pipeline_status = str(record.get("pipelineStatus") or "")
                legacy_completed = not pipeline_status and str(record.get("status") or "") == "completed"
                if pipeline_status not in QUEUE_RELEASED_PIPELINE_STATUSES and not legacy_completed:
                    return not_idle()
        # Formal projects share the same Fish/CapCut/resource gate, but do
        # not use ExampleBrand's 60-second auto-start eligibility contract.
        if not is_examplebrand:
            return True
        intent_path = ExampleBrand_CONTROL_DIR / f"{str(task.get('taskId') or '').strip()}.json"
        try:
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            eligible_at = datetime.fromisoformat(str(intent.get("eligibleAfter") or "").replace("Z", "+00:00"))
            if datetime.now().astimezone() < eligible_at:
                return not_idle()
        except (OSError, ValueError, TypeError):
            return not_idle()
        try:
            validate_static_resources(task, APP_DIR.parent)
        except ExamplebrandProductionError as exc:
            self._set_remote_pipeline_status(
                canonical_task_id(task), "examplebrand_resources_blocked", str(exc),
                error_code=exc.error_code,
            )
            return not_idle()
        idle_since = self.__dict__.setdefault("_examplebrand_idle_since", {}).setdefault(
            task_id, time.monotonic()
        )
        if time.monotonic() - idle_since < 60.0:
            self._set_remote_pipeline_status(
                task_id, "waiting_workbench_idle", "工作台资源已空闲，连续稳定观察60秒后自动领取ExampleBrand",
            )
            return False
        return True

    def _previous_remote_item_finished(self, task, status_items):
        """Only advance a batch after its immediately preceding project is final.

        A project waiting for missing links counts as parked: it stopped after
        Step 1/2 and holds no Fish/CapCut resource, so the next project may
        start (user decision 2026-10-08; a gap once idled the queue 5.5 h).
        Its replenishment resumes it once the pipeline is idle again, see
        ``_examplebrand_global_idle``.
        """
        try:
            sequence = int(task.get("batchSequence") or 1)
        except (TypeError, ValueError):
            sequence = 1
        if sequence <= 1:
            return True
        batch_id = str(task.get("batchId") or "").strip()
        if not batch_id:
            return False
        predecessor = sequence - 1
        for record in status_items.values():
            if not isinstance(record, dict):
                continue
            try:
                record_sequence = int(record.get("batchSequence") or 0)
            except (TypeError, ValueError):
                continue
            if str(record.get("batchId") or "") != batch_id or record_sequence != predecessor:
                continue
            pipeline_status = str(record.get("pipelineStatus") or "").strip()
            # New remote jobs wait for Step 5 final output. Older records did
            # not have pipelineStatus, so retain their historic recovery path.
            if pipeline_status in QUEUE_RELEASED_PIPELINE_STATUSES or (not pipeline_status and str(record.get("status") or "") == "completed"):
                return True
        return False

    def _previous_run_group_child_finished(self, task, status_items):
        """Keep formal and ExampleBrand child batches of one run group sequential."""
        run_group = str(task.get("runGroupId") or "").strip()
        batch_kind = str(task.get("batchKind") or "formal").strip()
        if not run_group or batch_kind != "examplebrand-natural-flow":
            return True
        formal_tasks = [
            candidate for _path, candidate in list_pending_tasks()
            if str(candidate.get("runGroupId") or "").strip() == run_group
            and str(candidate.get("batchKind") or "formal").strip() == "formal"
        ]
        if not formal_tasks:
            return True
        for candidate in formal_tasks:
            candidate_id = str(candidate.get("taskId") or "").strip()
            record = status_items.get(candidate_id)
            if not isinstance(record, dict):
                return False
            pipeline_status = str(record.get("pipelineStatus") or "").strip()
            if pipeline_status != "completed" and not (
                not pipeline_status and str(record.get("status") or "") == "completed"
            ):
                return False
        return True

    def auto_start_next_external_task(self):
        """Start the next eligible item of a remote task box, sequentially.

        The historic mode name ``auto_start_first`` is intentionally accepted:
        already-installed laptop submitters use it.  Its behaviour is now a
        safe ordered queue: first item starts immediately; every following item
        starts after the preceding project's final Dashboard status is saved.
        """
        if self.download_process and self.download_process.poll() is None:
            return
        if self.stitcher_controller is not None and self.stitcher_controller.is_running():
            return
        if self._prefetch_running():
            # The next item starts once its prefetch has finished, and then
            # skips every output the prefetch already verified.
            return
        registry = load_project_download_statuses()
        status_items = registry.get("items") if isinstance(registry, dict) else {}
        if not isinstance(status_items, dict):
            status_items = {}
        for label, (_path, task) in self.inbox_tasks.items():
            if not self._is_remote_auto_sequence_task(task):
                continue
            task_id = str(task.get("taskId") or "").strip()
            if not task_id or task_id in self.auto_start_claimed_task_ids:
                continue
            if not self._previous_remote_item_finished(task, status_items):
                continue
            if not self._previous_run_group_child_finished(task, status_items):
                continue
            if not self._examplebrand_global_idle(task, status_items):
                continue
            self.inbox_task_var.set(label)
            self.write_log(
                f"收到远程顺序任务：第 {task.get('batchSequence') or 1} 项 "
                f"{task.get('project')} / {task.get('material')}，正在自动启动。"
            )
            if self.import_external_task(auto_start=True):
                self.auto_start_claimed_task_ids.add(task_id)
            return

    # Keep the old internal name for any extension scripts that still call it.
    def auto_start_first_external_task(self):
        self.auto_start_next_external_task()

    # ---- Plan item P2: prefetch the next project's Step 1/2 ----------------
    # While the current project is in Step 3-5 (Fish, stitching, CapCut), the
    # next queued project of the same task box is downloaded and de-subtitled
    # by its own bridge process into its own directory and checkpoint.  When
    # the queue later activates that project, the normal bridge run skips every
    # output the checkpoint already verified.  Nothing here touches the visible
    # download session, the project status registry or the Dashboard relay.

    def _prefetch_enabled(self):
        variables = getattr(self, "vars", None)
        var = variables.get("prefetch_next_project") if isinstance(variables, dict) else None
        value = str(var.get() if var is not None else "1").strip().lower()
        return value not in {"0", "false", "off", "no", "否", "关"}

    @staticmethod
    def _prefetch_state_path(task_id):
        return DOWNLOAD_PREFETCH_DIR / f"{task_id}.json"

    def _read_prefetch_state(self, task_id):
        if not task_id:
            return {}
        return self._read_local_json_object(self._prefetch_state_path(task_id))

    def _prefetch_running(self):
        """Return the task ID of a live prefetch bridge, or ""; close dead ones."""
        try:
            paths = sorted(DOWNLOAD_PREFETCH_DIR.glob("*.json"))
        except OSError:
            return ""
        running = ""
        for path in paths:
            if path.name.endswith(".request.json"):
                continue
            state = self._read_local_json_object(path)
            if str(state.get("state") or "") != "running":
                continue
            task_id = str(state.get("taskId") or "")
            process = self.__dict__.get("prefetch_process")
            exit_code = None
            try:
                pid = int(state.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if process is not None and int(getattr(process, "pid", 0) or 0) == pid:
                exit_code = process.poll()
                alive = exit_code is None
            else:
                # A bridge started before a workbench restart keeps running.
                observation = observe_process_identity(state.get("processIdentity"))
                alive = observation == "matched_alive" or (
                    observation == "unverifiable" and pid > 0 and _pid_is_alive(pid)
                )
            if alive:
                running = running or task_id
                continue
            self._finish_prefetch(state, exit_code)
        return running

    def _finish_prefetch(self, state, exit_code):
        task_id = str(state.get("taskId") or "")
        done = completed_download_checkpoint_count({"taskId": task_id})
        state.update({
            "state": "finished",
            "finishedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "exitCode": exit_code,
            "completedOutputs": done,
        })
        try:
            self._atomic_write_json(self._prefetch_state_path(task_id), state)
        except OSError as exc:
            self.write_log(f"保存预取状态失败：{exc}")
            return
        process = self.__dict__.get("prefetch_process")
        if process is not None and int(getattr(process, "pid", 0) or 0) == int(state.get("pid") or 0):
            self.prefetch_process = None
        self.write_log(
            f"预取结束：第 {state.get('batchSequence')} 项 {state.get('project')} / {state.get('material')}，"
            f"已完成 {done}/{state.get('urlCount')} 条；其余在该项目开始时照常处理。"
        )

    def _prefetch_owns_directory(self, download_path):
        """Whether every video in the directory came from this task's prefetch.

        The prefetch only starts on a directory without videos, and the
        directory is specific to date/project/material, so files found there
        after it finished are this task's own outputs and intermediates.
        """
        task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        state = self._read_prefetch_state(str(task.get("taskId") or "").strip())
        if str(state.get("state") or "") != "finished" or not state.get("directoryWasEmpty"):
            return False
        try:
            return os.path.normcase(str(Path(state.get("downloadPath") or "").resolve())) == \
                os.path.normcase(str(Path(download_path).resolve()))
        except (OSError, ValueError):
            return False

    def _prefetch_candidate(self):
        """Return (task, directory) of the next queued item, or None."""
        active = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        active_id = str(active.get("taskId") or "").strip()
        if (not active_id or not self._is_remote_auto_sequence_task(active)
                or str(active.get("batchKind") or "") == "examplebrand-natural-flow"
                or active.get("isReplenishment")):
            return None
        if self.download_process and self.download_process.poll() is None:
            return None
        registry = load_project_download_statuses().get("items", {})
        if not isinstance(registry, dict):
            return None
        record = registry.get(canonical_task_id(active)) or {}
        pipeline = str(record.get("pipelineStatus") or "").strip().lower()
        if pipeline in STEP12_PIPELINE_STATUSES or pipeline == "completed":
            return None
        batch_id = str(active.get("batchId") or "").strip()
        sequence = self._integer(active.get("batchSequence"), 0)
        best = None
        for _label, (_path, task) in self.inbox_tasks.items():
            if not isinstance(task, dict):
                continue
            if task.get("isReplenishment"):
                # A gap fill needs the bridge next; never delay it.
                return None
            if (not self._is_remote_auto_sequence_task(task)
                    or str(task.get("batchId") or "").strip() != batch_id
                    or str(task.get("batchKind") or "") == "examplebrand-natural-flow"
                    or str(task.get("project") or "") == "ExampleBrand"):
                continue
            task_id = str(task.get("taskId") or "").strip()
            task_sequence = self._integer(task.get("batchSequence"), 0)
            if (not task_id or task_sequence <= sequence or task_id in self.auto_start_claimed_task_ids
                    or not [url for url in (task.get("urls") or []) if str(url).strip()]):
                continue
            if best is None or task_sequence < self._integer(best.get("batchSequence"), 0):
                best = task
        if best is None:
            return None
        best_id = str(best.get("taskId") or "").strip()
        if self._read_prefetch_state(best_id):
            return None
        own = registry.get(canonical_task_id(best)) or {}
        if str(own.get("pipelineStatus") or "").strip().lower() not in {"", "waiting"}:
            return None
        source_key = str(best.get("download_path_source") or "tikhub_downloads")
        source_var = self.vars.get(source_key)
        root = self.batch_root_sources.get(source_key) or (source_var.get().strip() if source_var else "")
        if not root:
            return None
        directory = batch_download_directory(root, best)
        try:
            occupied = directory.is_dir() and any(
                path.is_file() and path.suffix.lower() in VIDEO_EXTS for path in directory.iterdir()
            )
        except OSError:
            return None
        if occupied:
            # The queue start would ask before reusing these files; so do we.
            notice = f"prefetch-occupied|{best_id}"
            notices = self.__dict__.setdefault("_prefetch_notices", set())
            if notice not in notices:
                notices.add(notice)
                self.write_log(f"未预取第 {best.get('batchSequence')} 项：下载目录已有视频 {directory}")
            return None
        return best, directory

    def poll_prefetch_next_project(self):
        if self._prefetch_running() or not self._prefetch_enabled():
            return False
        candidate = self._prefetch_candidate()
        if not candidate:
            return False
        return self._start_prefetch(*candidate)

    def _start_prefetch(self, task, directory):
        runtime = DOWNLOAD_BRIDGE_RUNTIME
        bridge = APP_DIR / "tikhub_bridge.py"
        if not runtime.is_file() or not bridge.is_file():
            return False
        task_id = str(task.get("taskId") or "").strip()
        urls = [str(url).strip() for url in (task.get("urls") or []) if str(url).strip()]
        mode_name = task.get("subtitle_mode") if task.get("subtitle_mode") in SUBTITLE_MODE_IDS else self.mode.get()
        request = {
            # Same fields the queue start writes, so the checkpoint is reused.
            "urls": urls,
            "download_path": str(directory),
            "mode": SUBTITLE_MODE_IDS.get(mode_name, "ai-sttn"),
            "subtitle_enabled": bool(task.get("subtitle_enabled", True)),
            "download_workers": max(1, min(8, self._integer(task.get("download_workers"), 2))),
            "taskId": task_id,
            "batchId": str(task.get("batchId") or ""),
            "runGroupId": str(task.get("runGroupId") or ""),
            "batchKind": str(task.get("batchKind") or "formal"),
            "selectedProjects": list(task.get("selectedProjects") or []),
            "batchSequence": self._integer(task.get("batchSequence"), 1),
            "batchSize": self._integer(task.get("batchSize"), 1),
            "date": str(task.get("date") or ""),
            "project": str(task.get("project") or ""),
            "material": str(task.get("material") or ""),
            "expectedCount": self._integer(task.get("expectedCount"), len(urls)),
            "resumeInterrupted": False,
            # The project has not started: keep its Dashboard card unchanged.
            "statusRelay": False,
            "prefetch": True,
        }
        DOWNLOAD_PREFETCH_DIR.mkdir(parents=True, exist_ok=True)
        directory.mkdir(parents=True, exist_ok=True)
        request_file = DOWNLOAD_PREFETCH_DIR / f"{task_id}.request.json"
        self._atomic_write_json(request_file, request)
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        with open(DOWNLOAD_PREFETCH_DIR / f"{task_id}.log", "a", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(runtime), "-u", str(bridge), str(request_file)],
                cwd=str(APP_DIR), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=environment, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        self.prefetch_process = process
        state = {
            "taskId": task_id,
            "batchId": request["batchId"],
            "batchSequence": request["batchSequence"],
            "project": request["project"],
            "material": request["material"],
            "urlCount": len(urls),
            "downloadPath": str(directory),
            "directoryWasEmpty": True,
            "state": "running",
            "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pid": process.pid,
            "processIdentity": capture_process_identity(process.pid, role="prefetch-download-bridge"),
        }
        self._atomic_write_json(self._prefetch_state_path(task_id), state)
        self.write_log(
            f"开始预取下一项目第 1/2 步：第 {request['batchSequence']} 项 {request['project']} / "
            f"{request['material']}，共 {len(urls)} 条（当前项目继续第 3～5 步）。"
        )
        return True

    def _has_pending_remote_successor(self, task):
        """Whether this completed project should hand Step 1/2 to the next item."""
        if not self._is_remote_auto_sequence_task(task):
            return False
        batch_id = str(task.get("batchId") or "").strip()
        try:
            sequence = int(task.get("batchSequence") or 1)
        except (TypeError, ValueError):
            sequence = 1
        if not batch_id:
            return False
        for _path, candidate in list_pending_tasks():
            if not self._is_remote_auto_sequence_task(candidate):
                continue
            if str(candidate.get("batchId") or "") != batch_id:
                continue
            try:
                candidate_sequence = int(candidate.get("batchSequence") or 0)
            except (TypeError, ValueError):
                continue
            if candidate_sequence == sequence + 1:
                return True
        return False

    def _has_eligible_remote_pending_task(self):
        """Check whether any receiver item is now safe to run next."""
        registry = load_project_download_statuses()
        status_items = registry.get("items") if isinstance(registry, dict) else {}
        if not isinstance(status_items, dict):
            status_items = {}
        for _path, candidate in list_pending_tasks():
            if self._is_remote_auto_sequence_task(candidate) and self._previous_remote_item_finished(candidate, status_items):
                return True
        return False

    def _advance_remote_queue(self):
        """Refresh first so a just-arrived next project is visible to the queue."""
        self.refresh_task_inbox(False)
        self.auto_start_next_external_task()

    def _continue_recovery_identity(self, task):
        """Build the same stable identity used by the automatic controller."""
        from resume_plan import resolve_resume_plan, verify_final_row
        date_value = str(task.get("productionDate") or task.get("date") or "").strip()
        handoff = self.capcut_store.load_date(date_value) if date_value else {"tasks": []}
        live_record = load_project_download_statuses().get("items", {}).get(canonical_task_id(task), {})
        live_pipeline_status = str((live_record or {}).get("pipelineStatus") or "").strip().lower()
        # Download/AI-subtitle recovery has a complete URL contract and a
        # task-scoped checkpoint.  Resolve it before the Fish/CapCut planner;
        # otherwise a task interrupted at 3/4 is incorrectly treated as a
        # voice task with an incomplete audio contract.
        if live_pipeline_status in {
            "step12_interrupted", "blocked_download_runtime", "downloading",
            "step12_resuming", "blocked_step12_resume", "blocked_missing_links",
        } or str((live_record or {}).get("status") or "").strip().lower() == "interrupted":
            urls = [str(url).strip() for url in (task.get("urls") or []) if str(url).strip()]
            url_contract_hash = hashlib.sha256(
                json.dumps(urls, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest() if urls else ""
            return resolve_recovery_identity(task, {
                "step": "step12", "runGroupId": task.get("runGroupId"),
                "batchId": task.get("batchId"), "canonicalTaskId": canonical_task_id(task),
                "downloadTaskId": task.get("taskId") or canonical_task_id(task),
                "urlContractHash": url_contract_hash,
            })
        if live_pipeline_status in {"blocked_voice", "voice_failed", "voice_login_required", "voice_pending"}:
            plan = {"decision": "ready", "step": "step3", "nextAction": "reconcile_voice"}
        else:
            preferred_child_task_id = ""
            if live_pipeline_status.startswith("blocked_capcut"):
                preferred_child_task_id = str((live_record or {}).get("childTaskId") or "").strip()
            plan = resolve_resume_plan(
                task, handoff.get("tasks") or [], verify_final=verify_final_row,
                preferred_child_task_id=preferred_child_task_id,
            )
        if plan.get("decision") == "blocked":
            return {"verified": False, "error": plan.get("reason")}
        if plan.get("decision") == "completed":
            # A fully verified handoff is a terminal production fact, not a
            # new Safe Continue request.  Keep it observable so the caller
            # can finish only release/maintenance work without creating a
            # fresh recovery command or turning a completed task into a
            # synthetic identity failure.
            return {
                "verified": True,
                "completed": True,
                "stableRecoveryKey": f"step5|{canonical_task_id(task)}",
                "step": "step5",
                "runGroupId": task.get("runGroupId"),
                "batchId": task.get("batchId"),
                "canonicalTaskId": canonical_task_id(task),
                "childTaskId": "",
            }
        if plan.get("step") == "step5":
            row = plan["row"]
            return resolve_recovery_identity(task, {
                "step": "step5", "runGroupId": task.get("runGroupId"), "batchId": task.get("batchId"),
                "canonicalTaskId": canonical_task_id(task), "outputTaskId": plan["childTaskId"],
                "product": row.get("material"), "language": row.get("language"),
                "sourceVideoHash": plan["sourceVideoHash"], "outputIdentity": row.get("finalOutputPath"),
            })
        candidate = self._auto_recovery_candidate()
        if candidate and candidate.get("identityVerified"):
            return {
                "verified": True,
                "stableRecoveryKey": candidate.get("stableKey"),
                "step": candidate.get("step"),
                "runGroupId": candidate.get("runGroupId"),
                "batchId": candidate.get("batchId"),
                "canonicalTaskId": candidate.get("taskId"),
                "childTaskId": candidate.get("childTaskId"),
            }
        record = load_project_download_statuses().get("items", {}).get(canonical_task_id(task), {})
        # The project-status projection intentionally contains only pipeline
        # state.  A failed CapCut child keeps its immutable media identity in
        # the date-scoped Step-4/5 handoff row.  Merge that row before
        # resolving Safe Continue so an older status projection cannot make a
        # real child look identity-incomplete.
        if isinstance(record, dict):
            date_value = str(record.get("productionDate") or record.get("date")
                              or task.get("productionDate") or task.get("date") or "").strip()
            task_id = canonical_task_id(task)
            try:
                handoff = self.capcut_store.load_date(date_value) if date_value else {"tasks": []}
            except Exception:
                handoff = {"tasks": []}
            child_hint = str(
                record.get("childTaskId") or task.get("childTaskId") or ""
            ).strip()
            matching_row = next((row for row in (handoff.get("tasks") or [])
                                 if isinstance(row, dict)
                                 and str(row.get("remotePipelineTaskId") or "").strip() == task_id
                                 and (not child_hint or str(row.get("taskId") or "").strip() == child_hint)), None)
            if isinstance(matching_row, dict):
                merged = dict(record)
                for key, value in matching_row.items():
                    if value not in (None, "", [], {}):
                        merged[key] = value
                record = merged
        # Prefer explicit producer ownership over a stale/ambiguous pipeline
        # label.  A manual Safe Continue may be invoked while the dashboard
        # still shows a generic waiting state; the presence of a CapCut child
        # identity is the durable fact that this is Step 5.  Likewise, a
        # voice/audio child identity is the durable Step 3 fact.  Never fall
        # back to the parent batch as a recovery identity.
        has_step5_child = False if live_pipeline_status in {"blocked_voice", "voice_failed", "voice_login_required", "voice_pending"} else any(str(record.get(name) or task.get(name) or "").strip()
                              for name in ("capcutTaskId", "outputTaskId", "videoTaskId", "childTaskId"))
        has_step3_child = any(str(record.get(name) or task.get(name) or "").strip()
                              for name in ("voiceTaskId", "audioTaskId"))
        pipeline_status = str(record.get("pipelineStatus") or "").lower()
        if has_step5_child or pipeline_status.startswith(("blocked_capcut", "capcut_")):
            step = "step5"
        elif has_step3_child:
            step = "step3"
        else:
            step = "step3"
        child = str(
            (record.get("capcutTaskId") or record.get("outputTaskId") or record.get("videoTaskId")
             or record.get("childTaskId") or task.get("childTaskId"))
            if step == "step5" else (record.get("voiceTaskId") or record.get("audioTaskId"))
            or ""
        ).strip()
        identity_facts = {
            "step": step,
            "runGroupId": record.get("runGroupId") or task.get("runGroupId"),
            "batchId": record.get("batchId") or task.get("batchId"),
            "canonicalTaskId": record.get("canonicalTaskId") or canonical_task_id(task),
            "outputTaskId": child if step == "step5" else "",
            "audioTaskId": child if step == "step3" else "",
            "product": record.get("product") or record.get("material") or task.get("material"),
            "material": record.get("material") or task.get("material"),
            "language": record.get("language") or task.get("language"),
            "copyTextHash": record.get("copyTextHash") or task.get("copyTextHash"),
            "voiceContractId": record.get("voiceContractId") or record.get("voiceId") or task.get("voiceContractId") or task.get("voiceId"),
            "sourceVideoHash": record.get("sourceVideoHash") or task.get("sourceVideoHash"),
            "legacySourceIdentity": record.get("generationIdentity") or record.get("videoPath") or child,
            "outputIdentity": record.get("outputPath") or record.get("output") or child,
        }
        if step == "step5" and not identity_facts.get("sourceVideoHash"):
            source_path = str(record.get("videoPath") or task.get("videoPath") or "").strip()
            try:
                if source_path and Path(source_path).is_file():
                    digest = hashlib.sha256()
                    with Path(source_path).open("rb") as source_handle:
                        for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    identity_facts["sourceVideoHash"] = digest.hexdigest()
            except (OSError, PermissionError):
                pass
            if not identity_facts.get("sourceVideoHash"):
                identity_facts["sourceVideoHash"] = str(
                    record.get("generationIdentity") or task.get("generationIdentity") or ""
                ).strip()
        if step == "step3" and (
            not child
            or not identity_facts.get("copyTextHash")
            or not identity_facts.get("voiceContractId")
        ):
            identity_facts.update(self._voice_batch_recovery_facts(task, record))
        return resolve_recovery_identity(task, identity_facts)

    def _prepare_recovery_request(self, task):
        """Background-only claim; no Tk access and no production actions."""
        # Reconcile completed earlier-stage commands before claiming a later
        # stage. New manual requests may legitimately post-date the audio.
        from resume_plan import adopt_completed_voice
        identity = self._continue_recovery_identity(task)
        runtime = self._read_local_json_object(self._fish_bridge_runtime_file())
        handoff = self._read_local_json_object(self._voice_handoff_file())
        if identity.get("step") != "step12":
            for old in self.recovery_control.status().get("commands", {}).values():
                if old.get("state") not in {"accepted", "executing", "running", "reconciliation_required"}:
                    continue
                adoption = adopt_completed_voice(task, old, runtime, handoff)
                if adoption:
                    adoption["adoptedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    self.recovery_control.record_execution(old["commandId"], adoption, "completed")
        if not identity.get("verified"):
            raise RecoveryControlError("recovery_identity_unverified", str(identity.get("error") or "missing child identity"))
        # A finished project whose CapCut draft is still waiting for release
        # holds the queue, and that release only runs under a Safe Continue
        # command.  Returning "completed" here left such a project stuck for
        # good (10.9 item 3, 2026-10-10 02:08), so claim a command instead.
        if identity.get("completed") and not self._task_has_pending_draft_release(task):
            return {
                "decision": "completed",
                "reason": "已验证的交接已完成，仅需维护收口",
                "taskId": canonical_task_id(task),
            }
        pending = self._read_local_json_object(CONTINUE_COMMAND_FILE)
        task_id = canonical_task_id(task)
        matches = str(pending.get("taskId") or "") == task_id
        pending_state = str(pending.get("state") or "").strip().lower()
        # A terminal/blocked mailbox is historical evidence, not a request
        # alias.  Reusing its commandId makes the next manual Safe Continue
        # hit ``recovery_command_terminal`` forever after a failed retry.
        # Only an actually in-flight transport may be resumed by the same ID;
        # terminal commands must receive a fresh idempotent manual request.
        pending_id = (
            str(pending.get("commandId") or "")
            if matches and pending_state in {
                "requested", "accepted", "running", "recovering", "executing"
            }
            else ""
        )
        failure = {
            "failureEventId": str(pending.get("failureEventId") or "manual-" + uuid.uuid4().hex),
            "failureFingerprint": "manual-" + identity["stableRecoveryKey"],
            "errorCode": str(pending.get("errorCode") or "MANUAL_SAFE_CONTINUE"),
            "failedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        claim = self.recovery_control.claim_recovery(
            identity, "manual", request_id=pending_id or None, failure=failure,
            resource_guard=lambda: self._auto_recovery_resources_busy(task_id, pending_id),
        )
        if not claim.get("accepted"):
            return {"decision": "waiting", "reason": claim.get("code"), "taskId": task_id}
        command_id = claim["command"]["commandId"]
        accepted = self.recovery_control.accept_command(
            command_id, capture_process_identity(os.getpid(), role="workbench"),
            resource_guard=lambda: self._auto_recovery_resources_busy(task_id, command_id),
        )
        return {**accepted, "taskId": task_id}

    def continue_current_project(self, interactive=True):
        """Schedule the shared claim without blocking the Tk event callback."""
        from concurrent.futures import ThreadPoolExecutor
        self._last_recovery_outcome = "waiting"
        if self.__dict__.get('_recovery_close_requested'):
            return False
        if self.__dict__.get("_manual_recovery_future") is not None:
            return False
        if "_recovery_executor" not in self.__dict__:
            self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recovery-control")
            self._recovery_poll_future = None
        task = copy.deepcopy(self.active_external_task) if isinstance(self.active_external_task, dict) else {}
        self._auto_recovery_busy = True
        self.status_var.set("正在核验安全继续身份和命令，请勿重复启动。")
        self._manual_recovery_future = self._recovery_executor.submit(self._prepare_recovery_request, task)
        self.after(100, lambda: self._finish_recovery_request(interactive, task))
        return False

    def _finish_recovery_request(self, interactive, task):
        future = self.__dict__.get("_manual_recovery_future")
        if future is None:
            return
        if not future.done():
            self.after(100, lambda: self._finish_recovery_request(interactive, task))
            return
        try:
            prepared = future.result()
            if self.__dict__.get('_recovery_close_requested'):
                self._manual_recovery_future = None
                self._auto_recovery_busy = False
                return
            if prepared.get("decision") != "execute_new":
                reason = str(prepared.get("reason") or prepared.get("decision") or "reconciliation_required")
                decision = str(prepared.get("decision") or "blocked")
                if decision == "completed":
                    # Do not write a completed handoff back as a blocked
                    # mailbox or create a new command.  The durable result
                    # already proves there is no production action left.
                    message = str(prepared.get("reason") or "已验证完成，无需重复安全继续")
                    self.status_var.set(message)
                    self.write_log(message)
                    self._manual_recovery_future = None
                    self._auto_recovery_busy = False
                    return
                if decision == "observe_existing":
                    # A second manual/dashboard request is an observation of
                    # the already claimed command, not a production failure.
                    # Never overwrite the command mailbox as blocked while
                    # the original worker/client is still being reconciled.
                    message = "已有安全继续命令，正在对账：" + reason
                    self.status_var.set(message)
                    self.write_log(message)
                    self._manual_recovery_future = None
                    self._auto_recovery_busy = False
                    return
                if decision == "waiting":
                    self.status_var.set("安全继续等待门禁：" + reason)
                else:
                    self.status_var.set(reason)
                try:
                    mailbox = self._read_local_json_object(CONTINUE_COMMAND_FILE)
                    if (isinstance(mailbox, dict)
                            and str(mailbox.get("taskId") or "") == canonical_task_id(task)
                            and not str(mailbox.get("boundManifestId") or "").strip()):
                        if decision == "waiting":
                            mailbox["state"] = "waiting"
                            mailbox["phase"] = "recovery_waiting"
                            mailbox["message"] = f"安全继续等待：{reason}"
                        else:
                            mailbox["state"] = "blocked"
                            mailbox["phase"] = "recovery_claim_blocked"
                            mailbox["primaryError"] = reason
                            mailbox["message"] = f"安全继续未领取：{reason}"
                            mailbox["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                            mailbox["lastHeartbeat"] = mailbox["finishedAt"]
                        self._write_continue_command(mailbox)
                except Exception as mailbox_exc:
                    self.write_log(f"保存安全继续领取错误失败：{mailbox_exc}")
                self._manual_recovery_future = None
                self._auto_recovery_busy = False
                return
            if canonical_task_id(task) != canonical_task_id(self.active_external_task or {}):
                raise RecoveryControlError("recovery_command_identity_conflict", "active task changed while claiming")
            command_id = prepared["commandId"]
            # The existing UI-aware production method remains on Tk. It never
            # waits for a recovery controller lock; receipts run in the worker.
            result = self._continue_current_project_impl(interactive=interactive)
            receipt = self._recovery_execution_receipt(prepared["taskId"])
            receipt["requestAccepted"] = bool(result)
            if result:
                state_name = "running"
            else:
                # Every False return from _continue_current_project_impl means
                # it started nothing and left the concrete reason in
                # status_var: the work is already running elsewhere, a guard
                # refused it, or a precondition is missing.  Recording
                # reconciliation_required here locked this step's recovery key
                # for good, so every later Safe Continue was ignored.  End the
                # command instead and show the Dashboard the real reason.
                state_name = "blocked"
                receipt["blockedReason"] = self.status_var.get()
                self._close_unstarted_continue_command(task, command_id, receipt["blockedReason"])
            self._manual_recovery_future = self._recovery_executor.submit(
                self.recovery_control.record_execution, command_id, receipt, state_name
            )
            self.after(100, self._finish_recovery_receipt)
        except Exception as exc:
            self.status_var.set(f"安全继续暂停：{type(exc).__name__}: {exc}")
            self.write_log(self.status_var.get())
            # Keep the first concrete preparation/identity error on the
            # mailbox.  The next poll may otherwise classify the unbound
            # accepted command as stale and replace the cause with a generic
            # identity message, making diagnosis impossible.
            try:
                mailbox = self._read_local_json_object(CONTINUE_COMMAND_FILE)
                if (isinstance(mailbox, dict)
                        and str(mailbox.get("taskId") or "") == canonical_task_id(task)
                        and not str(mailbox.get("boundManifestId") or "").strip()):
                    mailbox["state"] = "blocked"
                    mailbox["phase"] = "recovery_prepare_failed"
                    mailbox["primaryError"] = f"{type(exc).__name__}: {exc}"
                    mailbox["message"] = f"安全继续准备失败：{type(exc).__name__}: {exc}"
                    mailbox["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    mailbox["lastHeartbeat"] = mailbox["finishedAt"]
                    self._write_continue_command(mailbox)
            except Exception as mailbox_exc:
                self.write_log(f"保存安全继续准备错误失败：{mailbox_exc}")
            self._manual_recovery_future = None
            self._auto_recovery_busy = False

    def _close_unstarted_continue_command(self, task, command_id, reason):
        """Finish the Dashboard mailbox of a Safe Continue that started nothing.

        A mailbox already bound to a CapCut attempt is left to the terminal
        status reconciler.
        """
        try:
            mailbox = self._read_local_json_object(CONTINUE_COMMAND_FILE)
            if not (str(mailbox.get("taskId") or "") == canonical_task_id(task)
                    and str(mailbox.get("commandId") or command_id) == command_id
                    and str(mailbox.get("state") or "") in {"requested", "accepted"}
                    and not str(mailbox.get("boundManifestId") or "").strip()):
                return
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            mailbox.update({
                "state": "blocked",
                "phase": "recovery_nothing_started",
                "message": reason or "安全继续没有启动新的步骤。",
                "finishedAt": now,
                "lastHeartbeat": now,
            })
            self._write_continue_command(mailbox)
        except Exception as exc:
            self.write_log(f"保存安全继续结果失败：{exc}")

    def _finish_recovery_receipt(self):
        future = self.__dict__.get("_manual_recovery_future")
        if future is not None and not future.done():
            self.after(100, self._finish_recovery_receipt)
            return
        try:
            if future is not None:
                future.result()
        except Exception as exc:
            self.status_var.set(f"执行回执待核对，禁止重新派发：{exc}")
            self.write_log(self.status_var.get())
        finally:
            self._manual_recovery_future = None
            self._auto_recovery_busy = False

    def _continue_current_project_impl(self, interactive=True):
        """Safely reconcile and resume the active remote project.

        This is deliberately a recovery action, not a restart action: it only
        recovers durable Step 4/5 records and lets the existing guarded resume
        path launch a client when the formal task-count contract is intact.
        It never clears media, recreates completed work, or guesses missing
        source files.
        """
        if not bool(self.auto_recovery_var.get()) and self.recovery_control.acceptance_verified():
            # A deliberate Safe Continue (including the Dashboard mailbox
            # path, which invokes this method with interactive=False) starts
            # the recovery session.  The automatic poller never calls this
            # method directly; it only claims/dispatches through the shared
            # controller, so this cannot silently re-enable a paused timer.
            # Enabling here does not reset any per-task budget.
            self.auto_recovery_var.set(True)
            self.set_auto_recovery_enabled()
        if interactive:
            # Manual Safe Continue and the automatic poller share one durable
            # command mailbox.  Do not let a click race an already claimed
            # automatic command or create a second client.
            pending = self._read_local_json_object(CONTINUE_COMMAND_FILE)
            if str(pending.get("source") or "") == "automatic_recovery" and str(pending.get("state") or "").lower() in {
                "requested", "accepted", "recovering", "running", "executing",
            }:
                text = "自动恢复命令正在执行，当前点击已忽略。"
                self.status_var.set(text); self.write_log(text)
                messagebox.showinfo("自动恢复进行中", text)
                return False
        # Resolve the requested canonical task first.  A previous project's
        # automatic client must never decide whether this project can recover.
        task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        if not task:
            task = load_current_context()
        source_task_id = str(task.get("taskId") or "").strip()
        task_id = canonical_task_id(task)

        if self.download_process and self.download_process.poll() is None:
            text = "当前下载与去字幕仍在运行，无需继续；系统会在本条完成后自动接力。"
            self.status_var.set(text); self.write_log(text)
            if interactive:
                messagebox.showinfo("任务仍在运行", text)
            return False
        if self.stitcher_controller is not None and self.stitcher_controller.is_running():
            text = "当前素材拼接仍在运行，无需继续；系统会在本条完成后自动接力。"
            self.status_var.set(text); self.write_log(text)
            if interactive:
                messagebox.showinfo("任务仍在运行", text)
            return False
        if task_id and self._current_capcut_client_is_running(task_id):
            text = "当前 CapCut 客户端仍在运行，无需继续；系统会同步本轮状态。"
            self.status_var.set(text); self.write_log(text)
            if interactive:
                messagebox.showinfo("任务仍在运行", text)
            return False

        if task_id and str(task.get("status") or "") == "cancelled":
            text = "当前远程任务已撤回，不能继续。请从任务接收器重新提交。"
            self.status_var.set(text); self.write_log(text)
            if interactive:
                messagebox.showwarning("任务已撤回", text)
            return False

        if source_task_id and task_id and source_task_id != task_id:
            parent = load_task_record(task_id)
            if not isinstance(parent, dict):
                text = "补链记录存在，但找不到原项目配置；已停止安全继续。"
                self.status_var.set(text); self.write_log(text)
                if interactive:
                    messagebox.showwarning("原项目不存在", text)
                return False
            parent = dict(parent)
            parent.update({
                "canonicalTaskId": task_id,
                "status": "step12_completed",
                "step1DownloadDirectory": str(
                    task.get("step1DownloadDirectory")
                    or parent.get("step1DownloadDirectory")
                    or self.vars["tikhub_downloads"].get()
                ),
                "replenishmentClosedByTaskId": source_task_id,
            })
            date_value = str(parent.get("date") or task.get("date") or "").strip()
            moved = self.capcut_store.reassign_remote_owner(date_value, source_task_id, task_id) if date_value else 0
            self.active_external_task = parent
            save_current_context(parent)
            task = parent
            if moved:
                self.write_log(f"安全继续已将 {moved} 条第五步任务从补链记录归回原项目 {task_id}。")

        if not task_id:
            self.refresh_task_inbox(False)
            self.refresh_capcut_tasks(True)
            self._sync_remote_pipeline_from_capcut()
            self.auto_start_next_external_task()
            self.status_var.set("已完成可恢复任务扫描；没有当前项目时只会启动队列中已满足前置条件的下一项。")
            self.write_log(self.status_var.get())
            return True

        self.active_external_task = task
        date_value = str(task.get("date") or "").strip()
        expected_project = str(task.get("project") or "").strip()
        expected_count = contract_expected_count(task)
        registry = load_project_download_statuses().get("items", {})
        download_record = registry.get(task_id) if isinstance(registry, dict) else None
        if not isinstance(download_record, dict):
            detail = "继续前对账未通过：找不到父项目第 1/2 步状态。"
            self.status_var.set(detail); self.write_log(detail)
            if interactive:
                messagebox.showwarning("第 1/2 步状态缺失", detail)
            return False
        source_expected = max(1, self._integer(download_record.get("expectedCount"), expected_count or 1))
        source_completed = max(0, self._integer(download_record.get("completed"), 0))
        source_missing = max(0, self._integer(download_record.get("missingCount"), source_expected - source_completed))
        if source_missing or source_completed != source_expected:
            try:
                interrupted_session = json.loads(DOWNLOAD_STATE_FILE.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError, UnicodeError):
                interrupted_session = {}
            resumable, resume_reason = interrupted_step12_resume_evidence(
                task, download_record, interrupted_session
            )
            recovery_plan = {}
            recovery_mode = "interrupted_session"
            if not resumable:
                checkpoint_path = DOWNLOAD_CHECKPOINT_DIR / f"{str(task.get('taskId') or '').strip()}.json"
                try:
                    checkpoint_payload = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError, TypeError, UnicodeError):
                    checkpoint_payload = {}
                checkpoint_resumable, checkpoint_reason, recovery_plan = checkpoint_step12_resume_evidence(
                    task, download_record, checkpoint_payload
                )
                if checkpoint_resumable:
                    resumable = True
                    resume_reason = checkpoint_reason
                    recovery_mode = "checkpoint_rebuild"
            prefetching = self._prefetch_running() if resumable else ""
            if prefetching:
                detail = f"下一项目正在预取第 1/2 步（{prefetching}），预取结束后再点安全继续。"
                self.status_var.set(detail); self.write_log(detail)
                return False
            if resumable:
                self.write_log(
                    f"检测到第 1/2 步具备完整恢复证据：{resume_reason}；"
                    f"恢复模式={recovery_mode}。"
                )
                self.active_external_task = dict(task)
                request = ((interrupted_session.get("context") or {}).get("request")
                           if isinstance(interrupted_session.get("context"), dict) else {})
                if recovery_mode == "checkpoint_rebuild":
                    try:
                        saved_request = json.loads((APP_DIR / "current_download_request.json").read_text(encoding="utf-8-sig"))
                    except (OSError, ValueError, TypeError, UnicodeError):
                        saved_request = {}
                    if (
                        isinstance(saved_request, dict)
                        and str(saved_request.get("taskId") or "").strip() == str(task.get("taskId") or "").strip()
                        and [str(url).strip() for url in (saved_request.get("urls") or []) if str(url).strip()]
                            == [str(url).strip() for url in (task.get("urls") or []) if str(url).strip()]
                    ):
                        request = saved_request
                    else:
                        request = {}
                    recovery_generation = f"step12-recovery-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
                    self.active_external_task["step12Recovery"] = {
                        **recovery_plan,
                        "recoveryGeneration": recovery_generation,
                        "mode": recovery_mode,
                        "rebuiltAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "errorCode": "STEP12_RECOVERY_SESSION_REBUILT",
                    }
                download_dir = str((request or {}).get("download_path") or task.get("step1DownloadDirectory") or "").strip()
                if download_dir:
                    self.active_external_task["step1DownloadDirectory"] = download_dir
                    self.vars["tikhub_downloads"].set(download_dir)
                urls = [str(url).strip() for url in (task.get("urls") or []) if str(url).strip()]
                self.links.delete("1.0", "end")
                self.links.insert("1.0", "\n".join(urls))
                self.links.edit_modified(False)
                self.update_link_count()
                self._set_remote_pipeline_status(
                    task_id,
                    "step12_resuming",
                    f"第 1/2 步断点恢复中：已完成 "
                    f"{int(recovery_plan.get('completedCount') or source_completed)}/{source_expected}，正在继续剩余 "
                    f"{int(recovery_plan.get('pendingCount') or (source_expected - source_completed))} 条",
                )
                started = self.start_background_download(auto_start=True, resume_existing=True)
                if started:
                    remaining_count = int(recovery_plan.get("pendingCount") or (source_expected - source_completed))
                    self.status_var.set(
                        f"第 1/2 步已从断点恢复：完成项不会重做，正在继续剩余 {remaining_count} 条。"
                    )
                    self.write_log(self.status_var.get())
                    return True
                detail = "第 1/2 步断点符合恢复条件，但下载桥未能启动；文件与检查点均已保留，可再次安全继续。"
                self._set_remote_pipeline_status(task_id, "blocked_step12_resume", detail)
                self.status_var.set(detail)
                self.write_log(detail)
                return False
            if source_missing:
                detail = f"当前项目仍停在第 1/2 步：有效素材 {source_completed}/{source_expected}，还差 {source_missing} 条。"
                blocked_state = "blocked_missing_links"
            else:
                detail = (
                    f"当前项目第 1/2 步尚未完成：有效素材 {source_completed}/{source_expected}，"
                    "原始链接数量完整，但未找到可安全恢复的下载会话。"
                )
                blocked_state = "blocked_step12_resume"
            self._set_remote_pipeline_status(task_id, blocked_state, detail)
            self.status_var.set(detail); self.write_log(detail)
            if interactive:
                messagebox.showwarning("仍需补链", detail)
            return False

        # A task-scoped CapCut handoff is stronger evidence than the cached
        # pipelineStatus.  In particular, pipelineStatus can still say
        # ``step12_completed`` after Step 4 has already produced a formal
        # manifest.  Resolve the current task's handoff before considering a
        # voice/stitch recovery so Safe Continue cannot send a 2/4 CapCut
        # project back through Step 4.
        capcut_payload = self.capcut_store.load_date(date_value) if date_value else {"tasks": []}
        current_capcut_rows = [
            item for item in capcut_payload.get("tasks") or []
            if str(item.get("remotePipelineTaskId") or "").strip() == task_id
        ]

        # Step 1/2 is already reconciled and this task has not reached the
        # CapCut handoff yet.  Advance its own next step before any global
        # CapCut recovery scan.  The latter can inspect a large historical
        # catalogue and must never make Fish recovery appear to be running
        # while no task-scoped voice plan or process has been created.
        pipeline_state = str(download_record.get("pipelineStatus") or "").lower()
        if not current_capcut_rows and pipeline_state in {
            "step1_completed", "step12_completed", "voice_pending",
            "blocked_voice", "voice_failed", "voice_login_required",
        }:
            self.total_var.set(str(source_expected))
            self.success_var.set(str(source_completed))
            self.failed_var.set("0")
            self.last_batch_result = {
                "total": source_expected,
                "succeeded": source_completed,
                "failed": 0,
            }
            completed_handoff = self._completed_voice_handoff_for_task(task)
            if completed_handoff is not None:
                self._set_remote_pipeline_status(task_id, "voice_completed", "已验证当前项目配音，正在进入第 4 步")
                self._handle_voice_handoff(completed_handoff)
                return task_id in self.stitch_auto_launch_task_ids
            return self._advance_after_step12(task, force_voice_retry=True)

        # The Step 1/2 -> voice transition must not enumerate the general
        # inbox first.  Inbox refresh updates Tk controls and can be delayed
        # by a visible desktop prompt; doing it before the task-scoped branch
        # left a valid Dashboard command stuck at ``running`` without ever
        # creating its Fish task plan.  Later recovery paths still refresh the
        # inbox before looking at the historical CapCut catalogue.
        self.refresh_task_inbox(False)
        self.refresh_capcut_tasks(True)
        self._sync_remote_pipeline_from_capcut()
        payload = capcut_payload
        archived_excess = self.capcut_store.archive_excess_pending_remote_tasks(
            date_value, task_id, expected_count
        ) if date_value and expected_count else 0
        if archived_excess:
            self.write_log(
                f"已将 {archived_excess} 条超额第五步记录移入诊断历史；"
                f"当前正式清单仅保留 {expected_count} 条。"
            )
            payload = self.capcut_store.load_date(date_value)
        owned = [
            item for item in payload.get("tasks") or []
            if str(item.get("remotePipelineTaskId") or "").strip() == task_id
        ]
        unique_ids = {str(item.get("taskId") or "") for item in owned if str(item.get("taskId") or "")}

        if expected_count and len(owned) == 0:
            self.total_var.set(str(source_expected))
            self.success_var.set(str(source_completed))
            self.failed_var.set("0")
            self.last_batch_result = {"total": source_expected, "succeeded": source_completed, "failed": 0}
            completed_handoff = self._completed_voice_handoff_for_task(task)
            if completed_handoff is not None:
                # A durable completed handoff is stronger evidence than the
                # old "already handled" marker.  Resume Step 4 directly and
                # never re-open Fish for this recovery path.
                self.stitch_auto_launch_task_ids.discard(task_id)
                self._set_remote_pipeline_status(task_id, "voice_completed", "已验证现有配音，正在恢复第 4 步")
                self._handle_voice_handoff(completed_handoff)
                if task_id in self.stitch_auto_launch_task_ids:
                    self.status_var.set("已复用当前任务有效配音，第四步拼接正在运行。")
                    self.write_log(self.status_var.get())
                    return True
                detail = "已找到有效配音，但第四步未能启动；已保留音频和任务，未重新启动 Fish。"
                self._set_remote_pipeline_status(task_id, "blocked_stitch", detail)
                self.status_var.set(detail)
                self.write_log(detail)
                return False
            return self._advance_after_step12(task, force_voice_retry=True)

        if expected_count and (len(owned) != expected_count or len(unique_ids) != expected_count):
            detail = (
                f"继续前对账未通过：{expected_project} 需要 {expected_count} 条，"
                f"当前交接记录 {len(owned)} 条、唯一任务 {len(unique_ids)} 条。"
                "已保留全部文件且未启动 CapCut；请按缺失任务清单修复后再继续。"
            )
            self._set_remote_pipeline_status(task_id, "blocked_reconcile", detail)
            self.status_var.set(detail)
            self.write_log(detail)
            if interactive:
                messagebox.showwarning("任务清单不完整", detail)
            return False

        resumed_capcut = self._resume_remote_capcut_if_ready(recovery=True)
        self._sync_remote_pipeline_from_capcut()
        if not resumed_capcut:
            latest = self.capcut_store.load_date(date_value) if date_value else {"tasks": []}
            release_blocked_rows = [
                item for item in latest.get("tasks") or []
                if str(item.get("remotePipelineTaskId") or "").strip() == task_id
                and self._capcut_row_has_pending_draft_release(item)
            ]
            if release_blocked_rows:
                # Preserve the confirmed draft-maintenance cause.  Replacing
                # it with the generic "no recovery manifest" message hid both
                # cross-volume evidence failures and missing quality facts.
                warning = next((
                    str(item.get("draftCleanupWarning") or "").strip()
                    for item in release_blocked_rows
                    if str(item.get("draftCleanupWarning") or "").strip()
                ), "草稿目录恢复尚未完成；已完成成片保持有效。")
                detail = f"第 5 步草稿目录恢复未启动：{warning}"
                self._set_remote_pipeline_status(
                    task_id,
                    "capcut_draft_release_pending",
                    detail,
                    error_code="CAPCUT_DRAFT_RELEASE_PENDING",
                )
                self.status_var.set(detail)
                self.write_log(detail)
                return False
            blocked_rows = [
                item for item in latest.get("tasks") or []
                if str(item.get("remotePipelineTaskId") or "").strip() == task_id
                and str(item.get("status") or "") in RUNNABLE_STATUSES
            ]
            if blocked_rows:
                detail = "第 5 步仍有未完成任务，但没有形成可启动的恢复清单。"
                self._set_remote_pipeline_status(task_id, "blocked_capcut", detail)
                self.status_var.set(detail)
                self.write_log(detail)
                return False
        if resumed_capcut:
            self.status_var.set("Remote Step 5 recovery is running; existing CapCut work is being observed.")
            self.write_log(self.status_var.get())
            return True
        self.status_var.set("当前项目已对账；已有有效产物会跳过，满足条件的下一步骤将自动继续。")
        self.write_log(self.status_var.get())
        return True

    def _capcut_run_started_for_command(self, task_id, request):
        """True when a CapCut attempt for this task began around this command.

        The mailbox binding can be missing even though CapCut runs (the
        attempt may start before the command is accepted), so an unbound
        command is not proof that CapCut never started.  Accept an attempt
        whose manifest was created at most 60 seconds before the command
        started and that is still heartbeating, has not written its first
        status yet, or ended after the command started; the terminal status
        reconciler then closes the command from that status file.
        """
        started_at = str(request.get("startedAt") or "").strip()
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        earliest = started.timestamp() - 60
        handoff_dir = self.capcut_store.root.parent / "handoff" / "capcut"
        try:
            manifests = [
                path for path in handoff_dir.glob("*.json")
                if not path.name.endswith(".status.json") and path.stat().st_mtime >= earliest
            ]
        except OSError:
            return False
        for manifest in manifests:
            owner_id, status_file = self._capcut_manifest_runtime(manifest)
            if owner_id != str(task_id or "").strip():
                continue
            status_path = Path(status_file) if status_file else None
            if status_path is None or not status_path.is_file():
                try:
                    if time.time() - manifest.stat().st_mtime <= 120:
                        return True
                except OSError:
                    pass
                continue
            if self._capcut_status_file_is_running(status_path):
                return True
            status = self._read_local_json_object(status_path)
            ended_at = status.get("updatedAt") or status.get("heartbeatAt")
            if not status.get("running") and ended_at and self._iso_at_or_after(ended_at, started_at):
                return True
        return False

    def poll_control_commands(self):
        """Run a Dashboard command on this local workbench only.

        The Dashboard merely writes a small command mailbox.  This process
        owns all recovery decisions and never lets a remote browser manipulate
        media paths directly.
        """
        try:
            request = json.loads(CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig"))
            # A request can be accepted before the background identity claim
            # returns.  If that claim fails, the old mailbox used to remain
            # ``accepted`` forever and hide the real reason.  Close only the
            # provably unbound case: there must be no operation binding, no
            # live client owned by this task, and no bound CapCut status file
            # reporting a live process.  Any binding or live status remains
            # untouched for normal reconciliation.
            if isinstance(request, dict) and request.get("step") == "step5" and str(request.get("state") or "").lower() in {
                "accepted", "executing", "recovering",
            } and not any(str(request.get(field) or "").strip() for field in (
                "boundManifestId", "boundAttemptId", "boundStatusFile",
                "clientPid", "capcutRecoveryStartedAt",
            )):
                stale_task = self.active_external_task if isinstance(self.active_external_task, dict) else load_current_context()
                stale_task_id = canonical_task_id(stale_task)
                stale_date = str(stale_task.get("date") or "").strip() if isinstance(stale_task, dict) else ""
                stale_rows = self.capcut_store.load_date(stale_date).get("tasks", []) if stale_date else []
                live_status = any(
                    isinstance(row, dict)
                    and str(row.get("remotePipelineTaskId") or "").strip() == stale_task_id
                    and self._capcut_status_file_is_running(row.get("statusFile"))
                    for row in stale_rows
                )
                if (stale_task_id and not live_status
                        and not self._current_capcut_client_is_running(stale_task_id)
                        and self.__dict__.get("_manual_recovery_future") is None
                        and not self._capcut_run_started_for_command(stale_task_id, request)):
                    request["state"] = "blocked"
                    request["phase"] = "recovery_identity_unverified"
                    request["message"] = "安全继续未能完成任务身份核验，也未发现本任务新的 CapCut 运行；原任务和已完成结果均已保留。"
                    request["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    request["lastHeartbeat"] = request["finishedAt"]
                    self._write_continue_command(request)
                    self.status_var.set(request["message"])
                    self.write_log(request["message"])
                    return
            if isinstance(request, dict) and request.get("state") == "requested":
                task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
                if not task:
                    task = load_current_context()
                registry = load_project_download_statuses()
                status_items = registry.get("items") if isinstance(registry, dict) else {}
                resolved_task, resolve_error = resolve_continue_command_task(
                    request,
                    task,
                    status_items,
                )
                if resolve_error:
                    request["state"] = "blocked"
                    request["stale"] = True
                    request["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    request["message"] = resolve_error
                    self._write_continue_command(request)
                    self.status_var.set(resolve_error)
                    self.write_log(resolve_error)
                    return
                recovery_command_id = str(request.get("commandId") or request.get("recoveryCommandId") or "").strip()
                if isinstance(resolved_task, dict) and canonical_task_id(resolved_task) != canonical_task_id(task):
                    old_task_id = canonical_task_id(task)
                    task = resolved_task
                    self.active_external_task = task
                    save_current_context(task)
                    record = status_items.get(canonical_task_id(task), {}) if isinstance(status_items, dict) else {}
                    expected = max(1, self._integer(record.get("expectedCount"), contract_expected_count(task) or 1))
                    completed = max(0, self._integer(record.get("completed"), 0))
                    self.total_var.set(str(expected))
                    self.success_var.set(str(completed))
                    self.failed_var.set("0")
                    self.progress_var.set(f"{min(100, int(completed * 100 / expected))}%")
                    self.last_batch_result = {"total": expected, "succeeded": completed, "failed": 0}
                    self.download_batch_context = {
                        "date": str(task.get("productionDate") or task.get("date") or ""),
                        "project": str(task.get("project") or ""),
                        "material": str(task.get("material") or ""),
                        "activeExternalTask": task,
                    }
                    step1_dir = str(task.get("step1DownloadDirectory") or "").strip()
                    if step1_dir:
                        self.vars["tikhub_downloads"].set(step1_dir)
                    self.save_download_session()
                    self.write_log(
                        "安全继续已收口完成的旧项目并切换到命令目标："
                        f"{old_task_id or '无'} → {canonical_task_id(task)}"
                    )
                try:
                    pipeline_snapshot = json.loads(PIPELINE_STATUS_FILE.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    pipeline_snapshot = {}
                target_ok, target_error = continue_command_target_matches(
                    request,
                    task,
                    str(pipeline_snapshot.get("updatedAt") or "") if isinstance(pipeline_snapshot, dict) else "",
                )
                if not target_ok:
                    request["state"] = "blocked"
                    request["stale"] = True
                    request["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    request["message"] = target_error
                    self._write_continue_command(request)
                    self.status_var.set(target_error)
                    self.write_log(target_error)
                    return
                # A command that reached this process has a valid startup path.
                # Clear feedback left by an earlier failed launch attempt so the
                # Dashboard reports the real recovery outcome, not stale 500 text.
                for field in (
                    "workbenchLaunchError", "workbenchLaunchCode", "workbenchLaunchLog",
                    "workbenchStartPending",
                ):
                    request.pop(field, None)
                request["workbenchStarted"] = True
                request["workbenchAlreadyRunning"] = True
                request["workbenchReady"] = True
                request["workbenchPid"] = os.getpid()
                request["state"] = "accepted"
                request["startedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                self._write_continue_command(request)
                request["phase"] = "executing_task_recovery"
                self._write_continue_command(request)
                try:
                    ok = self.continue_current_project(interactive=False)
                except Exception as exc:
                    # A recovery exception must never leave a command in an
                    # ambiguous running state.  Keep all media untouched,
                    # write the exact cause into the durable mailbox, and let
                    # the Dashboard offer a fresh idempotent retry.
                    detail = f"安全继续执行异常：{type(exc).__name__}: {exc}"
                    self.status_var.set(detail)
                    self.write_log(detail)
                    request["state"] = "blocked"
                    request["phase"] = "recovery_exception"
                    request["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    request["lastHeartbeat"] = request["finishedAt"]
                    request["message"] = detail
                    self._write_continue_command(request)
                    return
                outcome = str(getattr(self, "_last_recovery_outcome", "execute_new") or "execute_new")
                if outcome == 'waiting':
                    # Claim/accept is pending in the background worker. Do
                    # not race its mailbox with this stale local request.
                    return
                if outcome in {"observe_existing", "waiting", "blocked", "terminal"}:
                    # A duplicate request or a command awaiting reconciliation
                    # is not a production failure.  Never manufacture a
                    # failed receipt from the boolean return value.
                    request["state"] = "accepted" if outcome == "observe_existing" else request.get("state", "accepted")
                    request["phase"] = "observing_existing_command" if outcome == "observe_existing" else request.get("phase", "recovery_waiting")
                    request["message"] = self.status_var.get()
                    self._write_continue_command(request)
                    return
                # continue_current_project() may have launched CapCut and
                # persisted a one-attempt marker while this stack frame still
                # holds the older request object.  Merge that durable marker
                # before writing the command result, otherwise this write
                # would erase it and the 2-second poller could launch the same
                # client again.
                try:
                    latest_request = json.loads(
                        CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig")
                    )
                except (OSError, ValueError, TypeError):
                    latest_request = {}
                if (
                    isinstance(latest_request, dict)
                    and str(latest_request.get("commandId") or "")
                    == str(request.get("commandId") or "")
                ):
                    # A CapCut launch binds a durable operation while this
                    # stack frame still owns an older copy of the command.
                    # Preserve the complete binding, not only the legacy
                    # timestamp marker, so the terminal status can always
                    # close this exact recovery command.
                    for field in (
                        "capcutRecoveryStartedAt", "boundManifestId",
                        "boundAttemptId", "boundStatusFile", "clientPid",
                        "operationGeneration", "boundOperationState",
                    ):
                        if latest_request.get(field) not in (None, ""):
                            request[field] = latest_request[field]
                # A successful recovery command may only have *started* Fish.
                # Do not claim the whole project completed before the handoff
                # confirms its task-scoped audio results.
                # The command may only become running after the current
                # task's Node/Fish process is actually created.  Do not infer
                # that from a localized UI message.
                request["state"] = "recovering" if ok else "blocked"
                request["lastHeartbeat"] = datetime.now().astimezone().isoformat(timespec="seconds")
                if request["state"] == "recovering":
                    request.pop("finishedAt", None)
                    request["phase"] = "recovery_started"
                else:
                    request["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
                request["message"] = self.status_var.get()
                self._write_continue_command(request)
                if recovery_command_id:
                    try:
                        self.recovery_control.record_execution(
                            recovery_command_id,
                            {
                                **self._recovery_execution_receipt(str(request.get("taskId") or "").strip()),
                                "mailboxState": request.get("state"),
                                "message": request.get("message"),
                                "taskId": request.get("taskId"),
                            },
                            "running" if request["state"] == "recovering" else "failed",
                        )
                    except RecoveryControlError as exc:
                        self.write_log(f"恢复回执写入失败：{exc.code}: {exc}")
        except Exception as exc:
            self.write_log(f"读取 Dashboard 继续指令失败：{exc}")
        finally:
            self.after(2000, self.poll_control_commands)

    def refresh_task_inbox(self, notify=False):
        try:
            pending = list_pending_tasks()
        except OSError as exc:
            self.inbox_info_var.set("收件箱读取失败")
            if notify:
                messagebox.showerror("外部任务读取失败", str(exc))
            return
        previous = self.inbox_task_var.get()
        self.inbox_tasks = {}
        labels = []
        for path, task in pending:
            order = ""
            if task.get("batchId"):
                order = f"任务框 {task.get('batchSequence') or '?'} / {task.get('batchSize') or '?'} · "
            label = (
                f"{order}{task.get('date')} · {task.get('project')} · {task.get('material')} "
                f"· {len(task.get('urls') or [])} 条链接 · {task.get('taskId')}"
            )
            self.inbox_tasks[label] = (path, task)
            labels.append(label)
        self.inbox_task_box["values"] = labels
        if previous in self.inbox_tasks:
            self.inbox_task_var.set(previous)
        elif labels:
            self.inbox_task_var.set(labels[0])
        else:
            self.inbox_task_var.set("")
        self.inbox_info_var.set(f"待导入 {len(labels)} 个")
        if notify:
            self.write_log(f"已扫描第1步外部任务：{len(labels)} 个待导入。")

    def import_external_task(self, auto_start=False):
        if self.download_process and self.download_process.poll() is None:
            return messagebox.showwarning("当前正在下载", "请等待当前第1步任务结束后再导入新的外部任务。")
        selected = self.inbox_tasks.get(self.inbox_task_var.get())
        if not selected:
            return messagebox.showinfo("没有待导入任务", "请先使用“扫描外部任务”，或在公司任务投递器中创建任务。")
        path, task = selected
        source_key = str(task.get("download_path_source") or "tikhub_downloads")
        source_var = self.vars.get(source_key)
        download_path = self.batch_root_sources.get(source_key) or (source_var.get().strip() if source_var else "")
        if not download_path:
            return messagebox.showerror(
                "下载目录未配置",
                f"任务要求使用工作台设置“{source_key}”，但该目录为空。\n"
                "请先在工作台设置下载目录，再重新导入。",
            )
        batch_directory = batch_download_directory(download_path, task)
        try:
            batch_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return messagebox.showerror("批次目录不可用", f"无法创建第1步批次目录：\n{batch_directory}\n\n{exc}")
        urls = task.get("urls") or []
        self.links.delete("1.0", "end")
        self.links.insert("1.0", "\n".join(urls))
        self.links.edit_modified(False)
        self.update_link_count()
        if task.get("subtitle_mode") in self.mode_cards:
            self.select_mode(task["subtitle_mode"])
        self.auto_subtitle.set(bool(task.get("subtitle_enabled", True)))
        self.vars["download_workers"].set(str(self._integer(task.get("download_workers"), 2)))
        self.vars["tikhub_downloads"].set(str(batch_directory))
        self.active_external_task = dict(task)
        self.active_external_task["step1DownloadDirectory"] = str(batch_directory)
        task["step1DownloadDirectory"] = str(batch_directory)
        mark_imported(path, task)
        self.download_batch_context = {
            "date": str(task.get("date") or ""),
            "project": str(task.get("project") or ""),
            "material": str(task.get("material") or ""),
            "runGroupId": str(task.get("runGroupId") or ""),
            "batchKind": str(task.get("batchKind") or ("examplebrand-natural-flow" if str(task.get("project") or "") == "ExampleBrand" else "formal")),
            "selectedProjects": list(task.get("selectedProjects") or []),
            "pendingImport": True,
        }
        self.last_batch_result = None
        self.last_download_runtime_failure = None
        self.download_task_errors = {}
        self.save_project_download_progress("waiting")
        self.refresh_task_inbox(False)
        if task.get("isReplenishment"):
            base_done = self._integer(task.get("replenishmentCompletedBefore"), 0)
            base_expected = self._integer(task.get("replenishmentBaseExpected"), base_done + len(urls))
            summary = (
                f"已导入补链任务：{task.get('project')} / {task.get('material')} / {len(urls)} 条。\n"
                f"原批次已完成 {base_done}/{base_expected}，本次只补这 {len(urls)} 条；"
                "右侧旧统计会在本次补链完成后合并更新。\n\n"
                f"补链仍保存到原批次目录：\n{batch_directory}\n\n"
                "请检查链接后点击“开始下载与去字幕”。"
            )
        else:
            summary = (
                f"已导入外部任务：{task.get('project')} / {task.get('material')} / "
                f"{len(urls)} 条链接。第1步将保存到：\n{batch_directory}\n\n"
                "请检查设置后手动点击“开始下载与去字幕”。"
            )
        self.status_var.set(summary)
        self.write_log(summary)
        if auto_start:
            self.status_var.set("已自动导入第 1 项，正在启动下载与去字幕。")
            self.write_log(self.status_var.get())
            self.after(450, lambda: self.start_background_download(auto_start=True))
            return True
        messagebox.showinfo("已导入到第1步", summary)
        return True

    def confirm_download_directory_cleanup(self, download_path, auto_start=False):
        try:
            download_path.mkdir(parents=True, exist_ok=True)
            videos = [
                path for path in download_path.iterdir()
                if path.is_file() and path.suffix.lower() in VIDEO_EXTS
            ]
        except OSError as exc:
            messagebox.showerror("下载目录不可用", f"无法读取或创建下载目录：\n{download_path}\n\n{exc}")
            return False
        is_replenishment = bool((self.active_external_task or {}).get("isReplenishment"))
        if is_replenishment:
            self.write_log(
                f"补链任务保留原批次中已有的 {len(videos)} 个视频，不显示清理弹窗，"
                "仅下载本次补充链接。"
            )
            return True
        if auto_start and not videos:
            self.write_log("自动任务使用新的空下载目录：跳过目录清理确认。")
            return True
        if auto_start and videos and self._prefetch_owns_directory(download_path):
            self.write_log(
                f"使用本项目的预取结果：下载目录中的 {len(videos)} 个视频均在预取开始后产生；"
                "检查点中已完成的条目不会重做。"
            )
            return True
        if auto_start and videos:
            self.write_log("自动任务发现下载目录已有视频：为防止误删，未自动开始下载。")
            self.status_var.set("自动任务等待人工确认：下载目录已有视频。")
            return False
        choice = messagebox.askyesnocancel(
            "下载前清理目录",
            f"下载目录：\n{download_path}\n\n当前层共有 {len(videos)} 个视频文件。\n\n"
            "选择“是”：删除这些视频后开始下载\n"
            "选择“否”：保留这些视频并开始下载\n"
            "选择“取消”：不开始本次下载",
        )
        if choice is None:
            self.write_log("已取消下载，未改动下载目录。")
            return False
        if choice is False:
            self.write_log(f"已保留下载目录中的 {len(videos)} 个视频，继续开始下载。")
            return True
        failures = []
        for path in videos:
            try:
                path.unlink()
            except OSError as exc:
                failures.append(f"{path.name}：{exc}")
        if failures:
            messagebox.showerror(
                "目录清理未完成",
                "部分视频无法删除，本次下载已取消：\n\n" + "\n".join(failures[:8]),
            )
            self.write_log(f"下载目录清理失败，已删除 {len(videos) - len(failures)} 个，失败 {len(failures)} 个。")
            return False
        self.write_log(f"下载前已清理目录，共删除 {len(videos)} 个视频文件。")
        self.scan_subjects(switch_view=False, notify=False)
        return True

    def _build_subtitle_repair_items(self, task_id, urls, download_path):
        """Find blur-failed items that can resume from an existing STTN file."""
        checkpoint_path = APP_DIR / "handoff" / "download" / "checkpoints" / f"{task_id}.json"
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return []
        records = checkpoint.get("items") if isinstance(checkpoint, dict) else {}
        if not isinstance(records, dict):
            return []
        result = []
        for url in urls:
            item = records.get(url) if isinstance(records.get(url), dict) else {}
            if str(item.get("status") or "") != "failed":
                continue
            code = str(item.get("errorCode") or "")
            message = str(item.get("error") or "")
            verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
            residual_verification = (
                code == "SUBTITLE_VERIFICATION_FAILED"
                and str(verification.get("verificationStatus") or "") == "residual_detected"
            )
            if not (
                code in {"SUBTITLE_BLUR_FAILED", "SUBTITLE_BLUR_PARAMETER_INVALID", "AI_SUBTITLE_PROCESSING_FAILED"}
                and ("SUBTITLE_BLUR" in message or "SUBTITLE_BLUR" in code or code == "SUBTITLE_BLUR_FAILED")
            ) and not residual_verification and code != "DOWNLOAD_OR_SUBTITLE_FAILED":
                continue
            video_id = str(url).split("?")[0].rstrip("/").split("/")[-1]
            candidates = []
            for value in item.get("files") or []:
                path = Path(str(value))
                if path.is_file() and "_ai_sttn" in path.stem.lower():
                    candidates.append(path)
            if not candidates and video_id:
                try:
                    candidates = [
                        path for path in download_path.glob(f"*{video_id}*_ai_sttn.mp4")
                        if path.is_file() and path.stat().st_size >= 1024
                    ]
                except OSError:
                    candidates = []
            if not candidates:
                continue
            sttn_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
            result.append({
                "url": url,
                "input": str(sttn_path),
                "output": str(sttn_path),
                "verification": verification,
                "originalSubtitleBands": item.get("originalSubtitleBands") or [],
                "leadingTrim": item.get("leadingTrim") if isinstance(item.get("leadingTrim"), dict) else {},
                "detectFps": float(self._integer(self.vars.get("subtitle_detect_fps").get(), 2) if self.vars.get("subtitle_detect_fps") else 2),
            })
        return result

    def start_background_download(self, auto_start=False, resume_existing=False):
        if self.download_process and self.download_process.poll() is None:
            return messagebox.showinfo("正在运行", "当前批次仍在下载。")
        prefetching = self._prefetch_running()
        if prefetching:
            # One download bridge at a time: both share the GPU subtitle service.
            self.status_var.set(f"下一项目正在预取第 1/2 步（{prefetching}），完成后再开始下载。")
            self.write_log(self.status_var.get())
            return False
        active_task = self.active_external_task if isinstance(self.active_external_task, dict) else {}
        active_task_id = str(active_task.get("taskId") or "")
        current_task = load_current_context()
        if (
            active_task_id
            and isinstance(current_task, dict)
            and str(current_task.get("taskId") or "") == active_task_id
            and current_task.get("status") == "cancelled"
        ):
            return messagebox.showwarning("任务已撤回", "此远程任务已撤回，不能再开始下载。请扫描并导入新的任务。")
        urls = self.extract_links()
        if not urls:
            return messagebox.showwarning("没有链接", "请先在左侧粘贴一个或多个视频链接。")
        runtime = Path(r"E:\TikHub_AI_Subtitle_2.0.0-ai_Portable\.cuda_env\Scripts\python.exe")
        bridge = APP_DIR / "tikhub_bridge.py"
        download_path = Path(self.vars["tikhub_downloads"].get().strip())
        if not runtime.is_file() or not bridge.is_file():
            return messagebox.showerror("环境缺失", "找不到 E 盘 CUDA Python 或后台下载桥。")
        if resume_existing:
            try:
                download_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.write_log(f"断点恢复目录不可用：{exc}")
                return False
            self.write_log("第 1/2 步断点恢复：保留下载目录和既有成片，由任务检查点跳过已验证完成项。")
        elif not self.confirm_download_directory_cleanup(download_path, auto_start=auto_start):
            return False
        mode_map = SUBTITLE_MODE_IDS
        try:
            download_workers = max(1, min(8, int(self.vars["download_workers"].get())))
        except (TypeError, ValueError):
            download_workers = 3
        self.vars["download_workers"].set(str(download_workers))
        request = {
            "urls": urls,
            "download_path": str(download_path),
            "mode": mode_map[self.mode.get()],
            "subtitle_enabled": self.auto_subtitle.get(),
            "download_workers": download_workers,
            "taskId": active_task_id,
            "batchId": str(active_task.get("batchId") or ""),
            "runGroupId": str(active_task.get("runGroupId") or ""),
            "batchKind": str(active_task.get("batchKind") or ("examplebrand-natural-flow" if str(active_task.get("project") or "") == "ExampleBrand" else "formal")),
            "selectedProjects": list(active_task.get("selectedProjects") or []),
            "batchSequence": int(active_task.get("batchSequence") or 1),
            "batchSize": int(active_task.get("batchSize") or 1),
            "date": str(active_task.get("date") or ""),
            "project": str(active_task.get("project") or ""),
            "material": str(active_task.get("material") or ""),
            "expectedCount": int(active_task.get("expectedCount") or len(urls)),
            # Preserve the replacement contract across a workbench restart.
            # Without these fields a completed gap-fill was reported as a
            # separate one-link project instead of closing its parent gap.
            "isReplenishment": bool(active_task.get("isReplenishment")),
            "replenishForTaskId": str(active_task.get("replenishForTaskId") or ""),
            "replenishmentBaseExpected": int(active_task.get("replenishmentBaseExpected") or 0),
            "replenishmentCompletedBefore": int(active_task.get("replenishmentCompletedBefore") or 0),
            "replenishmentMissingBefore": int(active_task.get("replenishmentMissingBefore") or 0),
            "resumeInterrupted": bool(resume_existing),
        }
        if resume_existing and self.auto_subtitle.get() and active_task_id:
            repair_items = self._build_subtitle_repair_items(active_task_id, urls, download_path)
            if repair_items:
                request["repairOnly"] = repair_items
                self.write_log(f"断点恢复将复用 {len(repair_items)} 条已有STTN结果，只执行模糊兜底和复检。")
        recovery = active_task.get("step12Recovery") if isinstance(active_task.get("step12Recovery"), dict) else {}
        if resume_existing and recovery:
            request.update({
                "recoveryMode": str(recovery.get("mode") or "checkpoint_rebuild"),
                "recoveryGeneration": str(recovery.get("recoveryGeneration") or ""),
                "recoveryErrorCode": str(recovery.get("errorCode") or "STEP12_RECOVERY_SESSION_REBUILT"),
                "completedBeforeRecovery": int(recovery.get("completedCount") or 0),
                "pendingUrls": list(recovery.get("pendingUrls") or []),
            })
        self.download_batch_context = {
            "date": str((self.active_external_task or {}).get("date") or ""),
            "project": str((self.active_external_task or {}).get("project") or ""),
            "material": str((self.active_external_task or {}).get("material") or ""),
            "request": request,
        }
        request_file = APP_DIR / "current_download_request.json"
        request_file.parent.mkdir(parents=True, exist_ok=True)
        request_temp = request_file.with_suffix(".tmp")
        request_temp.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(request_temp, request_file)
        self.tree.delete(*self.tree.get_children())
        self.live_tasks.clear()
        self.task_rows.clear()
        self.last_batch_result = None
        for url in urls:
            values = (url, "等待", "未开始", "等待下载", "0%", "•••", "")
            self.live_tasks[url] = values
            row = self.tree.insert("", "end", values=values)
            self.task_rows[url] = row
        self.show_task_view("live")
        self.total_var.set(str(len(urls))); self.success_var.set("0"); self.failed_var.set("0"); self.progress_var.set("0%")
        self.download_button.configure(state="disabled")
        self.status_var.set(f"正在后台处理 {len(urls)} 个链接")
        self.save_download_session()
        self.save_project_download_progress("running")
        self._set_flow_active(0)
        self.write_log(f"后台任务已启动，共 {len(urls)} 个链接，并发下载 {download_workers} 个；AI 去字幕按顺序逐个处理。")
        bridge_environment = os.environ.copy()
        bridge_environment["PYTHONIOENCODING"] = "utf-8"
        bridge_environment["PYTHONUTF8"] = "1"
        self.download_process = subprocess.Popen(
            [str(runtime), "-u", str(bridge), str(request_file)],
            cwd=str(APP_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="strict", env=bridge_environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self.read_download_events, daemon=True).start()
        return True

    def read_download_events(self):
        assert self.download_process and self.download_process.stdout
        for line in self.download_process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.after(0, lambda e=event: self.handle_download_event(e))
        code = self.download_process.wait()
        self.after(0, lambda: self.finish_background_download(code))

    def handle_download_event(self, event):
        kind = event.get("type")
        if kind == "task":
            url = event.get("url", "")
            stage_map = {"waiting": "等待", "resolve": "解析链接", "download": "正在下载", "queue": "等待 AI 去字幕", "subtitle": "正在去字幕", "blur": "正在去字幕", "ocr": "OCR 检测", "sttn": "STTN 修复", "encode": "NVENC 编码", "trim": "裁剪前导封面", "complete": "等待拼接配置", "failed": "处理失败"}
            stage = stage_map.get(event.get("stage"), event.get("stage", "处理中"))
            progress = event.get("progress")
            status = "失败" if event.get("status") == "failed" else ("排队中" if progress is None else f"{progress}%")
            event_stage = str(event.get("stage") or "")
            error_code = str(event.get("errorCode") or "")
            phase = str(event.get("phase") or "")
            subtitle_error = (
                event_stage == "failed"
                and (
                    phase.startswith("subtitle")
                    or "SUBTITLE" in error_code
                    or error_code.startswith("AI_SUBTITLE")
                )
            )
            explicit_download_status = str(event.get("downloadStatus") or "").strip().lower()
            explicit_subtitle_status = str(event.get("subtitleStatus") or "").strip().lower()
            if event_stage == "failed":
                # A subtitle/blur failure happens after a valid source has
                # already been downloaded.  Never present it as a download
                # failure merely because the bridge emits one terminal event.
                if explicit_download_status in {"completed", "complete", "已完成"} or subtitle_error:
                    download_status = "已完成"
                elif explicit_download_status in {"failed", "failure", "失败"}:
                    download_status = "失败"
                else:
                    download_status = "处理中"
                if explicit_subtitle_status in {"not_requested", "未开始"}:
                    subtitle_status = "未开始"
                elif subtitle_error or explicit_subtitle_status in {"failed", "failure", "失败"}:
                    subtitle_status = "失败"
                else:
                    subtitle_status = "未开始"
            else:
                download_status = "已完成" if event_stage in ("queue", "subtitle", "blur", "ocr", "sttn", "encode", "trim", "complete") else "处理中"
                subtitle_status = "已完成" if event_stage == "complete" else ("等待中" if event_stage == "queue" else ("处理中" if event_stage in ("subtitle", "blur", "ocr", "sttn", "encode") else "未开始"))
            files = event.get("files") or []
            path = next((x for x in reversed(files) if isinstance(x, str) and Path(x).suffix.lower() in VIDEO_EXTS), "")
            old_path = self.live_tasks.get(url, ("", "", "", "", "", "", ""))[6]
            values = (url, download_status, subtitle_status, stage, status, "•••", path or old_path)
            self.live_tasks[url] = values
            row = self.task_rows.get(url)
            if row and self.tree.exists(row):
                self.tree.item(row, values=values)
            else:
                self.task_rows[url] = self.tree.insert("", "end", values=values)
            if event.get("stage") == "complete" and self.remove_completed_link(url):
                self.write_log(f"已从待处理链接中移除完成项：{url}")
            if event.get("stage") in ("subtitle", "blur"):
                self._set_flow_active(0)
            message = event.get("message")
            if message:
                self.write_log(message)
            if event.get("stage") == "failed":
                self.download_task_errors[url] = {
                    "errorCode": error_code or "DOWNLOAD_ITEM_FAILED",
                    "phase": phase or "download_or_subtitle",
                    "message": str(message or "下载或 AI 去字幕失败"),
                    "downloadStatus": download_status,
                    "subtitleStatus": subtitle_status,
                    "verification": event.get("verification") if isinstance(event.get("verification"), dict) else {},
                    "files": [str(value) for value in files if isinstance(value, str)],
                }
            self.save_download_session()
            self.save_project_download_progress("running")
        elif kind == "overall":
            self.status_var.set(f"总进度 {event.get('current')}/{event.get('total')} · 成功 {event.get('succeeded')} · 失败 {event.get('failed')}")
            total = max(int(event.get("total") or 0), 1)
            self.success_var.set(str(event.get("succeeded", 0))); self.failed_var.set(str(event.get("failed", 0)))
            self.progress_var.set(f"{int(int(event.get('current') or 0) / total * 100)}%")
        elif kind == "complete":
            total = int(event.get("total") or len(self.live_tasks))
            succeeded = int(event.get("succeeded") or 0)
            failed = int(event.get("failed") or 0)
            self.last_batch_result = {"total": total, "succeeded": succeeded, "failed": failed}
            self.total_var.set(str(total))
            self.success_var.set(str(succeeded))
            self.failed_var.set(str(failed))
            self.progress_var.set("100%")
            self.write_log(f"批次完成：成功 {event.get('succeeded')}，失败 {event.get('failed')}。")
            self.save_download_session()
        elif kind == "info":
            self.write_log(event.get("message", "后台任务信息"))
        elif kind == "fatal":
            self.last_download_runtime_failure = {
                "errorCode": str(event.get("errorCode") or "DOWNLOAD_BRIDGE_RUNTIME_FAILED"),
                "phase": str(event.get("phase") or "bridge_runtime"),
                "message": str(event.get("message") or "未知错误"),
                "observedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self.write_log("后台任务错误：" + self.last_download_runtime_failure["message"])
            self.save_download_session()

    def finish_background_download(self, code):
        self.download_button.configure(state="normal")
        product_count = self.scan_subjects(switch_view=False, notify=False)
        if code == 0:
            result = self.last_batch_result or {}
            self.status_var.set(
                f"后台任务已完成 · 成功 {result.get('succeeded', 0)} · 失败 {result.get('failed', 0)} · 已发现 {product_count} 个成品"
            )
            self.write_log(f"后台处理真正结束，成品目录已刷新，共发现 {product_count} 个成品。")
        else:
            if self.last_batch_result is None:
                if not self.last_download_runtime_failure:
                    self.last_download_runtime_failure = {
                        "errorCode": "DOWNLOAD_BRIDGE_RUNTIME_FAILED",
                        "phase": "bridge_runtime",
                        "message": f"下载桥异常退出（退出码 {code}）",
                        "observedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                self.failed_var.set(str(len(self._download_failure_items())))
                self.status_var.set(
                    f"后台任务运行中断 · 链接仍为 {len(self.live_tasks)}/{self._expected_download_count()} · 可安全继续"
                )
                self.show_task_view("live")
                self.save_download_session()
                self.save_project_download_progress("runtime_failed")
                self._set_flow_active(0)
                self.write_log("下载桥未写入终态：保留全部原始链接和检查点，不生成补链缺口。")
                return
            self.status_var.set(f"后台任务异常结束 · 已发现 {product_count} 个成品")
        self.show_task_view("live")
        self.save_download_session()
        self.save_project_download_progress("finished")
        missing_count, downstream_ready = self._close_step12_replenishment()
        if missing_count:
            self.status_var.set(
                f"当前项目已暂停：成功 {self.success_var.get()} / 计划 {self._expected_download_count()}，"
                f"还差 {missing_count} 条；请在任务投递器补链后再继续。"
            )
            self.write_log(self.status_var.get())
            self._set_flow_active(0)
        remote_queue_task = self._is_remote_auto_sequence_task(self.active_external_task or {})
        if self._integer((self.last_batch_result or {}).get("failed")) > 0 or missing_count or not downstream_ready:
            if remote_queue_task:
                # The laptop submitter polls the Dashboard and shows the gap
                # alert there.  A modal on the unattended home PC would block
                # the Tk event loop and prevent a later remote replenishment.
                self.write_log("项目缺口已同步到远程投递器；等待补链后再继续该批次。")
            else:
                self.after(300, self.show_batch_completeness)
        if not missing_count and downstream_ready:
            if code != 0:
                self.write_log("下载进程返回异常代码，但父项目清单与有效成品已对账完整；继续按下一未完成步骤恢复。")
            self._set_flow_active(1)
            # A completed project releases only its immediate successor.  A
            # blocked project never reaches this branch, so the company-side
            # gap prompt is always handled before the following project runs.
            # A remote task box is a full production queue.  Do not overwrite
            # this task's voice/stitch/CapCut context with the next project
            # before the calibrated Step 5 client has produced final output.
            self._advance_after_step12()

    def _auto_launch_voice_after_download(self, force_retry=False):
        """Honour the submitter's explicit new-voice decision after Step 1/2.

        Reuse tasks never open a browser.  Any subtitle/download failure blocks
        automatic voice generation so a partial source batch cannot continue.
        """
        task = self.active_external_task or load_current_context()
        if not isinstance(task, dict):
            return
        voice = self._task_voice_profile(task)
        should_create = bool(voice.get("requiresNewGeneration")) or voice.get("voicePlan") == "create_new"
        task_id = canonical_task_id(task)
        result = self.last_batch_result or {}
        if not task_id:
            return
        if self._integer(result.get("failed")) > 0 or self._expected_download_count() > self._integer(self.success_var.get()):
            self.write_log("第 1/2 步存在失败项：已停止自动启动语音客户端，请处理失败链接后重试。")
            return
        if not should_create:
            if not self._is_remote_auto_sequence_task(task) or task_id in self.stitch_auto_launch_task_ids:
                return False
            audio_dir, audio_error = self._validate_reuse_audio_contract(task)
            if audio_error:
                detail = f"复用音频合同无效：{audio_error}"
                self.last_voice_launch_error = {
                    "taskId": task_id,
                    "errorCode": "VOICE_REUSE_AUDIO_CONTRACT_INVALID",
                    "message": detail,
                }
                self._set_remote_pipeline_status(task_id, "blocked_voice_config", detail)
                self.status_var.set(detail)
                self.write_log(detail)
                self._finish_continue_command(task_id, "blocked", detail)
                return False
            target = self._match_stitch_project(str(task.get("project") or ""), str(task.get("material") or ""))
            if not target:
                self._set_remote_pipeline_status(task_id, "blocked_stitch", "未找到对应的第 4 步拼接项目")
                self.write_log("远程第 4 步未启动：找不到与项目/素材对应的拼接配置。")
                return False
            self.status_var.set("第 1/2 步完成，正在使用已存在语音自动进入第 4 步。")
            self.write_log(self.status_var.get())
            self._set_remote_pipeline_status(task_id, "voice_completed", "已使用当前项目已有配音，正在进入第 4 步")
            self.vars["audio_dir"].set(str(audio_dir))
            self.show_stitch_page(False, target_project=target)
            self.after(700, lambda value=task_id: self._start_remote_stitch(value))
            return True
        if task_id in self.voice_auto_launch_task_ids:
            if not force_retry:
                return False
            if self.voice_batch_process and self.voice_batch_process.poll() is None:
                self.write_log("第 3 步配音仍在运行；安全继续不会启动第二个语音进程。")
                return False
            self._release_voice_launch_guard(task_id)
        if self.voice_batch_process and self.voice_batch_process.poll() is None:
            detail = "其他项目的第 3 步配音仍在运行；当前项目保持等待，避免两个Fish批次争用同一浏览器。"
            self._set_remote_pipeline_status(task_id, "voice_pending", detail)
            self.write_log(detail)
            return False
        self.voice_auto_launch_task_ids.add(task_id)
        self._set_remote_pipeline_status(task_id, "voice_pending", "第 1/2 步已完整，正在启动当前项目第 3 步配音")
        self.status_var.set(f"第 1/2 步完成，正在按任务指令启动语音客户端：{voice.get('audioBatch')}")
        self.write_log(self.status_var.get())
        def launch_for_task(value=task_id):
            active = self.active_external_task or load_current_context() or {}
            if canonical_task_id(active) != value:
                self._release_voice_launch_guard(value)
                self.write_log("当前任务已变化，取消过期的语音启动请求。")
                return False
            launch_result = self.launch_voice_client(unattended=True)
            if launch_result == VOICE_LAUNCH_STARTED or launch_result is True:
                self.voice_launch_records[value] = {
                    "process": self.voice_batch_process,
                    "nodePid": int(getattr(self.voice_batch_process, "pid", 0) or 0),
                    "startedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                    # The bridge writes the real ID asynchronously.  It is
                    # bound in poll_voice_handoff only after a matching
                    # running handoff appears.
                    "batchId": "",
                }
                self._set_remote_pipeline_status(value, "voice_running", "第 3 步配音正在运行")
                return True
            elif launch_result in {VOICE_LAUNCH_ALREADY_STARTING, VOICE_LAUNCH_ALREADY_RUNNING}:
                state = "voice_running" if launch_result == VOICE_LAUNCH_ALREADY_RUNNING else "voice_starting"
                detail = (
                    "当前任务第 3 步配音正在运行；已忽略重复安全继续"
                    if launch_result == VOICE_LAUNCH_ALREADY_RUNNING
                    else "当前任务第 3 步配音正在启动；已忽略重复安全继续"
                )
                self._set_remote_pipeline_status(value, state, detail)
                self.status_var.set(detail)
                return True
            elif launch_result == VOICE_LAUNCH_BLOCKED_OTHER_TASK:
                self._release_voice_launch_guard(value)
                detail = "其他任务正在使用Fish；当前任务保持排队，未启动第二个配音进程。"
                self._set_remote_pipeline_status(
                    value, "voice_pending", detail, error_code="VOICE_BUSY_OTHER_TASK",
                )
                self.status_var.set(detail)
                self._finish_continue_command(value, "blocked", detail, phase="voice_busy_other_task")
                return True
            else:
                self._release_voice_launch_guard(value)
                launch_state = self._read_local_json_object(self._voice_launch_state_file())
                error_code = str(launch_state.get("errorCode") or "VOICE_CLIENT_LAUNCH_FAILED")
                detail = str(launch_state.get("message") or "第 3 步配音未能启动；可安全继续重试")
                self._set_remote_pipeline_status(
                    value, "blocked_voice", detail,
                    error_code=error_code,
                )
                return False
        # This path is reached by Dashboard's unattended safety command.
        # Do not leave the actual process creation in a deferred Tk callback:
        # the command could otherwise be recorded as completed while no Fish
        # plan or Node process was ever created.  Popen itself returns
        # immediately, so this remains non-blocking for the desktop UI.
        return launch_for_task()

    def launch_stitcher(self):
        path = Path(self.vars["stitcher_launcher"].get())
        if not path.is_file():
            return messagebox.showerror("无法启动", f"找不到拼接工具：\n{path}")
        # Do not use os.startfile for this .pyw launcher.  A shell file
        # association can route the request through a script host and lose
        # the quotation around a path such as
        # ``E:\\Backup\\Documents\\New project 3\\...``.  That produces the
        # misleading “没有文件扩展” dialog for ``...\\New``.  Starting the
        # script through the interpreter that is already running Workbench
        # keeps the script path as one argv item and bypasses file association.
        try:
            subprocess.Popen(
                [sys.executable, str(path)],
                cwd=str(path.parent),
                shell=False,
            )
        except OSError as exc:
            detail = f"第 4 步拼接器无法启动：{exc}"
            self.status_var.set(detail)
            self.write_log(detail)
            active = self.active_external_task if isinstance(self.active_external_task, dict) else {}
            task_id = canonical_task_id(active)
            if task_id:
                self._set_remote_pipeline_status(task_id, "blocked_stitch", detail)
                self._finish_continue_command(task_id, "blocked", detail)
            return messagebox.showerror("拼接器启动失败", detail)
        self.status_var.set("已启动 AI 素材拼接器；第 4 步状态将在生成记录回传后更新。")
        self.write_log(self.status_var.get())

    def scan_subjects(self, switch_view=True, notify=True):
        root = Path(self.vars["tikhub_downloads"].get().strip())
        if not root.is_dir():
            if notify:
                messagebox.showwarning("目录无效", "请先选择有效的 TikHub 下载目录。")
            return 0
        root_label = "未分类（下载目录）"
        locations = [(root_label, root)]
        locations.extend(
            (folder.name, folder)
            for folder in sorted(root.iterdir(), key=lambda path: path.name.lower())
            if folder.is_dir() and not folder.name.startswith(".")
        )
        catalog = {}
        category_paths = {}
        for category, folder in locations:
            files = [
                path for path in folder.iterdir()
                if path.is_file()
                and path.suffix.lower() in VIDEO_EXTS
                and any(path.stem.lower().endswith(suffix) for suffix in PROCESSED_SUFFIXES)
                and not any(x in path.name.lower() for x in ("preview", "预览"))
            ]
            if files:
                files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
                catalog[category] = files
                category_paths[category] = folder
        previous = self.product_category_var.get()
        categories = list(catalog)
        self.product_catalog = catalog
        self.product_category_paths = category_paths
        self.product_category_combo.configure(values=categories)
        if previous in catalog:
            self.product_category_var.set(previous)
        elif root_label in catalog:
            self.product_category_var.set(root_label)
        elif categories:
            self.product_category_var.set(categories[0])
        else:
            self.product_category_var.set("")
        self.refresh_product_tree()
        total_files = sum(len(files) for files in catalog.values())
        self.file_count_var.set(f"{total_files} 个视频")
        scanned_at = datetime.now().strftime("%H:%M:%S")
        self.product_info_var.set(f"{scanned_at} 扫描 · {len(catalog)} 个分类 · {total_files} 个成品")
        self.write_log(f"扫描完成：找到 {len(catalog)} 个材料分类、{total_files} 个去字幕成品。")
        if switch_view:
            self.show_task_view("products")
        return total_files

    def refresh_product_tree(self):
        self.product_tree.delete(*self.product_tree.get_children())
        category = self.product_category_var.get().strip()
        for path in self.product_catalog.get(category, [])[:500]:
            modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self.product_tree.insert(
                "", "end",
                values=(category, path.name, "文件存在", "已完成", "可用于拼接", modified, str(path)),
            )

    def handoff(self):
        try:
            self._stage_selected_subjects(required=True)
        except ValueError as exc:
            return messagebox.showwarning("无法进入拼接", str(exc))
        self.show_stitch_page(False)

    def on_stitch_records(self, project, records, record_path):
        task = self.active_external_task or {}
        owner_id = canonical_task_id(task)
        if owner_id:
            production_date = str(
                task.get("productionDate") or task.get("date") or ""
            ).strip()
            is_examplebrand = str(task.get("batchKind") or "") == "examplebrand-natural-flow"
            records = [{
                **item,
                "remotePipelineTaskId": owner_id,
                "remotePipelineTaskIdSource": "examplebrand_output_plan" if is_examplebrand else "formal_handoff",
                "formalTaskIdentity": True,
                # ExampleBrand owns 21 output identities below one receiver task. Formal
                # batches retain the historical one-owner identity unchanged.
                "canonicalTaskId": str(item.get("canonicalTaskId") or owner_id) if is_examplebrand else owner_id,
                "parentCanonicalTaskId": owner_id if is_examplebrand else "",
                "batchId": str(task.get("batchId") or "").strip(),
                "runGroupId": str(task.get("runGroupId") or "").strip(),
                "productionDate": production_date,
                "expectedTaskCount": contract_expected_count(task),
            } for item in records]
            try:
                persist_formal_stitch_identity(record_path, records)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                detail = (
                    "第 4 步任务身份无法持久化，已阻止进入 CapCut："
                    f"{exc}"
                )
                self._set_remote_pipeline_status(
                    owner_id, "blocked_stitch", detail,
                    error_code="STITCH_TASK_IDENTITY_PERSIST_FAILED",
                )
                self.status_var.set(detail)
                self.write_log(detail)
                self._finish_continue_command(owner_id, "blocked", detail)
                return
        succeeded = sum(1 for item in records if str(item.get("status")) == "success")
        failed = len(records) - succeeded
        expected_count = contract_expected_count(task) if owner_id else 0
        self.last_stitch_result = {
            "state": "failed" if failed else "completed",
            "stateText": "拼接部分失败" if failed else "AI素材拼接完成",
            "total": len(records),
            "completed": succeeded,
            "failed": failed,
            "progress": 100,
            "project": project,
            "recordPath": str(record_path or ""),
        }
        if owner_id and (failed or (expected_count and succeeded != expected_count)):
            detail = (
                f"第 4 步输出数量不符合正式清单：应有 {expected_count} 条，"
                f"实际成功 {succeeded} 条，失败 {failed} 条。已保留生成文件，未创建第五步任务。"
            )
            self._set_remote_pipeline_status(
                owner_id, "blocked_stitch", detail,
                error_code="STITCH_CONTRACT_COUNT_MISMATCH",
            )
            self.status_var.set(detail)
            self.write_log(detail)
            self._finish_continue_command(owner_id, "blocked", detail)
            return
        added = self.capcut_store.ingest_records(project, records, record_path)
        self.write_log(f"第四步已交接到第五步：新增 {added} 条 CapCut 任务。")
        self.status_var.set(f"第四步完成 · 第五步新增 {added} 条任务")
        if owner_id:
            self._set_remote_pipeline_status(owner_id, "capcut_pending", "第 4 步完成，正在准备第 5 步CapCut")
            # A handoff is not project completion.  Keep the command in the
            # recoverable state until the formal Step 5 manifest has a valid
            # final output for every expected task.
            self._update_continue_command_phase(owner_id, "capcut_handoff_created", "第 4 步已交接至 CapCut，正在等待正式成片校验。")
        if self.current_page == "capcut":
            self.refresh_capcut_tasks(False)

        if self._is_remote_auto_sequence_task(task) and succeeded and not failed:
            date_value = str(task.get("date") or "").strip()
            task_id = canonical_task_id(task)
            record_value = str(Path(record_path).resolve()) if record_path else ""
            if date_value and task_id and record_value:
                payload = self.capcut_store.load_date(date_value)
                capcut = (task.get("pipelineProfile") or {}).get("5_capcut") or {}
                expected_project = str(capcut.get("category") or task.get("project") or "").strip()
                expected_count = contract_expected_count(task)
                generated_ids = {
                    str(item.get("taskId") or "")
                    for item in payload.get("tasks") or []
                    if str(item.get("generationRecord") or "") == record_value
                    and str(item.get("project") or "") == expected_project
                    and str(item.get("status") or "") in RUNNABLE_STATUSES
                }
                if not expected_count:
                    self._set_remote_pipeline_status(task_id, "blocked_capcut", "第 5 步没有配置该项目的正式目标数量")
                    self.write_log(f"Remote Step 5 blocked: unknown production target for {expected_project}")
                elif len(generated_ids) != expected_count:
                    self._set_remote_pipeline_status(
                        task_id,
                        "blocked_capcut",
                        f"第 5 步正式清单不完整：应有 {expected_count} 条，当前仅有 {len(generated_ids)} 条",
                    )
                    self.write_log(f"Remote Step 5 blocked: expected {expected_count}, generated {len(generated_ids)}")
                else:
                    self.after(
                        800,
                        lambda value=task_id, day=date_value, ids=generated_ids: self._start_remote_capcut(value, day, ids),
                    )

    def _release_capcut_launch_guard(self, task_id):
        """Release a launch guard only after its exact child process ended."""
        task_id = str(task_id or "").strip()
        record = self.capcut_launch_records.get(task_id) or {}
        process = record.get("process")
        if process is not None and process.poll() is None:
            return False
        if task_id in self.capcut_auto_launch_task_ids:
            self.capcut_auto_launch_task_ids.discard(task_id)
            self.capcut_launch_records.pop(task_id, None)
            if process is not None:
                self.write_log(f"CapCut client exited for {task_id}; recovery lock released.")
        return True

    def _has_durable_capcut_final(self, item, upload_tasks=None):
        """A completed Step 5 item remains valid after cloud-verified local cleanup."""
        output = Path(str(item.get("finalOutputPath") or ""))
        if output.is_file() and output.stat().st_size > 0:
            return True
        if upload_tasks is None:
            payload = load_baidu_json(BAIDU_STATE_FILE, {"tasks": {}})
            upload_tasks = payload.get("tasks") if isinstance(payload, dict) else {}
        uploaded = upload_tasks.get(str(item.get("taskId") or "")) if isinstance(upload_tasks, dict) else None
        return bool(
            isinstance(uploaded, dict)
            and str(uploaded.get("status") or "") == "uploaded"
            and self._integer(uploaded.get("cloudSize"), 0) > 0
            and str((uploaded.get("cleanup") or {}).get("cloudVerifiedAt") or "")
        )

    def _start_remote_capcut(self, task_id, date_value, task_ids, release_only=False):
        """Create a narrow manifest and launch calibrated CapCut for one received task."""
        task = self.active_external_task or {}
        if str(task.get("taskId") or "") != task_id:
            return False
        previous_exited, previous_detail = self._wait_for_previous_capcut_client_exit(task_id)
        if not previous_exited:
            self._set_remote_pipeline_status(
                task_id, "blocked_capcut", previous_detail,
                error_code="CAPCUT_OWNED_PROCESS_EXIT_TIMEOUT",
            )
            self.status_var.set(previous_detail)
            self.write_log(f"Remote Step 5 not started: {previous_detail}")
            return False
        if not self._release_capcut_launch_guard(task_id):
            return True
        payload = self.capcut_store.load_date(date_value)
        if release_only and not self._continue_command_is_recovering_task(task_id):
            # Draft housekeeping is never a background production launch. It
            # is only started while the explicit Safe Continue/recovery
            # command owns this task.
            return False
        if release_only:
            try:
                manifest = self.capcut_store.create_release_manifest(
                    date_value, set(task_ids)
                )
                executable = Path(self.vars["capcut_client"].get().strip())
                self.capcut_manifest = manifest
                self.capcut_process = launch_capcut_client(executable, manifest)
                self.capcut_auto_launch_task_ids.add(task_id)
                self.capcut_launch_records[task_id] = {
                    "manifestId": str(manifest),
                    "pid": int(getattr(self.capcut_process, "pid", 0) or 0),
                    "process": self.capcut_process,
                }
                self._bind_continue_command_to_capcut(
                    task_id, manifest, self.capcut_process
                )
                self._set_remote_pipeline_status(
                    task_id, "capcut_running", "正在恢复已完成成片的草稿目录"
                )
                self.status_var.set("正在恢复 CapCut 草稿目录，不会重新剪辑已完成成片")
                self.write_log(f"Remote Step 5 release-only manifest: {manifest.name}")
                self.refresh_capcut_tasks(False)
                return True
            except Exception as exc:
                self.status_var.set("草稿目录恢复未启动，成片保持已完成")
                self._set_remote_pipeline_status(
                    task_id,
                    "capcut_draft_release_pending",
                    f"草稿目录恢复未启动：{exc}",
                    error_code="CAPCUT_DRAFT_RELEASE_PENDING",
                )
                self.write_log(f"Remote Step 5 release-only recovery was not started: {exc}")
                return False
        runnable = {
            str(item.get("taskId") or "")
            for item in payload.get("tasks") or []
            if str(item.get("taskId") or "") in set(task_ids)
            and str(item.get("status") or "") in RUNNABLE_STATUSES
        }
        if not runnable:
            message = "第 5 步没有可交给 CapCut 的已校验成片。"
            self._set_remote_pipeline_status(
                task_id, "blocked_capcut", message,
                error_code="CAPCUT_NO_RUNNABLE_TASKS",
            )
            self.write_log(f"Remote Step 5 did not start: {message}")
            return False
        try:
            # Bind the immutable project contract before creating the external
            # manifest.  CapCut then returns the same owner/count in every
            # status update, including after a restart.
            expected_project = str((task.get("pipelineProfile") or {}).get("5_capcut", {}).get("category") or task.get("project") or "").strip()
            expected_count = contract_expected_count(task)
            persisted = self.capcut_store.load_date(date_value)
            owned = [
                item for item in persisted.get("tasks") or []
                if str(item.get("remotePipelineTaskId") or "").strip() == task_id
            ]
            upload_payload = load_baidu_json(BAIDU_STATE_FILE, {"tasks": {}})
            upload_tasks = upload_payload.get("tasks") if isinstance(upload_payload, dict) else {}
            project_task_ids = {
                str(item.get("taskId") or "").strip()
                for item in owned
                if str(item.get("taskId") or "").strip()
            }
            completed_task_ids = {
                str(item.get("taskId") or "").strip()
                for item in owned
                if str(item.get("status") or "") == "completed"
                and self._has_durable_capcut_final(item, upload_tasks)
            }
            covered_ids = completed_task_ids | set(runnable)
            if (
                not expected_count
                or len(project_task_ids) != expected_count
                or covered_ids != project_task_ids
            ):
                message = (
                    f"第 5 步正式清单不完整：应有 {expected_count or '?'} 条，"
                    f"已验证完成 {len(completed_task_ids)} 条，本次可补跑 {len(runnable)} 条，"
                    f"正式任务 {len(project_task_ids)} 条"
                )
                self._set_remote_pipeline_status(
                    task_id, "blocked_capcut", message,
                    error_code="CAPCUT_FORMAL_MANIFEST_INCOMPLETE",
                )
                self.write_log(f"Remote Step 5 did not start: {message}")
                return False
            for item in persisted.get("tasks") or []:
                if str(item.get("taskId") or "") in runnable:
                    item["remotePipelineTaskId"] = task_id
                    item["expectedTaskCount"] = expected_count
                    item["projectExpectedCount"] = expected_count
                    item["attemptTaskCount"] = len(runnable)
            self.capcut_store.save_date(date_value, persisted)
            manifest = self.capcut_store.create_manifest(
                date_value,
                runnable,
                project_task_ids=project_task_ids,
                completed_task_ids=completed_task_ids,
            )
            executable = Path(self.vars["capcut_client"].get().strip())
            self.capcut_manifest = manifest
            self.capcut_process = launch_capcut_client(executable, manifest)
            self.capcut_auto_launch_task_ids.add(task_id)
            self.capcut_launch_records[task_id] = {
                "manifestId": str(manifest),
                "pid": int(getattr(self.capcut_process, "pid", 0) or 0),
                "process": self.capcut_process,
            }
            self._bind_continue_command_to_capcut(
                task_id, manifest, self.capcut_process
            )
            self._set_remote_pipeline_status(
                task_id,
                "capcut_preflight",
                "CapCut客户端已启动，正在校验并恢复任务草稿",
            )
            self.status_var.set(
                f"CapCut客户端已启动，正在进行草稿预检：{len(runnable)} 条"
            )
            self.write_log(f"Remote Step 5 manifest: {manifest.name}")
            self.refresh_capcut_tasks(False)
            return True
        except Exception as exc:
            self.status_var.set("Remote Step 5 could not start; the task was kept for review.")
            self._set_remote_pipeline_status(
                task_id, "blocked_capcut", f"第 5 步 CapCut 未启动：{exc}",
                error_code="CAPCUT_CLIENT_LAUNCH_FAILED",
            )
            self.write_log(f"Remote Step 5 was not started: {exc}")
            return False

    def _generation_record_roots(self):
        roots = []
        if self.stitcher_controller is not None:
            app = self.stitcher_controller.app
            roots.extend(
                Path(str(project.get("output_dir", "")).strip())
                for project in app.projects.values()
                if str(project.get("output_dir", "")).strip()
            )
        project_file = Path(self.vars["stitcher_projects"].get().strip())
        try:
            payload = json.loads(project_file.read_text(encoding="utf-8"))
            projects = payload.get("projects", payload) if isinstance(payload, dict) else {}
            if isinstance(projects, dict):
                roots.extend(
                    Path(str(project.get("output_dir", "")).strip())
                    for project in projects.values()
                    if isinstance(project, dict) and str(project.get("output_dir", "")).strip()
                )
        except (OSError, ValueError):
            pass
        unique = []
        seen = set()
        for root in roots:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

    def refresh_capcut_tasks(self, recover=True):
        try:
            if recover:
                added = self.capcut_store.recover_generation_records(self._generation_record_roots())
                synced = self.capcut_store.sync_statuses()
                if added:
                    self.write_log(f"从第四步历史生成记录恢复了 {added} 条第五步任务。")
                if synced:
                    self.write_log(f"已从 CapCut 客户端同步 {synced} 条任务状态。")
            dates = self.capcut_store.dates()
            self.capcut_date_combo.configure(values=dates)
            if self.capcut_date_var.get() not in dates:
                self.capcut_date_var.set(dates[0] if dates else "")
            self.capcut_tree.delete(*self.capcut_tree.get_children())
            date_value = self.capcut_date_var.get()
            if not date_value:
                self.capcut_summary_var.set("尚无第四步成功记录。完成第四步后会自动出现在这里。")
                return
            payload = self.capcut_store.load_date(date_value)
            visible = 0
            selected = 0
            invalid = 0
            completed = 0
            for task in payload["tasks"]:
                if not self.capcut_project_vars.get(task.get("project"), tk.BooleanVar(value=False)).get():
                    continue
                status = task.get("status", "waiting_capcut")
                status_group = (
                    "completed" if status == "completed"
                    else "failed" if status in {"failed", "stopped", "validation_failed", "manual_review"}
                    else "pending"
                )
                if not self.capcut_status_vars[status_group].get():
                    continue
                visible += 1
                if task.get("selected") and status != "completed":
                    selected += 1
                if status in {"validation_failed", "manual_review"}:
                    invalid += 1
                if status == "completed":
                    completed += 1
                tag = "error" if status in {"validation_failed", "failed", "manual_review"} else ("completed" if status == "completed" else "")
                self.capcut_tree.insert(
                    "",
                    "end",
                    iid=task["taskId"],
                    values=(
                        "☑" if task.get("selected") and status != "completed" else "☐",
                        task.get("project", ""),
                        task.get("material", ""),
                        task.get("language", ""),
                        Path(task.get("videoPath", "")).name,
                        Path(task.get("audioPath", "")).name,
                        task.get("finalOutputPath", ""),
                        STATUS_LABELS.get(status, status),
                        task["taskId"],
                    ),
                    tags=(tag,) if tag else (),
                )
            self.capcut_summary_var.set(
                f"{date_value} · 当前显示 {visible} 条 · 已勾选 {selected} 条 · 异常 {invalid} 条 · 已完成 {completed} 条"
            )
        except Exception as exc:
            self.write_log(f"刷新第五步失败：{exc}")
            messagebox.showerror("第五步刷新失败", str(exc))

    def on_capcut_tree_click(self, event):
        row = self.capcut_tree.identify_row(event.y)
        column = self.capcut_tree.identify_column(event.x)
        if not row or column != "#1":
            return
        date_value = self.capcut_date_var.get()
        payload = self.capcut_store.load_date(date_value)
        task = next((item for item in payload["tasks"] if item.get("taskId") == row), None)
        if not task or task.get("status") in {"completed", "validation_failed", "manual_review"}:
            return "break"
        self.capcut_store.set_selected(date_value, {row}, not bool(task.get("selected")))
        self.refresh_capcut_tasks(False)
        return "break"

    def select_capcut_visible(self, selected):
        ids = set(self.capcut_tree.get_children())
        if not ids:
            return
        self.capcut_store.set_selected(self.capcut_date_var.get(), ids, selected)
        self.refresh_capcut_tasks(False)

    def _selected_capcut_task(self):
        row = self.capcut_tree.focus()
        if not row:
            messagebox.showinfo("请选择任务", "请先在表格中选择一条任务。")
            return None
        payload = self.capcut_store.load_date(self.capcut_date_var.get())
        return next((item for item in payload["tasks"] if item.get("taskId") == row), None)

    def edit_capcut_task(self):
        task = self._selected_capcut_task()
        if not task:
            return
        dialog = tk.Toplevel(self)
        dialog.title("编辑第五步任务")
        dialog.geometry("760x360")
        dialog.transient(self)
        dialog.grab_set()
        fields = {}
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        specs = (
            ("project", "项目"),
            ("material", "材料"),
            ("language", "语言"),
            ("audioPath", "对应音频"),
            ("finalOutputPath", "最终输出文件"),
        )
        for row, (key, label) in enumerate(specs):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=7)
            value = tk.StringVar(value=str(task.get(key, "")))
            fields[key] = value
            ttk.Entry(body, textvariable=value).grid(row=row, column=1, sticky="ew", padx=10, pady=7)
            if key == "audioPath":
                ttk.Button(
                    body,
                    text="选择",
                    command=lambda var=value: (
                        lambda path: var.set(path) if path else None
                    )(filedialog.askopenfilename(title="选择对应音频", filetypes=[("音频", "*.mp3;*.wav;*.m4a;*.aac"), ("所有文件", "*.*")])),
                ).grid(row=row, column=2)
            elif key == "finalOutputPath":
                ttk.Button(
                    body,
                    text="选择",
                    command=lambda var=value: (
                        lambda path: var.set(path) if path else None
                    )(filedialog.asksaveasfilename(title="选择最终成片文件", defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])),
                ).grid(row=row, column=2)
        body.columnconfigure(1, weight=1)

        def save():
            self.capcut_store.update_task(
                self.capcut_date_var.get(),
                task["taskId"],
                {key: value.get().strip() for key, value in fields.items()},
            )
            dialog.destroy()
            self.refresh_capcut_tasks(False)

        ttk.Button(body, text="保存并重新校验", style="Primary.TButton", command=save).grid(row=len(specs), column=2, pady=(20, 0))

    def open_capcut_item(self, key):
        task = self._selected_capcut_task()
        if not task:
            return
        path = Path(str(task.get(key, "")))
        if path.is_file():
            os.startfile(str(path))
        else:
            messagebox.showwarning("文件不存在", str(path))

    def open_capcut_output(self):
        date_value = self.capcut_date_var.get()
        path = Path(self.vars["capcut_output_root"].get().strip() or "E:\\") / date_value
        if path.is_dir():
            os.startfile(str(path))
        else:
            messagebox.showinfo("目录尚未生成", f"第五步成片目录将在导出时建立：\n{path}")

    def choose_capcut_output_root(self):
        current = Path(self.vars["capcut_output_root"].get().strip() or "E:\\")
        folder = filedialog.askdirectory(
            title="选择第五步最终成片根目录",
            initialdir=str(current) if current.is_dir() else None,
        )
        if not folder:
            return
        try:
            changed = self.capcut_store.change_final_output_root(Path(folder))
            self.vars["capcut_output_root"].set(folder)
            self.save_settings()
            self.capcut_manifest = None
            self.refresh_capcut_tasks(False)
            self.write_log(f"第五步最终成片根目录已改为：{folder}；已更新 {changed} 条未完成任务。")
            messagebox.showinfo(
                "导出目录已更新",
                f"最终成片根目录：\n{folder}\n\n已重新计算 {changed} 条未完成任务的输出路径。",
            )
        except Exception as exc:
            messagebox.showerror("无法修改导出目录", str(exc))

    def start_capcut_handoff(self):
        date_value = self.capcut_date_var.get()
        payload = self.capcut_store.load_date(date_value)
        selected = {
            task["taskId"]
            for task in payload["tasks"]
            if task.get("selected") and task.get("status") in RUNNABLE_STATUSES
        }
        if not selected:
            return messagebox.showwarning("没有可启动任务", "请至少勾选一条等待处理或失败任务。")
        tasks = [task for task in payload["tasks"] if task.get("taskId") in selected]
        projects = "、".join(dict.fromkeys(task.get("project", "") for task in tasks))
        output_dirs = sorted({str(Path(task["finalOutputPath"]).parent) for task in tasks})
        summary = (
            f"日期：{date_value}\n"
            f"项目：{projects}\n"
            f"任务：{len(tasks)} 个视频 / {len({task.get('audioPath') for task in tasks})} 个音频\n"
            f"输出：{len(output_dirs)} 个分类目录\n\n"
            "启动后请不要操作鼠标、键盘或切换窗口。\n确定生成清单并启动 CapCut 客户端吗？"
        )
        if not messagebox.askyesno("确认启动第五步", summary):
            return
        try:
            manifest = self.capcut_store.create_manifest(date_value, selected)
            self.capcut_manifest = manifest
            executable = Path(self.vars["capcut_client"].get().strip())
            self.capcut_process = launch_capcut_client(executable, manifest)
            self.write_log(f"第五步已启动：{manifest.name}，共 {len(tasks)} 条任务。")
            self.status_var.set(f"CapCut 第五步已启动 · {len(tasks)} 条任务")
            self.refresh_capcut_tasks(False)
        except Exception as exc:
            self.write_log(f"无法启动第五步：{exc}")
            messagebox.showerror("无法启动 CapCut 客户端", str(exc))

    def prepare_capcut_handoff(self):
        date_value = self.capcut_date_var.get()
        payload = self.capcut_store.load_date(date_value)
        selected = {
            task["taskId"]
            for task in payload["tasks"]
            if task.get("selected") and task.get("status") in RUNNABLE_STATUSES
        }
        try:
            self.capcut_manifest = self.capcut_store.create_manifest(date_value, selected)
            self.write_log(f"第五步任务清单已生成：{self.capcut_manifest.name}")
            self.refresh_capcut_tasks(False)
            messagebox.showinfo(
                "第五步任务已准备",
                f"已生成 {len(selected)} 条任务：\n{self.capcut_manifest}\n\n"
                "检查无误后点击“确认并启动 CapCut”。",
            )
        except Exception as exc:
            messagebox.showerror("无法生成第五步任务", str(exc))

    def poll_capcut_status(self):
        try:
            changed = self.capcut_store.sync_statuses()
            active = self.active_external_task or {}
            active_id = canonical_task_id(active)
            self._resume_remote_capcut_if_ready()
            self._sync_remote_pipeline_from_capcut()
            if active_id:
                payload = self.capcut_store.load_date(str(active.get("date") or ""))
                owned = [
                    item for item in payload.get("tasks") or []
                    if str(item.get("remotePipelineTaskId") or "").strip() == active_id
                ]
                expected = contract_expected_count(active)
                states = {str(item.get("status") or "") for item in owned}
                # Reconcile the command against its bound CapCut operation
                # before interpreting examplebrandmalized task rows.  This closes a
                # terminal client even when the optional legacy launch marker
                # was never written.
                self._reconcile_continue_command_capcut_terminal(
                    active_id, owned
                )
                # A persisted ``waiting_client`` row is only active while its
                # exact status file still reports a live client.  Treating an
                # exited client's Waiting row as live kept refreshing the
                # Dashboard command heartbeat forever and permanently locked
                # the Safe Continue button.
                active_row = next((
                    item for item in owned
                    if self._capcut_row_has_live_client(item)
                ), None)
                if active_row:
                    phase = str(active_row.get("currentPhase") or active_row.get("status") or "")
                    self._set_remote_pipeline_status(active_id, "capcut_running", str(active_row.get("phaseText") or phase))
                    self._update_continue_command_phase(active_id, phase, str(active_row.get("phaseText") or phase))
                stopped_waiting = [
                    item for item in owned
                    if str(item.get("status") or "") == "waiting_client"
                    and not self._capcut_row_has_live_client(item)
                ]
                if (
                    stopped_waiting
                    and self._continue_command_has_launched_capcut(active_id)
                ):
                    self._finish_continue_command(
                        active_id,
                        "blocked",
                        "安全继续已结束：上一轮 CapCut 客户端已经退出，未完成任务仍为等待；可再次点击一次安全继续。",
                    )
                draft_release_pending = any(
                    self._capcut_row_has_pending_draft_release(item)
                    for item in owned
                )
                if expected and len(owned) == expected and states == {"completed"} and not draft_release_pending:
                    self._finish_continue_command(active_id, "completed", "安全继续已完成：CapCut 成片已按正式清单完成。")
                elif draft_release_pending:
                    self._set_remote_pipeline_status(
                        active_id,
                        "capcut_draft_release_pending",
                        "成片已完成并通过质检，草稿目录恢复待处理；下一条 CapCut 已暂停",
                        error_code="CAPCUT_DRAFT_RELEASE_PENDING",
                    )
                    self._update_continue_command_phase(
                        active_id,
                        "capcut_draft_release_pending",
                        "成片已完成，正在等待草稿目录恢复",
                    )
                elif (
                    states & {"failed", "stopped", "validation_failed", "manual_review"}
                    and (
                        not self._continue_command_is_recovering_task(active_id)
                        or self._continue_command_has_launched_capcut(active_id)
                    )
                ):
                    # A Dashboard click first writes a durable ``requested``
                    # command.  The old client status may still say Failed
                    # for one polling interval.  Do not let that stale fact
                    # close the fresh command before poll_control_commands()
                    # has had a chance to create the retry manifest.
                    self._finish_continue_command(active_id, "blocked", "安全继续已停止：CapCut 有失败或停止任务，可再次安全继续。")
            if changed and self.current_page == "capcut":
                self.refresh_capcut_tasks(False)
        except Exception as exc:
            self.write_log(f"同步 CapCut 状态失败：{exc}")
        finally:
            self.after(2000, self.poll_capcut_status)

    def _resume_remote_capcut_if_ready(self, recovery=False):
        """Resume a valid remote Step 5 after a workbench restart or repair."""
        task = self.active_external_task or {}
        if not self._is_remote_auto_sequence_task(task):
            return False
        task_id = str(task.get("taskId") or "").strip()
        date_value = str(task.get("date") or "").strip()
        if not task_id or not date_value:
            return False
        if not self._release_capcut_launch_guard(task_id):
            return True
        command_already_launched = self._continue_command_has_launched_capcut(task_id)
        payload = self.capcut_store.load_date(date_value)
        capcut = (task.get("pipelineProfile") or {}).get("5_capcut") or {}
        expected_project = str(capcut.get("category") or task.get("project") or "").strip()
        release_only = {
            str(item.get("taskId") or "")
            for item in payload.get("tasks") or []
            if str(item.get("project") or "") == expected_project
            and str(item.get("remotePipelineTaskId") or "").strip() == task_id
            and str(item.get("status") or "") == "completed"
            and str(item.get("generationRecord") or "").strip()
            and self._capcut_row_has_pending_draft_release(item)
        }
        runnable = {
            str(item.get("taskId") or "")
            for item in payload.get("tasks") or []
            if str(item.get("project") or "") == expected_project
            # waiting_client/running tasks already have a durable manifest
            # and must be observed through its status file, never launched a
            # second time just because the workbench restarted.  A missing
            # status file means the client died before it could accept the
            # manifest, so one safe recovery launch is allowed.
            and (
                str(item.get("status") or "") == "waiting_capcut"
                or (
                    recovery
                    and not command_already_launched
                    and str(item.get("status") or "") == "waiting_client"
                    and not self._capcut_status_file_is_running(item.get("statusFile"))
                )
                or (
                    recovery
                    and not command_already_launched
                    and str(item.get("status") or "") in {"failed", "stopped"}
                )
            )
            and str(item.get("generationRecord") or "").strip()
            and str(item.get("remotePipelineTaskId") or "").strip() == task_id
        }
        expected = contract_expected_count(task)
        completed = {
            str(item.get("taskId") or "")
            for item in payload.get("tasks") or []
            if str(item.get("remotePipelineTaskId") or "").strip() == task_id
            and str(item.get("status") or "") == "completed"
        }
        active_client = any(
            str(item.get("remotePipelineTaskId") or "").strip() == task_id
            and str(item.get("status") or "") in {"waiting_client", "running"}
            and self._capcut_status_file_is_running(item.get("statusFile"))
            for item in payload.get("tasks") or []
        )
        if active_client:
            return True
        if release_only:
            started = self._start_remote_capcut(
                task_id, date_value, release_only, release_only=True
            )
            if started:
                self._mark_continue_capcut_recovery_started(task_id)
            return started
        if runnable and len(completed | runnable) == expected:
            started = self._start_remote_capcut(task_id, date_value, runnable)
            if started:
                self._mark_continue_capcut_recovery_started(task_id)
            return started
        return False

    @staticmethod
    def _capcut_status_file_is_running(value):
        path = Path(str(value or "").strip())
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(payload, dict) or not bool(payload.get("running")):
            return False

        # A JSON flag is not a live process.  An interrupted handoff used to
        # leave running=true indefinitely, which made every later Safe Continue
        # believe the dead client still owned Step 5.
        heartbeat_text = str(
            payload.get("heartbeatAt") or payload.get("updatedAt") or ""
        ).strip()
        try:
            heartbeat = datetime.fromisoformat(heartbeat_text.replace("Z", "+00:00"))
            now = datetime.now(heartbeat.tzinfo) if heartbeat.tzinfo else datetime.now()
            if (now - heartbeat).total_seconds() > 60:
                return False
        except (TypeError, ValueError):
            return False

        try:
            client_pid = int(payload.get("clientPid") or 0)
        except (TypeError, ValueError):
            return False
        if client_pid <= 0:
            return False

        try:
            import psutil

            process = psutil.Process(client_pid)
            if not process.is_running():
                return False
            identity = " ".join(
                [str(process.name() or "")] + [str(part) for part in process.cmdline()]
            ).lower()
            return "capcutbatchclient" in identity
        except ImportError:
            # Older packaged workbench runtimes may not include psutil.  The
            # short heartbeat lease still prevents an indefinite ghost lock.
            return True
        except Exception as exc:
            try:
                import psutil
                if isinstance(exc, psutil.AccessDenied):
                    # Same-user production normally permits inspection.  If a
                    # policy blocks it, retain only the fresh 60-second lease
                    # rather than risking a concurrent client immediately.
                    return True
            except ImportError:
                pass
            return False

    def _capcut_row_has_live_client(self, item):
        """Return true only when this exact task row owns a live CapCut batch.

        A database label such as ``running`` or ``waiting_client`` is not a
        process heartbeat.  The bound status file is the durable fact source.
        """
        if not isinstance(item, dict):
            return False
        if str(item.get("status") or "") not in {"running", "waiting_client"}:
            return False
        return self._capcut_status_file_is_running(item.get("statusFile"))

    def _task_has_pending_draft_release(self, task):
        """Whether a completed output of this task still waits for draft release."""
        task_id = canonical_task_id(task)
        date_value = str((task or {}).get("productionDate") or (task or {}).get("date") or "").strip()
        if not task_id or not date_value:
            return False
        payload = self.capcut_store.load_date(date_value)
        return any(
            str(item.get("remotePipelineTaskId") or "").strip() == task_id
            and str(item.get("status") or "") == "completed"
            and self._capcut_row_has_pending_draft_release(item)
            for item in payload.get("tasks") or []
            if isinstance(item, dict)
        )

    @staticmethod
    def _capcut_row_has_pending_draft_release(item):
        """Return true for a quality-passed output whose draft lease remains.

        This is intentionally separate from the CapCut row status: a row can
        be production-completed while its post-production filesystem release
        is still pending.  Such a row is upload-eligible but must hold the
        single-editor queue until the release-only recovery succeeds.
        """
        if not isinstance(item, dict):
            return False
        if str(item.get("productionStatus") or "").lower() != "completed":
            return False
        return str(item.get("draftReleaseStatus") or "").lower() in {
            "pending", "release_pending", "failed"
        } or str(item.get("draftCleanupStatus") or "").lower() == "release_pending"

    def _update_continue_command_phase(self, task_id, phase, message):
        """Persist non-terminal recovery progress without closing the command."""
        if not task_id:
            return
        try:
            request = json.loads(CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig"))
            if not isinstance(request, dict) or str(request.get("taskId") or "") != str(task_id):
                return
            if str(request.get("state") or "") not in {"accepted", "recovering", "running"}:
                return
            request["state"] = "recovering"
            request["phase"] = phase
            request["message"] = message
            request["lastHeartbeat"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._write_continue_command(request)
        except (OSError, ValueError, TypeError) as exc:
            self.write_log(f"更新安全继续阶段失败：{exc}")

    @staticmethod
    def _continue_command_is_recovering_task(task_id):
        """Return true while a fresh Dashboard command owns this retry.

        This protects a newly written command from a stale failed/stopped
        CapCut row until the command dispatcher has rebuilt its manifest.
        Terminal commands are deliberately excluded so historical failures
        remain visible without keeping recovery locked forever.
        """
        try:
            request = json.loads(CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return False
        return bool(
            isinstance(request, dict)
            and str(request.get("taskId") or "").strip() == str(task_id or "").strip()
            and str(request.get("state") or "") in {"requested", "accepted", "recovering", "running"}
        )

    @staticmethod
    def _continue_command_has_launched_capcut(task_id):
        """True after this exact Dashboard command launched one CapCut attempt."""
        try:
            request = json.loads(CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return False
        return bool(
            isinstance(request, dict)
            and str(request.get("taskId") or "").strip() == str(task_id or "").strip()
            and str(request.get("state") or "") in {"accepted", "recovering", "running"}
            and (
                str(request.get("boundManifestId") or "").strip()
                or str(request.get("boundAttemptId") or "").strip()
                or str(request.get("boundStatusFile") or "").strip()
                or str(request.get("capcutRecoveryStartedAt") or "").strip()
            )
        )

    def _bind_continue_command_to_capcut(self, task_id, manifest_path,
                                         process):
        """Atomically bind the active command to one concrete CapCut run."""
        try:
            request = json.loads(
                CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig")
            )
            if not isinstance(request, dict):
                return
            if str(request.get("taskId") or "").strip() != str(task_id or "").strip():
                return
            if str(request.get("state") or "") not in {
                "accepted", "recovering", "running"
            }:
                return
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            request["boundManifestId"] = str(manifest.get("manifestId") or "")
            request["boundAttemptId"] = str(manifest.get("attemptId") or "")
            request["boundStatusFile"] = str(manifest.get("statusFile") or "")
            request["clientPid"] = int(getattr(process, "pid", 0) or 0)
            request["clientProcessIdentity"] = capture_process_identity(
                request["clientPid"], role="capcut-batch-client"
            )
            request["operationGeneration"] = str(
                manifest.get("operationGeneration")
                or request.get("commandId") or ""
            )
            request["boundOperationState"] = "running"
            request["capcutRecoveryStartedAt"] = now
            request["lastHeartbeat"] = now
            request["phase"] = "capcut_client_started"
            self._write_continue_command(request)
        except (OSError, ValueError, TypeError) as exc:
            self.write_log(f"Bind Safe Continue to CapCut operation failed: {exc}")

    def _recovery_execution_receipt(self, task_id):
        """Build a receipt from the actual producer process, never Tk itself."""
        request = self._read_local_json_object(CONTINUE_COMMAND_FILE)
        if str(request.get("taskId") or "").strip() != str(task_id or "").strip():
            return {"taskId": task_id, "binding": "missing"}
        voice_identity = self._live_voice_launch_identity(str(task_id or ""))
        if voice_identity:
            return {"processIdentity": voice_identity, "taskId": task_id, "producer": "fish"}
        identity = request.get("clientProcessIdentity")
        if not isinstance(identity, dict):
            try:
                pid = int(request.get("clientPid") or 0)
            except (TypeError, ValueError):
                pid = 0
            identity = capture_process_identity(pid, role="capcut-batch-client") if pid else {}
        return {
            "processIdentity": identity,
            "statusFile": str(request.get("boundStatusFile") or ""),
            "manifestId": str(request.get("boundManifestId") or ""),
            "attemptId": str(request.get("boundAttemptId") or ""),
            "operationGeneration": str(request.get("operationGeneration") or ""),
            "taskId": task_id,
        }

    @staticmethod
    def _capcut_client_pid_is_alive(value):
        try:
            pid = int(value or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            import psutil
            process = psutil.Process(pid)
            if not process.is_running():
                return False
            identity = " ".join(
                [str(process.name() or "")] +
                [str(part) for part in process.cmdline()]
            ).lower()
            return "capcutbatchclient" in identity
        except Exception:
            return False

    @staticmethod
    def _iso_at_or_after(left, right):
        """Return true when ISO timestamp left is not older than right."""
        if not str(right or "").strip():
            return True
        try:
            left_value = datetime.fromisoformat(
                str(left or "").replace("Z", "+00:00")
            )
            right_value = datetime.fromisoformat(
                str(right or "").replace("Z", "+00:00")
            )
            if left_value.tzinfo and not right_value.tzinfo:
                right_value = right_value.replace(tzinfo=left_value.tzinfo)
            if right_value.tzinfo and not left_value.tzinfo:
                left_value = left_value.replace(tzinfo=right_value.tzinfo)
            return left_value >= right_value
        except (TypeError, ValueError):
            return False

    def _reconcile_continue_command_capcut_terminal(self, task_id, rows):
        """Close a recovering command from its matching terminal status."""
        try:
            request = json.loads(
                CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(request, dict):
            return False
        if str(request.get("taskId") or "").strip() != str(task_id or "").strip():
            return False
        if str(request.get("state") or "") not in {
            "accepted", "recovering", "running"
        }:
            return False

        candidates = []
        bound_status = str(request.get("boundStatusFile") or "").strip()
        if bound_status:
            candidates.append(Path(bound_status))
        for row in rows or []:
            value = str((row or {}).get("statusFile") or "").strip()
            if value and all(str(path) != value for path in candidates):
                candidates.append(Path(value))

        for status_path in candidates:
            if not status_path.is_file():
                continue
            try:
                status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(status, dict) or bool(status.get("running")):
                continue
            if str(status.get("remotePipelineTaskId") or "").strip() != str(task_id or "").strip():
                continue
            bound_manifest = str(request.get("boundManifestId") or "").strip()
            bound_attempt = str(request.get("boundAttemptId") or "").strip()
            if bound_manifest and str(status.get("manifestId") or "").strip() != bound_manifest:
                continue
            if bound_attempt and str(status.get("attemptId") or "").strip() != bound_attempt:
                continue
            observed_at = str(
                status.get("updatedAt") or status.get("heartbeatAt") or ""
            ).strip()
            # A legacy command without durable binding may only consume a
            # terminal status produced after that command started.
            if not bound_manifest and not bound_attempt and not self._iso_at_or_after(
                    observed_at, request.get("startedAt")):
                continue
            client_pid = status.get("clientPid") or request.get("clientPid")
            if self._capcut_client_pid_is_alive(client_pid):
                continue

            task_states = {
                str(item.get("state") or item.get("status") or "").strip().lower()
                for item in status.get("tasks") or []
                if isinstance(item, dict)
            }
            failed = {
                "failed", "stopped", "validation_failed", "manual_review",
                "completed_with_errors",
            }
            release_pending = any(
                self._capcut_row_has_pending_draft_release(item)
                for item in (status.get("tasks") or [])
                if isinstance(item, dict)
            )
            if task_states and task_states <= {"completed"} and release_pending:
                self._finish_continue_command(
                    task_id, "blocked",
                    "成片已完成并通过质检，但草稿目录恢复仍待处理；请再次点击安全继续。",
                    phase="capcut_draft_release_pending",
                    terminal_observed_at=observed_at,
                    bound_operation_state="release_pending",
                )
                return True
            if task_states and task_states <= {"completed"}:
                self._finish_continue_command(
                    task_id, "completed",
                    "安全继续已完成：CapCut 当前尝试已全部完成。",
                    phase="capcut_completed",
                    terminal_observed_at=observed_at,
                    bound_operation_state="completed",
                )
                return True
            if task_states & failed or int(status.get("failed") or 0) > 0:
                detail = str(status.get("message") or "").strip()
                if not detail:
                    detail = "CapCut 当前尝试已终止且存在失败任务，可再次安全继续。"
                self._finish_continue_command(
                    task_id, "blocked", detail,
                    phase="capcut_recoverable_failed",
                    terminal_observed_at=observed_at,
                    bound_operation_state="failed",
                )
                return True
        return False

    def _mark_continue_capcut_recovery_started(self, task_id):
        """Bind one CapCut launch to the active idempotent continue command."""
        try:
            request = json.loads(CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig"))
            if not isinstance(request, dict):
                return
            if str(request.get("taskId") or "").strip() != str(task_id or "").strip():
                return
            if str(request.get("state") or "") not in {"accepted", "recovering", "running"}:
                return
            if request.get("capcutRecoveryStartedAt"):
                return
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            request["capcutRecoveryStartedAt"] = now
            request["lastHeartbeat"] = now
            request["phase"] = "capcut_client_started"
            self._write_continue_command(request)
        except (OSError, ValueError, TypeError) as exc:
            self.write_log(f"记录 CapCut 安全继续启动标记失败：{exc}")

    def _finish_continue_command(self, task_id, state, message, *,
                                 phase="", terminal_observed_at="",
                                 bound_operation_state=""):
        """Close only the Dashboard command that belongs to this task."""
        if not task_id:
            return
        try:
            request = json.loads(CONTINUE_COMMAND_FILE.read_text(encoding="utf-8-sig"))
            if not isinstance(request, dict):
                return
            if str(request.get("taskId") or "").strip() != str(task_id).strip():
                return
            if str(request.get("state") or "") not in {"requested", "accepted", "recovering", "running"}:
                return
            request["state"] = state
            request["message"] = message
            if phase:
                request["phase"] = phase
            if terminal_observed_at:
                request["terminalObservedAt"] = terminal_observed_at
            if bound_operation_state:
                request["boundOperationState"] = bound_operation_state
            request["lastHeartbeat"] = datetime.now().astimezone().isoformat(timespec="seconds")
            request["finishedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._write_continue_command(request)
        except (OSError, ValueError, TypeError) as exc:
            self.write_log(f"更新安全继续状态失败：{exc}")

    def _set_remote_pipeline_status(
        self, task_id, state, text, *, error_code="", missing_languages=None,
        child_task_id="", language="", failure_event_id="", child_task_type="",
    ):
        """Persist the full-pipeline state that controls a remote successor."""
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        registry = load_project_download_statuses()
        records = registry.get("items") if isinstance(registry, dict) else {}
        record = records.get(task_id) if isinstance(records, dict) else None
        if not isinstance(record, dict):
            return False
        if (
            str(record.get("pipelineStatus") or "") == state
            and str(record.get("pipelineStateText") or "") == text
            and str(record.get("errorCode") or "") == str(error_code or "")
            and (not child_task_id or str(record.get("childTaskId") or "") == str(child_task_id))
            and (not language or str(record.get("language") or "") == str(language))
            and (not failure_event_id or str(record.get("failureEventId") or "") == str(failure_event_id))
            and (state not in {"blocked_voice", "voice_failed", "voice_login_required", "blocked_capcut", "capcut_failed", "capcut_stopped"} or str(record.get("failureEventId") or ""))
        ):
            return False
        updated = dict(record)
        updated["pipelineStatus"] = state
        updated["pipelineStateText"] = text
        updated["pipelineUpdatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
        updated["currentPhase"] = state
        if error_code:
            next_error = str(error_code)
            previous_error = str(record.get("errorCode") or "").strip()
            if next_error == "AUTO_RECOVERY_EXHAUSTED" and previous_error and previous_error != next_error:
                secondary = list(updated.get("secondaryErrors") or [])
                if previous_error not in secondary:
                    secondary.append(previous_error)
                updated["secondaryErrors"] = secondary
                # Keep the first unresolved production cause visible.  Budget
                # exhaustion is a recovery state, not a replacement cause.
                updated["errorCode"] = previous_error
            else:
                updated["errorCode"] = next_error
        else:
            updated.pop("errorCode", None)
        failure_states = {"blocked_voice", "voice_failed", "voice_login_required", "blocked_capcut", "capcut_failed", "capcut_stopped"}
        if state in failure_states and error_code:
            if not str(record.get("failureEventId") or "").strip() or str(record.get("errorCode") or "") != str(error_code):
                updated["failureEventId"] = str(failure_event_id or f"failure-{uuid.uuid4().hex}")
        elif state not in failure_states and updated.get("failureEventId"):
            updated["lastFailureEventId"] = updated.pop("failureEventId")
        if missing_languages:
            updated["missingLanguages"] = [str(item) for item in missing_languages]
        else:
            updated.pop("missingLanguages", None)
        if child_task_id:
            updated["childTaskId"] = str(child_task_id)
            if child_task_type:
                updated["childTaskType"] = str(child_task_type)
        if language:
            updated["language"] = str(language)
        if failure_event_id:
            updated["failureEventId"] = str(failure_event_id)
        # ``tk.Tk.__getattr__`` recursively delegates missing attributes to
        # Tcl.  A lightweight test/recovery shell may not have initialized
        # this field yet, so read the instance dictionary directly.
        active_value = self.__dict__.get("active_external_task", {})
        active = active_value if isinstance(active_value, dict) else {}
        if canonical_task_id(active) == task_id:
            updated["batchId"] = str(active.get("batchId") or updated.get("batchId") or "")
            updated["runGroupId"] = str(active.get("runGroupId") or updated.get("runGroupId") or "")
            updated["batchKind"] = str(active.get("batchKind") or updated.get("batchKind") or ("examplebrand-natural-flow" if str(active.get("project") or "") == "ExampleBrand" else "formal"))
            updated["selectedProjects"] = list(active.get("selectedProjects") or updated.get("selectedProjects") or [])
            updated["productionDate"] = str(active.get("productionDate") or active.get("date") or updated.get("productionDate") or "")
            updated["canonicalTaskId"] = task_id
            capcut = self._capcut_ops_status() if state.startswith("capcut") else {}
            if capcut:
                updated["attemptId"] = str(capcut.get("attemptId") or updated.get("attemptId") or "")
                updated["manifestId"] = str(capcut.get("manifestId") or updated.get("manifestId") or "")
        save_project_download_status(updated)
        return True

    def _sync_remote_pipeline_from_capcut(self):
        """Release a successor only after its predecessor's Step 5 is final."""
        upload_payload = load_baidu_json(BAIDU_STATE_FILE, {"tasks": {}})
        upload_tasks = upload_payload.get("tasks") if isinstance(upload_payload, dict) else {}
        if not isinstance(upload_tasks, dict):
            upload_tasks = {}

        def has_durable_final(item):
            output = Path(str(item.get("finalOutputPath") or ""))
            if output.is_file() and output.stat().st_size > 0:
                return True
            uploaded = upload_tasks.get(str(item.get("taskId") or ""))
            return bool(
                isinstance(uploaded, dict)
                and str(uploaded.get("status") or "") == "uploaded"
                and self._integer(uploaded.get("cloudSize"), 0) > 0
                and str((uploaded.get("cleanup") or {}).get("cloudVerifiedAt") or "")
            )

        groups = {}
        for date_value in self.capcut_store.dates():
            payload = self.capcut_store.load_date(date_value)
            for item in payload.get("tasks") or []:
                owner = str(item.get("remotePipelineTaskId") or "").strip()
                if owner:
                    item_date = str(
                        item.get("productionDate") or item.get("date") or date_value
                    ).strip()
                    groups.setdefault((owner, item_date), []).append(item)
        for (owner, date_value), items in groups.items():
            owner_task = load_task_record(owner)
            if isinstance(owner_task, dict):
                owner_date = str(
                    owner_task.get("productionDate") or owner_task.get("date") or ""
                ).strip()
                if owner_date and date_value and owner_date != date_value:
                    self.write_log(
                        "Ignored cross-date CapCut attribution: "
                        f"owner={owner}, ownerDate={owner_date}, rowDate={date_value}, "
                        f"rows={len(items)}"
                    )
                    continue
            states = {str(item.get("status") or "") for item in items}
            expected_counts = {int(item.get("expectedTaskCount") or 0) for item in items}
            expected_counts.discard(0)
            unique_ids = {str(item.get("taskId") or "") for item in items if str(item.get("taskId") or "")}
            expected_count = next(iter(expected_counts), 0) if len(expected_counts) == 1 else 0
            valid_outputs = all(has_durable_final(item) for item in items)
            contract_ok = (
                expected_count > 0
                and len(items) == expected_count
                and len(unique_ids) == expected_count
                and len(expected_counts) == 1
            )
            draft_release_pending = any(
                self._capcut_row_has_pending_draft_release(item)
                for item in items
            )
            if states and states <= {"completed"} and contract_ok and valid_outputs and not draft_release_pending:
                if self._set_remote_pipeline_status(owner, "completed", "第 5 步 CapCut 成片完成，已交给自动上传"):
                    self.write_log(f"远程项目 {owner} 已完成第 5 步，允许队列进入下一项目。")
                    self.after(350, self._advance_remote_queue)
            elif draft_release_pending:
                self._set_remote_pipeline_status(
                    owner,
                    "capcut_draft_release_pending",
                    "成片已完成并通过质检，草稿目录恢复待处理；后续 CapCut 暂停",
                    error_code="CAPCUT_DRAFT_RELEASE_PENDING",
                )
            elif not contract_ok:
                self._set_remote_pipeline_status(
                    owner,
                    "blocked_capcut",
                    f"第 5 步正式清单不完整：预期 {expected_count or '?'}，实际 {len(unique_ids)}",
                )
            elif states & {"failed", "stopped", "validation_failed", "manual_review"}:
                failed_rows = [
                    item for item in items
                    if str(item.get("status") or "") in {
                        "failed", "stopped", "validation_failed", "manual_review"
                    }
                ]
                failed_row = failed_rows[0] if failed_rows else {}
                state, text, error_code = capcut_failure_pipeline_status(
                    failed_row
                )
                self._set_remote_pipeline_status(
                    owner,
                    state,
                    text,
                    error_code=error_code,
                    child_task_id=str(failed_row.get("taskId") or failed_row.get("canonicalTaskId") or "").strip(),
                    language=str(failed_row.get("language") or "").strip(),
                    failure_event_id=str(failed_row.get("failureEventId") or "").strip(),
                    child_task_type="capcut",
                )
            elif states & {"waiting_client", "running"}:
                client_started = any(
                    self._capcut_status_file_is_running(item.get("statusFile"))
                    for item in items
                )
                if client_started:
                    running = "running" in states
                    self._set_remote_pipeline_status(
                        owner,
                        "capcut_running" if running else "capcut_preflight",
                        "CapCut正在运行"
                        if running
                        else "CapCut客户端已启动，正在校验并恢复任务草稿",
                    )
            elif states and states <= {"completed"} and not valid_outputs:
                self._set_remote_pipeline_status(owner, "blocked_capcut", "第 5 步存在缺失或无效最终成片，后续项目保持等待")

    def configure_baidu_upload(self):
        config = load_baidu_config()
        secret_store = SecretStore()
        existing = secret_store.load()
        dialog = tk.Toplevel(self)
        dialog.title("配置百度网盘自动上传")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.configure(bg=self.BG)
        frame = ttk.Frame(dialog, style="Card.TFrame", padding=22)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(frame, text="百度网盘开放平台应用", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(
            frame,
            text="密钥使用 Windows DPAPI 加密，只能由当前 Windows 用户读取，不会写入 GitHub。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))
        app_name = tk.StringVar(value=str(config.get("app_name") or "AI视频生产线"))
        app_key = tk.StringVar()
        secret_key = tk.StringVar()
        for row, (label, variable, secret) in enumerate(
            (
                ("应用名称", app_name, False),
                ("App Key", app_key, True),
                ("Secret Key", secret_key, True),
            ),
            start=2,
        ):
            ttk.Label(frame, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=6
            )
            entry = ttk.Entry(frame, textvariable=variable, width=52, show="●" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=6)
        if existing.get("app_key"):
            ttk.Label(
                frame,
                text="App Key 与 Secret Key 已保存；留空表示不修改。",
                style="Muted.TLabel",
            ).grid(row=5, column=1, sticky="w", pady=(0, 8))
        ttk.Label(
            frame,
            text="提交后会打开百度授权页面。只需登录自己的账号并确认一次。",
            style="Muted.TLabel",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 14))

        def save_and_authorize():
            name = app_name.get().strip()
            key = app_key.get().strip()
            secret = secret_key.get().strip()
            if not name:
                return messagebox.showwarning("缺少应用名称", "请填写开放平台中的应用名称。", parent=dialog)
            current = secret_store.load()
            if key:
                current["app_key"] = key
            if secret:
                current["secret_key"] = secret
            if not current.get("app_key") or not current.get("secret_key"):
                return messagebox.showwarning(
                    "缺少密钥",
                    "首次配置必须填写 App Key 和 Secret Key。",
                    parent=dialog,
                )
            # Changing application credentials invalidates tokens from the previous app.
            if key or secret:
                for token_key in ("access_token", "refresh_token", "expires_at", "authorized_at"):
                    current.pop(token_key, None)
            secret_store.save(current)
            config["enabled"] = True
            config["app_name"] = name
            config["records_root"] = str(APP_DIR / "交接记录")
            config["state_file"] = str(BAIDU_STATE_FILE)
            save_baidu_config(config)
            dialog.destroy()
            self.begin_baidu_authorization()

        actions = ttk.Frame(frame, style="Card.TFrame")
        actions.grid(row=7, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="取消", style="Secondary.TButton", command=dialog.destroy).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(
            actions,
            text="保存并开始授权",
            style="Primary.TButton",
            command=save_and_authorize,
        ).pack(side="left")
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(20, (self.winfo_width() - dialog.winfo_width()) // 2)
        y = self.winfo_rooty() + max(20, (self.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")

    def begin_baidu_authorization(self):
        self.upload_status_var.set("正在向百度网盘申请授权码…")
        self.upload_detail_var.set("请稍候")

        def request():
            try:
                client = BaiduNetdiskClient(SecretStore())
                device = client.request_device_code()
                self.after(0, lambda: self.show_baidu_authorization(device))
            except Exception as exc:
                self.after(0, lambda: self._baidu_authorization_failed(str(exc)))

        threading.Thread(target=request, daemon=True, name="BaiduDeviceCode").start()

    def _baidu_authorization_failed(self, error):
        self.upload_status_var.set("百度网盘授权失败")
        self.upload_detail_var.set(error)
        messagebox.showerror("百度网盘授权失败", error)

    def show_baidu_authorization(self, device):
        code = str(device.get("user_code") or "")
        verification_url = str(device.get("verification_url") or "https://openapi.baidu.com/device")
        auth = tk.Toplevel(self)
        auth.title("连接百度网盘")
        auth.transient(self)
        auth.resizable(False, False)
        content = ttk.Frame(auth, style="Card.TFrame", padding=24)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="请在百度页面完成授权", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            content,
            text="浏览器已打开授权页面，请输入下面的用户码并使用自己的百度账号确认。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(6, 14))
        code_var = tk.StringVar(value=code)
        ttk.Entry(
            content, textvariable=code_var, state="readonly",
            justify="center", font=("Consolas", 18, "bold"), width=24,
        ).pack(fill="x")
        status = tk.StringVar(value="等待你在浏览器确认…")
        ttk.Label(content, textvariable=status, foreground=self.ORANGE).pack(
            anchor="w", pady=(12, 0)
        )

        def copy_code():
            self.clipboard_clear()
            self.clipboard_append(code)

        ttk.Button(content, text="复制用户码", style="Secondary.TButton", command=copy_code).pack(
            anchor="e", pady=(12, 0)
        )
        webbrowser.open(verification_url)
        self.upload_status_var.set("等待百度网盘授权")
        self.upload_detail_var.set(f"授权用户码：{code}")

        def poll():
            try:
                client = BaiduNetdiskClient(SecretStore())
                client.poll_device_token(
                    str(device.get("device_code") or ""),
                    int(device.get("interval") or 5),
                    int(device.get("expires_in") or 300),
                )
                self.after(0, lambda: self._baidu_authorization_succeeded(auth, status))
            except Exception as exc:
                self.after(
                    0,
                    lambda: self._baidu_authorization_poll_failed(auth, status, str(exc)),
                )

        threading.Thread(target=poll, daemon=True, name="BaiduOAuthPoll").start()

    def _baidu_authorization_succeeded(self, dialog, status_var):
        status_var.set("授权成功，正在启动自动上传服务")
        self.upload_status_var.set("百度网盘已授权")
        self.upload_detail_var.set("即将自动识别超级会员并使用32MB分片")
        self.ensure_upload_service()
        self.after(1200, dialog.destroy)
        self.after(1500, self.refresh_upload_page)

    def _baidu_authorization_poll_failed(self, dialog, status_var, error):
        status_var.set("授权失败：" + error)
        self.upload_status_var.set("百度网盘授权失败")
        self.upload_detail_var.set(error)
        dialog.lift()

    def ensure_upload_service(self, show_message=False):
        config = load_baidu_config()
        secrets = SecretStore().load()
        if not config.get("enabled"):
            if show_message:
                messagebox.showinfo("尚未配置", "请先点击“配置并授权百度网盘”。")
            return
        if not (secrets.get("access_token") or secrets.get("refresh_token")):
            if show_message:
                messagebox.showinfo("尚未授权", "请先完成百度网盘扫码授权。")
            return
        if not UPLOAD_SERVICE.is_file():
            if show_message:
                messagebox.showerror("上传服务缺失", f"找不到：{UPLOAD_SERVICE}")
            return
        if self.upload_process and self.upload_process.poll() is None:
            if show_message:
                messagebox.showinfo("上传服务", "百度网盘上传服务正在运行。")
            return
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.upload_process = subprocess.Popen(
                [sys.executable, str(UPLOAD_SERVICE)],
                cwd=str(APP_DIR),
                creationflags=flags,
            )
            self.upload_status_var.set("百度网盘上传服务已启动")
            self.upload_detail_var.set("等待第五步完成的最终成片")
            if show_message:
                messagebox.showinfo("上传服务", "上传服务已启动，将自动处理第五步最终成片。")
        except Exception as exc:
            self.upload_status_var.set("无法启动百度网盘上传服务")
            self.upload_detail_var.set(str(exc))
            if show_message:
                messagebox.showerror("启动失败", str(exc))

    def refresh_upload_page(self):
        state = load_baidu_json(BAIDU_STATE_FILE, {})
        config = load_baidu_config()
        tasks = list((state.get("tasks") or {}).values()) if isinstance(state, dict) else []
        completed = sum(1 for item in tasks if item.get("status") == "uploaded")
        failed = sum(1 for item in tasks if item.get("status") == "failed")
        uploading = sum(1 for item in tasks if item.get("status") == "uploading")
        waiting = max(0, len(tasks) - completed - failed - uploading)
        self.upload_account_var.set(str(state.get("account") or "--"))
        self.upload_queue_var.set(str(waiting + uploading))
        self.upload_completed_var.set(str(completed))
        self.upload_failed_var.set(str(failed))
        if not config.get("enabled"):
            self.upload_status_var.set("百度网盘未配置")
            self.upload_detail_var.set("点击“配置并授权百度网盘”开始")
        elif not state.get("authorized"):
            self.upload_status_var.set("等待百度网盘授权")
            self.upload_detail_var.set("配置已保存，但尚未获得账号授权")
        elif uploading:
            self.upload_status_var.set("正在上传最终成片")
            self.upload_detail_var.set(
                f"超级会员自动使用 {self._integer(state.get('chunkSizeMB'), 32)}MB 分片"
            )
        elif failed:
            self.upload_status_var.set(f"{failed} 条上传失败，将自动重试")
            self.upload_detail_var.set("失败只重试上传，不会重新剪辑或删除本地成片")
        else:
            self.upload_status_var.set("百度网盘上传服务就绪")
            self.upload_detail_var.set(
                f"已上传 {completed} 条；等待第五步最终成片"
            )
        if not hasattr(self, "upload_tree"):
            return
        self.upload_tree.delete(*self.upload_tree.get_children())
        labels = {
            "waiting_validation": "等待稳定校验",
            "uploading": "上传中",
            "uploaded": "上传完成",
            "failed": "上传失败",
        }
        for item in sorted(
            tasks,
            key=lambda value: (str(value.get("date") or ""), str(value.get("taskId") or "")),
            reverse=True,
        ):
            status = str(item.get("status") or "waiting_validation")
            tag = status if status in {"failed", "uploaded"} else ""
            self.upload_tree.insert(
                "",
                "end",
                iid=str(item.get("taskId") or uuid.uuid4().hex),
                values=(
                    item.get("date", ""),
                    item.get("project", ""),
                    Path(str(item.get("localPath") or "")).name,
                    item.get("remotePath", ""),
                    f"{self._integer(item.get('progress'))}%",
                    labels.get(status, status),
                    item.get("error", ""),
                ),
                tags=(tag,) if tag else (),
            )

    def poll_upload_status(self):
        try:
            if self.current_page == "upload":
                self.refresh_upload_page()
        except Exception as exc:
            self.upload_status_var.set("上传状态读取失败")
            self.upload_detail_var.set(str(exc))
        finally:
            self.after(2200, self.poll_upload_status)

    def open_upload_state_dir(self):
        BAIDU_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        os.startfile(str(BAIDU_STATE_FILE.parent))

    def confirm_selected_uploaded_download(self):
        if not hasattr(self, "upload_tree"):
            return
        task_ids = list(self.upload_tree.selection())
        if not task_ids:
            messagebox.showinfo("请选择成片", "请先在上传任务表中选择已上传完成的最终 MP4。")
            return
        state = load_baidu_json(BAIDU_STATE_FILE, {})
        tasks = state.get("tasks") if isinstance(state, dict) else {}
        valid_ids = [
            task_id for task_id in task_ids
            if isinstance(tasks, dict) and (tasks.get(task_id) or {}).get("status") == "uploaded"
        ]
        if not valid_ids:
            messagebox.showinfo("无法清理", "只能确认并清理状态为“上传完成”的最终 MP4。")
            return
        if not messagebox.askyesno(
            "确认公司已下载",
            f"已确认公司端下载 {len(valid_ids)} 条成片？\n\n"
            "这些本地最终 MP4 将移入 Windows 回收站；网盘成片、任务记录、素材库和日志不会删除。",
        ):
            return
        try:
            result = UploadCoordinator().confirm_downloaded_and_recycle_final(valid_ids)
        except Exception as exc:
            messagebox.showerror("清理失败", str(exc))
            return
        self.refresh_upload_page()
        messagebox.showinfo(
            "清理完成",
            f"已移入回收站：{len(result['recycled'])} 条\n"
            f"跳过：{len(result['skipped'])} 条\n"
            f"失败：{len(result['failed'])} 条",
        )

    def open_folder(self, value):
        path = Path(value.strip())
        if path.is_dir(): os.startfile(str(path))
        else: messagebox.showwarning("目录无效", "目录不存在，请先选择正确路径。")

    def new_batch(self):
        if self.download_process and self.download_process.poll() is None:
            return messagebox.showinfo("任务处理中", "当前批次仍在后台处理，完成或停止后才能新建批次。")
        self.vars["batch_name"].set(datetime.now().strftime("批次_%Y%m%d_%H%M"))
        self.tree.delete(*self.tree.get_children())
        self.live_tasks.clear()
        self.task_rows.clear()
        self.last_batch_result = None
        self.total_var.set("0"); self.success_var.set("0"); self.failed_var.set("0"); self.progress_var.set("0%")
        self.show_task_view("live")
        self.write_log("已创建新批次。")
        self.download_batch_context = {}
        self.save_download_session()

    def on_close(self):
        if self.__dict__.get('_recovery_close_requested'):
            return
        if self.stitcher_controller is not None and self.stitcher_controller.is_running():
            if not messagebox.askyesno("任务仍在运行", "AI 拼接任务仍在运行。确定取消任务并关闭整个工作台吗？"):
                return
        if self.download_process and self.download_process.poll() is None:
            if not messagebox.askyesno("下载仍在运行", "后台下载仍在运行。确定停止任务并关闭整个工作台吗？"):
                return
        from concurrent.futures import ThreadPoolExecutor
        self._recovery_close_requested = True
        self.auto_recovery_var.set(False)
        if '_recovery_executor' not in self.__dict__:
            self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='recovery-control')
            self._recovery_poll_future = None
        future = self._recovery_executor.submit(
            self.recovery_control.set_session_paused,
            {'pid': os.getpid(), 'sessionId': WORKBENCH_SESSION_ID}, True)
        self.after(100, lambda: self._finish_close_after_pause(future))

    def _finish_close_after_pause(self, future):
        if not future.done():
            self.after(100, lambda: self._finish_close_after_pause(future))
            return
        try:
            future.result()
        except Exception as exc:
            self._recovery_close_requested = False
            detail = f"保存自动恢复暂停意图失败，窗口保持打开：{type(exc).__name__}: {exc}"
            self.write_log(detail)
            self.status_var.set(detail)
            messagebox.showerror("无法安全关闭", detail)
            return
        if self.stitcher_controller is not None and self.stitcher_controller.is_running():
            self.stitcher_controller.cancel()
        if self.download_process and self.download_process.poll() is None:
            self.download_process.terminate()
        if self.stitcher_controller is not None:
            try:
                self.stitcher_controller.save()
            except Exception as exc:
                self.write_log(f"保存拼接项目时出现警告：{exc}")
        self.save_settings()
        self.save_download_session()
        self._recovery_executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


if __name__ == "__main__":
    if not acquire_workbench_instance():
        # Some imported production helpers own non-daemon background threads.
        # Raising SystemExit would leave a duplicate python.exe alive even
        # though it never acquired the Workbench lease.  Exit immediately so
        # process-based health checks also see exactly one instance.
        os._exit(0)
    try:
        app = Workbench()
        publish_workbench_ready()

        def _heartbeat():
            try:
                if app.winfo_exists():
                    publish_workbench_ready()
                    app.after(5000, _heartbeat)
            except tk.TclError:
                pass

        app.after(5000, _heartbeat)
        app.mainloop()
    finally:
        remove_workbench_ready()
        release_workbench_instance()
