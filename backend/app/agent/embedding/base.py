"""Embedding 抽象层（第 18 步）：协议 + 可识别异常。

设计要点：

- :class:`Embedder` 只保证一件事：文本列表 → **同序**的定长稠密向量列表；
- 异常体系刻意与「可降级」语义绑定 —— RAG 反哺接入方捕获
  :class:`EmbeddingError` 后只记 WARNING 并按「无 RAG」继续；
- **绝不静默截断 / 补零**：维度不符一律抛 :class:`EmbeddingDimensionError`。
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

__all__ = [
    "Embedder",
    "EmbeddingDimensionError",
    "EmbeddingError",
    "EmbeddingNotAvailableError",
]


# ---------------------------------------------------------------------------
# 异常（可识别：上层据此降级 / 转友好中文错误）
# ---------------------------------------------------------------------------
class EmbeddingError(RuntimeError):
    """embedding 服务可识别错误基类。"""


class EmbeddingNotAvailableError(EmbeddingError):
    """向量化依赖未安装 / 模型未下载 / 加载失败（消息内含清晰中文指引）。"""


class EmbeddingDimensionError(EmbeddingError):
    """向量维度与配置 / 数据库列不一致（不静默截断、不补零）。"""


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------
@runtime_checkable
class Embedder(Protocol):
    """向量化协议。

    - ``dim``：输出向量维度（必须与配置 ``EMBEDDING_DIM`` 一致）；
    - ``embed_texts``：**同序**返回；空输入返回空列表；不得抛裸异常
      （实现方把底层错误翻译成 :class:`EmbeddingError` 系列）。
    """

    dim: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """把文本列表编码成定长向量列表（与输入严格同序）。"""
        ...  # pragma: no cover - 协议声明


def assert_vector_dim(vec: Sequence[float], expected: int) -> None:
    """断言单条向量维度 == ``expected``；不符抛 :class:`EmbeddingDimensionError`。

    这是「不静默截断 / 不补零」红线的唯一执行点（service 层每条向量都会过这里）。
    """
    wanted = int(expected)
    if len(vec) != wanted:
        raise EmbeddingDimensionError(
            f"向量维度不符：期望 {wanted} 维"
            f"（EMBEDDING_DIM 配置应与知识库列 vector({wanted}) 一致），"
            f"实际 {len(vec)} 维。不会静默截断或补零；"
            "更换不同维度的模型需保持维度一致，或新建数据库迁移修改列维度并重建 "
            "HNSW 索引（本步不做）。"
        )
