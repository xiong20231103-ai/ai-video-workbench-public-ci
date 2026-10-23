import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from baidu_upload import (
    PROJECT_DIRS,
    UploadCoordinator,
    atomic_write_json,
    is_final_mp4,
    load_json,
    remote_path_for,
    save_config,
)


class AtomicStateWriteTests(unittest.TestCase):
    def test_atomic_write_retries_transient_windows_permission_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "state.json"
            real_replace = os.replace
            attempts = []

            def flaky_replace(source, destination):
                attempts.append((source, destination))
                if len(attempts) < 3:
                    raise PermissionError(5, "temporarily busy")
                return real_replace(source, destination)

            with patch("baidu_upload.os.replace", side_effect=flaky_replace), patch(
                "baidu_upload.time.sleep"
            ):
                atomic_write_json(target, {"ok": True})

            self.assertEqual({"ok": True}, load_json(target, {}))
            self.assertEqual(3, len(attempts))
            self.assertEqual([], list(Path(temp_dir).glob("*.tmp")))


class MemorySecrets:
    def __init__(self):
        self.value = {
            "app_key": "public-key",
            "secret_key": "secret",
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_at": int(time.time()) + 99999,
        }

    def load(self):
        return dict(self.value)

    def save(self, payload):
        self.value = dict(payload)

    def update(self, **changes):
        self.value.update(changes)
        return dict(self.value)


class FakeClient:
    cloud_files = {}

    def __init__(self, _store, _timeout):
        pass

    def upload_file(self, path, remote_path, progress=None, resume=None):
        progress(
            {
                "uploadId": "resume-id",
                "size": path.stat().st_size,
                "chunkSize": 32 * 1024 * 1024,
                "blockMd5s": ["abc"],
                "uploadedParts": [0],
                "vipType": 2,
                "account": "测试账号",
            }
        )
        result = {
            "fsId": "123",
            "path": remote_path,
            "size": path.stat().st_size,
            "md5": "abc",
            "vipType": 2,
            "chunkSize": 32 * 1024 * 1024,
            "account": "测试账号",
        }
        self.cloud_files["123"] = {
            "fs_id": 123,
            "path": remote_path,
            "size": path.stat().st_size,
        }
        return result

    def file_metadata(self, fs_id):
        if str(fs_id) not in self.cloud_files:
            raise RuntimeError("cloud file not found")
        return dict(self.cloud_files[str(fs_id)])


class UploadTests(unittest.TestCase):
    def test_project_remote_paths(self):
        for project, folder in PROJECT_DIRS.items():
            task = {
                "project": project,
                "date": "7.20",
                "finalOutputPath": rf"E:\7.20\{folder}\成片.mp4",
            }
            self.assertEqual(
                f"/apps/AI视频生产线/7.20/{folder}/成片.mp4",
                remote_path_for(task, "AI视频生产线"),
            )

    def test_remote_path_uses_discovered_local_path_filename(self):
        task = {
            "project": "ProjectA",
            "date": "7.17",
            "localPath": r"E:\7.17\ProjectA\ProjectA_碳岩板_德语_004_01.mp4",
        }
        self.assertEqual(
            "/apps/AI视频生产线/7.17/ProjectA/ProjectA_碳岩板_德语_004_01.mp4",
            remote_path_for(task, "AI视频生产线"),
        )

    def test_mp4_validation_rejects_non_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = root / "audio.mp3"
            other.write_bytes(b"x" * 2048)
            self.assertFalse(is_final_mp4(other)[0])
            video = root / "video.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 2048)
            self.assertTrue(is_final_mp4(video)[0])

    def test_completed_capcut_task_uploads_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "records"
            state = root / "state.json"
            config = root / "config.json"
            video = root / "final.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            atomic_write_json(
                records / "7.20" / "第四步到第五步.json",
                {
                    "date": "7.20",
                    "tasks": [
                        {
                            "taskId": "task-1",
                            "date": "7.20",
                            "project": "ProjectA",
                            "material": "碳岩板",
                            "language": "英语",
                            "finalOutputPath": str(video),
                            "status": "completed",
                        }
                    ],
                },
            )
            save_config(
                {
                    "enabled": True,
                    "app_name": "AI视频生产线",
                    "records_root": str(records),
                    "state_file": str(state),
                    "stable_seconds": 0,
                    "stable_checks": 1,
                },
                config,
            )
            coordinator = UploadCoordinator(
                config,
                secret_store=MemorySecrets(),
                client_factory=FakeClient,
            )
            coordinator.run_once()
            coordinator.run_once()
            task = coordinator.state["tasks"]["task-1"]
            self.assertEqual("uploaded", task["status"])
            self.assertEqual(100, task["progress"])
            self.assertEqual("123", task["fsId"])
            self.assertTrue(task["cleanup"]["cloudVerifiedAt"])
            self.assertEqual(video.stat().st_size, task["cleanup"]["cloudVerifiedSize"])
            self.assertEqual("legacy_retained", task["cleanup"]["state"])
            self.assertTrue(video.is_file())
            self.assertEqual(1, coordinator.summary()["completed"])
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual("uploaded", saved["tasks"]["task-1"]["status"])

    def test_worker_recovers_completed_capcut_status_without_workbench(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "records"
            state = root / "state.json"
            config = root / "config.json"
            capcut_status = root / "batch.status.json"
            video = root / "final.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            atomic_write_json(
                capcut_status,
                {"tasks": [{"taskId": "task-offline", "state": "Completed"}]},
            )
            atomic_write_json(
                records / "7.20" / "第四步到第五步.json",
                {
                    "date": "7.20",
                    "tasks": [
                        {
                            "taskId": "task-offline",
                            "date": "7.20",
                            "project": "ProjectB",
                            "material": "净白板",
                            "language": "英语",
                            "finalOutputPath": str(video),
                            "status": "waiting_client",
                            "statusFile": str(capcut_status),
                        }
                    ],
                },
            )
            save_config(
                {
                    "enabled": True,
                    "records_root": str(records),
                    "state_file": str(state),
                    "stable_seconds": 0,
                    "stable_checks": 1,
                },
                config,
            )
            coordinator = UploadCoordinator(
                config,
                secret_store=MemorySecrets(),
                client_factory=FakeClient,
            )
            coordinator.run_once()
            coordinator.run_once()
            self.assertEqual(
                "uploaded", coordinator.state["tasks"]["task-offline"]["status"]
            )

    def test_historical_manifest_is_retained_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, state, config = root / "records", root / "state.json", root / "config.json"
            status = root / "capcut.status.json"
            final = root / "final.mp4"; temporary = root / "final.capcut-temp.mp4"
            source = root / "source.mp4"; audio = root / "voice.mp3"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            temporary.write_bytes(b"temporary"); source.write_bytes(b"source"); audio.write_bytes(b"audio")
            atomic_write_json(status, {"tasks": [{
                "taskId": "cleanup-cloud", "state": "Completed",
                "temporaryOutputPath": str(temporary),
            }]})
            atomic_write_json(records / "7.20" / "第四步到第五步.json", {"date": "7.20", "tasks": [{
                "taskId": "cleanup-cloud", "date": "7.20", "project": "ProjectA", "material": "碳岩板",
                "language": "英语", "videoPath": str(source), "audioPath": str(audio),
                "finalOutputPath": str(final), "status": "waiting_client", "statusFile": str(status),
            }]})
            save_config({"enabled": True, "records_root": str(records), "state_file": str(state), "stable_seconds": 0, "stable_checks": 1}, config)
            coordinator = UploadCoordinator(config, secret_store=MemorySecrets(), client_factory=FakeClient)
            coordinator.run_once(); coordinator.run_once()
            item = coordinator.state["tasks"]["cleanup-cloud"]
            self.assertTrue(source.exists()); self.assertTrue(temporary.exists()); self.assertTrue(final.exists())
            self.assertTrue(audio.exists())
            self.assertIsNone(item["cleanup"].get("sourceVideoPathState"))
            self.assertIsNone(item["cleanup"].get("temporaryOutputPathState"))
            self.assertEqual("permanent_keep", item["cleanup"]["audioPathState"])
            self.assertEqual("legacy_retained", item["cleanup"]["state"])

    def test_historical_cloud_verification_never_enables_cleanup_or_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            config = root / "config.json"
            final = root / "legacy-final.mp4"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            save_config({"enabled": True, "state_file": str(state)}, config)
            coordinator = UploadCoordinator(
                config,
                secret_store=MemorySecrets(),
                client_factory=FakeClient,
            )
            FakeClient.cloud_files["legacy-fs"] = {
                "fs_id": "legacy-fs",
                "path": "/apps/AI视频生产线/7.20/ProjectA/legacy-final.mp4",
                "size": final.stat().st_size,
            }
            coordinator.state["tasks"] = {
                "legacy-task": {
                    "taskId": "legacy-task",
                    "status": "uploaded",
                    "completedAt": "2026-07-20T12:00:00+08:00",
                    "cleanupPolicyVersion": 0,
                    "localPath": str(final),
                    "fsId": "legacy-fs",
                    "remotePath": "/apps/AI视频生产线/7.20/ProjectA/legacy-final.mp4",
                    "cloudSize": final.stat().st_size,
                    "cleanup": {"state": "legacy_retained"},
                }
            }

            result = coordinator.verify_historical_cloud_records(FakeClient(None, 1))

            item = coordinator.state["tasks"]["legacy-task"]
            self.assertEqual(1, result["verified"])
            self.assertEqual(0, item["cleanupPolicyVersion"])
            self.assertEqual("legacy_retained", item["cleanup"]["state"])
            self.assertTrue(item["cleanup"]["cloudVerifiedAt"])
            self.assertEqual(
                "verified", item["cleanup"]["historyVerification"]["state"]
            )
            self.assertTrue(final.exists())

    def test_historical_cloud_verification_size_mismatch_preserves_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            config = root / "config.json"
            final = root / "legacy-final.mp4"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            save_config({"enabled": True, "state_file": str(state)}, config)
            coordinator = UploadCoordinator(
                config,
                secret_store=MemorySecrets(),
                client_factory=FakeClient,
            )
            FakeClient.cloud_files["wrong-size-fs"] = {
                "fs_id": "wrong-size-fs",
                "path": "/apps/AI视频生产线/7.20/ProjectA/legacy-final.mp4",
                "size": final.stat().st_size + 1,
            }
            coordinator.state["tasks"] = {
                "legacy-task": {
                    "taskId": "legacy-task",
                    "status": "uploaded",
                    "completedAt": "2026-07-20T12:00:00+08:00",
                    "cleanupPolicyVersion": 0,
                    "localPath": str(final),
                    "fsId": "wrong-size-fs",
                    "remotePath": "/apps/AI视频生产线/7.20/ProjectA/legacy-final.mp4",
                    "cloudSize": final.stat().st_size,
                    "cleanup": {"state": "legacy_retained"},
                }
            }

            result = coordinator.verify_historical_cloud_records(FakeClient(None, 1))

            item = coordinator.state["tasks"]["legacy-task"]
            self.assertEqual(1, result["failed"])
            self.assertFalse(item["cleanup"].get("cloudVerifiedAt"))
            self.assertEqual(
                "cloud_size_mismatch",
                item["cleanup"]["historyVerification"]["errorCode"],
            )
            self.assertEqual("legacy_retained", item["cleanup"]["state"])
            self.assertTrue(final.exists())

    def test_historical_cloud_request_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            config = root / "config.json"
            request = root / "historical-request.json"
            status = root / "historical-status.json"
            final = root / "legacy-final.mp4"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            save_config({"enabled": True, "state_file": str(state)}, config)
            FakeClient.cloud_files["request-fs"] = {
                "fs_id": "request-fs",
                "path": "/apps/AI视频生产线/7.20/ProjectA/legacy-final.mp4",
                "size": final.stat().st_size,
            }
            coordinator = UploadCoordinator(
                config,
                secret_store=MemorySecrets(),
                client_factory=FakeClient,
                historical_verify_request_path=request,
                historical_verify_status_path=status,
            )
            coordinator.state["configured"] = True
            coordinator.state["authorized"] = True
            coordinator.state["tasks"] = {
                "legacy-task": {
                    "taskId": "legacy-task",
                    "status": "uploaded",
                    "completedAt": "2026-07-20T12:00:00+08:00",
                    "cleanupPolicyVersion": 0,
                    "localPath": str(final),
                    "fsId": "request-fs",
                    "remotePath": "/apps/AI视频生产线/7.20/ProjectA/legacy-final.mp4",
                    "cloudSize": final.stat().st_size,
                    "cleanup": {"state": "legacy_retained"},
                }
            }
            atomic_write_json(
                request,
                {"commandId": "verify-one", "requestedAt": "now", "taskIds": []},
            )

            first = coordinator.process_historical_cloud_verification_request()
            second = coordinator.process_historical_cloud_verification_request()

            self.assertEqual("completed", first["state"])
            self.assertEqual(first, second)
            self.assertEqual("verify-one", second["commandId"])
            self.assertTrue(final.exists())

    def test_unconfirmed_cloud_performs_zero_local_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / "state.json"; config = root / "config.json"
            final = root / "final.mp4"; source = root / "source.mp4"; temporary = root / "temporary.mp4"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            source.write_bytes(b"source"); temporary.write_bytes(b"temporary")
            save_config({"enabled": True, "state_file": str(state)}, config)
            coordinator = UploadCoordinator(config, secret_store=MemorySecrets(), client_factory=FakeClient)
            coordinator.state["tasks"] = {"done": {
                "taskId": "done", "date": "7.20", "status": "uploaded", "completedAt": "now",
                "cleanupPolicyVersion": 2, "canonicalTaskId": "done-owner", "expectedTaskCount": 1,
                "fsId": "missing", "cloudSize": final.stat().st_size,
                "localPath": str(final), "sourceVideoPath": str(source),
                "temporaryOutputPath": str(temporary), "cleanup": {},
            }}
            coordinator._apply_post_upload_cleanup(FakeClient(None, 1))
            self.assertTrue(final.exists()); self.assertTrue(source.exists()); self.assertTrue(temporary.exists())
            self.assertEqual("blocked_cloud_lookup_failed", coordinator.state["tasks"]["done"]["cleanup"]["state"])

    def test_baidu_sync_directory_file_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / "config.json"
            sync_root = root / "BaiduNetdisk"; sync_root.mkdir()
            final = sync_root / "final.mp4"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            save_config({"enabled": True, "state_file": str(root / "state.json")}, config)
            coordinator = UploadCoordinator(config, secret_store=MemorySecrets(), client_factory=FakeClient)
            FakeClient.cloud_files["sync"] = {"fs_id": "sync", "path": "/apps/final.mp4", "size": final.stat().st_size}
            coordinator.state["tasks"] = {"sync": {
                "taskId": "sync", "date": "7.20", "status": "uploaded", "completedAt": "now",
                "cleanupPolicyVersion": 2, "canonicalTaskId": "sync-owner", "expectedTaskCount": 1,
                "fsId": "sync", "cloudSize": final.stat().st_size,
                "remotePath": "/apps/final.mp4", "localPath": str(final), "cleanup": {},
            }}
            coordinator._apply_post_upload_cleanup(FakeClient(None, 1))
            self.assertTrue(final.exists())
            self.assertEqual("blocked_candidate_preflight", coordinator.state["tasks"]["sync"]["cleanup"]["state"])
            self.assertIn(
                "localPath:baidu_sync_directory",
                coordinator.state["tasks"]["sync"]["cleanup"]["blockedReasons"],
            )

    def test_candidate_preflight_blocker_causes_zero_deletions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root / "config.json"
            sync_root = root / "BaiduNetdisk"; sync_root.mkdir()
            final = sync_root / "final.mp4"
            source = root / "registered-stitched.mp4"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            source.write_bytes(b"source")
            save_config({"enabled": True, "state_file": str(root / "state.json")}, config)
            coordinator = UploadCoordinator(
                config, secret_store=MemorySecrets(), client_factory=FakeClient
            )
            FakeClient.cloud_files["atomic-preflight"] = {
                "fs_id": "atomic-preflight",
                "path": "/apps/final.mp4",
                "size": final.stat().st_size,
            }
            coordinator.state["tasks"] = {"atomic-preflight": {
                "taskId": "atomic-preflight", "date": "7.20",
                "cleanupPolicyVersion": 2, "canonicalTaskId": "owner",
                "expectedTaskCount": 1, "status": "uploaded", "completedAt": "now",
                "fsId": "atomic-preflight", "cloudSize": final.stat().st_size,
                "remotePath": "/apps/final.mp4", "localPath": str(final),
                "sourceVideoPath": str(source), "cleanup": {},
            }}
            coordinator._apply_post_upload_cleanup(FakeClient(None, 1))
            self.assertTrue(final.exists())
            self.assertTrue(source.exists())
            self.assertEqual(
                "blocked_candidate_preflight",
                coordinator.state["tasks"]["atomic-preflight"]["cleanup"]["state"],
            )

    def test_v2_cleanup_deletes_registered_task_files_and_keeps_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            output = root / "output"; output.mkdir()
            prepared_root = root / "prepared-samples"; prepared_root.mkdir()
            final = output / "final.mp4"
            temporary = output / "final.capcut-temp.mp4"
            working = output / "final.first-frame-working.mp4"
            source = root / "stitched.mp4"
            prepared = prepared_root / "task-1000ms.mp4"
            audio = root / "voice.mp3"
            for path in (temporary, working, source, prepared, audio):
                path.write_bytes(path.name.encode("utf-8"))
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
            save_config({"enabled": True, "state_file": str(root / "state.json")}, config)
            coordinator = UploadCoordinator(
                config, secret_store=MemorySecrets(), client_factory=FakeClient
            )
            FakeClient.cloud_files["v2"] = {
                "fs_id": "v2", "path": "/apps/final.mp4", "size": final.stat().st_size
            }
            coordinator.state["tasks"] = {"v2": {
                "taskId": "v2", "cleanupPolicyVersion": 2,
                "canonicalTaskId": "owner", "expectedTaskCount": 1,
                "status": "uploaded", "completedAt": "now", "fsId": "v2",
                "cloudSize": final.stat().st_size, "remotePath": "/apps/final.mp4",
                "localPath": str(final), "sourceVideoPath": str(source),
                "temporaryOutputPath": str(temporary),
                "postProcessWorkingPath": str(working),
                "preparedSamplePath": str(prepared), "audioPath": str(audio),
                "subtitleSourcePaths": [], "cleanup": {},
            }}
            coordinator._apply_post_upload_cleanup(FakeClient(None, 1))
            self.assertFalse(final.exists())
            self.assertFalse(temporary.exists())
            self.assertFalse(working.exists())
            self.assertFalse(source.exists())
            self.assertFalse(prepared.exists())
            self.assertTrue(audio.exists())
            cleanup = coordinator.state["tasks"]["v2"]["cleanup"]
            self.assertEqual("cleanup_completed", cleanup["state"])
            self.assertEqual("permanent_keep", cleanup["audioPathState"])

    def test_shared_source_waits_for_last_verified_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            save_config({"enabled": True, "state_file": str(root / "state.json")}, config)
            coordinator = UploadCoordinator(
                config, secret_store=MemorySecrets(), client_factory=FakeClient
            )
            shared = root / "subtitle.mp4"; shared.write_bytes(b"shared")
            tasks = {}
            for index in (1, 2):
                output = root / f"output-{index}"; output.mkdir()
                final = output / "final.mp4"
                final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes([index]) * 4096)
                fs_id = f"shared-{index}"
                FakeClient.cloud_files[fs_id] = {
                    "fs_id": fs_id, "path": f"/apps/{index}.mp4", "size": final.stat().st_size
                }
                tasks[str(index)] = {
                    "taskId": str(index), "cleanupPolicyVersion": 2,
                    "canonicalTaskId": "owner", "expectedTaskCount": 2,
                    "status": "uploaded" if index == 1 else "waiting_validation",
                    "completedAt": "now" if index == 1 else "",
                    "fsId": fs_id, "cloudSize": final.stat().st_size,
                    "remotePath": f"/apps/{index}.mp4", "localPath": str(final),
                    "subtitleSourcePaths": [str(shared)], "cleanup": {},
                }
            coordinator.state["tasks"] = tasks
            coordinator._apply_post_upload_cleanup(FakeClient(None, 1))
            self.assertTrue(shared.exists())
            self.assertEqual("waiting_shared_release", tasks["1"]["cleanup"]["state"])
            tasks["2"]["status"] = "uploaded"; tasks["2"]["completedAt"] = "now"
            coordinator._apply_post_upload_cleanup(FakeClient(None, 1))
            self.assertFalse(shared.exists())
            self.assertTrue(all(
                item["cleanup"]["state"] == "cleanup_completed"
                for item in tasks.values()
            ))


if __name__ == "__main__":
    unittest.main()
