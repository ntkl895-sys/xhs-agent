"""数据回填 Agent 的契约（第 15 步）：字段口径 / 图像工具 / 专属异常 / 模型输出形状。

三部分：

1. **六个指标是唯一口径**：``views / likes / collects / comments / shares /
   new_followers``（与 ``note_metrics`` 表可落库列一一对应）。
   ``screenshot_spike`` 原型里的 14 个指标中，``impressions / clicks / ctr /
   profile_visits / avg_watch_seconds / danmaku / recommend_ratio / search_ratio``
   **一律忽略**——落不进表，也不作为指标字段返回。
2. **图像工具**（纯函数）：MIME 白名单判定 + 文件头魔数探测（不依赖 Pillow）。
3. **专属异常**：全部继承 ``AgentNonRetryableError``（不消耗节点重试额度），
   由 service 统一翻译成带中文指引的统一错误体（400 / 404 / 422 / 503）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.errors import AgentNonRetryableError

#: 允许落库 / 返回的**六个**指标（顺序即展示顺序，勿改）
METRIC_FIELDS: tuple[str, ...] = (
    "views",
    "likes",
    "collects",
    "comments",
    "shares",
    "new_followers",
)

#: 指标中文别名 → 标准字段名。仅用于把模型的同义写法**对齐**到六字段，
#: 不做任何「新增字段」；无法对齐的键（impressions 等）按口径忽略。
METRIC_ALIASES: dict[str, str] = {
    # 浏览（小红书里的「小眼睛」）
    "view": "views", "views": "views", "阅读": "views", "阅读量": "views",
    "播放": "views", "播放量": "views", "观看": "views", "观看量": "views",
    "小眼睛": "views", "浏览量": "views", "浏览": "views",
    # 点赞
    "like": "likes", "likes": "likes", "点赞": "likes", "点赞数": "likes", "赞": "likes",
    # 收藏
    "collect": "collects", "collects": "collects", "收藏": "collects",
    "收藏数": "collects", "收藏量": "collects",
    # 评论
    "comment": "comments", "comments": "comments", "评论": "comments",
    "评论数": "comments", "评论量": "comments",
    # 分享
    "share": "shares", "shares": "shares", "分享": "shares", "分享数": "shares",
    "转发": "shares", "转发量": "shares", "分享量": "shares",
    # 新增粉丝（涨粉）
    "new_follower": "new_followers", "new_followers": "new_followers",
    "涨粉": "new_followers", "涨粉数": "new_followers", "新增粉丝": "new_followers",
    "新增关注": "new_followers", "粉丝增量": "new_followers", "关注人数": "new_followers",
    "followers": "new_followers",
}


# ---------------------------------------------------------------------------
# 图像工具（纯函数，不依赖 Pillow）
# ---------------------------------------------------------------------------
def detect_image_mime(data: bytes) -> str | None:
    """按**文件头魔数**探测图片真实类型；不是 jpeg/png/webp 时返回 ``None``。

    支持：JPEG（``FF D8 FF``）、PNG（8 字节签名）、WebP（``RIFF....WEBP``）。
    """
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


#: 真实 MIME → 落盘后缀（仅用于 SAVE_SCREENSHOT=true 时命名文件）
SUFFIX_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


# ---------------------------------------------------------------------------
# 模型输出形状（只描述形状；别名归一 / 数字换算在 normalize 节点本地完成）
# ---------------------------------------------------------------------------
class MetricTranscription(BaseModel):
    """模型对**单个指标**的转录结果（``{"value":…, "raw":…}`` 或裸标量）。

    解析原则：**尽力而为、绝不抛错**——字段脏值（奇怪的类型等）一律降级为
    ``None``，由 normalize 节点登记 missing / uncertain，而不是让整条 payload 校验失败。
    """

    model_config = ConfigDict(extra="ignore")

    value: float | None = None
    raw: str | None = None
    confidence: float | None = None
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_scalar(cls, data: Any) -> Any:
        """裸标量归一：数字 → ``{"value": n}``；字符串 → ``{"raw": s}``。"""
        if isinstance(data, bool) or data is None:
            return {}
        if isinstance(data, (int, float)):
            return {"value": data}
        if isinstance(data, str):
            return {"raw": data}
        return data

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: Any) -> float | None:
        """数字（含「12000」这类纯数字字符串）→ float；其余 → None（不抛错）。"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip().replace(",", "").replace("，", ""))
        except (TypeError, ValueError):
            return None

    @field_validator("raw", "note", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> float | None:
        """置信度限定 0~1；越界或非数字 → None（不编造）。"""
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < 0 or number > 1:
            return None
        return round(number, 3)


class MetricRecognitionPayload(BaseModel):
    """模型对**整张截图**的转录结果（本步只消费六字段，多余一律忽略）。"""

    model_config = ConfigDict(extra="ignore")

    metrics: dict[str, MetricTranscription] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)
    overall_confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("metrics", mode="before")
    @classmethod
    def _coerce_metrics(cls, value: Any) -> Any:
        """``metrics`` 必须是对象；null / 数组等一律降级为空对象。"""
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {}

    @field_validator("missing_fields", "uncertain_fields", "warnings", mode="before")
    @classmethod
    def _coerce_str_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @field_validator("overall_confidence", mode="before")
    @classmethod
    def _clamp_overall(cls, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < 0 or number > 1:
            return None
        return round(number, 3)


# ---------------------------------------------------------------------------
# 专属异常（service 按类型翻译统一错误体）
# ---------------------------------------------------------------------------
class MetricRecognitionError(AgentNonRetryableError):
    """截图识别业务错误基类。（继承 AgentNonRetryableError：绝不进入节点重试。）"""


class ImageValidationError(MetricRecognitionError):
    """上传文件不合法（类型 / 魔数 / 大小 / 空文件）→ 422。"""


class NoteMissingError(MetricRecognitionError):
    """``note_id`` 指向的笔记不存在 → 404。"""


class VisionUnsupportedError(MetricRecognitionError):
    """所选模型不支持图片输入 → 400（换支持视觉的模型可解）。"""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class RecognitionGiveUpError(MetricRecognitionError):
    """JSON 解析重试超限等识别失败 → 400（可重试 / 换截图）。"""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


__all__ = [
    "METRIC_FIELDS",
    "METRIC_ALIASES",
    "SUFFIX_BY_MIME",
    "detect_image_mime",
    "MetricTranscription",
    "MetricRecognitionPayload",
    "MetricRecognitionError",
    "ImageValidationError",
    "NoteMissingError",
    "VisionUnsupportedError",
    "RecognitionGiveUpError",
]
