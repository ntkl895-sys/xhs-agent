"""第 24 步 · RRF 融合纯函数自检（**不连库、不加载模型、不联网**）。

覆盖：

1. 基本融合：两路都命中的文档分数 = Σ 1/(k+rank)，排序与手算一致；
2. 单路命中：只在向量路 / 只在关键字路，分数各自独立；
3. 同路重复 id 只按首次出现计分（防刷分）；
4. 空输入 / 空串 / ``None`` id 的容错（不抛错、不产生幽灵条目）；
5. ``top_n`` 截断、``k`` 语义（k 越小头部优势越大，比值可手算）；
6. 权重：等权 vs 加权；长度不符 / 负权 / ``k<=0`` 抛 ``ValueError``；
7. 确定性：同一输入重复调用结果完全一致；同分时按 best_rank 再按首现顺序；
8. 模式切换参数校验：非法 ``retrieval_mode`` 直接抛错，不静默兜底（不触库）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_rag_fusion.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.crud.retrieval_fusion import DEFAULT_RRF_K, rrf_fuse  # noqa: E402


class Checker:
    """极简断言计数器（与其它 selftest 保持同样的 PASS/FAIL 输出形态）。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))


def group1_basic(c: Checker) -> None:
    """手算对照：a 只在向量第 1、b 在两路第 2/第 1、c 只在向量第 3、d 只在关键字第 2。"""
    fused = rrf_fuse([["a", "b", "c"], ["b", "d"]], k=60, top_n=10)
    by_id = {item.doc_id: item for item in fused}

    c.check(
        [item.doc_id for item in fused] == ["b", "a", "d", "c"],
        "排序：两路命中的 b 居首，其后按单路分数降序",
        f"实际={[item.doc_id for item in fused]}",
    )
    c.check(
        abs(by_id["b"].score - (1 / 62 + 1 / 61)) < 1e-12,
        "b 的分数 = 1/(60+2) + 1/(60+1)",
        f"实际={by_id['b'].score}",
    )
    c.check(
        by_id["b"].ranks == (2, 1) and by_id["b"].best_rank == 1,
        "b 的两路排名记录为 (2, 1)，best_rank=1",
        f"实际={by_id['b'].ranks}/{by_id['b'].best_rank}",
    )
    c.check(
        by_id["a"].ranks == (1, None) and by_id["d"].ranks == (None, 2),
        "单路命中的另一路排名为 None（不是 0）",
    )


def group2_edge(c: Checker) -> None:
    """空输入与脏输入的容错。"""
    c.check(rrf_fuse([]) == [], "空路数 → 空列表")
    c.check(rrf_fuse([[], []]) == [], "两路皆空 → 空列表")
    c.check(rrf_fuse([["", "  "], []]) == [], "空串 / 空白 id 被忽略")

    fused = rrf_fuse([[None, "a"], ["a"]], k=60, top_n=5)  # type: ignore[list-item]
    c.check(
        [item.doc_id for item in fused] == ["a"],
        "None id 被忽略，且 a 仍按两路命中融合",
        f"实际={[item.doc_id for item in fused]}",
    )

    dup = rrf_fuse([["a", "a", "a"], ["a"]], k=60, top_n=5)
    c.check(
        len(dup) == 1 and dup[0].ranks == (1, 1),
        "同一路重复 id 只按首次出现计分（不会被刷成高分）",
        f"实际={dup[0].ranks if dup else None}",
    )


def group3_topn_and_k(c: Checker) -> None:
    """top_n 截断与 k 的平滑语义。"""
    rankings = [["a", "b", "c", "d"], []]
    c.check(len(rrf_fuse(rankings, top_n=2)) == 2, "top_n=2 只返回 2 条")
    c.check(len(rrf_fuse(rankings, top_n=None)) == 4, "top_n=None 返回全部")
    c.check(rrf_fuse(rankings, top_n=0) == [], "top_n=0 返回空列表（不抛错）")

    small_k = rrf_fuse(rankings, k=1, top_n=2)
    large_k = rrf_fuse(rankings, k=1000, top_n=2)
    ratio_small = small_k[0].score / small_k[1].score
    ratio_large = large_k[0].score / large_k[1].score
    c.check(
        ratio_small > ratio_large,
        "k 越小，头部排名的相对优势越大（k=1 的 1/2 比 vs k=1000 的 1/1001 比）",
        f"k=1:{ratio_small:.3f} k=1000:{ratio_large:.3f}",
    )


def group4_weights(c: Checker) -> None:
    """权重：等权时后出现的路不因顺序占优；加权可压过单路。"""
    equal = rrf_fuse([["a"], ["b"]], k=60, top_n=2)
    c.check(
        [item.doc_id for item in equal] == ["a", "b"],
        "两路各一条时按首现顺序排列（同分不随机）",
        f"实际={[item.doc_id for item in equal]}",
    )

    weighted = rrf_fuse([["a"], ["b"]], k=60, weights=[0.5, 2.0], top_n=2)
    c.check(
        [item.doc_id for item in weighted] == ["b", "a"],
        "加权后 b（权重 2.0）反超 a（权重 0.5）",
        f"实际={[item.doc_id for item in weighted]}",
    )

    for bad_kwargs, label in (
        ({"k": 0}, "k=0 抛 ValueError"),
        ({"k": -5}, "k 为负抛 ValueError"),
        ({"weights": [1.0]}, "权重个数不符抛 ValueError"),
        ({"weights": [1.0, -1.0]}, "负权重抛 ValueError"),
    ):
        try:
            rrf_fuse([["a"], ["b"]], top_n=1, **bad_kwargs)  # type: ignore[arg-type]
        except ValueError:
            c.check(True, label)
        else:
            c.check(False, label, "未抛异常")


def group5_determinism(c: Checker) -> None:
    """确定性：同输入两次完全一致；同分按 best_rank 再按首现顺序。"""
    rankings = [["x", "y", "z"], ["z", "y", "x"]]
    first = [(i.doc_id, i.score, i.ranks) for i in rrf_fuse(rankings, top_n=5)]
    second = [(i.doc_id, i.score, i.ranks) for i in rrf_fuse(rankings, top_n=5)]
    c.check(first == second, "同一输入重复调用结果完全一致（无字典序随机）")

    tie = rrf_fuse([["a", "b"], ["b", "a"]], k=60, top_n=2)
    c.check(
        all(item.score == tie[0].score for item in tie) and tie[0].doc_id == "a",
        "完全同分时按首现顺序稳定排序",
        f"实际={[(i.doc_id, i.score) for i in tie]}",
    )

    c.check(DEFAULT_RRF_K == 60, "默认 k 恒为 60（论文经验值，改动需显式评审）")


def group6_mode_guard(c: Checker) -> None:
    """模式切换的离线可测部分：非法值必须显式报错（不触库）。

    注意：空串 / 纯空白会被 ``or`` 视为「未指定」并回退默认值（这是刻意的，
    与 pydantic-settings 读到空环境变量的行为一致），因此不列入非法值。
    """
    from app.crud.knowledge import crud_knowledge

    for bad in ("bm25", "hybrid2", "vectors", "关键词"):
        try:
            # db=None 是故意的：校验发生在触库之前，能通过就说明顺序错了
            crud_knowledge.search_snippets(
                None,  # type: ignore[arg-type]
                query_text="标题写多少字",
                retrieval_mode=bad,
            )
        except ValueError:
            c.check(True, f"非法 retrieval_mode={bad!r} 抛 ValueError")
        except Exception as exc:  # noqa: BLE001
            c.check(
                False,
                f"非法 retrieval_mode={bad!r}",
                f"抛了非预期异常：{type(exc).__name__}",
            )
        else:
            c.check(False, f"非法 retrieval_mode={bad!r}", "未抛异常（被静默接受了）")


def main() -> int:
    print("=" * 72)
    print("RRF 融合纯函数自检（不连库 / 不加载模型）")
    print("=" * 72)
    c = Checker()
    group1_basic(c)
    group2_edge(c)
    group3_topn_and_k(c)
    group4_weights(c)
    group5_determinism(c)
    group6_mode_guard(c)

    print("-" * 72)
    if c.failures:
        print(f"[FAIL] {len(c.failures)} 项失败 / 共 {c.passed + len(c.failures)} 项")
        for item in c.failures:
            print(f"  - {item}")
        return 1
    print(f"[OK] 全部 {c.passed} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
