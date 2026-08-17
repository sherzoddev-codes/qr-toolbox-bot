import os
import re
import html
import asyncio
import logging
from io import BytesIO

from PIL import Image
import qrcode
from pyzbar.pyzbar import decode

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

# ==========================================
# 1. SOZLAMALAR
# ==========================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN topilmadi! .env faylini tekshiring.")

DB_NAME = os.getenv("DB_NAME", "qr_toolbox.sqlite3")
MAX_PHOTO_SIZE = 5_000_000  # 5 MB
ALLOWED_COLORS = {"black", "blue", "red", "green"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAIN_MENU_TEXTS = {"🔲 QR Yaratish", "📷 QR Skanerlash", "⚙️ Sozlamalar"}

# ==========================================
# 2. DATABASE (async, bloklamaydigan)
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as conn:
        await conn.execute(
            '''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                color TEXT DEFAULT 'black'
            )'''
        )
        await conn.commit()


async def get_color(user_id: int) -> str:
    async with aiosqlite.connect(DB_NAME) as conn:
        cursor = await conn.execute(
            'SELECT color FROM users WHERE user_id = ?', (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 'black'


async def update_color(user_id: int, color: str) -> None:
    if color not in ALLOWED_COLORS:
        color = "black"
    async with aiosqlite.connect(DB_NAME) as conn:
        await conn.execute(
            'INSERT OR REPLACE INTO users (user_id, color) VALUES (?, ?)',
            (user_id, color)
        )
        await conn.commit()


async def ensure_user(user_id: int) -> None:
    """Foydalanuvchini bazaga qo'shadi, agar mavjud bo'lmasa (default rang bilan)."""
    async with aiosqlite.connect(DB_NAME) as conn:
        await conn.execute(
            'INSERT OR IGNORE INTO users (user_id, color) VALUES (?, ?)',
            (user_id, 'black')
        )
        await conn.commit()

# ==========================================
# 3. HOLATLAR VA XAVFSIZLIK (FSM)
# ==========================================
class QRState(StatesGroup):
    waiting_for_data = State()


URL_PATTERN = re.compile(r'^(?:http|ftp)s?://', re.IGNORECASE)


def format_warning(url: str) -> str:
    safe_url = html.escape(url)
    return (
        "⚠️ <b>DIQQAT: XAVFSIZLIK OGOHLANTIRISHI</b>\n\n"
        "QR-kod ichida tashqi havola bor:\n"
        f"🔗 <code>{safe_url}</code>\n\n"
        "Noma'lum havolalarga kirishda ehtiyot bo'ling!"
    )

# ==========================================
# 4. BOT VA HANDLERLAR
# ==========================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔲 QR Yaratish"), KeyboardButton(text="📷 QR Skanerlash")],
        [KeyboardButton(text="⚙️ Sozlamalar")]
    ], resize_keyboard=True)


@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message.from_user.id)
    await message.answer(
        "QR Toolbox Botga xush kelibsiz! Kerakli bo'limni tanlang:",
        reply_markup=main_menu_kb()
    )


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_menu_kb())


@dp.message(F.text == "⚙️ Sozlamalar")
async def settings_cmd(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬛️ Qora", callback_data="color_black"),
         InlineKeyboardButton(text="🟦 Ko'k", callback_data="color_blue")],
        [InlineKeyboardButton(text="🟥 Qizil", callback_data="color_red"),
         InlineKeyboardButton(text="🟩 Yashil", callback_data="color_green")]
    ])
    await message.answer("QR-kod rangini tanlang:", reply_markup=kb)


@dp.callback_query(F.data.startswith("color_"))
async def set_color_cb(call: CallbackQuery):
    color = call.data.split("_", 1)[1]
    if color not in ALLOWED_COLORS:
        await call.answer("Noto'g'ri rang.", show_alert=True)
        return
    await update_color(call.from_user.id, color)
    await call.message.edit_text(f"✅ Rangi o'zgartirildi: {color.upper()}")
    await call.answer()


@dp.message(F.text == "🔲 QR Yaratish")
async def generate_prompt(message: Message, state: FSMContext):
    await state.set_state(QRState.waiting_for_data)
    await message.answer(
        "QR-kodga aylantirmoqchi bo'lgan matn yoki havolani yuboring.\n"
        "Bekor qilish uchun /cancel yozing."
    )


@dp.message(QRState.waiting_for_data)
async def generate_qr(message: Message, state: FSMContext):
    # Menyu tugmalaridan biri bosilgan bo'lsa - state'ni tozalab, o'sha handlerga yo'l qo'yamiz
    if message.text in MAIN_MENU_TEXTS:
        await state.clear()
        if message.text == "🔲 QR Yaratish":
            return await generate_prompt(message, state)
        elif message.text == "📷 QR Skanerlash":
            return await scan_prompt(message)
        elif message.text == "⚙️ Sozlamalar":
            return await settings_cmd(message, state)

    if not message.text:
        await message.answer("❌ Iltimos, faqat matn yoki havola yuboring.")
        return

    if len(message.text) > 1500:
        await message.answer("❌ Matn juda uzun (max 1500 belgi). Qisqaroq matn yuboring.")
        return

    color = await get_color(message.from_user.id)

    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(message.text)
        qr.make(fit=True)
        img = qr.make_image(fill_color=color, back_color="white")

        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        await message.answer_photo(
            BufferedInputFile(buffer.read(), filename="qr.png"),
            caption="✅ QR-kod tayyor!"
        )
    except Exception:
        logger.exception("QR generatsiyasida xatolik")
        await message.answer("❌ QR-kod yaratishda xatolik yuz berdi. Qaytadan urinib ko'ring.")
    finally:
        await state.clear()


@dp.message(F.text == "📷 QR Skanerlash")
async def scan_prompt(message: Message):
    await message.answer("Skanerlash uchun QR-kod rasmini yuboring.")


@dp.message(F.photo)
async def scan_qr(message: Message):
    photo = message.photo[-1]

    if photo.file_size and photo.file_size > MAX_PHOTO_SIZE:
        await message.answer("⚠️ Rasm hajmi juda katta (max 5MB). Kichikroq rasm yuboring.")
        return

    try:
        file = await bot.get_file(photo.file_id)
        downloaded = await bot.download_file(file.file_path)

        image = Image.open(BytesIO(downloaded.read()))
        decoded = decode(image)

        if not decoded:
            await message.answer("❌ Rasmdan QR-kod topilmadi. Aniqroq rasm yuboring.")
            return

        result = decoded[0].data.decode('utf-8', errors='replace')

        if URL_PATTERN.match(result):
            await message.answer(format_warning(result))
        else:
            safe_result = html.escape(result)
            await message.answer(f"✅ Skanerlash natijasi:\n<code>{safe_result}</code>")

    except Exception:
        logger.exception("QR skanerlashda xatolik")
        await message.answer("❌ O'qish jarayonida xatolik yuz berdi.")


# Kutilmagan xabar turlari uchun (stiker, ovoz, va h.k.) - foydalanuvchi "qotib qolmasligi" uchun
@dp.message()
async def fallback_handler(message: Message):
    await message.answer(
        "Iltimos, menyudan birini tanlang yoki /start bosing.",
        reply_markup=main_menu_kb()
    )

# ==========================================
# 5. ISHGA TUSHIRISH
# ==========================================
async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
