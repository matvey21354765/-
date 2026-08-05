"""Цена подписки в интерфейсе: реферальная скидка 10% и пейволл.

Скидка обещана в реферальной ссылке, поэтому она обязана быть видна везде,
где показана цена, и совпадать с суммой выставленного счёта.
"""
import os
import tempfile
import unittest

import referrals
import control_bot as cb


class PricingTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        referrals._DB_URL = f"sqlite://{self.db_path}"
        referrals._DB_IS_SQLITE = True
        referrals._DB_SQLITE_PATH = self.db_path
        referrals.init_referrals_db()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except Exception:
            pass

    def invited(self, uid=2, inviter=1):
        referrals.attach_referrer(uid, inviter, "ref")
        return uid


class TestPlanPrice(PricingTestCase):
    def test_plain_user_pays_full_price(self):
        for key, plan in cb.SUBSCRIPTION_PLANS.items():
            price = cb._plan_price(777, key)
            self.assertEqual(price["final"], plan["amount"])
            self.assertEqual(price["discount"], 0)
            self.assertIsNone(price["discount_type"])

    def test_invited_user_gets_ten_percent_off(self):
        uid = self.invited()
        for key, plan in cb.SUBSCRIPTION_PLANS.items():
            price = cb._plan_price(uid, key)
            self.assertEqual(price["discount_type"], "referral")
            self.assertEqual(price["original"], plan["amount"])
            self.assertEqual(price["final"], plan["amount"] - price["discount"])
            # Скидка — ровно ставка из referrals, без сюрпризов округления.
            self.assertAlmostEqual(
                price["discount"] / plan["amount"],
                referrals.REFERRAL_DISCOUNT_RATE, delta=0.005)

    def test_price_follows_tariff_when_discount_table_drifts(self):
        """Прайс живёт в SUBSCRIPTION_PLANS: расхождение не должно
        показывать одну цену, а выставлять счёт на другую."""
        uid = self.invited()
        old = referrals.REFERRAL_PRICES["week"]
        referrals.REFERRAL_PRICES["week"] = {
            "original": 100, "discount": 10, "final": 90}
        try:
            price = cb._plan_price(uid, "week")
            self.assertEqual(price["original"], cb.SUBSCRIPTION_PLANS["week"]["amount"])
            self.assertEqual(price["final"], price["original"] - price["discount"])
        finally:
            referrals.REFERRAL_PRICES["week"] = old

    def test_discount_disappears_after_first_payment(self):
        uid = self.invited()
        price = cb._plan_price(uid, "month")
        referrals.record_payment(uid, "month", "op-1", price["original"],
                                 price["final"], price["discount"], "referral")
        after = cb._plan_price(uid, "month")
        self.assertEqual(after["discount"], 0)
        self.assertEqual(after["final"], cb.SUBSCRIPTION_PLANS["month"]["amount"])

    def test_broken_referral_db_still_returns_full_price(self):
        referrals._DB_SQLITE_PATH = "/nonexistent/dir/x.db"
        price = cb._plan_price(2, "week")
        self.assertEqual(price["final"], cb.SUBSCRIPTION_PLANS["week"]["amount"])


class TestInvoiceMatchesShownPrice(PricingTestCase):
    def test_invoice_stores_exactly_the_price_the_user_saw(self):
        uid = self.invited()
        shown = cb._plan_price(uid, "week")
        referrals.create_invoice(uid, "week", "sub_2_week_1", shown)
        invoice = referrals.get_invoice("sub_2_week_1")
        self.assertEqual(invoice["final_amount"], shown["final"])
        self.assertEqual(invoice["original_amount"], shown["original"])
        self.assertEqual(invoice["applied_discount_type"], "referral")

    def test_invoice_without_override_keeps_old_behaviour(self):
        uid = self.invited()
        referrals.create_invoice(uid, "month", "sub_2_month_1")
        invoice = referrals.get_invoice("sub_2_month_1")
        self.assertEqual(invoice["final_amount"],
                         referrals.REFERRAL_PRICES["month"]["final"])


class TestPaywallShowsDiscount(PricingTestCase):
    def test_paywall_names_the_discount_for_invited_user(self):
        uid = self.invited()
        text = cb._paywall_text(uid)
        self.assertIn("10%", text)
        self.assertIn(str(cb._plan_price(uid, "month")["final"]), text)

    def test_paywall_stays_clean_for_everyone_else(self):
        text = cb._paywall_text(999)
        self.assertNotIn("Скидка", text)

    def test_buttons_show_personal_price(self):
        uid = self.invited()
        kb = cb._subscription_keyboard(uid, expired=True)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        week = cb._plan_price(uid, "week")
        self.assertTrue(any(f"{week['final']} ₽" in x for x in labels), labels)
        # «Назад» вело в закрытый раздел и возвращало тот же пейволл.
        self.assertFalse(any("Назад" in x for x in labels), labels)

    def test_back_button_present_while_access_is_alive(self):
        kb = cb._subscription_keyboard(999, expired=False)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Назад" in x for x in labels), labels)

    def test_invite_screens_are_open_without_subscription(self):
        """Приглашать друзей можно и без доступа — это способ его вернуть."""
        self.assertIn("ref_stats", cb.SubscriptionMiddleware.ALLOWED_CALLBACKS)
        self.assertIn("🤝 Пригласить друга", cb.SubscriptionMiddleware.ALLOWED_TEXTS)


if __name__ == "__main__":
    unittest.main()


class TestOneTimeDiscountLink(PricingTestCase):
    """Скидочной ссылкой можно заплатить один раз."""

    def test_repeated_clicks_reuse_the_same_link(self):
        uid = self.invited()
        price = cb._plan_price(uid, "week")
        referrals.create_invoice(uid, "week", "sub_2_week_100", price)
        again = referrals.find_open_invoice(uid, "week")
        self.assertEqual(again["label"], "sub_2_week_100")
        self.assertEqual(again["final_amount"], price["final"])

    def test_paid_link_is_not_offered_again(self):
        uid = self.invited()
        price = cb._plan_price(uid, "week")
        referrals.create_invoice(uid, "week", "sub_2_week_100", price)
        referrals.mark_invoice_paid("sub_2_week_100")
        self.assertIsNone(referrals.find_open_invoice(uid, "week"))

    def test_next_invoice_after_payment_is_full_price(self):
        uid = self.invited()
        price = cb._plan_price(uid, "week")
        referrals.create_invoice(uid, "week", "sub_2_week_100", price)
        referrals.mark_invoice_paid("sub_2_week_100")
        referrals.record_payment(uid, "week", "op-77", price["original"],
                                 price["final"], price["discount"], "referral")
        after = cb._plan_price(uid, "week")
        self.assertEqual(after["discount"], 0)
        self.assertEqual(after["final"], cb.SUBSCRIPTION_PLANS["week"]["amount"])

    def test_second_reward_is_not_granted_for_the_same_user(self):
        """Даже если оплатить дважды, реферер получает награду один раз."""
        uid = self.invited()
        price = cb._plan_price(uid, "month")
        first = referrals.record_payment(uid, "month", "op-1", price["original"],
                                         price["final"], price["discount"], "referral")
        second = referrals.record_payment(uid, "month", "op-2",
                                          price["original"], price["final"], 0, None)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "first_payment_already_recorded")

    def test_open_invoice_is_per_plan(self):
        uid = self.invited()
        referrals.create_invoice(uid, "week", "sub_2_week_1", cb._plan_price(uid, "week"))
        self.assertIsNone(referrals.find_open_invoice(uid, "month"))
