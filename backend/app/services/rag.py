"""RAG 反哺服务（第 18 步）：检索 → 提示词块组装 → **失败降级**。

本模块是「知识检索 → 拼进提示词上下文」的唯一封装，被选题图与创作图的
``load_context`` 节点调用：

- :func:`retrieve_snippets`：走 crud 的 pgvector 余弦 top-k；异常**向上抛**
  可识别错误（模型未下载 / 向量失败等）；
- :func:`format_rag_block`：纯函数 —— 把命中列表渲染成提示词块（逐条带类型
  标注与来源信息；空列表 → 空串）；
- :func:`safe_rag_block`：**选题 / 创作图唯一调用入口** —— 开关判断 + 全捕获，
  任何失败只记 WARNING 并返回空串（按「无 RAG」继续，主流程不阻断、不报错给用户）。

第 19 步增强（最小、可降级）：**人设知识纳入** —— 传入 ``persona_id`` 时，
把该人设关联的 active ``persona_knowledge``（人工维护的话术 / 风格 / 禁忌）
**无条件一并纳入**渲染（非向量检索；去重按 id；总条数仍受块上限约束）；
人设知识读取失败只 WARNING，按「无人设知识」继续 —— 无 persona / 失败时
提示词与本步之前**完全一致**。

红线：

- 命中文本虽来自本账号本地知识块，但拼进提示词时**必须标注为「本账号历史经验，
  仅供参考」**（非行业标准）；
- 返回值只含文本片段 —— **绝不携带 embedding 向量、绝不携带 llm_raw**；
- 检索失败降级不写任何业务表、不改任何状态机。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.agent.embedding.base import EmbeddingError
from app.core.config import settings
from app.core.errors import bad_request
from app.crud.knowledge import crud_knowledge
from app.models.enums import ChunkType

logger = logging.getLogger("xhs-agent.services.rag")

#: 块标题（注入提示词时使用；显式声明「仅供参考、非行业标准」）
RAG_BLOCK_TITLE = "本账号历史经验（仅供参考，非行业标准）"

#: chunk_type → 中文标注（渲染提示词块用）
_CHUNK_LABELS: dict[str, str] = {
    "hit_pattern": "爆款规律",
    "review_insight": "复盘心得",
    "topic_template": "选题模板",
    "persona_knowledge": "人设知识",
    "forbidden_word": "违禁词",
    "general": "通用知识",
}

#: 单次注入提示词的块最大条数（防御性；正常由 top_k 控制）
_MAX_BLOCK_ITEMS = 10

#: 单次纳入的人设知识最大条数（人工维护少量条目；与通用 top-k 合计仍受 _MAX_BLOCK_ITEMS 约束）
_PERSONA_MAX_ITEMS = 5


def retrieve_snippets(
    db: Session,
    *,
    query_text: str,
    chunk_types: tuple[str, ...] | list[str] | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """检索知识块（pgvector 余弦 top-k；``query_text`` 经 embedding 服务向量化）。

    异常（``EmbeddingError`` / 数据库错误）向上抛 —— 由 :func:`safe_rag_block`
    或调用方捕获降级。
    """
    k = int(top_k) if top_k is not None else int(settings.EMBEDDING_TOP_K)
    return crud_knowledge.search_snippets(
        db,
        query_text=str(query_text),
        chunk_types=chunk_types,
        k=max(1, min(k, _MAX_BLOCK_ITEMS)),
    )


def format_rag_block(snippets: list[dict[str, Any]]) -> str:
    """把命中列表渲染成提示词块正文（纯函数；空列表 → 空串）。

    每条渲染为：``- （爆款规律）本体文本（样本 5 篇；置信度 0.8）`` ——
    样本量与置信度来自 ``meta``（缺失则省略）。
    """
    if not snippets:
        return ""
    lines: list[str] = []
    for item in snippets[:_MAX_BLOCK_ITEMS]:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        chunk_type = str(item.get("chunk_type") or "")
        label = _CHUNK_LABELS.get(chunk_type, chunk_type or "知识")
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        suffix: list[str] = []
        sample_size = meta.get("sample_size")
        if isinstance(sample_size, int):
            suffix.append(f"样本 {sample_size} 篇")
        confidence = meta.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            suffix.append(f"置信度 {round(float(confidence), 3)}")
        tail = f"（{'；'.join(suffix)}）" if suffix else ""
        lines.append(f"- （{label}）{content}{tail}")
    return "\n".join(lines)


def render_rag_prompt_section(rag_context: str | None) -> str:
    """把块正文渲染成提示词的完整一节（含标题与前后空行；空 → 空串）。

    两个反哺图（选题 / 创作）的提示词模板共用本函数 —— 「本账号历史经验，
    仅供参考」的标注措辞单一定义，避免各处漂移。
    """
    text = (rag_context or "").strip()
    if not text:
        return ""
    return f"\n【{RAG_BLOCK_TITLE}】\n{text}\n"


def search_for_api(
    db: Session,
    *,
    query_text: str,
    chunk_types: tuple[str, ...] | list[str] | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """**检索测试接口**专用：把可识别错误翻译成统一错误体（400）。

    区别于 :func:`safe_rag_block` 的「静默降级」—— 检索测试是用户**主动**发起，
    失败必须如实告知（而不是返回空列表假装无命中）。
    """
    try:
        return retrieve_snippets(
            db, query_text=query_text, chunk_types=chunk_types, top_k=top_k
        )
    except EmbeddingError as exc:
        raise bad_request(
            "知识检索失败：向量化服务不可用（依赖未安装 / 模型未下载 / 配置缺失）。",
            details={
                "reason": str(exc),
                "hint": "请先安装向量化依赖并完成模型下载"
                "（可运行 scripts/check_embedding_local.py 预检）。",
            },
        ) from exc


def _dedupe_snippet_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 ``id`` 去重（顺序保留：先入在前 —— 人设知识排在通用检索之前）。"""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = str(item.get("id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(item)
    return merged


def _load_persona_snippets(db: Session, *, persona_id: str) -> list[dict[str, Any]]:
    """读取 persona 关联的 active ``persona_knowledge``（按类型全量，**非**向量检索）。

    人设知识为人工维护的少量条目（话术 / 风格 / 禁忌 / 口头禅），不做相似度筛选、
    无条件纳入上下文；条数受 :data:`_PERSONA_MAX_ITEMS` 约束。
    返回项与 ``search_snippets`` 的 8 键**同构**（不含 similarity、不含向量）。
    """
    rows = crud_knowledge.list_active_snippets(
        db,
        chunk_type=ChunkType.PERSONA_KNOWLEDGE,
        ref_id=uuid.UUID(str(persona_id)),
        limit=_PERSONA_MAX_ITEMS,
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": str(row.id),
                "chunk_type": row.chunk_type.value if row.chunk_type else None,
                "title": row.title,
                "content": row.content,
                "meta": row.meta if isinstance(row.meta, dict) else {},
                "status": row.status.value if row.status else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return items


def safe_rag_block(
    db: Session,
    *,
    query_text: str,
    chunk_types: tuple[str, ...] | list[str] | None = None,
    top_k: int | None = None,
    enabled: bool = True,
    source: str = "rag",
    persona_id: str | None = None,
) -> str:
    """**选题 / 创作图唯一调用入口**：开关 + 检索 + 渲染 + 全捕获降级。

    - ``enabled=False`` → 立即返回空串（零检索、零行为差异）；
    - 查询文本为空**且**无 ``persona_id`` → 返回空串；
    - ``persona_id`` 非空 → 先把该人设的 active ``persona_knowledge`` 纳入
      （读取失败只 WARNING，按「无人设知识」继续）；
    - ``query_text`` 非空 → 叠加通用向量检索 top-k（失败只 WARNING，
      按「无检索块」继续）；
    - 两组块按 ``id`` 去重（人设知识在前），渲染时受块上限约束；
    - 其余任何异常 → 只记 WARNING（附 ``source`` 便于定位），返回空串 ——
      按「无 RAG」继续，**主流程不阻断、不报错给用户**。

    Returns:
        已渲染的块正文（不含标题；标题由提示词模板统一给出）。调用方应直接用
        它填充提示词的「本账号历史经验」一节；空串表示本次无 RAG 内容。
    """
    if not enabled:
        return ""
    text = str(query_text or "").strip()
    if not text and not persona_id:
        return ""
    try:
        snippets: list[dict[str, Any]] = []
        if persona_id:
            try:
                snippets.extend(_load_persona_snippets(db, persona_id=str(persona_id)))
            except Exception as exc:  # noqa: BLE001 —— 人设知识失败不阻断
                logger.warning(
                    "%s：人设知识读取失败，按「无人设知识」继续（原因：%s：%s）",
                    source,
                    type(exc).__name__,
                    exc,
                )
        if text:
            try:
                snippets.extend(
                    retrieve_snippets(
                        db, query_text=text, chunk_types=chunk_types, top_k=top_k
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 检索失败不阻断
                logger.warning(
                    "%s：知识检索失败，按「无检索块」继续（原因：%s：%s）",
                    source,
                    type(exc).__name__,
                    exc,
                )
        return format_rag_block(_dedupe_snippet_items(snippets))
    except Exception as exc:  # noqa: BLE001 —— 红线：RAG 失败绝不阻断主流程
        logger.warning(
            "%s：RAG 组装失败，按「无 RAG」继续（原因：%s：%s）",
            source,
            type(exc).__name__,
            exc,
        )
        return ""


__all__ = [
    "RAG_BLOCK_TITLE",
    "format_rag_block",
    "render_rag_prompt_section",
    "retrieve_snippets",
    "safe_rag_block",
    "search_for_api",
]
