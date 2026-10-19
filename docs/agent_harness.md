# Agent Harness 基座 · xhs-agent

> 本文件描述**所有 LLM 智能流程的统一运行底座**：基于 LangChain / LangGraph 真正实现，
> 用 ``StateGraph`` 编排、Postgres 检查点持久化、``interrupt`` 人在回路、
> 统一可观测与审计。
>
> **本次只搭底座 + 一个离线冒烟图**，不实现选题 / 创作 / 诊断 / RAG 任何业务逻辑，
> 也不新增任何业务 HTTP 路由。

---

## 一、为什么需要这一层

后续的每一步（选题 Agent、创作 Agent、单篇诊断、RAG 检索）都会：
调用大模型 → 要求结构化输出 → 可能中途要人确认 → 需要记录成本与稳定性 → 出错要能重试。

如果每个业务模块各自实现一遍这套东西，会立刻出现：模型构造方式不一致、
JSON 容错各写一套、成本记录缺失、重试策略互相冲突。因此先立地基。

**这一层的产出**：一套约定 + 一组可复用件，业务图只需要写「节点做什么」，
其余（检查点、审计、重试、JSON 容错、配置闸门）全部由底座承担。

---

## 二、分层与数据流

```
 ┌──────────────────────────────────────────────┐
 FastAPI (lifespan) │ 启动：建池 + CREATE SCHEMA + setup 幂等 │
 app/main.py │ 关闭：关池 │
 └───────────────────┬──────────────────────────┘
 │
 业务调用方（后续步骤） ▼
 │ ┌──────────────────────────┐
 └── run_agent ────────►│ runner.py（统一同步入口）│
 kind / build_fn └───────────┬──────────────┘
 inputs / resume │ 包一层 AgentRunRecorder
 ▼
 ┌──────────────────────────────────────────┐
 │ LangGraph StateGraph │
 │ ├─ 状态 BaseAgentState (TypedDict) │
 │ ├─ 节点 draft / review / finalize … │
 │ ├─ 边 START → … → END │
 │ ├─ 重试 RetryPolicy(retry_on=可重试错误) │
 │ └─ 人在回路 interrupt / Command(resume) │
 └───────┬────────────────────┬──────────────┘
 │ │
 checkpointer │ │ 节点内调用
 ▼ ▼
 ┌──────────────────────────────┐ ┌────────────────────────────┐
 │ PostgresSaver │ │ llm/factory.get_chat_model │
 │ schema = langgraph（隔离） │ │ ChatOpenAI(base_url= │
 │ checkpoint_migrations │ │ DeepSeek, OpenAI 兼容) │
 │ checkpoints │ │ + json_repair.invoke_json │
 │ checkpoint_blobs │ │ （结构化输出 + 容错重试） │
 │ checkpoint_writes │ └────────────┬───────────────┘
 └──────────────────────────────┘ │
 │ 可选
 审计 ▼
 ┌──────────────────────────────┐ ┌────────────────────────────┐
 │ agent_runs（业务表 · Alembic）│ │ LangSmith（默认关闭） │
 │ token / 解析次数 / 节点轨迹 │ │ 未配置则完全不上报 │
 └──────────────────────────────┘ └────────────────────────────┘
```

**一次运行的时序**：

1. 调用方 ``run_agent(kind, build_fn, inputs)``；
2. ``runner`` 生成 ``run_id`` / ``thread_id``，用 ``AgentRunRecorder`` **先插入一行** ``agent_runs``；
3. 用 ``make_config(thread_id)`` 构造配置（含 ``recursion_limit``），``graph.invoke(state, config)``；
4. 节点内 ``invoke_json`` 拿结构化输出（自动 JSON 模式降级 + 纠正重问）；
5. 若节点调用 ``interrupt``，图**暂停**并把载荷写进检查点，``invoke`` 返回 ``__interrupt__``；
6. 恢复时 ``run_agent(..., resume=..., thread_id=<同一个>)`` → ``Command(resume=...)`` 继续；
7. ``recorder`` 回写 ``status`` / token 三字段 / ``json_parse_attempts`` / ``latency_ms`` / 错误。

---

## 三、三条职责边界（务必分清）

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| **LangGraph（本基座）** | 编排**需要大模型**的智能流程：选题生成、内容创作、单篇诊断、RAG 检索/生成 | 不做无 LLM 的定时采集；不代替人工审核 |
| **APScheduler** | **无 LLM** 的定时数据采集（热搜抓取、快照落库） | 不做任何 LLM 调用、不做流程编排 |
| **业务表状态流转 + 网页操作** | 人工审核并**手动发布**：改 ``topics.status`` / 回填 ``publish_url`` | 不自动发布、不登录小红书 |

> ``interrupt`` 在本次只作为**预留底座**（「中途审批 / 多候选选择 / 润色后继续」），
> 本次**不实现**任何具体审批业务。人工审核的主线仍是「业务表状态 + 网页操作」，
> 不依赖 interrupt。

---

## 四、检查点框架表：管理策略与隔离

### 4.1 为什么必须隔离

``agent_runs`` 等 10 张是**业务表**，只能由 Alembic 管；LangGraph 的
``checkpoints`` 等是**第三方框架托管表**，由 ``PostgresSaver.setup`` 幂等创建/升级。
两者如果混在 ``public``，此前「恰好 10 张业务表」的断言就会被污染。

### 4.2 实际做法（如实记录）

所装 ``langgraph-checkpoint-postgres 3.1.2`` 的 ``PostgresSaver`` **没有** ``schema``
构造参数；它的建表语句是 ``CREATE TABLE IF NOT EXISTS checkpoints (...)`` —— **不带 schema 前缀**，
落点由连接的 ``search_path`` 决定。

因此实现方式是：

1. 用 psycopg ``ConnectionPool`` 建**专用连接池**，连接参数带
 ``options='-c search_path=langgraph,public'``；
2. 先 ``CREATE SCHEMA IF NOT EXISTS langgraph``（``autocommit=True`` 立即提交）
 —— **这一步必不可少**：``search_path`` 里不存在的 schema 会被 PostgreSQL **静默跳过**，
 框架表就会悄悄落到 ``public``，隔离失效；
3. 再 ``PostgresSaver(pool).setup``，框架表便落在 ``langgraph``。

### 4.3 框架托管表清单（4 张）

| 表 | 所在 schema | 管理者 |
| --- | --- | --- |
| ``checkpoint_migrations`` | ``langgraph`` | ``setup`` |
| ``checkpoints`` | ``langgraph`` | ``setup`` |
| ``checkpoint_blobs`` | ``langgraph`` | ``setup`` |
| ``checkpoint_writes`` | ``langgraph`` | ``setup`` |

### 4.4 纪律

- **业务 10 表永远走 Alembic**；框架表**绝不手写进业务迁移**（``selftest_db.py`` 会断言迁移里不含框架表）；
- 升级 ``langgraph-checkpoint-postgres`` 后，**重新执行一次 ``setup``** 即完成框架表升级
 （``checkpoint_migrations`` 记录版本，重复执行幂等）；
- 第二道保险：``scripts/selftest_db.py`` 与 ``scripts/verify_schema.py`` 都内置了
 ``FRAMEWORK_TABLES`` 清单并显式排除 + 断言 —— 即使将来改成建在 ``public``，
 「业务恰好 10 张表」的断言依然成立。

### 4.5 隔离证据（实测）

```
[PASS] 恰好 10 张业务表且表名齐全
[PASS] 框架检查点表未落在 public（已被排除在业务表断言之外）
[INFO] 框架托管表 4 张，位于 schema=['langgraph']
```

``public`` 下的表：``accounts / personas / hotspots / topics / notes / covers /
note_metrics / diagnostics / knowledge_snippets / agent_runs`` + ``alembic_version``
—— 与此前完全一致。

---

## 五、全部配置项（``AGENT_*``）

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| ``LLM_API_KEY`` | 空 | DeepSeek 密钥。**只放本地 .env**，绝不入库/入日志/入 agent_runs |
| ``LLM_BASE_URL`` | ``https://api.deepseek.com`` | OpenAI 兼容端点；换供应商只改这里 |
| ``LLM_MODEL`` | 中文占位符 | **占位**，以 DeepSeek 控制台当前列表为准 |
| ``AGENT_LLM_ENABLED`` | ``false`` | 总开关。false 时（除真实检查脚本外）**不发起任何对外 LLM 调用** |
| ``AGENT_RECURSION_LIMIT`` | ``40`` | LangGraph 递归上限（新版默认 1000，这里显式收窄） |
| ``AGENT_NODE_MAX_RETRIES`` | ``2`` | 节点**重试次数**（不含首次）。仅重试可重试错误；换算见 §6 |
| ``AGENT_JSON_MAX_ATTEMPTS`` | ``3`` | 结构化输出最多尝试次数（含首次） |
| ``AGENT_NODE_TIMEOUT_SECONDS`` | ``60`` | 注入 ``ChatOpenAI`` 的 HTTP 超时（见 §6 说明） |
| ``AGENT_CHECKPOINTER`` | ``postgres`` | ``postgres`` / ``memory``（memory 仅离线自测） |
| ``AGENT_LANGSMITH_TRACING`` | ``false`` | LangSmith 观测开关，默认关闭 |
| ``AGENT_LANGSMITH_API_KEY`` | 空 | LangSmith key（仅在开启时使用） |
| ``AGENT_LANGSMITH_PROJECT`` | ``xhs-agent`` | LangSmith 项目名 |

### 成本公式（**不内置价格表**）

价格随时会变，本仓库只记录 **token 数**，成本由使用者按当时的实际单价自行计算：

```
本次成本 ≈ (prompt_tokens × 输入单价_per_token)
 + (completion_tokens × 输出单价_per_token)

total_tokens = prompt_tokens + completion_tokens
```

``agent_runs`` / 冒烟图状态里存的字段名对应关系：
``usage.input_tokens`` → ``prompt_tokens``，``usage.output_tokens`` → ``completion_tokens``。

单价请以 DeepSeek 官方为准：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing>

---

## 六、重试与超时的分层（含一个必须知道的事实）

**重试是分层的**，两层都会生效：

| 层 | 触发 | 次数 | 实现 |
| --- | --- | --- | --- |
| JSON 解析层 | 输出不是合法 JSON | ``AGENT_JSON_MAX_ATTEMPTS``（3 次） | ``json_repair.invoke_json`` 追加纠正提示重问 |
| 节点层 | 429 / 5xx / 网络异常 / JSON 解析仍失败 | 重试 ``AGENT_NODE_MAX_RETRIES``（2）次 → 总尝试 **3** 次 | LangGraph ``RetryPolicy`` |
| HTTP 层 | 连接抖动 | ``AGENT_NODE_MAX_RETRIES``（2） | OpenAI SDK 内建 ``max_retries`` |

⚠️ **语义换算（容易踩坑）**：``AGENT_NODE_MAX_RETRIES`` 是**重试次数**，
而 LangGraph 的 ``RetryPolicy.max_attempts`` 是**总尝试次数（含首次）**，因此：

```python
max_attempts = AGENT_NODE_MAX_RETRIES + 1 # 2 → 3
```

**最坏情况下模型调用次数 = 3 × 3 = 9 次**（JSON 层 3 次 × 节点层 3 次），
这是有意的纵深防御，但要清楚它会放大 token 消耗。
（实测确认：持续抛可重试错误时 ``model.calls == max_attempts``；不可重试错误 ``calls == 1``。）

分类规则（``app/agent/errors.py``）：

| HTTP | 归类 | 是否节点重试 |
| --- | --- | --- |
| 429 限流 / 5xx / 连接失败 / 超时 | ``AgentRetryableError`` | ✅ 重试 |
| 400 / 401 / 403 / 404 / 422 | ``AgentNonRetryableError`` | ❌ 立即失败 |
| JSON 解析彻底失败 | ``AgentJSONError``（继承可重试） | ✅ 重试 |

> ``AgentJSONError`` 之所以继承 ``AgentRetryableError``，是因为 Task 约定「JSON 解析失败」属于
> 可重试错误。因此该异常**先由 ``invoke_json`` 内部消化 3 次**，仍失败才交给节点层再兜 3 次。

> ⚠️ **一个必须知道的事实**：``AGENT_NODE_TIMEOUT_SECONDS`` **没有**用 LangGraph 的
> 节点级 ``timeout`` 实现。原因是 LangGraph 明确**不支持同步节点的 timeout**：
>
> ```
> ValueError: Node timeouts are only supported for async nodes because
> sync Python execution cannot be safely cancelled in-process.
> ```
>
> 而本项目**刻意只用同步栈**（与同步 SQLAlchemy、同步 service/路由一致，避免两套并发模型）。
> 因此该配置改为注入 ``ChatOpenAI`` 的 ``timeout``，由 HTTP 客户端层面保证「不会无限等待」。
> 这是权衡后的取舍，不是遗漏。

---

## 七、实际安装版本（本机实测）

| 依赖 | 版本 |
| --- | --- |
| langchain | **1.4.4** |
| langchain-core | **1.6.9** |
| langchain-openai | **1.7.0** |
| langgraph | **1.2.14** |
| langgraph-checkpoint | **4.2.0** |
| langgraph-checkpoint-postgres | **3.1.2** |
| langsmith | 0.14.7 |
| openai（SDK） | 3.28.0 |
| psycopg-pool | 3.3.3 |

**不引入** ``langchain-deepseek`` —— 一律走 ``langchain_openai.ChatOpenAI`` 指向 DeepSeek，
保证可替换到任何 OpenAI 兼容端点。

---

## 八、代码结构

```
backend/app/agent/
├── __init__.py # 包说明 + 异常导出
├── errors.py # 异常体系 + classify_exception（可重试/不可重试）
├── llm/factory.py # get_chat_model：OpenAI 兼容模型工厂 + 配置闸门
├── parsing/json_repair.py # 稳健 JSON 解析 + invoke_json（结构化输出闭环）
├── state/base.py # BaseAgentState + initial_state/merge_usage/merge_parse_attempts
├── checkpoint.py # Postgres/memory 检查点生命周期 + schema 隔离 + 落点报告
├── graph/base.py # new_thread_id/make_config/llm_retry_policy/new_state_graph/compile_graph
├── tracing/recorder.py # AgentRunRecorder（写 agent_runs）+ redact_secrets 脱敏
├── tracing/langsmith.py # LangSmith 可选开关（默认关闭）
├── runner.py # run_agent：统一同步入口
└── examples/smoke_graph.py # 离线冒烟图（含 ScriptedChatModel 假模型）
```

---

## 九、待真实 key 验证清单（⚠️ 本会话未验证）

本会话**没有** DeepSeek key，因此以下三项**未经验证**，需要你在
``backend/.env`` 填好 key 后手动运行 ``scripts/check_llm_harness.py`` 确认：

| # | 待验证项 | 为什么离线测不了 |
| --- | --- | --- |
| 1 | **真实结构化输出稳定性**（连续 3 次是否都成功） | 假模型不经过真实网络的采样与截断 |
| 2 | **``json_parse_attempts`` 是否恒为 1** | 取决于真实模型是否稳定吐合法 JSON |
| 3 | **真实 token 消耗**（``usage`` 是否被回填） | 只有真实端点才返回 ``usage_metadata`` |

```
backend\.venv\Scripts\python.exe scripts\check_llm_harness.py
```

脚本还会验证错误路径：故意用**错误的 key** 调用，确认落 ``failed`` 且
``error_message`` 不泄露 key。未配置时脚本以**退出码 2** 安全拒绝，**不发起任何请求**。

---

## 十、与后续步骤的衔接

| 步骤 | 在本底座上做什么 |
| --- | --- |
| 选题 Agent | ``run_agent("topic_generation", build_fn=...)`` + ``invoke_json``；写 ``topics`` / ``agent_runs`` |
| 创作 Agent | 复用同一 ``run_agent``；``personas.system_prompt`` 在此使用；写 ``notes`` |
| 单篇诊断 | 同上；写 ``diagnostics`` |
| RAG | 复用 ``agent_runs`` 审计；embedding 走本地模型（注意 ``EMBEDDING_DIM`` 决策） |
| 此前（若有调度器） | 其启停**必须合并进 ``app/main.py`` 现有的 lifespan**，严禁新建第二个 lifespan |

> ⚠️ ``app/main.py`` **当前已有一个 lifespan**（负责检查点与 LangSmith 生命周期）。
> 加入 APScheduler 时，请把调度器的 ``start`` / ``shutdown`` 加到同一个函数里。
