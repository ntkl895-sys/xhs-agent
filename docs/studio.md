# 创作台 · xhs-agent

> 范围：**人工编辑 + 复制搬运 + 手动发布闭环** —— 把此前产出的 `notes` 草稿变成
> 「可人工发布」的工作台。全步**零 LLM 调用 / 零数据库迁移 / 零新增依赖**；确定性工程，
> 唯一状态权威是 `notes.status` 状态机 + `PATCH /api/v1/notes/{note_id}` 一个接口。
> 本文件给「改了什么 / 口径与红线 / 怎么验证 / 边界在哪」；页面数据流另见 `docs/frontend_pages.md`（第 5c 节）。

---

## 一、定位与边界

- **它是什么**：AI 草稿 → 人工编辑（标题 / 正文 / 标签）→ 状态机定稿 → 复制内容 →
 到小红书**人工发布** → 回系统**标记「我已发布」**（可选回填链接与笔记 id）→ 可撤销。
- **它不是什么**：不做自动发布、不登录小红书、不抓取 / 保存账号密码 / Cookie / Token、
 不向任何小红书域名发起网络请求，**发布与否完全由人决定**。
- **唯一被允许的小红书地址**：前端常量 `XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"`
 （官方发布页，仅 `<a target="_blank" rel="noopener noreferrer">` 导航外链；**绝不 fetch / 不携带内容 /
 后端代码零小红书域名**）。全仓（含本次触碰文件）由自检扫描约束出现次数与位置。
- **零 LLM**：全部为确定性读写与状态流转；不新增 LangGraph 图、不改 `app/agent/` 基座与既有两图。
- **零迁移**：`notes` 表的 `status / published_at / publish_url / xhs_note_id` 四列**早在 `0001` 建好**，
 本次只是首次使用它们；`migrations/versions` 仍恰 2 支。

## 二、笔记状态机（唯一权威口径）

```
 定稿（draft→ready） 确认已发布（ready→published）
 draft ────────────────▶ ready ────────────────────────────▶ published
 ▲ │ ▲ │
 └───────────────────────┘ └───────────────────────────────────┘
 退回修改（ready→draft） 撤销发布标记（published→ready）
```

| 转移 | 触发（前端按钮） | 服务端动作（单事务） |
| --- | --- | --- |
| `draft → ready` | 「标记定稿（待发布）」 | 仅改 `status`；**不动** topic |
| `ready → draft` | 「退回修改」 | 仅改 `status`；**不动** topic |
| `ready → published` | 「我已在小红书发布」Dialog（可回填链接 / 笔记 id） | `status=published` + `published_at=服务端当前时间(UTC)` + 回填 `publish_url` / `xhs_note_id`（均可空）；**联动** 来源 topic `writing → published` |
| `published → ready` | 「撤销发布标记」+ 二次确认 | 清空 `published_at` / `publish_url` / `xhs_note_id` 三列；**联动** 来源 topic `published → writing` |

**硬规则（后端强制，违反一律 400）**：

- **禁止跳级**：`draft → published`（必须经 `ready`）、`draft/published → archived` 均 400；
- **同态重复**（`draft→draft`、`ready→ready`、`published→published`）400（前端不会发出，防脚本误调）；
- **`published → draft`** 400（必须先撤销回 `ready`）；
- draft / ready 阶段来源 topic **恒 `writing` 不得改动**（自检逐项断言）；
- topic 联动是「**尽力而为**」：仅当来源 topic 状态恰好匹配（writing / published）时同步，
 否则跳过也不阻断 note 的流转（topic 可能被人工提前改过，不因此 500）；
- archived 目标本身 400（本项目「归档代替删除」不适用于笔记流转，本次**不提供归档**）。

## 三、后端：`PATCH /api/v1/notes/{note_id}`（编辑 vs 流转分流）

**请求体 `NoteUpdateRequest`**（全字段可选；`extra="ignore"`）：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `title` | ≤ 100 字 | 编辑字段；**显式 `null` → 422** |
| `body` | 任意字符串 | 编辑字段；`null` 表示清空正文（后端存 NULL） |
| `tags` | 字符串数组 | 编辑字段；`null` 表示清空标签 |
| `status` | `draft / ready / published`（`archived` 会被 400 拦） | **流转字段**；显式 `null` → 422 |
| `publish_url` | 字符串 | 仅随 `status=published` 携带 |
| `xhs_note_id` | ≤ 64 字 | 仅随 `status=published` 携带 |

**分流语义（`model_dump(exclude_unset=True)` 判定）**：

1. payload **不带 `status`** ⇒ **编辑**：只允许 `title / body / tags` 三键，
 携带 `publish_url / xhs_note_id` → 400「发布字段仅随 status=published 携带」；全空 payload → 400；
2. payload **带 `status`** ⇒ **流转**：混入编辑字段（title/body/tags）→ 400；
 非 `published` 目标携带发布字段 → 400；`archived` → 400；随后按第二节状态机校验合法性；
3. 两条路径都**先查存在性**：note 不存在 → **404**（先于状态机）；`note_id` 非 uuid → 422；
4. `topic_id` 为 NULL 的笔记：流转照常，仅跳过 topic 联动。

**CRUD 四方法（`app/crud/note.py`，全部单事务）**：

| 方法 | 事务内容 | 违规 |
| --- | --- | --- |
| `update_content(db, note_id, data)` | 仅 `title / body / tags` | 不存在 → None（路由翻译 404） |
| `set_status_draft_ready(...)` | 仅 `status`（draft↔ready） | 非法前态 → `ValueError`（路由翻译 400） |
| `mark_published(..., published_at=None)` | note 四列 + topic `writing→published`，`try/except → rollback → raise` | 前态非 ready → `ValueError` |
| `revert_to_ready(...)` | note 清空三列 + topic `published→writing` | 前态非 published → `ValueError` |

**统一错误体**沿用 `{status, code, message, details}`；状态机 400 的 `details` 附
`{current: ..., target: ..., allowed: [...]}` 便于前端 / 脚本定位。

## 四、前端：`/notes` 列表页

- 状态 Tabs（**全部 / 草稿 / 待发布 / 已发布**）+ 标题搜索 `q` + 分页（`PAGE_SIZE=10`），
 筛选为页内状态**不入 URL**（沿用此前约定，因此无需 `useSearchParams` / `Suspense`）；
- 列：标题（Link 进创作台）/ 状态 Badge / 合规 Badge（`warning` 时附「N 处命中」）/
 标签（前 3 个 + `+N`）/ 人设 / 更新时间 / 操作（「打开创作台」）；
- **已发布行**展示 `publish_url` 外链（`target="_blank" rel="noopener noreferrer"`）；
- 本页**不提供任何发布动作**。

## 五、前端：`/notes/[id]` 创作台（三栏）

| 区域 | 内容 | 关键口径 |
| --- | --- | --- |
| 编辑 | 标题（`maxLength=100` + 字数提示）/ 正文 Textarea / 标签 `TagInput` + 「保存修改」 | `PATCH` 仅 `title/body/tags`；空正文 / 空标签提交 `null`；成功后**重新拉详情**（`reloadToken`）；命中违禁词**只提示不阻断** |
| 预览 / 封面 | 轻量图文预览（`whitespace-pre-wrap` **原样换行、不引入 markdown 库**）；当前选中封面**只读缩略图**（复用 `CoverCanvas` 按 `config_json` 实时渲染，150px 宽）；「去做 / 下载封面」→ `/covers?note_id=` | 封面候选拉 `GET /api/v1/covers?note_id=`；失败不阻塞主流程 |
| 发布 | 红线说明卡 + **复制标题 / 复制正文 / 复制标签**（`navigator.clipboard`，失败给手动复制兜底提示）+ 「前往小红书发布页 ↗」外链 + 状态机按钮（按 `status` 互斥展示：定稿 /（我已发布 + 退回）/（发布信息 + 撤销）） | 复制的是**已保存版本**（tags 输出 `#词1 #词2` 格式）；`published` 区展示 `published_at / publish_url / xhs_note_id` |

**Dialog**：「我已在小红书发布」——两个输入（作品链接 / 笔记 id）**均可留空**，
确认后 `PATCH {status:"published", publish_url, xhs_note_id}`；「撤销发布标记」走 `ConfirmDialog` 二次确认。

## 六、红线的工程落实（可复核）

| 要求 | 落实 | 复核方式 |
| --- | --- | --- |
| 不自动发布 | 全步无任何发布类请求；「已发布」只是本地状态记录 | OpenAPI 无相关接口；E2E 断言 |
| 不登录 / 不存凭证 | 代码与库中无 `cookie / token / session` 字段；扫描 `Cookie / access_token / sk-` 等模式 0 命中 | `selftest_studio.py` 组 9 |
| 唯一小红书地址 | 前端常量出现**恰 1 次**（`note-studio-client.tsx`）；**后端零域名** | 同上（全仓扫描断言） |
| 外链安全 | `target="_blank" rel="noopener noreferrer"`；**只导航**（E2E 只读属性、不点击） | E2E 4 项属性断言 |
| 复制搬运 | 剪贴板只在浏览器内存；不落库、不上传 | 代码审查（无对应网络请求） |

## 七、Next 16：动态段预渲染的「三件套」约定（本次最大工程坑）

`/notes/[id]` 无法穷举预生成参数，而全局 `SiteHeader` 用到 `usePathname`、页面用 `useParams`，
两者都是**运行时 URL 数据**。build 期静态壳预渲染会依次报
`blocking-prerender-client-hook` → `client rendering outside a Suspense boundary`。
最终解法（**组合拳，缺一不可**）：

1. **Server 壳**：`page.tsx`（Server Component）导出 `export const instant = false`
 （blocking route —— route segment config **只能由 Server Component 导出**；写在 `"use client"` 文件里
 会报「can only be used when the segment is a Server Component module」）；
2. **Client 组件**：交互实现整体拆到 `note-studio-client.tsx`（`"use client"`），
 壳内用 `<Suspense>` 包裹它；
3. **全局导航改造**：`site-header.tsx` 重构为 `HeaderShell`（纯展示，无 hook）+
 `HeaderWithPathname`（读 `usePathname`）+ 外层 `<Suspense fallback={<HeaderShell activeHref={null}/>}>`
 —— **fallback 与高亮前形态视觉一致**，无布局跳变。

结果：`/notes/[id]` 构建为 **◐ Partial Prerender**（静态壳 + 请求时求值动态段），`/notes` 为纯静态 ○。
**后续任何动态段页面照此办理。**

## 八、自检证据（本机实跑）

| 项 | 结果 |
| --- | --- |
| `scripts/selftest_studio.py`（离线，进程内 TestClient + 真实库；10 组） | **86/86 PASS，退出码 0** |
| ↳ 组 1 编辑（含 422/400 五边界 + 落库逐列复核 + 错误请求零副作用） | 21 项 |
| ↳ 组 2 非法流转（draft→published / archived / 同态，全 400 + `allowed` 清单） | 11 项 |
| ↳ 组 3 合法状态链（含 topic 联动两向往返） | 21 项 |
| ↳ 组 4 发布字段可留空 + 第二轮往返 | 9 项 |
| ↳ 组 5 混合 payload / ready→archived / 收尾 topic 恒 writing | 7 项 |
| ↳ 组 6 404 与统一错误体 | 5 项 |
| ↳ 组 7 接口面（`/notes/{note_id}` 恰 GET+PATCH、无 PUT/DELETE、`NoteOut` 无 `llm_raw`） | 8 项 |
| ↳ 组 8 零迁移（2 支 / `0002 (head)` / `alembic check` 退出码 0） | 4 项 |
| ↳ 组 9 合规（禁词扫描 + 小红书地址恰 1 次） | 4 项 |
| **浏览器 E2E（playwright-core + 本机 Chrome，`next start` 生产构建）** | **38/38 PASS**（列表过滤→编辑保存→定稿→发布回填→列表外链→三复制→外链属性→撤销→列表回归；`pageerror` / `console.error` 均空） |
| ↳ 截图留档 | `docs/screenshots/s14_01…s14_05*.png`（列表 / 编辑后 / 已发布 / 列表已发布 / 撤销后） |
| `next build` | 成功；`/notes` ○、`/notes/[id]` **◐**、3 处新增路由 |
| `npx tsc --noEmit` / `npm run lint` | **0 error** / **0 error · 0 warning** |
| 回归矩阵 | `selftest_db` 15 · `verify_schema` 13 · `selftest_api` 37 · `selftest_agent_harness` 58 · `selftest_hotspot` 54 · `selftest_topic_agent` 42 · `selftest_content_agent` **98** · `selftest_cover` 79 · `smoke_crud_api` 37 · `smoke_hotspot` 38 —— 全部 PASS |
| `healthcheck.py`（前后端同时在线） | 四项全绿（后端存活 / 就绪 database ✓ pgvector ✓ redis ✓ / 前端首页 / 前端代理） |
| 迁移 | `alembic current` = `0002 (head)`；`check` 退出码 0；`versions` 恰 2 支；`public` 仍 10 张业务表 |

**测试脚本加固（如实记录）**：`selftest_content_agent.py` 两处此前口径的断言
（「`/notes/{note_id}` 未附带写接口」）在本次新增 PATCH 后过时 → 已提级为
「恰 GET+PATCH，无 PUT/DELETE」，复跑 **98/98**。这是脚本随接口演进的正常加固，非产品缺陷。

## 九、未验证项（如实说明）

1. **剪贴板权限差异**：E2E 在 headless Chrome（已授予 `clipboard-read/write` 权限）下三个复制按钮
 实测成功且读回内容一致；**真实用户浏览器未授权剪贴板时的兜底提示路径**未实测（代码有该分支文案）。
2. **换行符平台差异**：Windows 剪贴板会把正文 `\n` 规范化为 `\r\n`（OS 行为），E2E 断言已按此归一化；
 其它平台未测。
3. **真实发布链接格式**：E2E 用 `https://example.com/…` 假链接验证回填与展示全链路；
 小红书真实作品链接的格式差异不影响系统（仅原样存储）。
4. **外链真实跳转**：E2E 只读取 `href / target / rel` 属性，**未点击**（红线：不对小红书域名发起请求）。

## 十、本次【不做】

- 不提供 `PUT / DELETE`（编辑为部分更新；笔记**不物理删除**——归档语义留待需要时另定）；
- 不提供 `archived` 归档（请求转 archived → 400）；
- 不做自动发布 / 定时发布 / 发布队列；不接小红书任何 API；不引入任何登录态概念；
- 不引入 markdown 渲染库（预览为「原样换行」轻量图文）；
- 不动 `app/agent/` 基座与既有两图、不动 compose / CORS / lifespan（仍只有一个）；
- 不做笔记的多版本 / 协同编辑（`notes.version` 仍恒 1）。
