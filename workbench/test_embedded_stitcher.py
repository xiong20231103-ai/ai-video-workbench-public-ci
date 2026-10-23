import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from embedded_stitcher import EmbeddedStitcherController, StitchContractValidationError


class _Var:
    def __init__(self, value=None):
        self.value = value

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class EmbeddedStitcherAudioHandoffTests(unittest.TestCase):
    def test_missing_korean_contract_is_structured_and_never_falls_back_to_english(self):
        controller = EmbeddedStitcherController.__new__(EmbeddedStitcherController)
        controller._task_contract = {
            "canonicalTaskId": "true-huang-wood",
            "expectedCount": 2,
            "languagePlan": [{"code": "ko", "outputCount": 2}],
        }
        controller.app = SimpleNamespace(
            language_tasks=[
                SimpleNamespace(
                    language_code="en",
                    audio_path="true-huang-wood_english.mp3",
                    count=1,
                )
            ]
        )

        with self.assertRaises(StitchContractValidationError) as raised:
            controller._apply_task_contract_counts()

        self.assertEqual("STITCH_LANGUAGE_CONFIG_MISSING", raised.exception.error_code)
        self.assertEqual(["ko"], raised.exception.missing_languages)
        self.assertEqual(0, controller.app.language_tasks[0].count)

    def test_source_handoff_is_runtime_only_and_does_not_save_legacy_path(self):
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "subtitle_removed"
            subject.mkdir()
            app = SimpleNamespace(old_folder_var=_Var(), scan_segments=lambda: None)
            controller = EmbeddedStitcherController.__new__(EmbeddedStitcherController)
            controller.app = app

            controller.apply_handoff(subject)

            self.assertEqual(str(subject), app.old_folder_var.value)

    def test_remote_source_handoff_applies_an_explicit_whitelist(self):
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "subtitle_removed"
            subject.mkdir()
            video = subject / "verified_no_subtitle.mp4"
            video.write_bytes(b"video")
            app = SimpleNamespace(
                old_folder_var=_Var(),
                scan_segments=lambda: None,
                segment_file_whitelists={},
            )
            controller = EmbeddedStitcherController.__new__(EmbeddedStitcherController)
            controller.app = app
            controller.module = SimpleNamespace(STANDARD_SEGMENT_NAMES=("01_AI素材", "02_老素材"))

            controller.apply_handoff(subject, [video])

            self.assertEqual((str(video.resolve()),), app.segment_file_whitelists["02_老素材"])
            self.assertTrue(app.segment_file_whitelist_required)

    def test_manual_source_handoff_disables_strict_manifest_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            subject = Path(temp) / "manual"
            subject.mkdir()
            app = SimpleNamespace(
                old_folder_var=_Var(),
                scan_segments=lambda: None,
                segment_file_whitelists={"02_老素材": ("stale.mp4",)},
                segment_file_whitelist_required=True,
            )
            controller = EmbeddedStitcherController.__new__(EmbeddedStitcherController)
            controller.app = app

            controller.apply_handoff(subject)

            self.assertEqual({}, app.segment_file_whitelists)
            self.assertFalse(app.segment_file_whitelist_required)

    def test_remote_audio_folder_replaces_legacy_material_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "木饰面" / "7.22"
            folder.mkdir(parents=True)
            saved = []
            project = {
                "audio_material_root": str(Path(temp) / "木纹"),
                "audio_folder": "",
                "audio_batch": "",
                "audio_auto_latest": True,
            }
            app = SimpleNamespace(
                audio_material_root_var=_Var(),
                audio_task_material_root=None,
                audio_batch_var=_Var(),
                audio_auto_latest_var=_Var(),
                audio_folder_var=_Var(),
                active_project_name="ProjectB木饰面",
                projects={"ProjectB木饰面": project},
                save_projects=lambda: saved.append(True),
            )
            controller = EmbeddedStitcherController.__new__(EmbeddedStitcherController)
            controller.app = app

            controller.apply_audio_handoff(folder, "7.22")

            self.assertEqual(app.audio_material_root_var.value, str(folder.parent))
            self.assertEqual(app.audio_folder_var.value, str(folder))
            self.assertEqual(app.audio_batch_var.value, "7.22")
            self.assertFalse(app.audio_auto_latest_var.value)
            self.assertEqual(project["audio_material_root"], str(folder.parent))
            self.assertEqual(project["audio_folder"], str(folder))
            self.assertEqual(saved, [True])

    def test_missing_audio_folder_stops_before_scan(self):
        controller = EmbeddedStitcherController.__new__(EmbeddedStitcherController)
        controller.app = SimpleNamespace()
        with self.assertRaises(FileNotFoundError):
            controller.apply_audio_handoff(Path("Z:/missing/audio"), "7.22")

    def test_unattended_validation_cannot_replace_submitted_production_date(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "7.28" / "ProjectB"
            calls = []
            app = SimpleNamespace(
                output_dir=_Var(""),
                language_tasks=[],
                refresh_language_tasks=lambda **_kwargs: None,
                refresh_audio_batches=lambda *args, **kwargs: None,
                apply_automatic_output_dir=lambda: calls.append("legacy-now"),
                start_generate=lambda: (
                    app.apply_automatic_output_dir(),
                    calls.append(app.output_dir.get()),
                    setattr(app, "worker", SimpleNamespace(is_alive=lambda: True)),
                ),
                worker=None,
            )
            controller = EmbeddedStitcherController.__new__(EmbeddedStitcherController)
            controller.app = app
            controller._task_contract = {}
            controller._acquire_stitch_lock = lambda: None
            controller._release_stitch_lock = lambda: None
            controller._apply_task_contract_counts = lambda: None
            controller.apply_task_contract({
                "pipelineProfile": {
                    "4_stitch": {"outputDirectoryRule": str(output)}
                }
            })

            self.assertTrue(controller.start_unattended())
            self.assertEqual([str(output)], calls)
            self.assertEqual(str(output), app.output_dir.get())


if __name__ == "__main__":
    unittest.main()
