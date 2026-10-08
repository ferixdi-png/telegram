import os
import hmac
import logging
import asyncio
import io
from pathlib import Path
from html import escape
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputFile, MessageEntity
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, BadRequest
from content import (
    STEPS, INTRO, SHOWCASE, CASES, CASES_URL, VIDEO_CAPTION,
    FINAL_MENU, DOUBTS_TEXT, JOIN_TEXT, STUDENT_TEXT, BUDGET_TEXT, MARKET_TEXT, ALGORITHM_TEXT, ZERO_TEXT, EASY_TEXT, SKILLED_TEXT, LATER_TEXT, CONTACT_URL,
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
# Process several Telegram updates simultaneously, instead of making every
# visitor wait while another chat uploads a first-time GIF.
# Official PTB update_queue handles dispatch and clean shutdown.
appbot = (
    Application.builder()
    .token(TOKEN)
    .updater(None)
    .concurrent_updates(8)
    .build()
)
cached_video_file_id = os.getenv("VIDEO_FILE_ID", "").strip()
MAX_EXAMPLE_PHOTOS = 11
# Exact original JPG attached by the author and committed to GitHub assets.
# If present, ALGORITHM_PHOTO_FILE_ID env is an optional Telegram-side override.
# Telegram photo ID supplied by the author via /algimage. No Render setup needed.
DEFAULT_ALGORITHM_PHOTO_FILE_ID = "AgACAgIAAxkBAAIBI2rHgX1h1MUnUXHy6x-xm0efe3kiAAKBGWsb0ek4SmS2X-tbmmN1AQADAgADeQADPQQ"
ALGORITHM_PHOTO_FILE_ID = (os.getenv("ALGORITHM_PHOTO_FILE_ID") or DEFAULT_ALGORITHM_PHOTO_FILE_ID).strip()
ALGORITHM_IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "instagram_algorithm_meme.jpg"
cached_algorithm_photo_id = None


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

FIRST_MYTH_BUTTON = "🔥 РАЗОБЛАЧИТЬ ПЕРВЫЙ МИФ →"


def keyboard(index: int):
    if index == -2:
        buttons = [[InlineKeyboardButton(FIRST_MYTH_BUTTON, callback_data="next:0")]]
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
            InlineKeyboardButton(FIRST_MYTH_BUTTON, callback_data="next:0")
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
            [InlineKeyboardButton("🧠 Почему Instagram требует большего?", callback_data="route:algorithm")],
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

    if route == "algorithm":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Посмотреть открытые кейсы", url=CASES_URL)],
            [InlineKeyboardButton("↩️ К вопросам", callback_data="route:doubts")],
        ])

    if route == "skilled":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🤝 Давай про трафик", url=CONTACT_URL)],
            [InlineKeyboardButton("↩️ Назад", callback_data="route:menu")],
        ])

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Назад", callback_data="route:menu")],
    ])


# Hand-drawn Telegram custom emoji letters from the owner's "Карандашик HD" pack.
# All ten lessons share this label, both in GIF captions and the text-only fallback.
CUSTOM_MYTH_EMOJI_IDS = (
    "5264807616227357702",  # М
    "5264908337505412642",  # И
    "5264998149566539809",  # Ф
)
custom_myth_font_supported = True


def myth_label(index: int, custom_font: bool = True) -> str:
    if custom_font:
        letters = "".join(
            f'<tg-emoji emoji-id="{emoji_id}">✏️</tg-emoji>'
            for emoji_id in CUSTOM_MYTH_EMOJI_IDS
        )
    else:
        letters = "<b>МИФ</b>"
    return f'{letters} <b>{index + 1}</b>'


def page(index: int, custom_font: bool | None = None):
    if index == -1:
        return INTRO
    if 0 <= index < len(STEPS):
        if custom_font is None:
            custom_font = custom_myth_font_supported
        step = STEPS[index]
        return (
            f'{myth_label(index, custom_font)}\n\n'
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
    """Acknowledge /start immediately; keep first-use MP4 conversion nonblocking."""
    if not update.message:
        return

    progress = None
    # On each Render restart the intro GIF cache is cold. Telegram can need
    # significant time to download/re-upload the MP4 before the first send.
    if not (INTRO_ANIMATION_ID or cached_intro_animation_id):
        try:
            progress = await update.message.reply_text("⏳ Загружаю приветствие, пару секунд...")
        except TelegramError as exc:
            log.warning("Start acknowledgment failed: %s", type(exc).__name__)

    try:
        await send_intro(update.message)
    except Exception:
        log.exception("Unexpected start handler failure")
        try:
            await update.message.reply_text(
                INTRO,
                reply_markup=keyboard(-1),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            log.error("Could not deliver start fallback: %s", type(exc).__name__)
    finally:
        if progress:
            try:
                await progress.delete()
            except TelegramError:
                pass
        prefetch_upcoming_step(1)

async def show_examples(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    appbot.create_task(clear_used_button(query))

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
    appbot.create_task(clear_used_button(query))
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
        "🏎 Вот такие истории заходят 🔥 А теперь покажу свои наблюдения.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(FIRST_MYTH_BUTTON, callback_data="next:0")
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


async def send_algorithm_image_id(message, photo_message):
    """Return the exact Telegram photo ID so the algorithm article survives deploys."""
    if photo_message is None or not photo_message.photo:
        await message.reply_text(
            "📸 Отправь картинку как обычное фото с подписью /algimage "
            "или ответь на неё отдельной командой /algimage."
        )
        return
    file_id = photo_message.photo[-1].file_id
    await message.reply_text(
        "✅ Картинка для блока об алгоритмах получена!\n\n"
        "KEY: ALGORITHM_PHOTO_FILE_ID\n"
        f"VALUE: {file_id}\n\n"
        "Перешли мне этот ответ в ChatGPT, подключу изображение "
        "в GitHub без изменений в Render. Видео и GIF останутся прежними."
    )


async def algimage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message:
        await send_algorithm_image_id(message, message.reply_to_message)


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.photo:
        if (update.message.caption or "").strip().split("@", 1)[0].lower() == "/algimage":
            await send_algorithm_image_id(update.message, update.message)
            return
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


async def load_step_mp4(number: int):
    """Fetch the next source file while the user reads the current screen."""
    try:
        source = await appbot.bot.get_file(STEP_MP4_DOCUMENT_IDS[number])
        return await source.download_as_bytearray()
    except (TelegramError, OSError, ValueError) as exc:
        log.warning("prefetch_step_failed step=%02d: %s", number, type(exc).__name__)
        return None


def prefetch_upcoming_step(number: int):
    """Bound memory to two prefetched MP4s; skip files with ready animation IDs."""
    if (
        number not in STEP_MP4_DOCUMENT_IDS
        or number in step_animation_ids
        or number in cached_step_animation_ids
        or number in prefetched_step_tasks
    ):
        return
    # A finished download is the whole point of prefetching: retain its bytes
    # until get_lesson_mp4() consumes them. Previous code deleted finished
    # tasks before use and forced a second download.
    for previous, task in list(prefetched_step_tasks.items()):
        if task.done() and (
            task.cancelled()
            or task.exception() is not None
            or task.result() is None
        ):
            prefetched_step_tasks.pop(previous, None)

    if len(prefetched_step_tasks) >= 2:
        # The cap still bounds memory. Evict the oldest completed unused
        # prefetch for a newer lesson; never cancel an in-flight download.
        for previous, task in list(prefetched_step_tasks.items()):
            if task.done():
                prefetched_step_tasks.pop(previous, None)
                break
    if len(prefetched_step_tasks) >= 2:
        return
    prefetched_step_tasks[number] = appbot.create_task(load_step_mp4(number))


async def get_lesson_mp4(number: int):
    """Reuse a prefetched download or fall back to the normal Telegram request."""
    task = prefetched_step_tasks.pop(number, None)
    if task is not None:
        result = await task
        if result is not None:
            return result
    return await load_step_mp4(number)


def is_custom_font_error(exc: BadRequest) -> bool:
    """Only fall back for unsupported Telegram custom-emoji entities."""
    details = str(exc).lower()
    return any(term in details for term in (
        "customemoji", "custom emoji", "custom_emoji", "tg-emoji",
        "can't parse entities", "parse entities",
    ))


async def send_myth_animation(source_message, index: int, animation):
    """Prefer the custom-letter heading without risking the GIF if denied."""
    global custom_myth_font_supported
    kwargs = dict(
        chat_id=source_message.chat_id,
        animation=animation,
        caption=page(index),
        parse_mode="HTML",
        reply_markup=keyboard(index),
        read_timeout=180,
        write_timeout=180,
    )
    try:
        return await appbot.bot.send_animation(**kwargs)
    except BadRequest as exc:
        if not custom_myth_font_supported or not is_custom_font_error(exc):
            raise
        custom_myth_font_supported = False
        log.warning("Custom MYTH font unavailable; preserving original GIF with normal header")
        kwargs["caption"] = page(index, custom_font=False)
        return await appbot.bot.send_animation(**kwargs)


async def send_lesson(source_message, index: int):
    """Show a looping MP4/GIF, complete lesson caption, and the existing next button."""
    global custom_myth_font_supported
    number = index + 1
    caption = page(index)
    reply_markup = keyboard(index)
    gif_id = step_animation_ids.get(number) or cached_step_animation_ids.get(number)

    if gif_id:
        try:
            await send_myth_animation(source_message, index, gif_id)
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
                    await send_myth_animation(source_message, index, cached_id)
                    return
                except TelegramError as exc:
                    log.warning(
                        "lesson_cached_animation_failed step=%02d: %s",
                        number, type(exc).__name__,
                    )

            if not cached_id or cached_id != gif_id:
                try:
                    mp4_bytes = await get_lesson_mp4(number)
                    if mp4_bytes is None:
                        raise ValueError("MP4 source unavailable")
                    mp4_data = io.BytesIO(mp4_bytes)
                    mp4_data.seek(0)
                    sent = await send_myth_animation(
                        source_message,
                        index,
                        InputFile(mp4_data, filename=f"ferixdi_step{number:02d}.mp4"),
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

    try:
        await source_message.reply_text(
            page(index),
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
    except BadRequest as exc:
        if not custom_myth_font_supported or not is_custom_font_error(exc):
            raise
        custom_myth_font_supported = False
        log.warning("Custom MYTH font unavailable for plain text; sending normal title")
        await source_message.reply_text(
            page(index, custom_font=False),
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


async def clear_used_button(query):
    """Remove the previous navigation button without blocking the new lesson."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


async def next_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    if not 0 <= index <= len(STEPS):
        return
    if 0 <= index < len(STEPS):
        await asyncio.gather(
            clear_used_button(query),
            send_lesson(query.message, index),
        )
        prefetch_upcoming_step(index + 2)
    else:
        await asyncio.gather(
            clear_used_button(query),
            query.message.reply_text(
                page(index),
                reply_markup=keyboard(index),
                disable_web_page_preview=True,
                parse_mode="HTML",
            ),
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
    if (update.message.caption or "").strip().split("@", 1)[0].lower() == "/algimage":
        await update.message.reply_text(
            "📸 Пришли изображение для блока об алгоритмах как обычное фото, "
            "а не как документ. Добавь подпись /algimage."
        )
        return
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


async def emoji_font(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read reusable custom letter emoji IDs from a replied-to Telegram message."""
    message = update.message
    if message is None:
        return

    source = message.reply_to_message
    if source is None:
        await message.reply_text(
            "✏️ Ответь командой /emojifont МИФ на уже отправленное сообщение "
            "с тремя кастомными буквами М И Ф. Тогда покажу их Telegram ID."
        )
        return

    entities = source.entities or source.caption_entities or []
    letters = [
        entity.custom_emoji_id
        for entity in entities
        if entity.type == MessageEntity.CUSTOM_EMOJI and entity.custom_emoji_id
    ]
    if not letters:
        await message.reply_text(
            "🔎 В выбранном сообщении Telegram не передал кастомных эмодзи. "
            "Пришли буквы именно как эмодзи из набора, а затем ответь "
            "на них командой /emojifont МИФ. Скриншот для этого не подойдёт."
        )
        return
    if len(letters) > 45:
        await message.reply_text("✏️ За один раз можно прочитать до 45 букв.")
        return

    label = "".join(context.args).replace(" ", "").upper()
    if not label and len(letters) == 3:
        label = "МИФ"
    if not label or len(label) != len(letters) or not label.isalpha():
        await message.reply_text(
            f"✏️ Нашёл {len(letters)} кастомных букв. "
            "Ответь командой /emojifont МИФ, где слово после команды "
            "перечисляет буквы в том же порядке, что и на картинке."
        )
        return

    unique_ids = list(dict.fromkeys(letters))
    descriptions = [f"{letter} = {emoji_id}" for letter, emoji_id in zip(label, letters)]
    await message.reply_text(
        f"✅ Кастомный шрифт: {label}\n"
        f"Найдено букв: {len(letters)}\n\n"
        + "\n".join(descriptions)
        + "\n\nСкопируй этот ответ в ChatGPT. "
        "По ID подключим шрифт к заголовкам бота, GIF останутся прежними."
    )

    # The fallback inside <tg-emoji> must match the associated sticker emoji.
    # A custom font uses images of letters, but its underlying text is still an emoji.
    try:
        stickers = await context.bot.get_custom_emoji_stickers(unique_ids)
        alt_by_id = {
            sticker.custom_emoji_id: (sticker.emoji or "✏️")
            for sticker in stickers
            if sticker.custom_emoji_id
        }
        preview = " ".join(
            f'<tg-emoji emoji-id="{emoji_id}">{escape(alt_by_id.get(emoji_id, "✏️"))}</tg-emoji>'
            for emoji_id in letters
        )
        sent = await message.reply_text(
            "🔎 Вот как бот отправляет этот шрифт: \n" + preview,
            parse_mode="HTML",
        )
        if not any(
            entity.type == MessageEntity.CUSTOM_EMOJI
            for entity in (sent.entities or [])
        ):
            await message.reply_text(
                "⚠️ Telegram отправил обычные символы вместо кастомных эмодзи. "
                "Для отображения в личных чатах у владельца бота обычно "
                "нужна активная подписка Telegram Premium."
            )
    except TelegramError as exc:
        log.warning("custom_emoji_preview_failed: %s", type(exc).__name__)
        await message.reply_text(
            "⚠️ ID получил, но Telegram пока отклонил тестовую отправку. "
            "Проверь Telegram Premium на аккаунте владельца бота. "
            "Сами ID из сообщения выше сохранились."
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
        "🛠 Ferixdi Bot v4.25\n"
        "16 мифов в 10 разборах. Кнопки активны.\n"
        f"Видео: источник {source}.\n"
        f"Примеры: {len(example_photo_ids)} фото.\n"
        + f"Знакомые скрины: {sum(photo_tag(fid) in KNOWN_GALLERY_LIKES for fid in example_photo_ids)}/{len(example_photo_ids)}.\n"
        + f"Источники анимаций: {len(set(STEP_MP4_DOCUMENT_IDS) | set(step_animation_ids))}/{len(STEPS)}.\n"
        + f"Быстрый GIF-кэш: {len(set(step_animation_ids) | set(cached_step_animation_ids))}/{len(STEPS)}.\n"
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


async def keep_cases_link_on_video(query):
    """Update the old MP4's buttons without delaying the new menu."""
    try:
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Глянуть кейсы на сайте", url=CASES_URL)
            ]])
        )
    except TelegramError:
        pass


async def video_finished(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Keep the cases link on the MP4 while sending the menu immediately.
    appbot.create_task(keep_cases_link_on_video(query))

    await query.message.reply_text(
        FINAL_MENU,
        reply_markup=route_keyboard("menu"),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    log.info("funnel_final_menu_opened")


async def send_algorithm_article(query):
    """Send the author's photo by Telegram ID, fall back to the original JPG."""
    global cached_algorithm_photo_id
    buttons = route_keyboard("algorithm")
    photo_id = cached_algorithm_photo_id or ALGORITHM_PHOTO_FILE_ID

    if photo_id:
        try:
            sent = await appbot.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo_id,
                caption=ALGORITHM_TEXT,
                parse_mode="HTML",
                reply_markup=buttons,
                read_timeout=120,
                write_timeout=120,
            )
            if sent.photo:
                cached_algorithm_photo_id = sent.photo[-1].file_id
            log.info("funnel_algorithm_photo_sent")
            return
        except TelegramError as exc:
            log.warning("Algorithm Q&A Telegram photo ID failed: %s", type(exc).__name__)

    # Preserve a real image even if Telegram expires or rejects the supplied ID.
    try:
        with ALGORITHM_IMAGE_PATH.open("rb") as original:
            sent = await appbot.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=original,
                caption=ALGORITHM_TEXT,
                parse_mode="HTML",
                reply_markup=buttons,
                read_timeout=120,
                write_timeout=120,
            )
        if sent.photo:
            cached_algorithm_photo_id = sent.photo[-1].file_id
        log.info("funnel_algorithm_photo_uploaded_from_repo")
        return
    except (TelegramError, OSError) as exc:
        log.warning("Algorithm Q&A original JPG failed: %s", type(exc).__name__)

    await query.message.reply_text(
        ALGORITHM_TEXT,
        reply_markup=buttons,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    log.info("funnel_algorithm_text_fallback")

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
    if route == "algorithm":
        await send_algorithm_article(query)
        return

    if route not in copy:
        return
    # A photo caption cannot be edited into a text-only question menu.
    if query.message.photo or query.message.animation or query.message.document:
        await query.message.reply_text(
            copy[route],
            reply_markup=route_keyboard(route),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await query.edit_message_text(
            copy[route],
            reply_markup=route_keyboard(route),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    log.info("funnel_final_route=%s", route)


appbot.add_handler(CommandHandler("start", start, block=False))
appbot.add_handler(CommandHandler("stepgif", stepgif))
appbot.add_handler(CommandHandler("fileid", fileid))
appbot.add_handler(CommandHandler("algimage", algimage))
appbot.add_handler(CommandHandler("emojifont", emoji_font))
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
appbot.add_handler(CallbackQueryHandler(final_route, pattern=r"^route:(menu|doubts|join|student|budget|market|algorithm|zero|easy|skilled|later)$"))

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
    update = Update.de_json(payload, appbot.bot)
    if update is None:
        raise HTTPException(status_code=400, detail="Invalid Telegram update")
    # Do not hold Telegram's webhook open for slow media API requests. Telegram
    # expects an immediate HTTP acknowledgment; PTB's running worker consumes
    # queued updates with bounded parallelism from concurrent_updates(8).
    appbot.update_queue.put_nowait(update)
    return {"ok": True}
