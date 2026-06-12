"""
ФинГрам-бот — бот по финансовой грамотности
Целевая аудитория: молодёжь 16–25 лет
Креативная механика: Финансовый IQ (очки за квизы и активность)
"""

import asyncio
import logging
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("FINBOT_TOKEN", "8496208426:AAGaQbb6HaqrEI-ONZGUpkKeU7LJAhNloLo")

router = Router()

# In-memory хранилище профилей пользователей
# {user_id: {"iq": int, "quizzes": int, "badges": list, "budget_used": bool}}
_profiles: dict = {}


def get_profile(uid: int) -> dict:
    if uid not in _profiles:
        _profiles[uid] = {"iq": 0, "quizzes": 0, "badges": [], "budget_used": False}
    return _profiles[uid]


def add_iq(uid: int, points: int, badge: str = None):
    p = get_profile(uid)
    p["iq"] += points
    if badge and badge not in p["badges"]:
        p["badges"].append(badge)


# ─── FSM ───────────────────────────────────────────────────────────────────────

class QuizState(StatesGroup):
    answering = State()


class BudgetState(StatesGroup):
    income = State()
    expenses = State()


# ─── КВИЗ ──────────────────────────────────────────────────────────────────────

QUIZ = [
    {
        "q": "Что такое инфляция?",
        "opts": ["Рост цен на товары и услуги", "Снижение налогов", "Повышение зарплат", "Курс доллара"],
        "ans": 0,
    },
    {
        "q": "Сколько рекомендуется иметь в «финансовой подушке безопасности»?",
        "opts": ["1 месячный расход", "3–6 месячных расходов", "10 месячных расходов", "Год расходов"],
        "ans": 1,
    },
    {
        "q": "Что такое диверсификация?",
        "opts": ["Вложение всего в один актив", "Распределение рисков по разным активам", "Снятие вклада досрочно", "Кредит в банке"],
        "ans": 1,
    },
    {
        "q": "Что такое ОФЗ?",
        "opts": ["Облигации федерального займа", "Открытый финансовый заём", "Обязательный фонд защиты", "Операция с фьючерсами"],
        "ans": 0,
    },
    {
        "q": "Правило «50/30/20» в бюджетировании означает:",
        "opts": [
            "50% — еда, 30% — жильё, 20% — развлечения",
            "50% — нужды, 30% — желания, 20% — накопления",
            "50% — налоги, 30% — кредиты, 20% — остаток",
            "50% — инвестиции, 30% — депозит, 20% — наличные",
        ],
        "ans": 1,
    },
    {
        "q": "Что такое сложный процент?",
        "opts": [
            "Процент только на начальную сумму",
            "Процент на сумму + накопленные проценты",
            "Штраф за просрочку кредита",
            "Комиссия банка",
        ],
        "ans": 1,
    },
    {
        "q": "Какой инструмент считается наименее рискованным?",
        "opts": ["Акции стартапов", "Криптовалюта", "Государственные облигации", "Форекс"],
        "ans": 2,
    },
    {
        "q": "ETF — это:",
        "opts": [
            "Электронный перевод денег",
            "Биржевой инвестиционный фонд",
            "Европейский торговый фонд",
            "Вид банковского счёта",
        ],
        "ans": 1,
    },
    {
        "q": "Что такое ликвидность актива?",
        "opts": [
            "Доходность за год",
            "Скорость превращения актива в деньги",
            "Налог при продаже",
            "Стоимость покупки",
        ],
        "ans": 1,
    },
    {
        "q": "Какой процент годовых от инфляции считается «реальной» доходностью?",
        "opts": [
            "Номинальная доходность",
            "Номинальная доходность минус инфляция",
            "Инфляция плюс ставка ЦБ",
            "Ключевая ставка ЦБ",
        ],
        "ans": 1,
    },
]

IQ_PER_CORRECT = 15
IQ_PER_QUIZ_FINISH = 50


# ─── СЛОВАРЬ ────────────────────────────────────────────────────────────────────

GLOSSARY = {
    "Акция": "Ценная бумага, дающая право на долю в компании и часть прибыли (дивиденды).",
    "Облигация": "Долговая ценная бумага: компания/государство берёт у вас деньги в долг под процент.",
    "ETF": "Биржевой фонд — корзина активов (акций, облигаций), торгуется как одна акция.",
    "Дивиденды": "Часть прибыли компании, выплачиваемая акционерам.",
    "Инфляция": "Рост общего уровня цен в экономике, снижающий покупательную способность денег.",
    "Диверсификация": "Распределение вложений по разным активам для снижения риска.",
    "Ликвидность": "Способность актива быстро превращаться в деньги без потери стоимости.",
    "ОФЗ": "Облигации Федерального Займа — государственные долговые бумаги России.",
    "Ключевая ставка": "Ставка Центробанка, влияющая на кредиты и вклады в стране.",
    "Подушка безопасности": "Резервный фонд на 3–6 месяцев расходов на случай непредвиденных ситуаций.",
    "Сложный процент": "Процент начисляется не только на основную сумму, но и на уже накопленные проценты.",
    "Портфель": "Совокупность всех инвестиционных активов инвестора.",
}

TIPS = [
    ("🎯 Правило 50/30/20", "50% дохода — на необходимое (еда, жильё, транспорт)\n30% — на желания (развлечения, кафе)\n20% — на накопления и инвестиции"),
    ("☕ Эффект латте", "Ежедневный кофе за 200₽ = 73 000₽ в год. Маленькие траты складываются в большие суммы — отслеживай их!"),
    ("📦 Конверт-метод", "Раздели деньги по конвертам (или счетам) по категориям в начале месяца. Потратил конверт — стоп."),
    ("📈 Инвестируй сразу", "Откладывай % от дохода ДО того, как начнёшь тратить — «заплати сначала себе»."),
    ("🛡 Подушка прежде всего", "Начни инвестировать только после того, как накопил 3–6 месячных расходов в резерве."),
    ("💳 Кредитная карта — не деньги", "Кредитка — это долг. Используй льготный период и гаси полностью каждый месяц."),
    ("🔄 Автоплатёж на вклад", "Настрой автоперевод на накопительный счёт в день получения зарплаты — не оставляй на «потом»."),
]


# ─── КЛАВИАТУРЫ ────────────────────────────────────────────────────────────────

def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🧠 Квиз: проверь знания", callback_data="quiz_start")],
        [Btn(text="💰 Калькулятор бюджета",  callback_data="budget_start")],
        [Btn(text="💡 Советы по экономии",    callback_data="tips_menu")],
        [Btn(text="📖 Словарь инвестора",     callback_data="glossary_menu")],
        [Btn(text="👤 Мой Финансовый IQ",     callback_data="profile")],
    ])


def tips_kb() -> InlineKeyboardMarkup:
    rows = [[Btn(text=title, callback_data=f"tip_{i}")] for i, (title, _) in enumerate(TIPS)]
    rows.append([Btn(text="« Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def glossary_kb() -> InlineKeyboardMarkup:
    terms = list(GLOSSARY.keys())
    rows = []
    for i in range(0, len(terms), 2):
        row = [Btn(text=terms[i], callback_data=f"term_{i}")]
        if i + 1 < len(terms):
            row.append(Btn(text=terms[i + 1], callback_data=f"term_{i+1}"))
        rows.append(row)
    rows.append([Btn(text="« Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[Btn(text="« Главное меню", callback_data="main_menu")]])


def quiz_kb(q_idx: int) -> InlineKeyboardMarkup:
    opts = QUIZ[q_idx]["opts"]
    rows = [[Btn(text=opt, callback_data=f"qa_{q_idx}_{i}")] for i, opt in enumerate(opts)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── IQ BADGE ──────────────────────────────────────────────────────────────────

def iq_level(iq: int) -> str:
    if iq < 50:   return "🥉 Новичок"
    if iq < 150:  return "🥈 Студент"
    if iq < 300:  return "🥇 Знаток"
    if iq < 500:  return "💎 Эксперт"
    return "🏆 Финансовый гуру"


# ─── ХЭНДЛЕРЫ ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    get_profile(msg.from_user.id)
    name = msg.from_user.first_name or "друг"
    await msg.answer(
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Я — <b>ФинГрам</b>, твой личный тренер по финансовой грамотности 💸\n\n"
        "Здесь ты сможешь:\n"
        "  🧠 Проверить знания в квизе\n"
        "  💰 Рассчитать личный бюджет\n"
        "  💡 Получить советы по экономии\n"
        "  📖 Узнать термины инвестора\n\n"
        "За каждый правильный ответ и действие ты получаешь <b>Финансовый IQ</b> — "
        "прокачивай его и стань финансовым гуру! 🏆\n\n"
        "Выбери раздел 👇",
        reply_markup=main_kb(), parse_mode="HTML"
    )


@router.callback_query(F.data == "main_menu")
async def cb_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыбери раздел 👇",
        reply_markup=main_kb(), parse_mode="HTML"
    )
    await call.answer()


# ─── ПРОФИЛЬ ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    p = get_profile(call.from_user.id)
    iq = p["iq"]
    level = iq_level(iq)
    badges_str = " ".join(p["badges"]) if p["badges"] else "пока нет"
    next_level_map = [
        (50, "🥈 Студент"),
        (150, "🥇 Знаток"),
        (300, "💎 Эксперт"),
        (500, "🏆 Финансовый гуру"),
    ]
    next_info = ""
    for threshold, name in next_level_map:
        if iq < threshold:
            next_info = f"\nДо уровня <b>{name}</b>: {threshold - iq} IQ"
            break

    await call.message.edit_text(
        f"👤 <b>Мой Финансовый IQ</b>\n\n"
        f"🎓 Уровень: <b>{level}</b>\n"
        f"⚡ IQ: <b>{iq}</b>{next_info}\n\n"
        f"📊 Квизов пройдено: <b>{p['quizzes']}</b>\n"
        f"🏅 Достижения: {badges_str}\n\n"
        "<i>Проходи квизы и используй калькулятор чтобы прокачать IQ!</i>",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


# ─── КВИЗ ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "quiz_start")
async def cb_quiz_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(QuizState.answering)
    await state.update_data(q=0, score=0)
    q = QUIZ[0]
    await call.message.edit_text(
        f"🧠 <b>Квиз: Финансовая грамотность</b>\n"
        f"Вопрос 1/{len(QUIZ)}\n\n"
        f"<b>{q['q']}</b>",
        reply_markup=quiz_kb(0), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("qa_"), QuizState.answering)
async def cb_quiz_answer(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    q_idx, chosen = int(parts[1]), int(parts[2])
    data = await state.get_data()

    if data.get("q") != q_idx:
        await call.answer()
        return

    correct = QUIZ[q_idx]["ans"] == chosen
    score = data["score"] + (1 if correct else 0)
    next_q = q_idx + 1

    if correct:
        add_iq(call.from_user.id, IQ_PER_CORRECT)
        feedback = "✅ Правильно! +15 IQ"
    else:
        correct_text = QUIZ[q_idx]["opts"][QUIZ[q_idx]["ans"]]
        feedback = f"❌ Неверно. Правильный ответ: <i>{correct_text}</i>"

    if next_q >= len(QUIZ):
        # квиз окончен
        p = get_profile(call.from_user.id)
        p["quizzes"] += 1
        add_iq(call.from_user.id, IQ_PER_QUIZ_FINISH)
        pct = round(score / len(QUIZ) * 100)
        badge = None
        if score == len(QUIZ):
            badge = "🎯"
            add_iq(call.from_user.id, 100, badge)
        elif score >= 7:
            badge = "⭐"
            add_iq(call.from_user.id, 30, badge)

        total_iq = get_profile(call.from_user.id)["iq"]
        await state.clear()
        await call.message.edit_text(
            f"{feedback}\n\n"
            f"🏁 <b>Квиз завершён!</b>\n\n"
            f"Результат: <b>{score}/{len(QUIZ)}</b> ({pct}%)\n"
            f"Получено IQ: <b>+{IQ_PER_QUIZ_FINISH + (IQ_PER_CORRECT if correct else 0)}{' +100 бонус!' if score == len(QUIZ) else ''}</b>\n"
            f"Твой IQ: <b>{total_iq}</b> — {iq_level(total_iq)}\n"
            + (f"\n🏅 Новое достижение: {badge}!" if badge else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [Btn(text="🔄 Пройти снова", callback_data="quiz_start")],
                [Btn(text="« Главное меню", callback_data="main_menu")],
            ]),
            parse_mode="HTML"
        )
    else:
        await state.update_data(q=next_q, score=score)
        q = QUIZ[next_q]
        await call.message.edit_text(
            f"{feedback}\n\n"
            f"🧠 <b>Квиз</b> — Вопрос {next_q + 1}/{len(QUIZ)}\n\n"
            f"<b>{q['q']}</b>",
            reply_markup=quiz_kb(next_q), parse_mode="HTML"
        )
    await call.answer()


# ─── БЮДЖЕТ ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "budget_start")
async def cb_budget_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(BudgetState.income)
    await call.message.edit_text(
        "💰 <b>Калькулятор бюджета</b>\n\n"
        "Введи свой <b>ежемесячный доход</b> в рублях (только число):\n\n"
        "<i>Например: 50000</i>",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.message(BudgetState.income)
async def budget_income(msg: Message, state: FSMContext):
    try:
        income = float(msg.text.replace(" ", "").replace(",", "."))
        if income <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи число больше нуля. Например: <b>50000</b>", parse_mode="HTML")
        return

    await state.update_data(income=income)
    await state.set_state(BudgetState.expenses)
    await msg.answer(
        f"✅ Доход: <b>{income:,.0f} ₽</b>\n\n"
        "Теперь введи сумму <b>обязательных расходов</b> в месяц\n"
        "(аренда, еда, транспорт, коммуналка):\n\n"
        "<i>Например: 30000</i>",
        parse_mode="HTML"
    )


@router.message(BudgetState.expenses)
async def budget_expenses(msg: Message, state: FSMContext):
    try:
        expenses = float(msg.text.replace(" ", "").replace(",", "."))
        if expenses < 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Введи число. Например: <b>30000</b>", parse_mode="HTML")
        return

    data = await state.get_data()
    income = data["income"]
    await state.clear()

    remaining = income - expenses
    if remaining < 0:
        await msg.answer(
            f"💸 Доход: <b>{income:,.0f} ₽</b>\n"
            f"💳 Расходы: <b>{expenses:,.0f} ₽</b>\n\n"
            f"⚠️ <b>Расходы превышают доход на {abs(remaining):,.0f} ₽!</b>\n\n"
            "Пора пересмотреть бюджет. Посмотри советы по экономии 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [Btn(text="💡 Советы по экономии", callback_data="tips_menu")],
                [Btn(text="« Главное меню", callback_data="main_menu")],
            ]),
            parse_mode="HTML"
        )
        return

    # Правило 50/30/20 расчёт
    needs_ideal = income * 0.50
    wants_ideal = income * 0.30
    savings_ideal = income * 0.20

    needs_status = "✅" if expenses <= needs_ideal else "⚠️"
    savings_actual = remaining
    savings_pct = round(savings_actual / income * 100)

    add_iq(msg.from_user.id, 30, "📊")
    total_iq = get_profile(msg.from_user.id)["iq"]

    await msg.answer(
        f"📊 <b>Анализ бюджета</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Доход: <b>{income:,.0f} ₽</b>\n"
        f"💳 Обязательные расходы: <b>{expenses:,.0f} ₽</b> {needs_status}\n"
        f"💰 Свободный остаток: <b>{remaining:,.0f} ₽</b>\n\n"
        f"📐 <b>Рекомендации (правило 50/30/20):</b>\n"
        f"  Нужды (50%): {needs_ideal:,.0f} ₽\n"
        f"  Желания (30%): {wants_ideal:,.0f} ₽\n"
        f"  Накопления (20%): <b>{savings_ideal:,.0f} ₽</b>\n\n"
        f"{'✅' if savings_pct >= 20 else '💡'} Ты откладываешь <b>{savings_pct}%</b> дохода\n"
        + ("Отличный результат! 🎉" if savings_pct >= 20 else f"Цель — 20% ({savings_ideal:,.0f} ₽)") +
        f"\n\n⚡ +30 IQ за использование калькулятора!\nТвой IQ: <b>{total_iq}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text="💡 Советы по экономии", callback_data="tips_menu")],
            [Btn(text="« Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="HTML"
    )


# ─── СОВЕТЫ ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "tips_menu")
async def cb_tips_menu(call: CallbackQuery):
    await call.message.edit_text(
        "💡 <b>Советы по экономии</b>\n\nВыбери совет 👇",
        reply_markup=tips_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("tip_"))
async def cb_tip(call: CallbackQuery):
    idx = int(call.data.split("_")[1])
    title, text = TIPS[idx]
    add_iq(call.from_user.id, 5)
    await call.message.edit_text(
        f"💡 <b>{title}</b>\n\n{text}\n\n<i>+5 IQ за изучение совета</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text="← Все советы", callback_data="tips_menu")],
            [Btn(text="« Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="HTML"
    )
    await call.answer()


# ─── СЛОВАРЬ ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "glossary_menu")
async def cb_glossary_menu(call: CallbackQuery):
    await call.message.edit_text(
        "📖 <b>Словарь инвестора</b>\n\nВыбери термин 👇",
        reply_markup=glossary_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("term_"))
async def cb_term(call: CallbackQuery):
    idx = int(call.data.split("_")[1])
    terms = list(GLOSSARY.keys())
    term = terms[idx]
    definition = GLOSSARY[term]
    add_iq(call.from_user.id, 5)
    await call.message.edit_text(
        f"📖 <b>{term}</b>\n\n{definition}\n\n<i>+5 IQ за изучение термина</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text="← Все термины", callback_data="glossary_menu")],
            [Btn(text="« Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="HTML"
    )
    await call.answer()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    if not BOT_TOKEN:
        print("Установи переменную окружения FINBOT_TOKEN")
        return
    import ssl
    import aiohttp
    from aiogram.client.session.aiohttp import AiohttpSession

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    http_session = aiohttp.ClientSession(connector=connector)

    aio_session = AiohttpSession()
    await aio_session.create_session()
    aio_session._session = http_session

    bot = Bot(token=BOT_TOKEN, session=aio_session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print("✅ ФинГрам-бот запущен!")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
