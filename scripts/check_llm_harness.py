"""Agent Harness **真实 LLM** 检查（需用户自行填 key 后手动运行）。

本脚本会**真的**向 DeepSeek 发起请求并**消耗 token**，因此**不会**被任何自动流程调用。

前置条件（backend/.env）：

.. code-block:: ini

    LLM_API_KEY=<你的 DeepSeek key>
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=<DeepSeek 控制台当前模型名>     # 例如 deepseek-chat，以控制台为准
    AGENT_LLM_ENABLED=true                    # 必须显式打开，否则本脚本拒绝运行

它验证三件**只能靠真实调用才能确认**的事：

1. 连续 3 次最小结构化输出是否都成功；
2. ``json_parse_attempts`` 是否恒为 1（= 模型稳定输出合法 JSON，无需纠正重问）；
3. ``usage`` token 是否被回填进 ``agent_runs``、状态是否落 ``success``。

另外验证错误路径：故意用**错误的 key** 调用，确认
① 落 ``failed``；② ``error_message`` **不泄露 key**。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_llm_harness.py

退出码：0 = 全部通过；1 = 有断言失败；2 = 未配置（未填 key / 开关关闭），**未发任何请求**。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from langchain_openai import ChatOpenAI  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agent.examples import smoke_graph as S  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.agent.runner import run_agent  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import engine  # noqa: E402

RUNS = 3
FAKE_WRONG_KEY = "sk-WRONGKEY000000000000000000"


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


def _refuse_if_not_configured() -> int | None:
    """未配置时打印清晰指引并以退出码 2 结束（**不发起任何请求**）。"""
    readiness = llm_factory.describe_llm_readiness()
    if readiness["ready"]:
        return None

    print("=" * 78)
    print("⚠️  未检测到可用的真实 LLM 配置，**未发起任何请求**。")
    print("=" * 78)
    print(f"  AGENT_LLM_ENABLED = {readiness['llm_enabled']}")
    print(f"  LLM_API_KEY 已填写 = {readiness['has_api_key']}")
    print(f"  LLM_MODEL = {readiness['model']!r}"
          f"{'  (仍是占位符)' if readiness['model_is_placeholder'] else ''}")
    print(f"  LLM_BASE_URL = {readiness['base_url']}")
    print()
    print("请在 backend/.env 中补全以下配置后重新运行：")
    print("  LLM_API_KEY=<你的 DeepSeek key>")
    print("  LLM_MODEL=<DeepSeek 控制台当前模型名>   # 以控制台为准")
    print("  AGENT_LLM_ENABLED=true")
    print()
    print("模型列表 / 定价：https://api-docs.deepseek.com/zh-cn/quick_start/pricing")
    print("（真实密钥只允许放在本地 .env，切勿提交到仓库。）")
    return 2


def _current_threads() -> set[str]:
    """当前 langgraph 检查点线程集合（供运行前后差集清理用）。"""
    with engine.connect() as conn:
        try:
            rows = conn.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
        except Exception:  # noqa: BLE001 —— 框架表可能尚未建
            return set()
    return {str(row[0]) for row in rows}


def cleanup(before_threads: set[str] | None = None) -> tuple[int, int]:
    """清理冒烟产生的 ``agent_runs`` 行与检查点线程，返回 ``(行数, 检查点行数)``。"""
    new_threads = _current_threads() - (before_threads or set())
    with engine.begin() as conn:
        deleted = conn.execute(
            text("DELETE FROM agent_runs WHERE inputs->>'kind' = :k"),
            {"k": S.SMOKE_KIND},
        ).rowcount
        deleted_threads = 0
        if new_threads:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                deleted_threads += conn.execute(
                    text(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(:tids)"),
                    {"tids": list(new_threads)},
                ).rowcount
    return deleted, deleted_threads


def main() -> int:
    refusal = _refuse_if_not_configured()
    if refusal is not None:
        return refusal

    c = Checker()
    print("Agent Harness 真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print(f"model={settings.LLM_MODEL} | base_url={settings.LLM_BASE_URL} | 运行 {RUNS} 次")
    print("-" * 78)

    before_threads = _current_threads()
    model = llm_factory.get_chat_model(temperature=0.2, want_json=True)

    run_ids: list[str] = []
    attempts_seen: list[int | None] = []
    usage_seen: list[dict] = []
    statuses: list[str] = []

    for index in range(1, RUNS + 1):
        try:
            result = run_agent(
                S.SMOKE_KIND,
                S.smoke_build_fn(model),
                inputs={
                    "kind": S.SMOKE_KIND,
                    "prompt": f"第 {index} 次：请输出一个标题、若干关键词与 0~100 的分数。",
                    "require_human": False,
                },
                run_type="other",
            )
        except BaseException as exc:  # noqa: BLE001
            statuses.append("failed")
            attempts_seen.append(None)
            usage_seen.append({})
            print(f"  第 {index} 次：FAILED  {type(exc).__name__}: {str(exc)[:120]}")
            continue

        run_ids.append(result["run_id"])
        statuses.append(result["status"])
        attempts_seen.append(result.get("parse_attempts"))
        usage_seen.append(result.get("usage") or {})
        print(
            f"  第 {index} 次：{result['status']:<8} parse_attempts={result.get('parse_attempts')} "
            f"usage={result.get('usage')} output={result.get('output')}"
        )

    print("-" * 78)

    # ---- ① 三次是否全部成功 ----
    c.check(
        all(s == "success" for s in statuses) and len(run_ids) == RUNS,
        f"连续 {RUNS} 次真实结构化输出全部成功",
        f"statuses={statuses}",
    )

    # ---- ② json_parse_attempts 是否恒为 1 ----
    c.check(
        attempts_seen and all(a == 1 for a in attempts_seen),
        "json_parse_attempts 恒为 1（模型输出稳定，无需纠正重问）",
        f"attempts={attempts_seen}",
    )

    # ---- ③ 数据库回填 ----
    if run_ids:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, status, prompt_tokens, completion_tokens, total_tokens, "
                    "json_parse_attempts, latency_ms, error_message "
                    "FROM agent_runs WHERE id = ANY(:ids)"
                ),
                {"ids": run_ids},
            ).mappings().all()
        tokens_ok = all(
            r["prompt_tokens"] is not None and r["completion_tokens"] is not None and r["total_tokens"] is not None
            for r in rows
        )
        c.check(len(rows) == len(run_ids), "agent_runs 行数与运行次数一致", f"{len(rows)}/{len(run_ids)}")
        c.check(
            all(r["status"] == "success" for r in rows),
            "agent_runs 全部记为 success",
            f"{[r['status'] for r in rows]}",
        )
        c.check(
            tokens_ok,
            "真实 token 用量已回填（prompt/completion/total 均非空）",
            f"{[{k: r[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')} for r in rows]}",
        )
        c.check(
            all(r["json_parse_attempts"] == 1 for r in rows),
            "数据库 json_parse_attempts 全部为 1",
            f"{[r['json_parse_attempts'] for r in rows]}",
        )
        c.check(
            all(r["error_message"] is None for r in rows),
            "成功运行没有 error_message 残留",
        )
        total = sum((r["total_tokens"] or 0) for r in rows)
        print(f"[INFO] 本次共消耗 total_tokens = {total}（成本 = token × 你的实际单价，本仓库不内置价格表）")

    # ---- ④ 错误路径：故意用错误的 key ----
    print("-" * 78)
    print("错误路径验证：故意使用错误的 key（预期 401 → failed，且不得泄露 key）")
    bad_model = ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=FAKE_WRONG_KEY,
        base_url=settings.LLM_BASE_URL,
        timeout=settings.AGENT_NODE_TIMEOUT_SECONDS,
        max_retries=0,
    )
    raised: BaseException | None = None
    try:
        run_agent(
            S.SMOKE_KIND,
            S.smoke_build_fn(bad_model),
            inputs={"kind": S.SMOKE_KIND, "require_human": False, "case": "wrong_key"},
            run_type="other",
        )
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    c.check(raised is not None, "错误 key 的运行如实抛异常", f"{type(raised).__name__ if raised else 'None'}")

    with engine.connect() as conn:
        bad_row = conn.execute(
            text(
                "SELECT status, error_message, llm_base_url FROM agent_runs "
                "WHERE inputs->>'kind' = :k AND inputs->>'case' = 'wrong_key' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"k": S.SMOKE_KIND},
        ).mappings().first()
    c.check(bad_row is not None and bad_row["status"] == "failed", "错误 key 运行落 failed", f"{dict(bad_row) if bad_row else None}")
    err_msg = (bad_row or {}).get("error_message") or ""
    c.check(
        FAKE_WRONG_KEY not in err_msg and "sk-" not in err_msg,
        "error_message 不泄露 key（已脱敏）",
        err_msg[:160],
    )

    deleted, deleted_threads = cleanup(before_threads)
    print(f"[INFO] 已清理本次冒烟行：agent_runs={deleted} / 检查点行={deleted_threads}")
    with engine.connect() as conn:
        residual = conn.execute(
            text("SELECT count(*) FROM agent_runs WHERE inputs->>'kind' = :k"),
            {"k": S.SMOKE_KIND},
        ).scalar()
    thread_residual = len(_current_threads() - before_threads)
    c.check(
        residual == 0 and thread_residual == 0,
        "冒烟残留为 0（agent_runs + 检查点线程）",
        f"agent_runs={residual} 新增线程={thread_residual}",
    )

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for f in c.failures:
            print(f"  - FAIL: {f}")
        return 1
    print("全部 PASS ✔（真实 LLM）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
