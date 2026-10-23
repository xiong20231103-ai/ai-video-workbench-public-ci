import json
import tempfile
import unittest
import workbench as workbench_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workbench import (
    Workbench,
    capcut_failure_pipeline_status,
    completed_download_checkpoint_count,
    checkpoint_step12_resume_evidence,
    continue_command_target_matches,
    interrupted_step12_resume_evidence,
    resolve_continue_command_task,
    voice_terminal_predates_continue_command,
)


class RemotePipelineQueueTests(unittest.TestCase):
    def test_capcut_draft_preflight_failure_is_not_reported_as_launch_failure(self):
        state, text, code = capcut_failure_pipeline_status({
            "errorCode": "CAPCUT_RECOVERY_DRAFT_BINDING_LOST",
            "error": "prepared recovery draft is not singular",
        })
        self.assertEqual(state, "blocked_capcut_preflight")
        self.assertEqual(code, "CAPCUT_RECOVERY_DRAFT_BINDING_LOST")
        self.assertIn("客户端已启动", text)
        self.assertNotIn("未启动", text)

    def test_interrupted_step12_with_full_original_urls_is_resumable(self):
        urls = ["https://example.test/1", "https://example.test/2", "https://example.test/3"]
        task = {"taskId": "task-1", "expectedCount": 3, "urls": urls}
        record = {"expectedCount": 3, "completed": 1, "missingCount": 2, "gapActive": True}
        session = {
            "activeExternalTask": task,
            "context": {"request": {"taskId": "task-1", "urls": urls}},
            "lastBatchResult": {},
            "tasks": [
                {"url": urls[0], "values": [urls[0], "已完成", "已完成", "等待拼接配置"]},
                {"url": urls[1], "values": [urls[1], "已完成", "处理中", "STTN 修复"]},
                {"url": urls[2], "values": [urls[2], "处理中", "未开始", "等待"]},
            ],
        }

        resumable, reason = interrupted_step12_resume_evidence(task, record, session)

        self.assertTrue(resumable, reason)
        self.assertIn("interrupted", reason)

    def test_unstarted_import_with_full_rows_is_resumable_without_legacy_request(self):
        urls = ["https://example.test/1", "https://example.test/2"]
        task = {"taskId": "task-new", "expectedCount": 2, "urls": urls}
        record = {
            "expectedCount": 2,
            "completed": 0,
            "missingCount": 0,
            "status": "waiting_download",
            "notStarted": True,
        }
        session = {
            "activeExternalTask": task,
            "context": {"pendingImport": True},
            "lastBatchResult": {},
            "tasks": [
                {"url": url, "values": [url, "等待", "未开始", "等待下载"]}
                for url in urls
            ],
        }

        resumable, reason = interrupted_step12_resume_evidence(task, record, session)

        self.assertTrue(resumable, reason)
        self.assertIn("interrupted", reason)

    def test_runtime_failed_snapshot_with_full_rows_is_resumable_without_request(self):
        urls = ["https://example.test/1", "https://example.test/2"]
        task = {"taskId": "task-runtime", "expectedCount": 2, "urls": urls}
        record = {
            "expectedCount": 2,
            "completed": 0,
            "missingCount": 0,
            "status": "blocked_download_runtime",
            "runtimeFailure": {"errorCode": "DOWNLOAD_BRIDGE_RUNTIME_FAILED"},
        }
        session = {
            "activeExternalTask": task,
            "context": {"pendingImport": True},
            "lastBatchResult": {},
            "tasks": [
                {"url": url, "values": [url, "等待", "未开始", "等待下载"]}
                for url in urls
            ],
        }

        resumable, reason = interrupted_step12_resume_evidence(task, record, session)

        self.assertTrue(resumable, reason)
        self.assertIn("interrupted", reason)

    def test_terminal_or_failed_step12_is_not_misclassified_as_interrupted(self):
        urls = ["https://example.test/1", "https://example.test/2"]
        task = {"taskId": "task-1", "expectedCount": 2, "urls": urls}
        record = {"expectedCount": 2, "completed": 1, "missingCount": 1}
        base = {
            "activeExternalTask": task,
            "context": {"request": {"taskId": "task-1", "urls": urls}},
            "tasks": [
                {"url": urls[0], "values": [urls[0], "已完成", "已完成", "等待拼接配置"]},
                {"url": urls[1], "values": [urls[1], "处理中", "未开始", "等待"]},
            ],
        }
        terminal = dict(base, lastBatchResult={"total": 2, "succeeded": 1, "failed": 1})
        self.assertFalse(interrupted_step12_resume_evidence(task, record, terminal)[0])
        failed = dict(base, lastBatchResult={})
        failed["tasks"] = list(base["tasks"])
        failed["tasks"][1] = {"url": urls[1], "values": [urls[1], "失败", "失败", "处理失败"]}
        self.assertFalse(interrupted_step12_resume_evidence(task, record, failed)[0])

    def test_failed_terminal_session_can_rebuild_from_task_checkpoint(self):
        urls = ["https://example.test/1", "https://example.test/2", "https://example.test/3"]
        task = {"taskId": "task-rebuild", "expectedCount": 3, "urls": urls}
        record = {"expectedCount": 3, "completed": 1, "missingCount": 2}
        with tempfile.TemporaryDirectory() as temp:
            completed = Path(temp) / "completed.mp4"
            completed.write_bytes(b"x" * 2048)
            checkpoint = {
                "taskId": "task-rebuild",
                "items": {
                    urls[0]: {"status": "completed", "outputPath": str(completed)},
                    urls[1]: {"status": "failed", "error": "connection reset"},
                },
            }

            resumable, reason, plan = checkpoint_step12_resume_evidence(task, record, checkpoint)

        self.assertTrue(resumable, reason)
        self.assertEqual(plan["completedUrls"], [urls[0]])
        self.assertEqual(plan["pendingUrls"], urls[1:])
        self.assertEqual(plan["pendingCount"], 2)

    def test_checkpoint_recovery_rejects_foreign_urls(self):
        urls = ["https://example.test/1", "https://example.test/2"]
        task = {"taskId": "task-rebuild", "expectedCount": 2, "urls": urls}
        record = {"expectedCount": 2, "completed": 0, "missingCount": 2}
        checkpoint = {
            "taskId": "task-rebuild",
            "items": {"https://foreign.test/video": {"status": "failed"}},
        }

        resumable, reason, plan = checkpoint_step12_resume_evidence(task, record, checkpoint)

        self.assertFalse(resumable)
        self.assertIn("outside", reason)
        self.assertEqual(plan, {})

    def test_missing_completed_artifact_is_requeued_not_reused(self):
        urls = ["https://example.test/1", "https://example.test/2"]
        task = {"taskId": "task-rebuild", "expectedCount": 2, "urls": urls}
        record = {"expectedCount": 2, "completed": 1, "missingCount": 1}
        checkpoint = {
            "taskId": "task-rebuild",
            "items": {urls[0]: {"status": "completed", "outputPath": "Z:/missing.mp4"}},
        }

        resumable, reason, plan = checkpoint_step12_resume_evidence(task, record, checkpoint)

        self.assertTrue(resumable, reason)
        self.assertEqual(plan["completedCount"], 0)
        self.assertEqual(plan["pendingUrls"], urls)
        self.assertEqual(plan["invalidCompletedUrls"], [urls[0]])

    def test_continue_switches_from_durably_completed_stale_context(self):
        current = {
            "taskId": "old-task", "batchId": "old-batch", "productionDate": "7.31",
        }
        requested = {
            "requestedBy": "dashboard", "commandId": "continue-new",
            "taskId": "new-task", "batchId": "new-batch", "productionDate": "8.3",
        }
        candidate = {
            "taskId": "new-task", "batchId": "new-batch", "productionDate": "8.3",
            "date": "8.3", "status": "imported_waiting_start",
        }
        records = {
            "old-task": {"pipelineStatus": "completed"},
            "new-task": {"pipelineStatus": "blocked_voice", "expectedCount": 4, "completed": 4},
        }

        resolved, error = resolve_continue_command_task(
            requested, current, records, task_loader=lambda task_id: candidate if task_id == "new-task" else None
        )

        self.assertEqual("", error)
        self.assertEqual("new-task", resolved["canonicalTaskId"])
        self.assertEqual("step12_completed", resolved["status"])

    def test_continue_cannot_leave_an_unfinished_current_project(self):
        current = {
            "taskId": "old-task", "batchId": "old-batch", "productionDate": "7.31",
        }
        requested = {
            "taskId": "new-task", "batchId": "new-batch", "productionDate": "8.3",
        }
        records = {
            "old-task": {"pipelineStatus": "blocked_capcut"},
            "new-task": {"pipelineStatus": "blocked_voice"},
        }

        resolved, error = resolve_continue_command_task(
            requested, current, records, task_loader=lambda _task_id: self.fail("must not load successor")
        )

        self.assertIsNone(resolved)
        self.assertIn("当前项目尚未完成", error)

    def test_continue_rejects_requested_task_identity_mismatch(self):
        requested = {
            "taskId": "new-task", "batchId": "new-batch", "productionDate": "8.3",
        }
        candidate = {
            "taskId": "new-task", "batchId": "other-batch", "productionDate": "8.3",
        }
        records = {
            "old-task": {"pipelineStatus": "completed"},
            "new-task": {"pipelineStatus": "blocked_voice"},
        }

        resolved, error = resolve_continue_command_task(
            requested,
            {"taskId": "old-task"},
            records,
            task_loader=lambda _task_id: candidate,
        )

        self.assertIsNone(resolved)
        self.assertIn("身份不一致", error)

    def test_continue_command_is_bound_to_canonical_task_and_snapshot(self):
        task = {
            "taskId": "supplement-1", "canonicalTaskId": "task-1",
            "batchId": "batch-1", "productionDate": "7.21",
        }
        command = {
            "requestedBy": "dashboard", "commandId": "continue-1",
            "taskId": "task-1", "batchId": "batch-1", "productionDate": "7.21",
            "snapshotUpdatedAt": "2026-07-22T10:00:00+08:00",
        }
        self.assertTrue(continue_command_target_matches(command, task, command["snapshotUpdatedAt"])[0])
        stale = dict(command, taskId="task-2")
        self.assertFalse(continue_command_target_matches(stale, task, command["snapshotUpdatedAt"])[0])
        # Workbench startup publishes a fresh heartbeat before it reads the
        # command mailbox.  A timestamp-only change must not reject a command
        # whose immutable batch/task/date identity still matches.
        self.assertTrue(continue_command_target_matches(command, task, "2026-07-22T10:01:00+08:00")[0])

    def test_continue_command_accepts_same_canonical_replenishment_batch(self):
        task = {
            "taskId": "supplement-1", "canonicalTaskId": "task-1",
            "replenishForTaskId": "task-1", "batchId": "web-replenishment",
            "productionDate": "7.21",
        }
        command = {
            "requestedBy": "dashboard", "commandId": "continue-1",
            "taskId": "task-1", "batchId": "formal-batch", "productionDate": "7.21",
            "snapshotUpdatedAt": "2026-07-22T10:00:00+08:00",
        }
        self.assertTrue(continue_command_target_matches(command, task)[0])

    def test_completed_checkpoint_count_recovers_persisted_replenishment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "completed.mp4"
            output.write_bytes(b"x" * 2048)
            checkpoint_dir = root / "checkpoints"
            checkpoint_dir.mkdir()
            (checkpoint_dir / "supplement-1.json").write_text(json.dumps({
                "taskId": "supplement-1",
                "items": {"url": {"status": "completed", "outputPath": str(output)}},
            }), encoding="utf-8")
            with patch.object(workbench_module, "DOWNLOAD_CHECKPOINT_DIR", checkpoint_dir):
                self.assertEqual(1, completed_download_checkpoint_count({"taskId": "supplement-1"}))

    def test_continue_command_allows_only_one_capcut_recovery_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            mailbox = Path(temp) / "continue.json"
            mailbox.write_text(json.dumps({
                "commandId": "continue-once",
                "taskId": "task-1",
                "state": "recovering",
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            app.write_log = lambda _message: None
            with patch("workbench.CONTINUE_COMMAND_FILE", mailbox):
                self.assertFalse(app._continue_command_has_launched_capcut("task-1"))
                app._mark_continue_capcut_recovery_started("task-1")
                self.assertTrue(app._continue_command_has_launched_capcut("task-1"))
                first_marker = json.loads(mailbox.read_text(encoding="utf-8"))[
                    "capcutRecoveryStartedAt"
                ]
                app._mark_continue_capcut_recovery_started("task-1")
                second_marker = json.loads(mailbox.read_text(encoding="utf-8"))[
                    "capcutRecoveryStartedAt"
                ]
            self.assertEqual(first_marker, second_marker)

    def test_waiting_client_row_is_not_live_after_client_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / "capcut.status.json"
            status.write_text(json.dumps({
                "running": False,
                "tasks": [{"taskId": "capcut-1", "state": "Waiting"}],
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            row = {"status": "waiting_client", "statusFile": str(status)}

            self.assertFalse(app._capcut_row_has_live_client(row))

    def test_waiting_client_row_is_live_only_while_bound_status_runs(self):
        app = object.__new__(Workbench)
        row = {"status": "waiting_client", "statusFile": "bound-capcut.status.json"}

        # The durable status helper owns the stricter heartbeat/PID check.
        # This row-level test only verifies that a waiting row delegates to it.
        with patch.object(Workbench, "_capcut_status_file_is_running", return_value=True) as live:
            self.assertTrue(app._capcut_row_has_live_client(row))
            live.assert_called_once_with("bound-capcut.status.json")

    def test_terminal_capcut_status_closes_recovering_command_without_legacy_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mailbox = root / "continue.json"
            status = root / "capcut.status.json"
            mailbox.write_text(json.dumps({
                "commandId": "continue-terminal",
                "taskId": "task-1",
                "state": "recovering",
                "startedAt": "2026-08-04T10:00:00+08:00",
            }), encoding="utf-8")
            status.write_text(json.dumps({
                "remotePipelineTaskId": "task-1",
                "manifestId": "manifest-1",
                "attemptId": "attempt-1",
                "running": False,
                "failed": 1,
                "updatedAt": "2026-08-04T10:01:00+08:00",
                "tasks": [{"taskId": "capcut-1", "state": "Failed"}],
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            app.write_log = lambda _message: None
            rows = [{"statusFile": str(status)}]
            with patch("workbench.CONTINUE_COMMAND_FILE", mailbox), patch.object(
                Workbench, "_capcut_client_pid_is_alive", return_value=False
            ):
                self.assertTrue(app._reconcile_continue_command_capcut_terminal(
                    "task-1", rows
                ))
            closed = json.loads(mailbox.read_text(encoding="utf-8"))
            self.assertEqual("blocked", closed["state"])
            self.assertEqual("capcut_recoverable_failed", closed["phase"])
            self.assertTrue(closed.get("finishedAt"))
            self.assertTrue(closed.get("terminalObservedAt"))

    def test_old_or_mismatched_capcut_terminal_cannot_close_new_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mailbox = root / "continue.json"
            status = root / "capcut.status.json"
            mailbox.write_text(json.dumps({
                "commandId": "continue-new",
                "taskId": "task-1",
                "state": "recovering",
                "startedAt": "2026-08-04T10:02:00+08:00",
                "boundManifestId": "manifest-new",
                "boundAttemptId": "attempt-new",
                "boundStatusFile": str(status),
            }), encoding="utf-8")
            status.write_text(json.dumps({
                "remotePipelineTaskId": "task-1",
                "manifestId": "manifest-old",
                "attemptId": "attempt-old",
                "running": False,
                "failed": 1,
                "updatedAt": "2026-08-04T10:01:00+08:00",
                "tasks": [{"taskId": "capcut-1", "state": "Failed"}],
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            app.write_log = lambda _message: None
            with patch("workbench.CONTINUE_COMMAND_FILE", mailbox):
                self.assertFalse(app._reconcile_continue_command_capcut_terminal(
                    "task-1", [{"statusFile": str(status)}]
                ))
            unchanged = json.loads(mailbox.read_text(encoding="utf-8"))
            self.assertEqual("recovering", unchanged["state"])

    def test_successor_waits_for_full_pipeline_not_only_download(self):
        successor = {"batchId": "queue-1", "batchSequence": 2}
        step1_only = {
            "one": {
                "batchId": "queue-1",
                "batchSequence": 1,
                "status": "completed",
                "pipelineStatus": "step1_completed",
            }
        }
        self.assertFalse(Workbench._previous_remote_item_finished(None, successor, step1_only))

        final = {
            "one": {
                "batchId": "queue-1",
                "batchSequence": 1,
                "status": "completed",
                "pipelineStatus": "completed",
            }
        }
        self.assertTrue(Workbench._previous_remote_item_finished(None, successor, final))

    def test_successor_starts_while_predecessor_is_parked_on_missing_links(self):
        successor = {"batchId": "queue-1", "batchSequence": 2}
        parked = {
            "one": {
                "batchId": "queue-1",
                "batchSequence": 1,
                "status": "blocked_missing_links",
                "pipelineStatus": "blocked_missing_links",
            }
        }
        self.assertTrue(Workbench._previous_remote_item_finished(None, successor, parked))

        mid_pipeline = {"one": dict(parked["one"], status="completed", pipelineStatus="voice_running")}
        self.assertFalse(Workbench._previous_remote_item_finished(None, successor, mid_pipeline))

    def _idle_gate_shell(self, root, active_task):
        app = object.__new__(Workbench)
        app.download_process = None
        app.voice_batch_process = None
        app.stitcher_controller = None
        app.capcut_process = None
        app.active_external_task = active_task
        app.vars = {"capcut_client": SimpleNamespace(get=lambda: str(root / "client" / "CapCutBatchClient.exe"))}
        app._read_local_json_object = lambda path: {}
        return app

    def _idle_gate(self, app, task, status_items):
        no_processes = SimpleNamespace(
            process_iter=lambda _attrs: [], NoSuchProcess=Exception, AccessDenied=Exception
        )
        with patch.dict("sys.modules", {"psutil": no_processes}):
            return app._examplebrand_global_idle(task, status_items)

    def test_parked_active_project_does_not_hold_the_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parked = {"taskId": "box-05", "batchId": "box", "batchSequence": 5, "startMode": "auto_start_sequential"}
            successor = {"taskId": "box-06", "batchId": "box", "batchSequence": 6, "startMode": "auto_start_sequential"}
            status_items = {
                "box-04": {"batchId": "box", "status": "completed", "pipelineStatus": "completed"},
                "box-05": {"batchId": "box", "status": "blocked_missing_links", "pipelineStatus": "blocked_missing_links"},
                "old-07": {"batchId": "older-box", "status": "blocked_capcut", "pipelineStatus": "blocked_capcut"},
            }
            app = self._idle_gate_shell(root, parked)
            self.assertTrue(self._idle_gate(app, successor, status_items))

            busy = dict(status_items, **{"box-04": dict(status_items["box-04"], pipelineStatus="capcut_running")})
            self.assertFalse(self._idle_gate(app, successor, busy),
                             "a same-box project mid Step 3-5 must keep the queue waiting")

    def test_resumed_parent_blocks_the_queue_while_replenishment_child_is_active(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            replenishment_child = {"taskId": "repl-05", "batchId": "repl-box", "batchSequence": 1}
            next_item = {"taskId": "box-08", "batchId": "box", "batchSequence": 8, "startMode": "auto_start_sequential"}
            status_items = {
                "repl-05": {"batchId": "repl-box", "status": "completed", "pipelineStatus": "completed"},
                "box-05": {"batchId": "box", "status": "completed", "pipelineStatus": "voice_running"},
                "box-07": {"batchId": "box", "status": "completed", "pipelineStatus": "completed"},
            }
            app = self._idle_gate_shell(root, replenishment_child)
            self.assertFalse(self._idle_gate(app, next_item, status_items),
                             "the resumed parent still owns Fish/CapCut")

    def test_legacy_completed_record_can_recover_existing_queue(self):
        successor = {"batchId": "queue-legacy", "batchSequence": 2}
        legacy = {
            "one": {
                "batchId": "queue-legacy",
                "batchSequence": 1,
                "status": "completed",
            }
        }
        self.assertTrue(Workbench._previous_remote_item_finished(None, successor, legacy))

    def test_remote_pipeline_state_is_persisted(self):
        app = object.__new__(Workbench)
        registry = {"items": {"task-1": {"taskId": "task-1", "status": "completed"}}}
        with patch("workbench.load_project_download_statuses", return_value=registry), patch(
            "workbench.save_project_download_status"
        ) as save:
            self.assertTrue(app._set_remote_pipeline_status("task-1", "completed", "CapCut done"))
        saved = save.call_args.args[0]
        self.assertEqual(saved["pipelineStatus"], "completed")
        self.assertEqual(saved["pipelineStateText"], "CapCut done")

    def test_completed_capcut_manifest_marks_project_complete_and_advances_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            final_output = Path(temp) / "final.mp4"
            final_output.write_bytes(b"valid-final")
            owner = "inbox-project-1"
            app = object.__new__(Workbench)
            app.capcut_store = SimpleNamespace(
                dates=lambda: ["7.28"],
                load_date=lambda _date: {
                    "tasks": [{
                        "taskId": "capcut-1",
                        "remotePipelineTaskId": owner,
                        "expectedTaskCount": 1,
                        "status": "completed",
                        "finalOutputPath": str(final_output),
                    }],
                },
            )
            pipeline_updates = []
            scheduled = []
            app._set_remote_pipeline_status = (
                lambda task_id, status, message: pipeline_updates.append((task_id, status, message)) or True
            )
            app.write_log = lambda _message: None
            app._advance_remote_queue = lambda: None
            app.after = lambda delay, callback: scheduled.append((delay, callback))

            with patch("workbench.load_baidu_json", return_value={"tasks": {}}):
                app._sync_remote_pipeline_from_capcut()

            self.assertEqual(owner, pipeline_updates[0][0])
            self.assertEqual("completed", pipeline_updates[0][1])
            self.assertEqual(350, scheduled[0][0])
            self.assertIs(scheduled[0][1], app._advance_remote_queue)

    def test_failed_voice_handoff_releases_only_its_launch_guard(self):
        app = object.__new__(Workbench)
        app.voice_auto_launch_task_ids = {"task-1", "task-2"}
        app.voice_batch_process = None

        app._release_voice_launch_guard("task-1")

        self.assertNotIn("task-1", app.voice_auto_launch_task_ids)
        self.assertIn("task-2", app.voice_auto_launch_task_ids)

    def test_terminal_voice_handoff_preserves_specific_task_error_code(self):
        app = object.__new__(Workbench)
        app.active_external_task = {
            "taskId": "task-1", "batchId": "batch-1", "date": "8.31",
            "project": "ProjectD", "material": "碳岩板",
        }
        command = {
            "taskId": "task-1", "batchId": "fish-1", "state": "recovering",
            "requestedAt": "2026-08-31T09:00:00+08:00",
        }
        payload = {
            "batchId": "fish-1", "status": "failed", "total": 8,
            "completed": 0, "failed": 1,
            "failedItems": [{
                "errorCode": "VOICE_SAVED_TAB_TIMEOUT",
                "error": "完整音色库标签尚未加载完成",
            }],
            "updatedAt": "2026-08-31T09:01:00+08:00",
        }
        updates = []
        app._read_local_json_object = lambda _path: command
        app._voice_payload_matches_task = lambda _payload, _task_id: True
        app._expected_remote_voice_task_count = lambda _task: 8
        app._retire_voice_launch_record = lambda *_args: None
        app._set_remote_pipeline_status = lambda *args, **kwargs: updates.append((args, kwargs))
        app._finish_continue_command = lambda *_args, **_kwargs: None
        app.write_log = lambda _message: None
        app.status_var = SimpleNamespace(set=lambda _value: None)

        self.assertTrue(app._reconcile_terminal_voice_command(payload))
        self.assertEqual("VOICE_SAVED_TAB_TIMEOUT", updates[0][1]["error_code"])

    def test_old_cancelled_voice_handoff_cannot_close_new_continue_command(self):
        cancelled = {
            "batchId": "old-fish-batch",
            "status": "cancelled",
            "cancelledAt": "2026-07-23T23:14:00+08:00",
        }
        newer_command = {
            "commandId": "continue-new",
            "requestedAt": "2026-07-23T23:16:04+08:00",
        }
        original_command = {
            "commandId": "continue-old",
            "requestedAt": "2026-07-23T23:10:00+08:00",
        }
        self.assertTrue(voice_terminal_predates_continue_command(cancelled, newer_command))
        self.assertFalse(voice_terminal_predates_continue_command(cancelled, original_command))

    def test_voice_handoff_matches_canonical_task_for_new_and_legacy_payloads(self):
        app = object.__new__(Workbench)
        self.assertTrue(app._voice_payload_matches_task(
            {"canonicalTaskIds": ["task-1"], "failedItems": []}, "task-1"
        ))
        self.assertTrue(app._voice_payload_matches_task(
            {"failedItems": [{"taskId": "remote-task-1-01"}]}, "task-1"
        ))
        self.assertFalse(app._voice_payload_matches_task(
            {"canonicalTaskIds": ["task-2"]}, "task-1"
        ))

    def test_same_task_live_voice_process_is_not_reported_as_launch_failure(self):
        app = object.__new__(Workbench)
        app.voice_batch_process = SimpleNamespace(pid=1234, poll=lambda: None)
        app.voice_launch_identity = {
            "runGroupId": "run-1", "batchId": "batch-1", "canonicalTaskId": "task-1",
            "productionDate": "8.31", "audioBatch": "8.31",
        }
        app._fish_bridge_listener_pid = lambda _port=5178: 0
        persisted = []
        app._persist_voice_launch_state = lambda *args, **kwargs: persisted.append((args, kwargs))
        app.write_log = lambda _message: None
        task = {
            "taskId": "task-1", "runGroupId": "run-1", "batchId": "batch-1", "date": "8.31",
            "pipelineProfile": {"3_voice": {"audioBatch": "8.31"}},
        }

        result = app._voice_launch_result_for_existing_process(task)

        self.assertEqual(workbench_module.VOICE_LAUNCH_ALREADY_STARTING, result)
        self.assertEqual("waiting_for_bridge", persisted[0][0][0])

    def test_other_task_live_voice_process_is_reported_as_busy_not_failed(self):
        app = object.__new__(Workbench)
        app.voice_batch_process = SimpleNamespace(pid=1234, poll=lambda: None)
        app.voice_launch_identity = {"canonicalTaskId": "task-old", "batchId": "batch-old"}
        app._persist_voice_launch_state = lambda *args, **kwargs: None
        app.write_log = lambda _message: None

        result = app._voice_launch_result_for_existing_process({"taskId": "task-new", "batchId": "batch-new"})

        self.assertEqual(workbench_module.VOICE_LAUNCH_BLOCKED_OTHER_TASK, result)

    def test_voice_ops_status_does_not_reuse_historical_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            (data / "last_batch_handoff.json").write_text(json.dumps({
                "status": "completed", "batchId": "fish-old", "canonicalTaskIds": ["task-old"],
                "total": 8, "completed": 8, "failed": 0,
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            app.active_external_task = {
                "taskId": "task-current", "runGroupId": "run-current", "batchId": "batch-current",
                "date": "8.31", "expectedCount": 2,
                "languagePlan": [
                    {"code": "en", "name": "英语", "audioCount": 1, "outputCount": 1},
                    {"code": "de", "name": "德语", "audioCount": 1, "outputCount": 1},
                ],
                "pipelineProfile": {"3_voice": {"audioBatch": "8.31"}},
            }
            app._voice_client_path = lambda: root / "BatchVoiceClient.exe"

            status = app._voice_ops_status()

            self.assertEqual("idle", status["state"])
            self.assertEqual(0, status["completed"])
            self.assertEqual("task-current", status["canonicalTaskId"])
            self.assertIn("8.31", status["stateText"])

    def test_safe_retry_replaces_stale_voice_launch_guard_without_duplicate_process(self):
        class Status:
            def set(self, _value):
                pass

            def get(self):
                return ""

        app = object.__new__(Workbench)
        app.active_external_task = {
            "taskId": "task-1",
            "pipelineProfile": {"3_voice": {"requiresNewGeneration": True, "audioBatch": "7.22"}},
        }
        app.last_batch_result = {"failed": 0}
        app.success_var = SimpleNamespace(get=lambda: "1")
        app.status_var = Status()
        app.voice_auto_launch_task_ids = {"task-1"}
        app.voice_batch_process = None
        app.voice_launch_records = {}
        app._expected_download_count = lambda: 1
        app._set_remote_pipeline_status = lambda *_args: True
        app.write_log = lambda _message: None
        launches = []
        app.after = lambda *_args: self.fail("unattended voice launch must not depend on a deferred Tk callback")
        app.launch_voice_client = lambda unattended=False: launches.append(unattended) or True

        self.assertTrue(app._auto_launch_voice_after_download(force_retry=True))
        self.assertEqual([True], launches)
        self.assertIn("task-1", app.voice_auto_launch_task_ids)
        self.assertIn("task-1", app.voice_launch_records)

    def test_voice_directory_sync_uses_current_material_helper(self):
        """A renamed preset helper must not prevent unattended Fish startup."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "projects.json"
            audio_root = root / "audio"
            config.write_text(json.dumps({
                "active_project": "demo",
                "projects": {"demo": {"audio_folder": str(audio_root)}},
            }, ensure_ascii=False), encoding="utf-8")
            app = object.__new__(Workbench)
            app.vars = {
                "stitcher_projects": SimpleNamespace(get=lambda: str(config)),
                "audio_dir": SimpleNamespace(set=lambda _value: None),
            }
            app.active_external_task = None
            app._voice_client_path = lambda: root / "voice-client.exe"
            app._ensure_material_stitch_projects = lambda payload, _path: payload
            app._voice_output_project_identity = lambda project, _data: (project, "")
            app.write_log = lambda _message: None

            active, rules = app._sync_voice_output_dirs()

            self.assertEqual(1, rules)
            self.assertIsNotNone(active)

    def test_stale_terminal_capcut_record_does_not_block_next_task(self):
        class Process:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        app = object.__new__(Workbench)
        stale = Process()
        app.capcut_launch_records = {
            "task-4": {"manifestId": "task-4.json", "pid": 42, "process": stale}
        }
        app.capcut_auto_launch_task_ids = {"task-4"}
        app.capcut_process = stale
        app.capcut_manifest = "task-4.json"
        app._capcut_manifest_runtime = lambda _path: ("task-4", "task-4.status.json")
        app._capcut_status_file_is_running = lambda _path: False
        app._pid_has_visible_window = lambda _pid: False
        app.write_log = lambda _message: None

        self.assertFalse(app._current_capcut_client_is_running("task-5"))
        self.assertNotIn("task-4", app.capcut_launch_records)

    def test_next_capcut_launch_waits_for_terminal_client_exit(self):
        class ExitedProcess:
            def poll(self):
                return 0

        app = object.__new__(Workbench)
        previous = ExitedProcess()
        app.capcut_launch_records = {
            "task-7": {"manifestId": "task-7.json", "pid": 71, "process": previous}
        }
        app.capcut_auto_launch_task_ids = {"task-7"}
        app.capcut_process = previous
        app.capcut_manifest = "task-7.json"
        app._capcut_manifest_runtime = lambda _path: ("task-7", "task-7.status.json")
        app._capcut_status_file_is_running = lambda _path: False
        app.write_log = lambda _message: None

        allowed, detail = app._wait_for_previous_capcut_client_exit("task-8", 0.0)
        self.assertTrue(allowed, detail)
        self.assertNotIn("task-7", app.capcut_launch_records)

    def test_next_capcut_launch_blocks_when_terminal_client_does_not_exit(self):
        class HangingProcess:
            def poll(self):
                return None

        app = object.__new__(Workbench)
        app.capcut_launch_records = {
            "task-7": {"manifestId": "task-7.json", "pid": 71, "process": HangingProcess()}
        }
        app.capcut_auto_launch_task_ids = {"task-7"}
        app._capcut_manifest_runtime = lambda _path: ("task-7", "task-7.status.json")
        app._capcut_status_file_is_running = lambda _path: False
        app.write_log = lambda _message: None

        allowed, detail = app._wait_for_previous_capcut_client_exit("task-8", 0.0)
        self.assertFalse(allowed)
        self.assertIn("capcut_client_exit_timeout", detail)

    def test_material_specific_stitcher_profiles_are_matchable(self):
        app = object.__new__(Workbench)
        project_file = Path(__file__).resolve().parent.parent / "video-stitcher" / ".board_video_projects.json"
        if not project_file.is_file():
            self.skipTest("machine-local stitcher project config is not present")
        app.vars ={"stitcher_projects": SimpleNamespace(get=lambda: str(project_file))}
        app.write_log = lambda _message: None
        cases = [
            ("ProjectB", "净白板", "ProjectB净白板"),
            ("ProjectA", "碳岩板", "ProjectA碳岩板"),
            ("ProjectC", "金属板", "ProjectC金属板"),
            ("SPC地板", "塑木地板", "SPC地板塑木地板"),
            ("SPC墙板", "晶岩墙板", "SPC墙板晶岩墙板"),
        ]
        for project, material, expected in cases:
            self.assertEqual(app._match_stitch_project(project, material), expected)

    def test_matching_never_falls_back_to_another_material_or_project(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "projects.json"
            path.write_text(json.dumps({"projects": {
                "默认项目": {"use_ai": True, "cover_material": "white_panel"},
                "ProjectB碳岩板": {"use_ai": True, "cover_material": "carbon_crystal"},
            }}, ensure_ascii=False), encoding="utf-8")
            app = object.__new__(Workbench)
            app.vars = {"stitcher_projects": SimpleNamespace(get=lambda: str(path))}
            app.write_log = lambda _message: None

            self.assertEqual(app._match_stitch_project("ProjectB", "碳岩板"), "ProjectB碳岩板")
            self.assertIsNone(app._match_stitch_project("ProjectB", "不存在材料"))
            self.assertIsNone(app._match_stitch_project("不存在项目", "碳岩板"))

    def test_completed_task_scoped_voice_handoff_can_be_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            audio = temp_path / "voice.mp3"
            audio.write_bytes(b"voice")
            handoff = temp_path / "handoff.json"
            handoff.write_text(json.dumps({
                "status": "completed", "canonicalTaskIds": ["task-1"],
                "completedItems": [{
                    "canonicalTaskId": "task-1", "project": "ProjectB", "material": "PU石",
                    "audioPath": str(audio),
                }],
            }, ensure_ascii=False), encoding="utf-8-sig")
            app = object.__new__(Workbench)
            app._voice_handoff_file = lambda: handoff
            app._expected_remote_voice_task_count = lambda _task: 1
            task = {"taskId": "task-1", "canonicalTaskId": "task-1", "project": "ProjectB", "material": "PU石"}

            recovered = app._completed_voice_handoff_for_task(task)
            self.assertIsNotNone(recovered)
            self.assertEqual(1, recovered["completed"])
            self.assertEqual([], recovered["failedItems"])

    def test_stitch_manifest_accepts_processed_and_verified_no_subtitle_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "media"
            media.mkdir()
            processed = media / "processed_ai_sttn.mp4"
            verified = media / "verified_original.mp4"
            unverified = media / "unverified_original.mp4"
            for path in (processed, verified, unverified):
                path.write_bytes(b"x" * 2048)
            checkpoint_root = root / "checkpoints"
            checkpoint_root.mkdir()
            (checkpoint_root / "task-1.json").write_text(json.dumps({
                "items": {
                    "processed": {"status": "completed", "outputPath": str(processed)},
                    "verified": {
                        "status": "completed", "outputPath": str(verified),
                        "subtitleOutcome": "verified_no_subtitle",
                    },
                    "unverified": {"status": "completed", "outputPath": str(unverified)},
                },
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            task = {"taskId": "task-1", "canonicalTaskId": "task-1", "expectedCount": 2}
            with patch("workbench.DOWNLOAD_CHECKPOINT_DIR", checkpoint_root):
                entries, failures, source_dir = app._task_stitch_source_manifest(task)

            self.assertEqual(2, len(entries))
            self.assertEqual(media.resolve(), source_dir.resolve())
            self.assertTrue(any("unverified_original" in item for item in failures))

    def test_stitch_manifest_accepts_task_bound_verified_warning_without_sttn(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "media"
            media.mkdir()
            output = media / "no-subtitle_leadtrim2s.mp4"
            output.write_bytes(b"x" * 2048)
            url = "https://www.douyin.com/video/123"
            verification = {
                "verified": True,
                "verificationStatus": "warning",
                "residualBands": [],
            }
            Path(f"{output}.subtitle-verification.json").write_text(json.dumps({
                "taskId": "task-1",
                "url": url,
                "outputPath": str(output),
                "subtitleTreatment": "minor_residual",
                "verification": verification,
            }), encoding="utf-8")
            checkpoint_root = root / "checkpoints"
            checkpoint_root.mkdir()
            (checkpoint_root / "task-1.json").write_text(json.dumps({
                "items": {url: {
                    "status": "completed",
                    "url": url,
                    "outputPath": str(output),
                    "subtitleOutcome": "minor_residual",
                    "verification": verification,
                }},
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            task = {"taskId": "task-1", "canonicalTaskId": "task-1", "expectedCount": 1}

            with patch("workbench.DOWNLOAD_CHECKPOINT_DIR", checkpoint_root):
                entries, failures, source_dir = app._task_stitch_source_manifest(task)

            self.assertEqual(1, len(entries))
            self.assertEqual("warning", entries[0]["subtitleVerificationStatus"])
            self.assertEqual([], failures)
            self.assertEqual(media.resolve(), source_dir.resolve())

    def test_stitch_manifest_rejects_unbound_warning_without_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "unverified_leadtrim2s.mp4"
            output.write_bytes(b"x" * 2048)
            checkpoint_root = root / "checkpoints"
            checkpoint_root.mkdir()
            (checkpoint_root / "task-1.json").write_text(json.dumps({
                "items": {"video": {
                    "status": "completed",
                    "outputPath": str(output),
                    "subtitleOutcome": "minor_residual",
                    "verification": {
                        "verified": True,
                        "verificationStatus": "warning",
                        "residualBands": [],
                    },
                }},
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            task = {"taskId": "task-1", "canonicalTaskId": "task-1", "expectedCount": 1}

            with patch("workbench.DOWNLOAD_CHECKPOINT_DIR", checkpoint_root):
                entries, failures, _source_dir = app._task_stitch_source_manifest(task)

            self.assertEqual([], entries)
            self.assertTrue(any("无法读取任务绑定的去字幕复检证据" in item for item in failures))

    def test_stitch_manifest_rejects_warning_sidecar_for_another_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "wrong-task_leadtrim2s.mp4"
            output.write_bytes(b"x" * 2048)
            verification = {
                "verified": True,
                "verificationStatus": "warning",
                "residualBands": [],
            }
            Path(f"{output}.subtitle-verification.json").write_text(json.dumps({
                "taskId": "another-task",
                "outputPath": str(output),
                "subtitleTreatment": "minor_residual",
                "verification": verification,
            }), encoding="utf-8")
            checkpoint_root = root / "checkpoints"
            checkpoint_root.mkdir()
            (checkpoint_root / "task-1.json").write_text(json.dumps({
                "items": {"video": {
                    "status": "completed",
                    "outputPath": str(output),
                    "subtitleOutcome": "minor_residual",
                    "verification": verification,
                }},
            }), encoding="utf-8")
            app = object.__new__(Workbench)
            task = {"taskId": "task-1", "canonicalTaskId": "task-1", "expectedCount": 1}

            with patch("workbench.DOWNLOAD_CHECKPOINT_DIR", checkpoint_root):
                entries, failures, _source_dir = app._task_stitch_source_manifest(task)

            self.assertEqual([], entries)
            self.assertTrue(any("当前任务身份不一致" in item for item in failures))

    def test_stitch_manifest_merges_every_replenishment_checkpoint_for_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "media"
            media.mkdir()
            first = media / "first_ai_sttn.mp4"
            second = media / "second_ai_sttn.mp4"
            first.write_bytes(b"a" * 2048)
            second.write_bytes(b"b" * 2048)
            checkpoint_root = root / "checkpoints"
            checkpoint_root.mkdir()
            imported_root = root / "imported"
            imported_root.mkdir()
            parent = "parent-task"
            children = ("child-one", "child-two")
            for child, output in zip(children, (first, second)):
                (checkpoint_root / f"{child}.json").write_text(json.dumps({
                    "taskId": child,
                    "items": {child: {
                        "status": "completed",
                        "outputPath": str(output),
                        "subtitleOutcome": "processed",
                    }},
                }), encoding="utf-8")
                (imported_root / f"{child}.json").write_text(json.dumps({
                    "taskId": child,
                    "status": "imported_waiting_start" if child == children[0] else "merged_into_parent",
                    "replenishForTaskId": parent,
                    "canonicalTaskId": parent,
                    "productionDate": "7.28",
                    "project": "ProjectB",
                    "material": "净白板",
                }, ensure_ascii=False), encoding="utf-8")
            task = {
                "taskId": parent,
                "canonicalTaskId": parent,
                "productionDate": "7.28",
                "project": "ProjectB",
                "material": "净白板",
                "expectedCount": 2,
                "replenishmentClosedByTaskId": children[1],
            }
            app = object.__new__(Workbench)
            with patch("workbench.DOWNLOAD_CHECKPOINT_DIR", checkpoint_root), patch(
                "workbench.TASK_IMPORTED_DIR", imported_root
            ):
                entries, failures, source_dir = app._task_stitch_source_manifest(task)

            self.assertEqual(2, len(entries))
            self.assertEqual({*children}, {item["checkpointId"] for item in entries})
            self.assertFalse(any("任务素材清单为" in item for item in failures))
            self.assertEqual(media.resolve(), source_dir.resolve())

    def test_stitch_manifest_rejects_duplicate_replenishment_source_as_one_unique_video(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root / "media"
            media.mkdir()
            shared = media / "shared_ai_sttn.mp4"
            shared.write_bytes(b"x" * 2048)
            checkpoint_root = root / "checkpoints"
            checkpoint_root.mkdir()
            imported_root = root / "imported"
            imported_root.mkdir()
            parent, child = "parent-task", "child-task"
            for task_id in (parent, child):
                (checkpoint_root / f"{task_id}.json").write_text(json.dumps({
                    "items": {task_id: {
                        "status": "completed",
                        "outputPath": str(shared),
                        "subtitleOutcome": "processed",
                    }},
                }), encoding="utf-8")
            (imported_root / f"{child}.json").write_text(json.dumps({
                "taskId": child,
                "status": "merged_into_parent",
                "replenishForTaskId": parent,
                "canonicalTaskId": parent,
                "productionDate": "8.2",
                "project": "ProjectD",
                "material": "木饰面",
            }, ensure_ascii=False), encoding="utf-8")
            task = {
                "taskId": parent,
                "canonicalTaskId": parent,
                "productionDate": "8.2",
                "project": "ProjectD",
                "material": "木饰面",
                "expectedCount": 2,
                "replenishmentClosedByTaskId": child,
            }
            app = object.__new__(Workbench)
            with patch("workbench.DOWNLOAD_CHECKPOINT_DIR", checkpoint_root), patch(
                "workbench.TASK_IMPORTED_DIR", imported_root
            ):
                entries, failures, source_dir = app._task_stitch_source_manifest(task)

            self.assertEqual(1, len(entries))
            self.assertEqual(media.resolve(), source_dir.resolve())
            self.assertTrue(any("重复视频" in item for item in failures))
            self.assertTrue(any("1/2" in item for item in failures))

    def test_reuse_audio_contract_uses_only_declared_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            declared = root / "8.5"
            newer = root / "8.6"
            declared.mkdir()
            newer.mkdir()
            language_plan = [
                {"code": "en", "name": "英语", "audioCount": 1, "outputCount": 2},
                {"code": "de", "name": "德语", "audioCount": 1, "outputCount": 2},
            ]
            for language in ("英语", "德语"):
                (declared / f"ProjectA_碳岩板_{language}_remote-inbox-old.mp3").write_bytes(b"audio")
            task = {
                "project": "ProjectA", "material": "碳岩板", "languagePlan": language_plan,
                "expectedCount": 4,
                "pipelineProfile": {"3_voice": {"audioBatch": "8.5", "voicePlan": "reuse_existing"}},
            }
            with patch("workbench.resolve_task_audio_directory", return_value=declared):
                resolved, error = Workbench._validate_reuse_audio_contract(task)

            self.assertEqual(declared, resolved)
            self.assertEqual("", error)

    def test_reuse_audio_contract_never_falls_back_when_declared_batch_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "7.17"
            newer = root / "8.5"
            newer.mkdir()
            (newer / "ProjectA_碳岩板_英语.mp3").write_bytes(b"audio")
            task = {
                "project": "ProjectA", "material": "碳岩板",
                "expectedCount": 1,
                "languagePlan": [{"code": "en", "name": "英语", "audioCount": 1, "outputCount": 1}],
                "pipelineProfile": {"3_voice": {"audioBatch": "7.17", "voicePlan": "reuse_existing"}},
            }
            with patch("workbench.resolve_task_audio_directory", return_value=missing):
                resolved, error = Workbench._validate_reuse_audio_contract(task)

            self.assertIsNone(resolved)
            self.assertIn("不存在", error)


if __name__ == "__main__":
    unittest.main()
