"""One-time, reversible v3 status migration for the current SPC floor task.

Run without ``--apply`` first.  The script changes state files only: it never
starts a worker, launches CapCut, moves media, or removes an old manifest.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from capcut_handoff import CapCutHandoffStore


APP_DIR = Path(__file__).resolve().parent
DATE_VALUE = "7.22"
PARENT_TASK_ID = "inbox-20260722161808-8b2a681b"
COMPLETED_IDS = {
    "f1c9f6df67dc1fe92c7b9306",  # Russian
    "fed3567d30342993b8d5545e",  # German
}
WAITING_IDS = {
    "82c7469c9f25fe8b8b8a824b",  # Italian stale running
    "16a82bba4db0c580ebbc26a9",  # English waiting
}


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def preview(store: CapCutHandoffStore) -> tuple[dict, list[dict]]:
    ledger = store.load_date(DATE_VALUE)
    records = [
        task for task in ledger.get("tasks", [])
        if str(task.get("remotePipelineTaskId") or "") == PARENT_TASK_ID
    ]
    identities = {str(task.get("taskId") or "") for task in records}
    expected = COMPLETED_IDS | WAITING_IDS
    if identities != expected:
        raise RuntimeError(
            "migration stopped: expected exactly four SPC floor identities; "
            f"found={sorted(identities)}"
        )
    unexpected = [task for task in records if task.get("status") == "completed" and str(task.get("taskId")) not in COMPLETED_IDS]
    if unexpected:
        raise RuntimeError("migration stopped: completed identity does not match the approved two outputs")
    report = []
    for task in records:
        task_id = str(task.get("taskId") or "")
        report.append({
            "taskId": task_id,
            "language": task.get("language"),
            "before": task.get("status"),
            "after": "completed" if task_id in COMPLETED_IDS else "waiting_capcut",
            "outputExists": Path(str(task.get("finalOutputPath") or "")).is_file(),
            "outputBytes": Path(str(task.get("finalOutputPath") or "")).stat().st_size
            if Path(str(task.get("finalOutputPath") or "")).is_file() else 0,
        })
    return ledger, report


def apply(store: CapCutHandoffStore, ledger: dict) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    context_path = APP_DIR / "handoff" / "current_pipeline_context.json"
    context = load_json(context_path)
    if str(context.get("taskId") or "") != PARENT_TASK_ID:
        raise RuntimeError("migration stopped: current context no longer belongs to the approved SPC floor task")
    project_path = APP_DIR / "handoff" / "download" / "project_status.json"
    project = load_json(project_path)
    item = (project.get("items") or {}).get(PARENT_TASK_ID)
    if not isinstance(item, dict):
        raise RuntimeError("migration stopped: approved parent project is absent from project_status")

    archives = ledger.setdefault("archivedAttempts", [])
    for task in ledger["tasks"]:
        if str(task.get("remotePipelineTaskId") or "") != PARENT_TASK_ID:
            continue
        task_id = str(task.get("taskId") or "")
        if task_id in WAITING_IDS:
            # Re-running this approved migration is intentionally idempotent:
            # retain exactly one history row per stale task rather than growing
            # the audit list while no media is ever touched.
            if not any(
                str(row.get("taskId") or "") == task_id
                and str(row.get("reason") or "") == "pipeline_v3_stale_or_waiting_migration"
                for row in archives if isinstance(row, dict)
            ):
                archives.append({
                    "manifestId": str(task.get("activeManifestId") or ""),
                    "attemptId": str(task.get("activeAttemptId") or ""),
                    "taskId": task_id,
                    "archivedAt": timestamp,
                    "reason": "pipeline_v3_stale_or_waiting_migration",
                })
            task.update({
                "status": "waiting_capcut",
                "selected": True,
                "error": "",
                "currentPhase": "waiting_capcut",
                "phaseText": "Queued for v3 safe recovery",
                "heartbeatAt": "",
                "clientPid": 0,
            })
            # The old files remain on disk as diagnostic evidence, but no old
            # manifest is eligible to update the fresh v3 attempt.
            for key in ("handoffManifest", "statusFile", "activeManifestId", "activeAttemptId"):
                task.pop(key, None)
        elif task_id in COMPLETED_IDS:
            task.update({
                "status": "completed",
                "selected": False,
                "currentPhase": "completed",
                "phaseText": "Verified completed output retained",
            })
    store.save_date(DATE_VALUE, ledger)

    context.update({
        "schemaVersion": max(3, int(context.get("schemaVersion") or 1)),
        "productionDate": DATE_VALUE,
        "date": DATE_VALUE,
        "status": "capcut_pending",
        "pipelineStatus": "capcut_pending",
        "pipelineStateText": "CapCut 2/4 complete; Italian and English are safely waiting",
        "currentPhase": "waiting_capcut",
        "manifestId": "",
        "attemptId": "",
        "updatedAt": timestamp,
    })
    write_json_atomic(context_path, context)

    item.update({
        "productionDate": DATE_VALUE,
        "pipelineStatus": "capcut_pending",
        "pipelineStateText": "CapCut 2/4 complete; 2 waiting for safe recovery",
        "currentPhase": "waiting_capcut",
        "attemptId": "",
        "manifestId": "",
        "pipelineUpdatedAt": timestamp,
    })
    project["schemaVersion"] = max(3, int(project.get("schemaVersion") or 1))
    project["updatedAt"] = timestamp
    write_json_atomic(project_path, project)

    # A terminal continue command belongs to the old failed/stopped attempt.
    # Archive it so the Dashboard can create a new command for the two waiting
    # v3 tasks instead of continuing to display an old "blocked" result.
    command_path = APP_DIR / "handoff" / "control" / "continue_current_project.json"
    command = load_json(command_path)
    if (
        str(command.get("taskId") or "") == PARENT_TASK_ID
        and str(command.get("state") or "") in {"blocked", "failed", "cancelled"}
    ):
        command.update({
            "state": "expired",
            "stale": True,
            "expiredReason": "pipeline_v3 migration archived an old terminal recovery result",
            "message": "旧恢复结果已归档；当前项目可安全恢复等待任务",
            "archivedAt": timestamp,
        })
        write_json_atomic(command_path, command)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write only the approved status migration")
    args = parser.parse_args()
    store = CapCutHandoffStore(APP_DIR / "交接记录", Path("E:/"))
    ledger, report = preview(store)
    print(json.dumps({"mode": "apply" if args.apply else "preview", "parentTaskId": PARENT_TASK_ID, "tasks": report}, ensure_ascii=False, indent=2))
    if args.apply:
        apply(store, ledger)
        print("MIGRATION_APPLIED: no workers or media were started/changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
