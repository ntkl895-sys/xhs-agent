# -*- coding: utf-8 -*-
"""度量专项 · 阶段二.2a：视觉识别（合成截图档）。

两段式设计：

1. ``--generate-fixtures``（**离线可跑、零请求**）：程序生成本地 HTML，经
   playwright-core + 本机 Chrome 渲染为 PNG（2x 缩放），六指标数值已知，
   产出 ``docs/benchmark/fixtures/vision_synth/manifest.json``（含 ground truth）；
2. 默认模式（**真实运行**）：对每张合成截图调用 ``recognize_metric_screenshot``
   （视觉模型），按 ``bench_common.canonical_metric_value`` 的口径逐字段计分，
   输出字段级准确率与 token / 时延。未配置 LLM → **退出码 2（零请求）**。

⚠️ 口径声明：本档为**合成夹具**（数值已知、程序生成），仅验证端到端管线与
计分口径，**不代表真实截图的识别准确率**；真实截图档见 ``bench_vision_real.py``
（由用户按模板补数，n=k 如实标注）。

用法（项目根目录）：

    :: ① 生成夹具（离线）
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_vision_synth.py --generate-fixtures

    :: ② 真实运行（视觉模型；交互确认）
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_vision_synth.py

退出码：0 完成；1 失败；2 未配置（零请求）；3 环境不满足（node/夹具缺失）。
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

SYNTH_DIR = bc.FIXTURES_DIR / "vision_synth"
METHOD_FIXTURE = "scripts/benchmark/bench_vision_synth.py --generate-fixtures（本地渲染，零请求）"
METHOD_PIPELINE = (
    "scripts/benchmark/bench_vision_synth.py：对合成截图调用 recognize_metric_screenshot，"
    "按 canonical 口径逐字段比对（去千分位 / 万·w 后缀归一后判等）"
)

METRIC_LABELS = {
    "views": "曝光量",
    "likes": "点赞",
    "collects": "收藏",
    "comments": "评论",
    "shares": "分享",
    "new_followers": "涨粉",
}

#: 12 张合成截图（数值形态 × 版式变体；覆盖千分位 / 万 / 混合 / 极值 / 零值）
FIXTURE_SPECS = [
    {"file": "synth_01.png", "layout": "standard",
     "values": {"views": "12.8万", "likes": "3286", "collects": "1215",
                "comments": "187", "shares": "96", "new_followers": "342"}},
    {"file": "synth_02.png", "layout": "standard",
     "values": {"views": "8664", "likes": "512", "collects": "233",
                "comments": "41", "shares": "17", "new_followers": "60"}},
    {"file": "synth_03.png", "layout": "standard",
     "values": {"views": "45.2万", "likes": "1.28万", "collects": "4560",
                "comments": "803", "shares": "291", "new_followers": "1500"}},
    {"file": "synth_04.png", "layout": "standard",
     "values": {"views": "1,234", "likes": "56", "collects": "7",
                "comments": "0", "shares": "12", "new_followers": "3"}},
    {"file": "synth_05.png", "layout": "compact",
     "values": {"views": "128,456", "likes": "12,345", "collects": "3,210",
                "comments": "456", "shares": "123", "new_followers": "789"}},
    {"file": "synth_06.png", "layout": "compact",
     "values": {"views": "9.9万", "likes": "1.5万", "collects": "8,888",
                "comments": "1,024", "shares": "512", "new_followers": "2,048"}},
    {"file": "synth_07.png", "layout": "compact",
     "values": {"views": "999", "likes": "99", "collects": "9",
                "comments": "0", "shares": "0", "new_followers": "1"}},
    {"file": "synth_08.png", "layout": "compact",
     "values": {"views": "100万", "likes": "10万", "collects": "5万",
                "comments": "1万", "shares": "5000", "new_followers": "2万"}},
    {"file": "synth_09.png", "layout": "large",
     "values": {"views": "0.5万", "likes": "0.8万", "collects": "0.3万",
                "comments": "0.1万", "shares": "0.05万", "new_followers": "0.2万"}},
    {"file": "synth_10.png", "layout": "large",
     "values": {"views": "1234567", "likes": "234567", "collects": "34567",
                "comments": "4567", "shares": "567", "new_followers": "67"}},
    {"file": "synth_11.png", "layout": "large",
     "values": {"views": "12.8万", "likes": "3286", "collects": "1215",
                "comments": "187", "shares": "96", "new_followers": "342"}},
    {"file": "synth_12.png", "layout": "large",
     "values": {"views": "86.5万", "likes": "5.6万", "collects": "2.4万",
                "comments": "1.1万", "shares": "0.9万", "new_followers": "3.3万"}},
]

#: 版式变体（字号 / 边距差异；模拟不同渲染环境）
_LAYOUTS = {
    "standard": {"value_font": 26, "label_font": 13, "cell_pad": "16px 18px",
                 "title_font": 22, "card_pad": "28px 32px", "gap": 14},
    "compact": {"value_font": 20, "label_font": 11, "cell_pad": "12px 14px",
                "title_font": 18, "card_pad": "36px 42px", "gap": 10},
    "large": {"value_font": 32, "label_font": 15, "cell_pad": "14px 16px",
              "title_font": 26, "card_pad": "22px 26px", "gap": 16},
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


def build_html(values: dict, layout: str = "standard") -> str:
    cfg = _LAYOUTS.get(layout, _LAYOUTS["standard"])
    cells = "".join(
        f'<div class="cell"><div class="label">{METRIC_LABELS[key]}</div>'
        f'<div class="value">{values.get(key, "—")}</div></div>'
        for key in METRIC_LABELS
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:920px; height:640px; background:#f6f7f9; font-family:"Microsoft YaHei",sans-serif; }}
  .card {{ margin:30px auto; width:780px; background:#fff; border-radius:16px;
           box-shadow:0 4px 18px rgba(0,0,0,0.08); padding:{cfg['card_pad']}; }}
  .title {{ font-size:{cfg['title_font']}px; font-weight:600; color:#333; margin-bottom:6px; }}
  .sub {{ font-size:12px; color:#999; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:{cfg['gap']}px; }}
  .cell {{ background:#f2f5ff; border-radius:10px; padding:{cfg['cell_pad']}; }}
  .label {{ font-size:{cfg['label_font']}px; color:#7a8bb0; margin-bottom:8px; }}
  .value {{ font-size:{cfg['value_font']}px; font-weight:700; color:#1f2d4d; }}
  .foot {{ margin-top:20px; font-size:12px; color:#bbb; text-align:right; }}
</style></head>
<body><div class="card">
  <div class="title">笔记数据 · 近 7 天</div>
  <div class="sub">数据更新时间：今日 09:00 ｜ 版式：{layout}</div>
  <div class="grid">{cells}</div>
  <div class="foot">（合成夹具 · benchmark）</div>
</div></body></html>
"""


def generate_fixtures() -> int:
    """离线生成合成截图 + manifest（零请求）。"""
    node = bc.find_node()
    if not node:
        print("[环境] 未找到 node 可执行文件，无法渲染合成截图。")
        return bc.EXIT_ENV
    cjs = Path(__file__).resolve().parent / "render_fixture.cjs"
    SYNTH_DIR.mkdir(parents=True, exist_ok=True)

    fixtures = []
    for spec in FIXTURE_SPECS:
        html_path = SYNTH_DIR / spec["file"].replace(".png", ".html")
        png_path = SYNTH_DIR / spec["file"]
        html_path.write_text(
            build_html(spec["values"], spec.get("layout", "standard")), encoding="utf-8"
        )
        rc, output = bc.run_capture(
            [node, str(cjs), str(html_path), str(png_path)],
            env=bc.node_env(),
            timeout=240,
        )
        if rc != 0 or not png_path.exists() or png_path.stat().st_size == 0:
            print(f"[失败] 渲染 {spec['file']}：rc={rc}；输出尾部：{output[-300:]}")
            return bc.EXIT_FAIL
        fixtures.append(
            {
                "name": spec["file"],
                "path": f"fixtures/vision_synth/{spec['file']}",
                "bytes": png_path.stat().st_size,
                "layout": spec.get("layout", "standard"),
                "expected": spec["values"],
            }
        )
        print(f"[渲染] {spec['file']}  OK（{png_path.stat().st_size} 字节）")

    manifest = {
        "purpose": "合成截图夹具：数值已知、程序生成；仅验证端到端管线与计分，不代表真实截图准确率",
        "generated_at": bc.now_iso(),
        "renderer": "scripts/benchmark/render_fixture.cjs（playwright-core + 本机 Chrome，2x）",
        "six_metrics": list(METRIC_LABELS),
        "fixtures": fixtures,
    }
    bc.write_json(SYNTH_DIR / "manifest.json", manifest)
    print(f"[夹具] manifest：{(SYNTH_DIR / 'manifest.json').relative_to(bc.ROOT)}")
    return bc.EXIT_OK


def run_pipeline(args) -> int:
    manifest_path = SYNTH_DIR / "manifest.json"
    if not manifest_path.exists():
        print("[环境] 缺少合成截图夹具；请先运行：")
        print("       backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_vision_synth.py --generate-fixtures")
        return bc.EXIT_ENV
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixtures = manifest.get("fixtures") or []

    readiness = llm_factory.describe_llm_readiness()
    print(f"[就绪] {json.dumps(readiness, ensure_ascii=False)}")
    if not readiness.get("ready"):
        print("[闸门] LLM / 视觉未配置，本脚本不发出任何请求（退出码 2）。")
        return bc.EXIT_GATED

    print(f"计划：{len(fixtures)} 张合成截图 → 最多 {len(fixtures)} 次视觉模型调用"
          "（token 量级取决于图片与输出；粗估、非承诺）。")
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
            image_path = bc.BENCH_ROOT / entry["path"]
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
            except Exception as exc:  # noqa: BLE001 - 如实记录并继续
                summary, error = None, f"{type(exc).__name__}: {exc}"
            wall_ms = round((time.perf_counter() - started) * 1000, 1)
            new_runs = _all_run_ids() - round_before
            runs = _fetch_runs(sorted(new_runs))

            if error:
                detail["errors"].append({"image": entry["name"], "error": error})
                print(f"[识别] {entry['name']}  失败：{error}")
                continue

            recognized = (summary.get("metrics") or {}) if summary else {}
            fields = {
                key: bc.score_metric_field(entry["expected"].get(key), recognized.get(key))
                for key in METRIC_LABELS
            }
            matched = sum(1 for item in fields.values() if item["matched"])
            record = {
                "image": entry["name"],
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
                f"[识别] {entry['name']}  字段命中 {matched}/{len(METRIC_LABELS)}  "
                f"parse_attempts={record['parse_attempts']}  tokens={record['tokens_total']}  {wall_ms}ms"
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
    whole_ok = sum(
        1 for item in images if item["fields_total"] and item["matched"] == item["fields_total"]
    )
    latency_stats = bc.stat_block([item["wall_ms"] for item in images])
    metrics.extend(
        [
            bc.metric("vision_synth.images_total", len(FIXTURE_SPECS), "张", n=len(FIXTURE_SPECS),
                      method=METHOD_FIXTURE, category=bc.CATEGORY_SYNTHETIC,
                      note="合成截图、程序生成（2x 渲染；含千分位/万/极值/零值与 3 种版式）；数值已知"),
            bc.metric("vision_synth.fields_total", fields_total, "字段", n=fields_total,
                      method=METHOD_PIPELINE, category=bc.CATEGORY_SYNTHETIC,
                      note="每张图 6 个字段（views/likes/collects/comments/shares/new_followers）"),
            bc.metric("vision_synth.fields_matched", fields_matched, "字段", n=fields_total,
                      method=METHOD_PIPELINE, category=bc.CATEGORY_SYNTHETIC),
            bc.metric("vision_synth.field_accuracy", (round(fields_matched / fields_total, 4) if fields_total else None),
                      "比率", n=fields_total, method=METHOD_PIPELINE, category=bc.CATEGORY_SYNTHETIC,
                      confidence="medium",
                      note=f"合成夹具字段级准确率（n={fields_total} 字段）；不代表真实截图准确率"),
            bc.metric("vision_synth.whole_image_accuracy",
                      (round(whole_ok / len(images), 4) if images else None),
                      "比率", n=len(images), method=METHOD_PIPELINE, category=bc.CATEGORY_SYNTHETIC,
                      confidence="medium",
                      note="整图全对率：6 个字段全部正确的图占比（合成夹具）"),
            bc.metric("vision_synth.tokens_total", sum(item["tokens_total"] for item in images), "token",
                      n=len(images), method=METHOD_PIPELINE, category=bc.CATEGORY_SYNTHETIC),
            bc.metric("vision_synth.latency_p50_ms", (latency_stats or {}).get("p50"), "毫秒",
                      n=len(images), method=METHOD_PIPELINE, category=bc.CATEGORY_SYNTHETIC),
            bc.metric("vision_synth.latency_p95_ms", (latency_stats or {}).get("p95"), "毫秒",
                      n=len(images), method=METHOD_PIPELINE, category=bc.CATEGORY_SYNTHETIC),
        ]
    )
    result_path = bc.write_suite_result(
        "vision_synth.json", suite="vision_synth", metrics=metrics, detail=detail,
        summary={"images": len(images), "fields_matched": fields_matched, "fields_total": fields_total},
        notes=[
            "合成夹具（数值已知、程序生成）；仅验证端到端管线与计分口径，不代表真实截图准确率。",
            "计分口径：去千分位逗号与空白、支持「万/w」后缀，归一为整数后判等；任一侧无法解析计不一致。",
            "agent_runs / 检查点线程已按差集清理。",
        ],
    )
    print("-" * 66)
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_FAIL if detail["errors"] and not images else bc.EXIT_OK


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


def main() -> int:
    parser = argparse.ArgumentParser(description="视觉识别·合成截图档（benchmark 套件）")
    parser.add_argument("--generate-fixtures", action="store_true", help="仅离线生成夹具（零请求）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（脚本化）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("度量专项 · 视觉识别（合成截图档）")
    if args.generate_fixtures:
        return generate_fixtures()
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
