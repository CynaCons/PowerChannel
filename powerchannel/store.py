"""
The board: one SQLite file shared by every powerchannel process on this machine.

Each app (Claude Code, Codex, Cursor, ...) starts its own powerchannel server;
they all open the same database file, so there is no daemon to run.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RETENTION_SECONDS = 7 * 24 * 3600
FIRST_READ_BACKLOG = 20  # messages shown the first time a name reads a channel
READ_BATCH = 200  # max messages returned by one read
HUMAN = "human"  # reserved author for posts made from the CLI

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class BoardError(ValueError):
    """A mistake in the caller's input, reported back to the agent as-is."""


@dataclass(frozen=True)
class Message:
    id: int
    channel: str
    author: str
    text: str
    created: float


def _data_base() -> Path:
    """The per-user local data folder for this platform."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def default_db_path() -> Path:
    """POWERCHANNEL_DB (or the legacy POWERBOARD_DB), else the per-user local data folder."""
    override = os.environ.get("POWERCHANNEL_DB") or os.environ.get("POWERBOARD_DB")
    if override:
        return Path(override)
    base = _data_base()
    new_path = base / "powerchannel" / "channels.db"
    _migrate_legacy(new_path, base / "powerboard" / "board.db")
    return new_path


def _migrate_legacy(new: Path, old: Path) -> None:
    """One-time copy of a legacy powerboard database to the new powerchannel location.

    Copies only when the new file is missing and the old one exists. The legacy
    file is read (checkpointed) and copied with the sqlite backup API; it is never
    deleted or modified. Any failure is reported and ignored, so the server still
    starts on an empty new database.
    """
    if new.exists() or not old.exists():
        return
    # Copy into a temp file and rename it into place, so a copy that fails half
    # way never leaves a partial database that would block the next attempt.
    tmp = new.with_name(new.name + ".migrating")
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        tmp.unlink(missing_ok=True)
        src = sqlite3.connect(old)
        try:
            src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, new)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"powerchannel: could not migrate legacy database {old}: {e}", file=sys.stderr)


def clean_name(value: str, what: str) -> str:
    """Normalise a channel or agent name: lowercase, letters/digits/._- only."""
    name = (value or "").strip().lower()
    if not _NAME_RE.match(name):
        raise BoardError(
            f"invalid {what} {value!r}: use 1-64 lowercase letters, digits, '.', '_' or '-', "
            f"starting with a letter or digit (e.g. 'auth-refactor')"
        )
    return name


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    author  TEXT NOT NULL,
    text    TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages (channel, id);
CREATE INDEX IF NOT EXISTS messages_created ON messages (created);
CREATE TABLE IF NOT EXISTS seen (
    reader  TEXT NOT NULL,
    channel TEXT NOT NULL,
    last_id INTEGER NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (reader, channel)
);
CREATE TABLE IF NOT EXISTS standby (
    reader  TEXT NOT NULL,
    channel TEXT NOT NULL,
    route   TEXT NOT NULL,             -- how the wake is delivered: "codex" or "shell"
    address TEXT NOT NULL DEFAULT '',  -- Codex thread id for the "codex" route
    owner   INTEGER NOT NULL,          -- pid of the process that delivers the wake
    since   REAL NOT NULL,
    updated REAL NOT NULL,             -- refreshed by the owner while it is alive
    PRIMARY KEY (reader, channel)
);
"""

STANDBY_FRESH_SECONDS = 30  # a standby row whose owner hasn't refreshed it for this long is stale
STANDBY_KEEP_SECONDS = 24 * 3600  # stale rows are deleted on the next post after this long


@dataclass(frozen=True)
class Standby:
    reader: str
    channel: str
    route: str
    address: str
    owner: int
    since: float
    updated: float


class Board:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we issue BEGIN IMMEDIATE ourselves for writes.
        self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                   check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(sql, params)
            self._db.execute("COMMIT")
            return cur
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    # -- posting ---------------------------------------------------------

    def post(self, author: str, channel: str, text: str) -> Message:
        author = clean_name(author, "name")
        channel = clean_name(channel, "channel")
        text = (text or "").strip()
        if not text:
            raise BoardError("message is empty")
        now = time.time()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("DELETE FROM messages WHERE created < ?", (now - RETENTION_SECONDS,))
            self._db.execute("DELETE FROM standby WHERE updated < ?", (now - STANDBY_KEEP_SECONDS,))
            cur = self._db.execute(
                "INSERT INTO messages (channel, author, text, created) VALUES (?, ?, ?, ?)",
                (channel, author, text, now),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return Message(cur.lastrowid, channel, author, text, now)

    # -- reading ---------------------------------------------------------

    def read_new(self, reader: str, channel: str, include_own: bool = False) -> tuple[list[Message], bool]:
        """Messages on `channel` that `reader` has not seen, excluding its own unless
        `include_own` (used when catching up, so a reader can see what it already answered).

        Marks them seen. The first read of a channel returns its recent backlog.
        Returns (messages, more_pending).
        """
        reader = clean_name(reader, "name")
        channel = clean_name(channel, "channel")
        own = "" if include_own else "AND author != ? "
        row = self._db.execute(
            "SELECT last_id FROM seen WHERE reader = ? AND channel = ?", (reader, channel)
        ).fetchone()
        if row is None:
            rows = self._db.execute(
                "SELECT id, channel, author, text, created FROM messages "
                f"WHERE channel = ? {own}ORDER BY id DESC LIMIT ?",
                (channel, FIRST_READ_BACKLOG) if include_own else (channel, reader, FIRST_READ_BACKLOG),
            ).fetchall()[::-1]
            newest = self._db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel = ?", (channel,)
            ).fetchone()[0]
            more = False
        else:
            rows = self._db.execute(
                "SELECT id, channel, author, text, created FROM messages "
                "WHERE channel = ? AND id > ? ORDER BY id LIMIT ?",
                (channel, row[0], READ_BATCH + 1),
            ).fetchall()
            more = len(rows) > READ_BATCH
            rows = rows[:READ_BATCH]
            newest = rows[-1][0] if rows else row[0]
            if not include_own:
                rows = [r for r in rows if r[2] != reader]
        self._write(
            "INSERT INTO seen (reader, channel, last_id, updated) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (reader, channel) DO UPDATE SET last_id = excluded.last_id, "
            "updated = excluded.updated",
            (reader, channel, newest, time.time()),
        )
        return [Message(*r) for r in rows], more

    def touch(self, reader: str, channel: str) -> None:
        """Mark `reader` as still reading `channel` (seen.updated = now) without moving
        its read position. Called while a read waits, so other apps can tell a name
        that is listening right now from one that read hours ago."""
        reader = clean_name(reader, "name")
        channel = clean_name(channel, "channel")
        self._write(
            "UPDATE seen SET updated = ? WHERE reader = ? AND channel = ?",
            (time.time(), reader, channel),
        )

    def last_read(self, reader: str, channel: str) -> float | None:
        """When `reader` last read (or was waiting on) `channel`, or None if never."""
        row = self._db.execute(
            "SELECT updated FROM seen WHERE reader = ? AND channel = ?",
            (clean_name(reader, "name"), clean_name(channel, "channel")),
        ).fetchone()
        return row[0] if row else None

    def has_read(self, reader: str, channel: str) -> bool:
        """True once `reader` has read `channel` at least once."""
        return self._db.execute(
            "SELECT 1 FROM seen WHERE reader = ? AND channel = ?", (reader, channel)
        ).fetchone() is not None

    def has_unseen(self, reader: str, channel: str) -> bool:
        """Cheap check used while waiting: is there anything new from someone else?"""
        row = self._db.execute(
            "SELECT last_id FROM seen WHERE reader = ? AND channel = ?", (reader, channel)
        ).fetchone()
        last_id = row[0] if row else 0
        return self._db.execute(
            "SELECT 1 FROM messages WHERE channel = ? AND id > ? AND author != ? LIMIT 1",
            (channel, last_id, reader),
        ).fetchone() is not None

    def recent(self, channel: str, after_id: int = 0, limit: int = FIRST_READ_BACKLOG) -> list[Message]:
        """Messages for the human `tail` view; does not touch anyone's read position."""
        channel = clean_name(channel, "channel")
        if after_id:
            rows = self._db.execute(
                "SELECT id, channel, author, text, created FROM messages "
                "WHERE channel = ? AND id > ? ORDER BY id", (channel, after_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id, channel, author, text, created FROM messages "
                "WHERE channel = ? ORDER BY id DESC LIMIT ?", (channel, limit),
            ).fetchall()[::-1]
        return [Message(*r) for r in rows]

    # -- standby ---------------------------------------------------------
    # An agent on standby has ended its turn and asked to be woken by the next message.
    # The owner process (the app's PowerChannel server for Codex, a `powerchannel wait`
    # command for apps woken by a background shell) delivers the wake and refreshes the row.

    def set_standby(self, reader: str, channel: str, route: str, address: str, owner: int) -> None:
        reader, channel = clean_name(reader, "name"), clean_name(channel, "channel")
        now = time.time()
        self._write(
            "INSERT INTO standby (reader, channel, route, address, owner, since, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (reader, channel) DO UPDATE SET "
            "route = excluded.route, address = excluded.address, owner = excluded.owner, "
            "since = excluded.since, updated = excluded.updated",
            (reader, channel, route, address, owner, now, now),
        )

    def get_standby(self, reader: str, channel: str) -> Standby | None:
        row = self._db.execute(
            "SELECT reader, channel, route, address, owner, since, updated FROM standby "
            "WHERE reader = ? AND channel = ?",
            (clean_name(reader, "name"), clean_name(channel, "channel")),
        ).fetchone()
        return Standby(*row) if row else None

    def clear_standby(self, reader: str, channel: str, owner: int | None = None) -> None:
        """Remove the standby row, only if `owner` still owns it when given."""
        reader, channel = clean_name(reader, "name"), clean_name(channel, "channel")
        if owner is None:
            self._write("DELETE FROM standby WHERE reader = ? AND channel = ?", (reader, channel))
        else:
            self._write("DELETE FROM standby WHERE reader = ? AND channel = ? AND owner = ?",
                        (reader, channel, owner))

    def standby_rows(self, owner: int | None = None, route: str | None = None) -> list[Standby]:
        sql = "SELECT reader, channel, route, address, owner, since, updated FROM standby WHERE 1 = 1"
        params: list = []
        if owner is not None:
            sql += " AND owner = ?"
            params.append(owner)
        if route is not None:
            sql += " AND route = ?"
            params.append(route)
        return [Standby(*r) for r in self._db.execute(sql + " ORDER BY since", params).fetchall()]

    def touch_standby(self, owner: int) -> None:
        """The owner is alive: refresh every row it owns."""
        self._write("UPDATE standby SET updated = ? WHERE owner = ?", (time.time(), owner))

    @staticmethod
    def is_fresh(row: Standby) -> bool:
        return row.updated > time.time() - STANDBY_FRESH_SECONDS

    # -- overview --------------------------------------------------------

    def channels(self) -> list[dict]:
        """Every channel with its message count, last message and active names."""
        out = []
        for channel, count in self._db.execute(
            "SELECT channel, COUNT(*) FROM messages GROUP BY channel"
        ).fetchall():
            last = self._db.execute(
                "SELECT id, channel, author, text, created FROM messages "
                "WHERE channel = ? ORDER BY id DESC LIMIT 1", (channel,),
            ).fetchone()
            since = time.time() - 24 * 3600
            names = {r[0] for r in self._db.execute(
                "SELECT DISTINCT author FROM messages WHERE channel = ? AND created > ?",
                (channel, since))}
            names |= {r[0] for r in self._db.execute(
                "SELECT reader FROM seen WHERE channel = ? AND updated > ?", (channel, since))}
            fresh = time.time() - STANDBY_FRESH_SECONDS
            standby = sorted(r[0] for r in self._db.execute(
                "SELECT reader FROM standby WHERE channel = ? AND updated > ?", (channel, fresh)))
            out.append({"channel": channel, "messages": count, "last": Message(*last),
                        "active": sorted(names | set(standby)), "standby": standby})
        out.sort(key=lambda c: c["last"].created, reverse=True)
        return out
