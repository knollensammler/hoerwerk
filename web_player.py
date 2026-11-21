"""Launch Audible in an optional, profile-scoped Chromium player window."""

import re
import subprocess

from account_window import installed_browser
from browser_login import browser_environment
from core import HoerwerkError, InvalidIdentifier
from device_login import ACTIVATION_URL
from jobs import require_plain_account

_ASIN = re.compile(r"[A-Z0-9]{10}\Z")


def audible_player_url(asin):
    if not isinstance(asin, str) or not _ASIN.fullmatch(asin):
        raise InvalidIdentifier("Der Audible-Titel hat keine gültige Kennung.")
    return f"https://www.audible.de/webplayer?asin={asin}&isSample=false"


def open_player_window(paths, profile, *, asin=None, x11=False):
    """Reuse the player browser session for activation and playback."""
    url = ACTIVATION_URL if asin is None else audible_player_url(asin)
    executable = installed_browser()
    if not executable:
        raise HoerwerkError(
            "Für das Testfenster wird Chrome, Chromium, Edge oder Vivaldi benötigt. "
            "Du kannst weiterhin „Abspielen“ im Standardbrowser verwenden."
        )
    account = require_plain_account(paths, profile)
    browser_data = account / "web-player"
    if browser_data.is_symlink():
        raise HoerwerkError("Der Ordner des Playerprofils darf keine Umleitung sein.")
    browser_data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if browser_data.resolve() != browser_data:
        raise HoerwerkError("Der Ordner des Playerprofils darf keine Umleitung sein.")

    window_options = ["--ozone-platform=x11"] if x11 else []
    if asin is not None:
        window_options.append("--window-size=560,710")
    subprocess.Popen(
        [executable, f"--user-data-dir={browser_data}", "--no-first-run",
         "--no-default-browser-check", "--disable-sync", *window_options, f"--app={url}"],
        shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=browser_environment(),
    )
    return browser_data
