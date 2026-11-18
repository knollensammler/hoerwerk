"""Open a separate browser session and hand the Amazon callback back locally."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlsplit

from browser_login import AMAZON_HOSTS, BrowserLoginError, browser_environment, validate_callback


def installed_browser():
    names = ("vivaldi", "vivaldi-stable", "google-chrome", "google-chrome-stable",
             "chromium", "chromium-browser", "msedge")
    for name in names:
        if executable := shutil.which(name):
            return executable
    if sys.platform == "win32":
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            if base := os.environ.get(variable):
                for relative in ("Vivaldi/Application/vivaldi.exe", "Google/Chrome/Application/chrome.exe",
                                 "Microsoft/Edge/Application/msedge.exe"):
                    path = Path(base) / relative
                    if path.is_file():
                        return str(path)
    return None


def open_account_window(url, controller, stop, events):
    """Observe only the main-frame Amazon redirect in a fresh browser context."""
    try:
        from playwright.sync_api import Error, sync_playwright

        executable = installed_browser()
        if not executable:
            events.put("manual")
            return
        with TemporaryDirectory(prefix="hoerwerk-amazon-") as profile, sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                profile, executable_path=executable, headless=False, chromium_sandbox=True,
                env=browser_environment(), timeout=12_000, service_workers="block",
            )
            try:
                if stop.is_set():
                    return
                page = next((p for p in context.pages if p.url == "about:blank"), None)
                if page is None:
                    page = context.new_page()
                page.bring_to_front()
                callback_received = False

                def capture(route):
                    nonlocal callback_received
                    request = route.request
                    target = urlsplit(request.url)
                    if (stop.is_set() or callback_received or target.hostname not in AMAZON_HOSTS
                            or target.path != "/ap/maplanding" or not request.is_navigation_request()
                            or request.frame != page.main_frame):
                        route.continue_()
                        return
                    try:
                        callback = validate_callback(request.url)
                    except BrowserLoginError:
                        route.continue_()
                        return
                    controller.submit_callback(callback)
                    callback_received = True
                    events.put("submitted")
                    route.abort()

                page.route("https://**/ap/maplanding**", capture)
                try:
                    response = page.goto(url, wait_until="commit", timeout=12_000)
                    if response is None and not callback_received:
                        if not stop.is_set():
                            events.put("manual")
                        return
                    if not callback_received and not stop.is_set():
                        page.bring_to_front()
                except Error:
                    if not callback_received and not stop.is_set():
                        events.put("manual")
                    return
                deadline = time.monotonic() + 1200
                while not stop.is_set() and not callback_received and time.monotonic() < deadline:
                    if page.is_closed():
                        events.put("manual")
                        return
                    page.wait_for_timeout(150)
            finally:
                context.close()
    except Exception:
        if not stop.is_set():
            events.put("manual")
