"""数据回填图：在 LangGraph harness 上的**第三个业务图**（第 15 步）。

链路（单张截图 → 六个指标的候选值，**不做落库**）：

    START
      │
    load_image            校验上传文件（mime 白名单 + 文件头魔数 + 大小），
      │                   转成 data URL（闭包暂存，绝不入状态）；
      │                   带 note_id 时校验笔记存在并载入标题（人工核对）
      │
    transcribe            视觉模型（want_vision）只读转录；JSON 容错与计数由
      │                   invoke_json 负责；「模型不支持图片」→ 返回标记
      │
      ├── 解析成功 ──────────────────────────────────────────► normalize_validate ─► END
      ├── 解析失败且 retry_count < AGENT_JSON_MAX_ATTEMPTS ──► transcribe（有限回路）
      └── 解析失败超限 / 模型不支持图片 ─────────────────────► give_up ─► 抛错（run 记 failed）

要点：

- **真 LangGraph**：多节点 + ``add_conditional_edges`` 条件边 + 有限回路 +
  检查点（由 ``compile_graph`` 绑定）；
- **视觉模型与图片字节都经闭包注入**，绝不进状态——图片是二进制大对象，
  放进状态会污染检查点（每一步都会把整张图写进 ``langgraph`` schema）；
- **图不做 persist**：识别结果只作为 ``output`` 返回，等待前端人工核对后
  才走 ``POST /api/v1/metrics`` 落库（人工确认是硬规则）；
- 只保留六个指标（``views / likes / collects / comments / shares / new_followers``），
  模型输出的其它字段（impressions / clicks / ctr / 时长等）一律忽略——
  它们落不进 ``note_metrics`` 表，也不作为指标字段返回。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START

from app.agent.errors import AgentJSONError
from app.agent.graph.base import compile_graph, llm_retry_policy, new_state_graph
from app.agent.metrics.contracts import (
    METRIC_ALIASES,
    METRIC_FIELDS,
    ImageValidationError,
    MetricRecognitionPayload,
    NoteMissingError,
    RecognitionGiveUpError,
    VisionUnsupportedError,
    detect_image_mime,
)
from app.agent.metrics.prompts import METRIC_PROMPT_VERSION, build_messages
from app.agent.metrics.state import MetricState
from app.agent.parsing.json_repair import invoke_json
from app.agent.state.base import merge_parse_attempts, merge_usage
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.note import Note

logger = logging.getLogger("xhs-agent.metrics.graph")

#: 节点名（自检会断言四个节点齐全）
NODE_LOAD_IMAGE = "load_image"
NODE_TRANSCRIBE = "transcribe"
NODE_NORMALIZE = "normalize_validate"
NODE_GIVE_UP = "give_up"

#: 模型可能把载荷包在这些键里（宽容解包，只解一层）
_WRAPPER_KEYS = ("data", "result", "output", "transcription")

#: 「模型不支持图片」的接口报错关键词（移植自 screenshot_spike/vision/client.py）
_VISION_UNSUPPORTED_PATTERNS = (
    r"vision",
    r"multi[- ]?modal",
    r"image[s]?\s*(input|content|url)?\s*(is\s*)?not\s*support",
    r"not\s*support(ed)?\s*(the\s*)?(image|vision|image_url)",
    r"does\s*n[o']?t\s*support\s*(image|vision)",
    r"不支持.{0,8}(图片|视觉|多模态)",
    r"(图片|视觉|多模态).{0,8}不支持",
    r"unsupported\s*(content|media)\s*type",
)
_VISION_UNSUPPORTED_REGEX = re.compile("|".join(_VISION_UNSUPPORTED_PATTERNS), re.IGNORECASE)

#: 转录原文里的空占位符（→ 视为 None）
_PLACEHOLDERS = {"-", "--", "—", "–", "－", "暂无", "?", "？", "无", "n/a"}

#: 中文 / 英文数量单位 → 倍数
_UNIT_MULTIPLIER = {
    "万": 10_000,
    "w": 10_000,
    "W": 10_000,
    "亿": 100_000_000,
    "千": 1_000,
    "k": 1_000,
    "K": 1_000,
}

#: 数字 + 可选单位（用于从「1.2万+」这类原文里换算出数值）
_COUNT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(亿|万|千|w|W|k|K)?")

#: 本地 warnings 上限（防止脏输出把 warnings 撑爆）
_MAX_WARNINGS = 12


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def looks_like_vision_unsupported(text: str) -> bool:
    """接口报错文本是否在说「当前模型不支持图片 / 视觉 / 多模态」。"""
    return bool(_VISION_UNSUPPORTED_REGEX.search(text or ""))


def _parse_count(text: Any) -> int | None:
    """把截图原文换算成整数计数；占位符 / 无法换算 → ``None``（绝不臆造）。

    规则：去掉千分位（半角 / 全角逗号）与空白；``1.2万 / 1.2w → 12000``、
    ``1.2亿 → 120000000``、``3.4千 / 3.4k → 3400``；``--/—/暂无/?`` → None；
    负数开头与含 ``%`` 的文本按无效处理（六个指标全部是「计数」语义）。
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    cleaned = raw.replace(",", "").replace("，", "").replace("\u00a0", "").replace(" ", "")
    if not cleaned or cleaned.lower() in _PLACEHOLDERS:
        return None
    if cleaned[0] in {"-", "—", "–", "－"}:
        # 负值前缀：半角减号 / em / en / 全角减号（U+FF0D）。
        # 全角减号一项由测试集评测（bench_testset_normalize，norm_104）发现补入：
        # 原实现漏判「－5」为负数，误得 5；修复后按「负数无效」返回 None。
        return None
    if "%" in cleaned:
        return None
    match = _COUNT_PATTERN.search(cleaned)
    if match is None:
        return None
    number = float(match.group(1))
    unit = match.group(2) or ""
    return int(round(number * _UNIT_MULTIPLIER.get(unit, 1)))


def _canonical_field(name: Any) -> str | None:
    """把模型声明的字段名（含 ``notes[0].clicks`` 之类前缀）对齐到六字段之一。"""
    if name is None:
        return None
    token = str(name).strip().lower()
    if "." in token:
        token = token.rsplit(".", 1)[-1]
    token = token.strip("[]（）() \t")
    return METRIC_ALIASES.get(token)


def _canonical_set(names: list[str]) -> set[str]:
    """批量对齐；无法对齐（或对齐到六字段之外）的丢弃。"""
    result: set[str] = set()
    for item in names:
        canonical = _canonical_field(item)
        if canonical in METRIC_FIELDS:
            result.add(canonical)
    return result


def _unwrap_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """宽容解包：模型偶发把 ``metrics`` 包在 data / result 等键里。"""
    if "metrics" in raw:
        return raw
    for key in _WRAPPER_KEYS:
        inner = raw.get(key)
        if isinstance(inner, dict) and "metrics" in inner:
            return inner
    return raw


# ---------------------------------------------------------------------------
# normalize：本地别名归一 + 数字换算 + evidence 生成（纯函数，可单测）
# ---------------------------------------------------------------------------
def normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """把模型原始 JSON 归一成「六指标 + evidence + missing / uncertain + warnings」。

    Args:
        raw: 模型输出的 JSON 对象（未经校验）。

    Returns:
        ``{metrics, evidence, missing_fields, uncertain_fields, warnings, captured_at}``

    归一规则（全部本地确定性完成，不追问模型）：

    - 键名按 :data:`METRIC_ALIASES` 对齐到六字段；六字段之外的键（impressions /
      clicks / …）记入 ``evidence.ignored_fields`` 并忽略；
    - ``raw`` 原文里带单位 / 千分位的数字 → 换算成整数；占位符 → None；
    - ``raw`` 与模型给的 ``value`` 冲突时**以 raw 换算为准**，并把该字段标记为
      uncertain（raw 是截图上的原文，换算规则是确定性的）；
    - ``raw`` 无法换算但有 ``value`` 时暂采 value 并标记 uncertain；
    - 最终仍为 None 的字段进 ``missing_fields``；「有值但不稳」的字段进
      ``uncertain_fields``（每字段的 missing / uncertain 明细都在 evidence 里）。
    """
    if not isinstance(raw, dict):
        raw = {}
        payload = MetricRecognitionPayload()
        structure_warning = "模型输出不是 JSON 对象，已按全部缺失处理"
    else:
        structure_warning = ""
        try:
            payload = MetricRecognitionPayload.model_validate(_unwrap_payload(raw))
        except Exception:  # noqa: BLE001 —— 极端的脏结构：整体降级为「全部缺失」，不炸节点
            payload = MetricRecognitionPayload()
            structure_warning = "模型输出结构无法解析，已按全部缺失处理"

    # --- 1) 键名对齐 + 记录被忽略的字段 ---
    mapped: dict[str, Any] = {}
    ignored: list[str] = []
    for key, item in payload.metrics.items():
        canonical = METRIC_ALIASES.get(str(key).strip().lower())
        if canonical is None:
            ignored.append(str(key).strip())
        elif canonical not in mapped:
            mapped[canonical] = item
    for key in _unwrap_payload(raw).keys():
        if key not in ("metrics", "missing_fields", "uncertain_fields", "overall_confidence", "warnings"):
            ignored.append(str(key))

    model_missing = _canonical_set(payload.missing_fields)
    model_uncertain = _canonical_set(payload.uncertain_fields)

    # --- 2) 逐字段换算 ---
    metrics: dict[str, int | None] = {}
    uncertain_flags: set[str] = set()
    local_warnings: list[str] = []
    for field in METRIC_FIELDS:
        item = mapped.get(field)
        raw_text = item.raw if item is not None else None
        value_number = item.value if item is not None else None

        from_raw = _parse_count(raw_text)
        from_value: int | None = None
        if value_number is not None:
            if value_number < 0:
                local_warnings.append(f"{field}: 模型给出的 value 为负数（{value_number}），按无效处理")
            else:
                from_value = int(round(value_number))

        if from_raw is not None:
            chosen = from_raw
            if from_value is not None and from_value != from_raw:
                uncertain_flags.add(field)
                local_warnings.append(
                    f"{field}: raw（{raw_text}）与 value（{value_number}）不一致，已按 raw 换算为准"
                )
        elif from_value is not None:
            chosen = from_value
            if raw_text:
                uncertain_flags.add(field)
                local_warnings.append(f"{field}: raw（{raw_text}）无法换算，暂采用模型给的 value")
        else:
            chosen = None
        metrics[field] = chosen
        if field in model_uncertain:
            uncertain_flags.add(field)

    missing_fields = [field for field in METRIC_FIELDS if metrics[field] is None]
    uncertain_fields = [
        field for field in METRIC_FIELDS if field in uncertain_flags and metrics[field] is not None
    ]

    # --- 3) warnings 汇总（模型 + 本地；本地优先展示）---
    warnings: list[str] = []
    if structure_warning:
        warnings.append(structure_warning)
    if ignored:
        sample = "、".join(ignored[:8])
        more = " 等" if len(ignored) > 8 else ""
        warnings.append(f"已忽略不在本表口径的字段 {len(ignored)} 个：{sample}{more}")
    warnings.extend(local_warnings)
    warnings.extend(str(item) for item in payload.warnings)
    warnings = list(dict.fromkeys(warnings))[:_MAX_WARNINGS]

    # --- 4) evidence（每字段来源 / 置信度 / missing / uncertain 标记）---
    fields_evidence: dict[str, Any] = {}
    for field in METRIC_FIELDS:
        item = mapped.get(field)
        fields_evidence[field] = {
            "raw": item.raw if item is not None else None,
            "value": metrics[field],
            "source": "screenshot",
            "confidence": item.confidence if item is not None else None,
            "note": item.note if item is not None else None,
            "missing": metrics[field] is None,
            "uncertain": field in uncertain_flags,
        }
    evidence = {
        "fields": fields_evidence,
        "overall_confidence": payload.overall_confidence,
        "ignored_fields": sorted(set(ignored)),
        "model_missing_fields": sorted(model_missing),
        "model_uncertain_fields": sorted(model_uncertain),
        "prompt_version": METRIC_PROMPT_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "metrics": metrics,
        "evidence": evidence,
        "missing_fields": missing_fields,
        "uncertain_fields": uncertain_fields,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------
def build_metric_graph(
    *,
    model: BaseChatModel,
    image_bytes: bytes,
    image_mime: str,
    image_name: str | None = None,
    use_checkpointer: bool = True,
    checkpointer: Any | None = None,
) -> Any:
    """构建并编译「单张截图 → 六指标候选」业务图。

    Args:
        model: 已注入的视觉 ChatModel（真实 ``ChatOpenAI(want_vision=True)`` 或离线假模型）。
            **经闭包注入节点，绝不放进状态**（状态要序列化进检查点）。
        image_bytes / image_mime / image_name: 上传图片的字节、声明的 MIME 与文件名；
            同样经闭包注入（图片是二进制大对象，进状态会污染检查点）。
        use_checkpointer: 是否绑定检查点（默认绑定）。
        checkpointer: 显式指定检查点实现；``None`` 表示取全局单例。

    Returns:
        CompiledStateGraph
    """
    allowed_types = settings.metric_allowed_image_types
    max_bytes = settings.METRIC_MAX_IMAGE_BYTES

    # 闭包暂存区：data URL 只在这里，不写状态、不写检查点、不进 output（隐私 & 体积）
    ctx: dict[str, Any] = {}

    # ------------------------------------------------------------- load_image
    def load_image_node(state: MetricState) -> dict[str, Any]:
        """校验上传文件（mime 白名单 + 文件头魔数 + 大小），转 data URL，载入笔记标题。"""
        started = time.perf_counter()

        declared = (image_mime or "").strip().lower()
        if declared not in allowed_types:
            raise ImageValidationError(
                f"不支持的图片类型「{declared or '未知'}」，"
                f"仅允许：{'、'.join(allowed_types)}"
            )
        size = len(image_bytes or b"")
        if size <= 0:
            raise ImageValidationError("上传文件为空，请重新选择截图")
        if size > max_bytes:
            raise ImageValidationError(
                f"图片超过大小上限 {max_bytes / 1024 / 1024:.1f} MB"
                f"（当前 {size / 1024 / 1024:.2f} MB）。请压缩 / 裁剪截图后重试"
                f"（上限可用 METRIC_MAX_IMAGE_BYTES 调整）。"
            )
        detected = detect_image_mime(image_bytes)
        if detected is None:
            raise ImageValidationError(
                "文件内容不是有效图片（文件头校验失败，可能是改名的非图片文件或已损坏）。"
                "请重新导出 jpg / png / webp 截图后重试。"
            )
        if detected != declared:
            logger.info(
                "声明类型（%s）与文件头探测（%s）不一致，以探测结果为准", declared, detected
            )

        b64_text = base64.b64encode(image_bytes).decode("ascii")
        ctx["data_url"] = f"data:{detected};base64,{b64_text}"

        safe_name = None
        if image_name:
            safe_name = str(image_name).replace("\\", "/").rsplit("/", 1)[-1][:120] or None
        image_meta = {
            "filename": safe_name,
            "content_type": detected,
            "size_bytes": size,
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        }

        note_title: str | None = None
        note_raw = state.get("note_id")
        if note_raw:
            with SessionLocal() as db:
                note = db.get(Note, uuid.UUID(str(note_raw)))
                if note is None:
                    raise NoteMissingError(f"笔记不存在：{note_raw}")
                note_title = note.title

        return {
            "image_meta": image_meta,
            "note_title": note_title,
            "node_trace": [
                {
                    "node": NODE_LOAD_IMAGE,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "content_type": detected,
                    "size_bytes": size,
                }
            ],
        }

    # ------------------------------------------------------------- transcribe
    def transcribe_node(state: MetricState) -> dict[str, Any]:
        """调视觉模型做只读转录；JSON 容错 / 计数交给 invoke_json。"""
        started = time.perf_counter()
        retry_count = int(state.get("retry_count") or 0) + 1
        data_url = ctx.get("data_url")
        if not data_url:
            raise ImageValidationError("图片数据缺失（内部状态异常）")

        messages = build_messages(image_data_url=data_url, feedback=state.get("feedback") or None)
        try:
            result = invoke_json(model, messages, want_json=True)
        except AgentJSONError as exc:
            # JSON 解析已在 invoke_json 内部用尽纠错次数：交给条件边决定「回路 / 放弃」
            return {
                "parse_failed": True,
                "llm_raw": exc.raw_text or state.get("llm_raw"),
                "retry_count": retry_count,
                "feedback": (
                    "上一次输出无法解析为 JSON。请只输出一个 JSON 对象："
                    "不要代码块围栏、不要解释文字，字符串内不要出现未转义的换行。"
                ),
                "node_trace": [
                    {
                        "node": NODE_TRANSCRIBE,
                        "status": "json_failed",
                        "duration_ms": _elapsed_ms(started),
                        "attempt": retry_count,
                    }
                ],
            }
        except Exception as exc:  # noqa: BLE001 —— 只拦截「不支持图片」关键词，其余原样抛出
            text = f"{type(exc).__name__}: {exc}"
            if looks_like_vision_unsupported(text):
                return {
                    "vision_unsupported": True,
                    "vision_error": text[:300],
                    "retry_count": retry_count,
                    "node_trace": [
                        {
                            "node": NODE_TRANSCRIBE,
                            "status": "vision_unsupported",
                            "duration_ms": _elapsed_ms(started),
                            "attempt": retry_count,
                        }
                    ],
                }
            raise

        raw_payload = result.payload if isinstance(result.payload, dict) else {}
        return {
            "raw": raw_payload,
            "llm_raw": result.raw_text,
            "parse_failed": False,
            "vision_unsupported": False,
            "retry_count": retry_count,
            "parse_attempts": merge_parse_attempts(state.get("parse_attempts"), result.attempts),
            "usage": merge_usage(state.get("usage"), result.usage),
            "node_trace": [
                {
                    "node": NODE_TRANSCRIBE,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "attempt": retry_count,
                    "json_parse_attempts": result.attempts,
                    "json_mode_used": result.json_mode_used,
                }
            ],
        }

    # ------------------------------------------------------ normalize_validate
    def normalize_validate_node(state: MetricState) -> dict[str, Any]:
        """本地别名归一 + 数字换算，生成 evidence / missing / uncertain，组装 output。"""
        started = time.perf_counter()
        normalized = normalize_payload(state.get("raw") or {})
        parse_attempts = int(state.get("parse_attempts") or 0)
        output = {
            "data_source": "screenshot",
            "metrics": normalized["metrics"],
            "evidence": normalized["evidence"],
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "missing_fields": normalized["missing_fields"],
            "uncertain_fields": normalized["uncertain_fields"],
            "parse_attempts": parse_attempts,
            "warnings": normalized["warnings"],
            # 人工核对辅助（服务端回显；识别结果仍需人工确认后才落库）
            "note_id": state.get("note_id"),
            "note_title": state.get("note_title"),
        }
        recognized = len(METRIC_FIELDS) - len(normalized["missing_fields"])
        return {
            "metrics": normalized["metrics"],
            "evidence": normalized["evidence"],
            "missing_fields": normalized["missing_fields"],
            "uncertain_fields": normalized["uncertain_fields"],
            "warnings": normalized["warnings"],
            "captured_at": output["captured_at"],
            "output": output,
            "node_trace": [
                {
                    "node": NODE_NORMALIZE,
                    "status": "ok",
                    "duration_ms": _elapsed_ms(started),
                    "recognized": recognized,
                    "missing": len(normalized["missing_fields"]),
                    "uncertain": len(normalized["uncertain_fields"]),
                }
            ],
        }

    # ---------------------------------------------------------------- give_up
    def give_up_node(state: MetricState) -> dict[str, Any]:
        """「模型不支持图片」或 JSON 重试超限：抛结构化错误（run 记 failed）。"""
        if state.get("vision_unsupported"):
            raise VisionUnsupportedError(
                "当前模型不支持图片输入（vision）。请在 DeepSeek 控制台的模型列表"
                "换用支持图片输入的模型，并填入 backend/.env 的 LLM_VISION_MODEL"
                "（留空则回退 LLM_MODEL）。",
                reason=state.get("vision_error"),
            )
        retry_count = int(state.get("retry_count") or 0)
        raise RecognitionGiveUpError(
            f"识别失败：连续 {retry_count} 轮输出均无法解析为合法 JSON。"
            "请重试、更换更清晰的截图，或检查所配视觉模型是否稳定支持 JSON 输出。",
            reason=f"retry_count={retry_count}",
        )

    # --------------------------------------------------------------- 条件边
    def route_after_transcribe(state: MetricState) -> str:
        """解析成功 → 归一；未超限 → 回炉；超限 / 不支持图片 → 放弃。"""
        if state.get("vision_unsupported"):
            return NODE_GIVE_UP
        if state.get("parse_failed"):
            retry_count = int(state.get("retry_count") or 0)
            if retry_count < max(1, settings.AGENT_JSON_MAX_ATTEMPTS):
                return NODE_TRANSCRIBE
            return NODE_GIVE_UP
        return NODE_NORMALIZE

    # ------------------------------------------------------------------ 装配
    graph = new_state_graph("metric-recognition", MetricState)
    graph.add_node(NODE_LOAD_IMAGE, load_image_node)
    # 调用 LLM 的节点统一带重试策略（仅重试 429/5xx/网络等瞬时错误）
    graph.add_node(NODE_TRANSCRIBE, transcribe_node, retry_policy=llm_retry_policy())
    graph.add_node(NODE_NORMALIZE, normalize_validate_node)
    graph.add_node(NODE_GIVE_UP, give_up_node)

    graph.add_edge(START, NODE_LOAD_IMAGE)
    graph.add_edge(NODE_LOAD_IMAGE, NODE_TRANSCRIBE)
    graph.add_conditional_edges(
        NODE_TRANSCRIBE,
        route_after_transcribe,
        {
            NODE_TRANSCRIBE: NODE_TRANSCRIBE,
            NODE_NORMALIZE: NODE_NORMALIZE,
            NODE_GIVE_UP: NODE_GIVE_UP,
        },
    )
    graph.add_edge(NODE_NORMALIZE, END)
    graph.add_edge(NODE_GIVE_UP, END)

    return compile_graph(graph, checkpointer=checkpointer, use_checkpointer=use_checkpointer)


__all__ = [
    "NODE_LOAD_IMAGE",
    "NODE_TRANSCRIBE",
    "NODE_NORMALIZE",
    "NODE_GIVE_UP",
    "build_metric_graph",
    "normalize_payload",
    "looks_like_vision_unsupported",
]
