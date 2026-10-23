"""Shared file and process helpers for every workbench runtime.

The workbench, task receiver, TikHub bridge, subtitle worker, OCR service and
upload service all launch their scripts from this folder under different
Python environments, so this module must stay standard-library only.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """Local time with its UTC offset, to the second."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    attempts: int = 8,
    retry_errors: tuple[type[BaseException], ...] = (PermissionError,),
) -> None:
    """Replace ``path`` with ``payload`` as JSON without ever exposing a partial file.

    Every call stages into its own uniquely named temporary file, so concurrent
    writers cannot overwrite each other's staging data, and fsyncs it before
    the replace.  On Windows a short-lived reader or antivirus handle can make
    ``os.replace`` fail with WinError 5, so ``retry_errors`` are retried with a
    short backoff before the last one is raised.  Text mode keeps the platform
    line endings every existing state file already uses.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(attempts):
            try:
                os.replace(temporary, path)
                return
            except retry_errors:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def pid_is_alive(pid: int) -> bool:
    """Report whether ``pid`` is a running process without signalling it.

    Never use ``os.kill(pid, 0)`` on Windows: signal 0 is not a probe there and
    terminates the target process.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            # use_last_error: without it get_last_error() always reads 0.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5  # Access denied: the process exists.
            exit_code = ctypes.c_ulong()
            try:
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and int(exit_code.value) == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_image_name(pid: int) -> str:
    """Return the executable path of a live process, or "" when unreadable.

    Read-only, like ``pid_is_alive``; used to tell a client process from an
    unrelated program that was later given the same PID.
    """
    if pid <= 0 or os.name != "nt":
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(32768)
            name = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                return ""
            return name.value
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return ""
