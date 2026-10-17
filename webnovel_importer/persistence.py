"""Checkpoint storage (progress.json) with atomic writes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import FINAL, SUCCESS, UNRESOLVED, now_iso

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def atomic_write_text(path: Path, text: str) -> None:
    """Write via temp file in the same directory, fsync, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:  # best-effort directory fsync so the rename itself is durable
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class ProgressStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.meta: dict[str, Any] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.load_warning: str | None = None
        self.existed = False

    # ---- loading / saving -------------------------------------------------
    def load(self, quarantine: bool = True) -> None:
        """Load the checkpoint. An unreadable file yields empty progress and a
        warning; with quarantine=True it is also moved aside (read-only
        commands pass False so they never touch the disk)."""
        self.meta, self.results, self.load_warning = {}, {}, None
        self.existed = self.path.exists()
        if not self.existed:
            self.meta = {"created_at": now_iso()}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
                raise ValueError("unexpected structure")
            if data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError(f"unsupported schema_version {data.get('schema_version')!r}")
            results = data["results"]
            if not all(isinstance(v, dict) and isinstance(v.get("status"), str) for v in results.values()):
                raise ValueError("malformed result entries")
        except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
            backup = self.path.with_name(f"{self.path.name}.corrupt-{now_iso().replace(':', '')}")
            where = "will be moved aside on the next run"
            if quarantine:
                try:
                    os.replace(self.path, backup)
                    where = f"moved to {backup.name}"
                except OSError:
                    where = "could not be moved aside"
            self.load_warning = f"checkpoint unreadable ({exc}); {where}; starting with empty progress"
            log.warning(self.load_warning)
            self.meta = {"created_at": now_iso()}
            return
        self.meta = {k: v for k, v in data.items() if k not in ("results", "schema_version")}
        self.results = results

    def save(self) -> None:
        self.meta["updated_at"] = now_iso()
        payload = {"schema_version": SCHEMA_VERSION, **self.meta, "results": self.results}
        atomic_write_text(self.path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    # ---- queries ----------------------------------------------------------
    def get(self, url: str) -> dict[str, Any] | None:
        return self.results.get(url)

    def status_of(self, url: str) -> str | None:
        rec = self.results.get(url)
        return rec["status"] if rec else None

    def pending(self, urls: list[str]) -> list[str]:
        """URLs with no result at all, in input order."""
        return [u for u in urls if u not in self.results]

    def unresolved(self, urls: list[str], only: set[str] | None = None) -> list[str]:
        wanted = UNRESOLVED if not only else (UNRESOLVED & only)
        return [u for u in urls if self.status_of(u) in wanted]

    def completed(self, urls: list[str]) -> list[str]:
        return [u for u in urls if self.status_of(u) in FINAL]

    # ---- input tracking ---------------------------------------------------
    def input_changed(self, new_fingerprint: str) -> bool | None:
        """True/False if a previous fingerprint exists, None if there is none."""
        old = self.meta.get("input_fingerprint")
        return None if old is None else old != new_fingerprint

    def set_input(self, path: Path, fp: str, count: int) -> None:
        self.meta.update(input_file=str(path), input_fingerprint=fp, input_url_count=count)

    # ---- mutation ---------------------------------------------------------
    def record(
        self,
        url: str,
        status: str,
        attempts: int,
        started_at: str,
        completed_at: str | None,
        http_status: int | None = None,
        details: str = "",
        click_attempted: bool = False,
    ) -> bool:
        """Store a result in memory. Returns False (and keeps the old result)
        if it would overwrite a verified success."""
        existing = self.results.get(url)
        if existing and existing["status"] in SUCCESS:
            if status != existing["status"]:
                log.warning("not overwriting %s result for %s with %s", existing["status"], url, status)
            return False
        self.results[url] = {
            "status": status,
            "attempts": attempts,
            "started_at": started_at,
            "completed_at": completed_at,
            "http_status": http_status,
            "details": details,
            "click_attempted": click_attempted,
        }
        return True

    def reset(self) -> None:
        self.results = {}
        self.meta = {"created_at": now_iso()}
