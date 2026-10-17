import pytest

from webnovel_importer.models import FAILED, NETWORK_ERROR, TIMEOUT
from webnovel_importer.page_detection import (
    is_login_url,
    PageState as S, Snapshot, UiError, classify, classify_exception, is_authenticated,
)


@pytest.mark.parametrize("kw,expected", [
    (dict(add_count=1), S.NOT_IN_LIBRARY),
    (dict(in_count=1), S.IN_LIBRARY),
    (dict(unavailable_title=1, unavailable_body=1), S.UNAVAILABLE),
    (dict(unavailable_title=1), S.UNAVAILABLE),
    (dict(unavailable_body=1), S.UNAVAILABLE),
    (dict(), S.UNKNOWN),
    (dict(add_count=2), S.AMBIGUOUS),
    (dict(add_count=1, in_count=1), S.AMBIGUOUS),
    (dict(in_count=2), S.AMBIGUOUS),
    (dict(add_count=1, login_controls=1), S.LOGIN_REQUIRED),
    (dict(login_controls=1), S.LOGIN_REQUIRED),
    (dict(login_url=True, add_count=1), S.LOGIN_REQUIRED),
    (dict(in_count=1, login_controls=1), S.IN_LIBRARY),  # IN LIBRARY implies an account
    (dict(on_allowed_host=False, host="evil.com", add_count=1), S.OFF_SITE),
    # A book page that merely mentions "Whoops!" but has a library control is NOT unavailable.
    (dict(unavailable_title=1, add_count=1), S.NOT_IN_LIBRARY),
])
def test_classify(kw, expected):
    assert classify(Snapshot(**kw))[0] is expected


def test_off_site_beats_everything():
    state, detail = classify(Snapshot(on_allowed_host=False, host="x.com", login_url=True, in_count=1))
    assert state is S.OFF_SITE and "x.com" in detail


def test_is_authenticated_needs_marker_when_required():
    ok, ind = is_authenticated(Snapshot(account_markers=0), require_marker=True)
    assert not ok and ind["no_login_control_visible"]
    assert is_authenticated(Snapshot(account_markers=1), require_marker=True)[0]
    assert is_authenticated(Snapshot(account_markers=0), require_marker=False)[0]


@pytest.mark.parametrize("kw", [dict(login_controls=1), dict(login_url=True, account_markers=1),
                                dict(on_allowed_host=False, account_markers=1)])
def test_is_authenticated_rejects_login_signals(kw):
    assert not is_authenticated(Snapshot(**kw), require_marker=False)[0]


class TimeoutError(Exception):  # same class name Playwright uses
    pass


@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("Timeout 45000ms exceeded."), (TIMEOUT, True)),
    (Exception("Page.goto: net::ERR_CONNECTION_RESET at https://x"), (NETWORK_ERROR, True)),
    (Exception("Page.goto: net::ERR_INTERNET_DISCONNECTED"), (NETWORK_ERROR, True)),
    (UiError("no button"), ("ui_changed", False)),
    (ValueError("boom"), (FAILED, False)),
])
def test_classify_exception(exc, expected):
    assert classify_exception(exc) == expected


def test_auth_check_reports_load_failure_instead_of_crashing():
    from webnovel_importer.browser import check_auth, ensure_authenticated
    from webnovel_importer.config import Config

    class Page:
        url = ""
        def goto(self, *a, **k):
            raise Exception("Page.goto: net::ERR_INTERNET_DISCONNECTED at https://x/\nCall log: ...")

    res = check_auth(Page(), Config())
    assert not res.ok and "ERR_INTERNET_DISCONNECTED" in res.error
    out, asked = [], []
    assert ensure_authenticated(Page(), Config(), prompt=lambda _: asked.append(1) or "", out=out.append) is False
    assert not asked and any("network" in line for line in out)  # no misleading login prompt


# ---- logged-in marker overrides login-looking controls (e.g. daily "Sign in" promo) ----
@pytest.mark.parametrize("kw,expected", [
    (dict(add_count=1, login_controls=1, account_markers=1, marker_configured=True), S.NOT_IN_LIBRARY),
    (dict(add_count=1, login_controls=1, marker_configured=True), S.LOGIN_REQUIRED),
    (dict(add_count=1, marker_configured=True), S.LOGIN_REQUIRED),  # marker configured but absent
    (dict(add_count=1, account_markers=1, marker_configured=True), S.NOT_IN_LIBRARY),
    (dict(add_count=1, marker_configured=False), S.NOT_IN_LIBRARY),  # no marker configured: legacy rule
    (dict(marker_configured=True), S.UNKNOWN),  # unloaded page is retried, not a login pause
    (dict(in_count=1, marker_configured=True), S.IN_LIBRARY),
    (dict(login_url=True, add_count=1, account_markers=1, marker_configured=True), S.LOGIN_REQUIRED),
])
def test_classify_with_account_marker(kw, expected):
    assert classify(Snapshot(**kw))[0] is expected


def test_marker_overrides_signin_control_for_auth():
    assert is_authenticated(Snapshot(login_controls=1, account_markers=1), require_marker=True)[0]
    assert not is_authenticated(Snapshot(login_controls=1), require_marker=False)[0]


# ---- login URL detection must not fire on novel slugs ----
import re as _re
_PAT = _re.compile(r"(log-?in|sign-?in|sign-?up|passport|oauth)", _re.IGNORECASE)


@pytest.mark.parametrize("url,expected", [
    ("https://www.webnovel.com/book/signing-in-for-eight-years-i-was-exposed-as-a-zillionaire_123", False),
    ("https://www.webnovel.com/book/login-system_123", False),
    ("https://www.webnovel.com/book/sign-in-god_123", False),
    ("https://www.webnovel.com/en/book/sign-in_1", False),
    ("https://www.webnovel.com/login", True),
    ("https://www.webnovel.com/signin", True),
    ("https://www.webnovel.com/sign-in/", True),
    ("https://www.webnovel.com/en/login", True),
    ("https://passport.webnovel.com/emaillogin.html", True),   # host label 'passport'
    ("https://passport.webnovel.com/anything", True),          # host label 'passport'
    ("https://www.webnovel.com/", False),
])
def test_is_login_url(url, expected):
    assert is_login_url(url, _PAT, []) is expected


def test_is_login_url_auth_hosts():
    assert is_login_url("https://accounts.google.com/x", _PAT, ["accounts.google.com"])
