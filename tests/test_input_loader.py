import pytest

from webnovel_importer.input_loader import (
    InputError, UrlError, display_name, fingerprint, load_urls, normalize_url,
)
from webnovel_importer.models import DUPLICATE_URL, INVALID_URL

DOM = ["webnovel.com"]
A = "https://www.webnovel.com/book/shadow-slave_22196546206090805"


def write(tmp_path, text, name="urls.txt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("raw", [
    A,
    A + "/",
    "http://www.webnovel.com/book/shadow-slave_22196546206090805",
    "https://webnovel.com/book/shadow-slave_22196546206090805",
    "HTTPS://WWW.WEBNOVEL.COM/book/shadow-slave_22196546206090805?utm=1#frag",
    "https://www.webnovel.com/book/12345678",
    "https://m.webnovel.com/book/shadow-slave_22196546206090805",
    "https://www.webnovel.com/en/book/shadow-slave_22196546206090805",
])
def test_valid_urls_normalize(raw):
    out = normalize_url(raw, DOM)
    assert out.startswith("https://")
    assert "?" not in out and "#" not in out and not out.endswith("/")


def test_normalization_is_canonical():
    assert normalize_url("http://webnovel.com/book/x_1/?a=b", DOM) == "https://www.webnovel.com/book/x_1"


@pytest.mark.parametrize("raw,fragment", [
    ("ftp://www.webnovel.com/book/a", "scheme"),
    ("javascript:alert(1)", "scheme"),
    ("www.webnovel.com/book/a", "scheme"),
    ("https://webnovel.com.evil.com/book/a", "allowed"),
    ("https://evilwebnovel.com/book/a", "allowed"),
    ("https://evil.com/webnovel.com/book/a", "allowed"),
    ("https://webnovel.com@evil.com/book/a", "credentials"),
    ("https://www.webnovel.com:8443/book/a", "port"),
    ("https://www.webnovel.com/", "/book/"),
    ("https://www.webnovel.com/book", "identifier"),
    ("https://www.webnovel.com/book/a/chapter_1", "chapter"),
    ("https://www.webnovel.com/book/a b", "whitespace"),
    ("https://", "host"),
    ("https://[::1/book/a", "malformed"),
])
def test_invalid_urls_rejected(raw, fragment):
    with pytest.raises(UrlError) as ei:
        normalize_url(raw, DOM)
    assert fragment in str(ei.value)


def test_non_strict_path_allows_other_paths():
    assert normalize_url("https://www.webnovel.com/anything/here", DOM, strict_book_path=False).endswith("/anything/here")


def test_load_filters_comments_blanks_whitespace_and_keeps_order(tmp_path):
    p = write(tmp_path, f"# header\n\n   {A}   \n#another\nhttps://www.webnovel.com/book/b_2\n\t\nhttps://www.webnovel.com/book/c_3\n")
    r = load_urls(p, DOM)
    assert r.valid_urls == [A, "https://www.webnovel.com/book/b_2", "https://www.webnovel.com/book/c_3"]
    assert r.total_lines == 7 and r.skipped_lines == 4
    assert not r.invalid and not r.duplicates


def test_duplicates_removed_preserving_first_occurrence(tmp_path):
    p = write(tmp_path, f"{A}\nhttps://www.webnovel.com/book/b_2\n{A}/\nhttp://webnovel.com/book/shadow-slave_22196546206090805?x=1\n")
    r = load_urls(p, DOM)
    assert r.valid_urls == [A, "https://www.webnovel.com/book/b_2"]
    assert [e.line_no for e in r.duplicates] == [3, 4]
    assert all(e.status == DUPLICATE_URL and "line 1" in e.reason for e in r.duplicates)


def test_invalid_lines_recorded_not_dropped(tmp_path):
    p = write(tmp_path, f"garbage\n{A}\nhttps://evil.com/book/a\n")
    r = load_urls(p, DOM)
    assert [(e.line_no, e.raw, e.status) for e in r.invalid] == [(1, "garbage", INVALID_URL), (3, "https://evil.com/book/a", INVALID_URL)]
    assert r.valid_urls == [A]
    assert len(r.entries) == 3  # every non-blank line is accounted for


def test_empty_file(tmp_path):
    r = load_urls(write(tmp_path, ""), DOM)
    assert r.valid_urls == [] and r.total_lines == 0 and r.entries == []


def test_comments_only_file(tmp_path):
    r = load_urls(write(tmp_path, "# a\n# b\n"), DOM)
    assert r.valid_urls == [] and r.skipped_lines == 2


def test_bom_and_crlf(tmp_path):
    p = tmp_path / "u.txt"
    p.write_bytes(b"\xef\xbb\xbf" + (A + "\r\n" + A.replace("shadow", "x") + "\r\n").encode())
    assert len(load_urls(p, DOM).valid_urls) == 2


def test_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(InputError, match="not found"):
        load_urls(tmp_path / "nope.txt", DOM)


def test_non_utf8_file_raises(tmp_path):
    p = tmp_path / "u.txt"
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(InputError):
        load_urls(p, DOM)


def test_fingerprint_is_order_sensitive_and_stable():
    assert fingerprint(["a", "b"]) == fingerprint(["a", "b"])
    assert fingerprint(["a", "b"]) != fingerprint(["b", "a"])


def test_display_name():
    assert display_name(A) == "Shadow Slave"
    assert display_name("https://www.webnovel.com/book/12345678") == "12345678"
