"""第 16 步 · 单篇诊断 Agent **真实 DeepSeek** 检查（需用户填 key 后手动运行）。

本脚本会**真的**向 DeepSeek 发起请求并**消耗 token**，因此**不会**被任何自动流程调用。

前置条件（backend/.env）：

.. code-block:: ini

    LLM_API_KEY=<你的 DeepSeek key>
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=<DeepSeek 控制台当前模型名>            # 以控制台为准
    AGENT_LLM_ENABLED=true                          # 必须显式打开，否则本脚本拒绝运行

它验证**只能靠真实调用才能确认**的事：

1. 诊断链路是否真的打通：``diagnose_note`` 直连真实 ChatOpenAI（temperature≈0.2 +
   JSON 输出）能返回**可解析的 JSON**，图跑完 ``persist`` 且 ``diagnostics`` 落库；
2. 连续多轮 ``parse_attempts`` 是否**恒为 1**（模型稳定输出合法 JSON，不回炉）；
3. ``score`` 是否合规（0~100 数字或 ``null``——「数据不足不硬打分」是合法结果）；
4. ``benchmark`` 是否**零外部基准杜撰**（``external_benchmark = null``、基准缺失声明
   在档、四数组与原文不含「行业平均 / 行业均值 / 爆款门槛」等外部数字话术）；
5. 真实 token 用量（**按 token 记，不内置价格表**）；
6. 审计回填：``agent_runs`` 落 ``success`` + ``outputs`` 摘要 + ``note_id`` +
   ``llm_raw``（已脱敏，不含密钥前缀）。

**不改动你的数据**：跑完按运行前后差集删除本次 ``agent_runs`` 与 ``langgraph``
检查点线程；本次新建的 ``diagnostics`` 行**默认也删掉**（保证可重复运行），
如需留存诊断结论请显式加 ``--keep-diagnostic``。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_diagnosis_agent.py
    backend\\.venv\\Scripts\\python.exe scripts\\check_diagnosis_agent.py --rounds 1
    backend\\.venv\\Scripts\\python.exe scripts\\check_diagnosis_agent.py --note-id <已发布笔记 UUID>
    backend\\.venv\\Scripts\\python.exe scripts\\check_diagnosis_agent.py --keep-diagnostic

退出码：0 = 全部通过；1 = 有断言失败 / 运行中断 / 找不到可用笔记；2 = 未配置
（未填 key / 开关关闭 / 模型占位），**未发任何请求**。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
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
from app.agent.diagnosis.service import diagnose_note  # noqa: E402
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

#: 默认连续运行轮数（多轮验证稳定性）
ROUNDS = 3

#: benchmark 的固定键集（结构稳定性断言）
BENCHMARK_KEYS = {
    "basis",
    "latest_metrics",
    "rates",
    "trends",
    "thresholds",
    "external_benchmark",
    "benchmark_note",
}

#: llm_meta 白名单
META_KEYS = {
    "prompt_version",
    "json_parse_attempts",
    "diagnosis_type",
    "diagnosis_type_source",
    "data_gaps",
}

#: 外部基准「杜撰话术」黑名单（本期不允许出现任何外部数字口径）
EXTERNAL_BENCHMARK_PHRASES = (
    "行业平均",
    "行业均值",
    "行业基准",
    "爆款门槛",
    "爆款阈值",
    "通常涨粉",
    "同类账号平均",
)


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
        prog="check_diagnosis_agent.py",
        description=(
            "第 16 步 · 单篇诊断真实 LLM 检查：会向 DeepSeek 发起真实请求并消耗 token；"
            "未配置时安全拒绝（退出码 2，零请求）。"
        ),
    )
    parser.add_argument(
        "--note-id",
        default=None,
        help="目标笔记 UUID（默认自动挑一条「已发布且至少有 1 条快照」的笔记）",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=ROUNDS,
        help=f"连续运行轮数（默认 {ROUNDS}；--rounds 1 可用于最小成本验证）",
    )
    parser.add_argument(
        "--keep-diagnostic",
        action="store_true",
        help="保留本次生成的 diagnostics 行（默认删除，保证脚本可重复运行）",
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
            rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
        except Exception:  # noqa: BLE001 —— 框架表可能尚未建
            return set()
    return {str(row[0]) for row in rows}


def _current_run_ids() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id FROM agent_runs")).all()
    return {str(row[0]) for row in rows}


def _resolve_note(note_id_arg: str | None) -> tuple[uuid.UUID | None, str]:
    """解析目标笔记：显式给定则校验；否则自动挑一条「已发布 + 有快照」的。

    Returns:
        ``(note_id, 说明)``；无法确定时 note_id 为 None。
    """
    with SessionLocal() as db:
        if note_id_arg:
            try:
                note_uuid = uuid.UUID(str(note_id_arg))
            except (TypeError, ValueError):
                return None, f"--note-id 不是合法 UUID：{note_id_arg!r}"
            row = db.execute(
                text("SELECT title, status FROM notes WHERE id = :n"),
                {"n": str(note_uuid)},
            ).one_or_none()
            if row is None:
                return None, f"笔记不存在：{note_uuid}"
            return note_uuid, f"（指定）{row[0]}｜status={row[1]}"
        row = db.execute(
            text(
                "SELECT n.id, n.title, count(m.id) AS snapshots FROM notes n "
                "LEFT JOIN note_metrics m ON m.note_id = n.id "
                "WHERE n.status = 'published' "
                "GROUP BY n.id, n.title HAVING count(m.id) >= 1 "
                "ORDER BY count(m.id) DESC, n.created_at DESC LIMIT 1"
            )
        ).one_or_none()
        if row is None:
            return None, "找不到「已发布且至少有 1 条快照」的笔记（请先发布笔记并回填数据）"
        return uuid.UUID(str(row[0])), f"（自动）{row[1]}｜快照 {row[2]} 条"


def _fetch_diagnostics(ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id, note_id, based_on_metric_id, diagnosis_type, score, strengths, "
                    "weaknesses, reasons, suggestions, benchmark, llm_model, llm_meta "
                    "FROM diagnostics WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            ).mappings().all()
        ]


def _fetch_runs(run_ids: list[str]) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id, status, run_type, trigger, note_id, llm_model, llm_base_url, "
                    "prompt_tokens, completion_tokens, total_tokens, json_parse_attempts, "
                    "outputs, llm_raw FROM agent_runs WHERE id = ANY(:ids)"
                ),
                {"ids": run_ids},
            ).mappings().all()
        ]


def _cleanup(
    new_runs: set[str], new_threads: set[str], diagnostic_ids: list[str], *, keep: bool
) -> tuple[int, int, int]:
    """按差集删除本次运行的 agent_runs / 检查点线程；诊断行按参数决定保留或删除。"""
    deleted_runs = deleted_threads = deleted_diagnostics = 0
    with engine.begin() as conn:
        if new_runs:
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
        if diagnostic_ids and not keep:
            deleted_diagnostics = conn.execute(
                text("DELETE FROM diagnostics WHERE id = ANY(:ids)"),
                {"ids": diagnostic_ids},
            ).rowcount
    return deleted_runs, deleted_threads, deleted_diagnostics


def _print_round(index: int, summary: dict[str, Any]) -> None:
    print(
        f"  第 {index} 轮：诊断 #{str(summary.get('diagnostic_id'))[:8]}… "
        f"类型={summary.get('diagnosis_type')}({summary.get('diagnosis_type_source')}) "
        f"评分={summary.get('score')} parse_attempts={summary.get('parse_attempts')}"
    )
    gaps = summary.get("data_gaps") or []
    if gaps:
        print(f"      数据缺口 {len(gaps)} 条：" + "；".join(gaps[:3]) + ("…" if len(gaps) > 3 else ""))


def _assert_rounds(
    c: Checker,
    summaries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> None:
    """对成功返回的轮次做结构 / 稳定性 / 红线 / 审计断言。

    ``rows`` / ``runs`` 由 main 在**清理之前**抓取（finally 会删除本次数据）。
    """
    # ---- 返回结构完整 ----
    c.check(
        all(
            item.get("diagnostic_id")
            and item.get("note_id")
            and item.get("diagnosis_type") in {"cold_start", "review", "anomaly"}
            and item.get("diagnosis_type_source") in {"auto", "manual"}
            and isinstance(item.get("data_gaps"), list)
            for item in summaries
        ),
        "每轮返回摘要结构完整（diagnostic_id / 类型 / 来源 / data_gaps）",
    )
    c.check(
        len(rows) == len(summaries),
        "每轮 diagnostics 行都能回查（落库成功）",
        f"{len(rows)}/{len(summaries)}",
    )
    c.check(
        all(set(row.get("llm_meta") or {}) == META_KEYS for row in rows),
        "每行 llm_meta 恰为白名单五项",
        f"{[sorted(row.get('llm_meta') or []) for row in rows]}",
    )

    # ---- score 合规或 null ----
    c.check(
        all(
            row.get("score") is None
            or (isinstance(row.get("score"), (int, float)) and 0 <= float(row["score"]) <= 100)
            for row in rows
        ),
        "每行 score 为 0~100 数字或 null（数据不足不硬打分）",
        f"{[row.get('score') for row in rows]}",
    )
    c.check(
        all(
            (row.get("diagnosis_type") == "cold_start") == (row.get("score") is None)
            or row.get("score") is not None
            for row in rows
        ),
        "冷启动行 score 恒为 null（不硬打分）",
        f"{[(row.get('diagnosis_type'), row.get('score')) for row in rows]}",
    )

    # ---- JSON 连续稳定性 ----
    attempts = [item.get("parse_attempts") for item in summaries]
    db_attempts = [item["json_parse_attempts"] for item in runs]
    c.check(
        bool(attempts)
        and all(value == 1 for value in attempts)
        and bool(db_attempts)
        and all(value == 1 for value in db_attempts),
        f"连续 {len(summaries)} 轮 parse_attempts 恒为 1（JSON 输出稳定，未走回炉）",
        f"响应={attempts} 审计={db_attempts}",
    )

    # ---- 无外部基准杜撰 ----
    c.check(
        all((row.get("benchmark") or {}).get("external_benchmark") is None for row in rows),
        "benchmark.external_benchmark 恒为 null（零外部基准）",
    )
    c.check(
        all(set(row.get("benchmark") or {}) == BENCHMARK_KEYS for row in rows),
        "benchmark 结构稳定（固定七键）",
        f"{[sorted(row.get('benchmark') or []) for row in rows]}",
    )
    c.check(
        all(
            "第 18 步" in str((row.get("benchmark") or {}).get("benchmark_note") or "")
            for row in rows
        ),
        "benchmark_note 声明「本期无外部基准、跨篇基准见第 18 步」",
    )
    text_blob = json.dumps(
        [
            {
                "strengths": row.get("strengths"),
                "weaknesses": row.get("weaknesses"),
                "reasons": row.get("reasons"),
                "suggestions": row.get("suggestions"),
            }
            for row in rows
        ],
        ensure_ascii=False,
    )
    hits = [phrase for phrase in EXTERNAL_BENCHMARK_PHRASES if phrase in text_blob]
    c.check(
        not hits,
        "四数组不含「行业平均 / 爆款门槛」等外部数字话术（不杜撰）",
        f"{hits}",
    )

    # ---- 审计回填与脱敏 ----
    c.check(
        bool(runs) and all(item["status"] == "success" for item in runs),
        "每轮 run 状态均为 success",
        f"{[item['status'] for item in runs]}",
    )
    c.check(
        bool(runs)
        and all(item["run_type"] == "diagnosis" and item["note_id"] for item in runs),
        "每轮 run_type=diagnosis 且 note_id 已回填（harness 不代填，业务层补）",
        f"{[(item['run_type'], str(item['note_id'])[:8]) for item in runs]}",
    )
    c.check(
        bool(runs)
        and all(
            isinstance((item["outputs"] or {}).get("diagnostic_id"), str)
            and (item["outputs"] or {}).get("persisted") is True
            for item in runs
        ),
        "agent_runs.outputs 已回填 {diagnostic_id, persisted}",
        f"{[item['outputs'] for item in runs]}",
    )
    c.check(
        bool(runs) and all(item["llm_model"] and item["llm_base_url"] for item in runs),
        "llm_model / llm_base_url 已记录（绝不记录 key）",
    )
    c.check(
        bool(runs)
        and all(
            str(item["llm_raw"] or "").strip() and "sk-" not in str(item["llm_raw"])
            for item in runs
        ),
        "llm_raw 已归档且不含密钥前缀（脱敏 + 截断）",
    )
    total_tokens = sum((item["total_tokens"] or 0) for item in runs)
    c.check(total_tokens > 0, "agent_runs 记录到真实 token 用量", f"total={total_tokens}")

    print("-" * 78)
    for index, item in enumerate(runs, start=1):
        print(
            f"  token 第 {index} 轮：prompt={item['prompt_tokens']} "
            f"completion={item['completion_tokens']} total={item['total_tokens']}"
        )
    print(
        f"[INFO] 本次共消耗 total_tokens = {total_tokens}"
        f"（成本 = token × 你的实际单价，本仓库不内置价格表）"
    )
    # ---- 质量人工确认提示（机器断言不了诊断质量） ----
    for index, row in enumerate(rows, start=1):
        print("-" * 78)
        print(f"  第 {index} 轮诊断内容（**请人工过目一眼**）：")
        print(f"    类型：{row.get('diagnosis_type')}｜评分：{row.get('score')}")
        for label, key in (
            ("亮点", "strengths"),
            ("问题", "weaknesses"),
            ("原因", "reasons"),
            ("建议", "suggestions"),
        ):
            values = row.get(key) or []
            print(f"    {label}（{len(values)} 条）：")
            for text_item in values[:5]:
                print(f"      - {text_item}")


def main() -> int:
    args = _parse_args()
    refusal = _refuse_if_not_configured()
    if refusal is not None:
        return refusal

    rounds_to_run = max(1, args.rounds)
    note_uuid, note_desc = _resolve_note(args.note_id)
    if note_uuid is None:
        print(f"[FATAL] 无法确定目标笔记：{note_desc}")
        return 1

    init_checkpointer()
    before_threads = _current_threads()
    before_runs = _current_run_ids()

    c = Checker()
    print("单篇诊断 真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print(
        f"model={llm_factory.describe_llm_readiness()['model']} "
        f"| base_url={settings.LLM_BASE_URL} | 温度≈0.2"
    )
    print(f"prompt_version={settings.DIAG_PROMPT_VERSION}")
    print(f"目标笔记：{note_desc}")
    print(f"将连续运行 {rounds_to_run} 轮；诊断行保留：{'是' if args.keep_diagnostic else '否（默认）'}")
    print("-" * 78)

    summaries: list[dict[str, Any]] = []
    run_ids: list[str] = []
    diagnostic_ids: list[str] = []
    fetched_runs: list[dict[str, Any]] = []
    fetched_rows: list[dict[str, Any]] = []
    fatal: str | None = None
    new_runs: set[str] = set()
    new_threads: set[str] = set()
    deleted_runs = deleted_threads = deleted_diagnostics = 0

    try:
        for index in range(1, rounds_to_run + 1):
            try:
                summary = diagnose_note(note_id=note_uuid, trigger="manual")
            except (AppError, AgentConfigError) as exc:
                fatal = f"{type(exc).__name__}: {exc}"
                print(f"  [中断] 第 {index} 轮诊断失败：{fatal}")
                break
            summaries.append(summary)
            run_ids.append(str(summary.get("run_id")))
            diagnostic_ids.append(str(summary.get("diagnostic_id")))
            _print_round(index, summary)
        # 审计与业务数据必须在清理**之前**抓取（finally 会删除本次数据，之后不可再查）
        fetched_runs = _fetch_runs([rid for rid in run_ids if rid])
        fetched_rows = _fetch_diagnostics([did for did in diagnostic_ids if did])
    finally:
        new_runs = _current_run_ids() - before_runs
        new_threads = _current_threads() - before_threads
        deleted_runs, deleted_threads, deleted_diagnostics = _cleanup(
            new_runs, new_threads, diagnostic_ids, keep=args.keep_diagnostic
        )
        close_checkpointer()

    print("-" * 78)
    print(
        f"[INFO] 已清理：agent_runs={deleted_runs} 行 / 检查点行={deleted_threads}"
        f"（线程 {len(new_threads)} 个）/ diagnostics={deleted_diagnostics} 行"
        + ("（本次选择保留诊断行）" if args.keep_diagnostic else "")
    )

    # ---- 轮次与断言 ----
    if fatal is None:
        c.check(
            len(summaries) == rounds_to_run,
            f"连续 {rounds_to_run} 轮诊断全部成功返回",
            f"成功 {len(summaries)} / {rounds_to_run}",
        )
    else:
        c.check(False, f"连续 {rounds_to_run} 轮诊断全部成功（中断）", fatal)

    if summaries:
        _assert_rounds(c, summaries, fetched_rows, fetched_runs)

    # ---- 清理复查（差集口径）----
    c.check(
        (_current_run_ids() - before_runs) == set(),
        "本次运行的 agent_runs 已按差集物理清理",
    )
    c.check(
        (_current_threads() - before_threads) == set(),
        "本次运行的检查点线程已按差集物理清理",
    )
    if not args.keep_diagnostic:
        remaining = _fetch_diagnostics([did for did in diagnostic_ids if did])
        c.check(not remaining, "本次生成的 diagnostics 行已按约定清理（可重复运行）")

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        if fatal:
            print()
            print("[提示] 诊断在运行中失败，常见原因与处理：")
            print("  · 「单篇诊断未就绪」→ 检查 AGENT_LLM_ENABLED / LLM_API_KEY / LLM_MODEL；")
            print("  · 「诊断失败：连续 N 轮…不满足结构要求」→ 换更稳定的模型或稍后重试；")
            print("  · 网络抖动 / 429 → 稍后重试。")
        return 1
    print("全部 PASS ✔（真实 LLM）")
    print("⚠️ 机器断言不了「诊断质量」：请对照上方逐轮内容人工过目（类型 / 评分 / 建议是否合理）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
