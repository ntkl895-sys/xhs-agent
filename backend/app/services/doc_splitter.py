"""文档切分（第 19 步 · 文档导入）：官方切分器优先 + 零依赖等效兜底。

背景与决策（如实说明）：

- 任务书要求「用已安装的 langchain 文本切分器（如 RecursiveCharacterTextSplitter）」，
  但同时要求**零新增第三方依赖**；
- 本机实测 ``langchain-text-splitters`` **不在已装依赖中**（langchain 1.x 起该能力
  为独立分发包，``langchain`` 1.4.4 不再内置）；
- 因此本模块采用「自适应」口径：**优先**用官方
  ``langchain_text_splitters.RecursiveCharacterTextSplitter``（若环境已装）；
  缺失时用**内置等效实现** :class:`_RecursiveCharacterSplitter`（同分隔符优先级、
  同 ``chunk_size`` / ``chunk_overlap`` 语义、纯标准库、不发任何请求）——
  两条路径对上层完全同接口（``split_text``），行为参数一致。

切分参数（默认值）：

- ``DEFAULT_CHUNK_SIZE = 800`` 字符：知识块的合理粒度（中文语义下约一段话到两段话）；
- ``DEFAULT_CHUNK_OVERLAP = 100`` 字符：相邻块保留少量重叠，避免语义在边界被切断。

边界校验：``chunk_size`` 200~4000；``chunk_overlap`` 0~500 且必须小于 ``chunk_size``
（非法值抛 ``ValueError``，由调用方翻译成 400 / 422）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("xhs-agent.services.doc_splitter")

#: 默认块大小（字符）
DEFAULT_CHUNK_SIZE = 800

#: 默认块间重叠（字符）
DEFAULT_CHUNK_OVERLAP = 100

#: 允许的块大小范围
MIN_CHUNK_SIZE = 200
MAX_CHUNK_SIZE = 4000

#: 允许的重叠范围
MAX_CHUNK_OVERLAP = 500

#: 分隔符优先级（与 RecursiveCharacterTextSplitter 的中文常用档对齐）
SEPARATORS: tuple[str, ...] = ("\n\n", "\n", "。", "！", "？", "；", "，", " ", "")


# ---------------------------------------------------------------------------
# 内置等效实现（官方包缺失时的零依赖兜底）
# ---------------------------------------------------------------------------
def _split_by_separator(text: str, separators: tuple[str, ...], size: int) -> list[str]:
    """按分隔符优先级递归切分，返回「长度 ≤ size 的原子片段」列表。"""
    if not text:
        return []
    if len(text) <= size:
        return [text]

    separator = separators[0]
    if separator == "":
        # 最低档：定长硬切（不再可切分）
        return [text[index : index + size] for index in range(0, len(text), size)]

    parts = text.split(separator)
    # 把分隔符回贴到各段尾部（保留原文语气断点），末段无分隔符
    pieces = [part + separator for part in parts[:-1]] + [parts[-1]]
    result: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if len(piece) <= size:
            result.append(piece)
        else:
            result.extend(_split_by_separator(piece, separators[1:], size))
    return result


class _RecursiveCharacterSplitter:
    """零依赖「递归字符切分器」（语义对齐 RecursiveCharacterTextSplitter）。

    - 逐级尝试分隔符（段落 → 换行 → 句号 → …→ 硬切），保证尽量在语义边界断开；
    - 组装阶段按 ``chunk_size`` 聚合并保留 ``chunk_overlap`` 尾部重叠。
    """

    def __init__(
        self,
        *,
        chunk_size: int,
        chunk_overlap: int,
        separators: tuple[str, ...] = SEPARATORS,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators

    def split_text(self, text: str) -> list[str]:
        if not text:
            return []
        atoms = _split_by_separator(text, self.separators, self.chunk_size)
        chunks: list[str] = []
        current = ""
        for atom in atoms:
            if not current:
                current = atom
                continue
            if len(current) + len(atom) <= self.chunk_size:
                current += atom
                continue
            chunks.append(current)
            # 保留重叠的尾部（受剩余空间约束：tail + atom 绝不超 chunk_size）
            keep = min(self.chunk_overlap, max(0, self.chunk_size - len(atom)))
            tail = current[-keep:] if keep > 0 else ""
            current = tail + atom
        if current:
            chunks.append(current)
        return [chunk for chunk in chunks if chunk.strip()]


# ---------------------------------------------------------------------------
# 官方切分器（若已安装）
# ---------------------------------------------------------------------------
def _load_official_class() -> Any | None:
    """尝试加载官方 ``RecursiveCharacterTextSplitter``；未安装返回 ``None``。"""
    try:
        from langchain_text_splitters import (  # type: ignore[import-not-found]
            RecursiveCharacterTextSplitter,
        )
    except ModuleNotFoundError:
        return None
    return RecursiveCharacterTextSplitter


# ---------------------------------------------------------------------------
# 统一接口
# ---------------------------------------------------------------------------
def validate_params(chunk_size: int, chunk_overlap: int) -> None:
    """参数边界校验（非法值抛 ``ValueError``，由调用方翻译为 400）。"""
    if not (MIN_CHUNK_SIZE <= int(chunk_size) <= MAX_CHUNK_SIZE):
        raise ValueError(
            f"chunk_size 需在 {MIN_CHUNK_SIZE}~{MAX_CHUNK_SIZE} 之间（当前 {chunk_size}）"
        )
    if not (0 <= int(chunk_overlap) <= MAX_CHUNK_OVERLAP):
        raise ValueError(
            f"chunk_overlap 需在 0~{MAX_CHUNK_OVERLAP} 之间（当前 {chunk_overlap}）"
        )
    if int(chunk_overlap) >= int(chunk_size):
        raise ValueError(
            f"chunk_overlap（{chunk_overlap}）必须小于 chunk_size（{chunk_size}）"
        )


def resolve_params(
    chunk_size: int | None, chunk_overlap: int | None
) -> tuple[int, int]:
    """把可选参数归一到实际取值（缺省用默认值），并做边界校验。"""
    size = int(chunk_size) if chunk_size is not None else DEFAULT_CHUNK_SIZE
    overlap = (
        int(chunk_overlap) if chunk_overlap is not None else DEFAULT_CHUNK_OVERLAP
    )
    validate_params(size, overlap)
    return size, overlap


def split_text(
    text: str,
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[str]:
    """把文本切分成知识块列表（空白块已剔除；空文本 → 空列表）。

    官方包已装 → 用 ``RecursiveCharacterTextSplitter``；未装 → 内置等效实现。
    """
    content = str(text or "")
    if not content.strip():
        return []
    size, overlap = resolve_params(chunk_size, chunk_overlap)

    official = _load_official_class()
    if official is not None:
        splitter = official(
            chunk_size=size,
            chunk_overlap=overlap,
            separators=list(SEPARATORS),
            keep_separator=True,
        )
        pieces = splitter.split_text(content)
    else:
        pieces = _RecursiveCharacterSplitter(
            chunk_size=size, chunk_overlap=overlap
        ).split_text(content)
    return [piece.strip() for piece in pieces if piece and piece.strip()]


def describe_splitter() -> dict[str, Any]:
    """就绪快照（自检 / 排障 / 文档引用）：当前实际使用的切分实现。"""
    official = _load_official_class()
    return {
        "impl": (
            "langchain_text_splitters.RecursiveCharacterTextSplitter"
            if official is not None
            else "builtin_recursive_character（官方包未安装，零依赖等效实现）"
        ),
        "official_available": official is not None,
        "default_chunk_size": DEFAULT_CHUNK_SIZE,
        "default_chunk_overlap": DEFAULT_CHUNK_OVERLAP,
        "min_chunk_size": MIN_CHUNK_SIZE,
        "max_chunk_size": MAX_CHUNK_SIZE,
        "max_chunk_overlap": MAX_CHUNK_OVERLAP,
    }


__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "MAX_CHUNK_OVERLAP",
    "MAX_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "SEPARATORS",
    "describe_splitter",
    "resolve_params",
    "split_text",
    "validate_params",
]
