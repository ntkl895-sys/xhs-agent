# -*- coding: utf-8 -*-
"""第 27 步 · 可靠性自进化 v2 离线自检（零 token）。

覆盖四块，全部**不联网、不调模型、不碰业务库**：

1. **known-failure（xfail）通道**：状态校验、标记前实测、拒绝「把通过的用例标成失败」、
   标记后版本/统计/指纹、门禁把 xfail 单列（标红不阻断、绝不计通过）；
2. **门禁负例**：坏候选（expected 写错）与「标成 xfail 但其实通过」都必须被拦并自动回滚；
3. **经验提炼器**：五类候选的规则归因、状态一律 pending、零 token；
4. **A/B 门禁**：真实 build_messages 注入 + 确定性假模型，好补丁通过、坏补丁与无变化补丁被否；
5. **候选状态机**：合法迁移放行、非法迁移拒绝。

**自检对夹具当前状态不敏感**：所有版本/条数/状态期望都从启动时的基线推导，
因此夹具已经存在 xfail（例如 norm_107）时同样成立。

**夹具与评测结果在结束时按字节还原**；工作目录用独立临时目录，结束删除。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts/selftest_evolution_v2.py

退出码：0 全绿；1 存在失败断言。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
BACKEND = ROOT / "backend"
for path in (str(SCRIPTS), str(SCRIPTS / "evolution"), str(BACKEND), str(SCRIPTS / "benchmark")):
    if path not in sys.path:
        sys.path.insert(0, path)

import evolve_common as ec  # noqa: E402
import evolve_ab as eab  # noqa: E402
import evolve_close as ecl  # noqa: E402
import evolve_gate as eg  # noqa: E402
import evolve_mark as em  # noqa: E402
import evolve_merge as emg  # noqa: E402
import evolve_reflect as erf  # noqa: E402
import evolve_store as est  # noqa: E402

WORKDIR = ROOT.parent / "evolution_selftest_v2"
TESTSETS = ("normalize", "json_robustness", "compliance")
RESULT_FILES = ("normalize.json", "json_robustness.json", "compliance.json")


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
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = fn(argv)
    return code, buffer.getvalue()


def _snapshot_bytes() -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for name in TESTSETS:
        for item in sorted(ec.testset_dir(name).iterdir()):
            if item.is_file():
                snapshot[f"fixture:{name}/{item.name}"] = item.read_bytes()
    for filename in RESULT_FILES:
        path = ec.RESULTS_DIR / filename
        if path.is_file():
            snapshot[f"result:{filename}"] = path.read_bytes()
    return snapshot


def _restore_bytes(snapshot: dict[str, bytes]) -> None:
    for key, payload in snapshot.items():
        kind, _, rest = key.partition(":")
        if kind == "fixture":
            name, _, filename = rest.partition("/")
            target = ec.testset_dir(name) / filename
        else:
            target = ec.RESULTS_DIR / rest
        if target.read_bytes() != payload:
            target.write_bytes(payload)


def _manifest(name: str) -> dict:
    return ec.load_manifest(name)


def _case(name: str, case_id: str) -> dict:
    _m, cases = ec.load_cases(name)
    return next(c for c in cases if str(c.get("id")) == case_id)


def _decisions(path: Path, items: list[dict]) -> Path:
    return ec.write_json(path, {"reviewed_at": ec.now_iso(), "items": items})


def _xfail_case(case_id: str, raw: str, expected, note: str, actual) -> dict:
    return {
        "id": case_id,
        "category": "invalid" if expected is None else "plain_int",
        "raw": raw,
        "expected": expected,
        "note": note,
        "status": "known_failure",
        "xfail": {
            "expected": expected,
            "actual": actual,
            "root_cause": "自检构造：验证 xfail 通道",
            "source_run_id": "selftest-v2",
            "opened_at": ec.now_iso(),
        },
    }


def _fake_bench_all_green(name: str, timeout: int = 900) -> dict:  # noqa: ARG001
    """注入用：假装该测试集全部通过（只验证转正逻辑，不代表代码已修）。"""
    manifest, cases = ec.load_cases(name)
    records = [
        {
            "id": case.get("id"),
            "correct": True,
            "actual": case.get("expected"),
            "expected": case.get("expected"),
            "outcome": "correct_parse" if case.get("expected") is not None else "correct_reject",
        }
        for case in cases
    ]
    return {
        "rc": 0,
        "stdout": "",
        "result": {
            "detail": {"records": records, "fixture_version": manifest.get("version")},
            "summary": {"n": len(cases), "overall_accuracy": 1.0},
        },
        "result_path": "",
    }


def _ensure_xfail(checker: Checker) -> None:
    """确保 norm_107 处于 known_failure（已是则跳过）。"""
    if ec.case_status(_case("normalize", "norm_107")) == "known_failure":
        return
    code, _ = _quiet(
        em.main,
        ["--testset", "normalize", "--case-id", "norm_107", "--root-cause",
         "_COUNT_PATTERN 前缀匹配未校验后续字符", "--source-run-id", "selftest-v2",
         "--workdir", str(WORKDIR)],
    )
    checker.eq(code, 0, "前置：norm_107 已标为 known_failure")


def main() -> int:
    checker = Checker()
    print("=" * 78)
    print("  第 27 步 · 可靠性自进化 v2 离线自检（零 token）")
    print("=" * 78)

    if WORKDIR.exists():
        shutil.rmtree(WORKDIR, ignore_errors=True)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    baseline = _snapshot_bytes()

    base_manifest = _manifest("normalize")
    base_version = str(base_manifest["version"])
    next_version = ec.bump_version(base_version)
    _m, base_cases = ec.load_cases("normalize")
    base_total = len(base_cases)
    base_counts = ec.status_counts(base_cases)
    base_pass = base_counts.get("passing", 0)
    base_xfail = base_counts.get("known_failure", 0)

    try:
        # ---------------------------------------------------------------- 0
        checker.section("0. 基线快照（期望值全部由基线推导）")
        checker.eq(len(baseline), 9, "快照覆盖 3 套夹具（各 2 文件）+ 3 份评测结果")
        checker.check(bool(base_version), "normalize 基线版本可读", base_version)
        print(f"  基线：v{base_version}，{base_total} 条，passing={base_pass}，known_failure={base_xfail}")
        checker.eq(ec.case_status(_case("normalize", "norm_001")), "passing", "普通用例缺省状态 = passing")
        if base_xfail:
            case_107 = _case("normalize", "norm_107")
            checker.eq(case_107["xfail"].get("actual"), 1, "已有 xfail 的 actual 来自实测（=1）")
            checker.check(bool(case_107["xfail"].get("root_cause")), "已有 xfail 带根因")

        # ---------------------------------------------------------------- 1
        checker.section("1. 状态校验（xfail 证据块必填）")
        problems = ec.validate_normalize_case({"id": "x", "category": "c", "raw": "1", "expected": 1,
                                              "note": "n", "status": "known_failure"}, set())
        checker.check(any("xfail" in item for item in problems), "known_failure 缺 xfail 块被拒", str(problems))
        problems = ec.validate_normalize_case(
            {
                "id": "x", "category": "c", "raw": "1", "expected": 1, "note": "n",
                "status": "known_failure",
                "xfail": {"expected": 1, "actual": 1, "root_cause": "r", "source_run_id": "s", "opened_at": "t"},
            },
            set(),
        )
        checker.check(any("actual" in item for item in problems), "xfail.actual == expected 被拒（不是失败样本）", str(problems))
        problems = ec.validate_normalize_case(
            {"id": "y", "category": "c", "raw": "1", "expected": 1, "note": "n", "status": "bogus"}, set()
        )
        checker.check(any("status 非法" in item for item in problems), "非法 status 被拒", str(problems))

        # ---------------------------------------------------------------- 2
        checker.section("2. 标记前实测（拒绝撒谎 + 拒绝不存在）")
        code, _ = _quiet(
            em.main,
            ["--testset", "normalize", "--case-id", "norm_999", "--root-cause", "x",
             "--source-run-id", "s", "--workdir", str(WORKDIR), "--dry-run"],
        )
        checker.eq(code, 1, "不存在的用例 → 退出码 1")

        code, text = _quiet(
            em.main,
            ["--testset", "normalize", "--case-id", "norm_001", "--root-cause", "x",
             "--source-run-id", "s", "--workdir", str(WORKDIR), "--dry-run"],
        )
        checker.eq(code, 1, "当前通过的用例 → 拒绝标 xfail")
        checker.check("当前实测" in text and "通过" in text, "拒绝理由写明「当前实测通过」")

        code, _ = _quiet(
            em.main,
            ["--testset", "normalize", "--case-id", "norm_107", "--root-cause", "多点小数被前缀匹配",
             "--source-run-id", "selftest-v2", "--workdir", str(WORKDIR), "--dry-run"],
        )
        checker.eq(code, 0, "norm_107 dry-run 通过（已 xfail 时为幂等）")
        checker.eq(str(_manifest("normalize")["version"]), base_version, "dry-run 不改版本")

        # ---------------------------------------------------------------- 3
        checker.section("3. 真实错例标 xfail（norm_107）")
        if base_xfail == 0:
            code, _ = _quiet(
                em.main,
                ["--testset", "normalize", "--case-id", "norm_107", "--root-cause",
                 "_COUNT_PATTERN 用 search 前缀匹配，'1.2.3' 命中 '1.2'",
                 "--source-run-id", "selftest-v2", "--reason", "冻结集评测暴露",
                 "--workdir", str(WORKDIR)],
            )
            checker.eq(code, 0, "标记成功")
            marked = _case("normalize", "norm_107")
            checker.eq(marked.get("status"), "known_failure", "状态已置 known_failure")
            checker.eq(marked["xfail"].get("actual"), 1, "xfail.actual 由实测填充（=1）")
            checker.eq(marked["xfail"].get("expected"), None, "xfail.expected 保留期望（None）")
            manifest = _manifest("normalize")
            checker.eq(manifest["version"], next_version, f"版本 {base_version} → {next_version}")
            checker.eq(manifest["stats"]["total"], base_total, "条数不变")
            checker.eq(manifest["stats"]["status"], {"known_failure": 1, "passing": base_pass - 1}, "stats.status 正确")
            entry = manifest["evolution"]["history"][-1]
            checker.eq(entry.get("action"), "mark_xfail", "history 记录 mark_xfail")
            checker.eq(entry.get("case_status_changes"),
                       [{"id": "norm_107", "from": "passing", "to": "known_failure"}], "history 记录状态迁移")
            checker.check(Path(entry.get("backup_dir") or "").is_dir(), "备份目录已生成")
            expect_pass_after = base_pass - 1
            expect_xfail_after = 1
        else:
            before_version = str(_manifest("normalize")["version"])
            code, _ = _quiet(
                em.main,
                ["--testset", "normalize", "--case-id", "norm_107", "--root-cause", "x",
                 "--source-run-id", "s", "--workdir", str(WORKDIR)],
            )
            checker.eq(code, 0, "已是 known_failure 时重复标记幂等（退出码 0）")
            checker.eq(str(_manifest("normalize")["version"]), before_version, "重复标记不升版本")
            entry = _manifest("normalize")["evolution"]["history"][-1]
            checker.eq(entry.get("action"), "mark_xfail", "history 首条即 mark_xfail（真实案例已落库）")
            expect_pass_after = base_pass
            expect_xfail_after = base_xfail

        # ---------------------------------------------------------------- 4
        checker.section("4. 门禁：xfail 单列、不阻断、绝不计通过")
        code, _ = _quiet(eg.main, ["--testset", "normalize", "--stage", "before", "--workdir", str(WORKDIR)])
        checker.eq(code, 0, "重取基线")
        code, text = _quiet(eg.main, ["--testset", "normalize", "--stage", "after", "--workdir", str(WORKDIR)])
        checker.eq(code, 0, "无变更时门禁通过（退出码 0）")
        checker.check(f"passing {expect_pass_after}/{expect_pass_after}" in text,
                      f"报告写明 passing {expect_pass_after}/{expect_pass_after}", text[-400:])
        checker.check(f"known_failure 仍红 {expect_xfail_after}/{expect_xfail_after}" in text,
                      f"报告写明 known_failure 仍红 {expect_xfail_after}/{expect_xfail_after}", text[-400:])
        report = json.loads(sorted((WORKDIR / "gate").glob("gate-*.json"))[-1].read_text(encoding="utf-8"))
        checker.eq(report["verdict"]["passing_ok"], expect_pass_after, "verdict 的 passing_ok 不含 xfail")
        checker.eq(report["verdict"]["xfail_total"], expect_xfail_after, "verdict 单列 xfail 总数")

        # ---------------------------------------------------------------- 5
        checker.section("5. 并入：passing 与 known_failure 两类新样本")
        _restore_bytes(baseline)
        checker.eq(str(_manifest("normalize")["version"]), base_version, "已还原到基线版本")
        good_case = {
            "id": "norm_901", "category": "wan", "raw": "2.5万", "expected": 25000,
            "note": "自检：当前实现应通过（回归锁定）",
        }
        xfail_case = _xfail_case("norm_902", "1.2.3.4", None, "自检：真实失败样本", 1)
        decisions = _decisions(
            WORKDIR / "review" / "decisions-merge.json",
            [
                {"candidate_id": "c1", "target_testset": "normalize", "decision": "adopt",
                 "case": good_case, "source_run_id": "selftest-v2"},
                {"candidate_id": "c2", "target_testset": "normalize", "decision": "adopt_xfail",
                 "case": xfail_case, "source_run_id": "selftest-v2"},
            ],
        )
        _quiet(eg.main, ["--testset", "normalize", "--stage", "before", "--workdir", str(WORKDIR)])
        code, _ = _quiet(emg.main, ["--decisions", str(decisions), "--workdir", str(WORKDIR)])
        checker.eq(code, 0, "并入成功")
        checker.eq(_manifest("normalize")["version"], next_version, "版本升一级")
        checker.eq(_manifest("normalize")["stats"]["total"], base_total + 2, "条数 +2")
        code, _ = _quiet(eg.main, ["--testset", "normalize", "--stage", "after", "--workdir", str(WORKDIR)])
        checker.eq(code, 0, "门禁通过（回归锁定一致 + xfail 标红）")
        report = json.loads(sorted((WORKDIR / "gate").glob("gate-*.json"))[-1].read_text(encoding="utf-8"))
        checker.eq(report["verdict"]["regression_lock_total"], 1, "新样本分到回归锁定 1 条")
        checker.eq(report["verdict"]["new_xfail_total"], 1, "新样本分到 known_failure 1 条")
        checker.eq(len(report["verdict"]["new_xfail_red"]), 1, "新增 xfail 仍红（不阻断）")
        checker.eq(report["verdict"]["xfail_total"], base_xfail + 1, "xfail 总数 = 既有 + 新增")

        # ---------------------------------------------------------------- 6
        checker.section("6. 门禁负例 A：expected 写错的 passing 候选 → 拦截 + 回滚")
        _restore_bytes(baseline)
        bad_case = {
            "id": "norm_903", "category": "plain_int", "raw": "12345", "expected": 99999,
            "note": "自检：故意写错期望",
        }
        decisions = _decisions(
            WORKDIR / "review" / "decisions-bad.json",
            [{"candidate_id": "c3", "target_testset": "normalize", "decision": "adopt",
              "case": bad_case, "source_run_id": "selftest-v2"}],
        )
        _quiet(eg.main, ["--testset", "normalize", "--stage", "before", "--workdir", str(WORKDIR)])
        _quiet(emg.main, ["--decisions", str(decisions), "--workdir", str(WORKDIR)])
        code, text = _quiet(eg.main, ["--testset", "normalize", "--stage", "after", "--workdir", str(WORKDIR)])
        checker.eq(code, 2, "门禁拦截（退出码 2）")
        checker.check("门禁拦截" in text, "输出写明拦截")
        checker.eq(str(_manifest("normalize")["version"]), base_version, "已回滚到基线版本")
        baseline_sha = json.loads(baseline["fixture:normalize/manifest.json"].decode("utf-8"))["sha256"]["cases.json"]
        checker.eq(ec.sha256_of(ec.load_cases("normalize")[1]), baseline_sha, "回滚后指纹等于基线")

        # ---------------------------------------------------------------- 7
        checker.section("7. 门禁负例 B：标成 xfail 但实际通过 → 拦截")
        _restore_bytes(baseline)
        fake_xfail = _xfail_case("norm_904", "777", 777, "自检：标错状态", None)
        decisions = _decisions(
            WORKDIR / "review" / "decisions-green-xfail.json",
            [{"candidate_id": "c4", "target_testset": "normalize", "decision": "adopt_xfail",
              "case": fake_xfail, "source_run_id": "selftest-v2"}],
        )
        _quiet(eg.main, ["--testset", "normalize", "--stage", "before", "--workdir", str(WORKDIR)])
        _quiet(emg.main, ["--decisions", str(decisions), "--workdir", str(WORKDIR)])
        code, text = _quiet(eg.main, ["--testset", "normalize", "--stage", "after", "--workdir", str(WORKDIR)])
        checker.eq(code, 2, "「假 xfail」被拦截（退出码 2）")
        checker.check("标为 known_failure 但实际通过" in text, "输出点明标注错误", text[-500:])
        checker.eq(str(_manifest("normalize")["version"]), base_version, "已回滚")

        # ---------------------------------------------------------------- 8
        checker.section("8. 缺陷关闭：未修复拒绝 / 修复后转正（注入绿色判定）")
        _restore_bytes(baseline)
        _ensure_xfail(checker)
        version_before_close = str(_manifest("normalize")["version"])
        code, text = _quiet(ecl.main, ["--testset", "normalize", "--case-id", "norm_107",
                                       "--reason", "未修复先试", "--workdir", str(WORKDIR),
                                       "--skip-regression"])
        checker.eq(code, 1, "未修复时拒绝转正")
        checker.check("仍然不通过" in text, "拒绝理由写明仍然不通过", text[-300:])

        original_bench = ec.run_bench
        ec.run_bench = _fake_bench_all_green  # type: ignore[assignment]
        try:
            code, _ = _quiet(ecl.main, ["--testset", "normalize", "--case-id", "norm_107",
                                        "--reason", "注入验证：模拟修复完成", "--workdir", str(WORKDIR),
                                        "--skip-regression"])
        finally:
            ec.run_bench = original_bench  # type: ignore[assignment]
        checker.eq(code, 0, "转绿后允许转正（注入）")
        closed = _case("normalize", "norm_107")
        checker.eq(closed.get("status"), "passing", "状态回到 passing")
        checker.check(closed["xfail"].get("closed_at"), "xfail 记录 closed_at")
        checker.eq(closed["xfail"].get("closed_reason"), "注入验证：模拟修复完成", "记录关闭原因")
        checker.eq(_manifest("normalize")["version"], ec.bump_version(version_before_close),
                   "关闭后版本再升一级")
        hist = _manifest("normalize")["evolution"]["history"][-1]
        checker.eq(hist.get("action"), "close_xfail", "history 记录 close_xfail")
        checker.eq(hist.get("case_status_changes"),
                   [{"id": "norm_107", "from": "known_failure", "to": "passing"}], "history 记录关闭迁移")

        # ---------------------------------------------------------------- 9
        checker.section("9. 回归未绿时拒绝转正")
        _restore_bytes(baseline)
        _ensure_xfail(checker)
        original_reg = eg._run_full_regression
        original_bench = ec.run_bench
        ec.run_bench = _fake_bench_all_green  # type: ignore[assignment]
        eg._run_full_regression = lambda: {"scripts_total": 21, "failures": [{"script": "x.py", "rc": 1, "tail": ["boom"]}], "passed": False}
        try:
            code, text = _quiet(ecl.main, ["--testset", "normalize", "--case-id", "norm_107",
                                           "--reason", "回归红", "--workdir", str(WORKDIR)])
        finally:
            eg._run_full_regression = original_reg  # type: ignore[assignment]
            ec.run_bench = original_bench  # type: ignore[assignment]
        checker.eq(code, 1, "回归红 → 拒绝转正")
        checker.check("全量回归存在失败项" in text, "理由写明回归失败", text[-300:])

        # ---------------------------------------------------------------- 10
        checker.section("10. 经验提炼器（纯规则，零 token）")
        harvest = {
            "harvested_at": ec.now_iso(),
            "source": "selftest",
            "signals": [
                {"signal_id": "json_unrecoverable:r1", "kind": "json_unrecoverable", "source_id": "r1",
                 "run_type": "content", "raw": '{"a": 1', "raw_chars": 7, "evidence": {}},
                {"signal_id": "json_unrecoverable:r2", "kind": "json_unrecoverable", "source_id": "r2",
                 "run_type": "content", "raw": '{"b": [1, 2', "raw_chars": 11, "evidence": {}},
                {"signal_id": "json_fallback:r3", "kind": "json_fallback", "source_id": "r3",
                 "run_type": "topics", "raw": 'prefix {"a": 1} suffix', "raw_chars": 22,
                 "evidence": {"source": "平衡扫描"}},
                {"signal_id": "json_fallback:r4", "kind": "json_fallback", "source_id": "r4",
                 "run_type": "topics", "raw": 'x {"b": 2} y', "raw_chars": 12,
                 "evidence": {"source": "平衡扫描"}},
                {"signal_id": "json_retry:r5", "kind": "json_retry", "source_id": "r5",
                 "run_type": "weekly", "raw": '{"ok": true}', "raw_chars": 13, "evidence": {}},
                {"signal_id": "json_retry:r6", "kind": "json_retry", "source_id": "r6",
                 "run_type": "weekly", "raw": '{"ok": false}', "raw_chars": 14, "evidence": {}},
                {"signal_id": "tool_hallucination:r7", "kind": "tool_hallucination", "source_id": "r7",
                 "run_type": "topic_generation", "raw": "", "evidence": {"requested_tools": ["delete_note"]}},
                {"signal_id": "tool_error:r8", "kind": "tool_error", "source_id": "r8",
                 "run_type": "topic_generation", "raw": "", "evidence": {"tool": "search_knowledge"}},
                {"signal_id": "tool_error:r9", "kind": "tool_error", "source_id": "r9",
                 "run_type": "topic_generation", "raw": "", "evidence": {"tool": "search_knowledge"}},
                {"signal_id": "compliance_hit:n1", "kind": "compliance_hit", "source_id": "n1",
                 "run_type": None, "raw": "内容 A", "evidence": {"hits": [{"word": "根治"}]}},
                {"signal_id": "compliance_hit:n2", "kind": "compliance_hit", "source_id": "n2",
                 "run_type": None, "raw": "内容 B", "evidence": {"hits": [{"word": "根治"}]}},
                {"signal_id": "topic_discarded:t1", "kind": "topic_discarded", "source_id": "t1",
                 "run_type": None, "raw": "", "evidence": {"review_note": "太平"}},
                {"signal_id": "topic_discarded:t2", "kind": "topic_discarded", "source_id": "t2",
                 "run_type": None, "raw": "", "evidence": {"review_note": "太泛"}},
                {"signal_id": "topic_discarded:t3", "kind": "topic_discarded", "source_id": "t3",
                 "run_type": None, "raw": "", "evidence": {"review_note": "无钩子"}},
            ],
        }
        payload = erf.build_candidates(harvest, min_count=2)
        types = {item["type"] for item in payload["candidates"]}
        checker.check({"rule", "prompt_patch", "few_shot", "bug_report", "hit_pattern"} <= types,
                      "五类候选都能产出", str(sorted(types)))
        checker.check(all(item["state"] == "pending" for item in payload["candidates"]), "候选状态一律 pending")
        checker.eq(payload["llm_used"], False, "纯规则模式未使用 LLM")
        hit = next(item for item in payload["candidates"] if item["type"] == "hit_pattern")
        checker.eq(hit["target_testset"], None, "hit_pattern 无目标测试集（只生成不应用）")
        low = erf.build_candidates({"signals": harvest["signals"][:1]}, min_count=2)
        checker.eq(low["candidates_total"], 0, "样本 < min_count 不立候选（不凭单次波动）")

        # ---------------------------------------------------------------- 11
        checker.section("11. 候选状态机")
        _quiet(est.main, ["--workdir", str(WORKDIR), "--ingest", str(
            ec.write_json(WORKDIR / "reflect" / "reflect-demo.json", payload)
        )])
        store = est.load_store(WORKDIR)
        checker.eq(len(store["candidates"]), payload["candidates_total"], "候选已入库")
        first = payload["candidates"][0]["candidate_id"]
        code, _ = _quiet(est.main, ["--workdir", str(WORKDIR), "--transition", first, "--to", "approved"])
        checker.eq(code, 0, "pending → approved 放行")
        code, _ = _quiet(est.main, ["--workdir", str(WORKDIR), "--transition", first, "--to", "rejected"])
        checker.eq(code, 1, "approved → rejected 拒绝（非法迁移）")
        code, _ = _quiet(est.main, ["--workdir", str(WORKDIR), "--transition", first, "--to", "applied"])
        checker.eq(code, 0, "approved → applied 放行")
        code, _ = _quiet(est.main, ["--workdir", str(WORKDIR), "--transition", first, "--to", "closed"])
        checker.eq(code, 0, "applied → closed 放行")
        code, _ = _quiet(est.main, ["--workdir", str(WORKDIR), "--transition", first, "--to", "pending"])
        checker.eq(code, 1, "closed 是终态（拒绝回退）")

        # ---------------------------------------------------------------- 12
        checker.section("12. A/B 门禁（真实 build_messages 注入 + 假模型）")
        cases = {
            "good": "【严格模式】ONLY_JSON：只输出 JSON，不要围栏、不要解释。",
            "bad": "【严格模式】TRUNCATE：先输出半截。",
            "noop": "【严格模式】保持专业语气。",
        }
        codes: dict[str, int] = {}
        for key, text in cases.items():
            path = ec.write_json(
                WORKDIR / "ab" / f"candidate-{key}.json",
                {"candidate_id": f"ab_{key}", "type": "prompt_patch",
                 "patch": {"target": "topics", "text": text}},
            )
            codes[key], _ = _quiet(eab.main, ["--candidate", str(path), "--target", "synthetic",
                                              "--workdir", str(WORKDIR)])
        checker.eq(codes["good"], 0, "好补丁 approved（退出码 0）")
        checker.eq(codes["bad"], 2, "坏补丁 rejected（退出码 2）")
        checker.eq(codes["noop"], 2, "无变化补丁 rejected（退出码 2）")
        reports = sorted((WORKDIR / "ab").glob("ab-*.json"))
        checker.eq(len(reports), 3, "三份 A/B 报告全部留档")
        verdicts = [json.loads(item.read_text(encoding="utf-8"))["decision"]["verdict"] for item in reports]
        checker.check("improved" in verdicts, "好补丁判定 improved")
        checker.check("degraded" in verdicts, "坏补丁判定 degraded")
        checker.check("no_change" in verdicts, "无变化补丁判定 no_change")

        # ---------------------------------------------------------------- 13
        checker.section("13. 夹具零残留（按字节还原）")
        _restore_bytes(baseline)
        for name in TESTSETS:
            manifest = _manifest(name)
            cases_now = ec.load_cases(name)[1]
            checker.eq(manifest["sha256"]["cases.json"], ec.sha256_of(cases_now), f"{name} 指纹自校验")
            checker.eq((ec.testset_dir(name) / "manifest.json").read_bytes(),
                       baseline[f"fixture:{name}/manifest.json"], f"{name} manifest 与基线字节一致")
            checker.eq((ec.testset_dir(name) / "cases.json").read_bytes(),
                       baseline[f"fixture:{name}/cases.json"], f"{name} cases 与基线字节一致")
        for filename in RESULT_FILES:
            key = f"result:{filename}"
            if key in baseline:
                checker.eq((ec.RESULTS_DIR / filename).read_bytes(), baseline[key], f"{filename} 已按字节还原")
    finally:
        _restore_bytes(baseline)
        shutil.rmtree(WORKDIR, ignore_errors=True)

    print("\n" + "=" * 78)
    print(f"  结果：{checker.passed} PASS / {checker.failed} FAIL")
    if checker.failures:
        print("  失败项：" + "；".join(checker.failures))
    print("=" * 78)
    return 0 if checker.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
