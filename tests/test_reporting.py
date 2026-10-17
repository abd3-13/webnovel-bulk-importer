import csv
import io
from datetime import datetime

from webnovel_importer.input_loader import load_urls
from webnovel_importer.models import ADDED, PENDING, TIMEOUT, UNAVAILABLE
from webnovel_importer.persistence import ProgressStore
from webnovel_importer.reporting import COLUMNS, build_rows, count_statuses, rows_to_csv, write_results_csv

A = "https://www.webnovel.com/book/a_1"
B = "https://www.webnovel.com/book/b_2"


def setup(tmp_path, text):
    p = tmp_path / "urls.txt"
    p.write_text(text)
    load = load_urls(p, ["webnovel.com"])
    store = ProgressStore(tmp_path / "progress.json"); store.load()
    return load, store


def test_required_columns_present():
    for c in ["url", "status", "attempts", "started_at", "completed_at", "http_status", "details"]:
        assert c in COLUMNS


def test_every_input_entry_gets_a_row(tmp_path):
    load, store = setup(tmp_path, f"{A}\nnot a url\n{A}\n{B}\n")
    store.record(A, ADDED, 1, "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:05+00:00", 200, "ok")
    rows = build_rows(load, store)
    assert [(r["input_line"], r["status"]) for r in rows] == [
        (1, ADDED), (2, "invalid_url"), (3, "duplicate_url"), (4, PENDING)]
    assert rows[1]["url"] == "not a url"


def test_orphan_results_are_kept(tmp_path):
    load, store = setup(tmp_path, f"{A}\n")
    store.record(B, UNAVAILABLE, 1, "t", "t", 404, "gone")
    rows = build_rows(load, store)
    assert rows[-1]["url"] == B and "not in current input" in rows[-1]["details"]


def test_csv_escaping_roundtrip(tmp_path):
    load, store = setup(tmp_path, f"{A}\n")
    nasty = 'has, comma "quote"\nand newline'
    store.record(A, TIMEOUT, 3, "t", "t", None, nasty)
    text = rows_to_csv(build_rows(load, store))
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["details"] == 'has, comma "quote" and newline'  # newlines collapsed, rest escaped
    assert parsed[0]["http_status"] == "" and parsed[0]["attempts"] == "3"
    assert text.splitlines()[0] == ",".join(COLUMNS)


def test_details_truncated(tmp_path):
    load, store = setup(tmp_path, f"{A}\n")
    store.record(A, TIMEOUT, 1, "t", "t", None, "x" * 5000)
    assert len(build_rows(load, store)[0]["details"]) <= 500


def test_timestamps_from_importer_are_iso8601():
    from webnovel_importer.models import now_iso
    datetime.fromisoformat(now_iso())


def test_write_results_csv_is_atomic_file(tmp_path):
    load, store = setup(tmp_path, f"{A}\nbad\n")
    out = tmp_path / "data" / "results.csv"
    rows = write_results_csv(out, load, store)
    assert out.exists() and len(rows) == 2
    assert [p.name for p in out.parent.iterdir()] == ["results.csv"]
    counts = count_statuses(rows)
    assert counts[PENDING] == 1 and counts["invalid_url"] == 1
