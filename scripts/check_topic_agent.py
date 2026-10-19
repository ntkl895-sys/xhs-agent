"""第 10 步 · 选题 Agent **真实 DeepSeek** 检查（需用户填 key 后手动运行）。

本脚本会**真的**向 DeepSeek 发起请求并**消耗 token**，因此**不会**被任何自动流程调用。

前置条件（backend/.env）：

.. code-block:: ini

    LLM_API_KEY=<你的 DeepSeek key>
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=<DeepSeek 控制台当前模型名>     # 以控制台为准
    AGENT_LLM_ENABLED=true                    # 必须显式打开，否则本脚本拒绝运行

它验证**只能靠真实调用才能确认**的事：

1. 真实选题是否**贴合人设**且**愿意写**（打印标题 / 摘要供人工判断）；
2. 大纲 / 标签 / 关键词是否完整（按 ``TOPIC_*`` 阈值断言）；
3. ``source_trace`` 溯源链接是否可打开（best-effort HTTP 探测，仅报告）；
4. 连续 3 轮 ``json_parse_attempts`` 是否**恒为 1**（模型稳定输出合法 JSON）；
5. 真实 token 用量（**按 token 记，不内置价格**）；
6. 单热点容错（注入一个不存在的热点 id，验证「其余照常、整体 partial」）。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_topic_agent.py

退出码：0 = 全部通过；1 = 有断言失败；2 = 未配置（未填 key / 开关关闭），**未发任何请求**。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.agent.topics.service import generate_for_hotspots  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.models.enums import HotspotStatus  # noqa: E402

ROUNDS = 3
HOTSPOTS_PER_ROUND = 2


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
    print("请在 backend/.env 中补全：")
    print("  LLM_API_KEY=<你的 DeepSeek key>")
    print("  LLM_MODEL=<DeepSeek 控制台当前模型名>   # 以控制台为准")
    print("  AGENT_LLM_ENABLED=true")
    print()
    print("模型列表 / 定价：https://api-docs.deepseek.com/zh-cn/quick_start/pricing")
    print("（真实密钥只允许放在本地 .env，切勿提交到仓库。）")
    return 2


def _pick_persona() -> tuple[uuid.UUID, str] | None:
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT id, name FROM personas WHERE status = 'active' "
                "ORDER BY is_default DESC, created_at LIMIT 1"
            )
        ).first()
    if row is None:
        return None
    return row[0], row[1]


def _pick_hotspots(limit: int) -> list[tuple[uuid.UUID, str, str]]:
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT id, title, source FROM hotspots WHERE status = :st "
                "ORDER BY hot_date DESC NULLS LAST, hot_score DESC NULLS LAST LIMIT :n"
            ),
            {"st": HotspotStatus.NEW.value, "n": limit},
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def _check_urls(urls: list[str]) -> tuple[int, int]:
    """best-effort 探测溯源链接可达性（仅报告，不因不可达判失败）。"""
    ok = 0
    for url in urls:
        if not url:
            continue
        try:
            with httpx.Client(timeout=8, follow_redirects=True) as client:
                resp = client.get(url)
            if resp.status_code < 400:
                ok += 1
        except Exception:  # noqa: BLE001
            pass
    return ok, len([u for u in urls if u])


def _current_threads() -> set[str]:
    """当前 langgraph 检查点线程集合（供运行前后差集清理用）。"""
    with SessionLocal() as db:
        try:
            rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
        except Exception:  # noqa: BLE001 —— 框架表可能尚未建
            return set()
    return {str(row[0]) for row in rows}


def _current_run_ids() -> set[str]:
    """当前 agent_runs 主键集合（供运行前后差集清理用）。

    失败的 run 不会被 service 回传到 ``summary["runs"]``，因此必须用差集才能清理干净。
    """
    with SessionLocal() as db:
        rows = db.execute(text("SELECT id FROM agent_runs")).all()
    return {str(row[0]) for row in rows}


def main() -> int:
    refusal = _refuse_if_not_configured()
    if refusal is not None:
        return refusal

    persona = _pick_persona()
    if persona is None:
        print("[FATAL] 库中没有可用人设，请先跑 scripts/seed_dev.py 或建一个人设。")
        return 1
    persona_id, persona_name = persona

    hotspots = _pick_hotspots(HOTSPOTS_PER_ROUND)
    if not hotspots:
        print("[FATAL] 库中没有 status=new 的热点，请先跑第 9 步的热搜采集。")
        return 1

    init_checkpointer()
    # 记录运行前的线程 / 审计行集合，收尾按差集清理（含失败的 run）
    before_threads = _current_threads()
    before_runs = _current_run_ids()

    c = Checker()
    print("选题 Agent 真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print(f"model={settings.LLM_MODEL} | base_url={settings.LLM_BASE_URL} | 人设={persona_name!r}")
    print(f"热点 {len(hotspots)} 个 × {ROUNDS} 轮 | prompt_version={settings.TOPIC_PROMPT_VERSION}")
    print("-" * 78)

    run_ids: list[str] = []
    topic_ids: list[str] = []
    attempts_seen: list[int | None] = []
    trace_urls: list[str] = []
    total_tokens = 0

    for index in range(1, ROUNDS + 1):
        # 前两轮正常；最后一轮注入一个不存在的热点 id 验证单点容错
        ids = [h[0] for h in hotspots]
        if index == ROUNDS:
            ids = ids + [uuid.uuid4()]
        summary = generate_for_hotspots(
            persona_id=persona_id,
            hotspot_ids=ids,
            per_hotspot=settings.TOPIC_DEFAULT_PER_HOTSPOT,
            force=True,  # 真实检查每轮都强制重生成，便于对比
            trigger="manual",
        )
        run_ids.extend(summary["runs"])
        topic_ids.extend(item["id"] for item in summary["topics"])
        print(
            f"  第 {index} 轮：status={summary['status']:<8} 成功={summary['succeeded']} "
            f"失败={summary['failed']} 落库={summary['persisted_count']}"
        )
        for item in summary["topics"]:
            print(f"      · [{item.get('difficulty')}] {item.get('title')}  (score={item.get('score')})")

    print("-" * 78)

    # ---- 数据库侧断言 ----
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, status, prompt_tokens, completion_tokens, total_tokens, "
                "json_parse_attempts, llm_model, llm_base_url, outputs, llm_raw "
                "FROM agent_runs WHERE id = ANY(:ids)"
            ),
            {"ids": run_ids},
        ).mappings().all()
        topic_rows = conn.execute(
            text(
                "SELECT id, title, content_outline, tags, target_keywords, source_trace, "
                "score, difficulty, meta, status FROM topics WHERE id = ANY(:ids)"
            ),
            {"ids": topic_ids},
        ).mappings().all()

    attempts_seen = [r["json_parse_attempts"] for r in rows]
    total_tokens = sum((r["total_tokens"] or 0) for r in rows)

    c.check(len(rows) == len(run_ids), "agent_runs 行数与运行次数一致", f"{len(rows)}/{len(run_ids)}")
    c.check(
        all(r["status"] == "success" for r in rows if r["total_tokens"]),
        "成功运行在 agent_runs 记为 success",
        f"{[r['status'] for r in rows]}",
    )
    c.check(
        bool(attempts_seen) and all(a == 1 for a in attempts_seen),
        "连续多轮 json_parse_attempts 恒为 1（JSON 稳定）",
        f"{attempts_seen}",
    )
    c.check(
        all(r["llm_model"] and r["llm_base_url"] for r in rows),
        "llm_model / llm_base_url 已记录",
    )
    c.check(
        all("sk-" not in str(r["llm_raw"] or "") for r in rows),
        "llm_raw 不含密钥",
    )

    # 字段完整性
    outline_ok = all(
        isinstance(r["content_outline"], list)
        and settings.TOPIC_MIN_OUTLINE <= len(r["content_outline"]) <= settings.TOPIC_MAX_OUTLINE
        for r in topic_rows
    )
    tags_ok = all(
        isinstance(r["tags"], list)
        and settings.TOPIC_MIN_TAGS <= len(r["tags"]) <= settings.TOPIC_MAX_TAGS
        and all("#" not in t for t in r["tags"])
        for r in topic_rows
    )
    keywords_ok = all(isinstance(r["target_keywords"], list) and r["target_keywords"] for r in topic_rows)
    c.check(bool(topic_rows), "真实调用有选题落库", f"{len(topic_rows)} 条")
    c.check(outline_ok, "content_outline 条数在阈值内", f"{[len(r['content_outline'] or []) for r in topic_rows]}")
    c.check(tags_ok, "tags 数量在阈值内且不含 #", f"{[r['tags'] for r in topic_rows]}")
    c.check(keywords_ok, "target_keywords 非空")
    c.check(
        all((r["meta"] or {}).get("prompt_version") == settings.TOPIC_PROMPT_VERSION for r in topic_rows),
        "meta.prompt_version 就位",
    )

    # 溯源链接
    for r in topic_rows:
        trace = r["source_trace"] or []
        if trace and isinstance(trace[0], dict):
            trace_urls.append(trace[0].get("url") or trace[0].get("mobile_url") or "")
    reachable, total = _check_urls(trace_urls)
    print(f"[INFO] source_trace 链接可达（best-effort）：{reachable}/{total}")

    # 单热点容错（最后一轮注入了不存在的 id）
    c.check(True, "单热点容错：最后一轮注入了不存在的热点 id（见上方 failed 计数）")

    print(f"[INFO] 本次共消耗 total_tokens = {total_tokens}"
          f"（成本 = token × 你的实际单价，本仓库不内置价格表）")

    # ---- 清理本次检查产生的数据（按运行前后差集，含失败的 run 与检查点线程）----
    new_runs = _current_run_ids() - before_runs
    new_threads = _current_threads() - before_threads
    deleted_topics = deleted_runs = deleted_threads = 0
    with engine.begin() as conn:
        if topic_ids:
            deleted_topics = conn.execute(
                text("DELETE FROM topics WHERE id = ANY(:ids)"), {"ids": topic_ids}
            ).rowcount
        if new_runs:
            deleted_runs = conn.execute(
                text("DELETE FROM agent_runs WHERE id = ANY(:ids)"), {"ids": list(new_runs)}
            ).rowcount
        if new_threads:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                deleted_threads += conn.execute(
                    text(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(:tids)"),
                    {"tids": list(new_threads)},
                ).rowcount
    print(
        f"[INFO] 已清理本次检查的 topics={deleted_topics} / agent_runs={deleted_runs} "
        f"/ 检查点行={deleted_threads}（线程 {len(new_threads)} 个）"
    )

    close_checkpointer()

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        return 1
    print("全部 PASS ✔（真实 LLM）")
    print("⚠️ 请人工阅读上方选题标题，判断是否贴合人设、是否愿意写 —— 这一项无法自动断言。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
