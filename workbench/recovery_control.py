"""Durable Step 3/5 recovery control plane.

The Workbench, Dashboard and automatic poller use this module as the single
authority for identities, budgets and commands.  The mailbox is only a
redeliverable transport copy; the state file is authoritative.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

if os.name == "nt":
    import msvcrt


def _windows_process_start_time(pid: int) -> float | None:
    if os.name != "nt" or pid <= 0:
        return None
    class _FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime), ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        creation = _FileTime()
        exit_time = _FileTime()
        kernel_time = _FileTime()
        user_time = _FileTime()
        try:
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            if not ok:
                return None
            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return ticks / 10_000_000.0 - 11_644_473_600.0
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


SCHEMA_VERSION = 4
MAX_ATTEMPTS = 2
DELAY_SECONDS = 30
ACTIVE_COMMAND_STATES = {"claimed", "dispatched", "accepted", "executing", "running", "reconciling"}
TERMINAL_COMMAND_STATES = {"completed", "failed", "blocked"}
BLOCKING_COMMAND_STATES = {"reconciliation_required"}


class RecoveryControlError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _native_process_identity(pid: int) -> dict[str, Any]:
    if os.name != 'nt':
        return {'observation': 'unverifiable'}
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    api.OpenProcess.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    api.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return {'observation': 'exited' if ctypes.get_last_error() == 87 else 'unverifiable'}
    try:
        exit_code = ctypes.c_ulong()
        if not api.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return {'observation': 'unverifiable'}
        if exit_code.value != 259:
            return {'observation': 'exited'}
        size = ctypes.c_ulong(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return {'observation': 'unverifiable'}
        started = _windows_process_start_time(pid)
        return {'observation': 'matched_alive' if started is not None else 'unverifiable',
                'processStartTime': started, 'executable': name.value}
    finally:
        api.CloseHandle(handle)


def capture_process_identity(pid: int | None = None, *, role: str = "") -> dict[str, Any]:
    """Return an OS process identity suitable for crash reconciliation.

    ``startedAt`` in old records was a workbench session UUID and cannot prove
    PID ownership.  New records use the operating-system creation timestamp;
    if it cannot be read, reconciliation must pause instead of guessing.
    """
    value = int(pid or os.getpid())
    result: dict[str, Any] = {"pid": value, "role": role}
    try:
        import psutil

        process = psutil.Process(value)
        result["processStartTime"] = float(process.create_time())
        with contextlib.suppress(psutil.Error, OSError):
            result["executable"] = str(process.exe() or "")
        with contextlib.suppress(psutil.Error, OSError):
            result["processName"] = str(process.name() or "")
    except ImportError:
        result.update(_native_process_identity(value))
        result.setdefault('processStartTime', None)
        if result["processStartTime"] is None:
            result["identityError"] = "process_identity_unavailable"
    except Exception as exc:
        result["processStartTime"] = None
        result["identityError"] = type(exc).__name__
    return result


def observe_process_identity(identity: dict[str, Any] | None) -> str:
    """Classify a saved PID as matched_alive, exited, pid_reused or unverifiable."""
    saved = identity if isinstance(identity, dict) else {}
    try:
        pid = int(saved.get("pid") or 0)
    except (TypeError, ValueError):
        return "unverifiable"
    expected_start = saved.get("processStartTime")
    if pid <= 0 or expected_start in (None, ""):
        return "unverifiable"
    try:
        import psutil

        process = psutil.Process(pid)
        if not process.is_running():
            return "exited"
        actual_start = float(process.create_time())
        if abs(actual_start - float(expected_start)) > 0.001:
            return "pid_reused"
        expected_exe = str(saved.get("executable") or "").strip().casefold()
        if expected_exe:
            actual_exe = str(process.exe() or "").strip().casefold()
            if actual_exe and actual_exe != expected_exe:
                return "pid_reused"
        return "matched_alive"
    except ImportError:
        actual = _native_process_identity(pid)
        if actual['observation'] != 'matched_alive':
            return actual['observation']
        if abs(actual['processStartTime'] - float(expected_start)) > 0.001:
            return 'pid_reused'
        expected_exe = str(saved.get('executable') or '').strip()
        if not expected_exe:
            return 'unverifiable'
        return 'matched_alive' if os.path.normcase(actual['executable']) == os.path.normcase(expected_exe) else 'pid_reused'
    except (ProcessLookupError, FileNotFoundError):
        return "exited"
    except Exception as exc:
        try:
            import psutil
            if isinstance(exc, psutil.NoSuchProcess):
                return "exited"
            if isinstance(exc, psutil.AccessDenied):
                return "unverifiable"
        except (ImportError, AttributeError):
            pass
        return "unverifiable"
    return "unverifiable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    try:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            return None
        result = datetime.fromisoformat(text)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _stable_part(value: Any) -> str:
    return str(value or "").strip()


def resolve_recovery_identity(task: dict[str, Any], producer_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve a stable Step 3/5 identity without parent-task fallback."""
    facts = producer_facts or {}
    step = _stable_part(facts.get("step") or task.get("step")).lower()
    if step in {"3", "step_3", "voice", "audio"}:
        step = "step3"
    elif step in {"5", "step_5", "capcut", "video", "output"}:
        step = "step5"
    elif step in {"12", "step_12", "step1", "step2", "download", "subtitle", "step12"}:
        # Step 1/2 has its own durable URL/checkpoint contract.  It must not
        # be forced through the Fish or CapCut child identity rules.
        step = "step12"
    if step not in {"step3", "step5", "step12"}:
        return {"verified": False, "error": "recovery_identity_unverified: unsupported step"}
    run_group = _stable_part(facts.get("runGroupId") or task.get("runGroupId"))
    batch = _stable_part(facts.get("batchId") or task.get("batchId"))
    canonical = _stable_part(facts.get("canonicalTaskId") or task.get("canonicalTaskId") or task.get("taskId"))
    if not run_group or not batch or not canonical:
        return {"verified": False, "error": "recovery_identity_unverified: production identity incomplete"}
    if step == "step12":
        child = _stable_part(facts.get("downloadTaskId") or facts.get("taskId") or facts.get("childTaskId") or canonical)
        contract_hash = _stable_part(facts.get("urlContractHash") or facts.get("sourceUrlContractHash"))
        if not child or not contract_hash:
            return {"verified": False, "error": "recovery_identity_unverified: download contract incomplete"}
        key = "v1|step12|" + "|".join([run_group, batch, canonical, child, contract_hash])
        return {
            "verified": True, "step": step, "stableRecoveryKey": key,
            "runGroupId": run_group, "batchId": batch,
            "canonicalTaskId": canonical, "childTaskId": child,
            "downloadTaskId": child, "urlContractHash": contract_hash,
        }
    if step == "step3":
        child = _stable_part(facts.get("audioTaskId") or facts.get("voiceTaskId") or facts.get("childTaskId"))
        product = _stable_part(facts.get("product") or facts.get("material") or task.get("material"))
        language = _stable_part(facts.get("language") or task.get("language"))
        text_hash = _stable_part(facts.get("copyTextHash") or facts.get("textHash") or task.get("copyTextHash"))
        voice_contract = _stable_part(facts.get("voiceContractId") or facts.get("voiceId") or task.get("voiceContractId"))
        # A remote Fish run can be a single producer command for several
        # language rows.  Before the first copy is selected there is no
        # per-language text hash yet, but the batch itself still has a
        # verifiable contract (task identity + material + language plan +
        # mapped voices).  Use that explicit aggregate contract instead of
        # rejecting Safe Continue or falling back to the parent task as a
        # sibling language identity.
        audio_batch_contract = _stable_part(facts.get("audioBatchContractId"))
        if audio_batch_contract:
            batch_language_set = _stable_part(
                facts.get("languageSet") or language or ""
            )
            pending_copy_contract = _stable_part(
                facts.get("copyContractState") or text_hash or "copy-plan-pending-v1"
            )
            if not child:
                child = _stable_part(facts.get("childTaskId")) or f"voice-batch-v1|{audio_batch_contract}"
            if not product or not batch_language_set or not voice_contract:
                return {"verified": False, "error": "recovery_identity_unverified: audio batch contract incomplete"}
            parts = [
                "v5", step, run_group, batch, canonical, child, product,
                batch_language_set, audio_batch_contract, pending_copy_contract,
                voice_contract,
            ]
            return {
                "verified": True,
                "step": step,
                "stableRecoveryKey": "|".join(parts),
                "runGroupId": run_group,
                "batchId": batch,
                "canonicalTaskId": canonical,
                "childTaskId": child,
                "audioBatchContractId": audio_batch_contract,
                "contractScope": "audio_batch",
            }
        if not child or not product or not language or not text_hash or not voice_contract:
            return {"verified": False, "error": "recovery_identity_unverified: audio contract incomplete"}
        parts = ["v4", step, run_group, batch, canonical, child, product, language, text_hash, voice_contract]
    else:
        child = _stable_part(facts.get("outputTaskId") or facts.get("capcutTaskId") or facts.get("videoTaskId"))
        source_hash = _stable_part(
            facts.get("sourceVideoHash")
            or facts.get("sourceHash")
            or facts.get("legacySourceIdentity")
        )
        output_identity = _stable_part(facts.get("outputIdentity") or facts.get("outputPath") or child)
        if not child or not source_hash or not output_identity:
            return {"verified": False, "error": "recovery_identity_unverified: output identity incomplete"}
        parts = ["v4", step, run_group, batch, canonical, child, source_hash, output_identity]
    key = "|".join(parts)
    return {
        "verified": True,
        "step": step,
        "stableRecoveryKey": key,
        "runGroupId": run_group,
        "batchId": batch,
        "canonicalTaskId": canonical,
        "childTaskId": child,
    }


def _default_state() -> dict[str, Any]:
    return {"schemaVersion": SCHEMA_VERSION, "manualPause": True, "entries": {}, "commands": {}, "history": []}


@contextlib.contextmanager
def _os_lock(path: Path, timeout: float = 5.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        if os.name == "nt":
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RecoveryControlError("recovery_lock_busy", "recovery lock busy")
                    time.sleep(0.05)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RecoveryControlError("recovery_lock_busy", "recovery lock busy")
                    time.sleep(0.05)
        yield
    finally:
        try:
            if acquired and os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif acquired:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return _default_state()
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise RecoveryControlError("recovery_state_corrupt", f"recovery state unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") not in {2, 3, SCHEMA_VERSION}:
        raise RecoveryControlError("recovery_state_schema_unsupported", "recovery schema is not supported")
    if not isinstance(payload.get("entries"), dict):
        raise RecoveryControlError("recovery_state_corrupt", "recovery state collections are invalid")
    if not isinstance(payload.get("commands", {}), dict) or not isinstance(payload.get("history", []), list):
        raise RecoveryControlError("recovery_state_corrupt", "recovery command collections are invalid")
    for key, entry in payload["entries"].items():
        if not isinstance(entry, dict):
            raise RecoveryControlError("recovery_state_corrupt", f"recovery entry is invalid: {key}")
        try:
            attempts = int(entry.get("automaticAttemptCount") or 0)
        except (TypeError, ValueError):
            raise RecoveryControlError("recovery_state_corrupt", f"invalid recovery budget: {key}")
        if attempts < 0 or attempts > MAX_ATTEMPTS:
            raise RecoveryControlError("recovery_state_corrupt", f"recovery budget out of range: {key}")
        if entry.get("nextEligibleAt") and _parse_time(entry.get("nextEligibleAt")) is None:
            raise RecoveryControlError("recovery_state_corrupt", f"invalid recovery time: {key}")
    for key, command in payload.get("commands", {}).items():
        if not isinstance(command, dict) or str(command.get("commandId") or key) != str(key):
            raise RecoveryControlError("recovery_state_corrupt", f"invalid recovery command: {key}")
        if not str(command.get("stableRecoveryKey") or "").strip():
            raise RecoveryControlError("recovery_state_corrupt", f"command identity missing: {key}")
    if payload.get("schemaVersion") == 2:
        for item in payload["entries"].values():
            if not isinstance(item, dict) or not str(item.get("childTaskId") or "").strip() or not str(item.get("step") or "").strip() or not str(item.get("stableRecoveryKey") or "").strip():
                raise RecoveryControlError("recovery_migration_blocked", "v2 entry lacks a proven child task identity")
    # v3 files were written before command history was separated.  They are
    # safe to normalize in memory; no budget is reset and no entry is dropped.
    if not isinstance(payload.get("commands"), dict):
        payload["commands"] = {}
    if not isinstance(payload.get("history"), list):
        payload["history"] = []
    if payload.get("schemaVersion") in {2, 3}:
        payload["migratedFromSchema"] = int(payload["schemaVersion"])
        payload["schemaVersion"] = SCHEMA_VERSION
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        last_error = None
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                if attempt >= 3:
                    raise RecoveryControlError("recovery_status_write_failed", str(exc)) from exc
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise RecoveryControlError("recovery_status_write_failed", str(last_error))
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


class RecoveryControl:
    def __init__(self, state_path: str | Path, lock_path: str | Path, mailbox_path: str | Path):
        self.state_path = Path(state_path)
        self.lock_path = Path(lock_path)
        self.mailbox_path = Path(mailbox_path)

    def acceptance_verified(self) -> bool:
        """Fail closed until a release report binds real evidence to this build.

        Neither a manual request nor a saved enable checkbox grants acceptance.
        This method is read-only; the controller never manufactures the report.
        """
        try:
            report_path = self.state_path.with_name('recovery_acceptance.json')
            report = json.loads(report_path.read_text(encoding='utf-8-sig'))
            if report.get('schemaVersion') != 1 or report.get('status') != 'accepted':
                return False
            builds = report['builds']
            required = {str(Path(__file__).resolve()), str(Path(__file__).with_name('workbench.py').resolve())}
            if not required.issubset({str(Path(p).resolve()) for p in builds}):
                return False
            for p, digest in builds.items():
                if hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest:
                    return False
            for step in ('step3', 'step5'):
                evidence = report['realAcceptance'][step]
                if evidence.get('result') != 'passed' or evidence.get('isolated') is not True:
                    return False
                if hashlib.sha256(Path(evidence['path']).read_bytes()).hexdigest() != evidence['sha256']:
                    return False
            return True
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def status(self) -> dict[str, Any]:
        try:
            with _os_lock(self.lock_path):
                return _read_json(self.state_path)
        except RecoveryControlError as exc:
            state = _default_state()
            state.update({"readError": f"{exc.code}: {exc}"})
            return state

    def enqueue_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Dashboard transport never overwrites the production mailbox."""
        command_id = str(request.get('commandId') or '')
        if not command_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in command_id):
            raise RecoveryControlError('recovery_request_invalid', 'invalid request id')
        path = self.state_path.parent / 'recovery-requests' / (command_id + '.json')
        with _os_lock(self.lock_path):
            if path.exists():
                old = json.loads(path.read_text(encoding='utf-8-sig'))
                if any(old.get(k) != request.get(k) for k in ('taskId', 'runGroupId', 'batchId')):
                    raise RecoveryControlError('recovery_command_identity_conflict', 'request target changed')
                return old
            payload = {**request, 'state': 'requested'}
            _write_json(path, payload)
            return payload

    def amend_request_identity(self, command_id: str, patch: dict[str, Any], *, task_id: str) -> dict[str, Any]:
        """Fill missing identity facts on an existing requested transport item.

        This is intentionally narrower than enqueue: it cannot change the
        command ID, target task, or an already populated identity field, and
        it only operates before delivery.  It lets a migrated request gain a
        verified Step 1/2 contract without creating a second command.
        """
        command_id = str(command_id or '').strip()
        if not command_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in command_id):
            raise RecoveryControlError('recovery_request_invalid', 'invalid request id')
        path = self.state_path.parent / 'recovery-requests' / (command_id + '.json')
        with _os_lock(self.lock_path):
            if not path.exists():
                raise RecoveryControlError('recovery_request_not_found', 'request not found')
            current = json.loads(path.read_text(encoding='utf-8-sig'))
            if str(current.get('taskId') or '') != str(task_id or ''):
                raise RecoveryControlError('recovery_command_identity_conflict', 'request target changed')
            if str(current.get('state') or '') != 'requested':
                return current
            for key, value in patch.items():
                if value in (None, ''):
                    continue
                existing = current.get(key)
                if existing not in (None, '') and str(existing) != str(value):
                    raise RecoveryControlError('recovery_command_identity_conflict', f'{key} already differs')
                current[key] = value
            _write_json(path, current)
            return current

    def requeue_unclaimed_request(self, command_id: str, *, task_id: str) -> dict[str, Any]:
        """Re-arm the same request after a claim was blocked before command creation."""
        command_id = str(command_id or '').strip()
        with _os_lock(self.lock_path):
            path = self.state_path.parent / 'recovery-requests' / (command_id + '.json')
            if not path.exists():
                raise RecoveryControlError('recovery_request_not_found', 'request not found')
            current = json.loads(path.read_text(encoding='utf-8-sig'))
            if str(current.get('taskId') or '') != str(task_id or ''):
                raise RecoveryControlError('recovery_command_identity_conflict', 'request target changed')
            if str(current.get('state') or '') not in {'requested', 'delivered'}:
                return current
            state = _read_json(self.state_path)
            if command_id in state.get('commands', {}):
                return current
            mailbox = {}
            if self.mailbox_path.exists():
                mailbox = json.loads(self.mailbox_path.read_text(encoding='utf-8-sig'))
            mailbox_id = str(mailbox.get('commandId') or mailbox.get('recoveryCommandId') or '')
            if mailbox_id and mailbox_id != command_id:
                raise RecoveryControlError('recovery_command_identity_conflict', 'mailbox belongs to another command')
            if mailbox and str(mailbox.get('state') or '').lower() not in {'waiting', 'blocked', 'requested', 'delivered'}:
                return current
            current['state'] = 'requested'
            _write_json(path, current)
            mailbox = {**mailbox, **current, 'state': 'requested', 'phase': 'recovery_requeued'}
            _write_json(self.mailbox_path, mailbox)
            return current

    def deliver_next_request(self) -> dict[str, Any] | None:
        """Deliver transport only when authority and mailbox are unoccupied."""
        with _os_lock(self.lock_path):
            state = _read_json(self.state_path)
            # A stale command from another production step must not prevent a
            # factually independent recovery (for example an old Fish step 3
            # reconciliation record blocking the current download step 1/2).
            # Requests without an explicit step/key remain conservative and
            # are blocked by any unresolved command.
            pending_requests = list((self.state_path.parent / 'recovery-requests').glob('*.json'))
            def request_order(path):
                try:
                    payload = json.loads(path.read_text(encoding='utf-8-sig'))
                    return (str(payload.get('requestedAt') or '9999'), path.name)
                except (OSError, ValueError, TypeError):
                    return ('9999', path.name)
            pending_requests.sort(key=request_order)
            # Older builds marked a different same-step request as ``observed``
            # while its predecessor was still reconciling.  Once that exact
            # predecessor is conclusively failed (the producer exited and
            # reported failure), re-arm the user's distinct request under its
            # original ID.  Never turn a duplicate of the same stable task into
            # another execution, and never re-arm an unknown/ambiguous result.
            for candidate_path in pending_requests:
                try:
                    candidate = json.loads(candidate_path.read_text(encoding='utf-8-sig'))
                except (OSError, ValueError, TypeError):
                    continue
                if str(candidate.get('state') or '').lower() != 'observed':
                    continue
                observed_id = str(candidate.get('observedCommandId') or '').strip()
                blocker = state['commands'].get(observed_id)
                if not isinstance(blocker, dict) or str(blocker.get('state') or '').lower() != 'failed':
                    continue
                candidate_step = str(candidate.get('step') or '').strip().lower()
                blocker_step = str(blocker.get('step') or '').strip().lower()
                candidate_key = str(candidate.get('stableRecoveryKey') or '').strip()
                blocker_key = str(blocker.get('stableRecoveryKey') or '').strip()
                receipt = blocker.get('executionReceipt')
                if (
                    not candidate_step or candidate_step != blocker_step
                    or not candidate_key or not blocker_key or candidate_key == blocker_key
                    or not isinstance(receipt, dict)
                    or str(receipt.get('status') or '').lower() != 'failed'
                    or str(receipt.get('processObservation') or '').lower() not in {'exited', 'pid_reused'}
                    or str(candidate.get('commandId') or '') in state['commands']
                ):
                    continue
                candidate['state'] = 'requested'
                candidate.pop('observedCommandId', None)
                candidate.pop('observedAt', None)
                candidate['requeuedAfterCommandId'] = observed_id
                candidate['requeuedAt'] = _now()
                _write_json(candidate_path, candidate)
            requested_candidates = []
            for candidate_path in pending_requests:
                try:
                    candidate = json.loads(candidate_path.read_text(encoding='utf-8-sig'))
                except (OSError, ValueError, TypeError):
                    continue
                if candidate.get('state') == 'requested':
                    requested_candidates.append((candidate_path, candidate))
            # Prefer a fully identified request. Old Dashboard retries may
            # remain in the transport folder without a step/key; they must
            # not hide a newer, verified Step 1/2 recovery request.
            selected_pair = next((pair for pair in requested_candidates
                                  if str(pair[1].get('step') or '').strip()
                                  and str(pair[1].get('stableRecoveryKey') or '').strip()), None)
            if selected_pair is None and requested_candidates:
                selected_pair = requested_candidates[0]
            requested = selected_pair[1] if selected_pair else None
            if requested is not None:
                requested_step = str(requested.get('step') or '').strip().lower()
                requested_key = str(requested.get('stableRecoveryKey') or '').strip()
                matching_command = None
                same_step_blocker = None
                for command in state['commands'].values():
                    if command.get('state') not in ACTIVE_COMMAND_STATES | BLOCKING_COMMAND_STATES:
                        continue
                    command_step = str(command.get('step') or '').strip().lower()
                    command_key = str(command.get('stableRecoveryKey') or '').strip()
                    if not requested_step or not requested_key:
                        return None
                    if not command_step or not command_key:
                        return None
                    if command_key == requested_key:
                        matching_command = command
                        break
                    if command_step == requested_step:
                        # A different child in the same producer stage is not
                        # a duplicate of the active command.  Keep the user's
                        # request queued until the current operation reaches a
                        # terminal, reconciled state instead of consuming it as
                        # an observation that can never be retried.
                        same_step_blocker = command
                if same_step_blocker is not None and matching_command is None:
                    return None
                if matching_command is not None:
                    # A repeated Dashboard click for an already active stable
                    # recovery is an observation, not a second execution.
                    for duplicate_path, duplicate in requested_candidates:
                        if (str(duplicate.get('taskId') or '') == str(requested.get('taskId') or '')
                                and str(duplicate.get('step') or '').strip().lower() == requested_step
                                and str(duplicate.get('stableRecoveryKey') or '').strip() == requested_key):
                            duplicate['state'] = 'observed'
                            duplicate['observedCommandId'] = str(matching_command.get('commandId') or '')
                            duplicate['observedAt'] = _now()
                            _write_json(duplicate_path, duplicate)
                    return None
            elif any(c.get('state') in ACTIVE_COMMAND_STATES | BLOCKING_COMMAND_STATES for c in state['commands'].values()):
                return None
            if self.mailbox_path.exists():
                existing = json.loads(self.mailbox_path.read_text(encoding='utf-8-sig'))
                if existing.get('state') in {'requested', 'accepted', 'running', 'recovering', 'executing'}:
                    return None
            ordered_candidates = []
            if selected_pair is not None:
                ordered_candidates.append(selected_pair)
            ordered_candidates.extend(pair for pair in requested_candidates if pair is not selected_pair)
            for path, request in ordered_candidates:
                old = state['commands'].get(request.get('commandId'))
                if old:
                    request['state'] = 'observed'
                    request['observedCommandId'] = str(old.get('commandId') or request.get('commandId') or '')
                    request['observedAt'] = _now()
                    _write_json(path, request)
                    continue
                _write_json(self.mailbox_path, request)
                # If we crash here, the same mailbox ID is still authoritative
                # for transport; it cannot be replaced by a later request.
                request['state'] = 'delivered'
                _write_json(path, request)
                delivered_step = str(request.get('step') or '').strip().lower()
                delivered_key = str(request.get('stableRecoveryKey') or '').strip()
                if delivered_step and delivered_key:
                    for duplicate_path, duplicate in requested_candidates:
                        if str(duplicate.get('commandId') or '') == str(request.get('commandId') or ''):
                            continue
                        if (str(duplicate.get('taskId') or '') == str(request.get('taskId') or '')
                                and str(duplicate.get('step') or '').strip().lower() == delivered_step
                                and str(duplicate.get('stableRecoveryKey') or '').strip() == delivered_key):
                            duplicate['state'] = 'observed'
                            duplicate['observedCommandId'] = str(request.get('commandId') or '')
                            duplicate['observedAt'] = _now()
                            _write_json(duplicate_path, duplicate)
                return request
            return None

    def observe_failure(self, identity: dict[str, Any], failure: dict[str, Any]) -> dict[str, Any]:
        if not identity.get("verified"):
            raise RecoveryControlError("recovery_identity_unverified", str(identity.get("error") or "identity missing"))
        key = identity["stableRecoveryKey"]
        now = _now()
        with _os_lock(self.lock_path):
            state = _read_json(self.state_path)
            entry = state["entries"].setdefault(key, {**identity, "automaticAttemptCount": 0, "manualPause": state.get("manualPause", True)})
            fingerprint = str(failure.get("failureFingerprint") or "")
            if fingerprint and fingerprint == entry.get("failureFingerprint") and entry.get("failureEventId"):
                return entry
            if str(entry.get("commandState") or "") in (ACTIVE_COMMAND_STATES | BLOCKING_COMMAND_STATES) and entry.get("activeCommandId"):
                secondary = list(entry.get("secondaryErrors") or [])
                code = str(failure.get("errorCode") or "")
                if code and code not in secondary:
                    secondary.append(code)
                entry["secondaryErrors"] = secondary
                _write_json(self.state_path, state)
                return dict(entry)
            event_id = str(failure.get("failureEventId") or uuid.uuid4().hex)
            failed_at = str(failure.get("failedAt") or now)
            failed_dt = _parse_time(failed_at) or datetime.now(timezone.utc)
            entry.update({
                **identity,
                "failureEventId": event_id,
                "failureFingerprint": fingerprint or hashlib.sha256(f"{key}|{failure.get('errorCode')}|{now}".encode()).hexdigest()[:24],
                "failedAt": failed_at,
                "nextEligibleAt": str(failure.get("nextEligibleAt") or (failed_dt + timedelta(seconds=DELAY_SECONDS)).isoformat(timespec="seconds")),
                "primaryError": entry.get("primaryError") or str(failure.get("errorCode") or "UNKNOWN_FAILURE"),
                "secondaryErrors": list(entry.get("secondaryErrors") or []),
                "state": "waiting",
                "commandState": "none",
            })
            _write_json(self.state_path, state)
            return dict(entry)

    def claim_recovery(
        self,
        identity: dict[str, Any],
        trigger: str,
        request_id: str | None = None,
        failure: dict[str, Any] | None = None,
        resource_guard: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not identity.get("verified"):
            return {"accepted": False, "code": "recovery_identity_unverified", "message": identity.get("error")}
        key = identity["stableRecoveryKey"]
        with _os_lock(self.lock_path):
            state = _read_json(self.state_path)
            aliases = state.setdefault('requestAliases', {})
            if not isinstance(aliases, dict):
                raise RecoveryControlError('recovery_state_corrupt', 'request aliases invalid')
            if request_id:
                old = aliases.get(str(request_id)) or state['commands'].get(str(request_id))
                if old and old.get('stableRecoveryKey') != key:
                    raise RecoveryControlError('recovery_command_identity_conflict', 'request id belongs to another identity')
                if old:
                    original = state['commands'].get(old.get('commandId'))
                    if original and original.get('state') in TERMINAL_COMMAND_STATES:
                        return {'accepted': False, 'code': 'recovery_command_terminal', 'command': original}
            entry = state["entries"].get(key)
            if not isinstance(entry, dict):
                if not failure:
                    return {"accepted": False, "code": "recovery_failure_missing"}
                now = _now()
                failed_at = str(failure.get("failedAt") or now)
                failed_dt = _parse_time(failed_at) or datetime.now(timezone.utc)
                entry = {
                    **identity,
                    "failureEventId": str(failure.get("failureEventId") or uuid.uuid4().hex),
                    "failureFingerprint": str(failure.get("failureFingerprint") or ""),
                    "failedAt": failed_at,
                    "nextEligibleAt": str(failure.get("nextEligibleAt") or (failed_dt + timedelta(seconds=DELAY_SECONDS)).isoformat(timespec="seconds")),
                    "primaryError": str(failure.get("errorCode") or "UNKNOWN_FAILURE"),
                    "secondaryErrors": [],
                    "automaticAttemptCount": 0,
                    "state": "waiting",
                    "commandState": "none",
                }
                state["entries"][key] = entry
            active_id = str(entry.get("activeCommandId") or "")
            if active_id:
                command = state["commands"].get(active_id, {})
                command_state = str(command.get("state") or "")
                if command_state in ACTIVE_COMMAND_STATES | BLOCKING_COMMAND_STATES:
                    # Different UI callers for the same stable task converge
                    # on the already-active command.  The request is an
                    # alias, not a new production attempt.
                    if request_id:
                        aliases[str(request_id)] = {'commandId': active_id, 'stableRecoveryKey': key}
                        _write_json(self.state_path, state)
                    return {"accepted": True, "reused": True, "command": command, "entry": entry}
            if request_id:
                existing = state["commands"].get(str(request_id))
                if isinstance(existing, dict) and str(existing.get("stableRecoveryKey") or "") != key:
                    raise RecoveryControlError("recovery_command_identity_conflict", "request id belongs to another recovery identity")
                if isinstance(existing, dict) and str(existing.get("state") or "") in TERMINAL_COMMAND_STATES | BLOCKING_COMMAND_STATES:
                    return {"accepted": False, "code": "recovery_command_terminal", "command": existing, "entry": entry}
            if resource_guard is not None:
                try:
                    if resource_guard():
                        return {'accepted': False, 'code': 'recovery_resources_busy'}
                except Exception as exc:
                    return {'accepted': False, 'code': 'recovery_resources_unverifiable', 'message': str(exc)}
            if trigger == "automatic":
                if not self.acceptance_verified():
                    return {'accepted': False, 'code': 'recovery_not_accepted'}
                if int(entry.get("automaticAttemptCount") or 0) >= MAX_ATTEMPTS:
                    return {"accepted": False, "code": "recovery_budget_exhausted"}
                # One automatic command is allowed for one failure event.  A
                # second command requires observe_failure() to record a new
                # producer failure event; polling the same terminal state must
                # never spend the second budget slot.
                if str(entry.get("claimedFailureEventId") or "") == str(entry.get("failureEventId") or ""):
                    return {"accepted": False, "code": "recovery_failure_already_claimed", "entry": entry}
                if bool(state.get("manualPause")):
                    return {"accepted": False, "code": "recovery_paused"}
                eligible = _parse_time(entry.get("nextEligibleAt"))
                if eligible is None:
                    return {"accepted": False, "code": "recovery_time_invalid"}
                if datetime.now(timezone.utc) < eligible:
                    return {"accepted": False, "code": "recovery_not_due", "nextEligibleAt": entry.get("nextEligibleAt")}
            # A stale command from another pipeline step (for example a Fish
            # Step 3 reconciliation) is not the same recovery identity as a
            # verified Step 1/2 download command.  Keep the same-step and
            # unknown-identity cases blocked, but do not let the former
            # reserve this task's independent recovery slot forever.
            for existing_command in state['commands'].values():
                if existing_command.get('state') not in ACTIVE_COMMAND_STATES | BLOCKING_COMMAND_STATES:
                    continue
                existing_step = str(existing_command.get('step') or '').strip().lower()
                existing_key = str(existing_command.get('stableRecoveryKey') or '').strip()
                current_step = str(identity.get('step') or '').strip().lower()
                if (not existing_step or not existing_key or
                        not current_step or existing_step == current_step or existing_key == key):
                    return {'accepted': False, 'code': 'recovery_resources_reserved'}
            command_id = str(request_id or f"recovery-{uuid.uuid4().hex}")
            count = int(entry.get("automaticAttemptCount") or 0) + (1 if trigger == "automatic" else 0)
            command = {
                "schemaVersion": SCHEMA_VERSION,
                "commandId": command_id,
                "stableRecoveryKey": key,
                "taskId": identity.get('canonicalTaskId', ''),
                "trigger": trigger,
                "failureEventId": entry.get("failureEventId"),
                "state": "claimed",
                "claimedAt": _now(),
                "automaticAttemptCount": count,
                "ownerProcessIdentity": capture_process_identity(os.getpid(), role="recovery-controller"),
                **{field: identity.get(field, "") for field in ("step", "runGroupId", "batchId", "canonicalTaskId", "childTaskId")},
            }
            entry.update({**identity, "activeCommandId": command_id, "commandState": "claimed", "automaticAttemptCount": count, "state": "claimed"})
            entry["claimedFailureEventId"] = entry.get("failureEventId")
            state["commands"][command_id] = command
            aliases[command_id] = {'commandId': command_id, 'stableRecoveryKey': key}
            _write_json(self.state_path, state)
            try:
                _write_json(self.mailbox_path, {**command, "state": "requested", "source": "automatic_recovery" if trigger == "automatic" else "manual"})
            except Exception:
                # Keep the durable claim.  Reconciliation will re-deliver the
                # exact command ID; never roll back the budget or mint another.
                raise RecoveryControlError("recovery_dispatch_pending", "claim persisted but mailbox write failed")
            command["state"] = "dispatched"
            state["commands"][command_id] = command
            entry["commandState"] = "dispatched"
            entry["state"] = "dispatched"
            _write_json(self.state_path, state)
            return {"accepted": True, "reused": False, "command": command, "entry": entry}

    def accept_command(self, command_id: str, consumer_identity: dict[str, Any], resource_guard=None) -> dict[str, Any]:
        with _os_lock(self.lock_path):
            state = _read_json(self.state_path)
            command = state["commands"].get(str(command_id))
            if not isinstance(command, dict):
                raise RecoveryControlError("recovery_command_unknown", "unknown recovery command")
            current_state = str(command.get("state") or "")
            if current_state in TERMINAL_COMMAND_STATES:
                return {**command, "decision": "terminal"}
            if current_state in BLOCKING_COMMAND_STATES:
                return {**command, "decision": "reconcile_required"}
            if current_state in {"accepted", "executing", "running", "reconciling"}:
                return {**command, "decision": "observe_existing"}
            if resource_guard is not None:
                try:
                    if resource_guard():
                        return {**command, 'decision': 'waiting', 'reason': 'recovery_resources_busy'}
                except Exception:
                    return {**command, 'decision': 'waiting', 'reason': 'recovery_resources_unverifiable'}
            command["state"] = "accepted"
            command["acceptedAt"] = _now()
            command["consumerProcessIdentity"] = dict(consumer_identity or {})
            state["commands"][str(command_id)] = command
            entry = state["entries"].get(command.get("stableRecoveryKey"), {})
            entry["commandState"] = "accepted"
            entry["state"] = "accepted"
            _write_json(self.state_path, state)
            return {**command, "decision": "execute_new"}

    def record_execution(self, command_id: str, receipt: dict[str, Any], state_name: str = "running") -> dict[str, Any]:
        if state_name not in ACTIVE_COMMAND_STATES | TERMINAL_COMMAND_STATES | BLOCKING_COMMAND_STATES:
            raise RecoveryControlError("recovery_state_invalid", f"invalid command state: {state_name}")
        with _os_lock(self.lock_path):
            state = _read_json(self.state_path)
            command = state["commands"].get(str(command_id))
            if not isinstance(command, dict):
                raise RecoveryControlError("recovery_command_unknown", "unknown recovery command")
            current_state = str(command.get("state") or "")
            if current_state in TERMINAL_COMMAND_STATES:
                return {**command, "ignored": True}
            if current_state in BLOCKING_COMMAND_STATES and state_name not in TERMINAL_COMMAND_STATES:
                return {**command, "ignored": True}
            if state_name in {"accepted", "executing", "running"} and current_state in {"completed", "failed", "blocked", "reconciliation_required"}:
                return {**command, "ignored": True}
            command["state"] = state_name
            command["executionReceipt"] = {**(command.get('executionReceipt') or {}), **dict(receipt or {})}
            command["updatedAt"] = _now()
            if state_name in TERMINAL_COMMAND_STATES:
                command["finishedAt"] = _now()
            state["commands"][str(command_id)] = command
            entry = state["entries"].get(command.get("stableRecoveryKey"), {})
            entry["commandState"] = state_name
            entry["state"] = state_name
            if state_name in TERMINAL_COMMAND_STATES:
                entry["activeCommandId"] = ""
                state["history"].append({"commandId": command_id, "stableRecoveryKey": command.get("stableRecoveryKey"), "finishedAt": command.get("finishedAt"), "state": state_name})
            _write_json(self.state_path, state)
            return dict(command)

    def reconcile_pending_commands(self, observer: Callable[[dict[str, Any]], dict[str, Any]], *, allow_redispatch: bool = True, resource_guard: Callable[..., bool] | None = None) -> list[dict[str, Any]]:
        results = []
        state = self.status()
        if state.get("readError"):
            return [{"code": "recovery_state_corrupt", "message": state["readError"]}]
        for command in list((state.get("commands") or {}).values()):
            if not isinstance(command, dict) or command.get("state") not in (ACTIVE_COMMAND_STATES | BLOCKING_COMMAND_STATES):
                continue
            # Manual Dashboard requests are consumed by the Workbench mailbox
            # on its Tk polling path.  The background automatic reconciler can
            # run in the same 2-second window and otherwise observes the new
            # command before the consumer has written its process receipt,
            # turning a valid request into reconciliation_required.  Give only
            # the initial manual dispatch states a bounded hand-off window;
            # an old accepted command still falls through to normal evidence
            # reconciliation after the grace period.
            if str(command.get("trigger") or "").lower() == "manual" and str(command.get("state") or "").lower() in {"claimed", "dispatched", "accepted"}:
                try:
                    mailbox = json.loads(self.mailbox_path.read_text(encoding="utf-8-sig")) if self.mailbox_path.exists() else {}
                except (OSError, ValueError, TypeError):
                    mailbox = {}
                mailbox_state = str(mailbox.get("state") or "").lower() if isinstance(mailbox, dict) else ""
                mailbox_command = str(mailbox.get("commandId") or "") if isinstance(mailbox, dict) else ""
                dispatch_time = _parse_time(str(command.get("acceptedAt") or command.get("claimedAt") or ""))
                age_seconds = (datetime.now(timezone.utc) - dispatch_time).total_seconds() if dispatch_time else 999999.0
                if mailbox_command == str(command.get("commandId") or "") and mailbox_state in {"requested", "accepted"} and age_seconds < 120:
                    results.append({"commandId": command.get("commandId"), "status": "manual_dispatch_pending"})
                    continue
            observation = observer(command) or {"status": "unknown"}
            status = str(observation.get("status") or "unknown")
            if status == "alive":
                results.append({"commandId": command.get("commandId"), "status": "alive"})
            elif status == "completed":
                results.append(self.record_execution(command["commandId"], observation, "completed"))
            elif status == "failed":
                results.append(self.record_execution(command["commandId"], observation, "failed"))
            elif status == "redispatch":
                if not allow_redispatch:
                    results.append({"commandId": command.get("commandId"), "status": "redispatch_blocked_paused"})
                    continue
                if resource_guard is not None:
                    try:
                        try:
                            busy = resource_guard(command)
                        except TypeError:
                            busy = resource_guard()
                        if busy:
                            results.append({"commandId": command.get("commandId"), "status": "redispatch_blocked_resources"})
                            continue
                    except Exception as exc:
                        results.append({"commandId": command.get("commandId"), "status": "redispatch_blocked_unverifiable", "message": str(exc)})
                        continue
                with _os_lock(self.lock_path):
                    latest = _read_json(self.state_path)
                    current = latest["commands"].get(command["commandId"])
                    if latest.get('manualPause', True):
                        results.append({'commandId': command['commandId'], 'status': 'redispatch_blocked_paused'})
                        continue
                    if current and current.get('trigger') == 'automatic' and not self.acceptance_verified():
                        results.append({'commandId': command['commandId'], 'status': 'recovery_not_accepted'})
                        continue
                    if isinstance(current, dict) and current.get("state") in {"claimed", "dispatched"}:
                        _write_json(self.mailbox_path, {**current, "state": "requested", "source": "recovery_reconcile"})
                        current["state"] = "dispatched"
                        latest["commands"][command["commandId"]] = current
                        _write_json(self.state_path, latest)
                        results.append({"commandId": command.get("commandId"), "status": "redispatched"})
                    else:
                        results.append({'commandId': command['commandId'], 'status': 'reconciliation_required'})
            else:
                self.record_execution(command["commandId"], observation, "reconciliation_required")
                results.append({"commandId": command.get("commandId"), "status": "reconciliation_required"})
        return results

    def set_session_paused(self, session_identity: dict[str, Any], paused: bool) -> dict[str, Any]:
        with _os_lock(self.lock_path):
            state = _read_json(self.state_path)
            state["manualPause"] = bool(paused)
            state["pauseUpdatedAt"] = _now()
            state["pauseIdentity"] = dict(session_identity or {})
            _write_json(self.state_path, state)
            return state
