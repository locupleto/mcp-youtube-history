# CLAUDE.md

## Overview

MCP server for syncing and searching YouTube watch history. Uses Google's Data Portability API to fetch history across all devices (phone, iPad, computers) tied to a Google account. Data stored in local SQLite.

## Architecture

- Single-file server: `youtube_history_mcp_server.py`
- Follows the same pattern as `mcp-gemini-video`
- Two modes: `production` (full sync) and `readonly` (search only)
- Batch entry point `sync_history_batch()` for cron jobs

## Tools

| Tool | Mode | Use When |
|------|------|----------|
| `sync_history` | production | Sync latest history from Google (24h cooldown) |
| `import_takeout` | production | Bulk import from Takeout JSON/ZIP |
| `search_history` | both | Find videos by keyword in title or channel |
| `get_recent_watches` | both | See what was watched recently |
| `get_watch_stats` | both | Analyze viewing patterns and top channels |

## Database

SQLite with three tables:
- `watch_history` — video_id, title, channel, watched_at (UNIQUE on video_id + watched_at)
- `sync_metadata` — audit trail of sync operations
- `transcript_cache` — placeholder for future Gemini transcript integration

## Environment Variables

- `YOUTUBE_HISTORY_MODE` — `production` or `readonly` (default: readonly)
- `YOUTUBE_HISTORY_DB_PATH` — path to SQLite database
- `YOUTUBE_CREDENTIALS_DIR` — directory with `client_secret.json`
- `YOUTUBE_TOKEN_DIR` — directory for OAuth token storage
