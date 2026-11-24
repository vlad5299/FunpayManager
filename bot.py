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

from FunPayAPI import Account, Runner, enums
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
RUNNER_HANG_TIMEOUT_SECONDS = max(25.0, float(os.getenv("RUNNER_HANG_TIMEOUT", "45.0")))
CONNECTION_CHECK_INTERVAL = max(30.0, float(os.getenv("CONNECTION_CHECK_INTERVAL", "90.0")))
ALERT_COOLDOWN_SECONDS = max(60.0, float(os.getenv("ALERT_COOLDOWN_SECONDS", "300.0")))
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
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://funpay.com/",
    "Origin": "https://funpay.com",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

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
    "rule_25",  # Возврат средств
    "rule_26",  # Жалоба / арбитраж
    "rule_27",  # Обвинение в обмане / скам
    "rule_17",  # Деньги списались, заказа нет
    "rule_16",  # Оплата не проходит
    "rule_24",  # Покупатель недоволен
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
        "keywords": ["товар есть", "есть сейчас", "актуально", "продаешь", "продаёшь", "остались", "есь", "актуалка"],
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
        "keywords": ["оплатил", "оплатил уже", "деньги отправил", "кинул оплату", "все оплачено", "всё оплачено"],
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
        "keywords": ["игноришь", "почему молчишь", "ответь", "ты живой"],
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


def keyword_matches(norm_text: str, norm_kw: str) -> bool:
    if not norm_text or not norm_kw:
        return False
    if " " not in norm_kw:
        max_dist = 0 if len(norm_kw) <= 2 else (1 if len(norm_kw) <= 5 else 2)
        for word in norm_text.split(" "):
            if max_dist == 0 and word == norm_kw:
                return True
            if max_dist > 0 and damerau_levenshtein_within(word, norm_kw, max_dist):
                return True
        compact = norm_text.replace(" ", "")
        if len(norm_kw) >= 4 and norm_kw in compact:
            return True
        return False

    if norm_kw in norm_text:
        return True
    kw_words = norm_kw.split(" ")
    txt_words = norm_text.split(" ")
    if len(txt_words) >= len(kw_words):
        for i in range(len(txt_words) - len(kw_words) + 1):
            ok = True
            for kw_w, tx_w in zip(kw_words, txt_words[i : i + len(kw_words)]):
                d = 0 if len(kw_w) <= 2 else (1 if len(kw_w) <= 6 else 2)
                if not damerau_levenshtein_within(tx_w, kw_w, d):
                    ok = False
                    break
            if ok:
                return True
    return False


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
            self.save_rules()
            return

        try:
            raw = json.loads(RULES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.auto_reply_enabled = bool(raw.get("auto_reply_enabled", True))
                raw_rules = raw.get("rules", [])
            elif isinstance(raw, list):
                self.auto_reply_enabled = True
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
                "rules": [asdict(r) for r in self.rules],
            }
        try:
            RULES_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.error("Не удалось сохранить правила автоответчика: %s", type(exc).__name__)

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
            if not self.auto_reply_enabled:
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
            if not self.auto_reply_enabled:
                return None, [], False
            enabled_rules = [r for r in self.rules if r.enabled]
            chat_ctx = next((c for c in self.recent_chats if c.chat_id == chat_id), None)
            prev_rule_id = chat_ctx.last_rule_id if chat_ctx else None
            last_reply_text = chat_ctx.last_reply_text if chat_ctx else None
            last_ack_at = chat_ctx.last_ack_at if chat_ctx else None

        if norm in {"а дешевле", "дешевле", "почем", "сколько"} and prev_rule_id == "rule_07":
            disc_rule = next((r for r in enabled_rules if r.id == "rule_08"), None)
            if disc_rule:
                return disc_rule.reply, [disc_rule], False

        matched: List[AutoReplyRule] = []
        for r in enabled_rules:
            for kw in r.keywords:
                if keyword_matches(norm, kw):
                    matched.append(r)
                    break

        if not matched:
            return None, [], True

        priority_hits = [r for r in matched if r.id in PRIORITY_RULE_IDS]
        if priority_hits:
            chosen = priority_hits[0]
            if chosen.reply == last_reply_text:
                return None, [chosen], False
            return chosen.reply, [chosen], False

        if len(matched) == 1 and matched[0].id in ACK_ONLY_RULE_IDS:
            now = time.time()
            if last_ack_at is not None and (now - last_ack_at) < 120:
                return None, matched, False
            if matched[0].reply == last_reply_text:
                return None, matched, False
            return matched[0].reply, matched, False

        replies: List[str] = []
        seen_replies: Set[str] = set()
        for r in matched:
            rep = r.reply.strip()
            if rep and rep not in seen_replies and rep != last_reply_text:
                seen_replies.add(rep)
                replies.append(rep)

        if not replies:
            return None, matched, False

        return " ".join(replies), matched, False


state = BotState()
funpay_account: Optional[Account] = None
active_runner: Optional[Runner] = None
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
    """
    Классифицирует ошибку опроса/сессии FunPay:
    возвращает (code, title, hint).
    """
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
            "FunPay может ограничивать IP-адрес сервера/хостинга (Railway/дата-центр) при прямом подключении.",
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
        "Проверьте логи деплоя на Railway для уточнения деталей.",
    )


def compute_backoff_seconds(error_count: int) -> float:
    if error_count <= 0:
        return POLL_DELAY_SECONDS
    delay = POLL_DELAY_SECONDS * (2 ** min(error_count - 1, 5))
    return min(MAX_RETRY_BACKOFF_SECONDS, max(POLL_DELAY_SECONDS, delay))


def apply_browser_masking(account_obj: Any, runner_obj: Optional[Any] = None) -> Dict[str, Any]:
    """
    Применяет браузерные заголовки (User-Agent, Referer, Origin, Sec-Fetch-*, Accept-*)
    к сессиям Account и Runner при прямом подключении без прокси.
    """
    applied_info: Dict[str, Any] = {
        "headers_applied": False,
        "user_agent": FUNPAY_USER_AGENT,
    }
    targets: List[Any] = []
    if account_obj is not None:
        targets.append(account_obj)
    if runner_obj is not None:
        targets.append(runner_obj)

    for obj in targets:
        headers_dict = getattr(obj, "headers", None)
        if isinstance(headers_dict, dict):
            headers_dict.update(BROWSER_HEADERS)
            headers_dict["User-Agent"] = FUNPAY_USER_AGENT
            applied_info["headers_applied"] = True

        session_obj = getattr(obj, "session", None)
        if session_obj is not None:
            sess_headers = getattr(session_obj, "headers", None)
            if isinstance(sess_headers, dict):
                sess_headers.update(BROWSER_HEADERS)
                sess_headers["User-Agent"] = FUNPAY_USER_AGENT
                applied_info["headers_applied"] = True

    return applied_info


def create_funpay_account(golden_key: str) -> Account:
    """Создаёт Account с маскировкой под браузер при прямом подключении."""
    try:
        acc = Account(golden_key, user_agent=FUNPAY_USER_AGENT)
    except TypeError:
        acc = Account(golden_key)
    apply_browser_masking(acc, None)
    return acc


def probe_funpay_connection(acc: Optional[Account]) -> Tuple[bool, str]:
    """Контроль соединения — проверяет доступность FunPay и актуальность аккаунта."""
    if acc is None:
        return False, "Аккаунт FunPay не инициализирован"
    try:
        apply_browser_masking(acc, active_runner)
        if hasattr(acc, "get") and callable(acc.get):
            acc.get()
        return True, "FunPay доступен, соединение проверено"
    except Exception as exc:
        _, title, _ = classify_funpay_issue(exc)
        return False, title


def stop_active_runner_safely() -> None:
    with funpay_lock:
        runner = active_runner
    if runner is not None:
        for attr in ("stop", "close"):
            fn = getattr(runner, attr, None)
            if callable(fn):
                try:
                    fn()
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
            InlineKeyboardButton("📋 Правила автоответа", callback_data="menu:rules"),
            InlineKeyboardButton("❓ Помощь", callback_data="menu:help"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def send_funpay_message(chat_id: int, text: str) -> Tuple[bool, Optional[str]]:
    clean_text = (text or "").strip()
    if not clean_text:
        return False, "Пустое сообщение не отправлено."
    with funpay_lock:
        if funpay_account is None:
            return False, "Подключение к FunPay ещё не установлено."
        try:
            apply_browser_masking(funpay_account, active_runner)
            funpay_account.send_message(chat_id, clean_text)
            return True, None
        except Exception as exc:
            err = sanitize_error_text(exc)
            log.error("Ошибка отправки сообщения в чат %s: %s", chat_id, err)
            with state.lock:
                state.last_error = err
            return False, err


def extract_order_info(account: Account, buyer_username: str) -> Optional[str]:
    try:
        if hasattr(account, "get_sells"):
            sells = account.get_sells()
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
                log.error("Не удалось отправить уведомление в Telegram: %s", type(exc).__name__)

        future.add_done_callback(_on_done)
    except Exception as exc:
        log.error("Ошибка постановки задачи уведомления в Telegram: %s", type(exc).__name__)


def _mark_poll_healthy(username: Optional[str] = None) -> bool:
    now = time.time()
    with state.lock:
        had_errors = state.consecutive_poll_errors > 0 or state.diagnosis_code != "ok"
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

    log.warning(
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
    """Защита от зависания: отслеживает зависание опроса событий и периодически проверяет соединение."""
    while True:
        time.sleep(10.0)
        now = time.time()

        with state.lock:
            last_ref = max(state.last_poll_at or 0.0, state.last_ok_poll_at or 0.0)
            last_check = state.last_conn_check_at or 0.0
            has_key = bool(FUNPAY_GOLDEN_KEY)

        if not has_key:
            continue

        # Периодический контроль соединения с FunPay
        if (now - last_check) >= CONNECTION_CHECK_INTERVAL:
            with funpay_lock:
                acc_ref = funpay_account
            ok, msg = probe_funpay_connection(acc_ref)
            with state.lock:
                state.last_conn_check_at = now
                state.connection_status = msg if ok else f"Проблема проверки связи: {msg}"

        # Защита от зависания потока опроса событий
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


def funpay_worker(golden_key: str, tg_app: Application, loop: asyncio.AbstractEventLoop) -> None:
    global funpay_account, active_runner
    backoff = POLL_DELAY_SECONDS

    watchdog = threading.Thread(
        target=runner_watchdog_loop,
        args=(tg_app, loop),
        name="FunPayWatchdogThread",
        daemon=True,
    )
    watchdog.start()

    while True:
        try:
            log.info("Подключение к FunPay (маскировка браузера включена, прямое соединение)...")
            acc = create_funpay_account(golden_key).get()
            apply_browser_masking(acc, None)
            with funpay_lock:
                funpay_account = acc

            ok_conn, conn_msg = probe_funpay_connection(acc)
            recovered = _mark_poll_healthy(getattr(acc, "username", None) or "Аккаунт FunPay")
            with state.lock:
                state.last_conn_check_at = time.time()
                state.connection_status = (
                    f"{conn_msg} (браузер-заголовки, без прокси)"
                    if ok_conn
                    else "Подключено, проверка связи требует внимания"
                )

            backoff = POLL_DELAY_SECONDS
            log.info("Подключение к FunPay установлено (%s).", state.funpay_username)

            if recovered:
                notify_admin_sync(
                    tg_app,
                    loop,
                    "✅ <b>Связь и опрос событий FunPay восстановлены.</b> Бот снова получает сообщения.",
                    alert_key="recovered",
                    force=True,
                )

            runner = Runner(acc)
            apply_browser_masking(acc, runner)
            with funpay_lock:
                active_runner = runner

            for event in runner.listen(requests_delay=POLL_DELAY_SECONDS):
                _mark_poll_healthy()

                if event.type is not enums.EventTypes.NEW_MESSAGE:
                    continue

                msg = getattr(event, "message", None)
                if msg is None:
                    continue

                author_id = getattr(msg, "author_id", None)
                my_id = getattr(acc, "id", None)
                if my_id is not None and author_id == my_id:
                    continue

                chat_id = getattr(msg, "chat_id", None)
                msg_id = getattr(msg, "id", None)
                text = getattr(msg, "text", None) or "[вложение / нетекстовое сообщение]"
                buyer_username = getattr(msg, "author", None) or f"User_{author_id}"

                if chat_id is None:
                    continue

                dedup_key = (
                    f"{chat_id}:{msg_id}"
                    if msg_id is not None
                    else f"{chat_id}:{hash(text)}:{int(time.time() // 5)}"
                )
                if not state.mark_seen_if_new(dedup_key):
                    continue

                order_info = extract_order_info(acc, str(buyer_username))
                state.record_chat(int(chat_id), str(buyer_username), str(text), order_info)

                order_line = f"\n📦 <b>Заказ:</b> {escape_html(order_info)}" if order_info else ""
                notif_html = (
                    f"💬 <b>Новое сообщение на FunPay</b>\n"
                    f"👤 <b>Покупатель:</b> {escape_html(str(buyer_username))} (чат <code>{chat_id}</code>)"
                    f"{order_line}\n"
                    f"📝 <b>Текст:</b>\n{escape_html(str(text))}"
                )
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Ответить", callback_data=f"quick_reply:{chat_id}")]]
                )
                notify_admin_sync(tg_app, loop, notif_html, reply_markup=kb)

                with state.lock:
                    auto_active = state.auto_reply_enabled

                if auto_active:
                    composed_reply, matched_rules, is_uncertain = state.evaluate_message(int(chat_id), str(text))
                    if matched_rules and composed_reply:
                        rule_tag = ",".join(r.id for r in matched_rules)
                        auto_key = f"auto:{chat_id}:{msg_id}:{rule_tag}"
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
                                    loop,
                                    f"🤖 <b>Автоответ отправлен</b> покупателю <b>{escape_html(str(buyer_username))}</b> "
                                    f"({escape_html(titles)}).",
                                )
                            else:
                                notify_admin_sync(
                                    tg_app,
                                    loop,
                                    f"⚠️ <b>Ошибка автоответа:</b> <code>{escape_html(err)}</code>",
                                )
                    elif is_uncertain:
                        allowed, wait_sec = state.claim_fallback_slot(int(chat_id))
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
                                    loop,
                                    f"🕒 <b>Резервный ответ отправлен</b> покупателю <b>{escape_html(str(buyer_username))}</b> "
                                    f"(интервал: {FALLBACK_COOLDOWN_SECONDS} сек). Требует внимания продавца.",
                                )
                            else:
                                with state.lock:
                                    state.fallback_last_sent.pop(int(chat_id), None)
                                notify_admin_sync(
                                    tg_app,
                                    loop,
                                    f"⚠️ <b>Ошибка резервного ответа:</b> <code>{escape_html(err)}</code>",
                                )
                        else:
                            log.info(
                                "Резервный ответ для чата %s пропущен антиспамом (осталось %s сек.)",
                                chat_id,
                                wait_sec,
                            )

        except Exception as exc:
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
    await update.message.reply_text(
        "🤖 <b>FunPay Manager</b> запущен.\n\n"
        f"🛒 FunPay: <b>{fp_line}</b>\n"
        f"🛡️ Автоответчик: <b>{auto_line}</b>\n\n"
        "Используйте кнопки ниже или команды:\n"
        "/status — состояние подключений\n"
        "/test — проверка работоспособности\n"
        "/reply — ответить покупателю\n"
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
        active_rules = sum(1 for r in state.rules if r.enabled)
        total_rules = len(state.rules)

    if not FUNPAY_GOLDEN_KEY:
        fp_status = "⚠️ Не задана переменная FUNPAY_GOLDEN_KEY"
    elif fp_ok:
        fp_status = f"🟢 Подключён ({escape_html(fp_user)})"
    else:
        fp_status = "🔴 Нет соединения"

    poll_info = "ещё не выполнялся" if not last_poll else f"{int(time.time() - last_poll)} сек. назад"
    err_block = f"\n⚠️ <b>Последняя ошибка:</b> <code>{escape_html(last_err)}</code>" if last_err else ""
    mask_line = "🎭 <b>Маскировка:</b> заголовки браузера (Chrome 131), прямое соединение без прокси"

    await update.message.reply_text(
        "📊 <b>Состояние системы:</b>\n\n"
        "🟢 <b>Telegram-бот:</b> работает\n"
        f"🛒 <b>FunPay:</b> {fp_status}\n"
        f"🌐 <b>Соединение:</b> {escape_html(conn_status)}\n"
        f"{mask_line}\n"
        f"⏱ <b>Последний опрос:</b> {poll_info} (интервал: {POLL_DELAY_SECONDS:.0f}с, рестартов: {hang_count})\n"
        f"🩺 <b>Диагностика:</b> <code>{escape_html(diag_code)}</code> — {escape_html(diag_detail)} "
        f"(ошибок подряд: {err_streak}, пауза: {cur_backoff:.0f}с)\n"
        f"🛡️ <b>Автоответчик:</b> {'🟢 включён' if auto_on else '🔴 выключен'} "
        f"(активно правил: {active_rules}/{total_rules}, резерв: раз в {FALLBACK_COOLDOWN_SECONDS} сек)"
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
        "/auto_on — включить автоответчик\n"
        "/auto_off — выключить автоответчик\n"
        "/help — эта справка\n\n"
        "Распознаёт сленг, опечатки, повторы букв, латиницу и сообщения без пунктуации.\n"
        f"Если смысл не распознан — отправляется резервный ответ (раз в {FALLBACK_COOLDOWN_SECONDS} сек на чат), "
        "а уведомление приходит вам.",
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
            last_err = state.last_error
            diag_code = state.diagnosis_code
            err_streak = state.consecutive_poll_errors
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
            f"🛡️ Автоответчик: {'🟢 ВКЛ' if auto_on else '🔴 ВЫКЛ'}{err_part}",
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
            "📖 Команды: /start, /status, /test, /reply, /auto_on, /auto_off, /help.\n"
            f"При сомнении отправляется резервный ответ (раз в {FALLBACK_COOLDOWN_SECONDS} сек на чат).",
            reply_markup=main_menu_keyboard(),
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
    app.add_handler(CommandHandler("auto_on", auto_on))
    app.add_handler(CommandHandler("auto_off", auto_off))
    app.add_handler(CommandHandler("private", deny))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    log.info("FunPay Manager Telegram interface starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
