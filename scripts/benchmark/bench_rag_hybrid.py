# -*- coding: utf-8 -*-
"""第 24 步 · RAG 检索增强对照实验（纯向量 baseline vs 向量+pg_trgm+RRF hybrid）。

设计要点（严格对照，不制造涨点）：

1. **同一次运行、同一批向量**：30 条语料只嵌入一次，两条路径共用同一份向量与
   同一个 HNSW 索引 —— 排除「两次独立跑、随机性不同」的干扰；
2. **所测即所跑**：两路都走 ``crud_knowledge.search_snippets`` 的真实 SQL 路径，
   不用 Python 替身重算相似度；
3. **临时库隔离**：``alembic upgrade head`` 建库 → 灌 30 条 → 跑完 drop；
   绝不触碰业务库 ``xhs_agent``；
4. **冻结夹具**：读 ``fixtures/testsets/rag_golden/`` 并校验 sha256，题目不改；
5. **如实 N/A**：bge 未就绪 / 临时库失败时，质量指标写 None + 原因，不伪造。

指标：Recall@1/3/5、MRR@5、nDCG@5（**二值增益**：命中记 1，未命中记 0；
测试集没有分级相关性，禁止编造 2/3 级）、全命中率@5、索引重建耗时、
查询延迟 P50/P95（工程参考，不进简历）。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\bench_rag_hybrid.py

退出码：0 = 完成（含如实 N/A）；1 = 环境错误（夹具缺失 / 指纹不符）。
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import bench_common as bc  # noqa: E402

METHOD = (
    "scripts/benchmark/bench_rag_hybrid.py：冻结黄金集（30 片段 / 40 查询，二值相关性）"
    "＋临时库（alembic 迁移）；本地 bge-large-zh-v1.5（1024 维，CPU）＋ pgvector HNSW "
    "余弦；对照 vector（单路）与 hybrid（向量 + pg_trgm word_similarity 两路，RRF k=60 融合）"
    "；类别=synthetic_fixture（合成语料，结论不代表真实语料）"
)

#: 检索返回条数（与生产 EMBEDDING_TOP_K 默认一致）
TOP_K = 5


# ---------------------------------------------------------------------------
# 指标（纯函数，便于复核）
# ---------------------------------------------------------------------------
def ndcg_at_k(ranked: list[str], expected: set[str], k: int = TOP_K) -> float:
    """二值增益 nDCG@k。

    ``DCG = Σ rel_i / log2(i+1)``（i 从 1 起），``IDCG`` 取「理想排序下前
    ``min(|expected|, k)`` 条全命中」。测试集只有 0/1 相关性，因此增益恒为 1。
    """
    gains = [1.0 if doc in expected else 0.0 for doc in ranked[:k]]
    dcg = sum(gain / math.log2(position + 1) for position, gain in enumerate(gains, start=1))
    ideal_hits = min(len(expected), k)
    idcg = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def first_hit_rank(ranked: list[str], expected: set[str]) -> int | None:
    """首个命中位置（1 起）；未命中返回 ``None``。"""
    for position, doc_id in enumerate(ranked, start=1):
        if doc_id in expected:
            return position
    return None


def metrics_for(queries: list[dict], rankings: list[list[str]]) -> dict:
    """一组排名 → Recall@1/3/5、MRR@5、nDCG@5、全命中率@5。"""
    n = len(queries)
    if n == 0:
        return {}
    recall = {1: 0.0, 3: 0.0, 5: 0.0}
    reciprocal: list[float] = []
    ndcg: list[float] = []
    all_hit = 0
    for entry, ranked in zip(queries, rankings):
        expected = set(entry["expected_ids"])
        for k in recall:
            recall[k] += len(expected & set(ranked[:k])) / len(expected)
        rank = first_hit_rank(ranked, expected)
        reciprocal.append(1.0 / rank if rank else 0.0)
        ndcg.append(ndcg_at_k(ranked, expected, TOP_K))
        if expected.issubset(set(ranked[:TOP_K])):
            all_hit += 1
    return {
        "recall_at_1": round(recall[1] / n, 4),
        "recall_at_3": round(recall[3] / n, 4),
        "recall_at_5": round(recall[5] / n, 4),
        "mrr_at_5": round(sum(reciprocal) / n, 4),
        "ndcg_at_5": round(sum(ndcg) / n, 4),
        "all_hit_at_5": round(all_hit / n, 4),
    }


# ---------------------------------------------------------------------------
# 主流程（临时库）
# ---------------------------------------------------------------------------
def execute(temp_db: str) -> dict:
    """在临时库上跑「一次嵌入、两路检索」的配对实验。"""
    import sqlalchemy as sa  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app.agent.embedding.service import embed_texts  # noqa: PLC0415
    from app.core.config import settings  # noqa: PLC0415
    from app.crud.knowledge import crud_knowledge  # noqa: PLC0415
    from app.models.enums import ChunkType, SnippetStatus  # noqa: PLC0415
    from app.models.knowledge import KnowledgeSnippet  # noqa: PLC0415

    _, files = bc.load_frozen_testsets("rag_golden", ["corpus.json", "queries.json"])
    corpus: list[dict] = files["corpus.json"]["entries"]
    queries: list[dict] = files["queries.json"]["entries"]

    dsn = f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"
    engine = sa.create_engine(dsn, pool_pre_ping=True, connect_args={"connect_timeout": 10})
    result: dict = {
        "temp_db": temp_db,
        "rrf_k": int(settings.RAG_RRF_K),
        "hybrid_pool": int(settings.RAG_HYBRID_POOL),
        "embedding_model": settings.EMBEDDING_MODEL,
        "embedding_dim": int(settings.EMBEDDING_DIM),
    }

    try:
        with Session(engine) as db:
            # ---- 1) 语料嵌入（全流程只做这一次）----
            started = time.perf_counter()
            contents = [f"{entry['title']}：{entry['content']}" for entry in corpus]
            vectors = embed_texts(contents)
            result["corpus_embed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["vector_dim"] = len(vectors[0]) if vectors else None

            # ---- 2) 入库 ----
            # 用「语料 id 派生」的确定性 UUID：避免 gen_random_uuid() 每次不同，
            # 导致同分文档按 id DESC 兜底排序时跨运行抖动（关键词路同分很常见）。
            import uuid as uuidlib  # noqa: PLC0415

            started = time.perf_counter()
            rows: list[KnowledgeSnippet] = []
            for entry, vector in zip(corpus, vectors):
                row = KnowledgeSnippet(
                    id=uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"xhs-agent/rag_golden/{entry['id']}"),
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
            id_to_kb = {str(row.id): (row.meta or {}).get("kb_id") for row in rows}

            # ---- 3) HNSW 索引重建计时（两路共用同一索引）----
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
                started = time.perf_counter()
                db.execute(sa.text(index_def))
                db.commit()
                result["index_build_ms"] = round((time.perf_counter() - started) * 1000, 1)

            # ---- 4) 查询编码预热（避免缓存命中差异污染延迟）----
            embed_texts([entry["query"] for entry in queries])
            # hybrid 首次调用会 CREATE EXTENSION，预热掉这次 DDL
            crud_knowledge.search_snippets(
                db, query_text=queries[0]["query"], k=TOP_K, retrieval_mode="hybrid"
            )
            result["trgm_ready"] = True

            # ---- 5) 逐查询两路检索（顺序固定：先 vector 后 hybrid，避免互相影响）----
            latency_vector: list[float] = []
            latency_hybrid: list[float] = []
            latency_vector_sql: list[float] = []
            vector_rankings: list[list[str]] = []
            hybrid_rankings: list[list[str]] = []
            trgm_rankings: list[list[str]] = []
            vector_sims: list[list[float | None]] = []
            hybrid_sims: list[list[float | None]] = []

            for entry in queries:
                started = time.perf_counter()
                vec_hits = crud_knowledge.search_snippets(
                    db, query_text=entry["query"], k=TOP_K, retrieval_mode="vector"
                )
                latency_vector.append(round((time.perf_counter() - started) * 1000, 2))

                query_vec = embed_texts([entry["query"]])[0]
                started = time.perf_counter()
                sql_hits = crud_knowledge.search_snippets(
                    db, query_vec=query_vec, k=TOP_K, retrieval_mode="vector"
                )
                latency_vector_sql.append(round((time.perf_counter() - started) * 1000, 2))

                started = time.perf_counter()
                hyb_hits = crud_knowledge.search_snippets(
                    db, query_text=entry["query"], k=TOP_K, retrieval_mode="hybrid"
                )
                latency_hybrid.append(round((time.perf_counter() - started) * 1000, 2))

                vector_rankings.append([id_to_kb.get(str(h["id"]), str(h["id"])) for h in vec_hits])
                hybrid_rankings.append([id_to_kb.get(str(h["id"]), str(h["id"])) for h in hyb_hits])
                # 诊断用：关键字路单独排名（与 hybrid 内部同一条 SQL，不经融合）
                trgm_hits = crud_knowledge._search_trgm(  # noqa: SLF001 —— 评测专用的只读诊断
                    db, text=entry["query"], k=TOP_K, chunk_types=None, include_archived=False
                )
                trgm_rankings.append([id_to_kb.get(str(h["id"]), str(h["id"])) for h in trgm_hits])
                vector_sims.append([h.get("similarity") for h in vec_hits])
                hybrid_sims.append([h.get("similarity") for h in hyb_hits])

                if [h["id"] for h in vec_hits] != [h["id"] for h in sql_hits]:
                    result.setdefault("warnings", []).append(
                        f"{entry['id']}：query_text 与 query_vec 两路结果不一致"
                    )

            result["latency_vector_ms"] = latency_vector
            result["latency_vector_sql_ms"] = latency_vector_sql
            result["latency_hybrid_ms"] = latency_hybrid
            result["vector_rankings"] = vector_rankings
            result["hybrid_rankings"] = hybrid_rankings
            result["trgm_rankings"] = trgm_rankings
            result["vector_similarities"] = vector_sims
            result["hybrid_similarities"] = hybrid_sims
            result["vector_metrics"] = metrics_for(queries, vector_rankings)
            result["hybrid_metrics"] = metrics_for(queries, hybrid_rankings)
            result["trgm_metrics"] = metrics_for(queries, trgm_rankings)

            # ---- 6) 逐查询配对差异（救回 / 挤出）----
            gained: list[dict] = []
            lost: list[dict] = []
            rank_up: list[dict] = []
            rank_down: list[dict] = []
            for entry, vec_ranked, hyb_ranked in zip(queries, vector_rankings, hybrid_rankings):
                expected = set(entry["expected_ids"])
                vec_recall = len(expected & set(vec_ranked[:TOP_K])) / len(expected)
                hyb_recall = len(expected & set(hyb_ranked[:TOP_K])) / len(expected)
                vec_first = first_hit_rank(vec_ranked, expected)
                hyb_first = first_hit_rank(hyb_ranked, expected)
                item = {
                    "id": entry["id"],
                    "query": entry["query"],
                    "expected_ids": entry["expected_ids"],
                    "vector_top5": vec_ranked,
                    "hybrid_top5": hyb_ranked,
                    "vector_recall_at_5": round(vec_recall, 4),
                    "hybrid_recall_at_5": round(hyb_recall, 4),
                    "vector_first_hit": vec_first,
                    "hybrid_first_hit": hyb_first,
                    "note": entry.get("note", ""),
                }
                if hyb_recall > vec_recall:
                    gained.append(item)
                elif hyb_recall < vec_recall:
                    lost.append(item)
                elif vec_first != hyb_first:
                    if vec_first is None or (hyb_first is not None and hyb_first < vec_first):
                        rank_up.append(item)
                    else:
                        rank_down.append(item)

            result["comparison"] = {
                "gained": gained,
                "lost": lost,
                "rank_improved": rank_up,
                "rank_worsened": rank_down,
                "n_gained": len(gained),
                "n_lost": len(lost),
                "n_rank_improved": len(rank_up),
                "n_rank_worsened": len(rank_down),
            }
        return result
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _metric_block(prefix: str, metrics: dict, timing: dict, n: int) -> list[dict]:
    """把一组指标转成 benchmark 统一 metric 结构。"""
    return [
        bc.metric(f"{prefix}.recall_at_1", metrics.get("recall_at_1"), "比率", n=n,
                  category=bc.CATEGORY_SYNTHETIC, method=METHOD,
                  note="Recall@1 = 平均 |期望∩top1|/|期望|（二值相关性）"),
        bc.metric(f"{prefix}.recall_at_3", metrics.get("recall_at_3"), "比率", n=n,
                  category=bc.CATEGORY_SYNTHETIC, method=METHOD, note="Recall@3"),
        bc.metric(f"{prefix}.recall_at_5", metrics.get("recall_at_5"), "比率", n=n,
                  category=bc.CATEGORY_SYNTHETIC, method=METHOD, note="Recall@5"),
        bc.metric(f"{prefix}.mrr_at_5", metrics.get("mrr_at_5"), "比率", n=n,
                  category=bc.CATEGORY_SYNTHETIC, method=METHOD,
                  note="MRR@5 = 平均 1/首个命中排名（top-5 内未命中记 0）"),
        bc.metric(f"{prefix}.ndcg_at_5", metrics.get("ndcg_at_5"), "比率", n=n,
                  category=bc.CATEGORY_SYNTHETIC, method=METHOD,
                  note="nDCG@5（二值增益：命中=1，未命中=0；IDCG 按 min(|期望|,5) 计）"),
        bc.metric(f"{prefix}.all_hit_at_5", metrics.get("all_hit_at_5"), "比率", n=n,
                  category=bc.CATEGORY_SYNTHETIC, method=METHOD,
                  note="top-5 包含全部期望片段的查询占比"),
        bc.metric(f"{prefix}.index_build_ms", timing.get("index_build_ms"), "毫秒", n=1,
                  category=bc.CATEGORY_SYNTHETIC, method=METHOD,
                  note="HNSW 余弦索引在 30 行上的重建耗时（两路共用同一索引）"),
        bc.metric(f"{prefix}.query_latency_p50_ms", (bc.stat_block(timing.get("latency") or []) or {}).get("p50"),
                  "毫秒", n=len(timing.get("latency") or []), category=bc.CATEGORY_SYNTHETIC,
                  method=METHOD, note="端到端单查询 P50（含查询编码；工程参考，不进简历）"),
        bc.metric(f"{prefix}.query_latency_p95_ms", (bc.stat_block(timing.get("latency") or []) or {}).get("p95"),
                  "毫秒", n=len(timing.get("latency") or []), category=bc.CATEGORY_SYNTHETIC,
                  method=METHOD, note="端到端单查询 P95（nearest-rank；工程参考，不进简历）"),
    ]


def main() -> int:
    bc.ensure_dirs()
    bc.add_backend_to_path()
    bc.print_banner("第 24 步 · RAG 检索增强对照（vector vs hybrid）")

    try:
        manifest, files = bc.load_frozen_testsets("rag_golden", ["corpus.json", "queries.json"])
    except Exception as exc:  # noqa: BLE001
        print(f"[环境错误] {exc}")
        return bc.EXIT_FAIL

    corpus = files["corpus.json"]["entries"]
    queries = files["queries.json"]["entries"]
    print(f"[夹具] rag_golden v{manifest['version']}：片段 {len(corpus)} / 查询 {len(queries)}（sha256 通过）")

    from app.agent.embedding.service import (  # noqa: PLC0415
        describe_embedding_blocker,
        describe_embedding_readiness,
    )

    readiness = describe_embedding_readiness()
    blocker = describe_embedding_blocker()
    bge_ready = blocker is None and bool(readiness.get("local_deps_installed"))
    print(f"[就绪] {readiness}")

    if not bge_ready:
        reason = f"bge 未就绪：{blocker}"
        print(f"[如实 N/A] {reason}")
        for tag, filename, suite in (
            ("baseline", "testsets/rag_golden_baseline.json", "testset_rag_baseline"),
            ("hybrid", "testsets/rag_golden_hybrid.json", "testset_rag_hybrid"),
        ):
            metrics = [
                bc.metric(f"{'testset_rag_baseline' if tag == 'baseline' else 'testset_rag_hybrid'}.{name}",
                          None, unit, n=None, category=bc.CATEGORY_NA, method=METHOD,
                          note=f"{note} ｜ N/A 原因：{reason}")
                for name, unit, note in (
                    ("recall_at_1", "比率", "Recall@1"), ("recall_at_3", "比率", "Recall@3"),
                    ("recall_at_5", "比率", "Recall@5"), ("mrr_at_5", "比率", "MRR@5"),
                    ("ndcg_at_5", "比率", "nDCG@5"), ("all_hit_at_5", "比率", "全命中率@5"),
                    ("index_build_ms", "毫秒", "索引重建"), ("query_latency_p50_ms", "毫秒", "P50"),
                    ("query_latency_p95_ms", "毫秒", "P95"),
                )
            ]
            bc.write_suite_result(filename, suite=suite, metrics=metrics,
                                  detail={"fixture_version": manifest["version"], "na_reason": reason},
                                  summary={"bge_ready": False, "n_queries": len(queries)},
                                  notes=["bge 未就绪：全部质量指标如实 N/A，不使用假向量。"])
        return bc.EXIT_OK

    temp_db = bc.temp_db_name("xhs_bench_ragcmp")
    rc_create, _ = bc.psql(f'CREATE DATABASE "{temp_db}"')
    try:
        rc_mig = 1
        if rc_create == 0:
            rc_mig, _ = bc.run_capture(
                [str(bc.VENV_PY), "-m", "alembic", "upgrade", "head"],
                cwd=bc.BACKEND_DIR,
                env=bc.clean_env({"DATABASE_URL": f"postgresql+psycopg://postgres:postgres@127.0.0.1:5432/{temp_db}"}),
                log_path=bc.LOGS_DIR / "ragcmp_alembic_tmp.txt",
                timeout=300,
            )
        if rc_create != 0 or rc_mig != 0:
            print("[错误] 临时库创建或迁移失败（如实 N/A）")
            return bc.EXIT_FAIL

        data = execute(temp_db)
        v = data["vector_metrics"]
        h = data["hybrid_metrics"]
        print("")
        print(f"[vector] Recall@1={v['recall_at_1']} @3={v['recall_at_3']} @5={v['recall_at_5']} "
              f"MRR@5={v['mrr_at_5']} nDCG@5={v['ndcg_at_5']} 全命中@5={v['all_hit_at_5']}")
        print(f"[hybrid] Recall@1={h['recall_at_1']} @3={h['recall_at_3']} @5={h['recall_at_5']} "
              f"MRR@5={h['mrr_at_5']} nDCG@5={h['ndcg_at_5']} 全命中@5={h['all_hit_at_5']}")
        t = data["trgm_metrics"]
        print(f"[trgm仅] Recall@1={t['recall_at_1']} @3={t['recall_at_3']} @5={t['recall_at_5']} "
              f"MRR@5={t['mrr_at_5']} nDCG@5={t['ndcg_at_5']}（诊断用，非主结论）")
        cmp = data["comparison"]
        print(f"[配对] 救回 {cmp['n_gained']} / 挤出 {cmp['n_lost']} / "
              f"排名改善 {cmp['n_rank_improved']} / 排名下降 {cmp['n_rank_worsened']}（n={len(queries)}）")

        n = len(queries)
        common_detail = {
            "fixture_version": manifest["version"],
            "fixture_sha256": manifest["sha256"],
            "corpus_total": len(corpus),
            "rrf_k": data["rrf_k"],
            "hybrid_pool": data["hybrid_pool"],
            "vector_dim": data["vector_dim"],
            "trgm_ready": data["trgm_ready"],
            "warnings": data.get("warnings", []),
        }

        baseline_path = bc.write_suite_result(
            "testsets/rag_golden_baseline.json",
            suite="testset_rag_baseline",
            metrics=_metric_block(
                "testset_rag_baseline", v,
                {"index_build_ms": data.get("index_build_ms"), "latency": data["latency_vector_ms"]}, n,
            ),
            detail={**common_detail, "retrieval_mode": "vector",
                    "per_query": [
                        {"id": q["id"], "query": q["query"], "expected_ids": q["expected_ids"],
                         "top5": r, "first_hit": first_hit_rank(r, set(q["expected_ids"])),
                         "ndcg_at_5": round(ndcg_at_k(r, set(q["expected_ids"])), 4),
                         "similarities": s}
                        for q, r, s in zip(queries, data["vector_rankings"], data["vector_similarities"])
                    ]},
            summary={"n_corpus": len(corpus), "n_queries": n, "retrieval_mode": "vector", **v},
            notes=[
                "before 基线：纯 pgvector 余弦 top-5（HNSW, m=16/ef_construction=64, ef_search=40）。",
                "相关性为二值：nDCG 用 gain=1/0，未编造分级。",
                "语料为 30 条 synthetic，结论不代表真实语料检索质量。",
            ],
            env_extra={"embedding_model": data["embedding_model"], "embedding_device": "cpu"},
        )

        hybrid_path = bc.write_suite_result(
            "testsets/rag_golden_hybrid.json",
            suite="testset_rag_hybrid",
            metrics=_metric_block(
                "testset_rag_hybrid", h,
                {"index_build_ms": data.get("index_build_ms"), "latency": data["latency_hybrid_ms"]}, n,
            ),
            detail={**common_detail, "retrieval_mode": "hybrid",
                    "per_query": [
                        {"id": q["id"], "query": q["query"], "expected_ids": q["expected_ids"],
                         "top5": r, "first_hit": first_hit_rank(r, set(q["expected_ids"])),
                         "ndcg_at_5": round(ndcg_at_k(r, set(q["expected_ids"])), 4),
                         "similarities": s}
                        for q, r, s in zip(queries, data["hybrid_rankings"], data["hybrid_similarities"])
                    ],
                    "comparison": {
                        "n_gained": cmp["n_gained"], "n_lost": cmp["n_lost"],
                        "n_rank_improved": cmp["n_rank_improved"],
                        "n_rank_worsened": cmp["n_rank_worsened"],
                        "gained": cmp["gained"], "lost": cmp["lost"],
                        "rank_improved": cmp["rank_improved"], "rank_worsened": cmp["rank_worsened"],
                    },
                    "baseline_metrics": v,
                    "trgm_only_metrics": data["trgm_metrics"],
                    "latency_vector_sql_ms": data["latency_vector_sql_ms"]},
            summary={"n_corpus": len(corpus), "n_queries": n, "retrieval_mode": "hybrid", **h},
            notes=[
                "after：向量 + pg_trgm word_similarity 两路，RRF(k=60) 融合，各取 top-20 后取 top-5。",
                "pg_trgm 提供的是三元组相似度，**不是 BM25**（无 IDF、无词频饱和）。",
                "语料为 30 条 synthetic，n=40，单条命中≈2.5pp，只做方向性结论。",
            ],
            env_extra={"embedding_model": data["embedding_model"], "embedding_device": "cpu"},
        )

        print(f"结果：{baseline_path.relative_to(bc.ROOT)}")
        print(f"结果：{hybrid_path.relative_to(bc.ROOT)}")
        return bc.EXIT_OK
    finally:
        bc.drop_temp_db(temp_db)
        leftover = bc.db_exists(temp_db)
        rc_scan, scan_out = bc.psql("SELECT datname FROM pg_database WHERE datname LIKE 'xhs_bench%'")
        residue = [line for line in scan_out.strip().splitlines() if line.strip()]
        if leftover or residue:
            print(f"[警告] 临时库残留：leftover={leftover} scan={residue}")
        else:
            print("[清理] 临时库已删除，无 xhs_bench* 残留")


if __name__ == "__main__":
    sys.exit(main())
