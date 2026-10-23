import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import workbench
from common_io import atomic_write_json
from workbench import Workbench


BATCH = "formal-batch-1"


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _task(sequence, **fields):
    return {
        "taskId": f"{BATCH}-{sequence:02d}", "batchId": BATCH, "batchSequence": sequence, "batchSize": 3,
        "startMode": "auto_start_sequential", "date": "10.9", "project": "SPC地板", "material": f"材料{sequence}",
        "urls": [f"https://www.douyin.com/video/{sequence}00{index}" for index in range(2)],
        "expectedCount": 2, **fields,
    }


class PrefetchTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.downloads = self.root / "downloads"
        self.prefetch_dir = self.root / "prefetch"
        self.checkpoints = self.root / "checkpoints"
        self.runtime = self.root / "python.exe"
        self.runtime.write_text("", encoding="utf-8")
        self.registry = {"items": {f"{BATCH}-01": {"pipelineStatus": "voice_running", "status": "completed"}}}
        self.popen = Mock(return_value=SimpleNamespace(pid=4321, poll=lambda: None))
        for patcher in (
            patch.object(workbench, "DOWNLOAD_PREFETCH_DIR", self.prefetch_dir),
            patch.object(workbench, "DOWNLOAD_CHECKPOINT_DIR", self.checkpoints),
            patch.object(workbench, "DOWNLOAD_BRIDGE_RUNTIME", self.runtime),
            patch.object(workbench, "load_project_download_statuses", side_effect=lambda: self.registry),
            patch.object(workbench, "capture_process_identity", return_value={"pid": 4321, "processStartTime": 1.0}),
            patch.object(workbench.subprocess, "Popen", self.popen),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _shell(self, active=None, pending=(2, 3), enabled="1"):
        app = object.__new__(Workbench)
        app.active_external_task = active if active is not None else _task(1)
        app.inbox_tasks = {}
        for item in pending:
            task = item if isinstance(item, dict) else _task(item)
            app.inbox_tasks[task["taskId"]] = (self.root / f"{task['taskId']}.json", task)
        app.download_process = None
        app.auto_start_claimed_task_ids = set()
        app.vars = {"prefetch_next_project": _Var(enabled), "tikhub_downloads": _Var(str(self.downloads))}
        app.batch_root_sources = {"tikhub_downloads": self.downloads}
        app.mode = _Var("AI 去除 STTN")
        app.status_var = _Var()
        app.logs = []
        app.write_log = app.logs.append
        app._atomic_write_json = atomic_write_json
        return app

    def _directory(self, sequence):
        return self.downloads / "任务批次" / "10.9" / "SPC地板" / f"材料{sequence}"

    def _state(self, sequence):
        path = self.prefetch_dir / f"{BATCH}-{sequence:02d}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


class PrefetchStartTests(PrefetchTestCase):
    def test_next_item_is_prefetched_while_the_current_one_is_in_step3(self):
        app = self._shell()
        self.assertTrue(app.poll_prefetch_next_project())
        self.popen.assert_called_once()
        request_file = Path(self.popen.call_args.args[0][-1])
        request = json.loads(request_file.read_text(encoding="utf-8"))
        self.assertEqual(f"{BATCH}-02", request["taskId"])
        self.assertEqual(str(self._directory(2)), request["download_path"])
        self.assertFalse(request["statusRelay"])
        self.assertEqual("ai-sttn", request["mode"])
        state = self._state(2)
        self.assertEqual(("running", 4321, True), (state["state"], state["pid"], state["directoryWasEmpty"]))
        self.assertIs(app.prefetch_process, self.popen.return_value)
        self.assertTrue(any("开始预取" in line for line in app.logs))

    def test_nothing_starts_while_the_current_project_is_still_in_step12(self):
        for pipeline in ("downloading", "step12_resuming", "blocked_missing_links", "completed", ""):
            self.registry["items"][f"{BATCH}-01"]["pipelineStatus"] = pipeline
            self.assertFalse(self._shell().poll_prefetch_next_project(), pipeline)
        self.popen.assert_not_called()

    def test_live_download_of_the_current_project_blocks_prefetch(self):
        app = self._shell()
        app.download_process = SimpleNamespace(poll=lambda: None)
        self.assertFalse(app.poll_prefetch_next_project())
        self.popen.assert_not_called()

    def test_switch_off_disables_prefetch(self):
        self.assertFalse(self._shell(enabled="0").poll_prefetch_next_project())
        self.popen.assert_not_called()

    def test_pending_replenishment_goes_first(self):
        gap = _task(9, isReplenishment=True, replenishForTaskId=f"{BATCH}-01")
        self.assertFalse(self._shell(pending=(2, gap)).poll_prefetch_next_project())
        self.popen.assert_not_called()

    def test_lowest_later_item_of_the_same_box_is_chosen(self):
        other_box = _task(2, batchId="formal-batch-other", taskId="other-02")
        app = self._shell(pending=(other_box, 3, 2))
        app.auto_start_claimed_task_ids.add(f"{BATCH}-02")
        self.assertTrue(app.poll_prefetch_next_project())
        self.assertEqual(f"{BATCH}-03", self._state(3)["taskId"])

    def test_examplebrand_and_manual_items_are_not_prefetched(self):
        examplebrand = _task(2, project="ExampleBrand")
        manual = _task(3, startMode="manual")
        self.assertFalse(self._shell(pending=(examplebrand, manual)).poll_prefetch_next_project())
        self.assertFalse(self._shell(active=_task(1, batchKind="examplebrand-natural-flow")).poll_prefetch_next_project())
        self.popen.assert_not_called()

    def test_directory_with_videos_is_left_alone_and_reported_once(self):
        directory = self._directory(2)
        directory.mkdir(parents=True)
        (directory / "old.mp4").write_bytes(b"x")
        app = self._shell(pending=(2,))
        self.assertFalse(app.poll_prefetch_next_project())
        self.assertFalse(app.poll_prefetch_next_project())
        self.popen.assert_not_called()
        self.assertEqual(1, sum("未预取" in line for line in app.logs))

    def test_started_item_or_earlier_prefetch_is_not_repeated(self):
        self.registry["items"][f"{BATCH}-02"] = {"pipelineStatus": "downloading"}
        self.assertFalse(self._shell(pending=(2,)).poll_prefetch_next_project())
        self.registry["items"].pop(f"{BATCH}-02")
        self.prefetch_dir.mkdir(parents=True)
        atomic_write_json(self.prefetch_dir / f"{BATCH}-02.json", {"taskId": f"{BATCH}-02", "state": "finished"})
        self.assertFalse(self._shell(pending=(2,)).poll_prefetch_next_project())
        self.popen.assert_not_called()


class PrefetchLifecycleTests(PrefetchTestCase):
    def _running(self, sequence=2, pid=4321):
        self.prefetch_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.prefetch_dir / f"{BATCH}-{sequence:02d}.json", {
            "taskId": f"{BATCH}-{sequence:02d}", "batchSequence": sequence, "project": "SPC地板",
            "material": f"材料{sequence}", "urlCount": 2, "downloadPath": str(self._directory(sequence)),
            "directoryWasEmpty": True, "state": "running", "pid": pid,
            "processIdentity": {"pid": pid, "processStartTime": 1.0},
        })

    def test_live_bridge_of_this_session_is_running(self):
        self._running()
        app = self._shell()
        app.prefetch_process = SimpleNamespace(pid=4321, poll=lambda: None)
        self.assertEqual(f"{BATCH}-02", app._prefetch_running())
        self.assertFalse(app.poll_prefetch_next_project())
        self.popen.assert_not_called()

    def test_finished_bridge_is_closed_with_its_checkpoint_count(self):
        self._running()
        output = self._directory(2) / "a_ai_sttn.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"v" * 2048)
        self.checkpoints.mkdir(parents=True)
        atomic_write_json(self.checkpoints / f"{BATCH}-02.json", {"items": {
            "u1": {"status": "completed", "outputPath": str(output)},
            "u2": {"status": "failed"},
        }})
        app = self._shell()
        app.prefetch_process = SimpleNamespace(pid=4321, poll=lambda: 0)
        self.assertEqual("", app._prefetch_running())
        state = self._state(2)
        self.assertEqual(("finished", 0, 1), (state["state"], state["exitCode"], state["completedOutputs"]))
        self.assertIsNone(app.prefetch_process)
        self.assertTrue(any("预取结束" in line and "1/2" in line for line in app.logs))

    def test_bridge_from_before_a_restart_is_observed_by_identity(self):
        self._running()
        app = self._shell()
        with patch.object(workbench, "observe_process_identity", return_value="matched_alive"):
            self.assertEqual(f"{BATCH}-02", app._prefetch_running())
        with patch.object(workbench, "observe_process_identity", return_value="exited"):
            self.assertEqual("", app._prefetch_running())
        self.assertEqual("finished", self._state(2)["state"])

    def test_unverifiable_identity_falls_back_to_the_pid(self):
        self._running()
        app = self._shell()
        with patch.object(workbench, "observe_process_identity", return_value="unverifiable"), \
                patch.object(workbench, "_pid_is_alive", return_value=True):
            self.assertEqual(f"{BATCH}-02", app._prefetch_running())


class PrefetchGateTests(PrefetchLifecycleTests):
    def _queue(self, prefetch_alive):
        self._running()
        app = self._shell()
        app.prefetch_process = SimpleNamespace(pid=4321, poll=lambda: None if prefetch_alive else 0)
        app.stitcher_controller = None
        app.inbox_task_var = _Var()
        app.import_external_task = Mock(return_value=True)
        app._previous_remote_item_finished = lambda *_args: True
        app._previous_run_group_child_finished = lambda *_args: True
        app._examplebrand_global_idle = lambda *_args: True
        app.auto_start_next_external_task()
        return app.import_external_task

    def test_queue_waits_for_a_running_prefetch(self):
        self._queue(prefetch_alive=True).assert_not_called()

    def test_queue_starts_the_item_once_its_prefetch_finished(self):
        self._queue(prefetch_alive=False).assert_called_once_with(auto_start=True)

    def test_download_start_waits_for_a_running_prefetch(self):
        self._running()
        app = self._shell()
        app.prefetch_process = SimpleNamespace(pid=4321, poll=lambda: None)
        self.assertFalse(app.start_background_download(auto_start=True, resume_existing=True))
        self.popen.assert_not_called()
        self.assertIn("正在预取", app.status_var.get())


class PrefetchReuseTests(PrefetchTestCase):
    def _finished(self, directory, **fields):
        self.prefetch_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.prefetch_dir / f"{BATCH}-02.json", {
            "taskId": f"{BATCH}-02", "state": "finished", "directoryWasEmpty": True,
            "downloadPath": str(directory), **fields,
        })

    def _start(self, app):
        directory = self._directory(2)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "a.mp4").write_bytes(b"v")
        (directory / "a_leadtrim2s_ai_sttn.mp4").write_bytes(b"v")
        return app.confirm_download_directory_cleanup(directory, auto_start=True)

    def test_queue_start_reuses_its_own_prefetch_results(self):
        self._finished(self._directory(2))
        app = self._shell(active=_task(2))
        self.assertTrue(self._start(app))
        self.assertTrue(any("使用本项目的预取结果" in line for line in app.logs))

    def test_without_a_prefetch_the_old_guard_still_refuses(self):
        self.assertFalse(self._start(self._shell(active=_task(2))))

    def test_prefetch_of_another_directory_or_unfinished_run_is_not_trusted(self):
        self._finished(self.root / "elsewhere")
        self.assertFalse(self._start(self._shell(active=_task(2))))
        self._finished(self._directory(2), state="running")
        self.assertFalse(self._start(self._shell(active=_task(2))))


if __name__ == "__main__":
    unittest.main()
