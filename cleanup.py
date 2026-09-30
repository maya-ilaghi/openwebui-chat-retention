#!/usr/bin/env python3
"""Delete Open WebUI conversations whose last message is older than N days."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATABASE = Path(__file__).parent / "data" / "webui.db"
DEFAULT_RETENTION_DAYS = 3
SECONDS_PER_DAY = 24 * 60 * 60
# How long to wait for the Open WebUI server to release a write lock before giving up.
LOCK_TIMEOUT_SECONDS = 30

log = logging.getLogger("cleanup")


def find_stale_conversations(connection: sqlite3.Connection, cutoff_timestamp: int):
    """Return (chat_id, last_activity) for chats whose last activity is before the cutoff.

    Last activity is the newest message in `chat_message`. A chat without message rows (empty,
    or a message row that failed to be written) falls back to `chat.updated_at`. That value is
    never earlier than the chat's real last message, so an active chat is never deleted too early,
    and no chat is kept forever just because it has no message rows.
    """
    return connection.execute(
        """
        SELECT c.id, COALESCE(MAX(cm.created_at), c.updated_at) AS last_activity
        FROM chat AS c
        LEFT JOIN chat_message AS cm ON cm.chat_id = c.id
        GROUP BY c.id
        HAVING last_activity < ?
        """,
        (cutoff_timestamp,),
    ).fetchall()


def delete_stale_conversations(
    database_path: Path,
    retention_days: int,
    dry_run: bool = False,
    now_timestamp: int | None = None,
) -> int:
    """Delete stale chats and return the number found/deleted."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")

    now_timestamp = int(time.time()) if now_timestamp is None else now_timestamp
    cutoff_timestamp = now_timestamp - retention_days * SECONDS_PER_DAY
    log.info("Deleting chats with last activity before %s%s", iso(cutoff_timestamp), " (dry run)" if dry_run else "")

    # closing(): `with sqlite3.connect()` alone commits but never closes the connection.
    with contextlib.closing(sqlite3.connect(database_path, timeout=LOCK_TIMEOUT_SECONDS)) as connection:
        # Must be enabled per connection: cascades the delete to chat_message, shared_chat and chat_file.
        connection.execute("PRAGMA foreign_keys = ON")

        if dry_run:
            # Read only: no write lock, so the dry run never blocks the server.
            stale_conversations = find_stale_conversations(connection, cutoff_timestamp)
            log_conversations("Would delete", stale_conversations)
            return len(stale_conversations)

        # BEGIN IMMEDIATE takes the write lock *before* the check, so the server cannot save a new
        # message between finding a stale chat and deleting it. Other writers wait until we commit.
        connection.execute("BEGIN IMMEDIATE")
        with connection:  # commit on success, roll back on any error: all stale chats are deleted, or none
            stale_conversations = find_stale_conversations(connection, cutoff_timestamp)
            log_conversations("Deleting", stale_conversations)
            connection.executemany(
                "DELETE FROM chat WHERE id = ?",
                [(chat_id,) for chat_id, _ in stale_conversations],
            )

    return len(stale_conversations)


def log_conversations(action: str, conversations) -> None:
    # Ids and dates only: titles are derived from user content, so they are personal data too.
    for chat_id, last_activity in conversations:
        log.info("%s %s (last activity %s)", action, chat_id, iso(last_activity))


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Delete Open WebUI conversations whose last message is older than the retention period."
    )
    parser.add_argument(
        "--days",
        type=positive_int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"retention period in days, at least 1 (default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"path to the SQLite database (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show stale conversations without deleting them",
    )
    parser.add_argument(
        "--every",
        type=positive_int,
        metavar="MINUTES",
        help="keep running and repeat the cleanup every MINUTES (default: run once and exit)",
    )
    return parser.parse_args(argv)


def run_once(args) -> None:
    count = delete_stale_conversations(args.database, args.days, args.dry_run)
    if args.dry_run:
        log.info("Found %d stale conversation(s). No data was deleted.", count)
    else:
        log.info("Deleted %d stale conversation(s).", count)


def main(argv: list[str] | None = None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)

    if not args.database.exists():
        raise SystemExit(f"Database not found: {args.database}")

    if args.every is None:
        run_once(args)
        return

    log.info("Running every %d minute(s). Stop with Ctrl+C.", args.every)
    while True:
        try:
            run_once(args)
        except sqlite3.Error as error:
            # e.g. the database is locked for too long: log it and try again on the next run.
            log.error("Cleanup failed: %s", error)
        time.sleep(args.every * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
