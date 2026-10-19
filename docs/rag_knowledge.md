# 爆款规律 RAG（本地 bge 向量化 + 规律提炼 + 选题/创作反哺）· xhs-agent

> 目标：**把本账号自己的运营数据变成可复用的规律** —— 从「已发布笔记 + 数据回填（`note_metrics`）+
> 单篇诊断（`diagnostics`）」中归纳爆款规律（**第六个业务图** `app/agent/insights/`），用**本地
> bge-large-zh-v1.5**（1024 维）向量化写入 `knowledge_snippets`，再在**选题**与**创作**两图的
> `load_context` 处按向量 top-k 检索、拼进提示词（**RAG 反哺**，失败静默降级不阻断）。
> **零数据库迁移**（`knowledge_snippets` 表 + HNSW 余弦索引在此前 0001 基线已建）；
> **harness 基座零改动**（业务明细由 service 用 `run_id` 裸 SQL 回填，与前五图同范式）；
> 后端新增依赖**仅 2 个 pip 包**（`langchain-huggingface` / `sentence-transformers`），**前端零新增**。
> 本文给「架构 / 选型 / 依赖 / 图与契约 / 检索 SQL / 接入点与降级 / 接口 / 页面 / 红线 / 证据 / 未验证项」。

---

## 一、定位与边界

- **数据流（单向闭环）**：

 ```
 已发布笔记 + 快照(六指标) + 六率/增量 + 诊断摘要 （只读，全部本地计算）
 │ build_insight_facts（ORM → 普通 dict）
 ▼
 规律提炼图（第六业务图）── 真 LLM 归纳 ──▶ 本地结构校验 ──▶ 真实向量化 ──▶ knowledge_snippets
 ▲ │
 │ 向量 top-k 检索
 人工触发（POST /extract，首版）/ 定时预留（默认关闭） ▼
 选题图 load_context / 创作图 load_context ──▶ 提示词「本账号历史经验（仅供参考）」块
 ```

- **规律只来自本账号本地数据**：六大来源全部在本地库（自己的已发布笔记 / 数据快照 / 诊断）；
 **不爬竞品、不联网抓取、不引入行业均值 / 爆款门槛 / 大盘数据等外部数字**（提示词明文禁止 + 自检断言）。
- **人工审核手动发布红线不变**：本次只产出「知识块」与「带 RAG 上下文的提示词」，不发布任何内容。
- **零迁移**：`RunType.INSIGHT` 只加枚举成员（`native_enum=False` 落 VARCHAR，无 DDL）；
 `ChunkType.HIT_PATTERN / REVIEW_INSIGHT`、`SnippetStatus`、`RefType` 均为 0001 基线已有。
- **harness 基座零改动**：`runner.py` / `state/base.py` / `graph/base.py` / `checkpoint.py` /
 `errors.py` / `parsing/` / `tracing/` / `llm/factory.py` 既有逻辑**一行未动**；
 `RunType.INSIGHT` 属「新增枚举成员」，业务明细（persisted / snippet_ids / llm_raw …）由
 `insights/service.py` 用 `run_id` 裸 SQL 回填 `agent_runs`（与第 10/12/15/16/17 步完全同范式）。

## 二、embedding 服务（`app/agent/embedding/`，新建）

| 文件 | 职责 |
| --- | --- |
| `base.py` | `Embedder` 协议（`dim` + `embed_texts(texts) -> list[list[float]]`，同序、空输入空列表）+ 可识别异常 `EmbeddingError` → `EmbeddingNotAvailableError`（依赖/模型问题）/ `EmbeddingDimensionError`（维度不符）+ `assert_vector_dim(vec, expected)`（**「不静默截断 / 不补零」的唯一执行点**，错误文案含修复指引） |
| `local.py` | `LocalBGEEmbedder`：`langchain_huggingface.HuggingFaceEmbeddings`，默认 `BAAI/bge-large-zh-v1.5`、`device=cpu`、`normalize_embeddings=True`（余弦即点积）；**懒加载单例**（`_MODEL_CACHE`：首次 `embed_texts` 才 import 与加载模型，**import 本模块与 FastAPI 启动均零加载、零下载**）；`local_deps_installed`（`importlib.util.find_spec` 探测，不加载）、`INSTALL_HINT` / `DOWNLOAD_HINT` |
| `openai_compat.py` | `OpenAICompatEmbedder`（`langchain-openai`，读 `EMBEDDING_OPENAI_MODEL / API_KEY / BASE_URL`）——仅 `EMBEDDING_PROVIDER=openai` 时启用，占位实现 |
| `service.py` | **唯一入口 `embed_texts(texts, *, embedder=None)`**：文本 sha256 进程内缓存（`OrderedDict`，上限 4096 条 FIFO）→ 未命中按 `EMBEDDING_BATCH_SIZE=16` 切块 → 逐条 `assert_vector_dim`（1024）→ 写回缓存；返回条数不符抛 `EmbeddingError`（不猜不补）；另有工厂 `get_embedder`（进程内单例）、`validate_dim_settings`（`EMBEDDING_DIM` vs `knowledge.EMBEDDING_DIM` 列维度，进程内只校验一次）、`describe_embedding_readiness`（12 键就绪快照，**无 key 明文**）、`reset_embedder` / `reset_all_caches`（测试辅助） |
| `__init__.py` | re-export 9 项（Embedder / 异常 / 就绪快照 / 入口 / 工厂 / 重置 / 维度校验） |

**向量流向红线**：向量只在**内存与数据库列**中流转 —— **绝不放进图状态**（检查点要序列化）、
**绝不进任何接口响应**（schema 白名单式输出，自检断言）。

## 三、规律提炼 Agent 图（`app/agent/insights/`，第六个业务图）

```
START
 │
load_context 统计已发布数（样本门槛 INSIGHTS_MIN_PUBLISHED=3）；调 build_insight_facts
 │ 载入聚合事实（ORM → 普通 dict）—— 零模型调用
 ├── 样本充足 ────────────────────────────────────────────▶ generate_patterns
 └── 样本不足 ────────────────────────────────────────────▶ insufficient_sample ─▶ END
 （不硬造规律：零模型调用、零写入；output.insufficient_sample=True，service 补记 partial）

generate_patterns 模型（闭包注入）依据程序算好的事实产出规律 JSON；invoke_json 三层兜底
 │
validate_patterns 本地结构校验（逐条剔除不合格项；「全军覆没」才算硬伤回炉）
 │
 ├── 合规 ────────────────────────────────────────────────▶ embed_and_persist ─▶ END
 ├── 未超限（retry_count < AGENT_JSON_MAX_ATTEMPTS）────────▶ generate_patterns（有限回路）
 └── 超限 ────────────────────────────────────────────────▶ give_up ─▶ 抛错（run 记 failed）
```

- **六节点 + 两条条件边 + 有界回路**：最坏模型调用 = `AGENT_JSON_MAX_ATTEMPTS × AGENT_JSON_MAX_ATTEMPTS`
 = **3×3 = 9**（JSON 尝试 × 回路上限；自检对「全坏 JSON → 恰 9 次」「结构全坏 → 恰 3 次」有精确断言）；
- **模型与 embedder 经闭包注入、绝不进状态**（`build_insights_graph(*, model, embedder=None)`）；
- **样本不足不是错误**：`insufficient_sample` 收尾，`data_gaps` 给出「样本不足：至少发布 N 篇并回填
 数据后再提炼（当前仅 X 篇已发布笔记）」；service 据此把 run 补记 `partial`；
- **幂等（绝不重复插入）**：规律文本 sha256 → `content_hash`；按 `(chunk_type, meta.content_hash)`
 查 **active** 记录 —— 已存在计 `reused`、批内重复计 `skipped`、其余计 `persisted`；
 `insert_many` **单事务**（失败整体回滚，不留半截记录）。

**落库字段**（`embed_and_persist` → `knowledge_snippets`）：

| 列 | 值 |
| --- | --- |
| `chunk_type` | `hit_pattern`（爆款规律）/ `review_insight`（复盘心得） |
| `ref_type` / `ref_id` | `NULL`（规律为多来源；来源在 `meta.source_note_ids`） |
| `title` | 规律文本前 50 字 |
| `content` | 规律文本本体 |
| `embedding` | **真实 bge 向量（1024 维）**（仅此处「过手」，不进状态） |
| `status` | `active`（归档走 `PATCH`，无物理删除） |
| `meta` | `source_note_ids / evidence{summary, metrics} / scenario / sample_size / data_range / confidence / content_hash / prompt_version` 九键 |

**契约纯函数**（`contracts.py`，可离线单测）：

- `build_insight_facts(db, *, min_published, max_notes=20)`：已发布笔记（`published_at` 倒序 nulls last）
 + 每篇最新六指标（**复用 `diagnosis.metric_latest`，不重写第二套口径**）+ 六率（`compute_rates`
 4 位小数）+ 首→最新总增量（`compute_trends`）+ 最新一条诊断摘要 + `data_gaps`；
- `normalize_patterns(raw, *, valid_note_ids, max_items, text_max_len)`：宽容解包（`result/data/…` 包裹）→
 逐条剔除（缺 pattern / 场景 / 证据，或来源 id 不在本次样本内）→ 类型非法归一 + 置信度越界置空（warning）→
 条数 / 单条长度上限截断；**来源必须是真实笔记 id —— 模型编造的 id 一律过滤**。

**审计回填**（`service.py`，harness 不改）：`_ensure_embedding_ready` 在消耗任何 token **之前**
检查向量化就绪（未装依赖 → 400 零请求零 run）；LLM 未配置 → 同样 400 零请求零 run；
跑完后用 `run_id` 裸 SQL `UPDATE agent_runs` 合并 `outputs`（persisted / generated / reused /
skipped / snippet_ids / sample_size / min_required / prompt_version / insufficient_sample / rejected /
data_gaps）+ `llm_raw`（`redact_secrets` 脱敏 + 截断 20000）+ 可选 status 修正（partial）。

## 四、检索管道（`crud/knowledge.search_snippets` + `services/rag`）

核心 SQL（SQLAlchemy ORM 表达式拼装，`embedding <=> :qvec` 走 HNSW 余弦索引）：

```sql
SET LOCAL hnsw.ef_search = 40; -- 事务级；仅影响本次查询
SELECT *, embedding <=> :qvec AS distance
FROM knowledge_snippets
WHERE embedding IS NOT NULL
 AND status = 'active' -- include_archived=True 时才不过滤
 AND chunk_type = ANY(:types) -- 可选（hit_pattern / review_insight / …）
ORDER BY distance ASC, created_at DESC, id DESC
LIMIT :k; -- 1~50（接口层校验 1~20）
```

- 返回 `{id, chunk_type, title, content, meta, status, created_at, similarity}` **恰 8 键** ——
 **绝不返回 `embedding` 原始向量**；`similarity = 1 - cosine_distance`（钳制 -1~1，4 位小数）；
- `query_vec` 或 `query_text` 二选一（后者经 `embed_texts` 向量化，异常向上抛可识别错误由调用方降级）；
- `services/rag.py` 三个层次：`retrieve_snippets`（检索）/ `format_rag_block`（渲染块正文，
 逐条「- （爆款规律）文本（样本 N 篇；置信度 X）」）/ `search_for_api`（检索测试接口专用：
 失败**如实告知** 400 + 安装指引，不假装无命中）/ `safe_rag_block`（**两图唯一调用入口**，见下节）。

## 五、RAG 反哺接入（选题 / 创作图，最小改动）

**接入点**（每图**恰 1 处**，均在 `load_context` 节点内，`with SessionLocal as db:` 块中）：

| 图 | 查询文本 | 检索类型 | 状态字段 | 提示词消费 |
| --- | --- | --- | --- | --- |
| 选题 `app/agent/topics/graph.py` | `人设定位 + 热点标题` | `hit_pattern / topic_template / review_insight` | `rag_context` | `generate_topics` → `build_messages(rag_context=…)` |
| 创作 `app/agent/content/graph.py` | `选题标题 + 切入点` | `hit_pattern / review_insight` | `rag_context` | `generate_content` → `build_messages(rag_context=…)` |

- **开关**：`TOPIC_RAG_ENABLED` / `CONTENT_RAG_ENABLED`（均默认 `true`，`.env` 一键回退）；
- **降级红线**：`safe_rag_block` 全捕获 —— 检索 / 向量化 / 渲染任何异常只记 WARNING
 （含来源标注如「选题图 load_context」）并返回空串，**按「无 RAG」继续：主流程不阻断、不报错给用户**；
- **提示词标注**：块正文由 `services/rag.render_rag_prompt_section` 单一定义处渲染为
 「**本账号历史经验（仅供参考，非行业标准）**」+ 逐条文本（含样本量 / 置信度）；
- **不影响既有逻辑的证据**（自检断言）：
 1. `rag_context` 为空 / 缺省时，两图 `build_messages` 的输出与接入前**逐字一致**
 （`with_rag.replace(render_rag_prompt_section(rag), "", 1) == without_rag`，运行时逐字比对）；
 2. 两图**仍为原五节点拓扑**（节点集 / 条件边 / 状态机 / 校验 / 持久化全未动 —— 构建图实测断言）；
 3. 两图源文件中 `safe_rag_block(` 与开关引用**各恰 1 处**（静态断言）；
 4. 向量与 `llm_raw` **不进状态、不进响应**（状态注解无向量字段，实测断言）。

## 六、接口面（第 12 只 router：`app/api/routes/knowledge.py`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/knowledge` | 知识块列表（`?chunk_type=&status=&limit=&offset=`；`created_at` 倒序 + `id` 兜底；**不含 embedding**） |
| `POST` | `/api/v1/knowledge/extract` | 跑规律提炼（**静态路径先注册**；201；embedding 未就绪 / LLM 未配置 → 400 零请求零 run；样本不足 → 201 + `insufficient_sample=true`） |
| `POST` | `/api/v1/knowledge/search` | 检索测试（`{query, chunk_types?, k}` → top-k 含 `similarity`；向量化未就绪 → 400 如实告知） |
| `GET` | `/api/v1/knowledge/{id}` | 详情（不存在 404） |
| `PATCH` | `/api/v1/knowledge/{id}` | 仅 `status` 在 `active` / `archived` 间切换（归档 / 恢复）；非法值 422 |

- **无 PUT、无 DELETE**（知识块只增 / 归档，自检对 OpenAPI 方法面有精确断言）；
- 响应 schema 白名单式（`SnippetOut` 恰 8 字段），**绝不含 `embedding` / `llm_raw` / key / 绝对路径**；
- 错误统一 `{status, code, message, details}`：400 / 404 / 422；路由层**零 SQLAlchemy 表达式**
 （此前分层纪律，`selftest_api` 静态断言 + 本次自检）。

## 七、前端：`/knowledge` 页（第 10 导航入口）

- **三 Tab**：「知识块」（chunk_type / status 筛选 + 表格 + 分页；行内「查看」Dialog 展示规律全文与
 来源证据 / 样本量 / 置信度 / 提示词版本 / 内容指纹 + 「复制规律文本」；「归档 / 恢复」走 PATCH）、
 「提炼规律」（显示当前已发布数与门槛；按钮「提炼爆款规律」**BFF 超时 120000ms**，期间禁用并提示
 「最长约 2 分钟」；结果卡展示 新增 / 复用 / 跳过 / 剔除 四项计数 + 数据缺口如实声明）、
 「检索测试」（query + 类型 + k → top-k 列表含 similarity）；
- **BFF 4 条**（`app/api/v1/knowledge/{route.ts, extract/route.ts, search/route.ts, [id]/route.ts}`）：
 `proxyToBackend` 薄转发（服务端 `API_BASE_URL`、`no-store`、原样透传；生成 120s）；
 `[id]` 用 `Context{params: Promise}`；**不写 `export const dynamic`**；
- 浏览器只请求同源 `/api/v1/**`；一律 `http://localhost:3000`；**npm 零新增依赖**；
- `site-header` 第 10 入口「知识库」保持 `HeaderShell + HeaderWithPathname + Suspense` 结构。

## 八、依赖与模型下载（**需用户本人操作**）

- **Python 新增 2 个包**（`backend/requirements.txt` 已写，**安装由用户执行**）：

 ```bash
 cd backend
 .venv\Scripts\python.exe -m pip install -r requirements.txt
 # 新增：langchain-huggingface（HuggingFaceEmbeddings 适配）+ sentence-transformers（含 torch CPU）
 ```

 首次安装体积较大（含 torch CPU 版，数百 MB 级）；之后 `pip check` 应为零冲突。
- **模型权重**：首次真实向量化时自动下载 `BAAI/bge-large-zh-v1.5`（**约 1.3GB**）。
 国内网络建议先设镜像：

 ```bat
 set HF_ENDPOINT=https://hf-mirror.com
 ```

 缓存位置：`%USERPROFILE%\.cache\huggingface`（可用 `HF_HOME` 改目录）。
 **下载过一次后离线可用**（`HF_HUB_OFFLINE=1`）；`EMBEDDING_DEVICE=cpu` 即可跑（无需 GPU）。
- **预检脚本**：`scripts/check_embedding_local.py`（不消耗 LLM token）——维度 1024 / 同义近无关远 /
 端到端写库检索可召回；模型未下载时给出清晰指引并非 0 退出、不崩溃。

## 九、隐私与红线（工程落实）

- **规律只来自本账号本地数据**：提示词明文「禁止编造未提供的数据」「**禁止出现行业平均、爆款门槛、
 平台官方算法、大盘数据等任何外部数字**」「只能写成『在本账号已发布的 N 篇笔记中……』的个人经验
 表述，禁止写成普适行业定律」（自检断言文案在档；真实 LLM 检查另外扫描产出文本）；
- **不请求任何小红书域名**：后端零平台域名；全前端域名仍**恰 1 处**（此前发布外链，本次未新增，
 自检对全量 ts/tsx 扫描断言）；**不登录、不抓登录态、不自动发布**；
- **向量与 llm_raw 流向**：向量绝不进状态 / 响应；`llm_raw` 只进 `agent_runs`（redact + 截断 20000），
 接口一律不返回；`describe_embedding_readiness` 无 key 明文；
- **只写 `knowledge_snippets`**：提炼 / 检索**不改** notes / topics / note_metrics / diagnostics ——
 自检对八张业务表计数前后相等有断言；
- **知识块只增 / 归档**：无物理删除接口；`PATCH` 仅两态切换。

## 十、自检与证据（本机实跑，2026-10-12）

- **本次离线自检 `scripts/selftest_rag.py`（FakeEmbedder + 假模型注入，不发真实请求）**：
 **161 项 PASS / 0 FAIL / 0 SKIP，退出码 0**（首次实跑即全绿）；关键实证：
 - 提炼成功路径 run=`12aeccf2…` status=success（generated=2 / persisted=2 / parse_attempts=1，假模型恰 1 次调用）；
 - 幂等复跑 run=`aa0cf39d…` reused=2 / persisted=0（查重先于嵌入，底层零新增调用）；
 - 样本不足 run=`f9b7b2af…` status=partial（sample=4/99，零模型调用零写入，data_gaps 就位）；
 - 全坏 JSON → 400 且模型调用**恰 9 次**（3 轮 × 3 次）；结构全坏 → 恰 3 轮回炉 → 400；两条 failed run 均留痕；
 - RAG 降级 WARNING 留痕（`EmbeddingNotAvailableError` → 按「无 RAG」继续）；替换等价断言通过；
 - 清理复查：测试数据按 id + 前缀双口径残留全 0；检查点线程与 agent_runs 按差集清理为 0；
- **回归矩阵（13 脚本）**：`selftest_db 15 · verify_schema 13 · selftest_api 37 · selftest_agent_harness 60 ·
 selftest_hotspot 54 · selftest_topic_agent 42 · selftest_content_agent 98 · selftest_cover 79 ·
 selftest_studio 86 · selftest_metrics 139 · selftest_diagnostics 156 · selftest_dashboard 176 ·
 selftest_rag 161` —— **全部 EXIT=0，零回退**（旧脚本 `include_router ≥ N` 口径天然兼容第 12 只）；见交付报告汇总；
- **前端**：`tsc --noEmit` EXIT 0；`eslint` EXIT 0；`next build` EXIT 0（`/knowledge` 静态壳 +
 4 条 BFF 路由均在路由清单）；
- **真实分支实测（不消耗 token）**：
 - `check_embedding_local.py`：本机未装依赖 → 清晰安装 / 镜像指引 + **退出码 2，不崩溃**（真实向量化未验证，见下节）；
 - `check_insights_agent.py` 四闸门分支实测：LLM 开关关闭 / 模型占位 / 向量化依赖未装 / 已发布样本不足
 （1 篇 < 3）→ 均**退出码 2 且零请求**、指引清晰；
- **迁移**：`migrations/versions` 仍恰 2 支（0001/0002）、`alembic current` = `0002 (head)`、
 `alembic check` 零漂移（退出码 0）、public 业务表仍恰 10 张；
- **浏览器冒烟**（生产 `next start` + headless Chrome）：`/knowledge` 三 Tab / 导航入口 /
 提炼信息 / 检索交互（向量化未装 → 「向量化未就绪」友好提示）/ 0 未预期 console error，
 截图 `docs/screenshots/s18_01~03`。

## 十一、未验证项（如实说明，未做美化）

- **真实本地 bge 向量化未验证**：本机 venv **尚未安装** `langchain-huggingface` /
 `sentence-transformers`（`local_deps_installed=false`），模型（约 1.3GB）未下载 ——
 `check_embedding_local.py` 的「真实维度 / 同义近无关远 / 端到端召回」分支**未跑过**；
 请安装依赖后运行该脚本验证（预期 0 退出）；
- **真实 LLM 规律提炼未验证**：`check_insights_agent.py` 的成功路径（真实 DeepSeek + 真实向量落库）
 需先满足上一条，再运行该脚本（会消耗 token；建议 `--rounds 1` 语义即单次提炼）；
- **检索召回的语义质量**：依赖真实模型；本地自检用「自查询向量必排第一」的确定性断言替代，
 真实语义排序需真实模型复核；
- **定时提炼**：未实现（配置项 `INSIGHTS_SCHEDULER_ENABLED` 仅预留，默认 `false`；首版以手动触发为主）。

## 十二、本次【不做】

- 违禁词从代码迁移到知识库、人设知识库、手动文档导入（**此前**）；
- 行业均值 / 竞品爆款基准 / 跨篇行业基准（外部数据一律不引入）；爬外部平台；自动发布；
- 非 1024 维的迁移（换模型维度不一致时给清晰错误，本次不做迁移）；
- 公网部署。
