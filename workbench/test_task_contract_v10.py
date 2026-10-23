from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import unittest

from task_contract import (
    assign_batch_contracts,
    canonical_video_identity,
    formal_batch_issues,
    validate_formal_submission,
)


def make_urls(start: int, count: int) -> list[str]:
    base = 7_264_000_000_000_000_000
    return [f"https://www.douyin.com/video/{base + start + offset}" for offset in range(count)]


def make_task(project: str, material: str, count: int, start: int, date: str = "7.28") -> dict:
    return {
        "taskId": f"{project}-{material}-{start}",
        "project": project,
        "material": material,
        "date": date,
        "productionDate": date,
        "expectedCount": count,
        "urls": make_urls(start, count),
    }


def formal_tasks() -> list[dict]:
    # Material splits are intentionally arbitrary. Only each project total is fixed.
    return [
        make_task("ProjectB", "碳岩板", 5, 0),
        make_task("ProjectB", "净白板", 5, 10),
        make_task("ProjectB", "PU石", 1, 20),
        make_task("ProjectB", "木饰面", 2, 30),
        make_task("ProjectA", "碳岩板", 24, 100),
        make_task("ProjectC", "金属板", 8, 200),
        make_task("ProjectD", "碳岩板", 26, 500),
        make_task("SPC地板", "塑木地板", 4, 300),
        make_task("SPC墙板", "晶岩墙板", 4, 400),
    ]


class ContractV10Tests(unittest.TestCase):
    def test_arbitrary_material_split_is_valid_when_project_total_matches(self):
        tasks = formal_tasks()
        valid, reason = validate_formal_submission(tasks)
        self.assertTrue(valid, reason)
        assign_batch_contracts(tasks)
        entity = [item for item in tasks if item["project"] == "ProjectB"]
        self.assertEqual(13, sum(item["expectedCount"] for item in entity))
        self.assertEqual(
            13,
            sum(plan["outputCount"] for item in entity for plan in item["languagePlan"]),
        )
        self.assertTrue(all(plan["code"] == "en" for item in entity for plan in item["languagePlan"]))

    def test_total_79_cannot_hide_wrong_project_total(self):
        tasks = formal_tasks()
        tasks[0]["expectedCount"] = 4
        tasks[0]["urls"] = tasks[0]["urls"][:4]
        tasks[4]["expectedCount"] = 25
        tasks[4]["urls"].append(make_urls(999, 1)[0])
        issues = formal_batch_issues(tasks)
        self.assertTrue(any("ProjectB计划总数必须为 13" in item for item in issues))
        self.assertTrue(any("ProjectA计划总数必须为 24" in item for item in issues))

    def test_real_huang_language_contract_is_exactly_26(self):
        tasks = formal_tasks()
        assign_batch_contracts(tasks)
        task = next(item for item in tasks if item["project"] == "ProjectD")
        plan = {item["code"]: item["outputCount"] for item in task["languagePlan"]}
        self.assertEqual(
            {"es": 4, "pt": 2, "id": 2, "de": 4, "it": 6, "ko": 2, "ru": 2, "en": 4},
            plan,
        )
        self.assertEqual(26, sum(plan.values()))

    def test_each_material_row_requires_exact_link_count(self):
        tasks = formal_tasks()
        tasks[0]["urls"] = tasks[0]["urls"][:-1]
        issues = formal_batch_issues(tasks)
        self.assertTrue(any("有效链接 4 条，计划 5 条" in item for item in issues))

    def test_duplicate_video_id_across_rows_is_rejected(self):
        tasks = formal_tasks()
        tasks[1]["urls"][0] = tasks[0]["urls"][0] + "?share_token=another"
        issues = formal_batch_issues(tasks)
        self.assertTrue(any("重复" in item for item in issues))

    def test_douyin_short_links_remain_supported(self):
        identity, error = canonical_video_identity("https://v.douyin.com/AbCdEfG/")
        self.assertFalse(error)
        self.assertEqual("douyin-url:v.douyin.com/abcdefg", identity)

    def test_mixed_production_dates_are_rejected(self):
        tasks = formal_tasks()
        tasks[-1]["productionDate"] = "7.27"
        issues = formal_batch_issues(tasks)
        self.assertTrue(any("只能使用一个生产日期" in item for item in issues))


def load_submitter_module():
    path = Path(__file__).with_name("company_task_submitter.pyw")
    loader = importlib.machinery.SourceFileLoader("company_task_submitter_v10", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class StatusDateFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.submitter = load_submitter_module()

    def test_empty_current_date_does_not_fall_back_to_history(self):
        items = [{"date": "7.22", "taskId": "old"}]
        result = self.submitter.filter_project_status_items(items, "7.28", "当前生产日期")
        self.assertEqual([], result)

    def test_history_is_only_visible_when_selected(self):
        items = [{"date": "7.22", "taskId": "old"}, {"date": "7.28", "taskId": "new"}]
        result = self.submitter.filter_project_status_items(items, "7.28", "历史任务")
        self.assertEqual(["old"], [item["taskId"] for item in result])


if __name__ == "__main__":
    unittest.main()
