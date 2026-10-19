"""热搜采集服务（第 9 步）。

采集链路：``DailyHotApi（本机容器）→ client（HTTP + 重试）→ adapters（归一化）
→ runner（多源并发 + 故障隔离）→ crud.upsert_many（幂等落库）→ agent_runs（审计）``。

合规红线（任何改动都不得违反）：
- 只采公开榜单；**不请求任何小红书域名**、不携带登录态 / Cookie / Token；
- 小程序标识 ``xhs`` / ``xiaohongshu`` 在 :mod:`adapters` 中被硬性拒绝。

模块职责：

- ``schemas.py``   ：采集内部数据模型（与 ``hotspots`` 表字段对齐）；
- ``client.py``    ：同步 httpx 客户端 + tenacity 重试（429/5xx/网络重试，4xx 立即失败）；
- ``adapters.py``  ：各源 ``data[]`` 归一化为统一条目 + 热度文本解析 + 源合规校验；
- ``runner.py``    ：ThreadPoolExecutor 并发拉取启用源，单源异常隔离；
- ``service.py``   ：单入口 ``collect_and_store()``（互斥锁 + 落库 + 审计）；
- ``scheduler.py`` ：APScheduler 定时器（启停并入 ``app/main.py`` 的唯一 lifespan）。
"""

from __future__ import annotations

from app.services.hotspot.schemas import (
    CollectSummary,
    CollectTotals,
    HotspotItem,
    SourceFetchOutcome,
    SourceStatus,
    SourceStoreStats,
)

__all__ = [
    "CollectSummary",
    "CollectTotals",
    "HotspotItem",
    "SourceFetchOutcome",
    "SourceStatus",
    "SourceStoreStats",
]
