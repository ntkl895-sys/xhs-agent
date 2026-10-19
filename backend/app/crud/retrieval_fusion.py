"""检索结果融合：Reciprocal Rank Fusion（RRF）纯函数。

为什么单独成文件：融合排序是**纯计算**，不碰数据库、不加载模型，必须能离线
单测（``scripts/selftest_rag_fusion.py`` 不连库、不加载 bge 也能验证）。

RRF 公式（Cormack et al., 2009）：

.. code-block:: text

    score(d) = Σ_i  w_i / (k + rank_i(d))

其中 ``rank_i(d)`` 是文档 d 在第 i 路结果里的排名（**从 1 开始**），未出现则为 0
分。``k`` 是平滑常数（经验值 60），作用是压低头部排名的绝对优势——这正是 RRF
不需要分数归一化的原因：它只吃排名，不吃相似度数值。

红线：本模块不引入任何第三方依赖，也不代表 BM25（pg_trgm 只提供三元组相似度，
没有 IDF 与词频饱和）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: RRF 默认平滑常数（原论文经验值）
DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class FusedItem:
    """融合结果中的一条文档。

    Attributes:
        doc_id: 文档 id（统一转成字符串，便于跨路比对）。
        score: RRF 得分（各 ``w/(k+rank)`` 之和）。
        ranks: 每一路的排名（1 起）；``None`` 表示该路未命中。
        best_rank: 各路中的最好排名，用于同分时的确定性兜底排序。
    """

    doc_id: str
    score: float
    ranks: tuple[int | None, ...]
    best_rank: int


def rrf_fuse(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: Sequence[float] | None = None,
    top_n: int | None = None,
) -> list[FusedItem]:
    """把多路排名融合成一路，按 RRF 得分降序返回。

    Args:
        rankings: 每路一个 id 序列，**按排名从优到劣**排列；可含重复 id 与空串。
        k: 平滑常数（必须 > 0；越小则头部排名优势越大）。
        weights: 每路权重，长度须与 ``rankings`` 一致；``None`` 表示等权 1.0。
        top_n: 只返回前 N 条；``None`` 返回全部。

    Returns:
        融合后的 :class:`FusedItem` 列表。

    Raises:
        ValueError: ``k <= 0`` / 权重个数与路数不一致 / 权重为负。

    Notes:
        - 同一路内的重复 id 只按**首次出现**的排名计分（防止刷分）；
        - 排序键：``(-score, best_rank, 首次出现顺序)``，保证完全确定性，
          同分不会因字典序随机翻转；
        - 全部输入为空时返回空列表，不抛错。
    """
    if int(k) <= 0:
        raise ValueError(f"RRF 的 k 必须为正整数，收到 {k}")

    path_count = len(rankings)
    if weights is None:
        normalized_weights: tuple[float, ...] = tuple(1.0 for _ in range(path_count))
    else:
        if len(weights) != path_count:
            raise ValueError(
                f"权重个数 {len(weights)} 与检索路数 {path_count} 不一致"
            )
        normalized_weights = tuple(float(item) for item in weights)
        if any(item < 0 for item in normalized_weights):
            raise ValueError("RRF 权重不允许为负")

    scores: dict[str, float] = {}
    ranks: dict[str, list[int | None]] = {}
    first_seen: dict[str, int] = {}
    order = 0

    for path_index, ranking in enumerate(rankings):
        seen_in_path: set[str] = set()
        for position, raw_id in enumerate(ranking, start=1):
            if raw_id is None:
                continue
            doc_id = str(raw_id).strip()
            if not doc_id or doc_id in seen_in_path:
                continue
            seen_in_path.add(doc_id)
            if doc_id not in ranks:
                ranks[doc_id] = [None] * path_count
                scores[doc_id] = 0.0
                first_seen[doc_id] = order
                order += 1
            ranks[doc_id][path_index] = position
            scores[doc_id] += normalized_weights[path_index] / (k + position)

    items: list[FusedItem] = []
    for doc_id, score in scores.items():
        path_ranks = tuple(ranks[doc_id])
        hit_ranks = [rank for rank in path_ranks if rank is not None]
        items.append(
            FusedItem(
                doc_id=doc_id,
                score=round(score, 12),
                ranks=path_ranks,
                best_rank=min(hit_ranks) if hit_ranks else 0,
            )
        )

    items.sort(key=lambda item: (-item.score, item.best_rank, first_seen[item.doc_id]))
    if top_n is not None:
        return items[: max(0, int(top_n))]
    return items


__all__ = ["DEFAULT_RRF_K", "FusedItem", "rrf_fuse"]
