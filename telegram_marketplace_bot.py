"""
Telegram Marketplace Bot (Барахолка по городам РФ)
================================================

This bot implements a simple classifieds marketplace on Telegram.  Users can
post listings for items they want to sell, search for listings in their
city, save favourites, and communicate anonymously through the bot.  All
interactions use edited messages to minimise chat clutter – the bot keeps a
single message per user session and updates its contents instead of
spamming the user with new messages.  Listings must be approved by an
administrator before they become visible.  Messages between buyers and
sellers are proxied by the bot, and users can mute notifications globally
or per‐conversation.  The implementation uses SQLite for persistence and
the `aiogram` library for Telegram integration.

Categories
----------

The bot uses a fixed list of categories inspired by the categories on the
Avito marketplace.  According to an Avito guide on choosing a category,
major categories include: “Личные вещи” (Personal items), “Транспорт”
(Transportation), “Работа” (Jobs), “Для дома и дачи” (Home and garden),
“Недвижимость” (Real estate), “Предложение услуг” (Services), “Хобби и
отдых” (Hobbies and leisure) and “Электроника” (Electronics)【171045959754390†L10-L13】.
These categories form the top‐level list used when publishing or filtering
listings.

Usage
-----

* Run this script with Python 3.10+ and install dependencies: `pip install
  aiogram aiosqlite`.
* Create a bot via @BotFather and copy the token into the `BOT_TOKEN`
  environment variable or assign it directly below.
* The database will be created in the same directory on first run.
* The administrator ID should be set to your Telegram numeric ID.
* To start the bot: `python telegram_marketplace_bot.py`.
* The bot stores user data, listings, favourites and messages in a
  SQLite database.  See the `init_db` function for schema details.
* Moderation commands are available only to admins: `/moderate` lists
  pending listings and lets the admin accept or deny them.

Limitations
-----------

This implementation is intended as a starting point and does not include
full error handling or a polished user interface.  It uses simple
in-memory state machines to guide the user through listing creation and
searching.  In a production deployment you would likely want to refine
navigation, improve efficiency, add caching and support rich media
previews.  Nonetheless, it demonstrates the core features described in the
specification.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from aiogram.client.default import DefaultBotProperties
from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InputMediaPhoto,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiosqlite
from aiogram import F
from aiogram.filters import Command, CommandStart, StateFilter


logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = "8210363818:AAFGWN9RxzFQbovE-VE4g0WW0fsOCvUP9Cg"
ADMIN_ID = 7066340788  # твой Telegram user_id (можно узнать у @userinfobot)


# List of stopwords to ignore when searching titles.  These are common
# conjunctions and prepositions in Russian.  Expand as needed.
STOPWORDS = {
    "и", "а", "ну", "да", "не", "если", "что", "как", "когда", "или",
    "но", "на", "под", "по", "с", "со", "из", "от", "до", "за", "для", "во",
}

# Top-level categories inspired by Avito【171045959754390†L10-L13】
CATEGORIES = [
    "Личные вещи",
    "Транспорт",
    "Работа",
    "Для дома и дачи",
    "Недвижимость",
    "Предложение услуг",
    "Хобби и отдых",
    "Электроника",
]

# Conditions for items
CONDITIONS = ["Новое", "Б/у"]

# Patterns for validating city and microdistrict inputs.  We do not enforce
# a fixed list of cities here to keep the file size reasonable, but we
# reject obviously invalid inputs.  Cities must consist of Russian letters,
# spaces or hyphens and be between 2 and 50 characters.  Microdistricts
# may also include digits.
CITY_PATTERN = re.compile(r"^[А-Яа-яЁё\s\-]{2,50}$")
MICRODISTRICT_PATTERN = re.compile(r"^[А-Яа-яЁё0-9\s\-]{2,50}$")

# SQLite database file
DB_FILENAME = "marketplace.db"


# ---------------------------------------------------------------------------
# Database helper functions
# ---------------------------------------------------------------------------
router = Router()

async def init_db():
    """Initialise the SQLite database with required tables."""
    async with aiosqlite.connect(DB_FILENAME) as db:
        # Users
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                city TEXT NOT NULL,
                microdistrict TEXT,
                muted INTEGER DEFAULT 0
            )"""
        )
        # Listings
        await db.execute(
            """CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                city TEXT NOT NULL,
                microdistrict TEXT,
                category TEXT NOT NULL,
                condition TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                price INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(owner_id) REFERENCES users(user_id)
            )"""
        )
        # Listing photos
        await db.execute(
            """CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER,
                file_id TEXT,
                FOREIGN KEY(listing_id) REFERENCES listings(id)
            )"""
        )
        # Favourites
        await db.execute(
            """CREATE TABLE IF NOT EXISTS favourites (
                user_id INTEGER,
                listing_id INTEGER,
                PRIMARY KEY(user_id, listing_id),
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(listing_id) REFERENCES listings(id)
            )"""
        )
        # Messages
        await db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                listing_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                read INTEGER DEFAULT 0,
                FOREIGN KEY(listing_id) REFERENCES listings(id)
            )"""
        )
        # Chat participants (to track chats per user and unread counts)
        await db.execute(
            """CREATE TABLE IF NOT EXISTS chats (
                chat_id TEXT PRIMARY KEY,
                user1_id INTEGER,
                user2_id INTEGER,
                listing_id INTEGER,
                last_message_time TIMESTAMP,
                FOREIGN KEY(user1_id) REFERENCES users(user_id),
                FOREIGN KEY(user2_id) REFERENCES users(user_id),
                FOREIGN KEY(listing_id) REFERENCES listings(id)
            )"""
        )
        await db.commit()


async def get_or_create_user(user_id: int, city: str = None, microdistrict: str = None) -> None:
    """Insert a user row if not exists, or update city/microdistrict if provided."""
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            # Update city/microdistrict if provided
            if city or microdistrict:
                await db.execute(
                    "UPDATE users SET city = COALESCE(?, city), microdistrict = COALESCE(?, microdistrict) WHERE user_id = ?",
                    (city, microdistrict, user_id),
                )
                await db.commit()
        else:
            await db.execute(
                "INSERT INTO users (user_id, city, microdistrict) VALUES (?, ?, ?)",
                (user_id, city or "", microdistrict),
            )
            await db.commit()


async def update_user_mute(user_id: int, muted: bool) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        await db.execute("UPDATE users SET muted = ? WHERE user_id = ?", (1 if muted else 0, user_id))
        await db.commit()


async def is_user_muted(user_id: int) -> bool:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute("SELECT muted FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row[0])


async def add_listing(owner_id: int, city: str, microdistrict: str, category: str, condition: str, title: str, description: str, price: int) -> int:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute(
            "INSERT INTO listings (owner_id, city, microdistrict, category, condition, title, description, price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, city, microdistrict, category, condition, title, description, price),
        )
        listing_id = cur.lastrowid
        await db.commit()
        return listing_id


async def add_photo(listing_id: int, file_id: str) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        await db.execute("INSERT INTO photos (listing_id, file_id) VALUES (?, ?)", (listing_id, file_id))
        await db.commit()


# ---------------------------------------------------------------------------
# Additional helpers
# ---------------------------------------------------------------------------

async def clear_listing_photos(listing_id: int) -> None:
    """Remove all stored photos for a given listing.

    When editing a listing the seller may wish to replace the existing
    images.  This helper deletes all photo rows for the listing so new
    images can be added fresh.
    """
    async with aiosqlite.connect(DB_FILENAME) as db:
        await db.execute("DELETE FROM photos WHERE listing_id = ?", (listing_id,))
        await db.commit()


async def get_listing(listing_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute(
            "SELECT id, owner_id, city, microdistrict, category, condition, title, description, price, status FROM listings WHERE id = ?",
            (listing_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        cur_ph = await db.execute("SELECT file_id FROM photos WHERE listing_id = ?", (listing_id,))
        photos = [r[0] for r in await cur_ph.fetchall()]
        return {
            "id": row[0],
            "owner_id": row[1],
            "city": row[2],
            "microdistrict": row[3],
            "category": row[4],
            "condition": row[5],
            "title": row[6],
            "description": row[7],
            "price": row[8],
            "status": row[9],
            "photos": photos,
        }


async def list_user_listings(user_id: int, status_filter: Optional[str] = None, offset: int = 0, limit: int = 5) -> List[dict]:
    async with aiosqlite.connect(DB_FILENAME) as db:
        if status_filter:
            cur = await db.execute(
                "SELECT id, title, price FROM listings WHERE owner_id = ? AND status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, status_filter, limit, offset),
            )
        else:
            cur = await db.execute(
                "SELECT id, title, price FROM listings WHERE owner_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            )
        rows = await cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "price": r[2]} for r in rows
        ]


async def list_pending_listings(offset: int = 0, limit: int = 5) -> List[dict]:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute(
            "SELECT id, owner_id, title, category, price FROM listings WHERE status = 'pending' ORDER BY created_at ASC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "owner_id": r[1],
                "title": r[2],
                "category": r[3],
                "price": r[4],
            }
            for r in rows
        ]


async def update_listing_status(listing_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        await db.execute("UPDATE listings SET status = ? WHERE id = ?", (status, listing_id))
        await db.commit()


async def update_listing_field(listing_id: int, field: str, value) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        await db.execute(f"UPDATE listings SET {field} = ? WHERE id = ?", (value, listing_id))
        await db.commit()


async def search_listings(city: str, microdistrict: Optional[str], category: Optional[str], condition: Optional[str], query: str, offset: int = 0, limit: int = 10) -> List[dict]:
    """Search for listings matching the criteria in the same city (and optionally microdistrict)."""
    async with aiosqlite.connect(DB_FILENAME) as db:
        # Build dynamic SQL query
        base_query = "SELECT id, title, price, category, condition FROM listings WHERE status = 'approved' AND city = ?"
        params: List = [city]
        # We intentionally ignore microdistrict when searching to broaden results
        if category:
            base_query += " AND category = ?"
            params.append(category)
        if condition:
            base_query += " AND condition = ?"
            params.append(condition)
        # Prepare search terms (remove stopwords and punctuation, case‐insensitive)
        terms = [
            t.lower()
            for t in re.split("\W+", query)
            if t and t.lower() not in STOPWORDS
        ]
        # Title and description search
        if terms:
            like_clauses = ["(title LIKE ? OR description LIKE ?)"] * len(terms)
            base_query += " AND " + " AND ".join(like_clauses)
            for t in terms:
                params.extend([f"%{t}%", f"%{t}%"])
        base_query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await db.execute(base_query, tuple(params))
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "price": r[2],
                "category": r[3],
                "condition": r[4],
            }
            for r in rows
        ]


async def add_favourite(user_id: int, listing_id: int) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        try:
            await db.execute("INSERT INTO favourites (user_id, listing_id) VALUES (?, ?)", (user_id, listing_id))
            await db.commit()
        except aiosqlite.IntegrityError:
            pass  # Already in favourites


async def remove_favourite(user_id: int, listing_id: int) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        await db.execute("DELETE FROM favourites WHERE user_id = ? AND listing_id = ?", (user_id, listing_id))
        await db.commit()


async def list_favourites(user_id: int, offset: int = 0, limit: int = 5) -> List[dict]:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute(
            "SELECT l.id, l.title, l.price FROM listings l JOIN favourites f ON l.id = f.listing_id WHERE f.user_id = ? AND l.status = 'approved' ORDER BY l.created_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "price": r[2]} for r in rows
        ]


def chat_id_from_users(user1: int, user2: int, listing_id: int) -> str:
    """Deterministically compute a chat ID for a pair of users and a listing."""
    # Sort user IDs to avoid duplicates (so chat is same whichever side initiates)
    a, b = sorted([user1, user2])
    return f"{a}_{b}_{listing_id}"


async def ensure_chat(chat_id: str, user1: int, user2: int, listing_id: int) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO chats (chat_id, user1_id, user2_id, listing_id, last_message_time) VALUES (?, ?, ?, ?, datetime('now'))",
                (chat_id, user1, user2, listing_id),
            )
            await db.commit()


async def store_message(chat_id: str, sender_id: int, receiver_id: int, listing_id: int, text: str) -> None:
    async with aiosqlite.connect(DB_FILENAME) as db:
        await db.execute(
            "INSERT INTO messages (chat_id, sender_id, receiver_id, listing_id, text) VALUES (?, ?, ?, ?, ?)",
            (chat_id, sender_id, receiver_id, listing_id, text),
        )
        # Update last_message_time on chat
        await db.execute(
            "UPDATE chats SET last_message_time = datetime('now') WHERE chat_id = ?",
            (chat_id,),
        )
        # Mark messages from receiver as read when sender replies
        await db.execute(
            "UPDATE messages SET read = 1 WHERE chat_id = ? AND receiver_id = ?",
            (chat_id, sender_id),
        )
        await db.commit()


async def fetch_messages(chat_id: str, offset: int = 0, limit: int = 5, reverse: bool = True) -> List[dict]:
    async with aiosqlite.connect(DB_FILENAME) as db:
        order = "DESC" if reverse else "ASC"
        cur = await db.execute(
            f"SELECT sender_id, receiver_id, text, created_at FROM messages WHERE chat_id = ? ORDER BY id {order} LIMIT ? OFFSET ?",
            (chat_id, limit, offset),
        )
        rows = await cur.fetchall()
        messages = [
            {
                "sender_id": r[0],
                "receiver_id": r[1],
                "text": r[2],
                "created_at": r[3],
            }
            for r in rows
        ]
        if reverse:
            messages.reverse()  # return chronological order
        return messages


async def list_user_chats(user_id: int, offset: int = 0, limit: int = 5) -> List[dict]:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute(
            "SELECT chat_id, user1_id, user2_id, listing_id FROM chats WHERE user1_id = ? OR user2_id = ? ORDER BY last_message_time DESC LIMIT ? OFFSET ?",
            (user_id, user_id, limit, offset),
        )
        rows = await cur.fetchall()
        chats = []
        for r in rows:
            chat_id = r[0]
            user1 = r[1]
            user2 = r[2]
            listing_id = r[3]
            partner_id = user2 if user1 == user_id else user1
            chats.append({
                "chat_id": chat_id,
                "partner_id": partner_id,
                "listing_id": listing_id,
            })
        return chats


# ---------------------------------------------------------------------------
# FSM states for creating and editing listings
# ---------------------------------------------------------------------------

class NewListing(StatesGroup):
    waiting_for_photo = State()
    waiting_for_more_photos = State()
    waiting_for_category = State()
    waiting_for_condition = State()
    waiting_for_price = State()
    waiting_for_title = State()
    waiting_for_description = State()


class EditListing(StatesGroup):
    selecting_listing = State()
    selecting_field = State()
    editing_photos = State()
    editing_category = State()
    editing_condition = State()
    editing_price = State()
    editing_title = State()
    editing_description = State()

# ---------------------------------------------------------------------------
# Additional state groups for registration, search and chat flows
# ---------------------------------------------------------------------------

class Registration(StatesGroup):
    """States for user onboarding: selecting city and microdistrict."""
    awaiting_city = State()
    awaiting_microdistrict = State()


class SearchStates(StatesGroup):
    """States for the search flow."""
    search_init = State()
    search_filter_category = State()
    search_filter_condition = State()
    search_query = State()


class ChatStates(StatesGroup):
    """State for active chat sessions."""
    chatting = State()

# Additional state group for changing location in settings
class ChangeLocation(StatesGroup):
    awaiting_city = State()
    awaiting_microdistrict = State()


# Helper dataclass for tracking user session message IDs
@dataclass
class Session:
    message_id: int
    current_menu: str


# In-memory mapping from user_id to Session (store message id and menu name)
SESSIONS: Dict[int, Session] = {}


# ---------------------------------------------------------------------------
# Bot initialisation
# ---------------------------------------------------------------------------

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ---------------------------------------------------------------------------
# Utility functions for sending and editing menus
# ---------------------------------------------------------------------------

async def get_user_info(user_id: int) -> Optional[Dict[str, str]]:
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute("SELECT city, microdistrict FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            return {"city": row[0], "microdistrict": row[1]}
        return None


async def ensure_session_message(chat_id: int, text: str, keyboard: InlineKeyboardMarkup) -> int:
    """Send a new message or edit the existing session message for the user."""
    session = SESSIONS.get(chat_id)
    if session:
        try:
            # Use keyword arguments for chat_id and message_id.  Passing them positionally
            # would cause aiogram 3.7+ to treat the second positional argument as
            # business_connection_id which must be a string.  See changelog for details.
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=session.message_id,
                reply_markup=keyboard,
            )
            session.current_menu = text
            return session.message_id
        except Exception as e:
            logging.warning(f"Failed to edit message: {e}")
    # No existing session message or edit failed: send a new one
    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    SESSIONS[chat_id] = Session(message_id=sent.message_id, current_menu=text)
    return sent.message_id


def main_menu_kb() -> InlineKeyboardMarkup:
    """Construct the main menu inline keyboard."""
    # Add a settings button on its own row for changing user preferences
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Поиск", callback_data="menu_search"),
                InlineKeyboardButton(text="📦 Твои объявления", callback_data="menu_listings"),
            ],
            [
                InlineKeyboardButton(text="💬 Чаты", callback_data="menu_chats"),
                InlineKeyboardButton(text="⭐ Избранное", callback_data="menu_favourites"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings"),
            ],
        ]
    )


async def show_main_menu(user_id: int) -> None:
    text = "<b>Главное меню</b>\nВыберите действие:"
    kb = main_menu_kb()
    await ensure_session_message(user_id, text, kb)

async def settings_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Display settings menu where user can update preferences."""
    user_id = callback.from_user.id
    text = "<b>Настройки</b>\nВыберите параметр для изменения:"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить город", callback_data="settings_change_city")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await ensure_session_message(user_id, text, kb)
    # no need to set state here; options will set states


# ---------------------------------------------------------------------------
# Registration and /start handler
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user_id = message.from_user.id
    # Check if user exists in DB
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute("SELECT city FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row and row[0]:
            # Already registered: show main menu
            await show_main_menu(user_id)
        else:
            # Ask for city
            await bot.send_message(user_id, "Добро пожаловать в барахолку! Пожалуйста, укажите ваш город.")
            await state.set_state(Registration.awaiting_city)


@router.message(StateFilter(Registration.awaiting_city))
async def process_city(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    city = (message.text or "").strip()
    # Validate city input: must match the allowed pattern
    if not CITY_PATTERN.match(city):
        await message.answer("Название города должно содержать только буквы, пробелы или дефис и быть не короче 2 символов. Попробуйте ещё раз.")
        return
    await get_or_create_user(user_id, city=city)
    await bot.send_message(user_id, "Укажите ваш микрорайон (можно пропустить, отправив '-').")
    await state.update_data(city=city)
    await state.set_state(Registration.awaiting_microdistrict)


@router.message(StateFilter(Registration.awaiting_microdistrict))
async def process_microdistrict(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    microdistrict = (message.text or "").strip()
    if microdistrict == "-":
        microdistrict = None
    else:
        # Validate microdistrict if provided
        if not MICRODISTRICT_PATTERN.match(microdistrict):
            await message.answer("Название микрорайона может содержать буквы, цифры, пробелы и дефис и должно быть не короче 2 символов. Попробуйте ещё раз, либо отправьте '-' чтобы пропустить.")
            return
    data = await state.get_data()
    city = data.get("city")
    await get_or_create_user(user_id, city=city, microdistrict=microdistrict)
    await bot.send_message(user_id, f"Город сохранён: {city}. Вы можете изменить в профиле позже.")
    await state.clear()
    await show_main_menu(user_id)


# ---------------------------------------------------------------------------
# Main menu callbacks
# ---------------------------------------------------------------------------

@router.callback_query(lambda cb: cb.data and cb.data.startswith("menu_"))
async def process_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    action = callback.data[len("menu_") :]
    await state.clear()
    if action == "search":
        await search_menu(callback, state)
    elif action == "listings":
        await listings_menu(callback, state)
    elif action == "chats":
        await chats_menu(callback, state)
    elif action == "favourites":
        await favourites_menu(callback, state)
    elif action == "settings":
        await settings_menu(callback, state)
    else:
        await show_main_menu(user_id)
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "back_main")
async def process_back_main(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    await state.clear()
    await show_main_menu(user_id)
    await callback.answer()


# ---------------------------------------------------------------------------
# Settings handlers
# ---------------------------------------------------------------------------

@router.callback_query(lambda cb: cb.data == "settings_change_city")
async def settings_change_city(callback: CallbackQuery, state: FSMContext) -> None:
    """Initiate changing user's city via settings."""
    user_id = callback.from_user.id
    # Clear any previous state
    await state.clear()
    await bot.send_message(user_id, "Введите новый город:")
    await state.set_state(ChangeLocation.awaiting_city)
    await callback.answer()

@router.message(StateFilter(ChangeLocation.awaiting_city))
async def settings_process_city(message: Message, state: FSMContext) -> None:
    """Process the city entered in settings flow."""
    user_id = message.from_user.id
    city = (message.text or "").strip()
    if not CITY_PATTERN.match(city):
        await message.answer(
            "Название города должно содержать только буквы, пробелы или дефис и быть не короче 2 символов. Попробуйте ещё раз."
        )
        return
    # Save city to state and ask for microdistrict
    await state.update_data(new_city=city)
    await bot.send_message(user_id, "Введите новый микрорайон (или '-' чтобы пропустить):")
    await state.set_state(ChangeLocation.awaiting_microdistrict)

@router.message(StateFilter(ChangeLocation.awaiting_microdistrict))
async def settings_process_microdistrict(message: Message, state: FSMContext) -> None:
    """Process microdistrict during settings change flow."""
    user_id = message.from_user.id
    microdistrict = (message.text or "").strip()
    if microdistrict == "-":
        microdistrict = None
    else:
        if not MICRODISTRICT_PATTERN.match(microdistrict):
            await message.answer(
                "Название микрорайона может содержать буквы, цифры, пробелы и дефис и должно быть не короче 2 символов. Попробуйте ещё раз, либо отправьте '-' чтобы пропустить."
            )
            return
    data = await state.get_data()
    new_city = data.get("new_city")
    # Update user record with new city and microdistrict
    await get_or_create_user(user_id, city=new_city, microdistrict=microdistrict)
    # Notify user and return to main menu
    await bot.send_message(user_id, f"Ваш город обновлён на: {new_city}.")
    await state.clear()
    await show_main_menu(user_id)


# ---------------------------------------------------------------------------
# Search menu and handlers
# ---------------------------------------------------------------------------

SEARCH_LIMIT = 5


async def search_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    # Ask the user to pick filters or skip
    text = "<b>Поиск лотов</b>\nВыберите фильтры или нажмите \u202f\u200b\u202fПропустить\u202f\u200b\u202f, чтобы перейти к поиску."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Категория", callback_data="search_category"),
                InlineKeyboardButton(text="Состояние", callback_data="search_condition"),
            ],
            [
                InlineKeyboardButton(text="Пропустить", callback_data="search_skip"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main"),
            ],
        ]
    )
    await ensure_session_message(user_id, text, kb)
    # Reset search state
    await state.clear()
    await state.set_state(SearchStates.search_init)


@router.callback_query(StateFilter(SearchStates.search_init), F.data.startswith("search_"))
async def process_search_filters(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    action = callback.data[len("search_") :]
    if action == "category":
        # Ask user to pick category
        # Build category keyboard with skip
        rows = []
        # two buttons per row
        temp = []
        for cat in CATEGORIES:
            temp.append(InlineKeyboardButton(text=cat, callback_data=f"filter_category_{cat}"))
            if len(temp) == 2:
                rows.append(temp)
                temp = []
        if temp:
            rows.append(temp)
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="filter_category_skip")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await ensure_session_message(user_id, "Выберите категорию:", kb)
        await state.set_state(SearchStates.search_filter_category)
    elif action == "condition":
        rows = []
        for cond in CONDITIONS:
            rows.append([InlineKeyboardButton(text=cond, callback_data=f"filter_condition_{cond}")])
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="filter_condition_skip")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await ensure_session_message(user_id, "Выберите состояние товара:", kb)
        await state.set_state(SearchStates.search_filter_condition)
    elif action == "skip":
        # Go directly to entering search query
        await ask_search_query(user_id, state)
    await callback.answer()


@router.callback_query(StateFilter(SearchStates.search_filter_category), F.data.startswith("filter_category_"))
async def set_filter_category(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    cat = callback.data[len("filter_category_") :]
    if cat == "skip":
        await state.update_data(category=None)
    else:
        await state.update_data(category=cat)
    # After selecting category, ask for condition filter or skip to search query
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Состояние", callback_data="search_condition"), InlineKeyboardButton(text="Перейти к поиску", callback_data="search_skip")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await ensure_session_message(user_id, "Категория выбрана. Выберите следующие действия:", kb)
    await state.set_state(SearchStates.search_init)
    await callback.answer()


@router.callback_query(StateFilter(SearchStates.search_filter_condition), F.data.startswith("filter_condition_"))
async def set_filter_condition(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    cond = callback.data[len("filter_condition_") :]
    if cond == "skip":
        await state.update_data(condition=None)
    else:
        await state.update_data(condition=cond)
    # Ask for category filter or proceed to search
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Категория", callback_data="search_category"), InlineKeyboardButton(text="Перейти к поиску", callback_data="search_skip")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await ensure_session_message(user_id, "Состояние выбрано. Выберите следующие действия:", kb)
    await state.set_state(SearchStates.search_init)
    await callback.answer()


async def ask_search_query(user_id: int, state: FSMContext) -> None:
    await state.set_state(SearchStates.search_query)
    await bot.send_message(user_id, "Введите поисковую фразу. Фильтры применяются автоматически. Отправьте '-' для поиска без слов.")


@router.message(StateFilter(SearchStates.search_query))
async def perform_search(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    query_text = (message.text or "").strip()
    if query_text == "-":
        query_text = ""
    # Get filters from state
    data = await state.get_data()
    category = data.get("category")
    condition = data.get("condition")
    user_info = await get_user_info(user_id)
    if not user_info:
        await message.answer("Не удалось определить ваш город. Попробуйте начать сначала.")
        await show_main_menu(user_id)
        await state.clear()
        return
    city = user_info["city"]
    # Do not filter by microdistrict during search.  Ignore user's microdistrict.
    results = await search_listings(city, None, category, condition, query_text, offset=0, limit=SEARCH_LIMIT)
    if results:
        # Build list of results with buttons
        rows = []
        for item in results:
            title = item["title"]
            price = item["price"]
            rows.append([InlineKeyboardButton(text=f"{title[:30]} — {price}₽", callback_data=f"view_listing_{item['id']}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await ensure_session_message(user_id, "Результаты поиска:", kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]])
        await ensure_session_message(user_id, "Ничего не найдено.", kb)
    await state.clear()


# ---------------------------------------------------------------------------
# View listing handler
# ---------------------------------------------------------------------------

@router.callback_query(lambda cb: cb.data and cb.data.startswith("view_listing_"))
async def view_listing(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    listing_id = int(callback.data[len("view_listing_"):])
    listing = await get_listing(listing_id)
    if not listing or listing["status"] != "approved":
        await callback.answer("Лот не доступен.")
        return
    owner_id = listing["owner_id"]
    # Build message text
    desc = listing["description"] or ""
    text_lines = [f"<b>{listing['title']}</b>"]
    text_lines.append(f"Категория: {listing['category']}")
    text_lines.append(f"Состояние: {listing['condition']}")
    text_lines.append(f"Цена: {listing['price']}₽")
    # Show city and microdistrict for location
    if listing.get("city"):
        loc = listing["city"]
        if listing.get("microdistrict"):
            loc += ", " + listing["microdistrict"]
        text_lines.append(f"Локация: {loc}")
    text_lines.append(desc)
    # Determine if user has favourited this listing
    async with aiosqlite.connect(DB_FILENAME) as db:
        cur = await db.execute(
            "SELECT 1 FROM favourites WHERE user_id = ? AND listing_id = ?", (user_id, listing_id)
        )
        fav_row = await cur.fetchone()
        is_fav = bool(fav_row)
    # Buttons: favourite/unfavourite, report, message seller, back
    action_buttons = []
    if is_fav:
        action_buttons.append(InlineKeyboardButton(text="❌ Удалить из избранного", callback_data=f"unfav_{listing_id}"))
    else:
        action_buttons.append(InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_{listing_id}"))
    action_buttons.append(InlineKeyboardButton(text="🚩 Пожаловаться", callback_data=f"report_{listing_id}"))
    if user_id != owner_id:
        action_buttons.append(InlineKeyboardButton(text="✉️ Написать продавцу", callback_data=f"start_chat_{listing_id}_{owner_id}"))
    action_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main"))
    # Arrange buttons in rows of two
    rows = []
    temp = []
    for btn in action_buttons:
        temp.append(btn)
        if len(temp) == 2:
            rows.append(temp)
            temp = []
    if temp:
        rows.append(temp)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    # If listing has photos, send as media group
    photos = listing.get("photos", [])
    if photos:
        media = []
        for idx, file_id in enumerate(photos[:3]):
            caption = "\n".join(text_lines) if idx == 0 else None
            media.append(InputMediaPhoto(media=file_id, caption=caption, parse_mode=ParseMode.HTML))
        # Delete old session message if exists
        session = SESSIONS.get(user_id)
        if session:
            try:
                await bot.delete_message(user_id, session.message_id)
            except Exception:
                pass
        await bot.send_media_group(user_id, media=media)
        msg = await bot.send_message(user_id, "Дополнительные действия:", reply_markup=kb)
        SESSIONS[user_id] = Session(message_id=msg.message_id, current_menu="view_listing")
    else:
        await ensure_session_message(user_id, "\n".join(text_lines), kb)
    await callback.answer()


# Favourite / unfavourite handlers

@router.callback_query(lambda cb: cb.data and cb.data.startswith("fav_"))
async def favourite_listing(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    listing_id = int(callback.data[len("fav_"):])
    await add_favourite(user_id, listing_id)
    await callback.answer("Добавлено в избранное")
    # Refresh view
    await view_listing(callback, state)


@router.callback_query(lambda cb: cb.data and cb.data.startswith("unfav_"))
async def unfavourite_listing(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    listing_id = int(callback.data[len("unfav_"):])
    await remove_favourite(user_id, listing_id)
    await callback.answer("Удалено из избранного")
    await view_listing(callback, state)


# Report listing handler (placeholder)

@router.callback_query(lambda cb: cb.data and cb.data.startswith("report_"))
async def report_listing(callback: CallbackQuery) -> None:
    # Extract listing ID and reporter information
    listing_id_str = callback.data[len("report_"):]
    try:
        listing_id = int(listing_id_str)
    except ValueError:
        await callback.answer("Неверный формат данных.")
        return
    user = callback.from_user
    # Notify the reporter
    await callback.answer("Спасибо, ваша жалоба отправлена модератору.")
    # Forward details to admin
    if ADMIN_ID:
        listing = await get_listing(listing_id)
        if listing:
            owner_id = listing["owner_id"]
            title = listing["title"]
            txt = (
                f"Поступила жалоба на лот {listing_id} от пользователя {user.full_name} ({user.id}).\n"
                f"Название: {title}\n"
                f"Категория: {listing['category']}\n"
                f"Цена: {listing['price']}₽\n"
                f"Описание: {listing['description'] or ''}\n"
                f"Продавец: {owner_id}"
            )
            await bot.send_message(ADMIN_ID, txt)


# ---------------------------------------------------------------------------
# Listings menu and creation/editing
# ---------------------------------------------------------------------------

LISTINGS_PAGE_SIZE = 5

# Number of listings per page when showing editable listings
EDIT_PAGE_SIZE = 5


async def listings_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    # Show list of own listings with pagination and options
    listings = await list_user_listings(user_id, offset=0, limit=LISTINGS_PAGE_SIZE)
    text = "<b>Ваши объявления</b>"
    rows = []
    for item in listings:
        rows.append([InlineKeyboardButton(text=f"{item['title'][:25]}", callback_data=f"my_listing_{item['id']}")])
    # Add option to create a new listing
    rows.append([InlineKeyboardButton(text="➕ Создать новое", callback_data="create_listing")])
    # Add option to edit existing listings
    rows.append([InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit_listings")])
    # Back to main menu
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await ensure_session_message(user_id, text, kb)
    await state.clear()
    await callback.answer()


@router.callback_query(lambda cb: cb.data == "edit_listings")
async def edit_listings(callback: CallbackQuery, state: FSMContext) -> None:
    """Show the user's listings for editing."""
    user_id = callback.from_user.id
    listings = await list_user_listings(user_id, offset=0, limit=50)
    if not listings:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]]
        )
        await ensure_session_message(user_id, "У вас нет объявлений для редактирования.", kb)
        await state.clear()
        await callback.answer()
        return
    rows = []
    for item in listings:
        # Each button leads to selection of what to edit
        rows.append([
            InlineKeyboardButton(
                text=f"✏️ {item['title'][:25]}",
                callback_data=f"edit_listing_{item['id']}"
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await ensure_session_message(user_id, "Выберите объявление для редактирования:", kb)
    await state.clear()
    await callback.answer()


@router.callback_query(lambda cb: cb.data and cb.data.startswith("edit_listing_"))
async def edit_listing_select(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt the user to choose which field of the listing to edit."""
    user_id = callback.from_user.id
    listing_id = int(callback.data.split("_")[2])
    # Remember which listing is being edited
    await state.update_data(edit_listing_id=listing_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼️ Фото", callback_data=f"edit_field_photo_{listing_id}")],
            [InlineKeyboardButton(text="💰 Цена", callback_data=f"edit_field_price_{listing_id}")],
            [InlineKeyboardButton(text="📝 Описание", callback_data=f"edit_field_desc_{listing_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="edit_listings")],
        ]
    )
    await ensure_session_message(user_id, "Что вы хотите изменить?", kb)
    await state.clear()
    await callback.answer()


@router.callback_query(lambda cb: cb.data and cb.data.startswith("edit_field_photo_"))
async def edit_field_photo(callback: CallbackQuery, state: FSMContext) -> None:
    """Initiate photo editing for a listing."""
    user_id = callback.from_user.id
    listing_id = int(callback.data.split("_")[3])
    await state.update_data(edit_listing_id=listing_id, edit_photos=[])
    # Prompt user to send new photos or skip
    await bot.send_message(
        user_id,
        "Отправьте новые фото (до 3). Введите 'Пропустить', чтобы оставить фото без изменений.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(EditListing.editing_photos)
    await callback.answer()


@router.callback_query(lambda cb: cb.data and cb.data.startswith("edit_field_price_"))
async def edit_field_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Initiate price editing for a listing."""
    user_id = callback.from_user.id
    listing_id = int(callback.data.split("_")[3])
    await state.update_data(edit_listing_id=listing_id)
    await bot.send_message(user_id, "Введите новую цену (в рублях):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(EditListing.editing_price)
    await callback.answer()


@router.callback_query(lambda cb: cb.data and cb.data.startswith("edit_field_desc_"))
async def edit_field_desc(callback: CallbackQuery, state: FSMContext) -> None:
    """Initiate editing of category and description for a listing."""
    user_id = callback.from_user.id
    listing_id = int(callback.data.split("_")[3])
    await state.update_data(edit_listing_id=listing_id)
    # Ask for new category
    reply_buttons = [[KeyboardButton(text=cat)] for cat in CATEGORIES]
    kb = ReplyKeyboardMarkup(keyboard=reply_buttons, resize_keyboard=True, one_time_keyboard=True)
    await bot.send_message(user_id, "Выберите новую категорию:", reply_markup=kb)
    await state.set_state(EditListing.editing_category)
    await callback.answer()


@router.message(StateFilter(EditListing.editing_photos))
async def handle_edit_photos(message: Message, state: FSMContext) -> None:
    """Handle new photos during editing."""
    user_id = message.from_user.id
    data = await state.get_data()
    photos: List[str] = data.get("edit_photos", [])
    if message.text and message.text.lower().startswith("проп"):
        # Skip photo replacement
        listing_id = data.get("edit_listing_id")
        # Do not modify photos
        await finish_editing(listing_id, user_id, state)
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        if len(photos) < 3:
            photos.append(file_id)
            await state.update_data(edit_photos=photos)
            if len(photos) < 3:
                await message.answer("Фото добавлено. Добавьте ещё фото или отправьте 'Пропустить'.")
                return
        # If reached 3 photos or user wants to stop
        listing_id = data.get("edit_listing_id")
        # Replace existing photos with new ones
        await clear_listing_photos(listing_id)
        for pid in photos:
            await add_photo(listing_id, pid)
        await finish_editing(listing_id, user_id, state)
        return
    else:
        await message.answer("Пожалуйста, отправьте фото или 'Пропустить'.")


@router.message(StateFilter(EditListing.editing_price))
async def handle_edit_price(message: Message, state: FSMContext) -> None:
    """Handle new price input."""
    user_id = message.from_user.id
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    text = (message.text or "")
    try:
        price = int(re.sub(r"\D", "", text))
    except Exception:
        price = 0
    await update_listing_field(listing_id, "price", price)
    await finish_editing(listing_id, user_id, state)


@router.message(StateFilter(EditListing.editing_category))
async def handle_edit_category(message: Message, state: FSMContext) -> None:
    """Handle category selection for editing description."""
    user_id = message.from_user.id
    category = (message.text or "").strip()
    matches = [cat for cat in CATEGORIES if category.lower() in cat.lower()]
    if not matches:
        await message.answer("Категория не найдена. Попробуйте ещё раз.")
        return
    category = matches[0]
    await state.update_data(edit_new_category=category)
    await message.answer("Введите новое описание (до 150 символов):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(EditListing.editing_description)


@router.message(StateFilter(EditListing.editing_description))
async def handle_edit_description(message: Message, state: FSMContext) -> None:
    """Handle new description input."""
    user_id = message.from_user.id
    desc = (message.text or "").strip()[:150]
    data = await state.get_data()
    listing_id = data.get("edit_listing_id")
    category = data.get("edit_new_category")
    # Update listing category and description
    await update_listing_field(listing_id, "category", category)
    await update_listing_field(listing_id, "description", desc)
    await finish_editing(listing_id, user_id, state)


async def finish_editing(listing_id: int, user_id: int, state: FSMContext) -> None:
    """Finalize editing: mark listing pending and notify user/admin."""
    # Set status to pending for moderation
    await update_listing_status(listing_id, "pending")
    # Notify user
    await bot.send_message(user_id, "Лот обновлён и отправлен на модерацию.")
    await state.clear()
    # Notify admin
    if ADMIN_ID:
        listing = await get_listing(listing_id)
        if listing:
            txt = (
                f"Обновление лота на модерацию:\nID лота: {listing_id}\n"
                f"Продавец: {user_id}\n"
                f"Название: {listing['title']}\n"
                f"Категория: {listing['category']}\n"
                f"Цена: {listing['price']}₽"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Принять", callback_data=f"admin_accept_{listing_id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_deny_{listing_id}")]]
            )
            await bot.send_message(ADMIN_ID, txt, reply_markup=kb)
    # Return to main menu
    await show_main_menu(user_id)


@router.callback_query(lambda cb: cb.data == "create_listing")
async def create_listing_init(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    await state.clear()
    await state.set_state(NewListing.waiting_for_photo)
    await bot.send_message(user_id, "Загрузите фото товара (до 3). Отправьте 'Пропустить', чтобы продолжить без фото.")
    await callback.answer()


@router.message(StateFilter(NewListing.waiting_for_photo))
async def handle_listing_photo(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    data = await state.get_data()
    photos = data.get("photos", [])
    # If user sends 'Пропустить', move on without photos
    if message.text and message.text.lower().startswith("проп"):
        await state.update_data(photos=photos)
        # Show categories
        reply_buttons = [[KeyboardButton(text=cat)] for cat in CATEGORIES]
        kb = ReplyKeyboardMarkup(keyboard=reply_buttons, resize_keyboard=True, one_time_keyboard=True)
        await message.answer("Категория:", reply_markup=kb)
        await state.set_state(NewListing.waiting_for_category)
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        if len(photos) < 3:
            photos.append(file_id)
            await state.update_data(photos=photos)
            if len(photos) < 3:
                await message.answer("Фото добавлено. Вы можете добавить ещё фото или написать 'Пропустить'.")
            else:
                await message.answer("Вы достигли максимума (3 фото). Давайте продолжим.")
                reply_buttons = [[KeyboardButton(text=cat)] for cat in CATEGORIES]
                kb = ReplyKeyboardMarkup(keyboard=reply_buttons, resize_keyboard=True, one_time_keyboard=True)
                await message.answer("Категория:", reply_markup=kb)
                await state.set_state(NewListing.waiting_for_category)
        else:
            await message.answer("Максимум 3 фото. Введите 'Пропустить', чтобы продолжить.")
    else:
        await message.answer("Пожалуйста, отправьте фото или 'Пропустить'.")


@router.message(StateFilter(NewListing.waiting_for_category))
async def handle_listing_category(message: Message, state: FSMContext) -> None:
    user_cat = (message.text or "").strip()
    # Try to find a matching category
    matches = [cat for cat in CATEGORIES if user_cat.lower() in cat.lower()]
    if not matches:
        await message.answer("Категория не найдена. Попробуйте выбрать из списка или указать более точно.")
        return
    category = matches[0]
    await state.update_data(category=category)
    # Ask for condition
    reply_buttons = [[KeyboardButton(text=cond)] for cond in CONDITIONS]
    kb = ReplyKeyboardMarkup(keyboard=reply_buttons, resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Состояние товара:", reply_markup=kb)
    await state.set_state(NewListing.waiting_for_condition)


@router.message(StateFilter(NewListing.waiting_for_condition))
async def handle_listing_condition(message: Message, state: FSMContext) -> None:
    condition = (message.text or "").strip()
    if condition not in CONDITIONS:
        await message.answer("Пожалуйста, выберите состояние из предложенных.")
        return
    await state.update_data(condition=condition)
    await message.answer("Укажите цену (в рублях):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(NewListing.waiting_for_price)


@router.message(StateFilter(NewListing.waiting_for_price))
async def handle_listing_price(message: Message, state: FSMContext) -> None:
    text = (message.text or "")
    try:
        price = int(re.sub(r"\D", "", text))
    except Exception:
        price = 0
    await state.update_data(price=price)
    await message.answer("Введите название товара:")
    await state.set_state(NewListing.waiting_for_title)


@router.message(StateFilter(NewListing.waiting_for_title))
async def handle_listing_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:80]
    await state.update_data(title=title)
    await message.answer("Введите описание (до 150 символов):")
    await state.set_state(NewListing.waiting_for_description)


@router.message(StateFilter(NewListing.waiting_for_description))
async def handle_listing_description(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()[:150]
    data = await state.get_data()
    user_id = message.from_user.id
    # Save listing to DB with status pending
    user_info = await get_user_info(user_id)
    city = user_info["city"]
    microdistrict = user_info.get("microdistrict")
    listing_id = await add_listing(
        owner_id=user_id,
        city=city,
        microdistrict=microdistrict,
        category=data.get("category"),
        condition=data.get("condition"),
        title=data.get("title"),
        description=desc,
        price=data.get("price", 0),
    )
    # Save photos
    photos = data.get("photos", [])
    for file_id in photos:
        await add_photo(listing_id, file_id)
    await state.clear()
    await message.answer("Лот создан и отправлен на модерацию.")
    # Notify admin about new listing
    if ADMIN_ID:
        owner_name = message.from_user.full_name
        txt = (
            f"Новая заявка на модерацию:\nID лота: {listing_id}\n"
            f"Продавец: {owner_name} ({user_id})\n"
            f"Название: {data.get('title')}\n"
            f"Категория: {data.get('category')}\n"
            f"Цена: {data.get('price')}₽"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Принять", callback_data=f"admin_accept_{listing_id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_deny_{listing_id}")]]
        )
        await bot.send_message(ADMIN_ID, txt, reply_markup=kb)
    # Return to main menu
    await show_main_menu(user_id)


# Edit listing process is omitted for brevity.  In a production implementation
# you would follow a similar pattern to NewListing but load existing values
# and update fields.  After editing, set status back to 'pending' and
# notify the admin.


# ---------------------------------------------------------------------------
# Chat and messaging handlers
# ---------------------------------------------------------------------------

@router.callback_query(lambda cb: cb.data and cb.data.startswith("start_chat_"))
async def start_chat(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    # callback.data has the form 'start_chat_{listing_id}_{owner_id}'.  Split
    # off the prefix and then separate listing and owner IDs.  We cannot
    # simply split on '_' because chat prefixes include multiple underscores.
    data = callback.data[len("start_chat_"):]
    # data should now be '{listing_id}_{owner_id}'
    listing_id_str, owner_id_str = data.split("_", 1)
    try:
        listing_id = int(listing_id_str)
        owner_id = int(owner_id_str)
    except ValueError:
        await callback.answer("Неверный формат данных.")
        return
    if user_id == owner_id:
        await callback.answer("Это ваше объявление.")
        return
    # Determine chat ID
    chat_id = chat_id_from_users(user_id, owner_id, listing_id)
    await ensure_chat(chat_id, user_id, owner_id, listing_id)
    # Show chat window: display last 5 messages
    msgs = await fetch_messages(chat_id, offset=0, limit=5)
    lines: List[str] = []
    for m in msgs:
        sender_label = "Вы" if m["sender_id"] == user_id else "Собеседник"
        lines.append(f"<b>{sender_label}:</b> {m['text']}")
    chat_text = "\n".join(lines) or "Начните беседу..."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить ещё", callback_data=f"load_more_{chat_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await ensure_session_message(user_id, chat_text, kb)
    # Save chat_id in state
    await state.update_data(active_chat=chat_id, partner_id=owner_id, listing_id=listing_id, offset=5)
    await state.set_state(ChatStates.chatting)
    await callback.answer()


@router.callback_query(StateFilter(ChatStates.chatting), F.data.startswith("load_more_"))
async def load_more_messages(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    data = await state.get_data()
    chat_id = data.get("active_chat")
    offset = data.get("offset", 0)
    new_offset = offset + 5
    msgs = await fetch_messages(chat_id, offset=offset, limit=5)
    lines: List[str] = []
    for m in msgs + await fetch_messages(chat_id, offset=0, limit=offset, reverse=True):
        sender_label = "Вы" if m["sender_id"] == user_id else "Собеседник"
        lines.append(f"<b>{sender_label}:</b> {m['text']}")
    chat_text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить ещё", callback_data=f"load_more_{chat_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await ensure_session_message(user_id, chat_text, kb)
    await state.update_data(offset=new_offset)
    await callback.answer()


@router.message(StateFilter(ChatStates.chatting))
async def handle_chat_message(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return
    data = await state.get_data()
    chat_id = data.get("active_chat")
    partner_id = data.get("partner_id")
    listing_id = data.get("listing_id")
    # Store message
    await store_message(chat_id, sender_id=user_id, receiver_id=partner_id, listing_id=listing_id, text=text)
    # Forward to partner if not muted
    if not await is_user_muted(partner_id):
        await bot.send_message(partner_id, "Новое сообщение от пользователя. Чтобы ответить, найдите чат в разделе 'Чаты'.")
    # Refresh chat window for sender
    msgs = await fetch_messages(chat_id, offset=0, limit=5)
    lines = []
    for m in msgs:
        sender_label = "Вы" if m["sender_id"] == user_id else "Собеседник"
        lines.append(f"<b>{sender_label}:</b> {m['text']}")
    chat_text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить ещё", callback_data=f"load_more_{chat_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await ensure_session_message(user_id, chat_text, kb)


async def chats_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    chats = await list_user_chats(user_id, offset=0, limit=LISTINGS_PAGE_SIZE)
    rows = []
    if not chats:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await ensure_session_message(user_id, "У вас нет чатов.", kb)
        await callback.answer()
        return
    for ch in chats:
        chat_id = ch["chat_id"]
        partner_id = ch["partner_id"]
        listing_id = ch["listing_id"]
        rows.append([InlineKeyboardButton(text=f"Чат по лоту {listing_id}", callback_data=f"open_chat_{chat_id}_{partner_id}_{listing_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await ensure_session_message(user_id, "Ваши чаты:", kb)
    await state.clear()
    await callback.answer()


@router.callback_query(lambda cb: cb.data and cb.data.startswith("open_chat_"))
async def open_chat(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    # callback.data has the form 'open_chat_{chat_id}_{partner_id}_{listing_id}'.
    # chat_id itself contains underscores (user1_user2_listing_id).  Extract the
    # partner_id and listing_id from the end and join the remaining parts back
    # into chat_id.
    data = callback.data[len("open_chat_"):]
    parts = data.split("_")
    if len(parts) < 3:
        await callback.answer("Неверный формат данных.")
        return
    # The last two elements are partner_id and listing_id
    partner_id_str = parts[-2]
    listing_id_str = parts[-1]
    chat_id = "_".join(parts[:-2])
    try:
        partner_id = int(partner_id_str)
        listing_id = int(listing_id_str)
    except ValueError:
        await callback.answer("Неверный формат данных.")
        return
    # Show last 5 messages
    msgs = await fetch_messages(chat_id, offset=0, limit=5)
    lines = []
    for m in msgs:
        sender_label = "Вы" if m["sender_id"] == user_id else "Собеседник"
        lines.append(f"<b>{sender_label}:</b> {m['text']}")
    chat_text = "\n".join(lines) or "Начните беседу..."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Загрузить ещё", callback_data=f"load_more_{chat_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
        ]
    )
    await ensure_session_message(user_id, chat_text, kb)
    await state.update_data(active_chat=chat_id, partner_id=partner_id, listing_id=listing_id, offset=5)
    await state.set_state(ChatStates.chatting)
    await callback.answer()


# ---------------------------------------------------------------------------
# Favourites menu
# ---------------------------------------------------------------------------

async def favourites_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id
    favs = await list_favourites(user_id, offset=0, limit=LISTINGS_PAGE_SIZE)
    rows = []
    if not favs:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await ensure_session_message(user_id, "У вас нет избранных лотов.", kb)
        await callback.answer()
        return
    for item in favs:
        rows.append([InlineKeyboardButton(text=f"{item['title'][:25]} — {item['price']}₽", callback_data=f"view_listing_{item['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await ensure_session_message(user_id, "Избранные лоты:", kb)
    await state.clear()
    await callback.answer()


# ---------------------------------------------------------------------------
# Admin moderation handlers
# ---------------------------------------------------------------------------

@router.callback_query(lambda cb: cb.data and cb.data.startswith("admin_accept_"))
async def admin_accept(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if user_id != ADMIN_ID:
        await callback.answer("Недостаточно прав")
        return
    listing_id = int(callback.data[len("admin_accept_"):])
    await update_listing_status(listing_id, "approved")
    await callback.answer("Лот одобрен.")
    # Notify seller
    listing = await get_listing(listing_id)
    if listing:
        await bot.send_message(listing["owner_id"], f"Ваш лот '{listing['title']}' принят и опубликован!")


@router.callback_query(lambda cb: cb.data and cb.data.startswith("admin_deny_"))
async def admin_deny(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    if user_id != ADMIN_ID:
        await callback.answer("Недостаточно прав")
        return
    listing_id = int(callback.data[len("admin_deny_"):])
    await update_listing_status(listing_id, "denied")
    await callback.answer("Лот отклонён.")
    listing = await get_listing(listing_id)
    if listing:
        await bot.send_message(listing["owner_id"], f"Ваш лот '{listing['title']}' отклонён и удалён. Пожалуйста, проверьте правила размещения.")


@router.message(Command("moderate"))
async def cmd_moderate(message: Message) -> None:
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        await message.answer("Недостаточно прав для модерации.")
        return
    pending = await list_pending_listings(offset=0, limit=10)
    if not pending:
        await message.answer("Нет лотов на модерации.")
        return
    for item in pending:
        text = (
            f"ID: {item['id']}\n"
            f"Продавец: {item['owner_id']}\n"
            f"Название: {item['title']}\n"
            f"Категория: {item['category']}\n"
            f"Цена: {item['price']}₽"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Принять", callback_data=f"admin_accept_{item['id']}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_deny_{item['id']}")]]
        )
        await message.answer(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# User mute/unmute commands
# ---------------------------------------------------------------------------

@router.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    user_id = message.from_user.id
    await update_user_mute(user_id, True)
    await message.answer("Уведомления от бота выключены. Используйте /unmute, чтобы включить.")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    user_id = message.from_user.id
    await update_user_mute(user_id, False)
    await message.answer("Уведомления от бота включены.")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    await init_db()
    logging.info("Bot is starting...")
    # Start polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())