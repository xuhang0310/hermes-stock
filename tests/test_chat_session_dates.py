"""Conversation timestamp and recency regression tests, using only in-memory SQLite."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.ai.ai_api import _chat_messages_out, agent_session_detail, agent_sessions
from backend.app.auth.auth import AuthContext
from backend.app.core.database import Base
from backend.app.core.models import AiChatMessage, AiChatSession, User


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def actor(db):
    user = User(username="chat-owner", display_name="Owner", role="researcher", password_hash="unused")
    db.add(user)
    db.flush()
    return AuthContext(user=user, permissions={"agent:use"})


def add_session(db, user_id, *, title="Conversation", at=None, status="active", source_type="web"):
    at = at or datetime(2026, 10, 28, 15, 59)
    row = AiChatSession(
        user_id=user_id,
        title=title,
        status=status,
        source_type=source_type,
        chat_mode="research",
        created_at=at,
        updated_at=at,
    )
    db.add(row)
    db.flush()
    return row


def add_message(db, session_id, at):
    row = AiChatMessage(session_id=session_id, role="user", content="Hello", created_at=at)
    db.add(row)
    db.flush()
    return row


def list_sessions(db, actor):
    return agent_sessions(chat_mode=None, digital_human_id=None, db=db, context=actor)


def test_message_timestamps_are_explicit_utc_without_changing_the_instant():
    naive = AiChatMessage(id=1, role="user", content="First", created_at=datetime(2026, 10, 28, 16))
    aware = AiChatMessage(
        id=2,
        role="user",
        content="Second",
        created_at=datetime(2026, 10, 29, 0, 1, tzinfo=timezone(timedelta(hours=8))),
    )

    payload = _chat_messages_out([naive, aware])

    assert payload[0]["created_at"] == "2026-10-28T16:00:00+00:00"
    assert payload[1]["created_at"] == "2026-10-28T16:01:00+00:00"


def test_recent_message_promotes_old_session_before_fifty_row_limit(db, actor):
    resumed = add_session(db, actor.user.id, title="Older resumed conversation", at=datetime(2026, 1, 1))
    for index in range(55):
        add_session(db, actor.user.id, title=f"Other {index}", at=datetime(2026, 10, 1) + timedelta(days=index % 20))
    add_message(db, resumed.id, datetime(2026, 10, 29, 1, 15))
    db.commit()

    rows = list_sessions(db, actor)

    assert len(rows) == 50
    assert rows[0]["id"] == resumed.id
    assert rows[0]["updated_at"] == "2026-10-29T01:15:00+00:00"
    assert rows[0]["created_at"] == "2026-01-01T00:00:00+00:00"


def test_equal_activity_is_ordered_by_descending_session_id(db, actor):
    first = add_session(db, actor.user.id)
    second = add_session(db, actor.user.id)
    at = datetime(2026, 10, 29, 2)
    add_message(db, first.id, at)
    add_message(db, second.id, at)
    db.commit()

    assert [row["id"] for row in list_sessions(db, actor)] == [second.id, first.id]


def test_new_activity_never_exposes_other_users_or_deleted_sessions(db, actor):
    other = User(username="other-owner", display_name="Other", role="researcher", password_hash="unused")
    db.add(other)
    db.flush()
    visible = add_session(db, actor.user.id)
    hidden = [
        add_session(db, other.id),
        add_session(db, actor.user.id, status="deleted"),
        add_session(db, actor.user.id, source_type="private-external"),
    ]
    for row in hidden:
        add_message(db, row.id, datetime(2026, 10, 30))
    db.commit()

    assert [row["id"] for row in list_sessions(db, actor)] == [visible.id]


def test_detail_and_list_share_activity_time_and_messages_have_explicit_timezone(db, actor):
    row = add_session(db, actor.user.id)
    add_message(db, row.id, datetime(2026, 10, 28, 16, 1))
    db.commit()

    listed = list_sessions(db, actor)[0]
    detail = agent_session_detail(row.id, db=db, context=actor)

    assert listed["created_at"] == detail["created_at"] == "2026-10-28T15:59:00+00:00"
    assert listed["updated_at"] == detail["updated_at"] == "2026-10-28T16:01:00+00:00"
    assert detail["messages"][0]["created_at"] == "2026-10-28T16:01:00+00:00"


def test_empty_conversation_keeps_its_original_timestamp(db, actor):
    row = add_session(db, actor.user.id)
    db.commit()

    listed = list_sessions(db, actor)[0]

    assert listed["id"] == row.id
    assert listed["created_at"] == listed["updated_at"] == "2026-10-28T15:59:00+00:00"
