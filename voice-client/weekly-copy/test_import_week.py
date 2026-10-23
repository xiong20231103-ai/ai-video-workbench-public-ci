import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import import_week

MONDAY_W44 = dt.date(2026, 10, 26)
HEADER = "source,material,language,copyId,title,text\n"


class ImportWeekTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = Path(self.temp.name) / "inbox"
        self.weekly = Path(self.temp.name) / "weekly"
        self.inbox.mkdir()

    def drop(self, name, body):
        path = self.inbox / name
        path.write_text(HEADER + body, encoding="utf-8")
        return path

    def run_import(self, week, path, today, accepted=True):
        report = {"accepted": accepted, "errors": [] if accepted else ["PU石-英语-2026W45-1 用了 bend"], "warnings": [], "rows": 528}
        with patch.object(import_week.checker, "check", return_value=report) as check:
            result = import_week.import_week(week, path, weekly=self.weekly, today=today, rules={}, existing=[])
        return result, check

    def test_accepted_week_is_written_with_its_hash(self):
        path = self.drop("weekly-2026-W45.csv", "a\n")
        result, _ = self.run_import("2026-W45", path, MONDAY_W44)
        self.assertEqual(result, "imported")
        record = json.loads((self.weekly / "weekly-2026-W45.accepted.json").read_text(encoding="utf-8"))
        self.assertEqual(record["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual((self.weekly / "weekly-2026-W45.csv").read_bytes(), path.read_bytes())
        self.assertEqual(self.run_import("2026-W45", path, MONDAY_W44)[0], "unchanged")

    def test_rejected_week_is_not_written_and_not_rechecked(self):
        path = self.drop("weekly-2026-W45.csv", "bad\n")
        self.assertEqual(self.run_import("2026-W45", path, MONDAY_W44, accepted=False)[0], "rejected")
        self.assertFalse((self.weekly / "weekly-2026-W45.csv").exists())
        result, check = self.run_import("2026-W45", path, MONDAY_W44, accepted=False)
        self.assertEqual(result, "rejected-before")
        check.assert_not_called()

    def test_week_in_use_is_never_replaced_but_a_future_week_can_be(self):
        first = self.drop("weekly-2026-W44.csv", "one\n")
        self.run_import("2026-W44", first, MONDAY_W44 - dt.timedelta(days=4))
        second = self.drop("weekly-2026-W44 (1).csv", "two\n")
        self.assertEqual(self.run_import("2026-W44", second, MONDAY_W44)[0], "refused-in-use")
        self.assertEqual((self.weekly / "weekly-2026-W44.csv").read_bytes(), first.read_bytes())
        self.assertEqual(self.run_import("2026-W44", second, MONDAY_W44 - dt.timedelta(days=1))[0], "imported")

    def test_past_weeks_are_ignored(self):
        path = self.drop("weekly-2026-W43.csv", "old\n")
        self.assertEqual(self.run_import("2026-W43", path, MONDAY_W44)[0], "skipped-past")

    def test_newest_taildrop_copy_wins(self):
        older = self.drop("weekly-2026-W45.csv", "old\n")
        newer = self.drop("weekly-2026-W45 (1).csv", "new\n")
        import os
        os.utime(older, (1, 1))
        self.drop("notes.csv", "x\n")
        self.assertEqual(import_week.candidates(self.inbox), {"2026-W45": newer})

    def test_iso_week_matches_company_calendar(self):
        self.assertEqual(import_week.iso_week(dt.date(2026, 10, 20)), "2026-W43")
        self.assertEqual(import_week.iso_week(dt.date(2027, 1, 1)), "2026-W53")


if __name__ == "__main__":
    unittest.main()
