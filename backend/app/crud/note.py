"""notes 的 CRUD（第 12 步 · 创作 Agent；第 14 步 · 创作台增补人工编辑与状态流转）。

职责：

- 列表过滤：``status`` / ``account_id`` / ``persona_id`` / ``topic_id`` / 标题模糊 ``q``；
- 关联展示：LEFT JOIN ``personas`` / ``accounts`` 取名（**不新增数据库列**）；
- **单事务创作落库**：插入一条 ``notes`` 的同时把来源 ``topics`` 由 ``approved`` 置为
  ``writing``；任一失败整体回滚，不留半成品；
- **人工编辑与状态流转**（第 14 步）：``update_content``（仅文字字段）、
  ``set_status_draft_ready``（draft↔ready，不联动选题）、``mark_published``（单事务写发布字段
  + 选题 writing→published）、``revert_to_ready``（单事务清空发布字段 + 选题 published→writing）；
- 遵循项目约定：**不提供物理删除**（列表/归档语义由后续步骤接管）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.crud.base import CRUDBase
from app.models.account import Account
from app.models.enums import NoteStatus, TopicStatus
from app.models.note import Note
from app.models.persona import Persona
from app.models.topic import Topic

#: 允许的排序字段（notes 无 generated_at 列，故用 created_at / updated_at）
SORT_CREATED_AT = "created_at"
SORT_UPDATED_AT = "updated_at"


class CRUDNote(CRUDBase[Note]):
    """笔记 CRUD。"""

    archived_status = NoteStatus.ARCHIVED

    def __init__(self) -> None:
        super().__init__(Note)

    # ------------------------------------------------------------------ 过滤
    def build_criteria(
        self,
        *,
        status: NoteStatus | None = None,
        account_id: uuid.UUID | None = None,
        persona_id: uuid.UUID | None = None,
        topic_id: uuid.UUID | None = None,
        q: str | None = None,
    ) -> Sequence[Any]:
        """把查询参数翻译成 SQLAlchemy 过滤条件。"""
        criteria: list[Any] = []
        if status is not None:
            criteria.append(Note.status == status)
        if account_id is not None:
            criteria.append(Note.account_id == account_id)
        if persona_id is not None:
            criteria.append(Note.persona_id == persona_id)
        if topic_id is not None:
            criteria.append(Note.topic_id == topic_id)
        if q:
            criteria.append(Note.title.ilike(f"%{q.strip()}%"))
        return tuple(criteria)

    def get_multi_with_names(
        self,
        db: Session,
        *,
        criteria: Sequence[Any] = (),
        offset: int = 0,
        limit: int = 20,
        sort: str = SORT_CREATED_AT,
        order: str = "desc",
    ) -> list[tuple[Note, str | None, str | None]]:
        """分页查询并带出人设名 / 账号名（LEFT JOIN，可为 None）。"""
        stmt: Select = (
            select(Note, Persona.name, Account.name)
            .outerjoin(Persona, Note.persona_id == Persona.id)
            .outerjoin(Account, Note.account_id == Account.id)
        )
        stmt = self._apply_criteria(stmt, criteria)
        column = Note.updated_at if sort == SORT_UPDATED_AT else Note.created_at
        direction = column.asc().nulls_last() if order == "asc" else column.desc().nulls_last()
        stmt = stmt.order_by(direction).offset(offset).limit(limit)
        return [(row[0], row[1], row[2]) for row in db.execute(stmt).all()]

    # ------------------------------------------------------------------ 写入
    def create_with_topic_transition(
        self,
        db: Session,
        data: dict[str, Any],
        *,
        topic_id: uuid.UUID,
        from_status: TopicStatus = TopicStatus.APPROVED,
        to_status: TopicStatus = TopicStatus.WRITING,
    ) -> Note:
        """**单事务**落库：插入一条 ``notes`` + 把来源选题由 ``approved`` 置为 ``writing``。

        全流程共用一个事务：任一步失败（含外键 / 长度等数据库错误）整体回滚，
        **不留半成品**，选题状态也不会被改成 ``writing``。

        Raises:
            ValueError: 选题不存在，或状态不是 ``from_status``（由调用方翻译成业务错误）。
        """
        topic = db.get(Topic, topic_id)
        if topic is None:
            raise ValueError(f"选题不存在：{topic_id}")
        if topic.status != from_status:
            raise ValueError(
                f"选题状态为 {topic.status.value}，只有 {from_status.value} 才能创作"
            )

        note = Note(**data)
        try:
            db.add(note)
            db.flush()  # 通过 RETURNING 取回 gen_random_uuid() 主键
            topic.status = to_status
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(note)
        return note

    # ------------------------------------------------------ 人工编辑（第 14 步）
    def update_content(
        self, db: Session, note_id: uuid.UUID, data: dict[str, Any]
    ) -> Note | None:
        """只更新 ``title / body / tags``（**不**改状态、**不**联动选题）；commit + refresh。

        返回 ``None`` 表示笔记不存在（由路由翻译成 404）。
        """
        note = db.get(Note, note_id)
        if note is None:
            return None
        try:
            for key in ("title", "body", "tags"):
                if key in data:
                    setattr(note, key, data[key])
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(note)
        return note

    def set_status_draft_ready(
        self, db: Session, note_id: uuid.UUID, target: NoteStatus
    ) -> Note | None:
        """在 ``draft ↔ ready`` 之间流转（**不**联动选题）。

        Raises:
            ValueError: ``target`` 不在 draft/ready 内，或当前状态不是对应前置
                （draft→ready 需当前 draft；ready→draft 需当前 ready）。
        """
        if target not in (NoteStatus.DRAFT, NoteStatus.READY):
            raise ValueError(f"只支持 draft / ready 之间的流转，收到 {target.value}")
        note = db.get(Note, note_id)
        if note is None:
            return None
        expected = NoteStatus.DRAFT if target == NoteStatus.READY else NoteStatus.READY
        if note.status != expected:
            raise ValueError(
                f"当前状态为 {note.status.value}，不能流转到 {target.value}"
                f"（该流转须先处于 {expected.value}）"
            )
        try:
            note.status = target
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(note)
        return note

    def mark_published(
        self,
        db: Session,
        note_id: uuid.UUID,
        *,
        publish_url: str | None,
        xhs_note_id: str | None,
        published_at: datetime | None = None,
    ) -> Note | None:
        """**单事务**标记发布：写发布字段 + 来源选题 ``writing → published``。

        - 前置状态必须为 ``ready``（否则 ``ValueError``，不写任何字段）；
        - ``published_at`` 缺省取服务端当前时间（UTC）；
        - 选题联动为**尽力而为**：无关联选题、或选题状态不是 ``writing`` 时跳过，
          不阻断笔记的发布标记（笔记才是发布主体）。

        返回 ``None`` 表示笔记不存在（由路由翻译成 404）。
        """
        note = db.get(Note, note_id)
        if note is None:
            return None
        if note.status != NoteStatus.READY:
            raise ValueError(f"当前状态为 {note.status.value}，只有 ready 才能标记发布")
        try:
            note.status = NoteStatus.PUBLISHED
            note.published_at = published_at or datetime.now(timezone.utc)
            note.publish_url = publish_url
            note.xhs_note_id = xhs_note_id
            if note.topic_id is not None:
                topic = db.get(Topic, note.topic_id)
                if topic is not None and topic.status == TopicStatus.WRITING:
                    topic.status = TopicStatus.PUBLISHED
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(note)
        return note

    def revert_to_ready(self, db: Session, note_id: uuid.UUID) -> Note | None:
        """**单事务**撤销发布：清空三个发布字段、状态回 ``ready``，选题 ``published → writing``。

        前置状态必须为 ``published``（否则 ``ValueError``，不写任何字段）。
        返回 ``None`` 表示笔记不存在（由路由翻译成 404）。
        """
        note = db.get(Note, note_id)
        if note is None:
            return None
        if note.status != NoteStatus.PUBLISHED:
            raise ValueError(f"当前状态为 {note.status.value}，只有 published 才能撤销发布")
        try:
            note.status = NoteStatus.READY
            note.published_at = None
            note.publish_url = None
            note.xhs_note_id = None
            if note.topic_id is not None:
                topic = db.get(Topic, note.topic_id)
                if topic is not None and topic.status == TopicStatus.PUBLISHED:
                    topic.status = TopicStatus.WRITING
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(note)
        return note

    # -------------------------------------------- 多版本批次（第 23 步 · V2 增强（二））
    def create_batch_with_topic_transition(
        self,
        db: Session,
        rows: list[dict[str, Any]],
        *,
        topic_id: uuid.UUID,
        from_status: TopicStatus = TopicStatus.APPROVED,
        to_status: TopicStatus = TopicStatus.WRITING,
    ) -> list[Note]:
        """**单事务**批量落库：一次插入 N 条 ``notes`` + 把来源选题由 ``approved`` 置为
        ``writing``（**只过渡一次**）。

        - ``rows`` 为空（全部版本被拦截 / 不合格）→ **不动选题、不写任何行**，返回 ``[]``
          —— 「零合格版本」不应把选题推进到 ``writing``；
        - 任一步失败（含外键 / 长度等数据库错误）整体回滚，不留半成品、选题状态不变。

        Raises:
            ValueError: 选题不存在，或状态不是 ``from_status``（由调用方翻译成业务错误 /
                AgentNonRetryableError）。
        """
        if not rows:
            return []
        topic = db.get(Topic, topic_id)
        if topic is None:
            raise ValueError(f"选题不存在：{topic_id}")
        if topic.status != from_status:
            raise ValueError(
                f"选题状态为 {topic.status.value}，只有 {from_status.value} 才能创作"
            )

        notes = [Note(**data) for data in rows]
        try:
            db.add_all(notes)
            db.flush()  # 通过 RETURNING 取回 gen_random_uuid() 主键
            topic.status = to_status
            db.commit()
        except Exception:
            db.rollback()
            raise
        for note in notes:
            db.refresh(note)
        return notes

    def list_variant_notes(self, db: Session, topic_id: uuid.UUID) -> list[Note]:
        """取某选题下全部**多版本批次**笔记（``llm_meta`` 含 ``variant`` 键）。

        排序：``created_at asc, id asc``（确定性；批次内再按 ``variant.index`` 在
        service 层排）。不排除任何状态（采用后可对照查看；接口不提供物理删除）。
        """
        stmt = (
            select(Note)
            .where(Note.topic_id == topic_id, Note.llm_meta.has_key("variant"))
            .order_by(Note.created_at.asc(), Note.id.asc())
        )
        return list(db.execute(stmt).scalars().all())

    def adopt_variant(
        self, db: Session, note_id: uuid.UUID
    ) -> tuple[Note, list[Note]] | None:
        """**单事务互斥采用**：目标版本置 ``adopted=True``，同批次其余置 ``False``。

        与第 13 步封面 ``select_one`` 同一范式（一个批次至多一个采用版；可反复切换）。
        ``adopted`` 只写 ``llm_meta.variant``（零迁移）；**不改** 笔记状态（仍 draft）
        与选题状态（采用只是「选哪版继续编辑」的标记）。

        Returns:
            ``(target, batch_rows)``；``None`` 表示笔记不存在（路由翻译成 404）。

        Raises:
            ValueError: 该笔记不是多版本批次成员（无 ``variant.group_id``）。
        """
        note = db.get(Note, note_id)
        if note is None:
            return None
        meta = note.llm_meta if isinstance(note.llm_meta, dict) else {}
        variant = meta.get("variant") if isinstance(meta.get("variant"), dict) else None
        group_id = str((variant or {}).get("group_id") or "")
        if not group_id:
            raise ValueError("该笔记不属于多版本批次，不能采用")

        stmt = (
            select(Note)
            .where(Note.llm_meta["variant"]["group_id"].astext == group_id)
            .order_by(Note.created_at.asc(), Note.id.asc())
        )
        rows = list(db.execute(stmt).scalars().all())
        if not rows:  # 防御：查询未命中时至少包含目标本身
            rows = [note]

        try:
            for row in rows:
                row_meta = row.llm_meta if isinstance(row.llm_meta, dict) else None
                row_variant = (
                    row_meta.get("variant")
                    if isinstance(row_meta, dict) and isinstance(row_meta.get("variant"), dict)
                    else None
                )
                if row_variant is None:
                    continue
                row_variant["adopted"] = str(row.id) == str(note.id)
                flag_modified(row, "llm_meta")
            db.commit()
        except Exception:
            db.rollback()
            raise
        for row in rows:
            db.refresh(row)
        return note, rows


crud_note = CRUDNote()

__all__ = ["CRUDNote", "crud_note", "SORT_CREATED_AT", "SORT_UPDATED_AT"]
