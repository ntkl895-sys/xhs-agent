# -*- coding: utf-8 -*-
"""测试集评测 · B：违禁词/合规扫描（真实扫描函数、零 token）。

读冻结测试集 ``fixtures/testsets/compliance/``（sha256 校验）：
128 条（正例 64 应命中 + 负例 64 期望不命中；含词表外变体 / 谐音 /
插入分隔符 / 空格 及易误伤语境）。逐条调**真实**扫描函数
``forbidden_words.scan_note``（内置保守词表，确定性、离线）。

输出：混淆矩阵 TP/FP/FN/TN、Accuracy / Precision / Recall / F1、
误报率（FP/负例）、漏报率（FN/正例）、7 个分组的命中率、
全部 FP / FN 明细（供人工核对）。

口径说明（如实）：

- 评测对象 = 内置词表（73 词）的确定性扫描器；知识库扩展词表为运行态数据，不纳入冻结集；
- 正例含「谐音/异体/词表外近似」等对抗变体——扫描器为「归一化后字面匹配」设计，
  对这些变体的漏报属**如实测出的能力边界**，明细逐条列出。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_testset_compliance.py

退出码：0 = 评测完成；1 = 夹具缺失 / 指纹不符等评测环境错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

BACKEND_DIR = BENCH_DIR.parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import bench_common as bc  # noqa: E402

from app.agent.content.forbidden_words import scan_note, describe_wordlist  # noqa: E402

METHOD = (
    "scripts/benchmark/bench_testset_compliance.py：读冻结夹具（sha256 校验），"
    "逐条调真实 scan_note（内置保守词表，归一化后字面匹配；零 LLM、零数据库）；"
    "口径=内置词表扫描器（知识库扩展词表不纳入）；类别=offline_objective"
)


def main() -> int:
    bc.ensure_dirs()
    bc.print_banner("测试集评测 B · 违禁词/合规扫描（零 token）")

    try:
        manifest, files = bc.load_frozen_testsets("compliance", ["cases.json"])
    except Exception as exc:  # noqa: BLE001
        print(f"[环境错误] {exc}")
        return bc.EXIT_FAIL

    cases: list[dict] = files["cases.json"]
    wordlist_info = describe_wordlist()
    print(
        f"[夹具] compliance v{manifest['version']}，{len(cases)} 条（sha256 校验通过）；"
        f"内置词表 {wordlist_info['total']} 词"
    )

    records: list[dict] = []
    for case in cases:
        hits = scan_note(body=case["text"])
        actual_hit = len(hits) > 0
        expect_hit = bool(case["expect_hit"])
        if expect_hit and actual_hit:
            outcome = "TP"
        elif expect_hit and not actual_hit:
            outcome = "FN"
        elif not expect_hit and actual_hit:
            outcome = "FP"
        else:
            outcome = "TN"
        records.append(
            {
                "id": case["id"],
                "group": case["group"],
                "text": case["text"],
                "expect_hit": expect_hit,
                "actual_hit": actual_hit,
                "outcome": outcome,
                "hits": [{"word": h["word"], "category": h["category"]} for h in hits],
                "correct": outcome in ("TP", "TN"),
                "note": case["note"],
            }
        )

    n = len(records)
    tp = sum(1 for r in records if r["outcome"] == "TP")
    fp = sum(1 for r in records if r["outcome"] == "FP")
    fn = sum(1 for r in records if r["outcome"] == "FN")
    tn = sum(1 for r in records if r["outcome"] == "TN")
    n_pos, n_neg = tp + fn, fp + tn

    def _rate(numer: int, denom: int) -> float | None:
        return round(numer / denom, 4) if denom else None

    accuracy = _rate(tp + tn, n)
    precision = _rate(tp, tp + fp)
    recall = _rate(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 4)
        if precision and recall
        else None
    )
    fpr = _rate(fp, n_neg)
    fnr = _rate(fn, n_pos)

    groups = sorted({r["group"] for r in records})
    group_stats = {
        group: {
            "n": sum(1 for r in records if r["group"] == group),
            "hit": sum(1 for r in records if r["group"] == group and r["actual_hit"]),
            "expect_hit": any(r["expect_hit"] for r in records if r["group"] == group),
        }
        for group in groups
    }

    fp_cases = [r for r in records if r["outcome"] == "FP"]
    fn_cases = [r for r in records if r["outcome"] == "FN"]

    metrics = [
        bc.metric("testset_compliance.cases_total", n, "条", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note=f"正例 {n_pos}（应命中）+ 负例 {n_neg}（期望不命中）"),
        bc.metric("testset_compliance.tp", tp, "条", n=n, category=bc.CATEGORY_OFFLINE, method=METHOD),
        bc.metric("testset_compliance.fp", fp, "条", n=n, category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="误报：负例被命中（全部明细见 detail.false_positives）"),
        bc.metric("testset_compliance.fn", fn, "条", n=n, category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="漏报：正例未命中（全部明细见 detail.false_negatives）"),
        bc.metric("testset_compliance.tn", tn, "条", n=n, category=bc.CATEGORY_OFFLINE, method=METHOD),
        bc.metric("testset_compliance.accuracy", accuracy, "比率", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD),
        bc.metric("testset_compliance.precision", precision, "比率", n=tp + fp,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="TP/(TP+FP)；分母为「扫描器命中」总数", confidence="medium"),
        bc.metric("testset_compliance.recall", recall, "比率", n=n_pos,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="TP/(TP+FN)；分母为正例数"),
        bc.metric("testset_compliance.f1", f1, "比率", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD),
        bc.metric("testset_compliance.false_positive_rate", fpr, "比率", n=n_neg,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="FP/负例数（误报率）"),
        bc.metric("testset_compliance.false_negative_rate", fnr, "比率", n=n_pos,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="FN/正例数（漏报率）"),
    ]
    for group, stat in group_stats.items():
        metrics.append(
            bc.metric(
                f"testset_compliance.group.{group}.hit_rate",
                _rate(stat["hit"], stat["n"]), "比率", n=stat["n"],
                category=bc.CATEGORY_OFFLINE, method=METHOD,
                note="该组被扫描命中的比例（正例组看召回、负例组看误伤）",
            )
        )

    result_path = bc.write_suite_result(
        "testsets/compliance.json",
        suite="testset_compliance",
        metrics=metrics,
        detail={
            "fixture": "fixtures/testsets/compliance/cases.json",
            "fixture_version": manifest["version"],
            "builtin_wordlist": wordlist_info,
            "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
            "groups": group_stats,
            "false_positives": fp_cases,
            "false_negatives": fn_cases,
            "records": records,
        },
        summary={
            "n": n,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1,
            "false_positive_rate": fpr, "false_negative_rate": fnr,
        },
        notes=[
            "口径：内置词表（73 词）确定性扫描器；知识库扩展词表为运行态数据，不纳入本冻结集。",
            "正例含谐音/异体对抗变体——漏报属如实测出的能力边界；全部 FP/FN 逐条留档供人工核对。",
            "结论只能写「本测试集（n=128）上的精确率/召回率」；不得外推为「平台合规准确率」。",
        ],
    )

    print("-" * 66)
    print(f"混淆矩阵：TP={tp}  FP={fp}  FN={fn}  TN={tn}（n={n}）")
    print(f"Accuracy={accuracy}  Precision={precision}  Recall={recall}  F1={f1}")
    print(f"误报率（FP/负例）={fpr}  漏报率（FN/正例）={fnr}")
    print("分组命中率：")
    for group, stat in group_stats.items():
        print(f"  {group:<20} {stat['hit']}/{stat['n']}（期望命中={stat['expect_hit']}）")
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
