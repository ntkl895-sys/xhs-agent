# -*- coding: utf-8 -*-
"""测试集评测 · A：JSON 兜底解析器鲁棒性（真实管道、零 token）。

读冻结测试集 ``fixtures/testsets/json_robustness/``（sha256 校验）：
200 条样本（8 类 × 25），每条标注 label（direct / repaired / failed）与期望字段；
逐样本经**真实** ``invoke_json`` 管道（FakeListChatModel 单条响应 + max_attempts=1，
不发任何真实请求）解析，与 label 比对。

输出指标（category = synthetic_fixture，确定性管道、无 LLM 调用）：

- 整体准确率（label 符合 + 成功时内容与期望一致）；
- direct 一次成功率（label=direct 的 25 条被一次解析且内容一致的比例）；
- repaired 救回率（label=repaired 的样本被兜底救回且内容一致的比例）；
- failed 正确拒绝率（label=failed 的样本未被硬解析的比例）；
- 分桶（8 类）准确率；期望 label × 实际结果 的三类匹配矩阵；
- 全部错例明细（含过度解析 / 内容不符 / 未救回三类）。

结论口径：这是**解析器离线鲁棒性**，不写成「模型稳定性」。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_testset_json_robustness.py

退出码：0 = 评测完成；1 = 夹具缺失 / 指纹不符等评测环境错误。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

BACKEND_DIR = BENCH_DIR.parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import bench_common as bc  # noqa: E402

from langchain_core.language_models.fake_chat_models import FakeListChatModel  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402

from app.agent.errors import AgentJSONError  # noqa: E402
from app.agent.parsing.json_repair import invoke_json  # noqa: E402

METHOD = (
    "scripts/benchmark/bench_testset_json_robustness.py：读冻结夹具（sha256 校验），"
    "逐样本经真实 invoke_json 管道（FakeListChatModel 单条响应、max_attempts=1，"
    "零真实请求）；actual 分类：json.loads(原文) 成功=direct、其余成功=repaired、"
    "AgentJSONError=failed；类别=offline_objective（确定性算法）"
)


def _classify(raw_text: str) -> str:
    """成功解析时区分 direct / repaired（以原文能否直接 json.loads 为准）。"""
    try:
        direct = json.loads(raw_text)
        if isinstance(direct, (dict, list)):
            return "direct"
    except Exception:  # noqa: BLE001
        pass
    return "repaired"


def _is_substructure(payload: object, expected: object) -> bool:
    """payload 是否为 expected 的某个真子节点（用于识别「降级提取」行为）。"""
    if isinstance(expected, dict):
        for value in expected.values():
            if value == payload or _is_substructure(payload, value):
                return True
    if isinstance(expected, list):
        for value in expected:
            if value == payload or _is_substructure(payload, value):
                return True
    return False


def run_case(case: dict) -> dict:
    """单样本：走真实管道，返回逐样本记录。"""
    raw = case["raw"]
    record = {
        "id": case["id"],
        "bucket": case["bucket"],
        "label": case["label"],
        "note": case["note"],
        "raw": raw,
        "expected": case["expected"],
    }
    model = FakeListChatModel(responses=[raw])
    try:
        result = invoke_json(model, [HumanMessage(content="(评测固定输入)")], max_attempts=1)
        actual = _classify(raw)
        record["actual"] = actual
        record["payload"] = result.payload
        record["attempts"] = result.attempts
    except AgentJSONError as exc:
        record["actual"] = "failed"
        record["payload"] = None
        record["error"] = str(exc)[:180]
    except Exception as exc:  # noqa: BLE001 —— 预期外的异常如实记录，不吞
        record["actual"] = "error"
        record["payload"] = None
        record["error"] = f"{type(exc).__name__}: {exc}"[:180]

    # ---- 判定 ----
    label = case["label"]
    actual = record["actual"]
    if label == "failed":
        if actual == "failed":
            record["correct"] = True
            record["verdict"] = "正确拒绝"
        elif actual in ("direct", "repaired"):
            record["correct"] = False
            record["verdict"] = "过度解析（应拒绝却成功，返回首段可解析片段）"
        else:
            record["correct"] = False
            record["verdict"] = f"异常（{actual}）"
    else:
        if actual in ("direct", "repaired"):
            if record["payload"] == case["expected"]:
                record["correct"] = True
                record["verdict"] = "成功且内容一致"
            elif _is_substructure(record["payload"], case["expected"]):
                record["correct"] = False
                record["verdict"] = "降级提取（返回内嵌子结构，顶层结构未恢复）"
            else:
                record["correct"] = False
                record["verdict"] = "内容不符（救回结果与期望字段不一致）"
        else:
            if actual == "failed":
                record["correct"] = False
                record["verdict"] = "未救回（期望成功但解析失败）"
            else:
                record["correct"] = False
                record["verdict"] = f"异常（{actual}）"
    return record


def main() -> int:
    bc.ensure_dirs()
    bc.print_banner("测试集评测 A · JSON 兜底解析器鲁棒性（零 token）")

    try:
        manifest, files = bc.load_frozen_testsets("json_robustness", ["cases.json"])
    except Exception as exc:  # noqa: BLE001
        print(f"[环境错误] {exc}")
        return bc.EXIT_FAIL

    cases: list[dict] = files["cases.json"]
    print(f"[夹具] json_robustness v{manifest['version']}，{len(cases)} 条（sha256 校验通过）")

    records = [run_case(case) for case in cases]

    # ---- 统计 ----
    n = len(records)
    correct_total = sum(1 for r in records if r["correct"])

    def _rate(numer: int, denom: int) -> float | None:
        return round(numer / denom, 4) if denom else None

    direct_cases = [r for r in records if r["label"] == "direct"]
    repaired_cases = [r for r in records if r["label"] == "repaired"]
    failed_cases = [r for r in records if r["label"] == "failed"]

    direct_ok = sum(1 for r in direct_cases if r["correct"])
    repaired_ok = sum(1 for r in repaired_cases if r["correct"])
    failed_ok = sum(1 for r in failed_cases if r["correct"])

    degraded = sum(1 for r in records if r["verdict"].startswith("降级提取"))
    mismatch = sum(1 for r in records if r["verdict"].startswith("内容不符"))
    over_parsed = sum(1 for r in records if r["verdict"].startswith("过度解析"))
    not_rescued = sum(1 for r in records if r["verdict"].startswith("未救回"))
    errors = sum(1 for r in records if r["actual"] == "error")

    # 三类匹配矩阵（期望 label × 实际结果）
    matrix: dict[str, dict[str, int]] = {}
    for label in ("direct", "repaired", "failed"):
        matrix[label] = {}
        for actual in ("direct", "repaired", "failed", "error"):
            matrix[label][actual] = sum(
                1 for r in records if r["label"] == label and r["actual"] == actual
            )

    # 分桶统计
    buckets = sorted({r["bucket"] for r in records})
    bucket_stats = {
        bucket: {
            "n": sum(1 for r in records if r["bucket"] == bucket),
            "correct": sum(1 for r in records if r["bucket"] == bucket and r["correct"]),
        }
        for bucket in buckets
    }

    # ---- 指标 ----
    metrics = [
        bc.metric(
            "testset_json.cases_total", n, "条", n=n,
            category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="8 类 × 25（valid/fence/ctrl/bracket/trailing/truncated/quotes/broken）",
        ),
        bc.metric(
            "testset_json.overall_accuracy", _rate(correct_total, n), "比率", n=n,
            category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="正确 = label 符合 且（成功时）内容与期望字段一致；逐样本明细见 detail.records",
        ),
        bc.metric(
            "testset_json.direct_first_pass_rate", _rate(direct_ok, len(direct_cases)), "比率",
            n=len(direct_cases), category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="label=direct（合法 JSON）被一次解析且内容一致的比例",
        ),
        bc.metric(
            "testset_json.repaired_rescue_rate", _rate(repaired_ok, len(repaired_cases)), "比率",
            n=len(repaired_cases), category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="label=repaired（需兜底）被救回且内容一致的比例（该救回的救回率）",
        ),
        bc.metric(
            "testset_json.failed_rejection_rate", _rate(failed_ok, len(failed_cases)), "比率",
            n=len(failed_cases), category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="label=failed 被正确拒绝（不硬解析）的比例",
        ),
        bc.metric(
            "testset_json.content_mismatch_total", mismatch, "条", n=n,
            category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="解析成功但结果与期望字段不一致（非子结构；逐条见 detail.mismatches）",
        ),
        bc.metric(
            "testset_json.shape_degraded_total", degraded, "条", n=n,
            category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="「降级提取」：顶层结构未恢复，返回内嵌子结构（silent shape 降级）——"
                 "观察到的解析器行为特征（parsing 属 harness 基座、本任务不改代码，如实记录）",
        ),
        bc.metric(
            "testset_json.over_parsed_total", over_parsed, "条", n=n,
            category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="本应拒绝却成功解析（返回文本中存在的首段可解析片段）的样本数",
        ),
        bc.metric(
            "testset_json.not_rescued_total", not_rescued, "条", n=n,
            category=bc.CATEGORY_OFFLINE, method=METHOD,
            note="期望兜底救回但解析失败的样本数（尾随逗号 / 引号变体为当前能力边界）",
        ),
    ]
    for bucket, stat in bucket_stats.items():
        metrics.append(
            bc.metric(
                f"testset_json.bucket.{bucket}.accuracy",
                _rate(stat["correct"], stat["n"]), "比率", n=stat["n"],
                category=bc.CATEGORY_OFFLINE, method=METHOD,
            )
        )

    mismatches = [r for r in records if not r["correct"]]

    result_path = bc.write_suite_result(
        "testsets/json_robustness.json",
        suite="testset_json_robustness",
        metrics=metrics,
        detail={
            "fixture": "fixtures/testsets/json_robustness/cases.json",
            "fixture_version": manifest["version"],
            "matrix": matrix,
            "buckets": bucket_stats,
            "mismatch_total": len(mismatches),
            "mismatches": mismatches,
            "records": records,
        },
        summary={
            "n": n,
            "correct": correct_total,
            "overall_accuracy": _rate(correct_total, n),
            "direct_first_pass_rate": _rate(direct_ok, len(direct_cases)),
            "repaired_rescue_rate": _rate(repaired_ok, len(repaired_cases)),
            "failed_rejection_rate": _rate(failed_ok, len(failed_cases)),
        },
        notes=[
            "口径：解析器离线鲁棒性（确定性管道、零 LLM 调用）；不得写成「模型稳定性」。",
            "离群值/错例全部保留在 detail.mismatches，供人工核对；未做任何剔除。",
            "label 由人工设计规则标注（见夹具 manifest.answer_source），不依赖被测实现。",
            "错例四分类：未救回（能力边界）/ 降级提取（返回内嵌子结构）/ 内容不符 / 过度解析；"
            "后两类与「顶层损坏时的片段提取行为」相关，详情逐条可审计。",
        ],
    )

    print("-" * 66)
    print(f"整体准确率：{correct_total}/{n} = {_rate(correct_total, n)}")
    print(f"direct 一次成功率：{direct_ok}/{len(direct_cases)} = {_rate(direct_ok, len(direct_cases))}")
    print(f"repaired 救回率：{repaired_ok}/{len(repaired_cases)} = {_rate(repaired_ok, len(repaired_cases))}")
    print(f"failed 正确拒绝率：{failed_ok}/{len(failed_cases)} = {_rate(failed_ok, len(failed_cases))}")
    print(
        f"错例构成：未救回 {not_rescued} 条 ｜ 降级提取 {degraded} 条 ｜ "
        f"内容不符 {mismatch} 条 ｜ 过度解析 {over_parsed} 条 ｜ 预期外异常 {errors} 条"
    )
    print("矩阵（期望行 × 实际列）：")
    for label, row in matrix.items():
        print(f"  {label:<9} " + "  ".join(f"{k}={v}" for k, v in row.items()))
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
