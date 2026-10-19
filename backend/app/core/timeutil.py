"""本地时区工具。

项目所有「当天」语义统一按 **Asia/Shanghai** 计算（热搜 ``hot_date`` 等），
避免容器 UTC 与人工判断不一致导致跨天错位。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: 本项目统一的本地时区（Asia/Shanghai，UTC+8，无夏令时）
LOCAL_TIMEZONE = timezone(timedelta(hours=8))


def now_local() -> datetime:
    """当前本地时间（带 +08:00 偏移）。"""
    return datetime.now(tz=LOCAL_TIMEZONE)


def today_local() -> date:
    """本地「今天」的日期（用于 ``hot_date``）。"""
    return now_local().date()
