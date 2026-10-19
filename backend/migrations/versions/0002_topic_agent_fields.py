"""topic agent fields: topics.content_outline / topics.tags / agent_runs.llm_raw

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10

第 10 步（选题 Agent）**勘误**迁移 —— 原任务书沿用了早期字段名，与第 7 步真实 schema 不符。

本迁移是**唯一**的结构变更，只新增 **3 个可空列**，全部 ``nullable=True``、**无 ``server_default``**、
**不建索引 / 不加外键 / 不新增枚举取值**：

- ``topics.content_outline`` —— ``JSONB``，内容大纲（字符串数组 3~6 条）；
- ``topics.tags`` —— ``JSONB``，小红书 #话题标签数组（纯词 3~8 个，不含 ``#``）；
- ``agent_runs.llm_raw`` —— ``Text``，模型原始输出（审计 / JSON 稳定性排障；**绝不记录 API Key**）。

其余选题字段**全部复用第 7 步已有列**（``title/angle/summary/pillar/target_keywords/source_hotspot_ids/
source_trace/difficulty/heat_score/score/llm_model/meta/status/generated_at/account_id``），不在此迁移中改动。

``downgrade`` 按 ``agent_runs.llm_raw → topics.tags → topics.content_outline`` **逆序** 删除。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # topics: 内容大纲 + 话题标签（均为可空 JSONB，禁止 server_default）
    op.add_column(
        "topics",
        sa.Column(
            "content_outline",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="内容大纲（字符串数组 3~6 条）",
        ),
    )
    op.add_column(
        "topics",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="小红书 #话题标签数组（纯词 3~8 个，不含 #）",
        ),
    )

    # agent_runs: 模型原始输出（审计 / JSON 稳定性排障，绝不存 API Key）
    op.add_column(
        "agent_runs",
        sa.Column(
            "llm_raw",
            sa.Text(),
            nullable=True,
            comment="模型原始输出；绝不记录 API Key",
        ),
    )


def downgrade() -> None:
    # 逆序删除
    op.drop_column("agent_runs", "llm_raw")
    op.drop_column("topics", "tags")
    op.drop_column("topics", "content_outline")
