"""第 14 步 · 创作台后端自检（**依赖本地 docker PostgreSQL**）。

覆盖任务书要求的全部后端断言，分 10 组：

1. 编辑字段：PATCH（不带 status）把 ``title / body / tags`` **逐列**落库；
   ``title`` 显式 null / 超 100 字、``tags`` 非数组、``xhs_note_id`` 超 64 字、
   ``status`` 显式 null → 422；空 payload / 编辑时携带发布字段 → 400；
2. 非法流转（draft 状态）：``draft→published`` 跳级、``draft→archived``、``draft→draft``
   一律 400，且**无任何副作用**（状态 / 发布字段 / 选题均不变）；
3. 合法链：``draft→ready→published（回填）→ready（撤销）→draft`` 全程正确；
   ``ready→ready``、``published→published``、``published→draft`` 均 400；
4. 发布细节：``published_at`` 由服务端补、链接可留空（留空场景单测）；
   标记发布时来源选题 ``writing→published``（**单事务**结果断言）；
5. 撤回细节：``published→ready`` 清空三个发布字段、选题回 ``writing``；
   状态流转请求不得混合编辑字段 / 非发布流转不得携带发布字段 / ``archived`` 一律 400；
6. 404 与统一错误体：不存在的 note（编辑与流转两种请求）→ 404 ``not_found``；
   错误体结构 = ``{status, code, message, details}``；
7. 接口面：OpenAPI 中 ``/api/v1/notes/{note_id}`` 恰为 GET + PATCH、**无 PUT / DELETE**、
   ``/generate`` 与 ``/runs`` 静态段仍在册、``NoteUpdateRequest`` 已生成、``NoteOut`` 无
   ``llm_raw`` 等敏感字段；
8. 零迁移：``migrations/versions`` 仍恰 2 支、``alembic current`` = ``0002 (head)``、
   ``alembic check`` 退出码 0；
9. 合规扫描：本步触碰的后端文件不含小红书域名 / 登录态字段 / ``sk-`` 密钥；
   发布页外链（``creator.xiaohongshu.com``）在**全前端**恰出现 1 次且只在笔记相关文件；
10. 清理复查：测试数据（前缀 ``[SELFTEST]创作台``）**按 id 删 + 前缀兜底扫**，
    双口径复查残留为 0。

HTTP 断言走**进程内** ``TestClient``（不需要 uvicorn）；数据复核走 SQLAlchemy 会话。

用法（在**项目根目录**下）：

    backend\\.venv\\Scripts\\python.exe scripts\\selftest_studio.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import os
import subprocess
import sys
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
FRONTEND_SRC = ROOT_DIR / "frontend" / "src"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.enums import (  # noqa: E402
    AccountStatus,
    ComplianceStatus,
    NoteStatus,
    PersonaStatus,
    TopicStatus,
)
from app.models.note import Note  # noqa: E402
from app.models.persona import Persona  # noqa: E402
from app.models.topic import Topic  # noqa: E402

#: 本脚本全部测试数据统一前缀（清理与复查的双口径都靠它兜底）
PREFIX = "[SELFTEST]创作台"

VERSIONS_DIR = BACKEND_DIR / "migrations" / "versions"

#: 本步触碰过的后端文件（合规扫描对象）
SCANNED_FILES = (
    BACKEND_DIR / "app" / "schemas" / "note.py",
    BACKEND_DIR / "app" / "crud" / "note.py",
    BACKEND_DIR / "app" / "api" / "routes" / "notes.py",
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

#: 后端不存在的假链接（测试数据刻意不用真实平台域名）
FAKE_URL = "https://example.com/explore/abc123"

#: ``notes`` 逐列核对用的列清单
NOTE_COLUMNS = (
    "id, account_id, persona_id, topic_id, title, body, tags, content_check, "
    "compliance_status, status, publish_url, xhs_note_id, published_at, version, llm_meta"
)


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
    topic_id: uuid.UUID
    note_id: uuid.UUID


# ---------------------------------------------------------------------------
# 夹具与清理
# ---------------------------------------------------------------------------
def setup_fixture() -> Fixture:
    """建 1 个测试账号 + 1 个人设 + 1 个选题（writing）+ 1 篇笔记（draft）。"""
    with SessionLocal() as db:
        account = Account(name=f"{PREFIX}账号", status=AccountStatus.ACTIVE)
        db.add(account)
        db.flush()
        persona = Persona(
            name=f"{PREFIX}人设", account_id=account.id, status=PersonaStatus.ACTIVE
        )
        db.add(persona)
        db.flush()
        # 笔记由创作 Agent 生成后，来源选题应处于 writing（第 12 步语义）
        topic = Topic(
            account_id=account.id,
            persona_id=persona.id,
            title=f"{PREFIX}选题",
            status=TopicStatus.WRITING,
        )
        db.add(topic)
        db.flush()
        note = Note(
            account_id=account.id,
            persona_id=persona.id,
            topic_id=topic.id,
            title=f"{PREFIX}笔记",
            body="初始正文",
            tags=["初始标签"],
            status=NoteStatus.DRAFT,
            compliance_status=ComplianceStatus.PENDING,
        )
        db.add(note)
        db.flush()
        fixture = Fixture(
            account_id=account.id,
            persona_id=persona.id,
            topic_id=topic.id,
            note_id=note.id,
        )
        db.commit()
    return fixture


def cleanup_and_verify(fx: Fixture) -> dict[str, int]:
    """物理清理 + 双口径复查（按 id 精确 + 按前缀全局兜底）。"""
    like = f"%{PREFIX}%"
    with SessionLocal() as db:
        # 1) 按 id 精确删（covers → notes → topics → personas → accounts）
        db.execute(text("DELETE FROM covers WHERE note_id = :n"), {"n": str(fx.note_id)})
        db.execute(text("DELETE FROM notes WHERE id = :n"), {"n": str(fx.note_id)})
        db.execute(text("DELETE FROM topics WHERE id = :t"), {"t": str(fx.topic_id)})
        db.execute(text("DELETE FROM personas WHERE id = :p"), {"p": str(fx.persona_id)})
        db.execute(text("DELETE FROM accounts WHERE id = :a"), {"a": str(fx.account_id)})
        # 2) 前缀兜底扫（覆盖 id 漂移、半途失败留下的孤儿）
        db.execute(
            text("DELETE FROM covers WHERE note_id IN (SELECT id FROM notes WHERE title LIKE :t)"),
            {"t": like},
        )
        db.execute(text("DELETE FROM notes WHERE title LIKE :t"), {"t": like})
        db.execute(text("DELETE FROM topics WHERE title LIKE :t"), {"t": like})
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
            "topics(by_id)": db.execute(
                text("SELECT count(*) FROM topics WHERE id = :t"), {"t": str(fx.topic_id)}
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
            "topics(prefix)": db.execute(
                text("SELECT count(*) FROM topics WHERE title LIKE :t"), {"t": like}
            ).scalar_one(),
            "personas(prefix)": db.execute(
                text("SELECT count(*) FROM personas WHERE name LIKE :t"), {"t": like}
            ).scalar_one(),
            "accounts(prefix)": db.execute(
                text("SELECT count(*) FROM accounts WHERE name LIKE :t"), {"t": like}
            ).scalar_one(),
        }


def _fetch_note(note_id: uuid.UUID) -> dict:
    """从数据库逐列读回一篇笔记（**绕过接口**，验证真实落库内容）。"""
    with SessionLocal() as db:
        row = db.execute(
            text(f"SELECT {NOTE_COLUMNS} FROM notes WHERE id = :n"),  # noqa: S608
            {"n": str(note_id)},
        ).one_or_none()
    if row is None:
        return {}
    keys = [item.strip() for item in NOTE_COLUMNS.split(",")]
    return dict(zip(keys, row, strict=True))


def _topic_status(topic_id: uuid.UUID) -> str:
    with SessionLocal() as db:
        return str(
            db.execute(
                text("SELECT status FROM topics WHERE id = :t"), {"t": str(topic_id)}
            ).scalar_one()
        )


def _patch(client: TestClient, note_id: uuid.UUID, payload: dict):
    return client.patch(f"/api/v1/notes/{note_id}", json=payload)


# ---------------------------------------------------------------------------
# 组 1：编辑字段逐列落库 + 入参边界（422 / 400）
# ---------------------------------------------------------------------------
def group1_edit(c: Checker, client: TestClient, fx: Fixture) -> None:
    edited = {
        "title": f"{PREFIX}新标题",
        "body": "编辑后的正文\n带换行的第二行",
        "tags": ["效率", "职场", "干货"],
    }
    r = _patch(client, fx.note_id, edited)
    c.check(r.status_code == 200, "PATCH 编辑 title/body/tags 返回 200", f"{r.status_code} {r.text[:200]}")
    data = r.json()
    c.check(data.get("title") == edited["title"], "响应 title 与请求一致")
    c.check(data.get("body") == edited["body"], "响应 body 与请求一致（含换行）")
    c.check(data.get("tags") == edited["tags"], "响应 tags 与请求一致")
    c.check(data.get("status") == "draft", "编辑**不**改状态（仍 draft）", f"{data.get('status')}")

    row = _fetch_note(fx.note_id)
    c.check(row.get("title") == edited["title"], "落库 title 逐字一致", f"{row.get('title')!r}")
    c.check(row.get("body") == edited["body"], "落库 body 逐字一致")
    c.check(row.get("tags") == edited["tags"], "落库 tags 逐项一致", f"{row.get('tags')}")
    c.check(row.get("status") == "draft", "落库 status 仍 draft（编辑不触碰状态机）")
    c.check(
        row.get("publish_url") is None and row.get("xhs_note_id") is None and row.get("published_at") is None,
        "编辑后三个发布字段仍为空",
    )
    c.check(_topic_status(fx.topic_id) == "writing", "编辑**不**联动选题（恒 writing）")

    r = client.get(f"/api/v1/notes/{fx.note_id}")
    c.check(
        r.status_code == 200 and r.json().get("title") == edited["title"],
        "GET 详情可回读到编辑后的标题（前端保存后回读路径）",
        f"{r.status_code}",
    )

    # ---- 422 边界 ----
    for bad_payload, label in (
        ({"title": None}, "title 显式 null → 422（非空列）"),
        ({"title": "超" * 101}, "title 超 100 字 → 422"),
        ({"tags": "not-a-list"}, "tags 非数组 → 422"),
        ({"xhs_note_id": "x" * 65}, "xhs_note_id 超 64 字 → 422"),
        ({"status": None}, "status 显式 null → 422（不流转禁携带）"),
    ):
        r = _patch(client, fx.note_id, bad_payload)
        c.check(r.status_code == 422, label, f"{r.status_code} {r.text[:150]}")

    # ---- 400 边界 ----
    r = _patch(client, fx.note_id, {})
    c.check(r.status_code == 400, "空 payload → 400（没有可更新的字段）", f"{r.status_code}")
    r = _patch(client, fx.note_id, {"publish_url": FAKE_URL})
    c.check(
        r.status_code == 400,
        "编辑请求携带发布字段 → 400（仅标记发布时可回填）",
        f"{r.status_code}",
    )

    row = _fetch_note(fx.note_id)
    c.check(
        row.get("title") == edited["title"] and row.get("status") == "draft",
        "全部 422/400 请求后数据未被改动",
    )


# ---------------------------------------------------------------------------
# 组 2：非法流转（draft 状态）与无副作用
# ---------------------------------------------------------------------------
def group2_invalid_transitions(c: Checker, client: TestClient, fx: Fixture) -> None:
    for payload, label in (
        ({"status": "published"}, "draft→published 跳级 → 400"),
        ({"status": "archived"}, "draft→archived → 400（本步不处理归档）"),
        ({"status": "draft"}, "draft→draft 无效流转 → 400"),
    ):
        r = _patch(client, fx.note_id, payload)
        c.check(r.status_code == 400, label, f"{r.status_code} {r.text[:150]}")
        c.check(
            r.json().get("code") == "bad_request",
            f"↳ 错误码为 bad_request（{label.split(' ')[0]}）",
            f"{r.json().get('code')}",
        )

    row = _fetch_note(fx.note_id)
    c.check(row.get("status") == "draft", "非法流转后 note 仍 draft（无副作用）")
    c.check(
        row.get("publish_url") is None and row.get("xhs_note_id") is None and row.get("published_at") is None,
        "非法流转后发布字段未被写入",
    )
    c.check(_topic_status(fx.topic_id) == "writing", "非法流转后选题仍 writing")

    detail = _patch(client, fx.note_id, {"status": "published"}).json().get("details") or {}
    c.check(
        isinstance(detail, dict) and "allowed" in detail,
        "非法流转错误体含 allowed 允许清单（前端可提示）",
        f"{detail}",
    )


# ---------------------------------------------------------------------------
# 组 3：合法链 draft→ready→published（回填）→ready（撤销）→draft
# ---------------------------------------------------------------------------
def group3_happy_chain(c: Checker, client: TestClient, fx: Fixture) -> None:
    # draft → ready
    r = _patch(client, fx.note_id, {"status": "ready"})
    c.check(r.status_code == 200 and r.json().get("status") == "ready", "draft→ready 返回 200 且状态为 ready", f"{r.status_code} {r.text[:150]}")
    c.check(_fetch_note(fx.note_id).get("status") == "ready", "落库 status = ready")
    c.check(_topic_status(fx.topic_id) == "writing", "draft→ready 不联动选题（恒 writing）")

    # ready → ready（重复流转）
    r = _patch(client, fx.note_id, {"status": "ready"})
    c.check(r.status_code == 400, "ready→ready 重复流转 → 400", f"{r.status_code}")

    # ready → published（回填链接与 id）
    r = _patch(
        client,
        fx.note_id,
        {"status": "published", "publish_url": FAKE_URL, "xhs_note_id": "abc123"},
    )
    c.check(r.status_code == 200 and r.json().get("status") == "published", "ready→published 返回 200 且状态为 published", f"{r.status_code} {r.text[:150]}")
    data = r.json()
    c.check(bool(data.get("published_at")), "响应 published_at 已被服务端补齐", f"{data.get('published_at')}")
    c.check(data.get("publish_url") == FAKE_URL, "响应回填 publish_url")
    c.check(data.get("xhs_note_id") == "abc123", "响应回填 xhs_note_id")

    row = _fetch_note(fx.note_id)
    c.check(row.get("status") == "published", "落库 status = published")
    c.check(row.get("published_at") is not None, "落库 published_at 非空（服务端当前时间）")
    c.check(row.get("publish_url") == FAKE_URL and row.get("xhs_note_id") == "abc123", "落库回填字段逐字一致")
    c.check(_topic_status(fx.topic_id) == "published", "标记发布联动选题 writing→published（单事务）", _topic_status(fx.topic_id))

    # published 状态下的非法流转
    r = _patch(client, fx.note_id, {"status": "published"})
    c.check(r.status_code == 400, "published→published 重复流转 → 400", f"{r.status_code}")
    r = _patch(client, fx.note_id, {"status": "draft"})
    c.check(r.status_code == 400, "published→draft 跳级 → 400（必须经 ready）", f"{r.status_code}")

    # published → ready（撤销发布）
    r = _patch(client, fx.note_id, {"status": "ready"})
    c.check(r.status_code == 200 and r.json().get("status") == "ready", "published→ready 撤销发布返回 200", f"{r.status_code} {r.text[:150]}")
    row = _fetch_note(fx.note_id)
    c.check(
        row.get("published_at") is None and row.get("publish_url") is None and row.get("xhs_note_id") is None,
        "撤销后三个发布字段全部清空",
        f"{row.get('published_at')!r} / {row.get('publish_url')!r} / {row.get('xhs_note_id')!r}",
    )
    c.check(_topic_status(fx.topic_id) == "writing", "撤销发布联动选题 published→writing", _topic_status(fx.topic_id))

    # ready → draft（退回修改）
    r = _patch(client, fx.note_id, {"status": "draft"})
    c.check(r.status_code == 200 and r.json().get("status") == "draft", "ready→draft 退回修改返回 200", f"{r.status_code}")
    c.check(_topic_status(fx.topic_id) == "writing", "退回修改不联动选题（恒 writing）")


# ---------------------------------------------------------------------------
# 组 4：发布细节（链接可留空）与再一轮完整往返
# ---------------------------------------------------------------------------
def group4_publish_optional(c: Checker, client: TestClient, fx: Fixture) -> None:
    r = _patch(client, fx.note_id, {"status": "ready"})
    c.check(r.status_code == 200, "（第 2 轮）draft→ready 返回 200", f"{r.status_code}")

    # 不带 publish_url / xhs_note_id 的发布（两字段均可留空）
    r = _patch(client, fx.note_id, {"status": "published"})
    c.check(r.status_code == 200, "（第 2 轮）published 且不回填链接返回 200", f"{r.status_code} {r.text[:150]}")
    data = r.json()
    c.check(data.get("publish_url") is None and data.get("xhs_note_id") is None, "留空时两字段为 null（允许）")
    c.check(bool(data.get("published_at")), "留空发布仍补 published_at")
    c.check(_topic_status(fx.topic_id) == "published", "第 2 轮发布同样联动选题")

    r = _patch(client, fx.note_id, {"status": "ready", "publish_url": FAKE_URL})
    c.check(r.status_code == 400, "撤销发布时携带发布字段 → 400（ready 流转禁带）", f"{r.status_code}")

    r = _patch(client, fx.note_id, {"status": "ready"})
    c.check(r.status_code == 200, "（第 2 轮）撤销发布返回 200", f"{r.status_code}")
    c.check(_topic_status(fx.topic_id) == "writing", "第 2 轮撤销选题回 writing")


# ---------------------------------------------------------------------------
# 组 5：混合请求 / 兜底语义
# ---------------------------------------------------------------------------
def group5_mixed(c: Checker, client: TestClient, fx: Fixture) -> None:
    # 当前状态：ready（组 4 收尾）
    r = _patch(client, fx.note_id, {"status": "draft", "body": "想混着改"})
    c.check(r.status_code == 400, "状态流转 + 编辑字段混合 → 400", f"{r.status_code} {r.text[:150]}")
    r = _patch(client, fx.note_id, {"status": "draft", "title": "混合标题"})
    c.check(r.status_code == 400, "状态流转 + title 混合 → 400", f"{r.status_code}")

    r = _patch(client, fx.note_id, {"status": "archived"})
    c.check(r.status_code == 400, "ready→archived → 400（归档留后续步骤）", f"{r.status_code}")

    # 收尾：ready → draft，把状态带回 draft（验证「全程 topic 恒 writing」最后一段）
    r = _patch(client, fx.note_id, {"status": "draft"})
    c.check(r.status_code == 200, "收尾 ready→draft 返回 200", f"{r.status_code}")
    row = _fetch_note(fx.note_id)
    c.check(row.get("status") == "draft", "收尾后 note = draft")
    c.check(_topic_status(fx.topic_id) == "writing", "全程 draft/ready 阶段选题恒为 writing")


# ---------------------------------------------------------------------------
# 组 6：404 与统一错误体
# ---------------------------------------------------------------------------
def group6_errors(c: Checker, client: TestClient) -> None:
    random_id = uuid.uuid4()
    r = _patch(client, random_id, {"title": "不存在的笔记"})
    c.check(r.status_code == 404, "PATCH 编辑不存在的 note → 404", f"{r.status_code}")
    c.check(r.json().get("code") == "not_found", "404 错误码 = not_found", f"{r.json().get('code')}")

    r = _patch(client, random_id, {"status": "ready"})
    c.check(r.status_code == 404, "PATCH 流转不存在的 note → 404（先于状态机判断）", f"{r.status_code}")

    r = client.get(f"/api/v1/notes/{random_id}")
    c.check(r.status_code == 404, "GET 不存在的 note → 404", f"{r.status_code}")

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
    c.check(
        sorted(paths.get("/api/v1/notes/{note_id}", {})) == ["get", "patch"],
        "/api/v1/notes/{note_id} 恰为 GET + PATCH",
        f"{sorted(paths.get('/api/v1/notes/{note_id}', {}))}",
    )
    c.check(
        sorted(paths.get("/api/v1/notes", {})) == ["get"],
        "/api/v1/notes 集合仍仅 GET（无 POST，生成走 /generate）",
        f"{sorted(paths.get('/api/v1/notes', {}))}",
    )
    c.check(
        "/api/v1/notes/generate" in paths and "/api/v1/notes/runs" in paths,
        "静态段 /generate、/runs 仍在册（注册顺序未破坏）",
    )
    bad = [
        path
        for path in paths
        if "/notes" in path and any(method in paths[path] for method in ("put", "delete"))
    ]
    c.check(not bad, "notes 路由中不存在 PUT / DELETE（本步不提供）", f"{bad}")

    schemas = app.openapi()["components"]["schemas"]
    c.check("NoteUpdateRequest" in schemas, "NoteUpdateRequest 已生成 OpenAPI 组件")
    req_props = set(schemas["NoteUpdateRequest"]["properties"])
    c.check(
        req_props == {"title", "body", "tags", "status", "publish_url", "xhs_note_id"},
        "NoteUpdateRequest 属性恰为 6 个约定字段",
        f"{sorted(req_props)}",
    )
    note_out_props = set(schemas["NoteOut"]["properties"])
    c.check("llm_raw" not in note_out_props, "NoteOut 仍不含 llm_raw（接口不回传）")
    sensitive = {"token", "cookie", "secret", "password", "llm_raw", "api_key"}
    c.check(
        not any(any(flag in name.lower() for flag in sensitive) for name in note_out_props),
        "NoteOut 不含 token / cookie / secret / password / llm_raw 等敏感字段",
        f"{sorted(note_out_props)}",
    )
    list_item_props = set(schemas["NoteListItem"]["properties"])
    c.check(
        "publish_url" in list_item_props and "llm_raw" not in list_item_props,
        "NoteListItem 增补 publish_url（列表展示发布外链所需）且仍无 llm_raw",
        f"{sorted(list_item_props)}",
    )


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

    # 「前往小红书发布页」外链常量：只允许出现在前端，且全前端恰 1 处
    frontend_hits: dict[str, int] = {}
    for pattern in ("*.ts", "*.tsx", "*.css", "*.json"):
        for path in FRONTEND_SRC.rglob(pattern):
            if any(part in {"node_modules", ".next", ".next.old"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "xiaohongshu" in text:
                frontend_hits[path.relative_to(ROOT_DIR).as_posix()] = text.count("xiaohongshu")
    c.check(
        sum(frontend_hits.values()) == 1
        and all("/notes" in key for key in frontend_hits),
        "发布页外链常量在**前端**恰出现 1 次且位于笔记相关文件（后端零出现）",
        f"{frontend_hits}",
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
        group1_edit(c, client, fx)
        group2_invalid_transitions(c, client, fx)
        group3_happy_chain(c, client, fx)
        group4_publish_optional(c, client, fx)
        group5_mixed(c, client, fx)
        group6_errors(c, client)
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
    print("全部 PASS ✔（第 14 步创作台后端自检，依赖本地 docker PostgreSQL）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
