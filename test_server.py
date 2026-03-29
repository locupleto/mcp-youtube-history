#!/usr/bin/env python3
"""
Tests for YouTube History MCP Server

Unit tests that run without API access. Tests cover:
- Group 1: Video ID extraction from URLs
- Group 2: Activity entry parsing
- Group 3: Database operations (in-memory SQLite)
- Group 4: Search logic
- Group 5: Import handler
- Group 6: Tool validation and readonly mode

Acceptance tests (require OAuth) are documented at the bottom as manual steps.
"""

import json
import os
import sqlite3
import sys
import tempfile
import zipfile

# Add server directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from youtube_history_mcp_server import (
    extract_video_id,
    parse_activity_entry,
    parse_activity_json,
    init_db,
    insert_watch_entries,
    get_last_sync_time,
    record_sync,
    update_sync,
    PRODUCTION_ONLY_MSG,
)

# Track test results
passed = 0
failed = 0
errors = []


def test(name: str, condition: bool, detail: str = ""):
    """Record a test result."""
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        msg = f"  ✗ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(name)


# =========================================================================
# Group 1: Video ID Extraction
# =========================================================================

def test_video_id_extraction():
    print("\n--- Group 1: Video ID Extraction ---")

    # Standard URL
    test("Standard youtube.com URL",
         extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ")

    # Short URL
    test("Short youtu.be URL",
         extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ")

    # URL with extra params
    test("URL with extra params",
         extract_video_id("https://www.youtube.com/watch?v=abc123&t=120&list=PLxyz") == "abc123")

    # Shorts URL
    test("Shorts URL",
         extract_video_id("https://youtube.com/shorts/abc123") == "abc123")

    # Invalid URL
    test("Invalid URL returns None",
         extract_video_id("https://example.com/page") is None)

    # Empty/None
    test("None URL returns None",
         extract_video_id(None) is None)

    test("Empty string returns None",
         extract_video_id("") is None)


# =========================================================================
# Group 2: Activity Entry Parsing
# =========================================================================

def test_activity_parsing():
    print("\n--- Group 2: Activity Entry Parsing ---")

    # Valid entry with all fields
    entry = {
        "header": "YouTube",
        "title": "Watched How to Build an MCP Server",
        "titleUrl": "https://www.youtube.com/watch?v=abc123",
        "subtitles": [{"name": "TechChannel", "url": "https://www.youtube.com/channel/UCxyz"}],
        "time": "2025-11-27T10:30:45.123Z"
    }
    result = parse_activity_entry(entry)
    test("Valid entry parses correctly",
         result is not None and result["video_id"] == "abc123",
         f"got {result}")

    test("Title has 'Watched' prefix stripped",
         result is not None and result["title"] == "How to Build an MCP Server",
         f"got title={result.get('title') if result else None}")

    test("Channel name extracted",
         result is not None and result["channel_name"] == "TechChannel")

    # Entry missing subtitles
    entry_no_sub = {
        "header": "YouTube",
        "title": "Watched Some Video",
        "titleUrl": "https://www.youtube.com/watch?v=def456",
        "time": "2025-11-27T10:30:45.123Z"
    }
    result2 = parse_activity_entry(entry_no_sub)
    test("Entry without subtitles has channel_name=None",
         result2 is not None and result2["channel_name"] is None)

    # Entry missing titleUrl → skipped
    entry_no_url = {
        "header": "YouTube",
        "title": "Watched Some Video",
        "time": "2025-11-27T10:30:45.123Z"
    }
    test("Entry without titleUrl is skipped",
         parse_activity_entry(entry_no_url) is None)

    # Non-watch entry (ad interaction, visited page)
    entry_visited = {
        "header": "YouTube",
        "title": "Visited YouTube Music",
        "titleUrl": "https://music.youtube.com",
        "time": "2025-11-27T10:30:45.123Z"
    }
    test("Non-watch entry (Visited) is skipped",
         parse_activity_entry(entry_visited) is None)

    # Batch parsing
    entries = [entry, entry_no_url, entry_visited, entry_no_sub]
    parsed = parse_activity_json(entries)
    test("Batch parsing filters correctly",
         len(parsed) == 2,
         f"expected 2, got {len(parsed)}")


# =========================================================================
# Group 3: Database Operations
# =========================================================================

def test_database_operations():
    print("\n--- Group 3: Database Operations ---")

    # Init in-memory DB
    conn = init_db(":memory:")

    # Check tables exist
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t[0] for t in tables]
    test("All tables created",
         "watch_history" in table_names and "sync_metadata" in table_names and "transcript_cache" in table_names,
         f"got {table_names}")

    # Insert entries
    test_entries = [
        {"video_id": "v1", "title": "Video One", "channel_name": "Chan A",
         "channel_url": "https://youtube.com/channel/A", "watched_at": "2025-01-15T10:00:00Z",
         "video_url": "https://youtube.com/watch?v=v1"},
        {"video_id": "v2", "title": "Video Two", "channel_name": "Chan B",
         "channel_url": None, "watched_at": "2025-01-16T11:00:00Z",
         "video_url": "https://youtube.com/watch?v=v2"},
        {"video_id": "v1", "title": "Video One Again", "channel_name": "Chan A",
         "channel_url": None, "watched_at": "2025-01-17T12:00:00Z",
         "video_url": "https://youtube.com/watch?v=v1"},
    ]
    added, skipped = insert_watch_entries(conn, test_entries)
    test("Insert new entries counts correctly",
         added == 3,
         f"expected 3 added, got {added}")

    # Insert duplicates
    added2, skipped2 = insert_watch_entries(conn, test_entries[:2])
    test("Duplicate entries are skipped",
         skipped2 == 2,
         f"expected 2 skipped, got {skipped2}")

    # get_last_sync_time with no syncs
    test("No syncs returns None",
         get_last_sync_time(conn) is None)

    # Record a sync
    sync_id = record_sync(conn, started_at="2025-01-15T10:00:00Z", status="started")
    test("Record sync returns valid ID",
         sync_id is not None and sync_id > 0,
         f"got {sync_id}")

    # Update sync to completed
    update_sync(conn, sync_id, status="completed",
                sync_completed_at="2025-01-15T10:05:00Z",
                entries_added=3, entries_skipped=0)

    last_sync = get_last_sync_time(conn)
    test("Last sync time returns completed sync",
         last_sync == "2025-01-15T10:05:00Z",
         f"got {last_sync}")

    conn.close()


# =========================================================================
# Group 4: Search Logic
# =========================================================================

def test_search_logic():
    print("\n--- Group 4: Search Logic ---")

    # Set up test database
    conn = init_db(":memory:")
    entries = [
        {"video_id": "v1", "title": "Python Tutorial for Beginners", "channel_name": "CodeAcademy",
         "watched_at": "2025-01-15T10:00:00Z", "video_url": "https://youtube.com/watch?v=v1"},
        {"video_id": "v2", "title": "JavaScript Crash Course", "channel_name": "TraversyMedia",
         "watched_at": "2025-02-20T11:00:00Z", "video_url": "https://youtube.com/watch?v=v2"},
        {"video_id": "v3", "title": "Cooking Italian Pasta", "channel_name": "Gordon Ramsay",
         "watched_at": "2025-03-10T12:00:00Z", "video_url": "https://youtube.com/watch?v=v3"},
        {"video_id": "v4", "title": "Python Advanced Tips", "channel_name": "ArjanCodes",
         "watched_at": "2025-04-05T13:00:00Z", "video_url": "https://youtube.com/watch?v=v4"},
    ]
    insert_watch_entries(conn, entries)

    # Search by title keyword
    rows = conn.execute(
        "SELECT title FROM watch_history WHERE title LIKE ? ORDER BY watched_at DESC",
        ("%Python%",)
    ).fetchall()
    test("Search by title keyword",
         len(rows) == 2,
         f"expected 2 Python videos, got {len(rows)}")

    # Search by channel name
    rows = conn.execute(
        "SELECT title FROM watch_history WHERE channel_name LIKE ?",
        ("%Ramsay%",)
    ).fetchall()
    test("Search by channel name",
         len(rows) == 1 and "Pasta" in rows[0][0],
         f"got {rows}")

    # Search with date range
    rows = conn.execute(
        "SELECT title FROM watch_history WHERE title LIKE ? AND watched_at >= ? AND watched_at <= ?",
        ("%Python%", "2025-03-01", "2025-12-31")
    ).fetchall()
    test("Search with date range filters correctly",
         len(rows) == 1 and "Advanced" in rows[0][0],
         f"got {rows}")

    # Search with no matches
    rows = conn.execute(
        "SELECT title FROM watch_history WHERE title LIKE ?",
        ("%Nonexistent%",)
    ).fetchall()
    test("Search with no matches returns empty",
         len(rows) == 0)

    conn.close()


# =========================================================================
# Group 5: Import Handler
# =========================================================================

def test_import_handler():
    print("\n--- Group 5: Import Handler ---")

    import asyncio

    # We need to test the handler with production mode
    import youtube_history_mcp_server as srv
    original_mode = srv.SERVER_MODE

    # Create test JSON data
    test_data = [
        {
            "header": "YouTube",
            "title": "Watched Test Video One",
            "titleUrl": "https://www.youtube.com/watch?v=test1",
            "subtitles": [{"name": "TestChannel", "url": "https://youtube.com/channel/UC1"}],
            "time": "2025-06-15T10:00:00Z"
        },
        {
            "header": "YouTube",
            "title": "Watched Test Video Two",
            "titleUrl": "https://www.youtube.com/watch?v=test2",
            "subtitles": [{"name": "TestChannel2", "url": "https://youtube.com/channel/UC2"}],
            "time": "2025-06-16T11:00:00Z"
        }
    ]

    # Test JSON import
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(test_data, f)
        json_path = f.name

    try:
        # Use temp database
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as db_file:
            temp_db = db_file.name

        srv.SERVER_MODE = "production"
        srv.DB_PATH = temp_db
        init_db(temp_db)

        result = asyncio.run(srv.handle_import_takeout({"file_path": json_path}))
        text = result[0].text
        test("Import from JSON file succeeds",
             "New entries: 2" in text,
             f"got: {text}")

        # Test duplicate import
        result2 = asyncio.run(srv.handle_import_takeout({"file_path": json_path}))
        text2 = result2[0].text
        test("Re-import deduplicates correctly",
             "Duplicates skipped: 2" in text2,
             f"got: {text2}")

    finally:
        os.unlink(json_path)
        if os.path.exists(temp_db):
            os.unlink(temp_db)

    # Test ZIP import
    with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as zf_file:
        zip_path = zf_file.name

    try:
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as db_file:
            temp_db = db_file.name

        srv.DB_PATH = temp_db
        init_db(temp_db)

        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("Takeout/YouTube and YouTube Music/history/watch-history.json",
                        json.dumps(test_data))

        result = asyncio.run(srv.handle_import_takeout({"file_path": zip_path}))
        text = result[0].text
        test("Import from ZIP file succeeds",
             "New entries: 2" in text,
             f"got: {text}")

    finally:
        os.unlink(zip_path)
        if os.path.exists(temp_db):
            os.unlink(temp_db)
        srv.SERVER_MODE = original_mode

    # Test nonexistent file
    srv.SERVER_MODE = "production"
    result = asyncio.run(srv.handle_import_takeout({"file_path": "/nonexistent/file.json"}))
    test("Nonexistent file returns error",
         "Error: File not found" in result[0].text)

    srv.SERVER_MODE = original_mode


# =========================================================================
# Group 6: Tool Validation and Readonly Mode
# =========================================================================

def test_tool_validation():
    print("\n--- Group 6: Tool Validation and Readonly Mode ---")

    import asyncio
    import youtube_history_mcp_server as srv
    original_mode = srv.SERVER_MODE

    # Test readonly mode blocks sync
    srv.SERVER_MODE = "readonly"
    result = asyncio.run(srv.handle_sync_history({}))
    test("sync_history blocked in readonly mode",
         PRODUCTION_ONLY_MSG in result[0].text,
         f"got: {result[0].text}")

    # Test readonly mode blocks import
    result = asyncio.run(srv.handle_import_takeout({"file_path": "/some/file.json"}))
    test("import_takeout blocked in readonly mode",
         PRODUCTION_ONLY_MSG in result[0].text)

    # Test search with empty query
    srv.SERVER_MODE = "production"
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as db_file:
        temp_db = db_file.name
    try:
        srv.DB_PATH = temp_db
        init_db(temp_db)

        result = asyncio.run(srv.handle_search_history({"query": ""}))
        test("search_history with empty query returns error",
             "Error:" in result[0].text,
             f"got: {result[0].text}")

        result = asyncio.run(srv.handle_search_history({}))
        test("search_history with missing query returns error",
             "Error:" in result[0].text)

        # Test get_recent_watches with invalid days
        result = asyncio.run(srv.handle_get_recent_watches({"days": "not_a_number"}))
        test("get_recent_watches with invalid days returns error",
             "Error:" in result[0].text,
             f"got: {result[0].text}")

    finally:
        os.unlink(temp_db)
        srv.SERVER_MODE = original_mode

    # Test tool list varies by mode
    srv.SERVER_MODE = "production"
    prod_tools = asyncio.run(srv.list_tools())
    prod_names = [t.name for t in prod_tools]

    srv.SERVER_MODE = "readonly"
    ro_tools = asyncio.run(srv.list_tools())
    ro_names = [t.name for t in ro_tools]

    test("Production mode has 5 tools",
         len(prod_names) == 5,
         f"got {len(prod_names)}: {prod_names}")

    test("Readonly mode has 3 tools",
         len(ro_names) == 3,
         f"got {len(ro_names)}: {ro_names}")

    test("sync_history not in readonly tool list",
         "sync_history" not in ro_names)

    test("import_takeout not in readonly tool list",
         "import_takeout" not in ro_names)

    srv.SERVER_MODE = original_mode


# =========================================================================
# Run All Tests
# =========================================================================

def main():
    print("=" * 60)
    print("YouTube History MCP Server — Test Suite")
    print("=" * 60)

    test_video_id_extraction()
    test_activity_parsing()
    test_database_operations()
    test_search_logic()
    test_import_handler()
    test_tool_validation()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if errors:
        print(f"Failed tests: {', '.join(errors)}")
    print(f"{'=' * 60}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()


# =========================================================================
# Manual Acceptance Tests (require OAuth + GCP setup)
# =========================================================================
#
# Prerequisites:
#   1. GCP project with Data Portability API enabled
#   2. OAuth Desktop App credentials in credentials/client_secret.json
#   3. YOUTUBE_HISTORY_MODE=production
#
# Steps:
#   1. Run: python -c "from youtube_history_mcp_server import sync_history_batch; sync_history_batch()"
#      → Browser opens for OAuth consent → authorize → history syncs
#      → Expected: "Sync completed successfully. New entries: N"
#
#   2. Run: sqlite3 history.db "SELECT COUNT(*) FROM watch_history;"
#      → Should show > 0 entries
#
#   3. Run sync again within 24h:
#      → Expected: "Rate limited — you can sync again after ..."
#
#   4. Test via MCP (restart Claude Code):
#      - search_history(query="some known video title")
#      - get_recent_watches(days=30)
#      - get_watch_stats()
