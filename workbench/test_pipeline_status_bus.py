import tempfile
import unittest
from pathlib import Path

from pipeline_status_bus import PipelineStatusStore


class PipelineStatusStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "snapshot.json"
        self.store = PipelineStatusStore(self.path)
        self.task = {
            "taskId": "task-1", "batchId": "batch-1", "batchSequence": 1,
            "batchSize": 9, "expectedCount": 1, "urls": ["https://example.test/1"],
        }
        self.store.register_task(self.task)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_event_is_ignored_after_restart(self):
        event = {"eventId": "event-1", "taskId": "task-1", "batchId": "batch-1",
                 "url": self.task["urls"][0], "stage": "ocr", "status": "running"}
        self.assertFalse(self.store.apply(event)["duplicate"])
        restarted = PipelineStatusStore(self.path)
        self.assertTrue(restarted.apply(event)["duplicate"])
        self.assertEqual(1, len(restarted.events_after(0)))

    def test_missing_final_file_cannot_be_completed(self):
        self.store.apply({"eventId": "event-2", "taskId": "task-1", "batchId": "batch-1",
                          "url": self.task["urls"][0], "stage": "complete",
                          "status": "done", "outputPath": str(Path(self.temp.name) / "missing.mp4")})
        project = self.store.snapshot()["projects"]["task-1"]
        self.assertEqual("failed", project["status"])
        self.assertEqual(0, project["completed"])

    def test_cancel_is_idempotent_and_removes_empty_batch(self):
        first = self.store.cancel_task("task-1", reason="operator_cancelled", task=self.task)
        second = self.store.cancel_task("task-1", reason="operator_cancelled", task=self.task)
        snapshot = self.store.snapshot()
        self.assertTrue(first["archived"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertNotIn("task-1", snapshot["projects"])
        self.assertNotIn("batch-1", snapshot["batches"])
        self.assertEqual("cancelled", snapshot["archivedProjects"]["task-1"]["status"])

    def test_late_event_cannot_reactivate_cancelled_task(self):
        self.store.cancel_task("task-1", reason="operator_cancelled", task=self.task)
        result = self.store.apply({
            "eventId": "late-1", "taskId": "task-1", "batchId": "batch-1",
            "url": self.task["urls"][0], "stage": "ocr", "status": "running",
        })
        snapshot = self.store.snapshot()
        self.assertTrue(result["ignored"])
        self.assertNotIn("task-1", snapshot["projects"])
        self.assertTrue(snapshot["events"][-1]["ignored"])

    def test_reconcile_archives_cancelled_and_merged_child_only(self):
        second = dict(self.task, taskId="task-2", batchSequence=2)
        child = dict(self.task, taskId="child-1", batchSequence=3)
        self.store.register_task(second)
        self.store.register_task(child)
        result = self.store.reconcile([
            dict(self.task, status="cancelled", cancelReason="revoked"),
            dict(child, status="merged_into_parent", replenishForTaskId="task-2"),
        ])
        snapshot = self.store.snapshot()
        self.assertEqual("healthy", result["state"])
        self.assertEqual(3, snapshot["schemaVersion"])
        self.assertIn("servedAt", snapshot)
        self.assertIn("task-2", snapshot["projects"])
        self.assertNotIn("task-1", snapshot["projects"])
        self.assertNotIn("child-1", snapshot["projects"])
        self.assertEqual("merged_into_parent", snapshot["archivedProjects"]["child-1"]["status"])

    def test_reconcile_uses_parent_identity_and_workbench_progress(self):
        # Simulate a completed one-link replenishment whose local progress file
        # accidentally carries its one-item batch identity.  The parent task
        # file must keep the original task box, while progress becomes 5/5.
        original = dict(
            self.task,
            batchId="formal-53", batchSequence=1, batchSize=9,
            expectedCount=5, productionDate="7.22", _authoritySource="task_file",
        )
        self.store.register_task(original)
        progress = {
            "taskId": "task-1", "batchId": "replenish-one", "batchSequence": 1,
            "batchSize": 1, "expectedCount": 5, "completed": 5, "failed": 0,
            "status": "completed", "stateText": "下载与 AI 去字幕完成：5 / 5",
            "_authoritySource": "workbench_project_status",
        }

        self.store.reconcile([original, progress])

        project = self.store.snapshot()["projects"]["task-1"]
        self.assertEqual("formal-53", project["batchId"])
        self.assertEqual(9, project["batchSize"])
        self.assertEqual(5, project["completed"])
        self.assertEqual(0, project["remaining"])
        self.assertEqual("completed", project["status"])

    def test_reset_starts_an_empty_reconciled_generation(self):
        self.store.apply({
            "eventId": "before-reset", "taskId": "task-1", "batchId": "batch-1",
            "url": self.task["urls"][0], "stage": "ocr", "status": "running",
        })

        result = self.store.reset("repeat-e2e")
        restarted = PipelineStatusStore(self.path).snapshot()

        self.assertEqual({}, result["projects"])
        self.assertEqual({}, restarted["batches"])
        self.assertEqual([], restarted["events"])
        self.assertEqual(0, restarted["sequence"])
        self.assertEqual("healthy", restarted["reconciliationState"])
        self.assertIn("repeat-e2e", restarted["reconciliationMessage"])


if __name__ == "__main__":
    unittest.main()
