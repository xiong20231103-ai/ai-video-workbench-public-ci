import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import fastapi  # noqa: F401  # the receiver runs in TikHubOPS\venv_cuda
except ImportError:
    raise unittest.SkipTest("fastapi is only installed in the receiver's runtime")

import task_receiver
import workbench


class WorkbenchLaunchTests(unittest.TestCase):
    def _runtime_patches(self, root: Path):
        (root / "launch.ps1").write_text("# test launcher\n", encoding="utf-8")
        (root / "workbench.py").write_text("# test workbench\n", encoding="utf-8")
        return (
            patch.object(task_receiver, "APP_DIR", root),
            patch.object(task_receiver, "WORKBENCH_INSTANCE_FILE", root / "handoff" / "control" / "workbench.pid"),
            patch.object(task_receiver, "WORKBENCH_LAUNCH_FILE", root / "handoff" / "control" / "workbench.launching"),
            patch.object(task_receiver, "WORKBENCH_READY_FILE", root / "handoff" / "control" / "workbench.ready.json"),
            patch.object(task_receiver, "WORKBENCH_START_LOG_FILE", root / "handoff" / "control" / "workbench_start.log"),
        )

    def test_existing_registered_instance_is_reused(self):
        # Isolate every registration file: ensure_workbench_running() reads
        # and may delete the ready/PID records of a live production workbench.
        with tempfile.TemporaryDirectory() as folder:
            runtime_patches = self._runtime_patches(Path(folder))
            with runtime_patches[0], runtime_patches[1], runtime_patches[2], runtime_patches[3], runtime_patches[4], \
                    patch.object(task_receiver, "_registered_workbench_pid", return_value=2468), \
                    patch.object(task_receiver, "_workbench_window_pid", return_value=2468), \
                    patch.object(task_receiver, "_pid_is_alive", return_value=True), \
                    patch.object(task_receiver.subprocess, "Popen") as popen:
                result = task_receiver.ensure_workbench_running()
        self.assertTrue(result["alreadyRunning"])
        self.assertFalse(result["started"])
        self.assertEqual(2468, result["pid"])
        popen.assert_not_called()

    def test_missing_instance_is_started_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime_patches = self._runtime_patches(root)
            process = Mock(pid=4321, poll=Mock(return_value=None))
            with runtime_patches[0], runtime_patches[1], runtime_patches[2], runtime_patches[3], runtime_patches[4], \
                    patch.object(task_receiver, "_registered_workbench_pid", return_value=0), \
                    patch.object(task_receiver, "_workbench_window_pid", return_value=0), \
                    patch.object(task_receiver, "_resolve_workbench_runtime", return_value=root / "python.exe"), \
                    patch.object(task_receiver, "_read_workbench_ready", side_effect=[{}, {"pid": 9876, "sessionId": "ready"}]), \
                    patch.object(task_receiver.time, "sleep"), \
                    patch.object(task_receiver.subprocess, "Popen", return_value=process) as popen:
                result = task_receiver.ensure_workbench_running()
            self.assertTrue(result["started"])
            self.assertTrue(result["ready"])
            self.assertFalse(result["alreadyRunning"])
            self.assertEqual(9876, result["pid"])
            popen.assert_called_once()

    def test_recent_launch_claim_prevents_duplicate_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime_patches = self._runtime_patches(root)
            launch_file = root / "handoff" / "control" / "workbench.launching"
            launch_file.parent.mkdir(parents=True, exist_ok=True)
            launch_file.write_text("123\n", encoding="ascii")
            with runtime_patches[0], runtime_patches[1], runtime_patches[2], runtime_patches[3], runtime_patches[4], \
                    patch.object(task_receiver, "_registered_workbench_pid", return_value=0), \
                    patch.object(task_receiver, "_workbench_window_pid", return_value=0), \
                    patch.object(task_receiver.subprocess, "Popen") as popen:
                result = task_receiver.ensure_workbench_running()
            self.assertTrue(result["startPending"])
            self.assertFalse(result["started"])
            popen.assert_not_called()

    def test_stale_pid_is_removed_before_direct_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime_patches = self._runtime_patches(root)
            pid_file = root / "handoff" / "control" / "workbench.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text("46228", encoding="ascii")
            with runtime_patches[0], runtime_patches[1], runtime_patches[2], runtime_patches[3], runtime_patches[4], \
                    patch.object(task_receiver, "_pid_is_alive", return_value=False):
                task_receiver._clear_stale_workbench_registration()
            self.assertFalse(pid_file.exists())

    def test_process_exit_reports_structured_startup_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime_patches = self._runtime_patches(root)
            process = Mock(pid=4321, poll=Mock(return_value=7), returncode=7)
            with runtime_patches[0], runtime_patches[1], runtime_patches[2], runtime_patches[3], runtime_patches[4], \
                    patch.object(task_receiver, "_registered_workbench_pid", return_value=0), \
                    patch.object(task_receiver, "_workbench_window_pid", return_value=0), \
                    patch.object(task_receiver, "_resolve_workbench_runtime", return_value=root / "python.exe"), \
                    patch.object(task_receiver, "_read_workbench_ready", return_value={}), \
                    patch.object(task_receiver.time, "sleep"), \
                    patch.object(task_receiver.subprocess, "Popen", return_value=process):
                with self.assertRaises(task_receiver.WorkbenchStartupError) as raised:
                    task_receiver.ensure_workbench_running()
            self.assertEqual("process_exited", raised.exception.code)


class LoginStartupTests(unittest.TestCase):
    def _run_startup(self, login, pending=False, error=None):
        with patch.object(task_receiver, "LOGIN_STARTUP", login), \
                patch.object(task_receiver, "replay_pipeline_outbox"), \
                patch.object(task_receiver, "read_tasks", return_value=[]), \
                patch.object(task_receiver, "reconcile_pipeline_store"), \
                patch.object(task_receiver, "_has_auto_start_task", return_value=pending), \
                patch.object(task_receiver, "_start_workbench_if_needed", side_effect=error) as start, \
                patch.object(task_receiver, "_write_workbench_start_log") as log:
            task_receiver.launch_workbench_for_existing_auto_task()
            return start.call_count, log.call_count

    def test_login_opens_workbench_with_empty_queue(self):
        self.assertEqual((1, 0), self._run_startup(True))

    def test_login_with_auto_task_launches_only_once(self):
        self.assertEqual((1, 0), self._run_startup(True, pending=True))

    def test_normal_empty_receiver_does_not_open_workbench(self):
        self.assertEqual((0, 0), self._run_startup(False))

    def test_normal_receiver_still_recovers_pending_auto_task(self):
        self.assertEqual((1, 0), self._run_startup(False, pending=True))

    def test_launch_failure_is_logged_without_stopping_receiver(self):
        self.assertEqual((1, 1), self._run_startup(True, error=OSError("missing runtime")))


class WorkbenchInstanceTests(unittest.TestCase):
    def test_pid_file_prevents_second_workbench_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "control"
            instance_file = root / "workbench.pid"
            launch_file = root / "workbench.launching"
            # The kernel mutex name is derived from APP_DIR; a temporary
            # APP_DIR keeps this test off the production workbench's mutex
            # and lock file while still exercising the real locking code.
            with patch.object(workbench, "APP_DIR", Path(folder)), \
                    patch.object(workbench, "WORKBENCH_LOCK_FILE", root / "workbench.instance.lock"), \
                    patch.object(workbench, "WORKBENCH_INSTANCE_FILE", instance_file), \
                    patch.object(workbench, "WORKBENCH_LAUNCH_FILE", launch_file):
                self.assertTrue(workbench.acquire_workbench_instance())
                self.assertFalse(workbench.acquire_workbench_instance())
                workbench.release_workbench_instance()
                self.assertFalse(instance_file.exists())


if __name__ == "__main__":
    unittest.main()
