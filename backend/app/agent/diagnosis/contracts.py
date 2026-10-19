"""单篇诊断 Agent 的契约（第 16 步）：字段口径 / 确定性计算 / 专属异常 / 输出形状。

三部分：

1. **六个指标继续沿用第 15 步口径**（``views / likes / collects / comments /
   shares / new_followers``）；本模块所有比率 / 环比 / 增长 / 覆盖时长**全部本地
   确定性计算**（纯函数、可单测）——只把算好的事实交给模型，**严禁让模型做算术**。
2. **漏斗比率「同快照配对」原则**：每条比率必须基于同一条**同时含 ``views`` 与
   对应分子**的、最靠后的快照计算（防跨快照错配、防除零）；比率统一 4 位小数。
3. **专属异常**：全部继承 ``AgentNonRetryableError``（不消耗节点重试额度），
   由 service 统一翻译成统一错误体（404 / 400）。

红线（自检断言）：诊断**只读 + 建议** —— 不修改 notes / topics / note_metrics
任何数据；**不引入任何外部基准数字**（行业均值 / 爆款阈值一律禁止，跨篇基准属
第 18 步 RAG）；数据不足必须如实声明（``data_gaps``），严重不足时 ``score`` 必须为
``None``（不硬打分）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.agent.errors import AgentNonRetryableError

#: 允许进入诊断的**六个**指标（顺序即展示顺序，与 note_metrics 列一一对应；勿改）
METRIC_FIELDS: tuple[str, ...] = (
    "views",
    "likes",
    "collects",
    "comments",
    "shares",
    "new_followers",
)

#: 六条漏斗比率（顺序即展示顺序）
RATE_FIELDS: tuple[str, ...] = (
    "like_rate",
    "collect_rate",
    "comment_rate",
    "share_rate",
    "follow_rate",
    "engagement_rate",
)

#: 单指标比率 → 分子字段（engagement_rate 特殊：四个互动之和，见 compute_rates）
_RATE_NUMERATORS: dict[str, str] = {
    "like_rate": "likes",
    "collect_rate": "collects",
    "comment_rate": "comments",
    "share_rate": "shares",
    "follow_rate": "new_followers",
}

#: engagement_rate 的四个组成指标
_ENGAGEMENT_PARTS: tuple[str, ...] = ("likes", "collects", "comments", "shares")

#: 比率统一保留的小数位
RATE_PRECISION = 4

#: 合并后 data_gaps 的条数上限（本地 + 模型补充）
DATA_GAPS_LIMIT = 12

#: 基准缺失声明（**本期固定话术**：没有外部基准，绝不杜撰行业均值）
BENCHMARK_NOTE = "本期无同类/历史爆款基准，仅基于该笔记自身数据；跨篇基准见第 18 步 RAG"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _iso(value: Any) -> str | None:
    """datetime / 字符串 → ISO 串（序列化进 benchmark 用）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_datetime(value: Any) -> datetime | None:
    """datetime / ISO 字符串 → datetime（解析失败 → None）。

    状态内的 ``captured_at`` 统一以 ISO **字符串**存放（检查点只看可序列化小字段），
    因此这里必须同时兼容字符串输入。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 1) latest：每个指标的最后一个非空值 + 最新快照 id
# ---------------------------------------------------------------------------
def metric_latest(
    snapshots: list[dict[str, Any]],
) -> tuple[dict[str, int | None], str | None]:
    """取「最新值」与「最新快照 id」。

    Args:
        snapshots: 按 ``captured_at`` **升序**排列的快照普通 dict 列表
            （由 ``load_context`` 节点把 ORM 行转成普通 dict 后传入）。

    Returns:
        ``(latest, latest_snapshot_id)``：

        - ``latest``：每个指标在时间线中**最后一个非空值**（全为空 → ``None``，
          绝不臆造）；键为 :data:`METRIC_FIELDS`；
        - ``latest_snapshot_id``：时间线中**最新一条**快照的 id（无快照 → ``None``）；
          它将被写入 ``diagnostics.based_on_metric_id``。
    """
    latest: dict[str, int | None] = {field: None for field in METRIC_FIELDS}
    for row in snapshots:
        for field in METRIC_FIELDS:
            value = row.get(field)
            if value is not None:
                latest[field] = int(value)
    latest_id = str(snapshots[-1]["id"]) if snapshots else None
    return latest, latest_id


# ---------------------------------------------------------------------------
# 2) 漏斗比率：「同快照配对」+ 防跨快照错配 + 防除零
# ---------------------------------------------------------------------------
def _latest_pair(
    snapshots: list[dict[str, Any]], numerator_field: str
) -> tuple[int | None, int | None]:
    """从后往前找**同一条同时含 views 与分子的**快照，返回 ``(views, numerator)``。

    找不到（所有快照的 views 缺失 / 为 0 / 分子缺失）→ ``(None, None)``。
    """
    for row in reversed(snapshots):
        views = row.get("views")
        numerator = row.get(numerator_field)
        if views is not None and int(views) > 0 and numerator is not None:
            return int(views), int(numerator)
    return None, None


def _latest_engagement_pair(
    snapshots: list[dict[str, Any]],
) -> tuple[int | None, int | None]:
    """engagement 的配对：要求同快照内四个互动指标**全部非空**才可求和。"""
    for row in reversed(snapshots):
        views = row.get("views")
        parts = [row.get(field) for field in _ENGAGEMENT_PARTS]
        if views is not None and int(views) > 0 and all(item is not None for item in parts):
            return int(views), sum(int(item) for item in parts)
    return None, None


def compute_rates(snapshots: list[dict[str, Any]]) -> dict[str, float | None]:
    """计算六条漏斗比率（全部本地确定性完成，4 位小数）。

    规则（任务书第 3 部分）：

    - 每条比率基于**同一条同时含 views 与对应分子的、最靠后的快照**；
    - ``views`` 为 ``None`` / ``0`` 或分子缺失 → 该比率为 ``None``（绝不臆造）；
    - ``engagement_rate = (likes + collects + comments + shares) / views``。
    """
    rates: dict[str, float | None] = {key: None for key in RATE_FIELDS}
    for rate_key, numerator_field in _RATE_NUMERATORS.items():
        views, numerator = _latest_pair(snapshots, numerator_field)
        if views is not None and numerator is not None:
            rates[rate_key] = round(numerator / views, RATE_PRECISION)
    views, engagement = _latest_engagement_pair(snapshots)
    if views is not None and engagement is not None:
        rates["engagement_rate"] = round(engagement / views, RATE_PRECISION)
    return rates


# ---------------------------------------------------------------------------
# 3) 环比 / 趋势（快照数 ≥ 2 才有意义）
# ---------------------------------------------------------------------------
def compute_trends(
    snapshots: list[dict[str, Any]],
    published_at: Any,
) -> dict[str, Any]:
    """计算相邻增量、总增量、增长率与数据覆盖时长。

    Returns:
        ``{segments, total_deltas, growth_rates, coverage_hours}``：

        - ``segments``：相邻快照（升序）逐段增量 —— 每段
          ``{from, to, deltas:{六指标}}``；任一为空则该段该指标 ``None``；
        - ``total_deltas``：首期（第一个非空值）→ 最新（最后一个非空值）的总增量
          （某一端缺失 → ``None``）；
        - ``growth_rates``：``总增量 / 首期值``（首期为 ``0`` / ``None`` → ``None``），
          4 位小数；
        - ``coverage_hours``：``published_at → 最新 captured_at`` 的小时数（整数）；
          无 ``published_at`` 时用**最早快照**的 ``captured_at`` 估算；无快照 → ``None``。
    """
    segments: list[dict[str, Any]] = []
    for prev, cur in zip(snapshots, snapshots[1:]):
        deltas: dict[str, int | None] = {}
        for field in METRIC_FIELDS:
            before = prev.get(field)
            after = cur.get(field)
            deltas[field] = (int(after) - int(before)) if (before is not None and after is not None) else None
        segments.append(
            {
                "from": _iso(prev.get("captured_at")),
                "to": _iso(cur.get("captured_at")),
                "deltas": deltas,
            }
        )

    total_deltas: dict[str, int | None] = {}
    growth_rates: dict[str, float | None] = {}
    for field in METRIC_FIELDS:
        values = [int(row[field]) for row in snapshots if row.get(field) is not None]
        if len(values) >= 2:
            first, last = values[0], values[-1]
            total_deltas[field] = last - first
            growth_rates[field] = (
                round((last - first) / first, RATE_PRECISION) if first > 0 else None
            )
        else:
            total_deltas[field] = None
            growth_rates[field] = None

    coverage_hours: int | None = None
    if snapshots:
        latest_at = _as_datetime(snapshots[-1].get("captured_at"))
        start_at = _as_datetime(published_at) or _as_datetime(snapshots[0].get("captured_at"))
        if latest_at is not None and start_at is not None:
            coverage_hours = int(round((latest_at - start_at).total_seconds() / 3600))

    return {
        "segments": segments,
        "total_deltas": total_deltas,
        "growth_rates": growth_rates,
        "coverage_hours": coverage_hours,
    }


# ---------------------------------------------------------------------------
# 4) 诊断类型判定（manual 优先；auto 三分支；给依据 reason）
# ---------------------------------------------------------------------------
def decide_diagnosis_type(
    snapshots: list[dict[str, Any]],
    manual_type: str | None = None,
    *,
    anomaly_drop_ratio: float = 0.30,
) -> dict[str, str]:
    """判定诊断类型：``cold_start`` / ``anomaly`` / ``review``。

    - 用户显式传入 ``manual_type`` 时以用户为准（``source="manual"``）；
    - 否则自动判定（``source="auto"``）：
      a. 无任何快照 / 最新 views 缺失 → ``cold_start``；
      b. 相邻快照中 ``views`` 或 ``likes`` 出现**负增长且降幅**
         ≥ ``anomaly_drop_ratio`` → ``anomaly``；
      c. 其余有可用数据 → ``review``。

    Returns:
        ``{"type": ..., "source": "auto|manual", "reason": "..."}``
    """
    if manual_type:
        return {
            "type": str(manual_type),
            "source": "manual",
            "reason": f"用户显式指定「{manual_type}」，未走自动判定",
        }

    if not snapshots:
        return {"type": "cold_start", "source": "auto", "reason": "无任何运营数据快照"}

    latest, _latest_id = metric_latest(snapshots)
    if latest.get("views") is None:
        return {
            "type": "cold_start",
            "source": "auto",
            "reason": "最新快照中缺少浏览量（views），无法建立漏斗口径",
        }

    for prev, cur in zip(snapshots, snapshots[1:]):
        for field in ("views", "likes"):
            before = prev.get(field)
            after = cur.get(field)
            if before is None or after is None or int(before) <= 0:
                continue
            drop = (int(before) - int(after)) / int(before)
            if drop >= anomaly_drop_ratio:
                percent = round(drop * 100, 1)
                limit = round(anomaly_drop_ratio * 100)
                return {
                    "type": "anomaly",
                    "source": "auto",
                    "reason": (
                        f"{field} 从 {before} 降到 {after}"
                        f"（降幅 {percent}% ≥ {limit}%），判定为异常波动"
                    ),
                }

    return {
        "type": "review",
        "source": "auto",
        "reason": "存在可用数据且未检出显著负增长，按常规复盘处理",
    }


# ---------------------------------------------------------------------------
# 5) 数据缺口收集（只列事实，绝不臆造）
# ---------------------------------------------------------------------------
def collect_data_gaps(
    *,
    snapshots: list[dict[str, Any]],
    latest_values: dict[str, int | None],
    rates: dict[str, float | None],
    has_cover: bool,
    published_at: Any,
    body_text: str | None,
    min_snapshots: int = 1,
) -> list[str]:
    """收集数据缺口（字符串数组；供提示词与持久化声明）。

    「没有数据也要如实说」，宁多列不缺报：无快照 / 缺指标 / 无法算比率 /
    无封面 / 无发布时间 / 仅 1 条快照 / 正文为空。
    """
    gaps: list[str] = []
    if not snapshots:
        gaps.append("无运营数据快照（尚未回填任何数据，做不了数据侧诊断）")
    else:
        if len(snapshots) < max(1, min_snapshots):
            gaps.append(f"快照数量少于配置下限 {max(1, min_snapshots)} 条")
        if len(snapshots) == 1:
            gaps.append("只有 1 条快照，无法计算环比与趋势（建议持续回填后复诊）")
        missing = [field for field in METRIC_FIELDS if latest_values.get(field) is None]
        if missing:
            gaps.append("最新快照缺少指标：" + "、".join(missing))
        uncomputable = [key for key in RATE_FIELDS if rates.get(key) is None]
        if uncomputable:
            gaps.append(
                "以下比率无法计算（同快照内 views 为 0 或对应分子缺失）："
                + "、".join(uncomputable)
            )
    if not has_cover:
        gaps.append("未选择封面（封面直接影响点击与阅读）")
    if published_at is None:
        gaps.append("笔记缺少发布时间（数据覆盖时长按最早快照估算）")
    if not (body_text or "").strip():
        gaps.append("正文为空（内容侧无从检查）")
    return gaps


# ---------------------------------------------------------------------------
# 6) 模型输出归一 + 校验（「数据不足型冷启动」宽容口径）
# ---------------------------------------------------------------------------
#: 模型可能把载荷包在这些键里（宽容解包，只解一层）
_WRAPPER_KEYS = ("diagnosis", "data", "result", "output")

#: score 文本占位符（→ None，合法）
_SCORE_PLACEHOLDERS = {"", "null", "none", "n/a", "无", "暂无"}


def _unwrap_payload(raw: Any) -> Any:
    """宽容解包：模型偶发把诊断对象包在 diagnosis / data 等键里。"""
    if isinstance(raw, dict):
        for key in _WRAPPER_KEYS:
            inner = raw.get(key)
            if isinstance(inner, dict):
                return inner
    return raw


def _parse_score(
    value: Any, score_min: float, score_max: float
) -> tuple[float | None, str | None]:
    """解析 score：合法 → (数字, None)；非法 → (None, 错误文案)。"""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "score 类型非法（布尔值不是分数）"
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if text in _SCORE_PLACEHOLDERS:
            return None, None
        try:
            number = float(text)
        except ValueError:
            return None, f"score 无法解析为数字（原文：{value!r}）"
    else:
        return None, f"score 类型非法（应为数字或 null，实际 {type(value).__name__}）"

    if number < score_min or number > score_max:
        return None, f"score 越界（{number} 不在 [{score_min}, {score_max}] 内）"
    return round(number, 2), None


def _coerce_text_list(
    label: str,
    value: Any,
    *,
    max_items: int,
    text_max_len: int,
    required: bool,
) -> tuple[list[str], list[str], list[str]]:
    """文本数组归一（宽容截断）+ 硬伤收集。

    Returns:
        ``(items, errors, warnings)`` —— ``errors`` 是**本地无法修复**的硬伤
        （类型错误 / 必填为空，触发回炉）；``warnings`` 只是截断等宽容修正记录。
    """
    errors: list[str] = []
    warnings: list[str] = []
    if value is None:
        items: list[str] = []
    elif isinstance(value, str):
        items = [value.strip()] if value.strip() else []
    elif isinstance(value, list):
        items = []
        for item in value:
            text = str(item).strip()
            if text:
                items.append(text)
    else:
        return [], [f"{label} 不是数组（实际 {type(value).__name__}）"], []

    cleaned: list[str] = []
    for text in items:
        if len(text) > text_max_len:
            warnings.append(f"{label} 单条超长已截断（>{text_max_len} 字）")
            text = text[:text_max_len]
        cleaned.append(text)
    if len(cleaned) > max_items:
        warnings.append(f"{label} 条数超限已截断（{len(cleaned)} → {max_items}）")
        cleaned = cleaned[:max_items]
    if required and not cleaned:
        errors.append(f"{label} 为空（必须给出可执行建议）")
    return cleaned, errors, warnings


def normalize_diagnosis_payload(
    raw: Any,
    *,
    cold_start: bool,
    max_items: int,
    text_max_len: int,
    score_min: float,
    score_max: float,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """把模型原始 JSON 归一成诊断载荷（纯函数，可单测）。

    Args:
        raw: 模型输出的 JSON（未经校验）。
        cold_start: 是否「数据严重不足」（无快照 / 最新 views 缺失）——
            为 True 时 ``score`` **强制为 None**（不硬打分），不因模型给了分数而回炉。
        max_items / text_max_len: 四个文本数组的条数与单条长度上限。
        score_min / score_max: ``score`` 的合法区间。

    Returns:
        ``(normalized, errors, warnings)``：

        - ``normalized``：``{score, strengths, weaknesses, reasons, suggestions,
          model_data_gaps, confidence}``；
        - ``errors``：硬伤（触发有限回路回炉）—— JSON 非对象 / score 越界或
          类型非法 / 数组类型错误 / ``suggestions`` 为空；
        - ``warnings``：宽容修正记录（截断、冷启动忽略评分等）。
    """
    warnings: list[str] = []
    data = _unwrap_payload(raw)
    if not isinstance(data, dict):
        return (
            {
                "score": None,
                "strengths": [],
                "weaknesses": [],
                "reasons": [],
                "suggestions": [],
                "model_data_gaps": [],
                "confidence": None,
            },
            ["模型输出不是 JSON 对象"],
            warnings,
        )

    score, score_error = _parse_score(data.get("score"), score_min, score_max)
    errors: list[str] = []
    if score_error:
        errors.append(score_error)
    if cold_start and score is not None:
        warnings.append("数据不足以支撑评分，已忽略模型给出的 score（不硬打分）")
        score = None

    strengths, list_errors, list_warnings = _coerce_text_list(
        "strengths",
        data.get("strengths"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=False,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    weaknesses, list_errors, list_warnings = _coerce_text_list(
        "weaknesses",
        data.get("weaknesses"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=False,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    reasons, list_errors, list_warnings = _coerce_text_list(
        "reasons",
        data.get("reasons"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=False,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    suggestions, list_errors, list_warnings = _coerce_text_list(
        "suggestions",
        data.get("suggestions"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=True,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    model_gaps, list_errors, list_warnings = _coerce_text_list(
        "data_gaps",
        data.get("data_gaps"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=False,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    confidence: float | None = None
    raw_confidence = data.get("confidence")
    if raw_confidence is not None and not isinstance(raw_confidence, bool):
        try:
            number = float(raw_confidence)
            if 0.0 <= number <= 1.0:
                confidence = round(number, 3)
        except (TypeError, ValueError):
            confidence = None

    normalized = {
        "score": score,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "reasons": reasons,
        "suggestions": suggestions,
        "model_data_gaps": model_gaps,
        "confidence": confidence,
    }
    return normalized, errors, warnings


def merge_data_gaps(
    local_gaps: list[str],
    model_gaps: list[str],
    *,
    limit: int = DATA_GAPS_LIMIT,
) -> list[str]:
    """合并本地缺口与模型补充缺口（本地优先、去重保序、限长）。"""
    merged: list[str] = []
    for item in list(local_gaps) + list(model_gaps):
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    return merged[:limit]


# ---------------------------------------------------------------------------
# 7) 专属异常（service 按类型翻译统一错误体）
# ---------------------------------------------------------------------------
class DiagnosisError(AgentNonRetryableError):
    """单篇诊断业务错误基类。（继承 AgentNonRetryableError：绝不进入节点重试。）"""


class DiagnosisContextError(DiagnosisError):
    """上下文不满足（笔记不存在等）→ 404。"""


class DiagnosisGiveUpError(DiagnosisError):
    """诊断放弃：重试超限 / 模型不可用 → 400（可重试）。"""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


__all__ = [
    "METRIC_FIELDS",
    "RATE_FIELDS",
    "RATE_PRECISION",
    "DATA_GAPS_LIMIT",
    "BENCHMARK_NOTE",
    "metric_latest",
    "compute_rates",
    "compute_trends",
    "decide_diagnosis_type",
    "collect_data_gaps",
    "normalize_diagnosis_payload",
    "merge_data_gaps",
    "DiagnosisError",
    "DiagnosisContextError",
    "DiagnosisGiveUpError",
]
