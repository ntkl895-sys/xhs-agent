"""多版本创作 Agent 的契约与常量（第 23 步 · V2 增强（二））。

- ``VariantDraft`` **继承第 12 步的 ``ContentDraft``**（复用其标题 / 正文 / 标签
  声明与 ``#`` 剥除归一化校验），仅追加 ``angle`` 字段 —— 不复制第二套解析逻辑；
- 版本数量区间（默认 3、允许 1~5）与判定词表集中在本模块；
- 判定结论采用三值词表（**与第 12 步的 ``ComplianceStatus`` 不同**，是「版本级」结论）：

      qualified 合格       —— 标题 / 正文非空 且 0 违禁词命中（落库为 note）
      blocked   违禁词命中 —— 拦截该版本，不落库；不阻断其他版本（partial 语义）
      rejected  不合格     —— 缺标题 / 正文或结构非法，不落库
"""

from __future__ import annotations

from pydantic import Field

from app.agent.content.contracts import ContentDraft

#: 版本数量区间与默认值（任务书口径：默认 3、允许 1~5；越界由 schema 拒绝为 422）
MIN_VARIANTS = 1
MAX_VARIANTS = 5
DEFAULT_VARIANTS = 3

#: 显式传入单个角度时的最大长度
ANGLE_MAX_LEN = 20

#: 版本级判定词表（落库 / 出参统一口径）
OUTCOME_QUALIFIED = "qualified"
OUTCOME_BLOCKED = "blocked"
OUTCOME_REJECTED = "rejected"


class VariantDraft(ContentDraft):
    """一个版本的草稿（LLM 输出结构）：角度 + 标题 / 正文 / 标签。

    继承自第 12 步 ``ContentDraft``：``tags`` 的剥 ``#`` / 保序 / 去重与
    字段类型校验**完全复用**，本类只追加 ``angle``。
    """

    angle: str = Field(
        default="", description="本版的角度 / 切入方式（如「反差观点」；可为空由上层归一）"
    )


__all__ = [
    "ANGLE_MAX_LEN",
    "DEFAULT_VARIANTS",
    "MAX_VARIANTS",
    "MIN_VARIANTS",
    "OUTCOME_BLOCKED",
    "OUTCOME_QUALIFIED",
    "OUTCOME_REJECTED",
    "VariantDraft",
]
