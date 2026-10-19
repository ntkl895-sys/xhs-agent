# -*- coding: utf-8 -*-
"""第 27 步 · 经验提炼器（reflection，离线、默认零 token）。

把采集到的失败/兜底/越权/被拒信号，归因成**结构化候选经验**。类型限定五种：

| 类型 | 含义 | 后续动作 |
| --- | --- | --- |
| `bug_report` | 代码/封装缺陷（有确定性的错误行为） | 走 `evolve_mark.py` 标 xfail，修复后 `evolve_close.py` 转正 |
| `prompt_patch` | 提示词增补（不改代码，运行时注入做 A/B） | 走 `evolve_ab.py` 对照，通过才允许人审并入 prompts.py |
| `few_shot` | 给某类输入补示例 | 同上（A/B） |
| `rule` | 规则/词表/用例覆盖策略 | 人审后改规则或用例，A/B 或全量回归验证 |
| `hit_pattern` | 内容规律（爆款结构） | **只生成不应用**，等真实笔记数据到位（阶段 4） |

**默认纯规则、零 token**：只有 `--use-llm` 才调模型做语义归纳，且必须先 `--estimate-only`
看调用次数与 token 区间；模型失败自动降级回规则文案（`llm_fallback=true`），不阻断。

产出只落工作目录 `reflect/reflect-<ts>.json`，状态 `pending`，**绝不自动生效**。

用法（项目根目录）：

    :: 纯规则（零 token）
    ... evolve_reflect.py --harvest D:\\项目1\\evolution\\harvest\\harvest-xxxx.json

    :: 估算 LLM 归纳成本（不发请求）
    ... evolve_reflect.py --harvest <file> --use-llm --estimate-only

    :: 真实归纳（需 --yes；建议先跑 --estimate-only）
    ... evolve_reflect.py --harvest <file> --use-llm --yes

退出码：0 完成；2 未授权/未配置（零请求）；3 参数或环境问题。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402

#: 归因规则的最小重复次数（单次波动不立候选，避免「凭一次失败改系统」）
DEFAULT_MIN_COUNT = 2

#: 单条 LLM 归纳的 token 估算（粗估，非承诺）
EST_INPUT_TOKENS = 900
EST_OUTPUT_TOKENS = 260

_TYPES = ("bug_report", "prompt_patch", "few_shot", "rule", "hit_pattern")


def _balance(raw: str) -> tuple[int, int, bool]:
    depth_obj = depth_arr = 0
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth_obj += 1
        elif ch == "}":
            depth_obj -= 1
        elif ch == "[":
            depth_arr += 1
        elif ch == "]":
            depth_arr -= 1
    return depth_obj, depth_arr, in_string


def _looks_truncated(raw: str) -> bool:
    depth_obj, depth_arr, open_quote = _balance(raw)
    return depth_obj > 0 or depth_arr > 0 or open_quote


def _confidence(count: int) -> str:
    if count >= 3:
        return "high"
    if count == 2:
        return "medium"
    return "low"


def _candidate(
    *,
    index: int,
    ctype: str,
    signals: list[dict],
    root_cause: str,
    suggestion: str,
    impact: str,
    target_testset: str | None,
    expected_source: str,
    extra: dict | None = None,
) -> dict:
    return {
        "candidate_id": f"refl_{index:04d}",
        "type": ctype,
        "state": "pending",
        "source_run_ids": sorted({str(item.get("source_id")) for item in signals}),
        "signal_kinds": sorted({str(item.get("kind")) for item in signals}),
        "evidence": {
            "count": len(signals),
            "signal_ids": [item.get("signal_id") for item in signals][:20],
            "samples": [
                {
                    "signal_id": item.get("signal_id"),
                    "run_type": item.get("run_type"),
                    "raw_chars": item.get("raw_chars"),
                    "evidence": item.get("evidence"),
                }
                for item in signals[:5]
            ],
            **(extra or {}),
        },
        "root_cause_hypothesis": root_cause,
        "suggestion": suggestion,
        "impact": impact,
        "confidence": _confidence(len(signals)),
        "target_testset": target_testset,
        "expected_source": expected_source,
        "created_at": ec.now_iso(),
    }


def build_candidates(
    harvest: dict,
    *,
    min_count: int = DEFAULT_MIN_COUNT,
    topic_tool_result: dict | None = None,
) -> dict:
    signals: list[dict] = list(harvest.get("signals") or [])
    by_kind: dict[str, list[dict]] = {}
    for signal in signals:
        by_kind.setdefault(str(signal.get("kind")), []).append(signal)

    candidates: list[dict] = []
    skipped: list[dict] = []

    def _next() -> int:
        return len(candidates) + 1

    # 规则 1：三层兜底全失败 → 补 failed 用例（覆盖缺口，不是改代码）
    unrecoverable = by_kind.get("json_unrecoverable", [])
    if len(unrecoverable) >= min_count:
        candidates.append(
            _candidate(
                index=_next(),
                ctype="rule",
                signals=unrecoverable,
                root_cause="模型输出形态超出三层兜底覆盖范围，当前只能拒绝",
                suggestion="把这些形态补成 json_robustness 的 failed 用例（期望正确拒绝），先锁住行为再谈改进",
                impact="测试集覆盖（不改变线上行为）",
                target_testset="json_robustness",
                expected_source="人工确认「该形态确实无法可靠恢复」后标注",
            )
        )
    elif unrecoverable:
        skipped.append({"kind": "json_unrecoverable", "count": len(unrecoverable),
                        "reason": f"样本 {len(unrecoverable)} < 最小重复次数 {min_count}"})

    # 规则 2：截断形态反复出现 → prompt 补丁（长字段后置 / 控制长度）
    truncated = [
        signal
        for signal in signals
        if str(signal.get("kind")).startswith("json_")
        and (signal.get("raw") or "").strip()
        and _looks_truncated(str(signal.get("raw") or ""))
    ]
    if len(truncated) >= min_count:
        candidates.append(
            _candidate(
                index=_next(),
                ctype="prompt_patch",
                signals=truncated,
                root_cause="输出在闭合前被截断（括号/引号未闭合），兜底也扫不出完整片段",
                suggestion=(
                    "在系统提示中要求：把长文本字段放在 JSON 末尾、先输出短字段；"
                    "同时检查 max_tokens 是否偏小"
                ),
                impact="所有走 invoke_json 的图（需 A/B 对照 JSON 一次成功率）",
                target_testset="json_robustness",
                expected_source="A/B：同一冻结集 baseline vs candidate 的 JSON 一次成功率",
                extra={"truncated_examples": [str(s.get("raw"))[:80] for s in truncated[:3]]},
            )
        )

    # 规则 3：平衡扫描救回反复出现 → few_shot（该形状模型本来就能说对，只是被废话包住）
    rescued = [
        signal
        for signal in by_kind.get("json_fallback", [])
        if str((signal.get("evidence") or {}).get("source") or "") not in ("", "原文")
    ]
    if len(rescued) >= min_count:
        candidates.append(
            _candidate(
                index=_next(),
                ctype="few_shot",
                signals=rescued,
                root_cause="模型习惯在 JSON 前后加解释/围栏，靠兜底才解析成功",
                suggestion="在该图提示词里补一个「只输出 JSON、无围栏无解释」的 few-shot 示例",
                impact="该图的 raw 合法率（需 A/B）",
                target_testset="json_robustness",
                expected_source="A/B：raw 原文直解率",
                extra={"sources": sorted({str((s.get('evidence') or {}).get('source')) for s in rescued})},
            )
        )

    # 规则 4：重问后才成功 → few_shot
    retried = by_kind.get("json_retry", [])
    if len(retried) >= min_count:
        candidates.append(
            _candidate(
                index=_next(),
                ctype="few_shot",
                signals=retried,
                root_cause="首次输出不合法，纠正后才成功——说明提示词没把格式约束说死",
                suggestion="补一个该图的 few-shot 示例，减少第一次就失败的概率",
                impact="attempts 分布与 token 成本（需 A/B）",
                target_testset="json_robustness",
                expected_source="A/B：attempts==1 的比例",
            )
        )

    # 规则 5：白名单外工具请求 → prompt 补丁
    hallucinated = by_kind.get("tool_hallucination", [])
    if hallucinated:
        candidates.append(
            _candidate(
                index=_next(),
                ctype="prompt_patch",
                signals=hallucinated,
                root_cause="模型请求了未提供的工具名（白名单外）",
                suggestion="强化工具指令：只能从给定列表选择；没有合适工具就不调用",
                impact="工具模式的越权请求率（需 A/B）",
                target_testset="topic_tool",
                expected_source="A/B：幻觉工具率 / 工具选择精确率",
            )
        )

    # 规则 6：同一工具反复报错 → bug_report
    tool_errors = by_kind.get("tool_error", [])
    grouped: dict[str, list[dict]] = {}
    for signal in tool_errors:
        tool = str((signal.get("evidence") or {}).get("tool") or "unknown")
        grouped.setdefault(tool, []).append(signal)
    for tool, items in grouped.items():
        if len(items) < min_count:
            continue
        candidates.append(
            _candidate(
                index=_next(),
                ctype="bug_report",
                signals=items,
                root_cause=f"工具 {tool} 反复执行失败，疑似封装/入参校验缺陷",
                suggestion=f"复现 {tool} 的失败入参并补单测；修复前先用 known_failure 追踪",
                impact="工具模式可用性",
                target_testset="topic_tool",
                expected_source="实测失败入参（确定性复现）",
            )
        )

    # 规则 7：同一违禁词反复命中 → 规则/词表候选
    compliance = by_kind.get("compliance_hit", [])
    word_groups: dict[str, list[dict]] = {}
    for signal in compliance:
        for hit in (signal.get("evidence") or {}).get("hits") or []:
            word = str(hit.get("word") or "")
            if word:
                word_groups.setdefault(word, []).append(signal)
    for word, items in word_groups.items():
        if len(items) < min_count:
            continue
        candidates.append(
            _candidate(
                index=_next(),
                ctype="rule",
                signals=items,
                root_cause=f"「{word}」多次命中，需判断是真违规还是语境误伤",
                suggestion="人工复核命中上下文；真违规保持词表，误伤则考虑上下文规则（不要直接删词）",
                impact="合规误伤率 / 漏检率（须同时看两侧，不能只看一侧）",
                target_testset="compliance",
                expected_source="compliance 冻结集混淆矩阵（n=128）",
            )
        )

    # 规则 8：人工废弃选题 → hit_pattern（只生成，不应用）
    discarded = by_kind.get("topic_discarded", [])
    if len(discarded) >= 3:
        candidates.append(
            _candidate(
                index=_next(),
                ctype="hit_pattern",
                signals=discarded,
                root_cause="人工废弃的选题存在共性（需要语义归纳，规则层只能标记）",
                suggestion="人工归纳被废弃原因，形成候选 hit_pattern；**数据到位前只记录不应用**",
                impact="内容质量（本轮不生效）",
                target_testset=None,
                expected_source="人工标注 + 真实笔记表现（当前无数据）",
            )
        )

    # 规则 9：工具调用过度（来自既有评测结果，非 agent_runs）
    if topic_tool_result:
        summary = (topic_tool_result.get("summary") or {}).get("tool") or {}
        precision = summary.get("precision_micro")
        if isinstance(precision, (int, float)) and precision < 0.6:
            candidates.append(
                {
                    "candidate_id": f"refl_{_next():04d}",
                    "type": "prompt_patch",
                    "state": "pending",
                    "source_run_ids": [],
                    "signal_kinds": ["tool_over_calling"],
                    "evidence": {
                        "source": "docs/benchmark/results/topic_tool_calling.json",
                        "precision_micro": precision,
                        "recall_micro": summary.get("recall_micro"),
                        "avg_rounds": summary.get("avg_rounds"),
                        "n": summary.get("n"),
                        "tokens": summary.get("total_tokens"),
                    },
                    "root_cause_hypothesis": "模型倾向把可用工具全调一遍，而不是按需选择",
                    "suggestion": "在工具指令中要求「只调用真正需要的工具；能凭已有信息判断就不调用」，并给反例",
                    "impact": "工具选择精确率与 token 成本（需 A/B，n=12）",
                    "confidence": "medium",
                    "target_testset": "topic_tool",
                    "expected_source": "A/B：precision_micro 提升且 recall 不降",
                    "created_at": ec.now_iso(),
                }
            )

    by_type: dict[str, int] = {}
    for item in candidates:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1

    return {
        "reflected_at": ec.now_iso(),
        "signals_total": len(signals),
        "min_count": min_count,
        "candidates_total": len(candidates),
        "by_type": dict(sorted(by_type.items())),
        "candidates": candidates,
        "skipped": skipped,
        "llm_used": False,
    }


# ---------------------------------------------------------------------------
# 可选：LLM 语义归纳（默认关闭；失败降级回规则）
# ---------------------------------------------------------------------------
def _estimate(candidates: list[dict]) -> dict:
    calls = len(candidates)
    return {
        "calls": calls,
        "est_input_tokens": calls * EST_INPUT_TOKENS,
        "est_output_tokens": calls * EST_OUTPUT_TOKENS,
        "est_total_tokens": calls * (EST_INPUT_TOKENS + EST_OUTPUT_TOKENS),
        "formula": "输入 token × 输入单价 + 输出 token × 输出单价（本仓库不内置价格表）",
    }


def _enrich_with_llm(candidate: dict, model: Any) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

    from app.agent.parsing.json_repair import invoke_json  # noqa: PLC0415

    system = SystemMessage(
        content=(
            "你是 AI 应用可靠性工程师。给定一条失败样本的归类证据，输出 JSON："
            '{"root_cause":"一句话根因","suggestion":"一条可执行改动建议",'
            '"confidence":"low|medium|high"}。只输出 JSON，不要解释。'
        )
    )
    human = HumanMessage(content=json.dumps(candidate.get("evidence") or {}, ensure_ascii=False)[:2500])
    result = invoke_json(model, [system, human], want_json=True)
    payload = result.payload if isinstance(result.payload, dict) else {}
    enriched = dict(candidate)
    enriched["root_cause_hypothesis"] = str(
        payload.get("root_cause") or candidate["root_cause_hypothesis"]
    )[:400]
    enriched["suggestion"] = str(payload.get("suggestion") or candidate["suggestion"])[:600]
    if payload.get("confidence") in ("low", "medium", "high"):
        enriched["confidence"] = payload["confidence"]
    enriched["llm_used"] = True
    enriched["llm_attempts"] = result.attempts
    enriched["llm_tokens"] = result.usage.total_tokens if result.usage else None
    return enriched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="经验提炼器（默认零 token）")
    parser.add_argument("--harvest", type=Path, required=True)
    parser.add_argument("--topic-tool-result", type=Path, default=None)
    parser.add_argument("--min-count", type=int, default=DEFAULT_MIN_COUNT)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    if not args.harvest.is_file():
        print(f"[参数错误] 找不到采集清单：{args.harvest}")
        return ec.EXIT_ENV
    harvest = ec.read_json(args.harvest)
    topic_tool = ec.read_json(args.topic_tool_result) if args.topic_tool_result else None

    payload = build_candidates(harvest, min_count=max(1, args.min_count), topic_tool_result=topic_tool)

    print("=" * 78)
    print("  经验提炼器（reflection）" + ("（LLM 归纳）" if args.use_llm else "（纯规则，零 token）"))
    print("=" * 78)
    print(f"信号 {payload['signals_total']} 条 → 候选 {payload['candidates_total']} 条：{payload['by_type']}")
    for item in payload["candidates"]:
        print(f"  {item['candidate_id']}  {item['type']:<12}{item['confidence']:<8}{item['suggestion'][:44]}")
    for item in payload["skipped"]:
        print(f"  [跳过] {item['kind']} × {item['count']}：{item['reason']}")

    if args.use_llm:
        estimate = _estimate(payload["candidates"])
        print("-" * 78)
        print(f"LLM 归纳计划：{estimate['calls']} 次调用，预计 {estimate['est_total_tokens']} token"
              f"（{EST_INPUT_TOKENS} in + {EST_OUTPUT_TOKENS} out / 条，粗估非承诺）")
        if args.estimate_only:
            print("仅估算，未发出任何请求。")
            return ec.EXIT_OK
        if not args.yes:
            print("[确认] 真实归纳需 --yes（未发出请求，退出码 2）。")
            return ec.EXIT_GATED
        try:
            ec.add_backend_to_path()
            from app.agent.llm.factory import get_chat_model  # noqa: PLC0415

            model = get_chat_model(temperature=0.0, want_json=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[闸门] 模型不可用（{type(exc).__name__}: {exc}）——降级为纯规则结果。")
        else:
            enriched: list[dict] = []
            for item in payload["candidates"]:
                try:
                    enriched.append(_enrich_with_llm(item, model))
                except Exception as exc:  # noqa: BLE001 —— 单条失败不拖垮整批
                    item["llm_fallback"] = f"{type(exc).__name__}: {exc}"
                    enriched.append(item)
            payload["candidates"] = enriched
            payload["llm_used"] = any(item.get("llm_used") for item in enriched)
            payload["llm_estimate"] = estimate

    workdir = args.workdir or ec.default_workdir()
    out_path = args.out or (ec.ensure_dir(workdir / "reflect") / f"reflect-{_stamp()}.json")
    ec.write_json(out_path, payload)
    print("-" * 78)
    print(f"候选经验：{out_path}")
    print("下一步：evolve_store.py --ingest <file>，再逐条 --transition 推进状态（不会自动生效）。")
    return ec.EXIT_OK


def _stamp() -> str:
    from datetime import datetime  # noqa: PLC0415

    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


if __name__ == "__main__":
    sys.exit(main())
