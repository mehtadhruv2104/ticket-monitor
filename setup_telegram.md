# Telegram Bot Setup

## 1. Create a Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "BMS Ticket Alert")
4. Choose a username (e.g., `bms_ticket_alert_bot`)
5. Copy the **HTTP API token** — this is your `TELEGRAM_BOT_TOKEN`

## 2. Get Your Chat ID

1. Open your new bot in Telegram and send it any message (e.g., "hello")
2. Open this URL in your browser (replace `<TOKEN>` with your bot token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. Look for `"chat":{"id":123456789}` in the response — that number is your `TELEGRAM_CHAT_ID`

## 3. Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:
```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789
```

## 4. Test

Run the monitor — it sends a test message on startup:

```bash
python monitor.py
```

You should receive a "Monitor started" message in Telegram.
