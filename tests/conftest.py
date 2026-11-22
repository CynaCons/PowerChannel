import pytest

from powerchannel import server
from powerchannel.store import Board


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    monkeypatch.setenv("POWERCHANNEL_DB", str(path))
    monkeypatch.setattr(server, "_board", None)
    monkeypatch.setattr(server, "_joined", set())
    yield path
    if server._board is not None:
        server._board.close()
        server._board = None


@pytest.fixture
def board(db_path):
    b = Board(db_path)
    yield b
    b.close()
