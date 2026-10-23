from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import time
from pathlib import Path

from baidu_upload import CONFIG_FILE, UploadCoordinator, atomic_write_json, load_config, now_iso
from common_io import pid_is_alive, process_image_name


APP_DIR = Path(__file__).resolve().parent
PID_FILE = APP_DIR / "handoff" / "upload" / "upload_service.pid"
SERVICE_ERROR_LOG = APP_DIR / "handoff" / "upload" / "upload_service_errors.log"
_SERVICE_MUTEX_HANDLE = None


def _process_exists(pid: int) -> bool:
    """Whether the PID in a leftover PID file may still be an upload service.

    Read-only: ``os.kill(pid, 0)`` terminates the target on Windows, and after
    a reboot that PID can belong to any program.  The service runs under
    Python, so a live PID held by another executable is a stale file.
    """
    if pid <= 0 or not pid_is_alive(pid):
        return False
    image = Path(process_image_name(pid)).name.lower()
    return not image or image.startswith("python")


def _acquire_windows_mutex() -> bool:
    global _SERVICE_MUTEX_HANDLE
    if os.name != "nt":
        return True
    if _SERVICE_MUTEX_HANDLE:
        return False
    digest = hashlib.sha256(str(APP_DIR).lower().encode("utf-8")).hexdigest()[:24]
    name = f"Local\\AIVideoUploadService-{digest}"
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _SERVICE_MUTEX_HANDLE = handle
    return True


def _release_windows_mutex() -> None:
    global _SERVICE_MUTEX_HANDLE
    if os.name == "nt" and _SERVICE_MUTEX_HANDLE:
        try:
            ctypes.windll.kernel32.CloseHandle(_SERVICE_MUTEX_HANDLE)
        finally:
            _SERVICE_MUTEX_HANDLE = None


def acquire_pid_file() -> bool:
    if not _acquire_windows_mutex():
        return False
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PID_FILE.is_file():
        try:
            existing = int(PID_FILE.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            existing = 0
        if _process_exists(existing):
            _release_windows_mutex()
            return False
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            _release_windows_mutex()
            return False
    try:
        atomic_write_json(
            PID_FILE.with_suffix(".json"),
            {
                "pid": os.getpid(),
                "startedAt": now_iso(),
                "script": str(Path(__file__).resolve()),
            },
        )
        temporary = PID_FILE.with_name(f".{PID_FILE.name}.{os.getpid()}.tmp")
        temporary.write_text(str(os.getpid()), encoding="ascii")
        os.replace(temporary, PID_FILE)
        return True
    except Exception:
        _release_windows_mutex()
        raise


def release_pid_file() -> None:
    try:
        if int(PID_FILE.read_text(encoding="ascii").strip()) == os.getpid():
            PID_FILE.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    try:
        PID_FILE.with_suffix(".json").unlink(missing_ok=True)
    except OSError:
        pass
    _release_windows_mutex()


def _record_service_error(message: str) -> None:
    try:
        SERVICE_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SERVICE_ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{now_iso()} {message}\n")
    except OSError:
        pass


def _safe_save_state(coordinator: UploadCoordinator) -> bool:
    try:
        coordinator.save_state()
        return True
    except Exception as exc:
        _record_service_error(f"state_write_failed: {exc}")
        return False


def main() -> int:
    if not acquire_pid_file():
        return 0
    coordinator = UploadCoordinator(CONFIG_FILE)
    started_at = now_iso()
    try:
        while True:
            coordinator.state.update(
                {
                    "running": True,
                    "servicePid": os.getpid(),
                    "serviceStartedAt": started_at,
                    "serviceHeartbeatAt": now_iso(),
                }
            )
            try:
                coordinator.run_once()
                coordinator.state["serviceError"] = ""
            except Exception as exc:
                coordinator.state["running"] = True
                coordinator.state["serviceError"] = str(exc)
                coordinator.state["serviceHeartbeatAt"] = now_iso()
                _record_service_error(f"run_once_failed: {exc}")
            _safe_save_state(coordinator)
            config = load_config(CONFIG_FILE)
            time.sleep(max(2, int(config.get("scan_interval_seconds") or 5)))
    except KeyboardInterrupt:
        return 0
    finally:
        coordinator.state["running"] = False
        coordinator.state["serviceStoppedAt"] = now_iso()
        coordinator.state["serviceHeartbeatAt"] = now_iso()
        _safe_save_state(coordinator)
        release_pid_file()


if __name__ == "__main__":
    sys.exit(main())

