"""图表配置业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.metric_release import MetricReleaseService

MODULE = "chart"
LIST_FIELDS = ["图表编号", "图表名称", "图表类型", "数据来源", "坐标字段", "聚合方式", "颜色方案", "图表状态"]
REQUIRED_FIELDS = ["图表编号", "图表名称", "图表类型"]
STATUS_ORDER = ["草稿", "已配置", "已发布", "已停用"]
ACTION_RULES = {"保存配置": "已配置", "发布图表": "已发布", "停用图表": "已停用"}
NEGATIVE_ACTIONS = ["停用图表"]


class ChartService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("图表编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"图表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于图表配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"图表已{action}"

    def metric_values(self, entry_id: int) -> dict[str, Any] | None:
        """图表刷新取值：只读取数据来源指标的一份完整已发布版本包。

        口径版本与指标值同包同发布边界返回，图表不会读到新口径配旧基准的值。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        metric_code = str(entry.get("数据来源") or "").strip()
        snapshot = MetricReleaseService().chart_snapshot(metric_code) if metric_code else {
            "ok": False,
            "has_value": False,
            "指标编号": None,
            "指标值": None,
            "口径版本": None,
            "message": "图表未配置指标数据来源",
        }
        return {
            "图表id": entry.get("id"),
            "图表编号": entry.get("图表编号"),
            "图表名称": entry.get("图表名称"),
            "数据来源": metric_code,
            "指标取值": snapshot,
            "口径版本": snapshot.get("口径版本"),
            "指标值": snapshot.get("指标值"),
            # 只有指标值来自已发布版本包时，口径版本与指标值才算同源可对账。
            "指标值口径版本一致": bool(snapshot.get("has_value") and snapshot.get("包编号")),
            "刷新说明": snapshot.get("message"),
        }
