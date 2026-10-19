# -*- coding: utf-8 -*-
"""第 26 步 · 人在环里的自进化飞轮 v1 —— 公共基座。

定位：**离线进化管道**，不是新 Agent、不是新业务图。
本包只做「只读采集 → 候选生成 → 人工确认 → 版本化并入 → 评测门禁」五步，
不 import 任何业务写接口、不发真实 LLM 请求、不写业务库。

硬约束（与任务书一致）：
- 零数据库迁移、零新增 LangGraph 图、零新增第三方依赖、harness 基座八文件零改动；
- 候选先落仓库外工作目录（默认 ``<workspace>/evolution``），
  **只有 review 采纳 + merge 显式执行**才会改动 ``docs/benchmark/fixtures/testsets``；
- 任何并入前先整目录备份，门禁失败自动按备份还原（回滚可验证）。

目录约定：

    docs/benchmark/fixtures/testsets/<name>/   ← 冻结夹具（本包唯一会写的仓库位置）
    docs/benchmark/results/testsets/<name>.json ← 评测结果（由既有 bench 脚本重写）
    <workdir>/harvest|review|evidence|merges|gate|backups/  ← 进化工作区（仓库外）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]          # xhs-agent/
BACKEND_DIR = ROOT / "backend"
TESTSETS_DIR = ROOT / "docs" / "benchmark" / "fixtures" / "testsets"
RESULTS_DIR = ROOT / "docs" / "benchmark" / "results" / "testsets"

_TZ8 = timezone(timedelta(hours=8))

EXIT_OK = 0        # 正常完成
EXIT_FAIL = 1      # 校验失败 / 候选非法
EXIT_GATED = 2     # 门禁拦截并已回滚
EXIT_ENV = 3       # 环境不满足

#: json_robustness 的 8 个分桶（与 build_testsets.py 保持一致，新增桶须升版本并在报告说明）
JSON_BUCKETS = ("valid", "fence", "ctrl", "bracket", "trailing", "truncated", "quotes", "broken")
JSON_LABELS = ("direct", "repaired", "failed")

#: 用例状态（v2 新增；缺省视为 passing，保证旧夹具零改动可读）
#: - passing       当前实现应通过；判定变化即回归
#: - known_failure 人工确认「期望正确、当前实现做不到」；标红追踪，不计通过、不阻断
#: - pending       已并入但尚未人工裁决；只记录，不参与门禁
CASE_STATUSES = ("passing", "known_failure", "pending")

#: xfail 必填字段（缺一不可：没有根因假设与来源的 xfail 等于「把 bug 藏起来」）
XFAIL_FIELDS = ("expected", "actual", "root_cause", "source_run_id", "opened_at")

#: 候选状态机（v2；存工作目录 JSON，零迁移）
CANDIDATE_STATES = ("pending", "approved", "rejected", "xfail", "applied", "closed")

#: 允许的状态迁移（其余一律拒绝）
CANDIDATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("approved", "rejected", "xfail"),
    "approved": ("applied",),
    "xfail": ("applied", "closed"),
    "applied": ("closed",),
    "rejected": (),
    "closed": (),
}

#: 采集信号 → 目标测试集（None = 当前无对应冻结集，只登记不产候选）
SIGNAL_TARGETS: dict[str, str | None] = {
    "json_fallback": "json_robustness",     # 兜底救回（去围栏/控制字符/平衡扫描）
    "json_retry": "json_robustness",        # 重问后才成功
    "json_unrecoverable": "json_robustness",  # 有原始输出但三层兜底全失败
    "run_failed": "json_robustness",        # run 失败（有 raw 才有意义）
    "run_partial": "json_robustness",       # run 半成品
    "compliance_hit": "compliance",         # 真实内容命中违禁词
    "tool_error": None,                     # 缺 persona/hotspot 骨架，需人工补齐
    "tool_hallucination": None,             # 同上
    "topic_discarded": None,                # 选题质量类，当前无对应冻结集
}

#: 同一 run 命中多个信号时的取舍顺序（越靠前越优先产候选）
SIGNAL_PRIORITY = (
    "json_fallback",
    "json_unrecoverable",
    "json_retry",
    "run_failed",
    "run_partial",
    "tool_hallucination",
    "tool_error",
    "compliance_hit",
    "topic_discarded",
)


def now_iso() -> str:
    """带时区的 ISO 8601（与 bench_common.now_iso 同格式）。"""
    return datetime.now(_TZ8).isoformat(timespec="seconds")


def default_workdir() -> Path:
    """仓库外工作目录；可用环境变量 ``EVOLUTION_WORKDIR`` 覆盖。"""
    raw = os.environ.get("EVOLUTION_WORKDIR")
    return Path(raw) if raw else ROOT.parent / "evolution"


def add_backend_to_path() -> None:
    """把 backend 加入 sys.path（只为了 import 只读工具，如 json_repair）。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def redact(value: Any) -> Any:
    """脱敏：复用 harness 的 ``recorder.redact_secrets``（同一条安全规则，不另写一套）。"""
    add_backend_to_path()
    from app.agent.tracing.recorder import redact_secrets  # noqa: PLC0415

    return redact_secrets(value)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# JSON / 指纹
# ---------------------------------------------------------------------------
def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> Path:
    """UTF-8（无 BOM）+ 2 空格缩进 + 结尾换行，与既有夹具格式一致。"""
    target = Path(path)
    ensure_dir(target.parent)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def sha256_of(payload: Any) -> str:
    """与 build_testsets._sha256_of / bench_common 完全一致的口径。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """字节级指纹（用于「文件没被改动」这类断言，与内容指纹不是一回事）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bump_version(version: str) -> str:
    """把最后一段数字 +1：``1.0`` → ``1.1``；``1.0.0`` → ``1.0.1``。"""
    parts = str(version).split(".")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].isdigit():
            parts[index] = str(int(parts[index]) + 1)
            return ".".join(parts)
    return f"{version}.1"


# ---------------------------------------------------------------------------
# 冻结夹具读写
# ---------------------------------------------------------------------------
def testset_dir(name: str) -> Path:
    return TESTSETS_DIR / name


def load_manifest(name: str) -> dict:
    return read_json(testset_dir(name) / "manifest.json")


def load_cases(name: str) -> tuple[dict, list[dict]]:
    """读 manifest + cases（**不校验指纹**；校验请用 :func:`verify_frozen`）。"""
    manifest = load_manifest(name)
    cases = read_json(testset_dir(name) / "cases.json")
    if not isinstance(cases, list):
        raise RuntimeError(f"{name}/cases.json 顶层应是数组，实际是 {type(cases).__name__}")
    return manifest, cases


def verify_frozen(name: str) -> dict:
    """按 manifest 重算内容指纹；不匹配直接抛错（与评测脚本同口径）。"""
    manifest, cases = load_cases(name)
    digest = sha256_of(cases)
    expected = (manifest.get("sha256") or {}).get("cases.json")
    if digest != expected:
        raise RuntimeError(
            f"{name}/cases.json 与 manifest 冻结指纹不符：\n"
            f"  实际 {digest}\n  期望 {expected}"
        )
    return {"name": name, "version": manifest.get("version"), "n": len(cases), "sha256": digest}


# ---------------------------------------------------------------------------
# 用例状态（v2：passing / known_failure / pending）
# ---------------------------------------------------------------------------
def case_status(case: Any) -> str:
    """读用例状态；缺省 = passing（旧夹具无需改动）。非法值按 passing 读，靠校验器拦截。"""
    value = str((case or {}).get("status") or "passing").strip().lower()
    return value if value in CASE_STATUSES else "passing"


def case_xfail(case: Any) -> dict:
    block = (case or {}).get("xfail")
    return block if isinstance(block, dict) else {}


def case_statuses(name: str) -> dict[str, str]:
    """``{case_id: status}``（读当前夹具，不校验指纹）。"""
    _manifest, cases = load_cases(name)
    return {str(case.get("id")): case_status(case) for case in cases}


def status_counts(cases: list[dict]) -> dict[str, int]:
    counts = {status: 0 for status in CASE_STATUSES}
    for case in cases:
        counts[case_status(case)] = counts.get(case_status(case), 0) + 1
    return {key: value for key, value in counts.items() if value}


def recompute_stats(name: str, cases: list[dict]) -> dict:
    """按夹具自身口径重算 stats（并入 / 改状态后必须更新，否则 manifest 与内容不一致）。"""
    if name == "json_robustness":
        buckets: dict[str, int] = {}
        labels: dict[str, int] = {}
        for case in cases:
            buckets[case["bucket"]] = buckets.get(case["bucket"], 0) + 1
            labels[case["label"]] = labels.get(case["label"], 0) + 1
        return {
            "total": len(cases),
            "buckets": dict(sorted(buckets.items())),
            "labels": dict(sorted(labels.items())),
            "status": status_counts(cases),
        }
    if name == "compliance":
        groups: dict[str, int] = {}
        positives = 0
        for case in cases:
            groups[case["group"]] = groups.get(case["group"], 0) + 1
            positives += 1 if case.get("expect_hit") else 0
        return {
            "total": len(cases),
            "positives": positives,
            "negatives": len(cases) - positives,
            "groups": dict(sorted(groups.items())),
            "status": status_counts(cases),
        }
    if name == "normalize":
        categories: dict[str, int] = {}
        none_expected = 0
        for case in cases:
            categories[case["category"]] = categories.get(case["category"], 0) + 1
            none_expected += 1 if case.get("expected") is None else 0
        return {
            "total": len(cases),
            "categories": dict(sorted(categories.items())),
            "none_expected": none_expected,
            "numeric_expected": len(cases) - none_expected,
            "status": status_counts(cases),
        }
    raise RuntimeError(f"未定义的测试集：{name}（当前支持 json_robustness / compliance / normalize）")


# ---------------------------------------------------------------------------
# 用例校验（并入前的最后一道静态门）
# ---------------------------------------------------------------------------
def _missing(case: Any, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(case, dict):
        return [f"case 必须是对象，实际是 {type(case).__name__}"]
    return [f"缺少字段 {key}" for key in keys if key not in case]


def _status_problems(case: dict) -> list[str]:
    """状态字段与 xfail 证据块校验（v2）。"""
    problems: list[str] = []
    if "status" not in case:
        return problems
    status = str(case.get("status") or "").strip().lower()
    if status not in CASE_STATUSES:
        problems.append(f"status 非法：{case.get('status')!r}（允许 {list(CASE_STATUSES)}）")
        return problems
    if status != "known_failure":
        return problems
    block = case.get("xfail")
    if not isinstance(block, dict):
        problems.append("status=known_failure 时必须带 xfail 对象（期望正确答案/当前行为/根因/来源）")
        return problems
    for field in XFAIL_FIELDS:
        if field not in block:
            problems.append(f"xfail 缺少字段 {field}")
    if "actual" in block and "expected" in block and block.get("actual") == block.get("expected"):
        problems.append("xfail.actual 与 xfail.expected 相同——这不是失败样本，不该标 known_failure")
    return problems


def validate_json_case(case: Any, existing_ids: set[str]) -> list[str]:
    problems = _missing(case, ("id", "bucket", "raw", "label", "expected", "note"))
    if problems:
        return problems
    case_id = case["id"]
    if not isinstance(case_id, str) or not case_id.strip():
        problems.append("id 必须是非空字符串")
    elif case_id in existing_ids:
        problems.append(f"id 重复：{case_id}")
    if case["bucket"] not in JSON_BUCKETS:
        problems.append(f"bucket 非法：{case['bucket']!r}（允许 {list(JSON_BUCKETS)}）")
    if case["label"] not in JSON_LABELS:
        problems.append(f"label 非法：{case['label']!r}（允许 {list(JSON_LABELS)}）")
    if not isinstance(case["raw"], str) or not case["raw"].strip():
        problems.append("raw 必须是非空字符串")
    problems.extend(_status_problems(case))
    if case_status(case) == "known_failure":
        # xfail 的 expected 记「期望正确答案」，与 label 的旧约束冲突，故跳过交叉校验
        return problems
    label = case.get("label")
    if label in ("direct", "repaired") and case.get("expected") is None:
        problems.append(f"label={label} 时 expected 不能为空（否则内容一致性无从判定）")
    if label == "failed" and case.get("expected") is not None:
        problems.append("label=failed 时 expected 应为 null（期望行为是正确拒绝）")
    if not isinstance(case["note"], str) or not case["note"].strip():
        problems.append("note 必须是非空字符串（说明该样本为什么进集）")
    return problems


def validate_compliance_case(case: Any, existing_ids: set[str]) -> list[str]:
    problems = _missing(case, ("id", "group", "text", "expect_hit", "note"))
    if problems:
        return problems
    case_id = case["id"]
    if not isinstance(case_id, str) or not case_id.strip():
        problems.append("id 必须是非空字符串")
    elif case_id in existing_ids:
        problems.append(f"id 重复：{case_id}")
    if not isinstance(case["group"], str) or not case["group"].startswith(("pos_", "neg_")):
        problems.append(f"group 应以 pos_ / neg_ 开头，实际 {case['group']!r}")
    if not isinstance(case["text"], str) or not case["text"].strip():
        problems.append("text 必须是非空字符串")
    if not isinstance(case["expect_hit"], bool):
        problems.append("expect_hit 必须是布尔值")
    if not isinstance(case["note"], str) or not case["note"].strip():
        problems.append("note 必须是非空字符串（说明该样本为什么进集）")
    problems.extend(_status_problems(case))
    return problems


def validate_normalize_case(case: Any, existing_ids: set[str]) -> list[str]:
    """normalize：``expected`` 是「应有的计数语义」，None 表示应被拒绝。"""
    problems = _missing(case, ("id", "category", "raw", "expected", "note"))
    if problems:
        return problems
    case_id = case["id"]
    if not isinstance(case_id, str) or not case_id.strip():
        problems.append("id 必须是非空字符串")
    elif case_id in existing_ids:
        problems.append(f"id 重复：{case_id}")
    if not isinstance(case["category"], str) or not case["category"].strip():
        problems.append("category 必须是非空字符串")
    if not isinstance(case["raw"], str) or not case["raw"].strip():
        problems.append("raw 必须是非空字符串")
    if case.get("expected") is not None and not isinstance(case["expected"], int):
        problems.append("expected 必须是整数或 null")
    if not isinstance(case["note"], str) or not case["note"].strip():
        problems.append("note 必须是非空字符串")
    problems.extend(_status_problems(case))
    return problems


VALIDATORS: dict[str, Callable[[Any, set[str]], list[str]]] = {
    "json_robustness": validate_json_case,
    "compliance": validate_compliance_case,
    "normalize": validate_normalize_case,
}

#: 每个测试集对应的既有评测脚本（门禁直接复用，不另写一套评分）
BENCH_SCRIPTS: dict[str, str] = {
    "json_robustness": "scripts/benchmark/bench_testset_json_robustness.py",
    "compliance": "scripts/benchmark/bench_testset_compliance.py",
    "normalize": "scripts/benchmark/bench_testset_normalize.py",
}

RESULT_FILES: dict[str, str] = {
    "json_robustness": "json_robustness.json",
    "compliance": "compliance.json",
    "normalize": "normalize.json",
}


def validate_case(name: str, case: Any, existing_ids: set[str]) -> list[str]:
    validator = VALIDATORS.get(name)
    if validator is None:
        return [f"未定义的测试集：{name}"]
    return validator(case, existing_ids)


# ---------------------------------------------------------------------------
# 备份 / 还原
# ---------------------------------------------------------------------------
def backup_testset(name: str, workdir: Path, *, tag: str = "") -> Path:
    """整目录复制到 ``<workdir>/backups/<name>/<version>-<ts>[-tag]``。"""
    manifest = load_manifest(name)
    stamp = datetime.now(_TZ8).strftime("%Y%m%d-%H%M%S-%f")
    suffix = f"-{tag}" if tag else ""
    target = ensure_dir(workdir / "backups" / name / f"v{manifest.get('version')}-{stamp}{suffix}")
    for item in sorted(testset_dir(name).iterdir()):
        if item.is_file():
            shutil.copy2(item, target / item.name)
    return target


def restore_backup(name: str, backup_dir: Path) -> dict:
    """把备份文件按字节写回夹具目录，并核验指纹回到备份时的状态。"""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        raise RuntimeError(f"备份目录不存在：{backup_dir}")
    restored: list[str] = []
    for item in sorted(backup_dir.iterdir()):
        if not item.is_file():
            continue
        shutil.copy2(item, testset_dir(name) / item.name)
        restored.append(item.name)
    info = verify_frozen(name)
    return {"restored_files": restored, "verified": info}


# ---------------------------------------------------------------------------
# 子进程（跑既有评测脚本）
# ---------------------------------------------------------------------------
def _clean_env() -> dict[str, str]:
    """剥掉沙箱注入的代理变量（离线脚本本不需要，但避免任何隐式网络尝试）。"""
    drop = {
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "NO_PROXY", "no_proxy",
    }
    return {key: value for key, value in os.environ.items() if key not in drop}


def run_bench(name: str, *, timeout: int = 900) -> dict:
    """跑该测试集既有评测脚本，返回 ``{rc, stdout, result}``（result 已解析）。"""
    script = BENCH_SCRIPTS[name]
    proc = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=str(ROOT),
        env=_clean_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    result_path = RESULTS_DIR / RESULT_FILES[name]
    payload = read_json(result_path) if result_path.exists() else None
    return {
        "rc": proc.returncode,
        "stdout": (proc.stdout or "") + (proc.stderr or ""),
        "result": payload,
        "result_path": str(result_path),
    }


def snapshot_records(result: dict, statuses: dict[str, str] | None = None) -> dict:
    """把一次评测结果压成可比较的快照：逐 case 判定 + 汇总 +（v2）用例状态。"""
    detail = (result or {}).get("detail") or {}
    records = detail.get("records") or []
    by_id: dict[str, bool] = {}
    for record in records:
        case_id = record.get("id")
        if case_id is None:
            continue
        by_id[str(case_id)] = bool(record.get("correct"))
    summary = (result or {}).get("summary") or {}
    return {
        "n": summary.get("n", len(by_id)),
        "accuracy": summary.get("accuracy", summary.get("overall_accuracy")),
        "by_id": by_id,
        "records_total": len(records),
        "fixture_version": detail.get("fixture_version"),
        "status_by_id": statuses or {},
    }


__all__ = [
    "ROOT", "TESTSETS_DIR", "RESULTS_DIR", "JSON_BUCKETS", "JSON_LABELS",
    "CASE_STATUSES", "XFAIL_FIELDS", "CANDIDATE_STATES", "CANDIDATE_TRANSITIONS",
    "SIGNAL_TARGETS", "SIGNAL_PRIORITY", "BENCH_SCRIPTS", "RESULT_FILES", "VALIDATORS",
    "EXIT_OK", "EXIT_FAIL", "EXIT_GATED", "EXIT_ENV",
    "now_iso", "default_workdir", "add_backend_to_path", "ensure_dir",
    "read_json", "write_json", "sha256_of", "file_sha256", "bump_version",
    "testset_dir", "load_manifest", "load_cases", "verify_frozen", "recompute_stats",
    "case_status", "case_xfail", "case_statuses", "status_counts",
    "validate_case", "backup_testset", "restore_backup",
    "run_bench", "snapshot_records",
]
