"""Real Chromium against a local fake site (never touches webnovel.com).

These exercise the actual Playwright locators, stability logic, click safety and
verification flow. They are skipped if Chromium cannot be launched.
"""

from __future__ import annotations

import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from webnovel_importer.browser import PlaywrightDriver, check_auth, ensure_authenticated
from webnovel_importer.config import Config
from webnovel_importer.importer import Importer, LoginRequired, run_batch
from webnovel_importer.page_detection import PageState as S
from webnovel_importer.persistence import ProgressStore

pw = pytest.importorskip("playwright.sync_api")


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a3c80000000049454e44ae426082")


class Site:
    def __init__(self):
        self.library: set[str] = set()
        self.posts: list[str] = []
        self.reads: list[str] = []
        self.hits: Counter = Counter()
        self.logged_in = False


def make_handler(site: Site):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, headers=None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            name = self.path.rsplit("/", 1)[-1]
            if self.path.startswith("/api/add/"):
                site.posts.append(name)
                if not name.startswith("noop-"):
                    site.library.add(name)
            elif self.path.startswith("/api/read/"):
                site.reads.append(name)
            self._send(200, "{}")

        def do_GET(self):
            if self.path == "/":
                nav = '<a href="/me">Profile</a>' if site.logged_in else '<a href="/x">Log in</a>'
                return self._send(200, f"<html><body><nav>{nav}</nav></body></html>")
            if self.path.startswith("/img.png"):
                site.hits["img:" + self.path] += 1
                data = PNG
                self.send_response(200); self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
                return
            if self.path.startswith("/app.js"):
                site.hits["app.js"] += 1
                body = b"window.x = 1"
                self.send_response(200); self.send_header("Content-Type", "application/javascript")
                self.send_header("Cache-Control", "max-age=3600"); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return
            if self.path.startswith("/imgpage"):
                return self._send(200, f'<html><body><script src="/app.js"></script><img src="/img.png?{self.path[-1]}"></body></html>',
                                  {"Cache-Control": "no-store"})
            name = self.path.rsplit("/", 1)[-1]
            site.hits[name] += 1
            if name.startswith("gone-"):
                return self._send(404, "<h1>Whoops!</h1><p>We might have some troubles to find out this page.</p>")
            if name.startswith("429-"):
                return self._send(429, "slow down", {"Retry-After": "0"})
            if name.startswith("flaky-") and site.hits[name] == 1:
                return self._send(503, "try later")
            in_lib = name in site.library or name.startswith("in-")
            label = "&#10003; IN LIBRARY" if in_lib else "+ ADD TO LIBRARY"
            extra = ""
            script = f"""
              const b = document.getElementById('lib');
              b.onclick = async () => {{ await fetch('/api/add/{name}', {{method:'POST'}}); b.textContent = '\\u2713 IN LIBRARY'; }};
              document.getElementById('read').onclick = () => fetch('/api/read/{name}', {{method:'POST'}});
            """
            if name.startswith("hydrate-"):  # renders ADD first, flips to IN LIBRARY after 600 ms
                label, in_lib = "+ ADD TO LIBRARY", True
                site.library.add(name)
                script += "setTimeout(() => { b.textContent = '\\u2713 IN LIBRARY'; }, 600);"
            if name.startswith("dupe-"):
                extra = "<button>+ ADD TO LIBRARY</button>"
            if name.startswith("login-"):
                extra = '<a href="/login">Sign in</a>'
            if name.startswith("nobtn-"):
                return self._send(200, "<html><body><h1>Book</h1><button id=read>READ</button></body></html>")
            marker = ""
            if name.startswith("m"):  # logged-in header marker: icon-only avatar link, empty alt
                avatar = '<a class="db j_header_user_trigger" href="#"><img alt="" src="/img.png?avatar"></a>'
                if name.startswith("mlate-"):
                    script += f"setTimeout(() => {{ document.getElementById('hdr').innerHTML = '{avatar}'; }}, 1500);"
                else:
                    marker = avatar
            if name.startswith("mdaily-"):  # logged-in page that still has a "Sign in" button (daily check-in)
                extra = "<button>Sign in</button> for a day get 1 spirit stone"
            html = (f"<html><body><header id=hdr>{marker}</header><h1>Book {name}</h1><button id=read>READ</button>"
                    f"<button id=lib>{label}</button>{extra}<script>{script}</script></body></html>")
            self._send(200, html)

    return H


@pytest.fixture(scope="module")
def site():
    s = Site()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
    s.base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield s
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()


def cfg_for(site, **kw):
    base = dict(
        allowed_domains=["127.0.0.1"], home_url=site.base + "/", action_timeout_s=4, navigation_timeout_s=10,
        stability_window_s=0.3, min_delay_s=0, max_delay_s=0, backoff_base_s=0.01, retries=1,
        rate_limit_pause_threshold=2,
    )
    base.update(kw)
    return Config(**base)


def url(site, name):
    return f"{site.base}/book/{name}"


def state_of(site, page, name, **kw):
    cfg = cfg_for(site, **kw)
    d = PlaywrightDriver(page, cfg)
    d.goto(url(site, name))
    return d.settle()[0]


@pytest.mark.parametrize("name,expected", [
    ("add-1", S.NOT_IN_LIBRARY), ("in-1", S.IN_LIBRARY), ("gone-1", S.UNAVAILABLE),
    ("dupe-1", S.AMBIGUOUS), ("login-1", S.LOGIN_REQUIRED), ("nobtn-1", S.UNKNOWN),
])
def test_state_detection_against_real_dom(site, page, name, expected):
    assert state_of(site, page, name, action_timeout_s=2) is expected


def test_hydration_flip_is_not_mistaken_for_not_in_library(site, page):
    before = len(site.posts)
    assert state_of(site, page, "hydrate-1") is S.IN_LIBRARY
    assert len(site.posts) == before


def test_off_site_redirect_is_flagged(site, page):
    cfg = cfg_for(site, allowed_domains=["example.invalid"])  # real host now "unexpected"
    d = PlaywrightDriver(page, cfg)
    d.goto(url(site, "add-1"))
    assert d.settle()[0] is S.OFF_SITE


def test_add_flow_clicks_only_library_button_and_verifies(site, page, tmp_path):
    cfg = cfg_for(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    imp = Importer(PlaywrightDriver(page, cfg), store, cfg)
    rec = imp.process_url(url(site, "add-2"))
    assert rec["status"] == "added" and "reload" in rec["details"]
    assert site.posts.count("add-2") == 1 and "add-2" not in site.reads  # READ never clicked


def test_already_in_library_is_not_clicked(site, page, tmp_path):
    cfg = cfg_for(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    before = list(site.posts)
    rec = Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "in-2"))
    assert rec["status"] == "already_in_library" and site.posts == before


def test_unavailable_recorded_without_click_or_retry(site, page, tmp_path):
    cfg = cfg_for(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    rec = Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "gone-2"))
    assert rec["status"] == "unavailable" and rec["attempts"] == 1 and rec["http_status"] == 404


def test_optimistic_ui_without_server_persistence_is_not_reported_as_added(site, page, tmp_path):
    cfg = cfg_for(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    rec = Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "noop-1"))
    assert rec["status"] == "verification_failed" and rec["click_attempted"] is True


def test_ambiguous_controls_refuse_to_click(site, page, tmp_path):
    cfg = cfg_for(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    before = list(site.posts)
    rec = Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "dupe-2"))
    assert rec["status"] == "ui_changed" and site.posts == before


def test_missing_button_is_ui_changed_after_limited_retries(site, page, tmp_path):
    cfg = cfg_for(site, action_timeout_s=1, retries=1)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    rec = Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "nobtn-2"))
    assert rec["status"] == "ui_changed" and rec["attempts"] == 2


def test_login_prompt_raises_instead_of_recording_failure(site, page, tmp_path):
    cfg = cfg_for(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    with pytest.raises(LoginRequired):
        Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "login-2"))
    assert store.get(url(site, "login-2")) is None


def test_transient_503_is_retried(site, page, tmp_path):
    cfg = cfg_for(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    rec = Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "flaky-1"))
    assert rec["status"] == "added" and rec["attempts"] == 2


def test_repeated_429_pauses_the_run(site, page, tmp_path):
    cfg = cfg_for(site, retries=0)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    imp = Importer(PlaywrightDriver(page, cfg), store, cfg)
    msgs = []

    class R:
        def item_start(self, *a): pass
        def item_done(self, *a): pass
        def message(self, t): msgs.append(t)

    items = [(i, url(site, f"429-{i}")) for i in range(1, 5)]
    res = run_batch(imp, items, 4, R(), lambda: False, sleep=lambda s: None)
    assert res.stopped == "rate_limit" and len(store.results) == 2 and msgs
    assert all(r["status"] == "network_error" for r in store.results.values())


def test_auth_check_and_manual_login_flow(site, page):
    cfg = cfg_for(site)
    site.logged_in = False
    assert not check_auth(page, cfg).ok
    answers = iter([""])

    def prompt(_):
        site.logged_in = True  # user "logs in" in the browser, then presses Enter
        return next(answers)

    out = []
    assert ensure_authenticated(page, cfg, prompt=prompt, out=out.append)
    assert check_auth(page, cfg).ok


def test_auth_quit_returns_false(site, page):
    site.logged_in = False
    assert not ensure_authenticated(page, cfg_for(site), prompt=lambda _: "q", out=lambda s: None)


def test_account_marker_selector_detects_icon_only_avatar(site, page):
    from webnovel_importer.page_detection import take_snapshot
    cfg = cfg_for(site, labels={**Config().labels, "account_marker": None,
                                "account_marker_selector": "a.j_header_user_trigger"})
    page.goto(site.base + "/")
    page.set_content('<div class="g_user j_header_user"><a class="db j_header_user_trigger" href="#"><img width=20 height=20></a></div>')
    assert take_snapshot(page, cfg).account_markers == 1
    page.set_content("<div class='g_user'></div>")
    assert take_snapshot(page, cfg).account_markers == 0


# ---------------- account marker / daily "Sign in" / resource blocking ----------------
MARKER = {"account_marker": None, "account_marker_selector": "a.j_header_user_trigger"}


def marker_cfg(site, **kw):
    kw.setdefault("login_confirm_window_s", 0.8)
    return cfg_for(site, labels={**Config().labels, **MARKER}, **kw)


def test_signin_button_on_logged_in_page_does_not_trigger_login_pause(site, page, tmp_path):
    cfg = marker_cfg(site)
    store = ProgressStore(tmp_path / "p.json"); store.load()
    rec = Importer(PlaywrightDriver(page, cfg), store, cfg).process_url(url(site, "mdaily-1"))
    assert rec["status"] == "added"


def test_without_marker_configured_signin_button_still_means_login(site, page):
    # legacy behaviour is preserved when no marker is configured
    assert state_of(site, page, "mdaily-2") is S.LOGIN_REQUIRED


def test_logged_out_book_page_with_marker_configured_is_login_required(site, page):
    d = PlaywrightDriver(page, marker_cfg(site))
    d.goto(url(site, "add-9"))  # no header marker on this page
    assert d.settle()[0] is S.LOGIN_REQUIRED


def test_late_hydrating_marker_is_not_mistaken_for_logout(site, page):
    cfg = marker_cfg(site, login_confirm_window_s=3.0, action_timeout_s=8)
    d = PlaywrightDriver(page, cfg)
    d.goto(url(site, "mlate-1"))  # marker appears after 1.5 s
    assert d.settle()[0] is S.NOT_IN_LIBRARY
