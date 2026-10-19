# 数据回填 · xhs-agent

> 范围：**已发布作品的运营数据沉淀** —— 手工录入，或上传创作中心截图由**视觉模型识别候选值**，
> **人工核对后**才落库为 `note_metrics` 快照。本次接通视觉（`app/agent/llm/factory.py` 的**唯一**基座改动）
> 并新增**第三个业务图** `app/agent/metrics/`；**图不做 persist**——识别链路零写入，落库永远走显式
> `POST /api/v1/metrics`。零数据库迁移（`note_metrics` 由 `0001` 建好）。
> 本文件给「改了什么 / 口径与红线 / 怎么验证 / 边界在哪」；页面数据流另见 `docs/frontend_pages.md`（第 5d 节）。

---

## 一、定位与边界

- **它是什么**：`/metrics` 数据回填页 —— ① 手工录入六指标（浏览 / 点赞 / 收藏 / 评论 / 分享 / 涨粉）；
 ② 上传截图由视觉模型**只读转录**为六个指标的候选值 → 人工核对 → 保存为快照 → 时间线回读。
- **它不是什么**：不做趋势图；不做单篇诊断；不抓取创作后台数据。
- **红线（全程不变）**：不自动发布、不登录小红书、不保存账号 / 密码 / 登录态凭据、**不向任何小红书域名
 发起网络请求**；截图由用户本人在页面选取、**仅本机流转**（后述隐私默认），识别结果**必须人工核对后才落库**。
- **唯一模型通道**：所有识别都经既有 harness `run_agent`（LangChain / LangGraph 基座），
 禁止裸 HTTP 自编排；LLM 统一 DeepSeek（OpenAI 兼容）。

## 二、数据模型与字段口径（直接复用，未重建）

**表 `note_metrics`（`0001` 已建，本次零迁移）**：`id` / `note_id`（FK→notes，`ON DELETE CASCADE`）/
`captured_at`（timestamptz）/ `data_source`（VARCHAR(16)，枚举 `screenshot` / `manual`）/
六个 `BigInteger` 可空指标 / `evidence`（JSONB）/ `screenshot_path`（Text）/ `created_at`。
约束：`uq_metric_snapshot(note_id, captured_at)`（同笔记同一时刻只能一条快照）+ `ix_note_metrics_note_id_captured_at`。

**六指标是唯一口径**（与表列一一对应）：`views / likes / collects / comments / shares / new_followers`。
`screenshot_spike` 原型的 14 个指标里，`impressions / clicks / ctr / profile_visits / avg_watch_seconds /
danmaku / recommend_ratio / search_ratio` **一律忽略**——落不进表，也不作为字段返回（模型若输出会被记入
`evidence.ignored_fields` 并忽略）。

**时间口径**：前端 `datetime-local` 提交时转为 UTC ISO；无时区输入按 **UTC+8**（`core/timeutil`）解释；
同笔记同 `captured_at` 唯一（冲突 → 409，页面提示改采集时刻）。

**`screenshot_path` 恒服务端受控写入**：请求模型**没有**该字段（客户端传了也忽略）；
仅当 `SAVE_SCREENSHOT=true` 且识别时用户勾选确认，才由路由层写 `{UTC 时间戳}_{sha256 前 12 位}.{后缀}`
（形状正则校验 + 目录限定，防路径注入）；默认整列为 NULL。

## 三、视觉接通（唯一基座改动：`app/agent/llm/factory.py` + config）

- `get_chat_model(..., want_vision=True)` 由「一律抛 `AgentConfigError`」改为返回**多模态** `ChatOpenAI`：
 模型名优先 `settings.LLM_VISION_MODEL`，为空回退 `settings.LLM_MODEL`；api_key / base_url 复用；
 闸门顺序不变（开关 → key → 模型名），构造过程**不发任何请求**；
- 工厂**不校验端点能力**——「模型不支持图片」的接口报错由识别图按关键词
 （`vision / multimodal / 不支持图片 …`）转成**友好中文错误**（附视觉文档链接 `VISION_DOC_URL`）；
- 新增就绪位：`is_vision_configured` 与 `describe_llm_readiness` 的
 `vision_model / vision_model_is_placeholder / vision_ready` 三键（**纯追加**，不破坏既有断言）；
- **不硬编码任何模型名**：`LLM_VISION_MODEL` 的取值以 DeepSeek 控制台当前列表为准（占位注释见 `.env.example`）。

相关配置项（`backend/.env`，全部有默认值）：`METRIC_PROMPT_VERSION=v1` / `LLM_VISION_MODEL=`（空 → 回退）/
`SAVE_SCREENSHOT=false` / `SCREENSHOT_DIR=data/screenshots` / `METRIC_MAX_IMAGE_BYTES=4194304` /
`METRIC_ALLOWED_IMAGE_TYPES=image/jpeg,image/png,image/webp`。

## 四、识别 Agent（`app/agent/metrics/`，第三个业务图）

**文件**：`__init__.py` / `state.py`（`MetricState`）/ `contracts.py`（六字段口径 + 别名字典 + 图像工具 +
专属异常）/ `prompts.py`（版本化提示词）/ `graph.py`（构图 + `normalize_payload`）/ `service.py`（编排 + 审计回填）。

```
START → load_image → transcribe ─┬─ 解析成功 ──────▶ normalize_validate → END
 ├─ 解析失败且未超限 → transcribe（有限回路）
 └─ 不支持图片 / 超限 → give_up（抛结构化错误，run 记 failed）
```

- **真 LangGraph**：四节点 + `add_conditional_edges` 条件边 + 有限回路（上界 `AGENT_JSON_MAX_ATTEMPTS`）
 + `compile_graph` 绑定检查点（经既有 harness）；
- **模型与图片字节均经闭包注入**（绝不入状态——图片是二进制大对象，放进状态会污染检查点）；
- `load_image`：MIME 白名单 + **文件头魔数**探测（JPEG / PNG / WebP，零图像库）+ 大小上限 + 空文件；
 转 `data:image/…;base64,…` 暂存闭包；带 `note_id` 时校验笔记存在并载入标题（人工核对用）；
- `transcribe`：`SystemMessage + HumanMessage(content=[text, image_url])` 多模态消息；
 JSON 解析复用 `json_repair.invoke_json` 三层兜底（`json_mode` 降级**不计** attempt）；
- `normalize_validate`（纯函数 `normalize_payload`，可单测）：别名归一（中文别名 → 六字段）+
 数字换算（`1.2万/1.2w→12000`、`1.2亿`、`3.4千/3.4k→3400`、去千分位逗号；`--/—/暂无/?` → null；负数 / `%` 不接收）+
 `raw` 与 `value` 冲突时**以 raw 换算为准**并标 uncertain + 生成 `evidence`（每字段 raw / value /
 来源 / 置信度 / missing / uncertain + ignored_fields + prompt_version）；
- **异常契约**：`ImageValidationError`(422) / `NoteMissingError`(404) / `VisionUnsupportedError`(400) /
 `RecognitionGiveUpError`(400)，全部继承 `AgentNonRetryableError`（不消耗节点重试额度），
 由 service 翻译成统一错误体——**响应绝不含原始堆栈**；
- **审计回填**（不改 harness，沿用第 10 / 12 步机制）：按 `run_id` 在 `agent_runs` 上补
 `outputs`（六指标 / missing / data_source …）+ `llm_raw`（先 `redact_secrets` 再截断）+ `note_id`；
 **绝不写图片数据**（无 base64 / 无绝对路径）。

## 五、接口面（`/api/v1/metrics`，第 8 只 router；无 PUT / DELETE）

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/metrics?note_id=` | 时间线（`captured_at` 倒序 + id 稳定排序）；缺 `note_id` → **400**、笔记不存在 → **404** |
| `POST /api/v1/metrics` | 快照落库（201）；`note_id` 不存在 → 404；(note_id, captured_at) 冲突 → **409**；负数 / 非整数 → 422；客户端伪传 `screenshot_path` 被忽略 |
| `POST /api/v1/metrics/recognize` | multipart 上传（`file` 必填 + `note_id` / `save_screenshot` 可选表单）；返回**候选**（**绝不写库**）；类型 / 大小 / 魔数非法 → 422；视觉未配置 / 不支持图片 / JSON 超限 → 400（均可操作提示） |

`metric_recognize` 为 `RunType` 枚举新成员（`native_enum=False` 落 VARCHAR，**无 DDL**）。

## 六、前端：`/metrics` 数据回填页

**页面结构**：顶部笔记选择器（`GET /api/v1/notes?status=published`，**只列已发布**；支持 `?note_id=` 预选）→
中部「新增数据快照」（Tabs：**手工录入** / **截图识别**；两种方式最终都走同一表单 → `POST /metrics`）→
底部「数据时间线」（倒序表格：采集时刻 / 来源 Badge / 六指标 / 截图留档）。

**识别流程（人工核对闭环）**：选择截图 → `POST /metrics/recognize` → 六指标**候选**回填表单 +
提示条列出 missing / uncertain / warnings → 人工核对 / 补填 → 「保存快照」（记 `data_source=screenshot`，
`evidence` 随快照保存）。**识别接口不写库是硬规则**。

**BFF（2 个薄代理）**：`/api/v1/metrics`（GET 带 search、POST `request.text` 原样透传）与
`/api/v1/metrics/recognize`（**接收 FormData 并以 multipart 原样转发**，`timeoutMs=120000`——
`proxyToBackend` 因此新增 `formData` 分支，不手设 Content-Type 让 fetch 自带 boundary）。

**入口**：站点导航第 **7** 入口「数据回填」；`/notes` 列表已发布行「回填数据」；
创作台头部（published 时）「数据回填 →」——均跳 `/metrics?note_id=<id>`。

## 七、隐私与红线（工程落实）

| 要求 | 落实 | 复核 |
| --- | --- | --- |
| 识别结果人工核对才落库 | 识别接口零写入（图无 persist）；落库仅 `POST /metrics` | 离线自检（识别前后 `note_metrics` 行数不变）+ 代码 |
| 截图默认不留存 | `SAVE_SCREENSHOT=false` 默认；开启也须识别时**用户勾选**（双闸门）才落盘；文件名受控生成 + 形状校验 | `selftest_metrics` 截图留档组 6 项（含 `../../` 注入拒绝） |
| 不留存截图时无路径 | `screenshot_path` 默认恒 NULL；客户端不可传 | 接口面断言 + 时间线显示「—」 |
| 不请求小红书域名 | 全链路只访问本地后端；截图仅上传到本机服务 | `selftest_metrics` 合规扫描（本次触碰文件 0 命中） |
| 响应 / 审计无敏感物 | 响应无 base64 / data URL / `llm_raw` / 绝对路径；`agent_runs.llm_raw` 已脱敏截断 | `selftest_metrics` + `check_metric_vision` 双方言断言 |
| 不保存账号 / 登录态凭据 | 全步不引入任何登录态概念 | 合规扫描 |

## 八、自检与证据（本机实跑）

| 项 | 结果 |
| --- | --- |
| `scripts/selftest_metrics.py`（离线，进程内 TestClient + 真实库；9 组） | **139/139 PASS，退出码 0** |
| ↳ 覆盖要点 | 图结构 / normalize 纯函数 / 手工录入全边界 / 识别成功与失败路径（假模型）/ 图片校验 5 例 / 截图留档 / 接口面 / 零迁移 / 合规 / 双口径清理残留 0 |
| `scripts/check_metric_vision.py`（**真实 LLM**，基准示例图连跑 3 轮） | **19/19 PASS**；3 轮**六指标全部与参考值一致（6/6×3）**；`parse_attempts` 恒 1；token 合计 5562 |
| 浏览器 E2E（playwright-core + 本机 Chrome，生产 `next start`） | **28/28 PASS**（选择器只列已发布 → 手工录入 → 时间线回读 → 409 → 留空 + 倒序 → `/notes` 入口预选）；`pageerror` 空 |
| ↳ 截图留档 | `docs/screenshots/s15_01…s15_05*.png` |
| `npx tsc --noEmit` / `npm run lint` / `npm run build` | 0 / 0 / 成功（`/metrics` ○ 静态 + 2 条 BFF 路由） |
| 回归矩阵（12 个既有脚本） | 全 PASS（`selftest_agent_harness` 因本次提级断言为 **60/60**；其余口径不变） |
| `healthcheck.py`（前后端同时在线） | 四项全绿 |
| 迁移 | `alembic current` = `0002 (head)`；`check` 退出码 0；`versions` 恰 2 支；`public` 仍 10 张业务表 |

> 真实 LLM 的图片输入行为**已实测**：DeepSeek 视觉模型接受 `data:image/…;base64,…` 消息并稳定返回
> 合法 JSON（3 轮 `parse_attempts` 恒 1）；第二轮模型还主动注明「曝光量 / 点击量 / 点击率 / 主页访问
> 不属于本次指标，已忽略」——口径理解正确。

## 九、未验证项（如实说明）

1. **非基准样本的准确度**：自动对照仅覆盖 `screenshot_spike` 的合成基准图（数字清晰无歧义）；
 **用户真实场景截图**（真机后台、压缩、暗色主题、局部遮挡等）未实测，属人工核对职责范围。
2. **更换 `LLM_VISION_MODEL` 后**的行为未实测（当前本机为 `deepseek-flash`；换模型只需改 `.env`，
 失败路径已有友好降级：不支持图片 → 400 + 文档链接）。
3. **`SAVE_SCREENSHOT=true` 的浏览器端到端**未走（离线自检已覆盖「配置 + 用户确认」双闸门与
 注入拒绝逻辑；浏览器 E2E 只验证默认关闭态不落盘）。
4. **极端图片边界**：超大分辨率 / 超长截图 / HEIC 等非白名单格式（后者会被 422 明确拒绝）未逐项实测。
5. **模型多轮波动**：仅一个样本 × 3 轮全对（`temperature=0`）；样本面窄，不做更广的稳定性声称。

## 十、成本（只记 token，不内置价格表）

- 每次识别一条 `agent_runs`，记录 `prompt_tokens / completion_tokens / total_tokens`（**不记图片本体**）；
- 实测参考（基准示例图 1024×754 级、60.6 KB PNG）：单轮约 `prompt 1564 + completion 262~329`，
 3 轮合计 **5562** token——**图片输入按视觉模型的图片 token 计费**（随分辨率 / 尺寸变化）；
- **成本 = token × 你在 DeepSeek 控制台看到的实际单价**；本仓库不内置任何价格表、不做金额换算。

## 十一、本次【不做】

- 不做趋势图/ 单篇诊断/ 数据看板；
- 不做自动发布 / 定时采集后台数据 / 登录创作中心 / 任何小红书域名请求（**永不做**）；
- 六指标之外的字段不落库、不作为指标返回；
- 不做快照的 `PUT / DELETE` / 物理删除（历史时间线只增不改）；
- 除 `factory.py` 接通视觉外**无任何基座改动**（未改 `runner` / `state/base` / `graph/base` /
 `parsing/json_repair` / `errors` / `checkpoint` / `tracing`）；
- 不新增图像处理库（**无 Pillow**；类型校验靠 MIME + 文件头魔数，不做解码）；
- 不引入 markdown / 图表 / 上传组件类前端库（**前端零新增依赖**；Python 仅 +`python-multipart`）；
- 不做公网部署（截图与数据只在本机与所选 LLM 端点之间流转）。
