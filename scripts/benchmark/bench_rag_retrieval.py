# -*- coding: utf-8 -*-
"""度量专项 · 阶段一.3：RAG 检索合成集（Recall@k / MRR，临时库 + pgvector）。

口径与边界（如实声明）：

- 本脚本使用**合成夹具**（12 条特征明显、答案明确的知识片段 + 12 条应命中
  查询，一一对应），仅用于**验证检索管线的正确性**，不代表真实语料的检索质量；
- 检索质量指标（Recall@k / MRR / 时延 / 索引耗时）**必须**由本地 bge 真实向量化
  产出：依赖缺失或模型不可加载时，一律写 ``N/A``（category=not_measured），
  **绝不用假向量替代产出「质量数字」**；
- 「接线自检」（mechanics）用**构造性一维锚点向量**验证：建库 → 迁移 →
  入库 → HNSW 重建 → 检索排序 → 召回计算等**机制**可跑通；其结果仅报
  ``mechanics.self_check_ok``，不产出任何检索质量指标；
- 全程只操作**临时库**（xhs_bench_rag_*），结束一律 DROP 并复查残留=0；
  不写真实库、不改任何业务数据。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_rag_retrieval.py

真实验（需先安装向量化依赖与模型，用户侧执行）：

    pip install langchain-huggingface sentence-transformers
    set HF_ENDPOINT=https://hf-mirror.com
    backend\\.venv\\Scripts\\python.exe scripts\\check_embedding_local.py
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_rag_retrieval.py

退出码：0 = 完成（含如实 N/A）；1 = 机制自检/真实实验断言失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import bench_common as bc

METHOD_REAL = (
    "scripts/benchmark/bench_rag_retrieval.py：临时库（alembic 迁移）上以本地 bge "
    "向量化 12 条合成知识片段，经 crud.search_snippets（pgvector 余弦 top-k，HNSW）"
    "计算 Recall@k / MRR 与查询时延"
)
METHOD_MECHANICS = (
    "scripts/benchmark/bench_rag_retrieval.py --mechanics：构造性锚点向量（非 bge，"
    "非检索质量）验证 建库→迁移→入库→索引重建→检索排序→召回统计 的机制接线"
)

#: 合成夹具：12 条片段 + 一一对应的查询（特征明显、答案唯一）。
FIXTURE: list[dict] = [
    {"title": "咖啡与睡眠", "content": "咖啡因在人体内的半衰期约为 5 小时。睡前 6 小时内摄入咖啡因，会显著推迟入睡时间并降低深睡比例。", "query": "睡前喝咖啡会影响睡眠吗？为什么？"},
    {"title": "Python 装饰器", "content": "Python 装饰器本质是接收函数并返回新函数的高阶函数，@ 语法等价于把函数作为参数传入并重新绑定同名变量。", "query": "Python 里 @ 语法糖的本质是什么？"},
    {"title": "北海道冬季", "content": "北海道 1 月平均气温约 -4℃，札幌雪祭通常在 2 月上旬举办，行程应优先防滑鞋与保暖内层。", "query": "一月去札幌看雪祭要注意什么？"},
    {"title": "跑步与心率", "content": "有氧慢跑常用心率区间为最大心率的 60%~70%，最大心率可用 220 减去年龄粗估。", "query": "慢跑时心率控制在什么范围比较合适？"},
    {"title": "SQL 索引", "content": "B 树索引适合等值与范围查询；对高选择性列建索引收益最大，而对频繁更新的低选择性列建索引反而拖慢写入。", "query": "数据库索引什么时候建、什么时候不该建？"},
    {"title": "相机曝光三角", "content": "曝光由光圈、快门、ISO 三者共同决定：光圈越大进光越多景深越浅，ISO 越高噪点越多。", "query": "摄影曝光三要素之间是什么关系？"},
    {"title": "酸奶保存", "content": "未开封酸奶应在 2℃~6℃ 冷藏并保质期内饮用；开封后建议 24 小时内喝完，避免反复取出回温。", "query": "打开的酸奶可以放多久？"},
    {"title": "Git 合并策略", "content": "rebase 会重写提交历史，适合整理私有分支；merge 保留分叉历史，适合公共分支。团队协作中不要对已推送的公共分支做 rebase。", "query": "代码协作里 rebase 和 merge 分别适合什么场景？"},
    {"title": "瓷砖防滑", "content": "卫生间选砖看防滑等级：干态与湿态摩擦系数差值越小越防滑，哑光砖湿态表现通常优于亮面砖。", "query": "浴室地砖怎么选才不容易滑倒？"},
    {"title": "番茄工作法", "content": "番茄工作法把 25 分钟专注 + 5 分钟休息设为一个循环；四个循环后休息 15~30 分钟，关键是打断时记录而不立刻处理。", "query": "番茄钟是怎么安排专注和休息的？"},
    {"title": "地铁安检", "content": "地铁禁止携带的物品包括：易燃易爆品、管制刀具、超过 100ml 的酒精制品；充电宝须标识清晰且符合容量上限。", "query": "坐地铁哪些东西不能带？"},
    {"title": "咖啡拉花", "content": "拿铁拉花的基础是打发绵密奶泡：奶温 60℃~65℃ 时蛋白质结构最稳定，注入时保持奶缸贴近杯口匀速抬升。", "query": "做拿铁拉花奶泡怎么打、温度多少合适？"},
]


# ---------------------------------------------------------------------------
# 夹具落盘（可复现输入）
# ---------------------------------------------------------------------------
def dump_fixture() -> Path:
    payload = {
        "purpose": "合成夹具：仅验证 RAG 检索管线正确性；不代表真实语料检索质量",
        "generated_at": bc.now_iso(),
        "entries": FIXTURE,
        "note": "12 条片段与其应命中查询一一对应（顺序即 ground truth 映射）",
    }
    path = bc.FIXTURES_DIR / "rag_fixture.json"
    bc.write_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# 临时库管线（mechanics=假向量 / real=bge）
# ---------------------------------------------------------------------------
def _temp_engine(dsn: str):
    import sqlalchemy as sa  # noqa: PLC0415

    return sa.create_engine(dsn)


def _one_hot(index: int, dim: int = 1024) -> list[float]:
    vec = [0.0] * dim
    vec[index] = 1.0
    return vec


def execute_pipeline(temp_db: str, *, mode: str) -> dict:
    """在临时库上执行完整管线；mode = "fake"（机制自检）或 "bge"（真实）。"""
    import sqlalchemy as sa  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app.agent.embedding.service import embed_texts  # noqa: PLC0415
    from app.crud.knowledge import crud_knowledge  # noqa: PLC0415
    from app.models.enums import ChunkType, SnippetStatus  # noqa: PLC0415
    from app.models.knowledge import KnowledgeSnippet  # noqa: PLC0415
    from app.services.rag import retrieve_snippets  # noqa: PLC0415

    dsn = f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"
    engine = _temp_engine(dsn)
    result: dict = {"mode": mode, "temp_db": temp_db}

    try:
        with Session(engine) as db:
            # ---- 1) 入库（bge：真实向量化；fake：构造锚点向量）----
            insert_started = time.perf_counter()
            if mode == "bge":
                contents = [entry["content"] for entry in FIXTURE]
                vectors = embed_texts(contents)
            else:
                vectors = [_one_hot(index) for index in range(len(FIXTURE))]
            rows: list[KnowledgeSnippet] = []
            for index, entry in enumerate(FIXTURE):
                row = KnowledgeSnippet(
                    chunk_type=ChunkType.GENERAL,
                    title=f"[BENCH-RAG] {entry['title']}",
                    content=entry["content"],
                    embedding=[float(value) for value in vectors[index]],
                    meta={"benchmark": "synthetic_fixture", "fixture_index": index},
                    status=SnippetStatus.ACTIVE,
                )
                db.add(row)
                rows.append(row)
            db.commit()
            ids = [str(row.id) for row in rows]
            result["insert_ms"] = round((time.perf_counter() - insert_started) * 1000, 1)
            result["snippets_inserted"] = len(ids)
            result["vector_dim"] = len(vectors[0]) if vectors else None

            # ---- 2) HNSW 索引重建计时（取迁移建好的索引定义原样重建）----
            index_def = db.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_knowledge_snippets_embedding_hnsw'"
                )
            ).scalar_one_or_none()
            result["index_def_found"] = bool(index_def)
            if index_def:
                db.execute(sa.text("DROP INDEX IF EXISTS ix_knowledge_snippets_embedding_hnsw"))
                db.commit()
                index_started = time.perf_counter()
                db.execute(sa.text(index_def))
                db.commit()
                result["index_build_ms"] = round((time.perf_counter() - index_started) * 1000, 1)

            # ---- 3) 检索（fake：query_vec 直传；bge：query_text 端到端）----
            rankings: list[list[str]] = []
            latency_end_to_end: list[float] = []
            latency_vector_only: list[float] = []
            if mode == "bge":
                query_vecs = embed_texts([entry["query"] for entry in FIXTURE])
            else:
                query_vecs = [_one_hot(index) for index in range(len(FIXTURE))]
            for round_index in range(2 if mode == "bge" else 1):
                for index, entry in enumerate(FIXTURE):
                    if mode == "bge":
                        started = time.perf_counter()
                        hits = retrieve_snippets(db, query_text=entry["query"], top_k=5)
                        latency_end_to_end.append(
                            round((time.perf_counter() - started) * 1000, 2)
                        )
                        started = time.perf_counter()
                        hits_vec = crud_knowledge.search_snippets(
                            db, query_vec=query_vecs[index], k=5
                        )
                        latency_vector_only.append(
                            round((time.perf_counter() - started) * 1000, 2)
                        )
                        if round_index == 0:
                            rankings.append([str(item["id"]) for item in hits])
                    else:
                        hits = crud_knowledge.search_snippets(
                            db, query_vec=query_vecs[index], k=5
                        )
                        rankings.append([str(item["id"]) for item in hits])
            result["rankings_top5"] = rankings
            result["latency_end_to_end_ms"] = latency_end_to_end
            result["latency_vector_only_ms"] = latency_vector_only

            # ---- 4) Recall@k / MRR ----
            recall = {1: 0, 3: 0, 5: 0}
            reciprocal: list[float] = []
            for index, ranked in enumerate(rankings):
                expected = ids[index]
                for k in recall:
                    if expected in ranked[:k]:
                        recall[k] += 1
                rank = ranked.index(expected) + 1 if expected in ranked else None
                reciprocal.append(1.0 / rank if rank else 0.0)
            n_queries = len(rankings)
            result["recall_at"] = {str(k): recall[k] / n_queries for k in sorted(recall)}
            result["mrr"] = round(sum(reciprocal) / n_queries, 4)
            result["n_queries"] = n_queries
        return result
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索合成集（benchmark 套件）")
    parser.add_argument("--mechanics-only", action="store_true", help="仅跑机制自检（假向量）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.add_backend_to_path()
    bc.print_banner("度量专项 · RAG 检索合成集（临时库 + pgvector）")

    fixture_path = dump_fixture()
    print(f"[夹具] 已落盘：{fixture_path.relative_to(bc.ROOT)}（12 片段 / 12 查询，一一对应）")

    # ---- 就绪检查（项目原生 API；不加载模型、不发请求）----
    from app.agent.embedding.service import (  # noqa: PLC0415
        describe_embedding_blocker,
        describe_embedding_readiness,
    )

    readiness = describe_embedding_readiness()
    blocker = describe_embedding_blocker()
    print(f"[就绪] {json.dumps(readiness, ensure_ascii=False)}")
    bge_ready = blocker is None and bool(readiness.get("local_deps_installed"))

    metrics: list[dict] = [
        bc.metric(
            "rag.fixture.snippets_total", len(FIXTURE), "条", n=len(FIXTURE),
            method="内置合成夹具（docs/benchmark/fixtures/rag_fixture.json）",
            category=bc.CATEGORY_SYNTHETIC,
            note="合成夹具、仅验证检索正确性；不代表真实语料质量",
        ),
        bc.metric(
            "rag.fixture.queries_total", len(FIXTURE), "条", n=len(FIXTURE),
            method="同上（查询与片段一一对应，答案唯一）",
            category=bc.CATEGORY_SYNTHETIC,
            note="合成夹具、仅验证检索正确性",
        ),
    ]

    # ---- pgvector 扩展实测（真实库，只读）----
    rc_ext, ext_out = bc.psql(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    )
    ext_version = ext_out.strip() if rc_ext == 0 else None
    metrics.append(
        bc.metric(
            "rag.pgvector.extversion", ext_version or "n/a", "版本号", n=1,
            method="docker compose exec psql：SELECT extversion FROM pg_extension（只读）",
            note="向量扩展可用性事实；与检索质量指标相互独立",
        )
    )

    detail: dict = {"readiness": readiness, "blocker": blocker, "mechanic": None, "real": None}
    outcomes: list[str] = []

    # ---- 机制自检（假向量；验证接线，不产出质量数字）----
    rc_docker, _ = bc.run_capture(["docker", "info"], timeout=60)
    if rc_docker != 0:
        outcomes.append("mechanic_skipped: Docker 引擎不可用")
        print("[机制] Docker 不可用，跳过机制自检。")
    else:
        temp_db = bc.temp_db_name("xhs_bench_rag")
        dsn = f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"
        rc_create, _ = bc.psql(f'CREATE DATABASE "{temp_db}"')
        mechanic_ok = 0
        try:
            if rc_create == 0:
                rc_mig, _ = bc.run_capture(
                    [str(bc.VENV_PY), "-m", "alembic", "upgrade", "head"],
                    cwd=bc.BACKEND_DIR,
                    env=bc.clean_env({"DATABASE_URL": dsn}),
                    log_path=bc.LOGS_DIR / "rag_alembic_tmp.txt",
                    timeout=300,
                )
                if rc_mig == 0:
                    mech = execute_pipeline(temp_db, mode="fake")
                    detail["mechanic"] = mech
                    expect_ok = (
                        mech["snippets_inserted"] == len(FIXTURE)
                        and mech["n_queries"] == len(FIXTURE)
                        and mech["recall_at"]["1"] == 1.0
                        and mech["mrr"] == 1.0
                    )
                    mechanic_ok = 1 if expect_ok else 0
                    print(
                        f"[机制] 接线自检：{len(FIXTURE)} 条入库 / 索引重建 "
                        f"{'OK' if mech.get('index_def_found') else '未找到索引定义'} / "
                        f"锚点召回@1={mech['recall_at']['1']}（构造性假向量，非检索质量）"
                    )
                else:
                    outcomes.append("mechanic_failed: alembic 迁移失败")
            else:
                outcomes.append("mechanic_failed: 临时库创建失败")
        finally:
            bc.drop_temp_db(temp_db)
            leftover = bc.db_exists(temp_db)
            if leftover:
                outcomes.append("temp_db_leftover")
            rc_scan, scan_out = bc.psql(
                "SELECT datname FROM pg_database WHERE datname LIKE 'xhs_bench%'"
            )
            residue = [line for line in scan_out.strip().splitlines() if line.strip()]
            if residue:
                outcomes.append(f"db_residue:{residue}")
        metrics.append(
            bc.metric(
                "rag.mechanics.self_check_ok", mechanic_ok, "布尔(1=是)", n=1,
                method=METHOD_MECHANICS, category=bc.CATEGORY_SYNTHETIC,
                log_path="docs/benchmark/logs/rag_mechanics.log",
                note="构造性假向量以验证机制接线；**不构成任何检索质量结论**",
            )
        )

    # ---- 真实实验（bge）或如实 N/A ----
    na_reason = None
    if args.mechanics_only:
        na_reason = "本命令为 --mechanics-only（显式跳过真实实验）"
    elif not bge_ready:
        na_reason = (
            "本地 bge 依赖/模型未就绪："
            + json.dumps(blocker, ensure_ascii=False)
            + "（安装后重跑本脚本即产出全部质量指标）"
        )

    if na_reason is None:
        temp_db = bc.temp_db_name("xhs_bench_rag")
        dsn = f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"
        rc_create, _ = bc.psql(f'CREATE DATABASE "{temp_db}"')
        try:
            rc_mig = 1
            if rc_create == 0:
                rc_mig, _ = bc.run_capture(
                    [str(bc.VENV_PY), "-m", "alembic", "upgrade", "head"],
                    cwd=bc.BACKEND_DIR,
                    env=bc.clean_env({"DATABASE_URL": dsn}),
                    log_path=bc.LOGS_DIR / "rag_alembic_tmp.txt",
                    timeout=300,
                )
            if rc_mig == 0:
                try:
                    real = execute_pipeline(temp_db, mode="bge")
                    detail["real"] = real
                except Exception as exc:  # noqa: BLE001 - 模型加载/向量化失败如实降级
                    na_reason = f"真实向量化执行失败（如实记录）：{type(exc).__name__}: {exc}"
            else:
                na_reason = "临时库迁移失败，真实实验未执行"
        finally:
            bc.drop_temp_db(temp_db)
            if bc.db_exists(temp_db):
                outcomes.append("temp_db_leftover(real)")
    if detail.get("real"):
        real = detail["real"]
        latency_e2e = bc.stat_block(real["latency_end_to_end_ms"])
        latency_vec = bc.stat_block(real["latency_vector_only_ms"])
        metrics.extend(
            [
                bc.metric("rag.vector_dim_measured", real["vector_dim"], "维", n=len(FIXTURE),
                          method=METHOD_REAL, category=bc.CATEGORY_SYNTHETIC,
                          note="实测 bge 输出维度；合成夹具"),
                bc.metric("rag.recall_at_1", real["recall_at"]["1"], "比率", n=real["n_queries"],
                          method=METHOD_REAL, category=bc.CATEGORY_SYNTHETIC,
                          note="合成夹具、仅验证检索正确性（12 查询一一对应）"),
                bc.metric("rag.recall_at_3", real["recall_at"]["3"], "比率", n=real["n_queries"],
                          method=METHOD_REAL, category=bc.CATEGORY_SYNTHETIC, note="同上"),
                bc.metric("rag.recall_at_5", real["recall_at"]["5"], "比率", n=real["n_queries"],
                          method=METHOD_REAL, category=bc.CATEGORY_SYNTHETIC, note="同上"),
                bc.metric("rag.mrr", real["mrr"], "比率", n=real["n_queries"],
                          method=METHOD_REAL, category=bc.CATEGORY_SYNTHETIC, note="同上"),
                bc.metric("rag.index_build_ms", real.get("index_build_ms"), "毫秒", n=len(FIXTURE),
                          method=METHOD_REAL, category=bc.CATEGORY_SYNTHETIC,
                          note="HNSW 索引在 12 行上的重建耗时（小样本）"),
                bc.metric("rag.query_latency_end_to_end_p50_ms",
                          (latency_e2e or {}).get("p50"), "毫秒", n=len(real["latency_end_to_end_ms"]),
                          method=METHOD_REAL + "（端到端；查询编码走进程内缓存，第 2 轮为缓存命中）",
                          category=bc.CATEGORY_SYNTHETIC),
                bc.metric("rag.query_latency_end_to_end_p95_ms",
                          (latency_e2e or {}).get("p95"), "毫秒", n=len(real["latency_end_to_end_ms"]),
                          method=METHOD_REAL + "（端到端；查询编码走进程内缓存，第 2 轮为缓存命中）",
                          category=bc.CATEGORY_SYNTHETIC),
                bc.metric("rag.query_latency_vector_only_p50_ms",
                          (latency_vec or {}).get("p50"), "毫秒", n=len(real["latency_vector_only_ms"]),
                          method=METHOD_REAL + "（仅 pgvector 检索，预计算向量）", category=bc.CATEGORY_SYNTHETIC),
                bc.metric("rag.query_latency_vector_only_p95_ms",
                          (latency_vec or {}).get("p95"), "毫秒", n=len(real["latency_vector_only_ms"]),
                          method=METHOD_REAL + "（仅 pgvector 检索，预计算向量）", category=bc.CATEGORY_SYNTHETIC),
            ]
        )
    else:
        for name, unit in (
            ("rag.vector_dim_measured", "维"),
            ("rag.recall_at_1", "比率"),
            ("rag.recall_at_3", "比率"),
            ("rag.recall_at_5", "比率"),
            ("rag.mrr", "比率"),
            ("rag.index_build_ms", "毫秒"),
            ("rag.query_latency_end_to_end_p50_ms", "毫秒"),
            ("rag.query_latency_end_to_end_p95_ms", "毫秒"),
            ("rag.query_latency_vector_only_p50_ms", "毫秒"),
            ("rag.query_latency_vector_only_p95_ms", "毫秒"),
        ):
            metrics.append(bc.na_metric(name, method=METHOD_REAL, reason=na_reason, unit=unit))
        print(f"[真实实验] 未执行：{na_reason}")

    result_path = bc.write_suite_result(
        "rag.json",
        suite="rag_retrieval",
        metrics=metrics,
        detail=detail,
        summary={
            "bge_ready": bge_ready,
            "outcomes": outcomes,
            "na_reason": na_reason,
        },
        notes=[
            "本实验为合成夹具：仅验证检索管线正确性，不代表真实语料检索质量。",
            "依赖未安装/模型不可加载时质量指标一律 N/A（绝不用假向量替代）。",
            "临时库已删除并全局兜底复查（xhs_bench% 残留=0）。",
        ],
    )
    print("-" * 66)
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    failed = [name for name in outcomes if "failed" in name or "leftover" in name or "residue" in name]
    mechanic_metric = next(
        (item for item in metrics if item["name"] == "rag.mechanics.self_check_ok"), None
    )
    if mechanic_metric is not None and mechanic_metric["value"] == 0:
        failed.append("mechanics_self_check")
    return bc.EXIT_FAIL if failed else bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
