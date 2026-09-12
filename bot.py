import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from flask import Flask, jsonify, request, send_from_directory
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cyber_arcade")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
SESSION_MINUTES = max(1, int(os.environ.get("GAME_SESSION_MINUTES", "20")))
INIT_DATA_MAX_AGE = max(60, int(os.environ.get("INIT_DATA_MAX_AGE", "86400")))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
if not MONGO_URI:
    raise RuntimeError("Missing MONGO_URI environment variable")
if not WEBAPP_URL.startswith("https://"):
    raise RuntimeError("WEBAPP_URL must be the real HTTPS Koyeb URL")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000)
db = mongo_client[os.getenv("MONGO_DB", "arcade_hub")]
users = db["users"]
scores = db["scores"]
sessions = db["game_sessions"]

# Indexes are idempotent and make leaderboard/profile queries fast.
users.create_index("telegram_id", unique=True)
scores.create_index([("game", ASCENDING), ("score", DESCENDING)])
scores.create_index([("telegram_id", ASCENDING), ("game", ASCENDING)], unique=True)
sessions.create_index("session_id", unique=True)
sessions.create_index("expires_at", expireAfterSeconds=0)
sessions.create_index([("telegram_id", ASCENDING), ("game", ASCENDING), ("started_at", DESCENDING)])

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

GAMES = {
    "shooter": {"title": "🚀 Cyber Void", "path": "/shooter/", "score_rate": 1200},
    "racing": {"title": "🏎️ Data Drift", "path": "/racing/", "score_rate": 90},
    "fighting": {"title": "🥊 Core Breaker", "path": "/fighting/", "score_rate": 3},
}


def game_url(game: str) -> str:
    return f"{WEBAPP_URL}{GAMES[game]['path']}"


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 PLAY GAMES", callback_data="menu:games")],
        [InlineKeyboardButton(text="🏆 LEADERBOARD", callback_data="menu:leaderboard")],
        [InlineKeyboardButton(text="👤 MY PROFILE", callback_data="menu:profile")],
        [InlineKeyboardButton(text="ℹ️ HOW TO PLAY", callback_data="menu:help")],
    ])


def games_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Cyber Void", web_app=WebAppInfo(url=game_url("shooter")))],
        [InlineKeyboardButton(text="🏎️ Data Drift", web_app=WebAppInfo(url=game_url("racing")))],
        [InlineKeyboardButton(text="🥊 Core Breaker", web_app=WebAppInfo(url=game_url("fighting")))],
        [InlineKeyboardButton(text="🏆 Leaderboard", callback_data="menu:leaderboard")],
        [InlineKeyboardButton(text="👤 My Profile", callback_data="menu:profile")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="menu:home")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="menu:home")]])


def leaderboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Shooter", callback_data="lb:shooter"),
            InlineKeyboardButton(text="🏎️ Racing", callback_data="lb:racing"),
        ],
        [InlineKeyboardButton(text="🥊 Fighting", callback_data="lb:fighting")],
        [InlineKeyboardButton(text="🌐 Global", callback_data="lb:global")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="menu:home")],
    ])


def validate_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData using Telegram's documented HMAC scheme."""
    if not init_data or len(init_data) > 8192:
        raise ValueError("Missing or invalid Telegram initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash or len(received_hash) != 64:
        raise ValueError("Missing Telegram hash")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid Telegram initData signature")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise ValueError("Invalid Telegram auth date")
    if not auth_date or abs(time.time() - auth_date) > INIT_DATA_MAX_AGE:
        raise ValueError("Telegram initData expired")

    try:
        user = json.loads(pairs["user"])
    except (KeyError, json.JSONDecodeError):
        raise ValueError("Telegram user data missing")
    if not isinstance(user, dict) or not user.get("id"):
        raise ValueError("Telegram user id missing")
    return user


def upsert_user(user: dict) -> None:
    now = datetime.now(timezone.utc)
    users.update_one(
        {"telegram_id": int(user["id"])},
        {
            "$set": {
                "username": user.get("username"),
                "first_name": user.get("first_name", "Player"),
                "last_name": user.get("last_name"),
                "last_seen": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def get_auth_user(req):
    user = validate_init_data(req.headers.get("X-Telegram-Init-Data", ""))
    upsert_user(user)
    return user


def leaderboard_rows(game=None, limit=10):
    query = {"game": game} if game in GAMES else {}
    cursor = scores.find(query).sort("score", DESCENDING).limit(limit)
    rows = []
    for rank, entry in enumerate(cursor, 1):
        rows.append({
            "rank": rank,
            "name": entry.get("first_name") or entry.get("username") or "Player",
            "username": entry.get("username"),
            "game": entry.get("game"),
            "score": int(entry.get("score", 0)),
        })
    return rows


def global_leaderboard_rows(limit=10):
    pipeline = [
        {"$group": {
            "_id": "$telegram_id",
            "total_score": {"$sum": "$score"},
            "first_name": {"$first": "$first_name"},
            "username": {"$first": "$username"},
        }},
        {"$sort": {"total_score": -1}},
        {"$limit": limit},
    ]
    rows = []
    for rank, entry in enumerate(scores.aggregate(pipeline), 1):
        rows.append({
            "rank": rank,
            "name": entry.get("first_name") or entry.get("username") or "Player",
            "username": entry.get("username"),
            "score": int(entry.get("total_score", 0)),
        })
    return rows


def leaderboard_text(game=None):
    if game in GAMES:
        title = f"🏆 **{GAMES[game]['title']} LEADERBOARD**"
    else:
        title = "🏆 **GLOBAL LEADERBOARD**"
    if game in GAMES:
        rows = leaderboard_rows(game)
        if not rows:
            return title + "\n\nNo scores recorded yet. Be the first to play!"
        lines = [title, ""]
        for row in rows:
            lines.append(f"{row['rank']}. {row['name']} — **{row['score']}**")
        return "\n".join(lines)

    rows = global_leaderboard_rows()
    if not rows:
        return title + "\n\nNo scores recorded yet. Be the first to play!"
    lines = [title, "", "Total of each player's best score across all games:"]
    for row in rows:
        lines.append(f"{row['rank']}. {row['name']} — **{row['score']}**")
    return "\n".join(lines)


@app.get("/")
def home():
    return "Cyber Arcade Hub Backend is running live!"


@app.get("/health")
def health():
    try:
        mongo_client.admin.command("ping")
        return jsonify({"status": "ok", "database": "ok"})
    except Exception:
        log.exception("Health check failed")
        return jsonify({"status": "degraded", "database": "unavailable"}), 503


@app.get("/shooter/")
def shooter_game():
    return send_from_directory("static/shooter", "index.html")


@app.get("/racing/")
def racing_game():
    return send_from_directory("static/racing", "index.html")


@app.get("/fighting/")
def fighting_game():
    return send_from_directory("static/fighting", "index.html")


@app.post("/api/game/start")
def start_game():
    try:
        user = get_auth_user(request)
        data = request.get_json(silent=True) or {}
        game = data.get("game")
        if game not in GAMES:
            return jsonify({"status": "error", "message": "Unknown game"}), 400

        now = datetime.now(timezone.utc)
        session_id = secrets.token_urlsafe(24)
        expires = now + timedelta(minutes=SESSION_MINUTES)
        sessions.insert_one({
            "session_id": session_id,
            "telegram_id": int(user["id"]),
            "game": game,
            "started_at": now,
            "expires_at": expires,
            "submitted": False,
        })
        return jsonify({"status": "success", "session_id": session_id, "expires_at": expires.isoformat()})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 401
    except Exception:
        log.exception("Failed to start game")
        return jsonify({"status": "error", "message": "Unable to start game"}), 500


@app.post("/api/save-score")
def save_score():
    try:
        user = get_auth_user(request)
        data = request.get_json(silent=True) or {}
        session_id = str(data.get("session_id") or "")
        game = data.get("game")
        score_value = data.get("score")

        if not session_id or len(session_id) > 128 or game not in GAMES:
            return jsonify({"status": "error", "message": "Invalid game session"}), 400
        try:
            score_value = int(score_value)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Score must be an integer"}), 400
        if score_value < 0 or score_value > 10_000_000:
            return jsonify({"status": "error", "message": "Score outside allowed range"}), 400

        telegram_id = int(user["id"])
        session = sessions.find_one({
            "session_id": session_id,
            "telegram_id": telegram_id,
            "game": game,
            "submitted": False,
        })
        if not session:
            return jsonify({"status": "error", "message": "Invalid or already-used session"}), 403

        now = datetime.now(timezone.utc)
        if session["expires_at"] < now:
            return jsonify({"status": "error", "message": "Game session expired"}), 403

        elapsed = max(1.0, (now - session["started_at"]).total_seconds())
        # Plausibility guard. This does not make client-side games cheat-proof,
        # but it blocks absurd score injections that are impossible in a normal session.
        max_plausible = int(elapsed * GAMES[game]["score_rate"] + 500)
        if score_value > max_plausible:
            return jsonify({"status": "error", "message": "Score failed validation"}), 422

        # Atomic claim: only one request can consume a session.
        claimed = sessions.find_one_and_update(
            {"_id": session["_id"], "submitted": False},
            {"$set": {"submitted": True, "submitted_at": now}},
        )
        if not claimed:
            return jsonify({"status": "error", "message": "Session already submitted"}), 409

        current = scores.find_one({"telegram_id": telegram_id, "game": game})
        old_score = int(current.get("score", 0)) if current else -1
        new_high = score_value > old_score

        if new_high:
            scores.update_one(
                {"telegram_id": telegram_id, "game": game},
                {
                    "$set": {
                        "score": score_value,
                        "username": user.get("username"),
                        "first_name": user.get("first_name", "Player"),
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "telegram_id": telegram_id,
                        "game": game,
                        "created_at": now,
                    },
                },
                upsert=True,
            )

        return jsonify({"status": "success", "new_high": new_high, "score": score_value})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 401
    except Exception:
        log.exception("Failed to save score")
        return jsonify({"status": "error", "message": "Unable to save score"}), 500


@app.get("/api/leaderboard")
def api_leaderboard():
    game = request.args.get("game")
    if game and game not in GAMES:
        return jsonify({"status": "error", "message": "Unknown game"}), 400
    rows = leaderboard_rows(game) if game else global_leaderboard_rows()
    return jsonify({"status": "success", "leaderboard": rows})


@app.get("/api/profile")
def api_profile():
    try:
        user = get_auth_user(request)
        telegram_id = int(user["id"])
        best = list(scores.find({"telegram_id": telegram_id}).sort("score", DESCENDING))
        return jsonify({
            "status": "success",
            "profile": {
                "telegram_id": telegram_id,
                "first_name": user.get("first_name", "Player"),
                "username": user.get("username"),
                "scores": [
                    {"game": row["game"], "score": int(row.get("score", 0))}
                    for row in best
                ],
            },
        })
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 401
    except Exception:
        log.exception("Failed to load profile")
        return jsonify({"status": "error", "message": "Unable to load profile"}), 500


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "⚡ **CYBER ARCADE HUB** ⚡\n\n"
        "Welcome, Agent. Choose an option below.\n\n"
        "🎮 Play games • 🏆 compete • 👤 track your best scores",
        reply_markup=main_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(Command("leaderboard"))
async def cmd_leaderboard(message: types.Message):
    await message.answer(leaderboard_text(), reply_markup=leaderboard_keyboard(), parse_mode="Markdown")


@dp.callback_query()
async def callbacks(callback: CallbackQuery):
    data = callback.data or ""
    await callback.answer()

    if not callback.message:
        return

    try:
        if data == "menu:home":
            await callback.message.edit_text(
                "⚡ **CYBER ARCADE HUB** ⚡\n\nChoose an option below.",
                reply_markup=main_keyboard(),
                parse_mode="Markdown",
            )
        elif data == "menu:games":
            await callback.message.edit_text(
                "🎮 **SELECT YOUR ARENA**\n\nChoose a game to launch as a Telegram Web App.",
                reply_markup=games_keyboard(),
                parse_mode="Markdown",
            )
        elif data == "menu:leaderboard":
            await callback.message.edit_text(
                leaderboard_text(), reply_markup=leaderboard_keyboard(), parse_mode="Markdown"
            )
        elif data.startswith("lb:"):
            selected = data.split(":", 1)[1]
            selected = None if selected == "global" else selected
            await callback.message.edit_text(
                leaderboard_text(selected), reply_markup=leaderboard_keyboard(), parse_mode="Markdown"
            )
        elif data == "menu:profile":
            telegram_id = callback.from_user.id
            user_scores = list(scores.find({"telegram_id": telegram_id}).sort("score", DESCENDING))
            if not user_scores:
                text = "👤 **MY PROFILE**\n\nYou have not recorded a score yet. Pick a game and start playing!"
            else:
                lines = [
                    "👤 **MY PROFILE**",
                    "",
                    f"Player: **{callback.from_user.first_name}**",
                    "",
                ]
                for entry in user_scores:
                    title = GAMES.get(entry.get("game"), {}).get("title", entry.get("game", "Unknown"))
                    lines.append(f"{title}: **{entry.get('score', 0)}**")
                text = "\n".join(lines)
            await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
        elif data == "menu:help":
            await callback.message.edit_text(
                "ℹ️ **HOW TO PLAY**\n\n"
                "1. Open a game from Play Games.\n"
                "2. The server verifies your Telegram identity.\n"
                "3. A short-lived game session is created.\n"
                "4. Your best score per game is saved.\n\n"
                "Never share your Telegram Web App init data or bot credentials.",
                reply_markup=back_keyboard(),
                parse_mode="Markdown",
            )
    except Exception:
        log.exception("Callback failed: %s", data)


def run_web():
    log.info("Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


async def run_bot():
    log.info("Starting Telegram polling")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True, name="flask-web").start()
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down")
