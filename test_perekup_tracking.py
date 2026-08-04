"""Тесты отслеживания объявлений (история цен, наблюдения, антидубли, статистика)."""
import os
import tempfile
import time
import unittest
from pathlib import Path


class TrackingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PEREKUP_DB_PATH"] = str(Path(self._tmp.name) / "t.db")
        import importlib
        import perekup_tracking
        importlib.reload(perekup_tracking)
        self.pt = perekup_tracking
        self.pt.init_db()

    def tearDown(self):
        self._tmp.cleanup()

    def item(self, price, url="https://avito.ru/car/1", source="avito", **kw):
        d = {"_price_int": price, "url": url, "source": source,
             "title": "Toyota Camry 2016"}
        d.update(kw)
        return d


class TestPriceHistory(TrackingTestBase):
    def test_first_seen(self):
        r = self.pt.record_listing(self.item(1_000_000))
        self.assertEqual(r["event"], "first_seen")

    def test_same_price_creates_no_event(self):
        self.pt.record_listing(self.item(1_000_000))
        r = self.pt.record_listing(self.item(1_000_000))
        self.assertEqual(r["event"], "none")
        # в истории только одно событие (first_seen)
        hist = self.pt.price_history(self.pt.listing_key(self.item(1_000_000)))
        self.assertEqual(len(hist), 1)

    def test_real_drop_creates_single_event(self):
        self.pt.record_listing(self.item(1_000_000))
        r = self.pt.record_listing(self.item(900_000))
        self.assertEqual(r["event"], "price_drop")
        self.assertEqual(r["drop"], 100_000)
        self.assertEqual(r["drops_count"], 1)
        hist = self.pt.price_history(self.pt.listing_key(self.item(900_000)))
        self.assertEqual(len(hist), 2)

    def test_price_increase(self):
        self.pt.record_listing(self.item(900_000))
        r = self.pt.record_listing(self.item(950_000))
        self.assertEqual(r["event"], "price_increase")

    def test_relisted(self):
        it = self.item(800_000)
        self.pt.record_listing(it)
        self.pt.mark_removed([self.pt.listing_key(it)])
        r = self.pt.record_listing(self.item(770_000))
        self.assertEqual(r["event"], "relisted")

    def test_significant_drop_thresholds(self):
        # дешёвая машина: 200к -> 195к = 2.5%, 5000₽ — незначимо
        self.assertFalse(self.pt.is_significant_drop(200_000, 195_000))
        # 200к -> 188к = 6% — значимо
        self.assertTrue(self.pt.is_significant_drop(200_000, 188_000))
        # дорогая: 2млн -> 1.97млн = 1.5% но 30 000₽ — значимо по сумме
        self.assertTrue(self.pt.is_significant_drop(2_000_000, 1_970_000))
        # рост ценой не является снижением
        self.assertFalse(self.pt.is_significant_drop(500_000, 550_000))


class TestWatches(TrackingTestBase):
    def test_watch_created_once(self):
        it = self.item(700_000)
        self.assertTrue(self.pt.add_watch(1, it))
        self.assertFalse(self.pt.add_watch(1, it))  # дубля нет
        self.assertEqual(len(self.pt.user_watches(1)), 1)

    def test_expired_watch_not_returned(self):
        it = self.item(700_000)
        past = time.time() - 100 * 86400
        self.pt.add_watch(1, it, days=1, now=past)
        self.assertEqual(self.pt.watchers_for(self.pt.listing_key(it)), [])
        self.pt.expire_watches()
        self.assertEqual(len(self.pt.user_watches(1)), 0)

    def test_unlimited_watch_for_garage(self):
        it = self.item(700_000)
        self.pt.add_watch(1, it, days=0)  # бессрочно
        self.assertEqual(len(self.pt.watchers_for(self.pt.listing_key(it))), 1)

    def test_stop_watch(self):
        it = self.item(700_000)
        self.pt.add_watch(1, it)
        self.pt.stop_watch(1, self.pt.listing_key(it))
        self.assertEqual(len(self.pt.user_watches(1)), 0)


class TestNotificationDedup(TrackingTestBase):
    def test_same_drop_sent_once(self):
        key = "avito:1"
        sig = self.pt.drop_signature(key, 1_000_000, 900_000)
        self.assertTrue(self.pt.should_notify(1, "price_drop", sig, key))
        self.assertFalse(self.pt.should_notify(1, "price_drop", sig, key))

    def test_new_drop_is_new_event(self):
        key = "avito:1"
        s1 = self.pt.drop_signature(key, 1_000_000, 900_000)
        s2 = self.pt.drop_signature(key, 900_000, 850_000)
        self.assertTrue(self.pt.should_notify(1, "price_drop", s1, key))
        self.assertTrue(self.pt.should_notify(1, "price_drop", s2, key))

    def test_per_user_isolation(self):
        key = "avito:1"
        sig = self.pt.drop_signature(key, 1_000_000, 900_000)
        self.assertTrue(self.pt.should_notify(1, "price_drop", sig, key))
        self.assertTrue(self.pt.should_notify(2, "price_drop", sig, key))


class TestViewsAndStats(TrackingTestBase):
    def test_view_recorded_and_counted(self):
        it = self.item(700_000)
        self.pt.record_view(5, it)
        self.assertIsNotNone(self.pt.has_viewed(5, self.pt.listing_key(it)))
        self.assertEqual(self.pt.get_stats(5)["opened"], 1)

    def test_stats_are_personal(self):
        self.pt.bump_stat(1, "matched", 3)
        self.pt.bump_stat(2, "matched", 7)
        self.assertEqual(self.pt.get_stats(1)["matched"], 3)
        self.assertEqual(self.pt.get_stats(2)["matched"], 7)

    def test_unknown_field_ignored(self):
        self.pt.bump_stat(1, "dealscore", 5)  # не должно падать
        self.assertEqual(self.pt.get_stats(1)["matched"], 0)


class TestAgeBuckets(TrackingTestBase):
    def test_fresh_under_2h(self):
        it = self.item(700_000, _first_seen_ts=time.time() - 600)  # 10 минут
        self.assertEqual(self.pt.age_bucket(it), "fresh")

    def test_today_2_to_24h(self):
        it = self.item(700_000, _first_seen_ts=time.time() - 5 * 3600)
        self.assertEqual(self.pt.age_bucket(it), "today")

    def test_days3_24_to_72h(self):
        it = self.item(700_000, _first_seen_ts=time.time() - 40 * 3600)
        self.assertEqual(self.pt.age_bucket(it), "days3")

    def test_old_over_72h(self):
        it = self.item(700_000, _first_seen_ts=time.time() - 10 * 86400)
        self.assertEqual(self.pt.age_bucket(it), "old")

    def test_days_on_site_fallback(self):
        self.assertEqual(self.pt.age_bucket({"_days_on_site": 2}), "days3")
        self.assertEqual(self.pt.age_bucket({"_days_on_site": 9}), "old")


class TestBargainReasons(TrackingTestBase):
    def test_old_listing_without_reason(self):
        it = self.item(700_000, url="https://avito.ru/car/nr")
        self.pt.record_listing(it)
        self.assertEqual(self.pt.bargain_reasons(it), [])

    def test_old_listing_with_drop_has_reason(self):
        it = self.item(700_000, url="https://avito.ru/car/dr")
        self.pt.record_listing(it)
        it2 = self.item(650_000, url="https://avito.ru/car/dr")
        self.pt.record_listing(it2)
        reasons = self.pt.bargain_reasons(it2)
        self.assertTrue(any("сниж" in r for r in reasons))

    def test_torg_in_text_is_reason(self):
        it = self.item(700_000, url="https://avito.ru/car/t",
                       description="Продам, возможен торг")
        self.pt.record_listing(it)
        self.assertTrue(any("торг" in r for r in self.pt.bargain_reasons(it)))


class TestListingKey(TrackingTestBase):
    def test_key_stable_across_query_params(self):
        a = self.item(1, url="https://avito.ru/car/5?utm=x")
        b = self.item(1, url="https://avito.ru/car/5/")
        self.assertEqual(self.pt.listing_key(a), self.pt.listing_key(b))

    def test_key_includes_source(self):
        a = self.item(1, url="https://x/1", source="avito")
        b = self.item(1, url="https://x/1", source="drom")
        self.assertNotEqual(self.pt.listing_key(a), self.pt.listing_key(b))


if __name__ == "__main__":
    unittest.main()
