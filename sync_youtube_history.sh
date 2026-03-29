#!/bin/bash
# sync_youtube_history.sh — Daily cron job for Mac mini production
# Syncs YouTube watch history via Data Portability API, then SCPs DB to Mac Studio
#
# Crontab entry:
#   0 5 * * * /path/to/mcp-youtube-history/sync_youtube_history.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
LOG_DIR="$HOME/Public/youtube-history/logs"
LOG_FILE="$LOG_DIR/sync_$(date +%Y%m%d).log"
DB_PATH="$HOME/Public/youtube-history/history.db"

# Mac Studio SCP target (via Tailscale)
MACSTUDIO_USER="urban"
MACSTUDIO_HOST="100.84.109.72"
MACSTUDIO_KEY="$HOME/.ssh/macstudio"
MACSTUDIO_DEST="/Volumes/Work/youtube-history/"

mkdir -p "$LOG_DIR"

echo "=== YouTube History Sync $(date) ===" >> "$LOG_FILE"

# Run sync via batch entry point
YOUTUBE_HISTORY_DB_PATH="$DB_PATH" \
YOUTUBE_HISTORY_MODE="production" \
YOUTUBE_CREDENTIALS_DIR="$SCRIPT_DIR/credentials" \
YOUTUBE_TOKEN_DIR="$SCRIPT_DIR/token" \
$VENV -c "
from youtube_history_mcp_server import sync_history_batch
sync_history_batch()
" >> "$LOG_FILE" 2>&1

SYNC_EXIT=$?

if [ $SYNC_EXIT -eq 0 ]; then
    echo "Sync completed successfully. SCPing to Mac Studio..." >> "$LOG_FILE"
    scp -i "$MACSTUDIO_KEY" "$DB_PATH" "$MACSTUDIO_USER@$MACSTUDIO_HOST:$MACSTUDIO_DEST" >> "$LOG_FILE" 2>&1
    SCP_EXIT=$?
    if [ $SCP_EXIT -eq 0 ]; then
        echo "SCP to Mac Studio completed." >> "$LOG_FILE"
    else
        echo "ERROR: SCP to Mac Studio failed (exit $SCP_EXIT)" >> "$LOG_FILE"
    fi
else
    echo "ERROR: Sync failed (exit $SYNC_EXIT). Skipping SCP." >> "$LOG_FILE"
fi

echo "=== Done $(date) ===" >> "$LOG_FILE"
