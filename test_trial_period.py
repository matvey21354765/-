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

    def test_existing_trial_not_cut_short(self):
        """Пользователь, начавший тест по старым правилам, сохраняет свои дни."""
        cb._USER_REGISTRY["9000004"] = {
            "first_seen": int(time.time()) - 4 * 86400,
            "trial_start": int(time.time()) - 4 * 86400,
            "searches": 0,
        }
        self.register(9_000_004)
        info = cb._trial_info(9_000_004)
        self.assertEqual(info["total"], cb.LEGACY_TRIAL_DAYS)
        self.assertFalse(info["ended"])

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


class TestTrialTexts(unittest.TestCase):
    def test_ui_texts_show_three_days(self):
        from pathlib import Path
        src = Path(cb.__file__).with_suffix(".py").read_text(encoding="utf-8")
        self.assertNotIn("Осталось 7 из 7 дней", src)
        self.assertNotIn("7 дней бесплатно", src)
        self.assertIn("дня бесплатно", src)


if __name__ == "__main__":
    unittest.main()
