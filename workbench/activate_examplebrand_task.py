from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from common_io import atomic_write_json as atomic_json
from examplebrand_production import derived_execution_profile, validate_static_resources


APP_DIR = Path(__file__).resolve().parent
PENDING_DIR = APP_DIR / "handoff" / "task_inbox" / "pending"
CONTROL_DIR = APP_DIR / "handoff" / "control" / "examplebrand_start_intents"
PROFILE_DIR = APP_DIR / "handoff" / "examplebrand_execution_profiles"
CONTRACT_DIR = APP_DIR / "handoff" / "task_inbox" / "contracts"


def activate(task_id: str, *, enabled: bool, delay_seconds: int = 60) -> tuple[Path, Path]:
    path = PENDING_DIR / f"{task_id}.json"
    contract_bytes = path.read_bytes()
    task = json.loads(contract_bytes.decode("utf-8-sig"))
    if not isinstance(task, dict) or str(task.get("taskId") or "") != task_id:
        raise ValueError("ExampleBrand pending task identity mismatch")
    validate_static_resources(task, APP_DIR.parent)
    contract_hash = hashlib.sha256(contract_bytes).hexdigest()
    contract_path = CONTRACT_DIR / f"{task_id}.json"
    if contract_path.exists():
        if hashlib.sha256(contract_path.read_bytes()).hexdigest() != contract_hash:
            raise ValueError("ExampleBrand immutable submission contract hash mismatch")
    else:
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = contract_path.with_name(f"{contract_path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(contract_bytes)
        os.replace(temporary, contract_path)
    profile = derived_execution_profile(task)
    profile["submissionContract"] = str(contract_path.resolve())
    profile["submissionContractHash"] = contract_hash
    profile_path = PROFILE_DIR / f"{task_id}.json"
    atomic_json(profile_path, profile)
    now = datetime.now().astimezone()
    intent = {
        "schemaVersion": 1,
        "taskId": task_id,
        "canonicalTaskId": str(task.get("canonicalTaskId") or task_id),
        "runGroupId": str(task.get("runGroupId") or ""),
        "batchId": str(task.get("batchId") or ""),
        "batchKind": "examplebrand-natural-flow",
        "startPolicy": "auto_when_idle",
        "enabled": bool(enabled),
        "executionProfile": str(profile_path.resolve()),
        "submissionContract": str(contract_path.resolve()),
        "submissionContractHash": contract_hash,
        "createdAt": now.isoformat(timespec="seconds"),
        "eligibleAfter": (now + timedelta(seconds=max(60, int(delay_seconds)))).isoformat(timespec="seconds"),
        "originalTaskHashPreserved": True,
    }
    intent_path = CONTROL_DIR / f"{task_id}.json"
    atomic_json(intent_path, intent)
    return profile_path, intent_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a ExampleBrand derived profile without mutating its receipt")
    parser.add_argument("task_id")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--delay-seconds", type=int, default=60)
    args = parser.parse_args()
    profile, intent = activate(args.task_id, enabled=args.enable, delay_seconds=args.delay_seconds)
    print(json.dumps({"profile": str(profile), "intent": str(intent), "enabled": args.enable}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
