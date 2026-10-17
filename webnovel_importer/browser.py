"""Playwright browser session, driver, and authentication checks.

Security notes: the persistent profile in `browser-profile/` holds the logged-in
session. This module never reads, prints or exports cookies/tokens, never asks
for credentials, and Playwright talks to Chromium over a pipe (no network
debugging port is opened).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .importer import NavResult
from .page_detection import (
    PageState,
    Snapshot,
    UiError,
    classify,
    find_unique,
    is_authenticated,
    take_snapshot,
)

log = logging.getLogger(__name__)
POLL_MS = 250

LOGIN_HELP = """\
Log in manually in the Chromium window (use the site's own login page).
Never type your password into this terminal. The browser-profile/ directory
holds your logged-in session: keep it local and never share or commit it."""


class BrowserError(Exception):
    pass


class BrowserSession:
    """Context manager around a persistent Chromium context."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw: Any = None
        self.context: Any = None
        self.page: Any = None
        self._cdp: Any = None

    def set_resource_blocking(self, enabled: bool) -> None:
        """Block cfg.block_resource_types (images/media by default) on the working page.

        Uses CDP Fetch filtered by resource type rather than context.route(): route
        interception disables Chromium's HTTP cache for *every* request (measured: JS
        re-downloaded on each navigation), whereas Fetch patterns only pause the
        matching requests. Chromium-only; the CDP session runs over Playwright's pipe.
        Enable only after login is verified: blocked images can break login challenges.
        """
        if self.page is None or enabled == (self._cdp is not None):
            return
        if enabled:
            types = [t.capitalize() for t in self.cfg.block_resource_types]
            if not types:
                return
            cdp = self.context.new_cdp_session(self.page)

            def on_paused(event: dict) -> None:
                try:  # never leave a paused request unanswered
                    cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
                except Exception:  # noqa: BLE001 - page/session already gone
                    pass

            cdp.on("Fetch.requestPaused", on_paused)
            cdp.send("Fetch.enable", {"patterns": [{"resourceType": t} for t in types]})
            self._cdp = cdp
        else:
            cdp, self._cdp = self._cdp, None
            for step in (lambda: cdp.send("Fetch.disable"), cdp.detach):
                try:
                    step()
                except Exception:  # noqa: BLE001
                    pass

    def __enter__(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("Playwright is not installed: pip install -r requirements.txt") from exc
        profile = self.cfg.browser_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(profile, 0o700)
        except OSError:
            pass
        self._pw = sync_playwright().start()
        try:
            kwargs: dict[str, Any] = {"headless": not self.cfg.headed, "accept_downloads": False}
            if self.cfg.headed:
                kwargs["no_viewport"] = True
            self.context = self._pw.chromium.launch_persistent_context(str(profile), **kwargs)
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            msg = str(exc)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise BrowserError("Chromium is not installed: run `python -m playwright install chromium`") from exc
            raise BrowserError(
                "Could not launch Chromium (is another browser using browser-profile/ already?): "
                + (msg.strip().splitlines() or [""])[0][:200]
            ) from exc
        self.context.set_default_navigation_timeout(self.cfg.navigation_timeout_s * 1000)
        self.context.set_default_timeout(self.cfg.action_timeout_s * 1000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._cdp = None
        for closer in (lambda: self.context and self.context.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001 - already shutting down
                pass
        self.context = self._pw = self.page = None


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None  # HTTP-date form is ignored; backoff still applies


class PlaywrightDriver:
    def __init__(self, page: Any, cfg: Config):
        self.page, self.cfg = page, cfg

    def _nav(self, resp: Any) -> NavResult:
        if resp is None:
            return NavResult(None, None, self.page.url)
        return NavResult(resp.status, _parse_retry_after(resp.headers.get("retry-after")), self.page.url)

    def goto(self, url: str) -> NavResult:
        return self._nav(self.page.goto(url, wait_until="domcontentloaded"))

    def reload(self) -> NavResult:
        return self._nav(self.page.reload(wait_until="domcontentloaded"))

    def _snap(self) -> tuple[Snapshot, PageState, str]:
        snap = take_snapshot(self.page, self.cfg)
        state, detail = classify(snap)
        log.debug("snapshot: %s -> %s", snap.summary(), state.value)
        return snap, state, detail

    def settle(self) -> tuple[PageState, str]:
        """Wait for a decisive, *stable* page state.

        Stability matters: SPAs may render ADD TO LIBRARY before the user's
        library state hydrates, so a state is only trusted after it has stayed
        unchanged for `stability_window_s`.
        """
        try:
            self.page.wait_for_load_state("load", timeout=min(self.cfg.action_timeout_s, 10) * 1000)
        except Exception:  # noqa: BLE001 - polling below decides
            pass
        deadline = time.monotonic() + self.cfg.action_timeout_s
        last: PageState | None = None
        since = 0.0
        while True:
            snap, state, detail = self._snap()
            now = time.monotonic()
            if state is PageState.OFF_SITE or (state is PageState.LOGIN_REQUIRED and snap.login_url):
                return state, detail
            if state is PageState.UNKNOWN:
                last = None
            else:
                if state is not last:
                    last, since = state, now
                window = self.cfg.login_confirm_window_s if state is PageState.LOGIN_REQUIRED else self.cfg.stability_window_s
                if now - since >= window:
                    return state, detail
            if now >= deadline:
                if state is PageState.UNKNOWN:
                    return state, detail
                return PageState.AMBIGUOUS, f"page state did not stabilise (last: {state.value}; {detail})"
            self.page.wait_for_timeout(POLL_MS)

    def click_add(self, before_click: Callable[[], None] | None = None) -> None:
        _, state, detail = self._snap()
        if state is not PageState.NOT_IN_LIBRARY:
            raise UiError(f"state changed before click: {state.value} ({detail})")
        control = find_unique(self.page, self.cfg, "add_to_library")
        if before_click:
            before_click()
        log.debug("clicking add-to-library control")
        control.click(timeout=self.cfg.action_timeout_s * 1000)

    def wait_after_click(self) -> tuple[PageState, str]:
        deadline = time.monotonic() + self.cfg.action_timeout_s
        while True:
            _, state, detail = self._snap()
            if state in (PageState.IN_LIBRARY, PageState.LOGIN_REQUIRED, PageState.OFF_SITE):
                return state, detail
            if time.monotonic() >= deadline:
                return state, detail
            self.page.wait_for_timeout(POLL_MS)


@dataclass
class AuthResult:
    ok: bool
    indicators: dict[str, bool]
    error: str | None = None  # set when the home page could not be loaded at all

    def describe(self) -> str:
        return ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in self.indicators.items())


def check_auth(page: Any, cfg: Config) -> AuthResult:
    """Load the home page and evaluate several indicators (no cookie access)."""
    try:
        page.goto(cfg.home_url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001 - reported to the user, not a crash
        log.warning("auth check could not load %s: %s", cfg.home_url, exc)
        first = (str(exc).strip().splitlines() or [""])[0][:200]
        return AuthResult(False, {"page_loaded": False}, error=f"{type(exc).__name__}: {first}")
    try:
        page.wait_for_load_state("load", timeout=10_000)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.monotonic() + max(cfg.action_timeout_s, 10)
    ok_since: float | None = None
    while True:
        snap = take_snapshot(page, cfg)
        ok, indicators = is_authenticated(snap, cfg.require_account_marker)
        now = time.monotonic()
        if ok:
            ok_since = ok_since or now
            if now - ok_since >= cfg.stability_window_s:
                return AuthResult(True, indicators)
        else:
            ok_since = None
        if now >= deadline:
            return AuthResult(False, indicators)
        page.wait_for_timeout(POLL_MS)


def ensure_authenticated(
    page: Any,
    cfg: Config,
    prompt: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> bool:
    """Verify login; if missing, let the user log in manually and re-verify."""
    result = check_auth(page, cfg)
    if result.ok:
        return True
    if result.error:
        out(f"\nCould not load {cfg.home_url}: {result.error}\nCheck your network connection and try again.")
        return False
    out("\nNot logged in (or login could not be verified).")
    out(LOGIN_HELP)
    while True:
        try:
            answer = prompt("\nPress Enter after logging in (or type 'q' to quit): ")
        except (EOFError, KeyboardInterrupt):
            return False
        if answer.strip().lower() in ("q", "quit"):
            return False
        result = check_auth(page, cfg)
        if result.ok:
            return True
        if result.error:
            out(f"Could not load the home page: {result.error}")
            continue
        out(f"Login not verified ({result.describe()}).")
        if not result.indicators.get("account_marker_present") and cfg.require_account_marker:
            out(
                "If you are logged in, the account-marker pattern may not match this site's header: "
                "run `python bulk_import.py diagnose` and adjust labels.account_marker in config.json."
            )
