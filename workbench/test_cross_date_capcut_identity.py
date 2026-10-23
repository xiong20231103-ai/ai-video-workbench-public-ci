import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capcut_handoff import CapCutHandoffStore
from workbench import Workbench, persist_formal_stitch_identity


class CrossDateCapCutIdentityTests(unittest.TestCase):
    def test_formal_identity_is_written_back_to_generation_record(self):
        with tempfile.TemporaryDirectory() as temp:
            record_path = Path(temp) / "generation_records.json"
            original = [{
                "status": "success",
                "output": "E:/generated/8.6/video.mp4",
                "audio_path": "E:/audio/8.5/remote-inbox-old.mp3",
                "remotePipelineTaskId": "inbox-old",
            }]
            record_path.write_text(json.dumps(original), encoding="utf-8")
            formal = [{
                **original[0],
                "remotePipelineTaskId": "inbox-current",
                "remotePipelineTaskIdSource": "formal_handoff",
                "formalTaskIdentity": True,
                "canonicalTaskId": "inbox-current",
                "batchId": "dispatch-current",
                "productionDate": "8.6",
                "expectedTaskCount": 24,
            }]

            self.assertTrue(persist_formal_stitch_identity(record_path, formal))

            saved = json.loads(record_path.read_text(encoding="utf-8"))[0]
            self.assertEqual("inbox-current", saved["remotePipelineTaskId"])
            self.assertEqual("formal_handoff", saved["remotePipelineTaskIdSource"])
            self.assertTrue(saved["formalTaskIdentity"])
            self.assertEqual("8.6", saved["productionDate"])
            self.assertEqual(24, saved["expectedTaskCount"])

    def test_explicit_stale_audio_owner_cannot_overwrite_formal_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video_dir = root / "8.6" / "cloud"
            video_dir.mkdir(parents=True)
            video = video_dir / "video.mp4"
            audio = root / "8.5" / "remote-inbox-old.mp3"
            audio.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            record_path = root / "generation_records.json"
            record_path.write_text("[]", encoding="utf-8")
            store = CapCutHandoffStore(root / "handoff", root / "final")
            base = {
                "created_at": "2026-08-06T12:40:05",
                "status": "success",
                "output": str(video),
                "audio_path": str(audio),
                "language_code": "en",
                "productionDate": "8.6",
                "cover": {"material_label": "carbon"},
            }
            store.ingest_records("cloud", [{
                **base,
                "remotePipelineTaskId": "inbox-current",
                "remotePipelineTaskIdSource": "formal_handoff",
                "formalTaskIdentity": True,
                "canonicalTaskId": "inbox-current",
                "batchId": "dispatch-current",
                "expectedTaskCount": 24,
            }], record_path)
            store.ingest_records("cloud", [{
                **base,
                "remotePipelineTaskId": "inbox-old",
                "expectedTaskCount": 24,
            }], record_path)

            task = store.load_date("8.6")["tasks"][0]
            self.assertEqual("inbox-current", task["remotePipelineTaskId"])
            self.assertEqual("formal_handoff", task["remotePipelineTaskIdSource"])
            self.assertTrue(task["formalTaskIdentity"])
            self.assertEqual("dispatch-current", task["batchId"])

    def test_cross_date_rows_do_not_double_current_owner_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_output = root / "8.5-final.mp4"
            wrong_output = root / "8.6-final.mp4"
            old_output.write_bytes(b"valid")
            wrong_output.write_bytes(b"valid")
            owner = "inbox-old"
            payloads = {
                "8.5": {"tasks": [{
                    "taskId": "old-one",
                    "remotePipelineTaskId": owner,
                    "productionDate": "8.5",
                    "expectedTaskCount": 1,
                    "status": "completed",
                    "finalOutputPath": str(old_output),
                }]},
                "8.6": {"tasks": [{
                    "taskId": "wrong-one",
                    "remotePipelineTaskId": owner,
                    "productionDate": "8.6",
                    "expectedTaskCount": 1,
                    "status": "completed",
                    "finalOutputPath": str(wrong_output),
                }]},
            }
            app = object.__new__(Workbench)
            app.capcut_store = SimpleNamespace(
                dates=lambda: ["8.5", "8.6"],
                load_date=lambda date: payloads[date],
            )
            updates = []
            logs = []
            app._set_remote_pipeline_status = (
                lambda task_id, status, message: updates.append((task_id, status, message)) or True
            )
            app.write_log = logs.append
            app._advance_remote_queue = lambda: None
            app.after = lambda _delay, _callback: None

            with patch("workbench.load_baidu_json", return_value={"tasks": {}}), patch(
                "workbench.load_task_record",
                return_value={"taskId": owner, "productionDate": "8.5"},
            ):
                app._sync_remote_pipeline_from_capcut()

            self.assertEqual(1, len(updates))
            self.assertEqual((owner, "completed"), updates[0][:2])
            self.assertTrue(any("Ignored cross-date CapCut attribution" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
