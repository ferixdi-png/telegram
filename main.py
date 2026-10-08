import os
import hmac
import logging
from html import escape
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from content import (
    STEPS, INTRO, SHOWCASE, CASES, VIDEO_HOOK, CASES_URL, VIDEO_CAPTION,
    FINAL_MENU, JOIN_TEXT, STUDENT_TEXT, BUDGET_TEXT, MARKET_TEXT, ZERO_TEXT, LATER_TEXT, CONTACT_URL,
)
from video_source import get_original_video, VIDEO_SOURCE_URL

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
cached_video_file_id = os.getenv("VIDEO_FILE_ID", "").strip()
example_photo_ids = [v.strip() for v in os.getenv("EXAMPLE_PHOTO_IDS", "").split(",") if v.strip()][:11]

def keyboard(index: int):
    if index == -2:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🏁 Теперь к 15 мифам", callback_data="next:0")
        ]])
    if index == -1:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Сначала покажи примеры", callback_data="show_examples")
        ]])
    elif index < len(STEPS) - 1:
        label = STEPS[index]["button"]
    elif index == len(STEPS) - 1:
        label = "📊 Покажи свои реальные цифры"
    elif index == len(STEPS):
        label = "🎥 А как ты это делаешь?"
    elif index == len(STEPS) + 1:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Забрать видео с разбором", callback_data="final_video")
        ]])
    else:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Смотреть кейсы с нуля", url=CASES_URL)
        ]])
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"next:{index+1}")]])

def route_keyboard(route: str):
    if route == "menu":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Хочу на обучение", callback_data="route:join")],
            [InlineKeyboardButton("✅ Я уже на обучении", callback_data="route:student")],
            [InlineKeyboardButton("💸 Пока дороговато", callback_data="route:budget")],
            [InlineKeyboardButton("🤖 AI-рынок перегрет?", callback_data="route:market")],
            [InlineKeyboardButton("🚀 С нуля сейчас сложно?", callback_data="route:zero")],
            [InlineKeyboardButton("⏳ Вернусь к этому позже", callback_data="route:later")],
            [InlineKeyboardButton("📊 Сайт и реальные кейсы", url=CASES_URL)],
        ])

    if route in ("join", "budget", "market", "zero"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Программа и кейсы на сайте", url=CASES_URL)],
            [InlineKeyboardButton("💬 Написать @ferixdiii", url=CONTACT_URL)],
            [InlineKeyboardButton("↩️ К выбору", callback_data="route:menu")],
        ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ К выбору", callback_data="route:menu")],
    ])


def page(index: int):
    if index == -1:
        return INTRO
    if index < len(STEPS):
        step = STEPS[index]
        return (
            f'🏁 <b>МИФ {index+1:02d}/{len(STEPS)}</b>\n\n'
            f'<b>{escape(step["myth"])}</b>\n\n'
            f'{escape(step["answer"])}'
        )
    if index == len(STEPS):
        return CASES
    return VIDEO_HOOK

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(page(-1), reply_markup=keyboard(-1), disable_web_page_preview=True, parse_mode="HTML")

async def show_examples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    if example_photo_ids:
        try:
            if len(example_photo_ids) == 1:
                await appbot.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=example_photo_ids[0],
                )
            elif len(example_photo_ids) <= 10:
                await appbot.bot.send_media_group(
                    chat_id=query.message.chat_id,
                    media=[InputMediaPhoto(media=photo_id) for photo_id in example_photo_ids],
                )
            else:
                for batch in (example_photo_ids[:6], example_photo_ids[6:]):
                    await appbot.bot.send_media_group(
                        chat_id=query.message.chat_id,
                        media=[InputMediaPhoto(media=photo_id) for photo_id in batch],
                    )
        except TelegramError as exc:
            log.warning("Could not send example photo gallery: %s", type(exc).__name__)

    await query.message.reply_text(
        SHOWCASE,
        reply_markup=keyboard(-2),
        disable_web_page_preview=True,
        parse_mode="HTML",
    )
    log.info("funnel_showcase")


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect image file IDs in the sender's chat for /galleryids; not public funnel state."""
    if not update.message or not update.message.photo:
        return
    ids = context.chat_data.setdefault("pending_example_ids", [])
    new_id = update.message.photo[-1].file_id
    if new_id not in ids:
        ids.append(new_id)
    context.chat_data["pending_example_ids"] = ids[-11:]


async def galleryids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = context.chat_data.get("pending_example_ids", [])
    if not ids:
        await update.message.reply_text(
            "📸 Пришли 11 скриншотов обычными фотографиями, затем отправь /galleryids."
        )
        return
    await update.message.reply_text(
        "✅ Фото для примеров сохранены в этом чате.\n\n"
        "Скопируй в Render → Environment:\n"
        "KEY: EXAMPLE_PHOTO_IDS\n"
        "VALUE:\n" + ",".join(ids) + "\n\n"
        "Сохрани настройки и дождись Live."
    )


async def galleryclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["pending_example_ids"] = []
    await update.message.reply_text("📸 Подборка очищена. Можно отправлять новые фото.")


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
    await query.message.reply_text(page(index), reply_markup=keyboard(index), disable_web_page_preview=True, parse_mode="HTML")
    log.info("funnel_step=%s", index)

def id_message(file_id: str) -> str:
    return (
        "✅ Видео получил! Telegram сохранил оригинальный файл.\n\n"
        "Теперь Render → Environment:\n"
        "KEY: VIDEO_FILE_ID\n"
        f"VALUE: {file_id}\n\n"
        "Добавь переменную, сохрани и дождись статуса Live. "
        "Последняя кнопка воронки начнёт выдавать этот MP4."
    )


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document:
        return
    if not (document.file_name or "").lower().endswith(".mp4"):
        await update.message.reply_text("🎬 Для финального этапа пришли MP4 как файл.")
        return
    context.chat_data["last_mp4_file_id"] = document.file_id
    await update.message.reply_text(id_message(document.file_id))


async def fileid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    original = msg.reply_to_message if msg else None
    file_id = original.document.file_id if original and original.document else None
    file_id = file_id or context.chat_data.get("last_mp4_file_id")
    if file_id:
        await msg.reply_text(id_message(file_id))
    else:
        await msg.reply_text(
            "🎬 Пришли исходный MP4 как файл. "
            "Бот сразу ответит с VIDEO_FILE_ID для Render."
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = "Telegram file_id" if cached_video_file_id else "GitHub Releases"
    await update.message.reply_text(
        "🛠 Ferixdi Bot v2.8\n"
        "15 мифов и кнопки активны.\n"
        f"Видео: источник {source}.\n"
        f"Примеры: {len(example_photo_ids)} фото."
    )


async def deliver_original_video(source_message):
    """Send the unchanged MP4 document, keeping the webhook response fast."""
    global cached_video_file_id
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Смотреть кейсы на сайте", url=CASES_URL)],
        [InlineKeyboardButton("🏁 Видео посмотрел, что дальше?", callback_data="video_finished")],
    ])
    try:
        if cached_video_file_id:
            result = await appbot.bot.send_document(
                chat_id=source_message.chat_id,
                document=cached_video_file_id,
                caption=VIDEO_CAPTION,
                parse_mode="HTML",
                reply_markup=markup,
                read_timeout=180,
                write_timeout=180,
            )
        else:
            video_path = await get_original_video()
            with video_path.open("rb") as video:
                result = await appbot.bot.send_document(
                    chat_id=source_message.chat_id,
                    document=video,
                    filename="ferixdi-process.mp4",
                    caption=VIDEO_CAPTION,
                    parse_mode="HTML",
                    reply_markup=markup,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                )
            if result.document:
                cached_video_file_id = result.document.file_id
        try:
            await source_message.edit_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        log.info("final_original_video_sent")
    except Exception as exc:
        log.warning("Original video delivery failed: %s", type(exc).__name__)
        await source_message.reply_text(
            "🎬 Видео сейчас готовится к выдаче. Загляни чуть позже."
        )


async def final_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🎬 Готовлю оригинальный MP4...")
    appbot.create_task(deliver_original_video(query.message))


async def video_finished(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Keep the link to cases on the original MP4, but move the menu to a text message.
    try:
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Смотреть кейсы на сайте", url=CASES_URL)
            ]])
        )
    except TelegramError:
        pass

    await query.message.reply_text(
        FINAL_MENU,
        reply_markup=route_keyboard("menu"),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    log.info("funnel_final_menu_opened")


async def final_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    route = query.data.split(":", 1)[1]
    copy = {
        "menu": FINAL_MENU,
        "join": JOIN_TEXT,
        "student": STUDENT_TEXT,
        "budget": BUDGET_TEXT,
        "market": MARKET_TEXT,
        "zero": ZERO_TEXT,
        "later": LATER_TEXT,
    }
    if route not in copy:
        return
    await query.edit_message_text(
        copy[route],
        reply_markup=route_keyboard(route),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    log.info("funnel_final_route=%s", route)


appbot.add_handler(CommandHandler("start", start))
appbot.add_handler(CommandHandler("fileid", fileid))
appbot.add_handler(CommandHandler("status", status))
appbot.add_handler(CommandHandler("galleryids", galleryids))
appbot.add_handler(CommandHandler("galleryclear", galleryclear))
appbot.add_handler(MessageHandler(filters.PHOTO, receive_photo))
appbot.add_handler(MessageHandler(filters.Document.ALL, receive_document))
appbot.add_handler(CallbackQueryHandler(next_step, pattern=r"^next:\d+$"))
appbot.add_handler(CallbackQueryHandler(show_examples, pattern=r"^show_examples$"))
appbot.add_handler(CallbackQueryHandler(final_video, pattern=r"^final_video$"))
appbot.add_handler(CallbackQueryHandler(video_finished, pattern=r"^video_finished$"))
appbot.add_handler(CallbackQueryHandler(final_route, pattern=r"^route:(menu|join|student|budget|market|zero|later)$"))

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
        # Leave the webhook configured during a rolling Render deployment.
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
