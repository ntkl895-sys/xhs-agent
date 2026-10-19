# 选题图的「LLM 自主选工具」（tool calling，可选）

> 目标：给选题环节补上 **function calling** 能力——模型在生成选题前自主决定调用哪些
> **只读**工具、调几次、何时停止；默认关闭，可与确定性基线逐场景对照。
> **不新增对外业务图**（业务图总数仍为 7），不改变最终落库契约。

## 1. 结论先行（n=12 小样本，方向性）

| 指标 | tool 模式 | baseline |
| --- | --- | --- |
| 工具选择 Precision（微观 / 宏观） | **0.3889 / 0.5417** | — |
| 工具选择 Recall（微观 / 宏观） | **1.0 / 1.0** | — |
| 白名单外幻觉工具率 | **0.0** | — |
| 平均工具轮数 | 2.0 | 0 |
| 3 轮内收敛率 | **1.0** | 1.0 |
| 最终 JSON 一次解析且落库 | **1.0（12/12）** | 1.0（12/12） |
| token 合计 | 68,775 | 11,935（**5.8×**） |
| 平均墙钟 | 11.7s | 3.2s |

**决策：`TOPIC_TOOLCALLING_ENABLED=false`（默认关闭）。** 能力打通了（召回 100%、零幻觉、
收敛 100%、JSON 质量不退），但**选择精确率只有 0.389**：9 个「需要工具」的场景里模型几乎
每次都把 4 个工具全部调一遍，而 ground truth 平均只要 1.7 个；换来的是 **5.8 倍 token**
和 **3.6 倍耗时**，最终 JSON 一次成功率没有任何提升。这是「能用」但「不划算」，按证据不启用。

> 这不是实现缺陷，而是工具使用型 Agent 的典型失败模式：在「可以调工具」的提示下，
> 模型倾向**求全**而非**求准**。要解决它需要工具路由训练、两阶段筛选或按场景裁剪工具集，
> 属于下一阶段的工作，不在本次范围内。

## 2. 拓扑

```
load_context ──► tool_decide ──(有合法 tool_calls 且未到上限)──► tool_exec
 │ │
 │◄──────────────(未到上限)────────────────────┘
 └──(无调用 / 到上限)──► generate_topics ──► validate_dedupe ──► persist / give_up
```

- 关闭开关时：**不新增任何节点**，`load_context → generate_topics` 与接入前一致，
 提示词逐字节不变（自检里有断言）。
- 开启时：多 `tool_decide` / `tool_exec` 两个节点与两条条件边，其余节点、校验、落库全未动。
- 最终结构化输出仍由原 `generate_topics` 走 `invoke_json`（三层兜底 + 计数），
 所以 `json_parse_attempts`、落库契约、人工审核边界全部不变。

## 3. 开关与配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TOPIC_TOOLCALLING_ENABLED` | `false` | 打开后走工具回路；关闭时零行为差异 |
| `TOPIC_TOOLCALLING_MAX_ROUNDS` | `3` | 工具执行轮数硬上限（到顶直接进生成，不再问模型） |

## 4. 工具（全部只读、白名单、幂等）

| 工具 | 入参（pydantic schema） | 底层现有实现 |
| --- | --- | --- |
| `search_hotspots` | `q: str=""`、`limit: 1~10=5` | `crud_hotspot.build_criteria(include_all_dates=True)` + `get_multi_sorted` |
| `search_knowledge` | `query: str`（必填）、`top_k: 1~5=3`、`chunk_types`（限 4 种） | `crud_knowledge.search_snippets`（pgvector 余弦） |
| `list_topics` | `q: str=""`、`limit: 1~20=10` | `crud_topic.build_criteria(persona_id=闭包)` + `get_multi_with_names` |
| `get_persona` | 无参数 | `db.get(Persona, 闭包 persona_id)`（含提示词里没有的 `taboo` / `signature_style`） |

**作用域**：`persona_id` 由闭包注入，**不是工具参数**——模型无法指定别人的 id，也不可能跨人设读数据。
**写操作**：V1 一个都不暴露（无生成 / 发布 / 删除 / 落库），没有任何外部域名请求。

## 5. 安全与收敛

1. **白名单**：工具名写死在 `TOOL_NAMES`；模型请求白名单外的名字时，该调用在
 `tool_decide` 里被**丢弃**（不进入消息通道、不执行），并记进 `hallucinated_tools`
 与 `node_trace`；不会触发 ToolNode 报错，也不会因此重试到上限。
2. **轮数硬上限**：`route_after_exec` 到顶直接进生成，**不再多问一次模型**。
3. **入参校验**：`args_schema` 由 pydantic 生成，非法参数由 ToolNode 转成错误 ToolMessage。
4. **异常降级**：`ToolNode(handle_tool_errors=True)` + 逐个执行，单个工具失败只影响上下文，
 节点状态记 `partial`，流程继续，不阻断生成、不诱导重试。
5. **可观测**：每次调用写 `tool_events`（`tool` / `args` / `ok` / `result_summary` /
 `duration_ms` / `error`），进 `node_trace`，并由 service 回填到
 `agent_runs.outputs.tool_calling`（含 `rounds` / `hallucinated` / `calls`），可回放。

## 6. 评测方法（先冻结、后评测）

- 夹具：`docs/benchmark/fixtures/testsets/topic_tool/`
 - `scenarios.json`：12 个场景，每场景含人设（含禁忌 / 口头禅）、热点、预置历史选题、
 **人工标注的 `expected_tools`** 与标注理由；
 - `manifest.json`：version / 统计 / sha256 指纹 / 冻结声明（改动即报错）。
- 场景构成：3 个「不该调工具」（测过度调用）、4 个多工具场景；四个工具各出现 2~5 次。
- 对照：同夹具、同提示词，仅切 `TOPIC_TOOLCALLING_ENABLED`；逐场景落库后读
 `agent_runs` 与 `outputs.tool_calling` 统计。
- 结果：`docs/benchmark/results/topic_tool_calling.json`（12 条逐场景明细 + 汇总）。

### 逐场景（tool 模式）

| 场景 | 标注 | 实际调用 | P | R | 轮数 |
| --- | --- | --- | --- | --- | --- |
| tt_01 | knowledge | knowledge, topics, persona, hotspots | 0.25 | 1.0 | 3 |
| tt_02 | hotspots | persona, topics, hotspots, knowledge | 0.25 | 1.0 | 3 |
| tt_03 | persona | persona, topics, knowledge, hotspots | 0.25 | 1.0 | 2 |
| tt_04 | topics | persona, topics, hotspots, knowledge | 0.25 | 1.0 | 3 |
| tt_05 | （无） | （无） | 1.0 | 1.0 | 0 |
| tt_06 | knowledge, hotspots | persona, knowledge, topics, hotspots | 0.5 | 1.0 | 3 |
| tt_07 | persona, knowledge | persona, topics, knowledge, hotspots | 0.5 | 1.0 | 3 |
| tt_08 | （无） | （无） | 1.0 | 1.0 | 0 |
| tt_09 | topics, knowledge | persona, topics, knowledge, hotspots | 0.5 | 1.0 | 2 |
| tt_10 | hotspots | persona, topics, hotspots, knowledge | 0.25 | 1.0 | 2 |
| tt_11 | （无） | （无） | 1.0 | 1.0 | 0 |
| tt_12 | knowledge, topics, hotspots | persona, topics, knowledge, hotspots | 0.75 | 1.0 | 3 |

（表中 knowledge=search_knowledge、hotspots=search_hotspots、topics=list_topics、persona=get_persona）

### 解读纪律

- **n=12 是小样本**：单个场景就能让微观精确率跳 8 个百分点，只做方向性判断；
- 「3 个无工具场景全部正确不调用」说明模型不是无脑调用，**是能判断"要不要用工具"的**；
 问题在「用几个」——这是本次最有价值的发现；
- baseline 的 JSON 一次成功率同为 1.0，说明**本次的工具调用没有带来质量增益**，
 不能写成「提升了选题质量」。

## 7. 复现

```text
:: 冻结夹具（已冻结；确需重建加 --force 并升 version）
backend\.venv\Scripts\python.exe scripts/benchmark/build_toolset.py

:: 零 token 自检（路由 / 收敛 / 越权拒绝 / 异常降级）
backend\.venv\Scripts\python.exe scripts/selftest_topic_tool_calling.py

:: 真实对照（约 60~80k token；不加 --yes 不发请求）
backend\.venv\Scripts\python.exe scripts/benchmark/bench_topic_tool_calling.py --yes

:: 只按已有 detail 重算汇总（零请求）
backend\.venv\Scripts\python.exe scripts/benchmark/bench_topic_tool_calling.py --reaggregate
```

## 8. 未验证项与边界（如实）

1. **真实选择质量的样本量**：n=12，且只覆盖一个模型（`deepseek-flash`）、一种提示词；
 不外推到「工具选择准确率 X%」。
2. **未验证**：多轮工具调用中的参数改写质量、工具结果对选题内容质量的影响（需要人工评分，
 本次只测选择行为与 JSON 契约）。
3. **不做的**：不暴露写操作、不做自动发布、不接任何平台搜索 / 登录态；
 不改 harness 基座八文件；不做工具路由训练或两阶段筛选（下一阶段）。
4. **提示词调整披露**：正式评测前跑过 1 个场景的烟测，发现模型会把 4 个工具全调一遍，
 因此在通用工具指令里补了一句「只调用真正需要的工具」（不针对任何具体场景）。
 评测在此之后一次跑完，未按结果二次调参。
