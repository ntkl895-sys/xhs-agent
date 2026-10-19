"""Embedding 服务（第 18 步 · RAG 爆款规律反哺）。

对外入口（推荐经本包 re-export 引用）：

- :class:`~app.agent.embedding.base.Embedder` —— 向量化协议（dim + embed_texts）；
- :func:`~app.agent.embedding.service.get_embedder` —— 按配置返回进程内单例；
- :func:`~app.agent.embedding.service.embed_texts` —— **唯一**「文本 → 向量」入口
  （批量切分 + sha256 缓存 + 维度强校验）；
- 异常 :class:`~app.agent.embedding.base.EmbeddingError` 系列 —— 可识别错误，
  由上层（RAG 反哺 / 提炼图 / 检查脚本）翻译成 WARNING 降级或友好中文错误。

红线：向量只在内存与数据库列（``knowledge_snippets.embedding``）中流转 ——
**绝不放进图状态、绝不进任何接口响应**。
"""

from app.agent.embedding.base import (
    Embedder,
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingNotAvailableError,
)
from app.agent.embedding.service import (
    describe_embedding_readiness,
    embed_texts,
    get_embedder,
    reset_embedder,
    validate_dim_settings,
)

__all__ = [
    "Embedder",
    "EmbeddingDimensionError",
    "EmbeddingError",
    "EmbeddingNotAvailableError",
    "describe_embedding_readiness",
    "embed_texts",
    "get_embedder",
    "reset_embedder",
    "validate_dim_settings",
]
