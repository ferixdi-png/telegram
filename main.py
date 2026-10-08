import os
import hmac
import logging
import asyncio
import io
from html import escape
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
from content import (
    STEPS, INTRO, SHOWCASE, CASES, CASES_URL, VIDEO_CAPTION,
    FINAL_MENU, DOUBTS_TEXT, JOIN_TEXT, STUDENT_TEXT, BUDGET_TEXT, MARKET_TEXT, ZERO_TEXT, EASY_TEXT, SKILLED_TEXT, LATER_TEXT, CONTACT_URL,
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
MAX_EXAMPLE_PHOTOS = 11

# Verified likes on the 11 screenshot originals, matched to the Telegram IDs
# given for this gallery. Keep only fingerprints in public source code.
# Other galleries can use EXAMPLE_PHOTO_LIKES, aligned with their ID order.
KNOWN_GALLERY_LIKES = {
    "5b0e1798": 136000,
    "f59e5840": 24600,
    "74d53395": 46400,
    "3479a1a3": 71800,
    "4a3cfa4c": 28700,
    "8323dfdd": 53400,
    "e5c7e7d3": 18100,
    "14acce47": 79000,
    "4afad786": 109000,
    "3390a63c": 23300,
    "5f1368aa": 36600,
}


def photo_tag(file_id: str) -> str:
    value = 2166136261
    for char in file_id:
        value = ((value ^ ord(char)) * 16777619) & 0xffffffff
    return f"{value:08x}"


def order_gallery_photos(ids: list[str]) -> tuple[list[str], bool]:
    if not ids:
        return ids, False

    manual = os.getenv("EXAMPLE_PHOTO_LIKES", "").strip()
    if manual:
        try:
            likes = [int(s.strip().replace(" ", "")) for s in manual.split(",")]
        except ValueError:
            likes = []
        if len(likes) == len(ids) and all(x >= 0 for x in likes):
            return [photo for photo, count in sorted(
                zip(ids, likes), key=lambda pair: pair[1], reverse=True
            )], True
        log.warning("EXAMPLE_PHOTO_LIKES must match EXAMPLE_PHOTO_IDS length")

    tags = [photo_tag(file_id) for file_id in ids]
    if len(ids) == MAX_EXAMPLE_PHOTOS and all(tag in KNOWN_GALLERY_LIKES for tag in tags):
        return sorted(ids, key=lambda file_id: KNOWN_GALLERY_LIKES[photo_tag(file_id)], reverse=True), True

    return ids, False


# Each Reels lesson can have a silent looping MP4/GIF shown above the full text.
# Set STEP_ANIMATION_01 ... STEP_ANIMATION_10 in Render Environment.
# Missing IDs automatically fall back to a regular text message.
step_animation_ids = {
    index: file_id
    for index in range(1, 11)
    if (file_id := os.getenv(f"STEP_ANIMATION_{index:02d}", "").strip())
}

# First lesson's MP4 was sent to this bot as a Telegram document.
# Telegram document IDs cannot directly be passed as animation IDs.
# On the first lesson request we re-upload it via send_animation and cache
# the resulting animation file_id for subsequent users of this process.
# STEP_ANIMATION_01 (a true animation ID) always takes priority if configured.
FIRST_STEP_MP4_DOCUMENT_ID = (
    "BQACAgIAAxkBAAO7asdXn8721DAThL8eSkEjnHtpOpMAAv6iAAJF4DhKVHOz1QP60cI9BA"
)
cached_first_step_animation_id = None
first_step_animation_lock = asyncio.Lock()

raw_example_photo_ids = [
    v.strip() for v in os.getenv("EXAMPLE_PHOTO_IDS", "").split(",") if v.strip()
][:MAX_EXAMPLE_PHOTOS]
example_photo_ids, photo_gallery_sorted = order_gallery_photos(raw_example_photo_ids)

def keyboard(index: int):
    if index == -2:
        buttons = [[InlineKeyboardButton("🏁 Давай к мифам", callback_data="next:0")]]
        if len(example_photo_ids) > 5:
            buttons.append([InlineKeyboardButton(
                f"📸 Ещё {len(example_photo_ids) - 5} скринов",
                callback_data="more_examples",
            )])
        return InlineKeyboardMarkup(buttons)
    if index == -1:
        if example_photo_ids:
            return InlineKeyboardMarkup([[
                InlineKeyboardButton("🔥 А какие ролики залетали?", callback_data="show_examples")
            ]])
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🏁 Поехали к мифам", callback_data="next:0")
        ]])
    if 0 <= index < len(STEPS):
        if index == len(STEPS) - 1:
            label = "📊 Ладно, показывай цифры"
        else:
            label = STEPS[index]["button"]
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(label, callback_data=f"next:{index + 1}")
        ]])
    if index == len(STEPS):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Скидывай видео!", callback_data="final_video")],
            [InlineKeyboardButton("🔎 Проверить все кейсы на сайте", url=CASES_URL)],
        ])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Кейсы на сайте", url=CASES_URL)
    ]])


def route_keyboard(route: str):
    if route == "menu":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎓 Хочу научиться так же", callback_data="route:join")],
            [InlineKeyboardButton("😎 Я уже умею, смотри", callback_data="route:skilled")],
            [InlineKeyboardButton("🤔 Есть пара вопросов", callback_data="route:doubts")],
            [InlineKeyboardButton("✅ Я уже с вами", callback_data="route:student")],
            [InlineKeyboardButton("📊 Глянуть все кейсы", url=CASES_URL)],
        ])

    if route == "doubts":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🧩 Боюсь запутаться", callback_data="route:easy")],
            [InlineKeyboardButton("💸 Для меня пока дорого", callback_data="route:budget")],
            [InlineKeyboardButton("🤖 AI-рынок перегрет?", callback_data="route:market")],
            [InlineKeyboardButton("🚀 Сейчас с нуля реально?", callback_data="route:zero")],
            [InlineKeyboardButton("⏳ Вернусь потом", callback_data="route:later")],
            [InlineKeyboardButton("↩️ Назад", callback_data="route:menu")],
        ])

    if route in ("join", "budget", "market", "zero", "easy"):
        back_to = "menu" if route == "join" else "doubts"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Глянуть программу", url=CASES_URL)],
            [InlineKeyboardButton("💬 Написать @ferixdiii", url=CONTACT_URL)],
            [InlineKeyboardButton("↩️ Назад", callback_data=f"route:{back_to}")],
        ])

    if route == "skilled":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Давай про трафик", url=CONTACT_URL)],
            [InlineKeyboardButton("↩️ Назад", callback_data="route:menu")],
        ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Назад", callback_data="route:menu")],
    ])


def page(index: int):
    if index == -1:
        return INTRO
    if 0 <= index < len(STEPS):
        step = STEPS[index]
        return (
            f'🏁 <b>ПРОВЕРЯЛ САМ {index + 1:02d}/{len(STEPS)}</b>\n\n'
            f'<b>{escape(step["myth"])}</b>\n\n'
            f'{escape(step["answer"])}'
        )
    return CASES


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
            visible = example_photo_ids[:5]
            if len(visible) == 1:
                await appbot.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=visible[0],
                )
            else:
                await appbot.bot.send_media_group(
                    chat_id=query.message.chat_id,
                    media=[InputMediaPhoto(media=photo_id) for photo_id in visible],
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


async def more_examples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    remaining = example_photo_ids[5:]
    if not remaining:
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass
    try:
        if len(remaining) == 1:
            await appbot.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=remaining[0],
            )
        else:
            await appbot.bot.send_media_group(
                chat_id=query.message.chat_id,
                media=[InputMediaPhoto(media=photo_id) for photo_id in remaining],
            )
    except TelegramError as exc:
        log.warning("Could not send extra examples: %s", type(exc).__name__)
    await query.message.reply_text(
        "🏎 Вот такие истории заходят 😄 А теперь покажу свои наблюдения.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔥 Давай первый миф", callback_data="next:0")
        ]]),
    )
    log.info("funnel_more_examples")


async def collect_gallery_item(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, file):
    """Keep Telegram photo or PNG/JPG document IDs until /galleryids prepares them."""
    items = context.chat_data.setdefault("gallery_items", [])
    unique_id = file.file_unique_id
    if any(item["unique_id"] == unique_id for item in items):
        return
    if len(items) >= MAX_EXAMPLE_PHOTOS:
        return
    items.append({"kind": kind, "file_id": file.file_id, "unique_id": unique_id})
    context.chat_data.pop("gallery_ready_ids", None)
    if len(items) == MAX_EXAMPLE_PHOTOS:
        await update.message.reply_text(
            "✅ Все 11 скриншотов на месте! Отправь /galleryids, соберу фото для галереи."
        )


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.photo:
        await collect_gallery_item(update, context, "photo", update.message.photo[-1])


async def prepare_gallery(message, context: ContextTypes.DEFAULT_TYPE):
    """Convert uploaded PNG/JPG documents to reusable Telegram photo IDs."""
    try:
        items = list(context.chat_data.get("gallery_items", []))
        ids = []
        for item in items:
            if item["kind"] == "photo":
                ids.append(item["file_id"])
                continue

            file = await appbot.bot.get_file(item["file_id"])
            image_bytes = await file.download_as_bytearray()
            converted = await appbot.bot.send_photo(
                chat_id=message.chat_id,
                photo=InputFile(io.BytesIO(image_bytes), filename="reels-example.png"),
                disable_notification=True,
            )
            ids.append(converted.photo[-1].file_id)
            try:
                await appbot.bot.delete_message(
                    chat_id=message.chat_id,
                    message_id=converted.message_id,
                )
            except TelegramError:
                pass
            await asyncio.sleep(1.05)

        if len(ids) != MAX_EXAMPLE_PHOTOS:
            raise ValueError("Gallery item count changed during preparation")
        context.chat_data["gallery_ready_ids"] = ids
        await message.reply_text(
            "✅ Все 11 фото готовы!\n\n"
            "В Render → Environment добавь:\n"
            "KEY: EXAMPLE_PHOTO_IDS\n"
            "VALUE:\n" + ",".join(ids) + "\n\n"
            "Сохрани настройки, дождись Live и проверь /status."
        )
    except Exception as exc:
        log.warning("Preparing gallery failed: %s", type(exc).__name__)
        await message.reply_text(
            "📸 С одним из изображений возникла проблема. "
            "Попробуй отправить скриншоты обычными фотографиями после /galleryclear."
        )
    finally:
        context.chat_data["gallery_preparing"] = False


async def galleryids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = context.chat_data.get("gallery_items", [])
    if len(items) < MAX_EXAMPLE_PHOTOS:
        await update.message.reply_text(
            f"📸 Пока получил {len(items)} из {MAX_EXAMPLE_PHOTOS} скриншотов. "
            "Можно присылать фото или PNG/JPG как файлы."
        )
        return
    ids = context.chat_data.get("gallery_ready_ids")
    if ids and len(ids) == MAX_EXAMPLE_PHOTOS:
        await update.message.reply_text(
            "✅ Готовая строка для Render → EXAMPLE_PHOTO_IDS:\n"
            + ",".join(ids)
        )
        return
    if context.chat_data.get("gallery_preparing"):
        await update.message.reply_text(
            "🔄 Уже готовлю фотографии. Через минуту пришлю готовую строку."
        )
        return
    context.chat_data["gallery_preparing"] = True
    await update.message.reply_text(
        "📸 Получил все 11. Сейчас подготовлю PNG-файлы для галереи. "
        "Это займёт около минуты, готовые ID пришлю сюда."
    )
    appbot.create_task(prepare_gallery(update.message, context))


async def galleryclear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["gallery_items"] = []
    context.chat_data["gallery_ready_ids"] = []
    await update.message.reply_text(
        "📸 Список очищен. Пришли 11 скриншотов как фото или как PNG/JPG-файлы. "
        "Когда всё отправишь, напиши /galleryids."
    )


async def send_lesson(source_message, index: int):
    """Send silent MP4/GIF with the complete lesson as its caption, or plain text."""
    caption = page(index)
    reply_markup = keyboard(index)
    gif_id = step_animation_ids.get(index + 1)
    global cached_first_step_animation_id
    if index == 0 and not gif_id:
        if cached_first_step_animation_id:
            gif_id = cached_first_step_animation_id
        else:
            async with first_step_animation_lock:
                if cached_first_step_animation_id:
                    gif_id = cached_first_step_animation_id
                else:
                    try:
                        original = await appbot.bot.get_file(FIRST_STEP_MP4_DOCUMENT_ID)
                        mp4_bytes = await original.download_as_bytearray()
                        mp4_data = io.BytesIO(mp4_bytes)
                        mp4_data.seek(0)
                        animation_message = await appbot.bot.send_animation(
                            chat_id=source_message.chat_id,
                            animation=InputFile(mp4_data, filename="ferixdi_step01.mp4"),
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                            read_timeout=180,
                            write_timeout=180,
                        )
                        if animation_message.animation:
                            cached_first_step_animation_id = animation_message.animation.file_id
                        log.info("lesson_animation_reuploaded step=01")
                        return
                    except (TelegramError, OSError, ValueError) as exc:
                        log.warning(
                            "lesson_animation_document_conversion_failed step=01: %s",
                            type(exc).__name__,
                        )
    if gif_id:
        try:
            await appbot.bot.send_animation(
                chat_id=source_message.chat_id,
                animation=gif_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
                read_timeout=180,
                write_timeout=180,
            )
            log.info("lesson_animation_sent step=%02d", index + 1)
            return
        except TelegramError as exc:
            log.warning("lesson_animation_error step=%02d: %s", index + 1, type(exc).__name__)

    await source_message.reply_text(
        caption,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
        parse_mode="HTML",
    )


async def stepgif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a reusable animation ID by replying /stepgif 1 to 10 to an uploaded clip."""
    usage = (
        "🎬 Пришли короткий MP4 без звука (или GIF) в этот чат. "
        "Затем ответь на него командой /stepgif 1 для первого разбора, "
        "/stepgif 2 для второго и так до 10."
    )
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(usage)
        return
    number = int(context.args[0])
    quoted = update.message.reply_to_message
    if number < 1 or number > len(STEPS) or quoted is None:
        await update.message.reply_text(usage)
        return

    animation = quoted.animation
    if animation:
        file_id = animation.file_id
    else:
        media = quoted.video or quoted.document
        if media is None or (
            quoted.document and not (
                (quoted.document.file_name or "").lower().endswith((".mp4", ".gif"))
                or quoted.document.mime_type in ("video/mp4", "image/gif")
            )
        ):
            await update.message.reply_text("🎬 Ответь на сообщение с MP4 или GIF.\n\n" + usage)
            return
        if media.file_size and media.file_size > 15_000_000:
            await update.message.reply_text("🎬 Файл больше 15 МБ. Сожми анимацию и пришли снова.")
            return

        try:
            telegram_file = await appbot.bot.get_file(media.file_id)
            original_bytes = await telegram_file.download_as_bytearray()
            source_bytes = io.BytesIO(original_bytes)
            source_bytes.seek(0)
            preview = await appbot.bot.send_animation(
                chat_id=update.message.chat_id,
                animation=InputFile(
                    source_bytes,
                    filename=(
                        f"ferixdi_step{number:02d}.gif"
                        if quoted.document and (quoted.document.file_name or "").lower().endswith(".gif")
                        else f"ferixdi_step{number:02d}.mp4"
                    ),
                ),
                caption=f"🎬 Проверка анимации для разбора {number:02d}/10",
                read_timeout=180,
                write_timeout=180,
            )
            if preview.animation is None:
                raise ValueError("Telegram did not accept this as an animation")
            file_id = preview.animation.file_id
        except (TelegramError, ValueError, OSError) as exc:
            log.warning("step_animation_upload_failed step=%02d: %s", number, type(exc).__name__)
            await update.message.reply_text(
                "🎬 Telegram пока отказывается принимать клип как GIF. "
                "Убери звуковую дорожку из MP4 и пришли ещё раз."
            )
            return

    key = f"STEP_ANIMATION_{number:02d}"
    await update.message.reply_text(
        f"✅ Анимация для разбора {number:02d}/10 готова!\n\n"
        "Добавь в Render → Environment:\n"
        f"KEY: {key}\nVALUE: {file_id}\n\n"
        "Сохрани и дождись Live. "
        "В боте анимация будет над полным текстом, с кнопкой следующего шага."
    )


async def next_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if not 0 <= index <= len(STEPS):
        return
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if 0 <= index < len(STEPS):
        await send_lesson(query.message, index)
    else:
        await query.message.reply_text(
            page(index),
            reply_markup=keyboard(index),
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
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
    name = (document.file_name or "").lower()
    mime = document.mime_type or ""
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")) or mime in (
        "image/png", "image/jpeg", "image/webp"
    ):
        await collect_gallery_item(update, context, "document", document)
        return
    if name.startswith("ferixdi_step") and name.endswith((".mp4", ".gif")):
        await update.message.reply_text(
            "🎬 Анимацию получил! Ответь на СВОЁ сообщение с этим файлом "
            "командой /stepgif 1 (или номером нужного разбора от 1 до 10). "
            "Сразу подготовлю ID для Render."
        )
        return
    if name.endswith(".mp4") or mime == "video/mp4":
        context.chat_data["last_mp4_file_id"] = document.file_id
        await update.message.reply_text(id_message(document.file_id))
        return
    await update.message.reply_text(
        "📸 Для подборки пришли PNG/JPG как файл или обычное фото. "
        "Для финального ролика подойдёт MP4."
    )


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


async def bot_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provide the actual t.me deep link without exposing the token or guessing a username."""
    info = await appbot.bot.get_me()
    await update.message.reply_text(
        f"🔗 Прямая ссылка на бота:\n"
        f"https://t.me/{info.username}?start=ferixdi\n\n"
        "При первом открытии человек нажмёт «Запустить», "
        "и бот сразу покажет приветствие."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = "Telegram file_id" if cached_video_file_id else "GitHub Releases (проверь, прикреплён ли MP4)"
    await update.message.reply_text(
        "🛠 Ferixdi Bot v4.2\n"
        "16 мифов в 10 разборах. Кнопки активны.\n"
        f"Видео: источник {source}.\n"
        f"Примеры: {len(example_photo_ids)} фото" + (" (по лайкам).\n" if photo_gallery_sorted else ".\n")
        + f"GIF к разборам: {len(step_animation_ids) + int(1 not in step_animation_ids and bool(cached_first_step_animation_id))}/{len(STEPS)}.\\n"
        + ("Первый разбор: MP4 подключён, GIF создаётся при первом показе."
           if 1 not in step_animation_ids and not cached_first_step_animation_id
           else "Первый разбор: GIF готов." if 1 in step_animation_ids or cached_first_step_animation_id
           else "Первый разбор: GIF отсутствует.")
    )


async def deliver_original_video(source_message):
    """Send the unchanged MP4 document, keeping the webhook response fast."""
    global cached_video_file_id
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Глянуть кейсы на сайте", url=CASES_URL)],
        [InlineKeyboardButton("🧩 А где узнать конкретные шаги?", callback_data="video_finished")],
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
            "🎬 Исходный MP4 сейчас отправить не получилось. Напиши @ferixdiii, проверю файл ❤️"
        )


async def final_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🎬 Сейчас скину исходник...")
    appbot.create_task(deliver_original_video(query.message))


async def video_finished(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Keep the link to cases on the original MP4, but move the menu to a text message.
    try:
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Глянуть кейсы на сайте", url=CASES_URL)
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
        "doubts": DOUBTS_TEXT,
        "join": JOIN_TEXT,
        "student": STUDENT_TEXT,
        "budget": BUDGET_TEXT,
        "easy": EASY_TEXT,
        "skilled": SKILLED_TEXT,
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
appbot.add_handler(CommandHandler("stepgif", stepgif))
appbot.add_handler(CommandHandler("fileid", fileid))
appbot.add_handler(CommandHandler("status", status))
appbot.add_handler(CommandHandler("link", bot_link))
appbot.add_handler(CommandHandler("galleryids", galleryids))
appbot.add_handler(CommandHandler("galleryclear", galleryclear))
appbot.add_handler(MessageHandler(filters.PHOTO, receive_photo))
appbot.add_handler(MessageHandler(filters.Document.ALL, receive_document))
appbot.add_handler(CallbackQueryHandler(next_step, pattern=r"^next:\d+$"))
appbot.add_handler(CallbackQueryHandler(show_examples, pattern=r"^show_examples$"))
appbot.add_handler(CallbackQueryHandler(more_examples, pattern=r"^more_examples$"))
appbot.add_handler(CallbackQueryHandler(final_video, pattern=r"^final_video$"))
appbot.add_handler(CallbackQueryHandler(video_finished, pattern=r"^video_finished$"))
appbot.add_handler(CallbackQueryHandler(final_route, pattern=r"^route:(menu|doubts|join|student|budget|market|zero|easy|skilled|later)$"))

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

@app.get("/start")
async def public_start_link():
    """A stable public link that redirects to this bot's actual Telegram username."""
    bot_info = await appbot.bot.get_me()
    if not bot_info.username:
        raise HTTPException(status_code=503, detail="Bot username unavailable")
    return RedirectResponse(
        url=f"https://t.me/{bot_info.username}?start=ferixdi",
        status_code=302,
    )

@app.post(PATH)
async def telegram_webhook(request: Request):
    sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(sent, WEBHOOK_SECRET):
        raise HTTPException(status_code=403)
    payload = await request.json()
    await appbot.process_update(Update.de_json(payload, appbot.bot))
    return {"ok": True}
