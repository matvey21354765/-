"""
ФинГрам-бот — бот по финансовой грамотности
Целевая аудитория: молодёжь 16–25 лет

Механики:
  1. Финансовый IQ (очки за квизы и активность)
  2. 🎰 Инвестиционный симулятор (4 раунда, рыночные события)
  3. 🗺 Финансовый квест (RPG-сценарий с ветвлением)
  4. 🔍 Финансовый детектив (найди ошибки в бюджете)
  5. 🏆 Таблица лидеров + ежедневный стрик
"""

import asyncio
import logging
import random
from datetime import date
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

# ─── ПРОФИЛИ ───────────────────────────────────────────────────────────────────
# {user_id: {"iq": int, "quizzes": int, "badges": list, "budget_used": bool,
#             "streak": int, "last_login": date, "name": str}}
_profiles: dict = {}


def get_profile(uid: int, name: str = "") -> dict:
    if uid not in _profiles:
        _profiles[uid] = {
            "iq": 0, "quizzes": 0, "badges": [], "budget_used": False,
            "streak": 0, "last_login": None, "name": name,
        }
    if name:
        _profiles[uid]["name"] = name
    return _profiles[uid]


def add_iq(uid: int, points: int, badge: str = None) -> int:
    p = get_profile(uid)
    p["iq"] += points
    if badge and badge not in p["badges"]:
        p["badges"].append(badge)
    return p["iq"]


def check_streak(uid: int) -> tuple[int, int]:
    p = get_profile(uid)
    today = date.today()
    last = p.get("last_login")
    if last == today:
        return p["streak"], 0
    if last is not None and (today - last).days == 1:
        p["streak"] += 1
    else:
        p["streak"] = 1
    p["last_login"] = today
    bonus = min(p["streak"] * 5, 50)   # +5 IQ за каждый день стрика, макс 50
    add_iq(uid, bonus)
    return p["streak"], bonus


def iq_level(iq: int) -> str:
    if iq < 50:   return "🥉 Новичок"
    if iq < 150:  return "🥈 Студент"
    if iq < 300:  return "🥇 Знаток"
    if iq < 500:  return "💎 Эксперт"
    return "🏆 Финансовый гуру"


def get_leaderboard(limit: int = 5) -> list[tuple[str, int]]:
    top = sorted(_profiles.items(), key=lambda x: x[1]["iq"], reverse=True)[:limit]
    return [(p["name"] or f"#{uid}", p["iq"]) for uid, p in top]


# ─── FSM ───────────────────────────────────────────────────────────────────────

class QuizState(StatesGroup):
    answering = State()


class BudgetState(StatesGroup):
    income = State()
    expenses = State()


class InvestState(StatesGroup):
    allocating = State()
    result = State()


class QuestState(StatesGroup):
    step = State()


class DetectiveState(StatesGroup):
    answering = State()


# ─── КВИЗ ──────────────────────────────────────────────────────────────────────

QUIZ = [
    {"q": "Что такое инфляция?",
     "opts": ["Рост цен на товары и услуги", "Снижение налогов", "Повышение зарплат", "Курс доллара"],
     "ans": 0},
    {"q": "Сколько рекомендуется иметь в «финансовой подушке безопасности»?",
     "opts": ["1 месячный расход", "3–6 месячных расходов", "10 месячных расходов", "Год расходов"],
     "ans": 1},
    {"q": "Что такое диверсификация?",
     "opts": ["Вложение всего в один актив", "Распределение рисков по разным активам", "Снятие вклада досрочно", "Кредит в банке"],
     "ans": 1},
    {"q": "Что такое ОФЗ?",
     "opts": ["Облигации федерального займа", "Открытый финансовый заём", "Обязательный фонд защиты", "Операция с фьючерсами"],
     "ans": 0},
    {"q": "Правило «50/30/20» в бюджетировании означает:",
     "opts": ["50% — еда, 30% — жильё, 20% — развлечения",
              "50% — нужды, 30% — желания, 20% — накопления",
              "50% — налоги, 30% — кредиты, 20% — остаток",
              "50% — инвестиции, 30% — депозит, 20% — наличные"],
     "ans": 1},
    {"q": "Что такое сложный процент?",
     "opts": ["Процент только на начальную сумму",
              "Процент на сумму + накопленные проценты",
              "Штраф за просрочку кредита", "Комиссия банка"],
     "ans": 1},
    {"q": "Какой инструмент считается наименее рискованным?",
     "opts": ["Акции стартапов", "Криптовалюта", "Государственные облигации", "Форекс"],
     "ans": 2},
    {"q": "ETF — это:",
     "opts": ["Электронный перевод денег", "Биржевой инвестиционный фонд",
              "Европейский торговый фонд", "Вид банковского счёта"],
     "ans": 1},
    {"q": "Что такое ликвидность актива?",
     "opts": ["Доходность за год", "Скорость превращения актива в деньги",
              "Налог при продаже", "Стоимость покупки"],
     "ans": 1},
    {"q": "Какой процент годовых от инфляции считается «реальной» доходностью?",
     "opts": ["Номинальная доходность", "Номинальная доходность минус инфляция",
              "Инфляция плюс ставка ЦБ", "Ключевая ставка ЦБ"],
     "ans": 1},
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

# ─── 🎰 ИНВЕСТИЦИОННЫЙ СИМУЛЯТОР ────────────────────────────────────────────────
# 4 раунда = 4 года. Каждый раунд: выбор аллокации → случайное рыночное событие → результат.

INVEST_START = 100_000  # стартовый капитал ₽

MARKET_EVENTS = [
    {"name": "📈 Бычий рынок", "desc": "Акции взлетели, всё растёт!",
     "deposit": 1.07, "ofz": 1.09, "stocks": 1.35, "crypto": 1.60},
    {"name": "📉 Кризис", "desc": "Рынки рухнули, паника среди инвесторов.",
     "deposit": 1.07, "ofz": 1.05, "stocks": 0.72, "crypto": 0.40},
    {"name": "〰️ Стагнация", "desc": "Рынок топчется на месте, всё тихо.",
     "deposit": 1.07, "ofz": 1.08, "stocks": 1.04, "crypto": 0.95},
    {"name": "🔥 Крипто-хайп", "desc": "Биткоин снова на хайпе!",
     "deposit": 1.07, "ofz": 1.08, "stocks": 1.10, "crypto": 2.20},
    {"name": "⚡ Инфляционный шок", "desc": "Инфляция 15% — наличные обесцениваются.",
     "deposit": 1.07, "ofz": 1.03, "stocks": 1.08, "crypto": 1.15},
    {"name": "🌍 Внешний кризис", "desc": "Геополитическая нестабильность бьёт по рынкам.",
     "deposit": 1.07, "ofz": 1.06, "stocks": 0.85, "crypto": 0.65},
]

INVEST_PRESETS = [
    ("🛡 Консерватор", "Депозит 60%, ОФЗ 40%",    {"deposit": 60, "ofz": 40, "stocks": 0, "crypto": 0}),
    ("⚖️ Сбалансированный", "Депозит 30%, ОФЗ 30%, Акции 40%", {"deposit": 30, "ofz": 30, "stocks": 40, "crypto": 0}),
    ("🚀 Агрессор", "Акции 60%, Крипто 30%, ОФЗ 10%", {"deposit": 0, "ofz": 10, "stocks": 60, "crypto": 30}),
    ("🎲 Крипто-геймблер", "Крипто 80%, Акции 20%", {"deposit": 0, "ofz": 0, "stocks": 20, "crypto": 80}),
    ("🏦 Классический", "Депозит 50%, ОФЗ 30%, Акции 20%", {"deposit": 50, "ofz": 30, "stocks": 20, "crypto": 0}),
]

INFLATION_PER_YEAR = 0.08  # 8% инфляция для расчёта реальной доходности


def invest_presets_kb() -> InlineKeyboardMarkup:
    rows = [[Btn(text=name, callback_data=f"inv_preset_{i}")] for i, (name, _, _) in enumerate(INVEST_PRESETS)]
    rows.append([Btn(text="« Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invest_continue_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="▶️ Следующий год", callback_data="inv_next")],
        [Btn(text="🏳 Завершить досрочно", callback_data="inv_finish")],
    ])


# ─── 🗺 ФИНАНСОВЫЙ КВЕСТ ──────────────────────────────────────────────────────
# Ветвящийся сценарий "Тебе 22 года, первая зарплата 70 000₽"
# Каждый узел: текст + варианты → следующий узел + изменение финансового здоровья (FH)

# FH = Financial Health Score, от 0 до 100, начинаем с 50
QUEST_START_FH = 50

QUEST_TREE = {
    "start": {
        "text": (
            "🎉 Поздравляю! Тебе 22 года, ты только что получил первую зарплату — <b>70 000 ₽</b>.\n\n"
            "Что делаешь в первую очередь?"
        ),
        "choices": [
            ("💰 Откладываю 20% и трачу остаток", "a1", +10),
            ("🎉 Гуляю — трачу всё, зарплата будет ещё", "a2", -15),
            ("🏦 Кладу всё на депозит", "a3", +5),
        ],
    },
    "a1": {
        "text": (
            "✅ Отложил 14 000₽. Молодец!\n\n"
            "Через 2 недели сломался телефон. Ремонт — 12 000₽.\n"
            "У тебя есть отложенные деньги. Что делаешь?"
        ),
        "choices": [
            ("🔧 Плачу из накоплений", "b1", +5),
            ("💳 Беру кредит — накопления не трогаю", "b2", -10),
            ("📱 Откладываю ремонт, буду экономить", "b3", 0),
        ],
    },
    "a2": {
        "text": (
            "🎊 Отличная вечеринка! Но теперь до следующей зарплаты — 2 недели, а в кошельке пусто.\n\n"
            "Тебе нужны 5 000₽ на еду и проезд. Что делаешь?"
        ),
        "choices": [
            ("👨‍👩‍👦 Занимаю у родителей", "b4", -5),
            ("💳 Беру микрозайм 5 000₽ под 1% в день", "b5", -20),
            ("🥗 Жёстко экономлю — справлюсь", "b6", +5),
        ],
    },
    "a3": {
        "text": (
            "🏦 Всё на депозите под 8% годовых.\n\n"
            "Коллега рассказывает про акции — говорит, заработал 40% за год.\n"
            "Предлагает вложить вместе в одну компанию. Что делаешь?"
        ),
        "choices": [
            ("📊 Вкладываю 30% накоплений в акции", "b7", +8),
            ("❌ Отказываюсь — слишком рискованно", "b8", +3),
            ("💸 Вкладываю всё — хочу быстро разбогатеть", "b9", -15),
        ],
    },
    "b1": {
        "text": "✅ Правильно! Финансовая подушка — для таких случаев. Ты уже думаешь как финансово грамотный человек.\n\n"
                "Через 3 месяца у тебя накопилось 35 000₽. Друг предлагает вместе открыть небольшой бизнес — нужно 30 000₽.",
        "choices": [
            ("🚀 Вкладываю в бизнес — риск оправдан", "c1", +10),
            ("🔒 Отказываюсь — это вся моя подушка", "c2", +5),
            ("🤝 Вкладываю половину — диверсифицирую риск", "c3", +15),
        ],
    },
    "b2": {
        "text": "💳 Взял кредит 12 000₽ под 19% годовых. Теперь каждый месяц отдаёшь 1 100₽.\n\n"
                "Зато накопления целы. Но зачем была подушка, если ты её не используешь?\n\n"
                "Через квартал скопилось 28 000₽. Куда вложишь?",
        "choices": [
            ("📈 Покупаю ETF на индекс", "c4", +12),
            ("🏦 Оставляю на депозите", "c5", +5),
            ("🎰 Пробую криптовалюту", "c6", -5),
        ],
    },
    "b3": {
        "text": "🥗 Справился, но это был стресс. Ты понял: нужна финансовая подушка.\n\n"
                "Со следующей зарплаты начинаешь откладывать. Сколько?",
        "choices": [
            ("💰 10% каждый месяц", "c7", +8),
            ("💰 30% — хочу быстро накопить", "c8", +15),
            ("😅 Посмотрим по ситуации...", "c9", -5),
        ],
    },
    "b4": {
        "text": "👨‍👩‍👦 Родители выручили. Но это неловко.\n\nТы решаешь никогда больше не попадать в такую ситуацию.\nСо следующей зарплаты начинаешь формировать подушку. Молодец! 💪",
        "choices": [
            ("✅ Продолжаю историю", "c7", +5),
        ],
    },
    "b5": {
        "text": "⚠️ Микрозайм 1% В ДЕНЬ = 365% годовых!\n\n5 000₽ через месяц превратились в 6 500₽. Ты попал в долговую ловушку.\n\nЭто дорогой урок. Никогда — никогда — не бери микрозаймы!",
        "choices": [
            ("😓 Понял, больше не буду", "c9", -5),
        ],
    },
    "b6": {
        "text": "💪 Выдержал! Питался гречкой и ходил пешком, но справился без долгов.\n\nЭтот опыт научил тебя ценить деньги. Следующую зарплату ты точно распределишь умно.",
        "choices": [
            ("✅ Продолжаю историю", "c8", +10),
        ],
    },
    "b7": {
        "text": "📊 30% в акции — разумная диверсификация!\n\nЧерез год акции выросли на 22%. Ты заработал дополнительные деньги не работая.",
        "choices": [
            ("✅ Продолжаю историю", "c4", +10),
        ],
    },
    "b8": {
        "text": "🔒 Осторожность — это хорошо. Но вечно держать всё на депозите под инфляцию — тоже не выход.\n\nЧерез год ты начинаешь изучать инвестиции самостоятельно.",
        "choices": [
            ("📚 Изучаю ETF и начинаю инвестировать", "c4", +8),
            ("🏦 Пока остаюсь на депозите", "c5", +2),
        ],
    },
    "b9": {
        "text": "📉 Вложил всё в одну компанию. Через 4 месяца она потеряла 60% стоимости из-за скандала.\n\nТы потерял больше половины накоплений. Больно, но урок усвоен: НИКОГДА не вкладывай всё в один актив!",
        "choices": [
            ("😓 Буду диверсифицировать", "c9", -15),
        ],
    },
    # Финальные узлы
    "c1": {"text": "🚀 Бизнес выстрелил! Через год ты вернул вложения и заработал ещё 20 000₽. Риск оправдался!", "choices": [], "final": True},
    "c2": {"text": "🔒 Разумно. Подушка важнее авантюр. Бизнес без тебя не взлетел. Ты сохранил стабильность.", "choices": [], "final": True},
    "c3": {"text": "🤝 Вложил 15 000₽, сохранил 20 000₽ в резерве. Бизнес принёс скромную прибыль. Идеальный баланс!", "choices": [], "final": True},
    "c4": {"text": "📈 ETF на индекс — классика! Через 3 года ты в плюсе на 45%. Сложный процент работает на тебя.", "choices": [], "final": True},
    "c5": {"text": "🏦 Депозит надёжен, но инфляция съедает реальную доходность. Пора изучать инвестиции!", "choices": [], "final": True},
    "c6": {"text": "🎰 Крипта упала на 70%. Ты потерял часть накоплений. Без диверсификации — это казино.", "choices": [], "final": True},
    "c7": {"text": "💰 10% каждый месяц — отличная привычка! Через год у тебя финансовая подушка на 2 месяца.", "choices": [], "final": True},
    "c8": {"text": "💰 30% — амбициозно! За 8 месяцев ты накопил полную подушку безопасности. Ты — молодец!", "choices": [], "final": True},
    "c9": {"text": "😅 «Посмотрим» превратилось в «снова ноль». Без системы деньги утекают сами. Начни с малого — 10%.", "choices": [], "final": True},
}


def quest_choices_kb(node_id: str) -> InlineKeyboardMarkup:
    node = QUEST_TREE[node_id]
    rows = []
    for i, (text, _, _) in enumerate(node["choices"]):
        rows.append([Btn(text=text, callback_data=f"quest_{node_id}_{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── 🔍 ФИНАНСОВЫЙ ДЕТЕКТИВ ───────────────────────────────────────────────────
# Показывается "бюджет клиента" с ошибками — нужно найти их все.

DETECTIVE_CASES = [
    {
        "title": "📁 Дело №1: Максим, 23 года",
        "story": (
            "Максим зарабатывает <b>60 000₽</b> в месяц.\n\n"
            "Вот его финансовый план:\n"
            "• Аренда: 25 000₽\n"
            "• Еда: 12 000₽\n"
            "• Развлечения и кафе: 18 000₽\n"
            "• Накопления: 0₽\n"
            "• Взял кредит на iPhone 150 000₽ под 24% годовых\n"
            "• Финансовой подушки нет\n\n"
            "Сколько <b>грубых финансовых ошибок</b> ты видишь?"
        ),
        "opts": ["1 ошибка", "2 ошибки", "3 ошибки", "4 ошибки"],
        "ans": 3,
        "explanation": (
            "❌ <b>3 грубые ошибки:</b>\n\n"
            "1️⃣ <b>Нет накоплений (0₽)</b> — нарушено правило 20%.\n"
            "   Должно быть минимум 12 000₽ в месяц.\n\n"
            "2️⃣ <b>Развлечения 30% дохода</b> — при отсутствии накоплений это расточительство.\n\n"
            "3️⃣ <b>Кредит на гаджет под 24%</b> — iPhone не актив, он не приносит доход.\n"
            "   Переплата за 2 года: ~40 000₽. Это 'дырка' в бюджете.\n\n"
            "💡 Отсутствие подушки безопасности — следствие всех этих ошибок."
        ),
    },
    {
        "title": "📁 Дело №2: Алина, 25 лет",
        "story": (
            "Алина зарабатывает <b>80 000₽</b>. Вот её план:\n\n"
            "• Все деньги хранит на карте (без вклада)\n"
            "• 50 000₽ вложила в акции одной компании\n"
            "• Берёт рассрочки на одежду — «это же не кредит»\n"
            "• Каждый месяц тратит «сколько осталось»\n"
            "• Мечтает разбогатеть на криптовалюте\n\n"
            "Какое <b>главное правило инвестирования</b> нарушила Алина?"
        ),
        "opts": [
            "Не диверсифицировала вложения",
            "Не завела кредитную карту",
            "Слишком много откладывает",
            "Не купила золото",
        ],
        "ans": 0,
        "explanation": (
            "✅ <b>Главная ошибка — нет диверсификации!</b>\n\n"
            "50 000₽ в одну компанию = если она упадёт, потеряешь всё.\n\n"
            "Но у Алины и другие ошибки:\n"
            "🔴 Деньги на карте без вклада — инфляция их съедает\n"
            "🔴 Рассрочка = тот же кредит, только завуалированный\n"
            "🔴 Тратит «сколько осталось» — нет системы бюджета\n\n"
            "💡 Правило: сначала откладывай, потом трать — не наоборот."
        ),
    },
    {
        "title": "📁 Дело №3: Дмитрий, 21 год",
        "story": (
            "Дмитрий получает стипендию <b>8 000₽</b> и подрабатывает на <b>25 000₽</b>.\n\n"
            "Его план:\n"
            "• Вся стипендия — на развлечения\n"
            "• Из подработки: 20 000₽ — расходы, 5 000₽ — накопления\n"
            "• Накопил 30 000₽ — хочет вложить в микрофинансовую компанию под 25% годовых\n"
            "• Финансовой подушки нет — есть только эти 30 000₽\n\n"
            "Что <b>опаснее всего</b> в плане Дмитрия?"
        ),
        "opts": [
            "Слишком маленькие накопления",
            "Вложение единственных сбережений в рискованный инструмент",
            "Тратит стипендию на развлечения",
            "Мало зарабатывает",
        ],
        "ans": 1,
        "explanation": (
            "⚠️ <b>Самое опасное — вложить ВСЕ сбережения в МФО под 25%</b>\n\n"
            "МФО с высокой доходностью = высокий риск потери денег.\n"
            "А это вся его финансовая подушка — если что-то случится, он ни с чем.\n\n"
            "Правило: рискованные инвестиции — только на деньги СВЕРХ подушки.\n\n"
            "Остальные ошибки тоже есть, но эта критическая — можно потерять всё."
        ),
    },
]

_DETECTIVE_IDX: dict[int, int] = {}  # uid → номер текущего дела


# ─── 📹 ВИДЕО-УРОКИ ───────────────────────────────────────────────────────────
# Реальные YouTube-видео по финансовой грамотности + конспект

VIDEO_LESSONS = [
    {
        "title": "💰 Как работает личный бюджет",
        "desc": (
            "📌 <b>Ключевые идеи урока:</b>\n\n"
            "• Бюджет — это план, а не ограничение\n"
            "• Записывай расходы хотя бы 1 месяц — увидишь «дыры»\n"
            "• Правило 50/30/20: нужды / желания / накопления\n"
            "• Автоматизируй накопления — переводи % сразу в день зарплаты\n\n"
            "🎯 <b>Задание:</b> посчитай свои расходы за последнюю неделю по категориям"
        ),
        "url": "https://www.youtube.com/watch?v=HQzoZfc3GwQ",
        "duration": "12 мин",
        "tag": "Бюджет",
    },
    {
        "title": "📈 Что такое инвестиции и с чего начать",
        "desc": (
            "📌 <b>Ключевые идеи урока:</b>\n\n"
            "• Инвестиции — это деньги, работающие на тебя\n"
            "• Депозит — самый простой старт (но не самый доходный)\n"
            "• ETF — корзина акций, идеально для новичка\n"
            "• Главное правило: не инвестируй деньги, которые могут понадобиться\n"
            "• Сложный процент: 10 000₽ под 15% = 40 000₽ через 10 лет\n\n"
            "🎯 <b>Задание:</b> открой демо-счёт на любом брокере и купи 1 ETF"
        ),
        "url": "https://www.youtube.com/watch?v=W6pVMXmUmMk",
        "duration": "15 мин",
        "tag": "Инвестиции",
    },
    {
        "title": "🛡 Финансовая подушка безопасности",
        "desc": (
            "📌 <b>Ключевые идеи урока:</b>\n\n"
            "• Подушка = 3–6 месяцев твоих расходов\n"
            "• Хранится в доступном месте: накопительный счёт или короткий депозит\n"
            "• НЕ инвестируется — это не для роста, это для защиты\n"
            "• Начни с 1 000₽ в месяц — главное привычка\n\n"
            "🎯 <b>Задание:</b> посчитай сколько тебе нужно на 3 месяца расходов"
        ),
        "url": "https://www.youtube.com/watch?v=7eoaQzLz-Xk",
        "duration": "8 мин",
        "tag": "Подушка",
    },
    {
        "title": "💳 Кредиты и кредитные карты: как не попасть в ловушку",
        "desc": (
            "📌 <b>Ключевые идеи урока:</b>\n\n"
            "• Кредит — это твои будущие доходы, потраченные сегодня\n"
            "• Микрозаймы: 1% в день = 365% годовых. Никогда!\n"
            "• Кредитка: используй льготный период, гаси полностью\n"
            "• Ипотека ≠ плохо, если ставка ниже инфляции + рост жилья\n"
            "• Правило: кредит только на активы или образование\n\n"
            "🎯 <b>Задание:</b> посчитай реальную переплату по любому кредиту"
        ),
        "url": "https://www.youtube.com/watch?v=PHe0bXAIuk0",
        "duration": "11 мин",
        "tag": "Кредиты",
    },
    {
        "title": "₿ Криптовалюта: что нужно знать перед входом",
        "desc": (
            "📌 <b>Ключевые идеи урока:</b>\n\n"
            "• Крипта — высокорискованный актив, не основа портфеля\n"
            "• Максимальная доля крипты в портфеле новичка: 5–10%\n"
            "• BTC и ETH — наименее рискованные из крипты\n"
            "• DYOR: Do Your Own Research — не верь хайпу\n"
            "• Храни на холодном кошельке, не на бирже\n\n"
            "🎯 <b>Задание:</b> изучи концепцию «белой бумаги» (whitepaper) Bitcoin"
        ),
        "url": "https://www.youtube.com/watch?v=1YyAzVmP9xQ",
        "duration": "18 мин",
        "tag": "Крипта",
    },
    {
        "title": "🧾 Налоги для начинающего инвестора",
        "desc": (
            "📌 <b>Ключевые идеи урока:</b>\n\n"
            "• С прибыли от инвестиций платится НДФЛ 13%\n"
            "• ИИС (тип А): возврат 13% от взноса до 52 000₽ в год\n"
            "• ИИС (тип Б): не платишь налог с прибыли совсем\n"
            "• Дивиденды облагаются налогом автоматически\n"
            "• Брокер — твой налоговый агент: сам считает и платит\n\n"
            "🎯 <b>Задание:</b> узнай что такое ИИС и открой его"
        ),
        "url": "https://www.youtube.com/watch?v=1K2xCB5cVRk",
        "duration": "14 мин",
        "tag": "Налоги",
    },
]

_VIDEO_WATCHED: dict[int, set] = {}  # uid → set of watched video indices


def videos_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    for i, v in enumerate(VIDEO_LESSONS):
        rows.append([Btn(text=f"▶️ {v['tag']}: {v['title']}", callback_data=f"video_{i}")])
    rows.append([Btn(text="« Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def video_kb(idx: int) -> InlineKeyboardMarkup:
    v = VIDEO_LESSONS[idx]
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text=f"▶️ Смотреть на YouTube ({v['duration']})", url=v["url"])],
        [Btn(text="✅ Я посмотрел — дай IQ!", callback_data=f"video_done_{idx}")],
        [Btn(text="← Все уроки", callback_data="videos_menu")],
        [Btn(text="« Главное меню", callback_data="main_menu")],
    ])


# ─── 🧬 ТЕСТ: КТО ТЫ КАК ИНВЕСТОР? ──────────────────────────────────────────

INVESTOR_TEST = [
    {
        "q": "У тебя 100 000₽. Что делаешь?",
        "opts": [
            ("Кладу на депозит — надёжно и понятно", "C"),
            ("Делю: часть на депозит, часть в ETF", "M"),
            ("Покупаю акции роста — хочу больше", "A"),
            ("Вкладываю в крипту — там реальные иксы", "S"),
        ],
    },
    {
        "q": "Твои инвестиции упали на 20%. Реакция?",
        "opts": [
            ("Продаю всё — лучше зафиксировать убыток", "C"),
            ("Жду восстановления — рынок цикличен", "M"),
            ("Докупаю — это распродажа!", "A"),
            ("Перекладываю в другой актив", "S"),
        ],
    },
    {
        "q": "Какой горизонт инвестирования тебе ближе?",
        "opts": [
            ("До 1 года — деньги могут понадобиться", "C"),
            ("3–5 лет — средний срок", "M"),
            ("7–10 лет — готов ждать", "A"),
            ("Хочу заработать уже завтра", "S"),
        ],
    },
    {
        "q": "Ты слышишь про актив, который вырос на 500% за год. Что делаешь?",
        "opts": [
            ("Игнорирую — скорее всего это пузырь", "C"),
            ("Изучаю фундаментал перед покупкой", "M"),
            ("Вкладываю небольшую часть портфеля", "A"),
            ("Беру кредит и закупаюсь на всё", "S"),
        ],
    },
    {
        "q": "Что для тебя важнее?",
        "opts": [
            ("Не потерять то, что есть", "C"),
            ("Баланс между ростом и надёжностью", "M"),
            ("Максимальная доходность даже с риском", "A"),
            ("Быстро разбогатеть любой ценой", "S"),
        ],
    },
    {
        "q": "Сколько времени готов тратить на анализ инвестиций?",
        "opts": [
            ("0 — хочу «вложил и забыл»", "C"),
            ("1–2 часа в месяц — посматривать", "M"),
            ("Несколько часов в неделю — мне интересно", "A"),
            ("Слежу за рынком каждый день", "S"),
        ],
    },
]

INVESTOR_TYPES = {
    "C": {
        "name": "🛡 Консерватор",
        "desc": (
            "Ты ценишь надёжность и предсказуемость.\n\n"
            "<b>Твой идеальный портфель:</b>\n"
            "• 60% — банковский депозит / накопительный счёт\n"
            "• 30% — ОФЗ (государственные облигации)\n"
            "• 10% — ETF на широкий рынок\n\n"
            "<b>Риски:</b> инфляция может «съесть» доходность\n"
            "<b>Совет:</b> хотя бы 10–20% вложи в ETF — долгосрочно это обгоняет депозит"
        ),
        "badge": "🛡",
    },
    "M": {
        "name": "⚖️ Умеренный инвестор",
        "desc": (
            "Ты ищешь баланс между ростом и безопасностью. Это лучшая позиция!\n\n"
            "<b>Твой идеальный портфель:</b>\n"
            "• 30% — депозит / облигации (подушка)\n"
            "• 50% — ETF на индексы (S&P500, MOEX)\n"
            "• 20% — отдельные акции / секторные ETF\n\n"
            "<b>Риски:</b> умеренные просадки в кризис\n"
            "<b>Совет:</b> ребалансируй портфель раз в год"
        ),
        "badge": "⚖️",
    },
    "A": {
        "name": "🚀 Агрессивный инвестор",
        "desc": (
            "Ты готов к риску ради высокой доходности. Уважаю!\n\n"
            "<b>Твой идеальный портфель:</b>\n"
            "• 10% — депозит (минимальная подушка)\n"
            "• 40% — акции роста (tech, biotech)\n"
            "• 35% — ETF развивающихся рынков\n"
            "• 15% — крипта (BTC/ETH)\n\n"
            "<b>Риски:</b> можешь потерять 40–50% в кризис\n"
            "<b>Совет:</b> диверсификация спасёт от катастрофы"
        ),
        "badge": "🚀",
    },
    "S": {
        "name": "🎰 Спекулянт",
        "desc": (
            "Ты хочешь быстрого результата — это опасно, но честно!\n\n"
            "<b>Реальность спекулянта:</b>\n"
            "• 90% трейдеров теряют деньги в долгосроке\n"
            "• Крипта и плечи могут обнулить счёт за день\n"
            "• Новостной трейдинг требует профессиональных знаний\n\n"
            "<b>Если всё же хочешь спекулировать:</b>\n"
            "• Выдели «игровые» деньги — не больше 5% капитала\n"
            "• Всегда ставь стоп-лосс\n"
            "• Основной капитал — в надёжных инструментах\n\n"
            "<b>Совет:</b> начни с симулятора (бумажная торговля)"
        ),
        "badge": "🎰",
    },
}


class InvestorTestState(StatesGroup):
    answering = State()


def investor_test_kb(q_idx: int) -> InlineKeyboardMarkup:
    q = INVESTOR_TEST[q_idx]
    rows = [[Btn(text=opt, callback_data=f"itest_{q_idx}_{t}")] for opt, t in q["opts"]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def detective_kb(case_idx: int) -> InlineKeyboardMarkup:
    case = DETECTIVE_CASES[case_idx]
    rows = [[Btn(text=opt, callback_data=f"det_{case_idx}_{i}")] for i, opt in enumerate(case["opts"])]
    rows.append([Btn(text="« Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── КЛАВИАТУРЫ ────────────────────────────────────────────────────────────────

def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🧠 Квиз: проверь знания",       callback_data="quiz_start")],
        [Btn(text="🎰 Инвестиционный симулятор",    callback_data="invest_start")],
        [Btn(text="🗺 Финансовый квест",             callback_data="quest_start")],
        [Btn(text="🔍 Финансовый детектив",          callback_data="detective_start")],
        [Btn(text="📹 Видео-уроки",                  callback_data="videos_menu")],
        [Btn(text="🧬 Тест: кто ты как инвестор?",  callback_data="investor_test_start")],
        [Btn(text="💰 Калькулятор бюджета",          callback_data="budget_start")],
        [Btn(text="💡 Советы",  callback_data="tips_menu"),
         Btn(text="📖 Словарь", callback_data="glossary_menu")],
        [Btn(text="👤 Мой Финансовый IQ",            callback_data="profile"),
         Btn(text="🏆 Рейтинг",                     callback_data="leaderboard")],
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


# ─── ХЭНДЛЕРЫ: СТАРТ ────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    name = msg.from_user.first_name or "друг"
    get_profile(msg.from_user.id, name)
    streak, bonus = check_streak(msg.from_user.id)

    streak_text = ""
    if bonus > 0:
        streak_text = f"\n🔥 Стрик <b>{streak} день</b>! +{bonus} IQ за ежедневный вход!\n"

    await msg.answer(
        f"👋 Привет, <b>{name}</b>!{streak_text}\n\n"
        "Я — <b>ФинГрам</b>, твой личный тренер по финансовой грамотности 💸\n\n"
        "📚 <b>Что умею:</b>\n"
        "  🎰 <b>Симулятор</b> — вложи 100 000₽ и прожи 4 инвестиционных года\n"
        "  🗺 <b>Квест</b> — RPG-сценарий с реальными финансовыми выборами\n"
        "  🔍 <b>Детектив</b> — найди ошибки в чужом бюджете\n"
        "  📹 <b>Видео-уроки</b> — 6 тем с конспектом и заданиями\n"
        "  🧬 <b>Тест инвестора</b> — узнай свой психотип\n"
        "  🏆 <b>Рейтинг</b> — соревнуйся с другими по IQ\n\n"
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
    streak = p.get("streak", 0)

    next_level_map = [(50, "🥈 Студент"), (150, "🥇 Знаток"), (300, "💎 Эксперт"), (500, "🏆 Финансовый гуру")]
    next_info = ""
    for threshold, lvl_name in next_level_map:
        if iq < threshold:
            next_info = f"\nДо уровня <b>{lvl_name}</b>: {threshold - iq} IQ"
            break

    await call.message.edit_text(
        f"👤 <b>Мой Финансовый IQ</b>\n\n"
        f"🎓 Уровень: <b>{level}</b>\n"
        f"⚡ IQ: <b>{iq}</b>{next_info}\n"
        f"🔥 Стрик: <b>{streak} дн.</b>\n\n"
        f"📊 Квизов пройдено: <b>{p['quizzes']}</b>\n"
        f"🏅 Достижения: {badges_str}\n\n"
        "<i>Заходи каждый день — стрик даёт бонусный IQ!</i>",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


# ─── РЕЙТИНГ ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "leaderboard")
async def cb_leaderboard(call: CallbackQuery):
    top = get_leaderboard(10)
    uid = call.from_user.id
    my_iq = get_profile(uid)["iq"]

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, iq) in enumerate(top):
        m = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{m} <b>{name}</b> — {iq} IQ")

    my_rank = sorted(_profiles.values(), key=lambda x: x["iq"], reverse=True)
    my_pos = next((i+1 for i, p in enumerate(my_rank) if p["iq"] == my_iq), "?")

    await call.message.edit_text(
        f"🏆 <b>Топ игроков</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + ("\n".join(lines) if lines else "Пока никого нет") +
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Ты на <b>{my_pos} месте</b> с {my_iq} IQ\n\n"
        "<i>Проходи квизы и симулятор — поднимайся в рейтинге!</i>",
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
        f"Вопрос 1/{len(QUIZ)}\n\n<b>{q['q']}</b>",
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
            f"{feedback}\n\n🏁 <b>Квиз завершён!</b>\n\n"
            f"Результат: <b>{score}/{len(QUIZ)}</b> ({pct}%)\n"
            f"Получено IQ: <b>+{IQ_PER_QUIZ_FINISH + (IQ_PER_CORRECT if correct else 0)}"
            f"{' +100 бонус!' if score == len(QUIZ) else ''}</b>\n"
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
            f"{feedback}\n\n🧠 <b>Квиз</b> — Вопрос {next_q + 1}/{len(QUIZ)}\n\n<b>{q['q']}</b>",
            reply_markup=quiz_kb(next_q), parse_mode="HTML"
        )
    await call.answer()


# ─── 🎰 ИНВЕСТИЦИОННЫЙ СИМУЛЯТОР ────────────────────────────────────────────────

@router.callback_query(F.data == "invest_start")
async def cb_invest_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(InvestState.allocating)
    await state.update_data(capital=INVEST_START, round=1, history=[])
    text = (
        "🎰 <b>Инвестиционный симулятор</b>\n\n"
        f"У тебя <b>{INVEST_START:,}₽</b> виртуального капитала.\n"
        "Ты проживёшь <b>4 инвестиционных года</b>.\n"
        "Каждый год — случайное рыночное событие.\n\n"
        "<b>Активы:</b>\n"
        "🏦 Депозит — ~7% гарантированно\n"
        "📜 ОФЗ — ~8% с небольшим риском\n"
        "📈 Акции — от -30% до +40%\n"
        "₿ Крипто — от -60% до +120%\n\n"
        "Выбери <b>стратегию на год 1:</b>"
    )
    await call.message.edit_text(text, reply_markup=invest_presets_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("inv_preset_"), InvestState.allocating)
async def cb_invest_preset(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split("_")[2])
    name, desc, alloc = INVEST_PRESETS[idx]
    data = await state.get_data()
    capital = data["capital"]
    round_num = data["round"]
    history = data["history"]

    # Случайное событие
    event = random.choice(MARKET_EVENTS)

    # Расчёт нового капитала
    new_capital = 0
    breakdown = []
    for asset, pct in alloc.items():
        if pct == 0:
            continue
        amount = capital * pct / 100
        multiplier = event[asset]
        result = amount * multiplier
        change_pct = (multiplier - 1) * 100
        sign = "+" if change_pct >= 0 else ""
        breakdown.append(f"  {asset.upper()} ({pct}%): {amount:,.0f}₽ → {result:,.0f}₽ ({sign}{change_pct:.0f}%)")
        new_capital += result

    change = new_capital - capital
    sign = "+" if change >= 0 else ""

    history.append({
        "round": round_num, "event": event["name"],
        "strategy": name, "start": capital, "end": new_capital,
    })

    if round_num >= 4:
        # Финал
        await state.clear()
        total_change = new_capital - INVEST_START
        total_pct = total_change / INVEST_START * 100
        # Реальная доходность с учётом инфляции
        inflation_4y = INVEST_START * ((1 + INFLATION_PER_YEAR) ** 4)
        real_gain = new_capital - inflation_4y
        real_word = "выиграл" if real_gain >= 0 else "проиграл инфляции"

        history_lines = "\n".join(
            f"  Год {h['round']}: {h['event']} | {h['strategy']} | {h['start']:,.0f}→{h['end']:,.0f}₽"
            for h in history
        )

        iq_reward = max(20, min(100, int(total_pct)))
        badge = "💼" if total_pct > 30 else None
        add_iq(call.from_user.id, iq_reward, badge)

        await call.message.edit_text(
            f"🎰 <b>Год {round_num} — {event['name']}</b>\n"
            f"<i>{event['desc']}</i>\n\n"
            f"Стратегия: <b>{name}</b>\n"
            + "\n".join(breakdown) +
            f"\n\nИтог года: <b>{sign}{change:,.0f}₽</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏁 <b>Симуляция завершена!</b>\n\n"
            f"Стартовый капитал: <b>{INVEST_START:,}₽</b>\n"
            f"Финальный капитал: <b>{new_capital:,.0f}₽</b>\n"
            f"Общая доходность: <b>{'+' if total_change >= 0 else ''}{total_change:,.0f}₽ ({'+' if total_pct >= 0 else ''}{total_pct:.1f}%)</b>\n\n"
            f"📊 Инфляция за 4 года (8%/год): {inflation_4y:,.0f}₽\n"
            f"💡 Реально ты <b>{real_word}</b> {abs(real_gain):,.0f}₽\n\n"
            f"<b>История:</b>\n{history_lines}\n\n"
            f"⚡ +{iq_reward} IQ за симуляцию!"
            + (f"\n🏅 Новый бейдж: {badge}!" if badge else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [Btn(text="🔄 Сыграть снова", callback_data="invest_start")],
                [Btn(text="« Главное меню", callback_data="main_menu")],
            ]),
            parse_mode="HTML"
        )
    else:
        await state.update_data(capital=new_capital, round=round_num + 1, history=history)
        await call.message.edit_text(
            f"🎰 <b>Год {round_num} — {event['name']}</b>\n"
            f"<i>{event['desc']}</i>\n\n"
            f"Стратегия: <b>{name}</b>\n"
            + "\n".join(breakdown) +
            f"\n\nИтог года: <b>{sign}{change:,.0f}₽</b>\n"
            f"Капитал: <b>{new_capital:,.0f}₽</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Год {round_num + 1} из 4 — выбери стратегию:</b>",
            reply_markup=invest_presets_kb(), parse_mode="HTML"
        )
    await call.answer()


# ─── 🗺 ФИНАНСОВЫЙ КВЕСТ ──────────────────────────────────────────────────────

@router.callback_query(F.data == "quest_start")
async def cb_quest_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(QuestState.step)
    await state.update_data(node="start", fh=QUEST_START_FH, fh_log=[])
    node = QUEST_TREE["start"]
    await call.message.edit_text(
        f"🗺 <b>Финансовый квест</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💚 Финансовое здоровье: <b>{QUEST_START_FH}/100</b>\n\n"
        + node["text"],
        reply_markup=quest_choices_kb("start"), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("quest_"), QuestState.step)
async def cb_quest_choice(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    node_id = parts[1]
    choice_idx = int(parts[2])

    data = await state.get_data()
    if data.get("node") != node_id:
        await call.answer()
        return

    node = QUEST_TREE[node_id]
    choice_text, next_node_id, fh_delta = node["choices"][choice_idx]
    fh = data["fh"] + fh_delta
    fh = max(0, min(100, fh))
    fh_log = data.get("fh_log", [])
    fh_log.append(f"{choice_text[:25]}… ({'+' if fh_delta >= 0 else ''}{fh_delta})")

    next_node = QUEST_TREE[next_node_id]

    if next_node.get("final"):
        await state.clear()
        # Финальная оценка
        if fh >= 80:
            verdict = "🏆 Финансовый гений! Отличные решения."
            badge = "🗺"
            iq_reward = 80
        elif fh >= 60:
            verdict = "✅ Хорошо! Ты принимал в целом правильные решения."
            badge = None
            iq_reward = 50
        elif fh >= 40:
            verdict = "⚠️ Средне. Были ошибки, но ты справился."
            badge = None
            iq_reward = 30
        else:
            verdict = "❌ Сложный путь. Но теперь ты знаешь свои ошибки!"
            badge = None
            iq_reward = 20

        add_iq(call.from_user.id, iq_reward, badge)
        fh_bar = "█" * (fh // 10) + "░" * (10 - fh // 10)

        await call.message.edit_text(
            f"🗺 <b>Квест завершён!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{next_node['text']}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💚 Финансовое здоровье: <b>{fh}/100</b>\n"
            f"<code>{fh_bar}</code>\n\n"
            f"{verdict}\n\n"
            f"⚡ +{iq_reward} IQ"
            + (f"\n🏅 Новый бейдж: {badge}!" if badge else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [Btn(text="🔄 Пройти снова", callback_data="quest_start")],
                [Btn(text="« Главное меню",  callback_data="main_menu")],
            ]),
            parse_mode="HTML"
        )
    else:
        await state.update_data(node=next_node_id, fh=fh, fh_log=fh_log)
        fh_bar = "█" * (fh // 10) + "░" * (10 - fh // 10)
        await call.message.edit_text(
            f"🗺 <b>Финансовый квест</b>\n"
            f"💚 Здоровье: <b>{fh}/100</b>  <code>{fh_bar}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            + next_node["text"],
            reply_markup=quest_choices_kb(next_node_id), parse_mode="HTML"
        )
    await call.answer()


# ─── 🔍 ФИНАНСОВЫЙ ДЕТЕКТИВ ───────────────────────────────────────────────────

@router.callback_query(F.data == "detective_start")
async def cb_detective_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(DetectiveState.answering)
    uid = call.from_user.id
    # Выбираем случайное дело, которое ещё не видел (или просто случайное)
    case_idx = _DETECTIVE_IDX.get(uid, 0) % len(DETECTIVE_CASES)
    await state.update_data(case_idx=case_idx)

    case = DETECTIVE_CASES[case_idx]
    await call.message.edit_text(
        f"🔍 <b>Финансовый детектив</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{case['title']}\n\n"
        f"{case['story']}",
        reply_markup=detective_kb(case_idx), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("det_"), DetectiveState.answering)
async def cb_detective_answer(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    case_idx, chosen = int(parts[1]), int(parts[2])
    data = await state.get_data()
    if data.get("case_idx") != case_idx:
        await call.answer()
        return

    await state.clear()
    case = DETECTIVE_CASES[case_idx]
    correct = case["ans"] == chosen
    uid = call.from_user.id

    if correct:
        iq_gain = 40
        add_iq(uid, iq_gain, "🔍")
        result_text = f"✅ <b>Верно, детектив!</b> +{iq_gain} IQ\n\n"
    else:
        iq_gain = 10
        add_iq(uid, iq_gain)
        result_text = f"❌ <b>Не совсем...</b> Правильный ответ: <i>{case['opts'][case['ans']]}</i>\n+{iq_gain} IQ за участие\n\n"

    _DETECTIVE_IDX[uid] = (case_idx + 1) % len(DETECTIVE_CASES)
    next_case_idx = _DETECTIVE_IDX[uid]

    await call.message.edit_text(
        f"🔍 <b>Финансовый детектив</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        + result_text
        + case["explanation"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text=f"➡️ Следующее дело", callback_data="detective_start")],
            [Btn(text="« Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="HTML"
    )
    await call.answer()


# ─── 📹 ВИДЕО-УРОКИ ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "videos_menu")
async def cb_videos_menu(call: CallbackQuery):
    uid = call.from_user.id
    watched = _VIDEO_WATCHED.get(uid, set())
    total = len(VIDEO_LESSONS)
    done = len(watched)
    await call.message.edit_text(
        f"📹 <b>Видео-уроки по финансам</b>\n\n"
        f"Прогресс: <b>{done}/{total}</b> уроков просмотрено\n"
        f"За каждый урок +20 IQ 🎓\n\n"
        "Выбери тему 👇",
        reply_markup=videos_menu_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("video_") & ~F.data.startswith("video_done_"))
async def cb_video_open(call: CallbackQuery):
    idx = int(call.data.split("_")[1])
    v = VIDEO_LESSONS[idx]
    uid = call.from_user.id
    watched = _VIDEO_WATCHED.get(uid, set())
    already = idx in watched
    status = "✅ Ты уже смотрел этот урок" if already else "👆 Нажми кнопку выше, посмотри видео, затем отметь"

    await call.message.edit_text(
        f"📹 <b>{v['title']}</b>  ·  {v['duration']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{v['desc']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{status}</i>",
        reply_markup=video_kb(idx), parse_mode="HTML",
        disable_web_page_preview=True
    )
    await call.answer()


@router.callback_query(F.data.startswith("video_done_"))
async def cb_video_done(call: CallbackQuery):
    idx = int(call.data.split("_")[2])
    uid = call.from_user.id
    if uid not in _VIDEO_WATCHED:
        _VIDEO_WATCHED[uid] = set()
    already = idx in _VIDEO_WATCHED[uid]
    if not already:
        _VIDEO_WATCHED[uid].add(idx)
        add_iq(uid, 20)
        iq_text = "+20 IQ начислено! 🎓"
    else:
        iq_text = "IQ за этот урок уже получен ✅"

    watched = _VIDEO_WATCHED[uid]
    total = len(VIDEO_LESSONS)
    done = len(watched)

    if done == total:
        add_iq(uid, 50, "🎓")
        bonus_text = "\n\n🏅 <b>Бонус: ты прошёл все уроки! +50 IQ и бейдж 🎓</b>"
    else:
        bonus_text = f"\nПросмотрено: {done}/{total} уроков"

    await call.answer(iq_text, show_alert=True)
    await call.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text=f"▶️ Смотреть снова на YouTube", url=VIDEO_LESSONS[idx]["url"])],
            [Btn(text="← Все уроки", callback_data="videos_menu")],
            [Btn(text="« Главное меню", callback_data="main_menu")],
        ])
    )


# ─── 🧬 ТЕСТ: КТО ТЫ КАК ИНВЕСТОР? ──────────────────────────────────────────

@router.callback_query(F.data == "investor_test_start")
async def cb_investor_test_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(InvestorTestState.answering)
    await state.update_data(q=0, scores={"C": 0, "M": 0, "A": 0, "S": 0})
    q = INVESTOR_TEST[0]
    await call.message.edit_text(
        f"🧬 <b>Тест: Кто ты как инвестор?</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Вопрос 1/{len(INVESTOR_TEST)}\n\n"
        f"<b>{q['q']}</b>",
        reply_markup=investor_test_kb(0), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("itest_"), InvestorTestState.answering)
async def cb_investor_test_answer(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    q_idx, inv_type = int(parts[1]), parts[2]
    data = await state.get_data()
    if data.get("q") != q_idx:
        await call.answer()
        return

    scores = data["scores"]
    scores[inv_type] = scores.get(inv_type, 0) + 1
    next_q = q_idx + 1

    if next_q >= len(INVESTOR_TEST):
        await state.clear()
        result_type = max(scores, key=scores.get)
        result = INVESTOR_TYPES[result_type]
        add_iq(call.from_user.id, 35, result["badge"])

        # Подробный разбор по всем типам
        breakdown = "  ".join(
            f"{INVESTOR_TYPES[t]['name'].split()[0]} {s}" for t, s in scores.items()
        )

        await call.message.edit_text(
            f"🧬 <b>Тест завершён!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Ты — <b>{result['name']}</b>\n\n"
            f"{result['desc']}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Распределение ответов: {breakdown}\n\n"
            f"⚡ +35 IQ и бейдж {result['badge']}!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [Btn(text="🔄 Пройти снова", callback_data="investor_test_start")],
                [Btn(text="🎰 Симулятор по моей стратегии", callback_data="invest_start")],
                [Btn(text="« Главное меню", callback_data="main_menu")],
            ]),
            parse_mode="HTML"
        )
    else:
        await state.update_data(q=next_q, scores=scores)
        q = INVESTOR_TEST[next_q]
        await call.message.edit_text(
            f"🧬 <b>Тест: Кто ты как инвестор?</b>\n"
            f"Вопрос {next_q + 1}/{len(INVESTOR_TEST)}\n\n"
            f"<b>{q['q']}</b>",
            reply_markup=investor_test_kb(next_q), parse_mode="HTML"
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
        "(аренда, еда, транспорт, коммуналка):\n\n<i>Например: 30000</i>",
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

    needs_ideal   = income * 0.50
    wants_ideal   = income * 0.30
    savings_ideal = income * 0.20
    needs_status  = "✅" if expenses <= needs_ideal else "⚠️"
    savings_pct   = round(remaining / income * 100)

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
        f"\n\n⚡ +30 IQ!\nТвой IQ: <b>{total_iq}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [Btn(text="💡 Советы по экономии", callback_data="tips_menu")],
            [Btn(text="« Главное меню", callback_data="main_menu")],
        ]),
        parse_mode="HTML"
    )


# ─── СОВЕТЫ И СЛОВАРЬ ──────────────────────────────────────────────────────────

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
