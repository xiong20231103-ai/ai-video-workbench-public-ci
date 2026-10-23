import hashlib
import tempfile
import unittest
from pathlib import Path

from voice_guardrails import validate_voice_handoff


class VoiceGuardrailTests(unittest.TestCase):
    def _rules(self, root):
        path = root / "rules.csv"
        path.write_text("language,voice\nGerman,Ben\nRussian,Analyst\n", encoding="utf-8")
        return path

    def _item(self, root, language="German", voice="Ben", task_id="task-1", content=b"audio"):
        audio = root / f"{language}.mp3"
        audio.write_bytes(content)
        return {
            "canonicalTaskId": task_id,
            "language": language,
            "audioPath": str(audio),
            "audioSha256": hashlib.sha256(content).hexdigest(),
            "expectedVoice": voice,
            "actualActiveVoice": voice,
            "voiceVerified": True,
            "voiceVerification": {
                "matchingSources": ["header", "rightCard"],
                "conflictingSources": [],
            },
        }

    def test_matching_contract_passes_without_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = validate_voice_handoff([self._item(root)], task_id="task-1", rules_file=self._rules(root))
            self.assertTrue(result["ok"])
            self.assertFalse(result["modelCalled"])

    def test_wrong_active_voice_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = self._item(root)
            item["actualActiveVoice"] = "Analyst"
            result = validate_voice_handoff([item], task_id="task-1", rules_file=self._rules(root))
            self.assertFalse(result["ok"])
            self.assertIn("active_voice_mismatch", {entry["code"] for entry in result["failures"]})

    def test_audio_hash_is_mandatory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = self._item(root)
            item.pop("audioSha256")
            result = validate_voice_handoff([item], task_id="task-1", rules_file=self._rules(root))
            self.assertFalse(result["ok"])
            self.assertIn("voice_audio_hash_missing", {entry["code"] for entry in result["failures"]})

    def test_same_audio_reused_for_two_languages_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self._item(root, content=b"same")
            second = self._item(root, language="Russian", voice="Analyst", content=b"same")
            result = validate_voice_handoff([first, second], task_id="task-1", rules_file=self._rules(root))
            self.assertFalse(result["ok"])
            self.assertIn("voice_audio_reused_across_languages", {entry["code"] for entry in result["failures"]})


if __name__ == "__main__":
    unittest.main()
