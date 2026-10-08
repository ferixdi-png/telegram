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

# Original 11 screenshot files were supplied in this particular sequence.
# A Telegram re-upload can change file_id fingerprints while retaining this order.
# Only use this positional fallback when all recognized IDs are consistent
# with the original upload sequence and no explicit counts are supplied.
ORIGINAL_GALLERY_UPLOAD_LIKES = (
    136000, 24600, 46400, 71800, 28700, 53400,
    18100, 79000, 109000, 23300, 36600,
)


def photo_tag(file_id: str) -> str:
    value = 2166136261
    for char in file_id:
        value = ((value ^ ord(char)) * 16777619) & 0xffffffff
    return f"{value:08x}"


def order_gallery_photos(ids: list[str]) -> tuple[list[str], bool]:
    """Order recognized screenshots by verified likes, without an all-or-nothing match."""
    if not ids:
        return ids, False

    # Original gallery mappings always win over external/manual values:
    # EXAMPLE_PHOTO_LIKES can be stale or attached to the wrong photo order.
    known_counts = [KNOWN_GALLERY_LIKES.get(photo_tag(file_id)) for file_id in ids]
    manual_counts = None
    manual = os.getenv("EXAMPLE_PHOTO_LIKES", "").strip()
    if manual:
        try:
            values = [int(s.strip().replace(" ", "")) for s in manual.split(",")]
            if len(values) == len(ids) and all(value >= 0 for value in values):
                manual_counts = values
            else:
                log.warning("Gallery likes override has incorrect count/negative values")
        except ValueError:
            log.warning("Gallery likes override has invalid values")

    counts = [
        known if known is not None else (
            manual_counts[index] if manual_counts is not None else None
        )
        for index, known in enumerate(known_counts)
    ]
    recognized = sum(value is not None for value in counts)

    same_original_sequence = (
        len(ids) == len(ORIGINAL_GALLERY_UPLOAD_LIKES)
        and all(
            known is None or known == ORIGINAL_GALLERY_UPLOAD_LIKES[index]
            for index, known in enumerate(known_counts)
        )
    )
    if recognized < len(ids) and same_original_sequence and manual_counts is None:
        # Telegram replaced one or more file IDs. Reuse the original upload's
        # known like values only while its observed sequence is still consistent.
        counts = [
            known if known is not None else ORIGINAL_GALLERY_UPLOAD_LIKES[index]
            for index, known in enumerate(known_counts)
        ]
        recognized = len(ids)
        log.info("Gallery sorted using original upload order as fallback")

    if not recognized:
        log.warning("Gallery ranking unavailable: no matching screenshot IDs or valid counts")
        return ids, False

    # If one file ID has changed, continue sorting every recognized image.
    # Unknowns go last in their original relative order; never invent a like count.
    indexed = list(enumerate(ids))
    indexed.sort(
        key=lambda pair: (
            counts[pair[0]] is not None,
            counts[pair[0]] if counts[pair[0]] is not None else -1,
        ),
        reverse=True,
    )
    if recognized < len(ids):
        log.warning(
            "Gallery partially ranked: %s of %s images recognized",
            recognized, len(ids),
        )
    else:
        log.info("Gallery ranked: all %s images have verified/manual counts", recognized)
    return [file_id for _, file_id in indexed], recognized == len(ids)


# Each Reels lesson can have a silent looping MP4/GIF shown above the full text.
# Set STEP_ANIMATION_01 ... STEP_ANIMATION_10 in Render Environment.
# Missing IDs automatically fall back to a regular text message.
step_animation_ids = {
    index: file_id
    for index in range(1, 11)
    if (file_id := os.getenv(f"STEP_ANIMATION_{index:02d}", "").strip())
}

# These files were sent to this bot as MP4 documents, not Telegram GIFs.
# On first use, resend them as animations, cache the animation IDs in memory,
# and keep the full lesson text as a caption with the existing navigation button.
# STEP_ANIMATION_01..10 (actual animation IDs) override these document sources.
STEP_MP4_DOCUMENT_IDS = {
    1: "BQACAgIAAxkBAAO7asdXn8721DAThL8eSkEjnHtpOpMAAv6iAAJF4DhKVHOz1QP60cI9BA",
    2: "BQACAgIAAxkBAAPXasdcAzlMa2n9cdZr6RZTzNxp_YgAAl2jAAJF4DhKXCGLrE7BuMk9BA",
    3: "BQACAgIAAxkBAAPZasdcXTV7iaNB4Ezs1UpjOY1qUuIAAmKjAAJF4DhKVPHVZTAw-Qw9BA",
    4: "BQACAgIAAxkBAAPpasdew5s5pwPzbmu3COOReZe6EVkAApujAAJF4DhK0qibIBsDpWk9BA",
    5: "BQACAgIAAxkBAAPrasdfCAbUpZhU2OAgzfko29cn6okAAp6jAAJF4DhKLKnGanWDTpA9BA",
    6: "BQACAgIAAxkBAAPtasdfnA3QWvdQlVffE3ZhEM12k44AAqajAAJF4DhKcB8z5ynlE5M9BA",
    7: "BQACAgIAAxkBAAPvasdgIQwo0omOxuvXTGk6F0EgBcIAAqijAAJF4DhKoDOo14uiGa49BA",
    8: "BQACAgIAAxkBAAPxasdguK8KFAiPz7G1odcwsYAwfDkAAq-jAAJF4DhK0lrLjFuxvTs9BA",
    9: "BQACAgIAAxkBAAPzasdhOiu1Z-h5AAEC7P5ZyHTSn2YUAAK1owACReA4SozzZUCtJxRkPQQ",
    10: "BQACAgIAAxkBAAP1asdhull9p_Nt9ueclc3IEAMG5kkAArujAAJF4DhKajE4ZQhotL49BA",
}
cached_step_animation_ids = {}
step_animation_locks = {number: asyncio.Lock() for number in STEP_MP4_DOCUMENT_IDS}
# Prefetch at most two upcoming MP4s while a reader is on the previous screen.
# This reduces cold-first-view waits without keeping the entire video set in RAM.
prefetched_step_tasks = {}

# Telegram document sent specifically for the welcome screen, not the final MP4.
INTRO_MP4_DOCUMENT_ID = (
    "BQACAgIAAxkBAAPIasdbCuZfneAEba80-kDPgCq06ZMAAkWjAAJF4DhKIg5vCsk91p49BA"
)
INTRO_ANIMATION_ID = os.getenv("INTRO_ANIMATION_ID", "").strip()
cached_intro_animation_id = None
intro_animation_lock = asyncio.Lock()

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
            f'🏁 <b>МИФ {index + 1:02d}/{len(STEPS)}</b>\n\n'
            f'<b>{escape(step["myth"])}</b>\n\n'
            f'{escape(step["answer"])}'
        )
    return CASES


async def send_intro(source_message):
    """Show the introduction with an animation and the full HTML text as caption."""
    global cached_intro_animation_id
    caption = INTRO
    reply_markup = keyboard(-1)
    gif_id = INTRO_ANIMATION_ID or cached_intro_animation_id

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
            log.info("intro_animation_sent")
            return
        except TelegramError as exc:
            log.warning("intro_animation_id_failed: %s", type(exc).__name__)

    if not INTRO_ANIMATION_ID:
        async with intro_animation_lock:
            if cached_intro_animation_id:
                try:
                    await appbot.bot.send_animation(
                        chat_id=source_message.chat_id,
                        animation=cached_intro_animation_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                        read_timeout=180,
                        write_timeout=180,
                    )
                    return
                except TelegramError as exc:
                    log.warning("intro_cached_animation_failed: %s", type(exc).__name__)
            try:
                original = await appbot.bot.get_file(INTRO_MP4_DOCUMENT_ID)
                content = await original.download_as_bytearray()
                data = io.BytesIO(content)
                data.seek(0)
                sent = await appbot.bot.send_animation(
                    chat_id=source_message.chat_id,
                    animation=InputFile(data, filename="ferixdi_intro.mp4"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    read_timeout=180,
                    write_timeout=180,
                )
                if sent.animation:
                    cached_intro_animation_id = sent.animation.file_id
                log.info("intro_animation_converted")
                return
            except (TelegramError, OSError, ValueError) as exc:
                log.warning("intro_animation_conversion_failed: %s", type(exc).__name__)

    await source_message.reply_text(
        caption,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await send_intro(update.message)

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
    """Show a looping MP4/GIF, complete lesson caption, and the existing next button."""
    number = index + 1
    caption = page(index)
    reply_markup = keyboard(index)
    gif_id = step_animation_ids.get(number) or cached_step_animation_ids.get(number)

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
            log.info("lesson_animation_sent step=%02d", number)
            return
        except TelegramError as exc:
            log.warning("lesson_animation_id_failed step=%02d: %s", number, type(exc).__name__)

    document_id = STEP_MP4_DOCUMENT_IDS.get(number)
    if document_id:
        async with step_animation_locks[number]:
            cached_id = cached_step_animation_ids.get(number)
            if cached_id and cached_id != gif_id:
                try:
                    await appbot.bot.send_animation(
                        chat_id=source_message.chat_id,
                        animation=cached_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                        read_timeout=180,
                        write_timeout=180,
                    )
                    return
                except TelegramError as exc:
                    log.warning(
                        "lesson_cached_animation_failed step=%02d: %s",
                        number, type(exc).__name__,
                    )

            if not cached_id or cached_id != gif_id:
                try:
                    source_file = await appbot.bot.get_file(document_id)
                    mp4_bytes = await source_file.download_as_bytearray()
                    mp4_data = io.BytesIO(mp4_bytes)
                    mp4_data.seek(0)
                    sent = await appbot.bot.send_animation(
                        chat_id=source_message.chat_id,
                        animation=InputFile(
                            mp4_data, filename=f"ferixdi_step{number:02d}.mp4"
                        ),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                        read_timeout=180,
                        write_timeout=180,
                    )
                    if sent.animation:
                        cached_step_animation_ids[number] = sent.animation.file_id
                    log.info("lesson_animation_converted step=%02d", number)
                    return
                except (TelegramError, OSError, ValueError) as exc:
                    log.warning(
                        "lesson_animation_conversion_failed step=%02d: %s",
                        number, type(exc).__name__,
                    )

    await source_message.reply_text(
        caption,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
        parse_mode="HTML",
    )


def stepgif_caption_number(message):
    """Recognize /stepgif N typed as a Telegram media caption."""
    tokens = (message.caption or "").strip().split()
    if len(tokens) == 2 and tokens[0].split("@", 1)[0].lower() == "/stepgif" and tokens[1].isdigit():
        return int(tokens[1])
    return None


async def prepare_stepgif(message, number: int, media_message):
    """Return a Telegram animation file ID from GIF, video or MP4 document."""
    help_text = (
        "🎬 Отправь видео, GIF или MP4-файл с подписью /stepgif 5 "
        "(где 5 — нужный номер от 1 до 10). "
        "Можно также ответить текстом /stepgif 5 на уже отправленный файл."
    )
    if not 1 <= number <= len(STEPS) or media_message is None:
        await message.reply_text(help_text)
        return

    animation = media_message.animation
    if animation:
        file_id = animation.file_id
    else:
        media = media_message.video or media_message.document
        if media is None or (
            media_message.document and not (
                (media_message.document.file_name or "").lower().endswith((".mp4", ".gif"))
                or media_message.document.mime_type in ("video/mp4", "image/gif")
            )
        ):
            await message.reply_text("🎬 В сообщении нет подходящего MP4 или GIF.\n\n" + help_text)
            return
        if media.file_size and media.file_size > 15_000_000:
            await message.reply_text("🎬 Файл больше 15 МБ. Сожми анимацию и отправь снова.")
            return

        try:
            telegram_file = await appbot.bot.get_file(media.file_id)
            original_bytes = await telegram_file.download_as_bytearray()
            source_bytes = io.BytesIO(original_bytes)
            source_bytes.seek(0)
            extension = (
                ".gif"
                if media_message.document and (media_message.document.file_name or "").lower().endswith(".gif")
                else ".mp4"
            )
            preview = await appbot.bot.send_animation(
                chat_id=message.chat_id,
                animation=InputFile(source_bytes, filename=f"ferixdi_step{number:02d}{extension}"),
                caption=f"🎬 Анимация для разбора {number:02d}/10",
                read_timeout=180,
                write_timeout=180,
            )
            if preview.animation is None:
                raise ValueError("Telegram returned no animation")
            file_id = preview.animation.file_id
        except (TelegramError, ValueError, OSError) as exc:
            log.warning("step_animation_upload_failed step=%02d: %s", number, type(exc).__name__)
            await message.reply_text(
                "🎬 Telegram пока не смог обработать MP4 как GIF. "
                "Убери звуковую дорожку, сожми до 15 МБ и повтори."
            )
            return

    await message.reply_text(
        f"✅ GIF для разбора {number:02d}/10 готова!\n\n"
        f"KEY: STEP_ANIMATION_{number:02d}\n"
        f"VALUE: {file_id}\n\n"
        "Пришли этот ответ мне в ChatGPT, и я добавлю GIF в GitHub "
        "без ручной настройки Render. "
        "Сейчас выдан ID; GIF появится в воронке после подключения и деплоя."
    )


async def stepgif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Support a separate /stepgif N reply to a previously uploaded video."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "🎬 Отправь MP4 или GIF с подписью /stepgif 5, "
            "либо ответь на сообщение с видео командой /stepgif 5. "
            "Номер разбора от 1 до 10."
        )
        return
    await prepare_stepgif(
        update.message,
        int(context.args[0]),
        update.message.reply_to_message,
    )


async def receive_video_or_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept /stepgif N directly under the uploaded video/Telegram animation."""
    message = update.message
    number = stepgif_caption_number(message)
    if number is not None:
        await prepare_stepgif(message, number, message)
    else:
        await message.reply_text(
            "🎬 Видео получил! Для привязки GIF отправь его с подписью "
            "/stepgif 5 или ответь на это видео отдельной командой /stepgif 5."
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
        "✅ MP4 получил! Вот Telegram ID файла:\n\n"
        f"{file_id}\n\n"
        "Если это GIF для приветствия или одного из 10 разборов, "
        "пришли мне ID с названием блока. "
        "Для финального исходного видео используй Render → Environment "
        "с ключом VIDEO_FILE_ID. Не подменяй финальный файл заставкой."
    )


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    number = stepgif_caption_number(update.message)
    if number is not None:
        await prepare_stepgif(update.message, number, update.message)
        return
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
        "🛠 Ferixdi Bot v4.14\n"
        "16 мифов в 10 разборах. Кнопки активны.\n"
        f"Видео: источник {source}.\n"
        f"Примеры: {len(example_photo_ids)} фото.\n"
        + f"Знакомые скрины: {sum(photo_tag(fid) in KNOWN_GALLERY_LIKES for fid in example_photo_ids)}/{len(example_photo_ids)}.\n"
        + f"GIF к разборам: {len(set(step_animation_ids) | set(cached_step_animation_ids))}/{len(STEPS)}.\n"
        + "MP4 для GIF: "
        + ", ".join(f"{n:02d}/10" for n in sorted(STEP_MP4_DOCUMENT_IDS))
        + " (автоподключение при первом показе)."
        + "\n"
        + ("Интро: GIF готов." if INTRO_ANIMATION_ID or cached_intro_animation_id
           else "Интро: MP4 подключён, GIF создаётся при первом /start.")
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
appbot.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION, receive_video_or_animation))
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
