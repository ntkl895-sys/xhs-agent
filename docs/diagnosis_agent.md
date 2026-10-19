# 单篇诊断 · xhs-agent

> 范围：**单篇笔记 → 一次诊断结论**（只读 + 建议）—— 选一篇已发布笔记，由第四个业务图
> （`app/agent/diagnosis/`，真 LangGraph 五节点）产出：数据事实（**由程序确定性计算**）/ 综合评分 /
> 亮点 / 问题 / 原因 / 建议 / 基准与数据缺口，落库一条 `diagnostics`（零迁移，`0001` 已建）。
> **不编造外部基准**（无跨篇样本时 `external_benchmark` 恒 `null`）；**数据不足必须声明**、
> 严重不足**不硬打分**（`score=null`）。本次**零数据库迁移、零新增依赖、harness 基座零改动**。
> 本文件给「改了什么 / 口径与红线 / 怎么验证 / 边界在哪」；页面数据流另见 `docs/frontend_pages.md`（第 5e 节）。

---

## 一、定位与边界

- **它是什么**：`/diagnostics` 单篇诊断页 —— ① 选笔记（只列已发布）→ ② 选诊断类型（自动 / 冷启动 /
 复盘 / 异常）→ ③ 「生成诊断」（同步，BFF 超时 120s）→ ④ 结果分块展示 + 底部诊断历史（只增不改）。
- **它不是什么**：不做趋势图；不做跨篇爆款 / 行业均值基准（此前 RAG）；
 不抓取小红书创作后台数据；不自动发布。
- **红线（全程不变）**：诊断**只读**本地库（不改 `notes` / `topics` / `note_metrics`，不触发发布）；
 **绝不编造外部基准数字**（无数据源就如实声明）；数据不足必须逐项写入 `data_gaps`；
 不登录、不保存账号 / 密码 / 登录态凭据、**不向任何小红书域名发起网络请求**。
- **唯一模型通道**：经既有 harness `run_agent`（LangChain / LangGraph 基座）+ 真 LangGraph 图，
 禁止裸 HTTP 自编排；LLM 统一 DeepSeek（OpenAI 兼容）。

## 二、确定性计算口径（`contracts.py`，纯函数、可单测、不依赖网络与 LLM）

**① 取数**：快照按 `captured_at` 升序；`latest` = 每个指标**最后一个非空值**（后条为空不覆盖前值）；
`latest_snapshot_id` = 最新快照 id（可空）→ 落库 `based_on_metric_id`。

**② 漏斗比率（六率，统一 4 位小数）**：**每条率独立配对快照** —— 取「同时含 `views` 与该分子、
最靠后的一条快照」计算；`views` 为 `None`/`0` 或分子缺失 → `None`（绝不臆断）：

| 率 | 公式 |
| --- | --- |
| `like_rate` | `likes / views` |
| `collect_rate` | `collects / views` |
| `comment_rate` | `comments / views` |
| `share_rate` | `shares / views` |
| `follow_rate` | `new_followers / views` |
| `engagement_rate` | `(likes + collects + comments + shares) / views` |

**③ 环比与趋势**：快照 ≥ 2 时给相邻增量 `segments`、首期→最新总增量 `total_deltas` 与增长率
`growth_rates`（首期 `0`/`None` → `None`，4 位小数）、覆盖时长 `coverage_hours`（`published_at` →
最新 `captured_at`；无 `published_at` 用最早 `captured_at`；整数小时）。

**④ 类型判定 `decide_diagnosis_type`**：用户显式指定 → `source="manual"`；否则自动 ——
无快照 / 最新 `views` 缺失 → `cold_start`；相邻 `views` 或 `likes` 负增长且降幅
≥ `DIAG_ANOMALY_DROP_RATIO`（默认 `0.30`）→ `anomaly`；其余 → `review`；均给 `reason`。

**⑤ 数据缺口 `data_gaps`**：程序侧收集（无快照 / 未选封面 / 正文缺失 / 缺曝光类字段说明等）+
模型侧声明合并（`DATA_GAPS_LIMIT=12`、完全相同去重、程序侧在前）。

**⑥ 归一校验 `normalize_diagnosis_payload`**：`score` 须在 `[DIAG_SCORE_MIN, DIAG_SCORE_MAX]` 或
`null`；四个字符串数组条数 ≤ `DIAG_MAX_LIST_ITEMS`（8）、单条 ≤ `DIAG_TEXT_MAX_LEN`（300 字）；
`suggestions` 非空；**冷启动强制 `score=None`**（模型给了也不采纳）。违规 → 判不合规回炉；
超长 / 超条数 → 截断并记 `warnings`。

**⑦ 专属异常**：`DiagnosisContextError`（上下文错误，如笔记不存在）/ `DiagnosisGiveUpError`
（结构校验连续超限），均继承 `AgentNonRetryableError`（不消耗节点重试额度），由 service 翻译成统一错误体——
**响应绝不含原始堆栈**。

## 三、诊断 Agent 图（`app/agent/diagnosis/`，第四个业务图）

**文件**：`__init__.py`（出口汇总）/ `state.py`（`DiagnosisState`）/ `contracts.py`（纯函数 + 常量 + 异常）/
`prompts.py`（版本化提示词 `DIAG_PROMPT_VERSION=v1`）/ `graph.py`（五节点构图）/ `service.py`（编排 + 审计回填）。

```
START → load_context → generate_diagnosis → validate_diagnosis ─┬─ 合规 ──────────────▶ persist → END
 ├─ 不合规且未超限 ─────▶ generate_diagnosis（有限回路）
 └─ 超限（≥3 轮）──────▶ give_up（结构化错误，run 记 failed）
```

- **真 LangGraph**：`new_state_graph` + `add_conditional_edges` + 有限回路（上界 `AGENT_JSON_MAX_ATTEMPTS`=3）
 + `compile_graph` 绑定检查点（经既有 harness）；**模型经闭包注入、绝不入状态**；
- `load_context`：校验笔记存在（不存在 → `DiagnosisContextError` → service 译 404，**不产生 run**）；
 载入笔记内容 / 人设 / 快照时间线（ORM → 普通 dict 入状态）/ 是否已选封面；
 调纯函数算 latest / rates / trends / 覆盖时长 / 类型判定 / `data_gaps`；**无数据不是错误**（仅声明缺口）；
- `generate_diagnosis`：系统人设 = 只基于已提供事实的资深运营顾问（禁引外部数字 / 数据不足声明 /
 严格 JSON / 建议可执行 / 不评价敏感话题）；`invoke_json` 三层兜底；`AgentJSONError` → `parse_failed`
 回炉（下一轮提示词带结构反馈）；
- `validate_diagnosis`：纯函数归一 + 结构校验；不合规且 `retry_count < 3` 回炉，超限 → `give_up`；
- `persist`：**单事务**写一条 `diagnostics`（含第四部分全部字段）；失败回滚不留半截；
- `give_up`：抛 `DiagnosisGiveUpError`（可操作中文指引），run 记 `failed`；
- 回路有界：最坏模型调用次数 = `AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)` = **3×3 = 9**；
- **「数据不足型冷启动」**：无快照时 `score=null`、`diagnosis_type=cold_start`、`external_benchmark=null`、
 `data_gaps` 含「无运营数据快照」；`suggestions` 以「尽快回填数据 + 内容侧
 （标题 / 封面 / 标签 / 正文开头）可检查项」为主 —— 明确是**内容侧初步检查**，不是数据诊断。

## 四、`diagnostics` 字段落库映射（零迁移逐列对齐）

| 列 | 来源 / 口径 |
| --- | --- |
| `note_id` | 必填；不存在 → 404（前置校验先于跑图，**不建 run**） |
| `based_on_metric_id` | `latest_snapshot_id`（无快照 → `null`） |
| `diagnosis_type` | `cold_start` / `review` / `anomaly`（枚举 `DiagnosisType`，`native_enum=False`） |
| `score` | `0~100` 或 `null`（数据严重不足不硬打分；冷启动恒 `null`） |
| `strengths` / `weaknesses` / `reasons` / `suggestions` | JSONB 字符串数组；简洁中文；条数 ≤8、单条 ≤300 字；`reasons` 指向具体依据；`suggestions` 可执行、禁空泛口号 |
| `benchmark` | JSONB 固定七键：`basis="self_only"` / `latest_metrics`（六指标）/ `rates`（六率）/ `trends`（segments / total_deltas / growth_rates / coverage_hours）/ `thresholds={"anomaly_drop_ratio": 0.30}` / `external_benchmark=null` / `benchmark_note`（原文：*本期无同类/历史爆款基准，仅基于该笔记自身数据；跨篇基准见此前 RAG*） |
| `llm_model` | 实际模型名（不含 key） |
| `llm_meta` | JSONB **白名单恰五项**：`prompt_version` / `json_parse_attempts` / `diagnosis_type` / `diagnosis_type_source`（`auto`/`manual`）/ `data_gaps` |

`agent_runs` 侧（**不改 harness**，沿用第 10 / 12 / 15 步机制）：service 用 `run_id` 裸 SQL 回填
`outputs = {diagnostic_id, diagnosis_type, score, persisted:true}`、`llm_raw`（先 `redact_secrets` 再截断
20000）、`note_id`；接口与页面**一律不返回 `llm_raw`**。

## 五、接口面（`/api/v1/diagnostics`，第 9 只 router；**无 PUT / PATCH / DELETE**）

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/diagnostics/generate` | 跑诊断并落库（**201**）；请求体 `{note_id, diagnosis_type?}`；400（未配置 / 超限）/ 404（笔记不存在）/ 422（非法类型）。**静态路径 `/generate` 先于动态段注册** |
| `GET /api/v1/diagnostics?note_id=` | 诊断历史（`created_at` 倒序 + id 兜底稳定排序）；缺参 **400**、笔记不存在 **404** |
| `GET /api/v1/diagnostics/{diagnostic_id}` | 单条详情；不存在 → **404** |

`RunType.DIAGNOSIS="diagnosis"`（`native_enum=False` 落 VARCHAR，**无 DDL**）。分层纪律：
routes → schemas → crud → models，路由层零 SQLAlchemy 表达式（`selftest_api` 静态断言）；
`crud/diagnostic.py` 只有 `create_diagnostic` / `list_by_note` / `get_by_id`（不做 update / delete）。

## 六、前端：`/diagnostics` 单篇诊断页

**页面结构（自上而下）**：① 笔记选择器（`GET /api/v1/notes?status=published`，**只列已发布**；
`?note_id=` 预选，不在列表的当前笔记如实标注）+ 诊断类型选择（默认「自动判定（推荐）」）+「生成诊断」
（生成期间禁用并提示「诊断中……（最长约 2 分钟）」）；② 结果区：**数据事实卡**（最新六指标 /
六率百分比（`null` 显示「—」）/ 环比增量与增长率 / 覆盖时长 / `based_on` 说明，标注「由程序计算，
非模型生成」）/ **综合评分**（`null` → 「数据不足，未评分」）/ 亮点 / 问题 / 原因分析 / 建议
（每条可「复制」，`navigator.clipboard` + textarea 兜底）/ **基准与数据缺口横幅**（amber 提示：
「仅基于该笔记自身数据、无行业 / 爆款基准（跨篇基准此前）」+ `data_gaps` 列表 + 生成时间 /
模型 / `prompt_version` / `parse_attempts`）；③ 诊断历史表（时间 / 类型 / 评分 / 判定来源 / 「查看」→
`GET /{id}` 回填结果区）。

**BFF（3 个薄代理，不写 `export const dynamic`）**：`/api/v1/diagnostics`（GET search 透传）/
`/api/v1/diagnostics/generate`（POST `request.text` 原样透传，**`timeoutMs=120000`**）/
`/api/v1/diagnostics/[id]`（GET 透传；动态段 `Context = { params: Promise<{ id: string }> }`）。

**入口**：站点导航第 **8** 入口「单篇诊断」；`/notes` 列表已发布行「诊断」；创作台（published 时）
「单篇诊断 →」——均跳 `/diagnostics?note_id=<id>`（**未新增任何平台域名地址**；全前端平台域名仍恰
此前发布外链 1 处）。

## 七、隐私与红线（工程落实）

| 要求 | 落实 | 复核 |
| --- | --- | --- |
| 诊断只读 | 不写 `notes` / `topics` / `note_metrics`、不触发发布 | 离线自检（诊断前后三表行数 / 字段不变）|
| 不编造外部基准 | `basis=self_only`、`external_benchmark=null`、`benchmark_note` 显式声明；四数组不得出现「行业平均 / 爆款门槛」等话术 | 离线自检 + 真实连跑 3 轮断言 |
| 数据不足必须声明 | `data_gaps` 逐项写明（含程序侧 + 模型侧）；严重不足 `score=null` | 冷启动组断言（模型给分也不采纳）|
| 接口不泄敏感物 | 响应无 `llm_raw` / key / base64 / 绝对路径；`llm_meta` 白名单恰五项 | 接口面断言 + 合规扫描 |
| 审计可追溯且脱敏 | `agent_runs` 记 `llm_model` / token / `outputs` / `llm_raw`（`redact_secrets` + 截断）| 真实连跑断言（无密钥前缀）|
| 不请求小红书域名 / 不存登录态 | 全链路只访问本地后端与所选 LLM 端点 | 合规扫描（本次触碰文件 0 命中）|

## 八、自检与证据（本机实跑，2026-10-12）

| 项 | 结果 |
| --- | --- |
| `scripts/selftest_diagnostics.py`（离线，进程内 TestClient + 真实库；9 组） | **156/156 PASS，退出码 0** |
| ↳ 覆盖要点 | 图结构（五节点 / 条件边三出口 / 有界回路）/ 纯函数（latest / 六率含同快照配对与防错配 / 趋势 / 类型判定 / 归一 / 缺口合并）/ 成功路径全字段 / 冷启动 / 失败路径（9 次调用上界、400 不留假数据、404 不建 run）/ 接口面 / 零迁移 / 合规扫描 / 双口径清理残留 0 |
| `scripts/check_diagnosis_agent.py`（**真实 LLM**）——未配置分支 | **退出码 2、零请求**（打印配置指引）；环境变量关闭开关复测同断言 |
| `scripts/check_diagnosis_agent.py`（**真实 LLM**）——连跑 3 轮 | **20/20 PASS，退出码 0**：3 轮类型 `review(auto)`、评分 `72.0 / 70.0 / 74.0`；**`parse_attempts` 恒 1**（JSON 连续稳定，未走回炉）；`external_benchmark` 恒 `null`；无外部数字话术；审计 run `success`、`note_id` 已回填、`llm_raw` 脱敏；token 合计 **17,231**（1492+6128 / 1492+3726 / 1492+2901） |
| ↳ 跑后清理 | 按差集物理清理：`agent_runs` 3 行 / 检查点 249 行（3 线程）/ `diagnostics` 3 行（默认不保留，可重复跑） |
| ↳ 诊断质量（人工过目） | 数据引用**全部来自程序提供的事实**（六指标 / 六率 / 增量逐项对上）；未杜撰任何外部数字；建议可执行（补封面、低门槛投票式评论引导、正文收藏钩子、标签细化）；并**如实指出人设（探店）与本篇（穿搭）的割裂**——系从提供的人设快照得出的合理推论 |
| **浏览器轻量冒烟**（playwright-core + 本机 Chrome，生产 `next start`） | **13/13 PASS，零 LLM 调用**：h1 / 导航第 8 入口高亮 / 选择器 URL 预选 / 类型默认值 / 按钮可用 / 历史区空态 / 无 `pageerror`、无 `console.error` |
| ↳ 截图留档 | `docs/screenshots/s16_01_diagnostics_page.png` |
| BFF 链路（经 `localhost:3000`） | 列表 GET 200（透传真实数据）；生成 POST 用不存在笔记 → **404 `笔记不存在，无法诊断`**（链路语义正确；零 token） |
| `npx tsc --noEmit` / `npm run lint` / `npm run build` | **0 error** / **0 error** / **成功**（`/diagnostics` ○ 静态 + 3 条 BFF 路由 ƒ） |
| 回归矩阵（10 个既有脚本） | **全部 PASS 零回退**：db 15 · schema 13 · api 37 · harness 60 · hotspot 54 · topic 42 · content 98 · cover 79 · studio 86 · metrics 139 |
| `healthcheck.py`（前后端同时在线） | **四项全绿** |
| 迁移 | `alembic current` = `0002 (head)`；`check` 退出码 0（零漂移）；`versions` 恰 2 支；`public` 仍 10 张业务表 |
| 🔧 本次修正（如实记录，非产品缺陷） | `service.py` 注释中「sk-」字样触发合规扫描误报 → 改为中文化措辞（先例：此前同类处理） |

## 九、未验证项（如实说明，未做美化）

1. **浏览器「生成诊断」完整点击链路**未在本次实测（避免超出已确认的 token 预算）：静态契约核对
 （auto 时 `diagnosis_type:null` 与后端 `Literal|None` 匹配）+ BFF 404 实链 + 后端 3 轮真实连跑已覆盖
 各段；如需补 1 次点击验证（再消耗 1 次调用 token）可随时补测。
2. **样本面窄**：真实连跑仅 1 条演示笔记 × 3 轮（自动判定均为 `review`）；`anomaly` / `cold_start`
 的**真实 LLM** 路径未逐一实测（离线自检已用假模型覆盖结构与口径）。
3. **更换 `LLM_MODEL` 后**的诊断质量未实测（本机为 `deepseek-flash`）。
4. **模型偶做内部比值运算**：如「收藏 / 点赞比 ≈ 59.6%（203/341）」——数字由程序提供的两个事实相除可得，
 未引入外部数字；如需完全禁止派生运算可在提示词再收紧（本次未做）。
5. **已知小瑕疵**：`data_gaps` 合并按**完全相同**去重，模型侧与程序侧近似重复（多一个句号）时会并存，
 观感偶有重复项；不影响事实正确性（后续可做模糊去重）。
6. **大量快照的成本面**未实测：当前 3 条快照 `prompt ≈ 1492` token；快照数量增长对提示词体积与
 费用的影响未做外推。

## 十、成本（只记 token，不内置价格表）

- 每次诊断一条 `agent_runs`，记录 `prompt_tokens / completion_tokens / total_tokens`（**不记金额**）；
- 实测参考（3 条快照演示笔记、`temperature≈0.2`）：3 轮合计 **17,231** token
 （`prompt 1492` 恒定；`completion 2901~6128` 随模型自选输出长度波动）；
- **成本 = token × 你在 DeepSeek 控制台看到的实际单价**；本仓库不内置任何价格表、不做金额换算。

## 十一、本次【不做】

- 不做趋势图/ 跨篇爆款与行业基准（此前 RAG）/ 自动重诊断调度 / 批量诊断；
- 不做诊断的 `PUT / PATCH / DELETE`（历史只增不改，随笔记 CASCADE）；
- 不改 `notes` / `topics` / `note_metrics`（诊断只读 + 建议）；不触发发布；
- 不做抓取创作后台数据 / 登录 / 保存登录态凭据 / 自动发布（**永不做**）；
- **零数据库迁移**（`diagnostics` 由 `0001` 建好；`RunType.DIAGNOSIS` 无 DDL）；
- **后端 requirements 零新增 / 前端 npm 零新增**；
- **harness 基座零改动**（`runner` / `state/base` / `graph/base` / `parsing` / `errors` /
 `checkpoint` / `tracing` / `llm/factory` 全未动）；
- 不做公网部署（数据只在本机与所选 LLM 端点之间流转）。
