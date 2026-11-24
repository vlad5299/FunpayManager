import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("funpay-manager")


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "🤖 FunPay Manager запущен.\n\n"
        "Пока работает безопасный тестовый режим: "
        "управление доступно только владельцу.\n\n"
        "/status — состояние бота\n"
        "/test — проверить уведомления\n"
        "/help — справка"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "🟢 Telegram-бот работает.\n"
        "🔴 Подключение FunPay ещё не настроено.\n"
        "🛡️ Автоматические ответы отключены."
    )


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "✅ Тестовое уведомление получено.\n"
        "Связь Telegram ↔ облачный бот работает."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Команды:\n"
        "/start — открыть панель\n"
        "/status — проверить состояние\n"
        "/test — тестовое уведомление\n"
        "/help — эта справка\n\n"
        "Подключение к FunPay добавим отдельным модулем "
        "после проверки доступного и разрешённого способа интеграции."
    )


async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Доступ закрыт.")


def main():
    if not TOKEN:
        raise RuntimeError("Не задана переменная TELEGRAM_BOT_TOKEN")
    if not ADMIN_ID:
        raise RuntimeError("Не задана переменная ADMIN_ID")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", lambda u, c: u.effective_message.reply_text(
        f"Ваш Telegram ID: {u.effective_user.id}"
    )))
    app.add_handler(CommandHandler("private", deny))
    log.info("FunPay Manager Telegram interface starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
