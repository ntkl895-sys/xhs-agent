"""各源 ``data[]`` 归一化 + 热度文本解析 + 源合规校验。

迁自 ``D:\\项目1\\hotspot_spike`` 中**已验证**的逻辑（2026-10-09 实测），
但把 YAML 配置改为由 ``Settings`` 的环境变量驱动，且**移除了 xhs 适配器**——
小红书源在本服务中一律禁用（见 :data:`FORBIDDEN_SOURCES`）。

职责边界：本模块**只做纯函数转换**，不发起网络请求、不访问数据库。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.config import settings
from app.services.hotspot.schemas import HotspotItem

logger = logging.getLogger("xhs-agent.hotspot.adapters")

#: 源标识 -> 中文名（缺失时回退为 key 本身）
SOURCE_NAMES: dict[str, str] = {
    "zhihu": "知乎",
    "douyin": "抖音",
    "bilibili": "B站",
    "toutiao": "今日头条",
    "qq-news": "腾讯新闻",
    "netease-news": "网易新闻",
    "douban-group": "豆瓣讨论",
    "smzdm": "什么值得买",
    "sspai": "少数派",
    "36kr": "36氪",
    "sina-news": "新浪新闻",
    "thepaper": "澎湃新闻",
    "weread": "微信读书",
    "weibo": "微博",
    "baidu": "百度",
    "github": "GitHub",  # 第 23 步启用（本地 DailyHotApi 镜像含 /github 趋势源）
}

#: ``raw_extra`` 的默认最小字段集（``HOTSPOT_KEEP_EXTRA_FIELDS=false`` 时仅保留这些）
DEFAULT_EXTRA_FIELDS: tuple[str, ...] = ("id", "mobileUrl", "timestamp")

#: 追加保留字段（``HOTSPOT_KEEP_EXTRA_FIELDS=true`` 时启用，并回填对应列）
OPTIONAL_EXTRA_FIELDS: tuple[str, ...] = ("desc", "cover", "author")

#: 合规硬禁用：任何小红书相关标识都不得出现在启用源里
FORBIDDEN_SOURCES: frozenset[str] = frozenset(
    {"xhs", "xiaohongshu", "xiaohongshu-hot", "redbook", "xhslink"}
)

#: 视为「空」、无需写入 ``raw_extra`` 的值
_EMPTY_VALUES: tuple[Any, ...] = (None, "", [], {})


# ---------------------------------------------------------------------------
# 热度文本解析（原样迁自 hotspot_spike/utils/hot_parser.py）
# ---------------------------------------------------------------------------
_CHINESE_UNITS: tuple[tuple[str, float], ...] = (
    ("亿", 1e8),
    ("万", 1e4),
    ("千", 1e3),
    ("百", 1e2),
)
_ASCII_UNITS: tuple[tuple[str, float], ...] = (("w", 1e4), ("k", 1e3))
_STRIP_CHARS = " ,，_　\t\r\n"
_FULLWIDTH_OFFSET = ord("０") - ord("0")
_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _normalize_number_text(text: str) -> str:
    """把全角数字 / 字母与千分位、空白归一为可直接解析的半角形式。"""
    buffer: list[str] = []
    for char in text:
        code = ord(char)
        if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF3A or 0xFF41 <= code <= 0xFF5A:
            buffer.append(chr(code - _FULLWIDTH_OFFSET))
        else:
            buffer.append(char)
    normalized = "".join(buffer)
    for char in _STRIP_CHARS:
        normalized = normalized.replace(char, "")
    return normalized


def parse_hot_value(raw: Any) -> int | None:
    """把上游 ``hot`` 字段尽力解析为整数，失败返回 ``None``。

    支持：纯整数 / 纯数字字符串 / 中文单位（万·亿·千·百）/ 英文缩写（w·k）。
    非数值标记（如「热」「爆」「新」）与缺失值一律返回 ``None``。
    """
    # 布尔是 int 子类，必须先排除，否则 True 会被解析成 1
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return int(raw)
        except (ValueError, OverflowError):
            return None

    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    normalized = _normalize_number_text(text)
    if not normalized:
        return None

    def _multiply(source: str, multiplier: float) -> int | None:
        match = _NUMBER_PATTERN.search(source)
        if not match:
            return None
        try:
            return int(float(match.group()) * multiplier)
        except (ValueError, OverflowError):
            return None

    for unit, multiplier in _CHINESE_UNITS:
        if unit in normalized:
            return _multiply(normalized.replace(unit, ""), multiplier)

    lowered = normalized.lower()
    for unit, multiplier in _ASCII_UNITS:
        if unit in lowered:
            return _multiply(lowered.replace(unit, ""), multiplier)

    match = _NUMBER_PATTERN.search(normalized)
    if match:
        try:
            return int(float(match.group()))
        except (ValueError, OverflowError):
            return None
    return None


# ---------------------------------------------------------------------------
# 源标识与合规
# ---------------------------------------------------------------------------
def source_name_of(key: str) -> str:
    """取源的中文名（未登记时回退为 key）。"""
    return SOURCE_NAMES.get(key, key)


def is_forbidden_source(key: str) -> bool:
    """是否为合规禁用的源（小红书相关）。"""
    return key.strip().lower() in FORBIDDEN_SOURCES


def ensure_source_allowed(key: str) -> None:
    """若源被合规禁用则抛 ``ValueError``（中文原因）。

    这是「小红书源永不请求」的第一道闸门——任何调用方都必须先过这一关。
    """
    if is_forbidden_source(key):
        raise ValueError(
            f"源 {key!r} 属于合规禁用范围（小红书公开热搜需登录态，"
            f"本服务不采集、不请求任何小红书域名）"
        )


def resolve_enabled_sources(explicit: list[str] | None = None) -> list[str]:
    """解析本次要采集的源，并**剔除**合规禁用项。

    优先级：``explicit``（接口显式传入）> ``settings.HOTSPOT_ENABLED_SOURCES``。
    重复项按首次出现去重；禁用的源被过滤并记 WARNING（绝不请求）。
    """
    raw = explicit if explicit is not None else settings.hotspot_source_list
    resolved: list[str] = []
    for item in raw:
        key = str(item).strip()
        if not key:
            continue
        if is_forbidden_source(key):
            logger.warning("源 %r 属于合规禁用范围，已忽略（不发起任何请求）", key)
            continue
        if key not in resolved:
            resolved.append(key)
    return resolved


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def normalize_items(
    payload: dict[str, Any],
    source: str,
    *,
    per_source_limit: int,
    keep_extra_fields: bool = False,
) -> list[HotspotItem]:
    """把某个源的上游响应体 ``payload`` 归一化为 :class:`HotspotItem` 列表。

    - 缺少 ``title`` 的脏数据**直接丢弃**；
    - ``rank_position`` 按归一化后的顺序从 1 开始重新编号；
    - ``raw_extra`` 默认只留 ``id / mobileUrl / timestamp``；
      ``keep_extra_fields=True`` 时追加 ``desc / cover / author`` 并回填对应列；
    - 最终条数以 ``per_source_limit`` 本地截断（服务端 limit 只是省带宽的优化）。

    Raises:
        ValueError: 响应体 ``data`` 字段存在但不是数组。
    """
    data = payload.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ValueError("响应体 data 字段不是数组")

    extra_fields: tuple[str, ...] = DEFAULT_EXTRA_FIELDS + (
        OPTIONAL_EXTRA_FIELDS if keep_extra_fields else ()
    )

    items: list[HotspotItem] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            # 无标题的条目对下游毫无价值（百度源曾出现），直接丢弃
            continue

        extra: dict[str, Any] = {}
        for field_name in extra_fields:
            value = row.get(field_name)
            if value in _EMPTY_VALUES:
                continue
            extra[field_name] = value

        raw_id = row.get("id")
        source_item_id = None if raw_id in (None, "") else str(raw_id)

        items.append(
            HotspotItem(
                source=source,
                source_item_id=source_item_id,
                title=title,
                url=row.get("url") or None,
                mobile_url=row.get("mobileUrl") or None,
                hot_score=parse_hot_value(row.get("hot")),
                rank_position=len(items) + 1,
                cover_url=(row.get("cover") or None) if keep_extra_fields else None,
                summary=(row.get("desc") or None) if keep_extra_fields else None,
                author=(str(row.get("author")) if keep_extra_fields and row.get("author") else None),
                raw_extra=extra,
            )
        )

    return items[: max(0, per_source_limit)]


__all__ = [
    "DEFAULT_EXTRA_FIELDS",
    "FORBIDDEN_SOURCES",
    "OPTIONAL_EXTRA_FIELDS",
    "SOURCE_NAMES",
    "ensure_source_allowed",
    "is_forbidden_source",
    "normalize_items",
    "parse_hot_value",
    "resolve_enabled_sources",
    "source_name_of",
]
