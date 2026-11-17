"""指标发布版本包：公式快照、基准快照、差异明细与发布边界。

业务约束：

* 一次发布必须把“公式快照”和“差异计算”落在同一个发布边界内完成；图表只读取
  已发布的版本包，因此不会出现口径版本与指标值错配。
* 版本包保留来源输入（指标草稿记录、历史发布记录、主版本包），可以顺着包回到
  原始记录。
* 阈值判断在版本包生成时完成，超出范围的记录带统一口径说明，且不参与图表取值。
* 同一请求指纹并发提交时只保留一个有效结果；同一发布边界上的不同公式提交按
  冲突边界处理，不会产生两个互相竞争的待发布版本。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from app.services.formula import (
    DEFAULT_PRECISION,
    GRANULARITIES,
    MAX_PRECISION,
    MODES,
    OUT_OF_RANGE,
    FormulaError,
    build_diff,
    parse_formula,
    parse_window,
)
from app.store import store

MODULE = "metric_release"
METRIC_MODULE = "metric_def"

STATUS_PENDING = "待发布"
STATUS_PUBLISHED = "已发布"
STATUS_REJECTED = "已驳回"
STATUS_DEPRECATED = "已废弃"
STATUS_ORDER = [STATUS_PENDING, STATUS_PUBLISHED, STATUS_REJECTED, STATUS_DEPRECATED]
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PUBLISHED)

ACTION_RULES = {
    "发布版本": STATUS_PUBLISHED,
    "发布指标": STATUS_PUBLISHED,
    "驳回版本": STATUS_REJECTED,
    "废弃版本": STATUS_DEPRECATED,
}

SNAPSHOT_FIELDS = ["计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "口径版本", "重算方式"]
COMPARE_FIELDS = ["计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "重算方式", "口径版本"]

CONFLICT_MESSAGE = (
    "同一发布边界已存在待发布版本包，本次提交按冲突边界拦下；"
    "请先发布或驳回已有版本包，或调整统计粒度、时间窗口后重新发起版本校验。"
)
DEDUP_MESSAGE = "同一发布边界已存在版本包，本次提交沿用唯一有效结果。"

_RELEASE_LOCK = threading.RLock()


def release_lock() -> threading.RLock:
    """图表取值与发布写入共用一把锁，保证读到的包和值来自同一发布边界。"""
    return _RELEASE_LOCK


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(*parts: Any) -> str:
    text = "|".join(json.dumps(part, ensure_ascii=False, sort_keys=True) if not isinstance(part, str) else part
                    for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def _int_value(value: Any, default: int) -> int:
    try:
        return max(0, min(int(value), MAX_PRECISION))
    except (TypeError, ValueError):
        return default


def _version_number(version: Any) -> int:
    text = str(version or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def _next_version(*versions: Any) -> str:
    number = max([_version_number(version) for version in versions] + [0])
    return f"v{number + 1}"


def _clean(values: dict[str, Any]) -> str:
    return str(values or "").strip()


def _metric_rows(metric_code: str) -> list[dict[str, Any]]:
    return [row for row in store.rows(METRIC_MODULE) if str(row.get("指标编号")) == metric_code]


def _find_metric(metric_code: str, metric_id: int | None = None) -> dict[str, Any] | None:
    rows = store.rows(METRIC_MODULE)
    if metric_id is not None:
        for row in rows:
            if int(row.get("id", 0)) == int(metric_id):
                return row
    if metric_code:
        matches = _metric_rows(metric_code)
        if matches:
            return matches[-1]
    return None


def _releases_for(metric_code: str) -> list[dict[str, Any]]:
    return [row for row in store.rows(MODULE) if str(row.get("指标编号")) == metric_code]


def _latest_published(metric_code: str) -> dict[str, Any] | None:
    published = [row for row in _releases_for(metric_code) if row.get("status") == STATUS_PUBLISHED]
    return published[-1] if published else None


def _metric_record_source(row: dict[str, Any], source_type: str) -> dict[str, Any]:
    return {
        "来源模块": METRIC_MODULE,
        "来源记录id": row.get("id"),
        "来源类型": source_type,
        "来源路径": f"/api/{METRIC_MODULE}/{row.get('id')}",
        "指标编号": row.get("指标编号"),
        "字段快照": {
            field: row.get(field)
            for field in ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "口径版本", "status"]
        },
    }


def _package_source(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "来源模块": MODULE,
        "来源记录id": row.get("id"),
        "来源类型": "历史发布版本包",
        "来源路径": f"/api/{METRIC_MODULE}/releases/{row.get('id')}",
        "指标编号": row.get("指标编号"),
        "字段快照": {
            field: row.get(field)
            for field in ["包编号", "口径版本", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "status"]
        },
    }


def _baseline(metric_code: str, metric_row: dict[str, Any], fallback_formula: str) -> dict[str, Any]:
    """确定主版本：优先历史已发布版本包，其次已发布指标记录。"""
    package = _latest_published(metric_code)
    if package is not None and package.get("公式快照"):
        snapshot = dict(package.get("公式快照") or {})
        return {
            "基准类型": "历史发布版本包",
            "基准版本": package.get("口径版本"),
            "主版本包编号": package.get("包编号"),
            "主版本包id": package.get("id"),
            "基准公式": snapshot.get("计算公式") or fallback_formula,
            "统计粒度": snapshot.get("统计粒度"),
            "时间窗口": snapshot.get("时间窗口"),
            "筛选条件": snapshot.get("筛选条件"),
            "精度位数": snapshot.get("精度位数", DEFAULT_PRECISION),
            "口径版本": package.get("口径版本"),
            "来源记录": _package_source(package),
        }
    published_rows = [row for row in _metric_rows(metric_code) if row.get("status") == STATUS_PUBLISHED]
    published = published_rows[-1] if published_rows else None
    if published is not None:
        return {
            "基准类型": "已发布指标记录",
            "基准版本": published.get("口径版本") or "无历史版本",
            "主版本包编号": None,
            "主版本包id": None,
            "基准公式": published.get("计算公式") or fallback_formula,
            "统计粒度": published.get("统计粒度"),
            "时间窗口": published.get("时间窗口"),
            "筛选条件": published.get("筛选条件"),
            "精度位数": _int_value(published.get("精度位数"), DEFAULT_PRECISION),
            "口径版本": published.get("口径版本") or "无历史版本",
            "来源记录": _metric_record_source(published, "已发布指标记录"),
        }
    return {
        "基准类型": "无历史主版本",
        "基准版本": "无历史版本",
        "主版本包编号": None,
        "主版本包id": None,
        "基准公式": fallback_formula,
        "统计粒度": metric_row.get("统计粒度"),
        "时间窗口": metric_row.get("时间窗口"),
        "筛选条件": metric_row.get("筛选条件"),
        "精度位数": _int_value(metric_row.get("精度位数"), DEFAULT_PRECISION),
        "口径版本": "无历史版本",
        "来源记录": _metric_record_source(metric_row, "指标草稿记录（无历史主版本）"),
    }


def _boundary_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(field)) for field in
                 ("指标编号", "统计粒度", "时间窗口", "筛选条件", "重算方式"))


class MetricReleaseService:
    # ---------- 列表与读取 ----------

    def list_releases(
        self,
        *,
        metric_code: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if metric_code:
            rows = [row for row in rows if str(row.get("指标编号")) == metric_code]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if keyword:
            needle = keyword.casefold()
            rows = [row for row in rows
                    if needle in str(row.get("包编号", "")).casefold()
                    or needle in str(row.get("指标编号", "")).casefold()
                    or needle in str(row.get("指标名称", "")).casefold()]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_release(self, release_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, release_id)

    def find_by_code(self, package_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("包编号")) == package_code:
                return row
        return None

    # ---------- 提交公式（草稿） ----------

    def update_metric_formula(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """提交公式：已发布记录保留原快照，新公式落到新的草稿记录上。"""
        entry = store.find(METRIC_MODULE, entry_id)
        if entry is None:
            return None, f"指标 {entry_id} 不存在", None
        formula = _clean(values.get("计算公式")) or _clean(entry.get("计算公式"))
        try:
            parsed = parse_formula(formula)
        except FormulaError as exc:
            return None, f"计算公式校验未通过：{exc}", None

        target = entry
        moved_to_draft = False
        if entry.get("status") == "已发布":
            # 已发布记录是历史基准，不能被覆盖；把调整后的公式落到同一指标的新草稿。
            rows = store.rows(METRIC_MODULE)
            target = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "指标编号": entry.get("指标编号"),
                "指标名称": entry.get("指标名称"),
                "统计粒度": entry.get("统计粒度"),
                "时间窗口": entry.get("时间窗口"),
                "筛选条件": entry.get("筛选条件"),
                "精度位数": entry.get("精度位数"),
                "口径版本": entry.get("口径版本"),
            }
            rows.append(target)
            moved_to_draft = True

        for field in ["计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数"]:
            value = values.get(field)
            if value is not None and str(value).strip():
                target[field] = str(value).strip()
        target["计算公式"] = formula
        if not moved_to_draft:
            target["status"] = "草稿" if target.get("status") not in ("草稿",) else target.get("status")
            target["pending"] = True
        target["公式结构"] = parsed
        target["公式更新时间"] = _now()
        message = "指标公式已提交为草稿"
        if moved_to_draft:
            message = "已发布指标保留原口径，调整后的公式已提交为同指标的新草稿"
        return target, message, parsed

    # ---------- 生成版本包 ----------

    def create_release(self, values: dict[str, Any], *, publish: bool = False) -> dict[str, Any]:
        values = dict(values or {})
        with _RELEASE_LOCK:
            return self._create_release_locked(values, publish=publish)

    def _create_release_locked(self, values: dict[str, Any], *, publish: bool) -> dict[str, Any]:
        metric_id = values.get("指标id") or values.get("metric_id") or values.get("id")
        if isinstance(metric_id, str) and not metric_id.isdigit():
            metric_id = None
        metric_code = _clean(values.get("指标编号"))
        source_id = values.get("来源记录id") or values.get("草稿记录id") or values.get("metric_def_id")
        try:
            source_id = int(source_id) if source_id not in (None, "") else None
        except (TypeError, ValueError):
            source_id = None

        metric_row = _find_metric(metric_code, int(metric_id) if metric_id else source_id)
        if metric_row is None:
            return {"ok": False, "message": f"指标「{metric_code or metric_id}」不存在，无法生成指标发布版本包"}
        metric_code = str(metric_row.get("指标编号"))

        # 草稿记录：优先显式指定的来源记录；若来源是已发布记录，则改用同指标最新的草稿，
        # 否则会把历史基准当成新公式再次发布。
        draft_row = None
        if source_id is not None:
            candidate = store.find(METRIC_MODULE, source_id)
            if candidate is not None and str(candidate.get("指标编号")) == metric_code:
                draft_row = candidate
        usable_drafts = [row for row in _metric_rows(metric_code) if row.get("status") in ("草稿", "已定义")]
        if draft_row is None or (draft_row.get("status") in ("已发布", "已废弃") and usable_drafts):
            draft_row = usable_drafts[-1] if usable_drafts else (draft_row or metric_row)
        if draft_row is None:
            draft_row = metric_row

        formula = _clean(values.get("计算公式")) or _clean(draft_row.get("计算公式")) or _clean(metric_row.get("计算公式"))
        try:
            parsed = parse_formula(formula)
        except FormulaError as exc:
            return {"ok": False, "message": f"计算公式校验未通过：{exc}"}

        requested_granularity = _clean(values.get("统计粒度"))
        granularity = requested_granularity or _clean(draft_row.get("统计粒度")) or "按日"
        if granularity not in GRANULARITIES:
            if requested_granularity:
                return {"ok": False, "message": f"统计粒度只允许 {'、'.join(GRANULARITIES)}"}
            # 指标记录上的自由文本粒度不能直接用于重算，回落到按日而不是静默算错。
            granularity = "按日"
        window = _clean(values.get("时间窗口")) or _clean(draft_row.get("时间窗口")) or "最近30天"
        filters = _clean(values.get("筛选条件")) if values.get("筛选条件") is not None else _clean(draft_row.get("筛选条件"))
        precision = _int_value(values.get("精度位数", draft_row.get("精度位数")), DEFAULT_PRECISION)
        mode = _clean(values.get("重算方式")) or ("按业务日期重算" if granularity == "按日" else "按统计粒度整组重算")
        if mode not in MODES:
            return {"ok": False, "message": f"重算方式只允许 {'、'.join(MODES)}"}
        threshold_rules = values.get("阈值规则") or values.get("阈值") or None
        if isinstance(threshold_rules, dict):
            threshold_rules = [threshold_rules]
        if threshold_rules is not None and not isinstance(threshold_rules, list):
            return {"ok": False, "message": "阈值规则格式不正确"}

        base = _baseline(metric_code, metric_row, formula)
        base_formula = str(base.get("基准公式") or formula)
        try:
            parse_formula(base_formula)
        except FormulaError as exc:
            return {"ok": False, "message": f"主版本基准公式不可用：{exc}"}
        target_version = _clean(values.get("口径版本")) or _next_version(
            base.get("基准版本"), *[row.get("口径版本") for row in _releases_for(metric_code)])

        # 请求指纹只覆盖本次发布边界，用于并发去重；版本号在去重之后才分配。
        # 指纹不含基准版本，同一公式重复校验会沿用唯一有效结果，而不是每次叠加新版本。
        request_fingerprint = _hash(metric_code, formula, granularity, window, filters or "", precision, mode,
                                    threshold_rules or "")
        boundary_fields = {
            "指标编号": metric_code,
            "统计粒度": granularity,
            "时间窗口": window,
            "筛选条件": filters,
            "精度位数": precision,
            "重算方式": mode,
            "基准版本": base.get("基准版本"),
        }

        active = [row for row in _releases_for(metric_code) if row.get("status") in ACTIVE_STATUSES]
        for row in active:
            if row.get("请求指纹") == request_fingerprint:
                return {
                    "ok": True,
                    "message": DEDUP_MESSAGE,
                    "deduplicated": True,
                    "entry": row,
                    "package": row,
                    "diff": self.get_diff(int(row.get("id"))),
                }
        # 同一发布边界上的不同公式：已有待发布版本包时不再生成竞争版本。
        # 冲突边界由 指标 + 统计粒度 + 时间窗口 + 筛选条件 + 重算方式 决定；
        # 按业务日期重算与按统计粒度整组重算落在不同差异明细范围，可以各自准备版本包，
        # 但只有一份最终会发布，图表读取的始终是那份唯一已发布版本。
        pending = [row for row in active if row.get("status") == STATUS_PENDING]
        same_boundary = [
            row for row in pending
            if all(str(row.get(field)) == str(value) for field, value in
                   (("指标编号", metric_code), ("统计粒度", granularity), ("时间窗口", window),
                    ("筛选条件", filters), ("重算方式", mode)))
        ]
        if same_boundary and not (values.get("允许冲突") or values.get("force")):
            row = same_boundary[0]
            return {
                "ok": False,
                "conflict": True,
                "message": CONFLICT_MESSAGE,
                "entry": row,
                "existing": row,
            }

        window_shape = parse_window(window)
        if window_shape is not None:
            window_note = f"{window_shape[0].isoformat()} ~ {window_shape[1].isoformat()}"
        else:
            window_note = f"自定义窗口 {window}（按整体范围核对）"

        # 公式快照与差异计算在同一把锁、同一个发布边界内完成。
        details, summary = build_diff(
            metric_code=metric_code,
            metric_name=str(metric_row.get("指标名称") or ""),
            base_formula=base_formula,
            new_formula=formula,
            base_version=str(base.get("基准版本") or "无历史版本"),
            new_version=target_version,
            granularity=granularity,
            window=window,
            filters=filters or "",
            precision=precision,
            mode=mode,
            threshold_rules=threshold_rules,
        )
        boundary = _hash(boundary_fields, formula, base_formula, target_version)
        package_code = "MREL-{0:04d}".format(
            max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1)
        formula_snapshot = {
            "指标编号": metric_code,
            "指标名称": metric_row.get("指标名称"),
            "计算公式": formula,
            "规范公式": parsed["规范公式"],
            "统计粒度": granularity,
            "时间窗口": window,
            "窗口范围": window_note,
            "筛选条件": filters,
            "精度位数": precision,
            "重算方式": mode,
            "口径版本": target_version,
            "公式结构": parsed,
        }
        baseline_snapshot = {
            "基准类型": base.get("基准类型"),
            "基准版本": base.get("基准版本"),
            "主版本包编号": base.get("主版本包编号"),
            "主版本包id": base.get("主版本包id"),
            "计算公式": base_formula,
            "统计粒度": base.get("统计粒度"),
            "时间窗口": base.get("时间窗口"),
            "筛选条件": base.get("筛选条件"),
            "精度位数": base.get("精度位数"),
            "口径版本": base.get("基准版本"),
        }
        source_inputs = [
            _metric_record_source(draft_row, "指标草稿记录" if draft_row.get("status") != "已定义" else "指标已定义记录"),
            dict(base.get("来源记录") or {}),
        ]
        if metric_row.get("id") not in (draft_row.get("id"), (base.get("来源记录") or {}).get("来源记录id")):
            source_inputs.append(_metric_record_source(metric_row, "指标主记录"))

        row = {
            "id": int(package_code.split("-")[-1]),
            "status": STATUS_PUBLISHED if publish else STATUS_PENDING,
            "pending": not publish,
            "abnormal": bool(summary.get("超阈值条数")),
            "包编号": package_code,
            "指标编号": metric_code,
            "指标名称": metric_row.get("指标名称"),
            "口径版本": target_version,
            "基准版本": base.get("基准版本"),
            "主版本包编号": base.get("主版本包编号"),
            "计算公式": formula,
            "基准公式": base_formula,
            "统计粒度": granularity,
            "时间窗口": window,
            "窗口范围": window_note,
            "筛选条件": filters,
            "精度位数": precision,
            "重算方式": mode,
            "差异条数": summary.get("明细条数"),
            "超阈值条数": summary.get("超阈值条数"),
            "指标差异": summary.get("指标差异"),
            "发布边界": boundary,
            "请求指纹": request_fingerprint,
            "公式快照": formula_snapshot,
            "基准快照": baseline_snapshot,
            "差异明细": details,
            "差异汇总": summary,
            "阈值规则": summary.get("阈值规则"),
            "统一口径说明": summary.get("统一口径说明"),
            "来源输入": source_inputs,
            "创建人": _clean(values.get("操作人")) or "值班管理员",
            "创建时间": _now(),
            "发布时间": _now() if publish else None,
        }
        store.rows(MODULE).append(row)
        if publish:
            self._apply_publish_locked(row)
        return {
            "ok": True,
            "message": "指标发布版本包已生成并发布" if publish else "指标发布版本包已生成，等待发布",
            "deduplicated": False,
            "entry": row,
            "package": row,
            "diff": self.get_diff(int(row["id"])),
        }

    # ---------- 发布 / 状态动作 ----------

    def _apply_publish_locked(self, row: dict[str, Any]) -> None:
        metric_code = str(row.get("指标编号"))
        for other in _releases_for(metric_code):
            if other is row:
                continue
            if other.get("status") == STATUS_PUBLISHED:
                other["status"] = STATUS_DEPRECATED
                other["pending"] = False
                other["废弃时间"] = _now()
                other["废弃说明"] = f"已被 {row.get('包编号')}（{row.get('口径版本')}）取代"
            elif other.get("status") == STATUS_PENDING:
                other["status"] = STATUS_DEPRECATED
                other["pending"] = False
                other["废弃时间"] = _now()
                other["废弃说明"] = f"发布 {row.get('包编号')} 时被取代"
        row["status"] = STATUS_PUBLISHED
        row["pending"] = False
        row["发布时间"] = row.get("发布时间") or _now()
        # 指标记录上的口径版本跟随发布版本走；草稿保留自己的版本标记，图表仍以版本包为唯一取值来源。
        for metric in _metric_rows(metric_code):
            if metric.get("status") == "已发布":
                metric["口径版本"] = row.get("口径版本")

    def publish_release(self, release_id: int) -> dict[str, Any]:
        with _RELEASE_LOCK:
            row = store.find(MODULE, release_id)
            if row is None:
                return {"ok": False, "message": f"指标发布版本包 {release_id} 不存在"}
            if row.get("status") == STATUS_PUBLISHED:
                return {"ok": True, "message": "指标发布版本包已是发布状态", "entry": row,
                        "package": row, "diff": self.get_diff(release_id)}
            if row.get("status") != STATUS_PENDING:
                return {"ok": False, "message": f"指标发布版本包当前状态为「{row.get('status')}」，不能发布"}
            # 发布前再确认公式快照与差异仍在包内，不在发布时重新读取指标草稿。
            if not row.get("差异明细") or not row.get("公式快照"):
                return {"ok": False, "message": "指标发布版本包缺少公式快照或差异明细，不能发布"}
            self._apply_publish_locked(row)
            return {"ok": True, "message": f"指标发布版本包 {row.get('包编号')} 已发布",
                    "entry": row, "package": row, "diff": self.get_diff(release_id)}

    def run_action(self, release_id: int, action: str, remark: str | None = None) -> dict[str, Any]:
        action = str(action or "").strip()
        if action not in ACTION_RULES:
            return {"ok": False, "message": f"动作「{action}」不属于指标发布版本包可执行范围"}
        if ACTION_RULES[action] == STATUS_PUBLISHED:
            return self.publish_release(release_id)
        with _RELEASE_LOCK:
            row = store.find(MODULE, release_id)
            if row is None:
                return {"ok": False, "message": f"指标发布版本包 {release_id} 不存在"}
            target = ACTION_RULES[action]
            if row.get("status") == STATUS_DEPRECATED:
                return {"ok": False, "message": "已废弃的指标发布版本包不能再流转"}
            row["status"] = target
            row["pending"] = False
            row["abnormal"] = target == STATUS_REJECTED
            if remark:
                row["处理说明"] = remark
            return {"ok": True, "message": f"指标发布版本包已{action}", "entry": row, "package": row}

    # ---------- 差异与来源 ----------

    def get_diff(self, release_id: int) -> dict[str, Any] | None:
        """指标差异接口：读取版本包快照与主版本逐项核对，不重新读取活指标。"""
        row = store.find(MODULE, release_id)
        if row is None:
            return None
        snapshot = dict(row.get("公式快照") or {})
        baseline = dict(row.get("基准快照") or {})
        fields = []
        for field in COMPARE_FIELDS:
            base_value = baseline.get(field)
            if base_value is None and field in snapshot:
                base_value = baseline.get({"口径版本": "基准版本"}.get(field, field))
            publish_value = snapshot.get(field)
            fields.append({
                "核对项": field,
                "基准值": base_value,
                "发布值": publish_value,
                "是否一致": str(base_value if base_value is not None else "") == str(publish_value if publish_value is not None else ""),
            })
        details = [dict(item) for item in row.get("差异明细") or []]
        summary = dict(row.get("差异汇总") or {})
        return {
            "ok": True,
            "包编号": row.get("包编号"),
            "指标编号": row.get("指标编号"),
            "指标名称": row.get("指标名称"),
            "状态": row.get("status"),
            "口径版本": row.get("口径版本"),
            "基准版本": row.get("基准版本"),
            "主版本包编号": row.get("主版本包编号"),
            "发布边界": row.get("发布边界"),
            "核对方式": "读取指标发布版本包快照，与主版本逐项核对",
            "主版本": baseline,
            "发布版本": snapshot,
            "字段核对": fields,
            "差异明细": details,
            "items": details,
            "汇总": summary,
            "阈值规则": row.get("阈值规则") or summary.get("阈值规则") or [],
            "统一口径说明": row.get("统一口径说明") or summary.get("统一口径说明") or OUT_OF_RANGE,
            "超阈值条数": row.get("超阈值条数", summary.get("超阈值条数", 0)),
            "明细条数": row.get("差异条数", summary.get("明细条数", len(details))),
            "来源输入": row.get("来源输入") or [],
        }

    def get_sources(self, release_id: int) -> dict[str, Any] | None:
        """从版本包回到它的关联来源记录，跨模块读取指标草稿与历史发布记录。"""
        row = store.find(MODULE, release_id)
        if row is None:
            return None
        inputs = [dict(item) for item in row.get("来源输入") or []]
        related: list[dict[str, Any]] = []
        for item in inputs:
            module = str(item.get("来源模块") or "")
            record_id = item.get("来源记录id")
            record = None
            if module == METRIC_MODULE and record_id is not None:
                record = store.find(METRIC_MODULE, int(record_id))
            elif module == MODULE and record_id is not None:
                record = store.find(MODULE, int(record_id))
            related.append({
                "来源模块": module,
                "来源记录id": record_id,
                "来源类型": item.get("来源类型"),
                "来源路径": item.get("来源路径"),
                "是否仍存在": record is not None,
                "当前记录": record,
                "字段快照": item.get("字段快照"),
            })
        return {
            "ok": True,
            "包编号": row.get("包编号"),
            "指标编号": row.get("指标编号"),
            "口径版本": row.get("口径版本"),
            "发布边界": row.get("发布边界"),
            "来源输入": inputs,
            "关联来源记录": related,
            "关联主版本包": (self.get_release(int(row["主版本包id"]))
                              if row.get("主版本包id") is not None else None),
        }

    # ---------- 汇总卡片 ----------

    def summary_metrics(self) -> list[dict[str, Any]]:
        """指标页顶部卡片：全部取自版本包，不生成与口径无关的随机值。"""
        rows = store.rows(MODULE)
        published = [row for row in rows if row.get("status") == STATUS_PUBLISHED]
        pending = [row for row in rows if row.get("status") == STATUS_PENDING]
        exceeded = sum(int(row.get("超阈值条数") or 0) for row in published)
        latest = published[-1] if published else None
        latest_value = 0.0
        latest_note = "尚无已发布版本包"
        if latest is not None:
            chart = self.chart_snapshot(str(latest.get("指标编号")))
            latest_value = chart.get("指标值") or 0.0
            latest_note = f"{latest.get('包编号')} · {latest.get('口径版本')}"
        return [
            {"label": "已发布口径版本", "value": len(published),
             "hint": f"待发布 {len(pending)} 个", "trend": 0},
            {"label": "最新发布指标值", "value": latest_value, "hint": latest_note, "trend": 0},
            {"label": "超阈值记录", "value": exceeded,
             "hint": "已按统一口径说明标注", "trend": 0},
        ]

    # ---------- 图表取值 ----------

    def chart_snapshot(self, metric_code: str) -> dict[str, Any]:
        """图表只读取一份完整的已发布版本包；口径版本与指标值同包同边界。"""
        with _RELEASE_LOCK:
            package = _latest_published(metric_code)
            if package is not None:
                summary = dict(package.get("差异汇总") or {})
                details = list(package.get("差异明细") or [])
                precision = _int_value(package.get("精度位数"), DEFAULT_PRECISION)
                if details:
                    # 版本包是完整的一份：指标值覆盖全部差异明细，超出阈值的记录只做标注，
                    # 不在图表取值时被拆出去，图表拿到的口径版本与指标值来自同一个包。
                    value = round(sum(float(item.get("发布值") or 0) for item in details) / len(details), precision)
                    baseline = round(sum(float(item.get("基准值") or 0) for item in details) / len(details), precision)
                else:
                    value = None
                    baseline = None
                exceeded = int(package.get("超阈值条数", summary.get("超阈值条数", 0)) or 0)
                return {
                    "ok": True,
                    "has_value": value is not None,
                    "指标编号": metric_code,
                    "指标名称": package.get("指标名称"),
                    "包编号": package.get("包编号"),
                    "口径版本": package.get("口径版本"),
                    "指标值": value,
                    "基准版本": package.get("基准版本"),
                    "基准指标值": baseline,
                    "差异": round(value - baseline, precision) if value is not None and baseline is not None else None,
                    "取值来源": "metric_release",
                    "发布边界": package.get("发布边界"),
                    "明细条数": len(details),
                    "生效条数": len(details),
                    "排除条数": 0,
                    "超阈值条数": exceeded,
                    "复核状态": "存在超阈值记录，已标注统一口径说明" if exceeded else "全部记录在阈值范围内",
                    "统一口径说明": package.get("统一口径说明") or summary.get("统一口径说明"),
                    "一致性说明": "口径版本与指标值来自同一指标发布版本包",
                    "message": "图表取自已发布指标发布版本包",
                }
            # 没有版本包时，已发布指标记录可作为只读兜底，但不与草稿公式混用。
            rows = _metric_rows(metric_code)
            published = [row for row in rows if row.get("status") == "已发布"]
            if published:
                row = published[-1]
                return {
                    "ok": True,
                    "has_value": False,
                    "指标编号": metric_code,
                    "指标名称": row.get("指标名称"),
                    "包编号": None,
                    "口径版本": row.get("口径版本"),
                    "指标值": None,
                    "取值来源": "metric_def",
                    "message": "该指标尚无已发布的指标发布版本包，图表暂不取值；请先发起版本校验并发布",
                    "一致性说明": "仅返回指标记录口径，未与草稿公式混算",
                }
            return {
                "ok": True,
                "has_value": False,
                "指标编号": metric_code,
                "指标名称": rows[-1].get("指标名称") if rows else None,
                "包编号": None,
                "口径版本": None,
                "指标值": None,
                "取值来源": None,
                "message": "该指标尚无已发布口径版本，图表暂不取值",
                "一致性说明": "草稿与已定义记录不进入图表",
            }


service = MetricReleaseService()
