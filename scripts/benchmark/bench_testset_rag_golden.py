# -*- coding: utf-8 -*-
"""测试集评测 · D：RAG 检索黄金集（临时库 + pgvector HNSW；bge 就绪才实跑）。

读冻结测试集 ``fixtures/testsets/rag_golden/``（sha256 校验）：
30 条知识片段（全 synthetic，含「主题相近但答案不同」近邻对）+ 40 条查询
（每条标注应命中片段 id，1-3 个）。评测流程（bge 就绪时）：

1. 临时库（alembic 迁移，绝不触碰既有库）；
2. 30 条片段经**真实 bge** 向量化入库（1024 维）；
3. HNSW 余弦索引重建计时（取迁移建好的索引定义原样重建）；
4. 40 条查询端到端检索 top-5（``retrieve_snippets``，含查询编码）+ 纯向量检索计时；
5. Recall@1/@3/@5、MRR、全命中率（top-5 含全部期望片段）、逐查询命中明细；
6. 删除临时库并全局兜底复查。

**bge 未就绪时**：全部质量指标如实写 N/A（不伪造、不用假向量出率），
补跑命令见 note；脚本退出码仍为 0（如实 N/A 是合法结果）。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_testset_rag_golden.py

依赖（用户安装后即可实跑；本脚本不会自动安装）：
    pip install langchain-huggingface sentence-transformers
    set HF_ENDPOINT=https://hf-mirror.com   （国内下载镜像）
    backend\\.venv\\Scripts\\python.exe scripts\\check_embedding_local.py  （模型预热/校验）

退出码：0 = 评测完成（含如实 N/A）；1 = 评测环境错误（夹具缺失 / 指纹不符）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import bench_common as bc  # noqa: E402

METHOD = (
    "scripts/benchmark/bench_testset_rag_golden.py：冻结黄金集（30 片段 / 40 查询，"
    "人工标注期望命中）＋临时库（alembic 迁移）；本地 bge 向量化（1024 维）+ "
    "pgvector HNSW 余弦 top-5；类别=synthetic_fixture（合成语料，仅代表该语料上的检索质量）"
)


def execute_golden(temp_db: str) -> dict:
    """在临时库上跑黄金集全流程（仅 bge 路径调用）。"""
    import sqlalchemy as sa  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app.agent.embedding.service import embed_texts  # noqa: PLC0415
    from app.crud.knowledge import crud_knowledge  # noqa: PLC0415
    from app.models.enums import ChunkType, SnippetStatus  # noqa: PLC0415
    from app.models.knowledge import KnowledgeSnippet  # noqa: PLC0415
    from app.services.rag import retrieve_snippets  # noqa: PLC0415

    _, files = bc.load_frozen_testsets("rag_golden", ["corpus.json", "queries.json"])
    corpus: list[dict] = files["corpus.json"]["entries"]
    queries: list[dict] = files["queries.json"]["entries"]

    dsn = f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"
    engine = sa.create_engine(dsn, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    result: dict = {"temp_db": temp_db}

    try:
        with Session(engine) as db:
            # ---- 1) 片段入库（真实 bge 向量化）----
            started = time.perf_counter()
            contents = [f"{entry['title']}：{entry['content']}" for entry in corpus]
            vectors = embed_texts(contents)
            rows: list[KnowledgeSnippet] = []
            for entry, vector in zip(corpus, vectors):
                row = KnowledgeSnippet(
                    chunk_type=ChunkType.GENERAL,
                    title=f"[TESTSET-RAG] {entry['title']}",
                    content=entry["content"],
                    embedding=[float(value) for value in vector],
                    meta={"benchmark": "rag_golden", "kb_id": entry["id"]},
                    status=SnippetStatus.ACTIVE,
                )
                db.add(row)
                rows.append(row)
            db.commit()
            result["insert_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["snippets_inserted"] = len(rows)
            result["vector_dim"] = len(vectors[0]) if vectors else None
            id_to_kb = {str(row.id): (row.meta or {}).get("kb_id") for row in rows}

            # ---- 2) HNSW 索引重建计时（迁移建好的索引定义原样重建）----
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

            # ---- 3) 检索（两轮；第一轮出排名，两轮延迟均留档）----
            latency_e2e: list[float] = []
            latency_vec: list[float] = []
            rankings: list[list[str]] = []
            for round_index in range(2):
                for index, entry in enumerate(queries):
                    started = time.perf_counter()
                    hits = retrieve_snippets(db, query_text=entry["query"], top_k=5)
                    latency_e2e.append(round((time.perf_counter() - started) * 1000, 2))
                    query_vec = embed_texts([entry["query"]])[0]
                    started = time.perf_counter()
                    hits_vec = crud_knowledge.search_snippets(db, query_vec=query_vec, k=5)
                    latency_vec.append(round((time.perf_counter() - started) * 1000, 2))
                    if round_index == 0:
                        rankings.append(
                            [id_to_kb.get(str(item["id"]), str(item["id"])) for item in hits]
                        )
            result["latency_e2e_ms"] = latency_e2e
            result["latency_vector_ms"] = latency_vec
            result["rankings_top5"] = rankings

            # ---- 4) 指标 ----
            per_query: list[dict] = []
            recall = {1: 0.0, 3: 0.0, 5: 0.0}
            reciprocal: list[float] = []
            all_hit5 = 0
            for entry, ranked in zip(queries, rankings):
                expected = set(entry["expected_ids"])
                per_query.append(
                    {
                        "id": entry["id"],
                        "query": entry["query"],
                        "expected_ids": entry["expected_ids"],
                        "top5": ranked,
                        "note": entry["note"],
                    }
                )
                for k in recall:
                    topk = set(ranked[:k])
                    recall[k] += len(expected & topk) / len(expected)
                rank = None
                for position, item in enumerate(ranked, start=1):
                    if item in expected:
                        rank = position
                        break
                reciprocal.append(1.0 / rank if rank else 0.0)
                if expected.issubset(set(ranked[:5])):
                    all_hit5 += 1
            n_queries = len(queries)
            result["recall_at"] = {str(k): round(recall[k] / n_queries, 4) for k in sorted(recall)}
            result["mrr"] = round(sum(reciprocal) / n_queries, 4)
            result["all_hit_rate_at_5"] = round(all_hit5 / n_queries, 4)
            result["per_query"] = per_query
        return result
    finally:
        engine.dispose()


def main() -> int:
    bc.ensure_dirs()
    bc.add_backend_to_path()
    bc.print_banner("测试集评测 D · RAG 检索黄金集（临时库 + pgvector）")

    try:
        manifest, files = bc.load_frozen_testsets("rag_golden", ["corpus.json", "queries.json"])
    except Exception as exc:  # noqa: BLE001
        print(f"[环境错误] {exc}")
        return bc.EXIT_FAIL

    corpus: list[dict] = files["corpus.json"]["entries"]
    queries: list[dict] = files["queries.json"]["entries"]
    print(
        f"[夹具] rag_golden v{manifest['version']}：片段 {len(corpus)} / 查询 {len(queries)}"
        "（sha256 校验通过）"
    )

    # ---- 就绪检查（项目原生 API；不加载模型、不发请求）----
    from app.agent.embedding.service import (  # noqa: PLC0415
        describe_embedding_blocker,
        describe_embedding_readiness,
    )

    readiness = describe_embedding_readiness()
    blocker = describe_embedding_blocker()
    bge_ready = blocker is None and bool(readiness.get("local_deps_installed"))
    print(f"[就绪] {json.dumps(readiness, ensure_ascii=False)}")

    metrics: list[dict] = [
        bc.metric(
            "testset_rag.corpus_total", len(corpus), "条", n=len(corpus),
            category=bc.CATEGORY_SYNTHETIC, method=METHOD,
            note="人工撰写、全 synthetic；含主题相近但答案不同的近邻对（hard negatives）",
        ),
        bc.metric(
            "testset_rag.queries_total", len(queries), "条", n=len(queries),
            category=bc.CATEGORY_SYNTHETIC, method=METHOD,
            note="每条人工标注期望命中片段 id（1-3 个）",
        ),
    ]

    detail: dict = {"readiness": readiness, "blocker": blocker, "golden": None}
    na_reason = None
    if not bge_ready:
        na_reason = (
            "本地 bge 依赖/模型未就绪："
            + json.dumps(blocker, ensure_ascii=False)
            + "；安装后重跑本脚本即产出全部质量指标"
        )

    quality_names = [
        ("testset_rag.vector_dim", "维", "实测向量维度（bge 本地）"),
        ("testset_rag.index_build_ms", "毫秒", "HNSW 余弦索引在 30 行上的重建耗时（小样本）"),
        ("testset_rag.recall_at_1", "比率", "Recall@1 = 平均 |期望∩top1|/|期望|（n=查询数）"),
        ("testset_rag.recall_at_3", "比率", "Recall@3（同上，k=3）"),
        ("testset_rag.recall_at_5", "比率", "Recall@5（同上，k=5）"),
        ("testset_rag.mrr", "比率", "MRR = 平均 1/首个命中排名（未命中记 0）"),
        ("testset_rag.all_hit_rate_at_5", "比率", "top-5 包含全部期望片段的查询占比"),
        ("testset_rag.query_latency_e2e_p50_ms", "毫秒", "端到端单查询 P50（含查询编码；n=80 次）"),
        ("testset_rag.query_latency_e2e_p95_ms", "毫秒", "端到端单查询 P95（nearest-rank）"),
        ("testset_rag.query_latency_vector_p50_ms", "毫秒", "纯向量检索 P50（不含编码；n=80 次）"),
        ("testset_rag.query_latency_vector_p95_ms", "毫秒", "纯向量检索 P95（nearest-rank）"),
    ]

    if na_reason is not None:
        print(f"[如实 N/A] {na_reason}")
        for name, unit, note in quality_names:
            metrics.append(
                bc.metric(name, None, unit, n=None, category=bc.CATEGORY_NA,
                          method=METHOD, note=f"{note} ｜ N/A 原因：{na_reason}")
            )
    else:
        temp_db = bc.temp_db_name("xhs_bench_ragtestset")
        rc_create, _ = bc.psql(f'CREATE DATABASE "{temp_db}"')
        try:
            rc_mig = 1
            if rc_create == 0:
                rc_mig, _ = bc.run_capture(
                    [str(bc.VENV_PY), "-m", "alembic", "upgrade", "head"],
                    cwd=bc.BACKEND_DIR,
                    env=bc.clean_env({"DATABASE_URL": f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"}),
                    log_path=bc.LOGS_DIR / "ragtestset_alembic_tmp.txt",
                    timeout=300,
                )
            if rc_create != 0 or rc_mig != 0:
                print("[错误] 临时库创建或迁移失败；质量指标写 N/A（如实）。")
                for name, unit, note in quality_names:
                    metrics.append(
                        bc.metric(name, None, unit, n=None, category=bc.CATEGORY_NA,
                                  method=METHOD, note=f"{note} ｜ N/A 原因：临时库创建/迁移失败")
                    )
            else:
                golden = execute_golden(temp_db)
                detail["golden"] = golden
                stats_e2e = bc.stat_block(golden["latency_e2e_ms"])
                stats_vec = bc.stat_block(golden["latency_vector_ms"])
                values = {
                    "testset_rag.vector_dim": golden.get("vector_dim"),
                    "testset_rag.index_build_ms": golden.get("index_build_ms"),
                    "testset_rag.recall_at_1": golden["recall_at"]["1"],
                    "testset_rag.recall_at_3": golden["recall_at"]["3"],
                    "testset_rag.recall_at_5": golden["recall_at"]["5"],
                    "testset_rag.mrr": golden["mrr"],
                    "testset_rag.all_hit_rate_at_5": golden["all_hit_rate_at_5"],
                    "testset_rag.query_latency_e2e_p50_ms": (stats_e2e or {}).get("p50"),
                    "testset_rag.query_latency_e2e_p95_ms": (stats_e2e or {}).get("p95"),
                    "testset_rag.query_latency_vector_p50_ms": (stats_vec or {}).get("p50"),
                    "testset_rag.query_latency_vector_p95_ms": (stats_vec or {}).get("p95"),
                }
                n_map = {
                    "testset_rag.vector_dim": 1,
                    "testset_rag.index_build_ms": 1,
                    "testset_rag.recall_at_1": len(queries),
                    "testset_rag.recall_at_3": len(queries),
                    "testset_rag.recall_at_5": len(queries),
                    "testset_rag.mrr": len(queries),
                    "testset_rag.all_hit_rate_at_5": len(queries),
                    "testset_rag.query_latency_e2e_p50_ms": len(golden["latency_e2e_ms"]),
                    "testset_rag.query_latency_e2e_p95_ms": len(golden["latency_e2e_ms"]),
                    "testset_rag.query_latency_vector_p50_ms": len(golden["latency_vector_ms"]),
                    "testset_rag.query_latency_vector_p95_ms": len(golden["latency_vector_ms"]),
                }
                for name, unit, note in quality_names:
                    metrics.append(
                        bc.metric(name, values.get(name), unit, n=n_map.get(name),
                                  category=bc.CATEGORY_SYNTHETIC, method=METHOD, note=note)
                    )
                print(
                    f"[真实] Recall@1={golden['recall_at']['1']} @3={golden['recall_at']['3']} "
                    f"@5={golden['recall_at']['5']}  MRR={golden['mrr']}  "
                    f"全命中@5={golden['all_hit_rate_at_5']}（n={len(queries)}）"
                )
        finally:
            bc.drop_temp_db(temp_db)
            residue_check = bc.db_exists(temp_db)
            rc_scan, scan_out = bc.psql(
                "SELECT datname FROM pg_database WHERE datname LIKE 'xhs_bench%'"
            )
            residue = [line for line in scan_out.strip().splitlines() if line.strip()]
            detail["cleanup"] = {
                "temp_db_leftover": bool(residue_check),
                "residue_scan": residue,
            }
            if residue_check or residue:
                print(f"[警告] 临时库残留：leftover={residue_check} scan={residue}")

    result_path = bc.write_suite_result(
        "testsets/rag_golden.json",
        suite="testset_rag_golden",
        metrics=metrics,
        detail={
            "fixture": "fixtures/testsets/rag_golden/",
            "fixture_version": manifest["version"],
            "na_reason": na_reason,
            **detail,
        },
        summary={
            "n_corpus": len(corpus),
            "n_queries": len(queries),
            "bge_ready": bge_ready,
            "recall_at_1": (detail.get("golden") or {}).get("recall_at", {}).get("1"),
            "recall_at_3": (detail.get("golden") or {}).get("recall_at", {}).get("3"),
            "recall_at_5": (detail.get("golden") or {}).get("recall_at", {}).get("5"),
            "mrr": (detail.get("golden") or {}).get("mrr"),
        },
        notes=[
            "口径：合成语料（30 条人工撰写）上的检索质量；不代表真实语料；未就绪时如实 N/A。",
            "Recall@k = 平均 |期望片段∩top-k| / |期望片段|；MRR 取首个命中排名倒数。",
            "临时库用毕即删；逐查询 top-5 明细在 detail.golden.per_query（含未命中清单）。",
        ],
    )
    print(f"结果：{result_path.relative_to(bc.ROOT)}")
    return bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
