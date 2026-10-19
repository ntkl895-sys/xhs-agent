"""爆款规律提炼契约（第 18 步）：取数事实 / 纯归一 / 专属异常 / 常量。

三部分：

1. **取数事实** :func:`build_insight_facts` —— 只读本账号**本地**数据（published
   笔记 + note_metrics + diagnostics），把每篇笔记转成**普通 dict**（可 JSON
   序列化进检查点）。六指标「每指标最后一个非空值」复用第 16 步
   ``diagnosis.contracts.metric_latest``；六率复用 ``compute_rates``；增量复用
   ``compute_trends`` 的总增量口径 —— **绝不重复实现第二套口径**。
2. **纯归一** :func:`normalize_patterns` —— 把模型输出归一成固定结构（逐条剔除
   不合格项并记录原因；「全军覆没」才算硬伤触发有限回路）；置信度越界宽容置空。
3. **专属异常**：继承 ``AgentNonRetryableError``（不消耗节点重试额度），由
   service 统一翻译成统一错误体。

红线（自检断言）：规律只来自本账号本地数据 —— 提示词明文禁止外部数字
（行业均值 / 爆款门槛）；样本不足**不硬造规律**（``sufficient=False``，图走
特定收尾，run 记 partial，零模型调用）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.diagnosis.contracts import compute_rates, compute_trends, metric_latest
from app.agent.errors import AgentNonRetryableError
from app.models.diagnostic import Diagnostic
from app.models.enums import NoteStatus
from app.models.note import Note
from app.services.dashboard.service import load_snapshots

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 单次提炼最多纳入的笔记数（按发布时间倒序取最近 N 篇；受上下文体积与检查点
#: 体积约束的经验上限，非平台口径）
MAX_NOTES = 20

#: 每篇笔记正文摘录的最大字数（进状态；全文不必要且体积大）
BODY_EXCERPT_MAX_LEN = 400

#: 每篇笔记诊断摘要：每个数组最多取几条 / 单条最大字数
DIAG_ITEMS_LIMIT = 4
DIAG_ITEM_MAX_LEN = 120

#: 允许的规律类型（写入 knowledge_snippets.chunk_type 的两个取值）
PATTERN_TYPES: tuple[str, ...] = ("hit_pattern", "review_insight")

#: 模型可能把数组包在这些键里（宽容解包，只解一层）
_WRAPPER_KEYS = ("result", "data", "output", "insights")

#: 六指标（扫描 evidence 文本时用；与 diagnosis 口径一致）
_METRIC_NAMES: tuple[str, ...] = (
    "views",
    "likes",
    "collects",
    "comments",
    "shares",
    "new_followers",
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _iso(value: Any) -> str | None:
    """datetime / 字符串 → ISO 串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _truncate(text: Any, limit: int) -> str:
    """非字符串先 str()；超长截断（不做省略号修饰，保持原文可检索）。"""
    value = str(text or "").strip()
    return value[:limit]


def _diag_items(value: Any) -> list[str]:
    """诊断摘要数组归一：条数与单条长度双限。"""
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for raw in value:
        text = _truncate(raw, DIAG_ITEM_MAX_LEN)
        if text:
            items.append(text)
        if len(items) >= DIAG_ITEMS_LIMIT:
            break
    return items


# ---------------------------------------------------------------------------
# 1) 取数事实（只读；ORM 行 → 普通 dict）
# ---------------------------------------------------------------------------
def build_insight_facts(
    db: Session,
    *,
    min_published: int,
    max_notes: int = MAX_NOTES,
) -> dict[str, Any]:
    """载入提炼所需的全部本地事实（published 笔记 + 指标 + 六率 + 增量 + 诊断摘要）。

    全部为本地确定性计算（模型零算术）。样本不足时 ``sufficient=False``
    且 ``data_gaps`` 给出「至少发布 N 篇并回填数据后再提炼」的明确声明。

    Returns:
        ``{sample_size, min_required, sufficient, notes, data_range, data_gaps}``：

        - ``notes``：普通 dict 列表（按发布时间倒序，最多 ``max_notes`` 篇），
          每篇含 ``note_id / title / tags / published_at / body_excerpt /
          metrics（最新值）/ rates（六率）/ total_deltas（首→最新总增量）/
          diagnosis（最新一条诊断的摘要；无 → None）``；
        - ``data_range``：样本的 ``earliest_/latest_published_at``；
        - ``data_gaps``：如实声明的数据缺口（中文）。
    """
    # ---- published 笔记（顺序确定：published_at 倒序 nulls last + created_at + id）----
    notes = (
        db.execute(
            select(Note)
            .where(Note.status == NoteStatus.PUBLISHED)
            .order_by(
                Note.published_at.desc().nulls_last(),
                Note.created_at.desc(),
                Note.id.desc(),
            )
            .limit(max(1, int(max_notes)))
        )
        .scalars()
        .all()
    )
    total_published = db.execute(
        select(Note.id).where(Note.status == NoteStatus.PUBLISHED)
    ).all()
    sample_size = len(total_published)

    note_ids = [row.id for row in notes]
    snapshots = load_snapshots(db, note_ids)  # 复用第 17 步唯一取数管道
    grouped: dict[str, list[dict[str, Any]]] = {str(nid): [] for nid in note_ids}
    for row in snapshots:
        grouped.setdefault(str(row["note_id"]), []).append(row)

    # ---- 每篇最新一条诊断（id 兜底保证顺序稳定）----
    diag_map: dict[str, Diagnostic] = {}
    if note_ids:
        diags = (
            db.execute(
                select(Diagnostic)
                .where(Diagnostic.note_id.in_(note_ids))
                .order_by(Diagnostic.note_id.asc(), Diagnostic.created_at.desc(), Diagnostic.id.desc())
            )
            .scalars()
            .all()
        )
        for row in diags:
            key = str(row.note_id)
            if key not in diag_map:
                diag_map[key] = row

    # ---- 逐篇组装（纯 dict）----
    notes_payload: list[dict[str, Any]] = []
    without_snapshots = 0
    without_diagnosis = 0
    published_ats: list[str] = []
    for note in notes:
        key = str(note.id)
        rows = grouped.get(key) or []
        latest, _latest_id = metric_latest(rows)
        rates = compute_rates(rows)
        trends = compute_trends(rows, note.published_at)
        diag = diag_map.get(key)
        diagnosis: dict[str, Any] | None = None
        if diag is not None:
            diagnosis = {
                "diagnosis_type": diag.diagnosis_type.value if diag.diagnosis_type else None,
                "score": diag.score,
                "strengths": _diag_items(diag.strengths),
                "weaknesses": _diag_items(diag.weaknesses),
                "suggestions": _diag_items(diag.suggestions),
            }
        else:
            without_diagnosis += 1
        if not rows:
            without_snapshots += 1
        if note.published_at is not None:
            published_ats.append(note.published_at.isoformat())

        notes_payload.append(
            {
                "note_id": key,
                "title": _truncate(note.title, 100),
                "tags": list(note.tags or []),
                "published_at": _iso(note.published_at),
                "body_excerpt": _truncate(note.body, BODY_EXCERPT_MAX_LEN),
                "metrics": latest,
                "rates": rates,
                "total_deltas": trends["total_deltas"],
                "diagnosis": diagnosis,
            }
        )

    data_range = {
        "earliest_published_at": min(published_ats) if published_ats else None,
        "latest_published_at": max(published_ats) if published_ats else None,
    }

    # ---- 数据缺口（如实声明；样本不足的话术与任务书一致）----
    data_gaps: list[str] = []
    sufficient = sample_size >= max(1, int(min_published))
    if not sufficient:
        data_gaps.append(
            f"样本不足：至少发布 {max(1, int(min_published))} 篇并回填数据后再提炼"
            f"（当前仅 {sample_size} 篇已发布笔记）"
        )
    if sample_size == 0:
        data_gaps.append("尚无已发布笔记：规律提炼需要真实运营数据")
    if without_snapshots:
        data_gaps.append(f"{without_snapshots} 篇笔记尚未回填任何运营数据（无快照）")
    if without_diagnosis:
        data_gaps.append(f"{without_diagnosis} 篇笔记尚无诊断记录")
    if sample_size > len(notes_payload):
        data_gaps.append(
            f"本次仅纳入最近 {len(notes_payload)} 篇（共 {sample_size} 篇已发布），"
            "更多笔记请分批积累后再提炼"
        )

    return {
        "sample_size": sample_size,
        "min_required": max(1, int(min_published)),
        "sufficient": sufficient,
        "notes": notes_payload,
        "data_range": data_range,
        "data_gaps": data_gaps,
    }


# ---------------------------------------------------------------------------
# 2) 模型输出归一（纯函数）
# ---------------------------------------------------------------------------
def _unwrap_patterns(raw: Any) -> Any:
    """宽容解包：模型偶发把数组包在 result / data 等键里。"""
    if isinstance(raw, dict):
        direct = raw.get("patterns")
        if isinstance(direct, list):
            return direct
        for key in _WRAPPER_KEYS:
            inner = raw.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("patterns"), list):
                return inner["patterns"]
        if isinstance(direct, dict):  # {"patterns": {"pattern": ...}} 单条对象
            return [direct]
    if isinstance(raw, list):
        return raw
    return None


def _scan_metrics(text: str) -> list[str]:
    """从证据文本中扫出出现的六指标名（确定性；只用于 meta 标注）。"""
    return [name for name in _METRIC_NAMES if name in text]


def normalize_patterns(
    raw: Any,
    *,
    valid_note_ids: set[str],
    max_items: int,
    text_max_len: int,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """把模型原始 JSON 归一成规律载荷（纯函数，可单测）。

    Args:
        raw: 模型输出的 JSON（未经校验）。
        valid_note_ids: 允许作为来源的笔记 id 集合（本账号本次样本）——
            模型编造的 id 一律过滤。
        max_items: 规律条数上限（超出截断，warning）。
        text_max_len: 单条文本（规律 / 场景 / 证据）最大字数（超出截断，warning）。

    Returns:
        ``(normalized, errors, warnings)``：

        - ``normalized``：``{"patterns": [...], "rejected": [...]}`` —— 合格条目
          固定为 ``{pattern, pattern_type, scenario, source_note_ids, evidence,
          confidence}``；``rejected`` 为被剔除条目及原因（``pattern`` 取前 30 字，
          ``reason`` 为可读中文）；
        - ``errors``：**硬伤**（触发有限回路回炉）—— JSON 非对象 / 数组缺失 /
          全部条目被剔除（无可用规律）；
        - ``warnings``：宽容修正记录（截断 / 置信度置空 / 类型归一 / 逐条剔除）。
    """
    warnings: list[str] = []
    items = _unwrap_patterns(raw)
    if items is None:
        return (
            {"patterns": [], "rejected": []},
            ["模型输出不是包含 patterns 数组的 JSON 对象"],
            warnings,
        )
    if not items:
        return (
            {"patterns": [], "rejected": []},
            ["patterns 为空数组（至少需要一条合格规律）"],
            warnings,
        )
    if len(items) > max_items:
        warnings.append(f"规律条数超限已截断（{len(items)} → {max_items}）")
        items = items[:max_items]

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items, start=1):
        if not isinstance(raw_item, dict):
            rejected.append({"pattern": f"（第 {index} 条）", "reason": "不是 JSON 对象"})
            continue

        pattern = _truncate(raw_item.get("pattern"), text_max_len)
        if len(str(raw_item.get("pattern") or "").strip()) > text_max_len:
            warnings.append(f"第 {index} 条规律文本超长已截断（>{text_max_len} 字）")
        if not pattern:
            rejected.append({"pattern": f"（第 {index} 条）", "reason": "缺少规律文本 pattern"})
            continue

        scenario_raw = str(raw_item.get("scenario") or "").strip()
        if not scenario_raw:
            rejected.append({"pattern": pattern[:30], "reason": "缺少适用场景 scenario"})
            continue
        scenario = _truncate(scenario_raw, text_max_len)

        # 来源过滤：只能引用本次样本中的笔记 id
        source_ids: list[str] = []
        raw_ids = raw_item.get("source_note_ids")
        if isinstance(raw_ids, list):
            for value in raw_ids:
                text = str(value).strip()
                if text in valid_note_ids and text not in source_ids:
                    source_ids.append(text)
        if not source_ids:
            rejected.append(
                {
                    "pattern": pattern[:30],
                    "reason": "支撑来源缺失（source_note_ids 为空或均不在本次样本内）",
                }
            )
            continue

        evidence_raw = str(raw_item.get("evidence") or "").strip()
        if not evidence_raw:
            rejected.append({"pattern": pattern[:30], "reason": "缺少指标证据 evidence"})
            continue
        evidence = _truncate(evidence_raw, text_max_len)

        pattern_type = str(raw_item.get("pattern_type") or "").strip().lower()
        if pattern_type not in PATTERN_TYPES:
            if pattern_type:
                warnings.append(f"第 {index} 条 pattern_type 非法（已归一为 hit_pattern）")
            pattern_type = PATTERN_TYPES[0]

        confidence: float | None = None
        raw_confidence = raw_item.get("confidence")
        if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
            number = float(raw_confidence)
            if 0.0 <= number <= 1.0:
                confidence = round(number, 3)
        if confidence is None:
            warnings.append(f"第 {index} 条置信度缺失或越界（已置空，不臆造）")

        accepted.append(
            {
                "pattern": pattern,
                "pattern_type": pattern_type,
                "scenario": scenario,
                "source_note_ids": source_ids,
                "evidence": evidence,
                "confidence": confidence,
            }
        )

    if rejected:
        warnings.append(f"有 {len(rejected)} 条不合格规律被剔除（原因见 rejected）")

    errors: list[str] = []
    if not accepted:
        errors.append(
            f"共 {len(items)} 条规律全部不合格（缺少规律文本 / 适用场景 / "
            "有效来源 note_id / 指标证据），没有可写入的规律"
        )

    normalized = {"patterns": accepted, "rejected": rejected}
    return normalized, errors, warnings


def scan_evidence_metrics(evidence: str) -> list[str]:
    """公开小工具：扫描证据文本中的六指标名（构建 meta.evidence 用）。"""
    return _scan_metrics(evidence)


# ---------------------------------------------------------------------------
# 3) 专属异常（service 按类型翻译统一错误体）
# ---------------------------------------------------------------------------
class InsightsError(AgentNonRetryableError):
    """规律提炼业务错误基类。（继承 AgentNonRetryableError：绝不进入节点重试。）"""


class InsightsContextError(InsightsError):
    """上下文不满足（防御性；service 译 422）。"""


class InsightsGiveUpError(InsightsError):
    """提炼放弃：结构重试超限 / 无可落库规律 → 400（可重试）。"""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


def new_uuid(value: Any) -> uuid.UUID | None:
    """宽容地把字符串转 UUID（失败 → None；供自检与防御使用）。"""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "BODY_EXCERPT_MAX_LEN",
    "DIAG_ITEMS_LIMIT",
    "DIAG_ITEM_MAX_LEN",
    "MAX_NOTES",
    "PATTERN_TYPES",
    "InsightsContextError",
    "InsightsError",
    "InsightsGiveUpError",
    "build_insight_facts",
    "new_uuid",
    "normalize_patterns",
    "scan_evidence_metrics",
]
