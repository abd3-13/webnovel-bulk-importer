import pytest

from webnovel_importer.cli import _select_items, build_parser
from webnovel_importer.config import Config
from webnovel_importer.importer import Importer, LoginRequired, NavResult, RateLimitPause, run_batch
from webnovel_importer.models import (
    ADDED, ALREADY_IN_LIBRARY, NETWORK_ERROR, TIMEOUT, UI_CHANGED, UNAVAILABLE, VERIFICATION_FAILED,
)
from webnovel_importer.page_detection import PageState as S, UiError
from webnovel_importer.persistence import ProgressStore

U = [f"https://www.webnovel.com/book/b{i}_{i}" for i in range(1, 6)]


class TimeoutError(Exception):  # mimics playwright's class name
    pass


class FakeDriver:
    def __init__(self, settle=(), nav=None, after_click=(S.IN_LIBRARY, "ok"), click_exc=None, goto_exc=None):
        self.settle_q = list(settle)
        self.nav_q = list(nav or [])
        self.after_click = after_click
        self.click_exc, self.goto_exc = click_exc, list(goto_exc or [])
        self.calls = []

    def goto(self, url):
        self.calls.append("goto")
        if self.goto_exc:
            e = self.goto_exc.pop(0)
            if e:
                raise e
        return self.nav_q.pop(0) if self.nav_q else NavResult(200)

    def settle(self):
        self.calls.append("settle")
        s = self.settle_q.pop(0)
        return s if isinstance(s, tuple) else (s, "fake")

    def click_add(self, before_click=None):
        self.calls.append("click")
        if self.click_exc:
            raise self.click_exc
        if before_click:
            before_click()

    def wait_after_click(self):
        self.calls.append("wait")
        return self.after_click

    def reload(self):
        self.calls.append("reload")
        return NavResult(200)


@pytest.fixture
def env(tmp_path):
    cfg = Config(retries=2, backoff_base_s=1.0, min_delay_s=3, max_delay_s=6, rate_limit_pause_threshold=3)
    store = ProgressStore(tmp_path / "progress.json"); store.load()
    sleeps = []
    return cfg, store, sleeps


def make(env, driver):
    cfg, store, sleeps = env
    return Importer(driver, store, cfg, sleep=sleeps.append, clock=lambda: "2026-01-01T00:00:00+00:00")


def test_already_in_library_never_clicks(env):
    d = FakeDriver([S.IN_LIBRARY])
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == ALREADY_IN_LIBRARY and "click" not in d.calls


def test_add_success_requires_state_observation_and_reload(env):
    d = FakeDriver([S.NOT_IN_LIBRARY, S.IN_LIBRARY])
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == ADDED and rec["click_attempted"]
    assert d.calls == ["goto", "settle", "click", "wait", "reload", "settle"]


def test_add_without_reload_verification_when_disabled(env):
    cfg, store, sleeps = env
    cfg.verify_with_reload = False
    d = FakeDriver([S.NOT_IN_LIBRARY])
    assert make(env, d).process_url(U[0])["status"] == ADDED
    assert "reload" not in d.calls


def test_click_without_exception_is_not_success_if_state_never_changes(env):
    d = FakeDriver([S.NOT_IN_LIBRARY, S.NOT_IN_LIBRARY], after_click=(S.NOT_IN_LIBRARY, "still add"))
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == VERIFICATION_FAILED and rec["click_attempted"]
    assert d.calls.count("click") == 1  # verification_failed is not auto-retried


def test_in_page_success_not_confirmed_by_reload(env):
    d = FakeDriver([S.NOT_IN_LIBRARY, S.NOT_IN_LIBRARY])
    assert make(env, d).process_url(U[0])["status"] == VERIFICATION_FAILED


def test_unseen_in_page_but_confirmed_after_reload_is_added(env):
    d = FakeDriver([S.NOT_IN_LIBRARY, S.IN_LIBRARY], after_click=(S.UNKNOWN, "?"))
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == ADDED and "confirmed after reload" in rec["details"]


def test_click_error_is_verification_failed_with_click_flag(env):
    d = FakeDriver([S.NOT_IN_LIBRARY], click_exc=RuntimeError("boom"))
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == VERIFICATION_FAILED and rec["click_attempted"]


def test_pre_click_ui_error_leaves_no_click_marker(env):
    d = FakeDriver([S.NOT_IN_LIBRARY], click_exc=UiError("2 buttons"))
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == UI_CHANGED and not rec["click_attempted"]


def test_unavailable_not_retried_and_not_clicked(env):
    d = FakeDriver([S.UNAVAILABLE], nav=[NavResult(404)])
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == UNAVAILABLE and d.calls == ["goto", "settle"] and rec["http_status"] == 404


def test_unknown_state_retried_limited_times_with_exponential_backoff(env):
    d = FakeDriver([S.UNKNOWN] * 3)
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == UI_CHANGED and rec["attempts"] == 3 and d.calls.count("goto") == 3
    assert env[2] == [1.0, 2.0]


def test_ambiguous_not_retried(env):
    d = FakeDriver([S.AMBIGUOUS])
    assert make(env, d).process_url(U[0])["status"] == UI_CHANGED and d.calls.count("goto") == 1


def test_timeout_retried_then_success(env):
    d = FakeDriver([S.IN_LIBRARY], goto_exc=[TimeoutError("Timeout 1ms exceeded")])
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == ALREADY_IN_LIBRARY and rec["attempts"] == 2


def test_persistent_timeout_recorded(env):
    d = FakeDriver(goto_exc=[TimeoutError("t")] * 3)
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == TIMEOUT and rec["attempts"] == 3


def test_network_error_classified_and_retried(env):
    d = FakeDriver([S.IN_LIBRARY], goto_exc=[Exception("net::ERR_CONNECTION_RESET")])
    assert make(env, d).process_url(U[0])["attempts"] == 2


def test_unrecoverable_error_is_failed_without_retry(env):
    d = FakeDriver(goto_exc=[ValueError("weird")])
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == "failed" and rec["attempts"] == 1


def test_login_required_propagates_and_records_nothing(env):
    d = FakeDriver([S.LOGIN_REQUIRED])
    with pytest.raises(LoginRequired):
        make(env, d).process_url(U[0])
    assert env[1].get(U[0]) is None


def test_prior_click_then_in_library_is_added(env):
    env[1].record(U[0], VERIFICATION_FAILED, 1, "t", "t", click_attempted=True)
    rec = make(env, FakeDriver([S.IN_LIBRARY])).process_url(U[0])
    assert rec["status"] == ADDED and "earlier click" in rec["details"] and rec["attempts"] == 2


def test_prior_success_is_never_overwritten(env):
    env[1].record(U[0], ADDED, 1, "t", "t")
    make(env, FakeDriver(goto_exc=[ValueError("x")])).process_url(U[0])
    assert env[1].status_of(U[0]) == ADDED


def test_429_honours_retry_after(env):
    d = FakeDriver([S.IN_LIBRARY], nav=[NavResult(429, retry_after=30.0)])
    rec = make(env, d).process_url(U[0])
    assert rec["status"] == ALREADY_IN_LIBRARY and env[2] == [30.0]


def test_consecutive_restrictions_stop_the_run_and_success_resets_counter(env):
    d = FakeDriver([S.IN_LIBRARY], nav=[NavResult(403), NavResult(403), NavResult(200), NavResult(403), NavResult(403), NavResult(403)])
    imp = make(env, d)
    imp.process_url(U[0]); imp.process_url(U[1])
    imp.process_url(U[2])  # 200 resets the counter
    imp.process_url(U[3]); imp.process_url(U[4])
    assert imp.consecutive_restrictions == 2
    with pytest.raises(RateLimitPause):
        imp.process_url("https://www.webnovel.com/book/z_9")
    assert env[1].status_of("https://www.webnovel.com/book/z_9") == NETWORK_ERROR  # recorded before stopping


class Rep:
    def __init__(self): self.msgs, self.started, self.done = [], [], []
    def item_start(self, i, t, u): self.started.append((i, t, u))
    def item_done(self, u, r): self.done.append(u)
    def message(self, t): self.msgs.append(t)


def test_run_batch_delays_between_but_not_after_last(env):
    cfg, store, sleeps = env
    d = FakeDriver([S.IN_LIBRARY] * 3)
    imp = make(env, d)
    delays = []
    res = run_batch(imp, [(1, U[0]), (2, U[1]), (3, U[2])], 5, Rep(), lambda: True,
                    sleep=delays.append, rng=lambda a, b: (a + b) / 2)
    assert res.processed == 3 and delays == [4.5, 4.5] and res.counts[ALREADY_IN_LIBRARY] == 3


def test_run_batch_login_pause_then_resume(env):
    class D(FakeDriver):
        n = 0
        def settle(self):
            self.n += 1
            return (S.LOGIN_REQUIRED, "login") if self.n == 1 else (S.IN_LIBRARY, "ok")
    asked = []
    res = run_batch(make(env, D()), [(1, U[0])], 1, Rep(), lambda: asked.append(1) or True, sleep=lambda s: None)
    assert res.processed == 1 and asked == [1] and res.stopped is None


def test_run_batch_login_declined_stops_and_marks_login_required(env):
    d = FakeDriver([S.LOGIN_REQUIRED])
    res = run_batch(make(env, d), [(1, U[0]), (2, U[1])], 2, Rep(), lambda: False, sleep=lambda s: None)
    assert res.stopped == "login" and res.processed == 0
    assert env[1].status_of(U[0]) == "login_required" and env[1].get(U[1]) is None


def test_run_batch_stops_if_login_keeps_reappearing(env):
    class D(FakeDriver):
        def settle(self): return (S.LOGIN_REQUIRED, "x")
    rep = Rep()
    res = run_batch(make(env, D()), [(1, U[0])], 1, rep, lambda: True, sleep=lambda s: None)
    assert res.stopped == "login" and any("diagnose" in m for m in rep.msgs)


def test_interrupt_then_resume_skips_completed(tmp_path):
    cfg = Config()
    path = tmp_path / "progress.json"
    store = ProgressStore(path); store.load()

    class D(FakeDriver):
        def settle(self):
            if self.calls.count("settle") == 1:  # 2nd call interrupts
                raise KeyboardInterrupt
            return super().settle()

    d = D([S.IN_LIBRARY, S.IN_LIBRARY])
    imp = Importer(d, store, cfg, sleep=lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        run_batch(imp, [(i + 1, u) for i, u in enumerate(U[:3])], 3, Rep(), lambda: True, sleep=lambda s: None)
    s2 = ProgressStore(path); s2.load()
    assert s2.completed(U[:3]) == [U[0]]
    assert s2.pending(U[:3]) == [U[1], U[2]]


def test_select_items_limit_counts_pending_not_original_lines(tmp_path):
    store = ProgressStore(tmp_path / "p.json"); store.load()
    for u in U[:2]:
        store.record(u, ADDED, 1, "t", "t")
    store.record(U[2], TIMEOUT, 1, "t", "t")
    args = build_parser().parse_args(["run", "--limit", "2"])
    assert _select_items("run", args, U, store) == [(4, U[3]), (5, U[4])]  # skips done AND unresolved
    args = build_parser().parse_args(["retry-failed"])
    assert _select_items("retry-failed", args, U, store) == [(3, U[2])]
    args = build_parser().parse_args(["retry-failed", "--status", "ui_changed"])
    assert _select_items("retry-failed", args, U, store) == []
