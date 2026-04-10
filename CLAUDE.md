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
