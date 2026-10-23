"""One-time guarded repair for the 8.6 bridge-start false gap.

This never starts production.  It only converts the synthetic 24/24 missing
link result back into a resumable runtime interruption after proving that the
original task still owns 24 distinct submitted URLs and 24 matching rows.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DOWNLOAD = ROOT / "handoff" / "download"
PROJECT_STATUS = DOWNLOAD / "project_status.json"
BATCH_STATE = DOWNLOAD / "download_batch_state.json"
TASK_ID = "inbox-20260806101159-43990e72"
BATCH_ID = "dispatch-20260806-101123-93f476"
EXPECTED = 24


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".migration.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    projects = read_json(PROJECT_STATUS)
    session = read_json(BATCH_STATE)
    task = session.get("activeExternalTask") or {}
    record = (projects.get("items") or {}).get(TASK_ID) or {}
    urls = [str(value).strip() for value in task.get("urls") or [] if str(value).strip()]
    rows = session.get("tasks") if isinstance(session.get("tasks"), list) else []
    row_urls = [str(item.get("url") or "").strip() for item in rows if isinstance(item, dict)]

    facts = (
        str(task.get("taskId") or "") == TASK_ID,
        str(task.get("batchId") or "") == BATCH_ID,
        str(record.get("batchId") or "") == BATCH_ID,
        len(urls) == EXPECTED,
        len(set(urls)) == EXPECTED,
        len(rows) == EXPECTED,
        set(row_urls) == set(urls),
        int(record.get("expectedCount") or 0) == EXPECTED,
        int(record.get("completed") or 0) == 0,
    )
    if not all(facts):
        raise RuntimeError("8.6 false-gap identity/count audit failed; no files changed")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "implementation-backups" / f"false-gap-8.6-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(PROJECT_STATUS, backup / PROJECT_STATUS.name)
    shutil.copy2(BATCH_STATE, backup / BATCH_STATE.name)

    runtime_failure = {
        "errorCode": "DOWNLOAD_BRIDGE_RUNTIME_FAILED",
        "phase": "bridge_runtime",
        "message": "下载桥在写入逐条终态前退出；24条原始链接完整保留，可安全继续",
        "taskId": TASK_ID,
        "batchId": BATCH_ID,
        "migratedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    record.update({
        "completed": 0,
        "failed": 0,
        "missingCount": 0,
        "terminal": False,
        "gapActive": False,
        "failureItems": [],
        "runtimeFailure": runtime_failure,
        "status": "blocked_download_runtime",
        "stateText": runtime_failure["message"],
        "pipelineStatus": "blocked_download_runtime",
        "pipelineStateText": runtime_failure["message"],
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    session["lastBatchResult"] = {}
    session["downloadRuntimeFailure"] = runtime_failure
    session["taskErrors"] = {}
    session["stats"] = {
        "total": str(EXPECTED),
        "success": "0",
        "failed": "0",
        "progress": "0%",
        "status": runtime_failure["message"],
    }
    for item in rows:
        url = str(item.get("url") or "").strip()
        item["values"] = [
            url, "处理中", "未开始", "等待", "0%", "•••",
            "下载桥运行中断，等待安全继续",
        ]
    session["updatedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")

    atomic_write(PROJECT_STATUS, projects)
    atomic_write(BATCH_STATE, session)
    print(json.dumps({
        "ok": True,
        "taskId": TASK_ID,
        "urlCount": len(urls),
        "status": "blocked_download_runtime",
        "backup": str(backup),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
