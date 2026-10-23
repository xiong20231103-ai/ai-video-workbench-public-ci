import json
import tempfile
import unittest
from pathlib import Path

import task_inbox as task_inbox_module
from task_inbox import (
    canonical_task_id,
    cancel_imported_task,
    cancel_pending_task,
    create_task,
    list_pending_tasks,
    mark_imported,
    resolve_task_audio_directory,
    save_project_download_status,
    submit_task,
    submit_task_batch,
    validate_task,
    request_pending_start,
)


class TaskInboxTests(unittest.TestCase):
    def test_korean_is_rejected_outside_true_huang(self):
        task = create_task("cloud-carbon", "https://example.com/korean", date_label="7.20")
        task["language"] = "韩语"
        task["languagePlan"] = [{"code": "ko", "name": "韩语", "audioCount": 1, "outputCount": 1}]
        valid, error = validate_task(task)
        self.assertFalse(valid)
        self.assertIn("韩语任务只允许用于ProjectD项目", error)

    def test_replenishment_uses_parent_as_canonical_production_identity(self):
        task = create_task("cloud-carbon", "https://example.com/replacement", date_label="7.21")
        task.update({
            "isReplenishment": True,
            "replenishForTaskId": "parent-24",
            "canonicalTaskId": "parent-24",
            "taskRole": "step12_replenishment",
        })
        self.assertEqual("parent-24", canonical_task_id(task))
        self.assertEqual((True, ""), validate_task(task))

    def test_create_and_submit_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates.json"
            templates.write_text(
                '{"templateVersion":"test","templates":[{"id":"cloud","project":"ProjectA","material":"碳岩板","download_path_source":"subject_dir","subtitle_mode":"AI 去除 STTN","subtitle_enabled":true,"download_workers":2}]}',
                encoding="utf-8",
            )
            task = create_task("cloud", "https://example.com/a\nhttps://example.com/a\nhttps://example.com/b", date_label="7.20", template_path=templates)
            self.assertEqual(task["project"], "ProjectA")
            self.assertEqual(task["material"], "碳岩板")
            self.assertEqual(len(task["urls"]), 2)
            self.assertEqual(task["pipelineProfile"]["4_stitch"]["logo"], "ExampleBrandC")
            self.assertEqual(task["pipelineProfile"]["5_capcut"]["export"], "2K")
            self.assertEqual(task["expectedCount"], 24)
            self.assertEqual(task["productionDate"], "7.20")
            inbox = root / "pending"
            submit_task(task, inbox)
            self.assertEqual(len(list_pending_tasks(inbox)), 1)

    def test_entity_profile_uses_baijax_and_entity_folder(self):
        task = create_task("physical-default", "https://example.com/a", date_label="7.20", material="净白板")
        profile = task["pipelineProfile"]
        self.assertEqual(profile["4_stitch"]["logo"], "ExampleBrandB")
        self.assertEqual(profile["4_stitch"]["project"], "ProjectB净白板")
        self.assertEqual(profile["3_voice"]["audioLocation"], "ProjectB / 净白板 / 7.20")
        self.assertEqual(
            resolve_task_audio_directory(task, {"audioRoots": {"ProjectB": r"D:\家里音频\ProjectB"}}),
            Path(r"D:\家里音频\ProjectB\净白板\7.20"),
        )

    def test_weekly_audio_batch_is_kept_separate_from_production_date(self):
        task = create_task("cloud-carbon", "https://example.com/a", date_label="7.20", audio_batch="7.17")
        profile = task["pipelineProfile"]
        self.assertEqual(task["audioBatch"], "7.17")
        self.assertEqual(profile["3_voice"]["cycle"], "weekly")
        self.assertTrue(profile["3_voice"]["reuseExistingBatch"])
        self.assertEqual(profile["3_voice"]["audioLocation"], "ProjectA / 7.17")
        self.assertEqual(profile["cleanup"]["audio"], "keep_until_weekly_batch_replaced")

    def test_new_weekly_voice_plan_is_preserved_for_the_workbench(self):
        task = create_task(
            "cloud-carbon", "https://example.com/a", date_label="7.20", audio_batch="7.20", voice_plan="create_new"
        )
        voice = task["pipelineProfile"]["3_voice"]
        self.assertEqual(task["voicePlan"], "create_new")
        self.assertTrue(voice["requiresNewGeneration"])
        self.assertFalse(voice["reuseExistingBatch"])
        self.assertEqual(voice["audioBatch"], "7.20")

    def test_entity_voice_plan_is_always_new_for_the_production_date(self):
        task = create_task(
            "physical-default", "https://example.com/a", date_label="7.20", audio_batch="7.01", voice_plan="reuse_existing"
        )
        voice = task["pipelineProfile"]["3_voice"]
        self.assertEqual(task["voicePlan"], "create_new")
        self.assertEqual(task["audioBatch"], "7.20")
        self.assertTrue(voice["requiresNewGeneration"])

    def test_cancel_keeps_an_auditable_record_and_removes_pending_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = create_task("cloud-carbon", "https://example.com/a", date_label="7.20")
            inbox = root / "pending"
            cancelled = root / "cancelled"
            submit_task(task, inbox)

            record = cancel_pending_task(task["taskId"], inbox_dir=inbox, cancelled_dir=cancelled)

            self.assertEqual(record["status"], "cancelled")
            self.assertTrue(record["cancelledAt"])
            self.assertEqual(list_pending_tasks(inbox), [])
            self.assertTrue((cancelled / f"{task['taskId']}.json").is_file())

    def test_revoke_imported_waiting_task_keeps_an_auditable_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = create_task("cloud-carbon", "https://example.com/a", date_label="7.20")
            pending = root / "pending"
            imported = root / "imported"
            cancelled = root / "cancelled"
            context = root / "current_pipeline_context.json"
            project_status = root / "project_status.json"
            source = submit_task(task, pending)
            mark_imported(source, task, imported_dir=imported, current_context_file=context)
            save_project_download_status({"taskId": task["taskId"], "expectedCount": 1, "completed": 0}, project_status)

            record = cancel_imported_task(
                task["taskId"],
                imported_dir=imported,
                cancelled_dir=cancelled,
                current_context_file=context,
                project_status_file=project_status,
            )

            self.assertEqual(record["status"], "cancelled")
            self.assertFalse((imported / f"{task['taskId']}.json").exists())
            self.assertTrue((cancelled / f"{task['taskId']}.json").is_file())
            self.assertEqual(context.read_text(encoding="utf-8").find('"status": "cancelled"') >= 0, True)
            self.assertNotIn(task["taskId"], project_status.read_text(encoding="utf-8"))

    def test_task_box_is_saved_in_declared_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = create_task("cloud-carbon", "https://example.com/first", date_label="7.20")
            second = create_task("huang-default", "https://example.com/second", date_label="7.20")
            for sequence, task in enumerate((first, second), start=1):
                task.update({"batchId": "dispatch-test", "batchSequence": sequence, "batchSize": 2})

            saved = submit_task_batch([first, second], root / "pending")
            pending = list_pending_tasks(root / "pending")

            self.assertEqual(len(saved), 2)
            self.assertEqual([task["taskId"] for _, task in pending], [first["taskId"], second["taskId"]])

    def test_batch_submit_materializes_missing_pipeline_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = create_task(
                "huang-default",
                "https://example.com/voice-profile-fallback",
                date_label="8.31",
                material="碳岩板",
                audio_batch="8.31",
                voice_plan="create_new",
            )
            task.pop("pipelineProfile", None)
            task.update({"batchId": "profile-fallback", "batchSequence": 1, "batchSize": 1})
            submit_task_batch([task], root / "pending")
            stored = json.loads((root / "pending" / f"{task['taskId']}.json").read_text(encoding="utf-8"))
            voice = stored["pipelineProfile"]["3_voice"]
            self.assertEqual("create_new", voice["voicePlan"])
            self.assertTrue(voice["requiresNewGeneration"])
            self.assertEqual("8.31", voice["audioBatch"])

    def test_examplebrand_start_request_creates_matching_idle_intent(self):
        """Receiver start must be consumable by the ExampleBrand workbench idle gate."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = {
                "schemaVersion": 2,
                "batchKind": "examplebrand-natural-flow",
                "runGroupId": "run-test-examplebrand-1",
                "batchId": "examplebrand-test-batch",
                "taskId": "examplebrand-test-task",
                "canonicalTaskId": "examplebrand-test-task",
                "status": "pending_confirmation",
                "date": "9.7",
                "productionDate": "9.7",
                "project": "ExampleBrand",
                "material": "潜污泵",
                "urls": ["https://example.com/video"],
            }
            pending = root / "pending"
            submit_task(task, pending)
            original_control_dir = task_inbox_module.ExampleBrand_CONTROL_DIR
            task_inbox_module.ExampleBrand_CONTROL_DIR = root / "control" / "examplebrand_start_intents"
            try:
                updated = request_pending_start(task["taskId"], inbox_dir=pending)
            finally:
                task_inbox_module.ExampleBrand_CONTROL_DIR = original_control_dir

            self.assertEqual("auto_start_first", updated["startMode"])
            intent_path = root / "control" / "examplebrand_start_intents" / f"{task['taskId']}.json"
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(task["taskId"], intent["taskId"])
            self.assertEqual(task["runGroupId"], intent["runGroupId"])
            self.assertEqual(task["batchId"], intent["batchId"])
            self.assertTrue(intent["enabled"])
            self.assertEqual("auto_when_idle", intent["startPolicy"])
            self.assertEqual(updated["startRequestedAt"], intent["eligibleAfter"])


if __name__ == "__main__":
    unittest.main()
