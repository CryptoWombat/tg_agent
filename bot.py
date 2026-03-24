import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# chat_id -> session_id mapping for conversation continuity
sessions: dict[int, str] = {}

# Usage tracking
usage_stats: dict[str, float | int] = {
    "total_cost": 0.0,
    "total_requests": 0,
    "started_at": datetime.now().isoformat(),
}
# Per-session cost tracking
session_costs: dict[str, float] = {}

MAX_MSG_LEN = 4096  # Telegram message length limit

# Built-in commands that are handled bot-side (not forwarded to CLI)
BUILTIN_COMMANDS = {
    "/usage", "/help", "/clear", "/compact",
    "/start", "/new", "/session",
}


async def is_allowed(update: Update) -> bool:
    if update.effective_user.id != ALLOWED_USER_ID:
        log.warning("Unauthorized access attempt from user %s", update.effective_user.id)
        await update.message.reply_text("Unauthorized.")
        return False
    return True


def run_claude(prompt: str, session_id: str | None = None) -> dict:
    """Run claude CLI in print mode and return parsed result."""
    cmd = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(prompt)

    log.info("Running: %s", " ".join(cmd[:6]) + " ...")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=os.path.expanduser("~"),
    )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    # Try to parse JSON response
    try:
        data = json.loads(stdout)
        return {
            "text": data.get("result", stdout),
            "session_id": data.get("session_id"),
            "cost": data.get("total_cost_usd"),
            "input_tokens": data.get("usage", {}).get("input_tokens", 0),
            "output_tokens": data.get("usage", {}).get("output_tokens", 0),
            "cache_read": data.get("usage", {}).get("cache_read_input_tokens", 0),
            "cache_create": data.get("usage", {}).get("cache_creation_input_tokens", 0),
            "num_turns": data.get("num_turns", 1),
            "model": next(iter(data.get("modelUsage", {}).keys()), "unknown"),
        }
    except (json.JSONDecodeError, TypeError):
        return {
            "text": stdout or stderr or "No response from Claude.",
            "session_id": None,
            "cost": None,
        }


async def send_long_message(update: Update, text: str):
    """Send a message, splitting into chunks if it exceeds Telegram's limit."""
    if not text:
        text = "(empty response)"
    for i in range(0, len(text), MAX_MSG_LEN):
        await update.message.reply_text(text[i : i + MAX_MSG_LEN])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await update.message.reply_text(
        "Claude Code bridge active.\n\n"
        "Bot commands:\n"
        "/new - start a fresh session\n"
        "/session - show current session ID\n"
        "/usage - show usage stats\n"
        "/help - show this help\n\n"
        "Any other message or /command is forwarded to Claude CLI."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await update.message.reply_text(
        "Bot commands (handled locally):\n"
        "/start - welcome message\n"
        "/new - start a fresh Claude session\n"
        "/session - show current session ID\n"
        "/usage - show cost & usage stats\n"
        "/help - this message\n\n"
        "Claude CLI skills (forwarded to Claude):\n"
        "/review - code review\n"
        "/security-review - security review\n"
        "/simplify - simplify code\n"
        "/pr-comments - PR comments\n"
        "/release-notes - release notes\n"
        "/init - initialize project\n"
        "/debug - debug help\n"
        "/insights - insights\n\n"
        "Any other text is sent as a prompt to Claude."
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    old = sessions.pop(chat_id, None)
    await update.message.reply_text(
        f"Session cleared (was: {old[:12]}...)." if old else "No active session. Starting fresh."
    )


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    sid = sessions.get(chat_id)
    cost = session_costs.get(sid, 0) if sid else 0
    msg = f"Session: {sid}\nSession cost: ${cost:.4f}" if sid else "No active session."
    await update.message.reply_text(msg)


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    sid = sessions.get(chat_id)
    session_cost = session_costs.get(sid, 0) if sid else 0

    msg = (
        f"Usage stats (since bot start)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total requests: {usage_stats['total_requests']}\n"
        f"Running since: {usage_stats['started_at'][:19]}\n"
    )
    await update.message.reply_text(msg)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return

    text = update.message.text
    if not text:
        return

    chat_id = update.effective_chat.id
    session_id = sessions.get(chat_id)

    # Show typing indicator
    await update.effective_chat.send_action("typing")

    # Run claude in a thread to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, run_claude, text, session_id)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("Claude timed out (10 min limit).")
        return
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return

    # Save session for continuity
    if result["session_id"]:
        sessions[chat_id] = result["session_id"]

    # Track usage
    cost = result.get("cost") or 0
    usage_stats["total_cost"] += cost
    usage_stats["total_requests"] += 1
    if result.get("session_id"):
        sid = result["session_id"]
        session_costs[sid] = session_costs.get(sid, 0) + cost

    # Build response
    response = result["text"]

    await send_long_message(update, response)


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("help", cmd_help))
    # All other text (including unrecognized /commands) forwarded to Claude CLI
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    log.info("Bot starting... (allowed user: %s)", ALLOWED_USER_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
