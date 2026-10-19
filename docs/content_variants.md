# 同选题多版本创作 Agent（V2 增强（二））

> 一句话：在「单篇创作 Agent」之外，新增**第七张 LangGraph 业务图** ——
> 对**同一个已通过选题**一次生成 **N 个角度明显不同**的图文版本（标题 / 正文 / 标签），
> 前端同屏横向对比，人工「采用」其中一个进入创作台编辑；其余版本**软标记为未采用**
> （不物理删除）。不改变「人工审核、手动发布」红线。
>
> 零数据库迁移 · 零新增依赖 · harness 基座零改动 · 接口不返回 `llm_raw` 与向量字段。

---

## 1. 模块边界与文件清单

| 位置 | 职责 |
| --- | --- |
| `backend/app/agent/content_variants/contracts.py` | 契约与常量：`VariantDraft`（**继承**此前 `ContentDraft`，仅加 `angle`）、N∈1~5、三值判定词表 |
| `backend/app/agent/content_variants/state.py` | 图状态（可 JSON 序列化；模型对象绝不入状态） |
| `backend/app/agent/content_variants/prompts.py` | 提示词模板（版本化）+ 内置角度池；渲染工具**复用**此前 |
| `backend/app/agent/content_variants/graph.py` | 第七张业务图（5 节点 + 条件边 + 有限回路） |
| `backend/app/agent/content_variants/service.py` | 编排 + 审计回填 + 批次查询 + 互斥采用 |
| `backend/app/schemas/content_variants.py` | 请求 / 响应 Pydantic 契约（无 `llm_raw`） |
| `backend/app/api/routes/content_variants.py` | 路由层：`/api/v1/content-variants`（仅 GET / POST / PATCH） |
| `backend/app/crud/note.py`（增补） | `create_batch_with_topic_transition` / `list_variant_notes` / `adopt_variant` |
| `scripts/selftest_content_variants.py` | 离线自检（脚本模型；不联网、不花 token） |
| `scripts/check_content_variants.py` | 真实 DeepSeek 检查（**由用户本机执行**，消耗 token） |

**复用而非复制**（硬性约束）：

- 取数：`load_creation_context`（此前抽为模块级函数，content 与 content_variants **同一份**）——
 选题 / 人设 / 账号 / RAG 块（含此前人设知识）读取口径完全一致；
- 违禁扫描：`services/forbidden_source.scan_with_knowledge`（知识库词表优先、内置兜底）——
 **同一实现、同一词表来源**，本图不复写任何扫描逻辑；
- JSON 三层兜底：`app/agent/parsing/json_repair.invoke_json`（围栏 / 控制字符 / 括号扫描）；
- 合规结构：`build_content_check`（`content_check` 与此前同构）；
- 审计回填：复用此前 `content/service._backfill_run`（同一套 run_id 裸 SQL）。

---

## 2. 图拓扑（真 LangGraph：多节点 + 条件边 + 有限回路 + 检查点）

```
START
 │
load_context 读 topic / persona / account + RAG（复用此前取数）
 │
generate_variants 单次调用 → N 版 JSON 数组（invoke_json 三层兜底；
 │ 节点带统一 RetryPolicy：只重试瞬时错误）
review_variants 逐版判定（rejected / blocked / qualified）
 │
 ├── 存在 rejected 且 retry_count < AGENT_JSON_MAX_ATTEMPTS ──► 回 generate_variants（feedback 注入）
 ├── 有可用版本（usable > 0）───► persist_variants ──► END
 └── 全部不合格且到上限 ────────► give_up ──► 抛错（run 记 failed）

persist_variants：单事务插入「合格版本」多条 notes（同 variant_group_id = run_id，
初始 draft、未采用）+ 把来源选题由 approved 置为 writing（只过渡一次）。
```

要点与对齐关系：

- **违禁命中不触发回炉**（与此前「命中风险词不单独强制回炉」一致）；
- **有限回路只在存在 rejected 时进行**，上限 `AGENT_JSON_MAX_ATTEMPTS`（默认 3 轮）；
- 到上限后：有可用版本 → 落库（部分合格）；无可用版本 → `give_up`；
- 每个节点独立 `SessionLocal`；模型**经闭包注入**（`build_kwargs={"model": ...}`），不进状态。

---

## 3. 版本级判定与落库语义（本次与**刻意差异**）

| 判定 | 条件 | 是否落库 | 说明 |
| --- | --- | --- | --- |
| `qualified` | 标题 / 正文非空 **且** 0 违禁命中 | ✅ 落库（draft、未采用） | `content_check` = passed |
| `blocked` | 标题 / 正文非空 **但** ≥1 违禁命中 | ❌ **拦截不落库** | 命中原因 / 明细随响应与 `agent_runs.outputs` 返回 |
| `rejected` | 缺标题 / 缺正文 / 结构非法 | ❌ 不落库 | 原因进 outputs 与响应 |

> ⚠️ 与差异是**验收口径**：单篇创作「命中只提示、草稿保留」（warning），
> 多版本场景「命中即拦截该版本」（blocked），**不阻断其他版本**（partial 语义）。
> 二者共用同一套扫描实现，只是**结论处理**不同。
>
> 版本级不合格只查「标题 / 正文是否为空」（验收口径），**不做**正文最小长度闸门；
> 长度质量由提示词目标（`NOTE_*`）约束并由人工对比把关——避免静默丢弃版本。

**run 状态（`agent_runs.status`，正确落库）**：

| 场景 | run 状态 | 落库数 |
| --- | --- | --- |
| 请求 N 版全部合格 | `success` | N |
| 0 < 落库数 < N（含 blocked / rejected / 模型少返） | `partial` | 部分 |
| 落库数 = 0（全 blocked / 全 rejected / JSON 全失败） | `failed` | 0（不落脏数据） |

**零落库时选题不动**：`approved → writing` 只在「确有版本落库」的同一事务中发生；
全失败不推进选题状态，用户可在修正后重试。

---

## 4. 字段与迁移策略（**零迁移**）

本次**不新增表、不新增列**（`public` schema 仍 10 张业务表；`versions` 仍恰 2 支）。
多版本元数据存 **`notes.llm_meta`（既有 JSONB，建表于 0001）** 的 `variant` 子块：

| 业务字段 | 存储位置（零迁移映射） |
| --- | --- |
| `variant_group_id`（批次号） | `notes.llm_meta.variant.group_id`（= 本次运行的 run_id） |
| `angle`（角度） | `notes.llm_meta.variant.angle` |
| `variant_index`（批次内序号） | `notes.llm_meta.variant.index` |
| `adopted`（是否被采用） | `notes.llm_meta.variant.adopted`（布尔） |

- 与既有键（`prompt_version` / `json_parse_attempts` / `model`）**并列共存**，互不影响；
- 查询口径：`llm_meta ? 'variant'`（批次成员）+ `llm_meta->'variant'->>'group_id'`（批次过滤）；
- 未加新索引（零迁移；数据量级下楼层过滤足够）；未改 `alembic`（`alembic check` 零漂移）；
- **不扩展** `NoteOut` 的 `sanitize_llm_meta` 白名单（既有接口行为零变化）；
 多版本信息由本次专属接口（批次 / 采用）返回。

**迁移三选一的事实依据**（开工前勘察结论，未触发「停下」条件）：

1. `notes` 可承载多版本：`topic_id` 无唯一约束、允许同选题多行；N 条一批**单事务**插入 +
 `approved → writing` **只过渡一次**（新增 `create_batch_with_topic_transition`，与既有
 `create_with_topic_transition` 并存）；
2. 「生成闸门」与此前完全一致：topic 必须 `approved`（`writing` 后不放开再生成——
 沿用既有限制，不扩权）；如需为既有选题再生成，与单篇创作的处理相同（既有限制，非本次引入）；
3. `llm_meta` JSONB 可载 angle / group / adopted ⇒ **无需 0003 迁移**。

---

## 5. 角度池与提示词

- **内置中文角度池**（不写 env）：`实用干货` / `情绪共鸣` / `反差观点` / `亲身经历` /
 `清单盘点` / `避坑提醒`；
- 模式：
 - **自动**（`angles` 留空）：模型从角度池自选 N 个，**必须互不重复**；
 - **显式**（提供 `angles`）：数量必须与 `n` 一致、不得重复、单条 ≤ 20 字（违反即 422）；
 提示词要求「严格使用」并按序对应；
- 提示词版本：`CONTENT_VARIANTS_PROMPT_VERSION`（默认 `v1`），写入 `notes.llm_meta.prompt_version`；
- 采样温度：`0.7`（高于 0.4）——多版本任务要点是角度差异，需要更强的多样性；
 质量由逐版判定 + 人工对比兜底；
- 角度去重为**提示词约束**（返回按原样记录）；真实模型服从度列入「未验证项」。

---

## 6. API 契约（仅 GET / POST / PATCH，**无 DELETE / PUT**）

```
POST /api/v1/content-variants/generate 生成 N 个版本（同步；长耗时，前端 BFF 超时 120s）
GET /api/v1/content-variants?topic_id=... 某选题的版本批次列表（不含 llm_raw）
PATCH /api/v1/content-variants/{note_id}/adopt 采用某版本（单事务互斥；可反复切换）
```

错误口径（与既有接口一致，`{status, code, message, details}`）：

| 场景 | 状态码 | code |
| --- | --- | --- |
| `n` 越界（1~5 之外）/ `angles` 数量不符 / 重复 / 空串 / 超长 / 缺参 | 422 | `validation_error` |
| 选题不存在（生成）/ 笔记不存在（采用）/ 批次查询选题不存在 | 404 | `not_found` |
| 选题非 approved / 未关联人设 / 人设归档 / 无线索账号 / 非批次笔记采用 | 400 | `bad_request` |
| LLM 未就绪（开关关闭 / 缺 key） | 400 | `bad_request`（`details.reason`） |

**采用语义（单事务互斥，封面 `select_one` 同范式）**：目标版本 `adopted=True`，
同批次其余置 `False`；**不改**笔记状态（仍 `draft`）与选题状态——采用只是「选哪版继续编辑」的标记；
未采用版本只做软标记，**任何接口都不提供物理删除**。

---

## 7. 审计回填（`agent_runs`，harness 零改动）

`run_agent(kind="content_variants")`（`RunType.CONTENT_VARIANTS`，枚举加值零 DDL）只写通用键；
业务明细由 service 用 `run_id` 回填：

- `outputs`：`variant_group_id` / `requested` / `generated` / `persisted` / `blocked` /
 `rejected` / `extra_ignored` / `note_ids` / `angles` / `blocked_details` / `rejected_details`；
- `llm_raw`：模型原始输出（经 `redact_secrets` 兜底；**只进 agent_runs，接口一律不返回**）；
- `note_id`：首条落库版本（全量见 `outputs.note_ids`）；`topic_id`：来源选题；
- `status`：按「全成功 / 部分合格 / 全失败」覆盖补记（partial / failed 由 service 写回）。

---

## 8. 前端

- **入口**：选题库（`/topics`）对 `approved` 选题的行操作新增「**多版本生成**」按钮 →
 弹窗选择 `N`（1~5，默认 3）与可选角度（留空 = 自动；勾选时须恰好 N 个）→ 提交；
 结果弹窗展示汇总（请求 / 落库 / 拦截 / 不合格）与被拦截原因 → 「查看版本对比」；
 对 `writing` / `published` 选题的行操作新增「**版本对比**」直达按钮；
- **对比视图**：`/notes/variants/[topicId]`（Server 壳 + Client 组件 + Suspense，同 `/notes/[id]` 范式）：
 同屏并列版本卡片（角度徽标 / 标题 / 正文 / 标签 / 合规状态 / 时间）；
 「采用此版本」→ PATCH → **跳转创作台详情页**继续人工编辑；返回对比页即见
 采用版高亮 + 「已采用」、其余置灰 + 「未采用」（**软标记，不删除**）；
- **BFF**（同源 `/api/v1/**`，127.0.0.1 仅本地）：
 `GET /api/v1/content-variants`（默认 15s）/ `POST .../generate`（**120000ms** 长耗时约定）/
 `PATCH .../[noteId]/adopt`；types 与后端 schema 对齐，**不含任何 token / llm_raw 字段**；
- 不新增导航入口、不新增 npm 依赖；角度池常量在前端镜像一份（注释注明与后端保持同步）。

---

## 9. 成本估算（只记 token，**不内置价格表**）

单次多版本调用 = **一次模型请求**产出 N 版（输入 ≈ 单篇提示词规模；输出 ≈ N × 单版正文规模），
因此：

```
多版本 token ≈ 单篇创作 token × [ 1 + (N − 1) × r ]
```

- `r` = 单篇创作中「输出 token 占比」，约 0.6 ~ 0.8，**与单版正文长度正相关**（正文越长 r 越大）；
- 直观口径：N=3 时约为**单篇创作的 2.2 ~ 2.6 倍**；N=5 时约 3.4 ~ 4.2 倍；
- 实际计费 = token × 用户自己的单价（DeepSeek 控制台为准）；本仓库不内置价格 / 配额表；
 真实 token 由 `scripts/check_content_variants.py` 与 `agent_runs` 如实记录。

---

## 10. 验证矩阵（本机实跑，2026-10-16）

| 项 | 结果 |
| --- | --- |
| `scripts/selftest_content_variants.py`（离线，脚本模型） | **146 PASS / 0 FAIL**，连跑两次一致（EXIT=0） |
| `scripts/check_content_variants.py` 安全退出分支 | 未配置（开关关闭 / 缺 key）→ **退出码 2、零请求**（实测） |
| 浏览器冒烟（playwright + 生产构建） | **14 PASS / 0 FAIL**：闸门提示 → 对比（3 卡 · 角度 · 合规）→ 采用 → 跳创作台（内容载入）→ 返回置灰「未采用」→ 零 console error（截图 `s23b_01~04`） |
| 回归（既有 16 脚本 + 本次） | 见 README「二十八、」证据矩阵（零回退） |
| 零迁移 | `versions` 恰 2 支 / `alembic current`=0002 (head) / `alembic check` 退出 0 / `public` 仍 10 张业务表 |
| harness 基座 | `runner` / `state` / `graph` / `checkpoint` / `llm.factory` / `errors` / `parsing` / `tracing` **文件时间戳逐项核对零改动** |
| 前端 | `tsc` 0 error / `eslint` 0 error / `next build` 成功（`/notes/variants/[topicId]` + 3 条新 BFF 在册） |

---

## 11. 未验证项与已知边界（如实说明）

1. **真实 LLM 三项**（由用户本机执行 `check_content_variants.py` 确认）：
 ① N 版真实差异度 / 质量（相似度仅记录、不设阈值断言）；② JSON 稳定性（期望 `json_parse_attempts` 恒 1）；
 ③ 真实 token 量（与第 9 节公式对照）。
2. **角度去重的真实服从度**：去重目前是提示词约束（返回原样记录）；若真实模型偶发重复角度，
 `check_content_variants.py` 的「角度互不重复」断言会显式失败并打印实际序列（诚实信号，不美化）。
3. **生成闸门沿用既有口径**：topic 必须 `approved`（`writing` 后不放开再生成，与此前一致）；
 本次未扩权。
4. **浏览器返回的缓存细节**：采用后自动跳转创作台；浏览器「后退」若命中路由缓存可能短暂显示
 旧状态，重新进入对比页（选题库「版本对比」/ 刷新）即为服务端最新状态。
5. 真实 LLM 的**成本与时长**随模型与正文长度波动（公式为估算口径）。
