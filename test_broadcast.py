"""Обычная рассылка: предпросмотр, подтверждение, доставка и отмена."""
import asyncio
import unittest
from types import SimpleNamespace

import control_bot as cb


class FakeBot:
    def __init__(self, blocked=(), broken=()):
        self.sent = []
        self.copied = []
        self.blocked = set(blocked)
        self.broken = set(broken)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        if chat_id in self.broken:
            raise RuntimeError("Bad Request: chat not found")
        self.sent.append((chat_id, text))

    async def copy_message(self, chat_id, from_chat, msg_id, **kw):
        if chat_id in self.blocked:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.copied.append((chat_id, from_chat, msg_id))


class FakeCallback:
    def __init__(self, uid, data):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.answers = []
        self.edits = []
        outer = self

        class _Msg:
            async def edit_text(self, text, **kw):
                outer.edits.append(text)

        self.message = _Msg()

    async def answer(self, text=None, **kw):
        self.answers.append(text)


class BroadcastTestCase(unittest.TestCase):
    ADMIN = 111
    USERS = [1, 2, 3, 4]

    def setUp(self):
        self._admins = set(cb.ADMIN_IDS)
        cb.ADMIN_IDS.clear()
        cb.ADMIN_IDS.add(self.ADMIN)
        self._bot = cb.bot
        self._all = cb._all_user_ids
        cb._all_user_ids = lambda: list(self.USERS)
        cb._pending_broadcast.clear()

    def tearDown(self):
        cb.ADMIN_IDS.clear()
        cb.ADMIN_IDS.update(self._admins)
        cb.bot = self._bot
        cb._all_user_ids = self._all
        cb._pending_broadcast.clear()

    def run_confirm(self, fake, action="go", uid=None):
        cb.bot = fake
        cbq = FakeCallback(uid if uid is not None else self.ADMIN, f"bcast|{action}")
        asyncio.run(cb.cb_broadcast(cbq))
        return cbq


class TestManualBroadcast(BroadcastTestCase):
    def test_text_reaches_every_user(self):
        cb._pending_broadcast[self.ADMIN] = {"text": "Привет всем"}
        fake = FakeBot()
        self.run_confirm(fake)
        # Последнее сообщение — отчёт админу, его в рассылку не считаем.
        delivered = [(uid, text) for uid, text in fake.sent[:-1]]
        self.assertEqual([uid for uid, _ in delivered], self.USERS)
        self.assertTrue(all(text == "Привет всем" for _, text in delivered))
        self.assertIn("Доставлено: 4", fake.sent[-1][1])

    def test_reply_mode_copies_the_original_message(self):
        """Ответ командой на сообщение рассылает его как есть — с фото."""
        cb._pending_broadcast[self.ADMIN] = {"from_chat": 99, "msg_id": 7}
        fake = FakeBot()
        self.run_confirm(fake)
        self.assertEqual([uid for uid, _, _ in fake.copied], self.USERS)
        self.assertEqual(fake.sent[-1][0], self.ADMIN)   # отчёт админу

    def test_blocked_users_do_not_break_the_run(self):
        cb._pending_broadcast[self.ADMIN] = {"text": "Текст"}
        fake = FakeBot(blocked={2}, broken={3})
        self.run_confirm(fake)
        delivered = [uid for uid, _ in fake.sent if uid in self.USERS]
        self.assertEqual(delivered, [1, 4])
        report = fake.sent[-1][1]
        self.assertIn("Доставлено: 2", report)
        self.assertIn("Заблокировали бота: 2", report)

    def test_cancel_sends_nothing(self):
        cb._pending_broadcast[self.ADMIN] = {"text": "Не отправлять"}
        fake = FakeBot()
        cbq = self.run_confirm(fake, action="cancel")
        self.assertEqual(fake.sent, [])
        self.assertIn("отменена", cbq.edits[0])

    def test_confirm_without_pending_sends_nothing(self):
        fake = FakeBot()
        self.run_confirm(fake)
        self.assertEqual(fake.sent, [])

    def test_non_admin_cannot_broadcast(self):
        cb._pending_broadcast[self.ADMIN] = {"text": "Текст"}
        fake = FakeBot()
        cbq = self.run_confirm(fake, uid=999)
        self.assertEqual(fake.sent, [])
        self.assertIn("администраторов", cbq.answers[0])


class TestScheduleIsEmpty(unittest.TestCase):
    def test_announcement_is_off(self):
        """Запланированный анонс снят: рассылка идёт только вручную."""
        import inspect
        src = inspect.getsource(cb._scheduled_broadcast_loop)
        self.assertNotIn("schedule_broadcast(", src)
        self.assertIn("drop_cancelled_broadcasts", src)


if __name__ == "__main__":
    unittest.main()


class TestCorruptedCacheDoesNotBreakMonitor(unittest.TestCase):
    """Монитор падал с «'str' object has no attribute 'get'» и молчал."""

    def test_strings_are_filtered_out(self):
        mixed = [{"url": "a"}, "мусор", None, {"url": "b"}, 42]
        self.assertEqual(cb._only_listings(mixed), [{"url": "a"}, {"url": "b"}])

    def test_non_list_becomes_empty(self):
        self.assertEqual(cb._only_listings("строка"), [])
        self.assertEqual(cb._only_listings(None), [])

    def test_monitor_sanitizes_the_cache(self):
        import inspect
        src = inspect.getsource(cb._global_monitor_loop)
        assert "_only_listings" in src
