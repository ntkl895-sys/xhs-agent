# 热搜服务化 + 定时采集 · xhs-agent

> 本文档描述**此前（热搜服务化）**的完整设计：采集链路、幂等落库、定时调度、
> 接口契约、配置项、自检与实测证据、以及**未验证项**。
> 前置： `hotspots` / `agent_runs` 表、 `schemas → crud → routes` 分层与统一错误体、
> Agent Harness 步建立的**唯一 lifespan**。

---

## 一、定位与边界

**本次做什么**

- 用 **DailyHotApi**（本机容器/服务）采集**公开**热榜，落 `hotspots` 表；
- 迁入 `D:\项目1\hotspot_spike` 中**已验证**的采集逻辑（HTTP + 重试 + 归一化 + 热度解析），
 配置从 YAML 改为 `Settings` 环境变量；
- **幂等落库**（`content_hash` + `ON CONFLICT DO UPDATE`），同一天多次采集不产生重复行；
- **APScheduler** 定时采集，启停**并入 `app/main.py` 的唯一 lifespan**；
- `/api/v1/hotspots` 系列接口（列表 / sources / refresh / runs）；
- 每次采集写一条 `agent_runs`（`run_type=hotspot_fetch`）。

**本次不做什么（明确留给后续步骤）**

| 不做 | 留给 |
| --- | --- |
| 前端页面与 Next 代理 | 此前 |
| 选题 / 创作（不接 LLM、不接 LangChain） | 第 10 / 12 步 |
| 热点「采纳 / 忽略」业务动作与 `hotspot.status` 流转 | 此前（本次仅落 `new`） |
| 修改业务表结构 / 新增 Alembic 迁移 | **本次零迁移**（唯一枚举变更见下） |
| Celery / 分布式任务队列 | 不引入（理由见第五节） |
| 登录认证 / 权限 | 不做（本地单用户工具） |
| 采集小红书（xhs）热搜 | **永不做（产品红线）** |

> **合规红线**：只采公开榜单；**不请求任何小红书域名**、不携带 / 不存储登录态、Cookie、Token；
> 不实现任何绕过登录 / 风控 / 验证码 / 请求签名的逻辑；不采集用户个人信息与笔记正文。

**唯一的枚举变更**：`app/models/enums.py` 的 `RunType` 增加 `HOTSPOT_FETCH = "hotspot_fetch"`。
该枚举以 `native_enum=False` 落为 `VARCHAR(32)`，**加值不需要 DDL / 迁移**。

---

## 二、采集链路

```
 ┌──────────────────────────────────────────────────────────┐
 定时（APScheduler）│ app/services/hotspot/scheduler.py │
 或 手动（POST │ interval 作业 → service.collect_and_store(trigger=...) │
 /hotspots/refresh）└───────────────────────┬──────────────────────────────────┘
 ▼
 app/services/hotspot/service.py ← 唯一采集入口
 · 模块级 threading.Lock 互斥（定时 / 手动共用）
 · 初始化 agent_runs 审计行（AgentRunRecorder）
 │
 ▼
 app/services/hotspot/runner.py
 · ThreadPoolExecutor 并发拉取启用源
 · 单源异常隔离（failed / blocked），绝不拖垮整体
 │
 ┌─────────────────────────────┼─────────────────────────────┐
 ▼ ▼ ▼
 adapters.normalize_items client.DailyHotClient adapters.resolve_enabled_sources
 · 无 title 脏数据丢弃 · httpx.Client（同步） · 过滤小红书（合规）
 · 热度文本解析 · tenacity：429/5xx/网络重试 · 去重保序
 · rank / raw_extra 归一化 · 4xx / 非法 JSON 立即失败
 │
 ▼
 HTTP GET {HOTSPOT_API_BASE_URL}/{key}?limit=N[&cache=false]
 │
 ▼
 DailyHotApi（docker compose 的 dailyhot，127.0.0.1:6688）
 │
 ┌───────────────────────┘
 ▼
 app/crud/hotspot.py upsert_many
 · content_hash = sha256(source | identity | hot_date)
 · INSERT ... ON CONFLICT (content_hash) DO UPDATE
 · RETURNING (xmax = 0) 区分 new / updated
 │
 ▼
 PostgreSQL public.hotspots（**零新增表 / 零新增列**）
 │
 ▼
 app/agent/tracing/recorder.py AgentRunRecorder
 → agent_runs：run_type=hotspot_fetch / trigger / status / outputs(每源+总计)
 latency_ms / error_message；**llm_* 留空、token 为 NULL**
```

**为什么「一个热点一条图」是事，而这一层是 service 批量循环？**
本次**不接 LLM**，没有多节点图的需要；采集是「多源并发 + 单事务落库」的批处理，
用 service 编排 + 每源独立 `SessionLocal` 更直接。选题才会用 LangGraph 图，
并复用本次落下的 `hotspots`。

---

## 三、目录与职责

| 文件 | 职责 |
| --- | --- |
| `app/services/hotspot/schemas.py` | 采集内部模型：`HotspotItem` / `SourceFetchOutcome` / `SourceStoreStats` / `CollectSummary` |
| `app/services/hotspot/client.py` | `DailyHotClient`：同步 httpx + tenacity 重试（可注入 `MockTransport` 供离线测试） |
| `app/services/hotspot/adapters.py` | 归一化 + 热度解析 + 源合规校验（`FORBIDDEN_SOURCES`） |
| `app/services/hotspot/runner.py` | `ThreadPoolExecutor` 并发拉取 + 单源故障隔离 |
| `app/services/hotspot/service.py` | 唯一入口 `collect_and_store`：互斥锁 / 落库 / 审计 / 汇总 |
| `app/services/hotspot/scheduler.py` | APScheduler 定时器：`start_scheduler` / `shutdown_scheduler` |
| `app/crud/hotspot.py` | `content_hash` / 过滤条件 / **幂等 upsert** |
| `app/schemas/hotspot.py` | HTTP 出入参（`HotspotOut` / `HotspotRefreshOut` / `HotspotRunOut` / `HotspotSourceOut`） |
| `app/api/routes/hotspots.py` | 4 个路由（列表 / sources / refresh / runs） |
| `app/core/timeutil.py` | 统一 `Asia/Shanghai`「今天」（`hot_date` 依据） |

**分层**（沿用此前单向依赖，新增 `services` 层）：

```
api/routes → services（编排：collection） → crud → models → db
 │ │ ▲
 └→ schemas └─────────→ schemas ─────────┘
```

- `crud` 不感知 HTTP、不做编排；`services` 负责跨 crud / 外部 IO 的用例编排；
- 路由层**不写 SQLAlchemy 表达式**（`selftest_api.py` 会静态断言）；
- 建表**只走 Alembic**（本次**不新增迁移**）。

---

## 四、源清单与合规

**默认启用（此前实测通过，2026-10-09，本地 dailyhot-api 2.0.8）**

| key | 名称 | 说明 |
| --- | --- | --- |
| `zhihu` | 知乎 | 稳定 |
| `douyin` | 抖音 | 稳定 |
| `bilibili` | B站 | 稳定（**注意不是 `bili`**） |
| `toutiao` | 今日头条 | 稳定 |
| `qq-news` | 腾讯新闻 | 稳定（**注意不是 `newsqq`**） |

**默认禁用（实测有问题或合规问题）**

| key | 原因 |
| --- | --- |
| `weibo` | 上游曾持续 HTTP 500（`m.weibo.cn` 接口异常） |
| `baidu` | 源改版失效，返回 `total=1` 且 `data` 内**无 title** 脏数据 |
| **`xhs` / 小红书** | **无对应路由，且公开热搜需登录态 —— 合规上不可采集** |

`adapters.FORBIDDEN_SOURCES = {xhs, xiaohongshu, xiaohongshu-hot, redbook, xhslink}`：
`resolve_enabled_sources` 会把它们**过滤掉并记 WARNING**；`ensure_source_allowed`
在适配器层二次拦截；`runner.fetch_one` 还有一道 `blocked` 兜底。**三道闸门，任一命中都不发请求。**

---

## 五、幂等落库与 key 设计

**唯一约束** `uq_hotspot_hash(content_hash)`（此前已建）。

```
identity = source_item_id（缺省时回退用标题）
content_hash = sha256( normalize(source) | normalize(identity) | hot_date )
hot_date = Asia/Shanghai 当天日期
```

- 归一化：去首尾空白、统一小写、压缩内部空白；
- **缺 `source_item_id` 时用标题兜底**——否则同一源当天所有「无 id」条目会因哈希相同而被
 错误地合并成一行（这是本次新增的防御，spike 时代此处直接按 id 哈希）；
- 同批次内 `content_hash` 先去重：PostgreSQL **不允许** `ON CONFLICT` 在同一语句里二次命中同一行。

**upsert 语义**

```sql
INSERT INTO hotspots (...) VALUES (...)
ON CONFLICT (content_hash) DO UPDATE
SET hot_score = EXCLUDED.hot_score,
 rank_position = EXCLUDED.rank_position,
 fetched_at = EXCLUDED.fetched_at,
 url = EXCLUDED.url, mobile_url = EXCLUDED.mobile_url,
 raw_extra = EXCLUDED.raw_extra, ...
RETURNING (xmax = 0) AS inserted; -- 新插入行 xmax=0
```

新行 `status = 'new'`；冲突行**只更新易变字段**，`status` 与 `created_at` 不动。

---

## 六、定时调度：为何 APScheduler 而不是 Celery

| 维度 | APScheduler（选用） | Celery（不选） |
| --- | --- | --- |
| 部署形态 | **进程内**线程调度，单机单进程 | 需独立 worker 进程 + broker（Redis/RabbitMQ） |
| 技术栈 | **纯 Python 同步**，与全项目一致 | 异步/并发模型，与「刻意同步栈」冲突 |
| Windows 支持 | 稳定（`BackgroundScheduler` 用线程池） | fork / 事件循环在 Windows 上有额外坑 |
| 运维成本 | 零（随 FastAPI 进程启停） | 需管理 broker、worker、结果后端 |
| 适配度 | 本项目**单机开发/演示**，定时采集就是「每小时跑一次函数」 | 面向分布式、高吞吐任务——**过度设计** |

**启停必须并入唯一 lifespan**（此前确立的硬约束；Agent Harness 步已占用同一 lifespan
管理 LangGraph 检查点）：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
 ...
 start_scheduler # 按 HOTSPOT_SCHEDULER_ENABLED 决定是否注册作业
 try:
 yield
 finally:
 shutdown_scheduler
 close_checkpointer
```

作业参数：`interval=HOTSPOT_FETCH_INTERVAL_MINUTES`、`coalesce=True`（错过多次只补跑一次）、
`max_instances=1`（同作业不重叠）。`HOTSPOT_FETCH_ON_STARTUP=true` 时额外注册一个
`run_date=now` 的一次性作业。

**互斥**：定时作业与手动刷新共用 `service._collect_lock`（模块级 `threading.Lock`）。
手动 `POST /refresh` 抢不到锁 → **409 `fetch_in_progress`**；定时作业抢不到 → **安静跳过**
（记 INFO，不产生 failed 审计）。

> **Redis 本次未使用**：单机单进程的互斥用进程内锁即可，无需分布式锁。
> `docker-compose.yml` 的 `redis` 保持「已启动、未写入」状态（为后续可能的跨进程缓存/限流预留）。

---

## 七、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/hotspots` | 分页列表。过滤 `source` / `status` / `q`（标题模糊）/ `hot_date`（缺省=今天，`all`=不限）；排序 `sort=hot_score`（默认）\| `fetched_at`，`order=desc`（默认）\| `asc`。返回 `{items,total,page,page_size}` |
| `GET` | `/api/v1/hotspots/sources` | 各启用源：`source` / `name` / `enabled` / `last_fetched_at` / `last_status` / `last_new` / `last_updated` / `last_failed`（聚合自最近一条 `hotspot_fetch` 的 `outputs.per_source`）。返回**普通数组** |
| `POST` | `/api/v1/hotspots/refresh` | 手动触发采集。body 可省：`{"sources": [...]}`。同步返回每源 + 总计统计。采集中 → **409 `fetch_in_progress`** |
| `GET` | `/api/v1/hotspots/runs?limit=10` | 最近 `hotspot_fetch` 运行（时间 / trigger / status / inputs / outputs / 耗时 / 错误）。返回**普通数组** |

**错误体**沿用此前统一结构：`{"status":"error","code":...,"message":...,"details":...}`；
`code` 取值含 `not_found/conflict/validation/bad_request` 与本次新增
`fetch_in_progress`（409）。

**健康检查未改动**：`/health` 与 `/health/ready` 仍只有 `database` / `pgvector` / `redis` 三项；
`dailyhot` **不是**就绪探针必需项——它未启动时后端照常可用，只是采集会失败（体现在 `/sources`）。

---

## 八、配置项（`backend/.env`，见 `backend/.env.example`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOTSPOT_API_BASE_URL` | `http://127.0.0.1:6688` | DailyHotApi 服务地址 |
| `HOTSPOT_ENABLED_SOURCES` | `zhihu,douyin,bilibili,toutiao,qq-news` | 启用的源（**不得含小红书**） |
| `HOTSPOT_PER_SOURCE_LIMIT` | `20` | 每源最多保留条数（服务端传 `limit`，本地再截断） |
| `HOTSPOT_FORCE_REFRESH` | `false` | `true` 时请求带 `cache=false`（唯一生效的强刷写法） |
| `HOTSPOT_KEEP_EXTRA_FIELDS` | `false` | `false`：`raw_extra` 仅 `id/mobileUrl/timestamp`，`cover_url/summary/author` 留 NULL；`true`：追加 `desc/cover/author` 并回填三列 |
| `HOTSPOT_TIMEOUT_SECONDS` | `10` | 单请求超时 |
| `HOTSPOT_MAX_ATTEMPTS` | `3` | 单源最多尝试次数（仅 429/5xx/网络重试） |
| `HOTSPOT_SCHEDULER_ENABLED` | `true` | 定时采集总开关 |
| `HOTSPOT_FETCH_INTERVAL_MINUTES` | `60` | 定时间隔（尊重上游约 60 分钟缓存） |
| `HOTSPOT_FETCH_ON_STARTUP` | `false` | 启动时是否立刻抓一次 |

> 成本：本次**不接 LLM、不耗 token**；`agent_runs` 的 `llm_model` 写空串、token 三列保持 NULL。

---

## 九、自检与实测证据

**离线自检**（不联网、不请求 DailyHotApi；用 `httpx.MockTransport` + 真实本地库）

```powershell
backend\.venv\Scripts\python.exe scripts\selftest_hotspot.py
```

覆盖：源合规 / 重试语义（4xx 不重试、429·5xx 重试到 `max_attempts`）/ 归一化（脏数据丢弃、
热度解析、`KEEP_EXTRA_FIELDS` 开关）/ `content_hash` 稳定性 / **两轮采集 upsert 幂等** /
调度器配置（`interval`·`coalesce`·`max_instances`、开关关闭不注册）/ 路由注册 /
合规扫描（源码无小红书域名、无 Cookie / 签名头）；`[SELFTEST]` 数据收尾**物理清理**。

**在线冒烟**（真实网络 + 真实库；需 dailyhot 与后端在跑）

```powershell
docker compose up -d # db / redis / dailyhot
cd backend; uvicorn app.main:app --reload --port 8000
backend\.venv\Scripts\python.exe scripts\smoke_hotspot.py
```

覆盖：首次采集落库条数 / `content_hash` 无重复 / 二次刷新幂等（只更新不新增）/
列表分页·过滤·排序 / `/sources`·`/runs` / **并发互斥（两次同时 refresh → 一次 409）** /
**坏源容错（partial，好源照常入库）** / **定时到点触发一次作业**。
dailyhot 或后端不可达时脚本以退出码 2 **安全拒绝**并给出指引（不伪造结果）。

---

## 十、清理演示数据（可选）

公开热点数据**默认保留**，供此前做演示。如需清空：

```sql
DELETE FROM agent_runs WHERE run_type = 'hotspot_fetch';
DELETE FROM hotspots WHERE hot_date = CURRENT_DATE;
```

---

## 十一、与前后步骤的关系

| 步骤 | 关系 |
| --- | --- |
| 此前 | 复用 `hotspots` / `agent_runs` 表，**零结构变更、零新迁移** |
| 此前 | 复用 `schemas → crud → routes` 分层、统一错误体、`Page` 分页、`CRUDBase` |
| Agent Harness 步 | 复用 `AgentRunRecorder`（审计 + 脱敏）；**调度器启停并入同一个 lifespan** |
| 此前（选题） | **直接读 `hotspots` 表**取当天 `status=new` 的热点（不依赖本次 HTTP 接口）；写入 `topics` |
| 此前（前端） | 消费 `/api/v1/hotspots` 四个端点；「采纳 / 忽略」届时走 `hotspot.status` 流转（本次仅落 `new`） |
| 第 18 / 19 步 | 与 `knowledge_snippets` 无关；如做「热点 → 选题」的向量去重，届时另议 |

---

## 十二、待人工验证 / 未验证项（如实列出）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 官方镜像 `imssyy/dailyhot-api:latest` 的**容器化启动** | ⚠️ **本机未验证** | 本机镜像加速器对该镜像返回 `denied` / 不在白名单，Docker Hub 直连被墙。**采集链路本身已实测通过**（见 README 第九节：用同版本 2.0.8 的服务实测），但官方镜像的拉取与容器内 healthcheck 需在可访问 Docker Hub 的环境确认 |
| `healthcheck` 的 `node` 一行探针 | ⚠️ 依赖官方镜像内含 `node` | 该镜像本身就是 Node 应用，`node` 必然存在；但**未在官方镜像内实测**。若探针失败，容器会显示 unhealthy 但**不影响采集功能**（后端对 dailyhot 容错） |
| 定时**长周期**（60 分钟）真实到点 | ⚠️ 未等满一周期 | 已用「短周期到点触发」实测作业确实按时执行（见 README）；60 分钟的周期未实际等待 |
| 上游各源的**当日可用性** | 随时间变化 | `weibo` / `baidu` 等源当时的问题可能变化，`HOTSPOT_ENABLED_SOURCES` 可按需调整 |

## 十三、本次【不做】

前端页面与 Next 代理、选题/创作（不接 LLM）、热点采纳与忽略动作、
RAG / 向量去重、Celery / 分布式队列、登录认证、生产部署、修改业务表结构、
采集小红书（**产品红线，永不做**）。
