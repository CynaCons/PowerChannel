import pytest

from powerchannel import store
from powerchannel.cli import main_deprecated
from powerchannel.store import Board


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.delenv("POWERCHANNEL_DB", raising=False)
    monkeypatch.delenv("POWERBOARD_DB", raising=False)
    monkeypatch.setattr(store, "_data_base", lambda: tmp_path)


def make_legacy(tmp_path, text="legacy hello"):
    legacy = tmp_path / "powerboard" / "board.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    b = Board(legacy)
    b.post("human", "release", text)
    b.close()
    return legacy


def test_legacy_db_is_migrated_to_the_new_location(tmp_path):
    make_legacy(tmp_path)
    path = store.default_db_path()
    assert path == tmp_path / "powerchannel" / "channels.db"
    assert path.exists()
    b = Board(path)
    try:
        assert [m.text for m in b.recent("release")] == ["legacy hello"]
    finally:
        b.close()


def test_second_call_does_not_copy_again(tmp_path):
    make_legacy(tmp_path)
    path = store.default_db_path()
    b = Board(path)
    b.post("human", "release", "new native")
    b.close()

    assert store.default_db_path() == path  # would re-copy if it migrated again
    b = Board(path)
    try:
        assert [m.text for m in b.recent("release")] == ["legacy hello", "new native"]
    finally:
        b.close()


def test_powerboard_db_env_is_returned_as_is(tmp_path, monkeypatch):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("POWERBOARD_DB", str(target))
    assert store.default_db_path() == target
    assert not (tmp_path / "powerchannel").exists()


def test_powerchannel_db_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERBOARD_DB", str(tmp_path / "old.db"))
    monkeypatch.setenv("POWERCHANNEL_DB", str(tmp_path / "new.db"))
    assert store.default_db_path() == tmp_path / "new.db"


def test_main_deprecated_prints_the_rename_notice(capsys):
    with pytest.raises(SystemExit):
        main_deprecated(["--version"])
    assert "powerboard was renamed powerchannel" in capsys.readouterr().err
