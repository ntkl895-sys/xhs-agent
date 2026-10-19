"""第 23 步 · V2 增强（二）· 多版本创作 Agent **真实 DeepSeek** 检查（需用户填 key 后手动运行）。

本脚本会**真的**向 DeepSeek 发起请求并**消耗 token**，因此**不会**被任何自动流程调用。

前置条件（backend/.env）：

.. code-block:: ini

    LLM_API_KEY=<你的 DeepSeek key>
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=<DeepSeek 控制台当前模型名>     # 以控制台为准
    AGENT_LLM_ENABLED=true                    # 必须显式打开，否则本脚本拒绝运行

它验证**只能靠真实调用才能确认**的事：

1. 单次调用是否能稳定产出 **N 个结构化版本**（标题 / 正文 / 标签 / 角度）；
2. ``json_parse_attempts`` 是否**恒为 1**（模型稳定输出合法 JSON 数组）；
3. **N 版真实差异度**：角度是否互不重复、正文两两相似度（越低差异越大）——打印
   供人工判断（相似度为参考性指标，不设阈值断言）；
4. 真实 token 用量（**按 token 记，不内置价格**）；多版本 ≈ 单篇创作的倍数关系见
   README「二十八、」的估算公式；
5. 落库口径：同批次（variant_group_id = run_id）、恒 draft、初始未采用、选题
   ``approved → writing``；
6. 违禁命中是否按「该版拦截、不落库、不阻断其他版」执行（打印 blocked 明细）。

**不改动你的数据**：只挑选 ``status=approved`` 的选题，跑完把选题状态**还原为
approved**，并删除本次生成的笔记 / 审计行 / 检查点线程（按运行前后差集）。
因此可重复运行。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_content_variants.py

退出码：0 = 全部通过；1 = 有断言失败；2 = 未配置（未填 key / 开关关闭），**未发任何请求**。
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer, init_checkpointer  # noqa: E402
from app.agent.content_variants.service import generate_variants  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

#: 本次真实验证的版本数（1~5；默认 3 —— 与产品默认一致）
N = 3
PREVIEW_CHARS = 220


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


def _pick_approved_topic() -> tuple[Any, str] | None:
    """挑一条可创作的选题（approved + 关联 active 人设）。"""
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT t.id, t.title FROM topics t "
                "JOIN personas p ON p.id = t.persona_id AND p.status = 'active' "
                "WHERE t.status = 'approved' "
                "ORDER BY t.created_at DESC LIMIT 1"
            )
        ).first()
    if row is None:
        return None
    return row[0], row[1]


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


def _topic_status(topic_id: Any) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM topics WHERE id = :tid"), {"tid": str(topic_id)}
        ).scalar_one_or_none()


def _short(text_value: str | None, limit: int = PREVIEW_CHARS) -> str:
    value = (text_value or "").replace("\n", " ⏎ ")
    return value if len(value) <= limit else f"{value[:limit]}…（共 {len(text_value or '')} 字）"


def _similarity(a: str, b: str) -> float:
    """正文两两相似度（0~1；越低表示差异越大）——参考性指标，只记录不断言。"""
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def main() -> int:
    refusal = _refuse_if_not_configured()
    if refusal is not None:
        return refusal

    picked = _pick_approved_topic()
    if picked is None:
        print("[FATAL] 库中没有 status=approved 且人设 active 的选题。")
        print("        请先在第 11 步的「选题库」页面把至少一条选题标记为 approved。")
        return 1

    topic_id, topic_title = picked
    init_checkpointer()
    before_threads = _current_threads()
    before_runs = _current_run_ids()

    c = Checker()
    print("多版本创作真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print(f"model={settings.LLM_MODEL} | base_url={settings.LLM_BASE_URL} | "
          f"prompt_version={settings.CONTENT_VARIANTS_PROMPT_VERSION} | N={N}")
    print(f"选题：{topic_title}")
    print("-" * 78)

    summary = generate_variants(topic_id=topic_id, n=N, trigger="manual")
    run_ids: list[str] = []
    if summary.get("run_id"):
        run_ids.append(str(summary["run_id"]))

    print(f"  运行结果：status={summary['status']} 请求={summary.get('requested')} "
          f"落库={summary.get('persisted')} 拦截={summary.get('blocked')} 不合格={summary.get('rejected')}")
    if summary.get("error_message"):
        print(f"  ⚠️ error_message：{summary['error_message']}")

    print("-" * 78)
    print("【逐版明细】")
    for index, item in enumerate(summary.get("items") or [], start=1):
        print(f"\n  === 第 {index} 版（{item.get('status')}，角度：{item.get('angle') or '—'}）===")
        print(f"  标题：{item.get('title') or '（空）'}")
        print(f"  标签：{item.get('tags')}")
        if item.get("reason"):
            print(f"  判定：{item['reason']}")
        for hit in (item.get("hits") or [])[:6]:
            print(f"      · 「{hit.get('word')}」（{hit.get('category')} / {hit.get('field')}）")
        print(f"  正文：{_short(item.get('body'))}")

    note_ids = [str(item) for item in (summary.get("note_ids") or [])]
    notes = _fetch_notes(note_ids)
    runs = _fetch_runs(run_ids)

    # ---- 生成与落库 ----
    c.check(
        summary["status"] in ("success", "partial") and len(notes) >= 1,
        "至少产出一个合格版本并落库（success / partial）",
        f"{summary['status']} notes={len(notes)}",
    )
    c.check(
        summary["status"] != "success" or summary.get("persisted") == N,
        "success 时恰落库 N 版",
        f"persisted={summary.get('persisted')}",
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
    group_id = summary.get("variant_group_id")
    c.check(
        bool(group_id) and all(
            str(((item["llm_meta"] or {}).get("variant") or {}).get("group_id")) == str(group_id)
            for item in notes
        ),
        "同批次：全部落库版本共享 variant_group_id（= run_id）",
        f"{group_id}",
    )
    c.check(
        all(((item["llm_meta"] or {}).get("variant") or {}).get("adopted") is False for item in notes),
        "初始未采用（adopted=False）",
    )

    # ---- 真实差异度（记录 + 角度互斥断言）----
    persisted_angles = [
        str(((item["llm_meta"] or {}).get("variant") or {}).get("angle")) for item in notes
    ]
    c.check(
        len(set(persisted_angles)) == len(persisted_angles) and len(persisted_angles) >= 1,
        "落库版本的角度互不重复（提示词约束的实际服从度）",
        f"{persisted_angles}",
    )
    bodies = [(item["title"], item["body"] or "") for item in notes]
    print("-" * 78)
    print("【N 版真实差异度（参考性指标，不断言阈值）】")
    print(f"  角度序列：{persisted_angles}（去重后 {len(set(persisted_angles))} 个）")
    if len(bodies) >= 2:
        ratios = [
            _similarity(bodies[i][1], bodies[j][1])
            for i in range(len(bodies))
            for j in range(i + 1, len(bodies))
        ]
        print(f"  正文两两相似度：最低 {min(ratios):.2f} / 平均 {sum(ratios) / len(ratios):.2f} "
              f"/ 最高 {max(ratios):.2f}（越低差异越大）")
        title_dupes = len(bodies) - len({item[0] for item in bodies})
        print(f"  标题重复数：{title_dupes}")
    if (summary.get("blocked") or 0) > 0:
        print(f"  blocked 明细：{[(i.get('angle'), i.get('reason')) for i in (summary.get('items') or []) if i.get('status') == 'blocked']}")

    # ---- 审计回填 ----
    c.check(bool(runs) and len(runs) == len(run_ids), "agent_runs 行数与运行次数一致",
            f"{len(runs)}/{len(run_ids)}")
    c.check(
        all(item["status"] in ("success", "partial", "failed") for item in runs),
        "运行状态归入 success / partial / failed（与汇总一致）",
        f"{[item['status'] for item in runs]}",
    )
    attempts = [item["json_parse_attempts"] for item in runs]
    c.check(
        bool(attempts) and all(value == 1 for value in attempts),
        "json_parse_attempts 恒为 1（JSON 输出稳定；期望值）",
        f"{attempts}",
    )
    c.check(
        all(item["llm_model"] and item["llm_base_url"] for item in runs),
        "llm_model / llm_base_url 已记录（绝不记录 key）",
    )
    c.check(
        all("sk-" not in str(item["llm_raw"] or "") and (item["llm_raw"] or "") for item in runs),
        "llm_raw 已归档且不含密钥",
    )
    outputs_ok = all(
        (item["outputs"] or {}).get("variant_group_id")
        and isinstance((item["outputs"] or {}).get("persisted"), int)
        and isinstance((item["outputs"] or {}).get("blocked"), int)
        and isinstance((item["outputs"] or {}).get("rejected"), int)
        for item in runs
    )
    c.check(
        outputs_ok,
        "agent_runs.outputs 已回填业务明细（批次 / 计数）",
        f"{[item['outputs'] for item in runs][:1]}",
    )

    # ---- 选题状态流转 ----
    status_now = _topic_status(topic_id)
    c.check(
        status_now == "writing" if len(notes) > 0 else status_now == "approved",
        "有落库 → 选题 approved→writing；零落库 → 选题保持 approved",
        f"{status_now}",
    )

    total_tokens = sum((item["total_tokens"] or 0) for item in runs)
    print("-" * 78)
    print(f"[INFO] 本次共消耗 total_tokens = {total_tokens}"
          f"（成本 = token × 你的实际单价，本仓库不内置价格表）")

    # ---- 人工判断素材（无法自动断言）----
    print("-" * 78)
    print("【人工判断】以下 3 项**无法自动断言**，请逐条阅读：")
    print("  ① N 个版本的角度 / 切入方式是否**真的明显不同**（而不是换汤不换药）；")
    print("  ② 每个版本是否都贴合该人设的口吻与选题方向、有信息量；")
    print("  ③ 你是否愿意采用其中一版进入创作台润色后发布。")

    # ---- 清理：还原选题状态 + 删除本次产物（按差集，含失败的 run）----
    new_runs = _current_run_ids() - before_runs
    new_threads = _current_threads() - before_threads
    deleted_notes = deleted_runs = deleted_threads = restored = 0
    with engine.begin() as conn:
        if note_ids:
            deleted_notes = conn.execute(
                text("DELETE FROM notes WHERE id = ANY(:ids)"), {"ids": note_ids}
            ).rowcount
        restored = conn.execute(
            text("UPDATE topics SET status = 'approved' WHERE id = :tid"), {"tid": str(topic_id)}
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
        f"已把选题状态还原为 approved"
    )

    with engine.connect() as conn:
        residue_notes = (
            conn.execute(
                text("SELECT count(*) FROM notes WHERE id = ANY(:ids)"), {"ids": note_ids}
            ).scalar_one()
            if note_ids
            else 0
        )
    c.check(residue_notes == 0, "本次生成的笔记已物理清理", f"残留 {residue_notes}")
    c.check(
        _topic_status(topic_id) == "approved",
        "选题状态已还原为 approved（脚本可重复运行）",
        f"{_topic_status(topic_id)}",
    )

    close_checkpointer()

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for item in c.failures:
            print(f"  - FAIL: {item}")
        return 1
    print("全部 PASS ✔（真实 LLM）")
    print("⚠️ 请阅读上方【人工判断】三问 —— 版本差异度与质量无法自动断言。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
