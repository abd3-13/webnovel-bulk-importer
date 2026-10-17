"""Configuration loading and validation (config.json)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# UNVERIFIED defaults unless noted. The library/unavailable strings come from
# the user's observations of the live site; role/name matching against the real
# accessibility tree has NOT been verified. Run `diagnose` and adjust here.
DEFAULT_LABELS: dict[str, Any] = {
    # Accessible-name / text patterns (case-insensitive regex).
    "add_to_library": r"\badd\s+to\s+library\b",  # observed: "+ ADD TO LIBRARY"
    "in_library": r"\bin\s+library\b",  # observed: "✓ IN LIBRARY"
    "unavailable_title": r"whoops!",  # observed
    "unavailable_body": r"troubles\s+to\s+find\s+out\s+this\s+page",  # observed
    "login": r"^\W*(log\s?in|sign\s?in|sign\s?up)(\s*/\s*(log\s?in|sign\s?in|sign\s?up))?\W*$",  # guess
    # Regex on accessible names of logged-in-only controls. Unset by default (no verified default).
    "account_marker": None,
    # CSS selector of a logged-in-only element, e.g. an icon-only avatar link with no
    # accessible name. Counted when present in the DOM (not only when visible).
    "account_marker_selector": None,
    # Optional CSS selector that scopes library-button lookup (null = whole page).
    "library_scope_selector": None,
    # Regex that must fully match a host label or path segment to flag a login page.
    "login_url_pattern": r"(log-?in|sign-?in|sign-?up|passport|oauth)",  # guess
}


BLOCKABLE = {"image", "media", "font"}
_SELECTORISH = re.compile(r"^[a-z][a-z0-9]*([.#][\w-]+)+$|^[.#][\w-]+")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    input_file: Path = Path("urls.txt")
    results_file: Path = Path("data/results.csv")
    progress_file: Path = Path("data/progress.json")
    browser_profile_dir: Path = Path("browser-profile")
    log_file: Path = Path("logs/importer.log")
    home_url: str = "https://www.webnovel.com/"
    allowed_domains: list[str] = field(default_factory=lambda: ["webnovel.com"])
    # Extra hosts permitted as authentication redirects (never navigated to by us).
    auth_redirect_hosts: list[str] = field(default_factory=list)
    strict_book_path: bool = True
    navigation_timeout_s: float = 45.0
    action_timeout_s: float = 15.0
    stability_window_s: float = 1.0
    # A login-required state must persist this long before it is believed (header hydration).
    login_confirm_window_s: float = 3.0
    min_delay_s: float = 3.0
    max_delay_s: float = 6.0
    retries: int = 2
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    rate_limit_pause_threshold: int = 3
    verify_with_reload: bool = True
    require_account_marker: bool = False  # set true once a marker is configured
    keep_browser_open: bool = False
    headed: bool = True
    # Resource types blocked while importing (never during login). [] disables blocking.
    block_resource_types: list[str] = field(default_factory=lambda: ["image", "media"])
    labels: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LABELS))

    def regex(self, key: str) -> re.Pattern[str]:
        return re.compile(self.labels[key], re.IGNORECASE)

    def to_json_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = str(v) if isinstance(v, Path) else v
        return out


_PATH_FIELDS = {"input_file", "results_file", "progress_file", "browser_profile_dir", "log_file"}
_NUMBERS = {
    "navigation_timeout_s": (1, 600),
    "action_timeout_s": (1, 300),
    "stability_window_s": (0, 30),
    "login_confirm_window_s": (0, 60),
    "min_delay_s": (0, 3600),
    "max_delay_s": (0, 3600),
    "backoff_base_s": (0, 600),
    "backoff_max_s": (0, 3600),
}
_INTS = {"retries": (0, 10), "rate_limit_pause_threshold": (1, 100)}
_BOOLS = {"strict_book_path", "verify_with_reload", "require_account_marker", "keep_browser_open", "headed"}


def validate(cfg: Config) -> None:
    for name, (lo, hi) in _NUMBERS.items():
        v = getattr(cfg, name)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ConfigError(f"{name} must be a number between {lo} and {hi}, got {v!r}")
    for name, (lo, hi) in _INTS.items():
        v = getattr(cfg, name)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ConfigError(f"{name} must be an integer between {lo} and {hi}, got {v!r}")
    for name in _BOOLS:
        if not isinstance(getattr(cfg, name), bool):
            raise ConfigError(f"{name} must be true or false")
    if cfg.login_confirm_window_s >= cfg.action_timeout_s:
        raise ConfigError("login_confirm_window_s must be smaller than action_timeout_s")
    if not isinstance(cfg.block_resource_types, list) or not set(cfg.block_resource_types) <= BLOCKABLE:
        raise ConfigError(f"block_resource_types must be a list drawn from {sorted(BLOCKABLE)}")
    if cfg.min_delay_s > cfg.max_delay_s:
        raise ConfigError("min_delay_s must be <= max_delay_s")
    if not cfg.allowed_domains or not all(isinstance(d, str) and d for d in cfg.allowed_domains):
        raise ConfigError("allowed_domains must be a non-empty list of domain names")
    if not all(isinstance(d, str) for d in cfg.auth_redirect_hosts):
        raise ConfigError("auth_redirect_hosts must be a list of host names")
    if not cfg.home_url.startswith(("http://", "https://")):
        raise ConfigError("home_url must be an http(s) URL")
    unknown = set(cfg.labels) - set(DEFAULT_LABELS)
    if unknown:
        raise ConfigError(f"unknown labels keys: {sorted(unknown)}")
    for key, value in cfg.labels.items():
        if value is None:
            if key in ("library_scope_selector", "account_marker", "account_marker_selector"):
                continue
            raise ConfigError(f"labels.{key} must not be null")
        if not isinstance(value, str):
            raise ConfigError(f"labels.{key} must be a string")
        if key not in ("library_scope_selector", "account_marker_selector"):
            try:
                re.compile(value)
            except re.error as exc:
                raise ConfigError(f"labels.{key} is not a valid regex: {exc}") from exc
    am = cfg.labels.get("account_marker")
    if am and _SELECTORISH.match(am):
        raise ConfigError(
            f"labels.account_marker ({am!r}) looks like a CSS selector, but it is a regex matched against "
            "accessible names. Move it to labels.account_marker_selector and set account_marker to null."
        )
    if cfg.require_account_marker and not (am or cfg.labels.get("account_marker_selector")):
        raise ConfigError("require_account_marker is true but account_marker and account_marker_selector are both empty")


def build_config(data: dict[str, Any], base_dir: Path | None = None) -> Config:
    known = {f.name for f in fields(Config)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)}")
    cfg = Config()
    for key, value in data.items():
        if key == "labels":
            if not isinstance(value, dict):
                raise ConfigError("labels must be an object")
            merged = dict(DEFAULT_LABELS)
            merged.update(value)
            cfg.labels = merged
        elif key in _PATH_FIELDS:
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{key} must be a non-empty path string")
            setattr(cfg, key, Path(value))
        else:
            setattr(cfg, key, value)
    if base_dir is not None:
        for key in _PATH_FIELDS:
            p = getattr(cfg, key)
            if not p.is_absolute():
                setattr(cfg, key, base_dir / p)
    validate(cfg)
    return cfg


def load_config(path: Path | None, explicit: bool = False) -> Config:
    """Load config.json. A missing default file yields defaults; a missing
    explicitly requested file is an error."""
    if path is None or not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        cfg = Config()
        validate(cfg)
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return build_config(data, base_dir=path.resolve().parent)
