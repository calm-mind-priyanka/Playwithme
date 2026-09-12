# Cyber Arcade Hub

Telegram WebApp mini-game bot with three games: Cyber Void, Data Drift and Core Breaker.

## Koyeb deployment

Set these environment variables/secrets in Koyeb:

- `TELEGRAM_BOT_TOKEN` — your NEW Telegram bot token
- `MONGO_URI` — your MongoDB connection string
- `WEBAPP_URL` — the exact HTTPS URL of this Koyeb service, for example `https://your-app.koyeb.app`
- `MONGO_DB` — optional, defaults to `arcade_hub`
- `GAME_SESSION_MINUTES` — optional, defaults to `20`
- `INIT_DATA_MAX_AGE` — optional, defaults to `86400`
- `LOG_LEVEL` — optional, defaults to `INFO`

The service listens on Koyeb's `PORT` environment variable. `Procfile` starts `python bot.py`, which runs the Flask API and Telegram polling bot in the same lightweight instance.

## Telegram BotFather

Use the bot's normal `/start` command. The bot displays callback-driven menus and WebApp launch buttons.

The Web Apps must be opened from Telegram because the server verifies Telegram `initData`.

## Security notes

- Never commit `.env`, Telegram tokens, or MongoDB credentials.
- Rotate any credentials that were previously exposed.
- Scores are tied to verified Telegram users and short-lived game sessions.
- The server applies basic score plausibility limits, but a browser game can never be made completely cheat-proof without moving game simulation/score calculation to a trusted server.
