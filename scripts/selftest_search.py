"""第 23 步 · 关键词搜索（GitHub）离线自检（离线：MockTransport + FakeCache；零网络、零真实 Redis、零 DB 写入）。

覆盖范围（对任务书验收清单逐项）：

1. 配置与静态合规：注册表仅 github / 响应模型无凭据字段 / 搜索相关文件
   无内容平台域名与写操作 / A 块回归（github 热搜源在册）；
2. provider 基础：200 正常解析与字段映射（full_name→title、html_url→url、
   description→snippet、stargazers_count→score、meta 含 language/updated_at/owner）、
   User-Agent / Accept 头、匿名无 Authorization、带 token 有、参数透传、limit 截断、脏数据丢弃；
3. 限速退避（假时钟）：429 / 403-限速 / Remaining=0 按 X-RateLimit-Reset 本地退避，
   Reset 前不再触网，越过 Reset 自动恢复；403 非限速与缺 Reset 头兜底；
4. 重试语义：5xx / 网络异常重试（3 次）耗尽转上游错误；404 / 非 JSON / 缺 items 不重试；
5. 缓存：miss / hit（查询归一化同键）/ force_refresh 跳过 / service 直调防护；
6. 路由（TestClient + mock）：q 缺失/空/空白/超长 422、source 未注册 400、
   limit 越界 422、sort 非法 422、正常 200、缓存标注、503 限速（不触网）、
   502 上游、错误响应与消息无 token 泄漏、POST 405（纯只读）、openapi 方法集 = {get}；
7. 前端静态：Tabs 引用、外链仅 github.com + rel=noopener noreferrer、
   BFF 走 proxyToBackend（60s）、types.ts 搜索类型无凭据字段、无内容平台域名、文案齐备。

退出码：

    0  全部通过
    1  有断言失败

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_search.py
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import httpx
import tenacity
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.schemas.search import SearchItemOut, SearchResponseOut  # noqa: E402
from app.services.hotspot import adapters as hotspot_adapters  # noqa: E402
from app.services.search import registry  # noqa: E402
from app.services.search import service as search_service  # noqa: E402
from app.services.search.cache import build_cache_key, normalize_query  # noqa: E402
from app.services.search.github import (  # noqa: E402
    GITHUB_ACCEPT,
    GITHUB_USER_AGENT,
    GitHubProvider,
)
from app.services.search.provider import (  # noqa: E402
    SearchRateLimitedError,
    SearchUpstreamError,
)

#: 仅测试用的假令牌（不连任何真实服务；用于断言请求头与脱敏）
TEST_TOKEN = "ghp_SELFTESTTOKEN_1234567890abcdef"

#: 内容平台域名黑名单（搜索相关文件不得出现；红线）
BANNED_DOMAINS = (
    "xiaohongshu",
    "xhslink",
    "douyin.com",
    "weibo.com",
    "zhihu.com",
    "kuaishou.com",
    "bilibili.com",
)

#: 写操作记号（搜索链路必须是纯只读）
WRITE_MARKERS = (
    "sessionlocal",
    "db.commit",
    "insert into",
    "delete from",
    "@router.post",
    "@router.patch",
    "@router.put",
    "@router.delete",
)

# 抑制 TestClient / httpx 的逐请求 INFO 日志（保持自检输出可读）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)


class Checker:
    """极简 PASS / FAIL 收集器。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))

    def info(self, label: str) -> None:
        print(f"[INFO] {label}")


class FakeClock:
    """可推进的假时钟（秒）。"""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeCache:
    """内存缓存（记录 get/set 次数；协议与 RedisSearchCache 一致）。"""

    def __init__(self) -> None:
        self.store: dict[str, list[dict]] = {}
        self.gets = 0
        self.sets = 0

    def get(self, key: str) -> list[dict] | None:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, items: list[dict], ttl_seconds: int) -> None:
        self.sets += 1
        self.store[key] = items


def _make_transport(plan: list[object]) -> tuple[httpx.MockTransport, dict[str, int], list[httpx.Request]]:
    """构造 MockTransport。

    ``plan`` 元素为 ``{"status", "json"?, "text"?, "headers"?}`` 或异常实例；
    超出 ``plan`` 长度后重复最后一个元素。
    """
    counter = {"calls": 0}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(counter["calls"], len(plan) - 1)
        counter["calls"] += 1
        seen.append(request)
        step = plan[index]
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, dict), step
        status = int(step.get("status", 200))
        headers = step.get("headers") or {}
        if "text" in step:
            return httpx.Response(status, text=str(step["text"]), headers=headers)
        return httpx.Response(status, json=step.get("json") or {}, headers=headers)

    return httpx.MockTransport(handler), counter, seen


def _provider(
    plan: list[object],
    *,
    token: str = "",
    now: FakeClock | None = None,
    max_attempts: int = 3,
) -> tuple[GitHubProvider, dict[str, int], list[httpx.Request]]:
    """构造带 MockTransport 的 GitHubProvider（零网络）。"""
    transport, counter, seen = _make_transport(plan)
    provider = GitHubProvider(
        base_url="https://selftest.invalid",
        timeout=5,
        max_attempts=max_attempts,
        token=token,
        limit_max=30,
        fallback_backoff_seconds=60,
        retry_waiter=tenacity.wait_none,
        transport=transport,
        now_func=now or FakeClock(),
    )
    return provider, counter, seen


def _gh_payload(n: int = 3) -> dict:
    """模拟 GitHub /search/repositories 的正常响应。"""
    items = []
    for i in range(n):
        items.append(
            {
                "full_name": f"selftest-org/repo-{i}",
                "html_url": f"https://github.com/selftest-org/repo-{i}",
                "description": f"[SELFTEST] 样例仓库 {i}",
                "stargazers_count": 1000 - i,
                "language": "Python",
                "updated_at": "2026-10-15T12:00:00Z",
                "owner": {"login": "selftest-org"},
                "forks_count": 10 + i,
            }
        )
    return {"total_count": n, "incomplete_results": False, "items": items}


# ---------------------------------------------------------------------------
# 1. 配置与静态合规
# ---------------------------------------------------------------------------
def check_config_and_static(c: Checker) -> None:
    print("\n--- 1. 配置与静态合规 ---")
    # A 块回归：github 热搜源在册
    c.check(
        "github" in settings.hotspot_source_list,
        "热搜启用源含 github（A 块回归）",
        f"{settings.hotspot_source_list}",
    )
    c.check(
        hotspot_adapters.source_name_of("github") == "GitHub",
        "github 源中文名 = GitHub",
        hotspot_adapters.source_name_of("github"),
    )
    c.check(
        "github" in hotspot_adapters.resolve_enabled_sources(),
        "resolve_enabled_sources 含 github",
    )

    # 搜索配置
    c.check(
        settings.GITHUB_API_BASE == "https://api.github.com",
        "GITHUB_API_BASE 默认官方地址",
        settings.GITHUB_API_BASE,
    )
    c.check(settings.SEARCH_CACHE_TTL_SECONDS == 1800, "缓存 TTL 默认 1800s（30 分钟）")
    c.check(settings.SEARCH_LIMIT_MAX == 30, "limit 上限 30（任务书 n<=30）")
    c.check(isinstance(settings.GITHUB_TOKEN, str), "GITHUB_TOKEN 为字符串（留空即匿名）")

    # 注册表
    c.check(registry.available_sources() == ["github"], "注册表恰含 github", f"{registry.available_sources()}")
    c.check(registry.get_provider("github") is not None, "get_provider('github') 可用")
    c.check(registry.get_provider("weibo") is None, "未注册源返回 None（路由转 400）")

    # 响应模型无凭据字段
    resp_keys = set(SearchResponseOut.model_fields.keys())
    item_keys = set(SearchItemOut.model_fields.keys())
    c.check(
        resp_keys == {"source", "query", "sort", "count", "cached", "items"},
        "SearchResponseOut 字段集",
        f"{sorted(resp_keys)}",
    )
    c.check(
        item_keys == {"title", "url", "source", "snippet", "score", "meta"},
        "SearchItemOut 字段集",
        f"{sorted(item_keys)}",
    )
    combined = " ".join(resp_keys | item_keys).lower()
    c.check(
        all(t not in combined for t in ("token", "authorization", "cookie", "secret", "password")),
        "响应模型无凭据类字段",
    )

    # 静态：搜索相关后端文件
    backend_files = sorted((BACKEND_DIR / "app" / "services" / "search").glob("*.py"))
    backend_files += [
        BACKEND_DIR / "app" / "api" / "routes" / "search.py",
        BACKEND_DIR / "app" / "schemas" / "search.py",
    ]
    domain_hits: list[str] = []
    write_hits: list[str] = []
    for path in backend_files:
        low = path.read_text(encoding="utf-8", errors="replace").lower()
        domain_hits += [f"{path.name}:{b}" for b in BANNED_DOMAINS if b in low]
        write_hits += [f"{path.name}:{m}" for m in WRITE_MARKERS if m in low]
    c.check(not domain_hits, "搜索后端文件无内容平台域名", f"{domain_hits}")
    c.check(not write_hits, "搜索后端文件无写操作（纯只读）", f"{write_hits}")


# ---------------------------------------------------------------------------
# 2. provider 基础
# ---------------------------------------------------------------------------
def check_provider_basics(c: Checker) -> None:
    print("\n--- 2. provider 基础（解析 / 请求头 / 参数透传） ---")
    payload = _gh_payload(3)
    provider, counter, seen = _provider([{"status": 200, "json": payload}])
    items = provider.search("langgraph agent", limit=5, sort="stars")

    c.check(len(items) == 3, "200 正常解析（3 条）", f"{len(items)}")
    first = items[0]
    c.check(first.title == "selftest-org/repo-0", "title = full_name", first.title)
    c.check(first.url == "https://github.com/selftest-org/repo-0", "url = html_url", first.url)
    c.check(first.source == "github", "source 标注 = github", first.source)
    c.check(first.score == 1000, "score = stargazers_count", f"{first.score}")
    c.check(bool(first.snippet) and "SELFTEST" in (first.snippet or ""), "snippet = description")
    c.check(
        first.meta.get("language") == "Python"
        and first.meta.get("owner") == "selftest-org"
        and first.meta.get("updated_at") == "2026-10-15T12:00:00Z"
        and first.meta.get("forks_count") == 10,
        "meta 齐备（language / owner / updated_at / forks_count）",
        f"{first.meta}",
    )

    req = seen[0]
    c.check(req.headers.get("user-agent") == GITHUB_USER_AGENT, "User-Agent 存在且为项目标识")
    c.check(req.headers.get("accept") == GITHUB_ACCEPT, "Accept = application/vnd.github+json")
    c.check(req.headers.get("authorization") is None, "匿名请求无 Authorization")
    c.check(req.url.params.get("q") == "langgraph agent", "q 参数透传", f"{req.url.params.get('q')}")
    c.check(
        req.url.params.get("sort") == "stars" and req.url.params.get("order") == "desc",
        "sort=stars / order=desc 透传",
    )
    c.check(req.url.params.get("per_page") == "5", "per_page = limit", f"{req.url.params.get('per_page')}")

    # 带 token
    provider2, _, seen2 = _provider([{"status": 200, "json": payload}], token=TEST_TOKEN)
    provider2.search("x", limit=1, sort="updated")
    c.check(
        seen2[0].headers.get("authorization") == f"Bearer {TEST_TOKEN}",
        "带 token 时 Authorization: Bearer <token>",
    )
    c.check(seen2[0].url.params.get("sort") == "updated", "sort=updated 透传")

    # limit 截断
    provider3, _, seen3 = _provider([{"status": 200, "json": payload}])
    provider3.search("x", limit=40, sort="stars")
    c.check(seen3[0].url.params.get("per_page") == "30", "limit 超上限本地截断为 30")

    # 脏数据丢弃
    dirty = {
        "items": [
            {"html_url": "https://github.com/x/y", "full_name": ""},
            {"full_name": "ok/repo"},
            payload["items"][0],
        ]
    }
    provider4, _, _ = _provider([{"status": 200, "json": dirty}])
    items4 = provider4.search("x", limit=10, sort="stars")
    c.check(len(items4) == 1, "缺 full_name / html_url 的脏数据被丢弃", f"{len(items4)}")


# ---------------------------------------------------------------------------
# 3. 限速退避
# ---------------------------------------------------------------------------
def check_rate_limit(c: Checker) -> None:
    print("\n--- 3. 限速退避（按 X-RateLimit-Reset，假时钟） ---")
    clock = FakeClock(1_000_000.0)
    reset_ts = 1_000_060
    plan = [
        {"status": 429, "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_ts)}},
        {"status": 200, "json": _gh_payload(1)},
    ]
    provider, counter, _ = _provider(plan, now=clock)

    try:
        provider.search("x", limit=5, sort="stars")
        c.check(False, "429 → 限速异常", "未抛出")
    except SearchRateLimitedError as exc:
        c.check(exc.reset_at == float(reset_ts), "429: reset_at = X-RateLimit-Reset", f"{exc.reset_at}")
    c.check(counter["calls"] == 1, "429 不重试（恰 1 次触网）", f"{counter['calls']}")

    try:
        provider.search("x", limit=5, sort="stars")
        c.check(False, "Reset 前二次请求快速失败", "未抛出")
    except SearchRateLimitedError:
        c.check(True, "Reset 前二次请求快速失败（不再触网）")
    c.check(counter["calls"] == 1, "Reset 前 calls 不变", f"{counter['calls']}")

    clock.t = reset_ts + 1
    items = provider.search("x", limit=5, sort="stars")
    c.check(
        len(items) == 1 and counter["calls"] == 2,
        "时钟越过 Reset → 自动恢复并触网",
        f"calls={counter['calls']}",
    )

    # 403 + Remaining=0 → 限速
    provider2, counter2, _ = _provider(
        [
            {
                "status": 403,
                "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000060"},
                "json": {"message": "API rate limit exceeded"},
            }
        ],
        now=FakeClock(2_000_000.0),
    )
    try:
        provider2.search("x", limit=5, sort="stars")
        c.check(False, "403-Remaining=0 → 限速", "未抛出")
    except SearchRateLimitedError:
        c.check(True, "403-Remaining=0 → 限速")

    # 403 非限速 → 不重试的上游错误
    provider3, counter3, _ = _provider(
        [
            {
                "status": 403,
                "headers": {"X-RateLimit-Remaining": "55"},
                "json": {"message": "Resource not accessible by integration"},
            }
        ]
    )
    try:
        provider3.search("x", limit=5, sort="stars")
        c.check(False, "403 非限速 → SearchUpstreamError", "未抛出")
    except SearchRateLimitedError:
        c.check(False, "403 非限速 → SearchUpstreamError", "被误判为限速")
    except SearchUpstreamError:
        c.check(True, "403 非限速 → SearchUpstreamError（不重试）")
        c.check(counter3["calls"] == 1, "403 非限速恰 1 次触网", f"{counter3['calls']}")

    # 200 + Remaining=0 → 本次照常返回，后续记退避
    provider4, counter4, _ = _provider(
        [
            {
                "status": 200,
                "json": _gh_payload(1),
                "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3000060"},
            }
        ],
        now=FakeClock(3_000_000.0),
    )
    items4 = provider4.search("x", limit=5, sort="stars")
    c.check(len(items4) == 1, "200 但 Remaining=0：本次照常返回")
    try:
        provider4.search("x", limit=5, sort="stars")
        c.check(False, "Remaining=0 已记退避（二次快速失败）", "未抛出")
    except SearchRateLimitedError:
        c.check(True, "Remaining=0 已记退避（二次快速失败）")
    c.check(counter4["calls"] == 1, "退避期内不再触网", f"{counter4['calls']}")

    # 缺 Reset 头 → 兜底 60s
    provider5, _, _ = _provider(
        [{"status": 429, "headers": {"X-RateLimit-Remaining": "0"}}],
        now=FakeClock(4_000_000.0),
    )
    try:
        provider5.search("x", limit=5, sort="stars")
        c.check(False, "缺 Reset 头 → 兜底退避", "未抛出")
    except SearchRateLimitedError as exc:
        c.check(exc.reset_at == 4_000_060.0, "缺 Reset 头 → 60s 兜底退避", f"{exc.reset_at}")


# ---------------------------------------------------------------------------
# 4. 重试语义
# ---------------------------------------------------------------------------
def check_retry(c: Checker) -> None:
    print("\n--- 4. 重试语义（5xx / 网络重试；4xx / 非法响应不重试） ---")
    provider, counter, _ = _provider(
        [{"status": 500}, {"status": 500}, {"status": 200, "json": _gh_payload(1)}]
    )
    items = provider.search("x", limit=5, sort="stars")
    c.check(
        len(items) == 1 and counter["calls"] == 3,
        "5xx 重试后成功（calls=3）",
        f"calls={counter['calls']}",
    )

    provider2, counter2, _ = _provider([{"status": 503}])
    try:
        provider2.search("x", limit=5, sort="stars")
        c.check(False, "5xx 重试耗尽 → SearchUpstreamError", "未抛出")
    except SearchUpstreamError:
        c.check(True, "5xx 重试耗尽 → SearchUpstreamError")
    c.check(counter2["calls"] == 3, "5xx 重试次数 = max_attempts（3）", f"{counter2['calls']}")

    provider3, counter3, _ = _provider([httpx.ConnectError("selftest connect error")])
    try:
        provider3.search("x", limit=5, sort="stars")
        c.check(False, "网络异常重试耗尽 → SearchUpstreamError", "未抛出")
    except SearchUpstreamError:
        c.check(True, "网络异常重试耗尽 → SearchUpstreamError")
    c.check(counter3["calls"] == 3, "网络异常同样重试 3 次", f"{counter3['calls']}")

    provider4, counter4, _ = _provider([{"status": 404, "json": {"message": "Not Found"}}])
    try:
        provider4.search("x", limit=5, sort="stars")
        c.check(False, "404 不重试 → 立即失败", "未抛出")
    except SearchUpstreamError:
        c.check(counter4["calls"] == 1, "404 不重试（calls=1）", f"{counter4['calls']}")

    provider5, counter5, _ = _provider([{"status": 200, "text": "<html>not json</html>"}])
    try:
        provider5.search("x", limit=5, sort="stars")
        c.check(False, "非 JSON 响应 → SearchUpstreamError", "未抛出")
    except SearchUpstreamError:
        c.check(counter5["calls"] == 1, "非 JSON 不重试（calls=1）", f"{counter5['calls']}")

    provider6, counter6, _ = _provider([{"status": 200, "json": {"message": "Validation Failed"}}])
    try:
        provider6.search("x", limit=5, sort="stars")
        c.check(False, "缺 items 数组 → SearchUpstreamError", "未抛出")
    except SearchUpstreamError:
        c.check(counter6["calls"] == 1, "缺 items 不重试（calls=1）", f"{counter6['calls']}")


# ---------------------------------------------------------------------------
# 5. 缓存
# ---------------------------------------------------------------------------
def check_cache(c: Checker) -> None:
    print("\n--- 5. 缓存（FakeCache + 键归一化） ---")
    cache = FakeCache()
    provider, counter, _ = _provider([{"status": 200, "json": _gh_payload(2)}])

    out1 = search_service.search_repositories(
        query="LangGraph  Agent", source="github", sort="stars", limit=5, provider=provider, cache=cache
    )
    c.check(out1.cached is False and out1.count == 2, "首次 miss：回源且 cached=False")
    c.check(counter["calls"] == 1, "首次恰 1 次触网", f"{counter['calls']}")

    out2 = search_service.search_repositories(
        query="langgraph agent", source="github", sort="stars", limit=5, provider=provider, cache=cache
    )
    c.check(out2.cached is True and out2.count == 2, "二次 hit：cached=True（大小写/空白归一化同键）")
    c.check(counter["calls"] == 1, "命中后不再触网", f"{counter['calls']}")

    out3 = search_service.search_repositories(
        query="langgraph agent",
        source="github",
        sort="stars",
        limit=5,
        force_refresh=True,
        provider=provider,
        cache=cache,
    )
    c.check(
        out3.cached is False and counter["calls"] == 2,
        "force_refresh 跳过缓存（再次触网）",
        f"calls={counter['calls']}",
    )

    c.check(normalize_query("  A   B ") == "a b", "normalize_query 折叠空白 + 小写")
    k1 = build_cache_key(source="github", query="A  B", sort="stars", limit=5)
    k2 = build_cache_key(source="github", query="a b", sort="stars", limit=5)
    c.check(
        k1 == k2 and k1.startswith("search:github:stars:5:"),
        "缓存键归一化一致且结构正确",
        k1,
    )

    try:
        search_service.search_repositories(query="x", source="gitlab", cache=cache)
        c.check(False, "service 直调未注册源 → ValueError", "未抛出")
    except ValueError:
        c.check(True, "service 直调未注册源 → ValueError（防直调误用）")


# ---------------------------------------------------------------------------
# 6. 路由
# ---------------------------------------------------------------------------
def check_routes(c: Checker) -> None:
    print("\n--- 6. 路由（TestClient + mock；含 503 限速 / 502 上游） ---")
    provider, counter, _ = _provider([{"status": 200, "json": _gh_payload(2)}])
    registry.register_provider(provider, replace=True)
    search_service._default_cache = FakeCache()
    client = TestClient(app)

    r = client.get("/api/v1/search")
    c.check(r.status_code == 422 and r.json().get("code") == "validation", "q 缺失 → 422", f"{r.status_code}")
    r = client.get("/api/v1/search?q=")
    c.check(r.status_code == 422, "q 空串 → 422", f"{r.status_code}")
    r = client.get("/api/v1/search", params={"q": "   "})
    c.check(r.status_code == 422, "q 纯空白 → 422", f"{r.status_code}")
    r = client.get("/api/v1/search", params={"q": "a" * 101})
    c.check(r.status_code == 422, "q 超长（101 字）→ 422", f"{r.status_code}")

    r = client.get("/api/v1/search", params={"q": "x", "source": "weibo"})
    body = r.json()
    c.check(
        r.status_code == 400 and body.get("code") == "bad_request",
        "source 未注册 → 400",
        f"{r.status_code} {body.get('code')}",
    )
    c.check("github" in str(body.get("details") or {}), "400 的 details 含可用来源")
    c.check("weibo" in str(body.get("message") or ""), "400 的 message 点明被拒来源")

    r = client.get("/api/v1/search", params={"q": "x", "limit": 0})
    c.check(r.status_code == 422, "limit=0 → 422", f"{r.status_code}")
    r = client.get("/api/v1/search", params={"q": "x", "limit": 31})
    c.check(r.status_code == 422, "limit=31 → 422", f"{r.status_code}")
    r = client.get("/api/v1/search", params={"q": "x", "sort": "bogus"})
    c.check(r.status_code == 422, "sort 非法 → 422", f"{r.status_code}")
    r = client.get("/api/v1/search", params={"q": "x", "sort": "updated", "limit": 30})
    c.check(r.status_code == 200, "边界 limit=30 + sort=updated → 200", f"{r.status_code}")

    # 正常查询 + 缓存标注
    r = client.get("/api/v1/search", params={"q": "langgraph agent"})
    body = r.json()
    c.check(
        r.status_code == 200 and body.get("count") == 2 and body.get("cached") is False,
        "正常 200（count=2、cached=False）",
        f"{r.status_code} {body.get('count')} {body.get('cached')}",
    )
    first = body["items"][0]
    c.check(
        set(first.keys()) == {"title", "url", "source", "snippet", "score", "meta"},
        "响应 item 字段集",
        f"{sorted(first.keys())}",
    )
    r2 = client.get("/api/v1/search", params={"q": "langgraph agent"})
    c.check(r2.json().get("cached") is True, "二次查询 cached=True")

    # 纯只读
    r = client.post("/api/v1/search", json={"q": "x"})
    c.check(r.status_code == 405, "POST → 405（纯只读）", f"{r.status_code}")
    methods = set(app.openapi()["paths"]["/api/v1/search"].keys())
    c.check(methods == {"get"}, "openapi：/api/v1/search 仅 GET", f"{methods}")

    # 限速 → 503（且退避期内不再触网）
    clock = FakeClock(5_000_000.0)
    provider_rl, counter_rl, _ = _provider(
        [
            {
                "status": 429,
                "headers": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000060"},
            }
        ],
        now=clock,
    )
    registry.register_provider(provider_rl, replace=True)
    search_service._default_cache = FakeCache()
    r = client.get("/api/v1/search", params={"q": "ratelimit-test"})
    body = r.json()
    c.check(
        r.status_code == 503 and body.get("code") == "search_rate_limited",
        "429 → 503 search_rate_limited",
        f"{r.status_code} {body.get('code')}",
    )
    c.check(
        isinstance((body.get("details") or {}).get("retry_after_seconds"), int),
        "503 details 含 retry_after_seconds",
        f"{body.get('details')}",
    )
    r = client.get("/api/v1/search", params={"q": "ratelimit-test"})
    c.check(
        r.status_code == 503 and counter_rl["calls"] == 1,
        "退避期内再次请求仍 503 且不再触网",
        f"calls={counter_rl['calls']}",
    )

    # 上游 5xx → 502；无 token 泄漏
    provider_500, _, _ = _provider([{"status": 500}], token=TEST_TOKEN)
    registry.register_provider(provider_500, replace=True)
    search_service._default_cache = FakeCache()
    r = client.get("/api/v1/search", params={"q": "upstream-500"})
    c.check(
        r.status_code == 502 and r.json().get("code") == "search_upstream_error",
        "5xx 重试耗尽 → 502 search_upstream_error",
        f"{r.status_code} {r.json().get('code')}",
    )
    c.check(TEST_TOKEN not in r.text, "错误响应文本无 token 泄漏")
    c.check("Bearer" not in r.text, "错误响应文本无 Authorization 字样")

    p_tok, _, _ = _provider([{"status": 500}], token=TEST_TOKEN)
    try:
        p_tok.search("x", limit=5, sort="stars")
    except SearchUpstreamError as exc:
        c.check(TEST_TOKEN not in str(exc), "provider 错误消息无 token 泄漏")

    # 恢复真实 provider（进程内整洁）
    registry.register_provider(GitHubProvider.from_settings(), replace=True)


# ---------------------------------------------------------------------------
# 7. 前端静态
# ---------------------------------------------------------------------------
def check_frontend_static(c: Checker) -> None:
    print("\n--- 7. 前端静态检查（外链 / 无凭据字段 / 无平台域名） ---")
    panel = (ROOT_DIR / "frontend" / "src" / "app" / "hotspots" / "search-panel.tsx").read_text(encoding="utf-8")
    bff = (ROOT_DIR / "frontend" / "src" / "app" / "api" / "v1" / "search" / "route.ts").read_text(encoding="utf-8")
    types_ts = (ROOT_DIR / "frontend" / "src" / "lib" / "types.ts").read_text(encoding="utf-8")
    page = (ROOT_DIR / "frontend" / "src" / "app" / "hotspots" / "page.tsx").read_text(encoding="utf-8")

    c.check(
        "TabsTrigger" in page and "TAB_SEARCH" in page and "KeywordSearchPanel" in page,
        "热点页含「关键词搜索」Tab（不新增顶级导航）",
    )
    c.check('rel="noopener noreferrer"' in panel and 'target="_blank"' in panel, "外链 rel=noopener noreferrer + 新标签")
    c.check(
        "https://github.com/" in panel and "GITHUB_PREFIX" in panel,
        "外链仅 github.com 域白名单",
    )
    c.check("proxyToBackend" in bff and "timeoutMs: 60000" in bff, "BFF 走 proxyToBackend（60s 超时）")
    c.check("搜索中…" in panel and "未找到相关仓库" in panel, "loading 与空态文案齐备")
    c.check("search_rate_limited" in panel, "限速 503 人话提示存在")

    # types.ts 搜索类型无凭据字段（按 interface 体提取字段名，避免注释误报）
    fields: list[str] = []
    for name in ("SearchItem", "SearchResponse"):
        match = re.search(rf"export interface {name} \{{(.*?)\n\}}", types_ts, re.S)
        c.check(match is not None, f"types.ts 含 {name} 接口")
        if match:
            fields += re.findall(r"^\s*(\w+)\s*[:?]", match.group(1), re.M)
    c.check(
        {"title", "url", "source", "snippet", "score", "meta", "cached", "count"} <= set(fields),
        "搜索类型字段齐全",
        f"{sorted(set(fields))}",
    )
    c.check(
        all(
            t not in f.lower()
            for f in fields
            for t in ("token", "authorization", "cookie", "secret", "password")
        ),
        "types.ts 搜索类型无凭据字段",
        f"{fields}",
    )

    # 无内容平台域名（前端搜索相关文件）
    domain_hits: list[str] = []
    for name, text in (("search-panel.tsx", panel), ("search route.ts", bff), ("hotspots/page.tsx", page)):
        low = text.lower()
        domain_hits += [f"{name}:{b}" for b in BANNED_DOMAINS if b in low]
    c.check(not domain_hits, "前端搜索相关文件无内容平台域名", f"{domain_hits}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()
    print("=" * 62)
    print(" 第 23 步 · 关键词搜索（GitHub）离线自检（MockTransport + FakeCache；零网络）")
    print("=" * 62)

    # 全程注入内存缓存：任何路径都不会触达真实 Redis
    search_service._default_cache = FakeCache()

    check_config_and_static(c)
    check_provider_basics(c)
    check_rate_limit(c)
    check_retry(c)
    check_cache(c)
    check_routes(c)
    check_frontend_static(c)

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for name in c.failures:
            print(f"  - FAIL: {name}")
        return 1
    print("全部 PASS ✔（离线自检：零网络、零真实 Redis、零 DB 写入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
