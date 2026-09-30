# Take-home Coding Challenge

## Solution

`cleanup.py` deletes Open WebUI conversations whose **last message** is older than the configured retention period.

The supplied database stores individual messages in `chat_message`. The script groups messages by conversation and uses `MAX(chat_message.created_at)` as the last-message timestamp. This follows the requirement directly rather than relying on the conversation's `updated_at`, which Open WebUI also changes when a chat is renamed, pinned, archived or tagged.

For the supplied test scenario, the default retention period is **3 days**.

## Requirements

Python 3.9+. The solution uses only the Python standard library; no packages need to be installed.

## Run

```bash
python3 cleanup.py
```

To specify a different retention period (at least 1 day):

```bash
python3 cleanup.py --days 7
```

To inspect what would be removed without changing the database:

```bash
python3 cleanup.py --days 3 --dry-run
```

A different database can be supplied when needed:

```bash
python3 cleanup.py --database ./data/webui.db --days 3
```

The output lists the id and last-activity date of every deleted chat, as an audit trail. Chat titles are not printed, because they are generated from what the user wrote and are personal data too.

## Running it automatically

The script has to run regularly to enforce the retention period. How often it runs decides how long data can outlive the limit: with an hourly run a conversation is deleted at most **3 days + 1 hour** after its last message; with a daily run, up to 4 days.

**Option 1: built-in loop.** Runs the cleanup now and then every N minutes until stopped, e.g. as a service next to Open WebUI:

```bash
python3 cleanup.py --every 60
```

A failed run (for example the database being locked for too long) is logged, and the next run tries again.

**Option 2: cron.** Runs the script once per hour:

```cron
0 * * * * cd /path/to/take-home-challenge-main && python3 cleanup.py >> cleanup.log 2>&1
```

Both are also available through `just`: `just cleanup` runs once, `just cleanup-every 60` keeps running.

## Tests

```bash
python3 -m unittest -v
```

The tests cover:

- deleting a conversation whose last message is older than the retention period;
- keeping a conversation that contains a recent message even if it also contains old messages;
- deleting a conversation with old messages even if it was renamed recently (`updated_at` is ignored);
- the exact cutoff boundary;
- chats without message rows (fallback to `updated_at`, see below);
- cascading deletion of the conversation's messages and shared copies;
- dry-run behavior, and that a dry run never blocks the server;
- no new message can be saved between the stale check and the delete (see "Running next to the server");
- rejecting a retention period below 1 day (`--days 0` would delete every chat);
- an end-to-end run on a **copy** of the supplied `data/webui.db`: 17 stale chats deleted, the 3 chats from 2026-09-28 kept, no orphaned messages.

## Design decisions

**Direct SQLite access instead of the Open WebUI API.** The script works on the database file itself, so it needs no admin credentials and no running server, and stays short. The trade-offs:

- *What the delete covers.* `PRAGMA foreign_keys = ON` makes SQLite's `ON DELETE CASCADE` remove the chat's `chat_message`, `shared_chat` (public share links) and `chat_file` rows together with the chat. Open WebUI's own delete endpoint does a bit more: it stops running LLM tasks for the chat, removes tags that are no longer used, and unlinks automation runs. None of that is message content.
- *Running next to the server.* The database runs in WAL mode, so the script can write while Open WebUI is running (tested). The stale check and the delete run in one `BEGIN IMMEDIATE` transaction: the write lock is taken *before* the check, so a user cannot add a message to a chat between it being found stale and being deleted. The lock is held for milliseconds; the server's reads are not blocked, and its writes wait until the commit. The script itself waits up to 30 s for the lock instead of failing immediately. A dry run only reads and takes no lock.
- *Coupling to the schema.* The script relies on the `chat` and `chat_message` tables. A future Open WebUI version could change them, so re-test the script after upgrading. If that becomes a burden, switching to the admin API (`DELETE /api/v1/chats/{id}`) is the natural next step.

**Chats without message rows.** Open WebUI writes every message to `chat_message` in addition to the chat's JSON, but a failed write is only logged. A chat can therefore have no rows in `chat_message`: an empty chat, or one where writes failed. Such a chat still holds personal data (at least its title), so it must not be kept forever. For these chats the script uses `chat.updated_at` instead. It is never earlier than the chat's real last message, so an active chat is never deleted too early.

## Known limitations

- Direct database deletion relies on Open WebUI's schema and foreign-key cascades. Application-managed metadata is not cleaned up: tag names that are no longer used by any chat stay in `tag`, and the legacy `chatidtag` table (unused in this version) has no cascade. Depending on the Open WebUI version, these may need separate cleanup.
- Files uploaded in a chat are not deleted: only the chat↔file link (`chat_file`) is removed. The file itself (the `file` table, the uploads folder and its vector-DB entries) should get its own retention rule.

## Open WebUI

The provided Open WebUI server can still be started with:

```bash
just serve
```
