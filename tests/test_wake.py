import os
import subprocess
import sys
import threading
import time

from powerchannel import wake
from powerchannel.store import Board





def test_wait_output_marks_human_messages_as_the_users(board):
    board.read_new("a", "c")
    threading.Timer(0.5, lambda: Board(board.path).post("human", "c", "stop now")).start()
    out = wake.run_wait(board, "a", "c", timeout=20)
    assert "human: stop now" in out and "from human were posted from the command line" in out


def test_wait_command_has_no_spaces_to_quote(monkeypatch, tmp_path):
    spaced = tmp_path / "with space"
    spaced.mkdir()
    monkeypatch.setenv("POWERCHANNEL_DB", str(spaced / "db.sqlite"))
    cmd = wake.wait_command("a", "c")
    db_part = cmd.split("--db ", 1)[1]
    assert "\\" not in cmd and (" " not in db_part or db_part.startswith('"'))


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
