from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.notify import notification_service


class InboxNotificationTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        db_patch = patch.object(notification_service, "SessionLocal", sessionmaker(bind=self.engine))
        db_patch.start()
        self.addCleanup(db_patch.stop)

    def test_times_are_stored_in_utc_and_serialized_with_zone(self) -> None:
        created = notification_service.create_notification(5, "每日选股", content="正文")

        self.assertTrue(created["created_at"].endswith("+00:00"))
        moment = datetime.fromisoformat(created["created_at"])
        self.assertLess(abs(moment - datetime.now(timezone.utc)), timedelta(minutes=1))

        self.assertTrue(notification_service.mark_read(5, created["id"]))
        items, unread = notification_service.list_notifications(5)
        self.assertEqual(unread, 0)
        self.assertTrue(items[0]["read_at"].endswith("+00:00"))
        self.assertEqual(notification_service.list_notifications(6), ([], 0))


if __name__ == "__main__":
    unittest.main()
