"""第 18 步 · 爆款规律提炼 Agent **真实 DeepSeek** 检查（需用户填 key / 装依赖后手动运行）。

本脚本会**真的**向 DeepSeek 发起请求并**消耗 token**，因此**不会**被任何自动流程调用；
且**必须先完成本地向量化依赖安装**（提炼落库要把规律真实向量化）。

双重前置（任一不满足 → 退出码 2，**零请求**）：

1. **LLM**（backend/.env）：

   .. code-block:: ini

       LLM_API_KEY=<你的 DeepSeek key>
       LLM_BASE_URL=https://api.deepseek.com
       LLM_MODEL=<DeepSeek 控制台当前模型名>            # 以控制台为准
       AGENT_LLM_ENABLED=true

2. **本地向量化**（第 18 步新增依赖）：

   .. code-block:: ini

       # 已 pip install -r backend/requirements.txt（langchain-huggingface /
       # sentence-transformers）；首次运行会下载 bge 模型（约 1.3GB）
       # 国内网络建议：set HF_ENDPOINT=https://hf-mirror.com

它验证**只能靠真实调用才能确认**的事：

1. 提炼链路是否真的打通：``generate_insights`` 直连真实 ChatOpenAI（温度≈0.2 +
   JSON 输出）→ 图跑完 ``embed_and_persist``（真实 bge 向量）→ 知识块落库；
2. 规律**结构合规**：每条都有真实来源（``meta.source_note_ids`` 必须 ⊆ 库内已发布
   笔记 id，模型编造的 id 已被本地校验过滤）、指标证据非空、置信度 0~1 或空；
3. ``parse_attempts`` 是否在合理范围（理想为 1；模型偶发需 1 次纠正重问仍属正常）；
4. **零外部数字**：规律本体 / 证据 / 场景**不得出现「行业平均 / 爆款门槛 / 大盘数据」
   等话术**（提示词已明文禁止；命中即红线 FAIL）；
5. 审计回填：``agent_runs`` 落 ``success``（或样本不足时 ``partial``）+ ``outputs``
   业务明细 + ``llm_raw``（已脱敏，不含密钥前缀）+ token 用量（**按 token 记，
   不内置价格表**）；
6. 样本不足硬门：库内已发布笔记 < ``INSIGHTS_MIN_PUBLISHED``（默认 3）时，
   **零模型调用零写入**、``insufficient_sample=True``、data_gaps 给出明确指引
   （用 ``--allow-insufficient`` 显式验证此路径）。

**不破坏你的数据**：跑完按 id 删除本次写入的 ``knowledge_snippets``；按运行前后
差集删除本次 ``agent_runs`` 与 ``langgraph`` 检查点线程。如需留存规律与运行记录，
显式加 ``--keep-run``（检查点线程仍按差集清理，避免框架表膨胀）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_insights_agent.py
    backend\\.venv\\Scripts\\python.exe scripts\\check_insights_agent.py --keep-run
    backend\\.venv\\Scripts\\python.exe scripts\\check_insights_agent.py --allow-insufficient

退出码：0 = 全部通过；1 = 有断言失败 / 运行中断；2 = 未就绪（LLM 未配置 /
向量化依赖未装 / 已发布样本不足且未给 ``--allow-insufficient``），**未发任何请求**。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
from app.agent.embedding.base import EmbeddingError  # noqa: E402
from app.agent.embedding.local import INSTALL_HINT, local_deps_installed  # noqa: E402
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.insights.service import generate_insights  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

#: 密钥形态（脱敏断言用；运行时拼接避免扫描误报）
SECRET_PATTERN = re.compile("s" + "k" + r"-[A-Za-z0-9_\-]{6,}")

#: 明文禁止出现的外部数字话术（红线：规律只来自本账号数据）
EXTERNAL_PHRASES = (
    "行业平均",
    "行业均值",
    "爆款门槛",
    "大盘数据",
    "平台官方算法",
    "行业基准",
    "全网平均",
    "大盘均值",
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
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 闸门（零请求）
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_insights_agent.py",
        description=(
            "第 18 步 · 爆款规律提炼真实 LLM 检查：会向 DeepSeek 发起真实请求并消耗 token；"
            "未配置 / 依赖未装 / 样本不足时安全拒绝（退出码 2，零请求）。"
        ),
    )
    parser.add_argument(
        "--keep-run",
        action="store_true",
        help="保留本次生成的知识块与 agent_runs 运行记录（默认删除，保证可重复运行）",
    )
    parser.add_argument(
        "--allow-insufficient",
        action="store_true",
        help="已发布笔记数 < INSIGHTS_MIN_PUBLISHED 时仍运行（验证「样本不足」真实路径）",
    )
    return parser.parse_args(argv)


def _refuse_if_not_configured(*, allow_insufficient: bool) -> int | None:
    """LLM / 向量化依赖 / 样本量三重前置检查；不满足 → 打印指引并返回 2（零请求）。"""
    readiness = llm_factory.describe_llm_readiness()
    if not readiness["ready"]:
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

    provider = (settings.EMBEDDING_PROVIDER or "local").strip().lower()
    if provider == "local" and not local_deps_installed():
        print("=" * 78)
        print("⚠️  本地向量化依赖未安装（提炼落库需要真实向量），**未发起任何请求**。")
        print("=" * 78)
        print("  1) cd backend && .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        print("  2) （国内网络建议）set HF_ENDPOINT=https://hf-mirror.com")
        print("     首次运行会自动下载 bge 模型（约 1.3GB），之后离线可用。")
        print()
        print(f"  详细说明：{INSTALL_HINT}")
        print("  可先运行 scripts/check_embedding_local.py 预检本地向量化。")
        return 2
    if provider == "openai" and (
        not settings.EMBEDDING_OPENAI_MODEL.strip() or not settings.EMBEDDING_API_KEY.strip()
    ):
        print("=" * 78)
        print("⚠️  openai 兼容 embedding 未配置（EMBEDDING_OPENAI_MODEL / EMBEDDING_API_KEY），")
        print("    **未发起任何请求**。")
        return 2

    with SessionLocal() as db:
        published = int(
            db.execute(
                text("SELECT count(*) FROM notes WHERE status = 'published'")
            ).scalar_one()
        )
    min_required = max(1, int(settings.INSIGHTS_MIN_PUBLISHED))
    if published < min_required and not allow_insufficient:
        print("=" * 78)
        print("⚠️  已发布笔记样本不足，**未发起任何请求**。")
        print("=" * 78)
        print(f"  当前已发布 {published} 篇 < 门槛 INSIGHTS_MIN_PUBLISHED={min_required}")
        print()
        print("  任选其一：")
        print("   a) 先把账号中的笔记状态推进到已发布并回填数据快照（推荐积累 ≥ 3 篇）；")
        print("   b) 临时把 backend/.env 的 INSIGHTS_MIN_PUBLISHED 调低（例如 1）；")
        print("   c) 用 --allow-insufficient 验证「样本不足」真实路径：")
        print("      「零模型调用、零写入、insufficient_sample=True、data_gaps 给出指引」。")
        return 2
    return None


# ---------------------------------------------------------------------------
# 低层助手
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


def _published_note_ids() -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(
            text("SELECT id::text FROM notes WHERE status = 'published'")
        ).all()
    return {str(row[0]) for row in rows}


def _fetch_snippet_rows(ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT id::text AS id, chunk_type, status, title, content, meta, "
                "(embedding IS NOT NULL) AS has_embedding "
                "FROM knowledge_snippets WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        ).all()
    return [dict(row._mapping) for row in rows]


def _fetch_run(run_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT run_type, status, outputs, llm_raw, llm_model, llm_base_url, "
                "prompt_tokens, completion_tokens, total_tokens, json_parse_attempts "
                "FROM agent_runs WHERE id = :rid"
            ),
            {"rid": run_id},
        ).first()
    return dict(row._mapping) if row is not None else {}


def _cleanup(
    new_runs: set[str], new_threads: set[str], snippet_ids: list[str], *, keep: bool
) -> tuple[int, int, int]:
    """差集删 agent_runs / 检查点线程；知识块按 id 删（--keep-run 时全部保留）。"""
    deleted_runs = deleted_threads = deleted_snippets = 0
    with engine.begin() as conn:
        if new_runs and not keep:
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
        if snippet_ids and not keep:
            deleted_snippets = conn.execute(
                text("DELETE FROM knowledge_snippets WHERE id = ANY(:ids)"),
                {"ids": snippet_ids},
            ).rowcount
    return deleted_runs, deleted_threads, deleted_snippets


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------
def _assert_result(
    c: Checker,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    run: dict[str, Any],
    *,
    allow_insufficient: bool,
) -> None:
    insufficient = bool(summary.get("insufficient_sample"))

    if insufficient:
        c.check(
            allow_insufficient,
            "样本不足仅在 --allow-insufficient 时出现（硬门生效）",
        )
        c.check(
            summary.get("status") == "partial"
            and summary.get("persisted") == 0
            and summary.get("generated") == 0,
            "样本不足：status=partial、零写入（不硬造规律）",
            f"status={summary.get('status')} persisted={summary.get('persisted')}",
        )
        gaps = " ".join(str(item) for item in (summary.get("data_gaps") or []))
        c.check(
            "样本不足" in gaps and "至少发布" in gaps and "回填数据后再提炼" in gaps,
            "data_gaps 给出「至少发布 N 篇并回填数据后再提炼」的明确指引",
            f"{gaps[:180]}",
        )
        c.check(
            int(summary.get("parse_attempts") or 0) == 0 and not rows,
            "样本不足：零模型调用（parse_attempts=0）且零知识块落库",
        )
        print("[INFO] 本次为「样本不足」真实路径验证（未消耗任何 LLM token）。")
        return

    c.check(
        summary.get("status") in {"success", "partial"}
        and int(summary.get("generated") or 0) >= 1
        and (int(summary.get("persisted") or 0) + int(summary.get("reused") or 0)) >= 1,
        "提炼成功：generated ≥ 1 且 persisted/reused 合计 ≥ 1",
        f"status={summary.get('status')} generated={summary.get('generated')} "
        f"persisted={summary.get('persisted')} reused={summary.get('reused')}",
    )

    attempts = int(summary.get("parse_attempts") or 0)
    c.check(
        1 <= attempts <= int(settings.AGENT_JSON_MAX_ATTEMPTS),
        f"parse_attempts 在 1~{settings.AGENT_JSON_MAX_ATTEMPTS}（理想为 1）",
        f"{attempts}",
    )
    if attempts == 1:
        print("[INFO] parse_attempts=1：模型首轮即输出合法 JSON，未走回炉。")
    else:
        print(f"[INFO] parse_attempts={attempts}：模型经历了纠正重问（仍属正常范围）。")

    # ---- 规律结构与来源真实性 ----
    published_ids = _published_note_ids()
    c.check(
        bool(rows) and all(row["has_embedding"] for row in rows),
        "知识块落库且 embedding 非空（真实 bge 向量）",
        f"{len(rows)} 行",
    )
    c.check(
        all(row["chunk_type"] in {"hit_pattern", "review_insight"} for row in rows),
        "chunk_type 均为 hit_pattern / review_insight",
        f"{[row['chunk_type'] for row in rows]}",
    )
    source_ok = True
    evidence_ok = True
    confidence_ok = True
    for row in rows:
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        sources = [str(item) for item in (meta.get("source_note_ids") or [])]
        if not sources or not set(sources) <= published_ids:
            source_ok = False
        evidence = meta.get("evidence")
        summary_text = ""
        if isinstance(evidence, dict):
            summary_text = str(evidence.get("summary") or "")
        if not summary_text.strip():
            evidence_ok = False
        confidence = meta.get("confidence")
        if confidence is not None and not (
            isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0
        ):
            confidence_ok = False
    c.check(
        source_ok,
        "每条规律的来源均 ⊆ 库内已发布笔记 id（编造来源已被本地校验过滤）",
        f"{[row['meta'].get('source_note_ids') if isinstance(row['meta'], dict) else None for row in rows]}",
    )
    c.check(evidence_ok, "每条规律均带指标证据（evidence 非空）")
    c.check(confidence_ok, "置信度均在 0~1 或为空（不臆造）")

    # ---- 零外部数字（红线）----
    blob = json.dumps(
        [
            {
                "content": row.get("content"),
                "meta": row.get("meta"),
            }
            for row in rows
        ],
        ensure_ascii=False,
    )
    hits = [phrase for phrase in EXTERNAL_PHRASES if phrase in blob]
    c.check(
        not hits,
        "规律内容不含「行业平均 / 爆款门槛 / 大盘数据」等外部数字话术（红线）",
        f"{hits}",
    )

    # ---- 审计回填与脱敏 ----
    c.check(
        run.get("run_type") == "insights"
        and run.get("status") in {"success", "partial"}
        and isinstance(run.get("outputs"), dict)
        and (run.get("outputs") or {}).get("persisted") is not None,
        "agent_runs：run_type=insights + status + outputs 业务明细已回填",
        f"run_type={run.get('run_type')} status={run.get('status')}",
    )
    llm_raw = str(run.get("llm_raw") or "")
    c.check(
        bool(llm_raw.strip()) and not SECRET_PATTERN.search(llm_raw),
        "llm_raw 已归档且不含密钥形态（脱敏 + 截断）",
        f"{llm_raw[:120]}",
    )
    c.check(
        bool(run.get("llm_model")) and bool(run.get("llm_base_url")),
        "llm_model / llm_base_url 已记录（绝不记录 key）",
    )
    total_tokens = int(run.get("total_tokens") or 0)
    c.check(
        total_tokens > 0,
        "agent_runs 记录到真实 token 用量",
        f"total={total_tokens}",
    )
    print("-" * 78)
    print(
        f"  token：prompt={run.get('prompt_tokens')} "
        f"completion={run.get('completion_tokens')} "
        f"total={total_tokens}"
        f"（成本 = token × 你的实际单价，本仓库不内置价格表）"
    )
    # ---- 质量人工确认提示（机器断言不了规律质量） ----
    for index, row in enumerate(rows, start=1):
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        print("-" * 78)
        print(f"  第 {index} 条规律（**请人工过目一眼**）：")
        print(f"    {row.get('content')}")
        print(f"    场景：{meta.get('scenario')}")
        print(f"    来源：{meta.get('source_note_ids')}｜置信度：{meta.get('confidence')}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    args = _parse_args()
    refusal = _refuse_if_not_configured(allow_insufficient=args.allow_insufficient)
    if refusal is not None:
        return refusal

    init_checkpointer()
    before_threads = _current_threads()
    before_runs = _current_run_ids()

    c = Checker()
    readiness = llm_factory.describe_llm_readiness()
    print("=" * 78)
    print("爆款规律提炼 真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print("=" * 78)
    print(
        f"  model={readiness['model']} | base_url={settings.LLM_BASE_URL} | 温度≈0.2"
    )
    print(f"  embedding：provider={settings.EMBEDDING_PROVIDER} "
          f"model={settings.EMBEDDING_MODEL} dim={settings.EMBEDDING_DIM}")
    print(f"  prompt_version={settings.INSIGHTS_PROMPT_VERSION} | "
          f"门槛={settings.INSIGHTS_MIN_PUBLISHED} 篇 | 条数上限={settings.INSIGHTS_PATTERN_MAX}")
    print(f"  保留运行记录：{'是（--keep-run）' if args.keep_run else '否（默认清理）'}")
    print("-" * 78)

    summary: dict[str, Any] | None = None
    fatal: str | None = None
    try:
        try:
            summary = generate_insights(trigger="manual")
        except (AppError, AgentConfigError, EmbeddingError) as exc:
            fatal = f"{type(exc).__name__}: {exc}"
            print(f"  [中断] 提炼失败：{fatal}")
    finally:
        # 审计与业务数据必须在清理**之前**抓取
        run: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        snippet_ids: list[str] = []
        if summary is not None:
            run = _fetch_run(str(summary.get("run_id") or ""))
            snippet_ids = [str(item) for item in (summary.get("snippet_ids") or [])]
            rows = _fetch_snippet_rows(snippet_ids)
        new_runs = _current_run_ids() - before_runs
        new_threads = _current_threads() - before_threads
        deleted_runs, deleted_threads, deleted_snippets = _cleanup(
            new_runs, new_threads, snippet_ids, keep=args.keep_run
        )
        close_checkpointer()

    print("-" * 78)
    print(
        f"[INFO] 已清理：agent_runs={deleted_runs} 行 / 检查点行={deleted_threads}"
        f"（线程 {len(new_threads)} 个）/ knowledge_snippets={deleted_snippets} 行"
        + ("（本次选择保留运行记录与知识块）" if args.keep_run else "")
    )

    if fatal is not None:
        c.check(False, "真实提炼运行成功返回", fatal)
    elif isinstance(summary, dict):
        print(
            f"[INFO] 提炼摘要：status={summary.get('status')} "
            f"sample={summary.get('sample_size')}/{summary.get('min_required')} "
            f"generated={summary.get('generated')} persisted={summary.get('persisted')} "
            f"reused={summary.get('reused')} skipped={summary.get('skipped')}"
        )
        _assert_result(
            c, summary, rows, run, allow_insufficient=args.allow_insufficient
        )

    # ---- 清理复查（差集口径）----
    if not args.keep_run:
        c.check(
            (_current_run_ids() - before_runs) == set(),
            "本次运行的 agent_runs 已按差集物理清理",
        )
    c.check(
        (_current_threads() - before_threads) == set(),
        "本次运行的检查点线程已按差集物理清理",
    )

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        if fatal:
            print()
            print("[提示] 提炼在运行中失败，常见原因与处理：")
            print("  · 「规律提炼未就绪」→ 检查 AGENT_LLM_ENABLED / LLM_API_KEY / LLM_MODEL；")
            print("  · 「规律提炼失败：连续 N 轮…」→ 换更稳定的模型或稍后重试；")
            print("  · 「规律向量化失败」→ 先跑 scripts/check_embedding_local.py 预检；")
            print("  · 网络抖动 / 429 → 稍后重试。")
        return 1
    print("全部 PASS ✔（真实 LLM）")
    print("⚠️ 机器断言不了「规律质量」：请对照上方逐条内容人工过目是否可复用、是否言之有据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
