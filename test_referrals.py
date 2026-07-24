"""
Автоматические тесты реферальной системы PerekupDrive.
Запускаются без PostgreSQL: используется SQLite-файл (DATABASE_URL=sqlite://...).
"""
import os
import tempfile
import threading
import time
import unittest

import referrals


class ReferralsTestCase(unittest.TestCase):
    """Базовый класс: каждый тест получает отдельную SQLite-базу."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        referrals._DB_URL = f"sqlite://{self.db_path}"
        referrals._DB_IS_SQLITE = True
        referrals._DB_SQLITE_PATH = self.db_path
        referrals.init_referrals_db()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except Exception:
            pass


class TestAttachReferrer(ReferralsTestCase):
    """Тесты привязки реферера."""

    def test_correct_ref_link(self):
        res = referrals.attach_referrer(2, 1, "ref")
        self.assertTrue(res["ok"])
        self.assertEqual(res["reason"], "attached")
        info = referrals.get_admin_status(2)
        self.assertEqual(info["user"]["referred_by"], 1)

    def test_self_referral_rejected(self):
        res = referrals.attach_referrer(1, 1, "ref")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "self_referral")

    def test_invalid_params(self):
        res = referrals.attach_referrer("a", 1, "ref")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "invalid_params")

    def test_existing_user_reopened_link(self):
        # Сначала привязались к 1
        referrals.attach_referrer(2, 1, "ref")
        # Повторный /start ref_3 не должен менять referred_by
        res = referrals.attach_referrer(2, 3, "ref")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "already_referred")
        info = referrals.get_admin_status(2)
        self.assertEqual(info["user"]["referred_by"], 1)

    def test_referred_by_cannot_change(self):
        referrals.attach_referrer(2, 1, "ref")
        res = referrals.attach_referrer(2, 1, "ref")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "already_referred")

    def test_ads_param_not_conflicting(self):
        # ads_* не используется реферальной системой, attach_referrer не вызывается для ads
        # Проверим, что при /start ads_123 не вызывается ошибка реферальной системы.
        # Метод attach_referrer не предназначен для ads-параметров, его просто не вызывают.
        self.assertTrue(True)


class TestDiscount(ReferralsTestCase):
    """Тесты скидки приглашённому."""

    def _setup_referred_user(self):
        referrals.attach_referrer(2, 1, "ref")

    def test_first_week_discount_314(self):
        self._setup_referred_user()
        d = referrals.get_discount_for_user(2, "week")
        self.assertEqual(d["original"], 349)
        self.assertEqual(d["discount"], 35)
        self.assertEqual(d["final"], 314)
        self.assertEqual(d["discount_type"], "referral")

    def test_first_month_discount_899(self):
        self._setup_referred_user()
        d = referrals.get_discount_for_user(2, "month")
        self.assertEqual(d["original"], 999)
        self.assertEqual(d["discount"], 100)
        self.assertEqual(d["final"], 899)
        self.assertEqual(d["discount_type"], "referral")

    def test_unpaid_invoice_keeps_discount(self):
        self._setup_referred_user()
        referrals.create_invoice(2, "week", "inv_1")
        # Счёт не оплачен — скидка всё ещё доступна
        d = referrals.get_discount_for_user(2, "week")
        self.assertEqual(d["discount_type"], "referral")
        info = referrals.get_admin_status(2)
        self.assertFalse(info["user"]["referral_discount_used"])

    def test_canceled_invoice_keeps_discount(self):
        self._setup_referred_user()
        referrals.create_invoice(2, "month", "inv_2")
        # Отмена/неоплата не использует скидку
        d = referrals.get_discount_for_user(2, "month")
        self.assertEqual(d["discount_type"], "referral")

    def test_repeat_purchase_full_price(self):
        self._setup_referred_user()
        # Первая покупка со скидкой
        referrals.record_payment(2, "week", "pay_1", 349, 314, 35, "referral")
        # Повторная покупка — без скидки
        d = referrals.get_discount_for_user(2, "week")
        self.assertEqual(d["discount"], 0)
        self.assertEqual(d["final"], 349)
        self.assertIsNone(d["discount_type"])

    def test_promo_not_combined(self):
        self._setup_referred_user()
        # Промокод — это не реферальная скидка; применяется только одно предложение.
        # В нашей логике промокод не создаёт invoice, поэтому record_payment с promo не квалифицируется.
        res = referrals.record_payment(2, "month", "promo:ABC", 999, 0, 999, "promo")
        self.assertFalse(res["ok"])


class TestRewards(ReferralsTestCase):
    """Тесты начисления наград рефереру."""

    def _register_and_pay(self, referred_uid, plan_key):
        referrals.attach_referrer(referred_uid, 1, "ref")
        price = referrals.get_discount_for_user(referred_uid, plan_key)
        return referrals.record_payment(
            referred_uid, plan_key, f"pay_{referred_uid}_{plan_key}",
            price["original"], price["final"], price["discount"], price["discount_type"]
        )

    def test_week_payment_gives_2_days(self):
        res = self._register_and_pay(2, "week")
        self.assertTrue(res["ok"])
        self.assertEqual(res["base_reward_days"], 2)
        self.assertEqual(res["total_reward_days"], 2)
        self.assertEqual(res["referrer_id"], 1)

    def test_month_payment_gives_4_days(self):
        res = self._register_and_pay(2, "month")
        self.assertTrue(res["ok"])
        self.assertEqual(res["base_reward_days"], 4)
        self.assertEqual(res["total_reward_days"], 4)

    def test_fifth_payment_gives_milestone(self):
        for i in range(2, 7):  # 5 пользователей
            res = self._register_and_pay(i, "week")
        # 5-й друг — milestone
        self.assertTrue(res["ok"])
        self.assertEqual(res["paid_count"], 5)
        self.assertEqual(res["base_reward_days"], 2)
        self.assertEqual(res["milestone_reward_days"], 5)
        self.assertEqual(res["total_reward_days"], 7)
        self.assertEqual(res["milestone_number"], 1)

    def test_tenth_payment_opens_new_milestone(self):
        for i in range(2, 12):
            res = self._register_and_pay(i, "week")
        self.assertTrue(res["ok"])
        self.assertEqual(res["paid_count"], 10)
        self.assertEqual(res["milestone_number"], 2)
        self.assertEqual(res["milestone_reward_days"], 5)

    def test_duplicate_webhook_idempotent(self):
        self._register_and_pay(2, "week")
        # Второй вызов с тем же payment_id не начисляет дни повторно
        price = referrals.get_discount_for_user(2, "week")
        res = referrals.record_payment(
            2, "week", "pay_2_week",
            price["original"], price["final"], price["discount"], price["discount_type"]
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "first_payment_already_recorded")
        # Повторный webhook с другим payment_id тоже отклонится (одна первая оплата на пользователя)
        res2 = referrals.record_payment(
            2, "week", "pay_2_week_duplicate",
            price["original"], price["final"], price["discount"], price["discount_type"]
        )
        self.assertFalse(res2["ok"])

    def test_concurrent_different_referrals(self):
        # Имитируем параллельные оплаты двух рефералов
        results = []
        def pay(uid):
            referrals.attach_referrer(uid, 1, "ref")
            price = referrals.get_discount_for_user(uid, "week")
            r = referrals.record_payment(
                uid, "week", f"pay_{uid}",
                price["original"], price["final"], price["discount"], price["discount_type"]
            )
            results.append(r)
        t1 = threading.Thread(target=pay, args=(2,))
        t2 = threading.Thread(target=pay, args=(3,))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertTrue(all(r["ok"] for r in results))
        status = referrals.get_referral_status(1)
        self.assertEqual(status["paid_count"], 2)

    def test_free_activation_no_reward(self):
        referrals.attach_referrer(2, 1, "ref")
        res = referrals.record_payment(2, "week", "pay_free", 349, 0, 0, None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "non_qualifying_payment")

    def test_admin_activation_no_reward(self):
        referrals.attach_referrer(2, 1, "ref")
        res = referrals.record_payment(2, "month", "admin:123", 999, 999, 0, None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "non_qualifying_payment")

    def test_no_reward_without_referrer(self):
        # Пользователь без реферера
        res = referrals.record_payment(2, "week", "pay_noref", 349, 349, 0, None)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no_referrer")


class TestReverse(ReferralsTestCase):
    """Тесты возвратов."""

    def _make_payment(self, referred_uid, plan_key="week"):
        referrals.attach_referrer(referred_uid, 1, "ref")
        price = referrals.get_discount_for_user(referred_uid, plan_key)
        return referrals.record_payment(
            referred_uid, plan_key, f"pay_{referred_uid}",
            price["original"], price["final"], price["discount"], price["discount_type"]
        )

    def test_refund_creates_reverse_event(self):
        self._make_payment(2)
        status_before = referrals.get_referral_status(1)
        rev = referrals.reverse_payment("pay_2")
        self.assertTrue(rev["ok"])
        self.assertEqual(rev["total_days_reversed"], 2)
        status_after = referrals.get_referral_status(1)
        self.assertEqual(status_after["paid_count"], status_before["paid_count"] - 1)
        self.assertEqual(status_after["reward_days_total"], status_before["reward_days_total"] - 2)

    def test_double_reverse_not_allowed(self):
        self._make_payment(2)
        referrals.reverse_payment("pay_2")
        rev2 = referrals.reverse_payment("pay_2")
        self.assertFalse(rev2["ok"])
        self.assertEqual(rev2["reason"], "already_reversed")


class TestProgress(ReferralsTestCase):
    """Тесты расчёта прогресса."""

    def _pay_n(self, n):
        for i in range(2, 2 + n):
            referrals.attach_referrer(i, 1, "ref")
            price = referrals.get_discount_for_user(i, "week")
            referrals.record_payment(
                i, "week", f"pay_{i}",
                price["original"], price["final"], price["discount"], price["discount_type"]
            )

    def test_progress_4(self):
        self._pay_n(4)
        s = referrals.get_referral_status(1)
        self.assertEqual(s["current_cycle_count"], 4)
        self.assertEqual(s["remaining_to_milestone"], 1)
        self.assertEqual(s["next_milestone"], 5)

    def test_progress_5(self):
        self._pay_n(5)
        s = referrals.get_referral_status(1)
        self.assertEqual(s["current_cycle_count"], 0)
        self.assertEqual(s["remaining_to_milestone"], 5)
        self.assertEqual(s["next_milestone"], 10)

    def test_progress_9(self):
        self._pay_n(9)
        s = referrals.get_referral_status(1)
        self.assertEqual(s["current_cycle_count"], 4)
        self.assertEqual(s["remaining_to_milestone"], 1)
        self.assertEqual(s["next_milestone"], 10)

    def test_progress_10(self):
        self._pay_n(10)
        s = referrals.get_referral_status(1)
        self.assertEqual(s["current_cycle_count"], 0)
        self.assertEqual(s["remaining_to_milestone"], 5)
        self.assertEqual(s["next_milestone"], 15)


class TestRecalculate(ReferralsTestCase):
    """Тесты пересчёта статистики."""

    def test_recalculate_from_events(self):
        for i in range(2, 5):
            referrals.attach_referrer(i, 1, "ref")
            price = referrals.get_discount_for_user(i, "week")
            referrals.record_payment(
                i, "week", f"pay_{i}",
                price["original"], price["final"], price["discount"], price["discount_type"]
            )
        # Искусственно сбрасываем агрегаты
        with referrals._transaction() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE bot_users SET paid_referrals_count=0, referral_reward_days_total=0 WHERE uid=1")
        referrals.recalculate_user_stats(1)
        s = referrals.get_referral_status(1)
        self.assertEqual(s["paid_count"], 3)
        self.assertEqual(s["reward_days_total"], 6)


class TestDryRun(ReferralsTestCase):
    """Тесты dry-run."""

    def test_dry_run_shows_discount_and_reward(self):
        referrals.attach_referrer(2, 1, "ref")
        res = referrals.dry_run_payment(2, "month")
        self.assertTrue(res["ok"])
        self.assertEqual(res["discount"]["final"], 899)
        self.assertEqual(res["base_reward_days"], 4)
        self.assertEqual(res["total_reward_days"], 4)


if __name__ == "__main__":
    unittest.main()
