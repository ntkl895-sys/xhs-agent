"""第 17 步 · 周报 Agent **真实 DeepSeek** 检查（需用户填 key 后手动运行）。

本脚本会**真的**向 DeepSeek 发起请求并**消耗 token**，因此**不会**被任何自动流程调用。

前置条件（backend/.env）：

.. code-block:: ini

    LLM_API_KEY=<你的 DeepSeek key>
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=<DeepSeek 控制台当前模型名>            # 以控制台为准
    AGENT_LLM_ENABLED=true                          # 必须显式打开，否则本脚本拒绝运行

它验证**只能靠真实调用才能确认**的事：

1. 周报链路是否真的打通：``generate_weekly_report`` 直连真实 ChatOpenAI
   （temperature≈0.2 + JSON 输出）能返回**可解析的 JSON**，图跑完 ``persist_run``
   且 ``agent_runs.outputs`` 回填 ``report``（**周报不写任何业务表**）；
2. 连续多轮 ``parse_attempts`` 是否**恒为 1**（模型稳定输出合法 JSON，不回炉）；
3. 报告结构是否稳定（summary / 亮点 / 问题 / 数据依据 / 下周建议 ≥1 / 数据缺口 /
   置信度），且本地缺口被完整并入 ``data_gaps``；
4. ``data_evidence`` 是否**引用给定周期事实中的数字**（本地重算 facts 做宽松比对），
   全文不含「行业平均 / 爆款门槛」等**外部数字话术**（不编造、零外部基准）；
5. 真实 token 用量（**按 token 记，不内置价格表**）；
6. 审计回填：``agent_runs`` 落 ``success`` + ``outputs``（report / 周期 /
   prompt_version / persisted）+ ``llm_raw``（已脱敏，不含密钥前缀）。

**不改动你的数据**：周报本身不写业务表；跑完按运行前后差集删除本次
``agent_runs`` 周报行与 ``langgraph`` 检查点线程；如需在页面上留存本次周报
（``/dashboard`` 周报 Tab 可查看），显式加 ``--keep-run``。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_weekly_report.py
    backend\\.venv\\Scripts\\python.exe scripts\\check_weekly_report.py --rounds 1
    backend\\.venv\\Scripts\\python.exe scripts\\check_weekly_report.py --days 14
    backend\\.venv\\Scripts\\python.exe scripts\\check_weekly_report.py --period-start 2026-10-01 --period-end 2026-10-07
    backend\\.venv\\Scripts\\python.exe scripts\\check_weekly_report.py --keep-run

退出码：0 = 全部通过；1 = 有断言失败 / 运行中断；2 = 未配置
（未填 key / 开关关闭 / 模型占位），**未发任何请求**。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---- 入口先摘掉沙箱注入的代理变量（本机全部真实请求进程的共同约定）----
for _key in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
):
    os.environ.pop(_key, None)

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.agent.weekly.contracts import build_period_facts  # noqa: E402
from app.agent.weekly.service import generate_weekly_report  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

#: 默认连续运行轮数（多轮验证稳定性）
ROUNDS = 3

#: 报告固定键（结构稳定性断言）
REPORT_KEYS = {
    "period_start",
    "period_end",
    "summary",
    "highlights",
    "issues",
    "data_evidence",
    "next_week_suggestions",
    "data_gaps",
    "confidence",
}

#: 外部数字「杜撰话术」黑名单（本期不允许出现任何外部口径；与第 16 步同清单）
EXTERNAL_PHRASES = (
    "行业平均",
    "行业均值",
    "行业基准",
    "爆款门槛",
    "爆款阈值",
    "通常涨粉",
    "同类账号平均",
)

#: 数字 token（整数或小数）
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            suffix = f"  ->  {detail}" if detail else ""
            print(f"[FAIL] {label}{suffix}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_weekly_report.py",
        description=(
            "第 17 步 · 周报真实 LLM 检查：会向 DeepSeek 发起真实请求并消耗 token；"
            "未配置时安全拒绝（退出码 2，零请求）。"
        ),
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=ROUNDS,
        help=f"连续运行轮数（默认 {ROUNDS}；--rounds 1 可用于最小成本验证）",
    )
    period = parser.add_mutually_exclusive_group()
    period.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"近 N 天（默认不传 = 配置默认 {settings.WEEKLY_DEFAULT_DAYS} 天；与显式起止互斥）",
    )
    period.add_argument(
        "--period-start",
        default=None,
        help="周期起点（YYYY-MM-DD 或 ISO 8601；配合 --period-end 或单用）",
    )
    parser.add_argument(
        "--period-end",
        default=None,
        help="周期终点（YYYY-MM-DD 或 ISO 8601；缺省 = 当前时刻）",
    )
    parser.add_argument(
        "--keep-run",
        action="store_true",
        help="保留本次生成的 agent_runs 周报行（默认删除，保证脚本可重复运行）",
    )
    return parser.parse_args(argv)


def _refuse_if_not_configured() -> int | None:
    """LLM 未配置（开关 / key / 模型占位）→ 打印指引并返回 2；**零请求**。"""
    readiness = llm_factory.describe_llm_readiness()
    if readiness["ready"]:
        return None
    print("=" * 78)
    print("⚠️  未检测到可用的真实 LLM 配置，**未发起任何请求**。")
    print("=" * 78)
    print(f"  AGENT_LLM_ENABLED = {readiness['llm_enabled']}")
    print(f"  LLM_API_KEY 已填写 = {readiness['has_api_key']}")
    print(
        f"  LLM_MODEL = {readiness['model']!r}"
        f"{'  (仍是占位符)' if readiness['model_is_placeholder'] else ''}"
    )
    print(f"  LLM_BASE_URL = {readiness['base_url']}")
    print()
    print("请在 backend/.env 中补全：")
    print("  LLM_API_KEY=<你的 DeepSeek key>")
    print("  LLM_MODEL=<DeepSeek 控制台当前模型名（以控制台为准）>")
    print("  AGENT_LLM_ENABLED=true")
    print()
    print("（真实密钥只允许放在本地 .env，切勿提交到仓库。）")
    return 2


# ---------------------------------------------------------------------------
# 低层查询 / 清理
# ---------------------------------------------------------------------------
def _current_threads() -> set[str]:
    with SessionLocal() as db:
        try:
            rows = db.execute(
                text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")
            ).all()
        except Exception:  # noqa: BLE001 —— 框架表可能尚未建
            return set()
    return {str(row[0]) for row in rows}


def _current_run_ids() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id FROM agent_runs")).all()
    return {str(row[0]) for row in rows}


def _fetch_runs(run_ids: list[str]) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id, status, run_type, trigger, llm_model, llm_base_url, "
                    "prompt_tokens, completion_tokens, total_tokens, json_parse_attempts, "
                    "outputs, llm_raw FROM agent_runs WHERE id = ANY(:ids)"
                ),
                {"ids": run_ids},
            )
            .mappings()
            .all()
        ]


def _cleanup(new_runs: set[str], new_threads: set[str], *, keep_run: bool) -> tuple[int, int]:
    """按差集删除本次运行的检查点线程；agent_runs 按 ``--keep-run`` 决定。"""
    deleted_runs = deleted_threads = 0
    with engine.begin() as conn:
        if new_runs and not keep_run:
            deleted_runs = conn.execute(
                text("DELETE FROM agent_runs WHERE id = ANY(:ids)"),
                {"ids": list(new_runs)},
            ).rowcount
        if new_threads:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                deleted_threads += conn.execute(
                    text(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(:tids)"),
                    {"tids": list(new_threads)},
                ).rowcount
    return deleted_runs, deleted_threads


# ---------------------------------------------------------------------------
# 「数据依据里的数字必须来自给定事实」的宽松比对
# ---------------------------------------------------------------------------
def _collect_numbers(value: Any, out: set[str]) -> None:
    """递归收集 facts 里的全部数字 → 多形态字符串集合（含百分数形态）。

    字符串里的数字 token 也一并收集（如周期 ISO 串中的 ``2026`` / ``10`` /
    ``06``，保证模型引用日期时不被误判为「凭空数字」）。
    """
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        number = float(value)
        if number == int(number):
            out.add(str(int(number)))
        else:
            out.add(("%.6f" % number).rstrip("0").rstrip("."))
            out.add(("%.4f" % number).rstrip("0").rstrip("."))
        percent = number * 100
        out.add(("%.4f" % percent).rstrip("0").rstrip("."))
        out.add(("%.2f" % percent).rstrip("0").rstrip("."))
        out.add(("%.1f" % percent).rstrip("0").rstrip("."))
        return
    if isinstance(value, str):
        for token in _NUMBER_RE.findall(value):
            out.add(token.lstrip("0") or "0")
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_numbers(item, out)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_numbers(item, out)
        return


def _token_in_facts(token: str, forms: set[str]) -> bool:
    """token 与任一 facts 数字形态「互相包含」即视为命中（宽容）。"""
    if token in forms:
        return True
    return any(form and (form in token or token in form) for form in forms)


def _check_data_evidence(c: Checker, rows: list[dict[str, Any]]) -> None:
    """逐轮：data_evidence 每条至少引用 1 个事实数字；大数字不得凭空出现。"""
    for index, row in enumerate(rows, start=1):
        report = row.get("_report") or {}
        evidence = [str(item) for item in (report.get("data_evidence") or [])]
        facts = row.get("_facts") or {}
        forms: set[str] = set()
        _collect_numbers(facts, forms)
        forms.discard("")
        if not evidence:
            c.check(
                True,
                f"第 {index} 轮 data_evidence 为空（事实不足时不硬凑，属合法结果）",
            )
            continue
        unmatched_big: list[str] = []
        cited = 0
        for text_item in evidence:
            tokens = _NUMBER_RE.findall(text_item)
            hits = [token for token in tokens if _token_in_facts(token, forms)]
            if hits:
                cited += 1
            for token in tokens:
                if token in hits:
                    continue
                is_big = ("." in token and float(token) >= 1) or (
                    "." not in token and int(token) >= 100
                )
                if is_big:
                    unmatched_big.append(token)
        c.check(
            cited == len(evidence),
            f"第 {index} 轮 data_evidence 每条都引用了给定事实中的数字（{cited}/{len(evidence)}）",
            f"{evidence}",
        )
        c.check(
            not unmatched_big,
            f"第 {index} 轮 data_evidence 无「凭空大数字」（≥100 或小数必须来自周期事实）",
            f"未命中={unmatched_big}",
        )


def _print_round(index: int, row: dict[str, Any]) -> None:
    report = row.get("_report") or {}
    print(
        f"  第 {index} 轮：run={str(row.get('id'))[:8]}… "
        f"周期=[{report.get('period_start', '?')} ~ {report.get('period_end', '?')}] "
        f"parse_attempts={row.get('json_parse_attempts')} "
        f"tokens={row.get('total_tokens')}"
    )
    print(f"    【概述】{report.get('summary', '')}")
    for label, key in (
        ("亮点", "highlights"),
        ("问题", "issues"),
        ("数据依据", "data_evidence"),
        ("下周建议", "next_week_suggestions"),
        ("数据缺口", "data_gaps"),
    ):
        values = report.get(key) or []
        print(f"    【{label}】（{len(values)} 条）")
        for text_item in values:
            print(f"      - {text_item}")
    print(f"    【置信度】{report.get('confidence')}")


def _assert_rounds(c: Checker, rows: list[dict[str, Any]]) -> None:
    """对成功返回的轮次做结构 / 稳定性 / 红线 / 审计断言（数据已在清理前抓取）。"""
    reports = [row.get("_report") or {} for row in rows]

    c.check(
        all(set(report) >= REPORT_KEYS for report in reports),
        "每轮报告九键齐全（周期 / 概述 / 亮点 / 问题 / 依据 / 建议 / 缺口 / 置信度）",
        f"{[sorted(report) for report in reports]}",
    )
    c.check(
        all(str(report.get("summary") or "").strip() for report in reports),
        "每轮 summary 非空",
    )
    c.check(
        all(len(report.get("next_week_suggestions") or []) >= 1 for report in reports),
        "每轮 next_week_suggestions 非空（硬伤字段，空会被回炉）",
        f"{[len(report.get('next_week_suggestions') or []) for report in reports]}",
    )
    c.check(
        all(
            report.get("confidence") is None
            or (
                isinstance(report.get("confidence"), (int, float))
                and 0 <= float(report["confidence"]) <= 1
            )
            for report in reports
        ),
        "每轮 confidence 为 0~1 数字或 null",
        f"{[report.get('confidence') for report in reports]}",
    )

    # ---- 本地缺口必须被完整并入 data_gaps（merge_data_gaps 本地优先）----
    merged_ok = True
    for row in rows:
        local_gaps = [str(item) for item in ((row.get("_facts") or {}).get("data_gaps") or [])]
        report_gaps = [str(item) for item in ((row.get("_report") or {}).get("data_gaps") or [])]
        if any(gap not in report_gaps for gap in local_gaps):
            merged_ok = False
    c.check(
        merged_ok,
        "每轮本地数据缺口被完整并入 report.data_gaps（不丢、不藏）",
    )

    # ---- JSON 连续稳定性 ----
    attempts = [row.get("json_parse_attempts") for row in rows]
    c.check(
        bool(attempts) and all(value == 1 for value in attempts),
        f"连续 {len(rows)} 轮 parse_attempts 恒为 1（JSON 输出稳定，未走回炉）",
        f"{attempts}",
    )

    # ---- 无外部数字话术 ----
    text_blob = json.dumps(
        [
            {
                "summary": report.get("summary"),
                "highlights": report.get("highlights"),
                "issues": report.get("issues"),
                "data_evidence": report.get("data_evidence"),
                "next_week_suggestions": report.get("next_week_suggestions"),
                "data_gaps": report.get("data_gaps"),
            }
            for report in reports
        ],
        ensure_ascii=False,
    )
    hits = [phrase for phrase in EXTERNAL_PHRASES if phrase in text_blob]
    c.check(
        not hits,
        "报告全文不含「行业平均 / 爆款门槛」等外部数字话术（不编造、零外部基准）",
        f"{hits}",
    )

    # ---- 数据依据数字来自给定事实 ----
    _check_data_evidence(c, rows)

    # ---- 审计回填与脱敏 ----
    c.check(
        bool(rows) and all(row["status"] == "success" for row in rows),
        "每轮 run 状态均为 success",
        f"{[row['status'] for row in rows]}",
    )
    c.check(
        bool(rows)
        and all(
            row["run_type"] == "weekly_report" and row["trigger"] == "manual"
            for row in rows
        ),
        "每轮 run_type=weekly_report、trigger=manual",
        f"{[(row['run_type'], row['trigger']) for row in rows]}",
    )
    c.check(
        bool(rows)
        and all(
            (row["outputs"] or {}).get("persisted") is True
            and isinstance((row["outputs"] or {}).get("report"), dict)
            and (row["outputs"] or {}).get("prompt_version") == settings.WEEKLY_PROMPT_VERSION
            for row in rows
        ),
        "outputs 已回填 {report, persisted:true, prompt_version}（周报本体在 run 里）",
    )
    c.check(
        bool(rows) and all(row["llm_model"] and row["llm_base_url"] for row in rows),
        "llm_model / llm_base_url 已记录（绝不记录 key）",
    )
    c.check(
        bool(rows)
        and all(
            str(row["llm_raw"] or "").strip() and "sk-" not in str(row["llm_raw"])
            for row in rows
        ),
        "llm_raw 已归档且不含密钥前缀（脱敏 + 截断）",
    )
    total_tokens = sum((row["total_tokens"] or 0) for row in rows)
    c.check(total_tokens > 0, "agent_runs 记录到真实 token 用量", f"total={total_tokens}")

    print("-" * 78)
    for index, row in enumerate(rows, start=1):
        print(
            f"  token 第 {index} 轮：prompt={row['prompt_tokens']} "
            f"completion={row['completion_tokens']} total={row['total_tokens']}"
        )
    print(
        f"[INFO] 本次共消耗 total_tokens = {total_tokens}"
        f"（成本 = token × 你的实际单价，本仓库不内置价格表）"
    )
    # ---- 质量人工确认提示（机器断言不了周报结论质量） ----
    for index, row in enumerate(rows, start=1):
        print("-" * 78)
        print(f"  第 {index} 轮周报全文（**请人工过目一眼**）：")
        _print_round(index, row)


def main() -> int:
    args = _parse_args()
    if args.days is not None and (args.period_start or args.period_end):
        print(
            "[参数错误] --days 与 --period-start / --period-end 不能同时指定"
            "（周期参数互斥）。",
            file=sys.stderr,
        )
        return 1
    refusal = _refuse_if_not_configured()
    if refusal is not None:
        return refusal

    rounds_to_run = max(1, args.rounds)
    period_kwargs: dict[str, Any]
    if args.period_start or args.period_end:
        period_kwargs = {
            "period_start": args.period_start,
            "period_end": args.period_end,
        }
    else:
        period_kwargs = {"days": args.days}

    init_checkpointer()
    before_threads = _current_threads()
    before_runs = _current_run_ids()

    c = Checker()
    print("周报 真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print(
        f"model={llm_factory.describe_llm_readiness()['model']} "
        f"| base_url={settings.LLM_BASE_URL} | 温度≈0.2"
    )
    print(f"prompt_version={settings.WEEKLY_PROMPT_VERSION}")
    print(
        "周期参数："
        + (
            f"显式 [start={args.period_start or '默认'} ~ end={args.period_end or '当前时刻'}]"
            if args.period_start or args.period_end
            else f"近 {args.days or settings.WEEKLY_DEFAULT_DAYS} 天（含今天）"
        )
    )
    print(f"将连续运行 {rounds_to_run} 轮；周报 run 保留：{'是' if args.keep_run else '否（默认）'}")
    print("-" * 78)

    run_ids: list[str] = []
    fetched_rows: list[dict[str, Any]] = []
    fatal: str | None = None
    new_runs: set[str] = set()
    new_threads: set[str] = set()
    deleted_runs = deleted_threads = 0

    try:
        for index in range(1, rounds_to_run + 1):
            try:
                summary = generate_weekly_report(
                    trigger="manual",
                    **period_kwargs,
                )
            except (AppError, AgentConfigError) as exc:
                fatal = f"{type(exc).__name__}: {exc}"
                print(f"  [中断] 第 {index} 轮周报失败：{fatal}")
                break
            run_ids.append(str(summary.get("run_id")))
            report = summary.get("report") or {}
            print(
                f"  第 {index} 轮：run={str(summary.get('run_id'))[:8]}… "
                f"周期=[{summary.get('period_start')} ~ {summary.get('period_end')}] "
                f"parse_attempts={summary.get('parse_attempts')} "
                f"建议={len(report.get('next_week_suggestions') or [])} 条 "
                f"缺口={len(summary.get('data_gaps') or [])} 条"
            )

        # 审计与事实底稿必须在清理**之前**抓取（finally 会删除本次数据，之后不可再查）
        raw_rows = _fetch_runs([rid for rid in run_ids if rid])
        for row in raw_rows:
            outputs = row.get("outputs") or {}
            report = outputs.get("report") if isinstance(outputs, dict) else None
            row["_report"] = report if isinstance(report, dict) else {}
            # 用 run 自己的周期重算事实（数字比对基准与模型看到的一致）
            facts: dict[str, Any] = {}
            start_iso = outputs.get("period_start") if isinstance(outputs, dict) else None
            end_iso = outputs.get("period_end") if isinstance(outputs, dict) else None
            if start_iso and end_iso:
                try:
                    with SessionLocal() as db:
                        facts = build_period_facts(
                            db,
                            period_start=datetime.fromisoformat(str(start_iso)),
                            period_end=datetime.fromisoformat(str(end_iso)),
                        )
                except Exception as exc:  # noqa: BLE001 —— 比对基准失败不致命
                    print(f"  [WARN] 重算周期事实失败（数字比对将宽松处理）：{exc}")
            row["_facts"] = facts
        fetched_rows = raw_rows
    finally:
        new_runs = _current_run_ids() - before_runs
        new_threads = _current_threads() - before_threads
        deleted_runs, deleted_threads = _cleanup(
            new_runs, new_threads, keep_run=args.keep_run
        )
        close_checkpointer()

    print("-" * 78)
    print(
        f"[INFO] 已清理：agent_runs={deleted_runs} 行 / 检查点行={deleted_threads}"
        f"（线程 {len(new_threads)} 个）"
        + ("（本次选择保留周报 run）" if args.keep_run else "")
    )

    if fatal is None:
        c.check(
            len(fetched_rows) == rounds_to_run,
            f"连续 {rounds_to_run} 轮周报全部成功返回",
            f"成功 {len(fetched_rows)} / {rounds_to_run}",
        )
    else:
        c.check(False, f"连续 {rounds_to_run} 轮周报全部成功（中断）", fatal)

    if fetched_rows:
        _assert_rounds(c, fetched_rows)

    # ---- 清理复查（差集口径）----
    c.check(
        (_current_threads() - before_threads) == set(),
        "本次运行的检查点线程已按差集物理清理",
    )
    if args.keep_run:
        remaining = _current_run_ids() & set(run_ids)
        c.check(
            len(remaining) == len(run_ids),
            "本次周报 run 已按约定保留（可在 /dashboard 周报 Tab 查看）",
            f"{len(remaining)}/{len(run_ids)}",
        )
    else:
        c.check(
            (_current_run_ids() - before_runs) == set(),
            "本次运行的 agent_runs 已按差集物理清理",
        )

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        if fatal:
            print()
            print("[提示] 周报在运行中失败，常见原因与处理：")
            print("  · 「周报未就绪」→ 检查 AGENT_LLM_ENABLED / LLM_API_KEY / LLM_MODEL；")
            print("  · 「周报生成失败：连续 N 轮…」→ 换更稳定的模型或稍后重试；")
            print("  · 网络抖动 / 429 → 稍后重试。")
        return 1
    print("全部 PASS ✔（真实 LLM）")
    print("⚠️ 机器断言不了「周报结论质量」：请对照上方逐轮全文人工过目。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
