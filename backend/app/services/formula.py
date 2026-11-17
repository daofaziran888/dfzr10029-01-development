"""指标公式解析与差异计算。

这里的函数不依赖数据仓库，指标发布版本包和示例数据都复用同一套解析/求值逻辑，
保证“公式快照”和“差异明细”算的是同一件事。
"""
from __future__ import annotations

import ast
import hashlib
import math
import re
from datetime import date, timedelta
from typing import Any

MAX_SCOPE_UNITS = 366
DEFAULT_PRECISION = 3
MAX_PRECISION = 8

MODES = ["按业务日期重算", "按统计粒度整组重算"]
GRANULARITIES = ["按日", "按周", "按月", "按季", "按年"]

OUT_OF_RANGE = (
    "统一口径说明：该记录超出阈值规则范围，按主版本基准口径归档并标记待复核；"
    "复核结论随下一个指标发布版本包生效。"
)
IN_RANGE = "统一口径说明：该记录在阈值规则范围内，按指标发布版本包口径取值。"

_ALLOWED_FUNCTIONS = {"SUM", "COUNT", "AVG", "MIN", "MAX", "ROUND", "ABS"}
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_UNARY = (ast.USub, ast.UAdd)
_NAME_RE = re.compile(r"^[0-9A-Za-z_一-鿿]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_WINDOW_SEPARATORS = ("~", "～", "至", " - ", "—", "–")


class FormulaError(ValueError):
    """公式无法解析或求值时抛出。"""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _walk(node: ast.AST, fields: set[str], functions: set[str], operators: set[str], constants: set[float]) -> int:
    """校验 AST 并收集公式结构；只允许白名单节点。"""
    if isinstance(node, ast.Expression):
        return _walk(node.body, fields, functions, operators, constants)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("公式只允许数字常量")
        constants.add(float(node.value))
        return 1
    if isinstance(node, ast.Name):
        if not _NAME_RE.match(node.id):
            raise FormulaError(f"字段名「{node.id}」不合法")
        fields.add(node.id)
        return 1
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise FormulaError("公式只允许 + - * / 四则运算")
        operators.add({ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}[type(node.op)])
        return 1 + _walk(node.left, fields, functions, operators, constants) + _walk(
            node.right, fields, functions, operators, constants)
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY):
            raise FormulaError("公式只允许一元正负号")
        operators.add("-" if isinstance(node.op, ast.USub) else "+")
        return 1 + _walk(node.operand, fields, functions, operators, constants)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id.upper() not in _ALLOWED_FUNCTIONS:
            raise FormulaError(f"公式函数只允许 {'、'.join(sorted(_ALLOWED_FUNCTIONS))}")
        if node.keywords:
            raise FormulaError("公式函数不支持关键字参数")
        functions.add(node.func.id.upper())
        return 1 + sum(_walk(arg, fields, functions, operators, constants) for arg in node.args)
    raise FormulaError("公式包含不支持的表达式")


def parse_formula(formula: str) -> dict[str, Any]:
    """解析计算公式，返回规范公式、引用字段、函数与运算符。"""
    raw = str(formula or "").strip()
    if not raw:
        raise FormulaError("计算公式不能为空")
    if len(raw) > 500:
        raise FormulaError("计算公式长度不能超过 500 个字符")
    try:
        tree = ast.parse(raw, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"计算公式无法解析：{exc.msg}") from exc
    fields: set[str] = set()
    functions: set[str] = set()
    operators: set[str] = set()
    constants: set[float] = set()
    try:
        node_count = _walk(tree, fields, functions, operators, constants)
    except FormulaError:
        raise
    canonical = _normalize(ast.unparse(tree.body))
    return {
        "原始公式": raw,
        "规范公式": canonical,
        "引用字段": sorted(fields),
        "函数": sorted(functions),
        "运算符": sorted(operators),
        "常量": sorted(constants),
        "节点数": node_count,
        "结构摘要": "函数:{funcs}；字段:{fields}；运算符:{operators}".format(
            funcs="、".join(sorted(functions)) or "无",
            fields="、".join(sorted(fields)) or "无",
            operators="、".join(sorted(operators)) or "无",
        ),
    }


def _fraction(*parts: Any) -> float:
    text = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _field_value(field: str, *, metric_code: str, granularity: str, window: str,
                 filters: str, unit_key: str, version_tag: str) -> float:
    """用稳定哈希模拟字段取值，同一口径/单元每次求值都一致。"""
    return 0.35 + _fraction(metric_code, granularity, window, filters, unit_key, version_tag, field) * 0.60


def _safe_div(left: float, right: float) -> float:
    if abs(right) < 1e-12:
        return left
    return left / right


def _apply_function(name: str, args: list[float]) -> float:
    if name == "COUNT":
        return float(len(args))
    if not args:
        return 0.0
    if name == "SUM":
        return float(sum(args))
    if name == "AVG":
        return sum(args) / len(args)
    if name == "MIN":
        return float(min(args))
    if name == "MAX":
        return float(max(args))
    if name == "ABS":
        return abs(args[0])
    if name == "ROUND":
        digits = int(args[1]) if len(args) > 1 else 0
        return round(args[0], max(0, min(digits, MAX_PRECISION)))
    return 0.0


def _evaluate_node(node: ast.AST, *, metric_code: str, granularity: str, window: str,
                   filters: str, unit_key: str, version_tag: str) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, metric_code=metric_code, granularity=granularity, window=window,
                              filters=filters, unit_key=unit_key, version_tag=version_tag)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return _field_value(node.id, metric_code=metric_code, granularity=granularity, window=window,
                            filters=filters, unit_key=unit_key, version_tag=version_tag)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_node(node.operand, metric_code=metric_code, granularity=granularity, window=window,
                               filters=filters, unit_key=unit_key, version_tag=version_tag)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, metric_code=metric_code, granularity=granularity, window=window,
                              filters=filters, unit_key=unit_key, version_tag=version_tag)
        right = _evaluate_node(node.right, metric_code=metric_code, granularity=granularity, window=window,
                               filters=filters, unit_key=unit_key, version_tag=version_tag)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return _safe_div(left, right)
    if isinstance(node, ast.Call):
        name = node.func.id.upper()  # type: ignore[union-attr]
        args = [_evaluate_node(arg, metric_code=metric_code, granularity=granularity, window=window,
                               filters=filters, unit_key=unit_key, version_tag=version_tag)
                for arg in node.args]
        return _apply_function(name, args)
    raise FormulaError("公式包含不支持的表达式")


def evaluate_formula(formula: str, *, metric_code: str, granularity: str, window: str,
                     filters: str, unit_key: str, version_tag: str) -> float:
    """按指标、统计粒度和时间窗口对公式求值，结果稳定且可复算。"""
    parse_formula(formula)
    try:
        tree = ast.parse(str(formula).strip(), mode="eval")
        value = _evaluate_node(tree, metric_code=metric_code, granularity=granularity, window=window,
                               filters=filters, unit_key=unit_key, version_tag=version_tag)
    except FormulaError:
        raise
    except Exception as exc:  # pragma: no cover - 防御性兜底
        raise FormulaError(f"计算公式求值失败：{exc}") from exc
    if not math.isfinite(value):
        raise FormulaError("计算公式结果不是有效数字")
    # 模拟指标值收敛到可展示区间，避免出现极端量纲。
    return round(max(0.01, min(value, 2.0)), 6)


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _month_span(text: str) -> tuple[date, date] | None:
    """把 YYYY-MM 解析为整月区间。"""
    if not _MONTH_RE.match(text):
        return None
    try:
        start = date.fromisoformat(text + "-01")
    except ValueError:
        return None
    if start.month == 12:
        end = date(start.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(start.year, start.month + 1, 1) - timedelta(days=1)
    return start, end


def parse_window(window: str) -> tuple[date, date] | None:
    """解析时间窗口，支持日期区间、整月区间、单日、整月与“最近 N 天”。"""
    text = str(window or "").strip()
    if not text:
        return None
    for sep in _WINDOW_SEPARATORS:
        if sep not in text:
            continue
        left, right = [part.strip() for part in text.split(sep, 1)]
        left_span = _month_span(left)
        right_span = _month_span(right)
        start = _parse_date(left) or (left_span[0] if left_span else None)
        end = _parse_date(right) or (right_span[1] if right_span else None)
        if start is None or end is None:
            return None
        return (start, end) if start <= end else (end, start)
    if _DATE_RE.match(text):
        day = _parse_date(text)
        return (day, day) if day else None
    span = _month_span(text)
    if span is not None:
        return span
    match = re.match(r"^最近\s*(\d+)\s*天$", text)
    if match:
        end = date(2026, 9, 30)
        start = end - timedelta(days=max(1, int(match.group(1))) - 1)
        return start, end
    return None


def _iter_days(start: date, end: date) -> list[date]:
    """按时间窗口枚举业务日期；超过上限时只保留窗口内最近的 MAX_SCOPE_UNITS 天。"""
    total = (end - start).days + 1
    if total <= 0:
        return []
    if total > MAX_SCOPE_UNITS:
        start = end - timedelta(days=MAX_SCOPE_UNITS - 1)
        total = MAX_SCOPE_UNITS
    return [start + timedelta(days=offset) for offset in range(total)]


def _group_label(day: date, granularity: str) -> str:
    if granularity == "按日":
        return day.isoformat()
    if granularity == "按周":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    if granularity == "按月":
        return day.strftime("%Y-%m")
    if granularity == "按季":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    if granularity == "按年":
        return str(day.year)
    return "全量"


def build_scope(window: str, granularity: str, mode: str) -> list[dict[str, Any]]:
    """按重算方式生成差异明细范围：业务日期逐日或统计粒度整组。"""
    if mode not in MODES:
        raise FormulaError(f"重算方式只允许 {'、'.join(MODES)}")
    parsed = parse_window(window)
    if parsed is None:
        # 无法识别的时间窗口仍然保留一个可核对的整体范围，而不是静默丢弃。
        return [{"范围键": str(window or "全部"), "业务日期": None, "统计分组": str(window or "全部"),
                 "起始日期": None, "结束日期": None}]
    start, end = parsed
    days = _iter_days(start, end)
    if mode == "按业务日期重算":
        return [
            {"范围键": day.isoformat(), "业务日期": day.isoformat(), "统计分组": day.isoformat(),
             "起始日期": day.isoformat(), "结束日期": day.isoformat()}
            for day in days
        ]
    groups: dict[str, list[date]] = {}
    for day in days:
        groups.setdefault(_group_label(day, granularity), []).append(day)
    units = []
    for label, group_days in groups.items():
        units.append({
            "范围键": label,
            "业务日期": None,
            "统计分组": label,
            "起始日期": group_days[0].isoformat(),
            "结束日期": group_days[-1].isoformat(),
        })
    return units


def _threshold_rule(rules: list[dict[str, Any]] | None) -> dict[str, Any]:
    if rules:
        return dict(rules[0])
    return {"规则编号": "TH-001", "核对项": "差异率", "下限": -0.10, "上限": 0.10,
            "说明": "指标值差异率需落在 ±10% 以内"}


def build_diff(*, metric_code: str, metric_name: str, base_formula: str, new_formula: str,
               base_version: str, new_version: str, granularity: str, window: str,
               filters: str, precision: int, mode: str,
               threshold_rules: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在同一个发布边界内计算基准与发布版本的逐项差异。

    基准与发布值都在同一个版本标签下求值，差异只来自公式变化；公式相同则差异为 0，
    不会因为版本号变化凭空产生口径差异。
    """
    precision = max(0, min(int(precision or DEFAULT_PRECISION), MAX_PRECISION))
    rule = _threshold_rule(threshold_rules)
    lower, upper = float(rule.get("下限", -0.10)), float(rule.get("上限", 0.10))
    units = build_scope(window, granularity, mode)
    details: list[dict[str, Any]] = []
    exceeded_labels: list[str] = []
    base_values: list[float] = []
    new_values: list[float] = []
    for unit in units:
        unit_key = str(unit["范围键"])
        base_value = evaluate_formula(base_formula, metric_code=metric_code, granularity=granularity,
                                      window=window, filters=filters, unit_key=unit_key,
                                      version_tag=new_version)
        new_value = evaluate_formula(new_formula, metric_code=metric_code, granularity=granularity,
                                     window=window, filters=filters, unit_key=unit_key,
                                     version_tag=new_version)
        base_value = round(base_value, precision)
        new_value = round(new_value, precision)
        diff = round(new_value - base_value, precision)
        rate = round(diff / base_value, 4) if base_value else 0.0
        exceeded = not (lower <= rate <= upper)
        if exceeded:
            exceeded_labels.append(unit_key)
        details.append({
            "范围键": unit_key,
            "业务日期": unit.get("业务日期"),
            "统计分组": unit.get("统计分组"),
            "起始日期": unit.get("起始日期"),
            "结束日期": unit.get("结束日期"),
            "基准值": base_value,
            "发布值": new_value,
            "差异": diff,
            "差异率": rate,
            "是否超阈值": exceeded,
            "阈值说明": f"{rule.get('核对项', '差异率')}需落在 {lower:.0%} ~ {upper:.0%} 之间",
            "统一口径说明": OUT_OF_RANGE if exceeded else IN_RANGE,
        })
        base_values.append(base_value)
        new_values.append(new_value)
    base_metric = round(sum(base_values) / len(base_values), precision) if base_values else 0.0
    new_metric = round(sum(new_values) / len(new_values), precision) if new_values else 0.0
    summary = {
        "指标编号": metric_code,
        "指标名称": metric_name,
        "统计粒度": granularity,
        "时间窗口": window,
        "重算方式": mode,
        "基准版本": base_version,
        "发布版本": new_version,
        "基准指标值": base_metric,
        "发布指标值": new_metric,
        "指标差异": round(new_metric - base_metric, precision),
        "明细条数": len(details),
        "超阈值条数": len(exceeded_labels),
        "超阈值范围": exceeded_labels,
        "阈值规则": [rule],
        "阈值说明": f"{rule.get('核对项', '差异率')}需落在 {lower:.0%} ~ {upper:.0%} 之间",
        "统一口径说明": OUT_OF_RANGE if exceeded_labels else IN_RANGE,
    }
    return details, summary
