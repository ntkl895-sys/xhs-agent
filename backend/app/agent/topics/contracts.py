"""LLM 候选选题的 Pydantic 契约。

本契约只描述**形状**（字段名 / 类型），不承载业务阈值 —— 阈值（标题长度、
大纲条数、标签个数、难度取值、置信度范围）由 ``validate_dedupe`` 节点按
``TOPIC_*`` 配置逐项校验，以便对不合格项给出**可读的拒绝原因**并作为下一轮反馈。

``tags`` 的一个细节：模型常把 ``#话题`` 连 ``#`` 一起输出，这里在**解析阶段**就剥掉
前导 ``#`` 与首尾空白（保留原始顺序、去重），避免污染库里的 ``topics.tags``。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TopicCandidate(BaseModel):
    """一条候选选题（LLM 输出结构）。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="", description="选题标题")
    summary: str | None = Field(default=None, description="为什么写 / 写给谁 / 核心观点")
    angle: str | None = Field(default=None, description="切入角度")
    pillar: str | None = Field(default=None, description="内容支柱")
    content_outline: list[str] = Field(default_factory=list, description="内容大纲")
    tags: list[str] = Field(default_factory=list, description="小红书 #话题标签（纯词，不含 #）")
    target_keywords: list[str] = Field(default_factory=list, description="目标关键词")
    difficulty: str | None = Field(default=None, description="easy / medium / hard")
    confidence: float | None = Field(default=None, description="模型自评置信度 0~1")

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

    @field_validator("content_outline", "target_keywords", mode="before")
    @classmethod
    def _clean_str_list(cls, value: Any) -> Any:
        """去掉空字符串、去重（保留原始顺序）。"""
        if not isinstance(value, list):
            return value
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return value
            token = item.strip()
            if token and token not in cleaned:
                cleaned.append(token)
        return cleaned


__all__ = ["TopicCandidate"]
