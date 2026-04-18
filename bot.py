#!/usr/bin/env python3
import asyncio
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from notion_client import AsyncClient as NotionClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB = os.environ["NOTION_DATABASE_ID"]
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "18"))
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "21"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Jerusalem")

DONE, TOMORROW = range(2)

notion = NotionClient(auth=NOTION_TOKEN)

# ── Topics ───────────────────────────────────────────────────────────────────

TOPICS_FILE = Path(__file__).parent / "topics.json"
TOPICS_STATE = Path(__file__).parent / "topics_state.json"

def get_todays_topic() -> dict:
    topics = json.loads(TOPICS_FILE.read_text())
    if TOPICS_STATE.exists():
        state = json.loads(TOPICS_STATE.read_text())
        last_date = state.get("last_date")
        last_index = state.get("last_index", -1)
    else:
        last_date, last_index = None, -1

    today_str = date.today().isoformat()
    if last_date == today_str:
        # Already sent today — return same topic
        index = last_index
    else:
        index = (last_index + 1) % len(topics)
        TOPICS_STATE.write_text(json.dumps({"last_date": today_str, "last_index": index}))

    return topics[index]


# ── Notion helpers ──────────────────────────────────────────────────────────

async def get_entry(day: date) -> dict | None:
    res = await notion.databases.query(
        database_id=NOTION_DB,
        filter={"property": "Date", "date": {"equals": day.isoformat()}},
    )
    results = res.get("results", [])
    return results[0] if results else None


async def get_entries_range(start: date, end: date) -> list[dict]:
    res = await notion.databases.query(
        database_id=NOTION_DB,
        filter={"and": [
            {"property": "Date", "date": {"on_or_after": start.isoformat()}},
            {"property": "Date", "date": {"on_or_before": end.isoformat()}},
        ]},
        sorts=[{"property": "Date", "direction": "ascending"}],
    )
    return res.get("results", [])


def extract_text(entry: dict, field: str) -> str:
    parts = entry["properties"].get(field, {}).get("rich_text", [])
    return parts[0]["text"]["content"] if parts else ""


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


async def logged_today() -> bool:
    return await get_entry(date.today()) is not None


# ── Scheduled messages ───────────────────────────────────────────────────────

async def send_evening_prompt(app: Application):
    keyboard = [[InlineKeyboardButton("📝 תעד את היום", callback_data="start_log")]]
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="🌙 *סוף יום!* בוא נתעד מה קרה היום.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_morning_reminder(app: Application):
    yesterday = date.today() - timedelta(days=1)
    entry = await get_entry(yesterday)
    plan = extract_text(entry, "Tomorrow") if entry else None

    topic = get_todays_topic()

    parts = ["☀️ *בוקר טוב!*"]
    if plan:
        parts.append(f"\n📋 *התוכנית להיום:*\n_{plan}_")
    parts.append(f"\n\n🧠 *נושא היום — {topic['title']}*\n{topic['body']}")

    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="\n".join(parts),
        parse_mode="Markdown",
    )


async def send_late_reminder(app: Application):
    if not await logged_today():
        keyboard = [[InlineKeyboardButton("📝 תעד עכשיו", callback_data="start_log")]]
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="⏰ עוד לא תיעדת את היום — לוקח רק דקה!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ── Conversation: /log ────────────────────────────────────────────────────────

async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END
    await update.message.reply_text("מה עשית היום?")
    return DONE


async def inline_start_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("מה עשית היום?")
    return DONE


async def received_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END
    context.user_data["done"] = update.message.text
    keyboard = [[InlineKeyboardButton("⏭ דלג", callback_data="skip_tomorrow")]]
    await update.message.reply_text(
        "מה אתה מתכנן למחר?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TOMORROW


async def received_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END
    await _finish_log(update.message.reply_text, context, update.message.text)
    return ConversationHandler.END


async def skip_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _finish_log(query.message.reply_text, context, "—")
    return ConversationHandler.END


async def _finish_log(reply_fn, context, tomorrow_text: str):
    done = context.user_data.get("done", "")
    today = date.today().isoformat()
    try:
        await save_to_notion(today, done, tomorrow_text)
        await reply_fn("✅ נשמר ב-Notion! לילה טוב 🌙")
    except Exception as e:
        log.error("Notion save failed: %s", e)
        await reply_fn(f"❌ שגיאה בשמירה: {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("בסדר, מבוטל.")
    return ConversationHandler.END


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    entry = await get_entry(date.today() - timedelta(days=1))
    if entry:
        plan = extract_text(entry, "Tomorrow")
        if plan and plan != "—":
            await update.message.reply_text(f"📋 *התוכנית להיום:*\n_{plan}_", parse_mode="Markdown")
            return
    await update.message.reply_text("אין תוכנית מוגדרת להיום.")


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    today = date.today()
    start = today - timedelta(days=6)
    entries = await get_entries_range(start, today)

    if not entries:
        await update.message.reply_text("אין רשומות לשבוע האחרון.")
        return

    lines = ["📊 *סיכום 7 הימים האחרונים:*\n"]
    for e in entries:
        day = e["properties"]["Date"]["date"]["start"]
        done = extract_text(e, "Done")
        lines.append(f"*{day}*\n_{done}_\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    today = date.today()
    streak = 0
    current = today

    # Count consecutive days logged (going back from today)
    while True:
        entry = await get_entry(current)
        if not entry:
            break
        streak += 1
        current -= timedelta(days=1)

    total_entries = await get_entries_range(date(2020, 1, 1), today)
    total = len(total_entries)

    emoji = "🔥" if streak >= 3 else "📅"
    await update.message.reply_text(
        f"{emoji} *הסטטיסטיקות שלך:*\n\n"
        f"רצף נוכחי: *{streak} ימים*\n"
        f"סה\"כ רשומות: *{total}*",
        parse_mode="Markdown",
    )


async def cmd_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    topic = get_todays_topic()
    await update.message.reply_text(
        f"🧠 *נושא היום — {topic['title']}*\n\n{topic['body']}",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text(
        "📖 *פקודות:*\n\n"
        "/log — תעד את היום\n"
        "/tomorrow — ראה את התוכנית להיום\n"
        "/summary — סיכום 7 הימים האחרונים\n"
        "/stats — streak וסטטיסטיקות\n"
        "/topic — נושא היום ב-DevOps/ארכיטקטורה\n"
        "/cancel — בטל פעולה",
        parse_mode="Markdown",
    )


# ── Setup ─────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_evening_prompt, CronTrigger(hour=EVENING_HOUR, minute=0, timezone=TIMEZONE), args=[app], id="evening")
    scheduler.add_job(send_morning_reminder, CronTrigger(hour=MORNING_HOUR, minute=0, timezone=TIMEZONE), args=[app], id="morning")
    scheduler.add_job(send_late_reminder, CronTrigger(hour=REMINDER_HOUR, minute=0, timezone=TIMEZONE), args=[app], id="reminder")
    scheduler.start()
    log.info("Scheduler started. Evening %d:00, morning %d:00, reminder %d:00 (%s)", EVENING_HOUR, MORNING_HOUR, REMINDER_HOUR, TIMEZONE)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("log", cmd_log),
            CallbackQueryHandler(inline_start_log, pattern="^start_log$"),
        ],
        states={
            DONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_done)],
            TOMORROW: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_tomorrow),
                CallbackQueryHandler(skip_tomorrow, pattern="^skip_tomorrow$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("topic", cmd_topic))
    app.add_handler(CommandHandler("help", cmd_help))

    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
