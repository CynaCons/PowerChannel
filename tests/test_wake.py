import os
import subprocess
import sys
import threading
import time

from powerchannel import server, wake
from powerchannel.store import Board

from test_server import call

CODEX_META = {"x-codex-turn-metadata": {"thread_id": "thread-1", "turn_id": "t"}, "threadId": "thread-1"}


class FakeWaker:
    started = 0

    def __init__(self, path):
        self.path = path

    def ensure_running(self):
        FakeWaker.started += 1


def as_app(monkeypatch, client, meta=None):
    monkeypatch.setattr(server, "_client", lambda ctx: client)
    monkeypatch.setattr(server, "_meta", lambda ctx: meta or {})


def test_route_for_each_app():
    assert wake.route_for("codex-mcp-client", CODEX_META) == ("codex", "thread-1")
    assert wake.route_for("codex-mcp-client", {"threadId": "t2"}) == ("codex", "t2")
    assert wake.route_for("codex-mcp-client", {}) == (None, "")
    assert wake.route_for("claude-code", None) == ("shell", "")
    assert wake.route_for("Cursor", None) == ("shell", "")
    assert wake.route_for("gemini-cli-mcp-client", None) == (None, "")


def test_standby_returns_waiting_messages_first(db_path, monkeypatch):
    as_app(monkeypatch, "claude-code")
    call("read", me="a", channel="c")
    call("post", me="b", channel="c", message="@a: ping")
    out = call("standby", me="a", channel="c")
    assert "b: @a: ping" in out and "call standby again" in out
    assert "background command" in call("standby", me="a", channel="c")


def test_standby_first_call_shows_history(db_path, monkeypatch):
    as_app(monkeypatch, "claude-code")
    call("post", me="b", channel="c", message="earlier")
    out = call("standby", me="a", channel="c")
    assert "recent history" in out and "b: earlier" in out


def test_standby_codex_registers_the_thread(db_path, monkeypatch):
    as_app(monkeypatch, "codex-mcp-client", CODEX_META)
    monkeypatch.setattr(server, "CodexWaker", FakeWaker)
    monkeypatch.setattr(server, "_waker", None)
    call("post", me="b", channel="c", message="hi")
    call("read", me="a", channel="c")
    out = call("standby", me="a", channel="c")
    assert "on standby" in out and "End your turn" in out
    row = server.board().get_standby("a", "c")
    assert (row.route, row.address, row.owner) == ("codex", "thread-1", os.getpid())
    assert FakeWaker.started >= 1
    assert "on standby: a" in call("channels")
    call("post", me="a", channel="c", message="@all: a on standby here")
    assert server.board().get_standby("a", "c") is not None  # posting keeps the standby
    call("read", me="a", channel="c")
    assert server.board().get_standby("a", "c") is None  # a read means a is awake: cancelled


def test_standby_codex_without_a_thread_id(db_path, monkeypatch):
    as_app(monkeypatch, "codex-mcp-client", {})
    call("read", me="a", channel="c")
    out = call("standby", me="a", channel="c")
    assert "didn't send a thread id" in out and server.board().get_standby("a", "c") is None


def test_wait_output_marks_human_messages_as_the_users(board):
    board.read_new("a", "c")
    threading.Timer(0.5, lambda: Board(board.path).post("human", "c", "stop now")).start()
    out = wake.run_wait(board, "a", "c", timeout=20)
    assert "human: stop now" in out and "from human were posted by the user" in out


def test_wait_command_has_no_spaces_to_quote(monkeypatch, tmp_path):
    spaced = tmp_path / "with space"
    spaced.mkdir()
    monkeypatch.setenv("POWERCHANNEL_DB", str(spaced / "db.sqlite"))
    cmd = wake.wait_command("a", "c")
    db_part = cmd.split("--db ", 1)[1]
    assert "\\" not in cmd and (" " not in db_part or db_part.startswith('"'))


def test_standby_shell_gives_the_wait_command(db_path, monkeypatch):
    as_app(monkeypatch, "Cursor")
    call("read", me="a", channel="c")
    out = call("standby", me="a", channel="c")
    assert "-m powerchannel wait --me a --channel c" in out and "POWERCHANNEL" in out
    assert "--db" in out  # the test database is set through POWERCHANNEL_DB


def test_standby_shell_when_a_wait_is_already_running(db_path, monkeypatch):
    as_app(monkeypatch, "claude-code")
    call("read", me="a", channel="c")
    server.board().set_standby("a", "c", "shell", "", 999999)
    assert "already on standby" in call("standby", me="a", channel="c")


def test_standby_in_an_app_that_cant_be_woken(db_path, monkeypatch):
    as_app(monkeypatch, "gemini-cli-mcp-client")
    call("read", me="a", channel="c")
    out = call("standby", me="a", channel="c")
    assert "can't be woken" in out and "wait_seconds=300" in out and "Don't end your turn" in out


def test_codex_waker_queues_once_and_clears(board):
    sent = []
    w = wake.CodexWaker(board.path, queue=lambda thread, text: (sent.append((thread, text)) or (True, "")))
    board.read_new("a", "c")
    board.set_standby("a", "c", "codex", "thread-1", w.pid)
    assert w.tick(board) and sent == []
    board.post("b", "c", "hello")
    w.tick(board)
    assert sent and sent[0][0] == "thread-1" and "me=a" in sent[0][1] and "hello" not in sent[0][1]
    assert board.get_standby("a", "c") is None
    assert w.tick(board) is False  # nothing left on standby: the thread can stop


def test_codex_waker_retries_after_a_failure(board):
    calls = []
    w = wake.CodexWaker(board.path, queue=lambda thread, text: (calls.append(1) or (False, "boom")))
    board.read_new("a", "c")
    board.set_standby("a", "c", "codex", "thread-1", w.pid)
    board.post("b", "c", "hello")
    w.tick(board)
    w.tick(board)
    assert len(calls) == 1 and board.get_standby("a", "c") is not None


def test_run_wait_returns_on_a_message(board):
    board.read_new("a", "c")
    threading.Timer(1.0, lambda: Board(board.path).post("b", "c", "wake up")).start()
    out = wake.run_wait(board, "a", "c", timeout=20)
    assert out.startswith("POWERCHANNEL: 1 new on #c for a") and "b: wake up" in out
    assert board.get_standby("a", "c") is None
    assert not board.has_unseen("a", "c")


def test_run_wait_for_a_new_name_skips_old_messages(board):
    board.post("b", "c", "old news")
    threading.Timer(1.0, lambda: Board(board.path).post("b", "c", "fresh")).start()
    out = wake.run_wait(board, "newcomer", "c", timeout=20)
    assert "b: fresh" in out and "old news" not in out


def test_run_wait_times_out(board):
    board.read_new("a", "c")
    out = wake.run_wait(board, "a", "c", timeout=1)
    assert "no new messages" in out and board.get_standby("a", "c") is None


def test_run_wait_skips_when_another_wait_runs(board):
    board.set_standby("a", "c", "shell", "", 999999)
    assert "already running" in wake.run_wait(board, "a", "c", timeout=5)


def test_wait_command_end_to_end(db_path):
    b = Board(db_path)
    b.post("other", "c", "first")
    b.read_new("w", "c")
    p = subprocess.Popen([sys.executable, "-m", "powerchannel", "wait", "--me", "w", "--channel", "c",
                          "--timeout", "30", "--db", str(db_path)],
                         stdout=subprocess.PIPE, text=True, encoding="utf-8")
    for _ in range(50):
        row = b.get_standby("w", "c")
        if row:
            break
        time.sleep(0.2)
    assert row and row.route == "shell"
    assert "on standby: w" in subprocess.run(
        [sys.executable, "-m", "powerchannel", "channels"], capture_output=True, text=True,
        env={**os.environ, "POWERCHANNEL_DB": str(db_path)}).stdout
    b.post("other", "c", "héllo ✓")
    out, _ = p.communicate(timeout=20)
    assert "POWERCHANNEL: 1 new on #c for w" in out and "other: héllo ✓" in out
    b.close()
