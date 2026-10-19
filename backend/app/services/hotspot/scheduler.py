"""热搜定时采集调度器（APScheduler ``BackgroundScheduler``）。

为何单机 Windows 选 **APScheduler** 而非 Celery：

- 本项目是**单机开发 / 单进程**部署，不需要分布式任务队列与 broker；
- APScheduler 是**纯 Python 同步**库，与全项目的同步技术栈一致，
  不引入额外的 worker 进程 / 消息中间件 / 运维组件；
- Celery 需要独立 worker + broker（Redis/RabbitMQ），对本项目属**过度设计**，
  且 Windows 下事件循环与 fork 语义有额外坑。

启停约束：**必须合并进 ``app/main.py`` 那个唯一的 lifespan**（第 6 步确立，
Agent Harness 步已占用同一 lifespan 管理 LangGraph 检查点）。严禁新建第二个
``lifespan`` 互相覆盖。本模块只提供 ``start_scheduler()`` / ``shutdown_scheduler()``，
由 lifespan 调用。
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.core.timeutil import LOCAL_TIMEZONE, now_local

logger = logging.getLogger("xhs-agent.hotspot.scheduler")

#: 定时作业 id（``coalesce`` + ``max_instances=1`` 保证不与上一次重叠）
FETCH_JOB_ID = "hotspot_fetch_interval"
STARTUP_JOB_ID = "hotspot_fetch_startup"

_scheduler: BackgroundScheduler | None = None


def _run_scheduled_fetch() -> None:
    """定时作业体：调用统一采集入口（``trigger="scheduled"``）。

    抢不到锁（已有采集在跑）时**安静跳过**，不抛异常、不产生 failed 审计。
    """
    from app.services.hotspot import service

    try:
        service.collect_and_store(trigger="scheduled", raise_if_busy=False)
    except Exception:  # noqa: BLE001 - 调度线程内不得让异常冒泡终止调度器
        logger.exception("定时热搜采集执行失败")


def start_scheduler() -> BackgroundScheduler | None:
    """按配置启动调度器；已启动或配置关闭时直接返回。

    Returns:
        启动后的调度器实例；``HOTSPOT_SCHEDULER_ENABLED=false`` 时返回 ``None``。
    """
    global _scheduler

    if not settings.HOTSPOT_SCHEDULER_ENABLED:
        logger.info("热搜定时采集已关闭（HOTSPOT_SCHEDULER_ENABLED=false），不注册作业")
        return None

    if _scheduler is not None and _scheduler.running:
        logger.info("热搜调度器已在运行，跳过重复启动")
        return _scheduler

    interval = max(1, int(settings.HOTSPOT_FETCH_INTERVAL_MINUTES))
    scheduler = BackgroundScheduler(timezone=LOCAL_TIMEZONE)

    scheduler.add_job(
        _run_scheduled_fetch,
        trigger="interval",
        minutes=interval,
        id=FETCH_JOB_ID,
        name="热搜定时采集",
        coalesce=True,        # 错过多次触发只补跑一次
        max_instances=1,      # 同一作业不重叠
        replace_existing=True,
    )

    if settings.HOTSPOT_FETCH_ON_STARTUP:
        scheduler.add_job(
            _run_scheduled_fetch,
            trigger="date",
            run_date=now_local(),
            id=STARTUP_JOB_ID,
            name="热搜启动采集",
            replace_existing=True,
        )
        logger.info("已注册启动即采集作业（HOTSPOT_FETCH_ON_STARTUP=true）")

    scheduler.start()
    _scheduler = scheduler
    logger.info("热搜调度器已启动：间隔 %d 分钟（coalesce=True, max_instances=1）", interval)
    return scheduler


def shutdown_scheduler() -> None:
    """停止调度器（幂等；未启动时静默）。"""
    global _scheduler
    if _scheduler is None:
        return
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("热搜调度器已关闭")
    _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    """返回当前调度器实例（未启动为 ``None``）。"""
    return _scheduler


def next_run_time() -> datetime | None:
    """下一次触发时间（供诊断 / 自检；无作业时返回 ``None``）。"""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(FETCH_JOB_ID)
    if job is None:
        return None
    return getattr(job, "next_run_time", None)


__all__ = [
    "FETCH_JOB_ID",
    "STARTUP_JOB_ID",
    "get_scheduler",
    "next_run_time",
    "shutdown_scheduler",
    "start_scheduler",
]
