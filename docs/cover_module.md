# 封面模块 · xhs-agent

> 把 `cover_spike/` 技术验证的**纯前端程序化封面**能力迁移进正式工程：4 套模板 × 5 组配色，
> 1242×1656（3:4）画布，浏览器内实时渲染 + 本地导出 PNG，对接 `covers` 表做**候选库**。
> 本文件说明：定位与边界、技术路线、模板与配色、主标题自适应口径、**字段映射**、
> 单事务互斥选中、HTTP 接口、前端结构、浏览器内自检、自检证据、合规与隐私、不做项。
>
> **一句话**：封面是**确定性模板渲染**（不调 LLM、零成本、不出网），后端只存候选**配置数据**，
> 图片像素由浏览器本机生成后直接下载 —— **不落服务器磁盘、不入库、不上传任何平台**。

---

## 一、定位与边界

- **定位**：为笔记准备**封面候选**。用户在「封面工作室」（`/covers`）里选笔记 → 编辑表单
 → 实时预览 → 保存为候选 → 从候选库里挑一张「选中」。候选数据写入 `covers` 表；
 **图片本身**由前端导出 PNG **直接下载到用户本机**。
- **本次做的**：后端 `schemas/cover.py` + `crud/cover.py` + `api/routes/covers.py`（三接口，
 `main.py` 注册为第 7 只 router）；前端 `src/lib/cover/` 五件套（常量 / 配色 / 模板 / 自适应 /
 导出）、`/covers` 工作室页、3 个 BFF 薄代理、导航与类型；`docs/cover_module.md` 本文件；
 离线自检 `scripts/selftest_cover.py`。
- **本次不做的**：不动任何 LangGraph 图与 harness 基座；不新增第二个 `lifespan`；
 **零 LLM 调用 / 零 API Key / 零成本**（封面不经过大模型）；前端笔记页（后续步骤）；
 封面数据回流与诊断（此前起）。
- **红线**：不登录小红书 / 不抓登录态 / **不请求任何小红书域名** / 不自动发布；
 背景大图与导出 PNG **不入库**；图片资产只在浏览器内存与本机磁盘。
- **零迁移**：`covers` 表由 `0001` 基线建好，本次直接映射
 （`alembic current` 仍 `0002 (head)`、`alembic check` 零漂移、迁移文件仍恰 2 支）。

---

## 二、技术路线：为什么封面不用 LLM

spike（`D:\项目1\cover_spike\`）已验证：封面是**版式工程**，不是生成式任务。

| 维度 | 程序化模板渲染（本次） | 生成式（刻意不用） |
| --- | --- | --- |
| 确定性 | 同一输入 ⇒ 同一输出，可回归 | 每次不同 |
| 成本 | **0**（无 token） | 每次调用计费 |
| 依赖 | 纯前端（npm 打包含截图库） | 需要 key / 网络 / 后端编排 |
| 合规 | 文字与配色完全可控 | 可能产出违禁内容 |
| 迭代 | 改模板代码即改风格 | 调提示词碰运气 |

**迁移口径**（spike → 正式工程）：

- **常量 / 配色 / 模板 / 自适应算法 / 导出封装逐项迁移，值保持完全一致**
 （`constants.ts` 文件头注明「不要在别处重复定义」）；
- spike 用 `file://` + vendor UMD 加载截图库 → 正式工程改 **npm 依赖**
 （`html-to-image@1.11.13`，见第十节），打包后**断网可导出**；
- spike 的 `esc` 手动转义 → React JSX 文本插值**天然转义**（不用 `dangerouslySetInnerHTML`）；
- 非法 `template` / `theme` 一律**回退第一项**（`getTemplate` / `getTheme`），脏数据不白屏。

---

## 三、模板与配色（4 × 5 = 20 种组合）

**4 套模板**（`frontend/src/lib/cover/templates.tsx`）：

| id | 名称 | 结构 | 特殊要求 |
| --- | --- | --- | --- |
| `gradient_hero` | 渐变大字报 | 大主标题 + 副标题钩子 + 标签胶囊 + 署名 | — |
| `list_clean` | 纯色清单体 | 主标题 + 01/02/03 编号要点 + 标签署名 | `needBullets`（要点列表） |
| `bg_image` | 背景图风 | 本地背景图 + 渐变遮罩 + 主副标题（白字可读） | 背景图仅编辑会话内存 |
| `highlight` | 关键词高亮 | 主标题中指定关键词用高亮色块强调 + 副标题 + 标签 | `needKeyword`（高亮词） |

**5 组配色**（`frontend/src/lib/cover/themes.ts`，每组 9 个 CSS 变量 + `bgColor` 兜底）：

| id | 名称 | 主色 | 适合 |
| --- | --- | --- | --- |
| `coral` | 珊瑚橙 | `#FF6B4A` 渐变 | 生活 / 美食 |
| `cream` | 奶白暖调 | `#FFF8F0` 浅底 | 知识 / 干货 |
| `forest` | 墨绿奶黄 | `#14452F` 深绿 | 职场 / 成长 |
| `neon` | 黑底荧光 | `#101014` 黑底 | 潮流 / 数码 |
| `ocean` | 深海蓝 | `#0F4C81` 深蓝 | 商业 / 理性 |

配色套用方式：把变量写到 `.cover` 根节点的 `style` 上（`CoverCanvas` 组件），
模板元素只消费 `var(--cv-*)`；`bgColor` 作为导出 PNG 的 `backgroundColor`（渐变之外的兜底）。

---

## 四、主标题自适应（`fitTitle`，真实 DOM 测量）

`frontend/src/lib/cover/fitTitle.ts`（自 spike `app.js` 的 `fitTitle` 逐行等价迁移）：

- 字号档位 `TITLE_FONT_STEPS = [150, 132, 116, 100, 88, 76, 64, 56, 48]`（px），从大到小**降档命中即定**；
- 目标：主标题在安全区内**最多 3 行**（`TITLE_MAX_LINES`）且不溢出；
- 可用高度 `avail = 安全区高 − 其它块高 − 标题块内兄弟元素高 − 24px 余量`（下限 120px）；
- 最小档仍放不下 ⇒ 交给 `-webkit-line-clamp` **截断并显示省略号**（不撑破画布）。

**三个 spike 实测踩坑（勿改，已写进文件头注释）**：

1. 判定用 `offsetHeight`（**自然高度**），**不能**用 `scrollHeight vs clientHeight`
 —— CJK 字形会超出 line box 约 5px，那个差值会把**所有档位误判为溢出**；
2. `nextFrame` 必须 **rAF + `setTimeout(50)` 双保险** —— 后台标签页 / 无头模式可能不触发 rAF，
 只等 rAF 会把「测量」卡死；
3. 测量阶段先取消 `maxHeight` 与 `line-clamp`，测得自然高度后再定档；
 放得下时 `maxHeight = 自然高度 + 12px`（避免 `overflow:hidden` 裁掉字形底部）。

渲染时机：`CoverCanvas` 用 isomorphic 的 `useLayoutEffect`（SSR 回落 `useEffect`），
在**渲染后、绘制前**跑 `fitTitle` —— 预览与导出共用同一个已定档的 DOM 节点。

---

## 五、字段映射（严格对齐 `covers` 真实列，**零迁移**）

| 封面概念 | 落库列 | 说明 |
| --- | --- | --- |
| 模板 | `template_key` | varchar(32)，非空；4 个模板 id |
| 配色 | `color_scheme` | varchar(32)，非空；5 个配色 id |
| 主标题 | `title_text` | varchar(100) 可空；空白 → NULL |
| 副标题 | `subtitle_text` | varchar(100) 可空；空白 → NULL |
| 标签 / 要点 / 署名 / 页码 / 高亮词 | `config_json` | JSONB 可空：`{tags, bullets, author, page_no, highlight_word}` |
| 图片路径 | `image_path` | **一律留 NULL**（本工程不把导出 PNG 落服务器磁盘） |
| 画布尺寸 | `width` / `height` | 默认 `1242` / `1656`（后端常量），创建时显式写入 |
| 选中态 | `is_selected` | 默认 `false`；一个 note 仅一张 `true`（应用层单事务保证） |
| 版本 | `version` | 恒 `1`（本次无版本演进） |
| 归属 | `note_id` | FK → `notes.id`，`ON DELETE CASCADE` |

**图片资产口径（重要）**：

- **用户背景图（dataURL）禁止入 `config_json` / 落库** —— 大图入 JSONB 会让库膨胀且无版本管理；
 它只存在于**编辑会话内存**（`bg_image` 字段，`CoverDraft` 中注明「仅内存」）；
- 候选缩略图与最终导出都由**前端按 `config_json` 实时渲染**，后端只存配置数据；
- 「保存为候选」提交的是**配置**（模板/配色/文字/标签），而不是 PNG。

---

## 六、候选库与「唯一选中」单事务互斥

`backend/app/crud/cover.py` 的关键方法：

| 方法 | 职责 |
| --- | --- |
| `create_with_defaults` | 新建候选：`width=1242` / `height=1656` / `is_selected=false` / `version=1` 由后端补默认 |
| `list_by_note` | 某笔记的候选列表（`created_at` 正序 + `id` 决胜，保证稳定顺序） |
| `update_fields` | 部分字段更新（commit + refresh） |
| `select_one` | **互斥选中**：同一事务内先把该 note 全部候选置 `false`，再把目标置 `true`，一次 commit；异常整体 rollback |

- **「一个 note 仅一张选中封面」由应用层单事务保证**（无数据库约束 —— 刻意避免复杂部分索引，
 与 `0001` 的既定设计一致）；
- 选中操作**不改动 `notes.status`**（笔记定稿属后续「笔记」步）；
- 允许「没有选中封面」的中间态（初始即如此；也可显式 `PATCH is_selected=false` 取消）。
- **无物理 DELETE**：封面候选不单独删除，随笔记 `ON DELETE CASCADE` 清理。

---

## 七、HTTP 接口（`/api/v1/covers`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/covers?note_id=<UUID 必填>` | 候选列表 + `selected_id`；缺参 **400**、笔记不存在 **404** |
| POST | `/api/v1/covers` | 新建候选；笔记不存在 **404**；字段校验 **422** |
| PATCH | `/api/v1/covers/{cover_id}` | 更新字段 / 互斥选中；封面不存在 **404**；显式 null 传非空列 **422** |

- **不提供 PUT / DELETE**（自检扫描 OpenAPI 断言：`/covers` 路径面只有 GET + POST，
 `/{cover_id}` 只有 PATCH）；
- `PATCH is_selected=true` ⇒ 走 `select_one` 互斥；`is_selected=false` ⇒ 显式取消选中；
- 非空列（`template_key` / `color_scheme`）显式传 `null` 提前拦为 **422**（统一错误体）；
- 统一错误体 `{status, code, message, details}`（复用全站 4 个异常处理器）。

---

## 八、前端结构

```
frontend/src/
├── lib/cover/
│ ├── constants.ts # 8 个规格常量（1242×1656 / 安全区 96+48 / 字号档位 / 5MB 上限）
│ ├── themes.ts # 5 组配色（CSS 变量 + bgColor）+ getTheme 回退
│ ├── templates.tsx # CoverDraft / DEFAULT_DRAFT / 4 个模板组件 / CoverCanvas / getTemplate
│ ├── fitTitle.ts # 主标题自适应（真实 DOM 测量）+ nextFrame
│ └── exportPng.ts # 【唯一】绑定截图库处：captureToPng / waitFontsReady / 下载工具
├── app/covers/
│ ├── page.tsx # 封面工作室（"use client"；外层 Suspense 包裹内层 CoversInner）
│ └── covers.css # 自 spike styles.css 对应节逐字迁移 + 缩略图样式
└── app/api/v1/
 ├── covers/route.ts # GET（search 透传）/ POST 薄代理
 ├── covers/[id]/route.ts # PATCH（params Promise + encodeURIComponent）
 └── notes/route.ts # GET（笔记选择器必要转发）
```

页面（`/covers`）交互流：

1. **笔记选择器**：`GET /api/v1/notes?page=1&page_size=100&status=all`；支持 `?note_id=` URL 预选；
2. **编辑表单**：主标题 / 副标题 / 标签（TagInput） / 署名 / 页码 / 模板（Select） /
 配色（radiogroup）/ 背景图（`<input type="file">`，jpg/jpeg/png/webp → FileReader → dataURL，**仅内存**）；
3. **实时预览**：1242×1656 画布，`transform: scale` 由 `ResizeObserver` 跟随舞台宽度
 （缩放不影响导出像素）；
4. **候选库**：缩略图 = 同款画布 `scale(110/1242)` 实时渲染（选中的带「当前」徽标）；
 点击缩略图 → PATCH `is_selected=true`；「载入编辑」把候选配置还原进表单；
5. **操作**：保存为候选（POST）/ 下载 PNG（`exportNode` + 浏览器下载）/
 封面自检（见第九节）。
6. **BFF**：浏览器只请求同源 `/api/v1/**`，服务端 `proxyToBackend` 透传（默认超时 15000，
 本次三接口均为轻量同步操作，不放大）。

---

## 九、封面自检（浏览器内，离线可跑）

页面右侧「封面自检」按钮（或访问 `/covers#autotest` 自动触发）**不依赖后端、不依赖笔记选择**，
在浏览器内直接跑 7 项检查：

| # | 检查项 | 断言口径 |
| --- | --- | --- |
| 1 | 逻辑画布尺寸 | 画布节点实测 1242×1656（与目标一致；舞台缩放不影响导出） |
| 2 | 字体就绪 | `document.fonts.status=loaded` + 实际 font-family 栈 |
| 3 | 标题 2/20/40 字自适应 | 三档样例：2 字→字号 150px/1 行；20 字→3 行；40 字→降档 3 行，均不溢出 |
| 4 | 导出像素与体积 | 单张 PNG `naturalWidth×Height=1242×1656`；体积 ≤ 5 MB 上限 |
| 5 | 模板×配色全矩阵导出 | 4 模板 × 5 配色 = **20 次导出**全部成功且 1242×1656 |
| 6 | 背景图导出（dataURL 注入） | 内存测试图（canvas 生成 800×600）注入后导出成功，无 SecurityError |
| 7 | 离线可导出（bundle 依赖） | `html-to-image` 来自 npm bundle；页面无任何外链脚本 / 在线字体 |

自动化协议：面板带 `data-selftest-done="1"` 与 `data-selftest-summary`（JSON），
另有隐藏 `<pre data-selftest-json>`（逐项明细），供无头浏览器断言读取。

---

## 十、自检证据（本机实跑）

**① 后端离线自检 `scripts/selftest_cover.py`：79/79 PASS，退出码 0**（连跑两次一致）

覆盖 10 组：静态规格（表名 / 13 列集合 / 非空可空 / FK CASCADE / 常量 / server_default）/
创建候选 21 项（含逐列落库复核）/ 列表结构与顺序 / **互斥选中**（选 B → 恰一张 true →
改选 A → DB 层 count=1）/ PATCH 字段与「不改 notes.status」/ 错误码 400·422·404 /
接口面（OpenAPI 无 PUT/DELETE、`main.py` 恰 7 只 router、唯一 lifespan）/
**零迁移**（迁移文件恰 2 支、`alembic current` 含 `0002` + `(head)`、`alembic check` 退出码 0）/
合规（无心域名、无 `sk-`）/ 物理清理复查残留 0（双口径）。

**② 浏览器端到端（playwright-core + 本机 Chrome，生产构建 `next start`）**

| 项 | 结果 |
| --- | --- |
| 封面自检 7 项 | **7/7 PASS**（画布 1242×1656 / 字体 loaded / 2·20·40 字自适应 / 单张导出 688.3 KB / 20 次矩阵导出 / 背景图无 SecurityError / 离线可导出） |
| E2E：保存为候选 | 提示出现，候选库缩略图出现（1 张） |
| E2E：选中互斥 | 点击缩略图 → 「当前」徽标出现（`selectedAppeared=true`） |
| E2E：下载 PNG | 文件名 `cover_gradient_hero_coral_20261011_195918.png`，落盘校验 **1242×1656 / 0.670 MB（≤5MB）** |
| 页面错误 | `pageerror` / `console.error` 收集为空（`problems: []`） |

**③ 未验证项（如实标注，请在本机自行另测）**

- **用户真实大图背景**（数 MB 的 jpg）：自检用的是程序化生成的小测试图（800×600），
 真实大图在个别浏览器上可能触发 `html-to-image` 的性能 / 内存问题，请自行实测；
- **中文字体个体差异**：导出字体取系统字体栈（PingFang SC → Microsoft YaHei → …），
 不同机器字形宽窄略有差异，`fitTitle` 按实测自适应，但极端长标题的换行位置可能有别；
- **断网导出**：已断言「无外链脚本 / 字体 / 截图库来自 bundle」，物理断网场景请自行复验。

---

## 十一、合规与隐私

- **零 LLM / 零 API Key / 零成本**：封面链路从头到尾不经过任何大模型；
- **零出网**：导出走本地 bundle，无 CDN / 无在线字体；后端接口只写本机 PostgreSQL；
- **背景图与 PNG 不入库**：不落服务器磁盘、不入 `config_json`、`image_path` 恒 NULL；
- **不请求小红书域名 / 不登录 / 不抓登录态 / 不自动发布**（产品红线，源码级扫描在
 `selftest_cover.py` 中执行：4 个本次触碰文件无 `xiaohongshu` / `xhs.com` / Cookie / Token / `sk-` 形态）；
- 导出的 PNG 由浏览器**直接下载**到用户本机，是否发布、发到哪里**完全由用户决定**。

---

## 十二、本次【不做】

- ❌ 前端笔记页（后续步骤）；封面之外的任何页面改动；
- ❌ 封面数据回流与诊断（此前起）；RAG / 知识库（第 18 / 19 步）；
- ❌ 服务器端渲染 / 存储 PNG（`image_path` 恒 NULL）；封面版本演进（`version` 恒 1）；
- ❌ 物理删除接口（无 DELETE；随笔记 CASCADE）；
- ❌ 改 LangGraph 图 / harness 基座 / 数据模型与迁移 / compose / CORS；
- ❌ 新增第二个 `lifespan` / 任何 Python 依赖（npm 仅 `html-to-image@1.11.13`，精确锁定）；
- ❌ 登录小红书 / 抓取登录态 / 请求任何小红书域名 / 自动发布（**永不做，产品红线**）。
