import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from capcut_handoff import (
    CapCutHandoffStore,
    _pid_is_alive,
    _status_is_stale,
    production_target,
    subtitle_placement_metadata,
)
from common_io import process_image_name


class CapCutHandoffTests(unittest.TestCase):
    def test_verified_sttn_source_moves_captions_to_original_band(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "verified.mp4"
            source.write_bytes(b"video")
            sidecar = source.with_suffix(source.suffix + ".subtitle-verification.json")
            sidecar.write_text(json.dumps({
                "outputPath": str(source.resolve()),
                "subtitleTreatment": "sttn_clean",
                "originalSubtitleBands": [[1200, 1500]],
                "sourceVideoHeight": 1920,
            }), encoding="utf-8")
            result = subtitle_placement_metadata([str(source)])
            self.assertEqual("original_band", result["captionPlacement"])
            self.assertAlmostEqual(-0.40625, result["captionTargetY"], places=5)

    def test_blur_fallback_keeps_company_default_caption_position(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "blurred.mp4"
            source.write_bytes(b"video")
            source.with_suffix(source.suffix + ".subtitle-verification.json").write_text(
                json.dumps({
                    "outputPath": str(source.resolve()),
                    "subtitleTreatment": "blur_fallback",
                    "originalSubtitleBands": [[1200, 1500]],
                    "sourceVideoHeight": 1920,
                }), encoding="utf-8")
            result = subtitle_placement_metadata([str(source)])
            self.assertEqual("company_default", result["captionPlacement"])
            self.assertNotIn("captionTargetY", result)

    def test_save_date_writes_the_same_bytes_as_before(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CapCutHandoffStore(Path(temp) / "交接记录", Path(temp) / "final")
            payload = {"tasks": [{"taskId": "t1", "status": "completed", "名称": "SPC地板"}], "archivedAttempts": []}
            store.save_date("10.9", payload)
            saved = store.date_file("10.9")
            legacy = saved.with_name("legacy.json")
            # The writer this replaced: text mode, indent=2, no ASCII escaping.
            legacy.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.assertEqual(legacy.read_bytes(), saved.read_bytes())
            self.assertEqual(["legacy.json", "第四步到第五步.json"], sorted(p.name for p in saved.parent.iterdir()))

    def test_save_date_retries_a_briefly_locked_target(self):
        import common_io
        with tempfile.TemporaryDirectory() as temp:
            store = CapCutHandoffStore(Path(temp) / "交接记录", Path(temp) / "final")
            real_replace = os.replace
            calls = []

            def flaky_replace(source, target):
                calls.append(target)
                if len(calls) == 1:
                    raise PermissionError(5, "拒绝访问")
                return real_replace(source, target)

            with patch.object(common_io.os, "replace", side_effect=flaky_replace), \
                    patch.object(common_io.time, "sleep"):
                store.save_date("10.9", {"tasks": []})
            self.assertEqual(2, len(calls))
            self.assertEqual([], store.load_date("10.9")["tasks"])
            self.assertEqual(["第四步到第五步.json"], [p.name for p in store.date_file("10.9").parent.iterdir()])

    def test_missing_pid_is_not_alive_and_never_raises(self):
        self.assertFalse(_pid_is_alive(2_147_483_647))

    def test_probe_never_signals_the_process(self):
        # os.kill(pid, 0) terminates the target on Windows (problem B).
        with patch("os.kill", side_effect=AssertionError("os.kill must not be used")):
            self.assertFalse(_pid_is_alive(os.getpid()))  # alive, but python.exe is not the client
            self.assertFalse(_pid_is_alive(2_147_483_647))
            self.assertFalse(_pid_is_alive("not-a-pid"))

    def test_only_a_live_capcut_client_counts(self):
        client = r"E:\capcut-auto-caption-client\bin\CapCutBatchClient.exe"
        with patch("capcut_handoff.pid_is_alive", return_value=True):
            with patch("capcut_handoff.process_image_name", return_value=client):
                self.assertTrue(_pid_is_alive(4321))
            with patch("capcut_handoff.process_image_name", return_value=r"C:\Program Files\Google\chrome.exe"):
                self.assertFalse(_pid_is_alive(4321))
            with patch("capcut_handoff.process_image_name", return_value=""):
                self.assertTrue(_pid_is_alive(4321))
        with patch("capcut_handoff.pid_is_alive", return_value=False), \
                patch("capcut_handoff.process_image_name", return_value=client):
            self.assertFalse(_pid_is_alive(4321))

    def test_stale_heartbeat_with_live_client_is_left_alone(self):
        old = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()
        status = {"running": True, "heartbeatAt": old, "clientPid": 4321}
        with patch("capcut_handoff._pid_is_alive", return_value=True):
            self.assertFalse(_status_is_stale(status))
        with patch("capcut_handoff._pid_is_alive", return_value=False):
            self.assertTrue(_status_is_stale(status))
        fresh = {**status, "heartbeatAt": datetime.now().astimezone().isoformat()}
        with patch("capcut_handoff._pid_is_alive", return_value=False):
            self.assertFalse(_status_is_stale(fresh))

    def test_process_image_name_reads_this_interpreter(self):
        if os.name != "nt":
            self.skipTest("Windows process API")
        self.assertEqual(Path(sys.executable).name.lower(), Path(process_image_name(os.getpid())).name.lower())
        self.assertEqual("", process_image_name(2_147_483_647))

    def test_ingest_keeps_exact_audio_and_merges_spc_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.19" / "SPC"
            source.mkdir(parents=True)
            subject = root / "subject.mp4"
            audio = root / "SPC地板_塑木地板_英语_009.mp3"
            video = source / "SPC地板_塑木地板_英语_009_0001.mp4"
            subject.write_bytes(b"subject")
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            added = store.ingest_records(
                "SPC地板",
                [{
                    "created_at": "2026-07-19T10:00:00",
                    "status": "success",
                    "output": str(video),
                    "audio_path": str(audio),
                    "subject_source_paths": [str(subject)],
                    "language_code": "en",
                    "cover": {"material_label": "塑木地板"},
                }],
            )
            self.assertEqual(1, added)
            task = store.load_date("7.19")["tasks"][0]
            self.assertEqual(str(audio.resolve()), task["audioPath"])
            self.assertEqual([str(subject.resolve())], task["subtitleSourcePaths"])
            self.assertEqual("SPC地板", task["project"])
            self.assertEqual("SPC", Path(task["finalOutputPath"]).parent.name)
            self.assertEqual(
                "SPC地板_塑木地板_英语_009_01.mp4",
                Path(task["finalOutputPath"]).name,
            )

    def test_repeated_ingest_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.19" / "ProjectA"
            source.mkdir(parents=True)
            audio = root / "ProjectA_碳岩板_西班牙语_001.mp3"
            video = source / "ProjectA_碳岩板_西班牙语_001_0001.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            record = {
                "status": "success",
                "output": str(video),
                "audio_path": str(audio),
                "language_code": "es",
                "cover": {"material_label": "碳岩板"},
            }
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            self.assertEqual(1, store.ingest_records("ProjectA", [record]))
            self.assertEqual(0, store.ingest_records("ProjectA", [record]))
            self.assertEqual(1, len(store.load_date("7.19")["tasks"]))

    def test_remote_records_with_same_legacy_number_get_unique_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.21" / "ProjectB"
            source.mkdir(parents=True)
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            records = []
            for suffix in ("2", "5"):
                audio = root / f"ProjectB_净白板_英语_remote-inbox-demo-01_净白板-英语-{suffix}.mp3"
                video = source / f"ProjectB_净白板_英语_remote-inbox-demo-01_净白板-英语-{suffix}_0001.mp4"
                audio.write_bytes(b"audio")
                video.write_bytes(b"video")
                records.append({
                    "created_at": "2026-07-21T10:00:00",
                    "status": "success",
                    "output": str(video),
                    "audio_path": str(audio),
                    "language_code": "en",
                    "cover": {"material_label": "净白板"},
                })
            self.assertEqual(2, store.ingest_records("ProjectB", records))
            tasks = store.load_date("7.21")["tasks"]
            outputs = {task["finalOutputPath"] for task in tasks}
            self.assertEqual(2, len(outputs))
            manifest = store.create_manifest("7.21", {task["taskId"] for task in tasks})
            self.assertEqual(2, len(json.loads(manifest.read_text(encoding="utf-8"))["tasks"]))

    def test_formal_remote_audio_uses_structured_identity_not_filename_number(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "8.31" / "ProjectD"
            source.mkdir(parents=True)
            audio = root / "ProjectD_碳岩板_俄语_remote-formal-batch-demo-01_碳岩板-俄语-4.mp3"
            video = source / "ProjectD_碳岩板_俄语_remote-formal-batch-demo-01_碳岩板-俄语-4_0001.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            record = {
                "status": "success",
                "output": str(video),
                "audio_path": str(audio),
                "language_code": "ru",
                "cover": {"material_label": "碳岩板"},
                "formalTaskIdentity": True,
                "canonicalTaskId": "formal-demo-01",
                "batchId": "formal-demo",
                "remotePipelineTaskId": "formal-demo-01",
                "remotePipelineTaskIdSource": "formal_handoff",
                "productionDate": "8.31",
                "expectedTaskCount": 26,
            }
            self.assertEqual(1, store.ingest_records("ProjectD", [record]))
            store.sync_statuses()
            task = store.load_date("8.31")["tasks"][0]
            self.assertEqual("waiting_capcut", task["status"])
            self.assertEqual("", task["error"])

    def test_formal_remote_identity_missing_fields_is_blocked_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "8.31" / "ProjectD"
            source.mkdir(parents=True)
            audio = root / "formal-missing-id.mp3"
            video = source / "formal-missing-id_0001.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            record = {
                "status": "success", "output": str(video), "audio_path": str(audio),
                "language_code": "ru", "cover": {"material_label": "碳岩板"},
                "formalTaskIdentity": True, "batchId": "formal-demo",
            }
            self.assertEqual(1, store.ingest_records("ProjectD", [record]))
            store.sync_statuses()
            task = store.load_date("8.31")["tasks"][0]
            self.assertEqual("validation_failed", task["status"])
            self.assertIn("正式任务身份不完整", task["error"])

    def test_remote_material_count_wins_over_project_total(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.22" / "ProjectB"
            source.mkdir(parents=True)
            audio = root / "ProjectB_碳岩板_英语_remote-inbox-demo-01.mp3"
            video = source / "ProjectB_碳岩板_英语_remote-inbox-demo-01_0001.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            store.ingest_records("ProjectB", [{
                "status": "success",
                "output": str(video),
                "audio_path": str(audio),
                "language_code": "en",
                "remotePipelineTaskId": "inbox-demo",
                "expectedTaskCount": 5,
                "cover": {"material_label": "碳岩板"},
            }])
            task = store.load_date("7.22")["tasks"][0]
            self.assertEqual(5, task["expectedTaskCount"])
            store.reassign_remote_owner("7.22", "inbox-demo", "inbox-parent")
            task = store.load_date("7.22")["tasks"][0]
            self.assertEqual(5, task["expectedTaskCount"])

    def test_partial_retry_manifest_carries_completed_formal_task_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.22" / "ProjectB"
            source.mkdir(parents=True)
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            records = []
            for number in (1, 2):
                audio = root / f"ProjectB_PU石_英语_remote-inbox-demo-01_{number}.mp3"
                video = source / f"ProjectB_PU石_英语_remote-inbox-demo-01_{number}_0001.mp4"
                audio.write_bytes(b"audio")
                video.write_bytes(b"video")
                records.append({
                    "status": "success",
                    "output": str(video),
                    "audio_path": str(audio),
                    "language_code": "en",
                    "remotePipelineTaskId": "inbox-demo",
                    "expectedTaskCount": 2,
                    "cover": {"material_label": "PU石"},
                })
            self.assertEqual(2, store.ingest_records("ProjectB", records))
            tasks = store.load_date("7.22")["tasks"]
            all_ids = {task["taskId"] for task in tasks}
            completed_id = tasks[0]["taskId"]
            retry_id = tasks[1]["taskId"]

            manifest = store.create_manifest(
                "7.22", {retry_id}, project_task_ids=all_ids, completed_task_ids={completed_id}
            )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(all_ids, set(payload["projectTaskIds"]))
            self.assertEqual([completed_id], payload["completedTaskIds"])
            self.assertEqual(1, payload["attemptTaskCount"])
            self.assertEqual([retry_id], [task["taskId"] for task in payload["tasks"]])

    def test_pending_duplicate_delivery_from_two_generation_records_is_collapsed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.22" / "ProjectB"
            source.mkdir(parents=True)
            audio = root / "ProjectB_碳岩板_英语_remote-inbox-demo-01.mp3"
            video = source / "ProjectB_碳岩板_英语_remote-inbox-demo-01_0001.mp4"
            first_record = source / "generation_records_first.json"
            second_record = source / "generation_records_second.json"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            first_record.write_text("[]", encoding="utf-8")
            second_record.write_text("[]", encoding="utf-8")
            record = {
                "created_at": "2026-07-22T20:00:00",
                "status": "success",
                "output": str(video),
                "audio_path": str(audio),
                "language_code": "en",
                "remotePipelineTaskId": "inbox-demo",
                "expectedTaskCount": 5,
                "cover": {"material_label": "碳岩板"},
            }
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            self.assertEqual(1, store.ingest_records("ProjectB", [record], first_record))
            self.assertEqual(0, store.ingest_records("ProjectB", [record], second_record))
            tasks = store.load_date("7.22")["tasks"]
            self.assertEqual(1, len(tasks))
            self.assertEqual(5, tasks[0]["expectedTaskCount"])

    def test_excess_pending_remote_tasks_are_archived_without_media_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.22" / "ProjectB"
            source.mkdir(parents=True)
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            records = []
            videos = []
            for index in range(1, 7):
                audio = root / f"ProjectB_碳岩板_英语_remote-inbox-demo-{index}.mp3"
                video = source / f"ProjectB_碳岩板_英语_{index:04d}.mp4"
                audio.write_bytes(b"audio")
                video.write_bytes(b"video")
                videos.append(video)
                records.append({
                    "status": "success", "output": str(video), "audio_path": str(audio),
                    "language_code": "en", "remotePipelineTaskId": "inbox-demo",
                    "expectedTaskCount": 5, "cover": {"material_label": "碳岩板"},
                })
            self.assertEqual(6, store.ingest_records("ProjectB", records))
            self.assertEqual(1, store.archive_excess_pending_remote_tasks("7.22", "inbox-demo", 5))
            payload = store.load_date("7.22")
            self.assertEqual(5, len(payload["tasks"]))
            self.assertEqual(1, len(payload["archivedOverproduction"]))
            self.assertTrue(all(video.is_file() for video in videos))

    def test_manifest_contains_only_selected_valid_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.19" / "ProjectB"
            source.mkdir(parents=True)
            audio = root / "ProjectB_净白板_英语_001.mp3"
            video = source / "ProjectB_净白板_英语_001_0001.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            store.ingest_records("ProjectB", [{
                "status": "success",
                "output": str(video),
                "audio_path": str(audio),
                "language_code": "en",
                "cover": {"material_label": "净白板"},
            }])
            task = store.load_date("7.19")["tasks"][0]
            manifest = store.create_manifest("7.19", {task["taskId"]})
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["schemaVersion"])
            self.assertTrue(payload["manifestId"])
            self.assertTrue(payload["attemptId"])
            self.assertEqual(task["taskId"], payload["tasks"][0]["taskId"])
            self.assertEqual(task["audioPath"], payload["tasks"][0]["audioPath"])

    def test_release_manifest_only_requeues_verified_pending_draft_rows(self):
        """A draft-repair manifest must never turn an unverified output into work."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "verified.mp4"
            output.write_bytes(b"verified-output")
            store = CapCutHandoffStore(root / "handoff", root / "final")
            store.save_date("9.1", {
                "schemaVersion": 3,
                "date": "9.1",
                "tasks": [
                    {
                        "taskId": "verified-pending",
                        "project": "ProjectD",
                        "material": "碳岩板",
                        "status": "completed",
                        "productionStatus": "completed",
                        "draftReleaseStatus": "pending",
                        "draftCleanupStatus": "release_pending",
                        "qualityStatus": "passed",
                        "finalOutputPath": str(output),
                    },
                    {
                        "taskId": "verified-released",
                        "project": "ProjectD",
                        "material": "碳岩板",
                        "status": "completed",
                        "productionStatus": "completed",
                        "draftReleaseStatus": "released",
                        "qualityStatus": "passed",
                        "finalOutputPath": str(output),
                    },
                    {
                        "taskId": "not-verified",
                        "project": "ProjectD",
                        "material": "碳岩板",
                        "status": "completed",
                        "productionStatus": "completed",
                        "draftReleaseStatus": "pending",
                        "qualityStatus": "blocked",
                        "finalOutputPath": str(output),
                    },
                ],
            })

            manifest = store.create_release_manifest("9.1", {
                "verified-pending", "verified-released", "not-verified"
            })
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertTrue(payload["releaseOnly"])
            self.assertEqual(["verified-pending"], [
                item["taskId"] for item in payload["tasks"]
            ])
            self.assertTrue(payload["tasks"][0]["releaseOnly"])
            refreshed = store.load_date("9.1")
            row = next(item for item in refreshed["tasks"]
                       if item["taskId"] == "verified-pending")
            self.assertEqual("waiting_client", row["status"])
            self.assertEqual("completed", row["productionStatus"])
            self.assertEqual("pending", row["draftReleaseStatus"])

    def test_status_sync_persists_quality_evidence_for_release_recovery(self):
        """Safe Continue and release manifest must consume the same quality fact."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "verified.mp4"
            report = root / "verified.mp4.quality.json"
            output.write_bytes(b"verified-output")
            report.write_text('{"status":"passed"}', encoding="utf-8")
            status_file = root / "verified.status.json"
            status_file.write_text(json.dumps({
                "running": False,
                "manifestId": "manifest-quality",
                "attemptId": "attempt-quality",
                "tasks": [{
                    "taskId": "quality-sync",
                    "manifestId": "manifest-quality",
                    "attemptId": "attempt-quality",
                    "state": "Completed",
                    "productionStatus": "completed",
                    "draftReleaseStatus": "pending",
                    "draftCleanupStatus": "release_pending",
                    "qualityStatus": "passed",
                    "qualityReportPath": str(report),
                    "qualityCheckedAt": "2026-09-02T00:00:00+08:00",
                    "qualityWarnings": ["diagnostic-only"],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            store = CapCutHandoffStore(root / "handoff", root / "final")
            store.save_date("9.1", {
                "schemaVersion": 3,
                "date": "9.1",
                "tasks": [{
                    "taskId": "quality-sync",
                    "status": "completed",
                    "productionStatus": "completed",
                    "draftReleaseStatus": "pending",
                    "draftCleanupStatus": "release_pending",
                    "qualityStatus": "",
                    "finalOutputPath": str(output),
                    "statusFile": str(status_file),
                    "activeManifestId": "manifest-quality",
                    "activeAttemptId": "attempt-quality",
                }],
            })

            self.assertEqual(1, store.sync_statuses())
            row = store.load_date("9.1")["tasks"][0]
            self.assertEqual("passed", row["qualityStatus"])
            self.assertEqual(str(report), row["qualityReportPath"])
            self.assertEqual(["diagnostic-only"], row["qualityWarnings"])
            manifest = store.create_release_manifest("9.1", {"quality-sync"})
            released = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(["quality-sync"], [
                item["taskId"] for item in released["tasks"]
            ])

    def test_status_sync_keeps_passed_quality_when_release_attempt_reports_none(self):
        """2026-10-10: a release-only attempt reported an empty quality result
        and wiped the recorded "passed", deadlocking the draft release."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = root / "verified.mp4.quality.json"
            report.write_text('{"status":"passed"}', encoding="utf-8")
            status_file = root / "release.status.json"
            status_file.write_text(json.dumps({
                "running": False,
                "manifestId": "manifest-release",
                "attemptId": "attempt-release",
                "tasks": [{
                    "taskId": "quality-keep",
                    "manifestId": "manifest-release",
                    "attemptId": "attempt-release",
                    "state": "Completed",
                    "productionStatus": "completed",
                    "draftReleaseStatus": "pending",
                    "qualityStatus": "",
                    "qualityReportPath": "",
                    "qualityCheckedAt": "",
                    "qualityWarnings": [],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            store = CapCutHandoffStore(root / "handoff", root / "final")
            store.save_date("10.9", {
                "schemaVersion": 3,
                "date": "10.9",
                "tasks": [{
                    "taskId": "quality-keep",
                    "status": "completed",
                    "productionStatus": "completed",
                    "draftReleaseStatus": "pending",
                    "qualityStatus": "passed",
                    "qualityReportPath": str(report),
                    "qualityCheckedAt": "2026-10-09T18:33:00+08:00",
                    "statusFile": str(status_file),
                    "activeManifestId": "manifest-release",
                    "activeAttemptId": "attempt-release",
                }],
            })

            store.sync_statuses()
            row = store.load_date("10.9")["tasks"][0]
            self.assertEqual("passed", row["qualityStatus"])
            self.assertEqual(str(report), row["qualityReportPath"])
            self.assertEqual("2026-10-09T18:33:00+08:00", row["qualityCheckedAt"])

    def test_formal_project_targets_are_fixed(self):
        self.assertEqual(24, production_target("ProjectA"))
        self.assertEqual(13, production_target("ProjectB"))
        self.assertEqual(8, production_target("ProjectC"))
        self.assertEqual(4, production_target("SPC地板"))
        self.assertEqual(4, production_target("SPC墙板"))
        self.assertEqual(0, production_target("测试项目"))

    def test_changing_output_root_reroutes_pending_but_not_completed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.19" / "ProjectA"
            source.mkdir(parents=True)
            audio = root / "ProjectA_碳岩板_英语_001.mp3"
            video = source / "ProjectA_碳岩板_英语_001_0001.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            store = CapCutHandoffStore(root / "交接记录", root / "old-final")
            store.ingest_records("ProjectA", [{
                "status": "success",
                "output": str(video),
                "audio_path": str(audio),
                "language_code": "en",
                "cover": {"material_label": "碳岩板"},
            }])
            changed = store.change_final_output_root(root / "new-final")
            task = store.load_date("7.19")["tasks"][0]
            self.assertEqual(1, changed)
            self.assertEqual("new-final", Path(task["finalOutputPath"]).parents[2].name)

    def test_old_status_file_does_not_overwrite_requeued_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "7.21" / "ProjectB"
            source.mkdir(parents=True)
            audio = root / "ProjectB_净白板_英语_001.mp3"
            video = source / "ProjectB_净白板_英语_001_0001.mp4"
            audio.write_bytes(b"audio")
            video.write_bytes(b"video")
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            store.ingest_records("ProjectB", [{
                "status": "success", "output": str(video), "audio_path": str(audio),
                "language_code": "en", "cover": {"material_label": "净白板"},
            }])
            task = store.load_date("7.21")["tasks"][0]
            old_status = root / "old.status.json"
            old_status.write_text(json.dumps({
                "running": False,
                "tasks": [{"taskId": task["taskId"], "state": "Failed", "error": "old batch"}],
            }), encoding="utf-8")
            payload = store.load_date("7.21")
            payload["tasks"][0].update({"status": "waiting_capcut", "error": "", "statusFile": ""})
            # A separate historical task keeps the old status file visible to
            # the synchronizer. It must not update the new retry attempt.
            payload["tasks"].append({
                "taskId": "historical", "status": "completed", "statusFile": str(old_status),
                "error": "", "selected": False,
            })
            store.save_date("7.21", payload)
            store.sync_statuses()
            refreshed = next(item for item in store.load_date("7.21")["tasks"] if item["taskId"] == task["taskId"])
            self.assertEqual("waiting_capcut", refreshed["status"])
            self.assertEqual("", refreshed["error"])

    def test_waiting_status_with_stale_stop_text_is_runnable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CapCutHandoffStore(root / "handoff", root / "final")
            status = root / "retry.status.json"
            status.write_text(json.dumps({
                "running": False,
                "tasks": [{
                    "taskId": "retry-task",
                    "state": "Waiting",
                    "error": "任务已停止，可再次启动继续",
                }],
            }), encoding="utf-8")
            store.save_date("7.22", {
                "schemaVersion": 1,
                "date": "7.22",
                "tasks": [{
                    "taskId": "retry-task",
                    "status": "failed",
                    "error": "旧失败",
                    "selected": True,
                    "statusFile": str(status),
                }],
            })

            store.sync_statuses()

            refreshed = store.load_date("7.22")["tasks"][0]
            self.assertEqual("waiting_client", refreshed["status"])
            self.assertEqual("", refreshed["error"])
            self.assertFalse(refreshed["selected"])

    def test_failed_status_remains_blocking_after_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CapCutHandoffStore(root / "handoff", root / "final")
            status = root / "failed.status.json"
            status.write_text(json.dumps({
                "running": False,
                "tasks": [{
                    "taskId": "failed-task",
                    "state": "Failed",
                    "error": "独立音频从零点对齐失败",
                }],
            }), encoding="utf-8")
            store.save_date("7.22", {
                "schemaVersion": 1,
                "date": "7.22",
                "tasks": [{
                    "taskId": "failed-task",
                    "status": "waiting_client",
                    "error": "",
                    "selected": False,
                    "statusFile": str(status),
                }],
            })

            store.sync_statuses()

            refreshed = store.load_date("7.22")["tasks"][0]
            self.assertEqual("failed", refreshed["status"])
            self.assertEqual("独立音频从零点对齐失败", refreshed["error"])
            self.assertTrue(refreshed["selected"])

    def test_completed_task_stays_completed_after_verified_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            payload = {
                "schemaVersion": 1,
                "date": "7.21",
                "tasks": [{
                    "taskId": "uploaded-task",
                    "project": "ProjectA",
                    "material": "碳岩板",
                    "language": "印尼语",
                    "videoPath": str(root / "removed-source.mp4"),
                    "audioPath": str(root / "preserved-audio.mp3"),
                    "finalOutputPath": str(root / "removed-after-upload.mp4"),
                    "status": "completed",
                    "error": "",
                }],
            }
            store.save_date("7.21", payload)

            store.update_task("7.21", "uploaded-task", {"selected": False})

            task = store.load_date("7.21")["tasks"][0]
            self.assertEqual("completed", task["status"])
            self.assertEqual("", task["error"])

    def test_proven_invalid_completed_task_is_archived_and_requeued(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CapCutHandoffStore(root / "handoff", root / "final")
            store.save_date("7.22", {
                "schemaVersion": 3,
                "date": "7.22",
                "tasks": [{
                    "taskId": "wall-russian",
                    "project": "SPC墙板",
                    "material": "晶岩墙板",
                    "status": "completed",
                    "selected": False,
                    "finalOutputPath": str(root / "wall-russian.mp4"),
                    "activeManifestId": "old-manifest",
                }],
            })

            self.assertTrue(store.requeue_invalid_completed_task(
                "7.22", "wall-russian", "draft_reopen_identity_mismatch",
                str(root / "quarantine" / "wall-russian.mp4"),
            ))
            payload = store.load_date("7.22")
            task = payload["tasks"][0]
            self.assertEqual("waiting_capcut", task["status"])
            self.assertTrue(task["selected"])
            self.assertEqual("", task["activeManifestId"])
            self.assertEqual(1, len(payload["archivedAttempts"]))
            self.assertEqual("completed", payload["archivedAttempts"][0]["status"])
            self.assertEqual("draft_reopen_identity_mismatch", payload["archivedAttempts"][0]["archiveReason"])

    def test_completed_formal_rows_archive_only_stale_waiting_overproduction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CapCutHandoffStore(root / "handoff", root / "final")
            owner = "inbox-formal"
            tasks = [
                {"taskId": f"done-{index}", "remotePipelineTaskId": owner,
                 "status": "completed", "sequence": index}
                for index in range(1, 6)
            ]
            tasks.extend(
                {"taskId": f"old-{index}", "remotePipelineTaskId": owner,
                 "status": "waiting_capcut", "sequence": index}
                for index in range(1, 14)
            )
            store.save_date("7.22", {"schemaVersion": 1, "date": "7.22", "tasks": tasks})

            self.assertEqual(13, store.archive_excess_pending_remote_tasks("7.22", owner, 5))
            payload = store.load_date("7.22")
            active = [task for task in payload["tasks"] if task.get("remotePipelineTaskId") == owner]
            archived = [task for task in payload["archivedOverproduction"] if task.get("remotePipelineTaskId") == owner]
            self.assertEqual({f"done-{index}" for index in range(1, 6)}, {task["taskId"] for task in active})
            self.assertEqual(13, len(archived))

    def test_partial_completed_cross_date_retry_keeps_one_row_per_formal_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CapCutHandoffStore(root / "handoff", root / "final")
            owner = "inbox-cross-date"
            tasks = [
                {
                    "taskId": "day-one-done",
                    "remotePipelineTaskId": owner,
                    "languageCode": "en",
                    "number": "1",
                    "sequence": 1,
                    "status": "completed",
                    "videoPath": str(root / "7.28" / "ProjectB" / "one.mp4"),
                    "handoffManifest": "formal-one.json",
                },
                {
                    "taskId": "day-one-waiting",
                    "remotePipelineTaskId": owner,
                    "languageCode": "en",
                    "number": "1",
                    "sequence": 2,
                    "status": "waiting_client",
                    "videoPath": str(root / "7.28" / "ProjectB" / "two.mp4"),
                    "handoffManifest": "formal-two.json",
                },
                {
                    "taskId": "next-day-one",
                    "remotePipelineTaskId": owner,
                    "languageCode": "en",
                    "number": "1",
                    "sequence": 1,
                    "status": "waiting_capcut",
                    "videoPath": str(root / "7.29" / "ProjectB" / "one.mp4"),
                },
                {
                    "taskId": "next-day-two",
                    "remotePipelineTaskId": owner,
                    "languageCode": "en",
                    "number": "1",
                    "sequence": 2,
                    "status": "waiting_capcut",
                    "videoPath": str(root / "7.29" / "ProjectB" / "two.mp4"),
                },
            ]
            store.save_date("7.28", {"schemaVersion": 3, "date": "7.28", "tasks": tasks})

            self.assertEqual(2, store.archive_excess_pending_remote_tasks("7.28", owner, 2))

            payload = store.load_date("7.28")
            active = [item for item in payload["tasks"] if item.get("remotePipelineTaskId") == owner]
            self.assertEqual({"day-one-done", "day-one-waiting"}, {item["taskId"] for item in active})
            self.assertEqual(
                {"next-day-one", "next-day-two"},
                {item["taskId"] for item in payload["archivedOverproduction"]},
            )

    def test_remote_owner_migration_does_not_recreate_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CapCutHandoffStore(root / "交接记录", root / "final")
            payload = {
                "schemaVersion": 1,
                "date": "7.21",
                "tasks": [
                    {"taskId": "one", "project": "ProjectA", "remotePipelineTaskId": "child", "status": "completed"},
                    {"taskId": "two", "project": "ProjectA", "remotePipelineTaskId": "child", "status": "waiting_client"},
                ],
            }
            store.save_date("7.21", payload)

            changed = store.reassign_remote_owner("7.21", "child", "parent")

            tasks = store.load_date("7.21")["tasks"]
            self.assertEqual(2, changed)
            self.assertEqual({"one", "two"}, {task["taskId"] for task in tasks})
            self.assertEqual({"parent"}, {task["remotePipelineTaskId"] for task in tasks})
            self.assertTrue(all("expectedTaskCount" not in task for task in tasks))


if __name__ == "__main__":
    unittest.main()
