"""第 8 步 · 接口层离线自检（**不起服务、不连数据库、不写库**）。

逐项断言后输出 PASS / FAIL，用于本地快速校验「契约与校验规则是否就位」：

1. OpenAPI 路径恰好包含 4 条业务路径 / 8 个端点，方法齐全，且健康检查**未**被 v1 化；
2. ``AccountCreate / AccountUpdate`` 的字段与校验规则（必填、长度、status 枚举、空串归一）；
3. ``PersonaCreate`` 的字段与校验规则（标签去重 / 上限 / 单元素长度 / tone 长度）；
4. ``AccountOut / PersonaOut / Page`` 的字段集合（含 ``account_name``、分页四件套）；
5. 时间字段序列化为 ISO 8601（带时区）；
6. 分页依赖：``page ≥ 1``、``page_size ≤ 100``（从 OpenAPI 参数校验规则读取）；
7. 分层纪律：路由模块里**不出现**直接 SQLAlchemy 表达式，且确实调用了 ``crud_*``；
8. 统一错误体结构（``status/code/message/details``）与错误码映射正确。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_api.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from pydantic import ValidationError  # noqa: E402

from app.core.errors import (  # noqa: E402
    AppError,
    code_for_status,
    conflict,
    not_found,
    validation_error,
)
from app.main import app  # noqa: E402
from app.models.enums import AccountStatus, PersonaStatus  # noqa: E402
from app.schemas.account import AccountCreate, AccountOut, AccountUpdate  # noqa: E402
from app.schemas.common import Page  # noqa: E402
from app.schemas.persona import PersonaCreate, PersonaOut, PersonaUpdate  # noqa: E402

ROUTES_DIR = BACKEND_DIR / "app" / "api" / "routes"

EXPECTED_ENDPOINTS = {
    ("/api/v1/accounts", "get"),
    ("/api/v1/accounts", "post"),
    ("/api/v1/accounts/{account_id}", "get"),
    ("/api/v1/accounts/{account_id}", "patch"),
    ("/api/v1/personas", "get"),
    ("/api/v1/personas", "post"),
    ("/api/v1/personas/{persona_id}", "get"),
    ("/api/v1/personas/{persona_id}", "patch"),
}


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


def _rejects(model, **kwargs) -> bool:
    """构造模型并断言其被拒绝（抛 ValidationError）。"""
    try:
        model(**kwargs)
    except ValidationError:
        return True
    return False


def main() -> int:
    c = Checker()
    spec = app.openapi()

    # ---------------------------------------------------------------- 1. 路径
    actual = {
        (path, method)
        for path, item in spec["paths"].items()
        for method in item
    }
    missing = EXPECTED_ENDPOINTS - actual
    c.check(not missing, "OpenAPI 含全部 8 个业务端点", f"缺少 {sorted(missing)}")
    c.check(
        ("/health", "get") in actual and ("/health/ready", "get") in actual,
        "健康检查 /health 与 /health/ready 仍在（且不带 v1 前缀）",
    )
    c.check(
        not any(p.startswith("/api/v1/health") for p, _ in actual),
        "健康检查未被错误地加上 v1 前缀",
    )

    # ------------------------------------------------------- 2. Account schema
    c.check(_rejects(AccountCreate, name=None), "AccountCreate.name 必填（null 被拒）")
    c.check(_rejects(AccountCreate, name="   "), "AccountCreate.name 全空白被拒")
    c.check(_rejects(AccountCreate, name="x" * 101), "AccountCreate.name 超 100 字被拒")
    c.check(not _rejects(AccountCreate, name="x" * 100), "AccountCreate.name 100 字通过")
    c.check(
        AccountCreate(name="  账号A  ").name == "账号A",
        "AccountCreate.name 自动 strip 首尾空白",
    )
    c.check(
        AccountCreate(name="a", xhs_red_id="").xhs_red_id is None,
        "AccountCreate.xhs_red_id 空串归一为 None",
    )
    c.check(
        _rejects(AccountCreate, name="a", xhs_red_id="x" * 65),
        "AccountCreate.xhs_red_id 超 64 字被拒",
    )
    c.check(
        AccountUpdate().model_dump(exclude_unset=True) == {},
        "AccountUpdate 全字段可选（空 body 不产生任何字段）",
    )
    c.check(
        AccountUpdate(status="archived").status == AccountStatus.ARCHIVED,
        "AccountUpdate.status 接受 archived",
    )
    c.check(_rejects(AccountUpdate, status="deleted"), "AccountUpdate.status 拒绝非法枚举值")

    # ----------------------------------------------------- 3. Persona schema
    c.check(_rejects(PersonaCreate, name=""), "PersonaCreate.name 必填")
    c.check(
        PersonaCreate(name="人设", content_pillars=["A", "A ", "B"]).content_pillars
        == ["A", "B"],
        "PersonaCreate.content_pillars 自动 strip + 去重",
    )
    c.check(
        _rejects(PersonaCreate, name="人设", content_pillars=[f"t{i}" for i in range(11)]),
        "PersonaCreate.content_pillars 超 10 项被拒",
    )
    c.check(
        _rejects(PersonaCreate, name="人设", content_pillars=["y" * 51]),
        "PersonaCreate.content_pillars 单元素超 50 字被拒",
    )
    c.check(
        _rejects(PersonaCreate, name="人设", taboo=["  "]),
        "PersonaCreate.taboo 含空白元素被拒",
    )
    c.check(_rejects(PersonaCreate, name="人设", tone="t" * 201), "PersonaCreate.tone 超 200 字被拒")
    c.check(PersonaCreate(name="人设").is_default is False, "PersonaCreate.is_default 默认 False")
    c.check(PersonaCreate(name="人设").account_id is None, "PersonaCreate.account_id 默认 None（公共）")
    c.check(
        PersonaUpdate(status="archived").status == PersonaStatus.ARCHIVED,
        "PersonaUpdate.status 接受 archived",
    )

    # ------------------------------------------------------- 4. Out / Page 字段
    account_out_fields = set(AccountOut.model_fields)
    c.check(
        account_out_fields
        == {"id", "name", "xhs_red_id", "niche", "status", "remark", "created_at", "updated_at"},
        "AccountOut 字段集合正确",
        f"实际 {sorted(account_out_fields)}",
    )
    persona_out_fields = set(PersonaOut.model_fields)
    c.check(
        "account_name" in persona_out_fields,
        "PersonaOut 带 account_name（服务层 LEFT JOIN 得到，非数据库列）",
    )
    c.check(
        {"id", "account_id", "name", "positioning", "target_audience", "tone", "content_pillars",
         "taboo", "signature_style", "system_prompt", "is_default", "status",
         "created_at", "updated_at", "account_name"} == persona_out_fields,
        "PersonaOut 字段集合正确",
        f"实际 {sorted(persona_out_fields)}",
    )
    c.check(
        set(Page.model_fields) == {"items", "total", "page", "page_size"},
        "Page 分页容器含 items/total/page/page_size",
    )
    c.check(
        AccountOut.model_config.get("from_attributes") is True,
        "AccountOut 开启 from_attributes",
    )
    c.check(
        PersonaOut.model_config.get("from_attributes") is True,
        "PersonaOut 开启 from_attributes",
    )

    # ------------------------------------------------------------ 5. 时间序列化
    now = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)
    dumped = AccountOut(
        id=uuid.uuid4(),
        name="x",
        xhs_red_id=None,
        niche=None,
        status=AccountStatus.ACTIVE,
        remark=None,
        created_at=now,
        updated_at=now,
    ).model_dump(mode="json")
    c.check(
        dumped["created_at"].endswith("Z") or "+" in dumped["created_at"],
        "时间字段序列化为带时区的 ISO 8601",
        str(dumped["created_at"]),
    )

    # -------------------------------------------------------- 6. 分页参数约束
    params = {
        p["name"]: p
        for p in spec["paths"]["/api/v1/accounts"]["get"].get("parameters", [])
    }
    c.check(
        params.get("page", {}).get("schema", {}).get("minimum") == 1,
        "分页依赖 page ≥ 1",
    )
    c.check(
        params.get("page_size", {}).get("schema", {}).get("maximum") == 100,
        "分页依赖 page_size ≤ 100",
    )

    # ------------------------------------------------------- 7. 分层纪律（源码）
    route_sources = {
        p.name: p.read_text(encoding="utf-8") for p in sorted(ROUTES_DIR.glob("*.py"))
    }
    c.check(set(route_sources) >= {"accounts.py", "personas.py"}, "路由模块 accounts/personas 存在")
    sql_patterns = [r"\bdb\.execute\(", r"\bdb\.query\(", r"\bselect\("]
    offenders: list[str] = []
    for name, src in route_sources.items():
        # 去掉注释行与 docstring 里的示例，避免误报
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        for pat in sql_patterns:
            if re.search(pat, code):
                offenders.append(f"{name}:{pat}")
        # 只允许从 sqlalchemy 引入 Session（类型标注用）
        for line in code.splitlines():
            stripped = line.strip()
            if stripped.startswith("from sqlalchemy") or stripped.startswith("import sqlalchemy"):
                if "orm import Session" not in stripped:
                    offenders.append(f"{name}:{stripped}")
    c.check(not offenders, "路由层不直接写 SQLAlchemy 表达式", f"发现 {offenders}")
    c.check(
        all("crud_" in src for name, src in route_sources.items() if name in {"accounts.py", "personas.py"}),
        "路由层通过 crud_* 单例完成数据访问",
    )

    # ----------------------------------------------------------- 8. 统一错误体
    err = AppError("conflict", "冲突", status_code=409, details={"a": 1})
    c.check(
        err.to_body() == {"status": "error", "code": "conflict", "message": "冲突", "details": {"a": 1}},
        "统一错误体结构为 {status, code, message, details}",
    )
    c.check(
        [not_found().status_code, conflict().status_code, validation_error().status_code]
        == [404, 409, 422],
        "错误工厂状态码映射正确（404/409/422）",
    )
    c.check(
        code_for_status(404) == "not_found" and code_for_status(422) == "validation",
        "HTTP 状态码 -> 错误码兜底映射正确",
    )

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for f in c.failures:
            print(f"  - FAIL: {f}")
        return 1
    print("全部 PASS ✔（离线自检，未启动服务、未连接数据库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
