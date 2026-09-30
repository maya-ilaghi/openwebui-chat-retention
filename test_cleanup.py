import contextlib
import io
import logging
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import cleanup
from cleanup import DEFAULT_DATABASE, SECONDS_PER_DAY, delete_stale_conversations, parse_args

logging.disable(logging.CRITICAL)


class CleanupTest(unittest.TestCase):
    NOW = 2_000_000_000

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "test.db"

        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, title TEXT, updated_at INTEGER)")
            connection.execute(
                """
                CREATE TABLE chat_message (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chat(id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE shared_chat (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chat(id) ON DELETE CASCADE
                )
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def days_ago(self, days):
        return self.NOW - days * SECONDS_PER_DAY

    def add_chat(self, chat_id, message_ages_in_days, updated_days_ago=None):
        if updated_days_ago is None:
            updated_days_ago = min(message_ages_in_days, default=0)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO chat (id, title, updated_at) VALUES (?, ?, ?)",
                (chat_id, chat_id, self.days_ago(updated_days_ago)),
            )
            for index, age in enumerate(message_ages_in_days):
                connection.execute(
                    "INSERT INTO chat_message (id, chat_id, created_at) VALUES (?, ?, ?)",
                    (f"{chat_id}-{index}", chat_id, self.days_ago(age)),
                )

    def count(self, table, chat_id):
        column = "id" if table == "chat" else "chat_id"
        with sqlite3.connect(self.database) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (chat_id,)).fetchone()[0]

    def test_deletes_chat_when_last_message_is_older_than_three_days(self):
        self.add_chat("stale", [5, 4])

        deleted = delete_stale_conversations(self.database, 3, now_timestamp=self.NOW)

        self.assertEqual(1, deleted)
        self.assertEqual(0, self.count("chat", "stale"))
        self.assertEqual(0, self.count("chat_message", "stale"))

    def test_keeps_chat_when_it_has_a_recent_message(self):
        self.add_chat("active", [10, 1])

        deleted = delete_stale_conversations(self.database, 3, now_timestamp=self.NOW)

        self.assertEqual(0, deleted)
        self.assertEqual(1, self.count("chat", "active"))

    def test_recently_renamed_chat_with_old_messages_is_deleted(self):
        # A rename/pin bumps chat.updated_at, but only messages count.
        self.add_chat("renamed", [10], updated_days_ago=0)

        deleted = delete_stale_conversations(self.database, 3, now_timestamp=self.NOW)

        self.assertEqual(1, deleted)

    def test_message_exactly_at_cutoff_is_kept(self):
        self.add_chat("boundary", [3])

        deleted = delete_stale_conversations(self.database, 3, now_timestamp=self.NOW)

        self.assertEqual(0, deleted)
        self.assertEqual(1, self.count("chat", "boundary"))

    def test_chat_without_messages_falls_back_to_updated_at(self):
        self.add_chat("empty-old", [], updated_days_ago=5)
        self.add_chat("empty-new", [], updated_days_ago=1)

        deleted = delete_stale_conversations(self.database, 3, now_timestamp=self.NOW)

        self.assertEqual(1, deleted)
        self.assertEqual(0, self.count("chat", "empty-old"))
        self.assertEqual(1, self.count("chat", "empty-new"))

    def test_shared_copies_are_deleted_with_the_chat(self):
        self.add_chat("stale", [5])
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO shared_chat (id, chat_id) VALUES ('share-1', 'stale')")

        delete_stale_conversations(self.database, 3, now_timestamp=self.NOW)

        self.assertEqual(0, self.count("shared_chat", "stale"))

    def test_dry_run_does_not_delete(self):
        self.add_chat("stale", [4])

        found = delete_stale_conversations(self.database, 3, dry_run=True, now_timestamp=self.NOW)

        self.assertEqual(1, found)
        self.assertEqual(1, self.count("chat", "stale"))

    def test_rejects_retention_below_one_day(self):
        # --days 0 would delete every chat.
        with self.assertRaises(ValueError):
            delete_stale_conversations(self.database, 0, now_timestamp=self.NOW)
        for argv in (["--days", "0"], ["--every", "0"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parse_args(argv)

    def try_new_message_during_check(self, chat_id):
        """Patch the stale check so that, right after it runs, another connection (standing in for
        the Open WebUI server) tries to save a new message. Returns the outcome of that attempt."""
        outcome = {}
        original_check = cleanup.find_stale_conversations

        def check_then_write(connection, cutoff_timestamp):
            stale = original_check(connection, cutoff_timestamp)
            server = sqlite3.connect(self.database, timeout=0)  # fail immediately instead of waiting
            try:
                with server:
                    server.execute(
                        "INSERT INTO chat_message (id, chat_id, created_at) VALUES ('new', ?, ?)",
                        (chat_id, self.NOW),
                    )
                outcome["written"] = True
            except sqlite3.OperationalError as error:
                outcome["error"] = str(error)
            finally:
                server.close()
            return stale

        return outcome, check_then_write

    def test_no_message_can_be_saved_between_check_and_delete(self):
        self.add_chat("stale", [5])
        outcome, check_then_write = self.try_new_message_during_check("stale")

        with mock.patch("cleanup.find_stale_conversations", check_then_write):
            deleted = delete_stale_conversations(self.database, 3, now_timestamp=self.NOW)

        # The write lock is held from before the check until the commit: the new message is refused
        # (the server would wait and retry), so a chat is never deleted right after becoming active.
        self.assertEqual("database is locked", outcome.get("error"))
        self.assertEqual(1, deleted)
        self.assertEqual(0, self.count("chat", "stale"))

    def test_dry_run_does_not_block_the_server(self):
        self.add_chat("stale", [5])
        outcome, check_then_write = self.try_new_message_during_check("stale")

        with mock.patch("cleanup.find_stale_conversations", check_then_write):
            delete_stale_conversations(self.database, 3, dry_run=True, now_timestamp=self.NOW)

        self.assertTrue(outcome.get("written"))


class ProvidedDatabaseTest(unittest.TestCase):
    """Runs against a copy of the real Open WebUI database shipped in ./data (never the original)."""

    NOW = int(datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc).timestamp())

    def test_deletes_the_17_stale_chats_and_keeps_the_3_recent_ones(self):
        if not DEFAULT_DATABASE.exists():
            self.skipTest("data/webui.db not present")

        with tempfile.TemporaryDirectory() as temp_dir:
            # Copy the -wal/-shm files too: unmerged changes may still live in the WAL.
            for source in DEFAULT_DATABASE.parent.glob("webui.db*"):
                shutil.copy(source, temp_dir)
            database = Path(temp_dir) / "webui.db"
            if self.chat_count(database) != 20:
                self.skipTest("data/webui.db is not the original test data (was the cleanup already run?)")

            deleted = delete_stale_conversations(database, 3, now_timestamp=self.NOW)

            self.assertEqual(17, deleted)
            self.assertEqual(3, self.chat_count(database))
            with sqlite3.connect(database) as connection:
                orphans = connection.execute(
                    "SELECT COUNT(*) FROM chat_message WHERE chat_id NOT IN (SELECT id FROM chat)"
                ).fetchone()[0]
            self.assertEqual(0, orphans)

    @staticmethod
    def chat_count(database):
        with sqlite3.connect(database) as connection:
            return connection.execute("SELECT COUNT(*) FROM chat").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
