import asyncio
import base64
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, Conflict, TimedOut
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

VERSION = "0.5.0"


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
OWNER_USER_ID = int(os.environ["TELEGRAM_USER_ID"])
ALLOWED_USER_IDS = {OWNER_USER_ID, 517263385}

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
DEFAULT_PROVIDER = "claude"
SUPPORTED_PROVIDERS = {"claude", "codex"}


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state():
    data = {
        "provider": {str(k): v for k, v in providers.items()},
        "sessions": {
            str(chat_id): values for chat_id, values in provider_sessions.items()
        },
        "working_dirs": {str(k): v for k, v in working_dirs.items()},
        "models": {
            str(chat_id): values for chat_id, values in provider_models.items()
        },
        "history": {str(k): v for k, v in _history.items()},
        "show_tools": {str(k): v for k, v in _show_tools.items()},
    }
    tmp = STATE_FILE + ".tmp"
    with _state_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)


_state = _load_state()
providers: dict[int, str] = {
    int(k): v for k, v in _state.get("provider", {}).items() if v in SUPPORTED_PROVIDERS
}
provider_sessions: dict[int, dict[str, str]] = {
    int(k): {pk: pv for pk, pv in v.items() if pk in SUPPORTED_PROVIDERS}
    for k, v in _state.get("sessions", {}).items()
    if isinstance(v, dict)
}
provider_models: dict[int, dict[str, str]] = {
    int(k): {pk: pv for pk, pv in v.items() if pk in SUPPORTED_PROVIDERS}
    for k, v in _state.get("models", {}).items()
    if isinstance(v, dict)
}

# Backward compatibility with older flat Claude-only state.
for chat_id, session_id in _state.get("sessions", {}).items():
    if isinstance(session_id, str):
        provider_sessions.setdefault(int(chat_id), {})["claude"] = session_id
for chat_id, model_id in _state.get("models", {}).items():
    if isinstance(model_id, str):
        provider_models.setdefault(int(chat_id), {})["claude"] = model_id

working_dirs: dict[int, str] = {int(k): v for k, v in _state.get("working_dirs", {}).items()}

MAX_MSG_LEN = 4096
MAX_HISTORY = 20  # per chat
_SESSION_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{8,200}$')
_chat_locks: dict[int, asyncio.Lock] = {}  # per-chat concurrency locks
_active_procs: dict[int, subprocess.Popen] = {}  # chat_id -> running claude process
_history: dict[int, list] = {int(k): v for k, v in _state.get("history", {}).items()}
_show_tools: dict[int, bool] = {int(k): v for k, v in _state.get("show_tools", {}).items()}
_last_usage_pct: dict[str, int] = {}  # bucket -> last reported 10% band
_state_lock = threading.Lock()          # protects _save_state writes
_active_procs_lock = threading.Lock()   # protects _active_procs dict
_bot_ref = None  # set at startup for use in background tasks


def _cli_bin(name: str) -> str:
    if os.name == "nt":
        return f"{name}.cmd" if name == "codex" else f"{name}.exe" if shutil.which(f"{name}.exe") else name
    return name


def _get_provider(chat_id: int) -> str:
    return providers.get(chat_id, DEFAULT_PROVIDER)


def _get_session(chat_id: int, provider: str | None = None) -> str | None:
    provider = provider or _get_provider(chat_id)
    return provider_sessions.get(chat_id, {}).get(provider)


def _set_session(chat_id: int, provider: str, session_id: str | None):
    provider_sessions.setdefault(chat_id, {})
    if session_id:
        provider_sessions[chat_id][provider] = session_id
    else:
        provider_sessions[chat_id].pop(provider, None)
        if not provider_sessions[chat_id]:
            provider_sessions.pop(chat_id, None)


def _get_model(chat_id: int, provider: str | None = None) -> str | None:
    provider = provider or _get_provider(chat_id)
    return provider_models.get(chat_id, {}).get(provider)


def _set_model(chat_id: int, provider: str, model_id: str | None):
    provider_models.setdefault(chat_id, {})
    if model_id:
        provider_models[chat_id][provider] = model_id
    else:
        provider_models[chat_id].pop(provider, None)
        if not provider_models[chat_id]:
            provider_models.pop(chat_id, None)


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
    if update.effective_user.id not in ALLOWED_USER_IDS:
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


async def _send_to_chat(bot, chat_id: int, text: str):
    """Send a message to a chat_id, with MarkdownV2 formatting and chunking."""
    if not text:
        return
    for i in range(0, len(text), MAX_MSG_LEN):
        chunk = text[i : i + MAX_MSG_LEN]
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=_escape_md2(chunk),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            try:
                await bot.send_message(chat_id=chat_id, text=chunk)
            except Exception:
                pass


async def _stream_consumer(chat_id: int, stream_queue: queue.Queue, bot, show_tools: bool):
    """Drain stream_queue and send messages to Telegram. Runs until 'done' sentinel."""
    streamed_any = False
    loop = asyncio.get_running_loop()
    while True:
        try:
            item = await loop.run_in_executor(None, lambda: stream_queue.get(timeout=1.0))
        except queue.Empty:
            continue

        etype = item[0]
        if etype == "done":
            return streamed_any

        if etype == "text":
            text = item[1]
            if text.strip():
                streamed_any = True
                await _send_to_chat(bot, chat_id, text)
        elif etype in ("tool", "tool_result") and show_tools:
            await _send_to_chat(bot, chat_id, item[1])

    return streamed_any


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


def _format_tool_event(block: dict) -> str:
    """Format a tool_use block into a human-readable string."""
    tool = block.get("name", "?")
    inp = block.get("input", {})
    if tool == "Bash" and "command" in inp:
        return f"Tool: {tool}\n$ {inp['command']}"
    elif tool in ("Read", "Edit", "Write") and "file_path" in inp:
        return f"Tool: {tool} — {inp['file_path']}"
    elif tool in ("Grep", "Glob") and "pattern" in inp:
        return f"Tool: {tool} — {inp['pattern']}"
    else:
        summary = json.dumps(inp, ensure_ascii=False)
        if len(summary) > 200:
            summary = summary[:200] + "..."
        return f"Tool: {tool}\n{summary}"


def _run_claude_once(cmd: list, cwd: str, chat_id: int | None = None,
                     stream_queue: queue.Queue | None = None) -> dict:
    """Execute claude CLI once, return parsed result dict."""
    c = COLORS
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=cwd, bufsize=1, encoding="utf-8", errors="replace",
    )

    if chat_id is not None:
        with _active_procs_lock:
            _active_procs[chat_id] = proc

    result_data = None
    detected_cwd = None
    cancelled = False
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
                # Push events to stream queue for Telegram delivery
                if stream_queue is not None:
                    etype = event.get("type", "")
                    if etype == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                stream_queue.put(("text", block["text"]))
                            elif block.get("type") == "tool_use":
                                stream_queue.put(("tool", _format_tool_event(block)))
                    elif etype == "tool_result":
                        content = event.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content if b.get("type") == "text"
                            )
                        if content:
                            preview = content[:300] + ("..." if len(content) > 300 else "")
                            stream_queue.put(("tool_result", f"→ {preview}"))
            except json.JSONDecodeError:
                pass
        proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"text": "Claude timed out.", "session_id": None, "cwd": cwd, "_timeout": True}
    finally:
        with _active_procs_lock:
            _active_procs.pop(chat_id, None)

    # Detect cancellation: on Unix -9 (SIGKILL) or -15 (SIGTERM),
    # on Windows returncode 1 when no result was produced
    rc = proc.returncode
    if result_data is None and rc is not None and rc != 0:
        if rc in (-9, -15) or (os.name == "nt" and rc == 1):
            return {"text": "Cancelled.", "session_id": None, "cwd": cwd, "_cancelled": True}

    if result_data:
        return {
            "text": result_data.get("result", ""),
            "session_id": result_data.get("session_id"),
            "cwd": detected_cwd or cwd,
        }
    # Non-zero exit or no result — transient failure
    return {"text": "", "session_id": None, "cwd": cwd, "_failed": True}


def _run_codex_once(cmd: list, cwd: str, output_path: str, chat_id: int | None = None) -> dict:
    """Execute Codex CLI once, return parsed result dict."""
    c = COLORS
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )

    if chat_id is not None:
        with _active_procs_lock:
            _active_procs[chat_id] = proc

    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip()
            if not line:
                continue
            print(f"{c['green']}Codex:{c['reset']} {line}")
            sys.stdout.flush()
        proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"text": "Codex timed out.", "session_id": None, "cwd": cwd, "_timeout": True}
    finally:
        with _active_procs_lock:
            _active_procs.pop(chat_id, None)

    if proc.returncode == -9 or proc.returncode == -15:
        return {"text": "Cancelled.", "session_id": None, "cwd": cwd, "_cancelled": True}

    text = ""
    if os.path.exists(output_path):
        try:
            with open(output_path, encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            text = ""

    if proc.returncode != 0 and not text:
        return {"text": "", "session_id": None, "cwd": cwd, "_failed": True}

    return {
        "text": text,
        "session_id": "last",
        "cwd": cwd,
    }


def run_claude(prompt: str, session_id: str | None = None,
               cwd: str | None = None, model: str | None = None,
               chat_id: int | None = None,
               stream_queue: queue.Queue | None = None) -> dict:
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

    try:
        for attempt in range(1, MAX_RETRIES + 2):  # 1 try + MAX_RETRIES retries
            result = _run_claude_once(cmd, cwd, chat_id, stream_queue=stream_queue)

            if result.get("_timeout"):
                result.pop("_timeout")
                return result

            if result.get("_cancelled"):
                result.pop("_cancelled")
                return result

            if result.get("_failed") and attempt <= MAX_RETRIES:
                # If resuming a session failed, drop --resume and start fresh
                if session_id and "--resume" in cmd:
                    print(f"  {c['yellow']}Session resume failed — starting fresh session...{c['reset']}")
                    cmd = [a for a in cmd if a != "--resume" and a != session_id]
                    session_id = None
                print(f"  {c['yellow']}Retry {attempt}/{MAX_RETRIES}...{c['reset']}")
                sys.stdout.flush()
                time.sleep(RETRY_DELAY)
                continue

            result.pop("_failed", None)
            if not result["text"]:
                result["text"] = "No response from Claude."
            return result

        return {"text": "No response from Claude after retries.", "session_id": None, "cwd": cwd}
    finally:
        if stream_queue is not None:
            stream_queue.put(("done", ""))


def run_codex(prompt: str, session_id: str | None = None,
              cwd: str | None = None, model: str | None = None,
              chat_id: int | None = None) -> dict:
    import time

    cwd = cwd or DEFAULT_CWD
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as out:
        output_path = out.name

    cmd = [
        _cli_bin("codex"),
        "exec",
        "--skip-git-repo-check",
        "--full-auto",
        "-C", cwd,
        "-o", output_path,
    ]
    if model:
        cmd.extend(["-m", model])
    if session_id:
        cmd.append("resume")
        if session_id == "last":
            cmd.append("--last")
        else:
            cmd.append(session_id)
    cmd.append(prompt)

    c = COLORS
    print(f"\n{c['cyan']}{c['bold']}You:{c['reset']} {prompt}")
    print(f"{c['dim']}  provider: codex  cwd: {cwd}{c['reset']}")
    sys.stdout.flush()

    try:
        for attempt in range(1, MAX_RETRIES + 2):
            result = _run_codex_once(cmd, cwd, output_path, chat_id)

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
                result["text"] = "No response from Codex."
            return result
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass

    return {"text": "No response from Codex after retries.", "session_id": None, "cwd": cwd}


def run_agent(provider: str, prompt: str, session_id: str | None = None,
              cwd: str | None = None, model: str | None = None,
              chat_id: int | None = None,
              stream_queue: queue.Queue | None = None) -> dict:
    if provider == "codex":
        return run_codex(prompt, session_id, cwd, model, chat_id)
    return run_claude(prompt, session_id, cwd, model, chat_id, stream_queue=stream_queue)


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


def _short_path(path: str) -> str:
    home = os.path.expanduser("~")
    norm_path = os.path.normpath(path)
    norm_home = os.path.normpath(home)
    if norm_path == norm_home:
        return "~"
    prefix = norm_home + os.sep
    if norm_path.startswith(prefix):
        return "~" + os.sep + norm_path[len(prefix):]
    return path


def _run_cli_capture(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, str(e)

    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()).strip()
    return proc.returncode == 0, output


def _extract_codex_account(login_output: str) -> str | None:
    if not login_output:
        return None
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", login_output)
    if m:
        return m.group(0)
    m = re.search(r"logged in(?: as)?[: ]+(.+)", login_output, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _display_codex_version(version_output: str) -> str:
    if not version_output:
        return "OpenAI Codex"
    line = version_output.splitlines()[0].strip()
    m = re.search(r"(\d+\.\d+\.\d+)", line)
    if m:
        return f"OpenAI Codex (v{m.group(1)})"
    if line.lower().startswith("codex"):
        return "OpenAI " + line[0].upper() + line[1:]
    return line


def _decode_jwt_payload(token: str | None) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _load_codex_auth_profile() -> dict:
    path = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
    try:
        with open(path, encoding="utf-8") as f:
            auth = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    tokens = auth.get("tokens", {})
    access_payload = _decode_jwt_payload(tokens.get("access_token"))
    id_payload = _decode_jwt_payload(tokens.get("id_token"))
    api_auth = access_payload.get("https://api.openai.com/auth", {})
    profile = access_payload.get("https://api.openai.com/profile", {})

    email = profile.get("email") or id_payload.get("email")
    plan = api_auth.get("chatgpt_plan_type")
    if isinstance(plan, str):
        plan = plan.capitalize()

    return {
        "email": email,
        "plan": plan,
    }


def _load_latest_codex_thread() -> dict:
    path = os.path.join(os.path.expanduser("~"), ".codex", "state_5.sqlite")
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT id, cwd, sandbox_policy, approval_mode, cli_version, model, reasoning_effort "
            "FROM threads ORDER BY updated_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()
    except sqlite3.Error:
        return {}

    return dict(row) if row else {}


def _codex_permissions_label(thread: dict) -> str:
    approval = thread.get("approval_mode")
    policy_raw = thread.get("sandbox_policy")
    try:
        policy = json.loads(policy_raw) if policy_raw else {}
    except json.JSONDecodeError:
        policy = {}

    sandbox_type = policy.get("type")
    if approval == "never" and sandbox_type == "workspace-write":
        return "Full auto"
    if approval == "on-request" and sandbox_type == "workspace-write":
        return "Default"
    if approval and sandbox_type:
        return f"{sandbox_type} / {approval}"
    return "unknown"


def _pad_status(label: str, value: str) -> str:
    return f"{label:<19}{value}"


def _format_codex_usage(chat_id: int) -> str:
    cwd = working_dirs.get(chat_id, DEFAULT_CWD)
    model = _get_model(chat_id, "codex") or "default"
    session = _get_session(chat_id, "codex") or "none"
    thread = _load_latest_codex_thread()
    profile = _load_codex_auth_profile()

    ver_ok, ver_output = _run_cli_capture([_cli_bin("codex"), "--version"], timeout=5)
    login_ok, login_output = _run_cli_capture([_cli_bin("codex"), "login", "status"], timeout=8)

    version = _display_codex_version(ver_output or thread.get("cli_version", ""))
    if not ver_ok and not ver_output:
        version = "OpenAI Codex"

    if thread.get("cwd"):
        cwd = thread["cwd"]
    if thread.get("model"):
        model = thread["model"]
    if thread.get("id"):
        session = thread["id"]

    account = profile.get("email") or (_extract_codex_account(login_output) if login_ok else None)
    if account and profile.get("plan"):
        account = f"{account} ({profile['plan']})"
    if not account:
        account = "unavailable"

    auth_ok = "OK" if login_ok else "Unavailable"
    collab_mode = "Default" if _codex_permissions_label(thread) == "Default" else "Bot bridge"

    lines = [
        f">_ {version}",
        "",
        "Visit https://chatgpt.com/codex/settings/usage for up-to-date",
        "information on rate limits and credits",
        "",
        _pad_status("Model:", f"{model} (reasoning none, summaries auto)"),
        _pad_status("Directory:", _short_path(cwd)),
        _pad_status("Permissions:", _codex_permissions_label(thread)),
        _pad_status("Agents.md:", "<none>"),
        _pad_status("Account:", account),
        _pad_status("Collaboration mode:", collab_mode),
        _pad_status("Session:", session),
        "",
        _pad_status("Context window:", "not captured by bot runtime"),
        _pad_status("5h limit:", "not captured by bot runtime"),
        _pad_status("Weekly limit:", "not captured by bot runtime"),
        "",
        _pad_status("Auth:", auth_ok),
    ]
    return "```text\n" + "\n".join(lines) + "\n```"


async def _check_usage_thresholds(bot):
    """Fetch usage and notify if any quota crossed a new 10% band since last check."""
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _fetch_usage)
        if not data:
            return

        alerts = []

        for key, bucket in data.items():
            if not isinstance(bucket, dict):
                continue
            pct = bucket.get("utilization")
            if pct is None:
                continue
            label = _BUCKET_LABELS.get(key, key.replace("_", " ").title())
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
            await bot.send_message(chat_id=OWNER_USER_ID, text=msg)
    except Exception as e:
        log.warning(f"Usage threshold check failed: {e}")


def _time_until(iso_ts: str) -> str:
    try:
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
    except (ValueError, TypeError):
        return "unknown"


# ── Bot command handlers ────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await reply(update, f"Code CLI bridge v{VERSION} · {BUILD_ID}\nType /help for commands.")


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
        "LLM:\n"
        "/llm - show current provider\n"
        "/llm <claude|codex> - switch provider\n"
        "/model - show current provider model\n"
        "/model <name> - set model for current provider\n\n"
        "Info:\n"
        "/usage - Claude usage, or /status output when using Codex\n"
        "/status - version & account info\n"
        "/doctor - health check\n"
        "/history - recent exchanges\n"
        "/cancel - stop running task (immediate)\n"
        "Messages during a task run as side queries\n"
        "/tool - toggle tool message streaming (default: off)\n"
        "/version - show bot version\n\n"
        "Skills (best with Claude):\n"
        "/review /security_review /simplify\n"
        "/pr_comments /release_notes /init\n"
        "/debug /insights /batch\n\n"
        "Everything else is sent as a prompt."
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    provider = _get_provider(chat_id)
    old = _get_session(chat_id, provider)
    _set_session(chat_id, provider, None)
    _save_state()
    await reply(update,
        f"{provider.capitalize()} session cleared (was: {old[:12]}...)." if old else f"Starting fresh with {provider}."
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    provider = _get_provider(chat_id)
    args = " ".join(context.args) if context.args else ""
    if not args:
        await reply(update,"Usage: /resume <session_id>")
        return
    sid = args.strip()
    if provider == "codex" and sid.lower() == "last":
        sid = "last"
    elif not _SESSION_ID_RE.match(sid):
        await reply(update, "Invalid session ID format.")
        return
    _set_session(chat_id, provider, sid)
    _save_state()
    await reply(update, f"{provider.capitalize()} session set to: {sid[:12]}...")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    provider = _get_provider(chat_id)
    sid = _get_session(chat_id, provider)
    cwd = working_dirs.get(chat_id, DEFAULT_CWD)
    model = _get_model(chat_id, provider) or "default"
    msg = (
        f"Provider: {provider}\n"
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
    "quoteroo":   r"C:\Users\w0mb4\Quoteroo",
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
        current_name = next(
            (k for k, v in PROJECTS.items() if os.path.normpath(v) == os.path.normpath(current)),
            None
        )
        if current_name:
            await reply(update, f"Project: {current_name}\nDirectory: {current}")
        else:
            names = "\n".join(f"  {k} → {v}" for k, v in PROJECTS.items())
            await reply(update, f"No active project (cwd: {current})\n\nAvailable:\n{names}")
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

    provider = _get_provider(chat_id)

    # Auto-summarize outgoing project if switching away
    if current_project and current_project != args:
        session_id = _get_session(chat_id, provider)
        if session_id and provider == "claude":
            lock = _get_lock(chat_id)
            if lock.locked():
                await update.message.reply_text("Waiting for current task before switching...")
            async with lock:
                await update.message.reply_text(f"Saving {current_project} summary...")
                typing_task = asyncio.create_task(_keep_typing(update.effective_chat))
                try:
                    loop = asyncio.get_running_loop()
                    summary_result = await loop.run_in_executor(
                        None, lambda: run_claude(
                            "Summarize the current work in this project in 2-3 paragraphs. "
                            "Focus on what was done, what's in progress, and any open questions. "
                            "Output ONLY the summary, no preamble.",
                            session_id=session_id, cwd=current_cwd,
                            model=_get_model(chat_id, provider), chat_id=chat_id,
                        )
                    )
                    summary_text = summary_result.get("text", "")
                    if summary_text:
                        os.makedirs(SUMMARIES_DIR, exist_ok=True)
                        spath = os.path.join(SUMMARIES_DIR, f"{current_project}.md")
                        with open(spath, "w", encoding="utf-8") as f:
                            f.write(f"# {current_project}\n\n")
                            f.write(f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
                            f.write(summary_text)
                except Exception as e:
                    log.warning("Auto-summarize failed: %s", e)
                finally:
                    typing_task.cancel()

        # Auto-clear session
        _set_session(chat_id, provider, None)

    # Switch directory
    working_dirs[chat_id] = target
    _save_state()

    msg = f"Switched to project: {args}\nDirectory: {target}\nSession cleared."

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
        f"or mention the path in your prompt and the active CLI will access it."
    )


async def cmd_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    args = " ".join(context.args).strip().lower() if context.args else ""
    current = _get_provider(chat_id)

    if not args:
        await reply(update, f"Current provider: {current}")
        return

    if args not in SUPPORTED_PROVIDERS:
        await reply(update, "Usage: /llm <claude|codex>")
        return

    providers[chat_id] = args
    _save_state()
    model = _get_model(chat_id, args) or "default"
    session = _get_session(chat_id, args) or "none"
    await reply(update, f"Provider set to: {args}\nModel: {model}\nSession: {session}")


CLAUDE_MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# Ordered low → high for up/down switching
CLAUDE_MODEL_LADDER = [
    ("haiku", "claude-haiku-4-5-20251001"),
    ("sonnet", "claude-sonnet-4-6"),
    ("opus", "claude-opus-4-6"),
]

DEFAULT_CLAUDE_MODEL_ID = "claude-sonnet-4-6"


def _model_name(model_id: str) -> str:
    """Return a friendly name for a model ID."""
    for name, mid in CLAUDE_MODEL_LADDER:
        if mid == model_id:
            return f"{name.capitalize()} ({mid})"
    return model_id


def _ladder_index(model_id: str) -> int:
    for i, (_, mid) in enumerate(CLAUDE_MODEL_LADDER):
        if mid == model_id:
            return i
    return -1


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    provider = _get_provider(chat_id)
    args = " ".join(context.args).strip().lower() if context.args else ""
    current = _get_model(chat_id, provider)
    if provider == "claude" and not current:
        current = DEFAULT_CLAUDE_MODEL_ID

    if not args:
        pretty = _model_name(current) if provider == "claude" and current else current or "default"
        await reply(update, f"Current {provider} model: {pretty}")
        return

    if provider == "claude" and args == "up":
        idx = _ladder_index(current)
        if idx < 0:
            idx = 0  # unknown model, start from bottom
        if idx >= len(CLAUDE_MODEL_LADDER) - 1:
            await reply(update, f"Already on highest: {_model_name(current)}")
            return
        _, new_id = CLAUDE_MODEL_LADDER[idx + 1]
        _set_model(chat_id, provider, new_id)
        _save_state()
        await reply(update, f"Upgraded to: {_model_name(new_id)}")
        return

    if provider == "claude" and args == "down":
        idx = _ladder_index(current)
        if idx < 0:
            idx = len(CLAUDE_MODEL_LADDER) - 1  # unknown model, start from top
        if idx <= 0:
            await reply(update, f"Already on lowest: {_model_name(current)}")
            return
        _, new_id = CLAUDE_MODEL_LADDER[idx - 1]
        _set_model(chat_id, provider, new_id)
        _save_state()
        await reply(update, f"Downgraded to: {_model_name(new_id)}")
        return

    model_id = CLAUDE_MODEL_ALIASES.get(args, args) if provider == "claude" else args
    _set_model(chat_id, provider, model_id)
    _save_state()
    pretty = _model_name(model_id) if provider == "claude" else model_id
    await reply(update, f"{provider.capitalize()} model set to: {pretty}")


# Friendly labels for known bucket keys; unknown keys shown as-is
_BUCKET_LABELS = {
    "five_hour":        "5-hour session",
    "seven_day":        "Weekly quota",
    "seven_day_opus":   "Weekly Opus",
    "daily":            "Daily quota",
    "monthly":          "Monthly quota",
}


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    if _get_provider(update.effective_chat.id) != "claude":
        await reply(update, _format_codex_usage(update.effective_chat.id))
        return

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _fetch_usage)

    if not data:
        await reply(update, "Could not fetch usage data.")
        return

    lines = []
    for key, bucket in data.items():
        if not isinstance(bucket, dict):
            continue
        pct = bucket.get("utilization")
        if pct is None:
            continue
        label = _BUCKET_LABELS.get(key, key.replace("_", " ").title())
        resets_at = bucket.get("resets_at")
        bar = _progress_bar(pct)
        reset_str = f"  resets in {_time_until(resets_at)}" if resets_at else ""
        lines.append(f"{label}\n{bar}  {pct:.0f}%{reset_str}")

    msg = "\n\n".join(lines) if lines else "No usage data available."
    await reply(update, msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    provider = _get_provider(chat_id)
    if provider == "codex":
        await reply(update, _format_codex_usage(chat_id))
        return

    loop = asyncio.get_running_loop()
    def _get_ver():
        try:
            return subprocess.run(
                [_cli_bin(provider), "--version"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:
            return "unknown"
    ver = await loop.run_in_executor(None, _get_ver)

    await reply(update,
        f"Bot: v{VERSION} · {BUILD_ID}\n"
        f"Built: {BUILD_TIME}\n"
        f"Provider: {provider}\n"
        f"{provider.capitalize()} CLI: {ver}\n"
        f"Model: {_get_model(chat_id, provider) or 'default'}\n"
        f"Directory: {working_dirs.get(chat_id, DEFAULT_CWD)}\n"
        f"Session: {_get_session(chat_id, provider) or 'none'}"
    )
    return


async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    provider = _get_provider(update.effective_chat.id)
    loop = asyncio.get_running_loop()
    def _run_doctor():
        try:
            if provider == "claude":
                r = subprocess.run(
                    ["claude", "doctor"], capture_output=True, text=True, timeout=15
                )
                return (r.stdout + r.stderr).strip() or "No output."
            ver = subprocess.run(
                [_cli_bin("codex"), "--version"], capture_output=True, text=True, timeout=10
            )
            login = subprocess.run(
                [_cli_bin("codex"), "login", "status"], capture_output=True, text=True, timeout=10
            )
            return (
                f"Codex CLI: {(ver.stdout or ver.stderr).strip() or 'unknown'}\n"
                f"{(login.stdout + login.stderr).strip() or 'No login status output.'}"
            )
        except Exception as e:
            return f"Error: {e}"
    output = await loop.run_in_executor(None, _run_doctor)
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
    with _active_procs_lock:
        proc = _active_procs.get(chat_id)
    if proc and proc.poll() is None:
        proc.terminate()
        await reply(update, "Cancelled running task.")
    else:
        await reply(update, "Nothing running.")


async def cmd_tool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    chat_id = update.effective_chat.id
    current = _show_tools.get(chat_id, False)
    _show_tools[chat_id] = not current
    _save_state()
    state = "ON" if _show_tools[chat_id] else "OFF"
    await reply(update, f"Tool message streaming: {state}")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return
    await reply(update, f"v{VERSION} · {BUILD_ID}")


DEPLOY_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deploy-lock")


async def cmd_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle deploy lock — pauses the file-watcher auto-restart."""
    if not await is_allowed(update):
        return
    if os.path.exists(DEPLOY_LOCK_FILE):
        os.remove(DEPLOY_LOCK_FILE)
        await reply(update, "🔓 Deploy lock removed — auto-restart resumed.")
    else:
        with open(DEPLOY_LOCK_FILE, "w") as f:
            f.write("")
        await reply(update, "🔒 Deploy lock set — auto-restart paused.\nUse /deploy again to unlock.")


def _store_result_state(chat_id: int, provider: str, result: dict):
    if result.get("session_id"):
        _set_session(chat_id, provider, result["session_id"])
    if result.get("cwd"):
        working_dirs[chat_id] = result["cwd"]
    _save_state()


# ── Message handler (forwarding to Claude CLI) ─────────────────

async def _run_with_streaming(chat_id: int, update: Update, prompt: str):
    """Run a CLI agent with real-time streaming to Telegram."""
    provider = _get_provider(chat_id)
    cwd = working_dirs.get(chat_id, DEFAULT_CWD)
    model = _get_model(chat_id, provider)
    show_tools_flag = _show_tools.get(chat_id, False)

    typing_task = asyncio.create_task(_keep_typing(update.effective_chat))

    # Create streaming queue for Claude provider
    sq = queue.Queue() if provider == "claude" and _bot_ref else None
    consumer_task = None
    if sq:
        consumer_task = asyncio.create_task(
            _stream_consumer(chat_id, sq, _bot_ref, show_tools_flag)
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: run_agent(
                provider, prompt, _get_session(chat_id, provider),
                cwd, model, chat_id=chat_id, stream_queue=sq,
            )
        )
    except subprocess.TimeoutExpired:
        if sq:
            sq.put(("done", ""))
        if consumer_task:
            await consumer_task
        await reply(update, f"{provider.capitalize()} timed out (10 min limit).")
        return
    except Exception as e:
        if sq:
            sq.put(("done", ""))
        if consumer_task:
            await consumer_task
        print(f">>> ERROR: {e}", flush=True)
        await reply(update, f"Error: {e}")
        return
    finally:
        typing_task.cancel()

    # Wait for consumer to finish draining
    streamed = False
    if consumer_task:
        streamed = await consumer_task

    _store_result_state(chat_id, provider, result)

    _record(chat_id, "user", prompt)
    _record(chat_id, "bot", result["text"] or "(empty)")

    # Only send final result if nothing was streamed
    if not streamed:
        await _send_message(update, result["text"])

    if _bot_ref and provider == "claude":
        asyncio.create_task(_check_usage_thresholds(_bot_ref))


async def _run_btw(chat_id: int, update: Update, prompt: str):
    """Run a side query (btw) without blocking or using the main session."""
    provider = _get_provider(chat_id)
    if provider != "claude":
        await reply(update, "Side queries only work with Claude. This will run after the current task.")
        return False  # signal: not handled

    cwd = working_dirs.get(chat_id, DEFAULT_CWD)
    model = _get_model(chat_id, provider)

    c = COLORS
    print(f"\n{c['yellow']}{c['bold']}You (btw):{c['reset']} {prompt}")
    print(f"{c['dim']}  cwd: {cwd}{c['reset']}", flush=True)

    # Fresh call, no session resume, no session save
    cmd = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                cwd=cwd, encoding="utf-8", errors="replace",
            )
        )
        stdout = result.stdout.strip()
        try:
            data = json.loads(stdout)
            response = data.get("result", stdout)
        except (json.JSONDecodeError, TypeError):
            response = stdout or "No response."

        print(f"{c['green']}{c['bold']}Claude (btw):{c['reset']} {response}", flush=True)
        await _send_message(update, response)
        _record(chat_id, "user", f"[btw] {prompt}")
        _record(chat_id, "bot", response)
    except subprocess.TimeoutExpired:
        await reply(update, "Side query timed out.")
    except Exception as e:
        await reply(update, f"Side query error: {e}")

    return True  # handled


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_allowed(update):
        return

    text = update.message.text
    if not text:
        return

    chat_id = update.effective_chat.id
    lock = _get_lock(chat_id)

    if lock.locked():
        # Task is running — handle as a side query instead of queuing
        handled = await _run_btw(chat_id, update, text)
        if handled:
            return
        # If not handled (e.g. codex), fall through to queue
        await update.message.reply_text("Queued — waiting for current task to finish...")

    async with lock:
        await _run_with_streaming(chat_id, update, text)


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
    try:
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
            await _run_with_streaming(chat_id, update, prompt)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
    try:
        ogg_path = os.path.join(tmp_dir, "voice.ogg")
        await file_obj.download_to_drive(ogg_path)

        loop = asyncio.get_running_loop()
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
            await _run_with_streaming(chat_id, update, text)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
    app.add_handler(CommandHandler("llm", cmd_llm))
    app.add_handler(CommandHandler("model", cmd_model))

    # Info
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("tool", cmd_tool))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("deploy", cmd_deploy))

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
        try:
            await application.bot.send_message(
                chat_id=OWNER_USER_ID,
                text=f"Bot restarted — v{VERSION} · {BUILD_ID}\nLast changed: {BUILD_TIME}",
            )
        except Exception as e:
            log.warning("Startup notification failed: %s", e)

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
    DEPLOY_LOCK = os.path.join(os.path.dirname(SCRIPT), ".deploy-lock")
    RESTART_DELAY = 5
    COOLDOWN = 30  # seconds to ignore mtime changes after restart
    SETTLE_TIME = 10  # wait for file changes to settle before restarting
    c = COLORS
    proc = None
    file_changed = threading.Event()
    stop_watcher = threading.Event()
    cooldown_until = [0.0]  # mutable container for thread access
    last_change_time = [0.0]  # track when last change was seen

    def _watch():
        """Poll bot.py mtime every 3s, signal when it changes (after settling)."""
        last_mtime = os.path.getmtime(SCRIPT)
        while not stop_watcher.is_set():
            _time.sleep(3)
            # Pause watcher while deploy lock exists
            if os.path.exists(DEPLOY_LOCK):
                last_mtime = os.path.getmtime(SCRIPT)
                continue
            if _time.time() < cooldown_until[0]:
                last_mtime = os.path.getmtime(SCRIPT)
                continue
            try:
                current = os.path.getmtime(SCRIPT)
                if current != last_mtime:
                    last_mtime = current
                    last_change_time[0] = _time.time()
                    # Don't restart yet — wait for changes to settle
                    continue

                # If a change was detected, wait SETTLE_TIME before triggering
                if last_change_time[0] > 0 and (_time.time() - last_change_time[0]) >= SETTLE_TIME:
                    last_change_time[0] = 0
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
        last_change_time[0] = 0
        cooldown_until[0] = _time.time() + COOLDOWN
        print(f"{c['cyan']}Starting bot (pid will follow)...{c['reset']}", flush=True)

        proc = subprocess.Popen(
            [sys.executable, SCRIPT, "--run"],
            stdout=None, stderr=None,  # inherit terminal
        )

        proc.wait()

        if file_changed.is_set():
            print(f"{c['cyan']}Reloading with updated code...{c['reset']}", flush=True)
            _time.sleep(2)
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

