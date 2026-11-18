"""Offline coverage of the browser navigation boundary."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from account_window import open_account_window
from browser_login import validate_callback


class AccountWindowTests(unittest.TestCase):
    def test_only_valid_main_frame_return_reaches_controller(self):
        try:
            import playwright.sync_api
        except ImportError:
            self.skipTest("Playwright nicht installiert")
        main_frame = object()
        callback = "https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=offline-probe"
        controller = Mock()
        controller.submit_callback.side_effect = validate_callback
        browser, page, runtime = Mock(), Mock(), MagicMock()
        page.url = "about:blank"
        browser.pages = [page]
        browser.new_page.return_value = page
        runtime.__enter__.return_value.chromium.launch_persistent_context.return_value = browser
        page.main_frame = main_frame
        events = queue.Queue()
        entries = (
            (callback.replace("www.amazon.de", "amazon.de.example"), main_frame, True),
            (callback, object(), True),
            (callback, main_frame, False),
            (callback + "&openid.oa2.authorization_code=duplicate", main_frame, True),
            (callback, main_frame, True),
        )
        routes = [Mock(request=SimpleNamespace(url=url, frame=frame,
                  is_navigation_request=lambda nav=nav: nav)) for url, frame, nav in entries]

        def navigate(*_args, **_kwargs):
            capture = page.route.call_args.args[1]
            for route in routes:
                capture(route)

        page.goto.side_effect = navigate
        with patch("playwright.sync_api.sync_playwright", return_value=runtime), \
             patch("account_window.installed_browser", return_value="/fixture/chromium"):
            open_account_window("https://www.amazon.de/ap/signin", controller, threading.Event(), events)
        controller.submit_callback.assert_called_once_with(callback)
        for route in routes[:-1]:
            route.continue_.assert_called_once()
        routes[-1].abort.assert_called_once()
        self.assertEqual(events.get_nowait(), "submitted")
        page.bring_to_front.assert_called()
        browser.new_page.assert_not_called()
        browser.close.assert_called_once()
        self.assertTrue(runtime.__enter__.return_value.chromium.launch_persistent_context.call_args.kwargs["chromium_sandbox"])

    def test_missing_browser_produces_manual_fallback(self):
        events = queue.Queue()
        with patch("account_window.installed_browser", return_value=None):
            open_account_window("https://www.amazon.de/ap/signin", Mock(), threading.Event(), events)
        self.assertEqual(events.get_nowait(), "manual")
