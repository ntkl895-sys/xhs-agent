# 选题 Agent · xhs-agent

> 在 **LangChain / LangGraph harness** 上实现的**第一个业务图**：把一个热点 → K 个可写选题。
> 本文件说明：图结构（节点 / 条件边 / 有限回路）、批量与容错、**字段映射**、
> 状态机、配置、审计回填机制、自检证据、成本口径与**真实 LLM 验证结果**。

---

## 一、定位与边界

- **定位**：需要大模型的业务流程（`run_type=topic_generation`），由 harness 的
 `run_agent` 统一运行、统一审计。
- **本次做的**：LangGraph 业务图 + service 编排 + `/api/v1/topics` 接口 + 自检与文档。
- **本次不做的**：前端 / Next 代理、选题定时（**仅** `trigger=manual`）、
 正文与封面（第 12/13 步）、违禁词库、RAG。
- **红线**：人工审核 + 手动发布；不登录小红书 / 不抓登录态 / 不自动发布；
 Key 只在本机 `.env`，**绝不**入代码 / 日志 / `agent_runs` / `topics` / `.example`。
- **不改 harness 基座**：`runner.py` / `recorder.py` / `graph/base.py` / `state/base.py` /
 `factory.py` / `json_repair.py` 一律未改；业务审计缺口由 service 层回填解决（见第七节）。

---

## 二、图：节点 / 条件边 / 有限回路

```
 START
 │
 ▼
 ┌───────────────┐
 │ load_context │ 独立 SessionLocal：读 persona + hotspot
 └───────┬───────┘ 写纯 dict 快照（persona_snapshot / hotspot_snapshot）
 │ 缺失 / 已归档 → AgentNonRetryableError
 ▼
 ┌───────────────┐
 ┌─────►│generate_topics│ 闭包注入的 model + invoke_json（JSON 容错）
 │ └───────┬───────┘ node 带 RetryPolicy（只重试瞬时错误）
 │ │ r.raw_text → state.llm_raw
 │ ▼
 │ ┌────────────────┐
 │ │validate_dedupe │ TopicCandidate 校验 + 归一化判重（vs 库内未 discarded）
 │ └───────┬────────┘ 合格 → validated（累积）；不合格 → rejected + feedback
 │ │
 │ route_after_validate（条件边）
 │ │
 │ ┌──────────┼───────────────────────┬──────────────────────┐
 │ │ │ │ │
 │ │ validated < per_hotspot │ validated ≥ │ validated 为空
 │ │ 且 retry_count < 上限 │ per_hotspot │ 且已到上限
 │ ▼ ▼ ▼ ▼
 └───┘ ┌──────────┐ ┌──────────┐ ┌──────────┐
 （带 feedback │ persist │ │ persist │ │ give_up │ 抛不可重试错误
 回 generate）└────┬─────┘ └────┬─────┘ └────┬─────┘
 │ │ │
 ▼ ▼ ▼
 END END 异常 → run_agent 记 failed
```

- **必须是真 LangGraph**：多节点 + 条件边 + 有限回路 + `checkpointer` + `run_agent` 审计，
 **不退化**为单次模型调用或自研编排。
- **回路有界**：`retry_count` 每轮 `validate_dedupe` 自增；
 未达 `per_hotspot` 且 `retry_count < AGENT_JSON_MAX_ATTEMPTS`（默认 3）才回退重生成，
 避免「模型持续少给合格项」导致死循环。
- **模型绝不进状态**：状态要序列化进检查点；模型经**闭包**注入节点。

---

## 三、单热点一张图 · service 串行批量 · 单点容错

- **单热点一张图**：`generate_for_hotspots` 对每个热点各跑一次 `run_agent`，
 每次运行有自己的 `run_id` / `thread_id` / `agent_runs` 行。
- **service 串行批量**：`TOPIC_CONCURRENCY` 默认 **1**（串行），**不引入多线程 / asyncio**，
 与同步栈（同步 SQLAlchemy / 同步路由）保持一致。
- **单点失败容错**：`run_agent` 已把失败落 `agent_runs=failed` 并抛出；
 service `try/except` 捕获后记该热点 failed，**继续下一热点**，整批不中断。
- **整批状态**：`failed==0` → `success`；`succeeded==0` → `failed`；否则 `partial`。

---

## 四、字段映射（严格对齐 0002 之后的 `topics` 真实列）

| 选题字段 | 落库列 | 说明 |
| --- | --- | --- |
| title / angle / summary / pillar | 同名已有列 | 已有列 |
| content_outline | **`content_outline`（0002 新增，JSONB）** | 字符串数组 3~6 条 |
| tags | **`tags`（0002 新增，JSONB）** | 小红书 #话题标签：**纯词** 3~8 个，**不含 `#`**（解析阶段即剥除） |
| target_keywords | `target_keywords`（已有 JSONB） | SEO / 检索用关键词，**与 tags 不同**：tags 是给平台的话题词，keywords 是给搜索/归档的检索词 |
| difficulty | `difficulty`（已有枚举列） | `easy` / `medium` / `hard` |
| heat_score | `heat_score`（已有 Float） | 取自**来源热点**的 `hot_score` |
| score | `score`（已有 Float，`ix_topics_score`） | **V1：score == 模型 confidence**（透明口径） |
| llm_model | `llm_model` | 本次运行的模型 ID |
| meta | `meta`（已有 JSONB） | `{prompt_version, json_parse_attempts, confidence}` |
| status | `status` | 一律 `TopicStatus.DRAFT`（`"draft"`） |
| generated_at | `generated_at` | 当前 **UTC** 时间 |
| account_id | `account_id` | 由 `persona.account_id` 派生 |
| 溯源 | `source_hotspot_ids` = `[该热点 id]` | JSONB 数组，**不加单列外键**，热点存在性由应用层校验 |
| 溯源 | `source_trace` = `[{source,title,url,mobile_url,hot_date,hot_score,rank_position}]` | 取自 `hotspots` 行 |
| 模型原始输出 | **不写 topics** → `agent_runs.llm_raw` | 见第七节（避免重复与臃肿） |

> 因为 V1 的 `score == confidence`，「综合排序分」就是模型自评置信度；
> 列表默认 `score DESC`，走此前已建的 `ix_topics_score`。

---

## 五、`TopicStatus` 状态机（本次范围）

```
draft ──► approved （PATCH /{id}，人工评审通过）
 │
 └──────► discarded （PATCH /{id}，废弃）

writing / published 不在本次范围（属后续「笔记」步）
```

- 生成时一律落 `draft`；
- 本次 `PATCH` **仅允许** `draft → approved` 与 `draft → discarded`；
 其余流转（含 `→ writing` / `→ published`）返回 **422**（`invalid_transition`）。
- `PATCH /topics/{id}` **不联动** `hotspot.status`（此前显式操作）。

---

## 六、配置项（`backend/.env`，见 `backend/.env.example`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `TOPIC_PROMPT_VERSION` | `v1` | 写入 `topics.meta.prompt_version`，改模板必须升版本 |
| `TOPIC_DEFAULT_PER_HOTSPOT` | `3` | 单热点默认生成条数 |
| `TOPIC_DEFAULT_HOTSPOT_LIMIT` | `3` | 未指定热点时默认取几个 |
| `TOPIC_MAX_HOTSPOTS_PER_REQUEST` | `10` | 单次请求热点数硬上限 |
| `TOPIC_CONCURRENCY` | `1` | 批量并发度（1 = 串行） |
| `TOPIC_TITLE_MAX_LEN` | `20` | 标题最大长度（超出判不合格） |
| `TOPIC_MIN_TAGS` / `TOPIC_MAX_TAGS` | `3` / `8` | 话题标签个数区间 |
| `TOPIC_MIN_OUTLINE` / `TOPIC_MAX_OUTLINE` | `3` / `6` | 大纲条数区间 |

节点超时复用 `AGENT_NODE_TIMEOUT_SECONDS`（注入 `ChatOpenAI(timeout=...)`），不另设。

---

## 七、审计回填机制（**不改 harness**）

`run_agent` 自动写：`run_type` / `trigger` / `inputs` / `llm_model` / `llm_base_url` /
token 三列 / `json_parse_attempts` / `node_trace` / `status` / `latency_ms`，
但其 `outputs` **只写 output 的键名**。业务明细由 **service 在 `run_agent` 返回后用 `run_id` 回填**：

```sql
UPDATE agent_runs
 SET outputs = COALESCE(outputs, '{}'::jsonb) || CAST(:patch AS jsonb),
 llm_raw = COALESCE(:raw, llm_raw)
 WHERE id = :run_id;
```

- `patch = {topic_ids, generated, persisted, rejected, skipped}`
 （`outputs` 用 `||` **合并保留** harness 已写内容）；
- `llm_raw` = 该热点本次 `r.raw_text`，写入前过
 `app.agent.tracing.recorder.redact_secrets` 兜底，**断言不含 `sk-`**；
- **部分合格**（`0 < persisted < per_hotspot`）时，service 额外把该 run 的
 `status` 置 `partial`（harness 只按 interrupt 判 success/partial，这里按业务补正）。
- **模型原始输出为什么放 `agent_runs`**：一次热点一份，放 `topics` 会随选题条数重复、臃肿；
 且它属于「运行审计」而非「选题内容」。

---

## 八、HTTP 接口（`/api/v1/topics`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/topics/generate` | 触发生成（同步）；`persona_id` 必填，404 / 422 / 400（LLM 未就绪） |
| GET | `/api/v1/topics` | 列表：分页 + 过滤 `persona_id` / `account_id` / `status` / `hotspot_id`（JSONB 包含）/ `q` / `generated_at`；排序 `score`(默认)/`generated_at` + `order` |
| GET | `/api/v1/topics/runs` | 最近 `topic_generation` 运行记录（默认**不返回** `llm_raw`） |
| GET | `/api/v1/topics/{id}` | 详情（含 `content_outline`/`tags`/`source_trace`/`meta`；**不含** `llm_raw`） |
| PATCH | `/api/v1/topics/{id}` | 人工微调；`status` 仅 `draft→approved` / `draft→discarded` |

> ⚠️ **注册顺序**：静态路径 `/generate`、`/runs` 必须排在 UUID 路径 `/{topic_id}` **之前**，
> 否则 `/runs` 会被当作 `topic_id` 解析而 422。
> `generated_at` 过滤按 **UTC 日历日**取整日区间。

---

## 九、自检与实测证据

**离线自检** `scripts/selftest_topic_agent.py`（**不连外网、不需 key**，用脚本模型驱动）：

```
backend\.venv\Scripts\python.exe scripts\selftest_topic_agent.py
→ 通过 42 项，失败 0 项。全部 PASS ✔
```

覆盖 7 组 + 路由注册：图节点与条件边；合格响应落库与字段映射；脏 JSON 与 `parse_attempts`；
首轮不合格→回环→落库、始终不合格→failed 且无半成品；`force=False` 跳过与归一化判重；
persona 404 与单点容错 partial；无 `sk-`、无小红书域名 / 登录态。
测试数据一律 `[SELFTEST]`，结束时**物理清理** `topics` / `agent_runs` / 测试账号人设热点 / 新检查点线程，
复查残留为 0。

**真实 LLM 检查** `scripts/check_topic_agent.py`（需用户在 `backend/.env` 配置后**手动**运行）：
2 个真实热点 × 3 轮 + 最后一轮注入不存在热点验证单点容错；报告选题是否贴合人设、
大纲/标签/关键词完整性、`source_trace` 链接可达性（best-effort）、
连续 3 轮 `json_parse_attempts` 是否恒为 1、真实 token（按 token 记，不内置价格）。

**回归（本次实跑）**：`healthcheck` 四绿；`selftest_db` 15/15（已支持多迁移合并）；
`verify_schema` 13/13；`selftest_api` 37/37；`selftest_agent_harness` 59/59；
`selftest_hotspot` 54/54；`smoke_crud_api` 37/37；`smoke_hotspot` 31/31；
`alembic current` = `0002 (head)`、`alembic check` 零漂移；`public` 仍 10 张业务表（**本次不加表列**）。

---

## 十、成本口径（**不内置价格**）

- 只记 token：`agent_runs.prompt_tokens` / `completion_tokens` / `total_tokens`；
- 成本 = token × **你的实际单价**（价格随时会变，仓库不硬编码价格表）；
- 最坏情况模型调用次数 = `AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)`（默认 3 × 3 = 9），
 再叠加本图的**回路轮数**上限（`AGENT_JSON_MAX_ATTEMPTS`）。

---

## 十一、真实 LLM 验证结果（2026-10-10 晚实跑）+ 待人工项

`backend/.env` 配 `AGENT_LLM_ENABLED=true` + `LLM_API_KEY` + `LLM_MODEL=deepseek-flash` 后，
`scripts/check_topic_agent.py` 实跑 **11/11 PASS（退出码 0）**：

| 项 | 结果 |
| --- | --- |
| **真实 JSON 连续稳定性**（`json_parse_attempts` 是否恒为 1） | ✅ 3 轮 × 2 热点，**全部恒为 1** |
| **真实 token 用量** | ✅ 本次共 **17242** tokens，三列均已回填（按 token 记，**不内置价格**） |
| **真实选题质量** | ✅ 3 轮共 18 条落库并通过字段校验；例「追颁奖礼嘴馋？这3家夜宵人均60」贴合「探店碎碎念」人设 |
| 溯源链接可达性 | ✅ 18/18 |
| 单点容错（末轮注入不存在热点 id） | ✅ 整批 `partial`，好点照常、坏点如实计 `failed` |
| 冒烟残留 | ✅ `topics` / `agent_runs` / 检查点线程 **= 0** |

- ✅ **选题质量的最终取舍仍由人工审核**（红线：不自动发布）——脚本无法自动断言「愿不愿意写」，
 已将真实标题打印出来供人工阅读。
- ⚠️ 未做**跨请求并发控制**（未加全局锁）：单用户本地工具场景，本次按设计要求未引入；
 如需严格串行，可在 service 增加互斥锁（模式同此前热搜服务）。
- 🐞 **本次实测暴露并修复一处真实缺陷**：`check_topic_agent.py` 原清理只删「成功的 run」，
 失败的 run 与检查点线程会残留。已改为**运行前后差集**清理（同 `selftest_topic_agent.py` 范式）；
 `check_llm_harness.py` 有同类问题，一并修复。

---

## 十二、本次【不做】

- 不做前端 / Next 代理；
- 不做选题**定时**（仅 `trigger=manual`）；
- 不写正文 / 封面 / 违禁词库（第 12/13/19 步）；
- 不做 RAG / 知识库（第 18/19 步）；
- 不做语义去重（归一化判重在标题层面，语义去重留此前）；
- 不进 `writing` / `published` 状态（属后续笔记步）；
- 不改 harness 基座、不改业务表结构（除已定案的 `0002` 三列）、不改 lifespan。
