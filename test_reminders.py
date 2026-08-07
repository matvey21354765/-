"""Напоминания о боте: возврат, мотивирующие письма, конец доступа."""
import inspect
import time
import unittest

import control_bot as cb


class TestLastActivityFallback(unittest.TestCase):
    """Реестр может быть неполным — напоминания это не должно выключать."""

    def setUp(self):
        self._backup = dict(cb._USER_REGISTRY)
        cb._USER_REGISTRY.clear()

    def tearDown(self):
        cb._USER_REGISTRY.clear()
        cb._USER_REGISTRY.update(self._backup)

    def test_registry_entry_is_used_when_present(self):
        cb._USER_REGISTRY["42"] = {"last_seen": 1_700_000_000}
        self.assertEqual(cb._ps_last_activity(42), 1_700_000_000)

    def test_search_time_is_used_when_registry_is_empty(self):
        """После сбоя статистики отметки нет — берём время поиска.

        Иначе «молчание» считается от нуля, это сотни тысяч часов, и
        напоминание не уходит никогда.
        """
        got = cb._ps_last_activity(42, {"updated_at": 1_700_000_500})
        self.assertEqual(got, 1_700_000_500)

    def test_created_at_is_the_last_resort(self):
        self.assertEqual(cb._ps_last_activity(42, {"created_at": 1_700_000_100}),
                         1_700_000_100)

    def test_no_marks_at_all_returns_zero(self):
        self.assertEqual(cb._ps_last_activity(42, {}), 0.0)

    def test_loop_skips_users_without_any_activity_mark(self):
        """Ноль — не повод считать, что человек молчит сотни тысяч часов."""
        src = inspect.getsource(cb._ps_comeback_loop)
        self.assertIn("if _seen_at <= 0:", src)
        self.assertIn("_ps_last_activity(uid, s)", src)


class TestComebackLoop(unittest.TestCase):
    def test_window_is_sane(self):
        self.assertGreaterEqual(cb.PS_COMEBACK_AFTER_HOURS, 1)
        self.assertGreater(cb.PS_COMEBACK_MAX_HOURS, cb.PS_COMEBACK_AFTER_HOURS)

    def test_reminder_respects_quiet_hours_and_subscription(self):
        src = inspect.getsource(cb._ps_comeback_loop)
        self.assertIn("in_quiet_hours", src)
        self.assertIn('_subscription_info(uid)["ended"]', src)

    def test_reminder_is_sent_once_a_day(self):
        src = inspect.getsource(cb._ps_comeback_loop)
        self.assertIn('notify_once', src)
        self.assertIn("%Y-%m-%d", src)

    def test_reminder_carries_real_recommendations(self):
        src = inspect.getsource(cb._ps_comeback_loop)
        self.assertIn("_ps.recommendations", src)
        self.assertIn("_ps_send_recommendations", src)


class TestPushLoop(unittest.TestCase):
    """Мотивирующие письма раз в 2–3 дня."""

    def test_first_pass_is_not_a_day_away(self):
        """Сутки ожидания = цикл не доживал до первой проверки при частых
        перезапусках контейнера и не слал ничего вообще."""
        src = inspect.getsource(cb._push_notification_loop)
        self.assertIn("PUSH_FIRST_DELAY_SEC", src)
        self.assertNotIn("await asyncio.sleep(24 * 3600)\n    while True:", src)

    def test_interval_between_letters_is_kept_per_user(self):
        src = inspect.getsource(cb._push_notification_loop)
        self.assertIn("last_push_notif.txt", src)
        self.assertIn("_PUSH_INTERVAL_SEC", src)
        self.assertGreaterEqual(cb._PUSH_INTERVAL_SEC, 2 * 86400)

    def test_night_is_respected(self):
        src = inspect.getsource(cb._push_notification_loop)
        self.assertIn("in_quiet_hours", src)

    def test_user_can_switch_it_off(self):
        src = inspect.getsource(cb._push_notification_loop)
        self.assertIn('tips_enabled', src)


class TestExpiryReminder(unittest.TestCase):
    def test_night_is_respected(self):
        src = inspect.getsource(cb._trial_notification_loop)
        self.assertIn("in_quiet_hours", src)

    def test_mark_is_set_only_after_a_successful_send(self):
        """Пропуск по тишине не должен «съедать» напоминание."""
        src = inspect.getsource(cb._trial_notification_loop)
        quiet = src.index("in_quiet_hours")
        send = src.index("await bot.send_message")
        mark = src.index("notified.append(marker)", send)
        self.assertLess(quiet, send)
        self.assertLess(send, mark)

    def test_thresholds_cover_a_three_day_trial(self):
        hours = [h for h, _ in cb._ACCESS_REMINDERS]
        self.assertIn(24, hours)
        self.assertIn(6, hours)


if __name__ == "__main__":
    unittest.main()
