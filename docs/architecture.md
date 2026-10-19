# 架构说明 · xhs-agent（早期 + Agent Harness）

> 本文件描述**当前已搭好的骨架、数据层、业务链路、Agent 基座、热搜采集服务与第一个业务图**，一页说明：目录职责、端口、
> 请求链路、中间件用途、数据模型与迁移、**API 分层与前端代理范式**、**LLM 智能流程底座**、**热搜采集与定时调度**、
> **选题 Agent 业务图**、以及后续步骤映射。
> 此前搭架子通链路、此前建核心表、此前打通**账号 / 人设 CRUD**（后续业务模块的范式）、
> Agent Harness 步立起 **LangChain / LangGraph 基座**（选题/创作/诊断/RAG 的统一运行底座）、
> 完成**热搜服务化 + 定时采集**（DailyHotApi 容器 + 幂等落库 + APScheduler，仍不接 LLM）、
> 完成**选题 Agent**（在基座上写的**第一个业务图**：多节点 + 条件边 + 有限回路 + 检查点 + 审计回填）。

---

## 一、功能定位与产品边界

- 产品：内容创作辅助 —— 热搜采集 → 选题 → 创作辅助 → 封面 → 数据分析。
- **边界（红线，全程不变）**：人工审核并手动发布；**不登录小红书、不抓取登录态、不自动发布**。
- monorepo 骨架 + 本地基础设施 + 一条最窄的端到端链路。
- **数据层** —— 10 张核心表 + Alembic 迁移 + 离线/在线自检。**不写 REST 接口 / CRUD**。
- **账号 / 人设 CRUD** —— 确立 `schemas → crud → routes` 分层 + 前端服务端代理范式。
- Agent Harness 步：**LLM 智能流程基座** —— LangGraph `StateGraph` 编排 + Postgres 检查点 +
 `interrupt` 人在回路 + 统一审计。**不实现任何业务图、不新增业务路由、不改业务表**。
- **热搜服务化 + 定时采集** —— DailyHotApi 容器 + 归一/合规闸门 + 幂等落库 + APScheduler 调度 +
 `/api/v1/hotspots`。**不接 LLM、不引入 Celery、不改业务表结构、不新增迁移、不做前端**。
- **选题 Agent（第一个业务图）** —— LangGraph 真图（5 节点 + 条件边 + 有限回路 + 检查点）+
 service 串行批量与单点容错 + `/api/v1/topics` + **审计回填（不改 harness）**。
 **唯一结构变更 = 已定案的迁移 `0002`（3 个可空列）**；**不做前端、仅 `trigger=manual`、
 不写正文/封面（第 12/13 步）、不做 RAG（第 18/19 步）**。

---

## 二、monorepo 目录职责

```
xhs-agent/
├── docker-compose.yml # 本地中间件编排（PostgreSQL / Redis / dailyhot），不含前后端容器
├── .env.example # 根配置模板：PG / Redis + DeepSeek 预留段 + HOTSPOT_API_BASE_URL
├── docker/postgres/init/ # 首启自动执行的 SQL（建 vector 扩展）
├── scripts/
│ ├── install-docker.ps1 # Windows 下检测并安装 / 启动 Docker Desktop
│ ├── healthcheck.py # 纯标准库一键连通自检
│ ├── selftest_db.py # 数据层离线自检（不连库；此前改为多迁移合并）
│ ├── verify_schema.py # 数据层在线验收（真连库 + 向量测试）
│ ├── seed_dev.py # 可选开发种子
│ ├── selftest_api.py # 接口层离线自检（不起服务、不连库）
│ ├── smoke_crud_api.py # CRUD 在线冒烟 + [SMOKE] 数据自清理
│ ├── selftest_hotspot.py # 热搜采集离线自检（MockTransport，54 项）
│ ├── smoke_hotspot.py # 热搜采集在线冒烟（需 dailyhot，31 项）
│ ├── selftest_topic_agent.py # 选题 Agent 离线自检（脚本模型驱动，42 项）
│ └── check_topic_agent.py # 选题 Agent 真实 LLM 检查（需 key，手动运行）
├── docs/
│ ├── architecture.md # 本文件
│ ├── agent_harness.md # Agent Harness 基座
│ ├── hotspot_service.md # 热搜采集服务
│ └── agent_topics.md # 选题 Agent 业务图
├── frontend/ # Next.js（App Router）+ TS + Tailwind + shadcn/ui（此前未改动）
│ └── src/
│ ├── lib/
│ │ ├── api-server.ts # 服务端代理封装
│ │ ├── api-client.ts # 浏览器侧封装 + ApiError
│ │ └── types.ts # 与后端 schema 对应的 TS 类型
│ └── app/
│ ├── page.tsx # 环境自检首页
│ ├── accounts/page.tsx # 账号与人设页面
│ └── api/
│ ├── health/route.ts # 健康检查代理
│ └── v1/… # 业务薄代理
└── backend/ # FastAPI + SQLAlchemy 2 + psycopg3 + Pydantic v2
 ├── alembic.ini # sqlalchemy.url 留空；保持纯 ASCII
 ├── migrations/ # Alembic 迁移工程
 │ ├── env.py # 从 settings.DATABASE_URL 注入 + 导入 app.models
 │ └── versions/
 │ ├── 0001_baseline.py # 10 表 + 约束 + 索引 + HNSW
 │ └── 0002_topic_agent_fields.py # 3 个可空列（topics×2 + agent_runs×1）
 └── app/
 ├── main.py # 应用入口：CORS、router 注册、4 个统一异常处理器、
 │ # **唯一 lifespan（LangGraph 检查点 + LangSmith + 热搜调度器）**
 ├── core/
 │ ├── config.py # pydantic-settings 配置（LLM_* / AGENT_* / HOTSPOT_* / **此前 TOPIC_***）
 │ ├── errors.py # AppError + 统一错误体 + 错误码映射
 │ └── timeutil.py # Asia/Shanghai now_local/today_local
 ├── agent/ # Agent Harness 基座（Agent Harness 步）+ 业务图
 │ ├── errors.py runner.py checkpoint.py
 │ ├── llm/factory.py parsing/json_repair.py state/base.py graph/base.py
 │ ├── tracing/recorder.py tracing/langsmith.py
 │ └── topics/ # 第一个业务图（选题 Agent）
 │ ├── state.py contracts.py prompts.py
 │ ├── graph.py # 5 节点 + 条件边 + 有限回路 + 检查点
 │ └── service.py # generate_for_hotspots：串行批量/容错/跳过 + 审计回填
 ├── db/
 │ ├── base.py # 统一 Declarative Base + 命名约定
 │ ├── session.py # SQLAlchemy 引擎 / 会话工厂 / get_db 依赖
 │ └── redis_client.py # Redis 客户端 + ping（此前未使用 Redis）
 ├── models/ # 10 张表模型（此前未改动）
 │ ├── __init__.py # 汇总导入（Alembic 靠它发现全部表）
 │ ├── enums.py # 枚举集中定义（此前仅加 RunType.HOTSPOT_FETCH）
 │ ├── account.py persona.py hotspot.py topic.py
 │ ├── note.py # notes + covers
 │ └── metric.py diagnostic.py knowledge.py agent_run.py
 ├── services/ # 业务服务层（新增，编排而非 HTTP）
 │ └── hotspot/
 │ ├── schemas.py # HotspotItem（对齐 hotspots 表）+ 采集摘要结构
 │ ├── client.py # DailyHotClient：sync httpx + tenacity 重试
 │ ├── adapters.py # 各源 data[] 归一 + 合规闸门（禁 xhs 系）
 │ ├── runner.py # ThreadPoolExecutor 并发采集（单源异常隔离）
 │ ├── service.py # collect_and_store：加锁 + 落库 + 写 agent_runs 审计
 │ └── scheduler.py # APScheduler BackgroundScheduler（合并入唯一 lifespan）
 ├── schemas/ # Pydantic v2 出入参
 │ ├── common.py # Page[T] / ErrorOut / ErrorOutSpec / 复用校验器
 │ ├── account.py persona.py
 │ ├── hotspot.py # 列表 / 刷新 / 源统计 / 运行记录出参
 │ └── topic.py # 生成/微调入参 + TopicListItem / TopicOut / TopicRunOut
 ├── crud/ # 数据访问层
 │ ├── base.py # 泛型 CRUDBase（get/get_multi/count/create/update/archive）
 │ ├── account.py persona.py
 │ ├── hotspot.py # 幂等 upsert（ON CONFLICT content_hash）
 │ └── topic.py # 筛选构造 / JOIN 人名账号名 / 单事务 insert_many / 判重
 └── api/
 ├── deps.py # 分页依赖 + get_db 转发
 ├── health.py # /health 与 /health/ready（第 9 / 10 步未改动）
 └── routes/ # 业务路由
 ├── accounts.py personas.py
 ├── hotspots.py # /api/v1/hotspots
 └── topics.py # /api/v1/topics（生成/列表/runs/详情/微调）
```

**分层约定**：前端只通过 HTTP 调用后端；后端**严格单向**：

```
api/routes → crud → models → db
 │ ▲
 └→ schemas ─┘（校验与序列化）
 └→ core/errors（统一错误）

services/→ crud → models → db
 ▲
api/routes 或 scheduler（触发方，二者共用同一把服务层锁）
```

- 路由层**只做参数解析与依赖编排**，不出现任何 SQLAlchemy 表达式（`scripts/selftest_api.py` 会静态断言）；
- 配置集中在 `core/config.py`；错误集中在 `core/errors.py`；分页参数集中在 `api/deps.py`；
- **建表只走 Alembic**，模型层不做 DDL。

---

## 三、端口表

| 端口 | 服务 | 说明 |
| --- | --- | --- |
| 3000 | 前端 Next.js | 开发服务器（`npm run dev`） |
| 8000 | 后端 FastAPI | `uvicorn app.main:app --reload --port 8000` |
| 5432 | PostgreSQL 16 + pgvector | Docker Compose 的 `db`，仅绑定 `127.0.0.1` |
| 6379 | Redis 7 | Docker Compose 的 `redis`，仅绑定 `127.0.0.1`（此前未使用） |
| 6688 | DailyHotApi | Docker Compose 的 `dailyhot`，仅绑定 `127.0.0.1`（新增） |

> 中间件端口只映射到本机回环（`127.0.0.1`），不暴露到局域网。

---

## 四、请求链路

**① 健康检查链路**

```
浏览器 (http://localhost:3000)
 │ 首页 page.tsx（客户端组件）请求本站 /api/health
 ▼
Next.js Route Handler src/app/api/health/route.ts
 │ 服务端 fetch ${API_BASE_URL}/health/ready （API_BASE_URL 默认 http://localhost:8000）
 ▼
FastAPI GET /health/ready
 ├─ PostgreSQL SELECT 1
 ├─ pgvector 查询 pg_extension 是否存在 vector 扩展
 └─ Redis PING
 ▼
返回 {"status":"ok|degraded","checks":{"database":bool,"pgvector":bool,"redis":bool}}
 │ （任一不通过 → HTTP 503，但响应体仍带完整 checks）
 ▼
前端状态页渲染四项绿✓/红✗/灰未测 + 原始 JSON，并提供「重新检测」按钮
```

**② 业务链路（与 ① 同构）**

```
浏览器 http://localhost:3000/accounts
 │ page.tsx 只请求【本站】/api/v1/**（永远拿不到后端地址）
 ▼
Next.js Route Handler（服务端薄代理）
 /api/v1/accounts[/{id}] /api/v1/personas[/{id}] ← GET / POST / PATCH
 │ 统一调用 src/lib/api-server.ts → proxyToBackend
 │ · API_BASE_URL 是【服务端】变量（无 NEXT_PUBLIC_ 前缀）
 │ · cache: "no-store" + AbortSignal.timeout(15s)
 │ · 原样透传后端的状态码与响应体
 ▼
FastAPI /api/v1/**
 routes（参数解析 / 分页依赖 / 依赖注入）
 → schemas（Pydantic 校验 + 序列化）
 → crud（唯一校验 / 过滤 / 默认人设规则）
 → models（SQLAlchemy ORM）
 → PostgreSQL
 ▼
成功：对象 或 {"items":[…],"total":n,"page":1,"page_size":20}
失败：{"status":"error","code":"not_found|conflict|validation|bad_request","message":…,"details":…}
```

> 后端地址只存在于服务端环境变量，浏览器产物里不含它 —— 这是本工程的安全约定。

**③ 热搜采集链路（**不经前端**，两条触发路径共用同一把锁）**

```
触发方
 ├─ 手动：POST /api/v1/hotspots/refresh（可带 {"sources":[...]}）
 └─ 定时：APScheduler interval job（HOTSPOT_FETCH_INTERVAL_MINUTES，默认 60min）
 │ （两者都调用 services/hotspot/service.collect_and_store）
 ▼
service 层：模块级 threading.Lock
 · 已在运行 → 手动触发返回 409 fetch_in_progress；定时触发静默跳过（不排队）
 · 拿到锁 → 创建 AgentRunRecorder(run_type=hotspot_fetch, trigger=manual|scheduled)
 ▼
runner：ThreadPoolExecutor（上限 4 worker）按源并发
 · 合规闸门：xhs / xiaohongshu 等源在 adapters 层直接拦截（BLOCKED），不发请求
 · 单源异常隔离：某源失败不拖垮其它源（计入 failed，整体置 partial）
 ▼
client：sync httpx GET {base}/{source}?limit=N[&cache=false]
 · tenacity 重试：仅 429 / 5xx / 网络异常；4xx 立即失败（不重试）
 · 响应 data[] → adapters.normalize_items → HotspotItem
 ▼
crud/hotspot.upsert_many（每源一个会话）
 · content_hash = sha256(source + source_item_id + hot_date)（hot_date = Asia/Shanghai 今日）
 · INSERT ... ON CONFLICT (content_hash) DO UPDATE → 区分 new / updated，同日不重复
 · status 固定 HotspotStatus.NEW；KEEP_EXTRA_FIELDS=false 时 raw_extra 仅保留最小键集
 ▼
回写 agent_runs（outputs=每源 new/updated/failed/blocked/fetched，latency_ms，status=success|partial|failed）
 ▼
读侧：GET /api/v1/hotspots（分页/筛选/排序）、/sources（源状态快照）、/runs（最近运行记录）
```

**④ 选题 Agent 链路（HTTP 触发 → 进程内跑 LangGraph 业务图）**

```
POST /api/v1/topics/generate {"persona_id":…, "hotspot_ids":[…], "per_hotspot":3}
 │ （route 只解析参数 + 调 service；不写 SQLAlchemy）
 ▼
services 编排：app/agent/topics/service.py generate_for_hotspots
 · 人设校验（缺失 404 / 已归档 400）；解析热点（显式 ids 或「今天 + status=new」取前 N）
 · force=False 跳过已覆盖热点；**串行**逐个热点（TOPIC_CONCURRENCY=1）；单点失败不中断（整批 partial）
 ▼ 每个热点一条线程（thread_id = topic-<hotspot_id>-<run_id>）
run_agent(kind="topic_generation", build_fn=build_topic_graph, …) ← Harness 统一入口
 ▼
LangGraph StateGraph（**本次核心**）
 load_context ─▶ generate_topics ─▶ validate_dedupe ─┬─(够数/达标)──────────▶ persist
 ▲ └─(不合格且未超限)──┐
 └──────────── retry_count++ / feedback ─────────────────────────────┘
 └─(到上限仍空)─────▶ give_up
 · generate_topics：get_chat_model → invoke_json（JSON 容错重试）；r.raw_text → state.llm_raw
 · 检查点：PostgresSaver（框架表隔离在独立 schema `langgraph`）
 ▼
crud/topic.insert_many 单事务批量写 topics（score == confidence；tags 已剥 `#`；status=draft）
 ▼
PostgreSQL public.topics（source_hotspot_ids / source_trace 溯源）
 ▼
service 层【审计回填】（**不改 harness**）：按 run_id 合并 agent_runs.outputs 业务计数 + 写 llm_raw（脱敏）
 ▼
读侧：GET /api/v1/topics（分页/筛选/排序）、/runs（最近运行记录）、/{id}（详情）、PATCH /{id}（微调）
```

> 详细图 / 节点 / 条件边 / 回路 / 字段映射 / 状态机 / 配置 / 证据 / 未验证项见 **[`agent_topics.md`](agent_topics.md)**。
> 合规：只消费此前采来的**公开榜单**热点；**不请求任何小红书域名 / 登录态**；不自动发布。

---

## 五、API 层与前端代理范式

**后端四层职责**

| 层 | 目录 | 只做 | 不做 |
| --- | --- | --- | --- |
| 路由 | `app/api/routes/` | 解析参数、调用 crud、组装响应模型 | 不写 SQL / 不写业务规则 |
| 校验 | `app/schemas/` | 出入参结构、字段约束、序列化 | 不访问数据库 |
| 数据 | `app/crud/` | 查询条件、唯一冲突翻译、业务规则（默认人设唯一） | 不感知 HTTP |
| 模型 | `app/models/` | ORM 映射 | 不做 DDL（建表只走 Alembic） |

- 分页统一由 `app/api/deps.py` 的 `pagination` 依赖产出（`page ≥ 1`，`page_size` 默认 20 / 上限 100）；
- 错误统一由 `app/core/errors.py` 的 `AppError` 抛出，`app/main.py` 注册 4 个异常处理器
 （业务异常 / 请求校验 / 框架 HTTPException / 兜底 500）渲染成同一结构；
- **归档代替删除**：`CRUDBase.archive` 是唯一「删除」原语，`PATCH {"status":"archived"}` 触发，
 全项目无物理 DELETE 端点。

**前端代理范式**

| 文件 | 运行位置 | 职责 |
| --- | --- | --- |
| `src/lib/api-server.ts` | 服务端 | 读 `API_BASE_URL`、超时、**透传状态码与响应体**；后端不可达返回 503 结构化错误 |
| `src/app/api/v1/**/route.ts` | 服务端 | 极薄的转发（转发 query / body），不写业务逻辑 |
| `src/lib/api-client.ts` | 浏览器 | 只请求本站 `/api/v1`，把非 2xx 转成 `ApiError(status, code, message, details)` |
| `src/app/accounts/page.tsx` | 浏览器 | 受控表单 + 手写校验 + 加载/空/错误三态；无表单库、无状态库 |

---

## 六、中间件用途（现阶段仅预留）

| 中间件 | 当前状态 | 后续用途 |
| --- | --- | --- |
| PostgreSQL + **pgvector** | 已启动、已建 `vector` 扩展；**此前已建 10 张核心表**（含 1 张向量表）；**另独立 schema `langgraph` 存放框架检查点表**；此前起 `hotspots` 有真实数据 | **此前**用 pgvector 做 RAG 向量检索（写入 `knowledge_snippets.embedding`） |
| **Redis** | 已启动，**未写入任何数据** | 定时调度改用 **APScheduler 进程内调度**（未用 Redis），Redis 仍留待后续步骤（队列 / 缓存） |
| **DailyHotApi** | 已启动（容器 `xhs-dailyhot`，`127.0.0.1:6688`），此前采集的公开榜单数据源 | 无状态，内存缓存（`CACHE_TTL=3600`）；后续如需持久化缓存再接 Redis |

---

## 六之二、Agent Harness 基座（Agent Harness 步）

**定位**：所有**需要大模型**的智能流程（选题 / 创作 / 诊断 / RAG）的统一运行底座。
LangGraph 只编排 LLM 流程；**无 LLM 的定时采集**归 APScheduler（此前已落地）；
**人工审核并手动发布**归业务表状态流转 + 网页操作。

```
业务调用方 ──run_agent──► runner ──► LangGraph StateGraph ──► ChatOpenAI(DeepSeek, OpenAI 兼容)
 │ │ + invoke_json（JSON 容错）
 │ └── checkpointer ──► PostgresSaver（schema=langgraph）
 └── AgentRunRecorder ──► agent_runs（业务表，Alembic 管）
 └─ 可选 LangSmith（默认关闭）
```

| 关注点 | 落点 |
| --- | --- |
| 编排 / 人在回路 | `graph/base.py`（构图/编译/配置）+ `interrupt` / `Command(resume=...)` |
| 模型构造 | `llm/factory.py`（唯一入口，未配 key / 开关关闭时**不发起真实调用**） |
| 结构化输出 | `parsing/json_repair.py`（围栏 / 裸控制字符 / 噪声三层兜底） |
| 状态 | `state/base.py`（`BaseAgentState`，含 `usage` / `parse_attempts` / `node_trace`） |
| 持久化 | `checkpoint.py`（**框架表隔离在独立 schema**，不写进业务迁移） |
| 审计 | `tracing/recorder.py`（写 `agent_runs`，内置密钥脱敏） |

> 详细分层图、职责边界、框架表策略、成本公式与待验证清单见 **[`agent_harness.md`](agent_harness.md)**。
> ⚠️ `app/main.py` **只有一个** `lifespan`；热搜调度器启停**已合并进它**（见下一节），
> 后续步骤若再引入常驻组件，**同样必须并入同一个 lifespan，严禁新建第二个**。

---

## 六之三、热搜采集服务

**定位**：无 LLM 的**公开榜单采集 + 幂等落库**，由 REST（手动）与 APScheduler（定时）两条路径触发。
**`app/services/` 是本工程新引入的「服务层」**（业务编排），与 `crud`（数据访问）分层互补。

| 关注点 | 落点 |
| --- | --- |
| 数据源 | 容器 `xhs-dailyhot`：`GET /{source}?limit=N[&cache=false]` |
| 归一 | `services/hotspot/adapters.py`（`data[]` → `HotspotItem`，字段对齐 `hotspots` 表） |
| 合规闸门 | `adapters.FORBIDDEN_SOURCES`（xhs/小红书系）→ `ensure_source_allowed` 抛错拦截 |
| 采集容错 | `client.py` tenacity 仅重试 429/5xx/网络；`runner.py` 单源异常隔离（置 failed） |
| 幂等 | `crud/hotspot.py`：`content_hash=sha256(source+source_item_id+hot_date)` + `ON CONFLICT DO UPDATE` |
| 并发互斥 | `service.py` 模块级 `threading.Lock`：手动触发冲突 → 409；定时触发冲突 → 跳过 |
| 审计 | 复用 Harness 的 `AgentRunRecorder`，`run_type=hotspot_fetch`，`llm_*` 全空 |
| 调度 | `scheduler.py`：`BackgroundScheduler` + `interval` job（`coalesce=True, max_instances=1`） |
| 生命周期 | **并入 `app/main.py` 唯一的 lifespan**：启动 `start_scheduler`，关闭 `shutdown_scheduler` |

**为何用 APScheduler 而非 Celery**：本工程后端是**单机 Windows + 同步栈**，采集任务轻量（分钟级、
无跨机分发、无需消息中间件），Celery 需额外引入 broker（Redis/RabbitMQ）+ worker 进程 + 部署复杂度，
收益不匹配。APScheduler 是**进程内**调度，零外部依赖、与现有同步栈天然契合。

> 详细链路图、去重语义、配置表、自检证据与未验证项见 **[`hotspot_service.md`](hotspot_service.md)**。

---

## 六之四、选题 Agent 业务图

**定位**：**在 Harness 基座上写的第一个业务图** —— 把一个热点 → K 个可写选题，`run_type=topic_generation`。
它证明了「业务图怎么写」：真 LangGraph（多节点 + 条件边 + 有限回路 + 检查点），**不退化**为单次模型调用。

| 关注点 | 落点 |
| --- | --- |
| 图结构 | `agent/topics/graph.py`：5 节点 `load_context / generate_topics / validate_dedupe / persist / give_up` |
| 条件边 | `route_after_validate`：按 `validated` 数、`per_hotspot`、`retry_count` 三路分支 |
| 有限回路 | 不合格 → 回 `generate_topics`（带 `feedback`），`retry_count` 每轮自增，达 `AGENT_JSON_MAX_ATTEMPTS` 即停 |
| 状态 | `agent/topics/state.py`：`TopicState(BaseAgentState)`，只放**纯 dict 快照**与业务计数（模型不进状态） |
| 契约 | `agent/topics/contracts.py`：`TopicCandidate`（tags 剥 `#`、outline/keywords 清洗去重） |
| 提示词 | `agent/topics/prompts.py`：JSON 输出契约 `{"topics":[…]}` + 选题方法论（痛点/数字/反差/人群/场景/收藏点） |
| 编排 | `agent/topics/service.py`：`generate_for_hotspots` 单热点一张图 + 串行批量 + 单点容错 + `force` 跳过 |
| 审计回填 | service 在 `run_agent` 返回后用 `run_id` 合并 `agent_runs.outputs` 业务计数 + 写 `llm_raw`（脱敏） |
| 落库 | `crud/topic.py`：`insert_many`（单事务）；`score == confidence`；`source_hotspot_ids`/`source_trace` 溯源 |
| HTTP | `api/routes/topics.py`：`/api/v1/topics`（generate / 列表 / runs / 详情 / PATCH 微调） |
| 迁移 | `0002`（唯一结构变更）：`topics.content_outline` / `topics.tags` / `agent_runs.llm_raw` 三个可空列 |

**为何审计要「回填」而不是改 Harness**：`run_agent` 已统一写 token / `json_parse_attempts` / `node_trace` /
`status` / `latency_ms`，但其 `outputs` 只写 output 的**键名**。业务明细（`topic_ids` / `generated` /
`persisted` / `rejected` / `skipped`）属**业务语义**，由 service 用 `run_id` 合并回填——这样 Harness 基座
零改动，业务审计也不缺口。

**为何 `llm_raw` 放 `agent_runs` 不放 `topics`**：一次热点一份原始输出；放 `topics` 会随选题条数重复、臃肿，
且它属于「运行审计」而非「选题内容」。接口一律**不返回** `llm_raw`。

> 详细图、字段映射表、状态机、配置项、自检证据与未验证项见 **[`agent_topics.md`](agent_topics.md)**。

---

## 七、数据层

**分层**

```
models/（SQLAlchemy 2 模型，10 张表，无 DDL）
 ▲
db/base.py（统一 Base + 命名约定） db/session.py（engine / SessionLocal / get_db）
 ▲ ▲
migrations/env.py ── 从 core/config.settings.DATABASE_URL 注入 ──┘
 ▲
migrations/versions/0001_baseline.py（唯一建表途径）
```

**10 张表**（完整字段字典、状态机、索引决策见 `README.md` 第七节）

```
accounts ──1:N──> personas ──┐
 │ └──1:N──> topics ──────┼──1:N──> notes ──1:N──> covers
 │ │ ├──1:N──> note_metrics ──1:N──> diagnostics
 └──1:N──> agent_runs <─────┘（上下文弱引用） └──1:N──> diagnostics

hotspots 独立快照表（此前起有真实数据：5 源 × 20 条/天，幂等去重）
knowledge_snippets 独立向量表（第 18/19 步），embedding vector(1024) + HNSW 余弦索引
agent_runs 审计表（token / JSON 稳定性 / 排障），不记录任何 API Key；此前起 `llm_raw` 存模型原始输出
```

> **表结构变更**：唯一迁移 `0002` 给 `topics` 加 `content_outline`(JSONB) / `tags`(JSONB)、
> 给 `agent_runs` 加 `llm_raw`(Text)，**均为可空**；**不加表 / 外键 / 索引 / 枚举取值**，业务表仍严格 10 张。

**关键约定**

| 约定 | 内容 |
| --- | --- |
| 主键 | `UUID` + `gen_random_uuid` |
| 时间戳 | 统一 `timestamptz`；`created_at` 默认 `now`；`updated_at` 走 ORM `onupdate` |
| 枚举 | 落 `VARCHAR`（`native_enum=False`），取值集中在 `app/models/enums.py`，新增取值无需迁移 |
| 半结构化 | 统一 `JSONB`；标签 / 热点 id 用 JSONB 数组，**不建关联表**（表数严格 10 张） |
| 外键策略 | `notes.account_id`→RESTRICT；`covers/note_metrics/diagnostics.note_id`→CASCADE；其余上下文→SET NULL |
| 建表 | **只走 Alembic**；全仓无 `create_all`；`session.py` 导入时不建表 |
| 向量 | `embedding vector(1024)` 为**此前占位**；维度变更需新增迁移并重建 HNSW 索引 |
| 连接 | 唯一来源 `settings.DATABASE_URL`，主机 `127.0.0.1`；`alembic.ini` 的 url 留空 |

---

## 八、与后续步骤的映射（早期）

| 步骤 | 内容 | 与本次的衔接点 |
| --- | --- | --- |
| 此前 | 核心表与数据库迁移 | ✅ **已完成**（10 表已建、downgrade/upgrade 可逆、向量检索已验证） |
| 此前 | 账号与人设 CRUD | ✅ **已完成** —— 复用 `accounts` / `personas` 模型，确立 `schemas→crud→routes` 范式 |
| Agent Harness 步 | LangChain / LangGraph 基座 | ✅ **已完成**（离线 59 项全绿；真实 LLM **11/11 已验证**，`model=deepseek-flash`） |
| 此前 | 热搜服务化 + 定时采集 | ✅ **已完成** —— `hotspots` 真实落库（幂等）；APScheduler 调度已并入唯一 lifespan；`/api/v1/hotspots` 就绪；**不改表结构、不新增迁移、不接 LLM、未用 Celery / Redis** |
| 此前 | 选题 Agent | ✅ **已完成** —— Harness 基座上的**第一个业务图**：真 LangGraph（5 节点 + 条件边 + 有限回路 + 检查点）+ service 串行批量与单点容错 + `/api/v1/topics` + 审计回填。**唯一结构变更 = 迁移 `0002` 三个可空列**（`topics.content_outline`/`topics.tags`/`agent_runs.llm_raw`），其余映射到此前已有列；**不改 harness 基座、不做前端、仅 `trigger=manual`**。真实 LLM **11/11 已验证**（`check_topic_agent.py`，3 轮 18 条选题、`json_parse_attempts` 恒为 1、17242 tokens） |
| 此前 | 创作 Agent | 同一 `run_agent` 入口；写 `notes`；`personas.system_prompt` 在此使用 |
| 此前 | 封面模块 | 写 `covers`；迁入 `cover_spike` 的模板与 `captureToPng` |
| 此前 | 数据分析 / 录入 | 写 `note_metrics`（截图识别 `screenshot_spike` 补测后启用） |
| 此前 | 单篇诊断 | 复用 Harness；写 `diagnostics` |
| 第 18 / 19 步 | RAG / 知识库 | 写 `knowledge_snippets` 并生成真实 embedding（注意维度决策）；审计复用 `agent_runs` |
| 此前 | 生产部署 | 为前后端补 Dockerfile / CI（明确不做） |

> 说明：此前**不迁入任何 spike 代码**；上表的「迁入」均发生在对应后续步骤。
> 此前只碰 `accounts` / `personas`；Agent Harness 步**不新增任何业务路由、不改业务表结构**；
> 此前只碰 `hotspots`（读侧走 `crud`，写侧走新 `services` 层），**同样不改业务表结构、不新增迁移**；
> 此前是新 `agent/topics` **业务图** + `crud/topic.py` + `schemas/topic.py` + `routes/topics.py`，
> **唯一结构变更为已定案的迁移 `0002`（3 个可空列）**，业务表仍 10 张。
