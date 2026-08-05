"""Тесты постоянного поиска: категории по возрасту, сохранение, снижение цены,
дедупликация уведомлений, утренний и вечерний отчёты, рыночная цена."""
import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path


class SearchTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["PEREKUP_DB_PATH"] = str(Path(self._tmp.name) / "s.db")
        import perekup_tracking
        importlib.reload(perekup_tracking)
        import perekup_search
        importlib.reload(perekup_search)
        self.pt = perekup_tracking
        self.ps = perekup_search
        self.ps.init_db()
        self.now = time.time()

    def tearDown(self):
        self._tmp.cleanup()

    def item(self, price=100_000, url="https://avito.ru/car/1", source="avito",
             title="ВАЗ-2114, 2008", region="perm", **kw):
        d = {"_price_int": price, "url": url, "source": source, "title": title,
             "region": region, "description": ""}
        d.update(kw)
        return d

    def ingest(self, **kw):
        it = self.item(**kw)
        self.ps.ingest_listing(it, now=kw.get("_now", self.now))
        return self.pt.listing_key(it)


class TestParsing(SearchTestBase):
    def test_brand_and_year(self):
        self.assertEqual(self.ps.parse_brand("ВАЗ-2114, 2008"), "vaz")
        self.assertEqual(self.ps.parse_brand("Toyota Camry, 2016"), "toyota")
        self.assertEqual(self.ps.parse_year({"title": "Toyota Camry, 2016"}), 2016)

    def test_condition(self):
        self.assertEqual(self.ps.detect_condition({"title": "Ваз", "description": "не на ходу"}),
                         "repair")
        self.assertEqual(self.ps.detect_condition({"title": "Ваз на ходу", "description": ""}),
                         "running")

    def test_junk(self):
        self.assertTrue(self.ps.is_junk({"title": "ВАЗ на запчасти", "description": ""}))
        self.assertFalse(self.ps.is_junk({"title": "ВАЗ-2114", "description": ""}))


class TestAgeCategories(SearchTestBase):
    def test_fresh_today_days3(self):
        for hours, expect in ((0.5, "fresh"), (5, "today"), (40, "days3")):
            lst = {"published_at": self.now - hours * 3600, "first_seen_at": self.now,
                   "listing_key": f"k{hours}", "title": "x", "description": ""}
            self.assertEqual(self.ps.category_of(lst, self.now), expect, hours)

    def test_old_without_bargain_reason_is_hidden(self):
        lst = {"published_at": self.now - 100 * 3600, "first_seen_at": self.now - 100 * 3600,
               "listing_key": "nope", "title": "ВАЗ-2114", "description": ""}
        self.assertEqual(self.ps.category_of(lst, self.now), "")

    def test_old_with_bargain_word_is_bargain(self):
        lst = {"published_at": self.now - 100 * 3600, "first_seen_at": self.now - 100 * 3600,
               "listing_key": "yes", "title": "ВАЗ-2114 торг", "description": ""}
        self.assertEqual(self.ps.category_of(lst, self.now), "bargain")

    def test_old_with_price_drop_is_bargain(self):
        key = self.ingest(price=100_000)
        self.ps.ingest_listing(self.item(price=80_000), now=self.now)
        lst = self.ps.get_pool_listing(key)
        lst["published_at"] = self.now - 100 * 3600
        self.assertEqual(self.ps.category_of(lst, self.now), "bargain")

    def test_age_label_falls_back_to_first_seen(self):
        lst = {"published_at": None, "first_seen_at": self.now - 3600,
               "listing_key": "k", "title": "x", "description": ""}
        self.assertIn("бот впервые увидел", self.ps.age_label(lst, self.now))

    def test_age_label_exact_when_published(self):
        lst = {"published_at": self.now - 18 * 60, "first_seen_at": self.now,
               "listing_key": "k", "title": "x", "description": ""}
        self.assertNotIn("впервые увидел", self.ps.age_label(lst, self.now))
        self.assertIn("18 минут назад", self.ps.age_label(lst, self.now))

    def test_published_ts_from_strings(self):
        """Площадки отдают время публикации текстом/ISO — оно должно парситься,
        иначе в карточке показывается момент, когда объявление увидел бот."""
        p = self.ps.parse_published_ts
        self.assertAlmostEqual(p("2 часа назад", self.now), self.now - 7200, delta=2)
        self.assertAlmostEqual(p("30 минут назад", self.now), self.now - 1800, delta=2)
        self.assertIsNotNone(p("Сегодня 12:30", self.now))
        self.assertIsNotNone(p("вчера в 20:29", self.now))
        self.assertIsNotNone(p("5 августа в 13:05", self.now))
        self.assertIsNotNone(p("2024-05-01T10:00:00Z", self.now + 0))
        self.assertIsNone(p("", self.now))
        self.assertIsNone(p(None, self.now))
        # миллисекунды и секунды одинаковы
        self.assertEqual(p(1_700_000_000_000, self.now), p(1_700_000_000, self.now))

    def test_ingest_keeps_publish_time_from_string(self):
        self.ps.ingest_listing({
            "source": "avito", "title": "Lada 2107, 2005", "url": "http://a/1",
            "_price_int": 100000, "published_at": "2 часа назад",
        }, now=self.now)
        row = self.ps.get_pool_listing(
            self.ps._pt.listing_key({"url": "http://a/1", "source": "avito"}))
        self.assertIsNotNone(row["published_at"])
        self.assertAlmostEqual(row["published_at"], self.now - 7200, delta=60)


    def test_unknown_publish_time_goes_to_today_not_fresh(self):
        """Без времени публикации объявление не «Кто быстрее»: возраст считается
        от первого показа, и раньше вся свежесобранная база валилась в fresh,
        а «Новые сегодня» оставался пустым."""
        lst = {"published_at": None, "first_seen_at": self.now - 120,
               "listing_key": "u1", "title": "x", "description": ""}
        self.assertEqual(self.ps.category_of(lst, self.now), "today")

    def test_unknown_publish_time_older_than_day_is_days3(self):
        lst = {"published_at": None, "first_seen_at": self.now - 30 * 3600,
               "listing_key": "u2", "title": "x", "description": ""}
        self.assertEqual(self.ps.category_of(lst, self.now), "days3")

    def test_known_publish_time_still_fresh(self):
        lst = {"published_at": self.now - 600, "first_seen_at": self.now,
               "listing_key": "u3", "title": "x", "description": ""}
        self.assertEqual(self.ps.category_of(lst, self.now), "fresh")


class TestPhotos(SearchTestBase):
    def test_photo_of_reads_every_scraper_key(self):
        self.assertEqual(self.ps.photo_of({"_photo_url": "http://a/1.jpg"}), "http://a/1.jpg")
        self.assertEqual(self.ps.photo_of({"image": "http://a/2.jpg"}), "http://a/2.jpg")
        self.assertEqual(self.ps.photo_of({"photos": ["http://a/3.jpg"]}), "http://a/3.jpg")
        self.assertEqual(self.ps.photo_of({"images": [{"640x480": "http://a/4.jpg"}]}),
                         "http://a/4.jpg")
        self.assertEqual(self.ps.photo_of({"photo": "/local/none.jpg"}), "")

    def test_photo_survives_repeat_ingest_without_photo(self):
        key = self.ingest(_photo_url="http://a/1.jpg")
        self.ps.ingest_listing(self.item(), now=self.now)   # повтор без фото
        self.assertEqual(self.ps.get_pool_listing(key)["photo"], "http://a/1.jpg")

    def test_set_pool_photo_saves_fetched_photo(self):
        key = self.ingest()
        self.ps.set_pool_photo(key, "http://a/9.jpg")
        self.assertEqual(self.ps.get_pool_listing(key)["photo"], "http://a/9.jpg")


class TestPagination(SearchTestBase):
    def test_category_total_counts_all_pages(self):
        self.ps.save_search(1, region="perm", price_max=200_000)
        for i in range(23):
            self.ps.ingest_listing(
                self.item(price=100_000 + i, url=f"https://avito.ru/car/{i}",
                          published_at=self.now - 600),
                now=self.now)
        total = self.ps.category_total(1, "fresh", self.now)
        self.assertEqual(total, 23)
        self.assertEqual(len(self.ps.search_listings(1, "fresh", now=self.now)), 10)
        self.assertEqual(
            len(self.ps.search_listings(1, "fresh", offset=20, now=self.now)), 3)


class TestSearchAndFilters(SearchTestBase):
    def test_single_active_search(self):
        self.ps.save_search(1, region="perm", price_max=100_000)
        self.ps.save_search(1, region="perm", price_max=200_000)
        s = self.ps.get_active_search(1)
        self.assertEqual(s["price_max"], 200_000)
        self.assertEqual(len(self.ps.all_active_searches()), 1)

    def test_filters(self):
        self.ps.save_search(1, region="perm", price_max=100_000, brands=["vaz"],
                            year_min=2005, condition="running")
        base = {"price": 90_000, "region": "perm", "brand": "vaz", "model": "vaz 2114",
                "year": 2008, "condition": "running", "title": "ВАЗ-2114", "source": "avito"}
        s = self.ps.get_active_search(1)
        self.assertTrue(self.ps.matches_search(base, s))
        self.assertFalse(self.ps.matches_search({**base, "price": 150_000}, s))
        self.assertFalse(self.ps.matches_search({**base, "region": "moskva"}, s))
        self.assertFalse(self.ps.matches_search({**base, "brand": "toyota"}, s))
        self.assertFalse(self.ps.matches_search({**base, "year": 2001}, s))
        self.assertFalse(self.ps.matches_search({**base, "condition": "repair"}, s))

    def test_region_name_and_slug_match(self):
        """Источники отдают регион и slug'ом, и человеческим именем."""
        self.ps.REGION_NAMES.update({"ekaterinburg": "Екатеринбург"})
        try:
            self.ps.save_search(20, region="ekaterinburg", price_max=200_000)
            key = self.ingest(price=100_000, region="Екатеринбург",
                              url="https://avito.ru/e/1", _published_ts=self.now - 600)
            lst = self.ps.get_pool_listing(key)
            self.assertEqual(lst["region"], "ekaterinburg")
            self.assertTrue(self.ps.matches_search(lst, self.ps.get_active_search(20)))
            self.assertEqual(self.ps.category_counts(20, self.now)["fresh"], 1)
        finally:
            self.ps.REGION_NAMES.clear()

    def test_no_price_limit_shows_expensive_cars(self):
        self.ps.save_search(21, region="perm", price_max=self.ps.NO_PRICE_LIMIT)
        self.ingest(price=4_300_000, url="https://avito.ru/exp/1",
                    title="Jeep Wrangler, 2020", _published_ts=self.now - 600)
        self.assertEqual(self.ps.category_counts(21, self.now)["fresh"], 1)

    def test_zero_price_max_means_no_limit(self):
        self.ps.save_search(22, region="perm", price_max=0)
        self.ingest(price=4_300_000, url="https://avito.ru/exp/2",
                    title="Jeep Wrangler, 2020", _published_ts=self.now - 600)
        self.assertEqual(self.ps.category_counts(22, self.now)["fresh"], 1)

    def test_listings_and_counts_use_pool_not_network(self):
        self.ps.save_search(7, region="perm", price_max=200_000)
        self.ingest(price=100_000, url="https://avito.ru/car/a", _published_ts=self.now - 600)
        self.ingest(price=110_000, url="https://avito.ru/car/b", _published_ts=self.now - 5 * 3600)
        counts = self.ps.category_counts(7, self.now)
        self.assertEqual(counts["fresh"], 1)
        self.assertEqual(counts["today"], 1)
        self.assertEqual(len(self.ps.search_listings(7, "fresh", now=self.now)), 1)

    def test_hidden_listing_excluded(self):
        self.ps.save_search(8, region="perm", price_max=200_000)
        key = self.ingest(price=100_000, _published_ts=self.now - 600)
        self.ps.hide_listing(8, key)
        self.assertEqual(self.ps.category_counts(8, self.now)["fresh"], 0)


class TestSaving(SearchTestBase):
    def test_save_enables_watch_and_is_idempotent(self):
        key = self.ingest(price=100_000)
        self.assertTrue(self.ps.save_car(5, key))
        self.assertFalse(self.ps.save_car(5, key))
        self.assertTrue(self.ps.is_saved(5, key))
        self.assertEqual(len(self.ps.saved_cars(5)), 1)
        self.assertEqual(len(self.pt.user_watches(5)), 1)
        self.assertEqual(self.pt.get_stats(5)["saved"], 1)

    def test_unsave_stops_watch(self):
        key = self.ingest(price=100_000)
        self.ps.save_car(5, key)
        self.ps.unsave_car(5, key)
        self.assertFalse(self.ps.is_saved(5, key))
        self.assertEqual(self.pt.user_watches(5), [])

    def test_record_interest_counts_open(self):
        key = self.ingest(price=100_000)
        self.ps.record_interest(9, key)
        self.assertEqual(self.pt.get_stats(9)["opened"], 1)

    def test_cheaper_similar(self):
        key = self.ingest(price=1_620_000, url="https://avito.ru/c/1",
                          title="Toyota Camry, 2016")
        self.ingest(price=1_520_000, url="https://avito.ru/c/2", title="Toyota Camry, 2016")
        cand = self.ps.cheaper_similar(3, key)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["gap"], 100_000)


class TestPriceDrop(SearchTestBase):
    def test_drop_appears_in_feed_once(self):
        self.ps.save_search(2, region="perm", price_max=200_000)
        key = self.ingest(price=100_000, _published_ts=self.now - 600)
        ev = self.ps.ingest_listing(self.item(price=85_000), now=self.now)
        self.assertEqual(ev["event"], "price_drop")
        feed = self.ps.price_drop_feed(2, now=self.now)
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["drop"], 15_000)
        self.assertEqual(feed[0]["listing_key"], key)
        self.assertEqual(self.ps.category_counts(2, self.now)["price_drop"], 1)

    def test_saved_car_drop_visible_without_matching_search(self):
        key = self.ingest(price=100_000)
        self.ps.save_car(4, key)
        self.ps.ingest_listing(self.item(price=85_000), now=self.now)
        self.assertEqual(len(self.ps.price_drop_feed(4, now=self.now)), 1)


class TestNotificationDedup(SearchTestBase):
    def test_one_event_one_message(self):
        key = self.ingest(price=100_000)
        sig = self.pt.drop_signature(key, 100_000, 85_000)
        self.assertTrue(self.ps.notify_once(1, "price_drop", key, sig))
        self.assertFalse(self.ps.notify_once(1, "price_drop", key, sig))

    def test_new_drop_is_new_event(self):
        key = self.ingest(price=100_000)
        self.assertTrue(self.ps.notify_once(
            1, "price_drop", key, self.pt.drop_signature(key, 100_000, 85_000)))
        self.assertTrue(self.ps.notify_once(
            1, "price_drop", key, self.pt.drop_signature(key, 85_000, 80_000)))

    def test_same_car_two_searches_one_message(self):
        key = self.ingest(price=100_000)
        sig = self.ps.new_listing_signature(key, 100_000)
        self.assertTrue(self.ps.notify_once(1, "new_listing", key, sig))
        self.assertFalse(self.ps.notify_once(1, "new_listing", key, sig))

    def test_quiet_hours(self):
        self.ps.set_pref(1, "quiet_from", 23)
        self.ps.set_pref(1, "quiet_to", 8)
        import datetime as _dt
        night = _dt.datetime(2026, 1, 1, 2, 0, tzinfo=self.ps.MSK).timestamp()
        day = _dt.datetime(2026, 1, 1, 12, 0, tzinfo=self.ps.MSK).timestamp()
        self.assertTrue(self.ps.in_quiet_hours(1, night))
        self.assertFalse(self.ps.in_quiet_hours(1, day))


class TestMarketPrice(SearchTestBase):
    def test_median_of_similar(self):
        for i, p in enumerate((100_000, 105_000, 110_000, 115_000, 120_000)):
            self.ingest(price=p, url=f"https://avito.ru/m/{i}", title="ВАЗ-2114, 2008")
        res = self.ps.avito_market_price("vaz", "vaz 2114", 2008, "perm")
        self.assertEqual(res["price"], 110_000)
        self.assertEqual(res["sample"], 5)
        self.assertFalse(res["preliminary"])

    def test_junk_excluded(self):
        for i, p in enumerate((100_000, 105_000, 110_000, 115_000, 120_000)):
            self.ingest(price=p, url=f"https://avito.ru/m/{i}", title="ВАЗ-2114, 2008")
        self.ingest(price=15_000, url="https://avito.ru/m/junk",
                    title="ВАЗ-2114, 2008", description="на запчасти, без документов")
        res = self.ps.avito_market_price("vaz", "vaz 2114", 2008, "perm")
        self.assertEqual(res["price"], 110_000)

    def test_small_sample_is_preliminary(self):
        self.ingest(price=100_000, url="https://avito.ru/m/1", title="ВАЗ-2114, 2008")
        res = self.ps.avito_market_price("vaz", "vaz 2114", 2008, "perm")
        self.assertTrue(res["preliminary"])

    def test_refresh_writes_to_pool(self):
        for i, p in enumerate((100_000, 105_000, 110_000, 115_000, 120_000)):
            self.ingest(price=p, url=f"https://avito.ru/m/{i}", title="ВАЗ-2114, 2008")
        key = self.pt.listing_key(self.item(url="https://avito.ru/m/0"))
        self.ps.refresh_market_price(key)
        self.assertEqual(self.ps.get_pool_listing(key)["market_price"], 110_000)


class TestCardAndSummary(SearchTestBase):
    def test_card_has_no_dealscore(self):
        key = self.ingest(price=85_000, _published_ts=self.now - 18 * 60)
        lst = self.ps.get_pool_listing(key)
        lst["market_price"] = 108_000
        lst["market_sample"] = 7
        card = self.ps.format_card(lst, now=self.now)
        self.assertNotIn("DealScore", card)
        self.assertIn("18 минут назад", card)
        self.assertIn("Рынок Авито", card)
        self.assertIn("Почему показали", card)

    def test_summary_lists_all_sections(self):
        self.ps.save_search(6, region="perm", price_max=100_000)
        text = self.ps.format_summary(6, self.now)
        for part in ("Кто быстрее", "Новые сегодня", "До 3 дней", "Снизили цену",
                     "Простор для торга", "Сохранённые"):
            self.assertIn(part, text)


class TestReports(SearchTestBase):
    def test_morning_report_only_with_events(self):
        self.ps.save_search(10, region="perm", price_max=200_000)
        self.assertIsNone(self.ps.morning_report(10, self.now))
        self.ingest(price=100_000, _published_ts=self.now - 600)
        rep = self.ps.morning_report(10, self.now)
        self.assertIsNotNone(rep)
        self.assertEqual(rep["new"], 1)
        self.assertIn("За ночь", rep["text"])

    def test_morning_report_counts_drops(self):
        self.ps.save_search(11, region="perm", price_max=200_000)
        self.ingest(price=100_000, _published_ts=self.now - 600)
        self.ps.ingest_listing(self.item(price=85_000), now=self.now)
        rep = self.ps.morning_report(11, self.now)
        self.assertEqual(rep["drops"], 1)

    def test_evening_report_is_personal(self):
        self.ps.save_search(12, region="perm", price_max=200_000)
        key = self.ingest(price=100_000, _published_ts=self.now - 600)
        self.ps.record_interest(12, key)
        self.ps.save_car(12, key)
        rep = self.ps.evening_report(12, self.now)
        self.assertEqual(rep["opened"], 1)
        self.assertEqual(rep["saved"], 1)
        self.assertEqual(rep["matched"], 1)
        self.assertIn("Итоги дня", rep["text"])

    def test_evening_report_none_without_activity(self):
        self.ps.save_search(13, region="perm", price_max=200_000)
        self.assertIsNone(self.ps.evening_report(13, self.now))


class TestNotificationTexts(SearchTestBase):
    def test_texts(self):
        key = self.ingest(price=85_000, _published_ts=self.now - 8 * 60)
        lst = self.ps.get_pool_listing(key)
        lst["market_price"] = 108_000
        self.assertIn("Новое подходящее объявление",
                      self.ps.format_new_listing_notification(lst, self.now))
        drop = self.ps.format_price_drop_notification(lst, 890_000, 820_000,
                                                      saved_at=self.now - 6 * 86400,
                                                      drops_count=2, now=self.now)
        self.assertIn("Снижение: 70 000 ₽", drop)
        self.assertIn("6 дней назад", drop)
        self.assertIn("снова появилась", self.ps.format_relisted_notification(lst, 35_000))
        self.assertIn("похожий вариант дешевле", self.ps.format_cheaper_similar_notification(
            {"saved_title": "Camry", "saved_price": 1_620_000, "price": 1_520_000,
             "gap": 100_000}))


class TestEndToEnd(SearchTestBase):
    def test_full_path(self):
        """Поиск → категории → открыл и сохранил → снижение → ровно одно
        уведомление → раздел «Снизили цену» → персональный отчёт дня."""
        uid = 42
        self.ps.save_search(uid, region="perm", price_max=200_000, brands=["vaz"])

        key = self.ingest(price=100_000, _published_ts=self.now - 600)
        self.assertEqual(self.ps.category_counts(uid, self.now)["fresh"], 1)
        self.assertEqual(len(self.ps.search_listings(uid, "fresh", now=self.now)), 1)

        self.ps.record_interest(uid, key)
        self.assertTrue(self.ps.save_car(uid, key))

        ev = self.ps.ingest_listing(self.item(price=85_000), now=self.now)
        self.assertEqual(ev["event"], "price_drop")

        sig = self.pt.drop_signature(key, 100_000, 85_000)
        sent = [i for i in range(3)
                if self.ps.notify_once(uid, "price_drop", key, sig)]
        self.assertEqual(len(sent), 1, "должно уйти ровно одно уведомление")

        feed = self.ps.price_drop_feed(uid, now=self.now)
        self.assertEqual([f["listing_key"] for f in feed], [key])

        rep = self.ps.evening_report(uid, self.now)
        self.assertEqual(rep["opened"], 1)
        self.assertEqual(rep["saved"], 1)
        self.assertGreaterEqual(rep["matched"], 1)


if __name__ == "__main__":
    unittest.main()
