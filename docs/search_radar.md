# 关键词搜索雷达（V2 增强（一）：GitHub 先行）

> 目标：在「热点雷达」页内新增**按关键词搜索公开源**的能力——首期只接 **GitHub 官方
> API**（`api.github.com`，公开只读数据），并同步启用热搜采集的 **GitHub Trending 源**。
> 关键词搜索是**确定性工具 / 服务层**：不挂 LangGraph 图、不调用 LLM、不烧 token，
> 服务函数可直接被未来 Agent 图作为工具调用。
>
> 红线（任何改动都不得违反）：只接「公开、免登录、有官方 / 稳定接口」的源；
> **不请求任何内容平台（小红书 / 抖音 / 微博 / 知乎等）的搜索域名**；不使用任何
> 登录态 / Cookie / 账号；不自动发布。纯本地、只绑 `127.0.0.1`。

---

## 一、可插拔的搜索源（SearchProvider）

```
backend/app/services/search/
├── schemas.py # 标准化结果 SearchResult（title/url/source/snippet/score/meta）
│ # + 服务产物 SearchOutcome（含 cached 标志）
├── provider.py # SearchProvider 抽象 + 错误类型（SearchRateLimitedError / SearchUpstreamError）
├── registry.py # provider 注册表（可插拔；未注册源 → 路由转 400）
├── github.py # GitHub 仓库搜索（本期唯一实现；限速退避 / 重试 / 代理口径见下）
├── cache.py # Redis 缓存（key 归一化；读写失败降级，绝不影响搜索）
└── service.py # 单入口 search_repositories：缓存 → provider → 回写
```

**扩展位说明**：后续接入新源（如 Hacker News、Product Hunt 等同样「公开免登录有官方接口」的
源）只需：

1. 实现 `SearchProvider` 抽象（`search(query, limit, sort) -> list[SearchResult]`）；
2. 在 `registry.register_provider(...)` 注册（含 `source` 标识与展示名）；
3. 前端「来源」下拉增加一个选项。

路由、service、缓存与前端表格**无需改动**。内容平台搜索**不在扩展范围**（见第七节红线）。

## 二、API 契约

```
GET /api/v1/search?q=&source=github&sort=stars&limit=10[&force_refresh=false]
```

| 参数 | 说明 | 校验 |
| --- | --- | --- |
| `q` | 关键词（必填） | strip 后为空 / 超长（> 100 字）→ **422** |
| `source` | 搜索来源（默认 `github`） | 不在注册表 → **400**（本次仅注册 github） |
| `sort` | `stars`（默认）/ `updated`（最近更新） | 其它 → **422** |
| `limit` | 返回条数（默认 10） | 1 ~ 30，越界 → **422** |
| `force_refresh` | 跳过缓存强刷（预留） | 默认 `false` |

响应（`SearchResponseOut`）：

```json
{
 "source": "github",
 "query": "langgraph agent",
 "sort": "stars",
 "count": 10,
 "cached": false,
 "items": [
 {
 "title": "langchain-ai/langgraph",
 "url": "https://github.com/langchain-ai/langgraph",
 "source": "github",
 "snippet": "Build resilient agents.",
 "score": 43309,
 "meta": { "language": "Python", "updated_at": "2026-10-15T14:18:00Z",
 "owner": "langchain-ai", "forks_count": 3600 }
 }
 ]
}
```

- **纯只读**：只有 `GET`（POST / PUT / DELETE → 405）；不写业务库、不写任何平台；
- 字段映射：`full_name → title`、`html_url → url`、`description → snippet`、
 `stargazers_count → score`；`language / updated_at / owner / forks_count` 进 `meta`；
- 错误体与其它接口一致：`{status, code, message, details}`。

**错误码**：

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `bad_request` | 来源不在注册表（details 含 `available_sources`） |
| 422 | `validation` | q 空 / 超长；limit 越界；sort 非法 |
| 502 | `search_upstream_error` | 上游 4xx（非限速）/ 5xx 重试耗尽 / 网络异常 / 响应结构异常 |
| 503 | `search_rate_limited` | 触发上游限速（details 含 `reset_at` / `retry_after_seconds`） |

## 三、限速口径（以响应头为准，动态退让，不硬刷）

GitHub 搜索接口的限速**一律以响应头 `X-RateLimit-Limit / Remaining / Reset` 为准**。
经验参考值（非承诺值，仅供直觉）：匿名约 10 次/分钟，配置 `GITHUB_TOKEN` 后约 30 次/分钟。

本模块的处理：

1. 每个响应（无论状态码）都会读取 `X-RateLimit-Remaining`；**`Remaining=0`** ⇒ 记
 `blocked_until = X-RateLimit-Reset`，该时刻前**不再发起任何请求**（本地快速失败）；
2. 收到 **429 / 403（限速，含 `Remaining=0` 或 message 含 rate limit）** ⇒ 同样按
 `X-RateLimit-Reset` 退避，返回 **503 `search_rate_limited`**（message 给出恢复时刻，
 details 给 `retry_after_seconds`）；**不重试**；
3. 缺 `X-RateLimit-Reset` 头时兜底退避 60 秒（`SEARCH_BACKOFF_FALLBACK_SECONDS`）；
4. `5xx` / 网络异常 ⇒ tenacity 指数退避重试（`SEARCH_MAX_ATTEMPTS`，默认 3 次）后转 502；
5. `4xx`（非限速）/ 非 JSON / 结构异常 ⇒ **不重试**，直接 502。

## 四、缓存（尊重上游限额）

- Redis 缓存，key = `search:{source}:{sort}:{limit}:{sha1(归一化 query)[:16]}`
 （归一化 = 去首尾空白 + 折叠连续空白 + 小写）；
- TTL 默认 **1800 秒（30 分钟）**，由 `SEARCH_CACHE_TTL_SECONDS` 配置；
- 响应中的 `cached` 字段如实标注是否命中；
- 预留 `force_refresh=true` 强刷（默认关闭）；
- Redis 不可达 / 序列化失败：只记 WARNING 并当作「无缓存」继续回源——**缓存绝不阻塞搜索**。

## 五、令牌（GITHUB_TOKEN，可选）与获取路径

- **可选**：留空即匿名搜索；填写后提升限额（以响应头为准）；
- 获取路径：<https://github.com/settings/tokens>（只读公开数据**无需勾选任何 scope**）；
- 配置方式：只填 `backend/.env`（`GITHUB_TOKEN=...`），`.env.example` 只放占位注释；
- 安全约定：令牌只出现在请求头 `Authorization: Bearer`；**绝不打印 / 回显 / 入日志 /
 入响应 / 入前端产物**（错误消息统一过脱敏函数兜底，自检对「错误信息无 token 泄漏」有断言）；
- 前端与 `types.ts` 中**没有任何** token 字段。

## 六、代理与国内访问

- `localhost` / `127.0.0.1` 地址（自建镜像 / 反代场景）**绝不走代理**；
- 外网 `https` 地址读环境变量 `HTTPS_PROXY`（`http` 地址读 `HTTP_PROXY`），
 由 `GitHubProvider` 显式决议（`trust_env=False`，不受隐式环境影响）；
- 国内访问如直连不稳，可在 `backend/.env` 设置：
 - `HTTPS_PROXY=http://127.0.0.1:<port>`（本机代理）；
 - 或 `GITHUB_API_BASE=https://<镜像或反代地址>`（保持返回结构为 GitHub 官方格式）；
- 超时：连接 5s / 读取 10s（`SEARCH_TIMEOUT_SECONDS`），显式设置，不会无限挂起。

## 七、为什么不做内容平台搜索（红线说明）

- 小红书等**内容平台没有「公开、免登录、有官方稳定接口」的搜索能力**——公开搜索接口
 需要登录态 / Cookie / 账号，与本项目红线直接冲突；
- 因此本能力**只接官方公开接口**（本期 = GitHub），并在代码与自检中双重约束：
 搜索相关全部文件不得出现任何内容平台域名（自检静态扫描断言）；
- 全链路：不登录、不抓登录态、不自动发布、不请求平台域名。

## 八、前端使用（热点雷达页 · 关键词搜索 Tab）

- 入口：`/hotspots` 页内 Tabs 第二页「关键词搜索」（不新增顶级导航）；
- 交互：关键词输入 + 来源下拉（当前仅 GitHub）+ 排序（Star 数 / 最近更新）+ 查询按钮；
- 结果列表：仓库（外链）/ 描述 / Star / 语言 / 最近更新 / 操作（复制链接）；
- **外链仅渲染 `github.com` 域**（其余域名降级为纯文本），且一律
 `target="_blank" rel="noopener noreferrer"`；
- 状态提示：loading（搜索中…）、空态（未找到相关仓库）、限速 503（人话提示 + 指引配置
 GITHUB_TOKEN）、网络失败（BFF 统一 503 文案）；
- 链路：浏览器 → 本站 BFF（`/api/v1/search`，`proxyToBackend`，60s 超时）→ FastAPI。

## 九、自检与真实检查

| 脚本 | 性质 | 内容 |
| --- | --- | --- |
| `scripts/selftest_search.py` | **离线**（MockTransport + FakeCache；零网络零真实 Redis 零 DB 写入） | 94 项断言：配置 / provider 解析与头部 / 限速退避（假时钟）/ 重试语义 / 缓存 / 路由（422/400/503/502）/ 前端静态 / A 块回归 |
| `scripts/check_github_search.py` | **真实网络**（只读 api.github.com） | 真跑一次关键词搜索并校验字段映射；退出码 0/1/2（2 = 网络或限速不可用，打印说明不刷屏） |

运行（在项目根目录下）：

```powershell
backend\.venv\Scripts\python.exe scripts\selftest_search.py
backend\.venv\Scripts\python.exe scripts\check_github_search.py
backend\.venv\Scripts\python.exe scripts\check_github_search.py --query "react hooks" --limit 3
```

## 十、GitHub Trending 热搜源（A 块）

- 本地 DailyHotApi 镜像 `/all` 共 56 个路由，**包含 `github`**（GitHub 趋势日榜，
 来源 `github.com/trending`，字段含 owner/repo/language/stars，`hot` 为千分位字符串）；
- 已按与 `zhihu` / `bilibili` 同级方式启用：`HOTSPOT_ENABLED_SOURCES` 默认值追加 `github`；
 `adapters.py` 登记中文名「GitHub」；
- 采集、幂等落库、缓存、前端「来源」下拉均**沿用既有口径**，无任何特殊分支。

## 十一、后续项（本期不做）

- **「一键生成选题」**（从搜索结果直达选题创作）：改动面较大（需要新的 service 编排 +
 话题桥接 + 前端 Dialog），**本期不做**，列为后续增强；
- 其余源（Hacker News / Product Hunt 等）：只留扩展位（见第一节），本期不实现；
- 搜索结果落库：本期**不落库**（只做 Redis 缓存）；如后续需要沉淀「已采用的搜索结果」，
 再单独评估最小方案（加表属迁移，须走独立步骤评审）。

---

*本文件随此前交付；如与代码冲突，以代码与自检脚本为准。*
