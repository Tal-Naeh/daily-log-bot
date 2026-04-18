#!/usr/bin/env python3
import asyncio
import logging
import os
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from notion_client import AsyncClient as NotionClient
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, ConversationHandler

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB = os.environ["NOTION_DATABASE_ID"]
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "18"))
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jerusalem")

DONE, TOMORROW = range(2)

notion = NotionClient(auth=NOTION_TOKEN)


async def save_to_notion(today: str, done: str, tomorrow: str):
    await notion.pages.create(
        parent={"database_id": NOTION_DB},
        properties={
            "Name": {"title": [{"text": {"content": today}}]},
            "Done": {"rich_text": [{"text": {"content": done}}]},
            "Tomorrow": {"rich_text": [{"text": {"content": tomorrow}}]},
            "Date": {"date": {"start": today}},
        },
    )


async def get_yesterdays_plan() -> str | None:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    res = await notion.databases.query(
        database_id=NOTION_DB,
        filter={"property": "Date", "date": {"equals": yesterday}},
    )
    results = res.get("results", [])
    if not results:
        return None
    props = results[0]["properties"]
    tomorrow = props.get("Tomorrow", {}).get("rich_text", [])
    return tomorrow[0]["text"]["content"] if tomorrow else None


async def send_evening_prompt(app: Application):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="🌙 *סוף יום!*\n\nשלח /log כדי לתעד את היום",
        parse_mode="Markdown",
    )


async def send_morning_reminder(app: Application):
    plan = await get_yesterdays_plan()
    if plan:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"☀️ *בוקר טוב!*\n\nהתוכנית שלך להיום:\n_{plan}_",
            parse_mode="Markdown",
        )
    else:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="☀️ *בוקר טוב!*\n\nאין תוכנית מאתמול — שלח /log בסוף היום כדי לתכנן מחר.",
            parse_mode="Markdown",
        )


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END
    await update.message.reply_text("מה עשית היום?")
    return DONE


async def received_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END
    context.user_data["done"] = update.message.text
    await update.message.reply_text("מה אתה מתכנן למחר?")
    return TOMORROW


async def received_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END
    done = context.user_data.get("done", "")
    tomorrow = update.message.text
    today = date.today().isoformat()
    try:
        await save_to_notion(today, done, tomorrow)
        await update.message.reply_text("✅ נשמר ב-Notion! לילה טוב 🌙")
    except Exception as e:
        log.error("Notion save failed: %s", e)
        await update.message.reply_text(f"❌ שגיאה בשמירה ל-Notion: {e}")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("בסדר, מבוטל.")
    return ConversationHandler.END


async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        send_evening_prompt,
        CronTrigger(hour=EVENING_HOUR, minute=0, timezone=TIMEZONE),
        args=[app],
        id="evening",
    )
    scheduler.add_job(
        send_morning_reminder,
        CronTrigger(hour=MORNING_HOUR, minute=0, timezone=TIMEZONE),
        args=[app],
        id="morning",
    )
    scheduler.start()
    log.info("Scheduler started. Evening at %d:00, morning at %d:00 (%s)", EVENING_HOUR, MORNING_HOUR, TIMEZONE)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("log", cmd_log)],
        states={
            DONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_done)],
            TOMORROW: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_tomorrow)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
