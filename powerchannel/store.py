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
ALL = "all"
# A delivered message longer than this is cut short in the delivery; read still returns it in full.
DELIVERY_TEXT_CAP = 6000  # `to` value (and @all) for a message meant for everyone on the channel
FIRST_READ_BACKLOG = 20  # messages shown the first time a name reads a channel
READ_BATCH = 200  # max messages returned by one read
HUMAN = "human"  # reserved author for posts made from the CLI

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MENTION_RE = re.compile(r"^\s*@([A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?)")


class BoardError(ValueError):
    """A mistake in the caller's input, reported back to the agent as-is."""


@dataclass(frozen=True)
class Message:
    id: int
    channel: str
    author: str
    text: str
    created: float
    recipient: str = ""  # "" = everyone on the channel, else the one name it is addressed to


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


def flat(text: str) -> str:
    """Message text for display: continuation lines indented, so a line inside a message can
    never look like a new message ("[11-24 14:05] human: ...")."""
    lines = (text or "").splitlines() or [""]  # every Unicode line break, not only \\n
    return "\n    ".join(lines)


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
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    channel   TEXT NOT NULL,
    author    TEXT NOT NULL,
    text      TEXT NOT NULL,
    created   REAL NOT NULL,
    recipient TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages (channel, id);
CREATE INDEX IF NOT EXISTS messages_created ON messages (created);
CREATE TABLE IF NOT EXISTS seen (
    reader    TEXT NOT NULL,
    channel   TEXT NOT NULL,
    last_id   INTEGER NOT NULL,           -- read position: the newest message read returned
    updated   REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0, -- newest message pushed into the reader's session
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

CREATE_MEMBERS = """
CREATE TABLE IF NOT EXISTS members (
    reader    TEXT NOT NULL,
    channel   TEXT NOT NULL,
    route     TEXT NOT NULL,     -- how messages reach the session: "claude", "codex" or "cursor"
    address   TEXT NOT NULL,     -- the session's id in that app
    owner     INTEGER NOT NULL,  -- random token of the process that delivers to it
    joined    REAL NOT NULL,
    updated   REAL NOT NULL,     -- refreshed by the owner while it is alive
    delivered INTEGER NOT NULL,  -- newest message delivered (or the start, the newest at join)
    error     TEXT NOT NULL DEFAULT '',  -- the last failed delivery, '' after a success
    PRIMARY KEY (reader, channel)
);
"""

STANDBY_FRESH_SECONDS = 30  # a standby row whose owner hasn't refreshed it for this long is stale
STANDBY_KEEP_SECONDS = 24 * 3600  # stale rows are deleted on the next post after this long


@dataclass(frozen=True)
class Member:
    reader: str
    channel: str
    route: str
    address: str
    owner: int
    joined: float
    updated: float
    delivered: int
    error: str = ""


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
        self._db.executescript(CREATE_MEMBERS)
        for table, col, decl in (("messages", "recipient", "TEXT NOT NULL DEFAULT ''"),  # before 0.7
                                 ("seen", "delivered", "INTEGER NOT NULL DEFAULT 0"),
                                 ("members", "error", "TEXT NOT NULL DEFAULT ''")):
            if col not in {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}:
                try:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:  # another process added it first
                    pass

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

    def post(self, author: str, channel: str, text: str, to: str | None = None) -> Message:
        """Post `text`. `to` is one name (direct) or "all"; when it is None, a leading
        "@name" in the text addresses it, and anything else goes to everyone."""
        author = clean_name(author, "name")
        channel = clean_name(channel, "channel")
        text = (text or "").strip()
        if not text:
            raise BoardError("message is empty")
        if to is None:
            m = _MENTION_RE.match(text)
            to = m.group(1) if m else ALL
        recipient = clean_name(to, "recipient")
        if recipient == ALL:
            recipient = ""
        now = time.time()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("DELETE FROM messages WHERE created < ?", (now - RETENTION_SECONDS,))
            self._db.execute("DELETE FROM standby WHERE updated < ?", (now - STANDBY_KEEP_SECONDS,))
            self._db.execute("DELETE FROM members WHERE updated < ?", (now - STANDBY_KEEP_SECONDS,))
            cur = self._db.execute(
                "INSERT INTO messages (channel, author, text, created, recipient) VALUES (?, ?, ?, ?, ?)",
                (channel, author, text, now, recipient),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return Message(cur.lastrowid, channel, author, text, now, recipient)

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
                "SELECT id, channel, author, text, created, recipient FROM messages "
                f"WHERE channel = ? {own}ORDER BY id DESC LIMIT ?",
                (channel, FIRST_READ_BACKLOG) if include_own else (channel, reader, FIRST_READ_BACKLOG),
            ).fetchall()[::-1]
            newest = self._db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel = ?", (channel,)
            ).fetchone()[0]
            more = False
        else:
            rows = self._db.execute(
                "SELECT id, channel, author, text, created, recipient FROM messages "
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

    def has_unseen(self, reader: str, channel: str, for_me: bool = False) -> bool:
        """Cheap check used while waiting: is there anything new from someone else (with
        `for_me`, only messages addressed to everyone or to `reader`)?"""
        row = self._db.execute(
            "SELECT last_id FROM seen WHERE reader = ? AND channel = ?", (reader, channel)
        ).fetchone()
        last_id = row[0] if row else 0
        mine = "AND recipient IN ('', ?) " if for_me else ""
        params = (channel, last_id, reader) + ((reader,) if for_me else ())
        return self._db.execute(
            f"SELECT 1 FROM messages WHERE channel = ? AND id > ? AND author != ? {mine}LIMIT 1", params,
        ).fetchone() is not None

    def delivered_id(self, reader: str, channel: str) -> int:
        """The newest message pushed into `reader`'s session on `channel` (0 if none). Kept in
        `seen` too, so it outlives the member row."""
        reader, channel = clean_name(reader, "name"), clean_name(channel, "channel")
        a = self._db.execute("SELECT delivered FROM seen WHERE reader = ? AND channel = ?", (reader, channel)).fetchone()
        b = self._db.execute("SELECT delivered FROM members WHERE reader = ? AND channel = ?", (reader, channel)).fetchone()
        return max(a[0] if a else 0, b[0] if b else 0)

    def ensure_seen(self, reader: str, channel: str) -> None:
        """Give `reader` a read position on `channel` if it has none: the newest message. A
        session that joins by posting first then gets every message posted after that."""
        reader, channel = clean_name(reader, "name"), clean_name(channel, "channel")
        self._write(
            "INSERT OR IGNORE INTO seen (reader, channel, last_id, updated, delivered) VALUES "
            "(?, ?, (SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel = ?), ?, 0)",
            (reader, channel, channel, time.time()))

    def recent(self, channel: str, after_id: int = 0, limit: int = FIRST_READ_BACKLOG) -> list[Message]:
        """Messages for the human `tail` view; does not touch anyone's read position."""
        channel = clean_name(channel, "channel")
        if after_id:
            rows = self._db.execute(
                "SELECT id, channel, author, text, created, recipient FROM messages "
                "WHERE channel = ? AND id > ? ORDER BY id", (channel, after_id),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id, channel, author, text, created, recipient FROM messages "
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

    # -- members: sessions that get messages delivered natively -----------
    # A member is an agent name on a channel whose app session PowerChannel can reach. The
    # owner process (the session's own PowerChannel server, or Cursor's stop hook) pushes
    # each message meant for it into the session and records how far it got.

    def newest_id(self, channel: str) -> int:
        return self._db.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel = ?",
                                (clean_name(channel, "channel"),)).fetchone()[0]

    def join(self, reader: str, channel: str, route: str, address: str, owner: int) -> Member:
        """Make `reader` a member of `channel`, reached through `route`/`address` by `owner`.
        Delivery starts after the newest of: what it has read, what was delivered to it, and (for
        a new member that never read the channel) the newest message when it joined. A member
        row that exists keeps its position when a new owner (session or hook) takes it over."""
        reader, channel = clean_name(reader, "name"), clean_name(channel, "channel")
        now = time.time()
        self._write(
            "INSERT INTO members (reader, channel, route, address, owner, joined, updated, delivered, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT last_id FROM seen WHERE reader = ? AND channel = ?), "
            "(SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel = ?)), '') "
            "ON CONFLICT (reader, channel) DO UPDATE SET "
            "route = excluded.route, address = CASE WHEN excluded.address = '' THEN members.address "
            "ELSE excluded.address END, owner = excluded.owner, "
            "updated = excluded.updated, error = CASE WHEN members.owner = excluded.owner "
            "THEN members.error ELSE '' END",
            (reader, channel, route, address, owner, now, now, reader, channel, channel),
        )
        return self.get_member(reader, channel)  # type: ignore[return-value]

    def get_member(self, reader: str, channel: str) -> Member | None:
        row = self._db.execute(
            "SELECT reader, channel, route, address, owner, joined, updated, delivered, error FROM members "
            "WHERE reader = ? AND channel = ?", (clean_name(reader, "name"), clean_name(channel, "channel")),
        ).fetchone()
        return Member(*row) if row else None

    def members(self, owner: int | None = None, channel: str | None = None) -> list[Member]:
        sql = "SELECT reader, channel, route, address, owner, joined, updated, delivered, error FROM members WHERE 1 = 1"
        params: list = []
        if owner is not None:
            sql += " AND owner = ?"
            params.append(owner)
        if channel is not None:
            sql += " AND channel = ?"
            params.append(clean_name(channel, "channel"))
        return [Member(*r) for r in self._db.execute(sql + " ORDER BY joined", params).fetchall()]

    def touch_members(self, owner: int) -> None:
        self._write("UPDATE members SET updated = ? WHERE owner = ?", (time.time(), owner))

    def leave(self, reader: str, channel: str) -> None:
        self._write("DELETE FROM members WHERE reader = ? AND channel = ?",
                    (clean_name(reader, "name"), clean_name(channel, "channel")))

    def pending_for(self, member: Member, limit: int = READ_BATCH) -> tuple[list[Message], int]:
        """Messages to deliver to `member`: from others, for everyone or for it, newer than both
        what was delivered and what it has read. Returns (messages, newest id looked at)."""
        row = self._db.execute("SELECT last_id, delivered FROM seen WHERE reader = ? AND channel = ?",
                               (member.reader, member.channel)).fetchone()
        current = self.get_member(member.reader, member.channel) or member
        after = max(current.delivered, *(row or (0, 0)))
        rows = self._db.execute(
            "SELECT id, channel, author, text, created, recipient FROM messages "
            "WHERE channel = ? AND id > ? ORDER BY id LIMIT ?", (member.channel, after, limit),
        ).fetchall()
        newest = rows[-1][0] if rows else after
        mine = [Message(*r) for r in rows if r[2] != member.reader and r[5] in ("", member.reader)]
        return mine, newest

    def mark_delivered(self, marks: list[tuple[str, str, int]], owner: int | None = None) -> bool:
        """Record delivery of [(reader, channel, last_id), ...] in one transaction. With `owner`,
        nothing is recorded unless that owner still holds every one of those members. The
        read position doesn't move: read still returns these, marked as already delivered."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for reader, channel, _ in marks:
                if owner is not None:
                    row = self._db.execute("SELECT owner FROM members WHERE reader = ? AND channel = ?",
                                           (reader, channel)).fetchone()
                    if not row or row[0] != owner:
                        self._db.execute("ROLLBACK")
                        return False
            for reader, channel, last_id in marks:
                # seen keeps it past the member row; no seen row is created here, so an agent
                # that never read the channel still gets "recent history" on its first read
                self._db.execute("UPDATE seen SET delivered = MAX(delivered, ?) WHERE reader = ? AND channel = ?",
                                 (last_id, reader, channel))
                self._db.execute("UPDATE members SET delivered = MAX(delivered, ?), error = '' "
                                 "WHERE reader = ? AND channel = ?", (last_id, reader, channel))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return True

    def set_error(self, reader: str, channel: str, error: str) -> None:
        self._write("UPDATE members SET error = ? WHERE reader = ? AND channel = ?", (error[:300], reader, channel))

    # -- overview --------------------------------------------------------

    def channels(self) -> list[dict]:
        """Every channel with its message count, last message and active names."""
        out = []
        for channel, count in self._db.execute(
            "SELECT channel, COUNT(*) FROM messages GROUP BY channel"
        ).fetchall():
            last = self._db.execute(
                "SELECT id, channel, author, text, created, recipient FROM messages "
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
            live = sorted(r[0] for r in self._db.execute(
                "SELECT reader FROM members WHERE channel = ? AND updated > ?",
                (channel, fresh)))
            out.append({"channel": channel, "messages": count, "last": Message(*last),
                        "active": sorted(names | set(standby) | set(live)), "standby": standby,
                        "live": live})
        out.sort(key=lambda c: c["last"].created, reverse=True)
        return out
