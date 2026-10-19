# -*- coding: utf-8 -*-
"""第 26 步 · 自进化飞轮自检（离线、零 token、零真实 LLM 调用）。

用**合成事件**把五步闭环完整走一遍，并对「门禁是否真的拦得住」做负例验证：

1. 采集：合成 9 类信号（兜底解析 / 不可救回 / 重问 / 失败 / 半成品 / 工具错误 /
   越权工具 / 合规命中 / 人工废弃选题 + 超长 raw），断言分类与降级正确；
2. 候选：断言只对有冻结集的信号产候选，且 id 不冲突、label 与 expected 自洽；
3. 确认：非交互 decisions 覆盖 采纳 / 拒绝 / 跳过 / 非法四态，断言未列出的按 skip；
4. 并入：断言版本 +1、条数 +1、sha256 变化、备份存在、evolution.history 可追溯；
5. 门禁：断言 legacy 逐条不变、新样本一致性 1.0、score_all 只记录；
6. **坏候选**：expected 写错 → 门禁必须拦截并自动回滚（版本/指纹/条数回到并入前）；
7. **非法候选**：缺字段 → merge 直接拒绝，夹具字节不变；
8. 收尾：两套夹具与两份评测结果**按字节还原**，工作目录删除，断言指纹回到基线。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts/selftest_evolution.py

退出码：0 全部通过；1 存在失败项。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVOLUTION_DIR = ROOT / "scripts" / "evolution"
if str(EVOLUTION_DIR) not in sys.path:
    sys.path.insert(0, str(EVOLUTION_DIR))

import evolve_common as ec  # noqa: E402
import evolve_gate  # noqa: E402
import evolve_harvest  # noqa: E402
import evolve_merge  # noqa: E402
import evolve_propose  # noqa: E402
import evolve_review  # noqa: E402

RESULT_FILES = ("json_robustness.json", "compliance.json")
TESTSETS = ("json_robustness", "compliance")


# ---------------------------------------------------------------------------
# 断言框架
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [PASS] {label}")
        else:
            self.failed += 1
            self.failures.append(label)
            print(f"  [FAIL] {label}" + (f" —— {detail}" if detail else ""))
        return ok

    def eq(self, actual, expected, label: str) -> bool:
        return self.check(actual == expected, label, f"实际 {actual!r} ≠ 期望 {expected!r}")

    def section(self, title: str) -> None:
        print(f"\n== {title} ==")


def _quiet(fn, argv: list[str]) -> tuple[int, str]:
    """跑各步 main()，吞掉噪声输出但保留文本（失败时便于定位）。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = fn(argv)
    return code, buffer.getvalue()


# ---------------------------------------------------------------------------
# 合成事件
# ---------------------------------------------------------------------------
def _synthetic_events() -> dict:
    padding = "长" * 4200
    return {
        "runs": [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "run_type": "content",
                "status": "success",
                "json_parse_attempts": 1,
                "llm_raw": '```json\n{"title": "秋日通勤穿搭", "tags": ["毛衣", "通勤"]}\n```',
                "inputs": {"topic_id": "t-1"},
                "outputs": {},
                "error_message": None,
                "created_at": "2026-10-18T09:00:00+08:00",
            },
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "run_type": "diagnosis",
                "status": "partial",
                "json_parse_attempts": 3,
                "llm_raw": '{"summary": "这篇笔记的完播率偏低，主要问题在开头三秒没有钩子',
                "inputs": {"note_id": "n-2"},
                "outputs": {},
                "error_message": None,
                "created_at": "2026-10-18T09:05:00+08:00",
            },
            {
                "id": "33333333-3333-4333-8333-333333333333",
                "run_type": "topic_generation",
                "status": "success",
                "json_parse_attempts": 1,
                "llm_raw": '{"candidates": []}',
                "inputs": {"n": 3},
                "outputs": {
                    "tool_calling": {
                        "enabled": True,
                        "rounds": 2,
                        "hallucinated": [],
                        "calls": [
                            {"tool": "search_hotspots", "args": {"q": "护肤"}, "ok": True},
                            {
                                "tool": "search_knowledge",
                                "args": {"query": "护肤"},
                                "ok": False,
                                "result_summary": "工具异常已降级：timeout",
                            },
                        ],
                    }
                },
                "error_message": None,
                "created_at": "2026-10-18T09:10:00+08:00",
            },
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "run_type": "topic_generation",
                "status": "success",
                "json_parse_attempts": 1,
                "llm_raw": '{"candidates": []}',
                "inputs": {"n": 3},
                "outputs": {
                    "tool_calling": {
                        "enabled": True,
                        "rounds": 1,
                        "hallucinated": ["delete_note"],
                        "calls": [],
                    }
                },
                "error_message": None,
                "created_at": "2026-10-18T09:15:00+08:00",
            },
            {
                "id": "55555555-5555-4555-8555-555555555555",
                "run_type": "weekly_report",
                "status": "failed",
                "json_parse_attempts": None,
                "llm_raw": None,
                "inputs": {"days": 7},
                "outputs": {},
                "error_message": "AgentNetworkError: 上游超时",
                "created_at": "2026-10-18T09:20:00+08:00",
            },
            {
                "id": "88888888-8888-4888-8888-888888888888",
                "run_type": "content_variants",
                "status": "success",
                "json_parse_attempts": 1,
                "llm_raw": f"```json\n{{\"variants\": [\"{padding}\"]}}\n```",
                "inputs": {"topic_id": "t-9"},
                "outputs": {},
                "error_message": None,
                "created_at": "2026-10-18T09:22:00+08:00",
            },
        ],
        "notes": [
            {
                "id": "66666666-6666-4666-8666-666666666666",
                "title": "祛斑两个月记录",
                "body": "宝宝脸上的斑坚持调理了两个月，终于祛斑成功，太开心了！",
                "compliance_status": "warning",
                "content_check": {
                    "status": "warning",
                    "hits": [{"word": "祛斑", "category": "medical"}],
                    "checked_fields": ["title", "body", "tags"],
                },
                "status": "draft",
                "created_at": "2026-10-18T09:25:00+08:00",
            }
        ],
        "topics": [
            {
                "id": "77777777-7777-4777-8777-777777777777",
                "title": "医美项目避坑指南",
                "angle": "亲历者视角",
                "summary": "写自己做医美的踩坑经历",
                "status": "discarded",
                "review_note": "踩红线：涉及医疗项目推荐",
                "created_at": "2026-10-18T09:30:00+08:00",
            }
        ],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    checker = Checker()
    workdir = ROOT.parent / "evolution" / f"_selftest-{_stamp()}"
    ec.ensure_dir(workdir)

    baseline: dict[str, dict] = {}
    result_bytes: dict[str, bytes] = {}
    fixture_bytes: dict[str, bytes] = {}

    try:
        # ---------------------------------------------------------------- 0
        checker.section("0. 基线快照（结束时必须按字节还原）")
        for name in TESTSETS:
            info = ec.verify_frozen(name)
            backup_dir = ec.backup_testset(name, workdir, tag="selftest-baseline")
            baseline[name] = {**info, "backup": str(backup_dir)}
            fixture_bytes[name] = ec.file_sha256(ec.testset_dir(name) / "cases.json")
            checker.check(
                (ec.testset_dir(name) / "manifest.json").exists(), f"{name} 冻结夹具可读（v{info['version']}）"
            )
        for filename in RESULT_FILES:
            path = ec.RESULTS_DIR / filename
            result_bytes[filename] = path.read_bytes()
            checker.check(path.exists(), f"评测结果存在：{filename}")

        # ---------------------------------------------------------------- 1
        checker.section("1. 采集（只读，合成事件）")
        events_path = ec.write_json(workdir / "events.json", _synthetic_events())
        harvest_path = workdir / "harvest.json"
        code, _ = _quiet(
            evolve_harvest.main,
            ["--from-json", str(events_path), "--workdir", str(workdir), "--out", str(harvest_path)],
        )
        checker.eq(code, 0, "harvest 退出码 0")
        harvest = ec.read_json(harvest_path)
        checker.eq(harvest["signals_total"], 10, "信号 10 条（9 类 + 超长 raw 1 条）")
        expected_kinds = {
            "json_fallback", "json_unrecoverable", "json_retry", "run_failed",
            "run_partial", "tool_error", "tool_hallucination", "compliance_hit",
            "topic_discarded",
        }
        checker.eq(set(harvest["by_kind"]), expected_kinds, "9 类信号全部覆盖")
        checker.eq(harvest["deferred_total"], 5, "无对应冻结集的信号 5 条")
        oversize = [s for s in harvest["signals"] if s["source_id"].startswith("88888888")]
        checker.check(
            bool(oversize) and oversize[0]["target_testset"] is None
            and "超过" in (oversize[0]["deferred_reason"] or ""),
            "超长 raw 被降级为 deferred（不把半截文本当样本）",
            str(oversize[:1]),
        )
        checker.check(
            all("sk-" not in json.dumps(s, ensure_ascii=False) for s in harvest["signals"]),
            "采集清单不含密钥形态",
        )

        # ---------------------------------------------------------------- 2
        checker.section("2. 候选生成（规则化，零 token）")
        candidates_path = workdir / "candidates.json"
        code, _ = _quiet(
            evolve_propose.main,
            ["--harvest", str(harvest_path), "--workdir", str(workdir), "--out", str(candidates_path)],
        )
        checker.eq(code, 0, "propose 退出码 0")
        proposal = ec.read_json(candidates_path)
        checker.eq(proposal["candidates_total"], 3, "3 条候选（json×2 + compliance×1）")
        checker.eq(proposal["skipped_total"], 0, "无可疑信号被跳过")
        by_kind = {c["signal_kind"]: c for c in proposal["candidates"]}
        fallback = by_kind.get("json_fallback", {})
        unrecoverable = by_kind.get("json_unrecoverable", {})
        checker.eq(fallback.get("case", {}).get("label"), "repaired", "兜底样本 → label=repaired")
        checker.eq(
            fallback.get("case", {}).get("expected"),
            {"title": "秋日通勤穿搭", "tags": ["毛衣", "通勤"]},
            "repaired 候选带完整 expected（供内容一致性判定）",
        )
        checker.eq(unrecoverable.get("case", {}).get("label"), "failed", "不可救回样本 → label=failed")
        checker.eq(unrecoverable.get("case", {}).get("expected"), None, "failed 候选 expected 为 null")
        checker.eq(unrecoverable.get("case", {}).get("bucket"), "truncated", "截断样本归入 truncated 桶")
        ids = [c["case"]["id"] for c in proposal["candidates"]]
        checker.eq(len(set(ids)), len(ids), "候选 id 互不冲突")
        checker.check(
            all(c["expected_source"] for c in proposal["candidates"]), "每条候选都标注期望来源"
        )
        checker.check(
            not any(c["target_testset"] == "topic_tool" for c in proposal["candidates"]),
            "工具类信号不产候选（缺场景骨架，不硬造）",
        )

        # ---------------------------------------------------------------- 3
        checker.section("3. 人工确认门（采纳 / 拒绝 / 跳过 / 非法）")
        cid = {c["signal_kind"]: c["candidate_id"] for c in proposal["candidates"]}
        decisions_in = {
            cid["json_fallback"]: {"decision": "adopt"},
            cid["json_unrecoverable"]: {"decision": "reject", "reason": "自检：拒绝路径"},
            # compliance 不出现 → 必须按 skip 处理
        }
        decisions_path = ec.write_json(workdir / "decisions.json", decisions_in)
        decisions_out = workdir / "decisions_out.json"
        code, _ = _quiet(
            evolve_review.main,
            ["--candidates", str(candidates_path), "--decisions", str(decisions_path),
             "--workdir", str(workdir), "--out", str(decisions_out)],
        )
        checker.eq(code, 0, "review 退出码 0")
        reviewed = ec.read_json(decisions_out)
        checker.eq(reviewed["adopted_total"], 1, "只采纳 1 条")
        statuses = {item["candidate_id"]: item["decision"] for item in reviewed["items"]}
        checker.eq(statuses.get(cid["compliance_hit"]), "skip", "未出现在 decisions 里的候选 = skip")
        checker.eq(statuses.get(cid["json_unrecoverable"]), "reject", "拒绝路径生效")
        checker.eq(
            ec.file_sha256(ec.testset_dir("json_robustness") / "cases.json"),
            fixture_bytes["json_robustness"],
            "review 阶段夹具字节未变（此刻还没并入）",
        )

        # 非法候选：缺 note → 必须被拒
        invalid_candidates = {
            "candidates": [
                {
                    "candidate_id": "cand_bad_schema",
                    "signal_id": "x",
                    "signal_kind": "json_fallback",
                    "target_testset": "json_robustness",
                    "case": {"id": "json_999", "bucket": "fence", "raw": "{", "label": "repaired",
                             "expected": {}, },
                    "expected_source": "自检",
                }
            ]
        }
        invalid_path = ec.write_json(workdir / "invalid_candidates.json", invalid_candidates)
        invalid_out = workdir / "invalid_decisions.json"
        _quiet(
            evolve_review.main,
            ["--candidates", str(invalid_path), "--decisions", str(ec.write_json(
                workdir / "invalid_in.json", {"cand_bad_schema": {"decision": "adopt"}}
            )), "--workdir", str(workdir), "--out", str(invalid_out)],
        )
        invalid_reviewed = ec.read_json(invalid_out)
        checker.eq(
            invalid_reviewed["items"][0]["decision"], "rejected_invalid", "缺字段的候选被静态门拒绝"
        )

        # ---------------------------------------------------------------- 4
        checker.section("4. 门禁基线（并入前）")
        for name in TESTSETS:
            code, _ = _quiet(evolve_gate.main, ["--testset", name, "--stage", "before",
                                               "--workdir", str(workdir)])
            checker.eq(code, 0, f"{name} 基线采集成功")

        # ---------------------------------------------------------------- 5
        checker.section("5. 版本化并入")
        argv = ["--decisions", str(decisions_out), "--workdir", str(workdir)]
        code, output = _quiet(evolve_merge.main, argv)
        checker.eq(code, 0, "merge 退出码 0")
        after_manifest, after_cases = ec.load_cases("json_robustness")
        checker.eq(after_manifest["version"], "1.1", "版本 1.0 → 1.1")
        checker.eq(len(after_cases), 201, "条数 200 → 201")
        checker.check(
            after_manifest["sha256"]["cases.json"] != baseline["json_robustness"]["sha256"],
            "sha256 已随内容重算",
        )
        checker.eq(
            after_manifest["stats"]["total"], 201, "manifest.stats 与内容同步"
        )
        history = (after_manifest.get("evolution") or {}).get("history") or []
        checker.eq(len(history), 1, "evolution.history 记录 1 条合并")
        checker.check(
            bool(history and history[0].get("source_run_ids")), "合并记录含来源 run id（可追溯）"
        )
        backups = list((workdir / "backups" / "json_robustness").glob("*pre-merge*"))
        checker.check(bool(backups), "并入前已生成备份")

        # 非法候选：merge 直接拒绝，文件字节不变
        hash_before_invalid = ec.file_sha256(ec.testset_dir("json_robustness") / "cases.json")
        invalid_merge_decisions = ec.write_json(
            workdir / "invalid_merge.json",
            {
                "items": [
                    {
                        "candidate_id": "cand_bad_schema",
                        "target_testset": "json_robustness",
                        "decision": "adopt",
                        "case": {"id": "json_999", "bucket": "fence", "raw": "{", "label": "repaired",
                                 "expected": {}},
                        "signal_kind": "json_fallback",
                        "signal_id": "x",
                    }
                ]
            },
        )
        code, _ = _quiet(
            evolve_merge.main,
            ["--decisions", str(invalid_merge_decisions), "--workdir", str(workdir)],
        )
        checker.eq(code, ec.EXIT_FAIL, "非法候选被 merge 拒绝（退出码 1）")
        checker.eq(
            ec.file_sha256(ec.testset_dir("json_robustness") / "cases.json"),
            hash_before_invalid,
            "拒绝后夹具字节未变",
        )

        # ---------------------------------------------------------------- 6
        checker.section("6. 门禁通过（好候选）")
        gate_report_path = workdir / "gate_good.json"
        code, output = _quiet(
            evolve_gate.main,
            ["--testset", "json_robustness", "--stage", "after", "--workdir", str(workdir)],
        )
        checker.eq(code, 0, "好候选门禁通过（退出码 0）")
        reports = sorted((workdir / "gate").glob("gate-*.json"))
        latest = ec.read_json(reports[-1])
        checker.check(latest["passed"] is True, "门禁报告 passed=true")
        checker.eq(latest["verdict"]["legacy_invariant"], True, "legacy_invariant 通过（旧样本逐条不变）")
        checker.eq(latest["verdict"]["new_case_consistency"], 1.0, "新样本一致性 1.0")
        checker.check(
            latest["verdict"]["score_all_after"] is not None
            and latest["verdict"]["score_all_before"] is not None,
            "score_all 前后都记录（仅记录不作门禁）",
        )

        # ---------------------------------------------------------------- 7
        checker.section("7. 门禁拦截 + 自动回滚（坏候选：expected 写错）")
        # 每次并入前都要重取基线，否则「与基线一致」比的是陈旧状态
        code, _ = _quiet(
            evolve_gate.main,
            ["--testset", "json_robustness", "--stage", "before", "--workdir", str(workdir)],
        )
        checker.eq(code, 0, "坏候选并入前重取基线")
        version_before_bad = ec.load_manifest("json_robustness")["version"]
        n_before_bad = len(ec.load_cases("json_robustness")[1])
        sha_before_bad = ec.load_manifest("json_robustness")["sha256"]["cases.json"]

        bad_case = {
            "id": "json_203",
            "bucket": "fence",
            "raw": '```json\n{"title": "秋日通勤穿搭", "tags": ["毛衣", "通勤"]}\n```',
            "label": "repaired",
            "expected": {"title": "这条期望是错的", "tags": []},
            "note": "坏候选：expected 与真实解析结果不符，用于验证门禁",
        }
        bad_decisions = ec.write_json(
            workdir / "bad_decisions.json",
            {
                "items": [
                    {
                        "candidate_id": "cand_bad",
                        "target_testset": "json_robustness",
                        "decision": "adopt",
                        "case": bad_case,
                        "signal_kind": "json_fallback",
                        "signal_id": "json_fallback:bad",
                    }
                ]
            },
        )
        code, _ = _quiet(
            evolve_merge.main, ["--decisions", str(bad_decisions), "--workdir", str(workdir)]
        )
        checker.eq(code, 0, "坏候选能并入（静态校验拦不住语义错误——这正是门禁的意义）")
        checker.eq(len(ec.load_cases("json_robustness")[1]), n_before_bad + 1, "坏候选已进集")

        code, output = _quiet(
            evolve_gate.main,
            ["--testset", "json_robustness", "--stage", "after", "--workdir", str(workdir)],
        )
        checker.eq(code, ec.EXIT_GATED, "门禁拦截（退出码 2）")
        reports = sorted((workdir / "gate").glob("gate-*.json"))
        bad_report = ec.read_json(reports[-1])
        checker.eq(bad_report["passed"], False, "门禁报告 passed=false")
        checker.check(
            bad_report["verdict"]["new_case_failed"] == ["json_203"],
            "被拦住的正是那条 bad case",
            str(bad_report["verdict"]["new_case_failed"]),
        )
        checker.eq(bad_report["rolled_back"], True, "已自动回滚")
        checker.eq(bad_report["restore_verified"], True, "回滚后夹具与备份逐字节一致")
        checker.eq(bad_report["restore_matches_baseline"], True, "回滚后复跑与基线逐条一致")
        restored_manifest = ec.load_manifest("json_robustness")
        checker.eq(restored_manifest["version"], version_before_bad, f"版本回到 {version_before_bad}")
        checker.eq(
            len(ec.load_cases("json_robustness")[1]), n_before_bad, "条数回到并入前"
        )
        checker.eq(
            restored_manifest["sha256"]["cases.json"], sha_before_bad, "指纹回到并入前（逐字节可复现）"
        )

        # ---------------------------------------------------------------- 8
        checker.section("8. 收尾：按字节还原")
        for name in TESTSETS:
            ec.restore_backup(name, Path(baseline[name]["backup"]))
            info = ec.verify_frozen(name)
            checker.eq(info["version"], baseline[name]["version"], f"{name} 版本回到 {baseline[name]['version']}")
            checker.eq(
                ec.file_sha256(ec.testset_dir(name) / "cases.json"),
                fixture_bytes[name],
                f"{name} cases.json 与基线逐字节一致",
            )
        for filename in RESULT_FILES:
            expected = hashlib.sha256(result_bytes[filename]).hexdigest()
            (ec.RESULTS_DIR / filename).write_bytes(result_bytes[filename])
            checker.eq(
                ec.file_sha256(ec.RESULTS_DIR / filename),
                expected,
                f"{filename} 评测结果已按字节还原",
            )
    finally:
        # 兜底还原：正常路径在 §8 已还原过一次，这里防止中途异常把夹具留在「已并入」状态
        for name in TESTSETS:
            if name in baseline:
                with contextlib.suppress(Exception):
                    ec.restore_backup(name, Path(baseline[name]["backup"]))
        for filename, data in result_bytes.items():
            with contextlib.suppress(Exception):
                (ec.RESULTS_DIR / filename).write_bytes(data)
        shutil.rmtree(workdir, ignore_errors=True)
        checker.check(not workdir.exists(), "工作目录已清理（零残留）")

    print("\n" + "=" * 66)
    print(f"结果：{checker.passed} PASS / {checker.failed} FAIL")
    if checker.failed:
        for item in checker.failures:
            print(f"  - {item}")
        return 1
    return 0


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S")


if __name__ == "__main__":
    sys.exit(main())
