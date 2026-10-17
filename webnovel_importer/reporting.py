"""results.csv generation and terminal summaries."""

from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path

from .input_loader import LoadResult
from .models import (
    ADDED,
    ALREADY_IN_LIBRARY,
    DUPLICATE_URL,
    INVALID_URL,
    PENDING,
    UNAVAILABLE,
    UNRESOLVED,
)
from .persistence import ProgressStore, atomic_write_text

COLUMNS = ["url", "status", "attempts", "started_at", "completed_at", "http_status", "details", "input_line"]
_MAX_DETAILS = 500


def _clean(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_DETAILS else text[: _MAX_DETAILS - 1] + "…"


def build_rows(load: LoadResult, store: ProgressStore) -> list[dict[str, object]]:
    """One row per input entry (valid, invalid, duplicate), then orphans."""
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    blank = {"attempts": 0, "started_at": "", "completed_at": "", "http_status": ""}
    for e in load.entries:
        if e.status == INVALID_URL:
            rows.append({**blank, "url": e.raw, "status": INVALID_URL, "details": _clean(e.reason), "input_line": e.line_no})
        elif e.status == DUPLICATE_URL:
            rows.append({**blank, "url": e.url, "status": DUPLICATE_URL, "details": _clean(e.reason), "input_line": e.line_no})
        else:
            assert e.url is not None
            seen.add(e.url)
            rec = store.get(e.url)
            if rec is None:
                rows.append({**blank, "url": e.url, "status": PENDING, "details": "", "input_line": e.line_no})
            else:
                rows.append(_row_from_record(e.url, rec, e.line_no))
    for url, rec in store.results.items():
        if url not in seen:
            row = _row_from_record(url, rec, "")
            row["details"] = _clean("not in current input file; " + str(rec.get("details", "")))
            rows.append(row)
    return rows


def _row_from_record(url: str, rec: dict, line: object) -> dict[str, object]:
    http = rec.get("http_status")
    return {
        "url": url,
        "status": rec["status"],
        "attempts": rec.get("attempts", 0),
        "started_at": rec.get("started_at") or "",
        "completed_at": rec.get("completed_at") or "",
        "http_status": "" if http is None else http,
        "details": _clean(rec.get("details", "")),
        "input_line": line,
    }


def rows_to_csv(rows: list[dict[str, object]]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_results_csv(path: Path, load: LoadResult, store: ProgressStore) -> list[dict[str, object]]:
    rows = build_rows(load, store)
    atomic_write_text(path, rows_to_csv(rows))
    return rows


def count_statuses(rows: list[dict[str, object]]) -> Counter:
    return Counter(str(r["status"]) for r in rows)


def format_counts(counts: Counter) -> str:
    unresolved = sum(counts[s] for s in UNRESOLVED)
    lines = [
        f"  Added:              {counts[ADDED]}",
        f"  Already in library: {counts[ALREADY_IN_LIBRARY]}",
        f"  Unavailable:        {counts[UNAVAILABLE]}",
        f"  Unresolved:         {unresolved}",
    ]
    for s in sorted(UNRESOLVED):
        if counts[s]:
            lines.append(f"    {s}: {counts[s]}")
    lines += [
        f"  Pending:            {counts[PENDING]}",
        f"  Invalid URLs:       {counts[INVALID_URL]}",
        f"  Duplicate URLs:     {counts[DUPLICATE_URL]}",
    ]
    return "\n".join(lines)
