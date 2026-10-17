"""Page-state detection.

Pure logic (Snapshot, classify, is_authenticated, classify_exception) is
separated from the Playwright-facing code (take_snapshot, find_unique) so the
decision rules can be unit-tested without a browser.

Locator strategy: role-based (button/link) accessible-name match first, then an
anchored visible-text fallback. A library control only counts when exactly ONE
visible match exists; anything else is reported as ambiguous/unknown rather
than guessed. Patterns live in config.json (labels) and are UNVERIFIED against
the live DOM except for the strings the user observed. Use `diagnose`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .models import FAILED, NETWORK_ERROR, TIMEOUT


class UiError(Exception):
    """The expected UI could not be identified confidently."""


class PageState(str, Enum):
    UNAVAILABLE = "unavailable"
    LOGIN_REQUIRED = "login_required"
    NOT_IN_LIBRARY = "not_in_library"
    IN_LIBRARY = "in_library"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"
    OFF_SITE = "off_site"


@dataclass
class Snapshot:
    url: str = ""
    host: str = ""
    on_allowed_host: bool = True
    login_url: bool = False
    unavailable_title: int = 0
    unavailable_body: int = 0
    add_count: int = 0
    in_count: int = 0
    login_controls: int = 0
    account_markers: int = 0
    marker_configured: bool = False
    add_source: str = "role"

    def summary(self) -> str:
        return (
            f"add={self.add_count}({self.add_source}) in={self.in_count} login_ctl={self.login_controls} "
            f"marker={self.account_markers}/{'cfg' if self.marker_configured else 'unset'} "
            f"whoops={self.unavailable_title}/{self.unavailable_body} host={self.host or '-'}"
        )


def classify(s: Snapshot) -> tuple[PageState, str]:
    """Map a snapshot to one page state. Order matters; see inline notes."""
    if not s.on_allowed_host:
        return PageState.OFF_SITE, f"unexpected redirect to host {s.host or '(none)'}"
    if s.login_url:
        return PageState.LOGIN_REQUIRED, "on a login/auth page"
    lib = s.add_count + s.in_count
    if (s.unavailable_title or s.unavailable_body) and lib == 0:
        seen = "title+body" if (s.unavailable_title and s.unavailable_body) else (
            "title only" if s.unavailable_title else "body only")
        return PageState.UNAVAILABLE, f"unavailable-page message found ({seen})"
    # An IN LIBRARY control implies a logged-in account, so it outranks login-control checks.
    if s.in_count == 1 and s.add_count == 0:
        return PageState.IN_LIBRARY, "single IN LIBRARY control"
    # A positive logged-in marker overrides login-looking controls: logged-in pages can
    # legitimately contain a "Sign in" button (daily check-in promo and the like).
    logged_in = s.account_markers > 0
    if s.login_controls > 0 and not logged_in:
        return PageState.LOGIN_REQUIRED, "login/sign-in control visible and no logged-in marker"
    if s.add_count == 1 and s.in_count == 0:
        if s.marker_configured and not logged_in:
            return PageState.LOGIN_REQUIRED, "ADD control visible but logged-in marker absent"
        return PageState.NOT_IN_LIBRARY, f"single ADD TO LIBRARY control ({s.add_source})"
    if lib > 1:
        return PageState.AMBIGUOUS, f"multiple library controls (add={s.add_count}, in={s.in_count})"
    return PageState.UNKNOWN, "no library control and no unavailable message"


def is_authenticated(s: Snapshot, require_marker: bool) -> tuple[bool, dict[str, bool]]:
    """Multiple-indicator auth check, run against the home page snapshot."""
    indicators = {
        "on_allowed_host": s.on_allowed_host,
        "not_login_url": not s.login_url,
        "no_login_control_visible": s.login_controls == 0,
        "account_marker_present": s.account_markers > 0,
    }
    base = indicators["on_allowed_host"] and indicators["not_login_url"]
    if indicators["account_marker_present"]:
        ok = base  # positive evidence wins over stray "Sign in"-looking controls
    else:
        ok = base and indicators["no_login_control_visible"] and not require_marker
    return ok, indicators


def classify_exception(exc: BaseException) -> tuple[str, bool]:
    """Return (status, retryable) for an exception raised during an attempt."""
    name = type(exc).__name__
    msg = str(exc)
    if name == "TimeoutError" or "Timeout" in name:
        return TIMEOUT, True
    if "net::ERR_" in msg or "NS_ERROR_" in msg:
        return NETWORK_ERROR, True
    if isinstance(exc, UiError):
        return "ui_changed", False
    return FAILED, False


_LOCALE_SEG = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.IGNORECASE)


def is_login_url(url: str, pattern: re.Pattern[str], auth_hosts: list[str]) -> bool:
    """True if the URL is a login/auth page.

    The pattern must match a *whole* host label or path segment, never a substring:
    a novel slug like "signing-in-for-eight-years" must not look like /sign-in.
    Book pages are never login pages.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in [h.lower() for h in auth_hosts]:
        return True
    segments = [s for s in parts.path.split("/") if s]
    if segments and _LOCALE_SEG.match(segments[0]) and len(segments) > 1:
        segments = segments[1:]
    if segments and segments[0].lower() == "book":
        return False
    return any(pattern.fullmatch(tok) for tok in host.split(".") + segments)


def host_allowed(host: str, cfg: Config) -> bool:
    hosts = [d.lower() for d in cfg.allowed_domains] + [h.lower() for h in cfg.auth_redirect_hosts]
    return any(host == d or host.endswith("." + d) for d in hosts)


# --------------------------------------------------------------------------
# Playwright-facing code
# --------------------------------------------------------------------------
def _visible(loc: Any, cap: int = 10) -> list[Any]:
    try:
        n = min(loc.count(), cap)
    except Exception:  # page navigating; treat as nothing found
        return []
    out = []
    for i in range(n):
        item = loc.nth(i)
        try:
            if item.is_visible():
                out.append(item)
        except Exception:
            continue
    return out


def _root(page: Any, cfg: Config) -> Any:
    scope = cfg.labels.get("library_scope_selector")
    return page.locator(scope) if scope else page


def find_controls(root: Any, pattern: re.Pattern[str]) -> tuple[list[Any], str]:
    role_loc = root.get_by_role("button", name=pattern).or_(root.get_by_role("link", name=pattern))
    found = _visible(role_loc)
    if found:
        return found, "role"
    anchored = re.compile(rf"^\W*(?:{pattern.pattern})\W*$", pattern.flags)
    return _visible(root.get_by_text(anchored)), "text"


def take_snapshot(page: Any, cfg: Config) -> Snapshot:
    url = page.url or ""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    snap = Snapshot(url=url, host=host)
    snap.on_allowed_host = host_allowed(host, cfg) if host else True  # about:blank
    snap.login_url = is_login_url(url, cfg.regex("login_url_pattern"), cfg.auth_redirect_hosts)
    if not snap.on_allowed_host:
        return snap  # never inspect or act on pages outside the allowed set

    root = _root(page, cfg)
    add, src = find_controls(root, cfg.regex("add_to_library"))
    inn, _ = find_controls(root, cfg.regex("in_library"))
    snap.add_count, snap.in_count, snap.add_source = len(add), len(inn), src
    snap.unavailable_title = len(_visible(page.get_by_text(cfg.regex("unavailable_title"))))
    snap.unavailable_body = len(_visible(page.get_by_text(cfg.regex("unavailable_body"))))
    snap.login_controls = len(find_controls(page, cfg.regex("login"))[0])
    regex, selector = cfg.labels.get("account_marker"), cfg.labels.get("account_marker_selector")
    snap.marker_configured = bool(regex or selector)
    if regex:
        marker = cfg.regex("account_marker")
        snap.account_markers += len(
            _visible(page.get_by_role("button", name=marker).or_(page.get_by_role("link", name=marker)))
        )
    if selector:
        # Attached, not visible: an icon-only avatar collapses to 0x0 when images are blocked.
        snap.account_markers += page.locator(selector).count()
    return snap


def find_unique(page: Any, cfg: Config, key: str) -> Any:
    """Return the single visible library control for `key` or raise UiError."""
    found, src = find_controls(_root(page, cfg), cfg.regex(key))
    if len(found) != 1:
        raise UiError(f"expected exactly 1 visible '{key}' control, found {len(found)} (via {src})")
    return found[0]


_DESCRIBE_JS = r"""
() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const chain = e => { const out = []; let n = e.parentElement;
    for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
      const c = (typeof n.className === 'string' && n.className.trim())
        ? '.' + n.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
      out.push(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') + c); }
    return out.join(' < '); };
  return Array.from(document.querySelectorAll('button,a,[role=button],[role=link]'))
    .filter(vis).slice(0, 80).map(e => ({
      tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || '',
      text: (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      aria: e.getAttribute('aria-label') || '',
      cls: (typeof e.className === 'string' ? e.className : '').trim().slice(0, 60),
      parents: chain(e) }));
}
"""


def describe_controls(page: Any, cfg: Config) -> list[dict[str, Any]]:
    """Visible buttons/links with the info needed to calibrate locators."""
    items = page.evaluate(_DESCRIBE_JS)
    add_re, in_re, login_re = cfg.regex("add_to_library"), cfg.regex("in_library"), cfg.regex("login")
    for it in items:
        label = f"{it['text']} {it['aria']}"
        tags = [n for n, rx in (("ADD", add_re), ("IN", in_re), ("LOGIN", login_re)) if rx.search(label)]
        it["matches"] = ",".join(tags)
    return items
