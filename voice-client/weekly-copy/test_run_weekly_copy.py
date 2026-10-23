import datetime as dt
import unittest

import copy_week_check as checker
import run_weekly_copy as runner

RULES = checker.load_rules()
ENDING = RULES["fixedEndings"]["英语"]


def english(material: str, number: int, text: str) -> dict:
    return {"material": material, "language": "英语", "copyId": f"{material}-英语-2026W44-{number}", "text": text}


class NextWeekTest(unittest.TestCase):
    def test_thursday_generates_the_following_iso_week(self):
        self.assertEqual(runner.next_week(dt.date(2026, 10, 29)), "2026-W45")
        self.assertEqual(runner.next_week(dt.date(2026, 12, 24)), "2026-W53")
        self.assertEqual(runner.next_week(dt.date(2026, 12, 31)), "2027-W01")


class ExtractCsvTest(unittest.TestCase):
    def test_drops_fences_and_chatter_before_header(self):
        header = ",".join(RULES["columns"])
        reply = f"好的，下面是 CSV：\n```csv\n{header}\na,b,c,d,e,f\n```\n"
        self.assertEqual(runner.extract_csv(reply, RULES["columns"]), f"{header}\na,b,c,d,e,f\n")

    def test_missing_header_is_an_error(self):
        with self.assertRaises(ValueError):
            runner.extract_csv("", RULES["columns"])


class BlameTest(unittest.TestCase):
    def test_errors_naming_a_material_go_to_it(self):
        found = runner.blame(["第 4 行 PU石-英语-3：copyId 应为 PU石-英语-2026W44-1～6", "PU软石/德语：应为 6 条，实际 5 条"], [], RULES)
        self.assertEqual(set(found), {"PU石", "PU软石"})

    def test_repeated_sentence_blames_materials_past_the_limit(self):
        line = "This finish can change the whole feeling of a space."
        rows = [english(m, 1, f"{line} Hook. {ENDING}") for m in ["净白板", "碳岩板", "金属板"]]
        error = f"同一句话本周用了 3 次（最多 2 次）：{line.lower()}"
        self.assertEqual(list(runner.blame([error], rows, RULES)), ["金属板"])

    def test_shared_opening_blames_materials_after_the_first_four(self):
        materials = list(RULES["materials"])[:6]
        rows = [english(m, 1, f"Have you ever seen this look? More. {ENDING}") for m in materials]
        error = "6 种材料的钩子开头相同（最多 4 种）：have you ever seen"
        self.assertEqual(list(runner.blame([error], rows, RULES)), materials[4:])


if __name__ == "__main__":
    unittest.main()
