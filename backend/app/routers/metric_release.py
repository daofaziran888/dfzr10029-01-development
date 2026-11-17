"""指标发布版本包接口：提交公式、发起版本校验、生成版本包并核对差异明细。

路由只做参数与错误呈现，发布边界、阈值判断和并发去重都在
`app.services.metric_release` 里完成。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.metric_release import MetricReleaseService
from app.store import store

import csv
import io

router = APIRouter(prefix="/api/metric_def", tags=["指标发布版本包"])
service = MetricReleaseService()

PACKAGE_FIELDS = ["包编号", "指标编号", "指标名称", "口径版本", "基准版本", "统计粒度",
                  "时间窗口", "重算方式", "差异条数", "超阈值条数", "指标差异"]


def _package_view(row: dict[str, Any], *, with_details: bool = False) -> dict[str, Any]:
    view = {field: row.get(field) for field in PACKAGE_FIELDS}
    view.update({
        "id": row.get("id"),
        "status": row.get("status"),
        "pending": row.get("pending"),
        "abnormal": row.get("abnormal"),
        "计算公式": row.get("计算公式"),
        "基准公式": row.get("基准公式"),
        "筛选条件": row.get("筛选条件"),
        "精度位数": row.get("精度位数"),
        "窗口范围": row.get("窗口范围"),
        "主版本包编号": row.get("主版本包编号"),
        "发布边界": row.get("发布边界"),
        "统一口径说明": row.get("统一口径说明"),
        "阈值规则": row.get("阈值规则"),
        "创建人": row.get("创建人"),
        "创建时间": row.get("创建时间"),
        "发布时间": row.get("发布时间"),
        "来源输入数量": len(row.get("来源输入") or []),
    })
    if with_details:
        view["公式快照"] = row.get("公式快照")
        view["基准快照"] = row.get("基准快照")
        view["差异明细"] = row.get("差异明细") or []
        view["差异汇总"] = row.get("差异汇总") or {}
        view["来源输入"] = row.get("来源输入") or []
    return view


@router.get("/releases", response_model=PageResult[dict])
def list_releases(
    metric_code: str | None = Query(default=None, description="按指标编号过滤"),
    status: str | None = Query(default=None, description="待发布、已发布、已驳回、已废弃"),
    keyword: str | None = Query(default=None, description="按包编号、指标编号或指标名称检索"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """列出指标发布版本包；默认返回全部版本，可按指标与状态收窄。"""
    items, total = service.list_releases(
        metric_code=metric_code, status=status, keyword=keyword, page=page, size=size)
    return PageResult(items=[_package_view(row) for row in items], total=total, page=page, size=size)


@router.get("/releases/{release_id}")
def get_release(release_id: int) -> dict[str, Any]:
    """读取单个版本包，包含公式快照、基准快照与来源输入。"""
    row = service.get_release(release_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"指标发布版本包 {release_id} 不存在")
    return _package_view(row, with_details=True)


@router.get("/releases/{release_id}/diff")
def get_release_diff(release_id: int) -> dict[str, Any]:
    """指标差异接口：读取版本包快照，与主版本逐项核对。"""
    diff = service.get_diff(release_id)
    if diff is None:
        raise HTTPException(status_code=404, detail=f"指标发布版本包 {release_id} 不存在")
    return diff


@router.get("/releases/{release_id}/sources")
def get_release_sources(release_id: int) -> dict[str, Any]:
    """回到版本包的关联来源记录：指标草稿与历史发布记录。"""
    sources = service.get_sources(release_id)
    if sources is None:
        raise HTTPException(status_code=404, detail=f"指标发布版本包 {release_id} 不存在")
    return sources


@router.get("/releases/{release_id}/export")
def export_release_diff(release_id: int) -> StreamingResponse:
    """导出差异明细 CSV，便于逐项复核阈值外的记录。"""
    diff = service.get_diff(release_id)
    if diff is None:
        raise HTTPException(status_code=404, detail=f"指标发布版本包 {release_id} 不存在")
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    columns = ["范围键", "业务日期", "统计分组", "起始日期", "结束日期", "基准值", "发布值",
               "差异", "差异率", "是否超阈值", "阈值说明", "统一口径说明"]
    writer.writerow(["包编号", "指标编号", "口径版本", "基准版本", *columns])
    for item in diff.get("差异明细") or []:
        row = [diff.get("包编号"), diff.get("指标编号"), diff.get("口径版本"), diff.get("基准版本")]
        for column in columns:
            value = item.get(column, "")
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="metric_release_{release_id}.csv"'})


@router.get("/{entry_id}/chart")
def get_chart_snapshot(entry_id: int) -> dict[str, Any]:
    """图表数据源：始终只返回一份完整的已发布版本包，口径版本与指标值同包。"""
    entry = store.find("metric_def", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"指标 {entry_id} 不存在")
    return service.chart_snapshot(str(entry.get("指标编号")))


@router.post("/{entry_id}/formula", response_model=ActionResult)
def submit_formula(entry_id: int, payload: EntryPayload) -> ActionResult:
    """指标页提交公式：已发布记录保留原快照，调整后的公式落到同指标的新草稿。"""
    entry, message, parsed = service.update_metric_formula(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    entry_view = dict(entry)
    entry_view["公式结构"] = parsed
    return ActionResult(ok=True, message=message, entry=entry_view)


@router.post("/{entry_id}/validate", response_model=ActionResult)
def validate_version(entry_id: int, payload: EntryPayload) -> ActionResult:
    """发起版本校验：在同一发布边界内生成公式快照、基准快照和差异明细。"""
    values = dict(payload.values or {})
    values.setdefault("来源记录id", entry_id)
    result = service.create_release(values, publish=False)
    message = str(result.get("message") or "")
    entry = result.get("package")
    if entry is not None:
        message = f"{message}（{entry.get('包编号')} · {entry.get('口径版本')}）"
    return ActionResult(ok=bool(result.get("ok")), message=message,
                        entry=_package_view(entry, with_details=True) if entry else None)


@router.post("/{entry_id}/release", response_model=ActionResult)
def release_version(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """一步发布：提交公式校验并直接生成已发布的指标版本包。"""
    values = dict((payload.values if payload else {}) or {})
    values.setdefault("来源记录id", entry_id)
    result = service.create_release(values, publish=True)
    entry = result.get("package")
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""),
                        entry=_package_view(entry, with_details=True) if entry else None)


@router.post("/releases/{release_id}/actions", response_model=ActionResult)
def run_release_action(release_id: int, payload: ActionPayload) -> ActionResult:
    """发布、驳回或废弃一个指标发布版本包。"""
    result = service.run_action(release_id, payload.action.strip(), payload.remark)
    entry = result.get("entry")
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""),
                        entry=_package_view(entry, with_details=True) if entry else None)
