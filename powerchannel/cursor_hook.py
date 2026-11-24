"""Cursor's relay: a stop hook that delivers PowerChannel messages into the chat.

Cursor tells MCP servers nothing about which chat they serve, but it runs a user-wide stop hook
(`~/.cursor/hooks.json`) after every agent turn, with the chat's `conversation_id` and transcript
path. The hook finds the PowerChannel names and channels that chat used (from the tool calls in
its transcript), then holds for up to POWERCHANNEL_CURSOR_HOLD seconds (default 3500). The first
message meant for it comes back as `followup_message`, which Cursor submits as the chat's next
user message: that wakes the agent and carries the text. Chats that never used PowerChannel
return at once.

If the hold runs out with nothing to deliver, the chat stays idle until its next turn ends
(usually when the user types); messages that arrive meanwhile are delivered then.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .deliver import BATCH, CURSOR, frame
from .store import Board, BoardError, clean_name

HOLD_SECONDS = int(os.environ.get("POWERCHANNEL_CURSOR_HOLD") or 3500)
POLL_SECONDS = 1.0
SETTLE_SECONDS = 5.0  # transcript changes after this mean the user started a new turn
HOOK_MARK = "powerchannel cursor-hook"


def names_in_transcript(path: str | None) -> list[tuple[str, str]]:
    """(me, channel) pairs from the PowerChannel tool calls in a Cursor transcript."""
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found: dict[tuple[str, str], None] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            me, ch = node.get("me"), node.get("channel")
            if isinstance(me, str) and isinstance(ch, str):
                try:
                    found[(clean_name(me, "name"), clean_name(ch, "channel"))] = None
                except BoardError:
                    pass
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for raw in lines:
        if "powerchannel" not in raw or '"me"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("role") == "assistant":
            walk(entry.get("message"))
    return list(found)


def run_hook(stdin_text: str, board: Board, hold: float = HOLD_SECONDS) -> dict:
    try:
        info = json.loads(stdin_text or "{}")
    except ValueError:
        return {}
    conv = str(info.get("conversation_id") or "")
    pairs = names_in_transcript(info.get("transcript_path"))
    if not conv or not pairs:
        return {}
    # A random owner id, not the pid (pids are reused). The newest hook for a chat takes over:
    # an older one still holding (Cursor can run the hook twice, or the user typed meanwhile)
    # sees it lost the chat and returns without delivering.
    owner = secrets.randbits(62)
    members = [board.join(me, ch, CURSOR, conv, owner) for me, ch in pairs]
    transcript = Path(str(info.get("transcript_path") or ""))

    def transcript_stamp():
        try:
            st = transcript.stat()
            return st.st_size, st.st_mtime
        except OSError:
            return None

    started = transcript_stamp()
    settle_until = time.monotonic() + SETTLE_SECONDS
    deadline = time.monotonic() + max(0.0, hold)
    try:
        while True:
            current = [board.get_member(m.reader, m.channel) for m in members]
            if any(c is None or c.owner != owner for c in current):
                return {}
            stamp = transcript_stamp()
            if time.monotonic() < settle_until:
                started = stamp  # Cursor may still be finishing the turn's last transcript lines
            elif stamp != started:  # the user typed: this stop event is over
                return {}
            texts, marks = [], []
            for m in current:
                messages, newest = board.pending_for(m, limit=BATCH + 1)  # type: ignore[arg-type]
                if messages:
                    more = len(messages) > BATCH
                    text, last, _cut = frame(m, messages[:BATCH], more)  # type: ignore[arg-type]
                    done_all = last == messages[min(len(messages), BATCH) - 1].id and not more
                    texts.append(text)
                    marks.append((m.reader, m.channel, newest if done_all else last))  # type: ignore[union-attr]
            if texts:
                if not board.mark_delivered(marks, owner=owner):
                    return {}
                return {"followup_message": "\n\n".join(texts)}
            if time.monotonic() >= deadline:
                return {}
            time.sleep(POLL_SECONDS)
            board.touch_members(owner)
    finally:
        # No longer holding: mark the rows stale, so `who` and `channels` stop showing the chat live.
        for m in members:
            board._write("UPDATE members SET updated = ? WHERE reader = ? AND channel = ? AND owner = ?",
                         (time.time() - 3600, m.reader, m.channel, owner))


def main(db: str | None = None) -> None:
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")  # Cursor Desktop on Windows sends a BOM
    board = Board(db)
    try:
        answer = run_hook(raw, board)
    except Exception as e:  # never break the user's chat over a PowerChannel problem
        print(f"powerchannel cursor-hook: {e}", file=sys.stderr)
        answer = {}
    finally:
        board.close()
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()


# -- install / uninstall ----------------------------------------------------------------------

def hooks_path() -> Path:
    return Path.home() / ".cursor" / "hooks.json"


def _in_uv_cache() -> bool:
    """True when running from `uvx`: its interpreter lives in uv's cache and may be pruned."""
    parts = [p.lower() for p in Path(sys.executable).parts]
    return "uv" in parts and "cache" in parts or "archive-v0" in parts


def hook_command(db: str | None) -> str:
    if _in_uv_cache() and shutil.which("uvx"):
        parts = ["uvx", "powerchannel", "cursor-hook"]
    else:
        parts = [sys.executable, "-m", "powerchannel", "cursor-hook"]
    if db:
        parts += ["--db", db]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def _is_ours(entry: dict) -> bool:
    cmd = str(entry.get("command", ""))
    return "powerchannel" in cmd and "cursor-hook" in cmd


def install(db: str | None = None, path: Path | None = None) -> str:
    path = path or hooks_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except ValueError as e:
            raise SystemExit(f"error: {path} isn't valid JSON ({e}); fix or move it, then run this again")
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    stop = [h for h in hooks.get("stop", []) if not _is_ours(h)]
    stop.append({"command": hook_command(db), "timeout": HOLD_SECONDS + 60, "loop_limit": None})
    hooks["stop"] = stop
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return str(path)


def uninstall(path: Path | None = None) -> bool:
    path = path or hooks_path()
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    stop = data.get("hooks", {}).get("stop", [])
    kept = [h for h in stop if not _is_ours(h)]
    if len(kept) == len(stop):
        return False
    data["hooks"]["stop"] = kept
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def installed(path: Path | None = None) -> bool:
    path = path or hooks_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError):
        return False
    return any(_is_ours(h) for h in data.get("hooks", {}).get("stop", []) if isinstance(h, dict))
