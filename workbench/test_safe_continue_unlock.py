import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import workbench
from workbench import Workbench


TASK_ID = "formal-batch-1-01"


class _ImmediateFuture:
    def __init__(self, value=None):
        self._value = value

    def done(self):
        return True

    def result(self):
        return self._value


class _ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        return _ImmediateFuture(fn(*args, **kwargs))


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FinishRecoveryRequestTests(unittest.TestCase):
    """A Safe Continue that starts nothing must end, never lock its step."""

    def _shell(self, root, impl_result, impl_message):
        app = object.__new__(Workbench)
        app.active_external_task = {"taskId": TASK_ID}
        app.status_var = _Var()
        app.write_log = lambda _text: None
        app.after = lambda _delay, callback=None: None
        app._recovery_executor = _ImmediateExecutor()
        app._auto_recovery_busy = True
        app._manual_recovery_future = _ImmediateFuture(
            {"decision": "execute_new", "commandId": "continue-1", "taskId": TASK_ID}
        )
        recorded = []
        app.recovery_control = SimpleNamespace(
            record_execution=lambda command_id, receipt, state: recorded.append((command_id, receipt, state))
        )
        app._recovery_execution_receipt = lambda _task_id: {"taskId": TASK_ID}

        def impl(interactive=True):
            app.status_var.set(impl_message)
            return impl_result

        app._continue_current_project_impl = impl
        return app, recorded

    def _mailbox(self, root, **fields):
        mailbox = root / "continue.json"
        payload = {"taskId": TASK_ID, "commandId": "continue-1", "state": "accepted",
                   "phase": "executing_task_recovery", **fields}
        mailbox.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return mailbox

    def test_nothing_started_ends_command_with_the_real_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mailbox = self._mailbox(root)
            reason = "当前 CapCut 客户端仍在运行，无需继续；系统会同步本轮状态。"
            app, recorded = self._shell(root, False, reason)
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", mailbox):
                app._finish_recovery_request(False, {"taskId": TASK_ID})
            self.assertEqual([("continue-1", "blocked")], [(c, s) for c, _r, s in recorded])
            self.assertEqual(reason, recorded[0][1]["blockedReason"])
            saved = json.loads(mailbox.read_text(encoding="utf-8"))
            self.assertEqual("blocked", saved["state"])
            self.assertEqual("recovery_nothing_started", saved["phase"])
            self.assertEqual(reason, saved["message"])
            self.assertTrue(saved["finishedAt"])

    def test_started_recovery_stays_running_and_keeps_the_mailbox(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mailbox = self._mailbox(root)
            app, recorded = self._shell(root, True, "已启动")
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", mailbox):
                app._finish_recovery_request(False, {"taskId": TASK_ID})
            self.assertEqual([("continue-1", "running")], [(c, s) for c, _r, s in recorded])
            self.assertEqual("accepted", json.loads(mailbox.read_text(encoding="utf-8"))["state"])

    def test_mailbox_bound_to_a_capcut_attempt_is_left_to_terminal_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mailbox = self._mailbox(root, boundManifestId="capcut-1")
            app, recorded = self._shell(root, False, "原因")
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", mailbox):
                app._finish_recovery_request(False, {"taskId": TASK_ID})
            self.assertEqual("blocked", recorded[0][2])
            self.assertEqual("accepted", json.loads(mailbox.read_text(encoding="utf-8"))["state"])


class CapCutRunStartedForCommandTests(unittest.TestCase):
    """An unbound Step 5 command is not 'CapCut never started' without proof."""

    STARTED = datetime(2026, 10, 7, 11, 34, 16).astimezone()

    def _shell(self, root):
        app = object.__new__(Workbench)
        app.capcut_store = SimpleNamespace(root=root / "交接记录")
        return app

    def _attempt(self, root, name, owner=TASK_ID, created=None, status=None):
        folder = root / "handoff" / "capcut"
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / f"10.6_启动批次_{name}.json"
        status_file = manifest.with_suffix(".status.json")
        manifest.write_text(json.dumps({
            "remotePipelineTaskId": owner, "statusFile": str(status_file),
        }, ensure_ascii=False), encoding="utf-8")
        created = created or self.STARTED - timedelta(seconds=4)
        os.utime(manifest, (created.timestamp(), created.timestamp()))
        if status is not None:
            status_file.write_text(json.dumps({"remotePipelineTaskId": owner, **status}), encoding="utf-8")
        return manifest

    def _request(self):
        return {"taskId": TASK_ID, "startedAt": self.STARTED.isoformat(timespec="seconds")}

    def test_running_attempt_created_just_before_the_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._attempt(root, "a", status={"running": True, "heartbeatAt": datetime.now().astimezone().isoformat()})
            # The live-client check (heartbeat + CapCutBatchClient pid) is
            # covered by its own tests; here it reports a verified client.
            with patch.object(Workbench, "_capcut_status_file_is_running", return_value=True):
                self.assertTrue(self._shell(root)._capcut_run_started_for_command(TASK_ID, self._request()))

    def test_ghost_running_flag_without_a_live_client_is_not_a_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._attempt(root, "a", status={"running": True, "heartbeatAt": self.STARTED.isoformat()})
            with patch.object(Workbench, "_capcut_status_file_is_running", return_value=False):
                self.assertFalse(self._shell(root)._capcut_run_started_for_command(TASK_ID, self._request()))

    def test_attempt_that_ended_after_the_command_started(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ended = (self.STARTED + timedelta(seconds=72)).isoformat()
            self._attempt(root, "a", status={"running": False, "updatedAt": ended})
            self.assertTrue(self._shell(root)._capcut_run_started_for_command(TASK_ID, self._request()))

    def test_fresh_manifest_whose_client_has_not_written_status_yet(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._attempt(root, "a", created=datetime.now().astimezone())
            request = {"taskId": TASK_ID, "startedAt": datetime.now().astimezone().isoformat(timespec="seconds")}
            self.assertTrue(self._shell(root)._capcut_run_started_for_command(TASK_ID, request))

    def test_attempt_of_another_task_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._attempt(root, "a", owner="other-task",
                          status={"running": True, "heartbeatAt": datetime.now().astimezone().isoformat()})
            self.assertFalse(self._shell(root)._capcut_run_started_for_command(TASK_ID, self._request()))

    def test_attempt_created_long_before_the_command_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ended = (self.STARTED + timedelta(seconds=10)).isoformat()
            self._attempt(root, "old", created=self.STARTED - timedelta(minutes=6),
                          status={"running": False, "updatedAt": ended})
            self.assertFalse(self._shell(root)._capcut_run_started_for_command(TASK_ID, self._request()))

    def test_attempt_that_ended_before_the_command_started_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ended = (self.STARTED - timedelta(seconds=2)).isoformat()
            self._attempt(root, "a", status={"running": False, "updatedAt": ended})
            self.assertFalse(self._shell(root)._capcut_run_started_for_command(TASK_ID, self._request()))

    def test_missing_start_time_keeps_the_previous_behaviour(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(self._shell(Path(temp))._capcut_run_started_for_command(TASK_ID, {"taskId": TASK_ID}))


class WorkbenchLogFileTests(unittest.TestCase):
    def test_log_lines_are_appended_to_the_daily_file(self):
        with tempfile.TemporaryDirectory() as temp:
            log_dir = Path(temp) / "logs"
            now = datetime(2026, 10, 7, 11, 34, 25)
            with patch.object(workbench, "WORKBENCH_LOG_DIR", log_dir):
                workbench.append_workbench_log("第一行", now=now)
                workbench.append_workbench_log("第二行", now=now)
            lines = (log_dir / "workbench-20261007.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(2, len(lines))
            self.assertTrue(lines[0].startswith("2026-10-07T11:34:25 [pid "))
            self.assertTrue(lines[1].endswith("第二行"))

    def _log_shell(self):
        app = object.__new__(Workbench)
        lines = []
        app.log = SimpleNamespace(insert=lambda _where, text: lines.append(text.split("] ", 1)[1].rstrip("\n")),
                                  see=lambda _where: None)
        return app, lines

    def test_repetitive_progress_lines_are_throttled_and_other_lines_kept(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(workbench, "WORKBENCH_LOG_DIR", Path(temp) / "logs"):
            app, lines = self._log_shell()
            clock = [1000.0]
            with patch.object(workbench.time, "monotonic", side_effect=lambda: clock[0]):
                for percent in (1, 2, 3):
                    app.write_log(f"视频下载 {percent}%")
                for _ in range(3):
                    app.write_log("CUDA STTN subtitle removal attempt 1")
                app.write_log("CUDA STTN subtitle removal attempt 2")
                app.write_log("Subtitle verification: GPU OCR sample 1/30")
                app.write_log("Global subtitle verification: GPU OCR sample 2/15")
                app.write_log("下载与去字幕完成")
                app.write_log("下载与去字幕完成")
                app.write_log("Ignored cross-date CapCut attribution: owner=a, rows=24")
                app.write_log("Ignored cross-date CapCut attribution: owner=a, rows=24")
                app.write_log("Ignored cross-date CapCut attribution: owner=b, rows=3")
                clock[0] += 31
                app.write_log("视频下载 40%")
            self.assertEqual([
                "视频下载 1%",
                "CUDA STTN subtitle removal attempt 1",
                "CUDA STTN subtitle removal attempt 2",
                "Subtitle verification: GPU OCR sample 1/30",
                "下载与去字幕完成",
                "下载与去字幕完成",
                "Ignored cross-date CapCut attribution: owner=a, rows=24",
                "Ignored cross-date CapCut attribution: owner=b, rows=3",
                "视频下载 40%",
            ], lines)
            written = (Path(temp) / "logs").glob("workbench-*.log")
            file_lines = [line.split("] ", 1)[1] for path in written
                          for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines, file_lines, "the daily file must mirror the window exactly")

    def test_unwritable_log_never_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            with patch.object(workbench, "WORKBENCH_LOG_DIR", blocker / "logs"):
                workbench.append_workbench_log("不会抛出异常")


class Step12ReconcileTests(unittest.TestCase):
    """A Step 1/2 Safe Continue is judged by its download, never left unknown."""

    COMMAND = {"commandId": "continue-1", "taskId": TASK_ID, "canonicalTaskId": TASK_ID,
               "step": "step12", "state": "running"}

    def _shell(self, process=None, active=TASK_ID):
        app = object.__new__(Workbench)
        app.active_external_task = {"taskId": active}
        app.download_process = process
        return app

    def _observe(self, record, process=None, alive=True):
        app = self._shell(process)
        registry = {"items": {TASK_ID: record} if record is not None else {}}
        with patch.object(workbench, "load_project_download_statuses", return_value=registry),                 patch.object(workbench, "_pid_is_alive", return_value=alive):
            return app._step12_recovery_observation(dict(self.COMMAND))

    @staticmethod
    def _age(seconds):
        return (datetime.now().astimezone() - timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def test_live_download_of_this_task_is_alive(self):
        process = SimpleNamespace(pid=4321, poll=lambda: None)
        result = self._observe({"status": "interrupted", "pipelineStatus": "step12_interrupted"}, process)
        self.assertEqual("alive", result["status"])

    def test_dead_bridge_pid_is_not_alive(self):
        process = SimpleNamespace(pid=4321, poll=lambda: None)
        result = self._observe({"status": "interrupted", "pipelineStatus": "step12_interrupted",
                                "stateText": "已中断：20 / 24，可安全继续"}, process, alive=False)
        self.assertEqual(("failed", "interrupted"), (result["status"], result["outcome"]))
        self.assertEqual("已中断：20 / 24，可安全继续", result["reason"])

    def test_parked_at_missing_links_completes_the_command(self):
        result = self._observe({"status": "blocked_missing_links", "pipelineStatus": "blocked_missing_links",
                                "stateText": "待补链：已完成 21 / 24，还差 3 条"})
        self.assertEqual(("completed", "missing_links"), (result["status"], result["outcome"]))
        self.assertEqual("待补链：已完成 21 / 24，还差 3 条", result["reason"])

    def test_project_past_step2_completes_the_command(self):
        result = self._observe({"status": "completed", "pipelineStatus": "voice_running"})
        self.assertEqual(("completed", "step12_completed"), (result["status"], result["outcome"]))

    def test_runtime_failure_fails_the_command(self):
        result = self._observe({"status": "blocked_download_runtime", "pipelineStatus": "blocked_download_runtime"})
        self.assertEqual("failed", result["status"])

    def test_fresh_running_record_waits_for_the_final_report(self):
        result = self._observe({"status": "running", "pipelineStatus": "downloading", "updatedAt": self._age(30)})
        self.assertEqual("alive", result["status"])

    def test_stale_running_record_without_a_download_fails(self):
        result = self._observe({"status": "running", "pipelineStatus": "downloading", "updatedAt": self._age(3600)})
        self.assertEqual(("failed", "interrupted"), (result["status"], result["outcome"]))

    def test_unknown_record_keeps_the_conservative_path(self):
        self.assertIsNone(self._observe(None))

    def test_unexecuted_command_is_not_judged_by_the_download(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._shell()
            command = {**self.COMMAND, "state": "dispatched"}
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", Path(temp) / "continue.json"),                     patch.object(Workbench, "_step12_recovery_observation") as observe:
                result = app._recovery_process_observation(command)
            observe.assert_not_called()
            self.assertEqual("redispatch", result["status"])

    def _mailbox(self, root, **fields):
        mailbox = root / "continue.json"
        mailbox.write_text(json.dumps({"commandId": "continue-1", "taskId": TASK_ID, "state": "recovering",
                                       **fields}, ensure_ascii=False), encoding="utf-8")
        return mailbox

    def _reconciled(self, state, outcome, **extra):
        return [{**self.COMMAND, "state": state,
                 "executionReceipt": {"outcome": outcome, "reason": "待补链：已完成 21 / 24，还差 3 条"}, **extra}]

    def test_stopped_resume_closes_its_mailbox(self):
        with tempfile.TemporaryDirectory() as temp:
            mailbox = self._mailbox(Path(temp))
            app = self._shell()
            app._atomic_write_json = workbench.atomic_write_json
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", mailbox):
                events = app._close_step12_continue_mailbox(self._reconciled("completed", "missing_links"))
            saved = json.loads(mailbox.read_text(encoding="utf-8"))
            self.assertEqual(("blocked", "step12_missing_links"), (saved["state"], saved["phase"]))
            self.assertEqual("待补链：已完成 21 / 24，还差 3 条", saved["message"])
            self.assertTrue(saved["finishedAt"])
            self.assertEqual(1, len(events))

    def test_resume_that_reached_step3_keeps_its_mailbox(self):
        self._assert_mailbox_kept(self._reconciled("completed", "step12_completed"))

    def test_other_command_or_ignored_record_keeps_the_mailbox(self):
        self._assert_mailbox_kept(self._reconciled("completed", "missing_links", commandId="continue-2"))
        self._assert_mailbox_kept(self._reconciled("completed", "missing_links", ignored=True))
        self._assert_mailbox_kept(self._reconciled("completed", "missing_links", step="step5"))

    def _assert_mailbox_kept(self, reconciled):
        with tempfile.TemporaryDirectory() as temp:
            mailbox = self._mailbox(Path(temp))
            app = self._shell()
            app._atomic_write_json = workbench.atomic_write_json
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", mailbox):
                self.assertEqual([], app._close_step12_continue_mailbox(reconciled))
            self.assertEqual("recovering", json.loads(mailbox.read_text(encoding="utf-8"))["state"])


class CompletedProjectDraftReleaseTests(unittest.TestCase):
    """A finished project whose draft still waits for release gets a command."""

    TASK = {"taskId": TASK_ID, "date": "10.9", "startMode": "auto_start_sequential"}

    def _shell(self, rows):
        app = object.__new__(Workbench)
        app.capcut_store = SimpleNamespace(load_date=lambda _date: {"tasks": rows})
        app._continue_recovery_identity = lambda _task: {
            "verified": True, "completed": True, "step": "step5",
            "stableRecoveryKey": f"step5|{TASK_ID}", "canonicalTaskId": TASK_ID,
        }
        app._fish_bridge_runtime_file = lambda: Path("missing-runtime.json")
        app._voice_handoff_file = lambda: Path("missing-handoff.json")
        app._auto_recovery_resources_busy = lambda *_args: False
        claims = []
        app.recovery_control = SimpleNamespace(
            status=lambda: {"commands": {}},
            claim_recovery=lambda identity, trigger, **_kw: claims.append(identity) or {
                "accepted": True, "command": {"commandId": "continue-new"}},
            accept_command=lambda command_id, *_a, **_kw: {"commandId": command_id, "decision": "execute_new"},
        )
        return app, claims

    @staticmethod
    def _row(**fields):
        return {"remotePipelineTaskId": TASK_ID, "status": "completed", "productionStatus": "completed",
                "draftReleaseStatus": "released", "draftCleanupStatus": "completed", **fields}

    def test_pending_release_claims_a_command(self):
        app, claims = self._shell([self._row(), self._row(draftReleaseStatus="pending", draftCleanupStatus="pending")])
        with tempfile.TemporaryDirectory() as temp,                 patch.object(workbench, "CONTINUE_COMMAND_FILE", Path(temp) / "continue.json"):
            result = app._prepare_recovery_request(dict(self.TASK))
        self.assertEqual("execute_new", result["decision"])
        self.assertEqual(1, len(claims))

    def test_fully_released_project_stays_completed(self):
        app, claims = self._shell([self._row(), self._row()])
        with tempfile.TemporaryDirectory() as temp,                 patch.object(workbench, "CONTINUE_COMMAND_FILE", Path(temp) / "continue.json"):
            result = app._prepare_recovery_request(dict(self.TASK))
        self.assertEqual("completed", result["decision"])
        self.assertEqual([], claims)


class CapCutStoppedCommandTests(unittest.TestCase):
    """2026-10-11 15:13: the client could not acquire the draft root, wrote a
    terminal status with the task Waiting + error code, and exited; the
    command stayed reconciliation_required and absorbed later clicks."""

    COMMAND = {
        "commandId": "continue-c1", "step": "step5", "state": "accepted",
        "taskId": TASK_ID, "childTaskId": "child-1",
        "claimedAt": "2026-10-11T07:13:45+00:00",
        "acceptedAt": "2026-10-11T07:13:47+00:00",
    }

    @staticmethod
    def _status(updated_at="2026-10-11T15:13:53.98+08:00", **task_fields):
        task = {"taskId": "child-1", "state": "Waiting",
                "errorCode": "CAPCUT_DRAFT_RELEASE_PENDING",
                "error": "已请求停止；草稿目录恢复待处理", **task_fields}
        return {"running": False, "completed": 0, "failed": 0,
                "remotePipelineTaskId": TASK_ID, "currentTaskId": "child-1",
                "updatedAt": updated_at, "tasks": [task]}

    def _observe(self, status):
        with tempfile.TemporaryDirectory() as temp:
            status_path = Path(temp) / "run.status.json"
            status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
            app = object.__new__(Workbench)
            app.active_external_task = {"taskId": TASK_ID}
            command = {**self.COMMAND, "executionReceipt": {
                "processIdentity": {}, "statusFile": str(status_path), "taskId": TASK_ID}}
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", Path(temp) / "continue.json"):
                return app._recovery_process_observation(command)

    def test_stopped_attempt_closes_the_command_as_failed(self):
        result = self._observe(self._status())
        self.assertEqual("failed", result["status"])
        self.assertEqual("已请求停止；草稿目录恢复待处理", result["reason"])

    def test_status_older_than_the_command_stays_unknown(self):
        result = self._observe(self._status(updated_at="2026-10-11T15:02:38+08:00"))
        self.assertEqual("unknown", result["status"])

    def test_waiting_task_without_error_stays_unknown(self):
        result = self._observe(self._status(errorCode="", error=""))
        self.assertEqual("unknown", result["status"])

    def test_attempt_that_completed_work_then_stopped_is_failed(self):
        # 2026-10-16 09:33: one item completed, then the next stopped with
        # CAPCUT_EDITOR_PROBE_TIMEOUT; the command stayed reconciliation_required.
        status = {**self._status(errorCode="CAPCUT_EDITOR_PROBE_TIMEOUT",
                                 error="创建本条独立草稿失败"), "completed": 1}
        result = self._observe(status)
        self.assertEqual("failed", result["status"])
        self.assertEqual("创建本条独立草稿失败", result["reason"])

    def test_completed_current_task_is_not_failed(self):
        status = {**self._status(state="Completed"), "completed": 1}
        self.assertEqual("completed", self._observe(status)["status"])

    def _observe_without_receipt(self, poll_result):
        with tempfile.TemporaryDirectory() as temp:
            app = object.__new__(Workbench)
            app.active_external_task = {"taskId": TASK_ID}
            process = SimpleNamespace(pid=os.getpid(), poll=lambda: poll_result)
            app.capcut_launch_records = {TASK_ID: {"pid": os.getpid(), "process": process}}
            command = {**self.COMMAND, "executionReceipt": {
                "processIdentity": {}, "statusFile": "", "taskId": TASK_ID, "requestAccepted": True}}
            with patch.object(workbench, "CONTINUE_COMMAND_FILE", Path(temp) / "continue.json"):
                return app._recovery_process_observation(command)

    def test_client_launched_after_the_receipt_is_alive(self):
        # 2026-10-11 15:52-16:13: the receipt was written before the queue
        # launched the client, so every poll reported reconciliation_required.
        self.assertEqual("alive", self._observe_without_receipt(None)["status"])

    def test_exited_client_without_status_stays_unknown(self):
        self.assertEqual("unknown", self._observe_without_receipt(0)["status"])

    def test_running_status_is_not_terminal(self):
        status = {**self._status(), "running": True}
        self.assertEqual("unknown", self._observe(status)["status"])


if __name__ == "__main__":
    unittest.main()
