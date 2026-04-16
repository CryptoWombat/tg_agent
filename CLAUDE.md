# CLAUDE.md — tg_agent

## Project Summary

**Read first:** `C:\Users\w0mb4\project-summaries\tg_agent.md` — contains current state, version, recent work, and what's in progress. This file is auto-maintained and should be checked before every session.

**Scan for updates:** Run `bash ~/project-summaries/scan-updates.sh` to detect new commits across all projects since summaries were last updated.

## Golden Rule

**Do everything yourself.** Never ask Andre to run migrations, tests, SQL, deployments, or any manual task. Use CLI tools, APIs, or write new tooling if needed. The only exception: asking for API keys/tokens — and even then, provide clear instructions on how to obtain them.

**Secrets vault:** All credentials live in `conductor_secrets` table in Konduktor Supabase (`okcqeyjufahbsreaqysp`). Check there before asking Andre. When Andre provides a new key, **immediately store it in the vault** — never leave credentials only in conversation history.

**Never block the conversation.** If something fails after 2-3 attempts, dispatch it to a background agent and stay responsive. Never run long-running commands in the foreground. The pattern: try, try, try, then park it and move on.

## Project Overview

**tg_agent** is a Telegram bot that bridges Andre's Telegram to Claude Code and Codex CLIs. Multi-provider AI assistant with project switching, streaming responses, voice transcription, and persistent sessions.

- **Repo:** CryptoWombat/tg_agent
- **Location:** C:\Users\w0mb4\tg_agent
- **Runs:** Locally on Windows, auto-starts via `tg_bot.bat` in Startup folder

## Key Patterns

- `bot.py` (~1850 lines) — main bot, auto-reloads on file change
- `state.json` — per-chat persistent state (sessions, dirs, history, models)
- `project-summaries/` — centralized summaries, auto-generated on `/project` switch
- Session persistence via Claude CLI `--resume`

## Commands

`/project`, `/llm`, `/model`, `/deploy`, `/cd`, `/cancel`, `/newsession`, `/status`, `/usage`, `/help`

## NotebookLM discipline — MANDATORY, BI-DIRECTIONAL

The full canonical rule lives in `~/.claude/CLAUDE.md` under "NotebookLM discipline — MANDATORY, non-negotiable, BI-DIRECTIONAL". Read it. The summary below is project-scoped.

**tg_agent work uses notebook alias `tg-agent`.** Create on first use:
```bash
nlm notebook get tg-agent 2>/dev/null || {
  NB_ID=$(nlm notebook create "Telegram Agent" --quiet)
  nlm alias set tg-agent "$NB_ID"
}
```

**Applies to**: every session, every spawned sub-agent, every scheduled task, every autonomous loop. There is no exempt context.

**At session start**:
1. `export PYTHONIOENCODING=utf-8 && (nlm login --check || nlm login --profile andrey)`
2. Drain `/c/Users/w0mb4/.claude/nlm-pending/` (push each pending file to its project notebook by filename, delete on success)
3. `nlm notebook query tg-agent "current state, deferred work, recent slash command changes"` — pull fresh context

**At end of every shipped batch**:
- `nlm source add tg-agent --text "$(cat /tmp/summary.md)" --title "tg_agent v<X.Y.Z> session — <YYYY-MM-DD>"` — versions, surfaces shipped, vault touched, deferred work, process learnings.
- For CLAUDE.md / slash command additions / session-handler changes → push fresh source titled `tg_agent <docname> (current — <YYYY-MM-DD>)`
- For new bot tokens / env vars → push source `tg_agent infra <YYYY-MM-DD>`

**On sync failure**: do NOT block work. Retry 3× → re-auth once → spool to `/c/Users/w0mb4/.claude/nlm-pending/<timestamp>-tg-agent.md` → keep working → end response with red flag IN ALL CAPS:

```
🚨 NLM SYNC FAILED 🚨
Pending summary at: /c/Users/w0mb4/.claude/nlm-pending/<filename>
Last error: <one line>
```

**Autonomous mode is not exempt.** Keep retrying opportunistically and surface the failure in the run's reporting channel.

**If you ship and skip NLM, the work is unfinished. No exceptions.**
