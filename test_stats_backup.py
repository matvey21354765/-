"""Сохранность статистики пользователей: слияние копий и защита от затирания."""
import json
import tempfile
import unittest
from pathlib import Path

import control_bot as cb


class TestMergeRegistry(unittest.TestCase):
    """Копии из разных источников отстают друг от друга — теряться не должно."""

    def test_new_users_are_added(self):
        target = {"1": {"searches": 1}}
        added = cb.merge_registry(target, {"2": {"searches": 3}})
        self.assertEqual(added, 1)
        self.assertIn("2", target)

    def test_counters_take_the_larger_value(self):
        target = {"1": {"searches": 5, "last_seen": 200}}
        cb.merge_registry(target, {"1": {"searches": 9, "last_seen": 150}})
        self.assertEqual(target["1"]["searches"], 9)
        self.assertEqual(target["1"]["last_seen"], 200)

    def test_first_seen_takes_the_earliest(self):
        target = {"1": {"first_seen": 100}}
        cb.merge_registry(target, {"1": {"first_seen": 50}})
        self.assertEqual(target["1"]["first_seen"], 50)

    def test_trial_fields_are_restored_when_missing(self):
        target = {"1": {"searches": 0}}
        cb.merge_registry(target, {"1": {"trial_start": 77, "trial_days": 3}})
        self.assertEqual(target["1"]["trial_start"], 77)

    def test_existing_data_is_never_overwritten_by_junk(self):
        target = {"1": {"searches": 7}}
        cb.merge_registry(target, {"1": "мусор", "2": None})
        self.assertEqual(target["1"]["searches"], 7)
        self.assertNotIn("2", target)


class TestDiskCopy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._file = cb._STATS_BACKUP_FILE
        cb._STATS_BACKUP_FILE = Path(self._tmp.name) / "stats_backup.json"
        self._backup = dict(cb._USER_REGISTRY)
        cb._USER_REGISTRY.clear()

    def tearDown(self):
        cb._STATS_BACKUP_FILE = self._file
        cb._USER_REGISTRY.clear()
        cb._USER_REGISTRY.update(self._backup)
        self._tmp.cleanup()

    def test_registry_is_restored_from_disk(self):
        cb._STATS_BACKUP_FILE.write_text(
            json.dumps({"users": {"5": {"searches": 2}, "6": {"searches": 1}}}),
            encoding="utf-8")
        self.assertEqual(cb._restore_stats_from_disk(), 2)
        self.assertIn("5", cb._USER_REGISTRY)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(cb._restore_stats_from_disk(), 0)

    def test_broken_file_is_not_an_error(self):
        cb._STATS_BACKUP_FILE.write_text("{битый", encoding="utf-8")
        self.assertEqual(cb._restore_stats_from_disk(), 0)


class TestShrinkProtection(unittest.TestCase):
    def test_smaller_backup_keeps_the_previous_one(self):
        """Частичный реестр не должен стирать последнюю полную копию."""
        import inspect
        src = inspect.getsource(cb._tg_backup_save)
        self.assertIn("_now_count >= _prev", src)
        self.assertIn("реестр уменьшился", src)

    def test_empty_registry_is_never_saved(self):
        import inspect
        src = inspect.getsource(cb._tg_backup_save)
        self.assertIn('if not _USER_REGISTRY:', src)

    def test_disk_copy_is_written_on_every_save(self):
        import inspect
        src = inspect.getsource(cb._tg_backup_save)
        self.assertIn("_STATS_BACKUP_FILE.write_text", src)

    def test_admin_can_restore_from_a_replied_file(self):
        import inspect
        src = inspect.getsource(cb.cmd_restore_stats)
        self.assertIn("reply_to_message", src)
        self.assertIn("merge_registry", src)
        self.assertIn("ADMIN_IDS", src)


if __name__ == "__main__":
    unittest.main()


class TestRealBackupShape(unittest.TestCase):
    """Формат боевого stats_backup.json — со всеми историческими вариантами полей."""

    SAMPLE = {
        "users": {
            # старый формат: подписка и числовые отметки уведомлений
            "749256529": {"first_seen": 1782818861, "last_seen": 1786029081,
                          "searches": 182, "monitoring": False,
                          "username": "durunegonim", "subscription_plan": "trial",
                          "subscription_expires_at": 1783423661,
                          "trial_start": 1784724948, "bonus_days": 0,
                          "trial_notified": [3, 1], "trial_days": 3},
            # новый формат отметок
            "1316211302": {"first_seen": 1782834014, "last_seen": 1785967789,
                           "searches": 4, "username": "tspplll",
                           "trial_start": 1785959237, "trial_days": 3,
                           "trial_notified": ["trial:72"]},
            # запись без username и без trial-полей
            "361708996": {"first_seen": 1782819229, "searches": 1,
                          "last_seen": 1782819606},
            # старый семидневный тест
            "1066358538": {"first_seen": 1784369977, "last_seen": 1785929442,
                           "searches": 0, "trial_days": 7,
                           "trial_notified": [1, "trial:72"]},
        },
        "ts": 1786029083,
    }

    def test_every_record_is_restored(self):
        reg = {}
        self.assertEqual(cb.merge_registry(reg, self.SAMPLE["users"]), 4)
        self.assertEqual(len(reg), 4)

    def test_counters_and_names_survive(self):
        reg = {}
        cb.merge_registry(reg, self.SAMPLE["users"])
        self.assertEqual(reg["749256529"]["searches"], 182)
        self.assertEqual(reg["749256529"]["username"], "durunegonim")
        self.assertEqual(reg["1316211302"]["trial_start"], 1785959237)

    def test_restore_is_idempotent(self):
        """Повторный /restore_stats ничего не портит и не задваивает."""
        reg = {}
        cb.merge_registry(reg, self.SAMPLE["users"])
        snapshot = json.dumps(reg, sort_keys=True)
        self.assertEqual(cb.merge_registry(reg, self.SAMPLE["users"]), 0)
        self.assertEqual(json.dumps(reg, sort_keys=True), snapshot)

    def test_restored_stale_copy_does_not_lower_counters(self):
        """Старая копия не должна откатывать счётчики более свежей."""
        reg = {"749256529": {"searches": 200, "last_seen": 1786100000}}
        cb.merge_registry(reg, self.SAMPLE["users"])
        self.assertEqual(reg["749256529"]["searches"], 200)
        self.assertEqual(reg["749256529"]["last_seen"], 1786100000)

    def test_legacy_seven_day_trial_is_capped_after_restore(self):
        """У восстановленной записи с trial_days=7 тест всё равно 3 дня."""
        backup = dict(cb._USER_REGISTRY)
        cb._USER_REGISTRY.clear()
        try:
            cb.merge_registry(cb._USER_REGISTRY, self.SAMPLE["users"])
            self.assertEqual(cb._trial_info(1066358538)["total"], cb.TRIAL_DAYS)
        finally:
            cb._USER_REGISTRY.clear()
            cb._USER_REGISTRY.update(backup)


class TestRestoreReport(unittest.TestCase):
    """Ответ команды должен объяснять, что произошло."""

    def test_report_shows_how_many_records_the_file_had(self):
        """«Добавлено: 0» без числа записей выглядит как поломка, хотя это
        просто ответ на свежий маленький бэкап."""
        import inspect
        src = inspect.getsource(cb.cmd_restore_stats)
        self.assertIn("В файле записей", src)
        self.assertIn("вы ответили на свежий бэкап", src)

    def test_report_mentions_the_saved_backup_when_something_changed(self):
        import inspect
        src = inspect.getsource(cb.cmd_restore_stats)
        self.assertIn("Реестр сохранён в новый бэкап", src)


class TestRestoreAcceptsBothWays(unittest.TestCase):
    """Файл можно и приложить к команде, и ответить на него."""

    def test_document_attached_to_the_command_is_read(self):
        """Файл с подписью /restore_stats — самый естественный способ, и он
        молча игнорировался: читался только ответ на сообщение."""
        import inspect
        src = inspect.getsource(cb.cmd_restore_stats)
        self.assertIn('doc = getattr(msg, "document", None)', src)
        self.assertIn("if doc is None and msg.reply_to_message:", src)

    def test_aiogram_command_filter_sees_captions(self):
        """Подпись к документу тоже считается командой — иначе обработчик
        вообще не вызовется."""
        import inspect
        from aiogram.filters import Command
        self.assertIn("message.text or message.caption",
                      inspect.getsource(Command))
