# 运营看板 + 周报 · xhs-agent

> 目标：**确定性看板在前、LLM 周报在后**。看板（`/api/v1/dashboard/*`）是**纯函数聚合 +
> 只读取数**（零 LLM、零网络）；周报（`/api/v1/reports/weekly*`）把周期事实全部**本地算好后
> 交给模型归纳成可执行建议**，模型**不做算术、不引外部数字**、数据不足必须声明。
> 新增**第五个业务图** `app/agent/weekly/`；**零数据库迁移、零新增依赖、harness 基座零改动**；
> 看板图表全部**纯 SVG / CSS 自绘**（不引任何图表库）。本文给「口径 / 图 / 接口 / 页面 / 红线 /
> 证据 / 未验证项」。

---

## 一、定位与边界

- **看板（确定性）**：`app/services/dashboard/` —— `aggregator.py` 纯函数（可离线单测）+
 `service.py` ORM 取数组装。**只读**：不改 `notes` / `topics` / `note_metrics` / `diagnostics`，
 不触发发布；不做任何 LLM 调用；不请求任何外部域名。
- **周报（LLM 归纳）**：`app/agent/weekly/` —— 与选题/ 创作/
 识别/ 诊断**同一套 harness**：真 LangGraph 图、
 `run_agent` 统一入口、检查点、审计（`agent_runs`）。周报**只写 `agent_runs.outputs`**，
 **不写任何业务表**。
- **数据来源**：全部来自本地库（10 张业务表不变）。周期内发布数 / 快照 / 热点 / 选题 / 诊断，
 全部是**本地程序计算**的事实。
- **本期无外部基准**：周报严禁引用「行业平均 / 爆款门槛 / 同类账号平均」等外部数字；
 跨篇基准属此前 RAG。

## 二、确定性聚合口径（`app/services/dashboard/aggregator.py`，纯函数、零依赖）

| 口径 | 规则 |
| --- | --- |
| 最新值 | 每笔记**按 `captured_at` 升序**；每个指标取**最后一个非空值**（残缺快照不覆盖旧值；**复用此前 `diagnosis/contracts.metric_latest` 唯一实现**，不重写第二套口径） |
| 六率（合计口径） | `views` 合计 ≤ 0 / 缺失 → 六率**全 `None`**（绝不臆造）；否则 `like/collect/comment/share/follow_rate` = 分子合计 / `views` 合计，`engagement_rate` = `(赞+藏+评+转)/浏览`；统一 **4 位小数** |
| 覆盖率可识别 | `covered_notes` = 至少一个非空值的笔记数；`metric_coverage` = 每指标有值的笔记数（前端如实展示「缺失按 0 参与合计」） |
| 趋势 | 桶边界按**本地自然日**（`range_days` 含今天）；`bucket=day` 超过点数上限自动收紧为等间隔（响应如实回 `auto`）；**同一桶内每篇笔记取桶内最新非空值再求和**；**空桶 → `null`（前端断线，禁止补 0 / 插值）** |
| 排行 | 仅 **published** 笔记；按最新非空值**降序**、缺失值排最后（tie 用发布时间降序）；`limit` 默认 `DASHBOARD_RANKING_TOP_N=10`（上限 100） |
| 参数校验 | `range_days` ∈ [1, 365]（默认 `DASHBOARD_DEFAULT_RANGE_DAYS=30`）；`metric` 六值枚举；`bucket` ∈ {day, auto}；**全部服务端校验**（越界 422） |
| 空集合语义 | 无数据返回 200 + 空集合 / 全 `None`，**不报错**；`note_id` 不存在 → 404 |

## 三、周报 Agent 图（`app/agent/weekly/`，第五个业务图）

```
START → load_context → generate_report → validate_report ─┬─ 合规 ──────────▶ persist_run → END
 ├─ 未超限 ────────▶ generate_report（有限回路）
 └─ 超限（≥3 轮）──▶ give_up（结构化错误，run 记 failed）
```

- **模型闭包注入、绝不入状态**；`load_context` 校验周期（非法 → `WeeklyContextError`）后调
 `build_period_facts` 把周期事实**全部本地算好**（ORM 行转普通 dict）；
- **周期解析**（`resolve_period`）：默认近 `WEEKLY_DEFAULT_DAYS=7` 天（含今天）；支持显式
 `period_start` / `period_end`（互斥于 `days`）；跨度 1~90 天；非法 → **422（零 `agent_runs` 产生）**；
- JSON 复用 `invoke_json` 三层兜底；最坏模型调用 = `AGENT_JSON_MAX_ATTEMPTS × (1 + AGENT_NODE_MAX_RETRIES)`
 = **3×3 = 9**；`persist_run` **零业务表写入**，只组装 `state["output"]`；
- `service.py` 在 `run_agent` 后用**裸 SQL 回填** `agent_runs.outputs`（`report / period_start /
 period_end / parse_attempts / prompt_version / persisted`）与 `llm_raw`（`redact_secrets` 脱敏 +
 截断 20000）；**harness 基座零改动**（业务明细由 service 补齐，与前四图同范式）；
- 服务端→图传参只传**已解析的显式起止**（不传 `days`），避免与图内二次校验的
 「days 与显式起止互斥」规则冲突（本次自检抓出并修复的真实缺陷，见第八节）。

## 四、周期事实与报告契约（`contracts.py`）

**周期事实（`build_period_facts`，唯一触库函数、只读）**：

- `counts`：周期内发布数 / `ready` / `draft` / 在写选题；
- `hotspot_topic_counts`：周期内热点采集数 / 当前累计被采用热点数（字段名以 `_total` 明示
 「无状态变更时间戳，只能给当前累计」）/ 周期内新建选题数；
- `period_totals`：全账号 published 最新值合计 + 六率（**与看板同口径、同取数管道**）；
- `period_deltas`：各笔记**周期增量求和**（主口径「周期内末值 − 周期前末值」，无基线退化为
 「周期内末 − 周期内首」，单条无基线 → `None`）；
- `new_published_notes`：周期内新发布清单（title / published_at / 最新六指标 / 周期增量，上限 20）；
- `diagnostics`：周期内诊断摘要（类型 / 评分 / 核心建议前 3 条，上限 10）；
- `data_gaps`：**本地缺口如实声明**（无发布 / 无快照 / 覆盖率不足 / 无诊断 / 增量不可算…）。

**报告归一（`normalize_report_payload`）**：

- 宽容解包 `report / data / result / weekly_report` 包装层；周期以**程序事实为准**（模型回显错误不采信）；
- `summary` 必填（≤600 字截断）；`highlights / issues / data_evidence` 各 ≤8 条（单条 ≤300 字）；
- **`next_week_suggestions` 必填非空**（为空 → 硬伤触发回炉）；每条必须是**具体可执行动作**；
- `confidence` ∈ [0,1]（越界 / 非法 → `None` + warning，宽容不打回）；
- 模型声明的 `data_gaps` 与本地缺口 **`merge_data_gaps` 合并去重**（本地优先、上限 12）。

## 五、接口面（第 10、11 只 router）

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/dashboard/summary?range_days=` | 计数七键 + 全账号最新漏斗（六指标合计 + 六率 + 覆盖率） |
| `GET /api/v1/dashboard/trend?metric=&range_days=&note_id=&bucket=` | 指标趋势点（空桶 `null`）；`note_id` 缺省 = 全账号（仅 published） |
| `GET /api/v1/dashboard/ranking?metric=&limit=` | published 笔记排行（缺失值垫底） |
| `POST /api/v1/reports/weekly/generate` | 跑周报（201；`{period_start?, period_end?, days?}` 全可选）；400 / 422 全语义；**静态路径先于 `/{run_id}` 注册** |
| `GET /api/v1/reports/weekly?limit=` | 周报历史（`created_at` 倒序 + id 稳定排序） |
| `GET /api/v1/reports/weekly/{run_id}` | 单条详情；不存在 → 404 |

- **看板三端点仅 GET、周报仅 GET / POST，无 PUT / PATCH / DELETE**；
- 路由层**零 SQLAlchemy 表达式**（`selftest_api.py` 静态断言），SQL 全在 service 层；
- 所有响应**不含 `llm_raw` / API key / base64 / 绝对路径**；`llm_raw` 只进 `agent_runs` 列。

## 六、前端：`/dashboard` 页（第 9 导航入口）

- **纯 SVG 图表组件**（`src/lib/charts/`，**不引任何图表库**）：
 - `LineChart.tsx`：折线，`null` 断线不补点、每点 `<title>` 悬浮显示桶 + 值、ResizeObserver 自适应；
 - `HBars.tsx`：横向条形（按最大值归一；零值 / 缺失占位）；
 - `FunnelBars.tsx`：漏斗六指标逐级 + 对应率（`views=0` 显示「不可计算」）。
 - 三者**只接收已聚合 props**：不发请求、不做二次算术。
- **页面结构**（`src/app/dashboard/page.tsx`，Suspense 壳 + Client 内容）：
 - 顶部时间范围选择（默认 30 天）+「看板 / 周报」Tabs；
 - **看板 Tab**：KPI 卡行（七项计数）→ 漏斗卡 → 趋势卡（指标切换 + 单笔记 / 全账号）→
 排行卡（点标题跳 `/notes/{id}`）；
 - **周报 Tab**：周期选择（近 7 天默认 / 显式起止）+「生成周报」（BFF 超时 120s，期间禁用）；
 结构化周报展示（周期 / 概览 / 亮点 / 问题 / 数据依据 / **下周建议逐条「复制」** / 数据缺口 /
 置信度 + 元信息）；周报历史表（「查看」拉详情回填）；**周报只增不改**。
- **BFF 薄代理 6 条**（`src/app/api/v1/dashboard/*`、`src/app/api/v1/reports/weekly/*`）：
 一律走 `proxyToBackend`，不写 `export const dynamic`；生成请求原样透传 + 120s 超时。
- 依赖与端口：**npm 零新增**；浏览器一律 `http://localhost:3000`（禁 `127.0.0.1:3000`）。

## 七、隐私与红线（工程落实）

| 要求 | 落实 |
| --- | --- |
| 只读 | 看板三接口 + 周报生成全程**不改任何业务表**（离线自检断言八张业务表计数前后一致） |
| 不自动发布 / 不登录 | 全链路不请求任何平台域名（全前端域名仍**恰 1 处**：此前发布外链常量，仅导航）；不保存账号 / 密码 / 登录态（合规扫描 0 命中） |
| 不编外部数字 | System 提示词**明文禁令** + 检查脚本「外部话术黑名单」双保险；`data_evidence` 数字须来自给定周期事实（本地重算比对） |
| 数据不足不硬造 | 本地 `data_gaps` 全量并入报告；无数据仍可成功产出但必须声明缺口 |
| 接口 / 审计无敏感物 | 响应无 `llm_raw` / key / base64 / 绝对路径；`llm_raw` 先脱敏再截断（20000）后只落审计列 |
| 不内置价格表 | 只记 `prompt / completion / total_tokens`；成本 = token × **你在控制台看到的实际单价** |
| 定时默认关闭 | `WEEKLY_SCHEDULER_ENABLED=false`（字段默认值 + 实测 `start_weekly_scheduler` 返回 `None`）；开启时作业**合并进唯一 `lifespan`**，`coalesce=True` / `max_instances=1` |

## 八、自检与证据（本机实跑，2026-10-12）

- `scripts/selftest_dashboard.py`（离线，9 组）**176/176 PASS，退出码 0**：图结构（五节点 +
 条件边 + 有界回路）/ 聚合纯函数（latest 向前取非空、六率 4 位小数与 views≤0 全 None、步长
 收紧 90/30→(3,auto)、趋势桶内最新再求和 [[100,180,300]]、空桶 None 不插值、排行缺失垫底）/
 周期事实（计数 / 覆盖率 / 增量 ≥160 / 诊断摘要并入）/ 看板接口（漏斗**自洽重算**、趋势三点
 精确 [100,180,260]、无快照全 None、404 / 422）/ 成功路径（201、`parse_attempts=1`、假模型恰
 1 次、提示词含事实且首轮无回炉块、`outputs` 白名单、`llm_raw` 脱敏截断、**八表只读**）/
 失败路径（全坏 JSON → 400 且模型调用 **9 = 3×3**、未配置 → 400 **零请求零 run**、五类非法
 周期 → 全 422 零 run）/ 接口面（六端点方法精确、main.py `include_router` **恰 11**、
 lifespan 唯一、定时默认关闭）/ 零迁移（versions 恰 2 支、`alembic current`=`0002 (head)`、
 `check` 退出码 0、public 恰 10 张业务表）/ 合规扫描（后端 18 + 前端 12 文件 0 命中；全前端
 域名恰 1 处）/ 清理复查（双口径残留 0）。
- 全量回归 **11 脚本 779 项零回退**（db 15 / schema 13 / api 37 / harness 60 / hotspot 54 /
 topic 42 / content 98 / cover 79 / studio 86 / metrics 139 / diagnostics 156）；
- 前端 `npx tsc --noEmit` / `npm run lint` / `npm run build` **全绿**；`/dashboard` 静态预渲染
 （○）、6 条 BFF 全部动态（ƒ）标记正确；
- 浏览器冒烟（生产构建 + headless Chrome）**17/17 PASS、0 console 错误**，截图
 `docs/screenshots/s17_01_dashboard_board.png`（看板）/ `s17_02_dashboard_weekly.png`
 （周报 Tab）/ `s17_03_weekly_detail.png`（详情卡）；`healthcheck` 四项全绿；
- 本次自检抓出并修复 1 个**本次新增代码自身**的缺陷：service 向图传参时把「已解析起止 + days」
 一起下发，与图内 `resolve_period` 的互斥规则冲突（表现为 POST 恒 422）；修复 = 只传显式起止。

## 九、未验证项（如实说明，未做美化）

1. **真实 LLM 三项**（周报结论质量是否认可 / 连续 3 轮 `parse_attempts` 是否恒 1 / 真实 token
 用量）：**未验证**（需用户显式运行 `scripts/check_weekly_report.py`，会消耗 token）；
 未配置 / 开关关闭 / key 缺失三种退化分支已验证**退出码 2、零请求**；参数互斥 → 退出码 1；
2. `WEEKLY_SCHEDULER_ENABLED=true` 的真实定时触发未实测（默认关闭；cron 解析与作业注册路径已
 在离线自检覆盖）；
3. 趋势 / 排行在**大数据量**下的表现未实测（当前演示库 1 篇笔记 / 3 快照）；
4. 换 `LLM_MODEL` 后的周报质量未实测（本机为 `deepseek-flash`）；
5. 浏览器冒烟用了 `[SMOKE]` 模拟周报行渲染历史与详情（**模拟数据，非模型生成**；冒烟后已删除）；
 真实 LLM 生成后的页面展示链路未实测。

## 十、成本（只记 token，不内置价格表）

- 每次周报一条 `agent_runs`：`prompt_tokens / completion_tokens / total_tokens`（+ 模型名与
 base_url，**绝不记录 key**）；看板全部接口**零 token**（确定性聚合，无 LLM）；
- 检查脚本默认 **3 轮**（`--rounds 1` 最小成本验证），逐轮打印 token 与汇总；
- **成本 = token × 你在 DeepSeek 控制台看到的实际单价**，本仓库不做任何金额换算。

## 十一、本次【不做】

- 不做跨篇爆款 / 行业基准（此前 RAG）；不做周报的编辑 / 删除（历史只增不改）；
- 不做自动发布 / 登录 / 抓取平台后台（**永不做**）；
- 不做推送（邮件 / 企微）与多用户权限；不做公网部署；
- **零数据库迁移 / 后端 requirements 零新增 / 前端 npm 零新增（图表纯 SVG 自绘）/
 harness 基座零改动**。
