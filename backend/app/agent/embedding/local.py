"""本地 bge 向量化（HuggingFaceEmbeddings + sentence-transformers，第 18 步）。

⚠️ **懒加载硬约束**（任务书第 一.1 节）：

- 本模块 import 时**不 import** ``langchain_huggingface`` / ``sentence_transformers``，
  更不加载 / 下载模型 —— 应用启动与健康检查**零负担**；
- 首次 :meth:`LocalBGEEmbedder.embed_texts` 才：函数内 import → 构造
  ``HuggingFaceEmbeddings``（此时才读本地缓存 / 下载权重）→ 进程内单例复用；
- 依赖未安装 → :class:`EmbeddingNotAvailableError`（含清晰安装指引）；
- 模型加载失败（未下载 / 网络不可达）→ 同样是可识别错误，
  带 Hugging Face 镜像（``HF_ENDPOINT=https://hf-mirror.com``）指引。

依赖安装由**用户本人**执行（``pip install -r requirements.txt``；首次体积较大，
会带 transformers / torch CPU 版；模型权重约 1.3GB）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.embedding.base import EmbeddingNotAvailableError

logger = logging.getLogger("xhs-agent.agent.embedding.local")

#: 进程内模型单例缓存（key=(model_name, device)）——同进程只加载一次
_MODEL_CACHE: dict[tuple[str, str], Any] = {}

#: 安装指引（检查脚本与错误消息共用）
INSTALL_HINT = (
    "本地向量化依赖未安装或不可用：请先安装 requirements.txt 中的 "
    "langchain-huggingface 与 sentence-transformers"
    "（pip install -r requirements.txt；首次安装体积较大，CPU 可跑）。"
)

#: 模型下载指引
DOWNLOAD_HINT = (
    "首次真实向量化时会从 Hugging Face 自动下载模型权重（默认约 1.3GB）；"
    "国内网络可先设置镜像环境变量后重试：set HF_ENDPOINT=https://hf-mirror.com"
)


def reset_local_cache() -> None:
    """清空进程内模型单例（仅供测试 / 极端排障；不删磁盘上的模型缓存）。"""
    _MODEL_CACHE.clear()


def local_deps_installed() -> bool:
    """探测依赖是否可 import（**不加载模型、不联网**；供就绪快照使用）。"""
    import importlib.util

    return (
        importlib.util.find_spec("langchain_huggingface") is not None
        and importlib.util.find_spec("sentence_transformers") is not None
    )


class LocalBGEEmbedder:
    """本地中文向量模型（默认 BAAI/bge-large-zh-v1.5，归一化余弦友好）。

    实现 :class:`~app.agent.embedding.base.Embedder` 协议；模型「懒加载单例」。
    """

    def __init__(self, *, model_name: str, device: str, dim: int) -> None:
        self.model_name = model_name
        self.device = device
        self.dim = int(dim)

    # ------------------------------------------------------------------ 懒加载
    def _ensure_model(self) -> Any:
        """首次调用才 import 依赖并构造模型（此后进程内复用）。"""
        key = (self.model_name, self.device)
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached

        try:
            # 函数内 import：未安装依赖时模块仍可被安全 import（懒加载红线）
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as exc:  # pragma: no cover - 本机未安装分支由检查脚本覆盖
            raise EmbeddingNotAvailableError(INSTALL_HINT) from exc

        try:
            model = HuggingFaceEmbeddings(
                model_name=self.model_name,
                model_kwargs={"device": self.device},
                # bge 系列官方建议：向量归一化（余弦相似度口径稳定）
                encode_kwargs={"normalize_embeddings": True},
            )
        except Exception as exc:  # noqa: BLE001 —— 统一转可识别错误（含下载失败）
            raise EmbeddingNotAvailableError(
                f"本地向量模型加载失败（{self.model_name}）：{type(exc).__name__}。{DOWNLOAD_HINT}"
            ) from exc

        _MODEL_CACHE[key] = model
        logger.info(
            "本地向量模型已加载（model=%s device=%s dim=%s）",
            self.model_name,
            self.device,
            self.dim,
        )
        return model

    # ------------------------------------------------------------------ 编码
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """把文本列表编码为定长向量列表（与输入严格同序；空输入 → 空列表）。"""
        if not texts:
            return []
        model = self._ensure_model()
        vectors = model.embed_documents([str(text) for text in texts])
        return [[float(value) for value in vec] for vec in vectors]


__all__ = [
    "DOWNLOAD_HINT",
    "INSTALL_HINT",
    "LocalBGEEmbedder",
    "local_deps_installed",
    "reset_local_cache",
]
