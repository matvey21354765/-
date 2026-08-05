"""Статические проверки главного меню и пробного периода.

control_bot.py импортировать в тестах дорого (сеть/прокси на старте), поэтому
меню и обработчики проверяются разбором исходника (AST + текстовый поиск).
"""
import ast
import re
import unittest
from pathlib import Path

SRC_PATH = Path(__file__).with_name("control_bot.py")
SRC = SRC_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SRC)

NEW_MENU = [
    "🔍 Найти авто", "🚨 Кто быстрее", "🔥 Новые сегодня", "📉 Снизили цену",
    "🤝 Простор для торга", "⭐ Сохранённые", "⚡ Мониторинг", "📊 Сегодня",
    "💎 Подписка", "🤝 Пригласить друга", "⚙️ Настройки",
]

REMOVED_FROM_MENU = [
    "🌐 Глобальный поиск", "🎯 Следить за маркой", "🚗 Мой гараж",
    "💼 Мои сделки", "❓ Помощь", "♻️ Сбросить историю", "🔔 Уведомления",
]


def _menu_rows() -> list[list[str]]:
    """Тексты кнопок из литерала _MAIN_ROWS."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_MAIN_ROWS" for t in node.targets):
            rows = []
            for row in node.value.elts:
                texts = []
                for call in row.elts:
                    for kw in call.keywords:
                        if kw.arg == "text":
                            texts.append(kw.value.value)
                rows.append(texts)
            return rows
    raise AssertionError("_MAIN_ROWS не найден")


def _handled_texts() -> set[str]:
    """Все строки, на которые есть message-обработчик по F.text."""
    out = set()
    out.update(re.findall(r'F\.text\s*==\s*"([^"]+)"', SRC))
    for block in re.findall(r"F\.text\.in_\(\{([^}]+)\}\)", SRC):
        out.update(re.findall(r'"([^"]+)"', block))
    return out


def _callback_data_in_keyboards() -> set[str]:
    return set(re.findall(r'callback_data=f?"([^"{]+)', SRC))


def _callback_handlers() -> tuple[set[str], set[str]]:
    """(точные F.data ==, префиксы F.data.startswith)."""
    exact = set(re.findall(r'F\.data\s*==\s*"([^"]+)"', SRC))
    prefixes = set(re.findall(r'F\.data\.startswith\("([^"]+)"\)', SRC))
    return exact, prefixes


class TestMainMenu(unittest.TestCase):
    def test_menu_contains_exactly_new_buttons(self):
        flat = [t for row in _menu_rows() for t in row]
        self.assertEqual(flat, NEW_MENU)

    def test_removed_buttons_not_in_menu(self):
        flat = [t for row in _menu_rows() for t in row]
        for btn in REMOVED_FROM_MENU:
            self.assertNotIn(btn, flat, btn)

    def test_every_menu_button_has_handler(self):
        handled = _handled_texts()
        for btn in NEW_MENU:
            self.assertIn(btn, handled, f"нет обработчика для кнопки {btn}")

    def test_old_buttons_still_handled_as_aliases(self):
        """Старые сообщения и клавиатуры не должны ломаться."""
        handled = _handled_texts()
        for btn in REMOVED_FROM_MENU:
            self.assertIn(btn, handled, f"потерян алиас {btn}")

    def test_old_callback_data_still_handled(self):
        exact, prefixes = _callback_handlers()
        legacy = ["do_search", "reset_seen", "reset_and_search", "open_subscribe",
                  "open_monitor", "ref_stats", "ref_terms", "ref_back", "promo_help",
                  "notify_toggle", "notify_settings", "deal_add"]
        for cb in legacy:
            self.assertTrue(cb in exact or any(cb.startswith(p) for p in prefixes),
                            f"потерян старый callback_data {cb}")

    def test_no_button_without_callback_handler(self):
        exact, prefixes = _callback_handlers()
        skip_prefixes = ("http", "region|", "brand|", "cat|", "price|", "src|")
        for cd in _callback_data_in_keyboards():
            if cd.startswith(skip_prefixes) or not cd:
                continue
            ok = cd in exact or any(cd.startswith(p) for p in prefixes)
            self.assertTrue(ok, f"кнопка без обработчика: {cd}")

    def test_new_sections_do_not_start_network_search(self):
        """Разделы читают общий пул, а не запускают do_search_for_user."""
        i = SRC.index("async def _ps_send_category")
        j = SRC.index("async def _ps_new_listing_loop")
        body = SRC[i:j]
        self.assertNotIn("do_search_for_user", body)
        self.assertIn("_ps.search_listings", body)


class TestTrialPeriod(unittest.TestCase):
    def test_single_trial_setting_is_three_days(self):
        self.assertIn('os.getenv("TRIAL_DAYS", "3")', SRC)
        self.assertIsNone(re.search(r"(?<!LEGACY_)TRIAL_DAYS\s*=\s*7\b", SRC))

    def test_no_seven_days_trial_text(self):
        self.assertNotIn("Осталось 7 из 7 дней", SRC)
        self.assertNotIn("7 дней бесплатно", SRC)

    def test_trial_days_stored_per_user(self):
        self.assertIn('u["trial_days"]', SRC)
        self.assertIn("LEGACY_TRIAL_DAYS", SRC)

    def test_referral_reward_untouched(self):
        ref = Path(__file__).with_name("referrals.py").read_text(encoding="utf-8")
        # Награда за друга и тарифы не должны зависеть от TRIAL_DAYS.
        self.assertNotIn("TRIAL_DAYS", ref)
        self.assertIn("друг выбрал 7 дней — тебе +2 дня", ref)


if __name__ == "__main__":
    unittest.main()
