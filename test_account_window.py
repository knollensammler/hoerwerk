"""Offline coverage of the browser navigation boundary."""
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from account_window import open_account_window
from browser_login import validate_callback
from core import InvalidIdentifier, PortablePaths
from web_player import open_player_window


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


    def test_test_player_reuses_only_its_own_browser_profile(self):
        with TemporaryDirectory() as temporary, \
             patch("web_player.installed_browser", return_value="/fixture/chromium"), \
             patch("web_player.subprocess.Popen") as launch:
            paths = PortablePaths(Path(temporary))
            open_player_window(paths, "standard")
            open_player_window(paths, "standard", asin="B012345678")
            open_player_window(paths, "other", asin="B012345678")
            first, play, other = [call.args[0] for call in launch.call_args_list]
            self.assertEqual(first[1], play[1])
            self.assertNotEqual(play[1], other[1])
            self.assertEqual(play[1], f"--user-data-dir={paths.account('standard') / 'web-player'}")
            self.assertEqual(first[-1], "--app=https://www.amazon.de/code")
            self.assertEqual(play[-1], "--app=https://www.audible.de/webplayer?asin=B012345678&isSample=false")
            self.assertTrue(all(call.kwargs["shell"] is False for call in launch.call_args_list))
            self.assertFalse((paths.account("standard") / "auth.json").exists())
            launch.reset_mock()
            with self.assertRaises(InvalidIdentifier):
                open_player_window(paths, "standard", asin="B012345678&url=elsewhere")
            launch.assert_not_called()
