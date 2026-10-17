"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .browser import BrowserError, BrowserSession, PlaywrightDriver, ensure_authenticated
from .config import Config, ConfigError, load_config
from .importer import Importer, run_batch
from .input_loader import InputError, LoadResult, UrlError, display_name, fingerprint, load_urls, normalize_url
from .models import ADDED, ALL_STATUSES, ALREADY_IN_LIBRARY, SUCCESS, UNAVAILABLE, UNRESOLVED, now_iso
from .persistence import ProgressStore
from .reporting import build_rows, count_statuses, format_counts, write_results_csv

TITLE = "WebNovel Bulk Library Importer"
EXIT_OK, EXIT_ERROR, EXIT_STOPPED, EXIT_RATE_LIMIT, EXIT_INTERRUPT = 0, 1, 3, 4, 130


# ---- setup ---------------------------------------------------------------
def setup_logging(cfg: Config, verbose: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG if verbose else logging.ERROR)
    ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(ch)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bulk_import.py", description=TITLE)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, default=None, help="config file (default: ./config.json)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    def add_input(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--input", type=Path, help="URL list (overrides config)")

    def add_run_opts(sp: argparse.ArgumentParser) -> None:
        add_input(sp)
        sp.add_argument("--limit", type=int, help="process at most N pending URLs")
        sp.add_argument("--min-delay", type=float, help="min seconds between books")
        sp.add_argument("--max-delay", type=float, help="max seconds between books")
        sp.add_argument("--retries", type=int, help="retries for transient errors")
        sp.add_argument("--keep-open", action="store_true", help="keep the browser open at the end")
        sp.add_argument("--allow-images", action="store_true", help="do not block images/media while importing")
        sp.add_argument("--headless", action="store_true", help="run without a visible window (login needs headed)")

    r = sub.add_parser("run", help="import pending URLs (modifies your WebNovel library)")
    add_run_opts(r)
    rf = sub.add_parser("retry-failed", help="retry URLs with unresolved results")
    add_run_opts(rf)
    rf.add_argument("--status", action="append", choices=sorted(UNRESOLVED), help="only retry this status (repeatable)")
    add_input(sub.add_parser("validate", help="check the URL list; no browser, no changes"))
    add_input(sub.add_parser("status", help="show progress; no browser"))
    rp = sub.add_parser("report", help="regenerate results.csv and show a summary; no browser")
    add_input(rp)
    rp.add_argument("--status", action="append", choices=sorted(ALL_STATUSES), help="list URLs with this status")
    rs = sub.add_parser("reset", help="discard saved progress (a backup is kept)")
    rs.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    add_input(rs)
    sub.add_parser("login", help="open the browser to log in and verify the session")
    d = sub.add_parser("diagnose", help="inspect a page's buttons/states without clicking anything")
    d.add_argument("url", nargs="?", help="a book URL (default: the home page)")
    return p


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    changes: dict[str, Any] = {}
    if getattr(args, "input", None):
        changes["input_file"] = args.input
    for opt, field_name in (("min_delay", "min_delay_s"), ("max_delay", "max_delay_s"), ("retries", "retries")):
        if getattr(args, opt, None) is not None:
            changes[field_name] = getattr(args, opt)
    if getattr(args, "keep_open", False):
        changes["keep_browser_open"] = True
    if getattr(args, "allow_images", False):
        changes["block_resource_types"] = []
    if getattr(args, "headless", False):
        changes["headed"] = False
    if not changes:
        return cfg
    new = replace(cfg, **changes)
    from .config import validate  # local import avoids a cycle in type checkers
    validate(new)
    return new


# ---- shared helpers ------------------------------------------------------
def load_input(cfg: Config) -> LoadResult:
    return load_urls(cfg.input_file, cfg.allowed_domains, cfg.strict_book_path)


def print_input_summary(cfg: Config, load: LoadResult, store: ProgressStore | None = None) -> None:
    valid = load.valid_urls
    print(f"\n{TITLE}\n")
    print(f"Input: {cfg.input_file}")
    print(f"Total input lines: {load.total_lines} (blank/comment: {load.skipped_lines})")
    print(f"Valid unique URLs: {len(valid)}")
    print(f"Invalid URLs: {len(load.invalid)}")
    print(f"Duplicate URLs: {len(load.duplicates)}")
    if store is not None:
        print(f"Completed (added/already/unavailable): {len(store.completed(valid))}")
        print(f"Unresolved (retry with `retry-failed`): {len(store.unresolved(valid))}")
        print(f"Pending: {len(store.pending(valid))}")
        changed = store.input_changed(fingerprint(valid))
        if changed:
            print("Note: the input URL list changed since the last checkpoint; existing results are kept by URL.")


def print_problem_lines(load: LoadResult, limit: int = 20) -> None:
    for title, entries in (("Invalid", load.invalid), ("Duplicate", load.duplicates)):
        for e in entries[:limit]:
            print(f"  {title} line {e.line_no}: {e.raw[:100]} -> {e.reason}")
        if len(entries) > limit:
            print(f"  ... and {len(entries) - limit} more {title.lower()} entries (see results.csv)")


class TerminalReporter:
    def __init__(self, store: ProgressStore, valid: list[str], verbose: bool):
        self.store, self.valid, self.verbose = store, valid, verbose

    def item_start(self, index: int, total: int, url: str) -> None:
        print(f"[{index:03d}/{total}] {display_name(url)}", flush=True)

    def item_done(self, url: str, record: dict[str, Any]) -> None:
        print(f"         Status: {record['status']}")
        if record["status"] not in SUCCESS or self.verbose:
            if record.get("details"):
                print(f"         Details: {record['details']}")
        c = Counter(self.store.status_of(u) for u in self.valid)
        done = sum(1 for u in self.valid if u in self.store.results)
        unresolved = sum(c[s] for s in UNRESOLVED)
        print(
            f"         Progress: {done}/{len(self.valid)} | Added: {c[ADDED]} | Already: {c[ALREADY_IN_LIBRARY]} "
            f"| Unavailable: {c[UNAVAILABLE]} | Unresolved: {unresolved}",
            flush=True,
        )

    def message(self, text: str) -> None:
        print(f"\n!! {text}")


# ---- commands ------------------------------------------------------------
def cmd_validate(cfg: Config, args: argparse.Namespace) -> int:
    load = load_input(cfg)
    print_input_summary(cfg, load)
    print_problem_lines(load)
    if not load.valid_urls:
        print("\nNo valid URLs found.")
        return EXIT_ERROR
    print("\nvalidate made no changes.")
    return EXIT_OK


def _read_only_state(cfg: Config) -> tuple[LoadResult, ProgressStore]:
    load = load_input(cfg)
    store = ProgressStore(cfg.progress_file)
    store.load(quarantine=False)
    return load, store


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    load, store = _read_only_state(cfg)
    print_input_summary(cfg, load, store)
    if store.load_warning:
        print(f"Warning: {store.load_warning}")
    rows = build_rows(load, store)
    print("\nBreakdown:\n" + format_counts(count_statuses(rows)))
    if store.meta.get("updated_at"):
        print(f"\nCheckpoint last updated: {store.meta['updated_at']}")
    print(f"Progress file: {cfg.progress_file}\nResults file:  {cfg.results_file}")
    return EXIT_OK


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    load, store = _read_only_state(cfg)
    rows = write_results_csv(cfg.results_file, load, store)
    print(f"Wrote {len(rows)} rows to {cfg.results_file}\n")
    print(format_counts(count_statuses(rows)))
    for status in args.status or []:
        matching = [r for r in rows if r["status"] == status]
        print(f"\n{status} ({len(matching)}):")
        for r in matching:
            print(f"  {r['url']}" + (f"  -- {r['details']}" if r["details"] else ""))
    return EXIT_OK


def cmd_reset(cfg: Config, args: argparse.Namespace) -> int:
    path = cfg.progress_file
    if not path.exists():
        print("No progress file; nothing to reset.")
        return EXIT_OK
    if not args.yes:
        print(f"This discards all saved progress in {path} (a backup copy is kept).")
        try:
            answer = input("Type RESET to confirm: ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip() != "RESET":
            print("Aborted; nothing changed.")
            return EXIT_ERROR
    backup = path.with_name(f"{path.name}.bak-{now_iso().replace(':', '')}")
    path.replace(backup)
    print(f"Progress reset. Previous checkpoint saved as {backup}")
    try:
        load = load_input(cfg)
        write_results_csv(cfg.results_file, load, ProgressStore(path))
    except InputError:
        pass
    return EXIT_OK


def _launch(cfg: Config) -> BrowserSession:
    print("Launching Chromium (profile: %s)..." % cfg.browser_profile_dir)
    print("The browser profile contains sensitive login data: keep it local and never share it.")
    return BrowserSession(cfg).__enter__()


def cmd_login(cfg: Config, args: argparse.Namespace) -> int:
    session = _launch(cfg)
    try:
        if ensure_authenticated(session.page, cfg):
            print("Authentication: verified. The session is saved in the browser profile.")
            return EXIT_OK
        print("Authentication was not verified.")
        return EXIT_STOPPED
    finally:
        session.close()


def cmd_diagnose(cfg: Config, args: argparse.Namespace) -> int:
    from .page_detection import describe_controls, is_authenticated, take_snapshot

    url = cfg.home_url
    if args.url:
        try:
            url = normalize_url(args.url, cfg.allowed_domains, cfg.strict_book_path)
        except UrlError as exc:
            print(f"Invalid URL: {exc}")
            return EXIT_ERROR
    session = _launch(cfg)
    try:
        driver = PlaywrightDriver(session.page, cfg)
        nav = driver.goto(url)
        state, detail = driver.settle()
        snap = take_snapshot(session.page, cfg)
        print(f"\nURL: {snap.url}\nHTTP status: {nav.status}\nState: {state.value} ({detail})")
        print(f"Snapshot: {snap.summary()}")
        ok, ind = is_authenticated(snap, cfg.require_account_marker)
        print(f"Auth indicators: {ind} -> {'authenticated' if ok else 'NOT verified'}")
        print("\nVisible buttons/links (nothing was clicked; no page content is saved):")
        for it in describe_controls(session.page, cfg):
            mark = f"  <== {it['matches']}" if it["matches"] else ""
            print(f"- <{it['tag']}{' role=' + it['role'] if it['role'] else ''}> text={it['text']!r} "
                  f"aria={it['aria']!r} class={it['cls']!r}\n    in: {it['parents']}{mark}")
        if cfg.keep_browser_open or not sys.stdin.isatty():
            return EXIT_OK
        input("\nPress Enter to close the browser...")
        return EXIT_OK
    finally:
        session.close()


def _select_items(mode: str, args: argparse.Namespace, valid: list[str], store: ProgressStore) -> list[tuple[int, str]]:
    index = {u: i for i, u in enumerate(valid, start=1)}
    if mode == "retry-failed":
        urls = store.unresolved(valid, only=set(args.status) if args.status else None)
    else:
        urls = store.pending(valid)
    if args.limit is not None:
        urls = urls[: max(args.limit, 0)]
    return [(index[u], u) for u in urls]


def cmd_import(cfg: Config, args: argparse.Namespace) -> int:
    mode = args.command
    load = load_input(cfg)
    valid = load.valid_urls
    store = ProgressStore(cfg.progress_file)
    store.load()
    if store.load_warning:
        print(f"Warning: {store.load_warning}")
    print_input_summary(cfg, load, store)
    fp = fingerprint(valid)
    store.set_input(cfg.input_file, fp, len(valid))
    # Write the report immediately so invalid/duplicate lines are recorded even if we stop early.
    write_results_csv(cfg.results_file, load, store)
    if not valid:
        print("\nNo valid URLs to process.")
        return EXIT_ERROR
    items = _select_items(mode, args, valid, store)
    print(f"\nSelected for this run: {len(items)}" + (f" (limit {args.limit})" if args.limit is not None else ""))
    if not items:
        print("Nothing to do." + (" Use `retry-failed` for unresolved URLs." if mode == "run" else ""))
        return EXIT_OK

    session = _launch(cfg)
    exit_code = EXIT_OK
    try:
        if not ensure_authenticated(session.page, cfg):
            print("Authentication not verified; nothing was processed.")
            return EXIT_STOPPED
        print("\nAuthentication: verified\n")
        # Blocking starts only now: login pages and challenges always load fully.
        session.set_resource_blocking(True)
        importer = Importer(PlaywrightDriver(session.page, cfg), store, cfg)

        def resume_after_login() -> bool:
            print("Log in again in the browser window; processing resumes only once login is verified.")
            session.set_resource_blocking(False)  # let login pages/challenges load fully
            ok = ensure_authenticated(session.page, cfg)
            session.set_resource_blocking(ok)
            return ok

        try:
            result = run_batch(importer, items, len(valid), TerminalReporter(store, valid, args.verbose), resume_after_login)
            if result.stopped == "login":
                exit_code = EXIT_STOPPED
            elif result.stopped == "rate_limit":
                exit_code = EXIT_RATE_LIMIT
        except KeyboardInterrupt:
            print("\nInterrupted. Progress up to the last completed URL is saved; re-run to resume.")
            exit_code = EXIT_INTERRUPT
        finally:
            rows = write_results_csv(cfg.results_file, load, store)
            store.save()
        print("\nSummary (whole list):\n" + format_counts(count_statuses(rows)))
        print(f"\nProgress file: {cfg.progress_file}\nResults file:  {cfg.results_file}\nLog file:      {cfg.log_file}")
        if cfg.keep_browser_open and exit_code != EXIT_INTERRUPT and sys.stdin.isatty():
            input("\nBrowser left open for inspection. Press Enter to close it...")
        return exit_code
    finally:
        session.close()


COMMANDS = {
    "run": cmd_import,
    "retry-failed": cmd_import,
    "validate": cmd_validate,
    "status": cmd_status,
    "report": cmd_report,
    "reset": cmd_reset,
    "login": cmd_login,
    "diagnose": cmd_diagnose,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        explicit = args.config is not None
        cfg = load_config(args.config or Path("config.json"), explicit=explicit)
        cfg = apply_overrides(cfg, args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    # validate/status are strictly read-only: do not even create the log directory for them.
    if args.command in ("validate", "status"):
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.ERROR)
    else:
        setup_logging(cfg, args.verbose)
    try:
        return COMMANDS[args.command](cfg, args)
    except (InputError, BrowserError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPT
