"""Leave evidence when the Workbench process ends unexpectedly.

The Workbench runs under pythonw, so stderr is discarded. On 2026-10-21 PID
31804 vanished during STTN subtitle removal with no log line, no Windows
error event and no traceback, so the cause could not be established. This
module makes the next occurrence diagnosable without changing production
behaviour: native faults dump every thread's stack to a file, uncaught
exceptions reach the Workbench log, and a ready file left behind by a dead
process is reported at the next start.
"""
import faulthandler
import json
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

_FATAL_HANDLE = None


def install(log_dir, log):
    """Route fatal faults and uncaught exceptions to durable files.

    ``log`` is the Workbench line logger. Never raises: forensics must not
    stop production.
    """
    global _FATAL_HANDLE
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        if _FATAL_HANDLE is None:
            _FATAL_HANDLE = open(Path(log_dir) / "workbench-fatal.log", "a", encoding="utf-8")
            _FATAL_HANDLE.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} faulthandler armed\n")
            _FATAL_HANDLE.flush()
            faulthandler.enable(file=_FATAL_HANDLE, all_threads=True)
    except OSError:
        pass

    def _record(prefix, exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
        try:
            log(f"{prefix}：{exc_type.__name__}: {exc}")
            if _FATAL_HANDLE is not None:
                _FATAL_HANDLE.write(f"--- {datetime.now().isoformat(timespec='seconds')} {prefix}\n{text}\n")
                _FATAL_HANDLE.flush()
        except Exception:
            pass

    sys.excepthook = lambda t, e, tb: _record("主线程未捕获异常", t, e, tb)
    threading.excepthook = lambda args: _record(
        f"后台线程 {getattr(args.thread, 'name', '?')} 未捕获异常",
        args.exc_type, args.exc_value, args.exc_traceback)
    return _record


def previous_abnormal_exit(ready_file, current_pid, pid_is_alive):
    """Describe a ready record whose owner died without removing it.

    A clean shutdown always removes its own ready file, so a record from a
    different, no-longer-running PID means the previous Workbench was killed
    or crashed. Returns "" when there is nothing to report.
    """
    try:
        payload = json.loads(Path(ready_file).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return ""
    if pid <= 0 or pid == current_pid or pid_is_alive(pid):
        return ""
    return (f"上次工作台进程（pid {pid}）未正常退出：就绪记录未被清除，"
            f"最后心跳 {payload.get('heartbeatAt') or '未知'}；可能被外部结束或崩溃，"
            "原因请查 workbench-fatal.log 与当时日志")
