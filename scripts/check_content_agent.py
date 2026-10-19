"""第 12 步 · 创作 Agent **真实 DeepSeek** 检查（需用户填 key 后手动运行）。

本脚本会**真的**向 DeepSeek 发起请求并**消耗 token**，因此**不会**被任何自动流程调用。

前置条件（backend/.env）：

.. code-block:: ini

    LLM_API_KEY=<你的 DeepSeek key>
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=<DeepSeek 控制台当前模型名>     # 以控制台为准
    AGENT_LLM_ENABLED=true                    # 必须显式打开，否则本脚本拒绝运行

它验证**只能靠真实调用才能确认**的事：

1. 真实草稿是否**贴合人设 / 选题**且**愿意发**（打印标题 / 正文 / 标签供人工判断）；
2. 正文长度与标签数量是否满足 ``NOTE_*`` 阈值（不满足只能由「降级落库」兜底，属异常信号）；
3. 连续多轮 ``json_parse_attempts`` 是否**恒为 1**（模型稳定输出合法 JSON）；
4. 真实 token 用量（**按 token 记，不内置价格**）；
5. 违禁词自检对**真实模型输出**的命中情况（打印命中的词 / 字段 / 片段）；
6. 落库字段与选题状态流转（``approved`` → ``writing``）是否与离线自检一致。

**不改动你的数据**：只挑选 ``status=approved`` 的选题，跑完把选题状态**还原为 approved**，
并删除本次生成的笔记 / 审计行 / 检查点线程（按运行前后差集）。因此可重复运行。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_content_agent.py

退出码：0 = 全部通过；1 = 有断言失败；2 = 未配置（未填 key / 开关关闭），**未发任何请求**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.content.service import create_note_for_topic  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

ROUNDS = 3
PREVIEW_CHARS = 260


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


def _pick_approved_topics(limit: int) -> list[tuple[Any, str]]:
    """挑可创作的选题（approved + 关联 active 人设）。"""
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT t.id, t.title FROM topics t "
                "JOIN personas p ON p.id = t.persona_id AND p.status = 'active' "
                "WHERE t.status = 'approved' "
                "ORDER BY t.created_at DESC LIMIT :n"
            ),
            {"n": limit},
        ).all()
    return [(row[0], row[1]) for row in rows]


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


def _fetch_notes(note_ids: list[str]) -> list[dict[str, Any]]:
    if not note_ids:
        return []
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id, topic_id, title, body, tags, content_check, compliance_status, "
                    "status, version, publish_url, xhs_note_id, published_at, llm_meta "
                    "FROM notes WHERE id = ANY(:ids)"
                ),
                {"ids": note_ids},
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
                    "SELECT id, status, llm_model, llm_base_url, prompt_tokens, completion_tokens, "
                    "total_tokens, json_parse_attempts, outputs, llm_raw FROM agent_runs "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": run_ids},
            ).mappings().all()
        ]


def _topic_status_map(topic_ids: list[str]) -> dict[str, str]:
    if not topic_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, status FROM topics WHERE id = ANY(:ids)"), {"ids": topic_ids}
        ).all()
    return {str(row[0]): row[1] for row in rows}


def _short(text_value: str | None, limit: int = PREVIEW_CHARS) -> str:
    value = (text_value or "").replace("\n", " ⏎ ")
    return value if len(value) <= limit else f"{value[:limit]}…（共 {len(text_value or '')} 字）"


def main() -> int:
    refusal = _refuse_if_not_configured()
    if refusal is not None:
        return refusal

    topics = _pick_approved_topics(ROUNDS)
    if not topics:
        print("[FATAL] 库中没有 status=approved 且人设 active 的选题。")
        print("        请先在第 11 步的「选题库」页面把至少一条选题标记为 approved。")
        return 1

    init_checkpointer()
    before_threads = _current_threads()
    before_runs = _current_run_ids()

    c = Checker()
    print("创作 Agent 真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print(f"model={settings.LLM_MODEL} | base_url={settings.LLM_BASE_URL} | "
          f"prompt_version={settings.CONTENT_PROMPT_VERSION}")
    print(f"阈值：标题≤{settings.NOTE_TITLE_MAX_LEN} 字 | 正文≥{settings.NOTE_BODY_MIN_LEN} 字 | "
          f"标签 {settings.NOTE_MIN_TAGS}~{settings.NOTE_MAX_TAGS} 个")
    print(f"将使用 {len(topics)} 条已通过选题（不足 {ROUNDS} 条时按实际条数跑）")
    print("-" * 78)

    used_topic_ids = [str(item[0]) for item in topics]
    run_ids: list[str] = []
    note_ids: list[str] = []
    summaries: list[dict[str, Any]] = []

    for index, (topic_id, topic_title) in enumerate(topics, start=1):
        summary = create_note_for_topic(topic_id=topic_id, trigger="manual")
        summaries.append(summary)
        if summary.get("run_id"):
            run_ids.append(str(summary["run_id"]))
        if summary.get("note_id"):
            note_ids.append(str(summary["note_id"]))
        print(
            f"  第 {index} 轮：status={summary['status']:<8} "
            f"compliance={summary.get('compliance_status')} "
            f"命中={summary.get('forbidden_hits')} 标签={summary.get('tags_count')}"
        )
        print(f"      选题：{topic_title}")
        print(f"      标题：{summary.get('title')}")
        if summary.get("error_message"):
            print(f"      ⚠️ error_message：{summary['error_message']}")

    print("-" * 78)

    notes = _fetch_notes(note_ids)
    runs = _fetch_runs(run_ids)
    status_map = _topic_status_map(used_topic_ids)

    # ---- 生成结果 ----
    c.check(
        bool(notes) and len(notes) == len(topics),
        "每条已通过选题都产出了笔记草稿",
        f"notes={len(notes)} / topics={len(topics)}",
    )
    c.check(
        all(item["status"] == "draft" for item in notes),
        "落库 status 恒为 draft（不自动 ready / 不自动发布）",
        f"{[item['status'] for item in notes]}",
    )
    c.check(
        all(item["version"] == 1 for item in notes),
        "version 恒为 1",
    )
    c.check(
        all(item["publish_url"] is None and item["xhs_note_id"] is None
            and item["published_at"] is None for item in notes),
        "publish_url / xhs_note_id / published_at 全部留空（不自动发布）",
    )

    # ---- 字段完整性（真实质量的核心信号）----
    title_ok = all(len(item["title"] or "") <= settings.NOTE_TITLE_MAX_LEN for item in notes)
    body_ok = all(len(item["body"] or "") >= settings.NOTE_BODY_MIN_LEN for item in notes)
    tags_ok = all(
        isinstance(item["tags"], list)
        and settings.NOTE_MIN_TAGS <= len(item["tags"]) <= settings.NOTE_MAX_TAGS
        and all("#" not in tag for tag in item["tags"])
        for item in notes
    )
    c.check(title_ok, f"标题均不超过 {settings.NOTE_TITLE_MAX_LEN} 字",
            f"{[len(item['title'] or '') for item in notes]}")
    c.check(body_ok, f"正文均不少于 {settings.NOTE_BODY_MIN_LEN} 字（否则会走降级落库）",
            f"{[len(item['body'] or '') for item in notes]}")
    c.check(tags_ok, "标签数量在阈值内且不含 #",
            f"{[item['tags'] for item in notes]}")

    # ---- 合规自检 ----
    statuses = [item["compliance_status"] for item in notes]
    c.check(
        all(value in ("passed", "warning") for value in statuses),
        "compliance_status 只有 passed / warning（无 blocked，不丢弃草稿）",
        f"{statuses}",
    )
    checks_ok = all(
        isinstance(item["content_check"], dict)
        and item["content_check"].get("status") == item["compliance_status"]
        and isinstance(item["content_check"].get("hits"), list)
        and item["content_check"].get("checked_fields") == ["title", "body", "tags"]
        for item in notes
    )
    c.check(checks_ok, "content_check 与 compliance_status 同步且记录 checked_fields")
    hit_counts = [len((item["content_check"] or {}).get("hits") or []) for item in notes]
    c.check(
        all(
            (count == 0) == (item["compliance_status"] == "passed")
            for count, item in zip(hit_counts, notes, strict=True)
        ),
        "命中数与合规结论一致（0 命中 ⇒ passed）",
        f"{hit_counts}",
    )

    # ---- 审计回填 ----
    c.check(bool(runs) and len(runs) == len(run_ids), "agent_runs 行数与运行次数一致",
            f"{len(runs)}/{len(run_ids)}")
    c.check(
        all(item["status"] in ("success", "partial") for item in runs),
        "运行状态为 success（字段/JSON 正常）或 partial（降级落库）",
        f"{[item['status'] for item in runs]}",
    )
    attempts = [item["json_parse_attempts"] for item in runs]
    c.check(
        bool(attempts) and all(value == 1 for value in attempts),
        "连续多轮 json_parse_attempts 恒为 1（JSON 输出稳定）",
        f"{attempts}",
    )
    c.check(
        all(item["llm_model"] and item["llm_base_url"] for item in runs),
        "llm_model / llm_base_url 已记录（绝不记录 key）",
    )
    c.check(
        all("sk-" not in str(item["llm_raw"] or "") for item in runs)
        and all((item["llm_raw"] or "") for item in runs),
        "llm_raw 已归档且不含密钥",
    )
    outputs_ok = all(
        (item["outputs"] or {}).get("note_id")
        and (item["outputs"] or {}).get("generated") == 1
        and (item["outputs"] or {}).get("compliance_status") in ("passed", "warning")
        and isinstance((item["outputs"] or {}).get("forbidden_hits"), int)
        and isinstance((item["outputs"] or {}).get("tags_count"), int)
        and (item["outputs"] or {}).get("title")
        for item in runs
    )
    c.check(
        outputs_ok,
        "agent_runs.outputs 已回填业务明细（note_id / generated / 合规 / 命中数 / 标签数 / 标题）",
        f"{[item['outputs'] for item in runs][:1]}",
    )
    meta_ok = all(
        isinstance(item["llm_meta"], dict)
        and item["llm_meta"].get("prompt_version") == settings.CONTENT_PROMPT_VERSION
        and isinstance(item["llm_meta"].get("json_parse_attempts"), int)
        for item in notes
    )
    c.check(meta_ok, "notes.llm_meta 含 prompt_version / json_parse_attempts",
            f"{[item['llm_meta'] for item in notes][:1]}")

    # ---- 选题状态流转 ----
    c.check(
        all(status_map.get(tid) == "writing" for tid in used_topic_ids),
        "persist 同事务把选题 approved → writing",
        f"{status_map}",
    )

    total_tokens = sum((item["total_tokens"] or 0) for item in runs)
    print(f"[INFO] 本次共消耗 total_tokens = {total_tokens}"
          f"（成本 = token × 你的实际单价，本仓库不内置价格表）")

    # ---- 人工判断素材（无法自动断言）----
    print("-" * 78)
    print("【人工判断】以下 3 项**无法自动断言**，请逐条阅读：")
    print("  ① 草稿是否贴合该人设的口吻与定位；")
    print("  ② 是否真的围绕选题角度展开、有信息量（不是空话凑字数）；")
    print("  ③ 你是否愿意在此基础上人工润色后发布。")
    for index, item in enumerate(notes, start=1):
        hits = (item["content_check"] or {}).get("hits") or []
        print(f"\n  === 第 {index} 篇 ===")
        print(f"  标题：{item['title']}")
        print(f"  标签：{item['tags']}")
        print(f"  合规：{item['compliance_status']}"
              + (f"，命中 {len(hits)} 处" if hits else "（无命中）"))
        for hit in hits[:8]:
            print(f"      · 「{hit.get('word')}」（{hit.get('category')} / {hit.get('field')}）"
                  f"  …{hit.get('snippet')}…")
        print(f"  正文：{_short(item['body'])}")

    # ---- 清理：还原选题状态 + 删除本次产物（按差集，含失败的 run）----
    new_runs = _current_run_ids() - before_runs
    new_threads = _current_threads() - before_threads
    deleted_notes = deleted_runs = deleted_threads = restored = 0
    with engine.begin() as conn:
        if note_ids:
            deleted_notes = conn.execute(
                text("DELETE FROM notes WHERE id = ANY(:ids)"), {"ids": note_ids}
            ).rowcount
        if used_topic_ids:
            restored = conn.execute(
                text("UPDATE topics SET status = 'approved' WHERE id = ANY(:ids)"),
                {"ids": used_topic_ids},
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
    print("-" * 78)
    print(
        f"[INFO] 已清理：notes={deleted_notes} / agent_runs={deleted_runs} "
        f"/ 检查点行={deleted_threads}（线程 {len(new_threads)} 个）；"
        f"已把 {restored} 条选题状态还原为 approved"
    )

    # 复查清理与还原
    with engine.connect() as conn:
        residue_notes = conn.execute(
            text("SELECT count(*) FROM notes WHERE id = ANY(:ids)"), {"ids": note_ids or [None]}
        ).scalar_one() if note_ids else 0
        after_status = _topic_status_map(used_topic_ids)
    c.check(residue_notes == 0, "本次生成的笔记已物理清理", f"残留 {residue_notes}")
    c.check(
        all(value == "approved" for value in after_status.values()),
        "选题状态已还原为 approved（脚本可重复运行）",
        f"{after_status}",
    )

    close_checkpointer()

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        return 1
    print("全部 PASS ✔（真实 LLM）")
    print("⚠️ 请阅读上方【人工判断】三问 —— 草稿质量无法自动断言。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
