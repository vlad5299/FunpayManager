import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- Переменные окружения (бот стартует даже до добавления FUNPAY_GOLDEN_KEY) ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
FUNPAY_GOLDEN_KEY = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()
FALLBACK_COOLDOWN_SECONDS = max(10, int(os.getenv("FALLBACK_COOLDOWN_SECONDS", "60")))
POLL_DELAY_SECONDS = max(5.0, float(os.getenv("FUNPAY_POLL_DELAY", "7.0")))
MAX_RETRY_BACKOFF_SECONDS = max(30.0, float(os.getenv("FUNPAY_MAX_BACKOFF", "120.0")))
RUNNER_HANG_TIMEOUT_SECONDS = max(60.0, float(os.getenv("RUNNER_HANG_TIMEOUT", "120.0")))
CONNECTION_CHECK_INTERVAL = max(30.0, float(os.getenv("CONNECTION_CHECK_INTERVAL", "90.0")))
ALERT_COOLDOWN_SECONDS = max(60.0, float(os.getenv("ALERT_COOLDOWN_SECONDS", "300.0")))
AUTO_RAISE_ENABLED_DEFAULT = os.getenv("AUTO_RAISE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
AUTO_RAISE_INTERVAL_SECONDS = max(300, int(os.getenv("AUTO_RAISE_INTERVAL_SECONDS", "14400")))
AUTO_RAISE_CHECK_STEP_SECONDS = max(60.0, float(os.getenv("AUTO_RAISE_CHECK_STEP_SECONDS", "180.0")))


def _parse_silence_seconds(raw_val: Optional[str], default_sec: int = 900) -> int:
    s = (raw_val or "").strip().lower()
    if not s:
        return default_sec
    try:
        if s.endswith(("m", "min", "mins", "мин", "м")):
            num = float(re.sub(r"[a-zа-я]+$", "", s).strip())
            return max(30, int(num * 60))
        if s.endswith(("h", "hr", "hrs", "ч")):
            num = float(re.sub(r"[a-zа-я]+$", "", s).strip())
            return max(30, int(num * 3600))
        if s.endswith(("s", "sec", "secs", "сек", "с")):
            num = float(re.sub(r"[a-zа-я]+$", "", s).strip())
            return max(30, int(num))
        return max(30, int(float(s)))
    except Exception:
        return default_sec


MANUAL_SILENCE_SECONDS = _parse_silence_seconds(os.getenv("MANUAL_SILENCE_SECONDS"), 900)
FUNPAY_USER_AGENT = os.getenv(
    "FUNPAY_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
).strip()

BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent": FUNPAY_USER_AGENT,
    "Referer": "https://funpay.com/",
}

DEFAULT_FALLBACK_REPLY = (
    "Здравствуйте! Если в описании товара указана автоматическая доставка, "
    "вы можете оплатить заказ сразу, не дожидаясь продавца. В остальных случаях "
    "продавец ответит в течение 15 минут. Рабочее время: 7:00–22:00."
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("funpay-manager")

import inspect
import funpaybotengine
from funpaybotengine import AioHttpSession, Bot, Dispatcher, Router

try:
    from funpaybotengine import enums as _fpe_enums
except Exception:
    _fpe_enums = None
try:
    from FunPayAPI.common import enums as _fpapi_enums
except Exception:
    _fpapi_enums = None
enums = _fpe_enums or _fpapi_enums

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
FUNPAY_LOG_LEVEL_NAME = os.getenv("FUNPAY_LOG_LEVEL", "INFO").upper().strip()
FUNPAY_LOG_LEVEL = getattr(logging, FUNPAY_LOG_LEVEL_NAME, logging.INFO)
logging.getLogger("funpaybotengine").setLevel(FUNPAY_LOG_LEVEL)
logging.getLogger("FunPayAPI").setLevel(FUNPAY_LOG_LEVEL)

RULES_FILE = Path("autoresponder_rules.json")
MAX_SEEN_IDS = 5000
MAX_RECENT_CHATS = 12

SLANG_AND_LATIN_MAP: Dict[str, str] = {
    "чё": "что",
    "чо": "что",
    "че": "что",
    "скок": "сколько",
    "скока": "сколько",
    "ща": "сейчас",
    "щас": "сейчас",
    "мб": "может быть",
    "хз": "не знаю",
    "пж": "пожалуйста",
    "плз": "пожалуйста",
    "норм": "нормально",
    "имба": "отлично",
    "база": "отлично",
    "спс": "спасибо",
    "пасиб": "спасибо",
    "сенкс": "спасибо",
    "thx": "спасибо",
    "thanks": "спасибо",
    "spasibo": "спасибо",
    "skolko": "сколько",
    "privet": "привет",
    "tovar est": "товар есть",
    "tovar": "товар",
    "est": "есть",
    "куку": "ку",
    "ку-ку": "ку",
    "угу": "ага",
    "окей": "ок",
    "пон": "понял",
    "робит": "работает",
    "пашет": "работает",
    "рефанд": "возврат",
    "кидалово": "скам",
    "мошенник": "скам",
    "развод": "скам",
}

PRIORITY_RULE_IDS: List[str] = [
    "rule_25",
    "rule_26",
    "rule_27",
    "rule_17",
    "rule_16",
    "rule_24",
]

ACK_ONLY_RULE_IDS: Set[str] = {"rule_33"}

DEFAULT_RULES_DATA: List[Dict[str, Any]] = [
    {
        "id": "rule_01",
        "title": "Как дела?",
        "enabled": True,
        "keywords": ["как дела", "как сам", "ну как дела", "че как", "что как", "как поживаешь"],
        "reply": "Всё хорошо, спасибо! Чем могу помочь?",
    },
    {
        "id": "rule_02",
        "title": "Ку",
        "enabled": True,
        "keywords": ["ку", "ку здарова", "здарова"],
        "reply": "Здравствуйте! Чем могу помочь?",
    },
    {
        "id": "rule_03",
        "title": "Пока",
        "enabled": True,
        "keywords": ["пока", "до свидания", "бывай", "удачи", "все давай", "всё давай"],
        "reply": "До свидания! Хорошего дня!",
    },
    {
        "id": "rule_04",
        "title": "Проверка наличия",
        "enabled": True,
        "keywords": ["товар есть", "есть сейчас", "актуально", "продаешь", "продаёшь", "актуалка"],
        "reply": "Да, товар доступен к покупке. Можете оформить заказ.",
    },
    {
        "id": "rule_05",
        "title": "Вопрос о конкретном количестве",
        "enabled": True,
        "keywords": ["сколько осталось", "есть несколько", "можно два", "есть еще", "есть ещё"],
        "reply": "Уточню наличие нужного количества и сообщу вам.",
    },
    {
        "id": "rule_06",
        "title": "Когда появится товар",
        "enabled": True,
        "keywords": ["когда будет", "ожидается ли пополнение", "пополнение", "скоро завезешь", "скоро завезёшь"],
        "reply": "Пока не могу назвать точное время пополнения. Можете написать позже.",
    },
    {
        "id": "rule_07",
        "title": "Вопрос о цене",
        "enabled": True,
        "keywords": ["сколько стоит", "почем", "почём", "цена", "что по деньгам"],
        "reply": "Актуальная цена указана в карточке товара.",
    },
    {
        "id": "rule_08",
        "title": "Просьба о скидке",
        "enabled": True,
        "keywords": ["скидка", "скидос", "дешевле можно", "уступишь", "скинешь цену", "а дешевле", "дешевле"],
        "reply": "Спасибо за интерес! Пока цена остаётся указанной в объявлении.",
    },
    {
        "id": "rule_09",
        "title": "Торг",
        "enabled": True,
        "keywords": ["торг уместен", "торг", "заберу дешевле", "за сколько отдашь"],
        "reply": "Стоимость указана в объявлении. Другие условия пока не предусмотрены.",
    },
    {
        "id": "rule_10",
        "title": "Сравнение цен",
        "enabled": True,
        "keywords": ["у других дешевле", "почему так дорого", "нашел дешевле", "нашёл дешевле"],
        "reply": "Понимаю. Вы можете сравнить предложения и выбрать наиболее подходящее.",
    },
    {
        "id": "rule_11",
        "title": "Намерение купить",
        "enabled": True,
        "keywords": ["беру", "забираю", "покупаю", "го", "оформляю"],
        "reply": "Отлично! Можете оформить заказ через FunPay.",
    },
    {
        "id": "rule_12",
        "title": "Как купить",
        "enabled": True,
        "keywords": ["куда нажать", "как заказать", "как оформить", "что делать"],
        "reply": "Откройте карточку товара, выберите подходящий вариант и оформите заказ.",
    },
    {
        "id": "rule_13",
        "title": "Покупатель передумал",
        "enabled": True,
        "keywords": ["передумал", "не надо", "отмена", "не буду брать"],
        "reply": "Хорошо, если появятся вопросы — обращайтесь.",
    },
    {
        "id": "rule_14",
        "title": "Вопрос о способе оплаты",
        "enabled": True,
        "keywords": ["как оплатить", "чем платить", "какие способы оплаты", "способы оплаты"],
        "reply": "Доступные способы оплаты отображаются при оформлении заказа на FunPay.",
    },
    {
        "id": "rule_15",
        "title": "Покупатель сообщает, что оплатил",
        "enabled": True,
        "keywords": ["оплатил", "деньги отправил", "кинул оплату"],
        "reply": "Спасибо! Проверю статус заказа.",
    },
    {
        "id": "rule_16",
        "title": "Оплата не проходит",
        "enabled": True,
        "keywords": ["не могу оплатить", "ошибка оплаты", "платеж не проходит", "платёж не проходит"],
        "reply": "Попробуйте проверить способ оплаты и статус заказа. Если ошибка повторяется, сообщите мне.",
    },
    {
        "id": "rule_17",
        "title": "Деньги списались, заказа нет",
        "enabled": True,
        "keywords": ["деньги сняли", "оплата прошла а заказа нет", "списало деньги", "заказа нет"],
        "reply": "Не переживайте, я проверю ситуацию. Пока не оплачивайте заказ повторно.",
    },
    {
        "id": "rule_18",
        "title": "Сколько ждать",
        "enabled": True,
        "keywords": ["сколько ждать", "долго", "когда выдача", "через сколько придет", "через сколько придёт"],
        "reply": "Срок выдачи указан в описании товара. Если заказ уже оформлен, проверю его статус.",
    },
    {
        "id": "rule_19",
        "title": "Где товар",
        "enabled": True,
        "keywords": ["где товар", "ничего нет", "не пришло", "жду выдачу"],
        "reply": "Проверю статус вашего заказа и уточню ситуацию.",
    },
    {
        "id": "rule_20",
        "title": "Как получить товар",
        "enabled": True,
        "keywords": ["как забрать", "куда придет", "куда придёт", "где получить", "что дальше"],
        "reply": "Инструкция по получению указана в описании товара. Если возникнут сложности, напишите мне.",
    },
    {
        "id": "rule_21",
        "title": "Товар не работает",
        "enabled": True,
        "keywords": ["не работает", "ошибка", "баг", "глюк", "не запускается"],
        "reply": "Опишите, пожалуйста, проблему подробнее. Я проверю ситуацию.",
    },
    {
        "id": "rule_22",
        "title": "Получен не тот товар",
        "enabled": True,
        "keywords": ["не то выдал", "другой товар", "не соответствует описанию"],
        "reply": "Пришлите, пожалуйста, описание проблемы. Я проверю заказ.",
    },
    {
        "id": "rule_23",
        "title": "Товар не подходит",
        "enabled": True,
        "keywords": ["не подходит", "не могу использовать"],
        "reply": "Уточните, пожалуйста, что именно не получается.",
    },
    {
        "id": "rule_24",
        "title": "Покупатель недоволен",
        "enabled": True,
        "keywords": ["ужас", "плохо", "фигня", "разочарован", "недоволен"],
        "reply": "Сожалею, что возникла такая ситуация. Передам ваше сообщение администратору.",
    },
    {
        "id": "rule_25",
        "title": "Просьба вернуть деньги",
        "enabled": True,
        "keywords": ["верни деньги", "возврат", "хочу обратно деньги"],
        "reply": "Ваше обращение будет передано администратору для рассмотрения.",
    },
    {
        "id": "rule_26",
        "title": "Угроза жалобой",
        "enabled": True,
        "keywords": ["пожалуюсь", "напишу жалобу", "репорт", "поддержка", "арбитраж"],
        "reply": "Понимаю ваше недовольство. Передам обращение администратору.",
    },
    {
        "id": "rule_27",
        "title": "Обвинение в обмане",
        "enabled": True,
        "keywords": ["скам", "мошенник"],
        "reply": "Мне жаль, что у вас сложилось такое впечатление. Передам ситуацию администратору.",
    },
    {
        "id": "rule_28",
        "title": "Продавец на месте",
        "enabled": True,
        "keywords": ["ты тут", "ау", "алло", "онлайн", "продавец где"],
        "reply": "Да, я на связи. Чем могу помочь?",
    },
    {
        "id": "rule_29",
        "title": "Давно нет ответа",
        "enabled": True,
        "keywords": ["игноришь", "почему молчишь", "ты живой"],
        "reply": "Извините за ожидание! Я получил ваше сообщение.",
    },
    {
        "id": "rule_30",
        "title": "Кто отвечает",
        "enabled": True,
        "keywords": ["бот", "это автомат", "ты человек", "кто отвечает"],
        "reply": "Я автоматический помощник продавца. Если понадобится, передам сообщение продавцу.",
    },
    {
        "id": "rule_31",
        "title": "Благодарность",
        "enabled": True,
        "keywords": ["спасибо", "респект"],
        "reply": "Пожалуйста! Рад помочь.",
    },
    {
        "id": "rule_32",
        "title": "Положительная оценка",
        "enabled": True,
        "keywords": ["красава", "отлично", "все супер", "всё супер"],
        "reply": "Спасибо за отзыв! Приятно, что всё понравилось.",
    },
    {
        "id": "rule_33",
        "title": "Подтверждение",
        "enabled": True,
        "keywords": ["ок", "ага", "понял", "ясно"],
        "reply": "Хорошо!",
    },
]


def collapse_repeats(text: str) -> str:
    return re.sub(r"([а-яёa-z])\1{2,}", r"\1", text)


def normalize_text(raw: str) -> str:
    text = (raw or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    tokens: List[str] = []
    for tok in text.split(" "):
        clean_tok = collapse_repeats(tok)
        mapped = SLANG_AND_LATIN_MAP.get(clean_tok, clean_tok)
        tokens.extend(mapped.split())
    return re.sub(r"\s+", " ", " ".join(tokens)).strip()


def damerau_levenshtein_within(a: str, b: str, max_dist: int) -> bool:
    if abs(len(a) - len(b)) > max_dist:
        return False
    if a == b:
        return True
    len_a, len_b = len(a), len(b)
    dp = [[0] * (len_b + 1) for _ in range(len_a + 1)]
    for i in range(len_a + 1):
        dp[i][0] = i
    for j in range(len_b + 1):
        dp[0][j] = j
    for i in range(1, len_a + 1):
        row_min = max_dist + 1
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            val = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                val = min(val, dp[i - 2][j - 2] + 1)
            dp[i][j] = val
            if val < row_min:
                row_min = val
        if row_min > max_dist:
            return False
    return dp[len_a][len_b] <= max_dist


def _max_word_dist(word_len: int) -> int:
    if word_len <= 3:
        return 0
    if word_len <= 5:
        return 1
    return 1


def keyword_score(norm_text: str, norm_kw: str) -> int:
    if not norm_text or not norm_kw:
        return 0
    txt_words = [w for w in norm_text.split(" ") if w]
    if not txt_words:
        return 0

    if " " not in norm_kw:
        max_dist = _max_word_dist(len(norm_kw))
        best = 0
        for word in txt_words:
            if word == norm_kw:
                best = max(best, 100 + len(norm_kw))
            elif max_dist > 0 and damerau_levenshtein_within(word, norm_kw, max_dist):
                best = max(best, 60 + len(norm_kw))
        return best

    if f" {norm_kw} " in f" {norm_text} ":
        return 150 + len(norm_kw)

    kw_words = [w for w in norm_kw.split(" ") if w]
    if len(txt_words) >= len(kw_words):
        for i in range(len(txt_words) - len(kw_words) + 1):
            ok = True
            exact_all = True
            for kw_w, tx_w in zip(kw_words, txt_words[i : i + len(kw_words)]):
                if tx_w == kw_w:
                    continue
                exact_all = False
                d = _max_word_dist(len(kw_w))
                if d <= 0 or not damerau_levenshtein_within(tx_w, kw_w, d):
                    ok = False
                    break
            if ok:
                return (140 if exact_all else 90) + len(norm_kw)
    return 0


@dataclass
class AutoReplyRule:
    id: str
    title: str
    keywords: List[str]
    reply: str
    enabled: bool = True


@dataclass
class ChatSummary:
    chat_id: int
    buyer_username: str
    last_message: str
    order_info: Optional[str]
    timestamp: float
    last_rule_id: Optional[str] = None
    last_reply_text: Optional[str] = None
    last_ack_at: Optional[float] = None


class BotState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.auto_reply_enabled: bool = True
        self.funpay_connected: bool = False
        self.funpay_username: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_poll_at: Optional[float] = None
        self.last_ok_poll_at: Optional[float] = None
        self.last_conn_check_at: Optional[float] = None
        self.connection_status: str = "Ожидание запуска"
        self.diagnosis_code: str = "init"
        self.diagnosis_detail: str = "Ожидание первого опроса"
        self.consecutive_poll_errors: int = 0
        self.current_backoff: float = POLL_DELAY_SECONDS
        self.hang_restarts: int = 0
        self.seen_message_ids: deque[str] = deque(maxlen=MAX_SEEN_IDS)
        self.seen_set: set[str] = set()
        self.auto_replied_keys: deque[str] = deque(maxlen=MAX_SEEN_IDS)
        self.auto_replied_set: set[str] = set()
        self.fallback_last_sent: Dict[int, float] = {}
        self.manual_reply_last_at: Dict[int, float] = {}
        self.disabled_chats: Set[int] = set()
        self.auto_raise_enabled: bool = AUTO_RAISE_ENABLED_DEFAULT
        self.last_raise_at: Optional[float] = None
        self.last_raise_summary: str = "Ещё не выполнялось"
        self.total_raised_count: int = 0
        self.recent_chats: List[ChatSummary] = []
        self.last_chat_id: Optional[int] = None
        self.rules: List[AutoReplyRule] = []
        self.pending_input: Dict[int, Dict[str, Any]] = {}
        self._load_or_init_state()

    def _build_default_rules(self) -> List[AutoReplyRule]:
        built: List[AutoReplyRule] = []
        for item in DEFAULT_RULES_DATA:
            built.append(
                AutoReplyRule(
                    id=str(item["id"]),
                    title=str(item["title"]),
                    keywords=[normalize_text(k) for k in item["keywords"] if normalize_text(k)],
                    reply=str(item["reply"]),
                    enabled=bool(item.get("enabled", True)),
                )
            )
        return built

    def _load_or_init_state(self) -> None:
        if not RULES_FILE.exists():
            self.rules = self._build_default_rules()
            self.auto_reply_enabled = True
            self.auto_raise_enabled = AUTO_RAISE_ENABLED_DEFAULT
            self.disabled_chats = set()
            self.save_rules()
            return

        try:
            raw = json.loads(RULES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.auto_reply_enabled = bool(raw.get("auto_reply_enabled", True))
                self.auto_raise_enabled = bool(raw.get("auto_raise_enabled", AUTO_RAISE_ENABLED_DEFAULT))
                raw_disabled = raw.get("disabled_chats", [])
                self.disabled_chats = {
                    int(cid) for cid in raw_disabled if str(cid).lstrip("-").isdigit()
                } if isinstance(raw_disabled, list) else set()
                raw_rules = raw.get("rules", [])
            elif isinstance(raw, list):
                self.auto_reply_enabled = True
                self.auto_raise_enabled = AUTO_RAISE_ENABLED_DEFAULT
                self.disabled_chats = set()
                raw_rules = raw
            else:
                self.rules = self._build_default_rules()
                return

            rules: List[AutoReplyRule] = []
            for idx, item in enumerate(raw_rules, start=1):
                if isinstance(item, dict) and "keywords" in item and "reply" in item:
                    rid = str(item.get("id") or f"rule_{idx:02d}")
                    title = str(item.get("title") or f"Правило #{idx}")
                    kws = [normalize_text(k) for k in item["keywords"] if normalize_text(k)]
                    rules.append(
                        AutoReplyRule(
                            id=rid,
                            title=title,
                            keywords=kws,
                            reply=str(item["reply"]),
                            enabled=bool(item.get("enabled", True)),
                        )
                    )
            self.rules = rules if rules else self._build_default_rules()
        except Exception as exc:
            log.error("Не удалось загрузить правила автоответчика: %s", type(exc).__name__)
            self.rules = self._build_default_rules()

    def save_rules(self) -> None:
        with self.lock:
            payload = {
                "auto_reply_enabled": self.auto_reply_enabled,
                "auto_raise_enabled": self.auto_raise_enabled,
                "disabled_chats": sorted(self.disabled_chats),
                "rules": [asdict(r) for r in self.rules],
            }
        try:
            RULES_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.error("Не удалось сохранить правила автоответчика: %s", type(exc).__name__)

    def is_chat_disabled(self, chat_id: int) -> bool:
        with self.lock:
            return int(chat_id) in self.disabled_chats

    def set_chat_disabled(self, chat_id: int, disabled: bool) -> bool:
        cid = int(chat_id)
        with self.lock:
            if disabled:
                self.disabled_chats.add(cid)
            else:
                self.disabled_chats.discard(cid)
                self.manual_reply_last_at.pop(cid, None)
            now_disabled = cid in self.disabled_chats
        self.save_rules()
        return now_disabled

    def toggle_chat_disabled(self, chat_id: int) -> bool:
        cid = int(chat_id)
        with self.lock:
            if cid in self.disabled_chats:
                self.disabled_chats.discard(cid)
                self.manual_reply_last_at.pop(cid, None)
                now_disabled = False
            else:
                self.disabled_chats.add(cid)
                now_disabled = True
        self.save_rules()
        return now_disabled

    def mark_seen_if_new(self, msg_key: str) -> bool:
        with self.lock:
            if msg_key in self.seen_set:
                return False
            if len(self.seen_message_ids) == self.seen_message_ids.maxlen:
                oldest = self.seen_message_ids.popleft()
                self.seen_set.discard(oldest)
            self.seen_message_ids.append(msg_key)
            self.seen_set.add(msg_key)
            return True

    def mark_auto_replied_if_new(self, reply_key: str) -> bool:
        with self.lock:
            if reply_key in self.auto_replied_set:
                return False
            if len(self.auto_replied_keys) == self.auto_replied_keys.maxlen:
                oldest = self.auto_replied_keys.popleft()
                self.auto_replied_set.discard(oldest)
            self.auto_replied_keys.append(reply_key)
            self.auto_replied_set.add(reply_key)
            return True

    def claim_fallback_slot(self, chat_id: int, cooldown_sec: int = FALLBACK_COOLDOWN_SECONDS) -> Tuple[bool, int]:
        now = time.time()
        with self.lock:
            if not self.auto_reply_enabled or int(chat_id) in self.disabled_chats:
                return False, 0
            last_ts = self.fallback_last_sent.get(chat_id)
            if last_ts is not None:
                elapsed = now - last_ts
                if elapsed < cooldown_sec:
                    return False, max(1, int(cooldown_sec - elapsed))
            self.fallback_last_sent[chat_id] = now
            if len(self.fallback_last_sent) > 2000:
                cutoff = now - max(cooldown_sec * 4, 3600)
                self.fallback_last_sent = {
                    cid: ts for cid, ts in self.fallback_last_sent.items() if ts >= cutoff
                }
            return True, 0

    def mark_manual_reply(self, chat_id: int) -> None:
        now = time.time()
        with self.lock:
            self.manual_reply_last_at[int(chat_id)] = now
            if len(self.manual_reply_last_at) > 2000:
                cutoff = now - max(MANUAL_SILENCE_SECONDS * 4, 3600)
                self.manual_reply_last_at = {
                    cid: ts for cid, ts in self.manual_reply_last_at.items() if ts >= cutoff
                }

    def get_manual_silence_remaining(self, chat_id: int, silence_sec: int = MANUAL_SILENCE_SECONDS) -> int:
        now = time.time()
        with self.lock:
            last_ts = self.manual_reply_last_at.get(int(chat_id))
            if last_ts is None:
                return 0
            elapsed = now - last_ts
            if elapsed < silence_sec:
                return max(1, int(silence_sec - elapsed))
            return 0

    def record_chat(self, chat_id: int, buyer_username: str, text: str, order_info: Optional[str]) -> None:
        with self.lock:
            self.last_chat_id = chat_id
            prev = next((c for c in self.recent_chats if c.chat_id == chat_id), None)
            summary = ChatSummary(
                chat_id=chat_id,
                buyer_username=buyer_username or f"Чат #{chat_id}",
                last_message=text,
                order_info=order_info,
                timestamp=time.time(),
                last_rule_id=prev.last_rule_id if prev else None,
                last_reply_text=prev.last_reply_text if prev else None,
                last_ack_at=prev.last_ack_at if prev else None,
            )
            self.recent_chats = [c for c in self.recent_chats if c.chat_id != chat_id]
            self.recent_chats.insert(0, summary)
            self.recent_chats = self.recent_chats[:MAX_RECENT_CHATS]

    def update_chat_auto_meta(
        self,
        chat_id: int,
        rule_id: Optional[str] = None,
        reply_text: Optional[str] = None,
        ack_ts: Optional[float] = None,
    ) -> None:
        with self.lock:
            for c in self.recent_chats:
                if c.chat_id == chat_id:
                    if rule_id is not None:
                        c.last_rule_id = rule_id
                    if reply_text is not None:
                        c.last_reply_text = reply_text
                    if ack_ts is not None:
                        c.last_ack_at = ack_ts
                    break

    def get_chat(self, chat_id: int) -> Optional[ChatSummary]:
        with self.lock:
            for c in self.recent_chats:
                if c.chat_id == chat_id:
                    return c
            return None

    def evaluate_message(self, chat_id: int, text: str) -> Tuple[Optional[str], List[AutoReplyRule], bool]:
        raw = (text or "").strip()
        if not raw or raw.startswith("[вложение"):
            return None, [], True

        norm = normalize_text(raw)
        if not norm:
            return None, [], True

        with self.lock:
            if not self.auto_reply_enabled or int(chat_id) in self.disabled_chats:
                return None, [], False
            enabled_rules = [r for r in self.rules if r.enabled]
            chat_ctx = next((c for c in self.recent_chats if c.chat_id == chat_id), None)
            prev_rule_id = chat_ctx.last_rule_id if chat_ctx else None
            last_reply_text = chat_ctx.last_reply_text if chat_ctx else None
            last_ack_at = chat_ctx.last_ack_at if chat_ctx else None

        if norm in {"а дешевле", "дешевле", "почем", "сколько"} and prev_rule_id == "rule_07":
            disc_rule = next((r for r in enabled_rules if r.id == "rule_08"), None)
            if disc_rule and disc_rule.reply.strip() != (last_reply_text or "").strip():
                return disc_rule.reply.strip(), [disc_rule], False

        scored: List[Tuple[int, int, AutoReplyRule]] = []
        for idx, r in enumerate(enabled_rules):
            best_s = 0
            for kw in r.keywords:
                s = keyword_score(norm, kw)
                if s > best_s:
                    best_s = s
            if best_s > 0:
                prio_bonus = 500 if r.id in PRIORITY_RULE_IDS else 0
                scored.append((prio_bonus + best_s, -idx, r))

        if not scored:
            return None, [], True

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        matched = [item[2] for item in scored]

        priority_hits = [r for r in matched if r.id in PRIORITY_RULE_IDS]
        if priority_hits:
            for chosen in priority_hits:
                rep = chosen.reply.strip()
                if rep and rep != (last_reply_text or "").strip():
                    return rep, [chosen], False
            return None, [priority_hits[0]], False

        if len(matched) == 1 and matched[0].id in ACK_ONLY_RULE_IDS:
            now = time.time()
            if last_ack_at is not None and (now - last_ack_at) < 180:
                return None, matched, False
            rep = matched[0].reply.strip()
            if not rep or rep == (last_reply_text or "").strip():
                return None, matched, False
            return rep, matched, False

        for chosen in matched:
            rep = chosen.reply.strip()
            if rep and rep != (last_reply_text or "").strip():
                return rep, [chosen], False

        return None, matched, False


state = BotState()
funpay_account: Optional[Any] = None
active_runner: Optional[Any] = None
funpay_loop: Optional[asyncio.AbstractEventLoop] = None
funpay_lock = threading.Lock()
worker_started = False
worker_start_lock = threading.Lock()
last_alert_lock = threading.Lock()
last_alert_meta: Dict[str, float] = {}


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def escape_html(value: Optional[str]) -> str:
    if not value:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def sanitize_error_text(exc: Exception) -> str:
    msg = str(exc)
    if TOKEN and TOKEN in msg:
        msg = msg.replace(TOKEN, "[HIDDEN_TOKEN]")
    if FUNPAY_GOLDEN_KEY and FUNPAY_GOLDEN_KEY in msg:
        msg = msg.replace(FUNPAY_GOLDEN_KEY, "[HIDDEN_KEY]")
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def classify_funpay_issue(exc: Exception) -> Tuple[str, str, str]:
    detail = sanitize_error_text(exc)
    low = detail.lower()

    if "unauthorized" in low or "401" in low or "golden_key" in low or "login" in low or "auth" in low:
        return (
            "auth",
            f"Сессия/ключ FunPay не принят ({detail})",
            "Проверьте FUNPAY_GOLDEN_KEY и не заходил ли кто-то в аккаунт с другого устройства.",
        )
    if "429" in low or "too many requests" in low or "rate limit" in low:
        return (
            "rate_limit",
            f"Лимит запросов HTTP 429 со стороны FunPay ({detail})",
            "Увеличьте FUNPAY_POLL_DELAY или снизьте частоту опросов/запросов.",
        )
    if "403" in low or "forbidden" in low or "cloudflare" in low or "captcha" in low or "капч" in low:
        return (
            "ip_block",
            f"Возможная блокировка IP / защита / капча HTTP 403 ({detail})",
            "FunPay может ограничивать IP-адрес сервера/хостинга при прямом подключении.",
        )
    if (
        "timeout" in low
        or "timed out" in low
        or "connection" in low
        or "reset" in low
        or "network" in low
        or "ssl" in low
        or "chunked" in low
    ):
        return (
            "net_timeout",
            f"Сетевой тайм-аут или обрыв соединения ({detail})",
            "Временный сбой сети между сервером и FunPay или долгий ответ площадки.",
        )
    return (
        "poll_error",
        f"Ошибка при получении событий ({detail})",
        "Проверьте логи деплоя для уточнения деталей.",
    )


def compute_backoff_seconds(error_count: int) -> float:
    if error_count <= 0:
        return POLL_DELAY_SECONDS
    delay = POLL_DELAY_SECONDS * (2 ** min(error_count - 1, 5))
    return min(MAX_RETRY_BACKOFF_SECONDS, max(POLL_DELAY_SECONDS, delay))


def _build_session(golden_key: str) -> Optional[Any]:
    headers = {
        "User-Agent": FUNPAY_USER_AGENT,
        "Referer": "https://funpay.com/",
    }
    for kwargs in (
        {"golden_key": golden_key, "headers": headers, "user_agent": FUNPAY_USER_AGENT},
        {"golden_key": golden_key, "headers": headers},
        {"golden_key": golden_key},
        {"headers": headers},
        {},
    ):
        try:
            return AioHttpSession(**kwargs)
        except TypeError:
            continue
    return None


async def _create_and_start_bot(golden_key: str) -> Any:
    session = _build_session(golden_key)
    bot_obj: Optional[Any] = None
    for kwargs in (
        {"golden_key": golden_key, "session": session},
        {"golden_key": golden_key},
        {"token": golden_key, "session": session},
        {"token": golden_key},
    ):
        try:
            bot_obj = Bot(**kwargs)
            break
        except TypeError:
            continue
    if bot_obj is None:
        bot_obj = Bot(golden_key)

    for method_name in ("update", "get", "fetch", "init", "start", "login"):
        fn = getattr(bot_obj, method_name, None)
        if callable(fn):
            res = fn()
            if inspect.isawaitable(res):
                await res
            break
    return bot_obj


def inspect_funpay_diagnostics(acc: Optional[Any], runner: Optional[Any]) -> str:
    parts: List[str] = ["engine=funpaybotengine(Bot/Dispatcher)"]
    if acc is not None:
        parts.append(f"bot_type={type(acc).__name__}")
        parts.append(f"id={getattr(acc, 'id', None)}")
        parts.append(f"username={getattr(acc, 'username', None)}")
    if runner is not None:
        parts.append(f"dp_type={type(runner).__name__}")
    return ", ".join(parts)


def probe_funpay_connection(acc: Optional[Any]) -> Tuple[bool, str]:
    if acc is None:
        return False, "Бот FunPay не инициализирован"
    return True, "FunPay Bot активен"


def stop_active_runner_safely() -> None:
    with funpay_lock:
        dp = active_runner
        bot_obj = funpay_account
        loop = funpay_loop
    if loop is None or loop.is_closed():
        return
    for obj in (dp, bot_obj):
        if obj is None:
            continue
        for method_name in ("stop_polling", "stop", "close"):
            fn = getattr(obj, method_name, None)
            if callable(fn):
                try:
                    res = fn()
                    if inspect.isawaitable(res):
                        asyncio.run_coroutine_threadsafe(res, loop)
                except Exception:
                    pass


async def safe_edit_text(message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if message is None:
        return
    try:
        await message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        log.warning("Не удалось отредактировать сообщение: %s", exc)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    auto_label = "🟢 Автоответчик: ВКЛ" if state.auto_reply_enabled else "🔴 Автоответчик: ВЫКЛ"
    raise_label = "📈 Автоподнятие: ВКЛ" if state.auto_raise_enabled else "📉 Автоподнятие: ВЫКЛ"
    rows = [
        [
            InlineKeyboardButton("📊 Статус", callback_data="menu:status"),
            InlineKeyboardButton("🧪 Тест", callback_data="menu:test"),
        ],
        [
            InlineKeyboardButton("✉️ Ответить", callback_data="menu:reply"),
            InlineKeyboardButton(auto_label, callback_data="menu:toggle_auto"),
        ],
        [
            InlineKeyboardButton(raise_label, callback_data="menu:toggle_raise"),
            InlineKeyboardButton("🚀 Поднять лоты", callback_data="menu:raise_now"),
        ],
        [
            InlineKeyboardButton("🔕 Чаты (выкл/вкл)", callback_data="menu:chat_silence"),
            InlineKeyboardButton("📋 Правила автоответа", callback_data="menu:rules"),
        ],
        [
            InlineKeyboardButton("❓ Помощь", callback_data="menu:help"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def _async_send_funpay_message(bot_obj: Any, chat_id: int, clean_text: str) -> None:
    targets: List[Any] = [bot_obj]
    for sub_attr in ("client", "session", "api", "funpay", "account", "http"):
        sub = getattr(bot_obj, sub_attr, None)
        if sub is not None and sub not in targets:
            targets.append(sub)
    with funpay_lock:
        if active_runner is not None and active_runner not in targets:
            targets.append(active_runner)

    method_names = (
        "send_message",
        "send_msg",
        "send_chat_message",
        "post_message",
        "send",
    )

    last_exc: Optional[Exception] = None
    tried_any = False

    for target in targets:
        for m_name in method_names:
            send_fn = getattr(target, m_name, None)
            if not callable(send_fn):
                continue
            tried_any = True
            sig_params: List[str] = []
            try:
                sig = inspect.signature(send_fn)
                sig_params = list(sig.parameters.keys())
            except Exception:
                sig_params = []

            call_variants: List[Tuple[tuple, dict]] = []
            if "chat_id" in sig_params and "text" in sig_params:
                call_variants.append(((), {"chat_id": chat_id, "text": clean_text}))
            if "chat_id" in sig_params and "message" in sig_params:
                call_variants.append(((), {"chat_id": chat_id, "message": clean_text}))
            if "node_id" in sig_params and "text" in sig_params:
                call_variants.append(((), {"node_id": chat_id, "text": clean_text}))

            call_variants.extend([
                ((), {"chat_id": chat_id, "text": clean_text}),
                ((), {"text": clean_text, "chat_id": chat_id}),
                ((), {"chat_id": chat_id, "message": clean_text}),
                ((chat_id, clean_text), {}),
                ((str(chat_id), clean_text), {}),
            ])

            for args_v, kwargs_v in call_variants:
                try:
                    res = send_fn(*args_v, **kwargs_v)
                    if inspect.isawaitable(res):
                        await res
                    log.info("Сообщение FunPay успешно отправлено в чат %s через %s.%s", chat_id, type(target).__name__, m_name)
                    return
                except TypeError as te:
                    last_exc = te
                    continue
                except Exception as exc:
                    last_exc = exc
                    log.warning(
                        "Неудачная попытка отправки через %s.%s: %s",
                        type(target).__name__,
                        m_name,
                        sanitize_error_text(exc),
                    )
                    break

    if not tried_any:
        raise RuntimeError(
            f"Не найден метод отправки сообщения в funpaybotengine (проверено объектов: {len(targets)})."
        )
    if last_exc is not None:
        raise RuntimeError(
            f"Все варианты вызова отправки отклонены ({type(last_exc).__name__}: {last_exc})."
        ) from last_exc


def send_funpay_message(chat_id: int, text: str) -> Tuple[bool, Optional[str]]:
    clean_text = (text or "").strip()
    if not clean_text:
        return False, "Пустое сообщение не отправлено."
    with funpay_lock:
        bot_obj = funpay_account
        loop = funpay_loop
    if bot_obj is None or loop is None or loop.is_closed():
        return False, "Подключение к FunPay ещё не установлено."
    try:
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(_async_send_funpay_message(bot_obj, int(chat_id), clean_text), timeout=12.0),
            loop,
        )
        fut.result(timeout=14.0)
        state.update_chat_auto_meta(int(chat_id), reply_text=clean_text)
        return True, None
    except Exception as exc:
        err = sanitize_error_text(exc)
        log.error("Ошибка отправки сообщения в чат %s: %s", chat_id, err)
        with state.lock:
            state.last_error = err
        return False, err


async def _call_raise_method(target: Any, method_name: str, *args: Any, **kwargs: Any) -> Tuple[bool, Any]:
    fn = getattr(target, method_name, None)
    if not callable(fn):
        return False, None
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        res = await res
    return True, res


def _extract_items_list(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    for attr in ("lots", "offers", "items", "categories", "nodes", "data"):
        sub = getattr(raw, attr, None)
        if isinstance(sub, dict):
            return list(sub.values())
        if isinstance(sub, (list, tuple, set)):
            return list(sub)
    return []


async def _async_raise_all_lots(bot_obj: Any) -> Tuple[bool, int, str]:
    targets = _collect_search_targets(bot_obj, dp=active_runner)
    raised_categories: List[str] = []
    errors: List[str] = []
    tried_any = False

    # 1) Прямые массовые методы поднятия всех лотов (если поддерживаются библиотекой)
    for target in targets:
        for bulk_name in ("raise_all_lots", "raise_lots", "boost_all_lots", "up_all_lots"):
            if not callable(getattr(target, bulk_name, None)):
                continue
            tried_any = True
            try:
                ok, res = await _call_raise_method(target, bulk_name)
                if ok:
                    cnt = len(res) if isinstance(res, (list, tuple, set, dict)) else (int(res) if isinstance(res, int) and res > 0 else 1)
                    return True, cnt, f"Выполнено массовое поднятие ({bulk_name})"
            except TypeError:
                pass
            except Exception as exc:
                errors.append(sanitize_error_text(exc))

    # 2) Получение списка лотов / категорий пользователя и поочерёдное поднятие
    lots: List[Any] = []
    for target in targets:
        for getter_name in (
            "get_my_lots",
            "get_user_lots",
            "get_lots",
            "get_offers",
            "get_my_offers",
            "fetch_lots",
            "lots",
        ):
            attr = getattr(target, getter_name, None)
            if attr is None:
                continue
            try:
                if callable(attr):
                    try:
                        res = attr()
                    except TypeError:
                        uid = getattr(bot_obj, "id", None)
                        if uid is None:
                            continue
                        res = attr(uid)
                    if inspect.isawaitable(res):
                        res = await res
                else:
                    res = attr
                extracted = _extract_items_list(res)
                if extracted:
                    lots = extracted
                    break
            except Exception as exc:
                errors.append(sanitize_error_text(exc))
        if lots:
            break

    seen_nodes: Set[str] = set()
    for lot in lots:
        if isinstance(lot, dict):
            node_id = lot.get("node_id") or lot.get("category_id") or lot.get("subcategory_id") or lot.get("id")
            game_id = lot.get("game_id") or lot.get("section_id")
            title = str(lot.get("title") or lot.get("name") or f"Категория {node_id}")
        else:
            node_id = (
                getattr(lot, "node_id", None)
                or getattr(lot, "category_id", None)
                or getattr(lot, "subcategory_id", None)
                or getattr(getattr(lot, "subcategory", None), "id", None)
                or getattr(getattr(lot, "category", None), "id", None)
                or getattr(lot, "id", None)
            )
            game_id = (
                getattr(lot, "game_id", None)
                or getattr(getattr(lot, "game", None), "id", None)
                or getattr(lot, "section_id", None)
            )
            title = str(
                getattr(lot, "subcategory_name", None)
                or getattr(lot, "category_name", None)
                or getattr(lot, "title", None)
                or getattr(lot, "name", None)
                or f"Категория {node_id}"
            )

        if node_id is None:
            continue
        dedup_node = f"{game_id}:{node_id}"
        if dedup_node in seen_nodes:
            continue
        seen_nodes.add(dedup_node)

        raised_this = False
        for target in targets:
            for m_name in ("raise_lots", "raise_lot", "raise_offer", "up_lots", "boost_lots", "raise_category"):
                fn = getattr(target, m_name, None)
                if not callable(fn):
                    continue
                tried_any = True
                variants: List[Tuple[tuple, dict]] = []
                if game_id is not None:
                    variants.extend([
                        ((game_id, node_id), {}),
                        ((), {"game_id": game_id, "node_id": node_id}),
                        ((), {"game_id": game_id, "category_id": node_id}),
                    ])
                variants.extend([
                    ((node_id,), {}),
                    ((), {"node_id": node_id}),
                    ((), {"category_id": node_id}),
                    ((), {"lot_id": node_id}),
                    ((lot,), {}),
                ])
                for a_v, kw_v in variants:
                    try:
                        res = fn(*a_v, **kw_v)
                        if inspect.isawaitable(res):
                            await res
                        raised_this = True
                        break
                    except TypeError:
                        continue
                    except Exception as exc:
                        err_txt = sanitize_error_text(exc)
                        if any(w in err_txt.lower() for w in ("подожд", "wait", "cooldown", "нельзя", "рано", "через")):
                            errors.append(f"{title}: кулдаун площадки")
                        else:
                            errors.append(f"{title}: {err_txt}")
                        break
                if raised_this:
                    break
            if raised_this:
                break

        if raised_this:
            raised_categories.append(title)
            await asyncio.sleep(1.5)

    if raised_categories:
        summary = f"Поднято категорий: {len(raised_categories)} ({', '.join(raised_categories[:5])})"
        return True, len(raised_categories), summary

    if not lots and not tried_any:
        return False, 0, "Не найдены лоты или метод поднятия в текущей версии funpaybotengine."

    if errors:
        return False, 0, "; ".join(dict.fromkeys(errors))[:300]

    return True, 0, "Все доступные категории проверены (сейчас на кулдауне FunPay)."


def trigger_raise_lots_sync() -> Tuple[bool, int, str]:
    with funpay_lock:
        bot_obj = funpay_account
        loop = funpay_loop
    if bot_obj is None or loop is None or loop.is_closed():
        return False, 0, "Подключение к FunPay ещё не установлено."
    try:
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(_async_raise_all_lots(bot_obj), timeout=45.0),
            loop,
        )
        ok, count, detail = fut.result(timeout=48.0)
        with state.lock:
            state.last_raise_at = time.time()
            state.last_raise_summary = detail
            if ok and count > 0:
                state.total_raised_count += count
        return ok, count, detail
    except Exception as exc:
        err = sanitize_error_text(exc)
        with state.lock:
            state.last_raise_at = time.time()
            state.last_raise_summary = f"Ошибка: {err}"
        return False, 0, err


def auto_raise_loop(tg_app: Application, loop: asyncio.AbstractEventLoop) -> None:
    time.sleep(20.0)
    while True:
        try:
            with state.lock:
                enabled = state.auto_raise_enabled
                fp_ok = state.funpay_connected
                last_ts = state.last_raise_at

            now = time.time()
            due = (last_ts is None) or ((now - last_ts) >= AUTO_RAISE_INTERVAL_SECONDS)

            if enabled and fp_ok and due:
                ok, count, detail = trigger_raise_lots_sync()
                if ok and count > 0:
                    notify_admin_sync(
                        tg_app,
                        loop,
                        f"🚀 <b>Автоподнятие лотов выполнено</b>\n"
                        f"📌 {escape_html(detail)}\n"
                        f"⏱ Следующая проверка через {AUTO_RAISE_INTERVAL_SECONDS // 60} мин.",
                        alert_key="auto_raise_ok",
                    )
                elif not ok:
                    log.warning("Автоподнятие лотов завершилось предупреждением: %s", detail)
        except Exception as exc:
            log.error("Ошибка в цикле auto_raise_loop: %s", sanitize_error_text(exc))

        time.sleep(AUTO_RAISE_CHECK_STEP_SECONDS)


def extract_order_info(account: Any, buyer_username: str) -> Optional[str]:
    try:
        if hasattr(account, "get_sells"):
            sells = account.get_sells()
            if inspect.isawaitable(sells):
                return None
            if sells:
                for order in sells:
                    order_buyer = getattr(order, "buyer_username", None)
                    if order_buyer and buyer_username and order_buyer.lower() == buyer_username.lower():
                        order_id = getattr(order, "id", "—")
                        desc = getattr(order, "description", "")
                        status_obj = getattr(order, "status", None)
                        status_name = getattr(status_obj, "name", str(status_obj)) if status_obj else "—"
                        return f"#{order_id} ({status_name}) — {desc[:120]}"
    except Exception:
        pass
    return None


def extract_author_name(msg: Any, author_id: Any) -> str:
    if isinstance(msg, dict):
        for k in ("sender_username", "author_username", "author", "sender", "username", "name"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return f"User_{author_id}"
    for attr in ("sender_username", "author_username", "author", "sender", "username"):
        val = getattr(msg, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if val is not None:
            uname = getattr(val, "username", None) or getattr(val, "name", None)
            if isinstance(uname, str) and uname.strip():
                return uname.strip()
    return f"User_{author_id}"


def notify_admin_sync(
    tg_app: Application,
    loop: asyncio.AbstractEventLoop,
    html_text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    alert_key: Optional[str] = None,
    force: bool = False,
) -> None:
    if tg_app is None or loop.is_closed() or not ADMIN_ID:
        return
    if alert_key and not force:
        now = time.time()
        with last_alert_lock:
            prev_ts = last_alert_meta.get(alert_key, 0.0)
            if (now - prev_ts) < ALERT_COOLDOWN_SECONDS:
                return
            last_alert_meta[alert_key] = now

    coro = tg_app.bot.send_message(
        chat_id=ADMIN_ID,
        text=html_text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)

        def _on_done(fut: asyncio.Future) -> None:
            try:
                fut.result()
            except Exception as exc:
                log.error("Не удалось отправить уведомление в Telegram: %s", sanitize_error_text(exc))

        future.add_done_callback(_on_done)
    except Exception as exc:
        log.error("Ошибка постановки задачи уведомления в Telegram: %s", type(exc).__name__)


def _mark_poll_healthy(username: Optional[str] = None) -> bool:
    now = time.time()
    with state.lock:
        had_errors = state.consecutive_poll_errors > 0 or state.diagnosis_code not in ("init", "ok")
        state.funpay_connected = True
        if username:
            state.funpay_username = username
        state.last_poll_at = now
        state.last_ok_poll_at = now
        state.consecutive_poll_errors = 0
        state.current_backoff = POLL_DELAY_SECONDS
        state.last_error = None
        state.diagnosis_code = "ok"
        state.diagnosis_detail = "Опрос событий работает штатно"
        if state.connection_status == "Ожидание запуска":
            state.connection_status = "Соединение активно"
    return had_errors


def _record_poll_failure(exc: Exception, tg_app: Application, loop: asyncio.AbstractEventLoop) -> float:
    code, title, hint = classify_funpay_issue(exc)
    with state.lock:
        state.consecutive_poll_errors += 1
        err_count = state.consecutive_poll_errors
        delay = compute_backoff_seconds(err_count)
        state.current_backoff = delay
        state.last_error = title
        state.diagnosis_code = code
        state.diagnosis_detail = title
        if code == "auth":
            state.funpay_connected = False
            state.connection_status = "Требуется проверка сессии / FUNPAY_GOLDEN_KEY"
        else:
            state.connection_status = f"Нестабильный опрос ({code}), повтор через {delay:.0f}с"

    log.error(
        "Контроль опроса FunPay [%s] (ошибок подряд: %s, пауза %.1fс): %s",
        code,
        err_count,
        delay,
        title,
    )

    if err_count in (1, 3, 10) or (err_count % 25 == 0):
        notify_admin_sync(
            tg_app,
            loop,
            f"⚠️ <b>Проблема получения сообщений на FunPay</b>\n"
            f"🔎 <b>Диагностика:</b> <code>{escape_html(code)}</code> — {escape_html(title)}\n"
            f"📉 <b>Ошибок подряд:</b> {err_count} | <b>Пауза:</b> {delay:.0f} сек.\n"
            f"💡 {escape_html(hint)}",
            alert_key=f"poll_err_{code}_{1 if err_count < 3 else (3 if err_count < 10 else 10)}",
        )
    return delay


def runner_watchdog_loop(tg_app: Application, loop: asyncio.AbstractEventLoop) -> None:
    while True:
        time.sleep(15.0)
        now = time.time()

        with state.lock:
            last_ref = max(state.last_poll_at or 0.0, state.last_ok_poll_at or 0.0)
            last_check = state.last_conn_check_at or 0.0
            has_key = bool(FUNPAY_GOLDEN_KEY)

        if not has_key:
            continue

        if (now - last_check) >= CONNECTION_CHECK_INTERVAL:
            with funpay_lock:
                acc_ref = funpay_account
            ok, msg = probe_funpay_connection(acc_ref)
            with state.lock:
                state.last_conn_check_at = now
                state.connection_status = msg if ok else f"Проблема проверки связи: {msg}"

        if last_ref > 0 and (now - last_ref) > RUNNER_HANG_TIMEOUT_SECONDS:
            idle_sec = int(now - last_ref)
            log.warning(
                "Обнаружено зависание опроса FunPay (%s сек без активности, лимит %.0f сек) — перезапуск runner.",
                idle_sec,
                RUNNER_HANG_TIMEOUT_SECONDS,
            )
            stop_active_runner_safely()
            with state.lock:
                state.hang_restarts += 1
                state.last_poll_at = now
                state.diagnosis_code = "hang_restart"
                state.diagnosis_detail = f"Зависание опроса ({idle_sec}с) — выполнен перезапуск"
                state.connection_status = "Перезапуск зависшего опроса событий"

            notify_admin_sync(
                tg_app,
                loop,
                f"🔄 <b>Защита от зависания сработала</b>\n"
                f"Опрос событий FunPay не обновлялся <b>{idle_sec} сек.</b> — выполнен перезапуск цикла опроса.",
                alert_key="runner_hang",
            )


def _extract_msg_from_payload(payload: Any) -> Optional[Any]:
    if payload is None:
        return None
    if isinstance(payload, (list, tuple)):
        for item in reversed(payload):
            found = _extract_msg_from_payload(item)
            if found is not None:
                return found
        return None
    if isinstance(payload, dict):
        for key in ("message", "chat_message", "new_message", "event_message", "last_message", "data"):
            if key in payload and payload[key] is not None:
                found = _extract_msg_from_payload(payload[key])
                if found is not None:
                    return found
        if any(k in payload for k in ("text", "chat_id", "last_message_text", "sender_id", "author_id")):
            return payload
        return None
    for attr in ("message", "chat_message", "new_message", "event_message", "last_message", "data"):
        obj = getattr(payload, attr, None)
        if obj is not None and any(
            hasattr(obj, a)
            for a in ("text", "chat_id", "author_id", "sender_id", "last_message_text", "sender_username")
        ):
            return obj
    if any(hasattr(payload, a) for a in ("text", "chat_id", "last_message_text", "sender_id", "author_id")):
        return payload
    return None


def _normalize_message_fields(bot_obj: Any, msg: Any) -> Optional[Tuple[Any, Any, Any, Any, str, str]]:
    if msg is None:
        return None

    my_id = getattr(bot_obj, "id", None)

    if isinstance(msg, dict):
        author_id = (
            msg.get("author_id")
            or msg.get("sender_id")
            or msg.get("last_message_author_id")
            or msg.get("user_id")
        )
        chat_id = msg.get("chat_id") or (msg.get("chat", {}).get("id") if isinstance(msg.get("chat"), dict) else None) or msg.get("id")
        msg_id = msg.get("id") or msg.get("message_id") or msg.get("last_message_id")
        raw_text = msg.get("text") if msg.get("text") is not None else (
            msg.get("last_message_text") if msg.get("last_message_text") is not None else msg.get("message")
        )
        text = str(raw_text) if raw_text is not None else "[вложение / нетекстовое сообщение]"
        buyer = extract_author_name(msg, author_id)
        return (author_id, my_id, chat_id, msg_id, text, str(buyer))

    author_id = (
        getattr(msg, "author_id", None)
        or getattr(msg, "sender_id", None)
        or getattr(msg, "last_message_author_id", None)
        or getattr(msg, "user_id", None)
    )
    chat_id = (
        getattr(msg, "chat_id", None)
        or getattr(getattr(msg, "chat", None), "id", None)
    )
    if chat_id is None and (hasattr(msg, "text") or hasattr(msg, "sender_id")):
        chat_id = getattr(msg, "id", None)

    msg_id = (
        getattr(msg, "message_id", None)
        or getattr(msg, "last_message_id", None)
        or (getattr(msg, "id", None) if (hasattr(msg, "text") or hasattr(msg, "last_message_text")) else None)
    )
    raw_text = (
        getattr(msg, "text", None)
        if getattr(msg, "text", None) is not None
        else (
            getattr(msg, "last_message_text", None)
            if getattr(msg, "last_message_text", None) is not None
            else getattr(msg, "message", None)
        )
    )
    text = str(raw_text) if raw_text is not None else "[вложение / нетекстовое сообщение]"

    buyer_username = extract_author_name(msg, author_id)
    if buyer_username.startswith("User_") and getattr(msg, "name", None):
        buyer_username = str(getattr(msg, "name")).strip()

    return author_id, my_id, chat_id, msg_id, text, buyer_username


def _is_own_message(author_id: Any, my_id: Any, buyer_username: str, bot_username: str, clean_incoming: str, last_bot_rep: str) -> bool:
    if my_id is not None and author_id is not None and str(author_id) == str(my_id):
        return True
    if bot_username and buyer_username and buyer_username.strip().lower() == bot_username:
        return True
    if clean_incoming and last_bot_rep and clean_incoming.strip() == last_bot_rep.strip():
        return True
    return False


def _process_incoming_funpay_message(
    bot_obj: Any,
    msg: Any,
    tg_app: Application,
    tg_loop: asyncio.AbstractEventLoop,
) -> None:
    _mark_poll_healthy(getattr(bot_obj, "username", None))
    parsed = _normalize_message_fields(bot_obj, msg)
    if parsed is None:
        return

    author_id, my_id, chat_id, msg_id, text, buyer_username = parsed

    if chat_id is None:
        log.debug("Пропуск элемента без chat_id: msg_id=%s, author=%s", msg_id, buyer_username)
        return

    clean_incoming = str(text or "").strip()
    bot_username = str(getattr(bot_obj, "username", "") or "").strip().lower()
    prev_chat = state.get_chat(int(chat_id))
    last_bot_rep = (prev_chat.last_reply_text or "").strip() if prev_chat else ""

    if _is_own_message(author_id, my_id, str(buyer_username), bot_username, clean_incoming, last_bot_rep):
        log.debug("Пропуск собственного/эхо-сообщения в чате %s (author_id=%s, my_id=%s)", chat_id, author_id, my_id)
        return

    dedup_key = f"{chat_id}:{msg_id}" if msg_id is not None else f"{chat_id}:{buyer_username}:{clean_incoming}"
    if not state.mark_seen_if_new(dedup_key):
        return

    log.info(
        "Входящее сообщение FunPay: chat_id=%s, msg_id=%s, author=%s, text=%r",
        chat_id,
        msg_id,
        buyer_username,
        clean_incoming[:160],
    )

    order_info = extract_order_info(bot_obj, str(buyer_username))
    state.record_chat(int(chat_id), str(buyer_username), clean_incoming, order_info)

    chat_is_disabled = state.is_chat_disabled(int(chat_id))
    order_line = f"\n📦 <b>Заказ:</b> {escape_html(order_info)}" if order_info else ""
    chat_mode_line = "\n🔕 <b>Автоответ в этом чате:</b> ВЫКЛЮЧЕН" if chat_is_disabled else ""
    toggle_label = "🔔 Включить автоответ в чате" if chat_is_disabled else "🔕 Выключить автоответ в чате"
    notif_html = (
        f"💬 <b>Новое сообщение на FunPay</b>\n"
        f"👤 <b>Покупатель:</b> {escape_html(str(buyer_username))} (чат <code>{chat_id}</code>)"
        f"{order_line}{chat_mode_line}\n"
        f"📝 <b>Текст:</b>\n{escape_html(clean_incoming)}"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↩️ Ответить", callback_data=f"quick_reply:{chat_id}")],
            [InlineKeyboardButton(toggle_label, callback_data=f"toggle_chat:{chat_id}")],
        ]
    )
    notify_admin_sync(tg_app, tg_loop, notif_html, reply_markup=kb)

    with state.lock:
        auto_active = state.auto_reply_enabled

    if not auto_active:
        return

    if chat_is_disabled:
        notify_admin_sync(
            tg_app,
            tg_loop,
            f"🔕 <b>Автоответчик не вмешался</b> в чат <code>{chat_id}</code> "
            f"(<b>{escape_html(str(buyer_username))}</b>) — автоответ выключен для этого чата.",
            alert_key=f"chat_disabled_{chat_id}",
        )
        return

    silence_left = state.get_manual_silence_remaining(int(chat_id))
    if silence_left > 0:
        mins_left = round(silence_left / 60.0, 1)
        notify_admin_sync(
            tg_app,
            tg_loop,
            f"⏸ <b>Автоответчик не вмешался</b> в чат <code>{chat_id}</code> "
            f"(<b>{escape_html(str(buyer_username))}</b>) — пауза после вашего ответа "
            f"(осталось ~<b>{mins_left} мин.</b>).",
            alert_key=f"manual_silence_{chat_id}",
        )
        return

    composed_reply, matched_rules, is_uncertain = state.evaluate_message(int(chat_id), clean_incoming)
    if matched_rules and composed_reply:
        rule_tag = ",".join(r.id for r in matched_rules)
        reply_bucket = int(time.time() // 240)
        auto_key = f"auto:{chat_id}:{reply_bucket}:{hash(composed_reply)}:{rule_tag}"
        if state.mark_auto_replied_if_new(auto_key):
            ok, err = send_funpay_message(int(chat_id), composed_reply)
            if ok:
                ack_ts = time.time() if any(r.id in ACK_ONLY_RULE_IDS for r in matched_rules) else None
                state.update_chat_auto_meta(
                    int(chat_id),
                    rule_id=matched_rules[0].id,
                    reply_text=composed_reply,
                    ack_ts=ack_ts,
                )
                titles = ", ".join(r.title for r in matched_rules)
                notify_admin_sync(
                    tg_app,
                    tg_loop,
                    f"✅ <b>Отвечено автоответом</b> покупателю <b>{escape_html(str(buyer_username))}</b> "
                    f"(чат <code>{chat_id}</code>)\n"
                    f"📌 <b>Правило:</b> {escape_html(titles)}\n"
                    f"💬 <b>Текст:</b>\n<blockquote>{escape_html(composed_reply)}</blockquote>",
                )
            else:
                notify_admin_sync(
                    tg_app,
                    tg_loop,
                    f"⚠️ <b>Ошибка автоответа</b> в чате <code>{chat_id}</code> "
                    f"(<b>{escape_html(str(buyer_username))}</b>):\n"
                    f"<code>{escape_html(err)}</code>",
                )
    elif is_uncertain:
        allowed, _ = state.claim_fallback_slot(int(chat_id))
        if allowed:
            ok, err = send_funpay_message(int(chat_id), DEFAULT_FALLBACK_REPLY)
            if ok:
                state.update_chat_auto_meta(
                    int(chat_id),
                    rule_id="fallback",
                    reply_text=DEFAULT_FALLBACK_REPLY,
                )
                notify_admin_sync(
                    tg_app,
                    tg_loop,
                    f"✅ <b>Отвечено автоответом (резерв)</b> покупателю <b>{escape_html(str(buyer_username))}</b> "
                    f"(чат <code>{chat_id}</code>, интервал: {FALLBACK_COOLDOWN_SECONDS} сек).\n"
                    f"💬 <b>Текст:</b>\n<blockquote>{escape_html(DEFAULT_FALLBACK_REPLY)}</blockquote>",
                )
            else:
                with state.lock:
                    state.fallback_last_sent.pop(int(chat_id), None)
                notify_admin_sync(
                    tg_app,
                    tg_loop,
                    f"⚠️ <b>Ошибка резервного автоответа</b> в чате <code>{chat_id}</code> "
                    f"(<b>{escape_html(str(buyer_username))}</b>):\n"
                    f"<code>{escape_html(err)}</code>",
                )


def _try_register_handler(target: Any, handler: Any) -> Optional[str]:
    if target is None:
        return None

    observer_names = (
        "on_new_message",
        "new_message",
        "message",
        "on_message",
        "on_chat_message",
        "chat_message",
        "on_event",
        "event",
        "on_update",
        "update",
    )
    for attr in observer_names:
        observer = getattr(target, attr, None)
        if observer is None:
            continue

        if callable(observer):
            try:
                decorator_or_res = observer()
                if callable(decorator_or_res):
                    decorator_or_res(handler)
                    return f"{type(target).__name__}.{attr}()"
            except TypeError:
                pass
            except Exception as exc:
                log.debug("Ошибка вызова %s.%s(): %s", type(target).__name__, attr, exc)

            try:
                observer(handler)
                return f"{type(target).__name__}.{attr}(handler)"
            except TypeError:
                pass
            except Exception as exc:
                log.debug("Ошибка вызова %s.%s(handler): %s", type(target).__name__, attr, exc)

        for reg_name in ("register", "add_handler", "on", "connect", "subscribe"):
            reg_fn = getattr(observer, reg_name, None)
            if callable(reg_fn):
                try:
                    reg_fn(handler)
                    return f"{type(target).__name__}.{attr}.{reg_name}(handler)"
                except TypeError:
                    pass
                except Exception as exc:
                    log.debug(
                        "Ошибка вызова %s.%s.%s: %s",
                        type(target).__name__,
                        attr,
                        reg_name,
                        exc,
                    )

    for dict_attr in ("observers", "handlers", "events", "_observers", "_handlers"):
        mapping = getattr(target, dict_attr, None)
        if isinstance(mapping, dict):
            for key in (
                "new_message",
                "message",
                "chat_message",
                "event",
                "update",
                "on_new_message",
                "on_message",
            ):
                obs = mapping.get(key)
                if obs is None:
                    continue
                if callable(obs):
                    try:
                        obs(handler)
                        return f"{type(target).__name__}.{dict_attr}[{key!r}]()"
                    except Exception:
                        pass
                for reg_name in ("register", "add_handler", "append"):
                    reg_fn = getattr(obs, reg_name, None)
                    if callable(reg_fn):
                        try:
                            reg_fn(handler)
                            return f"{type(target).__name__}.{dict_attr}[{key!r}].{reg_name}"
                        except Exception:
                            pass

    return None


def _collect_search_targets(bot_obj: Any, dp: Optional[Any] = None, router: Optional[Any] = None) -> List[Any]:
    targets: List[Any] = []
    for candidate in (bot_obj, dp, router):
        if candidate is not None and candidate not in targets:
            targets.append(candidate)
    for owner in list(targets):
        for sub_attr in ("client", "api", "session", "account", "runner", "dp", "router", "bot"):
            sub_obj = getattr(owner, sub_attr, None)
            if sub_obj is not None and sub_obj not in targets:
                targets.append(sub_obj)
    return targets


async def _poll_chats_fallback_loop(
    bot_obj: Any,
    tg_app: Application,
    tg_loop: asyncio.AbstractEventLoop,
    dp: Optional[Any] = None,
    router: Optional[Any] = None,
) -> None:
    last_empty_log_at = 0.0
    last_summary_log_at = 0.0

    while True:
        _mark_poll_healthy(getattr(bot_obj, "username", None))
        chats_source: Any = None
        used_source_name: Optional[str] = None
        search_targets = _collect_search_targets(bot_obj, dp=dp, router=router)

        for owner in search_targets:
            for getter_name in (
                "get_chats",
                "get_chat_list",
                "fetch_chats",
                "load_chats",
                "get_dialogs",
                "chats",
            ):
                attr = getattr(owner, getter_name, None)
                if attr is None:
                    continue
                try:
                    if callable(attr):
                        res = attr()
                        if inspect.isawaitable(res):
                            res = await res
                        chats_source = res
                    else:
                        chats_source = attr
                    if chats_source is not None:
                        used_source_name = f"{type(owner).__name__}.{getter_name}"
                        break
                except TypeError:
                    continue
                except Exception as exc:
                    log.debug(
                        "Ошибка получения списка чатов через %s.%s: %s",
                        type(owner).__name__,
                        getter_name,
                        sanitize_error_text(exc),
                    )
            if chats_source is not None:
                break

        if isinstance(chats_source, dict):
            chat_items = list(chats_source.values())
        elif isinstance(chats_source, (list, tuple, set)):
            chat_items = list(chats_source)
        else:
            chat_items = []

        if not chat_items:
            now = time.time()
            if now - last_empty_log_at >= 120.0:
                last_empty_log_at = now
                log.info("Опрос активен (source=%s), новых чатов/событий пока нет.", used_source_name)
        else:
            now = time.time()
            if now - last_summary_log_at >= 120.0:
                last_summary_log_at = now
                log.info("Опрос активен: найдено чатов=%s (source=%s)", len(chat_items), used_source_name)

            for chat_item in chat_items:
                _mark_poll_healthy(getattr(bot_obj, "username", None))
                direct_msg = _extract_msg_from_payload(chat_item)
                if direct_msg is not None and (
                    getattr(direct_msg, "text", None) is not None
                    or getattr(direct_msg, "last_message_text", None) is not None
                    or (isinstance(direct_msg, dict) and ("text" in direct_msg or "last_message_text" in direct_msg))
                ):
                    _process_incoming_funpay_message(bot_obj, direct_msg, tg_app, tg_loop)

                chat_id = (
                    getattr(chat_item, "chat_id", None)
                    or getattr(chat_item, "id", None)
                    or (chat_item.get("chat_id") or chat_item.get("id") if isinstance(chat_item, dict) else None)
                )
                if chat_id is not None:
                    for owner in search_targets:
                        for msg_getter in (
                            "get_chat_messages",
                            "get_messages",
                            "fetch_messages",
                            "get_history",
                        ):
                            m_fn = getattr(owner, msg_getter, None)
                            if not callable(m_fn):
                                continue
                            try:
                                m_res = m_fn(chat_id)
                                if inspect.isawaitable(m_res):
                                    m_res = await m_res
                                if isinstance(m_res, (list, tuple)) and m_res:
                                    for m_item in m_res[-5:]:
                                        m_obj = _extract_msg_from_payload(m_item)
                                        if m_obj is not None:
                                            _process_incoming_funpay_message(bot_obj, m_obj, tg_app, tg_loop)
                                    break
                            except TypeError:
                                continue
                            except Exception:
                                break

        await asyncio.sleep(POLL_DELAY_SECONDS)


async def _run_bot_engine_cycle(
    golden_key: str,
    tg_app: Application,
    tg_loop: asyncio.AbstractEventLoop,
) -> None:
    global funpay_account, active_runner

    bot_obj = await _create_and_start_bot(golden_key)
    dp = Dispatcher()
    router = Router()

    async def _on_event(*args: Any, **kwargs: Any) -> None:
        _mark_poll_healthy(getattr(bot_obj, "username", None))
        candidates: List[Any] = list(kwargs.values()) + list(args)
        for cand in candidates:
            msg_obj = _extract_msg_from_payload(cand)
            if msg_obj is not None:
                _process_incoming_funpay_message(bot_obj, msg_obj, tg_app, tg_loop)

    registered_via: Optional[str] = None
    for target in (dp, router, bot_obj):
        registered_via = _try_register_handler(target, _on_event)
        if registered_via:
            break

    include_fn = getattr(dp, "include_router", None)
    if callable(include_fn):
        try:
            include_fn(router)
        except Exception as exc:
            log.debug("Не удалось подключить router к dp: %s", exc)

    log.info("Обработчик событий зарегистрирован через: %s", registered_via)

    with funpay_lock:
        funpay_account = bot_obj
        active_runner = dp

    recovered = _mark_poll_healthy(getattr(bot_obj, "username", None) or "Аккаунт FunPay")
    with state.lock:
        state.last_conn_check_at = time.time()
        state.connection_status = "Соединение активно (funpaybotengine)"

    log.info("Подключение к FunPay установлено (%s).", state.funpay_username)
    if recovered:
        notify_admin_sync(
            tg_app,
            tg_loop,
            "✅ <b>Связь и опрос событий FunPay восстановлены.</b> Бот снова получает сообщения.",
            alert_key="recovered",
            force=False,
        )

    poll_task = asyncio.create_task(_poll_chats_fallback_loop(bot_obj, tg_app, tg_loop, dp=dp, router=router))
    try:
        listen_events_fn = getattr(bot_obj, "listen_events", None)
        if callable(listen_events_fn):
            res = listen_events_fn(dp)
            if inspect.isawaitable(res):
                await res
            return

        for target in (dp, bot_obj):
            for method_name in ("start_polling", "run_polling", "poll", "listen", "start"):
                fn = getattr(target, method_name, None)
                if callable(fn):
                    try:
                        res = fn(dp if target is bot_obj else bot_obj)
                        if inspect.isawaitable(res):
                            await res
                        return
                    except TypeError:
                        res = fn()
                        if inspect.isawaitable(res):
                            await res
                        return

        await poll_task
    finally:
        if not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass


def funpay_worker(golden_key: str, tg_app: Application, loop: asyncio.AbstractEventLoop) -> None:
    global funpay_account, active_runner, funpay_loop
    backoff = POLL_DELAY_SECONDS

    watchdog = threading.Thread(
        target=runner_watchdog_loop,
        args=(tg_app, loop),
        name="FunPayWatchdogThread",
        daemon=True,
    )
    watchdog.start()

    raiser_thread = threading.Thread(
        target=auto_raise_loop,
        args=(tg_app, loop),
        name="FunPayAutoRaiseThread",
        daemon=True,
    )
    raiser_thread.start()

    worker_loop = asyncio.new_event_loop()
    with funpay_lock:
        funpay_loop = worker_loop
    asyncio.set_event_loop(worker_loop)

    while True:
        try:
            log.info("Подключение к FunPay через funpaybotengine (Bot/Dispatcher)...")
            worker_loop.run_until_complete(_run_bot_engine_cycle(golden_key, tg_app, loop))
            backoff = POLL_DELAY_SECONDS
            time.sleep(POLL_DELAY_SECONDS)
        except Exception as exc:
            log.error(
                "Исключение в основном цикле funpay_worker: %s",
                sanitize_error_text(exc),
                exc_info=exc,
            )
            with funpay_lock:
                active_runner = None
            backoff = _record_poll_failure(exc, tg_app, loop)
            with state.lock:
                was_connected = state.funpay_connected
                code, _, _ = classify_funpay_issue(exc)
                if code == "auth":
                    state.funpay_connected = False
                    with funpay_lock:
                        funpay_account = None

            if was_connected and code == "auth":
                notify_admin_sync(
                    tg_app,
                    loop,
                    f"🔌 <b>Сессия FunPay требует внимания!</b>\n"
                    f"Причина: <code>{escape_html(sanitize_error_text(exc))}</code>\n"
                    f"Повторная попытка через {backoff:.0f} сек.",
                    alert_key="session_lost",
                )

            time.sleep(backoff)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    fp_line = (
        "🟢 Подключён"
        if state.funpay_connected
        else ("⚠️ Не задан FUNPAY_GOLDEN_KEY" if not FUNPAY_GOLDEN_KEY else "🟡 Ожидание подключения")
    )
    auto_line = "🟢 ВКЛЮЧЁН" if state.auto_reply_enabled else "🔴 ВЫКЛЮЧЕН"
    raise_line = "🟢 ВКЛЮЧЕНО" if state.auto_raise_enabled else "🔴 ВЫКЛЮЧЕНО"
    disabled_cnt = len(state.disabled_chats)
    await update.message.reply_text(
        "🤖 <b>FunPay Manager</b> запущен.\n\n"
        f"🛒 FunPay: <b>{fp_line}</b>\n"
        f"🛡️ Автоответчик: <b>{auto_line}</b>\n"
        f"🚀 Автоподнятие лотов: <b>{raise_line}</b>\n"
        f"🔕 Выключено чатов: <b>{disabled_cnt}</b>\n\n"
        "Используйте кнопки ниже или команды:\n"
        "/status — состояние подключений\n"
        "/test — проверка работоспособности\n"
        "/reply — ответить покупателю\n"
        "/raise_now — поднять лоты прямо сейчас\n"
        "/raise_on / /raise_off — автоподнятие лотов\n"
        "/chat_off — выключить автоответ в конкретном чате\n"
        "/chat_on — включить автоответ в конкретном чате\n"
        "/auto_on — включить автоответчик\n"
        "/auto_off — выключить автоответчик\n"
        "/help — справка",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with state.lock:
        fp_ok = state.funpay_connected
        fp_user = state.funpay_username or "—"
        last_err = state.last_error
        last_poll = state.last_poll_at
        conn_status = state.connection_status
        diag_code = state.diagnosis_code
        diag_detail = state.diagnosis_detail
        err_streak = state.consecutive_poll_errors
        cur_backoff = state.current_backoff
        hang_count = state.hang_restarts
        auto_on = state.auto_reply_enabled
        raise_on = state.auto_raise_enabled
        last_raise = state.last_raise_at
        raise_summary = state.last_raise_summary
        raised_total = state.total_raised_count
        active_rules = sum(1 for r in state.rules if r.enabled)
        total_rules = len(state.rules)
        disabled_list = sorted(state.disabled_chats)

    if not FUNPAY_GOLDEN_KEY:
        fp_status = "⚠️ Не задана переменная FUNPAY_GOLDEN_KEY"
    elif fp_ok:
        fp_status = f"🟢 Подключён ({escape_html(fp_user)})"
    else:
        fp_status = "🔴 Нет соединения"

    poll_info = "ещё не выполнялся" if not last_poll else f"{int(time.time() - last_poll)} сек. назад"
    err_block = f"\n⚠️ <b>Последняя ошибка:</b> <code>{escape_html(last_err)}</code>" if last_err else ""
    disabled_line = (
        "\n🔕 <b>Выключенные чаты:</b> " + ", ".join(f"<code>{cid}</code>" for cid in disabled_list)
        if disabled_list
        else "\n🔕 <b>Выключенные чаты:</b> нет"
    )
    mask_line = "🎭 <b>Маскировка:</b> заголовки браузера (Chrome 131), движок funpaybotengine"

    await update.message.reply_text(
        "📊 <b>Состояние системы:</b>\n\n"
        "🟢 <b>Telegram-бот:</b> работает\n"
        f"🛒 <b>FunPay:</b> {fp_status}\n"
        f"🌐 <b>Соединение:</b> {escape_html(conn_status)}\n"
        f"{mask_line}\n"
        f"⏱ <b>Последний опрос:</b> {poll_info} (интервал: {POLL_DELAY_SECONDS:.0f}с, рестартов: {hang_count})\n"
        f"🩺 <b>Диагностика:</b> <code>{escape_html(diag_code)}</code> — {escape_html(diag_detail)} "
        f"(ошибок подряд: {err_streak}, пауза: {cur_backoff:.0f}с)\n"
        f"⏳ <b>Пауза после вашего ответа:</b> {MANUAL_SILENCE_SECONDS} сек. ({MANUAL_SILENCE_SECONDS / 60:.0f} мин.)\n"
        f"🛡️ <b>Автоответчик:</b> {'🟢 включён' if auto_on else '🔴 выключен'} "
        f"(активно правил: {active_rules}/{total_rules}, резерв: раз в {FALLBACK_COOLDOWN_SECONDS} сек)\n"
        f"🚀 <b>Автоподнятие лотов:</b> {'🟢 включено' if raise_on else '🔴 выключено'} "
        f"(интервал: {AUTO_RAISE_INTERVAL_SECONDS // 60} мин, поднято всего: {raised_total})\n"
        f"📌 <b>Последнее поднятие:</b> {'не выполнялось' if not last_raise else f'{int(time.time() - last_raise)} сек. назад'} — {escape_html(raise_summary)}"
        f"{disabled_line}"
        f"{err_block}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with funpay_lock:
        connected = funpay_account is not None and state.funpay_connected
        acc_id = getattr(funpay_account, "id", None) if funpay_account else None
        acc_ref = funpay_account

    ok_probe, probe_msg = probe_funpay_connection(acc_ref) if connected else (False, state.connection_status)
    with state.lock:
        state.last_conn_check_at = time.time()
        state.connection_status = probe_msg
        diag_code = state.diagnosis_code
        diag_detail = state.diagnosis_detail

    if connected and ok_probe:
        fp_msg = f"🟢 Сессия FunPay активна (ID аккаунта: <code>{acc_id}</code>). {escape_html(probe_msg)}."
    elif not FUNPAY_GOLDEN_KEY:
        fp_msg = "⚠️ Переменная <code>FUNPAY_GOLDEN_KEY</code> не задана."
    else:
        err = state.last_error or probe_msg
        fp_msg = (
            f"🔴 FunPay пока не подключён (<code>{escape_html(err)}</code>).\n"
            f"🩺 Диагностика: <code>{escape_html(diag_code)}</code> — {escape_html(diag_detail)}"
        )

    await update.message.reply_text(
        "✅ Тестовое уведомление получено.\n"
        "Связь Telegram ↔ облачный бот работает.\n"
        f"{fp_msg}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "📖 <b>Команды FunPay Manager:</b>\n\n"
        "/start — открыть главное меню\n"
        "/status — проверить состояние Telegram, FunPay и диагностику\n"
        "/test — проверить уведомления, сессию и доступность FunPay\n"
        "/reply — выбрать диалог или ответить на последнее сообщение\n"
        "/raise_now — поднять все доступные лоты сейчас\n"
        "/raise_on — включить автоподнятие лотов\n"
        "/raise_off — выключить автоподнятие лотов\n"
        "/chat_off &lt;chat_id&gt; — выключить автоответ в конкретном чате\n"
        "/chat_on &lt;chat_id&gt; — включить автоответ в конкретном чате\n"
        "/auto_on — включить автоответчик\n"
        "/auto_off — выключить автоответчик\n"
        "/help — эта справка\n\n"
        "Распознаёт сленг, опечатки, повторы букв, латиницу и сообщения без пунктуации.\n"
        f"Если смысл не распознан — отправляется резервный ответ (раз в {FALLBACK_COOLDOWN_SECONDS} сек на чат), "
        "а уведомление приходит вам.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


def _resolve_target_chat_id(args: List[str]) -> Optional[int]:
    for arg in args:
        cleaned = arg.strip()
        if cleaned.lstrip("-").isdigit():
            return int(cleaned)
    with state.lock:
        return state.last_chat_id


async def chat_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    target_id = _resolve_target_chat_id(context.args or [])
    if target_id is None:
        await update.message.reply_text(
            "⚠️ Укажите ID чата: <code>/chat_off 123456</code> (или дождитесь входящего сообщения).",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        return
    state.set_chat_disabled(target_id, True)
    chat_info = state.get_chat(target_id)
    buyer = chat_info.buyer_username if chat_info else f"Чат {target_id}"
    await update.message.reply_text(
        f"🔕 Автоответ в чате <b>{escape_html(buyer)}</b> (<code>{target_id}</code>) <b>ВЫКЛЮЧЕН</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def chat_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    target_id = _resolve_target_chat_id(context.args or [])
    if target_id is None:
        await update.message.reply_text(
            "⚠️ Укажите ID чата: <code>/chat_on 123456</code> (или дождитесь входящего сообщения).",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        return
    state.set_chat_disabled(target_id, False)
    chat_info = state.get_chat(target_id)
    buyer = chat_info.buyer_username if chat_info else f"Чат {target_id}"
    await update.message.reply_text(
        f"🔔 Автоответ в чате <b>{escape_html(buyer)}</b> (<code>{target_id}</code>) <b>ВКЛЮЧЁН</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def send_chat_silence_picker(target_msg, edit: bool = False) -> None:
    with state.lock:
        chats = list(state.recent_chats)
        disabled_ids = sorted(state.disabled_chats)

    buttons: List[List[InlineKeyboardButton]] = []
    seen_ids: Set[int] = set()

    for c in chats[:8]:
        seen_ids.add(c.chat_id)
        is_off = c.chat_id in state.disabled_chats
        mark = "🔕 ВЫКЛ" if is_off else "🔔 ВКЛ"
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{mark} · {c.buyer_username} ({c.chat_id})",
                    callback_data=f"toggle_chat:{c.chat_id}",
                )
            ]
        )

    for cid in disabled_ids:
        if cid not in seen_ids:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🔕 ВЫКЛ · Чат {cid}",
                        callback_data=f"toggle_chat:{cid}",
                    )
                ]
            )

    if not buttons:
        text = (
            "🔕 <b>Управление автоответом по чатам</b>\n\n"
            "Пока нет недавних чатов и выключенных диалогов.\n"
            "Вы также можете использовать команду <code>/chat_off &lt;chat_id&gt;</code>."
        )
        markup = main_menu_keyboard()
        if edit:
            await safe_edit_text(target_msg, text, reply_markup=markup)
        else:
            await target_msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu:home")])
    text = (
        "🔕 <b>Выключение / включение автоответа для конкретного чата</b>\n"
        "Нажмите на чат, чтобы переключить режим:"
    )
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await safe_edit_text(target_msg, text, reply_markup=markup)
    else:
        await target_msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def raise_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    wait_msg = await update.message.reply_text("⏳ Запускаю поднятие лотов на FunPay...")
    loop = asyncio.get_running_loop()
    ok, count, detail = await loop.run_in_executor(None, trigger_raise_lots_sync)
    icon = "✅" if ok else "⚠️"
    await safe_edit_text(
        wait_msg,
        f"{icon} <b>Результат поднятия лотов:</b>\n{escape_html(detail)}",
        reply_markup=main_menu_keyboard(),
    )


async def raise_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with state.lock:
        state.auto_raise_enabled = True
    state.save_rules()
    await update.message.reply_text(
        f"🟢 <b>Автоподнятие лотов включено.</b>\nИнтервал проверки: каждые {AUTO_RAISE_INTERVAL_SECONDS // 60} мин.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def raise_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with state.lock:
        state.auto_raise_enabled = False
    state.save_rules()
    await update.message.reply_text(
        "🔴 <b>Автоподнятие лотов выключено.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with state.lock:
        state.auto_reply_enabled = True
    state.save_rules()
    await update.message.reply_text(
        "🟢 <b>Автоответчик включён.</b>\n"
        f"Сначала проверяются правила (с приоритетом жалоб/оплаты); при сомнении отправляется резервный ответ "
        f"(раз в {FALLBACK_COOLDOWN_SECONDS} сек на чат).",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with state.lock:
        state.auto_reply_enabled = False
    state.save_rules()
    await update.message.reply_text(
        "🔴 <b>Автоответчик выключен.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def send_reply_picker(target_msg) -> None:
    with state.lock:
        chats = list(state.recent_chats)
        last_id = state.last_chat_id

    if not chats:
        await target_msg.reply_text(
            "📭 Пока нет входящих сообщений от покупателей за текущую сессию.",
            reply_markup=main_menu_keyboard(),
        )
        return

    buttons: List[List[InlineKeyboardButton]] = []
    if last_id is not None:
        last_chat = state.get_chat(last_id)
        label = last_chat.buyer_username if last_chat else f"Чат {last_id}"
        buttons.append(
            [InlineKeyboardButton(f"⚡ Последний диалог: {label}", callback_data=f"select_chat:{last_id}")]
        )

    for c in chats[:8]:
        buttons.append(
            [InlineKeyboardButton(f"💬 {c.buyer_username} (чат {c.chat_id})", callback_data=f"select_chat:{c.chat_id}")]
        )

    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu:home")])
    await target_msg.reply_text(
        "Выберите покупателя, которому хотите отправить ответ:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await send_reply_picker(update.message)


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Доступ закрыт.")


async def render_rules_menu(target_message, page: int = 0, edit: bool = False) -> None:
    page_size = 8
    with state.lock:
        rules = list(state.rules)
        enabled = state.auto_reply_enabled

    total_pages = max(1, (len(rules) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * page_size
    chunk = rules[start_idx : start_idx + page_size]

    lines = [
        f"📋 <b>Правила автоответчика</b> ({'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}) · стр. {page + 1}/{total_pages}:",
        f"При сомнении — резервный ответ (раз в {FALLBACK_COOLDOWN_SECONDS} сек на чат).\n",
    ]
    buttons: List[List[InlineKeyboardButton]] = []

    for offset, r in enumerate(chunk):
        idx = start_idx + offset + 1
        mark = "🟢" if r.enabled else "⚪"
        kw_str = ", ".join(r.keywords[:4]) + ("…" if len(r.keywords) > 4 else "")
        preview = r.reply[:50] + ("…" if len(r.reply) > 50 else "")
        lines.append(f"{idx}. {mark} <b>{escape_html(r.title)}</b> [{escape_html(kw_str)}] → {escape_html(preview)}")
        buttons.append(
            [
                InlineKeyboardButton(f"{mark} #{idx}", callback_data=f"rule_toggle:{r.id}:{page}"),
                InlineKeyboardButton(f"✏️ #{idx}", callback_data=f"rule_edit:{r.id}"),
                InlineKeyboardButton(f"🗑 #{idx}", callback_data=f"rule_del:{r.id}:{page}"),
            ]
        )

    nav_row: List[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"rules_page:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"rules_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("➕ Добавить правило", callback_data="rule_add")])
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu:home")])

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await safe_edit_text(target_message, text, reply_markup=markup)
    else:
        await target_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""

    if data == "menu:home":
        with state.lock:
            state.pending_input.pop(ADMIN_ID, None)
        await safe_edit_text(
            query.message,
            "🏠 <b>Главное меню FunPay Manager</b>",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:status":
        with state.lock:
            fp_ok = state.funpay_connected
            fp_user = state.funpay_username or "—"
            auto_on = state.auto_reply_enabled
            raise_on = state.auto_raise_enabled
            raise_summary = state.last_raise_summary
            last_err = state.last_error
            diag_code = state.diagnosis_code
            err_streak = state.consecutive_poll_errors
            disabled_cnt = len(state.disabled_chats)
        if not FUNPAY_GOLDEN_KEY:
            fp_part = "⚠️ не задан FUNPAY_GOLDEN_KEY"
        elif fp_ok:
            fp_part = f"🟢 ({escape_html(fp_user)})"
        else:
            fp_part = "🔴 нет связи"
        err_part = f"\n⚠️ <code>{escape_html(last_err)}</code>" if last_err else ""
        await safe_edit_text(
            query.message,
            f"📊 <b>Статус:</b>\n"
            f"🟢 Telegram: работает\n"
            f"🛒 FunPay: {fp_part}\n"
            f"🩺 Диагностика: <code>{escape_html(diag_code)}</code> (ошибок подряд: {err_streak})\n"
            f"🛡️ Автоответчик: {'🟢 ВКЛ' if auto_on else '🔴 ВЫКЛ'}\n"
            f"🚀 Автоподнятие: {'🟢 ВКЛ' if raise_on else '🔴 ВЫКЛ'} ({escape_html(raise_summary)})\n"
            f"🔕 Выключено чатов: {disabled_cnt}{err_part}",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:test":
        with funpay_lock:
            ok = funpay_account is not None and state.funpay_connected
        await safe_edit_text(
            query.message,
            "✅ Telegram-бот работает. Сессия FunPay активна."
            if ok
            else "⚠️ Telegram-бот работает, но сессия FunPay не активна.",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:help":
        await safe_edit_text(
            query.message,
            "📖 Команды: /start, /status, /test, /reply, /raise_now, /raise_on, /raise_off, /chat_off, /chat_on, /auto_on, /auto_off, /help.\n"
            f"При сомнении отправляется резервный ответ (раз в {FALLBACK_COOLDOWN_SECONDS} сек на чат).",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:chat_silence":
        await send_chat_silence_picker(query.message, edit=True)
    elif data.startswith("toggle_chat:"):
        chat_id = int(data.split(":", 1)[1])
        now_disabled = state.toggle_chat_disabled(chat_id)
        chat_info = state.get_chat(chat_id)
        buyer = chat_info.buyer_username if chat_info else f"Чат {chat_id}"
        status_label = "🔕 ВЫКЛЮЧЕН" if now_disabled else "🔔 ВКЛЮЧЁН"
        await query.message.reply_text(
            f"Автоответ для <b>{escape_html(buyer)}</b> (чат <code>{chat_id}</code>): <b>{status_label}</b>.",
            parse_mode=ParseMode.HTML,
        )
    elif data == "menu:toggle_auto":
        with state.lock:
            state.auto_reply_enabled = not state.auto_reply_enabled
            new_state = state.auto_reply_enabled
        state.save_rules()
        await safe_edit_text(
            query.message,
            f"🛡️ Автоответчик теперь {'🟢 ВКЛЮЧЁН' if new_state else '🔴 ВЫКЛЮЧЕН'}.",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:toggle_raise":
        with state.lock:
            state.auto_raise_enabled = not state.auto_raise_enabled
            new_raise = state.auto_raise_enabled
        state.save_rules()
        await safe_edit_text(
            query.message,
            f"🚀 Автоподнятие лотов теперь {'🟢 ВКЛЮЧЕНО' if new_raise else '🔴 ВЫКЛЮЧЕНО'} "
            f"(интервал: {AUTO_RAISE_INTERVAL_SECONDS // 60} мин).",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:raise_now":
        await safe_edit_text(query.message, "⏳ Запускаю поднятие лотов на FunPay...")
        loop = asyncio.get_running_loop()
        ok, count, detail = await loop.run_in_executor(None, trigger_raise_lots_sync)
        icon = "✅" if ok else "⚠️"
        await safe_edit_text(
            query.message,
            f"{icon} <b>Результат поднятия лотов:</b>\n{escape_html(detail)}",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:reply":
        await send_reply_picker(query.message)
    elif data.startswith("quick_reply:"):
        chat_id = int(data.split(":", 1)[1])
        chat_info = state.get_chat(chat_id)
        buyer = chat_info.buyer_username if chat_info else f"Чат {chat_id}"
        with state.lock:
            state.pending_input[ADMIN_ID] = {
                "mode": "awaiting_reply_text",
                "target_chat_id": chat_id,
                "buyer_username": buyer,
            }
        await query.message.reply_text(
            f"✍️ Введите текст ответа для покупателя <b>{escape_html(buyer)}</b> (чат <code>{chat_id}</code>).\n"
            "Перед отправкой я покажу получателя для подтверждения.",
            parse_mode=ParseMode.HTML,
        )
    elif data.startswith("select_chat:"):
        chat_id = int(data.split(":", 1)[1])
        chat_info = state.get_chat(chat_id)
        buyer = chat_info.buyer_username if chat_info else f"Чат {chat_id}"
        with state.lock:
            state.pending_input[ADMIN_ID] = {
                "mode": "awaiting_reply_text",
                "target_chat_id": chat_id,
                "buyer_username": buyer,
            }
        await safe_edit_text(
            query.message,
            f"✍️ Введите текст ответа для покупателя <b>{escape_html(buyer)}</b> (чат <code>{chat_id}</code>):",
        )
    elif data == "confirm_send:yes":
        with state.lock:
            pending = state.pending_input.pop(ADMIN_ID, None)
        if not pending or pending.get("mode") != "awaiting_confirm_send":
            await safe_edit_text(query.message, "⌛ Запрос на отправку устарел.", reply_markup=main_menu_keyboard())
            return
        target_chat_id = int(pending["target_chat_id"])
        buyer = str(pending["buyer_username"])
        reply_text = str(pending["reply_text"])
        ok, err = send_funpay_message(target_chat_id, reply_text)
        if ok:
            state.update_chat_auto_meta(target_chat_id, rule_id="manual_admin", reply_text=reply_text)
            state.mark_manual_reply(target_chat_id)
            with state.lock:
                state.fallback_last_sent[target_chat_id] = time.time()
            await safe_edit_text(
                query.message,
                f"✅ Сообщение отправлено покупателю <b>{escape_html(buyer)}</b> (чат <code>{target_chat_id}</code>):\n"
                f"<blockquote>{escape_html(reply_text)}</blockquote>",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await safe_edit_text(
                query.message,
                f"❌ Ошибка отправки: <code>{escape_html(err)}</code>",
                reply_markup=main_menu_keyboard(),
            )
    elif data == "confirm_send:no":
        with state.lock:
            state.pending_input.pop(ADMIN_ID, None)
        await safe_edit_text(query.message, "🚫 Отправка отменена.", reply_markup=main_menu_keyboard())
    elif data == "menu:rules":
        await render_rules_menu(query.message, page=0, edit=True)
    elif data.startswith("rules_page:"):
        page = int(data.split(":", 1)[1])
        await render_rules_menu(query.message, page=page, edit=True)
    elif data.startswith("rule_toggle:"):
        parts = data.split(":")
        rule_id = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        with state.lock:
            for r in state.rules:
                if r.id == rule_id:
                    r.enabled = not r.enabled
                    break
        state.save_rules()
        await render_rules_menu(query.message, page=page, edit=True)
    elif data == "rule_add":
        with state.lock:
            state.pending_input[ADMIN_ID] = {"mode": "awaiting_new_rule"}
        await safe_edit_text(
            query.message,
            "➕ <b>Новое правило автоответа</b>\n\n"
            "Отправьте сообщение в формате:\n"
            "<code>ключевое слово 1, ключевое слово 2 | Текст автоответа</code>",
        )
    elif data.startswith("rule_edit:"):
        rule_id = data.split(":", 1)[1]
        with state.lock:
            state.pending_input[ADMIN_ID] = {"mode": "awaiting_edit_rule", "rule_id": rule_id}
        await safe_edit_text(
            query.message,
            f"✏️ <b>Редактирование правила <code>{escape_html(rule_id)}</code></b>\n\n"
            "Отправьте новые ключевые слова и текст в формате:\n"
            "<code>ключ1, ключ2 | Новый текст автоответа</code>",
        )
    elif data.startswith("rule_del:"):
        parts = data.split(":")
        rule_id = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        with state.lock:
            state.rules = [r for r in state.rules if r.id != rule_id]
        state.save_rules()
        await render_rules_menu(query.message, page=page, edit=True)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    with state.lock:
        pending = state.pending_input.get(ADMIN_ID)

    if not pending:
        return

    mode = pending.get("mode")

    if mode == "awaiting_reply_text":
        target_chat_id = int(pending["target_chat_id"])
        buyer = str(pending["buyer_username"])
        with state.lock:
            state.pending_input[ADMIN_ID] = {
                "mode": "awaiting_confirm_send",
                "target_chat_id": target_chat_id,
                "buyer_username": buyer,
                "reply_text": user_text,
            }
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Отправить", callback_data="confirm_send:yes"),
                    InlineKeyboardButton("❌ Отмена", callback_data="confirm_send:no"),
                ]
            ]
        )
        await update.message.reply_text(
            f"📤 <b>Подтверждение отправки:</b>\n"
            f"👤 <b>Кому:</b> {escape_html(buyer)} (чат <code>{target_chat_id}</code>)\n"
            f"💬 <b>Сообщение:</b>\n{escape_html(user_text)}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    if mode in ("awaiting_new_rule", "awaiting_edit_rule"):
        if "|" not in user_text:
            await update.message.reply_text(
                "⚠️ Неверный формат. Используйте разделитель <code>|</code>:\n"
                "<code>ключ1, ключ2 | Текст автоответа</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        kw_part, reply_part = user_text.split("|", 1)
        keywords = [normalize_text(k) for k in kw_part.split(",") if normalize_text(k)]
        reply_text = reply_part.strip()
        if not keywords or not reply_text:
            await update.message.reply_text("⚠️ Укажите хотя бы одно ключевое слово и непустой текст ответа.")
            return

        with state.lock:
            state.pending_input.pop(ADMIN_ID, None)
            if mode == "awaiting_new_rule":
                new_id = f"r_{int(time.time() * 1000)}"
                state.rules.append(
                    AutoReplyRule(
                        id=new_id,
                        title=keywords[0],
                        keywords=keywords,
                        reply=reply_text,
                        enabled=True,
                    )
                )
            else:
                rule_id = pending.get("rule_id")
                for r in state.rules:
                    if r.id == rule_id:
                        r.keywords = keywords
                        r.reply = reply_text
                        break
        state.save_rules()
        await render_rules_menu(update.message, page=0, edit=False)


async def post_init(application: Application) -> None:
    global worker_started
    if not FUNPAY_GOLDEN_KEY:
        log.warning("FUNPAY_GOLDEN_KEY не задан — фоновый поток FunPay не запущен.")
        return

    with worker_start_lock:
        if worker_started:
            return
        worker_started = True

    loop = asyncio.get_running_loop()
    thread = threading.Thread(
        target=funpay_worker,
        args=(FUNPAY_GOLDEN_KEY, application, loop),
        name="FunPayListenerThread",
        daemon=True,
    )
    thread.start()
    log.info("Фоновый поток FunPay запущен.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Не задана переменная TELEGRAM_BOT_TOKEN")
    if not ADMIN_ID:
        raise RuntimeError("Не задана переменная ADMIN_ID")

    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(CommandHandler("raise_now", raise_now_command))
    app.add_handler(CommandHandler("raise_on", raise_on_command))
    app.add_handler(CommandHandler("raise_off", raise_off_command))
    app.add_handler(CommandHandler("chat_off", chat_off))
    app.add_handler(CommandHandler("chat_on", chat_on))
    app.add_handler(CommandHandler("auto_on", auto_on))
    app.add_handler(CommandHandler("auto_off", auto_off))
    app.add_handler(CommandHandler("private", deny))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    log.info("FunPay Manager Telegram interface starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
