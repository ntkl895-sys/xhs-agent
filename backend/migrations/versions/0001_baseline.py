"""baseline: 核心 10 张表 + pgvector 扩展

Revision ID: 0001
Revises:
Create Date: 2026-10-10

第 7 步基线迁移。本迁移**一次性**建立内容运营所需的核心 10 张表：

    accounts, personas, hotspots, topics, notes, covers,
    note_metrics, diagnostics, knowledge_snippets, agent_runs

设计要点：

- 先 ``CREATE EXTENSION IF NOT EXISTS vector``（``knowledge_snippets.embedding`` 依赖该类型）；
- 枚举一律以 ``VARCHAR`` 存储（``native_enum=False``，无 PG 原生 ENUM、无 CHECK 约束），
  后期新增取值不需要迁移；
- 时间戳统一 ``timestamptz``；主键 UUID 默认 ``gen_random_uuid()``；
- 半结构化字段统一 ``JSONB``；
- 外键删除策略：account 被业务引用时 ``RESTRICT``、审计/上下文 ``SET NULL``、
  cover/note_metrics/diagnostic → note 用 ``CASCADE``；
- 最后建 ``knowledge_snippets.embedding`` 的 HNSW + 余弦距离索引；
- ``downgrade`` 逆序删表，并 ``DROP EXTENSION``，保证 ``downgrade base`` 干净。

注意：本文件由 ``alembic revision --autogenerate`` 生成后人工修订（补充扩展语句与类型导入）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """建立 pgvector 扩展与全部 10 张核心表。"""

    # ------------------------------------------------------------------
    # 0) pgvector 扩展（必须先于 knowledge_snippets 建表）
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------------
    # 1) accounts —— 小红书账号（不含任何登录态）
    # ------------------------------------------------------------------
    op.create_table(
        "accounts",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False, comment="账号备注名（用户自定义，便于识别）"),
        sa.Column("xhs_red_id", sa.String(length=64), nullable=True, comment="用户手填的小红书号；不自动抓取；唯一但允许多个 NULL"),
        sa.Column("niche", sa.String(length=100), nullable=True, comment="账号领域，如「职场」「家居」"),
        sa.Column("status", sa.Enum("active", "archived", name="accountstatus", native_enum=False, length=20), server_default="active", nullable=False, comment="active / archived（归档代替物理删除）"),
        sa.Column("remark", sa.Text(), nullable=True, comment="备注"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("xhs_red_id", name=op.f("uq_accounts_xhs_red_id")),
    )

    # ------------------------------------------------------------------
    # 2) hotspots —— 热搜快照（第 9 步写入）
    # ------------------------------------------------------------------
    op.create_table(
        "hotspots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, comment="zhihu / douyin / bilibili / toutiao / qq-news ..."),
        sa.Column("source_item_id", sa.String(length=128), nullable=True, comment="上游条目标识"),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("mobile_url", sa.Text(), nullable=True),
        sa.Column("hot_score", sa.BigInteger(), nullable=True, comment="热度值"),
        sa.Column("rank_position", sa.Integer(), nullable=True, comment="榜单名次"),
        sa.Column("cover_url", sa.Text(), nullable=True, comment="封面图（默认不采集）"),
        sa.Column("summary", sa.Text(), nullable=True, comment="上游摘要 desc（默认不采集）"),
        sa.Column("author", sa.String(length=128), nullable=True, comment="作者（默认不采集）"),
        sa.Column("raw_extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="仅保留最小集：id / mobileUrl / timestamp"),
        sa.Column("hot_date", sa.Date(), nullable=True, comment="归属日期 / 批次"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True, comment="采集时间"),
        sa.Column("content_hash", sa.String(length=64), nullable=False, comment="source+source_item_id+hot_date 归一化哈希"),
        sa.Column("status", sa.Enum("new", "adopted", "ignored", name="hotspotstatus", native_enum=False, length=20), server_default="new", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hotspots")),
        sa.UniqueConstraint("content_hash", name="uq_hotspot_hash"),
    )
    op.create_index("ix_hotspots_source_hot_date", "hotspots", ["source", "hot_date"], unique=False)
    op.create_index("ix_hotspots_status", "hotspots", ["status"], unique=False)
    op.create_index("ix_hotspots_fetched_at", "hotspots", ["fetched_at"], unique=False)

    # ------------------------------------------------------------------
    # 3) knowledge_snippets —— 向量知识块（第 18/19 步写入向量）
    # ------------------------------------------------------------------
    op.create_table(
        "knowledge_snippets",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("chunk_type", sa.Enum("hit_pattern", "forbidden_word", "persona_knowledge", "topic_template", "review_insight", "general", name="chunktype", native_enum=False, length=32), nullable=False),
        sa.Column("ref_type", sa.Enum("note", "diagnostic", "topic", "manual_doc", name="reftype", native_enum=False, length=32), nullable=True, comment="多态来源类型（弱关联）"),
        sa.Column("ref_id", sa.UUID(), nullable=True, comment="多态来源 id；不加强外键"),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(dim=1024), nullable=True, comment="第 18 步写入；本步只建列（EMBEDDING_DIM=1024 占位）"),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.Enum("active", "archived", name="snippetstatus", native_enum=False, length=20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_snippets")),
    )
    op.create_index("ix_knowledge_snippets_chunk_type", "knowledge_snippets", ["chunk_type"], unique=False)
    op.create_index(
        "ix_knowledge_snippets_embedding_hnsw",
        "knowledge_snippets",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # ------------------------------------------------------------------
    # 4) personas —— 写作人设（account_id NULL = 公共人设库）
    # ------------------------------------------------------------------
    op.create_table(
        "personas",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True, comment="归属账号；NULL = 公共人设库"),
        sa.Column("name", sa.String(length=100), nullable=False, comment="人设名"),
        sa.Column("positioning", sa.Text(), nullable=True, comment="我是谁 / 目标读者 / 价值主张"),
        sa.Column("target_audience", sa.Text(), nullable=True, comment="目标读者"),
        sa.Column("tone", sa.String(length=200), nullable=True, comment="语气风格"),
        sa.Column("content_pillars", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="内容支柱数组"),
        sa.Column("taboo", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="人设禁忌数组"),
        sa.Column("signature_style", sa.Text(), nullable=True, comment="口头禅 / 标志性表达"),
        sa.Column("system_prompt", sa.Text(), nullable=True, comment="第 12 步用的人设提示词模板"),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Enum("active", "archived", name="personastatus", native_enum=False, length=20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name=op.f("fk_personas_account_id_accounts"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personas")),
    )
    op.create_index("ix_personas_account_id", "personas", ["account_id"], unique=False)

    # ------------------------------------------------------------------
    # 5) topics —— 选题库
    # ------------------------------------------------------------------
    op.create_table(
        "topics",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("persona_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("angle", sa.Text(), nullable=True, comment="切入角度"),
        sa.Column("summary", sa.Text(), nullable=True, comment="为什么写 / 写给谁 / 核心观点"),
        sa.Column("pillar", sa.String(length=100), nullable=True, comment="内容支柱"),
        sa.Column("source_hotspot_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="来源热点 id 数组"),
        sa.Column("source_trace", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="溯源：平台 + 热点标题"),
        sa.Column("target_keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("heat_score", sa.Float(), nullable=True),
        sa.Column("difficulty", sa.Enum("easy", "medium", "hard", name="topicdifficulty", native_enum=False, length=10), nullable=True),
        sa.Column("score", sa.Float(), nullable=True, comment="综合排序分"),
        sa.Column("llm_model", sa.String(length=100), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="生成元数据，含 json_parse_attempts 等"),
        sa.Column("review_note", sa.Text(), nullable=True, comment="人工评审备注"),
        sa.Column("status", sa.Enum("draft", "approved", "writing", "published", "discarded", name="topicstatus", native_enum=False, length=20), server_default="draft", nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name=op.f("fk_topics_account_id_accounts"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["persona_id"], ["personas.id"], name=op.f("fk_topics_persona_id_personas"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topics")),
    )
    op.create_index("ix_topics_status_account_id", "topics", ["status", "account_id"], unique=False)
    op.create_index("ix_topics_score", "topics", [sa.literal_column("score DESC")], unique=False)
    op.create_index("ix_topics_generated_at", "topics", ["generated_at"], unique=False)

    # ------------------------------------------------------------------
    # 6) notes —— 笔记作品（account_id RESTRICT）
    # ------------------------------------------------------------------
    op.create_table(
        "notes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False, comment="ON DELETE RESTRICT：引导用 archived 归档，禁止物理删除有数据的账号"),
        sa.Column("persona_id", sa.UUID(), nullable=True),
        sa.Column("topic_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("body", sa.Text(), nullable=True, comment="正文"),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="话题标签数组"),
        sa.Column("content_check", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="违禁词命中与修改建议"),
        sa.Column("compliance_status", sa.Enum("pending", "passed", "warning", name="compliancestatus", native_enum=False, length=20), server_default="pending", nullable=False),
        sa.Column("status", sa.Enum("draft", "ready", "published", "archived", name="notestatus", native_enum=False, length=20), server_default="draft", nullable=False),
        sa.Column("publish_url", sa.Text(), nullable=True, comment="人工发布后回填的作品链接"),
        sa.Column("xhs_note_id", sa.String(length=64), nullable=True, comment="手填或截图识别的小红书笔记 id"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True, comment="人工标记的发布时间"),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("llm_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name=op.f("fk_notes_account_id_accounts"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["persona_id"], ["personas.id"], name=op.f("fk_notes_persona_id_personas"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], name=op.f("fk_notes_topic_id_topics"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )
    op.create_index("ix_notes_account_id_status", "notes", ["account_id", "status"], unique=False)
    op.create_index("ix_notes_status_published_at", "notes", ["status", "published_at"], unique=False)
    op.create_index("ix_notes_topic_id", "notes", ["topic_id"], unique=False)
    op.create_index("ix_notes_xhs_note_id", "notes", ["xhs_note_id"], unique=False)

    # ------------------------------------------------------------------
    # 7) agent_runs —— Agent 运行审计（trigger 为 PG 保留字，需引号）
    # ------------------------------------------------------------------
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_type", sa.Enum("topic_generation", "content_creation", "diagnosis", "embedding", "other", name="runtype", native_enum=False, length=32), nullable=False),
        sa.Column("trigger", sa.Enum("manual", "scheduled", name="runtrigger", native_enum=False, length=16), server_default="manual", nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=True),
        sa.Column("persona_id", sa.UUID(), nullable=True),
        sa.Column("note_id", sa.UUID(), nullable=True),
        sa.Column("topic_id", sa.UUID(), nullable=True),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="运行入参快照"),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="产出 id 列表 / 摘要"),
        sa.Column("llm_model", sa.String(length=100), nullable=True),
        sa.Column("llm_base_url", sa.Text(), nullable=True, comment="模型 base_url；绝不记录 API Key"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("json_parse_attempts", sa.Integer(), nullable=True, comment="JSON 解析尝试次数（理想恒为 1）"),
        sa.Column("status", sa.Enum("success", "partial", "failed", name="runstatus", native_enum=False, length=16), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], name=op.f("fk_agent_runs_account_id_accounts"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["persona_id"], ["personas.id"], name=op.f("fk_agent_runs_persona_id_personas"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], name=op.f("fk_agent_runs_note_id_notes"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], name=op.f("fk_agent_runs_topic_id_topics"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ix_agent_runs_run_type_created_at", "agent_runs", ["run_type", sa.literal_column("created_at DESC")], unique=False)
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"], unique=False)

    # ------------------------------------------------------------------
    # 8) covers —— 封面（note_id CASCADE）
    # ------------------------------------------------------------------
    op.create_table(
        "covers",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("template_key", sa.String(length=32), nullable=False, comment="模板标识"),
        sa.Column("color_scheme", sa.String(length=32), nullable=False, comment="配色方案"),
        sa.Column("title_text", sa.String(length=100), nullable=True),
        sa.Column("subtitle_text", sa.String(length=100), nullable=True),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("image_path", sa.Text(), nullable=True, comment="导出 PNG 的本地路径 / 对象 key"),
        sa.Column("width", sa.Integer(), server_default=sa.text("1242"), nullable=False),
        sa.Column("height", sa.Integer(), server_default=sa.text("1656"), nullable=False),
        sa.Column("is_selected", sa.Boolean(), server_default=sa.text("false"), nullable=False, comment="是否当前选用；一个 note 仅一个 true，由应用层保证"),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], name=op.f("fk_covers_note_id_notes"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_covers")),
    )
    op.create_index("ix_covers_note_id", "covers", ["note_id"], unique=False)

    # ------------------------------------------------------------------
    # 9) note_metrics —— 笔记数据快照（note_id CASCADE）
    # ------------------------------------------------------------------
    op.create_table(
        "note_metrics",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, comment="数据采集时刻"),
        sa.Column("data_source", sa.Enum("screenshot", "manual", name="datasource", native_enum=False, length=16), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=True, comment="浏览量"),
        sa.Column("likes", sa.BigInteger(), nullable=True, comment="点赞"),
        sa.Column("collects", sa.BigInteger(), nullable=True, comment="收藏"),
        sa.Column("comments", sa.BigInteger(), nullable=True, comment="评论"),
        sa.Column("shares", sa.BigInteger(), nullable=True, comment="分享"),
        sa.Column("new_followers", sa.BigInteger(), nullable=True, comment="新增粉丝"),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="每字段的来源区域 / 置信度 / missing / uncertain"),
        sa.Column("screenshot_path", sa.Text(), nullable=True, comment="截图路径（默认不保存作者信息）"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], name=op.f("fk_note_metrics_note_id_notes"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_note_metrics")),
        sa.UniqueConstraint("note_id", "captured_at", name="uq_metric_snapshot"),
    )
    op.create_index("ix_note_metrics_note_id_captured_at", "note_metrics", ["note_id", sa.literal_column("captured_at DESC")], unique=False)

    # ------------------------------------------------------------------
    # 10) diagnostics —— 单篇诊断（note_id CASCADE）
    # ------------------------------------------------------------------
    op.create_table(
        "diagnostics",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("based_on_metric_id", sa.UUID(), nullable=True, comment="所依据的数据快照"),
        sa.Column("diagnosis_type", sa.Enum("cold_start", "review", "anomaly", name="diagnosistype", native_enum=False, length=32), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("strengths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("weaknesses", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("suggestions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("benchmark", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="同类 / 历史均值对比"),
        sa.Column("llm_model", sa.String(length=100), nullable=True),
        sa.Column("llm_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["based_on_metric_id"], ["note_metrics.id"], name=op.f("fk_diagnostics_based_on_metric_id_note_metrics"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], name=op.f("fk_diagnostics_note_id_notes"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_diagnostics")),
    )
    op.create_index("ix_diagnostics_note_id_created_at", "diagnostics", ["note_id", sa.literal_column("created_at DESC")], unique=False)


def downgrade() -> None:
    """逆序删除 10 张表，并移除 pgvector 扩展，保证 ``downgrade base`` 干净。"""
    op.drop_index("ix_diagnostics_note_id_created_at", table_name="diagnostics")
    op.drop_table("diagnostics")

    op.drop_index("ix_note_metrics_note_id_captured_at", table_name="note_metrics")
    op.drop_table("note_metrics")

    op.drop_index("ix_covers_note_id", table_name="covers")
    op.drop_table("covers")

    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_run_type_created_at", table_name="agent_runs")
    op.drop_table("agent_runs")

    op.drop_index("ix_notes_xhs_note_id", table_name="notes")
    op.drop_index("ix_notes_topic_id", table_name="notes")
    op.drop_index("ix_notes_status_published_at", table_name="notes")
    op.drop_index("ix_notes_account_id_status", table_name="notes")
    op.drop_table("notes")

    op.drop_index("ix_topics_generated_at", table_name="topics")
    op.drop_index("ix_topics_score", table_name="topics")
    op.drop_index("ix_topics_status_account_id", table_name="topics")
    op.drop_table("topics")

    op.drop_index("ix_personas_account_id", table_name="personas")
    op.drop_table("personas")

    op.drop_index(
        "ix_knowledge_snippets_embedding_hnsw",
        table_name="knowledge_snippets",
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_index("ix_knowledge_snippets_chunk_type", table_name="knowledge_snippets")
    op.drop_table("knowledge_snippets")

    op.drop_index("ix_hotspots_fetched_at", table_name="hotspots")
    op.drop_index("ix_hotspots_status", table_name="hotspots")
    op.drop_index("ix_hotspots_source_hot_date", table_name="hotspots")
    op.drop_table("hotspots")

    op.drop_table("accounts")

    # 最后移除扩展（此时已无依赖对象）
    op.execute("DROP EXTENSION IF EXISTS vector")
