"""embedding 统一入口（第 18 步）：工厂 + 批量切分 + 进程内缓存 + 维度强校验。

对外只暴露四件事：

- :func:`get_embedder`：按 ``EMBEDDING_PROVIDER`` 返回**进程内单例**（local 默认 /
  openai 占位）；构造期零模型加载、零网络请求；
- :func:`embed_texts`：**唯一**「文本 → 向量」入口 —— 按文本 sha256 命中进程内缓存，
  未命中的按 ``EMBEDDING_BATCH_SIZE`` 切块调用底层 embedder，逐条断言维度
  （不静默截断 / 补零，不符抛 :class:`EmbeddingDimensionError`）；
- :func:`validate_dim_settings`：比对 ``EMBEDDING_DIM`` 与数据库列维度
  （``knowledge.EMBEDDING_DIM``，建表即 1024）——不一致给清晰中文错误（需新建
  迁移改列维度，本步不做）；首次真实向量化前自动执行一次（进程内仅一次）；
- :func:`describe_embedding_readiness`：给自检 / 排障用的就绪快照（**无 key 明文**）。

红线：向量只在内存与数据库列中流转 —— **绝不放进图状态、绝不进任何接口响应**。
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Any

from app.agent.embedding.base import Embedder, EmbeddingError, assert_vector_dim
from app.agent.embedding.local import (
    INSTALL_HINT,
    LocalBGEEmbedder,
    local_deps_installed,
    reset_local_cache,
)
from app.agent.embedding.openai_compat import OpenAICompatEmbedder
from app.core.config import settings

logger = logging.getLogger("xhs-agent.agent.embedding.service")

#: 进程内单例（首次 get_embedder 时构造；不加载模型）
_DEFAULT_EMBEDDER: Embedder | None = None

#: 向量缓存上限（条）；超出按 FIFO 淘汰。仅进程内、进程重启即失效。
_CACHE_MAX = 4096

#: 文本 sha256 → 向量（插入序即淘汰序）
_VECTOR_CACHE: "OrderedDict[str, list[float]]" = OrderedDict()

#: 「维度一致性」进程内只校验一次
_DIM_CHECKED = False

#: 支持的 provider
_PROVIDERS = ("local", "openai")


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def _provider() -> str:
    """当前配置的 provider（小写归一；未知值原样返回，交由 get_embedder 报错）。"""
    return (settings.EMBEDDING_PROVIDER or "local").strip().lower()


def get_embedder() -> Embedder:
    """按 ``EMBEDDING_PROVIDER`` 返回进程内单例。

    - ``local``（默认）：:class:`~app.agent.embedding.local.LocalBGEEmbedder`，
      模型**懒加载**（首次 embed_texts 才加载 / 下载）；
    - ``openai``：:class:`~app.agent.embedding.openai_compat.OpenAICompatEmbedder`（占位）；
    - 未知取值 → :class:`EmbeddingError`（清晰中文错误）。

    构造本函数返回的实例**不加载模型、不发任何请求**。
    """
    global _DEFAULT_EMBEDDER
    if _DEFAULT_EMBEDDER is not None:
        return _DEFAULT_EMBEDDER

    provider = _provider()
    if provider == "local":
        _DEFAULT_EMBEDDER = LocalBGEEmbedder(
            model_name=settings.EMBEDDING_MODEL,
            device=settings.EMBEDDING_DEVICE,
            dim=settings.EMBEDDING_DIM,
        )
    elif provider == "openai":
        _DEFAULT_EMBEDDER = OpenAICompatEmbedder(
            model=settings.EMBEDDING_OPENAI_MODEL,
            api_key=settings.EMBEDDING_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL,
            dim=settings.EMBEDDING_DIM,
        )
    else:
        raise EmbeddingError(
            f"未知的 EMBEDDING_PROVIDER：{settings.EMBEDDING_PROVIDER!r}"
            f"（仅支持 {' / '.join(_PROVIDERS)}）。请检查 backend/.env。"
        )
    return _DEFAULT_EMBEDDER


def reset_embedder() -> None:
    """清空进程内单例与向量缓存（测试辅助；不动磁盘上的模型缓存）。"""
    global _DEFAULT_EMBEDDER, _DIM_CHECKED
    _DEFAULT_EMBEDDER = None
    _DIM_CHECKED = False
    _VECTOR_CACHE.clear()


def reset_all_caches() -> None:
    """清空单例 + 向量缓存 + 本地模型单例（测试辅助）。"""
    reset_embedder()
    reset_local_cache()


# ---------------------------------------------------------------------------
# 维度一致性
# ---------------------------------------------------------------------------
def validate_dim_settings() -> None:
    """比对 ``EMBEDDING_DIM`` 与数据库列维度（``knowledge.EMBEDDING_DIM``）。

    两者不一致（例如换了非 1024 维模型却未迁移）→
    :class:`~app.agent.embedding.base.EmbeddingDimensionError`，
    提示需新建迁移修改列维度并重建 HNSW 索引（本步不做）。
    """
    from app.agent.embedding.base import EmbeddingDimensionError
    from app.models.knowledge import EMBEDDING_DIM as DB_EMBEDDING_DIM

    if int(settings.EMBEDDING_DIM) != int(DB_EMBEDDING_DIM):
        raise EmbeddingDimensionError(
            f"EMBEDDING_DIM={settings.EMBEDDING_DIM} 与知识库 embedding 列维度 "
            f"vector({int(DB_EMBEDDING_DIM)}) 不一致。本步不做维度迁移；"
            "请把 EMBEDDING_DIM / EMBEDDING_MODEL 调回同维度模型"
            "（默认 bge-large-zh-v1.5 = 1024 维），"
            "或另行新建 Alembic 迁移修改列维度并重建 HNSW 索引后使用。"
        )


def _ensure_dim_checked() -> None:
    """首次真实向量化前执行一次维度一致性校验（进程内仅一次）。"""
    global _DIM_CHECKED
    if not _DIM_CHECKED:
        validate_dim_settings()
        _DIM_CHECKED = True


# ---------------------------------------------------------------------------
# 缓存（sha256 → 向量）
# ---------------------------------------------------------------------------
def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> list[float] | None:
    hit = _VECTOR_CACHE.get(key)
    if hit is not None:
        _VECTOR_CACHE.move_to_end(key)
    return hit


def _cache_put(key: str, vec: list[float]) -> None:
    _VECTOR_CACHE[key] = vec
    _VECTOR_CACHE.move_to_end(key)
    while len(_VECTOR_CACHE) > _CACHE_MAX:
        _VECTOR_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# 唯一入口：文本 → 向量
# ---------------------------------------------------------------------------
def embed_texts(
    texts: list[str],
    *,
    embedder: Embedder | None = None,
) -> list[list[float]]:
    """把文本列表编码为定长向量列表（与输入严格同序）。

    流程：sha256 缓存命中直接用 → 未命中的按 ``EMBEDDING_BATCH_SIZE`` 切块调用
    底层 embedder → 逐条维度断言（:func:`~app.agent.embedding.base.assert_vector_dim`，
    不静默截断 / 补零）→ 写回缓存。

    Args:
        texts: 待编码文本（含空串 / 非字符串也会先 ``str()`` 归一）。
        embedder: 显式注入的 embedder（自检用假实现）；``None`` 时取
            :func:`get_embedder` 单例。

    Returns:
        与 ``texts`` 等长、同序的向量列表；``texts`` 为空 → ``[]``。

    Raises:
        EmbeddingError 系列：维度不符 / 依赖缺失 / 模型加载失败。
    """
    if not texts:
        return []
    normalized = [str(text) for text in texts]
    keys = [_cache_key(text) for text in normalized]

    results: list[list[float] | None] = [None] * len(normalized)
    pending: list[tuple[int, str]] = []  # (下标, 文本)
    for index, (text, key) in enumerate(zip(normalized, keys)):
        hit = _cache_get(key)
        if hit is not None:
            results[index] = hit
        else:
            pending.append((index, text))

    if pending:
        _ensure_dim_checked()
        active = embedder if embedder is not None else get_embedder()
        batch_size = max(1, int(settings.EMBEDDING_BATCH_SIZE))
        for start in range(0, len(pending), batch_size):
            chunk = pending[start : start + batch_size]
            vectors = active.embed_texts([text for _, text in chunk])
            if len(vectors) != len(chunk):
                raise EmbeddingError(
                    f"向量化返回条数不符：期望 {len(chunk)}，实际 {len(vectors)}。"
                    "已放弃本次批量（不猜、不补）。"
                )
            for (index, _), vec in zip(chunk, vectors):
                assert_vector_dim(vec, settings.EMBEDDING_DIM)
                clean = [float(value) for value in vec]
                results[index] = clean
                _cache_put(keys[index], clean)

    # 到此 results 应全部就位（pending 非空时逐条填写；为空时全部命中缓存）。
    # 若仍出现 None（内部一致性问题），显式报错而不是静默缩短列表（不猜、不丢）。
    final: list[list[float]] = []
    for index, vec in enumerate(results):
        if vec is None:  # pragma: no cover - 防御分支
            raise EmbeddingError(
                f"第 {index + 1} 条文本未能得到向量（内部一致性问题），已放弃本次批量。"
            )
        final.append(vec)
    return final


# ---------------------------------------------------------------------------
# 就绪快照（自检 / 排障；无 key 明文）
# ---------------------------------------------------------------------------
def describe_embedding_blocker() -> dict[str, Any] | None:
    """向量化「未就绪原因」（第 19 步新增函数；``None`` = 就绪）。

    **不加载模型、不发任何请求、不写任何状态** —— 只做依赖探测与配置检查，
    供知识库人工维护入口（手动创建 / 文档导入）在**产生任何副作用之前**做闸门，
    返回的 dict 由业务层翻译成 400 的 ``details``：

    - local：依赖未安装 → ``{provider, reason, hint}``；
    - openai：缺 ``EMBEDDING_OPENAI_MODEL`` / ``EMBEDDING_API_KEY`` → ``{provider, reason, missing}``；
    - 未知 provider → ``{provider, reason}``。

    （与 ``app/agent/insights/service`` 的图内闸门同型；此处为「知识库维护」场景
    的通用探测，不改动 insights 既有实现与行为。）
    """
    provider = _provider()
    if provider == "local":
        if not local_deps_installed():
            return {
                "provider": "local",
                "reason": "本地向量化依赖未安装（langchain-huggingface / sentence-transformers）",
                "hint": INSTALL_HINT,
            }
        return None
    if provider == "openai":
        missing: list[str] = []
        if not settings.EMBEDDING_OPENAI_MODEL.strip():
            missing.append("EMBEDDING_OPENAI_MODEL")
        if not settings.EMBEDDING_API_KEY.strip():
            missing.append("EMBEDDING_API_KEY")
        if missing:
            return {
                "provider": "openai",
                "reason": "openai 兼容 embedding 未配置",
                "missing": missing,
            }
        return None
    return {
        "provider": provider,
        "reason": f"未知的 EMBEDDING_PROVIDER：{settings.EMBEDDING_PROVIDER!r}"
        "（仅支持 local / openai），请检查 backend/.env。",
    }


def describe_embedding_readiness() -> dict[str, Any]:
    """给自检 / 排障用的「就绪状态」快照（**绝不包含 key 明文**）。"""
    from app.models.knowledge import EMBEDDING_DIM as DB_EMBEDDING_DIM

    provider = _provider()
    return {
        "provider": provider,
        "model": settings.EMBEDDING_MODEL,
        "dim": int(settings.EMBEDDING_DIM),
        "db_dim": int(DB_EMBEDDING_DIM),
        "dim_consistent": int(settings.EMBEDDING_DIM) == int(DB_EMBEDDING_DIM),
        "device": settings.EMBEDDING_DEVICE,
        "batch_size": int(settings.EMBEDDING_BATCH_SIZE),
        "top_k": int(settings.EMBEDDING_TOP_K),
        "local_deps_installed": local_deps_installed(),
        "openai_model": settings.EMBEDDING_OPENAI_MODEL,
        "has_api_key": bool(settings.EMBEDDING_API_KEY.strip()),
        "cache_size": len(_VECTOR_CACHE),
    }


# 从 base 再导出维度断言（避免调用方多 import 一处）
__all__ = [
    "assert_vector_dim",
    "describe_embedding_blocker",
    "describe_embedding_readiness",
    "embed_texts",
    "get_embedder",
    "reset_all_caches",
    "reset_embedder",
    "validate_dim_settings",
]
