import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import workbench


class AutomaticRecoveryPolicyTests(unittest.TestCase):
    def _shell(self, root, record, task):
        app = object.__new__(workbench.Workbench)
        app.active_external_task = task
        app.auto_recovery_var = SimpleNamespace(get=lambda: True, set=lambda _value: None)
        app._auto_recovery_busy = False
        app.download_process = None
        app.voice_batch_process = None
        app.stitcher_controller = None
        app.status_var = SimpleNamespace(set=lambda _value: None)
        app.write_log = lambda _value: None
        app._current_capcut_client_is_running = lambda _task_id: False
        # The unit fixture has no production process table; isolate the
        # controller policy from the host's protected Windows processes.
        app._auto_recovery_resources_busy = lambda _task_id, _ignore_command_id="": False
        app._examplebrand_global_idle = lambda _task, _items: True
        app._read_local_json_object = lambda path: (
            json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else {}
        )
        app._set_remote_pipeline_status = lambda *_args, **_kwargs: None
        app.capcut_store = SimpleNamespace(load_date=lambda _date: {"tasks": []})
        app.after = lambda *_args: None
        return app

    def test_first_failure_waits_and_claim_is_per_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "pipelineStatus": "blocked_capcut",
                "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY",
                "runGroupId": "run-1",
                "batchId": "batch-1",
                "taskId": "parent-1",
                "capcutTaskId": "video-child-1",
                "canonicalTaskId": "parent-1",
                "sourceVideoHash": "video-hash",
                "outputPath": "E:/out/video-child-1.mp4",
                "pipelineUpdatedAt": datetime.now(timezone.utc).isoformat(),
            }
            task = {
                "taskId": "parent-1", "canonicalTaskId": "parent-1",
                "runGroupId": "run-1", "batchId": "batch-1", "project": "ProjectD",
            }
            app = self._shell(root, record, task)
            # This test covers the retry policy once a release has passed
            # real acceptance; the acceptance gate itself fails closed.
            with patch.object(workbench, "AUTO_RECOVERY_STATE_FILE", root / "recovery.json"), \
                 patch.object(workbench, "AUTO_RECOVERY_LOCK_FILE", root / "recovery.lock"), \
                 patch.object(workbench, "CONTINUE_COMMAND_FILE", root / "continue.json"), \
                 patch.object(workbench.RecoveryControl, "acceptance_verified", return_value=True), \
                 patch.object(workbench, "load_project_download_statuses", return_value={"items": {"parent-1": record}}):
                app._write_auto_recovery_state({"schemaVersion": 2, "manualPause": False, "entries": {}})
                def settle_poll():
                    future = app.__dict__.get("_recovery_poll_future")
                    if future is not None:
                        future.result(timeout=5)
                app.poll_automatic_recovery()
                settle_poll()
                state = app._read_auto_recovery_state()
                key = app._auto_recovery_candidate()["stableKey"]
                self.assertEqual(state["entries"][key]["automaticAttemptCount"], 0)
                self.assertFalse((root / "continue.json").exists())

                state["entries"][key]["nextEligibleAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                app._write_auto_recovery_state(state)
                app.poll_automatic_recovery()
                settle_poll()
                command = json.loads((root / "continue.json").read_text(encoding="utf-8"))
                self.assertEqual(command["source"], "automatic_recovery")
                self.assertEqual(command["childTaskId"], "video-child-1")
                state = app._read_auto_recovery_state()
                self.assertEqual(state["entries"][key]["automaticAttemptCount"], 1)

    def test_corrupt_state_stays_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "recovery.json").write_text("{broken", encoding="utf-8")
            with patch.object(workbench, "AUTO_RECOVERY_STATE_FILE", root / "recovery.json"), \
                 patch.object(workbench, "AUTO_RECOVERY_LOCK_FILE", root / "recovery.lock"):
                app = object.__new__(workbench.Workbench)
                state = app._read_auto_recovery_state()
                self.assertTrue(state["manualPause"])
                self.assertTrue(state.get("readError"))

    def test_step5_without_output_identity_is_paused_not_parent_scoped(self):
        record = {
            "pipelineStatus": "blocked_capcut",
            "errorCode": "CAPCUT_EDITOR_WINDOW_NOT_READY",
            "runGroupId": "run-1", "batchId": "batch-1", "taskId": "parent-1",
            "pipelineUpdatedAt": datetime.now(timezone.utc).isoformat(),
        }
        task = {"taskId": "parent-1", "canonicalTaskId": "parent-1", "runGroupId": "run-1", "batchId": "batch-1"}
        app = self._shell(Path("."), record, task)
        with patch.object(workbench, "load_project_download_statuses", return_value={"items": {"parent-1": record}}):
            candidate = app._auto_recovery_candidate()
        self.assertFalse(candidate["identityVerified"])
        self.assertIn("recovery_identity_unverified", candidate["identityError"])

    def test_pending_multi_language_voice_batch_can_build_recovery_identity(self):
        task = {
            "taskId": "formal-batch-1-01",
            "canonicalTaskId": "formal-batch-1-01",
            "runGroupId": "run-1",
            "batchId": "batch-1",
            "productionDate": "9.11",
            "project": "ProjectD",
            "material": "碳岩板",
            "languagePlan": [
                {"code": "es", "name": "西班牙语", "audioCount": 1, "outputCount": 1},
                {"code": "pt", "name": "葡萄牙语", "audioCount": 1, "outputCount": 1},
            ],
            "pipelineProfile": {"3_voice": {"audioBatch": "9.11"}},
        }
        app = object.__new__(workbench.Workbench)
        app.active_external_task = task
        # Repo-relative so the checked-in voice rules are found on any machine.
        app._voice_client_path = lambda: Path(__file__).resolve().parents[1] / "voice-client" / "BatchVoiceClient.exe"
        app.capcut_store = SimpleNamespace(load_date=lambda _date: {"tasks": []})
        with patch.object(workbench, "load_project_download_statuses", return_value={"items": {}}):
            identity = app._continue_recovery_identity(task)
        self.assertTrue(identity["verified"])
        self.assertEqual(identity["contractScope"], "audio_batch")
        self.assertNotEqual(identity["childTaskId"], task["canonicalTaskId"])


if __name__ == "__main__":
    unittest.main()
