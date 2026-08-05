"""Тесты пробного периода: 3 дня для новых, сохранность оплаченных подписок."""
import time
import unittest

import control_bot as cb


class TrialBase(unittest.TestCase):
    def setUp(self):
        self._backup = dict(cb._USER_REGISTRY)
        cb._USER_REGISTRY.clear()

    def tearDown(self):
        cb._USER_REGISTRY.clear()
        cb._USER_REGISTRY.update(self._backup)

    def register(self, uid):
        cb._register_user(uid, username=None, is_search=False, persist_db=False)


class TestTrialLength(TrialBase):
    def test_single_setting_is_three_days(self):
        self.assertEqual(cb.TRIAL_DAYS, 3)
        self.assertEqual(cb._env_trial_days(), 3)

    def test_new_user_gets_exactly_three_days(self):
        self.register(9_000_001)
        info = cb._trial_info(9_000_001)
        self.assertEqual(info["total"], 3)
        self.assertEqual(info["days_left"], 3)
        self.assertFalse(info["ended"])

    def test_end_date_is_start_plus_three_days(self):
        self.register(9_000_002)
        u = cb._USER_REGISTRY["9000002"]
        self.assertAlmostEqual(cb._trial_info(9_000_002)["ends_at"],
                               u["trial_start"] + 3 * 86400, delta=2)
        self.assertIn("МСК", cb._trial_info(9_000_002)["ends_at_msk"])

    def test_trial_cannot_be_restarted(self):
        self.register(9_000_003)
        start = cb._USER_REGISTRY["9000003"]["trial_start"]
        cb._USER_REGISTRY["9000003"]["trial_start"] = start - 5 * 86400
        self.register(9_000_003)  # повторный /start
        self.assertEqual(cb._USER_REGISTRY["9000003"]["trial_start"], start - 5 * 86400)
        self.assertTrue(cb._trial_info(9_000_003)["ended"])

    def test_trial_length_is_the_same_for_everyone(self):
        """Тест единый для всех: у давно зарегистрированных тоже TRIAL_DAYS.

        Раньше пользователи, пришедшие до перехода на короткий тест, держали
        семь дней, и рядом жили аккаунты с разным сроком.
        """
        cb._USER_REGISTRY["9000004"] = {
            "first_seen": int(time.time()) - 4 * 86400,
            "trial_start": int(time.time()) - 4 * 86400,
            "searches": 0,
        }
        self.register(9_000_004)
        info = cb._trial_info(9_000_004)
        self.assertEqual(info["total"], cb.TRIAL_DAYS)
        self.assertEqual(cb.TRIAL_DAYS, 3)

    def test_stored_longer_trial_is_shortened(self):
        """Сохранённые 7 дней не поднимают срок выше общего."""
        cb._USER_REGISTRY["9000006"] = {
            "first_seen": int(time.time()),
            "trial_start": int(time.time()),
            "trial_days": 7,
            "searches": 0,
        }
        self.register(9_000_006)
        self.assertEqual(cb._trial_info(9_000_006)["total"], cb.TRIAL_DAYS)

    def test_referral_bonus_days_add_on_top(self):
        self.register(9_000_005)
        cb._USER_REGISTRY["9000005"]["bonus_days"] = 4
        info = cb._trial_info(9_000_005)
        self.assertEqual(info["total"], 7)
        self.assertEqual(info["bonus"], 4)


class TestPaidAccess(TrialBase):
    def test_paid_subscription_not_affected(self):
        uid = 9_000_010
        s = cb.load_settings(uid)
        s["subscription_until"] = time.time() + 20 * 86400
        s["subscription_type"] = "month"
        cb.save_settings(uid, s)
        try:
            self.register(uid)
            info = cb._subscription_info(uid)
            self.assertTrue(info["is_paid"])
            self.assertFalse(info["ended"])
            self.assertGreaterEqual(info["days_left"], 19)
            self.assertEqual(info["total_days"], cb.SUBSCRIPTION_PLANS["month"]["days"])
        finally:
            s = cb.load_settings(uid)
            s.pop("subscription_until", None)
            s.pop("subscription_type", None)
            cb.save_settings(uid, s)

    def test_expired_trial_keeps_saved_data(self):
        """После теста поиски и сохранённые машины остаются в БД."""
        uid = 9_000_011
        cb._USER_REGISTRY["9000011"] = {
            "first_seen": int(time.time()) - 40 * 86400,
            "trial_start": int(time.time()) - 40 * 86400,
            "trial_days": 3, "searches": 0,
        }
        self.assertTrue(cb._trial_info(uid)["ended"])
        cb._ps.init_db()
        cb._ps.save_search(uid, region="perm", price_max=100_000)
        self.assertIsNotNone(cb._ps.get_active_search(uid))


class TestBudgetNormalization(unittest.TestCase):
    """Бюджет 0–0 отсекал ВСЕ объявления (in_price_range: 0 <= p <= 0)."""

    def test_zero_max_means_no_limit(self):
        s = cb.normalize_budget({"price_min": 0, "price_max": 0})
        self.assertEqual(s["price_max"], cb.NO_PRICE_LIMIT)

    def test_missing_max_means_no_limit(self):
        self.assertEqual(cb.normalize_budget({})["price_max"], cb.NO_PRICE_LIMIT)

    def test_max_below_min_means_no_limit(self):
        s = cb.normalize_budget({"price_min": 500_000, "price_max": 100})
        self.assertEqual(s["price_max"], cb.NO_PRICE_LIMIT)

    def test_real_budget_untouched(self):
        s = cb.normalize_budget({"price_min": 100_000, "price_max": 300_000})
        self.assertEqual((s["price_min"], s["price_max"]), (100_000, 300_000))

    def test_broken_budget_repaired_on_load(self):
        uid = 9_000_020
        cb.save_settings(uid, {"region": "ekaterinburg", "price_min": 0, "price_max": 0})
        try:
            self.assertEqual(cb.load_settings(uid)["price_max"], cb.NO_PRICE_LIMIT)
        finally:
            cb.save_settings(uid, {})

    def test_listings_pass_filter_with_no_limit(self):
        s = cb.normalize_budget({"price_min": 0, "price_max": 0})
        item = {"_price_int": 2_348_000, "price": "2 348 000", "title": "OMODA C5, 2026"}
        self.assertTrue(cb.in_price_range(item, s["price_min"], s["price_max"]))
        # тот же товар при бюджете 0–0 раньше отсекался
        self.assertFalse(cb.in_price_range(item, 0, 0))


class TestTrialTexts(unittest.TestCase):
    def test_ui_texts_show_three_days(self):
        from pathlib import Path
        src = Path(cb.__file__).with_suffix(".py").read_text(encoding="utf-8")
        self.assertNotIn("Осталось 7 из 7 дней", src)
        self.assertNotIn("7 дней бесплатно", src)
        self.assertIn("дня бесплатно", src)


class TestAccessExpiryReminders(TrialBase):
    """Напоминания о скором конце доступа — и теста, и платной подписки."""

    def test_free_access_is_three_days(self):
        self.register(9_000_020)
        info = cb._trial_info(9_000_020)
        self.assertEqual(cb.TRIAL_DAYS, 3)
        self.assertEqual(info["total"], 3)
        self.assertFalse(info["ended"])

    def test_trial_left_seconds_counted_from_start(self):
        uid = 9_000_021
        self.register(uid)
        cb._USER_REGISTRY[str(uid)]["trial_start"] = int(time.time()) - 2 * 86400
        left, is_paid = cb._access_left_seconds(uid)
        self.assertFalse(is_paid)
        self.assertAlmostEqual(left / 3600.0, 24, delta=1)

    def test_paid_subscription_is_tracked_too(self):
        """Раньше напоминания были только про тест, подписка кончалась молча."""
        uid = 9_000_022
        self.register(uid)
        s = cb.load_settings(uid)
        s["subscription_until"] = time.time() + 5 * 3600
        s["subscription_type"] = "month"
        cb.save_settings(uid, s)
        try:
            left, is_paid = cb._access_left_seconds(uid)
            self.assertTrue(is_paid)
            self.assertAlmostEqual(left / 3600.0, 5, delta=0.1)
        finally:
            s["subscription_until"] = 0
            cb.save_settings(uid, s)

    def test_thresholds_fit_a_three_day_trial(self):
        """Порог «за 3 дня» на трёхдневном тесте сработал бы в первый час."""
        hours = [h for h, _ in cb._ACCESS_REMINDERS]
        self.assertIn(24, hours)
        self.assertIn(6, hours)
        self.assertEqual(sorted(hours, reverse=True), hours)


if __name__ == "__main__":
    unittest.main()
