import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Suppress noisy HTTP logs from telegram library
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Per-chat state ──────────────────────────────────────────────
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
DEFAULT_CWD = os.path.expanduser("~")
DEFAULT_MODEL = None  # use CLI default


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state():
    data = {
        "sessions": {str(k): v for k, v in sessions.items()},
        "working_dirs": {str(k): v for k, v in working_dirs.items()},
        "models": {str(k): v for k, v in models.items()},
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


_state = _load_state()
sessions: dict[int, str] = {int(k): v for k, v in _state.get("sessions", {}).items()}
working_dirs: dict[int, str] = {int(k): v for k, v in _state.get("working_dirs", {}).items()}
models: dict[int, str] = {int(k): v for k, v in _state.get("models", {}).items()}

MAX_MSG_LEN = 4096
MAX_HISTORY = 20  # per chat
_chat_locks: dict[int, asyncio.Lock] = {}  # per-chat concurrency locks
_history: dict[int, list] = {}  # chat_id -> list of {"role", "text", "ts"}


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


def _record(chat_id: int, role: str, text: str):
    """Append an exchange to per-chat history."""
    if chat_id not in _history:
        _history[chat_id] = []
    _history[chat_id].append({
        "role": role,
        "text": text[:500],  # truncate for memory
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    _history[chat_id] = _history[chat_id][-MAX_HISTORY:]


CREDENTIALS_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"

COLORS = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "magenta": "\033[35m", "blue": "\033[34m", "red": "\033[31m",
}

# Skills that work when forwarded to `claude -p` as prompts
CLI_SKILLS = {
    "review", "security-review", "simplify", "pr-comments",
    "release-notes", "init", "debug", "insights", "compact",
    "context", "cost", "batch", "loop", "schedule", "claude-api",
    "update-config", "extra-usage",
}


# ── Helpers ─────────────────────────────────────────────────────

async def is_allowed(update: Update) -> bool:
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("Unauthorized.")
        return False
    return True


async def _keep_typing(chat):
    """Send 'typing' action every 4s until cancelled."""
    try:
        while True:
            await chat.send_action("typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def _escape_md2(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2, preserving code blocks and inline code."""
    # Extract code blocks and inline code, escape everything else
    SPECIAL = r'_*[]()~`>#+=|{}.!-'
    parts = []
    pos = 0
    # Match ```...``` blocks and `...` inline code
    pattern = re.compile(r'(```[\s\S]*?```|`[^`\n]+`)')
    for m in pattern.finditer(text):
        # Escape text before this code span
        before = text[pos:m.start()]
        for ch in SPECIAL:
            before = before.replace(ch, f'\\{ch}')
        parts.append(before)
        # Code blocks/inline code: only escape backslashes and backticks minimally
        code = m.group(0)
        if code.startswith('```'):
            # Telegram expects ```lang\n...\n``` — keep as-is inside
            inner = code[3:-3]
            parts.append(f'```{inner}```')
        else:
            inner = code[1:-1]
            parts.append(f'`{inner}`')
        pos = m.end()
    # Escape remaining text
    tail = text[pos:]
    for ch in SPECIAL:
        tail = tail.replace(ch, f'\\{ch}')
    parts.append(tail)
    return ''.join(parts)


async def _send_message(update: Update, text: str):
    """Send a message with MarkdownV2 formatting, falling back to plain text."""
    if not text:
        text = "(empty response)"
    for i in range(0, len(text), MAX_MSG_LEN):
        chunk = text[i : i + MAX_MSG_LEN]
        try:
            await update.message.reply_text(
                _escape_md2(chunk), parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            await update.message.reply_text(chunk)


async def reply(update: Update, text: str):
    """Reply to the user in Telegram AND print both sides to the terminal."""
    c = COLORS
    user_text = update.message.text or ""
    print(f"\n{c['cyan']}{c['bold']}You:{c['reset']} {user_text}")
    print(f"{c['green']}{c['bold']}Bot:{c['reset']} {text}", flush=True)
    await _send_message(update, text)


# ── Terminal display ────────────────────────────────────────────

def _print_event(event: dict):
    c = COLORS
    etype = event.get("type", "")
    subtype = event.get("subtype", "")

    if etype == "system" and subtype == "init":
        model = event.get("model", "unknown")
        session = event.get("session_id", "")[:12]
        print(f"\n{c['cyan']}{'━' * 60}")
        print(f"  Claude Code  |  model: {model}  |  session: {session}...")
        print(f"{'━' * 60}{c['reset']}")

    elif etype == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "text":
                print(f"\n{c['green']}{c['bold']}Claude:{c['reset']} {block['text']}")
            elif block.get("type") == "tool_use":
                tool = block.get("name", "?")
                inp = block.get("input", {})
                print(f"\n{c['yellow']}Tool: {tool}{c['reset']}")
                if tool == "Bash" and "command" in inp:
                    print(f"  {c['dim']}$ {inp['command']}{c['reset']}")
                elif tool in ("Read", "Edit", "Write") and "file_path" in inp:
                    print(f"  {c['dim']}{tool.lower()}: {inp['file_path']}{c['reset']}")
                elif tool in ("Grep", "Glob") and "pattern" in inp:
                    print(f"  {c['dim']}{tool.lower()}: {inp['pattern']}{c['reset']}")
                else:
                    summary = json.dumps(inp, ensure_ascii=False)
                    if len(summary) > 200:
                        summary = summary[:200] + "..."
                    print(f"  {c['dim']}{summary}{c['reset']}")

    elif etype == "tool_result":
        content = event.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        if content:
            preview = content[:300] + ("..." if len(content) > 300 else "")
            print(f"  {c['blue']}→ {preview}{c['reset']}")

    elif etype == "result":
        duration = event.get("duration_ms", 0)
        turns = event.get("num_turns", 0)
        print(f"\n{c['magenta']}{'─' * 40}")
        print(f"  Done in {duration/1000:.1f}s  |  {turns} turn(s)")
        print(f"{'─' * 40}{c['reset']}\n")

    sys.stdout.flush()


def _detect_cwd_change(event: dict, current_cwd: str) -> str | None:
    if event.get("type") != "assistant":
        return None
    for block in event.get("message", {}).get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "Bash":
            command = block.get("input", {}).get("command", "")
            m = re.search(r'\bcd\s+["\']?([^"\';&|]+)', command)
            if m:
                target = os.path.expanduser(m.group(1).strip())
                if not os.path.isabs(target):
                    target = os.path.normpath(os.path.join(current_cwd, target))
                if os.path.isdir(target):
                    return target
    return None


# ── Claude CLI runner ───────────────────────────────────────────

MAX_RETRIES = 2
RETRY_DELAY = 3  # seconds


def _run_claude_once(cmd: list, cwd: str) -> dict:
    """Execute claude CLI once, return parsed result dict."""
    c = COLORS
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=cwd, bufsize=1, encoding="utf-8", errors="replace",
    )

    result_data = None
    detected_cwd = None
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                _print_event(event)
                new_cwd = _detect_cwd_change(event, cwd)
                if new_cwd:
                    detected_cwd = new_cwd
                    print(f"  {c['yellow']}cwd → {detected_cwd}{c['reset']}")
                    sys.stdout.flush()
                if event.get("type") == "result":
                    result_data = event
            except json.JSONDecodeError:
                pass
        proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"text": "Claude timed out.", "session_id": None, "cwd": cwd, "_timeout": True}

    if result_data:
        return {
            "text": result_data.get("result", ""),
            "session_id": result_data.get("session_id"),
            "cwd": detected_cwd or cwd,
        }
    # Non-zero exit or no result — transient failure
    return {"text": "", "session_id": None, "cwd": cwd, "_failed": True}


def run_claude(prompt: str, session_id: str | None = None,
               cwd: str | None = None, model: str | None = None) -> dict:
    import time
    cwd = cwd or DEFAULT_CWD
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions"]
    if session_id:
        cmd.extend(["--resume", session_id])
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    c = COLORS
    print(f"\n{c['cyan']}{c['bold']}You:{c['reset']} {prompt}")
    print(f"{c['dim']}  cwd: {cwd}{c['reset']}")
    sys.stdout.flush()

    for attempt in range(1, MAX_RETRIES + 2):  # 1 try + MAX_RETRIES retries
        result = _run_claude_once(cmd, cwd)

        if result.get("_timeout"):
            result.pop("_timeout")
            return result

        if result.get("_failed") and attempt <= MAX_RETRIES:
            print(f"  {c['yellow']}Retry {attempt}/{MAX_RETRIES}...{c['reset']}")
            sys.stdout.flush()
            time.sleep(RETRY_DELAY)
            continue

        result.pop("_failed", None)
        if not result["text"]:
            result["text"] = "No response from Claude."
        return result

    return {"text": "No response from Claude after retries.", "session_id": None, "cwd": cwd}


# ── Usage API ───────────────────────────────────────────────────

def _fetch_usage() -> dict | None:
    try:
        with open(CREDENTIALS_PATH) as f:
            creds = json.load(f)
        token = creds["claudeAiOauth"]["accessToken"]
    except (FileNotFoundError, KeyError):
        return None

    req = urllib.request.Request(USAGE_API_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _progress_bar(pct: float, width: int = 20) -> str:
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _time_until(iso_ts: str) -> str:
    target = datetime.fromisoformat(iso_ts)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = target - datetime.now(timezone.utc)
    total_secs = max(int(delta.total_seconds()), 0)
    days, rem = divmod(total_secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


# ── Bot command handlers ────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await reply(update, "Claude Code bridge active. Type /help for commands.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await reply(update,
        "Session:\n"
        "/new, /clear, /reset - fresh session\n"
        "/session - show session ID\n"
        "/resume <id> - resume a session\n"
        "/compact [instructions] - compact context\n"
        "/context - show context usage\n"
        "/cost - show token usage\n\n"
        "Navigation:\n"
        "/cd, /cwd - show working directory\n"
        "/cd <path> - change directory\n"
        "/add_dir <path> - add extra directory\n\n"
        "Model:\n"
        "/model - show current model\n"
        "/model <name> - switch model (opus, sonnet, haiku)\n\n"
        "Info:\n"
        "/usage - plan usage & rate limits\n"
        "/status - version & account info\n"
        "/doctor - health check\n"
        "/history - recent exchanges\n\n"
        "Skills (forwarded to Claude):\n"
        "/review /security_review /simplify\n"
        "/pr_comments /release_notes /init\n"
        "/debug /insights /batch\n\n"
        "Everything else is sent as a prompt."
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    old = sessions.pop(chat_id, None)
    _save_state()
    await reply(update,
        f"Session cleared (was: {old[:12]}...)." if old else "Starting fresh."
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    args = " ".join(context.args) if context.args else ""
    if not args:
        await reply(update,"Usage: /resume <session_id>")
        return
    sessions[chat_id] = args.strip()
    _save_state()
    await reply(update,f"Resumed session: {args.strip()[:12]}...")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    sid = sessions.get(chat_id)
    cwd = working_dirs.get(chat_id, DEFAULT_CWD)
    model = models.get(chat_id, "default")
    msg = (
        f"Session: {sid or 'none'}\n"
        f"Directory: {cwd}\n"
        f"Model: {model}"
    )
    await reply(update,msg)


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    args = " ".join(context.args) if context.args else ""
    if not args:
        cwd = working_dirs.get(chat_id, DEFAULT_CWD)
        await reply(update,f"Current directory: {cwd}")
        return
    target = os.path.expanduser(args)
    if not os.path.isabs(target):
        current = working_dirs.get(chat_id, DEFAULT_CWD)
        target = os.path.normpath(os.path.join(current, target))
    if os.path.isdir(target):
        working_dirs[chat_id] = target
        _save_state()
        await reply(update,f"Changed to: {target}")
    else:
        await reply(update,f"Not a directory: {target}")


async def cmd_add_dir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    args = " ".join(context.args) if context.args else ""
    if not args:
        await reply(update,"Usage: /add_dir <path>")
        return
    # add_dir is passed as a CLI flag — store it and inform user
    await reply(update,
        f"Note: --add-dir is a CLI startup flag. Use /cd to change directory, "
        f"or mention the path in your prompt and Claude will access it."
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    args = " ".join(context.args).strip() if context.args else ""
    if not args:
        current = models.get(chat_id, "default (CLI decides)")
        await reply(update,f"Current model: {current}")
        return
    models[chat_id] = args
    _save_state()
    await reply(update,f"Model set to: {args}")


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch_usage)

    if not data:
        await reply(update,"Could not fetch usage data.")
        return

    lines = []
    for key, label in [("five_hour", "5-hour session"), ("seven_day", "Weekly quota")]:
        bucket = data.get(key)
        if not bucket:
            continue
        pct = bucket.get("utilization", 0)
        resets_at = bucket.get("resets_at")
        bar = _progress_bar(pct)
        reset_str = f"resets in {_time_until(resets_at)}" if resets_at else ""
        lines.append(f"{label}\n{bar}  {pct:.0f}%  ({reset_str})")

    opus = data.get("seven_day_opus")
    if opus and opus.get("utilization", 0) > 0:
        pct = opus["utilization"]
        resets_at = opus.get("resets_at")
        bar = _progress_bar(pct)
        reset_str = f"resets in {_time_until(resets_at)}" if resets_at else ""
        lines.append(f"Weekly Opus\n{bar}  {pct:.0f}%  ({reset_str})")

    msg = "\n\n".join(lines) if lines else "No usage data available."
    await reply(update,msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    try:
        ver = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        ver = "unknown"

    chat_id = update.effective_chat.id
    await reply(update,
        f"Claude Code version: {ver}\n"
        f"Model: {models.get(chat_id, 'default')}\n"
        f"Directory: {working_dirs.get(chat_id, DEFAULT_CWD)}\n"
        f"Session: {sessions.get(chat_id, 'none')}"
    )


async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    try:
        result = subprocess.run(
            ["claude", "doctor"], capture_output=True, text=True, timeout=15
        )
        output = (result.stdout + result.stderr).strip() or "No output."
    except Exception as e:
        output = f"Error: {e}"
    await reply(update, output)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    entries = _history.get(chat_id, [])
    if not entries:
        await reply(update, "No history yet.")
        return
    lines = []
    for e in entries:
        ts = e["ts"][:16].replace("T", " ")
        role = "You" if e["role"] == "user" else "Bot"
        preview = e["text"][:100] + ("..." if len(e["text"]) > 100 else "")
        lines.append(f"[{ts}] {role}: {preview}")
    await reply(update, "\n".join(lines))


# ── Message handler (forwarding to Claude CLI) ─────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return

    text = update.message.text
    if not text:
        return

    chat_id = update.effective_chat.id

    async with _get_lock(chat_id):
        session_id = sessions.get(chat_id)
        cwd = working_dirs.get(chat_id, DEFAULT_CWD)
        model = models.get(chat_id)

        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, run_claude, text, session_id, cwd, model)
        except subprocess.TimeoutExpired:
            await reply(update, "Claude timed out (10 min limit).")
            return
        except Exception as e:
            print(f">>> ERROR: {e}", flush=True)
            await reply(update, f"Error: {e}")
            return
        finally:
            typing_task.cancel()

        if result.get("session_id"):
            sessions[chat_id] = result["session_id"]
        if result.get("cwd"):
            working_dirs[chat_id] = result["cwd"]
        _save_state()

        _record(chat_id, "user", text)
        _record(chat_id, "bot", result["text"] or "(empty)")

        # Don't use reply() here — run_claude already printed to terminal
        await _send_message(update, result["text"])


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos and documents — download and pass to Claude."""
    if not await is_allowed(update):
        return

    chat_id = update.effective_chat.id
    msg = update.message
    caption = msg.caption or ""

    # Determine what to download
    if msg.photo:
        file_obj = await msg.photo[-1].get_file()  # highest resolution
        ext = ".jpg"
    elif msg.document:
        file_obj = await msg.document.get_file()
        ext = os.path.splitext(msg.document.file_name or "")[1] or ".bin"
    else:
        return

    # Download to temp file
    tmp_dir = tempfile.mkdtemp(prefix="tg_claude_")
    filename = f"upload{ext}"
    local_path = os.path.join(tmp_dir, filename)
    await file_obj.download_to_drive(local_path)

    prompt = f"I've attached a file at {local_path}"
    if caption:
        prompt += f"\n\nUser says: {caption}"
    else:
        prompt += "\n\nPlease analyze this file."

    async with _get_lock(chat_id):
        session_id = sessions.get(chat_id)
        cwd = working_dirs.get(chat_id, DEFAULT_CWD)
        model = models.get(chat_id)

        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, run_claude, prompt, session_id, cwd, model)
        except Exception as e:
            print(f">>> ERROR: {e}", flush=True)
            await reply(update, f"Error: {e}")
            return
        finally:
            typing_task.cancel()

        if result.get("session_id"):
            sessions[chat_id] = result["session_id"]
        if result.get("cwd"):
            working_dirs[chat_id] = result["cwd"]
        _save_state()

        _record(chat_id, "user", caption or "[file]")
        _record(chat_id, "bot", result["text"] or "(empty)")

        await _send_message(update, result["text"])


# ── Main ────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Session management
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("clear", cmd_new))       # alias
    app.add_handler(CommandHandler("reset", cmd_new))       # alias
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("help", cmd_help))

    # Navigation
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("cwd", cmd_cd))          # alias
    app.add_handler(CommandHandler("add_dir", cmd_add_dir))

    # Model
    app.add_handler(CommandHandler("model", cmd_model))

    # Info
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("history", cmd_history))

    # Skills forwarded to CLI — registered as commands so Telegram
    # doesn't eat them; handler just forwards the text as-is
    for skill in CLI_SKILLS:
        safe = skill.replace("-", "_")
        app.add_handler(CommandHandler(safe, handle_message))

    # Photos and documents
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))

    # Everything else (plain text + unknown /commands)
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print(f"{COLORS['cyan']}Bot started. Listening for messages...{COLORS['reset']}")
    sys.stdout.flush()
    app.run_polling()


if __name__ == "__main__":
    main()
