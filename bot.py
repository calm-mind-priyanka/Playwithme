import os
import logging
from flask import Flask, send_from_directory, request, jsonify
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO)

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8071698760:AAFAqbIQM4gqbscPzDbLeBhYG69XzkESsmg"
MONGO_URI = "mongodb+srv://Vivekchaun24_23:SPNaSnJhXQ39ZMtg@cluster0.74vyoq2.mongodb.net/?appName=Cluster0"
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-koyeb-app-name.koyeb.app")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["arcade_hub"]
scores_collection = db["scores"]

app = Flask(__name__, static_folder="static")

@app.route("/")
def home():
    return "Cyber Arcade Hub Backend is running live!"

@app.route("/shooter/")
def shooter_game():
    return send_from_directory("static/shooter", "index.html")

@app.route("/racing/")
def racing_game():
    return send_from_directory("static/racing", "index.html")

@app.route("/fighting/")
def fighting_game():
    return send_from_directory("static/fighting", "index.html")

@app.route("/api/save-score", methods=["POST"])
def save_score():
    data = request.json
    user_id = data.get("user_id")
    game_name = data.get("game")
    score = data.get("score")

    if not user_id or not game_name or score is None:
        return jsonify({"status": "error", "message": "Invalid data"}), 400

    existing = scores_collection.find_one({"user_id": user_id, "game": game_name})
    if not existing or score > existing["score"]:
        scores_collection.update_one(
            {"user_id": user_id, "game": game_name},
            {"$set": {"score": score}},
            upsert=True
        )
        return jsonify({"status": "success", "new_high": True})
    
    return jsonify({"status": "success", "new_high": False})

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Play Cyber Void (Shooter)", web_app=WebAppInfo(url=f"{WEBAPP_URL}/shooter/"))],
        [InlineKeyboardButton(text="🏎️ Play Data Drift (Racing)", web_app=WebAppInfo(url=f"{WEBAPP_URL}/racing/"))],
        [InlineKeyboardButton(text="🥊 Play Core Breaker (Fighter)", web_app=WebAppInfo(url=f"{WEBAPP_URL}/fighting/"))]
    ])
    
    welcome_text = (
        "⚡ **CYBER ARCADE HUB** ⚡\n\n"
        "Welcome, Agent. Select an arena below to begin your mission, "
        "set high scores, and compete globally against other operatives!"
    )
    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(Command("leaderboard"))
async def cmd_leaderboard(message: types.Message):
    top_scores = scores_collection.find().sort("score", -1).limit(5)
    
    lb_text = "🏆 **GLOBAL CYBER LEADERBOARD** 🏆\n\n"
    count = 1
    for entry in top_scores:
        lb_text += f"{count}. Game: **{entry.get('game').capitalize()}** | Score: **{entry.get('score')}**\n"
        count += 1
        
    if count == 1:
        lb_text += "No scores recorded yet. Be the first to play!"
        
    await message.answer(lb_text, parse_mode="Markdown")

if __name__ == "__main__":
    import threading
    port = int(os.environ.get("PORT", 8000))
    flask_thread = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port))
    flask_thread.daemon = True
    flask_thread.start()
    
    import asyncio
    async def main():
        await dp.start_polling(bot)
    
    asyncio.run(main())
