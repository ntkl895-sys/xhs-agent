"""第 9 步 · 热搜服务化离线自检（**不联网、不请求 DailyHotApi**）。

用 ``httpx.MockTransport`` 伪造上游响应，用真实本地 PostgreSQL 验证幂等 upsert。
逐项断言后输出 PASS / FAIL；全部通过退出码 0，任一 FAIL 退出码 1。

覆盖（对应任务书 §9）：

1. **配置合规**：启用源不含小红书；``xhs`` 会被硬性过滤；禁用清单非空；
2. **重试语义**：4xx(404) **不重试**（仅 1 次调用）；500 / 429 **重试**到 ``max_attempts``；
   网络传输异常可重试；成功即返回；
3. **归一化**：无 ``title`` 的脏数据被跳过；``rank_position`` 重编号；热度文本解析；
   ``HOTSPOT_KEEP_EXTRA_FIELDS`` 的开关行为；
4. **content_hash**：相同 (源, id, 日期) 稳定；不同日期不同；缺 id 时以标题兜底（不塌缩）；
5. **幂等落库 + 端到端**：经统一入口 ``collect_and_store()`` 跑两轮，
   第一轮 ``new=N``、第二轮 ``new=0/updated=N``；``hotspots`` 无重复 ``content_hash``；
   ``agent_runs`` 写一条 ``hotspot_fetch`` 且 ``llm_model`` 为空、token 为 NULL；
6. **调度器配置**：``interval`` 分钟数 / ``coalesce`` / ``max_instances=1`` 正确；
   开关关闭时不注册作业；
7. **路由注册**：``/api/v1/hotspots`` 系列 4 个端点已注册；
8. **合规扫描**：源码不含小红书域名、不构造 Cookie / 签名请求头。

测试数据统一打 ``[SELFTEST]`` 标记，脚本结束**物理清理**并复查无残留。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_hotspot.py
"""

from __future__ import annotations

import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
import tenacity  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.timeutil import today_local  # noqa: E402
from app.crud.hotspot import compute_content_hash, crud_hotspot  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.enums import RunType  # noqa: E402
from app.models.hotspot import Hotspot  # noqa: E402
from app.services.hotspot import adapters, service  # noqa: E402
from app.services.hotspot import scheduler as scheduler_mod  # noqa: E402
from app.services.hotspot.client import (  # noqa: E402
    DailyHotClient,
    HotspotClientError,
    HotspotRetryableError,
)
from app.services.hotspot.schemas import SourceStatus  # noqa: E402

SELFTEST_PREFIX = "[SELFTEST]"

OK_PAYLOAD = {
    "code": 200,
    "name": "知乎",
    "title": "知乎",
    "total": 3,
    "fromCache": True,
    "data": [
        {"id": "q1", "title": f"{SELFTEST_PREFIX}热点一", "url": "https://x/1", "mobileUrl": "m1", "hot": 12345, "timestamp": "t1"},
        {"id": "q2", "title": f"{SELFTEST_PREFIX}热点二", "url": "https://x/2", "mobileUrl": "m2", "hot": "1.2万", "timestamp": "t2"},
        {"id": "q3", "title": f"{SELFTEST_PREFIX}热点三", "url": "https://x/3", "mobileUrl": "m3", "hot": "热", "timestamp": "t3"},
    ],
}


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


def _make_transport(plan: list[object]) -> tuple[httpx.MockTransport, dict[str, int]]:
    """构造 MockTransport；``plan`` 元素为 ``(status, payload)`` 或一个异常实例。

    超出 ``plan`` 长度后重复最后一个元素。
    """
    counter = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(counter["calls"], len(plan) - 1)
        counter["calls"] += 1
        step = plan[index]
        if isinstance(step, Exception):
            raise step
        status, payload = step  # type: ignore[misc]
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), counter


def _client(plan: list[object], *, max_attempts: int = 3) -> tuple[DailyHotClient, dict[str, int]]:
    transport, counter = _make_transport(plan)
    client = DailyHotClient(
        base_url="http://selftest.local",
        timeout=5,
        per_source_limit=5,
        force_refresh=False,
        max_attempts=max_attempts,
        retry_waiter=tenacity.wait_none,
        transport=transport,
    )
    return client, counter


# ---------------------------------------------------------------------------
# 1. 配置合规
# ---------------------------------------------------------------------------
def check_config(c: Checker) -> None:
    print("\n--- 1. 配置合规 ---")
    enabled = settings.hotspot_source_list
    c.check(
        not any(adapters.is_forbidden_source(s) for s in enabled),
        "启用源不含小红书（合规）",
        f"enabled={enabled}",
    )
    c.check(
        adapters.FORBIDDEN_SOURCES, "小红书禁用清单非空", f"{sorted(adapters.FORBIDDEN_SOURCES)}"
    )
    resolved = adapters.resolve_enabled_sources(["zhihu", "xhs", "xiaohongshu", "douyin"])
    c.check(resolved == ["zhihu", "douyin"], "resolve 会过滤小红书并去重保序", f"{resolved}")
    try:
        adapters.ensure_source_allowed("xhs")
        raised = False
    except ValueError:
        raised = True
    c.check(raised, "ensure_source_allowed('xhs') 抛异常")
    c.check(
        bool(settings.HOTSPOT_ENABLED_SOURCES) and settings.HOTSPOT_PER_SOURCE_LIMIT > 0,
        "关键 HOTSPOT_* 配置就位",
        f"sources={settings.HOTSPOT_ENABLED_SOURCES!r} limit={settings.HOTSPOT_PER_SOURCE_LIMIT}",
    )


# ---------------------------------------------------------------------------
# 2. 重试语义
# ---------------------------------------------------------------------------
def check_retry(c: Checker) -> None:
    print("\n--- 2. 客户端重试语义（MockTransport，离线） ---")

    # 404：不重试
    client, counter = _client([(404, {"code": 404})], max_attempts=3)
    try:
        client.get_list("zhihu")
        ok = False
    except HotspotClientError:
        ok = True
    client.close()
    c.check(ok and counter["calls"] == 1, "4xx(404) 立即失败且不重试", f"calls={counter['calls']}")

    # 500：重试到 max_attempts
    client, counter = _client([(500, {})], max_attempts=3)
    try:
        client.get_list("zhihu")
        ok = False
    except HotspotRetryableError:
        ok = True
    client.close()
    c.check(ok and counter["calls"] == 3, "5xx 重试到 max_attempts=3", f"calls={counter['calls']}")

    # 429 → 200：第二次成功
    client, counter = _client([(429, {}), (200, OK_PAYLOAD)], max_attempts=3)
    payload = client.get_list("zhihu")
    client.close()
    c.check(
        counter["calls"] == 2 and payload.get("code") == 200,
        "429 重试后成功（第二次即返回）",
        f"calls={counter['calls']}",
    )

    # 网络异常 → 200
    client, counter = _client([httpx.ConnectError("boom"), (200, OK_PAYLOAD)], max_attempts=3)
    payload = client.get_list("zhihu")
    client.close()
    c.check(
        counter["calls"] == 2 and payload.get("code") == 200,
        "网络传输异常可重试",
        f"calls={counter['calls']}",
    )

    # force_refresh 才带 cache=false
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(200, json=OK_PAYLOAD)

    plain = DailyHotClient(
        base_url="http://selftest.local", timeout=5, per_source_limit=5,
        force_refresh=False, max_attempts=1, retry_waiter=tenacity.wait_none,
        transport=httpx.MockTransport(handler),
    )
    plain.get_list("zhihu")
    plain.close()
    no_cache_param = "cache" not in seen_params
    seen_params.clear()
    forced = DailyHotClient(
        base_url="http://selftest.local", timeout=5, per_source_limit=5,
        force_refresh=True, max_attempts=1, retry_waiter=tenacity.wait_none,
        transport=httpx.MockTransport(handler),
    )
    forced.get_list("zhihu")
    forced.close()
    c.check(
        no_cache_param and seen_params.get("cache") == "false",
        "默认不带 cache 参数；force_refresh 才带 cache=false",
        f"plain_has_cache={not no_cache_param} forced={seen_params.get('cache')}",
    )


# ---------------------------------------------------------------------------
# 3. 归一化
# ---------------------------------------------------------------------------
def check_normalize(c: Checker) -> None:
    print("\n--- 3. 归一化 ---")
    payload = {
        "code": 200,
        "data": [
            {"id": "a", "title": "正常一", "hot": "1.2万"},
            {"id": "b", "title": "   "},          # 空标题 → 丢
            {"hot": 999},                          # 无标题 → 丢
            {"id": "c", "title": "正常二", "hot": "热"},
        ],
    }
    items = adapters.normalize_items(
        payload, "zhihu", per_source_limit=10, keep_extra_fields=False
    )
    c.check(len(items) == 2, "无 title 脏数据被跳过", f"len={len(items)}")
    c.check(
        [i.rank_position for i in items] == [1, 2],
        "rank_position 按归一化顺序重编号",
        f"{[i.rank_position for i in items]}",
    )
    c.check(items[0].hot_score == 12000, "热度 '1.2万' 解析为 12000", f"{items[0].hot_score}")
    c.check(items[1].hot_score is None, "非数值热度（热）解析为 None", f"{items[1].hot_score}")
    c.check(
        items[0].raw_extra.get("id") == "a" and "desc" not in items[0].raw_extra,
        "raw_extra 默认仅最小集（id/mobileUrl/timestamp）",
        f"{items[0].raw_extra}",
    )

    rich = {
        "code": 200,
        "data": [{"id": "a", "title": "带附加", "hot": 1, "desc": "摘要", "cover": "cv", "author": "作者"}],
    }
    items2 = adapters.normalize_items(
        rich, "zhihu", per_source_limit=10, keep_extra_fields=True
    )
    c.check(
        items2[0].summary == "摘要" and items2[0].cover_url == "cv" and items2[0].author == "作者",
        "KEEP_EXTRA_FIELDS=true 时回填 cover/summary/author",
        f"{items2[0].model_dump()}",
    )

    c.check(adapters.parse_hot_value(True) is None, "bool 不被当作热度 1")
    c.check(adapters.parse_hot_value("3.5亿") == 350000000, "热度 '3.5亿' 解析正确")


# ---------------------------------------------------------------------------
# 4. content_hash
# ---------------------------------------------------------------------------
def check_content_hash(c: Checker) -> None:
    print("\n--- 4. content_hash 稳定性 ---")
    d1 = date(2026, 10, 10)
    d2 = d1 + timedelta(days=1)
    h_same_1 = compute_content_hash("zhihu", "q1", "标题", d1)
    h_same_2 = compute_content_hash("zhihu", "q1", "标题", d1)
    h_other_day = compute_content_hash("zhihu", "q1", "标题", d2)
    c.check(h_same_1 == h_same_2, "相同 (源,id,日期) 哈希稳定")
    c.check(h_same_1 != h_other_day, "不同日期哈希不同")
    c.check(len(h_same_1) == 64, "sha256 十六进制长度 64", f"{len(h_same_1)}")
    # 缺 id 时以标题兜底，避免当日所有无 id 条目塌缩成一行
    h_title_a = compute_content_hash("zhihu", None, "标题A", d1)
    h_title_b = compute_content_hash("zhihu", None, "标题B", d1)
    c.check(h_title_a != h_title_b, "缺 id 时以标题兜底（不同标题不塌缩）")


# ---------------------------------------------------------------------------
# 5. 幂等落库 + 端到端（真实本地库）
# ---------------------------------------------------------------------------
def check_end_to_end(c: Checker) -> dict[str, object]:
    print("\n--- 5. 幂等落库 + 端到端（MockTransport + 真实本地库） ---")
    created_run_ids: list[str] = []

    original_from_settings = DailyHotClient.from_settings.__func__

    def fake_from_settings(cls, **kwargs):  # noqa: ANN001, ANN003
        transport, _ = _make_transport([(200, OK_PAYLOAD)])
        return DailyHotClient(
            base_url="http://selftest.local",
            timeout=5,
            per_source_limit=5,
            force_refresh=False,
            max_attempts=2,
            retry_waiter=tenacity.wait_none,
            transport=transport,
        )

    DailyHotClient.from_settings = classmethod(fake_from_settings)  # type: ignore[assignment]
    try:
        first = service.collect_and_store(sources=["zhihu"], trigger="manual")
        assert first is not None
        created_run_ids.append(str(first.run_id))
        c.check(
            first.status == "success" and first.totals.new == len(OK_PAYLOAD["data"]),
            "第一轮：状态 success 且新增 = 条目数",
            f"status={first.status} new={first.totals.new}",
        )
        c.check(
            first.totals.fetched == len(OK_PAYLOAD["data"]),
            "第一轮：fetched 计数正确",
            f"{first.totals.fetched}",
        )

        second = service.collect_and_store(sources=["zhihu"], trigger="manual")
        assert second is not None
        created_run_ids.append(str(second.run_id))
        c.check(
            second.totals.new == 0 and second.totals.updated == len(OK_PAYLOAD["data"]),
            "第二轮：new=0 / updated=N（幂等 upsert）",
            f"new={second.totals.new} updated={second.totals.updated}",
        )
    finally:
        DailyHotClient.from_settings = classmethod(original_from_settings)  # type: ignore[assignment]

    with SessionLocal() as db:
        rows = list(
            db.execute(select(Hotspot).where(Hotspot.title.like(f"{SELFTEST_PREFIX}%"))).scalars()
        )
        c.check(len(rows) == len(OK_PAYLOAD["data"]), "落库条数 = 条目数", f"{len(rows)}")
        hashes = [r.content_hash for r in rows]
        c.check(len(set(hashes)) == len(hashes), "同一天多次采集无重复 content_hash")
        all_new = all(r.status.value == "new" for r in rows)
        c.check(all_new, "新行 status=待落库的 new")
        assoc_ok = all(
            r.source == "zhihu" and r.hot_date == today_local() for r in rows
        )
        c.check(assoc_ok, "source / hot_date 关联正确（今天）", f"{[(r.source, r.hot_date) for r in rows][:1]}")

        runs = list(
            db.execute(
                select(AgentRun).where(
                    AgentRun.run_type == RunType.HOTSPOT_FETCH,
                    AgentRun.id.in_([uuid.UUID(r) for r in created_run_ids]),
                )
            ).scalars()
        )
        c.check(len(runs) == 2, "agent_runs 写入 2 条 hotspot_fetch", f"{len(runs)}")
        c.check(
            all(r.status is not None and r.status.value == "success" for r in runs),
            "agent_runs 状态 success",
            f"{[r.status for r in runs]}",
        )
        c.check(
            all((r.llm_model or "") == "" for r in runs),
            "热搜审计的 llm_model 留空（不接 LLM）",
            f"{[r.llm_model for r in runs]}",
        )
        c.check(
            all(r.total_tokens is None and r.prompt_tokens is None for r in runs),
            "热搜审计 token 三列保持 NULL",
            f"{[(r.prompt_tokens, r.total_tokens) for r in runs]}",
        )
        c.check(
            all(r.outputs and "per_source" in r.outputs for r in runs),
            "agent_runs.outputs 含 per_source 统计",
        )

    return {"run_ids": created_run_ids}


# ---------------------------------------------------------------------------
# 6. 调度器配置
# ---------------------------------------------------------------------------
def check_scheduler(c: Checker) -> None:
    print("\n--- 6. 调度器配置 ---")
    scheduler_mod.shutdown_scheduler()
    started = scheduler_mod.start_scheduler()
    try:
        c.check(started is not None, "启用状态下调度器可启动")
        job = scheduler_mod.get_scheduler().get_job(scheduler_mod.FETCH_JOB_ID) if started else None
        c.check(job is not None, "已注册 interval 作业")
        if job is not None:
            interval = getattr(job.trigger, "interval", None)
            expected = timedelta(minutes=settings.HOTSPOT_FETCH_INTERVAL_MINUTES)
            c.check(interval == expected, "interval 分钟数 = 配置值", f"{interval} vs {expected}")
            c.check(job.coalesce is True, "coalesce=True")
            c.check(job.max_instances == 1, "max_instances=1")
        c.check(
            settings.HOTSPOT_FETCH_ON_STARTUP is False
            or started.get_job(scheduler_mod.STARTUP_JOB_ID) is not None,
            "启动即采集作业与配置一致",
        )
    finally:
        scheduler_mod.shutdown_scheduler()

    # 关闭开关后不再注册
    original = settings.HOTSPOT_SCHEDULER_ENABLED
    settings.HOTSPOT_SCHEDULER_ENABLED = False
    try:
        c.check(scheduler_mod.start_scheduler() is None, "开关关闭时不注册调度器")
        c.check(scheduler_mod.get_scheduler() is None, "关闭后无调度器实例")
    finally:
        settings.HOTSPOT_SCHEDULER_ENABLED = original


# ---------------------------------------------------------------------------
# 7. 路由注册
# ---------------------------------------------------------------------------
def check_routes(c: Checker) -> None:
    print("\n--- 7. 路由注册 ---")
    from app.main import app

    paths = app.openapi()["paths"]
    expected = [
        ("/api/v1/hotspots", "get"),
        ("/api/v1/hotspots/sources", "get"),
        ("/api/v1/hotspots/refresh", "post"),
        ("/api/v1/hotspots/runs", "get"),
    ]
    for path, method in expected:
        c.check(
            path in paths and method in paths[path],
            f"已注册 {method.upper()} {path}",
            f"实际 paths={sorted(paths)}",
        )
    health_paths = {p for p in paths if p.startswith("/health")}
    c.check(health_paths == {"/health", "/health/ready"}, "健康端点未被改动（仍 2 个）", f"{sorted(health_paths)}")


# ---------------------------------------------------------------------------
# 8. 合规扫描（源码级）
# ---------------------------------------------------------------------------
def check_compliance_scan(c: Checker) -> None:
    print("\n--- 8. 合规扫描（源码级） ---")
    scan_root = BACKEND_DIR / "app"
    forbidden_patterns = {
        "小红书域名": re.compile(r"xiaohongshu\.com|xhslink\.com", re.IGNORECASE),
        "Cookie 请求头/参数": re.compile(r"cookies\s*=|[\"']Cookie[\"']\s*:"),
        "小红书签名头 x-s/x-t": re.compile(r"[\"']x-s[\"']\s*:|[\"']x-t[\"']\s*:"),
    }
    hits: dict[str, list[str]] = {name: [] for name in forbidden_patterns}
    for path in scan_root.rglob("*.py"):
        text_src = path.read_text(encoding="utf-8", errors="ignore")
        for name, pattern in forbidden_patterns.items():
            if pattern.search(text_src):
                hits[name].append(str(path.relative_to(ROOT_DIR)))
    for name, found in hits.items():
        c.check(not found, f"源码不含「{name}」", f"{found}")
    c.check(
        not any(adapters.is_forbidden_source(s) for s in settings.hotspot_source_list),
        "启用源再次确认不含小红书",
    )


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------
def cleanup(created: dict[str, object], c: Checker) -> None:
    print("\n--- 清理与残留复查 ---")
    with SessionLocal() as db:
        deleted = crud_hotspot.delete_by_title_prefix(db, SELFTEST_PREFIX)
        c.info(f"已删除 {deleted} 条 {SELFTEST_PREFIX} hotspots")
        run_ids = [uuid.UUID(r) for r in created.get("run_ids", [])]  # type: ignore[union-attr]
        removed_runs = 0
        for rid in run_ids:
            row = db.get(AgentRun, rid)
            if row is not None:
                db.delete(row)
                removed_runs += 1
        db.commit()
        c.info(f"已删除 {removed_runs} 条自检 agent_runs")

        leftover_hotspots = db.execute(
            select(func.count()).select_from(Hotspot).where(
                Hotspot.title.like(f"{SELFTEST_PREFIX}%")
            )
        ).scalar_one()
        c.check(leftover_hotspots == 0, "无 [SELFTEST] hotspots 残留", f"{leftover_hotspots}")

        leftover_runs = 0
        for rid in run_ids:
            if db.get(AgentRun, rid) is not None:
                leftover_runs += 1
        c.check(leftover_runs == 0, "无自检 agent_runs 残留", f"{leftover_runs}")

    c.check(not service.is_running(), "采集锁已释放")

    # 框架/业务表总数信息（便于人工核对，不做脆弱断言）
    with SessionLocal() as db:
        business = db.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' "
                "AND table_name <> 'alembic_version'"
            )
        ).scalar_one()
        c.info(f"public 业务表数量（含框架排除前）：{business}")


def main() -> int:
    c = Checker()
    print("=" * 62)
    print(" 第 9 步 · 热搜服务化离线自检（离线，MockTransport + 本地库）")
    print("=" * 62)

    created: dict[str, object] = {"run_ids": []}
    try:
        check_config(c)
        check_retry(c)
        check_normalize(c)
        check_content_hash(c)
        created = check_end_to_end(c)
        check_scheduler(c)
        check_routes(c)
        check_compliance_scan(c)
    finally:
        cleanup(created, c)

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for name in c.failures:
            print(f"  - FAIL: {name}")
        return 1
    print("全部 PASS ✔（离线自检，未请求任何公网 / DailyHotApi）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
