"""BrowserSession tests (own module: sync Playwright cannot be nested inside the shared fixture)."""

import threading
from http.server import ThreadingHTTPServer

import pytest

from test_browser_integration import Site, cfg_for, make_handler, marker_cfg, url  # noqa: F401

pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def site():
    s = Site()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
    s.base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield s
    srv.shutdown()


def test_marker_found_even_when_avatar_collapses_with_blocked_image(site, tmp_path):
    from webnovel_importer.browser import BrowserSession
    cfg = marker_cfg(site, browser_profile_dir=tmp_path / "prof", headed=False)
    with BrowserSession(cfg) as s:
        s.set_resource_blocking(True)
        s.page.goto(url(site, "m-1"))
        assert take_snapshot_marker(s.page, cfg) == 1


def take_snapshot_marker(page, cfg):
    from webnovel_importer.page_detection import take_snapshot
    return take_snapshot(page, cfg).account_markers


def test_resource_blocking_blocks_images_only_when_enabled_and_keeps_cache(site, tmp_path):
    from webnovel_importer.browser import BrowserSession
    cfg = cfg_for(site, browser_profile_dir=tmp_path / "prof", headed=False)
    site.hits.clear()
    with BrowserSession(cfg) as s:
        s.page.goto(site.base + "/imgpage?1", wait_until="load")          # blocking off
        assert site.hits["img:/img.png?1"] == 1
        s.set_resource_blocking(True)
        for n in (2, 3):
            s.page.goto(site.base + f"/imgpage?{n}", wait_until="load")   # blocking on
        assert site.hits["img:/img.png?2"] == 0 and site.hits["img:/img.png?3"] == 0
        assert site.hits["app.js"] == 1                                    # HTTP cache still works
        s.set_resource_blocking(False)
        s.page.goto(site.base + "/imgpage?4", wait_until="load")
        assert site.hits["img:/img.png?4"] == 1


def test_resource_blocking_disabled_by_empty_config(site, tmp_path):
    from webnovel_importer.browser import BrowserSession
    cfg = cfg_for(site, browser_profile_dir=tmp_path / "prof", headed=False, block_resource_types=[])
    site.hits.clear()
    with BrowserSession(cfg) as s:
        s.set_resource_blocking(True)
        s.page.goto(site.base + "/imgpage?5", wait_until="load")
        assert site.hits["img:/img.png?5"] == 1
