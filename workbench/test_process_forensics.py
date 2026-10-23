import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import process_forensics


class PreviousAbnormalExitTests(unittest.TestCase):
    def _ready(self, root, payload):
        path = Path(root) / "workbench.ready.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_dead_owner_left_ready_file_is_reported_with_heartbeat(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._ready(root, {"pid": 31804, "heartbeatAt": "2026-10-21T14:21:30+08:00"})
            message = process_forensics.previous_abnormal_exit(path, 56224, lambda pid: False)
        self.assertIn("31804", message)
        self.assertIn("2026-10-21T14:21:30+08:00", message)

    def test_clean_exit_live_owner_or_own_record_reports_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "absent.json"
            self.assertEqual("", process_forensics.previous_abnormal_exit(missing, 1, lambda pid: False))
            path = self._ready(root, {"pid": 4648})
            self.assertEqual("", process_forensics.previous_abnormal_exit(path, 9, lambda pid: True))
            self.assertEqual("", process_forensics.previous_abnormal_exit(path, 4648, lambda pid: False))

    def test_corrupt_or_foreign_shapes_report_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "workbench.ready.json"
            for raw in ("{", "[]", json.dumps({"pid": "x"}), json.dumps({"pid": 0})):
                path.write_text(raw, encoding="utf-8")
                self.assertEqual("", process_forensics.previous_abnormal_exit(path, 1, lambda pid: False))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._hooks = (sys.excepthook, threading.excepthook)

    def tearDown(self):
        sys.excepthook, threading.excepthook = self._hooks
        import faulthandler
        faulthandler.disable()
        if process_forensics._FATAL_HANDLE is not None:
            process_forensics._FATAL_HANDLE.close()
            process_forensics._FATAL_HANDLE = None

    def test_uncaught_thread_exception_reaches_log_and_fatal_file(self):
        lines = []
        with tempfile.TemporaryDirectory() as root:
            process_forensics.install(root, lines.append)

            def boom():
                raise RuntimeError("sttn worker died")

            worker = threading.Thread(target=boom, name="sttn-test")
            worker.start()
            worker.join()
            process_forensics._FATAL_HANDLE.flush()
            fatal = (Path(root) / "workbench-fatal.log").read_text(encoding="utf-8")
            process_forensics._FATAL_HANDLE.close()
            process_forensics._FATAL_HANDLE = None
        self.assertTrue(any("sttn-test" in line and "sttn worker died" in line for line in lines))
        self.assertIn("faulthandler armed", fatal)
        self.assertIn("RuntimeError: sttn worker died", fatal)

    def test_logger_failure_never_escapes(self):
        def broken(_line):
            raise OSError("disk full")

        with tempfile.TemporaryDirectory() as root:
            record = process_forensics.install(root, broken)
            try:
                raise ValueError("x")
            except ValueError as exc:
                record("测试", type(exc), exc, exc.__traceback__)
            process_forensics._FATAL_HANDLE.close()
            process_forensics._FATAL_HANDLE = None


if __name__ == "__main__":
    unittest.main()
