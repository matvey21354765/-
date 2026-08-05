"""Мастер поиска: шаг выбора площадок и время, отведённое на обход."""
import unittest

import control_bot as cb


class TestSourcesStep(unittest.TestCase):
    """Раньше мастер молча брал все площадки и писал «Все доступные»."""

    def test_wizard_has_a_sources_step(self):
        import inspect
        self.assertTrue(hasattr(cb.Setup, "sources"))
        self.assertIn("_setup_ask_sources", inspect.getsource(cb.cb_setup_condition))

    def test_summary_reflects_the_choice(self):
        # Состав ALL_SOURCES зависит от провайдера Авито, поэтому берём
        # первые две площадки, какими бы они ни были.
        first, second = cb.ALL_SOURCES[0], cb.ALL_SOURCES[1]
        other = cb.ALL_SOURCES[-1]
        self.assertEqual(cb._sources_summary([]), "Все доступные площадки")
        self.assertEqual(cb._sources_summary(list(cb.ALL_SOURCES)),
                         "Все доступные площадки")
        text = cb._sources_summary([first, second])
        self.assertTrue(text.startswith("Площадки:"))
        self.assertIn(cb.SOURCE_NAMES[first], text)
        self.assertIn(cb.SOURCE_NAMES[second], text)
        self.assertNotIn(cb.SOURCE_NAMES[other], text)

    def test_keyboard_marks_selected_platforms(self):
        picked = cb.ALL_SOURCES[0]
        labels = [b.text for row in cb._setup_sources_keyboard([picked]).inline_keyboard
                  for b in row]
        self.assertTrue(any(x.startswith("✅") and cb.SOURCE_NAMES[picked] in x
                            for x in labels), labels)
        self.assertTrue(any(x.startswith("☐") for x in labels), labels)
        self.assertTrue(any("Готово" in x for x in labels), labels)

    def test_empty_choice_means_every_platform(self):
        # Последняя строка — служебные кнопки, площадки только выше неё.
        rows = cb._setup_sources_keyboard([]).inline_keyboard[:-1]
        labels = [b.text for row in rows for b in row]
        self.assertEqual(sum(1 for x in labels if x.startswith("✅")),
                         len([s for s in cb.ALL_SOURCES if s in cb.SOURCE_NAMES]))

    def test_confirmation_screen_offers_to_change_sources(self):
        import inspect
        self.assertIn("sw_edit_sources", inspect.getsource(cb._setup_ask_confirm))

    def test_chosen_platforms_are_saved_to_settings(self):
        import inspect
        src = inspect.getsource(cb.cb_setup_start)
        self.assertIn('settings["sources"]', src)


class TestSearchTime(unittest.TestCase):
    def test_source_timeout_exceeds_the_avito_budget(self):
        """Таймаут обязан быть больше бюджета обхода: иначе поиск обрывает
        уже собранные объявления и показывает «Avito: 0»."""
        budget = cb._avito_budget_sec()
        self.assertGreaterEqual(budget, 120)
        self.assertGreater(max(90, budget + 40), budget)

    def test_user_is_warned_the_search_is_slow(self):
        import inspect
        self.assertIn("до 2 минут", inspect.getsource(cb.do_search_for_user))


if __name__ == "__main__":
    unittest.main()
