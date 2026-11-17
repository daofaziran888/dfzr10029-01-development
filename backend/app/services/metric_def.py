"""指标定义 分析看板服务：汇总指标、运行分析并生成模拟结果。

指标值不再随手生成随机数：指标页与图表都以指标发布版本包为唯一取值来源，
这里只返回指标记录本身和它当前生效的发布版本信息。
"""
from __future__ import annotations

from typing import Any

from app.services.metric_release import MetricReleaseService
from app.store import store

MODULE = "metric_def"
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "已定义", "已发布", "已废弃"]
ACTION_RULES = {"提交定义": "已定义", "发布指标": "已发布", "废弃指标": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_results(self) -> list[dict[str, Any]]:
        releases = MetricReleaseService()
        results: list[dict] = []
        for row in store.rows(MODULE):
            d = dict(row)
            snapshot = releases.chart_snapshot(str(row.get("指标编号")))
            d["生效包编号"] = snapshot.get("包编号")
            d["生效口径版本"] = snapshot.get("口径版本")
            d["指标值"] = snapshot.get("指标值")
            d["基准版本"] = snapshot.get("基准版本")
            d["基准指标值"] = snapshot.get("基准指标值")
            d["差异"] = snapshot.get("差异")
            d["口径一致性说明"] = snapshot.get("一致性说明")
            results.append(d)
        return results

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """按指标/粒度/时间窗口对已生效版本包做一次展示汇总，不产生新的口径版本。"""
        metric_code = str(params.get("指标编号") or "").strip()
        releases = MetricReleaseService()
        snapshot = releases.chart_snapshot(metric_code) if metric_code else {}
        rows = store.rows(MODULE)
        matched = [row for row in rows if metric_code and str(row.get("指标编号")) == metric_code]
        if not matched:
            return []
        return [{
            "id": 1,
            "指标编号": metric_code,
            "指标名称": matched[-1].get("指标名称"),
            "统计粒度": params.get("统计粒度") or matched[-1].get("统计粒度"),
            "时间窗口": params.get("时间窗口") or matched[-1].get("时间窗口"),
            "包编号": snapshot.get("包编号"),
            "口径版本": snapshot.get("口径版本"),
            "指标值": snapshot.get("指标值"),
            "基准版本": snapshot.get("基准版本"),
            "基准指标值": snapshot.get("基准指标值"),
            "差异": snapshot.get("差异"),
            "统一口径说明": snapshot.get("统一口径说明"),
            "说明": snapshot.get("message"),
        }]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)
