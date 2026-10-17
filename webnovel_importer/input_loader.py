"""Read and validate urls.txt."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .models import DUPLICATE_URL, INVALID_URL

_LOCALE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.IGNORECASE)


class InputError(Exception):
    """The input file itself cannot be used (missing, unreadable, bad encoding)."""


class UrlError(ValueError):
    """A single URL failed validation."""


@dataclass
class InputEntry:
    line_no: int
    raw: str
    url: str | None  # canonical URL for valid and duplicate entries
    status: str | None  # None = valid unique; INVALID_URL; DUPLICATE_URL
    reason: str = ""


@dataclass
class LoadResult:
    path: Path
    entries: list[InputEntry] = field(default_factory=list)
    total_lines: int = 0
    skipped_lines: int = 0  # blank or comment

    @property
    def valid_urls(self) -> list[str]:
        return [e.url for e in self.entries if e.status is None and e.url]

    @property
    def invalid(self) -> list[InputEntry]:
        return [e for e in self.entries if e.status == INVALID_URL]

    @property
    def duplicates(self) -> list[InputEntry]:
        return [e for e in self.entries if e.status == DUPLICATE_URL]


def _host_allowed(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def normalize_url(raw: str, allowed_domains: list[str], strict_book_path: bool = True) -> str:
    """Return the canonical URL or raise UrlError.

    Canonical form: https, lowercase host (bare domain -> www.), no query,
    no fragment, no trailing slash. Book identity lives in the path.
    """
    if any(c.isspace() for c in raw):
        raise UrlError("contains whitespace")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise UrlError(f"malformed URL ({exc})") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlError(f"unsupported scheme {parts.scheme!r}" if parts.scheme else "missing http(s) scheme")
    if parts.username is not None or parts.password is not None:
        raise UrlError("credentials in URL are not allowed")
    if port not in (None, 80, 443):
        raise UrlError(f"unexpected port {port}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlError("missing host")
    doms = [d.lower() for d in allowed_domains]
    if not _host_allowed(host, doms):
        raise UrlError(f"host {host!r} is not an allowed WebNovel domain")
    if host in doms:
        host = "www." + host

    segments = [s for s in parts.path.split("/") if s]
    if strict_book_path:
        rest = segments
        if len(rest) > 1 and _LOCALE.match(rest[0]) and rest[1].lower() == "book":
            rest = rest[1:]
        if not rest or rest[0].lower() != "book":
            raise UrlError("path does not start with /book/ (not a book page)")
        if len(rest) == 1:
            raise UrlError("missing book identifier after /book/")
        if len(rest) > 2:
            raise UrlError("path goes below the book page (chapter URL?); use the novel's own page")
    path = "/" + "/".join(segments)
    return f"https://{host}{path}"


def load_urls(path: Path, allowed_domains: list[str], strict_book_path: bool = True) -> LoadResult:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise InputError(f"input file not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc

    result = LoadResult(path=path)
    first_seen: dict[str, int] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        result.total_lines += 1
        raw = line.strip()
        if not raw or raw.startswith("#"):
            result.skipped_lines += 1
            continue
        try:
            url = normalize_url(raw, allowed_domains, strict_book_path)
        except UrlError as exc:
            result.entries.append(InputEntry(line_no, raw, None, INVALID_URL, str(exc)))
            continue
        if url in first_seen:
            result.entries.append(
                InputEntry(line_no, raw, url, DUPLICATE_URL, f"duplicate of line {first_seen[url]}")
            )
            continue
        first_seen[url] = line_no
        result.entries.append(InputEntry(line_no, raw, url, None))
    return result


def fingerprint(urls: list[str]) -> str:
    """Order-sensitive SHA-256 of the valid unique URL list."""
    h = hashlib.sha256()
    for u in urls:
        h.update(u.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def display_name(url: str) -> str:
    """Human-readable title derived from the URL slug (no DOM access)."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    last = segments[-1] if segments else url
    slug = re.sub(r"_\d+$", "", last)
    name = slug.replace("-", " ").strip()
    return name.title() if name else last
