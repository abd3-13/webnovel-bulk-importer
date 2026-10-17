import json

import pytest

from webnovel_importer import persistence
from webnovel_importer.models import ADDED, ALREADY_IN_LIBRARY, TIMEOUT, UNAVAILABLE, VERIFICATION_FAILED
from webnovel_importer.persistence import ProgressStore, atomic_write_text

U1, U2, U3 = (f"https://www.webnovel.com/book/b{i}_{i}" for i in (1, 2, 3))


def rec(store, url, status, **kw):
    return store.record(url, status, kw.pop("attempts", 1), "t0", "t1", **kw)


def test_missing_file_starts_empty_and_save_creates_it(tmp_path):
    s = ProgressStore(tmp_path / "data" / "progress.json")
    s.load()
    assert s.results == {} and not s.existed and s.load_warning is None
    s.save()
    data = json.loads((tmp_path / "data" / "progress.json").read_text())
    assert data["schema_version"] == 1 and data["results"] == {}


def test_roundtrip_and_resume(tmp_path):
    p = tmp_path / "progress.json"
    s = ProgressStore(p); s.load()
    rec(s, U1, ADDED); rec(s, U2, TIMEOUT)
    s.save()
    s2 = ProgressStore(p); s2.load()
    urls = [U1, U2, U3]
    assert s2.pending(urls) == [U3]
    assert s2.unresolved(urls) == [U2]
    assert s2.completed(urls) == [U1]


def test_unresolved_filter_by_status(tmp_path):
    s = ProgressStore(tmp_path / "p.json"); s.load()
    rec(s, U1, TIMEOUT); rec(s, U2, VERIFICATION_FAILED)
    assert s.unresolved([U1, U2], only={TIMEOUT}) == [U1]


def test_success_never_overwritten_by_failure(tmp_path):
    s = ProgressStore(tmp_path / "p.json"); s.load()
    assert rec(s, U1, ADDED)
    assert not rec(s, U1, TIMEOUT)
    assert not rec(s, U1, ALREADY_IN_LIBRARY)
    assert s.status_of(U1) == ADDED


def test_failure_can_be_replaced_by_success(tmp_path):
    s = ProgressStore(tmp_path / "p.json"); s.load()
    rec(s, U1, VERIFICATION_FAILED, click_attempted=True)
    assert rec(s, U1, ADDED)
    assert s.status_of(U1) == ADDED


def test_unavailable_counts_as_completed(tmp_path):
    s = ProgressStore(tmp_path / "p.json"); s.load()
    rec(s, U1, UNAVAILABLE)
    assert s.completed([U1]) == [U1] and s.unresolved([U1]) == []


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"schema_version": 1, "results": []}',
                                      '{"schema_version": 99, "results": {}}',
                                      '{"schema_version": 1, "results": {"u": {"nostatus": 1}}}'])
def test_malformed_checkpoint_is_quarantined_not_crashing(tmp_path, content):
    p = tmp_path / "progress.json"
    p.write_text(content)
    s = ProgressStore(p); s.load()
    assert s.results == {} and s.load_warning
    assert not p.exists()
    assert list(tmp_path.glob("progress.json.corrupt-*"))


def test_read_only_load_does_not_touch_corrupt_file(tmp_path):
    p = tmp_path / "progress.json"
    p.write_text("{bad")
    s = ProgressStore(p); s.load(quarantine=False)
    assert s.load_warning and p.read_text() == "{bad"


def test_atomic_write_replaces_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "f.txt"
    atomic_write_text(p, "one"); atomic_write_text(p, "two")
    assert p.read_text() == "two"
    assert [x.name for x in tmp_path.iterdir()] == ["f.txt"]


def test_interrupted_write_preserves_previous_checkpoint(tmp_path, monkeypatch):
    p = tmp_path / "progress.json"
    s = ProgressStore(p); s.load()
    rec(s, U1, ADDED); s.save()
    good = p.read_text()
    rec(s, U2, ADDED)

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(persistence.os, "replace", boom)
    with pytest.raises(OSError):
        s.save()
    monkeypatch.undo()
    assert p.read_text() == good  # old checkpoint intact
    assert [x.name for x in tmp_path.iterdir()] == ["progress.json"]  # temp file cleaned up
    s2 = ProgressStore(p); s2.load()
    assert list(s2.results) == [U1]


def test_input_change_detection(tmp_path):
    s = ProgressStore(tmp_path / "p.json"); s.load()
    assert s.input_changed("abc") is None
    s.set_input(tmp_path / "urls.txt", "abc", 3); s.save()
    s2 = ProgressStore(tmp_path / "p.json"); s2.load()
    assert s2.input_changed("abc") is False
    assert s2.input_changed("xyz") is True


def test_reset_clears_results(tmp_path):
    s = ProgressStore(tmp_path / "p.json"); s.load()
    rec(s, U1, ADDED); s.reset()
    assert s.results == {}
