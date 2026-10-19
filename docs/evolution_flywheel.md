# 人在环里的自进化飞轮 v1（失败样本驱动的评测集进化）

> **它是什么**：一条**离线**进化管道，把线上失败样本变成冻结评测集里的回归用例。
> **它不是什么**：不是新 Agent、不是新业务图、不是自动调 prompt / 自动改代码 / 自动发布。

## 1. 位置与红线

| 约束 | 落实方式 |
| --- | --- |
| 不新建 harness | 复用 `run_agent` / `invoke_json` / `agent_runs` / 现有评测脚本，`backend/app/agent/` 与 `app/core/errors.py` **八文件零改动** |
| 不新建业务图 | 图数仍为 7；本包只有 CLI 脚本，没有 LangGraph 图 |
| 零数据库迁移 | `versions` 恒 2 支；候选与决定全部落 JSON |
| 零新增依赖 | 只用标准库 + 项目已装的 `sqlalchemy` / `langchain-*` |
| 不自动生效 | 采集只 `SELECT`；候选只写仓库外工作目录；**只有 `evolve_merge.py` 会写夹具**，且必须喂入 `decision=adopt` 的决定清单 |
| 可回滚 | 并入前整目录备份；门禁不通过自动还原并复跑确认 |

## 2. 五步闭环

```
① harvest（只读） agent_runs / notes / topics ──► 信号清单（JSON）
 │ 9 类信号：兜底解析 / 不可救回 / 重问 / 失败 / 半成品 /
 ▼ 工具错误 / 越权工具 / 合规命中 / 人工废弃选题
② propose（零 token） 信号 ──► 候选 case（规则化，带 expected_source 与证据）
 │
 ▼
③ review（人在环） 逐条 采纳 / 拒绝 / 改写 / 跳过 ──► 决定清单
 │ 未出现在决定里的候选 = skip
 ▼
④ merge（唯一写夹具） 静态再校验 → 备份 → 追加 → 版本 +1 → 重算 stats/sha256
 │
 ▼
⑤ gate（不过即回滚） 跑既有评测脚本 → 三口径判定 → 不通过则还原并复跑确认
```

## 3. 信号 → 目标测试集

| 信号 | 判定依据 | 目标 | 候选 label |
| --- | --- | --- | --- |
| `json_fallback` | `llm_raw` 经 `extract_json_with_source` 命中「去围栏 / 平衡扫描」 | json_robustness | `repaired` |
| `json_unrecoverable` | 有 `llm_raw` 但三层兜底全失败 | json_robustness | `failed` |
| `json_retry` | `json_parse_attempts > 1` | json_robustness | `direct` / `repaired` |
| `run_failed` / `run_partial` | `status` 为 failed / partial 且有 `llm_raw` | json_robustness | `failed` |
| `compliance_hit` | `notes.content_check.hits` 非空 | compliance | `expect_hit=true` |
| `tool_error` / `tool_hallucination` | `outputs.tool_calling.calls[].ok=false` / `hallucinated[]` | **暂不产候选** | — |
| `topic_discarded` | `topics.status='discarded'` | **暂不产候选** | — |

后三类只登记不产候选，原因写在 `deferred_reason` 里（缺 persona/hotspot 场景骨架、当前无对应冻结集），
**不硬造一个测不出东西的用例**。

## 4. 门禁三口径（本次最关键的设计）

| 口径 | 定义 | 门禁要求 |
| --- | --- | --- |
| `legacy_invariant` | 并入前已存在的 case，逐条判定是否完全没变 | **必须 100% 相同** |
| `new_case_consistency` | 新增 case 的 label/期望与实现实际行为是否一致 | **必须 1.0** |
| `score_all` | 全集合准确率（分母变大了） | **只记录，不作门禁** |

### 为什么 `score_all` 不能当门禁

失败样本按定义就是「当前实现过不了的样本」。如果要求「并入后全集合准确率不下降」，
那么任何真实失败样本都会被挡在门外——飞轮会自我否定，永远只能收录已经能通过的样本。

真正的「退化」只有两种：

1. **旧样本的行为变了**（`legacy_invariant`）：确定性评测下必须逐条一致，变了就是写坏了；
2. **新样本被标错了**（`new_case_consistency`）：把「实现救不回」的样本标成 `repaired`，
 或 `expected` 与真实解析结果不符——这类坏候选会让测试集说谎，必须拦。

所以门禁拦的是「假」而不是「难」；`score_all` 的变化如实写进报告，用作「测试集变难了」的记录。

> 这也是本次与「自动刷分」的分界线：系统不会为了指标好看而拒收难题，
> 也不会为了收录难题而放松一致性校验。

## 5. 回滚机制

- `evolve_merge.py` 在写任何文件前，把整个测试集目录复制到
 `<workdir>/backups/<name>/v<旧版本>-<时间戳>-pre-merge/`；
- `evolve_gate.py --stage after` 判定失败时，读最近一条 merge 记录的 `backup_dir`（缺失则回退
 `manifest.evolution.history[-1].backup_dir`），按字节还原后**复跑评测**；
- 报告的 `restore_verified` 为**字节级**判据（还原后的内容指纹必须等于备份指纹），
 `restore_matches_baseline` 为口径级判据（仅当基线快照与备份同版本才有意义）；
- 两者都通过才算回滚成功，否则退出码 2 并提示人工按 git 恢复。

## 6. 产物与数据契约

工作目录默认 `D:\项目1\evolution`（**仓库外**，可用 `EVOLUTION_WORKDIR` 覆盖）：

| 路径 | 内容 |
| --- | --- |
| `harvest/harvest-<ts>.json` | 信号清单：`signal_id / kind / target_testset / deferred_reason / raw / evidence / input_payload` |
| `review/candidates-<ts>.json` | 候选：`candidate_id / case / expected_source / requires_expected / source_run_id / evidence` |
| `review/decisions-<ts>.json` | 决定：`decision ∈ adopt / reject / skip / rejected_invalid`，含被采纳的 case 与理由 |
| `merges/merge-<ts>.json` | 并入记录：前后版本、条数、指纹、备份路径 |
| `gate/latest-before-<set>.json` | 门禁基线快照（逐 case 判定 + 汇总） |
| `gate/gate-<ts>.json` | 门禁报告：三口径结果、是否回滚、还原校验 |
| `backups/<set>/...` | 并入前整目录备份（回滚来源） |

夹具侧唯一新增的元数据：

```json
"evolution": {
 "history": [{
 "merged_by": "scripts/evolution/evolve_merge.py",
 "parent_version": "1.0", "version": "1.1",
 "added_case_ids": ["json_201"], "source_run_ids": ["…"],
 "decision_file": "…", "backup_dir": "…",
 "cases_before": 200, "cases_after": 201
 }]
}
```

## 7. 复现

```text
:: 1) 采集（真实库只读；或 --from-json 用合成事件）
backend\.venv\Scripts\python.exe scripts/evolution/evolve_harvest.py --from-db --days 30

:: 2) 候选（零 token）
backend\.venv\Scripts\python.exe scripts/evolution/evolve_propose.py --harvest <harvest.json>

:: 3) 人工确认（交互；或 --decisions 非交互）
backend\.venv\Scripts\python.exe scripts/evolution/evolve_review.py --candidates <candidates.json>

:: 4) 门禁基线（必须在并入之前）
backend\.venv\Scripts\python.exe scripts/evolution/evolve_gate.py --testset json_robustness --stage before

:: 5) 并入（唯一会写夹具的一步）
backend\.venv\Scripts\python.exe scripts/evolution/evolve_merge.py --decisions <decisions.json>

:: 6) 门禁判定（不通过自动回滚）
backend\.venv\Scripts\python.exe scripts/evolution/evolve_gate.py --testset json_robustness --stage after

:: 离线自检（合成事件 + 坏候选 + 回滚，零 token）
backend\.venv\Scripts\python.exe scripts/selftest_evolution.py
```

## 8. 合成事件闭环演示（本次实跑）

真实库里几乎没有 LLM 失败样本（81 条 run：61 success + 15 partial + 5 success，0 failed、0 重问；
`notes` 0 条有 `content_check`；只有 3 条 `discarded` 选题），因此闭环用**合成事件**验证——
这只证明**机制正确**，不代表真实进化收益。

| 步骤 | 结果 |
| --- | --- |
| ① 采集 | 6 条合成 run + 1 note + 1 topic → **10 条信号 / 9 类**，其中 5 条无对应冻结集（含 1 条超长 raw 被降级） |
| ② 候选 | 5 条有目标信号 → 去重后 **3 条候选**（json×2 + compliance×1），每条带 `expected_source` |
| ③ 确认 | 非交互 decisions：采纳 1 / 拒绝 1 / 未列出 1（=skip）/ 缺字段 1（=rejected_invalid） |
| ④ 并入 | json_robustness **v1.0 → v1.1，200 → 201 条**，stats 与 sha256 同步重算，备份生成，`history` 落 1 条 |
| ⑤ 门禁 | `legacy_invariant` 200/200 不变、`new_case_consistency` 1.0 → **通过**；`score_all` 0.675 → 0.6766（仅记录） |

另一次演示并入 2 条 json + 1 条 compliance：json_robustness 200→202（0.675→0.6782）、
compliance 128→129（0.75→0.7519），两套门禁均通过、旧样本零变化。

## 9. 门禁被证明有效（负例）

| 负例 | 构造 | 结果 |
| --- | --- | --- |
| **坏候选（语义错）** | `label=repaired` 但 `expected` 写错 | 静态校验放行（字段合法）→ 门禁 `new_case_consistency=0.0` → **拦截、自动回滚**，版本/条数/指纹回到并入前，复跑与基线逐条一致 |
| **非法候选（结构错）** | 缺 `note` 字段 | review 阶段即标 `rejected_invalid`；直接被 merge 拒绝（退出码 1），**夹具字节不变** |
| **陈旧基线** | 两次并入之间不重取基线 | 门禁提示「基线版本 ≠ 备份版本」，改用字节级还原判据，不假装「与基线一致」 |

`selftest_evolution.py` 把上述全部固化为 **63 条断言**（0 token），并在收尾按字节还原两套夹具与两份评测结果。

## 10. 阶段 2 设计（v1 当时的设想；此前已落地前两项，见下方 v2 章节）

| 项 | 设计要点 | 前置依赖 |
| --- | --- | --- |
| ~~`evolution_candidates` 表~~ | **v2 改用工作目录 JSON 状态机**（`evolve_store.py`），仍零迁移；建表仍待单独授权 | 需要 1 支迁移 |
| 前端待审队列 | `/evolution` 页：左列表（按信号类型筛选）+ 右详情（原始输出 diff / 期望 / 证据 / 通过预览）；操作只写候选状态，**不直接改夹具**，并入仍由后端 merge 任务执行 | 需先有表；复用现有 `proxyToBackend` 与 `Page<T>` |
| 知识进化（候选 `hit_pattern`） | 从人工确认的爆款拆解 / 诊断结论生成候选知识块，走同一套「人工确认 → 幂等写入 → 回滚」；写入复用 `find_active_by_hash`（`(chunk_type, meta.content_hash)` 查重，无唯一约束、无需迁移） | 依赖爆款拆解台 / 此前真实数据，当前 `knowledge_snippets` 仅 30 条 synthetic |
| 自动回归调度 | 并入后自动跑全量 19 脚本（当前 `--full-regression` 手动触发） | 需要 ops 侧计划任务支持，属此前范畴 |

## 11. 简历口径

**可写**：

> 设计人在环里的自进化闭环：自动从 `agent_runs` 挖掘失败 / 兜底 / 越权样本 → 规则化生成候选回归 case
> → 人工确认门（采纳/拒绝/改写）→ 版本化并入冻结评测集并重算 sha256 → 评测门禁按三口径拦截退化、
> 失败自动回滚且回滚可验证，形成「失败样本驱动评测集增长」的飞轮。

**必须带的口径**：v1 只用**合成事件**验证机制（真实库当前 0 条 LLM 失败样本），
不得写成「系统越用越准」或「真实收益 X%」。

**不可写**：

- 全自动自进化 / 自动优化 prompt 或模型参数 / 自动改代码；
- 「系统越用越准」「效果持续提升」——没有真实前后对比数据；
- 「完全自主 Agent」——本次所有生效动作都要人工点头；
- 任何未标 n 的进化收益数字。

---

# v2 · 可靠性自进化（新增）

> v1 的飞轮只能收「已经能通过」的样本——真实失败样本按定义过不了 `new_case_consistency`，
> 于是飞轮会自我否定。v2 补三条腿：**known-failure（xfail）通道**、**经验提炼器（reflection）**、
> **变更对照评测（A/B gate）**，让「失败」能被如实记录、被归因、被验证是否真的改好了。

## 12. 七步闭环（v2）

```
① harvest 只读采集 9 类信号
② propose 规则化生成候选 case（零 token）
 reflect 归因 → 五类候选经验（bug_report / prompt_patch / few_shot / rule / hit_pattern）
③ store 候选状态机（pending → approved/rejected/xfail → applied → closed）
④ review 人工确认（采纳 / 采纳为 known_failure / 拒绝 / 改写 / 跳过）
⑤ merge 新增用例并入（版本 +1、重算 sha256）
 mark 给**已有**用例标 xfail（版本 +1、备份、实测后才允许）
⑥ gate 旧样本不变 + 新样本按状态分类判定（不通过自动回滚）
⑦ close xfail 转绿 + 全量回归绿 → 转正为正式回归用例（版本 +1）
```

新增脚本 5 个（全部 CLI，零图、零迁移、零依赖）：`evolve_store.py`、`evolve_reflect.py`、
`evolve_mark.py`、`evolve_close.py`、`evolve_ab.py`。

## 13. known-failure（xfail）通道

用例新增可选字段：

```json
{
 "id": "norm_107",
 "status": "known_failure",
 "xfail": {
 "expected": null, // 期望正确答案（人工标注，不依赖实现）
 "actual": 1, // 当前实际行为（由 evolve_mark 实测填充，人不许手写）
 "root_cause": "_COUNT_PATTERN 用 re.search 前缀匹配……",
 "source_run_id": "fixture:normalize/norm_107",
 "opened_at": "2026-10-18T10:34:35+08:00"
 }
}
```

三条硬规则：

1. **标记前必须实测**：`evolve_mark` 先跑评测脚本，确认该用例当前确实不通过；
 当前通过的用例一律拒绝标 xfail（把正常说成异常，和把 bug 藏起来一样是失真）。
2. **门禁分两类**：与实现一致的「回归锁定」必须 1.0；与实现不一致但人工确认期望正确的
 「known_failure」标红单列、**不阻断、绝不计为通过**。报告同时给
 `passing_ok/passing_total` 与 `known_failure 仍红 N/M`。
3. **降级要显式**：把既有 passing 用例改成 known_failure 属于降级，门禁默认拦截，
 必须 `--allow-xfail-downgrade` 才放行，并在报告里记 `legacy_downgraded`。

转正（`evolve_close.py`）三个条件缺一不可：该用例实测通过、全量离线回归绿、版本 +1 且留痕。
xfail 一旦转正就变成回归锁定用例，此后任何退化都会被门禁拦下。

### 真实案例（本次实跑，非合成）

`norm_107` 的 `raw="1.2.3"`、`expected=null`，实测 `actual=1`：

| 步骤 | 结果 |
| --- | --- |
| 取基线（v1.0） | n=108，accuracy=0.9907，passing=108 |
| `evolve_mark` 标记 | 实测不通过 → **v1.0 → v1.1**（条数仍 108），xfail 块落库，备份生成 |
| 门禁 after（不带参数） | **拦截**：`legacy_downgraded=1`，退出码 2，按备份回滚，复跑与基线逐条一致 |
| 再次标记 + `--allow-xfail-downgrade` | **通过**：`passing 107/107`、`known_failure 仍红 1/1`、`score_all` 0.9907 不变 |

「回滚」不是口头承诺：回滚后字节级指纹等于备份，复跑 n 与 accuracy 与基线一致。

## 14. 经验提炼器（reflection）

默认**纯规则、零 token**，输入是 harvest 清单（可选叠加既有评测结果），输出五类候选：

| 规则（触发条件） | 类型 | 建议方向 |
| --- | --- | --- |
| `json_unrecoverable` ≥ min_count | `rule` | 补 failed 用例，先锁行为 |
| 输出截断（括号/引号未闭合）≥ min_count | `prompt_patch` | 长字段后置 / 查 max_tokens |
| 兜底「平衡扫描」救回 ≥ min_count | `few_shot` | 补「只输出 JSON」示例 |
| `json_retry` ≥ min_count | `few_shot` | 同上，压 attempts |
| 白名单外工具请求 | `prompt_patch` | 强化工具白名单指令 |
| 同一工具报错 ≥ min_count | `bug_report` | 复现 + 单测，先 xfail 追踪 |
| 同一违禁词命中 ≥ min_count | `rule` | 人工复核语境，不直接删词 |
| 人工废弃选题 ≥ 3 | `hit_pattern` | **只生成不应用**（等内容数据） |
| 工具选择精确率 < 0.6（既有评测） | `prompt_patch` | 「只调真正需要的工具」 |

每条候选带 `source_run_ids / evidence / root_cause_hypothesis / suggestion / impact /
confidence / expected_source / state=pending`，**不自动生效**。

`--use-llm` 才做语义归纳：先 `--estimate-only` 打印调用次数与 token 区间（900 in + 260 out / 条），
`--yes` 才发请求；单条失败降级回规则文案（`llm_fallback`），不阻断整批。
本次**未使用** LLM 归纳（真实信号只有 18 条，规则足够）。

## 15. 变更对照评测门禁（A/B gate）

只对 `prompt_patch` / `few_shot`：**不改生产代码**，运行时把补丁追加到该图 system 消息末尾。

```python
import app.agent.topics.graph as g
g.build_messages = wrapper # 必须替换 graph 模块上的引用（它 import 时已绑定函数对象）
```

替换 `prompts.build_messages` 无效——这是本次核查确认的绑定事实。

两个目标：

| target | 说明 | token |
| --- | --- | --- |
| `synthetic` | 真实 `build_messages` + 确定性假模型（8 条），验证注入/双跑/diff/判定 | **0** |
| `topic_tool` | 真实 LLM，12 个冻结场景 × 2 模式 × 2 轮 | 约 137k |

预置门槛（写死，执行后不许按结果改）：主指标提升 ≥ 0.05 且次要指标不降才 approved；
无变化或退化一律 rejected 并留档。本次只跑了 `synthetic`（零 token），结论：
好补丁 improved/approved、坏补丁 degraded/rejected、无关补丁 no_change/rejected。

> `topic_tool` 真实 A/B 路径已实现但**未执行**（约 137k token、20 分钟），需单独授权。

## 16. 内容质量自进化（阶段 4，**只设计不实现**）

| 项 | 设计 | 硬前置 |
| --- | --- | --- |
| 数据来源 | `note_metrics`（真实表现）+ 人工「采用/编辑」留痕 + 爆款拆解台产出的 `hit_pattern` | 此前端到端实跑 + **≥20–30 篇真实/对标样本** |
| 闭环 | 拆解 → 候选 `hit_pattern` → 人审 → 幂等写入 `knowledge_snippets` → 创作 RAG 反哺 → 再观测 | `find_active_by_hash` 已可承接，无需迁移 |
| 指标 | 采用率、编辑距离、发布后互动中位数（分层对比） | 样本量不足时只报 n，不报率 |
| 边界 | 只能做**相关**，不能声称因果；不能按单篇表现调 prompt；不能自动发布 | 任何「提升」都要有对照与 n |

触发条件写死：`published notes ≥ 20` 且 `note_metrics 覆盖 ≥ 20 篇` 才允许产出第一版候选；
当前真实库 `notes=1 / note_metrics=3`，**不满足**，因此本阶段只留设计。

## 17. 简历口径（v2）

**可写**：

> 把失败样本飞轮升级为可靠性自进化：为冻结评测集引入 known-failure 通道（标记前实测、
> 门禁单列标红不阻断、修复后转正需全量回归绿）、规则化经验提炼（五类候选、状态机、
> 零自动生效）与运行时注入的 A/B 对照门禁（预置门槛、无提升即否决并留档）。

**必须带**：xfail 真实案例只有 1 条（norm_107，合成输入的确定性缺陷）；A/B 真实 LLM 对照未执行；
内容质量层无数据。

**不可写**：全自动自进化 / 自动改 prompt 或代码 / 「系统越用越准」/ 任何未标 n 的提升数字。
