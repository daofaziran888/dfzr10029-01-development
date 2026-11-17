"""指标发布版本包规则测试：公式解析、发布边界、差异明细、阈值与并发去重。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import concurrent.futures
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.formula import FormulaError, build_diff, evaluate_formula, parse_formula, parse_window
from app.services.metric_release import MetricReleaseService
from app.store import store


class FormulaTests(unittest.TestCase):
    def test_parse_formula_collects_structure(self) -> None:
        parsed = parse_formula("SUM(成交金额)/COUNT(订单编号) + 0.5")
        self.assertEqual(parsed["规范公式"], "SUM(成交金额)/COUNT(订单编号)+0.5")
        self.assertEqual(parsed["函数"], ["COUNT", "SUM"])
        self.assertEqual(parsed["引用字段"], ["成交金额", "订单编号"])

    def test_dangerous_formula_rejected(self) -> None:
        for formula in ["__import__('os').system('ls')", "SUM(a, b=1)", "a ** b", "a > b", "a if b else c",
                        "lambda x: x", "[a for a in b]", "SUM(a.b)"]:
            with self.assertRaises(FormulaError):
                parse_formula(formula)

    def test_blank_formula_rejected(self) -> None:
        with self.assertRaises(FormulaError):
            parse_formula("   ")

    def test_evaluate_is_stable(self) -> None:
        kwargs = dict(metric_code="METR-1", granularity="按日", window="2026-09-01",
                      filters="", unit_key="2026-09-01", version_tag="v1")
        first = evaluate_formula("SUM(a)/COUNT(b)", **kwargs)
        second = evaluate_formula("SUM(a)/COUNT(b)", **kwargs)
        self.assertEqual(first, second)

    def test_windows(self) -> None:
        self.assertEqual(parse_window("2026-07~2026-08")[1].isoformat(), "2026-08-31")
        self.assertEqual(parse_window("2026-09-01~2026-09-03")[0].isoformat(), "2026-09-01")
        self.assertEqual(parse_window("2026-09")[1].isoformat(), "2026-09-30")
        self.assertEqual(parse_window("2026-09-01")[0], parse_window("2026-09-01")[1])

    def test_scope_differs_by_recompute_mode(self) -> None:
        _, by_day = build_diff(metric_code="M", metric_name="m", base_formula="SUM(a)/COUNT(b)",
                               new_formula="SUM(a)/COUNT(b)", base_version="v1", new_version="v2",
                               granularity="按月", window="2026-06-01~2026-08-31", filters="",
                               precision=2, mode="按业务日期重算", threshold_rules=None)
        _, by_group = build_diff(metric_code="M", metric_name="m", base_formula="SUM(a)/COUNT(b)",
                                 new_formula="SUM(a)/COUNT(b)", base_version="v1", new_version="v2",
                                 granularity="按月", window="2026-06-01~2026-08-31", filters="",
                                 precision=2, mode="按统计粒度整组重算", threshold_rules=None)
        self.assertEqual(by_day["明细条数"], 30 + 31 + 31)
        self.assertEqual(by_group["明细条数"], 3)

    def test_threshold_marks_out_of_range_with_unified_copy(self) -> None:
        details, summary = build_diff(metric_code="M", metric_name="m",
                                      base_formula="SUM(a)/COUNT(b)",
                                      new_formula="SUM(a)/COUNT(b)+1",
                                      base_version="v1", new_version="v2", granularity="按日",
                                      window="2026-09-01~2026-09-03", filters="", precision=2,
                                      mode="按业务日期重算", threshold_rules=None)
        self.assertTrue(all(item["是否超阈值"] for item in details))
        self.assertTrue(all(item["统一口径说明"].startswith("统一口径说明") for item in details))
        self.assertEqual(summary["超阈值条数"], 3)

    def test_same_formula_has_zero_diff(self) -> None:
        details, summary = build_diff(metric_code="M", metric_name="m",
                                      base_formula="SUM(a)/COUNT(b)",
                                      new_formula="SUM(a)/COUNT(b)",
                                      base_version="v1", new_version="v2", granularity="按日",
                                      window="2026-09-01~2026-09-03", filters="", precision=2,
                                      mode="按业务日期重算", threshold_rules=None)
        self.assertEqual(summary["超阈值条数"], 0)
        self.assertTrue(all(item["差异"] == 0 for item in details))


class ReleaseServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        store._tables["metric_release"] = []
        self.service = MetricReleaseService()

    def _metric_id(self, code: str) -> int:
        row = next(row for row in store.rows("metric_def") if row["指标编号"] == code)
        return int(row["id"])

    def _create(self, code: str, **values):
        payload = {"来源记录id": self._metric_id(code), "统计粒度": "按日",
                   "时间窗口": "2026-09-01~2026-09-03", "重算方式": "按业务日期重算"}
        payload.update(values)
        return self.service.create_release(payload)

    def test_package_keeps_sources_and_release_boundary(self) -> None:
        result = self._create("METR-0001", 计算公式="COUNT(user_id)+COUNT(device_id)")
        self.assertTrue(result["ok"], result.get("message"))
        package = result["package"]
        # METR-0001 是草稿指标，首次发布从 v1 起步，基准为“无历史版本”。
        self.assertEqual(package["口径版本"], "v1")
        self.assertEqual(package["基准版本"], "无历史版本")
        self.assertEqual(package["差异条数"], 3)
        self.assertTrue(package["发布边界"])
        source_types = [item["来源类型"] for item in package["来源输入"]]
        self.assertIn("指标草稿记录", source_types)
        self.assertIn("指标草稿记录（无历史主版本）", source_types)
        self.assertEqual(package["公式快照"]["计算公式"], "COUNT(user_id)+COUNT(device_id)")

    def test_diff_reads_snapshot_not_live_metric(self) -> None:
        result = self._create("METR-0001", 计算公式="COUNT(user_id)+COUNT(device_id)")
        release_id = result["package"]["id"]
        # 版本包生成后再改指标草稿，不应改变已生成版本包的差异口径。
        draft = next(row for row in store.rows("metric_def")
                     if row["指标编号"] == "METR-0001" and row["status"] != "已发布")
        draft["计算公式"] = "COUNT(user_id)*10"
        diff = self.service.get_diff(release_id)
        self.assertEqual(diff["发布版本"]["计算公式"], "COUNT(user_id)+COUNT(device_id)")
        self.assertEqual(diff["核对方式"], "读取指标发布版本包快照，与主版本逐项核对")
        self.assertTrue(all(field["核对项"] for field in diff["字段核对"]))

    def test_dedup_returns_single_valid_result(self) -> None:
        first = self._create("METR-0001")
        second = self._create("METR-0001")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["package"]["id"], second["package"]["id"])
        self.assertEqual(len(store.rows("metric_release")), 1)

    def test_conflict_boundary_rejects_competing_formula(self) -> None:
        self._create("METR-0001", 计算公式="COUNT(user_id)+1")
        result = self._create("METR-0001", 计算公式="COUNT(user_id)+2")
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflict"])
        self.assertEqual(len(store.rows("metric_release")), 1)

    def test_recompute_mode_gets_its_own_detail_scope(self) -> None:
        first = self._create("METR-0001", 统计粒度="按月", 时间窗口="2026-06-01~2026-08-31",
                             重算方式="按业务日期重算")
        second = self._create("METR-0001", 统计粒度="按月", 时间窗口="2026-06-01~2026-08-31",
                              重算方式="按统计粒度整组重算")
        self.assertTrue(second["ok"], second.get("message"))
        self.assertNotEqual(first["package"]["差异条数"], second["package"]["差异条数"])
        self.assertNotEqual(first["package"]["发布边界"], second["package"]["发布边界"])

    def test_concurrent_identical_submission_keeps_one_package(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self._create("METR-0001"), range(16)))
        ids = {result["package"]["id"] for result in results if result.get("package")}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(store.rows("metric_release")), 1)

    def test_publish_deprecates_previous_and_chart_is_consistent(self) -> None:
        first = self._create("METR-0001")
        self.assertTrue(self.service.publish_release(first["package"]["id"])["ok"])
        chart = self.service.chart_snapshot("METR-0001")
        self.assertTrue(chart["has_value"])
        self.assertEqual(chart["口径版本"], "v1")
        self.assertEqual(chart["包编号"], first["package"]["包编号"])
        self.assertEqual(chart["一致性说明"], "口径版本与指标值来自同一指标发布版本包")

        # 发布新版本后旧包废弃，图表只读新包。
        draft = next(row for row in store.rows("metric_def") if row["指标编号"] == "METR-0001")
        draft["计算公式"] = "COUNT(user_id)+COUNT(device_id)"
        second = self._create("METR-0001")
        self.assertEqual(second["package"]["口径版本"], "v2")
        self.assertEqual(second["package"]["基准版本"], "v1")
        self.assertEqual(second["package"]["主版本包编号"], first["package"]["包编号"])
        self.assertTrue(self.service.publish_release(second["package"]["id"])["ok"])
        self.assertEqual(self.service.get_release(first["package"]["id"])["status"], "已废弃")
        chart = self.service.chart_snapshot("METR-0001")
        self.assertEqual(chart["包编号"], second["package"]["包编号"])
        self.assertEqual(chart["基准版本"], "v1")

    def test_published_metric_formula_goes_to_new_draft(self) -> None:
        published = next(row for row in store.rows("metric_def") if row["指标编号"] == "METR-0003")
        original = published["计算公式"]
        entry, message, parsed = self.service.update_metric_formula(
            published["id"], {"计算公式": "SUM(成交金额)/COUNT(订单编号)+SUM(优惠金额)/COUNT(订单编号)"})
        self.assertIsNotNone(entry)
        self.assertIn("新草稿", message)
        self.assertNotEqual(entry["id"], published["id"])
        self.assertEqual(published["计算公式"], original)
        self.assertTrue(parsed["函数"])
        again = self.service.update_metric_formula(entry["id"], {"计算公式": "SUM(成交金额)+0.1"})
        self.assertEqual(again[0]["id"], entry["id"])

    def test_sources_trace_back_to_records(self) -> None:
        result = self._create("METR-0003")
        sources = self.service.get_sources(result["package"]["id"])
        self.assertTrue(sources["关联来源记录"])
        for item in sources["关联来源记录"]:
            self.assertTrue(item["是否仍存在"])
            self.assertIsNotNone(item["当前记录"])
            self.assertTrue(item["来源路径"].startswith("/api/"))

    def test_missing_metric_and_bad_formula(self) -> None:
        missing = self.service.create_release({"指标编号": "METR-9999"})
        self.assertFalse(missing["ok"])
        bad = self._create("METR-0001", 计算公式="__import__('os')")
        self.assertFalse(bad["ok"])
        self.assertIn("计算公式", bad["message"])


if __name__ == "__main__":
    unittest.main()
