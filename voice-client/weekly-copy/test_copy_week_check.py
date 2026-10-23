import copy
import unittest

import copy_week_check as checker

RULES = checker.load_rules()
WEEK = "2026-W44"
def english_text(material, number):
    name = RULES["materials"][material]["en"]
    return (f"{name} has a fresh idea number {number} like {name}. This is our {name}. "
            f"It gives idea {number} of {material} a refined decorative look with a practical and convenient installation. "
            f"Perfect for project type {number} of {material}. {RULES['fixedEndings']['英语']}")


def build_week():
    rows = []
    for material in RULES["materials"]:
        for number in range(1, 7):
            en = english_text(material, number)
            for language in checker.languages_for(RULES, material):
                if language == "英语":
                    text = en
                else:
                    intro = RULES["productIntroByLanguage"][material][language]
                    text = f"[{language}] Idee {number} {material}. {intro} Text {number} {material}. {RULES['fixedEndings'][language]}"
                rows.append({
                    "source": f"weekly-{WEEK}", "material": material, "language": language,
                    "copyId": f"{material}-{language}-2026W44-{number}",
                    "title": text.split(".")[0], "text": text,
                })
    return rows


def english_row(rows, material, number):
    return next(r for r in rows if r["copyId"] == f"{material}-英语-2026W44-{number}")


class CopyWeekCheckTests(unittest.TestCase):
    def run_check(self, rows, existing=None):
        return checker.check(rows, RULES["columns"], WEEK, RULES, existing)

    def test_complete_valid_week_is_accepted(self):
        rows = build_week()
        self.assertEqual(528, len(rows))  # 7 materials x 10 langs + 2 x 9 langs, 6 each
        report = self.run_check(rows)
        self.assertTrue(report["accepted"], report["errors"][:5])

    def test_changed_fixed_ending_is_rejected(self):
        rows = build_week()
        rows[0]["text"] = rows[0]["text"].replace("WhatsApp and Email.", "WhatsApp or Email.")
        report = self.run_check(rows)
        self.assertFalse(report["accepted"])
        self.assertTrue(any("结尾必须一字不差" in e for e in report["errors"]))

    def test_missing_entry_and_wrong_count_are_rejected(self):
        rows = build_week()[1:]
        report = self.run_check(rows)
        self.assertTrue(any("应为 6 条" in e for e in report["errors"]))

    def test_banned_word_missing_name_and_exclusive_fact(self):
        rows = build_week()
        en = [r for r in rows if r["language"] == "英语"]
        en[0]["text"] = en[0]["text"].replace("refined", "best")
        en[1]["text"] = en[1]["text"].replace(RULES["materials"][en[1]["material"]]["en"], "panel")
        en[2]["text"] = en[2]["text"].replace("Perfect for", "Lasts 20 years. Perfect for")
        report = self.run_check(rows)
        errors = report["errors"]
        self.assertTrue(any("禁用词" in e for e in errors))
        self.assertTrue(any("没有提到产品英文名" in w for w in report["warnings"]))
        self.assertTrue(any("20 年以上寿命" in e for e in errors))

    def test_reused_old_copy_and_duplicate_hooks_are_rejected(self):
        rows = build_week()
        old = [copy.deepcopy(next(r for r in rows if r["language"] == "英语"))]
        errors = self.run_check(rows, existing=old)["errors"]
        self.assertTrue(any("与旧文案" in e for e in errors))
        rows = build_week()
        en = [r for r in rows if r["language"] == "英语" and r["material"] == "净白板"]
        en[1]["text"] = en[0]["text"]
        en[1]["title"] = en[0]["title"]
        self.assertTrue(any("开头句重复" in e for e in self.run_check(rows)["errors"]))

    def test_untranslated_wrong_language_bad_id_and_header(self):
        rows = build_week()
        de = next(r for r in rows if r["language"] == "德语")
        en = next(r for r in rows if r["language"] == "英语" and r["copyId"].endswith(de["copyId"][-1]) and r["material"] == de["material"])
        de["text"] = en["text"]
        rows.append(dict(rows[0], language="韩语", material="塑木地板", copyId="塑木地板-韩语-2026W44-1"))
        rows[5]["copyId"] = "wrong-id"
        errors = self.run_check(rows)["errors"]
        self.assertTrue(any("结尾必须" in e or "未翻译" in e for e in errors))
        self.assertTrue(any("不应有 韩语" in e for e in errors))
        self.assertTrue(any("copyId 应为" in e for e in errors))
        report = checker.check(rows, ["a", "b"], WEEK, RULES)
        self.assertFalse(report["accepted"])


    def test_misleading_hook_and_forbidden_claim(self):
        rows = build_week()
        row = english_row(rows, "PU石", 5)
        row["text"] = "Have you ever seen real stone that is lightweight? " + row["text"]
        row = english_row(rows, "塑木地板", 1)
        row["text"] = row["text"].replace("Perfect for", "Great outdoor choice. Perfect for")
        errors = self.run_check(rows)["errors"]
        self.assertTrue(any("钩子把产品说成了传统材料" in e for e in errors))
        self.assertTrue(any("不允许写「outdoor」" in e for e in errors))

    def test_plain_stone_hook_from_original_style_is_allowed(self):
        rows = build_week()
        row = english_row(rows, "PU软石", 1)
        row["text"] = "Have you ever seen stone that can bend? " + row["text"]
        self.assertFalse(any("钩子把产品" in e for e in self.run_check(rows)["errors"]))

    def test_repeated_sentence_and_same_opening_across_materials(self):
        rows = build_week()
        for number in (1, 2, 3):
            row = english_row(rows, "晶岩墙板", number)
            row["text"] = row["text"].replace(f"Perfect for project type {number} of 晶岩墙板.", "Perfect for hotels and commercial spaces.")
        for material in list(RULES["materials"])[:5]:
            row = english_row(rows, material, 1)
            row["text"] = f"Most people think this {material} is special. " + row["text"]
        errors = self.run_check(rows)["errors"]
        self.assertTrue(any("同一句话本周用了 3 次" in e for e in errors))
        self.assertTrue(any("种材料的钩子开头相同" in e for e in errors))

    def test_english_product_name_in_localized_language_is_rejected(self):
        rows = build_week()
        row = next(r for r in rows if r["copyId"] == "碳岩板-西班牙语-2026W44-1")
        row["text"] = row["text"].replace(RULES["productIntroByLanguage"]["碳岩板"]["西班牙语"], "Este es nuestro Carbon Crystal Board.")
        self.assertTrue(any("产品名未本地化" in e for e in self.run_check(rows)["errors"]))
        # German keeps the English name in the original library, so it stays allowed.
        self.assertIn("Carbon Crystal Board", RULES["productIntroByLanguage"]["碳岩板"]["德语"])

    def test_prompt_lists_localized_names_and_rotates_phrasings(self):
        import make_prompt
        text = make_prompt.build(WEEK, material="镜面板")
        self.assertIn("Este es nuestro Panel espejo.", text)
        self.assertIn("이것은 저희 미러 패널입니다.", text)
        self.assertNotIn("塑木地板/", text)
        phrasings = {make_prompt.assigned_phrasing(RULES, WEEK, m, "A") for m in RULES["materials"]}
        self.assertGreater(len(phrasings), 1)
        self.assertIn("禁止写：outdoor", make_prompt.build(WEEK, material="塑木地板"))

    def test_exclusive_capability_of_another_material_is_rejected(self):
        rows = build_week()
        row = english_row(rows, "碳岩板", 5)
        row["text"] = "Have you ever seen a marble look that can bend? " + row["text"]
        row = english_row(rows, "晶岩墙板", 5)
        row["text"] = row["text"].replace("Perfect for", "Stone cannot offer this flexibility. Perfect for")
        errors = self.run_check(rows)["errors"]
        self.assertTrue(any("碳岩板" in e and "bend" in e for e in errors))
        self.assertTrue(any("晶岩墙板" in e and "flexibility" in e for e in errors))
        row = english_row(rows, "PU软石", 2)
        row["text"] = row["text"].replace("Perfect for", "It can bend for curved walls. Perfect for")
        self.assertFalse(any(e.startswith("PU软石-英语-2026W44-2") for e in self.run_check(rows)["errors"]))

    def test_week_format(self):
        with self.assertRaises(ValueError):
            checker.week_tag("2026-44")


if __name__ == "__main__":
    unittest.main()
