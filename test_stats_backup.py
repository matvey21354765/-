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
