"""第 9 步 · 热搜服务化在线冒烟（**真实网络 + 真实库**）。

前置（缺一不可）：

    docker compose up -d          # 起 db / redis / dailyhot
    cd backend && uvicorn app.main:app --reload --port 8000   # 起后端

它会：真实调用 ``POST /api/v1/hotspots/refresh`` 采集公开榜单并落库，
断言落库条数、``content_hash`` 无重复、二次刷新幂等（只更新不新增），
再校验列表的分页 / 过滤 / 排序、``/sources``、``/runs``，验证并发互斥锁，
注入一个坏源验证容错（partial），**验证热点状态标记 PATCH 的三态互逆 / 非法值 / 不存在**（第 11 步），
最后在进程内让调度器"到点"触发一次作业。

退出码：

    0  全部通过
    1  有断言失败
    2  前置不满足（dailyhot 或后端不可达）——**不伪造结果**

说明：公开热点数据**保留**作第 11 步演示；如需清理，见脚本末尾打印的 SQL。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\smoke_hotspot.py
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func, select, text  # noqa: E402

from app.core.timeutil import LOCAL_TIMEZONE, now_local, today_local  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.enums import RunTrigger, RunType  # noqa: E402
from app.models.hotspot import Hotspot  # noqa: E402

# 本机请求必须绕过系统代理，否则可能得到 502 假故障（同 healthcheck.py）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SMOKE_SOURCES = ["zhihu", "douyin"]
BAD_SOURCE = "definitely-not-a-real-source"


class Checker:
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


def http_json(
    url: str, *, method: str = "GET", payload: dict | None = None, timeout: float = 120.0
) -> tuple[int | None, object]:
    """发起一次本机 JSON 请求，返回 (status_code, parsed_body)。网络失败返回 (None, reason)。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body) if body else None
        except json.JSONDecodeError:
            return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="第 9 步热搜服务化在线冒烟")
    parser.add_argument("--backend-host", default="localhost")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--dailyhot", default="http://127.0.0.1:6688")
    args = parser.parse_args()

    backend = f"http://{args.backend_host}:{args.backend_port}"
    dailyhot = args.dailyhot.rstrip("/")
    api = f"{backend}/api/v1/hotspots"

    print("=" * 66)
    print(" 第 9 步 · 热搜服务化在线冒烟")
    print("=" * 66)
    print(f" 后端   ：{backend}")
    print(f" DailyHot：{dailyhot}")
    print("-" * 66)

    # ---- 前置检查（不满足直接退出码 2，绝不伪造） ----
    code, body = http_json(f"{dailyhot}/zhihu?limit=1", timeout=15)
    if code is None:
        print(f"[SKIP] DailyHotApi 不可达（{body}）——请先 docker compose up -d")
        print("       未发起任何采集，也未伪造任何结果。退出码 2。")
        return 2
    code, body = http_json(f"{backend}/health", timeout=15)
    if code != 200:
        print(f"[SKIP] 后端不可达（{backend}/health -> {code}）——请先启动 uvicorn。退出码 2。")
        return 2

    c = Checker()
    today = today_local()
    with SessionLocal() as db:
        before_rows = db.execute(
            select(func.count()).select_from(Hotspot).where(Hotspot.hot_date == today)
        ).scalar_one()
    if before_rows:
        print(f"[INFO] 库中今日已有 {before_rows} 行 → 本轮走幂等更新路径（脚本可重复运行）")

    # --------------------------------------------------------------- 首次采集
    print("\n--- A. 采集（POST /refresh） ---")
    code, first = http_json(f"{api}/refresh", method="POST", payload={"sources": SMOKE_SOURCES})
    c.check(code == 200 and isinstance(first, dict), "refresh 返回 200 + JSON", f"code={code} body={first}")
    if not isinstance(first, dict):
        return _finish(c)
    totals = first.get("totals", {})
    c.check(first.get("status") == "success", "整次状态 success", f"{first.get('status')}")
    c.check(totals.get("sources") == len(SMOKE_SOURCES), "采集源数正确", f"{totals}")
    c.check(
        totals.get("new", 0) + totals.get("updated", 0) == totals.get("fetched", 0),
        "fetched = new + updated（条条有归属）",
        f"{totals}",
    )
    if before_rows == 0:
        c.check(totals.get("new", 0) > 0, "首次新增 > 0（库中今日原本为空）", f"new={totals.get('new')}")
    else:
        # 库中已有今日数据时，``new`` **允许 > 0**：公开榜单会随时间轮换，出现新条目属正常。
        # 「同一批次连续两次刷新 new=0」的严格幂等性由 B 段专门断言。
        c.check(
            totals.get("new", 0) + totals.get("updated", 0) == totals.get("fetched", 0),
            "已有数据时 fetched = new + updated（new 允许 >0：榜单会轮换）",
            f"{totals}",
        )
    c.check(first.get("run_id") is not None, "返回 run_id")

    with SessionLocal() as db:
        today_rows = db.execute(
            select(func.count()).select_from(Hotspot).where(Hotspot.hot_date == today)
        ).scalar_one()
        dup = db.execute(
            text(
                "SELECT count(*) FROM ("
                "  SELECT content_hash FROM hotspots GROUP BY content_hash HAVING count(*) > 1"
                ") t"
            )
        ).scalar_one()
    c.check(
        today_rows == before_rows + totals.get("new", 0),
        "落库净增 == new（无重复插入）",
        f"{before_rows} + {totals.get('new')} vs {today_rows}",
    )
    c.check(dup == 0, "content_hash 全局无重复", f"dup={dup}")
    print(f"[INFO] 今日 hotspots 行数：{today_rows}")

    # --------------------------------------------------------------- 幂等
    print("\n--- B. 二次刷新幂等（只更新不新增） ---")
    code, second = http_json(f"{api}/refresh", method="POST", payload={"sources": SMOKE_SOURCES})
    c.check(code == 200 and isinstance(second, dict), "二次 refresh 返回 200", f"code={code}")
    if isinstance(second, dict):
        t2 = second.get("totals", {})
        c.check(t2.get("new") == 0, "二次刷新 new=0", f"new={t2.get('new')}")
        c.check(t2.get("updated", 0) > 0, "二次刷新 updated > 0", f"updated={t2.get('updated')}")
    with SessionLocal() as db:
        after_rows = db.execute(
            select(func.count()).select_from(Hotspot).where(Hotspot.hot_date == today)
        ).scalar_one()
    c.check(after_rows == today_rows, "二次刷新后总行数不变（幂等）", f"{today_rows} -> {after_rows}")

    # --------------------------------------------------------------- 列表
    print("\n--- C. 列表分页 / 过滤 / 排序 ---")
    code, page = http_json(f"{api}?hot_date=all&page_size=5")
    c.check(code == 200 and isinstance(page, dict), "GET /hotspots 200", f"code={code}")
    if isinstance(page, dict):
        c.check(
            set(page) >= {"items", "total", "page", "page_size"},
            "响应含 {items,total,page,page_size}",
            f"{sorted(page)}",
        )
        c.check(page.get("page_size") == 5 and len(page.get("items", [])) <= 5, "分页生效")

    code, filtered = http_json(f"{api}?source=zhihu&hot_date=all&page_size=10")
    if isinstance(filtered, dict):
        items = filtered.get("items", [])
        c.check(items and all(i.get("source") == "zhihu" for i in items), "source 过滤生效", f"{len(items)}")

    code, asc = http_json(f"{api}?hot_date=all&sort=fetched_at&order=asc&page_size=5")
    if isinstance(asc, dict):
        times = [i.get("fetched_at") for i in asc.get("items", [])]
        c.check(times == sorted(times), "sort=fetched_at&order=asc 升序", f"{times[:3]}")

    code, desc = http_json(f"{api}?hot_date=all&sort=hot_score&order=desc&page_size=10")
    if isinstance(desc, dict):
        scores = [i.get("hot_score") for i in desc.get("items", []) if i.get("hot_score") is not None]
        c.check(scores == sorted(scores, reverse=True), "sort=hot_score&order=desc 降序", f"{scores[:5]}")

    # --------------------------------------------------------------- sources / runs
    print("\n--- D. /sources 与 /runs ---")
    code, sources = http_json(f"{api}/sources")
    c.check(code == 200 and isinstance(sources, list) and sources, "GET /sources 返回非空数组")
    if isinstance(sources, list) and sources:
        c.check(
            all({"source", "name", "enabled"} <= set(s) for s in sources),
            "每源含 source/name/enabled",
            f"{sources[0]}",
        )
        c.check(
            all("xhs" not in str(s.get("source", "")).lower() for s in sources),
            "/sources 不含小红书",
        )

    code, runs = http_json(f"{api}/runs?limit=5")
    c.check(code == 200 and isinstance(runs, list) and runs, "GET /runs 返回非空数组")
    if isinstance(runs, list) and runs:
        c.check(runs[0].get("run_type") == "hotspot_fetch", "run_type=hotspot_fetch", f"{runs[0].get('run_type')}")
    with SessionLocal() as db:
        run_count = db.execute(
            select(func.count()).select_from(AgentRun).where(AgentRun.run_type == RunType.HOTSPOT_FETCH)
        ).scalar_one()
    c.check(run_count >= 2, "agent_runs 已写入 hotspot_fetch 记录", f"{run_count}")

    # --------------------------------------------------------------- 并发锁
    print("\n--- E. 并发互斥（两次同时 refresh，应有一次 409） ---")
    statuses: list[int | None] = []
    barrier = threading.Barrier(2)

    def _fire() -> None:
        barrier.wait()
        code, _ = http_json(f"{api}/refresh", method="POST", payload={"sources": SMOKE_SOURCES})
        statuses.append(code)

    threads = [threading.Thread(target=_fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.check(all(s in (200, 409) for s in statuses), "并发请求状态 ∈ {200,409}", f"{statuses}")
    c.check(409 in statuses, "并发互斥锁生效：至少一个 409 fetch_in_progress", f"{statuses}")

    # --------------------------------------------------------------- 坏源容错
    print("\n--- F. 坏源容错（partial，好源数据照常入库） ---")
    code, mixed = http_json(
        f"{api}/refresh", method="POST", payload={"sources": ["zhihu", BAD_SOURCE]}
    )
    if isinstance(mixed, dict):
        c.check(mixed.get("status") == "partial", "含坏源时整次 status=partial", f"{mixed.get('status')}")
        c.check(mixed.get("totals", {}).get("failed", 0) >= 1, "failed 源计数 >= 1")
        per = {s.get("source"): s for s in mixed.get("per_source", [])}
        c.check(
            per.get("zhihu", {}).get("status") == "success",
            "好源 zhihu 仍 success（未被拖垮）",
            f"{per.get('zhihu')}",
        )

    # --------------------------------------------------------------- 状态标记（第 11 步）
    print("\n--- G. 热点状态标记（PATCH /hotspots/{id}，第 11 步新增） ---")
    with SessionLocal() as db:
        target = db.execute(
            select(Hotspot.id, Hotspot.title).order_by(Hotspot.created_at.desc()).limit(1)
        ).first()
        total_before = db.execute(select(func.count()).select_from(Hotspot)).scalar_one()

    if target is None:
        c.check(False, "库中存在热点可供 PATCH 测试", "库中无热点数据")
    else:
        hotspot_id, original_title = str(target[0]), target[1]

        def _patch_status(target_id: str, value: str) -> tuple[int | None, object]:
            return http_json(f"{api}/{target_id}", method="PATCH", payload={"status": value})

        code, body = _patch_status(hotspot_id, "ignored")
        status = body.get("status") if isinstance(body, dict) else body
        c.check(
            code == 200 and status == "ignored",
            "PATCH new→ignored 生效",
            f"code={code} status={status}",
        )

        code, body = _patch_status(hotspot_id, "adopted")
        status = body.get("status") if isinstance(body, dict) else body
        c.check(
            code == 200 and status == "adopted",
            "PATCH ignored→adopted 生效（互逆）",
            f"code={code} status={status}",
        )

        code, body = _patch_status(hotspot_id, "new")
        status = body.get("status") if isinstance(body, dict) else body
        c.check(
            code == 200 and status == "new",
            "PATCH adopted→new 可逆回位",
            f"code={code} status={status}",
        )

        if isinstance(body, dict):
            c.check(
                body.get("title") == original_title,
                "PATCH 只改 status，未动其它字段",
                f"{body.get('title')!r} != {original_title!r}",
            )

        code, _ = _patch_status(hotspot_id, "not-a-status")
        c.check(code == 422, "非法状态 → 422（Pydantic 校验）", f"code={code}")

        code, _ = _patch_status("00000000-0000-4000-8000-000000000000", "adopted")
        c.check(code == 404, "不存在热点 → 404", f"code={code}")

        with SessionLocal() as db:
            total_after = db.execute(select(func.count()).select_from(Hotspot)).scalar_one()
        c.check(
            total_after == total_before,
            "PATCH 不物理删除 / 不新增热点行",
            f"{total_before} -> {total_after}",
        )

    # --------------------------------------------------------------- 定时到点执行
    print("\n--- H. 定时到点执行（进程内 BackgroundScheduler） ---")
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.services.hotspot import service as hotspot_service

    triggered: dict[str, object] = {}

    def _job() -> None:
        summary = hotspot_service.collect_and_store(trigger="scheduled", raise_if_busy=False)
        triggered["summary"] = summary

    scheduler = BackgroundScheduler(timezone=LOCAL_TIMEZONE)
    fire_at = now_local() + timedelta(seconds=3)
    scheduler.add_job(_job, trigger="date", run_date=fire_at, id="smoke_scheduled")
    scheduler.start()
    try:
        deadline = time.time() + 40
        while time.time() < deadline and "summary" not in triggered:
            time.sleep(0.5)
    finally:
        scheduler.shutdown(wait=False)
    summary = triggered.get("summary")
    c.check(summary is not None, "调度器到点触发了作业", f"{summary}")
    with SessionLocal() as db:
        scheduled_rows = db.execute(
            select(func.count()).select_from(AgentRun).where(
                AgentRun.run_type == RunType.HOTSPOT_FETCH,
                AgentRun.trigger == RunTrigger.SCHEDULED,
            )
        ).scalar_one()
    c.check(scheduled_rows >= 1, "agent_runs 出现 trigger=scheduled 记录", f"{scheduled_rows}")

    return _finish(c)


def _finish(c: Checker) -> int:
    print("\n" + "-" * 66)
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for name in c.failures:
            print(f"  - FAIL: {name}")
        print("如需清理本次演示数据（可选）：")
        print("  DELETE FROM agent_runs WHERE run_type='hotspot_fetch';")
        print("  DELETE FROM hotspots WHERE hot_date = CURRENT_DATE;")
        return 1
    print("全部 PASS ✔（真实网络 + 真实库）")
    print("[INFO] 公开热点数据已保留作第 11 步演示；如需清理见 README 第九节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
