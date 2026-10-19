# xhs-agent · 度量报告（benchmark 套件）

> 生成时间：2026-10-17T09:45:38+08:00｜生成器：`scripts/benchmark/build_report.py`（重跑可复现）
>
> **原则**：全部数字来自本机实跑；每条指标含 name / value / unit / n / category / method / 
> env / timestamp / log_path / confidence；跑不出的如实写 N/A 并说明原因；**不删离群值**；
> 本报告不含任何编造或美化的数字；token 按量记录，**不内置价格表**。

**四类口径**（每条指标的 `category` 字段）：`offline_objective` 离线客观值（确定性、可复跑）｜
`llm_small_sample` 真实 LLM 小样本抽样（必须带 n，不得外推）｜`synthetic_fixture` 合成夹具结果
（仅验证管线/正确性）｜`not_measured` 尚未测 / 跑不出（N/A）。

## 0. 复现方法（一条命令 or 逐项脚本）

```powershell
# 阶段一全量（零 token；含服务启停与报告生成）
backend\.venv\Scripts\python.exe scripts\benchmark\run_all_offline.py

# 单项（每类一个，可单独运行）
backend\.venv\Scripts\python.exe scripts\benchmark\bench_regression_scale.py # 回归规模（离线 17 脚本）
backend\.venv\Scripts\python.exe scripts\benchmark\bench_regression_scale.py --smoke-only # 在线冒烟（需服务）
backend\.venv\Scripts\python.exe scripts\benchmark\bench_reliability.py # 故障注入与可靠性
backend\.venv\Scripts\python.exe scripts\benchmark\bench_rag_retrieval.py # RAG 合成集（bge 就绪则实跑）
backend\.venv\Scripts\python.exe scripts\benchmark\bench_api_latency.py # 本地只读接口时延（需服务）
backend\.venv\Scripts\python.exe scripts\benchmark\build_report.py # 重新生成本报告

# 阶段二（真实 LLM；默认零请求，由用户本机执行）
backend\.venv\Scripts\python.exe scripts\benchmark\bench_llm_stability.py --dry-run # 安全预检（不发请求）
backend\.venv\Scripts\python.exe scripts\benchmark\bench_llm_stability.py # 7 图 × n=5（交互确认）
backend\.venv\Scripts\python.exe scripts\benchmark\bench_vision_synth.py --generate-fixtures
backend\.venv\Scripts\python.exe scripts\benchmark\bench_vision_synth.py # 合成档识别
backend\.venv\Scripts\python.exe scripts\benchmark\bench_vision_real.py --init # 真实档模板
```

结果目录：`docs/benchmark/results/`｜原始日志：`docs/benchmark/logs/`｜夹具：`docs/benchmark/fixtures/`

**本次编排清单**（`results/run_manifest.json`）：

- regression_scale: rc=1
- reliability: rc=1
- rag: rc=0
- services: rc=0（latency=0, smoke=0）
- testset:json_robustness: rc=0
- testset:compliance: rc=0
- testset:normalize: rc=0
- testset:rag_golden: rc=0
- report: rc=0

## 1. 回归规模（最新实测值：2026-10-18 此前后 · `run_selftests.py` 单次聚合）

> 一条命令 `scripts/run_selftests.py --group all --allow-destructive` 串行跑完 21 个脚本
> （20 个 `selftest_*.py` + `verify_schema.py`），逐脚本按 `[PASS]` / `[FAIL]` / `[SKIP]` 计数。
> 机器可读结果：`docs/benchmark/results/regression_run.json`（逐脚本 rc/PASS/FAIL/SKIP/用时 + 合计）。

| # | 脚本 | 退出码 | 通过 | 失败 | 跳过 | 用时(s) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `selftest_agent_harness.py` | 0 | 63 | 0 | 0 | 8.9 |
| 2 | `selftest_api.py` | 0 | 37 | 0 | 0 | 3.8 |
| 3 | `selftest_content_agent.py` | 0 | 98 | 0 | 0 | 23.2 |
| 4 | `selftest_content_variants.py` | 0 | 146 | 0 | 0 | 27.0 |
| 5 | `selftest_cover.py` | 0 | 79 | 0 | 0 | 6.6 |
| 6 | `selftest_dashboard.py` | 0 | 176 | 0 | 0 | 7.2 |
| 7 | `selftest_db.py` | 0 | 15 | 0 | 0 | 1.2 |
| 8 | `selftest_diagnostics.py` | 0 | 156 | 0 | 0 | 7.1 |
| 9 | `selftest_evolution.py` | 0 | 63 | 0 | 0 | 13.9 |
| 10 | `selftest_evolution_v2.py` | 0 | 79 | 0 | 0 | 43.8 |
| 11 | `selftest_hotspot.py` | 0 | 54 | 0 | 0 | 4.4 |
| 12 | `selftest_knowledge_admin.py` | 0 | 158 | 0 | 1 | 7.6 |
| 13 | `selftest_metrics.py` | 0 | 139 | 0 | 0 | 7.8 |
| 14 | `selftest_ops.py` | 0 | 130 | 0 | 0 | 159.6 |
| 15 | `selftest_rag.py` | 0 | 161 | 0 | 0 | 7.6 |
| 16 | `selftest_rag_fusion.py` | 0 | 26 | 0 | 0 | 0.8 |
| 17 | `selftest_search.py` | 0 | 94 | 0 | 0 | 4.2 |
| 18 | `selftest_studio.py` | 0 | 86 | 0 | 0 | 7.1 |
| 19 | `selftest_topic_agent.py` | 0 | 42 | 0 | 0 | 23.8 |
| 20 | `selftest_topic_tool_calling.py` | 0 | 29 | 0 | 0 | 2.9 |
| 21 | `verify_schema.py` | 0 | 13 | 0 | 0 | 1.0 |

**合计**：脚本 **21** 个 ｜ 运行时用例 **1845**（通过 **1844** / 失败 **0** / 跳过 **1**）｜ 退出码全 **0**。

> 口径：`PASS` 只计 `[PASS]`；`SKIP` 单列，**不计通过、也不抵减失败**。当前仅剩 1 条 SKIP：
> `selftest_knowledge_admin` 的「扫描器自指豁免」（逻辑豁免，代码内已注明不代表通过）。
> 此前把 6 条环境探针式 SKIP 改为 monkeypatch 确定性断言：**1833/0/7（运行时 1840）→ 1844/0/1（运行时 1845）**，
> 逐分支变化见 §7.8；其余脚本 PASS 数不变。
> 与 2026-10-17 的 17 脚本快照（1642）不是同一口径，不可直接相加；历史差异与本次变化见 §7.7 / §7.8。
> 历史时点值「1116 项（截至当时前）」已被逐脚本实测取代。

**在线冒烟（真实网络 + 真实库；services 阶段）**

| 脚本 | 退出码 | 通过 | 失败 | 用时(s) | 原始日志 |
| --- | --- | --- | --- | --- | --- |
| `smoke_crud_api.py` | 0 | 37 | 0 | 2.2 | `docs\benchmark\logs\reg_smoke_crud_api.txt` |
| `smoke_hotspot.py` | 0 | 38 | 0 | 10.52 | `docs\benchmark\logs\reg_smoke_hotspot.txt` |

## 2. 故障注入与可靠性（隔离环境实跑）

| 指标 | 值 | 单位 | n | 口径摘要 |
| --- | --- | --- | --- | --- |
| `reliability.health_ready_db_down.status_code` | 503 | HTTP 状态码 | 10 | 响应体 checks.database=false、checks.pgvector=false；redis 仍为真实实例 |
| `reliability.health_ready_db_down.latency_p50_ms` | 3016.5 | 毫秒 | 10 | 同上；nearest-rank 百分位 |
| `reliability.health_ready_db_down.latency_p95_ms` | 3037.9 | 毫秒 | 10 | 同上；nearest-rank 百分位 |
| `reliability.db_down_boot_ok` | 1 | 布尔(1=是) | 1 | 已验证「初始化失败不阻断启动」的设计在 DB 不可达时成立 |
| `reliability.hotspot.404_attempts_per_call` | 1,1,1 | 次请求（逐次） | 3 | scripts/benchmark/bench_reliability.py：本机回环 mock 上游（127.0.0.1 随机端口）注入故障，统计 mock 实际接收的请求次数与墙钟耗时 |
| `reliability.hotspot.5xx_attempts_per_call` | 3,3,3 | 次请求（逐次） | 3 | max_attempts=3 ⇒ 最多 3 次尝试（含首次；重试 2 次），退避 1s+2s |
| `reliability.hotspot.5xx_backoff_total_p50_ms` | 3006.8 | 毫秒 | 3 | 含 mock 即时应答开销；退避为主（≈3000ms） |
| `reliability.search.404_attempts_per_call` | 1,1,1 | 次请求（逐次） | 3 | scripts/benchmark/bench_reliability.py：本机回环 mock 上游（127.0.0.1 随机端口）注入故障，统计 mock 实际接收的请求次数与墙钟耗时 |
| `reliability.search.5xx_attempts_per_call` | 3,3,3 | 次请求（逐次） | 3 | scripts/benchmark/bench_reliability.py：本机回环 mock 上游（127.0.0.1 随机端口）注入故障，统计 mock 实际接收的请求次数与墙钟耗时 |
| `reliability.search.5xx_backoff_total_p50_ms` | 3009.8 | 毫秒 | 3 | scripts/benchmark/bench_reliability.py：本机回环 mock 上游（127.0.0.1 随机端口）注入故障，统计 mock 实际接收的请求次数与墙钟耗时 |
| `reliability.hotspot.partial_status` | partial | 状态 | 1 | 2 坏源（1×500、1×404）+ 2 好源 |
| `reliability.hotspot.partial_new_rows` | 5 | 条 | 1 | scripts/benchmark/bench_reliability.py 子进程：临时库（alembic 迁移）+ mock DailyHotApi，真实调用 collect_and_store 全链路（落库 + … |
| `reliability.hotspot.partial_failed_sources` | 2 | 个 | 1 | scripts/benchmark/bench_reliability.py 子进程：临时库（alembic 迁移）+ mock DailyHotApi，真实调用 collect_and_store 全链路（落库 + … |
| `reliability.hotspot.all_fail_status` | failed | 状态 | 1 | scripts/benchmark/bench_reliability.py 子进程：临时库（alembic 迁移）+ mock DailyHotApi，真实调用 collect_and_store 全链路（落库 + … |
| `reliability.hotspot.all_fail_new_rows` | 0 | 条 | 1 | 全失败零落库（无脏数据） |
| `reliability.hotspot.lock_second_call` | FetchInProgress | 异常类名 | 1 | 进行中再次手动触发被拒；定时触发安静跳过；mock 请求数=源数（无重复采集） |

断言：**33 通过 / 0 失败**；mock 上游为本机回环 HTTP（故障为程序注入），临时库已删除并全局兜底复查。

- DB 不可达 503 时延逐样本（ms，n=10，未删离群值）：3023.3, 3007.5, 3015.0, 3016.5, 3016.5, 3018.7, 3026.8, 3031.7, 3012.0, 3037.9
- 该场景响应体 checks：`{"database": false, "pgvector": false, "redis": true}`

## 3. RAG 检索合成集（临时库 + pgvector）

| 指标 | 值 | 单位 | n | 口径摘要 |
| --- | --- | --- | --- | --- |
| `rag.fixture.snippets_total` | 12 | 条 | 12 | 合成夹具、仅验证检索正确性；不代表真实语料质量 |
| `rag.fixture.queries_total` | 12 | 条 | 12 | 合成夹具、仅验证检索正确性 |
| `rag.pgvector.extversion` | n/a | 版本号 | 1 | 向量扩展可用性事实；与检索质量指标相互独立 |
| `rag.mechanics.self_check_ok` | 1 | 布尔(1=是) | 1 | 构造性假向量以验证机制接线；**不构成任何检索质量结论** |
| `rag.vector_dim_measured` | 1024 | 维 | 12 | 实测向量维度（本地 bge）；12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.recall_at_1` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.recall_at_3` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.recall_at_5` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.mrr` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.index_build_ms` | 6.5 | 毫秒 | 12 | HNSW 余弦索引在 12 行上的重建耗时；小样本工程参考 |
| `rag.query_latency_end_to_end_p50_ms` | 6.04 | 毫秒 | 24 | 端到端单查询 P50（含编码，n=24）；本机环境，仅工程参考 |
| `rag.query_latency_end_to_end_p95_ms` | 7.37 | 毫秒 | 24 | 端到端单查询 P95；本机环境，仅工程参考 |
| `rag.query_latency_vector_only_p50_ms` | 5.98 | 毫秒 | 24 | 纯向量检索 P50（不含编码）；仅工程参考 |
| `rag.query_latency_vector_only_p95_ms` | 8.06 | 毫秒 | 24 | 纯向量检索 P95；仅工程参考 |

**如实说明**：检索质量指标未产出 —— 本地 bge 依赖/模型未就绪：{"provider": "local", "reason": "本地向量化依赖未安装（langchain-huggingface / sentence-transformers）", "hint": "本地向量化依赖未安装或不可用：请先安装 requirements.txt 中的 langchain-huggingface 与 sentence-transformers（pip install -r …

补跑命令（用户侧；需先安装依赖与模型）：

```powershell
pip install langchain-huggingface sentence-transformers # 于 backend\.venv 内
set HF_ENDPOINT=https://hf-mirror.com
backend\.venv\Scripts\python.exe scripts\check_embedding_local.py
backend\.venv\Scripts\python.exe scripts\benchmark\bench_rag_retrieval.py
```

## 4. 本地性能（localhost 单机，非生产）

| 接口 | 说明 | n | P50 (ms) | P95 (ms) | max (ms) | 非200 |
| --- | --- | --- | --- | --- | --- | --- |
| `/health` | 后端存活探针（基线，不查库/缓存） | 20 | 1.91 | 21.53 | 26.89 | 0 |
| `/api/v1/hotspots` | 热点列表（当日） | 20 | 6.92 | 30.84 | 31.06 | 0 |
| `/api/v1/hotspots/sources` | 热点来源快照 | 20 | 5.54 | 25.4 | 29.55 | 0 |
| `/api/v1/topics` | 选题列表（分页） | 20 | 7.57 | 31.09 | 32.51 | 0 |
| `/api/v1/notes` | 笔记列表（分页） | 20 | 6.97 | 31.09 | 31.7 | 0 |
| `/api/v1/accounts` | 账号列表 | 20 | 6.54 | 28.18 | 31.18 | 0 |
| `/api/v1/personas` | 人设列表 | 20 | 6.81 | 23.5 | 31.23 | 0 |
| `/api/v1/dashboard/summary` | 看板·汇总 | 20 | 10.81 | 22.77 | 46.82 | 0 |
| `/api/v1/dashboard/trend` | 看板·趋势（metric=likes，30 天） | 20 | 6.87 | 30.66 | 31.02 | 0 |
| `/api/v1/dashboard/ranking` | 看板·排行（metric=likes，前 20） | 20 | 6.63 | 31.22 | 31.39 | 0 |
| `/api/v1/reports/weekly` | 周报列表 | 20 | 5.43 | 25.28 | 30.26 | 0 |
| `/api/v1/knowledge` | 知识块列表（分页） | 20 | 6.35 | 29.33 | 31.85 | 0 |
| `/api/v1/metrics` | 数据时间线（动态取 1 个 note_id） | 20 | 9.15 | 31.21 | 31.79 | 0 |

> 口径：localhost 单机、串行单并发、warmup 3 次不计入、n=20/接口；含 HTTP 客户端（urllib）开销；
> **不得外推为线上性能 / 承载能力**；逐样本原值（含离群值）在 `results/api_latency.json` 中留档。

## 5. 测试集评测（带 ground truth）

> 夹具在**评测前冻结**（`fixtures/testsets/`，sha256 指纹记录于各 manifest.json；评测脚本读取时校验，任何改动即报错）；全部答案人工构造、**独立于被测实现**；
> 结论口径：确定性算法（offline_objective）；逐样本明细在 `results/testsets/*.json` 可审计；未做任何离群值剔除、未调参刷分。

### 5.1 JSON 兜底解析器鲁棒性（真实 invoke_json 管道，零 token）

| 指标 | 值 | n | 口径摘要 |
| --- | --- | --- | --- |
| 整体准确率 | 0.675 | 200 | label 符合且（成功时）内容与期望一致 |
| direct 一次成功率 | 1.0 | 25 | 合法 JSON 被一次解析且内容一致 |
| repaired 救回率 | 0.5413 | 109 | 需兜底样本被救回且内容一致（该救回的救回率） |
| failed 正确拒绝率 | 0.7727 | 66 | 不可修复样本未被硬解析 |

**三类匹配矩阵**（期望 label × 实际结果）：

| 期望 \ 实际 | direct | repaired | failed | error |
| --- | --- | --- | --- | --- |
| direct | 25 | 0 | 0 | 0 |
| repaired | 0 | 77 | 32 | 0 |
| failed | 0 | 15 | 51 | 0 |

错例构成（逐条明细可查）：**未救回 32**（尾随逗号 / 引号变体为当前能力边界）· **降级提取 18**（顶层结构未恢复、返回内嵌子结构）· **过度解析 15**（从损坏文本返回首段可解析片段）· **内容不符 0**；路径：`results/testsets/json_robustness.json`（detail.mismatches）。

### 5.2 违禁词/合规扫描（真实 scan_note；内置词表口径）

混淆矩阵：**TP=46 / FP=14 / FN=18 / TN=50**（n=128，正例 64 应命中 + 负例 64 期望不命中）

| 指标 | 值 | n | 口径摘要 |
| --- | --- | --- | --- |
| Accuracy | 0.75 | 128 | (TP+TN)/n |
| Precision | 0.7667 | 60 | TP/(TP+FP)；**夹具含对抗变体，须带此口径引用** |
| Recall | 0.7188 | 64 | TP/(TP+FN) |
| F1 | 0.742 | 128 | 2PR/(P+R) |
| 误报率 | 0.2188 | 64 | FP/负例（易误伤语境全部命中，属字面匹配设计特征） |
| 漏报率 | 0.2812 | 64 | FN/正例（谐音/异体变体为主要漏报来源） |

分组表现（命中数/样本数）：易误伤·字面 14/14（期望 0）、易误伤·敏感字 0/14（期望 0）、干净负例 0/36（期望 0）、自然语境 20/20、大小写变体 5/10、分隔符/空格 14/14、谐音/词表外 7/20。全部 FP/FN 明细：`results/testsets/compliance.json`。

### 5.3 视觉数值归一化（真实 _parse_count）

| 指标 | 值 | n | 口径摘要 |
| --- | --- | --- | --- |
| 总正确率 | 0.9907 | 108 | （数值正确 + 正确拒绝）/ 总数（**含 1 条 known_failure**） |
| 数值样本解析正确率 | 1.0 | 91 | 与人工标注完全一致 |
| None 样本正确拒绝率 | 0.9412 | 17 | 空对空不判命中、单独统计 |
| passing / known_failure | **107 / 1** | 108 | v1.1 起按状态分列；xfail 不计通过、单独标红 |

> 评测发现并修复 1 处缺陷：「－5」（全角减号负值）原误得 5 → 修复后按负数无效返回 None（修复前 106/108 → 修复后 107/108；痕迹见 `results/testsets/normalize.json` 与 git 等价记录）。
>
> 此前把仅剩的 `norm_107`（`"1.2.3"` 被前缀匹配成 1）登记为 **known_failure**（normalize v1.0 → v1.1，条数不变）：
> 根因是 `_COUNT_PATTERN` 用 `re.search` 做前缀匹配、未校验后续字符；**未修改实现**，只标红追踪，
> 修复需单独授权，修复后由 `evolve_close.py` 在全量回归绿时转正。因此本节应读作
> 「passing 107/107 + known_failure 1（不计通过）」，而不是「99.07% 的系统准确率」。

### 5.4 RAG 检索黄金集（30 片段 / 40 查询；含 hard negatives）

**已实跑**（2026-10-17）：本地 `bge-large-zh-v1.5`（1024 维，CPU）+ 临时库 pgvector HNSW
（m=16 / ef_construction=64 / `vector_cosine_ops`，`ef_search=40`）。**同一次运行**内采集
「纯向量 / 混合」两路，共用同一批语料向量与同一索引；两路都走生产同一条 SQL 路径
（`crud_knowledge.search_snippets`），不用 Python 替身重算相似度。相关性为**二值**（`expected_ids`
是集合，无分级），nDCG 用 gain=1/0、IDCG 按 `min(|期望|,5)` 计，未编造分级。

| 指标 | 纯向量（before） | 混合 RRF（after） | 差值 | n |
| --- | --- | --- | --- | --- |
| Recall@1 | **0.8625** | 0.7625 | −0.1000 | 40 |
| Recall@3 | 0.9750 | 0.9750 | 0 | 40 |
| Recall@5 | **0.9875** | 0.9750 | −0.0125 | 40 |
| MRR@5 | **0.9875** | 0.9125 | −0.0750 | 40 |
| nDCG@5 | **0.9789** | 0.9247 | −0.0542 | 40 |
| 全命中率@5 | 0.9750 | 0.9500 | −0.0250 | 40 |
| HNSW 索引重建 | 6.1 ms | 6.1 ms（两路共用） | — | 1 |
| 查询 P50 / P95 | 5.81 / 7.88 ms | 14.43 / 17.63 ms | +8.6 / +9.8 ms | 40 |

**决策：默认保持 `vector`；hybrid 代码保留但默认关闭**（`RAG_RETRIEVAL_MODE=vector`，
两路切换、降级、回滚都已具备）。

理由（逐查询，不挑不删，明细见 `results/testsets/rag_golden_hybrid.json` 的 `detail.comparison`）：

- 40 条查询里 **0 条被关键词路救回、5 条排名下降、1 条召回丢失**（`ragq_36` 的第二个期望片段被挤出 top-5）；
- 典型失败：`ragq_14「每天待办列多少件合适」` 向量第 1 命中 `kb_14`，融合后第 1 变成 `kb_25`（记账分类），
 正确片段掉到第 3；`ragq_37「账号安全最该先做哪两件事」` 正确片段从第 1 掉到第 3，被 `kb_29/kb_14` 顶掉；
- 根因两条：
 1. **pg_trgm 没有 IDF**。它只算三元组覆盖率与词频，"多少 / 什么 / 怎么 / 要" 这类高频词对所有文档
 同等加分，泛化片段（记账、复盘、效率方法）被顶到头部。诊断：关键词路**单独**跑 Recall@5 仅 0.6375、
 MRR@5 0.6625（n=40），与向量的 0.9875 / 0.9875 差了一个量级；
 2. **天花板效应**。纯向量 Recall@5 已是 0.9875（40 条只剩 1 条未命中），等权 RRF 用 `1/(60+rank)`
 把强路优势稀释成"两路各占一半"，弱路噪声直接改写头部。
- 代价：延迟从 5.81/7.88 ms 升到 14.43/17.63 ms（多一次全表 trigram 计算 + 融合），换来的是负收益。

**结论的有效边界**：n=40，单条查询命中变化约 2.5pp；语料 30 条全 synthetic、句子短（对 trigram 友好），
**不能外推到真实语料**，也不能据此断言"混合检索普遍无效"。若要继续，应先扩语料到 200+ 条、
换带 IDF 的关键词方案（BM25 / `tsvector`）或加 rerank，而不是在这套 30 条语料上调 RRF 权重刷分。

**可复现性（评测过程中发现并修复）**：首轮插入用 `gen_random_uuid`，两次运行 hybrid 指标不一致
（Recall@1 0.7625 vs 0.7375）——根因是关键词路同分时按 `id DESC` 兜底排序，而 UUID 每次不同。
改为按语料 id 派生确定性 UUID（`uuid5`）后复跑两次，**两路指标与逐查询排名逐位一致**。

**本节简历判定（详版见 §8 / §9）**：

- **可写**：「RAG 检索黄金集实测（40 查询，二值相关性）：纯向量 Recall@5 0.9875 / MRR@5 0.9875 / nDCG@5 0.9789；
 混合检索（向量+pg_trgm+RRF）对照 n=40 无增益（0 救回 / 5 排名下降），据证据保持默认纯向量」；
- **须带口径**：先声明 30 条 synthetic + n=40 + 单条≈2.5pp，再给数字；负结果本身是加分项（有决策、有回滚）；
- **不建议单独写**：`trgm 单独 Recall@5 0.6375`（诊断数字，非产品指标）；P50/P95（本机小样本，仅工程参考）；
 任何"混合检索提升 X%"（本实验方向相反）；把 pg_trgm 称作 BM25（它没有 IDF 与词频饱和）。

### 5.5 可靠性自进化 v2（元评测：门禁本身被验证）

此前给飞轮补了 known-failure 通道与 A/B 门禁。这里记录的不是模型指标，而是**门禁是否拦得住**：

| 验证项 | 结果 | 性质 |
| --- | --- | --- |
| 真实错例标 xfail（norm_107） | normalize v1.0 → v1.1；`passing 107/107`、`known_failure 仍红 1/1` | **真实**（归一化确定性缺陷） |
| 降级未显式允许 | 门禁拦截、按备份回滚、字节级与复跑双校验一致 | **真实** |
| expected 写错的候选 | `new_case_consistency=0` → 拦截 + 回滚 | 合成注入 |
| 标成 xfail 但实际通过 | `xfail_green` 命中 → 拦截（标错即假） | 合成注入 |
| 未修复先转正 | 拒绝（"仍然不通过"） | 真实 |
| 回归未绿先转正 | 拒绝（"全量回归存在失败项"） | 注入 |
| A/B：好 / 坏 / 无关补丁 | improved-approved / degraded-rejected / no_change-rejected | 确定性假模型（零 token） |

**未执行**：真实 LLM 的 prompt A/B（`topic_tool` 12 场景，约 137k token）、`_parse_count` 修复与转正。
这两项都需要单独授权，报告不得把"机制已验证"写成"效果已提升"。

**§5 整体简历判定（5.1–5.5 合并）**：

- **可写**：视觉数值归一化 99.1%（n=108，修复缺陷后；**须同时说明 107 passing + 1 known_failure**）；
 「构建 sha256 冻结评测集（436 条 + RAG 黄金集 30 片段/40 查询），
 全部指标可逐样本审计」的评测方法论；RAG 纯向量基线 Recall@5 0.9875 / nDCG@5 0.9789（n=40，须带 synthetic 口径）；
- **须带口径**：JSON 解析 repaired 救回率（54.1%，n=109）、合规原始 Precision/Recall（76.7%/71.9%，含对抗变体）、
 hybrid 对照的负结果（n=40，0 救回 / 5 下降）；
- **不建议单独写**：降级提取/过度解析条数（行为特征，非质量结论）；trgm 单独指标；任何未带 n 的比率。

## 6. 真实 LLM 小样本（n=5 × 7 图，已执行）

**执行记录（2026-10-17 13:02–13:07）**：`bench_llm_stability.py --n 5 --yes`，模型 `deepseek-flash`
（OpenAI 兼容端点 `https://api.deepseek.com`，温度 0.2）。**35 轮 0 失败**；其中 **30 轮有真实模型调用**，
`insights` 5 轮因 `published=1 < INSIGHTS_MIN_PUBLISHED=3` 走样本不足分支（零调用零 token）。

| 图 | 轮数 | 有调用 | 一次成功率（attempts=1） | raw 原文直解 | 字段契约通过 | raw 缺失 | token 合计 | 延迟 P50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 选题 topics | 5 | 5 | 1.0 | 1.0 (5/5) | 1.0 (5/5) | 0 | 5,457 | 3,712 ms |
| 创作 content | 5 | 5 | 1.0 | 1.0 (5/5) | 1.0 (5/5) | 0 | 4,436 | 4,034 ms |
| 多版本 content_variants | 5 | 5 | 1.0 | 1.0 (5/5) | 1.0 (5/5) | 0 | 8,769 | 6,793 ms |
| 指标识别 metric_vision（视觉） | 5 | 5 | 1.0 | 0.8 (4/5) | 0.8 (4/5) | 0 | 11,260 | 2,221 ms |
| 诊断 diagnosis | 5 | 5 | 1.0 | 1.0 (5/5) | 1.0 (5/5) | 0 | 23,826 | 20,532 ms |
| 周报 weekly | 5 | 5 | 1.0 | 1.0 (5/5) | 1.0 (5/5) | 0 | 31,214 | 19,059 ms |
| 洞察 insights | 5 | **0** | —（零调用） | — | — | 0 | 0 | — |
| **合计** | **35** | **30** | **1.0（n=30）** | **0.9667（29/30）** | **0.9667（29/30）** | **0** | **84,962** | 5,290 ms |

**三个必须分开报的口径**：

1. `first_try_rate = 1.0`：attempts==1 且 status=success 的**轮次占比**，不是 raw 合法率；
2. `raw_direct_parse_rate = 0.9667`（29/30，missing=0）：把 `agent_runs.llm_raw` 用
 `extract_json_with_source` 离线复算，命中方式为「原文」的占比——**这才是「模型一次吐对」**；
 唯一 1 条走了「平衡扫描（对象）」才解析成功，与 attempts 口径的 1.0 形成对照；
3. `contract_pass_rate = 0.9667`（29/30）：原始 JSON 再按**各图自己的** Pydantic 契约 /
 `normalize_*` 纯函数做字段级校验。**与 success_rate 不等价**（success 1.0 vs 契约 0.9667）：
 视觉图有 1 条虽然最终 status=success，但六个指标一个都没出现，被图内归一化「宽容降级」成全部缺失；
 这正是契约口径要单独度量的原因。

**分母纪律**：`llm_called` 用证据判定（`json_parse_attempts>0` 或 `total_tokens>0`）。
insights 每轮虽建了 1 条 run 行，但零 attempts、零 token，按零调用处理，**不计入任何率的分母**；
`raw_missing` 单列（本次 0），raw 类指标分母 = 可得样本数，绝不静默缩小分母。

**已修复的两处脚本缺陷（2026-10-17）**：

1. **结果落盘在清理之后** → 旧版 `cleanup` 在 `finally` 里抛外键冲突，异常冒泡导致结果文件根本没写出
 （首轮 rc=1）。现改为「先写结果 + fsync → 再清理 → 二次落盘补 cleanup 明细」，清理异常单独记
 `detail.cleanup.cleanup_error`，不再吞结果。
2. **清理按标题前缀删笔记** → content / content_variants 的笔记标题由模型生成、不带 `[BENCH-LLM]` 前缀，
 删 `accounts` 时被 `notes.account_id`（ON DELETE RESTRICT）挡住。现改为**按 `account_id` 关联**、
 按外键依赖顺序删除（diagnostics → note_metrics → notes → topics → personas → hotspot → account），
 删前计数、删后「按账号 + 按前缀」双口径复查。本次删除 notes 21 / topics 15 / diagnostics 5 /
 metrics 2 / persona 1 / hotspot 1 / account 1，双口径残留均为 0。

**版本留档**：修复前由日志 + `llm_raw` 捕获重建的那份结果已归档为
`results/llm_stability.rebuild-20261017.json`（raw 96.55%，n=29）；另有聚合口径修正前的首轮真实运行
`results/llm_stability.run1-20261017.json`（把 insights 的零调用 run 误计入分母，已废弃）。
**对外引用的只有 `results/llm_stability.json`。**

**口径与边界**：
- n=5/图、总 n=30，属小样本抽样，**禁止外推为总体稳定性**；
- 视觉图 5 轮全部成功（模型即 `LLM_VISION_MODEL` 回退的 `deepseek-flash`），但**只验证了管线、格式与字段契约**，
 合成截图的字段级准确率另见 `bench_vision_*`（未执行）；
- 延迟含网络与本机 CPU 编码，仅工程参考，不进简历；
- token 随输出长度波动（多版本 8.8k~11.8k、周报 31k~39k），不作为成本结论。

- `bench_vision_synth.py`：合成截图夹具已生成（`fixtures/vision_synth/`，12 张、3 种版式、程序渲染），
 计分脚本就绪（未配置 → 退出码 2），**本次未执行**。
- `bench_vision_real.py`（真实档）：**模板已就绪、待补数**（n=k 由用户提供 k 张标注截图）。

> 阶段二结论口径：只能写「n=5 抽样中一次解析成功率 100%」一类**带样本量**的表述；
> 禁止改写为总体稳定性 / 准确率。

### 6.1 选题图工具调用（n=12 小样本）

夹具：`fixtures/testsets/topic_tool/`（12 场景，3 个「不该调工具」、4 个多工具场景；
人工标注 `expected_tools`，sha256 冻结）。对照：同夹具、同提示词，仅切
`TOPIC_TOOLCALLING_ENABLED`。结果：`results/topic_tool_calling.json`。

| 指标 | tool 模式 | baseline |
| --- | --- | --- |
| 工具选择 P（微观 / 宏观） | **0.3889 / 0.5417** | — |
| 工具选择 R（微观 / 宏观） | **1.0 / 1.0** | — |
| 白名单外幻觉工具率 | **0.0** | — |
| 平均工具轮数 | 2.0 | 0 |
| 3 轮内收敛率 | 1.0 | 1.0 |
| 最终 JSON 一次解析且落库 | 1.0（12/12） | 1.0（12/12） |
| token 合计 | 68,775 | 11,935（5.8×） |

**结论（负向，按证据默认关闭）**：3 个无工具场景全部正确「不调用」，说明模型能判断
「要不要用工具」；但 9 个需要工具的场景里几乎都把 4 个工具全调一遍（标注平均 1.7 个），
精确率仅 0.389，token 5.8×、耗时 3.6×，最终 JSON 质量零增益。
工程上保留 `TOPIC_TOOLCALLING_ENABLED`（默认 false）与全部代码，结论与根因见
`docs/topic_tool_calling.md`。**禁止**把本实验写成「工具调用提升选题质量」。

## 7. 未测项与说明（如实）

1. **RAG 检索质量**：**已实跑**（2026-10-17），冻结黄金集见 §5.4（n=40，纯向量 Recall@5 0.9875），
 12 条构造集见 §3（仅验证管线，100% 是设计使然）。**仍未测**：真实语料（非 synthetic）上的检索质量——
 需脱敏知识库语料，扩到 200+ 条后升版夹具重跑。
2. **真实 LLM 稳定性**：**已执行**（§6），7 图 × n=5，30 轮有真实调用，attempts 口径一次成功率 1.0，
 raw 直解率 0.9667（n=30，missing=0），schema 字段级契约通过率 0.9667（n=30，多模态识别 4/5）。
 **视觉两档仍未执行**：合成档（12 张）与真实档（n=k）脚本就绪，待用户执行。
3. **可选补充项（未做，待确认）**：pytest-cov 后端行覆盖率（需新增 dev 依赖）；前端 `next build` 耗时与首屏 JS 包体积（无需新依赖，可在获准后补充）。
4. **离群值**：性能与时延样本一律保留原始值（`results/*.json` 的 detail.samples 可逐条审计），未做任何剔除；合成与 mock 场景已显式标注。
5. **环境边界**：所有数字出自同一台 Windows 开发机（localhost + Docker Desktop）；不同机器 / 不同数据集下数值会不同。

6. **环境性波动如实记录（不美化）**：`selftest_ops` 与 `reliability` 曾在全量编排中出现失败，根因均为本机**沙箱删除守卫**（每命令累计 50 次删除阈值）——① `next dev` 清理 `.next/dev` 缓存（171 个文件）时被拦截，导致 -Watch 健康检查超时（2026-10-16 与 10-17 两次复现）；② `bench_reliability` 清理 `reliability_child.json` 时同一命令内累计删除已达 50 阈值（2026-10-17，scale 之后顺序执行所致）。两者**均属本机沙箱特性、非产品缺陷**：清理/改名 `.next/dev` 后分命令复跑，`selftest_ops` 130/130、`reliability` 33/33 全绿（证据与 safe-delete 报错原文在 `docs/benchmark/logs/archive/`；规范化复跑结果见 §1 / §2）。

7. **回归总数口径修正（2026-10-18，`1804 → 1833`）**：此前汇报里的 **1804 是分批结果手算遗漏，不是实测合计**。
 上一轮实际分 4 批跑：`154 + 419 + 557 + 561 = 1691`（19 脚本），加 `selftest_evolution` **63** = **1754**，
 加 `selftest_evolution_v2` **79** = **1833**。`1804 = (1691 − 29) + 63 + 79`——少加的 29 正是
 `selftest_topic_tool_calling`（选题图工具回路自检）。此前收尾时**单次全量 21 脚本**实测
 **1833 通过 / 0 失败 / 7 跳过（运行时 1840）/ 退出码全 0**，与 `1754 + 79 = 1833` 一致；
 这是此前改造前的口径。**`selftest_evolution` 的 63 项仍在，v2 净增 79 项**，不存在「删旧自检凑数」。
 此前把 6 条环境 SKIP 改为确定性断言后，当前基线为 **1844 / 0 / 1（运行时 1845）**，逐分支见第 8 条。

8. **此前「SKIP→真实断言」计数变化（1833/0/7 → 1844/0/1）**：只改测试脚本，产品代码零改动；
 所有 monkeypatch 均在 `try/finally` / 上下文管理器中还原。逐脚本：

 | 脚本 | 改造前（PASS/FAIL/SKIP） | 改造后 | 变化 | 被真正纳入的分支 |
 | --- | --- | --- | --- | --- |
 | `selftest_agent_harness.py` | 60 / 0 / 0 | **63 / 0 / 0** | +3 PASS | 删除 `c.check(True, …)` 占位；注入 `AGENT_LLM_ENABLED=false`，真实断言异常类型 / 错误含 `AGENT_LLM_ENABLED` / `is_llm_configured=False`；再注入假 key + 非占位模型名断言就绪分支（只构造实例，零请求） |
 | `selftest_knowledge_admin.py` | 153 / 0 / 5 | **158 / 0 / 1** | +5 PASS，−4 SKIP | ① manual(general) 400；② import-text 400；③ import 零写入；④ 空词表回退内置；⑤ 非空词表内置隔离。剩余 1 条 = 扫描器自指豁免（逻辑豁免，保留） |
 | `selftest_rag.py` | 158 / 0 / 2 | **161 / 0 / 0** | +3 PASS，−2 SKIP | ① `/search` 400；② `/extract` 400；③ `/extract` 零 run |
 | 其余 18 个脚本 | 不变 | 不变 | 0 | — |
 | **合计** | **1833 / 0 / 7** | **1844 / 0 / 1** | **+11 PASS，−6 SKIP** | 运行时用例 1840 → **1845** |

 **注入点（必须是被测代码真正读取的位置）**：
 - `selftest_knowledge_admin` 的 400 闸门：patch `app.agent.embedding.service.local_deps_installed`
 （`describe_embedding_blocker` 在该模块内按模块全局名读取）；
 - `selftest_rag` 的 `/search`：patch `LocalBGEEmbedder._ensure_model` 抛
 `EmbeddingNotAvailableError`（`get_embedder` 本身不探测依赖，错误发生在模型懒加载点）；
 - `selftest_rag` 的 `/extract`：patch `app.agent.insights.service.local_deps_installed`；
 - 词表空/非空：patch `app.services.forbidden_source.load_active_wordlist` 返回受控列表；
 - `selftest_agent_harness`：临时改 `settings.AGENT_LLM_ENABLED / LLM_API_KEY / LLM_MODEL`，用后还原。
 纯属环境探测的 6 条 SKIP 已全部转为真实断言；扫描器自指豁免属合理逻辑豁免，保留 SKIP 并在代码注释说明。

## 8. 可写进简历的指标（附原始出处与口径）

> 筛选标准：来源可复现、口径可解释、数字不依赖小样本外推。
> **口径时点**：本节第 1、3 条的「17 脚本 / 1642 项 / 1,717 项」是 2026-10-17 快照；当前全量基线为
> **21 脚本 / 1844 通过 / 1 跳过**（此前后，见 §1、§7.8）。两套口径不可混加，引用时须写明时点。

1. **「为自研多智能体系统构建 17 个离线自检脚本的回归矩阵，合计 1642 项断言实跑全绿（0 失败）」**
 - 原始出处：`results/regression_scale.json`（逐脚本明细 + 原始日志）
 - 样本量/口径：17 个脚本 × 全量用例（运行时执行数；跳跃为条件性跳过、非失败）；本机离线零 token 可复跑（`bench_regression_scale.py`）
 - 应届生适用性：**适合**（体现工程质量与可复现证据链）

2. **「通过 mock 注入验证多层可靠性语义：4xx 不重试 / 5xx 指数退避重试（1s→2s，最多 3 次尝试）/ 并发互斥锁防重复采集 / 数据库不可达时健康探针降级返回 503」**
 - 原始出处：`results/reliability.json`；关键值：404 请求数=1,1,1、5xx 尝试数=3,3,3、锁拒绝=FetchInProgress、503 时延 P50=3016.5ms
 - 样本量/口径：隔离实验（本机回环 mock + 临时库全链路）；表述须保留「通过 mock 注入验证」前缀
 - 应届生适用性：**适合**（机制清晰、面试可展开）

3. **「系统规模：7 张 LangGraph 业务图 + 19 个回归/冒烟脚本 + 1,717 项断言 + 10 张业务表（零漂移迁移）」**
 - 原始出处：`regression_scale` 合计、`run_manifest`、既有 README/迁移文件
 - 口径：工程事实（可当场演示复现命令）
 - 应届生适用性：**适合**（数字全部可查）

4. **「构建 4 套带 ground truth 的冻结评测集（sha256 指纹；JSON 200 条 + 合规 128 条 + 归一化 108 条 + RAG 黄金集 30 片段/40 查询），全部指标可逐样本审计、错例全量留档」**
 - 原始出处：`results/testsets/*.json`（含混淆矩阵 / 匹配矩阵 / 逐样本明细）；夹具 `fixtures/testsets/`（冻结指纹、答案来源）
 - 样本量/口径：先冻结后评测；答案独立于被测实现（防循环论证）；逐样本可审计
 - 应届生适用性：**适合**（体现评测方法论与防过拟合意识，面试可展开）

5. **「视觉数值归一化：数值样本解析正确率 100%（n=91）、总正确率 99.1%（n=108）；评测驱动发现并修复 1 处边界缺陷（全角负号识别）」**
 - 原始出处：`results/testsets/normalize.json`（修复前后 106/108 → 107/108 留痕）
 - 口径：确定性函数、人工标注 12 类数值写法；缺例逐条可查
 - 应届生适用性：**适合**（小体量、可验证、有「评测→修复→复测」闭环）

6. **「JSON 兜底解析管线：合法输出一次解析通过率 100%（n=25）；并以三类匹配矩阵量化修复/拒绝行为（固定管道、零 token）」**
 - 原始出处：`results/testsets/json_robustness.json`（200 条 / 8 类）
 - 口径：**须保留「合成用例 + 解析器离线鲁棒性」前缀**；救回率与拒绝率引用时带 n
 - 应届生适用性：**适合**（矩阵与错例分类可讲深）

## 9. 不建议写进简历的指标（附原因）

1. **本地接口 P50/P95（localhost）**——原始出处 §4。原因：环境依赖强、无生产意义、面试追问「线上多少」会露怯；建议只作为面试口头补充（并声明「本机开发环境」）。
2. **合成截图的字段级准确率**——合成数据只验证管线与计分口径，**不能**代表模型识别准确率；真实档（n=k）补数后也须带样本量并注明截图来源。
3. **n=5 的真实 LLM 抽样数字**——**已有**（§6：30 轮 attempts 口径 1.0、raw 直解率 0.9667 / n=30、
 字段契约 0.9667 / n=30）；可写，但必须带「n=5/图、总 n=30、抽样」前缀；
 禁止写成「稳定性 100% / 准确率 100%」。
4. **RAG Recall@k / MRR**——**已产出**（§5.4，n=40，纯向量 Recall@5 0.9875 / MRR@5 0.9875 / nDCG@5 0.9789）。
 引用必须带「30 条 synthetic + n=40」前缀；hybrid 的负结果（0 救回 / 5 下降）可作为「按证据做决策」的素材，
 但不得包装成「稳健性提升」。12 条构造集（§3）的 100% 一一对应，不可作为质量结论。
5. **token 总量**——按量记录、无价格表，单个数字对简历无意义；如需展示成本意识，写「按 token 计量并做 P50/P95 成本评估」即可。

6. **合规扫描的原始 Precision/Recall（76.7% / 71.9%）**——夹具刻意含对抗变体（谐音/异体/繁体），直接引用会误导为「合规能力不足」；如提及必须带完整口径，或改用分组数据（自然语境召回 100%、分隔符变体 100%）。
7. **JSON「降级提取 / 过度解析」条数**——属解析器行为特征观察（顶层损坏时返回内嵌片段），不是质量结论；不得包装为「修复能力」或「稳定性」。

---

## 10. 面试怎么讲这些率（速查页）

> 三条底线：**答案独立**（夹具不由被测系统生成）· **先冻结后评测**（sha256 指纹，改夹具须升版本并留两版）· **口径必带**（n / 合成 / 本地环境，不包装成总体结论）。

| 指标 | 定义（公式） | n | ground truth 来源 | 讲法要点 |
| --- | --- | --- | --- | --- |
| JSON direct 一次成功率 | 直接解析成功且内容一致 / 合法样本 | 25 | 人工构造合法样本（素材池 dumps） | 设计基线：正常输出必须一次过 |
| JSON repaired 救回率 | 兜底救回且内容一致 / 需兜底样本 | 109 | 人工按「语义可安全修复」标注 | 能救：围栏 / 控制字符 / 多余括号；不能救：尾随逗号 / 引号变体（能力边界，逐条可查） |
| JSON failed 正确拒绝率 | 未硬解析 / 不可修复样本 | 66 | 人工标注（截断 / 结构错位 / 纯文本） | 安全语义：宁可拒绝，不交付形状错误的数据 |
| 三类匹配矩阵 | 期望 label × 实际结果（3×3） | 200 | 同上 | 错例四分类：未救回 / 降级提取 / 内容不符 / 过度解析 |
| 合规 Precision / Recall | TP/(TP+FP) ｜ TP/(TP+FN) | 128（正 64 / 负 64） | 人工撰写；正例禁抄词表、含谐音/分隔符/易误伤 | **必须带「含对抗变体」口径**；分组数据更立体（自然语境 100%） |
| 归一化数值正确率 | 解析值与标注一致 / 数值样本 | 91 | 人工标注 12 类数值写法 | 确定性函数；「评测 → 修复 → 复测」闭环（99.1%） |
| 归一化拒绝正确率 | 空值样本正确返回 None / 空值样本 | 17 | 同上 | 细节加分项：空对空不判命中、单独统计 |
| RAG Recall@k / MRR / nDCG | 平均（命中片段数/期望片段数）｜ 首个命中排名倒数 ｜ 二值增益折扣累计 | 40 查询 | 人工标注查询-片段对应（含 12 条 hard negative） | 纯向量 0.9875 / 0.9875 / 0.9789；hybrid 对照 0 救回 5 下降（n=40）；**必须声明 synthetic + n** |

**防自评偏差 / 过拟合的做法**（面试展开）：测集与标注人工另建、不复用被测系统词表/规则；评测前 sha256 冻结、评测脚本对夹具只读；小样本（n=5 LLM / n=k 真实截图）一律带 n 前缀；合成 / localhost 结果不进简历主 bullet。

---

### 附：本报告全部指标（机器可读镜像见 `results/metrics.jsonl`）

| 指标 | 值 | 单位 | n | 类别 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `regression.scripts_total` | 17 | 个 | 17 | offline_objective | regression_scale |
| `regression.cases_passed_total` | 1642 | 项 | 17 | offline_objective | regression_scale |
| `regression.cases_failed_total` | 0 | 项 | 17 | offline_objective | regression_scale |
| `regression.cases_skipped_total` | 3 | 项 | 17 | offline_objective | regression_scale |
| `regression.check_sites_static_total` | 1479 | 处 | 17 | offline_objective | regression_scale |
| `regression.pass_rate` | 1 | 比率 | 17 | offline_objective | regression_scale |
| `regression.selftest_agent_harness.py.cases` | 60 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_api.py.cases` | 37 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_content_agent.py.cases` | 98 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_content_variants.py.cases` | 146 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_cover.py.cases` | 79 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_dashboard.py.cases` | 176 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_db.py.cases` | 15 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_diagnostics.py.cases` | 156 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_hotspot.py.cases` | 54 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_knowledge_admin.py.cases` | 159 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_metrics.py.cases` | 139 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_ops.py.cases` | 130 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_rag.py.cases` | 161 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_search.py.cases` | 94 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_studio.py.cases` | 86 | 项 | 1 | offline_objective | regression_scale |
| `regression.selftest_topic_agent.py.cases` | 42 | 项 | 1 | offline_objective | regression_scale |
| `regression.verify_schema.py.cases` | 13 | 项 | 1 | offline_objective | regression_scale |
| `regression_smoke.scripts_total` | 2 | 个 | 2 | offline_objective | regression_smoke |
| `regression_smoke.cases_passed_total` | 75 | 项 | 2 | offline_objective | regression_smoke |
| `regression_smoke.cases_failed_total` | 0 | 项 | 2 | offline_objective | regression_smoke |
| `regression_smoke.check_sites_static_total` | 78 | 处 | 2 | offline_objective | regression_smoke |
| `regression_smoke.pass_rate` | 1 | 比率 | 2 | offline_objective | regression_smoke |
| `regression_smoke.smoke_crud_api.py.cases` | 37 | 项 | 1 | offline_objective | regression_smoke |
| `regression_smoke.smoke_hotspot.py.cases` | 38 | 项 | 1 | offline_objective | regression_smoke |
| `reliability.health_ready_db_down.status_code` | 503 | HTTP 状态码 | 10 | offline_objective | reliability |
| `reliability.health_ready_db_down.latency_p50_ms` | 3016.5 | 毫秒 | 10 | offline_objective | reliability |
| `reliability.health_ready_db_down.latency_p95_ms` | 3037.9 | 毫秒 | 10 | offline_objective | reliability |
| `reliability.db_down_boot_ok` | 1 | 布尔(1=是) | 1 | offline_objective | reliability |
| `reliability.hotspot.404_attempts_per_call` | 1,1,1 | 次请求（逐次） | 3 | offline_objective | reliability |
| `reliability.hotspot.5xx_attempts_per_call` | 3,3,3 | 次请求（逐次） | 3 | offline_objective | reliability |
| `reliability.hotspot.5xx_backoff_total_p50_ms` | 3006.8 | 毫秒 | 3 | offline_objective | reliability |
| `reliability.search.404_attempts_per_call` | 1,1,1 | 次请求（逐次） | 3 | offline_objective | reliability |
| `reliability.search.5xx_attempts_per_call` | 3,3,3 | 次请求（逐次） | 3 | offline_objective | reliability |
| `reliability.search.5xx_backoff_total_p50_ms` | 3009.8 | 毫秒 | 3 | offline_objective | reliability |
| `reliability.hotspot.partial_status` | partial | 状态 | 1 | offline_objective | reliability |
| `reliability.hotspot.partial_new_rows` | 5 | 条 | 1 | offline_objective | reliability |
| `reliability.hotspot.partial_failed_sources` | 2 | 个 | 1 | offline_objective | reliability |
| `reliability.hotspot.all_fail_status` | failed | 状态 | 1 | offline_objective | reliability |
| `reliability.hotspot.all_fail_new_rows` | 0 | 条 | 1 | offline_objective | reliability |
| `reliability.hotspot.lock_second_call` | FetchInProgress | 异常类名 | 1 | offline_objective | reliability |
| `rag.fixture.snippets_total` | 12 | 条 | 12 | synthetic_fixture | rag |
| `rag.fixture.queries_total` | 12 | 条 | 12 | synthetic_fixture | rag |
| `rag.pgvector.extversion` | n/a | 版本号 | 1 | offline_objective | rag |
| `rag.mechanics.self_check_ok` | 1 | 布尔(1=是) | 1 | synthetic_fixture | rag |
| `rag.vector_dim_measured` | 1024 | 维 | 12 | 实测向量维度（本地 bge）；12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.recall_at_1` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.recall_at_3` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.recall_at_5` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.mrr` | 1.0 | 比率 | 12 | 12 条片段与 12 条查询一一对应的构造集：命中即「能召回自己」，100% 是设计使然，**不构成检索质量结论**；质量结论见 §5.4 |
| `rag.index_build_ms` | 6.5 | 毫秒 | 12 | HNSW 余弦索引在 12 行上的重建耗时；小样本工程参考 |
| `rag.query_latency_end_to_end_p50_ms` | 6.04 | 毫秒 | 24 | 端到端单查询 P50（含编码，n=24）；本机环境，仅工程参考 |
| `rag.query_latency_end_to_end_p95_ms` | 7.37 | 毫秒 | 24 | 端到端单查询 P95；本机环境，仅工程参考 |
| `rag.query_latency_vector_only_p50_ms` | 5.98 | 毫秒 | 24 | 纯向量检索 P50（不含编码）；仅工程参考 |
| `rag.query_latency_vector_only_p95_ms` | 8.06 | 毫秒 | 24 | 纯向量检索 P95；仅工程参考 |
| `api_latency.health.p50_ms` | 1.91 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.health.p95_ms` | 21.53 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_hotspots.p50_ms` | 6.92 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_hotspots.p95_ms` | 30.84 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_hotspots_sources.p50_ms` | 5.54 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_hotspots_sources.p95_ms` | 25.4 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_topics.p50_ms` | 7.57 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_topics.p95_ms` | 31.09 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_notes.p50_ms` | 6.97 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_notes.p95_ms` | 31.09 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_accounts.p50_ms` | 6.54 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_accounts.p95_ms` | 28.18 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_personas.p50_ms` | 6.81 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_personas.p95_ms` | 23.5 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_dashboard_summary.p50_ms` | 10.81 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_dashboard_summary.p95_ms` | 22.77 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_dashboard_trend.p50_ms` | 6.87 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_dashboard_trend.p95_ms` | 30.66 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_dashboard_ranking.p50_ms` | 6.63 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_dashboard_ranking.p95_ms` | 31.22 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_reports_weekly.p50_ms` | 5.43 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_reports_weekly.p95_ms` | 25.28 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_knowledge.p50_ms` | 6.35 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_knowledge.p95_ms` | 29.33 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_metrics.p50_ms` | 9.15 | 毫秒 | 20 | offline_objective | api_latency |
| `api_latency.api_v1_metrics.p95_ms` | 31.21 | 毫秒 | 20 | offline_objective | api_latency |
| `testset_json.cases_total` | 200 | 条 | 200 | offline_objective | testset_json_robustness |
| `testset_json.overall_accuracy` | 0.675 | 比率 | 200 | offline_objective | testset_json_robustness |
| `testset_json.direct_first_pass_rate` | 1 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.repaired_rescue_rate` | 0.5413 | 比率 | 109 | offline_objective | testset_json_robustness |
| `testset_json.failed_rejection_rate` | 0.7727 | 比率 | 66 | offline_objective | testset_json_robustness |
| `testset_json.content_mismatch_total` | 0 | 条 | 200 | offline_objective | testset_json_robustness |
| `testset_json.shape_degraded_total` | 18 | 条 | 200 | offline_objective | testset_json_robustness |
| `testset_json.over_parsed_total` | 15 | 条 | 200 | offline_objective | testset_json_robustness |
| `testset_json.not_rescued_total` | 32 | 条 | 200 | offline_objective | testset_json_robustness |
| `testset_json.bucket.bracket.accuracy` | 0.56 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.bucket.broken.accuracy` | 1 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.bucket.ctrl.accuracy` | 1 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.bucket.fence.accuracy` | 1 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.bucket.quotes.accuracy` | 0 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.bucket.trailing.accuracy` | 0 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.bucket.truncated.accuracy` | 0.84 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_json.bucket.valid.accuracy` | 1 | 比率 | 25 | offline_objective | testset_json_robustness |
| `testset_compliance.cases_total` | 128 | 条 | 128 | offline_objective | testset_compliance |
| `testset_compliance.tp` | 46 | 条 | 128 | offline_objective | testset_compliance |
| `testset_compliance.fp` | 14 | 条 | 128 | offline_objective | testset_compliance |
| `testset_compliance.fn` | 18 | 条 | 128 | offline_objective | testset_compliance |
| `testset_compliance.tn` | 50 | 条 | 128 | offline_objective | testset_compliance |
| `testset_compliance.accuracy` | 0.75 | 比率 | 128 | offline_objective | testset_compliance |
| `testset_compliance.precision` | 0.7667 | 比率 | 60 | offline_objective | testset_compliance |
| `testset_compliance.recall` | 0.7188 | 比率 | 64 | offline_objective | testset_compliance |
| `testset_compliance.f1` | 0.742 | 比率 | 128 | offline_objective | testset_compliance |
| `testset_compliance.false_positive_rate` | 0.2188 | 比率 | 64 | offline_objective | testset_compliance |
| `testset_compliance.false_negative_rate` | 0.2812 | 比率 | 64 | offline_objective | testset_compliance |
| `testset_compliance.group.neg_bait_literal.hit_rate` | 1 | 比率 | 14 | offline_objective | testset_compliance |
| `testset_compliance.group.neg_bait_sensitive.hit_rate` | 0 | 比率 | 14 | offline_objective | testset_compliance |
| `testset_compliance.group.neg_clean.hit_rate` | 0 | 比率 | 36 | offline_objective | testset_compliance |
| `testset_compliance.group.pos_basic.hit_rate` | 1 | 比率 | 20 | offline_objective | testset_compliance |
| `testset_compliance.group.pos_case.hit_rate` | 0.5 | 比率 | 10 | offline_objective | testset_compliance |
| `testset_compliance.group.pos_separated.hit_rate` | 1 | 比率 | 14 | offline_objective | testset_compliance |
| `testset_compliance.group.pos_variant.hit_rate` | 0.35 | 比率 | 20 | offline_objective | testset_compliance |
| `testset_normalize.cases_total` | 108 | 条 | 108 | offline_objective | testset_normalize |
| `testset_normalize.overall_accuracy` | 0.9907 | 比率 | 108 | offline_objective | testset_normalize |
| `testset_normalize.numeric_accuracy` | 1 | 比率 | 91 | offline_objective | testset_normalize |
| `testset_normalize.rejection_accuracy` | 0.9412 | 比率 | 17 | offline_objective | testset_normalize |
| `testset_normalize.wrong_parse_total` | 0 | 条 | 108 | offline_objective | testset_normalize |
| `testset_normalize.wrong_accept_total` | 1 | 条 | 108 | offline_objective | testset_normalize |
| `testset_normalize.error_total` | 0 | 条 | 108 | offline_objective | testset_normalize |
| `testset_normalize.category.decimal.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.fullwidth_comma.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.invalid.accuracy` | 0.8889 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.kilo.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.letter_w.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.mixed_text.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.placeholder.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.plain_int.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.spaces.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.thousand_sep.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.wan.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_normalize.category.yi.accuracy` | 1 | 比率 | 9 | offline_objective | testset_normalize |
| `testset_rag.corpus_total` | 30 | 条 | 30 | synthetic_fixture | testset_rag_golden |
| `testset_rag.queries_total` | 40 | 条 | 40 | synthetic_fixture | testset_rag_golden |
| `testset_rag.vector_dim` | N/A | 维 | None | not_measured | testset_rag_golden |
| `testset_rag.index_build_ms` | N/A | 毫秒 | None | not_measured | testset_rag_golden |
| `testset_rag.recall_at_1` | N/A | 比率 | None | not_measured | testset_rag_golden |
| `testset_rag.recall_at_3` | N/A | 比率 | None | not_measured | testset_rag_golden |
| `testset_rag.recall_at_5` | N/A | 比率 | None | not_measured | testset_rag_golden |
| `testset_rag.mrr` | N/A | 比率 | None | not_measured | testset_rag_golden |
| `testset_rag.all_hit_rate_at_5` | N/A | 比率 | None | not_measured | testset_rag_golden |
| `testset_rag.query_latency_e2e_p50_ms` | N/A | 毫秒 | None | not_measured | testset_rag_golden |
| `testset_rag.query_latency_e2e_p95_ms` | N/A | 毫秒 | None | not_measured | testset_rag_golden |
| `testset_rag.query_latency_vector_p50_ms` | N/A | 毫秒 | None | not_measured | testset_rag_golden |
| `testset_rag.query_latency_vector_p95_ms` | N/A | 毫秒 | None | not_measured | testset_rag_golden |

