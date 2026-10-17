import json

import pytest

from webnovel_importer import cli
from webnovel_importer.models import ADDED, TIMEOUT
from webnovel_importer.persistence import ProgressStore

A = "https://www.webnovel.com/book/a_1"
B = "https://www.webnovel.com/book/b_2"


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "urls.txt").write_text(f"{A}\nnope\n{A}\n{B}\n")
    (tmp_path / "config.json").write_text(json.dumps({}))
    return tmp_path


def run(proj, *args):
    return cli.main(["--config", str(proj / "config.json"), *args])


def snapshot(proj):
    return sorted(str(p.relative_to(proj)) for p in proj.rglob("*"))


def test_validate_reports_and_changes_nothing(proj, capsys):
    before = snapshot(proj)
    assert run(proj, "validate") == 0
    out = capsys.readouterr().out
    assert "Valid unique URLs: 2" in out and "Invalid URLs: 1" in out and "Duplicate URLs: 1" in out
    assert snapshot(proj) == before


def test_validate_missing_input_is_error(proj, capsys):
    assert run(proj, "validate", "--input", str(proj / "missing.txt")) == 1


def test_status_is_read_only_and_shows_breakdown(proj, capsys):
    store = ProgressStore(proj / "data" / "progress.json"); store.load()
    store.record(A, ADDED, 1, "t", "t"); store.save()
    before = snapshot(proj)
    assert run(proj, "status") == 0
    out = capsys.readouterr().out
    assert "Completed (added/already/unavailable): 1" in out and "Pending: 1" in out
    assert snapshot(proj) == before


def test_status_does_not_move_corrupt_checkpoint(proj, capsys):
    p = proj / "data" / "progress.json"; p.parent.mkdir(); p.write_text("{bad")
    assert run(proj, "status") == 0
    assert p.read_text() == "{bad" and "Warning" in capsys.readouterr().out


def test_report_writes_csv_with_every_line(proj, capsys):
    assert run(proj, "report", "--status", "invalid_url") == 0
    csv_text = (proj / "data" / "results.csv").read_text()
    assert csv_text.count("\n") == 5  # header + 4 entries
    assert "nope" in capsys.readouterr().out


def test_reset_requires_confirmation(proj, monkeypatch):
    store = ProgressStore(proj / "data" / "progress.json"); store.load()
    store.record(A, ADDED, 1, "t", "t"); store.save()
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert run(proj, "reset") == 1
    assert (proj / "data" / "progress.json").exists()
    monkeypatch.setattr("builtins.input", lambda _: "RESET")
    assert run(proj, "reset") == 0
    assert not (proj / "data" / "progress.json").exists()
    assert list((proj / "data").glob("progress.json.bak-*"))


def test_run_with_nothing_pending_never_launches_browser(proj, monkeypatch, capsys):
    store = ProgressStore(proj / "data" / "progress.json"); store.load()
    store.record(A, ADDED, 1, "t", "t"); store.record(B, TIMEOUT, 1, "t", "t"); store.save()

    def boom(*a, **k):
        raise AssertionError("browser must not launch")
    monkeypatch.setattr(cli, "_launch", boom)
    assert run(proj, "run") == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_bad_config_is_reported(proj, capsys):
    (proj / "config.json").write_text(json.dumps({"retries": -5}))
    assert run(proj, "validate") == 1
    assert "Config error" in capsys.readouterr().err


def test_cli_override_validation(proj, capsys):
    assert run(proj, "validate", "--input", str(proj / "urls.txt")) == 0
