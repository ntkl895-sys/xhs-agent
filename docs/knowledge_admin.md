# 知识库人工维护（违禁词入库 + 手动块 CRUD + 人设知识 + 文档导入）· xhs-agent

> 目标：把此前「只读浏览 + 提炼」的知识库升级为**可人工维护** ——
> ① 违禁词入库统一管理（创作图扫描词表切换为「数据库优先」，任何异常**保守回退内置词表**）；
> ② 手动知识块新建 / 编辑（`POST /manual` + `PATCH` 扩展，正文变化触发重向量化）；
> ③ 人设知识库（`RefType.PERSONA`，选题 / 创作反哺时按当前人设纳入）；
> ④ 文档导入（粘贴文本或上传 `.txt` / `.md`，切分 → 逐块向量化 → 单事务入库）。
> **零数据库迁移**（`ChunkType` / `knowledge_snippets` 均在 0001 基线已建；`RefType` 只加枚举成员，
> `native_enum=False` 落 VARCHAR 无 DDL）；**harness 基座零改动**（`runner/state/graph/checkpoint/
> errors/parsing/tracing/llm.factory` 一行未动）；**前后端零新增第三方依赖**。
> 本文给「定位 / 词表切换 / 手动块 / 人设知识 / 文档导入 / 接口 / 前端 / 红线 / 证据 / 未验证项」。

---

## 一、定位与边界

- **知识块生命周期不变**：只增 / 编辑 / 归档（`active` ↔ `archived`），**无物理删除**；
 本次新增的四个端点与 `PATCH` 均遵守该口径（自检对 OpenAPI 方法面有精确断言：无 PUT / DELETE）。
- **违禁词双轨制**：知识库词表为**主**（可人工增删归档）、内置 73 词为**兜底** ——
 读取异常 / 词表为空 / 扫描出错三种情况都**回退内置并记 WARNING**，
 **任何情况不得因知识库问题跳过检查**（保守兜底，自检三路径逐一实测）。
- **离线可维护**：`forbidden_word` 类型**不向量化**（精确匹配无需向量），未安装向量化依赖时
 违禁词的入库 / 管理 / 创作图扫描全部正常；而文档导入 / 人设知识 / 其它类型手动块
 需要向量化就绪（未就绪 → 400 + 安装指引，**零写入**）。
- **零迁移 / 零依赖**（见页首）；`langchain-text-splitters` 属此前环境实测缺口，
 由 `doc_splitter` 自适应解决（见第五节，**如实说明**未使用官方包）。

## 二、违禁词入库与创作图词表切换（最高优先级）

**新建**：

| 文件 | 职责 |
| --- | --- |
| `scripts/seed_forbidden_words.py` | 一次性种子脚本：读内置词表 → `seed_builtin_words`；`--dry-run` 只预检；入口先摘代理环境变量；退出码 0/1 |
| `app/services/forbidden_source.py` | 词表服务：`word_hash`（归一化词 sha256）/ `load_active_wordlist(db)`（**按类型全量读取**，非向量检索；上限 `MAX_FORBIDDEN_WORDS=2000`）/ `scan_with_knowledge(title, body, tags)`（**三路回退内置**）/ `seed_builtin_words(db, *, dry_run)`（幂等：按 `(chunk_type, meta.content_hash)` 查重跳过计 reused） |

**修改（最小增强，不复制第二份扫描实现）**：

- `app/agent/content/forbidden_words.py`：
 - 新增 `build_word_index(items) -> dict[归一化词, 类别]`（首次出现优先、空词跳过）；
 - `scan_text` / `scan_note` 增加**可选** `wordlist` 参数（缺省 `None` = 内置索引，
 此前行为**逐字一致**）；`_scan_one_field` 增 `index` 参数复用同一扫描逻辑；
 - `_NORMALIZED_INDEX` 惰性构建改为复用 `build_word_index`。
- `app/agent/content/graph.py`：`validate_compliance_node` 的扫描调用由 `scan_note(...)`
 改为 **`scan_with_knowledge(...)`**（词表来源切换）；**节点拓扑 / 条件边 / 状态机 /
 字段硬伤校验 / `content_check` 结构 / 落库逻辑全部未动**。

**循环导入坑（已固化）**：`app/agent/content/__init__.py` 聚合 `graph`，而 `graph` 又 import
`forbidden_source` —— 若 `forbidden_source` 在**导入期** `from app.agent.content.forbidden_words
import ...`，当它以导入链起点进入时会形成环（`ImportError: partially initialized module`）。
**解法 = 调用时延迟导入**（`_fw` 辅助函数，与 `crud/knowledge` 延迟导入 embedding 同思路）。

## 三、手动知识块 CRUD（`app/services/knowledge_admin.py`，新建）

**`create_manual_snippet`（`POST /manual` 唯一入口）**：

- 六类型白名单（`ChunkType`）；正文必填、≤20000 字；标题 ≤200 字；类别 ≤64 字；
- `forbidden_word`：`meta = {"category": 类别（缺省 manual）, "content_hash": word_hash(词)}`，
 **不向量化**（embedding 留空，离线可维护）；
- 其它类型：`meta = {"content_hash": sha256(正文), "origin": "manual"}`，
 先做**零副作用闸门**（`describe_embedding_blocker`），再 `embed_texts`，
 **失败 → 400 且不落库**（不静默丢向量）；
- `ref_type=persona`：`ref_id` 必填且**人设必须存在**（否则 400）；
 **`persona_knowledge` 强制要求关联人设**（未关联的块不会被任何反哺检索纳入 → 400 明说）。

**`update_snippet_admin`（`PATCH` 扩展）**：

- 仅传 `status`：走 `crud_knowledge.set_status` —— 与此前 `active/archived` 切换**逐字一致**
 （分层纪律：`update_fields` 白名单不含 `status`；⚠️ 自检曾抓到「status 混入 update_fields 被
 白名单静默丢弃」的真实缺陷，已改回 `set_status` 并回归通过）；
- `title` / `content`：显式传入才更新（空串标题 = 清空）；`content` 变化时重算
 `meta.content_hash` 并**重新向量化**；重向量化失败 → `embedding` 置空 + 响应 `warnings`
 非阻断提示，**正文照常保存**（不阻断、不丢正文；`forbidden_word` 只重算 hash 不动向量）。
- 异常原因经 `_safe_reason` 脱敏（`redact_secrets` + 去 Windows/POSIX 绝对路径 + 截断 300）。

**CRUD 增补（`crud/knowledge.py`）**：`update_fields(db, id, values)`（白名单
`{title, content, embedding, meta}`，`None` 也写入以支持「置空向量」）、
`list_active_snippets(db, *, chunk_type, ref_id=None, limit=200)`。
⚠️ `ChunkType` 是 `str, Enum` 混合类，`str(成员)` 返回 `'ChunkType.FORBIDDEN_WORD'` ——
归一必须用 `chunk_type.value if isinstance(...) else str(...)`（自检前实测暴露）。

## 四、人设知识库（`RefType.PERSONA` + RAG 纳入）

- `app/models/enums.py`：`RefType` 增 `PERSONA = "persona"`（`native_enum=False`，**零 DDL**）；
- `app/services/rag.py` 最小可降级扩展：
 - `_load_persona_snippets(db, *, persona_id)`：取该人设 **active** 的 `persona_knowledge`
 （`ref_type=persona + ref_id` 过滤；`_PERSONA_MAX_ITEMS=5` 上限）；
 - `safe_rag_block(..., persona_id=None)`：先人设知识 → 再向量检索 → `_dedupe_snippet_items`
 （按 id 去重、顺序保留）→ 统一渲染（人设知识条目带「（人设知识）」标注）；
 **任何异常只 WARNING、按「无人设知识」继续**；
 - **无 persona / 失败时提示词与本次前一致**（`if not text and not persona_id: return ""`）；
- 两图接入（各恰 1 处，均在 `load_context` 的 `safe_rag_block` 调用处）：
 - `app/agent/content/graph.py`、`app/agent/topics/graph.py` 增传 `persona_id=str(persona.id)`；
 - **两图节点拓扑与状态机零变化**（回归 `selftest_content_agent 98 / selftest_topic_agent 42`
 全绿并有替换等价断言背书）。

## 五、文档导入（`app/services/doc_splitter.py` + `import_document`）

**切分器（如实说明的工程决策）**：设计要求「用已安装的 langchain 文本切分器」，
但实测本机 `langchain 1.4.4` **不内置** `langchain-text-splitters`（1.x 起为独立分发包），
且设计要求同时要求**零新增依赖** —— 故 `doc_splitter` 采用**自适应**口径：

- 官方 `RecursiveCharacterTextSplitter` 若已安装 → 优先使用（`keep_separator=True`）；
- 缺失 → **内置零依赖等效实现**（同分隔符优先级 `\n\n → \n → 。→ …→ 硬切`、
 同 `chunk_size` / `chunk_overlap` 语义；组装时 `keep = min(overlap, max(0, size - len(atom)))`
 保证 `tail + atom` 绝不超限）；
- 两条路径对上层同接口（`split_text`），`describe_splitter["impl"]` 如实标注实际实现；
- 参数边界：`chunk_size 200~4000`、`chunk_overlap 0~500 且 < chunk_size`（非法 → 400 / 422）。

**`import_document`（`/import-text` 与 `/import-file` 同流程）**：

1. 校验（文本 ≤200000 字 / 类型 ∈ {general, persona_knowledge} 且后者必须带存在的人设）；
2. `split_text` 切分（默认 800 / 100，可带参）；
3. **就绪闸门在切分之后、写库之前**（未就绪 → 400 零写入）；
4. 逐块：**查重先于向量化**（`(chunk_type, content_hash)` 命中 active 即跳过，批内重复同计
 `skipped`）→ `embed_texts` 单块失败计 `failed`（**如实计数、不影响其它块**）；
5. 成功块 `insert_many` **单事务**批量入库（失败整体回滚）；
6. 结果如实返回 `{total_chunks, inserted, skipped, failed, snippet_ids, warnings,
 chunk_size, chunk_overlap, splitter}`。

**`decode_upload`（文件上传专属校验）**：仅 `.txt` / `.md`（其它扩展名 400 明确拒绝，
**不做 .docx / .pdf 解析、不做 URL 抓取**）；≤1MB；`utf-8-sig` 解码（其它编码 400 提示另存）。

## 六、接口面（第 12 只 router 增 4 端点，静态路径先注册）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/knowledge/manual` | 手动创建（**201**；forbidden_word 不向量化；其它类型失败 400 零写入；参数非法 422） |
| `POST` | `/api/v1/knowledge/seed-forbidden-words` | 幂等同步内置词表（`?dry_run=`；`{total, inserted, reused, would_insert, dry_run}`） |
| `POST` | `/api/v1/knowledge/import-text` | 文档导入·文本（**201**；结果如实计数） |
| `POST` | `/api/v1/knowledge/import-file` | 文档导入·上传（multipart；字段 `file` + `chunk_type` / `ref_id` / `chunk_size` / `chunk_overlap`） |
| `PATCH` | `/api/v1/knowledge/{id}` | **扩展**：`status` / `title` / `content`（响应 `SnippetWriteOut` = 知识块 + `warnings`） |

- 注册顺序（自检断言）：`extract → search → manual → seed-forbidden-words → import-text →
 import-file → ""(GET) → /{id}(GET/PATCH)`；
- 新增 schema 5 个：`SnippetManualCreateRequest` / `SnippetWriteOut` / `SeedForbiddenWordsOut` /
 `ImportTextRequest` / `ImportResultOut`（`extra="ignore"`；**`SnippetOut` 字段集未动**，
 此前断言兼容）；
- 响应**绝不含** embedding 原始向量 / `llm_raw` / key / 绝对路径（自检 12 个模型逐一扫）；
- 路由层**零 SQLAlchemy 表达式**（此前分层纪律）。

## 七、前端：`/knowledge` 管理扩展（BFF 4 条）

- 新增 BFF：`manual`（POST，120s）/ `seed-forbidden-words`（POST `dry_run` 透传，60s）/
 `import-text`（POST 120s）/ `import-file`（POST multipart 120s，**不手设 Content-Type**）；
 全部 `proxyToBackend` 薄转发、**不写 `export const dynamic`**；
- 交互组件集中在 `app/knowledge/knowledge-admin.tsx`：新建 Dialog / 编辑 Dialog /
 违禁词管理条 / 文档导入 Tab（详见 `docs/frontend_pages.md` 5h）；
- 两个 Dialog 由父级 `key` 每次打开重新挂载（同 `TopicGenerateDialog` 范式）；
- 本次顺手修复：知识块 / 检索两个筛选下拉**未传 `items` 导致 trigger 显示原始枚举值
 （`all`）** 的显示缺陷（改为中文标签，与项目既有多处 Select 范式对齐）；清理 3 处
 可见文案中未渲染的 Markdown `**`。

## 八、红线与隐私（工程落实）

- **不请求任何平台域名**：后端零平台域名；全前端域名仍**恰 1 处**（此前发布外链）；
 不登录、不抓登录态、不自动发布；
- **知识块只增 / 编辑 / 归档**：无物理删除接口（自检扫 PUT / DELETE 零命中）；
- **向量与 llm_raw**：本次全部新接口响应无向量、无 `llm_raw`；embedding 仅在内存与列中流转；
- **路径与密钥**：`_safe_reason` 过滤异常文本中的绝对路径与密钥前缀（自检断言）；
- **词表管理不绕过合规**：知识库问题 → 内置兜底（保守），不会「无检查发布」。

## 九、自检与证据（本机实跑，2026-10-13）

- **本次离线自检 `scripts/selftest_knowledge_admin.py`**（TestClient + 假 embedder / 假模型
 注入，不发真实请求、不下载模型、不需要 key）：**156 项 PASS / 0 FAIL / 3 SKIP，退出码 0**；
 关键实证：
 - 词表三路径：空库 → 内置命中；有自定义词 → 命中自定义且类别取自知识库（内置词不命中）；
 读取异常注入 → 回退内置 + WARNING（**不跳过检查**）；
 - seed：dry_run 零写入；首次 `inserted=缺失数`、二次 `inserted=0 / reused=全量`（幂等）；
 经接口 `?dry_run=true` 复核；forbidden_word 行 `embedding IS NULL` 实证；
 - 手动块：未装依赖 `general → 400`（零副作用）而 `forbidden_word → 201`（离线可维护并列证明）；
 `general` 行向量维度 1024、`content_hash` 64 位；人设不存在 → 400；persona_knowledge 无
 人设 → 400；向量化失败 → 400 且**不落库**；
 - 编辑：title / content 更新、content 变化 → 向量重生成 + hash 更新；重向量化失败 →
 **200 + 正文保留 + embedding 置空 + warnings**；仅 status → 归档 / 恢复（此前一致）；
 - 人设 RAG：纳入渲染 + 他人设隔离 + **归档后剔除**（p1 正文不再出现）+ 读取失败降级 +
 与向量检索融合去重且人设知识在前；
 - 文档导入：多块切分全写入 / 同文再导全跳过（幂等）/ 部分块失败如实计数（`chunk_overlap=0`
 下 `failed=1`、`inserted=total-1`、warnings 恰 1 条）/ `.pdf` 与非 UTF-8 与空文件 400 /
 `persona_knowledge` 关联人设导入 / 超长与非法参数 422/400 / 未就绪 400 零写入；
 - 创作图合规**双路径**：仅存于自定义词表的词 → `content_check.hits` 命中（图走库）；
 仅存于内置词表的词 → 命中（回退兜底，条件允许时）；`content_check` 结构不变、笔记仍 draft；
 - 接口面 / 零迁移：8 条路径方法精确、无 PUT/DELETE、静态在前、`limit=0 → 422`、
 12 个响应模型无敏感字段、`include_router ≥ 12`、`lifespan` 恰 1、`migrations/versions` 恰 2 支、
 `alembic current=0002 (head)`、`alembic check` 零漂移、public 业务表恰 10 张；
 - 合规扫描：20 个产物文件零禁词（**扫描器自指豁免：本脚本仅含模式定义**，SKIP 明示不代表通过）；
 - 清理复查：按 id + 前缀 `[SELFTEST]知识管理` 双口径残留全 0（含 agent_runs / 检查点线程差集）。
 - SKIP 2 项：均为**数据条件跳过**（库内已有「稳赚」系词 / 已有 active 违禁词），如实标注。
- **回归矩阵（13 脚本 1116 项，全部 EXIT=0 零回退）**：`selftest_db 15 · verify_schema 13 ·
 selftest_api 37 · selftest_agent_harness 60 · selftest_hotspot 54 · selftest_topic_agent 42 ·
 selftest_content_agent 98 · selftest_cover 79 · selftest_studio 86 · selftest_metrics 139 ·
 selftest_diagnostics 156 · selftest_dashboard 176 · selftest_rag 161`
 （旧脚本 `include_router ≥ N` 口径天然兼容；`selftest_rag` 的 PATCH 断言实测兼容新响应模型）；
- **前端**：`tsc --noEmit` EXIT 0；`eslint` EXIT 0；`next build` EXIT 0
 （`/knowledge` ○ 静态 + 4 条新 BFF 在路由清单）；
- **healthcheck 四项全绿**：后端存活 / 后端就绪（database ✓ pgvector ✓ redis ✓）/ 前端首页 /
 前端代理透传；
- **浏览器冒烟**（生产 `next start` + 无头 Chrome）：**20 项 PASS / 0 FAIL / 1 NOTE，
 0 条未预期 console error** —— 四 Tab、类型筛选选「违禁词」→ 管理条出现（seed 按钮 +
 新增违禁词 + 保守兜底说明）、经「新增违禁词」打开创建 Dialog（预设类型 = 违禁词，
 「违禁词 *」「类别（可选）」「关联人设（可选）」就位，取消关闭 = **零写库**）、
 文档导入 Tab 三件套（导入方式 / 文本内容 / 开始导入 + .txt/.md 说明）；
 截图 `docs/screenshots/s19_01~04`。
 NOTE：当前知识库为空（无行）→ **未截「编辑 Dialog」与「违禁词行类别展示」**；
 两处代码路径均由离线自检全量覆盖（PATCH 编辑断言 / 列表带出 category 断言），
 真实数据的视觉复核留给用户自行录入后查看（不代为写库）。

## 十、未验证项（如实说明，未做美化）

- **真实 bge 向量化未验证**（本机 `local_deps_installed == false`）：
 文档导入 / 人设知识 / 非违禁词手动块的**真实向量化分支未跑过** —— 当前证据是
 「未就绪 → 400 零写入」的负分支 + 假 embedder 的正分支；请安装依赖并下载模型后实跑；
- **真实导入切分质量未评估**：内置等效切分器为 800/100 默认参数下的行为已由自检断言，
 真实文档的切分观感需用户粘贴实际素材确认（官方包若安装将自动切换，无需改码）；
- **真实召回与反哺效果**：人设知识 / 手动块入库后的真实语义召回质量依赖真实模型，未验证；
- **浏览器冒烟未覆盖的视觉分支**：编辑 Dialog 打开态、违禁词行类别展示（库内无数据），
 见第九节 NOTE。

**用户侧命令（按需）**：

```bat
:: 1) 安装向量化依赖（一次性；约数百 MB）
cd backend && .venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2) 预检向量化（维度 / 同义近无关远 / 端到端召回；不消耗 LLM token）
.venv\Scripts\python.exe ..\scripts\check_embedding_local.py

:: 3)（可选）预检内置词表写入条数；确认后真跑
.venv\Scripts\python.exe ..\scripts\seed_forbidden_words.py --dry-run
.venv\Scripts\python.exe ..\scripts\seed_forbidden_words.py

:: 4) 重启前后端后在 /knowledge 页实测：新建知识块 / 文档导入 / 违禁词管理
```

## 十一、本次【不做】

- 违禁词命中后的**自动改写 / 替换建议**（本次只入库与扫描，不改创作逻辑）；
- `.docx` / `.pdf` 解析、URL / 网页抓取、目录批量导入；
- 知识块的物理删除、批量删除、回收站；
- 向量重建 / 重索引工具（换模型维度不一致时给清晰错误，本次不做迁移）；
- 定时自动导入 / 定时 seed；公网部署。
