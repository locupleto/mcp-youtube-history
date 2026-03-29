# MCP YouTube History Server

An MCP server that syncs and searches your YouTube watch history using Google's Data Portability API. History is stored in a local SQLite database for fast keyword search and analytics.

## Features

- **sync_history** — Sync watch history from Google (incremental, 24h cooldown)
- **import_takeout** — Bulk import from Google Takeout JSON/ZIP files
- **search_history** — Search by keyword across titles and channels
- **get_recent_watches** — Recently watched videos grouped by date
- **get_watch_stats** — Top channels, viewing patterns by day/hour, rewatched videos

## Dual-Environment Support

| Mode | Env Var | Available Tools |
|------|---------|----------------|
| `production` | `YOUTUBE_HISTORY_MODE=production` | All 5 tools |
| `readonly` | `YOUTUBE_HISTORY_MODE=readonly` | search, recent, stats only |

Production mode runs on the machine that owns the Google OAuth credentials (e.g., Mac mini). Readonly mode runs on development machines with a copy of the database.

## Prerequisites

- Python 3.11+
- A Google Cloud Platform project with the Data Portability API enabled

## GCP Setup (one-time)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Data Portability API**:
   - Navigate to APIs & Services → Library
   - Search for "Data Portability API"
   - Click Enable
4. Create OAuth credentials:
   - Go to APIs & Services → Credentials
   - Click "Create Credentials" → "OAuth client ID"
   - Application type: **Desktop app**
   - Download the JSON file
5. Place the downloaded JSON file in the `credentials/` directory

## Installation

```bash
git clone https://github.com/locupleto/mcp-youtube-history.git
cd mcp-youtube-history
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Add to your `.mcp.json`:

```json
{
  "youtube-history": {
    "type": "stdio",
    "command": "/path/to/mcp-youtube-history/venv/bin/python3",
    "args": ["/path/to/mcp-youtube-history/youtube_history_mcp_server.py"],
    "env": {
      "YOUTUBE_HISTORY_MODE": "production",
      "YOUTUBE_HISTORY_DB_PATH": "/path/to/history.db"
    }
  }
}
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YOUTUBE_HISTORY_MODE` | `readonly` | `production` or `readonly` |
| `YOUTUBE_HISTORY_DB_PATH` | `./history.db` | Path to SQLite database |
| `YOUTUBE_CREDENTIALS_DIR` | `./credentials` | Directory containing `client_secret.json` |
| `YOUTUBE_TOKEN_DIR` | `./token` | Directory for storing OAuth tokens |

## First Sync

On first run of `sync_history`, a browser window will open for Google OAuth consent. Select the **180-day** access duration for recurring exports. After consent, syncs run automatically using the stored refresh token.

## Batch Sync (cron)

Use `sync_youtube_history.sh` for automated daily sync on production:

```bash
# Add to crontab
0 5 * * * /path/to/mcp-youtube-history/sync_youtube_history.sh
```

The script runs sync, then SCPs the database to the development machine.

## Testing

```bash
source venv/bin/activate
python test_server.py
```

## Troubleshooting

- **"OAuth client_secret.json not found"** — Download from GCP Console and place in `credentials/`
- **"Rate limited"** — Data Portability API allows one export per 24 hours
- **"Token refresh failed"** — Delete `token/token.json` and re-authorize
- **"production only" message** — Set `YOUTUBE_HISTORY_MODE=production` in `.mcp.json`
