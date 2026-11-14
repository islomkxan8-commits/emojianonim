"""
anonim_emoji.py — Birlashtirilgan bot: "Name Emojis" (newbot_fixed) + "Logo/Text Emojis"
(tgs_make_bot) bitta botda.

/start bosilganda 2 ta bo'lim ko'rsatiladi:
  🔤 Name Emojis      — so'zdan tayyor shablon ustiga yozilgan animatsiyali emoji
  🖼 Logo/Text Emojis — 103 ta tayyor shablondan birini tanlab, ustiga
                        o'zingizning matningizni joylash

FAYL TUZILISHI:
    anonim_emoji.py        <- shu fayl (Telegram handlerlari)
    logo_engine.py         <- SVG/Lottie generatsiya "dvigateli" (tgs_make_bot'dan)
    template_engine.py     <- Name emoji render dvigateli (o'zgarishsiz)
    font_render.py         <- Name emoji shrift render (o'zgarishsiz)
    templates_config.py    <- Name emoji shablonlari sozlamalari (o'zgarishsiz)
    templates/*.json       <- Name emoji shablonlari
    templates_tgs/*.json   <- Logo/Text emoji shablonlari (yangilangan JSON'lar)
    fonts/*.ttf             <- Har ikkala bo'lim uchun shriftlar
"""

import asyncio
import json
import logging
import os
import random
import re
import string
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    PreCheckoutQuery,
)

import logo_engine  # webapp.py ctx orqali ishlatadi
import premium_emoji
from aiogram.types import WebAppInfo
from template_engine import render_template, save_as_tgs  # webapp.py ishlatadi
from templates_config import TEMPLATE_ORDER, TEMPLATES  # webapp.py ishlatadi

def _load_env():
    """Serverda: shu papkadagi .env faylidan KEY=VALUE qatorlarini o'qiydi (muhitda yo'q bo'lsa)."""
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(f):
        for line in open(f, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#") and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip().strip('"\'')


_load_env()
# Render: manzil va portni o'zi beradi
if os.environ.get("RENDER_EXTERNAL_URL"):
    os.environ.setdefault("WEBAPP_URL", os.environ["RENDER_EXTERNAL_URL"])
if os.environ.get("PORT"):
    os.environ.setdefault("WEBAPP_PORT", os.environ["PORT"])
# Doimiy ma'lumotlar papkasi (Render Disk bo'lsa DATA_DIR=/var/data)
DATA = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA, exist_ok=True)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
MAX_LEN = 12
DEFAULT_EXTRA_ADMINS = {6862368906}  # birinchi ishga tushganda admins.json ga yoziladi (@xwinzer)


def _random_nick(length: int = 12) -> str:
    """Telegram-style random lowercase nick, e.g. 'wiwheowuwhsj'."""
    return "".join(random.choices(string.ascii_lowercase, k=length))


ADMIN_ID = 0  # <-- shu yerga o'zingizning Telegram user_id'ingizni yozing
LOG_CHAT_ID = 0  # <-- shu yerga o'z log kanalingizning id'sini yozing
# Serverda ADMIN_ID / LOG_CHAT_ID ni kodga yozmasdan, muhit o'zgaruvchisi orqali ham berish mumkin:
ADMIN_ID = int(os.environ.get("ADMIN_ID") or ADMIN_ID)
LOG_CHAT_ID = int(os.environ.get("LOG_CHAT_ID") or LOG_CHAT_ID)
ADMINS_FILE = os.path.join(DATA, "admins.json")
USERS_FILE = os.path.join(DATA, "users.json")
EMOJI_PACK_FILE = os.path.join(DATA, "emoji_pack.json")
PRICE_FILES = {
    "name": os.path.join(DATA, "price_name.json"),
    "logo": os.path.join(DATA, "price_logo.json"),
}
STATS_FILE = os.path.join(DATA, "stats.json")
REFUND_REQUESTS_FILE = os.path.join(DATA, "refund_requests.json")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
DEFAULT_PRICE_STARS = 15
# Placeholder shown to anyone running a copy of this code who hasn't set
# their own support contact yet (settings.json isn't included in the sold
# .zip, so buyers never see the seller's real contact here).
DEFAULT_SUPPORT_CONTACT = "@Jahonngir777"
LOGO_PAGE_SIZE = 10
TOTAL_LOGO_TEMPLATES = 103

logging.basicConfig(level=logging.INFO)
router = Router()


# ============================================================================
# Umumiy: pack saqlash, ruxsatlar, foydalanuvchilar, narx, kreditlar, referal,
# majburiy kanallar — ikkala bo'lim (Name / Logo) uchun ham baravar ishlatiladi.
# ============================================================================

def _atomic_save_json(path: str, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)

def load_packs() -> dict:
    if not os.path.exists(EMOJI_PACK_FILE):
        return {}
    try:
        with open(EMOJI_PACK_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_packs(packs: dict):
    _atomic_save_json(EMOJI_PACK_FILE, packs)


def _safe_nick(nick: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", nick)
    return cleaned or "pack"


async def add_stickers_to_pack(
    bot: Bot, sticker_paths: list[str], nick: str, pack_kind: str, progress_message: Message | None = None,
    owner_id: int | None = None, title: str | None = None,
):
    """Create or reuse a sticker set for this nick+kind and add all the
    given stickers to it. pack_kind is 'emoji' (premium custom emoji pack)
    or 'sticker' (regular stickers pack). Returns the pack name, or None
    if it failed.

    owner_id is the Telegram user who ordered the pack - the set is
    created under their account (so it shows up in their own sticker/emoji
    catalog in Telegram), not the bot admin's. Falls back to ADMIN_ID if
    no user is available (e.g. an internal/admin-triggered call).

    Telegram enforces its own rate limit on sticker-set edits: if we hit
    it, the API raises TelegramRetryAfter rather than adding the sticker.
    We catch it per-sticker, sleep for the time Telegram asks for, and
    retry that same sticker - so the pack always finishes once the wait
    is over, instead of stopping partway.

    Progress is shown by editing a single message in place (not by
    sending a new message per sticker), so the chat doesn't get flooded
    with one line per item."""
    from aiogram.types import InputSticker

    sticker_type = "custom_emoji" if pack_kind == "emoji" else "regular"
    owner_id = owner_id or ADMIN_ID
    packs = load_packs()
    storage_key = f"{pack_kind}:{nick}:{owner_id}"
    name = packs.get(storage_key)
    total = len(sticker_paths)
    last_text = None

    async def update(text: str):
        nonlocal last_text
        if progress_message is None or text == last_text:
            return
        last_text = text
        try:
            await progress_message.edit_text(text)
        except Exception:
            pass

    try:
        me = await bot.get_me()
        if not name:
            name = f"{_safe_nick(nick)}_{owner_id}_by_{me.username}"

        for i, sticker_path in enumerate(sticker_paths):
            item = InputSticker(sticker=FSInputFile(sticker_path), format="animated", emoji_list=["🙂"])
            while True:
                try:
                    if i == 0 and storage_key not in packs:
                        try:
                            await bot.create_new_sticker_set(
                                user_id=owner_id,
                                name=name,
                                title=title or nick,
                                stickers=[item],
                                sticker_type=sticker_type,
                            )
                        except TelegramBadRequest as e:
                            if "already occupied" not in str(e).lower():
                                raise
                            await bot.add_sticker_to_set(user_id=owner_id, name=name, sticker=item)
                        packs[storage_key] = name
                        save_packs(packs)
                    else:
                        await bot.add_sticker_to_set(user_id=owner_id, name=name, sticker=item)
                    break
                except TelegramRetryAfter as e:
                    await update(
                        f"⏳ Telegram cheklovi sababli {e.retry_after}s kutyapmiz "
                        f"({i}/{total} qo'shildi), keyin davom etamiz..."
                    )
                    await asyncio.sleep(e.retry_after + 1)
                    continue

            await update(f"⏳ Tayyorlanmoqda: {i + 1}/{total} qo'shildi")
        return name
    except Exception as e:
        logging.warning(f"pack update failed: {e}")
        return None


def load_admins() -> set[int]:
    """Qo'shimcha adminlar (asosiy ADMIN_ID dan tashqari)."""
    if not os.path.exists(ADMINS_FILE):
        return set()
    try:
        with open(ADMINS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_admins(ids: set[int]):
    _atomic_save_json(ADMINS_FILE, sorted(ids))


def is_owner(user_id: int) -> bool:
    """Asosiy admin (ADMIN_ID) — adminlarni qo'shish/o'chirish va refund faqat unda."""
    return user_id == ADMIN_ID


def is_admin(user_id: int) -> bool:
    """Asosiy admin yoki qo'shilgan admin."""
    return user_id == ADMIN_ID or user_id in load_admins()


def load_users() -> set[int]:
    if not os.path.exists(USERS_FILE):
        return set()
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_users(ids: set[int]):
    _atomic_save_json(USERS_FILE, sorted(ids))


def record_user(user_id: int):
    """Remember that this user has started the bot, so broadcasts can reach them."""
    users = load_users()
    if user_id not in users:
        users.add(user_id)
        save_users(users)


def load_price(kind: str = "name") -> int:
    path = PRICE_FILES.get(kind, PRICE_FILES["name"])
    default = DEFAULT_PRICE_STARS
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return int(json.load(f).get("stars", default))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return default


def save_price(kind: str, stars: int):
    path = PRICE_FILES.get(kind, PRICE_FILES["name"])
    _atomic_save_json(path, {"stars": stars})


def load_stats() -> dict:
    if not os.path.exists(STATS_FILE):
        return {}
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_stats(stats: dict):
    _atomic_save_json(STATS_FILE, stats)


def record_pack_created(user_id: int, username: str | None):
    """Track how many packs (emoji/sticker sets) each user has generated,
    for the admin 'who made the most' leaderboard."""
    stats = load_stats()
    key = str(user_id)
    entry = stats.get(key, {"packs": 0, "stars": 0, "username": None})
    entry["packs"] = entry.get("packs", 0) + 1
    if username:
        entry["username"] = username
    stats[key] = entry
    save_stats(stats)


def record_stars_spent(user_id: int, username: str | None, amount: int):
    """Track how many Stars each user has paid the bot in total, for the
    admin 'who spent the most' leaderboard."""
    if amount <= 0:
        return
    stats = load_stats()
    key = str(user_id)
    entry = stats.get(key, {"packs": 0, "stars": 0, "username": None})
    entry["stars"] = entry.get("stars", 0) + amount
    if username:
        entry["username"] = username
    stats[key] = entry
    save_stats(stats)


def load_refund_requests() -> dict:
    if not os.path.exists(REFUND_REQUESTS_FILE):
        return {}
    try:
        with open(REFUND_REQUESTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_refund_requests(requests: dict):
    _atomic_save_json(REFUND_REQUESTS_FILE, requests)


def create_refund_request(user_id: int, charge_id: str, amount: int) -> str:
    """Stash a pending stale-price refund so the admin can trigger it with
    one tap from the log channel, without the charge_id (which can be
    long) needing to round-trip through callback_data."""
    requests = load_refund_requests()
    ref_id = f"{user_id}_{len(requests)}_{random.randint(0, 999999)}"
    requests[ref_id] = {
        "user_id": user_id, "charge_id": charge_id, "amount": amount, "done": False,
    }
    save_refund_requests(requests)
    return ref_id


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_settings(settings: dict):
    _atomic_save_json(SETTINGS_FILE, settings)


def get_channels() -> list[str]:
    channels = load_settings().get("channels")
    if channels:
        return channels
    legacy = load_settings().get("channel")
    return [legacy] if legacy else []


def set_channels(channels: list[str]):
    settings = load_settings()
    settings["channels"] = channels
    settings.pop("channel", None)
    save_settings(settings)


def get_support_contact() -> str:
    """Support/help contact shown in user-facing messages. Stored in
    settings.json (per-deployment, not shipped in the sold code package)
    so each buyer of the bot code sets their own without ever seeing the
    seller's."""
    return load_settings().get("support_contact") or DEFAULT_SUPPORT_CONTACT


def set_support_contact(contact: str):
    settings = load_settings()
    settings["support_contact"] = contact
    save_settings(settings)


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    channels = get_channels()
    if not channels:
        return True
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception as e:
            logging.warning(f"channel check failed for {channel}: {e}")
            continue
    return True


def subscribe_keyboard(channels: list[str]):
    rows = []
    for i, channel in enumerate(channels, start=1):
        uname = channel.lstrip("@")
        label = f"➕ Obuna bo'lish #{i}" if len(channels) > 1 else "📢 Kanalga o'tish"
        rows.append([InlineKeyboardButton(text=label, url=f"https://t.me/{uname}", style="primary")])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="checksub", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _normalize_hex(text: str):
    m = re.match(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$", (text or "").strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# ============================================================================
# FSM holatlar
# ============================================================================







# ============================================================================
# Bosh menyu (/start) — 2 bo'lim: Name Emojis / Logo Text Emojis
# ============================================================================

def app_url() -> str:
    return os.environ.get("WEBAPP_URL", "").strip()


def app_keyboard(tab: str = ""):
    url = app_url()
    if not url:
        return None
    if tab:
        url += ("&" if "?" in url else "?") + "tab=" + tab
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🚀 Ilovani ochish", web_app=WebAppInfo(url=url), style="success")]])


AVATAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "avatar.jpg")
_avatar_id = None


async def _home(message: Message):
    """Chatda faqat avatar va «Ilovani ochish» tugmasi turadi."""
    global _avatar_id
    caption = "🕶 <b>Anonim Emoji</b>\nAnimatsiyali premium emoji yasang 👇"
    try:
        m = await message.answer_photo(_avatar_id or FSInputFile(AVATAR), caption=caption, parse_mode="HTML", reply_markup=app_keyboard())
        _avatar_id = m.photo[-1].file_id
    except Exception:
        logging.exception("avatar yuborilmadi")
        await message.answer("Emoji yasash uchun ilovani oching 👇", reply_markup=app_keyboard())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    record_user(uid)
    channels = get_channels()
    if channels and not is_admin(uid) and not await is_subscribed(message.bot, uid):
        await message.answer("Botdan foydalanish uchun avval kanal(lar)ga a'zo bo'ling, so'ng \"Tekshirish\" tugmasini bosing:",
                             reply_markup=subscribe_keyboard(channels))
        return
    await _home(message)


@router.callback_query(F.data == "checksub")
async def check_subscription(callback: CallbackQuery):
    if get_channels() and not await is_subscribed(callback.bot, callback.from_user.id):
        await callback.answer("Hali barcha kanallarga a'zo bo'lmadingiz.", show_alert=True)
        return
    await callback.answer("✅ Tasdiqlandi!")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _home(callback.message)


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if is_admin(message.from_user.id):
        await message.answer("🛠 Admin panel ilovada ochiladi 👇", reply_markup=app_keyboard("admin"))


PRICE_LABELS = {"name": "Name emoji", "logo": "Logo/Text emoji"}


async def notify_price_change(bot: Bot, kind: str, old: int, new: int, who: str):
    if old == new:
        return
    try:
        await bot.send_message(LOG_CHAT_ID, f"💰 {PRICE_LABELS.get(kind, kind)} narxi o'zgardi\nNarxi: {new} ⭐ (oldin: {old} ⭐)\nO'zgartirdi: {who}")
    except Exception as e:
        logging.warning(f"price notify failed: {e}")


async def _finalize_pack(
    bot: Bot, chat_id: int, paths: list[str], nick: str, pack_kind: str, user=None, title: str | None = None,
):
    total = len(paths)
    status = await bot.send_message(chat_id, f"⏳ Tayyorlanmoqda: 0/{total}")

    owner_id = user.id if user is not None else ADMIN_ID
    pack_name = await add_stickers_to_pack(
        bot, paths, nick, pack_kind, progress_message=status, owner_id=owner_id, title=title,
    )
    if pack_name:
        link = "addemoji" if pack_kind == "emoji" else "addstickers"
        url = f"https://t.me/{link}/{pack_name}"
        try:
            await status.edit_text(f"✅ Tayyor! Mana emojingiz, to'lov uchun rahmat 🙏\n{url}")
        except Exception:
            await bot.send_message(chat_id, f"✅ Tayyor! Mana emojingiz, to'lov uchun rahmat 🙏\n{url}")
        try:
            who = f"@{user.username}" if user and user.username else f"id {user.id}" if user else "noma'lum"
            await bot.send_message(
                LOG_CHAT_ID, f"🆕 Yangi emoji: {who}\nNik: <code>{nick}</code>\nlink: <a href=\"{url}\">Ochish</a>",
                parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
            for p in paths:
                await bot.send_sticker(LOG_CHAT_ID, FSInputFile(p))
        except Exception as e:
            logging.warning(f"log channel post failed: {e}")
        if user is not None:
            record_pack_created(user.id, user.username)
            try:
                import webapp as _wa
                _wa.add_pack(user.id, title or nick, url, pack_kind)
            except Exception as e:
                logging.warning(f"pack history failed: {e}")
    else:
        try:
            await status.edit_text(f"❌ To'plamga qo'shib bo'lmadi. Xatolik bo'lsa {get_support_contact()} ga yozing.")
        except Exception:
            await bot.send_message(chat_id, f"❌ To'plamga qo'shib bo'lmadi. Xatolik bo'lsa {get_support_contact()} ga yozing.")
    await bot.send_message(chat_id, "Yangisini yasash uchun /start bosing.")


@router.pre_checkout_query()
async def process_pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    """Ilova to'lovlari webapp.py da qayta ishlanadi; bu yerga faqat noma'lum to'lovlar tushadi."""
    sp, user = message.successful_payment, message.from_user
    amount = sp.total_amount or 0
    record_stars_spent(user.id, user.username, amount)
    ref_id = create_refund_request(user.id, sp.telegram_payment_charge_id, amount)
    await message.answer(f"❌ Bu to'lov tasdiqlanmadi. {get_support_contact()} ga yozing.")
    who = f"@{user.username}" if user.username else f"id {user.id}"
    try:
        await message.bot.send_message(LOG_CHAT_ID, f"⚠️ Noma'lum to'lov: {who}, {amount} ⭐", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"💸 {amount} ⭐ ni qaytarish", callback_data=f"refundcode:{ref_id}")]]))
    except Exception as e:
        logging.warning(f"log channel post failed: {e}")


@router.callback_query(F.data.startswith("refundcode:"))
async def refund_code_payment(callback: CallbackQuery, state: FSMContext):
    """The refund button posted to the log channel. Anyone in that channel
    can see it, but only ADMIN_ID is allowed to actually trigger the
    refund - everyone else just gets a 'no permission' popup and nothing
    happens."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Sizda bu tugmadan foydalanish huquqi yo'q.", show_alert=True)
        return

    ref_id = callback.data.split(":", 1)[1]
    requests = load_refund_requests()
    entry = requests.get(ref_id)
    if not entry:
        await callback.answer("Bu so'rov topilmadi (eskirgan bo'lishi mumkin).", show_alert=True)
        return
    if entry.get("done"):
        await callback.answer("Bu allaqachon qaytarilgan.", show_alert=True)
        return

    try:
        await callback.bot.refund_star_payment(
            user_id=entry["user_id"], telegram_payment_charge_id=entry["charge_id"],
        )
    except Exception as e:
        logging.exception("manual refund failed")
        await callback.answer(f"Qaytarishda xatolik: {e}", show_alert=True)
        return

    entry["done"] = True
    requests[ref_id] = entry
    save_refund_requests(requests)

    await callback.answer("✅ Qaytarildi.", show_alert=True)
    try:
        await callback.message.edit_text(callback.message.text + "\n\n✅ Qaytarildi.")
    except Exception:
        pass


@router.errors()
async def errors_handler(event) -> bool:
    """Telegram raises TelegramForbiddenError whenever we try to message a
    user who has blocked the bot or deleted the chat - this is routine and
    not a bug, so we just note it quietly instead of dumping a traceback
    for every occurrence. Anything else is still logged with its
    traceback so real bugs stay visible."""
    from aiogram.exceptions import TelegramForbiddenError

    exception = event.exception
    if isinstance(exception, TelegramForbiddenError):
        logging.info(f"user blocked the bot / chat unavailable: {exception}")
        return True
    logging.exception("Unhandled error while processing update", exc_info=exception)
    return True


HEARTBEAT_FILE = os.path.join(DATA, "heartbeat.json")


def _fmt_dur(sec: float) -> str:
    m = int(sec // 60)
    d, h, m = m // 1440, m % 1440 // 60, m % 60
    return " ".join(x for x in (f"{d} kun" if d else "", f"{h} soat" if h else "", f"{m} daqiqa" if m or not (d or h) else "") if x)


async def _heartbeat():
    while True:
        _atomic_save_json(HEARTBEAT_FILE, {"ts": time.time()})
        await asyncio.sleep(20)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    if not os.path.exists(ADMINS_FILE):
        save_admins(set(DEFAULT_EXTRA_ADMINS) - {ADMIN_ID})
    # Premium emoji: middleware + /pe_add, /pe_list, /pe_del (asosiy router'dan OLDIN ulanadi)
    premium_emoji.setup(dp, bot, is_admin=is_admin)
    # Mini App (WEBAPP_URL berilsa yoqiladi) — asosiy router'dan OLDIN ulanadi
    import sys as _sys, webapp
    webapp.setup(dp, _sys.modules[__name__])
    asyncio.create_task(webapp.run_server(bot, _sys.modules[__name__]))
    asyncio.create_task(webapp.contest_loop(bot, _sys.modules[__name__]))
    dp.include_router(router)

    # Faol / o'chiq holat xabarlari (log kanalga)
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            last = float(json.load(f).get("ts", 0))
    except (OSError, ValueError, TypeError):
        last = 0
    text = "🟢 Bot faol holatda!"
    if last and time.time() - last > 90:
        text += f"\n⏱ Bot {_fmt_dur(time.time() - last)} o'chiq turdi."
    try:
        await bot.send_message(LOG_CHAT_ID, text)
    except Exception as e:
        logging.warning(f"start notify failed: {e}")
    hb = asyncio.create_task(_heartbeat())

    async def on_shutdown():
        hb.cancel()
        _atomic_save_json(HEARTBEAT_FILE, {"ts": time.time()})
        try:
            await bot.send_message(LOG_CHAT_ID, "🔴 Bot o'chirildi. Qayta yoqilganda necha vaqt o'chiq turgani aytiladi.")
        except Exception:
            pass

    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)


_LOCK_PATH = os.path.join(DATA, "anonim_emoji.lock")
_lock_file_handle = None  # keep a reference so the OS lock isn't released early


def _acquire_single_instance_lock():
    """Bitta paytda faqat bitta bot nusxasi ishlashini ta'minlaydi.

    PID'ni faylga yozib, keyin kill(pid, 0) bilan tekshirish (eski usul)
    ishonchsiz edi: agar server/konteyner qayta ishga tushsa yoki PID
    boshqa, umuman bog'liq bo'lmagan jarayonga qayta berilib qolsa, bot
    "band" deb bekorga to'xtab qolishi yoki, aksincha, band emas deb
    ikkinchi nusxa ham ishga tushib ketishi mumkin edi — aynan shu ikkinchi
    holat ikkita jarayon bitta botning xabarlarini bo'lib olib, savol
    ikki marta so'ralishi / /start va /admin kech yoki qo'sh javob
    berishiga sabab bo'ladi.

    Buning o'rniga OS darajasidagi eksklyuziv fayl qulfidan (flock)
    foydalanamiz: uni faqat bitta jarayon ushlab turishi mumkin, va
    jarayon qanday tugasa ham (hatto kutilmagan crash bilan) OS uni
    darhol avtomatik bo'shatadi — shuning uchun eskirgan/noto'g'ri qulf
    qolib ketishi mumkin emas."""
    global _lock_file_handle

    _lock_file_handle = open(_LOCK_PATH, "w")
    try:
        try:
            import fcntl  # Linux / macOS
            fcntl.flock(_lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # Windows
            import msvcrt
            msvcrt.locking(_lock_file_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(
            "❌ Bot allaqachon ishlab turibdi (boshqa nusxa shu faylni band qilgan: "
            f"{_LOCK_PATH}). Avval eski jarayonni to'xtating: pkill -f 'python3 anonim_emoji.py'",
            flush=True,
        )
        raise SystemExit(1)

    _lock_file_handle.write(str(os.getpid()))
    _lock_file_handle.flush()
    # _lock_file_handle qasddan yopilmaydi - jarayon tugaganda OS qulfni
    # o'zi bo'shatadi (garchi bu funksiya qaytib ketsa ham).


if __name__ == "__main__":
    _acquire_single_instance_lock()
    asyncio.run(main())
