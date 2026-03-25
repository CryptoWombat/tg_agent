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
from telegram.error import NetworkError, Conflict, TimedOut
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

VERSION = "0.3.0"


def _git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        return "dev"


BUILD_ID = _git_hash()
BUILD_TIME = datetime.fromtimestamp(
    os.path.getmtime(os.path.abspath(__file__)), tz=timezone.utc
).strftime("%Y-%m-%d %H:%M:%S UTC")

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
        "history": {str(k): v for k, v in _history.items()},
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
_active_procs: dict[int, subprocess.Popen] = {}  # chat_id -> running claude process
_history: dict[int, list] = {int(k): v for k, v in _state.get("history", {}).items()}
_last_usage_pct: dict[str, int] = {}  # bucket -> last reported 10% band
_bot_ref = None  # set at startup for use in background tasks


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
    SPECIAL = r'\_*[]()~`>#+=|{}.!-'
    parts = []
    pos = 0
    # Match ```...``` blocks and `...` inline code
    pattern = re.compile(r'(```[\s\S]*?```|`[^`\n]+`)')
    for m in pattern.finditer(text):
        # Escape text before this code span
        before = text[pos:m.start()]
        before = before.replace('\\', '\\\\')  # escape backslashes first
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
    tail = tail.replace('\\', '\\\\')  # escape backslashes first
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


def _run_claude_once(cmd: list, cwd: str, chat_id: int | None = None) -> dict:
    """Execute claude CLI once, return parsed result dict."""
    c = COLORS
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=cwd, bufsize=1, encoding="utf-8", errors="replace",
    )

    if chat_id is not None:
        _active_procs[chat_id] = proc

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
    finally:
        _active_procs.pop(chat_id, None)

    if proc.returncode == -9 or proc.returncode == -15:
        return {"text": "Cancelled.", "session_id": None, "cwd": cwd, "_cancelled": True}

    if result_data:
        return {
            "text": result_data.get("result", ""),
            "session_id": result_data.get("session_id"),
            "cwd": detected_cwd or cwd,
        }
    # Non-zero exit or no result — transient failure
    return {"text": "", "session_id": None, "cwd": cwd, "_failed": True}


def run_claude(prompt: str, session_id: str | None = None,
               cwd: str | None = None, model: str | None = None,
               chat_id: int | None = None) -> dict:
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
        result = _run_claude_once(cmd, cwd, chat_id)

        if result.get("_timeout"):
            result.pop("_timeout")
            return result

        if result.get("_cancelled"):
            result.pop("_cancelled")
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


async def _check_usage_thresholds(bot):
    """Fetch usage and notify if any quota crossed a new 10% band since last check."""
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_usage)
        if not data:
            return

        alerts = []
        bucket_labels = {
            "five_hour": "5-hour session",
            "seven_day": "Weekly quota",
            "seven_day_opus": "Weekly Opus",
        }

        for key, label in bucket_labels.items():
            bucket = data.get(key)
            if not bucket:
                continue
            pct = bucket.get("utilization", 0)
            band = int(pct // 10)
            last_band = _last_usage_pct.get(key, -1)

            if band > last_band:
                _last_usage_pct[key] = band
                resets_at = bucket.get("resets_at")
                reset_str = f"  resets in {_time_until(resets_at)}" if resets_at else ""
                bar = _progress_bar(pct)
                alerts.append(f"{label}\n{bar}  {pct:.0f}%{reset_str}")

        if alerts:
            msg = "Usage update:\n\n" + "\n\n".join(alerts)
            await bot.send_message(chat_id=ALLOWED_USER_ID, text=msg)
    except Exception as e:
        log.warning(f"Usage threshold check failed: {e}")


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
    await reply(update, f"Claude Code bridge v{VERSION} · {BUILD_ID}\nType /help for commands.")


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
        "/project - list projects\n"
        "/project <name> - switch project\n"
        "/add_dir <path> - add extra directory\n\n"
        "Model:\n"
        "/model - show current model\n"
        "/model <name> - switch model (opus, sonnet, haiku)\n\n"
        "Info:\n"
        "/usage - plan usage & rate limits\n"
        "/status - version & account info\n"
        "/doctor - health check\n"
        "/history - recent exchanges\n"
        "/cancel - stop running task\n"
        "/version - show bot version\n\n"
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


PROJECTS = {
    "tg_agent":   r"C:\Users\w0mb4\tg_agent",
    "quotebot":   r"C:\Users\w0mb4\QuoteBot",
    "alphaforge": r"C:\Users\w0mb4\AlphaForge",
}
SUMMARIES_DIR = r"C:\Users\w0mb4\project-summaries"


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    args = " ".join(context.args).strip().lower() if context.args else ""

    if not args:
        current = working_dirs.get(chat_id, DEFAULT_CWD)
        names = "\n".join(f"  {k} → {v}" for k, v in PROJECTS.items())
        await reply(update, f"Current directory: {current}\n\nProjects:\n{names}")
        return

    target = PROJECTS.get(args)
    if not target:
        names = ", ".join(PROJECTS.keys())
        await reply(update, f"Unknown project '{args}'. Available: {names}")
        return

    if not os.path.isdir(target):
        await reply(update, f"Directory not found: {target}")
        return

    # Detect current project name from cwd
    current_cwd = working_dirs.get(chat_id, DEFAULT_CWD)
    current_project = next(
        (k for k, v in PROJECTS.items() if os.path.normpath(v) == os.path.normpath(current_cwd)),
        None
    )

    # Switch directory
    working_dirs[chat_id] = target
    _save_state()

    msg = f"Switched to project: {args}\nDirectory: {target}"

    # Load summary for new project if it exists
    summary_path = os.path.join(SUMMARIES_DIR, f"{args}.md")
    if os.path.exists(summary_path):
        try:
            with open(summary_path, encoding="utf-8") as f:
                summary = f.read(1500)
            msg += f"\n\nContext summary:\n{summary}"
        except Exception:
            pass
    else:
        msg += "\n\n(No saved summary for this project yet.)"

    if current_project and current_project != args:
        msg += f"\n\nTip: clear context when ready (/new clears Claude session)."

    await reply(update, msg)


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


MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# Ordered low → high for up/down switching
MODEL_LADDER = [
    ("haiku", "claude-haiku-4-5-20251001"),
    ("sonnet", "claude-sonnet-4-6"),
    ("opus", "claude-opus-4-6"),
]

DEFAULT_MODEL_ID = "claude-sonnet-4-6"


def _model_name(model_id: str) -> str:
    """Return a friendly name for a model ID."""
    for name, mid in MODEL_LADDER:
        if mid == model_id:
            return f"{name.capitalize()} ({mid})"
    return model_id


def _ladder_index(model_id: str) -> int:
    for i, (_, mid) in enumerate(MODEL_LADDER):
        if mid == model_id:
            return i
    return -1


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    args = " ".join(context.args).strip().lower() if context.args else ""
    current = models.get(chat_id, DEFAULT_MODEL_ID)

    if not args:
        await reply(update, f"Current model: {_model_name(current)}")
        return

    if args == "up":
        idx = _ladder_index(current)
        if idx < 0:
            idx = 0  # unknown model, start from bottom
        if idx >= len(MODEL_LADDER) - 1:
            await reply(update, f"Already on highest: {_model_name(current)}")
            return
        _, new_id = MODEL_LADDER[idx + 1]
        models[chat_id] = new_id
        _save_state()
        await reply(update, f"Upgraded to: {_model_name(new_id)}")
        return

    if args == "down":
        idx = _ladder_index(current)
        if idx < 0:
            idx = len(MODEL_LADDER) - 1  # unknown model, start from top
        if idx <= 0:
            await reply(update, f"Already on lowest: {_model_name(current)}")
            return
        _, new_id = MODEL_LADDER[idx - 1]
        models[chat_id] = new_id
        _save_state()
        await reply(update, f"Downgraded to: {_model_name(new_id)}")
        return

    model_id = MODEL_ALIASES.get(args, args)
    models[chat_id] = model_id
    _save_state()
    await reply(update, f"Model set to: {_model_name(model_id)}")


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
        f"Bot: v{VERSION} · {BUILD_ID}\n"
        f"Built: {BUILD_TIME}\n"
        f"Claude CLI: {ver}\n"
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


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    proc = _active_procs.get(chat_id)
    if proc and proc.poll() is None:
        proc.terminate()
        await reply(update, "Cancelled running task.")
    else:
        await reply(update, "Nothing running.")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await reply(update, f"v{VERSION} · {BUILD_ID}")


# ── Message handler (forwarding to Claude CLI) ─────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return

    text = update.message.text
    if not text:
        return

    chat_id = update.effective_chat.id
    lock = _get_lock(chat_id)

    if lock.locked():
        await update.message.reply_text("Queued — waiting for current task to finish...")

    async with lock:
        session_id = sessions.get(chat_id)
        cwd = working_dirs.get(chat_id, DEFAULT_CWD)
        model = models.get(chat_id)

        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: run_claude(text, session_id, cwd, model, chat_id=chat_id)
            )
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
        if _bot_ref:
            asyncio.create_task(_check_usage_thresholds(_bot_ref))


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

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Queued — waiting for current task to finish...")

    async with lock:
        session_id = sessions.get(chat_id)
        cwd = working_dirs.get(chat_id, DEFAULT_CWD)
        model = models.get(chat_id)

        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: run_claude(prompt, session_id, cwd, model, chat_id=chat_id)
            )
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
        if _bot_ref:
            asyncio.create_task(_check_usage_thresholds(_bot_ref))


def _transcribe_voice(ogg_path: str) -> str:
    """Convert .ogg voice note to text via speech_recognition + pydub."""
    import speech_recognition as sr
    from pydub import AudioSegment

    wav_path = ogg_path.rsplit(".", 1)[0] + ".wav"
    audio = AudioSegment.from_ogg(ogg_path)
    audio.export(wav_path, format="wav")

    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio_data)
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        return f"[transcription failed: {e}]"


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages — transcribe and forward to Claude."""
    if not await is_allowed(update):
        return

    chat_id = update.effective_chat.id
    voice = update.message.voice
    if not voice:
        return

    file_obj = await voice.get_file()
    tmp_dir = tempfile.mkdtemp(prefix="tg_claude_")
    ogg_path = os.path.join(tmp_dir, "voice.ogg")
    await file_obj.download_to_drive(ogg_path)

    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _transcribe_voice, ogg_path)

    if not text:
        await reply(update, "Could not transcribe voice message.")
        return

    # Show what was transcribed
    c = COLORS
    print(f"\n{c['cyan']}{c['bold']}You (voice):{c['reset']} {text}", flush=True)
    await update.message.reply_text(f"Transcribed: {text}")

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Queued — waiting for current task to finish...")

    async with lock:
        session_id = sessions.get(chat_id)
        cwd = working_dirs.get(chat_id, DEFAULT_CWD)
        model = models.get(chat_id)

        typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

        try:
            result = await loop.run_in_executor(
                None, lambda: run_claude(text, session_id, cwd, model, chat_id=chat_id)
            )
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

        _record(chat_id, "user", f"[voice] {text}")
        _record(chat_id, "bot", result["text"] or "(empty)")

        await _send_message(update, result["text"])
        if _bot_ref:
            asyncio.create_task(_check_usage_thresholds(_bot_ref))


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
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("add_dir", cmd_add_dir))

    # Model
    app.add_handler(CommandHandler("model", cmd_model))

    # Info
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("version", cmd_version))

    # Skills forwarded to CLI — registered as commands so Telegram
    # doesn't eat them; handler just forwards the text as-is
    for skill in CLI_SKILLS:
        safe = skill.replace("-", "_")
        app.add_handler(CommandHandler(safe, handle_message))

    # Photos, documents, and voice
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Everything else (plain text + unknown /commands)
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    async def _notify_startup(application):
        global _bot_ref
        _bot_ref = application.bot
        await application.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=f"Bot restarted — v{VERSION} · {BUILD_ID}\nLast changed: {BUILD_TIME}",
        )

    app.post_init = _notify_startup

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        """Silently ignore transient network errors; log real ones."""
        err = context.error
        if isinstance(err, (NetworkError, Conflict, TimedOut)):
            return  # transient — bot auto-retries
        log.error("Unhandled exception: %s", err, exc_info=err)

    app.add_error_handler(error_handler)

    print(f"{COLORS['cyan']}Bot v{VERSION} · {BUILD_ID} started. Listening for messages...{COLORS['reset']}")
    sys.stdout.flush()
    app.run_polling()


def _run_with_auto_reload():
    """Run the bot as a subprocess; restart on file change or crash."""
    import time as _time
    import threading

    SCRIPT = os.path.abspath(__file__)
    RESTART_DELAY = 3
    COOLDOWN = 5  # seconds to ignore mtime changes after restart
    c = COLORS
    proc = None
    file_changed = threading.Event()
    stop_watcher = threading.Event()
    cooldown_until = [0.0]  # mutable container for thread access

    def _watch():
        """Poll bot.py mtime every 2s, signal when it changes."""
        last_mtime = os.path.getmtime(SCRIPT)
        while not stop_watcher.is_set():
            _time.sleep(2)
            if _time.time() < cooldown_until[0]:
                last_mtime = os.path.getmtime(SCRIPT)
                continue
            try:
                current = os.path.getmtime(SCRIPT)
                if current != last_mtime:
                    last_mtime = current
                    print(f"\n{c['yellow']}{c['bold']}File change detected — restarting...{c['reset']}", flush=True)
                    file_changed.set()
                    if proc and proc.poll() is None:
                        proc.terminate()
            except OSError:
                pass

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()

    while True:
        file_changed.clear()
        cooldown_until[0] = _time.time() + COOLDOWN
        print(f"{c['cyan']}Starting bot (pid will follow)...{c['reset']}", flush=True)

        proc = subprocess.Popen(
            [sys.executable, SCRIPT, "--run"],
            stdout=None, stderr=None,  # inherit terminal
        )

        proc.wait()

        if file_changed.is_set():
            print(f"{c['cyan']}Reloading with updated code...{c['reset']}", flush=True)
            _time.sleep(1)
            continue

        if proc.returncode == 0:
            print(f"{c['yellow']}Bot exited cleanly.{c['reset']}")
            break

        print(f"{c['red']}{c['bold']}Bot exited with code {proc.returncode}{c['reset']}")
        print(f"{c['yellow']}Restarting in {RESTART_DELAY}s...{c['reset']}", flush=True)
        _time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    if "--run" in sys.argv:
        # Actually run the bot (launched by the wrapper)
        try:
            main()
        except KeyboardInterrupt:
            pass
    else:
        # Wrapper with auto-reload
        try:
            _run_with_auto_reload()
        except KeyboardInterrupt:
            print(f"\n{COLORS['yellow']}Stopped by user.{COLORS['reset']}")
