"""Quarantine one proven wrong-draft CapCut output and requeue its exact task.

This is a one-purpose recovery tool for the 7.22 SPC wall-board Russian
attempt that reopened an SPC floor draft.  It is deliberately opt-in: without
``--apply`` it only reports the target and does not write anything.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from capcut_handoff import CapCutHandoffStore
from task_inbox import (CURRENT_CONTEXT_FILE, load_project_download_statuses,
                        save_current_context, save_project_download_status)


APP_DIR = Path(__file__).resolve().parent
DATE = "7.22"
TASK_ID = "ff91bd37d4540b7be0947dcc"
REASON = "draft_reopen_identity_mismatch: SPC wall-board task reopened an SPC floor draft"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="perform the reversible migration")
    args = parser.parse_args()

    store = CapCutHandoffStore(APP_DIR / "交接记录", Path("E:\\"))
    payload = store.load_date(DATE)
    task = next((item for item in payload.get("tasks") or [] if str(item.get("taskId") or "") == TASK_ID), None)
    if not isinstance(task, dict):
        raise SystemExit(f"task not found: {TASK_ID}")
    if str(task.get("project") or "") != "SPC墙板" or str(task.get("material") or "") != "晶岩墙板" or str(task.get("language") or "") != "俄语":
        raise SystemExit("identity mismatch: refusing to migrate a different task")

    output = Path(str(task.get("finalOutputPath") or ""))
    prior_quarantine = str((task.get("invalidOutput") or {}).get("quarantinedPath") or "")
    already_requeued = str(task.get("status") or "") == "waiting_capcut" and bool(prior_quarantine)
    if not already_requeued and not output.is_file():
        raise SystemExit(f"expected invalid artifact is missing: {output}")
    quarantine = APP_DIR / "handoff" / "quarantine" / "spc-wall-draft-reopen-identity-mismatch" / datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = quarantine / output.name
    print(f"task={TASK_ID}")
    print(f"output={output}")
    print(f"quarantine={prior_quarantine or destination}")
    print(f"apply={args.apply}")
    if not args.apply:
        return 0

    if not already_requeued:
        quarantine.mkdir(parents=True, exist_ok=False)
        shutil.move(str(output), str(destination))
        try:
            if not store.requeue_invalid_completed_task(DATE, TASK_ID, REASON, str(destination)):
                raise RuntimeError("ledger update refused")
        except Exception:
            # The artifact must not disappear if the ledger write fails.
            shutil.move(str(destination), str(output))
            raise

    # The erroneous Russian success shared one stale attempt with the other
    # three wall-board rows.  Archive each old attempt and make the complete
    # formal wall-board set 0/4 waiting; otherwise its old status file could
    # resurrect a running/complete row after the user clicks Safe Continue.
    payload = store.load_date(DATE)
    archived = payload.setdefault("archivedAttempts", [])
    reset_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for item in payload.get("tasks") or []:
        if str(item.get("project") or "") != "SPC墙板" or str(item.get("material") or "") != "晶岩墙板":
            continue
        if not item.get("wallDraftMismatchResetAt"):
            history = dict(item)
            history.update({
                "archivedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "archiveReason": "spc_wall_batch_reset_after_draft_reopen_identity_mismatch",
            })
            archived.append(history)
        item.update({
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
            "wallDraftMismatchResetAt": reset_at,
            # Explicitly tells the state bus that these four ledger rows are
            # a deliberate reset, not older metadata to be superseded by the
            # stale CapCut attempt that caused the identity mismatch.
            "contractResetAt": reset_at,
        })
    store.save_date(DATE, payload)

    statuses = load_project_download_statuses()
    records = statuses.get("items") if isinstance(statuses, dict) else {}
    owner = str(task.get("remotePipelineTaskId") or "").strip()
    record = records.get(owner) if isinstance(records, dict) else None
    if isinstance(record, dict):
        updated = dict(record)
        updated.update({
            "pipelineStatus": "capcut_pending",
            "pipelineStateText": "SPC墙板错误草稿成片已隔离；第5步恢复为0/4等待。",
            "pipelineUpdatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "currentPhase": "capcut_pending",
        })
        save_project_download_status(updated)
    try:
        context = json.loads(CURRENT_CONTEXT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        context = {}
    if isinstance(context, dict) and str(context.get("taskId") or "") == owner:
        context.update({
            "pipelineStatus": "capcut_pending",
            "pipelineStateText": "SPC墙板错误草稿成片已隔离；第5步恢复为0/4等待。",
            "pipelineUpdatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "currentPhase": "capcut_pending",
        })
        save_current_context(context)
    print("migration=completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
