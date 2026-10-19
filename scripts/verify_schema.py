"""第 7 步 · 在线 schema 验收脚本（需要真实 PostgreSQL 连通）。

做的事（只读，除向量测试用事务且必然回滚）：

1. 断言 public schema 下**恰好 10 张业务表**且表名与约定一致；
2. 逐条核对**外键**及其 ON DELETE 策略；
3. 核对**唯一约束**与**普通索引**是否齐全（含 DESC 排序）；
4. 核对 ``knowledge_snippets.embedding`` 为 ``vector(1024)``，
   且存在 ``USING hnsw (...) vector_cosine_ops`` 索引；
5. **向量可用性**：在事务内插入 3 条 ``vector(1024)`` 测试数据，
   用 ``ORDER BY embedding <=> :q LIMIT`` 验证能按余弦距离排序，随后**回滚**不留垃圾。

用法（在**项目根目录**下，用后端 venv 的解释器）：

    backend\\.venv\\Scripts\\python.exe scripts\\verify_schema.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 本脚本位于 <项目根>/scripts/，backend 包在同级 backend/ 下
ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.db.session import engine  # noqa: E402
from app.models import EMBEDDING_DIM  # noqa: E402

EXPECTED_TABLES = {
    "accounts",
    "personas",
    "hotspots",
    "topics",
    "notes",
    "covers",
    "note_metrics",
    "diagnostics",
    "knowledge_snippets",
    "agent_runs",
}

# (表, 列) -> 期望的 ON DELETE 行为
EXPECTED_FK_ONDELETE = {
    ("personas", "account_id"): "SET NULL",
    ("topics", "account_id"): "SET NULL",
    ("topics", "persona_id"): "SET NULL",
    ("notes", "account_id"): "RESTRICT",
    ("notes", "persona_id"): "SET NULL",
    ("notes", "topic_id"): "SET NULL",
    ("agent_runs", "account_id"): "SET NULL",
    ("agent_runs", "persona_id"): "SET NULL",
    ("agent_runs", "note_id"): "SET NULL",
    ("agent_runs", "topic_id"): "SET NULL",
    ("covers", "note_id"): "CASCADE",
    ("note_metrics", "note_id"): "CASCADE",
    ("diagnostics", "note_id"): "CASCADE",
    ("diagnostics", "based_on_metric_id"): "SET NULL",
}

EXPECTED_UNIQUE = {
    ("accounts", "xhs_red_id"),
    ("hotspots", "content_hash"),
    ("note_metrics", "note_id,captured_at"),
}

# ---------------------------------------------------------------------------
# 框架托管表（LangGraph 检查点）——**不是业务表**，必须排除在「恰好 10 张业务表」之外。
#
# 这些表由 langgraph-checkpoint-postgres 的 PostgresSaver.setup() 幂等创建/升级，
# **不写进业务 Alembic 迁移**。本仓库把它们隔离在独立 schema（默认 langgraph）里，
# 因此 public 里本来就不会有；下面的排除清单是**第二道保险**：
# 万一将来改成建在 public，业务表断言依然成立、并且能在报告里看到它们落在哪。
# ---------------------------------------------------------------------------
FRAMEWORK_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)

EXPECTED_INDEXES = {
    "ix_personas_account_id",
    "ix_hotspots_source_hot_date",
    "ix_hotspots_status",
    "ix_hotspots_fetched_at",
    "ix_topics_status_account_id",
    "ix_topics_score",
    "ix_topics_generated_at",
    "ix_notes_account_id_status",
    "ix_notes_status_published_at",
    "ix_notes_topic_id",
    "ix_notes_xhs_note_id",
    "ix_covers_note_id",
    "ix_note_metrics_note_id_captured_at",
    "ix_diagnostics_note_id_created_at",
    "ix_knowledge_snippets_chunk_type",
    "ix_knowledge_snippets_embedding_hnsw",
    "ix_agent_runs_run_type_created_at",
    "ix_agent_runs_status",
}

# pg_constraint.confdeltype 编码
CONFDEL_MAP = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}


class Checker:
    """极简的 PASS/FAIL 收集器。"""

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


def _base_vector(idx: int) -> str:
    """构造一个 1024 维 one-hot 向量的字面量（用于 CAST(x AS vector)）。"""
    values = ["0"] * EMBEDDING_DIM
    values[idx] = "1"
    return "[" + ",".join(values) + "]"


def main() -> int:
    c = Checker()

    with engine.connect() as conn:
        # ---------------------------------------------------------------
        # 1) 表清单
        # ---------------------------------------------------------------
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version' "
                "AND table_name <> ALL(:framework)"
            ),
            {"framework": list(FRAMEWORK_TABLES)},
        ).scalars().all()
        actual_tables = set(rows)
        c.check(
            actual_tables == EXPECTED_TABLES,
            f"恰好 {len(EXPECTED_TABLES)} 张业务表且表名齐全",
            f"实际={sorted(actual_tables)} 缺失={sorted(EXPECTED_TABLES - actual_tables)} 多余={sorted(actual_tables - EXPECTED_TABLES)}",
        )

        # ------------------------------------------------ 1b) 框架托管表落点登记
        framework_rows = conn.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_name = ANY(:framework) ORDER BY table_schema, table_name"
            ),
            {"framework": list(FRAMEWORK_TABLES)},
        ).all()
        framework_schemas = sorted({r[0] for r in framework_rows})
        in_public = [r[1] for r in framework_rows if r[0] == "public"]
        if not framework_rows:
            print("[INFO] 框架检查点表尚未创建（未运行过 Agent Harness 的 setup()），跳过隔离登记")
        else:
            c.check(
                not in_public,
                "框架检查点表未落在 public（已被排除在业务表断言之外）",
                f"落到 public 的框架表={in_public}",
            )
            print(f"[INFO] 框架托管表 {len(framework_rows)} 张，位于 schema={framework_schemas}")

        # ---------------------------------------------------------------
        # 2) 外键 + ON DELETE
        # ---------------------------------------------------------------
        fk_rows = conn.execute(
            text(
                """
                SELECT tc.table_name, kcu.column_name, c.confdeltype
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN pg_class rel ON rel.relname = tc.table_name
                JOIN pg_constraint c ON c.conname = tc.constraint_name AND c.conrelid = rel.oid
                WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
                """
            )
        ).all()
        actual_fk = {(t, col): CONFDEL_MAP.get(dt, dt) for t, col, dt in fk_rows}
        c.check(
            actual_fk == EXPECTED_FK_ONDELETE,
            f"外键及其 ON DELETE 策略全部正确（{len(EXPECTED_FK_ONDELETE)} 条）",
            f"实际={actual_fk}",
        )

        # ---------------------------------------------------------------
        # 3) 唯一约束
        # ---------------------------------------------------------------
        uq_rows = conn.execute(
            text(
                """
                SELECT tc.table_name,
                       string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) AS cols
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'UNIQUE' AND tc.table_schema = 'public'
                GROUP BY tc.table_name, tc.constraint_name
                """
            )
        ).all()
        actual_uq = {(t, cols) for t, cols in uq_rows}
        c.check(
            actual_uq == EXPECTED_UNIQUE,
            f"唯一约束齐全（{len(EXPECTED_UNIQUE)} 条）",
            f"实际={sorted(actual_uq)}",
        )

        # ---------------------------------------------------------------
        # 4) 普通索引（含 HNSW）
        # ---------------------------------------------------------------
        idx_rows = conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        ).scalars().all()
        actual_idx = set(idx_rows)
        c.check(
            EXPECTED_INDEXES <= actual_idx,
            f"索引齐全（{len(EXPECTED_INDEXES)} 个）",
            f"缺失={sorted(EXPECTED_INDEXES - actual_idx)}",
        )

        # DESC 排序索引：确认 pg_get_indexdef 里带 DESC
        desc_idx = {}
        for name in ("ix_topics_score", "ix_note_metrics_note_id_captured_at", "ix_agent_runs_run_type_created_at"):
            ddl = conn.execute(
                text("SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass(:n)"),
                {"n": name},
            ).scalar()
            desc_idx[name] = bool(ddl and "DESC" in ddl.upper())
        c.check(all(desc_idx.values()), "DESC 排序索引定义正确", f"{desc_idx}")

        # HNSW + vector_cosine_ops
        hnsw_ddl = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND indexname='ix_knowledge_snippets_embedding_hnsw'"
            )
        ).scalar()
        c.check(
            bool(hnsw_ddl) and "hnsw" in hnsw_ddl.lower() and "vector_cosine_ops" in hnsw_ddl.lower(),
            "HNSW(vector_cosine_ops) 索引存在",
            str(hnsw_ddl),
        )

        # ---------------------------------------------------------------
        # 5) embedding 列类型为 vector(1024)
        # ---------------------------------------------------------------
        emb_type = conn.execute(
            text(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                WHERE c.relname = 'knowledge_snippets' AND a.attname = 'embedding'
                """
            )
        ).scalar()
        c.check(
            emb_type == f"vector({EMBEDDING_DIM})",
            f"knowledge_snippets.embedding 类型为 vector({EMBEDDING_DIM})",
            f"实际={emb_type}",
        )

        # pgvector 扩展版本
        ext_ver = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        c.check(bool(ext_ver), "pgvector 扩展已启用", f"版本={ext_ver}")

        # ---------------------------------------------------------------
        # 6) 向量可用性（事务内插入 → 查询 → 回滚）
        # ---------------------------------------------------------------
        # 前面的只读查询已自动开启事务，先结束它，再开新事务做写入测试。
        conn.rollback()
        try:
            samples = [
                ("向量测试A(e0)", _base_vector(0)),
                ("向量测试B(e1)", _base_vector(1)),
                ("向量测试C(e0/e1 混合)", None),
            ]
            # C：0.5*e0 + 0.5*e1
            mixed = ["0"] * EMBEDDING_DIM
            mixed[0] = "0.5"
            mixed[1] = "0.5"
            samples[2] = (samples[2][0], "[" + ",".join(mixed) + "]")

            for content, vec in samples:
                conn.execute(
                    text(
                        "INSERT INTO knowledge_snippets (chunk_type, content, embedding, status) "
                        "VALUES ('general', :c, CAST(:e AS vector), 'active')"
                    ),
                    {"c": content, "e": vec},
                )

            q = _base_vector(0)  # 查询向量 = e0
            ordered = conn.execute(
                text(
                    "SELECT content, embedding <=> CAST(:q AS vector) AS dist "
                    "FROM knowledge_snippets WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 3"
                ),
                {"q": q},
            ).all()
            order = [r[0] for r in ordered]
            dists = [round(float(r[1]), 4) for r in ordered]

            c.check(len(order) == 3, "向量检索返回 3 条", f"{order}")
            c.check(
                order[0] == "向量测试A(e0)" and order[-1] == "向量测试B(e1)",
                "余弦距离排序正确（最近=同向 e0，最远=正交 e1）",
                f"order={order} dist={dists}",
            )
            c.check(
                dists[0] < dists[1] < dists[2],
                "距离单调递增",
                f"dist={dists}",
            )
        finally:
            conn.rollback()
        # 回滚后确认没有残留测试数据
        leftover = conn.execute(
            text("SELECT count(*) FROM knowledge_snippets WHERE content LIKE '向量测试%'")
        ).scalar()
        c.check(leftover == 0, "向量测试数据已回滚，无残留", f"残留={leftover}")

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for f in c.failures:
            print(f"  - FAIL: {f}")
        return 1
    print("全部 PASS ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
