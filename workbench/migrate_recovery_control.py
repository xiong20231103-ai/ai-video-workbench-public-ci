"""Read-only recovery state audit.

Usage: python migrate_recovery_control.py [state.json]
It never rewrites production state; it classifies entries for a controlled
migration or leaves them blocked.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from recovery_control import observe_process_identity


def audit(path: str | Path) -> dict:
    target = Path(path)
    report = {"path": str(target), "state": "missing", "entries": [], "commands": [], "migrationBlocked": False}
    if not target.is_file():
        return report
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        report.update({"state": "unreadable", "migrationBlocked": True, "error": f"{type(exc).__name__}: {exc}"})
        return report
    if not isinstance(payload, dict):
        report.update({"state": "invalid", "migrationBlocked": True, "error": "state is not an object"})
        return report
    report["state"] = "readable"
    report["schemaVersion"] = payload.get("schemaVersion")
    report["manualPause"] = bool(payload.get("manualPause", True))
    for key, entry in (payload.get("entries") or {}).items():
        if not isinstance(entry, dict):
            report["entries"].append({"key": key, "classification": "migration_blocked", "reason": "entry is not an object"})
            report["migrationBlocked"] = True
            continue
        proven = bool(entry.get("stableRecoveryKey") and entry.get("childTaskId") and entry.get("step"))
        report["entries"].append({
            "key": key,
            "childTaskId": entry.get("childTaskId"),
            "step": entry.get("step"),
            "automaticAttemptCount": entry.get("automaticAttemptCount", 0),
            "commandState": entry.get("commandState"),
            "classification": "verified" if proven else "migration_blocked",
            "reason": "" if proven else "stable child identity is not proven",
        })
        report["migrationBlocked"] |= not proven
    for command_id, command in (payload.get("commands") or {}).items():
        if isinstance(command, dict):
            report["commands"].append({
                "commandId": command_id,
                "stableRecoveryKey": command.get("stableRecoveryKey"),
                "state": command.get("state"),
                "failureEventId": command.get("failureEventId"),
                "processIdentity": command.get("consumerProcessIdentity") or command.get("ownerProcessIdentity"),
                "processObservation": observe_process_identity(
                    command.get("consumerProcessIdentity") or command.get("ownerProcessIdentity")
                ),
            })
    return report


if __name__ == "__main__":
    default = Path(__file__).resolve().parent / "handoff" / "control" / "automatic_recovery.json"
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    print(json.dumps(audit(target), ensure_ascii=False, indent=2))
