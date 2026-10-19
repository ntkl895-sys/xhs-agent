"""周报确定性计算（第 17 步）——纯函数 + 唯一触库入口 + 专属异常。

定位（照第 16 步 ``diagnosis/contracts.py`` 范式）：

- **所有数字都在本地算好**再喂给模型——模型只做「运营解读」，不许做算术；
- ``resolve_period`` / ``period_delta`` / ``normalize_report_payload`` 为**纯函数**
  （可离线单测）；``build_period_facts`` 是**唯一触库函数**（只读查询 →
  普通 dict，供图状态序列化）；
- 聚合口径**复用第 17 步看板**（``app.services.dashboard``）：最新值 / 合计漏斗
  与看板同一套纯函数，「绝不重复实现第二套口径」；
- ``normalize_report_payload`` 的文本数组归一直接复用第 16 步已固化的
  ``_coerce_text_list``（条数 / 长度截断口径全项目一致）。

红线：本模块**只读**（零写入）；不请求外部域名；不产生任何外部基准数字
（行业均值 / 爆款阈值一律禁止，跨篇基准属第 18 步 RAG）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.diagnosis.contracts import (
    DATA_GAPS_LIMIT,
    METRIC_FIELDS,
    _coerce_text_list,
    merge_data_gaps,
)
from app.agent.errors import AgentNonRetryableError
from app.core.config import settings
from app.core.timeutil import LOCAL_TIMEZONE, now_local
from app.models.diagnostic import Diagnostic
from app.models.enums import HotspotStatus, NoteStatus, TopicStatus
from app.models.hotspot import Hotspot
from app.models.note import Note
from app.models.topic import Topic
from app.services.dashboard.aggregator import (
    as_datetime,
    latest_values_per_note,
    sum_funnel,
)
from app.services.dashboard.service import load_snapshots

logger = logging.getLogger("xhs-agent.weekly.contracts")

# ---------------------------------------------------------------------------
# 常量（上限口径）
# ---------------------------------------------------------------------------
#: 周期跨度上限（天）——超过直接判非法（防止把整年数据塞进一次周报）
MAX_PERIOD_DAYS = 90

#: 报告四个文本数组的条数上限（复用「条数 / 截断」范式）
REPORT_MAX_LIST_ITEMS = 8
#: 报告四个文本数组的单条最大字数
REPORT_TEXT_MAX_LEN = 300
#: 报告 summary（周期概览）的最大字数（长文汇报，比数组单条放宽）
REPORT_SUMMARY_MAX_LEN = 600

#: facts 里随载的诊断摘要条数上限（控 token；按最近生成倒序取）
DIAG_SUMMARY_LIMIT = 10
#: 每条诊断摘要随载的建议条数（只取前几条代表「核心建议」）
DIAG_SUGGESTION_TAIL = 3
#: facts 里「周期内新发布笔记清单」的条数上限（超长时截断并在缺口里声明；
#: 一周 20 篇已是高频创作者，超过的部分只影响清单明细、不影响计数与合计）
NEW_NOTES_LIMIT = 20

#: ``_unwrap_payload`` 的候选包裹键（模型偶发把报告对象包在这些键里）
_WRAPPER_KEYS: tuple[str, ...] = ("report", "data", "result", "weekly_report")


# ---------------------------------------------------------------------------
# 1) 周期解析（纯函数）
# ---------------------------------------------------------------------------
def _parse_bound(value: Any, *, is_end: bool) -> datetime:
    """解析周期端点：``YYYY-MM-DD``（整天）或 ISO 8601 时间。

    - 日期形式：起点取 ``00:00:00``、终点取 ``23:59:59.999999``（本地时区）；
    - 无时区的时间视为本地时区（Asia/Shanghai）；
    - 非法 → :class:`WeeklyContextError`（service 译 422）。
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        raise WeeklyContextError("周期时间不能为空")
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            day = date.fromisoformat(text)
            return datetime.combine(
                day, time.max if is_end else time.min, tzinfo=LOCAL_TIMEZONE
            )
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WeeklyContextError(
            f"周期时间格式非法：{value!r}（支持 YYYY-MM-DD 或 ISO 8601 时间）"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed


def resolve_period(
    *,
    period_start: Any = None,
    period_end: Any = None,
    days: Any = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime, int]:
    """解析并校验报告周期（纯函数）。

    规则（任务书 3.2）：

    - 默认 ``[now - (WEEKLY_DEFAULT_DAYS-1) 天 00:00, now]``；
    - ``days``：近 N 天（与显式起止**互斥**；非法 / 越界 → 错误）；
    - ``period_start`` / ``period_end``：ISO 时间或日期；只给一端时另一端
      缺省（start 缺 → 按默认天数向前推；end 缺 → 取 ``now``）；
    - ``start > end`` 非法；跨度（自然日，含首尾）1 ~ :data:`MAX_PERIOD_DAYS`。

    Returns:
        ``(start, end, span_days)``（均为本地时区 aware datetime）。
    """
    current = now or now_local()

    if days is not None and (period_start is not None or period_end is not None):
        raise WeeklyContextError("days 与 period_start / period_end 不能同时指定")

    if days is not None:
        try:
            days_value = int(days)
        except (TypeError, ValueError) as exc:
            raise WeeklyContextError(f"days 不是合法整数：{days!r}") from exc
        if days_value < 1 or days_value > MAX_PERIOD_DAYS:
            raise WeeklyContextError(f"days 越界（{days_value} 不在 [1, {MAX_PERIOD_DAYS}] 内）")
        start = datetime.combine(
            current.date() - timedelta(days=days_value - 1),
            time.min,
            tzinfo=LOCAL_TIMEZONE,
        )
        end = current
    elif period_start is not None or period_end is not None:
        start = _parse_bound(period_start, is_end=False) if period_start is not None else None
        end = _parse_bound(period_end, is_end=True) if period_end is not None else None
        if start is None:  # 只给了终点：按默认天数向前推整天
            default_days = max(1, int(settings.WEEKLY_DEFAULT_DAYS))
            start = datetime.combine(
                (end - timedelta(days=default_days - 1)).date(),
                time.min,
                tzinfo=LOCAL_TIMEZONE,
            )
        if end is None:  # 只给了起点：终点取当前时刻
            end = current
    else:
        default_days = max(1, int(settings.WEEKLY_DEFAULT_DAYS))
        start = datetime.combine(
            current.date() - timedelta(days=default_days - 1),
            time.min,
            tzinfo=LOCAL_TIMEZONE,
        )
        end = current

    if start > end:
        raise WeeklyContextError(
            f"周期起点晚于终点（{start.isoformat()} > {end.isoformat()}）"
        )

    span_days = max(1, -(-int((end - start).total_seconds()) // 86400))  # ceil
    if span_days > MAX_PERIOD_DAYS:
        raise WeeklyContextError(
            f"周期跨度为 {span_days} 天，超过上限 {MAX_PERIOD_DAYS} 天"
        )
    return start, end, span_days


# ---------------------------------------------------------------------------
# 2) 单笔记周期增量（纯函数）
# ---------------------------------------------------------------------------
def period_delta(
    snapshots: list[dict[str, Any]], *, start: datetime, end: datetime
) -> dict[str, int | None]:
    """单笔记的周期增量（每个指标一个数；无可用基线 → ``None``）。

    口径（诚实、不臆造）：

    - 主口径：``周期内最后一个非空值 − 周期前最后一个非空值``
      （周期前无基线则退化为下一条）；
    - 退化口径：周期内至少两条快照时，``周期内末值 − 周期内首值``
      （即「周期内观察到的增长」）；
    - 只有一条周期内快照且无周期前基线 → ``None``（不算增量）。
    """
    deltas: dict[str, int | None] = {field: None for field in METRIC_FIELDS}
    for field in METRIC_FIELDS:
        before: int | None = None
        first_in: int | None = None
        last_in: int | None = None
        for row in snapshots:
            value = row.get(field)
            if value is None:
                continue
            captured = as_datetime(row.get("captured_at"))
            if captured is None:
                continue
            if captured < start:
                before = int(value)
            elif captured <= end:
                if first_in is None:
                    first_in = int(value)
                last_in = int(value)

        if last_in is None:
            continue
        if before is not None:
            deltas[field] = last_in - before
        elif first_in is not None and first_in != last_in:
            deltas[field] = last_in - first_in
    return deltas


# ---------------------------------------------------------------------------
# 3) 周期事实构建（唯一触库函数；返回纯 dict）
# ---------------------------------------------------------------------------
def _count(db: Session, model: Any, *criteria: Any) -> int:
    """计数小工具（只读）。"""
    return int(
        db.execute(select(func.count()).select_from(model).where(*criteria)).scalar() or 0
    )


def build_period_facts(
    db: Session, *, period_start: datetime, period_end: datetime
) -> dict[str, Any]:
    """把周期内的聚合事实全部本地算好（只读；结果可直接进图状态）。

    Returns:
        dict（供提示词与 ``agent_runs.outputs`` 引用的**事实底稿**）：

        - ``period_start`` / ``period_end`` / ``period_days``；
        - ``counts``：周期内发布数 / 待发布（ready）/ 草稿（draft）/ 在写选题；
        - ``hotspot_topic_counts``：周期内热点采集数 / 当前累计被采用热点数 /
          周期内新建选题数（热点「被采用」无状态变更时间戳，只能给**当前累计**，
          字段名以 ``_total`` 明示）；
        - ``period_totals``：全账号 published 最新值合计 + 六率（**与看板同口径**）；
        - ``period_deltas``：全账号周期增量合计（各笔记增量求和；全无 → None）；
        - ``new_published_notes``：周期内新发布笔记清单（title / published_at /
          最新六指标 / 周期内增量）；
        - ``diagnostics``：周期内生成的诊断摘要（类型 / 评分 / 核心建议前 3 条）；
        - ``data_gaps``：本地收集的数据缺口（如实声明）。
    """
    start, end = period_start, period_end

    # ---- 计数 ----
    notes_published_in_period = _count(
        db,
        Note,
        Note.status == NoteStatus.PUBLISHED,
        Note.published_at.is_not(None),
        Note.published_at >= start,
        Note.published_at <= end,
    )
    notes_ready = _count(db, Note, Note.status == NoteStatus.READY)
    notes_draft = _count(db, Note, Note.status == NoteStatus.DRAFT)
    topics_writing = _count(db, Topic, Topic.status == TopicStatus.WRITING)
    topics_created_in_period = _count(
        db, Topic, Topic.created_at >= start, Topic.created_at <= end
    )
    hotspots_fetched_in_period = _count(
        db,
        Hotspot,
        Hotspot.fetched_at.is_not(None),
        Hotspot.fetched_at >= start,
        Hotspot.fetched_at <= end,
    )
    hotspots_adopted_total = _count(db, Hotspot, Hotspot.status == HotspotStatus.ADOPTED)

    # ---- 全量 published 笔记 + 快照（复用看板取数管道）----
    all_pub_rows = db.execute(
        select(Note.id)
        .where(Note.status == NoteStatus.PUBLISHED)
        .order_by(Note.created_at.desc(), Note.id.desc())
    ).all()
    all_pub_ids = [row[0] for row in all_pub_rows]

    snapshots = load_snapshots(db, all_pub_ids)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for snap in snapshots:
        grouped.setdefault(str(snap["note_id"]), []).append(snap)

    latest_by_note = latest_values_per_note(snapshots)
    period_totals = sum_funnel(latest_by_note)
    period_totals["published_notes"] = len(all_pub_ids)

    # ---- 周期增量（每笔记算一次，复用给「新发布清单」）----
    deltas_by_note: dict[str, dict[str, int | None]] = {
        str(note_id): period_delta(grouped.get(str(note_id), []), start=start, end=end)
        for note_id in all_pub_ids
    }
    period_deltas: dict[str, int | None] = {}
    for field in METRIC_FIELDS:
        values = [
            delta[field]
            for delta in deltas_by_note.values()
            if delta.get(field) is not None
        ]
        period_deltas[field] = sum(values) if values else None

    # ---- 周期内新发布笔记清单 ----
    pub_rows = db.execute(
        select(Note.id, Note.title, Note.published_at)
        .where(
            Note.status == NoteStatus.PUBLISHED,
            Note.published_at.is_not(None),
            Note.published_at >= start,
            Note.published_at <= end,
        )
        .order_by(Note.published_at.desc(), Note.id.desc())
        .limit(NEW_NOTES_LIMIT)
    ).all()
    empty_latest = {field: None for field in METRIC_FIELDS}
    new_published_notes: list[dict[str, Any]] = []
    for note_id, title, published_at in pub_rows:
        key = str(note_id)
        new_published_notes.append(
            {
                "note_id": key,
                "title": title,
                "published_at": published_at.isoformat() if published_at else None,
                "latest": dict(latest_by_note.get(key) or empty_latest),
                "deltas": dict(deltas_by_note.get(key) or empty_latest),
            }
        )

    # ---- 周期内诊断摘要（类型 / 评分 / 核心建议前 N 条）----
    diag_rows = db.execute(
        select(
            Diagnostic.note_id,
            Diagnostic.diagnosis_type,
            Diagnostic.score,
            Diagnostic.suggestions,
            Diagnostic.created_at,
        )
        .where(Diagnostic.created_at >= start, Diagnostic.created_at <= end)
        .order_by(Diagnostic.created_at.desc(), Diagnostic.id.desc())
        .limit(DIAG_SUMMARY_LIMIT)
    ).all()
    diagnostics: list[dict[str, Any]] = [
        {
            "note_id": str(row[0]),
            "diagnosis_type": row[1].value if row[1] is not None else None,
            "score": row[2],
            "suggestions": [str(item) for item in (row[3] or [])][:DIAG_SUGGESTION_TAIL],
            "created_at": row[4].isoformat() if row[4] else None,
        }
        for row in diag_rows
    ]

    # ---- 数据缺口（如实声明，绝不硬造亮点）----
    covered = int(period_totals.get("covered_notes") or 0)
    data_gaps: list[str] = []
    if notes_published_in_period == 0:
        data_gaps.append("周期内没有新发布笔记，本次只能基于存量笔记复盘")
    if not all_pub_ids:
        data_gaps.append("当前没有已发布笔记，无法做数据复盘")
    elif not snapshots:
        data_gaps.append("已发布笔记均没有数据快照，无法做数据复盘（请先在「数据回填」补齐）")
    elif covered < len(all_pub_ids):
        data_gaps.append(f"仅 {covered}/{len(all_pub_ids)} 篇已发布笔记有指标数据")
    if not diagnostics:
        data_gaps.append("周期内没有单篇诊断记录，缺少逐篇视角")
    if all(value is None for value in period_deltas.values()):
        data_gaps.append("周期内无法计算增量（缺少周期前基线或周期内快照）")
    if notes_published_in_period > len(new_published_notes):
        data_gaps.append(
            f"周期内新发布笔记较多（{notes_published_in_period} 篇），清单仅列最近 {NEW_NOTES_LIMIT} 篇"
        )

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_days": max(1, -(-int((end - start).total_seconds()) // 86400)),
        "counts": {
            "notes_published_in_period": notes_published_in_period,
            "notes_ready": notes_ready,
            "notes_draft": notes_draft,
            "topics_writing": topics_writing,
        },
        "hotspot_topic_counts": {
            "hotspots_fetched_in_period": hotspots_fetched_in_period,
            "hotspots_adopted_total": hotspots_adopted_total,
            "topics_created_in_period": topics_created_in_period,
        },
        "period_totals": period_totals,
        "period_deltas": period_deltas,
        "new_published_notes": new_published_notes,
        "diagnostics": diagnostics,
        "data_gaps": data_gaps,
    }


# ---------------------------------------------------------------------------
# 4) 报告归一（纯函数）
# ---------------------------------------------------------------------------
def _unwrap_payload(raw: Any) -> Any:
    """宽容解包：模型偶发把报告对象包在 report / data / result 等键里。"""
    if isinstance(raw, dict):
        for key in _WRAPPER_KEYS:
            inner = raw.get(key)
            if isinstance(inner, dict) and any(
                probe in inner
                for probe in ("summary", "highlights", "issues", "next_week_suggestions")
            ):
                return inner
    return raw


def normalize_report_payload(
    raw: Any,
    *,
    period_start: str,
    period_end: str,
    max_items: int = REPORT_MAX_LIST_ITEMS,
    text_max_len: int = REPORT_TEXT_MAX_LEN,
    summary_max_len: int = REPORT_SUMMARY_MAX_LEN,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """把模型原始 JSON 归一成周报载荷（纯函数，可单测）。

    规则：

    - ``period_start`` / ``period_end``：**以程序事实为准**直接填入
      （模型回显错误也不采信）；
    - ``summary``：必填字符串（超长截断）；
    - ``highlights`` / ``issues`` / ``data_evidence``：可选文本数组
      （条数 / 长度宽容截断）；
    - ``next_week_suggestions``：**必填**（为空 → 硬伤触发回炉）；
    - ``data_gaps``：模型声明的补充缺口（与本地缺口在 persist 时合并去重）；
    - ``confidence``：0~1（越界 / 非法 → 置 None 并记 warning，不打回）。

    Returns:
        ``(normalized, errors, warnings)``——``errors`` 为硬伤（回炉 / 放弃），
        ``warnings`` 为宽容修正记录。
    """
    warnings: list[str] = []
    data = _unwrap_payload(raw)
    empty: dict[str, Any] = {
        "period_start": period_start,
        "period_end": period_end,
        "summary": "",
        "highlights": [],
        "issues": [],
        "data_evidence": [],
        "next_week_suggestions": [],
        "data_gaps": [],
        "confidence": None,
    }
    if not isinstance(data, dict):
        return empty, ["模型输出不是 JSON 对象"], warnings

    errors: list[str] = []

    summary_text = ""
    raw_summary = data.get("summary")
    if isinstance(raw_summary, str):
        summary_text = raw_summary.strip()
    elif raw_summary is not None:
        errors.append(f"summary 类型非法（应为字符串，实际 {type(raw_summary).__name__}）")
    if not summary_text and not any(
        item.startswith("summary 类型非法") for item in errors
    ):
        errors.append("summary 缺失或为空")
    if len(summary_text) > summary_max_len:
        warnings.append(f"summary 超长已截断（>{summary_max_len} 字）")
        summary_text = summary_text[:summary_max_len]

    highlights, list_errors, list_warnings = _coerce_text_list(
        "highlights",
        data.get("highlights"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=False,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    issues, list_errors, list_warnings = _coerce_text_list(
        "issues",
        data.get("issues"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=False,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    data_evidence, list_errors, list_warnings = _coerce_text_list(
        "data_evidence",
        data.get("data_evidence"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=False,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    suggestions, list_errors, list_warnings = _coerce_text_list(
        "next_week_suggestions",
        data.get("next_week_suggestions"),
        max_items=max_items,
        text_max_len=text_max_len,
        required=True,
    )
    errors.extend(list_errors)
    warnings.extend(list_warnings)

    model_gaps, list_errors, list_warnings = _coerce_text_list(
        "data_gaps",
        data.get("data_gaps"),
        max_items=DATA_GAPS_LIMIT,
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
            else:
                warnings.append(f"confidence 越界已忽略（{number} 不在 [0, 1] 内）")
        except (TypeError, ValueError):
            warnings.append("confidence 无法解析为数字，已置空")

    normalized: dict[str, Any] = {
        "period_start": period_start,
        "period_end": period_end,
        "summary": summary_text,
        "highlights": highlights,
        "issues": issues,
        "data_evidence": data_evidence,
        "next_week_suggestions": suggestions,
        "data_gaps": model_gaps,
        "confidence": confidence,
    }
    return normalized, errors, warnings


# ---------------------------------------------------------------------------
# 5) 专属异常（service 按类型翻译统一错误体）
# ---------------------------------------------------------------------------
class WeeklyReportError(AgentNonRetryableError):
    """周报业务错误基类。（继承 AgentNonRetryableError：绝不进入节点重试。）"""


class WeeklyContextError(WeeklyReportError):
    """上下文 / 周期参数不合法 → 422（由 service 翻译；零 run 产生）。"""


class WeeklyGiveUpError(WeeklyReportError):
    """周报放弃：结构校验连续超限 → 400（可重试）。"""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


__all__ = [
    "DATA_GAPS_LIMIT",
    "DIAG_SUGGESTION_TAIL",
    "DIAG_SUMMARY_LIMIT",
    "MAX_PERIOD_DAYS",
    "NEW_NOTES_LIMIT",
    "REPORT_MAX_LIST_ITEMS",
    "REPORT_SUMMARY_MAX_LEN",
    "REPORT_TEXT_MAX_LEN",
    "WeeklyContextError",
    "WeeklyGiveUpError",
    "WeeklyReportError",
    "build_period_facts",
    "merge_data_gaps",
    "normalize_report_payload",
    "period_delta",
    "resolve_period",
]
