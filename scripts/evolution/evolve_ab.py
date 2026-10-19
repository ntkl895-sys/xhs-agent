# -*- coding: utf-8 -*-
"""第 27 步 · 变更对照评测门禁（A/B gate）。

目的：给 `prompt_patch` / `few_shot` 这类**不改代码**的候选一个可量化的准入条件——
同一冻结集、同一 n，跑 baseline 与 candidate，看关键指标是否**确实提升且不退化**。

注入方式（不动生产代码）：

```
import app.agent.<target>.graph as g
original = g.build_messages          # 该模块 import 时就绑定了函数对象
g.build_messages = wrapper            # 只在本次进程内替换；退出即失效
```

注意替换的是 **graph 模块上的引用**——替换 `prompts.build_messages` 无效，
因为 graph 在 import 时已经把函数对象绑进了自己的命名空间。

两个目标：

| target | 说明 | token |
| --- | --- | --- |
| `synthetic` | 确定性假模型，验证「注入 → 双跑 → diff → 判定」机制 | **0** |
| `topic_tool` | 真实 LLM，12 个冻结场景（tool + baseline 两模式） | 约 2×68k |

判定规则（预置，不按结果调）：

- `synthetic`：`raw_direct_rate` 提升 ≥ 0.05 且 `json_first_try_rate` 不降 → approved；
- `topic_tool`：`precision_micro` 提升 ≥ 0.05、`recall_micro` 不降、`json_first_try_rate` 不降 → approved；
- 其余（无变化 / 退化）→ rejected，**负结果照样留档**。

用法：

    :: 机制验证（零 token）
    ... evolve_ab.py --candidate ab-demo.json --target synthetic

    :: 真实对照（需 --yes，约 137k token）
    ... evolve_ab.py --candidate ab-demo.json --target topic_tool --yes

退出码：0 approved；2 rejected（已留档）；3 参数/环境问题。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402

#: 预置门槛（写死，执行后不允许按结果改）
MIN_GAIN = 0.05


# ---------------------------------------------------------------------------
# patch 注入
# ---------------------------------------------------------------------------
def apply_patch(target: str, text: str) -> Any:
    """把补丁文本追加到该图 system 消息末尾；返回原函数用于还原。"""
    ec.add_backend_to_path()
    from langchain_core.messages import SystemMessage  # noqa: PLC0415

    module = importlib.import_module(f"app.agent.{target}.graph")
    original = getattr(module, "build_messages")

    def patched(**kwargs):
        messages = original(**kwargs)
        if messages and isinstance(messages[0], SystemMessage):
            messages[0] = SystemMessage(content=f"{messages[0].content}\n\n{text}")
        return messages

    setattr(module, "build_messages", patched)
    return original


def restore_patch(target: str, original: Any) -> None:
    module = importlib.import_module(f"app.agent.{target}.graph")
    setattr(module, "build_messages", original)


# ---------------------------------------------------------------------------
# synthetic 目标（确定性假模型，零 token）
# ---------------------------------------------------------------------------
SYNTH_N = 8


def _synth_run(patch_text: str) -> dict:
    """用真实 build_messages 渲染 8 条消息，再由确定性假模型产出输出并计入。**

    假模型行为（写死，用来验证门禁而不是验证模型）：
    - 补丁含 ``ONLY_JSON`` → 输出纯 JSON（原文直解）
    - 补丁含 ``TRUNCATE``  → 输出截断 JSON（解析失败）
    - 其余（无补丁 / 无关补丁）→ 输出 ```json 围栏（需兜底才解析）
    """
    ec.add_backend_to_path()
    from app.agent.parsing.json_repair import extract_json_with_source  # noqa: PLC0415
    from app.agent.topics.graph import build_messages  # noqa: PLC0415

    direct = 0
    parsed = 0
    for index in range(SYNTH_N):
        messages = build_messages(
            persona_snapshot={
                "name": f"测试人设{index}",
                "positioning": "通勤穿搭",
                "target_audience": "上班族",
                "tone": "克制",
                "content_pillars": ["穿搭", "效率"],
            },
            hotspot_snapshot={"source": "weibo", "title": f"热点{index}", "hot_score": 1000 + index},
            per_hotspot=3,
            title_max_len=20,
            min_tags=3,
            max_tags=6,
        )
        system = str(messages[0].content)
        payload = '{"topics":[{"title":"x","angle":"y"}]}'
        if "ONLY_JSON" in system:
            output = payload
        elif "TRUNCATE" in system:
            output = payload[: len(payload) // 2]
        else:
            output = f"```json\n{payload}\n```"
        value, source = extract_json_with_source(output)
        parsed += 1 if value is not None else 0
        direct += 1 if source == "原文" else 0
    return {
        "n": SYNTH_N,
        "raw_direct_rate": round(direct / SYNTH_N, 4),
        "json_first_try_rate": round(parsed / SYNTH_N, 4),
    }


def run_synthetic(patch_text: str) -> tuple[dict, dict]:
    target = "topics"
    original = apply_patch(target, patch_text)
    try:
        candidate = _synth_run(patch_text)
    finally:
        restore_patch(target, original)
    baseline = _synth_run("")
    return baseline, candidate


# ---------------------------------------------------------------------------
# topic_tool 目标（真实 LLM）
# ---------------------------------------------------------------------------
def run_topic_tool(patch_text: str | None) -> dict:
    """在进程内跑一次 12 场景评测；patch_text 为 None 表示基线。"""
    bench_dir = ec.ROOT / "scripts" / "benchmark"
    if str(bench_dir) not in sys.path:
        sys.path.insert(0, str(bench_dir))
    import bench_topic_tool_calling as bench  # noqa: PLC0415

    result_path = ec.ROOT / "docs" / "benchmark" / "results" / "topic_tool_calling.json"
    original_bytes = result_path.read_bytes() if result_path.exists() else None

    target = "topics"
    original = apply_patch(target, patch_text) if patch_text else None
    argv_backup = sys.argv[:]
    try:
        sys.argv = ["bench_topic_tool_calling.py", "--yes"]
        code = bench.main()
    finally:
        sys.argv = argv_backup
        if original is not None:
            restore_patch(target, original)

    payload = ec.read_json(result_path) if result_path.exists() else {}
    if original_bytes is not None:
        result_path.write_bytes(original_bytes)  # 还原正式结果文件（A/B 不污染仓库）
    tool_summary = (payload.get("summary") or {}).get("tool") or {}
    return {
        "rc": code,
        "n": tool_summary.get("n"),
        "precision_micro": tool_summary.get("precision_micro"),
        "recall_micro": tool_summary.get("recall_micro"),
        "json_first_try_rate": tool_summary.get("json_first_try_rate"),
        "tokens": tool_summary.get("total_tokens"),
    }


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def _delta(base: Any, cand: Any) -> float | None:
    if isinstance(base, (int, float)) and isinstance(cand, (int, float)):
        return round(float(cand) - float(base), 4)
    return None


def decide(target: str, baseline: dict, candidate: dict) -> dict:
    if target == "synthetic":
        gain = _delta(baseline.get("raw_direct_rate"), candidate.get("raw_direct_rate"))
        side = _delta(baseline.get("json_first_try_rate"), candidate.get("json_first_try_rate"))
        approved = bool(gain is not None and gain >= MIN_GAIN and (side is None or side >= 0))
        reason = (
            f"raw_direct_rate {baseline.get('raw_direct_rate')} → {candidate.get('raw_direct_rate')}"
            f"（Δ={gain}，门槛 ≥{MIN_GAIN}）；json_first_try_rate Δ={side}"
        )
        secondaries = [side]
    else:
        gain = _delta(baseline.get("precision_micro"), candidate.get("precision_micro"))
        recall_delta = _delta(baseline.get("recall_micro"), candidate.get("recall_micro"))
        parse_delta = _delta(baseline.get("json_first_try_rate"), candidate.get("json_first_try_rate"))
        approved = bool(
            gain is not None
            and gain >= MIN_GAIN
            and (recall_delta is None or recall_delta >= 0)
            and (parse_delta is None or parse_delta >= 0)
        )
        reason = (
            f"precision {baseline.get('precision_micro')} → {candidate.get('precision_micro')}"
            f"（Δ={gain}）；recall Δ={recall_delta}；JSON 一次 Δ={parse_delta}"
        )
        secondaries = [recall_delta, parse_delta]

    degraded_side = [value for value in secondaries if value is not None and value < 0]
    if gain is None or (gain == 0 and not degraded_side):
        verdict = "no_change"
    elif (gain is not None and gain < 0) or degraded_side:
        verdict = "degraded"
    else:
        verdict = "improved"
    return {
        "approved": approved,
        "verdict": verdict,
        "delta": {"primary": gain, "secondaries": secondaries},
        "reason": reason,
        "min_gain": MIN_GAIN,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B 对照门禁（prompt_patch / few_shot）")
    parser.add_argument("--candidate", type=Path, required=True, help="候选 JSON（含 patch.target / patch.text）")
    parser.add_argument("--target", required=True, choices=("synthetic", "topic_tool"))
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--yes", action="store_true", help="topic_tool 目标必须显式确认（真实计费）")
    args = parser.parse_args(argv)

    if not args.candidate.is_file():
        print(f"[参数错误] 找不到候选文件：{args.candidate}")
        return ec.EXIT_ENV
    candidate_payload = ec.read_json(args.candidate)
    patch = candidate_payload.get("patch") or {}
    patch_text = str(patch.get("text") or "")
    patch_target = str(patch.get("target") or "topics")
    if not patch_text.strip():
        print("[参数错误] 候选缺少 patch.text")
        return ec.EXIT_ENV

    print("=" * 78)
    print(f"  A/B 对照门禁 · target={args.target} · patch→{patch_target}")
    print("=" * 78)
    print(f"补丁：{patch_text[:120]}{'…' if len(patch_text) > 120 else ''}")

    if args.target == "synthetic":
        print(f"[计划] 确定性假模型 × {SYNTH_N} 条，零 token；跑 baseline 与 candidate 两轮。")
        baseline, candidate = run_synthetic(patch_text)
    else:
        if not args.yes:
            print("[确认] topic_tool 目标会发起真实调用（约 2×68k token），需 --yes。未发出请求。")
            return ec.EXIT_GATED
        print("[计划] 12 场景 × 2 模式 × 2 轮（baseline / candidate）——真实计费，约 20 分钟。")
        baseline = run_topic_tool(None)
        candidate = run_tool_topic_safe(patch_text)

    decision = decide(args.target, baseline, candidate)
    report = {
        "ran_at": ec.now_iso(),
        "target": args.target,
        "patch": {"target": patch_target, "text": patch_text,
                  "sha256": hashlib.sha256(patch_text.encode("utf-8")).hexdigest()},
        "candidate_ref": str(args.candidate),
        "baseline": baseline,
        "candidate": candidate,
        "decision": decision,
        "n": candidate.get("n") or baseline.get("n"),
    }

    workdir = args.workdir or ec.default_workdir()
    out_path = args.out or (ec.ensure_dir(workdir / "ab") / f"ab-{_stamp()}.json")
    ec.write_json(out_path, report)

    print("-" * 78)
    print(f"baseline ：{json.dumps(baseline, ensure_ascii=False)}")
    print(f"candidate：{json.dumps(candidate, ensure_ascii=False)}")
    print(f"判定：{decision['verdict']} → {'approved' if decision['approved'] else 'rejected'}"
          f"（{decision['reason']}）")
    print(f"报告：{out_path}")
    if not decision["approved"]:
        print("负结果照样留档：不合并、不改生产代码。")
        return ec.EXIT_GATED
    print("下一步：人工复核后只允许并入业务 prompts.py（禁止动 harness）。")
    return ec.EXIT_OK


def run_tool_topic_safe(patch_text: str) -> dict:
    return run_topic_tool(patch_text)


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


if __name__ == "__main__":
    sys.exit(main())
