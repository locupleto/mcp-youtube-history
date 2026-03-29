#!/usr/bin/env python3
"""
MCP Server for YouTube Watch History

Syncs and searches your YouTube watch history using Google's Data Portability API.
History is stored in a local SQLite database for fast search and analytics.

Tools:
- sync_history: Sync watch history from Google (production mode only)
- import_takeout: Import from a Google Takeout JSON/ZIP file (production mode only)
- search_history: Search watch history by keyword
- get_recent_watches: Get recently watched videos
- get_watch_stats: View watching statistics and patterns

Environment Variables:
- YOUTUBE_HISTORY_MODE: "production" (sync enabled) or "readonly" (search only)
- YOUTUBE_HISTORY_DB_PATH: Path to SQLite database file
- YOUTUBE_CREDENTIALS_DIR: Path to directory containing client_secret.json
- YOUTUBE_TOKEN_DIR: Path to directory for storing OAuth tokens
"""

import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any, Sequence

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("youtube-history-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SERVER_MODE = os.environ.get("YOUTUBE_HISTORY_MODE", "readonly")
DB_PATH = os.environ.get("YOUTUBE_HISTORY_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db"))
CREDENTIALS_DIR = os.environ.get("YOUTUBE_CREDENTIALS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials"))
TOKEN_DIR = os.environ.get("YOUTUBE_TOKEN_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "token"))

SCOPE = "https://www.googleapis.com/auth/dataportability.myactivity.youtube"
SCOPE_PREFIX = "https://www.googleapis.com/auth/dataportability."
API_SERVICE = "dataportability"
API_VERSION = "v1"

PRODUCTION_ONLY_MSG = "This tool is only available in production mode. Set YOUTUBE_HISTORY_MODE=production to enable sync/import."

# Initialize MCP server
server = Server("youtube-history")

# Video ID extraction patterns
VIDEO_ID_PATTERN = re.compile(
    r'(?:youtube\.com/watch\?.*?v=|youtu\.be/|youtube\.com/shorts/)([\w-]+)'
)

# ---------------------------------------------------------------------------
# SQLite Database Layer
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize database with schema. Returns open connection."""
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            title TEXT,
            channel_name TEXT,
            channel_url TEXT,
            watched_at TEXT NOT NULL,
            video_url TEXT,
            synced_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(video_id, watched_at)
        );

        CREATE INDEX IF NOT EXISTS idx_watched_at ON watch_history(watched_at);
        CREATE INDEX IF NOT EXISTS idx_video_id ON watch_history(video_id);
        CREATE INDEX IF NOT EXISTS idx_channel_name ON watch_history(channel_name);

        CREATE TABLE IF NOT EXISTS sync_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_started_at TEXT NOT NULL,
            sync_completed_at TEXT,
            job_id TEXT,
            status TEXT NOT NULL,
            entries_added INTEGER DEFAULT 0,
            entries_skipped INTEGER DEFAULT 0,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS transcript_cache (
            video_id TEXT PRIMARY KEY,
            transcript TEXT,
            cached_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def get_db() -> sqlite3.Connection:
    """Get a database connection."""
    return init_db(DB_PATH)


def insert_watch_entries(conn: sqlite3.Connection, entries: list[dict]) -> tuple[int, int]:
    """Insert watch history entries with dedup. Returns (added, skipped)."""
    added = 0
    skipped = 0
    for entry in entries:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO watch_history
                   (video_id, title, channel_name, channel_url, watched_at, video_url)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (entry['video_id'], entry.get('title'), entry.get('channel_name'),
                 entry.get('channel_url'), entry['watched_at'], entry.get('video_url'))
            )
            if conn.total_changes > 0:
                # Check if the last insert actually added a row
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    added += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        except sqlite3.Error:
            skipped += 1
    conn.commit()
    return added, skipped


def get_last_sync_time(conn: sqlite3.Connection) -> str | None:
    """Get the most recent successful sync completion time."""
    row = conn.execute(
        "SELECT sync_completed_at FROM sync_metadata WHERE status='completed' ORDER BY sync_completed_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def record_sync(conn: sqlite3.Connection, started_at: str, completed_at: str | None = None,
                job_id: str | None = None, status: str = "started",
                entries_added: int = 0, entries_skipped: int = 0,
                error_message: str | None = None) -> int:
    """Record a sync operation in metadata. Returns the sync ID."""
    cursor = conn.execute(
        """INSERT INTO sync_metadata
           (sync_started_at, sync_completed_at, job_id, status, entries_added, entries_skipped, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (started_at, completed_at, job_id, status, entries_added, entries_skipped, error_message)
    )
    conn.commit()
    return cursor.lastrowid


def update_sync(conn: sqlite3.Connection, sync_id: int, **kwargs):
    """Update a sync metadata record."""
    sets = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [sync_id]
    conn.execute(f"UPDATE sync_metadata SET {sets} WHERE id=?", values)
    conn.commit()


# ---------------------------------------------------------------------------
# Video ID and Entry Parsing
# ---------------------------------------------------------------------------

def extract_video_id(url: str | None) -> str | None:
    """Extract video ID from a YouTube URL."""
    if not url:
        return None
    match = VIDEO_ID_PATTERN.search(url)
    return match.group(1) if match else None


def parse_activity_entry(entry: dict) -> dict | None:
    """Parse a single MyActivity JSON entry into a watch record.

    Returns None for entries that should be skipped (non-video, ads, etc).
    """
    title = entry.get("title", "")

    # Skip non-watch entries
    if not title.startswith("Watched "):
        return None

    title_url = entry.get("titleUrl")
    video_id = extract_video_id(title_url)
    if not video_id:
        return None

    # Strip "Watched " prefix
    clean_title = title[8:] if title.startswith("Watched ") else title

    # Extract channel info
    channel_name = None
    channel_url = None
    subtitles = entry.get("subtitles")
    if subtitles and isinstance(subtitles, list) and len(subtitles) > 0:
        channel_name = subtitles[0].get("name")
        channel_url = subtitles[0].get("url")

    watched_at = entry.get("time", "")

    return {
        "video_id": video_id,
        "title": clean_title,
        "channel_name": channel_name,
        "channel_url": channel_url,
        "watched_at": watched_at,
        "video_url": title_url,
    }


def parse_activity_json(data: list[dict]) -> list[dict]:
    """Parse a list of MyActivity entries, filtering out non-video entries."""
    entries = []
    for item in data:
        parsed = parse_activity_entry(item)
        if parsed:
            entries.append(parsed)
    return entries


# ---------------------------------------------------------------------------
# OAuth Authentication Layer
# ---------------------------------------------------------------------------

def _get_client_secret_path() -> str:
    """Get path to client_secret.json."""
    # Look for any JSON file in credentials dir (Google names it various ways)
    if os.path.isdir(CREDENTIALS_DIR):
        for f in os.listdir(CREDENTIALS_DIR):
            if f.endswith('.json') and f != '.gitkeep':
                return os.path.join(CREDENTIALS_DIR, f)
    return os.path.join(CREDENTIALS_DIR, "client_secret.json")


def _get_token_path() -> str:
    """Get path to stored token."""
    return os.path.join(TOKEN_DIR, "token.json")


def validate_credentials_dir():
    """Validate that OAuth credentials exist."""
    path = _get_client_secret_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"OAuth client_secret.json not found in {CREDENTIALS_DIR}. "
            "Download it from Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs."
        )


def get_credentials():
    """Get valid OAuth credentials. Triggers browser flow if needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = _get_token_path()
    scopes = [SCOPE]
    creds = None

    # Try loading stored token
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, scopes)
        except Exception as e:
            logger.warning(f"Failed to load stored token: {e}")
            creds = None

    # Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _store_credentials(creds)
            logger.info("Refreshed OAuth token")
        except Exception as e:
            logger.warning(f"Token refresh failed: {e}")
            creds = None

    # Run OAuth flow if no valid credentials
    if not creds or not creds.valid:
        validate_credentials_dir()
        client_secret = _get_client_secret_path()
        flow = InstalledAppFlow.from_client_secrets_file(client_secret, scopes)
        creds = flow.run_local_server(port=0, open_browser=True)
        _store_credentials(creds)
        logger.info("Completed OAuth flow and stored new token")

    return creds


def _store_credentials(creds):
    """Store OAuth credentials to disk."""
    os.makedirs(TOKEN_DIR, exist_ok=True)
    token_path = _get_token_path()
    with open(token_path, 'w') as f:
        f.write(creds.to_json())


# ---------------------------------------------------------------------------
# Data Portability API Layer
# ---------------------------------------------------------------------------

def get_api_client(creds):
    """Build the Data Portability API client."""
    from googleapiclient.discovery import build
    return build(API_SERVICE, API_VERSION, credentials=creds)


def initiate_archive(api_client, start_time: str | None = None, end_time: str | None = None) -> tuple[str, str]:
    """Initiate a portability archive. Returns (job_id, access_type).

    Raises googleapiclient.errors.HttpError on API errors.
    """
    body = {"resources": ["myactivity.youtube"]}
    if start_time:
        body["start_time"] = start_time
    if end_time:
        body["end_time"] = end_time

    response = api_client.portabilityArchive().initiate(body=body).execute()
    return response["archiveJobId"], response.get("accessType", "unknown")


async def poll_archive_state(api_client, job_id: str) -> list[str]:
    """Poll archive state with exponential backoff until complete. Returns signed URLs."""
    delay = 3.0
    max_delay = 3600.0
    multiplier = 1.5

    get_state = api_client.archiveJobs().getPortabilityArchiveState(
        name=f'archiveJobs/{job_id}/portabilityArchiveState'
    )

    while True:
        state = get_state.execute()
        current_state = state.get("state", "UNKNOWN")
        logger.info(f"Archive state: {current_state}")

        if current_state == "COMPLETE":
            return state.get("urls", [])
        elif current_state == "FAILED":
            raise RuntimeError(f"Archive job {job_id} failed")
        elif current_state == "CANCELLED":
            raise RuntimeError(f"Archive job {job_id} was cancelled")

        # IN_PROGRESS — wait and retry
        await asyncio.sleep(delay)
        delay = min(delay * multiplier, max_delay)


def download_and_parse_archive(urls: list[str]) -> list[dict]:
    """Download ZIP archives from signed URLs and parse MyActivity JSON."""
    all_entries = []

    for url in urls:
        logger.info(f"Downloading archive from signed URL...")
        response = urllib.request.urlopen(url)
        data = response.read()

        try:
            zf = zipfile.ZipFile(io.BytesIO(data), 'r')
            for info in zf.infolist():
                if 'My Activity' in info.filename and info.filename.endswith('.json'):
                    content = json.loads(zf.read(info))
                    if isinstance(content, list):
                        entries = parse_activity_json(content)
                        all_entries.extend(entries)
                        logger.info(f"Parsed {len(entries)} entries from {info.filename}")
        except zipfile.BadZipFile:
            # Maybe it's raw JSON
            try:
                content = json.loads(data)
                if isinstance(content, list):
                    entries = parse_activity_json(content)
                    all_entries.extend(entries)
            except json.JSONDecodeError:
                logger.error("Downloaded file is neither a valid ZIP nor JSON")

    return all_entries


# ---------------------------------------------------------------------------
# Helper: Parse RESOURCE_EXHAUSTED error
# ---------------------------------------------------------------------------

def parse_cooldown_error(error) -> str | None:
    """Extract retry timestamp from RESOURCE_EXHAUSTED error."""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(error, HttpError) and error.resp.status == 429:
            details = error.error_details
            if details and len(details) > 0:
                metadata = details[0].get("metadata", {})
                return metadata.get("timestamp_after_24hrs")
    except Exception:
        pass
    return None


def parse_already_exists_error(error) -> str | None:
    """Extract existing job_id from ALREADY_EXISTS error."""
    try:
        from googleapiclient.errors import HttpError
        if isinstance(error, HttpError) and error.resp.status == 409:
            details = error.error_details
            if details and len(details) > 0:
                metadata = details[0].get("metadata", {})
                return metadata.get("job_id")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# MCP Tool Handlers
# ---------------------------------------------------------------------------

async def handle_sync_history(arguments: dict) -> Sequence[TextContent]:
    """Sync watch history from Google Data Portability API."""
    if SERVER_MODE != "production":
        return [TextContent(type="text", text=PRODUCTION_ONLY_MSG)]

    start_time = arguments.get("start_time")
    end_time = arguments.get("end_time")
    sync_started = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    sync_id = record_sync(conn, started_at=sync_started)

    try:
        # Get OAuth credentials
        creds = get_credentials()
        api_client = get_api_client(creds)

        # Use last sync time if no start_time provided
        if not start_time:
            start_time = get_last_sync_time(conn)
            if start_time:
                logger.info(f"Incremental sync from {start_time}")

        # Initiate archive
        try:
            job_id, access_type = initiate_archive(api_client, start_time, end_time)
            logger.info(f"Archive job initiated: {job_id} (access: {access_type})")
        except Exception as e:
            # Check for cooldown
            retry_time = parse_cooldown_error(e)
            if retry_time:
                msg = f"Rate limited — you can sync again after {retry_time}"
                update_sync(conn, sync_id, status="rate_limited", error_message=msg,
                           sync_completed_at=datetime.now(timezone.utc).isoformat())
                conn.close()
                return [TextContent(type="text", text=msg)]

            # Check for existing job
            existing_job = parse_already_exists_error(e)
            if existing_job:
                job_id = existing_job
                logger.info(f"Resuming existing archive job: {job_id}")
            else:
                raise

        update_sync(conn, sync_id, job_id=job_id, status="polling")

        # Poll until complete
        urls = await poll_archive_state(api_client, job_id)

        # Download and parse
        entries = download_and_parse_archive(urls)

        # Insert into database
        added, skipped = insert_watch_entries(conn, entries)

        # Get total count
        total = conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0]

        completed_at = datetime.now(timezone.utc).isoformat()
        update_sync(conn, sync_id, status="completed", sync_completed_at=completed_at,
                    entries_added=added, entries_skipped=skipped)

        result = (f"Sync completed successfully.\n"
                  f"New entries: {added}\n"
                  f"Duplicates skipped: {skipped}\n"
                  f"Total entries in database: {total}")

        conn.close()
        return [TextContent(type="text", text=result)]

    except Exception as e:
        error_msg = f"Sync failed: {str(e)}"
        logger.error(error_msg)
        update_sync(conn, sync_id, status="failed",
                    error_message=str(e),
                    sync_completed_at=datetime.now(timezone.utc).isoformat())
        conn.close()
        return [TextContent(type="text", text=error_msg)]


async def handle_import_takeout(arguments: dict) -> Sequence[TextContent]:
    """Import watch history from a Google Takeout JSON or ZIP file."""
    if SERVER_MODE != "production":
        return [TextContent(type="text", text=PRODUCTION_ONLY_MSG)]

    file_path = arguments.get("file_path")
    if not file_path:
        return [TextContent(type="text", text="Error: 'file_path' parameter is required")]

    if not os.path.exists(file_path):
        return [TextContent(type="text", text=f"Error: File not found: {file_path}")]

    try:
        entries = []

        if file_path.endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as zf:
                for info in zf.infolist():
                    if 'watch-history' in info.filename.lower() and info.filename.endswith('.json'):
                        content = json.loads(zf.read(info))
                        if isinstance(content, list):
                            entries.extend(parse_activity_json(content))
                    elif 'My Activity' in info.filename and info.filename.endswith('.json'):
                        content = json.loads(zf.read(info))
                        if isinstance(content, list):
                            entries.extend(parse_activity_json(content))
        elif file_path.endswith('.json'):
            with open(file_path, 'r', encoding='utf-8') as f:
                content = json.load(f)
            if isinstance(content, list):
                entries = parse_activity_json(content)
        else:
            return [TextContent(type="text", text="Error: File must be .json or .zip")]

        if not entries:
            return [TextContent(type="text", text="No watch history entries found in file.")]

        conn = get_db()
        added, skipped = insert_watch_entries(conn, entries)
        total = conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0]
        conn.close()

        result = (f"Import completed.\n"
                  f"New entries: {added}\n"
                  f"Duplicates skipped: {skipped}\n"
                  f"Total entries in database: {total}")

        return [TextContent(type="text", text=result)]

    except Exception as e:
        error_msg = f"Import failed: {str(e)}"
        logger.error(error_msg)
        return [TextContent(type="text", text=error_msg)]


async def handle_search_history(arguments: dict) -> Sequence[TextContent]:
    """Search watch history by keyword."""
    query = arguments.get("query", "").strip()
    if not query:
        return [TextContent(type="text", text="Error: 'query' parameter is required")]

    start_date = arguments.get("start_date")
    end_date = arguments.get("end_date")
    limit = min(arguments.get("limit", 20), 100)

    conn = get_db()

    sql = "SELECT title, channel_name, watched_at, video_url FROM watch_history WHERE (title LIKE ? OR channel_name LIKE ?)"
    params: list[Any] = [f"%{query}%", f"%{query}%"]

    if start_date:
        sql += " AND watched_at >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND watched_at <= ?"
        params.append(end_date)

    sql += " ORDER BY watched_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        return [TextContent(type="text", text=f"No results found for '{query}'.")]

    lines = [f"Search results for '{query}' ({len(rows)} matches):\n"]
    for title, channel, watched_at, url in rows:
        date_str = watched_at[:10] if watched_at else "unknown"
        channel_str = f" — {channel}" if channel else ""
        url_str = f"\n  {url}" if url else ""
        lines.append(f"- [{date_str}] {title}{channel_str}{url_str}")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_get_recent_watches(arguments: dict) -> Sequence[TextContent]:
    """Get recently watched videos."""
    days = arguments.get("days", 7)
    if not isinstance(days, int) or days < 1:
        return [TextContent(type="text", text="Error: 'days' must be a positive integer")]

    limit = min(arguments.get("limit", 50), 200)

    conn = get_db()
    rows = conn.execute(
        """SELECT title, channel_name, watched_at, video_url FROM watch_history
           WHERE watched_at >= datetime('now', ?)
           ORDER BY watched_at DESC LIMIT ?""",
        (f"-{days} days", limit)
    ).fetchall()
    conn.close()

    if not rows:
        return [TextContent(type="text", text=f"No watch history found in the last {days} days.")]

    # Group by date
    grouped: dict[str, list] = {}
    for title, channel, watched_at, url in rows:
        date_key = watched_at[:10] if watched_at else "unknown"
        if date_key not in grouped:
            grouped[date_key] = []
        grouped[date_key].append((title, channel, url))

    lines = [f"Recent watches (last {days} days, {len(rows)} videos):\n"]
    for date_key in sorted(grouped.keys(), reverse=True):
        lines.append(f"\n**{date_key}**")
        for title, channel, url in grouped[date_key]:
            channel_str = f" — {channel}" if channel else ""
            url_str = f"\n  {url}" if url else ""
            lines.append(f"  - {title}{channel_str}{url_str}")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_get_watch_stats(arguments: dict) -> Sequence[TextContent]:
    """Get watching statistics and patterns."""
    start_date = arguments.get("start_date")
    end_date = arguments.get("end_date")

    conn = get_db()

    where = "WHERE 1=1"
    params: list[Any] = []
    if start_date:
        where += " AND watched_at >= ?"
        params.append(start_date)
    if end_date:
        where += " AND watched_at <= ?"
        params.append(end_date)

    # Total count
    total = conn.execute(f"SELECT COUNT(*) FROM watch_history {where}", params).fetchone()[0]

    if total == 0:
        conn.close()
        return [TextContent(type="text", text="No watch history data available.")]

    # Date range
    date_range = conn.execute(
        f"SELECT MIN(watched_at), MAX(watched_at) FROM watch_history {where}", params
    ).fetchone()

    # Top channels
    top_channels = conn.execute(
        f"""SELECT channel_name, COUNT(*) as cnt FROM watch_history
            {where} AND channel_name IS NOT NULL
            GROUP BY channel_name ORDER BY cnt DESC LIMIT 20""",
        params
    ).fetchall()

    # Watches by day of week (0=Sunday in SQLite strftime %w)
    day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    by_day = conn.execute(
        f"""SELECT CAST(strftime('%w', watched_at) AS INTEGER) as dow, COUNT(*) as cnt
            FROM watch_history {where}
            GROUP BY dow ORDER BY dow""",
        params
    ).fetchall()

    # Watches by hour
    by_hour = conn.execute(
        f"""SELECT CAST(strftime('%H', watched_at) AS INTEGER) as hour, COUNT(*) as cnt
            FROM watch_history {where}
            GROUP BY hour ORDER BY hour""",
        params
    ).fetchall()

    # Most rewatched
    rewatched = conn.execute(
        f"""SELECT title, video_id, COUNT(*) as cnt FROM watch_history
            {where}
            GROUP BY video_id HAVING cnt > 1 ORDER BY cnt DESC LIMIT 10""",
        params
    ).fetchall()

    conn.close()

    # Format output
    lines = [f"YouTube Watch Statistics\n{'='*40}\n"]
    lines.append(f"Total videos watched: {total}")
    lines.append(f"Date range: {date_range[0][:10] if date_range[0] else '?'} to {date_range[1][:10] if date_range[1] else '?'}")

    lines.append(f"\nTop Channels:")
    for name, cnt in top_channels:
        lines.append(f"  {cnt:>5}  {name}")

    lines.append(f"\nWatches by Day of Week:")
    for dow, cnt in by_day:
        day_name = day_names[dow] if 0 <= dow <= 6 else f"Day {dow}"
        bar = "█" * max(1, cnt * 30 // max(c for _, c in by_day))
        lines.append(f"  {day_name:<10} {cnt:>5}  {bar}")

    lines.append(f"\nWatches by Hour (UTC):")
    max_hourly = max((c for _, c in by_hour), default=1)
    for hour, cnt in by_hour:
        bar = "█" * max(1, cnt * 30 // max_hourly)
        lines.append(f"  {hour:02d}:00  {cnt:>5}  {bar}")

    if rewatched:
        lines.append(f"\nMost Rewatched Videos:")
        for title, vid, cnt in rewatched:
            lines.append(f"  {cnt:>3}x  {title}")

    return [TextContent(type="text", text="\n".join(lines))]


# ---------------------------------------------------------------------------
# MCP Tool Definitions and Dispatch
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    tools = []

    if SERVER_MODE == "production":
        tools.append(Tool(
            name="sync_history",
            description="Sync YouTube watch history from Google via the Data Portability API. "
                        "Requires OAuth consent on first run (opens browser). "
                        "Can only be called once every 24 hours. "
                        "Uses incremental sync by default (from last sync time).",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "Start time for sync in ISO 8601 format (e.g., 2025-01-01T00:00:00Z). Defaults to last sync time."
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time for sync in ISO 8601 format. Defaults to now."
                    }
                },
                "required": []
            }
        ))

        tools.append(Tool(
            name="import_takeout",
            description="Import YouTube watch history from a Google Takeout JSON or ZIP file. "
                        "Deduplicates against existing entries.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the Takeout .json or .zip file"
                    }
                },
                "required": ["file_path"]
            }
        ))

    tools.extend([
        Tool(
            name="search_history",
            description="Search YouTube watch history by keyword. "
                        "Searches across video titles and channel names.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword(s)"
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date filter (ISO 8601, e.g., 2025-01-01)"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date filter (ISO 8601)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default 20, max 100)",
                        "default": 20,
                        "minimum": 1,
                        "maximum": 100
                    }
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="get_recent_watches",
            description="Get recently watched YouTube videos, grouped by date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (default 7)",
                        "default": 7,
                        "minimum": 1
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default 50, max 200)",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 200
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="get_watch_stats",
            description="Get YouTube watching statistics: top channels, viewing patterns by day/hour, most rewatched videos.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date filter (ISO 8601)"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date filter (ISO 8601)"
                    }
                },
                "required": []
            }
        ),
    ])

    return tools


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
    """Handle tool calls."""
    if name == "sync_history":
        return await handle_sync_history(arguments)
    elif name == "import_takeout":
        return await handle_import_takeout(arguments)
    elif name == "search_history":
        return await handle_search_history(arguments)
    elif name == "get_recent_watches":
        return await handle_get_recent_watches(arguments)
    elif name == "get_watch_stats":
        return await handle_get_watch_stats(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Batch Entry Point (for cron jobs)
# ---------------------------------------------------------------------------

def sync_history_batch():
    """Non-MCP entry point for cron-based sync.

    Runs sync_history synchronously. Prints results to stdout.
    Exits with 0 on success, 1 on failure.
    """
    async def _run():
        return await handle_sync_history({})

    try:
        # Ensure DB is initialized
        init_db(DB_PATH)

        result = asyncio.run(_run())
        for content in result:
            print(content.text)

        # Check if it was an error
        for content in result:
            if content.text.startswith("Sync failed:") or content.text.startswith("Error:"):
                sys.exit(1)

    except Exception as e:
        print(f"Batch sync error: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    """Main entry point for the MCP server."""
    logger.info(f"Starting YouTube History MCP Server (mode={SERVER_MODE})...")

    try:
        init_db(DB_PATH)
        logger.info(f"Database initialized at {DB_PATH}")

        async with stdio_server() as (read_stream, write_stream):
            logger.info("MCP Server ready and listening on stdio")
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options()
            )
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
