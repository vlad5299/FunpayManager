import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from FunPayAPI import Account, Runner, enums
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("funpay-manager")
# Скрываем URL запросов в логах httpx/telegram, чтобы токен бота не попал в логи Railway
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

RULES_FILE = Path("autoresponder_rules.json")
MAX_SEEN_IDS = 5000
MAX_RECENT_CHATS = 12


@dataclass
class AutoReplyRule:
    id: str
    keywords: List[str]
    reply: str


@dataclass
class ChatSummary:
    chat_id: int
    buyer_username: str
    last_message: str
    order_info: Optional[str]
    timestamp: float


class BotState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # Автоответчик строго ВЫКЛЮЧЕН при каждом запуске проекта
        self.auto_reply_enabled: bool = False
        self.funpay_connected: bool = False
        self.funpay_username: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_poll_at: Optional[float] = None
        self.seen_message_ids: deque[str] = deque(maxlen=MAX_SEEN_IDS)
        self.seen_set: set[str] = set()
        self.auto_replied_keys: deque[str] = deque(maxlen=MAX_SEEN_IDS)
        self.auto_replied_set: set[str] = set()
        self.recent_chats: List[ChatSummary] = []
        self.last_chat_id: Optional[int] = None
        self.rules: List[AutoReplyRule] = self._load_rules()
        self.pending_input: Dict[int, Dict[str, Any]] = {}

    def _load_rules(self) -> List[AutoReplyRule]:
        if not RULES_FILE.exists():
            return []
        try:
            raw = json.loads(RULES_FILE.read_text(encoding="utf-8"))
            rules: List[AutoReplyRule] = []
            for item in raw:
                if isinstance(item, dict) and "id" in item and "keywords" in item and "reply" in item:
                    rules.append(
                        AutoReplyRule(
                            id=str(item["id"]),
                            keywords=[str(k).strip().lower() for k in item["keywords"] if str(k).strip()],
                            reply=str(item["reply"]),
                        )
                    )
            return rules
        except Exception as exc:
            log.error("Не удалось загрузить правила автоответчика: %s", type(exc).__name__)
            return []

    def save_rules(self) -> None:
        with self.lock:
            payload = [asdict(r) for r in self.rules]
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

    def record_chat(self, chat_id: int, buyer_username: str, text: str, order_info: Optional[str]) -> None:
        with self.lock:
            self.last_chat_id = chat_id
            summary = ChatSummary(
                chat_id=chat_id,
                buyer_username=buyer_username or f"Чат #{chat_id}",
                last_message=text,
                order_info=order_info,
                timestamp=time.time(),
            )
            self.recent_chats = [c for c in self.recent_chats if c.chat_id != chat_id]
            self.recent_chats.insert(0, summary)
            self.recent_chats = self.recent_chats[:MAX_RECENT_CHATS]

    def get_chat(self, chat_id: int) -> Optional[ChatSummary]:
        with self.lock:
            for c in self.recent_chats:
                if c.chat_id == chat_id:
                    return c
            return None

    def match_rule(self, text: str) -> Optional[AutoReplyRule]:
        normalized = (text or "").strip().lower()
        if not normalized:
            return None
        with self.lock:
            if not self.auto_reply_enabled:
                return None
            for rule in self.rules:
                for kw in rule.keywords:
                    if kw and kw in normalized:
                        return rule
        return None


state = BotState()
funpay_account: Optional[Account] = None
funpay_lock = threading.Lock()
worker_started = False
worker_start_lock = threading.Lock()


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
            funpay_account.send_message(chat_id, clean_text)
            return True, None
        except Exception as exc:
            err = sanitize_error_text(exc)
            log.error("Ошибка отправки сообщения в чат %s: %s", chat_id, type(exc).__name__)
            with state.lock:
                state.last_error = err
            return False, err


def extract_order_info(account: Account, buyer_username: str) -> Optional[str]:
    """Ищет активный/недавний заказ покупателя, если get_sells() доступен в FunPayAPI."""
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
) -> None:
    if tg_app is None or loop.is_closed():
        return
    coro = tg_app.bot.send_message(
        chat_id=ADMIN_ID,
        text=html_text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        future.result(timeout=15)
    except Exception as exc:
        log.error("Не удалось отправить уведомление в Telegram: %s", type(exc).__name__)


def funpay_worker(golden_key: str, tg_app: Application, loop: asyncio.AbstractEventLoop) -> None:
    global funpay_account
    backoff = 5

    while True:
        try:
            log.info("Подключение к FunPay...")
            acc = Account(golden_key).get()
            with funpay_lock:
                funpay_account = acc

            with state.lock:
                state.funpay_connected = True
                state.funpay_username = getattr(acc, "username", None) or "Аккаунт FunPay"
                state.last_error = None

            backoff = 5
            log.info("Подключение к FunPay установлено (%s).", state.funpay_username)

            runner = Runner(acc)
            for event in runner.listen(requests_delay=5):
                with state.lock:
                    state.last_poll_at = time.time()
                    state.funpay_connected = True

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

                # Автоответчик (строго по правилам, без придумывания ответов)
                matched_rule = state.match_rule(str(text))
                if matched_rule is not None:
                    auto_key = f"auto:{chat_id}:{msg_id}:{matched_rule.id}"
                    if state.mark_auto_replied_if_new(auto_key):
                        ok, err = send_funpay_message(int(chat_id), matched_rule.reply)
                        if ok:
                            notify_admin_sync(
                                tg_app,
                                loop,
                                f"🤖 <b>Автоответ отправлен</b> покупателю <b>{escape_html(str(buyer_username))}</b> "
                                f"(правило <code>{escape_html(matched_rule.id)}</code>).",
                            )
                        else:
                            notify_admin_sync(
                                tg_app,
                                loop,
                                f"⚠️ <b>Ошибка автоответа:</b> <code>{escape_html(err)}</code>",
                            )

        except Exception as exc:
            err_msg = sanitize_error_text(exc)
            log.error("Ошибка или обрыв соединения с FunPay: %s", type(exc).__name__)
            with state.lock:
                was_connected = state.funpay_connected
                state.funpay_connected = False
                state.last_error = err_msg
            with funpay_lock:
                funpay_account = None

            if was_connected:
                notify_admin_sync(
                    tg_app,
                    loop,
                    f"🔌 <b>Потеряно соединение с FunPay!</b>\n"
                    f"Причина: <code>{escape_html(err_msg)}</code>\n"
                    f"Повторная попытка через {backoff} сек.",
                )

            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


# --- Команды (сохранены все существующие + добавлены новые) ---

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
        auto_on = state.auto_reply_enabled
        rules_count = len(state.rules)

    if not FUNPAY_GOLDEN_KEY:
        fp_status = "⚠️ Не задана переменная FUNPAY_GOLDEN_KEY в Railway"
    elif fp_ok:
        fp_status = f"🟢 Подключён ({escape_html(fp_user)})"
    else:
        fp_status = "🔴 Нет соединения"

    poll_info = "ещё не выполнялся" if not last_poll else f"{int(time.time() - last_poll)} сек. назад"
    err_block = f"\n⚠️ <b>Последняя ошибка:</b> <code>{escape_html(last_err)}</code>" if last_err else ""

    await update.message.reply_text(
        "📊 <b>Состояние системы:</b>\n\n"
        "🟢 <b>Telegram-бот:</b> работает\n"
        f"🛒 <b>FunPay:</b> {fp_status}\n"
        f"⏱ <b>Последний опрос:</b> {poll_info}\n"
        f"🛡️ <b>Автоответчик:</b> {'🟢 включён' if auto_on else '🔴 выключен'} (правил: {rules_count})"
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

    if connected:
        fp_msg = f"🟢 Сессия FunPay активна (ID аккаунта: <code>{acc_id}</code>)."
    elif not FUNPAY_GOLDEN_KEY:
        fp_msg = "⚠️ Переменная <code>FUNPAY_GOLDEN_KEY</code> не задана в Railway."
    else:
        err = state.last_error or "ожидание подключения"
        fp_msg = f"🔴 FunPay пока не подключён (<code>{escape_html(err)}</code>)."

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
        "/status — проверить состояние Telegram и FunPay\n"
        "/test — проверить уведомления и сессию\n"
        "/reply — выбрать диалог или ответить на последнее сообщение\n"
        "/auto_on — включить автоответчик\n"
        "/auto_off — выключить автоответчик\n"
        "/help — эта справка\n\n"
        "Для управления шаблонами автоответов нажмите «📋 Правила автоответа» в меню.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with state.lock:
        state.auto_reply_enabled = True
    await update.message.reply_text(
        "🟢 <b>Автоответчик включён.</b>\n"
        "Ответы будут отправляться только при совпадении ключевых слов из ваших правил.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    with state.lock:
        state.auto_reply_enabled = False
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


async def render_rules_menu(target_message, edit: bool = False) -> None:
    with state.lock:
        rules = list(state.rules)
        enabled = state.auto_reply_enabled

    lines = [
        f"📋 <b>Правила автоответчика</b> ({'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}):",
        "Если подходящего правила нет, бот не отвечает сам.\n",
    ]
    buttons: List[List[InlineKeyboardButton]] = []

    if not rules:
        lines.append("Правил пока нет. Нажмите «➕ Добавить правило».")
    else:
        for idx, r in enumerate(rules, start=1):
            kw_str = ", ".join(r.keywords)
            preview = r.reply[:60] + ("…" if len(r.reply) > 60 else "")
            lines.append(f"{idx}. <b>[{escape_html(kw_str)}]</b> → {escape_html(preview)}")
            buttons.append(
                [
                    InlineKeyboardButton(f"✏️ Изменить #{idx}", callback_data=f"rule_edit:{r.id}"),
                    InlineKeyboardButton(f"🗑 Удалить #{idx}", callback_data=f"rule_del:{r.id}"),
                ]
            )

    buttons.append([InlineKeyboardButton("➕ Добавить правило", callback_data="rule_add")])
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="menu:home")])

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(buttons)
    if edit:
        await target_message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
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
        await query.message.edit_text(
            "🏠 <b>Главное меню FunPay Manager</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:status":
        with state.lock:
            fp_ok = state.funpay_connected
            fp_user = state.funpay_username or "—"
            auto_on = state.auto_reply_enabled
            last_err = state.last_error
        if not FUNPAY_GOLDEN_KEY:
            fp_part = "⚠️ не задан FUNPAY_GOLDEN_KEY"
        elif fp_ok:
            fp_part = f"🟢 ({escape_html(fp_user)})"
        else:
            fp_part = "🔴 нет связи"
        err_part = f"\n⚠️ <code>{escape_html(last_err)}</code>" if last_err else ""
        await query.message.edit_text(
            f"📊 <b>Статус:</b>\n"
            f"🟢 Telegram: работает\n"
            f"🛒 FunPay: {fp_part}\n"
            f"🛡️ Автоответчик: {'🟢 ВКЛ' if auto_on else '🔴 ВЫКЛ'}{err_part}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:test":
        with funpay_lock:
            ok = funpay_account is not None and state.funpay_connected
        await query.message.edit_text(
            "✅ Telegram-бот работает. Сессия FunPay активна."
            if ok
            else "⚠️ Telegram-бот работает, но сессия FunPay не активна.",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:help":
        await query.message.edit_text(
            "📖 Команды: /start, /status, /test, /reply, /auto_on, /auto_off, /help.\n"
            "Перед отправкой ответа бот всегда показывает получателя для подтверждения.",
            reply_markup=main_menu_keyboard(),
        )
    elif data == "menu:toggle_auto":
        with state.lock:
            state.auto_reply_enabled = not state.auto_reply_enabled
            new_state = state.auto_reply_enabled
        await query.message.edit_text(
            f"🛡️ Автоответчик теперь {'🟢 ВКЛЮЧЁН' if new_state else '🔴 ВЫКЛЮЧЕН'}.",
            parse_mode=ParseMode.HTML,
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
        await query.message.edit_text(
            f"✍️ Введите текст ответа для покупателя <b>{escape_html(buyer)}</b> (чат <code>{chat_id}</code>):",
            parse_mode=ParseMode.HTML,
        )
    elif data == "confirm_send:yes":
        with state.lock:
            pending = state.pending_input.pop(ADMIN_ID, None)
        if not pending or pending.get("mode") != "awaiting_confirm_send":
            await query.message.edit_text("⌛ Запрос на отправку устарел.", reply_markup=main_menu_keyboard())
            return
        target_chat_id = int(pending["target_chat_id"])
        buyer = str(pending["buyer_username"])
        reply_text = str(pending["reply_text"])
        ok, err = send_funpay_message(target_chat_id, reply_text)
        if ok:
            await query.message.edit_text(
                f"✅ Сообщение отправлено покупателю <b>{escape_html(buyer)}</b> (чат <code>{target_chat_id}</code>):\n"
                f"<blockquote>{escape_html(reply_text)}</blockquote>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(),
            )
        else:
            await query.message.edit_text(
                f"❌ Ошибка отправки: <code>{escape_html(err)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(),
            )
    elif data == "confirm_send:no":
        with state.lock:
            state.pending_input.pop(ADMIN_ID, None)
        await query.message.edit_text("🚫 Отправка отменена.", reply_markup=main_menu_keyboard())
    elif data == "menu:rules":
        await render_rules_menu(query.message, edit=True)
    elif data == "rule_add":
        with state.lock:
            state.pending_input[ADMIN_ID] = {"mode": "awaiting_new_rule"}
        await query.message.edit_text(
            "➕ <b>Новое правило автоответа</b>\n\n"
            "Отправьте сообщение в формате:\n"
            "<code>ключевое слово 1, ключевое слово 2 | Текст автоответа</code>",
            parse_mode=ParseMode.HTML,
        )
    elif data.startswith("rule_edit:"):
        rule_id = data.split(":", 1)[1]
        with state.lock:
            state.pending_input[ADMIN_ID] = {"mode": "awaiting_edit_rule", "rule_id": rule_id}
        await query.message.edit_text(
            f"✏️ <b>Редактирование правила <code>{escape_html(rule_id)}</code></b>\n\n"
            "Отправьте новые ключевые слова и текст в формате:\n"
            "<code>ключ1, ключ2 | Новый текст автоответа</code>",
            parse_mode=ParseMode.HTML,
        )
    elif data.startswith("rule_del:"):
        rule_id = data.split(":", 1)[1]
        with state.lock:
            state.rules = [r for r in state.rules if r.id != rule_id]
        state.save_rules()
        await render_rules_menu(query.message, edit=True)


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
        keywords = [k.strip().lower() for k in kw_part.split(",") if k.strip()]
        reply_text = reply_part.strip()
        if not keywords or not reply_text:
            await update.message.reply_text("⚠️ Укажите хотя бы одно ключевое слово и непустой текст ответа.")
            return

        with state.lock:
            state.pending_input.pop(ADMIN_ID, None)
            if mode == "awaiting_new_rule":
                new_id = f"r_{int(time.time() * 1000)}"
                state.rules.append(AutoReplyRule(id=new_id, keywords=keywords, reply=reply_text))
            else:
                rule_id = pending.get("rule_id")
                for r in state.rules:
                    if r.id == rule_id:
                        r.keywords = keywords
                        r.reply = reply_text
                        break
        state.save_rules()
        await render_rules_menu(update.message, edit=False)


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
