"""第 15 步 · 截图识别（视觉）**真实 DeepSeek** 检查（需用户填 key 后手动运行）。

本脚本会**真的**向 DeepSeek 发起视觉请求并**消耗 token**，因此**不会**被任何自动流程调用。

前置条件（backend/.env）：

.. code-block:: ini

    LLM_API_KEY=<你的 DeepSeek key>
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=<DeepSeek 控制台当前模型名>            # 以控制台为准
    LLM_VISION_MODEL=<控制台里支持图片输入的模型名>   # 留空则回退 LLM_MODEL
    AGENT_LLM_ENABLED=true                          # 必须显式打开，否则本脚本拒绝运行

它验证**只能靠真实调用才能确认**的事：

1. 视觉链路是否真的打通：接口接受 ``data:image/...;base64,`` 图片消息，
   并返回**可解析的 JSON**（本仓库的解析 / 纠错链路全程不抛网络层错误）；
2. 连续多轮 ``parse_attempts`` 是否**恒为 1**（模型稳定输出合法 JSON）；
3. 对**基准示例图**的识别准确度：逐轮把六个指标与参考读数**自动对照**
   （示例图 ``smoke_test_panel.png`` 只读引用自 ``screenshot_spike``，绝不改动）；
4. 真实 token 用量（**按 token 记，不内置价格表**）；
5. 审计回填：``agent_runs`` 是否落 ``success`` + ``outputs`` 摘要 +
   ``llm_raw``（已脱敏 + 截断），且**不含图片 base64 / 绝对路径 / key**。

**不改动你的数据**：识别链路零落库（图不做 persist）；跑完按运行前后差集删除
本次 ``agent_runs`` 与 ``langgraph`` 检查点线程，因此可重复运行。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\check_metric_vision.py
    backend\\.venv\\Scripts\\python.exe scripts\\check_metric_vision.py --rounds 1
    backend\\.venv\\Scripts\\python.exe scripts\\check_metric_vision.py --image <你的截图.png>

退出码：0 = 全部通过；1 = 有断言失败 / 识别中断；2 = 未配置（未填 key / 开关关闭），
**未发任何请求**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.agent.metrics.contracts import METRIC_FIELDS, detect_image_mime  # noqa: E402
from app.agent.metrics.service import recognize_metric_screenshot  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

#: 默认连续运行轮数（任务书要求「示例截图连跑 3 次」）
ROUNDS = 3

#: spike 示例截图（**只读引用，绝不改动 screenshot_spike 目录**）
SPIKE_SAMPLE = ROOT_DIR.parent / "screenshot_spike" / "samples" / "input" / "smoke_test_panel.png"

#: 基准示例图的 sha256 —— 命中才启用「与参考读数自动对照」；
#: 换图（自定义 --image）时按「自定义样本」处理，准确度留给人工核对。
SPIKE_SAMPLE_SHA256 = "2d5d31bf9312e2b6030253eddfe60dc54e8450eb603111ef7f79a6b892a9040a"

#: 基准示例图上清晰可见的参考读数（六指标口径）：
#:   阅读量 8600 → views ｜ 点赞 520 → likes ｜ 收藏 88 → collects
#:   评论 43 → comments ｜ 分享 12 → shares ｜ 涨粉 6 → new_followers
#:   （曝光量 1.2万 / 点击量 720 / 点击率 6.1% / 主页访问 310 —— 不在六字段口径，应被忽略）
SPIKE_REFERENCE: dict[str, int] = {
    "views": 8600,
    "likes": 520,
    "collects": 88,
    "comments": 43,
    "shares": 12,
    "new_followers": 6,
}


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
        prog="check_metric_vision.py",
        description=(
            "第 15 步 · 截图识别（视觉）真实 LLM 检查：会向 DeepSeek 发起真实视觉请求并"
            "消耗 token；未配置时安全拒绝（退出码 2，零请求）。"
        ),
    )
    parser.add_argument(
        "--image",
        default=None,
        help="自定义截图路径（默认使用 screenshot_spike 的示例图；默认样本启用基准对照）",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=ROUNDS,
        help=f"连续运行轮数（默认 {ROUNDS}；--rounds 1 可用于最小成本验证）",
    )
    return parser.parse_args(argv)


def _refuse_if_not_configured() -> int | None:
    """视觉未配置（开关 / key / 视觉模型名）→ 打印指引并返回 2；**零请求**。"""
    readiness = llm_factory.describe_llm_readiness()
    if readiness["vision_ready"]:
        return None
    print("=" * 78)
    print("⚠️  未检测到可用的真实**视觉** LLM 配置，**未发起任何请求**。")
    print("=" * 78)
    print(f"  AGENT_LLM_ENABLED = {readiness['llm_enabled']}")
    print(f"  LLM_API_KEY 已填写 = {readiness['has_api_key']}")
    print(f"  LLM_VISION_MODEL = {settings.LLM_VISION_MODEL!r}（留空则回退 LLM_MODEL）")
    print(
        f"  LLM_MODEL = {readiness['model']!r}"
        f"{'  (仍是占位符)' if readiness['model_is_placeholder'] else ''}"
    )
    print(f"  LLM_BASE_URL = {readiness['base_url']}")
    print(f"  vision_model（实际生效）= {readiness['vision_model']!r}")
    print()
    print("请在 backend/.env 中补全：")
    print("  LLM_API_KEY=<你的 DeepSeek key>")
    print("  LLM_VISION_MODEL=<DeepSeek 控制台复制【支持图片输入】的模型名；留空回退 LLM_MODEL>")
    print("  AGENT_LLM_ENABLED=true")
    print()
    print(f"视觉文档：{llm_factory.VISION_DOC_URL}")
    print("（真实密钥只允许放在本地 .env，切勿提交到仓库。）")
    return 2


def _load_image(path_arg: str | None) -> tuple[bytes, str, str, Path] | None:
    """读取截图（默认示例图）；返回 (bytes, mime, 文件名, 路径)；失败返回 None。"""
    path = Path(path_arg).expanduser() if path_arg else SPIKE_SAMPLE
    if not path.is_file():
        print(f"[FATAL] 找不到图片：{path}")
        if not path_arg:
            print("        默认为 screenshot_spike 的示例图（只读引用，不随仓库分发）；")
            print("        可用 --image <你的截图.png> 指定自定义样本。")
        return None
    data = path.read_bytes()
    mime = detect_image_mime(data)
    if mime is None:
        print(f"[FATAL] 文件不是 jpeg / png / webp（文件头魔数校验失败）：{path}")
        return None
    return data, mime, path.name, path


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


def _print_round(index: int, summary: dict[str, Any]) -> None:
    metrics = summary.get("metrics") or {}
    filled = sum(1 for value in metrics.values() if value is not None)
    print(f"  第 {index} 轮：识别 {filled}/6  parse_attempts={summary.get('parse_attempts')}")
    print("      值：" + " | ".join(f"{field}={metrics.get(field)}" for field in METRIC_FIELDS))
    fields = (summary.get("evidence") or {}).get("fields") or {}
    raw_parts = " | ".join(
        f"{field}→{fields.get(field, {}).get('raw')!r}" for field in METRIC_FIELDS
    )
    print(f"      原文：{raw_parts}")
    missing = summary.get("missing_fields") or []
    uncertain = summary.get("uncertain_fields") or []
    if missing:
        print(f"      missing={missing}")
    if uncertain:
        print(f"      uncertain={uncertain}")
    warnings = summary.get("warnings") or []
    for item in warnings[:3]:
        print(f"      ⚠ {item}")
    if len(warnings) > 3:
        print(f"      … 其余 {len(warnings) - 3} 条 warnings 略")


def _cleanup(new_runs: set[str], new_threads: set[str]) -> tuple[int, int]:
    """按运行前后差集删除本次识别的 agent_runs 与检查点线程。"""
    deleted_runs = deleted_threads = 0
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
    return deleted_runs, deleted_threads


def _assert_rounds(
    c: Checker,
    summaries: list[dict[str, Any]],
    run_ids: list[str],
    runs: list[dict[str, Any]],
    *,
    is_baseline: bool,
) -> None:
    """对成功返回的轮次做结构 / 稳定性 / 准确度（基准样本）/ 审计 / 安全断言。

    ``runs`` 由 main 在**清理之前**抓取（finally 会删掉本次 agent_runs，之后不可再查）。
    """

    # ---- 结构与口径 ----
    c.check(
        all(item.get("data_source") == "screenshot" for item in summaries),
        "每轮 data_source 恒为 screenshot",
    )
    c.check(
        all(
            isinstance(item.get("metrics"), dict)
            and set(item["metrics"]) == set(METRIC_FIELDS)
            for item in summaries
        ),
        "每轮 metrics 恰为六字段口径（不多不少）",
    )
    c.check(
        all(
            all(
                value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)
                for value in item["metrics"].values()
            )
            for item in summaries
        ),
        "每轮指标值均为「非负整数或 null」（绝不臆造负数 / 小数）",
    )
    c.check(
        all(
            any(value is not None for value in item["metrics"].values())
            for item in summaries
        ),
        "每轮至少识别出 1 个指标（视觉链路确实读到数字）",
    )

    # ---- JSON 连续稳定性 ----
    attempts = [item.get("parse_attempts") for item in summaries]
    db_attempts = [item["json_parse_attempts"] for item in runs]
    c.check(
        bool(attempts)
        and all(value == 1 for value in attempts)
        and bool(db_attempts)
        and all(value == 1 for value in db_attempts),
        f"连续 {len(summaries)} 轮 parse_attempts 恒为 1（JSON 输出稳定）",
        f"响应={attempts} 审计={db_attempts}",
    )

    # ---- 准确度：基准样本逐轮自动对照（自定义图无参考值，留人工核对）----
    if is_baseline:
        for index, item in enumerate(summaries, start=1):
            actual = item["metrics"]
            mismatch = {
                field: {"识别": actual.get(field), "参考": SPIKE_REFERENCE[field]}
                for field in METRIC_FIELDS
                if actual.get(field) != SPIKE_REFERENCE[field]
            }
            c.check(
                not mismatch,
                f"第 {index} 轮：六指标与基准参考值完全一致（6/6）",
                f"偏差：{mismatch}" if mismatch else "",
            )

    # ---- 审计回填 ----
    c.check(
        len(runs) == len(run_ids),
        "agent_runs 行数与识别轮数一致",
        f"{len(runs)}/{len(run_ids)}",
    )
    c.check(
        bool(runs) and all(item["status"] == "success" for item in runs),
        "每轮 run 状态均为 success",
        f"{[item['status'] for item in runs]}",
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
        "llm_raw 已归档且不含密钥前缀",
    )
    c.check(
        bool(runs)
        and all(
            isinstance((item["outputs"] or {}).get("metrics"), dict)
            and set((item["outputs"] or {}).get("metrics") or {}) == set(METRIC_FIELDS)
            and isinstance((item["outputs"] or {}).get("missing_fields"), list)
            and (item["outputs"] or {}).get("data_source") == "screenshot"
            for item in runs
        ),
        "agent_runs.outputs 已回填识别摘要（六指标 / missing / data_source）",
    )

    # ---- 安全：响应不含图片数据 / 密钥 / 绝对路径 ----
    blobs = [json.dumps(item, ensure_ascii=False, default=str) for item in summaries]
    c.check(
        all("base64" not in blob and "data:image" not in blob and "llm_raw" not in blob for blob in blobs),
        "识别响应不含 base64 / data URL / llm_raw",
    )
    c.check(
        all('D:\\\\' not in blob and "/home/" not in blob and "file://" not in blob for blob in blobs),
        "识别响应不含绝对路径 / 文件 URI",
    )

    # ---- token 明细（按 token 记，不内置价格）----
    print("-" * 78)
    for index, item in enumerate(runs, start=1):
        print(
            f"  token 第 {index} 轮：prompt={item['prompt_tokens']} "
            f"completion={item['completion_tokens']} total={item['total_tokens']}"
        )
    total_tokens = sum((item["total_tokens"] or 0) for item in runs)
    c.check(total_tokens > 0, "agent_runs 记录到真实 token 用量", f"total={total_tokens}")
    print(
        f"[INFO] 本次共消耗 total_tokens = {total_tokens}"
        f"（成本 = token × 你的实际单价，本仓库不内置价格表）"
    )


def main() -> int:
    args = _parse_args()
    refusal = _refuse_if_not_configured()
    if refusal is not None:
        return refusal

    rounds_to_run = max(1, args.rounds)
    loaded = _load_image(args.image)
    if loaded is None:
        return 1
    image_bytes, image_mime, image_name, image_path = loaded
    digest = hashlib.sha256(image_bytes).hexdigest()
    is_baseline = digest == SPIKE_SAMPLE_SHA256

    init_checkpointer()
    before_threads = _current_threads()
    before_runs = _current_run_ids()

    c = Checker()
    print("截图识别（视觉）真实 LLM 检查 —— 将向 DeepSeek 发起真实请求并消耗 token")
    print(
        f"vision_model={llm_factory.resolve_vision_model_name()} "
        f"| base_url={settings.LLM_BASE_URL}"
    )
    print(
        f"prompt_version={settings.METRIC_PROMPT_VERSION} "
        f"| 六指标：{' / '.join(METRIC_FIELDS)}"
    )
    baseline_note = "启用（基准示例图自动对照）" if is_baseline else "关闭（自定义图，准确度人工核对）"
    print(
        f"样本：{image_path}（{len(image_bytes) / 1024:.1f} KB，sha256={digest[:12]}…）"
        f"｜基准对照：{baseline_note}"
    )
    print(f"将连续运行 {rounds_to_run} 轮")
    print("-" * 78)

    summaries: list[dict[str, Any]] = []
    run_ids: list[str] = []
    fetched_runs: list[dict[str, Any]] = []
    fatal: str | None = None
    new_runs: set[str] = set()
    new_threads: set[str] = set()
    deleted_runs = deleted_threads = 0

    try:
        for index in range(1, rounds_to_run + 1):
            try:
                summary = recognize_metric_screenshot(
                    image_bytes,
                    image_mime,
                    image_name=image_name,
                )
            except (AppError, AgentConfigError) as exc:
                fatal = f"{type(exc).__name__}: {exc}"
                print(f"  [中断] 第 {index} 轮识别失败：{fatal}")
                break
            summaries.append(summary)
            run_ids.append(str(summary.get("run_id")))
            _print_round(index, summary)
        # 审计数据必须在清理**之前**抓取（finally 会删掉本次 agent_runs，之后不可再查）
        fetched_runs = _fetch_runs([rid for rid in run_ids if rid])
    finally:
        new_runs = _current_run_ids() - before_runs
        new_threads = _current_threads() - before_threads
        deleted_runs, deleted_threads = _cleanup(new_runs, new_threads)
        close_checkpointer()

    print("-" * 78)
    print(
        f"[INFO] 已清理：agent_runs={deleted_runs} 行 / 检查点行={deleted_threads}"
        f"（线程 {len(new_threads)} 个）—— 识别链路零落库，无业务数据需还原"
    )

    # ---- 轮次与结构断言 ----
    if fatal is None:
        c.check(
            len(summaries) == rounds_to_run,
            f"连续 {rounds_to_run} 轮识别全部成功返回",
            f"成功 {len(summaries)} / {rounds_to_run}",
        )
    else:
        c.check(False, f"连续 {rounds_to_run} 轮识别全部成功（中断）", fatal)

    if summaries:
        _assert_rounds(c, summaries, run_ids, fetched_runs, is_baseline=is_baseline)

    # ---- 清理复查（差集口径）----
    c.check(
        (_current_run_ids() - before_runs) == set(),
        "本次识别的 agent_runs 已按差集物理清理",
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
            print("[提示] 识别在运行中失败，常见原因与处理：")
            print("  · 「模型不支持图片输入」→ 到 DeepSeek 控制台换【支持图片输入】的模型，")
            print(f"    填进 backend/.env 的 LLM_VISION_MODEL；视觉文档：{llm_factory.VISION_DOC_URL}")
            print("  · 「JSON 连续解析失败」→ 换更稳定的视觉模型 / 稍后重试；")
            print("  · 网络抖动 / 429 → 稍后重试。")
        return 1
    print("全部 PASS ✔（真实视觉 LLM）")
    if is_baseline:
        print("⚠️ 基准样本已自动对照；视觉转录的绝对质量仍建议人工抽查一眼（对照截图与上方原文）。")
    else:
        print("⚠️ 自定义样本无参考值：请人工对照截图核对上方每轮六指标数值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
