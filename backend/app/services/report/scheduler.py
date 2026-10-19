"""周报定时调度器（APScheduler ``BackgroundScheduler``；**默认关闭**）。

与热搜调度器（``app/services/hotspot/scheduler.py``）同理由、同范式：

- 单机开发 / 单进程部署 ⇒ APScheduler（纯 Python 同步），不引入 Celery / broker；
- **仅在 ``WEEKLY_SCHEDULER_ENABLED=true`` 时注册作业**（默认 false —— 首要是
  「不打扰」：定时跑周报会消耗真实 token）；
- 作业体调 :func:`app.agent.weekly.service.generate_weekly_report`
  （``trigger="scheduled"``）；调度线程内**异常只 log 不冒泡**（不终止调度器，
  也不会让失败悄悄消失——``agent_runs`` 里同样会留 failed 审计）。

启停约束：**必须合并进 ``app/main.py`` 那个唯一的 lifespan**（与热搜 scheduler、
LangGraph 检查点共存于同一 lifespan）。严禁新建第二个 ``lifespan`` 互相覆盖。
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.timeutil import LOCAL_TIMEZONE

logger = logging.getLogger("xhs-agent.report.scheduler")

#: 定时作业 id（``coalesce`` + ``max_instances=1`` 保证不与上一次重叠）
WEEKLY_JOB_ID = "weekly_report_cron"

_scheduler: BackgroundScheduler | None = None


def _run_scheduled_weekly_report() -> None:
    """定时作业体：调用周报统一入口（``trigger="scheduled"``）。

    - 未配置 LLM（``AgentConfigError``）⇒ 只记 WARNING（**零请求**）；
    - 其它异常 ⇒ ``logger.exception``；两者都不向调度线程外抛。
    """
    from app.agent.errors import AgentConfigError
    from app.agent.weekly.service import generate_weekly_report

    try:
        generate_weekly_report(trigger="scheduled")
    except AgentConfigError as exc:
        logger.warning("定时周报跳过：LLM 未就绪（%s）", exc)
    except Exception:  # noqa: BLE001 - 调度线程内不得让异常冒泡终止调度器
        logger.exception("定时周报生成失败")


def start_weekly_scheduler() -> BackgroundScheduler | None:
    """按配置启动调度器；已启动或配置关闭时直接返回。

    Returns:
        启动后的调度器实例；``WEEKLY_SCHEDULER_ENABLED=false`` 时返回 ``None``。
    """
    global _scheduler

    if not settings.WEEKLY_SCHEDULER_ENABLED:
        logger.info("周报定时已关闭（WEEKLY_SCHEDULER_ENABLED=false），不注册作业")
        return None

    if _scheduler is not None and _scheduler.running:
        logger.info("周报调度器已在运行，跳过重复启动")
        return _scheduler

    cron_expr = str(settings.WEEKLY_SCHEDULE_CRON or "").strip()
    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone=LOCAL_TIMEZONE)
    except ValueError:
        logger.error("周报 cron 表达式非法：%r（调度器不启动，请修正后重启）", cron_expr)
        return None

    scheduler = BackgroundScheduler(timezone=LOCAL_TIMEZONE)
    scheduler.add_job(
        _run_scheduled_weekly_report,
        trigger=trigger,
        id=WEEKLY_JOB_ID,
        name="周报定时生成",
        coalesce=True,        # 错过多次触发只补跑一次
        max_instances=1,      # 同一作业不重叠
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("周报调度器已启动：cron=%r（coalesce=True, max_instances=1）", cron_expr)
    return scheduler


def shutdown_weekly_scheduler() -> None:
    """停止调度器（幂等；未启动时静默）。"""
    global _scheduler
    if _scheduler is None:
        return
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("周报调度器已关闭")
    _scheduler = None


def get_weekly_scheduler() -> BackgroundScheduler | None:
    """返回当前调度器实例（未启动为 ``None``）。"""
    return _scheduler


def next_weekly_run_time() -> datetime | None:
    """下一次触发时间（供诊断 / 自检；无作业时返回 ``None``）。"""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(WEEKLY_JOB_ID)
    if job is None:
        return None
    return getattr(job, "next_run_time", None)


__all__ = [
    "WEEKLY_JOB_ID",
    "get_weekly_scheduler",
    "next_weekly_run_time",
    "shutdown_weekly_scheduler",
    "start_weekly_scheduler",
]
