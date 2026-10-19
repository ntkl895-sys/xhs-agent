"""同选题多版本创作 Agent（第 23 步 · V2 增强（二））。

在 LangGraph harness 上实现的**第七个业务图**：一个已通过选题 → N 个角度明显不同、
可横向对比、互斥采用的图文版本（标题 / 正文 / 标签），落库为**同批次 N 条 notes**。

模块边界：

- ``contracts`` —— 输出契约（``VariantDraft`` 继承第 12 步 ``ContentDraft``）与数量常量；
- ``state`` —— 业务图状态（可 JSON 序列化；模型对象绝不入状态）；
- ``prompts`` —— 提示词模板 + 内置中文角度池（不写 env）；
- ``graph`` —— 图装配（load_context 复用第 12 步同一份取数逻辑）；
- ``service`` —— 编排 + 审计回填 + 批次查询 + 互斥采用（给路由层调用）。

红线不变：不请求平台域名、不用登录态、不自动发布；接口不返回 ``llm_raw`` 与向量字段。
"""

from app.agent.content_variants.contracts import (
    DEFAULT_VARIANTS,
    MAX_VARIANTS,
    MIN_VARIANTS,
    OUTCOME_BLOCKED,
    OUTCOME_QUALIFIED,
    OUTCOME_REJECTED,
    VariantDraft,
)
from app.agent.content_variants.state import ContentVariantsState

__all__ = [
    "DEFAULT_VARIANTS",
    "MAX_VARIANTS",
    "MIN_VARIANTS",
    "OUTCOME_BLOCKED",
    "OUTCOME_QUALIFIED",
    "OUTCOME_REJECTED",
    "VariantDraft",
    "ContentVariantsState",
]
