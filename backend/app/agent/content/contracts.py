"""创作 Agent 的 Pydantic 契约（模型输出形状）。

本契约只描述**形状**（字段名 / 类型），不承载业务阈值——标题长度、正文长度、
标签个数由 ``validate_compliance`` 节点按 ``NOTE_*`` 配置逐项校验，
以便对不合格项给出**可读的拒绝原因**并作为下一轮反馈。

``tags`` 的处理与第 10 步一致：模型常把 ``#话题`` 连 ``#`` 一起输出，
这里在**解析阶段**就剥掉前导 ``#`` 与首尾空白（保序、去重），
避免污染库里的 ``notes.tags``。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContentDraft(BaseModel):
    """一篇笔记草稿（LLM 输出结构）：标题 / 正文 / 标签。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", description="笔记标题（≤ NOTE_TITLE_MAX_LEN 字）")
    body: str = Field(default="", description="正文（≥ NOTE_BODY_MIN_LEN 字）")
    tags: list[str] = Field(
        default_factory=list, description="小红书 #话题标签（纯词，不含 #）"
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _strip_hash(cls, value: Any) -> Any:
        """剥掉标签前导 ``#`` 与首尾空白（非法类型原样交给 Pydantic 报错）。"""
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return value  # 交给 Pydantic 类型校验统一报错
            token = item.strip().lstrip("#").strip()
            if token and token not in cleaned:
                cleaned.append(token)
        return cleaned


__all__ = ["ContentDraft"]
