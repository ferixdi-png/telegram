import os
import hmac
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError
from content import STEPS, INTRO, CASES, OFFER, COURSE_URL, VIDEO_CAPTION

logging.basicConfig(level=logging.INFO)
# httpx logs request URLs; Telegram Bot API URLs contain the bot token.
# Keep these requests out of Render logs to avoid leaking credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("ferixdi")
TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "https://" + os.environ["RENDER_EXTERNAL_HOSTNAME"]).rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
PATH = "/telegram/webhook"
appbot = Application.builder().token(TOKEN).updater(None).build()

def keyboard(index: int):
    if index == -1:
        label = "🔥 Узнать первый миф"
    elif index < len(STEPS) - 1:
        label = STEPS[index]["button"]
    elif index == len(STEPS) - 1:
        label = "📊 Посмотреть реальные кейсы"
    elif index == len(STEPS):
        label = "🎓 Как устроено обучение?"
    elif index == len(STEPS) + 1:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Получить оригинальное видео", callback_data="final_video")
        ]])
    else:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Посмотреть программу", url=COURSE_URL)]])
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"next:{index+1}")]])

def page(index: int):
    if index == -1:
        return INTRO
    if index < len(STEPS):
        step = STEPS[index]
        return f'🔥 МИФ №{index+1} ИЗ {len(STEPS)}\n\n❌ {step["myth"]}\n\n✅ МОЙ ОПЫТ\n{step["answer"]}'
    if index == len(STEPS):
        return CASES
    return OFFER

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(page(-1), reply_markup=keyboard(-1), disable_web_page_preview=True)

async def next_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if not 0 <= index <= len(STEPS) + 1:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(page(index), reply_markup=keyboard(index), disable_web_page_preview=True)
    log.info("funnel_step=%s", index)

async def fileid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get the reusable Telegram file_id without downloading or altering the file."""
    msg = update.message
    original = msg.reply_to_message if msg else None
    if not original or not original.document:
        await msg.reply_text(
            "🎬 Пришли исходный MP4 в этот чат именно как ФАЙЛ "
            "(без сжатия). Затем ответь на сообщение с файлом командой /fileid."
        )
        return
    document = original.document
    await msg.reply_text(
        "✅ Файл сохранён на стороне Telegram без перекодирования.\n\n"
        "Добавь в Render → Environment:\n"
        "KEY: VIDEO_FILE_ID\n"
        f"VALUE: {document.file_id}\n\n"
        "Сохрани и дождись перезапуска сервиса. "
        "После этого последний шаг воронки будет отправлять исходный MP4."
    )


async def final_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    file_id = os.getenv("VIDEO_FILE_ID", "").strip()
    if not file_id:
        await query.message.reply_text("🎬 Видео скоро появится здесь. Загляни немного позже.")
        return

    try:
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_id,
            caption=VIDEO_CAPTION,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Посмотреть программу обучения", url=COURSE_URL)
            ]]),
            read_timeout=90,
            write_timeout=90,
        )
    except TelegramError as exc:
        log.error("Final original file delivery failed: %s", type(exc).__name__)
        await query.message.reply_text("Видео временно недоступно. Попробуй ещё раз позже.")
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    log.info("final_original_video_sent")


appbot.add_handler(CommandHandler("start", start))
appbot.add_handler(CommandHandler("fileid", fileid))
appbot.add_handler(CallbackQueryHandler(next_step, pattern=r"^next:\d+$"))
appbot.add_handler(CallbackQueryHandler(final_video, pattern=r"^final_video$"))

@asynccontextmanager
async def lifespan(app):
    await appbot.initialize()
    await appbot.start()
    await appbot.bot.set_webhook(
        url=WEBHOOK_BASE_URL + PATH,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"],
    )
    log.info("Webhook initialized")
    try:
        yield
    finally:
        await appbot.bot.delete_webhook()
        await appbot.stop()
        await appbot.shutdown()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    return {"status": "ok", "bot": "Ferixdi AI Reels"}

@app.post(PATH)
async def telegram_webhook(request: Request):
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(sent, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)
    payload = await request.json()
    await appbot.process_update(Update.de_json(payload, appbot.bot))
    return {"ok": True}
