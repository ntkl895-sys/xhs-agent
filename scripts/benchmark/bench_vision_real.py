# -*- coding: utf-8 -*-
"""度量专项 · 阶段二.2b：视觉识别（真实截图档，由用户补数）。

本脚本**不内置任何真实截图**，只提供：

1. 空白清单模板（``docs/benchmark/fixtures/vision_real/manifest.json``）与填写说明；
2. 计分脚本：用户把 k 张真实截图放入同目录并按模板填好 ``expected``（六指标，
   按截图上的展示形态填写，例如 ``"1.2万"`` / ``"3286"``）后运行本脚本，
   逐图调用 ``recognize_metric_screenshot`` 并输出**字段级准确率**（n = k × 6 字段）。

口径：真实截图集由用户提供并标注标准答案；n=k 如实标注，**不得外推**；
未配置 LLM → **退出码 2（零请求）**；模板为空 → 打印待办说明并退出 0（N/A）。

用法（项目根目录）：

    :: ① 初始化模板（离线，零请求；也可直接运行自动创建）
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_vision_real.py --init

    :: ② 补数后真实运行（交互确认；脚本化加 --yes）
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_vision_real.py

退出码：0 完成（含模板为空时的 N/A）；1 失败；2 未配置（零请求）；3 环境问题。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import bench_common as bc

bc.add_backend_to_path()

from sqlalchemy import text  # noqa: E402

from app.agent.checkpoint import close_checkpointer  # noqa: E402
from app.agent.errors import AgentConfigError  # noqa: E402
from app.agent.llm import factory as llm_factory  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

REAL_DIR = bc.FIXTURES_DIR / "vision_real"
MANIFEST_PATH = REAL_DIR / "manifest.json"
METHOD = (
    "scripts/benchmark/bench_vision_real.py：用户提供的真实截图集 + 标准答案逐字段计分"
    "（canonical 口径：去千分位 / 万·w 后缀归一后判等）"
)

METRIC_LABELS = {
    "views": "曝光量",
    "likes": "点赞",
    "collects": "收藏",
    "comments": "评论",
    "shares": "分享",
    "new_followers": "涨粉",
}

TEMPLATE = {
    "purpose": "真实截图集（用户补数）：统计视觉识别字段级准确率（n=k）",
    "instructions": [
        "1) 把 k 张真实「笔记数据」截图放入本目录（docs/benchmark/fixtures/vision_real/）；",
        "2) 在下方 fixtures 数组中逐张登记：name 文件名、path 相对本目录的路径、"
        "expected 六指标标准答案（按截图上的展示形态填写，如 \"1.2万\" / \"3286\"）；",
        "3) 六指标键固定为 views / likes / collects / comments / shares / new_followers；",
        "4) 保存后运行：backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_vision_real.py；",
        "5) 结果写入 docs/benchmark/results/vision_real.json（n=k×6 字段，如实标注，不得外推）。",
    ],
    "six_metrics": list(METRIC_LABELS),
    "fixtures": [
        # 示例（请删除示例后填写真实条目）：
        # {
        #   "name": "real_01.png",
        #   "path": "real_01.png",
        #   "expected": {"views": "1.2万", "likes": "3286", "collects": "1215",
        #                "comments": "187", "shares": "96", "new_followers": "342"}
        # }
    ],
}


def _all_run_ids() -> set[str]:
    with SessionLocal() as db:
        return {str(row[0]) for row in db.execute(text("SELECT id FROM agent_runs")).all()}


def _all_thread_ids() -> set[str]:
    with SessionLocal() as db:
        try:
            rows = db.execute(text("SELECT DISTINCT thread_id FROM langgraph.checkpoints")).all()
        except Exception:  # noqa: BLE001
            return set()
    return {str(row[0]) for row in rows}


def _fetch_runs(run_ids: list[str]) -> list[dict]:
    if not run_ids:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT id, status, total_tokens, json_parse_attempts FROM agent_runs "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": run_ids},
        ).mappings().all()
    return [dict(row) for row in rows]


def ensure_template() -> bool:
    """确保模板存在；返回是否「新创建」。"""
    REAL_DIR.mkdir(parents=True, exist_ok=True)
    if MANIFEST_PATH.exists():
        return False
    bc.write_json(MANIFEST_PATH, TEMPLATE)
    return True


def resolve_image_path(raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return REAL_DIR / raw


def main() -> int:
    parser = argparse.ArgumentParser(description="视觉识别·真实截图档（benchmark 套件）")
    parser.add_argument("--init", action="store_true", help="仅初始化空白模板（离线，零请求）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（脚本化）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("度量专项 · 视觉识别（真实截图档）")

    created = ensure_template()
    if created:
        print(f"[模板] 已创建空白清单：{MANIFEST_PATH.relative_to(bc.ROOT)}")
        for line in TEMPLATE["instructions"]:
            print(f"        {line}")
    if args.init:
        return bc.EXIT_OK

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    fixtures = manifest.get("fixtures") or []

    readiness = llm_factory.describe_llm_readiness()
    print(f"[就绪] {json.dumps(readiness, ensure_ascii=False)}")
    if not readiness.get("ready"):
        print("[闸门] LLM / 视觉未配置，本脚本不发出任何请求（退出码 2）。")
        return bc.EXIT_GATED

    if not fixtures:
        print("[待补数] 真实截图集为空（N/A）——请按模板放入图片并填写 expected 后重跑。")
        print(f"         模板：{MANIFEST_PATH.relative_to(bc.ROOT)}")
        return bc.EXIT_OK

    print(f"计划：{len(fixtures)} 张真实截图 → 最多 {len(fixtures)} 次视觉模型调用（粗估，非承诺）。")
    if not args.yes:
        if not sys.stdin.isatty():
            print("[确认] 非交互终端：请加 --yes 明确授权真实调用。已停止（零请求）。")
            return bc.EXIT_ENV
        try:
            confirmed = input("确认发起真实视觉调用？输入 yes 继续：").strip().lower()
        except EOFError:
            print("[确认] 标准输入不可读（EOF）：视为未确认，已停止（零请求）。")
            return bc.EXIT_ENV
        if confirmed != "yes":
            print("[确认] 未确认，已停止（零请求）。")
            return bc.EXIT_GATED

    from app.agent.metrics.service import recognize_metric_screenshot  # noqa: PLC0415

    before_runs = _all_run_ids()
    before_threads = _all_thread_ids()
    detail: dict = {"images": [], "errors": []}
    metrics: list[dict] = []
    try:
        for entry in fixtures:
            image_path = resolve_image_path(str(entry.get("path") or entry.get("name") or ""))
            if not image_path.exists():
                detail["errors"].append({"image": entry.get("name"), "error": f"文件不存在：{image_path}"})
                print(f"[识别] {entry.get('name')}  失败：文件不存在")
                continue
            image_bytes = image_path.read_bytes()
            round_before = _all_run_ids()
            started = time.perf_counter()
            try:
                summary = recognize_metric_screenshot(
                    image_bytes, "image/png", image_name=image_path.name
                )
                error = None
            except AgentConfigError as exc:
                detail["vision_gated"] = str(exc)
                print(f"[闸门] 视觉模型未配置：{exc}（停止，零请求）")
                break
            except Exception as exc:  # noqa: BLE001
                summary, error = None, f"{type(exc).__name__}: {exc}"
            wall_ms = round((time.perf_counter() - started) * 1000, 1)
            new_runs = _all_run_ids() - round_before
            runs = _fetch_runs(sorted(new_runs))

            if error:
                detail["errors"].append({"image": entry.get("name"), "error": error})
                print(f"[识别] {entry.get('name')}  失败：{error}")
                continue

            recognized = (summary.get("metrics") or {}) if summary else {}
            expected = entry.get("expected") or {}
            fields = {
                key: bc.score_metric_field(expected.get(key), recognized.get(key))
                for key in METRIC_LABELS
            }
            matched = sum(1 for item in fields.values() if item["matched"])
            record = {
                "image": entry.get("name"),
                "matched": matched,
                "fields_total": len(METRIC_LABELS),
                "fields": fields,
                "parse_attempts": summary.get("parse_attempts"),
                "wall_ms": wall_ms,
                "runs": runs,
                "tokens_total": sum(int(row["total_tokens"] or 0) for row in runs),
            }
            detail["images"].append(record)
            print(
                f"[识别] {entry.get('name')}  字段命中 {matched}/{len(METRIC_LABELS)}  "
                f"parse_attempts={record['parse_attempts']}  {wall_ms}ms"
            )
    finally:
        new_runs_all = _all_run_ids() - before_runs
        new_threads = _all_thread_ids() - before_threads
        if new_runs_all:
            with SessionLocal() as db:
                db.execute(text("DELETE FROM agent_runs WHERE id = ANY(:ids)"), {"ids": list(new_runs_all)})
                db.commit()
        if new_threads:
            with SessionLocal() as db:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    try:
                        db.execute(
                            text(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(:tids)"),
                            {"tids": list(new_threads)},
                        )
                    except Exception:  # noqa: BLE001
                        db.rollback()
                db.commit()
        try:
            close_checkpointer()
        except Exception:  # noqa: BLE001
            pass
        detail["cleanup"] = {"deleted_runs": len(new_runs_all), "deleted_threads": len(new_threads)}

    if detail.get("vision_gated") and not detail["images"]:
        print("[闸门] 视觉未配置，未产生任何识别结果（退出码 2、零请求）。")
        return bc.EXIT_GATED

    images = detail["images"]
    fields_total = len(images) * len(METRIC_LABELS)
    fields_matched = sum(item["matched"] for item in images)
    metrics.extend(
        [
            bc.metric("vision_real.images_total", len(images), "张", n=len(images),
                      method=METHOD, category=bc.CATEGORY_LLM_SAMPLE,
                      confidence="medium", note="真实截图集（用户提供并标注）；n=k 小样本，不得外推"),
            bc.metric("vision_real.fields_total", fields_total, "字段", n=fields_total,
                      method=METHOD, category=bc.CATEGORY_LLM_SAMPLE, confidence="medium"),
            bc.metric("vision_real.fields_matched", fields_matched, "字段", n=fields_total,
                      method=METHOD, category=bc.CATEGORY_LLM_SAMPLE, confidence="medium"),
            bc.metric("vision_real.field_accuracy",
                      (round(fields_matched / fields_total, 4) if fields_total else None),
                      "比率", n=fields_total, method=METHOD, category=bc.CATEGORY_LLM_SAMPLE,
                      confidence="medium", note=f"n={fields_total} 字段（k 张真实截图）；不得外推"),
            bc.metric("vision_real.tokens_total", sum(item["tokens_total"] for item in images), "token",
                      n=len(images), method=METHOD, category=bc.CATEGORY_LLM_SAMPLE, confidence="medium"),
        ]
    )
    result_path = bc.write_suite_result(
        "vision_real.json", suite="vision_real", metrics=metrics, detail=detail,
        summary={"images": len(images), "fields_matched": fields_matched, "fields_total": fields_total},
        notes=[
            "真实截图集由用户提供并标注标准答案；n=k×6 字段如实标注，不得外推。",
            "计分口径与合成档一致（canonical 归一后判等）；两侧原始值均在 detail 中留档。",
            "agent_runs / 检查点线程已按差集清理。",
        ],
    )
    print("-" * 66)
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_FAIL if detail["errors"] and not images else bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
