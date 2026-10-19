"""openai 兼容 embedding（占位实现，第 18 步）。

用途：为将来接入官方 / 第三方 embedding 接口预留（``EMBEDDING_PROVIDER=openai``）。
**不新增任何依赖** —— 走既有的 ``langchain_openai.OpenAIEmbeddings``。

安全约定：

- 仅当 ``EMBEDDING_PROVIDER=openai`` 时才启用；key / base_url / 模型名全部来自
  ``backend/.env``（配置项默认空，绝不硬编码真实值）；
- 同样「懒加载」：import 本模块不构造任何实例、不发任何请求；
- key / 模型名缺失 → :class:`EmbeddingNotAvailableError`（清晰中文指引）。

⚠️ 本步真实启用的默认路径是本地 bge（``local.py``）；本模块属**占位**，
未安装 / 未配置时不影响主链路。
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.embedding.base import EmbeddingNotAvailableError

logger = logging.getLogger("xhs-agent.agent.embedding.openai_compat")


class OpenAICompatEmbedder:
    """openai 兼容 embedding（懒加载单例；实现 Embedder 协议）。"""

    def __init__(self, *, model: str, api_key: str, base_url: str, dim: int) -> None:
        self.model_name = model
        self.api_key = api_key
        self.base_url = base_url
        self.dim = int(dim)
        self._instance: Any | None = None

    def _ensure_model(self) -> Any:
        if self._instance is not None:
            return self._instance
        if not self.model_name.strip():
            raise EmbeddingNotAvailableError(
                "EMBEDDING_PROVIDER=openai 但 EMBEDDING_OPENAI_MODEL 为空："
                "请在 backend/.env 填写 openai 兼容 embedding 的模型名"
                "（真实值以服务商控制台为准，代码不硬编码）。"
            )
        if not self.api_key.strip():
            raise EmbeddingNotAvailableError(
                "EMBEDDING_PROVIDER=openai 但 EMBEDDING_API_KEY 为空："
                "请在 backend/.env 填写密钥（真实密钥只放本地 .env，绝不写入代码 / 日志）。"
            )

        # 函数内 import：与 local.py 保持同一懒加载纪律
        from langchain_openai import OpenAIEmbeddings

        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "api_key": self.api_key,
        }
        if self.base_url.strip():
            kwargs["base_url"] = self.base_url.strip()
        self._instance = OpenAIEmbeddings(**kwargs)
        logger.info("openai 兼容 embedding 已构造（model=%s）", self.model_name)
        return self._instance

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """把文本列表编码为定长向量列表（与输入严格同序；空输入 → 空列表）。"""
        if not texts:
            return []
        model = self._ensure_model()
        vectors = model.embed_documents([str(text) for text in texts])
        return [[float(value) for value in vec] for vec in vectors]


__all__ = ["OpenAICompatEmbedder"]
