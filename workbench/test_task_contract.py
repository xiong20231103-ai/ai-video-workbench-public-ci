import unittest

from task_contract import assign_batch_contracts, formal_batch_issues, language_plan, validate_formal_batch


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [
            {"project": "ProjectB", "material": "碳岩板", "expectedCount": 5},
            {"project": "ProjectB", "material": "净白板", "expectedCount": 1},
            {"project": "ProjectB", "material": "木饰面", "expectedCount": 5},
            {"project": "ProjectB", "material": "PU石", "expectedCount": 2},
            {"project": "ProjectC", "material": "碳岩板", "expectedCount": 4},
            {"project": "ProjectC", "material": "净白板", "expectedCount": 4},
            {"project": "SPC地板", "material": "塑木地板", "expectedCount": 4},
            {"project": "SPC墙板", "material": "晶岩墙板", "expectedCount": 4},
            {"project": "ProjectD", "material": "碳岩板", "expectedCount": 26},
            {"project": "ProjectA", "material": "碳岩板", "expectedCount": 24},
        ]

    def test_material_counts_are_dynamic_and_formal_total_is_79(self):
        assign_batch_contracts(self.tasks)
        self.assertEqual([5, 1, 5, 2, 4, 4, 4, 4, 26, 24], [task["expectedCount"] for task in self.tasks])
        self.assertEqual((True, ""), validate_formal_batch(self.tasks))
        for task in self.tasks:
            self.assertEqual(task["expectedCount"], sum(row["outputCount"] for row in task["languagePlan"]))

    def test_entity_uses_one_english_audio_per_material(self):
        assign_batch_contracts(self.tasks)
        for task in self.tasks[:4]:
            self.assertEqual(
                [{"code": "en", "name": "英语", "audioCount": 1, "outputCount": task["expectedCount"]}],
                language_plan(task),
            )

    def test_huang_and_spc_use_four_languages_per_material(self):
        assign_batch_contracts(self.tasks)
        for task in self.tasks[4:8]:
            self.assertEqual({"it", "en", "de", "ru"}, {row["code"] for row in language_plan(task)})
            self.assertEqual([1, 1, 1, 1], [row["outputCount"] for row in language_plan(task)])

    def test_cloud_distribution_is_24(self):
        assign_batch_contracts(self.tasks)
        plan = {row["code"]: row["outputCount"] for row in language_plan(self.tasks[-1])}
        self.assertEqual(6, plan["es"])
        self.assertEqual(4, plan["de"])
        self.assertEqual(2, plan["en"])
        self.assertEqual(24, sum(plan.values()))

    def test_real_huang_distribution_is_26(self):
        assign_batch_contracts(self.tasks)
        plan = {row["code"]: row["outputCount"] for row in language_plan(self.tasks[-2])}
        self.assertEqual(
            {"es": 4, "pt": 2, "id": 2, "de": 4, "it": 6, "ko": 2, "ru": 2, "en": 4},
            plan,
        )

    def test_today_distribution_is_not_hard_coded(self):
        tasks = [
            {"project": "ProjectB", "material": "碳岩板", "expectedCount": 3},
            {"project": "ProjectB", "material": "净白板", "expectedCount": 4},
            {"project": "ProjectB", "material": "木饰面", "expectedCount": 6},
        ]
        assign_batch_contracts(tasks)
        self.assertEqual([3, 4, 6], [sum(row["outputCount"] for row in language_plan(task)) for task in tasks])

    def test_formal_total_accepts_a_different_material_split(self):
        tasks = [
            {"project": "ProjectB", "material": "碳岩板", "expectedCount": 4},
            {"project": "ProjectB", "material": "净白板", "expectedCount": 3},
            {"project": "ProjectB", "material": "木饰面", "expectedCount": 4},
            {"project": "ProjectB", "material": "PU石", "expectedCount": 2},
            {"project": "ProjectC", "material": "碳岩板", "expectedCount": 5},
            {"project": "ProjectC", "material": "净白板", "expectedCount": 3},
            {"project": "SPC地板", "material": "塑木地板", "expectedCount": 4},
            {"project": "SPC墙板", "material": "晶岩墙板", "expectedCount": 4},
            {"project": "ProjectD", "material": "碳岩板", "expectedCount": 26},
            {"project": "ProjectA", "material": "碳岩板", "expectedCount": 24},
        ]
        assign_batch_contracts(tasks)
        self.assertEqual((True, ""), validate_formal_batch(tasks))
        self.assertEqual(
            [4, 3, 4, 2, 5, 3, 4, 4, 26, 24],
            [sum(row["outputCount"] for row in language_plan(task)) for task in tasks],
        )

    def test_real_huang_korean_outputs_stay_in_one_material_row(self):
        tasks = [
            {"project": "ProjectB", "material": "碳岩板", "expectedCount": 5},
            {"project": "ProjectB", "material": "净白板", "expectedCount": 1},
            {"project": "ProjectB", "material": "木饰面", "expectedCount": 5},
            {"project": "ProjectB", "material": "PU石", "expectedCount": 2},
            {"project": "ProjectC", "material": "碳岩板", "expectedCount": 4},
            {"project": "ProjectC", "material": "净白板", "expectedCount": 4},
            {"project": "SPC地板", "material": "塑木地板", "expectedCount": 4},
            {"project": "SPC墙板", "material": "晶岩墙板", "expectedCount": 4},
            {"project": "ProjectD", "material": "碳岩板", "expectedCount": 8},
            {"project": "ProjectD", "material": "净白板", "expectedCount": 18},
            {"project": "ProjectA", "material": "碳岩板", "expectedCount": 24},
        ]
        assign_batch_contracts(tasks)
        korean_rows = [
            task for task in tasks
            if any(row["code"] == "ko" for row in language_plan(task))
        ]
        self.assertEqual(1, len(korean_rows))
        korean = {row["code"]: row["outputCount"] for row in language_plan(korean_rows[0])}
        self.assertEqual(2, korean["ko"])
        self.assertEqual(1, [task["material"] for task in korean_rows].count(korean_rows[0]["material"]))

    def test_korean_contract_is_rejected_outside_true_huang(self):
        tasks = [{
            "project": "ProjectA",
            "material": "碳岩板",
            "expectedCount": 1,
            "languagePlan": [{"code": "ko", "name": "韩语", "audioCount": 1, "outputCount": 1}],
            "urls": ["https://www.douyin.com/video/7264784543293459770"],
            "productionDate": "8.2",
        }]
        issues = formal_batch_issues(tasks)
        self.assertTrue(any("韩语只允许用于ProjectD项目" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
