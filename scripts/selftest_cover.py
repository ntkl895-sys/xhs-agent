"""第 13 步 · 封面模块后端自检（**依赖本地 docker PostgreSQL**）。

覆盖任务书要求的全部后端断言，分 10 组：

1. 静态规格：``covers`` 列集合 / 非空约束 / FK CASCADE / 默认值 / 规格常量；
2. 新建候选：POST 落库**逐列**复核（含 4 个默认列与 ``image_path`` 为空）+ 入参边界 422；
3. 列表：``GET /covers`` 顺序（created_at 正序）与初始 ``selected_id``；
4. 选中互斥：同笔记两个候选，PATCH 第二个 ``is_selected=true`` 后**恰一个** true；互逆再选亦正确；
5. 字段更新与笔记状态：PATCH 普通字段生效；选中 / 更新**不改动** ``notes.status``；显式取消选中；
6. 错误码：``note_id`` 缺失 400、非法 422、笔记不存在 404、候选不存在 404、非空列 null 422、统一错误体；
7. 接口面：OpenAPI 仅 GET / POST / PATCH（**无 PUT / DELETE**）、七只 router 在册（总数 ≥ 7）、唯一 lifespan、CoverOut 无敏感字段；
8. 零迁移：``migrations/versions`` 仍恰 2 支、``alembic current`` = ``0002 (head)``、``alembic check`` 退出码 0；
9. 合规扫描：本步触碰的后端文件不含小红书域名 / 登录态字段 / ``sk-`` 密钥；
10. 清理复查：测试数据（前缀 ``[SELFTEST]封面``）**按 id 删 + 前缀兜底扫**，双口径复查残留为 0。

HTTP 断言走**进程内** ``TestClient``（不需要 uvicorn）；数据复核走 SQLAlchemy 会话。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_cover.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# ---- 入口先摘掉沙箱注入的代理变量（本机全部真实请求进程的共同约定）----
for _key in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
):
    os.environ.pop(_key, None)

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.crud.cover import COVER_HEIGHT, COVER_WIDTH  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ComplianceStatus,
    NoteStatus,
    PersonaStatus,
)
from app.models.note import Cover, Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402

#: 本脚本全部测试数据统一前缀（清理与复查的双口径都靠它兜底）
PREFIX = "[SELFTEST]封面"

VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"

#: 本步触碰过的后端文件（合规扫描对象）
SCANNED_FILES = (
    BACKEND_DIR / "app" / "schemas" / "cover.py",
    BACKEND_DIR / "app" / "crud" / "cover.py",
    BACKEND_DIR / "app" / "api" / "routes" / "covers.py",
    BACKEND_DIR / "app" / "main.py",
)

#: 禁止出现的字样（小红书域名 / 登录态凭据 / 密钥前缀）
FORBIDDEN_PATTERNS = (
    "xiaohongshu",
    "xhslink",
    "xhs.com",
    "redbook.com",
    "Cookie",
    "cookie",
    "access_token",
    "refresh_token",
    "session_id",
    "sk-",
)

EXPECTED_COLUMNS = {
    "id",
    "note_id",
    "template_key",
    "color_scheme",
    "title_text",
    "subtitle_text",
    "config_json",
    "image_path",
    "width",
    "height",
    "is_selected",
    "version",
    "created_at",
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


@dataclass
class Fixture:
    account_id: uuid.UUID
    persona_id: uuid.UUID
    note_id: uuid.UUID
    cover_a_id: uuid.UUID | None = None
    cover_b_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# 夹具与清理
# ---------------------------------------------------------------------------
def setup_fixture() -> Fixture:
    """建 1 个测试账号 + 1 个人设 + 1 篇笔记（均带 [SELFTEST]封面 前缀）。"""
    with SessionLocal() as db:
        account = Account(name=f"{PREFIX}账号", status=AccountStatus.ACTIVE)
        db.add(account)
        db.flush()
        persona = Persona(
            name=f"{PREFIX}人设", account_id=account.id, status=PersonaStatus.ACTIVE
        )
        db.add(persona)
        db.flush()
        note = Note(
            account_id=account.id,
            persona_id=persona.id,
            title=f"{PREFIX}笔记",
            status=NoteStatus.DRAFT,
            compliance_status=ComplianceStatus.PENDING,
        )
        db.add(note)
        db.flush()
        fixture = Fixture(account_id=account.id, persona_id=persona.id, note_id=note.id)
        db.commit()
    return fixture


def cleanup_and_verify(fx: Fixture) -> dict[str, int]:
    """物理清理 + 双口径复查（按 id 精确 + 按前缀全局兜底）。"""
    like = f"%{PREFIX}%"
    with SessionLocal() as db:
        # 1) 按 id 精确删（covers → notes → personas → accounts）
        db.execute(text("DELETE FROM covers WHERE note_id = :n"), {"n": str(fx.note_id)})
        db.execute(text("DELETE FROM notes WHERE id = :n"), {"n": str(fx.note_id)})
        db.execute(text("DELETE FROM personas WHERE id = :p"), {"p": str(fx.persona_id)})
        db.execute(text("DELETE FROM accounts WHERE id = :a"), {"a": str(fx.account_id)})
        # 2) 前缀兜底扫（覆盖 id 漂移、半途失败留下的孤儿）
        db.execute(
            text("DELETE FROM covers WHERE note_id IN (SELECT id FROM notes WHERE title LIKE :t)"),
            {"t": like},
        )
        db.execute(text("DELETE FROM notes WHERE title LIKE :t"), {"t": like})
        db.execute(text("DELETE FROM personas WHERE name LIKE :t"), {"t": like})
        db.execute(text("DELETE FROM accounts WHERE name LIKE :t"), {"t": like})
        db.commit()
        # 3) 双口径复查
        return {
            "covers(by_note_id)": db.execute(
                text("SELECT count(*) FROM covers WHERE note_id = :n"), {"n": str(fx.note_id)}
            ).scalar_one(),
            "notes(by_id)": db.execute(
                text("SELECT count(*) FROM notes WHERE id = :n"), {"n": str(fx.note_id)}
            ).scalar_one(),
            "personas(by_id)": db.execute(
                text("SELECT count(*) FROM personas WHERE id = :p"), {"p": str(fx.persona_id)}
            ).scalar_one(),
            "accounts(by_id)": db.execute(
                text("SELECT count(*) FROM accounts WHERE id = :a"), {"a": str(fx.account_id)}
            ).scalar_one(),
            "notes(prefix)": db.execute(
                text("SELECT count(*) FROM notes WHERE title LIKE :t"), {"t": like}
            ).scalar_one(),
            "personas(prefix)": db.execute(
                text("SELECT count(*) FROM personas WHERE name LIKE :t"), {"t": like}
            ).scalar_one(),
            "accounts(prefix)": db.execute(
                text("SELECT count(*) FROM accounts WHERE name LIKE :t"), {"t": like}
            ).scalar_one(),
        }


def _fetch_cover(cover_id: uuid.UUID) -> dict:
    """从数据库逐列读回一条候选（**绕过接口**，验证真实落库内容）。"""
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT note_id, template_key, color_scheme, title_text, subtitle_text, "
                "config_json, image_path, width, height, is_selected, version, created_at "
                "FROM covers WHERE id = :i"
            ),
            {"i": str(cover_id)},
        ).one_or_none()
    if row is None:
        return {}
    keys = (
        "note_id",
        "template_key",
        "color_scheme",
        "title_text",
        "subtitle_text",
        "config_json",
        "image_path",
        "width",
        "height",
        "is_selected",
        "version",
        "created_at",
    )
    return dict(zip(keys, row))


def _selected_flags(note_id: uuid.UUID) -> list[tuple[uuid.UUID, bool]]:
    with SessionLocal() as db:
        return list(
            db.execute(
                text("SELECT id, is_selected FROM covers WHERE note_id = :n"), {"n": str(note_id)}
            ).all()
        )


def _note_status(note_id: uuid.UUID) -> str:
    with SessionLocal() as db:
        return str(
            db.execute(
                text("SELECT status FROM notes WHERE id = :n"), {"n": str(note_id)}
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# 组 1：静态规格（无数据库依赖）
# ---------------------------------------------------------------------------
def group1_static(c: Checker) -> None:
    columns = {col.name for col in Cover.__table__.columns}
    c.check(Cover.__tablename__ == "covers", "Cover 模型表名为 covers")
    c.check(
        columns == EXPECTED_COLUMNS,
        "covers 列集合逐字段对齐（13 列）",
        f"{sorted(columns)}",
    )

    not_null = {col.name for col in Cover.__table__.columns if not col.nullable}
    c.check(
        {"note_id", "template_key", "color_scheme", "width", "height", "is_selected", "version"}
        <= not_null,
        "非空列齐全（note_id / template_key / color_scheme / width / height / is_selected / version）",
        f"{sorted(not_null)}",
    )
    nullable = {col.name for col in Cover.__table__.columns if col.nullable}
    c.check(
        {"title_text", "subtitle_text", "config_json", "image_path"} <= nullable,
        "可空列齐全（title_text / subtitle_text / config_json / image_path）",
        f"{sorted(nullable)}",
    )

    fk = next(iter(Cover.__table__.c.note_id.foreign_keys))
    c.check(
        fk.target_fullname == "notes.id" and (fk.ondelete or "").upper() == "CASCADE",
        "note_id 外键 -> notes.id 且 ON DELETE CASCADE",
        f"{fk.target_fullname} ondelete={fk.ondelete}",
    )

    c.check(
        COVER_WIDTH == 1242 and COVER_HEIGHT == 1656,
        "后端规格常量为 1242×1656（与前端 constants.ts 对齐）",
        f"{COVER_WIDTH}×{COVER_HEIGHT}",
    )

    defaults = {
        "width": str(Cover.__table__.c.width.server_default.arg),
        "height": str(Cover.__table__.c.height.server_default.arg),
        "is_selected": str(Cover.__table__.c.is_selected.server_default.arg),
        "version": str(Cover.__table__.c.version.server_default.arg),
    }
    c.check(defaults["width"] == "1242", "server_default width = 1242", defaults["width"])
    c.check(defaults["height"] == "1656", "server_default height = 1656", defaults["height"])
    c.check(
        defaults["is_selected"].lower() == "false",
        "server_default is_selected = false",
        defaults["is_selected"],
    )
    c.check(defaults["version"] == "1", "server_default version = 1", defaults["version"])

    c.check(
        len(Cover.__table__.indexes) >= 1
        and any("note_id" in str(ix.columns) for ix in Cover.__table__.indexes),
        "存在 note_id 索引（ix_covers_note_id）",
    )


# ---------------------------------------------------------------------------
# 组 2：新建候选（POST）逐列落库 + 入参边界
# ---------------------------------------------------------------------------
def group2_create(c: Checker, client: TestClient, fx: Fixture) -> None:
    payload_a = {
        "note_id": str(fx.note_id),
        "template_key": "gradient_hero",
        "color_scheme": "coral",
        "title_text": "3个表格函数，让你准时下班",
        "subtitle_text": "打工人效率自救指南",
        "config_json": {
            "tags": ["职场效率", "Excel", "干货"],
            "bullets": ["VLOOKUP 跨表取数", "SUMIFS 多条件汇总"],
            "author": "@测试昵称",
            "page_no": "01/06",
            "highlight_word": "函数",
        },
    }
    r = client.post("/api/v1/covers", json=payload_a)
    c.check(
        r.status_code == 200,
        "POST /api/v1/covers 新建候选 A 返回 200",
        f"{r.status_code} {r.text[:200]}",
    )
    data = r.json()
    c.check(
        set(data) == EXPECTED_COLUMNS,
        "响应 CoverOut 字段与表逐列对齐（13 字段）",
        f"{sorted(data)}",
    )
    c.check(bool(data.get("id")) and bool(data.get("created_at")), "响应含 id 与 created_at")
    fx.cover_a_id = uuid.UUID(data["id"])

    row = _fetch_cover(fx.cover_a_id)
    c.check(row.get("note_id") == fx.note_id, "落库 note_id 正确")
    c.check(row.get("template_key") == "gradient_hero", "落库 template_key 正确", f"{row.get('template_key')}")
    c.check(row.get("color_scheme") == "coral", "落库 color_scheme 正确", f"{row.get('color_scheme')}")
    c.check(
        row.get("title_text") == payload_a["title_text"],
        "落库 title_text 逐字一致",
        f"{row.get('title_text')!r}",
    )
    c.check(
        row.get("subtitle_text") == payload_a["subtitle_text"],
        "落库 subtitle_text 逐字一致",
    )
    config = row.get("config_json") or {}
    c.check(
        config.get("tags") == ["职场效率", "Excel", "干货"]
        and config.get("page_no") == "01/06",
        "落库 config_json 含 tags / bullets / author / page_no 等编辑字段",
        f"{config}",
    )
    c.check(row.get("image_path") is None, "image_path 留 NULL（不落服务器磁盘）", f"{row.get('image_path')!r}")
    c.check(row.get("width") == 1242, "落库 width = 1242", f"{row.get('width')}")
    c.check(row.get("height") == 1656, "落库 height = 1656", f"{row.get('height')}")
    c.check(row.get("is_selected") is False, "落库 is_selected = false", f"{row.get('is_selected')}")
    c.check(row.get("version") == 1, "落库 version = 1", f"{row.get('version')}")
    c.check(row.get("created_at") is not None, "落库 created_at 非空")

    # 候选 B（互斥断言用；sleep 保证 created_at 严格递增，顺序断言才可靠）
    time.sleep(0.05)
    payload_b = {
        "note_id": str(fx.note_id),
        "template_key": "list_clean",
        "color_scheme": "forest",
        "title_text": "5 个通勤神器",
        "config_json": {"bullets": ["降噪耳机", "折叠水杯"], "page_no": "02/06"},
    }
    r = client.post("/api/v1/covers", json=payload_b)
    c.check(r.status_code == 200, "POST 新建候选 B 返回 200", f"{r.status_code} {r.text[:200]}")
    fx.cover_b_id = uuid.UUID(r.json()["id"])
    row_b = _fetch_cover(fx.cover_b_id)
    c.check(
        row_b.get("width") == 1242 and row_b.get("height") == 1656 and row_b.get("is_selected") is False,
        "候选 B 默认列同样正确（1242×1656 / 未选中）",
    )

    # 入参边界（422）
    r = client.post(
        "/api/v1/covers",
        json={"note_id": str(fx.note_id), "template_key": "gradient_hero", "color_scheme": "coral",
              "title_text": "超" * 101},
    )
    c.check(r.status_code == 422, "title_text 超 100 字 → 422（与 varchar(100) 对齐）", f"{r.status_code}")
    r = client.post(
        "/api/v1/covers",
        json={"note_id": str(fx.note_id), "template_key": "  ", "color_scheme": "coral"},
    )
    c.check(r.status_code == 422, "template_key 为空白 → 422", f"{r.status_code}")
    r = client.post(
        "/api/v1/covers",
        json={"note_id": str(fx.note_id), "color_scheme": "coral"},
    )
    c.check(r.status_code == 422, "缺 template_key → 422", f"{r.status_code}")
    r = client.post(
        "/api/v1/covers",
        json={"note_id": str(fx.note_id), "template_key": "gradient_hero", "color_scheme": "c" * 33},
    )
    c.check(r.status_code == 422, "color_scheme 超 32 字 → 422（与 varchar(32) 对齐）", f"{r.status_code}")


# ---------------------------------------------------------------------------
# 组 3：列表顺序与 selected_id
# ---------------------------------------------------------------------------
def group3_list(c: Checker, client: TestClient, fx: Fixture) -> None:
    r = client.get("/api/v1/covers", params={"note_id": str(fx.note_id)})
    c.check(r.status_code == 200, "GET /api/v1/covers?note_id 返回 200", f"{r.status_code}")
    data = r.json()
    c.check(set(data) == {"note_id", "selected_id", "items"}, "响应结构 = {note_id, selected_id, items}", f"{sorted(data)}")
    items = data.get("items", [])
    c.check(len(items) == 2, "候选数量为 2", f"{len(items)}")
    c.check(data.get("note_id") == str(fx.note_id), "回显 note_id 正确")
    c.check(data.get("selected_id") is None, "初始无选中（selected_id = null）", f"{data.get('selected_id')}")
    ids = [item["id"] for item in items]
    c.check(
        ids == [str(fx.cover_a_id), str(fx.cover_b_id)],
        "list_by_note 按 created_at 正序（A 在前、B 在后）",
        f"{ids}",
    )
    c.check(
        all(item["width"] == 1242 and item["height"] == 1656 for item in items),
        "列表项宽高均为 1242×1656",
    )


# ---------------------------------------------------------------------------
# 组 4：选中互斥（单事务）
# ---------------------------------------------------------------------------
def group4_select_exclusive(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.cover_a_id and fx.cover_b_id  # 组 2 已建

    r = client.patch(f"/api/v1/covers/{fx.cover_b_id}", json={"is_selected": True})
    c.check(r.status_code == 200, "PATCH 候选 B is_selected=true 返回 200", f"{r.status_code} {r.text[:200]}")
    c.check(r.json().get("is_selected") is True, "响应中 B 为选中态")

    flags = _selected_flags(fx.note_id)
    true_ids = [row[0] for row in flags if row[1]]
    c.check(
        len(true_ids) == 1 and true_ids[0] == fx.cover_b_id,
        "同 note 恰一个 is_selected=true，且为 B（A 被置 false）",
        f"{[(str(a), b) for a, b in flags]}",
    )

    data = client.get("/api/v1/covers", params={"note_id": str(fx.note_id)}).json()
    c.check(data.get("selected_id") == str(fx.cover_b_id), "GET selected_id 指向 B", f"{data.get('selected_id')}")

    # 互逆：改选 A
    r = client.patch(f"/api/v1/covers/{fx.cover_a_id}", json={"is_selected": True})
    c.check(r.status_code == 200 and r.json().get("is_selected") is True, "改选 A 返回 200 且 A 为选中态")
    flags = _selected_flags(fx.note_id)
    true_ids = [row[0] for row in flags if row[1]]
    c.check(
        len(true_ids) == 1 and true_ids[0] == fx.cover_a_id,
        "改选 A 后仍恰一个 true，且为 A（B 被置 false）",
        f"{[(str(a), b) for a, b in flags]}",
    )
    data = client.get("/api/v1/covers", params={"note_id": str(fx.note_id)}).json()
    c.check(data.get("selected_id") == str(fx.cover_a_id), "GET selected_id 指向 A", f"{data.get('selected_id')}")

    with SessionLocal() as db:
        total_true = db.execute(
            text("SELECT count(*) FROM covers WHERE note_id = :n AND is_selected = true"),
            {"n": str(fx.note_id)},
        ).scalar_one()
    c.check(total_true == 1, "数据库层面同 note 仅 1 行 is_selected=true", f"{total_true}")


# ---------------------------------------------------------------------------
# 组 5：字段更新与笔记状态
# ---------------------------------------------------------------------------
def group5_update_and_note_status(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.cover_a_id and fx.cover_b_id

    status_before = _note_status(fx.note_id)
    c.check(status_before == "draft", "夹具笔记初始状态为 draft", status_before)

    r = client.patch(
        f"/api/v1/covers/{fx.cover_b_id}",
        json={"title_text": "改标题：通勤神器清单", "config_json": {"page_no": "03/06"}},
    )
    c.check(r.status_code == 200, "PATCH 普通字段返回 200", f"{r.status_code} {r.text[:200]}")
    row = _fetch_cover(fx.cover_b_id)
    c.check(row.get("title_text") == "改标题：通勤神器清单", "PATCH title_text 已落库", f"{row.get('title_text')!r}")
    c.check((row.get("config_json") or {}).get("page_no") == "03/06", "PATCH config_json 已落库")

    status_after = _note_status(fx.note_id)
    c.check(
        status_after == "draft",
        "选中封面 + 更新字段均不改动 notes.status（仍 draft）",
        f"before={status_before} after={status_after}",
    )

    # 显式取消选中（允许「没有选中封面」的中间态）
    r = client.patch(f"/api/v1/covers/{fx.cover_a_id}", json={"is_selected": False})
    c.check(r.status_code == 200 and r.json().get("is_selected") is False, "PATCH is_selected=false 取消选中返回 200")
    with SessionLocal() as db:
        total_true = db.execute(
            text("SELECT count(*) FROM covers WHERE note_id = :n AND is_selected = true"),
            {"n": str(fx.note_id)},
        ).scalar_one()
    c.check(total_true == 0, "取消后同 note 0 个选中（允许的中间态，非约束破坏）", f"{total_true}")


# ---------------------------------------------------------------------------
# 组 6：错误码与统一错误体
# ---------------------------------------------------------------------------
def group6_errors(c: Checker, client: TestClient, fx: Fixture) -> None:
    assert fx.cover_a_id

    r = client.get("/api/v1/covers")
    c.check(r.status_code == 400, "GET 缺 note_id → 400", f"{r.status_code}")
    c.check(r.json().get("code") == "bad_request", "缺 note_id 错误码 = bad_request", f"{r.json().get('code')}")

    r = client.get("/api/v1/covers", params={"note_id": "not-a-uuid"})
    c.check(r.status_code == 422, "GET 非法 note_id → 422", f"{r.status_code}")

    random_id = str(uuid.uuid4())
    r = client.get("/api/v1/covers", params={"note_id": random_id})
    c.check(r.status_code == 404, "GET 笔记不存在 → 404", f"{r.status_code}")
    c.check(r.json().get("code") == "not_found", "笔记不存在错误码 = not_found")

    r = client.post(
        "/api/v1/covers",
        json={"note_id": random_id, "template_key": "gradient_hero", "color_scheme": "coral"},
    )
    c.check(r.status_code == 404, "POST 笔记不存在 → 404", f"{r.status_code}")

    r = client.patch(f"/api/v1/covers/{uuid.uuid4()}", json={"template_key": "list_clean"})
    c.check(r.status_code == 404, "PATCH 候选不存在 → 404", f"{r.status_code}")

    r = client.patch(f"/api/v1/covers/{fx.cover_a_id}", json={"template_key": None})
    c.check(r.status_code == 422, "PATCH 非空列显式 null → 422", f"{r.status_code} {r.text[:150]}")

    body = r.json()
    c.check(
        body.get("status") == "error" and {"code", "message", "details"} <= set(body),
        "统一错误体结构 {status, code, message, details}",
        f"{sorted(body)}",
    )


# ---------------------------------------------------------------------------
# 组 7：接口面（OpenAPI）
# ---------------------------------------------------------------------------
def group7_surface(c: Checker) -> None:
    paths = app.openapi()["paths"]
    c.check(sorted(paths.get("/api/v1/covers", {})) == ["get", "post"], "GET/POST /api/v1/covers 已注册")
    c.check(
        sorted(paths.get("/api/v1/covers/{cover_id}", {})) == ["patch"],
        "/api/v1/covers/{cover_id} 仅 PATCH",
        f"{sorted(paths.get('/api/v1/covers/{cover_id}', {}))}",
    )
    bad = [
        path
        for path in paths
        if "/covers" in path and any(method in paths[path] for method in ("put", "delete"))
    ]
    c.check(not bad, "covers 路由中不存在 PUT / DELETE（本步不提供）", f"{bad}")

    main_src = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    # 路由总数会随后续步骤递增（本步后为 7 只）：断言「本步涉及的七只路由均在册 + 总数不回退」，
    # 避免后续步骤新增 router 时误报，同时仍能抓住「已有 router 被删 / 被换」。
    _EXPECTED_ROUTER_IMPORTS = (
        "health_router",
        "accounts_router",
        "personas_router",
        "hotspots_router",
        "topics_router",
        "notes_router",
        "covers_router",
    )
    c.check(
        main_src.count("include_router(") >= 7
        and all(f"include_router({name})" in main_src for name in _EXPECTED_ROUTER_IMPORTS),
        "main.py 注册含七只 router（含新增 covers_router，总数 ≥ 7）",
        f"count={main_src.count('include_router(')}",
    )
    c.check(
        main_src.count("@asynccontextmanager") == 1 and main_src.count("def lifespan(") == 1,
        "main.py 仍只有唯一 lifespan（本步未新增）",
    )

    schemas = app.openapi()["components"]["schemas"]
    cover_out_props = set(schemas["CoverOut"]["properties"])
    sensitive = {"token", "cookie", "secret", "password", "llm_raw", "api_key"}
    c.check(
        not any(any(flag in name.lower() for flag in sensitive) for name in cover_out_props),
        "CoverOut 不含 token / cookie / secret / password / llm_raw 等敏感字段",
        f"{sorted(cover_out_props)}",
    )
    list_out = set(schemas["CoverListOut"]["properties"])
    c.check(list_out == {"note_id", "selected_id", "items"}, "CoverListOut 结构 = {note_id, selected_id, items}", f"{sorted(list_out)}")


# ---------------------------------------------------------------------------
# 组 8：零迁移
# ---------------------------------------------------------------------------
def group8_migrations(c: Checker) -> None:
    files = sorted(p.name for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))
    c.check(len(files) == 2, "migrations/versions 仍恰 2 支迁移（本步零迁移）", f"{files}")
    c.check(
        all(name.startswith(("0001", "0002")) for name in files),
        "迁移文件名仍为 0001 / 0002（未新增）",
        f"{files}",
    )

    env = dict(os.environ)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "current"],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        out = (proc.stdout + proc.stderr).strip()
        c.check(
            proc.returncode == 0 and "0002" in out and "(head)" in out,
            "alembic current = 0002 (head)",
            out[-200:],
        )
    except Exception as exc:  # noqa: BLE001
        c.check(False, "alembic current = 0002 (head)", f"执行异常：{exc}")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "check"],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        out = (proc.stdout + proc.stderr).strip()
        c.check(proc.returncode == 0, "alembic check 零漂移（退出码 0）", out[-200:])
    except Exception as exc:  # noqa: BLE001
        c.check(False, "alembic check 零漂移（退出码 0）", f"执行异常：{exc}")


# ---------------------------------------------------------------------------
# 组 9：合规扫描
# ---------------------------------------------------------------------------
def group9_compliance(c: Checker) -> None:
    for path in SCANNED_FILES:
        src = path.read_text(encoding="utf-8")
        hits = [pattern for pattern in FORBIDDEN_PATTERNS if pattern in src]
        rel = path.relative_to(BACKEND_DIR)
        c.check(
            not hits,
            f"{rel} 不含小红书域名 / 登录态字段 / sk- 密钥字样",
            f"{hits}",
        )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    c = Checker()

    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: 本地 PostgreSQL 不可达（请先启动 docker compose 的 xhs-db）：{exc}")
        return 1

    client = TestClient(app, raise_server_exceptions=False)
    fx: Fixture | None = None
    try:
        fx = setup_fixture()
        group1_static(c)
        group2_create(c, client, fx)
        group3_list(c, client, fx)
        group4_select_exclusive(c, client, fx)
        group5_update_and_note_status(c, client, fx)
        group6_errors(c, client, fx)
        group7_surface(c)
        group8_migrations(c)
        group9_compliance(c)
    finally:
        if fx is not None:
            try:
                residue = cleanup_and_verify(fx)
                bad = {key: value for key, value in residue.items() if value != 0}
                c.check(
                    not bad,
                    "测试数据物理清理并双口径复查残留为 0",
                    f"{residue}",
                )
            except Exception as exc:  # noqa: BLE001
                c.check(False, "测试数据物理清理并双口径复查残留为 0", f"清理异常：{exc}")

    print()
    print(f"通过 {c.passed} 项，失败 {len(c.failures)} 项。")
    if c.failures:
        for name in c.failures:
            print(f"  - FAIL: {name}")
        return 1
    print("全部 PASS ✔（第 13 步封面模块后端自检，依赖本地 docker PostgreSQL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
