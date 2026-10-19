"""违禁词词表来源服务（第 19 步）：知识库优先 + 内置保守兜底 + 种子导入。

三条职责（本模块是违禁词的**唯一来源封装**）：

- :func:`word_hash`：违禁词指纹（``normalize_text(词)`` 的 sha256）——
  seed 查重、手动新增、编辑重算**统一口径**；
- :func:`scan_with_knowledge`：**创作图 validate_compliance 的唯一扫描入口** ——
  先从知识库读 active 违禁词（按类型全量，**不是向量检索**），用自定义词表扫描；
  **读取异常 / 词表为空 / 扫描本身出错 → 回退内置 scan_note（保守兜底 + WARNING）**，
  任何情况下都不因知识库问题而跳过违禁词检查；
- :func:`seed_builtin_words`：把内置词表**幂等**写入知识库（按
  ``(chunk_type=forbidden_word, meta.content_hash)`` 查重，已存在跳过计 reused）——
  ``scripts/seed_forbidden_words.py`` 与 ``POST /api/v1/knowledge/seed-forbidden-words``
  共用同一实现。

红线：

- 违禁词用于**精确匹配**（归一化后子串查找），**不需要向量**：``embedding`` 允许为空；
- 本模块只读 / 增 / 归档知识块，绝不触碰 notes / topics / metrics / diagnostics；
- WARNING 日志不含密钥、不含绝对路径。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.crud.knowledge import crud_knowledge
from app.db.session import SessionLocal
from app.models.enums import ChunkType

logger = logging.getLogger("xhs-agent.services.forbidden_source")


# ---------------------------------------------------------------------------
# 延迟导入（破解循环依赖）
# ---------------------------------------------------------------------------
# 说明：``app.agent.content.__init__`` 会聚合 ``graph``；而 ``graph``（第 19 步）
# 又 import 本模块的 ``scan_with_knowledge``。若本模块在**导入期**直接
# ``from app.agent.content.forbidden_words import ...``，当本模块是导入链起点时
# （如路由先导入本模块）会触发上述聚合从而形成循环。因此统一经此函数在
# **调用时**导入（与 ``crud/knowledge.py`` 延迟导入 embedding 同思路）。
def _fw() -> Any:
    """取得 ``app.agent.content.forbidden_words`` 模块（调用时导入）。"""
    from app.agent.content import forbidden_words

    return forbidden_words

#: 单次读取的违禁词条数上限（防御性；正常为几十 ~ 几百条）
MAX_FORBIDDEN_WORDS = 2000

#: 违禁词的默认类别（手动新增未给 category 时）
DEFAULT_CATEGORY = "manual"


def word_hash(word: str) -> str:
    """违禁词指纹：``normalize_text(词)`` 的 sha256（十六进制）。

    与内置词表的 seed / 手动新增 / 编辑重算**同一口径**，保证幂等查重跨入口一致。
    """
    normalized = _fw().normalize_text(word)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_active_wordlist(db: Session) -> list[dict[str, str]]:
    """读取知识库中**全部 active 违禁词**（按类型全量，非向量检索）。

    Returns:
        ``[{"word": ..., "category": ...}, ...]``（``created_at`` 升序 + ``id`` 升序）；
        空表示知识库尚未维护违禁词（调用方自行决定是否回退内置）。
    """
    rows = crud_knowledge.list_active_snippets(
        db, chunk_type=ChunkType.FORBIDDEN_WORD, limit=MAX_FORBIDDEN_WORDS
    )
    wordlist: list[dict[str, str]] = []
    for row in rows:
        content = str(row.content or "").strip()
        if not content:
            continue
        meta = row.meta if isinstance(row.meta, dict) else {}
        category = str(meta.get("category") or DEFAULT_CATEGORY)
        wordlist.append({"word": content, "category": category})
    return wordlist


def _builtin_scan(
    title: str | None, body: str | None, tags: list[str] | None
) -> list[dict[str, Any]]:
    """内置词表扫描（回退路径的单一出口）。"""
    return _fw().scan_note(title=title, body=body, tags=tags)


def scan_with_knowledge(
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """**创作图合规扫描入口**：知识库词表优先；异常 / 空表 / 扫描出错 → 回退内置。

    回退策略（保守兜底，任务书第 19 步第一节）：

    - 读取知识库异常 → WARNING + 用内置词表扫描；
    - 知识库 active 违禁词为空 → WARNING + 用内置词表扫描；
    - 用知识库词表扫描时出错 → WARNING + 重新用内置词表扫描。

    返回结构与 :func:`~app.agent.content.forbidden_words.scan_note` 完全一致
    （``[{word, category, field, snippet}]``），保证 ``content_check`` 结构不变。
    """
    wordlist: list[dict[str, str]] | None = None
    try:
        with SessionLocal() as db:
            wordlist = load_active_wordlist(db)
    except Exception as exc:  # noqa: BLE001 —— 红线：不得因知识库问题跳过检查
        logger.warning(
            "违禁词知识库读取失败，回退内置词表（保守兜底）：%s：%s",
            type(exc).__name__,
            exc,
        )
        return _builtin_scan(title, body, tags)

    if not wordlist:
        logger.warning(
            "知识库违禁词为空，回退内置词表（保守兜底）。"
            "如需以知识库为准，可运行 scripts/seed_forbidden_words.py 同步内置词表。"
        )
        return _builtin_scan(title, body, tags)

    try:
        return _fw().scan_note(
            title=title,
            body=body,
            tags=tags,
            wordlist=[(item["word"], item["category"]) for item in wordlist],
        )
    except Exception as exc:  # noqa: BLE001 —— 扫描出错同样不允许跳过检查
        logger.warning(
            "知识库词表扫描出错，回退内置词表（保守兜底）：%s：%s",
            type(exc).__name__,
            exc,
        )
        return _builtin_scan(title, body, tags)


def seed_builtin_words(db: Session, *, dry_run: bool = False) -> dict[str, Any]:
    """把内置词表**幂等**写入知识库（``status=active``；``embedding`` 留空）。

    写入格式：``chunk_type=forbidden_word``、``content=词``、
    ``meta={"category": 类别, "content_hash": word_hash(词)}``。

    幂等口径：按当前 active 违禁词的 ``content_hash`` 集合查重，
    已存在则跳过（计 reused）；同一批内置词表内的重复归一化词只写一次。

    Args:
        dry_run: ``True`` 时**不写库**，只计算将写入 / 复用条数。

    Returns:
        ``{total, inserted, reused, would_insert, dry_run}`` ——
        ``total`` = 内置词总数；``would_insert`` = 本次待写入数（不含 reused）。
    """
    existing: set[str] = set()
    for row in crud_knowledge.list_active_snippets(
        db, chunk_type=ChunkType.FORBIDDEN_WORD, limit=MAX_FORBIDDEN_WORDS
    ):
        meta = row.meta if isinstance(row.meta, dict) else {}
        value = str(meta.get("content_hash") or "")
        if value:
            existing.add(value)

    pending: list[dict[str, Any]] = []
    reused = 0
    builtin_words = _fw().FORBIDDEN_WORDS
    for item in builtin_words:
        digest = word_hash(item.word)
        if digest in existing:
            reused += 1
            continue
        pending.append(
            {
                "chunk_type": ChunkType.FORBIDDEN_WORD,
                "content": item.word,
                "meta": {"category": item.category, "content_hash": digest},
            }
        )
        existing.add(digest)  # 防内置词表内的归一化重复

    inserted = 0
    if not dry_run and pending:
        crud_knowledge.insert_many(db, pending)
        inserted = len(pending)

    return {
        "total": len(builtin_words),
        "inserted": inserted,
        "reused": reused,
        "would_insert": len(pending),
        "dry_run": bool(dry_run),
    }


__all__ = [
    "DEFAULT_CATEGORY",
    "MAX_FORBIDDEN_WORDS",
    "load_active_wordlist",
    "scan_with_knowledge",
    "seed_builtin_words",
    "word_hash",
]
