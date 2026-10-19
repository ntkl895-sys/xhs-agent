# -*- coding: utf-8 -*-
"""测试集评测 · C：视觉数值归一化（真实函数、零 token）。

读冻结测试集 ``fixtures/testsets/normalize/``（sha256 校验）：
108 条 (raw, expected)，12 类数值写法（纯数字 / 千分位 / 全角逗号 / 万 / 亿 /
w / k / 空格 / 小数 / 混合文本 / 占位符 / 非法边界）。逐条调**真实**
``app.agent.metrics.graph._parse_count``（截图转录值 → 整数计数）。

输出：总正确率、数值样本解析正确率、None 样本正确拒绝率（**空对空不判命中**，
单独统计、不与数值正确率混算）、全部错例明细。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_testset_normalize.py

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

from app.agent.metrics.graph import _parse_count  # noqa: E402

METHOD = (
    "scripts/benchmark/bench_testset_normalize.py：读冻结夹具（sha256 校验），"
    "逐条调真实 _parse_count（截图转录值→整数，确定性；零 LLM）；"
    "expected 由人工按「计数语义」标注（不依赖实现）；类别=offline_objective"
)


def main() -> int:
    bc.ensure_dirs()
    bc.print_banner("测试集评测 C · 视觉数值归一化（零 token）")

    try:
        manifest, files = bc.load_frozen_testsets("normalize", ["cases.json"])
    except Exception as exc:  # noqa: BLE001
        print(f"[环境错误] {exc}")
        return bc.EXIT_FAIL

    cases: list[dict] = files["cases.json"]
    print(f"[夹具] normalize v{manifest['version']}，{len(cases)} 条（sha256 校验通过）")

    records: list[dict] = []
    for case in cases:
        raw = case["raw"]
        expected = case["expected"]
        record = {
            "id": case["id"],
            "category": case["category"],
            "raw": raw,
            "expected": expected,
            "note": case["note"],
        }
        try:
            actual = _parse_count(raw)
            record["actual"] = actual
        except Exception as exc:  # noqa: BLE001 —— 预期外异常如实记录（不吞）
            record["actual"] = None
            record["error"] = f"{type(exc).__name__}: {exc}"[:180]

        if "error" in record:
            record["outcome"] = "error"
            record["correct"] = False
        elif expected is None and actual is None:
            record["outcome"] = "correct_reject"
            record["correct"] = True
        elif expected is None and actual is not None:
            record["outcome"] = "wrong_accept"
            record["correct"] = False
        elif expected is not None and actual == expected:
            record["outcome"] = "correct_parse"
            record["correct"] = True
        else:
            record["outcome"] = "wrong_parse"
            record["correct"] = False
        records.append(record)

    n = len(records)
    numeric = [r for r in records if r["expected"] is not None]
    none_cases = [r for r in records if r["expected"] is None]
    correct_total = sum(1 for r in records if r["correct"])
    correct_parse = sum(1 for r in numeric if r["outcome"] == "correct_parse")
    correct_reject = sum(1 for r in none_cases if r["outcome"] == "correct_reject")
    wrong_parse = sum(1 for r in records if r["outcome"] == "wrong_parse")
    wrong_accept = sum(1 for r in records if r["outcome"] == "wrong_accept")
    errors = sum(1 for r in records if r["outcome"] == "error")

    def _rate(numer: int, denom: int) -> float | None:
        return round(numer / denom, 4) if denom else None

    categories = sorted({r["category"] for r in records})
    category_stats = {
        cat: {
            "n": sum(1 for r in records if r["category"] == cat),
            "correct": sum(1 for r in records if r["category"] == cat and r["correct"]),
        }
        for cat in categories
    }

    mismatches = [r for r in records if not r["correct"]]

    metrics = [
        bc.metric("testset_normalize.cases_total", n, "条", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note=f"数值样本 {len(numeric)}（expected 非 None）+ None 样本 {len(none_cases)}"),
        bc.metric("testset_normalize.overall_accuracy", _rate(correct_total, n), "比率", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="（数值正确 + 正确拒绝）/ 总数"),
        bc.metric("testset_normalize.numeric_accuracy", _rate(correct_parse, len(numeric)), "比率",
                  n=len(numeric), category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="数值样本：解析值与 expected 完全一致的比例"),
        bc.metric("testset_normalize.rejection_accuracy", _rate(correct_reject, len(none_cases)), "比率",
                  n=len(none_cases), category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="None 样本：正确返回空值的比例（空对空不判命中，单独统计）"),
        bc.metric("testset_normalize.wrong_parse_total", wrong_parse, "条", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="数值解析错误（结果与 expected 不一致）"),
        bc.metric("testset_normalize.wrong_accept_total", wrong_accept, "条", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="应为空值却返回了数值"),
        bc.metric("testset_normalize.error_total", errors, "条", n=n,
                  category=bc.CATEGORY_OFFLINE, method=METHOD,
                  note="预期外异常（如有——属真实缺陷线索，明细见 detail.mismatches）"),
    ]
    for cat, stat in category_stats.items():
        metrics.append(
            bc.metric(
                f"testset_normalize.category.{cat}.accuracy",
                _rate(stat["correct"], stat["n"]), "比率", n=stat["n"],
                category=bc.CATEGORY_OFFLINE, method=METHOD,
            )
        )

    result_path = bc.write_suite_result(
        "testsets/normalize.json",
        suite="testset_normalize",
        metrics=metrics,
        detail={
            "fixture": "fixtures/testsets/normalize/cases.json",
            "fixture_version": manifest["version"],
            "categories": category_stats,
            "mismatch_total": len(mismatches),
            "mismatches": mismatches,
            "records": records,
        },
        summary={
            "n": n,
            "numeric_n": len(numeric),
            "none_n": len(none_cases),
            "overall_accuracy": _rate(correct_total, n),
            "numeric_accuracy": _rate(correct_parse, len(numeric)),
            "rejection_accuracy": _rate(correct_reject, len(none_cases)),
        },
        notes=[
            "口径：确定性函数 _parse_count（截图转录值→整数）；expected 由人工按计数语义标注。",
            "空对空不判命中：expected=None 且实测 None 计「正确拒绝」，与数值正确率分开统计。",
            "错例（wrong_parse / wrong_accept / error）全部留档，未做任何剔除；未调参、未改实现。",
        ],
    )

    print("-" * 66)
    print(f"总正确率：{correct_total}/{n} = {_rate(correct_total, n)}")
    print(f"数值样本解析正确率：{correct_parse}/{len(numeric)} = {_rate(correct_parse, len(numeric))}")
    print(f"None 样本正确拒绝率：{correct_reject}/{len(none_cases)} = {_rate(correct_reject, len(none_cases))}")
    print(f"错例：wrong_parse={wrong_parse}  wrong_accept={wrong_accept}  error={errors}")
    if mismatches:
        print("错例样例（前 6 条）：")
        for item in mismatches[:6]:
            got = item.get("error") or repr(item["actual"])
            print(f"  {item['id']} [{item['category']}] raw={item['raw']!r} 期望={item['expected']!r} 实际={got}")
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
