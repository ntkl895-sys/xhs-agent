"""第 8 步 · CRUD 在线冒烟（需要后端与数据库已启动）。

通过真实 HTTP 把账号 / 人设完整走一遍：
建 -> 列表（过滤 / 分页）-> 详情 -> 更新 -> 归档 / 恢复，
并逐一断言错误码语义：

    - 重复 xhs_red_id      -> 409 conflict
    - 不存在 id            -> 404 not_found
    - 坏数据（空名 / 超长）-> 422 validation（details 带字段级错误）
    - 归档后默认列表不可见，传 status=archived 可见
    - 归档账号不级联改动人设
    - 同账号范围内默认人设唯一

所有测试数据名称统一带 ``[SMOKE]`` 前缀；脚本开始与结束都会用数据库会话
**物理清理** ``[SMOKE]`` 数据（先 personas 再 accounts），结束后复查残留 = 0。

用法（在**项目根目录**下，先确保 backend 已启动）：

    backend\\.venv\\Scripts\\python.exe scripts\\smoke_crud_api.py

可选参数：
    ``--base-url``     默认 http://127.0.0.1:8000（直连后端）
                       传 http://localhost:3000 可顺带验证「前端 Route Handler 代理链路」
    ``--health-path``  连通性探测路径，默认 /health（走后端时）；
                       走前端代理时应传 /api/health
全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

SMOKE_PREFIX = "[SMOKE]"

# 本机自检必须绕过系统代理，否则 localhost 请求会被发给 127.0.0.1:xxxx 代理而假失败
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def request(
    base_url: str,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, object]:
    """发起一次请求，返回 (HTTP 状态码, 解析后的 JSON 或原始文本)。

    4xx / 5xx 不抛异常，而是返回其状态码与响应体，便于断言错误语义。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


# ---------------------------------------------------------------------------
# 数据库清理（物理删除，仅限 [SMOKE] 前缀数据）
# ---------------------------------------------------------------------------
def purge_smoke_data() -> tuple[int, int]:
    """物理删除全部 ``[SMOKE]`` 人设与账号，返回删除行数 (personas, accounts)。"""
    from sqlalchemy import text

    from app.db.session import engine

    with engine.begin() as conn:
        personas = conn.execute(
            text("DELETE FROM personas WHERE name LIKE :p"), {"p": f"{SMOKE_PREFIX}%"}
        ).rowcount
        accounts = conn.execute(
            text("DELETE FROM accounts WHERE name LIKE :p"), {"p": f"{SMOKE_PREFIX}%"}
        ).rowcount
    return int(personas or 0), int(accounts or 0)


def count_smoke_residual() -> tuple[int, int]:
    """统计 ``[SMOKE]`` 残留行数。"""
    from sqlalchemy import text

    from app.db.session import engine

    with engine.connect() as conn:
        personas = conn.execute(
            text("SELECT count(*) FROM personas WHERE name LIKE :p"), {"p": f"{SMOKE_PREFIX}%"}
        ).scalar_one()
        accounts = conn.execute(
            text("SELECT count(*) FROM accounts WHERE name LIKE :p"), {"p": f"{SMOKE_PREFIX}%"}
        ).scalar_one()
    return int(personas), int(accounts)


def main() -> int:
    parser = argparse.ArgumentParser(description="第 8 步 CRUD 在线冒烟")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--health-path", default="/health")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    c = Checker()
    run = uuid.uuid4().hex[:8]

    # 前置清理：保证本次运行的断言不受上次残留影响
    print("=== 前置清理（[SMOKE] 历史残留）===")
    try:
        p, a = purge_smoke_data()
        print(f"已清理 personas={p} accounts={a}")
    except Exception as exc:  # noqa: BLE001
        print(f"前置清理失败：{exc}")
        return 1

    # ------------------------------------------------------------ 0. 连通性
    status, payload = request(base, "GET", args.health_path)
    c.check(status == 200, f"连通性探测 {args.health_path} 可达", f"status={status}")
    if status != 200:
        print("目标不可达，终止冒烟。请先启动 uvicorn / docker compose / 前端。")
        return 1

    # ------------------------------------------------------------ 1. 建账号
    red_id = f"smoke_red_{run}"
    account_name_1 = f"{SMOKE_PREFIX} 账号 {run}"
    status, account = request(
        base,
        "POST",
        "/api/v1/accounts",
        {"name": account_name_1, "xhs_red_id": red_id, "niche": "职场", "remark": "冒烟"},
    )
    c.check(status == 201, "POST /accounts 新建返回 201", f"status={status} body={account}")
    if status != 201:
        return 1
    account_id = account["id"]
    c.check(account["status"] == "active", "新建账号默认 status=active")
    c.check(account["xhs_red_id"] == red_id, "新建账号 xhs_red_id 正确回显")

    # 重复小红书号 -> 409
    status, payload = request(
        base, "POST", "/api/v1/accounts", {"name": f"{SMOKE_PREFIX} 重复 {run}", "xhs_red_id": red_id}
    )
    c.check(status == 409, "重复 xhs_red_id 返回 409", f"status={status} body={payload}")
    c.check(
        isinstance(payload, dict) and payload.get("code") == "conflict",
        "409 响应体 code=conflict",
        str(payload),
    )

    # ------------------------------------------------------------ 2. 详情 / 更新
    status, payload = request(base, "GET", f"/api/v1/accounts/{account_id}")
    c.check(status == 200 and payload["name"] == account_name_1, "GET /accounts/{id} 详情正确")

    account_name_2 = f"{SMOKE_PREFIX} 账号改 {run}"
    status, payload = request(
        base, "PATCH", f"/api/v1/accounts/{account_id}", {"name": account_name_2, "niche": "家居"}
    )
    c.check(
        status == 200 and payload["name"] == account_name_2 and payload["niche"] == "家居",
        "PATCH /accounts/{id} 部分更新生效",
        f"status={status} body={payload}",
    )

    # ------------------------------------------------------------ 3. 列表 / 过滤 / 分页
    status, payload = request(base, "GET", f"/api/v1/accounts?q=%5BSMOKE%5D&status=active")
    c.check(
        status == 200 and set(payload) == {"items", "total", "page", "page_size"},
        "列表响应结构为 {items,total,page,page_size}",
        str(payload)[:200],
    )
    c.check(
        any(item["id"] == account_id for item in payload["items"]),
        "q 模糊过滤能命中新建账号",
    )

    status, payload = request(base, "GET", "/api/v1/accounts?q=%5BSMOKE%5D&page=1&page_size=1")
    c.check(
        payload["page_size"] == 1 and len(payload["items"]) == 1 and payload["total"] >= 1,
        "分页参数生效（page_size=1）",
        str(payload)[:200],
    )

    # ------------------------------------------------------------ 4. 错误语义
    ghost = str(uuid.uuid4())
    status, payload = request(base, "GET", f"/api/v1/accounts/{ghost}")
    c.check(
        status == 404 and payload.get("code") == "not_found",
        "GET 不存在的账号返回 404 not_found",
        f"status={status} body={payload}",
    )
    status, payload = request(base, "PATCH", f"/api/v1/accounts/{ghost}", {"name": "x"})
    c.check(status == 404, "PATCH 不存在的账号返回 404", f"status={status}")

    status, payload = request(base, "POST", "/api/v1/accounts", {"name": "   "})
    c.check(
        status == 422 and payload.get("code") == "validation" and isinstance(payload.get("details"), list),
        "空 name 返回 422 validation 且 details 为字段级列表",
        f"status={status} body={payload}",
    )
    status, payload = request(base, "POST", "/api/v1/accounts", {"name": "x" * 101})
    c.check(status == 422, "超长 name 返回 422", f"status={status}")
    # status 只在 Update 里参与校验；Create 不含该字段（多余字段被忽略）
    status, payload = request(
        base, "PATCH", f"/api/v1/accounts/{account_id}", {"status": "nope"}
    )
    c.check(status == 422, "PATCH 非法 status 返回 422", f"status={status} body={payload}")

    # ------------------------------------------------------------ 5. 建人设
    status, persona1 = request(
        base,
        "POST",
        "/api/v1/personas",
        {
            "name": f"{SMOKE_PREFIX} 人设1 {run}",
            "account_id": account_id,
            "positioning": "我是职场博主",
            "tone": "轻松",
            "content_pillars": ["求职", "求职 ", "副业"],
            "taboo": ["政治", " 政治 "],
            "is_default": True,
        },
    )
    c.check(status == 201, "POST /personas 新建返回 201", f"status={status} body={persona1}")
    if status != 201:
        return 1
    persona1_id = persona1["id"]
    c.check(persona1["content_pillars"] == ["求职", "副业"], "content_pillars 自动 strip + 去重")
    c.check(persona1["taboo"] == ["政治"], "taboo 自动 strip + 去重")
    c.check(persona1["account_name"] == account_name_2, "PersonaOut.account_name 由 LEFT JOIN 返回")

    status, persona2 = request(
        base,
        "POST",
        "/api/v1/personas",
        {"name": f"{SMOKE_PREFIX} 人设2 {run}", "account_id": account_id, "is_default": True},
    )
    c.check(status == 201 and persona2["is_default"] is True, "同账号第二条默认人设创建成功")
    status, payload = request(base, "GET", f"/api/v1/personas/{persona1_id}")
    c.check(payload["is_default"] is False, "同账号范围内默认人设唯一（旧默认被取消）", str(payload))

    # 公共人设库
    status, public_persona = request(
        base,
        "POST",
        "/api/v1/personas",
        {"name": f"{SMOKE_PREFIX} 公共人设 {run}", "is_default": True},
    )
    c.check(
        status == 201 and public_persona["account_id"] is None and public_persona["account_name"] is None,
        "公共人设（account_id=null）创建成功且 account_name 为 null",
        f"status={status} body={public_persona}",
    )

    # 归属账号不存在 -> 400
    status, payload = request(
        base,
        "POST",
        "/api/v1/personas",
        {"name": f"{SMOKE_PREFIX} 坏归属 {run}", "account_id": ghost},
    )
    c.check(
        status == 400 and payload.get("code") == "bad_request",
        "归属账号不存在返回 400 bad_request",
        f"status={status} body={payload}",
    )

    # 人设列表过滤
    status, payload = request(base, "GET", "/api/v1/personas?account_id=public&q=%5BSMOKE%5D")
    c.check(
        all(item["account_id"] is None for item in payload["items"]) and payload["total"] >= 1,
        "account_id=public 只看公共人设库",
        str(payload)[:200],
    )
    status, payload = request(base, "GET", f"/api/v1/personas?account_id={account_id}&q=%5BSMOKE%5D")
    c.check(payload["total"] == 2, "account_id=<uuid> 只返回该账号的人设（2 条）", str(payload)[:200])

    # 人设详情 / 更新 / 404
    status, payload = request(base, "GET", f"/api/v1/personas/{ghost}")
    c.check(status == 404, "GET 不存在的人设返回 404", f"status={status}")
    status, payload = request(
        base, "PATCH", f"/api/v1/personas/{persona1_id}", {"tone": "专业", "target_audience": "应届生"}
    )
    c.check(
        status == 200 and payload["tone"] == "专业" and payload["target_audience"] == "应届生",
        "PATCH /personas/{id} 更新生效",
        f"status={status} body={payload}",
    )
    status, payload = request(
        base, "PATCH", f"/api/v1/personas/{persona1_id}", {"content_pillars": [f"t{i}" for i in range(11)]}
    )
    c.check(status == 422, "content_pillars 超 10 项返回 422", f"status={status}")

    # ------------------------------------------------------------ 6. 归档 / 恢复
    status, payload = request(base, "PATCH", f"/api/v1/accounts/{account_id}", {"status": "archived"})
    c.check(
        status == 200 and payload["status"] == "archived",
        "PATCH status=archived 归档账号",
        f"status={status} body={payload}",
    )
    status, payload = request(base, "GET", "/api/v1/accounts?q=%5BSMOKE%5D")
    c.check(
        all(item["id"] != account_id for item in payload["items"]),
        "归档后默认列表（status=active）不可见",
        str(payload)[:200],
    )
    status, payload = request(base, "GET", "/api/v1/accounts?q=%5BSMOKE%5D&status=archived")
    c.check(
        any(item["id"] == account_id for item in payload["items"]),
        "status=archived 可查到已归档账号",
    )
    status, payload = request(base, "GET", f"/api/v1/personas?account_id={account_id}&q=%5BSMOKE%5D")
    c.check(
        payload["total"] == 2,
        "归档账号不级联改动人设（人设仍在、仍为 active）",
        str(payload)[:200],
    )

    status, payload = request(
        base, "PATCH", f"/api/v1/personas/{persona2['id']}", {"status": "archived"}
    )
    c.check(status == 200 and payload["status"] == "archived", "人设归档成功", str(payload))
    status, payload = request(
        base, "PATCH", f"/api/v1/personas/{persona2['id']}", {"status": "active"}
    )
    c.check(status == 200 and payload["status"] == "active", "人设恢复成功", str(payload))

    status, payload = request(base, "PATCH", f"/api/v1/accounts/{account_id}", {"status": "active"})
    c.check(status == 200 and payload["status"] == "active", "账号恢复成功", str(payload))

    # ------------------------------------------------------------ 7. 清理与复查
    print("\n=== 收尾清理（物理删除 [SMOKE] 数据）===")
    try:
        p, a = purge_smoke_data()
        print(f"已清理 personas={p} accounts={a}")
        rp, ra = count_smoke_residual()
        c.check(rp == 0 and ra == 0, "库内 [SMOKE] 残留为 0", f"personas={rp} accounts={ra}")
    except Exception as exc:  # noqa: BLE001
        c.check(False, "收尾清理与残留复查", str(exc))

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for f in c.failures:
            print(f"  - FAIL: {f}")
        return 1
    print("全部 PASS ✔（在线冒烟，数据已清理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
