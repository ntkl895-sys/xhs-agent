# 前端业务页面文档（此前 两页 + 此前 封面工作室 + 此前 创作台 + 此前 数据回填 + 此前 单篇诊断 + 此前 运营看板 / 周报 + 此前 知识库 + 此前 知识库人工维护）

> 范围：`frontend/` 的业务页面与其共用的代理层、类型、共享组件 ——
> `/hotspots`（热点雷达）、`/topics`（选题库）， `/covers`（封面工作室），
> `/notes`（笔记列表）+ `/notes/[id]`（创作台）， `/metrics`（数据回填），
> `/diagnostics`（单篇诊断）， `/dashboard`（运营看板 + 周报），
> `/knowledge`（知识库 · 爆款规律 RAG）。
> 技术栈与约束沿用此前（Next 16 App Router + React 19 + TS + Tailwind 4 + shadcn/base-nova）。
> 依赖：此前零新增；此前仅新增 `html-to-image@1.11.13`（精确锁定，见第 9 节）；
> **此前零新增**；**此前零新增**（后端仅 +`python-multipart`，见第 9 节）；**此前零新增**；
> **此前零新增**（看板图表全部纯 SVG / CSS 自绘，不引任何图表库）；
> **此前零新增**（知识库页复用既有组件，检索 / 提炼均为薄转发；后端仅 +2 个 Python 包，见第 8 节）。

---

## 1. 数据流（两层代理不变量）

```
浏览器（"use client" 页面）
 │ fetch 同源相对路径：/api/v1/**（永远不出现后端地址）
 ▼
本站 Route Handler（Next 服务端） src/app/api/v1/**
 │ proxyToBackend(path, { method, search | body, timeoutMs? })
 ▼
FastAPI（后端地址只来自服务端变量 API_BASE_URL = http://localhost:8000，无 NEXT_PUBLIC_）
```

- 浏览器侧统一走 `src/lib/api-client.ts` 的 `apiClient.get/post/patch` + `ApiError` + `toQuery`；
- 服务端侧统一走 `src/lib/api-server.ts` 的 `proxyToBackend`：`no-store`、**状态码与响应体原样透传**；
- 后端不可达时 `proxyToBackend` 返回 **503 + `{status:"error",code:"backend_unreachable",message,...}`**，页面以 `ErrorText` 展示，不白屏。

## 2. 代理清单与超时策略

新增 9 个 Route Handler（全部薄转发）；**此前再新增 3 个**：

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/api/v1/hotspots` | GET | 列表；`request.nextUrl.search` 原样透传 |
| `/api/v1/hotspots/sources` | GET | 源状态 |
| `/api/v1/hotspots/refresh` | POST | 手动采集，**timeoutMs = 60000** |
| `/api/v1/hotspots/runs` | GET | 采集运行记录 |
| `/api/v1/hotspots/[id]` | **仅 PATCH** | 人工标记状态（后端无单条 GET，故不导出 GET） |
| `/api/v1/topics` | GET | 列表 |
| `/api/v1/topics/generate` | POST | 选题生成（同步阻塞），**timeoutMs = 120000** |
| `/api/v1/topics/runs` | GET | 选题运行记录 |
| `/api/v1/topics/[id]` | GET、PATCH | 详情 / 人工微调与审核流转 |
| `/api/v1/covers` | GET、POST | **此前**：候选列表（`?note_id=` 透传）/ 新建候选 |
| `/api/v1/covers/[id]` | **仅 PATCH** | **此前**：更新字段 / 互斥选中（后端无 PUT/DELETE） |
| `/api/v1/notes` | **仅 GET** | **此前建**：笔记选择器；**此前**创作台列表页共用（后端还有 `/notes/generate`、`/notes/runs` 未接） |
| `/api/v1/notes/[id]` | GET、PATCH | **此前**：详情 / 编辑与状态流转（`request.text` 原样透传；后端无 PUT/DELETE） |
| `/api/v1/metrics` | GET、POST | **此前**：时间线（`?note_id=` 透传）/ 快照落库（`request.text` 原样透传） |
| `/api/v1/metrics/recognize` | **仅 POST** | **此前**：multipart **以 FormData 原样转发**（不手设 Content-Type），**timeoutMs = 120000** |
| `/api/v1/diagnostics` | **仅 GET** | **此前**：诊断历史（`?note_id=` 透传；后端无 POST 到该路径） |
| `/api/v1/diagnostics/generate` | **仅 POST** | **此前**：跑诊断并落库（同步阻塞），`request.text` 原样透传，**timeoutMs = 120000** |
| `/api/v1/diagnostics/[id]` | **仅 GET** | **此前**：单条详情（后端无 PUT/DELETE） |
| `/api/v1/dashboard/summary` | **仅 GET** | **此前**：看板汇总（`?range_days=` 透传） |
| `/api/v1/dashboard/trend` | **仅 GET** | **此前**：指标趋势（`?metric=&range_days=&note_id=&bucket=` 透传） |
| `/api/v1/dashboard/ranking` | **仅 GET** | **此前**：笔记排行（`?metric=&limit=` 透传） |
| `/api/v1/reports/weekly` | **仅 GET** | **此前**：周报历史（`?limit=` 透传；后端无 POST 到该路径） |
| `/api/v1/reports/weekly/generate` | **仅 POST** | **此前**：跑周报（同步阻塞），`request.text` 原样透传，**timeoutMs = 120000** |
| `/api/v1/reports/weekly/[run_id]` | **仅 GET** | **此前**：单条周报详情（后端无 PUT/PATCH/DELETE） |
| `/api/v1/knowledge` | **仅 GET** | **此前**：知识块列表（`?chunk_type=&status=&limit=&offset=` 透传） |
| `/api/v1/knowledge/extract` | **仅 POST** | **此前**：跑规律提炼（同步阻塞，无请求体），**timeoutMs = 120000** |
| `/api/v1/knowledge/search` | **仅 POST** | **此前**：检索测试（后端无 GET 到该路径），`request.text` 原样透传，**timeoutMs = 120000** |
| `/api/v1/knowledge/[id]` | GET、PATCH | **此前**：详情 / 归档与恢复（仅 `status`）；**此前** PATCH 扩展为可编辑 `title` / `content`（正文变化触发重向量化）；后端无 PUT/DELETE |
| `/api/v1/knowledge/manual` | **仅 POST** | **此前**：手动创建知识块（六类型 + 可选人设关联），`request.text` 原样透传，**timeoutMs = 120000**（可能向量化） |
| `/api/v1/knowledge/seed-forbidden-words` | **仅 POST** | **此前**：幂等同步内置违禁词表（`?dry_run=` 透传），纯 DB 批量写，**timeoutMs = 60000** |
| `/api/v1/knowledge/import-text` | **仅 POST** | **此前**：文档导入·文本粘贴，`request.text` 原样透传，**timeoutMs = 120000** |
| `/api/v1/knowledge/import-file` | **仅 POST** | **此前**：文档导入·.txt/.md 上传（multipart **以 FormData 原样转发**，不手设 Content-Type），**timeoutMs = 120000** |

**超时策略**：`ProxyInit.timeoutMs?: number`（可选）→ `AbortSignal.timeout(init.timeoutMs ?? 15000)`。
默认 15000ms 不变（既有调用一律不传、行为不变）；同步长耗时接口放大：选题生成 120s、热搜刷新 60s、
**截图识别 120s**、**诊断生成 120s**、**周报生成 120s**、
**规律提炼 120s**。POST 请求体用 `await request.text`
**原样转发**（此前起也支持 `formData` 分支——multipart 时**不手设** `Content-Type`，让 fetch 自带
boundary）；详情路由用 `encodeURIComponent(id)` 编码后再拼路径。

## 3. 热点状态标记口径（本次唯一后端改动）

- 枚举 `HotspotStatus`：`new`（新）/ `adopted`（已采用）/ `ignored`（已忽略）—— 存储为 VARCHAR（`native_enum=False`），**无需迁移**；
- `PATCH /api/v1/hotspots/{hotspot_id}`：仅**一个字段** `status`；**三态可人工互逆切换**（无状态机限制）；
- 实现：`crud_hotspot.set_status` —— `setattr` + `commit`，**刻意不走 `CRUDBase.update`**
 （其 `status` 分支会命中 `archived_status`，对热点语义不成立）；
- **不物理删除、不改其他字段、不联动 `topics`**；不存在 → 404；非法值 → 422（Pydantic）。
- 页面口径：雷达列表仅展示该状态；行内「采用 / 忽略」（`new` 行）与「重置为新」（非 `new` 行）均经 `ConfirmDialog` 二次确认后 PATCH，成功后 `Notice` 提示并刷新列表。

## 4. 选题审核状态机口径（此前已定，前端只调用）

- `TopicStatus`：`draft / approved / writing / published / discarded`；
- **本次可人工操作的仅两条**：`draft → approved`（「通过」）、`draft → discarded`（「弃用」）；
- 其余流转与「改回」**不在本次范围**（后端同样拒绝：非法流转 → 422；前端仅在 `draft` 行显示这两个按钮，其余状态只给「查看」）；
- 除状态外，`draft` 行可「编辑」文字字段（标题/摘要/角度/支柱/大纲/标签/关键词/审核备注）——PATCH 部分更新；
- **接口不返回 `llm_raw`**，页面对应的详情 / 编辑 / runs 也就不可能展示它。

## 5. 页面 URL 查询参数

`/topics` 支持从雷达页/外部带参跳转：

| 参数 | 取值 | 行为 |
| --- | --- | --- |
| `hotspot_id` | UUID | 追加为列表请求的过滤条件（按来源热点筛选），并在页面顶部显示提示条 + 「清除该筛选」（`router.replace("/topics")`） |
| `status` | `all` 或合法枚举值 | 作为初始状态筛选（如结果页「去选题库查看」跳 `/topics?status=all`）；非法值忽略、回落默认 `draft` |

> ⚠️ Next 16 要求：`useSearchParams` 必须包裹在 `<Suspense>` 中（否则构建失败）。
> 因此 `/topics` 页面把用到它的部分拆为子组件 `TopicsInner`，外层 `<Suspense>` 包裹。

`/hotspots` 的筛选状态（来源 / 状态 / 归属日期 / 关键词 / 排序）为页内状态，**不入 URL**。

## 5b. 封面工作室页（`/covers`，此前）

**页面结构（三栏 grid `xl:grid-cols-[380px_minmax(0,1fr)_380px]`）**：

1. 顶部**笔记选择器**：`GET /api/v1/notes?page=1&page_size=100&status=all`；
 未选笔记时右侧候选区显示占位提示（自检功能**不依赖**笔记选择，可离线跑）；
2. 左栏**编辑表单**：主标题 / 副标题 / 标签（TagInput）/ 署名 / 页码 / 模板（Select）/
 配色（radiogroup）/ 背景图（`<input type="file">`，仅 jpg/jpeg/png/webp）；
3. 中栏**实时预览**：1242×1656 画布，外框按舞台宽度 `transform: scale` 等比缩放
 （`ResizeObserver` 驱动；缩放只影响观感，**导出直接读画布节点，像素恒 1242×1656**）；
4. 右栏**操作 + 候选库**：保存为候选（POST）/ 下载 PNG（纯前端）/ 封面自检；
 候选缩略图 = 同款画布 `scale(110/1242)` 实时渲染（选中的带「当前」徽标）。

**数据流**：

| 动作 | 请求 | 备注 |
| --- | --- | --- |
| 选笔记 → 拉候选 | `GET /api/v1/covers?note_id=…` | 返回 `{note_id, selected_id, items}` |
| 保存为候选 | `POST /api/v1/covers` | 提交**配置**（模板/配色/文字/标签 `config_json`），非 PNG |
| 选中候选 | `PATCH /api/v1/covers/{id}` `{is_selected:true}` | 后端单事务互斥；**不改** `notes.status` |
| 下载 PNG | **无请求** | `exportPng.ts` 本地导出 + 浏览器下载 |

**URL 参数与自动化钩子**：

| 参数 / 钩子 | 行为 |
| --- | --- |
| `?note_id=<UUID>` | 笔记选择器预选该笔记（供外部跳转 / 自动化使用） |
| `#autotest` | 打开页面后自动跑一遍**封面自检**，结果写入 DOM：面板带 `data-selftest-done="1"` + `data-selftest-summary`（JSON 计数），另附隐藏 `<pre data-selftest-json>`（逐项明细） |

**⚠️ 关键口径**：

- **背景图（dataURL）仅存在于编辑会话内存**：不入 `config_json`、不落库、不上传
 （「保存为候选」提交的 `config_json` 里**不含** `bg_image`）；
- 候选**缩略图与导出**都是前端按配置实时渲染 —— 后端不存图片，`covers.image_path` 恒 NULL；
- 模板 / 配色对未知值**回退第一项**（脏数据不白屏）；
- 主标题自适应（`fitTitle`）在渲染后用**真实 DOM 测量**定档，详见 `docs/cover_module.md` 第四节。

## 5c. 创作台（`/notes` 列表 + `/notes/[id]` 创作台，此前）

**两页职责**：`/notes` 是「笔记总列表」（进入创作台的入口）；`/notes/[id]` 是「单篇工作台」
（编辑 / 预览 / 封面只读 / 复制搬运 / 手动发布闭环）。**两页都不提供自动发布。**

**`/notes` 列表页**：状态 Tabs（全部 / 草稿 / 待发布 / 已发布）+ 标题搜索 `q` + 分页（10/页），
筛选为页内状态**不入 URL**（无需 Suspense）；7 列（标题（Link）/ 状态 / 合规（`warning` 附命中数）/
标签（前 3 + `+N`）/ 人设 / 更新时间 / 操作）；已发布行展示 `publish_url` 外链（`noopener noreferrer`）。

**`/notes/[id]` 创作台（三栏 `xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)_minmax(0,0.85fr)]`）**：

| 区域 | 内容 | 关键口径 |
| --- | --- | --- |
| 编辑 | 标题（100 字上限 + 计数）/ 正文 Textarea / `TagInput` / 「保存修改」 | PATCH **仅** `title/body/tags`；空正文、空标签提交 `null`；成功后重拉详情；违禁词命中仅提示、不阻断 |
| 预览 / 封面 | `whitespace-pre-wrap` 原样换行（**不引 markdown 库**）；选中封面 150px 只读缩略图（`CoverCanvas` 按 `config_json` 实时渲染）；「去做 / 下载封面」→ `/covers?note_id=` | 封面拉取失败**不阻塞**主流程 |
| 发布 | 红线说明卡 / **复制标题·正文·标签**（`navigator.clipboard` + 失败兜底提示）/ 「前往小红书发布页 ↗」外链 / 状态机按钮（按 `status` 互斥展示） | 复制的是**已保存版本**；标签输出 `#词1 #词2`；外链**只导航** |

**数据流**：

| 动作 | 请求 | 备注 |
| --- | --- | --- |
| 列表 / 过滤 | `GET /api/v1/notes?page=&page_size=10&status=&q=` | `status=all` 时省略该参数 |
| 打开详情 | `GET /api/v1/notes/{id}` | 表单同步回填（`reloadToken` 驱动重拉） |
| 保存修改 | `PATCH {title, body, tags}` | 不带 `status` ⇒ 编辑分支 |
| 定稿 / 退回 / 撤销 | `PATCH {status: "ready"｜"draft"}` | 状态机非法流转 → 400（`details.allowed`） |
| 标记已发布 | `PATCH {status:"published", publish_url, xhs_note_id}` | 两个发布字段可留空（`null`） |
| 封面候选 | `GET /api/v1/covers?note_id=` | 只读展示选中项 |
| 复制 / 发布页跳转 | **无请求** | 剪贴板在浏览器内存；外链仅导航 |

**状态机口径**：`draft →(定稿)→ ready →(我已发布)→ published`；`ready→draft`（退回）、
`published→ready`（撤销，二次确认后清空发布三列）——非法跳级一律 400；`draft/ready` 阶段
来源 topic 恒 `writing` 不动；完整规则（含 topic 联动与「尽力而为」语义）见 `docs/studio.md` 第二节。

**红线**：`https://creator.xiaohongshu.com/publish/publish` 是全前端**唯一**小红书地址常量
（`note-studio-client.tsx`），仅 `<a target="_blank" rel="noopener noreferrer">`；
**绝不 fetch / 不携带内容 / 后端零域名**；复制与发布页跳转均**不产生网络请求**。

**动态段结构（Next 16）**：`/notes/[id]` = Server 壳 `page.tsx`（`export const instant = false` +
`<Suspense>`）+ Client 组件 `note-studio-client.tsx`；全局 `site-header.tsx` 已重构为
`HeaderShell` + `HeaderWithPathname` + `<Suspense>`（详见第 6 节与 `docs/studio.md` 第七节）。

## 5d. 数据回填页（`/metrics`，此前）

**页面结构（自上而下三块）**：

1. **笔记选择器**：`GET /api/v1/notes?page=1&page_size=100&status=published` —— **只列已发布**
 （数据回填针对已发布作品）；支持 `?note_id=<UUID>` 预选（供 `/notes` 列表与创作台「回填数据」入口直达）；
2. **新增数据快照**（Tabs 两种录入，最终都走**同一份可编辑表单** → `POST /api/v1/metrics`）：
 - ① **手工录入**：采集时刻（`datetime-local`，默认当前）+ 六指标数字输入（可留空 = 未获取；
 留空提交为 `null`，绝不臆造）；
 - ② **截图识别**：原生 `<input type="file">`（accept `image/png,image/jpeg,image/webp`）→
 `POST /api/v1/metrics/recognize`（multipart）→ 候选**回填表单** + 提示条列出
 missing / uncertain / warnings；**识别结果绝不自动落库**，必须人工核对后点「保存快照」；
 （可选）「留档截图」勾选框 → 仅当后端 `SAVE_SCREENSHOT=true` 时服务端才留存（默认不保存）；
3. **数据时间线**：`GET /api/v1/metrics?note_id=`（采集时刻倒序表格：时刻 / 来源 Badge /
 六指标（千分位，空值「—」）/ 截图留档（默认「—」））。**趋势图属此前，本页不做。**

**数据流**：

| 动作 | 请求 | 备注 |
| --- | --- | --- |
| 打开页面 / 选笔记 | `GET /api/v1/notes?status=published`、`GET /api/v1/metrics?note_id=` | 选择器只列已发布；时间线随选择刷新 |
| 截图识别（**不写库**） | `POST /api/v1/metrics/recognize`（FormData） | 120s 超时；422 文件不合法 / 400 视觉未配置或不支持图片 |
| 保存快照 | `POST /api/v1/metrics` | 201；同笔记同时刻冲突 **409**（页面提示改「采集时刻」） |

**URL 参数**：`?note_id=<UUID>` 预选笔记（`useSearchParams` 已包 `<Suspense>`——沿用此前约定）。

**空值口径**：六个数字输入留空提交为 `null`（「未获取」），时间线显示「—」；提交前完成非负整数校验
（含则表单错误提示，不发请求）。

**入口**：导航第 7 入口「数据回填」；`/notes` 列表已发布行「回填数据」；创作台（published 时）
「数据回填 →」——均跳 `/metrics?note_id=<id>`。

## 5e. 单篇诊断页（`/diagnostics`，此前）

**页面结构（自上而下三块）**：

1. **顶部操作区**：笔记选择器（`GET /api/v1/notes?page=1&page_size=100&status=published` ——
 **只列已发布**；`?note_id=<UUID>` 预选；不在列表的当前笔记如实标注「当前笔记（不在已发布列表）」）+
 诊断类型选择（**自动判定（推荐）/ 冷启动（数据不足）/ 复盘（常规）/ 异常（明显负增长）**，
 默认自动）+「生成诊断」按钮（生成期间禁用并提示「诊断中……（最长约 2 分钟）」）；
 状态栏显示「当前笔记：X；已有诊断 N 条」；
2. **结果区**（生成成功或点历史「查看」后展示）：
 - **数据事实卡**（标注「**由程序计算，非模型生成**」）：最新六指标 / 六个互动率（百分比，`null` 显示
 「—」）/ 环比总增量与增长率 / 覆盖时长（小时）/ `based_on` 快照说明；
 - **综合评分**：`0~100` 数字；`null` → 显示「**数据不足，未评分**」（不硬打分）；
 - **亮点 / 问题 / 原因分析 / 建议**四个分区（建议每条带「复制」按钮：`navigator.clipboard` +
 隐藏 textarea 兜底，复制失败给手动提示）；
 - **基准与数据缺口**（amber 显著提示）：「仅基于该笔记自身数据、无行业 / 爆款基准（跨篇基准此前）」
 + `benchmark_note` + `data_gaps` 列表 + 元信息（生成时间 / 模型 / `prompt_version` / `parse_attempts`）；
3. **诊断历史**：`GET /api/v1/diagnostics?note_id=`（时间倒序表格：时间 / 类型 / 评分 / 判定来源
 自动·手动 / 「查看」按钮 → `GET /{id}` 回填结果区）。**诊断历史只增不改、无编辑 / 删除**。

**数据流**：

| 动作 | 请求 | 备注 |
| --- | --- | --- |
| 打开页面 / 选笔记 | `GET /api/v1/notes?status=published`、`GET /api/v1/diagnostics?note_id=` | 选择器只列已发布；历史随选择刷新 |
| 生成诊断 | `POST /api/v1/diagnostics/generate` | 201；**120s 超时**；400（未配置 / 结构超限）/ 404（笔记不存在） |
| 查看历史单条 | `GET /api/v1/diagnostics/{id}` | 结果区回填；不存在 → 404 |

**URL 参数**：`?note_id=<UUID>` 预选笔记（`useSearchParams` 已包 `<Suspense>`——沿用此前约定）。

**入口**：导航第 **8** 入口「单篇诊断」；`/notes` 列表已发布行「诊断」；创作台（published 时）
「单篇诊断 →」——均跳 `/diagnostics?note_id=<id>`。**未新增任何平台域名地址**。

## 5f. 运营看板页（`/dashboard`，此前）

**定位**：确定性看板（零 LLM、只读）+ LLM 周报（只写 `agent_runs`，不改任何业务表）。页面为
**Suspense 壳 + Client 内容**（`page.tsx` 内 `DashboardPage` 包 `DashboardInner`），build 产物
**○ 静态**（无动态段）。

**纯 SVG 图表组件**（`src/lib/charts/`，**不引任何图表库**；只接收已聚合 props，不发请求、不做二次算术）：

| 组件 | 说明 |
| --- | --- |
| `LineChart.tsx` | 折线：`value=null` **断线不补点**（禁止插值）；每点 `<title>` 悬浮显示「桶 + 值」；ResizeObserver 自适应宽度、高度固定；空态「暂无数据」；x 轴标签自动抽稀 |
| `HBars.tsx` | 横向条形：按最大值归一；零值 / 缺失占位；可配 `href`（排行点标题跳 `/notes/{id}`） |
| `FunnelBars.tsx` | 漏斗六指标逐级横条 + 对应率；`views` 合计 ≤ 0 → 显示「浏览量合计为 0，各率不可计算」 |

**页面结构**（Tabs `看板 / 周报`，顶部时间范围选择默认 30 天）：

1. **看板 Tab**：
 - KPI 卡行（七项：账号总数 / 笔记总数 / 已发布 / 待发布 / 草稿 / 在写选题 / 周期内发布）→
 `GET /api/v1/dashboard/summary?range_days=`；
 - **漏斗卡**：全账号最新六指标合计 + 六率 + 覆盖率说明（「X/Y 篇已发布笔记有指标数据；
 缺失按 0 参与合计」，`views=0` 显示不可计算）；
 - **趋势卡**：指标切换（六项）+ 单笔记 / 全账号切换；`LineChart`；空桶断线（不补 0）；
 底部注明「分桶 / 覆盖 / 空桶缺线展示」；
 - **排行卡**：指标切换 + `HBars`；**只列已发布**，点标题跳 `/notes/{id}`；
2. **周报 Tab**：
 - **生成卡**：常用周期（近 7 天默认 / 近 14 天 / 近 30 天）+ 显式起始 / 结束日期（可选，
 二选一语义：只用显式起止时不采常用周期）+「生成周报」（期间禁用并提示「最长约 2 分钟」，
 **BFF 120s 超时**；400 时提示检查 LLM 开关 / key / 模型占位）；
 - **报告展示卡**：「周期复盘」标题 + 周期徽章 + 状态 + 周期概览 / 亮点 / 问题 / 数据依据 /
 **下周建议（按重要性排序，逐条「复制」——与诊断页同款双兜底）** / 数据缺口（amber 卡，
 明示「本期无外部行业基准，跨篇基准见此前」）+ 元信息（置信度 / JSON 解析尝试 / 模型 /
 查看编号）；
 - **周报历史**：`GET /api/v1/reports/weekly`（生成时间 / 周期 / 概览 / 建议数 / 缺口数 /
 「查看」→ `GET /{run_id}` 回填）。**周报只增不改、无编辑 / 删除**。

**数据流**：

| 动作 | 请求 | 备注 |
| --- | --- | --- |
| 打开页面 / 切范围 | `GET /api/v1/dashboard/summary`、`/trend`、`/ranking` | 三个只读聚合，默认 15000ms 超时 |
| 切周报 Tab | `GET /api/v1/reports/weekly` | 历史列表（挂载 + 生成后刷新） |
| 生成周报 | `POST /api/v1/reports/weekly/generate` | 201；**120s 超时**；400（未配置 / 结构超限）/ 422（周期非法） |
| 查看历史单条 | `GET /api/v1/reports/weekly/{run_id}` | 报告卡回填；不存在 → 404 |

**进入方式**：导航第 **9** 入口「运营看板」。**未新增任何平台域名地址**（全前端域名仍恰 1 处：
此前发布外链常量）。

## 5g. 知识库页（`/knowledge`，此前）

**定位**：爆款规律 RAG 的人工面 —— 只读浏览 + 手动触发提炼 + 检索测试；知识块**只增 / 归档、
无物理删除**。页面为 **Suspense 壳 + Client 内容**（`page.tsx` 内 `KnowledgePage` 包 `KnowledgeInner`），
build 产物 **○ 静态**（无动态段）。**规律只来自本账号本地数据**（自己的已发布笔记 + 数据回填 +
单篇诊断），不爬竞品、不引入行业均值等外部数字；接口**不返回** embedding 向量与 `llm_raw`。

**页面结构**（Tabs `知识块 / 提炼规律 / 检索测试`；新增「文档导入」Tab 并扩展管理能力，见 **5h**）：

1. **知识块 Tab**：
 - 筛选行（类型 `chunk_type` + 状态 `status`）+ 分页（每页 20，`Pagination`）；
 - 表格列：标题 / 类型徽章 / 样本数 / 置信度 / 状态 / 时间；行操作「查看」（`Dialog`：全文、
 适用范围、样本量、置信度、指标证据摘要、来源笔记链接、复制全文）与「归档 / 恢复」
 （`ConfirmDialog` 二次确认后 PATCH；归档后不再参与检索）；
 - 空态：「暂无知识块：去「提炼规律」Tab 从已发布笔记中提炼爆款规律。」
2. **提炼规律 Tab**：
 - 卡头「提炼爆款规律（第六个业务图）」；样本情况行：当前已发布笔记数 / 样本门槛
 （`INSIGHTS_MIN_PUBLISHED`，展示用 `GET /api/v1/dashboard/summary` 计数，**仅展示、不拦按钮**，
 真正的门槛判定在后端）；
 - 「提炼爆款规律」按钮：期间禁用并提示「提炼中…（最长约 2 分钟）」；按钮下注明「首次运行会
 加载 / 下载本地向量模型（约 1.3GB）；样本不足时不调用模型、不写入」；
 - 结果卡：新增 / 复用 / 跳过 / 拒绝条数 + 样本数 / 门槛 + JSON 解析尝试 + `run_id`；
 **样本不足**（`insufficient_sample`）时展示 partial 说明与「至少发布 N 篇并回填数据」；
 - 错误兜底：400 时提示「向量化未就绪（依赖未安装 / 模型未下载）或 LLM 未配置」。
3. **检索测试 Tab**：
 - 输入：查询文本 + 类型（全部 / 爆款规律 / 复盘心得）+ k（3 / 5 / 10 / 20，默认 5）；
 - 结果：top-k 列表按相似度降序（标题 / 类型 / 相似度四位小数 / 内容摘要）；
 - 空态：「无命中：知识库中还没有相似内容（可先提炼规律，或换个说法再试）。」；
 - 用途：人工验收 RAG 召回质量；**永不展示向量原始值**。

**数据流**：

| 动作 | 请求 | 备注 |
| --- | --- | --- |
| 打开页面 / 筛选 / 翻页 | `GET /api/v1/knowledge` | `?chunk_type=&status=&limit=&offset=`；默认 15000ms 超时 |
| 查看详情 | `GET /api/v1/knowledge/{id}` | Dialog 回填；不存在 → 404 |
| 归档 / 恢复 | `PATCH /api/v1/knowledge/{id}` | 仅 `status`（active / archived）；无 PUT / DELETE；**此前**同一 PATCH 扩展支持 `title` / `content`（见 5h） |
| 提炼规律 | `POST /api/v1/knowledge/extract` | **201**；**120s 超时**；400（向量化未就绪 / LLM 未配置 / 结构超限）；样本不足为 201 + partial |
| 检索测试 | `POST /api/v1/knowledge/search` | 200；`{query, chunk_types?, k?}`；**120s 超时**（首次会加载本地模型）；向量化未就绪 → 400（如实告知，不假装无命中） |

**进入方式**：导航第 **10** 入口「知识库」。**未新增任何平台域名地址**（全前端域名仍恰 1 处：
此前发布外链常量）。

## 5h. 知识库管理扩展（`/knowledge`，此前）

**定位**：把知识库从「只读浏览 + 提炼」升级为**可人工维护** —— 手动新建 / 编辑知识块、
违禁词入库统一管理、人设知识、文档导入。页面壳与原有三 Tab 在 `page.tsx`，
新增的四个交互组件集中在同目录 **`app/knowledge/knowledge-admin.tsx`**（单文件承载，便于审计）。

**页面变化**：

1. **知识块 Tab 扩展**：
 - 顶部新增「新建知识块」按钮 → `ManualCreateDialog`（类型六选一 / 标题 / 正文 /
 违禁词类别 / 可选关联人设；`persona_knowledge` 强制要求选人设，未选时前端即拦截）；
 - 行操作新增「编辑」（`EditSnippetDialog`：改标题 / 正文；正文变化时提示「保存将触发重新向量化」；
 重向量化失败时后端置空向量并回 `warnings`，页面以 Notice 展示、**正文不丢**）；
 - `forbidden_word` 行在类型徽章下展示「类别：…」（取自 `meta.category`）；
 - **类型筛选 = 违禁词时**，筛选行下方出现 `ForbiddenAdminBar`：
 「同步内置词表（seed）」幂等按钮（结果 Notice 展示 新增 / 复用 / 词表总数）+
 「新增违禁词」入口（复用创建 Dialog、预设 forbidden_word）；归档 / 恢复用行内既有按钮。
2. **新增「文档导入」Tab（第 4 个 Tab）**：`ImportTab`
 - 导入方式：粘贴文本 / 上传文件（`.txt` / `.md`，UTF-8，≤1MB）；
 - 导入为：`general`（默认）或 `persona_knowledge`（须选人设）；
 - 可选切分参数：块大小（200~4000，留空 800）/ 块间重叠（0~500，留空 100），前端做同界校验；
 - 结果卡：切分总块数 / 写入 / 跳过（已存在）/ 失败四项计数 + 实际切分实现 + 参数 + 失败明细
 （后端逐块如实计数）；400 时给「向量化未就绪 / 文件不合规」人话提示。

**数据流**（新增 4 条）：

| 动作 | 请求 | 备注 |
| --- | --- | --- |
| 新建知识块 | `POST /api/v1/knowledge/manual` | **201**；违禁词不向量化（离线可建）；其它类型未就绪 → 400（零写入） |
| 编辑知识块 | `PATCH /api/v1/knowledge/{id}` | `{title?, content?}`；正文变化 → 后端重算内容指纹 + 重向量化；仅改 status 时与此前一致 |
| 同步内置词表 | `POST /api/v1/knowledge/seed-forbidden-words` | 200；`{total, inserted, reused, would_insert, dry_run}`；幂等可重复点 |
| 文档导入 | `POST /api/v1/knowledge/import-text`（JSON）/ `import-file`（FormData） | **201**；**120s 超时**；结果如实计数 failed / skipped |

**组件与范式**：两个 Dialog 由父级 **`key` 每次打开重新挂载**（同 `TopicGenerateDialog` 范式，
避免 `react-hooks/set-state-in-effect`）；人设下拉由 **`usePersonaOptions(enabled)`** 统一加载
（`GET /api/v1/personas?status=active&page_size=100`，仅 Dialog 打开 / 导入 Tab 挂载时拉取）。
**导航与整体 Suspense/Shell 结构不变**（仍第 10 入口「知识库」）；**未新增任何平台域名地址**。

## 6. Next 16 / 运行注意

- 开发与手测一律用 **`http://localhost:3000`**（`127.0.0.1:3000` 会被视为跨源拦截 dev 资源）；
- `cacheComponents: true` 下 Route Handler **默认即动态**——**不要**写 `export const dynamic`；
- 遵守 `react-hooks/set-state-in-effect`：数据请求放在 effect 内的异步 IIFE，**首次 await 之前不 setState**；加载态由事件处理器显式打开（`applyQuery` / `reload`），配 `cancelled` 标志防竞态；
- **动态段页面（此前 `/notes/[id]` 起）走「三件套」**：① Server 壳导出 `export const instant = false`
 （route segment config **只能由 Server Component 导出**，写在 `"use client"` 文件里会直接报错）；
 ② 交互实现整体拆为 Client 组件、壳内 `<Suspense>` 包裹；③ 所有运行时 URL hook
 （`useParams` / 全局 `usePathname`）外层**必须有 `<Suspense>`**（全局导航已重构为
 `HeaderShell` + `HeaderWithPathname` + Suspense，fallback 视觉一致）。结果构建为
 **◐ Partial Prerender**；否则 build 依次报 `blocking-prerender-client-hook` →
 `client rendering outside a Suspense boundary`。详见 `docs/studio.md` 第七节；
- 交互页顶部一律 `"use client"`；
- 生成 Dialog 由父级用 `key` 重新挂载（打开即全新状态），避免「打开时重置」的 effect。

## 7. 共享展示件与导航

- `src/components/shared.tsx`：从 `accounts/page.tsx` **原样搬移**并于三页复用的通用件 ——
 `LoadState<T>` / `INITIAL_LOAD` / `toApiError` / `formatDateTime` / `formatNumber`（热度千分位）/ `formatScore`（0~1 两位小数）/
 `ErrorText` / `Notice` / `Field` / `Pagination` / `ConfirmDialog` / `TagInput`；
- `src/components/site-header.tsx`：顶部导航（此前四入口 → 此前五入口「封面」→
 此前六入口「创作台」→ 此前七入口「数据回填」→ 此前八入口「单篇诊断」→
 此前九入口「运营看板」→ **此前十入口「知识库」**）；结构为
 `HeaderShell`（纯展示，无 hook）
 + `HeaderWithPathname`（读 `usePathname`）+ 外层 `<Suspense>`（动态段预渲染所需），`aria-current="page"` 高亮；
- `src/components/topic-generate-dialog.tsx`：两页共用的生成 Dialog（雷达页传 `hotspotIds` 固定单热点；选题库页不传、走后端自动取热点）。

## 8. 依赖声明

**此前零新增依赖**：npm 未新增任何包（未加 swr / react-query / axios / 表单库 / 日期库）；
UI 组件仅复用既有 shadcn(base-nova/@base-ui/react)；开关用原生 `<input type="checkbox">`，日期用原生 `<input type="date">`。

**新增 1 个 npm 依赖（精确锁定）**：

- `html-to-image@1.11.13`（`npm install --save-exact`）—— 封面 DOM → PNG 截图的**唯一**依赖，
 只在 `src/lib/cover/exportPng.ts` 里 import（全站唯一绑定处，换库只改这一个函数）；
- 替代 spike 的 vendor UMD / CDN 方案：**打包含在 bundle 内，断网可导出**；
- Python：此前零新增；**仍无新迁移**（`0001` / `0002` 之外零变更，`covers` 表由 `0001` 建好）。

**此前零新增**：npm 0 个 / Python 0 个 —— 未加 markdown / 剪贴板 / 富文本编辑器类库；
复制用原生 `navigator.clipboard`（失败给手动兜底提示），预览用 `whitespace-pre-wrap` 原样换行；
**仍无新迁移**（`notes` 的 `status` / `published_at` / `publish_url` / `xhs_note_id` 四列早在 `0001` 建好，本次首次使用）。

**此前前端零新增**：npm 0 个 —— 未加上传 / 表单 / 图表 / 日期类库；数字输入用原生
`<input type="number">`、采集时刻用原生 `<input type="datetime-local">`、文件用原生 `<input type="file">`；
multipart 转发用原生 `FormData`（`proxyToBackend` 新增 `formData` 分支，不手设 `Content-Type`）。
**后端仅 +`python-multipart`**（FastAPI `File/Form` 解析 multipart 的**必需**依赖，非可选）；
**仍无新迁移**（`note_metrics` 由 `0001` 建好、本次首次使用；`metric_recognize` 为 `native_enum=False`
的枚举成员，**无 DDL**）。

**此前前端零新增**：npm 0 个 —— 未加剪贴板 / 图表 / 表单类库；「复制建议」用原生
`navigator.clipboard`（失败降级隐藏 textarea + `execCommand`，再失败给手动提示）；下拉用既有
base-nova `Select`。**后端 requirements 零新增**（无 multipart / 无新库）；**仍无新迁移**
（`diagnostics` 由 `0001` 建好、本次首次写入；`RunType.DIAGNOSIS` 为 `native_enum=False` 的枚举成员，
**无 DDL**）。

**此前前端零新增**：npm 0 个 —— **看板图表全部纯 SVG / CSS 自绘**
（`src/lib/charts/LineChart.tsx` `HBars.tsx` `FunnelBars.tsx`，**未引 recharts / echarts / chart.js / d3
等任何图表库**）；下拉 / Tabs / 徽章复用既有组件；数字格式化沿用 `shared.tsx`。
**后端 requirements 零新增 / Python 0 个**（预聚合与周报图全用既有依赖）；**仍无新迁移**
（看板纯只读；周报只写 `agent_runs`；`RunType.WEEKLY_REPORT` 为 `native_enum=False` 的枚举成员，
**无 DDL**；`WEEKLY_SCHEDULER_ENABLED` 默认 false，开启时并入唯一 lifespan）。

**此前前端零新增**：npm 0 个 —— 知识库页（三 Tabs）复用既有 Tabs / Dialog / Select / Table /
`shared.tsx`；「复制全文」沿用诊断页双兜底写法；检索结果与详情**不含任何向量渲染**。
**后端仅 +2 个 Python 包**：`langchain-huggingface`、`sentence-transformers`（本地 bge 向量化的
必需依赖，非可选；见 `README` 此前专章与 `docs/rag_knowledge.md`）。
**仍无新迁移**：`knowledge_snippets.embedding vector(1024)` 由 `0001` 基线建好（bge-large-zh-v1.5
输出恰 1024 维）；`RunType.INSIGHT` / `ChunkType.HIT_PATTERN` / `REVIEW_INSIGHT` 均为
`native_enum=False` 的枚举成员，**无 DDL**；migrations/versions 仍恰 2 支（`0001` / `0002`）。
