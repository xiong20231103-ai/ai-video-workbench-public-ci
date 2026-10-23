"""Small Windows named-mutex guard shared by the two submitter windows."""
from __future__ import annotations

import ctypes
import os


_HANDLE = None
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080


def acquire_instance_lock(name: str) -> bool:
    """Return False when another instance currently owns the named mutex."""
    global _HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, True, f"Local\\{name}")
    if not handle:
        return True
    result = kernel32.WaitForSingleObject(handle, 0)
    if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
        kernel32.CloseHandle(handle)
        return False
    _HANDLE = handle
    return True
