# 创作 Agent · xhs-agent

> 在 **LangChain / LangGraph harness** 上实现的**第二个业务图**：把一个**已通过评审**的选题
> → 一篇**笔记草稿**（标题 / 正文 / 标签）+ **违禁词合规自检**。
> 本文件说明：图结构（节点 / 条件边 / 有限回路）、违禁词模块口径、**字段映射**、
> 状态机、配置、审计回填机制、自检证据、成本口径与**真实 LLM 验证结果**。

---

## 一、定位与边界

- **定位**：需要大模型的「成稿」环节（`run_type=content_creation`），由 harness 的
 `run_agent` 统一运行、统一审计；产出的草稿一律 `draft`，**必须人工审核**。
- **本次做的**：LangGraph 业务图 + 违禁词自检模块 + service 编排 + `/api/v1/notes` 接口
 + 离线自检 / 真实 LLM 检查脚本 + 文档。
- **本次不做的**：前端页面 / Next 代理（后续步骤）；封面；笔记的
 `PATCH` / `PUT` / `DELETE`（人工编辑、定稿、发布回填属后续「笔记」步）；
 笔记数据回流与诊断（此前起）；RAG / 知识库统一词表（第 18/19 步）。
- **红线**：人工审核 + 手动发布；不登录小红书 / 不抓登录态 / **不请求任何小红书域名** /
 不自动发布；不要账号密码 Cookie Token；Key 只在本机 `backend/.env`，
 **绝不**入代码 / 日志 / `agent_runs` / `notes` / `.example`。
- **不改 harness 基座**：`runner.py` / `checkpoint.py` / `state/base.py` / `graph/base.py` /
 `llm/factory.py` / `parsing/` / `errors.py` / `tracing/` 一律未改；业务审计缺口由
 service 层用 `run_id` 回填解决（见第七节，机制同此前）。
- **不改表结构**：`notes` / `covers` 由 `0001` 基线建好，本次**零迁移**
 （`alembic current` 仍 `0002 (head)`、`alembic check` 零漂移）。

---

## 二、图：节点 / 条件边 / 有限回路

```
 START
 │
 ▼
 ┌───────────────┐
 │ load_context │ 独立 SessionLocal：读 topic（必须 approved）+ persona
 └───────┬───────┘ + account（topic.account_id 优先，回退 persona.account_id）
 │ 写纯 dict 快照；缺失 / 已归档 / 状态不符 → AgentNonRetryableError
 ▼
 ┌────────────────┐
 ┌─────►│generate_content│ 闭包注入的 model + invoke_json（JSON 容错 + 解析计数）
 │ └───────┬────────┘ node 带 RetryPolicy（只重试瞬时错误）
 │ │ r.raw_text → state.llm_raw；usage / parse_attempts 合并
 │ ▼
 │ ┌───────────────────┐
 │ │validate_compliance│ ① 字段硬伤（标题空 / 超长；正文空 / 过短；标签数量）
 │ └────────┬──────────┘ ② 本地**确定性**违禁词扫描 → content_check / compliance_status
 │ │ （命中**不**单独强制回炉；只写入 feedback 与 content_check）
 │ route_after_validate（条件边）
 │ │
 │ ┌───────────┼──────────────────────────┬─────────────────────┐
 │ │ │ │ │
 │ │ 无字段硬伤 │ 有硬伤且 │ 有硬伤、已到上限 │ 有硬伤、已到上限
 │ │ │ retry_count < 上限 │ 且**有可用正文** │ 且**无可用正文**
 │ ▼ ▼ ▼ ▼
 └───┘ ┌──────────┐ ┌──────────┐ ┌──────────┐
 （带 feedback │ persist │ │ persist │ │ give_up │ 抛不可重试错误
 回 generate）└────┬─────┘ └────┬─────┘ └────┬─────┘
 │ │ │
 ▼ ▼ ▼
 END END 异常 → run_agent 记 failed
```

- **真 LangGraph**：五节点 + 条件边 + 有限回路 + `checkpointer` + `run_agent` 审计，
 **不退化**为单次模型调用或自研编排。
- **回路有界**：`retry_count` 每轮 `validate_compliance` 自增；仅当
 `retry_count < AGENT_JSON_MAX_ATTEMPTS`（默认 3）才回炉，避免「模型持续给不合格草稿」死循环。
 自检实测：模型恰被调用 **3** 次后停止回炉（`group5`）。
- **模型绝不进状态**：状态要序列化进检查点；模型经**闭包**注入
 `build_content_graph(*, model, ...)`。
- **「可用正文」口径** = 标题非空 **且** 正文非空：够落一条*可人工修订*的草稿即可，
 不因为「正文短了 30 字」把模型辛苦写的内容丢掉。
- **单事务落库**：`persist` 一次事务里插入 `notes` **并**把来源选题
 `approved → writing`；任一失败整体回滚，**不留半成品**（自检 `group7` 用外键冲突实测）。

---

## 三、违禁词合规自检模块（`app/agent/content/forbidden_words.py`）

> ⚠️ **免责声明（与代码头部一致）**：内置《基础风险词表》**仅为示例**，用于演示
> 「合规自检 → 命中提示」这条链路，**不是完整清单，也不构成任何法律 / 平台规则意见**。
> 使用者需自行核实并补充所在行业、所在平台的最新规则（本表刻意保守，宁多勿漏）。
> 此前接入 `knowledge_snippets`（RAG 知识库）后，词表将**统一由知识库管理**；
> 本模块届时保留「无知识库时的保守兜底」角色，**接口不变**。

**分类与条数（内置 73 词 / 7 类）**

| 类别 key | 展示名 | 条数 | 示例词 |
| --- | --- | --- | --- |
| `absolute_wording` | 广告法绝对化用语 | 19 | 国家级、最高级、最佳、全国第一、100%有效 |
| `medical_claim` | 医疗 / 功效断言 | 13 | 包治百病、根治、治愈、降血压、增强免疫力 |
| `financial_promise` | 收益 / 保本承诺 | 11 | 稳赚、稳赚不赔、保本、零风险、一夜暴富 |
| `fake_authority` | 虚假权威背书 | 7 | 官方指定、央视推荐、国家认证、权威认证 |
| `superstition` | 迷信 / 玄学功效 | 7 | 转运、招财、辟邪、开光、风水改命 |
| `platform_diversion` | 站外导流 / 联系方式 | 10 | 加微信、加vx、微信号、扫码进群、站外链接 |
| `vulgar_risk` | 低俗 / 高风险导向 | 6 | 博彩、赌场、网贷、刷单、套现、灰色产业 |

**实现口径（刻意简单、可解释、可复现）**

- **大小写不敏感 + 去常见标点 / 空白**（含全角）后做**精确匹配**；不做分词、不做复杂 NLP；
- 去标点顺带覆盖「用符号隔开规避」的写法：`加-微-信` 归一后命中 `加微信`；
- **剪枝**：被**同字段**更长命中词包含的短词不再单列（`稳赚不赔` 命中时不再另报 `稳赚`），
 避免同一句话被重复计数；**跨字段**重复（标题与正文各命中一次）会**各报一次**（位置不同，都有价值）；
- 单次扫描最多返回 `MAX_HITS = 50` 条，防止超长文本产生巨型 `content_check`；
- 命中 ⇒ `ComplianceStatus.warning`；0 命中 ⇒ `ComplianceStatus.passed`；
 **不新增 `blocked` 状态、不丢弃草稿、不静默改写正文** —— 只提示，最终由人工决定。

**安全约定**：词表**只内置在代码里** —— 不写 env、不写日志、不落库到独立表；
`scan_*` 返回项只含「词 / 类别 / 字段 / 片段」，不含任何密钥或隐私信息。

**公开 API**

| 函数 | 作用 |
| --- | --- |
| `normalize_text(text)` | 归一化（转小写 + 去标点 / 空白），仅供匹配使用 |
| `scan_text(text, *, field="body")` | 扫单段文本 → `[{word, category, field, snippet}]`（无命中返空表） |
| `scan_note(title, body, tags)` | 扫整篇笔记，按 title → body → tags 顺序返回 |
| `compliance_of(hits)` | 命中列表 → `passed` / `warning` |
| `build_content_check(hits, …)` | 组装落 `notes.content_check` 的结构 |
| `describe_wordlist` | 词表概况（数量 / 分类），供文档与自检引用 |

---

## 四、字段映射（严格对齐 `notes` 真实列，**零迁移**）

| 产出 / 语义 | 落库列 | 说明 |
| --- | --- | --- |
| 标题 | `title` | `String(100)`；超 `NOTE_TITLE_MAX_LEN` 判字段硬伤；落库前按阈值截断兜底 |
| 正文 | `body` | `Text`；少于 `NOTE_BODY_MIN_LEN` 判字段硬伤 |
| 标签 | `tags` | JSONB 纯词数组：**剥前导 `#`、去空白 / 空项、去重**（保留顺序） |
| 合规命中 | `content_check` | `{status, hits:[{word,category,field,snippet}], checked_fields, checked_at}` |
| 合规结论 | `compliance_status` | `passed`（0 命中）/ `warning`（≥1 命中）；**无 blocked** |
| 笔记状态 | `status` | **恒为 `NoteStatus.DRAFT`**（即使 passed 也不自动 `ready`，必须人工审核） |
| 版本 | `version` | `1`（首次生成；后续人工编辑再升版本） |
| 发布回填 | `publish_url` / `xhs_note_id` / `published_at` | **全部留空**（人工发布后才回填，本次不写） |
| 生成元数据 | `llm_meta` | `{prompt_version, json_parse_attempts, model}` |
| 归属 | `account_id` / `persona_id` / `topic_id` | 由上下文回填：`account_id = topic.account_id or persona.account_id` |
| 模型原始输出 | **不写 notes** → `agent_runs.llm_raw` | 见第七节（属「运行审计」而非「笔记内容」） |

> `notes` 与 `covers` 的关系见 `app/models/note.py` 头注：**没有** `notes.cover_id`，
> 「一个 note 仅一个选中封面」由 `covers.is_selected` 在应用层保证（此前写入）。
> `notes.account_id` 是 `ON DELETE RESTRICT`（有数据的账号只能归档），
> 因此自检 / 检查脚本的清理顺序必须是「先删 notes，再删 accounts」。

---

## 五、`NoteStatus` / `ComplianceStatus` 状态机（本次范围）

```
ComplianceStatus: pending ──► passed （0 命中）
 │
 └──────► warning （≥1 命中；**不阻断落库**）

NoteStatus: draft ← 本次恒为此值
 │
 └──► ready / published / archived （属后续「笔记」步）
```

- 本次**只写** `draft`；`ready` / `published` / `archived` 的流转属后续步骤；
- 本次**不提供** `PATCH` / `PUT` / `DELETE`（不做人工编辑与状态流转接口）；
- `notes.status` 的归档语义由 `CRUDNote.archived_status = NoteStatus.ARCHIVED` 声明备好。

---

## 六、配置项（`backend/.env`，见 `backend/.env.example`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `CONTENT_PROMPT_VERSION` | `v1` | 写入 `notes.llm_meta.prompt_version`，改模板必须升版本 |
| `NOTE_TITLE_MAX_LEN` | `100` | 标题最大长度（与 `notes.title` 的 `String(100)` 对齐） |
| `NOTE_BODY_MIN_LEN` | `200` | 正文最小长度（低于此判字段硬伤，触发回炉） |
| `NOTE_BODY_MAX_LEN` | `1000` | 正文目标上限（写进提示词作为口语化约束，**不作为硬失败**） |
| `NOTE_MIN_TAGS` / `NOTE_MAX_TAGS` | `3` / `10` | 标签个数区间 |

节点超时复用 `AGENT_NODE_TIMEOUT_SECONDS`（注入 `ChatOpenAI(timeout=...)`），不另设。
**风险词表不进 env**（见第三节安全约定）；`LLM_MODEL` 以 **DeepSeek 控制台**当前列表为准，
仓库不写死模型 ID。

---

## 七、审计回填机制（**不改 harness**）

`run_agent` 自动写：`run_type` / `trigger` / `inputs` / `account_id` / `persona_id` /
`llm_model` / `llm_base_url` / token 三列 / `json_parse_attempts` / `node_trace` /
`status` / `latency_ms`，但其 `outputs` **只写 output 的键名**。业务明细由
**service 在 `run_agent` 返回后用 `run_id` 回填**：

```sql
UPDATE agent_runs
 SET outputs = COALESCE(outputs, '{}'::jsonb) || CAST(:patch AS jsonb),
 llm_raw = COALESCE(:raw, llm_raw)
 WHERE id = :run_id;
```

- `patch = {note_id, generated: 1, compliance_status, forbidden_hits, tags_count, title, degraded}`
 （`outputs` 用 `||` **合并保留** harness 已写内容）；
- `llm_raw` = 本次 `r.raw_text`，写入前过 `redact_secrets` 兜底，**断言不含 `sk-`**，
 并按 `LLM_RAW_MAX_LEN = 20000` 截断；
- **`note_id` / `topic_id` 也在此补**：`AgentRunRecorder` **支持**这两个参数，但 `run_agent`
 只转发 `account_id` / `persona_id`（harness 基座本次**禁止改动**），故本层用
 `COALESCE(:note_id, note_id)` / `COALESCE(:topic_id, topic_id)` 在同一套回填 SQL 里补上，
 让审计行能直接回答「这次运行产出了哪篇笔记 / 来自哪个选题」；
- **`outputs.degraded`**：不补的话 `GET /notes/runs` 的 `degraded` 字段**永远是 `null`**
 （schema 有、数据没有）—— 属离线自检暴露的真实缺陷，已修；
- **降级落库**（重试耗尽但有可用正文）时，service 额外把该 run 的 `status` 置 `partial`
 （harness 只按 interrupt 判 success/partial，这里按业务补正）；
- `notes.llm_meta` 另存 `json_parse_attempts`，与 `agent_runs` 互为印证。

---

## 八、HTTP 接口（`/api/v1/notes`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/notes/generate` | 触发创作（同步）；`topic_id` 必填。404（选题不存在）/ 400（状态非 approved、未关联人设、人设已归档、**LLM 未就绪**） |
| GET | `/api/v1/notes/runs` | 最近 `content_creation` 运行记录（**不含** `llm_raw`） |
| GET | `/api/v1/notes` | 列表：分页 + 过滤 `account_id` / `persona_id` / `topic_id` / `status` / 标题 `q`；排序 `created_at`(默认) / `updated_at` + `order` |
| GET | `/api/v1/notes/{note_id}` | 详情（含正文 / `content_check`；**不含** `llm_raw`；`llm_meta` 只回白名单键） |

> ⚠️ **注册顺序**：静态路径 `/generate`、`/runs` 必须排在 UUID 路径 `/{note_id}` **之前**，
> 否则 `/runs` 会被当作 `note_id` 解析而 422。
> `POST /generate` 在 LLM 未就绪时返回 **400** 且**不发起任何对外请求**
> （`AgentConfigError` → `bad_request`）；离线自检通过「关掉 `AGENT_LLM_ENABLED` 后工厂抛错」
> 与路由源码静态断言共同覆盖。
> **任何响应模型都不含 `llm_raw`**：离线自检会扫描整份 OpenAPI 组件的 `properties` 做断言。

---

## 九、自检与实测证据

**离线自检** `scripts/selftest_content_agent.py`（**不连外网、不需 key**，用脚本模型驱动）：

```
backend\.venv\Scripts\python.exe scripts\selftest_content_agent.py
→ 通过 98 项，失败 0 项。全部 PASS ✔
```

覆盖 11 组：图结构（五节点 + 三条条件边 + 有界回路）；合格响应逐列落库与
`approved → writing` 同事务流转 + 审计回填；归属账号回退；业务错误码与 LLM 配置闸门
（404 / 400 / 模型调用 0 次 / 闸门抛错）；违禁词（命中必 `warning`、无命中 `passed`、
剪枝与符号规避、分类齐全）；有限回路（**feedback 确实注入提示词**）；降级落库 → `partial`；
放弃 → `failed` 且**无半成品**；JSON 容错（脏 JSON / 先噪后 valid）；**单事务回滚**（外键冲突）；
接口无 `llm_raw` + `llm_meta` 白名单 + 无 `sk-` + 无小红书域名 + 词表不进 env / 不落库 +
`alembic current` 仍 0002 head 且 `check` 零漂移。
测试数据一律 `[SELFTEST]`，结束时**物理清理** `notes` / `topics` / `agent_runs` / 测试账号人设 /
新检查点线程，复查残留为 0。

**真实 LLM 检查** `scripts/check_content_agent.py`（**2026-10-11 已实跑：20/20 PASS，退出码 0**；
结果见第十一节；脚本可随时安全复跑）：
挑最多 3 条 `approved` 选题各跑一轮，打印标题 / 正文 / 标签 / 命中明细供人工判断，
断言字段完整性、连续多轮 `json_parse_attempts` 是否恒为 1、真实 token；
**运行前后差集清理**并**把选题状态还原为 `approved`**，因此可重复运行。
未配置时**退出码 2 且不发起任何请求**。

**回归（本次实跑，全部不因本次回退）**：`selftest_db` 15 · `verify_schema` 13 · `selftest_api` 37 ·
`selftest_agent_harness` 58（⚠️ 计数随「本机是否配了真实 key」分支变化：未配置走 3 项、
已配置走 2 项，故历史记录的 59 是未配置态）· `selftest_hotspot` 54 · `selftest_topic_agent` 42 ·
`smoke_crud_api` 37（`[SMOKE]` 残留 0）· `smoke_hotspot` 38；
`healthcheck` 后端存活 / 后端就绪（database ✓ pgvector ✓ redis ✓）全绿
（前端两项因本次未启动前端而 unreachable，属预期）。

**新接口在线验证（真实 HTTP，uvicorn + 真实库）**

| 请求 | 结果 |
| --- | --- |
| `GET /api/v1/notes?page=1&page_size=2` | **200** `{"items":[],"total":0,"page":1,"page_size":2}` |
| `GET /api/v1/notes/runs?limit=3` | **200** `[]` |
| `GET /api/v1/notes/{不存在}` | **404** `not_found` |
| `POST /generate`（选题不存在） | **404** `not_found` |
| `POST /generate`（选题 `draft`） | **400** `bad_request`（**未发起任何模型调用**） |
| `POST /generate`（非法 uuid） | **422** `validation`（统一错误体 + 字段级 `details`） |
| 响应体扫描 | `llm_raw` 出现 **0** 次 |

**迁移与结构**：迁移文件仍恰 2 支；`alembic current` = `0002 (head)`；`alembic check` **零漂移**；
`public` 仍 **10 张业务表**。

**🐞 本次实测暴露并修复的三处缺陷（如实记录）**

1. `_backfill_run` 只回填 `outputs` / `llm_raw` / `status`，导致
 `agent_runs.note_id` / `topic_id` 恒为 `NULL`，且 `outputs` 里没有 `degraded`
 ⇒ `GET /notes/runs` 的 `degraded` 字段**永远是 `null`**（schema 有、数据没有）。
 **处置**：在**本次自己的** `service.py::_backfill_run` 内用同一套回填 SQL 补上
 `note_id` / `topic_id` / `outputs.degraded`（**未改 harness 任何文件**）。
2. `scripts/selftest_topic_agent.py` **偶发** 40/42（`rows[0]` 的 tags / target_keywords 断言失败）：
 其 `_fetch_topic_rows` **缺 `ORDER BY`**，却断言 `rows[0]`；PostgreSQL 不保证无序 SELECT 的行序，
 而此前对 `topics` 表的大量增删改变了物理行序，把这枚**潜伏的顺序依赖**暴露出来（复跑即恢复 42/42）。
 **处置**：给该测试助手加 `ORDER BY title`（**测试脚本一行修复**，生产代码零改动），
 并以 `topic → content → topic` 交叉复跑三轮验证稳定。属此前遗留的**测试脚本**缺陷，非生产缺陷。
3. `scripts/selftest_content_agent.py` **自己留残留**：3 条 `[SELFTEST]创作选题10` 成孤儿
 （`persona_id` / `account_id` 均被 FK `SET NULL`），而复查却报「残留为 0」（**假阴性**）。
 根因：夹具索引 10 刻意**不带 `persona_id`**（用于验证「未关联人设 → 400」），
 而 `cleanup` 用 `WHERE persona_id = :pid` 删选题 ⇒ 永远删不到它；复查也只按 `persona_id` 计数 ⇒ 同一盲区。
 **处置**：`cleanup` 改为**按 id 集合**删选题；复查改**双口径**（按 id 精确 + 按标题前缀
 `[SELFTEST]创作%` 全局兜底扫）。已手工清掉 3 条孤儿，连跑两次自检 + 全局扫描均 **0 残留**。

---

## 十、成本口径（**不内置价格**）

- 只记 token：`agent_runs.prompt_tokens` / `completion_tokens` / `total_tokens`；
- 成本 = token × **你的实际单价**（价格随时会变，仓库不硬编码价格表）；
- 最坏情况模型调用次数 = `AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)`（默认 3 × 3 = 9），
 再叠加本图的**回路轮数**上限（`AGENT_JSON_MAX_ATTEMPTS`，默认 3）⇒ 理论最坏 **27** 次；
 正常的单题创作是 **1** 次。

---

## 十一、真实 LLM 验证结果 + 待人工项

> 状态：**2026-10-11 已实跑**（本机 `backend/.env`，`deepseek-flash`）。
> `scripts/check_content_agent.py` → **20/20 PASS，退出码 0**。以下为实测数字，未做美化。

| 验证项 | 结果 | 说明 |
| --- | --- | --- |
| 真实草稿生成 | 3 轮 `status=success`，3 篇落库 | 挑 3 条 `approved` 选题各跑一轮 |
| **真实 JSON 连续稳定性** | **`json_parse_attempts` 连续 3 轮恒为 1** | 模型稳定输出合法 JSON，未触发修复层 |
| **真实 token 消耗** | 合计 `total_tokens = 4547` | 只打印 token；**成本 = token × 你的实际单价**（仓库不内置价格表） |
| 合规自检（真实输出） | 3 篇均 `compliance_status=passed`，命中 0 | 内置词表对真实模型输出**未误报** |
| 落库字段 / 状态流转 | `status=draft` / `version=1` / 发布三列留空 / 同事务 `approved→writing` | 与离线自检断言完全一致 |
| 审计回填 | 行数 = 运行数；`outputs` 明细齐全；`llm_raw` 归档且不含密钥 | 三个 `outputs` 键均非 `null`（含 `degraded=false`） |
| 清理与可重复性 | 差集清理 notes=3 / runs=3 / 检查点 195 行（3 线程）；选题还原 `approved` | **独立复查**：notes 总量 0、runs 残留 0、标记残留 0 |
| **真实草稿质量** | 3 篇样例（625 / 625 / 519 字），标题 / 标签数见下表 | **「是否愿意发」只能人工判断**——最终由你定 |

**3 篇真实草稿**

| # | 选题 | 生成标题 | 标签数 | 标注 |
| --- | --- | --- | --- | --- |
| 1 | 国庆后新店扎堆，我先替你们试了 | 国庆后新店扎堆，我先替你们试了：人均60-80这三家，只有一家我会再去 | 8 | passed |
| 2 | 郑钦文进决赛，看球聚餐别乱选 | 郑钦文进中网决赛，聚餐选店我先帮你踩了 | 7 | passed |
| 3 | 颁奖礼夜宵去哪看？这3家能投屏 | 颁奖礼夜宵去哪看？这3家能投屏，我替你们踩过了 | 6 | passed |

> 阅读印象（基于脚本打印的正文节选，如实不美化）：三篇均为探店口吻、围绕选题角度展开，
> 含具体量化信息（等位时长 / 餐均 / 动线 / 硬指标筛选逻辑），未发现空话凑字数，
> 未命中任何内置风险词。**是否愿意发布请以你本人过目为准**。
>
> 另：复查发现 4 个历史检查点线程（2026-10-10 的 `topic_generation` 运行，审计行仍在）——
> 属「运行前后差集」原则下刻意保留的历史数据，非本次残留。

- ⚠️ **未做跨请求并发控制**（未加全局锁）：本次按设计要求**未引入**并发锁，也**不限制**
 同一选题重复生成——唯一的闸门是「选题必须 `approved`」，而一次成功生成会把它置为
 `writing`，因此天然限制了重复：(第二次调用会返回 400)。如需严格串行，
 可在 service 增加互斥锁（模式同此前热搜服务）。
- ⚠️ **风险词表是示例而非完整清单**（见第三节免责声明）；真实合规结论需人工复核 +
 后续接入知识库统一管理。
- ⚠️ **`NOTE_BODY_MAX_LEN` 不构成硬失败**：正文过长只会在提示词层面约束，
 超出不会触发回炉（避免为了砍字数反复烧 token）。

---

## 十二、本次【不做】

- 不做前端页面 / Next 代理（后续步骤）；
- 不做封面生成；
- 不做笔记的人工编辑 / 定稿 / 发布回填接口（`PATCH` / `PUT` / `DELETE` 全部不提供）；
- 不做笔记数据回流与诊断（此前起）；
- 不做 RAG / 知识库统一词表（第 18/19 步）；
- 不做违禁词的**自动改写**或**阻断落库**（只提示；红线：不静默改写正文、不丢草稿）；
- 不做选题定时创作（仅 `trigger=manual`；`RunTrigger.SCHEDULED` 已备好但本次不用）；
- 不改 harness 基座、不改业务表结构（**零迁移**）、不新增第二个 `lifespan`。
