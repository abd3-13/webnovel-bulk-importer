"""Per-URL import workflow, retries, and batch loop.

The browser is accessed only through a Driver, so this logic is testable with
a fake driver. The click flow is deliberately pessimistic: success is only
recorded after the in-library state is observed (and, by default, confirmed
again after a page reload).
"""

from __future__ import annotations

import logging
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import Config
from .models import (
    ADDED,
    ALREADY_IN_LIBRARY,
    FAILED,
    LOGIN_REQUIRED,
    NETWORK_ERROR,
    UI_CHANGED,
    UNAVAILABLE,
    VERIFICATION_FAILED,
    Outcome,
    now_iso,
)
from .page_detection import PageState, UiError, classify_exception
from .persistence import ProgressStore

log = logging.getLogger(__name__)
RESTRICTION_STATUSES = (403, 429)
MAX_RETRY_AFTER_S = 120.0


@dataclass
class NavResult:
    status: int | None = None
    retry_after: float | None = None
    final_url: str = ""


class Driver(Protocol):
    def goto(self, url: str) -> NavResult: ...
    def settle(self) -> tuple[PageState, str]: ...
    def click_add(self, before_click: Callable[[], None] | None = None) -> None: ...
    def wait_after_click(self) -> tuple[PageState, str]: ...
    def reload(self) -> NavResult: ...


class LoginRequired(Exception):
    """Authentication is missing or expired; processing must pause."""


class RateLimitPause(Exception):
    """Repeated rate-limit/access-denied responses; processing must stop."""


def _short(exc: BaseException) -> str:
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first[:200]}"


class Importer:
    def __init__(
        self,
        driver: Driver,
        store: ProgressStore,
        cfg: Config,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], str] = now_iso,
    ):
        self.driver, self.store, self.cfg = driver, store, cfg
        self.sleep, self.clock = sleep, clock
        self.consecutive_restrictions = 0

    # ---- public ---------------------------------------------------------
    def process_url(self, url: str) -> dict[str, Any]:
        prior = self.store.get(url) or {}
        prior_clicked = bool(prior.get("click_attempted"))
        attempts = int(prior.get("attempts", 0))
        started = prior.get("started_at") or self.clock()
        outcome = Outcome(FAILED, "no attempt made")

        for n in range(1, self.cfg.retries + 2):
            attempts += 1
            try:
                outcome = self._attempt(url, prior_clicked, started, attempts)
            except (LoginRequired, KeyboardInterrupt):
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                status, retryable = classify_exception(exc)
                log.debug("attempt %d for %s raised %s", n, url, exc, exc_info=True)
                outcome = Outcome(status, _short(exc), retryable=retryable)
            prior_clicked = prior_clicked or outcome.click_attempted

            self._track_restriction(url, outcome, attempts, started)
            if outcome.retryable and n <= self.cfg.retries:
                delay = min(self.cfg.backoff_max_s, self.cfg.backoff_base_s * 2 ** (n - 1))
                if outcome.retry_after:
                    delay = max(delay, min(outcome.retry_after, MAX_RETRY_AFTER_S))
                log.info("transient %s for %s; retry %d/%d in %.1fs", outcome.status, url, n, self.cfg.retries, delay)
                self.sleep(delay)
                continue
            break

        self._store(url, outcome, attempts, started, prior_clicked)
        return self.store.results[url]

    def record_login_required(self, url: str, details: str) -> None:
        prior = self.store.get(url) or {}
        self.store.record(
            url, LOGIN_REQUIRED, int(prior.get("attempts", 0)) + 1,
            prior.get("started_at") or self.clock(), self.clock(),
            details=details, click_attempted=bool(prior.get("click_attempted")),
        )
        self.store.save()

    # ---- internals ------------------------------------------------------
    def _store(self, url: str, o: Outcome, attempts: int, started: str, clicked: bool) -> None:
        self.store.record(
            url, o.status, attempts, started, self.clock(),
            http_status=o.http_status, details=o.details, click_attempted=clicked or o.click_attempted,
        )
        self.store.save()

    def _track_restriction(self, url: str, o: Outcome, attempts: int, started: str) -> None:
        if o.http_status in RESTRICTION_STATUSES:
            self.consecutive_restrictions += 1
        else:
            self.consecutive_restrictions = 0
        if self.consecutive_restrictions >= self.cfg.rate_limit_pause_threshold:
            self._store(url, o, attempts, started, o.click_attempted)
            raise RateLimitPause(
                f"{self.consecutive_restrictions} consecutive rate-limit/access-denied responses "
                f"(HTTP {o.http_status}); stopping. Wait before resuming."
            )

    def _attempt(self, url: str, prior_clicked: bool, started: str, attempts: int) -> Outcome:
        nav = self.driver.goto(url)
        http = nav.status
        if http == 429:
            return Outcome(NETWORK_ERROR, "HTTP 429 rate limited", http, retryable=True, retry_after=nav.retry_after)
        if http == 403:
            return Outcome(NETWORK_ERROR, "HTTP 403 access denied", http)
        if http is not None and http >= 500:
            return Outcome(NETWORK_ERROR, f"HTTP {http} server error", http, retryable=True)

        state, detail = self.driver.settle()
        log.debug("%s -> %s (%s)", url, state.value, detail)

        if state is PageState.UNAVAILABLE:
            return Outcome(UNAVAILABLE, detail, http)
        if state is PageState.LOGIN_REQUIRED:
            raise LoginRequired(detail)
        if state in (PageState.OFF_SITE, PageState.AMBIGUOUS):
            return Outcome(UI_CHANGED, detail, http)
        if state is PageState.UNKNOWN:
            hint = f"; HTTP {http}" if http and http >= 400 else ""
            return Outcome(UI_CHANGED, f"{detail}{hint} (page may be partially loaded)", http, retryable=True)
        if state is PageState.IN_LIBRARY:
            if prior_clicked:
                return Outcome(
                    ADDED,
                    "in library on re-inspection after an earlier click attempt (not in library before)",
                    http, click_attempted=True,
                )
            return Outcome(ALREADY_IN_LIBRARY, detail, http)
        assert state is PageState.NOT_IN_LIBRARY
        return self._click_and_verify(url, started, attempts, http)

    def _click_and_verify(self, url: str, started: str, attempts: int, http: int | None) -> Outcome:
        def mark() -> None:
            # Pessimistic checkpoint written immediately before the click so a crash
            # leaves "outcome unknown", never a false success or a lost click.
            self.store.record(
                url, VERIFICATION_FAILED, attempts, started, None, http_status=http,
                details="click issued; outcome not yet verified (interrupted?)", click_attempted=True,
            )
            self.store.save()

        try:
            self.driver.click_add(before_click=mark)
        except UiError as exc:
            return Outcome(UI_CHANGED, f"pre-click check failed: {exc}", http)
        except Exception as exc:  # noqa: BLE001
            return Outcome(VERIFICATION_FAILED, f"click raised, outcome unknown ({_short(exc)})", http, click_attempted=True)

        try:
            state, detail = self.driver.wait_after_click()
            if state is PageState.LOGIN_REQUIRED:
                raise LoginRequired("login prompt appeared after clicking")
            in_page = state is PageState.IN_LIBRARY
            if in_page and not self.cfg.verify_with_reload:
                return Outcome(ADDED, "IN LIBRARY control observed after click", http, click_attempted=True)

            nav = self.driver.reload()
            state2, detail2 = self.driver.settle()
            if state2 is PageState.LOGIN_REQUIRED:
                raise LoginRequired("login required when re-checking after click")
            if state2 is PageState.IN_LIBRARY:
                how = "observed in page and confirmed after reload" if in_page else "not seen in page; confirmed after reload"
                return Outcome(ADDED, how, nav.status or http, click_attempted=True)
            why = "in-page success not confirmed by reload" if in_page else "no IN LIBRARY state after click"
            return Outcome(VERIFICATION_FAILED, f"{why}: reload shows {state2.value} ({detail2})", http, click_attempted=True)
        except LoginRequired:
            raise
        except Exception as exc:  # noqa: BLE001 - click already happened
            return Outcome(VERIFICATION_FAILED, f"verification error after click ({_short(exc)})", http, click_attempted=True)


# --------------------------------------------------------------------------
class BatchReporter(Protocol):
    def item_start(self, index: int, total: int, url: str) -> None: ...
    def item_done(self, url: str, record: dict[str, Any]) -> None: ...
    def message(self, text: str) -> None: ...


@dataclass
class BatchResult:
    processed: int = 0
    counts: Counter = field(default_factory=Counter)
    stopped: str | None = None  # None | "login" | "rate_limit"


def run_batch(
    importer: Importer,
    items: list[tuple[int, str]],
    total: int,
    reporter: BatchReporter,
    on_login_required: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[float, float], float] = random.uniform,
    max_login_pauses_per_url: int = 2,
) -> BatchResult:
    """Process (index, url) items sequentially with a randomized delay between books."""
    cfg = importer.cfg
    result = BatchResult()
    for pos, (index, url) in enumerate(items):
        reporter.item_start(index, total, url)
        pauses = 0
        while True:
            try:
                rec = importer.process_url(url)
                break
            except LoginRequired as exc:
                pauses += 1
                reporter.message(f"Authentication required: {exc}. Processing paused.")
                if pauses > max_login_pauses_per_url or not on_login_required():
                    reason = f"login required: {exc}"
                    if pauses > max_login_pauses_per_url:
                        reporter.message(
                            "Login prompt keeps appearing although the home page looked authenticated. "
                            "Run `diagnose <url>` and review labels.login in config.json."
                        )
                    importer.record_login_required(url, reason)
                    result.stopped = "login"
                    return result
            except RateLimitPause as exc:
                reporter.message(str(exc))
                result.stopped = "rate_limit"
                return result
        result.processed += 1
        result.counts[rec["status"]] += 1
        reporter.item_done(url, rec)
        if pos < len(items) - 1:
            sleep(rng(cfg.min_delay_s, cfg.max_delay_s))
    return result
