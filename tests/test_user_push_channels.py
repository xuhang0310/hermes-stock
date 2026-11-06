"""多用户推送：按所有者走自己的通道，没配就只进站内通知，绝不发到系统群。内存 SQLite，飞书打桩，不连 MySQL。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import ROLE_PERMISSIONS, AuthContext
from backend.app.core.models import Base, Notification, NotifyChannel, User
from backend.app.feishu import feishu
from backend.app.feishu.feishu import SCHEDULE_CHANNEL_KEY, SKIPPED, encrypt_value, owner_channel_key, send_feishu, user_channel_key
from backend.app.scheduler import rhythm_api

ADMIN, SECOND_ADMIN, ZHANG, WANG = 1, 2, 8, 9
SYSTEM_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/system"
ZHANG_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/zhang"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.posted: list[str] = []
        self.inbox: list[tuple] = []
        patches = [
            patch.object(feishu, "_post_pages", side_effect=self._fake_post),
            patch("backend.app.notify.notification_service.create_notification", side_effect=lambda user_id, title, **kwargs: self.inbox.append((user_id, title, kwargs))),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with self.Session() as db:
            for user_id, role in ((ADMIN, "super_admin"), (SECOND_ADMIN, "super_admin"), (ZHANG, "researcher"), (WANG, "researcher")):
                db.add(User(id=user_id, username=f"u{user_id}", display_name=f"u{user_id}", password_hash="x", role=role, is_enabled=True))
            db.add(self._channel(SCHEDULE_CHANNEL_KEY, SYSTEM_URL))
            db.commit()

    def _channel(self, key: str, url: str | None, status: str = "enabled") -> NotifyChannel:
        return NotifyChannel(channel_key=key, name=key, purpose="schedule", status=status, webhook_url_cipher=encrypt_value(url) if url else None)

    def _fake_post(self, webhook_url: str, payloads: list) -> tuple[int, int, str]:
        self.posted.append(webhook_url)
        return len(payloads), 200, ""

    def _zhang_channel(self, status: str = "enabled", url: str | None = ZHANG_URL) -> None:
        with self.Session() as db:
            db.add(self._channel(user_channel_key(ZHANG), url, status))
            db.commit()

    def _send(self, owner: int | None, **kwargs) -> Notification:
        with self.Session() as db:
            return send_feishu(db, SCHEDULE_CHANNEL_KEY, "标题", "正文，有持仓", owner_user_id=owner, **kwargs)


class RoutingTests(_Case):
    def test_the_earliest_super_admin_keeps_the_system_group(self) -> None:
        self.assertEqual(self._send(ADMIN).status, "sent")
        self.assertEqual(self.posted, [SYSTEM_URL])

    def test_a_system_alert_without_an_owner_goes_to_the_system_group(self) -> None:
        self.assertEqual(self._send(None).status, "sent")
        self.assertEqual(self.posted, [SYSTEM_URL])

    def test_another_user_goes_to_their_own_group(self) -> None:
        self._zhang_channel()
        notification = self._send(ZHANG)
        self.assertEqual(notification.status, "sent")
        self.assertEqual(notification.channel, f"feishu:{user_channel_key(ZHANG)}")
        self.assertEqual(self.posted, [ZHANG_URL])

    def test_the_job_channel_value_cannot_send_a_user_to_the_system_group(self) -> None:
        self._zhang_channel()
        with self.Session() as db:
            send_feishu(db, SCHEDULE_CHANNEL_KEY, "t", "c", owner_user_id=ZHANG)
        self.assertNotIn(SYSTEM_URL, self.posted)

    def test_no_channel_never_falls_back_to_the_system_group(self) -> None:
        notification = self._send(WANG)
        self.assertEqual(notification.status, SKIPPED)
        self.assertEqual(self.posted, [])

    def test_no_channel_leaves_an_inbox_notice_for_that_user(self) -> None:
        self._send(WANG)
        self.assertEqual([(user, title) for user, title, _ in self.inbox], [(WANG, "标题")])
        self.assertEqual(self.inbox[0][2]["category"], "push")

    def test_the_same_message_only_notifies_once(self) -> None:
        self._send(WANG, event_id="e1")
        self._send(WANG, event_id="e1")
        self._send(WANG, report_id="r1")
        self._send(WANG, report_id="r1")
        self.assertEqual(len(self.inbox), 2)

    def test_a_disabled_channel_counts_as_not_configured(self) -> None:
        self._zhang_channel(status="disabled")
        self.assertEqual(self._send(ZHANG).status, SKIPPED)
        self.assertEqual(self.posted, [])

    def test_a_channel_without_a_webhook_counts_as_not_configured(self) -> None:
        self._zhang_channel(url=None)
        with self.Session() as db:
            self.assertIsNone(owner_channel_key(db, ZHANG))

    def test_another_super_admin_does_not_share_the_system_group(self) -> None:
        self.assertEqual(self._send(SECOND_ADMIN).status, SKIPPED)
        self.assertEqual(self.posted, [])

    def test_a_broken_inbox_does_not_stop_the_job(self) -> None:
        with patch("backend.app.notify.notification_service.create_notification", side_effect=RuntimeError("boom")):
            self.assertEqual(self._send(WANG).status, SKIPPED)

    def test_a_user_who_configures_later_gets_the_retry(self) -> None:
        self.assertEqual(self._send(ZHANG, event_id="e2").status, SKIPPED)
        self._zhang_channel()
        self.assertEqual(self._send(ZHANG, event_id="e2").status, "sent")


class MyChannelApiTests(_Case):
    def _context(self, user_id: int, role: str = "researcher") -> AuthContext:
        with self.Session() as db:
            return AuthContext(user=db.get(User, user_id), permissions=set(ROLE_PERMISSIONS[role]))

    def _save(self, user_id: int, **fields) -> object:
        body = {"name": "我的群", "status": "enabled", **fields}
        with self.Session() as db:
            return rhythm_api.save_my_notify_channel(rhythm_api.NotifyChannelIn(**body), db=db, context=self._context(user_id))

    def _mine(self, user_id: int) -> dict:
        with self.Session() as db:
            return rhythm_api.my_notify_channel(db=db, context=self._context(user_id))["data"]["channel"]

    def test_saving_creates_a_channel_that_only_the_owner_sees(self) -> None:
        self._save(ZHANG, webhook_url=ZHANG_URL, secret="s")
        mine = self._mine(ZHANG)
        self.assertEqual(mine["channel_key"], user_channel_key(ZHANG))
        self.assertIsNone(self._mine(WANG))

    def test_the_response_never_contains_the_webhook_or_secret(self) -> None:
        response = self._save(ZHANG, webhook_url=ZHANG_URL, secret="topsecret")
        text = str(response) + str(self._mine(ZHANG))
        self.assertNotIn("hook/zhang", text)
        self.assertNotIn("topsecret", text)
        self.assertTrue(self._mine(ZHANG)["has_secret"])

    def test_the_webhook_is_encrypted_in_the_database(self) -> None:
        self._save(ZHANG, webhook_url=ZHANG_URL)
        with self.Session() as db:
            cipher = db.scalar(select(NotifyChannel.webhook_url_cipher).where(NotifyChannel.channel_key == user_channel_key(ZHANG)))
        self.assertNotIn("hook", cipher)

    def test_only_feishu_addresses_are_accepted(self) -> None:
        response = self._save(ZHANG, webhook_url="https://169.254.169.254/latest/meta-data")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._mine(ZHANG))

    def test_the_first_save_needs_a_webhook(self) -> None:
        self.assertEqual(self._save(ZHANG).status_code, 400)

    def test_later_saves_keep_the_saved_webhook(self) -> None:
        self._save(ZHANG, webhook_url=ZHANG_URL)
        self._save(ZHANG, name="改名")
        mine = self._mine(ZHANG)
        self.assertEqual(mine["name"], "改名")
        self.assertTrue(mine["has_webhook"])

    def test_a_user_cannot_touch_the_system_channel(self) -> None:
        self._save(ZHANG, webhook_url=ZHANG_URL)
        with self.Session() as db:
            system = db.scalar(select(NotifyChannel).where(NotifyChannel.channel_key == SCHEDULE_CHANNEL_KEY))
            self.assertEqual(feishu.decrypt_value(system.webhook_url_cipher), SYSTEM_URL)

    def test_the_first_super_admin_edits_the_system_channel(self) -> None:
        self.assertEqual(self._mine(ADMIN)["channel_key"], SCHEDULE_CHANNEL_KEY)

    def test_the_test_button_only_uses_my_own_channel(self) -> None:
        self._save(ZHANG, webhook_url=ZHANG_URL)
        with self.Session() as db, patch.object(rhythm_api, "test_channel", return_value={"ok": True, "latency_ms": 1}) as test:
            rhythm_api.test_my_notify_channel(rhythm_api.MyNotifyChannelTestIn(), db=db, context=self._context(ZHANG))
        self.assertEqual(test.call_args.kwargs["channel_key"], user_channel_key(ZHANG))

    def test_testing_before_saving_uses_the_typed_address(self) -> None:
        with self.Session() as db, patch.object(rhythm_api, "test_channel", return_value={"ok": True, "latency_ms": 1}) as test:
            rhythm_api.test_my_notify_channel(rhythm_api.MyNotifyChannelTestIn(webhook_url=ZHANG_URL), db=db, context=self._context(ZHANG))
        self.assertIsNone(test.call_args.kwargs["channel_key"])
        self.assertEqual(test.call_args.kwargs["webhook_url"], ZHANG_URL)

    def test_researchers_can_and_viewers_cannot(self) -> None:
        self.assertIn("schedule:manage", ROLE_PERMISSIONS["researcher"])
        self.assertNotIn("schedule:manage", ROLE_PERMISSIONS["viewer"])


if __name__ == "__main__":
    unittest.main()
