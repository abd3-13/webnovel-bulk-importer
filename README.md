# WebNovel Bulk Library Importer

Automates the manual workflow "open book page -> click **+ ADD TO LIBRARY**" for a list of URLs, using a real Chromium window (Playwright) and **your own manual login**. It does not bypass login, CAPTCHA, rate limits or any other restriction.

> **Status of live-site validation:** the page-detection rules use the labels observed on the live site (`+ ADD TO LIBRARY`, `✓ IN LIBRARY`, `Whoops!` / `We might have some troubles to find out this page.`), but they were developed and tested against a local fake site, not against webnovel.com. Run the **first live test** below (5 URLs) and use `diagnose` if anything is classified `ui_changed`.

## Security

- `browser-profile/` holds your logged-in session. **Never share, upload or commit it.** (`.gitignore` excludes it, plus `data/`, `logs/`, `urls.txt`.)
- The tool never asks for, stores or prints your password, and never reads, logs or exports cookies/session tokens.
- Chromium is driven over a pipe; no remote-debugging port is opened.
- Only validated WebNovel URLs are navigated to. If a page redirects off-site, nothing is clicked there and the URL is recorded as `ui_changed`.
- No HTML or screenshots are saved. Nothing is sent to external services.

## Install (Linux)

```bash
cd webnovel-bulk-import
python3 -m venv .venv && source .venv/bin/activate     # Python 3.11+
pip install -r requirements.txt
python -m playwright install chromium
# If Chromium fails to start due to missing system libraries:
python -m playwright install-deps chromium             # needs sudo
```

## Quick start

```bash
# 1. Put your URLs in urls.txt (one per line; blank lines and # comments ignored)
python bulk_import.py validate        # checks the list; no browser, no changes
python bulk_import.py login           # opens Chromium; log in manually; session is saved
python bulk_import.py run --limit 5   # first live test (modifies your library)
python bulk_import.py report          # inspect data/results.csv, then:
python bulk_import.py run             # process everything still pending
```

## Commands

| Command | Browser? | Modifies library? | Purpose |
|---|---|---|---|
| `validate` | no | no | Validate `urls.txt`; lists invalid/duplicate lines. Writes nothing. |
| `status` | no | no | Counts per status, pending, input-changed warning. Read-only. |
| `report [--status S]` | no | no | Rebuild `results.csv`; optionally list URLs with status `S`. |
| `login` | yes | no | Open Chromium, let you log in, verify the session. |
| `diagnose [URL]` | yes | no | Print page state + visible buttons/links. Clicks nothing. |
| `run [--limit N]` | yes | **yes** | Process up to N **pending** URLs (never-attempted only). |
| `retry-failed [--status S] [--limit N]` | yes | **yes** | Re-process unresolved URLs (see statuses). |
| `reset [--yes]` | no | no | Discard progress after typing `RESET`; a `.bak-<time>` copy is kept. |

Global: `--config FILE`, `-v/--verbose`. Run options: `--input`, `--min-delay`, `--max-delay`, `--retries`, `--keep-open`, `--headless` (login needs a visible window).

`--limit 5` means five **pending** URLs, not the first five lines of the file.

## Result statuses

| Status | Meaning | `run` re-processes? | `retry-failed`? |
|---|---|---|---|
| `added` | Was not in library; clicked; IN LIBRARY observed and (by default) confirmed after a page reload | no | no |
| `already_in_library` | IN LIBRARY found before any click | no | no |
| `unavailable` | Observed "Whoops!"/"troubles to find out this page" message, and no library button | no | no |
| `invalid_url` / `duplicate_url` | Input problems (report only) | - | - |
| `login_required`, `timeout`, `network_error`, `ui_changed`, `verification_failed`, `failed` | Unresolved | no | **yes** |

Rules: a click returning without error is never treated as success; a verified success is never overwritten by a later failure; `verification_failed` means a click may or may not have worked, so the next attempt **inspects the page first** and only clicks again if the book is still not in the library. If it finds IN LIBRARY after an earlier click attempt, it records `added`.

## Configuration (`config.json`)

All keys are optional. Relative paths resolve against the config file's directory. Notable keys: `min_delay_s`/`max_delay_s` (default 3-6 s between books), `retries` (2, exponential backoff, `Retry-After` honoured up to 120 s), `navigation_timeout_s`, `action_timeout_s`, `stability_window_s` (a library state must stay unchanged this long before it is trusted; guards against pages that show ADD before hydrating the real state), `verify_with_reload`, `keep_browser_open`, `headed`, `rate_limit_pause_threshold` (3 consecutive 403/429 responses stop the run).

`labels` holds the regexes used to find controls. `add_to_library`, `in_library`, `unavailable_*` come from your observations; **`login`, `account_marker`, `login_url_pattern` are guesses** and may need adjusting via `diagnose`. `library_scope_selector` can restrict the library-button search to a CSS container if the page contains several matching buttons.

## How it decides what to click

Library controls are located by role (button/link) and accessible name, falling back to an anchored visible-text match. A control counts only if **exactly one** visible match exists; zero or several -> nothing is clicked. The READ button cannot match because the name regexes must contain `add to library` / `in library`. Right before clicking, the page state is re-evaluated.

## Manual live integration test (first run)

1. `python bulk_import.py login` and log in. If it says login is not verified although you are logged in, run `diagnose` (see Troubleshooting).
2. Make a 3-5 line `urls.txt`: one book **not** in your library, `shadow-slave_22196546206090805` (already in library), and, if you have one, an unavailable URL.
3. `python bulk_import.py diagnose <url-not-in-library>`: confirm `State: not_in_library` and that the `<== ADD` line is the right button; repeat for the in-library and unavailable URLs.
4. `python bulk_import.py run --limit 5`, watch the window.
5. `python bulk_import.py report`; check the statuses and open `data/results.csv`. Confirm in the WebNovel UI that added books really appear in your library.
6. Only then run the full list.

## Resume, interruption, recovery

- Progress is written to `data/progress.json` (atomic temp-file + rename + fsync) after every URL, and `data/results.csv` is rebuilt at the same time. `Ctrl-C` is safe; re-run `run` to continue.
- Just before each click a "click issued, outcome unknown" checkpoint is written, so a crash mid-click is recorded as `verification_failed`, never as success. `retry-failed` resolves these by inspecting the book first.
- Editing `urls.txt` is fine: results are matched by canonical URL (https, host lower-cased, no query/fragment/trailing slash). A changed list is reported in `status`/`run`.
- A corrupt checkpoint is moved aside as `progress.json.corrupt-<time>` and the run starts with empty progress (books already in your library will simply be detected as `already_in_library`).
- Login expires mid-run: the run pauses, you log in again in the window, and it resumes after verification.
- Rate limiting: 3 consecutive 403/429 responses stop the run (exit code 4). Wait before resuming; the tool never rotates proxies/accounts.

Exit codes: 0 ok, 1 error, 3 stopped (login not verified), 4 rate limited, 130 interrupted.

## Troubleshooting

- **"Chromium is not installed"** -> `python -m playwright install chromium`.
- **"Could not launch Chromium"** -> another browser instance is using `browser-profile/`; close it.
- **Login not verified though you are logged in** -> `python bulk_import.py diagnose`; look at the "Auth indicators" line and the listed controls; adjust `labels.account_marker` / `labels.login` in `config.json`, or as a last resort set `require_account_marker` to `false`.
- **Many `ui_changed`** -> `diagnose <url>`; if it reports several ADD/IN controls, set `labels.library_scope_selector`; if none, adjust `labels.add_to_library`/`in_library` to match the printed text/aria-label.
- **Google/social login blocked in the automated browser** -> use the site's own email/password login; the tool does not try to hide automation.
- Logs: `logs/importer.log` (`-v` for debug). No cookies/tokens are logged.

## Tests

```bash
pytest -q          # ~50 s; includes real-Chromium tests against a local fake site
pytest -q --ignore=tests/test_browser_integration.py   # fast, no browser (< 1 s)
```
Browser tests are skipped automatically if Chromium cannot be launched. No test touches webnovel.com or your account.

## Layout

```
bulk_import.py            entry point
config.json               settings and label regexes
webnovel_importer/
  cli.py                  argparse commands, terminal output
  config.py               config loading/validation
  input_loader.py         urls.txt parsing, URL validation, dedupe
  models.py               status constants, Outcome
  page_detection.py       Snapshot/classify (pure) + Playwright locators
  browser.py              persistent Chromium session, driver, auth check
  importer.py             per-URL workflow, retries, verification, batch loop
  persistence.py          progress.json with atomic writes
  reporting.py            results.csv and summaries
tests/                    pytest suite
```
