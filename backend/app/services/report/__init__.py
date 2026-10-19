"""周报服务（第 17 步）——定时调度（**默认关闭**）。

对外：

- :func:`app.services.report.scheduler.start_weekly_scheduler` /
  :func:`app.services.report.scheduler.shutdown_weekly_scheduler` ——
  由 ``app/main.py`` 的**唯一 lifespan** 调用（与热搜调度器、检查点共存）；
- 作业体固定走 ``generate_weekly_report(trigger="scheduled")``。

周报生成本体在 ``app.agent.weekly``（第五个业务图）；本包只负责「什么时候跑」。
"""

from __future__ import annotations

from app.services.report.scheduler import (
    WEEKLY_JOB_ID,
    get_weekly_scheduler,
    next_weekly_run_time,
    shutdown_weekly_scheduler,
    start_weekly_scheduler,
)

__all__ = [
    "WEEKLY_JOB_ID",
    "get_weekly_scheduler",
    "next_weekly_run_time",
    "shutdown_weekly_scheduler",
    "start_weekly_scheduler",
]
