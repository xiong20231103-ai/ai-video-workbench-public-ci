import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("board_video_stitcher.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("board_video_stitcher_new_first", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _Segment:
    def __init__(self, path):
        self.path = str(path)


class NewMaterialFirstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "木纹"
        self.folder.mkdir()
        self.history = self.root / "history.json"
        self.logs = []

    def app(self):
        app = MODULE.BoardVideoStitcherApp.__new__(MODULE.BoardVideoStitcherApp)
        app.ai_usage_history_path = lambda: self.history
        app._queue_log = self.logs.append
        app._log = self.logs.append
        return app

    def add(self, *names):
        paths = []
        for name in names:
            path = self.folder / name
            path.write_bytes(b"x")
            paths.append(path)
        return paths

    def files(self):
        return sorted(self.folder.glob("*.mp4"))

    def run_picks(self, count):
        """One generation run: load history, pick `count`, save history."""
        app = self.app()
        usage = app.load_ai_usage_history()
        picks = [app.choose_global_ai_file(_Segment(self.folder), self.files(), usage).name for _ in range(count)]
        app.save_ai_usage_history(usage)
        return picks

    def test_existing_library_is_baseline_not_prioritised(self):
        self.add("old1.mp4", "old2.mp4", "old3.mp4")
        picks = self.run_picks(3)
        self.assertEqual({"old1.mp4", "old2.mp4", "old3.mp4"}, set(picks))  # normal round
        self.assertFalse(any("新加入" in line for line in self.logs))

    def test_new_files_are_used_before_any_old_file_then_mix_resumes(self):
        self.add(*(f"old{i}.mp4" for i in range(6)))
        self.run_picks(1)  # establishes the baseline; one old file now used this round
        self.add("new1.mp4", "new2.mp4")
        picks = self.run_picks(4)
        self.assertEqual({"new1.mp4", "new2.mp4"}, set(picks[:2]))
        self.assertTrue(all(name.startswith("old") for name in picks[2:]))
        self.assertTrue(any("新加入" in line for line in self.logs))

    def test_new_files_stay_new_across_runs_until_used(self):
        self.add("old1.mp4", "old2.mp4")
        self.run_picks(1)
        self.add("new1.mp4", "new2.mp4", "new3.mp4")
        first = self.run_picks(1)
        second = self.run_picks(2)
        self.assertEqual({"new1.mp4", "new2.mp4", "new3.mp4"}, set(first + second))

    def test_round_reset_and_clear_do_not_make_old_files_new_again(self):
        self.add("old1.mp4", "old2.mp4")
        self.run_picks(2)
        app = self.app()
        with mock.patch.object(MODULE.messagebox, "askyesno", return_value=True), \
                mock.patch.object(MODULE.messagebox, "showinfo"):
            app.clear_ai_usage_history()  # UI clear without a prior load
        data = json.loads(self.history.read_text(encoding="utf-8"))
        self.assertEqual({}, data["folders"])
        self.assertEqual(2, len(next(iter(data["seen"].values()))))
        self.logs.clear()
        self.run_picks(3)  # completes a round and starts a new one
        self.assertFalse(any("新加入" in line for line in self.logs))

    def test_version_1_history_still_loads(self):
        old = self.add("old1.mp4", "old2.mp4")
        key = MODULE.BoardVideoStitcherApp.normalized_media_path(old[0])
        folder_key = MODULE.BoardVideoStitcherApp.normalized_media_path(self.folder)
        self.history.write_text(json.dumps({"version": 1, "folders": {folder_key: [key]}}), encoding="utf-8")
        picks = self.run_picks(1)
        self.assertEqual(["old2.mp4"], picks)  # existing round state respected
        self.assertEqual(2, json.loads(self.history.read_text(encoding="utf-8"))["version"])

    def test_avoids_repeating_previous_when_more_than_one_new(self):
        self.add("old1.mp4")
        self.run_picks(1)
        new = self.add("new1.mp4", "new2.mp4")
        app = self.app()
        usage = app.load_ai_usage_history()
        with mock.patch.object(MODULE.random, "choice", side_effect=lambda seq: seq[0]):
            pick = app.choose_global_ai_file(_Segment(self.folder), self.files(), usage, previous=str(new[0]))
        self.assertEqual("new2.mp4", pick.name)


if __name__ == "__main__":
    unittest.main()
