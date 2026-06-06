from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton as Btn

POLY_URL = "https://polymarket.com/markets/crypto?via=max-chron0n"


def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🚀 Запустить PredictBot", callback_data="open_menu")],
    ])


def main_menu(notifications: bool = False) -> InlineKeyboardMarkup:
    notif_text = "🔔 Уведомления: ВКЛ" if notifications else "🔕 Уведомления: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🌐 Обзор рынка", callback_data="overview")],
        [Btn(text="₿ BTC", callback_data="sig_BTC"),
         Btn(text="Ξ ETH", callback_data="sig_ETH"),
         Btn(text="◎ SOL", callback_data="sig_SOL")],
        [Btn(text="📊 Статистика", callback_data="stats_menu"),
         Btn(text="📋 История", callback_data="hist_ALL_0")],
        [Btn(text="🎯 Polymarket", url=POLY_URL)],
        [Btn(text="💳 Подписка", callback_data="subscription"),
         Btn(text=notif_text, callback_data="toggle_notifications")],
    ])


def signal_kb(coin: str, sig_id: int = None) -> InlineKeyboardMarkup:
    rows = []
    if sig_id:
        rows.append([Btn(text="📝 Полный анализ", callback_data=f"full_{sig_id}")])
    rows.append([
        Btn(text="🔄 Свежий анализ", callback_data=f"refresh_{coin}"),
        Btn(text="📋 История", callback_data=f"hist_{coin}_0"),
    ])
    rows.append([Btn(text="« Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def full_analysis_kb(coin: str, sig_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🔄 Новый сигнал", callback_data=f"sig_{coin}"),
         Btn(text="📋 История", callback_data=f"hist_{coin}_0")],
        [Btn(text="« Назад к сигналу", callback_data=f"back_sig_{sig_id}")],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


def stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="7д", callback_data="stats_7d"),
         Btn(text="30д", callback_data="stats_30d"),
         Btn(text="90д", callback_data="stats_90d"),
         Btn(text="Всё", callback_data="stats_all")],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


def history_kb(coin: str, page: int, has_next: bool) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(Btn(text="← Назад", callback_data=f"hist_{coin}_{page - 1}"))
    if has_next:
        nav.append(Btn(text="Вперёд →", callback_data=f"hist_{coin}_{page + 1}"))
    rows = [
        [Btn(text="BTC", callback_data="hist_BTC_0"),
         Btn(text="ETH", callback_data="hist_ETH_0"),
         Btn(text="SOL", callback_data="hist_SOL_0"),
         Btn(text="Все", callback_data="hist_ALL_0")],
    ]
    if nav:
        rows.append(nav)
    rows.append([Btn(text="« Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def overview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="₿ BTC", callback_data="sig_BTC"),
         Btn(text="Ξ ETH", callback_data="sig_ETH"),
         Btn(text="◎ SOL", callback_data="sig_SOL")],
        [Btn(text="🎯 Polymarket", url=POLY_URL)],
        [Btn(text="🔄 Обновить обзор", callback_data="overview")],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


def subscription_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🎁 Ввести промокод", callback_data="enter_promo")],
        [Btn(text="💬 Написать @nn0likkkkk", url="https://t.me/nn0likkkkk")],
        [Btn(text="💬 Написать @n3m1r", url="https://t.me/n3m1r")],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])



def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="« Главное меню", callback_data="main_menu")]
    ])
